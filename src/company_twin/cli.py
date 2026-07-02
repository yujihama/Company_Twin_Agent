from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from .acceptance import run_acceptance
from .agents import openrouter_ready
from .campaign import run_design_campaign, static_world_surface_lint
from .corpus import Corpus
from .design_loader import load_design
from .env import load_local_env, normalize_openrouter_model
from .harness import make_run_root, run_s0, run_s1_episode, run_s2_world
from .oracles import write_triage

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
    corpus = Corpus.from_design(design)
    target_root = (run_root or make_run_root(base, f"s0_{probe}_{seat}")).resolve()
    result = run_s0(design=design, corpus=corpus, probe_id=probe, seat_id=seat, run_root=target_root, model=model, variant=variant)
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
) -> None:
    """Run one live S1 multi-seat episode."""
    base = _root(root)
    _require_live(base)
    design = load_design(base)
    corpus = Corpus.from_design(design)
    knobs = {"K-completion-gate": strict_completion, "K-material-picker": strict_material}
    target_root = (run_root or make_run_root(base, f"s1_{probe}")).resolve()
    result = run_s1_episode(design=design, corpus=corpus, probe_id=probe, run_root=target_root, model=model, knobs=knobs, seed=seed, ticks=ticks)
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
) -> None:
    """Run one live S2 world (full deck)."""
    base = _root(root)
    _require_live(base)
    design = load_design(base)
    corpus = Corpus.from_design(design)
    target_root = (run_root or make_run_root(base, "anchor_s2" if anchor else "s2")).resolve()
    result = run_s2_world(design=design, corpus=corpus, run_root=target_root, model=model, knobs={}, seed=seed, ticks=ticks, anchor=anchor)
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
    )
    _echo_json(payload)


@app.command("triage")
def triage(run_root: Annotated[Path, typer.Argument()]) -> None:
    """Run deterministic L0 triage over a run bundle."""
    payload = write_triage(run_root.resolve())
    _echo_json(payload)


@app.command("acceptance")
def acceptance(
    campaign_root: Annotated[Path, typer.Option("--campaign-root")],
    scope: Annotated[str, typer.Option("--scope", help="auto | s0_s1 | full_world")] = "auto",
    root: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Run the unfakeable acceptance gates (A-01..A-09) over a campaign root."""
    base = _root(root)
    design = load_design(base)
    corpus = Corpus.from_design(design)
    payload = run_acceptance(campaign_root=campaign_root.resolve(), design=design, corpus=corpus, scope=scope)
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


if __name__ == "__main__":
    app()
