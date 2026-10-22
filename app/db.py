"""SQLite persistence.

Draft state lives on disk, not in memory: if the server or the browser dies
mid-draft you reload the page and carry on from the last pick.
"""

import json
import sqlite3
from contextlib import contextmanager

import numpy as np

from .config import DB_PATH, DEFAULT_LEAGUE

# numpy scalars support the buffer protocol, so sqlite3 stores them as 8-byte
# BLOBs instead of rejecting them - silent corruption that only surfaces later
# when something tries to read the value back as a number. Since every ingest
# path here runs through pandas, adapt them globally.
for _np_int in (np.int8, np.int16, np.int32, np.int64, np.uint8, np.uint16, np.uint32):
    sqlite3.register_adapter(_np_int, int)
for _np_float in (np.float16, np.float32, np.float64):
    sqlite3.register_adapter(_np_float, float)
sqlite3.register_adapter(np.bool_, lambda v: int(bool(v)))

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    player_key    TEXT PRIMARY KEY,   -- normalized name + position
    name          TEXT NOT NULL,
    position      TEXT NOT NULL,
    team          TEXT,
    bye           INTEGER,
    gsis_id       TEXT,
    espn_id       TEXT,
    pfr_id        TEXT,
    ecr           REAL,               -- expert consensus rank, from the base file
    adp           REAL,               -- consensus ADP, from the rankings files
    espn_adp      REAL,               -- ESPN platform ADP, kept separate on purpose
    tier          INTEGER,
    pos_rank      INTEGER,
    proj_points   REAL,
    vor           REAL,               -- value over replacement at position
    vegas_proj    REAL,               -- season points implied by Vegas odds
    vegas_vor     REAL,               -- VOR from vegas_proj, rescaled to vor units
    pts_last      REAL,               -- fantasy points, last completed season
    ppg_last      REAL,
    games_last    INTEGER,
    target_share  REAL,
    wopr          REAL,
    snap_pct      REAL,
    age           REAL,
    rookie_season INTEGER,
    notes         TEXT,
    source        TEXT                -- rankings | nflverse
);
CREATE INDEX IF NOT EXISTS idx_players_pos ON players(position);
CREATE INDEX IF NOT EXISTS idx_players_ecr ON players(ecr);

CREATE TABLE IF NOT EXISTS managers (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    draft_slot INTEGER NOT NULL UNIQUE,
    is_me      INTEGER NOT NULL DEFAULT 0,
    owner_guid TEXT                    -- joins to ESPN history when available
);

CREATE TABLE IF NOT EXISTS picks (
    overall    INTEGER PRIMARY KEY,
    round      INTEGER NOT NULL,
    slot       INTEGER NOT NULL,
    manager_id INTEGER NOT NULL,
    player_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (manager_id) REFERENCES managers(id),
    FOREIGN KEY (player_key) REFERENCES players(player_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_picks_player ON picks(player_key);

-- Historical picks imported from ESPN (DraftTool.py output).
CREATE TABLE IF NOT EXISTS history (
    season       INTEGER NOT NULL,
    overall_pick INTEGER NOT NULL,
    round        INTEGER,
    round_pick   INTEGER,
    owner_guid   TEXT,
    owner_label  TEXT,
    player_name  TEXT,
    position     TEXT,
    pro_team     TEXT,
    keeper       INTEGER,
    espn_player_id INTEGER,-- raw ESPN id; resolves name/position via nflverse
    market_rank  REAL,     -- ESPN draft rank that season (the reach baseline)
    market_adp   REAL,     -- ESPN ADP that season, where ESPN served one
    name_key     TEXT,     -- normalized name, joins to players/nflverse
    draft_slot   INTEGER,  -- derived: which seat this owner drafted from
    pos_taken    INTEGER,  -- Nth player at this position taken that season
    season_pts   REAL,     -- fantasy points the player actually scored
    pos_finish   INTEGER,  -- where that ranked at the position that season
    PRIMARY KEY (season, overall_pick)
);
CREATE INDEX IF NOT EXISTS idx_history_owner ON history(owner_guid);
CREATE INDEX IF NOT EXISTS idx_history_season ON history(season);

-- Historical preseason ADP, one row per player per season. The contemporaneous
-- market baseline that backtesting needs: without it the available pool can only be
-- reconstructed from players who were drafted, which understates what was on the
-- board.
CREATE TABLE IF NOT EXISTS adp_history (
    season    INTEGER NOT NULL,
    name_key  TEXT NOT NULL,
    position  TEXT NOT NULL,
    player_name TEXT,
    team      TEXT,
    bye       INTEGER,
    rank      REAL,      -- consensus overall rank
    adp       REAL,      -- consensus ADP (average across sources)
    espn_adp  REAL,      -- ESPN's own ADP, where the source file carries it
    source    TEXT,
    PRIMARY KEY (season, name_key, position)
);
CREATE INDEX IF NOT EXISTS idx_adp_history_season ON adp_history(season, adp);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so they are applied by hand against the live table.
MIGRATIONS = {
    "players": {
        "espn_adp": "REAL",
        "vegas_proj": "REAL",
        "vegas_vor": "REAL",
    },
    "history": {
        "espn_player_id": "INTEGER",
        "market_rank": "REAL",
        "market_adp": "REAL",
        "name_key": "TEXT",
        "draft_slot": "INTEGER",
        "pos_taken": "INTEGER",
        "season_pts": "REAL",
        "pos_finish": "INTEGER",
    },
}


def _migrate(conn):
    for table, columns in MIGRATIONS.items():
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:                    # table not created yet
            continue
        for column, decl in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db():
    with session() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        if not conn.execute("SELECT 1 FROM settings WHERE key='league'").fetchone():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('league', ?)",
                (json.dumps(DEFAULT_LEAGUE),),
            )
        if not conn.execute("SELECT 1 FROM managers").fetchone():
            _seed_managers(conn, DEFAULT_LEAGUE["n_teams"], DEFAULT_LEAGUE["my_slot"])


def _seed_managers(conn, n_teams, my_slot):
    conn.execute("DELETE FROM managers")
    for slot in range(1, n_teams + 1):
        conn.execute(
            "INSERT INTO managers (name, draft_slot, is_me) VALUES (?, ?, ?)",
            (f"Team {slot}", slot, 1 if slot == my_slot else 0),
        )


def get_league(conn=None):
    if conn is None:
        with session() as c:
            return get_league(c)
    row = conn.execute("SELECT value FROM settings WHERE key='league'").fetchone()
    league = dict(DEFAULT_LEAGUE)
    if row:
        league.update(json.loads(row["value"]))
    return league


def set_league(conn, league):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('league', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps(league),),
    )


def reseed_managers(conn, n_teams, my_slot):
    """Resize the league. Only safe before picks exist."""
    _seed_managers(conn, n_teams, my_slot)
