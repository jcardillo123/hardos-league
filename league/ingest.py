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

    # Transactions last, because reconstructing what a trade moved and what it
    # was worth reads the `lineups` rows written above.
    txs = fetch_transactions(session, season, max(weeks) if weeks else 17,
                             refresh=refresh)
    mv = build_moves(conn, season, txs)
    print(f"  {season} transactions: {mv['transactions']} rows, "
          f"{mv['trades']} trades reconstructed from {mv['agreed']} agreed "
          f"({mv['accepts']} accept rows), {mv['pickups']} pickups",
          file=sys.stderr)

    return {
        "season": season, "teams": len(teams), "matchups": len(schedule),
        "weeks": len(weeks_to_pull), "lineup_rows": lineup_rows,
        "team_weeks": tw, "lineup": lineup, "flex": flex_slots,
        "moves": mv,
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


# --------------------------------------------------------------------------- #
#  transactions: trades and waiver pickups
# --------------------------------------------------------------------------- #

# ESPN serves the transaction log one scoring period at a time. `view=
# mTransactions2` on its own returns no `transactions` key at all for a past
# season, and no `x-fantasy-filter` widens it - limit/offset, a type filter and
# `filterScoringPeriodIds` were all tried and all return the same nothing. So
# the log is swept a week at a time. Every response goes through the same cache
# as the rosters, so the sweep costs 19 requests the first time and nothing
# after that.

# The two kinds that put a player on a roster from outside it. Both are reported
# as pickups.
POOL_KINDS = ("WAIVER", "FREEAGENT")

# Every kind that can move a player without it being a trade. Used only to
# exclude players from the trade reconstruction, so it is deliberately wider
# than POOL_KINDS: `ROSTER` carries 268 drops and `DRAFT` the draft itself, and
# a player either of them touched must not be read as having been traded.
NON_TRADE_KINDS = ("WAIVER", "FREEAGENT", "ROSTER", "DRAFT")


def fetch_transactions(session, season: int, last_week: int,
                       refresh: bool = False) -> dict:
    """Every transaction ESPN will serve for a season, keyed by its id.

    Scoring period 0 is included: that is where preseason and draft-day moves
    land. The same transaction is returned by more than one week in some cases,
    hence the keying rather than a list.
    """
    out: dict = {}
    for week in range(0, max(last_week, 17) + 2):
        d = espn.fetch(session, season, ["mTransactions2"], week=week,
                       refresh=refresh)
        for t in (d or {}).get("transactions") or []:
            if t.get("id"):
                out.setdefault(t["id"], t)
    return out


# Why the roster, and not the log, decides what moved.
#
# A `TRADE_ACCEPT` row is supposed to carry an `items` list naming each player
# and the teams either side of him. In this league 65 trades produce 74 accept
# rows, and only 10 of the 65 yield any items - 9 because the proposal that
# `relatedTransactionId` points at happens to still be served, 1 from the accept
# itself. The other 55 have no item data anywhere, in any view, at any week.
#
# Of those 10, 8 agree with the roster diff exactly, 1 is short a player who was
# cut the moment he arrived, and 1 contradicts the rosters completely: its
# `relatedTransactionId` points at a proposal that was superseded, so the items
# describe a trade that never happened. So the log is not merely incomplete, it
# is occasionally wrong, and the roster is the arbiter even where both exist.
#
# What the log is good for is identity and timing. Accept rows that share a
# `relatedTransactionId` are one trade, which is how 74 rows reduce to 65, and
# the `TRADE_UPHOLD` and `TRADE_VETO` rows alongside them are individual
# managers' votes rather than outcomes - trades keyed to a veto row still moved
# players, so a veto here is a vote cast and not a trade undone.
#
# The alignment is measured, not assumed. A transaction at scoring period W
# appears in week W's roster snapshot: of the accepts that do carry items, 8 of
# 10 agree with the W-vs-W-1 diff completely and none agree at W+1 or W+2, and
# waiver adds first become visible at W in 1430 cases against 11 at W+1. So the
# move week is also the first week the player can score for his new team.
#
# Two things a roster diff cannot see on its own, both handled below:
#
#   - A player dropped by one team and claimed by another inside the same week
#     is indistinguishable from a trade. Add and drop transactions do carry
#     their items, so every player one of them touched that week is taken out
#     before the trade grouping runs.
#   - A player received and cut before the next snapshot never appears on his
#     new roster at all. His drop row names a `fromTeamId` he was not on last
#     week, which is enough to place him, and recovers 4 such players.
#
# Players are then grouped into trades by unordered team pair rather than by the
# component they form. Grouping by component fuses every trade a busy manager
# made in one week into a single 16-player monster - 2025 week 4 collapsed six
# trades into two - whereas pairing reproduces the log's own count of 65 almost
# exactly, at 67. The cost is that a genuine three-way trade is recorded as its
# legs, which conserves every player and every point but splits one trade into
# three rows. 15 of the 67 pairs show players moving one way only, each of them
# either such a leg or a cut that could not be recovered.
def build_moves(conn, season: int, txs: dict) -> dict:
    """Write one `moves` row per player who changed hands, with his value."""
    conn.execute("DELETE FROM moves WHERE season = ?", (season,))

    # Two indexes over the same rows: one per player to walk his season
    # forwards and backwards, one per week to diff two Sundays against each
    # other. A player is on at most one roster in a week, so both are unique.
    by_player: dict = {}
    by_week: dict = {}
    for r in conn.execute(
        "SELECT week, team_id, player_id, points, started "
        "FROM lineups WHERE season = ?", (season,)
    ):
        by_player.setdefault(r["player_id"], {})[r["week"]] = {
            "team": r["team_id"], "points": r["points"] or 0.0,
            "started": r["started"],
        }
        by_week.setdefault(r["week"], {})[r["player_id"]] = r["team_id"]
    weeks = sorted(by_week)

    # Names come from any season, not just this one: a player picked up and
    # dropped between two Sunday snapshots never appears in this season's
    # rosters, and a name from 2023 is still his name.
    names = {r["player_id"]: (r["player_name"], r["position"])
             for r in conn.execute(
                 "SELECT player_id, player_name, position FROM lineups "
                 "GROUP BY player_id")}

    def before(pid: int, week: int) -> float:
        """Points scored this season before the move.

        Only counts weeks he was on somebody's roster here - points he scored
        while sitting in the free pool are not in `lineups` and cannot be. For
        a trade that is the whole story; for a waiver pickup off the pool it is
        why `pts_before` is often zero.
        """
        return round(sum(c["points"] for w, c in
                         by_player.get(pid, {}).items() if w < week), 2)

    def after(pid: int, team: int, week: int) -> tuple[float, float, int]:
        """What he scored for that team from the move week until he left.

        Stops at the first week he is not on that roster, so a player who was
        traded on and a player who was cut both stop contributing the moment
        they stop being this manager's problem.
        """
        mine = by_player.get(pid, {})
        pts = started = 0.0
        held = 0
        for w in weeks:
            if w < max(week, 1):
                continue
            c = mine.get(w)
            if not c or c["team"] != team:
                break
            held += 1
            pts += c["points"]
            if c["started"]:
                started += c["points"]
        return round(pts, 2), round(started, 2), held

    rows = []

    # ---- waiver and free-agent adds, straight from the log --------------- #
    #
    # `moved` collects every (week, player) that a non-trade transaction touched
    # and `cut` the drops that name the team doing the dropping, both of which
    # the trade reconstruction below needs.
    moved, cut = set(), {}
    for t in txs.values():
        if t.get("type") not in NON_TRADE_KINDS:
            continue
        week = t.get("scoringPeriodId") or 0
        executed = t.get("status") == "EXECUTED"
        for it in t.get("items") or []:
            moved.add((week, it.get("playerId")))
            if it.get("type") == "DROP" and it.get("fromTeamId"):
                cut.setdefault((week, it["playerId"]), it["fromTeamId"])
            if not executed or it.get("type") != "ADD" or not it.get("toTeamId"):
                continue
            rows.append((week, t["type"], it["playerId"],
                         it.get("fromTeamId") or None, it["toTeamId"],
                         t.get("bidAmount") or 0, None))

    # ---- trades, from the roster diff at every week a trade was agreed --- #
    trade_weeks = sorted({t.get("scoringPeriodId") or 0 for t in txs.values()
                          if t.get("type") == "TRADE_ACCEPT"})
    unresolved, one_way = [], 0
    for week in trade_weeks:
        # Needs a snapshot either side of the boundary. A draft-day trade at
        # period 0 has nothing before it, so it is reported rather than guessed.
        cur, prev = by_week.get(week), by_week.get(week - 1)
        if not cur or not prev:
            unresolved.append(week)
            continue
        edges = [(pid, prev[pid], team) for pid, team in cur.items()
                 if prev.get(pid) is not None and prev[pid] != team
                 and (week, pid) not in moved]

        # A player who was traded for and cut the same week is on nobody's
        # roster by Sunday, but the drop row names the team that cut him and
        # last week's roster names the team that sent him.
        for (w, pid), by_team in cut.items():
            if w != week or pid in cur:
                continue
            was = prev.get(pid)
            if was is not None and was != by_team:
                edges.append((pid, was, by_team))

        if not edges:
            unresolved.append(week)
            continue

        # One trade per pair of teams. See the note above for why this beats
        # grouping by connected component.
        pairs: dict = {}
        for pid, frm, to in edges:
            pairs.setdefault(tuple(sorted((frm, to))), []).append((pid, frm, to))
        for (a, b), es in sorted(pairs.items()):
            if len({(frm, to) for _, frm, to in es}) == 1:
                one_way += 1
            tid = f"{season}w{week}-{a}-{b}"
            for pid, frm, to in es:
                rows.append((week, "TRADE", pid, frm, to, 0, tid))

    n = 0
    for week, kind, pid, frm, to, bid, tid in rows:
        name, pos = names.get(pid, (None, None))
        pts, started, held = after(pid, to, week)
        conn.execute(
            "INSERT OR REPLACE INTO moves (season, week, kind, player_id, "
            "player_name, position, from_team, to_team, bid_amount, trade_id, "
            "pts_before, pts_after, started_after, weeks_after) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (season, week, kind, pid, name, pos, frm, to, bid, tid,
             before(pid, week), pts, started, held),
        )
        n += 1

    # `agreed` is what the log thinks happened: accept rows sharing a
    # `relatedTransactionId` are one trade. `trades` is what the rosters show
    # actually happened. They should be close, and `verify` checks that they are.
    accepts = [t for t in txs.values() if t.get("type") == "TRADE_ACCEPT"]
    agreed = {t.get("relatedTransactionId") or t["id"] for t in accepts}
    return {
        "transactions": len(txs), "moves": n,
        "trades": len({r[6] for r in rows if r[6]}),
        "accepts": len(accepts), "agreed": len(agreed),
        "trade_weeks_unresolved": len(unresolved), "one_way": one_way,
        "pickups": sum(1 for r in rows if r[1] in POOL_KINDS),
    }


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
        mv = r.get("moves") or {}
        print(f"  {r['season']}: {r['teams']} teams, {r['matchups']} matchups, "
              f"{r['weeks']} weeks, {r['lineup_rows']} lineup rows, "
              f"{r['team_weeks']} team-weeks, "
              f"{mv.get('trades', 0)} trades, {mv.get('pickups', 0)} pickups")
    print("=" * 62)
    lin = next((r.get("lineup") for r in results if r.get("lineup")), None)
    if lin:
        flex = next(r.get("flex") for r in results if r.get("lineup"))
        print(f"  lineup read from ESPN: {lin} + {flex} FLEX")


if __name__ == "__main__":
    main()
