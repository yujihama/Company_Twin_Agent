from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .acceptance import run_acceptance
from .agents import CustomerLLM, SeatFactory, load_role_card, role_system_prompt
from .corpus import Corpus
from .design_loader import DesignInputs
from .env import normalize_openrouter_model
from .harness import run_s0, run_s1_episode, run_s2_world
from .oracles import aggregate_ensemble_triage, write_triage

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
    "プローブ",
    "probe",
)

PRODUCTION_SOURCE_BANNED_SYMBOLS = (
    "agent" + "_policy",
    "run" + "_policy" + "_seat" + "_turn",
    "_handle" + "_customer" + "_utterance",
    "_handle" + "_application" + "_work",
    "policy" + "_fixture",
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
    model: str | None = None,
    s0_models: list[str] | None = None,
    s0_variants: int = 2,
    s0_limit: int | None = None,
    s1_probe: str | None = None,
    s1_k: int = 3,
    with_s2: bool = False,
    s2_k: int = 1,
    s2_ticks: int = 40,
    seat_factory: SeatFactory | None = None,
    customer_llm: CustomerLLM | None = None,
) -> dict[str, Any]:
    """Live-only campaign: S0 battery -> divergence aggregation -> S1 ensemble -> (optional) S2 + anchor.

    Cost is controlled by stage promotion (s0_limit / s1_k / with_s2), never by
    replacing agents with scripts. There is no non-live execution path.
    """
    model_name = normalize_openrouter_model(model)
    campaign_root = root / "runs" / f"design_campaign_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    campaign_root.mkdir(parents=True, exist_ok=True)

    matrix = build_s0_matrix(design, models=s0_models or [model_name], variants=s0_variants)
    (campaign_root / "s0_matrix.json").write_text(json.dumps([asdict(row) for row in matrix], ensure_ascii=False, indent=2), encoding="utf-8")
    selected = matrix if s0_limit is None else _diverse_s0_rows(matrix, budget=s0_limit)

    s0_results: list[dict[str, Any]] = []
    for idx, row in enumerate(selected):
        s0_root = campaign_root / f"s0_{idx:03d}_{row.probe_id}_{row.seat_id}_v{row.variant}"
        result = run_s0(design=design, corpus=corpus, probe_id=row.probe_id, seat_id=row.seat_id, run_root=s0_root, span_id=row.span_id, model=row.model, variant=row.variant, seat_factory=seat_factory)
        write_triage(s0_root)
        s0_results.append({**asdict(row), **result})
    (campaign_root / "s0_results.json").write_text(json.dumps(s0_results, ensure_ascii=False, indent=2), encoding="utf-8")

    divergence = aggregate_s0_divergence(design, s0_results, campaign_root=campaign_root)

    if s1_probe:
        promoted_probe = s1_probe
        promotion_reason: dict[str, Any] = {"mode": "explicit", "probe_id": s1_probe}
    else:
        promoted = _promote_probe(divergence)
        if promoted is None:
            raise RuntimeError("S0 did not produce any promotion-eligible divergence cell; refusing fixed-probe fallback")
        promoted_probe = str(promoted["probe_id"])
        promotion_reason = promoted
    s1_roots: list[str] = []
    for seed in range(s1_k):
        s1_root = campaign_root / f"s1_{promoted_probe}_seed{seed}"
        run_s1_episode(design=design, corpus=corpus, probe_id=promoted_probe, run_root=s1_root, model=model_name, knobs={}, seed=seed, seat_factory=seat_factory, customer_llm=customer_llm)
        write_triage(s1_root)
        s1_roots.append(str(s1_root))

    s2_roots: list[str] = []
    anchor_root: str | None = None
    if with_s2:
        anchor_path = campaign_root / "anchor_s2_seed0"
        run_s2_world(design=design, corpus=corpus, run_root=anchor_path, model=model_name, knobs={}, seed=0, ticks=s2_ticks, anchor=True, seat_factory=seat_factory, customer_llm=customer_llm)
        write_triage(anchor_path)
        anchor_root = str(anchor_path)
        for seed in range(s2_k):
            s2_root = campaign_root / f"s2_seed{seed}"
            run_s2_world(design=design, corpus=corpus, run_root=s2_root, model=model_name, knobs={}, seed=seed, ticks=s2_ticks, anchor=False, seat_factory=seat_factory, customer_llm=customer_llm)
            write_triage(s2_root)
            s2_roots.append(str(s2_root))

    summary = {
        "campaign_root": str(campaign_root),
        "model": model_name,
        "s0_matrix_rows_generated": len(matrix),
        "s0_rows_executed": len(selected),
        "promoted_probe": promoted_probe,
        "promotion_reason": promotion_reason,
        "s1_k": s1_k,
        "s1_roots": s1_roots,
        "with_s2": with_s2,
        "s2_k": s2_k if with_s2 else 0,
        "s2_roots": s2_roots,
        "anchor_run": anchor_root,
    }
    aggregate_ensemble_triage(campaign_root)
    acceptance = run_acceptance(campaign_root=campaign_root, design=design, corpus=corpus, scope="full_world" if with_s2 else "s0_s1")
    summary["acceptance_passed"] = acceptance["passed"]
    (campaign_root / "campaign_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


# ---------------------------------------------------------------------------
# S0 divergence aggregation (span x role cells over live answers)
# ---------------------------------------------------------------------------

def aggregate_s0_divergence(design: DesignInputs, s0_results: list[dict[str, Any]], *, campaign_root: Path | None = None) -> dict[str, Any]:
    role_of = {seat_id: seat.role for seat_id, seat in design.seats.items()}
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    all_live = True
    for row in s0_results:
        if not row.get("response"):
            continue
        meta_path = Path(str(row.get("run_root") or "")) / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not meta.get("live"):
                all_live = False
        span_id = str(row.get("span_id") or "")
        role = role_of.get(str(row.get("seat_id") or ""), "unknown")
        cells.setdefault((span_id, role), []).append(row)
    out_cells: list[dict[str, Any]] = []
    for (span_id, role), rows in sorted(cells.items()):
        candidates = design.spans[span_id].candidates if span_id in design.spans else {}
        clusters = Counter(classify_answer(_answer_text(row), candidates) for row in rows)
        probe_counts = Counter(str(row.get("probe_id") or "") for row in rows if row.get("probe_id"))
        primary_probe_id = probe_counts.most_common(1)[0][0] if probe_counts else ""
        span_consistent = all(str(row.get("span_id_from_run") or row.get("span_id") or "") == span_id for row in rows)
        parsed_answers = sum(1 for row in rows if row.get("parsed") is True)
        novel_count = int(clusters.get("novel_or_unclassified", 0))
        out_cells.append(
            {
                "span_id": span_id,
                "role": role,
                "probe_ids": sorted(probe_counts),
                "probe_counts": dict(probe_counts),
                "primary_probe_id": primary_probe_id,
                "answers": len(rows),
                "answer_count": len(rows),
                "parsed_answers": parsed_answers,
                "parsed_rate": round(parsed_answers / len(rows), 4) if rows else 0.0,
                "model_count": len({row.get("model") for row in rows}),
                "variant_count": len({row.get("variant") for row in rows}),
                "span_specific": span_consistent,
                "clusters": dict(clusters),
                "machine_clusters": dict(clusters),
                "human_confirmed_class": None,
                "novel_count": novel_count,
                "human_review_required": novel_count > 0,
                "entropy": round(entropy(clusters), 4),
            }
        )
    human_review_queue = [
        {
            "span_id": cell["span_id"],
            "role": cell["role"],
            "primary_probe_id": cell["primary_probe_id"],
            "novel_count": cell["novel_count"],
            "machine_clusters": cell["machine_clusters"],
        }
        for cell in out_cells
        if cell["human_review_required"]
    ]
    payload = {
        "cells": out_cells,
        "all_answers_live": all_live,
        "answer_total": sum(cell["answers"] for cell in out_cells),
        "human_review_queue": human_review_queue,
        "novel_status": "machine_candidate_only_until_human_confirmed",
    }
    if campaign_root is not None:
        (campaign_root / "s0_divergence.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _answer_text(row: dict[str, Any]) -> str:
    parts = [str(row.get("likely_reading") or ""), str(row.get("required_approver_or_evidence") or ""), str(row.get("next_action") or "")]
    joined = " ".join(part for part in parts if part)
    return joined or str(row.get("response") or "")


def classify_answer(answer: str, candidates: dict[str, str]) -> str:
    lowered = answer.lower()
    best_key, best_score = "", 0
    for key, text in candidates.items():
        score = _overlap_score(text, answer)
        if score > best_score:
            best_key, best_score = key, score
    if best_key and best_score >= 2:
        return best_key
    if "第二線" in answer:
        return "second_line_route"
    if "管理者" in answer:
        return "manager_route"
    if "同意" in answer or "録音" in answer or "証跡" in answer:
        return "evidence_first"
    return "novel_or_unclassified"


def _overlap_score(candidate: str, answer: str) -> int:
    """Character-bigram overlap over Japanese/word tokens; robust without a segmenter."""
    grams: set[str] = set()
    for token in _tokenize(candidate):
        if re.fullmatch(r"[A-Za-z0-9_-]+", token):
            grams.add(token.lower())
            continue
        grams.update(token[idx : idx + 2] for idx in range(len(token) - 1))
    lowered = answer.lower()
    return sum(1 for gram in grams if gram in lowered or gram in answer)


def entropy(counts: Counter[str]) -> float:
    total = sum(counts.values())
    if total == 0:
        return 0.0
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_-]+|[\u3040-\u30ff\u3400-\u9fff]{2,}", text)


def _promote_probe(divergence: dict[str, Any]) -> dict[str, Any] | None:
    """Choose the S1 probe from measured S0 divergence, never from row order."""
    candidates: list[dict[str, Any]] = []
    for cell in divergence.get("cells") or []:
        probe_id = str(cell.get("primary_probe_id") or "")
        if not probe_id:
            continue
        answers = int(cell.get("answer_count") or cell.get("answers") or 0)
        if answers < 2:
            continue
        parsed_rate = float(cell.get("parsed_rate") or 0.0)
        entropy_value = float(cell.get("entropy") or 0.0)
        novel_count = int(cell.get("novel_count") or 0)
        model_count = int(cell.get("model_count") or 0)
        variant_count = int(cell.get("variant_count") or 0)
        if parsed_rate <= 0 and entropy_value <= 0 and novel_count <= 0:
            continue
        candidates.append(
            {
                "mode": "s0_divergence",
                "probe_id": probe_id,
                "span_id": cell.get("span_id"),
                "role": cell.get("role"),
                "entropy": entropy_value,
                "novel_count": novel_count,
                "parsed_rate": parsed_rate,
                "model_count": model_count,
                "variant_count": variant_count,
                "answer_count": answers,
                "reason": "novel_or_unclassified" if novel_count > 0 else "max_entropy",
            }
        )
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            item["novel_count"] > 0,
            item["entropy"],
            item["model_count"] >= 2 and item["variant_count"] >= 2,
            item["parsed_rate"],
            item["answer_count"],
            str(item["probe_id"]),
        ),
        reverse=True,
    )
    return candidates[0]


