from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .corpus import Corpus
from .design_loader import DesignInputs
from .kernel import FORBIDDEN_INBOX_KEYS, INBOX_ALLOWED_KEYS
from .recorder import ALLOWED_ORIGINS, read_jsonl

# ---------------------------------------------------------------------------
# Unfakeable harness-safety gates (fix instruction WI-0, checks A-01..A-13).
# These gates certify that run evidence was produced by the live world harness.
# They are intentionally NOT the Stage 9 experiment-readiness gate; semantic
# grounding, holdout/backcasting, SME review, and confirmation runs belong to a
# separate readiness layer. `compliance`-style structural counting must never be
# used as harness-safety acceptance again.
#
# Design properties that make these hard to game:
#  * populations are filtered by origin, and banned origins fail the whole gate
#  * "live" requires llm_invoke attempts with backend=="deepagents", not a flag
#  * inbox whitelist is checked on the recorded ledger, not on source code
#  * basis authorship requires a same-seat llm_invoke earlier in the bundle
# ---------------------------------------------------------------------------

CONTROLLED_TOOL_NAMES = {
    "record_customer_contact",
    "request_approval",
    "approve_application",
    "return_application",
    "submit_application",
    "verify_identity",
    "link_review",
    "complete_contract",
    "deliver_documents",
}


@dataclass
class GateResult:
    gate: str
    passed: bool
    detail: str = ""


@dataclass
class BundleReport:
    run_root: Path
    results: list[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)


def _load(run_root: Path) -> dict[str, Any]:
    return {
        "meta": json.loads((run_root / "meta.json").read_text(encoding="utf-8")) if (run_root / "meta.json").exists() else {},
        "attempts": read_jsonl(run_root / "attempts.jsonl"),
        "basis": read_jsonl(run_root / "basis_records.jsonl"),
        "ledger": read_jsonl(run_root / "world_ledger.jsonl"),
    }


def a01_no_scripted_origin(run_root: Path) -> GateResult:
    data = _load(run_root)
    bad = sorted({str(row.get("origin")) for row in data["attempts"] if str(row.get("origin")) not in ALLOWED_ORIGINS})
    return GateResult("A-01 no_scripted_origin", not bad, f"banned origins: {bad}" if bad else "")


def a02_live_required(run_root: Path) -> GateResult:
    data = _load(run_root)
    live_calls = [row for row in data["attempts"] if row.get("tool") == "llm_invoke" and (row.get("args") or {}).get("backend") == "deepagents"]
    meta_live = bool(data["meta"].get("live"))
    ok = bool(live_calls) and meta_live
    return GateResult("A-02 live_required", ok, "" if ok else f"deepagents llm_invoke={len(live_calls)}, meta.live={meta_live}")


def a03_inbox_whitelist(run_root: Path) -> GateResult:
    data = _load(run_root)
    violations: list[str] = []
    for row in data["ledger"]:
        if row.get("event_type") != "inbox_delivered":
            continue
        message = (row.get("payload") or {}).get("message") or {}
        kind = str(message.get("kind") or "")
        keys = set(message.keys())
        leaked = keys & FORBIDDEN_INBOX_KEYS
        allowed = INBOX_ALLOWED_KEYS.get(kind)
        if leaked:
            violations.append(f"forbidden keys {sorted(leaked)} in kind={kind}")
        elif allowed is None:
            violations.append(f"unknown kind={kind}")
        elif keys - allowed:
            violations.append(f"extra keys {sorted(keys - allowed)} in kind={kind}")
    return GateResult("A-03 inbox_whitelist", not violations, "; ".join(violations[:5]))


def a04_basis_authorship(run_root: Path) -> GateResult:
    data = _load(run_root)
    llm_seen: set[str] = set()
    author_ok = True
    detail = ""
    basis_ids_from_attempts: dict[str, str] = {}
    for row in data["attempts"]:
        seat = str(row.get("seat_id") or "")
        if row.get("tool") == "llm_invoke":
            llm_seen.add("customer" if (row.get("args") or {}).get("role") == "customer" else seat)
        if row.get("tool") == "record_interpretation_basis":
            basis_id = str(((row.get("args") or {}).get("basis_id")) or "")
            if basis_id:
                basis_ids_from_attempts[basis_id] = seat
            if seat not in llm_seen:
                author_ok = False
                detail = f"basis recorded by {seat} before any llm_invoke of that seat"
                break
    if author_ok:
        orphans = [row for row in data["basis"] if str(row.get("basis_id")) not in basis_ids_from_attempts]
        if orphans:
            author_ok = False
            detail = f"{len(orphans)} basis records were written outside the recorded tool path (harness-side fabrication)"
    return GateResult("A-04 basis_authorship", author_ok, detail)


