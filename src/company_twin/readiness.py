from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .semantic_grounding import READINESS_ALLOWED_JUDGE_BACKENDS

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
        _backcasting_check(campaign_root),
        _sme_blind_review_check(campaign_root),
        _holdout_check(campaign_root),
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
        "backcasting_report.json": _backcasting_report_payload(campaign_root),
        "sme_blind_review.json": _sme_blind_review_report_payload(campaign_root),
        "holdout_report.json": _holdout_report_payload(campaign_root),
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
    disallowed = 0
    observations: list[dict[str, Any]] = []
    for metrics_path in campaign_root.glob("**/triage/metrics.json"):
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        if metrics.get("stage") != "S2":
            continue
        observation = _semantic_observation(metrics_path.parents[1], metrics)
        observations.append(observation)
        if observation["status"] == "missing":
            missing += 1
            continue
        if observation["status"] == "disallowed_backend":
            disallowed += 1
            continue
        value = observation["rate"]
        observed.append(float(value))
    ok = bool(observed) and min(observed) >= semantic_threshold
    detail = f"observed={observed}, missing_semantic_metrics={missing}, disallowed_backend_metrics={disallowed}, threshold={semantic_threshold}, observations={observations}"
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


ROUTINE_SMOKE_MAX_CORRUPT_LINE_RATIO = 0.01


