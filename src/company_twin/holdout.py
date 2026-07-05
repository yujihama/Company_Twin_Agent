"""WP-14 holdout-verification machinery.

Stage 9 gate 8 (data/design/MASTER_DESIGN.md section 12, "ホールドアウト検証")
requires a world with known-answer injected anomalies where the oracle and
analysis pipeline are checked against ground truth ("観測所側の検証" -
verification on the observatory side, not the world side). This module is the
offline harness for that gate:

- build_holdout_injection_plan(): selects catalogued WP-06 runtime mutations
  (data/compiled_data/mutation_operators_v1.json) as the known-answer
  injections, stamping each planned injection with a content hash so a later
  live run can be checked against exactly what was planned. Each injection is
  also stamped with a pre-registered ``expected_finding_types`` spec (see
  ``_expected_finding_types``) mapping the mutation's operator family to the
  L0/L1 signals that would genuinely indicate its detection. This spec is
  frozen at planning time, before any scoring happens, so post-hoc "what
  counts as a hit" choices are impossible.
- compute_holdout_detection_rate(): consumes L0 triage findings
  (triage/buckets.json / triage/metrics.json under each run bundle) and L1
  monitoring-rule signals (metrics.json's detection_miss_rate/rule_hit_rate)
  to compute two detection rates per injected mutation and overall:
  ``lenient_detection_rate`` (any L0∪L1 signal fired on a matching run --
  the original, gameable definition, kept for visibility) and
  ``strict_detection_rate`` (only L0 finding_types / L1-detected finding_types
  that appear in the injection's pre-registered ``expected_finding_types``).
  The strict rate is the official acceptance basis
  (``detection_rate_basis: "strict"``); lenient is reported alongside it but
  never gates.
- write_holdout_inputs()/write_holdout_report(): write the readiness-facing
  evidence files in the schema_version envelope used across Stage 9 reports.

2026-07-05 approved recalibration (MASTER_DESIGN.md section 17.5): every
injection also carries an `arm` ("positive_control" | "benign_control",
sealed into plan_hash) and the plan seals `control_run_roots` (designated
no-mutation control run roots). `positive_control` strict detection is
delta-aware -- an expected finding_type must exceed the no-mutation control
baseline, not merely be present (see _score_injection/_compute_control_baseline).
`benign_control` injections (role_table_fix by default -- a corrective
operator, not one expected to introduce a new anomaly) are excluded from the
positive-control denominator and instead scored by score_benign_controls on
whether nothing went newly wrong, reported in their own section.

2026-07-06 approved activation-aware holdout protocol (MASTER_DESIGN.md
section 17.9): a positive-control trial can only demonstrate detection if the
injected stimulus was actually ACTIVATED in that run -- EXPOSURE (the
injected/patched document was actually read by a seat) AND OPPORTUNITY (at
least one expected finding type had a genuine opportunity_count > 0 in the
run's triage metrics). This closes the false-negative-looking hole found via
a holdout-activation diagnosis (MASTER_DESIGN.md section 17.7): a run can
score as an undetected miss when in truth the injected stimulus never reached
the world surface / never had an opportunity to be observed, which is not a
detection failure at all. Every run scored per injection now records
exposure/opportunity/activated; strict detection is evaluated only over
ACTIVATED trials, and an injection with ZERO activated trials among its
planned runs FAILS OUTRIGHT (inactivation is recorded honestly, never used to
excuse an injection from the denominator or quietly drop it). See
`_run_activation`, `_score_injection`, and `build_holdout_injection_plan`'s
`seeds_per_injection` (multi-seed support, K>1 gives each injection K planned
run roots named `holdout_<mutation_id>_seed<N>`; K=1 keeps the pre-existing
`holdout_<mutation_id>` naming for backward compatibility).

This module never calls an LLM or external API. Detection-rate measurement
against live campaign data happens later, by pointing compute_holdout_detection_rate
at real run bundles; this module only supplies the plan/measurement machinery
and the honest-fail path (rate < target -> FAIL, no evidence -> FAIL).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .mutations import load_mutation_catalog
from .readiness import REPORT_SCHEMA_VERSION
from .recorder import read_jsonl
from .world_config import _json_hash

HOLDOUT_INPUTS_SCHEMA_VERSION = "company_twin.holdout_inputs.v1"
HOLDOUT_DETECTION_TARGET = 0.80


ARM_POSITIVE_CONTROL = "positive_control"
ARM_BENIGN_CONTROL = "benign_control"
INJECTION_ARMS: tuple[str, ...] = (ARM_POSITIVE_CONTROL, ARM_BENIGN_CONTROL)

# Default arm assignment by operator family (MASTER_DESIGN.md section 17,
# 2026-07-05 approved recalibration). role_table_fix is a corrective/
# de-ambiguating operator -- the mutation catalog row itself frames it as
# "帰属矛盾の解消は誤宛先報告を減らすか" (does resolving attribution ambiguity
# REDUCE misdirected reports), and world_config_v2.yaml/FUZZING_HARNESS_DESIGN
# mutation_space frame it the same way -- so it is not expected to produce an
# approval/SoD anomaly finding the way clarify/contradict/dangling_fill are;
# it is scored as a benign_control (a run where nothing should go newly
# wrong), not a positive_control (a run where a specific anomaly should be
# newly detected). All other catalogued operators default to positive_control.
_ARM_BY_OPERATOR: dict[str, str] = {
    "clarify": ARM_POSITIVE_CONTROL,
    "contradict": ARM_POSITIVE_CONTROL,
    "dangling_fill": ARM_POSITIVE_CONTROL,
    "role_table_fix": ARM_BENIGN_CONTROL,
}


def _default_arm_for_operator(operator: str) -> str:
    return _ARM_BY_OPERATOR.get(operator, ARM_POSITIVE_CONTROL)


def build_holdout_injection_plan(
    root: Path,
    *,
    mutation_ids: list[str] | None = None,
    run_roots: list[str] | None = None,
    auto_run_roots: bool = False,
    planned_ticks: int = 0,
    control_run_roots: list[str] | None = None,
    seeds_per_injection: int = 1,
) -> dict[str, Any]:
    """Build a WP-14 holdout injection plan from the WP-06 mutation catalog.

    Each planned injection reuses an existing catalogued mutation_id (no new
    world-visible text is authored here) and records a content hash so a
    later live run can be verified to have applied exactly the planned
    mutation. Planning is a pure function of the catalog; it does not touch
    the network and does not execute any run.

    `planned_ticks` (default 0 = no tick-coverage requirement, for backward
    compatibility with plans built before this field existed) is the expected
    world_ledger tick coverage a live S2 bundle attributed to this injection
    must reach; see holdout.verify_holdout_bundles/write_holdout_report.

    Each injection gains an `arm` field ("positive_control" | "benign_control",
    see `_ARM_BY_OPERATOR`/`_default_arm_for_operator`), sealed as part of
    `plan_hash` -- an arm cannot be silently swapped after the plan is built.
    `control_run_roots` (designated no-mutation control run-root names, e.g.
    the campaign's anchor/plain S2 bundles) are likewise RECORDED IN THE PLAN
    (sealed into `plan_hash`) so the delta-aware detection basis in
    `compute_holdout_detection_rate` and the benign-control baseline check in
    `score_benign_controls` both compare against the exact control set that
    was pre-registered at plan-build time, not one chosen post-hoc at scoring
    time.

    `seeds_per_injection` (default 1, backward compatible) is the number of
    independent seeded runs `auto_run_roots` plans for EACH injection --
    2026-07-06 approved activation-aware holdout protocol (MASTER_DESIGN.md
    section 17.9), multi-seed support. With K=1 (the default), an injection's
    `planned_run_roots` is `["holdout_<mutation_id>"]`, unchanged from before
    this field existed. With K>1, `auto_run_roots` must also be set (each
    injection needs one-to-one attribution per seed) and `planned_run_roots`
    becomes `["holdout_<mutation_id>_seed1", ..., "holdout_<mutation_id>_seedK"]`;
    this K is sealed into `plan_hash` (a plan built with a different K for the
    same mutation set hashes differently, even though `run_roots`/
    `auto_run_roots` alone don't change). An injection is DETECTED when at
    least one of its K seeded trials is both activated and a strict hit; see
    `compute_holdout_detection_rate`/`_score_injection`.
    """
    if seeds_per_injection < 1:
        raise ValueError("seeds_per_injection must be >= 1")
    if seeds_per_injection > 1 and not auto_run_roots:
        raise ValueError("seeds_per_injection > 1 requires auto_run_roots=True (each injection needs one-to-one per-seed attribution)")
    if auto_run_roots and run_roots:
        raise ValueError("pass either run_roots (shared, attributed to every injection) or auto_run_roots (per-injection root named after the injection_id), not both")
    catalog = load_mutation_catalog(root)
    if not catalog:
        raise ValueError("mutation catalog is empty; holdout plan requires at least one runtime mutation")
    selected_ids = list(mutation_ids) if mutation_ids else sorted(catalog)
    unknown = [mutation_id for mutation_id in selected_ids if mutation_id not in catalog]
    if unknown:
        raise ValueError(f"unknown mutation_id(s) in holdout plan: {unknown}")
    injections: list[dict[str, Any]] = []
    for mutation_id in selected_ids:
        spec = catalog[mutation_id]
        expected_finding_types = _expected_finding_types(spec)
        if not expected_finding_types:
            raise ValueError(
                f"mutation_id {mutation_id!r} (operator={spec.get('operator')!r}) has no pre-registered "
                "expected_finding_types mapping; add one to _EXPECTED_FINDING_TYPES_BY_OPERATOR before "
                "it can be scored"
            )
        operator = str(spec.get("operator") or "")
        if auto_run_roots:
            if seeds_per_injection > 1:
                planned_run_roots = [f"holdout_{mutation_id}_seed{seed}" for seed in range(1, seeds_per_injection + 1)]
            else:
                planned_run_roots = [f"holdout_{mutation_id}"]
        else:
            planned_run_roots = list(run_roots or [])
        injections.append(
            {
                "injection_id": f"holdout_{mutation_id}",
                "mutation_id": mutation_id,
                "operator": spec.get("operator"),
                "action": spec.get("action"),
                "target_doc_id": spec.get("doc_id") or spec.get("target_doc_id"),
                "expected_finding_types": expected_finding_types,
                "spec_hash": _json_hash(spec),
                # auto_run_roots gives each injection its own run root(s),
                # named after the injection_id (one per seed when
                # seeds_per_injection > 1), so a multi-mutation plan can be
                # sealed with one-to-one bundle attribution before any run
                # exists (shared run_roots would attribute every bundle to
                # every injection and correctly fail verification).
                "planned_run_roots": planned_run_roots,
                "planned_ticks": int(planned_ticks),
                "arm": _default_arm_for_operator(operator),
                "seeds_per_injection": int(seeds_per_injection),
            }
        )
    payload = {
        "schema_version": HOLDOUT_INPUTS_SCHEMA_VERSION,
        "kind": "injection_plan",
        "detection_target": HOLDOUT_DETECTION_TARGET,
        "detection_target_basis": "measured miss_rate=1.0 blind spots on prior monitoring-rule coverage; L0∪L1 >= 0.80 is the pre-registered acceptance target",
        "mutation_catalog_path": "data/compiled_data/mutation_operators_v1.json",
        "injection_count": len(injections),
        "injections": injections,
        "control_run_roots": list(control_run_roots or []),
        "plan_hash": _json_hash({"injections": injections, "control_run_roots": list(control_run_roots or [])}),
        "note": "Planning artifact only. Execution and scoring require live run bundles scored by compute_holdout_detection_rate.",
    }
    return payload


# Pre-registered mapping from a WP-06 mutation-operator family
# (data/compiled_data/mutation_operators_v1.json) to the L0 triage
# finding_type / L1 monitoring-rule "detects" values
# (src/company_twin/oracles.py DEFAULT_DETECTION_RULES) that would genuinely
# indicate the injected mutation was noticed, as opposed to an unrelated
# finding merely co-occurring on the same run. This is frozen at plan-build
# time (build_holdout_injection_plan calls it before any run bundle exists)
# so scoring cannot cherry-pick what counts as a hit after the fact.
#
# Rationale per operator, cross-checked against the actual injected text and
# the full finding_type universe (evidence_gap, grounding_gap, version_gap,
# deadline_overrun, sod_pattern, approval_concentration, version_mix,
# tacit_chat_to_action, rapid_resubmit_after_return,
# alternative_approval_chain):
#
# - clarify: injects a runtime_notice that conditions "high-age customer"
#   handling on an understanding-confirmation *record* that may be missing,
#   and additionally exists in two role-scoped variants (all roles vs.
#   sales-only) describing the same policy differently by audience. A
#   genuine detection of this mutation looks like an ungrounded claim of
#   having confirmed understanding (grounding_gap) or a
#   role/version-inconsistent picture of which policy text applies
#   (version_gap, version_mix) -- not, say, an unrelated deadline_overrun.
# - contradict: injects a runtime_notice that explicitly authorizes a
#   temporary chat-based approval ahead of/instead of the formal approval
#   record, to be "transcribed later". This directly contradicts the
#   separation-of-duties approval control, so genuine detection looks like a
#   chat message immediately preceding an approval-adjacent action
#   (tacit_chat_to_action) or an approval-control pattern break
#   (sod_pattern, alternative_approval_chain) -- the mutation is a SoD
#   bypass authored as a "workflow exception", not a grounding problem.
# - dangling_fill: injects a runtime_notice that tells sales to keep a
#   supplementary reference document whose search/reference key is a stub
#   pointing nowhere resolvable. A genuine detection looks like retrieval
#   producing a gap between what's cited and what's actually
#   available/current (grounding_gap, version_gap) -- the mutation is a
#   dangling reference, not an approval-control issue.
# - role_table_fix: patches an existing role/authority table document to
#   reassign which role owns exception approval vs. re-review. A genuine
#   detection looks like an approval/SoD-shaped finding: same-actor
#   submit+approve (sod_pattern), one seat concentrating approvals
#   (approval_concentration), or approvals routed through more than one
#   distinct approver chain after the reassignment
#   (alternative_approval_chain) -- not a grounding or version finding,
#   since no document content about evidence or dates was touched.
_EXPECTED_FINDING_TYPES_BY_OPERATOR: dict[str, list[str]] = {
    "clarify": ["grounding_gap", "version_gap", "version_mix"],
    "contradict": ["tacit_chat_to_action", "sod_pattern", "alternative_approval_chain"],
    "dangling_fill": ["grounding_gap", "version_gap"],
    "role_table_fix": ["sod_pattern", "approval_concentration", "alternative_approval_chain"],
}


def _expected_finding_types(spec: dict[str, Any]) -> list[str]:
    """Pre-registered expectation of which L0/L1 finding_type(s) an injected
    mutation should surface, based on the operator family (see
    ``_EXPECTED_FINDING_TYPES_BY_OPERATOR`` for the mapping and rationale).

    This spec is recorded on the injection plan at build time -- before any
    scoring happens -- and is what ``strict_detection_rate`` checks against.
    An operator with no registered mapping raises in
    ``build_holdout_injection_plan`` rather than silently scoring as
    undetectable, so every planned injection is pre-committed to a concrete,
    checkable expectation.
    """
    operator = str(spec.get("operator") or "")
    return list(_EXPECTED_FINDING_TYPES_BY_OPERATOR.get(operator, []))


def _injection_arm(injection: dict[str, Any]) -> str:
    """Resolve an injection's arm with backward compatibility: a plan built
    before the `arm` field existed (no `arm` key at all) treats every
    injection as `positive_control` -- the old behavior, and the strictest
    interpretation (nothing is quietly exempted from the positive
    denominator just because the plan predates arms)."""
    arm = injection.get("arm")
    if arm in INJECTION_ARMS:
        return arm
    return ARM_POSITIVE_CONTROL


def compute_holdout_detection_rate(
    campaign_root: Path,
    injection_plan: dict[str, Any],
    *,
    run_lookup: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Score a holdout injection plan against real run-bundle evidence.

    For every planned injection, this looks at run bundles whose recorded
    world.corpus.mutations (config.json) or meta.json mutation_ids include the
    injected mutation_id, and asks whether L0 triage
    (triage/buckets.json / triage/metrics.json finding_types) or L1
    monitoring rules (triage/metrics.json rule_hit_rate /
    detection_miss_rate monitoring_rules) registered a signal on that run.
    Two detection rates are computed per injection and overall:

    - ``lenient``: *any* L0 finding_type or L1 monitoring-rule hit on a
      matching run counts as detected, regardless of what fired. This is the
      original definition and is gameable -- an unrelated finding on a
      mutated run counts as a "hit" -- so it is retained only for
      visibility, never as the acceptance gate.
    - ``strict``: only L0 finding_types / L1-detected finding_types that
      appear in the injection's pre-registered ``expected_finding_types``
      (frozen at plan-build time by ``build_holdout_injection_plan`` /
      ``_expected_finding_types``) AND exceed the no-mutation control
      baseline (``compute_holdout_detection_rate``'s delta-aware gating --
      see ``_score_injection``/``_compute_control_baseline``) count as
      detected. This is the official acceptance basis
      (``detection_rate_basis: "strict"``).

    Only ``positive_control``-arm injections count toward the official
    ``detection_rate``/``detected_count``/``injection_count`` (and therefore
    the pass/fail gate): a ``benign_control``-arm injection (e.g.
    role_table_fix, a corrective/de-ambiguating operator that is not expected
    to produce a NEW anomaly finding) is scored separately by
    ``score_benign_controls`` and reported under its own section, never
    folded into the positive-control denominator. A plan built before the
    ``arm`` field existed has every injection default to ``positive_control``
    (old behavior, strictest -- see ``_injection_arm``).

    Every mutation's evidence is itemized so the readiness check can reject
    an unsupported claim.

    run_lookup lets fixtures/tests point injection ids at specific bundles
    without needing real campaign directory scanning; when absent, run roots
    declared in the plan's planned_run_roots are resolved under campaign_root.
    """
    injections = injection_plan.get("injections") or []
    if not injections:
        raise ValueError("injection plan has no injections to score")
    for injection in injections:
        if not injection.get("expected_finding_types"):
            raise ValueError(
                f"injection {injection.get('injection_id')!r} has no pre-registered expected_finding_types; "
                "holdout scoring requires every injection to carry a pre-registered expected-detection spec "
                "so strict_detection_rate cannot be chosen post-hoc"
            )
    control_run_roots = list(injection_plan.get("control_run_roots") or [])
    per_injection: list[dict[str, Any]] = []
    lenient_detected_count = 0
    strict_detected_count = 0
    positive_control_count = 0
    unactivated_positive_control_count = 0
    for injection in injections:
        mutation_id = str(injection.get("mutation_id") or "")
        expected_finding_types = list(injection.get("expected_finding_types") or [])
        target_doc_id = str(injection.get("target_doc_id") or "")
        arm = _injection_arm(injection)
        run_roots = _resolve_run_roots(campaign_root, injection, run_lookup=run_lookup)
        evidence = _score_injection(
            campaign_root,
            mutation_id,
            run_roots,
            expected_finding_types=expected_finding_types,
            control_run_roots=control_run_roots,
            target_doc_id=target_doc_id,
        )
        if arm == ARM_POSITIVE_CONTROL:
            positive_control_count += 1
            if evidence["lenient_detected"]:
                lenient_detected_count += 1
            if evidence["strict_detected"]:
                strict_detected_count += 1
            if not evidence["activation"]["any_activated"]:
                unactivated_positive_control_count += 1
        per_injection.append(
            {
                "injection_id": injection.get("injection_id"),
                "mutation_id": mutation_id,
                "spec_hash": injection.get("spec_hash"),
                "arm": arm,
                "expected_finding_types": expected_finding_types,
                "target_doc_id": target_doc_id,
                # Backward-compatible alias: "detected"/"reason" reflect the
                # official strict basis, matching the top-level passed field.
                "detected": evidence["strict_detected"],
                "reason": evidence["strict_reason"],
                # activation_summary duplicates evidence["activation"] under a
                # more discoverable top-level key for report consumers that
                # don't want to reach into the raw evidence blob.
                "activation_summary": {
                    "activated_trials": evidence["activation"]["activated_trials"],
                    "total_trials": evidence["activation"]["total_trials"],
                    "any_activated": evidence["activation"]["any_activated"],
                },
                **evidence,
            }
        )
    total = positive_control_count
    lenient_detection_rate = lenient_detected_count / total if total else 0.0
    strict_detection_rate = strict_detected_count / total if total else 0.0
    target = float(injection_plan.get("detection_target") or HOLDOUT_DETECTION_TARGET)
    return {
        "schema_version": HOLDOUT_INPUTS_SCHEMA_VERSION,
        "kind": "detection_rate_measurement",
        "campaign_root": str(campaign_root),
        "plan_hash": injection_plan.get("plan_hash"),
        "detection_target": target,
        "detection_rate_basis": "strict",
        # injection_count/detected_count/detection_rate are POSITIVE-CONTROL
        # ONLY -- benign_control-arm injections are excluded from this
        # denominator (see score_benign_controls for their own reporting).
        "injection_count": total,
        "total_injection_count": len(injections),
        "benign_control_count": len(injections) - positive_control_count,
        # Official fields (strict basis) -- these are what gates acceptance.
        "detected_count": strict_detected_count,
        "detection_rate": strict_detection_rate,
        "passed": total > 0 and strict_detection_rate >= target,
        # Both bases kept visible side by side.
        "strict_detected_count": strict_detected_count,
        "strict_detection_rate": strict_detection_rate,
        "lenient_detected_count": lenient_detected_count,
        "lenient_detection_rate": lenient_detection_rate,
        # 2026-07-06 approved activation-aware holdout protocol
        # (MASTER_DESIGN.md section 17.9): how many positive_control
        # injections had ZERO activated trials among their planned runs --
        # these fail outright (see _score_injection), recorded here for
        # visibility at the measurement level too.
        "unactivated_positive_control_count": unactivated_positive_control_count,
        "per_injection": per_injection,
    }


def _resolve_run_roots(campaign_root: Path, injection: dict[str, Any], *, run_lookup: dict[str, Path] | None) -> list[Path]:
    injection_id = str(injection.get("injection_id") or "")
    if run_lookup is not None and injection_id in run_lookup:
        return [run_lookup[injection_id]]
    declared = list(injection.get("planned_run_roots") or [])
    if declared:
        return [campaign_root / name for name in declared]
    mutation_id = str(injection.get("mutation_id") or "")
    return _matching_mutation_run_roots(campaign_root, mutation_id)


def _matching_mutation_run_roots(campaign_root: Path, mutation_id: str) -> list[Path]:
    if not campaign_root.exists():
        return []
    roots: list[Path] = []
    for path in sorted(p for p in campaign_root.iterdir() if p.is_dir()):
        config = _read_json(path / "config.json")
        meta = _read_json(path / "meta.json")
        mutation_ids = {
            str(item.get("mutation_id") or "")
            for item in (((config.get("world") or {}).get("corpus") or {}).get("mutations") or [])
        }
        mutation_ids |= {str(item) for item in (meta.get("mutation_ids") or [])}
        if mutation_id in mutation_ids:
            roots.append(path)
    return roots


def _run_finding_type_rates(run_root: Path) -> dict[str, dict[str, Any]]:
    """Per-finding_type opportunity-normalized rate for one run bundle.

    L1 finding_types (mapped via rule_hit_rate's recorded finding_type) carry
    a genuine opportunity denominator (rule_hit_rate's `opportunity_count`),
    so their rate is `hit_count / opportunity_count`. L0 finding_types
    (triage/metrics.json `finding_types` counts) have no such denominator
    recorded on the bundle; their rate is instead the raw count as a
    presence-weighted proxy (denominator=1, i.e. rate == count), which is
    only ever used for a same-run-shape control-vs-mutated *comparison*
    (never presented as a probability). Every finding_type present on the
    run at all is included, with rate 0.0 for hit_count==0 entries kept only
    if population is still explicitly present in the metrics.
    """
    metrics = _read_json(run_root / "triage" / "metrics.json")
    finding_types = metrics.get("finding_types") or {}
    rule_hit = metrics.get("rule_hit_rate") or {}
    rates: dict[str, dict[str, Any]] = {}
    for finding_type, count in finding_types.items():
        rates[str(finding_type)] = {"rate": float(count or 0), "opportunity_count": None, "hit_count": int(count or 0), "source": "l0"}
    for row in rule_hit.values():
        finding_type = str(row.get("finding_type") or "")
        if not finding_type:
            continue
        opportunity_count = int(row.get("opportunity_count") or 0)
        hit_count = int(row.get("hit_count") or 0)
        rate = (hit_count / opportunity_count) if opportunity_count else float(hit_count)
        existing = rates.get(finding_type)
        if existing is None or rate > existing["rate"]:
            rates[finding_type] = {"rate": rate, "opportunity_count": opportunity_count, "hit_count": hit_count, "source": "l1"}
    return rates


# ---------------------------------------------------------------------------
# 2026-07-06 approved activation-aware holdout protocol (MASTER_DESIGN.md
# section 17.9)
# ---------------------------------------------------------------------------
#
# A positive-control trial can only demonstrate detection if the injected
# stimulus was actually ACTIVATED: EXPOSURE (the injected/patched document was
# actually read by at least one seat) AND OPPORTUNITY (at least one of the
# injection's expected finding types had a genuine opportunity_count > 0 in
# the run's triage rule_hit_rate metrics -- the denominators already recorded
# on every scored bundle). Without both, an "undetected" run is not evidence
# of a detection miss: the stimulus never reached the world surface, or there
# was nothing for a detector to have a chance to fire on, by construction
# (see MASTER_DESIGN.md section 17.6's role_table_fix_quality_owner finding,
# and section 17.7's probe-stimulus-delivery gap, both cases where an
# "undetected" run in fact had zero opportunity/exposure).


def _run_exposure(run_root: Path, target_doc_id: str) -> dict[str, Any]:
    """Was `target_doc_id` (the injected/patched document) actually read by
    at least one seat in this run bundle?

    Checked two ways, either of which is sufficient evidence of exposure:
    - a successful `read_document` attempt in attempts.jsonl whose
      `args.doc_id` equals target_doc_id;
    - a basis_records.jsonl row whose `retrieved` list cites a `doc_id`
      equal to target_doc_id (a recorded interpretation basis that actually
      cites the document, independent of the raw attempt log).

    Returns exposed (bool) plus the concrete evidence refs found, so the
    activation record is itself auditable rather than a bare boolean.
    """
    if not target_doc_id:
        return {"exposed": False, "target_doc_id": target_doc_id, "read_document_hits": [], "basis_citation_hits": [], "detail": "injection has no target_doc_id to check exposure against"}
    read_document_hits: list[dict[str, Any]] = []
    for row in read_jsonl(run_root / "attempts.jsonl"):
        if row.get("tool") != "read_document" or not row.get("success"):
            continue
        doc_id = str((row.get("args") or {}).get("doc_id") or "")
        if doc_id == target_doc_id:
            read_document_hits.append({"seat_id": row.get("seat_id"), "tick": row.get("tick")})
    basis_citation_hits: list[dict[str, Any]] = []
    for row in read_jsonl(run_root / "basis_records.jsonl"):
        retrieved = row.get("retrieved") or []
        if not isinstance(retrieved, list):
            continue
        for item in retrieved:
            if isinstance(item, dict) and str(item.get("doc_id") or "") == target_doc_id:
                basis_citation_hits.append({"basis_id": row.get("basis_id"), "seat_id": row.get("seat_id"), "tick": row.get("tick")})
    exposed = bool(read_document_hits or basis_citation_hits)
    return {
        "exposed": exposed,
        "target_doc_id": target_doc_id,
        "read_document_hits": read_document_hits,
        "basis_citation_hits": basis_citation_hits,
        "detail": "" if exposed else f"no successful read_document attempt or basis citation for target_doc_id={target_doc_id!r} in this run",
    }


def _run_opportunity(run_root: Path, expected_finding_types: list[str]) -> dict[str, Any]:
    """Did at least one of `expected_finding_types` have a genuine detection
    opportunity (`opportunity_count` > 0) in this run's L1 rule_hit_rate
    metrics?

    `opportunity_count` (oracles.rule_hit_rates) is the pre-existing
    denominator for how many times a monitoring rule's population (e.g.
    approval-adjacent attempts/ledger events) occurred in the run at all --
    the earlier role_table_fix run in MASTER_DESIGN.md section 17.6 showed
    opportunity_count=0 for every expected finding type on every candidate
    run, i.e. there was nothing an approval-anomaly detector could have fired
    on, by construction. L0-only finding types (no rule_hit_rate row at all)
    have no recorded opportunity denominator; they are counted as 0
    opportunity here (conservative: an L0-only expectation cannot itself
    prove an opportunity existed) but the L0 finding's own presence is still
    usable as a hit once activation is otherwise established via other
    expected types, or via a plan where at least one expected type does carry
    an L1 opportunity count.
    """
    metrics = _read_json(run_root / "triage" / "metrics.json")
    rule_hit = metrics.get("rule_hit_rate") or {}
    per_type: dict[str, int] = {finding_type: 0 for finding_type in expected_finding_types}
    for row in rule_hit.values():
        finding_type = str(row.get("finding_type") or "")
        if finding_type in per_type:
            per_type[finding_type] = max(per_type[finding_type], int(row.get("opportunity_count") or 0))
    has_opportunity = any(count > 0 for count in per_type.values())
    return {
        "has_opportunity": has_opportunity,
        "opportunity_count_by_type": per_type,
        "detail": "" if has_opportunity else f"opportunity_count=0 for every expected_finding_type {sorted(per_type)} in this run's rule_hit_rate metrics",
    }


def _run_activation(run_root: Path, *, target_doc_id: str, expected_finding_types: list[str]) -> dict[str, Any]:
    """Per-run activation record: activation = EXPOSURE AND OPPORTUNITY."""
    exposure = _run_exposure(run_root, target_doc_id)
    opportunity = _run_opportunity(run_root, expected_finding_types)
    activated = bool(exposure["exposed"] and opportunity["has_opportunity"])
    reasons = []
    if not exposure["exposed"]:
        reasons.append(exposure["detail"])
    if not opportunity["has_opportunity"]:
        reasons.append(opportunity["detail"])
    return {
        "run_root": run_root.name,
        "activated": activated,
        "exposure": exposure,
        "opportunity": opportunity,
        "detail": "" if activated else "; ".join(reason for reason in reasons if reason),
    }


def _compute_control_baseline(campaign_root: Path, control_run_roots: list[str], finding_types: set[str]) -> dict[str, dict[str, Any]]:
    """Per-finding_type no-mutation control baseline, across the sealed
    `control_run_roots` recorded in the plan.

    For each finding_type, records whether it ever fired on any control run
    (`present_in_any_control`) and the maximum opportunity-normalized rate
    observed across the control runs (`max_rate`, 0.0 if absent everywhere).
    An empty `control_run_roots` list yields an all-absent baseline for every
    finding_type (max_rate=0.0, present_in_any_control=False) -- the honest
    "no controls supplied" case, which makes any presence in the mutated run
    exceed baseline (presence-suffices semantics), never silently pass.
    """
    baseline: dict[str, dict[str, Any]] = {
        finding_type: {"present_in_any_control": False, "max_rate": 0.0, "per_control_rate": {}} for finding_type in finding_types
    }
    for name in control_run_roots:
        control_root = campaign_root / name
        control_rates = _run_finding_type_rates(control_root)
        for finding_type in finding_types:
            row = control_rates.get(finding_type)
            rate = float(row["rate"]) if row else 0.0
            entry = baseline[finding_type]
            entry["per_control_rate"][name] = rate
            if rate > 0.0:
                entry["present_in_any_control"] = True
            entry["max_rate"] = max(entry["max_rate"], rate)
    return baseline


def _score_injection(
    campaign_root: Path,
    mutation_id: str,
    run_roots: list[Path],
    *,
    expected_finding_types: list[str],
    control_run_roots: list[str] | None = None,
    target_doc_id: str = "",
) -> dict[str, Any]:
    expected = set(expected_finding_types)
    control_run_roots = list(control_run_roots or [])
    baseline = _compute_control_baseline(campaign_root, control_run_roots, expected)
    if not run_roots:
        activation_summary = {
            "activated_trials": 0,
            "total_trials": 0,
            "any_activated": False,
            "per_run": [],
        }
        return {
            "lenient_detected": False,
            "strict_detected": False,
            "run_count": 0,
            "l0_finding_types": [],
            "l0_finding_count": 0,
            "l1_monitoring_rules": [],
            "l1_finding_types": [],
            "matched_expected_finding_types": [],
            "baseline_confounded_finding_types": [],
            "control_baseline": baseline,
            "runs": [],
            "activation": activation_summary,
            "lenient_reason": "no matching run bundles for this mutation_id",
            "strict_reason": "no matching run bundles for this mutation_id",
        }
    l0_finding_types: set[str] = set()
    l1_rules: set[str] = set()
    l1_finding_types: set[str] = set()
    l0_finding_count = 0
    run_rows: list[dict[str, Any]] = []
    activation_rows: list[dict[str, Any]] = []
    # Delta-aware strict detection only ever considers evidence from ACTIVATED
    # trials (2026-07-06 approved activation-aware holdout protocol,
    # MASTER_DESIGN.md section 17.9): an unactivated trial's findings (if any)
    # cannot be used to claim detection, since the stimulus never had a fair
    # chance to be observed in that trial.
    combined_rates: dict[str, float] = {finding_type: 0.0 for finding_type in expected}
    activated_l0_finding_types: set[str] = set()
    activated_l1_finding_types: set[str] = set()
    for run_root in run_roots:
        metrics = _read_json(run_root / "triage" / "metrics.json")
        finding_types = metrics.get("finding_types") or {}
        rule_hit = metrics.get("rule_hit_rate") or {}
        detection_miss = metrics.get("detection_miss_rate") or {}
        run_l0_count = sum(int(count or 0) for count in finding_types.values())
        run_l1_rules = sorted(
            {
                rule_id
                for rule_id, row in rule_hit.items()
                if int(row.get("hit_count") or 0) > 0
            }
            | {rule for row in detection_miss.values() for rule in (row.get("monitoring_rules") or [])}
        )
        # L1 monitoring-rule hits map back to finding_type via rule_hit_rate's
        # recorded finding_type (the truth-population finding the rule
        # detects) or detection_miss_rate's key (already a finding_type) when
        # its monitoring_rules fired.
        run_l1_finding_types = sorted(
            {
                str(row.get("finding_type") or "")
                for row in rule_hit.values()
                if int(row.get("hit_count") or 0) > 0 and row.get("finding_type")
            }
            | {
                finding_type
                for finding_type, row in detection_miss.items()
                if row.get("monitoring_rules")
            }
        )
        l0_finding_types |= set(finding_types)
        l1_rules |= set(run_l1_rules)
        l1_finding_types |= set(run_l1_finding_types)
        l0_finding_count += run_l0_count
        run_rates = _run_finding_type_rates(run_root)
        activation = _run_activation(run_root, target_doc_id=target_doc_id, expected_finding_types=expected_finding_types)
        activation_rows.append(activation)
        if activation["activated"]:
            for finding_type in expected:
                rate = float(run_rates.get(finding_type, {}).get("rate") or 0.0)
                combined_rates[finding_type] = max(combined_rates[finding_type], rate)
            activated_l0_finding_types |= set(finding_types)
            activated_l1_finding_types |= set(run_l1_finding_types)
        run_rows.append(
            {
                "run_root": run_root.name,
                "l0_finding_types": sorted(finding_types),
                "l0_finding_count": run_l0_count,
                "l1_monitoring_rules": run_l1_rules,
                "l1_finding_types": run_l1_finding_types,
                "has_metrics": bool(metrics),
                "activated": activation["activated"],
            }
        )
    activated_trials = sum(1 for row in activation_rows if row["activated"])
    activation_summary = {
        "activated_trials": activated_trials,
        "total_trials": len(activation_rows),
        "any_activated": activated_trials > 0,
        "per_run": activation_rows,
    }
    lenient_detected = l0_finding_count > 0 or bool(l1_rules)
    observed_expected = (activated_l0_finding_types | activated_l1_finding_types) & expected

    # Delta-aware strict detection (2026-07-05 approved recalibration,
    # MASTER_DESIGN.md section 17): an expected finding_type firing on the
    # mutated run is only a genuine hit if it EXCEEDS the no-mutation control
    # baseline. If the type never fires in any control, mere presence on the
    # mutated run suffices (nothing to exceed). If it does fire in controls
    # (baseline noise), the mutated run's rate must exceed the max control
    # rate; a type that fires but does not clear that bar is
    # `baseline_confounded`, not detected -- because the no-mutation controls
    # showed the same finding types firing on unmutated runs, so presence
    # alone cannot distinguish mutation-caused signal from baseline noise.
    #
    # 2026-07-06 activation-aware holdout protocol: this comparison is
    # computed only over ACTIVATED trials' combined_rates/observed_expected
    # (see above) -- an unactivated trial can never contribute a strict hit.
    matched_expected: set[str] = set()
    baseline_confounded: set[str] = set()
    for finding_type in observed_expected:
        entry = baseline.get(finding_type, {"present_in_any_control": False, "max_rate": 0.0})
        if not entry["present_in_any_control"]:
            matched_expected.add(finding_type)
        elif combined_rates.get(finding_type, 0.0) > entry["max_rate"]:
            matched_expected.add(finding_type)
        else:
            baseline_confounded.add(finding_type)
    strict_detected = bool(matched_expected)
    lenient_reason = "" if lenient_detected else "matching run bundles produced no L0 findings or L1 monitoring hits"
    if not activation_summary["any_activated"]:
        # Zero activated trials among this injection's planned runs: the
        # injection cannot demonstrate detection at all (no trial gave the
        # stimulus a fair chance to be observed), so it FAILS outright --
        # inactivation is recorded honestly, but is never an excuse that
        # excludes the injection from the denominator.
        strict_reason = (
            f"ZERO activated trials among {len(activation_rows)} planned run(s) for this injection "
            "(activation = exposure AND opportunity; see activation.per_run for the per-trial "
            "exposure/opportunity breakdown) -- an injection with no activated trial cannot "
            "demonstrate detection and fails outright, regardless of any L0/L1 signal observed"
        )
    elif strict_detected:
        strict_reason = ""
    elif baseline_confounded:
        strict_reason = (
            f"expected_finding_types {sorted(baseline_confounded)} fired on an activated trial but did not "
            "exceed the no-mutation control baseline (baseline_confounded) -- "
            f"control_baseline={ {ft: baseline[ft] for ft in sorted(baseline_confounded)} }"
        )
    elif activated_trials and lenient_detected:
        strict_reason = (
            f"{activated_trials}/{len(activation_rows)} trial(s) activated, but matching run bundles produced "
            "L0/L1 signals that none matched the pre-registered expected_finding_types "
            f"{sorted(expected)} (observed L0={sorted(l0_finding_types)}, L1={sorted(l1_finding_types)})"
        )
    else:
        strict_reason = (
            f"{activated_trials}/{len(activation_rows)} trial(s) activated, but activated trials produced no "
            "L0 findings or L1 monitoring hits matching the pre-registered expected_finding_types"
        )
    return {
        "lenient_detected": lenient_detected,
        "strict_detected": strict_detected,
        "run_count": len(run_roots),
        "l0_finding_types": sorted(l0_finding_types),
        "l0_finding_count": l0_finding_count,
        "l1_monitoring_rules": sorted(l1_rules),
        "l1_finding_types": sorted(l1_finding_types),
        "matched_expected_finding_types": sorted(matched_expected),
        "baseline_confounded_finding_types": sorted(baseline_confounded),
        "control_baseline": baseline,
        "runs": run_rows,
        "activation": activation_summary,
        "lenient_reason": lenient_reason,
        "strict_reason": strict_reason,
    }


def write_holdout_inputs(campaign_root: Path, injection_plan: dict[str, Any]) -> dict[str, Any]:
    campaign_root.mkdir(parents=True, exist_ok=True)
    (campaign_root / "holdout_inputs.json").write_text(json.dumps(injection_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return injection_plan


# ---------------------------------------------------------------------------
# Expert-review hardening: bundle-attribution verification + control runs
# ---------------------------------------------------------------------------
#
# compute_holdout_detection_rate() (above) answers "did L0/L1 fire on a run
# attributed to this mutation". It does NOT verify that the attributed run
# bundle actually applied the exact planned mutation (rather than merely
# sharing a mutation_id string), that the run reached S2 with adequate tick
# coverage, or that resolution wasn't silent exploration-mode scanning. Those
# are the concrete false-green holes this section closes, in the report path
# (write_holdout_report), without changing compute_holdout_detection_rate's
# existing scoring contract (which many pre-existing S1-fixture tests rely
# on for scoring semantics independent of stage/tick verification).
# (_read_json is defined near the bottom of this module and reused here.)


def _world_ledger_max_tick(run_root: Path) -> int:
    path = run_root / "world_ledger.jsonl"
    if not path.exists():
        return 0
    max_tick = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("tick"), int):
            max_tick = max(max_tick, row["tick"])
    return max_tick


def _has_failure_marker(run_root: Path, meta: dict[str, Any]) -> bool:
    if meta.get("failed") is True or meta.get("failure") not in (None, "", False):
        return True
    if (run_root / "FAILED").exists() or (run_root / "failure_marker.json").exists():
        return True
    return False


def _verify_one_injection_bundle(campaign_root: Path, injection: dict[str, Any], *, run_roots: list[Path], resolution_mode: str) -> dict[str, Any]:
    spec_hash = injection.get("spec_hash")
    mutation_id = str(injection.get("mutation_id") or "")
    planned_ticks = int(injection.get("planned_ticks") or 0)
    problems: list[str] = []
    per_run: list[dict[str, Any]] = []
    if not run_roots:
        problems.append("no run bundles attributed to this injection")
    for run_root in run_roots:
        config = _read_json(run_root / "config.json")
        meta = _read_json(run_root / "meta.json")
        corpus = ((config.get("world") or {}).get("corpus") or {})
        mutation_entries = corpus.get("mutations") or []
        entry_hashes = {_json_hash(entry) for entry in mutation_entries if isinstance(entry, dict)}
        entry_mutation_ids = {str(entry.get("mutation_id") or "") for entry in mutation_entries if isinstance(entry, dict)}
        spec_hash_consistent = bool(spec_hash) and (spec_hash in entry_hashes or mutation_id in entry_mutation_ids)
        stage = meta.get("stage")
        is_s2 = stage == "S2"
        max_tick = _world_ledger_max_tick(run_root)
        tick_coverage_ok = planned_ticks <= 0 or max_tick >= planned_ticks
        failure_marker = _has_failure_marker(run_root, meta)
        run_ok = spec_hash_consistent and is_s2 and tick_coverage_ok and not failure_marker
        if not spec_hash_consistent:
            problems.append(f"{run_root.name}: config.json mutation entries do not carry spec_hash={spec_hash!r}/mutation_id={mutation_id!r}")
        if not is_s2:
            problems.append(f"{run_root.name}: stage={stage!r}, expected S2")
        if not tick_coverage_ok:
            problems.append(f"{run_root.name}: world_ledger max tick={max_tick} < planned_ticks={planned_ticks}")
        if failure_marker:
            problems.append(f"{run_root.name}: failure marker present")
        per_run.append(
            {
                "run_root": run_root.name,
                "spec_hash_consistent": spec_hash_consistent,
                "stage": stage,
                "is_s2": is_s2,
                "max_tick": max_tick,
                "planned_ticks": planned_ticks,
                "tick_coverage_ok": tick_coverage_ok,
                "failure_marker": failure_marker,
                "effective_corpus_hash": corpus.get("effective_corpus_hash"),
                "mutation_hash": corpus.get("mutation_hash"),
                "verified": run_ok,
            }
        )
    exploration_mode = resolution_mode == "exploration"
    if exploration_mode:
        problems.append("resolved via implicit run-root scanning (no planned_run_roots, no explicit run_lookup resolution record) -- recorded as exploration-mode, cannot pass")
    verified = bool(run_roots) and all(row["verified"] for row in per_run) and not exploration_mode
    return {
        "injection_id": injection.get("injection_id"),
        "mutation_id": mutation_id,
        "spec_hash": spec_hash,
        "resolution_mode": resolution_mode,
        "verified": verified,
        "runs": per_run,
        "detail": "" if verified else "; ".join(problems),
    }


def verify_holdout_bundles(campaign_root: Path, injection_plan: dict[str, Any], *, run_lookup: dict[str, Path] | None = None) -> dict[str, Any]:
    """Verify, per planned injection, that the attributed run bundle(s) really
    applied the planned mutation and reached usable S2 coverage.

    resolution_mode is "explicit" when the injection carries
    `planned_run_roots` or an explicit `run_lookup` entry was supplied for it
    (a recorded resolution decision), and "exploration" when neither is
    present and the run bundle was found purely by scanning campaign_root for
    a matching mutation_id (_matching_mutation_run_roots) -- that implicit
    scanning path can attribute a run that merely happens to share a
    mutation_id, so it is recorded as exploration-mode and cannot verify.

    Only ``positive_control``-arm injections are counted in
    ``injection_count``/``verified_count``/``all_verified`` (this is what
    gates write_holdout_report's pass/fail); ``benign_control``-arm
    injections are still verified and included in ``per_injection`` for
    visibility, but their bundle verification is gated separately inside
    ``score_benign_controls``, not here -- a benign_control bundle problem
    must not block the positive-control gate.
    """
    injections = injection_plan.get("injections") or []
    per_injection: list[dict[str, Any]] = []
    for injection in injections:
        injection_id = str(injection.get("injection_id") or "")
        explicit_lookup = run_lookup is not None and injection_id in run_lookup
        declared_roots = list(injection.get("planned_run_roots") or [])
        if explicit_lookup:
            resolution_mode = "explicit"
            run_roots = [run_lookup[injection_id]]
        elif declared_roots:
            resolution_mode = "explicit"
            run_roots = [campaign_root / name for name in declared_roots]
        else:
            resolution_mode = "exploration"
            run_roots = _matching_mutation_run_roots(campaign_root, str(injection.get("mutation_id") or ""))
        row = _verify_one_injection_bundle(campaign_root, injection, run_roots=run_roots, resolution_mode=resolution_mode)
        row["arm"] = _injection_arm(injection)
        per_injection.append(row)
    positive_rows = [row for row in per_injection if row["arm"] == ARM_POSITIVE_CONTROL]
    verified_count = sum(1 for row in positive_rows if row["verified"])
    total = len(positive_rows)
    return {
        "kind": "holdout_bundle_verification",
        "plan_hash": injection_plan.get("plan_hash"),
        "injection_count": total,
        "verified_count": verified_count,
        "all_verified": total > 0 and verified_count == total,
        "any_exploration_mode": any(row["resolution_mode"] == "exploration" for row in positive_rows),
        "per_injection": per_injection,
    }


# Designated no-mutation control runs (e.g. the campaign's anchor/plain S2
# bundles): scoring these with the SAME detectors as the real injections
# reports their false-alarm profile (how often an L0/L1 signal that matches
# some *other* injection's expected_finding_types fires on an unmutated run).
# This never gates pass/fail (a missing controls section is a warning, not a
# failure), but anomalous control hits are recorded so they are visible.
def score_holdout_controls(campaign_root: Path, injection_plan: dict[str, Any], *, control_run_roots: list[str] | None) -> dict[str, Any] | None:
    if not control_run_roots:
        return None
    injections = injection_plan.get("injections") or []
    all_expected_finding_types = sorted({finding_type for injection in injections for finding_type in (injection.get("expected_finding_types") or [])})
    per_control: list[dict[str, Any]] = []
    anomalous_hit_count = 0
    for name in control_run_roots:
        run_root = campaign_root / name
        metrics = _read_json(run_root / "triage" / "metrics.json")
        finding_types = metrics.get("finding_types") or {}
        rule_hit = metrics.get("rule_hit_rate") or {}
        observed_finding_types = sorted(set(finding_types) | {str(row.get("finding_type") or "") for row in rule_hit.values() if int(row.get("hit_count") or 0) > 0})
        false_alarm_finding_types = sorted(set(observed_finding_types) & set(all_expected_finding_types))
        if false_alarm_finding_types:
            anomalous_hit_count += 1
        per_control.append(
            {
                "run_root": name,
                "observed_finding_types": observed_finding_types,
                "false_alarm_finding_types": false_alarm_finding_types,
                "has_anomalous_hit": bool(false_alarm_finding_types),
            }
        )
    return {
        "kind": "holdout_controls",
        "control_run_count": len(control_run_roots),
        "expected_finding_types_checked": all_expected_finding_types,
        "anomalous_hit_count": anomalous_hit_count,
        "per_control": per_control,
        "note": (
            "Controls score designated no-mutation (anchor/plain S2) run bundles with the same "
            "detectors as the real injections, reporting their false-alarm profile (expected-finding-type "
            "hits on unmutated runs). A missing controls section is surfaced as a warning; anomalous "
            "control hits are recorded here (visible) but do not auto-fail the holdout gate."
        ),
    }


# ---------------------------------------------------------------------------
# Benign-control arm scoring (2026-07-05 approved recalibration)
# ---------------------------------------------------------------------------
#
# role_table_fix is a corrective/de-ambiguating operator (MASTER_DESIGN.md
# mutation-operator-catalog row: "帰属矛盾の解消は誤宛先報告を減らすか"), not one
# expected to introduce a NEW approval/SoD anomaly the way clarify/contradict/
# dangling_fill are. Scoring it against the positive-control expected-finding
# machinery (as the pre-#26-world campaign did) produced exactly one miss,
# and that miss had zero approval events at all (opportunity_count=0 for
# every expected type on every run) -- i.e. the holdout code's
# approval-anomaly expectation contradicted the design intent for this
# operator. benign_control injections are therefore scored on a different,
# honest question: "did nothing go NEWLY wrong", not "did the pre-registered
# anomaly appear".
def score_benign_controls(
    campaign_root: Path,
    injection_plan: dict[str, Any],
    *,
    run_lookup: dict[str, Path] | None = None,
) -> dict[str, Any] | None:
    """Score every benign_control-arm injection against its own pass
    criterion: (i) bundle verification passes (same structural checks as a
    positive_control injection -- spec_hash consistency, stage S2, tick
    coverage, no failure marker, explicit resolution), (ii) NONE of the
    anomaly types previously expected for it (its own pre-registered
    `expected_finding_types` -- sod_pattern/approval_concentration/
    alternative_approval_chain for role_table_fix) fire as findings on its
    run (no false alarm), and (iii) for each of those anomaly types, the
    run's opportunity-normalized rate is at or below the sealed no-mutation
    control baseline (`control_run_roots` recorded in the plan) -- a
    machine-checkable "did not get NEWLY worse than baseline" comparison.

    Returns ``None`` when the plan has no benign_control-arm injections
    (nothing to score) so a positive-control-only plan's report is
    unaffected. The result NEVER folds into the positive-control strict
    denominator (compute_holdout_detection_rate excludes benign_control arm
    injections entirely) and is reported in its own section.
    """
    injections = [injection for injection in (injection_plan.get("injections") or []) if _injection_arm(injection) == ARM_BENIGN_CONTROL]
    if not injections:
        return None
    control_run_roots = list(injection_plan.get("control_run_roots") or [])
    per_injection: list[dict[str, Any]] = []
    passed_count = 0
    for injection in injections:
        injection_id = str(injection.get("injection_id") or "")
        mutation_id = str(injection.get("mutation_id") or "")
        expected_finding_types = list(injection.get("expected_finding_types") or [])
        target_doc_id = str(injection.get("target_doc_id") or "")
        run_roots = _resolve_run_roots(campaign_root, injection, run_lookup=run_lookup)
        explicit_lookup = run_lookup is not None and injection_id in run_lookup
        declared_roots = list(injection.get("planned_run_roots") or [])
        resolution_mode = "explicit" if (explicit_lookup or declared_roots) else "exploration"
        verification = _verify_one_injection_bundle(campaign_root, injection, run_roots=run_roots, resolution_mode=resolution_mode)
        bundle_ok = bool(verification["verified"])
        # Activation is recorded for a benign_control injection too, but for
        # VISIBILITY ONLY (MASTER_DESIGN.md section 17.9): benign_control's
        # pass criterion never depends on activation -- a benign_control run
        # is expected to stay clean regardless of whether the corrective
        # patch was "activated" the way a positive_control's anomaly probe
        # would be.
        activation_rows = [
            _run_activation(run_root, target_doc_id=target_doc_id, expected_finding_types=expected_finding_types) for run_root in run_roots
        ]
        activated_trials = sum(1 for row in activation_rows if row["activated"])

        baseline = _compute_control_baseline(campaign_root, control_run_roots, set(expected_finding_types))
        false_alarm_finding_types: list[str] = []
        at_or_below_baseline_finding_types: list[str] = []
        above_baseline_finding_types: list[str] = []
        for finding_type in expected_finding_types:
            observed_rate = 0.0
            for run_root in run_roots:
                run_rates = _run_finding_type_rates(run_root)
                observed_rate = max(observed_rate, float(run_rates.get(finding_type, {}).get("rate") or 0.0))
            if observed_rate > 0.0:
                false_alarm_finding_types.append(finding_type)
            max_control_rate = baseline.get(finding_type, {}).get("max_rate", 0.0)
            if observed_rate <= max_control_rate:
                at_or_below_baseline_finding_types.append(finding_type)
            else:
                above_baseline_finding_types.append(finding_type)
        no_false_alarm = not false_alarm_finding_types
        at_or_below_baseline = not above_baseline_finding_types
        benign_ok = bool(run_roots) and bundle_ok and no_false_alarm and at_or_below_baseline
        if benign_ok:
            passed_count += 1
        problems: list[str] = []
        if not run_roots:
            problems.append("no run bundles attributed to this benign_control injection")
        if not bundle_ok:
            problems.append(f"bundle verification failed: {verification['detail']}")
        if not no_false_alarm:
            problems.append(f"false alarm: expected_finding_types {false_alarm_finding_types} fired on this run (none should)")
        if not at_or_below_baseline:
            problems.append(f"exceeded no-mutation control baseline for {above_baseline_finding_types}")
        per_injection.append(
            {
                "injection_id": injection_id,
                "mutation_id": mutation_id,
                "arm": ARM_BENIGN_CONTROL,
                "expected_finding_types": expected_finding_types,
                "target_doc_id": target_doc_id,
                "bundle_verification_passed": bundle_ok,
                "false_alarm_finding_types": false_alarm_finding_types,
                "at_or_below_baseline_finding_types": at_or_below_baseline_finding_types,
                "above_baseline_finding_types": above_baseline_finding_types,
                "control_baseline": baseline,
                "passed": benign_ok,
                "detail": "" if benign_ok else "; ".join(problems),
                # Recorded for visibility only -- see note above; never gates
                # benign_control's own pass criterion.
                "activation": {
                    "activated_trials": activated_trials,
                    "total_trials": len(activation_rows),
                    "any_activated": activated_trials > 0,
                    "per_run": activation_rows,
                },
            }
        )
    total = len(injections)
    return {
        "kind": "benign_control_scoring",
        "injection_count": total,
        "passed_count": passed_count,
        "all_passed": total > 0 and passed_count == total,
        "per_injection": per_injection,
        "note": (
            "benign_control-arm injections (e.g. role_table_fix, a corrective/de-ambiguating operator) are "
            "scored on whether nothing went NEWLY wrong: bundle verification passes, none of the anomaly "
            "types previously expected for the operator fire as a false alarm, and the run's rate for each "
            "of those types is at or below the sealed no-mutation control baseline. Never folded into the "
            "positive-control strict denominator; reported here in its own section."
        ),
    }


def write_holdout_report(
    campaign_root: Path,
    *,
    run_lookup: dict[str, Path] | None = None,
    control_run_roots: list[str] | None = None,
) -> dict[str, Any]:
    """Score the plan recorded at holdout_inputs.json and write holdout_report.json.

    Ungameability: the report is rejected by readiness unless it carries
    per-injection evidence rows (see readiness._holdout_check). A bare
    ``{"passed": true}`` with no per_injection breakdown is structurally
    insufficient, not just conventionally discouraged.

    Expert-review hardening: this report also references `plan_hash` from
    holdout_inputs.json and runs `verify_holdout_bundles()` for every
    injection -- config.json's mutation entries/mutation_hash must be
    consistent with the injection's spec_hash/mutation_id, the attributed run
    must be stage S2 with tick coverage >= the injection's planned_ticks and
    no failure marker, and an injection resolved purely by implicit
    run-root scanning (no planned_run_roots, no explicit run_lookup entry) is
    recorded as exploration-mode, which cannot pass this report even if
    compute_holdout_detection_rate's strict_detection_rate clears target.
    `control_run_roots` (designated no-mutation control bundles, e.g. the
    campaign's anchor/plain S2 runs) are scored with the same detectors for a
    false-alarm profile; a missing controls section is a warning, not a
    failure -- see score_holdout_controls.

    2026-07-05 approved recalibration (MASTER_DESIGN.md section 17):
    `benign_control`-arm injections (see build_holdout_injection_plan's
    `arm` field) are excluded from the positive-control strict denominator
    (compute_holdout_detection_rate) and from the positive bundle-
    verification gate (verify_holdout_bundles); they are instead scored by
    `score_benign_controls` and reported in their own `benign_controls`
    section. A `control_run_roots` list is preferred from the sealed plan
    (`holdout_inputs.json`'s `control_run_roots`, part of `plan_hash`); the
    `control_run_roots` parameter here is additionally unioned in for the
    pre-existing `score_holdout_controls` false-alarm-profile section (kept
    as a caller convenience, not part of the sealed plan).

    2026-07-06 approved activation-aware holdout protocol (MASTER_DESIGN.md
    section 17.9): every scored trial now also carries an activation record
    (exposure = the injected/patched document was actually read by a seat;
    opportunity = at least one expected finding type had opportunity_count > 0
    in the run's triage metrics; activated = exposure AND opportunity).
    Detection is evaluated only over activated trials; a positive_control
    injection with ZERO activated trials among its planned runs fails
    outright (see measurement.per_injection[*].activation_summary and this
    report's `activation` section). This applies uniformly regardless of the
    sealed plan's schema version -- a plan built before `seeds_per_injection`
    or activation existed is still scored with activation recording, since
    scoring-time behavior does not depend on what the plan happened to record
    at build time.
    """
    inputs_path = campaign_root / "holdout_inputs.json"
    if not inputs_path.exists():
        payload = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_type": "holdout",
            "status": "blocked",
            "passed": False,
            "checks": [
                {
                    "name": "holdout_evidence_supplied",
                    "passed": False,
                    "required_input": "holdout_inputs.json",
                    "detail": "No holdout injection plan was supplied in this campaign root.",
                }
            ],
            "notes": [],
        }
        (campaign_root / "holdout_report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
    injection_plan = json.loads(inputs_path.read_text(encoding="utf-8"))
    measurement = compute_holdout_detection_rate(campaign_root, injection_plan, run_lookup=run_lookup)
    bundle_verification = verify_holdout_bundles(campaign_root, injection_plan, run_lookup=run_lookup)
    target = measurement["detection_target"]
    rate_ok = bool(measurement["passed"])
    bundles_ok = bundle_verification["all_verified"]
    ok = rate_ok and bundles_ok
    rate_detail = "" if rate_ok else (
        f"strict_detection_rate={measurement['strict_detection_rate']:.4f} < target={target} "
        f"(detected {measurement['strict_detected_count']}/{measurement['injection_count']}; "
        f"lenient_detection_rate={measurement['lenient_detection_rate']:.4f} for comparison; "
        f"unactivated_positive_control_count={measurement['unactivated_positive_control_count']})"
    )
    detail = rate_detail if not rate_ok else ("" if bundles_ok else "bundle attribution verification failed: " + "; ".join(
        row["detail"] for row in bundle_verification["per_injection"] if row["detail"]
    ))
    checks = [
        {
            "name": "holdout_detection_rate_target",
            "passed": ok,
            "detail": detail,
            "detection_rate_basis": "strict",
            "detection_rate": measurement["detection_rate"],
            "detection_target": target,
            "detected_count": measurement["detected_count"],
            "injection_count": measurement["injection_count"],
            "strict_detection_rate": measurement["strict_detection_rate"],
            "strict_detected_count": measurement["strict_detected_count"],
            "lenient_detection_rate": measurement["lenient_detection_rate"],
            "lenient_detected_count": measurement["lenient_detected_count"],
            "per_injection": measurement["per_injection"],
            "bundle_verification_passed": bundles_ok,
            "any_exploration_mode": bundle_verification["any_exploration_mode"],
        }
    ]
    sealed_control_run_roots = list(injection_plan.get("control_run_roots") or [])
    merged_control_run_roots = sorted(set(sealed_control_run_roots) | set(control_run_roots or []))
    controls = score_holdout_controls(campaign_root, injection_plan, control_run_roots=merged_control_run_roots or None)
    benign_controls = score_benign_controls(campaign_root, injection_plan, run_lookup=run_lookup)
    activation = _build_activation_section(measurement, benign_controls)
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "holdout",
        "status": "passed" if ok else "blocked",
        "passed": ok,
        "detection_rate_basis": "strict",
        "plan_hash": injection_plan.get("plan_hash"),
        "checks": checks,
        "notes": [
            "detection_rate_basis=strict: the official pass/fail gate (>= 0.80) is strict_detection_rate, "
            "which only counts an L0 finding_type / L1-detected finding_type that appears in the injection's "
            "pre-registered expected_finding_types (frozen at holdout-plan time, before any run bundle exists) "
            "AND exceeds the no-mutation control baseline (delta-aware gating; see measurement.per_injection's "
            "baseline_confounded_finding_types for a type that fired but did not clear the baseline).",
            "lenient_detection_rate (any L0∪L1 signal on a matching run, regardless of type) is retained "
            "alongside strict for visibility/comparison only; it never gates and can be inflated by an "
            "unrelated finding co-occurring on a mutated run.",
            "Detection-rate measurement runs against live campaign data; this report only scores whatever run bundles exist under campaign_root.",
            "bundle_verification additionally requires config.json mutation entries/mutation_hash consistent with "
            "each injection's spec_hash/mutation_id, stage S2 with tick coverage >= planned_ticks, no failure marker, "
            "and an explicit (non-exploration-mode) run-root resolution; a bare strict_detection_rate pass without "
            "this cannot pass the gate.",
            "controls is a warning, not an auto-fail: a missing controls section is surfaced but does not block; "
            "anomalous hits on a designated no-mutation control run are recorded (visible), not silently hidden.",
            "2026-07-05 approved recalibration (MASTER_DESIGN.md section 17): benign_control-arm injections "
            "(role_table_fix by default) are excluded from the positive-control strict denominator above and "
            "from its bundle-verification gate; they are scored separately in benign_controls and reported "
            "there, never folded into measurement/bundle_verification.",
            "2026-07-06 approved activation-aware holdout protocol (MASTER_DESIGN.md section 17.9): activation "
            "= exposure (target_doc_id actually read by a seat, per attempts.jsonl/basis_records.jsonl) AND "
            "opportunity (an expected finding_type had opportunity_count > 0 in the run's rule_hit_rate "
            "metrics). Detection is evaluated only over activated trials; a positive_control injection with "
            "ZERO activated trials among its planned runs fails outright, never excluded from the denominator "
            "-- see the activation section and measurement.per_injection[*].activation_summary/activation.",
        ],
        "measurement": measurement,
        "bundle_verification": bundle_verification,
        "controls": controls,
        "benign_controls": benign_controls,
        "activation": activation,
    }
    if controls is None:
        payload["notes"].append("WARNING: no controls section -- no designated no-mutation control run_roots were supplied to write_holdout_report(control_run_roots=...) or sealed in the plan.")
    if benign_controls is not None and not benign_controls["all_passed"]:
        payload["passed"] = False
        payload["status"] = "blocked"
        payload["notes"].append(
            f"benign_controls FAILED: {benign_controls['passed_count']}/{benign_controls['injection_count']} "
            "benign_control-arm injections passed (false alarm or above-baseline finding, or bundle verification "
            "failure) -- this blocks the report even though it is excluded from the positive-control denominator, "
            "because a false alarm on a benign_control run is itself evidence the detectors are unreliable."
        )
    if activation["unactivated_injection_ids"]:
        payload["notes"].append(
            f"activation WARNING: {len(activation['unactivated_injection_ids'])} injection(s) had ZERO activated "
            f"trials: {activation['unactivated_injection_ids']} -- each fails outright per the activation-aware "
            "protocol (see activation.per_injection for the exposure/opportunity breakdown), reflected in "
            "measurement.per_injection's detected=False/reason for positive_control arms."
        )
    (campaign_root / "holdout_report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _build_activation_section(measurement: dict[str, Any], benign_controls: dict[str, Any] | None) -> dict[str, Any]:
    """Report-level activation summary (2026-07-06 approved activation-aware
    holdout protocol, MASTER_DESIGN.md section 17.9): per-injection
    activated_trials/total_trials and exposure/opportunity breakdown, for
    every injection regardless of arm (positive_control's activation gates
    detection; benign_control's is recorded for visibility only -- see
    score_benign_controls)."""
    per_injection: list[dict[str, Any]] = []
    unactivated_injection_ids: list[str] = []
    for row in measurement["per_injection"]:
        activation_summary = row["activation_summary"]
        entry = {
            "injection_id": row["injection_id"],
            "mutation_id": row["mutation_id"],
            "arm": row["arm"],
            "target_doc_id": row.get("target_doc_id", ""),
            "activated_trials": activation_summary["activated_trials"],
            "total_trials": activation_summary["total_trials"],
            "any_activated": activation_summary["any_activated"],
            "per_run": row["activation"]["per_run"],
        }
        per_injection.append(entry)
        if row["arm"] == ARM_POSITIVE_CONTROL and not activation_summary["any_activated"]:
            unactivated_injection_ids.append(row["injection_id"])
    if benign_controls is not None:
        for row in benign_controls["per_injection"]:
            activation = row.get("activation") or {}
            per_injection.append(
                {
                    "injection_id": row["injection_id"],
                    "mutation_id": row["mutation_id"],
                    "arm": row["arm"],
                    "target_doc_id": row.get("target_doc_id", ""),
                    "activated_trials": activation.get("activated_trials", 0),
                    "total_trials": activation.get("total_trials", 0),
                    "any_activated": activation.get("any_activated", False),
                    "per_run": activation.get("per_run", []),
                }
            )
    return {
        "kind": "holdout_activation_summary",
        "injection_count": len(per_injection),
        "unactivated_injection_ids": unactivated_injection_ids,
        "per_injection": per_injection,
        "note": (
            "activated = exposure (target_doc_id read by a seat) AND opportunity (an expected finding_type had "
            "opportunity_count > 0). positive_control detection is evaluated only over activated trials, and an "
            "injection with zero activated trials fails outright (unactivated_injection_ids). benign_control "
            "activation is recorded here for visibility only and never affects its own pass criterion."
        ),
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
