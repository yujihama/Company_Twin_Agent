from __future__ import annotations

import json
from pathlib import Path


def _load(name: str) -> dict:
    root = Path(__file__).resolve().parents[1]
    return json.loads((root / "docs" / "progress" / name).read_text(encoding="utf-8"))


def test_stall2_analysis_pins_missing_customer_id_mechanism() -> None:
    analysis = _load("phase3_m3_stall2_analysis_20260712.json")

    assert analysis["schema_version"] == "company_twin.m3_stall2_analysis.v1"
    assert analysis["method"]["cost"] == "zero_api_spend"

    agg = analysis["aggregate"]
    assert agg["handoff_chats_to_application_role_total"] == 149
    assert agg["handoff_with_application_id_total"] == 145
    assert agg["handoff_with_customer_id_total"] == 0
    assert agg["handoff_with_product_total"] == 94
    assert agg["application_role_turns_total"] == 68
    assert agg["submit_application_attempts_total"] == 9
    assert agg["submit_application_succeeded_total"] == 1


def test_stall2_analysis_agrees_with_repilot_receipt_attempt_counts() -> None:
    analysis = _load("phase3_m3_stall2_analysis_20260712.json")
    receipt = _load("phase3_m3_repilot_result_20260712.json")

    receipt_attempts = [r["submit_application_attempts"]["attempted"] for r in receipt["runs"]]
    assert receipt_attempts == [2, 0, 6, 1]
    assert sum(receipt_attempts) == analysis["aggregate"]["submit_application_attempts_total"]

    receipt_handoffs = [
        r["handoff_chats_delivered_to_application_role"] for r in receipt["runs"]
    ]
    assert sum(receipt_handoffs) == analysis["aggregate"]["handoff_chats_to_application_role_total"]


def test_stall2_analysis_records_diagnosis_and_boundaries() -> None:
    analysis = _load("phase3_m3_stall2_analysis_20260712.json")

    primary = " ".join(analysis["diagnosis"]["primary_causes"])
    assert "customer_id_never_rendered_to_any_seat_prompt" in primary
    assert "no_lookup_tool_exists" in primary

    boundaries = analysis["boundaries"]
    assert boundaries["effect_estimation"] == "not_performed"
    assert boundaries["mutation_effect_interpretation"] == "forbidden"
    assert boundaries["redesign_requires_separate_owner_approval"] is True
    assert boundaries["this_document_authorizes_no_change_and_no_run"] is True


def test_stall2_report_proposes_but_does_not_authorize() -> None:
    root = Path(__file__).resolve().parents[1]
    report = (
        root / "docs" / "progress" / "phase3_m3_stall2_analysis_20260712.md"
    ).read_text(encoding="utf-8")

    assert "承認#15・2026-07-12承認: 候補1+候補2を採用" in report
    assert "本文書はいかなる実行も許可しない" in report
    assert "顧客ID(CUS-パターン)を含むものは" in report
