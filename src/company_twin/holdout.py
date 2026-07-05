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
from .world_config import _json_hash

HOLDOUT_INPUTS_SCHEMA_VERSION = "company_twin.holdout_inputs.v1"
HOLDOUT_DETECTION_TARGET = 0.80


def build_holdout_injection_plan(
    root: Path,
    *,
    mutation_ids: list[str] | None = None,
    run_roots: list[str] | None = None,
    planned_ticks: int = 0,
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
    """
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
        injections.append(
            {
                "injection_id": f"holdout_{mutation_id}",
                "mutation_id": mutation_id,
                "operator": spec.get("operator"),
                "action": spec.get("action"),
                "target_doc_id": spec.get("doc_id") or spec.get("target_doc_id"),
                "expected_finding_types": expected_finding_types,
                "spec_hash": _json_hash(spec),
                "planned_run_roots": list(run_roots or []),
                "planned_ticks": int(planned_ticks),
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
        "plan_hash": _json_hash(injections),
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
      ``_expected_finding_types``) count as detected. This is the official
      acceptance basis (``detection_rate_basis: "strict"``).

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
    per_injection: list[dict[str, Any]] = []
    lenient_detected_count = 0
    strict_detected_count = 0
    for injection in injections:
        mutation_id = str(injection.get("mutation_id") or "")
        expected_finding_types = list(injection.get("expected_finding_types") or [])
        run_roots = _resolve_run_roots(campaign_root, injection, run_lookup=run_lookup)
        evidence = _score_injection(campaign_root, mutation_id, run_roots, expected_finding_types=expected_finding_types)
        if evidence["lenient_detected"]:
            lenient_detected_count += 1
        if evidence["strict_detected"]:
            strict_detected_count += 1
        per_injection.append(
            {
                "injection_id": injection.get("injection_id"),
                "mutation_id": mutation_id,
                "spec_hash": injection.get("spec_hash"),
                "expected_finding_types": expected_finding_types,
                # Backward-compatible alias: "detected"/"reason" reflect the
                # official strict basis, matching the top-level passed field.
                "detected": evidence["strict_detected"],
                "reason": evidence["strict_reason"],
                **evidence,
            }
        )
    total = len(injections)
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
        "injection_count": total,
        # Official fields (strict basis) -- these are what gates acceptance.
        "detected_count": strict_detected_count,
        "detection_rate": strict_detection_rate,
        "passed": total > 0 and strict_detection_rate >= target,
        # Both bases kept visible side by side.
        "strict_detected_count": strict_detected_count,
        "strict_detection_rate": strict_detection_rate,
        "lenient_detected_count": lenient_detected_count,
        "lenient_detection_rate": lenient_detection_rate,
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


def _score_injection(
    campaign_root: Path,
    mutation_id: str,
    run_roots: list[Path],
    *,
    expected_finding_types: list[str],
) -> dict[str, Any]:
    expected = set(expected_finding_types)
    if not run_roots:
        return {
            "lenient_detected": False,
            "strict_detected": False,
            "run_count": 0,
            "l0_finding_types": [],
            "l0_finding_count": 0,
            "l1_monitoring_rules": [],
            "l1_finding_types": [],
            "matched_expected_finding_types": [],
            "runs": [],
            "lenient_reason": "no matching run bundles for this mutation_id",
            "strict_reason": "no matching run bundles for this mutation_id",
        }
    l0_finding_types: set[str] = set()
    l1_rules: set[str] = set()
    l1_finding_types: set[str] = set()
    l0_finding_count = 0
    run_rows: list[dict[str, Any]] = []
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
        run_rows.append(
            {
                "run_root": run_root.name,
                "l0_finding_types": sorted(finding_types),
                "l0_finding_count": run_l0_count,
                "l1_monitoring_rules": run_l1_rules,
                "l1_finding_types": run_l1_finding_types,
                "has_metrics": bool(metrics),
            }
        )
    lenient_detected = l0_finding_count > 0 or bool(l1_rules)
    matched_expected = (l0_finding_types | l1_finding_types) & expected
    strict_detected = bool(matched_expected)
    lenient_reason = "" if lenient_detected else "matching run bundles produced no L0 findings or L1 monitoring hits"
    if strict_detected:
        strict_reason = ""
    elif lenient_detected:
        strict_reason = (
            "matching run bundles produced L0/L1 signals but none matched the pre-registered "
            f"expected_finding_types {sorted(expected)} (observed L0={sorted(l0_finding_types)}, "
            f"L1={sorted(l1_finding_types)})"
        )
    else:
        strict_reason = "matching run bundles produced no L0 findings or L1 monitoring hits"
    return {
        "lenient_detected": lenient_detected,
        "strict_detected": strict_detected,
        "run_count": len(run_roots),
        "l0_finding_types": sorted(l0_finding_types),
        "l0_finding_count": l0_finding_count,
        "l1_monitoring_rules": sorted(l1_rules),
        "l1_finding_types": sorted(l1_finding_types),
        "matched_expected_finding_types": sorted(matched_expected),
        "runs": run_rows,
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
        per_injection.append(_verify_one_injection_bundle(campaign_root, injection, run_roots=run_roots, resolution_mode=resolution_mode))
    verified_count = sum(1 for row in per_injection if row["verified"])
    total = len(per_injection)
    return {
        "kind": "holdout_bundle_verification",
        "plan_hash": injection_plan.get("plan_hash"),
        "injection_count": total,
        "verified_count": verified_count,
        "all_verified": total > 0 and verified_count == total,
        "any_exploration_mode": any(row["resolution_mode"] == "exploration" for row in per_injection),
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
        f"lenient_detection_rate={measurement['lenient_detection_rate']:.4f} for comparison)"
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
    controls = score_holdout_controls(campaign_root, injection_plan, control_run_roots=control_run_roots)
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
            "pre-registered expected_finding_types (frozen at holdout-plan time, before any run bundle exists).",
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
        ],
        "measurement": measurement,
        "bundle_verification": bundle_verification,
        "controls": controls,
    }
    if controls is None:
        payload["notes"].append("WARNING: no controls section -- no designated no-mutation control run_roots were supplied to write_holdout_report(control_run_roots=...).")
    (campaign_root / "holdout_report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
