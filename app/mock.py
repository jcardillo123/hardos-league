"""Mock mode: your leaguemates draft for themselves.

Opponents' picks are chosen by the same model the Next picks panel projects with,
so a mock draft is a run through the actual predictions rather than a separate
toy. Every input the projection uses is in play:

* **their tendencies** - the shrunk position-share rates for that round's phase
* **their roster needs** - a filled starter slot is downweighted
* **what is on the board** - a position with nobody left cannot be picked, and a
  bargain sitting there raises that position's odds
* **their reach habit** - a manager who historically jumps the market has a
  flatter preference within a position, so they are likelier to skip the
  consensus best player and take *their* guy; a manager who waits takes the top
  of the board

Bot picks land in the same `picks` table as manual ones, so the board, grid,
roster panel, projections and suggestion box all work unchanged.
"""

import math
import random

from . import db, draft, tendencies

# Bounds on how much a reach habit reshapes within-position preference. A reacher
# gets a flatter curve (more likely to take a non-obvious player at the position);
# a waiter gets a sharper one (takes the best available).
REACH_DECAY_RANGE = (0.7, 1.6)
REACH_DECAY_SCALE = 24.0
# How deep into a position's board a bot will reach for a player. Scales with the
# round: early on, managers take one of the top few at a position, and the pool they
# will consider widens as the draft wears on. A fixed depth of 14 let a bot take the
# 10th-ranked tight end as the second TE off the board in round 2 - a tail event of
# the rank-decay curve, but one no human would produce.
BOT_POSITION_DEPTH = 14
BOT_DEPTH_BASE = 2


def position_depth(round_number: int) -> int:
    return max(2, min(BOT_POSITION_DEPTH, BOT_DEPTH_BASE + int(round_number)))


def _decay_for(profile: dict | None) -> float:
    """Within-position preference sharpness for one manager."""
    if not profile or not profile.get("n"):
        return tendencies.RANK_DECAY
    low, high = REACH_DECAY_RANGE
    # Median, not mean: reach has heavy tails and the mean flips sign on several
    # managers in this league.
    scale = 1.0 + (profile["median_reach"] / REACH_DECAY_SCALE)
    return tendencies.RANK_DECAY * min(max(scale, low), high)


def _weighted_choice(items, weights, rng):
    total = sum(weights)
    if total <= 0:
        return items[0]
    target = rng.random() * total
    running = 0.0
    for item, weight in zip(items, weights):
        running += weight
        if running >= target:
            return item
    return items[-1]


def bot_pick(conn, entry, league, shares, base, reach, signal, rng) -> dict | None:
    """Choose a player for one simulated pick, or None if the board is empty."""
    slot_to_manager = {m["draft_slot"]: m for m in draft.managers(conn)}
    manager = slot_to_manager.get(entry["slot"])
    if not manager:
        return None

    ranked, sample_n, phase, _meta = tendencies._ranked_distribution(
        conn, entry, manager, shares, base, league, signal
    )
    candidates = [(pos, p) for pos, p in ranked if p > 0]
    if not candidates:
        return None

    profile = reach.get(manager.get("owner_guid"))
    decay = _decay_for(profile)
    depth = position_depth(entry["round"])
    weights = [math.exp(-i / decay) for i in range(depth)]
    field = tendencies._signal_field(signal)

    # Try positions in sampled order; a position can be empty even with non-zero
    # probability if the pool ran dry, so fall through rather than wasting a pick.
    remaining = list(candidates)
    for _ in range(len(remaining)):
        position = _weighted_choice(
            [p for p, _ in remaining], [w for _, w in remaining], rng
        )
        remaining = [(p, w) for p, w in remaining if p != position]
        players = draft.available(conn, position, None, field, depth)
        if not players:
            if not remaining:
                break
            continue
        # Board position AND distance from this player's own ranking. Without the
        # second term, being "best left at the position" is enough to justify any
        # pick, however far ahead of his ADP.
        player_weights = [
            weight * tendencies.player_reach_weight(
                entry["overall"], player.get(field), entry["round"]
            )
            for weight, player in zip(weights, players)
        ]
        chosen = _weighted_choice(players, player_weights, rng)
        return {
            "player": chosen,
            "manager": manager,
            "position": position,
            "phase": phase,
            "sample_size": sample_n,
            "reach_median": (profile or {}).get("median_reach"),
            "decay": round(decay, 2),
        }

    # Nothing at any projected position: take the best player left anywhere.
    fallback = draft.available(conn, None, None, field, 1)
    if not fallback:
        return None
    return {
        "player": fallback[0],
        "manager": manager,
        "position": fallback[0]["position"],
        "phase": phase,
        "sample_size": sample_n,
        "reach_median": (profile or {}).get("median_reach"),
        "decay": round(decay, 2),
        "note": "no player left at any projected position",
    }


