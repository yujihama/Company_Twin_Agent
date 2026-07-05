"""Tests for WP-14 calibration machinery: backcasting, holdout, SME blind review.

All fixtures here are offline: no LLM/API call is made anywhere in this file.
Coverage includes the honest-fail path for each gate (missing input, unfilled
packet, zero-detection holdout) and the ungameability property that a
hand-crafted report claiming ``passed: true`` without structural evidence rows
is rejected by readiness.run_readiness_gate.
"""
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from company_twin.backcasting import (
    BACKCASTING_REPRODUCTION_TARGET,
    extract_backcasting_cases,
    score_backcasting_reproduction,
    write_backcasting_inputs,
    write_backcasting_report,
)
from company_twin.cli import app
from company_twin.design_loader import load_design
from company_twin.holdout import (
    HOLDOUT_DETECTION_TARGET,
    build_holdout_injection_plan,
    compute_holdout_detection_rate,
    write_holdout_inputs,
    write_holdout_report,
)
from company_twin.readiness import REPORT_SCHEMA_VERSION, run_readiness_gate, write_readiness_reports
from company_twin.sme_blind_review import (
    SME_PLAUSIBILITY_TARGET,
    build_blind_review_packet,
    sample_run_bundle_excerpts,
    score_sme_blind_review,
    strip_experimenter_vocabulary,
    write_sme_blind_review_inputs,
    write_sme_blind_review_report,
)


def _design():
    return load_design(Path.cwd())


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


# ---------------------------------------------------------------------------
# Backcasting
# ---------------------------------------------------------------------------


def test_extract_backcasting_cases_finds_real_corpus_exemplars_with_provenance() -> None:
    design = _design()

    extraction = extract_backcasting_cases(design)

    assert extraction["schema_version"] == "company_twin.backcasting_inputs.v1"
    assert extraction["documents_scanned"] > 0
    assert extraction["distinct_case_count"] > 0
    assert extraction["raw_occurrence_count"] >= extraction["distinct_case_count"]
    case = extraction["cases"][0]
    assert case["situation"] and case["documented_response"]
    assert case["occurrences"][0]["doc_id"].startswith("DFH-")
    assert case["occurrence_count"] == len(case["occurrences"])
    # No experimenter-plane vocabulary should leak into extracted case text.
    for banned in ("probe", "span", "mutation", "experiment", "oracle"):
        assert banned not in case["situation"].lower()
        assert banned not in case["documented_response"].lower()


def test_extract_backcasting_cases_dedupes_repeated_faq_boilerplate() -> None:
    design = _design()
    extraction = extract_backcasting_cases(design)

    # The same 現場判断事例/現場判断メモ pair within one document must collapse
    # to a single distinct case with occurrence_count >= 2, not be double counted.
    duplicated = [case for case in extraction["cases"] if case["occurrence_count"] >= 2]
    assert duplicated, "expected at least one case duplicated across 現場判断事例/現場判断メモ within a document"


def test_backcasting_report_blocked_when_inputs_missing(tmp_path: Path) -> None:
    payload = write_backcasting_report(tmp_path)

    assert payload["schema_version"] == REPORT_SCHEMA_VERSION
    assert payload["passed"] is False
    assert payload["status"] == "blocked"
    assert (tmp_path / "backcasting_inputs.json").exists() is False
    assert not (tmp_path / "backcasting_report.json").read_text(encoding="utf-8") == ""


def test_backcasting_report_blocked_when_extraction_has_zero_cases(tmp_path: Path) -> None:
    empty_extraction = {
        "schema_version": "company_twin.backcasting_inputs.v1",
        "kind": "exemplar_case_extraction",
        "documents_scanned": 3,
        "documents_with_cases": 0,
        "raw_occurrence_count": 0,
        "distinct_case_count": 0,
        "cases": [],
    }
    write_backcasting_inputs(tmp_path, empty_extraction)

    payload = write_backcasting_report(tmp_path)

    assert payload["passed"] is False
    assert "zero distinct cases" in payload["checks"][0]["detail"]


def test_backcasting_report_blocked_without_scored_resimulation_results(tmp_path: Path) -> None:
    design = _design()
    extraction = extract_backcasting_cases(design)
    write_backcasting_inputs(tmp_path, extraction)

    payload = write_backcasting_report(tmp_path, resimulation_results=[])

    assert payload["passed"] is False
    assert "no re-simulation results" in payload["checks"][0]["detail"]
    assert payload["scoring"]["rows"] == []


