"""SQLite persistence for league history.

Everything here is derived from ESPN and can be refetched, so the database is
a cache rather than a system of record - `ingest` is safe to re-run and
replaces a season at a time.
"""

import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
-- One row per season, with the roster shape that applied *that* season. The
-- league grew from 10 teams to 12 in 2024, so almost every aggregate has to be
-- normalized by something stored here rather than by a constant.
CREATE TABLE IF NOT EXISTS seasons (
    season         INTEGER PRIMARY KEY,
    league_name    TEXT,
    n_teams        INTEGER,
    reg_weeks      INTEGER,     -- matchup periods before the playoffs
    total_weeks    INTEGER,     -- including playoff rounds
    playoff_teams  INTEGER,
    scoring_type   TEXT,
    roster_slots   TEXT,        -- JSON: slot label -> count
    complete       INTEGER NOT NULL DEFAULT 1,
    fetched_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Managers are identified by ESPN owner GUID, not by team name or display name.
-- Team names change every year and display names change on a whim; the GUID is
-- the only thing that reliably says "this is the same person as last season".
CREATE TABLE IF NOT EXISTS owners (
    owner_guid TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    first_seen INTEGER,
    last_seen  INTEGER,
    seasons    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS teams (
    season        INTEGER NOT NULL,
    team_id       INTEGER NOT NULL,
    owner_guid    TEXT,
    owner_label   TEXT,
    team_name     TEXT,
    abbrev        TEXT,
    division_id   INTEGER,
    wins          INTEGER,
    losses        INTEGER,
    ties          INTEGER,
    points_for    REAL,
    points_against REAL,
    final_rank    INTEGER,     -- where they actually finished
    playoff_seed  INTEGER,
    PRIMARY KEY (season, team_id)
);
CREATE INDEX IF NOT EXISTS idx_teams_owner ON teams(owner_guid);

CREATE TABLE IF NOT EXISTS matchups (
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    matchup_id   INTEGER NOT NULL,
    home_team    INTEGER,
    away_team    INTEGER,
    home_points  REAL,
    away_points  REAL,
    winner       TEXT,          -- HOME | AWAY | TIE | UNDECIDED
    playoff_tier TEXT,
    is_playoff   INTEGER NOT NULL DEFAULT 0,
    is_bye       INTEGER NOT NULL DEFAULT 0,   -- playoff bye: one side is null
    PRIMARY KEY (season, week, matchup_id)
);
CREATE INDEX IF NOT EXISTS idx_matchups_week ON matchups(season, week);

-- One row per rostered player per team per week, started or not. This is the
-- table that makes the interesting questions answerable: what a manager left on
-- the bench is only visible if the bench is stored too.
CREATE TABLE IF NOT EXISTS lineups (
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    team_id     INTEGER NOT NULL,
    player_id   INTEGER NOT NULL,
    player_name TEXT,
    position    TEXT,
    pro_team    TEXT,
    lineup_slot INTEGER,
    slot_label  TEXT,
    started     INTEGER NOT NULL DEFAULT 0,
    points      REAL,
    projected   REAL,
    PRIMARY KEY (season, week, team_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_lineups_team ON lineups(season, week, team_id);
CREATE INDEX IF NOT EXISTS idx_lineups_player ON lineups(player_id);

-- Derived at ingest from `lineups` + `matchups`. Could be a view, but the
-- optimal-lineup solve is not expressible in useful SQL and every page wants
-- these numbers, so they are computed once and stored.
CREATE TABLE IF NOT EXISTS team_weeks (
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    team_id      INTEGER NOT NULL,
    opp_team     INTEGER,
    actual       REAL,       -- what the starting lineup scored
    optimal      REAL,       -- the best lineup available from that roster
    bench_points REAL,       -- points that sat (excludes IR, which cannot start)
    opp_actual   REAL,
    result       TEXT,       -- W | L | T
    is_playoff   INTEGER NOT NULL DEFAULT 0,
    all_play_w   INTEGER,    -- record vs every other team's score that week
    all_play_l   INTEGER,
    all_play_t   INTEGER,
    PRIMARY KEY (season, week, team_id)
);
CREATE INDEX IF NOT EXISTS idx_tw_season ON team_weeks(season, team_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
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


def init_db():
    with session() as conn:
        conn.executescript(SCHEMA)


def replace_season(conn, season: int):
    """Drop a season's rows so it can be re-ingested idempotently.

    Scoped to one season so a failed 2026 refresh cannot damage 2022-2025.
    """
    for table in ("teams", "matchups", "lineups", "team_weeks"):
        conn.execute(f"DELETE FROM {table} WHERE season = ?", (season,))
    conn.execute("DELETE FROM seasons WHERE season = ?", (season,))
