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
    ARM_DEFERRED_PRESSURE_DEPENDENT,
    ARM_POSITIVE_CONTROL,
    HOLDOUT_DETECTION_TARGET,
    DEFERRED_FINDING_TEXT,
    DEFERRED_PRE_REGISTRATION_REFERENCE,
    build_holdout_injection_plan,
    compute_holdout_detection_rate,
    score_benign_controls,
    score_deferred_injections,
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


def test_build_holdout_injection_plan_auto_run_roots_gives_one_to_one_attribution() -> None:
    plan = build_holdout_injection_plan(Path.cwd(), auto_run_roots=True)

    for injection in plan["injections"]:
        assert injection["planned_run_roots"] == [injection["injection_id"]]
    # One-to-one attribution must be sealed: two plans differing only in
    # auto_run_roots have different plan hashes.
    shared = build_holdout_injection_plan(Path.cwd())
    assert plan["plan_hash"] != shared["plan_hash"]


def test_build_holdout_injection_plan_rejects_auto_and_shared_run_roots_together() -> None:
    with pytest.raises(ValueError, match="not both"):
        build_holdout_injection_plan(Path.cwd(), run_roots=["some_root"], auto_run_roots=True)


_DEFAULT_TARGET_DOC_ID = "DFH-SAL-901"  # clarify_elderly_understanding_all's doc_id


def _activation_rule_hit(expected_finding_types: list[str], *, hit_types: dict[str, int] | None = None) -> dict[str, Any]:
    """Synthesize a rule_hit_rate block giving every expected_finding_type a
    genuine opportunity_count > 0 (activation-aware holdout protocol,
    MASTER_DESIGN.md section 17.9) -- one rule per finding_type, opportunity
    always > 0, hit_count from hit_types (defaults to 0)."""
    hit_types = hit_types or {}
    return {
        f"MON-ACTIVATION-{finding_type.upper()}": {
            "finding_type": finding_type,
            "opportunity_count": 3,
            "hit_count": int(hit_types.get(finding_type, 0)),
        }
        for finding_type in expected_finding_types
    }


def _write_attempts_exposing_doc(root: Path, *, doc_id: str, seat_id: str = "seat_sales_1") -> None:
    if not doc_id:
        (root / "attempts.jsonl").write_text("", encoding="utf-8")
        return
    row = {
        "ts": "2026-07-06T00:00:00+00:00",
        "run_id": "test",
        "tick": 1,
        "seat_id": seat_id,
        "tool": "read_document",
        "args": {"doc_id": doc_id},
        "success": True,
        "result": {"citation_handle": "H-1", "doc_id": doc_id, "version": "1.1"},
        "denied_reason": None,
        "origin": "agent",
    }
    (root / "attempts.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")


def _run_bundle_with_findings(
    root: Path,
    *,
    finding_types: dict[str, int],
    rule_hit: dict[str, Any] | None = None,
    expected_finding_types: list[str] | None = None,
    target_doc_id: str = _DEFAULT_TARGET_DOC_ID,
    activated: bool = True,
    mutation_id: str = "clarify_elderly_understanding_all",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "triage").mkdir(exist_ok=True)
    # Activation (2026-07-06 approved protocol, MASTER_DESIGN.md section
    # 17.9): give every expected_finding_type a genuine opportunity_count > 0
    # (defaulting to the finding_types passed in when expected_finding_types
    # isn't explicitly given, so existing single-type fixtures still activate
    # on their own finding_type) unless rule_hit is explicitly supplied by the
    # caller (some tests construct rule_hit_rate themselves for L1-only
    # evidence) or activated=False (dedicated unactivated-path tests).
    if rule_hit is None and activated:
        rule_hit = _activation_rule_hit(expected_finding_types or list(finding_types), hit_types=finding_types)
    (root / "triage" / "metrics.json").write_text(
        json.dumps({"stage": "S1", "finding_types": finding_types, "rule_hit_rate": rule_hit or {}, "detection_miss_rate": {}}),
        encoding="utf-8",
    )
    (root / "meta.json").write_text(json.dumps({"stage": "S1", "mutation_ids": [mutation_id]}), encoding="utf-8")
    _write_attempts_exposing_doc(root, doc_id=target_doc_id if activated else "")


def _verified_s2_bundle(
    root: Path,
    *,
    injection: dict[str, Any],
    finding_types: dict[str, int],
    planned_ticks: int = 4,
    activated: bool = True,
    rule_hit: dict[str, Any] | None = None,
) -> None:
    """Build a run bundle that passes holdout bundle-attribution verification
    (verify_holdout_bundles): stage S2, config.json mutation entries matching
    the injection's spec_hash/mutation_id, and world_ledger tick coverage.

    By default (activated=True) this also builds ACTIVATION evidence
    (2026-07-06 approved protocol, MASTER_DESIGN.md section 17.9): a
    successful read_document attempt on the injection's target_doc_id
    (exposure) plus a rule_hit_rate giving every expected_finding_type an
    opportunity_count > 0 (opportunity). Pass activated=False to build an
    unactivated bundle (e.g. for the zero-activation-fails test path)."""
    from company_twin.world_config import _json_hash as world_json_hash

    root.mkdir(parents=True, exist_ok=True)
    (root / "triage").mkdir(exist_ok=True)
    if rule_hit is None and activated:
        rule_hit = _activation_rule_hit(list(injection.get("expected_finding_types") or []), hit_types=finding_types)
    (root / "triage" / "metrics.json").write_text(
        json.dumps({"stage": "S2", "finding_types": finding_types, "rule_hit_rate": rule_hit or {}, "detection_miss_rate": {}}),
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
    target_doc_id = str(injection.get("target_doc_id") or "")
    _write_attempts_exposing_doc(root, doc_id=target_doc_id if activated else "")


def test_compute_holdout_detection_rate_counts_l0_and_l1_evidence(tmp_path: Path) -> None:
    # clarify_elderly_understanding_sales_only (not _all -- reclassified
    # benign_control per MASTER_DESIGN.md section 17.11) is used here as the
    # positive_control clarify example; its target_doc_id is DFH-SAL-902.
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_sales_only", "dangling_fill_search_key_stub"])
    # grounding_gap is in the pre-registered expected_finding_types for both
    # clarify and dangling_fill, so this is a strict hit for the matching one.
    _run_bundle_with_findings(tmp_path / "s1_run0", finding_types={"grounding_gap": 2}, target_doc_id="DFH-SAL-902", mutation_id="clarify_elderly_understanding_sales_only")

    measurement = compute_holdout_detection_rate(tmp_path, plan)

    assert measurement["detection_rate_basis"] == "strict"
    assert measurement["injection_count"] == 2
    assert measurement["detected_count"] == 1
    assert measurement["detection_rate"] == 0.5
    assert measurement["strict_detection_rate"] == 0.5
    assert measurement["lenient_detection_rate"] == 0.5
    detected_row = next(row for row in measurement["per_injection"] if row["mutation_id"] == "clarify_elderly_understanding_sales_only")
    assert detected_row["detected"] is True
    assert detected_row["strict_detected"] is True
    assert detected_row["lenient_detected"] is True
    assert detected_row["l0_finding_types"] == ["grounding_gap"]
    undetected_row = next(row for row in measurement["per_injection"] if row["mutation_id"] == "dangling_fill_search_key_stub")
    assert undetected_row["detected"] is False
    assert undetected_row["reason"]


def test_compute_holdout_detection_rate_counts_l1_only_evidence(tmp_path: Path) -> None:
    """contradict_chat_approval_recorded is deferred_pressure_dependent as of
    MASTER_DESIGN.md section 17.16 (approval #7, pre-registered before
    era-6), so it is excluded from the positive-control strict denominator --
    detected_count/strict_detected_count/lenient_detected_count (all
    positive-control-only fields) are 0 here even though the raw evidence
    would have been a strict hit. The raw per-injection evidence itself
    (l1_monitoring_rules/l1_finding_types/strict_detected) is unaffected by
    arm -- _score_injection scores every injection's evidence the same way
    regardless of arm; only the denominator/official counts change."""
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["contradict_chat_approval_recorded"])
    _run_bundle_with_findings(
        tmp_path / "s1_run0",
        finding_types={},
        rule_hit={"MON-SAME-SUBMITTER-APPROVER": {"finding_type": "sod_pattern", "hit_count": 1, "opportunity_count": 2}},
        target_doc_id="DFH-SAL-903",  # contradict_chat_approval_recorded's doc_id -- exposure evidence
    )
    (tmp_path / "s1_run0" / "meta.json").write_text(
        json.dumps({"stage": "S1", "mutation_ids": ["contradict_chat_approval_recorded"]}), encoding="utf-8"
    )

    measurement = compute_holdout_detection_rate(tmp_path, plan)

    # Excluded from the positive-control denominator -> official counts are 0.
    assert measurement["injection_count"] == 0
    assert measurement["detected_count"] == 0
    assert measurement["strict_detected_count"] == 0
    assert measurement["lenient_detected_count"] == 0
    assert measurement["deferred_count"] == 1
    # sod_pattern is in contradict's pre-registered expected_finding_types, so
    # the L1-only hit is STILL a strict hit in the raw itemized evidence --
    # deferral doesn't hide or alter the evidence, it just doesn't count it.
    assert measurement["per_injection"][0]["arm"] == "deferred_pressure_dependent"
    assert measurement["per_injection"][0]["strict_detected"] is True
    assert measurement["per_injection"][0]["l1_monitoring_rules"] == ["MON-SAME-SUBMITTER-APPROVER"]
    assert measurement["per_injection"][0]["l1_finding_types"] == ["sod_pattern"]


