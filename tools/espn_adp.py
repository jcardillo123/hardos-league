"""Pull ESPN's platform-wide ADP and draft ranks.

## Why this is written as a probe

The ADP fields live behind `kona_player_info` — the same view whose player-name
lookup already failed on this league, which is why every imported pick came back
with a null position. So this tool does not assume any single endpoint works. It
tries several URL + view + filter combinations, reports exactly what each one
returned, and uses the first that yields usable ADP.

## The open question it answers

Current-season ADP is what ESPN shows in a live draft room, so it is the number
your leaguemates are actually looking at — arguably a better model of their
behavior than any expert ranking.

Whether *historical* ADP is retrievable is genuinely unknown. ESPN may return
present-day ownership figures for a past-season request rather than what the ADP
was at the time, in which case it is useless for scoring past picks. Run this
against an old season and look at the reported ADP range: plausible spread means
real historical data, all-zeros or values identical to the current season means
ESPN is not serving history.

Usage:
    python tools/espn_adp.py --seasons 2026
    python tools/espn_adp.py --seasons 2022 2023 2024 2025 2026 --outdir data/adp
"""

import argparse
import json
import sys
import time
from pathlib import Path
import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import os  # noqa: E402

from app.ingest import ESPN_PRO_TEAM, normalize_position  # noqa: E402

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
CURRENT_SEASON = 2026
POSITION_MAP = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}


def build_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
        "X-Fantasy-Source": "kona",
        "X-Fantasy-Platform": "kona-PROD",
    })
    s2, swid = os.getenv("ESPN_S2"), os.getenv("ESPN_SWID")
    if s2 and swid:
        session.cookies.update({"espn_s2": s2, "SWID": swid})
    return session


def candidate_requests(league_id, season, scoring):
    """Endpoint shapes to try.

    Measured, not guessed: the standalone /seasons/{season}/players endpoint
    returns 11,534 players unauthenticated but carries NO `ownership` and NO
    `draftRanksByRankType` under any view (players_wl, kona_player_info, or no
    view at all), and it ignores the filter's `limit`. It is a fine name and
    position resolver — see app.ingest.espn_player_universe, which uses it — but
    it cannot supply ADP.

    ADP exists only on the league-scoped kona_player_info view, which requires
    valid ESPN_S2 / ESPN_SWID cookies even when the league itself is public.
    """
    rank_type = "PPR" if scoring == "ppr" else "STANDARD"
    sort_ranks = {
        "sortDraftRanks": {
            "sortPriority": 100, "sortAsc": True, "value": rank_type
        },
        "limit": 600,
        "offset": 0,
    }
    sort_owned = {
        "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        "limit": 600,
        "offset": 0,
    }

    if season >= CURRENT_SEASON - 1:
        league_url = (
            f"{BASE}/seasons/{season}/segments/0/leagues/{league_id}"
            "?view=kona_player_info"
        )
    else:
        league_url = (
            f"{BASE}/leagueHistory/{league_id}?seasonId={season}"
            "&view=kona_player_info"
        )

    return [
        ("kona_player_info + sortDraftRanks", league_url, {"players": sort_ranks}),
        ("kona_player_info + sortPercOwned", league_url, {"players": sort_owned}),
    ]


