"""Tests for the Option-A world-quality fix after the blind SME review.

data/design/MASTER_DESIGN.md §17 follow-up (2026-07-05): a blind SME review of
S2 world records failed honestly (11/39 pass, 25/39 flagged "artificial
markers"). This fixes the WORLD so records genuinely read as natural business
documents, structurally where possible:

(a) tick -> date rendering (company_twin.world_calendar)
(b) display-name mapping determinism + no "emp-" pattern in rendered
    world-visible text (company_twin.identity, company_twin.harness._turn_prompt)
(c) the new diegetic record-writing-standard corpus document passes the
    existing world-surface leak lint
(d) the SME sampler's detection patterns catch tick/emp- id leaks, extending
    tests/test_wp14_calibration.py's strip_experimenter_vocabulary coverage

All fixtures here are offline: no LLM/API call is made anywhere in this file.
"""
from __future__ import annotations

import re
from datetime import date

import pytest

from company_twin.campaign import _world_prompt_leak_failures, static_world_surface_lint
from company_twin.corpus import RECORD_STANDARD_DOC_ID, RECORD_STANDARD_TEXT, Corpus
from company_twin.design_loader import load_design
from company_twin.harness import _turn_prompt
from company_twin.identity import display_name_for_seat, display_names_for_seats, render_seat_reference
from company_twin.sme_blind_review import strip_experimenter_vocabulary
from company_twin.world_calendar import render_deadline_date, render_tick_as_date, tick_to_world_date

from pathlib import Path


def _design():
    return load_design(Path.cwd())


# ---------------------------------------------------------------------------
# (a) tick -> date rendering
# ---------------------------------------------------------------------------


def test_tick_to_world_date_epoch_is_2026_04_01_am():
    world_date = tick_to_world_date(1)

    assert world_date.calendar_date == date(2026, 4, 1)
    assert world_date.half_day == "午前"
    assert world_date.display() == "2026年4月1日(水)午前"


def test_tick_to_world_date_advances_half_day_then_next_business_day():
    tick2 = tick_to_world_date(2)
    tick3 = tick_to_world_date(3)

    assert tick2.calendar_date == date(2026, 4, 1)
    assert tick2.half_day == "午後"
    assert tick3.calendar_date == date(2026, 4, 2)
    assert tick3.half_day == "午前"


def test_tick_to_world_date_skips_weekends():
    # 2026-04-01 is a Wednesday; walking forward business half-days must never
    # land on a Saturday/Sunday.
    for tick in range(1, 60):
        world_date = tick_to_world_date(tick)
        assert world_date.calendar_date.weekday() < 5, f"tick {tick} landed on a weekend: {world_date.calendar_date}"


def test_tick_to_world_date_is_deterministic_and_monotonic():
    dates = [tick_to_world_date(tick).calendar_date for tick in range(1, 41)]
    # non-decreasing calendar date as tick advances
    assert dates == sorted(dates)
    # re-computing must be pure/deterministic
    assert [tick_to_world_date(tick).calendar_date for tick in range(1, 41)] == dates


def test_render_tick_as_date_never_contains_the_word_tick():
    for tick in (1, 2, 3, 10, 23, 40):
        rendered = render_tick_as_date(tick)
        assert "tick" not in rendered.lower()
        assert "ティック" not in rendered


def test_render_deadline_date_is_a_calendar_date_not_a_business_day_count():
    rendered = render_deadline_date(6)
    assert re.search(r"\d{4}年\d{1,2}月\d{1,2}日", rendered)
    assert "営業日" not in rendered
    assert "約" not in rendered


def test_deadline_display_never_uses_template_parameter_phrasing():
    from company_twin.customer_agent import deadline_display

    for now_tick, deadline_tick in ((1, 1), (1, 6), (1, 20), (5, 40)):
        rendered = deadline_display(now_tick, deadline_tick)
        assert "営業日" not in rendered, rendered
        assert not re.search(r"約\d", rendered), rendered


# ---------------------------------------------------------------------------
# (b) display-name mapping determinism + no "emp-" pattern in rendered text
# ---------------------------------------------------------------------------


def test_display_name_for_seat_is_deterministic_across_calls():
    for seat_id in ("emp-A", "emp-B", "emp-F", "emp-G", "emp-C", "emp-M", "emp-Q"):
        first = display_name_for_seat(seat_id)
        second = display_name_for_seat(seat_id)
        assert first == second
        assert first.strip() == first
        assert " " in first  # "department surname" shape


