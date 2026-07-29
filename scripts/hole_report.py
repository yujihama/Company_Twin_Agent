"""Generate the hole report from a finished deviation sweep. Zero spend:
reads only judged bundles / archived summaries already on disk.

Usage:
    .venv/bin/python scripts/hole_report.py runs/deviation_sweep_20260729
    .venv/bin/python scripts/hole_report.py docs/progress/deviation_tree_fixed_20260728.json -o out/
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from company_twin.hole_report import write_hole_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="sweep root, one tree's output dir, or an archived deviation_tree.json")
    parser.add_argument("-o", "--output-dir", type=Path, default=None)
    parser.add_argument("--continuation-ticks", type=int, default=None, help="ticks the branches ran, for the reproduction recipe")
    args = parser.parse_args()
    paths = write_hole_report(args.root, output_dir=args.output_dir, continuation_ticks=args.continuation_ticks)
    print(paths["md"])
    print(paths["json"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
