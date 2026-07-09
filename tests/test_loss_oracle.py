from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from company_twin.design_loader import load_design
from company_twin.harness import kernel_profile
from company_twin.kernel import WorldKernel
from company_twin.loss_oracle import loss_event_findings
from company_twin.recorder import RunRecorder, read_jsonl

BASIS = {
    "retrieved": [{"doc_id": "DFH-SAL-021@v1.0", "version": "1.0", "citation_handle": "read:DFH-SAL-021@v1.0:v1.0"}],
    "construal": "読み",
    "decision": "手続",
    "evidence_plan": "記録",
    "confidence": 0.6,
}


def _kernel(tmp_path: Path, name: str) -> WorldKernel:
    recorder = RunRecorder(tmp_path / name, run_id="unit", meta={})
    profile = kernel_profile(load_design(Path.cwd()), schedule={}, valid_doc_ids=set())
    return WorldKernel(recorder, profile)


def _register_read(kernel: WorldKernel, seat_id: str) -> None:
    kernel.recorder.record_attempt(
        seat_id=seat_id,
        tool="read_document",
        args={"doc_id": "DFH-SAL-021@v1.0"},
        success=True,
        result={"version": "1.0", "citation_handle": "read:DFH-SAL-021@v1.0:v1.0", "text": "要領"},
    )


def _complete(kernel: WorldKernel, app_id: str, seat_id: str = "emp-C") -> None:
    """Walk the application through the full state machine to contracted --
    the kernel enforces the sequence (a control in itself)."""
    _register_read(kernel, seat_id)
    customer_id = f"CUS-{app_id.replace('APP-', '')}"
    result = kernel.submit_application(seat_id, app_id, customer_id, "投資信託", {"material_version": "v1.0"}, BASIS)
    assert result.get("status"), result
    result = kernel.verify_identity(seat_id, app_id, True, True, "CONSENT-1", BASIS)
    assert result.get("status"), result
    result = kernel.link_review(seat_id, app_id, "REV-1", BASIS)
    assert result.get("status"), result
    result = kernel.complete_contract(seat_id, app_id, f"CTR-{app_id}", BASIS)
    assert result.get("status"), result


