"""League-history modeling: what each manager tends to do, and what they will
probably do next.

## Why this is shaped the way it is

A 12-team league with four seasons of history gives each manager roughly 64
picks total — about four per round. Estimating "what does Dave do in round 6"
from four observations produces noise that looks like insight. So:

* Rounds are bucketed into phases (early / mid / late), not treated
  individually.
* Every per-manager rate is shrunk toward the league base rate in proportion to
  how little data supports it (empirical Bayes). A manager with two recorded
  picks in a phase gets an estimate close to the league average; one with forty
  gets close to their own rate.
* Sample sizes travel with every number, so a confident-looking 80% backed by
  five picks is visibly distinguishable from one backed by fifty.

Keepers are excluded throughout. A keeper is not a draft decision, and counting
one as a "round 1 RB tendency" would be wrong.

## What is deliberately not here

There is no reach-vs-ADP metric. That needs the ADP that was current *at the
time of each past draft*, and no free source publishes historical fantasy ADP.
Reconstructing a baseline from four seasons of one league's own picks yields
roughly four observations per positional slot — too thin to report as a number.
Positional share and first-at-position timing measure the same instinct with
data that actually supports it.

Outcome value uses `pos_taken` vs `pos_finish`: if a manager took the 5th RB off
the board and he finished as RB2, that is +3. This measures results, which is
part skill and part luck, and is reported separately from tendency.
"""

import argparse
import math
import random
import sys

from . import db, draft
from .config import FLEX_POSITIONS
from .ingest import norm_name

POSITIONS = ("QB", "RB", "WR", "TE", "K", "DST")

# Round buckets. Draft behavior changes character at these boundaries: early is
# best-player-available, mid is roster construction, late is streamers and
# lottery tickets.
PHASES = (("early", 1, 3), ("mid", 4, 8), ("late", 9, 99))

# Pseudo-counts for shrinkage toward the league base rate.
#
# Backtested on three seasons, 608 out-of-sample picks. Position log-loss / top-1:
#
#     K=2     1.4633   41.9%
#     K=6     1.4280   42.9%   <- the original guess, over-weighting managers
#     K=25    1.4085   44.6%   <- adopted
#     K=50    1.4071   44.1%
#     K=1000  1.4097   43.4%   <- per-manager tendencies effectively switched off
#
# The honest reading: who the manager is buys about one point of top-1 accuracy
# over ignoring them entirely, and only when shrunk hard. At K=6 the tendencies
# were actively hurting. League base rates, roster needs and board state do most
# of the work.
SHRINK_K = 25.0

# How sharply managers are assumed to prefer the top-ranked available player at
# a position. Lower = more deterministic. 1.8 puts ~45% of positional mass on
# the top board player, ~25% on the second.
#
# Backtested, not guessed. Sweeping this against three seasons of real drafts:
# 1.8 -> player log-loss 6.311, 2.5 -> 6.179, 3.5 -> 6.121, 5.0 -> 6.106.
# The minimum is flat from 3.5 to 6.5, but top-3 accuracy peaks at 3.5 (26.2%) and
# falls away by 5.0 (24.7%) - past 3.5 the gain is pure hedging, which helps
# log-loss and hurts a simulator.
#
# Re-swept once historical ADP gave a real available pool: loss bottoms at 3.5
# (4.4454) but top-3 accuracy peaks sharper, at 1.8 (33.7%) falling to 32.6% by 3.5.
# 2.5 sits between them - 4.4530 loss, 33.4% top-3 - and a sharper curve is also the
# safer choice for a simulator, which wants plausible picks rather than hedged ones.
RANK_DECAY = 2.5

# Multiplier applied when a manager has already filled their starter slots at a
# position. They still draft backups, just far less often.
FILLED_SLOT_PENALTY = 0.35

# Fallback roster ceilings, used only when there is no league history to measure
# from. With history, `roster_depth_curve` supersedes these entirely.
#
# Hand-picked caps were a mistake and backtesting proved it. Capping TE at 2 and
# DST at 1 assigned probability ZERO to things this league actually did - 3 TEs in
# four manager-seasons, 3 DSTs in one - and a confident zero on an event that
# happened is what drove position log-loss (1.80) above a naive base-rate baseline
# (1.54). Hard zeros belong only on the genuinely impossible.
POSITION_ROSTER_CAP = {"QB": 3, "RB": 8, "WR": 9, "TE": 3, "K": 2, "DST": 3}
# Beyond the ceiling: rare, not impossible.
OVER_CAP_MULTIPLIER = 0.01
# Taking a position earlier than anyone in this league ever has. A soft penalty,
# because "never observed in four seasons" is not the same as "cannot happen" - and
# a hard zero here also cost log-loss when 2025 took a kicker a round earlier than
# any prior season had.
ROUND_FLOOR_MULTIPLIER = 0.05
# Weight given to a mandatory starter slot still unfilled with the draft running
# out of rounds. Overrides the ordinary tiers so a roster is never left illegal.
MUST_FILL_MULTIPLIER = 6.0

# How a FLEX slot divides across the positions eligible to fill it. Same split as
# `replacement_levels` uses, so needs and VOR agree about what a starter is.
FLEX_SHARE = {"RB": 0.45, "WR": 0.45, "TE": 0.10}

# How much board value shifts a position's odds. If the best available player at
# a position is ranked N picks earlier than the pick now being made, that is
# `N / BEST_AVAILABLE_SCALE` units of bargain, and the position gets boosted -
# elite players sliding do not slide much further. Clamped so the value term can
# reshape a distribution but never dominate the manager's actual habits.
# Kept at 24, now with evidence rather than in spite of it.
#
# An earlier sweep against a pool reconstructed from only *drafted* players showed
# log-loss improving monotonically out to 72, where the term is effectively off -
# which read as "the bargain term does not work". Re-run against the real historical
# pool, the picture reverses on the metrics that measure discrimination:
#
#     scale    pos loss   pos top-1   player top-3
#        6       1.6232      42.9%        33.4%
#       24       1.4280      42.9%        34.5%   <- best accuracy
#       72       1.3908      41.3%        32.4%
#      200       1.3848      42.1%        32.6%   <- best loss, worst accuracy
#
# Log-loss keeps improving because weakening the term makes predictions less
# confident, and hedging is never punished. Accuracy peaks at 24. The term earns its
# place; the earlier negative result was an artefact of a pool missing its best
# undrafted players.
BEST_AVAILABLE_SCALE = 24.0
# Asymmetric on purpose. A bargain sitting on the board raises a position's odds
# linearly, but a position whose best remaining player is ranked far LATER than the
# current pick decays exponentially - taking him would be a reach, and reaches get
# rapidly less likely the bigger they are. The old symmetric clamp bottomed out at
# 0.6, which left a kicker whose best man was ranked 160 picks away holding 60% of
# his weight; that is how bots ended up making picks no human would.
VALUE_FACTOR_RANGE = (0.05, 1.8)


def phase_for(round_number: int) -> str:
    for name, low, high in PHASES:
        if low <= round_number <= high:
            return name
    return PHASES[-1][0]


# --------------------------------------------------------------------------- #
# history access
# --------------------------------------------------------------------------- #

def history_picks(
    conn,
    include_keepers: bool = False,
    before_season: int | None = None,
) -> list[dict]:
    """Historical picks, optionally only those before a given season.

    `before_season` exists for backtesting. Fitting tendencies on the very season
    you are trying to predict leaks the answer, and tuning against a leaked score
    optimises for memorising the past rather than forecasting the next draft.
    """
    sql = "SELECT * FROM history WHERE position IS NOT NULL"
    params: list = []
    if not include_keepers:
        sql += " AND COALESCE(keeper, 0) = 0"
    if before_season is not None:
        sql += " AND season < ?"
        params.append(int(before_season))
    return [
        dict(r) for r in conn.execute(sql + " ORDER BY season, overall_pick", params)
    ]


def history_summary(conn) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS picks, COUNT(DISTINCT season) AS seasons, "
        "COUNT(DISTINCT owner_guid) AS owners, "
        "SUM(COALESCE(keeper, 0)) AS keepers, "
        "SUM(CASE WHEN season_pts IS NOT NULL THEN 1 ELSE 0 END) AS with_outcomes, "
        "MIN(season) AS first_season, MAX(season) AS last_season FROM history"
    ).fetchone()
    summary = dict(row)
    summary["linked"] = conn.execute(
        "SELECT COUNT(*) AS n FROM managers WHERE owner_guid IS NOT NULL"
    ).fetchone()["n"]
    summary["seasons_list"] = [
        r["season"] for r in conn.execute(
            "SELECT DISTINCT season FROM history ORDER BY season"
        )
    ]
    return summary


