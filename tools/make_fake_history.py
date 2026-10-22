"""Generate a SYNTHETIC league draft history with known, planted tendencies.

This is a development fixture, not data about anybody's real league. It exists
because the tendency engine cannot be checked against real history until you
have pulled yours from ESPN — and because an engine that finds "tendencies" in
noise is worse than no engine. Each fake owner is given one deliberate habit,
and `python -m app.tendencies` should recover it.

    python tools/make_fake_history.py fake_draft_history.csv
    python -m app.ingest --history fake_draft_history.csv
    python -m app.tendencies

Expected in the report:

    Early QB Eric      first taken: QB R1, early-phase QB well above league
    Zero RB Zoe        first taken: RB R5+, early-phase RB well below league
    TE Tom             first taken: TE R1, early-phase TE above league
    Run Chaser Randy   chases runs, ~80% vs a league rate near 43%
    Balanced B*        cluster near the league base rates

Real player names come from nflverse so the outcome join runs the same code path
it will on real data. Clear the fixture out before a real draft:

    DELETE FROM history;
"""

import random
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.ingest import load_season_stats  # noqa: E402

SEASONS = [2022, 2023, 2024, 2025]
ROUNDS = 16
random.seed(20260808)

OWNERS = {
    "Early QB Eric": "early_qb",
    "Zero RB Zoe": "zero_rb",
    "TE Tom": "early_te",
    "Run Chaser Randy": "run_chaser",
    "Balanced Bill": "balanced",
    "Balanced Beth": "balanced",
    "Balanced Ben": "balanced",
    "Balanced Bree": "balanced",
    "Balanced Bob": "balanced",
    "Balanced Bea": "balanced",
    "Balanced Brad": "balanced",
    "Balanced Britt": "balanced",
}
GUIDS = {name: f"{{FAKE-{i:04d}}}" for i, name in enumerate(OWNERS)}
ROSTER_CAPS = {"QB": 3, "RB": 6, "WR": 7, "TE": 3, "K": 1}


def pool_for(season):
    """Draftable players in *preseason expectation* order.

    Ordering strictly by what they actually scored would make the Nth player
    taken at a position always finish Nth, so `pos_taken - pos_finish` would be
    identically zero and the outcome metric would never be exercised. Real
    drafts order by expectation, which diverges from the result, so noise is
    added to reproduce that divergence.
    """
    stats = load_season_stats(season).rename(columns={"pts_ppr": "pts"})
    stats = stats[stats["position"].isin(("QB", "RB", "WR", "TE", "K"))]
    stats = stats[stats["pts"] > 0]
    by_pos = {}
    for pos in ROSTER_CAPS:
        rows = stats[stats["position"] == pos].nlargest(60, "pts")
        spread = rows["pts"].std() or 1.0
        players = [
            {
                "name": r.display_name,
                "team": r.team,
                "expected": r.pts + random.gauss(0, spread * 0.55),
            }
            for r in rows.itertuples()
        ]
        players.sort(key=lambda p: p["expected"], reverse=True)
        by_pos[pos] = players
    return by_pos


def choose_position(style, rnd, roster, recent):
    if rnd >= 15 and roster.get("K", 0) < 1:
        return "K"
    if style == "early_qb" and roster.get("QB", 0) == 0 and rnd <= 2:
        return "QB"
    if style == "zero_rb" and rnd < 5:
        return random.choice(["WR", "WR", "WR", "TE", "QB"])
    if style == "early_te" and roster.get("TE", 0) == 0 and rnd <= 3:
        return "TE"
    if style == "run_chaser" and len(recent) >= 2 and recent[-1] == recent[-2]:
        if random.random() < 0.8:
            return recent[-1]

    if rnd <= 3:
        return random.choices(["RB", "WR", "TE", "QB"], [45, 42, 8, 5])[0]
    if rnd <= 8:
        return random.choices(["RB", "WR", "QB", "TE"], [30, 38, 18, 14])[0]
    return random.choices(["WR", "RB", "QB", "TE"], [38, 32, 18, 12])[0]


def run_season(season, pool):
    slots = list(OWNERS)
    random.shuffle(slots)                   # seats change year to year
    slot_of = {name: i + 1 for i, name in enumerate(slots)}

    taken, rosters, recent, rows = set(), {n: {} for n in OWNERS}, [], []
    overall = 0

    for rnd in range(1, ROUNDS + 1):
        order = slots if rnd % 2 else list(reversed(slots))
        for round_pick, owner in enumerate(order, start=1):
            overall += 1
            for _ in range(12):             # retry until a legal position
                pos = choose_position(OWNERS[owner], rnd, rosters[owner], recent)
                if rosters[owner].get(pos, 0) < ROSTER_CAPS[pos]:
                    break
            else:
                pos = "WR"

            player = next((p for p in pool[pos] if p["name"] not in taken), None)
            if player is None:
                continue
            taken.add(player["name"])
            rosters[owner][pos] = rosters[owner].get(pos, 0) + 1
            recent.append(pos)

            rows.append({
                "season": season,
                "overall_pick": overall,
                "round": rnd,
                "round_pick": round_pick,
                "team_id": slot_of[owner],
                "owner_guid": GUIDS[owner],
                "owner_label": owner,
                "espn_player_id": abs(hash(player["name"])) % 999999,
                "player_name": player["name"],
                "position": pos,
                "pro_team": player["team"],
                # A couple of keepers, to prove they get excluded.
                "keeper": bool(rnd == 1 and owner == "Balanced Bill" and season >= 2024),
                "auction_bid": None,
            })
    return rows


def main():
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "fake_draft_history.csv")
    rows = []
    for season in SEASONS:
        print(f"season {season}...", file=sys.stderr)
        rows.extend(run_season(season, pool_for(season)))
    out = pd.DataFrame(rows)
    out.to_csv(dest, index=False)
    print(f"wrote {len(out)} synthetic picks to {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