def test_compute_holdout_detection_rate_unrelated_finding_counts_lenient_not_strict(tmp_path: Path) -> None:
    """Ungameability: an unrelated finding_type on a mutated run inflates the
    lenient rate but must NOT count as a strict hit."""
    # clarify_elderly_understanding_sales_only (not _all -- reclassified
    # benign_control per MASTER_DESIGN.md section 17.11) is used here as the
    # positive_control clarify example.
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_sales_only"])
    expected_finding_types = plan["injections"][0]["expected_finding_types"]
    # deadline_overrun has nothing to do with the clarify mutation's
    # pre-registered expectation (grounding_gap/version_gap/version_mix); it
    # is an unrelated finding merely co-occurring on the mutated run. The run
    # is activated (exposure + a genuine opportunity for the real expected
    # types) so this exercises the "activated but wrong finding" strict_reason
    # path, not the zero-activation path.
    _run_bundle_with_findings(
        tmp_path / "s1_run0",
        finding_types={"deadline_overrun": 3},
        expected_finding_types=expected_finding_types,
        target_doc_id="DFH-SAL-902",
        mutation_id="clarify_elderly_understanding_sales_only",
    )

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
    # 2026-07-06 approved holdout pressure-dependent deferral (MASTER_DESIGN.md
    # section 17.16, approval #7, pre-registered before era-6): only TWO of
    # these five mutations are positive_control now (
    # clarify_elderly_understanding_sales_only, dangling_fill) --
    # clarify_elderly_understanding_all and role_table_fix_quality_owner are
    # both benign_control, and contradict_chat_approval_recorded is now
    # deferred_pressure_dependent. Zero of the two positive-control mutations
    # produce a matching run bundle with findings here -> 0.0 < 0.80 target.
    failing = write_holdout_report(tmp_path)
    assert failing["passed"] is False
    assert failing["measurement"]["injection_count"] == 2  # positive_control only
    assert failing["measurement"]["deferred_count"] == 1
    assert failing["measurement"]["detection_rate"] < HOLDOUT_DETECTION_TARGET
    assert failing["measurement"]["strict_detection_rate"] < HOLDOUT_DETECTION_TARGET
    assert failing["detection_rate_basis"] == "strict"
    assert failing["deferred_injections"] is not None
    assert failing["deferred_injections"]["injection_count"] == 1

    # Now supply matching run bundles for all five mutations. The two
    # positive_control mutations each produce a finding_type that is actually
    # in that mutation's own pre-registered expected_finding_types (not a
    # blanket grounding_gap) -> strict rate 1.0. Both benign_control
    # mutations get a CLEAN bundle (no findings at all) -- a benign_control
    # injection is expected to produce nothing new, so a clean bundle is what
    # "passing" looks like for it, not an injected finding. The
    # deferred_pressure_dependent mutation gets an UNACTIVATED bundle (no
    # opportunity) -- exactly era-6's confirmed finding for
    # contradict_chat_approval_recorded (exposure without opportunity) -- and
    # this must not block the report, since it is excluded from the gate.
    # Every bundle is verified (stage S2, config.json mutation entry matching
    # spec_hash, adequate tick coverage, no failure marker).
    run_lookup = {}
    for idx, injection in enumerate(plan["injections"]):
        run_root = tmp_path / f"s2_holdout_{idx}"
        if injection["arm"] == "benign_control":
            _verified_s2_bundle(run_root, injection=injection, finding_types={})
        elif injection["arm"] == "deferred_pressure_dependent":
            _verified_s2_bundle(run_root, injection=injection, finding_types={}, activated=False)
        else:
            finding_type = injection["expected_finding_types"][0]
            _verified_s2_bundle(run_root, injection=injection, finding_types={finding_type: 1})
        run_lookup[injection["injection_id"]] = run_root

    passing = write_holdout_report(tmp_path, run_lookup=run_lookup)
    assert passing["passed"] is True
    assert passing["measurement"]["detection_rate"] == 1.0
    assert passing["measurement"]["strict_detection_rate"] == 1.0
    assert passing["measurement"]["lenient_detection_rate"] == 1.0
    assert passing["measurement"]["injection_count"] == 2  # positive_control only
    assert len(passing["checks"][0]["per_injection"]) == 5  # all arms still itemized
    assert passing["bundle_verification"]["all_verified"] is True
    assert passing["benign_controls"]["all_passed"] is True
    assert passing["benign_controls"]["injection_count"] == 2
    assert passing["deferred_injections"]["injection_count"] == 1
    assert passing["deferred_injections"]["per_injection"][0]["detected"] is False
    assert passing["deferred_injections"]["per_injection"][0]["confirmed_finding"]
    assert passing["deferred_injections"]["per_injection"][0]["pre_registration_reference"]
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
    # clarify_elderly_understanding_sales_only (not _all -- reclassified
    # benign_control per MASTER_DESIGN.md section 17.11) is used here as the
    # positive_control example, so the positive-only verification gate is
    # actually exercised.
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_sales_only"])  # no run_roots -> exploration
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
    # clarify_elderly_understanding_sales_only (not _all -- reclassified
    # benign_control per MASTER_DESIGN.md section 17.11) is used here as the
    # positive_control example.
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_sales_only"], run_roots=["s2_holdout_0"])
    write_holdout_inputs(tmp_path, plan)
    injection = plan["injections"][0]
    _verified_s2_bundle(tmp_path / "s2_holdout_0", injection=injection, finding_types={injection["expected_finding_types"][0]: 1})

    report = write_holdout_report(tmp_path, run_lookup={injection["injection_id"]: tmp_path / "s2_holdout_0"})

    assert report["passed"] is True  # missing controls never auto-fails
    assert report["controls"] is None
    assert any("no controls section" in note.lower() for note in report["notes"])


def test_holdout_report_controls_records_anomalous_hits_without_failing(tmp_path: Path) -> None:
    # clarify_elderly_understanding_sales_only (not _all -- reclassified
    # benign_control per MASTER_DESIGN.md section 17.11) is used here as the
    # positive_control example.
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_sales_only"], run_roots=["s2_holdout_0"])
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
    """2026-07-06 approved holdout pressure-dependent deferral (MASTER_DESIGN.md
    section 17.16, approval #7, pre-registered before era-6): arm assignment
    is per-mutation_id. positive_control = {dangling_fill_search_key_stub,
    clarify_elderly_understanding_sales_only}; benign_control =
    {clarify_elderly_understanding_all, role_table_fix_quality_owner};
    deferred_pressure_dependent = {contradict_chat_approval_recorded}."""
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

    arms_by_mutation = {injection["mutation_id"]: injection["arm"] for injection in plan["injections"]}
    assert arms_by_mutation["contradict_chat_approval_recorded"] == ARM_DEFERRED_PRESSURE_DEPENDENT
    assert arms_by_mutation["dangling_fill_search_key_stub"] == ARM_POSITIVE_CONTROL
    assert arms_by_mutation["clarify_elderly_understanding_sales_only"] == ARM_POSITIVE_CONTROL
    assert arms_by_mutation["clarify_elderly_understanding_all"] == ARM_BENIGN_CONTROL
    assert arms_by_mutation["role_table_fix_quality_owner"] == ARM_BENIGN_CONTROL

    positive_count = sum(1 for arm in arms_by_mutation.values() if arm == ARM_POSITIVE_CONTROL)
    assert positive_count == 2  # positive denominator = 2 under the deferred mapping
    deferred_count = sum(1 for arm in arms_by_mutation.values() if arm == ARM_DEFERRED_PRESSURE_DEPENDENT)
    assert deferred_count == 1


def test_arm_by_mutation_id_override_takes_precedence_over_operator_default() -> None:
    """_resolve_arm's per-mutation_id override (MASTER_DESIGN.md section
    17.11) must take precedence over the operator-level default -- this is
    exactly why clarify's two variants can now get different arms even though
    they share the same operator ("clarify")."""
    from company_twin.holdout import _ARM_BY_MUTATION_ID, _default_arm_for_operator, _resolve_arm

    # Both variants share the "clarify" operator, whose OWN operator-level
    # default is positive_control...
    assert _default_arm_for_operator("clarify") == ARM_POSITIVE_CONTROL
    # ...but the per-mutation_id override makes them diverge.
    assert _resolve_arm("clarify_elderly_understanding_all", "clarify") == ARM_BENIGN_CONTROL
    assert _resolve_arm("clarify_elderly_understanding_sales_only", "clarify") == ARM_POSITIVE_CONTROL
    # A mutation_id with no override falls back to the operator default.
    assert "some_future_clarify_variant" not in _ARM_BY_MUTATION_ID
    assert _resolve_arm("some_future_clarify_variant", "clarify") == ARM_POSITIVE_CONTROL
    # role_table_fix's override agrees with its operator default (redundant
    # but present for explicitness/documentation).
    assert _resolve_arm("role_table_fix_quality_owner", "role_table_fix") == ARM_BENIGN_CONTROL


