from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .recorder import read_jsonl


DETECTION_RULE_SCHEMA_VERSION = "company_twin.detection_rules.v1"
COVERAGE_MAP_SCHEMA_VERSION = "company_twin.coverage_map.v1"
MIN_REPRO_RESULTS_SCHEMA_VERSION = "company_twin.min_repro_results.v1"

DEFAULT_DETECTION_RULES = {
    "schema_version": DETECTION_RULE_SCHEMA_VERSION,
    "rules": [
        {"rule_id": "R-EVIDENCE-GAP", "finding_type": "evidence_gap", "attempt_tools": ["submit_application"], "successes_only": True},
        {"rule_id": "R-GROUNDING-GAP", "finding_type": "grounding_gap", "basis_population": "action_bound"},
        {"rule_id": "R-VERSION-GAP", "finding_type": "version_gap", "basis_population": "retrieved_items"},
        {"rule_id": "R-DEADLINE-OVERRUN", "finding_type": "deadline_overrun", "ledger_event_types": ["contract_completed", "documents_delivered"]},
        {"rule_id": "R-SOD-PATTERN", "finding_type": "sod_pattern", "attempt_tools": ["approve_application"], "successes_only": True},
        {"rule_id": "R-APPROVAL-CONCENTRATION", "finding_type": "approval_concentration", "attempt_tools": ["approve_application"], "successes_only": True},
        {"rule_id": "R-VERSION-MIX", "finding_type": "version_mix", "basis_population": "action_bound"},
    ],
}


@dataclass(frozen=True)
class Finding:
    finding_type: str
    signature: str
    seat_id: str
    anchor_id: str
    phase: str
    detail: str
    opportunity_denominator: int = 1
    rate: float = 1.0


def run_l0_triage(run_root: Path) -> list[Finding]:
    attempts = read_jsonl(run_root / "attempts.jsonl")
    basis = read_jsonl(run_root / "basis_records.jsonl")
    ledger = read_jsonl(run_root / "world_ledger.jsonl")
    findings: list[Finding] = []

    for row in attempts:
        if not row.get("success") and row.get("denied_reason"):
            findings.append(_finding("hard_constraint_denial", row.get("seat_id", ""), row.get("tool", ""), "workflow", row.get("denied_reason", "")))
        if row.get("tool") == "submit_application" and row.get("success"):
            evidence = ((row.get("args") or {}).get("evidence") or {})
            missing = [key for key in ("consent_log_id", "recording_id", "material_version") if not evidence.get(key)]
            if missing:
                findings.append(_finding("evidence_gap", row.get("seat_id", ""), "submit_application", "application", "missing " + ",".join(missing)))

    read_by_seat_tick = _read_docs_by_seat_tick(attempts)
    read_handles_by_seat_tick = _read_handles_by_seat_tick(attempts)
    for row in basis:
        retrieved = row.get("retrieved") or []
        if not retrieved:
            findings.append(_finding("grounding_gap", row.get("seat_id", ""), row.get("trigger_event", ""), "basis", "basis has no retrieved documents"))
            continue
        for item in retrieved:
            doc_id = str(item.get("doc_id") or "")
            citation_handle = str(item.get("citation_handle") or "")
            if "span_id" in item:
                findings.append(_finding("world_basis_leak", row.get("seat_id", ""), doc_id, "basis", "basis includes span_id instead of citation_handle"))
            if not citation_handle:
                findings.append(_finding("grounding_gap", row.get("seat_id", ""), doc_id, "basis", "basis missing citation_handle"))
            elif not _handle_read_before(read_handles_by_seat_tick, str(row.get("seat_id") or ""), citation_handle, int(row.get("tick") or 0)):
                findings.append(_finding("grounding_gap", row.get("seat_id", ""), citation_handle, "basis", "basis citation_handle was not read before action"))
            if doc_id and not _read_before(read_by_seat_tick, str(row.get("seat_id") or ""), doc_id, int(row.get("tick") or 0)):
                findings.append(_finding("grounding_gap", row.get("seat_id", ""), doc_id, "basis", "basis doc was not read before action"))
            if not item.get("version"):
                findings.append(_finding("version_gap", row.get("seat_id", ""), doc_id, "basis", "basis missing document version"))

    for row in basis:
        for item in row.get("retrieved") or []:
            if str(item.get("doc_id") or "").endswith("@v1.0"):
                findings.append(_finding("version_skew_reference", row.get("seat_id", ""), str(item.get("doc_id")), "basis", "basis cites a stale v1.0 document"))
    findings.extend(_deadline_findings(ledger))
    findings.extend(_sod_findings(attempts))
    findings.extend(_version_mix_findings(basis))
    findings.extend(_concentration_findings(attempts))
    return findings


