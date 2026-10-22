"""Data ingest: nflverse stats, FantasyPros rankings, ESPN league history.

Everything lands in the local SQLite `players` table, which the draft board
reads. Re-running is safe and idempotent.

    python -m app.ingest              # rebuild the player pool
    python -m app.ingest --refresh    # re-download nflverse parquet first
"""

import argparse
import json
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from . import db
from .config import (
    ADP_DIR, CACHE, FLEX_POSITIONS, LAST_SEASON, NFLVERSE, RANKINGS_DIR,
    VEGAS_DIR,
)

NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}

# Sources disagree on a handful of names, and every disagreement silently drops a
# player from a join. Canonicalized to nflverse's spelling, because nflverse is the
# stats backbone that both rankings and ESPN history have to reach.
#
# Worked example: FantasyPros says "Hollywood Brown", RotoBaller and nflverse say
# "Marquise Brown". With FantasyPros as the pool base, that one name matched
# neither the stats join nor the projections join.
NAME_ALIASES = {
    "kenneth gainwell": "kenny gainwell",
    "ken gainwell": "kenny gainwell",
    "hollywood brown": "marquise brown",
}

# FantasyPros writes team defenses as nicknames; nflverse uses abbreviations.
NICKNAME_TO_ABBR = {
    "cardinals": "ARI", "falcons": "ATL", "ravens": "BAL", "bills": "BUF",
    "panthers": "CAR", "bears": "CHI", "bengals": "CIN", "browns": "CLE",
    "cowboys": "DAL", "broncos": "DEN", "lions": "DET", "packers": "GB",
    "texans": "HOU", "colts": "IND", "jaguars": "JAX", "chiefs": "KC",
    "raiders": "LV", "chargers": "LAC", "rams": "LAR", "dolphins": "MIA",
    "vikings": "MIN", "patriots": "NE", "saints": "NO", "giants": "NYG",
    "jets": "NYJ", "eagles": "PHI", "steelers": "PIT", "49ers": "SF",
    "seahawks": "SEA", "buccaneers": "TB", "titans": "TEN", "commanders": "WAS",
}

# Normalize the various abbreviations these sources disagree on.
TEAM_ALIASES = {
    "JAC": "JAX", "WSH": "WAS", "LA": "LAR", "OAK": "LV", "SD": "LAC",
    "STL": "LAR", "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU",
}


# --------------------------------------------------------------------------- #
# name / team normalization
# --------------------------------------------------------------------------- #

def norm_name(value) -> str:
    """Fold a player name to a comparable key: ascii, lowercase, no suffix."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " ")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    parts = [p for p in text.split() if p and p not in NAME_SUFFIXES]
    joined = " ".join(parts)
    return NAME_ALIASES.get(joined, joined)


def norm_team(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().upper()
    if not text or text in {"FA", "NAN", "NONE", "-"}:
        return None
    return TEAM_ALIASES.get(text, text)


def normalize_position(value) -> str | None:
    """Map source-specific position labels onto QB/RB/WR/TE/K/DST."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = re.sub(r"[^A-Z/]", "", str(value).upper())
    if text.startswith(("DST", "D/ST", "DEF")):
        return "DST"
    for pos in ("QB", "RB", "WR", "TE"):
        if text.startswith(pos):
            return pos
    if text.startswith("PK") or text == "K":
        return "K"
    return None


def player_key(name: str, position: str) -> str:
    return f"{norm_name(name)}|{position}"


def _py(value):
    """Convert a pandas/numpy scalar to a plain Python value for SQLite."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value.item() if hasattr(value, "item") else value


# --------------------------------------------------------------------------- #
# nflverse
# --------------------------------------------------------------------------- #

def cached_parquet(release: str, asset: str, refresh: bool = False) -> pd.DataFrame:
    """Download an nflverse release asset once, then read it from disk."""
    local = CACHE / asset
    if refresh or not local.exists():
        url = f"{NFLVERSE}/{release}/{asset}"
        print(f"  downloading {asset}", file=sys.stderr)
        req = Request(url, headers={"User-Agent": "draft-tool"})
        with urlopen(req, timeout=120) as resp:
            local.write_bytes(resp.read())
    return pd.read_parquet(local)


def load_crosswalk(refresh: bool = False) -> pd.DataFrame:
    """One row per player with the IDs needed to join every other source."""
    df = cached_parquet("players", "players.parquet", refresh)
    keep = [
        "gsis_id", "display_name", "position", "latest_team", "status",
        "birth_date", "rookie_season", "last_season", "espn_id", "pfr_id",
    ]
    df = df[[c for c in keep if c in df.columns]].copy()
    df["position"] = df["position"].map(normalize_position)
    df["latest_team"] = df["latest_team"].map(norm_team)
    df["name_key"] = df["display_name"].map(norm_name)
    df["age"] = _age_from_birth(df.get("birth_date"))
    return df


def _age_from_birth(series) -> pd.Series:
    if series is None:
        return pd.Series(dtype="float64")
    born = pd.to_datetime(series, errors="coerce")
    today = pd.Timestamp(date.today())
    return ((today - born).dt.days / 365.25).round(1)


def load_season_stats(season: int, refresh: bool = False) -> pd.DataFrame:
    """Regular-season totals for one season, with all three scoring variants."""
    df = cached_parquet(
        "stats_player", f"stats_player_reg_{season}.parquet", refresh
    )
    cols = {
        "player_id": "gsis_id",
        "player_display_name": "display_name",
        "position": "position",
        "recent_team": "team",
        "games": "games_last",
        "fantasy_points": "pts_standard",
        "fantasy_points_ppr": "pts_ppr",
        "receptions": "receptions",
        "target_share": "target_share",
        "wopr": "wopr",
    }
    have = {k: v for k, v in cols.items() if k in df.columns}
    out = df[list(have)].rename(columns=have)
    for col in ("pts_standard", "pts_ppr", "receptions", "games_last"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
    out["pts_half"] = out["pts_standard"] + 0.5 * out.get("receptions", 0.0)

    # nflverse's fantasy_points columns ignore kicking entirely, so every K
    # comes back near zero. Score them from the FG/PAT detail instead.
    kicking = _kicker_points(df)
    is_k = out["position"].isin(("K", "PK"))
    for col in ("pts_standard", "pts_ppr", "pts_half"):
        out.loc[is_k, col] = kicking.loc[is_k]
    out["position"] = out["position"].map(normalize_position)
    out["team"] = out["team"].map(norm_team)
    out["name_key"] = out["display_name"].map(norm_name)
    return out


# Distance-banded field goal values, the near-universal default.
FG_VALUES = {
    "fg_made_0_19": 3, "fg_made_20_29": 3, "fg_made_30_39": 3,
    "fg_made_40_49": 4, "fg_made_50_59": 5, "fg_made_60_": 5,
}


def _kicker_points(df: pd.DataFrame) -> pd.Series:
    """FG by distance band + PATs. Missed kicks are ignored (league-dependent)."""
    total = pd.Series(0.0, index=df.index)
    for col, value in FG_VALUES.items():
        if col in df.columns:
            total += pd.to_numeric(df[col], errors="coerce").fillna(0.0) * value
    if "pat_made" in df.columns:
        total += pd.to_numeric(df["pat_made"], errors="coerce").fillna(0.0)
    return total.round(1)


def load_snap_share(season: int, refresh: bool = False) -> pd.DataFrame:
    """Mean offensive snap share per player for the season, keyed by pfr id."""
    df = cached_parquet("snap_counts", f"snap_counts_{season}.parquet", refresh)
    df = df[df["game_type"] == "REG"] if "game_type" in df.columns else df
    grouped = (
        df.groupby("pfr_player_id", dropna=True)["offense_pct"]
        .mean()
        .reset_index()
        .rename(columns={"pfr_player_id": "pfr_id", "offense_pct": "snap_pct"})
    )
    # nflverse stores these as 0-1 in some seasons and 0-100 in others.
    if grouped["snap_pct"].max(skipna=True) is not None and grouped["snap_pct"].max() <= 1.5:
        grouped["snap_pct"] = grouped["snap_pct"] * 100
    grouped["snap_pct"] = grouped["snap_pct"].round(1)
    return grouped


def load_byes(season: int, refresh: bool = False) -> dict[str, int]:
    """Derive each team's bye week from the schedule. Empty dict if unpublished."""
    try:
        games = cached_parquet("schedules", "games.parquet", refresh)
    except Exception as exc:                                  # noqa: BLE001
        print(f"  bye weeks unavailable: {exc}", file=sys.stderr)
        return {}
    games = games[(games["season"] == season) & (games["game_type"] == "REG")]
    if games.empty:
        return {}
    byes: dict[str, int] = {}
    weeks = set(games["week"].dropna().astype(int))
    for team in set(games["home_team"]) | set(games["away_team"]):
        played = set(
            games.loc[
                (games["home_team"] == team) | (games["away_team"] == team), "week"
            ].dropna().astype(int)
        )
        missing = sorted(weeks - played)
        if len(missing) == 1:
            byes[norm_team(team)] = missing[0]
    return byes


# --------------------------------------------------------------------------- #
# FantasyPros rankings CSV
# --------------------------------------------------------------------------- #

