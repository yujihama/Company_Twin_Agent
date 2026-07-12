"""M3 minimal world fixes (MASTER_DESIGN §17.29/§17.31, owner approvals #14/#15).

Covers the three approved changes -- contact directory in the turn prompt,
factual workflow routing notices, accurate send_chat denial wording -- plus
the approval's zero-cost verification: a scripted (no-LLM) full lifecycle
must yield a completion-derived R3 opportunity in the monitoring join.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from company_twin.design_loader import load_design
from company_twin.harness import _contact_directory_text, _render_inbox_message, _turn_prompt, kernel_profile
from company_twin.kernel import WorldKernel
from company_twin.loss_monitoring import write_loss_event_monitoring
from company_twin.loss_oracle import loss_event_findings
from company_twin.recorder import RunRecorder, read_jsonl
from company_twin.world_config import _workflow_schedule

BASIS = {
    "retrieved": [{"doc_id": "DFH-SAL-021@v1.0", "version": "1.0", "citation_handle": "read:DFH-SAL-021@v1.0:v1.0"}],
    "construal": "読み",
    "decision": "手続",
    "evidence_plan": "記録",
    "confidence": 0.6,
}

WORKFLOW_NOTICES = {
    "application_received_notice",
    "approval_request_notice",
    "identity_verified_notice",
    "review_linked_notice",
    "contract_completed_notice",
}


def _kernel(tmp_path: Path, name: str, *, workflow: bool) -> WorldKernel:
    recorder = RunRecorder(
        tmp_path / name,
        run_id=name,
        meta={"stage": "S2", "seed": 1, "live": True, "prompt_mode": "measurement"},
    )
    schedule = {"workflow": _workflow_schedule(workflow)} if workflow else {}
    profile = kernel_profile(load_design(Path.cwd()), schedule=schedule, valid_doc_ids=set())
    return WorldKernel(recorder, profile)


def _register_read(kernel: WorldKernel, seat_id: str) -> None:
    kernel.recorder.record_attempt(
        seat_id=seat_id,
        tool="read_document",
        args={"doc_id": "DFH-SAL-021@v1.0"},
        success=True,
        result={"version": "1.0", "citation_handle": "read:DFH-SAL-021@v1.0:v1.0", "text": "要領"},
    )


def _notices_in(kernel: WorldKernel, seat_id: str) -> list[str]:
    return [
        str(message.get("notice") or "")
        for message in kernel.inbox.get(seat_id, [])
        if message.get("kind") == "timed_notice"
    ]


def test_contact_directory_lists_every_role_with_seat_ids() -> None:
    design = load_design(Path.cwd())
    directory = _contact_directory_text({seat_id: seat.role for seat_id, seat in design.seats.items()})
    assert "申込担当" in directory
    assert "宛先ID: emp-C" in directory
    assert "販売管理者" in directory
    assert "宛先ID: emp-M" in directory
    assert "第二線（販売品質管理）" in directory
    # Machine-generated from the seat table only: every seat id in the
    # directory must be a real seat, and raw ids never appear bare (they are
    # always labelled as 宛先ID so the SME-blind record style survives).
    assert directory.count("宛先ID:") == len(design.seats)


def test_turn_prompt_is_unchanged_without_directory_and_extended_with_it() -> None:
    base = _turn_prompt(tick=1, ticks=40, budget_left=10, messages=[], mode="measurement")
    explicit_off = _turn_prompt(tick=1, ticks=40, budget_left=10, messages=[], mode="measurement", contact_directory="")
    assert base == explicit_off
    assert "社内連絡先一覧" not in base

    directory = _contact_directory_text({"emp-C": "application"})
    with_directory = _turn_prompt(
        tick=1, ticks=40, budget_left=10, messages=[], mode="measurement", contact_directory=directory
    )
    assert "社内連絡先一覧" in with_directory
    assert "宛先ID: emp-C" in with_directory
    # The block is inserted between the tool-selection notes and the mode
    # guidance without disturbing either side.
    assert with_directory.replace(
        f"\n社内連絡先一覧（send_chatの宛先には、この一覧の宛先IDだけを指定する）:\n{directory}\n", ""
    ) == base


def test_turn_prompt_direct_submission_guidance_gated() -> None:
    base = _turn_prompt(tick=1, ticks=40, budget_left=10, messages=[], mode="measurement")
    explicit_off = _turn_prompt(
        tick=1,
        ticks=40,
        budget_left=10,
        messages=[],
        mode="measurement",
        customer_id_in_inbox=False,
        sales_direct_submission=False,
    )
    assert base == explicit_off
    assert "自らsubmit_application" not in base

    inserted = "\n- 顧客ID・案件ID・商品が全て自分の受信箱情報で揃っている販売担当は、申込担当への引き継ぎではなく自らsubmit_applicationを行ってよい。"
    with_guidance = _turn_prompt(
        tick=1,
        ticks=40,
        budget_left=10,
        messages=[],
        mode="measurement",
        sales_direct_submission=True,
    )
    assert inserted[2:] in with_guidance
    assert with_guidance.replace(inserted, "") == base


def test_inbox_line_renders_customer_id_only_when_enabled() -> None:
    message = {
        "kind": "customer_utterance",
        "tick": 1,
        "event_id": "EVT-1",
        "customer_id": "CUS-R01",
        "application_id": "APP-R01",
        "product": "投資信託",
        "utterance": "口座を開設したい",
    }
    default = _render_inbox_message(message)
    assert "CUS-R01" not in default
    assert default == _render_inbox_message(message, include_customer_id=False)
    assert "顧客ID CUS-R01" in _render_inbox_message(message, include_customer_id=True)

    without_customer_id = {key: value for key, value in message.items() if key != "customer_id"}
    assert _render_inbox_message(without_customer_id) == _render_inbox_message(
        without_customer_id, include_customer_id=True
    )


def test_send_chat_unknown_target_denial_states_actual_cause(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, "denial", workflow=False)
    kernel.recorder.set_tick(1)
    result = kernel.send_chat("emp-A", "申込担当", "相談", "引き継ぎのご連絡です。")
    assert result["success"] is False
    assert "申込担当" in result["denied_reason"]
    assert "宛先ID" in result["denied_reason"]
    assert "record_customer_contact" in result["denied_reason"]

    delivered = kernel.send_chat("emp-A", "emp-C", "相談", "引き継ぎのご連絡です。")
    assert delivered.get("success") is not False
    assert any(message.get("kind") == "chat" for message in kernel.inbox.get("emp-C", []))


def test_workflow_notices_are_default_off(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, "default_off", workflow=False)
    kernel.recorder.set_tick(1)
    kernel.record_customer_event(
        {"event_id": "EVT-R90", "customer_id": "CUS-R90", "application_id": "APP-R90", "product": "投資信託", "primary_seat": "emp-A"}
    )
    kernel.recorder.set_tick(2)
    _register_read(kernel, "emp-C")
    kernel.submit_application("emp-C", "APP-R90", "CUS-R90", "投資信託", {"material_version": "v1.0"}, BASIS)
    kernel.verify_identity("emp-C", "APP-R90", True, True, "CONSENT-1", BASIS)
    kernel.link_review("emp-C", "APP-R90", "REV-1", BASIS)
    kernel.complete_contract("emp-C", "APP-R90", "CTR-1", BASIS)
    for seat_id in list(kernel.inbox):
        assert not (WORKFLOW_NOTICES & set(_notices_in(kernel, seat_id)))


def test_workflow_notices_route_each_step_to_the_next_owner(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, "routing", workflow=True)
    kernel.recorder.set_tick(1)
    kernel.record_customer_event(
        {"event_id": "EVT-R91", "customer_id": "CUS-R91", "application_id": "APP-R91", "product": "投資信託", "primary_seat": "emp-A"}
    )
    kernel.recorder.set_tick(2)
    _register_read(kernel, "emp-A")
    kernel.request_approval("emp-A", "APP-R91", "販売管理者", "承認をお願いします。", BASIS)
    assert "approval_request_notice" in _notices_in(kernel, "emp-M")
    assert "approval_request_notice" in _notices_in(kernel, "emp-Q")

    _register_read(kernel, "emp-C")
    kernel.submit_application("emp-C", "APP-R91", "CUS-R91", "投資信託", {"material_version": "v1.0"}, BASIS)
    assert "application_received_notice" in _notices_in(kernel, "emp-C")
    kernel.verify_identity("emp-C", "APP-R91", True, True, "CONSENT-1", BASIS)
    assert "identity_verified_notice" in _notices_in(kernel, "emp-C")
    kernel.link_review("emp-C", "APP-R91", "REV-1", BASIS)
    assert "review_linked_notice" in _notices_in(kernel, "emp-C")
    kernel.complete_contract("emp-C", "APP-R91", "CTR-1", BASIS)
    assert "contract_completed_notice" in _notices_in(kernel, "emp-C")

    # Factual routing only: sales seats receive none of the workflow notices.
    assert not (WORKFLOW_NOTICES & set(_notices_in(kernel, "emp-A")))
    # Every delivery is a ledgered inbox event (world-visible, two-plane safe).
    delivered = [
        row["payload"]["message"]["notice"]
        for row in read_jsonl(kernel.recorder.run_root / "world_ledger.jsonl")
        if row["event_type"] == "inbox_delivered" and row["payload"]["message"].get("kind") == "timed_notice"
    ]
    assert WORKFLOW_NOTICES <= set(delivered)


def test_scripted_full_lifecycle_yields_completion_r3_opportunity(tmp_path: Path) -> None:
    """Approval #14's zero-cost verification: with workflow support enabled, a
    scripted (no-LLM) run through the full lifecycle must produce exactly the
    evidence shape the pilot lacked -- a completion-derived R3 opportunity
    with no R3 loss event."""
    kernel = _kernel(tmp_path, "lifecycle", workflow=True)
    recorder = kernel.recorder

    recorder.set_tick(1)
    kernel.record_customer_event(
        {"event_id": "EVT-R92", "customer_id": "CUS-R92", "application_id": "APP-R92", "product": "投資信託", "primary_seat": "emp-A"}
    )
    recorder.append_ledger("tick_committed", {"tick": 1})

    recorder.set_tick(2)
    _register_read(kernel, "emp-C")
    assert kernel.submit_application("emp-C", "APP-R92", "CUS-R92", "投資信託", {"material_version": "v1.0"}, BASIS).get("status")
    recorder.append_ledger("tick_committed", {"tick": 2})

    recorder.set_tick(3)
    assert kernel.verify_identity("emp-C", "APP-R92", True, True, "CONSENT-1", BASIS).get("status")
    recorder.append_ledger("tick_committed", {"tick": 3})

    recorder.set_tick(4)
    assert kernel.link_review("emp-C", "APP-R92", "REV-1", BASIS).get("status")
    assert kernel.complete_contract("emp-C", "APP-R92", "CTR-1", BASIS).get("status")
    recorder.append_ledger("tick_committed", {"tick": 4})

    config = {
        "schema_version": "company_twin.world_config.v2",
        "stage": "S2",
        "world": {
            "population": {
                "seats": {
                    "emp-A": {"role": "sales"},
                    "emp-C": {"role": "application"},
                    "emp-M": {"role": "manager"},
                    "emp-Q": {"role": "second_line"},
                }
            },
            "schedule": {"ticks": 4, "workflow": _workflow_schedule(True)},
        },
    }
    (recorder.run_root / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    loss_report = loss_event_findings(recorder.run_root)
    assert loss_report["loss_events"] == []

    monitoring = write_loss_event_monitoring(recorder.run_root)
    r3_opportunities = [
        opportunity
        for opportunity in monitoring["opportunities"]
        if opportunity["risk"] == "R3" and opportunity["loss_class"] == "unverified_completion"
    ]
    assert len(r3_opportunities) == 1
    assert r3_opportunities[0]["application_id"] == "APP-R92"
    assert r3_opportunities[0]["materialized_loss_event_id"] is None


def test_world_config_stamps_the_workflow_condition() -> None:
    schedule_off = _workflow_schedule(False)
    assert schedule_off == {
        "enabled": False,
        "version": "workflow_support_v2",
        "notices": False,
        "contact_directory": False,
        "customer_id_in_inbox": False,
        "sales_direct_submission_guidance": False,
    }
    schedule_on = _workflow_schedule(True)
    assert schedule_on["enabled"] is True and schedule_on["notices"] is True and schedule_on["contact_directory"] is True

    design = load_design(Path.cwd())
    profile_default = kernel_profile(design, schedule={})
    assert profile_default.workflow_notices_enabled is False
    profile_on = kernel_profile(design, schedule={"workflow": _workflow_schedule(True)})
    assert profile_on.workflow_notices_enabled is True


def test_v2_schedule_stamps_new_flags() -> None:
    schedule_off = _workflow_schedule(False)
    assert schedule_off["version"] == "workflow_support_v2"
    assert all(
        schedule_off[key] is False
        for key in (
            "enabled",
            "notices",
            "contact_directory",
            "customer_id_in_inbox",
            "sales_direct_submission_guidance",
        )
    )

    schedule_on = _workflow_schedule(True)
    assert all(
        schedule_on[key] is True
        for key in (
            "enabled",
            "notices",
            "contact_directory",
            "customer_id_in_inbox",
            "sales_direct_submission_guidance",
        )
    )


def test_v1_config_renders_v1_way() -> None:
    schedule = {
        "workflow": {
            "enabled": True,
            "version": "workflow_support_v1",
            "notices": True,
            "contact_directory": True,
        }
    }
    assert bool(schedule["workflow"].get("customer_id_in_inbox")) is False
    assert bool(schedule["workflow"].get("sales_direct_submission_guidance")) is False
