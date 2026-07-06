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
(`credits_preflight`). The check itself never blocks a batch when the
endpoint is unreachable or returns garbage: an unavailable balance check is a
warning, not a launch failure, because the endpoint being down says nothing
about whether the runs would succeed.
"""
from __future__ import annotations

import copy
import json
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

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "BatchSpec":
        raw_runs = payload.get("runs")
        if not isinstance(raw_runs, list) or not raw_runs:
            raise BatchSpecError("batch spec must contain a non-empty 'runs' list")
        runs = [RunSpec.from_dict(entry) for entry in raw_runs]
        return BatchSpec(
            runs=runs,
            root=payload.get("root"),
            concurrency=int(payload.get("concurrency", DEFAULT_CONCURRENCY)),
            stagger_seconds=float(payload.get("stagger_seconds", 0.0)),
        )


def validate_batch_spec(spec: BatchSpec, *, base_dir: Path) -> None:
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
    for run in spec.runs:
        resolved_root = resolved[run.run_id]
        if resolved_root.exists():
            problems.append(f"run_root already exists, refusing to overwrite: {resolved_root} (run_id {run.run_id!r})")
    if spec.concurrency < 1:
        problems.append(f"concurrency must be >= 1, got {spec.concurrency}")
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
    report["status"] = "ok"
    report["total_credits"] = total_credits
    report["total_usage"] = total_usage
    report["remaining_credits"] = total_credits - total_usage
    return report


def _default_python_cmd(run: RunSpec) -> list[str]:
    return [sys.executable, "-m", "company_twin.cli", *run.build_cli_args()]


def run_batch(
    spec: BatchSpec,
    *,
    base_dir: Path,
    batch_dir: Path,
    command_builder: Any = None,
    env: dict[str, str] | None = None,
    credits_preflight: dict[str, Any] | None = None,
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
    the batch started.
    """
    command_builder = command_builder or _default_python_cmd
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
        "git_commit": _git_commit(base_dir),
        "concurrency": spec.concurrency,
        "stagger_seconds": spec.stagger_seconds,
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
    return BatchSpec(runs=retry_runs, root=original_spec.root, concurrency=original_spec.concurrency, stagger_seconds=original_spec.stagger_seconds)


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
