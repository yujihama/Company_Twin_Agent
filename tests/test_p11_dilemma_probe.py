from __future__ import annotations

from pathlib import Path

from company_twin.action_replay import classify_sample_attempts
from company_twin.campaign import WORLD_PROMPT_BANNED_TERMS
from company_twin.deck import build_customer_deck, event_for_probe, probe_absence_ticks_for_event, probe_assumes_manager_absence
from company_twin.design_loader import load_design
from company_twin.world_config import build_world_config


def _design():
    return load_design(Path.cwd())


def test_p11_exists_with_same_day_deadline() -> None:
    event = event_for_probe(_design(), "P-11")
    assert event.deadline_tick == event.trigger_tick + 1  # closed valve: no time for formal approval
    assert event.primary_seat == "emp-A"
    assert probe_assumes_manager_absence("P-11")


def test_p11_absence_spans_the_deadline_ordinary_and_compressed() -> None:
    design = _design()
    event = event_for_probe(design, "P-11")
    span = probe_absence_ticks_for_event(event.to_dict())
    assert span == list(range(event.trigger_tick, event.deadline_tick + 1))

    for pressure in (False, True):
        config = build_world_config(design, stage="S2", model=None, seed=0, ticks=40, time_pressure=pressure)
        absence = set(config["world"]["schedule"]["manager_absence_ticks"])
        deck_event = next(e for e in config["world"]["deck"]["events"] if e["probe_id"] == "P-11")
        needed = set(range(deck_event["trigger_tick"], deck_event["deadline_tick"] + 1))
        assert needed <= absence, (pressure, needed, absence)


def test_p11_absence_span_does_not_change_other_probes() -> None:
    design = _design()
    p04 = event_for_probe(design, "P-04")
    assert probe_absence_ticks_for_event(p04.to_dict()) == [p04.trigger_tick]
    p01 = event_for_probe(design, "P-01")
    assert probe_absence_ticks_for_event(p01.to_dict()) == []


def test_p11_world_visible_text_passes_banned_term_lint() -> None:
    event = event_for_probe(_design(), "P-11")
    lowered = event.world_visible.lower()
    hits = [term for term in WORLD_PROMPT_BANNED_TERMS if term.lower() in lowered]
    assert not hits, hits
    assert "管理者" in event.world_visible and "本日中" in event.world_visible


def test_deck_still_contains_all_eleven_probes_and_routines() -> None:
    events = build_customer_deck(_design(), include_routine=True)
    probe_ids = sorted(e.probe_id for e in events if not e.routine)
    assert probe_ids == [f"P-{i:02d}" for i in range(1, 12)]


def test_classifier_v2_reports_any_case_actions() -> None:
    attempts = [
        {"seat_id": "emp-A", "tool": "record_customer_contact", "args": {"customer_id": "CUS-R01"}, "success": True},
        {"seat_id": "emp-A", "tool": "search_corpus", "args": {"query": "x"}, "success": True},
    ]
    result = classify_sample_attempts(attempts, probe_markers={"APP-P-11"}, probe_id="P-11")
    assert result["attempted_class"] == "no_staff_action_on_probe"  # nothing probe-linked
    assert result["acted_on_any_case"] is True  # ...but the seat was demonstrably busy
    assert result["any_case_attempted_tools"] == ["record_customer_contact"]
