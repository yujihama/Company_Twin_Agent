"""Tests for the round-4 blind-SME-review fixes (data/design/MASTER_DESIGN.md
§17.8).

Round 4 flagged three findings:

1. Content-fidelity bug + template uniformity: every internal-share memo
   ("連絡事項の共有") rendered from the identical single skeleton
   "お客様より{product}の申込希望あり。期日は{date}。" -- differing only in
   product/date substitutions (20 identical-skeleton memos), AND asserting
   "申込希望あり" (application request) even for a customer whose event was
   only at the consultation/hesitation stage. Fixed by (a) giving
   CustomerEvent a genuine `customer_stage` field (deck.py), driving both the
   customer's own world_visible text and the memo renderer, and (b) a seeded
   pool of several skeletons per stage in
   sme_blind_review._summarize_inbox_customer_share.
2. The sampler still paired each customer utterance with its own inbox-share
   memo about the same underlying event. Fixed by making
   sample_run_bundle_excerpts sample at most one excerpt per underlying
   customer event_id, backfilling freed slots with other available excerpts.
3. The language-mixing guard (customer_agent.detect_non_japanese_tokens)
   missed Latin-script mixing ("お Busy だと思いますが" in a customer
   utterance). Extended to flag standalone Latin words, with an
   evidence-based allowlist for legitimate business Latin already used in
   this world's own corpus/role-card text.

All fixtures here are offline: no LLM/API call is made anywhere in this file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import re

from company_twin.campaign import WORLD_PROMPT_BANNED_PATTERNS, WORLD_PROMPT_BANNED_TERMS
from company_twin.customer_agent import (
    _LATIN_TOKEN_ALLOWLIST,
    detect_non_japanese_tokens,
    world_visible_message,
)
from company_twin.deck import (
    CustomerEvent,
    _PROBE_STAGE_OVERRIDES,
    _ROUTINE_WORLD_VISIBLE_BY_STAGE,
    _probe_stage,
    _seeded_stage,
    build_customer_deck,
)
from company_twin.design_loader import load_design
from company_twin.kernel import FORBIDDEN_INBOX_KEYS, INBOX_ALLOWED_KEYS, validate_inbox_message
from company_twin.mutations import LEAK_PATTERNS
from company_twin.sme_blind_review import (
    _SHARE_MEMO_SKELETONS_BY_STAGE,
    _SHARE_MEMO_SKELETONS_NO_PRODUCT,
    _summarize_inbox_customer_share,
    sample_run_bundle_excerpts,
    strip_experimenter_vocabulary,
)


def _design():
    return load_design(Path.cwd())


def _event(cid: str, *, product: str = "投資信託", stage: str = "application_intent", trigger_tick: int = 1, deadline_tick: int = 6) -> CustomerEvent:
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
        world_visible="顧客から説明、確認、申込の扱いについて対応の依頼が届いた。",
        latent_truth="customer may reveal uncertainty only through repeated questions",
        customer_stage=stage,
    )


# ---------------------------------------------------------------------------
# (1a) stage-aware memo rendering: a consultation-stage event is never
# rendered as an application request.
# ---------------------------------------------------------------------------


def _share_message(event: CustomerEvent, *, to_seat: str = "emp-A") -> dict:
    return world_visible_message(event, tick=1, utterance="（顧客の発話はここでは使わない）")


def test_consultation_stage_memo_never_asserts_application_request():
    event = _event("CUS-01", product="投資信託", stage="consultation")
    message = _share_message(event)
    summary = _summarize_inbox_customer_share(message, to_seat="emp-A")
    assert "申込希望" not in summary
    assert "ご相談" in summary or "検討" in summary or "聞きたい" in summary or "申し出" in summary


def test_application_intent_stage_memo_asserts_application_request():
    event = _event("CUS-02", product="保険相談", stage="application_intent")
    message = _share_message(event)
    summary = _summarize_inbox_customer_share(message, to_seat="emp-A")
    assert "申込" in summary


def test_procedural_request_stage_memo_never_asserts_application_request():
    event = _event("CUS-03", product="銀行口座", stage="procedural_request")
    message = _share_message(event)
    summary = _summarize_inbox_customer_share(message, to_seat="emp-A")
    assert "申込希望" not in summary
    assert "確認" in summary


def test_full_deck_consultation_events_never_render_as_application_request():
    design = _design()
    deck = build_customer_deck(design, include_routine=True)
    consultation_events = [event for event in deck if event.customer_stage == "consultation"]
    assert consultation_events, "expected at least one consultation-stage event in the full deck"
    for event in consultation_events:
        message = world_visible_message(event, tick=event.trigger_tick, utterance="（発話本文はここでは使わない）")
        summary = _summarize_inbox_customer_share(message, to_seat=event.primary_seat)
        assert "申込希望" not in summary, f"{event.event_id} is consultation-stage but memo asserts an application request: {summary!r}"


def test_full_deck_has_a_real_spread_of_customer_stages():
    # The whole point of the fix: not every routine customer is
    # "application_intent" -- the deck must show all three stages.
    design = _design()
    deck = build_customer_deck(design, include_routine=True)
    stages = {event.customer_stage for event in deck}
    assert stages == {"consultation", "application_intent", "procedural_request"}


# ---------------------------------------------------------------------------
# (1b) memo skeleton variation across seats/events: at least 3 distinct
# skeletons over the deck, with byte-identical structured events before/after.
# ---------------------------------------------------------------------------


def test_share_memo_skeleton_varies_across_the_deck():
    design = _design()
    deck = build_customer_deck(design, include_routine=True)
    summaries = set()
    for event in deck:
        message = world_visible_message(event, tick=event.trigger_tick, utterance="（発話本文はここでは使わない）")
        summary = _summarize_inbox_customer_share(message, to_seat=event.primary_seat)
        # Strip the trailing deadline sentence so we're comparing skeletons,
        # not skeleton+date combinations (which would trivially vary).
        skeleton = summary.split("期日は")[0]
        summaries.add(skeleton)
    assert len(summaries) >= 3, f"expected at least 3 distinct memo skeletons over the deck, got {len(summaries)}: {summaries}"


def test_share_memo_is_deterministic_for_the_same_event_and_seat():
    event = _event("CUS-04", stage="application_intent")
    message = _share_message(event)
    first = _summarize_inbox_customer_share(message, to_seat="emp-A")
    second = _summarize_inbox_customer_share(message, to_seat="emp-A")
    assert first == second


def test_share_memo_rendering_never_touches_structured_event_fields():
    design = _design()
    deck_before = build_customer_deck(design, include_routine=True)
    for event in deck_before:
        message = world_visible_message(event, tick=event.trigger_tick, utterance="x")
        _summarize_inbox_customer_share(message, to_seat=event.primary_seat)
    deck_after = build_customer_deck(design, include_routine=True)
    assert [event.to_dict() for event in deck_before] == [event.to_dict() for event in deck_after]


def test_probe_events_are_classified_application_intent_or_procedural_request_never_consultation():
    design = _design()
    for probe_id in design.probes:
        stage = _probe_stage(probe_id)
        assert stage in {"application_intent", "procedural_request"}


def test_seeded_stage_is_deterministic():
    assert _seeded_stage("EVT-R01") == _seeded_stage("EVT-R01")


# ---------------------------------------------------------------------------
# customer_stage is whitelisted world-visible content, never forbidden
# routing metadata (primary_seat stays forbidden).
# ---------------------------------------------------------------------------


def test_world_visible_message_carries_customer_stage_and_passes_inbox_validation():
    event = _event("CUS-05", stage="consultation")
    message = world_visible_message(event, tick=1, utterance="投資信託について少し伺いたいのですが。")
    assert message["customer_stage"] == "consultation"
    assert "primary_seat" not in message
    validate_inbox_message(message)  # must not raise


def test_customer_stage_is_whitelisted_and_primary_seat_stays_forbidden():
    assert "customer_stage" in INBOX_ALLOWED_KEYS["customer_utterance"]
    assert "primary_seat" in FORBIDDEN_INBOX_KEYS


# ---------------------------------------------------------------------------
# (2) sampler one-per-event: a fixture with an utterance+share pair from the
# same event yields exactly one sampled excerpt for that event.
# ---------------------------------------------------------------------------


def _write_bundle(tmp_path: Path, ledger_rows: list[dict]) -> Path:
    run_root = tmp_path / "run"
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "chat_channel.jsonl").write_text("", encoding="utf-8")
    (run_root / "world_ledger.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ledger_rows), encoding="utf-8"
    )
    (run_root / "attempts.jsonl").write_text("", encoding="utf-8")
    return run_root


def _utterance_and_share_rows(event_id: str, customer_id: str, *, product: str = "投資信託", stage: str = "application_intent") -> list[dict]:
    utterance_text = f"{product}の件でご連絡しました（{event_id}）。"
    return [
        {"event_type": "customer_utterance", "payload": {"event_id": event_id, "customer_id": customer_id, "utterance": utterance_text, "reply": False}},
        {
            "event_type": "inbox_delivered",
            "payload": {
                "to_seat": "emp-A",
                "message": {
                    "kind": "customer_utterance",
                    "tick": 1,
                    "event_id": event_id,
                    "customer_id": customer_id,
                    "application_id": f"APP-{customer_id}",
                    "product": product,
                    "deadline_display": "2026年4月3日(金)まで",
                    "utterance": utterance_text,
                    "customer_stage": stage,
                },
            },
        },
    ]


def test_sampler_yields_exactly_one_excerpt_per_utterance_share_pair(tmp_path):
    run_root = _write_bundle(tmp_path, _utterance_and_share_rows("EVT-1", "CUS-1"))

    excerpts = sample_run_bundle_excerpts(run_root)

    assert len(excerpts) == 1


def test_sampler_never_emits_both_utterance_and_share_for_the_same_event(tmp_path):
    rows = []
    for idx in range(1, 6):
        rows.extend(_utterance_and_share_rows(f"EVT-{idx}", f"CUS-{idx}"))
    run_root = _write_bundle(tmp_path, rows)

    excerpts = sample_run_bundle_excerpts(run_root, limit=20)

    # 5 independent customer events -> exactly 5 excerpts, never 10.
    assert len(excerpts) == 5


def test_sampler_backfills_freed_slots_with_other_available_excerpts(tmp_path):
    rows = _utterance_and_share_rows("EVT-1", "CUS-1")
    # A second, unrelated event with only a chat message (no linkage at all)
    # to backfill with once EVT-1's pair collapses to one excerpt.
    rows.append({"event_type": "month_end_close", "payload": {"body": "月次締め処理を完了しました。"}})
    run_root = _write_bundle(tmp_path, rows)

    excerpts = sample_run_bundle_excerpts(run_root, limit=10)

    kinds_text = [excerpt["text"] for excerpt in excerpts]
    assert len(excerpts) == 2
    assert any("月次締め" in text for text in kinds_text)


def test_sampler_still_dedupes_multiple_utterances_for_the_same_event_including_replies(tmp_path):
    # A reply shares the same event_id as the initial utterance -- both must
    # collapse to at most one sampled excerpt for that event.
    rows = [
        {"event_type": "customer_utterance", "payload": {"event_id": "EVT-1", "customer_id": "CUS-1", "utterance": "最初の発話です。", "reply": False}},
        {"event_type": "customer_utterance", "payload": {"event_id": "EVT-1", "customer_id": "CUS-1", "utterance": "返信の発話です。", "reply": True}},
    ]
    run_root = _write_bundle(tmp_path, rows)

    excerpts = sample_run_bundle_excerpts(run_root, limit=10)

    assert len(excerpts) == 1


# ---------------------------------------------------------------------------
# (3) Latin-mixing detector: flags "お Busy", passes DFH-SAL-001/eKYC/etc.
# ---------------------------------------------------------------------------


def test_detect_non_japanese_tokens_flags_standalone_latin_word():
    hits = detect_non_japanese_tokens("お Busy だと思いますが、よろしくお願いします。")
    assert "Busy" in hits


@pytest.mark.parametrize("allowed_token", sorted(_LATIN_TOKEN_ALLOWLIST))
def test_detect_non_japanese_tokens_passes_allowlisted_business_terms(allowed_token):
    hits = detect_non_japanese_tokens(f"{allowed_token}の件について確認をお願いします。")
    assert hits == []


def test_detect_non_japanese_tokens_passes_doc_ids():
    hits = detect_non_japanese_tokens("DFH-SAL-001の内容を確認しました。")
    assert hits == []
    hits = detect_non_japanese_tokens("DFH-SAL-021とDFH-SAL-048を参照しました。")
    assert hits == []


def test_detect_non_japanese_tokens_still_flags_previously_known_signals():
    # Round-3 regressions guard: existing detections must be unaffected.
    hits = detect_non_japanese_tokens("担当者からのご指引に従って手続きします。")
    assert "ご指引" in hits
    hits = detect_non_japanese_tokens("这个手続をお願いします。")
    assert hits


def test_detect_non_japanese_tokens_clean_on_natural_japanese_with_allowlisted_terms():
    hits = detect_non_japanese_tokens("eKYCの確認とCRMへの登録、FAQの案内をお願いします。KPIの件も確認します。")
    assert hits == []


def test_detect_non_japanese_tokens_flags_unlisted_latin_word_even_when_short():
    hits = detect_non_japanese_tokens("ちょっとOKですか。")
    assert "OK" in hits


# ---------------------------------------------------------------------------
# Leak lint coverage over the new content pools (same test pattern as
# tests/test_sme_round2_fixes.py / test_sme_round3_fixes.py).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", sorted(_SHARE_MEMO_SKELETONS_BY_STAGE))
def test_share_memo_skeletons_pass_world_prompt_banned_terms_lint(stage):
    for text in (*_SHARE_MEMO_SKELETONS_BY_STAGE[stage], *_SHARE_MEMO_SKELETONS_NO_PRODUCT[stage]):
        low = text.lower()
        for term in WORLD_PROMPT_BANNED_TERMS:
            assert term.lower() not in low, f"banned term {term!r} leaked into share memo skeleton {text!r}"


@pytest.mark.parametrize("stage", sorted(_SHARE_MEMO_SKELETONS_BY_STAGE))
def test_share_memo_skeletons_pass_world_prompt_banned_patterns_and_leak_patterns(stage):
    for text in (*_SHARE_MEMO_SKELETONS_BY_STAGE[stage], *_SHARE_MEMO_SKELETONS_NO_PRODUCT[stage]):
        for pattern, label in (*WORLD_PROMPT_BANNED_PATTERNS, *LEAK_PATTERNS):
            assert not re.search(pattern, text, flags=re.IGNORECASE), f"pattern {label!r} matched share memo skeleton {text!r}"


def test_routine_world_visible_by_stage_texts_pass_world_leak_lint():
    for stage, template in _ROUTINE_WORLD_VISIBLE_BY_STAGE.items():
        rendered = template.format(product="投資信託")
        stripped = strip_experimenter_vocabulary(rendered)
        assert stripped["was_clean"] is True, f"leak in routine world_visible for stage {stage!r}: {rendered!r} -> {stripped['redactions']}"


def test_rendered_share_memos_across_deck_pass_strip_experimenter_vocabulary():
    design = _design()
    deck = build_customer_deck(design, include_routine=True)
    for event in deck:
        message = world_visible_message(event, tick=event.trigger_tick, utterance="x")
        summary = _summarize_inbox_customer_share(message, to_seat=event.primary_seat)
        if not summary:
            continue
        stripped = strip_experimenter_vocabulary(summary)
        assert stripped["was_clean"] is True, f"leak in rendered share memo for {event.event_id}: {summary!r} -> {stripped['redactions']}"


def test_probe_stage_overrides_are_never_consultation():
    for probe_id, stage in _PROBE_STAGE_OVERRIDES.items():
        assert stage != "consultation", f"{probe_id} override must not be consultation (probes are fixed, already-committed scenarios)"
