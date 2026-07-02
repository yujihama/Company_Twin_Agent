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
from .harness import make_run_root, run_s0, run_s1_episode, run_s2_world
from .oracles import write_triage
from .tools import build_role_tools


WORLD_PROMPT_BANNED_TERMS = ("experiment", "evaluation", "fuzzing", "seeded span", "hidden test", "実験", "評価", "seeded", "span registry")


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
) -> dict[str, Any]:
    model_name = normalize_openrouter_model(model)
    campaign_root = root / "runs" / f"design_compliance_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    campaign_root.mkdir(parents=True, exist_ok=True)
    matrix = build_s0_matrix(design, models=[model_name], variants=2)
    (campaign_root / "s0_matrix.json").write_text(json.dumps([asdict(row) for row in matrix], ensure_ascii=False, indent=2), encoding="utf-8")

    run_roots: list[str] = []
    live_calls_used = 0

    anchor_root = campaign_root / "anchor_s2_seed0"
    run_s2_world(design=design, corpus=corpus, run_root=anchor_root, live=False, model=model_name, knobs={}, seed=0, max_agent_calls=0)
    write_triage(anchor_root)
    run_roots.append(str(anchor_root))

    first = matrix[0]
    s0_root = campaign_root / f"s0_{first.probe_id}_{first.seat_id}_seed0"
    s0_live = live and live_calls_used < max_live_agent_calls
    run_s0(design=design, corpus=corpus, probe_id=first.probe_id, seat_id=first.seat_id, run_root=s0_root, live=s0_live, model=first.model)
    if s0_live:
        live_calls_used += 1
    write_triage(s0_root)
    run_roots.append(str(s0_root))

    s1_root = campaign_root / "s1_P-04_emp-A_seed0"
    s1_live = live and live_calls_used < max_live_agent_calls
    run_s1_episode(
        design=design,
        corpus=corpus,
        probe_id="P-04",
        seat_id="emp-A",
        run_root=s1_root,
        live=s1_live,
        model=model_name,
        knobs={"K-completion-gate": True, "K-material-picker": False},
    )
    if s1_live:
        live_calls_used += 1
    write_triage(s1_root)
    run_roots.append(str(s1_root))

    s2_root = campaign_root / "s2_all_seats_seed0"
    remaining_calls = max(max_live_agent_calls - live_calls_used, 0)
    s2_live = live and remaining_calls > 0
    s2_summary = run_s2_world(
        design=design,
        corpus=corpus,
        run_root=s2_root,
        live=s2_live,
        model=model_name,
        knobs={"K-completion-gate": True, "K-material-picker": True},
        seed=0,
        max_agent_calls=remaining_calls if s2_live else 0,
    )
    write_triage(s2_root)
    run_roots.append(str(s2_root))

    compliance = check_design_compliance(campaign_root=campaign_root, design=design, run_roots=[Path(path) for path in run_roots])
    summary = {
        "campaign_root": str(campaign_root),
        "model": model_name,
        "live": live,
        "live_calls_used": live_calls_used + int(s2_summary.get("agent_calls", "0")),
        "s0_matrix_rows": len(matrix),
        "run_roots": run_roots,
        "compliance": compliance,
    }
    (campaign_root / "campaign_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def check_design_compliance(*, campaign_root: Path | None, design: DesignInputs, run_roots: list[Path] | None = None) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    prompts = [role_system_prompt("emp-A", "sales")]
    for prompt in prompts:
        lower = prompt.lower()
        for term in WORLD_PROMPT_BANNED_TERMS:
            if term.lower() in lower:
                failures.append({"check": "world_prompt_leak", "detail": term})

    # Tool docstrings are model-visible through LangChain tool schemas.
    dummy_docs = []
    for name, obj in inspect.getmembers(__import__("company_twin.tools", fromlist=["build_role_tools"])):
        if callable(obj) and obj.__doc__:
            dummy_docs.append(obj.__doc__)
    for doc in dummy_docs:
        lower = doc.lower()
        for term in WORLD_PROMPT_BANNED_TERMS:
            if term.lower() in lower:
                failures.append({"check": "tool_doc_leak", "detail": term})

    matrix_size = len(build_s0_matrix(design, models=[normalize_openrouter_model(None)], variants=2))
    if matrix_size == 0:
        failures.append({"check": "s0_matrix", "detail": "empty matrix"})

    for run_root in run_roots or []:
        required = ["config.json", "meta.json", "attempts.jsonl", "basis_records.jsonl", "chat_channel.jsonl", "world_ledger.jsonl", "oracle_l0.parquet", "triage/buckets.json", "triage/review.html"]
        for rel in required:
            if not (run_root / rel).exists():
                failures.append({"check": "run_bundle", "detail": f"{run_root.name} missing {rel}"})

    result = {"passed": not failures, "failure_count": len(failures), "failures": failures, "s0_matrix_rows": matrix_size}
    if campaign_root:
        (campaign_root / "compliance_report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
