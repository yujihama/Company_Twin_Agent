from __future__ import annotations

import hashlib
import json
from pathlib import Path

from company_twin.loss_campaign import _validate_sealed_batch_spec, load_loss_campaign_plan
from company_twin.parallel_runner import BatchSpec


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_repository_m3_repilot_plan_is_a_non_executable_draft() -> None:
    root = Path(__file__).resolve().parents[1]
    plan_path = root / "docs" / "progress" / "phase3_m3_loss_repilot_plan_20260712.json"
    batch_path = root / "docs" / "progress" / "phase3_m3_loss_repilot_batch_20260712.json"

    plan = load_loss_campaign_plan(plan_path, root=root)
    batch = BatchSpec.from_dict(json.loads(batch_path.read_text(encoding="utf-8")))

    assignments = _validate_sealed_batch_spec(plan, batch, root=root)

    assert _sha256(batch_path) == plan["batch_spec_sha256"]
    assert plan["campaign_role"] == "feasibility_pilot"
    assert plan["kind"] == "pre_execution_pilot_plan"
    assert plan["execution_authorized_by_this_file"] is False
    assert plan["approval_granted_by_this_file"] is False
    assert plan["cost_guard"]["execution_authorized_by_this_file"] is False
    assert batch.credit_guard is not None
    assert batch.credit_guard.to_dict() == {
        "minimum_credits": 7.0,
        "abort_on_low_credits": True,
        "require_available": True,
    }
    assert batch.waves == []
    assert len(assignments) == 4
    assert {assignment["seed"] for assignment in assignments.values()} == {953, 954}

    expected_mutations = {
        "m3_repilot_r1_control_seed953": [],
        "m3_repilot_r1_clarify_seed953": ["clarify_elderly_understanding_sales_only"],
        "m3_repilot_r4_control_seed954": [],
        "m3_repilot_r4_contradict_seed954": ["contradict_chat_approval_recorded"],
    }

    for run in batch.runs:
        run_root = str(run.run_root)
        assert run_root.startswith("runs/phase3_m3_repilot_20260712/")
        assert not (root / run_root).exists()
        assert run.extra_args == ["--circulate-notices", "--workflow-support"]
        assert run.run_id in expected_mutations
        assert run.mutations == expected_mutations[run.run_id]


def test_repository_m3_repilot_plan_passes_sealed_validators() -> None:
    root = Path(__file__).resolve().parents[1]
    plan_path = root / "docs" / "progress" / "phase3_m3_loss_repilot_plan_20260712.json"
    batch_path = root / "docs" / "progress" / "phase3_m3_loss_repilot_batch_20260712.json"

    plan = load_loss_campaign_plan(plan_path, root=root)
    batch = BatchSpec.from_dict(json.loads(batch_path.read_text(encoding="utf-8")))
    assignments = _validate_sealed_batch_spec(plan, batch, root=root)

    assert {assignment["seed"] for assignment in assignments.values()} == {953, 954}