def test_display_name_for_seat_has_no_collisions_across_dfh_roster():
    seats = ("emp-A", "emp-B", "emp-F", "emp-G", "emp-C", "emp-M", "emp-Q", "audit-in-world")
    names = display_names_for_seats(list(seats))
    assert len(set(names.values())) == len(seats)


def test_display_name_for_seat_never_contains_raw_seat_id_pattern():
    for seat_id in ("emp-A", "emp-B", "emp-F", "emp-G", "emp-C", "emp-M", "emp-Q", "audit-in-world", "emp-Z"):
        name = display_name_for_seat(seat_id)
        assert not re.search(r"\bemp-[A-Za-z]\b", name)


def test_render_seat_reference_reads_as_natural_prose():
    rendered = render_seat_reference("emp-M", honorific="さん")
    assert rendered == "営業管理部の渡辺さん"
    assert "emp-" not in rendered


def test_turn_prompt_never_leaks_tick_or_symbolic_seat_ids():
    messages = [
        {
            "kind": "customer_utterance",
            "tick": 1,
            "event_id": "EVT-LINT",
            "customer_id": "CUS-LINT",
            "application_id": "APP-LINT",
            "product": "商品",
            "deadline_display": "本日中",
            "utterance": "手続きの進め方を確認したいです。",
        },
        {"kind": "chat", "tick": 2, "from": "emp-B", "channel": "workflow", "body": "APP-LINT の確認をお願いします"},
        {"kind": "timed_notice", "tick": 2, "notice": "approval_deadline_overrun", "detail": "承認が期限を超えています。"},
    ]

    prompt = _turn_prompt(tick=2, ticks=6, budget_left=5, messages=messages)

    assert "tick" not in prompt.lower()
    assert "ティック" not in prompt
    assert not re.search(r"\bemp-[A-Za-z]\b", prompt)
    assert not re.search(r"\bemp-[A-Za-z]emp-[A-Za-z]\b", prompt)
    # the rendered date must actually appear (diegetic, not merely absent-tick)
    assert re.search(r"\d{4}年\d{1,2}月\d{1,2}日", prompt)


def test_turn_prompt_renders_chat_sender_as_display_name():
    messages = [{"kind": "chat", "tick": 1, "from": "emp-M", "channel": "workflow", "body": "ご確認ください。"}]

    prompt = _turn_prompt(tick=1, ticks=6, budget_left=5, messages=messages)

    assert display_name_for_seat("emp-M") in prompt


def test_role_system_prompt_uses_display_name_not_raw_seat_id():
    from company_twin.agents import role_system_prompt

    prompt = role_system_prompt("emp-A", "sales")

    assert display_name_for_seat("emp-A") in prompt
    assert not re.search(r"\bemp-[A-Za-z]\b", prompt)


# ---------------------------------------------------------------------------
# (c) the new corpus document passes the world-surface leak lint
# ---------------------------------------------------------------------------


def test_record_standard_document_is_present_and_visible_to_all_roles():
    design = _design()
    corpus = Corpus.from_design(design)

    assert RECORD_STANDARD_DOC_ID in corpus.documents
    doc = corpus.get(RECORD_STANDARD_DOC_ID)
    for role in ("sales", "manager", "application", "second_line", "audit"):
        assert corpus.readable_by(RECORD_STANDARD_DOC_ID, role), role
    assert doc.text  # real body text, not a stub


def test_record_standard_document_passes_world_prompt_leak_lint():
    failures = _world_prompt_leak_failures("record_standard_doc", RECORD_STANDARD_TEXT)
    assert failures == []


def test_record_standard_document_contains_no_symbolic_ids_or_tick_vocabulary():
    assert not re.search(r"\bemp-[A-Za-z]\b", RECORD_STANDARD_TEXT)
    assert "tick" not in RECORD_STANDARD_TEXT.lower()
    assert "ティック" not in RECORD_STANDARD_TEXT


def test_static_world_surface_lint_still_passes_with_record_standard_document():
    design = _design()
    result = static_world_surface_lint(design)
    assert result["passed"] is True, result["failures"]


def test_record_standard_document_does_not_change_design_document_manifest_count():
    # The manifest-tracked count (design.documents, asserted == 50 elsewhere)
    # must stay untouched; the new doc is injected at the Corpus layer only,
    # the same layer that already synthesizes the @v1.0 stale mirrors.
    design = _design()
    assert len(design.documents) == 50


