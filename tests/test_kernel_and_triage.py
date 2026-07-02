from pathlib import Path

from company_twin.kernel import KernelProfile, WorldKernel
from company_twin.oracles import run_l0_triage, signature_for
from company_twin.recorder import RunRecorder


def test_kernel_denies_missing_completion_evidence(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, "test-run")
    kernel = WorldKernel(recorder, KernelProfile(knobs={"K-completion-gate": True}))

    result = kernel.submit_application(
        "emp-A",
        "APP-1",
        "CUS-1",
        "投信",
        {"material_version": "v1.1"},
        {"retrieved": [{"doc_id": "DFH-SAL-020"}], "construal": "read", "decision": "submit"},
    )

    assert result["success"] is False
    findings = run_l0_triage(tmp_path)
    assert findings
    assert findings[0].finding_type == "hard_constraint_denial"


def test_kernel_denies_missing_required_fields(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, "test-run")
    kernel = WorldKernel(recorder)

    result = kernel.submit_application(
        "emp-A",
        "",
        "CUS-1",
        "投信",
        {},
        {"retrieved": [{"doc_id": "DFH-SAL-020"}], "construal": "read", "decision": "submit"},
    )

    assert result["success"] is False
    assert "missing required fields" in result["denied_reason"]


def test_kernel_checksheet_gate_requires_approval(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, "test-run")
    kernel = WorldKernel(recorder, KernelProfile(knobs={"K-checksheet-gate": True}))

    result = kernel.submit_application(
        "emp-A",
        "APP-2",
        "CUS-2",
        "投信",
        {"material_version": "v1.1", "checksheet_status": "requires_approval"},
        {"retrieved": [{"doc_id": "DFH-SAL-020"}], "construal": "read", "decision": "submit"},
    )

    assert result["success"] is False
    assert "K-checksheet-gate" in result["denied_reason"]


def test_kernel_qualification_gate_blocks_unqualified_product(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, "test-run")
    kernel = WorldKernel(recorder, KernelProfile(knobs={"K-qualification-gate": True}, seat_qualifications={"emp-B": {"保険"}}))

    result = kernel.submit_application(
        "emp-B",
        "APP-3",
        "CUS-3",
        "投資信託",
        {"material_version": "v1.1"},
        {"retrieved": [{"doc_id": "DFH-SAL-020"}], "construal": "read", "decision": "submit"},
    )

    assert result["success"] is False
    assert "K-qualification-gate" in result["denied_reason"]


def test_signature_masks_ids_and_uses_role() -> None:
    sig1 = signature_for(finding_type="evidence_gap", anchor_id="APP-123", seat_id="emp-A", phase="application", artifact_skeleton="missing ID 999")
    sig2 = signature_for(finding_type="evidence_gap", anchor_id="APP-999", seat_id="emp-B", phase="application", artifact_skeleton="missing ID 111")

    assert sig1 == sig2
