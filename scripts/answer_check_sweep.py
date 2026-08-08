"""Plan-B full sweep for the answer check: defect-linked cases x every
process stage, 3 branches per decision point, multi-step deviations on.

Two-phase by design (the paid-run rule: show the exact size first):

    # Phase 1 -- enumerate + firm estimate, zero spend. Writes plan.json.
    .venv/bin/python scripts/answer_check_sweep.py runs/phase3_m3_confirmatory_v4_20260724/r* \
        --output runs/answer_check_sweep_YYYYMMDD

    # Phase 2 -- the paid run, only after the estimate has been approved.
    #   --max-credits is a hard cap measured against the provider's own
    #   balance; the run stops cleanly when it is reached and reports how
    #   many decision points were left unexecuted.
    ... --execute --max-credits 12

Plan B's scope filter is mechanical: keep the decision points of the
authored verification cases (the ones the planted defects were designed
around) and drop the routine-customer cases. Both the kept and dropped
counts are reported -- a narrowed sweep must never read as a full one.

Re-running with the same --output resumes: decision points whose output
directory already holds a result are skipped without spending.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from company_twin.deviation_tree import (  # noqa: E402
    TreeConfig,
    enumerate_decision_points,
    expand_deviation_frontier,
    frontier_item_for_point,
)
from company_twin.recorder import read_jsonl  # noqa: E402

# Measured on the first wide sweep (2026-07-29): ~0.073 credits per 10-tick
# branch world; the upper bound absorbs retries and longer dossiers.
COST_PER_WORLD_LOW = 0.05
COST_PER_WORLD_HIGH = 0.1
BRANCHES_PER_POINT = 3
CONTINUATION_WINDOW = 10
MAX_STEPS_PER_CANDIDATE = 3  # "record first, then submit" style sequences


def plan_b_filter(points: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(kept, dropped): keep authored verification cases, drop routine ones."""
    kept = [point for point in points if not point.get("routine")]
    dropped = [point for point in points if point.get("routine")]
    return kept, dropped


def defect_bound_case_types(design) -> set[str]:
    """Case types whose scenario binds at least one planted-defect span --
    the mechanical reading of 'cases the defects were designed around'."""
    return {
        probe_id
        for probe_id in design.probes
        if any(span in design.spans for span in design.probes[probe_id].binds)
    }


def cap_per_combo(
    points: list[tuple[Path, dict[str, Any]]], cap: int
) -> tuple[list[tuple[Path, dict[str, Any]]], int]:
    """Keep at most `cap` worlds per (case type x stage) combination, in
    run-root name order (never chosen by hand). Returns (kept, dropped_count).
    Assumes `points` is already sorted by run root."""
    seen: dict[tuple[str, str], int] = {}
    kept: list[tuple[Path, dict[str, Any]]] = []
    dropped = 0
    for run_root, point in points:
        combo = (str(point.get("probe_id")), str(point.get("rule")))
        count = seen.get(combo, 0)
        if count >= cap:
            dropped += 1
            continue
        seen[combo] = count + 1
        kept.append((run_root, point))
    return kept, dropped


def estimate(point_count: int) -> dict[str, Any]:
    worlds = point_count * BRANCHES_PER_POINT
    return {
        "decision_points": point_count,
        "branches_per_point": BRANCHES_PER_POINT,
        "worlds": worlds,
        "credits_low": round(worlds * COST_PER_WORLD_LOW, 1),
        "credits_high": round(worlds * COST_PER_WORLD_HIGH, 1),
    }


def point_output_name(run_root: Path, point: dict[str, Any]) -> str:
    return f"{Path(run_root).name}__{point['application_id']}__{point['rule']}"