def owners(conn) -> list[dict]:
    """Distinct historical owners, labelled by their most recent team name."""
    rows = conn.execute(
        """
        SELECT h.owner_guid, h.owner_label, COUNT(*) AS picks,
               COUNT(DISTINCT h.season) AS seasons, MAX(h.season) AS last_season,
               (SELECT id FROM managers m WHERE m.owner_guid = h.owner_guid) AS manager_id
        FROM history h
        WHERE h.owner_guid IS NOT NULL AND COALESCE(h.keeper, 0) = 0
        GROUP BY h.owner_guid
        ORDER BY picks DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# tendency: positional share by phase
# --------------------------------------------------------------------------- #

def league_base_rates(picks: list[dict]) -> dict[str, dict[str, float]]:
    """P(position | phase) across the whole league. The shrinkage target."""
    counts = {name: {p: 0 for p in POSITIONS} for name, _, _ in PHASES}
    totals = {name: 0 for name, _, _ in PHASES}
    for pick in picks:
        if pick["round"] is None:
            continue
        phase = phase_for(int(pick["round"]))
        if pick["position"] in counts[phase]:
            counts[phase][pick["position"]] += 1
            totals[phase] += 1
    return {
        phase: {
            pos: (counts[phase][pos] / totals[phase] if totals[phase] else 0.0)
            for pos in POSITIONS
        }
        for phase in counts
    }


def position_share(conn, before_season: int | None = None) -> dict:
    """Per-owner P(position | phase), shrunk toward the league rate.

    Returns {owner_guid: {phase: {"n": int, "rates": {pos: p}, "raw": {pos: p}}}}
    """
    picks = history_picks(conn, before_season=before_season)
    base = league_base_rates(picks)

    per_owner: dict[str, dict[str, dict[str, int]]] = {}
    for pick in picks:
        guid, rnd = pick["owner_guid"], pick["round"]
        if not guid or rnd is None:
            continue
        phase = phase_for(int(rnd))
        bucket = per_owner.setdefault(guid, {}).setdefault(
            phase, {p: 0 for p in POSITIONS}
        )
        if pick["position"] in bucket:
            bucket[pick["position"]] += 1

    out: dict[str, dict] = {}
    for guid, phases in per_owner.items():
        out[guid] = {}
        for phase, counts in phases.items():
            n = sum(counts.values())
            rates, raw = {}, {}
            for pos in POSITIONS:
                raw[pos] = counts[pos] / n if n else 0.0
                rates[pos] = (
                    (counts[pos] + SHRINK_K * base[phase][pos]) / (n + SHRINK_K)
                )
            out[guid][phase] = {"n": n, "rates": rates, "raw": raw}
    out["_league"] = {
        phase: {"n": None, "rates": base[phase], "raw": base[phase]}
        for phase in base
    }
    # Earliest round anyone in this league has ever taken each position. Three
    # coarse phases treat round 9 and round 18 identically, so a 10% late-phase
    # kicker rate put kickers off the board by round 7 - while this league has
    # never taken one before round 14. This is the floor that fixes it.
    earliest: dict[str, int] = {}
    for pick in picks:
        pos, rnd = pick["position"], pick["round"]
        if pos not in POSITIONS or rnd is None:
            continue
        rnd = int(rnd)
        if pos not in earliest or rnd < earliest[pos]:
            earliest[pos] = rnd
    out["_earliest"] = earliest
    out["_depth"] = roster_depth_curve(picks)
    return out


# --------------------------------------------------------------------------- #
# tendency: when they first take each position
# --------------------------------------------------------------------------- #

def first_position_round(conn) -> dict:
    """Per owner and position, the round they first take it, by season.

    The single most legible tendency signal: "she has never left round 6 without
    a tight end" is directly actionable, and it survives small samples because
    it is one observation per season rather than a rate.
    """
    picks = history_picks(conn)
    firsts: dict[str, dict[str, dict[int, int]]] = {}
    for pick in picks:
        guid, pos, rnd, season = (
            pick["owner_guid"], pick["position"], pick["round"], pick["season"]
        )
        if not guid or rnd is None or pos not in POSITIONS:
            continue
        seen = firsts.setdefault(guid, {}).setdefault(pos, {})
        if season not in seen or int(rnd) < seen[season]:
            seen[season] = int(rnd)

    out: dict[str, dict] = {}
    for guid, positions in firsts.items():
        out[guid] = {}
        for pos, by_season in positions.items():
            rounds = sorted(by_season.values())
            out[guid][pos] = {
                "n": len(rounds),
                "median": _median(rounds),
                "earliest": rounds[0],
                "latest": rounds[-1],
                "by_season": by_season,
            }
    return out


def _median(values: list[int]) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


# --------------------------------------------------------------------------- #
# tendency: run-following
# --------------------------------------------------------------------------- #

def run_following(conn, run_length: int = 2) -> dict:
    """How often an owner takes the same position as the picks right before them.

    A "run" here is `run_length` consecutive picks at one position immediately
    preceding the owner's pick. Compared against the league rate so that a
    position simply being popular does not read as run-chasing.
    """
    picks = history_picks(conn)
    by_season: dict[int, list[dict]] = {}
    for pick in picks:
        by_season.setdefault(pick["season"], []).append(pick)

    faced: dict[str, int] = {}
    followed: dict[str, int] = {}
    league_faced = league_followed = 0

    for season_picks in by_season.values():
        ordered = sorted(season_picks, key=lambda p: p["overall_pick"])
        for i, pick in enumerate(ordered):
            if i < run_length or not pick["owner_guid"]:
                continue
            window = [ordered[j]["position"] for j in range(i - run_length, i)]
            if len(set(window)) != 1:
                continue
            guid = pick["owner_guid"]
            faced[guid] = faced.get(guid, 0) + 1
            league_faced += 1
            if pick["position"] == window[0]:
                followed[guid] = followed.get(guid, 0) + 1
                league_followed += 1

    league_rate = league_followed / league_faced if league_faced else 0.0
    return {
        "league_rate": league_rate,
        "run_length": run_length,
        "owners": {
            guid: {
                "faced": faced[guid],
                "followed": followed.get(guid, 0),
                "rate": followed.get(guid, 0) / faced[guid],
                "vs_league": followed.get(guid, 0) / faced[guid] - league_rate,
            }
            for guid in faced
        },
    }


# --------------------------------------------------------------------------- #
# outcome: did their picks work out
# --------------------------------------------------------------------------- #

# What counts as "a player you could actually start" at each position, for a
# 12-team league: one starting QB/TE/K/DST per team, and two RB/WR per team plus
# flex. Deliberately fixed rather than read from league config, so the number
# means the same thing across seasons when the league changed size.
STARTER_THRESHOLD = {"QB": 12, "RB": 24, "WR": 24, "TE": 12, "K": 12, "DST": 12}


def draft_value(conn) -> dict:
    """Per owner, mean (pos_taken - pos_finish) over picks with known outcomes.

    `pos_finish` ranks against every player at the position, including undrafted
    breakouts. Those leapfrog drafted busts, so the league-wide mean delta sits
    structurally *below* zero — in testing, around -1.1 slots. Judging an owner
    against zero would therefore mark the whole league as bad drafters. What
    matters is `vs_league`: their delta minus the league's.

    This is results, not process: a chunk of it is luck, and it says nothing
    about what they will do next. Kept separate from tendency for that reason.
    """
    rows = conn.execute(
        """
        SELECT owner_guid, season, round, position, player_name,
               pos_taken, pos_finish, season_pts
        FROM history
        WHERE COALESCE(keeper, 0) = 0
          AND pos_taken IS NOT NULL AND pos_finish IS NOT NULL
          AND owner_guid IS NOT NULL
        """
    ).fetchall()

    def blank():
        return {
            "n": 0, "delta_sum": 0.0, "hits": 0, "starters": 0, "pts": 0.0,
            "by_pos": {}, "by_season": {}, "best": None, "worst": None,
        }

    agg: dict[str, dict] = {}
    league = blank()

    for row in rows:
        delta = row["pos_taken"] - row["pos_finish"]
        points = row["season_pts"] or 0.0
        started = row["pos_finish"] <= STARTER_THRESHOLD.get(row["position"], 24)

        for bucket in (agg.setdefault(row["owner_guid"], blank()), league):
            bucket["n"] += 1
            bucket["delta_sum"] += delta
            bucket["pts"] += points
            if delta > 0:
                bucket["hits"] += 1
            if started:
                bucket["starters"] += 1

            pos = bucket["by_pos"].setdefault(
                row["position"], {"n": 0, "delta_sum": 0.0}
            )
            pos["n"] += 1
            pos["delta_sum"] += delta

            season = bucket["by_season"].setdefault(
                row["season"], {"n": 0, "pts": 0.0, "starters": 0}
            )
            season["n"] += 1
            season["pts"] += points
            season["starters"] += 1 if started else 0

        entry = {
            "player": row["player_name"], "season": row["season"],
            "round": row["round"], "position": row["position"],
            "taken": row["pos_taken"], "finish": row["pos_finish"],
            "delta": delta, "points": round(points, 1),
        }
        owner = agg[row["owner_guid"]]
        if owner["best"] is None or delta > owner["best"]["delta"]:
            owner["best"] = entry
        # Worst is restricted to players drafted AS starters. Otherwise it is
        # always some round-17 flier who never played - technically the largest
        # negative delta, but it cost nothing and says nothing about the manager.
        drafted_as_starter = (
            row["pos_taken"] <= STARTER_THRESHOLD.get(row["position"], 24)
        )
        if drafted_as_starter and (
            owner["worst"] is None or delta < owner["worst"]["delta"]
        ):
            owner["worst"] = entry

    def summarize(bucket):
        if not bucket["n"]:
            return None
        n = bucket["n"]
        return {
            "n": n,
            "mean_delta": round(bucket["delta_sum"] / n, 2),
            "hit_rate": round(bucket["hits"] / n, 3),
            "starter_rate": round(bucket["starters"] / n, 3),
            "pts_per_pick": round(bucket["pts"] / n, 1),
            "total_points": round(bucket["pts"], 1),
            "by_season": {
                season: {
                    "picks": v["n"],
                    "pts_per_pick": round(v["pts"] / v["n"], 1),
                    "total_points": round(v["pts"], 1),
                    "starters": v["starters"],
                }
                for season, v in sorted(bucket["by_season"].items())
            },
            "by_position": {
                pos: round(v["delta_sum"] / v["n"], 2)
                for pos, v in bucket["by_pos"].items()
                if v["n"] >= 3
            },
        }

    league_summary = summarize(league) or {}
    out = {"_league": league_summary}
    for guid, bucket in agg.items():
        summary = summarize(bucket)
        if not summary:
            continue
        summary["vs_league"] = round(
            summary["mean_delta"] - league_summary.get("mean_delta", 0.0), 2
        )
        summary["hit_rate_vs_league"] = round(
            summary["hit_rate"] - league_summary.get("hit_rate", 0.0), 3
        )
        summary["starter_rate_vs_league"] = round(
            summary["starter_rate"] - league_summary.get("starter_rate", 0.0), 3
        )
        summary["pts_per_pick_vs_league"] = round(
            summary["pts_per_pick"] - league_summary.get("pts_per_pick", 0.0), 1
        )
        league_by_pos = league_summary.get("by_position", {})
        summary["by_position_vs_league"] = {
            pos: round(delta - league_by_pos[pos], 1)
            for pos, delta in summary["by_position"].items()
            if pos in league_by_pos
        }
        summary["best"] = bucket["best"]
        summary["worst"] = bucket["worst"]
        out[guid] = summary
    return out


# --------------------------------------------------------------------------- #
# tendency: reaching vs waiting against the ESPN market
# --------------------------------------------------------------------------- #

# Roughly one full round in a 12-team league. Used only to classify a pick as a
# reach or a wait for the rate statistics, not in the mean.
REACH_ROUND = 12


def reach_picks(conn, before_season: int | None = None) -> list[dict]:
    """Every historical pick scored against ESPN's implied draft order.

    ## Why raw rank minus pick does not work

    `espn_draft_rank` ranks ~1,700 players, most of whom nobody drafts, so rank
    climbs much faster than pick number. In 2025 round 1 averaged rank 7 while
    round 13 averaged rank 300 against picks 145-156. Subtracting directly would
    label every late-round pick a huge reach purely from that scale drift, and
    the measured mean would be +49 to +71 depending on the season.

    ## What is done instead

    Ranks are converted to an implied draft order *within each season*: sort that
    season's picks by ESPN rank, then hand out the same set of pick numbers in
    that order. The i-th best-ranked player gets the i-th earliest pick that was
    actually used.

        reach = expected_pick - actual_pick

    Positive means taken earlier than ESPN's ordering implies. Because expected
    picks are a permutation of the actual picks, the league's mean reach is
    exactly 0 by construction - so unlike the outcome metric, 0 genuinely is the
    baseline here and no league-mean correction is needed.

    Seasons ESPN serves no ranks for (2022) drop out automatically.
    """
    sql = """
        SELECT season, overall_pick, round, position, player_name,
               owner_guid, owner_label, market_rank
        FROM history
        WHERE COALESCE(keeper, 0) = 0
          AND market_rank IS NOT NULL
          AND owner_guid IS NOT NULL
    """
    params: list = []
    if before_season is not None:
        sql += " AND season < ?"
        params.append(int(before_season))
    rows = [dict(r) for r in conn.execute(sql, params)]
    by_season: dict[int, list[dict]] = {}
    for row in rows:
        by_season.setdefault(row["season"], []).append(row)

    scored = []
    for season, picks in by_season.items():
        # The pick numbers actually used that season, in order.
        slots = sorted(p["overall_pick"] for p in picks)
        # Those same picks, in ESPN's recommended order.
        ordered = sorted(picks, key=lambda p: p["market_rank"])
        for expected, pick in zip(slots, ordered):
            scored.append({
                **pick,
                "expected_pick": expected,
                "reach": expected - pick["overall_pick"],
            })
    return scored


def reach_profile(conn, before_season: int | None = None) -> dict:
    """Per-manager reach tendency: do they jump the market, or let it come?"""
    scored = reach_picks(conn, before_season=before_season)
    if not scored:
        return {
            "_league": {"n": 0},
            "note": "no market ranks stamped - run tools/espn_adp.py then "
                    "python -m app.ingest --market-ranks",
        }

    def blank():
        return {
            "reaches": [], "by_pos": {}, "by_phase": {},
            "biggest_reach": None, "biggest_wait": None,
        }

    agg: dict[str, dict] = {}
    league = blank()

    for pick in scored:
        reach = pick["reach"]
        for bucket in (agg.setdefault(pick["owner_guid"], blank()), league):
            bucket["reaches"].append(reach)
            bucket["by_pos"].setdefault(pick["position"], []).append(reach)
            bucket["by_phase"].setdefault(
                phase_for(int(pick["round"])), []
            ).append(reach)

        entry = {
            "player": pick["player_name"], "season": pick["season"],
            "round": pick["round"], "position": pick["position"],
            "actual_pick": pick["overall_pick"],
            "expected_pick": pick["expected_pick"],
            "reach": reach,
        }
        owner = agg[pick["owner_guid"]]
        if owner["biggest_reach"] is None or reach > owner["biggest_reach"]["reach"]:
            owner["biggest_reach"] = entry
        if owner["biggest_wait"] is None or reach < owner["biggest_wait"]["reach"]:
            owner["biggest_wait"] = entry

    def summarize(bucket):
        values = bucket["reaches"]
        if not values:
            return None
        n = len(values)
        return {
            "n": n,
            "mean_reach": round(sum(values) / n, 1),
            "median_reach": round(_median(values), 1),
            "reach_rate": round(
                sum(1 for v in values if v >= REACH_ROUND) / n, 3
            ),
            "wait_rate": round(
                sum(1 for v in values if v <= -REACH_ROUND) / n, 3
            ),
            "by_position": {
                pos: round(sum(v) / len(v), 1)
                for pos, v in bucket["by_pos"].items() if len(v) >= 3
            },
            "by_phase": {
                phase: round(sum(v) / len(v), 1)
                for phase, v in bucket["by_phase"].items() if len(v) >= 3
            },
        }

    out = {"_league": summarize(league) or {"n": 0}}
    out["_league"]["seasons"] = sorted({p["season"] for p in scored})
    out["_league"]["reach_round"] = REACH_ROUND
    for guid, bucket in agg.items():
        summary = summarize(bucket)
        if not summary:
            continue
        summary["biggest_reach"] = bucket["biggest_reach"]
        summary["biggest_wait"] = bucket["biggest_wait"]
        out[guid] = summary
    return out


# --------------------------------------------------------------------------- #
# projection: what happens before my next pick
# --------------------------------------------------------------------------- #

def roster_depth_curve(picks: list[dict]) -> dict[str, dict[int, float]]:
    """Measured P(this manager takes another at this position | already holds n).

    Replaces hand-picked caps and penalties with the thing they were guessing at.
    For each position, look at how many of that position every manager finished each
    season with, then read off the conditional probability of going deeper:

        multiplier(pos, held) = P(final count > held | final count >= held)

    On this league that yields TE 1 -> 0.59, TE 2 -> 0.17, TE 3 -> 0.10, and
    K 1 -> 0.05 - so a second tight end is ordinary, a third is unusual, and a
    second kicker is close to unheard of. Which is exactly the shape a hand-tuned
    penalty was trying and failing to express.

    Laplace-smoothed so nothing is ever exactly zero: an unobserved depth is
    improbable, not forbidden.
    """
    finals: dict[tuple, dict[str, int]] = {}
    for pick in picks:
        key = (pick["season"], pick["owner_guid"])
        finals.setdefault(key, {})
        pos = pick["position"]
        finals[key][pos] = finals[key].get(pos, 0) + 1

    counts_by_pos: dict[str, list[int]] = {}
    for mix in finals.values():
        for pos in POSITIONS:
            counts_by_pos.setdefault(pos, []).append(mix.get(pos, 0))

    curve: dict[str, dict[int, float]] = {}
    for pos, counts in counts_by_pos.items():
        if not counts:
            continue
        curve[pos] = {}
        for held in range(0, max(counts) + 2):
            at_least = sum(1 for c in counts if c >= held)
            more = sum(1 for c in counts if c > held)
            if at_least == 0:
                curve[pos][held] = OVER_CAP_MULTIPLIER
                continue
            curve[pos][held] = max(
                (more + 0.25) / (at_least + 1.0), OVER_CAP_MULTIPLIER
            )
    return curve


def starter_targets(league: dict) -> dict[str, float]:
    """How many of each position a manager wants as starters.

    Dedicated slots plus that position's share of the FLEX slots. In a league with
    one TE slot and two FLEX, the target for TE is 1 + 2*0.10 = 1.2 - so a *second*
    tight end is depth, not a starter. That distinction is the whole point.
    """
    slots = league.get("roster_slots", {})
    flex = int(slots.get("FLEX", 0))
    return {
        pos: int(slots.get(pos, 0)) + flex * FLEX_SHARE.get(pos, 0.0)
        for pos in POSITIONS
    }


def _need_multipliers(
    conn,
    manager_id: int,
    league: dict,
    round_number: int | None = None,
    depth_curve: dict | None = None,
) -> dict[str, float]:
    """Weight positions by how badly this manager still needs one.

    Three tiers, replacing an earlier flex check that was badly wrong: it treated
    every flex-eligible position as a live starter need while *any* FLEX slot sat
    open, so a manager holding two tight ends still scored TE at full weight and
    could take a third. Counting against a per-position target and a hard cap fixes
    that, and it applies to the projections as well as to mock bots.
    """
    needs = draft.roster_needs(conn, manager_id, league)
    counts = needs.get("_counts", {})
    targets = starter_targets(league)

    slots = league.get("roster_slots", {})
    multipliers = {}
    for pos in POSITIONS:
        held = counts.get(pos, 0)
        if depth_curve and pos in depth_curve:
            # Measured probability this manager goes deeper here. Supersedes the
            # target/cap tiers entirely - it encodes both, from real behaviour.
            multipliers[pos] = depth_curve[pos].get(held, OVER_CAP_MULTIPLIER)
        elif held >= POSITION_ROSTER_CAP.get(pos, 6):
            multipliers[pos] = OVER_CAP_MULTIPLIER
        elif held < targets.get(pos, 0):
            multipliers[pos] = 1.0
        else:
            multipliers[pos] = FILLED_SLOT_PENALTY

    # Endgame pressure. Nobody finishes a draft without a kicker when they have a
    # kicker slot, but the ordinary weights make K so unattractive that a bot could
    # run out of rounds still missing one. Once the picks remaining are barely
    # enough to cover the unfilled mandatory slots, those positions get urgent.
    if round_number is not None:
        picks_left = int(league.get("rounds", 18)) - int(round_number) + 1
        unfilled = [
            pos for pos in POSITIONS
            if counts.get(pos, 0) < int(slots.get(pos, 0))
        ]
        if unfilled:
            if picks_left <= len(unfilled):
                # No slack left. Every remaining pick has to be a mandatory slot,
                # so nothing else is even a candidate. A 6x nudge was not enough:
                # it still left roughly two managers per mock without a kicker.
                for pos in POSITIONS:
                    multipliers[pos] = (
                        MUST_FILL_MULTIPLIER if pos in unfilled else 0.0
                    )
            elif picks_left <= len(unfilled) + 2:
                for pos in unfilled:
                    multipliers[pos] = MUST_FILL_MULTIPLIER
    return multipliers


def _signal_field(signal: str) -> str:
    return signal if signal in ("adp", "espn_adp") else "ecr"


# How many picks before his own ADP a player becomes an unlikely selection. Same
# shape as the position-level bargain term, applied per player.
#
# Backtested over 608 out-of-sample picks. Player-level accuracy against the scale:
#
#     scale     loss    top-1   top-3
#        12   4.7683   15.0%   36.3%
#        18   4.6259   15.8%   36.2%
#        24   4.5659   16.1%   35.7%   <- adopted, best top-1
#       off   4.4593   15.1%   34.4%
#
# Log-loss is *best* with the penalty off, and it is wrong to follow here. Softening
# a distribution always flatters log-loss, because hedging is never punished; the
# accuracy metrics are what measure whether the model picks the right player, and
# both improve. 24 also matches BEST_AVAILABLE_SCALE - both express "picks of slack".
PLAYER_REACH_SCALE = 24.0


# Reach tolerance is not constant - it grows steadily through a draft. Measured on
# this league (reach = ADP - pick, positive meaning taken early):
#
#     round      median   p90     max    >=20 early
#     R1            0.2   3.1     4.0        0%
#     R2-3          1.7   8.0    19.7        0%
#     R4-8          2.6  20.3    37.8       11%
#     R9-13        13.8  53.3   132.3       44%
#
# Across 44 real round-1 picks the largest reach was FOUR picks: round 1 is
# essentially locked to the market. A flat scale cannot express that and produced
# tight ends going 30-60 picks early. Since exp(-2.3) ~ 0.1, a scale of s puts the
# 90th percentile reach near 2.3*s, so this line is fitted to the p90 column.
PLAYER_REACH_SCALE_BASE = 1.0
PLAYER_REACH_SCALE_PER_ROUND = 1.8


def player_reach_scale(round_number: int | None) -> float:
    if round_number is None:
        return PLAYER_REACH_SCALE
    return max(
        1.0,
        PLAYER_REACH_SCALE_BASE + PLAYER_REACH_SCALE_PER_ROUND * (int(round_number) - 1),
    )


def player_reach_weight(
    overall: int, rank: float | None, round_number: int | None = None
) -> float:
    """How plausible is taking THIS player at THIS pick, given where he is ranked.

    The position-level availability term asks whether a position is worth taking by
    looking at its best remaining player. It cannot see which player is then chosen,
    so once a position is selected the model would happily take anyone still on its
    board - which is how a tight end with ADP 80 went at pick 22, a 59-pick reach,
    purely because the better tight ends were gone.

    A player who has *fallen* past his ADP is a bargain and unpenalised. Taking one
    early decays exponentially in how early. That is also why deviation naturally
    grows in later rounds: by then the pick number has caught up with everyone's
    ADP, so nothing is a reach any more.
    """
    if rank is None:
        return 1.0
    slack = (overall - float(rank)) / player_reach_scale(round_number)
    return 1.0 if slack >= 0 else math.exp(slack)


def _best_available_rank(conn, pos: str, signal: str) -> tuple[float | None, str]:
    """Rank of the best remaining player at a position, under one signal.

    Returns (rank, signal_actually_used). Either ADP covers only the top few
    hundred players, so deep in a draft no remaining player at a position has one.
    Falling back to ECR there keeps the model working; returning a neutral 1.0
    instead would quietly flatten the distribution exactly when it matters most.
    """
    field = _signal_field(signal)
    players = draft.available(conn, pos, None, field, 1)
    if not players:
        return None, "empty"
    value = players[0].get(field)
    if value is not None:
        return float(value), signal
    if field != "ecr":
        alt = draft.available(conn, pos, None, "ecr", 1)
        if alt and alt[0].get("ecr") is not None:
            return float(alt[0]["ecr"]), "ecr"
    return None, "unranked"


def _availability_factors(
    conn, overall: int, signal: str = "ecr"
) -> tuple[dict[str, float], dict]:
    """Weight positions by what is actually still sitting on the board.

    Two effects, both real:

    * A position with nobody left cannot be picked, so its factor is zero.
    * A position whose best available player is ranked well ahead of the current
      pick is a bargain, and bargains get taken. A consensus top-5 player still
      there at pick 25 is far more likely to go than his position's base rate
      suggests.

    `signal` chooses the ranking that defines "ahead of value": `ecr` is expert
    consensus, `adp` is where players actually come off the board in ESPN
    leagues. For a league that drafts on ESPN, ADP is arguably the better model
    of opponents, since it is the ordering they see in the draft room.

    This is a heuristic, not a fitted model. It is clamped by
    `VALUE_FACTOR_RANGE` so it can reshape a manager's distribution without
    overriding their demonstrated habits.
    """
    low, high = VALUE_FACTOR_RANGE
    factors: dict[str, float] = {}
    meta = {"signal": signal, "fell_back_to_ecr": [], "ranks": {}}
    for pos in POSITIONS:
        rank, used = _best_available_rank(conn, pos, signal)
        if rank is None:
            factors[pos] = 0.0 if used == "empty" else 1.0
            continue
        if used != signal:
            meta["fell_back_to_ecr"].append(pos)
        meta["ranks"][pos] = round(rank, 1)
        slack = (overall - rank) / BEST_AVAILABLE_SCALE
        if slack >= 0:
            # A player ranked earlier than this pick is still here: a bargain.
            factors[pos] = min(1.0 + slack, high)
        else:
            # Best man left is ranked later than this pick, so taking him is a
            # reach. Decay exponentially rather than flooring out.
            factors[pos] = max(math.exp(slack), low)
    return factors, meta


def _position_distribution(
    shares, guid, phase, base, multipliers, availability=None
) -> dict[str, float]:
    """Blend an owner's shrunk phase rates with roster needs and board value."""
    if guid and guid in shares and phase in shares[guid]:
        rates = shares[guid][phase]["rates"]
        n = shares[guid][phase]["n"]
    else:
        rates = base[phase]["rates"]
        n = 0
    availability = availability or {}
    weighted = {
        p: rates.get(p, 0.0)
        * multipliers.get(p, 1.0)
        * availability.get(p, 1.0)
        for p in POSITIONS
    }
    total = sum(weighted.values())
    if total <= 0:
        # Nothing wanted anywhere. Spread over positions that at least have roster
        # room left rather than uniformly over all six - a uniform fallback is what
        # let capped positions come back from the dead and produce six kickers.
        room = [p for p in POSITIONS if multipliers.get(p, 1.0) > 0]
        pool = room or list(POSITIONS)
        return {
            "_n": n,
            **{p: (1.0 / len(pool) if p in pool else 0.0) for p in POSITIONS},
        }
    return {"_n": n, **{p: weighted[p] / total for p in POSITIONS}}


# Three distinct views of the market, kept separate because they measure different
# things: ECR is what analysts recommend, `adp` is broad consensus draft position
# from the rankings files, `espn_adp` is specifically where players come off the
# board in ESPN leagues - the platform this league actually drafts on.
def _apply_round_floor(multipliers: dict, earliest: dict, round_number) -> dict:
    """Zero positions nobody in this league has ever taken this early.

    Three coarse phases treat round 9 and round 18 alike, so a 10% late-phase
    kicker rate is enough to put a kicker off the board in round 7. This league has
    never taken one before round 14 - measured, K earliest R14, DST earliest R13.

    Mutates and returns `multipliers`. A must-fill always wins: in the last round
    with a mandatory slot empty, filling it beats the convention.
    """
    if round_number is None:
        return multipliers
    for pos, floor in earliest.items():
        if (
            int(round_number) < int(floor)
            and multipliers.get(pos, 0.0) < MUST_FILL_MULTIPLIER
        ):
            multipliers[pos] *= ROUND_FLOOR_MULTIPLIER
    return multipliers


AVAILABILITY_SIGNALS = ("ecr", "adp", "espn_adp")
SIGNAL_LABELS = {
    "ecr": "ECR", "adp": "Consensus ADP", "espn_adp": "ESPN ADP",
}


def _ranked_distribution(conn, entry, manager, shares, base, league, signal):
    """One pick's position distribution under one availability signal."""
    phase = phase_for(entry["round"])
    multipliers = _need_multipliers(
        conn, manager["id"], league, round_number=entry["round"],
        depth_curve=shares.get("_depth"),
    )

    _apply_round_floor(multipliers, shares.get("_earliest", {}), entry["round"])
    availability, meta = _availability_factors(conn, entry["overall"], signal)
    dist = _position_distribution(
        shares, manager.get("owner_guid"), phase, base, multipliers, availability
    )
    n = dist.pop("_n")
    ranked = sorted(dist.items(), key=lambda kv: kv[1], reverse=True)
    return ranked, n, phase, meta


def project_upcoming(
    conn,
    limit: int | None = None,
    signal: str = "ecr",
    compare: bool = False,
    compare_with: str | None = None,
) -> dict:
    """Position probabilities for each pick between now and my next turn.

    `signal` selects which ranking drives the board-availability term — see
    `_availability_factors`. With `compare=True` each position also carries the
    probability the other signal would have produced, so the two can be read
    side by side without a second request.
    """
    if signal not in AVAILABILITY_SIGNALS:
        signal = "ecr"
    league = db.get_league(conn)
    current = draft.current_pick(conn, league)
    if current is None:
        return {"picks": [], "note": "draft complete"}

    timing = draft.picks_until_my_turn(conn, league)
    if limit is not None:
        # An explicit limit means "the next N picks, whoever they belong to". The
        # board panel must keep projecting while I am on the clock, when
        # picks-until-my-turn is 0.
        horizon = limit
    else:
        horizon = timing.get("until_next") or 0

    shares = position_share(conn)
    base = shares["_league"]
    reach = reach_profile(conn)
    order = draft.pick_order(league)
    slot_to_manager = {m["draft_slot"]: m for m in draft.managers(conn)}
    made = conn.execute("SELECT COUNT(*) AS n FROM picks").fetchone()["n"]

    projected = []
    # Bound before the loop: with a zero horizon the loop never runs, and the
    # return statement still reports which signal the comparison used. With three
    # signals "the other one" is ambiguous, so the baseline is explicit - default
    # to ECR, or to consensus ADP when ECR is already the active signal.
    if compare:
        other_signal = compare_with or ("adp" if signal == "ecr" else "ecr")
        if other_signal not in AVAILABILITY_SIGNALS or other_signal == signal:
            other_signal = "adp" if signal == "ecr" else "ecr"
    else:
        other_signal = None

    for entry in order[made:made + max(horizon, 0)]:
        manager = slot_to_manager.get(entry["slot"])
        if not manager:
            continue
        guid = manager.get("owner_guid")
        ranked, n, phase, meta = _ranked_distribution(
            conn, entry, manager, shares, base, league, signal
        )

        other_lookup = {}
        if compare:
            other_ranked, _, _, other_meta = _ranked_distribution(
                conn, entry, manager, shares, base, league, other_signal
            )
            other_lookup = dict(other_ranked)
            meta["compare"] = {
                "signal": other_signal,
                "fell_back_to_ecr": other_meta["fell_back_to_ecr"],
                "ranks": other_meta["ranks"],
            }

        positions = []
        for pos, probability in ranked:
            item = {"position": pos, "p": round(probability, 3)}
            if compare:
                alt = other_lookup.get(pos, 0.0)
                item["p_other"] = round(alt, 3)
                item["delta"] = round(probability - alt, 3)
            positions.append(item)

        needs = draft.roster_needs(conn, manager["id"], league)
        projected.append({
            "overall": entry["overall"],
            "round": entry["round"],
            "slot": entry["slot"],
            "manager_id": manager["id"],
            "manager_name": manager["name"],
            "is_me": manager.get("is_me", 0),
            "has_history": bool(guid) and n > 0,
            "sample_size": n,
            "phase": phase,
            "open_needs": [
                pos for pos in ("QB", "RB", "WR", "TE", "FLEX", "K", "DST")
                if needs.get(pos, 0) > 0
            ],
            "roster_size": needs.get("_drafted", 0),
            "positions": positions,
            "likely": _likely_players(conn, ranked[:3], signal),
            "availability": meta,
            "reach": _reach_for_pick(reach.get(guid), [p for p, _ in ranked[:4]]),
        })
    return {
        "picks": projected,
        "my_next_overall": timing.get("my_next_overall"),
        "horizon": len(projected),
        "signal": signal,
        "compare_signal": other_signal,
    }


# A reach worth surfacing mid-draft. Below this the number is noise against a
# ~50-pick sample and would just clutter the panel.
REACH_NOTABLE = 5


def _reach_for_pick(profile: dict | None, positions: list[str]) -> dict | None:
    """Trim a manager's reach profile to what a live draft panel needs.

    Median rather than mean: reach distributions have heavy tails, and on this
    league three of twelve managers have a mean and median that disagree in sign.
    The median is the better description of a habit.
    """
    if not profile or not profile.get("n"):
        return None
    median = profile["median_reach"]
    by_position = profile.get("by_position") or {}
    return {
        "n": profile["n"],
        "median": median,
        "mean": profile["mean_reach"],
        "reach_rate": profile["reach_rate"],
        "wait_rate": profile["wait_rate"],
        "label": (
            "reaches" if median >= 3 else "waits" if median <= -3 else "near market"
        ),
        # Only the positions this pick is actually likely to be, and only where
        # the tendency is big enough to act on.
        "by_position": {
            pos: by_position[pos]
            for pos in positions
            if pos in by_position and abs(by_position[pos]) >= REACH_NOTABLE
        },
    }


def _likely_players(conn, top_positions, signal: str = "ecr") -> list[dict]:
    """Name the board leader at each of the most likely positions."""
    field = _signal_field(signal)
    out = []
    for pos, probability in top_positions:
        players = draft.available(conn, pos, None, field, 1)
        if players and players[0].get(field) is None and field != "ecr":
            players = draft.available(conn, pos, None, "ecr", 1)
        if players:
            out.append({
                "position": pos,
                "p": round(probability, 3),
                "name": players[0]["name"],
                "player_key": players[0]["player_key"],
            })
    return out


# --------------------------------------------------------------------------- #
# projection: will this player last until my next pick
# --------------------------------------------------------------------------- #

def survival_odds(conn, limit: int = 40, signal: str = "ecr") -> dict:
    """P(still available at my next pick) for the top of the current board.

    ## Model

    For each pick between now and mine, the chance that pick lands on player X
    is P(position of X) * P(X | that position). The second term assumes managers
    reach for the top of the board at a position, with exponentially decaying
    preference down the ranking (`RANK_DECAY`).

    ## Assumptions this makes, which are not free

    Picks are treated as independent, and a player's rank among available
    players is held fixed across the horizon. Neither is strictly true: if X
    survives one pick he is nearer the top of the board at the next, which makes
    him *more* exposed. So these odds are mildly optimistic for players deep in
    the horizon. They are useful for ranking who is at risk, not as calibrated
    probabilities.
    """
    league = db.get_league(conn)
    timing = draft.picks_until_my_turn(conn, league)
    horizon = timing.get("until_next")
    if horizon is None:
        return {"players": [], "note": "no upcoming pick for you"}

    projection = project_upcoming(conn, signal=signal)
    if not projection["picks"]:
        return {
            "players": [
                {**p, "survival": 1.0, "risk": 0.0}
                for p in draft.available(conn, None, None, "ecr", limit)
            ],
            "horizon": 0,
            "note": "you are on the clock",
        }

    # Rank of each candidate within its own position, on the live board. Ordered
    # by the same signal driving the projection: if we are modelling opponents as
    # following ADP, then who they reach for within a position follows ADP too.
    order_field = _signal_field(signal)
    by_position: dict[str, list[dict]] = {}
    for pos in POSITIONS:
        by_position[pos] = draft.available(conn, pos, None, order_field, 30)

    position_weights = {
        pos: _decay_weights(len(players)) for pos, players in by_position.items()
    }

    candidates = draft.available(conn, None, None, order_field, limit)
    index = {
        pos: {p["player_key"]: i for i, p in enumerate(players)}
        for pos, players in by_position.items()
    }

    out = []
    for player in candidates:
        pos = player["position"]
        rank = index.get(pos, {}).get(player["player_key"])
        if rank is None:
            out.append({**player, "survival": 1.0, "risk": 0.0})
            continue
        weight = position_weights[pos][rank]
        survival = 1.0
        for pick in projection["picks"]:
            p_pos = next(
                (d["p"] for d in pick["positions"] if d["position"] == pos), 0.0
            )
            survival *= 1.0 - min(p_pos * weight, 0.99)
        out.append({
            **player,
            "survival": round(survival, 3),
            "risk": round(1.0 - survival, 3),
        })

    out.sort(key=lambda p: p["risk"], reverse=True)
    return {
        "players": out,
        "horizon": len(projection["picks"]),
        "my_next_overall": timing.get("my_next_overall"),
        "signal": signal,
    }


def _decay_weights(count: int) -> list[float]:
    """Normalized exponential preference for the top of a position's board."""
    if count <= 0:
        return []
    raw = [math.exp(-i / RANK_DECAY) for i in range(count)]
    total = sum(raw)
    return [r / total for r in raw]


# --------------------------------------------------------------------------- #
# recommendation: what should I take
# --------------------------------------------------------------------------- #

SIM_TRIALS = 300
# A player who fills an open starter slot is worth more than pure bench depth.
NEED_FACTOR_STARTER = 1.0
NEED_FACTOR_DEPTH = 0.55
# Do not recommend someone who is probably gone before the pick is made.
SUGGEST_MIN_SURVIVAL = 0.15
# At most this many candidates per position in the ranked list.
SUGGEST_MAX_PER_POSITION = 2
# How much the market-scarcity term counts relative to projected value.
#
#     score = projected_value + scarcity_weight * cost_of_waiting
#
# 1.0 weights them equally. Lower it to let this season's projections dominate;
# raise it to chase positional runs harder. Projected value is already the larger
# term at the top of the board - the best RB projects ~190 above replacement while
# the RB drop-off is ~71 - so projections lead by default.
SCARCITY_WEIGHT = 1.0
# How deep into a position's board the simulation will reach for a pick.
SIM_POSITION_DEPTH = 14


# Which projection source supplies the value a suggestion is scored on.
#
#   rankings  RotoBaller/FantasyPros projections        -> vor
#   vegas     winwithodds season totals from odds       -> vegas_vor
#   blend     weighted average of the two               -> blend_vor
#
# `vegas_vor` is already rescaled into `vor` units at ingest, so averaging them
# is arithmetic on one scale rather than two.
VALUE_SOURCES = {"rankings": "vor", "vegas": "vegas_vor", "blend": "blend_vor"}
DEFAULT_VALUE_SOURCE = "blend"
# How much of the blend is Vegas. 0.5 = equal say. Two independent projections
# averaged beat either one alone often enough that an even split is the honest
# default; there is no backtest behind this number, because no historical
# winwithodds file exists to fit one on.
VEGAS_BLEND_WEIGHT = 0.5


def _blend_vor(player: dict, weight: float = VEGAS_BLEND_WEIGHT) -> float | None:
    """Average the two VORs, tolerating a player only one source covers."""
    primary = player.get("vor")
    vegas = player.get("vegas_vor")
    if primary is None and vegas is None:
        return None
    if vegas is None:
        return float(primary)
    if primary is None:
        return float(vegas)
    return (1.0 - weight) * float(primary) + weight * float(vegas)


def _goodness(player: dict, basis: str) -> float | None:
    """A single higher-is-better number for a player, in consistent units.

    VOR is points above positional replacement, which is the right currency for
    a draft decision. When the pool has no projections (so no VOR), the fallback
    is negated ECR: still higher-is-better, still consistent, but ranks rather
    than points. Mixing the two would make the arithmetic meaningless, so the
    basis is chosen once and reported.
    """
    if basis == "ecr":
        ecr = player.get("ecr")
        return None if ecr is None else -float(ecr)
    if basis == "blend_vor":
        return _blend_vor(player)
    value = player.get(basis)
    return None if value is None else float(value)


def _best_available(entries, taken, basis, depth=SIM_POSITION_DEPTH):
    """Highest-goodness player at a position that this trial has not taken."""
    best, best_value = None, None
    seen = 0
    for player in entries:
        if player["player_key"] in taken:
            continue
        value = _goodness(player, basis)
        seen += 1
        if value is not None and (best_value is None or value > best_value):
            best, best_value = player, value
        if seen >= depth:
            break
    return best, best_value


def _sample_pick(entries, taken, weights, rng, overall=None, field="ecr",
                 round_number=None):
    """Pick a player at one position, favouring the top of its board."""
    available = []
    for player in entries:
        if player["player_key"] not in taken:
            available.append(player)
        if len(available) >= len(weights):
            break
    if not available:
        return None
    pool_weights = weights[:len(available)]
    if overall is not None:
        pool_weights = [
            w * player_reach_weight(overall, p.get(field), round_number)
            for w, p in zip(pool_weights, available)
        ]
    total = sum(pool_weights)
    if total <= 0:
        return available[0]
    target = rng.random() * total
    # Walk the cumulative distribution.
    running = 0.0
    for player, weight in zip(available, pool_weights):
        running += weight
        if running >= target:
            return player
    return available[-1]


def suggest_pick(
    conn,
    trials: int = SIM_TRIALS,
    limit: int = 6,
    signal: str = "ecr",
    seed: int | None = 12345,
    scarcity_weight: float = SCARCITY_WEIGHT,
    value_source: str = DEFAULT_VALUE_SOURCE,
) -> dict:
    """Recommend a pick by simulating the draft forward to your turn, and past it.

    ## Why a simulation and not the marginal projections

    `project_upcoming` computes each upcoming pick against the board as it stands,
    independently. That is fine for "who is likely to go next", but useless for
    "what should I take", because it never removes the players those picks would
    consume. This runs the intervening picks jointly, `trials` times, so the board
    that arrives at your turn is a distribution rather than a guess.

    ## The scoring

    For each position the simulation measures the value of the best player who
    survives to your pick, and the value of the best who survives to the pick
    after that. The gap is the cost of waiting:

        VONA(pos) = E[best value at my pick] - E[best value at my following pick]

    That is Value Over Next Available: how much you lose at a position by taking
    something else now. Both terms are in the same units, so:

        score = (value + VONA) * need_factor

    `need_factor` is 1.0 for a player filling an open starter slot and 0.55 for
    pure depth. Candidates unlikely to survive to your pick are dropped, since
    recommending them is pointless.

    ## Assumptions worth knowing

    Managers' position distributions are computed once, from their rosters as they
    stand now, and are not recomputed inside a trial. Over a one-to-two round
    horizon that is a reasonable simplification, but it means a manager can take
    two of the same position in a trial slightly more often than they would in
    reality. Your own pick at your turn is assumed to be the highest-value player
    available, to avoid the recommendation depending on itself.
    """
    rng = random.Random(seed)
    league = db.get_league(conn)
    current = draft.current_pick(conn, league)
    if current is None:
        return {"suggestions": [], "note": "draft complete"}

    me = draft.my_manager(conn)
    if not me:
        return {"suggestions": [], "note": "no seat marked as yours"}

    timing = draft.picks_until_my_turn(conn, league)
    my_next = timing.get("my_next_overall")
    if my_next is None:
        return {"suggestions": [], "note": "no picks remaining for you"}
    my_following = timing.get("my_following_overall")

    made = conn.execute("SELECT COUNT(*) AS n FROM picks").fetchone()["n"]
    order = draft.pick_order(league)
    slot_to_manager = {m["draft_slot"]: m for m in draft.managers(conn)}
    shares = position_share(conn)
    base = shares["_league"]

    # Every pick from now up to and including my following turn.
    before_me = [e for e in order[made:] if e["overall"] < my_next]
    after_me = (
        [e for e in order if my_next < e["overall"] < my_following]
        if my_following else []
    )

    def distribution_for(entry):
        manager = slot_to_manager.get(entry["slot"])
        if not manager:
            return None
        ranked, _, _, _ = _ranked_distribution(
            conn, entry, manager, shares, base, league, signal
        )
        return [(pos, p) for pos, p in ranked if p > 0]

    # Carry the pick number alongside each distribution: the per-player reach
    # penalty needs to know how early in the draft the pick is being made.
    plan_before = [
        (e["overall"], e["round"], d)
        for e, d in ((e, distribution_for(e)) for e in before_me) if d
    ]
    plan_after = [
        (e["overall"], e["round"], d)
        for e, d in ((e, distribution_for(e)) for e in after_me) if d
    ]

    # Load the board once; the simulation runs entirely in memory.
    #
    # The candidate set is the union of the top of the market board AND the top by
    # projected points. Taking only the market's top N would let a player the
    # projections love get excluded because his ECR or ADP is mediocre - which is
    # precisely the disagreement worth acting on.
    order_field = _signal_field(signal)
    market_board = draft.available(conn, None, None, order_field, 320)
    projection_board = draft.available(conn, None, None, "proj", 320)
    board, seen = [], set()
    for player in market_board + projection_board:
        if player["player_key"] in seen:
            continue
        seen.add(player["player_key"])
        board.append(player)

    # Per-position lists stay in MARKET order: they model who opponents reach for,
    # and opponents follow the market, not my projections.
    by_pos: dict[str, list[dict]] = {}
    for player in market_board:
        by_pos.setdefault(player["position"], []).append(player)
    weights = _decay_weights(SIM_POSITION_DEPTH)

    # Honour the requested value source, but only if it is actually populated.
    # Asking for Vegas with no file in data/vegas/ must fall back to the rankings
    # projections rather than score every player as None and rank on noise.
    wanted = VALUE_SOURCES.get(value_source or DEFAULT_VALUE_SOURCE, "vor")
    head = board[:40]

    def covered(field):
        return sum(1 for p in head if p.get(field) is not None)

    if wanted == "vegas_vor" and covered("vegas_vor") < 3:
        wanted = "vor"
    elif wanted == "blend_vor" and covered("vegas_vor") < 3:
        wanted = "vor"
    basis = wanted if (
        covered("vor") >= 3 or covered("vegas_vor") >= 3
    ) else "ecr"

    # Share the one need model rather than keeping a second copy. An earlier
    # duplicate here had the same flex bug that let a manager hold three tight
    # ends, and no roster caps at all - so the suggestion box would happily
    # recommend a second kicker and a fifth defence. This is the engine you follow
    # on draft night, so it has to agree with the projections about what a need is.
    my_round = next(
        (e["round"] for e in draft.pick_order(league) if e["overall"] == my_next),
        None,
    )
    need_multipliers = _need_multipliers(
        conn, me["id"], league, round_number=my_round,
        depth_curve=shares.get("_depth"),
    )
    # Without this the box recommends a kicker in round 7: an empty K slot scores
    # need 1.0 while your running backs sit at 0.35, and K's value-over-replacement
    # is enough to win. Nobody in this league has taken a kicker before round 14.
    _apply_round_floor(need_multipliers, shares.get("_earliest", {}), my_round)

    def need_factor(pos):
        return need_multipliers.get(pos, NEED_FACTOR_DEPTH)

    def sample_position(dist):
        target = rng.random() * sum(p for _, p in dist)
        running = 0.0
        for pos, p in dist:
            running += p
            if running >= target:
                return pos
        return dist[-1][0]

    survived: dict[str, int] = {}
    best_at_mine: dict[str, list[float]] = {}
    best_at_next: dict[str, list[float]] = {}

    for _ in range(trials):
        taken: set[str] = set()

        for overall, rnd, dist in plan_before:
            pos = sample_position(dist)
            entries = by_pos.get(pos)
            if not entries:
                continue
            chosen = _sample_pick(
                entries, taken, weights, rng, overall, order_field, rnd
            )
            if chosen:
                taken.add(chosen["player_key"])

        # State of the board at my pick.
        for player in board:
            if player["player_key"] not in taken:
                survived[player["player_key"]] = survived.get(
                    player["player_key"], 0
                ) + 1
        mine_best = {}
        for pos, entries in by_pos.items():
            best, value = _best_available(entries, taken, basis)
            if value is not None:
                best_at_mine.setdefault(pos, []).append(value)
                mine_best[pos] = best

        # Assume I take the best available overall, then run to my next turn.
        my_take = max(
            (b for b in mine_best.values() if b),
            key=lambda p: _goodness(p, basis) or -1e9,
            default=None,
        )
        if my_take:
            taken.add(my_take["player_key"])
        for overall, rnd, dist in plan_after:
            pos = sample_position(dist)
            entries = by_pos.get(pos)
            if not entries:
                continue
            chosen = _sample_pick(
                entries, taken, weights, rng, overall, order_field, rnd
            )
            if chosen:
                taken.add(chosen["player_key"])
        for pos, entries in by_pos.items():
            _, value = _best_available(entries, taken, basis)
            if value is not None:
                best_at_next.setdefault(pos, []).append(value)

    def mean(values):
        return sum(values) / len(values) if values else None

    vona = {}
    for pos in by_pos:
        now = mean(best_at_mine.get(pos, []))
        later = mean(best_at_next.get(pos, []))
        if now is None:
            continue
        vona[pos] = {
            "position": pos,
            "expected_best_now": round(now, 1),
            "expected_best_later": round(later, 1) if later is not None else None,
            "vona": round(now - later, 1) if later is not None else 0.0,
            "need": need_factor(pos) >= NEED_FACTOR_STARTER,
        }

    suggestions = []
    for player in board:
        value = _goodness(player, basis)
        if value is None:
            continue
        pos = player["position"]
        survival = survived.get(player["player_key"], 0) / trials
        if survival < SUGGEST_MIN_SURVIVAL:
            continue
        drop = vona.get(pos, {}).get("vona", 0.0)
        factor = need_factor(pos)
        # A capped position is not a recommendation, it is a mistake. Drop rather
        # than list with a zero score.
        if factor <= 0:
            continue
        # Ingest sets proj_points = pts_last for anyone the projections files did
        # not cover, so an identical pair means this player's value is last
        # season's scoring wearing a projection's name. Flagged per player, not
        # just league-wide: a partially-covered pool gives real projections at the
        # top of the board and fallbacks deeper down.
        last = player.get("pts_last")
        projected = player.get("proj_points")
        from_projection = not (
            projected is not None and last is not None and abs(projected - last) <= 0.1
        )
        suggestions.append({
            "value_from_projection": from_projection,
            "player_key": player["player_key"],
            "name": player["name"],
            "position": pos,
            "team": player["team"],
            "bye": player["bye"],
            "ecr": player["ecr"],
            "adp": player["adp"],
            "espn_adp": player["espn_adp"],
            "proj_points": player["proj_points"],
            "value": round(value, 1),
            "vona": round(drop, 1),
            "need": factor >= NEED_FACTOR_STARTER,
            "survival": round(survival, 3),
            "score": round((value + scarcity_weight * drop) * factor, 1),
        })

    suggestions.sort(key=lambda s: s["score"], reverse=True)

    # VONA is constant within a position, so an unfiltered list is just the top N
    # of whichever position has the steepest drop-off - six running backs tells you
    # nothing you could act on. Capping per position surfaces the actual trade-off.
    capped, per_position = [], {}
    for item in suggestions:
        pos = item["position"]
        if per_position.get(pos, 0) >= SUGGEST_MAX_PER_POSITION:
            continue
        per_position[pos] = per_position.get(pos, 0) + 1
        capped.append(item)
        if len(capped) >= limit:
            break

    for rank, item in enumerate(capped, start=1):
        item["rank"] = rank
        item["reason"] = _suggestion_reason(item, basis)
    suggestions = capped

    # Whether "value" is a projection or just last season's scoring decides how
    # much any of this is worth. Detectable: with no projections column, ingest
    # sets proj_points equal to pts_last for every player.
    forward_looking = conn.execute(
        "SELECT COUNT(*) AS n FROM players WHERE proj_points IS NOT NULL "
        "AND pts_last IS NOT NULL AND ABS(proj_points - pts_last) > 0.1"
    ).fetchone()["n"]

    notes = []
    fallback_shown = sum(
        1 for s in suggestions if not s.get("value_from_projection", True)
    )
    if basis != "ecr" and forward_looking and fallback_shown:
        notes.append(
            f"{fallback_shown} of these have no 2026 projection, so their value is "
            "last season's scoring — marked on the row."
        )
    # Only the rankings projections fall back to last season's scoring. Vegas
    # totals are forward-looking by construction, so on a pure-Vegas basis this
    # warning would be false.
    if basis in ("vor", "blend_vor") and not forward_looking:
        notes.append(
            "Value is last season's actual scoring, not a projection - no FPTS "
            "column in any rankings file. Players who missed time are undervalued "
            "and 2025 breakouts are overvalued. Add a FantasyPros projections "
            "export to fix this."
        )
    if basis == "ecr":
        notes.append(
            "No projections in the pool, so value is expressed in ranking "
            "positions rather than points."
        )

    return {
        "notes": notes,
        "value_is_last_season": basis in ("vor", "blend_vor") and not forward_looking,
        "value_source": value_source,
        "my_next_overall": my_next,
        "my_following_overall": my_following,
        "on_the_clock": bool(current.get("is_me")) and my_next == current["overall"],
        "intervening_picks": len(plan_before),
        "trials": trials,
        "signal": signal,
        "value_basis": basis,
        "scarcity_weight": scarcity_weight,
        "positions": sorted(
            vona.values(), key=lambda v: v["vona"], reverse=True
        ),
        "suggestions": suggestions,
    }


def _suggestion_reason(item: dict, basis: str) -> str:
    bits = []
    unit = "ranks" if basis == "ecr" else "pts"
    bits.append(f"{item['value']:+.0f} {unit} value")
    if item["vona"] >= 5:
        bits.append(f"{item['vona']:.0f} {unit} drop-off if you wait")
    if item["need"]:
        bits.append("fills a starter slot")
    else:
        bits.append("depth only")
    if item["survival"] < 0.6:
        bits.append(f"only {item['survival']:.0%} likely to last")
    if basis in ("vor", "blend_vor") and not item.get("value_from_projection", True):
        bits.append("value is LAST SEASON, no projection for him")
    return "; ".join(bits)


# --------------------------------------------------------------------------- #
# text report
# --------------------------------------------------------------------------- #

def scouting_report(conn) -> str:
    summary = history_summary(conn)
    if not summary["picks"]:
        return (
            "No league history imported yet.\n\n"
            "  python DraftTool.py --seasons 2022 2023 2024 2025\n"
            "  python -m app.ingest --history draft_history.csv\n"
        )

    shares = position_share(conn)
    firsts = first_position_round(conn)
    runs = run_following(conn)
    value = draft_value(conn)
    reach = reach_profile(conn)
    base = shares["_league"]

    lines = [
        f"LEAGUE HISTORY  {summary['first_season']}-{summary['last_season']}"
        f"  ({summary['picks']} picks, {summary['owners']} owners, "
        f"{summary['keepers']} keepers excluded)",
        f"outcome stats attached to {summary['with_outcomes']} picks; "
        f"{summary['linked']} owners linked to board seats",
        "",
        "LEAGUE BASE RATES BY PHASE",
    ]
    for phase, _, _ in PHASES:
        rates = base[phase]["rates"]
        top = sorted(rates.items(), key=lambda kv: kv[1], reverse=True)
        lines.append(
            f"  {phase:<6} " + "  ".join(f"{p} {v:4.0%}" for p, v in top if v > 0.01)
        )
    lines.append(
        f"  run-following: league takes the same position as the previous "
        f"{runs['run_length']} picks {runs['league_rate']:.0%} of the time"
    )
    lines.append("")

    league_reach = reach.get("_league", {})
    if league_reach.get("n"):
        lines.extend(_reach_legend(league_reach))
    elif reach.get("note"):
        lines.append(f"REACH: {reach['note']}")
    lines.append("")

    league_value = value.get("_league", {})
    if league_value.get("n"):
        lines.extend(_value_legend(league_value))
        lines.append("")

    for owner in owners(conn):
        guid = owner["owner_guid"]
        lines.append(
            f"{owner['owner_label']}  "
            f"({owner['picks']} picks, {owner['seasons']} seasons"
            + (", linked" if owner["manager_id"] else ", NOT linked to a seat")
            + ")"
        )

        for phase, _, _ in PHASES:
            bucket = shares.get(guid, {}).get(phase)
            if not bucket or not bucket["n"]:
                continue
            ranked = sorted(
                bucket["rates"].items(), key=lambda kv: kv[1], reverse=True
            )[:4]
            deltas = []
            for pos, rate in ranked:
                delta = rate - base[phase]["rates"][pos]
                arrow = "+" if delta > 0.05 else ("-" if delta < -0.05 else " ")
                deltas.append(f"{pos} {rate:4.0%}{arrow}")
            lines.append(f"  {phase:<6} n={bucket['n']:<3} " + "  ".join(deltas))

        timing = firsts.get(guid, {})
        notable = [
            f"{pos} R{int(v['median'])}" + (f" ({v['earliest']}-{v['latest']})"
                                            if v["earliest"] != v["latest"] else "")
            for pos, v in sorted(timing.items(), key=lambda kv: kv[1]["median"])
            if v["n"] >= 2
        ]
        if notable:
            lines.append("  first taken (median round):  " + ",  ".join(notable))

        run = runs["owners"].get(guid)
        if run and run["faced"] >= 4:
            direction = "chases" if run["vs_league"] > 0.05 else (
                "resists" if run["vs_league"] < -0.05 else "neutral on"
            )
            lines.append(
                f"  {direction} runs: {run['followed']}/{run['faced']} "
                f"({run['rate']:.0%} vs league {runs['league_rate']:.0%})"
            )

        rch = reach.get(guid)
        if rch and rch["n"] >= 10:
            lines.extend(_reach_block(rch))

        val = value.get(guid)
        if val and val["n"] >= 10:
            lines.extend(_value_block(val, league_value))
        lines.append("")

    lines.append(
        "Rates are shrunk toward the league base rate; low n means the number is "
        "mostly league average, not a discovered tendency."
    )
    return "\n".join(lines)


def _reach_legend(league: dict) -> list[str]:
    """Explain the reach metric once, including why 0 is the true baseline."""
    seasons = ", ".join(str(s) for s in league.get("seasons", []))
    return [
        "HOW TO READ REACH",
        "  Reach measures whether a manager jumps the market or lets it come to",
        "  them, using ESPN's own draft ranks as the market.",
        "",
        "  Raw rank-minus-pick does not work: ESPN ranks ~1,700 players, most of",
        "  whom nobody drafts, so rank climbs far faster than pick number. So each",
        "  season's picks are re-sorted into ESPN's recommended order and handed",
        "  back the same pick numbers. The best-ranked player gets that season's",
        "  earliest pick, and so on.",
        "",
        "    reach = expected_pick - actual_pick",
        "",
        "  +12 means taken a full round earlier than ESPN's order implies.",
        "  -12 means they waited a round longer than ESPN would have.",
        "  Expected picks are a permutation of the actual picks, so the league",
        "  average is EXACTLY 0. Unlike the outcome metric, 0 is the real bar.",
        "",
        f"  reach/wait rates count picks at least {league['reach_round']} picks "
        "early or late.",
        f"  Seasons covered: {seasons or 'none'}  (ESPN serves no ranks for 2022, "
        "so it is excluded).",
        f"  LEAGUE BASELINE  mean {league['mean_reach']:+.1f}  ·  "
        f"{league['reach_rate']:.0%} reaches  ·  {league['wait_rate']:.0%} waits  "
        f"(n={league['n']})",
    ]


def _describe_reach(entry: dict | None) -> str:
    if not entry:
        return "–"
    return (
        f"{entry['player']} ({entry['season']} R{entry['round']}) "
        f"pick {entry['actual_pick']} vs expected {entry['expected_pick']} "
        f"({entry['reach']:+d})"
    )


def _reach_block(rch: dict) -> list[str]:
    mean, median = rch["mean_reach"], rch["median_reach"]
    tendency = (
        "reaches" if mean >= 3 else "waits" if mean <= -3 else "drafts near market"
    )
    lines = [
        f"  REACH  {tendency}, mean {mean:+.1f} picks vs ESPN order "
        f"(median {median:+.1f}, n={rch['n']})",
        f"    rates      {rch['reach_rate']:.0%} of picks a round-plus early, "
        f"{rch['wait_rate']:.0%} a round-plus late",
    ]
    # A mean and median that disagree in sign means a handful of extreme picks are
    # driving the mean. The median is the better description of their habit.
    if mean * median < 0 and abs(mean - median) >= 3:
        lines.append(
            f"    NOTE       mean and median disagree in sign - a few outlier "
            f"picks pull the mean. Trust the median ({median:+.1f})."
        )
    phases = [
        f"{phase} {rch['by_phase'][phase]:+.0f}"
        for phase, _, _ in PHASES if phase in rch.get("by_phase", {})
    ]
    if phases:
        lines.append("    by phase   " + "   ".join(phases))
    positions = sorted(
        (rch.get("by_position") or {}).items(), key=lambda kv: kv[1], reverse=True
    )
    if positions:
        lines.append(
            "    by position " + "  ".join(f"{p} {v:+.0f}" for p, v in positions)
        )
    lines.append(f"    most early {_describe_reach(rch.get('biggest_reach'))}")
    lines.append(f"    most late  {_describe_reach(rch.get('biggest_wait'))}")
    return lines


def _value_legend(league: dict) -> list[str]:
    """Explain the outcome numbers once, so per-manager lines can stay terse."""
    # Group positions that share a threshold: "QB/TE/K/DST top-12, RB/WR top-24".
    grouped: dict[int, list[str]] = {}
    for pos, limit in STARTER_THRESHOLD.items():
        grouped.setdefault(limit, []).append(pos)
    thresholds = ", ".join(
        f"{'/'.join(positions)} top-{limit}"
        for limit, positions in sorted(grouped.items())
    )
    return [
        "HOW TO READ THE DRAFT RESULTS BELOW",
        "  Each pick is scored by comparing two facts about the player:",
        "    taken   the Nth player at his position off the board that season",
        "            (\"taken RB5\" = the 5th running back drafted)",
        "    finish  where he actually ended that season among all players at",
        "            his position, by fantasy points (\"finished RB2\")",
        "",
        "  value = taken - finish.  Draft the 5th RB, he finishes RB2, that is +3.",
        "  Negative means the player finished worse than where he was drafted.",
        "",
        f"  Finish is measured against EVERY player at the position, including",
        f"  undrafted breakouts who leapfrog drafted busts. So the league-wide",
        f"  average value is {league['mean_delta']:+.1f}, not 0. THAT is the bar.",
        f"  \"+2.0 vs league\" means their picks beat the slot they were taken at",
        f"  by 2.0 more slots than a typical manager in this league managed.",
        "",
        "  hit       a pick whose player finished better than he was drafted",
        f"  starter   finished inside a startable range: {thresholds}",
        "  bust      only counted against picks taken INSIDE that startable range,",
        "            so a late-round flier who never played does not qualify",
        "  pts/pick  total fantasy points those players scored, per pick made -",
        "            raw production, ignoring where they were drafted",
        "",
        f"  LEAGUE BASELINE  value {league['mean_delta']:+.1f} slots  ·  "
        f"{league['hit_rate']:.0%} hits  ·  {league['starter_rate']:.0%} starters  ·  "
        f"{league['pts_per_pick']:.0f} pts/pick  (n={league['n']})",
    ]


def _describe_pick(entry: dict | None) -> str:
    if not entry:
        return "–"
    return (
        f"{entry['player']} ({entry['season']} R{entry['round']}) "
        f"taken {entry['position']}{entry['taken']}, "
        f"finished {entry['position']}{entry['finish']} "
        f"({entry['delta']:+d}, {entry['points']:.0f} pts)"
    )


def _value_block(val: dict, league: dict) -> list[str]:
    """Per-manager draft results, each number stated against the league bar."""
    def vs(amount, fmt="{:+.1f}"):
        return fmt.format(amount)

    lines = [
        f"  DRAFT RESULTS  (n={val['n']} picks with known outcomes)",
        f"    value      {vs(val['vs_league'])} slots vs league   "
        f"(their {val['mean_delta']:+.1f} against the league's "
        f"{league.get('mean_delta', 0):+.1f})",
        f"    hits       {val['hit_rate']:.0%} of picks beat their draft slot   "
        f"(league {league.get('hit_rate', 0):.0%}, "
        f"{vs(val['hit_rate_vs_league'] * 100, '{:+.0f}')} pts)",
        f"    starters   {val['starter_rate']:.0%} became startable   "
        f"(league {league.get('starter_rate', 0):.0%}, "
        f"{vs(val['starter_rate_vs_league'] * 100, '{:+.0f}')} pts)",
        f"    production {val['pts_per_pick']:.0f} pts per pick   "
        f"(league {league.get('pts_per_pick', 0):.0f}, "
        f"{vs(val['pts_per_pick_vs_league'])})",
    ]

    by_season = val.get("by_season") or {}
    if by_season:
        parts = [
            f"{season} {v['pts_per_pick']:.0f}/pick, {v['starters']}/{v['picks']} start"
            for season, v in by_season.items()
        ]
        lines.append("    by season  " + "   ".join(parts))

    lines.append(f"    best steal {_describe_pick(val.get('best'))}")
    lines.append(f"    worst bust {_describe_pick(val.get('worst'))}")

    # vs-league, so it reads on the same scale as every other line in the block.
    strong = sorted(
        (val.get("by_position_vs_league") or {}).items(),
        key=lambda kv: kv[1], reverse=True,
    )
    if strong:
        lines.append(
            "    by position " + "  ".join(f"{p} {v:+.0f}" for p, v in strong)
            + "   (vs league, per position)"
        )
    return lines


def main():
    parser = argparse.ArgumentParser(description="League tendency report")
    parser.add_argument(
        "--json", action="store_true", help="emit raw model output instead of text"
    )
    args = parser.parse_args()

    db.init_db()
    with db.session() as conn:
        if args.json:
            import json

            print(json.dumps({
                "summary": history_summary(conn),
                "owners": owners(conn),
                "position_share": position_share(conn),
                "first_position_round": first_position_round(conn),
                "run_following": run_following(conn),
                "draft_value": draft_value(conn),
            }, indent=2, default=str))
        else:
            print(scouting_report(conn))


if __name__ == "__main__":
    main()
