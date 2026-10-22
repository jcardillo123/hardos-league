"""Explain why the tendency report is missing sections.

`owners()` only needs `owner_guid`, but every rate in the model needs BOTH
`position` and `round` to be non-null and sane. So owners can list fine while
the base rates come back empty. This narrows down which of those is broken.

    python tools/diagnose_history.py
    python tools/diagnose_history.py --show-owners   # include real owner names
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db                                            # noqa: E402
from app.tendencies import PHASES, phase_for                  # noqa: E402


def rule(title):
    print(f"\n--- {title} " + "-" * max(0, 62 - len(title)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--show-owners", action="store_true",
                        help="print real owner labels (otherwise redacted)")
    args = parser.parse_args()

    db.init_db()
    verdicts = []

    with db.session() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM history").fetchone()["n"]
        print(f"history rows: {total}")
        if not total:
            print("\nVERDICT: the history table is empty. The import never ran, or "
                  "it ran against a different working directory.\n"
                  "  python -m app.ingest --history draft_history.csv")
            return 1

        rule("column health")
        columns = [
            "season", "overall_pick", "round", "round_pick", "owner_guid",
            "owner_label", "player_name", "position", "keeper", "name_key",
            "draft_slot", "pos_taken", "season_pts", "pos_finish",
        ]
        for column in columns:
            row = conn.execute(
                f"SELECT COUNT(*) AS n, SUM(CASE WHEN {column} IS NULL THEN 1 ELSE 0 END) "
                f"AS nulls, COUNT(DISTINCT {column}) AS distinct_vals FROM history"
            ).fetchone()
            types = [
                r["t"] for r in conn.execute(
                    f"SELECT DISTINCT typeof({column}) AS t FROM history"
                )
            ]
            flag = ""
            if row["nulls"] == total:
                flag = "  <-- ALL NULL"
            elif row["nulls"]:
                flag = f"  ({row['nulls']} null)"
            if "blob" in types:
                flag += "  <-- STORED AS BLOB, numbers unreadable"
            print(f"  {column:<14} distinct={row['distinct_vals']:<6} "
                  f"types={','.join(types):<14}{flag}")

        # ---- the two columns the model cannot work without -------------- #
        rule("position values")
        pos_rows = conn.execute(
            "SELECT position, COUNT(*) AS n FROM history "
            "GROUP BY position ORDER BY n DESC"
        ).fetchall()
        for row in pos_rows:
            label = row["position"] if row["position"] is not None else "NULL"
            print(f"  {label!s:<8} {row['n']}")
        null_pos = next((r["n"] for r in pos_rows if r["position"] is None), 0)
        if null_pos == total:
            verdicts.append(
                "Every `position` is NULL. DraftTool.py could not resolve player "
                "names/positions - its kona_player_info lookup failed (look for "
                "'name lookup failed' or 'unresolved player names' in its output). "
                "Re-run DraftTool.py; the model needs position on every pick."
            )
        elif null_pos:
            verdicts.append(
                f"{null_pos} of {total} picks have NULL position and are silently "
                "excluded from every rate. Usually unresolved player names."
            )

        rule("round values")
        round_rows = conn.execute(
            "SELECT round, COUNT(*) AS n FROM history GROUP BY round ORDER BY round"
        ).fetchall()
        printable = ", ".join(
            f"{r['round'] if r['round'] is not None else 'NULL'}:{r['n']}"
            for r in round_rows
        )
        print(f"  {printable}")
        null_round = next((r["n"] for r in round_rows if r["round"] is None), 0)
        if null_round == total:
            verdicts.append(
                "Every `round` is NULL, so no pick can be assigned to a phase. "
                "ESPN's older leagueHistory responses sometimes omit roundId. "
                "Round can be recomputed from overall_pick and team count - say "
                "the word and I will add that fallback to the importer."
            )

        rule("phase assignment (what the base rates are built from)")
        phase_counts = Counter()
        usable = 0
        for row in conn.execute(
            "SELECT round, position, keeper FROM history"
        ).fetchall():
            if row["position"] is None or row["round"] is None:
                continue
            if row["keeper"]:
                continue
            usable += 1
            phase_counts[phase_for(int(row["round"]))] += 1
        for name, low, high in PHASES:
            print(f"  {name:<6} (rounds {low}-{high}): {phase_counts.get(name, 0)}")
        print(f"  usable picks after all filters: {usable} of {total}")

        rule("keepers")
        keeper_rows = conn.execute(
            "SELECT COALESCE(keeper, 0) AS k, COUNT(*) AS n FROM history GROUP BY k"
        ).fetchall()
        for row in keeper_rows:
            print(f"  keeper={row['k']}: {row['n']}")
        keepers = sum(r["n"] for r in keeper_rows if r["k"])
        if keepers == total:
            verdicts.append(
                "Every pick is flagged as a keeper, and keepers are excluded from "
                "tendency analysis - so nothing is left. Check how DraftTool.py "
                "wrote the `keeper` column."
            )

        rule("per season")
        for row in conn.execute(
            """
            SELECT season, COUNT(*) AS picks,
                   COUNT(DISTINCT owner_guid) AS owners,
                   COUNT(DISTINCT round) AS rounds,
                   SUM(CASE WHEN position IS NULL THEN 1 ELSE 0 END) AS null_pos,
                   SUM(CASE WHEN round IS NULL THEN 1 ELSE 0 END) AS null_round
            FROM history GROUP BY season ORDER BY season
            """
        ).fetchall():
            print(f"  {row['season']}  picks={row['picks']:<4} owners={row['owners']:<3} "
                  f"rounds={row['rounds']:<3} null_pos={row['null_pos']:<4} "
                  f"null_round={row['null_round']}")

        rule("owner linking")
        linked = conn.execute(
            "SELECT COUNT(*) AS n FROM managers WHERE owner_guid IS NOT NULL"
        ).fetchone()["n"]
        print(f"  managers linked to an ESPN owner: {linked}")
        for row in conn.execute(
            "SELECT owner_guid, owner_label, COUNT(*) AS n FROM history "
            "GROUP BY owner_guid ORDER BY n DESC"
        ).fetchall():
            label = row["owner_label"] if args.show_owners else "(redacted)"
            guid = (row["owner_guid"] or "NULL")
            tail = guid[-6:] if row["owner_guid"] else "NULL"
            print(f"  ...{tail}  {label!s:<22} {row['n']} picks")

        rule("sample rows")
        for row in conn.execute(
            "SELECT season, overall_pick, round, round_pick, position, "
            "player_name, keeper FROM history ORDER BY season, overall_pick LIMIT 8"
        ).fetchall():
            print(f"  {row['season']} #{row['overall_pick']:<3} "
                  f"R{row['round']}.{row['round_pick']}  "
                  f"{row['position']!s:<5} {row['player_name']!s:<24} "
                  f"keeper={row['keeper']}")

    print()
    if verdicts:
        print("VERDICT")
        for i, verdict in enumerate(verdicts, 1):
            print(f"  {i}. {verdict}")
    else:
        print("VERDICT: position, round and keeper all look usable. If base rates "
              "still print empty, paste this whole output and I will dig further.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
