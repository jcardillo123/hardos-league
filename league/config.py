"""Paths, ESPN id maps, and the league's shape."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "cache" / "league"
DB_PATH = DATA / "league.db"

for _d in (DATA, CACHE):
    _d.mkdir(parents=True, exist_ok=True)

# Shared with the draft tool. Same league, same credentials, same .env.
ENV_PATH = ROOT / ".env"

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"

# Seasons to pull. 2026 is in progress - `ingest` marks it incomplete and
# skips weeks that have not been played rather than storing zeros.
SEASONS = (2022, 2023, 2024, 2025, 2026)
CURRENT_SEASON = 2026

# ESPN's `/leagueHistory/` route serves matchup scores for old seasons but
# returns *no* per-player weekly rosters for them. The `/seasons/{year}/` route
# serves both, for every season back to 2022. Measured, not assumed: the
# leagueHistory route returned 0 player entries for 2022/2023/2024 while
# /seasons/ returned 185/184/224 for the same weeks. Always use /seasons/.
def season_url(league_id: str, season: int) -> str:
    return f"{BASE}/seasons/{season}/segments/0/leagues/{league_id}"


POSITION = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}

# ESPN lineup slot ids. Only the ones this league uses are labelled; the rest
# exist so an unexpected slot shows up as its number instead of vanishing.
SLOT = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE",
    7: "OP", 16: "DST", 17: "K", 18: "P", 19: "HC", 20: "BE", 21: "IR",
    23: "FLEX", 24: "EDR",
}
BENCH_SLOTS = (20, 21)      # not in the lineup, so they score nothing
IR_SLOT = 21                # and IR cannot be started at all - see stats.py

PRO_TEAM = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL",
    7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV",
    14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG",
    20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF",
    26: "SEA", 27: "TB", 28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL",
    34: "HOU",
}

# The starting lineup, as position -> count, plus how many FLEX slots draw from
# FLEX_POSITIONS. Read from ESPN per season at ingest; this is the fallback and
# the documented expectation. Confirmed identical across 2022-2026:
#   QB 1, RB 2, WR 2, TE 1, FLEX 2, K 1, DST 1, bench 8, IR 1
LINEUP = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DST": 1}
FLEX_SLOTS = 2
FLEX_POSITIONS = ("RB", "WR", "TE")

# `statSourceId` on a player's weekly stat line: 0 is what he actually scored,
# 1 is what ESPN projected before the week. Keeping both is what makes
# "started the wrong guy" separable from "nobody could have known".
STAT_ACTUAL = 0
STAT_PROJECTED = 1

STATIC = Path(__file__).resolve().parent / "static"

PORT = 8100     # the draft tool owns 8000