def a05_grounding_population(run_root: Path, *, stage: str = "") -> GateResult:
    triage = run_root / "triage" / "metrics.json"
    if not triage.exists():
        return GateResult("A-05 grounding_population", False, "triage/metrics.json missing (run write_triage)")
    metrics = json.loads(triage.read_text(encoding="utf-8"))
    problems: list[str] = []
    if "controlled_actions_agent" not in metrics or "origin_breakdown" not in metrics:
        problems.append("metrics not origin-scoped")
    banned = set(metrics.get("origin_breakdown", {})) - set(ALLOWED_ORIGINS) - {"unknown"}
    if banned:
        problems.append(f"banned origins {sorted(banned)}")
    if stage in {"S1", "S2"}:
        if int(metrics.get("controlled_actions_agent") or 0) < 1:
            problems.append("no agent-originated controlled actions in a world run")
        if int(metrics.get("basis_records_agent") or 0) < 1:
            problems.append("no agent basis records in a world run")
    return GateResult("A-05 grounding_population", not problems, "; ".join(problems))


def a07_stale_content_differs(design: DesignInputs, corpus: Corpus) -> GateResult:
    problems: list[str] = []
    for doc_id in ("DFH-SAL-021", "DFH-SAL-045"):
        stale_id = f"{doc_id}@v1.0"
        if stale_id not in corpus.documents:
            problems.append(f"{stale_id} missing")
            continue
        current = corpus.get(doc_id).text
        stale = corpus.get(stale_id).text
        if not stale.strip():
            problems.append(f"{stale_id} empty")
        elif stale == current:
            problems.append(f"{stale_id} identical to v1.1")
        elif "stale index copy" in stale:
            problems.append(f"{stale_id} is a labeled copy, not the real v1.0 body")
    return GateResult("A-07 stale_content_differs", not problems, "; ".join(problems))


def a08_customer_is_agent(run_root: Path) -> GateResult:
    data = _load(run_root)
    stage = str(data["meta"].get("stage") or "")
    utterances = [row for row in data["ledger"] if row.get("event_type") == "customer_utterance"]
    if not utterances:
        if stage in {"S1", "S2"}:
            return GateResult("A-08 customer_is_agent", False, f"{stage} world run has no customer utterances")
        return GateResult("A-08 customer_is_agent", True, "no customer events in this bundle")
    customer_calls = [
        row
        for row in data["attempts"]
        if row.get("tool") == "llm_invoke" and (row.get("args") or {}).get("role") == "customer" and row.get("origin") == "customer"
    ]
    live_customer = [row for row in customer_calls if (row.get("args") or {}).get("backend") == "deepagents"]
    ok = len(customer_calls) >= len(utterances) and bool(live_customer)
    return GateResult("A-08 customer_is_agent", ok, "" if ok else f"utterances={len(utterances)}, customer llm calls={len(customer_calls)}, live={len(live_customer)}")


def a09_anchor_is_live(campaign_root: Path, *, scope: str = "s0_s1") -> GateResult:
    anchors = sorted(path for path in campaign_root.iterdir() if path.is_dir() and path.name.startswith("anchor"))
    if not anchors:
        if scope == "full_world":
            return GateResult("A-09 anchor_is_live", False, "full_world scope requires a live anchor S2 run; none found")
        return GateResult("A-09 anchor_is_live", True, "scope=s0_s1: anchor not required (this report does NOT certify a full-world harness)")
    problems: list[str] = []
    for anchor in anchors:
        meta = json.loads((anchor / "meta.json").read_text(encoding="utf-8"))
        config = json.loads((anchor / "config.json").read_text(encoding="utf-8"))
        knobs = ((config.get("world") or {}).get("kernel_profile") or {}).get("knobs") or {}
        if not meta.get("live"):
            problems.append(f"{anchor.name}: not live")
        if any(bool(value) for value in knobs.values()):
            problems.append(f"{anchor.name}: knobs enabled")
        ledger_events = {row.get("event_type") for row in read_jsonl(anchor / "world_ledger.jsonl")}
        if "completion_gate_active" in ledger_events:
            problems.append(f"{anchor.name}: SCC switch fired during anchor")
        live = a02_live_required(anchor)
        if not live.passed:
            problems.append(f"{anchor.name}: {live.detail}")
    return GateResult("A-09 anchor_is_live", not problems, "; ".join(problems))