def _diverse_s0_rows(matrix: list[S0MatrixRow], *, budget: int) -> list[S0MatrixRow]:
    """Select rows cell-complete: whole (probe,span,seat) cells at a time so every
    executed cell carries its full model x variant set (reviewer Major 3)."""
    cells: dict[tuple[str, str, str], list[S0MatrixRow]] = {}
    for row in matrix:
        cells.setdefault((row.probe_id, row.span_id, row.seat_id), []).append(row)
    selected: list[S0MatrixRow] = []
    for _, rows in sorted(cells.items()):
        if len(selected) + len(rows) > budget and selected:
            break
        selected.extend(rows)
        if len(selected) >= budget:
            break
    return selected or matrix[:budget]


# ---------------------------------------------------------------------------
# Static lint (NOT acceptance): world-surface vocabulary hygiene.
# ---------------------------------------------------------------------------

def static_world_surface_lint(design: DesignInputs) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    for role in ("sales", "application", "manager", "second_line", "audit"):
        card = load_role_card(design.root, role)
        prompt = role_system_prompt(f"emp-X", role, role_card=card).lower()
        for term in WORLD_PROMPT_BANNED_TERMS:
            if term.lower() in prompt:
                failures.append({"check": "role_prompt_leak", "detail": f"{role}: {term}"})
        for span_prefix in ("AMB-", "CONTRA-", "STR-", "SCC-"):
            if span_prefix.lower() in card.lower():
                failures.append({"check": "role_card_span_leak", "detail": f"{role}: {span_prefix}"})
        if "dfh-sal-" in card.lower():
            failures.append({"check": "role_card_doc_reference", "detail": role})
    import company_twin.tools as tools_module
    import inspect

    for _, obj in inspect.getmembers(tools_module):
        if callable(obj) and getattr(obj, "__doc__", None):
            lower = obj.__doc__.lower()
            for term in WORLD_PROMPT_BANNED_TERMS:
                if term.lower() in lower:
                    failures.append({"check": "tool_doc_leak", "detail": term})
    for path in sorted((design.root / "src" / "company_twin").glob("*.py")):
        if path.name == "campaign.py":
            continue
        text = path.read_text(encoding="utf-8")
        for symbol in PRODUCTION_SOURCE_BANNED_SYMBOLS:
            if symbol in text:
                failures.append({"check": "production_source_banned_symbol", "detail": f"{path.name}: {symbol}"})
    return {"passed": not failures, "failures": failures}
