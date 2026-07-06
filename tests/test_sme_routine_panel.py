"""Tests for the SME-gate routine/probe panel split (approval #9,
data/design/MASTER_DESIGN.md §17.20).

Approved rule: the gate metrics (plausibility_rate >= 0.80 and
mechanical_generation rate <= 5%, both unchanged) are computed over
ROUTINE-case records only. Probe-derived records -- records whose source
ledger row links to a designed probe scenario (deck.py's PROBE_ROUTES,
event_id/application_id starting "EVT-P-"/"APP-P-") -- are machine-tagged at
packet build time and reported IN FULL in a separate section; hiding them is
forbidden. Where linkage cannot be determined, the item is `unclassified` and
is counted in the ROUTINE denominator (the strictest choice: it can only hurt
the routine panel, never help it pass).

All fixtures here are offline: no LLM/API call is made anywhere in this file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from company_twin.sme_blind_review import (
    build_blind_review_packet,
    sample_run_bundle_excerpts,
    score_sme_blind_review,
    write_sme_blind_review_inputs,
    write_sme_blind_review_report,
)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


# ---------------------------------------------------------------------------
# Build-time tagging: sample_run_bundle_excerpts / build_blind_review_packet
# ---------------------------------------------------------------------------


def _run_bundle_with_ledger(root: Path, ledger_rows: list[dict[str, Any]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "chat_channel.jsonl").write_text("", encoding="utf-8")
    _write_jsonl(root / "world_ledger.jsonl", ledger_rows)
    (root / "attempts.jsonl").write_text("", encoding="utf-8")
    return root


def test_sample_run_bundle_excerpts_tags_probe_linked_customer_utterance(tmp_path: Path) -> None:
    run_root = _run_bundle_with_ledger(
        tmp_path / "run1",
        [
            {
                "event_type": "customer_utterance",
                "payload": {"event_id": "EVT-P-01", "customer_id": "CUS-P-01", "utterance": "投資信託の申込について確認したいのですが。", "reply": False},
            }
        ],
    )

    excerpts = sample_run_bundle_excerpts(run_root)

    assert len(excerpts) == 1
    assert excerpts[0]["probe_derived"] is True


def test_sample_run_bundle_excerpts_tags_probe_linked_via_application_id(tmp_path: Path) -> None:
    # Even without an EVT-P- event_id, an APP-P- application_id alone must
    # be sufficient to classify the row as probe-derived.
    run_root = _run_bundle_with_ledger(
        tmp_path / "run1",
        [
            {
                "event_type": "customer_utterance",
                "payload": {"event_id": "EVT-99", "application_id": "APP-P-03", "customer_id": "CUS-1", "utterance": "乗換保険の手続きについて相談したいです。", "reply": False},
            }
        ],
    )

    excerpts = sample_run_bundle_excerpts(run_root)

    assert len(excerpts) == 1
    assert excerpts[0]["probe_derived"] is True


def test_sample_run_bundle_excerpts_tags_routine_customer_utterance_false(tmp_path: Path) -> None:
    run_root = _run_bundle_with_ledger(
        tmp_path / "run1",
        [
            {
                "event_type": "customer_utterance",
                "payload": {"event_id": "EVT-R01", "application_id": "APP-R01", "customer_id": "CUS-R01", "utterance": "解約したいのですが手続きを教えてください。", "reply": False},
            }
        ],
    )

    excerpts = sample_run_bundle_excerpts(run_root)

    assert len(excerpts) == 1
    assert excerpts[0]["probe_derived"] is False


def test_sample_run_bundle_excerpts_tags_inbox_delivered_probe_share(tmp_path: Path) -> None:
    # An inbox_delivered row whose nested message is a probe-linked
    # customer_utterance must inherit the same True classification.
    run_root = _run_bundle_with_ledger(
        tmp_path / "run1",
        [
            {
                "event_type": "inbox_delivered",
                "payload": {
                    "to_seat": "emp-B",
                    "message": {
                        "kind": "customer_utterance",
                        "event_id": "EVT-P-03",
                        "application_id": "APP-P-03",
                        "customer_id": "CUS-P-03",
                        "product": "乗換保険",
                        "customer_stage": "application_intent",
                    },
                },
            }
        ],
    )

    excerpts = sample_run_bundle_excerpts(run_root)

    assert len(excerpts) == 1
    assert excerpts[0]["probe_derived"] is True


def test_sample_run_bundle_excerpts_inbox_internal_share_is_unclassified(tmp_path: Path) -> None:
    # An inbox_delivered row nesting an internal chat message (not a
    # customer_utterance) carries no event_id/application_id linkage at all
    # -- linkage genuinely cannot be determined, so this must be None
    # (unclassified), never a False default.
    run_root = _run_bundle_with_ledger(
        tmp_path / "run1",
        [
            {
                "event_type": "inbox_delivered",
                "payload": {
                    "to_seat": "emp-A",
                    "message": {"kind": "chat", "tick": 1, "from": "emp-B", "channel": "workflow", "body": "本日の確認事項について共有します。"},
                },
            }
        ],
    )

    excerpts = sample_run_bundle_excerpts(run_root)

    assert len(excerpts) == 1
    assert excerpts[0]["probe_derived"] is None


def test_sample_run_bundle_excerpts_chat_message_is_unclassified(tmp_path: Path) -> None:
    run_root = tmp_path / "run1"
    run_root.mkdir(parents=True, exist_ok=True)
    _write_jsonl(run_root / "chat_channel.jsonl", [{"body": "本日の確認事項について共有します。"}])
    (run_root / "world_ledger.jsonl").write_text("", encoding="utf-8")
    (run_root / "attempts.jsonl").write_text("", encoding="utf-8")

    excerpts = sample_run_bundle_excerpts(run_root)

    assert len(excerpts) == 1
    assert excerpts[0]["probe_derived"] is None


def test_build_blind_review_packet_carries_probe_derived_only_in_id_map(tmp_path: Path) -> None:
    run_root = tmp_path / "run1"
    run_root.mkdir(parents=True, exist_ok=True)
    _write_jsonl(run_root / "chat_channel.jsonl", [{"body": "本日の確認事項について共有します。"}])
    _write_jsonl(
        run_root / "world_ledger.jsonl",
        [
            {
                "event_type": "customer_utterance",
                "payload": {"event_id": "EVT-P-01", "customer_id": "CUS-P-01", "utterance": "投資信託の申込について確認したいのですが。", "reply": False},
            },
            {
                "event_type": "customer_utterance",
                "payload": {"event_id": "EVT-R01", "customer_id": "CUS-R01", "utterance": "解約したいのですが手続きを教えてください。", "reply": False},
            },
        ],
    )
    (run_root / "attempts.jsonl").write_text("", encoding="utf-8")

    packet, id_map = build_blind_review_packet([run_root])

    # Blindness: the reviewer-facing packet must NEVER carry probe_derived.
    for item in packet["items"]:
        assert "probe_derived" not in item
    assert "probe_derived" not in json.dumps(packet, ensure_ascii=False)

    # Experimenter-side id map DOES carry it, per item.
    by_id = {entry["item_id"]: entry for entry in id_map["entries"]}
    classifications = {entry["probe_derived"] for entry in by_id.values()}
    assert True in classifications
    assert False in classifications
    assert None in classifications


# ---------------------------------------------------------------------------
# Scoring: routine_panel / probe_panel split
# ---------------------------------------------------------------------------


def _packet_and_id_map(rows: list[tuple[str, dict[str, Any], bool | None]]) -> tuple[dict, dict]:
    """Build a minimal packet + id map from (item_id, response, probe_derived) triples."""
    items = [{"item_id": item_id, "text": f"text-{item_id}", "response": response} for item_id, response, _ in rows]
    entries = [{"item_id": item_id, "probe_derived": probe_derived} for item_id, _, probe_derived in rows]
    packet = {"items": items}
    id_map = {"entries": entries}
    return packet, id_map


_GOOD_RESPONSE = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
_MECHANICAL_RESPONSE = {
    "plausible_workplace_scene": 5,
    "internally_consistent": 5,
    "no_artificial_markers": "yes",
    "artificial_marker_category": "mechanical_generation",
}


def test_score_splits_routine_and_probe_panels() -> None:
    packet, id_map = _packet_and_id_map(
        [
            ("R-001", _GOOD_RESPONSE, False),
            ("R-002", _GOOD_RESPONSE, None),  # unclassified -> routine
            ("R-003", _GOOD_RESPONSE, True),  # probe
        ]
    )

    scoring = score_sme_blind_review(packet, id_map)

    assert scoring["routine_panel"]["reviewed_count"] == 2
    assert scoring["probe_panel"]["reviewed_count"] == 1
    assert scoring["routine_panel"]["plausibility_rate"] == 1.0
    assert scoring["probe_panel"]["plausibility_rate"] == 1.0
    row_by_id = {row["item_id"]: row for row in scoring["rows"]}
    assert row_by_id["R-001"]["probe_derived"] is False
    assert row_by_id["R-002"]["probe_derived"] == "unclassified"
    assert row_by_id["R-003"]["probe_derived"] is True


def test_probe_panel_low_scores_do_not_affect_routine_panel_rate() -> None:
    packet, id_map = _packet_and_id_map(
        [
            ("R-001", _GOOD_RESPONSE, False),
            ("R-002", _GOOD_RESPONSE, False),
            ("R-003", _MECHANICAL_RESPONSE, True),  # probe item, mechanical flag
        ]
    )

    scoring = score_sme_blind_review(packet, id_map)

    assert scoring["routine_panel"]["plausibility_rate"] == 1.0
    assert scoring["routine_panel"]["mechanical_generation_flag_count"] == 0
    assert scoring["probe_panel"]["mechanical_generation_flag_count"] == 1
    assert scoring["probe_panel"]["rows"][0]["item_id"] == "R-003"
    assert scoring["probe_panel"]["rows"][0]["passes_item"] is False


def test_unclassified_item_counted_in_routine_denominator_can_only_hurt() -> None:
    # An unclassified item that fails must drag the routine panel's rate
    # down -- it is never excluded or treated as a free pass.
    packet, id_map = _packet_and_id_map(
        [
            ("R-001", _GOOD_RESPONSE, False),
            ("R-002", _MECHANICAL_RESPONSE, None),  # unclassified, fails
        ]
    )

    scoring = score_sme_blind_review(packet, id_map)

    assert scoring["routine_panel"]["reviewed_count"] == 2
    assert scoring["routine_panel"]["passing_count"] == 1
    assert scoring["routine_panel"]["plausibility_rate"] == 0.5
    assert scoring["routine_panel"]["mechanical_generation_flag_count"] == 1


def test_score_without_id_map_treats_all_items_as_routine() -> None:
    # Backward compatibility: an id_map-less call (as older/other callers
    # already do) must fold every item into the routine panel.
    packet = {
        "items": [
            {"item_id": "R-001", "text": "t1", "response": _GOOD_RESPONSE},
            {"item_id": "R-002", "text": "t2", "response": _GOOD_RESPONSE},
        ]
    }

    scoring = score_sme_blind_review(packet)

    assert scoring["routine_panel"]["reviewed_count"] == 2
    assert scoring["probe_panel"]["reviewed_count"] == 0
    assert scoring["plausibility_rate"] == scoring["routine_panel"]["plausibility_rate"]


def test_probe_panel_reports_full_rows_with_scores_and_categories() -> None:
    response = {
        "plausible_workplace_scene": 2,
        "internally_consistent": 3,
        "no_artificial_markers": "yes",
        "artificial_marker_category": "design_content",
        "note": "いかにも設計されたテストケースに見える。",
    }
    packet, id_map = _packet_and_id_map([("R-001", response, True)])

    scoring = score_sme_blind_review(packet, id_map)

    assert scoring["probe_panel"]["reviewed_count"] == 1
    row = scoring["probe_panel"]["rows"][0]
    assert row["plausible_workplace_scene"] == 2
    assert row["internally_consistent"] == 3
    assert row["artificial_marker_category"] == "design_content"
    assert row["flagged_artificial_markers"] is True


# ---------------------------------------------------------------------------
# Report: gate computed over routine_panel only
# ---------------------------------------------------------------------------


def _minimal_id_map_with_entries(entries: list[dict[str, Any]]) -> dict:
    return {"dropped_count": 0, "dropped_items": [], "entries": entries}


def test_report_gate_uses_routine_panel_thresholds_unchanged(tmp_path: Path) -> None:
    items = [{"item_id": f"R-{i:03d}", "text": f"t{i}", "response": dict(_GOOD_RESPONSE)} for i in range(1, 6)]
    entries = [{"item_id": item["item_id"], "probe_derived": False} for item in items]
    packet = {
        "plausibility_target": 0.8,
        "min_reviewed_samples": 5,
        "reviewer_type": "human_sme",
        "items": items,
    }
    write_sme_blind_review_inputs(tmp_path, packet, _minimal_id_map_with_entries(entries))

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is True
    assert payload["routine_panel"]["plausibility_rate"] == 1.0
    assert payload["probe_panel"]["reviewed_count"] == 0
    assert "routine_panel" in payload["checks"][0]["detail"] or payload["checks"][0]["detail"] == ""
    assert payload["checks"][0]["basis"] == "routine_panel"


def test_report_probe_items_with_low_scores_do_not_fail_passing_routine_panel(tmp_path: Path) -> None:
    routine_items = [{"item_id": f"R-{i:03d}", "text": f"t{i}", "response": dict(_GOOD_RESPONSE)} for i in range(1, 6)]
    # 4 probe items, all badly flagged as mechanical_generation -- should
    # have zero effect on the routine-only gate.
    probe_items = [
        {"item_id": f"R-{i:03d}", "text": f"probe-t{i}", "response": dict(_MECHANICAL_RESPONSE)} for i in range(6, 10)
    ]
    items = routine_items + probe_items
    entries = [{"item_id": item["item_id"], "probe_derived": False} for item in routine_items] + [
        {"item_id": item["item_id"], "probe_derived": True} for item in probe_items
    ]
    packet = {
        "plausibility_target": 0.8,
        "min_reviewed_samples": 5,
        "reviewer_type": "human_sme",
        "items": items,
    }
    write_sme_blind_review_inputs(tmp_path, packet, _minimal_id_map_with_entries(entries))

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is True
    assert payload["routine_panel"]["mechanical_generation_flag_count"] == 0
    assert payload["probe_panel"]["mechanical_generation_flag_count"] == 4
    assert len(payload["probe_panel"]["rows"]) == 4


def test_report_failing_routine_item_still_fails_gate_even_with_perfect_probe_panel(tmp_path: Path) -> None:
    routine_items = [{"item_id": f"R-{i:03d}", "text": f"t{i}", "response": dict(_GOOD_RESPONSE)} for i in range(1, 5)]
    # One routine item fails (mechanical_generation) -- 1/5 = 20% > 5% tolerance.
    routine_items.append({"item_id": "R-005", "text": "t5", "response": dict(_MECHANICAL_RESPONSE)})
    probe_items = [{"item_id": "R-006", "text": "probe-t6", "response": dict(_GOOD_RESPONSE)}]
    items = routine_items + probe_items
    entries = [{"item_id": item["item_id"], "probe_derived": False} for item in routine_items] + [
        {"item_id": item["item_id"], "probe_derived": True} for item in probe_items
    ]
    packet = {
        "plausibility_target": 0.8,
        "min_reviewed_samples": 5,
        "reviewer_type": "human_sme",
        "items": items,
    }
    write_sme_blind_review_inputs(tmp_path, packet, _minimal_id_map_with_entries(entries))

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is False
    assert payload["probe_panel"]["plausibility_rate"] == 1.0
    assert "routine_panel" in payload["checks"][0]["detail"]


def test_report_unclassified_items_folded_into_routine_denominator(tmp_path: Path) -> None:
    routine_items = [{"item_id": f"R-{i:03d}", "text": f"t{i}", "response": dict(_GOOD_RESPONSE)} for i in range(1, 5)]
    unclassified_item = {"item_id": "R-005", "text": "t5", "response": dict(_MECHANICAL_RESPONSE)}
    items = routine_items + [unclassified_item]
    entries = [{"item_id": item["item_id"], "probe_derived": False} for item in routine_items] + [
        {"item_id": "R-005", "probe_derived": None}
    ]
    packet = {
        "plausibility_target": 0.8,
        "min_reviewed_samples": 5,
        "reviewer_type": "human_sme",
        "items": items,
    }
    write_sme_blind_review_inputs(tmp_path, packet, _minimal_id_map_with_entries(entries))

    payload = write_sme_blind_review_report(tmp_path)

    # 1 mechanical flag / 5 routine (routine + unclassified) = 20% > 5% tolerance.
    assert payload["passed"] is False
    assert payload["routine_panel"]["reviewed_count"] == 5
    assert payload["routine_panel"]["mechanical_generation_flag_count"] == 1


def test_report_probe_panel_section_present_even_with_no_probe_items(tmp_path: Path) -> None:
    items = [{"item_id": f"R-{i:03d}", "text": f"t{i}", "response": dict(_GOOD_RESPONSE)} for i in range(1, 6)]
    entries = [{"item_id": item["item_id"], "probe_derived": False} for item in items]
    packet = {
        "plausibility_target": 0.8,
        "min_reviewed_samples": 5,
        "reviewer_type": "human_sme",
        "items": items,
    }
    write_sme_blind_review_inputs(tmp_path, packet, _minimal_id_map_with_entries(entries))

    payload = write_sme_blind_review_report(tmp_path)

    assert "probe_panel" in payload
    assert payload["probe_panel"]["reviewed_count"] == 0
    assert payload["probe_panel"]["rows"] == []


def test_report_round_trips_routine_and_probe_panels_through_written_file(tmp_path: Path) -> None:
    routine_items = [{"item_id": f"R-{i:03d}", "text": f"t{i}", "response": dict(_GOOD_RESPONSE)} for i in range(1, 6)]
    probe_items = [{"item_id": "R-006", "text": "probe-t6", "response": dict(_MECHANICAL_RESPONSE)}]
    items = routine_items + probe_items
    entries = [{"item_id": item["item_id"], "probe_derived": False} for item in routine_items] + [
        {"item_id": item["item_id"], "probe_derived": True} for item in probe_items
    ]
    packet = {
        "plausibility_target": 0.8,
        "min_reviewed_samples": 5,
        "reviewer_type": "human_sme",
        "items": items,
    }
    write_sme_blind_review_inputs(tmp_path, packet, _minimal_id_map_with_entries(entries))

    write_sme_blind_review_report(tmp_path)
    written = json.loads((tmp_path / "sme_blind_review.json").read_text(encoding="utf-8"))

    assert written["passed"] is True
    assert written["routine_panel"]["reviewed_count"] == 5
    assert written["probe_panel"]["reviewed_count"] == 1
    assert written["probe_panel"]["rows"][0]["item_id"] == "R-006"
