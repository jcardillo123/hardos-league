"""Local web server for the draft board.

    python -m uvicorn app.main:app --reload --port 8000

Then open http://127.0.0.1:8000. Nothing leaves the machine except the initial
nflverse downloads, so this works offline once the pool is built.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, draft, ingest, mock, tendencies
from .config import STATIC


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Draft Tool", lifespan=lifespan)


@app.middleware("http")
async def no_cache_ui(request, call_next):
    """Never let the browser cache the UI.

    This is a local single-user tool that gets edited between refreshes; a stale
    cached index.html silently serving an old board is far worse than re-reading
    a few KB from disk.
    """
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# --------------------------------------------------------------------------- #
# request bodies
# --------------------------------------------------------------------------- #

class PickBody(BaseModel):
    player_key: str
    manager_id: int | None = None


class LeagueBody(BaseModel):
    n_teams: int | None = None
    rounds: int | None = None
    my_slot: int | None = None
    scoring: str | None = None
    snake_type: str | None = None
    roster_slots: dict[str, int] | None = None


class ManagerBody(BaseModel):
    id: int
    name: str | None = None
    is_me: bool | None = None
    # Explicit sentinel handling: omitted means "leave alone", empty string means
    # "unassign". A bare None cannot distinguish those two.
    owner_guid: str | None = None


class ManagersBody(BaseModel):
    managers: list[ManagerBody]


class IngestBody(BaseModel):
    refresh: bool = False


class MockStartBody(BaseModel):
    my_slot: int
    signal: str = "ecr"
    seed: int | None = None


class MockAdvanceBody(BaseModel):
    signal: str | None = None
    seed: int | None = None
    # Cap on how many opponent picks to simulate. Advancing always stops at your
    # turn regardless, so this only ever shortens the run.
    picks: int | None = None


class HistoryImportBody(BaseModel):
    csv_path: str
    refresh: bool = False


class LinkBody(BaseModel):
    owner_guid: str
    manager_id: int | None = None   # None clears the link


# --------------------------------------------------------------------------- #
# api
# --------------------------------------------------------------------------- #

@app.get("/api/state")
def get_state():
    with db.session() as conn:
        return draft.state(conn)


@app.get("/api/players")
def get_players(
    position: str | None = None,
    q: str | None = None,
    sort: str = "ecr",
    limit: int = 300,
):
    with db.session() as conn:
        return {"players": draft.available(conn, position, q, sort, limit)}


@app.get("/api/board")
def get_board(sort: str = "ecr", per_column: int = 60):
    """One ranked column of available players per position group."""
    from .config import BOARD_COLUMNS

    with db.session() as conn:
        columns = []
        for label, positions in BOARD_COLUMNS:
            columns.append({
                "label": label,
                "positions": list(positions),
                "players": draft.available(
                    conn, ",".join(positions), None, sort, per_column
                ),
            })
        return {"columns": columns}


@app.get("/api/grid")
def get_grid():
    with db.session() as conn:
        league = db.get_league(conn)
        return {
            "league": league,
            "managers": draft.managers(conn),
            "grid": draft.board_grid(conn, league),
        }


@app.post("/api/pick")
def post_pick(body: PickBody):
    with db.session() as conn:
        league = db.get_league(conn)
        # In mock mode the opponents pick for themselves, so a manual pick can only
        # ever be yours. Without this guard a stray click while a bot is on the
        # clock would silently be recorded as that opponent's selection.
        if league.get("mock_mode") and body.manager_id is None:
            current = draft.current_pick(conn, league)
            if current and not current.get("is_me"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Mock mode: it is {current['manager_name']}'s pick. "
                           "Simulate their pick instead of entering it.",
                )
        try:
            result = draft.make_pick(conn, body.player_key, league, body.manager_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Roll the opponents forward so control comes straight back to you.
        advanced = []
        if league.get("mock_mode"):
            run = mock.advance(conn, signal=league.get("mock_signal"))
            advanced = run.get("picks", [])
        return {
            "ok": True, **result, "advanced": advanced,
            "state": draft.state(conn),
        }


@app.post("/api/undo")
def post_undo():
    with db.session() as conn:
        removed = draft.undo_pick(conn)
        if removed is None:
            raise HTTPException(status_code=400, detail="No picks to undo")
        return {"ok": True, "removed": removed, "state": draft.state(conn)}


@app.post("/api/reset")
def post_reset():
    with db.session() as conn:
        n = draft.reset_draft(conn)
        return {"ok": True, "cleared": n, "state": draft.state(conn)}


@app.post("/api/league")
def post_league(body: LeagueBody):
    with db.session() as conn:
        league = db.get_league(conn)
        changes = body.model_dump(exclude_none=True)
        resize = (
            "n_teams" in changes and changes["n_teams"] != league["n_teams"]
        ) or ("my_slot" in changes and changes["my_slot"] != league["my_slot"])

        made = conn.execute("SELECT COUNT(*) AS n FROM picks").fetchone()["n"]
        if resize and made:
            raise HTTPException(
                status_code=400,
                detail="Cannot resize the league after picks are made. Reset first.",
            )
        league.update(changes)
        if resize:
            # Reseeding replaces every manager with a blank "Team N" and drops the
            # owner links, so a remembered owner GUID would dangle. Identity resets
            # to the chosen seat until owners are linked again.
            league["my_owner_guid"] = None
            db.set_league(conn, league)
            db.reseed_managers(conn, league["n_teams"], league["my_slot"])
        else:
            db.set_league(conn, league)
            draft.resolve_me(conn, league)
        return {"ok": True, "league": league, "state": draft.state(conn)}


@app.post("/api/managers")
def post_managers(body: ManagersBody):
    with db.session() as conn:
        # Owner assignment first, in two phases: clear every seat being reassigned
        # before setting any, or assigning owner A to a seat that already holds B
        # can collide with a seat still holding A.
        assigning = [m for m in body.managers if m.owner_guid is not None]
        for manager in assigning:
            conn.execute(
                "UPDATE managers SET owner_guid = NULL WHERE id = ?", (manager.id,)
            )
        for manager in assigning:
            guid = manager.owner_guid.strip()
            if not guid:
                continue
            label = conn.execute(
                "SELECT owner_label FROM history WHERE owner_guid = ? "
                "ORDER BY season DESC LIMIT 1",
                (guid,),
            ).fetchone()
            if not label:
                raise HTTPException(
                    status_code=400, detail=f"Unknown owner_guid: {guid}"
                )
            # One seat per owner.
            conn.execute(
                "UPDATE managers SET owner_guid = NULL WHERE owner_guid = ?", (guid,)
            )
            conn.execute(
                "UPDATE managers SET owner_guid = ?, name = ? WHERE id = ?",
                (guid, label["owner_label"], manager.id),
            )

        for manager in body.managers:
            # A typed name still wins, so leagues with no imported history work.
            if manager.name is not None and manager.owner_guid is None:
                conn.execute(
                    "UPDATE managers SET name = ? WHERE id = ?",
                    (manager.name.strip() or f"Team {manager.id}", manager.id),
                )

        league = db.get_league(conn)
        claimed = next((m for m in body.managers if m.is_me), None)
        if claimed:
            # Ticking the radio declares an identity, not just a chair.
            draft.claim_identity(conn, league, claimed.id)
        else:
            # No explicit claim: re-derive my seat from who I am, so reordering the
            # draft moves me with my owner instead of leaving me in the old chair.
            draft.resolve_me(conn, league)
        return {"ok": True, "state": draft.state(conn)}


@app.post("/api/ingest")
def post_ingest(body: IngestBody):
    """Rebuild the player pool. Clears picks, so it is a pre-draft action."""
    try:
        summary = ingest.build_pool(refresh=body.refresh)
    except Exception as exc:                                   # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    with db.session() as conn:
        return {"ok": True, "summary": summary, "state": draft.state(conn)}


# --------------------------------------------------------------------------- #
# mock draft
# --------------------------------------------------------------------------- #

@app.post("/api/mock/start")
def post_mock_start(body: MockStartBody):
    """Clear the board, take a seat, and let the opponents draft to your turn."""
    _check_signal(body.signal)
    with db.session() as conn:
        try:
            summary = mock.start(conn, body.my_slot, body.signal, body.seed)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "summary": summary, "state": draft.state(conn)}


@app.post("/api/mock/advance")
def post_mock_advance(body: MockAdvanceBody):
    """Simulate opponents' picks — to your next turn, or exactly `picks` of them."""
    _check_signal(body.signal)
    with db.session() as conn:
        league = db.get_league(conn)
        if not league.get("mock_mode"):
            raise HTTPException(
                status_code=400,
                detail="Mock mode is off. Start a mock draft first.",
            )
        # `picks` caps how far to run; it never licenses drafting for the user.
        # Stopping at your turn is unconditional - a simulator that quietly makes
        # your pick for you is worse than useless.
        run = mock.advance(
            conn,
            max_picks=body.picks if body.picks else 400,
            stop_at_my_turn=True,
            signal=body.signal,
            seed=body.seed,
        )
        return {"ok": True, **run, "state": draft.state(conn)}