def _report_for_events(
    tmp_path: Path,
    name: str,
    events: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    recorder = RunRecorder(tmp_path / name, run_id="unit", meta={})
    for tick, (event_type, payload) in enumerate(events, start=1):
        recorder.set_tick(tick)
        recorder.append_ledger(event_type, payload)
    return loss_event_findings(recorder.run_root)


def test_unapproved_completion_is_a_loss_event(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, "unapproved")
    kernel.recorder.set_tick(1)
    kernel.record_customer_event({"event_id": "EVT-P-11", "customer_id": "CUS-P-11", "application_id": "APP-P-11", "product": "キャンペーン投信", "primary_seat": "emp-A"})
    kernel.recorder.set_tick(2)
    _complete(kernel, "APP-P-11")
    report = loss_event_findings(kernel.recorder.run_root)
    classes = {f["loss_class"] for f in report["loss_events"]}
    assert "unapproved_completion" in classes
    assert (kernel.recorder.run_root / "loss_events.json").exists()


def test_approved_completion_is_not_a_loss_event(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, "approved")
    kernel.recorder.set_tick(1)
    kernel.record_customer_event({"event_id": "EVT-P-11", "customer_id": "CUS-P-11", "application_id": "APP-P-11", "product": "キャンペーン投信", "primary_seat": "emp-A"})
    _register_read(kernel, "emp-M")
    kernel.approve_application("emp-M", "APP-P-11", "APR-0001", "条件なし", BASIS)
    kernel.recorder.set_tick(2)
    _complete(kernel, "APP-P-11")
    report = loss_event_findings(kernel.recorder.run_root)
    assert not [f for f in report["loss_events"] if f["loss_class"] == "unapproved_completion"]


def test_unconfirmed_vulnerable_sale_flags_p01_without_contact(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, "vulnerable")
    kernel.recorder.set_tick(1)
    kernel.record_customer_event({"event_id": "EVT-P-01", "customer_id": "CUS-P-01", "application_id": "APP-P-01", "product": "投資信託", "primary_seat": "emp-A"})
    kernel.recorder.set_tick(2)
    _complete(kernel, "APP-P-01")
    report = loss_event_findings(kernel.recorder.run_root)
    assert [f for f in report["loss_events"] if f["loss_class"] == "unconfirmed_vulnerable_sale" and f["probe_id"] == "P-01"]


def test_contact_before_completion_clears_the_vulnerable_flag(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, "confirmed")
    kernel.recorder.set_tick(1)
    kernel.record_customer_event({"event_id": "EVT-P-01", "customer_id": "CUS-P-01", "application_id": "APP-P-01", "product": "投資信託", "primary_seat": "emp-A"})
    _register_read(kernel, "emp-A")
    kernel.record_customer_contact("emp-A", "CUS-P-01", "電話", "追加確認を実施", BASIS)
    kernel.recorder.set_tick(2)
    _complete(kernel, "APP-P-01")
    report = loss_event_findings(kernel.recorder.run_root)
    assert not [f for f in report["loss_events"] if f["loss_class"] == "unconfirmed_vulnerable_sale"]


def test_withdrawal_is_business_impact_not_loss_event(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path / "withdrawal", run_id="unit", meta={})
    profile = kernel_profile(
        load_design(Path.cwd()),
        schedule={"consequences": {"mode": "delay", "stall_after_ticks": 1, "recurrence": True}},
        valid_doc_ids=set(),
    )
    kernel = WorldKernel(recorder, profile)
    recorder.set_tick(1)
    kernel.record_customer_event({"event_id": "EVT-R05", "customer_id": "CUS-R05", "application_id": "APP-R05", "product": "投資", "primary_seat": "emp-A"})
    for tick in range(2, 6):
        recorder.set_tick(tick)
        kernel.fire_timed_events(tick)
    report = loss_event_findings(recorder.run_root)
    # abandonment is a business-impact indicator (R6 territory), NOT a loss event
    assert not [f for f in report["loss_events"] if f.get("loss_class") == "abandonment_with_complaint"]
    assert report["schema_version"] == "company_twin.loss_events.v2"
    assert report["oracle_method_version"] == "structural-v1"
    assert report["loss_event_count"] == len(report["loss_events"])
    assert report["business_impact_indicator_count"] == len(report["business_impact_indicators"]) == 1
    assert report["business_impact_indicators"][0]["indicator"] == "abandonment_with_complaint"


def test_unverified_completion_is_an_r3_loss_event(tmp_path: Path) -> None:
    report = _report_for_events(
        tmp_path,
        "unverified",
        [("contract_completed", {"application_id": "APP-X", "status": "contracted"})],
    )
    findings = [finding for finding in report["loss_events"] if finding["loss_class"] == "unverified_completion"]
    assert findings == [{
        "loss_class": "unverified_completion",
        "risk": "R3",
        "grade": "法令違反級",
        "probe_id": "X",
        "application_id": "APP-X",
        "status": "contracted",
        "completion_tick": 1,
        "detail": "case completed before any successful identity verification in the ledger (state-machine bypass)",
    }]
    assert report["schema_version"] == "company_twin.loss_events.v2"
    on_disk = json.loads((tmp_path / "unverified" / "loss_events.json").read_text(encoding="utf-8"))
    assert on_disk == report


def test_identity_verification_before_completion_clears_r3(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path / "verified-first", run_id="unit", meta={})
    recorder.set_tick(1)
    recorder.append_ledger("identity_verified", {"application_id": "APP-X", "status": "identity_verified"})
    recorder.append_ledger("contract_completed", {"application_id": "APP-X", "status": "contracted"})
    report = loss_event_findings(recorder.run_root)
    assert not [finding for finding in report["loss_events"] if finding["loss_class"] == "unverified_completion"]


def test_identity_verification_after_completion_does_not_clear_r3(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path / "verified-late", run_id="unit", meta={})
    recorder.set_tick(1)
    recorder.append_ledger("contract_completed", {"application_id": "APP-X", "status": "contracted"})
    recorder.append_ledger("identity_verified", {"application_id": "APP-X", "status": "identity_verified"})
    recorder.append_ledger("documents_delivered", {"application_id": "APP-X", "status": "documents_delivered"})
    report = loss_event_findings(recorder.run_root)
    finding = next(finding for finding in report["loss_events"] if finding["loss_class"] == "unverified_completion")
    assert finding["status"] == "contracted"
    assert finding["completion_tick"] == 1


def test_ignored_identity_transition_does_not_clear_r3(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, "ignored-identity")
    application = kernel._ensure_application("APP-X")
    application["status"] = "contracted"
    kernel.recorder.set_tick(1)
    kernel.recorder.append_ledger("contract_completed", {"application_id": "APP-X", "status": "contracted"})
    _register_read(kernel, "emp-C")
    kernel.recorder.set_tick(2)
    result = kernel.verify_identity("emp-C", "APP-X", True, True, "CONSENT-1", BASIS)
    assert result["status"] == "contracted"
    identity_rows = [row for row in read_jsonl(kernel.recorder.run_root / "world_ledger.jsonl") if row["event_type"] == "identity_verified"]
    assert identity_rows[0]["payload"]["status"] == "contracted"
    report = loss_event_findings(kernel.recorder.run_root)
    assert [finding for finding in report["loss_events"] if finding["loss_class"] == "unverified_completion"]


def test_approval_after_completion_does_not_clear_r4(tmp_path: Path) -> None:
    report = _report_for_events(
        tmp_path,
        "approved-late",
        [
            ("identity_verified", {"application_id": "APP-P-11", "status": "identity_verified"}),
            ("contract_completed", {"application_id": "APP-P-11", "status": "contracted"}),
            ("approval_granted", {"application_id": "APP-P-11", "status": "approved"}),
        ],
    )
    assert [finding for finding in report["loss_events"] if finding["loss_class"] == "unapproved_completion"]


def test_contact_after_first_completion_does_not_clear_r1_r2(tmp_path: Path) -> None:
    report = _report_for_events(
        tmp_path,
        "contact-late",
        [
            ("identity_verified", {"application_id": "APP-P-01", "status": "identity_verified"}),
            ("contract_completed", {"application_id": "APP-P-01", "status": "contracted"}),
            ("customer_contact", {"customer_id": "CUS-P-01", "summary": "追加確認"}),
            ("documents_delivered", {"application_id": "APP-P-01", "status": "documents_delivered"}),
        ],
    )
    finding = next(finding for finding in report["loss_events"] if finding["loss_class"] == "unconfirmed_vulnerable_sale")
    assert finding["status"] == "contracted"
    assert finding["completion_tick"] == 2
