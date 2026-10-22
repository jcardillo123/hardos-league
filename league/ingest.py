"""Pull league history from ESPN into data/league.db.

    .venv/Scripts/python.exe -m league.ingest              # all seasons, cached
    .venv/Scripts/python.exe -m league.ingest --refresh    # re-hit ESPN
    .venv/Scripts/python.exe -m league.ingest --seasons 2026 --refresh

Safe to re-run: each season is replaced as a unit, so a failed 2026 refresh
cannot damage the completed seasons.
"""

import argparse
import json
import sys

from . import db, espn
from .config import (
    BENCH_SLOTS, CURRENT_SEASON, FLEX_POSITIONS, FLEX_SLOTS, LINEUP,
    POSITION, PRO_TEAM, SEASONS, SLOT, STAT_ACTUAL, STAT_PROJECTED,
)
from .stats import all_play, bench_total, optimal_lineup, started_total

# ESPN's lineupSlotCounts is keyed by slot id as a string. Translate to the
# position labels the rest of the app uses, so a league that changes its roster
# shape is picked up rather than silently scored against last year's.
def _lineup_from_slots(slots: dict) -> tuple[dict, int]:
    lineup, flex = {}, 0
    for sid, count in (slots or {}).items():
        if not count:
            continue
        label = SLOT.get(int(sid), str(sid))
        if label == "FLEX":
            flex += int(count)
        elif label in ("QB", "RB", "WR", "TE", "K", "DST"):
            lineup[label] = lineup.get(label, 0) + int(count)
    return (lineup or dict(LINEUP)), (flex or FLEX_SLOTS)


# Rosters come from `rosterForCurrentScoringPeriod`, not `rosterForMatchupPeriod`.
# The latter looks like the right field by name and is not: it carries only the
# ten starters with no bench, and it stamps every one of them `lineupSlotId: 0`,
# so it can say neither who sat nor what slot anyone filled.
#
# Verified against ESPN's own scores on 788 team-weeks: 787 match to the cent.
# The one that does not is 2022 wk6 team 5, where the matchup's `totalPoints`
# reads 130.80 while ESPN's `pointsByScoringPeriod`, its roster's
# `appliedStatTotal`, and the sum of its own player rows all read 120.10. The
# 10.70 gap is not reconstructible from any player data ESPN serves, so it is an
# upstream artifact - a stat correction applied to the matchup total and never to
# the archived roster. It does not change the result (both numbers beat the
# opponent's 114.38). `league.verify` re-checks this after every ingest.
def _player_points(entry: dict, week: int) -> tuple[float, float]:
    """(actual, projected) for this player in this week.

    `appliedStatTotal` on the pool entry is the actual total, but the projection
    only exists inside the stats list, so both are read from there when the
    week's stat lines are present and the total is used as the fallback.
    """
    pool = entry.get("playerPoolEntry") or {}
    actual = pool.get("appliedStatTotal")
    projected = None
    for line in (pool.get("player") or {}).get("stats") or []:
        if line.get("scoringPeriodId") != week:
            continue
        total = line.get("appliedTotal")
        if line.get("statSourceId") == STAT_ACTUAL and total is not None:
            actual = total
        elif line.get("statSourceId") == STAT_PROJECTED and total is not None:
            projected = total
    return (
        round(float(actual), 2) if actual is not None else None,
        round(float(projected), 2) if projected is not None else None,
    )


