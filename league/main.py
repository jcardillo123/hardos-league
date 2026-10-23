"""Local web server for the league history dashboard.

    python -m uvicorn league.main:app --reload --port 8100

Then open http://127.0.0.1:8100. Port 8100 because the draft tool owns 8000 and
both are often running at once.

Reads only. The 2026 refresh is the one write, and it is explicit: `POST
/api/refresh` re-hits ESPN for a season on demand. There is no background poll
and no schedule - the dashboard shows what was last ingested, and the page says
how old that is so a stale number can never pass for a live one.
"""

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import db, espn, ingest, queries
from .config import CURRENT_SEASON, SEASONS, STATIC


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="League History", lifespan=lifespan)

# Tracks an in-flight refresh so the UI can show progress and a second click
# cannot start a concurrent ESPN pull against the same season.
_refresh_state: dict = {"running": False, "season": None, "message": "",
                        "error": None}


@app.middleware("http")
async def no_cache_ui(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# --------------------------------------------------------------------------- #
#  meta
# --------------------------------------------------------------------------- #

@app.get("/api/seasons")
def api_seasons():
    with db.session() as conn:
        rows = queries.seasons(conn)
    if not rows:
        raise HTTPException(
            503, "No seasons ingested yet. Run `python -m league.ingest`."
        )
    return {"seasons": rows, "current": CURRENT_SEASON,
            "credentials": espn.has_credentials()}


@app.get("/api/owners")
def api_owners():
    with db.session() as conn:
        return {"owners": [dict(r) for r in conn.execute(
            "SELECT owner_guid, label, first_seen, last_seen, seasons "
            "FROM owners ORDER BY seasons DESC, label")]}


# --------------------------------------------------------------------------- #
#  1. standings and weekly results
# --------------------------------------------------------------------------- #

@app.get("/api/standings/{season}")
def api_standings(season: int):
    with db.session() as conn:
        rows = queries.standings(conn, season)
    if not rows:
        raise HTTPException(404, f"Season {season} is not ingested.")
    return {"season": season, "standings": rows}


@app.get("/api/results/{season}")
def api_results(season: int, week: int | None = None):
    with db.session() as conn:
        return {
            "season": season,
            "weeks": queries.season_weeks(conn, season),
            "results": queries.weekly_results(conn, season, week),
        }


@app.get("/api/roster/{season}/{week}/{team_id}")
def api_roster(season: int, week: int, team_id: int):
    with db.session() as conn:
        rows = queries.roster_week(conn, season, week, team_id)
    if not rows:
        raise HTTPException(404, "No roster stored for that team-week.")
    return {"season": season, "week": week, "team_id": team_id,
            "players": rows}


# --------------------------------------------------------------------------- #
#  2. luck and lineup management
# --------------------------------------------------------------------------- #

@app.get("/api/luck")
def api_luck(season: int | None = None):
    with db.session() as conn:
        return {"season": season, "board": queries.luck_board(conn, season)}


@app.get("/api/lineup-misses")
def api_lineup_misses(season: int | None = None,
                      limit: int = Query(25, ge=1, le=200)):
    with db.session() as conn:
        return {"season": season,
                "misses": queries.worst_lineup_calls(conn, season, limit)}


# --------------------------------------------------------------------------- #
#  3. head to head
# --------------------------------------------------------------------------- #

@app.get("/api/h2h")
def api_h2h(regular_only: bool = False):
    with db.session() as conn:
        return queries.head_to_head(conn, regular_only)


@app.get("/api/rivalry/{guid_a}/{guid_b}")
def api_rivalry(guid_a: str, guid_b: str):
    with db.session() as conn:
        out = queries.rivalry(conn, guid_a, guid_b)
    if not out["games"]:
        raise HTTPException(404, "Those two have never played each other.")
    return out


@app.get("/api/careers")
def api_careers():
    with db.session() as conn:
        return {"careers": queries.manager_careers(conn)}


@app.get("/api/manager/{owner_guid}")
def api_manager(owner_guid: str):
    with db.session() as conn:
        hist = queries.season_history(conn, owner_guid)
        if not hist:
            raise HTTPException(404, "No such manager.")
        return {"owner_guid": owner_guid, "seasons": hist,
                "loyalty": queries.owner_player_loyalty(conn, owner_guid)}


# --------------------------------------------------------------------------- #
#  4. players
# --------------------------------------------------------------------------- #

@app.get("/api/players")
def api_players(season: int | None = None, position: str | None = None,
                limit: int = Query(60, ge=1, le=500)):
    with db.session() as conn:
        return {"seasons": queries.player_seasons(conn, season, position, limit)}


@app.get("/api/performances")
def api_performances(season: int | None = None, position: str | None = None,
                     started_only: bool = True,
                     limit: int = Query(50, ge=1, le=500)):
    with db.session() as conn:
        return {"performances": queries.top_performances(
            conn, season, position, started_only, limit)}


@app.get("/api/player-search")
def api_player_search(name: str = Query(..., min_length=2)):
    with db.session() as conn:
        return {"name": name, "weeks": queries.player_search(conn, name)}


@app.get("/api/records")
def api_records():
    with db.session() as conn:
        return queries.record_book(conn)


@app.get("/api/moves")
def api_moves(season: int | None = None):
    """Trades and pickups together: the tab needs all three at once."""
    with db.session() as conn:
        return {
            "season": season,
            "trades": queries.trade_grades(conn, season),
            "pickups": queries.waiver_pickups(conn, season, limit=60),
            "managers": queries.transaction_summary(conn, season),
        }


# --------------------------------------------------------------------------- #
#  refresh
# --------------------------------------------------------------------------- #

def _do_refresh(season: int):
    """Re-ingest one season from ESPN. Runs on a worker thread."""
    _refresh_state.update(running=True, season=season, error=None,
                          message=f"fetching {season} from ESPN...")
    try:
        session = espn.build_session()
        with db.session() as conn:
            result = ingest.ingest_season(conn, session, season, refresh=True)
            ingest.refresh_owners(conn)
        _refresh_state["message"] = (
            f"{season}: {result.get('weeks', 0)} weeks, "
            f"{result.get('lineup_rows', 0)} lineup rows"
        )
    except Exception as exc:                        # surfaced to the UI as-is
        _refresh_state["error"] = str(exc)
        _refresh_state["message"] = f"{season} refresh failed"
    finally:
        _refresh_state["running"] = False


@app.post("/api/refresh")
def api_refresh(tasks: BackgroundTasks, season: int = CURRENT_SEASON):
    """Pull a season again from ESPN.

    Only ever triggered by a click. `replace_season` scopes the write to the one
    season, so a refresh that dies halfway cannot take the completed seasons
    with it.
    """
    if season not in SEASONS:
        raise HTTPException(400, f"Season {season} is not in {list(SEASONS)}.")
    if _refresh_state["running"]:
        raise HTTPException(409, f"A refresh of {_refresh_state['season']} "
                                 "is already running.")
    if not espn.has_credentials():
        raise HTTPException(
            503, "No ESPN_S2 / ESPN_SWID in .env - a private league read will "
                 "401. Run `python -m tools.check_env` to check them."
        )
    tasks.add_task(_do_refresh, season)
    return {"started": True, "season": season}


@app.get("/api/refresh/status")
def api_refresh_status():
    return _refresh_state


# --------------------------------------------------------------------------- #
#  UI
# --------------------------------------------------------------------------- #

STATIC.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    """Serve the shell with mtime-stamped asset URLs.

    Same reason as the draft tool: Cache-Control alone will not dislodge an
    already-cached app.js, and a stale asset silently serving an old dashboard
    is the worst failure mode a local tool can have.
    """
    path = STATIC / "index.html"
    if not path.exists():
        raise HTTPException(500, "league/static/index.html is missing.")
    html = path.read_text(encoding="utf-8")
    for asset in ("app.js", "styles.css"):
        a = STATIC / asset
        if a.exists():
            html = html.replace(
                f"/static/{asset}",
                f"/static/{asset}?v={int(a.stat().st_mtime)}",
            )
    return HTMLResponse(html)