def test_holdout_plan_arm_is_sealed_in_plan_hash() -> None:
    # clarify_elderly_understanding_sales_only is positive_control (see
    # MASTER_DESIGN.md section 17.11), so tampering it to benign_control below
    # actually changes the arm.
    plan_a = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_sales_only"])
    plan_b = json.loads(json.dumps(plan_a))
    plan_b["injections"][0]["arm"] = ARM_BENIGN_CONTROL

    # plan_hash was computed over the ORIGINAL arm; recomputing it over the
    # tampered copy's injections must differ, i.e. plan_hash is sensitive to
    # the arm field (it is part of what plan_hash seals).
    from company_twin.world_config import _json_hash as world_json_hash

    original_hash = world_json_hash(
        {"injections": plan_a["injections"], "control_run_roots": plan_a["control_run_roots"], "circulation_required": plan_a["circulation_required"]}
    )
    tampered_hash = world_json_hash(
        {"injections": plan_b["injections"], "control_run_roots": plan_b["control_run_roots"], "circulation_required": plan_b["circulation_required"]}
    )
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
    # clarify_elderly_understanding_sales_only (not _all -- reclassified
    # benign_control per MASTER_DESIGN.md section 17.11) is used here as the
    # positive_control example.
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["clarify_elderly_understanding_sales_only", "role_table_fix_quality_owner"],
    )
    # Only the positive_control mutation gets a matching, detected run bundle.
    _run_bundle_with_findings(
        tmp_path / "s1_run0",
        finding_types={"grounding_gap": 1},
        target_doc_id="DFH-SAL-902",
        mutation_id="clarify_elderly_understanding_sales_only",
    )

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
    # clarify_elderly_understanding_sales_only (not _all -- reclassified
    # benign_control per MASTER_DESIGN.md section 17.11) is used here as the
    # positive_control example.
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["clarify_elderly_understanding_sales_only"],
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
    # clarify_elderly_understanding_sales_only (not _all -- reclassified
    # benign_control per MASTER_DESIGN.md section 17.11) is used here as the
    # positive_control example.
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["clarify_elderly_understanding_sales_only"],
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
    # clarify_elderly_understanding_sales_only (not _all -- reclassified
    # benign_control per MASTER_DESIGN.md section 17.11) is used here as the
    # positive_control example paired with role_table_fix's benign_control.
    plan = build_holdout_injection_plan(
        Path.cwd(), mutation_ids=["clarify_elderly_understanding_sales_only", "role_table_fix_quality_owner"], control_run_roots=[]
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
    """A benign_control (role_table_fix) run that fires one of its
    previously-expected anomaly types ABOVE the (zero, no-controls-supplied)
    baseline must fail -- unlike a positive_control, nothing new should
    appear here. (role_table_fix's own expected types fire zero on every
    observed run in practice, so ANY firing here is above baseline; this is
    distinct from clarify_elderly_understanding_all's endemic-at-baseline
    case, which is why clarify_all -- not role_table_fix -- was reclassified
    to use the adjusted at-or-below-baseline criterion; see
    test_benign_control_above_baseline_without_bare_presence_still_fails for
    a case that fires but stays at baseline.)"""
    # clarify_elderly_understanding_sales_only (not _all -- reclassified
    # benign_control per MASTER_DESIGN.md section 17.11) is used here as the
    # positive_control example paired with role_table_fix's benign_control.
    plan = build_holdout_injection_plan(
        Path.cwd(), mutation_ids=["clarify_elderly_understanding_sales_only", "role_table_fix_quality_owner"], control_run_roots=[]
    )
    write_holdout_inputs(tmp_path, plan)
    positive = next(i for i in plan["injections"] if i["arm"] == ARM_POSITIVE_CONTROL)
    benign = next(i for i in plan["injections"] if i["arm"] == ARM_BENIGN_CONTROL)
    _verified_s2_bundle(tmp_path / "s2_positive", injection=positive, finding_types={positive["expected_finding_types"][0]: 1})
    # Benign control fires its own expected finding_type -- a false alarm,
    # and with no control_run_roots supplied the baseline is 0, so this is
    # also above baseline.
    _verified_s2_bundle(tmp_path / "s2_benign", injection=benign, finding_types={benign["expected_finding_types"][0]: 1})
    run_lookup = {positive["injection_id"]: tmp_path / "s2_positive", benign["injection_id"]: tmp_path / "s2_benign"}

    benign_result = score_benign_controls(tmp_path, plan, run_lookup=run_lookup)

    assert benign_result["all_passed"] is False
    assert benign_result["per_injection"][0]["passed"] is False
    assert benign["expected_finding_types"][0] in benign_result["per_injection"][0]["false_alarm_finding_types"]
    assert benign["expected_finding_types"][0] in benign_result["per_injection"][0]["above_baseline_finding_types"]

    report = write_holdout_report(tmp_path, run_lookup=run_lookup)
    # Even though positive_control's own strict_detection_rate clears target,
    # the benign_control above-baseline finding blocks the overall report --
    # an above-baseline anomaly on a benign_control run is evidence the
    # detectors are unreliable.
    assert report["passed"] is False
    assert any("benign_controls FAILED" in note for note in report["notes"])


def test_benign_control_fires_but_stays_at_baseline_passes(tmp_path: Path) -> None:
    """2026-07-06 approved benign criterion adjustment (MASTER_DESIGN.md
    section 17.11): a benign_control run whose expected finding_type DOES
    fire, but at a rate that does NOT exceed the sealed no-mutation control
    baseline, now PASSES -- this is exactly clarify_elderly_understanding_all's
    empirical situation (its expected types are endemic in no-mutation
    controls). This replaces the prior stricter "none fire at all" clause."""
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["clarify_elderly_understanding_all"],
        control_run_roots=["s2_control_anchor"],
    )
    write_holdout_inputs(tmp_path, plan)
    benign = plan["injections"][0]
    assert benign["arm"] == ARM_BENIGN_CONTROL
    finding_type = benign["expected_finding_types"][0]
    # Benign bundle: the expected finding_type fires once.
    _verified_s2_bundle(tmp_path / "s2_benign", injection=benign, finding_types={finding_type: 1})
    # Control run: the SAME finding_type fires at an equal (not lower) rate --
    # i.e. this is endemic-at-baseline noise, not a NEW anomaly.
    control_root = tmp_path / "s2_control_anchor"
    control_root.mkdir(parents=True, exist_ok=True)
    (control_root / "triage").mkdir(exist_ok=True)
    (control_root / "triage" / "metrics.json").write_text(
        json.dumps({"stage": "S2", "finding_types": {finding_type: 1}, "rule_hit_rate": {}, "detection_miss_rate": {}}),
        encoding="utf-8",
    )
    run_lookup = {benign["injection_id"]: tmp_path / "s2_benign"}

    benign_result = score_benign_controls(tmp_path, plan, run_lookup=run_lookup)

    assert benign_result is not None
    row = benign_result["per_injection"][0]
    # It DID fire (visible in false_alarm_finding_types) ...
    assert finding_type in row["false_alarm_finding_types"]
    # ... but did not exceed baseline, so it is NOT in above_baseline_finding_types ...
    assert finding_type not in row["above_baseline_finding_types"]
    assert finding_type in row["at_or_below_baseline_finding_types"]
    # ... and therefore still PASSES under the adjusted criterion.
    assert row["passed"] is True
    assert benign_result["all_passed"] is True
    assert row["visibility_note"]  # non-blocking note is still surfaced


def test_positive_control_denominator_excludes_benign_arm_end_to_end(tmp_path: Path) -> None:
    """2026-07-06 approved holdout pressure-dependent deferral (MASTER_DESIGN.md
    section 17.16, approval #7, pre-registered before era-6): positive
    denominator = 2 under the deferred mapping
    (clarify_elderly_understanding_sales_only, dangling_fill); benign = 2
    (clarify_elderly_understanding_all, role_table_fix); deferred = 1
    (contradict)."""
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
    assert len(positive_injections) == 2
    benign_injections = [i for i in plan["injections"] if i["arm"] == ARM_BENIGN_CONTROL]
    assert len(benign_injections) == 2
    deferred_injections = [i for i in plan["injections"] if i["arm"] == ARM_DEFERRED_PRESSURE_DEPENDENT]
    assert len(deferred_injections) == 1

    measurement = compute_holdout_detection_rate(tmp_path, plan)
    assert measurement["injection_count"] == 2
    assert measurement["total_injection_count"] == 5
    assert measurement["benign_control_count"] == 2
    assert measurement["deferred_count"] == 1