def ingest_season(conn, session, season: int, refresh: bool = False) -> dict:
    data = espn.fetch(
        session, season, ["mSettings", "mTeam", "mMatchupScore"], refresh=refresh
    )
    if not data:
        return {"season": season, "skipped": "no data"}

    settings = data.get("settings") or {}
    sched_cfg = settings.get("scheduleSettings") or {}
    slots = (settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}
    lineup, flex_slots = _lineup_from_slots(slots)
    teams = data.get("teams") or []
    owners = espn.owner_labels(data)

    schedule = data.get("schedule") or []
    weeks = sorted({m["matchupPeriodId"] for m in schedule
                    if m.get("matchupPeriodId")})
    reg_weeks = sched_cfg.get("matchupPeriodCount")

    db.replace_season(conn, season)
    conn.execute(
        "INSERT INTO seasons (season, league_name, n_teams, reg_weeks, "
        "total_weeks, playoff_teams, scoring_type, roster_slots, complete) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (season, settings.get("name"), len(teams), reg_weeks,
         max(weeks) if weeks else 0, sched_cfg.get("playoffTeamCount"),
         (settings.get("scoringSettings") or {}).get("scoringType"),
         json.dumps({SLOT.get(int(k), k): v for k, v in slots.items() if v}),
         0 if season >= CURRENT_SEASON else 1),
    )

    for t in teams:
        own = owners.get(t["id"], {})
        rec = ((t.get("record") or {}).get("overall")) or {}
        conn.execute(
            "INSERT INTO teams (season, team_id, owner_guid, owner_label, "
            "team_name, abbrev, division_id, wins, losses, ties, points_for, "
            "points_against, final_rank, playoff_seed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (season, t["id"], own.get("owner_guid"), own.get("owner_label"),
             t.get("name"), t.get("abbrev"), t.get("divisionId"),
             rec.get("wins"), rec.get("losses"), rec.get("ties"),
             rec.get("pointsFor"), rec.get("pointsAgainst"),
             t.get("rankCalculatedFinal") or t.get("rankFinal"),
             t.get("playoffSeed")),
        )

    played_weeks = []
    for m in schedule:
        week = m.get("matchupPeriodId")
        home, away = m.get("home") or {}, m.get("away") or {}
        hp, ap = home.get("totalPoints"), away.get("totalPoints")
        is_playoff = bool(reg_weeks and week and week > reg_weeks)
        # A playoff bye shows up as a matchup with only one side.
        is_bye = not (home.get("teamId") and away.get("teamId"))
        winner = m.get("winner")
        conn.execute(
            "INSERT OR REPLACE INTO matchups (season, week, matchup_id, "
            "home_team, away_team, home_points, away_points, winner, "
            "playoff_tier, is_playoff, is_bye) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (season, week, m.get("id"), home.get("teamId"), away.get("teamId"),
             hp, ap, winner, m.get("playoffTierType"),
             int(is_playoff), int(is_bye)),
        )
        if hp or ap:
            played_weeks.append(week)

    # ---- weekly rosters, one request per week ---------------------------- #
    weeks_to_pull = sorted(set(played_weeks)) or weeks
    lineup_rows = 0
    for week in weeks_to_pull:
        wk = espn.fetch(session, season, ["mBoxscore", "mMatchupScore"],
                        week=week, refresh=refresh)
        if not wk:
            continue
        for m in wk.get("schedule") or []:
            if m.get("matchupPeriodId") != week:
                continue
            for side in ("home", "away"):
                sd = m.get(side) or {}
                team_id = sd.get("teamId")
                if not team_id:
                    continue
                roster = sd.get("rosterForCurrentScoringPeriod") or {}
                for entry in roster.get("entries") or []:
                    pool = entry.get("playerPoolEntry") or {}
                    player = pool.get("player") or {}
                    slot = entry.get("lineupSlotId")
                    actual, projected = _player_points(entry, week)
                    conn.execute(
                        "INSERT OR REPLACE INTO lineups (season, week, "
                        "team_id, player_id, player_name, position, pro_team, "
                        "lineup_slot, slot_label, started, points, projected) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (season, week, team_id, entry.get("playerId"),
                         player.get("fullName"),
                         POSITION.get(player.get("defaultPositionId")),
                         PRO_TEAM.get(player.get("proTeamId")),
                         slot, SLOT.get(slot, str(slot)),
                         int(slot not in BENCH_SLOTS), actual, projected),
                    )
                    lineup_rows += 1
        print(f"  {season} wk{week:<2} rosters pulled", file=sys.stderr)

    tw = build_team_weeks(conn, season, lineup, flex_slots)
    return {
        "season": season, "teams": len(teams), "matchups": len(schedule),
        "weeks": len(weeks_to_pull), "lineup_rows": lineup_rows,
        "team_weeks": tw, "lineup": lineup, "flex": flex_slots,
    }


