"""ESPN fantasy API client.

Built on the patterns already proven in DraftTool.py - same undocumented v3 API,
same cookie auth, same habit of returning a single-element list instead of an
object. Two things are different here:

  * every request goes through `/seasons/{year}/`, never `/leagueHistory/`,
    because only the former serves per-player weekly rosters for past seasons
  * responses are cached to disk, so re-running ingest costs nothing and a
    schema change does not mean re-pulling five seasons
"""

import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from .config import CACHE, ENV_PATH, season_url

load_dotenv(ENV_PATH)

_THROTTLE = 0.35        # seconds between live requests


class EspnError(RuntimeError):
    pass


def league_id() -> str:
    lid = os.getenv("ESPN_LEAGUE_ID")
    if not lid:
        raise EspnError(
            "ESPN_LEAGUE_ID is not set. It lives in .env next to the draft "
            "tool's; run `python -m tools.check_env` to verify."
        )
    return lid


def build_session() -> requests.Session:
    """A requests session carrying the league cookies.

    Credential values are never logged or returned - only whether they loaded.
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept": "application/json",
    })
    s2, swid = os.getenv("ESPN_S2"), os.getenv("ESPN_SWID")
    if s2 and swid:
        s.cookies.update({"espn_s2": s2, "SWID": swid})
    return s


def has_credentials() -> bool:
    return bool(os.getenv("ESPN_S2") and os.getenv("ESPN_SWID"))


def _cache_path(season: int, views: tuple, week: int | None) -> Path:
    tag = "+".join(sorted(v.replace("m", "", 1) for v in views))
    name = f"{season}_{tag}" + (f"_wk{week}" if week else "") + ".json"
    return CACHE / name


def fetch(session, season: int, views, week: int | None = None,
          refresh: bool = False) -> dict | None:
    """One league request, memoized on disk.

    `week` maps to ESPN's scoringPeriodId, which is what makes the response
    carry that week's rosters rather than the current one's.
    """
    views = tuple(views)
    path = _cache_path(season, views, week)
    if path.exists() and not refresh:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            path.unlink(missing_ok=True)    # truncated write, refetch

    url = season_url(league_id(), season) + "?"
    url += "&".join(f"view={v}" for v in views)
    if week is not None:
        url += f"&scoringPeriodId={week}"

    resp = session.get(url, timeout=30)
    time.sleep(_THROTTLE)

    if resp.status_code == 401:
        raise EspnError(
            f"401 on season {season}. The league is private and the cookies "
            "were rejected - ESPN_S2 and ESPN_SWID in .env have probably "
            "expired. Re-copy them from the browser; SWID keeps its braces."
        )
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise EspnError(f"HTTP {resp.status_code} on season {season} {views}")

    data = resp.json()
    if isinstance(data, list):
        data = data[0] if data else None
    if data is None:
        return None

    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def owner_labels(data: dict) -> dict:
    """teamId -> {owner_guid, owner_label}.

    Prefers the member's real name over the team name, because team names are
    jokes that change every year and this has to join across five seasons.
    """
    members = {m["id"]: m for m in data.get("members", [])}
    out = {}
    for team in data.get("teams", []):
        owners = team.get("owners") or []
        guid = owners[0] if owners else team.get("primaryOwner")
        member = members.get(guid, {})
        label = (
            " ".join(filter(None, [member.get("firstName"),
                                   member.get("lastName")])).strip()
            or member.get("displayName")
            or team.get("name")
            or f"team {team['id']}"
        )
        out[team["id"]] = {"owner_guid": guid, "owner_label": label}
    return out
