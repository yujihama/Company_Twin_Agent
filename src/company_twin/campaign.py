from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .acceptance import run_acceptance
from .agents import CustomerLLM, SeatFactory, load_role_card, role_system_prompt
from .corpus import Corpus
from .design_loader import DesignInputs
from .env import normalize_openrouter_model
from .harness import TurnPromptMode, _turn_prompt, run_s0, run_s1_episode, run_s2_world
from .mutations import apply_corpus_mutations, lint_mutation_catalog, mutation_specs_from_values
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

WORLD_PROMPT_BANNED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bAMB-\d+[A-Za-z0-9_-]*\b", "seeded_span_id"),
    (r"\bCONTRA-\d+[A-Za-z0-9_-]*\b", "seeded_span_id"),
    (r"\bSTR-\d+[A-Za-z0-9_-]*\b", "seeded_span_id"),
    (r"\bSCC-\d+[A-Za-z0-9_-]*\b", "seeded_span_id"),
    (r"\bprobe_id\b", "probe_id"),
    (r"\blatent_truth\b", "latent_truth"),
    (r"\bseeded\s+span\b", "seeded_span"),
    (r"\bspan\s+registry\b", "span_registry"),
)


@dataclass(frozen=True)
class S0MatrixRow:
    probe_id: str
    span_id: str
    seat_id: str
    model: str
    variant: int


DEFAULT_S0_COLD_READ_MODELS = ("openrouter:qwen/qwen3.6-flash", "openrouter:qwen/qwen3.5-9b")
CONTROL_PAIR_CAMPAIGN_SCHEMA_VERSION = "company_twin.control_pair_campaign.v1"
S0_CONTROL_PAIR_ATTRIBUTION_SCHEMA_VERSION = "company_twin.s0_control_pair_attribution.v1"


