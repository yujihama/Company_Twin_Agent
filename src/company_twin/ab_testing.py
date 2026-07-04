from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .oracles import CONTROLLED_TOOL_NAMES, wilson_interval
from .recorder import read_jsonl


PROMPT_AB_SCHEMA_VERSION = "company_twin.prompt_mode_ab_report.v1"


def write_prompt_mode_ab_report(campaign_root: Path, *, output_path: Path | None = None) -> dict[str, Any]:
    campaign_root = campaign_root.resolve()
    groups: dict[str, dict[str, Any]] = {}
    for run_root in sorted(path for path in campaign_root.iterdir() if path.is_dir()):
        meta_path = run_root / "meta.json"
        metrics_path = run_root / "triage" / "metrics.json"
        if not meta_path.exists() or not metrics_path.exists():
            continue
        meta = _read_json(meta_path)
        metrics = _read_json(metrics_path)
        mode = str(meta.get("prompt_mode") or "unknown")
        if mode not in {"scaffold", "measurement"}:
            continue
        key = json.dumps(
            {
                "stage": meta.get("stage"),
                "probe": meta.get("probe"),
                "knobs": meta.get("knobs") or {},
                "anchor": bool(meta.get("anchor")),
                "prompt_mode": mode,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        group = groups.setdefault(
            key,
            {
                "config": json.loads(key),
                "run_count": 0,
                "seeds": [],
                "basis_action_bound": 0,
                "semantic_all3_count": 0,
                "controlled_actions": 0,
                "grounding_gap_findings": 0,
                "basis_fabrication_findings": 0,
                "tool_counts": Counter(),
            },
        )
        attempts = read_jsonl(run_root / "attempts.jsonl")
        finding_types = metrics.get("finding_types") or {}
        basis_action_bound = int(metrics.get("basis_action_bound") or 0)
        semantic_rate = metrics.get("grounding_semantic_all3_rate")
        semantic_count = int(round(float(semantic_rate) * basis_action_bound)) if semantic_rate is not None else 0
        group["run_count"] += 1
        if meta.get("seed") is not None:
            group["seeds"].append(meta.get("seed"))
        group["basis_action_bound"] += basis_action_bound
        group["semantic_all3_count"] += semantic_count
        group["controlled_actions"] += int(metrics.get("controlled_actions_agent") or 0)
        group["grounding_gap_findings"] += int(finding_types.get("grounding_gap") or 0)
        group["basis_fabrication_findings"] += int(finding_types.get("world_basis_leak") or 0) + int(finding_types.get("version_gap") or 0)
        for row in attempts:
            if row.get("origin") == "agent" and row.get("success") and row.get("tool") in CONTROLLED_TOOL_NAMES:
                group["tool_counts"][str(row.get("tool"))] += 1

    rows = [_render_group(row) for row in groups.values()]
    comparisons = _pairwise_prompt_mode_comparisons(rows)
    payload = {
        "schema_version": PROMPT_AB_SCHEMA_VERSION,
        "campaign_root": str(campaign_root),
        "groups": rows,
        "comparisons": comparisons,
        "minimum_live_design": {"prompt_modes": ["scaffold", "measurement"], "seeds_per_condition": 5},
        "note": "This report is deterministic over existing bundles. It does not by itself prove the K>=5 live A/B milestone was executed.",
    }
    target = output_path or (campaign_root / "prompt_mode_ab_report.json")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _render_group(group: dict[str, Any]) -> dict[str, Any]:
    basis_total = int(group["basis_action_bound"])
    semantic_success = int(group["semantic_all3_count"])
    semantic_low, semantic_high = wilson_interval(semantic_success, basis_total)
    controlled_total = int(group["controlled_actions"])
    grounding_gap = int(group["grounding_gap_findings"])
    gap_low, gap_high = wilson_interval(grounding_gap, max(basis_total, grounding_gap))
    return {
        "config": group["config"],
        "run_count": group["run_count"],
        "seeds": sorted(group["seeds"]),
        "basis_action_bound": basis_total,
        "semantic_all3_count": semantic_success,
        "semantic_all3_rate": (semantic_success / basis_total) if basis_total else None,
        "semantic_all3_wilson_95": [round(semantic_low, 4), round(semantic_high, 4)],
        "controlled_actions": controlled_total,
        "controlled_tool_distribution": dict(sorted(group["tool_counts"].items())),
        "grounding_gap_findings": grounding_gap,
        "grounding_gap_rate": (grounding_gap / max(basis_total, grounding_gap)) if max(basis_total, grounding_gap) else None,
        "grounding_gap_wilson_95": [round(gap_low, 4), round(gap_high, 4)],
        "basis_fabrication_findings": int(group["basis_fabrication_findings"]),
    }


def _pairwise_prompt_mode_comparisons(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_config: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        config = dict(row["config"])
        mode = str(config.pop("prompt_mode"))
        key = json.dumps(config, ensure_ascii=False, sort_keys=True)
        by_config[key][mode] = row
    comparisons = []
    for key, modes in sorted(by_config.items()):
        if "scaffold" not in modes or "measurement" not in modes:
            continue
        scaffold = modes["scaffold"]
        measurement = modes["measurement"]
        comparisons.append(
            {
                "config_without_prompt_mode": json.loads(key),
                "scaffold_runs": scaffold["run_count"],
                "measurement_runs": measurement["run_count"],
                "semantic_all3_delta_measurement_minus_scaffold": _rate_delta(measurement["semantic_all3_rate"], scaffold["semantic_all3_rate"]),
                "controlled_actions_delta_measurement_minus_scaffold": measurement["controlled_actions"] - scaffold["controlled_actions"],
                "grounding_gap_delta_measurement_minus_scaffold": _rate_delta(measurement["grounding_gap_rate"], scaffold["grounding_gap_rate"]),
                "ready_for_design_conclusion": scaffold["run_count"] >= 5 and measurement["run_count"] >= 5,
            }
        )
    return comparisons


def _rate_delta(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return round(left - right, 6)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
