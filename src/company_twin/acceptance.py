from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .corpus import Corpus
from .design_loader import DesignInputs
from .kernel import FORBIDDEN_INBOX_KEYS, INBOX_ALLOWED_KEYS
from .recorder import ALLOWED_ORIGINS, read_jsonl

# ---------------------------------------------------------------------------
# Unfakeable acceptance gates (fix instruction WI-0, checks A-01..A-10).
# These are the ONLY acceptance criteria for the harness. `compliance`-style
# structural counting must never be used as acceptance again.
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

LLM_ATTEMPT_TOOLS = {"llm_invoke", "llm_complete"}


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
        "config": json.loads((run_root / "config.json").read_text(encoding="utf-8")) if (run_root / "config.json").exists() else {},
        "attempts": read_jsonl(run_root / "attempts.jsonl"),
        "basis": read_jsonl(run_root / "basis_records.jsonl"),
        "ledger": read_jsonl(run_root / "world_ledger.jsonl"),
        "store": read_jsonl(run_root / "store_events.jsonl"),
    }


def _seat_configs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return (((config.get("world") or {}).get("population") or {}).get("seats") or {})


def _role_card_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


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


def a05_grounding_population(run_root: Path) -> GateResult:
    triage = run_root / "triage" / "metrics.json"
    if not triage.exists():
        return GateResult("A-05 grounding_population", False, "triage/metrics.json missing (run write_triage)")
    metrics = json.loads(triage.read_text(encoding="utf-8"))
    ok = "controlled_actions_agent" in metrics and "origin_breakdown" in metrics
    banned = set(metrics.get("origin_breakdown", {})) - set(ALLOWED_ORIGINS) - {"unknown"}
    if banned:
        ok = False
    return GateResult("A-05 grounding_population", ok, "" if ok else f"metrics not origin-scoped or banned origins {sorted(banned)}")


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
    utterances = [row for row in data["ledger"] if row.get("event_type") == "customer_utterance"]
    if not utterances:
        return GateResult("A-08 customer_is_agent", True, "no customer events in this bundle")
    customer_calls = [
        row
        for row in data["attempts"]
        if row.get("tool") == "llm_invoke" and (row.get("args") or {}).get("role") == "customer" and row.get("origin") == "customer"
    ]
    live_customer = [row for row in customer_calls if (row.get("args") or {}).get("backend") == "deepagents"]
    ok = len(customer_calls) >= len(utterances) and bool(live_customer)
    return GateResult("A-08 customer_is_agent", ok, "" if ok else f"utterances={len(utterances)}, customer llm calls={len(customer_calls)}, live={len(live_customer)}")


def a09_anchor_is_live(campaign_root: Path, *, require_anchor: bool = False) -> GateResult:
    anchors = sorted(path for path in campaign_root.iterdir() if path.is_dir() and path.name.startswith("anchor"))
    if not anchors:
        if require_anchor:
            return GateResult("A-09 anchor_is_live", False, "S2/anchor required for full_world scope")
        return GateResult("A-09 anchor_is_live", True, "no S2 stage in this campaign (anchor not required for s0_s1_only)")
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


def a10_s2_full_world_evidence(campaign_root: Path) -> GateResult:
    s2_runs = sorted(path for path in campaign_root.iterdir() if path.is_dir() and path.name.startswith("s2_"))
    if not s2_runs:
        return GateResult("A-10 s2_full_world_evidence", False, "full_world scope requires at least one S2 run")
    problems: list[str] = []
    for run_root in s2_runs:
        data = _load(run_root)
        events = [str(row.get("event_type") or "") for row in data["ledger"]]
        event_set = set(events)
        missing = [
            name
            for name in ("customer_event", "customer_utterance", "inbox_delivered", "month_end_close")
            if name not in event_set
        ]
        agent_seats = {
            str(row.get("seat_id") or "")
            for row in data["attempts"]
            if row.get("origin") == "agent" and row.get("tool") == "llm_invoke" and str(row.get("seat_id") or "")
        }
        if len(agent_seats) < 2:
            problems.append(f"{run_root.name}: active agent seats={sorted(agent_seats)}")
        if missing:
            problems.append(f"{run_root.name}: missing ledger events {missing}")
        triage = run_root / "triage" / "metrics.json"
        if not triage.exists():
            problems.append(f"{run_root.name}: triage/metrics.json missing")
    return GateResult("A-10 s2_full_world_evidence", not problems, "; ".join(problems[:6]))


