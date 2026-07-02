from __future__ import annotations

import inspect
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .agents import role_system_prompt
from .corpus import Corpus
from .design_loader import DesignInputs
from .env import normalize_openrouter_model
from .harness import run_s0, run_s1_episode, run_s2_world
from .oracles import write_triage
from .recorder import read_jsonl
from .world_config import assert_world_config_complete


WORLD_PROMPT_BANNED_TERMS = (
    "experiment",
    "evaluation",
    "fuzzing",
    "seeded span",
    "hidden test",
    "simulation",
    "simulated",
    "test harness",
    "campaign",
    "oracle",
    "recorder",
    "triage",
    "benchmark",
    "実験",
    "評価",
    "seeded",
    "span registry",
)


@dataclass(frozen=True)
class S0MatrixRow:
    probe_id: str
    span_id: str
    seat_id: str
    model: str
    variant: int


def build_s0_matrix(design: DesignInputs, *, models: list[str], variants: int = 2) -> list[S0MatrixRow]:
    seats = sorted(seat_id for seat_id in design.seats if seat_id.startswith("emp-"))
    rows: list[S0MatrixRow] = []
    for probe_id, probe in sorted(design.probes.items()):
        for span_id in probe.binds:
            if span_id not in design.spans:
                continue
            for seat_id in seats:
                for model in models:
                    for variant in range(variants):
                        rows.append(S0MatrixRow(probe_id=probe_id, span_id=span_id, seat_id=seat_id, model=model, variant=variant))
    return rows


