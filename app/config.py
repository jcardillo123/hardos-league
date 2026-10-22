"""Paths, constants and league defaults."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
RANKINGS_DIR = DATA / "rankings"
ADP_DIR = DATA / "adp"
# Vegas-derived projections live in their own folder, NOT in data/rankings/.
# Everything in rankings/ is merged into one consensus pool, and this is a
# second independent opinion - merging it would average the disagreement away,
# which is the one thing we want to keep.
VEGAS_DIR = DATA / "vegas"
DB_PATH = DATA / "draft.db"
STATIC = Path(__file__).resolve().parent / "static"

for _d in (DATA, CACHE, RANKINGS_DIR, ADP_DIR, VEGAS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# nflverse publishes its data as parquet assets on GitHub releases. This is
# what nfl_data_py wraps; we read the assets directly so we are not pinned to
# that package's numpy<2 requirement.
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"

# Last completed NFL season. Draft prep for season N uses season N-1 stats.
LAST_SEASON = 2025

DRAFT_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DST")

# Board columns: (label, positions shown in that column)
BOARD_COLUMNS = (
    ("QB", ("QB",)),
    ("RB", ("RB",)),
    ("WR", ("WR",)),
    ("TE", ("TE",)),
    ("K / DST", ("K", "DST")),
)

DEFAULT_LEAGUE = {
    "n_teams": 12,
    "rounds": 16,
    "my_slot": 1,
    # Who I am, as an ESPN owner GUID. `my_slot` and `managers.is_me` both describe
    # a *seat*, and seats change hands whenever the draft order is edited - so
    # identity is tracked separately and the seat is derived from it.
    "my_owner_guid": None,
    "scoring": "ppr",          # ppr | half | standard
    "roster_slots": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1, "BN": 7},
    "snake_type": "snake",     # snake | linear
    # Mock mode: opponents' picks are simulated instead of entered by hand.
    "mock_mode": False,
    "mock_signal": "ecr",
}

FLEX_POSITIONS = ("RB", "WR", "TE")
