"""Tests for the WP-14 backcasting LIVE re-simulation runner.

All fixtures here are offline: no LLM/API call is made anywhere in this file.
A fake tool-using seat factory stands in for the live deepagents-backed
default_seat_factory, matching the FakeSeatAgent pattern in tests/conftest.py
-- backend is stamped "test-fake" so it can never be mistaken for
readiness-eligible live evidence.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import company_twin.cli as cli_module
from company_twin.backcasting import extract_backcasting_cases, write_backcasting_inputs, write_backcasting_report
from company_twin.backcasting_run import (
    BACKCASTING_RESULTS_SCHEMA_VERSION,
    JUDGE_PROMPT_VERSION,
    NOT_REPRODUCED,
    READINESS_ALLOWED_JUDGE_BACKENDS,
    REPRODUCED,
    BackcastingInputsError,
    LocalReproductionJudge,
    assert_two_plane_clean,
    backcasting_seat_prompt,
    run_backcasting_resimulation,
    select_backcasting_sample,
)
from company_twin.cli import app
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.recorder import RunRecorder, read_jsonl


@pytest.fixture(scope="module")
def design():
    return load_design(Path.cwd())


@pytest.fixture(scope="module")
def corpus(design):
    return Corpus.from_design(design)


@pytest.fixture(scope="module")
def extraction(design):
    return extract_backcasting_cases(design)


def _write_inputs(tmp_path: Path, extraction: dict[str, Any]) -> dict[str, Any]:
    write_backcasting_inputs(tmp_path, extraction)
    return extraction


class ToolUsingFakeSeat:
    """Offline stand-in for DeepAgentSeat that actually uses the world tools.

    Searches the corpus, reads one real document, and answers -- so the
    per-case attempts.jsonl carries genuine search_corpus/read_document
    evidence just like a live run would.
    """

    backend = "test-fake"
    model = "fake:unit"

    def __init__(
        self,
        *,
        seat_id: str,
        role: str,
        tools: list[Any],
        recorder: RunRecorder,
        prompts_log: list[str] | None = None,
        fail: bool = False,
        empty: bool = False,
        self_reported_override: list[str] | None = None,
        skip_reading: bool = False,
    ):
        self.seat_id = seat_id
        self.role = role
        self.tools = {tool.__name__: tool for tool in tools}
        self.recorder = recorder
        self.prompts_log = prompts_log
        self.fail = fail
        self.empty = empty
        self.self_reported_override = self_reported_override
        self.skip_reading = skip_reading

    def turn(self, prompt: str) -> str:
        if self.prompts_log is not None:
            self.prompts_log.append(prompt)
        self.recorder.record_attempt(
            seat_id=self.seat_id,
            tool="llm_invoke",
            args={"backend": self.backend, "model": self.model, "prompt_chars": len(prompt)},
            success=True,
            result={"phase": "start"},
        )
        if self.fail:
            raise RuntimeError("simulated seat failure")
        if self.empty:
            return ""
        read_doc_ids: list[str] = []
        if not self.skip_reading:
            hits = json.loads(self.tools["search_corpus"]("高齢者 追加確認 承認", 3))
            doc_id = hits[0]["doc_id"] if hits else "DFH-SAL-021"
            self.tools["read_document"](doc_id, "確認", 800)
            read_doc_ids = [doc_id]
        cited = self.self_reported_override if self.self_reported_override is not None else read_doc_ids
        response = json.dumps(
            {
                "likely_reading": "追加確認と管理者への相談が必要と読む",
                "response": "処理を保留し、管理者の確認を取ってから進める",
                "required_approver_or_evidence": "管理者の確認と記録",
                "cited_doc_ids": cited,
            },
            ensure_ascii=False,
        )
        self.recorder.record_attempt(
            seat_id=self.seat_id,
            tool="llm_response",
            args={"backend": self.backend, "model": self.model, "prompt_chars": len(prompt)},
            success=True,
            result={"response_chars": len(response)},
        )
        return response


def _fake_factory(**seat_kwargs: Any):
    def factory(*, seat_id: str, role: str, tools: list[Any], recorder: RunRecorder, recursion_limit: int) -> ToolUsingFakeSeat:
        return ToolUsingFakeSeat(seat_id=seat_id, role=role, tools=tools, recorder=recorder, **seat_kwargs)

    return factory


class FakeJudge:
    backend = "test-fake-judge"
    model = "fake:unit"

    def __init__(self, label: str = REPRODUCED):
        self.label = label
        self.calls: list[dict[str, str]] = []

    def judge(self, *, situation: str, documented_response: str, seat_answer: str) -> dict[str, Any]:
        self.calls.append({"situation": situation, "documented_response": documented_response, "seat_answer": seat_answer})
        return {"label": self.label, "confidence": 0.9, "rationale": "fake judge"}


def _run(tmp_path: Path, design, corpus, *, judge=None, sample_size=2, sample_seed=0, factory=None, **kwargs):
    return run_backcasting_resimulation(
        tmp_path,
        design=design,
        corpus=corpus,
        judge=judge or FakeJudge(),
        sample_size=sample_size,
        sample_seed=sample_seed,
        seat_factory=factory or _fake_factory(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Pre-registered deterministic sampling
# ---------------------------------------------------------------------------


def test_select_backcasting_sample_is_deterministic_for_same_seed() -> None:
    cases = [{"case_id": f"case_{idx:03d}"} for idx in range(50)]

    first = select_backcasting_sample(cases, sample_size=10, sample_seed=42)
    second = select_backcasting_sample(cases, sample_size=10, sample_seed=42)

    assert first == second
    assert first["sample_size"] == 10
    assert len(first["selected_case_ids"]) == 10
    assert len(set(first["selected_case_ids"])) == 10  # no duplicates


def test_select_backcasting_sample_differs_across_seeds() -> None:
    cases = [{"case_id": f"case_{idx:03d}"} for idx in range(50)]

    seed_a = select_backcasting_sample(cases, sample_size=10, sample_seed=1)
    seed_b = select_backcasting_sample(cases, sample_size=10, sample_seed=2)

    assert seed_a["selected_case_ids"] != seed_b["selected_case_ids"]


def test_select_backcasting_sample_independent_of_input_order() -> None:
    cases = [{"case_id": f"case_{idx:03d}"} for idx in range(30)]
    shuffled = list(reversed(cases))

    ordered_result = select_backcasting_sample(cases, sample_size=8, sample_seed=7)
    shuffled_result = select_backcasting_sample(shuffled, sample_size=8, sample_seed=7)

    assert ordered_result["selected_case_ids"] == shuffled_result["selected_case_ids"]


def test_select_backcasting_sample_full_population_when_size_omitted() -> None:
    cases = [{"case_id": f"case_{idx:03d}"} for idx in range(12)]

    sample = select_backcasting_sample(cases, sample_size=None, sample_seed=0)

    assert sample["sample_size"] == 12
    assert set(sample["selected_case_ids"]) == {case["case_id"] for case in cases}


def test_select_backcasting_sample_caps_at_population_size() -> None:
    cases = [{"case_id": f"case_{idx:03d}"} for idx in range(5)]

    sample = select_backcasting_sample(cases, sample_size=1000, sample_seed=0)

    assert sample["sample_size"] == 5


# ---------------------------------------------------------------------------
# Two-plane separation
# ---------------------------------------------------------------------------


def test_backcasting_seat_prompt_contains_only_situation_never_documented_response() -> None:
    situation = "顧客から高齢を理由に理解確認の追加対応を求められた。"
    documented_response = "管理者に相談のうえ、追加の理解確認を実施し記録する。"

    prompt = backcasting_seat_prompt(situation)

    assert situation in prompt
    assert documented_response not in prompt
    for banned in ("case_id", "documented_response", "backcasting", "reproduction", "probe", "span", "oracle", "experiment", "mutation"):
        assert banned not in prompt.lower()
    assert_two_plane_clean(prompt)  # must not raise


def test_assert_two_plane_clean_raises_on_leaked_vocabulary() -> None:
    with pytest.raises(ValueError, match="two-plane violation"):
        assert_two_plane_clean("this probe's documented_response is X")


def test_run_backcasting_resimulation_prompt_sent_to_seat_is_two_plane_clean(tmp_path: Path, design, corpus, extraction) -> None:
    _write_inputs(tmp_path, extraction)
    selected = select_backcasting_sample(extraction["cases"], sample_size=1, sample_seed=0)["selected_case_ids"]
    cases_by_id = {c["case_id"]: c for c in extraction["cases"]}
    prompts: list[str] = []

    _run(tmp_path, design, corpus, sample_size=1, factory=_fake_factory(prompts_log=prompts))

    assert prompts, "seat should have been invoked"
    for prompt, case_id in zip(prompts, selected):
        case = cases_by_id[case_id]
        assert case_id not in prompt
        assert case["documented_response"] not in prompt or case["documented_response"] == ""
        assert_two_plane_clean(prompt)


# ---------------------------------------------------------------------------
# Real corpus access: seat receives working tools, calls land in the trace
# ---------------------------------------------------------------------------


def test_seat_receives_working_corpus_tools_and_calls_are_recorded(tmp_path: Path, design, corpus, extraction) -> None:
    _write_inputs(tmp_path, extraction)

    payload = _run(tmp_path, design, corpus, sample_size=2)

    for row in payload["results"]:
        attempts = read_jsonl(Path(row["run_root"]) / "attempts.jsonl")
        tools_used = {attempt["tool"] for attempt in attempts if attempt.get("success")}
        assert "llm_invoke" in tools_used
        assert "search_corpus" in tools_used
        assert "read_document" in tools_used
        # The read succeeded against the real compiled corpus.
        read_docs = [a for a in attempts if a["tool"] == "read_document" and a.get("success")]
        assert read_docs and all(a["args"]["doc_id"].startswith("DFH-") for a in read_docs)


def test_viewed_doc_ids_derived_from_trace_not_self_report(tmp_path: Path, design, corpus, extraction) -> None:
    _write_inputs(tmp_path, extraction)
    # Seat reads a real document via tools but self-reports a fabricated id:
    # the runner must trust the trace, not the model.
    factory = _fake_factory(self_reported_override=["DFH-FAKE-999"])

    payload = _run(tmp_path, design, corpus, sample_size=2, factory=factory)

    for row in payload["results"]:
        attempts = read_jsonl(Path(row["run_root"]) / "attempts.jsonl")
        trace_docs = sorted({a["args"]["doc_id"] for a in attempts if a["tool"] == "read_document" and a.get("success")})
        assert row["viewed_doc_ids"] == trace_docs
        assert "DFH-FAKE-999" not in row["viewed_doc_ids"]
        assert row["self_reported_doc_ids"] == ["DFH-FAKE-999"]
        assert row["cited_but_not_viewed_doc_ids"] == ["DFH-FAKE-999"]


def test_self_report_without_any_read_yields_empty_viewed_and_flags_fabrication(tmp_path: Path, design, corpus, extraction) -> None:
    _write_inputs(tmp_path, extraction)
    # Reproduces the live-pass defect shape: no read_document at all, but a
    # plausible-looking citation in the answer.
    factory = _fake_factory(skip_reading=True, self_reported_override=["DFH-SAL-021"])

    payload = _run(tmp_path, design, corpus, sample_size=2, factory=factory)

    for row in payload["results"]:
        assert row["viewed_doc_ids"] == []
        assert row["self_reported_doc_ids"] == ["DFH-SAL-021"]
        assert row["cited_but_not_viewed_doc_ids"] == ["DFH-SAL-021"]


def test_backcasting_case_artifact_records_both_provenance_lists(tmp_path: Path, design, corpus, extraction) -> None:
    _write_inputs(tmp_path, extraction)

    payload = _run(tmp_path, design, corpus, sample_size=1)

    row = payload["results"][0]
    case_artifact = json.loads((Path(row["run_root"]) / "backcasting_case.json").read_text(encoding="utf-8"))
    assert case_artifact["viewed_doc_ids"] == row["viewed_doc_ids"]
    assert case_artifact["self_reported_doc_ids"] == row["self_reported_doc_ids"]
    assert "provenance_note" in case_artifact


# ---------------------------------------------------------------------------
# Honest-fail: failed seat call is recorded, not dropped
# ---------------------------------------------------------------------------


def test_failed_seat_call_recorded_as_not_reproduced_with_detail_not_dropped(tmp_path: Path, design, corpus, extraction) -> None:
    _write_inputs(tmp_path, extraction)
    sample = select_backcasting_sample(extraction["cases"], sample_size=3, sample_seed=0)

    payload = _run(tmp_path, design, corpus, sample_size=3, factory=_fake_factory(fail=True))

    assert len(payload["results"]) == 3
    for row in payload["results"]:
        assert row["reproduced"] is False
        assert "seat call failed" in row["detail"]
    # Every selected case_id must appear -- nothing silently dropped.
    assert {row["case_id"] for row in payload["results"]} == set(sample["selected_case_ids"])


def test_seat_factory_construction_failure_is_an_honest_row(tmp_path: Path, design, corpus, extraction) -> None:
    _write_inputs(tmp_path, extraction)

    def broken_factory(**_kwargs: Any):
        raise KeyError("OPENROUTER_API_KEY")

    payload = _run(tmp_path, design, corpus, sample_size=2, factory=broken_factory)

    assert len(payload["results"]) == 2
    assert all(row["reproduced"] is False for row in payload["results"])
    assert all("seat call failed" in row["detail"] for row in payload["results"])


def test_empty_seat_response_is_an_honest_failed_row(tmp_path: Path, design, corpus, extraction) -> None:
    _write_inputs(tmp_path, extraction)

    payload = _run(tmp_path, design, corpus, sample_size=2, factory=_fake_factory(empty=True))

    assert len(payload["results"]) == 2
    for row in payload["results"]:
        assert row["reproduced"] is False
        assert "empty response" in row["detail"]


def test_run_backcasting_resimulation_raises_when_inputs_missing(tmp_path: Path, design, corpus) -> None:
    with pytest.raises(BackcastingInputsError, match="backcasting-extract"):
        _run(tmp_path, design, corpus, sample_size=5)

    # A silent no-op results file must never be written -- it could be
    # mistaken for a completed live pass.
    assert not (tmp_path / "backcasting_resimulation_results.json").exists()


def test_run_backcasting_resimulation_raises_when_inputs_have_zero_cases(tmp_path: Path, design, corpus) -> None:
    (tmp_path / "backcasting_inputs.json").write_text(json.dumps({"cases": []}), encoding="utf-8")

    with pytest.raises(BackcastingInputsError, match="zero cases"):
        _run(tmp_path, design, corpus, sample_size=5)

    assert not (tmp_path / "backcasting_resimulation_results.json").exists()


def test_run_backcasting_resimulation_rejects_unknown_seat_id(tmp_path: Path, design, corpus, extraction) -> None:
    _write_inputs(tmp_path, extraction)

    with pytest.raises(ValueError, match="unknown seat_id"):
        _run(tmp_path, design, corpus, sample_size=1, seat_id="not-a-seat")


# ---------------------------------------------------------------------------
# Proxy judge cannot mark readiness-eligible
# ---------------------------------------------------------------------------


def test_local_reproduction_judge_backend_not_in_readiness_allowlist() -> None:
    judge = LocalReproductionJudge()
    assert judge.backend not in READINESS_ALLOWED_JUDGE_BACKENDS


def test_run_backcasting_resimulation_local_proxy_judge_marks_not_readiness_eligible(tmp_path: Path, design, corpus, extraction) -> None:
    _write_inputs(tmp_path, extraction)

    payload = _run(tmp_path, design, corpus, sample_size=3, judge=LocalReproductionJudge())

    assert payload["judge"]["backend"] == "local_reproduction_proxy"
    assert payload["judge"]["readiness_eligible"] is False
    assert payload["judge"]["prompt_version"] == JUDGE_PROMPT_VERSION


def test_run_backcasting_resimulation_records_readiness_eligible_true_for_openrouter_backend(tmp_path: Path, design, corpus, extraction) -> None:
    _write_inputs(tmp_path, extraction)

    class FakeOpenRouterBackedJudge:
        backend = "openrouter"
        model = "fake-openrouter-model"

        def judge(self, *, situation: str, documented_response: str, seat_answer: str) -> dict[str, Any]:
            return {"label": REPRODUCED, "confidence": 0.8, "rationale": "stubbed openrouter-backend judge for offline test"}

    payload = _run(tmp_path, design, corpus, sample_size=2, judge=FakeOpenRouterBackedJudge())

    assert payload["judge"]["readiness_eligible"] is True


# ---------------------------------------------------------------------------
# Report round-trip
# ---------------------------------------------------------------------------


def test_backcasting_run_then_report_round_trip(tmp_path: Path, design, corpus, extraction) -> None:
    _write_inputs(tmp_path, extraction)

    run_payload = _run(tmp_path, design, corpus, sample_size=10, judge=FakeJudge(label=REPRODUCED))
    assert run_payload["schema_version"] == BACKCASTING_RESULTS_SCHEMA_VERSION

    report = write_backcasting_report(tmp_path)

    assert report["passed"] is True
    assert report["scoring"]["scored_result_count"] == 10
    assert report["scoring"]["reproduction_rate"] == 1.0


def test_backcasting_run_then_report_round_trip_below_target(tmp_path: Path, design, corpus, extraction) -> None:
    _write_inputs(tmp_path, extraction)

    _run(tmp_path, design, corpus, sample_size=10, judge=FakeJudge(label=NOT_REPRODUCED))
    report = write_backcasting_report(tmp_path)

    assert report["passed"] is False
    assert report["scoring"]["reproduction_rate"] == 0.0


def test_backcasting_run_writes_per_case_run_artifacts_under_campaign_root(tmp_path: Path, design, corpus, extraction) -> None:
    _write_inputs(tmp_path, extraction)
    sample_case_id = select_backcasting_sample(extraction["cases"], sample_size=1, sample_seed=0)["selected_case_ids"][0]

    _run(tmp_path, design, corpus, sample_size=1)

    case_run_root = tmp_path / "backcasting_runs" / sample_case_id
    assert case_run_root.exists()
    assert (case_run_root / "attempts.jsonl").exists()
    assert (case_run_root / "backcasting_case.json").exists()
    attempts_text = (case_run_root / "attempts.jsonl").read_text(encoding="utf-8")
    assert "llm_invoke" in attempts_text
    assert "read_document" in attempts_text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_backcasting_run_cli_offline_with_proxy_seat_flag(tmp_path: Path, extraction, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_inputs(tmp_path, extraction)
    # Guarantee fully-offline behavior regardless of the developer machine:
    # no API key from the shell, and no .env/.env.local reload inside the CLI.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(cli_module, "load_local_env", lambda root: None)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["backcasting-run", "--campaign-root", str(tmp_path), "--allow-proxy-seat", "--sample", "2", "--sample-seed", "0"],
    )

    # Without OPENROUTER_API_KEY the live seat construction fails per case,
    # but every sampled case must still be honestly recorded, not dropped.
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["results"]) == 2
    assert all(row["reproduced"] is False for row in payload["results"])
    assert all("seat call failed" in row["detail"] for row in payload["results"])
    assert (tmp_path / "backcasting_resimulation_results.json").exists()


def test_backcasting_run_cli_requires_seat_model_when_live_required(tmp_path: Path, extraction) -> None:
    _write_inputs(tmp_path, extraction)
    runner = CliRunner()

    result = runner.invoke(app, ["backcasting-run", "--campaign-root", str(tmp_path)])

    assert result.exit_code != 0


def test_backcasting_run_cli_rejects_unknown_seat(tmp_path: Path, extraction, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_inputs(tmp_path, extraction)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(cli_module, "load_local_env", lambda root: None)
    runner = CliRunner()

    result = runner.invoke(app, ["backcasting-run", "--campaign-root", str(tmp_path), "--allow-proxy-seat", "--seat", "not-a-seat"])

    assert result.exit_code != 0


def test_backcasting_run_cli_fails_loudly_when_inputs_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(cli_module, "load_local_env", lambda root: None)
    runner = CliRunner()

    result = runner.invoke(app, ["backcasting-run", "--campaign-root", str(tmp_path), "--allow-proxy-seat"])

    assert result.exit_code == 1
    combined = result.output
    try:
        combined += result.stderr
    except (AttributeError, ValueError):  # stderr may not be separately captured
        pass
    assert "backcasting-extract" in combined
    assert not (tmp_path / "backcasting_resimulation_results.json").exists()
