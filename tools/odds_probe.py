"""Discover what a the-odds-api.com plan actually exposes for NFL, without
spending credits unless told to.

## Why this is a probe and not an importer

The valuable Vegas input for a draft is a season-long player projection - passing
yards, rushing yards, receptions - which converts directly into fantasy points.
Whether those markets exist on this plan, and under which sport key, is not
something to assume. Nor is the credit cost: The Odds API meters requests, and
per-event player-prop requests are the expensive kind.

So this runs in two modes:

  * default        only the endpoints that are FREE and do not count against quota
  * --spend        also fetch odds, which costs credits; prints the cost first

Usage:
    python tools/odds_probe.py                     # free discovery only
    python tools/odds_probe.py --spend --sport americanfootball_nfl
    python tools/odds_probe.py --spend --sport americanfootball_nfl --props
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

BASE = "https://api.the-odds-api.com/v4"

# Season-long player markets, if the plan carries them. Named per The Odds API's
# convention; the probe reports which are actually accepted rather than assuming.
SEASON_PLAYER_MARKETS = [
    "player_pass_yds", "player_pass_tds", "player_rush_yds", "player_rush_tds",
    "player_reception_yds", "player_receptions", "player_rush_reception_yds",
]
GAME_MARKETS = ["h2h", "spreads", "totals"]


def call(path: str, params: dict) -> tuple[object, dict]:
    """GET a path. Returns (payload, response headers)."""
    query = urllib.parse.urlencode(params)
    url = f"{BASE}{path}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "draft-tool"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.load(resp), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:400]
        return {"_error": exc.code, "_body": body}, dict(exc.headers or {})


def quota(headers: dict) -> str:
    used = headers.get("x-requests-used", "?")
    left = headers.get("x-requests-remaining", "?")
    last = headers.get("x-requests-last", "?")
    return f"used {used}, remaining {left}, this call cost {last}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spend", action="store_true",
                        help="allow requests that consume API credits")
    parser.add_argument("--sport", default=None,
                        help="sport key to inspect (default: every NFL key found)")
    parser.add_argument("--props", action="store_true",
                        help="with --spend, also try season-long player markets")
    parser.add_argument("--regions", default="us")
    args = parser.parse_args()

    key = os.getenv("ODDS_API_KEY")
    if not key:
        raise SystemExit(
            "ODDS_API_KEY is not set. Add it to C:\\ProjectFiles\\.env, then run "
            "tools/check_env.py to confirm. Never paste the key into chat."
        )

    # ---- free: which sports exist, and what is my quota ------------------- #
    print("=== sports list (free, does not count against quota) ===")
    sports, headers = call("/sports", {"apiKey": key, "all": "true"})
    if isinstance(sports, dict) and sports.get("_error"):
        raise SystemExit(
            f"HTTP {sports['_error']} from /sports - key rejected?\n{sports['_body']}"
        )
    print(f"  quota: {quota(headers)}")
    print(f"  {len(sports)} sports visible on this plan")

    football = [s for s in sports if "americanfootball" in s.get("key", "")]
    print(f"\n  {len(football)} american-football keys:")
    for s in football:
        active = "active" if s.get("active") else "inactive"
        outrights = " OUTRIGHTS/FUTURES" if s.get("has_outrights") else ""
        print(f"    {s['key']:<44} {active:<9}{outrights}  {s.get('title')}")

    nfl_keys = [s["key"] for s in football if "nfl" in s["key"]]
    targets = [args.sport] if args.sport else nfl_keys

    # ---- free: what events exist under each NFL key ----------------------- #
    print("\n=== events per NFL key (free) ===")
    for sport in targets:
        events, headers = call(f"/sports/{sport}/events", {"apiKey": key})
        if isinstance(events, dict) and events.get("_error"):
            print(f"  {sport:<44} HTTP {events['_error']}")
            continue
        print(f"  {sport:<44} {len(events)} events")
        for event in events[:3]:
            print(f"      {event.get('commence_time', '?')[:10]}  "
                  f"{event.get('away_team')} @ {event.get('home_team')}")

    if not args.spend:
        print(
            "\n=== stopping here ===\n"
            "Everything above was free. Fetching actual odds costs credits\n"
            "(1 per market per region for game odds; player props are charged\n"
            "per event and are much more expensive).\n\n"
            "When you are ready:\n"
            "  python tools/odds_probe.py --spend --sport americanfootball_nfl\n"
            "  python tools/odds_probe.py --spend --sport americanfootball_nfl --props"
        )
        return

    # ---- costs credits from here ------------------------------------------ #
    for sport in targets:
        print(f"\n=== {sport}: game markets (COSTS CREDITS) ===")
        payload, headers = call("/sports/%s/odds" % sport, {
            "apiKey": key, "regions": args.regions,
            "markets": ",".join(GAME_MARKETS), "oddsFormat": "american",
        })
        print(f"  quota: {quota(headers)}")
        if isinstance(payload, dict) and payload.get("_error"):
            print(f"  HTTP {payload['_error']}: {payload['_body'][:200]}")
        else:
            print(f"  {len(payload)} events with odds")
            if payload:
                sample = payload[0]
                books = [b["key"] for b in sample.get("bookmakers", [])]
                print(f"  sample: {sample.get('away_team')} @ {sample.get('home_team')}")
                print(f"  bookmakers: {books[:6]}")
                for book in sample.get("bookmakers", [])[:1]:
                    for market in book.get("markets", []):
                        outcomes = ", ".join(
                            f"{o.get('name')} {o.get('point', o.get('price'))}"
                            for o in market.get("outcomes", [])[:3]
                        )
                        print(f"    {market['key']:<10} {outcomes}")

        if not args.props:
            continue

        print(f"\n=== {sport}: season-long player markets (COSTS MORE) ===")
        events, _ = call(f"/sports/{sport}/events", {"apiKey": key})
        if isinstance(events, dict) or not events:
            print("  no events to attach player markets to")
            continue
        event_id = events[0]["id"]
        for market in SEASON_PLAYER_MARKETS:
            payload, headers = call(
                f"/sports/{sport}/events/{event_id}/odds",
                {"apiKey": key, "regions": args.regions, "markets": market,
                 "oddsFormat": "american"},
            )
            if isinstance(payload, dict) and payload.get("_error"):
                print(f"  {market:<28} HTTP {payload['_error']} "
                      f"{payload['_body'][:90]}")
                continue
            books = payload.get("bookmakers", []) if isinstance(payload, dict) else []
            count = sum(len(m.get("outcomes", []))
                        for b in books for m in b.get("markets", []))
            print(f"  {market:<28} {len(books)} books, {count} outcomes  "
                  f"({quota(headers)})")


if __name__ == "__main__":
    main()
