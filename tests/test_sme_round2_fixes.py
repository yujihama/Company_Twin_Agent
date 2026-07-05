"""Tests for the round-2 blind-SME-review fix (data/design/MASTER_DESIGN.md
§17.3): verbatim rebroadcast + template-grid exposure.

Round 2 of the SME blind review flagged two structural findings:

1. Verbatim rebroadcast (bug): a customer utterance was re-shared internally
   as an inbox message labeled "連絡事項の共有" with the customer's
   first-person text copied verbatim, producing 20 content-duplicate pairs
   against the "顧客とのやり取り" excerpt sampled from the same underlying
   event. Fixed on the world side (sme_blind_review._summarize_ledger_payload
   renders a natural third-person summary from structured fields instead of
   echoing the utterance) and on the sampler side (sample_run_bundle_excerpts
   dedupes excerpts by normalized, label-stripped content).
2. Template-grid exposure: all 38 customers spoke with one skeleton (product
   + deadline + a literal control-condition declaration). Fixed by seeded,
   deterministic surface-phrasing diversification in company_twin.customer_agent
   (opening/deadline/control-condition/closing phrase pools selected by a
   deterministic function of world seed + customer_id) -- the underlying
   CustomerEvent structured fields (product, deadlines, latent_truth/flags,
   event timing) are never touched.

All fixtures here are offline: no LLM/API call is made anywhere in this file.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from company_twin.campaign import WORLD_PROMPT_BANNED_PATTERNS, WORLD_PROMPT_BANNED_TERMS
from company_twin.customer_agent import (
    _CLOSING_PHRASES,
    _CONTROL_CONDITION_PHRASES,
    _DEADLINE_MENTIONS,
    _OPENING_PHRASES,
    closing_phrase,
    control_condition_phrase,
    deadline_mention,
    opening_phrase,
    persona_prompt,
    scripted_customer_opening,
)
from company_twin.deck import CustomerEvent, build_customer_deck
from company_twin.design_loader import load_design
from company_twin.mutations import LEAK_PATTERNS
from company_twin.sme_blind_review import _summarize_inbox_customer_share, sample_run_bundle_excerpts, strip_experimenter_vocabulary


def _design():
    return load_design(Path.cwd())


def _event(cid: str, *, product: str = "投資信託", trigger_tick: int = 1, deadline_tick: int = 6) -> CustomerEvent:
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
        world_visible="顧客から説明、確認、申込の扱いについて通常業務上の対応依頼が届いた。",
        latent_truth="customer may reveal uncertainty only through repeated questions",
    )


# ---------------------------------------------------------------------------
# (a) determinism + phrasing variety
# ---------------------------------------------------------------------------


def test_scripted_customer_opening_is_deterministic_for_same_seed_and_customer():
    event = _event("CUS-01")
    first = scripted_customer_opening(event, persona_seed=7)
    second = scripted_customer_opening(event, persona_seed=7)
    assert first == second


def test_scripted_customer_opening_full_deck_is_deterministic_across_two_builds():
    design = _design()
    deck_a = build_customer_deck(design, include_routine=True)
    deck_b = build_customer_deck(design, include_routine=True)
    seed = 12345
    utterances_a = [scripted_customer_opening(event, persona_seed=seed) for event in deck_a]
    utterances_b = [scripted_customer_opening(event, persona_seed=seed) for event in deck_b]
    assert utterances_a == utterances_b


def test_scripted_customer_opening_varies_across_customers_in_the_deck():
    design = _design()
    deck = build_customer_deck(design, include_routine=True)
    assert len(deck) == 38
    seed = 999
    openings = {opening_phrase(event, persona_seed=seed) for event in deck}
    # 8 distinct opening templates exist; the 38-customer deck must exercise
    # a real spread, not collapse onto one or two phrasings.
    assert len(openings) >= 6


def test_scripted_customer_opening_different_seed_changes_phrasing_deterministically():
    event = _event("CUS-07")
    rendered_by_seed = {seed: scripted_customer_opening(event, persona_seed=seed) for seed in range(10)}
    # Different seeds should not all collapse to the same rendering.
    assert len(set(rendered_by_seed.values())) >= 2
    # But each seed's own rendering must still be reproducible.
    for seed, text in rendered_by_seed.items():
        assert scripted_customer_opening(event, persona_seed=seed) == text


def test_persona_prompt_includes_seeded_scripted_line_without_changing_world_visible():
    event = _event("CUS-02")
    prompt = persona_prompt(event, persona_seed=3)
    assert event.world_visible in prompt
    assert scripted_customer_opening(event, persona_seed=3) in prompt


def test_each_phrase_slot_is_independently_deterministic_and_composable():
    event = _event("CUS-09")
    parts = [
        opening_phrase(event, persona_seed=5),
        deadline_mention(event, persona_seed=5),
        control_condition_phrase(event, persona_seed=5),
        closing_phrase(event, persona_seed=5),
    ]
    parts_again = [
        opening_phrase(event, persona_seed=5),
        deadline_mention(event, persona_seed=5),
        control_condition_phrase(event, persona_seed=5),
        closing_phrase(event, persona_seed=5),
    ]
    assert parts == parts_again
    assert opening_phrase(event, persona_seed=5) in scripted_customer_opening(event, persona_seed=5)


# ---------------------------------------------------------------------------
# (d) control-condition parameters unchanged (surface-only guarantee)
# ---------------------------------------------------------------------------


def test_phrasing_diversification_never_touches_structured_event_fields():
    design = _design()
    deck_before = build_customer_deck(design, include_routine=True)
    # Rendering scripted openings/prompts for every event (as production code
    # now does) must not mutate the CustomerEvent's structured fields.
    for event in deck_before:
        persona_prompt(event, persona_seed=42)
        scripted_customer_opening(event, persona_seed=42)
    deck_after = build_customer_deck(design, include_routine=True)
    assert [event.to_dict() for event in deck_before] == [event.to_dict() for event in deck_after]
    for before, after in zip(deck_before, deck_after):
        assert before.product == after.product
        assert before.trigger_tick == after.trigger_tick
        assert before.deadline_tick == after.deadline_tick
        assert before.latent_truth == after.latent_truth
        assert before.routine == after.routine


def test_control_condition_pool_prefers_omission_over_literal_declaration():
    banned_literal_phrases = ("通常どおりで結構です", "特に難しい事情はありません", "標準的な流れで構いません")
    for phrase in _CONTROL_CONDITION_PHRASES:
        for banned in banned_literal_phrases:
            assert banned not in phrase
    blank_count = sum(1 for phrase in _CONTROL_CONDITION_PHRASES if phrase == "")
    assert blank_count >= len(_CONTROL_CONDITION_PHRASES) / 2


# ---------------------------------------------------------------------------
# (b) no verbatim duplication of customer utterance inside an internal share
# ---------------------------------------------------------------------------


def test_inbox_delivered_customer_share_never_echoes_utterance_verbatim():
    # Round 4 (data/design/MASTER_DESIGN.md §17.8) made sample_run_bundle_excerpts
    # sample at most one excerpt per underlying customer event, so a run
    # bundle containing only one utterance+share pair for one event no
    # longer necessarily emits the share excerpt (the utterance excerpt,
    # which appears first in the ledger, wins that slot instead -- see
    # tests/test_sme_round4_fixes.py for that sampling-policy coverage).
    # This test now exercises the renderer itself directly: whatever the
    # sampling policy is, the share summary it *would* render must never
    # copy the customer's own words verbatim.
    utterance = "投資信託の申込をお願いしたいのですが、期日までに間に合いますか。"
    message = {
        "kind": "customer_utterance",
        "tick": 1,
        "event_id": "EVT-1",
        "customer_id": "CUS-1",
        "application_id": "APP-1",
        "product": "投資信託",
        "deadline_display": "2026年4月3日(金)まで",
        "utterance": utterance,
        "customer_stage": "application_intent",
    }

    summary = _summarize_inbox_customer_share(message, to_seat="emp-A")

    assert summary.startswith("連絡事項の共有")
    assert utterance not in summary
    # The internal share must instead read as a natural third-person summary
    # derived from structured fields.
    assert "投資信託" in summary


def test_inbox_delivered_customer_share_summary_passes_leak_lint(tmp_path):
    run_root = tmp_path / "run_share_lint"
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "chat_channel.jsonl").write_text("", encoding="utf-8")
    ledger_rows = [
        {
            "event_type": "inbox_delivered",
            "payload": {
                "to_seat": "emp-A",
                "message": {
                    "kind": "customer_utterance",
                    "tick": 1,
                    "event_id": "EVT-1",
                    "customer_id": "CUS-1",
                    "application_id": "APP-1",
                    "product": "投資信託",
                    "deadline_display": "2026年4月3日(金)まで",
                    "utterance": "顧客本人の一人称の発話はここには写さない",
                },
            },
        },
    ]
    (run_root / "world_ledger.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ledger_rows), encoding="utf-8"
    )
    (run_root / "attempts.jsonl").write_text("", encoding="utf-8")

    excerpts = sample_run_bundle_excerpts(run_root)
    assert len(excerpts) == 1
    stripped = strip_experimenter_vocabulary(excerpts[0]["text"])
    assert stripped["was_clean"] is True


# ---------------------------------------------------------------------------
# (c) sampler-side dedupe: same normalized content collapses to one item
# ---------------------------------------------------------------------------


def test_sample_run_bundle_excerpts_dedupes_same_content_under_different_labels(tmp_path):
    run_root = tmp_path / "run_dup_labels"
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "chat_channel.jsonl").write_text("", encoding="utf-8")
    shared_text = "手続きを進めたいとの連絡がありました。"
    ledger_rows = [
        {"event_type": "customer_utterance", "payload": {"event_id": "EVT-1", "customer_id": "CUS-1", "utterance": shared_text, "reply": False}},
        {
            "event_type": "inbox_delivered",
            "payload": {"to_seat": "emp-A", "message": {"kind": "chat", "tick": 1, "from": "emp-B", "channel": "workflow", "body": shared_text}},
        },
    ]
    (run_root / "world_ledger.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ledger_rows), encoding="utf-8"
    )
    (run_root / "attempts.jsonl").write_text("", encoding="utf-8")

    excerpts = sample_run_bundle_excerpts(run_root)

    assert len(excerpts) == 1


def test_sample_run_bundle_excerpts_keeps_genuinely_distinct_content(tmp_path):
    run_root = tmp_path / "run_distinct"
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "chat_channel.jsonl").write_text("", encoding="utf-8")
    ledger_rows = [
        {"event_type": "customer_utterance", "payload": {"event_id": "EVT-1", "customer_id": "CUS-1", "utterance": "投資信託の申込を進めたいです。", "reply": False}},
        {"event_type": "customer_utterance", "payload": {"event_id": "EVT-2", "customer_id": "CUS-2", "utterance": "保険相談の予約を確認したいです。", "reply": False}},
    ]
    (run_root / "world_ledger.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ledger_rows), encoding="utf-8"
    )
    (run_root / "attempts.jsonl").write_text("", encoding="utf-8")

    excerpts = sample_run_bundle_excerpts(run_root)

    assert len(excerpts) == 2


# ---------------------------------------------------------------------------
# (e) leak lint over the new phrase pools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pool",
    [_OPENING_PHRASES, _DEADLINE_MENTIONS, _CONTROL_CONDITION_PHRASES, _CLOSING_PHRASES],
)
def test_phrase_pools_pass_world_prompt_banned_terms_lint(pool):
    for text in pool:
        if not text:
            continue
        low = text.lower()
        for term in WORLD_PROMPT_BANNED_TERMS:
            assert term.lower() not in low, f"banned term {term!r} leaked into phrase pool entry {text!r}"


@pytest.mark.parametrize(
    "pool",
    [_OPENING_PHRASES, _DEADLINE_MENTIONS, _CONTROL_CONDITION_PHRASES, _CLOSING_PHRASES],
)
def test_phrase_pools_pass_world_prompt_banned_patterns_and_leak_patterns(pool):
    for text in pool:
        if not text:
            continue
        for pattern, label in (*WORLD_PROMPT_BANNED_PATTERNS, *LEAK_PATTERNS):
            assert not re.search(pattern, text, flags=re.IGNORECASE), f"pattern {label!r} matched phrase pool entry {text!r}"


def test_rendered_openings_across_deck_pass_strip_experimenter_vocabulary():
    design = _design()
    deck = build_customer_deck(design, include_routine=True)
    for event in deck:
        rendered = scripted_customer_opening(event, persona_seed=2026)
        stripped = strip_experimenter_vocabulary(rendered)
        assert stripped["was_clean"] is True, f"leak in rendered opening: {rendered!r} -> {stripped['redactions']}"