# ---------------------------------------------------------------------------
# (d) SME sampler: detection patterns for tick/emp- ids, and the empty
# formulaic-entry fix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leaky_text",
    [
        "本日は第3ティックです。",
        "emp-Bさんに確認します。",
        "担当のemp-M様へ確認しました。",
        "emp-Wemp-Hへ引き継ぎました。",
        "tick2の時点で処理済みです。",
    ],
)
def test_strip_experimenter_vocabulary_flags_tick_and_symbolic_seat_ids(leaky_text: str):
    stripped = strip_experimenter_vocabulary(leaky_text)

    assert stripped["was_clean"] is False
    assert stripped["redactions"]
    assert "emp-" not in stripped["text"]
    assert "tick" not in stripped["text"].lower()
    assert "ティック" not in stripped["text"]


def test_strip_experimenter_vocabulary_leaves_natural_business_text_with_display_names_untouched():
    text = "営業部の佐藤が2026年4月1日午前に顧客対応記録を残した。"
    stripped = strip_experimenter_vocabulary(text)

    assert stripped["was_clean"] is True
    assert stripped["text"] == text


def test_sample_run_bundle_excerpts_drops_repeated_empty_inbox_delivered_boilerplate(tmp_path):
    from company_twin.sme_blind_review import sample_run_bundle_excerpts

    run_root = tmp_path / "run_empty_notices"
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "chat_channel.jsonl").write_text("", encoding="utf-8")
    ledger_rows = [
        {"event_type": "inbox_delivered", "payload": {"to_seat": "emp-A", "message": {"kind": "timed_notice"}}},
        {"event_type": "inbox_delivered", "payload": {"to_seat": "emp-B", "message": {"kind": "timed_notice"}}},
        {"event_type": "inbox_delivered", "payload": {"to_seat": "emp-C", "message": {"kind": "timed_notice"}}},
    ]
    (run_root / "world_ledger.jsonl").write_text(
        "".join(__import__("json").dumps(row, ensure_ascii=False) + "\n" for row in ledger_rows), encoding="utf-8"
    )
    (run_root / "attempts.jsonl").write_text("", encoding="utf-8")

    excerpts = sample_run_bundle_excerpts(run_root)

    # Previously this produced three identical, contentless "連絡事項の共有"
    # entries (a blind SME review flagged this as an artificial marker); now
    # bare-boilerplate rows are dropped instead of repeated.
    assert excerpts == []


def test_sample_run_bundle_excerpts_keeps_inbox_delivered_when_message_has_real_content(tmp_path):
    from company_twin.sme_blind_review import sample_run_bundle_excerpts

    run_root = tmp_path / "run_real_notices"
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "chat_channel.jsonl").write_text("", encoding="utf-8")
    ledger_rows = [
        {
            "event_type": "inbox_delivered",
            "payload": {"to_seat": "emp-A", "message": {"kind": "timed_notice", "detail": "承認期限を超過しています。"}},
        },
    ]
    (run_root / "world_ledger.jsonl").write_text(
        "".join(__import__("json").dumps(row, ensure_ascii=False) + "\n" for row in ledger_rows), encoding="utf-8"
    )
    (run_root / "attempts.jsonl").write_text("", encoding="utf-8")

    excerpts = sample_run_bundle_excerpts(run_root)

    assert len(excerpts) == 1
    assert "承認期限を超過しています" in excerpts[0]["text"]


def test_kernel_timed_notice_details_are_diegetic_japanese() -> None:
    # Round-6 blind review leak: kernel-authored world-visible notice details
    # were hardcoded English with experimenter vocabulary ("Campaign deadline
    # reached...", "... overdue since tick N"). World-visible notice text must
    # pass the same leak discipline as any other world surface: Japanese
    # business phrasing, no "tick", and clean under the reviewer-facing strip.
    import inspect

    from company_twin import kernel as kernel_module
    from company_twin.sme_blind_review import strip_experimenter_vocabulary

    src = inspect.getsource(kernel_module)
    assert "Campaign deadline reached" not in src
    assert "overdue since tick" not in src

    campaign_notice = "本日はキャンペーンの締切日です。処理中の案件について、証跡の確認、未了の承認、保留中の案件の状況をご確認のうえ対応を進めてください。"
    assert campaign_notice in src
    stripped = strip_experimenter_vocabulary(campaign_notice)
    assert stripped["was_clean"], stripped["redactions"]

    from company_twin.world_calendar import render_tick_as_date

    overdue_notice = f"承認依頼 APR-0001(案件 APP-R03)が期限({render_tick_as_date(6)})を超過しています。至急ご対応ください。"
    stripped2 = strip_experimenter_vocabulary(overdue_notice)
    assert stripped2["was_clean"], stripped2["redactions"]
    assert "tick" not in overdue_notice