def build_team_weeks(conn, season: int, lineup: dict, flex_slots: int) -> int:
    """Collapse lineups + matchups into one row per team per week."""
    conn.execute("DELETE FROM team_weeks WHERE season = ?", (season,))

    opp, playoff = {}, {}
    for m in conn.execute(
        "SELECT week, home_team, away_team, is_playoff FROM matchups "
        "WHERE season = ? AND is_bye = 0", (season,)
    ):
        opp[(m["week"], m["home_team"])] = m["away_team"]
        opp[(m["week"], m["away_team"])] = m["home_team"]
        playoff[(m["week"], m["home_team"])] = m["is_playoff"]
        playoff[(m["week"], m["away_team"])] = m["is_playoff"]

    rows = conn.execute(
        "SELECT season, week, team_id, position, lineup_slot, started, points "
        "FROM lineups WHERE season = ?", (season,)
    ).fetchall()

    rosters = {}
    for r in rows:
        rosters.setdefault((r["week"], r["team_id"]), []).append({
            "position": r["position"], "lineup_slot": r["lineup_slot"],
            "started": r["started"], "points": r["points"],
        })

    scored = {}
    for (week, team_id), players in rosters.items():
        best, _ = optimal_lineup(players, lineup, flex_slots, FLEX_POSITIONS)
        scored[(week, team_id)] = {
            "actual": started_total(players),
            "optimal": best,
            "bench": bench_total(players),
        }

    # all-play needs every score in the week, so it runs after the loop above.
    by_week = {}
    for (week, team_id), s in scored.items():
        by_week.setdefault(week, {})[team_id] = s["actual"]

    n = 0
    for (week, team_id), s in scored.items():
        others = [v for tid, v in by_week.get(week, {}).items() if tid != team_id]
        w, l, t = all_play(s["actual"], others)
        o = opp.get((week, team_id))
        opp_actual = by_week.get(week, {}).get(o)
        result = None
        if opp_actual is not None:
            result = ("W" if s["actual"] > opp_actual
                      else "L" if s["actual"] < opp_actual else "T")
        conn.execute(
            "INSERT OR REPLACE INTO team_weeks (season, week, team_id, "
            "opp_team, actual, optimal, bench_points, opp_actual, result, "
            "is_playoff, all_play_w, all_play_l, all_play_t) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (season, week, team_id, o, s["actual"], s["optimal"], s["bench"],
             opp_actual, result, int(playoff.get((week, team_id)) or 0),
             w, l, t),
        )
        n += 1
    return n


def refresh_owners(conn):
    """Rebuild the owner identity table from whatever seasons are loaded."""
    conn.execute("DELETE FROM owners")
    conn.execute(
        "INSERT INTO owners (owner_guid, label, first_seen, last_seen, seasons) "
        "SELECT owner_guid, "
        "       (SELECT owner_label FROM teams t2 WHERE t2.owner_guid = "
        "        t.owner_guid ORDER BY season DESC LIMIT 1), "
        "       MIN(season), MAX(season), COUNT(DISTINCT season) "
        "FROM teams t WHERE owner_guid IS NOT NULL GROUP BY owner_guid"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs="*", type=int, default=list(SEASONS))
    ap.add_argument("--refresh", action="store_true",
                    help="re-hit ESPN instead of using the cached responses")
    args = ap.parse_args()

    if not espn.has_credentials():
        print("No ESPN_S2 / ESPN_SWID in .env - private league reads will 401.",
              file=sys.stderr)

    db.init_db()
    session = espn.build_session()
    results = []
    with db.session() as conn:
        for season in sorted(args.seasons):
            print(f"season {season}...", file=sys.stderr)
            results.append(ingest_season(conn, session, season, args.refresh))
        refresh_owners(conn)

    print("\n" + "=" * 62)
    for r in results:
        if r.get("skipped"):
            print(f"  {r['season']}: skipped ({r['skipped']})")
            continue
        print(f"  {r['season']}: {r['teams']} teams, {r['matchups']} matchups, "
              f"{r['weeks']} weeks, {r['lineup_rows']} lineup rows, "
              f"{r['team_weeks']} team-weeks")
    print("=" * 62)
    lin = next((r.get("lineup") for r in results if r.get("lineup")), None)
    if lin:
        flex = next(r.get("flex") for r in results if r.get("lineup"))
        print(f"  lineup read from ESPN: {lin} + {flex} FLEX")


if __name__ == "__main__":
    main()
