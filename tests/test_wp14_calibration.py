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
    ARM_BENIGN_CONTROL,
    ARM_POSITIVE_CONTROL,
    HOLDOUT_DETECTION_TARGET,
    build_holdout_injection_plan,
    compute_holdout_detection_rate,
    score_benign_controls,
    score_holdout_controls,
    verify_holdout_bundles,
    write_holdout_inputs,
    write_holdout_report,
)
from company_twin.mutations import load_mutation_catalog
from company_twin.readiness import REPORT_SCHEMA_VERSION, run_readiness_gate, write_readiness_reports
from company_twin.sme_blind_review import (
    ARTIFICIAL_MARKER_CATEGORIES,
    REVIEW_QUESTIONS,
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


def _verified_s2_bundle(root: Path, *, injection: dict[str, Any], finding_types: dict[str, int], planned_ticks: int = 4) -> None:
    """Build a run bundle that passes holdout bundle-attribution verification
    (verify_holdout_bundles): stage S2, config.json mutation entries matching
    the injection's spec_hash/mutation_id, and world_ledger tick coverage."""
    from company_twin.world_config import _json_hash as world_json_hash

    root.mkdir(parents=True, exist_ok=True)
    (root / "triage").mkdir(exist_ok=True)
    (root / "triage" / "metrics.json").write_text(
        json.dumps({"stage": "S2", "finding_types": finding_types, "rule_hit_rate": {}, "detection_miss_rate": {}}),
        encoding="utf-8",
    )
    mutation_id = injection["mutation_id"]
    spec = load_mutation_catalog(Path.cwd())[mutation_id]
    assert world_json_hash(spec) == injection["spec_hash"]
    mutation_entry = dict(spec)
    (root / "config.json").write_text(
        json.dumps({"world": {"corpus": {"mutations": [mutation_entry], "mutation_hash": world_json_hash([mutation_entry]), "effective_corpus_hash": "test-hash"}}}),
        encoding="utf-8",
    )
    (root / "meta.json").write_text(json.dumps({"stage": "S2", "mutation_ids": [mutation_id]}), encoding="utf-8")
    ledger_rows = [{"tick": tick, "event_type": "tick_committed"} for tick in range(1, planned_ticks + 1)]
    (root / "world_ledger.jsonl").write_text("".join(json.dumps(row) + "\n" for row in ledger_rows), encoding="utf-8")


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
    # Only one of four positive-control mutations produces a run bundle with
    # findings -> 0.25 < 0.80 target. (role_table_fix_quality_owner is
    # benign_control and excluded from this denominator.)
    _run_bundle_with_findings(tmp_path / "s1_run0", finding_types={"grounding_gap": 1})
    (tmp_path / "s1_run0" / "meta.json").write_text(
        json.dumps({"stage": "S1", "mutation_ids": ["clarify_elderly_understanding_all"]}), encoding="utf-8"
    )

    failing = write_holdout_report(tmp_path)
    assert failing["passed"] is False
    assert failing["measurement"]["detection_rate"] < HOLDOUT_DETECTION_TARGET
    assert failing["measurement"]["strict_detection_rate"] < HOLDOUT_DETECTION_TARGET
    assert failing["detection_rate_basis"] == "strict"

    # Now supply matching run bundles for all five mutations. The four
    # positive_control mutations each produce a finding_type that is actually
    # in that mutation's own pre-registered expected_finding_types (not a
    # blanket grounding_gap) -> strict rate 1.0. The benign_control
    # (role_table_fix_quality_owner) gets a CLEAN bundle (no findings at all)
    # -- a benign_control injection is expected to produce nothing new, so a
    # clean bundle is what "passing" looks like for it, not an injected
    # finding. Every bundle is verified (stage S2, config.json mutation entry
    # matching spec_hash, adequate tick coverage, no failure marker).
    run_lookup = {}
    for idx, injection in enumerate(plan["injections"]):
        run_root = tmp_path / f"s2_holdout_{idx}"
        if injection["arm"] == "benign_control":
            _verified_s2_bundle(run_root, injection=injection, finding_types={})
        else:
            finding_type = injection["expected_finding_types"][0]
            _verified_s2_bundle(run_root, injection=injection, finding_types={finding_type: 1})
        run_lookup[injection["injection_id"]] = run_root

    passing = write_holdout_report(tmp_path, run_lookup=run_lookup)
    assert passing["passed"] is True
    assert passing["measurement"]["detection_rate"] == 1.0
    assert passing["measurement"]["strict_detection_rate"] == 1.0
    assert passing["measurement"]["lenient_detection_rate"] == 1.0
    assert passing["measurement"]["injection_count"] == 4  # positive_control only
    assert len(passing["checks"][0]["per_injection"]) == 5  # all arms still itemized
    assert passing["bundle_verification"]["all_verified"] is True
    assert passing["benign_controls"]["all_passed"] is True
    assert passing["benign_controls"]["injection_count"] == 1
    assert passing["plan_hash"] == plan["plan_hash"]


# ---------------------------------------------------------------------------
# Expert-review hardening: bundle-attribution verification + controls
# ---------------------------------------------------------------------------


def test_verify_holdout_bundles_fails_on_spec_hash_mismatch(tmp_path: Path) -> None:
    """Ungameability: a run bundle whose config.json mutation entries do not
    actually match the injection's spec_hash/mutation_id must not verify,
    even if L0/L1 findings happen to line up (e.g. a stale bundle re-used
    across mutation revisions)."""
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"], run_roots=["s2_bad"])
    write_holdout_inputs(tmp_path, plan)
    injection = plan["injections"][0]
    root = tmp_path / "s2_bad"
    root.mkdir(parents=True, exist_ok=True)
    (root / "triage").mkdir(exist_ok=True)
    (root / "triage" / "metrics.json").write_text(
        json.dumps({"stage": "S2", "finding_types": {injection["expected_finding_types"][0]: 1}, "rule_hit_rate": {}, "detection_miss_rate": {}}),
        encoding="utf-8",
    )
    # config.json declares an unrelated mutation entry, not this injection's spec.
    (root / "config.json").write_text(
        json.dumps({"world": {"corpus": {"mutations": [{"mutation_id": "some_other_mutation", "operator": "clarify"}]}}}),
        encoding="utf-8",
    )
    (root / "meta.json").write_text(json.dumps({"stage": "S2", "mutation_ids": [injection["mutation_id"]]}), encoding="utf-8")
    (root / "world_ledger.jsonl").write_text("".join(json.dumps({"tick": t, "event_type": "tick_committed"}) + "\n" for t in range(1, 5)), encoding="utf-8")

    verification = verify_holdout_bundles(tmp_path, plan, run_lookup={injection["injection_id"]: root})

    assert verification["all_verified"] is False
    assert verification["per_injection"][0]["runs"][0]["spec_hash_consistent"] is False

    report = write_holdout_report(tmp_path, run_lookup={injection["injection_id"]: root})
    assert report["passed"] is False
    assert report["bundle_verification"]["all_verified"] is False


def test_verify_holdout_bundles_records_exploration_mode_and_cannot_pass(tmp_path: Path) -> None:
    """Implicit run-root scanning (no planned_run_roots, no explicit
    run_lookup entry) must be recorded as exploration-mode and cannot pass
    the readiness check, even when the scanned bundle happens to look
    correct."""
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"])  # no run_roots -> exploration
    write_holdout_inputs(tmp_path, plan)
    injection = plan["injections"][0]
    _verified_s2_bundle(tmp_path / "s1_run0", injection=injection, finding_types={injection["expected_finding_types"][0]: 1})
    # _matching_mutation_run_roots scans meta.json mutation_ids, so this bundle IS discoverable by scanning.

    verification = verify_holdout_bundles(tmp_path, plan, run_lookup=None)

    assert verification["any_exploration_mode"] is True
    assert verification["all_verified"] is False
    assert verification["per_injection"][0]["resolution_mode"] == "exploration"
    assert "exploration-mode" in verification["per_injection"][0]["detail"]

    report = write_holdout_report(tmp_path)
    assert report["passed"] is False
    assert report["checks"][0]["any_exploration_mode"] is True


def test_holdout_report_missing_controls_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"], run_roots=["s2_holdout_0"])
    write_holdout_inputs(tmp_path, plan)
    injection = plan["injections"][0]
    _verified_s2_bundle(tmp_path / "s2_holdout_0", injection=injection, finding_types={injection["expected_finding_types"][0]: 1})

    report = write_holdout_report(tmp_path, run_lookup={injection["injection_id"]: tmp_path / "s2_holdout_0"})

    assert report["passed"] is True  # missing controls never auto-fails
    assert report["controls"] is None
    assert any("no controls section" in note.lower() for note in report["notes"])


def test_holdout_report_controls_records_anomalous_hits_without_failing(tmp_path: Path) -> None:
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"], run_roots=["s2_holdout_0"])
    write_holdout_inputs(tmp_path, plan)
    injection = plan["injections"][0]
    _verified_s2_bundle(tmp_path / "s2_holdout_0", injection=injection, finding_types={injection["expected_finding_types"][0]: 1})
    # Control run: no mutation applied, but its triage metrics show the same
    # expected_finding_type firing anyway -- a false alarm on an unmutated run.
    control_root = tmp_path / "s2_control_anchor"
    control_root.mkdir(parents=True, exist_ok=True)
    (control_root / "triage").mkdir(exist_ok=True)
    (control_root / "triage" / "metrics.json").write_text(
        json.dumps({"stage": "S2", "finding_types": {injection["expected_finding_types"][0]: 1}, "rule_hit_rate": {}, "detection_miss_rate": {}}),
        encoding="utf-8",
    )

    report = write_holdout_report(
        tmp_path,
        run_lookup={injection["injection_id"]: tmp_path / "s2_holdout_0"},
        control_run_roots=["s2_control_anchor"],
    )

    assert report["passed"] is True  # anomalous control hits are recorded, not auto-fail
    assert report["controls"] is not None
    assert report["controls"]["anomalous_hit_count"] == 1
    assert report["controls"]["per_control"][0]["has_anomalous_hit"] is True


def test_score_holdout_controls_returns_none_when_no_control_roots_given() -> None:
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"])
    assert score_holdout_controls(Path.cwd(), plan, control_run_roots=None) is None
    assert score_holdout_controls(Path.cwd(), plan, control_run_roots=[]) is None


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
# 2026-07-05 approved recalibration: holdout arms + delta-based detection (part 2)
# ---------------------------------------------------------------------------


def test_build_holdout_injection_plan_assigns_default_arms() -> None:
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=[
            "clarify_elderly_understanding_all",
            "contradict_chat_approval_recorded",
            "dangling_fill_search_key_stub",
            "role_table_fix_quality_owner",
        ],
    )

    arms_by_mutation = {injection["mutation_id"]: injection["arm"] for injection in plan["injections"]}
    assert arms_by_mutation["clarify_elderly_understanding_all"] == ARM_POSITIVE_CONTROL
    assert arms_by_mutation["contradict_chat_approval_recorded"] == ARM_POSITIVE_CONTROL
    assert arms_by_mutation["dangling_fill_search_key_stub"] == ARM_POSITIVE_CONTROL
    assert arms_by_mutation["role_table_fix_quality_owner"] == ARM_BENIGN_CONTROL