def _routine_smoke_report(campaign_root: Path, *, required_customer_utterances: int = 28) -> dict[str, Any]:
    s2_roots = _s2_roots(campaign_root)
    utterances = 0
    month_end = False
    active_seats: set[str] = set()
    parsed_lines = 0
    unparsed_lines = 0
    undecodable_lines = 0
    corrupted_lines = 0
    corrupted_files: dict[str, int] = {}
    missing_triage_metrics: list[str] = []
    for root in s2_roots:
        if not (root / "triage" / "metrics.json").exists():
            missing_triage_metrics.append(root.name)
        ledger_rows, ledger_stats = _read_jsonl_tolerant(root / "world_ledger.jsonl")
        attempt_rows, attempt_stats = _read_jsonl_tolerant(root / "attempts.jsonl")
        for filename, stats in (("world_ledger.jsonl", ledger_stats), ("attempts.jsonl", attempt_stats)):
            parsed_lines += stats["parsed"]
            unparsed_lines += stats["unparsed"]
            undecodable_lines += stats["undecodable"]
            corrupted_lines += stats["corrupted"]
            if stats["corrupted"]:
                corrupted_files[f"{root.name}/{filename}"] = stats["corrupted"]
        for row in ledger_rows:
            event_type = row.get("event_type")
            if event_type == "customer_utterance":
                utterances += 1
            if event_type == "month_end_close":
                month_end = True
        for row in attempt_rows:
            if row.get("origin") == "agent" and row.get("seat_id"):
                active_seats.add(str(row["seat_id"]))
    total_lines = parsed_lines + unparsed_lines
    corruption_ratio = (corrupted_lines / total_lines) if total_lines else 0.0
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
        {
            "name": "jsonl_corruption_within_tolerance",
            "passed": corruption_ratio <= ROUTINE_SMOKE_MAX_CORRUPT_LINE_RATIO,
            "observed": {
                "parsed_line_count": parsed_lines,
                "unparsed_line_count": unparsed_lines,
                "undecodable_line_count": undecodable_lines,
                "corrupted_line_count": corrupted_lines,
                "corruption_ratio": corruption_ratio,
                "corrupted_files": corrupted_files,
            },
            "max_corrupt_line_ratio": ROUTINE_SMOKE_MAX_CORRUPT_LINE_RATIO,
        },
    ]
    payload = _report(
        "routine_smoke",
        checks,
        notes=[
            "Stage 9 C-smoke expects routine coverage, not only a smoke S2 bundle.",
            "Corrupt JSONL lines are skipped for event counting but always counted and surfaced; thresholds are evaluated on successfully parsed data only.",
        ],
    )
    payload["run_roots_missing_triage_metrics"] = missing_triage_metrics
    return payload


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
    observed_reports = []
    for path in sorted(campaign_root.glob("**/g3_semantic_grounding.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if payload.get("schema_version") == "company_twin.g3_semantic_grounding.v1":
            observed_reports.append(
                {
                    "path": str(path.relative_to(campaign_root)).replace("\\", "/"),
                    "rate": payload.get("grounding_semantic_all3_rate"),
                    "proxy_rate": payload.get("grounding_semantic_all3_rate_proxy"),
                    "judge": payload.get("judge"),
                    "readiness_eligible": _judge_allowed(payload.get("judge")),
                }
            )
    return _report(
        "semantic_grounding",
        [{"name": check["check"], "passed": check["passed"], "detail": check["detail"], "threshold": semantic_threshold, "observed_reports": observed_reports}],
        notes=["This report requires g3 semantic grounding values; legacy machine lexical g3 alone is not sufficient."],
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


def _backcasting_report_payload(campaign_root: Path) -> dict[str, Any]:
    from .backcasting import write_backcasting_report

    return write_backcasting_report(campaign_root)


def _sme_blind_review_report_payload(campaign_root: Path) -> dict[str, Any]:
    from .sme_blind_review import write_sme_blind_review_report

    return write_sme_blind_review_report(campaign_root)


def _holdout_report_payload(campaign_root: Path) -> dict[str, Any]:
    from .holdout import write_holdout_report

    return write_holdout_report(campaign_root)


SME_BLIND_REVIEW_MIN_REVIEWED_ROWS = 1


def _backcasting_check(campaign_root: Path) -> dict[str, Any]:
    return _structural_evidence_check(
        campaign_root,
        filename="backcasting_report.json",
        check_name="backcasting_passed",
        row_path=("checks", 0, "rows"),
        min_rows=1,
    )


def _sme_blind_review_check(campaign_root: Path) -> dict[str, Any]:
    return _structural_evidence_check(
        campaign_root,
        filename="sme_blind_review.json",
        check_name="sme_blind_review_passed",
        row_path=("checks", 0, "rows"),
        min_rows=SME_BLIND_REVIEW_MIN_REVIEWED_ROWS,
    )


def _holdout_check(campaign_root: Path) -> dict[str, Any]:
    return _structural_evidence_check(
        campaign_root,
        filename="holdout_report.json",
        check_name="holdout_passed",
        row_path=("checks", 0, "per_injection"),
        min_rows=1,
    )


def _structural_evidence_check(
    campaign_root: Path,
    *,
    filename: str,
    check_name: str,
    row_path: tuple[Any, ...],
    min_rows: int,
) -> dict[str, Any]:
    """A stricter variant of _passed_report_check for the WP-14 manual-evidence
    gates (backcasting/SME blind review/holdout).

    Ungameability: it is not enough for the report file to say
    ``"passed": true`` with the right schema_version -- the report must also
    carry a non-empty per-item evidence breakdown (per-injection detection
    rows for holdout, per-case reproduction rows for backcasting, per-item
    reviewer rows for SME blind review) with at least `min_rows` entries.
    A hand-edited report claiming pass without that evidence is rejected here,
    the same way routine_smoke rejects corrupted-but-unsurfaced evidence.
    """
    base = _passed_report_check(campaign_root, filename, check_name)
    if not base["passed"]:
        return base
    path = campaign_root / filename
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"check": check_name, "passed": False, "detail": f"{filename} invalid json"}
    rows = _dig(payload, row_path)
    ok = isinstance(rows, list) and len(rows) >= min_rows
    if ok:
        return base
    return {
        "check": check_name,
        "passed": False,
        "detail": f"{filename}: passed=true but missing structural evidence rows at {'.'.join(str(p) for p in row_path)} (found {rows!r})",
    }


def _dig(payload: Any, path: tuple[Any, ...]) -> Any:
    current = payload
    for key in path:
        if isinstance(key, int):
            if not isinstance(current, list) or key >= len(current):
                return None
            current = current[key]
        else:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
    return current


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


def _read_jsonl_tolerant(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Read JSONL rows, skipping unusable lines while counting every degradation.

    Live S2 logs can contain partially flushed lines (observed on real Track A
    data: a stray non-UTF-8 byte in s2_seed0/attempts.jsonl). Readiness report
    generation must not crash on one corrupt byte, but it must never silently
    pretend the file was clean: callers surface these counts in the report
    payload and gate on the corruption ratio.

    Stats semantics per non-empty line:
    - parsed: decoded to a JSON object; used for threshold evaluation.
    - unparsed: failed json parsing or was not an object; skipped.
    - undecodable: contained invalid UTF-8 bytes (replacement chars present).
    - corrupted: unparsed or undecodable (unique line count).
    """
    stats = {"parsed": 0, "unparsed": 0, "undecodable": 0, "corrupted": 0}
    if not path.exists():
        return [], stats
    rows: list[dict[str, Any]] = []
    text = path.read_bytes().decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line.strip():
            continue
        undecodable = "�" in line
        if undecodable:
            stats["undecodable"] += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            row = None
        if not isinstance(row, dict):
            stats["unparsed"] += 1
            stats["corrupted"] += 1
            continue
        stats["parsed"] += 1
        if undecodable:
            stats["corrupted"] += 1
        rows.append(row)
    return rows, stats


def _semantic_observation(run_root: Path, metrics: dict[str, Any]) -> dict[str, Any]:
    metric_judge = metrics.get("semantic_grounding_judge")
    metric_rate = metrics.get("grounding_semantic_all3_rate")
    metric_proxy_rate = metrics.get("grounding_semantic_all3_rate_proxy")
    if metric_rate is not None:
        if _judge_allowed(metric_judge):
            return {"run_root": run_root.name, "status": "eligible", "rate": float(metric_rate), "proxy_rate": metric_proxy_rate, "judge": metric_judge}
        return {"run_root": run_root.name, "status": "disallowed_backend", "rate": metric_rate, "proxy_rate": metric_proxy_rate, "judge": metric_judge}

    report = _read_json(run_root / "g3_semantic_grounding.json")
    report_rate = report.get("grounding_semantic_all3_rate") if report else None
    report_proxy_rate = report.get("grounding_semantic_all3_rate_proxy") if report else metric_proxy_rate
    report_judge = report.get("judge") if report else metric_judge
    if report_rate is not None:
        if _judge_allowed(report_judge):
            return {"run_root": run_root.name, "status": "eligible", "rate": float(report_rate), "proxy_rate": report_proxy_rate, "judge": report_judge}
        return {"run_root": run_root.name, "status": "disallowed_backend", "rate": report_rate, "proxy_rate": report_proxy_rate, "judge": report_judge}
    if report_proxy_rate is not None or metric_proxy_rate is not None:
        return {"run_root": run_root.name, "status": "disallowed_backend", "rate": None, "proxy_rate": report_proxy_rate, "judge": report_judge}
    return {"run_root": run_root.name, "status": "missing", "rate": None, "proxy_rate": None, "judge": report_judge}


def _judge_allowed(judge: Any) -> bool:
    if not isinstance(judge, dict):
        return False
    return bool(judge.get("readiness_eligible")) and str(judge.get("backend") or "") in READINESS_ALLOWED_JUDGE_BACKENDS


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