# ---------------------------------------------------------------------------
# 2026-07-06 approved holdout pressure-dependent deferral (MASTER_DESIGN.md
# section 17.16, approval #7 -- PRE-REGISTERED before era-6 was launched)
# ---------------------------------------------------------------------------
#
# Pre-registration context: the project owner approved (2026-07-06, approval
# #7) the conditional rule BEFORE era-6 ran: "if seat behavior remains
# unchanged even with full-text delivery of the enabling notice, the finding
# 'notices alone do not change behavior without pressure' stands, and the
# contradict class defers to phase-3 D1 (time-pressure) validation." Era-6
# then confirmed the condition: contradict_chat_approval_recorded had
# exposure (full-text circular delivered) in all 5 seeds but ZERO
# opportunity (activation 0/5), while clarify_elderly_understanding_sales_only
# and dangling_fill_search_key_stub both activated and were strictly
# detected (1/1 each), and both benign controls passed.


def test_deferred_arm_is_sealed_in_plan_hash() -> None:
    """contradict_chat_approval_recorded's arm (deferred_pressure_dependent)
    must be sealed into plan_hash exactly like positive_control/
    benign_control -- tampering it after the fact must change the hash."""
    plan_a = build_holdout_injection_plan(Path.cwd(), mutation_ids=["contradict_chat_approval_recorded"])
    assert plan_a["injections"][0]["arm"] == ARM_DEFERRED_PRESSURE_DEPENDENT

    plan_b = json.loads(json.dumps(plan_a))
    plan_b["injections"][0]["arm"] = ARM_POSITIVE_CONTROL

    from company_twin.world_config import _json_hash as world_json_hash

    original_hash = world_json_hash(
        {"injections": plan_a["injections"], "control_run_roots": plan_a["control_run_roots"], "circulation_required": plan_a["circulation_required"]}
    )
    tampered_hash = world_json_hash(
        {"injections": plan_b["injections"], "control_run_roots": plan_b["control_run_roots"], "circulation_required": plan_b["circulation_required"]}
    )
    assert original_hash == plan_a["plan_hash"]
    assert tampered_hash != plan_a["plan_hash"]


