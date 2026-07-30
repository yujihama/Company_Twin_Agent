"""Depth-1 re-branch of inconclusive worlds from a judged sweep.

Usage:
    .venv/bin/python scripts/rebranch_inconclusive.py runs/deviation_sweep_20260729 \
        --output runs/deviation_rebranch_20260730 --limit 3 [--dry-run]

Selection is mechanical: all worlds judged "inconclusive" in the sweep,
sorted by (scene, node_id); the first --limit expandable ones are taken and
every skipped one is reported with its reason -- nothing is dropped silently.

A branch bundle's own config.json does not carry the continuation settings
(workflow flags, customer visit schedule, absence days, model bindings), so
re-branching from a bundle alone would silently downgrade the world (see the
warning in branch_execution.rebuild_kernel_state). Those settings are
deterministic outputs of build_world_config for the original campaign's
arguments (model/ticks/workflow_support from the sealed batch spec), so this
script reconstructs them and then VERIFIES the reconstruction against each
bundle's own pre-fork ledger (customer events and absence rows must match).
A mismatch aborts the run for that world rather than running degraded.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from company_twin.corpus import Corpus  # noqa: E402
from company_twin.design_loader import load_design  # noqa: E402
from company_twin.deviation_tree import (  # noqa: E402
    TreeConfig,
    constructed_decision_context,
    expand_deviation_frontier,
    next_decision_ordinal,
)
from company_twin.recorder import read_jsonl  # noqa: E402
from company_twin.world_config import build_world_config  # noqa: E402

# The original campaign's sealed execution parameters
# (docs/progress/phase3_m3_loss_campaign_v4_batch_20260724.json):
# model openrouter:qwen/qwen3.6-flash, ticks=40, prompt_mode=measurement,
# --circulate-notices --workflow-support.
CAMPAIGN_MODEL = "openrouter:qwen/qwen3.6-flash"
CAMPAIGN_TICKS = 40
CONTINUATION_WINDOW = 10  # same observation window the sweep used (point.json)


def reconstructed_continuation_settings(design, *, seed: int) -> dict[str, Any]:
    """The continuation-relevant slices of the original world's config,
    rebuilt from the same deterministic function that produced it."""
    config = build_world_config(
        design,
        stage="S2",
        model=CAMPAIGN_MODEL,
        seed=seed,
        ticks=CAMPAIGN_TICKS,
        circulate_notices=True,
        workflow_support=True,
    )
    world = config["world"]
    population = world["population"]
    return {
        "workflow": dict(world["schedule"]["workflow"]),
        "tick_budget": dict(population["tick_budget"]),
        "model_binding": dict(population["binding"]),
        "absence": dict(population["absence"]),
        "deck_events": list(world["deck"]["events"]),
        "customer_model": str(config["model"]["customer"]),
    }


def verify_against_ledger(settings: dict[str, Any], ledger: list[dict[str, Any]], *, up_to_ordinal: int) -> list[str]:
    """Every pre-fork customer event and absence row in the bundle's ledger
    must be explained by the reconstructed settings. Returns problems."""
    problems: list[str] = []
    deck_by_id = {str(e["event_id"]): e for e in settings["deck_events"]}
    for index, row in enumerate(ledger):
        if index >= up_to_ordinal:
            break
        payload = row.get("payload") or {}
        if row.get("event_type") == "customer_event":
            event = deck_by_id.get(str(payload.get("event_id")))
            if event is None:
                problems.append(f"ledger row {index}: customer_event {payload.get('event_id')} not in reconstructed deck")
                continue
            for key in ("customer_id", "application_id", "product", "primary_seat"):
                if str(payload.get(key)) != str(event[key]):
                    problems.append(
                        f"ledger row {index}: {payload.get('event_id')} {key} mismatch "
                        f"(ledger {payload.get(key)!r} vs reconstructed {event[key]!r})"
                    )
            if int(row.get("tick") or 0) != int(event["trigger_tick"]):
                problems.append(
                    f"ledger row {index}: {payload.get('event_id')} trigger tick mismatch "
                    f"(ledger {row.get('tick')} vs reconstructed {event['trigger_tick']})"
                )
        elif row.get("event_type") == "seat_absence":
            seat = str(payload.get("seat_id") or "")
            tick = int(row.get("tick") or 0)
            if tick not in set(settings["absence"].get(seat) or []):
                problems.append(f"ledger row {index}: seat_absence {seat}@{tick} not in reconstructed schedule")
    return problems


def collect_inconclusive(sweep_root: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for tree_path in sorted(sweep_root.glob("*/deviation_tree.json")):
        scene = tree_path.parent.name
        tree = json.loads(tree_path.read_text(encoding="utf-8"))
        for node in tree["nodes"]:
            if node.get("outcome") != "inconclusive":
                continue
            world_dir = tree_path.parent / str(node["node_id"])
            found.append({"scene": scene, "node_id": str(node["node_id"]), "world_dir": world_dir})
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sweep_root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--max-worlds", type=int, default=9)
    parser.add_argument("--dry-run", action="store_true", help="select + verify + report, no generation and no world execution")
    args = parser.parse_args()

    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)

    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for entry in collect_inconclusive(args.sweep_root):
        if len(selected) >= args.limit:
            break
        world_dir = entry["world_dir"]
        node = json.loads((world_dir / "node.json").read_text(encoding="utf-8"))
        meta = json.loads((world_dir / "meta.json").read_text(encoding="utf-8"))
        seat_id = str(node["seat_id"])
        injection_ordinal = int(node.get("injection_ordinal") or 0)
        next_ordinal = next_decision_ordinal(world_dir, seat_id=seat_id, after_ordinal=max(injection_ordinal - 1, 0))
        label = f"{entry['scene']}--{entry['node_id']}"
        if next_ordinal is None:
            skipped.append({"world": label, "reason": "the acting seat never completed another turn after the injection"})
            continue
        ledger = read_jsonl(world_dir / "world_ledger.jsonl")
        fork_tick = int(ledger[next_ordinal - 1].get("tick") or 0)
        horizon = int(meta.get("source_horizon") or 0)
        if horizon and fork_tick >= horizon:
            skipped.append({"world": label, "reason": f"no observation window left (fork tick {fork_tick} is at the horizon {horizon})"})
            continue
        settings = reconstructed_continuation_settings(design, seed=int(meta.get("seed") or 0))
        problems = verify_against_ledger(settings, ledger, up_to_ordinal=next_ordinal)
        if problems:
            skipped.append({"world": label, "reason": "reconstructed settings disagree with the bundle's own ledger", "problems": problems})
            continue
        context = constructed_decision_context(world_dir, str(node["application_id"]), up_to_ordinal=next_ordinal)
        selected.append(
            {
                "parent_id": label,
                "depth": 1,
                "run_root": world_dir,
                "at_ordinal": next_ordinal,
                "context": context["prompt"],
                "seat_id": seat_id,
                "application_id": str(node["application_id"]),
                "_fork_tick": fork_tick,
                "_window": max(min(CONTINUATION_WINDOW, horizon - fork_tick), 0) if horizon else CONTINUATION_WINDOW,
                "_settings": settings,
            }
        )

    report = {
        "selected": [
            {"parent": item["parent_id"], "fork_ordinal": item["at_ordinal"], "fork_tick": item["_fork_tick"], "window_ticks": item["_window"]}
            for item in selected
        ],
        "skipped": skipped,
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if args.dry_run:
        return 0
    if not selected:
        print("nothing to expand", file=sys.stderr)
        return 1

    settings_by_parent = {item["parent_id"]: item.pop("_settings") for item in selected}
    for item in selected:
        item.pop("_fork_tick", None)
        item.pop("_window", None)

    from company_twin.agents import _chat_model
    from company_twin.branch_execution import run_branch_continuation

    generation_llm = _chat_model(CAMPAIGN_MODEL)

    def generate(prompt: str) -> str:
        return str(generation_llm.invoke(prompt).content)

    current_parent: dict[str, str] = {}

    def runner(kernel, **kwargs):
        metadata = dict(kwargs["metadata"])
        settings = settings_by_parent[current_parent["id"]]
        restored = [key for key in ("workflow", "tick_budget", "model_binding", "absence", "deck_events") if not metadata.get(key)]
        if not metadata.get("customer_model"):
            restored.append("customer_model")
        for key in restored:
            metadata[key] = settings[key]
        metadata["reconstructed_continuation_settings"] = sorted(restored)
        return run_branch_continuation(
            kernel,
            metadata=metadata,
            design=design,
            corpus=corpus,
            ticks=kwargs["ticks"],
            allow_spend=True,
            model=CAMPAIGN_MODEL,
            injected_action=kwargs.get("injected_action"),
        )

    config = TreeConfig(
        max_branches_per_node=3,
        max_depth=1,
        max_worlds=args.max_worlds,
        continuation_ticks=CONTINUATION_WINDOW,
        max_steps_per_candidate=1,
    )
    results = []
    failures: list[dict[str, str]] = []
    for item in selected:
        current_parent["id"] = item["parent_id"]
        try:
            result = expand_deviation_frontier(
                design=design,
                frontier=[item],
                output_root=args.output,
                config=config,
                generate=generate,
                continuation_runner=runner,
                design_root=Path.cwd(),
            )
        except Exception as exc:  # isolate one decision point's failure; report, never hide
            failures.append({"parent": item["parent_id"], "error": f"{type(exc).__name__}: {exc}"[:500]})
            print(json.dumps({"parent": item["parent_id"], "error": failures[-1]["error"]}, ensure_ascii=False))
            continue
        results.append(result)
        print(json.dumps({"parent": item["parent_id"], "worlds_executed": result["worlds_executed"], "caps_hit": result["caps_hit"], "awaiting": result["awaiting_judgement"]}, ensure_ascii=False))

    merged = {
        "nodes": [node for result in results for node in result["nodes"]],
        "caps_hit": [note for result in results for note in result["caps_hit"]],
        "worlds_executed": sum(result["worlds_executed"] for result in results),
        "awaiting_judgement": [node_id for result in results for node_id in result["awaiting_judgement"]],
        "expansion_failures": failures,
        "selection_report": report,
    }
    (args.output / "frontier_result_d1.json").write_text(json.dumps(merged, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"total_worlds": merged["worlds_executed"], "awaiting_judgement": merged["awaiting_judgement"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
