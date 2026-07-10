"""Fail-closed campaign aggregation for loss-event experiments.

The sealed batch specification is the execution-condition source of truth;
batch manifests are attempt history; each RunSpec.run_root is the only bridge
to a completed world bundle.  No directory scanning or run-name inference is
used.

The campaign report deliberately keeps three concepts separate:

* loss-event occurrence (event / eligible opportunity),
* direct-discovery coverage (a rule-catalog design fact), and
* related control signals (descriptive only).

An uncovered loss class therefore has a direct detection miss rate of N/A,
not 100%.  A missing detector is a design gap, not an observed detector miss.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .loss_monitoring import (
    LOSS_MONITORING_JOIN_METHOD_VERSION,
    LOSS_MONITORING_SCHEMA_VERSION,
    LOSS_MONITOR_RULE_SCHEMA_VERSION,
    join_loss_events_to_monitoring,
    load_loss_monitor_rules,
)
from .loss_oracle import (
    LOSS_ORACLE_METHOD_VERSION,
    LOSS_ORACLE_SCHEMA_VERSION,
    LOSS_RULES,
    compute_loss_event_findings,
)
from .parallel_runner import BATCH_MANIFEST_SCHEMA_VERSION, BatchSpec, BatchSpecError, RunSpec
from .recorder import read_jsonl


LOSS_CAMPAIGN_PLAN_SCHEMA_VERSION = "company_twin.loss_event_campaign_plan.v1"
LOSS_CAMPAIGN_POLICY_SCHEMA_VERSION = "company_twin.loss_event_campaign_policy.v1"
LOSS_CAMPAIGN_REPORT_SCHEMA_VERSION = "company_twin.loss_event_campaign.v1"
MUTATION_CIRCULATION_GATE_SCHEMA_VERSION = "company_twin.mutation_circulation_gate.v1"
MUTATION_CIRCULATION_GATE_REPORT_SCHEMA_VERSION = "company_twin.mutation_circulation_gate_report.v1"

_KNOWN_ENDPOINTS = {
    ("R1/R2", "unconfirmed_vulnerable_sale"),
    ("R3", "unverified_completion"),
    ("R4", "unapproved_completion"),
}
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class LossCampaignError(ValueError):
    """Raised before a campaign report is written when any input drifts."""


@dataclass(frozen=True)
class ResolvedCampaignRun:
    batch_run_id: str
    bundle_run_id: str
    contrast_id: str
    condition: str
    seed: int
    run_root: Path
    run_spec: RunSpec
    successful_attempt: dict[str, Any]
    superseded_failed_attempts: tuple[dict[str, Any], ...]
    monitoring: dict[str, Any]
    meta: dict[str, Any]
    config: dict[str, Any]
    ledger: tuple[dict[str, Any], ...]
    source_hashes: dict[str, str]


def load_loss_campaign_plan(path: Path, *, root: Path) -> dict[str, Any]:
    """Load and validate the sealed plan's self-contained schema."""
    root = Path(root).resolve()
    path = _resolve_input_path(root, path, label="plan")
    payload = _read_json_object(path)
    _validate_plan(payload)
    return payload


def resolve_loss_campaign_runs(
    plan_path: Path,
    *,
    batch_manifest_paths: Sequence[Path],
    root: Path,
) -> list[ResolvedCampaignRun]:
    """Resolve and fully validate all sealed runs without aggregating them."""
    root = Path(root).resolve()
    plan_path = _resolve_input_path(root, plan_path, label="plan")
    plan = load_loss_campaign_plan(plan_path, root=root)
    batch_spec_path = _resolve_plan_path(root, str(plan["batch_spec"]), label="batch spec")
    actual_batch_hash = _file_sha256(batch_spec_path)
    if actual_batch_hash != plan["batch_spec_sha256"]:
        raise LossCampaignError(
            f"batch spec sha256 mismatch: plan={plan['batch_spec_sha256']}, actual={actual_batch_hash}"
        )
    raw_batch_spec = _read_json_object(batch_spec_path)
    try:
        batch_spec = BatchSpec.from_dict(raw_batch_spec)
    except (AttributeError, BatchSpecError, TypeError, ValueError) as exc:
        raise LossCampaignError(f"invalid sealed batch spec: {exc}") from exc

    assignments = _validate_sealed_batch_spec(plan, batch_spec, root=root)
    attempts_by_run, manifests = _load_manifest_chain(
        batch_manifest_paths,
        batch_spec=batch_spec,
        root=root,
    )
    _validate_execution_seal(
        plan_path=plan_path,
        batch_spec_path=batch_spec_path,
        execution_commit=str(manifests[0]["git_commit"]),
        root=root,
    )
    rules = load_loss_monitor_rules(root)
    rules_hash = _canonical_sha256(rules)
    if plan["policy"]["input_contract"]["monitor_rules_sha256"] != rules_hash:
        raise LossCampaignError("sealed policy monitor_rules_sha256 disagrees with the canonical rule catalog")
    resolved: list[ResolvedCampaignRun] = []
    specs_by_id = {run.run_id: run for run in batch_spec.runs}
    for batch_run_id in sorted(assignments):
        assignment = assignments[batch_run_id]
        run_spec = specs_by_id[batch_run_id]
        attempts = attempts_by_run[batch_run_id]
        successes = [attempt for attempt in attempts if attempt["status"] == "succeeded"]
        if len(successes) != 1 or attempts[-1]["status"] != "succeeded":
            raise LossCampaignError(f"run {batch_run_id!r} must have exactly one final successful attempt")
        run_root = _resolve_run_root(root, run_spec.run_root)
        bundle = _load_and_validate_bundle(
            run_root,
            run_spec=run_spec,
            rules=rules,
        )
        resolved.append(
            ResolvedCampaignRun(
                batch_run_id=batch_run_id,
                bundle_run_id=bundle["bundle_run_id"],
                contrast_id=assignment["contrast_id"],
                condition=assignment["condition"],
                seed=int(assignment["seed"]),
                run_root=run_root,
                run_spec=run_spec,
                successful_attempt=successes[0],
                superseded_failed_attempts=tuple(
                    attempt for attempt in attempts[:-1] if attempt["status"] == "failed"
                ),
                monitoring=bundle["monitoring"],
                meta=bundle["meta"],
                config=bundle["config"],
                ledger=tuple(bundle["ledger"]),
                source_hashes=bundle["source_hashes"],
            )
        )
    _validate_pair_bundle_deltas(plan, resolved)
    return resolved


