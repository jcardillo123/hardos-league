"""Check the ingested data against ESPN's own published numbers.

    .venv/Scripts/python.exe -m league.verify

Run this after any ingest, especially a 2026 refresh. Everything in the
dashboard is derived from `team_weeks`, and `team_weeks` is derived from a
roster parse - if the parse is wrong, every luck number is wrong with it and
nothing downstream will look obviously broken. The checks here are the ones
that would catch that:

  1. my summed starter points vs ESPN's own matchup score, per team-week
  2. my win/loss record vs the record ESPN reports on the team
  3. optimal >= actual, which must hold by construction
  4. coverage and null counts

Exit status is 0 when everything passes and 1 when a check fails, so this can
gate a refresh.
"""

import sys

from . import db

# ESPN's `totalPoints` on this one matchup disagrees with every player-level
# number ESPN serves for the same team-week. See the note in `ingest.py`.
KNOWN_ARTIFACTS = {(2022, 6, 5): "ESPN totalPoints 130.80 vs its own roster 120.10"}

TOLERANCE = 0.05


def check_scores(conn) -> list[str]:
    """My starter totals against ESPN's matchup scores."""
    rows = conn.execute(
        "SELECT m.season, m.week, m.home_team, m.away_team, m.home_points, "
        "       m.away_points, th.actual AS my_home, ta.actual AS my_away "
        "FROM matchups m "
        "LEFT JOIN team_weeks th ON th.season=m.season AND th.week=m.week "
        "                       AND th.team_id=m.home_team "
        "LEFT JOIN team_weeks ta ON ta.season=m.season AND ta.week=m.week "
        "                       AND ta.team_id=m.away_team "
        "WHERE m.is_bye=0 AND m.home_points IS NOT NULL AND m.home_points > 0"
    ).fetchall()

    per_season, problems, known = {}, [], 0
    for r in rows:
        for side in ("home", "away"):
            espn, mine = r[f"{side}_points"], r[f"my_{side}"]
            if espn is None or mine is None:
                continue
            team = r[f"{side}_team"]
            s = per_season.setdefault(r["season"], [0, 0])
            s[0] += 1
            if abs(espn - mine) <= TOLERANCE:
                continue
            key = (r["season"], r["week"], team)
            if key in KNOWN_ARTIFACTS:
                known += 1
                continue
            s[1] += 1
            problems.append(
                f"{r['season']} wk{r['week']} team {team}: "
                f"ESPN {espn:.2f} vs mine {mine:.2f}"
            )

    print("1. starter totals vs ESPN matchup scores")
    for season in sorted(per_season):
        n, bad = per_season[season]
        mark = "ok" if not bad else f"{bad} MISMATCHED"
        print(f"     {season}  {n:>4} team-weeks  {mark}")
    if known:
        print(f"     {known} known upstream artifact(s) skipped")
    return problems


def check_records(conn) -> list[str]:
    """My W/L from weekly results against the record ESPN reports."""
    rows = conn.execute(
        "SELECT t.season, t.owner_label, t.wins, t.losses, "
        "   SUM(CASE WHEN tw.result='W' THEN 1 ELSE 0 END) my_w, "
        "   SUM(CASE WHEN tw.result='L' THEN 1 ELSE 0 END) my_l "
        "FROM teams t JOIN team_weeks tw "
        "  ON tw.season=t.season AND tw.team_id=t.team_id "
        " AND tw.is_playoff=0 AND tw.result IS NOT NULL "
        "GROUP BY t.season, t.team_id"
    ).fetchall()
    problems = [
        f"{r['season']} {r['owner_label']}: ESPN {r['wins']}-{r['losses']} "
        f"vs mine {r['my_w']}-{r['my_l']}"
        for r in rows
        if (r["wins"], r["losses"]) != (r["my_w"], r["my_l"])
    ]
    print(f"\n2. records: {len(rows)} team-seasons, {len(problems)} disagree")
    return problems


def check_optimal(conn) -> list[str]:
    """`optimal` can never be below `actual` - the actual lineup was available."""
    v = conn.execute(
        "SELECT COUNT(*) n, "
        "  SUM(CASE WHEN optimal < actual - 0.01 THEN 1 ELSE 0 END) bad, "
        "  ROUND(AVG(actual),1) a, ROUND(AVG(optimal),1) o, "
        "  ROUND(AVG(optimal-actual),1) gap, ROUND(MAX(optimal-actual),1) worst "
        "FROM team_weeks WHERE actual IS NOT NULL AND actual > 0"
    ).fetchone()
    print(f"\n3. optimal lineups over {v['n']} team-weeks")
    print(f"     mean actual {v['a']}, mean optimal {v['o']}, "
          f"mean left on bench {v['gap']} (worst {v['worst']})")
    if v["bad"]:
        return [f"{v['bad']} team-weeks have optimal < actual - the solver or "
                "the IR exclusion is wrong"]
    return []


def check_coverage(conn) -> list[str]:
    """Nulls where there should be none, and weeks that went missing."""
    problems = []
    print("\n4. coverage")
    for r in conn.execute(
        "SELECT s.season, s.n_teams, s.complete, "
        "  (SELECT COUNT(DISTINCT week) FROM team_weeks w WHERE w.season=s.season) wk, "
        "  (SELECT COUNT(*) FROM lineups l WHERE l.season=s.season) n, "
        "  (SELECT COUNT(*) FROM lineups l WHERE l.season=s.season "
        "     AND l.position IS NULL) np, "
        "  (SELECT COUNT(*) FROM lineups l WHERE l.season=s.season "
        "     AND l.points IS NULL) nq "
        "FROM seasons s ORDER BY s.season"
    ):
        state = "complete" if r["complete"] else "in progress"
        print(f"     {r['season']}  {r['n_teams']} teams, {r['wk']} weeks, "
              f"{r['n']} lineup rows  ({state})")
        if r["np"]:
            problems.append(f"{r['season']}: {r['np']} lineup rows have no position")
        if r["nq"]:
            problems.append(f"{r['season']}: {r['nq']} lineup rows have no points")
    return problems


def main() -> int:
    with db.session() as conn:
        problems = (
            check_scores(conn)
            + check_records(conn)
            + check_optimal(conn)
            + check_coverage(conn)
        )

    print()
    if problems:
        print(f"FAILED - {len(problems)} problem(s):")
        for p in problems[:20]:
            print(f"  - {p}")
        if len(problems) > 20:
            print(f"  ... and {len(problems) - 20} more")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
