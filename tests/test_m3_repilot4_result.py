from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _receipt() -> dict:
    path = _root() / "docs" / "progress" / "phase3_m3_repilot4_result_20260721.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_repilot4_result_records_first_go() -> None:
    receipt = _receipt()

    decision = receipt["decision"]
    assert decision["pilot_checks_passed"] is True
    assert decision["gate_decision"] == "go"
    assert decision["next_campaign_prerequisite"] == "satisfied"
    assert decision["effect_estimation"] == "not_performed"
    assert decision["confirmatory_runs_executed"] is False

    boundaries = receipt["boundaries"]
    assert boundaries["run_artifacts_untouched"] is True
    assert boundaries["no_pooling_with_prior_generations"] is True
    assert boundaries["pilot_data_reuse_in_confirmatory"] == "forbidden"
    assert "unauthorized" in boundaries["confirmatory_campaign"]

    forbidden = {"arm_rates", "paired_deltas", "effect", "contrast", "direction"}
    assert not forbidden & set(receipt.keys())


def test_repilot4_result_per_run_facts_are_frozen() -> None:
    receipt = _receipt()
    runs = receipt["runs"]
    assert [r["trial_label"] for r in runs] == ["A", "B", "C", "D"]

    assert all(r["ledger_hash_chain_file_order_valid"] for r in runs)
    assert [r["ticks_committed"] for r in runs] == [40, 40, 40, 40]
    assert [r["completed_case_count"] for r in runs] == [7, 2, 6, 6]
    assert [r["gate"]["r3_opportunity_count"] for r in runs] == [7, 2, 6, 6]
    assert [r["gate"]["r3_event_count"] for r in runs] == [0, 0, 0, 0]
    assert all(r["gate"]["passed"] for r in runs)

    facts = receipt["v4_mechanism_facts"]
    assert facts["funnel_totals"]["contract_completed"] == 21
    assert facts["completions_per_trial"] == [7, 2, 6, 6]

    for run in runs:
        for value in run["artifact_sha256"].values():
            assert SHA256_RE.match(value)


def test_repilot4_result_pins_sealed_plan_and_batch_bytes() -> None:
    root = _root()
    receipt = _receipt()
    for key in ("plan", "batch_spec"):
        entry = receipt[key]
        actual = sha256((root / entry["path"]).read_bytes()).hexdigest()
        assert entry["sha256"] == actual
    assert len(receipt["batch_manifests"]) == 5


def test_repilot4_report_states_go_and_confirmatory_boundary() -> None:
    report = (
        _root() / "docs" / "progress" / "phase3_m3_repilot4_result_20260721.md"
    ).read_text(encoding="utf-8")
    assert "判定は go" in report
    assert "M3系で初の合格" in report
    assert "自体は未承認のまま" in report
    assert "新世代のconfirmatory封印plan" in report
