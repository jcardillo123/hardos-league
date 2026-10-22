# Fantasy Football Draft Tool

A local draft board. Click a player, they leave the board. Runs entirely on your
machine — after the first data download it works offline, which is the point:
draft night is the worst possible time to depend on someone else's uptime.

## Quick start

```bash
.venv\Scripts\python.exe -m app.ingest
```

```bash
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Then open <http://127.0.0.1:8000>.

## What exists now

| Piece | Status |
|---|---|
| Clickable draft board, 5 position columns | working |
| Pick tracking, snake order, on-the-clock | working |
| Undo / reset, crash-safe (state in SQLite) | working |
| "N picks until you", back-to-back detection | working |
| My roster + open starter slots | working |
| Round × manager grid view | working |
| League + manager setup UI | working |
| nflverse stats ingest, ID crosswalk | working |
| FantasyPros CSV rankings import | working |
| ESPN league history scrape (`DraftTool.py`) | working |
| History import + outcome join + owner linking | working |
| Tendency model (share, timing, runs, outcomes) | working, validated against planted fixtures |
| Pick projection + survival odds | working, API only |
| **Tendency UI on the board** | **not built yet** — API is ready |

## Where your league data actually lives

All of it is in **`data/draft.db`**, not in files on disk. The CSVs are inputs that
get imported once; nothing reads them again afterwards.

| Table | Holds | Rows here |
|---|---|---|
| `history` | every pick from 2022–2025 | 792 |
| `adp_history` | historical preseason ADP | 2,866 |
| `managers` | the twelve seats + ESPN owner links | 12 |
| `settings` | league config (teams, rounds, scoring, roster) | 1 |
| `players` | the current draft pool | 516 |
| `picks` | the live draft | varies |

`draft_history.csv` and `league_settings.csv` in the project root are the raw
DraftTool.py output — the source you imported *from*, kept for re-import, not read
at runtime.

## Data sources

Everything comes from [nflverse](https://github.com/nflverse/nflverse-data)
release assets, read directly as parquet.

> **Note:** `nfl_data_py` is *not* used. It pins `numpy<2`, and on Python 3.14
> that forces a source build of numpy 1.26 which fails without a C compiler.
> `app/ingest.py` reads the same parquet files the package wraps, so there is no
> functional loss. This also means no Pro-Football-Reference scraping — the
> `pfr_advstats` release already publishes PFR advanced stats.

| Release | Used for |
|---|---|
| `players` | ID crosswalk: `gsis_id`, `espn_id`, `pfr_id`, age, rookie season |
| `stats_player` | season fantasy points, target share, WOPR |
| `snap_counts` | mean offensive snap share |
| `schedules` | bye weeks |

The `players` table is what makes the whole thing join up: it carries both
`espn_id` and `pfr_id`, so your ESPN draft history and nflverse production data
land on the same player.

### Rankings

Drop FantasyPros CSV exports into `data/rankings/` and rebuild. **You want two
exports:** the draft rankings page supplies `RK` and `TIERS` but no projected
points, and the projections page supplies `FPTS`. Files are merged — the newest
file with a real ranking column is the base and defines the board order, and
other files may only fill columns the base leaves entirely empty.

Without a projections export, `Projected` and `VOR` fall back to last season's
actual scoring, which penalises anyone who was injured. Ingest says so explicitly
when that happens. See [data/rankings/README.md](data/rankings/README.md).

With no CSV present the pool falls back to the top 400 players by last season's
fantasy points. That is backward-looking by construction — it has no idea about
offseason moves, injuries, or rookies — so it is a way to click around and test
the board, not a draft strategy.

### Vegas-implied projections

Drop a [winwithodds.com](https://winwithodds.com) season-long projection CSV into
`data/vegas/` and rebuild. This is a **second, independent projection** — built
from betting markets instead of analysts — so it lives in its own folder rather
than being merged into the rankings pool, where the disagreement would be
averaged away.

It adds two columns: `vegas_proj` (season points) and `vegas_vor` (VOR from those
points, rescaled into the same units as `vor`). On the board that surfaces as the
`Vegas` and `Edge` columns, three new sorts, and a `Value from` toggle on the
suggestion box — `Proj`, `Vegas`, or `Blend`.

The two sources do not share a scale, so `vegas_vor` is multiplied by one global
factor fitted from your pool at ingest. Why one factor and not per-position, why
the Edge sort skips some players, and what happens with no file present:
[data/vegas/README.md](data/vegas/README.md).

## Layout

```
app/
  config.py    paths, league defaults, board columns
  db.py        SQLite schema; draft state persists across restarts
  ingest.py    nflverse + FantasyPros + ESPN history -> players table
  draft.py     pick order, roster needs, availability queries
  tendencies.py league history modeling, projection, survival odds
  main.py      FastAPI endpoints
  static/      board UI (vanilla JS, no build step)
data/
  cache/       downloaded parquet
  rankings/    your rankings / projections CSVs
  vegas/       winwithodds season-long projections (second opinion, not merged)
  adp/         ESPN ADP exports + historical preseason ADP
  draft.db     EVERYTHING stateful: player pool, live draft, league history,
               managers, league settings, historical ADP
tools/
  check_env.py          validate .env without printing secrets
  compare_signals.py    ECR vs ADP as the projection's availability signal
  odds_probe.py         discover what a the-odds-api.com plan exposes for NFL
  diagnose_history.py   explain why a report section is empty
  espn_adp.py           probe ESPN for platform-wide ADP
  make_fake_history.py  synthetic history fixture for testing the model
