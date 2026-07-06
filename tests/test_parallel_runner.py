"""Tests for the WP-12 parallel world-run executor (company_twin.parallel_runner).

All fixtures here are offline: no real `python -m company_twin.cli s0/s1/s2/...`
run is ever launched. Orchestration is exercised with a fake, near-instant
subprocess command (a `python -c` stub) substituted via `command_builder`, so
these tests prove the ORCHESTRATION (concurrency cap, failure isolation,
manifest correctness, retry-failed flow, spec validation) without touching
any real world logic.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from company_twin.cli import app
from company_twin.parallel_runner import (
    BATCH_MANIFEST_FILENAME,
    OPENROUTER_CREDITS_URL,
    BatchSpec,
    BatchSpecError,
    RunSpec,
    build_retry_spec,
    check_openrouter_credits,
    delete_partial_roots,
    load_batch_manifest,
    run_batch,
    validate_batch_spec,
)


def _run_dict(run_id: str, run_root: str, **extra) -> dict:
    payload = {"run_id": run_id, "stage": "s2", "run_root": run_root, "seed": 0, "ticks": 4}
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# batch spec validation
# ---------------------------------------------------------------------------


def test_validate_batch_spec_rejects_duplicate_run_roots(tmp_path: Path) -> None:
    spec = BatchSpec.from_dict({"runs": [_run_dict("a", "runs/shared"), _run_dict("b", "runs/shared")]})
    with pytest.raises(BatchSpecError, match="duplicate run_root"):
        validate_batch_spec(spec, base_dir=tmp_path)


def test_validate_batch_spec_rejects_duplicate_run_ids(tmp_path: Path) -> None:
    spec = BatchSpec.from_dict({"runs": [_run_dict("dup", "runs/one"), _run_dict("dup", "runs/two")]})
    with pytest.raises(BatchSpecError, match="duplicate run_id"):
        validate_batch_spec(spec, base_dir=tmp_path)


def test_validate_batch_spec_rejects_existing_run_root(tmp_path: Path) -> None:
    existing = tmp_path / "runs" / "already_there"
    existing.mkdir(parents=True)
    spec = BatchSpec.from_dict({"runs": [_run_dict("a", "runs/already_there")]})
    with pytest.raises(BatchSpecError, match="already exists"):
        validate_batch_spec(spec, base_dir=tmp_path)


def test_validate_batch_spec_reports_multiple_problems_together(tmp_path: Path) -> None:
    existing = tmp_path / "runs" / "taken"
    existing.mkdir(parents=True)
    spec = BatchSpec.from_dict(
        {
            "runs": [
                _run_dict("a", "runs/taken"),
                _run_dict("b", "runs/x"),
                _run_dict("b", "runs/x"),
            ]
        }
    )
    with pytest.raises(BatchSpecError) as excinfo:
        validate_batch_spec(spec, base_dir=tmp_path)
    message = str(excinfo.value)
    assert "already exists" in message
    assert "duplicate run_id" in message
    assert "duplicate run_root" in message


def test_validate_batch_spec_accepts_clean_spec(tmp_path: Path) -> None:
    spec = BatchSpec.from_dict({"runs": [_run_dict("a", "runs/fresh_a"), _run_dict("b", "runs/fresh_b")]})
    validate_batch_spec(spec, base_dir=tmp_path)  # must not raise


def test_run_spec_requires_core_fields() -> None:
    with pytest.raises(BatchSpecError, match="run_id"):
        RunSpec.from_dict({"stage": "s2", "run_root": "runs/x"})
    with pytest.raises(BatchSpecError, match="stage"):
        RunSpec.from_dict({"run_id": "a", "run_root": "runs/x"})
    with pytest.raises(BatchSpecError, match="run_root"):
        RunSpec.from_dict({"run_id": "a", "stage": "s2"})


def test_run_spec_rejects_unknown_stage() -> None:
    with pytest.raises(BatchSpecError, match="unknown stage"):
        RunSpec.from_dict({"run_id": "a", "stage": "s99", "run_root": "runs/x"})


def test_build_cli_args_reuses_existing_single_run_flags() -> None:
    run = RunSpec.from_dict(
        {
            "run_id": "holdout_x",
            "stage": "s2",
            "run_root": "runs/holdout_x",
            "seed": 3,
            "ticks": 40,
            "prompt_mode": "measurement",
            "model": "openrouter:qwen/qwen3.6-flash",
            "mutations": ["clarify_elderly_understanding_all"],
        }
    )
    args = run.build_cli_args()
    assert args[0] == "s2"
    assert "--seed" in args and args[args.index("--seed") + 1] == "3"
    assert "--ticks" in args and args[args.index("--ticks") + 1] == "40"
    assert "--run-root" in args and args[args.index("--run-root") + 1] == "runs/holdout_x"
    assert "--prompt-mode" in args and args[args.index("--prompt-mode") + 1] == "measurement"
    assert "--model" in args and args[args.index("--model") + 1] == "openrouter:qwen/qwen3.6-flash"
    assert "--mutation" in args and args[args.index("--mutation") + 1] == "clarify_elderly_understanding_all"


def test_build_cli_args_control_pair_campaign_requires_manifest() -> None:
    run = RunSpec.from_dict({"run_id": "cp", "stage": "control-pair-campaign", "run_root": "runs/cp"})
    with pytest.raises(BatchSpecError, match="manifest"):
        run.build_cli_args()


# ---------------------------------------------------------------------------
# orchestration with a fake, cheap subprocess command
# ---------------------------------------------------------------------------


def _sleep_stub(seconds: float, *, exit_code: int = 0, marker: str | None = None) -> list[str]:
    """A tiny `python -c` command standing in for a real world run.

    Sleeps briefly (to let concurrency actually overlap runs in time), then
    exits with the requested code. Never touches company_twin at all.
    """
    code = f"import time,sys; time.sleep({seconds})"
    if marker:
        code += f"; print({marker!r})"
    code += f"; sys.exit({exit_code})"
    return [sys.executable, "-c", code]


def test_run_batch_writes_manifest_and_creates_run_roots(tmp_path: Path) -> None:
    base_dir = tmp_path / "root"
    base_dir.mkdir()
    spec = BatchSpec.from_dict({"runs": [_run_dict("a", "runs/a"), _run_dict("b", "runs/b")], "concurrency": 2})
    validate_batch_spec(spec, base_dir=base_dir)

    def command_builder(run: RunSpec) -> list[str]:
        # Fake command creates the run_root itself, as a real `s2` run would,
        # so the manifest's run_root bookkeeping is exercised end to end.
        root_path = base_dir / run.run_root
        code = (
            "import pathlib,sys; "
            f"pathlib.Path(r'{root_path}').mkdir(parents=True, exist_ok=True); "
            "sys.exit(0)"
        )
        return [sys.executable, "-c", code]

    batch_dir = tmp_path / "batch_out"
    manifest = run_batch(spec, base_dir=base_dir, batch_dir=batch_dir, command_builder=command_builder)

    assert manifest["passed"] is True
    assert manifest["concurrency"] == 2
    assert len(manifest["runs"]) == 2
    assert all(r["status"] == "succeeded" for r in manifest["runs"])
    assert all(r["exit_code"] == 0 for r in manifest["runs"])
    assert (base_dir / "runs" / "a").exists()
    assert (base_dir / "runs" / "b").exists()
    assert (batch_dir / BATCH_MANIFEST_FILENAME).exists()
    for r in manifest["runs"]:
        assert Path(r["log_path"]).exists()
    # timestamps recorded per-run
    assert all(r["started_at"] and r["ended_at"] for r in manifest["runs"])
    assert manifest["git_commit"]  # best-effort, but this repo IS a git repo


def test_run_batch_enforces_concurrency_cap(tmp_path: Path) -> None:
    base_dir = tmp_path / "root"
    base_dir.mkdir()
    n = 6
    cap = 2
    runs = [_run_dict(f"r{i}", f"runs/r{i}") for i in range(n)]
    spec = BatchSpec.from_dict({"runs": runs, "concurrency": cap})
    validate_batch_spec(spec, base_dir=base_dir)

    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()

    def command_builder(run: RunSpec) -> list[str]:
        start_marker = marker_dir / f"{run.run_id}.start"
        end_marker = marker_dir / f"{run.run_id}.end"
        code = (
            "import pathlib,time,sys; "
            f"pathlib.Path(r'{start_marker}').write_text('1'); "
            "time.sleep(0.4); "
            f"pathlib.Path(r'{end_marker}').write_text('1'); "
            "sys.exit(0)"
        )
        return [sys.executable, "-c", code]

    # Sample concurrently-active count while the batch runs.
    import threading

    max_active_observed = {"value": 0}
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            active = sum(
                1
                for run in runs
                if (marker_dir / f"{run['run_id']}.start").exists() and not (marker_dir / f"{run['run_id']}.end").exists()
            )
            max_active_observed["value"] = max(max_active_observed["value"], active)
            time.sleep(0.02)

    sampler_thread = threading.Thread(target=sampler)
    sampler_thread.start()
    manifest = run_batch(spec, base_dir=base_dir, batch_dir=tmp_path / "batch_out", command_builder=command_builder)
    stop.set()
    sampler_thread.join()

    assert manifest["passed"] is True
    assert max_active_observed["value"] <= cap
    assert max_active_observed["value"] >= 1


def test_run_batch_isolates_failures(tmp_path: Path) -> None:
    base_dir = tmp_path / "root"
    base_dir.mkdir()
    runs = [_run_dict("ok1", "runs/ok1"), _run_dict("bad", "runs/bad"), _run_dict("ok2", "runs/ok2")]
    spec = BatchSpec.from_dict({"runs": runs, "concurrency": 3})
    validate_batch_spec(spec, base_dir=base_dir)

    def command_builder(run: RunSpec) -> list[str]:
        exit_code = 1 if run.run_id == "bad" else 0
        return _sleep_stub(0.05, exit_code=exit_code)

    manifest = run_batch(spec, base_dir=base_dir, batch_dir=tmp_path / "batch_out", command_builder=command_builder)

    assert manifest["passed"] is False
    assert manifest["failed_run_ids"] == ["bad"]
    by_id = {r["run_id"]: r for r in manifest["runs"]}
    assert by_id["ok1"]["status"] == "succeeded"
    assert by_id["ok2"]["status"] == "succeeded"
    assert by_id["bad"]["status"] == "failed"
    assert by_id["bad"]["exit_code"] == 1
    # the other two runs still completed even though "bad" failed
    assert by_id["ok1"]["exit_code"] == 0
    assert by_id["ok2"]["exit_code"] == 0


def test_run_batch_stagger_delays_launch(tmp_path: Path) -> None:
    base_dir = tmp_path / "root"
    base_dir.mkdir()
    runs = [_run_dict("a", "runs/a"), _run_dict("b", "runs/b")]
    spec = BatchSpec.from_dict({"runs": runs, "concurrency": 2, "stagger_seconds": 0.3})
    validate_batch_spec(spec, base_dir=base_dir)

    def command_builder(run: RunSpec) -> list[str]:
        return _sleep_stub(0.05)

    manifest = run_batch(spec, base_dir=base_dir, batch_dir=tmp_path / "batch_out", command_builder=command_builder)
    started = sorted(r["started_at"] for r in manifest["runs"])
    from datetime import datetime

    t0 = datetime.fromisoformat(started[0])
    t1 = datetime.fromisoformat(started[1])
    assert (t1 - t0).total_seconds() >= 0.25


# ---------------------------------------------------------------------------
# retry-failed flow
# ---------------------------------------------------------------------------


def test_retry_failed_reruns_only_failures_into_same_roots(tmp_path: Path) -> None:
    base_dir = tmp_path / "root"
    base_dir.mkdir()
    runs = [_run_dict("ok", "runs/ok"), _run_dict("bad", "runs/bad")]
    spec = BatchSpec.from_dict({"runs": runs, "concurrency": 2})
    validate_batch_spec(spec, base_dir=base_dir)

    def failing_builder(run: RunSpec) -> list[str]:
        root_path = base_dir / run.run_root
        exit_code = 1 if run.run_id == "bad" else 0
        code = (
            "import pathlib,sys; "
            f"pathlib.Path(r'{root_path}').mkdir(parents=True, exist_ok=True); "
            f"sys.exit({exit_code})"
        )
        return [sys.executable, "-c", code]

    batch_dir = tmp_path / "batch_out"
    manifest = run_batch(spec, base_dir=base_dir, batch_dir=batch_dir, command_builder=failing_builder)
    assert manifest["failed_run_ids"] == ["bad"]
    manifest_path = batch_dir / BATCH_MANIFEST_FILENAME
    loaded = load_batch_manifest(manifest_path)

    retry_spec = build_retry_spec(loaded, original_spec=spec)
    assert [r.run_id for r in retry_spec.runs] == ["bad"]
    assert retry_spec.runs[0].run_root == "runs/bad"

    # partial root exists from the failed attempt; must not be silently reused
    assert (base_dir / "runs" / "bad").exists()
    removed = delete_partial_roots(retry_spec, base_dir=base_dir)
    assert str((base_dir / "runs" / "bad").resolve()) in removed
    assert not (base_dir / "runs" / "bad").exists()
    # the successful run's root is untouched by a retry-failed pass
    assert (base_dir / "runs" / "ok").exists()

    def succeeding_builder(run: RunSpec) -> list[str]:
        root_path = base_dir / run.run_root
        code = (
            "import pathlib,sys; "
            f"pathlib.Path(r'{root_path}').mkdir(parents=True, exist_ok=True); "
            "sys.exit(0)"
        )
        return [sys.executable, "-c", code]

    retry_manifest = run_batch(retry_spec, base_dir=base_dir, batch_dir=tmp_path / "retry_out", command_builder=succeeding_builder)
    assert retry_manifest["passed"] is True
    assert [r["run_id"] for r in retry_manifest["runs"]] == ["bad"]
    assert (base_dir / "runs" / "bad").exists()


def test_build_retry_spec_raises_when_no_failures() -> None:
    manifest = {"failed_run_ids": [], "runs": []}
    spec = BatchSpec.from_dict({"runs": [_run_dict("a", "runs/a")]})
    with pytest.raises(BatchSpecError, match="no failed runs"):
        build_retry_spec(manifest, original_spec=spec)


def test_delete_partial_roots_only_removes_existing_dirs(tmp_path: Path) -> None:
    base_dir = tmp_path / "root"
    base_dir.mkdir()
    (base_dir / "runs" / "present").mkdir(parents=True)
    spec = BatchSpec.from_dict({"runs": [_run_dict("present", "runs/present"), _run_dict("absent", "runs/absent")]})
    removed = delete_partial_roots(spec, base_dir=base_dir)
    assert len(removed) == 1
    assert not (base_dir / "runs" / "present").exists()


# ---------------------------------------------------------------------------
# CLI-level wiring (typer app, offline): argument plumbing + pre-launch guard
# ---------------------------------------------------------------------------


def test_cli_run_batch_fails_loudly_before_launch_on_existing_run_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No API key -> the credits preflight is skipped without any network call.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    runner = CliRunner()
    existing = tmp_path / "runs" / "clash"
    existing.mkdir(parents=True)
    batch_spec_path = tmp_path / "batch.json"
    batch_spec_path.write_text(
        json.dumps({"runs": [_run_dict("a", "runs/clash")], "concurrency": 1}),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["run-batch", "--batch-spec", str(batch_spec_path), "--root", str(tmp_path)])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_cli_run_batch_warns_above_rate_limit_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import company_twin.cli as cli_module

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def fake_run_batch(spec, *, base_dir, batch_dir, **kwargs):
        return {
            "schema_version": "company_twin.batch_manifest.v1",
            "passed": True,
            "runs": [],
            "failed_run_ids": [],
            "concurrency": spec.concurrency,
        }

    monkeypatch.setattr(cli_module, "run_batch", fake_run_batch)
    runner = CliRunner()
    batch_spec_path = tmp_path / "batch.json"
    batch_spec_path.write_text(
        json.dumps({"runs": [_run_dict("a", "runs/a")], "concurrency": 5}),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["run-batch", "--batch-spec", str(batch_spec_path), "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "concurrency=5" in result.output or "exceeds" in result.output


# ---------------------------------------------------------------------------
# credits preflight (incident 2026-07-06: batch exhausted OpenRouter credits
# mid-flight; 402 Insufficient credits failed 8/11 runs). All HTTP here is a
# stub -- no test ever touches the real endpoint.
# ---------------------------------------------------------------------------


def _credits_body(total: float, usage: float) -> str:
    return json.dumps({"data": {"total_credits": total, "total_usage": usage}})


def test_check_openrouter_credits_reports_remaining_balance() -> None:
    seen: dict = {}

    def stub_http_get(url: str, headers: dict, timeout: float) -> tuple[int, str]:
        seen["url"] = url
        seen["headers"] = headers
        return 200, _credits_body(10.0, 4.5)

    report = check_openrouter_credits(api_key="sk-or-test", http_get=stub_http_get)
    assert report["status"] == "ok"
    assert report["remaining_credits"] == pytest.approx(5.5)
    assert report["total_credits"] == pytest.approx(10.0)
    assert report["total_usage"] == pytest.approx(4.5)
    assert report["checked_at"]
    assert seen["url"] == OPENROUTER_CREDITS_URL
    assert seen["headers"]["Authorization"] == "Bearer sk-or-test"


def test_check_openrouter_credits_skipped_without_api_key() -> None:
    def must_not_be_called(url: str, headers: dict, timeout: float) -> tuple[int, str]:
        raise AssertionError("no API key -> no HTTP call")

    report = check_openrouter_credits(api_key=None, http_get=must_not_be_called)
    assert report["status"] == "skipped"
    assert "OPENROUTER_API_KEY" in report["detail"]
    assert report["remaining_credits"] is None


def test_check_openrouter_credits_non_200_is_unavailable_not_fatal() -> None:
    report = check_openrouter_credits(api_key="k", http_get=lambda u, h, t: (500, "boom"))
    assert report["status"] == "unavailable"
    assert "HTTP 500" in report["detail"]
    assert report["remaining_credits"] is None


def test_check_openrouter_credits_network_error_is_unavailable_not_raised() -> None:
    def exploding_http_get(url: str, headers: dict, timeout: float) -> tuple[int, str]:
        raise OSError("connection refused")

    report = check_openrouter_credits(api_key="k", http_get=exploding_http_get)
    assert report["status"] == "unavailable"
    assert "connection refused" in report["detail"]


def test_check_openrouter_credits_bad_payload_is_unavailable() -> None:
    for body in ("not json", json.dumps({"data": {}}), json.dumps({"data": {"total_credits": "x", "total_usage": None}})):
        report = check_openrouter_credits(api_key="k", http_get=lambda u, h, t, body=body: (200, body))
        assert report["status"] == "unavailable", body
        assert report["remaining_credits"] is None


def test_run_batch_records_credits_preflight_in_manifest(tmp_path: Path) -> None:
    base_dir = tmp_path / "root"
    base_dir.mkdir()
    spec = BatchSpec.from_dict({"runs": [_run_dict("a", "runs/a")]})
    validate_batch_spec(spec, base_dir=base_dir)
    preflight = check_openrouter_credits(api_key="k", http_get=lambda u, h, t: (200, _credits_body(20.0, 3.0)))

    manifest = run_batch(
        spec,
        base_dir=base_dir,
        batch_dir=tmp_path / "batch_out",
        command_builder=lambda run: _sleep_stub(0.0),
        credits_preflight=preflight,
    )
    assert manifest["credits_preflight"] == preflight

    persisted = json.loads((tmp_path / "batch_out" / BATCH_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert persisted["credits_preflight"]["remaining_credits"] == pytest.approx(17.0)


def _cli_credits_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preflight: dict) -> tuple[CliRunner, Path, dict]:
    """Stub both the credits check and run_batch on the CLI module; returns
    (runner, batch spec path, dict capturing the run_batch call)."""
    import company_twin.cli as cli_module

    monkeypatch.setattr(cli_module, "check_openrouter_credits", lambda **kwargs: preflight)
    launched: dict = {}

    def fake_run_batch(spec, *, base_dir, batch_dir, **kwargs):
        launched["spec"] = spec
        launched["kwargs"] = kwargs
        return {
            "schema_version": "company_twin.batch_manifest.v1",
            "passed": True,
            "runs": [],
            "failed_run_ids": [],
            "concurrency": spec.concurrency,
            "credits_preflight": kwargs.get("credits_preflight"),
        }

    monkeypatch.setattr(cli_module, "run_batch", fake_run_batch)
    batch_spec_path = tmp_path / "batch.json"
    batch_spec_path.write_text(json.dumps({"runs": [_run_dict("a", "runs/a")], "concurrency": 1}), encoding="utf-8")
    return CliRunner(), batch_spec_path, launched


def test_cli_run_batch_prints_balance_and_records_it_in_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    preflight = {
        "status": "ok",
        "remaining_credits": 15.5,
        "total_credits": 20.0,
        "total_usage": 4.5,
        "detail": None,
        "checked_at": "2026-07-07T00:00:00+00:00",
    }
    runner, batch_spec_path, launched = _cli_credits_fixture(tmp_path, monkeypatch, preflight)
    result = runner.invoke(app, ["run-batch", "--batch-spec", str(batch_spec_path), "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "OpenRouter credits remaining: 15.50" in result.output
    assert "warning" not in result.output
    assert launched["kwargs"]["credits_preflight"] == preflight


def test_cli_run_batch_low_balance_warns_but_launches_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    preflight = {
        "status": "ok",
        "remaining_credits": 0.4,
        "total_credits": 20.0,
        "total_usage": 19.6,
        "detail": None,
        "checked_at": "2026-07-07T00:00:00+00:00",
    }
    runner, batch_spec_path, launched = _cli_credits_fixture(tmp_path, monkeypatch, preflight)
    result = runner.invoke(
        app, ["run-batch", "--batch-spec", str(batch_spec_path), "--root", str(tmp_path), "--min-credits", "2.0"]
    )
    assert result.exit_code == 0
    assert "below --min-credits" in result.output
    assert launched  # warn-only default: the batch still launched


def test_cli_run_batch_low_balance_aborts_before_launch_with_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    preflight = {
        "status": "ok",
        "remaining_credits": 0.4,
        "total_credits": 20.0,
        "total_usage": 19.6,
        "detail": None,
        "checked_at": "2026-07-07T00:00:00+00:00",
    }
    runner, batch_spec_path, launched = _cli_credits_fixture(tmp_path, monkeypatch, preflight)
    result = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--root",
            str(tmp_path),
            "--min-credits",
            "2.0",
            "--abort-on-low-credits",
        ],
    )
    assert result.exit_code != 0
    assert "below --min-credits" in result.output
    assert not launched  # aborted BEFORE any run launched


def test_cli_run_batch_unavailable_endpoint_warns_and_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    preflight = {
        "status": "unavailable",
        "remaining_credits": None,
        "total_credits": None,
        "total_usage": None,
        "detail": "credits endpoint unreachable: connection refused",
        "checked_at": "2026-07-07T00:00:00+00:00",
    }
    runner, batch_spec_path, launched = _cli_credits_fixture(tmp_path, monkeypatch, preflight)
    result = runner.invoke(
        app,
        ["run-batch", "--batch-spec", str(batch_spec_path), "--root", str(tmp_path), "--abort-on-low-credits"],
    )
    assert result.exit_code == 0  # unavailable endpoint never blocks, even with the abort flag
    assert "unavailable" in result.output
    assert launched["kwargs"]["credits_preflight"] == preflight