def test_holdout_plan_arm_is_sealed_in_plan_hash() -> None:
    plan_a = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"])
    plan_b = json.loads(json.dumps(plan_a))
    plan_b["injections"][0]["arm"] = ARM_BENIGN_CONTROL

    # plan_hash was computed over the ORIGINAL arm; recomputing it over the
    # tampered copy's injections must differ, i.e. plan_hash is sensitive to
    # the arm field (it is part of what plan_hash seals).
    from company_twin.world_config import _json_hash as world_json_hash

    original_hash = world_json_hash({"injections": plan_a["injections"], "control_run_roots": plan_a["control_run_roots"]})
    tampered_hash = world_json_hash({"injections": plan_b["injections"], "control_run_roots": plan_b["control_run_roots"]})
    assert original_hash == plan_a["plan_hash"]
    assert tampered_hash != plan_a["plan_hash"]


def test_holdout_plan_control_run_roots_sealed_in_plan_hash() -> None:
    plan_no_controls = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"])
    plan_with_controls = build_holdout_injection_plan(
        Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"], control_run_roots=["s2_anchor"]
    )

    assert plan_no_controls["control_run_roots"] == []
    assert plan_with_controls["control_run_roots"] == ["s2_anchor"]
    assert plan_no_controls["plan_hash"] != plan_with_controls["plan_hash"]


