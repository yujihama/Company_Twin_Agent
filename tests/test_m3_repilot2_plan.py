from __future__ import annotations

import hashlib
import json
from pathlib import Path

from company_twin.loss_campaign import _validate_sealed_batch_spec, load_loss_campaign_plan
from company_twin.parallel_runner import BatchSpec


def _paths() -> tuple[Path, Path, Path]:
    root = Path(__file__).resolve().parents[1]
    return (
        root,
        root / "docs" / "progress" / "phase3_m3_loss_repilot2_plan_20260712.json",
        root / "docs" / "progress" / "phase3_m3_loss_repilot2_batch_20260712.json",
    )


def test_repository_m3_repilot2_plan_is_authorized_for_pilot_execution_only() -> None:
    root, plan_path, batch_path = _paths()
    plan = load_loss_campaign_plan(plan_path, root=root)
    batch = BatchSpec.from_dict(json.loads(batch_path.read_text(encoding="utf-8")))
    assignments = _validate_sealed_batch_spec(plan, batch, root=root)

    assert hashlib.sha256(batch_path.read_bytes()).hexdigest() == plan["batch_spec_sha256"]
    assert plan["campaign_role"] == "feasibility_pilot"
    assert plan["kind"] == "pre_execution_pilot_plan"
    assert plan["execution_authorized_by_this_file"] is True
    assert plan["approval_granted_by_this_file"] is True
    assert plan["cost_guard"]["execution_authorized_by_this_file"] is True
    assert batch.credit_guard is not None
    assert batch.credit_guard.to_dict() == {
        "minimum_credits": 7.0,
        "abort_on_low_credits": True,
        "require_available": True,
    }
    assert batch.waves == []
    assert len(assignments) == 4
    assert {assignment["seed"] for assignment in assignments.values()} == {955, 956}

    expected_mutations = {
        "m3_repilot2_r1_control_seed955": [],
        "m3_repilot2_r1_clarify_seed955": ["clarify_elderly_understanding_sales_only"],
        "m3_repilot2_r4_control_seed956": [],
        "m3_repilot2_r4_contradict_seed956": ["contradict_chat_approval_recorded"],
    }
    for run in batch.runs:
        assert str(run.run_root).startswith("runs/phase3_m3_repilot2_20260712/")
        assert run.extra_args == ["--circulate-notices", "--workflow-support"]
        assert run.run_id in expected_mutations
        assert run.mutations == expected_mutations[run.run_id]


def test_repository_m3_repilot2_plan_records_v2_generation_boundary() -> None:
    _, plan_path, _ = _paths()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    note = plan["measurement_boundary"]["generation_note"]
    assert "workflow_support_v2" in note
    assert "Never pooled" in note
    assert plan["execution_conditions"]["seeds"] == [955, 956]
    assert plan["execution_conditions"]["workflow_support"] is True
