from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path

from company_twin.loss_monitoring import load_loss_monitor_rules


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def test_repository_m3_pilot_result_records_no_go_without_effect_output() -> None:
    root = Path(__file__).resolve().parents[1]
    result_path = root / "docs" / "progress" / "phase3_m3_loss_pilot_result_20260710.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))

    assert result["decision"] == {
        "pilot_checks_passed": False,
        "next_campaign_prerequisite": "not_satisfied",
        "reason": (
            "All four runs exercised their assigned business situation, but none produced a completed "
            "case from which the required completion-order check could be evaluated."
        ),
        "separate_owner_approval_required": True,
        "confirmatory_runs_executed": False,
    }
    assert result["checks"]["world_runs_completed"] == 4
    assert result["checks"]["world_runs_failed"] == 0
    assert result["checks"]["artifact_verification_passed"] is True
    assert result["checks"]["target_business_situations"] == {
        "required_per_run": 1,
        "observed_total": 8,
        "observed_per_run": [2, 2, 2, 2],
        "all_runs_met": True,
    }
    assert result["checks"]["completed_cases"] == {
        "required_per_run": 1,
        "observed_total": 0,
        "completed_before_identity_verification": 0,
        "all_runs_met": False,
    }
    assert len(result["runs"]) == 4
    assert all(run["target_business_situation_count"] == 2 for run in result["runs"])
    assert all(run["completed_case_count"] == 0 for run in result["runs"])
    assert all(run["completed_before_identity_verification"] == 0 for run in result["runs"])
    assert all(run["notice_delivery_correct"] is True for run in result["runs"])
    assert all(run["passed"] is False for run in result["runs"])

    assert result["boundaries"] == {
        "effect_comparison_performed": False,
        "causal_claim_allowed": False,
        "pilot_runs_eligible_for_later_pooling": False,
        "readiness_updated": False,
        "confirmatory_execution_authorized": False,
    }
    assert "arms" not in result
    assert "contrasts" not in result
    assert "rate_difference" not in result

    evidence = result["evidence"]
    hashes = [
        evidence["plan"]["sha256"],
        evidence["batch_spec"]["sha256"],
        evidence["batch_manifests"][0]["sha256"],
        evidence["raw_report"]["sha256"],
        evidence["monitor_rules_sha256"],
    ]
    for artifacts in evidence["per_run_artifacts"]:
        hashes.extend(
            artifacts[key]
            for key in ("config", "loss_events", "meta", "monitoring", "world_ledger")
        )
    assert all(SHA256_RE.fullmatch(value) for value in hashes)
    assert (
        sha256((root / evidence["plan"]["path"]).read_bytes()).hexdigest()
        == evidence["plan"]["sha256"]
    )
    assert (
        sha256((root / evidence["batch_spec"]["path"]).read_bytes()).hexdigest()
        == evidence["batch_spec"]["sha256"]
    )
    assert _canonical_sha256(load_loss_monitor_rules(root)) == evidence["monitor_rules_sha256"]
    assert (
        _canonical_sha256(evidence)
        == "a283846e47d6f8c7d232a75be838794c3a3d6b284ddff175585be9b2e72311ab"
    )