def test_compute_holdout_detection_rate_excludes_benign_control_from_denominator(tmp_path: Path) -> None:
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["clarify_elderly_understanding_all", "role_table_fix_quality_owner"],
    )
    # Only the positive_control mutation gets a matching, detected run bundle.
    _run_bundle_with_findings(tmp_path / "s1_run0", finding_types={"grounding_gap": 1})

    measurement = compute_holdout_detection_rate(tmp_path, plan)

    assert measurement["injection_count"] == 1  # positive_control only
    assert measurement["total_injection_count"] == 2
    assert measurement["benign_control_count"] == 1
    assert measurement["detected_count"] == 1
    assert measurement["detection_rate"] == 1.0
    assert measurement["passed"] is True
    # Both arms still show up in the itemized per_injection list.
    assert len(measurement["per_injection"]) == 2
    arms = {row["mutation_id"]: row["arm"] for row in measurement["per_injection"]}
    assert arms["role_table_fix_quality_owner"] == ARM_BENIGN_CONTROL


def test_backward_compat_plan_without_arm_field_defaults_all_positive_control(tmp_path: Path) -> None:
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all", "role_table_fix_quality_owner"])
    # Simulate an old plan built before the `arm` field existed.
    for injection in plan["injections"]:
        del injection["arm"]
    _run_bundle_with_findings(tmp_path / "s1_run0", finding_types={"grounding_gap": 1})

    measurement = compute_holdout_detection_rate(tmp_path, plan)

    # Old behavior: every injection (including what would now be
    # role_table_fix's benign_control) counts toward the positive denominator.
    assert measurement["injection_count"] == 2
    assert measurement["total_injection_count"] == 2
    assert measurement["benign_control_count"] == 0
    assert all(row["arm"] == ARM_POSITIVE_CONTROL for row in measurement["per_injection"])