def test_backcasting_report_passes_when_reproduction_rate_meets_target(tmp_path: Path) -> None:
    design = _design()
    extraction = extract_backcasting_cases(design)
    write_backcasting_inputs(tmp_path, extraction)
    case_ids = [case["case_id"] for case in extraction["cases"][:5]]
    results = [{"case_id": case_id, "reproduced": True, "probe_id": "P-02", "run_root": "s1_seed0"} for case_id in case_ids]

    payload = write_backcasting_report(tmp_path, resimulation_results=results)

    assert payload["passed"] is True
    assert payload["scoring"]["reproduction_rate"] == 1.0
    assert len(payload["checks"][0]["rows"]) == len(case_ids)


def test_backcasting_reproduction_rate_below_target_fails_honestly(tmp_path: Path) -> None:
    design = _design()
    extraction = extract_backcasting_cases(design)
    write_backcasting_inputs(tmp_path, extraction)
    case_ids = [case["case_id"] for case in extraction["cases"][:5]]
    results = [{"case_id": case_id, "reproduced": (idx == 0), "probe_id": "P-02"} for idx, case_id in enumerate(case_ids)]

    payload = write_backcasting_report(tmp_path, resimulation_results=results)

    assert payload["passed"] is False
    assert payload["scoring"]["reproduction_rate"] < BACKCASTING_REPRODUCTION_TARGET


def test_score_backcasting_reproduction_rejects_unknown_case_ids() -> None:
    extraction = {"cases": [{"case_id": "case_known"}]}

    scoring = score_backcasting_reproduction(extraction, [{"case_id": "case_unknown", "reproduced": True}])

    assert scoring["valid_result_count"] == 0
    assert scoring["reproduced_count"] == 0
    assert scoring["rows"][0]["matches_known_case"] is False


