"""
Pull multi-season draft history from an ESPN fantasy football league.

Setup:
    pip install requests pandas python-dotenv

    Create a .env file next to this script:
        ESPN_LEAGUE_ID=123456
        ESPN_S2=AEB...        # only needed for private leagues
        ESPN_SWID={XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}

    To get the cookies: log into fantasy.espn.com, open DevTools ->
    Application -> Cookies -> espn.com, and copy the espn_s2 and SWID
    values. Include the braces on SWID.

Usage:
    python espn_draft_history.py --seasons 2022 2023 2024 2025

Output:
    draft_history.csv   one row per pick, with owner names and player names
    league_settings.csv one row per season, roster + scoring config

Note: this API is undocumented and ESPN changes it without warning. Snapshot
your data early and keep the CSVs in version control.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

# Anchored to this file, not the working directory: otherwise running the script
# from anywhere but C:\ProjectFiles silently finds no .env and reports a
# confusing 401 instead of "your credentials were never loaded".
load_dotenv(Path(__file__).resolve().parent / ".env")

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
CURRENT_SEASON = 2026
POSITION_MAP = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
PRO_TEAM_MAP = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL",
    7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV",
    14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG",
    20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF",
    26: "SEA", 27: "TB", 28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL",
    34: "HOU",
}


def build_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept": "application/json",
    })
    espn_s2 = os.getenv("ESPN_S2")
    swid = os.getenv("ESPN_SWID")
    if espn_s2 and swid:
        session.cookies.update({"espn_s2": espn_s2, "SWID": swid})
    return session


def season_url(league_id, season):
    """Current-format seasons use the /seasons/ path; older ones use
    /leagueHistory/ and return a single-element list instead of an object."""
    if season >= CURRENT_SEASON - 1:
        return f"{BASE}/seasons/{season}/segments/0/leagues/{league_id}", False
    return f"{BASE}/leagueHistory/{league_id}?seasonId={season}", True


def fetch_league(session, league_id, season, views):
    url, is_history = season_url(league_id, season)
    sep = "&" if "?" in url else "?"
    url = url + sep + "&".join(f"view={v}" for v in views)

    resp = session.get(url, timeout=30)
    if resp.status_code == 401:
        raise SystemExit(
            f"401 for season {season}. Private league - check ESPN_S2 and "
            "ESPN_SWID in your .env, and confirm the SWID includes braces."
        )
    if resp.status_code == 404:
        print(f"  season {season}: not found, skipping", file=sys.stderr)
        return None
    resp.raise_for_status()

    data = resp.json()
    if is_history or isinstance(data, list):
        if not data:
            return None
        data = data[0]
    return data


def fetch_player_names(session, league_id, season, player_ids):
    """kona_player_info needs an X-Fantasy-Filter header. Chunk the id list -
    very large filters get rejected."""
    names = {}
    ids = sorted(set(int(p) for p in player_ids if p and int(p) > 0))
    url, is_history = season_url(league_id, season)
    sep = "&" if "?" in url else "?"
    url = url + sep + "view=kona_player_info"

    for i in range(0, len(ids), 250):
        chunk = ids[i:i + 250]
        filt = {"players": {"filterIds": {"value": chunk}, "limit": len(chunk)}}
        resp = session.get(
            url, timeout=30,
            headers={"X-Fantasy-Filter": json.dumps(filt)},
        )
        if resp.status_code != 200:
            print(f"  name lookup failed ({resp.status_code}), continuing",
                  file=sys.stderr)
            continue
        payload = resp.json()
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        for entry in payload.get("players", []):
            p = entry.get("player", {})
            names[entry.get("id")] = {
                "player_name": p.get("fullName"),
                "position": POSITION_MAP.get(p.get("defaultPositionId")),
                "pro_team": PRO_TEAM_MAP.get(p.get("proTeamId")),
            }
        time.sleep(0.4)
    return names


def owner_lookup(data):
    """teamId -> a stable human label. Manager display names change between
    seasons; team names change constantly. Prefer the owner GUID as the join
    key and carry the label along for readability."""
    members = {m["id"]: m for m in data.get("members", [])}
    out = {}
    for team in data.get("teams", []):
        owners = team.get("owners") or []
        guid = owners[0] if owners else None
        member = members.get(guid, {})
        label = " ".join(
            filter(None, [member.get("firstName"), member.get("lastName")])
        ) or member.get("displayName") or team.get("name") or f"team_{team['id']}"
        out[team["id"]] = {"owner_guid": guid, "owner_label": label.strip()}
    return out


def extract_season(session, league_id, season):
    print(f"season {season}...", file=sys.stderr)
    data = fetch_league(
        session, league_id, season,
        views=["mDraftDetail", "mTeam", "mSettings"],
    )
    if data is None:
        return None, None

    picks = data.get("draftDetail", {}).get("picks", [])
    if not picks:
        print(f"  no picks found for {season}", file=sys.stderr)
        return None, None

    owners = owner_lookup(data)
    names = fetch_player_names(
        session, league_id, season, [p.get("playerId") for p in picks]
    )

    rows = []
    for pick in picks:
        team_id = pick.get("teamId")
        owner = owners.get(team_id, {})
        info = names.get(pick.get("playerId"), {})
        rows.append({
            "season": season,
            "overall_pick": pick.get("overallPickNumber"),
            "round": pick.get("roundId"),
            "round_pick": pick.get("roundPickNumber"),
            "team_id": team_id,
            "owner_guid": owner.get("owner_guid"),
            "owner_label": owner.get("owner_label"),
            "espn_player_id": pick.get("playerId"),
            "player_name": info.get("player_name"),
            "position": info.get("position"),
            "pro_team": info.get("pro_team"),
            "keeper": pick.get("keeper", False),
            "auction_bid": pick.get("bidAmount"),
        })

    settings = data.get("settings", {})
    roster = settings.get("rosterSettings", {}).get("lineupSlotCounts", {})
    draft_cfg = settings.get("draftSettings", {})
    season_settings = {
        "season": season,
        "n_teams": len(data.get("teams", [])),
        "draft_type": draft_cfg.get("type"),
        "scoring_type": settings.get("scoringSettings", {}).get("scoringType"),
        "roster_slots": json.dumps(
            {k: v for k, v in roster.items() if v > 0}
        ),
    }
    return pd.DataFrame(rows), season_settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seasons", nargs="+", type=int, required=True)
    parser.add_argument("--league-id", default=os.getenv("ESPN_LEAGUE_ID"))
    parser.add_argument("--outdir", default=".")
    args = parser.parse_args()

    if not args.league_id:
        raise SystemExit("Set ESPN_LEAGUE_ID in .env or pass --league-id")

    session = build_session()
    frames, settings = [], []
    for season in sorted(args.seasons):
        df, cfg = extract_season(session, args.league_id, season)
        if df is not None:
            frames.append(df)
            settings.append(cfg)
        time.sleep(1.0)

    if not frames:
        raise SystemExit("No draft data retrieved.")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    picks = pd.concat(frames, ignore_index=True).sort_values(
        ["season", "overall_pick"]
    )
    picks.to_csv(outdir / "draft_history.csv", index=False)
    pd.DataFrame(settings).to_csv(outdir / "league_settings.csv", index=False)

    missing = picks["player_name"].isna().sum()
    print(
        f"\n{len(picks)} picks across {picks['season'].nunique()} seasons, "
        f"{picks['owner_guid'].nunique()} distinct owners"
        + (f"  ({missing} unresolved player names)" if missing else ""),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()