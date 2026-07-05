import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.errors import GraphRecursionError

from company_twin import agents
from company_twin.acceptance import a13_full_world_evidence, check_bundle
from company_twin.campaign import aggregate_s0_divergence
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.harness import CONTROLLED_ACTION_TOOLS, _tool_count, run_s0
from company_twin.oracles import aggregate_ensemble_triage
from company_twin.recorder import RunRecorder, read_jsonl
from company_twin.semantic_grounding import evaluate_semantic_grounding_campaign


def test_deepagentseat_records_failed_invoke(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    class FailingAgent:
        def invoke(self, *_args, **_kwargs):
            raise GraphRecursionError("recursion limit")

    monkeypatch.setattr(agents, "register_company_twin_profile", lambda: None)
    monkeypatch.setattr(agents, "_chat_model", lambda _model: object())
    monkeypatch.setitem(sys.modules, "deepagents", SimpleNamespace(create_deep_agent=lambda **_kwargs: FailingAgent()))

    recorder = RunRecorder(tmp_path, "failed")
    seat = agents.DeepAgentSeat(
        seat_id="emp-A",
        role="sales",
        tools=[],
        model="openrouter:qwen/qwen3.6-flash",
        root=tmp_path,
        recorder=recorder,
        recursion_limit=1,
    )

    with recorder.origin("agent"), pytest.raises(GraphRecursionError):
        seat.turn("prompt")

    attempts = read_jsonl(tmp_path / "attempts.jsonl")
    failed = [row for row in attempts if row["tool"] == "llm_invoke" and row["success"] is False]
    assert failed
    assert failed[0]["args"]["error_type"] == "GraphRecursionError"
    assert failed[0]["result"]["error_type"] == "GraphRecursionError"


class _RecursionSeat:
    backend = "deepagents"
    model = "openrouter:qwen/qwen3.6-flash"

    def turn(self, _prompt: str) -> str:
        raise GraphRecursionError("recursion limit")


def test_run_s0_records_recursion_exhausted_answer(tmp_path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)

    def factory(*_args, **_kwargs):
        return _RecursionSeat()

    result = run_s0(
        design=design,
        corpus=corpus,
        probe_id="P-01",
        span_id="AMB-02",
        seat_id="emp-A",
        run_root=tmp_path / "s0",
        variant=0,
        seat_factory=factory,
    )

    assert result["outcome"] == "recursion_exhausted"
    assert result["parsed"] is False
    assert result["response"] == ""
    answer = (tmp_path / "s0" / "s0_answer.json").read_text(encoding="utf-8")
    assert '"outcome": "recursion_exhausted"' in answer
    ledger = read_jsonl(tmp_path / "s0" / "world_ledger.jsonl")
    assert any(row["event_type"] == "agent_error" and row["payload"]["error_type"] == "GraphRecursionError" for row in ledger)


def test_s0_divergence_counts_recursion_as_no_grounded_answer(tmp_path) -> None:
    design = load_design(Path.cwd())
    rows = [
        {
            "probe_id": "P-01",
            "span_id": "AMB-02",
            "seat_id": "emp-A",
            "model": "openrouter:x",
            "variant": 0,
            "response": "{}",
            "parsed": True,
            "likely_reading": "ask_manager",
        },
        {
            "probe_id": "P-01",
            "span_id": "AMB-02",
            "seat_id": "emp-A",
            "model": "openrouter:y",
            "variant": 1,
            "response": "",
            "parsed": False,
            "outcome": "recursion_exhausted",
        },
    ]

    payload = aggregate_s0_divergence(design, rows, campaign_root=tmp_path)
    cell = payload["cells"][0]
    assert cell["answer_count"] == 2
    assert cell["parsed_rate"] == 0.5
    assert cell["clusters"]["no_grounded_answer"] == 1


def test_append_jsonl_flushes_and_fsyncs(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    calls: list[int] = []
    monkeypatch.setattr("company_twin.recorder.os.fsync", lambda fd: calls.append(fd))

    recorder = RunRecorder(tmp_path, "fsync")
    recorder.append_jsonl("attempts.jsonl", {"ok": True})

    assert calls
    assert read_jsonl(tmp_path / "attempts.jsonl") == [{"ok": True}]


def test_append_jsonl_serializes_concurrent_unicode_writes(tmp_path) -> None:
    recorder = RunRecorder(tmp_path, "concurrent")
    text = "証跡登録と文書検索の同時記録" * 200

    def append_row(idx: int) -> None:
        recorder.append_jsonl("attempts.jsonl", {"idx": idx, "text": text})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append_row, range(40)))

    rows = read_jsonl(tmp_path / "attempts.jsonl")
    assert sorted(row["idx"] for row in rows) == list(range(40))
    assert {row["text"] for row in rows} == {text}


