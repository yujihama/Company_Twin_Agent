"""Tests for the WP-14 backcasting LIVE re-simulation runner.

All fixtures here are offline: no LLM/API call is made anywhere in this file.
A fake seat/judge stand in for the live OpenRouter-backed classes, matching
the FakeSeatAgent pattern in tests/conftest.py -- backend is stamped so it
can never be mistaken for readiness-eligible live evidence.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from company_twin.backcasting import extract_backcasting_cases, write_backcasting_inputs, write_backcasting_report
from company_twin.backcasting_run import (
    BACKCASTING_RESULTS_SCHEMA_VERSION,
    JUDGE_PROMPT_VERSION,
    NOT_REPRODUCED,
    READINESS_ALLOWED_JUDGE_BACKENDS,
    REPRODUCED,
    LocalReproductionJudge,
    assert_two_plane_clean,
    backcasting_seat_prompt,
    run_backcasting_resimulation,
    select_backcasting_sample,
)
from company_twin.cli import app
from company_twin.design_loader import load_design


def _design():
    return load_design(Path.cwd())


def _extraction_and_write(tmp_path: Path):
    design = _design()
    extraction = extract_backcasting_cases(design)
    write_backcasting_inputs(tmp_path, extraction)
    return extraction


class FakeSeat:
    """Deterministic offline stand-in for OpenRouterBackcastingSeat."""

    backend = "test-fake"
    model = "fake:unit"

    def __init__(self, *, response_for: dict[str, str] | None = None, fail_for: set[str] | None = None):
        self.response_for = response_for or {}
        self.fail_for = fail_for or set()
        self.seen_prompts: list[str] = []

    def answer(self, prompt: str) -> str:
        self.seen_prompts.append(prompt)
        for marker in self.fail_for:
            if marker in prompt:
                raise RuntimeError(f"simulated seat failure for marker {marker!r}")
        for marker, response in self.response_for.items():
            if marker in prompt:
                return response
        return json.dumps({"likely_reading": "generic", "response": "generic response", "required_approver_or_evidence": "", "cited_doc_ids": []}, ensure_ascii=False)


class FakeJudge:
    backend = "test-fake-judge"
    model = "fake:unit"

    def __init__(self, label: str = REPRODUCED):
        self.label = label
        self.calls: list[dict[str, str]] = []

    def judge(self, *, situation: str, documented_response: str, seat_answer: str) -> dict[str, Any]:
        self.calls.append({"situation": situation, "documented_response": documented_response, "seat_answer": seat_answer})
        return {"label": self.label, "confidence": 0.9, "rationale": "fake judge"}


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


def test_run_backcasting_resimulation_prompt_sent_to_seat_is_two_plane_clean(tmp_path: Path) -> None:
    extraction = _extraction_and_write(tmp_path)
    case = extraction["cases"][0]
    seat = FakeSeat()
    judge = FakeJudge()

    run_backcasting_resimulation(tmp_path, seat=seat, judge=judge, sample_size=1, sample_seed=0)

    assert seat.seen_prompts, "seat should have been invoked"
    for prompt in seat.seen_prompts:
        assert case["case_id"] not in prompt
        assert case["documented_response"] not in prompt or case["documented_response"] == ""
        assert_two_plane_clean(prompt)


# ---------------------------------------------------------------------------
# Honest-fail: failed seat call is recorded, not dropped
# ---------------------------------------------------------------------------


def test_failed_seat_call_recorded_as_not_reproduced_with_detail_not_dropped(tmp_path: Path) -> None:
    extraction = _extraction_and_write(tmp_path)
    sample = select_backcasting_sample(extraction["cases"], sample_size=3, sample_seed=0)
    cases_by_id = {c["case_id"]: c for c in extraction["cases"]}
    failing_case_id = sample["selected_case_ids"][0]
    failing_situation_marker = cases_by_id[failing_case_id]["situation"][:10]

    seat = FakeSeat(fail_for={failing_situation_marker})
    judge = FakeJudge()

    payload = run_backcasting_resimulation(tmp_path, seat=seat, judge=judge, sample_size=3, sample_seed=0)

    assert len(payload["results"]) == 3
    failing_row = next(row for row in payload["results"] if row["case_id"] == failing_case_id)
    assert failing_row["reproduced"] is False
    assert "seat call failed" in failing_row["detail"]
    # Every selected case_id must appear -- nothing silently dropped.
    assert {row["case_id"] for row in payload["results"]} == set(sample["selected_case_ids"])


def test_every_selected_case_appears_in_results_even_when_all_fail(tmp_path: Path) -> None:
    extraction = _extraction_and_write(tmp_path)
    seat = FakeSeat()

    class AlwaysFailSeat:
        backend = "test-fake"
        model = "fake:unit"

        def answer(self, prompt: str) -> str:
            raise RuntimeError("boom")

    payload = run_backcasting_resimulation(tmp_path, seat=AlwaysFailSeat(), judge=FakeJudge(), sample_size=5, sample_seed=3)

    assert len(payload["results"]) == 5
    assert all(row["reproduced"] is False for row in payload["results"])
    assert all("detail" in row and row["detail"] for row in payload["results"])


def test_run_backcasting_resimulation_blocked_gracefully_when_inputs_missing(tmp_path: Path) -> None:
    payload = run_backcasting_resimulation(tmp_path, seat=FakeSeat(), judge=FakeJudge(), sample_size=5, sample_seed=0)

    assert payload["results"] == []
    assert payload["sample"]["sample_size"] == 0
    assert (tmp_path / "backcasting_resimulation_results.json").exists()


# ---------------------------------------------------------------------------
# Proxy judge cannot mark readiness-eligible
# ---------------------------------------------------------------------------


def test_local_reproduction_judge_backend_not_in_readiness_allowlist() -> None:
    judge = LocalReproductionJudge()
    assert judge.backend not in READINESS_ALLOWED_JUDGE_BACKENDS


def test_run_backcasting_resimulation_local_proxy_judge_marks_not_readiness_eligible(tmp_path: Path) -> None:
    extraction = _extraction_and_write(tmp_path)
    seat = FakeSeat()
    judge = LocalReproductionJudge()

    payload = run_backcasting_resimulation(tmp_path, seat=seat, judge=judge, sample_size=3, sample_seed=0)

    assert payload["judge"]["backend"] == "local_reproduction_proxy"
    assert payload["judge"]["readiness_eligible"] is False
    assert payload["judge"]["prompt_version"] == JUDGE_PROMPT_VERSION


def test_run_backcasting_resimulation_records_readiness_eligible_true_for_openrouter_backend(tmp_path: Path) -> None:
    extraction = _extraction_and_write(tmp_path)

    class FakeOpenRouterBackedJudge:
        backend = "openrouter"
        model = "fake-openrouter-model"

        def judge(self, *, situation: str, documented_response: str, seat_answer: str) -> dict[str, Any]:
            return {"label": REPRODUCED, "confidence": 0.8, "rationale": "stubbed openrouter-backend judge for offline test"}

    payload = run_backcasting_resimulation(tmp_path, seat=FakeSeat(), judge=FakeOpenRouterBackedJudge(), sample_size=2, sample_seed=0)

    assert payload["judge"]["readiness_eligible"] is True


# ---------------------------------------------------------------------------
# Report round-trip
# ---------------------------------------------------------------------------


def test_backcasting_run_then_report_round_trip(tmp_path: Path) -> None:
    extraction = _extraction_and_write(tmp_path)
    seat = FakeSeat()
    judge = FakeJudge(label=REPRODUCED)

    run_payload = run_backcasting_resimulation(tmp_path, seat=seat, judge=judge, sample_size=10, sample_seed=0)
    assert run_payload["schema_version"] == BACKCASTING_RESULTS_SCHEMA_VERSION

    report = write_backcasting_report(tmp_path)

    assert report["passed"] is True
    assert report["scoring"]["scored_result_count"] == 10
    assert report["scoring"]["reproduction_rate"] == 1.0


def test_backcasting_run_then_report_round_trip_below_target(tmp_path: Path) -> None:
    extraction = _extraction_and_write(tmp_path)
    seat = FakeSeat()
    judge = FakeJudge(label=NOT_REPRODUCED)

    run_backcasting_resimulation(tmp_path, seat=seat, judge=judge, sample_size=10, sample_seed=0)
    report = write_backcasting_report(tmp_path)

    assert report["passed"] is False
    assert report["scoring"]["reproduction_rate"] == 0.0


def test_backcasting_run_writes_per_case_run_artifacts_under_campaign_root(tmp_path: Path) -> None:
    extraction = _extraction_and_write(tmp_path)
    sample_case_id = select_backcasting_sample(extraction["cases"], sample_size=1, sample_seed=0)["selected_case_ids"][0]

    run_backcasting_resimulation(tmp_path, seat=FakeSeat(), judge=FakeJudge(), sample_size=1, sample_seed=0)

    case_run_root = tmp_path / "backcasting_runs" / sample_case_id
    assert case_run_root.exists()
    assert (case_run_root / "attempts.jsonl").exists()
    assert (case_run_root / "backcasting_case.json").exists()
    attempts_text = (case_run_root / "attempts.jsonl").read_text(encoding="utf-8")
    assert "llm_invoke" in attempts_text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_backcasting_run_cli_offline_with_proxy_seat_flag(tmp_path: Path) -> None:
    _extraction_and_write(tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "backcasting-run",
            "--campaign-root",
            str(tmp_path),
            "--allow-proxy-seat",
            "--sample",
            "2",
            "--sample-seed",
            "0",
        ],
    )

    # Without OPENROUTER_API_KEY this will fail the live seat call itself,
    # but must still honestly record failed rows rather than crash/drop.
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["results"]) == 2
    assert (tmp_path / "backcasting_resimulation_results.json").exists()


def test_backcasting_run_cli_requires_seat_model_when_live_required(tmp_path: Path) -> None:
    _extraction_and_write(tmp_path)
    runner = CliRunner()

    result = runner.invoke(app, ["backcasting-run", "--campaign-root", str(tmp_path)])

    assert result.exit_code != 0