RANK_COLUMN_HINTS = {
    "name": ("player name", "player", "playername", "name"),
    "position": ("pos", "position"),
    "team": ("team", "tm"),
    "ecr": ("rk", "rank", "ecr", "avg", "overall"),
    "tier": ("tiers", "tier"),
    "adp": ("adp", "avg adp", "average draft position"),
    "bye": ("bye week", "bye"),
    "proj": ("fpts", "pts", "proj fpts", "projected points", "points",
             "fantasy points"),
}

# Columns that must never be matched to any field:
#   vs/diff/delta   FantasyPros ships a differential literally named "ECR VS. ADP",
#                   and a substring match would read that as ADP.
#   stats           RotoBaller suffixes LAST season's actuals with "(Stats)" -
#                   "PTS (Stats)" sits right next to the projection "PTS", and
#                   importing it as a projection would reintroduce exactly the
#                   backward-looking bug projections are meant to fix.
#   sos/best/worst  strength-of-schedule and rank spreads, never a value we want.
EXCLUDE_SUBSTRINGS = (
    "vs", "diff", "delta", "sos", "std dev", "best", "worst", "stats",
)


def _match_column(columns: list[str], hints: tuple[str, ...]) -> str | None:
    cleaned = {
        c: re.sub(r"[^a-z ]", " ", c.strip().lower()).strip()
        for c in columns
    }
    cleaned = {
        c: s for c, s in cleaned.items()
        if not any(bad in s for bad in EXCLUDE_SUBSTRINGS)
    }
    for hint in hints:                       # exact match wins
        for original, simple in cleaned.items():
            if simple == hint:
                return original
    for hint in hints:                       # then whole-word containment
        for original, simple in cleaned.items():
            if hint in simple.split():
                return original
    for hint in hints:                       # finally loose containment
        for original, simple in cleaned.items():
            if hint in simple:
                return original
    return None


def _read_rankings_csv(path: Path) -> pd.DataFrame:
    """Read a rankings CSV, tolerating preamble rows above the real header."""
    for skip in range(0, 8):
        try:
            df = pd.read_csv(path, skiprows=skip, skip_blank_lines=True)
        except Exception:                                      # noqa: BLE001
            continue
        columns = [str(c).strip() for c in df.columns]
        if _match_column(columns, RANK_COLUMN_HINTS["name"]) and len(df):
            df.columns = columns
            return df
    raise SystemExit(
        f"{path.name}: no header row with a player-name column found in the "
        "first 8 lines. Re-export from FantasyPros as CSV."
    )


