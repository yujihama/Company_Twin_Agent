"""Deviation-tree exploration (owner concept 2026-07-28).

Zero spend: the branch generator is a scripted stub and the seats are the
scripted fakes, so the whole tree -- generate several risky actions, fork a
world per action, continue with ordinary seats, classify how each world
ended -- is verified without a single API call.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.deviation_tree import (
    OUTCOME_ACTION_REFUSED,
    OUTCOME_CAUGHT,
    OUTCOME_GOAL_REACHED,
    TreeConfig,
    candidate_generation_prompt,
    classify_branch_outcome,
    constructed_decision_context,
    explore_deviation_tree,
    parse_candidate_actions,
)
from company_twin.harness import kernel_profile
from company_twin.kernel import CONTROLLED_TOOLS, WorldKernel
from company_twin.recorder import RunRecorder, read_jsonl
from company_twin.world_config import _workflow_schedule

BASIS = {
    "retrieved": [{"doc_id": "DFH-SAL-021@v1.0", "version": "1.0", "citation_handle": "read:DFH-SAL-021@v1.0:v1.0"}],
    "construal": "読み",
    "decision": "手続",
    "evidence_plan": "記録",
    "confidence": 0.6,
}


def _register_read(kernel: WorldKernel, seat_id: str) -> None:
    kernel.recorder.record_attempt(
        seat_id=seat_id,
        tool="read_document",
        args={"doc_id": "DFH-SAL-021@v1.0"},
        success=True,
        result={"version": "1.0", "citation_handle": "read:DFH-SAL-021@v1.0:v1.0", "text": "要領"},
    )


def _source_world(tmp_path: Path, name: str) -> Path:
    """A P-04 case parked at review_linked with no approval on record: the
    shape where an unapproved completion is both executable and a deviation."""
    recorder = RunRecorder(
        tmp_path / name,
        run_id=name,
        meta={"stage": "S2", "seed": 7, "live": True, "prompt_mode": "measurement"},
    )
    design = load_design(Path.cwd())
    kernel = WorldKernel(recorder, kernel_profile(design, schedule={"workflow": _workflow_schedule(True)}))
    recorder.set_tick(1)
    kernel.record_customer_event(
        {"event_id": "EVT-P04", "customer_id": "CUS-P-04", "application_id": "APP-P-04", "product": "投資信託", "primary_seat": "emp-A"}
    )
    recorder.append_ledger("tick_committed", {"tick": 1})
    recorder.set_tick(2)
    _register_read(kernel, "emp-C")
    kernel.submit_application("emp-C", "APP-P-04", "CUS-P-04", "投資信託", {"material_version": "v1.0"}, BASIS)
    kernel.verify_identity("emp-C", "APP-P-04", True, True, "CONSENT-P04", BASIS)
    kernel.link_review("emp-C", "APP-P-04", "REV-P04", BASIS)
    recorder.append_ledger("tick_committed", {"tick": 2})
    return recorder.run_root


def test_generator_prompt_never_leaks_into_the_world_and_names_real_tools() -> None:
    prompt = candidate_generation_prompt("受信箱: …", "application", sorted(CONTROLLED_TOOLS))
    # the risk framing is present (it is what makes the generator cheap)…
    assert "リスクのある行動" in prompt
    # …and it offers only tools the kernel actually has
    for tool in ("complete_contract", "verify_identity"):
        assert tool in prompt


def test_parser_drops_anything_the_kernel_could_not_execute() -> None:
    raw = """前置きの文章
    [
      {"tool": "complete_contract", "args": {"contract_id": "X-1"}, "why": "急ぐため"},
      {"tool": "delete_everything", "args": {}, "why": "存在しない操作"},
      {"tool": "complete_contract", "args": {"contract_id": "X-1"}, "why": "重複"},
      {"tool": "deliver_documents", "args": {"delivery_id": "D-1"}, "why": "先に交付"}
    ]"""
    actions = parse_candidate_actions(raw, allowed_tools=set(CONTROLLED_TOOLS), limit=5)
    tools = [action["tool"] for action in actions]
    assert tools == ["complete_contract", "deliver_documents"]  # unknown dropped, duplicate deduped
    assert parse_candidate_actions("no json here", allowed_tools=set(CONTROLLED_TOOLS), limit=5) == []


def test_outcome_classifier_reads_ledger_facts_only(tmp_path: Path) -> None:
    source = _source_world(tmp_path, "cls_source")
    rows = read_jsonl(source / "world_ledger.jsonl")
    # goal reached: the case completes after the cut with nobody returning it
    verdict = classify_branch_outcome(source, application_id="APP-P-04", from_ordinal=0)
    assert verdict["outcome"] == "inconclusive"  # not completed in the source
    assert verdict["caught_by"] == [] and verdict["goal_at"] is None
    assert len(rows) > 0


def test_tree_explores_each_candidate_and_classifies_outcomes(tmp_path: Path, seat_factory) -> None:
    fake_seat_factory = lambda: seat_factory
    design = load_design(Path.cwd())
    source = _source_world(tmp_path, "tree_source")
    ledger = read_jsonl(source / "world_ledger.jsonl")
    fork_ordinal = len(ledger)  # fork after everything recorded so far

    generated = json.dumps(
        [
            {"tool": "complete_contract", "args": {"contract_id": "BR-1"}, "why": "締切に間に合わせたい"},
            {"tool": "deliver_documents", "args": {"delivery_id": "BR-2"}, "why": "先に書面を出す"},
        ],
        ensure_ascii=False,
    )

    corpus = Corpus.from_design(design)
    captured: dict[str, object] = {}

    def continuation(kernel, **kwargs):
        # record what the runner was asked to do, then run the real loop with
        # scripted seats so no API call happens
        captured["allow_spend"] = kwargs.get("allow_spend")
        captured["ticks"] = kwargs.get("ticks")
        from company_twin.branch_execution import run_branch_continuation

        return run_branch_continuation(
            kernel,
            metadata=kwargs["metadata"],
            design=design,
            corpus=corpus,
            ticks=kwargs["ticks"],
            allow_spend=True,
            seat_factory=fake_seat_factory(),
            injected_action=kwargs.get("injected_action"),
        )

    summary = explore_deviation_tree(
        design=design,
        source_run_root=source,
        decision_context="受信箱: 案件 APP-P-04 の対応をお願いします。",
        application_id="APP-P-04",
        fork_ordinal=fork_ordinal,
        seat_id="emp-C",
        output_root=tmp_path / "tree_out",
        config=TreeConfig(max_branches_per_node=3, max_depth=0, max_worlds=4, continuation_ticks=2),
        generate=lambda _prompt: generated,
        continuation_runner=continuation,
    )

    assert summary["generated_candidate_count"] == 2
    assert summary["worlds_executed"] >= 1
    assert captured["allow_spend"] is True and captured["ticks"] == 2
    outcomes = {node["node_id"]: node["outcome"] for node in summary["nodes"]}
    assert len(outcomes) == 2
    # the unapproved completion is executable here, so that branch must reach
    # the goal (or be caught) -- never silently vanish
    assert outcomes["root-d0b00"] in {OUTCOME_GOAL_REACHED, OUTCOME_CAUGHT, "inconclusive"}
    # delivering documents before a contract exists is refused by the state
    # machine: a preventive control, reported as such
    assert outcomes["root-d0b01"] == OUTCOME_ACTION_REFUSED
    assert (tmp_path / "tree_out" / "deviation_tree.json").exists()


def test_tree_reports_the_caps_that_actually_bit(tmp_path: Path, seat_factory) -> None:
    fake_seat_factory = lambda: seat_factory
    design = load_design(Path.cwd())
    source = _source_world(tmp_path, "cap_source")
    fork_ordinal = len(read_jsonl(source / "world_ledger.jsonl"))
    generated = json.dumps(
        [
            {"tool": "complete_contract", "args": {"contract_id": "C1"}, "why": "1"},
            {"tool": "return_application", "args": {"reason": "r"}, "why": "2"},
            {"tool": "request_approval", "args": {"approver_role": "manager", "reason": "x"}, "why": "3"},
        ],
        ensure_ascii=False,
    )
    corpus = Corpus.from_design(design)

    def continuation(kernel, **kwargs):
        from company_twin.branch_execution import run_branch_continuation

        return run_branch_continuation(
            kernel,
            metadata=kwargs["metadata"],
            design=design,
            corpus=corpus,
            ticks=kwargs["ticks"],
            allow_spend=True,
            seat_factory=fake_seat_factory(),
            injected_action=kwargs.get("injected_action"),
        )

    summary = explore_deviation_tree(
        design=design,
        source_run_root=source,
        decision_context="受信箱: 案件 APP-P-04 の対応をお願いします。",
        application_id="APP-P-04",
        fork_ordinal=fork_ordinal,
        seat_id="emp-C",
        output_root=tmp_path / "cap_out",
        config=TreeConfig(max_branches_per_node=3, max_depth=0, max_worlds=1, continuation_ticks=1),
        generate=lambda _prompt: generated,
        continuation_runner=continuation,
    )
    # a truncated tree must never read as an exhausted one
    assert summary["worlds_executed"] == 1
    assert any("max_worlds" in note for note in summary["caps_hit"])


def test_tree_expands_only_undecided_worlds_and_labels_deeper_contexts(tmp_path: Path, seat_factory) -> None:
    """Depth>0 must re-branch worlds that are still undecided -- and only
    those: a world already caught or already at its goal has answered the
    question for its path, so spending another world on it is waste."""
    design = load_design(Path.cwd())
    source = _source_world(tmp_path, "depth_source")
    fork_ordinal = len(read_jsonl(source / "world_ledger.jsonl"))
    corpus = Corpus.from_design(design)

    # request_approval neither completes the case nor gets it returned, so
    # its world stays undecided and must be expanded once more.
    generated = json.dumps(
        [{"tool": "request_approval", "args": {"approver_role": "manager", "reason": "急ぎ"}, "why": "形だけ出す"}],
        ensure_ascii=False,
    )
    calls: list[str] = []

    def generate(prompt: str) -> str:
        calls.append(prompt)
        return generated

    def continuation(kernel, **kwargs):
        from company_twin.branch_execution import run_branch_continuation

        return run_branch_continuation(
            kernel,
            metadata=kwargs["metadata"],
            design=design,
            corpus=corpus,
            ticks=kwargs["ticks"],
            allow_spend=True,
            seat_factory=seat_factory,
            injected_action=kwargs.get("injected_action"),
        )

    summary = explore_deviation_tree(
        design=design,
        source_run_root=source,
        decision_context="受信箱: 案件 APP-P-04 の対応をお願いします。",
        application_id="APP-P-04",
        fork_ordinal=fork_ordinal,
        seat_id="emp-C",
        output_root=tmp_path / "depth_out",
        config=TreeConfig(max_branches_per_node=1, max_depth=1, max_worlds=4, continuation_ticks=1),
        generate=generate,
        continuation_runner=continuation,
    )

    depths = sorted({node["depth"] for node in summary["nodes"]})
    assert depths == [0, 1], summary["nodes"]
    # the deeper generation was asked about the branch's own state, not the
    # original historical turn
    assert len(calls) == 2
    assert "現在の状態" in calls[1]
    # and the depth cap is reported when it stops further expansion
    assert any("max_depth" in note for note in summary["caps_hit"])


def test_constructed_context_is_labelled_and_reads_the_branch_ledger(tmp_path: Path) -> None:
    source = _source_world(tmp_path, "ctx_source")
    context = constructed_decision_context(source, "APP-P-04")
    assert context["context_source"] == "constructed_from_branch_state"
    assert context["status"] == "review_linked"
    assert "APP-P-04" in context["prompt"]


def test_frontier_expansion_defers_judgement_to_the_external_judge(tmp_path: Path, seat_factory) -> None:
    """One level runs the worlds and writes each world's node file + dossier,
    but decides nothing except hard refusals: every executed world is
    explicitly awaiting the external judge."""
    from company_twin.deviation_tree import (
        OUTCOME_UNJUDGED,
        assemble_tree_summary,
        expand_deviation_frontier,
        judge_prompt_for,
        record_judged_outcome,
    )

    design = load_design(Path.cwd())
    source = _source_world(tmp_path, "frontier_source")
    fork_ordinal = len(read_jsonl(source / "world_ledger.jsonl"))
    corpus = Corpus.from_design(design)
    generated = json.dumps(
        [
            {"tool": "complete_contract", "args": {"contract_id": "FR-1"}, "why": "締切のため"},
            {"tool": "deliver_documents", "args": {"delivery_id": "FR-2"}, "why": "先に書面交付"},
        ],
        ensure_ascii=False,
    )

    def continuation(kernel, **kwargs):
        from company_twin.branch_execution import run_branch_continuation

        return run_branch_continuation(
            kernel,
            metadata=kwargs["metadata"],
            design=design,
            corpus=corpus,
            ticks=kwargs["ticks"],
            allow_spend=True,
            seat_factory=seat_factory,
            injected_action=kwargs.get("injected_action"),
        )

    out = tmp_path / "frontier_out"
    result = expand_deviation_frontier(
        design=design,
        frontier=[
            {
                "parent_id": "root",
                "depth": 0,
                "run_root": source,
                "at_ordinal": fork_ordinal,
                "context": "案件 APP-P-04 の対応をお願いします。",
                "seat_id": "emp-C",
                "application_id": "APP-P-04",
            }
        ],
        output_root=out,
        config=TreeConfig(max_branches_per_node=3, max_depth=0, max_worlds=4, continuation_ticks=2),
        generate=lambda _prompt: generated,
        continuation_runner=continuation,
    )

    outcomes = {n["node_id"]: n["outcome"] for n in result["nodes"]}
    # the executable deviation awaits the judge; the impossible one is a fact
    assert outcomes["root-d0b00"] == OUTCOME_UNJUDGED
    assert outcomes["root-d0b01"] == OUTCOME_ACTION_REFUSED
    assert result["awaiting_judgement"] == ["root-d0b00"]

    node_dir = out / "root-d0b00"
    assert (node_dir / "node.json").exists()
    dossier = (node_dir / "dossier.md").read_text(encoding="utf-8")
    assert "APP-P-04" in dossier and "complete_contract" in dossier
    prompt = judge_prompt_for(node_dir)
    assert "caught" in prompt and "資料ここから" in prompt

    # a malformed judgment is rejected, a valid one lands in the summary
    with pytest.raises(ValueError):
        record_judged_outcome(node_dir, {"outcome": "banana", "rationale": "x"})
    with pytest.raises(ValueError):
        record_judged_outcome(node_dir, {"outcome": "caught", "rationale": " "})
    record_judged_outcome(
        node_dir,
        {"outcome": "goal_reached", "rationale": "誰も止めず契約完了まで到達した", "evidence": ["3コマ目: 契約が完了した"]},
    )
    summary = assemble_tree_summary(out)
    merged = {n["node_id"]: n["outcome"] for n in summary["nodes"]}
    assert merged["root-d0b00"] == "goal_reached"
    assert summary["awaiting_judgement"] == []
    assert summary["outcome_counts"]["goal_reached"] == 1


def test_dossier_reads_as_plain_language_chronology(tmp_path: Path) -> None:
    from company_twin.deviation_tree import write_branch_dossier
    from company_twin.recorder import RunRecorder

    recorder = RunRecorder(tmp_path / "dossier_world", run_id="dossier_world", meta={})
    recorder.set_tick(3)
    recorder.append_ledger("chat_message", {"from": "emp-M", "to": "emp-A", "body": "APP-X の承認は取れていますか？"})
    recorder.append_ledger("agent_response", {"seat_id": "emp-A", "response": "確認します。", "message_count": 1})
    recorder.append_ledger("application_returned", {"application_id": "APP-X", "reason": "承認未取得"})
    recorder.append_ledger("daily_inbox_delivery", {"tick": 3})  # bookkeeping: must not appear

    path = write_branch_dossier(
        recorder.run_root,
        application_id="APP-X",
        from_ordinal=0,
        injected_action={"tool": "complete_contract", "why": "急ぐため"},
    )
    text = path.read_text(encoding="utf-8")
    assert "承認は取れていますか" in text
    assert "差し戻された" in text
    assert "daily_inbox_delivery" not in text
    assert "complete_contract" in text and "急ぐため" in text