def advance(
    conn,
    max_picks: int = 400,
    stop_at_my_turn: bool = True,
    signal: str | None = None,
    seed: int | None = None,
) -> dict:
    """Simulate opponents' picks until it is my turn (or the draft ends).

    Returns what was drafted, so the UI can narrate the run rather than just
    showing a changed board.
    """
    league = db.get_league(conn)
    signal = signal or league.get("mock_signal", "ecr")
    if signal not in tendencies.AVAILABILITY_SIGNALS:
        signal = "ecr"

    rng = random.Random(seed)
    shares = tendencies.position_share(conn)
    base = shares["_league"]
    reach = tendencies.reach_profile(conn)

    made = []
    for _ in range(max_picks):
        entry = draft.current_pick(conn, league)
        if entry is None:
            return {"picks": made, "note": "draft complete", "signal": signal}
        if stop_at_my_turn and entry.get("is_me"):
            return {"picks": made, "note": "your turn", "signal": signal}

        choice = bot_pick(conn, entry, league, shares, base, reach, signal, rng)
        if choice is None:
            return {"picks": made, "note": "no players left", "signal": signal}

        draft.make_pick(conn, choice["player"]["player_key"], league)
        made.append({
            "overall": entry["overall"],
            "round": entry["round"],
            "slot": entry["slot"],
            "manager_name": choice["manager"]["name"],
            "name": choice["player"]["name"],
            "position": choice["position"],
            "team": choice["player"]["team"],
            "ecr": choice["player"]["ecr"],
            "phase": choice["phase"],
            "sample_size": choice["sample_size"],
            "reach_median": choice["reach_median"],
            "decay": choice["decay"],
            "note": choice.get("note"),
        })

    return {"picks": made, "note": f"stopped after {max_picks} picks",
            "signal": signal}


def start(
    conn,
    my_slot: int,
    signal: str = "ecr",
    seed: int | None = None,
    advance_to_me: bool = True,
) -> dict:
    """Begin a mock draft from a chosen seat. Clears the board first."""
    league = db.get_league(conn)
    teams = int(league.get("n_teams", 12))
    if not 1 <= int(my_slot) <= teams:
        raise ValueError(f"slot must be between 1 and {teams}")
    if signal not in tendencies.AVAILABILITY_SIGNALS:
        signal = "ecr"

    cleared = draft.reset_draft(conn)

    row = conn.execute(
        "SELECT id FROM managers WHERE draft_slot = ?", (int(my_slot),)
    ).fetchone()
    if not row:
        raise ValueError(f"no manager in slot {my_slot}")

    league["mock_mode"] = True
    league["mock_signal"] = signal
    db.set_league(conn, league)
    # Taking a seat for a mock is a deliberate claim, so it sets identity too -
    # otherwise a later reorder would yank you back to your real seat mid-mock.
    draft.claim_identity(conn, league, row["id"])

    run = advance(conn, signal=signal, seed=seed) if advance_to_me else {"picks": []}
    return {
        "cleared": cleared,
        "my_slot": int(my_slot),
        "signal": signal,
        "advanced": run.get("picks", []),
        "note": run.get("note"),
    }


def stop(conn) -> dict:
    """Leave mock mode. Picks are left alone - reset separately if you want."""
    league = db.get_league(conn)
    league["mock_mode"] = False
    db.set_league(conn, league)
    return {"mock_mode": False}
