from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from .acceptance import run_acceptance
from .agents import openrouter_ready
from .ab_testing import write_prompt_mode_ab_report
from .backcasting import extract_backcasting_cases, write_backcasting_inputs, write_backcasting_report
from .backcasting_run import (
    BackcastingInputsError,
    LocalReproductionJudge,
    OpenRouterReproductionJudge,
    run_backcasting_resimulation,
)
from .campaign import run_control_pair_campaign, run_design_campaign, static_world_surface_lint
from .corpus import Corpus
from .design_loader import load_design
from .env import load_local_env, normalize_openrouter_model
from .evidence_manifest import write_stage9_evidence_manifest
from .harness import make_run_root, run_s0, run_s1_episode, run_s2_world
from .holdout import build_holdout_injection_plan, write_holdout_inputs, write_holdout_report
from .mutations import apply_corpus_mutations, build_delta_one_pair_manifest, lint_mutation_specs, load_mutation_catalog, mutation_specs_from_values
from .oracles import execute_fresh_min_repro_confirmation, execute_min_repro_jobs, write_triage
from .parallel_runner import (
    BatchSpec,
    BatchSpecError,
    RATE_LIMIT_WARN_THRESHOLD,
    build_retry_spec,
    delete_partial_roots,
    load_batch_manifest,
    run_batch,
    validate_batch_spec,
)
from .readiness import run_readiness_gate, write_readiness_reports
from .semantic_grounding import (
    LocalSemanticJudge,
    OpenRouterSemanticJudge,
    evaluate_semantic_grounding_campaign,
    evaluate_semantic_grounding_run,
    export_g3_calibration_samples,
    score_g3_calibration_file,
)
from .sme_blind_review import build_blind_review_packet, write_sme_blind_review_inputs, write_sme_blind_review_report

app = typer.Typer(no_args_is_help=True)


def _echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=True, indent=2))


def _root(path: Path | None = None) -> Path:
    root = (path or Path.cwd()).resolve()
    load_local_env(root)
    return root


def _require_live(base: Path) -> None:
    ready, detail = openrouter_ready(base)
    if not ready:
        raise typer.BadParameter(f"live execution is required for all runs; {detail}")


def _seat_model_bindings(values: list[str] | None) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise typer.BadParameter(f"--seat-model must be seat_id=model, got {value!r}")
        seat_id, model = value.split("=", 1)
        seat_id = seat_id.strip()
        model = model.strip()
        if not seat_id or not model:
            raise typer.BadParameter(f"--seat-model must be seat_id=model, got {value!r}")
        bindings[seat_id] = normalize_openrouter_model(model)
    return bindings


def _corpus_with_mutations(base: Path, design, values: list[str] | None) -> tuple[Corpus, list[dict]]:
    corpus = Corpus.from_design(design)
    specs = mutation_specs_from_values(base, values)
    if not specs:
        return corpus, []
    failures = lint_mutation_specs(specs)
    if failures:
        raise typer.BadParameter(f"world-visible mutation text failed leak lint: {failures[0]}")
    result = apply_corpus_mutations(corpus, specs)
    return result.corpus, result.applied


