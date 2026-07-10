from __future__ import annotations

import json
import re
from pathlib import Path

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _load(name: str) -> dict:
    root = Path(__file__).resolve().parents[1]
    return json.loads((root / "docs" / "progress" / name).read_text(encoding="utf-8"))


def test_stall_analysis_pins_zero_completion_mechanism() -> None:
    analysis = _load("phase3_m3_stall_analysis_20260710.json")

    assert analysis["schema_version"] == "company_twin.m3_stall_analysis.v1"
    assert analysis["method"]["cost"] == "zero_api_spend"

    agg = analysis["aggregate"]
    assert agg["send_chat_failed_total"] == 74
    assert agg["send_chat_succeeded_total"] == 4
    assert agg["send_chat_failed_role_name_addressing"] == 74
    assert agg["send_chat_failed_seat_id_addressing"] == 0
    assert agg["send_chat_succeeded_targets"] == {"emp-F": 4}

    lifecycle = agg["lifecycle_tool_attempts_total"]
    assert lifecycle["submit_application"] == {"attempted": 1, "succeeded": 1}
    assert lifecycle["request_approval"] == {"attempted": 1, "succeeded": 1}
    for never_attempted in (
        "verify_identity",
        "link_review",
        "complete_contract",
        "deliver_documents",
        "approve_application",
        "return_application",
    ):
        assert lifecycle[never_attempted] == {"attempted": 0, "succeeded": 0}

    assert agg["application_role_turns_total"] == 5
    assert agg["approver_role_turns_total"] == 6
    assert agg["agent_turns_total"] == 168


def test_stall_analysis_per_trial_counts_and_hashes_match_sealed_receipt() -> None:
    analysis = _load("phase3_m3_stall_analysis_20260710.json")
    receipt = _load("phase3_m3_loss_pilot_result_20260710.json")

    receipt_hashes = {
        row["run_id"].removeprefix("m3_pilot_"): row["world_ledger"]
        for row in receipt["evidence"]["per_run_artifacts"]
    }

    trials = analysis["trials"]
    assert [t["trial_label"] for t in trials] == ["A", "B", "C", "D"]

    submitted = []
    for trial in trials:
        run_dir = trial["run_root"].rsplit("/", 1)[-1]
        assert SHA256_RE.match(trial["world_ledger_sha256"])
        assert SHA256_RE.match(trial["attempts_sha256"])
        assert trial["world_ledger_sha256"] == receipt_hashes[run_dir]

        events = trial["lifecycle_events"]
        assert events["application_drafted"] == 39
        submitted.append(events["application_submitted"])
        for terminal in (
            "identity_verified",
            "review_linked",
            "contract_completed",
            "documents_delivered",
            "approval_granted",
        ):
            assert events[terminal] == 0

        assert trial["application_role_seats"] == ["emp-C"]
        assert trial["approver_role_seats"] == ["emp-M", "emp-Q"]

    assert submitted == [1, 0, 0, 0]


def test_stall_analysis_records_diagnosis_and_forbids_effect_output() -> None:
    analysis = _load("phase3_m3_stall_analysis_20260710.json")

    diagnosis = analysis["diagnosis"]
    assert diagnosis["primary_causes"] == [
        "handoff_address_resolution_missing",
        "workflow_events_not_delivered_to_downstream_inboxes",
    ]
    assert "misleading_send_chat_denial_message" in diagnosis["secondary_causes"]

    boundaries = analysis["boundaries"]
    assert boundaries["effect_estimation"] == "not_performed"
    assert boundaries["mutation_effect_interpretation"] == "forbidden"
    assert boundaries["redesign_requires_separate_owner_approval"] is True
    assert boundaries["confirmatory_campaign"] == "remains_unauthorized"
    assert boundaries["pilot_data_reuse_in_confirmatory"] == "forbidden"

    forbidden_keys = {"arm_rates", "paired_deltas", "effect", "contrast"}
    assert not forbidden_keys & set(analysis.keys())
    assert not forbidden_keys & set(analysis["aggregate"].keys())


def test_redesign_proposal_exists_and_does_not_authorize_execution() -> None:
    root = Path(__file__).resolve().parents[1]
    proposal = (
        root / "docs" / "progress" / "phase3_m3_redesign_proposal_20260710.md"
    ).read_text(encoding="utf-8")

    assert "オーナー承認待ち" in proposal
    assert "未実装・未封印" in proposal
    assert "本文書は何も実行を許可しない" in proposal
    assert "別途の封印planと実行承認" in proposal