def build_loss_event_campaign_report(
    plan_path: Path,
    *,
    batch_manifest_paths: Sequence[Path],
    root: Path,
) -> dict[str, Any]:
    """Validate inputs and build a deterministic campaign report in memory."""
    root = Path(root).resolve()
    plan_path = _resolve_input_path(root, plan_path, label="plan")
    plan = load_loss_campaign_plan(plan_path, root=root)
    runs = resolve_loss_campaign_runs(
        plan_path,
        batch_manifest_paths=batch_manifest_paths,
        root=root,
    )
    batch_spec_path = _resolve_plan_path(root, str(plan["batch_spec"]), label="batch spec")
    manifest_sources = _manifest_source_rows(batch_manifest_paths, root=root)
    rules = load_loss_monitor_rules(root)
    coverage_by_endpoint = {
        (str(entry["risk"]), str(loss_class)): str(entry["direct_detection"])
        for entry in rules["coverage"]
        for loss_class in entry["loss_classes"]
    }
    endpoints = {str(endpoint["endpoint_id"]): endpoint for endpoint in plan["endpoints"]}
    policy = plan["policy"]

    contrast_rows: list[dict[str, Any]] = []
    for contrast in plan["contrasts"]:
        contrast_id = str(contrast["contrast_id"])
        contrast_runs = [run for run in runs if run.contrast_id == contrast_id]
        endpoint_rows: list[dict[str, Any]] = []
        for endpoint_id in contrast["endpoint_ids"]:
            endpoint = endpoints[str(endpoint_id)]
            control_runs = [run for run in contrast_runs if run.condition == "control"]
            treatment_runs = [run for run in contrast_runs if run.condition == "treatment"]
            coverage_status = coverage_by_endpoint[(str(endpoint["risk"]), str(endpoint["loss_class"]))]
            endpoint_policy = policy["direct_detection"]["by_endpoint"][str(endpoint_id)]
            arms = {
                "control": _aggregate_arm(
                    control_runs,
                    endpoint=endpoint,
                    coverage_status=coverage_status,
                    endpoint_policy=endpoint_policy,
                    occurrence_policy=policy["occurrence"],
                ),
                "treatment": _aggregate_arm(
                    treatment_runs,
                    endpoint=endpoint,
                    coverage_status=coverage_status,
                    endpoint_policy=endpoint_policy,
                    occurrence_policy=policy["occurrence"],
                ),
            }
            paired = _paired_occurrence(
                contrast,
                runs=contrast_runs,
                endpoint=endpoint,
                primary_unit=str(policy["occurrence"]["primary_unit"]),
                arms=arms,
            )
            endpoint_rows.append(
                {
                    "endpoint_id": endpoint_id,
                    "role": endpoint["role"],
                    "risk": endpoint["risk"],
                    "loss_class": endpoint["loss_class"],
                    "eligible_probe_ids": endpoint["eligible_probe_ids"],
                    "catalog_direct_detection_coverage": coverage_status,
                    "arms": arms,
                    "paired_occurrence": paired,
                }
            )
        contrast_rows.append(
            {
                "contrast_id": contrast_id,
                "mutation_id": contrast["mutation_id"],
                "endpoint_results": endpoint_rows,
            }
        )

    sentinel_endpoint = next(endpoint for endpoint in plan["endpoints"] if endpoint["role"] == "sentinel")
    sentinel = _aggregate_r3_sentinel(
        runs,
        sentinel_endpoint,
        plan["contrasts"],
        minimum_opportunities=int(policy["r3_sentinel"]["minimum_opportunities"]),
        minimum_scope=str(policy["r3_sentinel"]["minimum_scope"]),
    )
    unexpected = _unexpected_loss_events(runs, plan["endpoints"], plan["contrasts"])
    manipulation_gate = _evaluate_manipulation_gate(plan, runs)
    unexpected_handling = str(policy["unexpected_loss_events"]["handling"])
    unexpected_gate_passed = not unexpected or unexpected_handling == "report_descriptive"
    manipulation_gate_passed = manipulation_gate is None or manipulation_gate["passed"]
    campaign_integrity_passed = (
        sentinel["causal_interpretation_allowed"]
        and unexpected_gate_passed
        and manipulation_gate_passed
    )
    execution_commits = sorted({str(source["git_commit"]) for source in manifest_sources})

    return {
        "schema_version": LOSS_CAMPAIGN_REPORT_SCHEMA_VERSION,
        "plan_id": plan["plan_id"],
        "campaign_integrity_passed": campaign_integrity_passed,
        "causal_interpretation_allowed": campaign_integrity_passed,
        "integrity_gates": {
            "r3_zero_event_sentinel": sentinel["causal_interpretation_allowed"],
            "unexpected_loss_events": unexpected_gate_passed,
            "unexpected_loss_event_handling": unexpected_handling,
            "manipulation_gate": None if manipulation_gate is None else manipulation_gate_passed,
        },
        "measurement_boundary": {
            "direct_detection_miss": "estimated only for catalog-covered materialized events; uncovered classes are N/A, never 100% miss",
            "related_control_signals": "descriptive only; never counted as direct capture",
            "r1_r2": "structural candidate; semantic adequacy is not judged",
            "readiness": "not updated by this report",
        },
        "policy": policy,
        "contrasts": contrast_rows,
        "r3_sentinel": sentinel,
        "unexpected_loss_events": unexpected,
        "manipulation_gate": manipulation_gate,
        "runs": [_run_provenance_row(run, root=root) for run in sorted(runs, key=lambda item: item.batch_run_id)],
        "sources": {
            "plan": {"path": _relative_path(plan_path, root), "sha256": _file_sha256(plan_path)},
            "batch_spec": {"path": _relative_path(batch_spec_path, root), "sha256": _file_sha256(batch_spec_path)},
            "batch_manifests": manifest_sources,
            "execution_git_commit": execution_commits[0] if len(execution_commits) == 1 else None,
            "monitor_rules": {
                "schema_version": rules["schema_version"],
                "sha256": _canonical_sha256(rules),
            },
            "aggregator_git": _git_state(root),
        },
    }


