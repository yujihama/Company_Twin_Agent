from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from .acceptance import run_acceptance
from .agents import openrouter_ready
from .ab_testing import write_prompt_mode_ab_report
from .campaign import run_design_campaign, static_world_surface_lint
from .corpus import Corpus
from .design_loader import load_design
from .env import load_local_env, normalize_openrouter_model
from .harness import make_run_root, run_s0, run_s1_episode, run_s2_world
from .oracles import execute_min_repro_jobs, write_triage
from .readiness import run_readiness_gate, write_readiness_reports
from .semantic_grounding import LocalSemanticJudge, OpenRouterSemanticJudge, evaluate_semantic_grounding_campaign, evaluate_semantic_grounding_run, export_g3_calibration_samples

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
    corpus = Corpus.from_design(design)
    target_root = (run_root or make_run_root(base, f"s0_{probe}_{seat}")).resolve()
    result = run_s0(design=design, corpus=corpus, probe_id=probe, seat_id=seat, run_root=target_root, span_id=span_id, model=model, variant=variant)
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
) -> None:
    """Run one live S1 multi-seat episode."""
    base = _root(root)
    _require_live(base)
    design = load_design(base)
    corpus = Corpus.from_design(design)
    knobs = {"K-completion-gate": strict_completion, "K-material-picker": strict_material}
    target_root = (run_root or make_run_root(base, f"s1_{probe}")).resolve()
    result = run_s1_episode(design=design, corpus=corpus, probe_id=probe, run_root=target_root, model=model, knobs=knobs, seed=seed, ticks=ticks, prompt_mode=prompt_mode, model_bindings=_seat_model_bindings(seat_model), scc_switch_tick=scc_switch_tick)  # type: ignore[arg-type]
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
) -> None:
    """Run one live S2 world (full deck)."""
    base = _root(root)
    _require_live(base)
    design = load_design(base)
    corpus = Corpus.from_design(design)
    target_root = (run_root or make_run_root(base, "anchor_s2" if anchor else "s2")).resolve()
    result = run_s2_world(design=design, corpus=corpus, run_root=target_root, model=model, knobs={}, seed=seed, ticks=ticks, anchor=anchor, prompt_mode=prompt_mode, model_bindings=_seat_model_bindings(seat_model), scc_switch_tick=scc_switch_tick)  # type: ignore[arg-type]
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
) -> None:
    """Run a live campaign: S0 battery -> S1 ensemble -> optional S2 + anchor -> acceptance."""
    base = _root(root)
    _require_live(base)
    design = load_design(base)
    corpus = Corpus.from_design(design)
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


@app.command("g3")
def g3(
    run_root: Annotated[Path | None, typer.Option("--run-root", help="Single run bundle to evaluate")] = None,
    campaign_root: Annotated[Path | None, typer.Option("--campaign-root", help="Campaign root whose child bundles should be evaluated")] = None,
    judge_model: Annotated[str | None, typer.Option("--judge-model", help="OpenRouter model for live semantic judge; omitted uses local deterministic proxy")] = None,
) -> None:
    """Evaluate g3 semantic grounding over existing basis/read_document evidence."""
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
    payload = export_g3_calibration_samples(source_root.resolve(), output.resolve(), limit=limit)
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


@app.command("lint")
def lint(root: Annotated[Path | None, typer.Option("--root")] = None) -> None:
    """Static world-surface vocabulary lint (NOT an acceptance gate)."""
    base = _root(root)
    design = load_design(base)
    payload = static_world_surface_lint(design)
    _echo_json(payload)
    if not payload["passed"]:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
