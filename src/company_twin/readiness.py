from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPORT_SCHEMA_VERSION = "company_twin.readiness_report.v1"


def run_readiness_gate(campaign_root: Path, *, semantic_threshold: float = 0.8) -> dict[str, Any]:
    """Stage 9 experiment-readiness gate.

    This is intentionally separate from harness-safety acceptance. A harness can
    be live and unfakeable while still not being ready for design-level
    experiment conclusions.
    """
    checks = [
        _acceptance_check(campaign_root),
        _passed_report_check(campaign_root, "routine_smoke_report.json", "routine_smoke_passed"),
        _s0_divergence_sanity_check(campaign_root),
        _leak_lint_check(campaign_root),
        _passed_report_check(campaign_root, "retrieval_audit.json", "retrieval_audit_passed"),
        _semantic_grounding_check(campaign_root, semantic_threshold=semantic_threshold),
        _passed_report_check(campaign_root, "semantic_grounding_report.json", "semantic_grounding_report_passed"),
        _passed_report_check(campaign_root, "backcasting_report.json", "backcasting_passed"),
        _passed_report_check(campaign_root, "sme_blind_review.json", "sme_blind_review_passed"),
        _passed_report_check(campaign_root, "holdout_report.json", "holdout_passed"),
    ]
    payload = {
        "campaign_root": str(campaign_root),
        "gate": "stage9_experiment_readiness",
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
        "note": "Harness-safety acceptance is necessary but not sufficient for Stage 9 readiness.",
    }
    (campaign_root / "readiness_report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def write_readiness_reports(
    campaign_root: Path,
    *,
    corpus: Any | None = None,
    lint_payload: dict[str, Any] | None = None,
    semantic_threshold: float = 0.8,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Generate the Stage 9 report envelope files.

    This command intentionally does not mark unimplemented evidence as passed.
    It gives reviewers a stable schema and makes missing semantic/backcasting/
    SME/holdout evidence explicit instead of allowing empty placeholder files.
    """
    campaign_root.mkdir(parents=True, exist_ok=True)
    report_payloads = {
        "routine_smoke_report.json": _routine_smoke_report(campaign_root),
        "retrieval_audit.json": _retrieval_audit_report(corpus),
        "leak_lint_report.json": _leak_lint_report(lint_payload),
        "semantic_grounding_report.json": _semantic_grounding_report(campaign_root, semantic_threshold=semantic_threshold),
        "backcasting_report.json": _manual_evidence_report("backcasting", "backcasting_inputs.json"),
        "sme_blind_review.json": _manual_evidence_report("sme_blind_review", "sme_blind_review_inputs.json"),
        "holdout_report.json": _manual_evidence_report("holdout", "holdout_inputs.json"),
    }
    written: dict[str, Any] = {}
    for filename, payload in report_payloads.items():
        path = campaign_root / filename
        if path.exists() and not overwrite:
            try:
                written[filename] = json.loads(path.read_text(encoding="utf-8"))
                continue
            except json.JSONDecodeError:
                pass
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written[filename] = payload
    summary = {
        "campaign_root": str(campaign_root),
        "schema_version": REPORT_SCHEMA_VERSION,
        "reports": sorted(written),
        "passed_reports": sorted(filename for filename, payload in written.items() if payload.get("passed") is True),
        "blocked_reports": sorted(filename for filename, payload in written.items() if payload.get("passed") is not True),
        "note": "These are readiness evidence reports; Stage 9 still requires run_readiness_gate to pass.",
    }
    (campaign_root / "readiness_reports_manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def _acceptance_check(campaign_root: Path) -> dict[str, Any]:
    path = campaign_root / "acceptance_report.json"
    if not path.exists():
        return {"check": "full_world_harness_acceptance", "passed": False, "detail": "acceptance_report.json missing"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    ok = payload.get("scope") == "full_world" and payload.get("passed") is True
    return {
        "check": "full_world_harness_acceptance",
        "passed": ok,
        "detail": "" if ok else f"scope={payload.get('scope')}, passed={payload.get('passed')}",
    }


def _semantic_grounding_check(campaign_root: Path, *, semantic_threshold: float) -> dict[str, Any]:
    observed: list[float] = []
    missing = 0
    for metrics_path in campaign_root.glob("**/triage/metrics.json"):
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        if metrics.get("stage") != "S2":
            continue
        value = metrics.get("grounding_semantic_all3_rate")
        if value is None:
            missing += 1
            continue
        observed.append(float(value))
    ok = bool(observed) and min(observed) >= semantic_threshold
    detail = f"observed={observed}, missing_semantic_metrics={missing}, threshold={semantic_threshold}"
    return {"check": "semantic_grounding_all3_threshold", "passed": ok, "detail": "" if ok else detail}


def _s0_divergence_sanity_check(campaign_root: Path) -> dict[str, Any]:
    path = campaign_root / "s0_divergence.json"
    if not path.exists():
        return {"check": "s0_divergence_sanity", "passed": False, "detail": "s0_divergence.json missing"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    cells = payload.get("cells") or []
    answer_total = int(payload.get("answer_total") or 0)
    sane = bool(cells) and answer_total > 0 and payload.get("all_answers_live") is True
    return {
        "check": "s0_divergence_sanity",
        "passed": sane,
        "detail": "" if sane else f"cells={len(cells)}, answer_total={answer_total}, all_answers_live={payload.get('all_answers_live')}",
    }


def _leak_lint_check(campaign_root: Path) -> dict[str, Any]:
    report = campaign_root / "leak_lint_report.json"
    if report.exists():
        checked = _passed_report_check(campaign_root, "leak_lint_report.json", "leak_lint_passed")
        return checked
    legacy = campaign_root / "world_surface_lint.json"
    if legacy.exists():
        path = legacy
        payload = json.loads(path.read_text(encoding="utf-8"))
        ok = payload.get("passed") is True
        return {"check": "leak_lint_passed", "passed": ok, "detail": "" if ok else f"world_surface_lint.json: passed={payload.get('passed')}"}
    return {"check": "leak_lint_passed", "passed": False, "detail": "leak_lint_report.json or world_surface_lint.json missing"}


def _passed_report_check(campaign_root: Path, filename: str, check_name: str) -> dict[str, Any]:
    path = campaign_root / filename
    if not path.exists():
        return {"check": check_name, "passed": False, "detail": f"{filename} missing"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"check": check_name, "passed": False, "detail": f"{filename} invalid json: {exc}"}
    schema_ok = payload.get("schema_version") == REPORT_SCHEMA_VERSION
    ok = schema_ok and payload.get("passed") is True
    return {
        "check": check_name,
        "passed": ok,
        "detail": "" if ok else f"{filename}: schema_ok={schema_ok}, passed={payload.get('passed')}, status={payload.get('status')}",
    }


def _report(report_type: str, checks: list[dict[str, Any]], *, notes: list[str] | None = None) -> dict[str, Any]:
    passed = all(check.get("passed") is True for check in checks)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": report_type,
        "status": "passed" if passed else "blocked",
        "passed": passed,
        "checks": checks,
        "notes": notes or [],
    }


def _routine_smoke_report(campaign_root: Path, *, required_customer_utterances: int = 28) -> dict[str, Any]:
    s2_roots = _s2_roots(campaign_root)
    utterances = 0
    month_end = False
    active_seats: set[str] = set()
    for root in s2_roots:
        for row in _read_jsonl(root / "world_ledger.jsonl"):
            event_type = row.get("event_type")
            if event_type == "customer_utterance":
                utterances += 1
            if event_type == "month_end_close":
                month_end = True
        for row in _read_jsonl(root / "attempts.jsonl"):
            if row.get("origin") == "agent" and row.get("seat_id"):
                active_seats.add(str(row["seat_id"]))
    checks = [
        {"name": "s2_bundle_present", "passed": bool(s2_roots), "observed": len(s2_roots)},
        {
            "name": "routine_customer_utterances_minimum",
            "passed": utterances >= required_customer_utterances,
            "observed": utterances,
            "required": required_customer_utterances,
        },
        {"name": "month_end_present", "passed": month_end, "observed": month_end},
        {"name": "multiple_agent_seats_active", "passed": len(active_seats) >= 2, "observed": sorted(active_seats)},
    ]
    return _report("routine_smoke", checks, notes=["Stage 9 C-smoke expects routine coverage, not only a smoke S2 bundle."])


def _retrieval_audit_report(corpus: Any | None) -> dict[str, Any]:
    audit = corpus.audit_retrieval() if corpus is not None else {}
    sales_ids = list(audit.get("sales_elderly_top_ids") or [])
    sales_stale = list(audit.get("sales_stale_ids") or [])
    second_line_stale = list(audit.get("second_line_stale_ids") or [])
    checks = [
        {"name": "sales_elderly_returns_current_021", "passed": "DFH-SAL-021" in sales_ids, "observed": sales_ids},
        {"name": "sales_profile_can_see_stale_v1", "passed": bool(sales_stale), "observed": sales_stale},
        {"name": "second_line_cannot_see_stale_v1", "passed": not second_line_stale, "observed": second_line_stale},
        {"name": "corpus_audit_passed", "passed": audit.get("passed") is True, "observed": audit.get("passed")},
    ]
    return _report("retrieval_audit", checks, notes=["Retrieval audit is executable and schema-backed; it is separate from semantic grounding."])


def _leak_lint_report(lint_payload: dict[str, Any] | None) -> dict[str, Any]:
    checks = [
        {
            "name": "world_surface_lint_passed",
            "passed": bool(lint_payload) and lint_payload.get("passed") is True,
            "observed": None if lint_payload is None else lint_payload.get("passed"),
            "failures": [] if lint_payload is None else lint_payload.get("failures", []),
        }
    ]
    return _report("leak_lint", checks)


def _semantic_grounding_report(campaign_root: Path, *, semantic_threshold: float) -> dict[str, Any]:
    check = _semantic_grounding_check(campaign_root, semantic_threshold=semantic_threshold)
    return _report(
        "semantic_grounding",
        [{"name": check["check"], "passed": check["passed"], "detail": check["detail"], "threshold": semantic_threshold}],
        notes=["Machine lexical g3 is not accepted here; this report requires grounding_semantic_all3_rate."],
    )


def _manual_evidence_report(report_type: str, input_filename: str) -> dict[str, Any]:
    checks = [
        {
            "name": f"{report_type}_evidence_supplied",
            "passed": False,
            "required_input": input_filename,
            "detail": "No implemented evidence generator or reviewed input was supplied in this PR.",
        }
    ]
    return _report(report_type, checks)


def _s2_roots(campaign_root: Path) -> list[Path]:
    roots: list[Path] = []
    for path in sorted(campaign_root.iterdir()) if campaign_root.exists() else []:
        if not path.is_dir() or not (path / "meta.json").exists():
            continue
        try:
            meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if meta.get("stage") == "S2":
            roots.append(path)
    return roots


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows
