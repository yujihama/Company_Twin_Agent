from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .recorder import read_jsonl
from .semantic_grounding import READINESS_ALLOWED_JUDGE_BACKENDS, evaluate_semantic_grounding_run


DETECTION_RULE_SCHEMA_VERSION = "company_twin.detection_rules.v2"
COVERAGE_MAP_SCHEMA_VERSION = "company_twin.coverage_map.v1"
MIN_REPRO_RESULTS_SCHEMA_VERSION = "company_twin.min_repro_results.v1"
MIN_REPRO_CONFIRMATION_SCHEMA_VERSION = "company_twin.min_repro_confirmation.v1"

DEFAULT_DETECTION_RULES = {
    "schema_version": DETECTION_RULE_SCHEMA_VERSION,
    "rules": [
        {"rule_id": "TRUTH-EVIDENCE-GAP", "population": "truth", "finding_type": "evidence_gap", "attempt_tools": ["submit_application"], "successes_only": True},
        {"rule_id": "TRUTH-GROUNDING-GAP", "population": "truth", "finding_type": "grounding_gap", "basis_population": "action_bound"},
        {"rule_id": "TRUTH-VERSION-GAP", "population": "truth", "finding_type": "version_gap", "basis_population": "retrieved_items"},
        {"rule_id": "TRUTH-DEADLINE-OVERRUN", "population": "truth", "finding_type": "deadline_overrun", "ledger_event_types": ["contract_completed", "documents_delivered"]},
        {"rule_id": "TRUTH-SOD-PATTERN", "population": "truth", "finding_type": "sod_pattern", "attempt_tools": ["approve_application"], "successes_only": True},
        {"rule_id": "TRUTH-APPROVAL-CONCENTRATION", "population": "truth", "finding_type": "approval_concentration", "attempt_tools": ["approve_application"], "successes_only": True},
        {"rule_id": "TRUTH-VERSION-MIX", "population": "truth", "finding_type": "version_mix", "basis_population": "action_bound"},
        {
            "rule_id": "MON-MISSING-COMPLETION-EVIDENCE",
            "population": "monitoring",
            "mode": "missing_required_evidence",
            "detects": ["evidence_gap"],
            "attempt_tools": ["submit_application"],
            "required_evidence": ["consent_log_id", "recording_id", "material_version"],
        },
        {
            "rule_id": "MON-DEADLINE-OVERRUN",
            "population": "monitoring",
            "mode": "deadline_after_campaign_deadline",
            "detects": ["deadline_overrun"],
            "ledger_event_types": ["contract_completed", "documents_delivered"],
        },
        {
            "rule_id": "MON-SAME-SUBMITTER-APPROVER",
            "population": "monitoring",
            "mode": "same_submitter_approver",
            "detects": ["sod_pattern"],
        },
        {
            "rule_id": "MON-APPROVAL-CONCENTRATION",
            "population": "monitoring",
            "mode": "same_seat_approval_count",
            "detects": ["approval_concentration"],
            "attempt_tools": ["approve_application"],
            "min_count": 4,
            "min_share": 0.8,
        },
        {"rule_id": "TRUTH-TACIT-CHAT-ACTION", "population": "truth", "finding_type": "tacit_chat_to_action", "ledger_event_types": ["chat_message"]},
        {"rule_id": "TRUTH-RAPID-RESUBMIT", "population": "truth", "finding_type": "rapid_resubmit_after_return", "ledger_event_types": ["application_returned"]},
        {"rule_id": "TRUTH-ALTERNATIVE-APPROVAL-CHAIN", "population": "truth", "finding_type": "alternative_approval_chain", "attempt_tools": ["approve_application"], "successes_only": True},
        {
            "rule_id": "MON-CHAT-KEYWORD-ACTION-WINDOW",
            "population": "monitoring",
            "mode": "chat_keyword_followed_by_action",
            "detects": ["tacit_chat_to_action"],
            "keywords": ["承認", "例外", "証跡", "急ぎ", "不足", "差戻"],
            "window_ticks": 2,
        },
        {
            "rule_id": "MON-RAPID-RESUBMIT-SAME-TICK",
            "population": "monitoring",
            "mode": "returned_then_resubmitted_same_day",
            "detects": ["rapid_resubmit_after_return"],
            "window_ticks": 1,
        },
        {
            "rule_id": "MON-MULTI-APPROVER-SAME-APPLICATION",
            "population": "monitoring",
            "mode": "multiple_approvers_same_application",
            "detects": ["alternative_approval_chain"],
            "min_distinct_approvers": 2,
        },
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
    findings.extend(_chat_action_correlation_findings(attempts, ledger))
    findings.extend(_rapid_resubmit_findings(ledger))
    findings.extend(_approval_chain_findings(ledger))
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
    if not (run_root / "g3_semantic_grounding.json").exists():
        evaluate_semantic_grounding_run(run_root)
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
    min-repro outputs are candidate queues; the default min-repro command only
    collates exploration evidence and does not confirm findings."""
    groups: dict[str, dict[str, Any]] = {}
    run_roots = _ensemble_run_roots(campaign_root)
    for run_root in run_roots:
        meta_path = run_root / "meta.json"
        metrics_path = run_root / "triage" / "metrics.json"
        if not meta_path.exists() or not metrics_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        config = _config_from_run(run_root, meta)
        config_id = json.dumps(config, sort_keys=True, ensure_ascii=False)
        group = groups.setdefault(
            config_id,
            {
                "config": json.loads(config_id),
                "seeds": 0,
                "seed_values": set(),
                "run_roots": [],
                "finding_seed_counts": {},
                "finding_seed_presence": {},
                "any_finding_seed_count": 0,
                "controlled_actions": 0,
                "rule_hit": {},
                "detection_miss": {},
            },
        )
        group["seeds"] += 1
        group["run_roots"].append(run_root.name)
        seed = meta.get("seed")
        if seed is not None:
            group["seed_values"].add(int(seed))
        group["controlled_actions"] += int(metrics.get("controlled_actions_agent") or 0)
        if metrics.get("finding_types"):
            group["any_finding_seed_count"] += 1
        for finding_type, count in (metrics.get("finding_types") or {}).items():
            group["finding_seed_counts"][finding_type] = group["finding_seed_counts"].get(finding_type, 0) + 1
            if seed is not None:
                group["finding_seed_presence"].setdefault(finding_type, {})[int(seed)] = int(count or 0)
        for rule_id, row in sorted((metrics.get("rule_hit_rate") or {}).items()):
            accumulator = group["rule_hit"].setdefault(rule_id, {"opportunity_count": 0, "hit_count": 0, "finding_type": row.get("finding_type")})
            accumulator["opportunity_count"] += int(row.get("opportunity_count") or 0)
            accumulator["hit_count"] += int(row.get("hit_count") or 0)
        for finding_type, row in sorted((metrics.get("detection_miss_rate") or {}).items()):
            accumulator = group["detection_miss"].setdefault(finding_type, {"truth_count": 0, "detected_count": 0, "silent_count": 0, "monitoring_rules": set()})
            accumulator["truth_count"] += int(row.get("truth_count") or 0)
            accumulator["detected_count"] += int(row.get("detected_count") or 0)
            accumulator["silent_count"] += int(row.get("silent_count") or 0)
            accumulator["monitoring_rules"].update(row.get("monitoring_rules") or [])
    out = []
    for config_id, group in sorted(groups.items()):
        rates = {}
        for finding_type, seed_hits in sorted(group["finding_seed_counts"].items()):
            low, high = wilson_interval(seed_hits, group["seeds"])
            rates[finding_type] = {"seeds_with_finding": seed_hits, "seeds": group["seeds"], "rate": seed_hits / group["seeds"], "wilson_95": [round(low, 4), round(high, 4)]}
        rule_hit = {
            rule_id: {
                **row,
                "hit_rate": (row["hit_count"] / row["opportunity_count"]) if row["opportunity_count"] else None,
            }
            for rule_id, row in sorted(group["rule_hit"].items())
        }
        detection_miss = {
            finding_type: {
                **{key: value for key, value in row.items() if key != "monitoring_rules"},
                "monitoring_rules": sorted(row["monitoring_rules"]),
                "miss_rate": (row["silent_count"] / row["truth_count"]) if row["truth_count"] else None,
            }
            for finding_type, row in sorted(group["detection_miss"].items())
        }
        out.append(
            {
                "config": group["config"],
                "seeds": group["seeds"],
                "seed_values": sorted(group["seed_values"]),
                "run_roots": sorted(group["run_roots"]),
                "any_finding_seed_count": group["any_finding_seed_count"],
                "controlled_actions_total": group["controlled_actions"],
                "finding_rates": rates,
                "rule_hit_rate": rule_hit,
                "detection_miss_rate": detection_miss,
                "seed_stability_icc": _seed_stability_icc(group),
            }
        )
    attribution_table = _attribution_table(out)
    min_repro_jobs = _min_repro_jobs(out)
    finding_registry = _finding_registry(out, min_repro_jobs)
    (campaign_root / "attribution_table.json").write_text(json.dumps({"rows": attribution_table}, ensure_ascii=False, indent=2), encoding="utf-8")
    (campaign_root / "min_repro_jobs.json").write_text(json.dumps({"jobs": min_repro_jobs}, ensure_ascii=False, indent=2), encoding="utf-8")
    (campaign_root / "finding_registry.json").write_text(json.dumps(finding_registry, ensure_ascii=False, indent=2), encoding="utf-8")
    coverage_map = write_coverage_map(campaign_root, run_roots=run_roots)
    payload = {
        "groups": out,
        "attribution_table": attribution_table,
        "min_repro_jobs": min_repro_jobs,
        "finding_registry": finding_registry,
        "icc_summary": _icc_summary(out),
        "run_filter": _run_filter_metadata(campaign_root, run_roots),
        "coverage_map": {"path": "coverage_map.json", "cell_counts": coverage_map["cell_counts"]},
        "note": "candidate-level triage only: delta=1 attribution and min-repro jobs are queued until execute_min_repro_jobs runs",
    }
    (campaign_root / "ensemble_triage.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _attribution_table(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, left in enumerate(groups):
        for right in groups[idx + 1 :]:
            delta = _single_config_delta(left["config"], right["config"])
            if delta is None:
                continue
            finding_types = sorted(set(left.get("finding_rates", {})) | set(right.get("finding_rates", {})))
            if delta["field"] == "world.corpus.mutations":
                finding_types = ["any_l0_finding", *finding_types]
            for finding_type in finding_types:
                left_rate_row = _attribution_rate_row(left, finding_type)
                right_rate_row = _attribution_rate_row(right, finding_type)
                left_rate = float(left_rate_row.get("rate") or 0.0)
                right_rate = float(right_rate_row.get("rate") or 0.0)
                if left_rate == right_rate and finding_type != "any_l0_finding":
                    continue
                left_wilson = list(left_rate_row.get("wilson_95") or [0.0, 0.0])
                right_wilson = list(right_rate_row.get("wilson_95") or [0.0, 0.0])
                seed_bundle_match = _seed_bundle_match(left, right)
                rows.append(
                    {
                        "status": "candidate" if seed_bundle_match else "invalid_seed_mismatch",
                        "finding_type": finding_type,
                        "delta": delta["field"],
                        "delta_knob": delta.get("knob") or delta["field"],
                        "left_value": delta["left"],
                        "right_value": delta["right"],
                        "left_config": left["config"],
                        "right_config": right["config"],
                        "left_seeds": left.get("seed_values") or [],
                        "right_seeds": right.get("seed_values") or [],
                        "seed_bundle_match": seed_bundle_match,
                        "left_rate": left_rate,
                        "right_rate": right_rate,
                        "left_wilson_95": left_wilson,
                        "right_wilson_95": right_wilson,
                        "effect_delta": round(right_rate - left_rate, 6),
                        "effect_delta_wilson_95": [round(right_wilson[0] - left_wilson[1], 4), round(right_wilson[1] - left_wilson[0], 4)],
                    }
                )
    return rows


def _attribution_rate_row(group: dict[str, Any], finding_type: str) -> dict[str, Any]:
    if finding_type != "any_l0_finding":
        return (group.get("finding_rates") or {}).get(finding_type) or {}
    seeds = int(group.get("seeds") or 0)
    hits = int(group.get("any_finding_seed_count") or 0)
    low, high = wilson_interval(hits, seeds)
    return {
        "seeds_with_finding": hits,
        "seeds": seeds,
        "rate": hits / seeds if seeds else 0.0,
        "wilson_95": [round(low, 4), round(high, 4)],
    }


def _single_config_delta(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any] | None:
    comparable_keys = {"stage", "probe", "anchor"}
    if any(left.get(key) != right.get(key) for key in comparable_keys):
        return None
    left_knobs = left.get("knobs") or {}
    right_knobs = right.get("knobs") or {}
    all_knobs = sorted(set(left_knobs) | set(right_knobs))
    knob_diffs = [knob for knob in all_knobs if bool(left_knobs.get(knob)) != bool(right_knobs.get(knob))]
    left_mutations = list(left.get("mutation_ids") or [])
    right_mutations = list(right.get("mutation_ids") or [])
    mutation_diff = left_mutations != right_mutations
    if len(knob_diffs) + int(mutation_diff) != 1:
        return None
    if knob_diffs:
        knob = knob_diffs[0]
        return {"field": f"world.kernel_profile.knobs.{knob}", "knob": knob, "left": bool(left_knobs.get(knob)), "right": bool(right_knobs.get(knob))}
    return {"field": "world.corpus.mutations", "left": left_mutations, "right": right_mutations}


def _seed_bundle_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_seeds = set(left.get("seed_values") or [])
    right_seeds = set(right.get("seed_values") or [])
    return bool(left_seeds) and left_seeds == right_seeds


def _ensemble_run_roots(campaign_root: Path) -> list[Path]:
    roots = sorted(path for path in campaign_root.iterdir() if path.is_dir() and not (path / "failed_run.json").exists())
    if not (campaign_root / "control_pair_manifest.json").exists():
        return roots
    return [run_root for run_root in roots if (_read_json(run_root / "meta.json") or {}).get("campaign_mode") == "control_pairs"]


def _run_filter_metadata(campaign_root: Path, run_roots: list[Path]) -> dict[str, Any]:
    mode = "control_pairs" if (campaign_root / "control_pair_manifest.json").exists() else "all"
    excluded = sorted(path.name for path in campaign_root.iterdir() if path.is_dir() and (path / "failed_run.json").exists())
    return {"mode": mode, "included_run_count": len(run_roots), "included_run_ids": [path.name for path in run_roots], "excluded_failed_run_ids": excluded}


def _config_from_run(run_root: Path, meta: dict[str, Any]) -> dict[str, Any]:
    config = _config_from_meta(meta)
    run_config = _read_json(run_root / "config.json")
    corpus = ((run_config.get("world") or {}).get("corpus") or {}) if run_config else {}
    mutations = corpus.get("mutations") if corpus else meta.get("mutations")
    mutation_ids = [str(item.get("mutation_id") or "") for item in (mutations or []) if item.get("mutation_id")]
    if not mutation_ids:
        mutation_ids = [str(item) for item in (meta.get("mutation_ids") or []) if item]
    config["mutation_ids"] = mutation_ids
    config["mutation_hash"] = corpus.get("mutation_hash") or meta.get("mutation_hash") or ""
    config["effective_corpus_hash"] = corpus.get("effective_corpus_hash") or meta.get("effective_corpus_hash") or ""
    return config


def _seed_stability_icc(group: dict[str, Any]) -> dict[str, Any]:
    seed_values = sorted(group.get("seed_values") or [])
    by_finding_type: dict[str, Any] = {}
    if len(seed_values) < 2:
        return {
            "method": "binary_seed_presence_icc_proxy",
            "status": "not_enough_seeds",
            "seed_count": len(seed_values),
            "by_finding_type": by_finding_type,
            "mean_icc": None,
        }
    for finding_type, presence in sorted((group.get("finding_seed_presence") or {}).items()):
        vector = [1 if int(presence.get(seed) or 0) > 0 else 0 for seed in seed_values]
        positives = sum(vector)
        if not vector:
            icc = None
            status = "not_observed"
        elif positives in {0, len(vector)}:
            icc = 1.0
            status = "stable"
        else:
            mean = positives / len(vector)
            sample_variance = sum((value - mean) ** 2 for value in vector) / (len(vector) - 1)
            max_binary_variance = (len(vector) / (len(vector) - 1)) * mean * (1 - mean)
            icc = round(max(0.0, 1.0 - (sample_variance / max_binary_variance if max_binary_variance else 0.0)), 4)
            status = "mixed_seed_instability" if icc < 0.5 else "partially_stable"
        by_finding_type[finding_type] = {
            "icc": icc,
            "status": status,
            "seed_count": len(vector),
            "positive_seeds": positives,
            "rate": positives / len(vector) if vector else None,
        }
    values = [row["icc"] for row in by_finding_type.values() if row.get("icc") is not None]
    return {
        "method": "binary_seed_presence_icc_proxy",
        "status": "ok" if values else "no_findings",
        "seed_count": len(seed_values),
        "by_finding_type": by_finding_type,
        "mean_icc": round(sum(values) / len(values), 4) if values else None,
    }


def _icc_summary(groups: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for group in groups:
        icc = group.get("seed_stability_icc") or {}
        value = icc.get("mean_icc")
        rows.append({"config": group.get("config") or {}, "mean_icc": value, "status": icc.get("status")})
    values = [row["mean_icc"] for row in rows if row.get("mean_icc") is not None]
    return {
        "method": "binary_seed_presence_icc_proxy",
        "group_count": len(groups),
        "estimable_group_count": len(values),
        "mean_icc": round(sum(values) / len(values), 4) if values else None,
        "groups": rows,
    }


def _min_repro_jobs(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for group in groups:
        for finding_type, rate in sorted((group.get("finding_rates") or {}).items()):
            pre_registered = _pre_registered_confirmation_from_rate(rate)
            job = {
                "status": "pending",
                "min_repro_status": "pending",
                "finding_type": finding_type,
                "config": group["config"],
                "seeds_with_finding": rate["seeds_with_finding"],
                "seeds": rate["seeds"],
                "rate": rate["rate"],
                "wilson_95": rate["wilson_95"],
                "pre_registered_confirmation": pre_registered,
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
            "reason": "confirmation_run_not_reproduced",
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
        "note": "Only fresh confirmation runs with status=reproduced may become confirmed findings or audit hypothesis cards. Evidence-collation manifests from exploration bundles are never sufficient.",
    }


def execute_min_repro_jobs(campaign_root: Path, *, min_rate: float = 0.5, min_seeds: int = 3) -> dict[str, Any]:
    """Collate queued min-repro evidence from existing campaign bundles.

    This default path is not a confirmation run. It writes manifests that help a
    later live min-repro execution, but it must never promote findings to
    confirmed or audit hypothesis cards.
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
        if normalized.get("status") == "reproduced":
            # Fresh confirmation owns reproduced records; collation must not overwrite them.
            executed_jobs.append(normalized)
            result_rows.append(
                {
                    "job_id": normalized["job_id"],
                    "finding_type": normalized.get("finding_type"),
                    "config": normalized.get("config") or {},
                    "status": "reproduced",
                    "min_repro_status": "reproduced",
                    "pre_registered_confirmation": normalized.get("pre_registered_confirmation") or {},
                    "confirmation_path": normalized.get("confirmation_path"),
                    "source_bundle_count": normalized.get("source_bundle_count"),
                    "matching_bundle_count": normalized.get("matching_bundle_count"),
                    "confirmation_skipped": True,
                    "note": "already reproduced by fresh confirmation; existing record preserved",
                }
            )
            continue
        result = _execute_min_repro_job(campaign_root, normalized, min_rate=min_rate, min_seeds=min_seeds)
        updated = {
            **normalized,
            "pre_registered_confirmation": result["pre_registered_confirmation"],
            "status": result["status"],
            "min_repro_status": result["status"],
            "matching_bundle_count": result["matching_bundle_count"],
            "source_bundle_count": result["source_bundle_count"],
            "evidence_rate": result["evidence_rate"],
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
        "evidence_collated_count": sum(1 for row in result_rows if row["status"] == "evidence_collated"),
        "reproduced_count": sum(1 for row in result_rows if row["status"] == "reproduced"),
        "jobs": result_rows,
        "note": "This file collates exploration evidence only. It is not new confirmation evidence; already-reproduced jobs are preserved but not created by collation.",
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
        "evidence_collated_count": payload["evidence_collated_count"],
        "job_count": payload["job_count"],
    }
    ensemble["note"] = "min-repro evidence has been collated from exploration bundles only; confirmed findings require fresh status=reproduced confirmation runs"
    (campaign_root / "ensemble_triage.json").write_text(json.dumps(ensemble, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def execute_fresh_min_repro_confirmation(
    campaign_root: Path,
    *,
    design: Any | None = None,
    corpus: Any | None = None,
    job_id: str | None = None,
    finding_type: str | None = None,
    confirmation_seeds: int = 3,
    seed_start: int = 100,
    min_rate: float = 0.5,
    ticks: int = 6,
    model: str | None = None,
    prompt_mode: str = "measurement",
    model_bindings: dict[str, str] | None = None,
    seat_factory: Any | None = None,
    customer_llm_factory: Callable[[Path], Any] | None = None,
    timed_notice_recipients: list[str] | None = None,
    seats_subset: list[str] | None = None,
    allow_threshold_override: bool = False,
    confirmation_bundle_runner: Callable[[Path, int, dict[str, Any], str], None] | None = None,
) -> dict[str, Any]:
    """Run fresh confirmation bundles for one queued min-repro job.

    Unlike execute_min_repro_jobs(), this is the WP-10b path: it creates new
    run bundles under min_repro/<job_id>/runs with disjoint seeds, checks the
    same finding type, and only marks the job reproduced when fresh evidence
    reaches the pre-registered rate threshold.
    """
    if confirmation_seeds < 1:
        raise ValueError("confirmation_seeds must be >= 1")
    if ticks < 1:
        raise ValueError("ticks must be >= 1")
    if not 0 <= min_rate <= 1:
        raise ValueError("min_rate must be between 0 and 1")
    campaign_root = campaign_root.resolve()
    ensemble = _read_json(campaign_root / "ensemble_triage.json") or aggregate_ensemble_triage(campaign_root)
    groups = ensemble.get("groups") or []
    queued_payload = _read_json(campaign_root / "min_repro_jobs.json")
    jobs = queued_payload.get("jobs") or ensemble.get("min_repro_jobs") or _min_repro_jobs(groups)
    if not jobs:
        raise ValueError("no queued min-repro jobs found")
    selected = _select_min_repro_job(jobs, job_id=job_id, finding_type=finding_type)
    selected_job_id = str(selected.get("job_id") or _min_repro_job_id(selected))
    selected_finding = str(selected.get("finding_type") or "")
    config = selected.get("config") or {}
    stage = str(config.get("stage") or "")
    if stage not in {"S1", "S2"}:
        raise ValueError(f"fresh min-repro confirmation supports S1/S2 jobs, got stage={stage!r}")
    if stage == "S1" and not config.get("probe"):
        raise ValueError("S1 confirmation job is missing probe")
    pre_registered = _pre_registered_confirmation_for_job(selected)
    requested_threshold = {"min_rate": min_rate, "confirmation_seeds": confirmation_seeds}
    threshold_matches = _confirmation_threshold_matches(pre_registered, requested_threshold)
    threshold_override = {
        "enabled": not threshold_matches,
        "requested": requested_threshold,
        "pre_registered": pre_registered,
    }
    if not threshold_matches and not allow_threshold_override:
        raise ValueError(
            "confirmation threshold does not match pre_registered_confirmation; "
            f"requested={requested_threshold}, pre_registered={pre_registered}"
        )

    exploration_roots = _matching_run_roots(campaign_root, config)
    exploration_seeds = _seed_values(exploration_roots)
    reference_source_bundles = list(selected.get("source_bundles") or [])
    if not reference_source_bundles:
        reference_source_bundles = [
            evidence
            for run_root in exploration_roots
            if (evidence := _bundle_finding_evidence(campaign_root, run_root, selected_finding))
        ]
    expected_signatures = _bucket_signature_set(reference_source_bundles)
    inferred_seats_subset = _source_bundle_seats(reference_source_bundles)
    effective_seats_subset = sorted(set(seats_subset or inferred_seats_subset)) if stage == "S2" else None
    confirmation_config = dict(config)
    if effective_seats_subset:
        confirmation_config["seats_subset"] = effective_seats_subset
    fresh_seeds = _fresh_seed_values(seed_start=seed_start, count=confirmation_seeds, excluded=exploration_seeds)
    job_root = campaign_root / "min_repro" / selected_job_id
    runs_root = job_root / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    run_rows: list[dict[str, Any]] = []
    source_bundles: list[dict[str, Any]] = []
    for seed in fresh_seeds:
        run_root = runs_root / f"{stage.lower()}_{config.get('probe') or 'full_deck'}_confirm_seed{seed}"
        if confirmation_bundle_runner is not None:
            confirmation_bundle_runner(run_root, seed, confirmation_config, selected_finding)
        else:
            if design is None or corpus is None:
                raise ValueError("design and corpus are required when no confirmation_bundle_runner is supplied")
            _run_fresh_confirmation_bundle(
                run_root=run_root,
                design=design,
                corpus=corpus,
                config=confirmation_config,
                seed=seed,
                ticks=ticks,
                model=model,
                prompt_mode=prompt_mode,
                model_bindings=model_bindings,
                seat_factory=seat_factory,
                customer_llm_factory=customer_llm_factory,
                timed_notice_recipients=[] if timed_notice_recipients is None else timed_notice_recipients,
                seats_subset=effective_seats_subset,
            )
        if not (run_root / "triage" / "metrics.json").exists():
            write_triage(run_root)
        evidence = _bundle_finding_evidence(campaign_root, run_root, selected_finding, expected_signatures=expected_signatures)
        if evidence:
            source_bundles.append(evidence)
        meta = _read_json(run_root / "meta.json")
        metrics = _read_json(run_root / "triage" / "metrics.json")
        raw_finding_count = int(((metrics.get("finding_types") or {}).get(selected_finding)) or 0)
        run_rows.append(
            {
                "run_root": _relative_path(run_root, campaign_root),
                "seed": seed,
                "stage": meta.get("stage"),
                "probe": meta.get("probe"),
                "live": meta.get("live"),
                "backend": meta.get("backend"),
                "finding_count": raw_finding_count,
                "matched_signature_finding_count": int((evidence or {}).get("finding_count") or 0),
            }
        )

    type_confirmation_successes = sum(1 for row in run_rows if int(row.get("finding_count") or 0) > 0)
    signature_confirmation_successes = len(source_bundles)
    type_reproduction_rate = type_confirmation_successes / confirmation_seeds
    signature_reproduction_rate = signature_confirmation_successes / confirmation_seeds
    type_reproduction_wilson = _rounded_wilson(type_confirmation_successes, confirmation_seeds)
    signature_reproduction_wilson = _rounded_wilson(signature_confirmation_successes, confirmation_seeds)
    # Backward-compatible confirmation fields are signature-scoped: a fresh run
    # only confirms when it reproduces one of the queued source signatures.
    reproduction_rate = signature_reproduction_rate
    reproduction_wilson = signature_reproduction_wilson
    status = "reproduced" if source_bundles and signature_reproduction_rate >= min_rate else "not_reproduced"
    manifest = {
        "schema_version": MIN_REPRO_CONFIRMATION_SCHEMA_VERSION,
        "job_id": selected_job_id,
        "finding_type": selected_finding,
        "config": config,
        "status": status,
        "min_repro_status": status,
        "threshold": {"min_rate": min_rate, "confirmation_seeds": confirmation_seeds},
        "pre_registered_confirmation": pre_registered,
        "threshold_override": threshold_override,
        "exploration_seeds": sorted(exploration_seeds),
        "fresh_seeds": fresh_seeds,
        "expected_bucket_signatures": sorted(expected_signatures),
        "seats_subset": effective_seats_subset,
        "confirmation_run_count": len(run_rows),
        "source_bundle_count": len(source_bundles),
        "confirmation_successes": len(source_bundles),
        "reproduction_rate": reproduction_rate,
        "reproduction_rate_wilson_95": reproduction_wilson,
        **_confirmation_rate_fields(
            type_confirmation_successes=type_confirmation_successes,
            type_reproduction_rate=type_reproduction_rate,
            type_reproduction_wilson=type_reproduction_wilson,
            signature_confirmation_successes=signature_confirmation_successes,
            signature_reproduction_rate=signature_reproduction_rate,
            signature_reproduction_wilson=signature_reproduction_wilson,
        ),
        "source_bundles": source_bundles,
        "runs": run_rows,
        "coverage_cells": _coverage_cells_for_finding(campaign_root, selected_finding),
        "reduction_trace": _reduction_trace(selected, source_bundles),
        "note": "fresh confirmation run; reproduced status may promote confirmed findings only through this manifest",
    }
    manifest_path = job_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    updated_jobs = []
    for job in jobs:
        normalized = dict(job)
        normalized.setdefault("job_id", _min_repro_job_id(normalized))
        if str(normalized["job_id"]) == selected_job_id:
            normalized.update(
                {
                    "pre_registered_confirmation": pre_registered,
                    "status": status,
                    "min_repro_status": status,
                    "confirmation_path": _relative_path(manifest_path, campaign_root),
                    "source_bundles": source_bundles,
                    "source_bundle_count": len(source_bundles),
                    "matching_bundle_count": len(source_bundles),
                    "confirmation_successes": len(source_bundles),
                    "confirmation_seeds": confirmation_seeds,
                    "reproduction_rate": reproduction_rate,
                    "reproduction_rate_wilson_95": reproduction_wilson,
                    **_confirmation_rate_fields(
                        type_confirmation_successes=type_confirmation_successes,
                        type_reproduction_rate=type_reproduction_rate,
                        type_reproduction_wilson=type_reproduction_wilson,
                        signature_confirmation_successes=signature_confirmation_successes,
                        signature_reproduction_rate=signature_reproduction_rate,
                        signature_reproduction_wilson=signature_reproduction_wilson,
                    ),
                    "threshold_override": threshold_override,
                    "expected_bucket_signatures": sorted(expected_signatures),
                    "seats_subset": effective_seats_subset,
                    "coverage_cells": manifest["coverage_cells"],
                }
            )
        updated_jobs.append(normalized)

    registry = _finding_registry(groups, updated_jobs)
    payload = {
        "schema_version": MIN_REPRO_CONFIRMATION_SCHEMA_VERSION,
        "campaign_root": str(campaign_root),
        "job_id": selected_job_id,
        "status": status,
        "reproduced_count": 1 if status == "reproduced" else 0,
        "confirmed_count": len(registry.get("confirmed_findings") or []),
        "manifest_path": _relative_path(manifest_path, campaign_root),
        "source_bundle_count": len(source_bundles),
        "confirmation_successes": len(source_bundles),
        "reproduction_rate": reproduction_rate,
        "reproduction_rate_wilson_95": reproduction_wilson,
        **_confirmation_rate_fields(
            type_confirmation_successes=type_confirmation_successes,
            type_reproduction_rate=type_reproduction_rate,
            type_reproduction_wilson=type_reproduction_wilson,
            signature_confirmation_successes=signature_confirmation_successes,
            signature_reproduction_rate=signature_reproduction_rate,
            signature_reproduction_wilson=signature_reproduction_wilson,
        ),
        "pre_registered_confirmation": pre_registered,
        "threshold_override": threshold_override,
        "expected_bucket_signatures": sorted(expected_signatures),
        "seats_subset": effective_seats_subset,
        "runs": run_rows,
    }
    (campaign_root / "min_repro_confirmation_results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (campaign_root / "min_repro_jobs.json").write_text(json.dumps({"jobs": updated_jobs}, ensure_ascii=False, indent=2), encoding="utf-8")
    (campaign_root / "finding_registry.json").write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    ensemble["min_repro_jobs"] = updated_jobs
    ensemble["finding_registry"] = registry
    ensemble["min_repro_confirmation_results"] = {
        "path": "min_repro_confirmation_results.json",
        "schema_version": MIN_REPRO_CONFIRMATION_SCHEMA_VERSION,
        "reproduced_count": payload["reproduced_count"],
        "confirmed_count": payload["confirmed_count"],
    }
    ensemble["note"] = "fresh min-repro confirmation has run; only jobs with status=reproduced are confirmed"
    (campaign_root / "ensemble_triage.json").write_text(json.dumps(ensemble, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _select_min_repro_job(jobs: list[dict[str, Any]], *, job_id: str | None, finding_type: str | None) -> dict[str, Any]:
    matches = []
    for job in jobs:
        normalized = dict(job)
        normalized.setdefault("job_id", _min_repro_job_id(normalized))
        if job_id and str(normalized["job_id"]) != job_id:
            continue
        if finding_type and str(normalized.get("finding_type") or "") != finding_type:
            continue
        matches.append(normalized)
    if not matches:
        raise ValueError("no queued min-repro job matched the requested selector")
    matches.sort(key=lambda item: (-float(item.get("rate") or 0.0), str(item.get("finding_type") or ""), str(item.get("job_id") or "")))
    return matches[0]


def _seed_values(run_roots: list[Path]) -> set[int]:
    values: set[int] = set()
    for run_root in run_roots:
        meta = _read_json(run_root / "meta.json")
        if meta.get("seed") is not None:
            values.add(int(meta["seed"]))
    return values


def _fresh_seed_values(*, seed_start: int, count: int, excluded: set[int]) -> list[int]:
    seeds: list[int] = []
    candidate = seed_start
    while len(seeds) < count:
        if candidate not in excluded:
            seeds.append(candidate)
        candidate += 1
    return seeds


def _run_fresh_confirmation_bundle(
    *,
    run_root: Path,
    design: Any,
    corpus: Any,
    config: dict[str, Any],
    seed: int,
    ticks: int,
    model: str | None,
    prompt_mode: str,
    model_bindings: dict[str, str] | None,
    seat_factory: Any | None,
    customer_llm_factory: Callable[[Path], Any] | None,
    timed_notice_recipients: list[str],
    seats_subset: list[str] | None,
) -> None:
    from .harness import run_s1_episode, run_s2_world
    from .mutations import apply_corpus_mutations, mutation_specs_from_values

    mutation_ids = [str(item) for item in (config.get("mutation_ids") or []) if item]
    mutation_result = apply_corpus_mutations(corpus, mutation_specs_from_values(design.root, mutation_ids))
    customer_llm = customer_llm_factory(run_root) if customer_llm_factory is not None else None
    common = {
        "design": design,
        "corpus": mutation_result.corpus,
        "run_root": run_root,
        "model": model,
        "knobs": config.get("knobs") or {},
        "seed": seed,
        "seat_factory": seat_factory,
        "customer_llm": customer_llm,
        "prompt_mode": prompt_mode,
        "model_bindings": model_bindings,
        "mutations": mutation_result.applied,
        "timed_notice_recipients": timed_notice_recipients,
    }
    if config.get("stage") == "S1":
        run_s1_episode(probe_id=str(config.get("probe") or ""), ticks=ticks, **common)
    else:
        run_s2_world(ticks=ticks, anchor=False, seats_subset=seats_subset, **common)
    write_triage(run_root)


def _execute_min_repro_job(campaign_root: Path, job: dict[str, Any], *, min_rate: float, min_seeds: int) -> dict[str, Any]:
    finding_type = str(job.get("finding_type") or "")
    pre_registered = _pre_registered_confirmation_for_job(job)
    matching_roots = _matching_run_roots(campaign_root, job.get("config") or {})
    source_bundles = [
        evidence
        for run_root in matching_roots
        if (evidence := _bundle_finding_evidence(campaign_root, run_root, finding_type))
    ]
    denominator = max(int(job.get("seeds") or 0), len(matching_roots), 1)
    evidence_rate = len(source_bundles) / denominator
    status = "evidence_collated"
    coverage_cells = _coverage_cells_for_finding(campaign_root, finding_type)
    result = {
        "job_id": job["job_id"],
        "finding_type": finding_type,
        "config": job.get("config") or {},
        "status": status,
        "min_repro_status": status,
        "queued_rate": job.get("rate"),
        "queued_wilson_95": job.get("wilson_95"),
        "pre_registered_confirmation": pre_registered,
        "threshold": {"min_rate": min_rate, "min_seeds": min_seeds},
        "matching_bundle_count": len(matching_roots),
        "source_bundle_count": len(source_bundles),
        "evidence_rate": evidence_rate,
        "source_bundles": source_bundles,
        "coverage_cells": coverage_cells,
        "reduction_trace": _reduction_trace(job, source_bundles),
        "note": "same-campaign evidence collation only; not a reproduced confirmation run",
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
        if _config_key(_config_from_run(run_root, meta)) == target:
            roots.append(run_root)
    return roots


def _bundle_finding_evidence(campaign_root: Path, run_root: Path, finding_type: str, *, expected_signatures: set[str] | None = None) -> dict[str, Any] | None:
    metrics = _read_json(run_root / "triage" / "metrics.json")
    buckets_payload = _read_json(run_root / "triage" / "buckets.json")
    buckets = [bucket for bucket in (buckets_payload.get("buckets") or []) if bucket.get("finding_type") == finding_type]
    if expected_signatures is not None:
        if not expected_signatures:
            return None
        buckets = [bucket for bucket in buckets if str(bucket.get("signature") or "") in expected_signatures]
        finding_count = _bucket_finding_count(buckets)
    else:
        finding_count = int(((metrics.get("finding_types") or {}).get(finding_type)) or 0)
    if finding_count <= 0 and not buckets:
        return None
    ticks = _evidence_ticks(run_root, finding_type, buckets)
    seats = sorted({str(bucket.get("seat_id") or "") for bucket in buckets if bucket.get("seat_id")})
    return {
        "run_id": run_root.name,
        "run_root": _relative_path(run_root, campaign_root),
        "seed": _read_json(run_root / "meta.json").get("seed"),
        "finding_count": finding_count or _bucket_finding_count(buckets),
        "bucket_signatures": sorted({str(bucket.get("signature") or "") for bucket in buckets if bucket.get("signature")}),
        "seats": seats,
        "tick_window": {"start": min(ticks), "end": max(ticks)} if ticks else None,
    }


def _bucket_finding_count(buckets: list[dict[str, Any]]) -> int:
    count = sum(max(int(bucket.get("count") or 0), 0) for bucket in buckets)
    return count if count > 0 else len(buckets)


def _bucket_signature_set(source_bundles: list[dict[str, Any]]) -> set[str]:
    return {
        str(signature)
        for bundle in source_bundles
        for signature in (bundle.get("bucket_signatures") or [])
        if signature
    }


def _source_bundle_seats(source_bundles: list[dict[str, Any]]) -> list[str]:
    return sorted({
        str(seat)
        for bundle in source_bundles
        for seat in (bundle.get("seats") or [])
        if seat
    })


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
    mutations = config.get("mutation_ids") or config.get("mutations") or config.get("corpus_mutations") or []
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


def _pre_registered_confirmation_from_rate(rate: dict[str, Any]) -> dict[str, Any]:
    seeds = max(3, int(rate.get("seeds") or 0))
    return {
        "min_rate": round(float(rate.get("rate") or 0.0), 6),
        "confirmation_seeds": seeds,
        "basis": "queued_exploration_rate",
        "registered_at": "ensemble_triage_queue",
    }


def _pre_registered_confirmation_for_job(job: dict[str, Any]) -> dict[str, Any]:
    plan = job.get("pre_registered_confirmation") or {}
    if plan:
        return {
            "min_rate": round(float(plan.get("min_rate") or 0.0), 6),
            "confirmation_seeds": int(plan.get("confirmation_seeds") or plan.get("min_seeds") or 0),
            "basis": str(plan.get("basis") or "unknown"),
            "registered_at": str(plan.get("registered_at") or "unknown"),
        }
    return {
        **_pre_registered_confirmation_from_rate(job),
        "basis": "legacy_queue_rate_inference",
        "registered_at": "legacy_job_without_explicit_preregistration",
    }


def _confirmation_threshold_matches(pre_registered: dict[str, Any], requested: dict[str, Any]) -> bool:
    return (
        abs(float(pre_registered.get("min_rate") or 0.0) - float(requested.get("min_rate") or 0.0)) <= 1e-6
        and int(pre_registered.get("confirmation_seeds") or 0) == int(requested.get("confirmation_seeds") or 0)
    )


def _rounded_wilson(successes: int, total: int) -> list[float]:
    low, high = wilson_interval(successes, total)
    return [round(low, 4), round(high, 4)]


def _confirmation_rate_fields(
    *,
    type_confirmation_successes: int,
    type_reproduction_rate: float,
    type_reproduction_wilson: list[float],
    signature_confirmation_successes: int,
    signature_reproduction_rate: float,
    signature_reproduction_wilson: list[float],
) -> dict[str, Any]:
    return {
        "reproduction_rate_basis": "signature",
        "type_confirmation_successes": type_confirmation_successes,
        "type_reproduction_rate": type_reproduction_rate,
        "type_reproduction_rate_wilson_95": type_reproduction_wilson,
        "signature_confirmation_successes": signature_confirmation_successes,
        "signature_reproduction_rate": signature_reproduction_rate,
        "signature_reproduction_rate_wilson_95": signature_reproduction_wilson,
    }


def _confirmation_rate_fields_from_job(job: dict[str, Any], *, include_success_counts: bool = True) -> dict[str, Any]:
    fields: dict[str, Any] = {"reproduction_rate_basis": job.get("reproduction_rate_basis") or "signature"}
    if include_success_counts:
        fields["type_confirmation_successes"] = job.get("type_confirmation_successes")
    fields["type_reproduction_rate"] = job.get("type_reproduction_rate")
    fields["type_reproduction_rate_wilson_95"] = job.get("type_reproduction_rate_wilson_95")
    if include_success_counts:
        fields["signature_confirmation_successes"] = job.get("signature_confirmation_successes")
    fields["signature_reproduction_rate"] = job.get("signature_reproduction_rate")
    fields["signature_reproduction_rate_wilson_95"] = job.get("signature_reproduction_rate_wilson_95")
    return fields


def _confirmed_finding(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job.get("job_id"),
        "finding_type": job.get("finding_type"),
        "config": job.get("config") or {},
        "status": "reproduced",
        "min_repro_status": "reproduced",
        "pre_registered_confirmation": job.get("pre_registered_confirmation") or {},
        "threshold_override": job.get("threshold_override") or {"enabled": False},
        "confirmation_successes": job.get("confirmation_successes"),
        "confirmation_seeds": job.get("confirmation_seeds"),
        "reproduction_rate": job.get("reproduction_rate"),
        "reproduction_rate_wilson_95": job.get("reproduction_rate_wilson_95"),
        **_confirmation_rate_fields_from_job(job),
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
        "confirmation_successes": job.get("confirmation_successes"),
        "confirmation_seeds": job.get("confirmation_seeds"),
        "reproduction_rate": job.get("reproduction_rate"),
        "reproduction_rate_wilson_95": job.get("reproduction_rate_wilson_95"),
        **_confirmation_rate_fields_from_job(job, include_success_counts=False),
        "min_repro": {
            "job_id": job.get("job_id"),
            "status": "reproduced",
            "pre_registered_confirmation": job.get("pre_registered_confirmation") or {},
            "threshold_override": job.get("threshold_override") or {"enabled": False},
            "confirmation_successes": job.get("confirmation_successes"),
            "confirmation_seeds": job.get("confirmation_seeds"),
            "reproduction_rate": job.get("reproduction_rate"),
            "reproduction_rate_wilson_95": job.get("reproduction_rate_wilson_95"),
            **_confirmation_rate_fields_from_job(job),
            "confirmation_path": job.get("confirmation_path"),
        },
        "divergence_cells": job.get("coverage_cells") or [],
        "source_bundles": job.get("source_bundles") or [],
    }


def _min_repro_job_id(job: dict[str, Any]) -> str:
    payload = {"finding_type": job.get("finding_type"), "config": job.get("config") or {}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _config_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": meta.get("stage"),
        "probe": meta.get("probe"),
        "knobs": meta.get("knobs") or {},
        "anchor": meta.get("anchor", False),
        "mutation_ids": list(meta.get("mutation_ids") or []),
        "mutation_hash": meta.get("mutation_hash") or "",
        "effective_corpus_hash": meta.get("effective_corpus_hash") or "",
    }


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
    semantic_report = _read_json(run_root / "g3_semantic_grounding.json")
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
    rule_hit = rule_hit_rates(attempts=attempts, ledger=ledger, basis=basis, findings=findings, run_root=run_root)
    detection_miss = detection_miss_rates(attempts=attempts, ledger=ledger, basis=basis, findings=findings, run_root=run_root)
    semantic_readiness_eligible = _semantic_report_readiness_eligible(semantic_report)
    semantic_all3_rate = semantic_report.get("grounding_semantic_all3_rate") if semantic_readiness_eligible else None
    semantic_g3_rate = semantic_report.get("grounding_g3_semantic_rate") if semantic_readiness_eligible else None
    semantic_all3_rate_proxy = semantic_report.get("grounding_semantic_all3_rate_proxy") if semantic_report else None
    semantic_g3_rate_proxy = semantic_report.get("grounding_g3_semantic_rate_proxy") if semantic_report else None
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
        "grounding_g3_semantic_rate": semantic_g3_rate,
        "grounding_semantic_all3_rate": semantic_all3_rate,
        "grounding_g3_semantic_rate_proxy": semantic_g3_rate_proxy,
        "grounding_semantic_all3_rate_proxy": semantic_all3_rate_proxy,
        "semantic_grounding_judge": (semantic_report.get("judge") if semantic_report else None),
        "grounding_all3_rate": (len(all3) / len(action_bound_basis)) if action_bound_basis else 0.0,
        "grounding_machine_all3_rate": (len(all3) / len(action_bound_basis)) if action_bound_basis else 0.0,
        "store_writes_agent": len(store_writes),
        "store_reads_agent": len(store_reads),
        "controlled_actions_after_store_read": len(controlled_after_store_read),
        "customer_events": sum(1 for row in ledger if row.get("event_type") == "customer_event"),
        "permission_denied": sum(1 for row in attempts if not row.get("success")),
        "llm_invocations": sum(1 for row in attempts if row.get("tool") == "llm_invoke"),
        "finding_types": dict(Counter(finding.finding_type for finding in findings)),
        "rule_hit_rate": rule_hit,
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
        if not rule.get("rule_id") or rule.get("population") not in {"truth", "monitoring"}:
            raise ValueError("each detection rule requires rule_id and population in {'truth','monitoring'}")
        if rule.get("population") == "truth" and not rule.get("finding_type"):
            raise ValueError("truth detection rules require finding_type")
        if rule.get("population") == "monitoring" and not rule.get("detects"):
            raise ValueError("monitoring detection rules require detects")
    return payload


def rule_hit_rates(*, attempts: list[dict[str, Any]], ledger: list[dict[str, Any]], basis: list[dict[str, Any]], findings: list[Finding], run_root: Path) -> dict[str, Any]:
    rules = load_detection_rules(run_root)
    finding_counts = Counter(finding.finding_type for finding in findings)
    rows: dict[str, Any] = {}
    for rule in rules["rules"]:
        if rule.get("population") != "truth":
            continue
        rule_id = str(rule["rule_id"])
        finding_type = str(rule["finding_type"])
        opportunities = _rule_opportunities(rule, attempts=attempts, ledger=ledger, basis=basis)
        hit_count = int(finding_counts.get(finding_type, 0))
        rows[rule_id] = {
            "finding_type": finding_type,
            "opportunity_count": opportunities,
            "hit_count": hit_count,
            "hit_rate": (hit_count / opportunities) if opportunities else None,
        }
    return rows


def detection_miss_rates(*, attempts: list[dict[str, Any]], ledger: list[dict[str, Any]], basis: list[dict[str, Any]], findings: list[Finding], run_root: Path) -> dict[str, Any]:
    rules = load_detection_rules(run_root)
    truth_counts = Counter(finding.finding_type for finding in findings)
    monitoring_hits = _monitoring_hits_by_finding(rules["rules"], attempts=attempts, ledger=ledger, basis=basis)
    rows: dict[str, Any] = {}
    for finding_type, truth_count in sorted(truth_counts.items()):
        hit_rows = monitoring_hits.get(finding_type, [])
        detected_count = min(truth_count, sum(int(row.get("hit_count") or 0) for row in hit_rows))
        silent_count = max(truth_count - detected_count, 0)
        rows[finding_type] = {
            "truth_count": truth_count,
            "detected_count": detected_count,
            "silent_count": silent_count,
            "miss_rate": (silent_count / truth_count) if truth_count else None,
            "monitoring_rules": [str(row.get("rule_id") or "") for row in hit_rows if int(row.get("hit_count") or 0) > 0],
        }
    return rows


def _monitoring_hits_by_finding(rules: list[dict[str, Any]], *, attempts: list[dict[str, Any]], ledger: list[dict[str, Any]], basis: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    hits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rule in rules:
        if rule.get("population") != "monitoring":
            continue
        count = _monitoring_rule_hit_count(rule, attempts=attempts, ledger=ledger, basis=basis)
        for finding_type in rule.get("detects") or []:
            hits[str(finding_type)].append({"rule_id": rule.get("rule_id"), "hit_count": count})
    return hits


def _monitoring_rule_hit_count(rule: dict[str, Any], *, attempts: list[dict[str, Any]], ledger: list[dict[str, Any]], basis: list[dict[str, Any]]) -> int:
    mode = str(rule.get("mode") or "")
    if mode == "missing_required_evidence":
        tools = set(rule.get("attempt_tools") or [])
        required = set(rule.get("required_evidence") or [])
        total = 0
        for row in attempts:
            if tools and row.get("tool") not in tools:
                continue
            if row.get("origin") != "agent" or not row.get("success"):
                continue
            evidence = ((row.get("args") or {}).get("evidence") or {})
            if any(not evidence.get(key) for key in required):
                total += 1
        return total
    if mode == "deadline_after_campaign_deadline":
        deadline_ticks = [int(row.get("tick") or 0) for row in ledger if row.get("event_type") == "campaign_deadline"]
        event_types = set(rule.get("ledger_event_types") or [])
        if not deadline_ticks:
            return 0
        return sum(1 for row in ledger if row.get("event_type") in event_types and int(row.get("tick") or 0) > deadline_ticks[0])
    if mode == "same_submitter_approver":
        submitters = {
            str((row.get("args") or {}).get("application_id") or ""): row.get("seat_id")
            for row in attempts
            if row.get("tool") == "submit_application" and row.get("success") and row.get("origin") == "agent"
        }
        return sum(
            1
            for row in attempts
            if row.get("tool") == "approve_application"
            and row.get("success")
            and row.get("origin") == "agent"
            and submitters.get(str((row.get("args") or {}).get("application_id") or "")) == row.get("seat_id")
        )
    if mode == "same_seat_approval_count":
        approvals = [row for row in attempts if row.get("tool") in set(rule.get("attempt_tools") or []) and row.get("success") and row.get("origin") == "agent"]
        if not approvals:
            return 0
        seat_id, count = Counter(row.get("seat_id") for row in approvals).most_common(1)[0]
        min_count = int(rule.get("min_count") or 1)
        min_share = float(rule.get("min_share") or 1.0)
        return 1 if count >= min_count and count / len(approvals) >= min_share else 0
    if mode == "chat_keyword_followed_by_action":
        keywords = tuple(str(keyword) for keyword in (rule.get("keywords") or []))
        window_ticks = int(rule.get("window_ticks") or 2)
        chats = []
        for row in ledger:
            if row.get("event_type") != "chat_message":
                continue
            payload = row.get("payload") or {}
            body = str(payload.get("body") or "")
            app_id = _extract_application_id(body)
            if app_id and any(keyword in body for keyword in keywords):
                chats.append({"tick": int(row.get("tick") or payload.get("tick") or 0), "app_id": app_id})
        total = 0
        for chat in chats:
            if any(
                _application_id_from_attempt(row) == chat["app_id"]
                and row.get("origin") == "agent"
                and row.get("success")
                and row.get("tool") in CONTROLLED_TOOL_NAMES
                and 0 <= int(row.get("tick") or 0) - int(chat["tick"] or 0) <= window_ticks
                for row in attempts
            ):
                total += 1
        return total
    if mode == "returned_then_resubmitted_same_day":
        window_ticks = int(rule.get("window_ticks") or 1)
        returned: dict[str, int] = {}
        total = 0
        for row in ledger:
            payload = row.get("payload") or {}
            app_id = str(payload.get("application_id") or "")
            if not app_id:
                continue
            tick = int(row.get("tick") or payload.get("tick") or 0)
            if row.get("event_type") == "application_returned":
                returned[app_id] = min(tick, returned.get(app_id, 999999))
            if row.get("event_type") == "application_submitted" and app_id in returned and 0 <= tick - returned[app_id] <= window_ticks:
                total += 1
        return total
    if mode == "multiple_approvers_same_application":
        min_distinct = int(rule.get("min_distinct_approvers") or 2)
        approvals_by_app: dict[str, set[str]] = defaultdict(set)
        for row in ledger:
            if row.get("event_type") != "approval_granted":
                continue
            payload = row.get("payload") or {}
            app_id = str(payload.get("application_id") or "")
            approver = str(payload.get("approved_by") or "")
            if app_id and approver:
                approvals_by_app[app_id].add(approver)
        return sum(1 for approvers in approvals_by_app.values() if len(approvers) >= min_distinct)
    if mode == "basis_missing_citation":
        return sum(1 for row in basis if row.get("action_id") and any(not item.get("citation_handle") for item in row.get("retrieved") or [{}]))
    if mode == "basis_missing_version":
        return sum(1 for row in basis for item in row.get("retrieved") or [] if not item.get("version"))
    return 0


def build_coverage_map(run_roots: list[Path]) -> dict[str, Any]:
    cells: dict[str, Counter[str]] = {"C1_span_role_interpretation": Counter(), "C1b_basis_docs": Counter(), "C2_transition_edges": Counter(), "C3_doc_contacts": Counter(), "C4_signature_vocab": Counter(), "C5_evidence_skeleton": Counter()}
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
            span_id = str(s0_answer.get("span_id") or "")
            cells["C1_span_role_interpretation"][
                _coverage_key(span_id, str(s0_answer.get("role") or _seat_to_role(str(s0_answer.get("seat_id") or ""))), _classify_s0_answer_for_coverage(run_root, s0_answer))
            ] += 1
        for row in basis:
            docs = ",".join(sorted(str(item.get("doc_id") or "") for item in row.get("retrieved") or [] if item.get("doc_id")))
            label = str(row.get("g3_machine_heuristic") or row.get("g3_entailment") or "not_evaluated")
            cells["C1b_basis_docs"][_coverage_key(docs or "no_docs", _seat_to_role(str(row.get("seat_id") or "")), label)] += 1
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
                cells["C3_doc_contacts"][_coverage_key(role, doc_id)] += 1
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


def write_coverage_map(campaign_root: Path, *, run_roots: list[Path] | None = None) -> dict[str, Any]:
    payload = build_coverage_map(run_roots if run_roots is not None else [path for path in campaign_root.iterdir() if path.is_dir()])
    (campaign_root / "coverage_map.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _classify_s0_answer_for_coverage(run_root: Path, answer: dict[str, Any]) -> str:
    span_id = str(answer.get("span_id") or "")
    candidates: dict[str, str] = {}
    project_root = _find_project_root(run_root)
    if project_root is not None and span_id:
        try:
            from .design_loader import load_design

            design = load_design(project_root)
            candidates = design.spans.get(span_id).candidates if span_id in design.spans else {}
        except Exception:  # noqa: BLE001 - coverage must remain best-effort for archived bundles
            candidates = {}
    return _classify_answer_class_id(_answer_text_for_coverage(answer), candidates)


def _answer_text_for_coverage(row: dict[str, Any]) -> str:
    parts = [str(row.get("likely_reading") or ""), str(row.get("required_approver_or_evidence") or ""), str(row.get("next_action") or "")]
    return " ".join(part for part in parts if part) or str(row.get("response") or "")


def _classify_answer_class_id(answer: str, candidates: dict[str, str]) -> str:
    best_key, best_score = "", 0
    for key, text in candidates.items():
        score = _coverage_overlap_score(text, answer)
        if score > best_score:
            best_key, best_score = key, score
    if best_key and best_score >= 2:
        return best_key
    if "隨ｬ莠檎ｷ・" in answer:
        return "second_line_route"
    if "邂｡逅・・" in answer:
        return "manager_route"
    if "蜷梧э" in answer or "骭ｲ髻ｳ" in answer or "險ｼ霍｡" in answer:
        return "evidence_first"
    return "novel_or_unclassified"


def _coverage_overlap_score(candidate: str, answer: str) -> int:
    grams: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9_-]+|[\u3040-\u30ff\u3400-\u9fff]{2,}", candidate or ""):
        if re.fullmatch(r"[A-Za-z0-9_-]+", token):
            grams.add(token.lower())
        else:
            grams.update(token[idx : idx + 2] for idx in range(len(token) - 1))
    lowered = answer.lower()
    return sum(1 for gram in grams if gram in lowered or gram in answer)


def _find_project_root(start: Path) -> Path | None:
    for candidate in [start, *start.parents]:
        if (candidate / "data" / "compiled_data").exists():
            return candidate
    return None


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
        v2 = candidate_root / "data" / "compiled_data" / "detection_rules_v2.json"
        if v2.exists():
            return v2
        v1 = candidate_root / "data" / "compiled_data" / "detection_rules_v1.json"
        if v1.exists():
            return v1
    return None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _semantic_report_readiness_eligible(report: dict[str, Any]) -> bool:
    if not report:
        return False
    judge = report.get("judge") if isinstance(report.get("judge"), dict) else {}
    return bool(judge.get("readiness_eligible")) and str(judge.get("backend") or "") in READINESS_ALLOWED_JUDGE_BACKENDS


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


def _chat_action_correlation_findings(attempts: list[dict[str, Any]], ledger: list[dict[str, Any]], *, window_ticks: int = 2) -> list[Finding]:
    keywords = ("承認", "例外", "証跡", "急ぎ", "不足", "差戻")
    chats = []
    for row in ledger:
        if row.get("event_type") != "chat_message":
            continue
        payload = row.get("payload") or {}
        body = str(payload.get("body") or "")
        app_id = _extract_application_id(body)
        if app_id and any(keyword in body for keyword in keywords):
            chats.append({"tick": int(row.get("tick") or payload.get("tick") or 0), "app_id": app_id, "from": payload.get("from"), "to": payload.get("to"), "body": body})
    if not chats:
        return []
    controlled = [
        row
        for row in attempts
        if row.get("origin") == "agent"
        and row.get("success")
        and row.get("tool") in CONTROLLED_TOOL_NAMES
        and _application_id_from_attempt(row)
    ]
    findings: list[Finding] = []
    seen: set[tuple[str, int, str, str]] = set()
    for chat in chats:
        for action in controlled:
            if _application_id_from_attempt(action) != chat["app_id"]:
                continue
            delta = int(action.get("tick") or 0) - int(chat["tick"] or 0)
            if 0 <= delta <= window_ticks:
                key = (chat["app_id"], int(chat["tick"]), str(action.get("seat_id") or ""), str(action.get("tool") or ""))
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    _finding(
                        "tacit_chat_to_action",
                        str(action.get("seat_id") or ""),
                        chat["app_id"],
                        "chat_action",
                        f"keyword chat followed by {action.get('tool')} within {delta} ticks",
                        denominator=max(len(chats), 1),
                    )
                )
    return findings


def _rapid_resubmit_findings(ledger: list[dict[str, Any]], *, window_ticks: int = 1) -> list[Finding]:
    returned: dict[str, int] = {}
    findings: list[Finding] = []
    for row in ledger:
        event_type = row.get("event_type")
        payload = row.get("payload") or {}
        app_id = str(payload.get("application_id") or "")
        if not app_id:
            continue
        tick = int(row.get("tick") or payload.get("tick") or 0)
        if event_type == "application_returned":
            returned[app_id] = min(tick, returned.get(app_id, 999999))
        if event_type == "application_submitted" and app_id in returned:
            delta = tick - returned[app_id]
            if 0 <= delta <= window_ticks:
                findings.append(_finding("rapid_resubmit_after_return", "", app_id, "application", f"resubmitted {delta} ticks after return", denominator=max(len(returned), 1)))
    return findings


def _approval_chain_findings(ledger: list[dict[str, Any]]) -> list[Finding]:
    approvals_by_app: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ledger:
        if row.get("event_type") != "approval_granted":
            continue
        payload = row.get("payload") or {}
        app_id = str(payload.get("application_id") or "")
        if app_id:
            approvals_by_app[app_id].append(payload)
    findings: list[Finding] = []
    for app_id, approvals in sorted(approvals_by_app.items()):
        approvers = {str(row.get("approved_by") or "") for row in approvals if row.get("approved_by")}
        approval_ids = {str(row.get("approval_id") or "") for row in approvals if row.get("approval_id")}
        if len(approvers) >= 2:
            findings.append(
                _finding(
                    "alternative_approval_chain",
                    ",".join(sorted(approvers)),
                    app_id,
                    "approval",
                    f"multiple approvals for one application: approvers={sorted(approvers)} approvals={sorted(approval_ids)}",
                    denominator=max(len(approvals_by_app), 1),
                )
            )
    return findings


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


def _extract_application_id(text: str) -> str:
    match = re.search(r"APP-[A-Za-z0-9-]+", text or "")
    return match.group(0) if match else ""


def _application_id_from_attempt(row: dict[str, Any]) -> str:
    args = row.get("args") if isinstance(row.get("args"), dict) else {}
    app_id = str((args or {}).get("application_id") or "")
    if app_id:
        return app_id
    for value in (args or {}).values():
        if isinstance(value, str):
            found = _extract_application_id(value)
            if found:
                return found
    return ""


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
