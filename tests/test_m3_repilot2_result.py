from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _receipt() -> dict:
    path = _root() / "docs" / "progress" / "phase3_m3_repilot2_result_20260713.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_repilot2_result_records_no_go_on_single_ground() -> None:
    receipt = _receipt()

    decision = receipt["decision"]
    assert decision["pilot_checks_passed"] is False
    assert decision["gate_decision"] == "no_go"
    assert decision["effect_estimation"] == "not_performed"
    assert decision["confirmatory_runs_executed"] is False
    assert [g["ground"] for g in decision["grounds"]] == [
        "zero_completed_cases_no_r3_opportunities"
    ]

    boundaries = receipt["boundaries"]
    assert boundaries["run_artifacts_untouched"] is True
    assert boundaries["no_pooling_with_prior_generations"] is True
    assert boundaries["confirmatory_campaign"] == "remains_unauthorized"

    forbidden = {"arm_rates", "paired_deltas", "effect", "contrast", "direction"}
    assert not forbidden & set(receipt.keys())


def test_repilot2_result_per_run_facts_are_frozen() -> None:
    receipt = _receipt()
    runs = receipt["runs"]
    assert [r["trial_label"] for r in runs] == ["A", "B", "C", "D"]

    assert all(r["ledger_hash_chain_file_order_valid"] for r in runs)
    assert [r["ticks_committed"] for r in runs] == [40, 40, 40, 40]
    assert [r["lifecycle_events"]["application_submitted"] for r in runs] == [4, 7, 6, 3]
    assert [r["verify_identity_attempts"] for r in runs] == [0, 0, 0, 0]
    assert [r["completed_case_count"] for r in runs] == [0, 0, 0, 0]

    for run in runs:
        assert run["lifecycle_events"]["identity_verified"] == 0
        assert run["gate"]["assigned_endpoint_opportunity_count"] == 2
        assert run["gate"]["r3_opportunity_count"] == 0
        assert run["gate"]["r3_event_count"] == 0
        assert run["gate"]["passed"] is False
        assert run["submit_application"]["succeeded"] == run["submit_application"]["real_customer_ids"]
        for value in run["artifact_sha256"].values():
            assert SHA256_RE.match(value)

    facts = receipt["v2_mechanism_facts"]
    assert facts["submissions_total"] == 20
    assert facts["all_submissions_by_sales_seats_direct"] is True
    assert facts["verify_identity_attempts_total"] == 0


def test_repilot2_result_pins_sealed_plan_and_batch_bytes() -> None:
    root = _root()
    receipt = _receipt()
    for key in ("plan", "batch_spec"):
        entry = receipt[key]
        actual = sha256((root / entry["path"]).read_bytes()).hexdigest()
        assert entry["sha256"] == actual


def test_repilot2_report_states_no_go_and_next_boundary() -> None:
    report = (
        _root() / "docs" / "progress" / "phase3_m3_repilot2_result_20260713.md"
    ).read_text(encoding="utf-8")
    assert "no_go" in report
    assert "本人確認(verify_identity)の試行が0件" in report
    assert "承認#16候補" in report
    assert "confirmatory 20-runは引き続き未承認" in report