def test_backcasting_extract_cli_writes_inputs_file(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["backcasting-extract", "--campaign-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "backcasting_inputs.json").exists()


# ---------------------------------------------------------------------------
# Holdout
# ---------------------------------------------------------------------------


def test_build_holdout_injection_plan_reuses_wp06_mutation_catalog() -> None:
    plan = build_holdout_injection_plan(Path.cwd())

    assert plan["schema_version"] == "company_twin.holdout_inputs.v1"
    assert plan["detection_target"] == HOLDOUT_DETECTION_TARGET
    assert plan["injection_count"] == len(plan["injections"])
    assert plan["injection_count"] >= 5
    mutation_ids = {injection["mutation_id"] for injection in plan["injections"]}
    assert "clarify_elderly_understanding_all" in mutation_ids
    assert all(injection["spec_hash"] for injection in plan["injections"])
    # Every injection must carry a pre-registered, non-empty expected-detection
    # spec -- this is what makes strict scoring pre-registered rather than
    # chosen post-hoc.
    assert all(injection["expected_finding_types"] for injection in plan["injections"])


def test_build_holdout_injection_plan_rejects_unknown_mutation_id() -> None:
    with pytest.raises(ValueError, match="unknown mutation_id"):
        build_holdout_injection_plan(Path.cwd(), mutation_ids=["not_a_real_mutation"])


def _run_bundle_with_findings(root: Path, *, finding_types: dict[str, int], rule_hit: dict[str, Any] | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "triage").mkdir(exist_ok=True)
    (root / "triage" / "metrics.json").write_text(
        json.dumps({"stage": "S1", "finding_types": finding_types, "rule_hit_rate": rule_hit or {}, "detection_miss_rate": {}}),
        encoding="utf-8",
    )
    (root / "meta.json").write_text(json.dumps({"stage": "S1", "mutation_ids": ["clarify_elderly_understanding_all"]}), encoding="utf-8")


def test_compute_holdout_detection_rate_counts_l0_and_l1_evidence(tmp_path: Path) -> None:
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all", "dangling_fill_search_key_stub"])
    # grounding_gap is in the pre-registered expected_finding_types for both
    # clarify and dangling_fill, so this is a strict hit for the matching one.
    _run_bundle_with_findings(tmp_path / "s1_run0", finding_types={"grounding_gap": 2})

    measurement = compute_holdout_detection_rate(tmp_path, plan)

    assert measurement["detection_rate_basis"] == "strict"
    assert measurement["injection_count"] == 2
    assert measurement["detected_count"] == 1
    assert measurement["detection_rate"] == 0.5
    assert measurement["strict_detection_rate"] == 0.5
    assert measurement["lenient_detection_rate"] == 0.5
    detected_row = next(row for row in measurement["per_injection"] if row["mutation_id"] == "clarify_elderly_understanding_all")
    assert detected_row["detected"] is True
    assert detected_row["strict_detected"] is True
    assert detected_row["lenient_detected"] is True
    assert detected_row["l0_finding_types"] == ["grounding_gap"]
    undetected_row = next(row for row in measurement["per_injection"] if row["mutation_id"] == "dangling_fill_search_key_stub")
    assert undetected_row["detected"] is False
    assert undetected_row["reason"]


def test_compute_holdout_detection_rate_counts_l1_only_evidence(tmp_path: Path) -> None:
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["contradict_chat_approval_recorded"])
    _run_bundle_with_findings(
        tmp_path / "s1_run0",
        finding_types={},
        rule_hit={"MON-SAME-SUBMITTER-APPROVER": {"finding_type": "sod_pattern", "hit_count": 1}},
    )
    (tmp_path / "s1_run0" / "meta.json").write_text(
        json.dumps({"stage": "S1", "mutation_ids": ["contradict_chat_approval_recorded"]}), encoding="utf-8"
    )

    measurement = compute_holdout_detection_rate(tmp_path, plan)

    # sod_pattern is in contradict's pre-registered expected_finding_types, so
    # the L1-only hit counts under strict too.
    assert measurement["detected_count"] == 1
    assert measurement["strict_detected_count"] == 1
    assert measurement["lenient_detected_count"] == 1
    assert measurement["per_injection"][0]["l1_monitoring_rules"] == ["MON-SAME-SUBMITTER-APPROVER"]
    assert measurement["per_injection"][0]["l1_finding_types"] == ["sod_pattern"]


def test_compute_holdout_detection_rate_unrelated_finding_counts_lenient_not_strict(tmp_path: Path) -> None:
    """Ungameability: an unrelated finding_type on a mutated run inflates the
    lenient rate but must NOT count as a strict hit."""
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"])
    # deadline_overrun has nothing to do with the clarify mutation's
    # pre-registered expectation (grounding_gap/version_gap/version_mix); it
    # is an unrelated finding merely co-occurring on the mutated run.
    _run_bundle_with_findings(tmp_path / "s1_run0", finding_types={"deadline_overrun": 3})

    measurement = compute_holdout_detection_rate(tmp_path, plan)

    assert measurement["lenient_detection_rate"] == 1.0
    assert measurement["strict_detection_rate"] == 0.0
    assert measurement["detection_rate"] == 0.0  # official field follows strict
    assert measurement["passed"] is False
    row = measurement["per_injection"][0]
    assert row["lenient_detected"] is True
    assert row["strict_detected"] is False
    assert row["detected"] is False
    assert "expected_finding_types" in row["strict_reason"]


def test_holdout_report_blocked_when_inputs_missing(tmp_path: Path) -> None:
    payload = write_holdout_report(tmp_path)

    assert payload["passed"] is False
    assert payload["status"] == "blocked"


def test_holdout_report_fails_honestly_below_target_and_passes_above(tmp_path: Path) -> None:
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=[
            "clarify_elderly_understanding_all",
            "clarify_elderly_understanding_sales_only",
            "contradict_chat_approval_recorded",
            "dangling_fill_search_key_stub",
            "role_table_fix_quality_owner",
        ],
    )
    write_holdout_inputs(tmp_path, plan)
    # Only one of five mutations produces a run bundle with findings -> 0.2 < 0.80 target.
    _run_bundle_with_findings(tmp_path / "s1_run0", finding_types={"grounding_gap": 1})
    (tmp_path / "s1_run0" / "meta.json").write_text(
        json.dumps({"stage": "S1", "mutation_ids": ["clarify_elderly_understanding_all"]}), encoding="utf-8"
    )

    failing = write_holdout_report(tmp_path)
    assert failing["passed"] is False
    assert failing["measurement"]["detection_rate"] < HOLDOUT_DETECTION_TARGET
    assert failing["measurement"]["strict_detection_rate"] < HOLDOUT_DETECTION_TARGET
    assert failing["detection_rate_basis"] == "strict"

    # Now supply matching run bundles for all five mutations, each producing a
    # finding_type that is actually in that mutation's own pre-registered
    # expected_finding_types (not a blanket grounding_gap) -> strict rate 1.0.
    run_lookup = {}
    for idx, injection in enumerate(plan["injections"]):
        run_root = tmp_path / f"s1_holdout_{idx}"
        finding_type = injection["expected_finding_types"][0]
        _run_bundle_with_findings(run_root, finding_types={finding_type: 1})
        run_lookup[injection["injection_id"]] = run_root

    passing = write_holdout_report(tmp_path, run_lookup=run_lookup)
    assert passing["passed"] is True
    assert passing["measurement"]["detection_rate"] == 1.0
    assert passing["measurement"]["strict_detection_rate"] == 1.0
    assert passing["measurement"]["lenient_detection_rate"] == 1.0
    assert len(passing["checks"][0]["per_injection"]) == 5