@app.post("/api/mock/stop")
def post_mock_stop():
    with db.session() as conn:
        result = mock.stop(conn)
        return {"ok": True, **result, "state": draft.state(conn)}


# --------------------------------------------------------------------------- #
# league history / tendencies
# --------------------------------------------------------------------------- #

@app.get("/api/history")
def get_history():
    """Summary of what history is loaded and which owners map to which seats."""
    with db.session() as conn:
        return {
            "summary": tendencies.history_summary(conn),
            "owners": tendencies.owners(conn),
            "managers": draft.managers(conn),
        }


@app.get("/api/tendencies")
def get_tendencies():
    with db.session() as conn:
        if not tendencies.history_summary(conn)["picks"]:
            raise HTTPException(
                status_code=404,
                detail="No league history imported. Run DraftTool.py, then "
                       "POST /api/history/import.",
            )
        return {
            "position_share": tendencies.position_share(conn),
            "first_position_round": tendencies.first_position_round(conn),
            "run_following": tendencies.run_following(conn),
            "draft_value": tendencies.draft_value(conn),
            "reach_profile": tendencies.reach_profile(conn),
            "phases": [
                {"name": n, "from": lo, "to": hi} for n, lo, hi in tendencies.PHASES
            ],
        }


@app.get("/api/tendencies/report", response_class=PlainTextResponse)
def get_tendencies_report():
    with db.session() as conn:
        return tendencies.scouting_report(conn)


