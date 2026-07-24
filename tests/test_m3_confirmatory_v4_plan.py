from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from company_twin.loss_campaign import (
    LossCampaignError,
    _validate_managed_execution_authorization,
    _validate_sealed_batch_spec,
    load_loss_campaign_plan,
)
from company_twin.parallel_runner import BatchSpec

R1_SEEDS = [961, 962, 963, 964, 965]
R4_SEEDS = [966, 967, 968, 969, 970]


def _paths() -> tuple[Path, Path, Path]:
    root = Path(__file__).resolve().parents[1]
    return (
        root,
        root / "docs" / "progress" / "phase3_m3_loss_campaign_v4_plan_20260724.json",
        root / "docs" / "progress" / "phase3_m3_loss_campaign_v4_batch_20260724.json",
    )


def test_repository_m3_confirmatory_v4_plan_is_a_non_executable_draft() -> None:
    root, plan_path, batch_path = _paths()
    plan = load_loss_campaign_plan(plan_path, root=root)
    batch = BatchSpec.from_dict(json.loads(batch_path.read_text(encoding="utf-8")))
    assignments = _validate_sealed_batch_spec(plan, batch, root=root)

    assert hashlib.sha256(batch_path.read_bytes()).hexdigest() == plan["batch_spec_sha256"]
    assert plan["campaign_role"] == "confirmatory"
    assert plan["kind"] == "pre_execution_confirmatory_plan_draft"
    assert plan["approval_granted_by_this_file"] is False
    assert plan["execution_authorized_by_this_file"] is False
    assert plan["cost_guard"]["execution_authorized_by_this_file"] is False
    # Confirmatory plans must not carry a pilot gate.
    assert "pilot_gate" not in plan

    # The draft must be refused by managed aggregation until a separate
    # owner-approval commit changes kind and flips the booleans.
    with pytest.raises(LossCampaignError):
        _validate_managed_execution_authorization(plan, batch)

    assert batch.credit_guard is not None
    assert batch.credit_guard.to_dict() == {
        "minimum_credits": 9.0,
        "abort_on_low_credits": True,
        "require_available": True,
    }
    # Five fixed-order waves of four runs: one complete R1 pair plus one
    # complete R4 pair per wave, each behind its own credit preflight.
    assert [wave.wave_id for wave in batch.waves] == [f"wave-{i}" for i in range(1, 6)]
    assert [len(wave.run_ids) for wave in batch.waves] == [4, 4, 4, 4, 4]
    assert plan["wave_execution_policy"]["ordered_wave_ids"] == [f"wave-{i}" for i in range(1, 6)]
    assert plan["execution_conditions"]["wave_ids"] == [f"wave-{i}" for i in range(1, 6)]

    assert len(assignments) == 20
    assert {assignment["seed"] for assignment in assignments.values()} == set(R1_SEEDS + R4_SEEDS)
    assert plan["execution_conditions"]["seeds"] == R1_SEEDS + R4_SEEDS

    expected_mutations: dict[str, list[str]] = {}
    for seed in R1_SEEDS:
        expected_mutations[f"m3_conf_v4_r1_control_seed{seed}"] = []
        expected_mutations[f"m3_conf_v4_r1_clarify_seed{seed}"] = ["clarify_elderly_understanding_sales_only"]
    for seed in R4_SEEDS:
        expected_mutations[f"m3_conf_v4_r4_control_seed{seed}"] = []
        expected_mutations[f"m3_conf_v4_r4_contradict_seed{seed}"] = ["contradict_chat_approval_recorded"]
    assert {run.run_id for run in batch.runs} == set(expected_mutations)
    for run in batch.runs:
        assert str(run.run_root).startswith("runs/phase3_m3_confirmatory_v4_20260724/")
        assert run.extra_args == ["--circulate-notices", "--workflow-support"]
        assert run.mutations == expected_mutations[run.run_id]

    # Each wave keeps both pairs of one R1 seed and one R4 seed together.
    for index, wave in enumerate(batch.waves):
        r1_seed = R1_SEEDS[index]
        r4_seed = R4_SEEDS[index]
        assert list(wave.run_ids) == [
            f"m3_conf_v4_r1_control_seed{r1_seed}",
            f"m3_conf_v4_r1_clarify_seed{r1_seed}",
            f"m3_conf_v4_r4_control_seed{r4_seed}",
            f"m3_conf_v4_r4_contradict_seed{r4_seed}",
        ]


def test_repository_m3_confirmatory_v4_plan_binds_the_passing_repilot4_prerequisite() -> None:
    root, plan_path, _ = _paths()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    prerequisite = plan["pilot_prerequisite"]
    assert prerequisite["plan_id"] == "phase3-m3-document-mutation-feasibility-repilot4-20260720"
    assert prerequisite["required_result"] == "pilot_gate.passed=true"
    assert prerequisite["result_verdict"] == "go"
    assert prerequisite["pilot_seeds_excluded"] == [959, 960]
    for key in ("plan_path", "result_path"):
        bound_path = root / str(prerequisite[key])
        sha_key = key.replace("_path", "_sha256")
        assert hashlib.sha256(bound_path.read_bytes()).hexdigest() == prerequisite[sha_key]


def test_repository_m3_confirmatory_v4_plan_records_generation_and_seed_boundaries() -> None:
    _, plan_path, _ = _paths()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    note = plan["measurement_boundary"]["generation_note"]
    assert "workflow_support_v4" in note
    assert "STR-01" in note
    seeds_note = plan["execution_conditions"]["seeds_note"]
    assert "961-970" in seeds_note
    assert "940-949" in seeds_note
    assert "951-960" in seeds_note
