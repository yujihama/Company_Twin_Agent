import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.errors import GraphRecursionError

from company_twin import agents
from company_twin.campaign import aggregate_s0_divergence
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.harness import run_s0
from company_twin.recorder import RunRecorder, read_jsonl


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
