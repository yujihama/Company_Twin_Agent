from __future__ import annotations

import json
from pathlib import Path


def _plan() -> dict:
    root = Path(__file__).resolve().parents[1]
    path = root / "docs" / "progress" / "option_enum_first_measurement_plan_20260727.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_first_enumeration_plan_is_non_executable_and_internally_consistent() -> None:
    plan = _plan()
    assert plan["kind"] == "pre_execution_plan_pending_owner_approval"
    assert "authorizes no spend" in plan["approval_status"]
    assert plan["instrument"]["claim_level"] == "option_space_awareness_sandbox"
    # sealed scale must be arithmetically consistent
    runs = plan["target_runs"]
    assert runs["run_count"] == 20 == len(runs["ledger_sha256"])
    assert set(plan["probes"]) == {"P-11", "P-04"}
    assert plan["total_samples"] == runs["run_count"] * len(plan["probes"]) * plan["n_samples_per_run_per_probe"]
    # every sealed bundle hash is a lowercase sha256
    for name, digest in runs["ledger_sha256"].items():
        assert len(digest) == 64 and digest == digest.lower(), name
    # Layer 3 must be explicitly deferred with the recorded mechanical reason,
    # not silently dropped
    layer3 = plan["layer3_disposition"]
    assert layer3["status"] == "deferred_with_recorded_reason"
    assert "tick" in layer3["mechanical_fact"]
