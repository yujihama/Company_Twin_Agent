from pathlib import Path

from company_twin.kernel import KernelProfile, WorldKernel
from company_twin.oracles import run_l0_triage, signature_for, write_triage
from company_twin.recorder import RunRecorder


def _register_read(recorder: RunRecorder, *, seat_id: str = "emp-A", doc_id: str = "DFH-SAL-020", version: str = "1.1", text: str = "申込には証跡と確認を残す") -> str:
    handle = f"read:{doc_id}:v{version}"
    recorder.record_attempt(
        seat_id=seat_id,
        tool="read_document",
        args={"doc_id": doc_id},
        success=True,
        result={"version": version, "citation_handle": handle, "text": text},
    )
    return handle


def _basis(handle: str, *, doc_id: str = "DFH-SAL-020", version: str = "1.1", decision: str = "submit") -> dict:
    return {
        "retrieved": [{"doc_id": doc_id, "version": version, "citation_handle": handle}],
        "construal": "申込には証跡と確認を残す",
        "decision": decision,
        "evidence_plan": "証跡を保存する",
    }


def test_kernel_denies_missing_completion_evidence(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, "test-run")
    kernel = WorldKernel(recorder, KernelProfile(knobs={"K-completion-gate": True}))
    handle = _register_read(recorder)

    result = kernel.submit_application(
        "emp-A",
        "APP-1",
        "CUS-1",
        "投信",
        {"material_version": "v1.1"},
        _basis(handle),
    )

    assert result["success"] is False
    findings = run_l0_triage(tmp_path)
    assert findings
    assert findings[0].finding_type == "hard_constraint_denial"


def test_kernel_denies_missing_required_fields(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, "test-run")
    kernel = WorldKernel(recorder)
    handle = _register_read(recorder)

    result = kernel.submit_application(
        "emp-A",
        "",
        "CUS-1",
        "投信",
        {},
        _basis(handle),
    )

    assert result["success"] is False
    assert "missing required fields" in result["denied_reason"]


def test_kernel_checksheet_gate_requires_approval(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, "test-run")
    kernel = WorldKernel(recorder, KernelProfile(knobs={"K-checksheet-gate": True}))
    handle = _register_read(recorder)

    result = kernel.submit_application(
        "emp-A",
        "APP-2",
        "CUS-2",
        "投信",
        {"material_version": "v1.1", "checksheet_status": "requires_approval"},
        _basis(handle),
    )

    assert result["success"] is False
    assert "K-checksheet-gate" in result["denied_reason"]


def test_kernel_qualification_gate_blocks_unqualified_product(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, "test-run")
    kernel = WorldKernel(recorder, KernelProfile(knobs={"K-qualification-gate": True}, seat_qualifications={"emp-B": {"保険"}}))
    handle = _register_read(recorder, seat_id="emp-B")

    result = kernel.submit_application(
        "emp-B",
        "APP-3",
        "CUS-3",
        "投資信託",
        {"material_version": "v1.1"},
        _basis(handle),
    )

    assert result["success"] is False
    assert "K-qualification-gate" in result["denied_reason"]


def test_signature_masks_ids_and_uses_role() -> None:
    sig1 = signature_for(finding_type="evidence_gap", anchor_id="APP-123", seat_id="emp-A", phase="application", artifact_skeleton="missing ID 999")
    sig2 = signature_for(finding_type="evidence_gap", anchor_id="APP-999", seat_id="emp-B", phase="application", artifact_skeleton="missing ID 111")

    assert sig1 == sig2


def test_kernel_rejects_world_basis_span_id_leak(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, "span-leak")
    kernel = WorldKernel(recorder)
    handle = _register_read(recorder)

    result = kernel.record_customer_contact(
        "emp-A",
        "CUS-1",
        "phone",
        "説明",
        {
            "retrieved": [{"doc_id": "DFH-SAL-020", "version": "1.1", "citation_handle": handle, "span_id": "AMB-02"}],
            "construal": "申込には証跡と確認を残す",
            "decision": "contact",
        },
    )

    assert result["success"] is False
    assert "span_id is not world-visible" in result["denied_reason"]


def test_kernel_grounding_uses_citation_handle_not_seeded_span(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, "handle-grounding")
    recorder.set_tick(1)
    kernel = WorldKernel(recorder)
    with recorder.origin("agent"):
        handle = _register_read(recorder, text="顧客への説明では申込前に証跡と確認を残す")

        result = kernel.record_customer_contact("emp-A", "CUS-1", "phone", "説明", _basis(handle, decision="顧客へ説明し証跡を残す"))

    assert result["event_id"]
    basis_rows = (tmp_path / "basis_records.jsonl").read_text(encoding="utf-8")
    assert '"g1_citation_handle_exists": true' in basis_rows
    assert '"g1_span_exists": true' in basis_rows
    assert '"g2_prior_read": true' in basis_rows
    assert '"g3_machine_heuristic": "supported"' in basis_rows
    triage = write_triage(tmp_path)
    assert triage["metrics"]["grounding_g1_citation_handle_exists_rate"] == 1.0
    assert triage["metrics"]["grounding_g2_prior_read_rate"] == 1.0