DraftTool.py   ESPN multi-season draft history scraper
```

## Keyboard

| Key | Action |
|---|---|
| `/` | focus search |
| `Enter` | draft the top search result |
| `Esc` | clear search |
| `Ctrl+Z` | undo last pick |

## League history

`DraftTool.py` pulls your league's past drafts from ESPN's undocumented API.
Private leagues need cookies in `.env` (`ESPN_S2`, `ESPN_SWID`) — the file
header explains where to find them.

```bash
.venv\Scripts\python.exe DraftTool.py --seasons 2022 2023 2024 2025
```

```bash
.venv\Scripts\python.exe -m app.ingest --history draft_history.csv
```

```bash
.venv\Scripts\python.exe -m app.tendencies
```

```bash
.venv\Scripts\python.exe -m app.ingest --seed-managers
```

`--seed-managers` rebuilds the twelve manager rows from the newest imported
season — real names, and owner GUIDs linked in one pass — instead of typing them
and hoping the auto-matcher connects. The seats it writes are *that season's*
draft order; slots move every year, so set this year's order on the Setup tab.

### Player names: do not trust ESPN's lookup

`DraftTool.py` resolves `playerId` → name/position through ESPN's league-scoped
`kona_player_info` view. **That view is authenticated even when the league itself
is public**, and when it fails the scraper writes the pick with a null name and
carries on. The result is a `draft_history.csv` full of valid picks with empty
`player_name` and `position` columns — and since every tendency rate needs
`position`, the whole model silently reports nothing while owners still list
fine.

The importer no longer depends on it. `resolve_espn_ids()` fills gaps in two
passes:

1. **nflverse `players.parquet` via `espn_id`** — 99.8–100% coverage of
   skill-position players active 2022–2025, zero duplicate ids, already cached
   locally. Team defenses are decoded arithmetically from ESPN's negative ids
   (`-16000 - proTeamId`).
2. **ESPN's standalone `/seasons/{season}/players` endpoint** — needs no league
   and no cookies. Catches the handful nflverse has no `espn_id` for (two-way and
   practice-squad oddities). Cached as slim JSON since the raw response is ~38MB.

On a real 792-pick, four-season history this took resolution from 0/792 to
792/792.

If a report section comes back empty, run:

```bash
.venv\Scripts\python.exe tools\diagnose_history.py
```

It checks every column for nulls and BLOB storage, prints the round and position
distributions, shows how many picks survive each filter, and names the likely
cause. `--show-owners` includes real owner labels; they're redacted by default so
the output is safe to share.

The import adds three things ESPN does not give you:

- **`draft_slot`** — which seat each owner drafted from that season, derived from
  their round-1 pick. Seats move year to year, and that confounds any "they
  always open RB" reading, so it has to be recorded.
- **`pos_taken`** — the Nth player at that position taken that season.
- **`season_pts` / `pos_finish`** — what the player actually went on to score,
  joined from nflverse. This turns the history into an outcome record, not just
  a record of choices.

Owners are auto-linked to board seats by name. Anyone who renamed their team
needs a manual link via `POST /api/history/link`.

### "Me" is an identity, not a chair

`managers.is_me` and `league["my_slot"]` both describe a *seat*. Reordering the
draft reassigns owners to seats, so a seat-based marker silently makes you whoever
landed in your old chair — in practice "me" drifted from Jake Cardillo to Will
Finke to Nicky Yarnall across a few reorderings, with no warning.

Identity now lives in `league["my_owner_guid"]`, and the seat is derived from it:

- Ticking the **me** radio calls `claim_identity` — it records the *owner* sitting
  in that seat, not the seat number.
- Every manager save then calls `resolve_me`, which moves `is_me` to whichever seat
  that owner now occupies and syncs `my_slot`.
- Starting a mock from a seat is also a deliberate claim, so it sets identity too —
  otherwise a later reorder would yank you back to your real seat mid-mock.

Falls back to the existing seat when there is no identity to resolve (a league with
no imported history) or when your owner is not seated at all — better to leave the
board working than to blank out who is drafting. Resizing the league clears the
identity, because reseeding replaces every manager with a blank `Team N` and drops
the owner links.

Verified by swapping two seats without claiming anyone: "me" moved from slot 2 to
slot 1 *with Jake Cardillo*, instead of staying on seat 2 and silently becoming
Enrique Puig.

### What the model reports

| Metric | Robustness |
|---|---|
| First round each manager takes each position | strong — one observation per season, very legible |
| Position share by phase (early/mid/late) | good — shrunk toward league base rate |
| Run-following rate vs league | good |
| Reach vs the ESPN market | good — 592 picks, exact zero baseline |
| Draft results (value, hits, starters, production) | weak — heavily luck-driven, reported separately |

### Reach: who jumps the market

Measures whether a manager takes players earlier or later than ESPN's own draft
ranks imply. Requires market ranks stamped on history:

```bash
.venv\Scripts\python.exe -m app.ingest --market-ranks
```

#### Why raw rank − pick does not work

`espn_draft_rank` ranks ~1,700 players, most of whom nobody drafts, so rank
climbs far faster than pick number. Measured on this league in 2025: round 1
averaged rank 7, but round 13 averaged rank **300** against picks 145–156.
Subtracting directly gives a mean "reach" of +49 to +71 depending on season, and
labels every late-round pick a huge reach purely from scale drift.

#### What it does instead

Each season's picks are re-sorted into ESPN's recommended order and handed back
the *same set of pick numbers*. The best-ranked player gets that season's
earliest pick, and so on.

```
reach = expected_pick − actual_pick
```

`+12` means taken about a full round earlier than ESPN's order implies. Because
expected picks are a permutation of the actual picks, **the league mean is exactly
0** — unlike the outcome metric, zero genuinely is the baseline and no league
correction is needed. Verified: league mean `+0.0`, with 29% reaches and 29%
waits.

Coverage is 592 picks across 2023–2025 (170/180, 206/216, 216/216). **2022 is
excluded automatically** — ESPN serves no draft ranks for it.

Per manager the report gives mean and median reach, reach/wait rates, breakdowns
by phase and by position, and the single earliest and latest picks by name.

#### On the board, during the draft

Every pick in the Next picks panel carries a reach badge, and `/api/projection`
includes a trimmed `reach` object per pick. The badge uses the **median**, not the
mean, because reach has heavy tails and three of twelve managers here have a mean
and median that disagree in sign.

Per-position reach appears directly on the probability row it applies to —
`▲13` means this manager takes that position 13 picks earlier than the market
implies, `▼6` means six later. Only shown at `|reach| >= 5`; below that it is noise
against a ~50-pick sample.

This is where the overall number turns out to be misleading, and why the split
matters:

```
Enrique Puig   near market +1   1.01
  RB  ████████░░  59%  ▲13
  WR  █████░░░░░  34%
  TE  █░░░░░░░░░   5%