def find_rankings_file() -> Path | None:
    """Newest CSV in data/rankings/ wins, so re-exporting just works."""
    files = sorted(
        RANKINGS_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return files[0] if files else None


# Columns a secondary file is allowed to supply. Deliberately excludes `ecr`:
# ranking order must come from one source, or a stale export silently reshuffles
# the board.
SECONDARY_COLUMNS = ("proj", "adp", "tier", "bye")


def load_all_rankings() -> tuple[pd.DataFrame | None, list[dict]]:
    """Merge every CSV in data/rankings/ into one pool.

    FantasyPros splits what we need across separate exports: the draft rankings
    page has RK and TIERS but no projected points, while the projections page has
    FPTS. Without projections, `proj_points` falls back to last season's actual
    scoring — which makes VOR backward-looking and wrong for anyone who was hurt.

    The newest file that carries a ranking is the base and defines both the player
    list and the ranking order. Other files may only fill columns the base has
    *entirely* empty. That way dropping in a projections export adds FPTS, but an
    old rankings export left in the folder cannot quietly reorder the board.
    """
    files = sorted(
        RANKINGS_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    parsed = []
    for path in files:
        try:
            parsed.append((path, load_rankings(path)))
        except SystemExit as exc:
            print(f"  skipping {path.name}: {exc}", file=sys.stderr)
        except Exception as exc:                                # noqa: BLE001
            print(f"  skipping {path.name}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
    if not parsed:
        return None, []

    # Base selection, in priority order:
    #
    # 1. Must have a real ranking column. A projections export has none, so ECR
    #    gets synthesized from row order - letting that define the board would
    #    replace expert consensus with whatever order the file was sorted in.
    # 2. Then the LARGEST player pool, because the base defines which players
    #    exist. Newest-wins was wrong here: a 300-row projected-rankings file
    #    that happens to be newer than a 518-row draft-rankings file would
    #    silently delete 200+ late-round players from the board.
    # 3. Newest only as a tie-break, so re-exporting the same source still works.
    ranked = [p for p in parsed if not p[1].attrs.get("ecr_synthetic")]
    if ranked:
        ranked.sort(key=lambda pair: (-len(pair[1]), -pair[0].stat().st_mtime))
        parsed = ranked + [p for p in parsed if p not in ranked]

    base_path, base = parsed[0]
    base = base.copy()
    report = [{
        "file": base_path.name,
        "role": "base" + ("" if ranked else " (no real ranking column found)"),
        "rows": len(base),
        "provides": [
            c for c in ("ecr", "tier", "adp", "bye", "proj")
            if c in base.columns and base[c].notna().any()
        ],
    }]

    # Every secondary file may fill NULLs in these columns, and stays eligible as
    # long as NULLs remain. Filling is `fillna`, so a base that already owns a
    # column is untouched regardless - which is what protects `ecr`, excluded from
    # SECONDARY_COLUMNS entirely.
    #
    # This must not narrow after the first contribution: FantasyPros publishes
    # projections one file PER POSITION, so six separate exports each fill a
    # different slice of `proj`. Dropping `proj` from the eligible set as soon as
    # any value landed would have let only the first position through.
    for path, extra in parsed[1:]:
        gaps = [
            c for c in SECONDARY_COLUMNS
            if c not in base.columns or base[c].isna().any()
        ]
        usable = [c for c in gaps if c in extra.columns and extra[c].notna().any()]
        if not usable:
            report.append({
                "file": path.name, "role": "nothing new to add",
                "rows": len(extra), "provides": [],
            })
            continue
        right = _dedupe(extra, ["name_key", "position"], "ecr")
        right = right[["name_key", "position"] + usable]
        merged = base.merge(
            right, on=["name_key", "position"], how="left", suffixes=("", "_add")
        )
        filled = {}
        for column in usable:
            source = f"{column}_add"
            if source not in merged.columns:
                continue
            before = int(merged[column].notna().sum()) if column in merged else 0
            merged[column] = (
                merged[column].fillna(merged[source])
                if column in merged else merged[source]
            )
            filled[column] = int(merged[column].notna().sum()) - before
            merged = merged.drop(columns=[source])
        base = merged
        # Rows in a secondary file that match nothing in the base contribute
        # nothing and vanish without trace. Almost always a name spelling the
        # sources disagree on, so name them - a silent drop is how a projection
        # goes missing for a player you care about.
        base_keys = set(zip(base["name_key"], base["position"]))
        unmatched = [
            f"{r.name} ({r.position})"
            for r in extra.itertuples(index=False)
            if (r.name_key, r.position) not in base_keys
        ]
        report.append({
            "file": path.name,
            "role": "fills gaps",
            "rows": len(extra),
            "provides": [f"{k} (+{v})" for k, v in filled.items() if v],
            "unmatched": unmatched,
        })

    return base, report


# --------------------------------------------------------------------------- #
# Vegas-implied projections (winwithodds.com)
# --------------------------------------------------------------------------- #

VEGAS_COLUMN_HINTS = {
    "name": ("name", "player name", "player"),
    "position": ("pos", "position"),
    # The season-point total. "Projections" is what winwithodds calls it; the
    # generic hints are there so a re-export with a renamed column still lands.
    "proj": ("projections", "projection", "fpts", "points", "proj"),
}


def find_vegas_file() -> Path | None:
    """Newest CSV in data/vegas/ wins, so re-downloading just works."""
    files = sorted(
        VEGAS_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return files[0] if files else None


def load_vegas_projections(path: Path | None = None) -> pd.DataFrame | None:
    """Parse a winwithodds season-long projection export.

    Returns `name_key`, `position`, `vegas_proj` - deliberately nothing else.
    The export also carries a `Rank` column, but importing it would give the
    board a fourth ranking order competing with ECR for no benefit: the value
    here is the projection, and a rank is downstream of it anyway.

    Team defences arrive as position `DEF` with the team abbreviation in the
    name column ("LAR"), so they route through `canonical_dst_name` to reach the
    one spelling the rest of the database uses.
    """
    path = path or find_vegas_file()
    if path is None:
        return None
    df = _read_rankings_csv(path)
    columns = list(df.columns)
    found = {
        field: _match_column(columns, hints)
        for field, hints in VEGAS_COLUMN_HINTS.items()
    }
    missing = [f for f, c in found.items() if c is None]
    if missing:
        raise SystemExit(
            f"{path.name}: could not find column(s) for {', '.join(missing)}. "
            f"Header was: {', '.join(columns)}"
        )

    out = pd.DataFrame({
        "name": df[found["name"]].astype(str).str.strip(),
        "position": df[found["position"]].astype(str).str.strip().str.upper(),
        "vegas_proj": pd.to_numeric(df[found["proj"]], errors="coerce"),
    })
    out = out[out["vegas_proj"].notna()]
    out["position"] = out["position"].replace({"DEF": "DST", "D/ST": "DST"})
    dst = out["position"] == "DST"
    out.loc[dst, "name"] = out.loc[dst, "name"].map(canonical_dst_name)
    out = out[out["name"].notna()]
    out["name_key"] = out["name"].map(norm_name)
    out = _dedupe(out, ["name_key", "position"], "vegas_proj")
    out = out[["name_key", "position", "vegas_proj"]]
    out.attrs["file"] = path.name
    return out


# The two projection sources do not share a scale: winwithodds runs roughly 10%
# tighter than the rankings projections in VOR terms, and by different amounts
# per position (measured: QB .98, RB .81, WR .92, TE .97 on the 2026 pool).
# Left alone, blending them would quietly reweight positions against each other -
# a scoring-settings artifact masquerading as an opinion. So Vegas VOR is
# rescaled by ONE global factor, fitted from the data rather than hardcoded.
# A single factor is the point: per-position rescaling would erase the genuine
# cross-position disagreement, which is the signal worth having.
VEGAS_SCALE_POSITIONS = ("QB", "RB", "WR", "TE")


def vegas_scale_factor(pool: pd.DataFrame) -> float:
    """Factor putting `vegas_vor` into the same units as `vor`."""
    num = den = 0.0
    for pos in VEGAS_SCALE_POSITIONS:
        mask = (
            (pool["position"] == pos)
            & pool["vor"].notna()
            & pool["vegas_vor_raw"].notna()
        )
        both = pool.loc[mask]
        if len(both) < 10:
            continue
        primary = pd.to_numeric(both["vor"], errors="coerce").astype(float).std()
        vegas = pd.to_numeric(
            both["vegas_vor_raw"], errors="coerce"
        ).astype(float).std()
        if not primary or not vegas or primary <= 0:
            continue
        num += (primary / vegas) * len(both)
        den += len(both)
    return round(num / den, 4) if den else 1.0


def load_rankings(path: Path) -> pd.DataFrame:
    """Parse a FantasyPros export into a normalized frame.

    Their exports vary by page (ECR, cheat sheet, projections), so columns are
    detected rather than assumed.
    """
    raw = _read_rankings_csv(path)
    cols = list(raw.columns)

    mapping = {
        field: _match_column(cols, hints)
        for field, hints in RANK_COLUMN_HINTS.items()
    }
    if not mapping["name"]:
        raise SystemExit(
            f"{path.name}: could not find a player-name column. Columns seen: {cols}"
        )

    out = pd.DataFrame()
    out["name"] = raw[mapping["name"]].astype(str).str.strip()

    # FantasyPros sometimes packs "Name TEAM POS" into one cell.
    packed = out["name"].str.extract(r"^(?P<n>.+?)\s+\(?([A-Z]{2,3})\)?$")
    if mapping["team"]:
        out["team"] = raw[mapping["team"]].map(norm_team)
    else:
        out["team"] = packed[1].map(norm_team)
        out["name"] = packed["n"].fillna(out["name"])

    out["position"] = (
        raw[mapping["position"]].map(normalize_position)
        if mapping["position"] else None
    )
    for field in ("ecr", "tier", "adp", "bye", "proj"):
        col = mapping[field]
        out[field] = (
            pd.to_numeric(
                raw[col].astype(str).str.replace(r"[^0-9.\-]", "", regex=True),
                errors="coerce",
            )
            if col else pd.NA
        )

    # Position may be embedded in a "pos rank" style cell like "RB4".
    if out["position"].isna().all() and mapping["position"]:
        out["position"] = raw[mapping["position"]].map(normalize_position)

    out = out[out["name"].str.len() > 1]
    out["name_key"] = out["name"].map(norm_name)

    # Team defenses: resolve nickname -> abbreviation, then rewrite the name into the
    # one canonical form so they join against history and ADP.
    is_dst = out["position"] == "DST"
    if is_dst.any():
        nick = out.loc[is_dst, "name_key"].str.split().str[-1]
        resolved = nick.map(NICKNAME_TO_ABBR)
        out.loc[is_dst, "team"] = out.loc[is_dst, "team"].fillna(resolved)
        canonical = out.loc[is_dst, "team"].map(canonical_dst_name)
        out.loc[is_dst, "name"] = canonical.fillna(out.loc[is_dst, "name"])
        out.loc[is_dst, "name_key"] = out.loc[is_dst, "name"].map(norm_name)

    out = out.dropna(subset=["position"])

    # A projections export has no rank column, so ECR gets synthesized from row
    # order. That is fine as a fallback but must never be mistaken for a real
    # ranking - flag it so base selection can prefer a genuinely ranked file.
    synthetic = bool(out["ecr"].isna().all())
    if synthetic:
        out["ecr"] = range(1, len(out) + 1)
    out = out.sort_values("ecr", na_position="last").reset_index(drop=True)
    out.attrs["ecr_synthetic"] = synthetic
    return out


# --------------------------------------------------------------------------- #
# ESPN ADP (tools/espn_adp.py output)
# --------------------------------------------------------------------------- #

# ESPN reports an "effectively undrafted" ceiling rather than a null: exactly
# 170.0 plus a cluster of 169.9x values. Measured across 2022-2026, every real
# ADP sits below 169, so anything at or above it carries no information.
ADP_UNDRAFTED_CEILING = 169.0


def load_espn_adp(season: int) -> pd.DataFrame | None:
    """Real ADP values from a tools/espn_adp.py export, or None if unusable.

    ESPN does not serve ADP for every season. For 2025 it returned exactly 170.0
    for all 600 players - a single distinct value, i.e. no data at all wearing the
    shape of data. That case is rejected rather than imported as a flat column
    that would silently make every player look equally valued.
    """
    path = ADP_DIR / f"espn_adp_{season}.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    if "espn_adp" not in df.columns:
        return None

    df["adp"] = pd.to_numeric(df["espn_adp"], errors="coerce")
    df = df[df["adp"].notna() & (df["adp"] < ADP_UNDRAFTED_CEILING)]
    if df["adp"].nunique() <= 1:
        print(
            f"  ESPN ADP for {season} has no usable spread - ignoring",
            file=sys.stderr,
        )
        return None

    df["espn_key"] = df["espn_player_id"].map(
        lambda v: str(int(float(v))) if pd.notna(v) else None
    )
    df["position"] = df["position"].map(normalize_position)
    df["name_key"] = df["player_name"].map(norm_name)
    return df[["espn_key", "name_key", "position", "adp"]]


def apply_espn_adp(pool: pd.DataFrame, season: int) -> dict:
    """Write ESPN's platform ADP into its own `espn_adp` column.

    Deliberately NOT merged into `adp`. Consensus ADP from a rankings file and
    ESPN's platform ADP are different measurements over different player
    populations, and gap-filling one from the other produced a column that was
    mostly consensus with a few ESPN values mixed in - which then silently changed
    what the projection's "ADP" signal meant. Kept apart, each can be selected as
    its own signal: consensus ADP is the broad market, ESPN ADP is specifically
    where players go in the league you actually draft in.
    """
    stats = {"season": season, "by_id": 0, "by_name": 0, "available": 0}
    if pool is None:
        return stats
    pool["espn_adp"] = pd.NA
    adp = load_espn_adp(season)
    if adp is None:
        return stats
    stats["available"] = len(adp)
    pool["espn_adp"] = pd.to_numeric(pool["espn_adp"], errors="coerce")

    # Pass 1: ESPN player id, carried on the pool by the nflverse crosswalk.
    by_id = adp.dropna(subset=["espn_key"]).drop_duplicates("espn_key")
    id_map = dict(zip(by_id["espn_key"], by_id["adp"]))
    pool_keys = pool["espn_id"].map(
        lambda v: str(int(float(v))) if pd.notna(v) else None
    )
    pool["espn_adp"] = pool["espn_adp"].fillna(pool_keys.map(id_map))
    stats["by_id"] = int(pool["espn_adp"].notna().sum())

    # Pass 2: name + position, which is how team defenses and anyone missing an
    # espn_id get matched.
    by_name = _dedupe(adp, ["name_key", "position"], "adp")
    name_map = {
        (r.name_key, r.position): r.adp for r in by_name.itertuples(index=False)
    }
    before = int(pool["espn_adp"].notna().sum())
    fallback = pd.Series(
        [name_map.get((nk, pos)) for nk, pos in zip(pool["name_key"], pool["position"])],
        index=pool.index,
        dtype="float64",
    )
    pool["espn_adp"] = pool["espn_adp"].fillna(fallback)
    stats["by_name"] = int(pool["espn_adp"].notna().sum()) - before
    return stats


# --------------------------------------------------------------------------- #
# historical preseason ADP (FantasyPros "Overall ADP Rankings" exports)
# --------------------------------------------------------------------------- #

# FantasyPros packs name, team and bye into one cell, and does it three different
# ways:  "Jonathan Taylor   IND (14)"  |  "Denver Broncos DST   (12)"  |  "Tyreek Hill"
FP_BYE_RE = re.compile(r"\((\d+)\)\s*$")
FP_TEAM_RE = re.compile(r"^[A-Z]{2,3}$")
# Files carry a handful of IDP stragglers (CB, DT, LB, OL) that are not draftable
# here; normalize_position returns None for them and they are dropped.
FP_ADP_SOURCES = ("ESPN", "Sleeper", "CBS", "NFL", "RTSports", "Fantrax")


def _split_fp_player(cell, position: str | None):
    """Pull name, team and bye out of a FantasyPros "Player (Bye)" cell."""
    text = str(cell or "").strip()
    bye = None
    match = FP_BYE_RE.search(text)
    if match:
        bye = int(match.group(1))
        text = text[: match.start()].strip()

    if position == "DST":
        # "Denver Broncos DST" - no team code, so resolve from the nickname.
        text = re.sub(r"\s*D/?ST\s*$", "", text).strip()
        parts = norm_name(text).split()
        team = NICKNAME_TO_ABBR.get(parts[-1]) if parts else None
        return canonical_dst_name(team) or text, team, bye

    parts = text.split()
    if len(parts) >= 2 and FP_TEAM_RE.fullmatch(parts[-1]):
        return " ".join(parts[:-1]).strip(), norm_team(parts[-1]), bye
    return text, None, bye


def load_fantasypros_adp(path: Path, season: int) -> pd.DataFrame:
    """Parse one FantasyPros Overall ADP export into normalized rows."""
    raw = pd.read_csv(path)
    raw.columns = [str(c).strip() for c in raw.columns]

    player_col = next(
        (c for c in raw.columns if c.lower().startswith("player")), None
    )
    if player_col is None:
        raise SystemExit(f"{path.name}: no Player column. Columns: {list(raw.columns)}")
    pos_col = next((c for c in raw.columns if c.strip().upper() == "POS"), None)
    avg_col = next((c for c in raw.columns if c.strip().upper() == "AVG"), None)
    rank_col = next((c for c in raw.columns if c.strip().upper() == "RANK"), None)

    positions = (
        raw[pos_col].map(normalize_position) if pos_col else pd.Series(None, index=raw.index)
    )

    records = []
    for idx, position in positions.items():
        # `is None` alone is not enough: pandas stores a missing mapping result as
        # NaN, which is not None, so IDP strays (CB, DT, LB, OL) slipped through and
        # hit the NOT NULL constraint on insert.
        if position is None or pd.isna(position):
            continue
        name, team, bye = _split_fp_player(raw.at[idx, player_col], position)
        if not name:
            continue
        adp = pd.to_numeric(raw.at[idx, avg_col], errors="coerce") if avg_col else None
        rank = pd.to_numeric(raw.at[idx, rank_col], errors="coerce") if rank_col else None
        espn = None
        if "ESPN" in raw.columns:
            espn = pd.to_numeric(raw.at[idx, "ESPN"], errors="coerce")
        records.append({
            "season": season,
            "name_key": norm_name(name),
            "position": position,
            "player_name": name,
            "team": team,
            "bye": bye,
            "rank": rank,
            "adp": adp,
            "espn_adp": espn,
            "source": path.name,
        })

    df = pd.DataFrame(records)
    if df.empty:
        return df
    # One row per player: keep the best (lowest) ADP if a name repeats.
    df = df.sort_values("adp", na_position="last")
    return df.drop_duplicates(subset=["season", "name_key", "position"], keep="first")


ADP_HISTORY_FIELDS = (
    "season", "name_key", "position", "player_name", "team", "bye",
    "rank", "adp", "espn_adp", "source",
)
FP_ADP_FILE_RE = re.compile(r"(20\d\d)", re.I)


def import_adp_history() -> dict:
    """Load every FantasyPros Overall ADP export found in data/adp/."""
    files = sorted(ADP_DIR.glob("*.csv"))
    report = {"seasons": {}, "rows": 0}
    frames = []
    for path in files:
        name = path.name.lower()
        if "fantasypros" not in name or "adp" not in name:
            continue                     # skip our own espn_adp_*.csv exports
        match = FP_ADP_FILE_RE.search(path.name)
        if not match:
            print(f"  skipping {path.name}: no season in filename", file=sys.stderr)
            continue
        season = int(match.group(1))
        try:
            df = load_fantasypros_adp(path, season)
        except Exception as exc:                                # noqa: BLE001
            print(f"  skipping {path.name}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue
        if df.empty:
            continue
        frames.append(df)
        report["seasons"][season] = {
            "file": path.name,
            "players": len(df),
            "with_adp": int(df["adp"].notna().sum()),
            "with_espn": int(df["espn_adp"].notna().sum()),
            "max_adp": float(df["adp"].max()) if df["adp"].notna().any() else None,
            "by_position": df["position"].value_counts().to_dict(),
        }

    if not frames:
        return report

    combined = pd.concat(frames, ignore_index=True)
    rows = [
        tuple(_py(v) for v in rec)
        for rec in combined[list(ADP_HISTORY_FIELDS)].itertuples(index=False, name=None)
    ]
    with db.session() as conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO adp_history "
            f"({','.join(ADP_HISTORY_FIELDS)}) "
            f"VALUES ({','.join('?' * len(ADP_HISTORY_FIELDS))})",
            rows,
        )
    report["rows"] = len(rows)
    return report


def stamp_history_from_adp() -> dict:
    """Attach consensus ADP from adp_history onto each historical pick.

    Gives one ranking scale across every season, which `espn_draft_rank` cannot -
    it has no 2022 data and ESPN's own ADP is missing for 2025.
    """
    with db.session() as conn:
        updated = conn.execute(
            """
            UPDATE history SET market_adp = (
                SELECT a.adp FROM adp_history a
                WHERE a.season = history.season
                  AND a.name_key = history.name_key
                  AND a.position = history.position
            )
            WHERE EXISTS (
                SELECT 1 FROM adp_history a
                WHERE a.season = history.season
                  AND a.name_key = history.name_key
                  AND a.position = history.position
            )
            """
        ).rowcount
        coverage = [
            dict(r) for r in conn.execute(
                "SELECT season, COUNT(*) AS picks, "
                "SUM(CASE WHEN market_adp IS NOT NULL THEN 1 ELSE 0 END) AS matched "
                "FROM history WHERE COALESCE(keeper,0)=0 GROUP BY season ORDER BY season"
            )
        ]
    return {"updated": updated, "coverage": coverage}


# --------------------------------------------------------------------------- #
# pool assembly
# --------------------------------------------------------------------------- #

# Hook for levels the backtest shows the slot arithmetic gets wrong. Empty: as of
# the 2022-2025 measurement every slot-derived level survived testing.
#
# Two corrections looked obvious and were both wrong, which is why this is empty
# rather than absent:
#
#   QB 12 -> 16. The raw points curve at QB12 falls 7.5 pts/spot against ~3.5 for
#   RB/WR/TE, which reads as a baseline set too shallow. It is not: QBs simply
#   score about twice as much, and normalised by the points available at the
#   baseline QB is 2.9%/spot against TE 2.8% and RB 2.6%. Never compare raw
#   slopes across positions on different scales. Shipped and measured anyway:
#   the draft test got worse, -190.5 vs -154.4.
#
#   Flex 0.45/0.45/0.10 -> the observed 0.25/0.73/0.02. Also worse, -449.5,
#   because more WRs exist so more land in the flex pool regardless of value.
#
# Measure, then test the fix. Do not ship a correction because a number looked
# wrong on its own.
REPLACEMENT_OVERRIDES: dict[str, int] = {}


def replacement_levels(league: dict) -> dict[str, int]:
    """How many players at each position are starters league-wide.

    That count is the replacement baseline: the Nth-best player at a position
    is what you can get for free, so value over replacement is what a pick is
    actually worth.

    `REPLACEMENT_OVERRIDES` then replaces any level the backtest showed the
    slot arithmetic gets wrong.
    """
    teams = int(league.get("n_teams", 12))
    slots = league.get("roster_slots", {})
    flex = int(slots.get("FLEX", 0))
    levels = {}
    for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
        base = int(slots.get(pos, 0)) * teams
        if pos in FLEX_POSITIONS and flex:
            # Spread flex slots across RB/WR/TE by roughly how often each fills them.
            weight = {"RB": 0.45, "WR": 0.45, "TE": 0.10}[pos]
            base += round(flex * teams * weight)
        levels[pos] = max(base, 1)
    levels.update(REPLACEMENT_OVERRIDES)
    return levels


def build_pool(refresh: bool = False) -> dict:
    """Assemble the draftable player pool and write it to SQLite."""
    league = db.get_league()
    scoring = league.get("scoring", "ppr")
    pts_col = {"ppr": "pts_ppr", "half": "pts_half", "standard": "pts_standard"}.get(
        scoring, "pts_ppr"
    )

    print("building player pool...", file=sys.stderr)
    cross = load_crosswalk(refresh)
    stats = load_season_stats(LAST_SEASON, refresh)
    snaps = load_snap_share(LAST_SEASON, refresh)
    byes = load_byes(LAST_SEASON + 1, refresh)

    stats = stats.rename(columns={pts_col: "pts_last"})
    stats["pts_last"] = pd.to_numeric(stats["pts_last"], errors="coerce").fillna(0.0)

    pool, rankings_report = load_all_rankings()
    # The base file is what defines the board, not whichever file is newest.
    base_name = rankings_report[0]["file"] if rankings_report else None
    if pool is not None:
        for entry in rankings_report:
            detail = ", ".join(entry["provides"]) or "-"
            print(f"  rankings [{entry['role']}] {entry['file']}: {detail}",
                  file=sys.stderr)
            stray = entry.get("unmatched") or []
            if stray:
                shown = ", ".join(stray[:6])
                print(
                    f"      {len(stray)} row(s) matched no player in the base "
                    f"(name spelling differs): {shown}"
                    + (" ..." if len(stray) > 6 else ""),
                    file=sys.stderr,
                )
        pool["source"] = "rankings"
    else:
        print(
            "  no rankings CSV found in data/rankings/ - falling back to "
            f"{LAST_SEASON} production",
            file=sys.stderr,
        )
        pool = _pool_from_stats(stats)
        pool["source"] = "nflverse"

    # --- join nflverse ids and stats onto the pool -------------------------- #
    cross_idx = _dedupe(cross, ["name_key", "position"], "last_season")
    pool = pool.merge(
        cross_idx[
            ["name_key", "position", "gsis_id", "espn_id", "pfr_id", "age",
             "rookie_season", "latest_team"]
        ],
        on=["name_key", "position"],
        how="left",
    )

    stats_idx = _dedupe(stats, ["name_key", "position"], "pts_last")
    pool = pool.merge(
        stats_idx[["name_key", "position", "pts_last", "games_last",
                   "target_share", "wopr"]],
        on=["name_key", "position"],
        how="left",
    )
    pool = pool.merge(snaps, on="pfr_id", how="left")

    # ESPN ADP needs espn_id, so it runs after the crosswalk join.
    adp_stats = apply_espn_adp(pool, LAST_SEASON + 1)

    pool["team"] = pool["team"].fillna(pool["latest_team"])
    pool["bye"] = pool["bye"].fillna(pool["team"].map(byes))
    pool["games_last"] = pd.to_numeric(pool["games_last"], errors="coerce")
    pool["pts_last"] = pd.to_numeric(pool["pts_last"], errors="coerce")
    pool["ppg_last"] = (pool["pts_last"] / pool["games_last"].replace(0, pd.NA)).round(1)

    # --- projected points -------------------------------------------------- #
    # Prefer FantasyPros projections; fall back to last season's production.
    pool["proj_points"] = pd.to_numeric(pool["proj"], errors="coerce")
    pool["proj_points"] = pool["proj_points"].fillna(pool["pts_last"])

    # --- value over replacement ------------------------------------------- #
    levels = replacement_levels(league)
    pool["vor"] = _compute_vor(pool, levels)

    # --- Vegas-implied projections, as a second independent opinion -------- #
    vegas_stats = _apply_vegas(pool, levels)

    pool["pos_rank"] = (
        pool.groupby("position")["ecr"].rank(method="first", na_option="bottom")
        .astype("Int64")
    )
    pool["player_key"] = pool.apply(
        lambda r: player_key(r["name"], r["position"]), axis=1
    )
    pool = pool.drop_duplicates(subset=["player_key"], keep="first")

    # Whether projections are real or a stand-in for last season decides how far
    # VOR can be trusted, so it travels with the summary.
    projected_from_rankings = int(
        pd.to_numeric(pool.get("proj"), errors="coerce").notna().sum()
    ) if "proj" in pool.columns else 0

    write_stats = _write_pool(pool)
    written = write_stats["players"]
    matched = int(pool["gsis_id"].notna().sum())
    return {
        "players": written,
        "picks": write_stats,
        "source": pool["source"].iloc[0] if len(pool) else None,
        "rankings_file": base_name,
        "rankings_files": rankings_report,
        "projections": projected_from_rankings,
        "projections_are_last_season": projected_from_rankings == 0,
        "adp": {
            **adp_stats,
            "consensus_in_pool": int(
                pd.to_numeric(pool["adp"], errors="coerce").notna().sum()
            ),
            "espn_in_pool": int(
                pd.to_numeric(pool["espn_adp"], errors="coerce").notna().sum()
            ),
        },
        "vegas": vegas_stats,
        "id_matched": matched,
        "unmatched": written - matched,
        "scoring": scoring,
        "replacement_levels": levels,
    }


def _pool_from_stats(stats: pd.DataFrame) -> pd.DataFrame:
    """Rankings-free fallback: rank last season's producers by points."""
    keep = stats[stats["position"].isin(("QB", "RB", "WR", "TE", "K"))].copy()
    keep = keep.sort_values("pts_last", ascending=False)
    keep = keep[keep["pts_last"] > 0].head(400).reset_index(drop=True)
    out = pd.DataFrame({
        "name": keep["display_name"],
        "position": keep["position"],
        "team": keep["team"],
        "name_key": keep["name_key"],
        "ecr": range(1, len(keep) + 1),
        "tier": pd.NA,
        "adp": pd.NA,
        "bye": pd.NA,
        "proj": pd.NA,
    })
    return out


def _dedupe(df: pd.DataFrame, keys: list[str], prefer: str) -> pd.DataFrame:
    """Collapse duplicate join keys, keeping the row with the largest `prefer`."""
    if prefer not in df.columns:
        return df.drop_duplicates(subset=keys, keep="first")
    ordered = df.sort_values(prefer, ascending=False, na_position="last")
    return ordered.drop_duplicates(subset=keys, keep="first")


def _compute_vor(pool: pd.DataFrame, levels: dict[str, int]) -> pd.Series:
    vor = pd.Series(pd.NA, index=pool.index, dtype="Float64")
    for pos, level in levels.items():
        mask = pool["position"] == pos
        points = pd.to_numeric(pool.loc[mask, "proj_points"], errors="coerce")
        ranked = points.dropna().sort_values(ascending=False)
        # Without at least `level` players at the position there is no
        # replacement baseline to measure against. Leave VOR null rather than
        # anchoring on the worst player and reporting a meaningless zero.
        if len(ranked) < level:
            continue
        baseline = float(ranked.iloc[level - 1])
        vor.loc[mask] = (points - baseline).round(1)
    return vor


def _apply_vegas(pool: pd.DataFrame, levels: dict[str, int]) -> dict:
    """Join Vegas projections onto the pool and derive a comparable VOR.

    Replacement baselines come from the winwithodds file's OWN pool, not from
    the joined rows. Its pool is deeper than the board (630 vs ~516), and a
    baseline is only meaningful relative to the players the source itself
    ranked - borrowing the other source's cutoff would measure each player
    against a replacement level its own projections never saw.
    """
    pool["vegas_proj"] = pd.NA
    pool["vegas_vor_raw"] = pd.NA
    pool["vegas_vor"] = pd.NA

    try:
        vegas = load_vegas_projections()
    except SystemExit as exc:
        print(f"  skipping vegas projections: {exc}", file=sys.stderr)
        return {"file": None, "error": str(exc), "matched": 0}
    if vegas is None or vegas.empty:
        return {"file": None, "matched": 0}

    baselines = {}
    for pos, level in levels.items():
        ranked = (
            vegas.loc[vegas["position"] == pos, "vegas_proj"]
            .dropna().sort_values(ascending=False)
        )
        if len(ranked) >= level:
            baselines[pos] = float(ranked.iloc[level - 1])

    merged = pool.merge(vegas, on=["name_key", "position"], how="left",
                        suffixes=("", "_vegas"))
    pool["vegas_proj"] = pd.to_numeric(
        merged["vegas_proj_vegas"], errors="coerce"
    ).values
    baseline_series = pool["position"].map(baselines)
    pool["vegas_vor_raw"] = (
        pd.to_numeric(pool["vegas_proj"], errors="coerce") - baseline_series
    )

    scale = vegas_scale_factor(pool)
    pool["vegas_vor"] = (pool["vegas_vor_raw"] * scale).round(1)

    matched = int(pool["vegas_proj"].notna().sum())
    pool_keys = set(zip(pool["name_key"], pool["position"]))
    return {
        "file": getattr(vegas, "attrs", {}).get("file"),
        "rows": len(vegas),
        "matched": matched,
        "unmatched_in_pool": int(len(pool) - matched),
        "not_on_board": int(sum(
            1 for r in vegas.itertuples(index=False)
            if (r.name_key, r.position) not in pool_keys
        )),
        "scale": scale,
        "baselines": {k: round(v, 1) for k, v in baselines.items()},
    }


POOL_FIELDS = (
    "player_key", "name", "position", "team", "bye", "gsis_id", "espn_id",
    "pfr_id", "ecr", "adp", "espn_adp", "tier", "pos_rank", "proj_points", "vor",
    "vegas_proj", "vegas_vor",
    "pts_last", "ppg_last", "games_last", "target_share", "wopr", "snap_pct",
    "age", "rookie_season", "source",
)


def _write_pool(pool: pd.DataFrame) -> int:
    rows = []
    for record in pool.to_dict("records"):
        row = []
        for field in POOL_FIELDS:
            value = record.get(field)
            if value is None or (not isinstance(value, str) and pd.isna(value)):
                row.append(None)
            elif field in ("bye", "tier", "pos_rank", "games_last", "rookie_season"):
                row.append(int(value))
            elif field in ("player_key", "name", "position", "team", "gsis_id",
                           "espn_id", "pfr_id", "source"):
                row.append(str(value))
            else:
                row.append(float(value))
        rows.append(row)

    placeholders = ",".join("?" * len(POOL_FIELDS))
    with db.session() as conn:
        # Picks are preserved across a rebuild wherever possible. Blindly wiping
        # them means a rebuild mid-draft silently destroys the board - and a
        # rebuild is exactly what you do when a new rankings file lands.
        saved = [
            dict(r) for r in conn.execute(
                "SELECT overall, round, slot, manager_id, player_key, created_at "
                "FROM picks ORDER BY overall"
            )
        ]
        conn.execute("DELETE FROM picks")
        conn.execute("DELETE FROM players")
        conn.executemany(
            f"INSERT INTO players ({','.join(POOL_FIELDS)}) VALUES ({placeholders})",
            rows,
        )

        # Only a contiguous prefix can be restored: `overall` must stay gapless or
        # whose-turn-is-it arithmetic breaks. Stop at the first pick whose player
        # is no longer in the pool.
        surviving = {r[0] for r in conn.execute("SELECT player_key FROM players")}
        restored, lost = 0, None
        for pick in saved:
            if pick["player_key"] not in surviving:
                lost = pick["player_key"]
                break
            conn.execute(
                "INSERT INTO picks (overall, round, slot, manager_id, player_key, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (pick["overall"], pick["round"], pick["slot"], pick["manager_id"],
                 pick["player_key"], pick["created_at"]),
            )
            restored += 1

    return {
        "players": len(rows),
        "picks_before": len(saved),
        "picks_restored": restored,
        "picks_dropped": len(saved) - restored,
        "first_missing": lost,
    }


# --------------------------------------------------------------------------- #
# ESPN history (DraftTool.py output)
# --------------------------------------------------------------------------- #

HISTORY_FIELDS = (
    "season", "overall_pick", "round", "round_pick", "owner_guid",
    "owner_label", "player_name", "position", "pro_team", "keeper",
    "espn_player_id", "name_key", "draft_slot", "pos_taken", "season_pts",
    "pos_finish",
)

# ESPN encodes team defenses as negative player ids: -16000 - proTeamId.
ESPN_DST_BASE = -16000
ESPN_PRO_TEAM = {
    1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN",
    8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR",
    15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI",
    22: "ARI", 23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WAS",
    29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}
DST_FULL_NAME = {
    abbr: f"{abbr} D/ST" for abbr in ESPN_PRO_TEAM.values()
}


def canonical_dst_name(team: str | None) -> str | None:
    """One spelling for a team defence, everywhere.

    Sources disagree wildly - ESPN resolves to "BUF D/ST", FantasyPros rankings say
    "Buffalo Bills", RotoBaller says "Bills". Left alone, team defences never join
    between the player pool and league history, so they silently miss out on market
    ranks, ADP and outcome stats. Canonical form is the ESPN one, because that is
    what the history table already holds.
    """
    abbr = norm_team(team)
    return DST_FULL_NAME.get(abbr) if abbr else None


ESPN_PLAYERS_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
    "/players?scoringPeriodId=0&view=kona_player_info"
)
ESPN_POSITION_ID = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}


def espn_player_universe(season: int, refresh: bool = False) -> dict[str, dict]:
    """ESPN id -> name/position/team for a season's whole player universe.

    This endpoint needs no league and no cookies, unlike the league-scoped
    `kona_player_info` view that DraftTool.py depends on. The response is ~38MB,
    so it is slimmed to the three fields we need and cached on disk.

    Used only to mop up ids nflverse has no `espn_id` for — two-way and practice
    squad oddities like Travis Hunter.
    """
    cache = CACHE / f"espn_players_{season}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))

    print(f"  fetching ESPN player universe for {season}", file=sys.stderr)
    req = Request(
        ESPN_PLAYERS_URL.format(season=season),
        headers={
            "User-Agent": "draft-tool",
            "Accept": "application/json",
            "X-Fantasy-Filter": json.dumps({"players": {"limit": 20000}}),
        },
    )
    with urlopen(req, timeout=180) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    entries = payload if isinstance(payload, list) else payload.get("players", [])
    slim = {}
    for entry in entries:
        player = entry.get("player", entry) if isinstance(entry, dict) else {}
        pid = player.get("id")
        if pid is None:
            continue
        slim[str(pid)] = {
            "name": player.get("fullName"),
            "position": ESPN_POSITION_ID.get(player.get("defaultPositionId")),
            "team": ESPN_PRO_TEAM.get(player.get("proTeamId")),
        }
    cache.write_text(json.dumps(slim), encoding="utf-8")
    return slim


def resolve_espn_ids(df: pd.DataFrame, refresh: bool = False) -> tuple[pd.DataFrame, dict]:
    """Fill missing player_name / position / pro_team from nflverse espn_id.

    ESPN's own `kona_player_info` endpoint is the documented way to turn a
    playerId into a name, and it is the least reliable part of the scrape - it
    rate-limits, rejects large filters, and is not supported at all on the
    historical `leagueHistory` path. When it fails, DraftTool.py writes the pick
    with a null name and carries on, which silently guts the tendency model.

    nflverse's players table carries `espn_id` for ~100% of skill-position
    players active 2022-2025, with no duplicate ids, so the same lookup can be
    done offline against a file we already cache. That is strictly more reliable
    than asking ESPN twice.
    """
    stats = {"needed": 0, "resolved": 0, "dst": 0, "unresolved": []}
    if "espn_player_id" not in df.columns:
        return df, stats

    missing = df["player_name"].isna() | (df["position"].isna())
    stats["needed"] = int(missing.sum())
    if not stats["needed"]:
        return df, stats

    cross = load_crosswalk(refresh)
    cross = cross[cross["espn_id"].notna() & cross["position"].notna()]
    cross = cross.drop_duplicates(subset=["espn_id"], keep="first")
    lookup = cross.set_index(cross["espn_id"].astype(str).str.strip())

    names, positions, teams = {}, {}, {}
    for raw in df.loc[missing, "espn_player_id"].dropna().unique():
        try:
            pid = int(float(raw))
        except (TypeError, ValueError):
            continue
        if pid < 0:                                  # team defense
            abbr = ESPN_PRO_TEAM.get(ESPN_DST_BASE - pid)
            if abbr:
                names[raw] = DST_FULL_NAME[abbr]
                positions[raw] = "DST"
                teams[raw] = abbr
                stats["dst"] += 1
            continue
        key = str(pid)
        if key in lookup.index:
            row = lookup.loc[key]
            names[raw] = row["display_name"]
            positions[raw] = row["position"]
            teams[raw] = row["latest_team"]
        else:
            stats["unresolved"].append(pid)

    # When ESPN resolved nothing, these columns arrive entirely empty and pandas
    # types them float64 - which then refuses to hold the strings we are about to
    # write. Widen to object first.
    for column in ("player_name", "position", "pro_team"):
        df[column] = df[column].astype("object")

    ids = df.loc[missing, "espn_player_id"]
    df.loc[missing, "player_name"] = (
        df.loc[missing, "player_name"].fillna(ids.map(names))
    )
    df.loc[missing, "position"] = (
        df.loc[missing, "position"].fillna(ids.map(positions))
    )
    df.loc[missing, "pro_team"] = (
        df.loc[missing, "pro_team"].fillna(ids.map(teams))
    )
    # Second pass: anything nflverse has no espn_id for, ask ESPN's open player
    # endpoint. Per season, because retired players drop out of later universes.
    still = df["player_name"].isna() & df["espn_player_id"].notna()
    if still.any():
        for season in sorted(df.loc[still, "season"].dropna().unique()):
            season_mask = still & (df["season"] == season)
            if not season_mask.any():
                continue
            try:
                universe = espn_player_universe(int(season), refresh)
            except Exception as exc:                           # noqa: BLE001
                print(f"  ESPN universe lookup failed for {season}: {exc}",
                      file=sys.stderr)
                continue
            for idx in df.index[season_mask]:
                pid = df.at[idx, "espn_player_id"]
                try:
                    entry = universe.get(str(int(float(pid))))
                except (TypeError, ValueError):
                    entry = None
                if not entry or not entry.get("name"):
                    continue
                df.at[idx, "player_name"] = entry["name"]
                if pd.isna(df.at[idx, "position"]):
                    df.at[idx, "position"] = normalize_position(entry["position"])
                if pd.isna(df.at[idx, "pro_team"]):
                    df.at[idx, "pro_team"] = entry["team"]
                stats["from_espn"] = stats.get("from_espn", 0) + 1
        still = df["player_name"].isna() & df["espn_player_id"].notna()

    stats["resolved"] = int(df.loc[missing, "player_name"].notna().sum())
    stats["unresolved"] = [
        int(float(v)) for v in df.loc[still, "espn_player_id"].dropna().unique()
    ]
    return df, stats


def import_history(csv_path: Path, refresh: bool = False) -> dict:
    """Load draft_history.csv from DraftTool.py and enrich it.

    Three things get added that the raw ESPN export does not carry:

    * `draft_slot` - which seat an owner drafted from that season, derived from
      their first-round pick. Slot changes year to year and confounds any
      "they always take X in round 1" reading, so it has to be recorded.
    * `pos_taken` - the Nth player at that position taken that season, which is
      how you compare a pick against positional demand rather than raw ADP.
    * `season_pts` / `pos_finish` - what the player actually went on to score.
      That turns the history into an outcome record, not just a choice record.
    """
    df = pd.read_csv(csv_path)
    required = {"season", "overall_pick", "player_name"}
    if not required.issubset(df.columns):
        raise SystemExit(
            f"{csv_path.name} is missing columns: {required - set(df.columns)}"
        )
    for field in HISTORY_FIELDS:
        if field not in df.columns:
            df[field] = None

    df["position"] = df["position"].map(normalize_position)
    df["keeper"] = (
        df["keeper"].map(lambda v: str(v).strip().lower() in ("true", "1", "yes"))
        .astype(int)
    )

    # ESPN's name lookup fails often enough that it cannot be trusted; fill the
    # gaps from nflverse before anything downstream depends on name or position.
    df, resolution = resolve_espn_ids(df, refresh)

    df["name_key"] = df["player_name"].map(norm_name)
    df["season"] = pd.to_numeric(df["season"], errors="coerce").astype("Int64")
    df["overall_pick"] = pd.to_numeric(df["overall_pick"], errors="coerce").astype("Int64")
    df["round"] = pd.to_numeric(df["round"], errors="coerce").astype("Int64")
    df["round_pick"] = pd.to_numeric(df["round_pick"], errors="coerce").astype("Int64")

    df = df.dropna(subset=["season", "overall_pick"])
    df["draft_slot"] = _derive_draft_slots(df)
    df["pos_taken"] = (
        df.sort_values("overall_pick")
        .groupby(["season", "position"])
        .cumcount() + 1
    )

    df = _attach_outcomes(df, refresh)

    rows = [
        tuple(_py(v) for v in rec)
        for rec in df[list(HISTORY_FIELDS)].itertuples(index=False, name=None)
    ]
    with db.session() as conn:
        # Keys are (season, overall_pick); a re-import of the same seasons should
        # replace those rows, not accumulate alongside them.
        conn.execute(
            "DELETE FROM history WHERE season IN "
            f"({','.join('?' * df['season'].nunique())})",
            [int(s) for s in sorted(df["season"].dropna().unique())],
        )
        conn.executemany(
            f"INSERT OR REPLACE INTO history ({','.join(HISTORY_FIELDS)}) "
            f"VALUES ({','.join('?' * len(HISTORY_FIELDS))})",
            rows,
        )
        linked = link_owners_to_managers(conn)
        market = attach_market_ranks(conn)

    seasons = sorted(int(s) for s in df["season"].dropna().unique())
    return {
        "picks": len(rows),
        "seasons": seasons,
        "owners": int(df["owner_guid"].nunique()),
        "keepers": int(df["keeper"].sum()),
        "outcomes_matched": int(df["season_pts"].notna().sum()),
        "linked_managers": linked,
        "market_ranks": market,
        "positions_missing": int(df["position"].isna().sum()),
        "name_resolution": {
            "needed": resolution["needed"],
            "resolved": resolution["resolved"],
            "team_defenses": resolution["dst"],
            "from_espn": resolution.get("from_espn", 0),
            "unresolved_ids": resolution["unresolved"][:20],
            "unresolved_count": len(resolution["unresolved"]),
        },
    }


def _derive_draft_slots(df: pd.DataFrame) -> pd.Series:
    """Each owner's seat per season, taken from their round-1 pick."""
    firsts = (
        df[df["round"] == 1]
        .dropna(subset=["owner_guid"])
        .set_index(["season", "owner_guid"])["round_pick"]
    )
    firsts = firsts[~firsts.index.duplicated(keep="first")]
    keys = list(zip(df["season"], df["owner_guid"]))
    return pd.Series(
        [firsts.get(k) for k in keys], index=df.index, dtype="Int64"
    )


def _attach_outcomes(df: pd.DataFrame, refresh: bool) -> pd.DataFrame:
    """Join each historical pick to what the player actually scored that year."""
    df["season_pts"] = pd.NA
    df["pos_finish"] = pd.NA
    for season in sorted(int(s) for s in df["season"].dropna().unique()):
        try:
            stats = load_season_stats(season, refresh)
        except Exception as exc:                               # noqa: BLE001
            print(f"  no outcome stats for {season}: {exc}", file=sys.stderr)
            continue
        stats = stats.rename(columns={"pts_ppr": "season_pts"})
        stats = _dedupe(stats, ["name_key", "position"], "season_pts")
        stats["pos_finish"] = (
            stats.groupby("position")["season_pts"]
            .rank(ascending=False, method="min")
            .astype("Int64")
        )
        lookup = stats.set_index(["name_key", "position"])[
            ["season_pts", "pos_finish"]
        ]
        mask = df["season"] == season
        keys = list(zip(df.loc[mask, "name_key"], df.loc[mask, "position"]))
        df.loc[mask, "season_pts"] = [
            lookup["season_pts"].get(k) for k in keys
        ]
        df.loc[mask, "pos_finish"] = [
            lookup["pos_finish"].get(k) for k in keys
        ]
    return df


def attach_market_ranks(conn) -> dict:
    """Stamp each historical pick with ESPN's draft rank for that season.

    This is the baseline the reach metric measures against. Safe to re-run, and
    runnable independently of the history import, since the ADP exports usually
    arrive later:

        python -m app.ingest --market-ranks
    """
    seasons = [
        r["season"] for r in conn.execute(
            "SELECT DISTINCT season FROM history ORDER BY season"
        )
    ]
    report = {"seasons": {}, "updated": 0}

    for season in seasons:
        path = ADP_DIR / f"espn_adp_{season}.csv"
        if not path.exists():
            report["seasons"][season] = {"status": "no export", "matched": 0}
            continue

        adp = pd.read_csv(path)
        if "espn_draft_rank" not in adp.columns:
            report["seasons"][season] = {"status": "no rank column", "matched": 0}
            continue

        adp["key"] = adp["espn_player_id"].map(
            lambda v: str(int(float(v))) if pd.notna(v) else None
        )
        rank = pd.to_numeric(adp["espn_draft_rank"], errors="coerce")
        market_adp = pd.to_numeric(adp.get("espn_adp"), errors="coerce")
        # ESPN's "effectively undrafted" ceiling is not a real ADP.
        market_adp = market_adp.where(market_adp < ADP_UNDRAFTED_CEILING)

        rank_map = {
            k: v for k, v in zip(adp["key"], rank) if k and pd.notna(v)
        }
        adp_map = {
            k: v for k, v in zip(adp["key"], market_adp) if k and pd.notna(v)
        }
        if not rank_map:
            report["seasons"][season] = {
                "status": "ranks all empty", "matched": 0,
            }
            continue

        picks = conn.execute(
            "SELECT overall_pick, espn_player_id FROM history WHERE season = ?",
            (season,),
        ).fetchall()
        updates = []
        for pick in picks:
            pid = pick["espn_player_id"]
            if pid is None:
                continue
            key = str(int(pid))
            if key not in rank_map and key not in adp_map:
                continue
            updates.append((
                float(rank_map[key]) if key in rank_map else None,
                float(adp_map[key]) if key in adp_map else None,
                season, pick["overall_pick"],
            ))
        conn.executemany(
            "UPDATE history SET market_rank = ?, market_adp = ? "
            "WHERE season = ? AND overall_pick = ?",
            updates,
        )
        report["updated"] += len(updates)
        report["seasons"][season] = {
            "status": "ok",
            "matched": len(updates),
            "picks": len(picks),
        }
    return report


def seed_managers_from_history(conn, season: int | None = None) -> dict:
    """Rebuild the manager list from a season of league history.

    Saves typing twelve names, and links every owner GUID in one pass instead of
    relying on name matching. Uses the most recent season by default.

    Note the seats are that season's draft order, not this year's - slots move
    every season. Set the real order on the Setup tab before drafting; the names
    and GUID links are what matter here.
    """
    if season is None:
        row = conn.execute("SELECT MAX(season) AS s FROM history").fetchone()
        season = row["s"] if row else None
    if season is None:
        return {"seeded": 0, "season": None, "note": "no history imported"}

    made = conn.execute("SELECT COUNT(*) AS n FROM picks").fetchone()["n"]
    if made:
        raise ValueError(
            "Cannot reseed managers while a draft is in progress. Reset the draft first."
        )

    owners = conn.execute(
        """
        SELECT owner_guid, owner_label, MIN(draft_slot) AS slot, COUNT(*) AS picks
        FROM history
        WHERE season = ? AND owner_guid IS NOT NULL AND draft_slot IS NOT NULL
        GROUP BY owner_guid
        ORDER BY slot
        """,
        (season,),
    ).fetchall()
    if not owners:
        return {"seeded": 0, "season": season, "note": "no draft slots for that season"}

    # Identity first, seat second: the league's recorded owner if there is one,
    # otherwise whoever currently holds the is_me flag.
    league_now = db.get_league(conn)
    previous_me = conn.execute(
        "SELECT owner_guid FROM managers WHERE is_me = 1"
    ).fetchone()
    keep_me = league_now.get("my_owner_guid") or (
        previous_me["owner_guid"] if previous_me else None
    )

    conn.execute("DELETE FROM managers")
    for slot, owner in enumerate(owners, start=1):
        conn.execute(
            "INSERT INTO managers (name, draft_slot, is_me, owner_guid) "
            "VALUES (?, ?, ?, ?)",
            (
                owner["owner_label"] or f"Team {slot}",
                slot,
                1 if keep_me and owner["owner_guid"] == keep_me else 0,
                owner["owner_guid"],
            ),
        )

    league = get_league_for_seed(conn)
    if league["n_teams"] != len(owners):
        league["n_teams"] = len(owners)
    if keep_me:
        league["my_owner_guid"] = keep_me
    db.set_league(conn, league)
    # Seats were just rebuilt, so re-derive which one is mine.
    from . import draft as _draft
    _draft.resolve_me(conn, league)

    return {
        "seeded": len(owners),
        "season": season,
        "n_teams": len(owners),
        "managers": [dict(r) for r in conn.execute(
            "SELECT id, name, draft_slot, is_me, owner_guid FROM managers "
            "ORDER BY draft_slot"
        )],
    }


def get_league_for_seed(conn):
    return db.get_league(conn)


def link_owners_to_managers(conn) -> int:
    """Attach ESPN owner GUIDs to the manager rows on the draft board.

    Matches on normalized owner label vs manager name. Anything ambiguous is
    left alone for the user to set explicitly rather than guessed at.
    """
    owners = conn.execute(
        "SELECT owner_guid, owner_label, COUNT(*) AS n FROM history "
        "WHERE owner_guid IS NOT NULL GROUP BY owner_guid"
    ).fetchall()
    managers = conn.execute("SELECT id, name FROM managers").fetchall()
    by_name = {}
    for manager in managers:
        by_name.setdefault(norm_name(manager["name"]), []).append(manager["id"])

    linked = 0
    for owner in owners:
        candidates = by_name.get(norm_name(owner["owner_label"] or ""), [])
        if len(candidates) == 1:
            conn.execute(
                "UPDATE managers SET owner_guid = ? WHERE id = ?",
                (owner["owner_guid"], candidates[0]),
            )
            linked += 1
    return linked


def main():
    parser = argparse.ArgumentParser(description="Build the draft player pool")
    parser.add_argument(
        "--refresh", action="store_true",
        help="re-download nflverse parquet files instead of using the cache",
    )
    parser.add_argument(
        "--history", type=Path,
        help="also import a draft_history.csv from DraftTool.py",
    )
    parser.add_argument(
        "--seed-managers", action="store_true",
        help="rebuild the manager list and owner links from the newest season "
             "of imported history",
    )
    parser.add_argument(
        "--adp-history", action="store_true",
        help="load FantasyPros Overall ADP exports from data/adp/ and stamp "
             "consensus ADP onto imported league history",
    )
    parser.add_argument(
        "--market-ranks", action="store_true",
        help="stamp imported history with ESPN draft ranks from data/adp/ "
             "(the reach-metric baseline); run after tools/espn_adp.py",
    )
    args = parser.parse_args()

    db.init_db()
    summary = build_pool(refresh=args.refresh)

    print(
        f"\n{summary['players']} players written "
        f"(source: {summary['source']}"
        + (f" / {summary['rankings_file']}" if summary["rankings_file"] else "")
        + f", scoring: {summary['scoring']})",
        file=sys.stderr,
    )
    print(
        f"{summary['id_matched']} matched to nflverse ids, "
        f"{summary['unmatched']} unmatched",
        file=sys.stderr,
    )
    print(f"replacement levels: {summary['replacement_levels']}", file=sys.stderr)
    picks = summary.get("picks") or {}
    if picks.get("picks_before"):
        note = (
            f"kept {picks['picks_restored']}/{picks['picks_before']} existing picks"
        )
        if picks["picks_dropped"]:
            note += (
                f"; dropped {picks['picks_dropped']} from "
                f"'{picks['first_missing']}' onward - that player is no longer in "
                "the pool, and picks must stay contiguous"
            )
        print(note, file=sys.stderr)

    adp = summary.get("adp") or {}
    if adp:
        print(
            f"ADP: consensus {adp.get('consensus_in_pool', 0)} players "
            f"(rankings files), ESPN {adp.get('espn_in_pool', 0)} players "
            f"(season {adp.get('season')}) - kept as separate signals",
            file=sys.stderr,
        )
    else:
        print(
            "ADP: none. Run tools/espn_adp.py to fetch ESPN's platform-wide ADP.",
            file=sys.stderr,
        )
    if summary.get("projections_are_last_season"):
        print(
            "\nNOTE: no projected-points column in any rankings file, so "
            "Projected and VOR are computed from LAST SEASON'S actual scoring. "
            "That penalises anyone who was injured. Add a FantasyPros "
            "projections export (it has an FPTS column) to data/rankings/ - it "
            "will be merged in alongside the rankings file.",
            file=sys.stderr,
        )
    else:
        print(
            f"projections: {summary['projections']} players have real projected "
            "points from the rankings files",
            file=sys.stderr,
        )

    vegas = summary.get("vegas") or {}
    if vegas.get("file"):
        print(
            f"vegas: {vegas['matched']} players matched from {vegas['file']} "
            f"({vegas['rows']} rows, {vegas['not_on_board']} not on the board), "
            f"VOR rescaled x{vegas['scale']} to match the rankings projections",
            file=sys.stderr,
        )
    elif vegas.get("error"):
        print(f"vegas: {vegas['error']}", file=sys.stderr)
    else:
        print(
            "vegas: no CSV in data/vegas/ - Vegas-implied value is unavailable, "
            "suggestions fall back to the rankings projections alone",
            file=sys.stderr,
        )

    if args.history:
        h = import_history(args.history, refresh=args.refresh)
        print(
            f"\nimported {h['picks']} historical picks across seasons "
            f"{h['seasons']} for {h['owners']} owners "
            f"({h['keepers']} keepers, {h['outcomes_matched']} with outcome stats)",
            file=sys.stderr,
        )
        res = h["name_resolution"]
        if res["needed"]:
            print(
                f"resolved {res['resolved']}/{res['needed']} missing player names "
                f"({res['team_defenses']} team defenses, "
                f"{res.get('from_espn', 0)} via ESPN fallback)"
                + (f"; {res['unresolved_count']} ids unresolved: "
                   f"{res['unresolved_ids']}" if res["unresolved_count"] else ""),
                file=sys.stderr,
            )
        if h["positions_missing"]:
            print(
                f"WARNING: {h['positions_missing']} picks still have no position "
                "and will be excluded from every tendency rate",
                file=sys.stderr,
            )
        print(
            f"{h['linked_managers']} owners auto-linked to board managers by name",
            file=sys.stderr,
        )

    if args.adp_history:
        report = import_adp_history()
        print(f"\nADP history: {report['rows']} rows loaded", file=sys.stderr)
        for season, info in sorted(report["seasons"].items()):
            mix = ", ".join(
                f"{p} {n}" for p, n in sorted(info["by_position"].items())
            )
            print(
                f"  {season}  {info['players']:>4} players "
                f"({info['with_adp']} with ADP, {info['with_espn']} with ESPN ADP, "
                f"deepest ADP {info['max_adp']:.0f})  [{mix}]",
                file=sys.stderr,
            )
        stamped = stamp_history_from_adp()
        print(f"\nstamped consensus ADP onto history:", file=sys.stderr)
        for row in stamped["coverage"]:
            pct = row["matched"] / row["picks"] if row["picks"] else 0
            print(f"  {row['season']}  {row['matched']}/{row['picks']} picks "
                  f"({pct:.0%})", file=sys.stderr)

    if args.market_ranks:
        with db.session() as conn:
            market = attach_market_ranks(conn)
        print(f"\nmarket ranks stamped on {market['updated']} picks",
              file=sys.stderr)
        for season, info in sorted(market["seasons"].items()):
            detail = (
                f"{info['matched']}/{info['picks']}"
                if info["status"] == "ok" else info["status"]
            )
            print(f"  {season}  {detail}", file=sys.stderr)

    if args.seed_managers:
        with db.session() as conn:
            seeded = seed_managers_from_history(conn)
        if seeded["seeded"]:
            print(
                f"\nseeded {seeded['seeded']} managers from the {seeded['season']} "
                "draft (names + owner links set; verify THIS year's draft order "
                "on the Setup tab)",
                file=sys.stderr,
            )
            for manager in seeded["managers"]:
                print(f"  slot {manager['draft_slot']:>2}  {manager['name']}",
                      file=sys.stderr)
        else:
            print(f"\ncould not seed managers: {seeded.get('note')}", file=sys.stderr)


if __name__ == "__main__":
    main()