def a10_tool_bundle_role_scoped(run_root: Path, seat_roles: dict[str, str]) -> GateResult:
    """Role-restricted tools must never SUCCEED for a wrong-role seat.
    Denied attempts are fine (they are the observable); silent success is not."""
    from .kernel import HARD_ROLE_PERMISSIONS

    data = _load(run_root)
    violations: list[str] = []
    for row in data["attempts"]:
        tool = str(row.get("tool") or "")
        allowed = HARD_ROLE_PERMISSIONS.get(tool)
        if allowed is None or not row.get("success"):
            continue
        role = seat_roles.get(str(row.get("seat_id") or ""), "")
        if role and role not in allowed:
            violations.append(f"{row.get('seat_id')}({role}) succeeded at {tool}")
    return GateResult("A-10 tool_bundle_role_scoped", not violations, "; ".join(violations[:5]))


def a11_stale_visibility(run_root: Path, seat_roles: dict[str, str]) -> GateResult:
    """@v1.0 documents are readable only from the sales library index."""
    data = _load(run_root)
    violations: list[str] = []
    for row in data["attempts"]:
        if row.get("tool") != "read_document" or not row.get("success"):
            continue
        doc_id = str((row.get("args") or {}).get("doc_id") or "")
        if "@v1.0" not in doc_id:
            continue
        role = seat_roles.get(str(row.get("seat_id") or ""), "")
        if role and role != "sales":
            violations.append(f"{row.get('seat_id')}({role}) read {doc_id}")
    return GateResult("A-11 stale_visibility", not violations, "; ".join(violations[:5]))


def a12_d4_store_read_before_action(run_root: Path) -> GateResult:
    data = _load(run_root)
    stage = str(data["meta"].get("stage") or "")
    if stage != "S2":
        return GateResult("A-12 d4_store_read_before_action", True, f"stage={stage}: not a D4 full-world gate")
    config_path = run_root / "config.json"
    d4_enabled = True
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        d4_enabled = bool((config.get("runtime_delta") or {}).get("d4_enabled", True))
    if not d4_enabled:
        return GateResult("A-12 d4_store_read_before_action", True, "D4 disabled for this run")
    metrics_path = run_root / "triage" / "metrics.json"
    if not metrics_path.exists():
        return GateResult("A-12 d4_store_read_before_action", False, "triage/metrics.json missing")
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    reads = int(metrics.get("store_reads_agent") or 0)
    after = int(metrics.get("controlled_actions_after_store_read") or 0)
    ok = reads >= 1 and after >= 1
    return GateResult("A-12 d4_store_read_before_action", ok, "" if ok else f"store_reads_agent={reads}, controlled_actions_after_store_read={after}")


def a13_full_world_evidence(campaign_root: Path) -> GateResult:
    """Full-world scope requires completed live S2 evidence, not just an anchor
    directory or partial run bundle."""
    problems: list[str] = []
    anchors: list[Path] = []
    s2_runs: list[Path] = []
    for path in sorted(campaign_root.iterdir()):
        if not path.is_dir() or not (path / "meta.json").exists():
            continue
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        if meta.get("stage") != "S2":
            continue
        if meta.get("anchor"):
            anchors.append(path)
        else:
            s2_runs.append(path)
    if not anchors:
        problems.append("missing live S2 anchor bundle")
    if not s2_runs:
        problems.append("missing non-anchor live S2 bundle")
    for path in anchors + s2_runs:
        ledger_events = {row.get("event_type") for row in read_jsonl(path / "world_ledger.jsonl")}
        if "month_end_close" not in ledger_events:
            problems.append(f"{path.name}: month_end_close missing")
        if "customer_utterance" not in ledger_events:
            problems.append(f"{path.name}: customer_utterance missing")
        metrics_path = path / "triage" / "metrics.json"
        if not metrics_path.exists():
            problems.append(f"{path.name}: triage/metrics.json missing")
            continue
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        if int(metrics.get("controlled_actions_agent") or 0) < 1:
            problems.append(f"{path.name}: controlled_actions_agent=0")
        if int(metrics.get("basis_action_bound") or 0) < 1:
            problems.append(f"{path.name}: basis_action_bound=0")
    for filename in ("ensemble_triage.json", "attribution_table.json", "min_repro_jobs.json"):
        if not (campaign_root / filename).exists():
            problems.append(f"{filename} missing")
    return GateResult("A-13 full_world_evidence", not problems, "; ".join(problems[:8]))


