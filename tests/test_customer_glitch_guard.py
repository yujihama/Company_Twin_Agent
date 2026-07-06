"""Tests for the round-7 blind-SME-review customer-output glitch guard
(data/design/MASTER_DESIGN.md §17.14).

Round 7 flagged two genuine stochastic customer-LLM glitches (distinct from
the earlier language-mixing artifacts §17.5/§17.8 already guard against):

- R-037: a repeated contiguous fragment plus a truncated tail.
- R-038: broken/corrupted text ("進めてよかまだ").

(The third round-7 flag, R-008's "乗換保険", is a miscategorized
FROZEN-CORPUS product term, not a generation artifact -- that fix lives in
sme_blind_review.py and is tested in tests/test_sme_round7_fixes.py, never
here.)

Fixed by extending the existing customer-path-only guard shape
(customer_agent.detect_* + agents.DeepAgentCustomer.__call__ retry wiring,
same pattern as §17.5/§17.8's detect_non_japanese_tokens): deterministic
detectors for a repeated fragment (customer_agent.detect_repeated_fragment,
adapted from the tests' `_has_repeated_run` pattern in
test_probe_stimulus_delivery.py) and a broken/truncated tail
(customer_agent.detect_broken_tail), retried once with a corrective
instruction, keeping the text honestly if still flagged after retry.

All fixtures here are offline: no live LLM/API call is made anywhere in this
file.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from company_twin import agents
from company_twin.customer_agent import (
    detect_broken_tail,
    detect_customer_output_glitch,
    detect_repeated_fragment,
)
from company_twin.recorder import RunRecorder, read_jsonl

# ---------------------------------------------------------------------------
# (a) detect_repeated_fragment
# ---------------------------------------------------------------------------


def test_detect_repeated_fragment_clean_on_natural_japanese() -> None:
    text = "投資信託の申込を進めたいのですが、期日までに間に合いますでしょうか。よろしくお願いします。"
    assert detect_repeated_fragment(text) is False


def test_detect_repeated_fragment_flags_duplicated_clause() -> None:
    # The round-7 R-037 shape: the same clause emitted twice in one utterance.
    text = "担当者の方へのご連絡をお願いしたく存じます担当者の方へのご連絡をお願いしたく存じますので、よろしくお願いします。"
    assert detect_repeated_fragment(text) is True


def test_detect_repeated_fragment_does_not_flag_short_shared_boilerplate() -> None:
    # Ordinary shared closings ("よろしくお願いします。") repeated across two
    # otherwise-distinct sentences must not trip the detector -- it is well
    # under the ~20 char run length.
    text = "投資信託の件、よろしくお願いします。保険の件も、よろしくお願いします。"
    assert detect_repeated_fragment(text) is False


# ---------------------------------------------------------------------------
# (b) detect_broken_tail
# ---------------------------------------------------------------------------


def test_detect_broken_tail_clean_on_complete_sentence() -> None:
    text = "投資信託の申込を進めたいのですが、期日までに間に合いますでしょうか。"
    assert detect_broken_tail(text) is False


def test_detect_broken_tail_flags_missing_terminal_punctuation_with_short_tail() -> None:
    # Ends mid-clause, no sentence-final punctuation, and the trailing clause
    # is short -- the tractable "genuinely truncated" shape.
    text = "投資信託の申込を進めたいのですが、来週まで"
    assert detect_broken_tail(text) is True


def test_detect_broken_tail_does_not_flag_long_informal_trailing_clause() -> None:
    # A long trailing clause with no terminal punctuation is not flagged --
    # conservative, since informal-but-complete phrasing is common and
    # should not churn a retry.
    text = "投資信託の申込を進めたいのですが、来週までにお手続きが完了するかどうか少し不安に思っております"
    assert detect_broken_tail(text) is False


def test_detect_broken_tail_flags_repeated_character_corruption() -> None:
    text = "投資信託の申込をおねがいしまああああす。"
    assert detect_broken_tail(text) is True


def test_detect_broken_tail_flags_isolated_single_hiragana_clause() -> None:
    # The R-038 shape: "進めてよかまだ" reads as corrupted/garbled text ending
    # in a debris-like isolated clause.
    text = "手続きを進めてよろしいでしょうか、た"
    assert detect_broken_tail(text) is True


def test_detect_broken_tail_flags_r038_style_corrupted_text() -> None:
    text = "手続きを進めてよかまだ"
    assert detect_broken_tail(text) is True


# ---------------------------------------------------------------------------
# (c) detect_customer_output_glitch (combined)
# ---------------------------------------------------------------------------


def test_detect_customer_output_glitch_clean_on_natural_japanese() -> None:
    text = "投資信託の申込を進めたいのですが、期日までに間に合いますでしょうか。よろしくお願いします。"
    assert detect_customer_output_glitch(text) == []


def test_detect_customer_output_glitch_flags_repeated_fragment() -> None:
    text = "担当者の方へのご連絡をお願いしたく存じます担当者の方へのご連絡をお願いしたく存じますので、よろしくお願いします。"
    assert "repeated_fragment" in detect_customer_output_glitch(text)


def test_detect_customer_output_glitch_flags_broken_tail() -> None:
    text = "手続きを進めてよかまだ"
    assert "broken_tail" in detect_customer_output_glitch(text)


# ---------------------------------------------------------------------------
# (d) agents.DeepAgentCustomer retry wiring: same shape as the §17.5/§17.8
# language-mixing guard, now also covering glitch detection.
# ---------------------------------------------------------------------------


def test_deepagentcustomer_retries_once_on_repeated_fragment_and_records_both_attempts(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    glitched = "担当者の方へのご連絡をお願いしたく存じます担当者の方へのご連絡をお願いしたく存じますので、よろしくお願いします。"
    clean = "担当者の方へのご連絡をお願いしたく存じますので、よろしくお願いします。"
    responses = [glitched, clean]

    class SequencedAgent:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages, config=None):
            text = responses[min(self.calls, len(responses) - 1)]
            self.calls += 1
            return {"messages": [SimpleNamespace(content=text)]}

    fake_agent = SequencedAgent()
    monkeypatch.setattr(agents, "register_company_twin_profile", lambda: None)
    monkeypatch.setattr(agents, "_chat_model", lambda _model: object())
    monkeypatch.setitem(sys.modules, "deepagents", SimpleNamespace(create_deep_agent=lambda **_kwargs: fake_agent))

    recorder = RunRecorder(tmp_path, "glitch-guard")
    customer_llm = agents.DeepAgentCustomer(model="openrouter:qwen/qwen3.6-flash", recorder=recorder)

    with recorder.origin("customer"):
        result = customer_llm("persona prompt text")

    assert result == clean
    assert fake_agent.calls == 2

    attempts = read_jsonl(tmp_path / "attempts.jsonl")
    invoke_attempts = [row for row in attempts if row["tool"] == "llm_invoke"]
    response_attempts = [row for row in attempts if row["tool"] == "llm_response"]
    assert len(invoke_attempts) == 2
    assert len(response_attempts) == 2


def test_deepagentcustomer_retries_once_on_broken_tail_and_keeps_text_if_still_broken(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    stubborn_text = "手続きを進めてよかまだ"

    class StubbornAgent:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages, config=None):
            self.calls += 1
            return {"messages": [SimpleNamespace(content=stubborn_text)]}

    fake_agent = StubbornAgent()
    monkeypatch.setattr(agents, "register_company_twin_profile", lambda: None)
    monkeypatch.setattr(agents, "_chat_model", lambda _model: object())
    monkeypatch.setitem(sys.modules, "deepagents", SimpleNamespace(create_deep_agent=lambda **_kwargs: fake_agent))

    recorder = RunRecorder(tmp_path, "glitch-guard-stubborn")
    customer_llm = agents.DeepAgentCustomer(model="openrouter:qwen/qwen3.6-flash", recorder=recorder)

    with recorder.origin("customer"):
        result = customer_llm("persona prompt text")

    # Never a silent rewrite: the honest (still-flagged) text is kept, but it
    # was retried exactly once (not looped indefinitely).
    assert result == stubborn_text
    assert fake_agent.calls == 2


def test_deepagentcustomer_does_not_retry_when_clean_of_all_guards(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    clean_text = "投資信託の申込を進めたいです。よろしくお願いします。"

    class CleanAgent:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages, config=None):
            self.calls += 1
            return {"messages": [SimpleNamespace(content=clean_text)]}

    fake_agent = CleanAgent()
    monkeypatch.setattr(agents, "register_company_twin_profile", lambda: None)
    monkeypatch.setattr(agents, "_chat_model", lambda _model: object())
    monkeypatch.setitem(sys.modules, "deepagents", SimpleNamespace(create_deep_agent=lambda **_kwargs: fake_agent))

    recorder = RunRecorder(tmp_path, "glitch-guard-clean")
    customer_llm = agents.DeepAgentCustomer(model="openrouter:qwen/qwen3.6-flash", recorder=recorder)

    with recorder.origin("customer"):
        result = customer_llm("persona prompt text")

    assert result == clean_text
    assert fake_agent.calls == 1


def test_deepagentcustomer_retries_once_when_both_language_and_glitch_guards_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Both guards can fire on the same attempt; only a single retry is ever
    # performed either way.
    both_glitched = "担当者からのご指引をお願いします担当者からのご指引をお願いします。"
    clean = "担当者からのご案内をお願いします。"
    responses = [both_glitched, clean]

    class SequencedAgent:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages, config=None):
            text = responses[min(self.calls, len(responses) - 1)]
            self.calls += 1
            return {"messages": [SimpleNamespace(content=text)]}

    fake_agent = SequencedAgent()
    monkeypatch.setattr(agents, "register_company_twin_profile", lambda: None)
    monkeypatch.setattr(agents, "_chat_model", lambda _model: object())
    monkeypatch.setitem(sys.modules, "deepagents", SimpleNamespace(create_deep_agent=lambda **_kwargs: fake_agent))

    recorder = RunRecorder(tmp_path, "glitch-guard-both")
    customer_llm = agents.DeepAgentCustomer(model="openrouter:qwen/qwen3.6-flash", recorder=recorder)

    with recorder.origin("customer"):
        result = customer_llm("persona prompt text")

    assert result == clean
    assert fake_agent.calls == 2
