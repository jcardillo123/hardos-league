# Rankings CSVs

Drop FantasyPros CSV exports in this folder. **Multiple files are merged**, so you
want two of them.

## You need more than one export

FantasyPros splits what the board needs across separate pages:

| Export | Gives you | Missing |
|---|---|---|
| **Draft rankings (ECR)** | `RK`, `TIERS`, team, position, bye | projected points |
| **Projections** | `FPTS` — projected points for the *upcoming* season | rank and tiers |

Projections are published **one file per position** (QB, RB, WR, TE, K, DST).
Drop all of them in — each fills its own slice of `proj`, and ingest reports what
each contributed:

```
rankings [base] FantasyPros_2026_Draft_ALL_Rankings.csv: ecr, tier, bye
rankings [fills gaps] FantasyPros_Fantasy_Football_Projections_RB.csv: proj (+4)
rankings [fills gaps] FantasyPros_Fantasy_Football_Projections_WR.csv: proj (+4)
rankings [fills gaps] FantasyPros_Fantasy_Football_Projections_TE.csv: proj (+2)
```

Match the scoring format to your league on every projections download — a
standard-scoring projection will understate receivers badly in a PPR league.

With only the rankings export, `Projected` and `VOR` fall back to **last season's
actual scoring**. That is not a projection — it punishes anyone who was injured.
With the 2026 rankings alone, Justin Jefferson and CeeDee Lamb both showed VOR
around 30 (they missed most of 2025) while Christian McCaffrey topped the board.
Adding projections moved Jefferson to 149 and Lamb to 134, and dropped McCaffrey
to 143 — much closer to where consensus actually has them.

So: download **both**, drop both here, rebuild. Ingest prints which file supplied
what, and warns when projections are really last season in disguise.

## How merging works

The **newest file with a real ranking column** is the base. It defines the player
list and the ranking order. Other files may only *fill in blanks* — never
overwrite a value the base already has, and never supply `ecr`.

That rule matters in two ways. A projections export has no rank column, so one
gets synthesized from row order — the base rule makes sure that synthesized order
can never replace real expert consensus. And an old rankings export left in the
folder cannot quietly reshuffle the board, because `ecr` is never taken from a
secondary file.

Secondary files can supply: `proj` (FPTS), `adp`, `tier`, `bye`.

Then rebuild:

```bash
.venv\Scripts\python.exe -m app.ingest
```

Or hit **Rebuild pool** on the Setup tab.

## Where to export from

On FantasyPros, open the rankings page for your scoring format and use the
download / export link. Any of these work:

- **Draft rankings (ECR)** — best default. Gives rank, tier, team, position, bye.
- **Cheat sheet** — also fine; includes ADP.
- **Projections** — gives an `FPTS` column, which becomes the projection instead
  of last season's points.

Match the scoring format to your league (PPR / half / standard), and set the
same value on the Setup tab so value-over-replacement is computed consistently.

## Other sources work too

Nothing here is FantasyPros-specific — columns are detected, not assumed. A
RotoBaller "Overall PPR projected rankings" export drops in and supplies `PTS`
(projections) and `ADP (Average)` in one file.

Two things that file taught the loader:

- **`PTS` is a projection; `PTS (Stats)` is last season's actuals.** Any column
  containing `stats` is excluded outright, because importing that as a projection
  would silently reintroduce the exact backward-looking bug projections fix.
- **The largest ranked file wins the base, not the newest.** RotoBaller's 300-row
  file was newer than the 518-row FantasyPros file; newest-wins would have deleted
  200+ late-round players from the board.

### Where ADP ends up coming from

`adp` is filled from the rankings files first, then ESPN fills whatever gaps
remain. With a source like RotoBaller supplying 300 ADP values, ESPN contributes
almost nothing — so the projection's ADP signal becomes *that source's consensus
ADP*, not ESPN's platform ADP. Ingest prints the split so it is never ambiguous:

```
ADP: 303 players - 300 from the rankings files, 3 from ESPN 2026
     the ADP signal is therefore mostly your rankings file's consensus ADP,
     not ESPN's platform ADP
```

That matters if you were relying on ADP to model ESPN leaguemates specifically.

## Column detection

Columns are detected, not assumed, because exports differ per source and per page.
The loader handles:

- Preamble lines above the real header row
- `POS` values with the rank baked in (`WR1`, `RB12`, `TE3`, `K1`, `DST`)
- Name suffixes (`Jr.`, `III`) and punctuation (`Ja'Marr Chase`, `Amon-Ra St. Brown`)
- Team abbreviation disagreements (`JAC`/`JAX`, `WSH`/`WAS`, `LA`/`LAR`)
- Team defenses written as city + nickname (`Denver Broncos` → `DEN`)

It deliberately **ignores** comparative columns — `ECR VS. ADP`, `SOS SEASON`,
`Best`, `Worst`, `Std Dev` — so a differential never gets misread as an ADP.

If a name-bearing column can't be found in the first 8 lines, ingest stops and
prints the columns it saw. That usually means the file was saved as XLS rather
than CSV.

## Verifying the import

Ingest reports how many players matched an nflverse ID:

```
412 players written (source: rankings / FantasyPros_2026_Draft_ALL_Rankings.csv, scoring: ppr)
399 matched to nflverse ids, 13 unmatched
```

A handful of unmatched rows is normal — team defenses never match (nflverse has
no team-defense entries), and so do undrafted rookies not yet in the player
table. Unmatched players still appear on the board using their ranking data;
they just carry no stats. If *most* rows are unmatched, the position column
probably failed to parse.
