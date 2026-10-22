"""Freeze the whole dashboard into static files for GitHub Pages.

    python -m league.export

GitHub Pages serves files, not programs - there is no Python there to answer
`/api/standings/2025`. So every answer the API would ever give is written out
once, as JSON, into `docs/data/`, and the same UI reads those instead. The
numbers cannot drift between the two because they come from the same
`queries.py` functions; only the transport differs.

What the static build gives up is the one thing that needs a server: the
refresh button. On Pages the sequence is

    python -m league.ingest --season 2026     (or the refresh button locally)
    python -m league.verify
    python -m league.export
    git commit && git push

and the site is current again.

Filtering that the API does in SQL - the player season/position pickers and the
name search - is done in the browser in static mode, over the full tables
exported here. That is deliberate: exporting a file per filter combination
would mean 80-odd near-identical files, and any one of them going stale is a
silent wrong answer.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone

from . import db, queries
from .config import CURRENT_SEASON, ROOT, STATIC

DOCS = ROOT / "docs"
DATA = DOCS / "data"

# Injected into the copied index.html. `LEAGUE_STATIC` is what app.js checks to
# route reads at `data/*.json` instead of `/api/*`; the robots tag keeps a page
# carrying real people's names out of search results, which is a different
# question from whether the repository is private.
STATIC_HEAD = (
    '  <meta name="robots" content="noindex, nofollow">\n'
    '  <script>window.LEAGUE_STATIC = true;</script>\n'
)


def _write(name: str, payload) -> int:
    path = DATA / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _copy_ui() -> None:
    """Copy the UI across, marking the copy as the static build.

    index.html is rewritten rather than templated: one shell serves both modes,
    so a change to the live dashboard cannot be forgotten here.
    """
    DOCS.mkdir(parents=True, exist_ok=True)
    for asset in ("app.js", "styles.css"):
        shutil.copyfile(STATIC / asset, DOCS / asset)

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    if "LEAGUE_STATIC" not in html:
        html = html.replace("</head>", STATIC_HEAD + "</head>", 1)
    # The live server stamps asset URLs with an mtime to beat the browser
    # cache. Pages serves by ETag, so plain paths are correct here - and they
    # have to be relative, because a project site lives under /<repo>/.
    html = html.replace('href="/static/', 'href="').replace(
        'src="/static/', 'src="')
    (DOCS / "index.html").write_text(html, encoding="utf-8")

    # Tells Pages to serve the files as-is instead of running them through
    # Jekyll, which otherwise ignores any file or folder starting with "_".
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA)
    DATA.mkdir(parents=True, exist_ok=True)

    total = 0
    with db.session() as conn:
        seasons = queries.seasons(conn)
        years = [s["season"] for s in seasons]
        owners = [dict(r) for r in conn.execute(
            "SELECT owner_guid, label, first_seen, last_seen, seasons "
            "FROM owners ORDER BY seasons DESC, label")]

        total += _write("seasons.json", {
            "seasons": seasons,
            "current": CURRENT_SEASON,
            # The API reports whether ESPN credentials are loaded, so the UI
            # can tell "nothing to refresh" from "refresh is broken". A
            # published build has no credentials and nothing to refresh.
            "credentials": False,
        })
        total += _write("owners.json", {"owners": owners})

        for y in years:
            total += _write(f"standings-{y}.json",
                            {"season": y, "standings": queries.standings(conn, y)})
            total += _write(f"results-{y}.json", {
                "season": y,
                "weeks": queries.season_weeks(conn, y),
                "results": queries.weekly_results(conn, y),
            })
            # Keyed "week-teamid" so the weekly view can open any roster
            # without a request per team.
            rosters: dict[str, list] = {}
            for row in conn.execute(
                "SELECT DISTINCT week, team_id FROM lineups WHERE season = ?",
                (y,)
            ):
                rosters[f"{row['week']}-{row['team_id']}"] = queries.roster_week(
                    conn, y, row["week"], row["team_id"])
            total += _write(f"rosters-{y}.json", rosters)

        # Scope-keyed blobs: "all" plus one entry per season, matching the
        # `?season=` the live API takes.
        total += _write("luck.json", {
            "all": queries.luck_board(conn),
            **{str(y): queries.luck_board(conn, y) for y in years},
        })
        total += _write("misses.json", {
            "all": queries.worst_lineup_calls(conn, limit=25),
            **{str(y): queries.worst_lineup_calls(conn, y, 25) for y in years},
        })
        total += _write("h2h.json", {
            "full": queries.head_to_head(conn, False),
            "regular": queries.head_to_head(conn, True),
        })
        total += _write("careers.json", {"careers": queries.manager_careers(conn)})

        total += _write("managers.json", {
            o["owner_guid"]: {
                "seasons": queries.season_history(conn, o["owner_guid"]),
                "loyalty": queries.owner_player_loyalty(conn, o["owner_guid"]),
            } for o in owners
        })

        # Every pair that has actually played, keyed "a|b". Each game is stored
        # from both sides already, so this is symmetric by construction.
        rivalries: dict[str, dict] = {}
        for a in owners:
            for b in owners:
                if a["owner_guid"] == b["owner_guid"]:
                    continue
                r = queries.rivalry(conn, a["owner_guid"], b["owner_guid"])
                if r["games"]:
                    rivalries[f"{a['owner_guid']}|{b['owner_guid']}"] = r
        total += _write("rivalries.json", rivalries)

        # Unfiltered, with the limits lifted: the browser does the filtering
        # that the API does in SQL.
        #
        # `weeks` is every lineup row rather than `top_performances`, because it
        # has to serve the name search too, and search must find a week a player
        # sat out - which `points IS NOT NULL` would drop. The static reader
        # applies that filter itself for the leaderboard.
        total += _write("players.json", {
            "seasons": queries.player_seasons(conn, limit=10 ** 9),
            "weeks": [dict(r) for r in conn.execute(
                # Every column the API returns, including `team_name`, which no
                # current view reads - so adding a column to that table later
                # cannot produce one that works live and is blank on Pages.
                "SELECT l.season, l.week, l.player_name, l.position, "
                "       l.pro_team, l.points, l.projected, l.started, "
                "       l.slot_label, t.owner_label, t.team_name "
                "FROM lineups l "
                "JOIN teams t ON t.season=l.season AND t.team_id=l.team_id "
                # Same order as `queries.player_search`, so the static search
                # can filter this list and get the API's rows in the API's
                # order without sorting again.
                "ORDER BY l.season DESC, l.week DESC, l.player_name")],
        })
        total += _write("records.json", queries.record_book(conn))

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        total += _write("manifest.json", {
            "generated_at": stamp,
            "seasons": years,
            "through": {s["season"]: s["weeks_played"] for s in seasons},
        })

    _copy_ui()

    files = sorted(DATA.glob("*.json"))
    print(f"wrote {len(files)} data files, {total / 1_048_576:.1f} MB")
    for f in sorted(files, key=lambda p: -p.stat().st_size)[:6]:
        print(f"   {f.name:<22} {f.stat().st_size / 1024:>8.0f} KB")
    print(f"\nstatic site in {DOCS}")
    print("serve it locally with:")
    print(f"   python -m http.server -d {DOCS} 8200")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