def test_delta_detection_baseline_confounded_when_control_fires_equally(tmp_path: Path) -> None:
    """A run bundle where the expected finding_type fires, but a designated
    no-mutation control run fires the SAME finding_type at an equal or higher
    rate, must NOT count as a strict hit -- it is baseline_confounded, since
    presence alone cannot distinguish mutation-caused signal from baseline
    noise (pre-#26-world campaign found grounding_gap/version_gap/
    tacit_chat_to_action false alarms on unmutated control runs)."""
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["clarify_elderly_understanding_all"],
        control_run_roots=["s2_control_anchor"],
    )
    write_holdout_inputs(tmp_path, plan)
    injection = plan["injections"][0]
    _verified_s2_bundle(tmp_path / "s2_holdout_0", injection=injection, finding_types={"grounding_gap": 1})
    # Control run: same finding_type fires at the SAME rate (both are raw L0
    # counts, so equal counts -> equal rate) with no mutation applied.
    control_root = tmp_path / "s2_control_anchor"
    control_root.mkdir(parents=True, exist_ok=True)
    (control_root / "triage").mkdir(exist_ok=True)
    (control_root / "triage" / "metrics.json").write_text(
        json.dumps({"stage": "S2", "finding_types": {"grounding_gap": 1}, "rule_hit_rate": {}, "detection_miss_rate": {}}),
        encoding="utf-8",
    )

    report = write_holdout_report(tmp_path, run_lookup={injection["injection_id"]: tmp_path / "s2_holdout_0"})

    row = report["measurement"]["per_injection"][0]
    assert row["strict_detected"] is False
    assert "grounding_gap" in row["baseline_confounded_finding_types"]
    assert "baseline_confounded" in report["measurement"]["per_injection"][0]["strict_reason"]
    assert report["passed"] is False


def test_delta_detection_exceeding_baseline_counts_as_detected(tmp_path: Path) -> None:
    """When the mutated run's rate for an expected finding_type EXCEEDS the
    max control-run rate for that same type, it is a genuine strict hit."""
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["clarify_elderly_understanding_all"],
        control_run_roots=["s2_control_anchor"],
    )
    write_holdout_inputs(tmp_path, plan)
    injection = plan["injections"][0]
    # Mutated run: 3 grounding_gap findings.
    _verified_s2_bundle(tmp_path / "s2_holdout_0", injection=injection, finding_types={"grounding_gap": 3})
    # Control run: only 1 grounding_gap finding (lower rate) -- baseline noise
    # that the mutated run clearly exceeds.
    control_root = tmp_path / "s2_control_anchor"
    control_root.mkdir(parents=True, exist_ok=True)
    (control_root / "triage").mkdir(exist_ok=True)
    (control_root / "triage" / "metrics.json").write_text(
        json.dumps({"stage": "S2", "finding_types": {"grounding_gap": 1}, "rule_hit_rate": {}, "detection_miss_rate": {}}),
        encoding="utf-8",
    )

    report = write_holdout_report(tmp_path, run_lookup={injection["injection_id"]: tmp_path / "s2_holdout_0"})

    row = report["measurement"]["per_injection"][0]
    assert row["strict_detected"] is True
    assert "grounding_gap" in row["matched_expected_finding_types"]
    assert row["baseline_confounded_finding_types"] == []
    assert report["measurement"]["strict_detection_rate"] == 1.0


def test_delta_detection_presence_suffices_when_absent_in_all_controls(tmp_path: Path) -> None:
    """When an expected finding_type never fires in ANY control run, mere
    presence on the mutated run suffices (nothing to exceed) -- this
    reproduces the pre-recalibration strict-detection behavior for a type
    with a clean (zero) control baseline."""
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["clarify_elderly_understanding_all"],
        control_run_roots=["s2_control_anchor"],
    )
    write_holdout_inputs(tmp_path, plan)
    injection = plan["injections"][0]
    _verified_s2_bundle(tmp_path / "s2_holdout_0", injection=injection, finding_types={"grounding_gap": 1})
    # Control run: completely clean, no findings of any kind.
    control_root = tmp_path / "s2_control_anchor"
    control_root.mkdir(parents=True, exist_ok=True)
    (control_root / "triage").mkdir(exist_ok=True)
    (control_root / "triage" / "metrics.json").write_text(
        json.dumps({"stage": "S2", "finding_types": {}, "rule_hit_rate": {}, "detection_miss_rate": {}}), encoding="utf-8"
    )

    report = write_holdout_report(tmp_path, run_lookup={injection["injection_id"]: tmp_path / "s2_holdout_0"})

    row = report["measurement"]["per_injection"][0]
    assert row["strict_detected"] is True
    assert row["control_baseline"]["grounding_gap"]["present_in_any_control"] is False
    assert report["passed"] is True


def test_benign_control_clean_and_at_baseline_passes(tmp_path: Path) -> None:
    plan = build_holdout_injection_plan(
        Path.cwd(), mutation_ids=["clarify_elderly_understanding_all", "role_table_fix_quality_owner"], control_run_roots=[]
    )
    write_holdout_inputs(tmp_path, plan)
    positive = next(i for i in plan["injections"] if i["arm"] == ARM_POSITIVE_CONTROL)
    benign = next(i for i in plan["injections"] if i["arm"] == ARM_BENIGN_CONTROL)
    _verified_s2_bundle(tmp_path / "s2_positive", injection=positive, finding_types={positive["expected_finding_types"][0]: 1})
    # Benign control: clean bundle, no anomaly findings at all.
    _verified_s2_bundle(tmp_path / "s2_benign", injection=benign, finding_types={})
    run_lookup = {positive["injection_id"]: tmp_path / "s2_positive", benign["injection_id"]: tmp_path / "s2_benign"}

    benign_result = score_benign_controls(tmp_path, plan, run_lookup=run_lookup)

    assert benign_result is not None
    assert benign_result["all_passed"] is True
    assert benign_result["injection_count"] == 1
    assert benign_result["per_injection"][0]["passed"] is True
    assert benign_result["per_injection"][0]["false_alarm_finding_types"] == []

    report = write_holdout_report(tmp_path, run_lookup=run_lookup)
    assert report["passed"] is True
    assert report["benign_controls"]["all_passed"] is True


