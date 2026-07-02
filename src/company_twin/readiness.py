from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def run_readiness_gate(campaign_root: Path, *, semantic_threshold: float = 0.8) -> dict[str, Any]:
    """Stage 9 experiment-readiness gate.

    This is intentionally separate from harness-safety acceptance. A harness can
    be live and unfakeable while still not being ready for design-level
    experiment conclusions.
    """
    checks = [
        _acceptance_check(campaign_root),
        _file_check(campaign_root, "routine_smoke_report.json", "routine_smoke_present"),
        _s0_divergence_sanity_check(campaign_root),
        _leak_lint_check(campaign_root),
        _file_check(campaign_root, "retrieval_audit.json", "retrieval_audit_present"),
        _semantic_grounding_check(campaign_root, semantic_threshold=semantic_threshold),
        _file_check(campaign_root, "backcasting_report.json", "backcasting_present"),
        _file_check(campaign_root, "sme_blind_review.json", "sme_blind_review_present"),
        _file_check(campaign_root, "holdout_report.json", "holdout_present"),
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
    for filename in ("leak_lint_report.json", "world_surface_lint.json"):
        path = campaign_root / filename
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        ok = payload.get("passed") is True
        return {"check": "leak_lint_passed", "passed": ok, "detail": "" if ok else f"{filename}: passed={payload.get('passed')}"}
    return {"check": "leak_lint_passed", "passed": False, "detail": "leak_lint_report.json or world_surface_lint.json missing"}


def _file_check(campaign_root: Path, filename: str, check_name: str) -> dict[str, Any]:
    exists = (campaign_root / filename).exists()
    return {"check": check_name, "passed": exists, "detail": "" if exists else f"{filename} missing"}