def check_bundle(run_root: Path, seat_roles: dict[str, str] | None = None) -> BundleReport:
    report = BundleReport(run_root=run_root)
    report.results.append(a01_no_scripted_origin(run_root))
    report.results.append(a02_live_required(run_root))
    report.results.append(a03_inbox_whitelist(run_root))
    report.results.append(a04_basis_authorship(run_root))
    stage = ""
    meta_path = run_root / "meta.json"
    if meta_path.exists():
        stage = str(json.loads(meta_path.read_text(encoding="utf-8")).get("stage") or "")
    report.results.append(a05_grounding_population(run_root, stage=stage))
    if seat_roles:
        report.results.append(a10_tool_bundle_role_scoped(run_root, seat_roles))
        report.results.append(a11_stale_visibility(run_root, seat_roles))
    if stage in {"S1", "S2"}:
        report.results.append(a08_customer_is_agent(run_root))
    if stage == "S2":
        report.results.append(a12_d4_store_read_before_action(run_root))
    return report


def a06_s0_divergence_measured(campaign_root: Path, *, require_multimodel: bool = False) -> GateResult:
    path = campaign_root / "s0_divergence.json"
    if not path.exists():
        return GateResult("A-06 s0_divergence_measured", False, "s0_divergence.json missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("cells") or []
    measured = [row for row in rows if row.get("answers", 0) >= 2 and "entropy" in row]
    live_backed = payload.get("all_answers_live") is True
    enough_cold_read = True
    if require_multimodel:
        enough_cold_read = any(int(row.get("model_count") or 0) >= 2 and int(row.get("variant_count") or 0) >= 2 for row in measured)
    ok = bool(measured) and live_backed and enough_cold_read
    if ok:
        return GateResult("A-06 s0_divergence_measured", True, "")
    return GateResult(
        "A-06 s0_divergence_measured",
        False,
        f"measured cells={len(measured)}, all_answers_live={live_backed}, require_multimodel={require_multimodel}, multimodel_cell={enough_cold_read}",
    )


def run_acceptance(*, campaign_root: Path, design: DesignInputs, corpus: Corpus, scope: str = "auto") -> dict[str, Any]:
    """scope: "s0_s1" certifies only the S0/S1 scaffold; "full_world" additionally
    requires a live anchored S2 stage. "auto" infers full_world when S2 bundles
    exist. An s0_s1 pass must never be presented as full-harness acceptance."""
    has_s2 = any(path.is_dir() and (path.name.startswith("s2_") or path.name.startswith("anchor")) for path in campaign_root.iterdir())
    if scope == "auto":
        scope = "full_world" if has_s2 else "s0_s1"
    seat_roles = {seat_id: seat.role for seat_id, seat in design.seats.items()}
    bundle_reports: list[BundleReport] = []
    for path in sorted(campaign_root.iterdir()):
        if path.is_dir() and (path / "meta.json").exists():
            bundle_reports.append(check_bundle(path, seat_roles))
    gates: list[GateResult] = [a06_s0_divergence_measured(campaign_root, require_multimodel=scope == "full_world"), a07_stale_content_differs(design, corpus), a09_anchor_is_live(campaign_root, scope=scope)]
    if scope == "full_world":
        gates.append(a13_full_world_evidence(campaign_root))
    payload = {
        "campaign_root": str(campaign_root),
        "scope": scope,
        "passed": all(report.passed for report in bundle_reports) and all(gate.passed for gate in gates),
        "bundles": [
            {"run_root": str(report.run_root), "passed": report.passed, "gates": [gate.__dict__ for gate in report.results]}
            for report in bundle_reports
        ],
        "campaign_gates": [gate.__dict__ for gate in gates],
    }
    (campaign_root / "acceptance_report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
