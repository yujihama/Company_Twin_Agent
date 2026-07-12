from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _receipt() -> dict:
    path = _root() / "docs" / "progress" / "phase3_m3_repilot_result_20260712.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_repilot_result_records_no_go_with_two_independent_grounds() -> None:
    receipt = _receipt()

    decision = receipt["decision"]
    assert decision["pilot_checks_passed"] is False
    assert decision["next_campaign_prerequisite"] == "not_satisfied"
    assert decision["effect_estimation"] == "not_performed"
    assert decision["separate_owner_approval_required"] is True
    assert decision["confirmatory_runs_executed"] is False
    assert [g["ground"] for g in decision["grounds"]] == [
        "ledger_write_order_integrity_failure",
        "zero_completed_cases",
    ]

    boundaries = receipt["boundaries"]
    assert boundaries["run_artifacts_untouched"] is True
    assert boundaries["no_pooling_with_20260710_pilot"] is True
    assert boundaries["confirmatory_campaign"] == "remains_unauthorized"

    forbidden = {"arm_rates", "paired_deltas", "effect", "contrast", "direction"}
    assert not forbidden & set(receipt.keys())


def test_repilot_result_per_run_facts_are_frozen() -> None:
    receipt = _receipt()
    runs = receipt["runs"]
    assert [r["trial_label"] for r in runs] == ["A", "B", "C", "D"]

    assert [r["ledger_hash_chain_file_order_valid"] for r in runs] == [False, False, True, True]
    assert [r["ledger_file_order_break_at_ordinal"] for r in runs] == [393, 404, None, None]
    assert [r["completed_case_count"] for r in runs] == [0, 0, 0, 0]
    assert [r["lifecycle_events"]["application_submitted"] for r in runs] == [1, 0, 0, 0]
    assert [r["handoff_chats_delivered_to_application_role"] for r in runs] == [26, 39, 38, 46]
    assert [r["application_role_turns"] for r in runs] == [14, 22, 16, 16]

    for run in runs:
        assert run["lifecycle_events"]["identity_verified"] == 0
        assert run["lifecycle_events"]["contract_completed"] == 0
        assert run["lifecycle_events"]["documents_delivered"] == 0
        for value in run["artifact_sha256"].values():
            assert SHA256_RE.match(value)

    # The R4 pair reached the monitoring join; the R1 pair failed closed.
    assert [r["loss_event_monitoring"] for r in runs] == [
        "fail_closed_reordered_ledger",
        "fail_closed_reordered_ledger",
        "written",
        "written",
    ]
    assert [r["r3_opportunities"] for r in runs] == [None, None, 0, 0]


def test_repilot_result_pins_sealed_plan_and_batch_bytes() -> None:
    root = _root()
    receipt = _receipt()
    for key in ("plan", "batch_spec"):
        entry = receipt[key]
        actual = sha256((root / entry["path"]).read_bytes()).hexdigest()
        assert entry["sha256"] == actual


def test_repilot_result_report_states_no_go_and_next_boundary() -> None:
    report = (
        _root() / "docs" / "progress" / "phase3_m3_repilot_result_20260712.md"
    ).read_text(encoding="utf-8")
    assert "no_go" in report
    assert "完了案件ゼロ" in report
    assert "書込順序" in report
    assert "承認#15候補" in report
    assert "confirmatory 20-runは引き続き未承認" in report
