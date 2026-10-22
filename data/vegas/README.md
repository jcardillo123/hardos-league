# Vegas-implied projections

Drop a **winwithodds.com** season-long projection CSV in this folder and rebuild.
Only the newest `.csv` here is read, so re-downloading over the old one just
works.

```bash
.venv/Scripts/python.exe -m app.ingest
```

## Why this is a separate folder

Everything in `data/rankings/` is **merged into one pool** — a base file defines
the board and the others fill its gaps. That is right for FantasyPros exports,
which are slices of a single opinion split across pages.

This is not a slice. It is a second, independent opinion, built from betting
markets rather than from analysts. Merging it into `data/rankings/` would average
the disagreement away, and the disagreement is the entire reason to have it.

## What gets read

| Column | Used as |
|---|---|
| `Name` | player, matched by normalized name |
| `Pos` | position — `DEF` is rewritten to `DST` |
| `Projections` | season fantasy points → `vegas_proj` |

Everything else in the export (the per-stat columns, `Rank`, `7-Day Delta`) is
ignored. `Rank` in particular is deliberately dropped: the board already has
three ranking orders (ECR, consensus ADP, ESPN ADP), and a fourth derived from
the projection adds nothing the projection itself doesn't say.

Team defences arrive as position `DEF` with the team abbreviation in the name
column (`LAR`), and are rewritten to the one spelling the rest of the database
uses (`LAR D/ST`).

## The rescale, and why it exists

The two projection sources do not share a scale. Measured on the 2026 pool,
winwithodds runs tighter in VOR terms, and by different amounts per position.
The factor below is what `vegas_vor` is **multiplied by** — `std(vor) /
std(vegas_vor_raw)`, so a value above 1.0 means the Vegas spread has to be
stretched to match:

| | QB | RB | WR | TE |
|---|---|---|---|---|
| multiplier to reach rankings scale | 1.04 | 1.24 | 1.09 | 1.10 |

Mind the direction. The reciprocal of each of these (0.96, 0.81, 0.92, 0.91) is
the equally true statement that Vegas is *tighter than* the rankings — but it is
not the number to multiply by, and an earlier version of this file quoted that
reciprocal as if it were. If the reported factor is ever below 1.0 while the
Vegas spread is visibly tighter, the fit is upside down.

Left alone, blending them would quietly reweight positions against each other —
a scoring-settings artifact wearing the costume of an opinion. So `vegas_vor` is
multiplied by **one global factor**, fitted from your actual pool at ingest and
reported when you rebuild:

```
vegas: 487 players matched from season_long_proj_table (1).csv
       (632 rows, 145 not on the board),
       VOR rescaled x1.1294 to match the rankings projections
```

Sanity-check it after a rebuild: divide the stored `vegas_vor` spread by the
reported factor and compare against the raw `Projections` column in the CSV. They
should agree within a point or two. When they don't, the scale is wrong and every
Edge number is wrong with it.

One factor, not per-position, is the point. Per-position rescaling would force
the two sources to agree about QB-vs-RB by construction and erase the genuine
cross-position disagreement — which is the signal worth having.

The factor is fitted on QB/RB/WR/TE only. Kickers differ by ~30% on scoring
alone between the two sources, so their "disagreement" would be mostly that.

## Where it shows up

- **Big board** — `Vegas` column (VOR from these projections) and `Edge`
  (Vegas VOR minus projection VOR). Green means the betting market is higher on
  him than the consensus projection is.
- **Sort menu** — `Vegas VOR`, `Vegas projected`, `Vegas vs projection`.
- **Suggested pick** — the `Value from` toggle: `Proj`, `Vegas`, or `Blend`.

## Reading the Edge column

The Edge sort deliberately skips two groups, because for them the difference
stops meaning anything:

- **Below replacement in both sources.** The rankings projections floor deep
  bench players near zero, while the Vegas model still hands them a share of a
  real team total. Their gap is a property of the two methods, not of the
  player. Sorted raw, third-string running backs bury every name you would
  actually draft.
- **K and DST**, per the scoring mismatch above.

Both still show their Edge value in the column — only the ordering treats them
as unrankable.

## No file here?

Everything degrades quietly and on purpose. `vegas_proj` and `vegas_vor` stay
NULL, the `Vegas` and `Edge` columns read `–`, the `Value from` toggle disables
its Vegas and Blend buttons, and suggestions score on the rankings projections
alone — exactly as they did before this existed.
