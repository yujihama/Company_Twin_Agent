"""Plan-B sweep driver (scripts/answer_check_sweep.py): the zero-spend phase.

The paid phase is the same expand/judge machinery test_deviation_tree.py
already covers; what needs its own coverage is the driver's scope filter,
its firm estimate, and the gate that keeps a bare invocation zero-spend.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from company_twin.deviation_tree import enumerate_decision_points

from tests.test_deviation_tree import _enumeration_world

_SPEC = importlib.util.spec_from_file_location(
    "answer_check_sweep", Path(__file__).resolve().parent.parent / "scripts" / "answer_check_sweep.py"
)
sweep = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(sweep)


def test_plan_b_filter_keeps_authored_cases_and_reports_the_dropped(tmp_path: Path) -> None:
    result = enumerate_decision_points(_enumeration_world(tmp_path))
    kept, dropped = sweep.plan_b_filter(result["points"])
    assert {p["application_id"] for p in kept} == {"APP-E-01"}
    assert {p["application_id"] for p in dropped} == {"APP-E-02"}  # routine case
    assert len(kept) + len(dropped) == len(result["points"])


def test_estimate_is_a_count_not_a_guess() -> None:
    figure = sweep.estimate(140)
    assert figure["worlds"] == 140 * sweep.BRANCHES_PER_POINT
    assert figure["credits_low"] == round(figure["worlds"] * sweep.COST_PER_WORLD_LOW, 1)
    assert figure["credits_high"] == round(figure["worlds"] * sweep.COST_PER_WORLD_HIGH, 1)


def test_bare_invocation_enumerates_and_stops_before_any_spend(tmp_path: Path, monkeypatch) -> None:
    world = _enumeration_world(tmp_path)
    output = tmp_path / "sweep_out"
    monkeypatch.setattr(sys, "argv", ["answer_check_sweep.py", str(world), "--output", str(output)])
    assert sweep.main() == 0

    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    assert plan["estimate"]["decision_points"] == 3  # APP-E-01's contact + 2 stages
    assert plan["estimate"]["worlds"] == 9
    assert plan["dropped_routine_points"] == 1
    assert plan["not_enumerable"], "skipped points must be carried into the plan"
    # the gate: nothing but the plan documents may exist -- no world was run
    assert sorted(p.name for p in output.iterdir()) == ["plan.json", "selected_points.json"]
    # the fixed selection is what a partitioned run will consume verbatim
    selection = json.loads((output / "selected_points.json").read_text(encoding="utf-8"))
    assert len(selection) == 3 and all("run_root" in e and "point" in e for e in selection)


def test_per_combo_cap_keeps_the_first_worlds_in_name_order() -> None:
    points = [
        (Path("runs/w1"), {"probe_id": "P-01", "rule": "after_x"}),
        (Path("runs/w2"), {"probe_id": "P-01", "rule": "after_x"}),
        (Path("runs/w3"), {"probe_id": "P-01", "rule": "after_x"}),
        (Path("runs/w1"), {"probe_id": "P-01", "rule": "after_y"}),
    ]
    kept, dropped = sweep.cap_per_combo(points, 2)
    assert [str(root.name) for root, p in kept if p["rule"] == "after_x"] == ["w1", "w2"]
    assert dropped == 1
    # a different stage is its own combination, untouched by the cap
    assert any(p["rule"] == "after_y" for _, p in kept)


def test_defect_bound_case_types_reads_the_design() -> None:
    from company_twin.design_loader import load_design

    bound = sweep.defect_bound_case_types(load_design(Path.cwd()))
    # the two cases whose scenarios bind no planted-defect span stay out
    assert "P-11" in bound and "P-10" in bound
    assert "P-02" not in bound and "P-07" not in bound