def remaining_credits() -> float:
    from company_twin.parallel_runner import check_openrouter_credits

    result = check_openrouter_credits(api_key=os.environ.get("OPENROUTER_API_KEY"))
    if result.get("status") != "ok":
        raise RuntimeError(f"credit check failed: {result}")
    return float(result["remaining_credits"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_roots", type=Path, nargs="*", help="the finished source worlds (not needed with --from-selection)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="run the paid phase (default: enumerate + estimate only)")
    parser.add_argument("--max-credits", type=float, default=None, help="hard spend cap for this invocation, measured against the provider balance")
    parser.add_argument("--defect-bound-only", action="store_true", help="keep only case types whose scenario binds a planted-defect span")
    parser.add_argument("--per-combo-cap", type=int, default=None, help="at most N worlds per (case type x stage) combination, in name order")
    parser.add_argument("--from-selection", type=Path, default=None, help="run the exact point list a previous invocation fixed in selected_points.json, skipping enumeration -- the ONLY safe way to split one approved scope across processes")
    parser.add_argument("--partition", type=str, default=None, help="'i/n': with --from-selection, run only every n-th point starting at i (1-based)")
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    if args.from_selection is not None:
        # The scope was fixed once, globally; re-filtering per process could
        # widen it, so a partitioned run never re-enumerates.
        selection = json.loads(Path(args.from_selection).read_text(encoding="utf-8"))
        all_points = [(Path(entry["run_root"]), entry["point"]) for entry in selection]
        all_skipped = []
        plan = json.loads((args.output / "plan.json").read_text(encoding="utf-8")) if (args.output / "plan.json").exists() else {}
        if args.partition:
            index, count = (int(part) for part in args.partition.split("/"))
            all_points = all_points[index - 1 :: count]
            print(f"分担 {args.partition}: {len(all_points)} 場面")
    else:
        # ---- Phase 1: enumerate, filter, estimate (zero spend) ------------
        from company_twin.design_loader import load_design as _load_design

        all_points = []
        all_skipped = []
        dropped_total = 0
        for run_root in sorted(args.run_roots):
            result = enumerate_decision_points(run_root)
            kept, dropped = plan_b_filter(result["points"])
            dropped_total += len(dropped)
            all_skipped.extend({**item, "world": Path(run_root).name} for item in result["skipped"])
            all_points.extend((Path(run_root), point) for point in kept)

        dropped_unbound = 0
        if args.defect_bound_only:
            bound_types = defect_bound_case_types(_load_design(Path.cwd()))
            before = len(all_points)
            all_points = [(root, point) for root, point in all_points if point.get("probe_id") in bound_types]
            dropped_unbound = before - len(all_points)
        dropped_over_cap = 0
        if args.per_combo_cap is not None:
            all_points, dropped_over_cap = cap_per_combo(all_points, args.per_combo_cap)

        stage_counts = Counter(point["stage_label"] for _, point in all_points)
        plan = {
            "scope": "plan B: authored verification cases x every stage; routine cases dropped",
            "defect_bound_only": bool(args.defect_bound_only),
            "per_combo_cap": args.per_combo_cap,
            "worlds_scanned": len(args.run_roots),
            "estimate": estimate(len(all_points)),
            "points_by_stage": dict(stage_counts.most_common()),
            "dropped_routine_points": dropped_total,
            "dropped_unbound_case_points": dropped_unbound,
            "dropped_over_combo_cap": dropped_over_cap,
            "not_enumerable": all_skipped,
            "continuation_window": CONTINUATION_WINDOW,
            "max_steps_per_candidate": MAX_STEPS_PER_CANDIDATE,
        }
        (args.output / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        (args.output / "selected_points.json").write_text(
            json.dumps(
                [{"run_root": str(root), "point": point} for root, point in all_points],
                ensure_ascii=False,
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
        print(json.dumps({k: plan[k] for k in ("estimate", "points_by_stage", "dropped_routine_points")}, ensure_ascii=False, indent=1))
        print(f"列挙できなかった場面: {len(all_skipped)}(理由は plan.json に記録)")
    if not args.execute:
        print("見積りのみ(--execute で実行。費用のかかる実行の現行ルールに従い、承認を得てから)")
        return 0

    # ---- Phase 2: the paid run -------------------------------------------
    from company_twin.agents import _chat_model
    from company_twin.branch_execution import run_branch_continuation
    from company_twin.corpus import Corpus
    from company_twin.design_loader import load_design

    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    generation_llm = _chat_model(os.getenv("OPENROUTER_MODEL") or "openrouter:qwen/qwen3.6-flash")

    def generate(prompt: str) -> str:
        return str(generation_llm.invoke(prompt).content)

    def runner(kernel, **kwargs):
        return run_branch_continuation(
            kernel,
            metadata=kwargs["metadata"],
            design=design,
            corpus=corpus,
            ticks=kwargs["ticks"],
            allow_spend=True,
            injected_action=kwargs.get("injected_action"),
        )

    config = TreeConfig(
        max_branches_per_node=BRANCHES_PER_POINT,
        max_depth=0,
        max_worlds=BRANCHES_PER_POINT,
        continuation_ticks=CONTINUATION_WINDOW,
        max_steps_per_candidate=MAX_STEPS_PER_CANDIDATE,
    )
    start_credits = remaining_credits()
    executed = 0
    resumed = 0
    failures: list[dict[str, str]] = []
    stopped_by_cap: str | None = None
    for index, (run_root, point) in enumerate(all_points):
        name = point_output_name(run_root, point)
        point_dir = args.output / name
        if (point_dir / "frontier_result_d0.json").exists():
            resumed += 1
            continue
        if args.max_credits is not None:
            spent = start_credits - remaining_credits()
            if spent >= args.max_credits:
                stopped_by_cap = (
                    f"max_credits={args.max_credits} reached after {executed} decision point(s); "
                    f"{len(all_points) - index} left unexecuted"
                )
                break
        try:
            item = frontier_item_for_point(run_root, point)
            result = expand_deviation_frontier(
                design=design,
                frontier=[item],
                output_root=point_dir,
                config=config,
                generate=generate,
                continuation_runner=runner,
                design_root=Path.cwd(),
            )
        except Exception as exc:  # isolate one point's failure; report, never hide
            failures.append({"point": name, "error": f"{type(exc).__name__}: {exc}"[:500]})
            print(json.dumps({"point": name, "error": failures[-1]["error"]}, ensure_ascii=False), flush=True)
            continue
        (point_dir / "point.json").write_text(
            json.dumps({"point": point, "source_world": str(run_root), "window": CONTINUATION_WINDOW}, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        executed += 1
        print(
            json.dumps(
                {"point": name, "worlds_executed": result["worlds_executed"], "awaiting": result["awaiting_judgement"], "caps_hit": result["caps_hit"]},
                ensure_ascii=False,
            ),
            flush=True,
        )

    awaiting: list[str] = []
    for point_dir in sorted(args.output.iterdir()):
        frontier_file = point_dir / "frontier_result_d0.json"
        if not frontier_file.exists():
            continue
        result = json.loads(frontier_file.read_text(encoding="utf-8"))
        awaiting.extend(f"{point_dir.name}/{node_id}" for node_id in result.get("awaiting_judgement", []))
    summary = {
        "plan": plan,
        "executed_points": executed,
        "resumed_points": resumed,
        "failures": failures,
        "stopped_by_cap": stopped_by_cap,
        "credits_spent": round(start_credits - remaining_credits(), 2),
        "awaiting_judgement": awaiting,
    }
    (args.output / "sweep_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("executed_points", "resumed_points", "stopped_by_cap", "credits_spent")}, ensure_ascii=False))
    print(f"判定待ちの世界: {len(awaiting)}(一覧は sweep_summary.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