def default_s0_models(model_name: str) -> list[str]:
    configured = os.getenv("COMPANY_TWIN_S0_MODELS") or os.getenv("DEEPAGENT_S0_MODELS")
    raw_models = [item.strip() for item in configured.split(",")] if configured else []
    models = raw_models or [model_name, *DEFAULT_S0_COLD_READ_MODELS]
    normalized: list[str] = []
    for model in models:
        if not model:
            continue
        value = normalize_openrouter_model(model)
        if value not in normalized:
            normalized.append(value)
    if model_name not in normalized:
        normalized.insert(0, model_name)
    for fallback in DEFAULT_S0_COLD_READ_MODELS:
        value = normalize_openrouter_model(fallback)
        if value != model_name and value not in normalized:
            normalized.append(value)
        if len(normalized) >= 2:
            break
    return normalized


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
    prompt_mode: TurnPromptMode = "scaffold",
    model_bindings: dict[str, str] | None = None,
    scc_switch_tick: int | None = None,
    mutations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Live-only campaign: S0 battery -> divergence aggregation -> S1 ensemble -> (optional) S2 + anchor.

    Cost is controlled by stage promotion (s0_limit / s1_k / with_s2), never by
    replacing agents with scripts. There is no non-live execution path.
    """
    model_name = normalize_openrouter_model(model)
    campaign_root = root / "runs" / f"design_campaign_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    campaign_root.mkdir(parents=True, exist_ok=True)

    campaign_s0_models = [normalize_openrouter_model(model) for model in (s0_models or default_s0_models(model_name))]
    matrix = build_s0_matrix(design, models=campaign_s0_models, variants=s0_variants)
    (campaign_root / "s0_matrix.json").write_text(json.dumps([asdict(row) for row in matrix], ensure_ascii=False, indent=2), encoding="utf-8")
    selected = matrix if s0_limit is None else _diverse_s0_rows(matrix, budget=s0_limit)

    s0_results: list[dict[str, Any]] = []
    for idx, row in enumerate(selected):
        s0_root = campaign_root / f"s0_{idx:03d}_{row.probe_id}_{row.seat_id}_v{row.variant}"
        result = run_s0(design=design, corpus=corpus, probe_id=row.probe_id, seat_id=row.seat_id, run_root=s0_root, span_id=row.span_id, model=row.model, variant=row.variant, mutations=mutations, seat_factory=seat_factory)
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
        run_s1_episode(
            design=design,
            corpus=corpus,
            probe_id=promoted_probe,
            run_root=s1_root,
            model=model_name,
            knobs={},
            seed=seed,
            seat_factory=seat_factory,
            customer_llm=customer_llm,
            prompt_mode=prompt_mode,
            model_bindings=model_bindings,
            scc_switch_tick=scc_switch_tick,
            mutations=mutations,
        )
        write_triage(s1_root)
        s1_roots.append(str(s1_root))

    s2_roots: list[str] = []
    anchor_root: str | None = None
    if with_s2:
        anchor_path = campaign_root / "anchor_s2_seed0"
        run_s2_world(
            design=design,
            corpus=corpus,
            run_root=anchor_path,
            model=model_name,
            knobs={},
            seed=0,
            ticks=s2_ticks,
            anchor=True,
            seat_factory=seat_factory,
            customer_llm=customer_llm,
            prompt_mode=prompt_mode,
            model_bindings=model_bindings,
            scc_switch_tick=scc_switch_tick,
            mutations=mutations,
        )
        write_triage(anchor_path)
        anchor_root = str(anchor_path)
        for seed in range(s2_k):
            s2_root = campaign_root / f"s2_seed{seed}"
            run_s2_world(
                design=design,
                corpus=corpus,
                run_root=s2_root,
                model=model_name,
                knobs={},
                seed=seed,
                ticks=s2_ticks,
                anchor=False,
                seat_factory=seat_factory,
                customer_llm=customer_llm,
                prompt_mode=prompt_mode,
                model_bindings=model_bindings,
                scc_switch_tick=scc_switch_tick,
                mutations=mutations,
            )
            write_triage(s2_root)
            s2_roots.append(str(s2_root))

    summary = {
        "campaign_root": str(campaign_root),
        "model": model_name,
        "s0_models": campaign_s0_models,
        "model_bindings": model_bindings or {},
        "scc_switch_tick": scc_switch_tick,
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
        "prompt_mode": prompt_mode,
        "mutations": mutations or [],
    }
    aggregate_ensemble_triage(campaign_root)
    acceptance = run_acceptance(campaign_root=campaign_root, design=design, corpus=corpus, scope="full_world" if with_s2 else "s0_s1")
    summary["acceptance_passed"] = acceptance["passed"]
    (campaign_root / "campaign_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def run_control_pair_campaign(
    *,
    root: Path,
    design: DesignInputs,
    corpus: Corpus,
    manifest: dict[str, Any],
    model: str | None = None,
    probe: str = "P-01",
    stage: str = "S1",
    ticks: int = 6,
    seat_factory: SeatFactory | None = None,
    customer_llm: CustomerLLM | None = None,
    customer_llm_factory: Callable[[Path], CustomerLLM] | None = None,
    prompt_mode: TurnPromptMode = "measurement",
    model_bindings: dict[str, str] | None = None,
    scc_switch_tick: int | None = None,
    timed_notice_recipients: list[str] | None = None,
    s0_span: str | None = None,
    s0_seat: str = "emp-A",
    s0_models: list[str] | None = None,
    s0_variants: int = 2,
) -> dict[str, Any]:
    """Execute a pre-registered delta-one control-pair manifest.

    This is the WP-07 live-only path: every pair side becomes an ordinary run
    bundle, but run metadata marks it as `campaign_mode=control_pairs` so later
    aggregation can exclude exploratory bundles that happen to sit nearby.
    """
    if manifest.get("schema_version") != "company_twin.control_pairs.v1":
        raise ValueError(f"unexpected control-pair manifest schema: {manifest.get('schema_version')!r}")
    stage = stage.upper()
    if stage not in {"S0", "S1", "S2"}:
        raise ValueError("control-pair campaigns currently support stage S0, S1, or S2")
    if stage == "S1" and probe not in design.probes:
        raise ValueError(f"unknown probe for S1 control-pair campaign: {probe}")
    s0_span_id = ""
    s0_campaign_models: list[str] = []
    if stage == "S0":
        if probe not in design.probes:
            raise ValueError(f"unknown probe for S0 control-pair campaign: {probe}")
        s0_span_id = s0_span or (design.probes[probe].binds[0] if design.probes[probe].binds else "")
        if s0_span_id not in design.s0_question_templates:
            raise ValueError(f"unknown or untemplated S0 span: {s0_span_id}")
        if s0_seat not in design.seats:
            raise ValueError(f"unknown S0 seat: {s0_seat}")
        if s0_variants < 1:
            raise ValueError("s0_variants must be >= 1")
        s0_campaign_models = [normalize_openrouter_model(item) for item in (s0_models or default_s0_models(normalize_openrouter_model(model)))]
    if ticks < 1:
        raise ValueError("ticks must be >= 1")

    pairs = manifest.get("pairs") or []
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("control-pair manifest must contain at least one pair")

    model_name = normalize_openrouter_model(model)
    campaign_root = root / "runs" / f"control_pair_campaign_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    campaign_root.mkdir(parents=True, exist_ok=True)
    manifest_path = campaign_root / "control_pair_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    run_rows: list[dict[str, Any]] = []
    s0_rows: list[dict[str, Any]] = []
    for pair in pairs:
        normalized_pair = _validate_control_pair(pair)
        for condition in ("control", "treatment"):
            side = normalized_pair[condition]
            mutation_ids = list(side["mutations"])
            specs = mutation_specs_from_values(design.root, mutation_ids)
            mutation_result = apply_corpus_mutations(corpus, specs)
            if stage == "S0":
                for s0_model in s0_campaign_models:
                    for variant in range(s0_variants):
                        run_root = campaign_root / f"{normalized_pair['pair_id']}_{condition}_s0_{_run_slug(s0_model)}_v{variant}"
                        result = run_s0(
                            design=design,
                            corpus=mutation_result.corpus,
                            probe_id=probe,
                            seat_id=s0_seat,
                            run_root=run_root,
                            span_id=s0_span_id,
                            model=s0_model,
                            variant=variant,
                            mutations=mutation_result.applied,
                            seat_factory=seat_factory,
                        )
                        _stamp_control_pair_meta(
                            run_root,
                            pair=normalized_pair,
                            condition=condition,
                            mutation_ids=mutation_ids,
                            applied_mutations=mutation_result.applied,
                        )
                        write_triage(run_root)
                        row = {
                            "pair_id": normalized_pair["pair_id"],
                            "condition": condition,
                            "run_root": str(run_root),
                            "seed": side["seed"],
                            "mutations": mutation_ids,
                            "mutation_hash": mutation_result.mutation_hash,
                            "stage": stage,
                            "probe": probe,
                            "span_id": s0_span_id,
                            "seat_id": s0_seat,
                            "model": s0_model,
                            "variant": variant,
                            "result": result,
                            **result,
                        }
                        s0_rows.append(row)
                        run_rows.append(row)
                continue
            run_root = campaign_root / f"{normalized_pair['pair_id']}_{condition}"
            per_run_customer = customer_llm_factory(run_root) if customer_llm_factory is not None else customer_llm
            common = {
                "design": design,
                "corpus": mutation_result.corpus,
                "run_root": run_root,
                "model": model_name,
                "knobs": side["knobs"],
                "seed": side["seed"],
                "seat_factory": seat_factory,
                "customer_llm": per_run_customer,
                "prompt_mode": prompt_mode,
                "model_bindings": model_bindings,
                "scc_switch_tick": scc_switch_tick,
                "mutations": mutation_result.applied,
                "timed_notice_recipients": [] if timed_notice_recipients is None else timed_notice_recipients,
            }
            if stage == "S1":
                result = run_s1_episode(probe_id=probe, ticks=ticks, **common)
            else:
                result = run_s2_world(ticks=ticks, anchor=False, **common)
            _stamp_control_pair_meta(
                run_root,
                pair=normalized_pair,
                condition=condition,
                mutation_ids=mutation_ids,
                applied_mutations=mutation_result.applied,
            )
            write_triage(run_root)
            run_rows.append(
                {
                    "pair_id": normalized_pair["pair_id"],
                    "condition": condition,
                    "run_root": str(run_root),
                    "seed": side["seed"],
                    "mutations": mutation_ids,
                    "mutation_hash": mutation_result.mutation_hash,
                    "stage": stage,
                    "probe": probe if stage == "S1" else None,
                    "result": result,
                }
            )

    ensemble: dict[str, Any] = {}
    s0_endpoint: dict[str, Any] | None = None
    if stage == "S0":
        s0_endpoint = aggregate_s0_control_pair_attribution(design, s0_rows, campaign_root=campaign_root)
    else:
        ensemble = aggregate_ensemble_triage(campaign_root)
    summary = {
        "schema_version": CONTROL_PAIR_CAMPAIGN_SCHEMA_VERSION,
        "campaign_root": str(campaign_root),
        "manifest_path": str(manifest_path),
        "stage": stage,
        "probe": probe if stage in {"S0", "S1"} else None,
        "ticks": ticks,
        "model": model_name,
        "prompt_mode": prompt_mode,
        "timed_notice_recipients": [] if timed_notice_recipients is None else timed_notice_recipients,
        "s0_endpoint": {
            "span_id": s0_span_id,
            "seat_id": s0_seat,
            "models": s0_campaign_models,
            "variants": s0_variants,
            "rows": len((s0_endpoint or {}).get("rows") or []),
            "path": "s0_attribution_table.json",
        }
        if stage == "S0"
        else None,
        "pair_count": len(pairs),
        "condition_run_count": len(run_rows),
        "runs": run_rows,
        "ensemble_triage": None if stage == "S0" else {
            "groups": len(ensemble.get("groups") or []),
            "attribution_rows": len(ensemble.get("attribution_table") or []),
            "icc_summary": ensemble.get("icc_summary"),
        },
        "note": "WP-07b S0 endpoint only; S1/S2 L0 attribution, harness acceptance, and Stage 9 readiness remain separate gates."
        if stage == "S0"
        else "WP-07 control-pair execution only; harness acceptance and Stage 9 readiness remain separate gates.",
    }
    (campaign_root / "control_pair_campaign_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def _run_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return slug[:80] or "model"


def _validate_control_pair(pair: dict[str, Any]) -> dict[str, Any]:
    pair_id = str(pair.get("pair_id") or "")
    if not pair_id:
        raise ValueError("control-pair entry missing pair_id")
    if pair.get("delta") != "world.corpus.mutations":
        raise ValueError(f"{pair_id}: unsupported delta {pair.get('delta')!r}")
    normalized: dict[str, Any] = {
        "pair_id": pair_id,
        "delta": "world.corpus.mutations",
        "shared": pair.get("shared") or {},
    }
    seeds: list[int] = []
    for condition in ("control", "treatment"):
        side = pair.get(condition) or {}
        if not isinstance(side, dict):
            raise ValueError(f"{pair_id}: {condition} side must be an object")
        seed = int(side.get("seed"))
        mutations = [str(item) for item in (side.get("mutations") or [])]
        knobs = {str(key): bool(value) for key, value in (side.get("knobs") or {}).items()}
        normalized[condition] = {"seed": seed, "mutations": mutations, "knobs": knobs}
        seeds.append(seed)
    if len(set(seeds)) != 1 or (pair.get("seed") is not None and int(pair["seed"]) != seeds[0]):
        raise ValueError(f"{pair_id}: control and treatment must share the same seed")
    shared = normalized["shared"]
    if shared and len({int(value) for value in shared.values()}) != 1:
        raise ValueError(f"{pair_id}: shared seed fields must all match")
    if normalized["control"]["mutations"]:
        raise ValueError(f"{pair_id}: control side must not carry mutations")
    if len(normalized["treatment"]["mutations"]) != 1:
        raise ValueError(f"{pair_id}: treatment side must carry exactly one mutation for delta-one attribution")
    normalized["seed"] = seeds[0]
    return normalized


def _stamp_control_pair_meta(
    run_root: Path,
    *,
    pair: dict[str, Any],
    condition: str,
    mutation_ids: list[str],
    applied_mutations: list[dict[str, Any]],
) -> None:
    meta_path = run_root / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta["campaign_mode"] = "control_pairs"
    meta["control_pair"] = {
        "pair_id": pair["pair_id"],
        "condition": condition,
        "delta": pair["delta"],
        "seed": pair["seed"],
        "shared": pair.get("shared") or {},
        "planned_mutation_ids": mutation_ids,
    }
    meta["mutation_ids"] = mutation_ids
    meta["mutations"] = applied_mutations
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# S0 divergence aggregation (span x role cells over live answers)
# ---------------------------------------------------------------------------

def aggregate_s0_control_pair_attribution(design: DesignInputs, s0_results: list[dict[str, Any]], *, campaign_root: Path | None = None) -> dict[str, Any]:
    role_of = {seat_id: seat.role for seat_id, seat in design.seats.items()}
    observations: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
    all_live = True
    included_run_ids: list[str] = []
    for row in s0_results:
        condition = str(row.get("condition") or "")
        if condition not in {"control", "treatment"}:
            continue
        run_root = Path(str(row.get("run_root") or ""))
        if run_root:
            included_run_ids.append(run_root.name)
        meta_path = run_root / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not meta.get("live"):
                all_live = False
        span_id = str(row.get("span_id") or "")
        role = role_of.get(str(row.get("seat_id") or ""), "unknown")
        candidates = _s0_candidates_for_row(design, span_id, row)
        enriched = {
            **row,
            "role": role,
            "cluster": _s0_cluster(row, candidates),
            "observation_key": _s0_observation_key(row),
            "parsed": bool(row.get("parsed")),
        }
        observations.setdefault((span_id, role), {"control": [], "treatment": []})[condition].append(enriched)

    rows: list[dict[str, Any]] = []
    for (span_id, role), by_condition in sorted(observations.items()):
        left = _s0_condition_summary(by_condition.get("control") or [])
        right = _s0_condition_summary(by_condition.get("treatment") or [])
        all_clusters = sorted(set(left["cluster_distribution"]) | set(right["cluster_distribution"]))
        cluster_delta = {
            cluster: round(right["cluster_distribution"].get(cluster, 0.0) - left["cluster_distribution"].get(cluster, 0.0), 4)
            for cluster in all_clusters
        }
        total_variation = 0.5 * sum(abs(cluster_delta[cluster]) for cluster in all_clusters)
        left_keys = sorted(obs["observation_key"] for obs in by_condition.get("control") or [])
        right_keys = sorted(obs["observation_key"] for obs in by_condition.get("treatment") or [])
        row = {
            "status": "candidate",
            "endpoint": "s0_interpretation_entropy_and_cluster_shift",
            "delta": "world.corpus.mutations",
            "left_value": [],
            "right_value": sorted({mutation for obs in by_condition.get("treatment") or [] for mutation in (obs.get("mutations") or [])}),
            "span_id": span_id,
            "role": role,
            "left": left,
            "right": right,
            "entropy_delta": round(right["entropy"] - left["entropy"], 4),
            "cluster_shift_total_variation": round(total_variation, 4),
            "cluster_distribution_delta": cluster_delta,
            "observation_bundle_match": bool(left_keys) and left_keys == right_keys,
            "left_observation_keys": left_keys,
            "right_observation_keys": right_keys,
            "dominant_cluster_shifted": left["dominant_cluster"] != right["dominant_cluster"],
        }
        rows.append(row)

    payload = {
        "schema_version": S0_CONTROL_PAIR_ATTRIBUTION_SCHEMA_VERSION,
        "endpoint": "s0_interpretation_entropy_and_cluster_shift",
        "run_filter": {
            "mode": "control_pairs_s0",
            "included_run_count": len(included_run_ids),
            "included_run_ids": included_run_ids,
        },
        "all_answers_live": all_live,
        "rows": rows,
        "note": "S0 attribution compares interpretation entropy and cluster distributions. It is a screening endpoint and does not confirm S1/S2 action conversion.",
    }
    if campaign_root is not None:
        (campaign_root / "s0_attribution_table.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


ENTROPY_EXCLUDED_CLUSTERS = frozenset({"unparsed"})
# Late candidates should not relabel historical rows that lack a run-time snapshot.
S0_ROW_OPT_IN_CANDIDATES = frozenset({("CONTRA-01", "split_by_topic")})
S0_PROBE_SCOPED_CANDIDATES: dict[tuple[str, str], frozenset[str]] = {
    ("CONTRA-01", "split_by_topic"): frozenset({"P-09"}),
}


def _entropy_clusters(clusters: Counter[str]) -> Counter[str]:
    return Counter({cluster: count for cluster, count in clusters.items() if cluster not in ENTROPY_EXCLUDED_CLUSTERS})


def _entropy_excluded_clusters(clusters: Counter[str]) -> dict[str, int]:
    return {cluster: count for cluster, count in sorted(clusters.items()) if cluster in ENTROPY_EXCLUDED_CLUSTERS}


def _s0_condition_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    clusters = Counter(str(row.get("cluster") or "novel_or_unclassified") for row in rows)
    entropy_clusters = _entropy_clusters(clusters)
    total = sum(clusters.values())
    distribution = {cluster: round(count / total, 4) for cluster, count in sorted(clusters.items())} if total else {}
    dominant = clusters.most_common(1)[0][0] if clusters else None
    return {
        "answer_count": total,
        "parsed_answers": sum(1 for row in rows if row.get("parsed")),
        "parsed_rate": round(sum(1 for row in rows if row.get("parsed")) / total, 4) if total else 0.0,
        "seed_values": sorted({int(row.get("seed") or 0) for row in rows}),
        "model_values": sorted({str(row.get("model") or "") for row in rows if row.get("model")}),
        "variant_values": sorted({int(row.get("variant") or 0) for row in rows}),
        "clusters": dict(clusters),
        "cluster_distribution": distribution,
        "dominant_cluster": dominant,
        "entropy": round(entropy(entropy_clusters), 4),
        "entropy_clusters": dict(entropy_clusters),
        "entropy_excluded_clusters": _entropy_excluded_clusters(clusters),
    }


def _s0_observation_key(row: dict[str, Any]) -> str:
    return f"seed={int(row.get('seed') or 0)}|model={row.get('model') or ''}|variant={int(row.get('variant') or 0)}"

def aggregate_s0_divergence(design: DesignInputs, s0_results: list[dict[str, Any]], *, campaign_root: Path | None = None) -> dict[str, Any]:
    role_of = {seat_id: seat.role for seat_id, seat in design.seats.items()}
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    all_live = True
    for row in s0_results:
        if not row.get("response") and row.get("outcome") != "recursion_exhausted" and row.get("parsed") is not False:
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
        clusters = Counter(_s0_cluster(row, _s0_candidates_for_row(design, span_id, row)) for row in rows)
        entropy_clusters = _entropy_clusters(clusters)
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
                "entropy": round(entropy(entropy_clusters), 4),
                "entropy_clusters": dict(entropy_clusters),
                "entropy_excluded_clusters": _entropy_excluded_clusters(clusters),
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


def _s0_cluster(row: dict[str, Any], candidates: dict[str, str]) -> str:
    if row.get("outcome") == "recursion_exhausted":
        return "no_grounded_answer"
    if row.get("parsed") is False:
        return "unparsed"
    return classify_answer(_answer_text(row), candidates)


def _s0_candidates_for_row(design: DesignInputs, span_id: str, row: dict[str, Any]) -> dict[str, str]:
    candidates = dict(design.spans[span_id].candidates) if span_id in design.spans else {}
    probe_id = str(row.get("probe_id") or "")
    row_candidate_ids = {str(candidate_id) for candidate_id in row.get("candidate_ids", [])}
    for scoped_span_id, candidate_id in S0_ROW_OPT_IN_CANDIDATES:
        if span_id == scoped_span_id and candidate_id not in row_candidate_ids:
            candidates.pop(candidate_id, None)
    for (scoped_span_id, candidate_id), allowed_probes in S0_PROBE_SCOPED_CANDIDATES.items():
        if span_id == scoped_span_id and probe_id not in allowed_probes:
            candidates.pop(candidate_id, None)
    return candidates


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
    for failure in lint_mutation_catalog(design.root):
        failures.append({"check": "mutation_visible_text_leak", "detail": f"{failure['mutation_id']}: {failure['label']}"})
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
    turn_prompt = _turn_prompt(
        tick=1,
        ticks=6,
        budget_left=5,
        messages=[
            {
                "kind": "customer_utterance",
                "tick": 1,
                "event_id": "EVT-LINT",
                "customer_id": "CUS-LINT",
                "application_id": "APP-LINT",
                "product": "商品",
                "deadline_display": "本日中",
                "utterance": "手続きの進め方を確認したいです。",
            }
        ],
    )
    failures.extend(_world_prompt_leak_failures("turn_prompt", turn_prompt))
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


def _world_prompt_leak_failures(surface: str, text: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for pattern, label in WORLD_PROMPT_BANNED_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            failures.append({"check": "world_prompt_leak", "detail": f"{surface}: {label}: {pattern}"})
    lower = text.lower()
    for term in WORLD_PROMPT_BANNED_TERMS:
        if term.lower() in lower:
            failures.append({"check": "world_prompt_leak", "detail": f"{surface}: {term}"})
    return failures
