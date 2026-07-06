"""Tests for the round-7 blind-SME-review follow-up (data/design/MASTER_DESIGN.md
§17.14).

Round 7 flagged three `mechanical_generation` items:

1. R-008: the product name "乗換保険" was flagged as a machine-generation
   artifact, but it is the frozen-corpus product name for probe P-03
   (deck.py's PROBE_ROUTES["P-03"], data/compiled_data/world_config_v2.yaml,
   data/compiled_data/deck_v2.json) -- already documented in
   MASTER_DESIGN.md §17.6 as "frozen-corpus naming". This is a gate-semantics
   miscategorization, not a real defect (the term cannot be renamed away: the
   corpus document set is frozen for comparability across calibration
   rounds).
2. R-037: a duplicated fragment + truncated tail -- a genuine customer-LLM
   output glitch.
3. R-038: broken text ("進めてよかまだ") -- a genuine customer-LLM output
   glitch.

This file tests the APPROVED gate-semantics fix for (1):
`sme_blind_review.FROZEN_CORPUS_TERMS` and the recategorization wired into
`score_sme_blind_review`/`write_sme_blind_review_report`. (2)/(3) are tested
in tests/test_customer_glitch_guard.py (the detector/retry-guard fix, which
lives in customer_agent.py/agents.py, never here).

All fixtures here are offline: no LLM/API call is made anywhere in this file.
"""
from __future__ import annotations

import json
from pathlib import Path

from company_twin.sme_blind_review import (
    FROZEN_CORPUS_TERMS,
    score_sme_blind_review,
    write_sme_blind_review_inputs,
    write_sme_blind_review_report,
)


def test_frozen_corpus_term_is_a_probe_p03_product_name() -> None:
    # Verify the term actually appears in the corpus data, matching the
    # citation in MASTER_DESIGN.md §17.6 ("frozen-corpus naming (e.g.
    # 乗換保険)") -- deck.py's PROBE_ROUTES["P-03"]["product"].
    from company_twin.deck import PROBE_ROUTES

    assert PROBE_ROUTES["P-03"]["product"] == "乗換保険"
    assert "乗換保険" in FROZEN_CORPUS_TERMS


def _packet_with_response(*, item_text: str, response: dict) -> dict:
    return {
        "items": [
            {
                "item_id": "R-008",
                "text": item_text,
                "response": response,
            }
        ]
    }


def test_term_only_note_recategorizes_mechanical_flag_to_design_content() -> None:
    # The exact round-7 R-008 shape: the item's own text carries the
    # frozen-corpus term, and the reviewer's note cites ONLY that term as the
    # basis for the mechanical_generation flag.
    packet = _packet_with_response(
        item_text="お客様より乗換保険についてご相談あり。期日は6月8日まで。",
        response={
            "plausible_workplace_scene": 5,
            "internally_consistent": 5,
            "no_artificial_markers": "yes",
            "artificial_marker_category": "mechanical_generation",
            "note": "「乗換保険」という言い回しが機械的な生成物のように見えた。",
        },
    )

    scoring = score_sme_blind_review(packet)

    assert scoring["mechanical_generation_flag_count"] == 0
    assert scoring["artificial_marker_category_counts"]["design_content"] == 1
    assert scoring["recategorized_count"] == 1
    row = scoring["rows"][0]
    assert row["artificial_marker_category"] == "design_content"
    assert row["recategorized_from"] == "mechanical_generation"
    assert row["recategorization_basis"] == "frozen_corpus_term:乗換保険"
    # The item still passes (plausible>=4, consistent>=4, no mechanical flag).
    assert row["passes_item"] is True
    assert scoring["recategorized_rows"] == [row]


def test_mixed_basis_note_stays_mechanical_and_is_not_recategorized() -> None:
    # If the note ALSO cites another basis (duplication, broken text, system
    # vocabulary), the other basis stands -- recategorization must NOT occur,
    # and the mechanical_generation flag/gate-failure remains.
    packet = _packet_with_response(
        item_text="お客様より乗換保険についてご相談あり。期日は6月8日まで。",
        response={
            "plausible_workplace_scene": 5,
            "internally_consistent": 5,
            "no_artificial_markers": "yes",
            "artificial_marker_category": "mechanical_generation",
            "note": "「乗換保険」という語に加えて、文章の一部が重複していた。",
        },
    )

    scoring = score_sme_blind_review(packet)

    assert scoring["mechanical_generation_flag_count"] == 1
    assert scoring["recategorized_count"] == 0
    row = scoring["rows"][0]
    assert row["artificial_marker_category"] == "mechanical_generation"
    assert "recategorized_from" not in row
    assert row["passes_item"] is False