def _verified_s2_bundle_exposed_via_circulation_zero_opportunity(root: Path, *, injection: dict[str, Any], planned_ticks: int = 4) -> None:
    """Build a run bundle matching era-6's confirmed contradict result:
    EXPOSED (full-text circulation delivered this injection's circular to a
    seat) but ZERO OPPORTUNITY (no chat-approval behavior, no approval
    requests -- opportunity_count=0 for every expected finding type). This is
    exposure-without-opportunity, i.e. NOT activated, but for a different
    reason than a plain "nothing happened at all" unactivated bundle: the
    stimulus genuinely reached the world (delivery recorded), there was just
    nothing for a detector to fire on."""
    from company_twin.world_config import _json_hash as world_json_hash

    root.mkdir(parents=True, exist_ok=True)
    (root / "triage").mkdir(exist_ok=True)
    (root / "triage" / "metrics.json").write_text(
        json.dumps({"stage": "S2", "finding_types": {}, "rule_hit_rate": {}, "detection_miss_rate": {}}),
        encoding="utf-8",
    )
    mutation_id = injection["mutation_id"]
    target_doc_id = str(injection.get("target_doc_id") or "")
    spec = load_mutation_catalog(Path.cwd())[mutation_id]
    assert world_json_hash(spec) == injection["spec_hash"]
    mutation_entry = dict(spec)
    message = f"本日付の事務連絡を回覧します: 「{mutation_id}のテスト通知」\n本文のテキストです。"
    (root / "config.json").write_text(
        json.dumps(
            {
                "world": {
                    "corpus": {
                        "mutations": [mutation_entry],
                        "mutation_hash": world_json_hash([mutation_entry]),
                        "effective_corpus_hash": "test-hash",
                        "circulation": {
                            "enabled": True,
                            "mode": "full_text",
                            "announcements": [
                                {
                                    "mutation_id": mutation_id,
                                    "doc_id": target_doc_id,
                                    "tick": 1,
                                    "visible_roles": ["sales"],
                                    "message": message,
                                    "digest": message,
                                }
                            ],
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (root / "meta.json").write_text(json.dumps({"stage": "S2", "mutation_ids": [mutation_id]}), encoding="utf-8")
    ledger_rows = [{"tick": tick, "event_type": "tick_committed"} for tick in range(1, planned_ticks + 1)]
    ledger_rows.append(
        {
            "tick": 1,
            "event_type": "inbox_delivered",
            "payload": {"to_seat": "seat_sales_1", "message": {"kind": "timed_notice", "tick": 1, "notice": "document_circulation", "detail": message}},
        }
    )
    (root / "world_ledger.jsonl").write_text("".join(json.dumps(row) + "\n" for row in ledger_rows), encoding="utf-8")
    # No approval-adjacent behavior at all: no read_document attempts, no
    # chat-approval activity -- this is the "no opportunity" half of era-6's
    # finding.
    (root / "attempts.jsonl").write_text("", encoding="utf-8")


def _era6_style_plan_and_bundles(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    """Build a plan + run bundles reproducing era-6's confirmed result:
    contradict_chat_approval_recorded exposed but with zero opportunity in
    all 5 seeds (0/5 activation); clarify_elderly_understanding_sales_only and
    dangling_fill_search_key_stub both activated and strictly detected (1/1
    each); both benign controls pass."""
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=[
            "clarify_elderly_understanding_all",
            "clarify_elderly_understanding_sales_only",
            "contradict_chat_approval_recorded",
            "dangling_fill_search_key_stub",
            "role_table_fix_quality_owner",
        ],
        auto_run_roots=True,
        seeds_per_injection={"contradict_chat_approval_recorded": 5, "_default": 1},
    )
    write_holdout_inputs(tmp_path, plan)
    by_mutation = {injection["mutation_id"]: injection for injection in plan["injections"]}
    run_lookup: dict[str, Path] = {}

    contradict = by_mutation["contradict_chat_approval_recorded"]
    assert contradict["arm"] == ARM_DEFERRED_PRESSURE_DEPENDENT
    assert len(contradict["planned_run_roots"]) == 5
    for seed, root_name in enumerate(contradict["planned_run_roots"], start=1):
        run_root = tmp_path / root_name
        # Exposed (full-text circular delivered) but zero opportunity: era-6's
        # confirmed exposure-without-opportunity result, reproduced across all
        # 5 seeds.
        _verified_s2_bundle_exposed_via_circulation_zero_opportunity(run_root, injection=contradict)
        run_lookup[f"holdout_contradict_chat_approval_recorded_seed{seed}"] = run_root

    clarify_sales = by_mutation["clarify_elderly_understanding_sales_only"]
    clarify_root = tmp_path / "s2_clarify_sales"
    _verified_s2_bundle(clarify_root, injection=clarify_sales, finding_types={clarify_sales["expected_finding_types"][0]: 1})
    run_lookup[clarify_sales["injection_id"]] = clarify_root

    dangling = by_mutation["dangling_fill_search_key_stub"]
    dangling_root = tmp_path / "s2_dangling"
    _verified_s2_bundle(dangling_root, injection=dangling, finding_types={dangling["expected_finding_types"][0]: 1})
    run_lookup[dangling["injection_id"]] = dangling_root

    clarify_all = by_mutation["clarify_elderly_understanding_all"]
    clarify_all_root = tmp_path / "s2_clarify_all"
    _verified_s2_bundle(clarify_all_root, injection=clarify_all, finding_types={})
    run_lookup[clarify_all["injection_id"]] = clarify_all_root

    role_table = by_mutation["role_table_fix_quality_owner"]
    role_table_root = tmp_path / "s2_role_table"
    _verified_s2_bundle(role_table_root, injection=role_table, finding_types={})
    run_lookup[role_table["injection_id"]] = role_table_root

    return plan, run_lookup


def test_deferred_injection_excluded_from_denominator_two_of_two_passes(tmp_path: Path) -> None:
    """Reproduces era-6: with contradict deferred, the positive-control
    denominator is 2 (clarify_sales_only, dangling_fill), both strictly
    detected -> 2/2 = 1.0, clearing the 0.80 target, even though
    contradict's 5 seeds all show zero activation."""
    plan, run_lookup = _era6_style_plan_and_bundles(tmp_path)

    measurement = compute_holdout_detection_rate(tmp_path, plan, run_lookup=run_lookup)

    assert measurement["injection_count"] == 2
    assert measurement["detected_count"] == 2
    assert measurement["detection_rate"] == 1.0
    assert measurement["strict_detection_rate"] == 1.0
    assert measurement["passed"] is True
    assert measurement["deferred_count"] == 1
    assert measurement["benign_control_count"] == 2
    # contradict never counts as a positive-control unactivated failure --
    # it isn't in the positive-control denominator at all.
    assert measurement["unactivated_positive_control_count"] == 0

    contradict_rows = [row for row in measurement["per_injection"] if row["mutation_id"] == "contradict_chat_approval_recorded"]
    assert len(contradict_rows) == 1
    assert contradict_rows[0]["arm"] == ARM_DEFERRED_PRESSURE_DEPENDENT
    assert contradict_rows[0]["activation_summary"]["activated_trials"] == 0
    assert contradict_rows[0]["activation_summary"]["total_trials"] == 5


def test_deferred_injections_section_carries_activation_evidence_and_pre_registration(tmp_path: Path) -> None:
    """The deferred_injections report section must carry: activation evidence
    across all trials, the confirmed finding text, and the pre-registration
    reference -- and must NEVER mark the injection as detected."""
    plan, run_lookup = _era6_style_plan_and_bundles(tmp_path)

    deferred = score_deferred_injections(tmp_path, plan, run_lookup=run_lookup)

    assert deferred is not None
    assert deferred["injection_count"] == 1
    assert deferred["confirmed_finding"] == DEFERRED_FINDING_TEXT
    assert deferred["pre_registration_reference"] == DEFERRED_PRE_REGISTRATION_REFERENCE
    row = deferred["per_injection"][0]
    assert row["mutation_id"] == "contradict_chat_approval_recorded"
    assert row["arm"] == ARM_DEFERRED_PRESSURE_DEPENDENT
    assert row["deferred"] is True
    assert row["detected"] is False  # deferral NEVER counts as detected
    assert row["confirmed_finding"] == DEFERRED_FINDING_TEXT
    assert row["pre_registration_reference"] == DEFERRED_PRE_REGISTRATION_REFERENCE
    # activation evidence across all 5 trials is present and visible, not hidden.
    assert row["activation_summary"]["total_trials"] == 5
    assert row["activation_summary"]["activated_trials"] == 0
    assert row["activation_summary"]["any_activated"] is False
    assert len(row["evidence"]["activation"]["per_run"]) == 5
    for trial in row["evidence"]["activation"]["per_run"]:
        assert trial["activated"] is False
        assert trial["exposure"]["exposed"] is True  # exposed...
        assert trial["opportunity"]["has_opportunity"] is False  # ...but zero opportunity

    # End-to-end via write_holdout_report: the section is present, visible,
    # and the report still passes (deferred injections don't block the gate).
    report = write_holdout_report(tmp_path, run_lookup=run_lookup)
    assert report["passed"] is True
    assert report["deferred_injections"] is not None
    assert report["deferred_injections"]["injection_count"] == 1
    assert any("deferred" in note.lower() for note in report["notes"])
    assert report["scoring_note"]  # re-sealed-plan scoring note is populated
    assert "RE-SEALED" in report["scoring_note"]


def test_deferred_injection_zero_activation_does_not_trigger_activation_warning(tmp_path: Path) -> None:
    """A deferred_pressure_dependent injection's zero activation is the
    EXPECTED/confirmed finding, not a surprise -- it must not appear in the
    activation section's unactivated_injection_ids (that list is reserved for
    positive_control injections, whose zero activation is a genuine gap)."""
    plan, run_lookup = _era6_style_plan_and_bundles(tmp_path)

    report = write_holdout_report(tmp_path, run_lookup=run_lookup)

    assert "holdout_contradict_chat_approval_recorded" not in report["activation"]["unactivated_injection_ids"]
    assert report["activation"]["unactivated_injection_ids"] == []


def test_old_sealed_plan_positive_control_arm_unchanged_for_contradict(tmp_path: Path) -> None:
    """Backward compatibility (MASTER_DESIGN.md section 17.16): an EXISTING
    sealed plan that already lists contradict_chat_approval_recorded as
    positive_control (simulating a plan sealed BEFORE this change) continues
    to score under that ORIGINAL sealed arm -- old plans are not
    retroactively reinterpreted. Only a plan built AFTER this change (a
    fresh build_holdout_injection_plan call) gets the new deferred default."""
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["contradict_chat_approval_recorded"])
    # Simulate an old plan sealed before this change: force the arm back to
    # positive_control and recompute plan_hash the way the old code would have.
    from company_twin.world_config import _json_hash as world_json_hash

    old_plan = json.loads(json.dumps(plan))
    old_plan["injections"][0]["arm"] = ARM_POSITIVE_CONTROL
    old_plan["plan_hash"] = world_json_hash(
        {"injections": old_plan["injections"], "control_run_roots": old_plan["control_run_roots"], "circulation_required": old_plan["circulation_required"]}
    )
    write_holdout_inputs(tmp_path, old_plan)
    injection = old_plan["injections"][0]
    run_root = tmp_path / "s2_contradict_old"
    finding_type = injection["expected_finding_types"][0]
    _verified_s2_bundle(run_root, injection=injection, finding_types={finding_type: 1})

    measurement = compute_holdout_detection_rate(tmp_path, old_plan, run_lookup={injection["injection_id"]: run_root})

    # Old plan's sealed arm (positive_control) is honored -- it counts toward
    # the positive-control denominator, unlike a freshly-built plan.
    assert measurement["injection_count"] == 1
    assert measurement["deferred_count"] == 0
    assert measurement["per_injection"][0]["arm"] == ARM_POSITIVE_CONTROL
    assert measurement["detected_count"] == 1
    assert measurement["passed"] is True

    report = write_holdout_report(tmp_path, run_lookup={injection["injection_id"]: run_root})
    assert report["deferred_injections"] is None  # nothing deferred in this old plan
    # scoring_note explains this plan predates the deferred-arm rule.
    assert report["scoring_note"]
    assert "SEALED BEFORE" in report["scoring_note"]


def test_external_claim_readiness_deferred_item_false_when_deferred_class_present(tmp_path: Path) -> None:
    """external_claim_readiness's holdout_deferred_classes_validated item must
    be False whenever the holdout report records a deferred class (era-6's
    contradict_chat_approval_recorded is, by construction, an unresolved
    external claim pending phase-3 D1 validation)."""
    plan, run_lookup = _era6_style_plan_and_bundles(tmp_path)
    write_holdout_report(tmp_path, run_lookup=run_lookup, control_run_roots=[])

    from company_twin.readiness import build_external_claim_readiness_summary

    summary = build_external_claim_readiness_summary(tmp_path)

    item = next(item for item in summary["items"] if item["item"] == "holdout_deferred_classes_validated")
    assert item["passed"] is False
    assert "contradict_chat_approval_recorded" in item["detail"]
    assert item["deferred_class_count"] == 1
    assert summary["passed"] is False


def test_holdout_plan_cli_records_control_run_roots(tmp_path: Path) -> None:
    runner = CliRunner()
    plan_result = runner.invoke(
        app,
        [
            "holdout-plan",
            "--campaign-root",
            str(tmp_path),
            "--mutation",
            "clarify_elderly_understanding_sales_only",
            "--control-run-root",
            "s2_anchor",
        ],
    )
    assert plan_result.exit_code == 0, plan_result.output
    plan = json.loads((tmp_path / "holdout_inputs.json").read_text(encoding="utf-8"))
    assert plan["control_run_roots"] == ["s2_anchor"]
    assert plan["injections"][0]["arm"] == ARM_POSITIVE_CONTROL


# ---------------------------------------------------------------------------
# 2026-07-06 approved activation-aware holdout protocol (MASTER_DESIGN.md
# section 17.9): activation = exposure AND opportunity; detection is
# evaluated only over activated trials; zero activated trials -> fails
# outright; multi-seed (seeds_per_injection) plan support.
# ---------------------------------------------------------------------------


def test_run_exposure_detects_read_document_attempt(tmp_path: Path) -> None:
    """No config.json (or config.json without a recorded circulation mode)
    falls back to the original read-based exposure definition (backward
    compat with pre-full-text-circulation bundles)."""
    from company_twin.holdout import _run_exposure

    root = tmp_path / "run0"
    root.mkdir()
    _write_attempts_exposing_doc(root, doc_id="DFH-SAL-901")

    exposure = _run_exposure(root, "DFH-SAL-901")

    assert exposure["exposed"] is True
    assert exposure["basis"] == "content_read"
    assert exposure["content_read"] is True
    assert exposure["content_read_detail"]["read_document_hits"]
    assert exposure["content_read_detail"]["read_document_hits"][0]["seat_id"] == "seat_sales_1"


def test_run_exposure_detects_basis_citation(tmp_path: Path) -> None:
    from company_twin.holdout import _run_exposure

    root = tmp_path / "run0"
    root.mkdir()
    (root / "attempts.jsonl").write_text("", encoding="utf-8")
    basis_row = {
        "basis_id": "BASIS-000001",
        "seat_id": "seat_sales_1",
        "tick": 2,
        "retrieved": [{"doc_id": "DFH-SAL-901", "version": "1.1", "citation_handle": "H-1"}],
    }
    (root / "basis_records.jsonl").write_text(json.dumps(basis_row) + "\n", encoding="utf-8")

    exposure = _run_exposure(root, "DFH-SAL-901")

    assert exposure["exposed"] is True
    assert exposure["content_read_detail"]["basis_citation_hits"]
    assert exposure["content_read_detail"]["read_document_hits"] == []


def test_run_exposure_false_when_doc_never_read(tmp_path: Path) -> None:
    from company_twin.holdout import _run_exposure

    root = tmp_path / "run0"
    root.mkdir()
    (root / "attempts.jsonl").write_text("", encoding="utf-8")

    exposure = _run_exposure(root, "DFH-SAL-901")

    assert exposure["exposed"] is False
    assert "DFH-SAL-901" in exposure["detail"]


def test_run_opportunity_from_rule_hit_rate_metrics(tmp_path: Path) -> None:
    from company_twin.holdout import _run_opportunity

    root = tmp_path / "run0"
    root.mkdir()
    (root / "triage").mkdir()
    # Reproduces the earlier role_table_fix run: opportunity_count=0 for
    # every expected finding type -- no genuine opportunity, by construction.
    (root / "triage" / "metrics.json").write_text(
        json.dumps(
            {
                "rule_hit_rate": {
                    "MON-A": {"finding_type": "sod_pattern", "opportunity_count": 0, "hit_count": 0},
                    "MON-B": {"finding_type": "approval_concentration", "opportunity_count": 0, "hit_count": 0},
                }
            }
        ),
        encoding="utf-8",
    )

    opportunity = _run_opportunity(root, ["sod_pattern", "approval_concentration", "alternative_approval_chain"])

    assert opportunity["has_opportunity"] is False
    assert opportunity["opportunity_count_by_type"] == {"sod_pattern": 0, "approval_concentration": 0, "alternative_approval_chain": 0}

    # Now with a genuine opportunity for one expected type.
    (root / "triage" / "metrics.json").write_text(
        json.dumps({"rule_hit_rate": {"MON-A": {"finding_type": "sod_pattern", "opportunity_count": 4, "hit_count": 0}}}),
        encoding="utf-8",
    )
    opportunity2 = _run_opportunity(root, ["sod_pattern", "approval_concentration"])
    assert opportunity2["has_opportunity"] is True
    assert opportunity2["opportunity_count_by_type"]["sod_pattern"] == 4


def test_injection_with_zero_activated_trials_fails_with_named_reason(tmp_path: Path) -> None:
    """A run bundle with real findings but NO exposure and NO opportunity
    (unactivated) must fail the injection outright -- the stimulus never had
    a fair chance to be observed, so an "undetected" reading here is not
    evidence of a detection miss (MASTER_DESIGN.md section 17.6's
    role_table_fix_quality_owner finding / section 17.7's stimulus-delivery
    gap, both cases where the run was in fact unactivated)."""
    # clarify_elderly_understanding_sales_only (not _all -- reclassified
    # benign_control per MASTER_DESIGN.md section 17.11) is used here as the
    # positive_control example.
    plan = build_holdout_injection_plan(
        Path.cwd(), mutation_ids=["clarify_elderly_understanding_sales_only"], run_roots=["s2_holdout_0"]
    )
    write_holdout_inputs(tmp_path, plan)
    injection = plan["injections"][0]
    # Even though grounding_gap (an expected finding_type) technically fires,
    # activated=False means no exposure evidence and no opportunity_count is
    # recorded -- so this run cannot demonstrate detection.
    _verified_s2_bundle(tmp_path / "s2_holdout_0", injection=injection, finding_types={"grounding_gap": 1}, activated=False)

    report = write_holdout_report(tmp_path, run_lookup={injection["injection_id"]: tmp_path / "s2_holdout_0"})

    row = report["measurement"]["per_injection"][0]
    assert row["activation_summary"]["any_activated"] is False
    assert row["activation_summary"]["activated_trials"] == 0
    assert row["strict_detected"] is False
    assert row["detected"] is False
    assert "ZERO activated trials" in row["strict_reason"]
    assert report["passed"] is False
    assert report["measurement"]["unactivated_positive_control_count"] == 1
    assert report["activation"]["unactivated_injection_ids"] == [injection["injection_id"]]


def test_activated_but_undetected_injection_fails(tmp_path: Path) -> None:
    """An activated trial (exposure + opportunity both present) that produces
    no matching expected finding_type is a genuine detection miss, distinct
    from the zero-activation case -- it must still fail, with a reason that
    does NOT claim zero activation."""
    plan = build_holdout_injection_plan(
        Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"], run_roots=["s2_holdout_0"]
    )
    write_holdout_inputs(tmp_path, plan)
    injection = plan["injections"][0]
    # Activated (exposure + opportunity for the expected types) but the run's
    # only finding is unrelated (deadline_overrun), so it's a real miss.
    _verified_s2_bundle(tmp_path / "s2_holdout_0", injection=injection, finding_types={"deadline_overrun": 2}, activated=True)

    report = write_holdout_report(tmp_path, run_lookup={injection["injection_id"]: tmp_path / "s2_holdout_0"})

    row = report["measurement"]["per_injection"][0]
    assert row["activation_summary"]["any_activated"] is True
    assert row["strict_detected"] is False
    assert row["detected"] is False
    assert "ZERO activated trials" not in row["strict_reason"]
    assert report["passed"] is False


def test_activated_hit_among_k_seeds_counts_as_detected(tmp_path: Path) -> None:
    """seeds_per_injection > 1: an injection is detected when AT LEAST ONE of
    its K seeded trials is both activated and a strict hit, even if the other
    seeds are unactivated or miss."""
    # clarify_elderly_understanding_sales_only (not _all -- reclassified
    # benign_control per MASTER_DESIGN.md section 17.11) is used here as the
    # positive_control example.
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["clarify_elderly_understanding_sales_only"],
        auto_run_roots=True,
        seeds_per_injection=3,
    )
    injection = plan["injections"][0]
    assert injection["planned_run_roots"] == [
        "holdout_clarify_elderly_understanding_sales_only_seed1",
        "holdout_clarify_elderly_understanding_sales_only_seed2",
        "holdout_clarify_elderly_understanding_sales_only_seed3",
    ]
    write_holdout_inputs(tmp_path, plan)

    # Seed 1: unactivated. Seed 2: activated but a miss. Seed 3: activated hit.
    _verified_s2_bundle(tmp_path / "holdout_clarify_elderly_understanding_sales_only_seed1", injection=injection, finding_types={"grounding_gap": 1}, activated=False)
    _verified_s2_bundle(tmp_path / "holdout_clarify_elderly_understanding_sales_only_seed2", injection=injection, finding_types={"deadline_overrun": 1}, activated=True)
    _verified_s2_bundle(tmp_path / "holdout_clarify_elderly_understanding_sales_only_seed3", injection=injection, finding_types={"grounding_gap": 1}, activated=True)

    measurement = compute_holdout_detection_rate(tmp_path, plan)

    row = measurement["per_injection"][0]
    assert row["activation"]["activated_trials"] == 2
    assert row["activation"]["total_trials"] == 3
    assert row["strict_detected"] is True
    assert row["detected"] is True
    assert measurement["detection_rate"] == 1.0


def test_seeds_per_injection_k_changes_plan_hash_and_requires_auto_run_roots() -> None:
    plan_k1 = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"], auto_run_roots=True)
    plan_k3 = build_holdout_injection_plan(
        Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"], auto_run_roots=True, seeds_per_injection=3
    )

    assert plan_k1["plan_hash"] != plan_k3["plan_hash"]
    assert plan_k1["injections"][0]["planned_run_roots"] == ["holdout_clarify_elderly_understanding_all"]
    assert plan_k3["injections"][0]["planned_run_roots"] == [
        "holdout_clarify_elderly_understanding_all_seed1",
        "holdout_clarify_elderly_understanding_all_seed2",
        "holdout_clarify_elderly_understanding_all_seed3",
    ]

    with pytest.raises(ValueError, match="requires auto_run_roots"):
        build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"], seeds_per_injection=2)


def test_seeds_per_injection_default_k1_backward_compat_naming() -> None:
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"], auto_run_roots=True)

    assert plan["injections"][0]["planned_run_roots"] == ["holdout_clarify_elderly_understanding_all"]
    assert plan["injections"][0]["seeds_per_injection"] == 1

    # Same plan_hash-affecting shape as before this field existed: an
    # explicit seeds_per_injection=1 plan matches one built without the kwarg.
    plan_implicit = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"], auto_run_roots=True)
    assert plan["plan_hash"] == plan_implicit["plan_hash"]


def test_seeds_per_injection_per_mutation_dict_produces_correct_root_sets() -> None:
    """2026-07-06 approved holdout arm re-classification (MASTER_DESIGN.md
    section 17.11): seeds_per_injection accepts a per-mutation {mutation_id: K}
    dict -- needed for the final campaign (contradict K=5, everything else
    K=1). A mutation_id absent from the dict falls back to its "_default" key."""
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["contradict_chat_approval_recorded", "dangling_fill_search_key_stub", "clarify_elderly_understanding_sales_only"],
        auto_run_roots=True,
        seeds_per_injection={"contradict_chat_approval_recorded": 5, "_default": 1},
    )
    by_mutation = {injection["mutation_id"]: injection for injection in plan["injections"]}

    contradict = by_mutation["contradict_chat_approval_recorded"]
    assert contradict["seeds_per_injection"] == 5
    assert contradict["planned_run_roots"] == [f"holdout_contradict_chat_approval_recorded_seed{n}" for n in range(1, 6)]

    dangling = by_mutation["dangling_fill_search_key_stub"]
    assert dangling["seeds_per_injection"] == 1
    assert dangling["planned_run_roots"] == ["holdout_dangling_fill_search_key_stub"]

    clarify_sales_only = by_mutation["clarify_elderly_understanding_sales_only"]
    assert clarify_sales_only["seeds_per_injection"] == 1
    assert clarify_sales_only["planned_run_roots"] == ["holdout_clarify_elderly_understanding_sales_only"]


def test_seeds_per_injection_per_mutation_dict_without_default_key_falls_back_to_one() -> None:
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["contradict_chat_approval_recorded", "dangling_fill_search_key_stub"],
        auto_run_roots=True,
        seeds_per_injection={"contradict_chat_approval_recorded": 5},
    )
    by_mutation = {injection["mutation_id"]: injection for injection in plan["injections"]}
    assert by_mutation["contradict_chat_approval_recorded"]["seeds_per_injection"] == 5
    assert by_mutation["dangling_fill_search_key_stub"]["seeds_per_injection"] == 1


def test_seeds_per_injection_per_mutation_dict_sealed_in_plan_hash() -> None:
    """A per-mutation K dict must be sealed into plan_hash exactly like the
    global-int form -- a plan built with a different per-mutation K for the
    same mutation set hashes differently."""
    plan_uniform = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["contradict_chat_approval_recorded", "dangling_fill_search_key_stub"],
        auto_run_roots=True,
        seeds_per_injection={"contradict_chat_approval_recorded": 1, "dangling_fill_search_key_stub": 1},
    )
    plan_mixed = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["contradict_chat_approval_recorded", "dangling_fill_search_key_stub"],
        auto_run_roots=True,
        seeds_per_injection={"contradict_chat_approval_recorded": 5, "dangling_fill_search_key_stub": 1},
    )
    assert plan_uniform["plan_hash"] != plan_mixed["plan_hash"]
    # And it matches the equivalent global-int plan for the uniform K=1 case.
    plan_global = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["contradict_chat_approval_recorded", "dangling_fill_search_key_stub"],
        auto_run_roots=True,
        seeds_per_injection=1,
    )
    assert plan_uniform["plan_hash"] == plan_global["plan_hash"]