def write_triage(run_root: Path) -> dict[str, Any]:
    findings = run_l0_triage(run_root)
    triage_root = run_root / "triage"
    triage_root.mkdir(exist_ok=True)
    rows = [finding.__dict__ for finding in findings]
    if not rows:
        rows = [{"finding_type": "", "signature": "", "seat_id": "", "anchor_id": "", "phase": "", "detail": "", "opportunity_denominator": 0, "rate": 0.0}]
    pd.DataFrame(rows).to_parquet(run_root / "oracle_l0.parquet", index=False)
    buckets = _bucketize(findings, run_root)
    metrics = _metrics(run_root, findings)
    payload = {"run_root": str(run_root), "bucket_count": len(buckets), "finding_count": len(findings), "metrics": metrics, "buckets": list(buckets.values())}
    (triage_root / "buckets.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (triage_root / "review.html").write_text(_html_report(payload), encoding="utf-8")
    (triage_root / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 0.0)
    phat = successes / total
    denom = 1 + z * z / total
    center = (phat + z * z / (2 * total)) / denom
    margin = (z * ((phat * (1 - phat) / total + z * z / (4 * total * total)) ** 0.5)) / denom
    return (max(center - margin, 0.0), min(center + margin, 1.0))


def aggregate_ensemble_triage(campaign_root: Path) -> dict[str, Any]:
    """Ensemble-level triage (partial answer to reviewer Major 4): group run
    bundles by config identity (stage, probe, knobs) across seeds and report
    per-finding-type incidence rates with Wilson intervals. Attribution and
    min-repro outputs are candidate queues until the explicit min-repro runner
    marks jobs reproduced."""
    groups: dict[str, dict[str, Any]] = {}
    for run_root in sorted(path for path in campaign_root.iterdir() if path.is_dir()):
        meta_path = run_root / "meta.json"
        metrics_path = run_root / "triage" / "metrics.json"
        if not meta_path.exists() or not metrics_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        config_id = json.dumps({"stage": meta.get("stage"), "probe": meta.get("probe"), "knobs": meta.get("knobs") or {}, "anchor": meta.get("anchor", False)}, sort_keys=True, ensure_ascii=False)
        group = groups.setdefault(config_id, {"config": json.loads(config_id), "seeds": 0, "finding_seed_counts": {}, "controlled_actions": 0, "detection_miss": {}})
        group["seeds"] += 1
        group["controlled_actions"] += int(metrics.get("controlled_actions_agent") or 0)
        for finding_type in (metrics.get("finding_types") or {}):
            group["finding_seed_counts"][finding_type] = group["finding_seed_counts"].get(finding_type, 0) + 1
        for rule_id, row in sorted((metrics.get("detection_miss_rate") or {}).items()):
            accumulator = group["detection_miss"].setdefault(rule_id, {"opportunity_count": 0, "miss_count": 0, "hit_count": 0, "finding_type": row.get("finding_type")})
            accumulator["opportunity_count"] += int(row.get("opportunity_count") or 0)
            accumulator["miss_count"] += int(row.get("miss_count") or 0)
            accumulator["hit_count"] += int(row.get("hit_count") or 0)
    out = []
    for config_id, group in sorted(groups.items()):
        rates = {}
        for finding_type, seed_hits in sorted(group["finding_seed_counts"].items()):
            low, high = wilson_interval(seed_hits, group["seeds"])
            rates[finding_type] = {"seeds_with_finding": seed_hits, "seeds": group["seeds"], "rate": seed_hits / group["seeds"], "wilson_95": [round(low, 4), round(high, 4)]}
        detection_miss = {
            rule_id: {
                **row,
                "miss_rate": (row["miss_count"] / row["opportunity_count"]) if row["opportunity_count"] else None,
            }
            for rule_id, row in sorted(group["detection_miss"].items())
        }
        out.append({"config": group["config"], "seeds": group["seeds"], "controlled_actions_total": group["controlled_actions"], "finding_rates": rates, "detection_miss_rate": detection_miss})
    attribution_table = _attribution_table(out)
    min_repro_jobs = _min_repro_jobs(out)
    finding_registry = _finding_registry(out, min_repro_jobs)
    (campaign_root / "attribution_table.json").write_text(json.dumps({"rows": attribution_table}, ensure_ascii=False, indent=2), encoding="utf-8")
    (campaign_root / "min_repro_jobs.json").write_text(json.dumps({"jobs": min_repro_jobs}, ensure_ascii=False, indent=2), encoding="utf-8")
    (campaign_root / "finding_registry.json").write_text(json.dumps(finding_registry, ensure_ascii=False, indent=2), encoding="utf-8")
    coverage_map = write_coverage_map(campaign_root)
    payload = {
        "groups": out,
        "attribution_table": attribution_table,
        "min_repro_jobs": min_repro_jobs,
        "finding_registry": finding_registry,
        "coverage_map": {"path": "coverage_map.json", "cell_counts": coverage_map["cell_counts"]},
        "note": "candidate-level triage only: delta=1 attribution and min-repro jobs are queued until execute_min_repro_jobs runs",
    }
    (campaign_root / "ensemble_triage.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _attribution_table(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, left in enumerate(groups):
        for right in groups[idx + 1 :]:
            delta = _single_knob_delta(left["config"], right["config"])
            if delta is None:
                continue
            finding_types = sorted(set(left.get("finding_rates", {})) | set(right.get("finding_rates", {})))
            for finding_type in finding_types:
                left_rate = float(((left.get("finding_rates") or {}).get(finding_type) or {}).get("rate") or 0.0)
                right_rate = float(((right.get("finding_rates") or {}).get(finding_type) or {}).get("rate") or 0.0)
                if left_rate == right_rate:
                    continue
                rows.append(
                    {
                        "status": "candidate",
                        "finding_type": finding_type,
                        "delta_knob": delta["knob"],
                        "left_value": delta["left"],
                        "right_value": delta["right"],
                        "left_config": left["config"],
                        "right_config": right["config"],
                        "left_rate": left_rate,
                        "right_rate": right_rate,
                        "effect_delta": round(right_rate - left_rate, 6),
                    }
                )
    return rows


def _single_knob_delta(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any] | None:
    comparable_keys = {"stage", "probe", "anchor"}
    if any(left.get(key) != right.get(key) for key in comparable_keys):
        return None
    left_knobs = left.get("knobs") or {}
    right_knobs = right.get("knobs") or {}
    all_knobs = sorted(set(left_knobs) | set(right_knobs))
    diffs = [knob for knob in all_knobs if bool(left_knobs.get(knob)) != bool(right_knobs.get(knob))]
    if len(diffs) != 1:
        return None
    knob = diffs[0]
    return {"knob": knob, "left": bool(left_knobs.get(knob)), "right": bool(right_knobs.get(knob))}


def _min_repro_jobs(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for group in groups:
        for finding_type, rate in sorted((group.get("finding_rates") or {}).items()):
            job = {
                "status": "pending",
                "min_repro_status": "pending",
                "finding_type": finding_type,
                "config": group["config"],
                "seeds_with_finding": rate["seeds_with_finding"],
                "seeds": rate["seeds"],
                "rate": rate["rate"],
                "wilson_95": rate["wilson_95"],
                "confirmation_protocol": ["source_bundle_match", "deck_one_card_if_probe_bound", "tick_back_trim", "seat_shrink"],
            }
            job["job_id"] = _min_repro_job_id(job)
            jobs.append(job)
    return jobs


def _finding_registry(groups: list[dict[str, Any]], min_repro_jobs: list[dict[str, Any]]) -> dict[str, Any]:
    reproduced_jobs = [job for job in min_repro_jobs if job.get("status") == "reproduced"]
    confirmed = [_confirmed_finding(job) for job in reproduced_jobs]
    exploratory = [
        {
            "finding_type": finding_type,
            "config": group["config"],
            "status": "exploratory",
            "reason": "min_repro_not_reproduced",
            "rate": rate["rate"],
            "seeds_with_finding": rate["seeds_with_finding"],
            "seeds": rate["seeds"],
        }
        for group in groups
        for finding_type, rate in sorted((group.get("finding_rates") or {}).items())
        if not any(job.get("finding_type") == finding_type and job.get("config") == group["config"] for job in reproduced_jobs)
    ]
    return {
        "schema_version": "company_twin.finding_registry.v1",
        "confirmed_findings": confirmed,
        "exploratory_buckets": exploratory,
        "audit_hypothesis_cards": [_audit_hypothesis_card(job) for job in reproduced_jobs],
        "note": "Only reproduced min-repro jobs may become confirmed findings or audit hypothesis cards.",
    }


def execute_min_repro_jobs(campaign_root: Path, *, min_rate: float = 0.5, min_seeds: int = 1) -> dict[str, Any]:
    """Consume queued min-repro jobs using existing campaign evidence.

    The runner is deterministic: it does not fabricate a live rerun. It matches
    queued jobs back to same-config run bundles, writes a per-job min-repro
    manifest, and only promotes jobs whose observed source bundles satisfy the
    pre-registered reproduction threshold.
    """
    if not 0 <= min_rate <= 1:
        raise ValueError("--min-rate must be between 0 and 1")
    if min_seeds < 1:
        raise ValueError("--min-seeds must be >= 1")
    campaign_root = campaign_root.resolve()
    ensemble = _read_json(campaign_root / "ensemble_triage.json")
    if not ensemble:
        ensemble = aggregate_ensemble_triage(campaign_root)
    groups = ensemble.get("groups") or []
    queued_payload = _read_json(campaign_root / "min_repro_jobs.json")
    queued_jobs = queued_payload.get("jobs") or ensemble.get("min_repro_jobs") or _min_repro_jobs(groups)

    executed_jobs: list[dict[str, Any]] = []
    result_rows: list[dict[str, Any]] = []
    for job in queued_jobs:
        normalized = dict(job)
        normalized.setdefault("job_id", _min_repro_job_id(normalized))
        result = _execute_min_repro_job(campaign_root, normalized, min_rate=min_rate, min_seeds=min_seeds)
        updated = {
            **normalized,
            "status": result["status"],
            "min_repro_status": result["status"],
            "matching_bundle_count": result["matching_bundle_count"],
            "source_bundle_count": result["source_bundle_count"],
            "reproduction_rate": result["reproduction_rate"],
            "confirmation_path": result["confirmation_path"],
            "source_bundles": result["source_bundles"],
            "coverage_cells": result["coverage_cells"],
        }
        executed_jobs.append(updated)
        result_rows.append(result)

    registry = _finding_registry(groups, executed_jobs)
    payload = {
        "schema_version": MIN_REPRO_RESULTS_SCHEMA_VERSION,
        "campaign_root": str(campaign_root),
        "threshold": {"min_rate": min_rate, "min_seeds": min_seeds},
        "job_count": len(result_rows),
        "reproduced_count": sum(1 for row in result_rows if row["status"] == "reproduced"),
        "jobs": result_rows,
    }
    (campaign_root / "min_repro_results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (campaign_root / "min_repro_jobs.json").write_text(json.dumps({"jobs": executed_jobs}, ensure_ascii=False, indent=2), encoding="utf-8")
    (campaign_root / "finding_registry.json").write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")

    ensemble["min_repro_jobs"] = executed_jobs
    ensemble["finding_registry"] = registry
    ensemble["min_repro_results"] = {
        "path": "min_repro_results.json",
        "schema_version": MIN_REPRO_RESULTS_SCHEMA_VERSION,
        "reproduced_count": payload["reproduced_count"],
        "job_count": payload["job_count"],
    }
    ensemble["note"] = "min-repro jobs have been executed against recorded campaign evidence; confirmed findings require status=reproduced"
    (campaign_root / "ensemble_triage.json").write_text(json.dumps(ensemble, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _execute_min_repro_job(campaign_root: Path, job: dict[str, Any], *, min_rate: float, min_seeds: int) -> dict[str, Any]:
    finding_type = str(job.get("finding_type") or "")
    matching_roots = _matching_run_roots(campaign_root, job.get("config") or {})
    source_bundles = [
        evidence
        for run_root in matching_roots
        if (evidence := _bundle_finding_evidence(campaign_root, run_root, finding_type))
    ]
    denominator = max(int(job.get("seeds") or 0), len(matching_roots), 1)
    reproduction_rate = len(source_bundles) / denominator
    status = "reproduced" if len(source_bundles) >= min_seeds and reproduction_rate >= min_rate else "not_reproduced"
    coverage_cells = _coverage_cells_for_finding(campaign_root, finding_type)
    result = {
        "job_id": job["job_id"],
        "finding_type": finding_type,
        "config": job.get("config") or {},
        "status": status,
        "min_repro_status": status,
        "queued_rate": job.get("rate"),
        "queued_wilson_95": job.get("wilson_95"),
        "threshold": {"min_rate": min_rate, "min_seeds": min_seeds},
        "matching_bundle_count": len(matching_roots),
        "source_bundle_count": len(source_bundles),
        "reproduction_rate": reproduction_rate,
        "source_bundles": source_bundles,
        "coverage_cells": coverage_cells,
        "reduction_trace": _reduction_trace(job, source_bundles),
    }
    manifest_dir = campaign_root / "min_repro" / str(job["job_id"])
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "manifest.json"
    result["confirmation_path"] = _relative_path(manifest_path, campaign_root)
    manifest_path.write_text(json.dumps({"schema_version": MIN_REPRO_RESULTS_SCHEMA_VERSION, **result}, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _matching_run_roots(campaign_root: Path, config: dict[str, Any]) -> list[Path]:
    target = _config_key(config)
    roots: list[Path] = []
    for run_root in sorted(path for path in campaign_root.iterdir() if path.is_dir()):
        meta = _read_json(run_root / "meta.json")
        if not meta:
            continue
        if _config_key(_config_from_meta(meta)) == target:
            roots.append(run_root)
    return roots


def _bundle_finding_evidence(campaign_root: Path, run_root: Path, finding_type: str) -> dict[str, Any] | None:
    metrics = _read_json(run_root / "triage" / "metrics.json")
    finding_count = int(((metrics.get("finding_types") or {}).get(finding_type)) or 0)
    buckets_payload = _read_json(run_root / "triage" / "buckets.json")
    buckets = [bucket for bucket in (buckets_payload.get("buckets") or []) if bucket.get("finding_type") == finding_type]
    if finding_count <= 0 and not buckets:
        return None
    ticks = _evidence_ticks(run_root, finding_type, buckets)
    seats = sorted({str(bucket.get("seat_id") or "") for bucket in buckets if bucket.get("seat_id")})
    return {
        "run_id": run_root.name,
        "run_root": _relative_path(run_root, campaign_root),
        "seed": _read_json(run_root / "meta.json").get("seed"),
        "finding_count": finding_count or sum(int(bucket.get("count") or 0) for bucket in buckets),
        "bucket_signatures": [str(bucket.get("signature") or "") for bucket in buckets if bucket.get("signature")],
        "seats": seats,
        "tick_window": {"start": min(ticks), "end": max(ticks)} if ticks else None,
    }


def _evidence_ticks(run_root: Path, finding_type: str, buckets: list[dict[str, Any]]) -> list[int]:
    attempts = read_jsonl(run_root / "attempts.jsonl")
    basis = read_jsonl(run_root / "basis_records.jsonl")
    ledger = read_jsonl(run_root / "world_ledger.jsonl")
    bucket_seats = {str(bucket.get("seat_id") or "") for bucket in buckets if bucket.get("seat_id")}
    ticks: list[int] = []
    if finding_type == "evidence_gap":
        for row in attempts:
            evidence = ((row.get("args") or {}).get("evidence") or {})
            missing = [key for key in ("consent_log_id", "recording_id", "material_version") if not evidence.get(key)]
            if row.get("tool") == "submit_application" and row.get("success") and missing:
                ticks.append(int(row.get("tick") or 0))
    elif finding_type in {"hard_constraint_denial", "sod_pattern", "approval_concentration"}:
        for row in attempts:
            if not bucket_seats or str(row.get("seat_id") or "") in bucket_seats:
                ticks.append(int(row.get("tick") or 0))
    elif finding_type in {"grounding_gap", "version_gap", "version_mix", "version_skew_reference", "world_basis_leak"}:
        for row in basis:
            if not bucket_seats or str(row.get("seat_id") or "") in bucket_seats:
                ticks.append(int(row.get("tick") or 0))
    elif finding_type == "deadline_overrun":
        deadline_ticks = [int(row.get("tick") or 0) for row in ledger if row.get("event_type") == "campaign_deadline"]
        for row in ledger:
            if row.get("event_type") in {"contract_completed", "documents_delivered"} and deadline_ticks and int(row.get("tick") or 0) > deadline_ticks[0]:
                ticks.append(int(row.get("tick") or 0))
    if not ticks:
        ticks = [int(row.get("tick") or 0) for row in attempts + basis + ledger if row.get("tick") is not None]
    return [tick for tick in ticks if tick > 0]


def _coverage_cells_for_finding(campaign_root: Path, finding_type: str) -> list[dict[str, Any]]:
    coverage = _read_json(campaign_root / "coverage_map.json")
    rows = ((coverage.get("cells") or {}).get("C4_signature_vocab") or []) if coverage else []
    prefix = f"{finding_type} | "
    return [row for row in rows if str(row.get("cell") or "").startswith(prefix)]


def _reduction_trace(job: dict[str, Any], source_bundles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    config = job.get("config") or {}
    ticks = [bundle.get("tick_window") for bundle in source_bundles if bundle.get("tick_window")]
    seats = sorted({seat for bundle in source_bundles for seat in (bundle.get("seats") or [])})
    mutations = config.get("mutations") or config.get("corpus_mutations") or []
    return [
        {
            "step": "drop_inert_mutations",
            "status": "not_applicable" if not mutations else "requires_live_rerun",
            "retained_mutations": mutations,
        },
        {
            "step": "deck_one_card",
            "status": "selected" if config.get("probe") else "not_applicable",
            "probe": config.get("probe"),
        },
        {
            "step": "tick_back_trim",
            "status": "bounded" if ticks else "not_observed",
            "tick_window": {"start": min(row["start"] for row in ticks), "end": max(row["end"] for row in ticks)} if ticks else None,
        },
        {
            "step": "seat_shrink",
            "status": "bounded" if seats else "not_observed",
            "seats": seats,
        },
    ]


def _confirmed_finding(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job.get("job_id"),
        "finding_type": job.get("finding_type"),
        "config": job.get("config") or {},
        "status": "reproduced",
        "min_repro_status": "reproduced",
        "reproduction_rate": job.get("reproduction_rate"),
        "source_bundle_count": job.get("source_bundle_count"),
        "matching_bundle_count": job.get("matching_bundle_count"),
        "confirmation_path": job.get("confirmation_path"),
        "coverage_cells": job.get("coverage_cells") or [],
    }


def _audit_hypothesis_card(job: dict[str, Any]) -> dict[str, Any]:
    config = job.get("config") or {}
    finding_type = str(job.get("finding_type") or "")
    stage = str(config.get("stage") or "unknown")
    probe = str(config.get("probe") or "full_deck")
    return {
        "card_id": f"HYP-{str(job.get('job_id') or '')[:12]}",
        "finding_type": finding_type,
        "hypothesis": f"{finding_type} reproduces under {stage}/{probe} and should be reviewed as a confirmed audit hypothesis.",
        "min_repro": {
            "job_id": job.get("job_id"),
            "status": "reproduced",
            "reproduction_rate": job.get("reproduction_rate"),
            "confirmation_path": job.get("confirmation_path"),
        },
        "divergence_cells": job.get("coverage_cells") or [],
        "source_bundles": job.get("source_bundles") or [],
    }


def _min_repro_job_id(job: dict[str, Any]) -> str:
    payload = {"finding_type": job.get("finding_type"), "config": job.get("config") or {}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _config_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {"stage": meta.get("stage"), "probe": meta.get("probe"), "knobs": meta.get("knobs") or {}, "anchor": meta.get("anchor", False)}


def _config_key(config: dict[str, Any]) -> str:
    return json.dumps(_config_from_meta(config), sort_keys=True, ensure_ascii=False)


def _relative_path(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base)).replace("\\", "/")
    except ValueError:
        return str(path)


def signature_for(*, finding_type: str, anchor_id: str, seat_id: str, phase: str, artifact_skeleton: str) -> str:
    normalized = {
        "finding_type": finding_type,
        "anchor_id": _mask(anchor_id),
        "role": _seat_to_role(seat_id),
        "phase": phase,
        "artifact_skeleton": _mask(artifact_skeleton),
    }
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _finding(finding_type: str, seat_id: str, anchor_id: str, phase: str, detail: str, *, denominator: int = 1) -> Finding:
    signature = signature_for(finding_type=finding_type, anchor_id=anchor_id, seat_id=seat_id, phase=phase, artifact_skeleton=detail)
    denominator = max(denominator, 1)
    return Finding(finding_type=finding_type, signature=signature, seat_id=seat_id, anchor_id=anchor_id, phase=phase, detail=detail, opportunity_denominator=denominator, rate=1 / denominator)


def _bucketize(findings: list[Finding], run_root: Path) -> dict[str, dict[str, Any]]:
    counts = Counter(finding.signature for finding in findings)
    examples: dict[str, Finding] = {}
    for finding in findings:
        examples.setdefault(finding.signature, finding)
    buckets: dict[str, dict[str, Any]] = {}
    for signature, count in counts.items():
        example = examples[signature]
        denominator = max(example.opportunity_denominator, count)
        buckets[signature] = {
            "signature": signature,
            "count": count,
            "opportunity_denominator": denominator,
            "rate": count / denominator,
            "finding_type": example.finding_type,
            "seat_id": example.seat_id,
            "anchor_id": example.anchor_id,
            "phase": example.phase,
            "example": example.detail,
            "first_seen_stage": _stage(run_root),
            "first_seen_config": run_root.name,
            "min_repro_status": "candidate",
        }
    return buckets


CONTROLLED_TOOL_NAMES = {"record_customer_contact", "request_approval", "approve_application", "return_application", "submit_application", "verify_identity", "link_review", "complete_contract", "deliver_documents"}


class ScriptedOriginError(RuntimeError):
    """A run bundle contains records from the banned scripted path."""


def _metrics(run_root: Path, findings: list[Finding]) -> dict[str, Any]:
    attempts = read_jsonl(run_root / "attempts.jsonl")
    ledger = read_jsonl(run_root / "world_ledger.jsonl")
    basis = read_jsonl(run_root / "basis_records.jsonl")
    store_events = read_jsonl(run_root / "store_events.jsonl")
    origin_breakdown = dict(Counter(str(row.get("origin") or "unknown") for row in attempts))
    banned = {origin for origin in origin_breakdown if origin not in {"system", "agent", "customer"}}
    if banned:
        raise ScriptedOriginError(f"{run_root.name}: banned origins present in attempts: {sorted(banned)}")
    # Measurement population is agent-originated records ONLY (MASTER_DESIGN P8 /
    # fix WI-8): controlled actions and their basis must come from seat agents.
    controlled = [row for row in attempts if row.get("tool") in CONTROLLED_TOOL_NAMES and row.get("success") and row.get("origin") == "agent"]
    agent_seats = {row.get("seat_id") for row in attempts if row.get("origin") == "agent"}
    agent_basis = [row for row in basis if row.get("seat_id") in agent_seats]
    action_bound_basis = [row for row in agent_basis if row.get("action_id")]
    standalone_basis = [row for row in agent_basis if not row.get("action_id")]
    grounded = [row for row in action_bound_basis if row.get("grounded")]
    g1 = [row for row in action_bound_basis if _g1_citation_value(row) is True]
    g2 = [row for row in action_bound_basis if row.get("g2_prior_read") is True]
    g3_machine = [row for row in action_bound_basis if (row.get("g3_machine_heuristic") or row.get("g3_entailment")) == "supported"]
    all3 = [
        row
        for row in action_bound_basis
        if _g1_citation_value(row) is True and row.get("g2_prior_read") is True and (row.get("g3_machine_heuristic") or row.get("g3_entailment")) == "supported"
    ]
    store_reads = [row for row in store_events if row.get("op") == "read" and row.get("origin") == "agent"]
    store_writes = [row for row in store_events if row.get("op") == "write" and row.get("origin") == "agent"]
    first_read_tick_by_seat: dict[str, int] = {}
    for row in store_reads:
        seat_id = str(row.get("seat_id") or "")
        if not seat_id:
            continue
        tick = int(row.get("tick") or 0)
        first_read_tick_by_seat[seat_id] = min(tick, first_read_tick_by_seat.get(seat_id, 999999))
    controlled_after_store_read = [
        row
        for row in controlled
        if first_read_tick_by_seat.get(str(row.get("seat_id") or ""), 999999) <= int(row.get("tick") or 0)
    ]
    detection_miss = detection_miss_rates(attempts=attempts, ledger=ledger, basis=basis, findings=findings, run_root=run_root)
    return {
        "stage": _stage(run_root),
        "attempts": len(attempts),
        "origin_breakdown": origin_breakdown,
        "controlled_actions_agent": len(controlled),
        "basis_records_agent": len(agent_basis),
        "basis_action_bound": len(action_bound_basis),
        "basis_standalone": len(standalone_basis),
        "grounding_coverage_machine": (len(grounded) / len(controlled)) if controlled else 0.0,
        "grounding_g1_citation_handle_exists_rate": (len(g1) / len(action_bound_basis)) if action_bound_basis else 0.0,
        "grounding_g1_rate": (len(g1) / len(action_bound_basis)) if action_bound_basis else 0.0,
        "grounding_g2_prior_read_rate": (len(g2) / len(action_bound_basis)) if action_bound_basis else 0.0,
        "grounding_g2_rate": (len(g2) / len(action_bound_basis)) if action_bound_basis else 0.0,
        "grounding_g3_rate": (len(g3_machine) / len(action_bound_basis)) if action_bound_basis else 0.0,
        "grounding_g3_machine_heuristic_rate": (len(g3_machine) / len(action_bound_basis)) if action_bound_basis else 0.0,
        "grounding_semantic_all3_rate": None,
        "grounding_all3_rate": (len(all3) / len(action_bound_basis)) if action_bound_basis else 0.0,
        "grounding_machine_all3_rate": (len(all3) / len(action_bound_basis)) if action_bound_basis else 0.0,
        "store_writes_agent": len(store_writes),
        "store_reads_agent": len(store_reads),
        "controlled_actions_after_store_read": len(controlled_after_store_read),
        "customer_events": sum(1 for row in ledger if row.get("event_type") == "customer_event"),
        "permission_denied": sum(1 for row in attempts if not row.get("success")),
        "llm_invocations": sum(1 for row in attempts if row.get("tool") == "llm_invoke"),
        "finding_types": dict(Counter(finding.finding_type for finding in findings)),
        "detection_miss_rate": detection_miss,
    }


def load_detection_rules(root: Path | None = None) -> dict[str, Any]:
    path = _find_detection_rules_path(root) if root is not None else None
    if path is not None:
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = DEFAULT_DETECTION_RULES
    if payload.get("schema_version") != DETECTION_RULE_SCHEMA_VERSION:
        raise ValueError(f"detection rules schema_version must be {DETECTION_RULE_SCHEMA_VERSION}")
    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("detection rules must contain a non-empty rules list")
    for rule in rules:
        if not rule.get("rule_id") or not rule.get("finding_type"):
            raise ValueError("each detection rule requires rule_id and finding_type")
    return payload


def detection_miss_rates(*, attempts: list[dict[str, Any]], ledger: list[dict[str, Any]], basis: list[dict[str, Any]], findings: list[Finding], run_root: Path) -> dict[str, Any]:
    rules = load_detection_rules(run_root)
    finding_counts = Counter(finding.finding_type for finding in findings)
    rows: dict[str, Any] = {}
    for rule in rules["rules"]:
        rule_id = str(rule["rule_id"])
        finding_type = str(rule["finding_type"])
        opportunities = _rule_opportunities(rule, attempts=attempts, ledger=ledger, basis=basis)
        hit_count = int(finding_counts.get(finding_type, 0))
        miss_count = max(opportunities - min(hit_count, opportunities), 0)
        rows[rule_id] = {
            "finding_type": finding_type,
            "opportunity_count": opportunities,
            "hit_count": hit_count,
            "miss_count": miss_count,
            "miss_rate": (miss_count / opportunities) if opportunities else None,
        }
    return rows


def build_coverage_map(run_roots: list[Path]) -> dict[str, Any]:
    cells: dict[str, Counter[str]] = {"C1_span_role_interpretation": Counter(), "C2_transition_edges": Counter(), "C3_norm_contacts": Counter(), "C4_signature_vocab": Counter(), "C5_evidence_skeleton": Counter()}
    run_ids: list[str] = []
    for run_root in sorted(run_roots, key=lambda path: path.name):
        if not (run_root / "meta.json").exists():
            continue
        run_ids.append(run_root.name)
        attempts = read_jsonl(run_root / "attempts.jsonl")
        ledger = read_jsonl(run_root / "world_ledger.jsonl")
        basis = read_jsonl(run_root / "basis_records.jsonl")
        s0_answer = _read_json(run_root / "s0_answer.json")
        if s0_answer:
            cells["C1_span_role_interpretation"][_coverage_key(str(s0_answer.get("span_id") or ""), str(s0_answer.get("role") or _seat_to_role(str(s0_answer.get("seat_id") or ""))), str(s0_answer.get("likely_reading") or s0_answer.get("next_action") or "unparsed")[:80])] += 1
        for row in basis:
            docs = ",".join(sorted(str(item.get("doc_id") or "") for item in row.get("retrieved") or [] if item.get("doc_id")))
            label = str(row.get("g3_machine_heuristic") or row.get("g3_entailment") or "not_evaluated")
            cells["C1_span_role_interpretation"][_coverage_key(docs or "no_docs", _seat_to_role(str(row.get("seat_id") or "")), label)] += 1
        previous_event = ""
        for row in ledger:
            event_type = str(row.get("event_type") or "")
            if previous_event:
                cells["C2_transition_edges"][_coverage_key(previous_event, event_type)] += 1
            previous_event = event_type
        for row in attempts:
            role = _seat_to_role(str(row.get("seat_id") or ""))
            tool = str(row.get("tool") or "")
            if tool == "read_document" and row.get("success"):
                doc_id = str((row.get("args") or {}).get("doc_id") or "")
                cells["C3_norm_contacts"][_coverage_key(role, doc_id)] += 1
            if not row.get("success") and row.get("denied_reason"):
                cells["C2_transition_edges"][_coverage_key("denied", role, tool, str(row.get("denied_reason") or "")[:80])] += 1
            if tool in CONTROLLED_TOOL_NAMES:
                evidence = (row.get("args") or {}).get("evidence") or {}
                evidence_keys = ",".join(sorted(evidence)) if isinstance(evidence, dict) else ""
                cells["C5_evidence_skeleton"][_coverage_key(role, tool, "success" if row.get("success") else "denied", evidence_keys)] += 1
        buckets = _read_json(run_root / "triage" / "buckets.json")
        for bucket in (buckets.get("buckets") or []) if buckets else []:
            cells["C4_signature_vocab"][_coverage_key(str(bucket.get("finding_type") or ""), str(bucket.get("phase") or ""), str(bucket.get("signature") or ""))] += int(bucket.get("count") or 1)
    rendered = {
        name: [
            {"cell": cell, "count": count}
            for cell, count in sorted(counter.items())
        ]
        for name, counter in sorted(cells.items())
    }
    return {
        "schema_version": COVERAGE_MAP_SCHEMA_VERSION,
        "run_count": len(run_ids),
        "run_ids": run_ids,
        "cell_counts": {name: len(rows) for name, rows in rendered.items()},
        "cells": rendered,
    }


def write_coverage_map(campaign_root: Path) -> dict[str, Any]:
    run_roots = [path for path in campaign_root.iterdir() if path.is_dir()]
    payload = build_coverage_map(run_roots)
    (campaign_root / "coverage_map.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _rule_opportunities(rule: dict[str, Any], *, attempts: list[dict[str, Any]], ledger: list[dict[str, Any]], basis: list[dict[str, Any]]) -> int:
    total = 0
    tools = set(rule.get("attempt_tools") or [])
    if tools:
        rows = [row for row in attempts if row.get("tool") in tools and row.get("origin") == "agent"]
        if rule.get("successes_only", True):
            rows = [row for row in rows if row.get("success")]
        total += len(rows)
    ledger_types = set(rule.get("ledger_event_types") or [])
    if ledger_types:
        total += sum(1 for row in ledger if row.get("event_type") in ledger_types)
    basis_population = rule.get("basis_population")
    if basis_population == "action_bound":
        total += sum(1 for row in basis if row.get("action_id"))
    elif basis_population == "retrieved_items":
        total += sum(len(row.get("retrieved") or []) for row in basis)
    return total


def _find_detection_rules_path(start: Path | None) -> Path | None:
    if start is None:
        return None
    for candidate_root in [start, *start.parents]:
        candidate = candidate_root / "data" / "compiled_data" / "detection_rules_v1.json"
        if candidate.exists():
            return candidate
    return None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _coverage_key(*parts: str) -> str:
    return " | ".join(part for part in parts if part)


def _read_docs_by_seat_tick(attempts: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    reads: dict[tuple[str, str], int] = {}
    for row in attempts:
        if row.get("tool") != "read_document" or not row.get("success"):
            continue
        key = (str(row.get("seat_id") or ""), str((row.get("args") or {}).get("doc_id") or ""))
        reads[key] = min(int(row.get("tick") or 0), reads.get(key, 999999))
    return reads


def _read_handles_by_seat_tick(attempts: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    reads: dict[tuple[str, str], int] = {}
    for row in attempts:
        if row.get("tool") != "read_document" or not row.get("success"):
            continue
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        citation_handle = str((result or {}).get("citation_handle") or "")
        if not citation_handle:
            continue
        key = (str(row.get("seat_id") or ""), citation_handle)
        reads[key] = min(int(row.get("tick") or 0), reads.get(key, 999999))
    return reads


def _read_before(reads: dict[tuple[str, str], int], seat_id: str, doc_id: str, tick: int) -> bool:
    return reads.get((seat_id, doc_id), 999999) <= tick


def _handle_read_before(reads: dict[tuple[str, str], int], seat_id: str, citation_handle: str, tick: int) -> bool:
    return reads.get((seat_id, citation_handle), 999999) <= tick


def _g1_citation_value(row: dict[str, Any]) -> bool | None:
    value = row.get("g1_citation_handle_exists")
    if value is not None:
        return bool(value)
    legacy = row.get("g1_span_exists")
    if legacy is not None:
        return bool(legacy)
    return None


def _deadline_findings(ledger: list[dict[str, Any]]) -> list[Finding]:
    deadline_ticks = [row.get("tick") for row in ledger if row.get("event_type") == "campaign_deadline"]
    completed_after = [row for row in ledger if row.get("event_type") in {"contract_completed", "documents_delivered"} and deadline_ticks and int(row.get("tick") or 0) > int(deadline_ticks[0])]
    return [_finding("deadline_overrun", "", str((row.get("payload") or {}).get("application_id") or ""), "deadline", "completed after campaign deadline", denominator=max(len(completed_after), 1)) for row in completed_after]


def _sod_findings(attempts: list[dict[str, Any]]) -> list[Finding]:
    submitters = {str((row.get("args") or {}).get("application_id") or ""): row.get("seat_id") for row in attempts if row.get("tool") == "submit_application" and row.get("success")}
    findings: list[Finding] = []
    for row in attempts:
        if row.get("tool") != "approve_application" or not row.get("success"):
            continue
        app_id = str((row.get("args") or {}).get("application_id") or "")
        if app_id and submitters.get(app_id) == row.get("seat_id"):
            findings.append(_finding("sod_pattern", row.get("seat_id", ""), app_id, "approval", "submitter approved same application", denominator=max(len(submitters), 1)))
    return findings


def _version_mix_findings(basis: list[dict[str, Any]]) -> list[Finding]:
    versions_by_app: dict[str, set[str]] = defaultdict(set)
    for row in basis:
        action_id = str(row.get("action_id") or row.get("trigger_event") or "")
        for item in row.get("retrieved") or []:
            version = str(item.get("version") or "")
            if version:
                versions_by_app[action_id].add(version)
    return [_finding("version_mix", "", action_id, "basis", "multiple document versions cited in one action", denominator=len(versions_by_app)) for action_id, versions in versions_by_app.items() if len(versions) > 1]


def _concentration_findings(attempts: list[dict[str, Any]]) -> list[Finding]:
    approvals = [row for row in attempts if row.get("tool") == "approve_application" and row.get("success")]
    if len(approvals) < 4:
        return []
    counts = Counter(row.get("seat_id") for row in approvals)
    seat_id, count = counts.most_common(1)[0]
    if count / len(approvals) >= 0.8:
        return [_finding("approval_concentration", str(seat_id), "approve_application", "approval", "single approver concentration >=80%", denominator=len(approvals))]
    return []


def _stage(run_root: Path) -> str:
    meta = run_root / "meta.json"
    if meta.exists():
        try:
            return str(json.loads(meta.read_text(encoding="utf-8")).get("stage") or "")
        except json.JSONDecodeError:
            return ""
    return ""


def _seat_to_role(seat_id: str) -> str:
    if seat_id in {"emp-A", "emp-B", "emp-F", "emp-G"}:
        return "sales"
    if seat_id == "emp-C":
        return "application"
    if seat_id == "emp-M":
        return "manager"
    if seat_id == "emp-Q":
        return "second_line"
    return seat_id or "unknown"


def _mask(value: str) -> str:
    value = value or ""
    value = re.sub(r"DFH-SAL-\d{3}", "<DOC_ID>", value)
    value = re.sub(r"APP-[A-Za-z0-9-]+", "<APP_ID>", value)
    value = re.sub(r"CUS-[A-Za-z0-9-]+", "<CUSTOMER_ID>", value)
    value = re.sub(r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b", "<DATE>", value)
    value = re.sub(r"\b\d{1,2}:\d{2}\b", "<TIME>", value)
    value = re.sub(r"\b\d+(?:,\d{3})*(?:円|万円)?\b", "<AMOUNT>", value)
    return value[:300]


def _html_report(payload: dict[str, Any]) -> str:
    rows = []
    for bucket in payload["buckets"]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(bucket['signature'])}</td>"
            f"<td>{html.escape(bucket['finding_type'])}</td>"
            f"<td>{bucket['count']}</td>"
            f"<td>{bucket['opportunity_denominator']}</td>"
            f"<td>{bucket['rate']:.3f}</td>"
            f"<td>{html.escape(bucket['seat_id'])}</td>"
            f"<td>{html.escape(bucket['anchor_id'])}</td>"
            f"<td>{html.escape(bucket['example'])}</td>"
            "</tr>"
        )
    metrics = html.escape(json.dumps(payload["metrics"], ensure_ascii=False, indent=2))
    return """<!doctype html>
<html lang="ja">
<meta charset="utf-8">
<title>Company Twin Triage</title>
<style>
body { font-family: system-ui, sans-serif; margin: 32px; color: #1f2933; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #d8dee9; padding: 8px; vertical-align: top; }
th { background: #eef2f7; text-align: left; }
code, pre { background: #eef2f7; padding: 2px 4px; }
</style>
<h1>Company Twin Triage</h1>
<p>Run root: <code>""" + html.escape(payload["run_root"]) + """</code></p>
<p>Findings: """ + str(payload["finding_count"]) + """ / Buckets: """ + str(payload["bucket_count"]) + """</p>
<h2>Metrics</h2>
<pre>""" + metrics + """</pre>
<h2>Bucket Explorer</h2>
<table>
<thead><tr><th>Signature</th><th>Type</th><th>Count</th><th>Denominator</th><th>Rate</th><th>Seat</th><th>Anchor</th><th>Example</th></tr></thead>
<tbody>
""" + "\n".join(rows) + """
</tbody>
</table>
</html>
"""