def extract_players(payload):
    """Normalize the several shapes ESPN returns into a flat player list."""
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict) and "players" in payload[0]:
            payload = payload[0]
        else:
            return [p.get("player", p) for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        entries = payload.get("players")
        if entries is None:
            return []
        return [e.get("player", e) for e in entries]
    return []


def rows_from(players, season, scoring):
    rank_type = "PPR" if scoring == "ppr" else "STANDARD"
    rows = []
    for player in players:
        if not isinstance(player, dict):
            continue
        ownership = player.get("ownership") or {}
        ranks = player.get("draftRanksByRankType") or {}
        chosen = ranks.get(rank_type) or {}
        pid = player.get("id")
        pos = POSITION_MAP.get(player.get("defaultPositionId"))
        name = player.get("fullName")
        if pid is not None and pid < 0 and not name:
            abbr = ESPN_PRO_TEAM.get(-16000 - pid)
            name, pos = (f"{abbr} D/ST", "DST") if abbr else (name, pos)
        rows.append({
            "season": season,
            "espn_player_id": pid,
            "player_name": name,
            "position": normalize_position(pos),
            "pro_team": ESPN_PRO_TEAM.get(player.get("proTeamId")),
            "espn_adp": ownership.get("averageDraftPosition"),
            "percent_owned": ownership.get("percentOwned"),
            "percent_started": ownership.get("percentStarted"),
            "auction_value": player.get("draftAuctionValue"),
            "espn_draft_rank": chosen.get("rank"),
            "rank_type": rank_type,
        })
    return rows


def usable(rows):
    """How many rows carry a plausible ADP. Zero means the endpoint gave nothing."""
    values = [
        r["espn_adp"] for r in rows
        if isinstance(r["espn_adp"], (int, float)) and r["espn_adp"] > 0
    ]
    return values


def fetch_season(session, league_id, season, scoring, verbose=True):
    for label, url, filt in candidate_requests(league_id, season, scoring):
        try:
            resp = session.get(
                url, timeout=45,
                headers={"X-Fantasy-Filter": json.dumps(filt)},
            )
        except requests.RequestException as exc:
            print(f"    {label:36} network error: {exc}", file=sys.stderr)
            continue

        if resp.status_code != 200:
            hint = ""
            if resp.status_code == 401:
                hint = ("  <- needs ESPN_S2 + ESPN_SWID in .env; this view is "
                        "authenticated even for public leagues")
            elif resp.status_code == 404:
                hint = "  <- league did not exist that season"
            print(f"    {label:36} HTTP {resp.status_code}{hint}", file=sys.stderr)
            time.sleep(1.0)
            continue

        try:
            players = extract_players(resp.json())
        except ValueError:
            print(f"    {label:36} non-JSON response", file=sys.stderr)
            continue

        rows = rows_from(players, season, scoring)
        adp_values = usable(rows)
        print(
            f"    {label:36} {len(rows):4d} players, {len(adp_values):4d} with ADP",
            file=sys.stderr,
        )
        if verbose and players:
            sample_keys = sorted(players[0].keys())[:12]
            print(f"      sample player keys: {sample_keys}", file=sys.stderr)

        if adp_values:
            print(
                f"      ADP range {min(adp_values):.1f} - {max(adp_values):.1f}",
                file=sys.stderr,
            )
            return rows, label
        time.sleep(1.0)
    return [], None


def main():
    parser = argparse.ArgumentParser(description="Fetch ESPN ADP / draft ranks")
    parser.add_argument("--seasons", nargs="+", type=int, default=[CURRENT_SEASON])
    parser.add_argument("--league-id", default=os.getenv("ESPN_LEAGUE_ID"))
    parser.add_argument("--scoring", default="ppr", choices=["ppr", "standard"])
    parser.add_argument("--outdir", type=Path, default=ROOT / "data" / "adp")
    args = parser.parse_args()

    if not args.league_id:
        raise SystemExit("Set ESPN_LEAGUE_ID in .env or pass --league-id")

    session = build_session()
    args.outdir.mkdir(parents=True, exist_ok=True)
    summary = []

    for season in sorted(args.seasons):
        print(f"season {season}:", file=sys.stderr)
        rows, label = fetch_season(session, args.league_id, season, args.scoring)
        if not rows:
            print("    no endpoint returned usable ADP\n", file=sys.stderr)
            summary.append({"season": season, "rows": 0, "via": None})
            continue

        df = pd.DataFrame(rows)
        df = df[df["espn_player_id"].notna()].drop_duplicates("espn_player_id")
        df = df.sort_values("espn_adp", na_position="last")
        dest = args.outdir / f"espn_adp_{season}.csv"
        df.to_csv(dest, index=False)
        print(f"    wrote {len(df)} rows -> {dest}\n", file=sys.stderr)
        summary.append({
            "season": season,
            "rows": len(df),
            "via": label,
            "adp_min": df["espn_adp"].min(),
            "adp_max": df["espn_adp"].max(),
            "top5": df.head(5)["player_name"].tolist(),
        })
        time.sleep(1.5)

    print("SUMMARY", file=sys.stderr)
    for entry in summary:
        if not entry["rows"]:
            print(f"  {entry['season']}  FAILED", file=sys.stderr)
            continue
        print(
            f"  {entry['season']}  {entry['rows']:4d} rows via {entry['via']}\n"
            f"        ADP {entry['adp_min']:.1f}-{entry['adp_max']:.1f}  "
            f"top: {', '.join(str(n) for n in entry['top5'])}",
            file=sys.stderr,
        )
    print(
        "\nIf an older season's top-5 matches the current season's, ESPN served "
        "present-day ADP rather than that year's - it is not usable as history.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
