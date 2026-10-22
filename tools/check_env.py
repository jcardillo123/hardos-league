"""Validate .env for the ESPN scraper without printing any secret values.

Reports whether each variable is present and whether it *looks* structurally
right. Only lengths, prefixes and shapes are shown — never a full token — so the
output is safe to paste into a chat or an issue.

    python tools/check_env.py
"""

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

SWID_RE = re.compile(r"^\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                     r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}$")


def line(status: str, name: str, detail: str) -> str:
    return f"  [{status}] {name:<16} {detail}"


def main() -> int:
    print(f"env file: {ENV_PATH}")
    if not ENV_PATH.exists():
        print("  [FAIL] .env not found. Copy .env.example to .env and fill it in.")
        return 1
    load_dotenv(ENV_PATH)

    problems = 0
    results = []

    league_id = (os.getenv("ESPN_LEAGUE_ID") or "").strip()
    if not league_id:
        results.append(line("FAIL", "ESPN_LEAGUE_ID", "missing"))
        problems += 1
    elif not league_id.isdigit():
        results.append(line(
            "FAIL", "ESPN_LEAGUE_ID",
            f"should be digits only, got {len(league_id)} chars starting "
            f"{league_id[:2]!r}",
        ))
        problems += 1
    else:
        results.append(line("ok", "ESPN_LEAGUE_ID", f"{league_id} ({len(league_id)} digits)"))

    s2 = os.getenv("ESPN_S2") or ""
    if not s2:
        results.append(line("--", "ESPN_S2", "not set (fine if the league is PUBLIC)"))
    else:
        notes = [f"{len(s2)} chars", f"starts {s2[:4]!r}"]
        if len(s2) < 100:
            notes.append("SUSPICIOUSLY SHORT - likely a truncated copy")
            problems += 1
        if s2 != s2.strip():
            notes.append("has leading/trailing whitespace")
            problems += 1
        if any(c in s2 for c in " \t\r\n"):
            notes.append("contains whitespace INSIDE the value - copy went wrong")
            problems += 1
        if s2.startswith(("'", '"')) or s2.endswith(("'", '"')):
            notes.append("quote characters survived into the value")
            problems += 1
        status = "ok" if len(notes) == 2 else "WARN"
        results.append(line(status, "ESPN_S2", ", ".join(notes)))

    swid = os.getenv("ESPN_SWID") or ""
    if not swid:
        results.append(line("--", "ESPN_SWID", "not set (fine if the league is PUBLIC)"))
    elif SWID_RE.match(swid.strip()):
        results.append(line("ok", "ESPN_SWID", f"well-formed GUID with braces ({len(swid)} chars)"))
    else:
        detail = f"{len(swid)} chars"
        if not swid.strip().startswith("{"):
            detail += ", missing the opening brace"
        if not swid.strip().endswith("}"):
            detail += ", missing the closing brace"
        results.append(line("FAIL", "ESPN_SWID", detail + ", expected {8-4-4-4-12}"))
        problems += 1

    odds = (os.getenv("ODDS_API_KEY") or "").strip()
    if not odds:
        results.append(line("--", "ODDS_API_KEY", "not set (Vegas lines disabled)"))
    else:
        notes = [f"{len(odds)} chars", f"starts {odds[:4]!r}"]
        if len(odds) < 16:
            notes.append("SUSPICIOUSLY SHORT for an Odds API key")
            problems += 1
        if not re.fullmatch(r"[0-9a-zA-Z]+", odds):
            notes.append("contains punctuation or whitespace - check the copy")
            problems += 1
        results.append(
            line("ok" if len(notes) == 2 else "WARN", "ODDS_API_KEY", ", ".join(notes))
        )

    print("\n".join(results))

    if (s2 and not swid) or (swid and not s2):
        print("\n  [FAIL] ESPN_S2 and ESPN_SWID must both be set, or neither.")
        problems += 1

    print()
    if problems:
        print(f"{problems} problem(s) found. Fix these before running DraftTool.py.")
    elif not s2:
        print("Looks fine for a PUBLIC league. If DraftTool.py returns 401, the "
              "league is private and you need the two cookies.")
    else:
        print("Looks fine. Run: python DraftTool.py --seasons 2022 2023 2024 2025")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