def a11_role_tool_bundle_enforced(run_root: Path) -> GateResult:
    data = _load(run_root)
    seats = _seat_configs(data["config"])
    if not seats:
        return GateResult("A-11 role_tool_bundle_enforced", False, "world.population.seats missing")
    problems: list[str] = []
    app_only = {"submit_application", "verify_identity", "link_review", "complete_contract", "deliver_documents"}
    approval_only = {"approve_application", "return_application"}
    for seat_id, seat in seats.items():
        role = str(seat.get("role") or "")
        tools = set(seat.get("tools") or [])
        if role == "sales" and tools & app_only:
            problems.append(f"{seat_id}: sales has application tools {sorted(tools & app_only)}")
        if role == "sales" and tools & approval_only:
            problems.append(f"{seat_id}: sales has approval tools {sorted(tools & approval_only)}")
        if role == "manager" and tools & app_only:
            problems.append(f"{seat_id}: manager has application tools {sorted(tools & app_only)}")
        if role == "application" and tools & approval_only:
            problems.append(f"{seat_id}: application has approval tools {sorted(tools & approval_only)}")
    for row in data["attempts"]:
        if row.get("origin") != "agent":
            continue
        seat_id = str(row.get("seat_id") or "")
        if seat_id not in seats:
            continue
        tool = str(row.get("tool") or "")
        allowed = set(seats[seat_id].get("tools") or []) | LLM_ATTEMPT_TOOLS
        if tool not in allowed:
            problems.append(f"{seat_id}: attempted {tool} outside role bundle")
    return GateResult("A-11 role_tool_bundle_enforced", not problems, "; ".join(problems[:8]))


def a12_role_card_snapshot_matches_prompt(run_root: Path) -> GateResult:
    data = _load(run_root)
    seats = _seat_configs(data["config"])
    problems: list[str] = []
    for seat_id, seat in seats.items():
        role_card = seat.get("role_card") or {}
        text = str(role_card.get("text") or "")
        expected = str(role_card.get("sha256") or _role_card_hash(text))
        actual_text_hash = _role_card_hash(text)
        if expected != actual_text_hash:
            problems.append(f"{seat_id}: role_card sha256 does not match text")
    for row in data["attempts"]:
        if row.get("tool") != "llm_invoke":
            continue
        args = row.get("args") or {}
        if args.get("backend") != "deepagents":
            continue
        seat_id = str(row.get("seat_id") or "")
        if seat_id == "customer" or seat_id not in seats:
            continue
        role_card = seats[seat_id].get("role_card") or {}
        expected = str(role_card.get("sha256") or _role_card_hash(str(role_card.get("text") or "")))
        actual = str(args.get("role_card_hash") or "")
        if actual != expected:
            problems.append(f"{seat_id}: llm_invoke role_card_hash mismatch")
    return GateResult("A-12 role_card_snapshot_matches_prompt", not problems, "; ".join(problems[:8]))


def a13_d4_store_has_read_path(run_root: Path) -> GateResult:
    data = _load(run_root)
    stage = str(data["meta"].get("stage") or "")
    if stage not in {"S1", "S2"}:
        return GateResult("A-13 d4_store_has_read_path", True, "not an episode/world run")
    seats = _seat_configs(data["config"])
    store_enabled = any(bool((seat.get("store") or {}).get("enabled")) for seat in seats.values())
    if not store_enabled:
        return GateResult("A-13 d4_store_has_read_path", True, "store disabled")
    ops = [str(row.get("op") or "") for row in data["store"]]
    ok = "write" in ops and "read" in ops
    return GateResult("A-13 d4_store_has_read_path", ok, "" if ok else f"store ops={sorted(set(ops))}")


def check_bundle(run_root: Path) -> BundleReport:
    report = BundleReport(run_root=run_root)
    report.results.append(a01_no_scripted_origin(run_root))
    report.results.append(a02_live_required(run_root))
    report.results.append(a03_inbox_whitelist(run_root))
    report.results.append(a04_basis_authorship(run_root))
    report.results.append(a05_grounding_population(run_root))
    report.results.append(a11_role_tool_bundle_enforced(run_root))
    report.results.append(a12_role_card_snapshot_matches_prompt(run_root))
    stage = ""
    meta_path = run_root / "meta.json"
    if meta_path.exists():
        stage = str(json.loads(meta_path.read_text(encoding="utf-8")).get("stage") or "")
    if stage in {"S1", "S2"}:
        report.results.append(a08_customer_is_agent(run_root))
        report.results.append(a13_d4_store_has_read_path(run_root))
    return report


def a06_s0_divergence_measured(campaign_root: Path) -> GateResult:
    path = campaign_root / "s0_divergence.json"
    if not path.exists():
        return GateResult("A-06 s0_divergence_measured", False, "s0_divergence.json missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("cells") or []
    measured = [row for row in rows if row.get("answers", 0) >= 2 and "entropy" in row]
    live_backed = payload.get("all_answers_live") is True
    ok = bool(measured) and live_backed
    return GateResult("A-06 s0_divergence_measured", ok, "" if ok else f"measured cells={len(measured)}, all_answers_live={live_backed}")


def run_acceptance(*, campaign_root: Path, design: DesignInputs, corpus: Corpus, scope: str = "full_world") -> dict[str, Any]:
    if scope not in {"s0_s1_only", "full_world"}:
        raise ValueError("scope must be 's0_s1_only' or 'full_world'")
    bundle_reports: list[BundleReport] = []
    for path in sorted(campaign_root.iterdir()):
        if path.is_dir() and (path / "meta.json").exists():
            bundle_reports.append(check_bundle(path))
    gates: list[GateResult] = [
        a06_s0_divergence_measured(campaign_root),
        a07_stale_content_differs(design, corpus),
        a09_anchor_is_live(campaign_root, require_anchor=scope == "full_world"),
    ]
    if scope == "full_world":
        gates.append(a10_s2_full_world_evidence(campaign_root))
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