def test_note_referencing_term_not_in_item_text_is_not_recategorized() -> None:
    # Never recategorize on the reviewer's say-so alone -- the term must
    # actually appear in the item's own text.
    packet = _packet_with_response(
        item_text="お客様より投資信託についてご相談あり。",
        response={
            "plausible_workplace_scene": 5,
            "internally_consistent": 5,
            "no_artificial_markers": "yes",
            "artificial_marker_category": "mechanical_generation",
            "note": "「乗換保険」という言い回しが不自然だった。",
        },
    )

    scoring = score_sme_blind_review(packet)

    assert scoring["mechanical_generation_flag_count"] == 1
    assert scoring["recategorized_count"] == 0


def test_no_note_is_not_recategorized() -> None:
    # A bare mechanical_generation flag with no note at all is left as-is --
    # there is nothing to key the recategorization decision on.
    packet = _packet_with_response(
        item_text="お客様より乗換保険についてご相談あり。",
        response={
            "plausible_workplace_scene": 5,
            "internally_consistent": 5,
            "no_artificial_markers": "yes",
            "artificial_marker_category": "mechanical_generation",
        },
    )

    scoring = score_sme_blind_review(packet)

    assert scoring["mechanical_generation_flag_count"] == 1
    assert scoring["recategorized_count"] == 0


def test_design_content_flag_unaffected_by_recategorization_logic() -> None:
    # A flag already categorized as design_content (not mechanical_generation)
    # must pass through unchanged -- recategorization only ever applies to a
    # mechanical_generation flag.
    packet = _packet_with_response(
        item_text="お客様より乗換保険についてご相談あり。",
        response={
            "plausible_workplace_scene": 5,
            "internally_consistent": 5,
            "no_artificial_markers": "yes",
            "artificial_marker_category": "design_content",
            "note": "乗換保険というシナリオがいかにも設計されたテストケースに見える。",
        },
    )

    scoring = score_sme_blind_review(packet)

    assert scoring["recategorized_count"] == 0
    assert scoring["rows"][0]["artificial_marker_category"] == "design_content"
    assert "recategorized_from" not in scoring["rows"][0]


def test_gate_still_fails_with_any_true_mechanical_flag_alongside_a_recategorized_one() -> None:
    # The zero-mechanical-flags requirement is unchanged: a genuinely
    # mechanical-generation item elsewhere in the packet still fails the
    # gate, even though this item's flag was recategorized away.
    packet = {
        "items": [
            {
                "item_id": "R-008",
                "text": "お客様より乗換保険についてご相談あり。",
                "response": {
                    "plausible_workplace_scene": 5,
                    "internally_consistent": 5,
                    "no_artificial_markers": "yes",
                    "artificial_marker_category": "mechanical_generation",
                    "note": "「乗換保険」という言い回しが機械的に見えた。",
                },
            },
            {
                "item_id": "R-037",
                "text": "担当者へのご連絡、担当者へのご連絡、担当者へのご連絡をお願いしたく存じ",
                "response": {
                    "plausible_workplace_scene": 5,
                    "internally_consistent": 5,
                    "no_artificial_markers": "yes",
                    "artificial_marker_category": "mechanical_generation",
                    "note": "文章の一部が重複しており、末尾も途切れている。",
                },
            },
        ]
    }

    scoring = score_sme_blind_review(packet)

    assert scoring["recategorized_count"] == 1
    assert scoring["mechanical_generation_flag_count"] == 1


def _minimal_id_map() -> dict:
    return {"dropped_count": 0}


def test_report_surfaces_recategorization_counts(tmp_path: Path) -> None:
    packet = {
        "plausibility_target": 0.8,
        "min_reviewed_samples": 1,
        "reviewer_type": "human_sme",
        "items": [
            {
                "item_id": "R-008",
                "text": "お客様より乗換保険についてご相談あり。",
                "response": {
                    "plausible_workplace_scene": 5,
                    "internally_consistent": 5,
                    "no_artificial_markers": "yes",
                    "artificial_marker_category": "mechanical_generation",
                    "note": "「乗換保険」という言い回しが機械的に見えた。",
                },
            }
        ],
    }
    write_sme_blind_review_inputs(tmp_path, packet, _minimal_id_map())

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is True
    check = payload["checks"][0]
    assert check["recategorized_count"] == 1
    assert check["recategorized_rows"][0]["recategorization_basis"] == "frozen_corpus_term:乗換保険"
    assert payload["scoring"]["recategorized_count"] == 1
    # Verify it round-trips through the written file too.
    written = json.loads((tmp_path / "sme_blind_review.json").read_text(encoding="utf-8"))
    assert written["checks"][0]["recategorized_count"] == 1
