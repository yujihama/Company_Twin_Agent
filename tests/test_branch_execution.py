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


def test_branch_restores_the_work_each_seat_still_had_waiting(tmp_path: Path) -> None:
    """A fork must not hand the organization an empty desk.

    The seats only take a turn when something is waiting for them, so a
    branch that starts with every queue empty freezes the whole company and
    turns "nobody noticed the deviation" into an artifact of the fork.
    """
    kernel = _source_kernel(tmp_path, "source_pending")
    recorder = kernel.recorder

    recorder.set_tick(1)
    kernel.record_customer_event(
        {"event_id": "EVT-BR06", "customer_id": "CUS-BR06", "application_id": "APP-BR06", "product": "投資信託", "primary_seat": "emp-A"}
    )
    # emp-A handles what it was given; emp-M is handed something and never
    # gets to it, so that item is still outstanding at the fork.
    kernel.enqueue_inbox("emp-A", {"kind": "chat", "tick": 1, "from": "emp-M", "channel": "chat", "body": "案件をお願いします"})
    handed_to_a = len(kernel.pop_inbox("emp-A"))
    assert handed_to_a == 1
    recorder.append_ledger("agent_response", {"seat_id": "emp-A", "response": "対応しました", "message_count": handed_to_a})
    kernel.enqueue_inbox("emp-M", {"kind": "chat", "tick": 1, "from": "emp-A", "channel": "chat", "body": "確認をお願いします"})
    recorder.append_ledger("tick_committed", {"tick": 1})

    output_root = tmp_path / "branch_pending"
    new_kernel, metadata = rebuild_kernel_state(recorder.run_root, 1, output_root)

    assert new_kernel.inbox.get("emp-M"), "the manager's outstanding item was dropped by the fork"
    assert new_kernel.inbox["emp-M"][0]["body"] == "確認をお願いします"
    assert not new_kernel.inbox.get("emp-A"), "emp-A had already finished its turn before the fork"
    assert metadata["pending_work"]["source"] == "reconstructed_from_ledger"
    assert metadata["pending_work"]["per_seat"] == {"emp-M": 1}


def test_branch_bundle_saves_its_own_outstanding_work_for_the_next_fork(tmp_path: Path) -> None:
    """Second-level branching reads the exact state the first level ended
    with, rather than re-deriving it -- and the two must agree."""
    kernel = _source_kernel(tmp_path, "source_snapshot")
    recorder = kernel.recorder
    recorder.set_tick(1)
    kernel.record_customer_event(
        {"event_id": "EVT-BR07", "customer_id": "CUS-BR07", "application_id": "APP-BR07", "product": "投資信託", "primary_seat": "emp-A"}
    )
    kernel.enqueue_inbox("emp-M", {"kind": "chat", "tick": 1, "from": "emp-A", "channel": "chat", "body": "承認をお願いします"})
    kernel.enqueue_inbox("emp-A", {"kind": "chat", "tick": 1, "from": "emp-M", "channel": "chat", "body": "案件をお願いします"})
    recorder.append_ledger("tick_committed", {"tick": 1})

    first = tmp_path / "branch_level1"
    branch_kernel, metadata = rebuild_kernel_state(recorder.run_root, 1, first)
    run_branch_continuation(branch_kernel, metadata=metadata, allow_spend=False)

    snapshot = json.loads((first / "pending_work.json").read_text(encoding="utf-8"))
    assert snapshot["item_count"] >= 2  # the customer's message and the chat
    assert snapshot["ordinal"] == len(read_jsonl(first / "world_ledger.jsonl"))

    second = tmp_path / "branch_level2"
    next_kernel, next_metadata = rebuild_kernel_state(
        first, 0, second, up_to_ordinal=snapshot["ordinal"]
    )
    assert next_metadata["pending_work"]["source"] == "branch_snapshot"
    # the independent ledger reconstruction reaches the same answer
    assert next_metadata["pending_work"]["snapshot_agrees_with_ledger"] is True
    assert next_kernel.inbox.get("emp-M")
    assert next_kernel.inbox.get("emp-A")