def test_benign_control_false_alarm_fails(tmp_path: Path) -> None:
    """A benign_control (role_table_fix) run that DOES fire one of its
    previously-expected anomaly types is a false alarm and must fail --
    unlike a positive_control, nothing new should appear here."""
    plan = build_holdout_injection_plan(
        Path.cwd(), mutation_ids=["clarify_elderly_understanding_all", "role_table_fix_quality_owner"], control_run_roots=[]
    )
    write_holdout_inputs(tmp_path, plan)
    positive = next(i for i in plan["injections"] if i["arm"] == ARM_POSITIVE_CONTROL)
    benign = next(i for i in plan["injections"] if i["arm"] == ARM_BENIGN_CONTROL)
    _verified_s2_bundle(tmp_path / "s2_positive", injection=positive, finding_types={positive["expected_finding_types"][0]: 1})
    # Benign control fires its own expected finding_type -- a false alarm.
    _verified_s2_bundle(tmp_path / "s2_benign", injection=benign, finding_types={benign["expected_finding_types"][0]: 1})
    run_lookup = {positive["injection_id"]: tmp_path / "s2_positive", benign["injection_id"]: tmp_path / "s2_benign"}

    benign_result = score_benign_controls(tmp_path, plan, run_lookup=run_lookup)

    assert benign_result["all_passed"] is False
    assert benign_result["per_injection"][0]["passed"] is False
    assert benign["expected_finding_types"][0] in benign_result["per_injection"][0]["false_alarm_finding_types"]

    report = write_holdout_report(tmp_path, run_lookup=run_lookup)
    # Even though positive_control's own strict_detection_rate clears target,
    # the benign_control false alarm blocks the overall report -- a false
    # alarm on a benign_control run is evidence the detectors are unreliable.
    assert report["passed"] is False
    assert any("benign_controls FAILED" in note for note in report["notes"])


def test_positive_control_denominator_excludes_benign_arm_end_to_end(tmp_path: Path) -> None:
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
    positive_injections = [i for i in plan["injections"] if i["arm"] == ARM_POSITIVE_CONTROL]
    assert len(positive_injections) == 4
    benign_injections = [i for i in plan["injections"] if i["arm"] == ARM_BENIGN_CONTROL]
    assert len(benign_injections) == 1

    measurement = compute_holdout_detection_rate(tmp_path, plan)
    assert measurement["injection_count"] == 4
    assert measurement["total_injection_count"] == 5


def test_holdout_plan_cli_records_control_run_roots(tmp_path: Path) -> None:
    runner = CliRunner()
    plan_result = runner.invoke(
        app,
        [
            "holdout-plan",
            "--campaign-root",
            str(tmp_path),
            "--mutation",
            "clarify_elderly_understanding_all",
            "--control-run-root",
            "s2_anchor",
        ],
    )
    assert plan_result.exit_code == 0, plan_result.output
    plan = json.loads((tmp_path / "holdout_inputs.json").read_text(encoding="utf-8"))
    assert plan["control_run_roots"] == ["s2_anchor"]
    assert plan["injections"][0]["arm"] == ARM_POSITIVE_CONTROL


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

    packet, _id_map = build_blind_review_packet([run_root])

    assert packet["schema_version"] == "company_twin.sme_blind_review_inputs.v1"
    assert packet["item_count"] > 0
    for item in packet["items"]:
        assert item["response"] is None
        for banned in ("probe", "span", "oracle", "recorder", "experiment"):
            assert banned not in item["text"].lower()


def test_blind_review_packet_uses_neutral_sequential_reviewer_ids(tmp_path: Path) -> None:
    # A run-root-derived item_id like "anchor_s2_seed0:chat_0" puts
    # experimenter vocabulary (anchor/seed) directly in front of the blind
    # reviewer, who would correctly flag it as an artificial marker. The
    # reviewer-facing packet must carry only neutral sequential ids; the
    # mapping back to the run bundle lives in the experimenter-side id map.
    run_root = _fixture_run_bundle(tmp_path / "anchor_s2_seed0")

    packet, id_map = build_blind_review_packet([run_root])

    assert packet["item_count"] > 0
    for index, item in enumerate(packet["items"]):
        assert item["item_id"] == f"R-{index + 1:03d}"
        assert set(item) == {"item_id", "kind", "text", "questions", "response"}
    serialized = json.dumps(packet, ensure_ascii=False)
    assert "anchor_s2_seed0" not in serialized
    assert "run_root" not in serialized
    assert "was_clean" not in serialized
    assert "redaction" not in serialized
    assert "dropped" not in serialized

    assert id_map["schema_version"] == "company_twin.sme_blind_review_id_map.v1"
    assert id_map["packet_hash"] == packet["packet_hash"]
    by_id = {entry["item_id"]: entry for entry in id_map["entries"]}
    assert set(by_id) == {item["item_id"] for item in packet["items"]}
    assert all(entry["run_root"] == "anchor_s2_seed0" for entry in id_map["entries"])
    assert all(entry["redaction_count"] == 0 and entry["was_clean"] for entry in id_map["entries"])


