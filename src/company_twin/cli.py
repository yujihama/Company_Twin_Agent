from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from .agents import openrouter_ready
from .campaign import check_design_compliance, run_design_campaign
from .corpus import Corpus
from .design_loader import load_design
from .env import load_local_env, normalize_openrouter_model
from .harness import make_run_root, run_s0, run_s1_episode
from .oracles import write_triage


app = typer.Typer(no_args_is_help=True)


def _echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=True, indent=2))


def _root(path: Path | None = None) -> Path:
    root = (path or Path.cwd()).resolve()
    load_local_env(root)
    return root


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
    live: Annotated[bool, typer.Option("--live")] = False,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
) -> None:
    """Run or generate an S0 static interpretation battery for one probe and seat."""
    base = _root(root)
    design = load_design(base)
    if probe not in design.probes:
        raise typer.BadParameter(f"unknown probe: {probe}")
    if seat not in design.seats:
        raise typer.BadParameter(f"unknown seat: {seat}")
    if live:
        ready, detail = openrouter_ready(base)
        if not ready:
            raise typer.BadParameter(detail)
    corpus = Corpus.from_design(design)
    target_root = (run_root or make_run_root(base, f"s0_{probe}_{seat}")).resolve()
    result = run_s0(design=design, corpus=corpus, probe_id=probe, seat_id=seat, run_root=target_root, live=live, model=model)
    _echo_json(result)


@app.command("s1")
def s1(
    probe: Annotated[str, typer.Option("--probe")] = "P-04",
    seat: Annotated[str, typer.Option("--seat")] = "emp-A",
    live: Annotated[bool, typer.Option("--live")] = False,
    strict_completion: Annotated[bool, typer.Option("--strict-completion")] = False,
    strict_material: Annotated[bool, typer.Option("--strict-material")] = False,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
) -> None:
    """Run or generate an S1 episode for one probe and seat."""
    base = _root(root)
    design = load_design(base)
    if live:
        ready, detail = openrouter_ready(base)
        if not ready:
            raise typer.BadParameter(detail)
    corpus = Corpus.from_design(design)
    knobs = {"K-completion-gate": strict_completion, "K-material-picker": strict_material}
    target_root = (run_root or make_run_root(base, f"s1_{probe}_{seat}")).resolve()
    result = run_s1_episode(design=design, corpus=corpus, probe_id=probe, seat_id=seat, run_root=target_root, live=live, model=model, knobs=knobs)
    _echo_json(result)


@app.command("triage")
def triage(run_root: Annotated[Path, typer.Argument()]) -> None:
    """Run deterministic L0 triage over a run bundle."""
    payload = write_triage(run_root.resolve())
    _echo_json(payload)


@app.command("campaign")
def campaign(
    live: Annotated[bool, typer.Option("--live")] = False,
    max_live_agent_calls: Annotated[int, typer.Option("--max-live-agent-calls")] = 3,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
) -> None:
    """Run a design-compliance campaign with anchor, S0, S1, and S2 bundles."""
    base = _root(root)
    if live:
        ready, detail = openrouter_ready(base)
        if not ready:
            raise typer.BadParameter(detail)
    design = load_design(base)
    corpus = Corpus.from_design(design)
    payload = run_design_campaign(root=base, design=design, corpus=corpus, live=live, max_live_agent_calls=max_live_agent_calls, model=model)
    _echo_json(payload)


@app.command("compliance")
def compliance(root: Annotated[Path | None, typer.Option("--root")] = None, campaign_root: Annotated[Path | None, typer.Option("--campaign-root")] = None) -> None:
    """Run static compliance checks and optional run-bundle checks for a campaign root."""
    base = _root(root)
    design = load_design(base)
    run_roots: list[Path] = []
    if campaign_root:
        run_roots = [path for path in campaign_root.resolve().iterdir() if path.is_dir() and (path / "meta.json").exists()]
    payload = check_design_compliance(campaign_root=campaign_root.resolve() if campaign_root else None, design=design, run_roots=run_roots)
    _echo_json(payload)


if __name__ == "__main__":
    app()