def run_design_campaign(
    *,
    root: Path,
    design: DesignInputs,
    corpus: Corpus,
    live: bool,
    max_live_agent_calls: int = 3,
    model: str | None = None,
    s0_execute_budget: int = 8,
    s1_k: int = 5,
    s2_k: int = 3,
) -> dict[str, Any]:
    model_name = normalize_openrouter_model(model)
    campaign_root = root / "runs" / f"design_campaign_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    campaign_root.mkdir(parents=True, exist_ok=True)
    matrix = build_s0_matrix(design, models=[model_name], variants=2)
    (campaign_root / "s0_matrix.json").write_text(json.dumps([asdict(row) for row in matrix], ensure_ascii=False, indent=2), encoding="utf-8")

    run_roots: list[str] = []
    live_calls_used = 0
    s0_results: list[dict[str, Any]] = []
    selected_rows = _diverse_s0_rows(matrix, budget=s0_execute_budget)

    anchor_root = campaign_root / "anchor_s2_seed0"
    run_s2_world(design=design, corpus=corpus, run_root=anchor_root, live=False, model=model_name, knobs={}, seed=0, max_agent_calls=0, anchor=True)
    write_triage(anchor_root)
    run_roots.append(str(anchor_root))

    for idx, row in enumerate(selected_rows):
        s0_root = campaign_root / f"s0_{idx:02d}_{row.probe_id}_{row.seat_id}_v{row.variant}"
        s0_live = live and live_calls_used < min(max_live_agent_calls, 1)
        result = run_s0(design=design, corpus=corpus, probe_id=row.probe_id, seat_id=row.seat_id, run_root=s0_root, live=s0_live, model=row.model, variant=row.variant)
        if s0_live:
            live_calls_used += 1
        write_triage(s0_root)
        run_roots.append(str(s0_root))
        s0_results.append({**asdict(row), **result})

    promoted_probe = _promote_probe(s0_results) or selected_rows[0].probe_id
    s1_roots: list[str] = []
    for seed in range(s1_k):
        s1_root = campaign_root / f"s1_{promoted_probe}_seed{seed}"
        s1_live = live and max_live_agent_calls > 10 and live_calls_used < max_live_agent_calls and seed == 0
        result = run_s1_episode(
            design=design,
            corpus=corpus,
            probe_id=promoted_probe,
            seat_id="emp-A",
            run_root=s1_root,
            live=s1_live,
            model=model_name,
            knobs={"K-completion-gate": seed % 2 == 1, "K-material-picker": False, "K-sod-gate": False},
            seed=seed,
            max_agent_calls=1 if s1_live else 0,
        )
        if s1_live:
            live_calls_used += int(result.get("agent_calls", "0"))
        write_triage(s1_root)
        run_roots.append(str(s1_root))
        s1_roots.append(str(s1_root))

    s2_roots: list[str] = []
    for seed in range(s2_k):
        s2_root = campaign_root / f"s2_seed{seed}"
        remaining_calls = max(max_live_agent_calls - live_calls_used, 0)
        s2_live = live and remaining_calls > 0
        result = run_s2_world(
            design=design,
            corpus=corpus,
            run_root=s2_root,
            live=s2_live,
            model=model_name,
            knobs={"K-completion-gate": seed >= 1, "K-material-picker": seed == 2, "K-sod-gate": False},
            seed=seed,
            max_agent_calls=remaining_calls if s2_live else 0,
            anchor=False,
        )
        if s2_live:
            live_calls_used += int(result.get("agent_calls", "0"))
        write_triage(s2_root)
        run_roots.append(str(s2_root))
        s2_roots.append(str(s2_root))

    summary = {
        "campaign_root": str(campaign_root),
        "model": model_name,
        "live": live,
        "live_calls_used": live_calls_used,
        "s0_matrix_rows_generated": len(matrix),
        "s0_rows_executed": len(selected_rows),
        "s1_k": s1_k,
        "s2_k": s2_k,
        "anchor_run": str(anchor_root),
        "promoted_probe": promoted_probe,
        "s1_roots": s1_roots,
        "s2_roots": s2_roots,
        "run_roots": run_roots,
    }
    (campaign_root / "s0_results.json").write_text(json.dumps(s0_results, ensure_ascii=False, indent=2), encoding="utf-8")
    (campaign_root / "campaign_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    compliance = check_design_compliance(campaign_root=campaign_root, design=design, run_roots=[Path(path) for path in run_roots])
    summary["compliance"] = compliance
    (campaign_root / "campaign_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def check_design_compliance(*, campaign_root: Path | None, design: DesignInputs, run_roots: list[Path] | None = None) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    prompts = [role_system_prompt(seat_id, seat.role) for seat_id, seat in sorted(design.seats.items())]
    for prompt in prompts:
        lower = prompt.lower()
        for term in WORLD_PROMPT_BANNED_TERMS:
            if term.lower() in lower:
                failures.append({"check": "world_prompt_leak", "detail": term})

    for _, obj in inspect.getmembers(__import__("company_twin.tools", fromlist=["build_role_tools"])):
        if callable(obj) and obj.__doc__:
            lower = obj.__doc__.lower()
            for term in WORLD_PROMPT_BANNED_TERMS:
                if term.lower() in lower:
                    failures.append({"check": "tool_doc_leak", "detail": term})

    matrix_size = len(build_s0_matrix(design, models=[normalize_openrouter_model(None)], variants=2))
    if matrix_size == 0:
        failures.append({"check": "s0_matrix", "detail": "empty matrix"})

    if campaign_root and (campaign_root / "campaign_summary.json").exists():
        summary = json.loads((campaign_root / "campaign_summary.json").read_text(encoding="utf-8"))
        if int(summary.get("s0_rows_executed", 0)) < 2:
            failures.append({"check": "s0_executed_rows", "detail": "campaign must execute more than one S0 matrix row"})
        if int(summary.get("s1_k", 0)) < 5:
            failures.append({"check": "s1_ensemble", "detail": "S1 campaign must include K>=5 seeds"})
        if int(summary.get("s2_k", 0)) < 3:
            failures.append({"check": "s2_ensemble", "detail": "S2 campaign must include K>=3 seeds"})

    for run_root in run_roots or []:
        _check_run_bundle(run_root, failures)

    result = {"passed": not failures, "failure_count": len(failures), "failures": failures, "s0_matrix_rows": matrix_size}
    if campaign_root:
        (campaign_root / "compliance_report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _check_run_bundle(run_root: Path, failures: list[dict[str, str]]) -> None:
    required = ["config.json", "meta.json", "attempts.jsonl", "basis_records.jsonl", "chat_channel.jsonl", "world_ledger.jsonl", "store_events.jsonl", "oracle_l0.parquet", "triage/buckets.json", "triage/review.html"]
    for rel in required:
        if not (run_root / rel).exists():
            failures.append({"check": "run_bundle", "detail": f"{run_root.name} missing {rel}"})
            return
    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    for detail in assert_world_config_complete(config):
        failures.append({"check": "world_config_completeness", "detail": f"{run_root.name}: {detail}"})
    meta = json.loads((run_root / "meta.json").read_text(encoding="utf-8"))
    stage = str(meta.get("stage") or config.get("stage") or "")
    ledger = read_jsonl(run_root / "world_ledger.jsonl")
    attempts = read_jsonl(run_root / "attempts.jsonl")
    basis = read_jsonl(run_root / "basis_records.jsonl")
    store_events = read_jsonl(run_root / "store_events.jsonl")
    event_types = [row.get("event_type") for row in ledger]
    workflow_tools = {row.get("tool") for row in attempts if row.get("tool") in {"record_customer_contact", "request_approval", "approve_application", "submit_application", "verify_identity", "link_review", "complete_contract", "deliver_documents"}}
    workflow_attempts = [row for row in attempts if row.get("tool") in {"record_customer_contact", "request_approval", "approve_application", "submit_application", "verify_identity", "link_review", "complete_contract", "deliver_documents"}]
    agent_workflow_attempts = [row for row in workflow_attempts if str(row.get("origin") or "").startswith("agent")]
    unique_attempt_seats = {row.get("seat_id") for row in attempts if row.get("seat_id")}
    if stage == "S0":
        if "read_document" not in {row.get("tool") for row in attempts} or not basis:
            failures.append({"check": "s0_real_execution", "detail": f"{run_root.name} lacks search/read/basis execution"})
    if stage == "S1":
        if event_types.count("customer_event") < 1:
            failures.append({"check": "s1_customer_event", "detail": f"{run_root.name} has no customer event"})
        if len(unique_attempt_seats) < 2:
            failures.append({"check": "s1_multi_seat", "detail": f"{run_root.name} has fewer than 2 active seats"})
        if len(workflow_tools) < 3:
            failures.append({"check": "s1_workflow", "detail": f"{run_root.name} has too few workflow actions"})
        if not agent_workflow_attempts:
            failures.append({"check": "s1_agent_originated_actions", "detail": f"{run_root.name} has no agent-originated workflow action"})
    if stage == "S2":
        ticks = int(((config.get("world") or {}).get("schedule") or {}).get("ticks") or 0)
        if ticks < 40:
            failures.append({"check": "s2_ticks", "detail": f"{run_root.name} has ticks={ticks}"})
        if event_types.count("customer_event") < 10:
            failures.append({"check": "s2_customer_deck", "detail": f"{run_root.name} has too few customer/deck events"})
        if len(unique_attempt_seats) < 4:
            failures.append({"check": "s2_all_seats", "detail": f"{run_root.name} has fewer than 4 active seats"})
        if len(workflow_tools) < 5:
            failures.append({"check": "s2_workflow", "detail": f"{run_root.name} has too few workflow actions"})
        if len(agent_workflow_attempts) < 5:
            failures.append({"check": "s2_agent_originated_actions", "detail": f"{run_root.name} has too few agent-originated workflow actions"})
        if "month_end_close" not in event_types:
            failures.append({"check": "s2_month_end", "detail": f"{run_root.name} has no month-end close event"})
        if not store_events:
            failures.append({"check": "s2_d4_store", "detail": f"{run_root.name} has no private store writes"})
    if run_root.name.startswith("anchor"):
        world = config.get("world") or {}
        knobs = ((world.get("kernel_profile") or {}).get("knobs") or {})
        if not config.get("anchor"):
            failures.append({"check": "anchor_config", "detail": f"{run_root.name} anchor flag is false"})
        if any(bool(value) for value in knobs.values()):
            failures.append({"check": "anchor_config", "detail": f"{run_root.name} has enabled knobs"})
        if "completion_gate_active" in event_types:
            failures.append({"check": "anchor_runtime_purity", "detail": f"{run_root.name} activated completion gate during anchor run"})


def _diverse_s0_rows(matrix: list[S0MatrixRow], *, budget: int) -> list[S0MatrixRow]:
    selected: list[S0MatrixRow] = []
    seen_spans: set[str] = set()
    preferred_roles = ("emp-A", "emp-M", "emp-Q", "emp-C", "audit-in-world")
    for role in preferred_roles:
        for row in matrix:
            if row.seat_id == role and row.span_id not in seen_spans:
                selected.append(row)
                seen_spans.add(row.span_id)
                break
            if len(selected) >= budget:
                return selected
    for row in matrix:
        if len(selected) >= budget:
            break
        if row not in selected:
            selected.append(row)
    return selected


def _promote_probe(results: list[dict[str, Any]]) -> str | None:
    high_entropy = sorted(results, key=lambda row: float(row.get("entropy", 0)), reverse=True)
    return str(high_entropy[0]["probe_id"]) if high_entropy else None
