# League History

A dashboard over five seasons of the ESPN league — standings, weekly results,
schedule luck, lineup decisions, head-to-head, manager careers, player history,
and a record book.

Separate from the draft tool on purpose. The draft tool's database is wiped and
rebuilt on every ingest and holds live picks during a draft; this one is an
archive that only grows. They share one thing: the `.env` at the project root,
and therefore the ESPN credentials.

## Running it

```bash
.venv\Scripts\python.exe -m uvicorn league.main:app --reload --port 8100
```

Then open <http://127.0.0.1:8100>. Port 8100 because the draft tool owns 8000
and both are often up at once.

First time, or after a long gap:

```bash
.venv\Scripts\python.exe -m league.ingest
```

```bash
.venv\Scripts\python.exe -m league.verify
```

`verify` exits non-zero if anything fails, so it can gate a refresh.

## The 2026 season

2026 is in progress. It is included, marked `IN PROGRESS` in the top bar, and
refreshed **only when the button is clicked** — there is no background poll and
no schedule. Weeks that have not been played are skipped rather than stored as
zeros, so a partial season never drags an average down.

A refresh is scoped to one season, so one that dies halfway cannot take the
four completed seasons with it. Re-run `league.verify` afterwards.

## Publishing it

GitHub Pages serves files, not programs — there is no Python there to answer
`/api/standings/2025`. So `league.export` writes every answer the API would
ever give into `docs/data/*.json` and copies the UI beside it, with a marker
that switches the page's reader from `fetch('/api/…')` to those files. Same
HTML, same CSS, same `app.js`, same `queries.py` behind the numbers — only the
transport differs, which is why the two builds cannot disagree.

```bash
.venv\Scripts\python.exe -m league.export
```

Check it before pushing — it serves from plain files, no server needed:

```bash
.venv\Scripts\python.exe -m http.server -d docs 8200
```

**The published build is read-only.** The refresh button is hidden there and
the whole `/api/refresh` path is absent; updating the site means running the
ingest locally and exporting again:

```bash
.venv\Scripts\python.exe -m league.ingest --season 2026
```

then `league.verify`, then `league.export`, then commit and push. Refreshing
stays local, where the ESPN credentials are.

Two things the export deliberately does not reproduce. It reports
`credentials: false`, because a published build has none and nothing to
refresh. And the player filters — season, position, name search — are applied
in the browser over the full exported tables rather than exported one file per
filter combination; there are 84 of those, and one going stale is a silently
wrong answer rather than a missing page.

`tools/` has no test for this, but the export is checkable: every route
function and its exported counterpart return the same JSON, so a diff of the
two is a complete verification. That is how it was checked in — 1099
comparisons across every team-week roster, every manager, every rivalry pair in
both directions, all 35 player-filter combinations, and a set of searches.

Live at <https://jcardillo123.github.io/hardos-league/>, from `/docs` on
`main` — pushing is publishing, there is no deploy step.

The repository is public. Not a preference: Pages will not serve a private
repository on a Free plan, and the published page is public either way, so the
choice was between a public repo and a $4/month subscription that would have
hidden only the source. Nothing secret is in here — `.env` is ignored and
`.env.example` carries placeholders — but it does carry real names, so the
page ships with `noindex, nofollow` to keep it out of search results.

## What the numbers mean

**xW (expected wins)** — what each week's score would have earned against the
whole league rather than against one scheduled opponent. A week that outscored
nine of nine teams is a whole expected win; one that outscored five of eleven is
0.45.

**Luck** — actual wins minus xW. Positive means the schedule handed over wins
the scores did not earn. This is the schedule's doing, not the manager's.

**Eff%** — points scored as a share of the best lineup that roster could have
started. This one *is* the manager's doing. Luck and efficiency are kept apart
deliberately; a team can be high in both.

**Left** — points that sat on the bench. Players on IR are excluded, because
ESPN would never have allowed them to start.

Regular season only, everywhere luck appears: playoff matchups are decided by
seeding, so counting them folds the reward for a good regular season back into
the measure of it.

## Two things that will bite anyone editing this

**All-play must be reduced to a per-week rate before it is summed.** The league
grew from 10 teams to 12 in 2024. Beating 9 of 9 and beating 11 of 11 are both
one whole expected win, but pooling the raw counts and dividing once at the end
reports 0.90 for a team that went 9-0 one week and 0-11 another. `queries.py`
has a single `WEEK_RATE` fragment for this; use it rather than writing the
arithmetic again.

**Managers join on `owner_guid`, never on name.** Team names change every
season and several managers have had more than one.

## Layout

| file | what it does |
| --- | --- |
| `config.py` | paths, ESPN id maps, the league's shape |
| `espn.py` | the ESPN client and the disk cache under `data/cache/league/` |
| `ingest.py` | pulls a season and writes `seasons`/`teams`/`matchups`/`team_weeks`/`lineups` |
| `stats.py` | pure functions: optimal lineup, all-play, expected wins |
| `verify.py` | reconciles stored scores against ESPN's own; run after every ingest |
| `queries.py` | the read layer, one function per view |
| `main.py` | FastAPI routes and the one write (`POST /api/refresh`) |
| `export.py` | freezes every API answer into `docs/` for GitHub Pages |
| `static/` | the UI — plain HTML/CSS/JS, no build step |

Data lands in `data/league.db`. Raw ESPN responses are cached under
`data/cache/league/`, so re-running an ingest over completed seasons costs
nothing and hits nobody.

## One known upstream artifact

2022 week 6, team 5: ESPN's matchup `totalPoints` reads 130.80 while its own
`pointsByScoringPeriod`, the roster's `appliedStatTotal`, and the sum of its own
player rows all read 120.10. The 10.70 gap is not reconstructible from any
player data ESPN serves — it looks like a stat correction applied to the matchup
total and never to the archived roster. It does not change the result (both
numbers beat the opponent's 114.38). It is registered in
`verify.KNOWN_ARTIFACTS` so it is skipped rather than re-investigated every
time.
