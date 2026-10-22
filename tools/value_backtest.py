"""Backtest the VALUE model against what players actually scored.

`tools/backtest.py` validates the opponent model - who gets picked next. This
validates the other half: whether the numbers the board ranks players *by* bear
any relationship to the points those players went on to score.

## What can and cannot be tested here

Testable, because the inputs survive in `adp_history` (2022-2025) and the
outcomes survive in nflverse:

  - which preseason signal orders players best (`rank` / `adp` / `espn_adp`)
  - whether the static replacement levels sit where the value curve says
  - whether the 0.45/0.45/0.10 flex split matches who actually earns flex points
  - whether ranking on VOR beats ranking on raw ADP, in realized points

NOT testable: the projections themselves. The live model computes VOR from
RotoBaller/FantasyPros projected points, and no historical projection files
exist to replay. So this validates the *structure* built on top of a preseason
ordering - the baselines, the VOR transform, the signal choice - and not the
accuracy of the 2026 point estimates feeding it today.

That distinction matters when reading the output: a good score here means the
machinery is sound, not that this year's projections are right.

## Leakage

The signal comparison is leak-free by construction - preseason inputs, in-season
outcomes, nothing fitted.

The draft test does fit something: expected points by positional rank, used as a
stand-in for the projections we don't have. That is fitted on seasons < N only,
so 2022 is unscoreable and acts purely as training data.

## Scoring

PPR from nflverse, to keep one consistent scale across positions. A player in
`adp_history` with no nflverse stats row scored nothing that season and is
counted as 0.0 rather than dropped - dropping them would quietly delete every
preseason pick that busted, which is most of what we are trying to measure.

K and DST are excluded throughout: nflverse player stats do not carry them.

Usage:
    .venv/Scripts/python.exe tools/value_backtest.py
    .venv/Scripts/python.exe tools/value_backtest.py --seasons 2024 2025
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db                                              # noqa: E402
from app.ingest import load_season_stats, replacement_levels    # noqa: E402

SKILL = ("QB", "RB", "WR", "TE")
SIGNALS = ("rank", "adp", "espn_adp")
SIGNAL_LABEL = {
    "rank": "ECR (consensus rank)",
    "adp": "consensus ADP",
    "espn_adp": "ESPN ADP",
}


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #

def realized_points(season: int) -> dict[tuple[str, str], float]:
    """PPR points actually scored, keyed by (name_key, position)."""
    df = load_season_stats(season)
    out = {}
    for r in df.itertuples():
        if r.position in SKILL:
            out[(r.name_key, r.position)] = float(r.pts_ppr or 0.0)
    return out


def preseason(conn, season: int) -> list[dict]:
    """Every skill player carrying a preseason signal, with what he scored."""
    rows = [
        dict(r) for r in conn.execute(
            "SELECT name_key, position, player_name, rank, adp, espn_adp "
            "FROM adp_history WHERE season = ? AND position IN "
            "('QB','RB','WR','TE')",
            (season,),
        )
    ]
    points = realized_points(season)
    for r in rows:
        # Absent from nflverse means he did not record a stat all year. That is
        # a real zero, not a missing value - see the module docstring.
        r["points"] = points.get((r["name_key"], r["position"]), 0.0)
    return rows


# --------------------------------------------------------------------------- #
# stats helpers - no scipy dependency
# --------------------------------------------------------------------------- #

def _ranks(values: list[float]) -> list[float]:
    """Average ranks, ties shared."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[order[k]] = shared
        i = j + 1
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation, computed as Pearson on ranks."""
    if len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return None if dx == 0 or dy == 0 else num / (dx * dy)


# --------------------------------------------------------------------------- #
# 1. which signal orders players best
# --------------------------------------------------------------------------- #

def signal_report(rows: list[dict]) -> dict:
    """Spearman of each preseason signal against realized points.

    Negated, because every signal is "lower is better" while points are "higher
    is better" - so a positive number here means the signal was right.
    """
    out = {}
    for sig in SIGNALS:
        have = [r for r in rows if r.get(sig) is not None]
        entry = {"n": len(have)}
        if have:
            entry["all"] = spearman(
                [-float(r[sig]) for r in have], [r["points"] for r in have]
            )
            for pos in SKILL:
                sub = [r for r in have if r["position"] == pos]
                entry[pos] = spearman(
                    [-float(r[sig]) for r in sub], [r["points"] for r in sub]
                ) if len(sub) >= 3 else None
        out[sig] = entry
    return out


# --------------------------------------------------------------------------- #
# 2. are the replacement levels in the right place
# --------------------------------------------------------------------------- #

def replacement_report(rows: list[dict], levels: dict[str, int]) -> dict:
    """Compare the configured baseline against the realized points curve.

    The configured level says "the Nth best is free". If that is right, the
    curve should already be flat there: taking one more player at that position
    should buy you very little. `slope` is the points given up between the
    baseline and 6 spots later, per spot. A steep slope means real value is
    still on the table and the baseline is set too shallow.

    Read `slope_pct`, not `slope`, when comparing positions. Raw slopes are in
    points, and the positions are not on the same points scale - a QB baseline
    scores about twice what an RB baseline does, so QB's raw slope runs about
    twice as steep at an equally flat part of the curve. Reading the raw column
    across a row once produced a confident, wrong diagnosis that QB12 was too
    shallow; normalised, QB sits with the rest. `slope_pct` is the slope as a
    share of the points available at the baseline, which is comparable.
    """
    out = {}
    for pos in SKILL:
        pts = sorted(
            (r["points"] for r in rows if r["position"] == pos), reverse=True
        )
        level = levels.get(pos)
        if not level or len(pts) < level + 6:
            continue
        at = pts[level - 1]
        after = pts[level + 5]
        out[pos] = {
            "level": level,
            "points_at_level": round(at, 1),
            "points_6_later": round(after, 1),
            "slope_per_spot": round((at - after) / 6, 1),
            "slope_pct": round(100 * ((at - after) / 6) / at, 2) if at else 0.0,
            "top_of_position": round(pts[0], 1),
        }
    return out


# --------------------------------------------------------------------------- #
# 3. does the flex split match who actually earns flex points
# --------------------------------------------------------------------------- #

def flex_report(rows: list[dict], levels: dict[str, int], n_flex: int) -> dict:
    """Who actually fills flex, by realized points.

    Takes the players ranked just below each position's dedicated starter count,
    pools them, and asks which positions the top `n_flex` of that pool came
    from. That is the empirical version of the hardcoded 0.45/0.45/0.10.
    """
    pool = []
    for pos in ("RB", "WR", "TE"):
        pts = sorted(
            (r["points"] for r in rows if r["position"] == pos), reverse=True
        )
        # Dedicated starters only - strip the flex allowance back out, since
        # that is the thing being measured.
        dedicated = levels.get(pos, 0)
        if pos == "RB":
            dedicated = levels["RB"] - round(n_flex * 0.45)
        elif pos == "WR":
            dedicated = levels["WR"] - round(n_flex * 0.45)
        else:
            dedicated = levels["TE"] - round(n_flex * 0.10)
        pool.extend((p, pos) for p in pts[max(dedicated, 0):])
    pool.sort(reverse=True)
    winners = pool[:n_flex]
    counts = {pos: sum(1 for _, p in winners if p == pos) for pos in ("RB", "WR", "TE")}
    total = max(len(winners), 1)
    return {pos: round(counts[pos] / total, 3) for pos in counts}


# --------------------------------------------------------------------------- #
# 4. does VOR beat raw ADP as a draft ordering
# --------------------------------------------------------------------------- #

def expected_by_rank(train: list[list[dict]]) -> dict[str, list[float]]:
    """Mean realized points by positional rank, fitted on earlier seasons.

    This is the stand-in for the projections we cannot replay: if you knew
    nothing about a player except that he was the 14th WR off the board, this
    is what history says to expect. Fitted only on seasons before the one being
    scored.
    """
    buckets: dict[str, list[list[float]]] = {p: [] for p in SKILL}
    for rows in train:
        for pos in SKILL:
            have = sorted(
                (r for r in rows if r["position"] == pos and r.get("adp") is not None),
                key=lambda r: float(r["adp"]),
            )
            for i, r in enumerate(have):
                while len(buckets[pos]) <= i:
                    buckets[pos].append([])
                buckets[pos][i].append(r["points"])
    return {
        pos: [sum(b) / len(b) for b in cells if b]
        for pos, cells in buckets.items()
    }


def draft_test(rows: list[dict], curve: dict[str, list[float]],
               levels: dict[str, int], n_teams: int, rounds: int,
               hero: int = 0) -> dict:
    """Snake draft where everyone follows ADP except one seat.

    That seat drafts by VOR instead - the expected points for a player's
    positional rank, minus the expected points at that position's replacement
    level. Both strategies are then scored on what the drafted players really
    did. Same board, same opponents, one difference.

    Starters only: the score counts each team's best QB/RB/RB/WR/WR/TE plus the
    best remaining flex, because bench points are not points you can use.

    ## Why need_factor is here and not optional

    Raw VOR alone drafts fourteen quarterbacks. QB1 through QB14 all sit far
    above the QB12 baseline in absolute points, so an unconstrained argmax never
    leaves the position - and a roster of fourteen QBs starts one of them.

    That is not a flaw in VOR, it is the reason `suggest_pick` multiplies by
    `need_factor` at all. Omitting it here would have measured the harness
    rather than the model, so this mirrors the live rule: 1.0 for a player
    filling an open starter slot, 0.55 for depth, plus a hard positional cap so
    a run of 0.55-weighted picks cannot still corner one position.
    """
    starters = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
    flex_slots = 1
    caps = {"QB": 3, "RB": 8, "WR": 8, "TE": 3}
    board = sorted(
        (r for r in rows if r.get("adp") is not None), key=lambda r: float(r["adp"])
    )
    pos_rank = {p: 0 for p in SKILL}
    for r in board:
        r["_pos_rank"] = pos_rank[r["position"]]
        pos_rank[r["position"]] += 1

    def expected(r):
        c = curve.get(r["position"]) or []
        i = r["_pos_rank"]
        return c[i] if i < len(c) else (c[-1] if c else 0.0)

    def vor(r):
        c = curve.get(r["position"]) or []
        lvl = levels.get(r["position"], 1)
        base = c[lvl - 1] if lvl - 1 < len(c) else (c[-1] if c else 0.0)
        return expected(r) - base

    def need_factor(roster, pos):
        """1.0 while a starter slot is open, 0.55 once it is pure depth."""
        counts = {p: sum(1 for r in roster if r["position"] == p) for p in SKILL}
        if counts[pos] < starters[pos]:
            return 1.0
        if pos in ("RB", "WR", "TE"):
            surplus = sum(
                max(counts[p] - starters[p], 0) for p in ("RB", "WR", "TE")
            )
            if surplus < flex_slots:
                return 1.0
        return 0.55

    results = {}
    for strategy in ("adp", "vor"):
        pool = list(board)
        teams = {i: [] for i in range(n_teams)}
        for rnd in range(rounds):
            seats = range(n_teams) if rnd % 2 == 0 else reversed(range(n_teams))
            for seat in seats:
                if not pool:
                    break
                if seat == hero and strategy == "vor":
                    roster = teams[seat]
                    counts = {
                        p: sum(1 for r in roster if r["position"] == p)
                        for p in SKILL
                    }
                    eligible = [
                        r for r in pool if counts[r["position"]] < caps[r["position"]]
                    ] or pool
                    pick = max(
                        eligible,
                        key=lambda r: vor(r) * need_factor(roster, r["position"]),
                    )
                else:
                    pick = pool[0]
                pool.remove(pick)
                teams[seat].append(pick)
        results[strategy] = round(starters_points(teams[hero]), 1)
    results["gain"] = round(results["vor"] - results["adp"], 1)
    return results


def starters_points(roster: list[dict]) -> float:
    """Best QB + 2 RB + 2 WR + TE + best remaining RB/WR/TE."""
    by = {p: sorted((r["points"] for r in roster if r["position"] == p), reverse=True)
          for p in SKILL}
    need = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
    total = 0.0
    left = []
    for pos, n in need.items():
        got = by.get(pos, [])
        total += sum(got[:n])
        left.extend(got[n:] if pos != "QB" else [])
    return total + (max(left) if left else 0.0)


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs="*", type=int)
    args = ap.parse_args()

    with db.session() as conn:
        league = db.get_league(conn)
        levels = replacement_levels(league)
        n_teams = int(league.get("n_teams", 12))
        n_flex = int(league.get("roster_slots", {}).get("FLEX", 1)) * n_teams

        available = [r[0] for r in conn.execute(
            "SELECT DISTINCT season FROM adp_history ORDER BY season"
        )]
        seasons = args.seasons or available

        data = {s: preseason(conn, s) for s in seasons}

    print("=" * 74)
    print("VALUE MODEL BACKTEST".center(74))
    print("=" * 74)
    print(f"replacement levels in use: {levels}")
    print(f"scoring: PPR (nflverse) | skill positions only | seasons {seasons}")

    # 1 -------------------------------------------------------------------- #
    print("\n" + "-" * 74)
    print("1. WHICH PRESEASON SIGNAL PREDICTS POINTS BEST")
    print("-" * 74)
    print("   Spearman vs realized points. Higher = better ordering.\n")
    print(f"   {'season':<8}{'signal':<22}{'n':>5}{'all':>8}"
          f"{'QB':>7}{'RB':>7}{'WR':>7}{'TE':>7}")
    totals = {s: [] for s in SIGNALS}
    for season in seasons:
        rep = signal_report(data[season])
        for sig in SIGNALS:
            e = rep[sig]
            if not e.get("n"):
                continue
            if e.get("all") is not None:
                totals[sig].append(e["all"])
            cells = "".join(
                f"{e[p]:>7.2f}" if e.get(p) is not None else f"{'-':>7}"
                for p in SKILL
            )
            allc = f"{e['all']:>8.2f}" if e.get("all") is not None else f"{'-':>8}"
            print(f"   {season:<8}{SIGNAL_LABEL[sig]:<22}{e['n']:>5}{allc}{cells}")
        print()
    print("   mean across seasons:")
    for sig in SIGNALS:
        if totals[sig]:
            m = sum(totals[sig]) / len(totals[sig])
            print(f"      {SIGNAL_LABEL[sig]:<24}{m:>6.3f}")

    # The block above scores each signal over whatever players it covers, and
    # those universes differ a lot - ESPN ADP carries ~410 players where ECR
    # carries ~886. Spearman is not comparable across different universes, and
    # a narrower list of mostly-relevant players is the harder ranking problem.
    # So repeat the comparison on players carrying all three.
    print("\n   restricted to players carrying ALL THREE signals:")
    short = {"rank": "ECR", "adp": "ADP", "espn_adp": "ESPN"}
    print(f"      {'season':<8}{'n':>6}" +
          "".join(f"{short[s]:>9}" for s in SIGNALS))
    common = {s: [] for s in SIGNALS}
    for season in seasons:
        both = [
            r for r in data[season]
            if all(r.get(s) is not None for s in SIGNALS)
        ]
        if len(both) < 20:
            continue
        line = f"      {season:<8}{len(both):>6}"
        for sig in SIGNALS:
            c = spearman(
                [-float(r[sig]) for r in both], [r["points"] for r in both]
            )
            common[sig].append(c)
            line += f"{c:>9.3f}"
        print(line)
    if any(common.values()):
        print(f"      {'mean':<8}{'':>6}" + "".join(
            f"{sum(v)/len(v):>9.3f}" if v else f"{'-':>9}"
            for v in (common[s] for s in SIGNALS)
        ))

    # 2 -------------------------------------------------------------------- #
    print("\n" + "-" * 74)
    print("2. ARE THE REPLACEMENT LEVELS IN THE RIGHT PLACE")
    print("-" * 74)
    print("   A correct baseline sits where the curve has gone flat.")
    print("   slope = points/spot given up over the 6 spots after the level.")
    print("   slope%% = that slope as a share of the points at the level.")
    print("   COMPARE POSITIONS ON slope%%, NEVER ON slope: the positions are on")
    print("   different points scales, and a raw comparison reads QB as steep")
    print("   when it is only bigger.\n")
    print(f"   {'season':<8}{'pos':<5}{'level':>7}{'pts@level':>11}"
          f"{'+6 later':>10}{'slope':>8}{'slope%':>8}")
    slopes = {p: [] for p in SKILL}
    pcts = {p: [] for p in SKILL}
    for season in seasons:
        rep = replacement_report(data[season], levels)
        for pos, e in rep.items():
            slopes[pos].append(e["slope_per_spot"])
            pcts[pos].append(e["slope_pct"])
            print(f"   {season:<8}{pos:<5}{e['level']:>7}"
                  f"{e['points_at_level']:>11}{e['points_6_later']:>10}"
                  f"{e['slope_per_spot']:>8}{e['slope_pct']:>7.1f}%")
        print()
    print("   mean slope at the configured baseline:")
    for pos in SKILL:
        if slopes[pos]:
            m = sum(slopes[pos]) / len(slopes[pos])
            p = sum(pcts[pos]) / len(pcts[pos])
            # Flags on the normalised figure only. Even then it is a lead, not a
            # verdict - the one time this flagged, section 4 said the "fix" was
            # worse. Re-run section 4 before changing a level.
            flag = "  <-- steep for its scale; TEST in section 4" if p > 4.0 else ""
            print(f"      {pos:<5}{m:>7.1f}{p:>7.1f}%{flag}")

    # 3 -------------------------------------------------------------------- #
    print("\n" + "-" * 74)
    print("3. DOES THE FLEX SPLIT MATCH REALITY")
    print("-" * 74)
    print("   configured: RB 0.45 / WR 0.45 / TE 0.10\n")
    acc = {"RB": [], "WR": [], "TE": []}
    for season in seasons:
        rep = flex_report(data[season], levels, n_flex)
        for k, v in rep.items():
            acc[k].append(v)
        print(f"   {season}   RB {rep['RB']:.2f} / WR {rep['WR']:.2f} "
              f"/ TE {rep['TE']:.2f}")
    print("\n   observed mean: " + " / ".join(
        f"{k} {sum(v)/len(v):.2f}" for k, v in acc.items() if v
    ))

    # 4 -------------------------------------------------------------------- #
    print("\n" + "-" * 74)
    print("4. DOES DRAFTING ON VOR BEAT DRAFTING ON ADP")
    print("-" * 74)
    print("   Same board, same ADP-following opponents, one seat differs.")
    print("   Score = starting lineup points actually delivered.")
    print("   Walk-forward: the rank->points curve is fitted on prior seasons.")
    print("   Every draft slot is run as the hero seat and averaged - a single")
    print("   seat swings hundreds of points on one lucky pick.\n")
    print(f"   {'season':<8}{'by ADP':>10}{'by VOR':>10}{'mean gain':>12}"
          f"{'seats won':>11}")
    gains = []
    rounds = 14
    for i, season in enumerate(seasons):
        train = [data[s] for s in seasons[:i]]
        if not train:
            print(f"   {season:<8}{'-':>10}{'-':>10}"
                  f"{'(training only)':>23}")
            continue
        curve = expected_by_rank(train)
        seat_results = [
            draft_test(data[season], curve, levels, n_teams, rounds, hero=s)
            for s in range(n_teams)
        ]
        adp_mean = sum(r["adp"] for r in seat_results) / n_teams
        vor_mean = sum(r["vor"] for r in seat_results) / n_teams
        won = sum(1 for r in seat_results if r["gain"] > 0)
        gains.extend(r["gain"] for r in seat_results)
        print(f"   {season:<8}{adp_mean:>10.1f}{vor_mean:>10.1f}"
              f"{vor_mean - adp_mean:>+12.1f}{won:>7}/{n_teams}")
    if gains:
        mean = sum(gains) / len(gains)
        sd = (sum((g - mean) ** 2 for g in gains) / max(len(gains) - 1, 1)) ** 0.5
        stderr = sd / (len(gains) ** 0.5)
        print(f"\n   mean gain from VOR: {mean:>+.1f} +/- {stderr:.1f} "
              f"starting-lineup points/season (n={len(gains)} seat-seasons)")
        verdict = ("VOR is not beating ADP here" if mean < 2 * stderr
                   else "VOR beats ADP")
        print(f"   verdict: {verdict}")
    print()


if __name__ == "__main__":
    main()
