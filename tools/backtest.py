"""Backtest the pick model against real drafts, and tune its knobs.

## The method

For each testable season, replay that draft pick by pick. At every pick the model
produces a probability distribution over positions, and over players within a
position. Score it against what the manager actually did.

Scoring is **teacher-forced**: the board state at pick N is rebuilt from the
*actual* picks 1..N-1, not from what the model would have done. That isolates
per-pick accuracy. A free-running simulation compounds its own errors and tells you
about drift rather than about the model.

## The leakage trap

Tendencies are fitted on league history. Fitting them on the season you are
predicting leaks the answer, and any knob tuned against a leaked score is tuned to
memorise rather than forecast. So this walks forward: to score season N, tendencies,
reach profiles and round floors are all fitted on seasons < N only.

That costs sample size. The earliest season has no prior history and cannot be
scored at all.

## Metrics

  position log-loss   -mean log P(actual position). The headline. Lower is better.
  position top-1      how often the model's most likely position was the pick.
  player log-loss     -mean log P(actual player), = P(position) * P(player|position).
  player top-1/top-3  how often the actual player was the model's first/top-3 choice.

A baseline that always predicts the league's base rates is reported alongside, so a
number can be judged as better-than-nothing rather than in the abstract.

## Known approximation

The available player pool is reconstructed from players drafted in that season, not
the full contemporaneous player universe - that would need a preseason ranking file
for every season. Late-round scarcity is therefore slightly overstated. The same
approximation applies to every knob setting, so relative comparisons hold.

Usage:
    python tools/backtest.py                       # baseline, current knobs
    python tools/backtest.py --seasons 2024 2025
    python tools/backtest.py --tune                # coarse search over knobs
    python tools/backtest.py --tune --trials 60
"""

import argparse
import itertools
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, tendencies as T                            # noqa: E402

EPSILON = 1e-9


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #

def season_picks(conn, season: int) -> list[dict]:
    """Real picks for a season, keyed to the contemporaneous consensus ADP.

    `market_adp` rather than `market_rank`: consensus ADP covers every season
    98-100%, while `espn_draft_rank` has no 2022 at all - which used to make 2023
    unscoreable for want of a fit season.
    """
    return [
        dict(r) for r in conn.execute(
            """
            SELECT overall_pick, round, round_pick, owner_guid, owner_label,
                   position, player_name, name_key, market_adp AS rank
            FROM history
            WHERE season = ? AND COALESCE(keeper, 0) = 0
              AND position IS NOT NULL AND market_adp IS NOT NULL
            ORDER BY overall_pick
            """,
            (season,),
        )
    ]


def season_pool(conn, season: int) -> dict[str, list[float]]:
    """Everyone who was on the board that season, by position, as ADP values.

    The whole point of loading historical ADP. Reconstructing the pool from players
    who were *drafted* understates the bargains genuinely available, which is what
    made the board-availability term look useless when it may simply have been
    measured against a pool with its best undrafted players missing.
    """
    pool: dict[str, list[float]] = {}
    for row in conn.execute(
        "SELECT position, adp FROM adp_history "
        "WHERE season = ? AND adp IS NOT NULL ORDER BY adp",
        (season,),
    ):
        pool.setdefault(row["position"], []).append(float(row["adp"]))
    return pool


def league_shape(conn, season: int) -> dict:
    """Team count and round count as they actually were that season."""
    row = conn.execute(
        "SELECT MAX(round_pick) AS teams, MAX(round) AS rounds FROM history "
        "WHERE season = ?", (season,),
    ).fetchone()
    base = db.get_league(conn)
    return {
        **base,
        "n_teams": int(row["teams"] or base["n_teams"]),
        "rounds": int(row["rounds"] or base["rounds"]),
    }


