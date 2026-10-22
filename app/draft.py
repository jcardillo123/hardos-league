"""Draft state: pick order, roster tracking, and who is on the clock."""

from . import db
from .config import FLEX_POSITIONS


def pick_order(league: dict) -> list[dict]:
    """Every pick in the draft, in order, as {overall, round, slot}."""
    teams = int(league.get("n_teams", 12))
    rounds = int(league.get("rounds", 16))
    snake = league.get("snake_type", "snake") == "snake"
    order = []
    overall = 0
    for rnd in range(1, rounds + 1):
        slots = list(range(1, teams + 1))
        if snake and rnd % 2 == 0:
            slots.reverse()
        for slot in slots:
            overall += 1
            order.append({"overall": overall, "round": rnd, "slot": slot})
    return order


def managers(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, draft_slot, is_me, owner_guid FROM managers "
        "ORDER BY draft_slot"
    ).fetchall()
    return [dict(r) for r in rows]


def picks(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.overall, p.round, p.slot, p.manager_id, p.player_key,
               m.name AS manager_name, m.is_me,
               pl.name, pl.position, pl.team, pl.ecr, pl.adp, pl.vor, pl.bye
        FROM picks p
        JOIN managers m ON m.id = p.manager_id
        JOIN players pl ON pl.player_key = p.player_key
        ORDER BY p.overall
        """
    ).fetchall()
    return [dict(r) for r in rows]


def current_pick(conn, league: dict) -> dict | None:
    """The next pick to be made, or None once the draft is complete."""
    made = conn.execute("SELECT COUNT(*) AS n FROM picks").fetchone()["n"]
    order = pick_order(league)
    if made >= len(order):
        return None
    slot_to_manager = {m["draft_slot"]: m for m in managers(conn)}
    entry = dict(order[made])
    manager = slot_to_manager.get(entry["slot"])
    entry["manager_id"] = manager["id"] if manager else None
    entry["manager_name"] = manager["name"] if manager else f"Slot {entry['slot']}"
    entry["is_me"] = manager["is_me"] if manager else 0
    return entry


def resolve_me(conn, league: dict) -> int | None:
    """Move `is_me` to whichever seat my owner currently occupies.

    `managers.is_me` and `league["my_slot"]` both describe a seat. Reordering the
    draft reassigns owners to seats, so a seat-based marker silently makes you
    whoever landed in your old chair - which is how "me" drifted from Jake Cardillo
    to Will Finke to Nicky Yarnall across a few reorderings.

    Identity lives in `league["my_owner_guid"]`; the seat is derived from it here,
    and `my_slot` is kept in step. Returns the resulting draft slot.

    Falls back to the existing seat when there is no identity to resolve (a league
    with no imported history), or when my owner is not seated at all - better to
    leave the board working than to blank out who is drafting.
    """
    guid = league.get("my_owner_guid")
    if guid:
        row = conn.execute(
            "SELECT id, draft_slot FROM managers WHERE owner_guid = ?", (guid,)
        ).fetchone()
        if row:
            conn.execute("UPDATE managers SET is_me = 0")
            conn.execute("UPDATE managers SET is_me = 1 WHERE id = ?", (row["id"],))
            if league.get("my_slot") != row["draft_slot"]:
                league["my_slot"] = row["draft_slot"]
                db.set_league(conn, league)
            return row["draft_slot"]

    me = conn.execute(
        "SELECT draft_slot FROM managers WHERE is_me = 1"
    ).fetchone()
    if me and league.get("my_slot") != me["draft_slot"]:
        league["my_slot"] = me["draft_slot"]
        db.set_league(conn, league)
    return me["draft_slot"] if me else None


def claim_identity(conn, league: dict, manager_id: int) -> dict:
    """Declare a seat as mine, and remember the *owner* sitting in it."""
    row = conn.execute(
        "SELECT id, draft_slot, owner_guid FROM managers WHERE id = ?", (manager_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"no manager with id {manager_id}")
    conn.execute("UPDATE managers SET is_me = 0")
    conn.execute("UPDATE managers SET is_me = 1 WHERE id = ?", (row["id"],))
    league["my_slot"] = row["draft_slot"]
    league["my_owner_guid"] = row["owner_guid"]
    db.set_league(conn, league)
    return {"draft_slot": row["draft_slot"], "owner_guid": row["owner_guid"]}


def my_manager(conn) -> dict | None:
    row = conn.execute(
        "SELECT id, name, draft_slot FROM managers WHERE is_me = 1"
    ).fetchone()
    return dict(row) if row else None


def picks_until_my_turn(conn, league: dict) -> dict:
    """How long until I pick again, and when my following pick lands.

    This is the number that actually drives draft decisions: it tells you how
    many players can come off the board before your next turn.
    """
    me = my_manager(conn)
    if not me:
        return {"until_next": None, "my_next_overall": None, "my_following_overall": None}
    made = conn.execute("SELECT COUNT(*) AS n FROM picks").fetchone()["n"]
    mine = [p["overall"] for p in pick_order(league) if p["slot"] == me["draft_slot"]]
    upcoming = [o for o in mine if o > made]
    return {
        "until_next": (upcoming[0] - made - 1) if upcoming else None,
        "my_next_overall": upcoming[0] if upcoming else None,
        "my_following_overall": upcoming[1] if len(upcoming) > 1 else None,
    }


def make_pick(conn, player_key: str, league: dict, manager_id: int | None = None) -> dict:
    """Record the next pick. Defaults to whoever is on the clock."""
    entry = current_pick(conn, league)
    if entry is None:
        raise ValueError("Draft is already complete")

    exists = conn.execute(
        "SELECT name FROM players WHERE player_key = ?", (player_key,)
    ).fetchone()
    if not exists:
        raise ValueError(f"Unknown player: {player_key}")
    taken = conn.execute(
        "SELECT overall FROM picks WHERE player_key = ?", (player_key,)
    ).fetchone()
    if taken:
        raise ValueError(f"{exists['name']} was already drafted at pick {taken['overall']}")

    target = manager_id if manager_id is not None else entry["manager_id"]
    conn.execute(
        "INSERT INTO picks (overall, round, slot, manager_id, player_key) "
        "VALUES (?, ?, ?, ?, ?)",
        (entry["overall"], entry["round"], entry["slot"], target, player_key),
    )
    return {"overall": entry["overall"], "player": exists["name"], "manager_id": target}


def undo_pick(conn) -> dict | None:
    row = conn.execute(
        "SELECT overall, player_key FROM picks ORDER BY overall DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM picks WHERE overall = ?", (row["overall"],))
    return dict(row)


def reset_draft(conn) -> int:
    n = conn.execute("SELECT COUNT(*) AS n FROM picks").fetchone()["n"]
    conn.execute("DELETE FROM picks")
    return n


def roster(conn, manager_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.overall, p.round, pl.name, pl.position, pl.team, pl.bye,
               pl.player_key, pl.proj_points, pl.vor
        FROM picks p JOIN players pl ON pl.player_key = p.player_key
        WHERE p.manager_id = ?
        ORDER BY p.overall
        """,
        (manager_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def roster_needs(conn, manager_id: int, league: dict) -> dict:
    """Starter slots still open for a manager, by position.

    FLEX is reported separately: it is only "needed" once the dedicated RB/WR/TE
    slots are full, which is what makes it a flex.
    """
    slots = dict(league.get("roster_slots", {}))
    counts: dict[str, int] = {}
    for player in roster(conn, manager_id):
        pos = player["position"]
        counts[pos] = counts.get(pos, 0) + 1

    needs = {}
    leftover = dict(counts)
    for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
        required = int(slots.get(pos, 0))
        filled = min(leftover.get(pos, 0), required)
        leftover[pos] = leftover.get(pos, 0) - filled
        needs[pos] = required - filled

    flex_required = int(slots.get("FLEX", 0))
    flex_filled = min(sum(leftover.get(p, 0) for p in FLEX_POSITIONS), flex_required)
    needs["FLEX"] = flex_required - flex_filled

    bench_required = int(slots.get("BN", 0))
    starters = sum(int(slots.get(k, 0)) for k in ("QB", "RB", "WR", "TE", "K", "DST"))
    starters += flex_required
    drafted = sum(counts.values())
    needs["BN"] = max(0, bench_required - max(0, drafted - starters))
    needs["_counts"] = counts
    needs["_drafted"] = drafted
    return needs


def available(
    conn,
    position: str | None = None,
    query: str | None = None,
    sort: str = "ecr",
    limit: int = 300,
) -> list[dict]:
    """Undrafted players, ordered by the chosen ranking."""
    sort_sql = {
        "ecr": "CASE WHEN pl.ecr IS NULL THEN 1 ELSE 0 END, pl.ecr ASC",
        "adp": "CASE WHEN pl.adp IS NULL THEN 1 ELSE 0 END, pl.adp ASC",
        "espn_adp": (
            "CASE WHEN pl.espn_adp IS NULL THEN 1 ELSE 0 END, pl.espn_adp ASC"
        ),
        "vor": "CASE WHEN pl.vor IS NULL THEN 1 ELSE 0 END, pl.vor DESC",
        "proj": "CASE WHEN pl.proj_points IS NULL THEN 1 ELSE 0 END, pl.proj_points DESC",
        "last": "CASE WHEN pl.pts_last IS NULL THEN 1 ELSE 0 END, pl.pts_last DESC",
        "vegas_vor": (
            "CASE WHEN pl.vegas_vor IS NULL THEN 1 ELSE 0 END, pl.vegas_vor DESC"
        ),
        "vegas_proj": (
            "CASE WHEN pl.vegas_proj IS NULL THEN 1 ELSE 0 END, pl.vegas_proj DESC"
        ),
        # Where the two projection sources disagree, biggest Vegas-likes-him-more
        # first. This is the view that finds targets: a player the market ranks
        # off his consensus projection while the odds imply he is worth more.
        #
        # Two exclusions, both because the difference stops meaning anything
        # rather than to flatter the list:
        #
        #  - Below replacement in BOTH sources. The rankings projections floor
        #    deep bench players near zero while the Vegas model still gives them
        #    a share of a real team total, so their gap is a property of the two
        #    methods, not of the player. Sorted raw, third-string running backs
        #    bury every name you would actually draft.
        #  - K and DST. The rescale that puts the two VORs in the same units is
        #    fitted on QB/RB/WR/TE only; for kickers the sources differ by ~30%
        #    on scoring alone, so their "disagreement" is mostly that.
        #
        # Both still show their Edge value in the column - only the ordering
        # treats them as unrankable.
        "edge": (
            "CASE WHEN pl.vegas_vor IS NULL OR pl.vor IS NULL "
            "OR pl.position IN ('K', 'DST') "
            "OR (pl.vegas_vor <= 0 AND pl.vor <= 0) THEN 1 ELSE 0 END, "
            "(pl.vegas_vor - pl.vor) DESC"
        ),
    }.get(sort, "pl.ecr ASC")

    sql = [
        """
        SELECT pl.* FROM players pl
        WHERE NOT EXISTS (SELECT 1 FROM picks k WHERE k.player_key = pl.player_key)
        """
    ]
    params: list = []
    if position and position != "ALL":
        placeholders = ",".join("?" * len(position.split(",")))
        sql.append(f"AND pl.position IN ({placeholders})")
        params.extend(position.split(","))
    if query:
        sql.append("AND pl.name LIKE ?")
        params.append(f"%{query}%")
    sql.append(f"ORDER BY {sort_sql} LIMIT ?")
    params.append(int(limit))

    rows = conn.execute(" ".join(sql), params).fetchall()
    return [dict(r) for r in rows]


def board_grid(conn, league: dict) -> list[list[dict | None]]:
    """Rounds x draft slots, the classic wall-chart view of what has happened."""
    teams = int(league.get("n_teams", 12))
    rounds = int(league.get("rounds", 16))
    by_overall = {p["overall"]: p for p in picks(conn)}
    grid: list[list[dict | None]] = []
    for entry in pick_order(league):
        rnd, slot = entry["round"], entry["slot"]
        while len(grid) < rnd:
            grid.append([None] * teams)
        grid[rnd - 1][slot - 1] = by_overall.get(entry["overall"])
    return grid[:rounds]


def state(conn) -> dict:
    league = db.get_league(conn)
    me = my_manager(conn)
    made = picks(conn)
    return {
        "league": league,
        "managers": managers(conn),
        "me": me,
        "picks": made,
        "n_picks": len(made),
        "total_picks": len(pick_order(league)),
        "current": current_pick(conn, league),
        "timing": picks_until_my_turn(conn, league),
        "my_roster": roster(conn, me["id"]) if me else [],
        "my_needs": roster_needs(conn, me["id"], league) if me else {},
        "pool_size": conn.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"],
    }