def test_seeds_per_injection_per_mutation_dict_requires_auto_run_roots_when_any_k_over_one() -> None:
    with pytest.raises(ValueError, match="requires auto_run_roots"):
        build_holdout_injection_plan(
            Path.cwd(),
            mutation_ids=["contradict_chat_approval_recorded"],
            seeds_per_injection={"contradict_chat_approval_recorded": 5},
        )


def test_holdout_plan_cli_injection_seeds_option_per_mutation_override(tmp_path: Path) -> None:
    """CLI: --injection-seeds mutation_id=K (repeatable) overrides
    --seeds-per-injection per mutation, falling back to the global default
    for any mutation not listed (MASTER_DESIGN.md section 17.11)."""
    runner = CliRunner()
    plan_result = runner.invoke(
        app,
        [
            "holdout-plan",
            "--campaign-root",
            str(tmp_path),
            "--mutation",
            "contradict_chat_approval_recorded",
            "--mutation",
            "dangling_fill_search_key_stub",
            "--auto-run-roots",
            "--seeds-per-injection",
            "1",
            "--injection-seeds",
            "contradict_chat_approval_recorded=5",
        ],
    )
    assert plan_result.exit_code == 0, plan_result.output
    plan = json.loads((tmp_path / "holdout_inputs.json").read_text(encoding="utf-8"))
    by_mutation = {injection["mutation_id"]: injection for injection in plan["injections"]}
    assert by_mutation["contradict_chat_approval_recorded"]["seeds_per_injection"] == 5
    assert by_mutation["contradict_chat_approval_recorded"]["planned_run_roots"] == [
        f"holdout_contradict_chat_approval_recorded_seed{n}" for n in range(1, 6)
    ]
    assert by_mutation["dangling_fill_search_key_stub"]["seeds_per_injection"] == 1
    assert by_mutation["dangling_fill_search_key_stub"]["planned_run_roots"] == ["holdout_dangling_fill_search_key_stub"]


