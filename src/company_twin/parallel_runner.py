"""WP-12 parallel world-run executor (`並列実行(規模が必要になった時点)`).

Phase-3 experiments run batches of independent S0/S1/S2/control-pair worlds
(e.g. a delta-one control-pair set at K=5 seeds is 10+ live runs of
35-60 minutes each). Running them one at a time wastes wall-clock for no
measurement benefit -- the runs are independent by construction (distinct
run_root, no shared mutable state).

This module ONLY orchestrates. It does not reimplement any run logic: each
batch entry is executed by spawning `python -m company_twin.cli <stage> ...`
as its own subprocess, i.e. exactly the same single-run entry points already
exposed by `cli.py` (`s0`, `s1`, `s2`, `control-pair-campaign`). Because every
run is a fresh interpreter process with its own corpus/kernel/recorder state,
batch execution is measurement-transparent: given the same seeds, a batched
run is bit-identical to the equivalent sequential run. Concurrency changes
wall-clock only, never world content, scores, or recorded evidence.

Process isolation, not threads: subprocess (never `os.fork`, which does not
exist on Windows and this project runs on Windows) so each world gets a
genuinely separate process/interpreter/heap, inheriting the parent's
environment (and therefore PYTHONPATH and whatever `.env`/`.env.local`
resolution `company_twin.env.load_local_env` performs from the *run's* cwd --
each subprocess is launched with cwd = the batch's `--root`, matching how a
manually-invoked `company-twin s2 --root ...` would resolve its env file).

Safety rails (all learned the hard way from earlier WP work in this repo):
  * run_roots in a batch spec must be pairwise distinct;
  * no run_root may already exist on disk -- this is checked for the WHOLE
    batch BEFORE any subprocess is launched, so a typo can never silently
    overwrite or partially-clobber a previous campaign's evidence;
  * the parallel phase never writes any campaign-level shared file (no
    triage aggregation, no manifest merge, no readiness/acceptance
    evaluation) -- it only launches per-run subprocesses and records their
    exit status. Collation (`triage`, `acceptance`, `readiness*`,
    `control-pair-campaign` aggregation, `holdout-score`, `sme-score`, ...)
    remains a separate, serial, later step run by hand against the
    resulting run_roots. This module intentionally has no "collate" mode.

Failure handling: one run failing (non-zero exit) never aborts the batch --
every other launched/queued run still gets its chance to run to completion.
Failures are recorded in `batch_manifest.json` with their exit code and log
path; the batch process itself exits non-zero if ANY run failed, so CI/shell
scripts see the aggregate outcome. `--retry-failed <batch_manifest>` re-runs
exactly the failed entries (never the succeeded ones) into the SAME
run_roots -- but only after their (partial) run_root directories are deleted,
and only when the caller passes `--delete-partial-roots` explicitly; there is
no default-delete path, matching the project's "no silent overwrite" rule.

Rate-limit note (observed 2026-07-05, single afternoon, OpenRouter, qwen3.6-
flash): running 3 concurrent S2 worlds slowed each individual run by roughly
20-30% (the shared OpenRouter per-key rate limit throttles concurrent callers)
while multiplying aggregate throughput by roughly 2.5x. The binding
constraint is the *provider* rate limit, not local CPU/RAM/disk -- raising
concurrency further does not scale linearly and can start trading individual
run latency for no net throughput gain (or trip hard rate-limit errors that
surface as ordinary run failures). `run-batch` defaults to `--concurrency 3`
and prints a warning (not a hard block) when concurrency > 4.

Credits preflight (incident 2026-07-06): an 11-run batch ran the OpenRouter
account out of credits ~2h in, stalling/failing 8 of 11 runs with 402
Insufficient credits. The ops-notes rule "キャンペーン前に残高確認" is now
structural: before launching anything, `run-batch` queries the OpenRouter
credits endpoint (`check_openrouter_credits`), prints the remaining balance,
warns -- or aborts with `--abort-on-low-credits` -- when it is below
`--min-credits`, and records the pre-launch balance in `batch_manifest.json`
(`credits_preflight`). Legacy specs preserve the warn-only behavior when the
endpoint is unavailable. Sealed specs can embed a strict `credit_guard`; such
specs require a successful balance check and abort below the sealed floor.
They may also exact-partition all runs into ordered `waves`, selected one at a
time with `run-batch --wave`, so each spend boundary has its own preflight and
manifest without changing the full sealed batch specification.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BATCH_MANIFEST_SCHEMA_VERSION = "company_twin.batch_manifest.v1"
BATCH_MANIFEST_FILENAME = "batch_manifest.json"
WAVE_STATE_SCHEMA_VERSION = "company_twin.wave_execution_state.v1"
DEFAULT_CONCURRENCY = 3
RATE_LIMIT_WARN_THRESHOLD = 4

OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
CREDITS_CHECK_TIMEOUT_SECONDS = 10.0
# Observed cost anchor for sizing --min-credits: one S2 40-tick run has cost
# ~0.85-1.2 OpenRouter credits (more when the customer seat runs a plus-tier
# model), so a batch needs roughly 1.2 x len(runs) credits of headroom.
DEFAULT_MIN_CREDITS = 1.0

_VALID_STAGES = {"s0", "s1", "s2", "control-pair-campaign"}


def _resolve_run_root(base_dir: Path, run_root: str) -> Path:
    candidate = Path(run_root)
    return (candidate if candidate.is_absolute() else base_dir / candidate).resolve()


class BatchSpecError(ValueError):
    """Raised for a structurally or semantically invalid batch spec.

    Always raised BEFORE any subprocess is launched -- validation is a pure
    function of the spec plus the filesystem's current state, never of any
    run outcome.
    """


@dataclass(frozen=True)
class CreditGuard:
    """Sealed, fail-closed balance policy embedded in a batch specification."""

    minimum_credits: float
    abort_on_low_credits: bool
    require_available: bool

    @staticmethod
    def from_dict(payload: Any) -> "CreditGuard":
        if not isinstance(payload, dict):
            raise BatchSpecError("batch spec field 'credit_guard' must be an object")
        expected = {"minimum_credits", "abort_on_low_credits", "require_available"}
        if set(payload) != expected:
            raise BatchSpecError(
                "batch spec field 'credit_guard' must contain exactly "
                f"{sorted(expected)}; got {sorted(payload)}"
            )
        minimum = payload["minimum_credits"]
        if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
            raise BatchSpecError("credit_guard.minimum_credits must be a positive finite number")
        minimum_float = float(minimum)
        if not math.isfinite(minimum_float) or minimum_float <= 0:
            raise BatchSpecError("credit_guard.minimum_credits must be a positive finite number")
        if payload["abort_on_low_credits"] is not True:
            raise BatchSpecError("sealed credit_guard.abort_on_low_credits must be true")
        if payload["require_available"] is not True:
            raise BatchSpecError("sealed credit_guard.require_available must be true")
        return CreditGuard(
            minimum_credits=minimum_float,
            abort_on_low_credits=True,
            require_available=True,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "minimum_credits": self.minimum_credits,
            "abort_on_low_credits": self.abort_on_low_credits,
            "require_available": self.require_available,
        }


@dataclass(frozen=True)
class WaveSpec:
    """One ordered, sealed subset of a full batch."""

    wave_id: str
    run_ids: list[str]

    @staticmethod
    def from_dict(payload: Any, *, index: int) -> "WaveSpec":
        if not isinstance(payload, dict):
            raise BatchSpecError(f"batch spec waves[{index}] must be an object")
        expected = {"wave_id", "run_ids"}
        if set(payload) != expected:
            raise BatchSpecError(
                f"batch spec waves[{index}] must contain exactly {sorted(expected)}; got {sorted(payload)}"
            )
        wave_id = payload["wave_id"]
        if not isinstance(wave_id, str) or not wave_id.strip() or wave_id != wave_id.strip():
            raise BatchSpecError(f"batch spec waves[{index}].wave_id must be a non-empty trimmed string")
        run_ids = payload["run_ids"]
        if not isinstance(run_ids, list) or not run_ids:
            raise BatchSpecError(f"batch spec wave {wave_id!r} must contain a non-empty run_ids list")
        if any(not isinstance(run_id, str) or not run_id.strip() or run_id != run_id.strip() for run_id in run_ids):
            raise BatchSpecError(f"batch spec wave {wave_id!r} run_ids must be non-empty trimmed strings")
        return WaveSpec(wave_id=wave_id, run_ids=list(run_ids))

    def to_dict(self) -> dict[str, Any]:
        return {"wave_id": self.wave_id, "run_ids": list(self.run_ids)}


@dataclass
class WaveExecutionLease:
    """Exclusive pre-spend lease for one ordered wave attempt."""

    lock_path: Path
    state_path: Path
    state: dict[str, Any]
    wave_id: str
    ordered_wave_ids: list[str]


def _write_wave_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def acquire_wave_execution_lease(
    *,
    root: Path,
    batch_spec_sha256: str,
    plan_sha256: str,
    plan_id: str,
    execution_git_commit: str,
    ordered_wave_ids: list[str],
    wave_id: str,
    retry_manifest_path: Path | None,
    retry_failed_run_ids: list[str] | None = None,
) -> WaveExecutionLease:
    """Atomically reject concurrent, duplicate, or out-of-order wave spend."""
    state_dir = root.resolve() / "runs" / ".wave_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / f"{batch_spec_sha256}.json"
    lock_path = state_dir / f"{batch_spec_sha256}.lock"
    try:
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise BatchSpecError(
            f"wave execution lock already exists for batch {batch_spec_sha256}; another launch may be active"
        ) from exc
    try:
        os.write(descriptor, f"pid={os.getpid()} wave_id={wave_id}\n".encode("utf-8"))
    finally:
        os.close(descriptor)
    try:
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BatchSpecError(f"wave execution state is unreadable: {state_path}") from exc
            expected_keys = {
                "schema_version",
                "batch_spec_sha256",
                "plan_sha256",
                "plan_id",
                "execution_git_commit",
                "ordered_wave_ids",
                "completed_wave_ids",
                "next_wave_id",
                "pending_retry_manifest",
                "pending_retry_manifest_sha256",
                "pending_failed_run_ids",
                "in_progress",
                "active_output_manifest",
            }
            if not isinstance(state, dict) or set(state) != expected_keys:
                raise BatchSpecError("wave execution state has missing or unknown fields")
            if (
                state.get("schema_version") != WAVE_STATE_SCHEMA_VERSION
                or state.get("batch_spec_sha256") != batch_spec_sha256
                or state.get("plan_sha256") != plan_sha256
                or state.get("plan_id") != plan_id
                or state.get("execution_git_commit") != execution_git_commit
                or state.get("ordered_wave_ids") != ordered_wave_ids
            ):
                raise BatchSpecError("wave execution state disagrees with the sealed batch")
            completed = state.get("completed_wave_ids")
            if not isinstance(completed, list) or completed != ordered_wave_ids[: len(completed)]:
                raise BatchSpecError("wave execution state completed_wave_ids is not an ordered prefix")
            expected_next = ordered_wave_ids[len(completed)] if len(completed) < len(ordered_wave_ids) else None
            if state.get("next_wave_id") != expected_next:
                raise BatchSpecError("wave execution state next_wave_id is inconsistent")
            if state.get("in_progress") is not False:
                raise BatchSpecError("wave execution state is marked in_progress; manual recovery is required")
        else:
            state = {
                "schema_version": WAVE_STATE_SCHEMA_VERSION,
                "batch_spec_sha256": batch_spec_sha256,
                "plan_sha256": plan_sha256,
                "plan_id": plan_id,
                "execution_git_commit": execution_git_commit,
                "ordered_wave_ids": list(ordered_wave_ids),
                "completed_wave_ids": [],
                "next_wave_id": ordered_wave_ids[0],
                "pending_retry_manifest": None,
                "pending_retry_manifest_sha256": None,
                "pending_failed_run_ids": [],
                "in_progress": False,
                "active_output_manifest": None,
            }
        if state["next_wave_id"] is None:
            raise BatchSpecError("all sealed waves are already complete")
        if wave_id != state["next_wave_id"]:
            raise BatchSpecError(
                f"out-of-order wave launch: next_wave_id={state['next_wave_id']!r}, requested={wave_id!r}"
            )
        pending = state["pending_retry_manifest"]
        retry_path = str(retry_manifest_path.resolve()) if retry_manifest_path is not None else None
        pending_hash = state["pending_retry_manifest_sha256"]
        pending_failed = state["pending_failed_run_ids"]
        if not isinstance(pending_failed, list) or any(
            not isinstance(run_id, str) or not run_id for run_id in pending_failed
        ):
            raise BatchSpecError("wave execution state pending_failed_run_ids is invalid")
        if (pending is None) != (pending_hash is None) or (pending is None) != (pending_failed == []):
            raise BatchSpecError("wave execution state pending retry fields are inconsistent")
        if pending is None and retry_path is not None:
            raise BatchSpecError("wave state has no pending failure; --retry-failed is not allowed")
        if pending is not None and retry_path is None:
            raise BatchSpecError(f"wave {wave_id!r} has failed runs; retry the exact pending manifest")
        if pending is not None and retry_path != pending:
            raise BatchSpecError("--retry-failed does not match the exact pending wave manifest")
        if pending is not None and retry_failed_run_ids != pending_failed:
            raise BatchSpecError("retry manifest failed_run_ids differ from the wave execution state")
        if pending is not None:
            try:
                actual_retry_hash = hashlib.sha256(Path(pending).read_bytes()).hexdigest()
            except OSError as exc:
                raise BatchSpecError("pending retry manifest cannot be read") from exc
            if actual_retry_hash != pending_hash:
                raise BatchSpecError("pending retry manifest bytes differ from the wave execution state")
        return WaveExecutionLease(
            lock_path=lock_path,
            state_path=state_path,
            state=state,
            wave_id=wave_id,
            ordered_wave_ids=list(ordered_wave_ids),
        )
    except Exception:
        lock_path.unlink(missing_ok=True)
        raise


def mark_wave_launch_started(lease: WaveExecutionLease, *, output_manifest_path: Path) -> None:
    lease.state["in_progress"] = True
    lease.state["active_output_manifest"] = str(output_manifest_path.resolve())
    _write_wave_state(lease.state_path, lease.state)


def complete_wave_launch(
    lease: WaveExecutionLease,
    *,
    output_manifest_path: Path,
    failed_run_ids: list[str],
) -> None:
    state = lease.state
    expected_output = str(output_manifest_path.resolve())
    if state.get("active_output_manifest") != expected_output or state.get("in_progress") is not True:
        raise BatchSpecError("wave execution state does not match the completed attempt")
    if not output_manifest_path.exists():
        raise BatchSpecError("wave attempt did not persist its batch manifest")
    state["in_progress"] = False
    state["active_output_manifest"] = None
    if failed_run_ids:
        state["pending_retry_manifest"] = expected_output
        state["pending_retry_manifest_sha256"] = hashlib.sha256(output_manifest_path.read_bytes()).hexdigest()
        state["pending_failed_run_ids"] = list(failed_run_ids)
    else:
        completed = list(state["completed_wave_ids"])
        completed.append(lease.wave_id)
        state["completed_wave_ids"] = completed
        state["next_wave_id"] = (
            lease.ordered_wave_ids[len(completed)]
            if len(completed) < len(lease.ordered_wave_ids)
            else None
        )
        state["pending_retry_manifest"] = None
        state["pending_retry_manifest_sha256"] = None
        state["pending_failed_run_ids"] = []
    _write_wave_state(lease.state_path, state)


def release_wave_execution_lease(lease: WaveExecutionLease) -> None:
    lease.lock_path.unlink(missing_ok=True)


@dataclass
class RunSpec:
    """One independent world run inside a batch.

    `run_root` is required and must be unique within the batch (and must not
    already exist on disk) -- see `validate_batch_spec`. `extra_args` carries
    any additional CLI flags verbatim (e.g. mutation ids, seat-model
    bindings) so this module never needs to know every flag `cli.py`
    supports.
    """

    run_id: str
    stage: str
    run_root: str
    seed: int | None = None
    ticks: int | None = None
    prompt_mode: str | None = None
    model: str | None = None
    mutations: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)
    # control-pair-campaign uses --manifest instead of seed/ticks-per-run
    manifest: str | None = None
    probe: str | None = None

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "RunSpec":
        if "run_id" not in payload:
            raise BatchSpecError("run spec missing required field: run_id")
        if "stage" not in payload:
            raise BatchSpecError(f"run {payload.get('run_id')!r} missing required field: stage")
        if "run_root" not in payload:
            raise BatchSpecError(f"run {payload.get('run_id')!r} missing required field: run_root")
        stage = str(payload["stage"]).strip().lower()
        if stage not in _VALID_STAGES:
            raise BatchSpecError(f"run {payload.get('run_id')!r} has unknown stage {stage!r}; expected one of {sorted(_VALID_STAGES)}")
        mutations = payload.get("mutations") or []
        if not isinstance(mutations, list):
            raise BatchSpecError(f"run {payload.get('run_id')!r} field 'mutations' must be a list")
        extra_args = payload.get("extra_args") or []
        if not isinstance(extra_args, list):
            raise BatchSpecError(f"run {payload.get('run_id')!r} field 'extra_args' must be a list")
        return RunSpec(
            run_id=str(payload["run_id"]),
            stage=stage,
            run_root=str(payload["run_root"]),
            seed=payload.get("seed"),
            ticks=payload.get("ticks"),
            prompt_mode=payload.get("prompt_mode"),
            model=payload.get("model"),
            mutations=[str(m) for m in mutations],
            extra_args=[str(a) for a in extra_args],
            manifest=payload.get("manifest"),
            probe=payload.get("probe"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "stage": self.stage,
            "run_root": self.run_root,
            "seed": self.seed,
            "ticks": self.ticks,
            "prompt_mode": self.prompt_mode,
            "model": self.model,
            "mutations": list(self.mutations),
            "extra_args": list(self.extra_args),
            "manifest": self.manifest,
            "probe": self.probe,
        }

    def build_cli_args(self) -> list[str]:
        """Translate this spec into `python -m company_twin.cli <stage> ...` args.

        Reuses the exact flags cli.py's single-run commands already accept;
        this function does not know how to run a world, only how to spell
        the existing CLI invocation for one.
        """
        args: list[str] = [self.stage]
        if self.stage == "control-pair-campaign":
            if not self.manifest:
                raise BatchSpecError(f"run {self.run_id!r} stage control-pair-campaign requires 'manifest'")
            args += ["--manifest", self.manifest]
            if self.probe:
                args += ["--probe", self.probe]
        else:
            if self.seed is not None:
                args += ["--seed", str(self.seed)]
            if self.probe and self.stage in {"s0", "s1"}:
                args += ["--probe", self.probe]
        if self.ticks is not None and self.stage in {"s1", "s2"}:
            args += ["--ticks", str(self.ticks)]
        args += ["--run-root", self.run_root]
        if self.prompt_mode and self.stage in {"s1", "s2", "control-pair-campaign"}:
            args += ["--prompt-mode", self.prompt_mode]
        if self.model:
            args += ["--model", self.model]
        for mutation_id in self.mutations:
            args += ["--mutation", mutation_id]
        args += list(self.extra_args)
        return args


@dataclass
class BatchSpec:
    runs: list[RunSpec]
    root: str | None = None
    concurrency: int = DEFAULT_CONCURRENCY
    stagger_seconds: float = 0.0
    credit_guard: CreditGuard | None = None
    waves: list[WaveSpec] = field(default_factory=list)
    # Operational metadata set only on the deterministic subset returned by
    # select_wave. It is not part of the batch JSON schema.
    selected_wave_id: str | None = None
    # Canonical fallback for programmatic callers. The CLI replaces this with
    # the SHA-256 of the exact input file bytes before writing a manifest.
    source_sha256: str | None = field(default=None, repr=False)

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "BatchSpec":
        if not isinstance(payload, dict):
            raise BatchSpecError("batch spec must be a JSON object")
        raw_runs = payload.get("runs")
        if not isinstance(raw_runs, list) or not raw_runs:
            raise BatchSpecError("batch spec must contain a non-empty 'runs' list")
        runs = [RunSpec.from_dict(entry) for entry in raw_runs]
        raw_waves = payload.get("waves")
        if raw_waves is None:
            waves: list[WaveSpec] = []
        elif not isinstance(raw_waves, list) or not raw_waves:
            raise BatchSpecError("batch spec field 'waves' must be a non-empty list when present")
        else:
            waves = [WaveSpec.from_dict(entry, index=index) for index, entry in enumerate(raw_waves)]
        credit_guard = None
        if "credit_guard" in payload:
            credit_guard = CreditGuard.from_dict(payload["credit_guard"])
        if waves and credit_guard is None:
            raise BatchSpecError("batch spec field 'waves' requires a sealed fail-closed credit_guard")
        raw_concurrency = payload.get("concurrency", DEFAULT_CONCURRENCY)
        if isinstance(raw_concurrency, bool) or not isinstance(raw_concurrency, int) or raw_concurrency < 1:
            raise BatchSpecError("batch spec concurrency must be an integer >= 1")
        raw_stagger = payload.get("stagger_seconds", 0.0)
        if (
            isinstance(raw_stagger, bool)
            or not isinstance(raw_stagger, (int, float))
            or not math.isfinite(float(raw_stagger))
            or float(raw_stagger) < 0
        ):
            raise BatchSpecError("batch spec stagger_seconds must be a finite number >= 0")
        spec = BatchSpec(
            runs=runs,
            root=payload.get("root"),
            concurrency=raw_concurrency,
            stagger_seconds=float(raw_stagger),
            credit_guard=credit_guard,
            waves=waves,
            source_sha256=hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        )
        _validate_wave_partition(spec)
        return spec


def _validate_wave_partition(spec: BatchSpec) -> None:
    """Validate an optional wave declaration against the full run list."""
    if not spec.waves:
        return
    problems: list[str] = []
    full_run_ids = [run.run_id for run in spec.runs]
    known = set(full_run_ids)
    if len(known) != len(full_run_ids):
        problems.append("waves require unique run_id values in the full runs list")
    seen_wave_ids: set[str] = set()
    seen_run_ids: set[str] = set()
    for wave in spec.waves:
        if wave.wave_id in seen_wave_ids:
            problems.append(f"duplicate wave_id: {wave.wave_id!r}")
        seen_wave_ids.add(wave.wave_id)
        local_seen: set[str] = set()
        for run_id in wave.run_ids:
            if run_id in local_seen:
                problems.append(f"duplicate run_id {run_id!r} within wave {wave.wave_id!r}")
            local_seen.add(run_id)
            if run_id not in known:
                problems.append(f"unknown run_id {run_id!r} in wave {wave.wave_id!r}")
            if run_id in seen_run_ids:
                problems.append(f"run_id {run_id!r} appears in more than one wave")
            seen_run_ids.add(run_id)
    missing = [run_id for run_id in full_run_ids if run_id not in seen_run_ids]
    if missing:
        problems.append(f"wave partition is missing run_id(s): {missing}")
    if problems:
        raise BatchSpecError("batch spec wave validation failed:\n  - " + "\n  - ".join(problems))


def select_wave(spec: BatchSpec, wave_id: str) -> BatchSpec:
    """Return the requested wave in its sealed run order.

    The full spec is validated by :meth:`BatchSpec.from_dict` before this
    selection. Clearing ``waves`` on the returned operational subset prevents
    later filesystem validation from treating already-completed waves as part
    of the current launch while retaining the full-spec hash and credit guard.
    """
    if not isinstance(wave_id, str) or not wave_id.strip():
        raise BatchSpecError("--wave must name a non-empty wave_id")
    if not spec.waves:
        raise BatchSpecError(f"batch spec does not declare waves; cannot select {wave_id!r}")
    matches = [wave for wave in spec.waves if wave.wave_id == wave_id]
    if not matches:
        raise BatchSpecError(f"unknown wave_id {wave_id!r}; expected one of {[wave.wave_id for wave in spec.waves]}")
    by_id = {run.run_id: run for run in spec.runs}
    selected = matches[0]
    return BatchSpec(
        runs=[copy.deepcopy(by_id[run_id]) for run_id in selected.run_ids],
        root=spec.root,
        concurrency=spec.concurrency,
        stagger_seconds=spec.stagger_seconds,
        credit_guard=spec.credit_guard,
        waves=[],
        selected_wave_id=selected.wave_id,
        source_sha256=spec.source_sha256,
    )


def validate_batch_spec(
    spec: BatchSpec,
    *,
    base_dir: Path,
    check_existing_roots: bool = True,
) -> None:
    """Fail loudly on any structural problem BEFORE any subprocess launches.

    Checked, in order: duplicate run_ids, duplicate run_roots (after path
    resolution, so `runs/a` and `./runs/a` collide correctly), and any
    run_root that already exists on disk. Raises BatchSpecError with every
    problem found (not just the first) so a caller fixing a spec by hand
    does not have to run validation repeatedly to discover each issue.
    """
    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_roots: dict[Path, str] = {}
    resolved: dict[str, Path] = {}
    for run in spec.runs:
        if run.run_id in seen_ids:
            problems.append(f"duplicate run_id: {run.run_id!r}")
        seen_ids.add(run.run_id)
        resolved_root = _resolve_run_root(base_dir, run.run_root)
        resolved[run.run_id] = resolved_root
        if resolved_root in seen_roots:
            problems.append(f"duplicate run_root: {run.run_root!r} (run_id {run.run_id!r} collides with {seen_roots[resolved_root]!r})")
        seen_roots[resolved_root] = run.run_id
    if check_existing_roots:
        for run in spec.runs:
            resolved_root = resolved[run.run_id]
            if resolved_root.exists():
                problems.append(f"run_root already exists, refusing to overwrite: {resolved_root} (run_id {run.run_id!r})")
    if spec.concurrency < 1:
        problems.append(f"concurrency must be >= 1, got {spec.concurrency}")
    if not math.isfinite(spec.stagger_seconds) or spec.stagger_seconds < 0:
        problems.append(f"stagger_seconds must be finite and >= 0, got {spec.stagger_seconds}")
    if problems:
        raise BatchSpecError("batch spec validation failed:\n  - " + "\n  - ".join(problems))


@dataclass
class RunResult:
    run_id: str
    run_root: str
    stage: str
    cmd: list[str]
    log_path: str
    started_at: str | None = None
    ended_at: str | None = None
    exit_code: int | None = None
    status: str = "pending"  # pending | running | succeeded | failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_root": self.run_root,
            "stage": self.stage,
            "cmd": self.cmd,
            "log_path": self.log_path,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "status": self.status,
        }


def _git_commit(root: Path) -> str:
    """Best-effort git commit of the code executing this batch. Never raises."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        commit = result.stdout.strip()
        return commit if result.returncode == 0 and commit else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def validate_managed_checkout(root: Path, *, plan_path: Path, batch_spec_path: Path) -> str:
    """Require a clean exact-HEAD checkout before any managed live spend."""
    root = root.resolve()
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root:
            raise BatchSpecError("managed execution --root must be the Git worktree root")
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if status.returncode != 0:
            raise BatchSpecError("cannot verify managed execution Git worktree status")
        if status.stdout.strip():
            raise BatchSpecError("managed execution requires a clean Git worktree (including untracked files)")
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        commit_sha = commit.stdout.strip()
        if commit.returncode != 0 or len(commit_sha) != 40:
            raise BatchSpecError("managed execution requires a valid Git HEAD")
        for label, raw_path in (("plan", plan_path), ("batch spec", batch_spec_path)):
            path = raw_path.resolve()
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError as exc:
                raise BatchSpecError(f"managed {label} must stay within --root") from exc
            committed = subprocess.run(
                ["git", "show", f"HEAD:{relative}"],
                cwd=str(root),
                capture_output=True,
                timeout=15,
                check=False,
            )
            if committed.returncode != 0:
                raise BatchSpecError(f"managed {label} must be tracked at HEAD")
            if committed.stdout != path.read_bytes():
                raise BatchSpecError(f"managed {label} bytes differ from HEAD")
        return commit_sha
    except (OSError, subprocess.SubprocessError) as exc:
        raise BatchSpecError(f"cannot verify managed execution Git checkout: {exc}") from exc


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_credits_http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
    """GET `url`, returning (status_code, body_text). HTTP errors are returned,
    not raised, so a 402/500 from the credits endpoint is data, not a crash."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def check_openrouter_credits(
    *,
    api_key: str | None,
    http_get: Any = None,
    timeout: float = CREDITS_CHECK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Query the OpenRouter credits endpoint and report the remaining balance.

    Added after the 2026-07-06 incident (batch ran out of credits mid-flight,
    402s failed 8/11 runs). Returns a plain dict so it can be embedded
    verbatim in batch_manifest.json:

      status: "ok" | "skipped" | "unavailable"
      remaining_credits / total_credits / total_usage: floats when status=="ok"
      detail: human-readable reason when status != "ok"
      checked_at: UTC ISO timestamp

    NEVER raises and never blocks by itself -- "the endpoint is down" must not
    be able to stop a batch (the caller decides what to do with a low
    balance). `http_get(url, headers, timeout) -> (status_code, body_text)`
    defaults to a stdlib urllib GET; tests substitute a stub here so no
    network call ever happens offline.
    """
    checked_at = _now_iso()
    report: dict[str, Any] = {
        "status": "unavailable",
        "remaining_credits": None,
        "total_credits": None,
        "total_usage": None,
        "detail": None,
        "checked_at": checked_at,
    }
    if not api_key:
        report["status"] = "skipped"
        report["detail"] = "OPENROUTER_API_KEY is not set; credits preflight skipped"
        return report
    http_get = http_get or _default_credits_http_get
    try:
        status_code, body = http_get(OPENROUTER_CREDITS_URL, {"Authorization": f"Bearer {api_key}"}, timeout)
    except Exception as exc:  # noqa: BLE001 -- an unreachable endpoint must warn, never block
        report["detail"] = f"credits endpoint unreachable: {exc}"
        return report
    if status_code != 200:
        report["detail"] = f"credits endpoint returned HTTP {status_code}"
        return report
    try:
        data = json.loads(body)["data"]
        total_credits = float(data["total_credits"])
        total_usage = float(data["total_usage"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        report["detail"] = f"credits endpoint returned an unexpected payload: {exc!r}"
        return report
    remaining_credits = total_credits - total_usage
    if (
        not math.isfinite(total_credits)
        or not math.isfinite(total_usage)
        or not math.isfinite(remaining_credits)
        or total_credits < 0
        or total_usage < 0
        or remaining_credits < 0
    ):
        report["detail"] = "credits endpoint returned non-finite or negative credit values"
        return report
    report["status"] = "ok"
    report["total_credits"] = total_credits
    report["total_usage"] = total_usage
    report["remaining_credits"] = remaining_credits
    return report


def _default_python_cmd(run: RunSpec) -> list[str]:
    return [sys.executable, "-m", "company_twin.cli", *run.build_cli_args()]


def _enforce_credit_guard_preflight(
    credit_guard: dict[str, Any] | None,
    credits_preflight: dict[str, Any] | None,
    *,
    require_fresh: bool = False,
) -> None:
    """Final no-side-effect balance boundary used by every run_batch caller."""
    if credit_guard is None:
        return
    require_available = credit_guard["require_available"]
    abort_on_low = credit_guard["abort_on_low_credits"]
    if not isinstance(credits_preflight, dict) or credits_preflight.get("status") != "ok":
        if require_available:
            raise BatchSpecError("credit_guard requires a successful credits_preflight before launch")
        return
    remaining = credits_preflight.get("remaining_credits")
    if (
        isinstance(remaining, bool)
        or not isinstance(remaining, (int, float))
        or not math.isfinite(float(remaining))
        or float(remaining) < 0
    ):
        raise BatchSpecError("credits_preflight.remaining_credits must be a finite non-negative number")
    if float(remaining) < float(credit_guard["minimum_credits"]) and abort_on_low:
        raise BatchSpecError("credits_preflight remaining balance is below credit_guard.minimum_credits")
    if require_fresh:
        checked_at = credits_preflight.get("checked_at")
        if not isinstance(checked_at, str) or not checked_at:
            raise BatchSpecError("managed credits_preflight.checked_at is required")
        try:
            checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BatchSpecError("managed credits_preflight.checked_at must be an ISO timestamp") from exc
        if checked.tzinfo is None:
            raise BatchSpecError("managed credits_preflight.checked_at must include a timezone")
        now = datetime.now(timezone.utc)
        age_seconds = (now - checked.astimezone(timezone.utc)).total_seconds()
        if age_seconds < -30:
            raise BatchSpecError("managed credits_preflight.checked_at is in the future")
        if age_seconds > 300:
            raise BatchSpecError("managed credits_preflight.checked_at is older than 5 minutes")


def run_batch(
    spec: BatchSpec,
    *,
    base_dir: Path,
    batch_dir: Path,
    command_builder: Any = None,
    env: dict[str, str] | None = None,
    credits_preflight: dict[str, Any] | None = None,
    batch_spec_sha256: str | None = None,
    wave_id: str | None = None,
    credit_guard: dict[str, Any] | None = None,
    plan_sha256: str | None = None,
    plan_id: str | None = None,
    managed_execution: bool = False,
    execution_git_commit: str | None = None,
) -> dict[str, Any]:
    """Execute every run in `spec` under a bounded worker pool.

    `command_builder(run: RunSpec) -> list[str]` defaults to spawning
    `python -m company_twin.cli <stage> ...`; tests substitute a cheap stub
    command here so no real world ever executes offline. `base_dir` is the
    cwd for every subprocess (matches `--root` resolution / `.env.local`
    discovery); `batch_dir` is where per-run logs and `batch_manifest.json`
    are written. Validation (see `validate_batch_spec`) must already have
    passed -- this function does not re-check run_root existence, so callers
    (the CLI command, tests) call `validate_batch_spec` first every time.
    `credits_preflight` is the (already-completed) `check_openrouter_credits`
    report from just before launch; it is recorded verbatim in
    batch_manifest.json so a post-mortem can see what the balance was when
    the batch started. `batch_spec_sha256` binds every wave and retry back to
    the exact full input file; `wave_id` and the effective `credit_guard` are
    recorded beside it for fail-closed campaign collation.
    """
    command_builder = command_builder or _default_python_cmd
    effective_hash = batch_spec_sha256 or spec.source_sha256
    if (
        not isinstance(effective_hash, str)
        or len(effective_hash) != 64
        or any(ch not in "0123456789abcdef" for ch in effective_hash)
    ):
        raise BatchSpecError("run_batch requires a lowercase 64-character batch_spec_sha256")
    effective_wave_id = wave_id if wave_id is not None else spec.selected_wave_id
    if wave_id is not None and spec.selected_wave_id is not None and wave_id != spec.selected_wave_id:
        raise BatchSpecError(
            f"run_batch wave_id {wave_id!r} conflicts with selected wave {spec.selected_wave_id!r}"
        )
    if credit_guard is not None:
        # The effective guard may be legacy/warn-only, so validate its shape
        # here without imposing the sealed all-true policy.
        if not isinstance(credit_guard, dict):
            raise BatchSpecError("effective credit_guard must be an object or null")
        expected_guard_keys = {"minimum_credits", "abort_on_low_credits", "require_available"}
        if set(credit_guard) != expected_guard_keys:
            raise BatchSpecError(f"effective credit_guard must contain exactly {sorted(expected_guard_keys)}")
        minimum = credit_guard["minimum_credits"]
        if (
            isinstance(minimum, bool)
            or not isinstance(minimum, (int, float))
            or not math.isfinite(float(minimum))
            or float(minimum) <= 0
        ):
            raise BatchSpecError("effective credit_guard.minimum_credits must be a positive finite number")
        if (
            type(credit_guard["abort_on_low_credits"]) is not bool
            or type(credit_guard["require_available"]) is not bool
        ):
            raise BatchSpecError("effective credit_guard boolean fields must be booleans")
        credit_guard = {
            "minimum_credits": float(minimum),
            "abort_on_low_credits": credit_guard["abort_on_low_credits"],
            "require_available": credit_guard["require_available"],
        }
    is_managed = managed_execution or spec.credit_guard is not None
    if is_managed:
        sealed_guard = spec.credit_guard.to_dict() if spec.credit_guard is not None else None
        if sealed_guard is not None and credit_guard != sealed_guard:
            raise BatchSpecError("managed run_batch credit_guard must exactly match spec.credit_guard")
        if (
            not isinstance(plan_sha256, str)
            or len(plan_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in plan_sha256)
        ):
            raise BatchSpecError("managed run_batch requires a lowercase 64-character plan_sha256")
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise BatchSpecError("managed run_batch requires a non-empty plan_id")
        if (
            not isinstance(execution_git_commit, str)
            or len(execution_git_commit) != 40
            or any(ch not in "0123456789abcdef" for ch in execution_git_commit)
        ):
            raise BatchSpecError("managed run_batch requires a full lowercase execution_git_commit")
        if _git_commit(base_dir) != execution_git_commit:
            raise BatchSpecError("managed run_batch checkout commit changed before launch")
        resolved_batch_dir = batch_dir.resolve()
        try:
            resolved_batch_dir.relative_to(base_dir.resolve())
        except ValueError as exc:
            raise BatchSpecError("managed run_batch batch_dir must stay within base_dir") from exc
        if resolved_batch_dir.exists():
            raise BatchSpecError("managed run_batch refuses to overwrite an existing batch_dir")
    _enforce_credit_guard_preflight(credit_guard, credits_preflight, require_fresh=is_managed)
    batch_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = batch_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, RunResult] = {}
    for run in spec.runs:
        cmd = command_builder(run)
        log_path = logs_dir / f"{run.run_id}.log"
        results[run.run_id] = RunResult(run_id=run.run_id, run_root=run.run_root, stage=run.stage, cmd=cmd, log_path=str(log_path))

    subprocess_env = dict(env if env is not None else os.environ)

    pending = list(spec.runs)
    active: dict[str, tuple[subprocess.Popen, Any]] = {}
    batch_started_at = _now_iso()

    def _launch(run: RunSpec) -> None:
        result = results[run.run_id]
        log_file = open(result.log_path, "w", encoding="utf-8")
        result.started_at = _now_iso()
        result.status = "running"
        proc = subprocess.Popen(
            result.cmd,
            cwd=str(base_dir),
            env=subprocess_env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        active[run.run_id] = (proc, log_file)

    while pending or active:
        while pending and len(active) < spec.concurrency:
            run = pending.pop(0)
            _launch(run)
            if spec.stagger_seconds > 0 and (pending or len(active) < spec.concurrency):
                time.sleep(spec.stagger_seconds)
        finished_ids = []
        for run_id, (proc, log_file) in active.items():
            exit_code = proc.poll()
            if exit_code is not None:
                finished_ids.append((run_id, exit_code, log_file))
        for run_id, exit_code, log_file in finished_ids:
            log_file.close()
            result = results[run_id]
            result.exit_code = exit_code
            result.ended_at = _now_iso()
            result.status = "succeeded" if exit_code == 0 else "failed"
            del active[run_id]
        if not finished_ids and active:
            time.sleep(0.2)

    batch_ended_at = _now_iso()
    ordered = [results[run.run_id] for run in spec.runs]
    any_failed = any(r.status != "succeeded" for r in ordered)
    manifest = {
        "schema_version": BATCH_MANIFEST_SCHEMA_VERSION,
        "batch_dir": str(batch_dir),
        "root": str(base_dir),
        "git_commit": execution_git_commit or _git_commit(base_dir),
        "concurrency": spec.concurrency,
        "stagger_seconds": spec.stagger_seconds,
        "batch_spec_sha256": effective_hash,
        "wave_id": effective_wave_id,
        "credit_guard": credit_guard,
        "plan_sha256": plan_sha256,
        "plan_id": plan_id,
        "started_at": batch_started_at,
        "ended_at": batch_ended_at,
        "credits_preflight": credits_preflight,
        "runs": [r.to_dict() for r in ordered],
        "failed_run_ids": [r.run_id for r in ordered if r.status != "succeeded"],
        "passed": not any_failed,
    }
    (batch_dir / BATCH_MANIFEST_FILENAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def load_batch_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != BATCH_MANIFEST_SCHEMA_VERSION:
        raise BatchSpecError(f"unexpected batch manifest schema: {payload.get('schema_version')!r}")
    return payload


def _parse_manifest_time(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise BatchSpecError(f"retry manifest {label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BatchSpecError(f"retry manifest {label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise BatchSpecError(f"retry manifest {label} must include a timezone")
    return parsed


def validate_retry_manifest(
    manifest: dict[str, Any],
    *,
    manifest_path: Path,
    original_spec: BatchSpec,
    base_dir: Path,
) -> None:
    """Fully validate an immediate retry source before any root is deleted."""
    if manifest.get("schema_version") != BATCH_MANIFEST_SCHEMA_VERSION:
        raise BatchSpecError("retry manifest has an unexpected schema_version")
    base_dir = base_dir.resolve()
    manifest_path = manifest_path.resolve()
    root_value = manifest.get("root")
    if not isinstance(root_value, str) or not Path(root_value).is_absolute() or Path(root_value).resolve() != base_dir:
        raise BatchSpecError("retry manifest root must equal the current absolute --root")
    batch_dir_value = manifest.get("batch_dir")
    if not isinstance(batch_dir_value, str) or not Path(batch_dir_value).is_absolute():
        raise BatchSpecError("retry manifest batch_dir must be an absolute path")
    batch_dir = Path(batch_dir_value).resolve()
    try:
        batch_dir.relative_to(base_dir)
    except ValueError as exc:
        raise BatchSpecError("retry manifest batch_dir must stay within --root") from exc
    if batch_dir != manifest_path.parent:
        raise BatchSpecError("retry manifest batch_dir must equal the manifest's containing directory")
    if manifest.get("concurrency") != original_spec.concurrency:
        raise BatchSpecError("retry manifest concurrency differs from the sealed batch spec")
    if manifest.get("stagger_seconds") != original_spec.stagger_seconds:
        raise BatchSpecError("retry manifest stagger_seconds differs from the sealed batch spec")
    commit = manifest.get("git_commit")
    if (
        not isinstance(commit, str)
        or len(commit) != 40
        or any(ch not in "0123456789abcdef" for ch in commit)
    ):
        raise BatchSpecError("retry manifest git_commit must be a full lowercase commit SHA")
    manifest_start = _parse_manifest_time(manifest.get("started_at"), label="started_at")
    manifest_end = _parse_manifest_time(manifest.get("ended_at"), label="ended_at")
    if manifest_start > manifest_end:
        raise BatchSpecError("retry manifest timestamps are reversed")
    rows = manifest.get("runs")
    if not isinstance(rows, list) or not rows:
        raise BatchSpecError("retry manifest requires a non-empty runs list")
    specs_by_id = {run.run_id: run for run in original_spec.runs}
    seen: set[str] = set()
    failed_ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise BatchSpecError("retry manifest run rows must be objects")
        run_id = row.get("run_id")
        if not isinstance(run_id, str) or run_id not in specs_by_id or run_id in seen:
            raise BatchSpecError(f"retry manifest has missing, duplicate, or unexpected run_id {run_id!r}")
        seen.add(run_id)
        spec = specs_by_id[run_id]
        if row.get("stage") != spec.stage:
            raise BatchSpecError(f"retry manifest stage drift for {run_id}")
        run_root = row.get("run_root")
        if not isinstance(run_root, str) or _resolve_run_root(base_dir, run_root) != _resolve_run_root(base_dir, spec.run_root):
            raise BatchSpecError(f"retry manifest run_root drift for {run_id}")
        cmd = row.get("cmd")
        if (
            not isinstance(cmd, list)
            or len(cmd) < 4
            or not isinstance(cmd[0], str)
            or not cmd[0]
            or cmd[1:3] != ["-m", "company_twin.cli"]
            or cmd[3:] != spec.build_cli_args()
        ):
            raise BatchSpecError(f"retry manifest command drift for {run_id}")
        status = row.get("status")
        exit_code = row.get("exit_code")
        if status not in {"succeeded", "failed"}:
            raise BatchSpecError(f"retry manifest run {run_id} has invalid status")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise BatchSpecError(f"retry manifest run {run_id} has invalid exit_code")
        if (status == "succeeded") != (exit_code == 0):
            raise BatchSpecError(f"retry manifest run {run_id} status and exit_code disagree")
        expected_log = (batch_dir / "logs" / f"{run_id}.log").resolve()
        log_path = row.get("log_path")
        if not isinstance(log_path, str) or Path(log_path).resolve() != expected_log:
            raise BatchSpecError(f"retry manifest log_path drift for {run_id}")
        started = _parse_manifest_time(row.get("started_at"), label=f"{run_id}.started_at")
        ended = _parse_manifest_time(row.get("ended_at"), label=f"{run_id}.ended_at")
        if started > ended or started < manifest_start or ended > manifest_end:
            raise BatchSpecError(f"retry manifest run timestamps are inconsistent for {run_id}")
        if status == "failed":
            failed_ids.append(run_id)
    if manifest.get("failed_run_ids") != failed_ids:
        raise BatchSpecError("retry manifest failed_run_ids disagree with its run rows")
    if not isinstance(manifest.get("passed"), bool) or manifest["passed"] != (not failed_ids):
        raise BatchSpecError("retry manifest passed flag disagrees with its run rows")
    if not failed_ids:
        raise BatchSpecError("retry manifest reports no failed runs; nothing to retry")
    guard = manifest.get("credit_guard")
    if guard is not None:
        expected_guard_keys = {"minimum_credits", "abort_on_low_credits", "require_available"}
        if not isinstance(guard, dict) or set(guard) != expected_guard_keys:
            raise BatchSpecError("retry manifest credit_guard has missing or unknown fields")
        minimum = guard["minimum_credits"]
        if (
            isinstance(minimum, bool)
            or not isinstance(minimum, (int, float))
            or not math.isfinite(float(minimum))
            or float(minimum) <= 0
            or type(guard["abort_on_low_credits"]) is not bool
            or type(guard["require_available"]) is not bool
        ):
            raise BatchSpecError("retry manifest credit_guard has invalid values")
        _enforce_credit_guard_preflight(guard, manifest.get("credits_preflight"))


def build_retry_spec(
    manifest: dict[str, Any],
    *,
    original_spec: BatchSpec,
) -> BatchSpec:
    """Build a BatchSpec containing only the failed runs from `manifest`.

    Looks up each failed run_id's full RunSpec from `original_spec` (the
    manifest itself does not carry enough to reconstruct build_cli_args
    beyond the already-materialized `cmd`, and reusing the original spec
    keeps a single source of truth for what a "run" is). Raises if a failed
    run_id in the manifest cannot be found in `original_spec` (spec drift
    between the original run and the retry attempt must be visible, not
    silently ignored).
    """
    failed_ids = set(manifest.get("failed_run_ids") or [])
    if not failed_ids:
        raise BatchSpecError("batch manifest reports no failed runs; nothing to retry")
    by_id = {run.run_id: run for run in original_spec.runs}
    missing = failed_ids - set(by_id)
    if missing:
        raise BatchSpecError(f"failed run_id(s) not found in the provided batch spec: {sorted(missing)}")
    retry_runs = [copy.deepcopy(by_id[run["run_id"]]) for run in manifest["runs"] if run["run_id"] in failed_ids]
    return BatchSpec(
        runs=retry_runs,
        root=original_spec.root,
        concurrency=original_spec.concurrency,
        stagger_seconds=original_spec.stagger_seconds,
        credit_guard=original_spec.credit_guard,
        waves=[],
        selected_wave_id=original_spec.selected_wave_id,
        source_sha256=original_spec.source_sha256,
    )


def delete_partial_roots(spec: BatchSpec, *, base_dir: Path) -> list[str]:
    """Delete each run's run_root directory if it exists. Caller-gated only.

    Never called implicitly -- the CLI's `--retry-failed` path only reaches
    this when the caller also passed `--delete-partial-roots`. Returns the
    list of paths actually removed (for the caller to log/echo).
    """
    removed: list[str] = []
    for run in spec.runs:
        resolved_root = _resolve_run_root(base_dir, run.run_root)
        if resolved_root.exists():
            shutil.rmtree(resolved_root)
            removed.append(str(resolved_root))
    return removed
