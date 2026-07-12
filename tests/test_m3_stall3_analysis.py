from __future__ import annotations

import json
from pathlib import Path


def _load(name: str) -> dict:
    root = Path(__file__).resolve().parents[1]
    return json.loads((root / "docs" / "progress" / name).read_text(encoding="utf-8"))


def test_stall3_analysis_pins_missing_fact_source_mechanism() -> None:
    analysis = _load("phase3_m3_stall3_analysis_20260713.json")

    assert analysis["schema_version"] == "company_twin.m3_stall3_analysis.v1"

    audit = analysis["fact_source_audit"]
    for fact in ("ekyc_completed", "sanctions_non_hit", "consent_log_id"):
        assert audit[fact]["source_exists"] is False

    agg = analysis["aggregate"]
    assert agg["submit_application_success_total"] == 20
    assert agg["application_received_notice_total"] == 20
    assert agg["empc_turns_total"] == 67
    assert agg["empc_verify_identity_attempts_total"] == 0


def test_stall3_analysis_rules_out_time_budget() -> None:
    analysis = _load("phase3_m3_stall3_analysis_20260713.json")

    timing = analysis["gate_timing_data"]
    assert str(timing["submissions_with_sufficient_remaining_ticks"]).startswith("20/20")
    assert str(timing["submissions_with_insufficient_remaining_ticks"]).startswith("0/20")
    assert str(timing["submissions_at_or_after_scc_switch"]).startswith("0/20")

    # every submission left at least 13 ticks; none happened at/after tick 30
    for run in analysis["per_run"].values():
        ticks = run["submission_ticks"]
        assert max(ticks) <= 27
        assert run["min_remaining_ticks"] == 40 - max(ticks)
        assert run["min_remaining_ticks"] >= 13
        assert run["submissions_at_or_after_scc_switch_tick30"] == 0


def test_stall3_analysis_records_seeded_defect_linkage_and_boundaries() -> None:
    analysis = _load("phase3_m3_stall3_analysis_20260713.json")

    linkage = analysis["diagnosis"]["seeded_defect_linkage"]
    assert "STR-01" in linkage["finding"]
    assert "preserving the STR-01 document defect" in linkage["implication"]

    boundaries = analysis["boundaries"]
    assert boundaries["effect_estimation"] == "not_performed"
    assert boundaries["redesign_requires_separate_owner_approval"] is True
    assert boundaries["this_document_authorizes_no_change_and_no_run"] is True


def test_stall3_report_proposes_but_does_not_authorize() -> None:
    root = Path(__file__).resolve().parents[1]
    report = (
        root / "docs" / "progress" / "phase3_m3_stall3_analysis_20260713.md"
    ).read_text(encoding="utf-8")

    assert "承認#16・2026-07-13承認: 候補1+候補2を採用" in report
    assert "STR-01" in report
    assert "判定条件の再検討に関する材料(判断はオーナー)" in report