def test_holdout_plan_cli_injection_seeds_option_rejects_malformed_entry(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "holdout-plan",
            "--campaign-root",
            str(tmp_path),
            "--mutation",
            "contradict_chat_approval_recorded",
            "--auto-run-roots",
            "--injection-seeds",
            "not-a-valid-entry",
        ],
    )
    assert result.exit_code != 0


def test_holdout_plan_cli_seeds_per_injection_option(tmp_path: Path) -> None:
    runner = CliRunner()
    plan_result = runner.invoke(
        app,
        [
            "holdout-plan",
            "--campaign-root",
            str(tmp_path),
            "--mutation",
            "clarify_elderly_understanding_all",
            "--auto-run-roots",
            "--seeds-per-injection",
            "2",
        ],
    )
    assert plan_result.exit_code == 0, plan_result.output
    plan = json.loads((tmp_path / "holdout_inputs.json").read_text(encoding="utf-8"))
    assert plan["injections"][0]["planned_run_roots"] == [
        "holdout_clarify_elderly_understanding_all_seed1",
        "holdout_clarify_elderly_understanding_all_seed2",
    ]


def test_activation_recorded_for_benign_control_for_visibility_only(tmp_path: Path) -> None:
    """benign_control activation is recorded but never gates its own pass
    criterion -- an unactivated benign_control bundle that is otherwise clean
    (no false alarm) still passes."""
    # clarify_elderly_understanding_sales_only (not _all -- reclassified
    # benign_control per MASTER_DESIGN.md section 17.11) is used here as the
    # positive_control example paired with role_table_fix's benign_control.
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["clarify_elderly_understanding_sales_only", "role_table_fix_quality_owner"],
        control_run_roots=[],
    )
    write_holdout_inputs(tmp_path, plan)
    positive = next(i for i in plan["injections"] if i["arm"] == ARM_POSITIVE_CONTROL)
    benign = next(i for i in plan["injections"] if i["arm"] == ARM_BENIGN_CONTROL)
    _verified_s2_bundle(tmp_path / "s2_positive", injection=positive, finding_types={positive["expected_finding_types"][0]: 1}, activated=True)
    _verified_s2_bundle(tmp_path / "s2_benign", injection=benign, finding_types={}, activated=False)
    run_lookup = {positive["injection_id"]: tmp_path / "s2_positive", benign["injection_id"]: tmp_path / "s2_benign"}

    benign_result = score_benign_controls(tmp_path, plan, run_lookup=run_lookup)

    assert benign_result is not None
    assert benign_result["per_injection"][0]["activation"]["any_activated"] is False
    assert benign_result["per_injection"][0]["passed"] is True  # activation never gates benign_control

    report = write_holdout_report(tmp_path, run_lookup=run_lookup)
    assert report["passed"] is True
    assert report["activation"] is not None
    benign_activation_row = next(row for row in report["activation"]["per_injection"] if row["injection_id"] == benign["injection_id"])
    assert benign_activation_row["any_activated"] is False
    # benign_control's lack of activation does not appear in the positive-control unactivated list.
    assert benign["injection_id"] not in report["activation"]["unactivated_injection_ids"]


def test_backward_compat_scoring_applies_activation_to_pre_existing_plan_schema(tmp_path: Path) -> None:
    """Activation recording applies at scoring time regardless of the sealed
    plan's schema version: a plan built the way build_holdout_injection_plan
    produced it BEFORE seeds_per_injection/activation existed (single-seed
    holdout_<mutation_id> root, no seeds_per_injection key) is still scored
    with activation, and the zero-activation-fails rule applies to it too."""
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"], auto_run_roots=True)
    injection = plan["injections"][0]
    del injection["seeds_per_injection"]  # simulate a pre-existing sealed plan without this key
    write_holdout_inputs(tmp_path, plan)

    # Unactivated bundle at the legacy single-seed root name.
    _verified_s2_bundle(tmp_path / "holdout_clarify_elderly_understanding_all", injection=injection, finding_types={"grounding_gap": 1}, activated=False)

    report = write_holdout_report(tmp_path)

    assert report["passed"] is False
    row = report["measurement"]["per_injection"][0]
    assert row["activation_summary"]["any_activated"] is False
    assert "ZERO activated trials" in row["strict_reason"]


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


# ---------------------------------------------------------------------------
# Round-9 pooled blind SME review follow-up (data/design/MASTER_DESIGN.md
# §17.18): cross-run normalized-content dedup. A pooled panel built from
# multiple run_roots (e.g. two same-world control bundles, per §17.17's
# approved pooled protocol) must never surface the same underlying content
# twice as two "different" reviewer-facing items just because it happened to
# be sampled from two different run bundles.
# ---------------------------------------------------------------------------


