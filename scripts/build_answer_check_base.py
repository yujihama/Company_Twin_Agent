"""Rebuild the pre-fork slice of each original world from the sweep's branch
bundles, as a base for the answer-check sweep.

The original 20 worlds were never committed; what PR #124's bundles carry is
each original world's ledger REPLAYED VERBATIM up to the fork (row hashes
chained, original ledger's sha256 recorded in the bundle meta). This script
extracts that prefix into a standalone world directory per original world:

- world_ledger.jsonl: the bundle's first `fork_ordinal` rows (validated as a
  hash chain before writing)
- config.json: the continuation settings reconstructed from the sealed
  campaign parameters, VERIFIED against the prefix (every customer event and
  absence row must match) -- the same reconstruct-and-check used for the
  depth-1 trial
- meta.json: stage/seed/prompt_mode
- provenance.json: which bundle the rows came from, the original ledger's
  sha256 as the bundle recorded it, the number of rows extracted, and the
  verification result

Usage:
    .venv/bin/python scripts/build_answer_check_base.py runs/deviation_sweep_20260729 \
        --output runs/answer_check_base_20260731
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from company_twin.branch_execution import _validate_source_ledger_chain  # noqa: E402
from company_twin.design_loader import load_design  # noqa: E402
from company_twin.recorder import read_jsonl  # noqa: E402

from rebranch_inconclusive import (  # noqa: E402
    CAMPAIGN_TICKS,
    reconstructed_continuation_settings,
    verify_against_ledger,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sweep_root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    design = load_design(Path.cwd())
    built = 0
    for scene_dir in sorted(args.sweep_root.iterdir()):
        point_path = scene_dir / "point.json"
        if not point_path.exists():
            continue
        point_info = json.loads(point_path.read_text(encoding="utf-8"))
        fork_ordinal = int(point_info["point"]["fork_ordinal"])
        bundle = scene_dir / "root-d0b00"
        bundle_meta = json.loads((bundle / "meta.json").read_text(encoding="utf-8"))
        rows = read_jsonl(bundle / "world_ledger.jsonl")[:fork_ordinal]
        _validate_source_ledger_chain(rows)  # fail closed BEFORE anything is written

        seed = int(bundle_meta.get("seed") or 0)
        settings = reconstructed_continuation_settings(design, seed=seed)
        problems = verify_against_ledger(settings, rows, up_to_ordinal=fork_ordinal)
        if problems:
            raise SystemExit(
                f"{scene_dir.name}: reconstructed settings disagree with the bundle's own prefix: {problems[:3]}"
            )

        world_name = scene_dir.name.split("__")[0]
        out = args.output / world_name
        out.mkdir(parents=True, exist_ok=True)
        ledger_path = out / "world_ledger.jsonl"
        with ledger_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        (out / "config.json").write_text(
            json.dumps(
                {
                    "schema_version": "company_twin.world_config.v2",
                    "stage": "S2",
                    "world": {
                        "schedule": {"ticks": CAMPAIGN_TICKS, "workflow": settings["workflow"]},
                        "population": {
                            "binding": settings["model_binding"],
                            "tick_budget": settings["tick_budget"],
                            "absence": settings["absence"],
                        },
                        "deck": {"events": settings["deck_events"]},
                    },
                    "model": {"customer": settings["customer_model"]},
                },
                ensure_ascii=False,
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
        (out / "meta.json").write_text(
            json.dumps(
                {"stage": "S2", "seed": seed, "prompt_mode": "measurement", "run_id": world_name},
                ensure_ascii=False,
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
        (out / "provenance.json").write_text(
            json.dumps(
                {
                    "note": "pre-fork slice of the original world, extracted verbatim from a branch bundle; NOT the full original world (rows past the fork are not recoverable)",
                    "source_bundle": str(bundle),
                    "original_ledger_sha256_per_bundle_meta": bundle_meta.get("source_ledger_sha256"),
                    "rows_extracted": len(rows),
                    "last_tick": int(rows[-1].get("tick") or 0) if rows else 0,
                    "settings_verification": "matched (customer events and absence rows agree with the reconstruction)",
                    "extracted_ledger_sha256": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
                },
                ensure_ascii=False,
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
        built += 1
        print(f"{world_name}: {len(rows)} rows, up to tick {rows[-1].get('tick')}")
    print(f"built {built} base worlds under {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
