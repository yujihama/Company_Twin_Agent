from __future__ import annotations

import json
from pathlib import Path


def _load(name: str) -> dict:
    root = Path(__file__).resolve().parents[1]
    return json.loads((root / "docs" / "progress" / name).read_text(encoding="utf-8"))


def test_stall4_analysis_pins_attention_allocation_mechanism() -> None:
    analysis = _load("phase3_m3_stall4_analysis_20260713.json")

    assert analysis["schema_version"] == "company_twin.m3_stall4_analysis.v1"

    agg = analysis["aggregate"]
    assert agg["post_first_verify_turns_total"] == 48
    assert agg["turns_with_advance_total"] == 11
    assert agg["turns_without_advance_total"] == 37
    assert agg["lookup_application_total"] == 88
    assert agg["funnel_total"] == {
        "submit_application": 26,
        "run_identity_check": 22,
        "verify_identity": 20,
        "link_review": 4,
        "complete_contract": 1,
        "deliver_documents": 1,
    }

    primary = " ".join(analysis["diagnosis"]["primary_causes"])
    assert "attention_allocation_not_id_source_scarcity" in primary
    assert "turn_allocation_not_per_turn_budget" in primary


def test_stall4_analysis_rules_out_id_fact_source_gap() -> None:
    analysis = _load("phase3_m3_stall4_analysis_20260713.json")
    audit = analysis["fact_source_audit"]
    for key in ("review_ticket_id", "contract_id", "delivery_id"):
        entry = audit[key]
        payload = json.dumps(entry, ensure_ascii=False)
        # kernel accepts any non-empty string; seats self-invent these ids
        assert "non-empty" in payload or "自己" in payload or "self" in payload


def test_stall4_analysis_agrees_with_repilot3_receipt() -> None:
    analysis = _load("phase3_m3_stall4_analysis_20260713.json")
    receipt = _load("phase3_m3_repilot3_result_20260713.json")
    # analysis keys are tool names; receipt keys are ledger event names
    tool_to_event = {
        "submit_application": "application_submitted",
        "run_identity_check": "identity_check_performed",
        "verify_identity": "identity_verified",
        "link_review": "review_linked",
        "complete_contract": "contract_completed",
        "deliver_documents": "documents_delivered",
    }
    funnel = analysis["aggregate"]["funnel_total"]
    receipt_funnel = receipt["v3_mechanism_facts"]["funnel_totals"]
    for tool, event in tool_to_event.items():
        assert funnel[tool] == receipt_funnel[event]


def test_stall4_report_proposes_but_does_not_authorize() -> None:
    root = Path(__file__).resolve().parents[1]
    report = (
        root / "docs" / "progress" / "phase3_m3_stall4_analysis_20260713.md"
    ).read_text(encoding="utf-8")
    assert "承認#17候補" in report
    boundaries = _load("phase3_m3_stall4_analysis_20260713.json")["boundaries"]
    assert boundaries["this_document_authorizes_no_change_and_no_run"] is True
    assert boundaries["redesign_requires_separate_owner_approval"] is True