def test_build_blind_review_packet_dedupes_identical_notice_across_two_run_roots(tmp_path: Path) -> None:
    # The exact round-9 shape: two control runs of the same world/seed
    # pairing produce a verbatim-identical kernel campaign-deadline notice
    # (R-026/R-065 in the round-9 pooled panel, offset by 39 positions).
    shared_notice = "キャンペーン投信の申込期日は7月10日までです。忘れずにご確認ください。"
    run_a = tmp_path / "control_run_a"
    run_a.mkdir(parents=True, exist_ok=True)
    _write_jsonl(run_a / "chat_channel.jsonl", [{"body": shared_notice}, {"body": "run_aだけにある別件の連絡です。"}])
    (run_a / "world_ledger.jsonl").write_text("", encoding="utf-8")
    (run_a / "attempts.jsonl").write_text("", encoding="utf-8")

    run_b = tmp_path / "control_run_b"
    run_b.mkdir(parents=True, exist_ok=True)
    _write_jsonl(run_b / "chat_channel.jsonl", [{"body": shared_notice}, {"body": "run_bだけにある別件の連絡です。"}])
    (run_b / "world_ledger.jsonl").write_text("", encoding="utf-8")
    (run_b / "attempts.jsonl").write_text("", encoding="utf-8")

    packet, id_map = build_blind_review_packet([run_a, run_b])

    # Only one packet item for the shared notice -- not two.
    matching_items = [item for item in packet["items"] if shared_notice in item["text"]]
    assert len(matching_items) == 1
    kept_item_id = matching_items[0]["item_id"]

    # Both run-unique excerpts still make it into the packet.
    all_text = " ".join(item["text"] for item in packet["items"])
    assert "run_aだけにある別件の連絡です。" in all_text
    assert "run_bだけにある別件の連絡です。" in all_text

    # The skip is recorded in the id map, never in the reviewer-facing packet.
    assert "deduped_cross_run" not in json.dumps(packet, ensure_ascii=False)
    dedup_drops = [item for item in id_map["dropped_items"] if item["reason"] == "deduped_cross_run"]
    assert len(dedup_drops) == 1
    assert dedup_drops[0]["run_root"] == "control_run_b"
    assert dedup_drops[0]["duplicate_of_item_id"] == kept_item_id
    assert id_map["dropped_count"] == 1


def test_cross_run_dedup_does_not_fail_the_gate(tmp_path: Path) -> None:
    # deduped_cross_run is benign, expected pooled-panel bookkeeping -- unlike
    # a leaked_vocabulary_redacted drop, it must never fail
    # write_sme_blind_review_report on its own.
    shared_notice = "キャンペーン投信の申込期日は7月10日までです。忘れずにご確認ください。"
    run_a = _fixture_run_bundle(tmp_path / "run_a")
    _write_jsonl(
        run_a / "chat_channel.jsonl",
        [
            {"body": "本日の申込、意向把握のメモが未記入なので確認してもらえますか。"},
            {"body": "承知しました、確認して午後に折り返します。"},
            {"body": "顧客への再説明は完了しました、記録も残しています。"},
            {"body": shared_notice},
        ],
    )
    run_b = tmp_path / "run_b"
    run_b.mkdir(parents=True, exist_ok=True)
    _write_jsonl(run_b / "chat_channel.jsonl", [{"body": shared_notice}])
    (run_b / "world_ledger.jsonl").write_text("", encoding="utf-8")
    (run_b / "attempts.jsonl").write_text("", encoding="utf-8")

    packet, id_map = build_blind_review_packet([run_a, run_b], samples_per_run=10)
    assert id_map["dropped_count"] >= 1
    assert any(item["reason"] == "deduped_cross_run" for item in id_map["dropped_items"])
    assert not any(item["reason"] == "leaked_vocabulary_redacted" for item in id_map["dropped_items"])
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    write_sme_blind_review_inputs(tmp_path, packet, id_map)

    payload = write_sme_blind_review_report(tmp_path)

    assert payload["passed"] is True
    assert payload["checks"][0]["leak_dropped_count"] == 0
    assert payload["checks"][0]["deduped_cross_run_count"] >= 1


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
    # Approval #9 (MASTER_DESIGN.md §17.20): the detail string now names the
    # routine-panel basis the verdict is actually computed over.
    assert "routine_panel" in payload["checks"][0]["detail"]
    assert "artificial_marker_category_counts" in payload["checks"][0]["detail"]


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
    # Approval #8: the gate is now a rate tolerance; one flag on this small
    # fixture panel is far above 5% and must still fail, with the rate shown.
    assert "mechanical_generation_rate=" in payload["checks"][0]["detail"]
    assert "(1/" in payload["checks"][0]["detail"]


def _synthetic_reviewed_packet(item_count: int, mechanical_flags: int) -> dict:
    items = []
    for i in range(item_count):
        response = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
        if i < mechanical_flags:
            response = {
                "plausible_workplace_scene": 5,
                "internally_consistent": 5,
                "no_artificial_markers": "yes",
                "artificial_marker_category": "mechanical_generation",
            }
        items.append({"item_id": f"R-{i + 1:03d}", "kind": "business_event", "text": f"記録{i}", "questions": [], "response": response})
    return {
        "schema_version": "company_twin.sme_blind_review_inputs.v1",
        "kind": "blind_review_packet",
        "plausibility_target": 0.8,
        "min_reviewed_samples": 5,
        "item_count": item_count,
        "items": items,
        "packet_hash": "synthetic",
        "reviewer_type": "ai_proxy",
    }


def test_sme_mechanical_rate_tolerance_passes_at_or_below_five_percent(tmp_path: Path) -> None:
    # Approval #8 (2026-07-06): 2 flags on an 80-item pooled panel (2.5%) is
    # within the 5% tolerance and must pass; 5 flags (6.25%) must fail.
    import json as _json

    from company_twin.sme_blind_review import write_sme_blind_review_report

    (tmp_path / "sme_blind_review_id_map.json").write_text(
        _json.dumps({"schema_version": "company_twin.sme_blind_review_id_map.v1", "dropped_count": 0, "dropped_items": [], "entries": []}),
        encoding="utf-8",
    )
    (tmp_path / "sme_blind_review_inputs.json").write_text(
        _json.dumps(_synthetic_reviewed_packet(80, 2), ensure_ascii=False), encoding="utf-8"
    )
    payload = write_sme_blind_review_report(tmp_path)
    assert payload["passed"] is True
    assert payload["checks"][0]["mechanical_generation_rate"] == 2 / 80

    (tmp_path / "sme_blind_review_inputs.json").write_text(
        _json.dumps(_synthetic_reviewed_packet(80, 5), ensure_ascii=False), encoding="utf-8"
    )
    payload = write_sme_blind_review_report(tmp_path)
    assert payload["passed"] is False
    assert "mechanical_generation_rate=" in payload["checks"][0]["detail"]


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
    assert "leak_dropped_count=1" in payload["checks"][0]["detail"]
    assert "artifact detection" in payload["checks"][0]["detail"]
    assert payload["checks"][0]["dropped_count"] == 1
    assert payload["checks"][0]["leak_dropped_count"] == 1
    assert payload["checks"][0]["deduped_cross_run_count"] == 0


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

    # clarify_elderly_understanding_sales_only (not _all -- reclassified
    # benign_control per MASTER_DESIGN.md section 17.11) is used here as the
    # positive_control example.
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_sales_only"], run_roots=["s2_holdout_0"])
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


def test_pooled_packet_suppresses_near_duplicate_pairs(tmp_path: Path) -> None:
    # Round-10 pooled panel: two runs of the SAME frozen deck share verbatim
    # deterministic components (situational cues, fixed notices), so paired
    # records read as copy-paste artifacts to a blind reviewer even though
    # neither record is individually defective. The packet build must keep
    # only the first member of any pair sharing a long verbatim run, recorded
    # as deduped_cross_run_near (benign bookkeeping, never gating).
    import json as _json

    shared_cue = "本日はキャンペーンの最終日で、時刻は18時50分です。担当の方が席を外しているため急いでおります。"
    run1 = _fixture_run_bundle(tmp_path / "run_a")
    run2 = _fixture_run_bundle(tmp_path / "run_b")
    for root, lead in ((run1, "投資信託の件でご相談です。"), (run2, "保険の件でご連絡しました。")):
        _write_jsonl(
            root / "world_ledger.jsonl",
            [
                {
                    "event_type": "customer_utterance",
                    "tick": 10,
                    "payload": {"utterance": lead + shared_cue, "customer_id": "CUS-1", "product": "x"},
                }
            ],
        )

    packet, id_map = build_blind_review_packet([run1, run2], samples_per_run=10)

    texts = [item["text"] for item in packet["items"]]
    assert sum(1 for t in texts if shared_cue in t) == 1
    near = [d for d in id_map["dropped_items"] if d.get("reason") == "deduped_cross_run_near"]
    assert len(near) == 1 and near[0]["duplicate_of_item_id"]