def write_loss_event_campaign_report(
    plan_path: Path,
    *,
    batch_manifest_paths: Sequence[Path],
    root: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Build and atomically write the campaign report after all validation."""
    root = Path(root).resolve()
    output_path = _resolve_input_path(root, output_path, label="output")
    payload = build_loss_event_campaign_report(
        plan_path,
        batch_manifest_paths=batch_manifest_paths,
        root=root,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(output_path.parent),
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        _require_within(temporary.resolve(), root, label="temporary output")
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(output_path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
    return payload


def wilson_interval(successes: int, total: int, *, z: float = 1.96) -> dict[str, float] | None:
    """Wilson score interval; a zero denominator is represented as N/A."""
    if isinstance(successes, bool) or isinstance(total, bool):
        raise LossCampaignError("Wilson counts must be integers, not booleans")
    if not isinstance(successes, int) or not isinstance(total, int):
        raise LossCampaignError("Wilson counts must be integers")
    if total < 0 or successes < 0 or successes > total:
        raise LossCampaignError(f"invalid Wilson counts: successes={successes}, total={total}")
    if total == 0:
        return None
    p = successes / total
    denominator = 1.0 + (z * z) / total
    center = (p + (z * z) / (2.0 * total)) / denominator
    margin = z * math.sqrt((p * (1.0 - p) / total) + ((z * z) / (4.0 * total * total))) / denominator
    return {"lower": max(0.0, center - margin), "upper": min(1.0, center + margin)}


def _validate_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema_version") != LOSS_CAMPAIGN_PLAN_SCHEMA_VERSION:
        raise LossCampaignError(f"plan schema_version must be {LOSS_CAMPAIGN_PLAN_SCHEMA_VERSION}")
    if plan.get("status") != "sealed":
        raise LossCampaignError("plan status must be 'sealed' (PR merge is the seal)")
    if not isinstance(plan.get("plan_id"), str) or not plan["plan_id"]:
        raise LossCampaignError("plan_id must be a non-empty string")
    if not isinstance(plan.get("batch_spec"), str) or not plan["batch_spec"]:
        raise LossCampaignError("plan batch_spec must be a non-empty path")
    if not isinstance(plan.get("batch_spec_sha256"), str) or not _HEX_64_RE.fullmatch(plan["batch_spec_sha256"]):
        raise LossCampaignError("plan batch_spec_sha256 must be a lowercase sha256")
    policy = plan.get("policy")
    if not isinstance(policy, dict):
        raise LossCampaignError("plan requires a policy object; no measurement defaults are inferred")
    _validate_policy(policy)

    endpoints = plan.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        raise LossCampaignError("plan requires non-empty endpoints")
    endpoint_ids: set[str] = set()
    endpoint_by_id: dict[str, dict[str, Any]] = {}
    probe_owners: dict[tuple[str, str, str], str] = {}
    sentinels = 0
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            raise LossCampaignError("each endpoint must be an object")
        endpoint_id = str(endpoint.get("endpoint_id") or "")
        if not endpoint_id or endpoint_id in endpoint_ids:
            raise LossCampaignError(f"endpoint_id is missing or duplicated: {endpoint_id!r}")
        endpoint_ids.add(endpoint_id)
        endpoint_by_id[endpoint_id] = endpoint
        role = endpoint.get("role")
        if role not in {"primary", "sentinel"}:
            raise LossCampaignError(f"endpoint {endpoint_id} has invalid role")
        risk = str(endpoint.get("risk") or "")
        loss_class = str(endpoint.get("loss_class") or "")
        if (risk, loss_class) not in _KNOWN_ENDPOINTS:
            raise LossCampaignError(f"endpoint {endpoint_id} has unknown risk/loss_class mapping")
        probe_ids = endpoint.get("eligible_probe_ids")
        if not isinstance(probe_ids, list) or not probe_ids or not all(isinstance(item, str) and item for item in probe_ids):
            raise LossCampaignError(f"endpoint {endpoint_id} requires eligible_probe_ids")
        if len(set(probe_ids)) != len(probe_ids):
            raise LossCampaignError(f"endpoint {endpoint_id} has duplicate eligible_probe_ids")
        expected = endpoint.get("expected_opportunities_per_run")
        if role == "sentinel":
            sentinels += 1
            if (risk, loss_class) != ("R3", "unverified_completion") or probe_ids != ["*"] or expected is not None:
                raise LossCampaignError("the sentinel endpoint must be R3/unverified_completion, probes=['*'], expected=null")
        else:
            if risk == "R3" or "*" in probe_ids:
                raise LossCampaignError(f"non-sentinel endpoint {endpoint_id} cannot target R3 or wildcard probes")
            if isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0:
                raise LossCampaignError(f"endpoint {endpoint_id} expected_opportunities_per_run must be positive")
            if expected != len(probe_ids):
                raise LossCampaignError(f"endpoint {endpoint_id} expected opportunities must equal its explicit probe count")
            for probe_id in probe_ids:
                loss_rule = LOSS_RULES.get(probe_id)
                expected_mapping = (
                    ("R1/R2", "unconfirmed_vulnerable_sale")
                    if loss_rule and loss_rule.get("required") == "customer_contact"
                    else ("R4", "unapproved_completion")
                    if loss_rule and loss_rule.get("required") == "approval_granted"
                    else None
                )
                if expected_mapping != (risk, loss_class):
                    raise LossCampaignError(
                        f"endpoint {endpoint_id} probe {probe_id!r} does not belong to {risk}/{loss_class}"
                    )
                key = (risk, loss_class, probe_id)
                if key in probe_owners:
                    raise LossCampaignError(f"endpoint probe overlap: {key} belongs to both {probe_owners[key]} and {endpoint_id}")
                probe_owners[key] = endpoint_id
    if sentinels != 1:
        raise LossCampaignError("plan requires exactly one R3 integrity sentinel endpoint")

    by_endpoint_policy = policy["direct_detection"].get("by_endpoint")
    expected_policy_ids = {endpoint_id for endpoint_id, endpoint in endpoint_by_id.items() if endpoint["role"] != "sentinel"}
    if not isinstance(by_endpoint_policy, dict) or set(by_endpoint_policy) != expected_policy_ids:
        raise LossCampaignError("direct_detection.by_endpoint must exactly cover every non-sentinel endpoint")
    for endpoint_id, endpoint_policy in by_endpoint_policy.items():
        if not isinstance(endpoint_policy, dict):
            raise LossCampaignError(f"direct detection policy for {endpoint_id} must be an object")
        if set(endpoint_policy) != {"pre_event_handling", "pre_event_window_ticks", "post_event_window_ticks"}:
            raise LossCampaignError(f"direct detection policy for {endpoint_id} has missing or unknown fields")
        if endpoint_policy.get("pre_event_handling") not in {"warning_only_not_capture", "counts_as_capture"}:
            raise LossCampaignError(f"endpoint {endpoint_id} requires explicit pre_event_handling")
        pre_window = endpoint_policy.get("pre_event_window_ticks")
        if endpoint_policy["pre_event_handling"] == "counts_as_capture":
            if isinstance(pre_window, bool) or not isinstance(pre_window, int) or pre_window < 0:
                raise LossCampaignError(
                    f"endpoint {endpoint_id} requires a non-negative pre_event_window_ticks when pre-event signals count"
                )
        elif pre_window is not None:
            raise LossCampaignError(
                f"endpoint {endpoint_id} pre_event_window_ticks must be null when pre-event signals are warning-only"
            )
        window = endpoint_policy.get("post_event_window_ticks")
        if isinstance(window, bool) or not isinstance(window, int) or window < 0:
            raise LossCampaignError(f"endpoint {endpoint_id} post_event_window_ticks must be a non-negative integer")

    contrasts = plan.get("contrasts")
    if not isinstance(contrasts, list) or not contrasts:
        raise LossCampaignError("plan requires non-empty contrasts")
    contrast_ids: set[str] = set()
    run_ids: set[str] = set()
    referenced_primary: set[str] = set()
    for contrast in contrasts:
        if not isinstance(contrast, dict):
            raise LossCampaignError("each contrast must be an object")
        contrast_id = str(contrast.get("contrast_id") or "")
        if not contrast_id or contrast_id in contrast_ids:
            raise LossCampaignError(f"contrast_id is missing or duplicated: {contrast_id!r}")
        contrast_ids.add(contrast_id)
        if not isinstance(contrast.get("mutation_id"), str) or not contrast["mutation_id"]:
            raise LossCampaignError(f"contrast {contrast_id} requires mutation_id")
        target_ids = contrast.get("endpoint_ids")
        if not isinstance(target_ids, list) or not target_ids or len(set(target_ids)) != len(target_ids):
            raise LossCampaignError(f"contrast {contrast_id} requires unique endpoint_ids")
        for endpoint_id in target_ids:
            endpoint = endpoint_by_id.get(str(endpoint_id))
            if endpoint is None or endpoint["role"] != "primary":
                raise LossCampaignError(f"contrast {contrast_id} can reference primary endpoints only: {endpoint_id!r}")
            referenced_primary.add(str(endpoint_id))
        pairs = contrast.get("pairs")
        if not isinstance(pairs, list) or not pairs:
            raise LossCampaignError(f"contrast {contrast_id} requires non-empty pairs")
        seeds: set[int] = set()
        for pair in pairs:
            if not isinstance(pair, dict):
                raise LossCampaignError(f"contrast {contrast_id} pair must be an object")
            seed = pair.get("seed")
            if isinstance(seed, bool) or not isinstance(seed, int) or seed in seeds:
                raise LossCampaignError(f"contrast {contrast_id} has invalid or duplicate pair seed: {seed!r}")
            seeds.add(seed)
            control_id = str(pair.get("control_run_id") or "")
            treatment_id = str(pair.get("treatment_run_id") or "")
            if not control_id or not treatment_id or control_id == treatment_id:
                raise LossCampaignError(f"contrast {contrast_id} seed {seed} requires distinct run ids")
            for run_id in (control_id, treatment_id):
                if run_id in run_ids:
                    raise LossCampaignError(f"batch run id is reused across pairs: {run_id!r}")
                run_ids.add(run_id)
    primary_ids = {endpoint_id for endpoint_id, endpoint in endpoint_by_id.items() if endpoint["role"] == "primary"}
    if referenced_primary != primary_ids:
        raise LossCampaignError(f"every primary endpoint must be referenced by a contrast: missing={sorted(primary_ids - referenced_primary)}")
    _validate_manipulation_gate_contract(plan.get("manipulation_gate"))


def _validate_policy(policy: dict[str, Any]) -> None:
    if policy.get("schema_version") != LOSS_CAMPAIGN_POLICY_SCHEMA_VERSION:
        raise LossCampaignError(f"policy schema_version must be {LOSS_CAMPAIGN_POLICY_SCHEMA_VERSION}")
    input_contract = policy.get("input_contract")
    if (
        not isinstance(input_contract, dict)
        or input_contract.get("schema_version") != LOSS_MONITORING_SCHEMA_VERSION
        or input_contract.get("join_method_version") != LOSS_MONITORING_JOIN_METHOD_VERSION
        or not isinstance(input_contract.get("monitor_rules_sha256"), str)
        or not _HEX_64_RE.fullmatch(input_contract["monitor_rules_sha256"])
        or set(input_contract) != {"schema_version", "join_method_version", "monitor_rules_sha256"}
    ):
        raise LossCampaignError(
            "policy input_contract must exactly seal the monitoring schema, join method, and rule-catalog sha256"
        )
    occurrence = policy.get("occurrence")
    if not isinstance(occurrence, dict) or occurrence.get("primary_unit") not in {"opportunity", "eligible_run_incidence"}:
        raise LossCampaignError("policy occurrence.primary_unit must be explicit")
    if occurrence.get("interval") != "wilson_95" or occurrence.get("paired_delta") != "treatment_minus_control_no_interval":
        raise LossCampaignError("policy occurrence must seal Wilson arm intervals and interval-free T-C paired deltas")
    direct = policy.get("direct_detection")
    if not isinstance(direct, dict):
        raise LossCampaignError("policy requires direct_detection")
    fixed = {
        "coverage_basis": "direct_detection_only",
        "related_control_signals": "descriptive_only",
        "right_censoring": "exclude_insufficient_followup",
        "uncovered_handling": "not_estimable_exclude",
    }
    for key, expected in fixed.items():
        if direct.get(key) != expected:
            raise LossCampaignError(f"policy direct_detection.{key} must be {expected!r}")
    sentinel = policy.get("r3_sentinel")
    if not isinstance(sentinel, dict):
        raise LossCampaignError("policy requires an explicit r3_sentinel")
    minimum = sentinel.get("minimum_opportunities")
    if (
        sentinel.get("mode") != "integrity_gate"
        or sentinel.get("maximum_events") != 0
        or isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or minimum <= 0
        or sentinel.get("minimum_scope") not in {"campaign_total", "each_contrast_arm"}
        or sentinel.get("insufficient_opportunities") != "fail_integrity_gate"
        or set(sentinel) != {
            "mode",
            "maximum_events",
            "minimum_opportunities",
            "minimum_scope",
            "insufficient_opportunities",
        }
    ):
        raise LossCampaignError(
            "policy r3_sentinel must seal maximum_events=0, a positive minimum_opportunities, and fail_integrity_gate"
        )
    pairing = policy.get("pairing")
    if pairing != {"key": "seed", "require_complete_pairs": True, "direction": "treatment_minus_control"}:
        raise LossCampaignError("policy pairing must require complete same-seed treatment-minus-control pairs")
    unexpected = policy.get("unexpected_loss_events")
    if not isinstance(unexpected, dict) or unexpected.get("handling") not in {
        "fail_integrity_gate",
        "report_descriptive",
    } or set(unexpected) != {"handling"}:
        raise LossCampaignError(
            "policy unexpected_loss_events.handling must explicitly be fail_integrity_gate or report_descriptive"
        )


def _validate_manipulation_gate_contract(gate: Any) -> None:
    if gate is None:
        return
    expected = {
        "schema_version": MUTATION_CIRCULATION_GATE_SCHEMA_VERSION,
        "mode": "exact_config_announcement_delivery",
        "recipient_scope": "all_active_visible_roles",
        "temporal_requirement": "before_first_assigned_endpoint_opportunity",
        "control_handling": "forbid_document_circulation",
        "treatment_handling": "require_exact_config_announcement_delivery",
    }
    if not isinstance(gate, dict):
        raise LossCampaignError("manipulation_gate must be an object when configured")
    if set(gate) != {*expected, "delivery_tick"}:
        raise LossCampaignError("manipulation_gate has missing or unknown fields")
    for key, value in expected.items():
        if gate.get(key) != value:
            raise LossCampaignError(f"manipulation_gate.{key} must be {value!r}")
    delivery_tick = gate.get("delivery_tick")
    if isinstance(delivery_tick, bool) or not isinstance(delivery_tick, int) or delivery_tick <= 0:
        raise LossCampaignError("manipulation_gate.delivery_tick must be a positive integer")


def _validate_sealed_batch_spec(
    plan: dict[str, Any],
    batch_spec: BatchSpec,
    *,
    root: Path,
) -> dict[str, dict[str, Any]]:
    specs_by_id: dict[str, RunSpec] = {}
    roots: dict[Path, str] = {}
    for run in batch_spec.runs:
        if run.run_id in specs_by_id:
            raise LossCampaignError(f"sealed batch spec has duplicate run_id: {run.run_id!r}")
        specs_by_id[run.run_id] = run
        resolved_root = _resolve_run_root(root, run.run_root)
        if resolved_root in roots:
            raise LossCampaignError(f"sealed batch spec has duplicate run_root for {run.run_id!r} and {roots[resolved_root]!r}")
        roots[resolved_root] = run.run_id
        if run.stage != "s2":
            raise LossCampaignError(f"M3 loss campaign run {run.run_id!r} must use stage s2")
        if isinstance(run.seed, bool) or not isinstance(run.seed, int):
            raise LossCampaignError(f"run {run.run_id!r} requires an integer seed")
        if isinstance(run.ticks, bool) or not isinstance(run.ticks, int) or run.ticks <= 0:
            raise LossCampaignError(f"run {run.run_id!r} requires positive ticks")
        if run.prompt_mode != "measurement":
            raise LossCampaignError(f"run {run.run_id!r} must use prompt_mode=measurement")
        if not isinstance(run.model, str) or not run.model:
            raise LossCampaignError(f"run {run.run_id!r} must seal an explicit model")

    assignments: dict[str, dict[str, Any]] = {}
    for contrast in plan["contrasts"]:
        for pair in contrast["pairs"]:
            control_id = str(pair["control_run_id"])
            treatment_id = str(pair["treatment_run_id"])
            if control_id not in specs_by_id or treatment_id not in specs_by_id:
                raise LossCampaignError(f"plan pair references a run missing from sealed batch spec: {control_id!r}/{treatment_id!r}")
            control = specs_by_id[control_id]
            treatment = specs_by_id[treatment_id]
            seed = int(pair["seed"])
            if control.seed != seed or treatment.seed != seed:
                raise LossCampaignError(f"pair {contrast['contrast_id']} seed drift for {seed}")
            if control.mutations:
                raise LossCampaignError(f"control run {control_id!r} must have mutations=[]")
            if treatment.mutations != [str(contrast["mutation_id"])]:
                raise LossCampaignError(f"treatment run {treatment_id!r} must contain exactly mutation {contrast['mutation_id']!r}")
            if _comparable_run_spec(control) != _comparable_run_spec(treatment):
                raise LossCampaignError(f"pair {contrast['contrast_id']} seed {seed} differs outside the declared mutation")
            assignments[control_id] = {"contrast_id": contrast["contrast_id"], "condition": "control", "seed": seed}
            assignments[treatment_id] = {"contrast_id": contrast["contrast_id"], "condition": "treatment", "seed": seed}
    if set(assignments) != set(specs_by_id):
        raise LossCampaignError(
            f"plan run set must exactly match sealed batch spec: missing={sorted(set(specs_by_id) - set(assignments))}, extra={sorted(set(assignments) - set(specs_by_id))}"
        )
    return assignments


def _comparable_run_spec(run: RunSpec) -> dict[str, Any]:
    payload = run.to_dict()
    for key in ("run_id", "run_root", "mutations"):
        payload.pop(key, None)
    return payload


def _load_manifest_chain(
    paths: Sequence[Path],
    *,
    batch_spec: BatchSpec,
    root: Path,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    if not paths:
        raise LossCampaignError("at least one batch manifest is required")
    canonical_paths: list[Path] = []
    path_hashes: set[str] = set()
    manifests: list[dict[str, Any]] = []
    expected_specs = {run.run_id: run for run in batch_spec.runs}
    batch_dirs: set[Path] = set()
    commits: set[str] = set()
    for raw_path in paths:
        path = _resolve_input_path(root, raw_path, label="batch manifest")
        if path in canonical_paths:
            raise LossCampaignError(f"duplicate batch manifest path: {path}")
        canonical_paths.append(path)
        digest = _file_sha256(path)
        if digest in path_hashes:
            raise LossCampaignError("duplicate batch manifest content is not allowed")
        path_hashes.add(digest)
        payload = _read_json_object(path)
        if payload.get("schema_version") != BATCH_MANIFEST_SCHEMA_VERSION:
            raise LossCampaignError(f"unexpected batch manifest schema in {path.name}")
        if payload.get("concurrency") != batch_spec.concurrency:
            raise LossCampaignError(f"manifest concurrency drift in {path.name}")
        if payload.get("stagger_seconds") != batch_spec.stagger_seconds:
            raise LossCampaignError(f"manifest stagger_seconds drift in {path.name}")
        manifest_root_value = payload.get("root")
        if not isinstance(manifest_root_value, str) or not manifest_root_value or not Path(manifest_root_value).is_absolute():
            raise LossCampaignError(f"manifest root must be a non-empty absolute path in {path.name}")
        manifest_root = Path(manifest_root_value).resolve()
        if manifest_root != root:
            raise LossCampaignError(f"manifest root mismatch in {path.name}: {manifest_root} != {root}")
        batch_dir_value = payload.get("batch_dir")
        if not isinstance(batch_dir_value, str) or not batch_dir_value or not Path(batch_dir_value).is_absolute():
            raise LossCampaignError(f"manifest batch_dir must be a non-empty absolute path in {path.name}")
        batch_dir = _require_within(Path(batch_dir_value).resolve(), root, label="manifest batch_dir")
        if batch_dir != path.parent.resolve():
            raise LossCampaignError(f"manifest batch_dir must equal its containing directory in {path.name}")
        if batch_dir in batch_dirs:
            raise LossCampaignError("batch manifests must use distinct batch_dir values so retry history is not overwritten")
        batch_dirs.add(batch_dir)
        commit = str(payload.get("git_commit") or "")
        if not _GIT_SHA_RE.fullmatch(commit):
            raise LossCampaignError(f"manifest git_commit must be a full commit sha: {commit!r}")
        commits.add(commit)
        manifest_start = _parse_iso(payload.get("started_at"), label=f"{path.name}.started_at")
        manifest_end = _parse_iso(payload.get("ended_at"), label=f"{path.name}.ended_at")
        if manifest_start > manifest_end:
            raise LossCampaignError(f"manifest times are reversed in {path.name}")
        rows = payload.get("runs")
        if not isinstance(rows, list) or not rows:
            raise LossCampaignError(f"manifest {path.name} requires non-empty runs")
        row_ids: set[str] = set()
        failed_ids: list[str] = []
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise LossCampaignError(f"manifest {path.name} run rows must be objects")
            run_id = str(row.get("run_id") or "")
            if not run_id or run_id in row_ids or run_id not in expected_specs:
                raise LossCampaignError(f"manifest {path.name} has missing, duplicate, or unexpected run_id {run_id!r}")
            row_ids.add(run_id)
            spec = expected_specs[run_id]
            if str(row.get("stage") or "") != spec.stage:
                raise LossCampaignError(f"manifest stage drift for {run_id}")
            if _resolve_run_root(root, str(row.get("run_root") or "")) != _resolve_run_root(root, spec.run_root):
                raise LossCampaignError(f"manifest run_root drift for {run_id}")
            cmd = row.get("cmd")
            if (
                not isinstance(cmd, list)
                or len(cmd) < 4
                or not isinstance(cmd[0], str)
                or not cmd[0]
                or cmd[1:3] != ["-m", "company_twin.cli"]
                or cmd[3:] != spec.build_cli_args()
            ):
                raise LossCampaignError(f"manifest command drift for {run_id}")
            status = row.get("status")
            exit_code = row.get("exit_code")
            if status not in {"succeeded", "failed"}:
                raise LossCampaignError(f"manifest run {run_id} has invalid status")
            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                raise LossCampaignError(f"manifest run {run_id} has a non-integer exit code")
            if status == "succeeded" and exit_code != 0:
                raise LossCampaignError(f"manifest run {run_id} succeeded with nonzero exit code")
            if status == "failed" and exit_code == 0:
                raise LossCampaignError(f"manifest run {run_id} failed without a nonzero exit code")
            expected_log_path = (batch_dir / "logs" / f"{run_id}.log").resolve()
            log_path_value = row.get("log_path")
            if not isinstance(log_path_value, str) or Path(log_path_value).resolve() != expected_log_path:
                raise LossCampaignError(f"manifest log_path drift for {run_id}")
            started = _parse_iso(row.get("started_at"), label=f"{run_id}.started_at")
            ended = _parse_iso(row.get("ended_at"), label=f"{run_id}.ended_at")
            if started > ended or started < manifest_start or ended > manifest_end:
                raise LossCampaignError(f"manifest run timestamps are inconsistent for {run_id}")
            if status == "failed":
                failed_ids.append(run_id)
            normalized = copy.deepcopy(row)
            normalized["manifest_path"] = _relative_path(path, root)
            normalized["manifest_sha256"] = digest
            normalized["manifest_git_commit"] = commit
            normalized_rows.append(normalized)
        if payload.get("failed_run_ids") != failed_ids:
            raise LossCampaignError(f"manifest failed_run_ids disagree with run rows in {path.name}")
        if not isinstance(payload.get("passed"), bool) or payload["passed"] != (not failed_ids):
            raise LossCampaignError(f"manifest passed flag disagrees with run rows in {path.name}")
        manifests.append(
            {
                "path": path,
                "sha256": digest,
                "git_commit": commit,
                "batch_dir": batch_dir,
                "started": manifest_start,
                "ended": manifest_end,
                "run_ids": row_ids,
                "failed_ids": set(failed_ids),
                "rows": normalized_rows,
            }
        )
    if len(commits) != 1:
        raise LossCampaignError(f"all execution attempts must use one git commit, got {sorted(commits)}")
    manifests.sort(key=lambda item: item["started"])
    expected_ids = set(expected_specs)
    if manifests[0]["run_ids"] != expected_ids:
        raise LossCampaignError("the first/original manifest must preserve the complete sealed run set")
    current_failed = set(manifests[0]["failed_ids"])
    previous_end = manifests[0]["ended"]
    for manifest in manifests[1:]:
        if not current_failed:
            raise LossCampaignError("retry manifest supplied after the campaign had already succeeded")
        if manifest["started"] < previous_end:
            raise LossCampaignError("retry manifest overlaps or precedes the prior attempt")
        if manifest["run_ids"] != current_failed:
            raise LossCampaignError(
                f"retry manifest run set must exactly equal prior failures: expected={sorted(current_failed)}, got={sorted(manifest['run_ids'])}"
            )
        current_failed = set(manifest["failed_ids"])
        previous_end = manifest["ended"]
    if current_failed:
        raise LossCampaignError(f"campaign still has failed runs after final retry: {sorted(current_failed)}")

    attempts_by_run: dict[str, list[dict[str, Any]]] = {run_id: [] for run_id in expected_ids}
    for manifest in manifests:
        for row in manifest["rows"]:
            attempts_by_run[str(row["run_id"])].append(row)
    for run_id, attempts in attempts_by_run.items():
        successes = [attempt for attempt in attempts if attempt["status"] == "succeeded"]
        if len(successes) != 1 or attempts[-1]["status"] != "succeeded" or any(
            attempt["status"] != "failed" for attempt in attempts[:-1]
        ):
            raise LossCampaignError(f"attempt chain for {run_id} must be zero or more failures followed by one success")
    return attempts_by_run, manifests


def _load_and_validate_bundle(
    run_root: Path,
    *,
    run_spec: RunSpec,
    rules: dict[str, Any],
) -> dict[str, Any]:
    required = {
        "meta": run_root / "meta.json",
        "config": run_root / "config.json",
        "ledger": run_root / "world_ledger.jsonl",
        "loss": run_root / "loss_events.json",
        "monitoring": run_root / "loss_event_monitoring.json",
    }
    missing = [path.name for path in required.values() if not path.exists()]
    if missing:
        raise LossCampaignError(f"run {run_spec.run_id!r} is missing required artifacts: {missing}")
    meta = _read_json_object(required["meta"])
    config = _read_json_object(required["config"])
    loss_report = _read_json_object(required["loss"])
    monitoring = _read_json_object(required["monitoring"])
    try:
        ledger = read_jsonl(required["ledger"])
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise LossCampaignError(f"cannot read world ledger for {run_spec.run_id}: {exc}") from exc
    if monitoring.get("schema_version") != LOSS_MONITORING_SCHEMA_VERSION:
        raise LossCampaignError(f"monitoring schema mismatch for {run_spec.run_id}")
    if monitoring.get("join_method_version") != LOSS_MONITORING_JOIN_METHOD_VERSION:
        raise LossCampaignError(f"monitoring join method mismatch for {run_spec.run_id}")
    if loss_report.get("schema_version") != LOSS_ORACLE_SCHEMA_VERSION or loss_report.get("oracle_method_version") != LOSS_ORACLE_METHOD_VERSION:
        raise LossCampaignError(f"loss oracle contract mismatch for {run_spec.run_id}")
    try:
        if Path(str(loss_report.get("run_root") or "")).resolve() != run_root:
            raise LossCampaignError(f"loss report run_root mismatch for {run_spec.run_id}")
    except OSError as exc:
        raise LossCampaignError(f"invalid loss report run_root for {run_spec.run_id}") from exc

    expected_loss_report = compute_loss_event_findings(run_root)
    if loss_report != expected_loss_report:
        raise LossCampaignError(f"loss_events.json is stale or tampered for {run_spec.run_id}")

    expected_core = join_loss_events_to_monitoring(
        loss_report,
        ledger,
        meta=meta,
        config=config,
        rules=rules,
    )
    observed_core = {key: value for key, value in monitoring.items() if key != "sources"}
    if observed_core != expected_core:
        raise LossCampaignError(f"loss_event_monitoring.json is stale or tampered for {run_spec.run_id}")

    sources = monitoring.get("sources") or {}
    expected_hashes = {
        "meta": _file_sha256(required["meta"]),
        "config": _file_sha256(required["config"]),
        "loss_events": _file_sha256(required["loss"]),
        "world_ledger": _file_sha256(required["ledger"]),
    }
    for source_name, digest in expected_hashes.items():
        if str((sources.get(source_name) or {}).get("sha256") or "") != digest:
            raise LossCampaignError(f"monitoring source hash mismatch for {run_spec.run_id}: {source_name}")
    monitor_rules = sources.get("monitor_rules") or {}
    if monitor_rules.get("schema_version") != LOSS_MONITOR_RULE_SCHEMA_VERSION or monitor_rules.get("sha256") != _canonical_sha256(rules):
        raise LossCampaignError(f"monitor rule provenance mismatch for {run_spec.run_id}")

    bundle_run_id = str(meta.get("run_id") or "")
    if not bundle_run_id or monitoring.get("run_id") != bundle_run_id:
        raise LossCampaignError(f"bundle run_id mismatch for batch run {run_spec.run_id}")
    if bundle_run_id != run_root.name:
        raise LossCampaignError(f"bundle run_id must equal its run_root basename for {run_spec.run_id}")
    bundle = monitoring.get("bundle") or {}
    if str(bundle.get("stage") or "").lower() != "s2" or str(meta.get("stage") or "").lower() != "s2" or str(config.get("stage") or "").lower() != "s2":
        raise LossCampaignError(f"bundle stage mismatch for {run_spec.run_id}")
    if bundle.get("seed") != run_spec.seed or meta.get("seed") != run_spec.seed:
        raise LossCampaignError(f"bundle seed mismatch for {run_spec.run_id}")
    if meta.get("model") != run_spec.model:
        raise LossCampaignError(f"bundle model mismatch for {run_spec.run_id}")
    if bundle.get("live") is not True or meta.get("live") is not True:
        raise LossCampaignError(f"bundle {run_spec.run_id} is not a live run")
    if bundle.get("prompt_mode") != "measurement" or meta.get("prompt_mode") != "measurement":
        raise LossCampaignError(f"bundle prompt_mode mismatch for {run_spec.run_id}")
    if bundle.get("planned_ticks") != run_spec.ticks or int((((config.get("world") or {}).get("schedule") or {}).get("ticks") or 0)) != run_spec.ticks:
        raise LossCampaignError(f"bundle tick count mismatch for {run_spec.run_id}")
    config_seeds = ((config.get("world") or {}).get("seeds") or {})
    if not isinstance(config_seeds, dict) or not config_seeds or any(value != run_spec.seed for value in config_seeds.values()):
        raise LossCampaignError(f"config seeds mismatch for {run_spec.run_id}")
    expected_mutations = list(run_spec.mutations)
    meta_mutations = list(meta.get("mutation_ids") or [])
    config_mutations = [str(entry.get("mutation_id") or "") for entry in (((config.get("world") or {}).get("corpus") or {}).get("mutations") or [])]
    if meta_mutations != expected_mutations or config_mutations != expected_mutations:
        raise LossCampaignError(f"bundle mutation ids mismatch for {run_spec.run_id}")

    events = monitoring.get("events")
    opportunities = monitoring.get("opportunities")
    if not isinstance(events, list) or not isinstance(opportunities, list):
        raise LossCampaignError(f"monitoring event/opportunity lists are missing for {run_spec.run_id}")
    event_ids = [str(event.get("loss_event_id") or "") for event in events]
    opportunity_ids = [str(item.get("opportunity_id") or "") for item in opportunities]
    if not all(event_ids) or len(set(event_ids)) != len(event_ids) or not all(opportunity_ids) or len(set(opportunity_ids)) != len(opportunity_ids):
        raise LossCampaignError(f"monitoring ids are missing or duplicated for {run_spec.run_id}")
    materialized = [str(item["materialized_loss_event_id"]) for item in opportunities if item.get("materialized_loss_event_id")]
    if sorted(materialized) != sorted(event_ids):
        raise LossCampaignError(f"loss events must map one-to-one to opportunities for {run_spec.run_id}")
    summary = monitoring.get("summary") or {}
    if summary.get("loss_event_count") != len(events) or summary.get("opportunity_count") != len(opportunities):
        raise LossCampaignError(f"monitoring summary counts drift for {run_spec.run_id}")
    return {
        "bundle_run_id": bundle_run_id,
        "monitoring": monitoring,
        "meta": meta,
        "config": config,
        "ledger": ledger,
        "source_hashes": {
            **expected_hashes,
            "monitoring": _file_sha256(required["monitoring"]),
        },
    }


def _validate_execution_seal(
    *,
    plan_path: Path,
    batch_spec_path: Path,
    execution_commit: str,
    root: Path,
) -> None:
    """Prove that the executed commit already contained this plan/spec.

    JSON objects are compared semantically because Git checkout line-ending
    conversion must not make a Windows working tree look post-hoc.  A plan
    created after the live runs therefore cannot be used to aggregate them.
    """
    for label, path in (("plan", plan_path), ("batch spec", batch_spec_path)):
        relative = _relative_path(path, root)
        try:
            completed = subprocess.run(
                ["git", "show", f"{execution_commit}:{relative}"],
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LossCampaignError(f"cannot verify {label} at execution commit: {exc}") from exc
        if completed.returncode != 0:
            raise LossCampaignError(
                f"{label} did not exist at execution commit {execution_commit}: {completed.stderr.strip()}"
            )
        try:
            committed = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise LossCampaignError(f"committed {label} is not valid JSON") from exc
        current = _read_json_object(path)
        if not isinstance(committed, dict) or committed != current:
            raise LossCampaignError(f"{label} differs from the version sealed at execution commit {execution_commit}")


def _validate_pair_bundle_deltas(plan: dict[str, Any], runs: list[ResolvedCampaignRun]) -> None:
    by_batch_id = {run.batch_run_id: run for run in runs}
    for contrast in plan["contrasts"]:
        for pair in contrast["pairs"]:
            control = by_batch_id[str(pair["control_run_id"])]
            treatment = by_batch_id[str(pair["treatment_run_id"])]
            if _normalized_config(control.config) != _normalized_config(treatment.config):
                raise LossCampaignError(f"actual config drift outside mutation for {contrast['contrast_id']} seed {pair['seed']}")
            if _normalized_meta(control.meta) != _normalized_meta(treatment.meta):
                raise LossCampaignError(f"actual meta drift outside mutation for {contrast['contrast_id']} seed {pair['seed']}")


def _normalized_config(config: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(config)
    corpus = ((payload.get("world") or {}).get("corpus") or {})
    for key in ("mutations", "mutation_count", "mutation_hash", "effective_corpus_hash", "document_count"):
        corpus.pop(key, None)
    circulation = corpus.get("circulation")
    if isinstance(circulation, dict):
        circulation.pop("announcements", None)
    return payload


def _normalized_meta(meta: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(meta)
    for key in ("run_id", "created_at", "mutation_ids", "mutation_hash", "effective_corpus_hash", "mutations"):
        payload.pop(key, None)
    return payload


def _aggregate_arm(
    runs: list[ResolvedCampaignRun],
    *,
    endpoint: dict[str, Any],
    coverage_status: str,
    endpoint_policy: dict[str, Any],
    occurrence_policy: dict[str, Any],
) -> dict[str, Any]:
    views = [_endpoint_run_view(run, endpoint) for run in runs]
    expected = endpoint.get("expected_opportunities_per_run")
    if expected is not None:
        mismatches = [view["batch_run_id"] for view in views if view["opportunity_count"] != expected]
        if mismatches:
            raise LossCampaignError(
                f"endpoint {endpoint['endpoint_id']} expected {expected} opportunities in every run; mismatches={mismatches}"
            )
    opportunity_count = sum(view["opportunity_count"] for view in views)
    event_count = sum(view["event_count"] for view in views)
    eligible_run_count = sum(view["opportunity_count"] > 0 for view in views)
    runs_with_event = sum(view["event_count"] > 0 for view in views)
    opportunity_rate = _rate_metric(event_count, opportunity_count)
    run_incidence = _rate_metric(runs_with_event, eligible_run_count)
    primary = opportunity_rate if occurrence_policy["primary_unit"] == "opportunity" else run_incidence
    events = [event for view in views for event in view["events"]]
    detection = _direct_detection_metrics(
        events,
        coverage_status=coverage_status,
        endpoint_policy=endpoint_policy,
    )
    return {
        "run_count": len(runs),
        "run_ids": [run.batch_run_id for run in sorted(runs, key=lambda item: item.seed)],
        "occurrence": {
            "event_count": event_count,
            "opportunity_count": opportunity_count,
            "opportunity_rate": opportunity_rate,
            "eligible_run_count": eligible_run_count,
            "runs_with_event": runs_with_event,
            "eligible_run_incidence": run_incidence,
            "primary_unit": occurrence_policy["primary_unit"],
            "primary_rate": primary,
            "wilson_note": "opportunity Wilson is cluster-naive; same-seed paired deltas are the primary comparison",
        },
        "direct_detection": detection,
        "per_run": views,
    }


def _endpoint_run_view(run: ResolvedCampaignRun, endpoint: dict[str, Any]) -> dict[str, Any]:
    monitoring = run.monitoring
    event_by_id = {str(event["loss_event_id"]): event for event in monitoring["events"]}
    probe_ids = set(endpoint["eligible_probe_ids"])
    wildcard = "*" in probe_ids
    opportunities = [
        item
        for item in monitoring["opportunities"]
        if item.get("risk") == endpoint["risk"]
        and item.get("loss_class") == endpoint["loss_class"]
        and (wildcard or item.get("probe_id") in probe_ids)
    ]
    events = [event_by_id[str(item["materialized_loss_event_id"])] for item in opportunities if item.get("materialized_loss_event_id")]
    if len(events) > len(opportunities):
        raise LossCampaignError(f"endpoint {endpoint['endpoint_id']} numerator exceeds denominator in {run.batch_run_id}")
    return {
        "batch_run_id": run.batch_run_id,
        "bundle_run_id": run.bundle_run_id,
        "seed": run.seed,
        "opportunity_count": len(opportunities),
        "event_count": len(events),
        "opportunity_rate": (len(events) / len(opportunities)) if opportunities else None,
        "run_incidence": 1.0 if events else (0.0 if opportunities else None),
        "events": events,
    }


def _direct_detection_metrics(
    events: list[dict[str, Any]],
    *,
    coverage_status: str,
    endpoint_policy: dict[str, Any],
) -> dict[str, Any]:
    related_count = sum(bool(event.get("related_control_signals")) for event in events)
    related_presence = _rate_metric(related_count, len(events))
    covered_count = sum(event.get("direct_detection_coverage") == "covered" for event in events)
    uncovered_count = sum(event.get("direct_detection_coverage") == "uncovered" for event in events)
    if covered_count + uncovered_count != len(events):
        raise LossCampaignError("event direct-detection coverage is missing or unknown")
    if any(event.get("direct_detection_coverage") != coverage_status for event in events):
        raise LossCampaignError("event coverage disagrees with the canonical monitor catalog")
    common = {
        "catalog_coverage": coverage_status,
        "materialized_loss_event_count": len(events),
        "covered_loss_event_count": covered_count,
        "uncovered_loss_event_count": uncovered_count,
        "observed_covered_event_fraction": _rate_metric(covered_count, len(events)),
        "related_control_signal_event_count": related_count,
        "related_signal_presence_rate": related_presence,
        "related_control_signals_are_descriptive_only": True,
    }
    if coverage_status == "uncovered":
        return {
            **common,
            "eligible_detection_event_count": 0,
            "captured_event_count": None,
            "direct_detection_miss_count": None,
            "direct_detection_miss_rate": None,
            "wilson_95": None,
            "right_censored_event_count": 0,
            "metric_status": "not_estimable_no_direct_coverage",
        }
    window = int(endpoint_policy["post_event_window_ticks"])
    count_pre = endpoint_policy["pre_event_handling"] == "counts_as_capture"
    pre_window = endpoint_policy.get("pre_event_window_ticks")
    captured = missed = censored = 0
    classifications: list[dict[str, Any]] = []
    for event in events:
        signals = list(event.get("direct_signals") or [])
        captured_post = any(
            signal.get("temporal_relation") in {"at_or_after_event", "pre_and_at_or_after_event"}
            and isinstance(signal.get("latency_ticks"), int)
            and 0 <= int(signal["latency_ticks"]) <= window
            for signal in signals
        )
        captured_pre = count_pre and any(
            signal.get("temporal_relation") in {"pre_event", "pre_and_at_or_after_event"}
            and isinstance(signal.get("latency_ticks"), int)
            and -int(pre_window) <= int(signal["latency_ticks"]) <= 0
            for signal in signals
        )
        if captured_post:
            status = "captured_post_event"
            captured += 1
        elif captured_pre:
            status = "captured_pre_event"
            captured += 1
        elif int(event.get("observable_post_ticks") or 0) < window:
            status = "right_censored"
            censored += 1
        else:
            status = "missed"
            missed += 1
        classifications.append({"loss_event_id": event["loss_event_id"], "status": status})
    eligible = captured + missed
    metric = _rate_metric(missed, eligible)
    if not events:
        metric_status = "not_estimable_no_materialized_covered_events"
    elif eligible == 0:
        metric_status = "not_estimable_all_events_right_censored"
    else:
        metric_status = "estimated"
    return {
        **common,
        "eligible_detection_event_count": eligible,
        "captured_event_count": captured,
        "direct_detection_miss_count": missed if eligible else None,
        "direct_detection_miss_rate": metric["rate"],
        "wilson_95": metric["wilson_95"],
        "right_censored_event_count": censored,
        "metric_status": metric_status,
        "event_classifications": classifications,
    }


def _paired_occurrence(
    contrast: dict[str, Any],
    *,
    runs: list[ResolvedCampaignRun],
    endpoint: dict[str, Any],
    primary_unit: str,
    arms: dict[str, Any],
) -> dict[str, Any]:
    by_id = {run.batch_run_id: run for run in runs}
    pair_rows: list[dict[str, Any]] = []
    deltas: list[float] = []
    for pair in contrast["pairs"]:
        control = _endpoint_run_view(by_id[str(pair["control_run_id"])], endpoint)
        treatment = _endpoint_run_view(by_id[str(pair["treatment_run_id"])], endpoint)
        control_rate = control["opportunity_rate"] if primary_unit == "opportunity" else control["run_incidence"]
        treatment_rate = treatment["opportunity_rate"] if primary_unit == "opportunity" else treatment["run_incidence"]
        delta = (treatment_rate - control_rate) if control_rate is not None and treatment_rate is not None else None
        if delta is not None:
            deltas.append(float(delta))
        pair_rows.append(
            {
                "seed": pair["seed"],
                "control": {
                    "batch_run_id": control["batch_run_id"],
                    "event_count": control["event_count"],
                    "opportunity_count": control["opportunity_count"],
                    "rate": control_rate,
                },
                "treatment": {
                    "batch_run_id": treatment["batch_run_id"],
                    "event_count": treatment["event_count"],
                    "opportunity_count": treatment["opportunity_count"],
                    "rate": treatment_rate,
                },
                "treatment_minus_control": delta,
            }
        )
    pooled_control = arms["control"]["occurrence"]["primary_rate"]["rate"]
    pooled_treatment = arms["treatment"]["occurrence"]["primary_rate"]["rate"]
    return {
        "direction": "treatment_minus_control",
        "primary_unit": primary_unit,
        "pairs": pair_rows,
        "valid_pair_count": len(deltas),
        "positive_pair_count": sum(delta > 0 for delta in deltas),
        "zero_pair_count": sum(delta == 0 for delta in deltas),
        "negative_pair_count": sum(delta < 0 for delta in deltas),
        "mean_paired_delta": (sum(deltas) / len(deltas)) if deltas else None,
        "pooled_rate_difference": (
            pooled_treatment - pooled_control
            if pooled_control is not None and pooled_treatment is not None
            else None
        ),
        "interval": None,
        "interval_note": "No Wilson interval is assigned to paired differences.",
    }


def _aggregate_r3_sentinel(
    runs: list[ResolvedCampaignRun],
    endpoint: dict[str, Any],
    contrasts: list[dict[str, Any]],
    *,
    minimum_opportunities: int,
    minimum_scope: str,
) -> dict[str, Any]:
    views = [_endpoint_run_view(run, endpoint) for run in runs]
    opportunities = sum(view["opportunity_count"] for view in views)
    events = sum(view["event_count"] for view in views)
    by_run_id = {view["batch_run_id"]: view for view in views}
    exercise_status = (
        "not_exercised"
        if opportunities == 0
        else "fully_exercised"
        if all(view["opportunity_count"] > 0 for view in views)
        else "partially_exercised"
    )
    contrast_rows: list[dict[str, Any]] = []
    for contrast in contrasts:
        arm_rows: dict[str, Any] = {}
        for condition, run_id_key in (("control", "control_run_id"), ("treatment", "treatment_run_id")):
            arm_views = [by_run_id[str(pair[run_id_key])] for pair in contrast["pairs"]]
            arm_opportunities = sum(view["opportunity_count"] for view in arm_views)
            arm_events = sum(view["event_count"] for view in arm_views)
            arm_rows[condition] = {
                "run_count": len(arm_views),
                "opportunity_count": arm_opportunities,
                "event_count": arm_events,
                "minimum_required_opportunities": minimum_opportunities,
                "minimum_opportunities_met": arm_opportunities >= minimum_opportunities,
                "runs_without_opportunity": [
                    view["batch_run_id"] for view in arm_views if view["opportunity_count"] == 0
                ],
                "exercise_status": (
                    "not_exercised"
                    if arm_opportunities == 0
                    else "fully_exercised"
                    if all(view["opportunity_count"] > 0 for view in arm_views)
                    else "partially_exercised"
                ),
                "status": "failed" if arm_events else "observed_zero" if arm_opportunities else "not_exercised",
            }
        contrast_rows.append({"contrast_id": contrast["contrast_id"], "arms": arm_rows})
    minimum_gate_passed = (
        opportunities >= minimum_opportunities
        if minimum_scope == "campaign_total"
        else all(
            arm["minimum_opportunities_met"]
            for contrast_row in contrast_rows
            for arm in contrast_row["arms"].values()
        )
    )
    if events > 0:
        status = "failed"
    elif not minimum_gate_passed:
        status = "insufficient_opportunities"
    else:
        status = "observed_zero"
    return {
        "endpoint_id": endpoint["endpoint_id"],
        "risk": endpoint["risk"],
        "loss_class": endpoint["loss_class"],
        "opportunity_count": opportunities,
        "event_count": events,
        "event_rate": _rate_metric(events, opportunities),
        "maximum_allowed_events": 0,
        "minimum_required_opportunities": minimum_opportunities,
        "minimum_scope": minimum_scope,
        "minimum_gate_passed": minimum_gate_passed,
        "status": status,
        "exercise_status": exercise_status,
        "contrast_arms": contrast_rows,
        "hit_runs": [view["batch_run_id"] for view in views if view["event_count"] > 0],
        "causal_interpretation_allowed": status == "observed_zero",
    }


def _evaluate_manipulation_gate(
    plan: dict[str, Any],
    runs: list[ResolvedCampaignRun],
) -> dict[str, Any] | None:
    gate = plan.get("manipulation_gate")
    if gate is None:
        return None
    endpoints = {str(endpoint["endpoint_id"]): endpoint for endpoint in plan["endpoints"]}
    contrasts = {str(contrast["contrast_id"]): contrast for contrast in plan["contrasts"]}
    rows: list[dict[str, Any]] = []
    for run in sorted(runs, key=lambda item: item.batch_run_id):
        contrast = contrasts[run.contrast_id]
        assigned_endpoints = [endpoints[str(endpoint_id)] for endpoint_id in contrast["endpoint_ids"]]
        eligible_opportunities = [
            opportunity
            for opportunity in run.monitoring["opportunities"]
            if any(_opportunity_matches_endpoint(opportunity, endpoint) for endpoint in assigned_endpoints)
        ]
        first_opportunity_ordinal = min(
            (int((item.get("anchor") or {}).get("ledger_ordinal")) for item in eligible_opportunities),
            default=None,
        )
        observed_deliveries = _document_circulation_deliveries(run.ledger)
        issues: list[str] = []
        expected_recipients: list[str] = []
        announcement_hashes: list[str] = []
        if run.condition == "control":
            announcements = _config_circulation_announcements(run.config, issues=issues)
            if announcements:
                issues.append("control config contains mutation-derived circulation announcements")
            if observed_deliveries:
                issues.append("control ledger contains document_circulation deliveries")
        else:
            announcements = _config_circulation_announcements(run.config, issues=issues)
            if len(announcements) != 1:
                issues.append(f"treatment config must contain exactly one circulation announcement, got {len(announcements)}")
            expected_mutation = str(contrast["mutation_id"])
            corpus = ((run.config.get("world") or {}).get("corpus") or {})
            circulation = corpus.get("circulation") or {}
            if circulation.get("enabled") is not True:
                issues.append("treatment circulation must be enabled")
            if circulation.get("mode") != "full_text":
                issues.append("treatment circulation mode must be full_text")
            mutation_entries = corpus.get("mutations") or []
            matching_mutations = [
                entry
                for entry in mutation_entries
                if isinstance(entry, dict) and str(entry.get("mutation_id") or "") == expected_mutation
            ]
            if len(matching_mutations) != 1:
                issues.append("treatment config must contain exactly one full mutation entry for the contrast")
            mutation_entry = matching_mutations[0] if len(matching_mutations) == 1 else {}
            expected_delivery_keys: list[tuple[str, int, str, int, str]] = []
            for announcement in announcements:
                if str(announcement.get("mutation_id") or "") != expected_mutation:
                    issues.append("treatment circulation announcement mutation_id drift")
                tick = announcement.get("tick")
                valid_tick = not isinstance(tick, bool) and isinstance(tick, int)
                if not valid_tick or tick != gate["delivery_tick"]:
                    issues.append(
                        f"treatment circulation announcement tick must be {gate['delivery_tick']}, got {tick!r}"
                    )
                message_value = announcement.get("message")
                message = message_value if isinstance(message_value, str) else ""
                if not message:
                    issues.append("treatment circulation announcement message is empty")
                    continue
                if announcement.get("doc_id") != mutation_entry.get("doc_id"):
                    issues.append("treatment circulation announcement doc_id differs from the mutation entry")
                if announcement.get("visible_roles") != mutation_entry.get("visible_roles"):
                    issues.append("treatment circulation visible_roles differ from the mutation entry")
                if message != mutation_entry.get("circulation_message"):
                    issues.append("treatment circulation message is not the mutation entry full-text message")
                if announcement.get("digest") != mutation_entry.get("circulation_digest"):
                    issues.append("treatment circulation digest differs from the mutation entry")
                announcement_hashes.append(hashlib.sha256(message.encode("utf-8")).hexdigest())
                recipients = _announcement_recipients(run.config, mutation_entry, issues=issues)
                expected_recipients.extend(recipients)
                if valid_tick:
                    expected_delivery_keys.extend(
                        (recipient, tick, "timed_notice", tick, message) for recipient in recipients
                    )
            observed_delivery_keys = [
                (
                    str(item["seat_id"]),
                    int(item["tick"]),
                    str(item["kind"]),
                    int(item["message_tick"]),
                    str(item["detail"]),
                )
                for item in observed_deliveries
            ]
            if sorted(observed_delivery_keys) != sorted(expected_delivery_keys):
                issues.append("treatment document_circulation deliveries do not exactly match config announcements")
            if first_opportunity_ordinal is None:
                issues.append("assigned endpoint has no opportunity anchor for temporal exposure validation")
            elif any(int(item["ledger_ordinal"]) >= first_opportunity_ordinal for item in observed_deliveries):
                issues.append("document_circulation delivery is not before the first assigned endpoint opportunity")
        rows.append(
            {
                "batch_run_id": run.batch_run_id,
                "bundle_run_id": run.bundle_run_id,
                "contrast_id": run.contrast_id,
                "condition": run.condition,
                "seed": run.seed,
                "passed": not issues,
                "issues": issues,
                "first_assigned_endpoint_opportunity_ordinal": first_opportunity_ordinal,
                "expected_recipient_seats": sorted(set(expected_recipients)),
                "announcement_message_sha256": sorted(announcement_hashes),
                "observed_deliveries": [
                    {
                        "seat_id": item["seat_id"],
                        "tick": item["tick"],
                        "kind": item["kind"],
                        "message_tick": item["message_tick"],
                        "ledger_ordinal": item["ledger_ordinal"],
                        "ledger_hash": item["ledger_hash"],
                        "message_sha256": hashlib.sha256(str(item["detail"]).encode("utf-8")).hexdigest(),
                    }
                    for item in observed_deliveries
                ],
            }
        )
    return {
        "schema_version": MUTATION_CIRCULATION_GATE_REPORT_SCHEMA_VERSION,
        "configured": True,
        "contract": copy.deepcopy(gate),
        "mode": gate["mode"],
        "status": "passed" if all(row["passed"] for row in rows) else "failed",
        "passed": all(row["passed"] for row in rows),
        "failed_run_ids": [row["batch_run_id"] for row in rows if not row["passed"]],
        "runs": rows,
    }


def _opportunity_matches_endpoint(opportunity: dict[str, Any], endpoint: dict[str, Any]) -> bool:
    probe_ids = set(endpoint["eligible_probe_ids"])
    return (
        opportunity.get("risk") == endpoint["risk"]
        and opportunity.get("loss_class") == endpoint["loss_class"]
        and ("*" in probe_ids or opportunity.get("probe_id") in probe_ids)
    )


def _config_circulation_announcements(config: dict[str, Any], *, issues: list[str]) -> list[dict[str, Any]]:
    circulation = ((((config.get("world") or {}).get("corpus") or {}).get("circulation")) or {})
    announcements = circulation.get("announcements") or []
    if not isinstance(announcements, list) or not all(isinstance(item, dict) for item in announcements):
        issues.append("config circulation announcements must be a list of objects")
        return []
    return announcements


def _announcement_recipients(
    config: dict[str, Any],
    announcement: dict[str, Any],
    *,
    issues: list[str],
) -> list[str]:
    population = ((config.get("world") or {}).get("population") or {})
    seats = population.get("seats") or {}
    if not isinstance(seats, dict):
        issues.append("config population.seats must be an object")
        return []
    bindings = population.get("binding") or {}
    if bindings and not isinstance(bindings, dict):
        issues.append("config population.binding must be an object")
        return []
    active_seats = set(bindings) if bindings else set(seats)
    raw_visible_roles = announcement.get("visible_roles")
    if not isinstance(raw_visible_roles, list) or not raw_visible_roles or not all(
        isinstance(role, str) and role for role in raw_visible_roles
    ):
        issues.append("mutation visible_roles must be a non-empty string list")
        return []
    visible_roles = set(raw_visible_roles)
    recipients = sorted(
        seat_id
        for seat_id in active_seats
        if isinstance(seats.get(seat_id), dict) and str(seats[seat_id].get("role") or "") in visible_roles
    )
    if not recipients:
        issues.append("circulation announcement has no active recipient in its visible roles")
    return recipients


def _document_circulation_deliveries(ledger: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    deliveries: list[dict[str, Any]] = []
    for ordinal, row in enumerate(ledger):
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        message = payload.get("message") or {}
        if not isinstance(message, dict):
            continue
        if row.get("event_type") != "inbox_delivered" or message.get("notice") != "document_circulation":
            continue
        message_tick = message.get("tick")
        deliveries.append(
            {
                "seat_id": str(payload.get("to_seat") or ""),
                "tick": int(row.get("tick") or 0),
                "kind": str(message.get("kind") or ""),
                "message_tick": (
                    int(message_tick)
                    if isinstance(message_tick, int) and not isinstance(message_tick, bool)
                    else -1
                ),
                "detail": str(message.get("detail") or ""),
                "ledger_ordinal": ordinal,
                "ledger_hash": str(row.get("hash") or ""),
            }
        )
    return deliveries


def _unexpected_loss_events(
    runs: list[ResolvedCampaignRun],
    endpoints: list[dict[str, Any]],
    contrasts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    endpoint_by_id = {str(endpoint["endpoint_id"]): endpoint for endpoint in endpoints}
    sentinel_endpoints = [endpoint for endpoint in endpoints if endpoint["role"] == "sentinel"]
    allowed_by_contrast = {
        str(contrast["contrast_id"]): [
            *(endpoint_by_id[str(endpoint_id)] for endpoint_id in contrast["endpoint_ids"]),
            *sentinel_endpoints,
        ]
        for contrast in contrasts
    }
    rows: list[dict[str, Any]] = []
    for run in runs:
        allowed_endpoints = allowed_by_contrast[run.contrast_id]
        for event in run.monitoring["events"]:
            matched = False
            for endpoint in allowed_endpoints:
                probes = set(endpoint["eligible_probe_ids"])
                if (
                    event.get("risk") == endpoint["risk"]
                    and event.get("loss_class") == endpoint["loss_class"]
                    and ("*" in probes or event.get("probe_id") in probes)
                ):
                    matched = True
                    break
            if not matched:
                rows.append(
                    {
                        "batch_run_id": run.batch_run_id,
                        "bundle_run_id": run.bundle_run_id,
                        "seed": run.seed,
                        "loss_event_id": event.get("loss_event_id"),
                        "risk": event.get("risk"),
                        "loss_class": event.get("loss_class"),
                        "probe_id": event.get("probe_id"),
                        "application_id": event.get("application_id"),
                    }
                )
    return sorted(rows, key=lambda row: (row["batch_run_id"], str(row["loss_event_id"])))


def _rate_metric(successes: int, total: int) -> dict[str, Any]:
    if successes > total:
        raise LossCampaignError(f"rate numerator exceeds denominator: {successes}>{total}")
    return {
        "numerator": successes,
        "denominator": total,
        "rate": (successes / total) if total else None,
        "wilson_95": wilson_interval(successes, total),
    }


def _run_provenance_row(run: ResolvedCampaignRun, *, root: Path) -> dict[str, Any]:
    return {
        "batch_run_id": run.batch_run_id,
        "bundle_run_id": run.bundle_run_id,
        "contrast_id": run.contrast_id,
        "condition": run.condition,
        "seed": run.seed,
        "run_root": _relative_path(run.run_root, root),
        "successful_attempt": run.successful_attempt,
        "superseded_failed_attempts": list(run.superseded_failed_attempts),
        "artifact_sha256": run.source_hashes,
    }


def _manifest_source_rows(paths: Sequence[Path], *, root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_path in paths:
        path = _resolve_input_path(root, raw_path, label="batch manifest")
        payload = _read_json_object(path)
        rows.append(
            {
                "path": _relative_path(path, root),
                "sha256": _file_sha256(path),
                "git_commit": payload.get("git_commit"),
                "started_at": payload.get("started_at"),
                "ended_at": payload.get("ended_at"),
                "passed": payload.get("passed"),
                "failed_run_ids": payload.get("failed_run_ids"),
            }
        )
    return sorted(rows, key=lambda row: str(row["started_at"]))


def _git_state(root: Path) -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return {
            "commit": commit.stdout.strip() if commit.returncode == 0 else "unknown",
            "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
        }
    except (OSError, subprocess.SubprocessError):
        return {"commit": "unknown", "dirty": None}


def _resolve_plan_path(root: Path, value: str, *, label: str) -> Path:
    path = Path(value)
    resolved = (path if path.is_absolute() else root / path).resolve()
    return _require_within(resolved, root, label=label)


def _resolve_input_path(root: Path, value: Path, *, label: str) -> Path:
    path = Path(value)
    resolved = (path if path.is_absolute() else root / path).resolve()
    return _require_within(resolved, root, label=label)


def _resolve_run_root(root: Path, value: str) -> Path:
    if not value:
        raise LossCampaignError("run_root must be non-empty")
    path = Path(value)
    resolved = (path if path.is_absolute() else root / path).resolve()
    return _require_within(resolved, root, label="run_root")


def _require_within(path: Path, root: Path, *, label: str) -> Path:
    root = root.resolve()
    path = path.resolve()
    if path != root and root not in path.parents:
        raise LossCampaignError(f"{label} must stay within repository root: {path}")
    return path


def _relative_path(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _parse_iso(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise LossCampaignError(f"{label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LossCampaignError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise LossCampaignError(f"{label} must include a timezone")
    return parsed


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LossCampaignError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise LossCampaignError(f"{path.name} must contain a JSON object")
    return payload


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise LossCampaignError(f"cannot hash {path}: {exc}") from exc


def _canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