def _need_multipliers_from_counts(counts, league, round_number, depth_curve=None):
    """The live need model, driven by a replayed roster instead of the picks table."""
    targets = T.starter_targets(league)
    slots = league.get("roster_slots", {})
    multipliers = {}
    for pos in T.POSITIONS:
        held = counts.get(pos, 0)
        if depth_curve and pos in depth_curve:
            multipliers[pos] = depth_curve[pos].get(held, T.OVER_CAP_MULTIPLIER)
        elif held >= T.POSITION_ROSTER_CAP.get(pos, 6):
            multipliers[pos] = T.OVER_CAP_MULTIPLIER
        elif held < targets.get(pos, 0):
            multipliers[pos] = 1.0
        else:
            multipliers[pos] = T.FILLED_SLOT_PENALTY

    picks_left = int(league.get("rounds", 18)) - int(round_number) + 1
    unfilled = [
        pos for pos in T.POSITIONS if counts.get(pos, 0) < int(slots.get(pos, 0))
    ]
    if unfilled:
        if picks_left <= len(unfilled):
            for pos in T.POSITIONS:
                multipliers[pos] = (
                    T.MUST_FILL_MULTIPLIER if pos in unfilled else 0.0
                )
        elif picks_left <= len(unfilled) + 2:
            for pos in unfilled:
                multipliers[pos] = T.MUST_FILL_MULTIPLIER
    return multipliers


def _availability_from_pool(pool, overall):
    """Same shape as tendencies._availability_factors, over a replayed pool."""
    low, high = T.VALUE_FACTOR_RANGE
    factors = {}
    for pos in T.POSITIONS:
        ranks = pool.get(pos)
        if not ranks:
            factors[pos] = 0.0
            continue
        best = min(ranks)
        slack = (overall - best) / T.BEST_AVAILABLE_SCALE
        factors[pos] = (
            min(1.0 + slack, high) if slack >= 0 else max(math.exp(slack), low)
        )
    return factors