def test_branch_continuation_hosts_the_scheduled_customers(tmp_path: Path) -> None:
    """After the fork, customers keep living: visits still ahead of the fork
    happen on their scheduled day, and a contacted customer answers on the
    next tick. Without this the branch world starves once the restored
    pending work is handled."""
    from company_twin.corpus import Corpus
    from company_twin.deck import CustomerEvent
    from tests.conftest import FakeCustomerLLM, fake_seat_factory

    kernel = _source_kernel(tmp_path, "source_customers")
    recorder = kernel.recorder
    recorder.set_tick(1)
    recorder.append_ledger("tick_committed", {"tick": 1})
    recorder.set_tick(2)
    recorder.append_ledger("tick_committed", {"tick": 2})

    event = CustomerEvent(
        event_id="EVT-T1",
        probe_id="",
        customer_id="CUS-T1",
        application_id="APP-T1",
        product="投資信託",
        trigger_tick=3,
        deadline_tick=8,
        primary_seat="emp-A",
        participant_seats=("emp-A", "emp-C"),
        required_doc_ids=("DFH-SAL-021",),
        span_ids=(),
        world_visible="投資信託の申込を進めたい。",
        latent_truth="unit-test customer",
        routine=True,
    )
    (recorder.run_root / "config.json").write_text(
        json.dumps(
            {
                "world": {
                    "deck": {"events": [event.to_dict()]},
                    "schedule": {"ticks": 6, "workflow": {}},
                    "population": {},
                },
                "model": {"customer": "fake:unit"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    output_root = tmp_path / "branch_customers"
    new_kernel, metadata = rebuild_kernel_state(recorder.run_root, 2, output_root)
    assert metadata["deck_events"], "the visit schedule must ride along in the branch metadata"

    summary = run_branch_continuation(
        new_kernel,
        metadata=metadata,
        design=design,
        corpus=corpus,
        ticks=99,  # capped to the source horizon
        allow_spend=True,
        seat_factory=fake_seat_factory(),
        customer_llm=FakeCustomerLLM(new_kernel.recorder),
    )
    assert summary["final_tick"] == 6  # horizon cap bit: 2 + min(99, 6-2)

    rows = read_jsonl(output_root / "world_ledger.jsonl")
    arrivals = [r for r in rows if r.get("event_type") == "customer_utterance" and not (r.get("payload") or {}).get("reply")]
    assert any((r.get("payload") or {}).get("customer_id") == "CUS-T1" and r.get("tick") == 3 for r in arrivals), (
        "the visit scheduled for day 3 must still happen in the branch"
    )
    replies = [r for r in rows if r.get("event_type") == "customer_utterance" and (r.get("payload") or {}).get("reply")]
    assert replies, "a contacted customer must answer on the next tick"
    responses = [r for r in rows if r.get("event_type") == "agent_response" and (r.get("payload") or {}).get("seat_id") == "emp-A"]
    assert responses, "the seat must actually take a turn on the delivered visit"


def test_branch_bundle_carries_continuation_settings_for_the_next_fork(tmp_path: Path) -> None:
    """A bundle must persist the conditions its continuation ran under
    (workflow tools, customer visit schedule, absence days, model bindings,
    per-seat budgets, customer model), so a deeper fork rebuilt from the
    bundle ALONE runs under the same conditions instead of silently
    reverting to a pre-v4 world. The first live depth-1 trial had to
    reconstruct these from the sealed campaign spec because bundles did not
    carry them."""
    from company_twin.deck import CustomerEvent

    kernel = _source_kernel(tmp_path, "source_settings")
    recorder = kernel.recorder
    recorder.set_tick(1)
    recorder.append_ledger("tick_committed", {"tick": 1})

    workflow = _workflow_schedule(True)
    # through json so tuple fields take the list form they have on disk
    deck_events = json.loads(json.dumps([
        CustomerEvent(
            event_id="EVT-S1",
            probe_id="",
            customer_id="CUS-S1",
            application_id="APP-S1",
            product="投資信託",
            trigger_tick=3,
            deadline_tick=8,
            primary_seat="emp-A",
            participant_seats=("emp-A", "emp-C"),
            required_doc_ids=("DFH-SAL-021",),
            span_ids=(),
            world_visible="投資信託の申込を進めたい。",
            latent_truth="unit-test customer",
            routine=True,
        ).to_dict()
    ]))
    (recorder.run_root / "config.json").write_text(
        json.dumps(
            {
                "world": {
                    "schedule": {"ticks": 6, "workflow": workflow},
                    "population": {
                        "binding": {"emp-A": "openrouter:qwen/unit"},
                        "tick_budget": {"emp-A": 5},
                        "absence": {"emp-M": [3, 4]},
                    },
                    "deck": {"events": deck_events},
                },
                "model": {"customer": "fake:unit"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    first = tmp_path / "settings_level1"
    branch_kernel, metadata = rebuild_kernel_state(recorder.run_root, 1, first)
    run_branch_continuation(branch_kernel, metadata=metadata, allow_spend=False)

    config = json.loads((first / "config.json").read_text(encoding="utf-8"))
    assert config["world"]["schedule"]["workflow"] == workflow
    assert config["world"]["population"]["binding"] == {"emp-A": "openrouter:qwen/unit"}
    assert config["world"]["population"]["tick_budget"] == {"emp-A": 5}
    assert config["world"]["population"]["absence"] == {"emp-M": [3, 4]}
    assert config["world"]["deck"]["events"] == deck_events
    assert config["model"]["customer"] == "fake:unit"

    # the round trip: a second-level rebuild from the bundle alone recovers
    # every continuation setting the original world had
    second = tmp_path / "settings_level2"
    _, next_metadata = rebuild_kernel_state(
        first, 0, second, up_to_ordinal=len(read_jsonl(first / "world_ledger.jsonl"))
    )
    assert next_metadata["workflow"] == workflow
    assert next_metadata["model_binding"] == {"emp-A": "openrouter:qwen/unit"}
    assert next_metadata["tick_budget"] == {"emp-A": 5}
    assert next_metadata["absence"] == {"emp-M": [3, 4]}
    assert next_metadata["deck_events"] == deck_events
    assert next_metadata["customer_model"] == "fake:unit"
    assert next_metadata["prompt_mode"] == "measurement"