def test_blind_review_packet_reviewer_facing_fields_pass_leak_lint(tmp_path: Path) -> None:
    # Every reviewer-visible string in the packet (keys and values alike) must
    # survive the same leak lint the excerpt texts are held to. The fixed
    # questionnaire is the one deliberate exception: it asks the reviewer
    # whether they noticed 実験/シミュレーション-style markers, so it is
    # asserted by identity against the canonical instrument instead of being
    # lint-scanned.
    run_root = _fixture_run_bundle(tmp_path / "anchor_s2_seed0")

    packet, _id_map = build_blind_review_packet([run_root])
    assert packet["item_count"] > 0

    def reviewer_strings(node: Any):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                if key == "questions":
                    continue
                yield from reviewer_strings(value)
        elif isinstance(node, list):
            for value in node:
                yield from reviewer_strings(value)
        elif isinstance(node, str):
            yield node

    for text in reviewer_strings(packet):
        stripped = strip_experimenter_vocabulary(text)
        assert stripped["was_clean"], f"reviewer-facing leak: {text!r} -> {stripped['redactions']}"
    for item in packet["items"]:
        assert item["questions"] == [dict(question) for question in REVIEW_QUESTIONS]


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

    packet, id_map = build_blind_review_packet([run_root])

    assert packet["item_count"] == 0
    # Drop bookkeeping is experimenter metadata and lives in the id map, not
    # the reviewer-facing packet.
    assert "dropped_count" not in packet and "dropped_items" not in packet
    assert id_map["dropped_count"] == 1
    assert id_map["dropped_items"][0]["reason"] == "leaked_vocabulary_redacted"
    assert id_map["dropped_items"][0]["redaction_count"] == 1
    assert id_map["dropped_items"][0]["run_root"] == "run_leaky"


def test_sme_blind_review_report_blocked_when_inputs_missing(tmp_path: Path) -> None:
    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is False
    assert payload["status"] == "blocked"