def test_holdout_report_gate_fails_on_strict_even_when_lenient_passes(tmp_path: Path) -> None:
    """The official pass/fail must gate on strict, not lenient: construct a
    campaign where every mutation's run bundle fires an L0 finding (so
    lenient_detection_rate == 1.0 >= 0.80) but none of those findings match
    the mutation's own pre-registered expectation (so strict stays 0.0)."""
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=[
            "clarify_elderly_understanding_all",
            "clarify_elderly_understanding_sales_only",
            "contradict_chat_approval_recorded",
            "dangling_fill_search_key_stub",
            "role_table_fix_quality_owner",
        ],
    )
    write_holdout_inputs(tmp_path, plan)

    run_lookup = {}
    for idx, injection in enumerate(plan["injections"]):
        run_root = tmp_path / f"s1_holdout_{idx}"
        # deadline_overrun is not in any of these mutations' expected sets,
        # so it lights up lenient without ever satisfying strict.
        _run_bundle_with_findings(run_root, finding_types={"deadline_overrun": 1})
        run_lookup[injection["injection_id"]] = run_root

    payload = write_holdout_report(tmp_path, run_lookup=run_lookup)

    assert payload["measurement"]["lenient_detection_rate"] == 1.0
    assert payload["measurement"]["strict_detection_rate"] == 0.0
    assert payload["passed"] is False
    assert payload["status"] == "blocked"


def test_compute_holdout_detection_rate_rejects_plan_without_expected_specs(tmp_path: Path) -> None:
    """Scoring must refuse a plan whose injections lack a pre-registered
    expected_finding_types spec -- otherwise "what counts as a hit" could be
    chosen post-hoc at scoring time instead of at plan-build time."""
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"])
    plan["injections"][0]["expected_finding_types"] = []
    _run_bundle_with_findings(tmp_path / "s1_run0", finding_types={"grounding_gap": 1})

    with pytest.raises(ValueError, match="expected_finding_types"):
        compute_holdout_detection_rate(tmp_path, plan)


def test_holdout_plan_and_score_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    plan_result = runner.invoke(app, ["holdout-plan", "--campaign-root", str(tmp_path), "--mutation", "clarify_elderly_understanding_all"])
    assert plan_result.exit_code == 0, plan_result.output
    assert (tmp_path / "holdout_inputs.json").exists()

    score_result = runner.invoke(app, ["holdout-score", "--campaign-root", str(tmp_path)])
    assert score_result.exit_code == 1  # honest fail: no matching run bundles
    assert (tmp_path / "holdout_report.json").exists()


# ---------------------------------------------------------------------------
# SME blind review
# ---------------------------------------------------------------------------


