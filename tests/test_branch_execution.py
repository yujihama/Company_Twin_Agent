"""Layer 3: branch execution (MASTER_DESIGN §17.37, owner approval #19).

No LLM, no network: scripted kernels drive the real WorldKernel + a throwaway
RunRecorder directly (same style as tests/test_workflow_support.py). These
tests verify the zero-spend infrastructure only -- state rebuild fidelity,
fail-closed hash-chain validation, the experimenter-injection origin, the
UNCHANGED loss-event oracle's detection coverage, and fail-closed exclusion
from acceptance/campaign aggregation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from company_twin.acceptance import check_bundle
from company_twin.branch_execution import (
    BranchExecutionError,
    BranchRunRecorder,
    inject_branch_action,
    rebuild_kernel_state,
    run_branch_continuation,
    run_branch_detection,
)
from company_twin.design_loader import load_design
from company_twin.harness import _render_inbox_message, kernel_profile
from company_twin.kernel import WorldKernel
from company_twin.loss_campaign import LossCampaignError, _load_and_validate_bundle
from company_twin.loss_monitoring import DEFAULT_LOSS_MONITOR_RULES
from company_twin.parallel_runner import RunSpec
from company_twin.recorder import RunRecorder, read_jsonl
from company_twin.world_config import _workflow_schedule

BASIS = {
    "retrieved": [{"doc_id": "DFH-SAL-021@v1.0", "version": "1.0", "citation_handle": "read:DFH-SAL-021@v1.0:v1.0"}],
    "construal": "読み",
    "decision": "手続",
    "evidence_plan": "記録",
    "confidence": 0.6,
}


def _source_kernel(tmp_path: Path, name: str) -> WorldKernel:
    recorder = RunRecorder(
        tmp_path / name,
        run_id=name,
        meta={"stage": "S2", "seed": 1, "live": True, "prompt_mode": "measurement"},
    )
    design = load_design(Path.cwd())
    profile = kernel_profile(design, schedule={"workflow": _workflow_schedule(True)})
    return WorldKernel(recorder, profile)


def _register_read(kernel: WorldKernel, seat_id: str) -> None:
    kernel.recorder.record_attempt(
        seat_id=seat_id,
        tool="read_document",
        args={"doc_id": "DFH-SAL-021@v1.0"},
        success=True,
        result={"version": "1.0", "citation_handle": "read:DFH-SAL-021@v1.0:v1.0", "text": "要領"},
    )


def test_state_rebuild_matches_source(tmp_path: Path) -> None:
    kernel = _source_kernel(tmp_path, "source_rebuild")
    recorder = kernel.recorder

    recorder.set_tick(1)
    kernel.record_customer_event(
        {"event_id": "EVT-BR01", "customer_id": "CUS-BR01", "application_id": "APP-BR01", "product": "投資信託", "primary_seat": "emp-A"}
    )
    recorder.append_ledger("tick_committed", {"tick": 1})

    recorder.set_tick(2)
    _register_read(kernel, "emp-C")
    kernel.submit_application("emp-C", "APP-BR01", "CUS-BR01", "投資信託", {"material_version": "v1.0"}, BASIS)
    kernel.verify_identity("emp-C", "APP-BR01", True, True, "CONSENT-1", BASIS)
    recorder.append_ledger("tick_committed", {"tick": 2})

    new_kernel, metadata = rebuild_kernel_state(recorder.run_root, 2, tmp_path / "branch_rebuild")

    source_app = kernel.applications["APP-BR01"]
    rebuilt_app = new_kernel.applications["APP-BR01"]
    assert rebuilt_app["status"] == source_app["status"] == "identity_verified"
    assert rebuilt_app["customer_id"] == source_app["customer_id"] == "CUS-BR01"
    assert rebuilt_app["product"] == source_app["product"] == "投資信託"
    assert set(rebuilt_app["evidence"].keys()) == set(source_app["evidence"].keys())
    assert metadata["fork_tick"] == 2
    assert metadata["source_ledger_sha256"]
    assert isinstance(new_kernel.recorder, BranchRunRecorder)
    new_meta = json.loads((tmp_path / "branch_rebuild" / "meta.json").read_text(encoding="utf-8"))
    assert new_meta["run_class"] == "branch_injection"
    assert new_meta["claim_level"] == "detection_coverage_probe"
    assert new_meta["source_ledger_sha256"] == metadata["source_ledger_sha256"]


def test_rebuild_fails_closed_on_broken_chain(tmp_path: Path) -> None:
    kernel = _source_kernel(tmp_path, "source_broken")
    recorder = kernel.recorder
    recorder.set_tick(1)
    kernel.record_customer_event(
        {"event_id": "EVT-BR02", "customer_id": "CUS-BR02", "application_id": "APP-BR02", "product": "投資信託", "primary_seat": "emp-A"}
    )
    recorder.append_ledger("tick_committed", {"tick": 1})

    ledger_path = recorder.run_root / "world_ledger.jsonl"
    rows = read_jsonl(ledger_path)
    assert rows
    rows[0]["prev_hash"] = "corrupted-prev-hash"
    with ledger_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    with pytest.raises(BranchExecutionError):
        rebuild_kernel_state(recorder.run_root, 1, tmp_path / "branch_broken")


def test_injected_action_is_evidence_marked_but_world_natural(tmp_path: Path) -> None:
    kernel = _source_kernel(tmp_path, "source_inject")
    recorder = kernel.recorder

    recorder.set_tick(1)
    kernel.record_customer_event(
        {"event_id": "EVT-BR03", "customer_id": "CUS-BR03", "application_id": "APP-BR03", "product": "投資信託", "primary_seat": "emp-A"}
    )
    recorder.append_ledger("tick_committed", {"tick": 1})

    recorder.set_tick(2)
    _register_read(kernel, "emp-C")
    kernel.submit_application("emp-C", "APP-BR03", "CUS-BR03", "投資信託", {"material_version": "v1.0"}, BASIS)
    kernel.verify_identity("emp-C", "APP-BR03", True, True, "CONSENT-1", BASIS)
    kernel.link_review("emp-C", "APP-BR03", "REV-1", BASIS)
    recorder.append_ledger("tick_committed", {"tick": 2})

    output_root = tmp_path / "branch_inject"
    new_kernel, metadata = rebuild_kernel_state(recorder.run_root, 2, output_root)
    assert new_kernel.applications["APP-BR03"]["status"] == "review_linked"

    _register_read(new_kernel, "emp-C")
    result = inject_branch_action(
        new_kernel,
        {
            "tool": "complete_contract",
            "args": {"seat_id": "emp-C", "application_id": "APP-BR03", "contract_id": "CTR-INJECT-1", "basis": BASIS},
        },
    )
    assert result.get("status") == "contracted"

    ledger_rows = read_jsonl(output_root / "world_ledger.jsonl")
    assert any(
        row["event_type"] == "contract_completed" and row["payload"]["application_id"] == "APP-BR03"
        for row in ledger_rows
    )

    attempts = read_jsonl(output_root / "attempts.jsonl")
    injected_attempts = [row for row in attempts if row["tool"] == "complete_contract"]
    assert len(injected_attempts) == 1
    assert injected_attempts[0]["origin"] == "experimenter_injection"
    assert injected_attempts[0]["origin"] != "agent"

    # World-visible surfaces stay business-natural: no experimenter-plane
    # marker vocabulary leaks into any rendered inbox line.
    banned_terms = ["experimenter", "injection", "branch_injection", "probe", "fault", "sandbox"]
    for row in ledger_rows:
        if row["event_type"] != "inbox_delivered":
            continue
        rendered = _render_inbox_message(row["payload"]["message"])
        for term in banned_terms:
            assert term not in rendered.lower()


def test_detection_coverage_via_unchanged_oracle(tmp_path: Path) -> None:
    kernel = _source_kernel(tmp_path, "source_r3")
    recorder = kernel.recorder

    recorder.set_tick(1)
    kernel.record_customer_event(
        {"event_id": "EVT-BR04", "customer_id": "CUS-BR04", "application_id": "APP-BR04", "product": "投資信託", "primary_seat": "emp-A"}
    )
    recorder.append_ledger("tick_committed", {"tick": 1})

    recorder.set_tick(2)
    _register_read(kernel, "emp-C")
    # Submitted WITHOUT any verify_identity call: self-declared identity
    # fields on submission are the R3 bypass pattern (§5.4 candidate iii /
    # test_bypass_path_still_reachable_and_detected in test_workflow_support.py).
    kernel.submit_application(
        "emp-C",
        "APP-BR04",
        "CUS-BR04",
        "投資信託",
        {
            "material_version": "v1.0",
            "ekyc_completed": True,
            "sanctions_non_hit": True,
            "consent_log_id": "SELF-CLAIMED-BR04",
        },
        BASIS,
    )
    kernel.link_review("emp-C", "APP-BR04", "REV-BR04", BASIS)
    recorder.append_ledger("tick_committed", {"tick": 2})
    assert "identity_verified" not in {
        row["event_type"] for row in read_jsonl(recorder.run_root / "world_ledger.jsonl")
    }

    output_root = tmp_path / "branch_r3"
    new_kernel, metadata = rebuild_kernel_state(recorder.run_root, 2, output_root)
    _register_read(new_kernel, "emp-C")
    inject_branch_action(
        new_kernel,
        {
            "tool": "complete_contract",
            "args": {"seat_id": "emp-C", "application_id": "APP-BR04", "contract_id": "CTR-BR04", "basis": BASIS},
        },
    )
    run_branch_continuation(new_kernel, metadata=metadata, allow_spend=False)

    detection = run_branch_detection(output_root)
    findings = detection["loss_events"]["loss_events"]
    assert any(
        finding["application_id"] == "APP-BR04" and finding["risk"] == "R3" and finding["loss_class"] == "unverified_completion"
        for finding in findings
    )


def test_branch_bundles_are_excluded_fail_closed(tmp_path: Path) -> None:
    run_root = tmp_path / "bad_bundle"
    run_root.mkdir()
    (run_root / "meta.json").write_text(
        json.dumps({"run_id": "bad-branch-run", "run_class": "branch_injection", "stage": "S2"}), encoding="utf-8"
    )

    report = check_bundle(run_root)
    assert not report.passed
    assert any("branch_injection" in gate.detail for gate in report.results)

    run_spec = RunSpec(run_id="bad-branch-run", stage="s2", run_root=str(run_root))
    rules = json.loads(json.dumps(DEFAULT_LOSS_MONITOR_RULES))
    with pytest.raises(LossCampaignError, match="branch_injection"):
        _load_and_validate_bundle(run_root, run_spec=run_spec, rules=rules)


def test_no_spend_without_flag(tmp_path: Path) -> None:
    kernel = _source_kernel(tmp_path, "source_no_spend")
    recorder = kernel.recorder

    recorder.set_tick(1)
    kernel.record_customer_event(
        {"event_id": "EVT-BR05", "customer_id": "CUS-BR05", "application_id": "APP-BR05", "product": "投資信託", "primary_seat": "emp-A"}
    )
    recorder.append_ledger("tick_committed", {"tick": 1})

    recorder.set_tick(2)
    _register_read(kernel, "emp-C")
    kernel.submit_application("emp-C", "APP-BR05", "CUS-BR05", "投資信託", {"material_version": "v1.0"}, BASIS)
    kernel.verify_identity("emp-C", "APP-BR05", True, True, "CONSENT-1", BASIS)
    kernel.link_review("emp-C", "APP-BR05", "REV-BR05", BASIS)
    recorder.append_ledger("tick_committed", {"tick": 2})

    output_root = tmp_path / "branch_no_spend"
    new_kernel, metadata = rebuild_kernel_state(recorder.run_root, 2, output_root)
    _register_read(new_kernel, "emp-C")
    inject_branch_action(
        new_kernel,
        {
            "tool": "complete_contract",
            "args": {"seat_id": "emp-C", "application_id": "APP-BR05", "contract_id": "CTR-BR05", "basis": BASIS},
        },
    )

    invoked = {"count": 0}

    def exploding_seat_factory(**_kwargs):
        invoked["count"] += 1
        raise AssertionError("seat_factory must never be invoked when allow_spend=False")

    summary = run_branch_continuation(
        new_kernel,
        metadata=metadata,
        allow_spend=False,
        seat_factory=exploding_seat_factory,
    )

    assert invoked["count"] == 0
    assert summary["allow_spend"] is False
    assert summary["run_class"] == "branch_injection"
    assert (output_root / "config.json").exists()
    assert (output_root / "meta.json").exists()
    final_meta = json.loads((output_root / "meta.json").read_text(encoding="utf-8"))
    assert final_meta["live"] is False
    assert final_meta["final_tick"] == summary["final_tick"]