def score_season(conn, season: int, verbose: bool = False) -> dict:
    """Walk one season's real draft and score the model's per-pick predictions."""
    picks = season_picks(conn, season)
    if not picks:
        return {"season": season, "n": 0, "note": "no scorable picks"}

    league = league_shape(conn, season)
    # Fitted on prior seasons ONLY.
    shares = T.position_share(conn, before_season=season)
    base = shares["_league"]
    earliest = shares.get("_earliest", {})
    reach = T.reach_profile(conn, before_season=season)
    if not any(k for k in shares if k not in ("_league", "_earliest")):
        return {"season": season, "n": 0, "note": "no prior history to fit on"}

    # The real board that season, from historical ADP.
    pool = season_pool(conn, season)
    if not pool:
        return {"season": season, "n": 0, "note": "no ADP pool for that season"}

    rosters: dict[str, dict[str, int]] = {}
    totals = {
        "pos_loss": 0.0, "pos_top1": 0, "player_loss": 0.0,
        "player_top1": 0, "player_top3": 0, "n": 0,
        "base_pos_loss": 0.0,
    }

    for pick in picks:
        guid = pick["owner_guid"]
        counts = rosters.setdefault(guid, {})
        phase = T.phase_for(int(pick["round"]))

        multipliers = _need_multipliers_from_counts(
            counts, league, pick["round"], shares.get("_depth")
        )
        T._apply_round_floor(multipliers, earliest, pick["round"])
        availability = _availability_from_pool(pool, pick["overall_pick"])

        dist = T._position_distribution(
            shares, guid, phase, base, multipliers, availability
        )
        dist.pop("_n", None)

        actual_pos = pick["position"]
        p_pos = dist.get(actual_pos, 0.0)
        totals["pos_loss"] += -math.log(max(p_pos, EPSILON))
        if actual_pos == max(dist, key=dist.get):
            totals["pos_top1"] += 1

        # Naive baseline: the league's own phase base rates, no manager, no board.
        p_base = base[phase]["rates"].get(actual_pos, 0.0)
        totals["base_pos_loss"] += -math.log(max(p_base, EPSILON))

        # Within-position: rank-decay over what was still available, using this
        # manager's reach-adjusted sharpness, exactly as the mock bots do.
        profile = reach.get(guid)
        decay = _decay_for(profile)
        depth = max(2, min(14, 2 + int(pick["round"])))
        ranked_pool = sorted(pool.get(actual_pos, []))[:depth]
        weights = [
            math.exp(-i / decay)
            * T.player_reach_weight(pick["overall_pick"], rank, pick["round"])
            for i, rank in enumerate(ranked_pool)
        ]
        total_w = sum(weights) or 1.0
        actual_rank = pick["rank"]
        try:
            idx = ranked_pool.index(actual_rank)
            p_player_given_pos = weights[idx] / total_w
        except ValueError:
            p_player_given_pos = EPSILON

        p_player = p_pos * p_player_given_pos
        totals["player_loss"] += -math.log(max(p_player, EPSILON))

        # Rank the model's player-level preferences to get top-1 / top-3.
        scored = []
        for pos, positional in dist.items():
            candidates = sorted(pool.get(pos, []))[:depth]
            w = [
                math.exp(-i / decay)
                * T.player_reach_weight(pick["overall_pick"], rank, pick["round"])
                for i, rank in enumerate(candidates)
            ]
            tw = sum(w) or 1.0
            for rank, weight in zip(candidates, w):
                scored.append((positional * weight / tw, pos, rank))
        scored.sort(reverse=True)
        top3 = [(p, r) for _, p, r in scored[:3]]
        if top3 and top3[0] == (actual_pos, actual_rank):
            totals["player_top1"] += 1
        if (actual_pos, actual_rank) in top3:
            totals["player_top3"] += 1

        totals["n"] += 1
        # Advance the replayed state with what ACTUALLY happened.
        counts[actual_pos] = counts.get(actual_pos, 0) + 1
        if actual_rank in pool.get(actual_pos, []):
            pool[actual_pos].remove(actual_rank)

    n = totals["n"]
    return {
        "season": season,
        "n": n,
        "pos_loss": totals["pos_loss"] / n,
        "pos_top1": totals["pos_top1"] / n,
        "player_loss": totals["player_loss"] / n,
        "player_top1": totals["player_top1"] / n,
        "player_top3": totals["player_top3"] / n,
        "base_pos_loss": totals["base_pos_loss"] / n,
    }


def _decay_for(profile):
    if not profile or not profile.get("n"):
        return T.RANK_DECAY
    low, high = (0.7, 1.6)
    scale = 1.0 + (profile["median_reach"] / 24.0)
    return T.RANK_DECAY * min(max(scale, low), high)


def run(conn, seasons, verbose=False) -> dict:
    results = [score_season(conn, s, verbose) for s in seasons]
    scored = [r for r in results if r.get("n")]
    if not scored:
        return {"seasons": results, "overall": None}
    total_n = sum(r["n"] for r in scored)
    overall = {
        key: sum(r[key] * r["n"] for r in scored) / total_n
        for key in ("pos_loss", "pos_top1", "player_loss", "player_top1",
                    "player_top3", "base_pos_loss")
    }
    overall["n"] = total_n
    return {"seasons": results, "overall": overall}


# --------------------------------------------------------------------------- #
# tuning
# --------------------------------------------------------------------------- #

KNOBS = {
    "SHRINK_K": [3.0, 6.0, 10.0, 16.0],
    "BEST_AVAILABLE_SCALE": [12.0, 18.0, 24.0, 36.0],
    "FILLED_SLOT_PENALTY": [0.15, 0.35, 0.55],
    "RANK_DECAY": [1.2, 1.8, 2.5],
    "VALUE_FLOOR": [0.02, 0.05, 0.15],
}


def apply_knobs(combo):
    T.SHRINK_K = combo["SHRINK_K"]
    T.BEST_AVAILABLE_SCALE = combo["BEST_AVAILABLE_SCALE"]
    T.FILLED_SLOT_PENALTY = combo["FILLED_SLOT_PENALTY"]
    T.RANK_DECAY = combo["RANK_DECAY"]
    T.VALUE_FACTOR_RANGE = (combo["VALUE_FLOOR"], T.VALUE_FACTOR_RANGE[1])


