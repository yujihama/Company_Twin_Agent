"""WP-14 holdout-verification machinery.

Stage 9 gate 8 (data/design/MASTER_DESIGN.md section 12, "ホールドアウト検証")
requires a world with known-answer injected anomalies where the oracle and
analysis pipeline are checked against ground truth ("観測所側の検証" -
verification on the observatory side, not the world side). This module is the
offline harness for that gate:

- build_holdout_injection_plan(): selects catalogued WP-06 runtime mutations
  (data/compiled_data/mutation_operators_v1.json) as the known-answer
  injections, stamping each planned injection with a content hash so a later
  live run can be checked against exactly what was planned.
- compute_holdout_detection_rate(): consumes L0 triage findings
  (triage/buckets.json / triage/metrics.json under each run bundle) and L1
  monitoring-rule signals (metrics.json's detection_miss_rate/rule_hit_rate)
  to compute an L0-union-L1 detection rate per injected mutation and overall.
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
) -> dict[str, Any]:
    """Build a WP-14 holdout injection plan from the WP-06 mutation catalog.

    Each planned injection reuses an existing catalogued mutation_id (no new
    world-visible text is authored here) and records a content hash so a
    later live run can be verified to have applied exactly the planned
    mutation. Planning is a pure function of the catalog; it does not touch
    the network and does not execute any run.
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
        injections.append(
            {
                "injection_id": f"holdout_{mutation_id}",
                "mutation_id": mutation_id,
                "operator": spec.get("operator"),
                "action": spec.get("action"),
                "target_doc_id": spec.get("doc_id") or spec.get("target_doc_id"),
                "expected_finding_types": _expected_finding_types(spec),
                "spec_hash": _json_hash(spec),
                "planned_run_roots": list(run_roots or []),
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


def _expected_finding_types(spec: dict[str, Any]) -> list[str]:
    """Best-effort expectation of which L0 finding_type an injected mutation
    should surface, based on the operator family. This is advisory only: the
    detection-rate computer treats *any* L0 finding or L1 monitoring hit tied
    to a run as evidence of detection, so an incomplete mapping here cannot
    inflate the measured rate -- it only narrows the documentation shown in
    the plan."""
    operator = str(spec.get("operator") or "")
    mapping = {
        "clarify": ["grounding_gap", "version_gap"],
        "contradict": ["grounding_gap", "sod_pattern", "tacit_chat_to_action"],
        "dangling_fill": ["grounding_gap", "version_gap"],
        "role_table_fix": ["sod_pattern", "approval_concentration", "alternative_approval_chain"],
    }
    return mapping.get(operator, [])


def compute_holdout_detection_rate(
    campaign_root: Path,
    injection_plan: dict[str, Any],
    *,
    run_lookup: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Score a holdout injection plan against real run-bundle evidence.

    For every planned injection, this looks at run bundles whose recorded
    world.corpus.mutations (config.json) or meta.json mutation_ids include the
    injected mutation_id, and asks: did L0 triage (triage/buckets.json /
    triage/metrics.json finding_types) or L1 monitoring rules
    (triage/metrics.json rule_hit_rate / detection_miss_rate monitoring_rules)
    register *any* signal on that run? This is a per-mutation, per-run
    detection check, not a bare pass/fail flag: every mutation's evidence is
    itemized so the readiness check can reject an unsupported claim.

    run_lookup lets fixtures/tests point injection ids at specific bundles
    without needing real campaign directory scanning; when absent, run roots
    declared in the plan's planned_run_roots are resolved under campaign_root.
    """
    injections = injection_plan.get("injections") or []
    if not injections:
        raise ValueError("injection plan has no injections to score")
    per_injection: list[dict[str, Any]] = []
    detected_count = 0
    for injection in injections:
        mutation_id = str(injection.get("mutation_id") or "")
        run_roots = _resolve_run_roots(campaign_root, injection, run_lookup=run_lookup)
        evidence = _score_injection(campaign_root, mutation_id, run_roots)
        if evidence["detected"]:
            detected_count += 1
        per_injection.append(
            {
                "injection_id": injection.get("injection_id"),
                "mutation_id": mutation_id,
                "spec_hash": injection.get("spec_hash"),
                **evidence,
            }
        )
    total = len(injections)
    detection_rate = detected_count / total if total else 0.0
    target = float(injection_plan.get("detection_target") or HOLDOUT_DETECTION_TARGET)
    return {
        "schema_version": HOLDOUT_INPUTS_SCHEMA_VERSION,
        "kind": "detection_rate_measurement",
        "campaign_root": str(campaign_root),
        "plan_hash": injection_plan.get("plan_hash"),
        "detection_target": target,
        "injection_count": total,
        "detected_count": detected_count,
        "detection_rate": detection_rate,
        "passed": total > 0 and detection_rate >= target,
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


def _score_injection(campaign_root: Path, mutation_id: str, run_roots: list[Path]) -> dict[str, Any]:
    if not run_roots:
        return {
            "detected": False,
            "run_count": 0,
            "l0_finding_types": [],
            "l0_finding_count": 0,
            "l1_monitoring_rules": [],
            "runs": [],
            "reason": "no matching run bundles for this mutation_id",
        }
    l0_finding_types: set[str] = set()
    l1_rules: set[str] = set()
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
        l0_finding_types |= set(finding_types)
        l1_rules |= set(run_l1_rules)
        l0_finding_count += run_l0_count
        run_rows.append(
            {
                "run_root": run_root.name,
                "l0_finding_types": sorted(finding_types),
                "l0_finding_count": run_l0_count,
                "l1_monitoring_rules": run_l1_rules,
                "has_metrics": bool(metrics),
            }
        )
    detected = l0_finding_count > 0 or bool(l1_rules)
    reason = "" if detected else "matching run bundles produced no L0 findings or L1 monitoring hits"
    return {
        "detected": detected,
        "run_count": len(run_roots),
        "l0_finding_types": sorted(l0_finding_types),
        "l0_finding_count": l0_finding_count,
        "l1_monitoring_rules": sorted(l1_rules),
        "runs": run_rows,
        "reason": reason,
    }


def write_holdout_inputs(campaign_root: Path, injection_plan: dict[str, Any]) -> dict[str, Any]:
    campaign_root.mkdir(parents=True, exist_ok=True)
    (campaign_root / "holdout_inputs.json").write_text(json.dumps(injection_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return injection_plan


def write_holdout_report(campaign_root: Path, *, run_lookup: dict[str, Path] | None = None) -> dict[str, Any]:
    """Score the plan recorded at holdout_inputs.json and write holdout_report.json.

    Ungameability: the report is rejected by readiness unless it carries
    per-injection evidence rows (see readiness._holdout_check). A bare
    ``{"passed": true}`` with no per_injection breakdown is structurally
    insufficient, not just conventionally discouraged.
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
    target = measurement["detection_target"]
    ok = bool(measurement["passed"])
    checks = [
        {
            "name": "holdout_detection_rate_target",
            "passed": ok,
            "detail": "" if ok else (
                f"detection_rate={measurement['detection_rate']:.4f} < target={target} "
                f"(detected {measurement['detected_count']}/{measurement['injection_count']})"
            ),
            "detection_rate": measurement["detection_rate"],
            "detection_target": target,
            "detected_count": measurement["detected_count"],
            "injection_count": measurement["injection_count"],
            "per_injection": measurement["per_injection"],
        }
    ]
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "holdout",
        "status": "passed" if ok else "blocked",
        "passed": ok,
        "checks": checks,
        "notes": [
            "L0∪L1 detection: a mutation counts as detected if any matching run bundle produced an L0 triage finding_type or an L1 monitoring-rule hit.",
            "Detection-rate measurement runs against live campaign data; this report only scores whatever run bundles exist under campaign_root.",
        ],
        "measurement": measurement,
    }
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