def test_sme_blind_review_report_blocked_when_packet_unfilled(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet, id_map = build_blind_review_packet([run_root])
    write_sme_blind_review_inputs(tmp_path, packet, id_map)

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is False
    assert payload["scoring"]["reviewed_count"] == 0
    assert "reviewed_count=0" in payload["checks"][0]["detail"]


def test_sme_blind_review_report_passes_when_filled_responses_meet_target(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet, id_map = build_blind_review_packet([run_root], samples_per_run=10)
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    write_sme_blind_review_inputs(tmp_path, packet, id_map)

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is True
    assert payload["scoring"]["plausibility_rate"] == 1.0
    assert payload["scoring"]["reviewed_count"] == packet["item_count"]


def test_sme_blind_review_report_fails_on_artificial_marker_flag(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet, id_map = build_blind_review_packet([run_root], samples_per_run=10)
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    packet["items"][0]["response"]["no_artificial_markers"] = "yes"
    write_sme_blind_review_inputs(tmp_path, packet, id_map)

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is False
    assert "artificial_marker_flag_count" in payload["checks"][0]["detail"]


# ---------------------------------------------------------------------------
# 2026-07-05 approved recalibration: artificial_marker_category (part 1)
# ---------------------------------------------------------------------------


def test_review_questions_include_artificial_marker_category_prompt() -> None:
    question_ids = {question["question_id"] for question in REVIEW_QUESTIONS}
    assert "artificial_marker_category" in question_ids
    category_question = next(q for q in REVIEW_QUESTIONS if q["question_id"] == "artificial_marker_category")
    for category in ARTIFICIAL_MARKER_CATEGORIES:
        assert category in category_question["prompt"]


def test_score_sme_blind_review_mechanical_generation_flag_fails_item() -> None:
    packet = {
        "items": [
            {
                "item_id": "a",
                "response": {
                    "plausible_workplace_scene": 5,
                    "internally_consistent": 5,
                    "no_artificial_markers": "yes",
                    "artificial_marker_category": "mechanical_generation",
                },
            }
        ]
    }

    scoring = score_sme_blind_review(packet)

    assert scoring["rows"][0]["passes_item"] is False
    assert scoring["rows"][0]["artificial_marker_category"] == "mechanical_generation"
    assert scoring["mechanical_generation_flag_count"] == 1
    assert scoring["artificial_marker_flag_count"] == 1
    assert scoring["artificial_marker_category_counts"]["mechanical_generation"] == 1
    assert scoring["plausibility_rate"] == 0.0


def test_score_sme_blind_review_design_content_flag_can_still_pass_item() -> None:
    # A design_content or statistical_structure flag is counted per category
    # but does not, on its own, fail the item -- these are the structurally
    # irreducible flags (recognizability of a designed probe scenario /
    # aggregate statistical structure) that round-3 blind review left behind,
    # per MASTER_DESIGN.md section 17's approved recalibration.
    packet = {
        "items": [
            {
                "item_id": "a",
                "response": {
                    "plausible_workplace_scene": 5,
                    "internally_consistent": 5,
                    "no_artificial_markers": "yes",
                    "artificial_marker_category": "design_content",
                },
            },
            {
                "item_id": "b",
                "response": {
                    "plausible_workplace_scene": 5,
                    "internally_consistent": 5,
                    "no_artificial_markers": "yes",
                    "artificial_marker_category": "statistical_structure",
                },
            },
        ]
    }

    scoring = score_sme_blind_review(packet)

    assert scoring["rows"][0]["passes_item"] is True
    assert scoring["rows"][1]["passes_item"] is True
    assert scoring["mechanical_generation_flag_count"] == 0
    assert scoring["artificial_marker_flag_count"] == 2
    assert scoring["artificial_marker_category_counts"] == {
        "mechanical_generation": 0,
        "design_content": 1,
        "statistical_structure": 1,
    }
    assert scoring["plausibility_rate"] == 1.0


def test_score_sme_blind_review_uncategorized_yes_treated_as_mechanical_strictest() -> None:
    # Backward compatibility hardening: a "yes" response with no category (an
    # old/unmigrated response packet) must be treated as mechanical_generation
    # -- the strictest category -- so it cannot pass more easily than a
    # properly categorized response.
    packet = {
        "items": [
            {
                "item_id": "a",
                "response": {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "yes"},
            }
        ]
    }

    scoring = score_sme_blind_review(packet)

    assert scoring["rows"][0]["artificial_marker_category"] == "mechanical_generation"
    assert scoring["rows"][0]["passes_item"] is False
    assert scoring["mechanical_generation_flag_count"] == 1


def test_score_sme_blind_review_unrecognized_category_treated_as_mechanical() -> None:
    packet = {
        "items": [
            {
                "item_id": "a",
                "response": {
                    "plausible_workplace_scene": 5,
                    "internally_consistent": 5,
                    "no_artificial_markers": "yes",
                    "artificial_marker_category": "not_a_real_category",
                },
            }
        ]
    }

    scoring = score_sme_blind_review(packet)

    assert scoring["rows"][0]["artificial_marker_category"] == "mechanical_generation"
    assert scoring["rows"][0]["passes_item"] is False


def test_sme_blind_review_report_passes_with_only_design_content_flags(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet, id_map = build_blind_review_packet([run_root], samples_per_run=10)
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    packet["items"][0]["response"] = {
        "plausible_workplace_scene": 5,
        "internally_consistent": 5,
        "no_artificial_markers": "yes",
        "artificial_marker_category": "design_content",
    }
    write_sme_blind_review_inputs(tmp_path, packet, id_map)

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is True
    assert payload["scoring"]["mechanical_generation_flag_count"] == 0
    assert payload["scoring"]["artificial_marker_category_counts"]["design_content"] == 1


def test_sme_blind_review_report_fails_on_mechanical_generation_category(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet, id_map = build_blind_review_packet([run_root], samples_per_run=10)
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    packet["items"][0]["response"] = {
        "plausible_workplace_scene": 5,
        "internally_consistent": 5,
        "no_artificial_markers": "yes",
        "artificial_marker_category": "mechanical_generation",
    }
    write_sme_blind_review_inputs(tmp_path, packet, id_map)

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is False
    assert "mechanical_generation_flag_count=1" in payload["checks"][0]["detail"]


def test_sme_blind_review_report_fails_on_uncategorized_yes_old_format_packet(tmp_path: Path) -> None:
    # Simulates an old-format packet/response predating artificial_marker_category.
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet, id_map = build_blind_review_packet([run_root], samples_per_run=10)
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    packet["items"][0]["response"] = {
        "plausible_workplace_scene": 5,
        "internally_consistent": 5,
        "no_artificial_markers": "yes",
        # no artificial_marker_category key at all
    }
    write_sme_blind_review_inputs(tmp_path, packet, id_map)

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is False
    assert payload["scoring"]["mechanical_generation_flag_count"] == 1


def test_sme_blind_review_report_blocked_when_id_map_missing(tmp_path: Path) -> None:
    """Expert-review hardening: sme_blind_review_id_map.json is required
    alongside the packet -- dropped_count can only be read from it."""
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet, _id_map = build_blind_review_packet([run_root], samples_per_run=10)
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    write_sme_blind_review_inputs(tmp_path, packet, id_map=None)  # id map deliberately omitted

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is False
    assert payload["status"] == "blocked"
    assert "sme_blind_review_id_map_supplied" in {check["name"] for check in payload["checks"]}


def test_sme_blind_review_report_fails_when_dropped_count_positive(tmp_path: Path) -> None:
    """Ungameability: a leaked_vocabulary_redacted drop is an artifact
    detection (the world leaked experimenter vocabulary), not exclusion
    bookkeeping. dropped_count > 0 must fail the report even when every
    remaining (clean) item scores perfectly."""
    run_root = tmp_path / "run_leaky"
    run_root.mkdir(parents=True, exist_ok=True)
    _write_jsonl(
        run_root / "chat_channel.jsonl",
        [
            {"body": "本日の申込、意向把握のメモが未記入なので確認してもらえますか。"},
            {"body": "承知しました、確認して午後に折り返します。"},
            {"body": "顧客への再説明は完了しました、記録も残しています。"},
            {"body": "解約希望のお客様には手続き案内を送付済みです。"},
            {"body": "月次の締め処理は問題なく完了しました。"},
            {"body": "本日のprobe対応は完了しました。"},  # this one leaks and gets dropped
        ],
    )
    (run_root / "world_ledger.jsonl").write_text("", encoding="utf-8")
    (run_root / "attempts.jsonl").write_text("", encoding="utf-8")

    packet, id_map = build_blind_review_packet([run_root], samples_per_run=10)
    assert id_map["dropped_count"] == 1
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    write_sme_blind_review_inputs(tmp_path, packet, id_map)

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is False
    assert payload["status"] == "blocked"
    assert "dropped_count=1" in payload["checks"][0]["detail"]
    assert "artifact detection" in payload["checks"][0]["detail"]
    assert payload["checks"][0]["dropped_count"] == 1


def test_sme_blind_review_report_labels_ai_proxy_as_internal_calibration(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet, id_map = build_blind_review_packet([run_root], samples_per_run=10, reviewer_type="ai_proxy")
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    write_sme_blind_review_inputs(tmp_path, packet, id_map)

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is True
    assert payload["reviewer_type"] == "ai_proxy"
    assert payload["claim_level"] == "internal_calibration"


def test_sme_blind_review_report_labels_human_sme_as_human_sme(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet, id_map = build_blind_review_packet(
        [run_root], samples_per_run=10, reviewer_type="human_sme", reviewer={"note": "blind reviewer, no prior context"}
    )
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    write_sme_blind_review_inputs(tmp_path, packet, id_map)

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is True
    assert payload["reviewer_type"] == "human_sme"
    assert payload["claim_level"] == "human_sme"
    assert payload["reviewer"] == {"note": "blind reviewer, no prior context"}


def test_build_blind_review_packet_rejects_unknown_reviewer_type(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    with pytest.raises(ValueError, match="reviewer_type"):
        build_blind_review_packet([run_root], reviewer_type="not_a_real_type")


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
    assert (tmp_path / "sme_blind_review_id_map.json").exists()
    reviewer_packet = json.loads((tmp_path / "sme_blind_review_inputs.json").read_text(encoding="utf-8"))
    assert run_root.name not in json.dumps(reviewer_packet, ensure_ascii=False)

    score_result = runner.invoke(app, ["sme-score", "--campaign-root", str(tmp_path)])
    assert score_result.exit_code == 1  # honest fail: packet is unfilled
    assert (tmp_path / "sme_blind_review.json").exists()


# ---------------------------------------------------------------------------
# Readiness integration + ungameability
# ---------------------------------------------------------------------------


def test_readiness_reports_wires_wp14_writers_end_to_end(tmp_path: Path) -> None:
    from company_twin.backcasting_run import BACKCASTING_RESULTS_SCHEMA_VERSION, JUDGE_PROMPT_VERSION, select_backcasting_sample

    design = _design()
    extraction = extract_backcasting_cases(design)
    write_backcasting_inputs(tmp_path, extraction)
    sample = select_backcasting_sample(extraction["cases"], sample_size=5, sample_seed=0)
    case_ids = sample["selected_case_ids"]
    (tmp_path / "backcasting_resimulation_results.json").write_text(
        json.dumps(
            {
                "schema_version": BACKCASTING_RESULTS_SCHEMA_VERSION,
                "sample": sample,
                "judge": {"backend": "openrouter", "model": "fake-openrouter-model", "prompt_version": JUDGE_PROMPT_VERSION, "readiness_eligible": True},
                "results": [{"case_id": cid, "reproduced": True, "viewed_doc_ids": ["DFH-SAL-021"]} for cid in case_ids],
            }
        ),
        encoding="utf-8",
    )

    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"], run_roots=["s2_holdout_0"])
    write_holdout_inputs(tmp_path, plan)
    _verified_s2_bundle(tmp_path / "s2_holdout_0", injection=plan["injections"][0], finding_types={plan["injections"][0]["expected_finding_types"][0]: 1})

    run_root = _fixture_run_bundle(tmp_path / "sme_run")
    packet, id_map = build_blind_review_packet([run_root])
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    write_sme_blind_review_inputs(tmp_path, packet, id_map)

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
