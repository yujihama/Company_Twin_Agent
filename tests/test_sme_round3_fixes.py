"""Tests for the round-3 blind-SME-review fix (data/design/MASTER_DESIGN.md
§17.5): condition-parameter verbalization + language mixing.

Round 3 of the SME blind review flagged 11/40 records where the customer
narrated their own abstract experiment-condition label out loud ("標準的な条件
で進めていただけますと", "通常の案件となりますので", "通常通りに進めさせて
ください", "標準的な書類等で") -- because persona_prompt/reply_prompt handed the
LLM the abstract label directly (event.world_visible for routine events used
to literally contain "通常案件"), and the model paraphrased that label back as
if a real customer would narrate their own scenario attributes. It also found
a non-Japanese token ("ご指引") inside an otherwise-Japanese utterance.

Fixed by:
1. Rewriting deck.py's world_visible text for routine/default events to
   describe concrete situational facts, never the word "通常" as a
   self-classifying label.
2. Adding an explicit negative instruction (_NEGATIVE_META_LABEL_INSTRUCTION)
   to persona_prompt/reply_prompt naming the exact banned self-labeling
   phrasings, while leaving all CustomerEvent structured parameters
   (product, deadline, latent_truth, flags, timing) byte-identical.
3. A best-effort, deterministic-structure language-mixing guard
   (customer_agent.detect_non_japanese_tokens) wired into
   agents.DeepAgentCustomer.__call__, which retries once through the ordinary
   llm_invoke/llm_response recording path (never a silent rewrite) if a
   non-Japanese token is detected.

All fixtures here are offline: no live LLM/API call is made anywhere in this
file.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from company_twin import agents
from company_twin.campaign import WORLD_PROMPT_BANNED_PATTERNS, WORLD_PROMPT_BANNED_TERMS
from company_twin.customer_agent import (
    _BANNED_META_LABEL_PHRASES,
    _NEGATIVE_META_LABEL_INSTRUCTION,
    detect_non_japanese_tokens,
    persona_prompt,
    reply_prompt,
)
from company_twin.deck import CustomerEvent, build_customer_deck
from company_twin.design_loader import load_design
from company_twin.mutations import LEAK_PATTERNS
from company_twin.recorder import RunRecorder, read_jsonl


def _design():
    return load_design(Path.cwd())


def _event(cid: str, *, product: str = "投資信託", trigger_tick: int = 1, deadline_tick: int = 6, world_visible: str | None = None) -> CustomerEvent:
    return CustomerEvent(
        event_id=f"EVT-{cid}",
        probe_id="P-01",
        customer_id=cid,
        application_id=f"APP-{cid}",
        product=product,
        trigger_tick=trigger_tick,
        deadline_tick=deadline_tick,
        primary_seat="emp-A",
        participant_seats=("emp-A",),
        required_doc_ids=(),
        span_ids=(),
        world_visible=world_visible or "顧客から説明、確認、申込の扱いについて対応の依頼が届いた。",
        latent_truth="customer may reveal uncertainty only through repeated questions",
    )


# ---------------------------------------------------------------------------
# (a) persona_prompt/reply_prompt contain no banned meta-label phrasing, for
# every one of the real 38-event deck's events.
# ---------------------------------------------------------------------------


def _prompt_without_negative_instruction(prompt: str) -> str:
    # The negative instruction necessarily *names* the banned phrases so the
    # LLM knows what to avoid saying -- that quoted listing is expected and
    # is not itself a leak. What must never happen is a banned phrase
    # appearing anywhere else in the prompt (e.g. in the scripted example
    # line or in event.world_visible), which would mean the customer's own
    # "voice" is the one carrying the abstract label.
    return prompt.replace(_NEGATIVE_META_LABEL_INSTRUCTION, "")


def test_persona_prompt_never_contains_banned_meta_label_phrases_for_full_deck():
    design = _design()
    deck = build_customer_deck(design, include_routine=True)
    assert len(deck) == 38
    for event in deck:
        prompt = _prompt_without_negative_instruction(persona_prompt(event, persona_seed=2026))
        for banned in _BANNED_META_LABEL_PHRASES:
            assert banned not in prompt, f"banned phrase {banned!r} leaked into persona_prompt for {event.event_id}"


def test_reply_prompt_never_contains_banned_meta_label_phrases_for_full_deck():
    design = _design()
    deck = build_customer_deck(design, include_routine=True)
    history = [("staff", "ご連絡ありがとうございます。内容を確認しますね。"), ("customer", "よろしくお願いします。")]
    for event in deck:
        prompt = _prompt_without_negative_instruction(reply_prompt(event, history))
        for banned in _BANNED_META_LABEL_PHRASES:
            assert banned not in prompt, f"banned phrase {banned!r} leaked into reply_prompt for {event.event_id}"


def test_deck_world_visible_text_itself_never_contains_banned_meta_label_phrases():
    # The root cause: event.world_visible (embedded verbatim in both prompts)
    # must not itself carry the abstract label -- otherwise a corrective
    # instruction is just papering over a leak still present in the source.
    design = _design()
    deck = build_customer_deck(design, include_routine=True)
    for event in deck:
        for banned in _BANNED_META_LABEL_PHRASES:
            assert banned not in event.world_visible, f"banned phrase {banned!r} in world_visible for {event.event_id}: {event.world_visible!r}"


def test_persona_prompt_includes_the_negative_meta_label_instruction():
    event = _event("CUS-01")
    prompt = persona_prompt(event, persona_seed=1)
    assert _NEGATIVE_META_LABEL_INSTRUCTION in prompt


def test_reply_prompt_includes_the_negative_meta_label_instruction():
    event = _event("CUS-01")
    prompt = reply_prompt(event, [("staff", "ご確認しました。")])
    assert _NEGATIVE_META_LABEL_INSTRUCTION in prompt


# ---------------------------------------------------------------------------
# (b) structured event parameters unchanged (byte-identical deck before/after)
# -- same test pattern as tests/test_sme_round2_fixes.py
# (test_phrasing_diversification_never_touches_structured_event_fields).
# ---------------------------------------------------------------------------


def test_meta_label_fix_never_touches_structured_event_fields():
    design = _design()
    deck_before = build_customer_deck(design, include_routine=True)
    for event in deck_before:
        persona_prompt(event, persona_seed=42)
        reply_prompt(event, [("staff", "確認しました。")])
    deck_after = build_customer_deck(design, include_routine=True)
    assert [event.to_dict() for event in deck_before] == [event.to_dict() for event in deck_after]
    for before, after in zip(deck_before, deck_after):
        assert before.product == after.product
        assert before.trigger_tick == after.trigger_tick
        assert before.deadline_tick == after.deadline_tick
        assert before.latent_truth == after.latent_truth
        assert before.routine == after.routine


# ---------------------------------------------------------------------------
# (c) the negative-instruction list itself passes the world leak lint.
# ---------------------------------------------------------------------------


def test_negative_meta_label_instruction_passes_world_leak_lint():
    low = _NEGATIVE_META_LABEL_INSTRUCTION.lower()
    for term in WORLD_PROMPT_BANNED_TERMS:
        assert term.lower() not in low, f"banned term {term!r} leaked into negative instruction"
    for pattern, label in (*WORLD_PROMPT_BANNED_PATTERNS, *LEAK_PATTERNS):
        assert not re.search(pattern, _NEGATIVE_META_LABEL_INSTRUCTION, flags=re.IGNORECASE), f"pattern {label!r} matched negative instruction"


def test_banned_meta_label_phrases_pool_passes_world_leak_lint():
    for phrase in _BANNED_META_LABEL_PHRASES:
        low = phrase.lower()
        for term in WORLD_PROMPT_BANNED_TERMS:
            assert term.lower() not in low
        for pattern, label in (*WORLD_PROMPT_BANNED_PATTERNS, *LEAK_PATTERNS):
            assert not re.search(pattern, phrase, flags=re.IGNORECASE), f"pattern {label!r} matched banned phrase {phrase!r}"


# ---------------------------------------------------------------------------
# (d) language-mixing detector: pure function behavior.
# ---------------------------------------------------------------------------


def test_detect_non_japanese_tokens_flags_the_round3_observed_token():
    hits = detect_non_japanese_tokens("担当者からのご指引に従って手続きします。")
    assert "ご指引" in hits


def test_detect_non_japanese_tokens_clean_on_natural_japanese():
    hits = detect_non_japanese_tokens("投資信託の申込を進めたいのですが、期日までに間に合いますか。")
    assert hits == []


def test_detect_non_japanese_tokens_flags_simplified_chinese_only_characters():
    hits = detect_non_japanese_tokens("这个手续をお願いします。")
    assert hits


# ---------------------------------------------------------------------------
# (e) mixing-guard retry path recorded: a fixture LLM that returns a Chinese
# token once then Japanese must show both attempts in attempts.jsonl.
# ---------------------------------------------------------------------------


def test_deepagentcustomer_retries_once_on_non_japanese_token_and_records_both_attempts(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    responses = ["担当者からのご指引をお願いします。", "担当者からのご案内をお願いします。"]

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

    recorder = RunRecorder(tmp_path, "mixing-guard")
    customer_llm = agents.DeepAgentCustomer(model="openrouter:qwen/qwen3.6-flash", recorder=recorder)

    with recorder.origin("customer"):
        result = customer_llm("persona prompt text")

    assert result == "担当者からのご案内をお願いします。"
    assert fake_agent.calls == 2

    attempts = read_jsonl(tmp_path / "attempts.jsonl")
    invoke_attempts = [row for row in attempts if row["tool"] == "llm_invoke"]
    response_attempts = [row for row in attempts if row["tool"] == "llm_response"]
    # Both the original and the retry are ordinary recorded attempts -- an
    # honest, auditable record rather than a silent rewrite.
    assert len(invoke_attempts) == 2
    assert len(response_attempts) == 2
    assert response_attempts[0]["result"]["response_chars"] == len(responses[0])
    assert response_attempts[1]["result"]["response_chars"] == len(responses[1])


def test_deepagentcustomer_keeps_text_if_non_japanese_persists_after_retry(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    stubborn_text = "担当者からのご指引をお願いします。"

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

    recorder = RunRecorder(tmp_path, "mixing-guard-stubborn")
    customer_llm = agents.DeepAgentCustomer(model="openrouter:qwen/qwen3.6-flash", recorder=recorder)

    with recorder.origin("customer"):
        result = customer_llm("persona prompt text")

    # Never a silent rewrite: the honest (still-flagged) text is kept, but it
    # was retried exactly once (not looped indefinitely).
    assert result == stubborn_text
    assert fake_agent.calls == 2


def test_deepagentcustomer_does_not_retry_when_clean_on_first_try(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
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

    recorder = RunRecorder(tmp_path, "mixing-guard-clean")
    customer_llm = agents.DeepAgentCustomer(model="openrouter:qwen/qwen3.6-flash", recorder=recorder)

    with recorder.origin("customer"):
        result = customer_llm("persona prompt text")

    assert result == clean_text
    assert fake_agent.calls == 1
