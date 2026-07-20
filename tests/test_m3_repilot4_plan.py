from __future__ import annotations

import hashlib
import json
from pathlib import Path

from company_twin.loss_campaign import (
    LOSS_FEASIBILITY_GATE_V2_SCHEMA_VERSION,
    _validate_sealed_batch_spec,
    load_loss_campaign_plan,
)
from company_twin.parallel_runner import BatchSpec


def _paths() -> tuple[Path, Path, Path]:
    root = Path(__file__).resolve().parents[1]
    return (
        root,
        root / "docs" / "progress" / "phase3_m3_loss_repilot4_plan_20260720.json",
        root / "docs" / "progress" / "phase3_m3_loss_repilot4_batch_20260720.json",
    )


def test_repository_m3_repilot4_plan_is_authorized_for_wave_execution_only() -> None:
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

    gate = plan["pilot_gate"]
    assert gate["schema_version"] == LOSS_FEASIBILITY_GATE_V2_SCHEMA_VERSION
    assert gate["campaign_minimum_r3_opportunities"] == 3
    assert gate["per_contrast_minimum_r3_opportunities"] == 1
    assert gate["maximum_r3_events"] == 0

    assert batch.credit_guard is not None
    assert batch.credit_guard.to_dict() == {
        "minimum_credits": 4.5,
        "abort_on_low_credits": True,
        "require_available": True,
    }
    # Two sealed waves (owner cost-reduction directive 2026-07-20): each
    # spend boundary carries its own credit preflight.
    assert [wave.wave_id for wave in batch.waves] == ["wave-1-r1-pair", "wave-2-r4-pair"]
    assert [len(wave.run_ids) for wave in batch.waves] == [2, 2]
    assert len(assignments) == 4
    assert {assignment["seed"] for assignment in assignments.values()} == {959, 960}

    expected_mutations = {
        "m3_repilot4_r1_control_seed959": [],
        "m3_repilot4_r1_clarify_seed959": ["clarify_elderly_understanding_sales_only"],
        "m3_repilot4_r4_control_seed960": [],
        "m3_repilot4_r4_contradict_seed960": ["contradict_chat_approval_recorded"],
    }
    for run in batch.runs:
        assert str(run.run_root).startswith("runs/phase3_m3_repilot4_20260720/")
        assert run.extra_args == ["--circulate-notices", "--workflow-support"]
        assert run.mutations == expected_mutations[run.run_id]


def test_repository_m3_repilot4_plan_records_v4_generation_boundary() -> None:
    _, plan_path, _ = _paths()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    note = plan["measurement_boundary"]["generation_note"]
    assert "workflow_support_v4" in note
    assert "gate v2" in note
    assert "STR-01" in note
    assert plan["execution_conditions"]["seeds"] == [959, 960]