```

He reads as "near market" overall — median +0.5 — while reaching **13 picks early
on running backs** and 21 on quarterbacks. The aggregate hides exactly the
behavior you need at 1.01.

#### Read the median when it disagrees with the mean

Reach distributions have heavy tails — one pick 70 slots early moves a mean built
from ~50 picks. Where mean and median disagree in sign by 3+ picks, the report
says so explicitly and tells you to trust the median. Three of twelve managers in
this league trip that check, including one at mean `+2.7` / median `−4.0`.

The per-position breakdown is where this gets actionable, especially crossed with
draft results: a manager who reaches `TE +20` while their TE outcome value runs 10
slots below league is reliably paying up for tight ends and reliably not getting
paid back.

### Reading the draft-results block

Each pick is scored by comparing two facts about the player:

- **taken** — the Nth player at his position off the board that season. "taken RB5"
  means the 5th running back drafted.
- **finish** — where he actually ended that season among all players at his
  position, by fantasy points. "finished RB2".

**value = taken − finish.** Draft the 5th RB, he finishes RB2, that is +3.

The catch that makes the raw number misleading: finish is measured against *every*
player at the position, including undrafted breakouts who leapfrog drafted busts.
So the league-wide average value is about **−9.4, not 0**. Judging anyone against
zero marks the whole league as incompetent. Everything is therefore reported
against the league bar — `+2.2 slots vs league` means their picks beat the slot
they were taken at by 2.2 more slots than a typical manager in this league.

Four measures, each stated against that bar:

| Line | Means |
|---|---|
| `value` | slots beaten vs the league average, as above |
| `hits` | share of picks whose player finished better than he was drafted |
| `starters` | share that finished startable — QB/TE/K/DST top-12, RB/WR top-24 |
| `production` | total fantasy points those players scored, per pick made — raw output, ignoring where they were drafted |

Plus `by season` (points per pick and starters per season), `best steal`,
`worst bust`, and per-position value vs league.

`worst bust` only considers players **taken inside** the startable range. Without
that restriction it is always some round-17 flier who never played — technically
the largest negative delta, but it cost nothing and says nothing about the
manager. Startable thresholds are fixed rather than read from league config, so
the number means the same thing across seasons when the league changed size.

Four seasons of a 12-team league is ~64 picks per manager, about four per round.
So rounds are bucketed into phases rather than modeled individually, and every
per-manager rate is shrunk toward the league base rate in proportion to how
little data supports it. Sample sizes travel with every number. Keepers are
excluded throughout — a keeper is not a draft decision.

Reach against the market is built on `espn_draft_rank` rather than ADP: ADP has no
2025 data at all, while draft ranks cover 2023–2026 consistently. See the reach
section below.

### ESPN ADP: what is and isn't available

Measured, not assumed:

| Endpoint | Auth | Names/positions | ADP fields |
|---|---|---|---|
| `/seasons/{season}/players` (any view) | none | yes, 11,534 players | **no** — no `ownership`, no `draftRanksByRankType`, and `limit` is ignored |
| League `kona_player_info` | **required** | yes | yes — `averageDraftPosition`, `percentOwned`, `draftRanksByRankType` (separate PPR/STANDARD), `draftAuctionValue` |

So ESPN ADP is real and useful, but it lives only behind the authenticated
league-scoped view — the same one whose name lookup fails. Getting it requires
`ESPN_S2` / `ESPN_SWID` in `.env`, even for a public league.

```bash
.venv\Scripts\python.exe tools\espn_adp.py --seasons 2022 2023 2024 2025 2026
```

Writes `data/adp/espn_adp_{season}.csv`. `app.ingest` picks up the current season
automatically and fills any ADP the rankings files left empty — matched by ESPN
id first, then by name so team defenses connect.

#### Measured coverage per season

Historical ADP *is* real, with one hole. Verified by spot-checking players whose
value moved sharply between years:

| Season | ADP | `espn_draft_rank` | Evidence |
|---|---|---|---|
| 2022 | **real**, 115 distinct | missing entirely | Cooper Kupp ADP 5.5 — correct for 2022 after his 2021 triple crown |
| 2023 | **real**, 211 distinct | real | McCaffrey 2.1 |
| 2024 | **real**, 244 distinct | real | Lamb, Jefferson top 5 |
| 2025 | **broken** — exactly 170.0 for all 600 | real | single distinct value; no data wearing the shape of data |
| 2026 | **real**, 200 distinct | real | Gibbs 1.7, Kupp 168.8 |

Two consequences. ESPN reports an "effectively undrafted" ceiling instead of a
null — exactly 170.0 plus a cluster of 169.9x values — so `ADP_UNDRAFTED_CEILING`
discards anything at or above 169. And a season whose ADP has a single distinct
value is rejected outright rather than imported as a flat column that would make
every player look equally valued.

Between the two columns every season is covered: ADP for 2022–2024 and 2026,
`espn_draft_rank` for 2023–2026. The reach metric is built on `espn_draft_rank`
for that reason — it has no 2025 hole. `--market-ranks` stamps both columns onto
`history` (`market_rank`, `market_adp`), so ADP is available there as a
cross-check.

### Testing it without your league

`tools/make_fake_history.py` generates a synthetic four-season history with
deliberately planted tendencies, so the engine can be checked for finding real
signal rather than noise:

```bash
.venv\Scripts\python.exe tools/make_fake_history.py fake_draft_history.csv
```

The report should recover *Early QB Eric* taking a QB in round 1 every year,
*Zero RB Zoe* never touching a running back before round 5, *TE Tom* on a tight
end in round 1, and *Run Chaser Randy* following positional runs ~80% of the
time against a league rate near 43%. It does. Clear the fixture before real use:
`DELETE FROM history;`

### Live projection

`GET /api/projection` gives position probabilities for every pick between now
and your next turn. For each pick and position, three independent terms multiply
and are then normalized:

```
w(pos) = tendency(pos) × need(pos) × availability(pos)
P(pos) = w(pos) / Σ w
```

| Term | Definition |
|---|---|
| `tendency` | their shrunk phase rate: `(count + K·league) / (n + K)`, `K = 6` |
| `need` | `1.0` with an open starter slot (or open FLEX for RB/WR/TE), else `0.35` |
| `availability` | `0` if none left, else `clamp(1 + (pick − bestRank) / 24, 0.6, 1.8)` |

`availability` is the bargain term: a player ranked well ahead of the current pick
who is still on the board tends to get taken. All four constants are named at
[tendencies.py:44](app/tendencies.py:44) and are chosen for defensible behavior,
not fitted to your league.

#### Switchable board signal: three separate markets

`availability` needs a ranking to define "ahead of value", and which one you pick
is a real modelling choice. Three are kept, deliberately **not** merged:

| Signal | Column | What it measures |
|---|---|---|
| `ecr` | `ecr` | expert consensus rank — what analysts recommend |
| `adp` | `adp` | consensus average draft position, from your rankings files |
| `espn_adp` | `espn_adp` | ESPN's platform ADP — where players go in the league you actually draft in |

They were briefly one column, with ESPN gap-filling whatever the rankings files
left empty. That was a mistake: a source like RotoBaller supplies 300 ADP values,
so ESPN contributed 3, and the "ADP" signal silently stopped meaning ESPN's ADP
while still being labelled that way. Consensus ADP and ESPN ADP measure different
things over different player populations, so they get their own columns and their
own toggle entries.

On a real pool the three genuinely diverge — Ja'Marr Chase sits at ECR 1,
consensus ADP 3.1, ESPN ADP 4.7 — and the ESPN signal names a different likely
receiver than ECR does.

Either ADP covers only the top few hundred players. Positions with no ADP left
fall back to ECR rather than going neutral, and the panel names which fell back.
Toggle entries whose column is empty are disabled rather than silently behaving
like ECR.

```bash
.venv\Scripts\python.exe tools\compare_signals.py --limit 12
```

Pass `signal=ecr|adp` to `/api/projection` and `/api/survival`; add
`compare=true` to the projection and every position also carries `p_other` and
`delta` for the opposite signal. The board has a toggle in the Next picks panel
that shows both numbers side by side and remembers the choice.

ADP covers only the top ~200 players, so deep in a draft no remaining player at
a position has one. Those positions fall back to ECR rather than going neutral —
a neutral 1.0 would flatten the distribution exactly when it matters — and the
panel names which positions fell back.

Measured on a real 12-team board at pick 9, the two signals differ by only about
**1.3 percentage points per position**, flipping the top position on 2 of 12
picks. The shift is systematic rather than noisy: ESPN drafters take running
backs earlier than consensus recommends, so ADP consistently lifts RB and drops
WR. It changes recommendations more than probabilities — the top RB under ECR was
Ashton Jeanty, under ADP De'Von Achane, and survival odds surface an entirely
different at-risk set. `GET /api/survival` ranks the board by the odds a player is gone
before you pick again.

### How VOR is calculated

VOR — value over replacement — is the currency every recommendation is priced in.
It answers "how many more points does this player score than what I could get for
free at his position?"

```
replacement_level(pos) = starters at pos, league-wide
VOR(player)            = proj_points(player) − proj_points(Nth best at pos)
```

**Step 1 — how many starters the league needs at each position.** Dedicated slots
times team count, plus a share of the FLEX slots split RB 45% / WR 45% / TE 10%
by how often each actually fills a flex. For a 12-team league with
`QB1 RB2 WR2 TE1 FLEX2 K1 DST1`:

| Pos | Dedicated | Flex share | Replacement level |
|---|---|---|---|
| QB | 1 × 12 = 12 | — | **12** |
| RB | 2 × 12 = 24 | round(2 × 12 × 0.45) = 11 | **35** |
| WR | 2 × 12 = 24 | round(2 × 12 × 0.45) = 11 | **35** |
| TE | 1 × 12 = 12 | round(2 × 12 × 0.10) = 2 | **14** |
| K | 1 × 12 = 12 | — | **12** |
| DST | 1 × 12 = 12 | — | **12** |

**Step 2 — the baseline is that Nth player's projection.** Sort every player at a
position by projected points and take the level-th one. That player is what's
freely available, so he is worth zero.

**Step 3 — subtract.** VOR is each player's projection minus his position's
baseline.

Worked from the live pool:

| Player | Pos | Proj | Baseline (Nth at pos) | VOR |
|---|---|---|---|---|
| Josh Allen | QB | 388 | QB12 ≈ 313 | **75** |
| Jahmyr Gibbs | RB | 373 | RB35 ≈ 183 | **190** |
| Christian McCaffrey | RB | 370 | RB35 ≈ 183 | **187** |
| Puka Nacua | WR | 349 | WR35 ≈ 192 | **157** |

**This is why the highest projection is not the best pick.** Josh Allen has the
single largest projection in the pool at 388 points, but the 12th-best quarterback
projects 313 — so starting Allen only gains you ~75 points over a quarterback you
could take in the last few rounds. Gibbs projects 15 fewer points than Allen, but
the 35th-best running back projects only 183, so he gains you 190. Quarterback is
deep; running back is not. VOR is what converts raw projections into that
comparison, and it is the only reason the suggestion box ranks Gibbs above Allen.

Two consequences worth knowing. VOR depends on your league settings — change
FLEX or team count and every VOR shifts, so rebuild the pool after editing them.
And VOR is only as good as `proj_points`: without a projections file it silently
becomes "points above replacement *last season*", which is a different and much
less useful statement.

### Suggested pick

`GET /api/suggest` recommends what to take, and unlike the projections it runs a
**joint simulation** rather than treating each upcoming pick independently.

`project_upcoming` is fine for "who goes next" but useless for "what should I
take", because it never removes the players those picks would consume. `suggest_pick`
runs the intervening picks together, 300 times by default, so the board arriving at
your turn is a distribution instead of a guess. It then keeps simulating *past*
your pick to your following one.

```
VONA(pos) = E[best value at my pick] − E[best value at my following pick]

