from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _receipt() -> dict:
    path = _root() / "docs" / "progress" / "phase3_m3_repilot3_result_20260713.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_repilot3_result_records_no_go_with_first_passing_run() -> None:
    receipt = _receipt()

    decision = receipt["decision"]
    assert decision["pilot_checks_passed"] is False
    assert decision["gate_decision"] == "no_go"
    assert decision["effect_estimation"] == "not_performed"
    assert decision["confirmatory_runs_executed"] is False
    assert [g["ground"] for g in decision["grounds"]] == ["completion_funnel_attenuation"]

    facts = receipt["v3_mechanism_facts"]
    assert facts["first_gate_passing_run"] == "m3_repilot3_r4_contradict_seed958"
    assert facts["funnel_totals"] == {
        "application_submitted": 26,
        "identity_check_performed": 22,
        "identity_verified": 20,
        "review_linked": 4,
        "contract_completed": 1,
        "documents_delivered": 1,
    }

    boundaries = receipt["boundaries"]
    assert boundaries["run_artifacts_untouched"] is True
    assert boundaries["no_pooling_with_prior_generations"] is True
    assert boundaries["confirmatory_campaign"] == "remains_unauthorized"

    forbidden = {"arm_rates", "paired_deltas", "effect", "contrast", "direction"}
    assert not forbidden & set(receipt.keys())


def test_repilot3_result_per_run_facts_are_frozen() -> None:
    receipt = _receipt()
    runs = receipt["runs"]
    assert [r["trial_label"] for r in runs] == ["A", "B", "C", "D"]

    assert all(r["ledger_hash_chain_file_order_valid"] for r in runs)
    assert [r["ticks_committed"] for r in runs] == [40, 40, 40, 40]
    assert [r["funnel_events"]["application_submitted"] for r in runs] == [7, 8, 4, 7]
    assert [r["funnel_events"]["identity_verified"] for r in runs] == [4, 4, 4, 8]
    assert [r["funnel_events"]["review_linked"] for r in runs] == [0, 3, 0, 1]
    assert [r["funnel_events"]["contract_completed"] for r in runs] == [0, 0, 0, 1]
    assert [r["gate"]["passed"] for r in runs] == [False, False, False, True]
    assert [r["gate"]["r3_opportunity_count"] for r in runs] == [0, 0, 0, 1]
    assert [r["gate"]["r3_event_count"] for r in runs] == [0, 0, 0, 0]

    for run in runs:
        for value in run["artifact_sha256"].values():
            assert SHA256_RE.match(value)


def test_repilot3_result_pins_sealed_plan_and_batch_bytes() -> None:
    root = _root()
    receipt = _receipt()
    for key in ("plan", "batch_spec"):
        entry = receipt[key]
        actual = sha256((root / entry["path"]).read_bytes()).hexdigest()
        assert entry["sha256"] == actual


def test_repilot3_report_states_no_go_and_owner_options() -> None:
    report = (
        _root() / "docs" / "progress" / "phase3_m3_repilot3_result_20260713.md"
    ).read_text(encoding="utf-8")
    assert "no_go" in report
    assert "初めて単独のrunゲートに合格" in report
    assert "漏斗の減衰" in report
    assert "承認#17候補" in report
    assert "測定方式の変更=オーナー承認事項" in report
    assert "confirmatory 20-runは引き続き未承認" in report
