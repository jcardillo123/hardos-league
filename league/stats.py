"""Derived metrics: optimal lineups, all-play records, luck.

Pure functions over rows, so they can be unit-checked without a database and
reused by both ingest and the API.
"""

from .config import FLEX_POSITIONS, FLEX_SLOTS, IR_SLOT, LINEUP


def optimal_lineup(players, lineup=None, flex_slots=FLEX_SLOTS,
                   flex_positions=FLEX_POSITIONS):
    """The highest-scoring legal lineup available from one week's roster.

    `players` is an iterable of dicts with `position`, `points` and
    `lineup_slot`. Returns (total, chosen) where chosen is the list of players
    in the optimal lineup.

    Greedy is provably optimal for this slot structure. The dedicated slots take
    a fixed count at a named position and the FLEX slots accept any of
    `flex_positions`, so the constraint is "at least 2 RB, at least 2 WR, at
    least 1 TE, plus 2 more from their union". Filling each named slot with that
    position's best and then the flex slots with the best of whatever remains
    cannot be beaten: swapping any chosen player for an unchosen one of the same
    eligibility trades down by construction, and a flex pick is eligible
    everywhere a leftover is.

    Players on IR are excluded. ESPN will not let an IR player into a lineup, so
    counting his points as "available" would invent a decision the manager never
    had.
    """
    lineup = lineup or LINEUP
    pool = [p for p in players if p.get("lineup_slot") != IR_SLOT]
    by_pos = {}
    for p in pool:
        by_pos.setdefault(p.get("position"), []).append(p)
    for v in by_pos.values():
        v.sort(key=lambda p: p.get("points") or 0.0, reverse=True)

    chosen, used = [], set()
    for pos, count in lineup.items():
        for p in by_pos.get(pos, [])[:count]:
            chosen.append(p)
            used.add(id(p))

    flex_pool = sorted(
        (p for pos in flex_positions for p in by_pos.get(pos, [])
         if id(p) not in used),
        key=lambda p: p.get("points") or 0.0, reverse=True,
    )
    chosen.extend(flex_pool[:flex_slots])

    return round(sum(p.get("points") or 0.0 for p in chosen), 2), chosen


def started_total(players):
    """What the lineup the manager actually set scored."""
    return round(
        sum(p.get("points") or 0.0 for p in players if p.get("started")), 2
    )


def bench_total(players):
    """Points that sat on the bench, excluding IR.

    Not the same as `optimal - actual`: most bench points were never startable,
    because only so many lineup slots exist. This is the raw total; the gap that
    actually cost games is `optimal - actual`.
    """
    return round(
        sum(p.get("points") or 0.0 for p in players
            if not p.get("started") and p.get("lineup_slot") != IR_SLOT), 2
    )


def all_play(score, other_scores):
    """Record against every other score in the same week.

    The luck-free strength measure: a team's schedule decides who it played, but
    not how it would have done against the whole league.
    """
    w = sum(1 for s in other_scores if score > s)
    l = sum(1 for s in other_scores if score < s)
    t = sum(1 for s in other_scores if score == s)
    return w, l, t


def expected_wins(all_play_w, all_play_l, all_play_t):
    """Wins a team "should" have had, from its all-play record.

    A team that beat 8 of 11 opponents' scores in a week won 8/11 of a win that
    week regardless of whom it was scheduled against. Summed over a season this
    is what actual wins gets compared to.
    """
    games = all_play_w + all_play_l + all_play_t
    if not games:
        return 0.0
    return (all_play_w + 0.5 * all_play_t) / games