def _check_signal(*signals: str | None) -> None:
    for signal in signals:
        if signal is not None and signal not in tendencies.AVAILABILITY_SIGNALS:
            raise HTTPException(
                status_code=400,
                detail=f"signal must be one of "
                       f"{list(tendencies.AVAILABILITY_SIGNALS)}",
            )


@app.get("/api/projection")
def get_projection(
    limit: int | None = None,
    signal: str = "ecr",
    compare: bool = False,
    compare_with: str | None = None,
):
    """Position probabilities for every pick between now and my next turn.

    `signal` picks the ranking that drives board availability: `ecr` (expert
    consensus), `adp` (consensus draft position from the rankings files), or
    `espn_adp` (where players go in ESPN leagues). `compare` adds a second
    signal's probability to every position; `compare_with` names it.
    """
    _check_signal(signal, compare_with)
    with db.session() as conn:
        return tendencies.project_upcoming(
            conn, limit, signal, compare, compare_with
        )


@app.get("/api/suggest")
def get_suggest(
    limit: int = 6,
    signal: str = "ecr",
    trials: int = tendencies.SIM_TRIALS,
    scarcity: float = tendencies.SCARCITY_WEIGHT,
    value: str = tendencies.DEFAULT_VALUE_SOURCE,
):
    """Recommend a pick by simulating the draft forward to your turn and past it.

    `scarcity` weights the market drop-off term against projected value. Lower it
    below 1.0 to let this season's projections dominate.

    `value` picks which projection source supplies that value: `rankings` (the
    RotoBaller/FantasyPros export), `vegas` (winwithodds season totals implied by
    the betting market), or `blend` for an even average of the two.
    """
    _check_signal(signal)
    if value not in tendencies.VALUE_SOURCES:
        raise HTTPException(
            400,
            f"unknown value source {value!r}; expected one of "
            f"{', '.join(sorted(tendencies.VALUE_SOURCES))}",
        )
    trials = max(20, min(int(trials), 2000))
    scarcity = max(0.0, min(float(scarcity), 5.0))
    with db.session() as conn:
        return tendencies.suggest_pick(
            conn, trials, limit, signal, scarcity_weight=scarcity,
            value_source=value,
        )


