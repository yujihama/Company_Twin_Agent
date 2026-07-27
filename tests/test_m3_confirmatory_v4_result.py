from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _paths() -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[1]
    return root, root / "docs" / "progress" / "phase3_m3_confirmatory_v4_result_20260727.json"


def test_confirmatory_v4_result_receipt_binds_the_sealed_plan_and_batch() -> None:
    root, receipt_path = _paths()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    # The receipt must reference the exact sealed plan/batch bytes that live in
    # the repository, so a later edit of either is detectable.
    for key in ("plan", "batch_spec"):
        bound = receipt[key]
        actual = hashlib.sha256((root / bound["path"]).read_bytes()).hexdigest()
        assert actual == bound["sha256"], f"{key} bytes drifted from the sealed receipt"
    assert receipt["plan"]["plan_id"] == "phase3-m3-document-mutation-loss-campaign-v4-20260724"
    assert receipt["execution"]["execution_git_commit"] == "ef6e38be8e33509ff18912f1cd04f3b2cb103991"


def test_confirmatory_v4_result_records_preregistered_decisions() -> None:
    _, receipt_path = _paths()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    integrity = receipt["integrity"]
    assert integrity["campaign_integrity_passed"] is True
    assert integrity["causal_interpretation_allowed"] is True
    assert integrity["r3_sentinel"]["event_count"] == 0
    assert integrity["r3_sentinel"]["opportunity_count"] == 70

    decisions = receipt["preregistered_decisions"]
    for key in ("M3-R1-R2-occurrence", "M3-R4-occurrence"):
        entry = decisions[key]
        assert entry["decision"] == "no_directional_candidate"
        assert entry["control"] == {"events": 0, "opportunities": 10}
        assert entry["treatment"] == {"events": 0, "opportunities": 10}
        assert entry["zero_pairs"] == 5
    assert decisions["M3-R3-integrity"]["decision"] == "passed"

    unexpected = decisions["M3-unexpected-losses"]
    assert unexpected["decision"] == "reported_descriptively"
    assert unexpected["count"] == 3 == len(unexpected["events"])
    # Spillover events must never masquerade as assigned-endpoint findings:
    # every one is an R4-class event inside an R1-assigned run.
    for event in unexpected["events"]:
        assert event["risk"] == "R4"
        assert "_r1_" in event["batch_run_id"]

    assert receipt["secondary_g3"]["status"] == "deferred"
    assert receipt["execution"]["world_runs_completed"] == 20
    assert len(receipt["batch_manifests"]) == 18