def _fixture_run_bundle(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _write_jsonl(
        root / "chat_channel.jsonl",
        [
            {"body": "本日の申込、意向把握のメモが未記入なので確認してもらえますか。"},
            {"body": "承知しました、確認して午後に折り返します。"},
            {"body": "顧客への再説明は完了しました、記録も残しています。"},
        ],
    )
    _write_jsonl(
        root / "world_ledger.jsonl",
        [
            {"event_type": "customer_utterance", "payload": {"utterance": "解約したいのですが手続きを教えてください。"}},
            {"event_type": "customer_utterance", "payload": {"utterance": "商品の説明をもう一度お願いできますか。"}},
            {"event_type": "month_end_close", "payload": {}},
        ],
    )
    (root / "attempts.jsonl").write_text("", encoding="utf-8")
    return root


def test_sample_run_bundle_excerpts_reads_chat_and_ledger(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")

    excerpts = sample_run_bundle_excerpts(run_root)

    kinds = {excerpt["kind"] for excerpt in excerpts}
    assert "chat_message" in kinds
    assert "business_event" in kinds
    assert any("解約したいのですが" in excerpt["text"] for excerpt in excerpts)


def test_strip_experimenter_vocabulary_removes_banned_terms_and_span_ids() -> None:
    stripped = strip_experimenter_vocabulary("この対応はprobe P-01のAMB-01検証で、シミュレーションのoracleが判定した。")

    assert "probe" not in stripped["text"].lower()
    assert "AMB-01" not in stripped["text"]
    assert "シミュレーション" not in stripped["text"] and "simulation" not in stripped["text"].lower()
    assert "oracle" not in stripped["text"].lower()
    assert stripped["redactions"]
    assert stripped["was_clean"] is False


def test_strip_experimenter_vocabulary_leaves_clean_business_text_untouched() -> None:
    text = "顧客から解約について問い合わせがあり、担当者が手続きを案内した。"
    stripped = strip_experimenter_vocabulary(text)

    assert stripped["text"] == text
    assert stripped["was_clean"] is True
    assert stripped["redactions"] == []


def test_build_blind_review_packet_strips_and_drops_excessively_leaky_items(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")

    packet = build_blind_review_packet([run_root])

    assert packet["schema_version"] == "company_twin.sme_blind_review_inputs.v1"
    assert packet["item_count"] > 0
    for item in packet["items"]:
        assert item["response"] is None
        for banned in ("probe", "span", "oracle", "recorder", "experiment"):
            assert banned not in item["text"].lower()


def test_build_blind_review_packet_drops_items_with_even_a_single_redaction(tmp_path: Path) -> None:
    # A shipped "[削除済み]" placeholder is itself an artificial marker that
    # would tip off a blind reviewer, so any redaction (not just heavy
    # saturation) must drop the item rather than ship a partially-redacted
    # fragment.
    run_root = tmp_path / "run_leaky"
    run_root.mkdir(parents=True, exist_ok=True)
    _write_jsonl(
        run_root / "chat_channel.jsonl",
        [{"body": "本日のprobe対応は完了しました。"}],  # exactly one leaking term
    )
    (run_root / "world_ledger.jsonl").write_text("", encoding="utf-8")
    (run_root / "attempts.jsonl").write_text("", encoding="utf-8")

    packet = build_blind_review_packet([run_root])

    assert packet["item_count"] == 0
    assert packet["dropped_count"] == 1
    assert packet["dropped_items"][0]["reason"] == "leaked_vocabulary_redacted"
    assert packet["dropped_items"][0]["redaction_count"] == 1


def test_sme_blind_review_report_blocked_when_inputs_missing(tmp_path: Path) -> None:
    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is False
    assert payload["status"] == "blocked"


def test_sme_blind_review_report_blocked_when_packet_unfilled(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet = build_blind_review_packet([run_root])
    write_sme_blind_review_inputs(tmp_path, packet)

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is False
    assert payload["scoring"]["reviewed_count"] == 0
    assert "reviewed_count=0" in payload["checks"][0]["detail"]


def test_sme_blind_review_report_passes_when_filled_responses_meet_target(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet = build_blind_review_packet([run_root], samples_per_run=10)
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    write_sme_blind_review_inputs(tmp_path, packet)

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is True
    assert payload["scoring"]["plausibility_rate"] == 1.0
    assert payload["scoring"]["reviewed_count"] == packet["item_count"]


def test_sme_blind_review_report_fails_on_artificial_marker_flag(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet = build_blind_review_packet([run_root], samples_per_run=10)
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    packet["items"][0]["response"]["no_artificial_markers"] = "yes"
    write_sme_blind_review_inputs(tmp_path, packet)

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is False
    assert "artificial_marker_flag_count" in payload["checks"][0]["detail"]


def test_score_sme_blind_review_treats_null_response_as_unreviewed_not_passing() -> None:
    packet = {"items": [{"item_id": "a", "response": None}, {"item_id": "b", "response": {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}}]}

    scoring = score_sme_blind_review(packet)

    assert scoring["unreviewed_count"] == 1
    assert scoring["reviewed_count"] == 1
    assert scoring["plausibility_rate"] == 1.0


def test_sme_pack_and_score_cli(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    runner = CliRunner()
    pack_result = runner.invoke(app, ["sme-pack", "--campaign-root", str(tmp_path), "--run-root", str(run_root)])
    assert pack_result.exit_code == 0, pack_result.output
    assert (tmp_path / "sme_blind_review_inputs.json").exists()

    score_result = runner.invoke(app, ["sme-score", "--campaign-root", str(tmp_path)])
    assert score_result.exit_code == 1  # honest fail: packet is unfilled
    assert (tmp_path / "sme_blind_review.json").exists()


# ---------------------------------------------------------------------------
# Readiness integration + ungameability
# ---------------------------------------------------------------------------


def test_readiness_reports_wires_wp14_writers_end_to_end(tmp_path: Path) -> None:
    design = _design()
    extraction = extract_backcasting_cases(design)
    write_backcasting_inputs(tmp_path, extraction)
    case_ids = [case["case_id"] for case in extraction["cases"][:5]]
    (tmp_path / "backcasting_resimulation_results.json").write_text(
        json.dumps({"results": [{"case_id": cid, "reproduced": True} for cid in case_ids]}), encoding="utf-8"
    )

    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"])
    write_holdout_inputs(tmp_path, plan)
    _run_bundle_with_findings(tmp_path / "s1_run0", finding_types={"grounding_gap": 1})

    run_root = _fixture_run_bundle(tmp_path / "sme_run")
    packet = build_blind_review_packet([run_root])
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    write_sme_blind_review_inputs(tmp_path, packet)

    manifest = write_readiness_reports(tmp_path)

    assert "backcasting_report.json" in manifest["passed_reports"]
    assert "holdout_report.json" in manifest["passed_reports"]
    assert "sme_blind_review.json" in manifest["passed_reports"]

    gate = run_readiness_gate(tmp_path)
    passed_checks = {check["check"] for check in gate["checks"] if check["passed"]}
    assert "backcasting_passed" in passed_checks
    assert "holdout_passed" in passed_checks
    assert "sme_blind_review_passed" in passed_checks


def test_readiness_rejects_bare_passed_flag_without_structural_evidence_backcasting(tmp_path: Path) -> None:
    # Ungameability: a hand-edited report claiming passed=true with the right
    # schema_version, but with no per-case rows, must still be rejected.
    bare = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "backcasting",
        "status": "passed",
        "passed": True,
        "checks": [{"name": "backcasting_reproduction_rate_target", "passed": True, "rows": []}],
        "notes": [],
    }
    (tmp_path / "backcasting_report.json").write_text(json.dumps(bare), encoding="utf-8")

    gate = run_readiness_gate(tmp_path)

    failed = {check["check"]: check for check in gate["checks"] if not check["passed"]}
    assert "backcasting_passed" in failed
    assert "structural evidence" in failed["backcasting_passed"]["detail"]


def test_readiness_rejects_bare_passed_flag_without_structural_evidence_holdout(tmp_path: Path) -> None:
    bare = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "holdout",
        "status": "passed",
        "passed": True,
        "checks": [{"name": "holdout_detection_rate_target", "passed": True, "per_injection": []}],
        "notes": [],
    }
    (tmp_path / "holdout_report.json").write_text(json.dumps(bare), encoding="utf-8")

    gate = run_readiness_gate(tmp_path)

    failed = {check["check"]: check for check in gate["checks"] if not check["passed"]}
    assert "holdout_passed" in failed
    assert "structural evidence" in failed["holdout_passed"]["detail"]


def test_readiness_rejects_bare_passed_flag_without_structural_evidence_sme(tmp_path: Path) -> None:
    bare = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "sme_blind_review",
        "status": "passed",
        "passed": True,
        "checks": [{"name": "sme_blind_review_plausibility_target", "passed": True, "rows": []}],
        "notes": [],
    }
    (tmp_path / "sme_blind_review.json").write_text(json.dumps(bare), encoding="utf-8")

    gate = run_readiness_gate(tmp_path)

    failed = {check["check"]: check for check in gate["checks"] if not check["passed"]}
    assert "sme_blind_review_passed" in failed
    assert "structural evidence" in failed["sme_blind_review_passed"]["detail"]


def test_readiness_accepts_real_report_with_structural_evidence_rows(tmp_path: Path) -> None:
    design = _design()
    extraction = extract_backcasting_cases(design)
    write_backcasting_inputs(tmp_path, extraction)
    case_ids = [case["case_id"] for case in extraction["cases"][:3]]
    write_backcasting_report(tmp_path, resimulation_results=[{"case_id": cid, "reproduced": True} for cid in case_ids])

    gate = run_readiness_gate(tmp_path)

    passed_checks = {check["check"] for check in gate["checks"] if check["passed"]}
    assert "backcasting_passed" in passed_checks
