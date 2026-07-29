"""Enumerate every decision point the given finished worlds offer, and print
the size and cost estimate for a full deviation sweep over them. Zero spend:
this only reads ledgers -- it exists so the estimate shown to the owner
before a paid run is a count, not a guess.

Usage:
    .venv/bin/python scripts/enumerate_decision_points.py runs/s2_*/ --branches 3
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from company_twin.deviation_tree import enumerate_decision_points  # noqa: E402

# Measured on the first wide sweep (2026-07-29): ~0.22 credits per decision
# point of 3 branch worlds incl. generation, i.e. ~0.07 per world. The upper
# bound doubles it to absorb longer observation windows.
COST_PER_WORLD_LOW = 0.05
COST_PER_WORLD_HIGH = 0.1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_roots", type=Path, nargs="+")
    parser.add_argument("--branches", type=int, default=3, help="branch worlds per decision point")
    args = parser.parse_args()

    total_points = 0
    total_skipped = 0
    stage_counts: Counter[str] = Counter()
    for run_root in args.run_roots:
        result = enumerate_decision_points(run_root)
        points = result["points"]
        total_points += len(points)
        total_skipped += len(result["skipped"])
        for point in points:
            stage_counts[point["stage_label"]] += 1
        print(f"{run_root}: {len(points)} 場面(列挙できず {len(result['skipped'])})")

    worlds = total_points * args.branches
    print()
    print(f"判断の場面の合計: {total_points}(列挙できず {total_skipped})")
    for stage, count in stage_counts.most_common():
        print(f"  - {stage}: {count}")
    print(f"分岐世界の合計(1場面{args.branches}分岐): {worlds}")
    print(
        f"概算費用: {worlds * COST_PER_WORLD_LOW:.1f} 〜 {worlds * COST_PER_WORLD_HIGH:.1f} クレジット"
        f"(1世界 {COST_PER_WORLD_LOW}〜{COST_PER_WORLD_HIGH} として)"
    )
    print("この見積りを提示し、承認を得てから実行すること(費用のかかる実行の現行ルール)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