def tune(conn, seasons, trials, seed=0):
    keys = list(KNOBS)
    grid = list(itertools.product(*(KNOBS[k] for k in keys)))
    rng = random.Random(seed)
    if trials and trials < len(grid):
        grid = rng.sample(grid, trials)
    print(f"searching {len(grid)} of {len(list(itertools.product(*(KNOBS[k] for k in keys))))} combinations\n")

    baseline = None
    rows = []
    for values in grid:
        combo = dict(zip(keys, values))
        apply_knobs(combo)
        result = run(conn, seasons)
        if not result["overall"]:
            continue
        rows.append((result["overall"]["player_loss"], result["overall"], combo))
        if baseline is None:
            baseline = result["overall"]

    rows.sort(key=lambda r: r[0])
    print(f"{'PLAYER LOSS':>12}{'POS LOSS':>10}{'POS TOP1':>10}{'PL TOP3':>9}   KNOBS")
    for loss, overall, combo in rows[:12]:
        knobs = " ".join(f"{k}={v}" for k, v in combo.items())
        print(f"{loss:>12.4f}{overall['pos_loss']:>10.4f}"
              f"{overall['pos_top1']:>9.1%}{overall['player_top3']:>9.1%}   {knobs}")
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seasons", nargs="*", type=int, default=None)
    parser.add_argument("--tune", action="store_true")
    parser.add_argument("--trials", type=int, default=40)
    args = parser.parse_args()

    db.init_db()
    with db.session() as conn:
        available = [
            r["season"] for r in conn.execute(
                "SELECT DISTINCT h.season FROM history h "
                "WHERE h.market_adp IS NOT NULL AND EXISTS "
                "(SELECT 1 FROM adp_history a WHERE a.season = h.season) "
                "ORDER BY h.season"
            )
        ]
        earliest_history = conn.execute(
            "SELECT MIN(season) AS s FROM history"
        ).fetchone()["s"]
        seasons = args.seasons or [s for s in available if s > earliest_history]

        print(f"seasons with a market baseline: {available}")
        print(f"earliest history: {earliest_history} (can't be scored - nothing to fit on)")
        print(f"scoring: {seasons}\n")

        if args.tune:
            tune(conn, seasons, args.trials)
            return

        result = run(conn, seasons, verbose=True)
        print(f"{'SEASON':<8}{'N':>5}{'POS LOSS':>10}{'BASE':>8}{'POS TOP1':>10}"
              f"{'PLAYER LOSS':>13}{'PL TOP1':>9}{'PL TOP3':>9}")
        for r in result["seasons"]:
            if not r.get("n"):
                print(f"{r['season']:<8}{'-':>5}  {r.get('note')}")
                continue
            print(f"{r['season']:<8}{r['n']:>5}{r['pos_loss']:>10.4f}"
                  f"{r['base_pos_loss']:>8.4f}{r['pos_top1']:>10.1%}"
                  f"{r['player_loss']:>13.4f}{r['player_top1']:>9.1%}"
                  f"{r['player_top3']:>9.1%}")
        o = result["overall"]
        if o:
            print(f"\n{'ALL':<8}{o['n']:>5}{o['pos_loss']:>10.4f}"
                  f"{o['base_pos_loss']:>8.4f}{o['pos_top1']:>10.1%}"
                  f"{o['player_loss']:>13.4f}{o['player_top1']:>9.1%}"
                  f"{o['player_top3']:>9.1%}")
            lift = o["base_pos_loss"] - o["pos_loss"]
            print(f"\nposition log-loss vs league-base-rate baseline: {lift:+.4f} "
                  f"({'better' if lift > 0 else 'WORSE'})")


if __name__ == "__main__":
    main()