score     = (value + scarcity_weight × VONA) × need_factor
```

VONA is Value Over Next Available: what a position costs you if you spend this
pick elsewhere. `value` is VOR (points above positional replacement);
`need_factor` is 1.0 for a player filling an open starter slot and 0.55 for pure
depth. Both terms are in the same units, so adding them is meaningful. Candidates
below 15% survival are dropped — recommending someone who will be gone is
pointless.

#### This season's projections already drive it

`value` is VOR, and VOR is computed from `proj_points` — so the recommendation is
projection-led by construction. Set `scarcity=0` and the scores come back exactly
equal to VOR (190, 187, 157, 142 on a real board), which is the cleanest proof
that market rank contributes nothing to the value term.

`scarcity_weight` (API: `scarcity=`) controls how much the market-scarcity term
counts against projected value. Default 1.0 weights them equally, but projected
value is already the larger term at the top of the board — the best RB projects
~190 above replacement while the RB drop-off is ~73. Lower it to let projections
dominate further; raise it to chase positional runs harder.

Market rank is used for exactly two things, both correct: which order *opponents*
reach within a position (they follow the market, not my projections), and seeding
the candidate pool. That pool is the **union** of the top 320 by market rank and
the top 320 by projected points, so a player the projections love can never be
excluded for having a mediocre ADP — that disagreement is the whole point.

Players with no 2026 projection fall back to last season's scoring, and each such
suggestion is flagged on its own row rather than only in a league-wide warning.

#### Which projection supplies the value

The `Value from` toggle (API: `value=`) chooses the source behind `value`:

| Setting | `value` comes from | Basis |
|---|---|---|
| `Proj` | the rankings projections (RotoBaller/FantasyPros) | `vor` |
| `Vegas` | winwithodds season totals implied by betting markets | `vegas_vor` |
| `Blend` *(default)* | an even average of the two | `blend_vor` |

Blend is the default because two independent projections averaged beat either
alone often enough to be the honest starting point. **There is no backtest behind
the 50/50 split** — validating it would need historical winwithodds files, which
do not exist to download. It is a prior, not a fitted parameter, and it is one
constant (`VEGAS_BLEND_WEIGHT`) if you want to move it.

Blending is only arithmetic because `vegas_vor` was rescaled into `vor` units at
ingest; see [data/vegas/README.md](data/vegas/README.md). A player only one source
covers falls back to that source rather than being dropped.

With no file in `data/vegas/`, `Vegas` and `Blend` disable themselves and the
basis silently downgrades to `vor` — the toggle never claims a signal it lacks.

The panel leads with the positional drop-off, because that is the actual decision
content. On a real 12-team board at pick 4:

```
Cost of waiting a turn    RB −107    WR −80    TE −25    QB −3
```

The ranked list is capped at **2 candidates per position**. VONA is constant within
a position, so an unfiltered list is just the top six of whichever position has
the steepest drop-off — six running backs, telling you nothing actionable.

Respects the ECR/ADP toggle. Switching to ADP moved RB's drop-off from −104 to
−116 and WR's from −84 to −78, consistent with ESPN drafters taking backs earlier
than consensus. Runs in ~80ms at 300 trials, ~210ms at 1000, so it refreshes on
every pick. When you are on the clock, rows are clickable to draft.

#### Assumptions

Manager distributions are computed once from current rosters and are *not*
recomputed inside a trial, so over a one-to-two round horizon a manager can take
two of the same position slightly more often than they really would. Your own pick
at your turn is assumed to be the highest-value player available, so the
recommendation does not depend on itself.

**The value figures are only as good as the projections.** With no FPTS column in
any rankings file, VOR is built on last season's actual scoring — so players who
missed time are undervalued and 2025 breakouts overvalued. The panel says so in
plain text whenever that is the case.

### Survival odds

The survival model treats picks as independent and holds each player's board
rank fixed across the horizon. Neither is strictly true — surviving one pick
moves a player nearer the top of the board and makes him *more* exposed at the
next — so the odds are mildly optimistic for players deep in the horizon. Use
them to rank who is at risk, not as calibrated probabilities.

## Mock mode

Your leaguemates draft for themselves so you can practise from any seat. Setup tab
→ **Mock draft**: pick a seat, a board signal, and optionally a seed for a
repeatable run.

It is not a separate toy. Bots use the *same* model the Next picks panel projects
with, so a mock is a playthrough of the predictions you are already reading:

| Input | Effect on a bot's pick |
|---|---|
| Phase tendencies | shrunk position-share rates for that round's phase |
| Roster needs | a filled starter slot is downweighted to 0.35 |
| Board availability | a position with nobody left can't be picked; a bargain raises its odds |
| **Reach habit** | sets how sharply they prefer the top of a position |

That last one is where reach stops being decorative. A manager who historically
jumps the market gets a *flatter* within-position curve, so they are likelier to
skip the consensus best player and take their guy; a manager who waits gets a
sharper curve and takes the top of the board. Measured on this league, Enrique Puig
(median reach +0.5) draws decay 1.84 while Jad Winner (−3.5) draws 1.54.

Bot picks go into the same `picks` table as manual ones, so the board, grid, roster
panel, projections and suggestion box all work unmodified.

### Making bots draft like people

Reaches and randomness are wanted — a bot that always takes the consensus best
player is useless practice. What is not wanted is picks no human would make. Four
constraints, each added because a full-draft audit produced something absurd:

**Positional roster caps, hard.** `POSITION_ROSTER_CAP` = QB 3, RB 7, WR 8, TE 2,
K 1, DST 1, at weight **zero**, not a small number. A small-but-nonzero weight gets
resurrected by normalization: late in a draft when every position is capped, six
equal small weights renormalize to uniform and a bot ends up with **six kickers**.
Capacity totals 22 against 18 rounds, because caps that only just cover the rounds
force violations the moment randomness pushes a roster one way.

**Starter targets, not slot counts.** A position is a live need while
`held < dedicated + FLEX × share`, with FLEX split RB 45 / WR 45 / TE 10. In a
1-TE + 2-FLEX league the TE target is 1.2, so a *second* tight end is depth and a
third is impossible. The previous logic treated every flex-eligible position as a
starter need while any FLEX slot sat open — which is exactly how one manager took
Brock Bowers, Harold Fannin and Trey McBride back to back to back.

**A round floor from your own history.** Three coarse phases treat round 9 and
round 18 alike, so a 10% late-phase kicker rate put kickers off the board in round
7. Measured on this league, K has never gone before **R14** and DST before **R13**;
positions are zeroed before their observed earliest round. A must-fill always wins
over the floor.

**Endgame must-fill.** Once picks remaining ≤ unfilled mandatory slots, nothing but
those slots is a candidate. A 6× nudge was not enough — it still left about two
managers per mock without a kicker.

**Within-position depth scales with the round** (`2 + round`, capped at 14). A fixed
depth of 14 let a bot take the 10th-ranked tight end as the second TE off the board
in round 2 — a real tail of the rank-decay curve, but not something a human does.

Audited across four seeds, full 18-round drafts, every roster plausible:

```
MANAGER                QB RB WR TE  K DST   FIRST 4 PICKS
Enrique Puig            3  5  6  2  1   1   WR RB QB WR
Jad Winner              3  3  8  2  1   1   WR RB RB WR
Jake Cardillo           2  4  8  2  1   1   WR WR WR WR
Ivan Sung               2  7  5  2  1   1   RB RB RB WR