def test_runtime_tool_count_uses_recorder_memory_not_attempts_file(tmp_path) -> None:
    recorder = RunRecorder(tmp_path, "memory-count")
    with recorder.origin("agent"):
        recorder.record_attempt(
            seat_id="emp-A",
            tool="record_customer_contact",
            args={},
            success=True,
            result={},
        )
    (tmp_path / "attempts.jsonl").write_bytes(b"\xa7 not utf8\n")

    assert _tool_count(recorder, "emp-A", CONTROLLED_ACTION_TOOLS) == 1


def test_marked_failed_bundle_reports_failure_without_decoding_jsonl(tmp_path) -> None:
    run_root = tmp_path / "s2_seed0"
    run_root.mkdir()
    (run_root / "meta.json").write_text('{"stage": "S2", "anchor": false}', encoding="utf-8")
    (run_root / "attempts.jsonl").write_bytes(b"\xa7 not utf8\n")
    (run_root / "failed_run.json").write_text('{"error_type": "UnicodeDecodeError"}', encoding="utf-8")

    report = check_bundle(run_root)

    assert report.passed is False
    assert report.results[0].gate == "bundle_completed"
    assert "UnicodeDecodeError" in report.results[0].detail


def test_ensemble_triage_excludes_marked_failed_bundle(tmp_path) -> None:
    run_root = tmp_path / "s2_seed0"
    run_root.mkdir()
    (run_root / "meta.json").write_text('{"stage": "S2", "anchor": false}', encoding="utf-8")
    (run_root / "attempts.jsonl").write_bytes(b"\xa7 not utf8\n")
    (run_root / "failed_run.json").write_text('{"error_type": "UnicodeDecodeError"}', encoding="utf-8")

    payload = aggregate_ensemble_triage(tmp_path)

    assert payload["run_filter"]["included_run_count"] == 0
    assert payload["run_filter"]["excluded_failed_run_ids"] == ["s2_seed0"]


def test_g3_campaign_excludes_marked_failed_bundle_without_decoding_jsonl(tmp_path) -> None:
    failed_root = tmp_path / "s2_seed0"
    failed_root.mkdir()
    (failed_root / "attempts.jsonl").write_bytes(b"\xa7 not utf8\n")
    (failed_root / "basis_records.jsonl").write_text(
        '{"action_id": "a1", "seat_id": "emp-A"}\n',
        encoding="utf-8",
    )
    (failed_root / "failed_run.json").write_text('{"error_type": "UnicodeDecodeError"}', encoding="utf-8")

    payload = evaluate_semantic_grounding_campaign(tmp_path)

    assert payload["run_count"] == 0
    assert payload["basis_action_bound"] == 0
    assert payload["excluded_failed_run_ids"] == ["s2_seed0"]
    assert not (failed_root / "g3_semantic_grounding.json").exists()


def test_a13_accepts_completed_s2_replacement_while_failed_bundle_remains(tmp_path) -> None:
    def completed_s2(name: str, *, anchor: bool) -> None:
        run_root = tmp_path / name
        run_root.mkdir()
        (run_root / "meta.json").write_text(f'{{"stage": "S2", "anchor": {str(anchor).lower()}}}', encoding="utf-8")
        (run_root / "world_ledger.jsonl").write_text(
            '{"event_type": "month_end_close"}\n{"event_type": "customer_utterance"}\n',
            encoding="utf-8",
        )
        (run_root / "triage").mkdir()
        (run_root / "triage" / "metrics.json").write_text(
            '{"controlled_actions_agent": 1, "basis_action_bound": 1}',
            encoding="utf-8",
        )

    completed_s2("anchor_s2_seed0", anchor=True)
    completed_s2("s2_seed0_retry1", anchor=False)
    failed_root = tmp_path / "s2_seed0"
    failed_root.mkdir()
    (failed_root / "meta.json").write_text('{"stage": "S2", "anchor": false}', encoding="utf-8")
    (failed_root / "failed_run.json").write_text('{"error_type": "UnicodeDecodeError"}', encoding="utf-8")
    for filename in ("ensemble_triage.json", "attribution_table.json", "min_repro_jobs.json", "finding_registry.json"):
        (tmp_path / filename).write_text("{}", encoding="utf-8")

    assert a13_full_world_evidence(tmp_path).passed
