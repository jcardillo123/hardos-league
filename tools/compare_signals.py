"""Compare ECR vs ADP as the projection's board-availability signal.

The projection multiplies three terms: historical tendency, roster need, and
board availability. Only the third depends on a ranking, so this isolates the
question "does it matter which ranking we use?" by holding the draft state fixed
and swapping only that term.

    python tools/compare_signals.py                # next 12 picks from live state
    python tools/compare_signals.py --limit 24
    python tools/compare_signals.py --quiet        # summary only

Reads the live draft state and does not modify it.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, tendencies as T                            # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=12,
                        help="how many upcoming picks to compare")
    parser.add_argument("--quiet", action="store_true",
                        help="summary only, no per-pick detail")
    args = parser.parse_args()

    db.init_db()
    with db.session() as conn:
        pool = conn.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN adp IS NOT NULL THEN 1 ELSE 0 END) "
            "AS adp FROM players"
        ).fetchone()
        if not pool["adp"]:
            raise SystemExit(
                "No ADP in the pool. Run tools/espn_adp.py, then "
                "python -m app.ingest."
            )
        print(f"pool: {pool['n']} players, {pool['adp']} with ADP")

        ecr = T.project_upcoming(conn, args.limit, signal="ecr")
        adp = T.project_upcoming(conn, args.limit, signal="adp")

    if not ecr["picks"]:
        raise SystemExit("No upcoming picks - is the draft complete?")

    total_abs, cells, flips, fell_back = 0.0, 0, 0, set()
    biggest = []

    for left, right in zip(ecr["picks"], adp["picks"]):
        lmap = {d["position"]: d["p"] for d in left["positions"]}
        rmap = {d["position"]: d["p"] for d in right["positions"]}
        fell_back.update((right.get("availability") or {}).get("fell_back_to_ecr", []))

        top_ecr = max(lmap, key=lmap.get)
        top_adp = max(rmap, key=rmap.get)
        flipped = top_ecr != top_adp
        flips += 1 if flipped else 0

        for pos in T.POSITIONS:
            delta = rmap.get(pos, 0.0) - lmap.get(pos, 0.0)
            total_abs += abs(delta)
            cells += 1
            biggest.append((abs(delta), pos, left["manager_name"],
                            left["overall"], lmap.get(pos, 0), rmap.get(pos, 0)))

        if not args.quiet:
            marker = "  <-- TOP PICK FLIPS" if flipped else ""
            print(f"\n#{left['overall']:<3} R{left['round']:<2} "
                  f"{left['manager_name']}{marker}")
            print(f"      {'POS':<5}{'ECR':>7}{'ADP':>7}{'delta':>8}"
                  f"{'bestECR':>9}{'bestADP':>9}")
            lranks = (left.get("availability") or {}).get("ranks", {})
            rranks = (right.get("availability") or {}).get("ranks", {})
            for pos in T.POSITIONS:
                lp, rp = lmap.get(pos, 0.0), rmap.get(pos, 0.0)
                if max(lp, rp) < 0.01:
                    continue
                print(f"      {pos:<5}{lp * 100:>6.0f}%{rp * 100:>6.0f}%"
                      f"{(rp - lp) * 100:>+7.0f}%"
                      f"{lranks.get(pos, '-'):>9}{rranks.get(pos, '-'):>9}")

    print("\n" + "=" * 62)
    print(f"picks compared            {len(ecr['picks'])}")
    print(f"mean absolute difference  {total_abs / cells * 100:.1f} percentage "
          f"points per position")
    print(f"top-position disagreements {flips}/{len(ecr['picks'])} picks")
    if fell_back:
        print(f"fell back to ECR          {', '.join(sorted(fell_back))} "
              "(no ADP left at those positions)")

    biggest.sort(reverse=True)
    print("\nlargest individual shifts:")
    for delta, pos, who, overall, lp, rp in biggest[:8]:
        if delta < 0.01:
            break
        print(f"  #{overall:<3} {who:<20}{pos:<4} "
              f"ECR {lp * 100:>3.0f}% -> ADP {rp * 100:>3.0f}%  "
              f"({(rp - lp) * 100:+.0f} pts)")

    print(
        "\nA large mean difference means the choice of signal materially changes "
        "who you expect to be picked. A small one means your league's tendencies "
        "and roster needs dominate, and the ranking barely matters."
    )


if __name__ == "__main__":
    main()