first off the board:  WR #1  RB #4  TE #9  QB #25  DST #148  K #161
```

Variety is preserved: across seeds the first TE goes anywhere from #3 to #37, and
builds range from RB-heavy to an 8-WR zero-RB shape. Deep picks still happen in the
right places — Kareem Hunt (ECR 327) in R15.

### The same bug was in the suggestion box

`suggest_pick` kept its **own copy** of the need logic, with the same flex bug and no
caps. So the box you follow on draft night would recommend a kicker in round 7 — an
empty K slot scores need 1.0 while your running backs sit at 0.35, and K's value
over replacement is enough to win. It now shares `_need_multipliers` and the round
floor, and capped positions are dropped from the list rather than shown with a zero
score.

Worth noting how this surfaced: the audit autopilots "my" picks from
`/api/suggest`, and the seat under test happened to be the one producing five
kickers. The mock bots were fine; the recommendation engine was not.

### Backtesting the model

```bash
.venv\Scripts\python.exe tools\backtest.py
.venv\Scripts\python.exe tools\backtest.py --tune --trials 48
```

Replays each real draft pick by pick and scores the distribution the model *would*
have predicted. Two design points make the number mean something:

**Teacher-forced.** The board at pick N is rebuilt from the *actual* picks 1..N-1,
not from what the model would have done. A free-running sim compounds its own
errors and measures drift instead of accuracy.

**Walk-forward, to avoid leakage.** To score season N, tendencies, reach profiles,
round floors and the depth curve are all fitted on seasons `< N` only. Fitting on
the season you are predicting leaks the answer, and a knob tuned against a leaked
score is tuned to memorise rather than forecast. The earliest season has nothing to
fit on and cannot be scored.

#### What it found immediately

The model was **worse than a naive baseline**. Position log-loss 1.7998 against
1.5410 for simply using the league's phase base rates.

The cause was my own hand-picked roster caps. Checked against reality, they were
factually wrong:

| Position | My cap | Actually observed | Manager-seasons exceeding my cap |
|---|---|---|---|
| TE | 2 | max **3** | 4 |
| K | 1 | max **2** | 2 |
| DST | 1 | max **3** | 5 |

A hard zero on something that really happened is a catastrophic log-loss, and it
was dragging the whole model below a dumb baseline. It also revealed that the
original complaint was subtler than I treated it: three TEs *across 18 rounds* is
real and happened four times; three TEs *with the first three picks* is not. The
problem was timing, and I had banned the total.

#### The fix: measure it instead of guessing

`roster_depth_curve` replaces caps, starter targets and depth penalties with one
measured quantity — P(take another at this position | already holds n), Laplace
smoothed so nothing is ever exactly zero:

```
QB   0->0.98  1->0.72  2->0.01
RB   0->0.98 ... 4->0.89  5->0.57  6->0.40  7->0.12
WR   0->0.98 ... 5->0.92  6->0.74  7->0.32  8->0.02
TE   0->0.98  1->0.58  2->0.16  3->0.05
K    0->0.98  1->0.05
DST  0->0.98  1->0.12  2->0.21
```

A second tight end is a coin flip, a third unusual, a second kicker close to
unheard of — the shape my hand-tuned penalty was trying and failing to express.

| | position log-loss | vs baseline | pos top-1 | player log-loss |
|---|---|---|---|---|
| Hand-picked caps | 1.7998 | **−0.26 worse** | 41.6% | 6.6247 |
| Measured depth curve | 1.4483 | **+0.09 better** | 44.1% | 6.3110 |
| + tuned `RANK_DECAY` | 1.4483 | +0.09 | 44.1% | **6.1210** |

`RANK_DECAY` was swept: 1.8 → 6.311, 2.5 → 6.179, 3.5 → 6.121, 5.0 → 6.106. The
minimum is flat past 3.5, but top-3 accuracy peaks at 3.5 (26.2%) and decays by 5.0
(24.7%) — beyond 3.5 the gain is pure hedging, which flatters log-loss and hurts a
simulator. Set to 3.5.

#### A negative result worth keeping

`BEST_AVAILABLE_SCALE` improves prediction *monotonically* as it grows, all the way
to 72 — where the bargain term is effectively switched off. As formulated, board
availability does not help predict real picks.

It is deliberately left at 24 rather than tuned to 72, because the backtest
reconstructs the available pool from players who were *drafted* that season, so it
understates the bargains genuinely on the board. The term may be sound and the
measurement wrong. Settling it needs a real preseason ranking of the full pool for
each season — see below.

#### Reaching for the right player, not just the right position

A reported symptom: tight ends going far too early — Trey McBride first overall,
Harold Fannin (TE6, ADP ~80) inside the first two rounds. Reproduced immediately:
**Fannin taken at pick 22 against an ADP of 80.8, a 59-pick reach.**

The gap was structural. The availability term asks whether a *position* is worth
taking by looking at its best remaining player. It cannot see which player is then
chosen — so once a position was selected, anyone still on its board was fair game.
Fannin was simply the best tight end left, and nothing asked whether pick 22 was
anywhere near ADP 80.

`player_reach_weight` adds the missing per-player check, and it is **scaled by
round**, because reach tolerance in real drafts is anything but constant:

| | median | p90 | max | ≥20 early |
|---|---|---|---|---|
| R1 | 0.2 | 3.1 | **4.0** | 0% |
| R2–3 | 1.7 | 8.0 | 19.7 | 0% |
| R4–8 | 2.6 | 20.3 | 37.8 | 11% |
| R9–13 | 13.8 | 53.3 | 132.3 | 44% |

Across 44 real round-1 picks the largest reach was **four picks**. Round one is
effectively locked to the market, and no flat penalty can express that alongside
round 13's tolerance of 50+. The scale is fitted to the p90 column, using the fact
that `exp(-2.3) ≈ 0.1` puts the 90th percentile near `2.3 × scale`.

Validated by comparing simulated reach distributions against real ones, rounds 1–3:

| | median | p90 | max | ≥20 early |
|---|---|---|---|---|
| real, all positions | 0.8 | 6.8 | 19.7 | 0.0% |
| simulated, before | 1.5 | 12.3 | 29.5 | 2.2% |
| **simulated, after** | **0.9** | **9.8** | 22.6 | **0.6%** |
| real, TE only | 0.8 | 3.8 | 4.0 | 0.0% |
| simulated TE, before | 7.0 | 24.4 | 29.5 | 20.0% |
| **simulated TE, after** | **3.3** | **10.3** | **12.3** | **0.0%** |

Accuracy improved alongside realism: player top-1 15.0% → 15.5%, top-3 34.4% →
35.9%, with position metrics unchanged.

Note that player log-loss *worsens* under this penalty (4.459 → 4.591). That is the
third time in this work that log-loss and accuracy have disagreed, always the same
way: softening a distribution flatters log-loss because hedging is never punished.
Where they conflict, the accuracy metrics and the measured reach distribution are
the ones that say whether a draft looks real.

#### Sample size honesty

592 out-of-sample picks across three seasons, tuning five knobs. Enough for coarse
calibration and for catching a sign error like the caps; not enough for fine
tuning. `FILLED_SLOT_PENALTY` scoring identically at 0.15 and 0.55 in the search is
a useful check that the depth curve genuinely supersedes it.

### Guarantees

**Simulation always stops at your turn.** `stop_at_my_turn` is unconditional in the
advance endpoint — the `picks` parameter only caps how far a run goes, it never
licenses drafting on your behalf. An early version honoured `picks` literally and
drafted Justin Jefferson for the user; a simulator that quietly makes your pick is
worse than useless.

**Manual picks can't be misattributed.** In mock mode `/api/pick` refuses a pick
while a bot is on the clock, so a stray click can't be recorded as an opponent's
selection.

**Starting a mock clears the board**, and says so before it does. Exiting mock mode
leaves the picks alone — reset separately if you want them gone.

After each of your picks the opponents roll forward automatically, so control comes
straight back to you. The **Sim to me** button in the top bar does the same on
demand and is a no-op when it is already your turn. The Mock draft panel logs every
simulated pick with the manager's reach, so a run is inspectable rather than a board
that silently changed.

## Vegas lines (exploration, not built yet)

The idea: sportsbook lines are the sharpest public forecast available and are
independent of fantasy analysts, so they are a genuine second opinion on
`proj_points` rather than a correlated one. Three tiers, best first:

| Tier | What it gives | Status on this plan |
|---|---|---|
| Season-long player props | lines convert straight into projected fantasy points | **not available** — game-level markets only |
| Implied team totals | offensive environment, derived from spreads + totals | available, but see the caveat below |
| Win totals | crude team-quality proxy | futures markets not on this plan |

**The caveat that matters.** Player props would have been an *uncorrelated* second
opinion on a player's projection. Team totals are not: any consensus projection
source already prices in expected team quality, so implied team totals largely
re-tell what `proj_points` already knows. Adding them as a projection multiplier
would mostly amplify existing bias while looking quantitative.

The defensible use is **disagreement detection**, not adjustment. Rank teams by
Vegas-implied scoring, rank the same teams by their skill players' summed
projections, and flag where the two disagree. Fantasy points are not team points,
so the comparison is rank-based to avoid a bogus unit conversion. A team Vegas
rates 3rd whose players project like the 8th offence is a place to look for
undervalued players — a flag for a human, not a number to multiply by.

A second caveat: game odds in August cover only the next week or two, and a single
game's implied total says as much about the opponent as about the team. Season-long
environment needs an average over enough games to wash that out, which is why the
first question is how far the posted schedule extends.

`ODDS_API_KEY` goes in `.env`. Then:

```bash
.venv\Scripts\python.exe tools\odds_probe.py
```

**The probe spends nothing by default.** The Odds API meters requests, and
per-event player-prop calls are the expensive kind, so anything that costs credits
is gated behind `--spend` and the tool prints `x-requests-remaining` after every
call. Free discovery covers the sports list (which reveals whether futures and
outright markets are on the plan) and the per-key event list.

One trap worth stating up front: taking a *weekly* player prop and multiplying by
17 is not a season projection. It ignores byes and injury risk, and weekly lines
are conditioned on that specific matchup. Only genuine season-long markets are
usable for draft prep.

## Next: surfacing this on the board

The engine and API are done; none of it is visible in the UI yet. Worth adding:

1. A tendency column or hover card per upcoming pick, showing likely positions.
2. Risk badges on player cards driven by `/api/survival`.
3. The scouting report on its own tab.
4. Owner-to-seat linking UI for managers whose names don't auto-match.

## Caveats

- **VOR needs a full rankings file.** If a rankings file has fewer players at a
  position than the replacement level, VOR is left null rather than reporting a
  meaningless zero, and the board shows projected points instead. See
  [How VOR is calculated](#how-vor-is-calculated).
- **Kicker points are computed, not read.** nflverse's `fantasy_points` ignores
  kicking entirely, so `app/ingest.py` scores FGs by distance band (3/4/5) plus
  PATs. Missed-kick penalties vary by league and are not applied.
- **Team defenses never match nflverse IDs.** `players.parquet` has no
  team-defense rows. DST comes from your rankings CSV alone, with no stats
  attached. This is expected, not a broken join.
- **Rebuilding the pool preserves picks where it can.** A rebuild replaces the
  `players` table, then restores every pick whose player is still in the new pool.
  Only a contiguous prefix can be restored — `overall` has to stay gapless or the
  whose-turn-is-it arithmetic breaks — so if a drafted player vanishes from the
  rankings, that pick and everything after it is dropped, and ingest says so.
  Imported league history is never touched by a rebuild.
- **Outcome value has a negative baseline, not zero.** `pos_finish` ranks against
  every player at the position, including undrafted breakouts who leapfrog
  drafted busts. So the league-wide mean sits around −1 slot. Judge a manager by
  `vs_league`, not by the raw number, or the whole league looks incompetent.
- **numpy scalars are adapted for SQLite in `db.py`.** They support the buffer
  protocol, so sqlite3 silently stores them as 8-byte BLOBs rather than
  rejecting them — corruption that only surfaces later when something reads the
  value back as a number. Do not remove those adapters.
- **ESPN's API is undocumented** and changes without notice. Snapshot your
  history CSVs early.