@app.command("inspect")
def inspect_inputs(root: Annotated[Path | None, typer.Option("--root")] = None, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Inspect compiled design inputs and raw document availability."""
    base = _root(root)
    design = load_design(base)
    missing_docs = sorted(doc_id for doc_id, meta in design.documents.items() if meta.path is None)
    payload = {
        "root": str(base),
        "documents": len(design.documents),
        "documents_with_raw_files": len(design.documents) - len(missing_docs),
        "missing_raw_files": missing_docs,
        "spans": len(design.spans),
        "probes": len(design.probes),
        "seats": len(design.seats),
        "model": normalize_openrouter_model(None),
    }
    if as_json:
        _echo_json(payload)
    else:
        typer.echo(f"documents={payload['documents']} raw={payload['documents_with_raw_files']} spans={payload['spans']} probes={payload['probes']} seats={payload['seats']}")
        typer.echo(f"model={payload['model']}")


@app.command("s0")
def s0(
    probe: Annotated[str, typer.Option("--probe")] = "P-01",
    span: Annotated[str | None, typer.Option("--span", help="Span id for the S0 battery row; defaults to the first span bound to --probe")] = None,
    seat: Annotated[str, typer.Option("--seat")] = "emp-A",
    variant: Annotated[int, typer.Option("--variant")] = 0,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    mutation: Annotated[list[str] | None, typer.Option("--mutation", help="Runtime corpus mutation_id from mutation_operators_v1.json; repeat for multiple")] = None,
) -> None:
    """Run one live S0 interpretation-battery row for a probe and seat."""
    base = _root(root)
    _require_live(base)
    design = load_design(base)
    if probe not in design.probes:
        raise typer.BadParameter(f"unknown probe: {probe}")
    if seat not in design.seats:
        raise typer.BadParameter(f"unknown seat: {seat}")
    span_id = span or (design.probes[probe].binds[0] if design.probes[probe].binds else "")
    if span_id not in design.s0_question_templates:
        raise typer.BadParameter(f"unknown or untemplated span for S0: {span_id}")
    corpus, applied_mutations = _corpus_with_mutations(base, design, mutation)
    target_root = (run_root or make_run_root(base, f"s0_{probe}_{seat}")).resolve()
    result = run_s0(design=design, corpus=corpus, probe_id=probe, seat_id=seat, run_root=target_root, span_id=span_id, model=model, variant=variant, mutations=applied_mutations)
    write_triage(target_root)
    _echo_json(result)


@app.command("s1")
def s1(
    probe: Annotated[str, typer.Option("--probe")] = "P-04",
    seed: Annotated[int, typer.Option("--seed")] = 0,
    ticks: Annotated[int, typer.Option("--ticks")] = 6,
    strict_completion: Annotated[bool, typer.Option("--strict-completion")] = False,
    strict_material: Annotated[bool, typer.Option("--strict-material")] = False,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    prompt_mode: Annotated[str, typer.Option("--prompt-mode", help="scaffold | measurement")] = "scaffold",
    seat_model: Annotated[list[str] | None, typer.Option("--seat-model", help="Per-seat model binding, e.g. emp-A=openrouter:qwen/qwen3.6-flash")] = None,
    scc_switch_tick: Annotated[int | None, typer.Option("--scc-switch-tick", help="Tick at which K-completion-gate becomes active")] = None,
    mutation: Annotated[list[str] | None, typer.Option("--mutation", help="Runtime corpus mutation_id from mutation_operators_v1.json; repeat for multiple")] = None,
) -> None:
    """Run one live S1 multi-seat episode."""
    base = _root(root)
    _require_live(base)
    design = load_design(base)
    corpus, applied_mutations = _corpus_with_mutations(base, design, mutation)
    knobs = {"K-completion-gate": strict_completion, "K-material-picker": strict_material}
    target_root = (run_root or make_run_root(base, f"s1_{probe}")).resolve()
    result = run_s1_episode(design=design, corpus=corpus, probe_id=probe, run_root=target_root, model=model, knobs=knobs, seed=seed, ticks=ticks, prompt_mode=prompt_mode, model_bindings=_seat_model_bindings(seat_model), scc_switch_tick=scc_switch_tick, mutations=applied_mutations)  # type: ignore[arg-type]
    write_triage(target_root)
    _echo_json(result)


@app.command("s2")
def s2(
    seed: Annotated[int, typer.Option("--seed")] = 0,
    ticks: Annotated[int, typer.Option("--ticks")] = 40,
    anchor: Annotated[bool, typer.Option("--anchor")] = False,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    prompt_mode: Annotated[str, typer.Option("--prompt-mode", help="scaffold | measurement")] = "scaffold",
    seat_model: Annotated[list[str] | None, typer.Option("--seat-model", help="Per-seat model binding, e.g. emp-A=openrouter:qwen/qwen3.6-flash")] = None,
    scc_switch_tick: Annotated[int | None, typer.Option("--scc-switch-tick", help="Tick at which K-completion-gate becomes active")] = None,
    mutation: Annotated[list[str] | None, typer.Option("--mutation", help="Runtime corpus mutation_id from mutation_operators_v1.json; repeat for multiple")] = None,
) -> None:
    """Run one live S2 world (full deck)."""
    base = _root(root)
    _require_live(base)
    design = load_design(base)
    corpus, applied_mutations = _corpus_with_mutations(base, design, mutation)
    target_root = (run_root or make_run_root(base, "anchor_s2" if anchor else "s2")).resolve()
    result = run_s2_world(design=design, corpus=corpus, run_root=target_root, model=model, knobs={}, seed=seed, ticks=ticks, anchor=anchor, prompt_mode=prompt_mode, model_bindings=_seat_model_bindings(seat_model), scc_switch_tick=scc_switch_tick, mutations=applied_mutations)  # type: ignore[arg-type]
    write_triage(target_root)
    _echo_json(result)


@app.command("campaign")
def campaign(
    s0_limit: Annotated[int | None, typer.Option("--s0-limit", help="Execute only N S0 rows (default: full matrix)")] = None,
    s0_models: Annotated[list[str] | None, typer.Option("--s0-model", help="S0 cold-read model; repeat for multiple models")] = None,
    s0_variants: Annotated[int, typer.Option("--s0-variants")] = 2,
    s1_probe: Annotated[str | None, typer.Option("--s1-probe")] = None,
    s1_k: Annotated[int, typer.Option("--s1-k")] = 3,
    with_s2: Annotated[bool, typer.Option("--with-s2")] = False,
    s2_k: Annotated[int, typer.Option("--s2-k")] = 1,
    s2_ticks: Annotated[int, typer.Option("--s2-ticks")] = 40,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    prompt_mode: Annotated[str, typer.Option("--prompt-mode", help="scaffold | measurement")] = "scaffold",
    seat_model: Annotated[list[str] | None, typer.Option("--seat-model", help="Per-seat model binding, e.g. emp-A=openrouter:qwen/qwen3.6-flash")] = None,
    scc_switch_tick: Annotated[int | None, typer.Option("--scc-switch-tick", help="Tick at which K-completion-gate becomes active")] = None,
    mutation: Annotated[list[str] | None, typer.Option("--mutation", help="Runtime corpus mutation_id from mutation_operators_v1.json; repeat for multiple")] = None,
) -> None:
    """Run a live campaign: S0 battery -> S1 ensemble -> optional S2 + anchor -> acceptance."""
    base = _root(root)
    _require_live(base)
    design = load_design(base)
    corpus, applied_mutations = _corpus_with_mutations(base, design, mutation)
    payload = run_design_campaign(
        root=base,
        design=design,
        corpus=corpus,
        model=model,
        s0_models=s0_models,
        s0_variants=s0_variants,
        s0_limit=s0_limit,
        s1_probe=s1_probe,
        s1_k=s1_k,
        with_s2=with_s2,
        s2_k=s2_k,
        s2_ticks=s2_ticks,
        prompt_mode=prompt_mode,  # type: ignore[arg-type]
        model_bindings=_seat_model_bindings(seat_model),
        scc_switch_tick=scc_switch_tick,
        mutations=applied_mutations,
    )
    _echo_json(payload)


@app.command("mutation-catalog")
def mutation_catalog(
    root: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """List runtime corpus mutation operators available for WP-06 runs."""
    base = _root(root)
    catalog = load_mutation_catalog(base)
    _echo_json({"schema_version": "company_twin.mutation_catalog_view.v1", "mutation_ids": sorted(catalog), "operators": catalog})


@app.command("control-pairs")
def control_pairs(
    mutation: Annotated[list[str] | None, typer.Option("--mutation", help="Mutation id to place in the treatment side; repeat for multiple pairs")] = None,
    k: Annotated[int, typer.Option("--k", help="Shared seed count per mutation")] = 5,
    output: Annotated[Path | None, typer.Option("--output", help="Optional JSON path to write")] = None,
    root: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Generate a WP-07 delta-one pair manifest; execution remains live-only."""
    base = _root(root)
    if not mutation:
        raise typer.BadParameter("at least one --mutation is required")
    payload = build_delta_one_pair_manifest(root=base, mutation_ids=mutation, seeds=k)
    if output is not None:
        output.resolve().parent.mkdir(parents=True, exist_ok=True)
        output.resolve().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _echo_json(payload)


@app.command("control-pair-campaign")
def control_pair_campaign(
    manifest: Annotated[Path, typer.Option("--manifest", help="WP-07 control-pairs JSON manifest to execute")],
    probe: Annotated[str, typer.Option("--probe", help="S0/S1 probe to run for every pair side")] = "P-01",
    stage: Annotated[str, typer.Option("--stage", help="S0 | S1 | S2")] = "S1",
    ticks: Annotated[int, typer.Option("--ticks")] = 6,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    prompt_mode: Annotated[str, typer.Option("--prompt-mode", help="scaffold | measurement")] = "measurement",
    seat_model: Annotated[list[str] | None, typer.Option("--seat-model", help="Per-seat model binding, e.g. emp-A=openrouter:qwen/qwen3.6-flash")] = None,
    scc_switch_tick: Annotated[int | None, typer.Option("--scc-switch-tick", help="Tick at which K-completion-gate becomes active")] = None,
    timed_notices: Annotated[bool, typer.Option("--timed-notices/--no-timed-notices", help="Deliver campaign deadline notices during the control-pair campaign")] = False,
    s0_span: Annotated[str | None, typer.Option("--s0-span", help="S0 span id to bind; defaults to the first span bound to --probe")] = None,
    s0_seat: Annotated[str, typer.Option("--s0-seat", help="S0 seat id / role cell to run")] = "emp-A",
    s0_model: Annotated[list[str] | None, typer.Option("--s0-model", help="S0 cold-read model; repeat for multiple models")] = None,
    s0_variants: Annotated[int, typer.Option("--s0-variants", help="Number of S0 paraphrase variants per model")] = 2,
) -> None:
    """Execute a live delta-one control-pair campaign from a manifest."""
    base = _root(root)
    _require_live(base)
    design = load_design(base)
    payload = run_control_pair_campaign(
        root=base,
        design=design,
        corpus=Corpus.from_design(design),
        manifest=json.loads(manifest.resolve().read_text(encoding="utf-8")),
        model=model,
        probe=probe,
        stage=stage,
        ticks=ticks,
        prompt_mode=prompt_mode,  # type: ignore[arg-type]
        model_bindings=_seat_model_bindings(seat_model),
        scc_switch_tick=scc_switch_tick,
        timed_notice_recipients=None if timed_notices else [],
        s0_span=s0_span,
        s0_seat=s0_seat,
        s0_models=s0_model,
        s0_variants=s0_variants,
    )
    _echo_json(payload)


@app.command("triage")
def triage(run_root: Annotated[Path, typer.Argument()]) -> None:
    """Run deterministic L0 triage over a run bundle."""
    payload = write_triage(run_root.resolve())
    _echo_json(payload)


@app.command("min-repro")
def min_repro(
    campaign_root: Annotated[Path, typer.Option("--campaign-root")],
    min_rate: Annotated[float, typer.Option("--min-rate", help="Pre-registered reproduction threshold for the later live confirmation plan")] = 0.5,
    min_seeds: Annotated[int, typer.Option("--min-seeds", help="Minimum fresh confirmation bundles required by the later live confirmation plan")] = 3,
) -> None:
    """Collate queued min-repro evidence without promoting confirmed findings."""
    payload = execute_min_repro_jobs(campaign_root.resolve(), min_rate=min_rate, min_seeds=min_seeds)
    _echo_json(payload)


@app.command("min-repro-confirm")
def min_repro_confirm(
    campaign_root: Annotated[Path, typer.Option("--campaign-root")],
    job_id: Annotated[str | None, typer.Option("--job-id", help="Queued min-repro job id to confirm")] = None,
    finding_type: Annotated[str | None, typer.Option("--finding-type", help="Select the highest-rate queued job for this finding type")] = None,
    confirmation_seeds: Annotated[int, typer.Option("--confirmation-seeds", help="Fresh disjoint seed count")] = 3,
    seed_start: Annotated[int, typer.Option("--seed-start", help="First candidate fresh seed")] = 100,
    min_rate: Annotated[float, typer.Option("--min-rate", help="Reproduction-rate threshold for status=reproduced")] = 0.5,
    ticks: Annotated[int, typer.Option("--ticks", help="Trimmed S1/S2 confirmation tick window")] = 6,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    prompt_mode: Annotated[str, typer.Option("--prompt-mode", help="scaffold | measurement")] = "measurement",
    seat_model: Annotated[list[str] | None, typer.Option("--seat-model", help="Per-seat model binding, e.g. emp-A=openrouter:qwen/qwen3.6-flash")] = None,
    seat_subset: Annotated[list[str] | None, typer.Option("--seat-subset", help="S2 seat id to retain during fresh confirmation; repeat for multiple")] = None,
    allow_threshold_override: Annotated[bool, typer.Option("--allow-threshold-override", help="Allow confirmation values that differ from the queued pre-registered threshold and mark the manifest/A-14")] = False,
    timed_notices: Annotated[bool, typer.Option("--timed-notices/--no-timed-notices", help="Deliver timed notices during confirmation runs")] = False,
) -> None:
    """Run fresh live confirmation bundles for one queued min-repro job."""
    base = _root(root)
    _require_live(base)
    design = load_design(base)
    payload = execute_fresh_min_repro_confirmation(
        campaign_root.resolve(),
        design=design,
        corpus=Corpus.from_design(design),
        job_id=job_id,
        finding_type=finding_type,
        confirmation_seeds=confirmation_seeds,
        seed_start=seed_start,
        min_rate=min_rate,
        ticks=ticks,
        model=model,
        prompt_mode=prompt_mode,
        model_bindings=_seat_model_bindings(seat_model),
        seats_subset=seat_subset,
        allow_threshold_override=allow_threshold_override,
        timed_notice_recipients=None if timed_notices else [],
    )
    _echo_json(payload)


@app.command("g3")
def g3(
    run_root: Annotated[Path | None, typer.Option("--run-root", help="Single run bundle to evaluate")] = None,
    campaign_root: Annotated[Path | None, typer.Option("--campaign-root", help="Campaign root whose child bundles should be evaluated")] = None,
    judge_model: Annotated[str | None, typer.Option("--judge-model", help="OpenRouter model for live semantic judge; omitted uses local deterministic proxy")] = None,
) -> None:
    """Evaluate g3 semantic grounding over existing basis/read_document evidence."""
    _root()
    if bool(run_root) == bool(campaign_root):
        raise typer.BadParameter("Provide exactly one of --run-root or --campaign-root")
    judge = OpenRouterSemanticJudge(judge_model) if judge_model else LocalSemanticJudge()
    payload = (
        evaluate_semantic_grounding_campaign(campaign_root.resolve(), judge=judge)
        if campaign_root is not None
        else evaluate_semantic_grounding_run(run_root.resolve(), judge=judge)
    )
    _echo_json(payload)


@app.command("g3-export-calibration")
def g3_export_calibration(
    source_root: Annotated[Path, typer.Option("--source-root", help="Run bundle or campaign root containing basis_records.jsonl")],
    output: Annotated[Path, typer.Option("--output", help="JSONL file to write for human labels")],
    limit: Annotated[int, typer.Option("--limit")] = 20,
) -> None:
    """Export action-bound basis samples for human g3 calibration labels."""
    _root()
    payload = export_g3_calibration_samples(source_root.resolve(), output.resolve(), limit=limit)
    _echo_json(payload)


@app.command("g3-score-calibration")
def g3_score_calibration(
    calibration_file: Annotated[Path, typer.Option("--calibration-file", help="Labeled calibration JSONL fixture (positive or negative)")],
    output: Annotated[Path, typer.Option("--output", help="Path to write the machine-readable specificity/agreement summary")],
    judge_model: Annotated[str | None, typer.Option("--judge-model", help="OpenRouter model for live semantic judge; omitted uses local deterministic proxy")] = None,
) -> None:
    """Score a labeled g3 calibration fixture with any judge and write a summary artifact.

    Use this for both the positive fixture (agreement rate) and the negative
    fixture docs/g3_negative_calibration_samples.jsonl (specificity rate).
    """
    _root()
    judge = OpenRouterSemanticJudge(judge_model) if judge_model else LocalSemanticJudge()
    payload = score_g3_calibration_file(calibration_file.resolve(), judge=judge, output_path=output.resolve())
    _echo_json(payload)


@app.command("prompt-ab-report")
def prompt_ab_report(campaign_root: Annotated[Path, typer.Option("--campaign-root")]) -> None:
    """Build the WP-05 scaffold-vs-measurement report from existing bundles."""
    payload = write_prompt_mode_ab_report(campaign_root.resolve())
    _echo_json(payload)


@app.command("acceptance")
def acceptance(
    campaign_root: Annotated[Path, typer.Option("--campaign-root")],
    scope: Annotated[str, typer.Option("--scope", help="auto | s0_s1 | full_world")] = "auto",
    root: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Run harness-safety acceptance gates (A-01..A-14), not Stage 9 readiness."""
    base = _root(root)
    design = load_design(base)
    corpus = Corpus.from_design(design)
    payload = run_acceptance(campaign_root=campaign_root.resolve(), design=design, corpus=corpus, scope=scope)
    _echo_json(payload)
    if not payload["passed"]:
        raise typer.Exit(code=1)


@app.command("readiness")
def readiness(
    campaign_root: Annotated[Path, typer.Option("--campaign-root")],
    semantic_threshold: Annotated[float, typer.Option("--semantic-threshold")] = 0.8,
) -> None:
    """Run Stage 9 experiment-readiness checks; stricter than harness acceptance."""
    payload = run_readiness_gate(campaign_root.resolve(), semantic_threshold=semantic_threshold)
    _echo_json(payload)
    if not payload["passed"]:
        raise typer.Exit(code=1)


@app.command("readiness-reports")
def readiness_reports(
    campaign_root: Annotated[Path, typer.Option("--campaign-root")],
    semantic_threshold: Annotated[float, typer.Option("--semantic-threshold")] = 0.8,
    overwrite: Annotated[bool, typer.Option("--overwrite")] = False,
    root: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Write Stage 9 report envelopes; missing evidence remains failed."""
    base = _root(root)
    design = load_design(base)
    corpus = Corpus.from_design(design)
    lint_payload = static_world_surface_lint(design)
    payload = write_readiness_reports(
        campaign_root.resolve(),
        corpus=corpus,
        lint_payload=lint_payload,
        semantic_threshold=semantic_threshold,
        overwrite=overwrite,
    )
    _echo_json(payload)


@app.command("stage9-evidence-manifest")
def stage9_evidence_manifest(
    campaign_root: Annotated[Path, typer.Option("--campaign-root")],
    root: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Build stage9_evidence_manifest.json, binding every readiness evidence artifact
    (backcasting/SME/holdout/g3/S2 bundles) to its provenance: git commit, command line,
    corpus/mutation hashes, judge backend/model, sample seed/hash, reviewer_type, plan_hash.
    World-version heterogeneity across evidence classes is recorded loudly, not hidden."""
    base = _root(root)
    payload = write_stage9_evidence_manifest(campaign_root.resolve(), command_line=list(sys.argv), code_root=base)
    _echo_json(payload)


@app.command("backcasting-extract")
def backcasting_extract(
    campaign_root: Annotated[Path, typer.Option("--campaign-root")],
    root: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """WP-14: extract candidate exemplar cases (現場判断事例) from the corpus into backcasting_inputs.json."""
    base = _root(root)
    design = load_design(base)
    extraction = extract_backcasting_cases(design)
    write_backcasting_inputs(campaign_root.resolve(), extraction)
    _echo_json(extraction)


@app.command("backcasting-report")
def backcasting_report(
    campaign_root: Annotated[Path, typer.Option("--campaign-root")],
) -> None:
    """WP-14: score backcasting_inputs.json against any recorded re-simulation results and write backcasting_report.json."""
    payload = write_backcasting_report(campaign_root.resolve())
    _echo_json(payload)


@app.command("backcasting-run")
def backcasting_run(
    campaign_root: Annotated[Path, typer.Option("--campaign-root")],
    seat_model: Annotated[str | None, typer.Option("--seat-model", help="OpenRouter model for the live seat re-simulation, e.g. openrouter:qwen/qwen3.6-flash")] = None,
    judge_model: Annotated[str | None, typer.Option("--judge-model", help="OpenRouter model for the live reproduction judge; omitted uses local deterministic proxy (never readiness-eligible)")] = None,
    sample: Annotated[int | None, typer.Option("--sample", help="Number of cases to sample from backcasting_inputs.json; omitted runs the full population")] = None,
    sample_seed: Annotated[int, typer.Option("--sample-seed", help="Pre-registered seed for deterministic sampling")] = 0,
    seat: Annotated[str, typer.Option("--seat", help="Seat id whose role card and reading tools the re-simulation seat uses")] = "emp-A",
    root: Annotated[Path | None, typer.Option("--root")] = None,
    require_live_seat: Annotated[bool, typer.Option("--require-live-seat/--allow-proxy-seat", help="Require --seat-model and a working OPENROUTER_API_KEY; disable only for offline/local testing")] = True,
) -> None:
    """WP-14 follow-up: run the live re-simulation pass over backcasting_inputs.json and write backcasting_resimulation_results.json.

    The seat receives ONLY the situation (S0-style natural business question)
    plus real search_corpus/read_document tools over the world corpus; the
    documented response and case_id never reach it. Per-row viewed_doc_ids is
    derived from the recorded read_document trace, never from the model's
    self-report. The judge is experimenter-plane and may see the documented
    response. Run `backcasting-report` afterwards to score the written
    results file.
    """
    base = _root(root)
    if require_live_seat:
        _require_live(base)
        if not seat_model:
            raise typer.BadParameter("--seat-model is required for a live run (pass --allow-proxy-seat for offline testing only)")
    design = load_design(base)
    if seat not in design.seats:
        raise typer.BadParameter(f"unknown seat: {seat}")
    corpus = Corpus.from_design(design)
    judge = OpenRouterReproductionJudge(judge_model) if judge_model else LocalReproductionJudge()
    try:
        payload = run_backcasting_resimulation(
            campaign_root.resolve(),
            design=design,
            corpus=corpus,
            judge=judge,
            sample_size=sample,
            sample_seed=sample_seed,
            seat_id=seat,
            seat_model=seat_model,
        )
    except BackcastingInputsError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _echo_json(payload)


@app.command("holdout-plan")
def holdout_plan(
    campaign_root: Annotated[Path, typer.Option("--campaign-root")],
    mutation: Annotated[list[str] | None, typer.Option("--mutation", help="Mutation id to include in the holdout injection plan; repeat for multiple. Defaults to the full catalog")] = None,
    run_root: Annotated[list[str] | None, typer.Option("--run-root", help="Planned run-root name(s) attributed to every injection (explicit resolution; omitting this makes bundle attribution exploration-mode, which cannot pass)")] = None,
    auto_run_roots: Annotated[bool, typer.Option("--auto-run-roots", help="Give each injection exactly one planned run root named after its injection_id (holdout_<mutation_id>) -- the one-to-one attribution a multi-mutation plan needs; mutually exclusive with --run-root")] = False,
    planned_ticks: Annotated[int, typer.Option("--planned-ticks", help="Expected world_ledger tick coverage for a live S2 bundle attributed to this plan's injections")] = 0,
    control_run_root: Annotated[list[str] | None, typer.Option("--control-run-root", help="Designated no-mutation control run-root name, sealed into the plan (part of plan_hash) for delta-aware detection and benign-control baseline scoring; repeat for multiple")] = None,
    seeds_per_injection: Annotated[int, typer.Option("--seeds-per-injection", help="Global default number of independent seeded run roots to plan per injection (requires --auto-run-roots when > 1); sealed into plan_hash. Default 1 keeps the pre-existing holdout_<mutation_id> naming. Overridden per-mutation by --injection-seeds")] = 1,
    injection_seeds: Annotated[list[str] | None, typer.Option("--injection-seeds", help="Per-mutation seed-count override as mutation_id=K (e.g. --injection-seeds contradict_chat_approval_recorded=5); repeat for multiple mutations. Falls back to --seeds-per-injection for any mutation_id not listed. Sealed into plan_hash (MASTER_DESIGN.md section 17.11)")] = None,
    root: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """WP-14: build a holdout injection plan from the WP-06 mutation catalog and write holdout_inputs.json."""
    base = _root(root)
    seeds_arg: int | dict[str, int] = seeds_per_injection
    if injection_seeds:
        per_mutation: dict[str, int] = {"_default": seeds_per_injection}
        for item in injection_seeds:
            if "=" not in item:
                raise typer.BadParameter(f"--injection-seeds expects mutation_id=K, got {item!r}")
            mutation_id, _, raw_k = item.partition("=")
            mutation_id = mutation_id.strip()
            try:
                k = int(raw_k.strip())
            except ValueError as exc:
                raise typer.BadParameter(f"--injection-seeds K must be an integer, got {item!r}") from exc
            per_mutation[mutation_id] = k
        seeds_arg = per_mutation
    plan = build_holdout_injection_plan(
        base,
        mutation_ids=mutation,
        run_roots=run_root,
        auto_run_roots=auto_run_roots,
        planned_ticks=planned_ticks,
        control_run_roots=control_run_root,
        seeds_per_injection=seeds_arg,
    )
    write_holdout_inputs(campaign_root.resolve(), plan)
    _echo_json(plan)


@app.command("holdout-score")
def holdout_score(
    campaign_root: Annotated[Path, typer.Option("--campaign-root")],
    control_run_root: Annotated[list[str] | None, typer.Option("--control-run-root", help="Designated no-mutation control run-root name (e.g. an anchor/plain S2 bundle) to score with the same detectors; repeat for multiple")] = None,
) -> None:
    """WP-14: score holdout_inputs.json against existing run bundles under --campaign-root and write holdout_report.json."""
    payload = write_holdout_report(campaign_root.resolve(), control_run_roots=control_run_root)
    _echo_json(payload)
    if not payload["passed"]:
        raise typer.Exit(code=1)


@app.command("sme-pack")
def sme_pack(
    campaign_root: Annotated[Path, typer.Option("--campaign-root")],
    run_root: Annotated[list[Path], typer.Option("--run-root", help="Run bundle to sample excerpts from; repeat for multiple")],
    samples_per_run: Annotated[int, typer.Option("--samples-per-run")] = 10,
    reviewer_type: Annotated[str, typer.Option("--reviewer-type", help="human_sme (default) | ai_proxy")] = "human_sme",
    reviewer_note: Annotated[str | None, typer.Option("--reviewer-note", help="Free-form reviewer prompt/model/blindness note preserved into the packet and report")] = None,
) -> None:
    """WP-14: build a leak-stripped SME blind-review packet; writes reviewer-facing sme_blind_review_inputs.json plus experimenter-side sme_blind_review_id_map.json."""
    reviewer = {"note": reviewer_note} if reviewer_note else None
    packet, id_map = build_blind_review_packet(
        [path.resolve() for path in run_root], samples_per_run=samples_per_run, reviewer_type=reviewer_type, reviewer=reviewer
    )
    write_sme_blind_review_inputs(campaign_root.resolve(), packet, id_map)
    _echo_json(packet)


@app.command("sme-score")
def sme_score(
    campaign_root: Annotated[Path, typer.Option("--campaign-root")],
) -> None:
    """WP-14: score sme_blind_review_inputs.json against filled-in reviewer responses and write sme_blind_review.json."""
    payload = write_sme_blind_review_report(campaign_root.resolve())
    _echo_json(payload)
    if not payload["passed"]:
        raise typer.Exit(code=1)


@app.command("lint")
def lint(root: Annotated[Path | None, typer.Option("--root")] = None) -> None:
    """Static world-surface vocabulary lint (NOT an acceptance gate)."""
    base = _root(root)
    design = load_design(base)
    payload = static_world_surface_lint(design)
    _echo_json(payload)
    if not payload["passed"]:
        raise typer.Exit(code=1)


@app.command("run-batch")
def run_batch_cmd(
    batch_spec: Annotated[Path | None, typer.Option("--batch-spec", help="JSON file with {\"runs\": [...], \"concurrency\": N, \"stagger_seconds\": S}")] = None,
    concurrency: Annotated[int | None, typer.Option("--concurrency", help="Max simultaneous run subprocesses (default 3; see module docstring for observed OpenRouter contention)")] = None,
    stagger_seconds: Annotated[float | None, typer.Option("--stagger-seconds", help="Delay between successive launches, in addition to the concurrency cap")] = None,
    batch_dir: Annotated[Path | None, typer.Option("--batch-dir", help="Directory for per-run logs and batch_manifest.json; defaults to --root/runs/batch_<timestamp>")] = None,
    retry_failed: Annotated[Path | None, typer.Option("--retry-failed", help="Path to a prior batch_manifest.json; re-run only its failed entries into the SAME run_roots")] = None,
    delete_partial_roots_flag: Annotated[bool, typer.Option("--delete-partial-roots", help="With --retry-failed, delete each failed run's (partial) run_root before re-launching it. Never happens by default -- required explicitly, once, per retry")] = False,
    root: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """WP-12: orchestrate independent S0/S1/S2/control-pair-campaign runs in parallel subprocesses.

    This command ONLY launches runs -- it reuses the existing single-run CLI
    commands (`s0`, `s1`, `s2`, `control-pair-campaign`) verbatim, one per
    subprocess, and never itself computes or writes any campaign-level shared
    artifact (no triage aggregation, no acceptance/readiness evaluation, no
    control-pair-campaign collation). Run `triage` / `acceptance` /
    `readiness*` / `control-pair-campaign` aggregation / `holdout-score` /
    `sme-score` as separate SERIAL steps afterwards against the resulting
    run_roots, exactly as you would after any sequential run.

    Safety rails: every run_root in the batch must be distinct and must not
    already exist on disk -- this is checked for the WHOLE batch before any
    subprocess is launched, so a bad spec fails loudly with zero side
    effects. One run failing never stops the batch; failures are recorded in
    batch_manifest.json (exit code + per-run log path) and this command exits
    non-zero if any run failed. Runs are bit-identical to running them one at
    a time with the same seeds -- subprocess isolation means no run shares
    mutable state with another; concurrency only changes wall-clock.

    Rate-limit note (observed 2026-07-05, OpenRouter, qwen3.6-flash): 3
    concurrent S2 worlds slowed each run ~20-30% while ~2.5x-ing aggregate
    throughput -- the binding constraint is the provider's rate limit, not
    local CPU/RAM. This command warns (does not block) above concurrency 4.
    """
    base = _root(root)

    if retry_failed is not None:
        if batch_spec is None:
            raise typer.BadParameter("--retry-failed requires --batch-spec (the same spec the failed batch used)")
        manifest = load_batch_manifest(retry_failed.resolve())
        original_spec = BatchSpec.from_dict(json.loads(batch_spec.resolve().read_text(encoding="utf-8")))
        try:
            spec = build_retry_spec(manifest, original_spec=original_spec)
        except BatchSpecError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        if delete_partial_roots_flag:
            removed = delete_partial_roots(spec, base_dir=base)
            for path in removed:
                typer.echo(f"removed partial run_root: {path}")
        target_batch_dir = (batch_dir or Path(manifest["batch_dir"])).resolve()
    else:
        if batch_spec is None:
            raise typer.BadParameter("--batch-spec is required (JSON file describing the batch; see module docstring)")
        spec = BatchSpec.from_dict(json.loads(batch_spec.resolve().read_text(encoding="utf-8")))
        target_batch_dir = (batch_dir or make_run_root(base, "batch")).resolve()

    if concurrency is not None:
        spec.concurrency = concurrency
    if stagger_seconds is not None:
        spec.stagger_seconds = stagger_seconds

    if spec.concurrency > RATE_LIMIT_WARN_THRESHOLD:
        typer.echo(
            f"warning: concurrency={spec.concurrency} exceeds {RATE_LIMIT_WARN_THRESHOLD}; "
            "observed contention (2026-07-05, OpenRouter) was ~20-30% per-run slowdown at "
            "concurrency=3 with ~2.5x throughput -- the OpenRouter rate limit is the binding "
            "constraint, not local resources, so higher concurrency may not scale and can "
            "surface as run failures instead of speedup.",
            err=True,
        )

    try:
        validate_batch_spec(spec, base_dir=base)
    except BatchSpecError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    manifest = run_batch(spec, base_dir=base, batch_dir=target_batch_dir)
    _echo_json(manifest)
    if not manifest["passed"]:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
