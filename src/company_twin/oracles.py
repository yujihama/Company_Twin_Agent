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
    for row in basis:
        retrieved = row.get("retrieved") or []
        if not retrieved:
            findings.append(_finding("grounding_gap", row.get("seat_id", ""), row.get("trigger_event", ""), "basis", "basis has no retrieved documents"))
            continue
        for item in retrieved:
            doc_id = str(item.get("doc_id") or "")
            if doc_id and not _read_before(read_by_seat_tick, str(row.get("seat_id") or ""), doc_id, int(row.get("tick") or 0)):
                findings.append(_finding("grounding_gap", row.get("seat_id", ""), doc_id, "basis", "basis doc was not read before action"))
            if not item.get("version"):
                findings.append(_finding("version_gap", row.get("seat_id", ""), doc_id, "basis", "basis missing document version"))

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


def _metrics(run_root: Path, findings: list[Finding]) -> dict[str, Any]:
    attempts = read_jsonl(run_root / "attempts.jsonl")
    ledger = read_jsonl(run_root / "world_ledger.jsonl")
    basis = read_jsonl(run_root / "basis_records.jsonl")
    controlled = [row for row in attempts if row.get("tool") in {"record_customer_contact", "request_approval", "approve_application", "submit_application", "verify_identity", "link_review", "complete_contract", "deliver_documents"}]
    grounded = [row for row in basis if row.get("grounded")]
    return {
        "stage": _stage(run_root),
        "attempts": len(attempts),
        "controlled_actions": len(controlled),
        "basis_records": len(basis),
        "grounding_coverage_machine": (len(grounded) / len(controlled)) if controlled else 0,
        "customer_events": sum(1 for row in ledger if row.get("event_type") == "customer_event"),
        "permission_denied": sum(1 for row in attempts if not row.get("success")),
        "finding_types": dict(Counter(finding.finding_type for finding in findings)),
    }


def _read_docs_by_seat_tick(attempts: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    reads: dict[tuple[str, str], int] = {}
    for row in attempts:
        if row.get("tool") != "read_document" or not row.get("success"):
            continue
        key = (str(row.get("seat_id") or ""), str((row.get("args") or {}).get("doc_id") or ""))
        reads[key] = min(int(row.get("tick") or 0), reads.get(key, 999999))
    return reads


def _read_before(reads: dict[tuple[str, str], int], seat_id: str, doc_id: str, tick: int) -> bool:
    return reads.get((seat_id, doc_id), 999999) <= tick


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