@app.get("/api/survival")
def get_survival(limit: int = 40, signal: str = "ecr"):
    """Odds each top-of-board player is gone before my next pick."""
    _check_signal(signal)
    with db.session() as conn:
        return tendencies.survival_odds(conn, limit, signal)


@app.post("/api/history/import")
def post_history_import(body: HistoryImportBody):
    path = Path(body.csv_path).expanduser()
    if not path.is_absolute():
        path = (Path(__file__).resolve().parent.parent / path).resolve()
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"No such file: {path}")
    try:
        summary = ingest.import_history(path, refresh=body.refresh)
    except SystemExit as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:                                   # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "summary": summary}


@app.post("/api/history/link")
def post_history_link(body: LinkBody):
    """Attach an ESPN owner to a board seat by hand.

    Auto-linking only fires when the owner label matches the manager name, so
    anyone who renamed their team needs this.
    """
    with db.session() as conn:
        known = conn.execute(
            "SELECT 1 FROM history WHERE owner_guid = ? LIMIT 1",
            (body.owner_guid,),
        ).fetchone()
        if not known:
            raise HTTPException(
                status_code=400, detail=f"Unknown owner_guid: {body.owner_guid}"
            )
        # One seat per owner: clear any previous holder of this guid.
        conn.execute(
            "UPDATE managers SET owner_guid = NULL WHERE owner_guid = ?",
            (body.owner_guid,),
        )
        if body.manager_id is not None:
            conn.execute(
                "UPDATE managers SET owner_guid = ? WHERE id = ?",
                (body.owner_guid, body.manager_id),
            )
        return {"ok": True, "managers": draft.managers(conn)}


@app.post("/api/history/seed-managers")
def post_seed_managers(season: int | None = None):
    """Rebuild the manager list and owner links from a season of history."""
    with db.session() as conn:
        try:
            summary = ingest.seed_managers_from_history(conn, season)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not summary["seeded"]:
            raise HTTPException(
                status_code=400,
                detail=summary.get("note") or "nothing to seed",
            )
        return {"ok": True, "summary": summary, "state": draft.state(conn)}


@app.post("/api/history/autolink")
def post_history_autolink():
    with db.session() as conn:
        linked = ingest.link_owners_to_managers(conn)
        return {"ok": True, "linked": linked, "managers": draft.managers(conn)}


# --------------------------------------------------------------------------- #
# static
# --------------------------------------------------------------------------- #

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    """Serve the shell with mtime-stamped asset URLs.

    Cache-Control alone is not enough: a browser that already cached app.js
    before the header existed keeps serving it, and never re-requests. Stamping
    the URL with the file's mtime changes the URL whenever the file changes, so
    an edit can never be masked by a stale asset.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for asset in ("app.js", "styles.css"):
        path = STATIC / asset
        if path.exists():
            html = html.replace(
                f"/static/{asset}", f"/static/{asset}?v={int(path.stat().st_mtime)}"
            )
    return HTMLResponse(html)
