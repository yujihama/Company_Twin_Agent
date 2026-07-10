"""Tests for the WP-12 parallel world-run executor (company_twin.parallel_runner).

All fixtures here are offline: no real `python -m company_twin.cli s0/s1/s2/...`
run is ever launched. Orchestration is exercised with a fake, near-instant
subprocess command (a `python -c` stub) substituted via `command_builder`, so
these tests prove the ORCHESTRATION (concurrency cap, failure isolation,
manifest correctness, retry-failed flow, spec validation) without touching
any real world logic.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from company_twin.cli import app
from company_twin.parallel_runner import (
    BATCH_MANIFEST_FILENAME,
    OPENROUTER_CREDITS_URL,
    BatchSpec,
    BatchSpecError,
    CreditGuard,
    RunSpec,
    WaveSpec,
    build_retry_spec,
    check_openrouter_credits,
    delete_partial_roots,
    load_batch_manifest,
    run_batch,
    select_wave,
    validate_batch_spec,
)


def _run_dict(run_id: str, run_root: str, **extra) -> dict:
    payload = {"run_id": run_id, "stage": "s2", "run_root": run_root, "seed": 0, "ticks": 4}
    payload.update(extra)
    return payload


def _sealed_batch_payload() -> dict:
    return {
        "description": "sealed wave test",
        "runs": [
            _run_dict("a", "runs/a"),
            _run_dict("b", "runs/b"),
            _run_dict("c", "runs/c"),
            _run_dict("d", "runs/d"),
        ],
        "concurrency": 2,
        "stagger_seconds": 0.5,
        "credit_guard": {
            "minimum_credits": 6,
            "abort_on_low_credits": True,
            "require_available": True,
        },
        "waves": [
            {"wave_id": "wave-1", "run_ids": ["b", "a"]},
            {"wave_id": "wave-2", "run_ids": ["d", "c"]},
        ],
    }


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


def test_batch_spec_parses_sealed_credit_guard_and_ordered_exact_partition_waves() -> None:
    spec = BatchSpec.from_dict(_sealed_batch_payload())
    assert isinstance(spec.credit_guard, CreditGuard)
    assert spec.credit_guard.to_dict() == {
        "minimum_credits": 6.0,
        "abort_on_low_credits": True,
        "require_available": True,
    }
    assert all(isinstance(wave, WaveSpec) for wave in spec.waves)
    assert [wave.wave_id for wave in spec.waves] == ["wave-1", "wave-2"]


def test_batch_spec_rejects_waves_without_fail_closed_credit_guard() -> None:
    payload = _sealed_batch_payload()
    payload.pop("credit_guard")
    with pytest.raises(BatchSpecError, match="waves.*requires.*credit_guard"):
        BatchSpec.from_dict(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("concurrency", True, "concurrency"),
        ("concurrency", 0, "concurrency"),
        ("stagger_seconds", -1, "stagger_seconds"),
        ("stagger_seconds", float("nan"), "stagger_seconds"),
    ],
)
def test_batch_spec_rejects_invalid_execution_controls(field: str, value: object, message: str) -> None:
    payload = _sealed_batch_payload()
    payload[field] = value
    with pytest.raises(BatchSpecError, match=message):
        BatchSpec.from_dict(payload)


@pytest.mark.parametrize(
    ("guard", "message"),
    [
        ({"minimum_credits": 0, "abort_on_low_credits": True, "require_available": True}, "positive finite"),
        ({"minimum_credits": True, "abort_on_low_credits": True, "require_available": True}, "positive finite"),
        ({"minimum_credits": 6, "abort_on_low_credits": False, "require_available": True}, "abort_on_low_credits"),
        ({"minimum_credits": 6, "abort_on_low_credits": True, "require_available": False}, "require_available"),
        (
            {"minimum_credits": 6, "abort_on_low_credits": True, "require_available": True, "extra": 1},
            "exactly",
        ),
    ],
)
def test_batch_spec_rejects_nonsealed_credit_guard_contract(guard: dict, message: str) -> None:
    payload = _sealed_batch_payload()
    payload["credit_guard"] = guard
    with pytest.raises(BatchSpecError, match=message):
        BatchSpec.from_dict(payload)


@pytest.mark.parametrize(
    ("waves", "message"),
    [
        ([{"wave_id": "wave-1", "run_ids": ["a", "b"]}], "missing"),
        (
            [
                {"wave_id": "wave-1", "run_ids": ["a", "b"]},
                {"wave_id": "wave-2", "run_ids": ["b", "c", "d"]},
            ],
            "more than one wave",
        ),
        (
            [
                {"wave_id": "wave-1", "run_ids": ["a", "b"]},
                {"wave_id": "wave-2", "run_ids": ["c", "unknown"]},
            ],
            "unknown run_id",
        ),
        (
            [
                {"wave_id": "same", "run_ids": ["a", "b"]},
                {"wave_id": "same", "run_ids": ["c", "d"]},
            ],
            "duplicate wave_id",
        ),
        (
            [
                {"wave_id": "wave-1", "run_ids": ["a", "a", "b"]},
                {"wave_id": "wave-2", "run_ids": ["c", "d"]},
            ],
            "within wave",
        ),
    ],
)
def test_batch_spec_rejects_nonpartition_wave_contract(waves: list[dict], message: str) -> None:
    payload = _sealed_batch_payload()
    payload["waves"] = waves
    with pytest.raises(BatchSpecError, match=message):
        BatchSpec.from_dict(payload)


def test_select_wave_is_deterministic_and_retains_full_spec_provenance() -> None:
    full_spec = BatchSpec.from_dict(_sealed_batch_payload())
    selected = select_wave(full_spec, "wave-1")
    assert [run.run_id for run in selected.runs] == ["b", "a"]
    assert selected.selected_wave_id == "wave-1"
    assert selected.credit_guard == full_spec.credit_guard
    assert selected.source_sha256 == full_spec.source_sha256
    assert selected.waves == []
    # Selection never mutates or drops the full spec's ordered declarations.
    assert [wave.wave_id for wave in full_spec.waves] == ["wave-1", "wave-2"]
    with pytest.raises(BatchSpecError, match="unknown wave_id"):
        select_wave(full_spec, "wave-x")


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


@pytest.mark.parametrize(
    ("total", "usage"),
    [
        (float("nan"), 1.0),
        (float("inf"), 1.0),
        (10.0, float("nan")),
        (-1.0, 0.0),
        (10.0, -1.0),
        (10.0, 11.0),
    ],
)
def test_check_openrouter_credits_rejects_nonfinite_or_negative_values(total: float, usage: float) -> None:
    report = check_openrouter_credits(
        api_key="k",
        http_get=lambda u, h, t: (200, _credits_body(total, usage)),
    )
    assert report["status"] == "unavailable"
    assert report["remaining_credits"] is None
    assert "non-finite or negative" in report["detail"]


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
    assert len(manifest["batch_spec_sha256"]) == 64
    assert manifest["wave_id"] is None
    assert manifest["credit_guard"] is None

    persisted = json.loads((tmp_path / "batch_out" / BATCH_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert persisted["credits_preflight"]["remaining_credits"] == pytest.approx(17.0)


def test_run_batch_manifest_binds_full_spec_hash_wave_and_effective_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import company_twin.parallel_runner as parallel_runner_module

    monkeypatch.setattr(parallel_runner_module, "_git_commit", lambda root: "c" * 40)
    base_dir = tmp_path / "root"
    base_dir.mkdir()
    selected = select_wave(BatchSpec.from_dict(_sealed_batch_payload()), "wave-2")
    validate_batch_spec(selected, base_dir=base_dir)
    guard = selected.credit_guard.to_dict()
    manifest = run_batch(
        selected,
        base_dir=base_dir,
        batch_dir=base_dir / "runs" / "batch_wave_2",
        command_builder=lambda run: _sleep_stub(0.0),
        credits_preflight={
            "status": "ok",
            "remaining_credits": 10.0,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        },
        batch_spec_sha256="a" * 64,
        wave_id="wave-2",
        credit_guard=guard,
        plan_sha256="b" * 64,
        plan_id="sealed-test-plan",
        execution_git_commit="c" * 40,
    )
    assert manifest["batch_spec_sha256"] == "a" * 64
    assert manifest["wave_id"] == "wave-2"
    assert manifest["credit_guard"] == guard
    assert manifest["plan_sha256"] == "b" * 64
    assert manifest["plan_id"] == "sealed-test-plan"
    assert [run["run_id"] for run in manifest["runs"]] == ["d", "c"]


@pytest.mark.parametrize(
    "preflight",
    [
        None,
        {"status": "unavailable", "remaining_credits": None},
        {"status": "ok", "remaining_credits": float("nan")},
        {"status": "ok", "remaining_credits": 5.0},
    ],
)
def test_run_batch_enforces_managed_guard_before_creating_batch_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preflight: dict | None
) -> None:
    import company_twin.parallel_runner as parallel_runner_module

    monkeypatch.setattr(parallel_runner_module, "_git_commit", lambda root: "c" * 40)
    selected = select_wave(BatchSpec.from_dict(_sealed_batch_payload()), "wave-1")
    batch_dir = tmp_path / "must_not_exist"
    with pytest.raises(BatchSpecError, match="credits_preflight"):
        run_batch(
            selected,
            base_dir=tmp_path,
            batch_dir=batch_dir,
            command_builder=lambda run: _sleep_stub(0.0),
            credits_preflight=preflight,
            batch_spec_sha256="a" * 64,
            wave_id="wave-1",
            credit_guard=selected.credit_guard.to_dict(),
            plan_sha256="b" * 64,
            plan_id="sealed-test-plan",
            execution_git_commit="c" * 40,
        )
    assert not batch_dir.exists()


@pytest.mark.parametrize(
    "guard",
    [
        None,
        {"minimum_credits": 1.0, "abort_on_low_credits": True, "require_available": True},
        {"minimum_credits": 6.0, "abort_on_low_credits": False, "require_available": True},
    ],
)
def test_run_batch_rejects_omitted_or_weakened_sealed_spec_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    guard: dict | None,
) -> None:
    import company_twin.parallel_runner as parallel_runner_module

    monkeypatch.setattr(parallel_runner_module, "_git_commit", lambda root: "c" * 40)
    selected = select_wave(BatchSpec.from_dict(_sealed_batch_payload()), "wave-1")
    batch_dir = tmp_path / "runs" / "guard-bypass"
    with pytest.raises(BatchSpecError, match="exactly match spec.credit_guard"):
        run_batch(
            selected,
            base_dir=tmp_path,
            batch_dir=batch_dir,
            command_builder=lambda run: _sleep_stub(0.0),
            credits_preflight={
                "status": "ok",
                "remaining_credits": 10.0,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
            batch_spec_sha256="a" * 64,
            wave_id="wave-1",
            credit_guard=guard,
            plan_sha256="b" * 64,
            plan_id="sealed-test-plan",
            execution_git_commit="c" * 40,
        )
    assert not batch_dir.exists()


@pytest.mark.parametrize(
    ("checked_at", "message"),
    [
        ((datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(), "older than 5 minutes"),
        ((datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), "in the future"),
        ("2026-07-10T00:00:00", "include a timezone"),
    ],
)
def test_run_batch_rejects_stale_or_future_managed_credit_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checked_at: str,
    message: str,
) -> None:
    import company_twin.parallel_runner as parallel_runner_module

    monkeypatch.setattr(parallel_runner_module, "_git_commit", lambda root: "c" * 40)
    selected = select_wave(BatchSpec.from_dict(_sealed_batch_payload()), "wave-1")
    batch_dir = tmp_path / "runs" / "stale-credit"
    with pytest.raises(BatchSpecError, match=message):
        run_batch(
            selected,
            base_dir=tmp_path,
            batch_dir=batch_dir,
            command_builder=lambda run: _sleep_stub(0.0),
            credits_preflight={"status": "ok", "remaining_credits": 10.0, "checked_at": checked_at},
            batch_spec_sha256="a" * 64,
            wave_id="wave-1",
            credit_guard=selected.credit_guard.to_dict(),
            plan_sha256="b" * 64,
            plan_id="sealed-test-plan",
            execution_git_commit="c" * 40,
        )
    assert not batch_dir.exists()


@pytest.mark.parametrize("mode", ["outside", "existing"])
def test_run_batch_final_boundary_rejects_invalid_managed_batch_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    import company_twin.parallel_runner as parallel_runner_module

    monkeypatch.setattr(parallel_runner_module, "_git_commit", lambda root: "c" * 40)
    selected = select_wave(BatchSpec.from_dict(_sealed_batch_payload()), "wave-1")
    if mode == "outside":
        batch_dir = tmp_path.parent / f"outside-{tmp_path.name}"
        message = "stay within base_dir"
    else:
        batch_dir = tmp_path / "runs" / "existing"
        batch_dir.mkdir(parents=True)
        message = "refuses to overwrite"
    with pytest.raises(BatchSpecError, match=message):
        run_batch(
            selected,
            base_dir=tmp_path,
            batch_dir=batch_dir,
            command_builder=lambda run: _sleep_stub(0.0),
            credits_preflight={
                "status": "ok",
                "remaining_credits": 10.0,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
            batch_spec_sha256="a" * 64,
            wave_id="wave-1",
            credit_guard=selected.credit_guard.to_dict(),
            plan_sha256="b" * 64,
            plan_id="sealed-test-plan",
            execution_git_commit="c" * 40,
        )


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


def _sealed_cli_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: dict | None = None,
    preflight: dict | None = None,
) -> tuple[CliRunner, Path, Path, dict, str]:
    import company_twin.cli as cli_module

    payload = payload or _sealed_batch_payload()
    preflight = preflight or {
        "status": "ok",
        "remaining_credits": 10.0,
        "total_credits": 20.0,
        "total_usage": 10.0,
        "detail": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    monkeypatch.setattr(cli_module, "check_openrouter_credits", lambda **kwargs: preflight)
    launched: dict = {}

    def fake_run_batch(spec, *, base_dir, batch_dir, **kwargs):
        launched["call_count"] = launched.get("call_count", 0) + 1
        launched["spec"] = spec
        launched["base_dir"] = base_dir
        launched["batch_dir"] = batch_dir
        launched["kwargs"] = kwargs
        failures = set(launched.pop("fail_next", []))
        batch_dir.mkdir(parents=True, exist_ok=True)
        (batch_dir / "logs").mkdir(exist_ok=True)
        rows = []
        launched["roots_existed_before_launch"] = {
            run.run_id: (base_dir / run.run_root).exists() for run in spec.runs
        }
        for run in spec.runs:
            (base_dir / run.run_root).mkdir(parents=True, exist_ok=True)
            failed = run.run_id in failures
            rows.append(
                {
                    "run_id": run.run_id,
                    "run_root": run.run_root,
                    "stage": run.stage,
                    "cmd": [sys.executable, "-m", "company_twin.cli", *run.build_cli_args()],
                    "log_path": str((batch_dir / "logs" / f"{run.run_id}.log").resolve()),
                    "started_at": "2026-07-10T00:00:01+00:00",
                    "ended_at": "2026-07-10T00:00:02+00:00",
                    "exit_code": 1 if failed else 0,
                    "status": "failed" if failed else "succeeded",
                }
            )
        result = {
            "schema_version": "company_twin.batch_manifest.v1",
            "batch_dir": str(batch_dir.resolve()),
            "root": str(base_dir.resolve()),
            "git_commit": kwargs["execution_git_commit"],
            "started_at": "2026-07-10T00:00:00+00:00",
            "ended_at": "2026-07-10T00:00:03+00:00",
            "passed": not failures,
            "runs": rows,
            "failed_run_ids": [run.run_id for run in spec.runs if run.run_id in failures],
            "concurrency": spec.concurrency,
            "stagger_seconds": spec.stagger_seconds,
            "batch_spec_sha256": kwargs["batch_spec_sha256"],
            "wave_id": kwargs["wave_id"],
            "credit_guard": kwargs["credit_guard"],
            "credits_preflight": kwargs["credits_preflight"],
            "plan_sha256": kwargs["plan_sha256"],
            "plan_id": kwargs["plan_id"],
        }
        (batch_dir / BATCH_MANIFEST_FILENAME).write_text(json.dumps(result), encoding="utf-8")
        return result

    monkeypatch.setattr(cli_module, "run_batch", fake_run_batch)
    batch_spec_path = tmp_path / "sealed_batch.json"
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    batch_spec_path.write_bytes(raw)
    batch_hash = hashlib.sha256(raw).hexdigest()
    plan_path = tmp_path / "sealed_plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": "company_twin.loss_event_campaign_plan.v1",
                "plan_id": "sealed-test-plan",
                "campaign_role": "confirmatory",
                "kind": "pre_execution_sealed_plan",
                "execution_authorized_by_this_file": True,
                "batch_spec": str(batch_spec_path),
                "batch_spec_sha256": batch_hash,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text("runs/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", ".gitignore", batch_spec_path.name, plan_path.name], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-q",
            "-m",
            "seal",
        ],
        cwd=tmp_path,
        check=True,
    )
    return CliRunner(), batch_spec_path, plan_path, launched, batch_hash


def test_cli_run_batch_selects_one_wave_and_records_full_exact_file_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, batch_spec_path, plan_path, launched, expected_hash = _sealed_cli_fixture(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["run-batch", "--batch-spec", str(batch_spec_path), "--plan", str(plan_path), "--root", str(tmp_path), "--wave", "wave-1"],
    )
    assert result.exit_code == 0, result.output
    assert [run.run_id for run in launched["spec"].runs] == ["b", "a"]
    assert launched["kwargs"]["batch_spec_sha256"] == expected_hash
    assert launched["kwargs"]["wave_id"] == "wave-1"
    assert launched["kwargs"]["credit_guard"] == {
        "minimum_credits": 6.0,
        "abort_on_low_credits": True,
        "require_available": True,
    }
    assert launched["kwargs"]["plan_id"] == "sealed-test-plan"
    assert launched["kwargs"]["plan_sha256"] == hashlib.sha256(plan_path.read_bytes()).hexdigest()


def test_cli_sealed_batch_requires_exact_plan_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, batch_spec_path, plan_path, launched, _ = _sealed_cli_fixture(tmp_path, monkeypatch)
    missing = runner.invoke(
        app,
        ["run-batch", "--batch-spec", str(batch_spec_path), "--root", str(tmp_path), "--wave", "wave-1"],
    )
    assert missing.exit_code != 0
    assert "requires --plan" in missing.output
    assert not launched

    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_payload["batch_spec_sha256"] = "0" * 64
    plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")
    mismatch = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--wave",
            "wave-1",
        ],
    )
    assert mismatch.exit_code != 0
    assert "batch_spec_sha256 does not match" in mismatch.output
    assert not launched


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            {
                "kind": "pre_registered_confirmatory_template_pending_pilot",
                "execution_authorized_by_this_file": False,
            },
            "kind must be 'pre_execution_sealed_plan'",
        ),
        ({"execution_authorized_by_this_file": False}, "execution_authorized_by_this_file must be true"),
        (
            {
                "campaign_role": "feasibility_pilot",
                "kind": "pre_execution_pilot_plan",
                "execution_authorized_by_this_file": True,
                "approval_granted_by_this_file": False,
            },
            "approval_granted_by_this_file must be true",
        ),
    ],
)
def test_cli_managed_plan_rejects_pending_or_unauthorized_execution_before_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    updates: dict,
    message: str,
) -> None:
    runner, batch_spec_path, plan_path, launched, _ = _sealed_cli_fixture(tmp_path, monkeypatch)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload.update(updates)
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--wave",
            "wave-1",
        ],
    )
    assert result.exit_code != 0
    assert message in result.output
    assert launched.get("call_count", 0) == 0
    assert not (tmp_path / "runs" / ".wave_state").exists()


def test_cli_managed_execution_rejects_dirty_worktree_before_state_or_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, batch_spec_path, plan_path, launched, _ = _sealed_cli_fixture(tmp_path, monkeypatch)
    (tmp_path / "untracked.txt").write_text("dirty", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--wave",
            "wave-1",
        ],
    )
    assert result.exit_code != 0
    assert "clean Git worktree" in result.output
    assert launched.get("call_count", 0) == 0
    assert not (tmp_path / "runs" / ".wave_state").exists()


@pytest.mark.parametrize("mode", ["outside", "existing"])
def test_cli_managed_batch_dir_must_be_new_and_within_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    runner, batch_spec_path, plan_path, launched, _ = _sealed_cli_fixture(tmp_path, monkeypatch)
    if mode == "outside":
        target = tmp_path.parent / f"outside-{tmp_path.name}"
        message = "stay within --root"
    else:
        target = tmp_path / "runs" / "existing-batch"
        target.mkdir(parents=True)
        message = "must not already exist"
    result = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--wave",
            "wave-1",
            "--batch-dir",
            str(target),
        ],
    )
    assert result.exit_code != 0
    assert message in result.output
    assert launched.get("call_count", 0) == 0
    assert not (tmp_path / "runs" / ".wave_state" / "state.json").exists()


def test_cli_validates_full_wave_spec_before_selecting_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _sealed_batch_payload()
    payload["runs"][3]["run_root"] = payload["runs"][0]["run_root"]
    runner, batch_spec_path, plan_path, launched, _ = _sealed_cli_fixture(
        tmp_path, monkeypatch, payload=payload
    )
    result = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--wave",
            "wave-1",
        ],
    )
    assert result.exit_code != 0
    assert "duplicate run_root" in result.output
    assert not launched


def test_cli_run_batch_requires_wave_when_sealed_spec_declares_waves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, batch_spec_path, plan_path, launched, _ = _sealed_cli_fixture(tmp_path, monkeypatch)
    result = runner.invoke(app, ["run-batch", "--batch-spec", str(batch_spec_path), "--plan", str(plan_path), "--root", str(tmp_path)])
    assert result.exit_code != 0
    assert "--wave is required" in result.output
    assert not launched


def test_cli_wave_state_rejects_out_of_order_and_duplicate_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, batch_spec_path, plan_path, launched, _ = _sealed_cli_fixture(tmp_path, monkeypatch)
    common = [
        "run-batch",
        "--batch-spec",
        str(batch_spec_path),
        "--plan",
        str(plan_path),
        "--root",
        str(tmp_path),
    ]
    out_of_order = runner.invoke(
        app,
        [*common, "--wave", "wave-2", "--batch-dir", str(tmp_path / "runs" / "wave2-too-early")],
    )
    assert out_of_order.exit_code != 0
    assert "out-of-order" in out_of_order.output
    assert launched.get("call_count", 0) == 0

    wave1 = runner.invoke(
        app,
        [*common, "--wave", "wave-1", "--batch-dir", str(tmp_path / "runs" / "wave1")],
    )
    assert wave1.exit_code == 0, wave1.output
    assert launched["call_count"] == 1

    duplicate = runner.invoke(
        app,
        [*common, "--wave", "wave-1", "--batch-dir", str(tmp_path / "runs" / "wave1-again")],
    )
    assert duplicate.exit_code != 0
    assert "out-of-order" in duplicate.output
    assert launched["call_count"] == 1

    wave2 = runner.invoke(
        app,
        [*common, "--wave", "wave-2", "--batch-dir", str(tmp_path / "runs" / "wave2")],
    )
    assert wave2.exit_code == 0, wave2.output
    assert launched["call_count"] == 2
    assert [run.run_id for run in launched["spec"].runs] == ["d", "c"]


def test_cli_wave_lock_rejects_concurrent_launch_before_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, batch_spec_path, plan_path, launched, batch_hash = _sealed_cli_fixture(tmp_path, monkeypatch)
    lock_dir = tmp_path / "runs" / ".wave_state"
    lock_dir.mkdir(parents=True)
    (lock_dir / f"{batch_hash}.lock").write_text("active", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--wave",
            "wave-1",
        ],
    )
    assert result.exit_code != 0
    assert "lock already exists" in result.output
    assert launched.get("call_count", 0) == 0


def _commit_code_only_change(root: Path) -> str:
    marker = root / "code_marker.py"
    marker.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", marker.name], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-q",
            "-m",
            "code drift",
        ],
        cwd=root,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_cli_wave_state_rejects_code_commit_drift_between_waves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, batch_spec_path, plan_path, launched, _ = _sealed_cli_fixture(tmp_path, monkeypatch)
    common = [
        "run-batch",
        "--batch-spec",
        str(batch_spec_path),
        "--plan",
        str(plan_path),
        "--root",
        str(tmp_path),
    ]
    first = runner.invoke(
        app,
        [*common, "--wave", "wave-1", "--batch-dir", str(tmp_path / "runs" / "wave1")],
    )
    assert first.exit_code == 0, first.output
    old_commit = launched["kwargs"]["execution_git_commit"]
    new_commit = _commit_code_only_change(tmp_path)
    assert new_commit != old_commit
    second = runner.invoke(
        app,
        [*common, "--wave", "wave-2", "--batch-dir", str(tmp_path / "runs" / "wave2")],
    )
    assert second.exit_code != 0
    assert "state disagrees with the sealed batch" in second.output
    assert launched["call_count"] == 1


def test_cli_retry_rejects_code_commit_drift_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, batch_spec_path, plan_path, launched, _ = _sealed_cli_fixture(tmp_path, monkeypatch)
    prior_dir = tmp_path / "runs" / "batches" / "attempt-1"
    launched["fail_next"] = ["b"]
    initial = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--wave",
            "wave-1",
            "--batch-dir",
            str(prior_dir),
        ],
    )
    assert initial.exit_code == 1
    _commit_code_only_change(tmp_path)
    result = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--retry-failed",
            str(prior_dir / BATCH_MANIFEST_FILENAME),
            "--batch-dir",
            str(tmp_path / "runs" / "batches" / "attempt-2"),
            "--delete-partial-roots",
        ],
    )
    assert result.exit_code != 0
    assert "git_commit does not match" in result.output
    assert (tmp_path / "runs" / "b").exists()
    assert launched["call_count"] == 1


@pytest.mark.parametrize(
    ("preflight", "message"),
    [
        (
            {
                "status": "unavailable",
                "remaining_credits": None,
                "total_credits": None,
                "total_usage": None,
                "detail": "endpoint down",
                "checked_at": "2026-07-10T00:00:00+00:00",
            },
            "requires an available balance check",
        ),
        (
            {
                "status": "ok",
                "remaining_credits": 5.99,
                "total_credits": 20.0,
                "total_usage": 14.01,
                "detail": None,
                "checked_at": "2026-07-10T00:00:00+00:00",
            },
            "below --min-credits",
        ),
    ],
)
def test_cli_sealed_credit_guard_fails_closed_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preflight: dict,
    message: str,
) -> None:
    runner, batch_spec_path, plan_path, launched, _ = _sealed_cli_fixture(tmp_path, monkeypatch, preflight=preflight)
    result = runner.invoke(
        app,
        ["run-batch", "--batch-spec", str(batch_spec_path), "--plan", str(plan_path), "--root", str(tmp_path), "--wave", "wave-1"],
    )
    assert result.exit_code != 0
    assert message in result.output
    assert not launched


@pytest.mark.parametrize(
    "override",
    [
        ["--min-credits", "7"],
        ["--warn-on-low-credits"],
        ["--concurrency", "3"],
        ["--stagger-seconds", "1"],
    ],
)
def test_cli_sealed_spec_rejects_conflicting_execution_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: list[str]
) -> None:
    runner, batch_spec_path, plan_path, launched, _ = _sealed_cli_fixture(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--wave",
            "wave-1",
            *override,
        ],
    )
    assert result.exit_code != 0
    assert "conflicts" in result.output
    assert not launched


def test_cli_sealed_pilot_without_waves_records_null_wave_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _sealed_batch_payload()
    payload.pop("waves")
    runner, batch_spec_path, plan_path, launched, _ = _sealed_cli_fixture(tmp_path, monkeypatch, payload=payload)
    result = runner.invoke(app, ["run-batch", "--batch-spec", str(batch_spec_path), "--plan", str(plan_path), "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert [run.run_id for run in launched["spec"].runs] == ["a", "b", "c", "d"]
    assert launched["kwargs"]["wave_id"] is None
    assert launched["kwargs"]["credit_guard"]["require_available"] is True


def test_cli_sealed_retry_inherits_wave_hash_and_guard_and_requires_distinct_batch_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, batch_spec_path, plan_path, launched, expected_hash = _sealed_cli_fixture(tmp_path, monkeypatch)
    prior_batch_dir = tmp_path / "runs" / "batches" / "attempt-1"
    launched["fail_next"] = ["b"]
    initial = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--wave",
            "wave-1",
            "--batch-dir",
            str(prior_batch_dir),
        ],
    )
    assert initial.exit_code == 1
    failed_root = tmp_path / "runs" / "b"
    assert failed_root.exists()
    prior_manifest_path = prior_batch_dir / BATCH_MANIFEST_FILENAME

    missing_dir = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--retry-failed",
            str(prior_manifest_path),
        ],
    )
    assert missing_dir.exit_code != 0
    assert "distinct --batch-dir" in missing_dir.output
    assert failed_root.exists()
    assert [run.run_id for run in launched["spec"].runs] == ["b", "a"]

    result = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--retry-failed",
            str(prior_manifest_path),
            "--batch-dir",
            str(tmp_path / "runs" / "batches" / "attempt-2"),
            "--delete-partial-roots",
        ],
    )
    assert result.exit_code == 0, result.output
    assert [run.run_id for run in launched["spec"].runs] == ["b"]
    assert launched["kwargs"]["wave_id"] == "wave-1"
    assert launched["kwargs"]["batch_spec_sha256"] == expected_hash
    assert launched["kwargs"]["credit_guard"]["minimum_credits"] == 6.0
    assert launched["roots_existed_before_launch"] == {"b": False}
    assert failed_root.exists()


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("cmd", "command drift"),
        ("failed-ids", "failed_run_ids disagree"),
        ("root", "root must equal"),
        ("batch-dir", "containing directory"),
        ("timestamp", "timestamps are inconsistent"),
        ("status", "status and exit_code disagree"),
    ],
)
def test_cli_validates_complete_retry_manifest_before_deleting_partial_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    message: str,
) -> None:
    runner, batch_spec_path, plan_path, launched, _ = _sealed_cli_fixture(tmp_path, monkeypatch)
    prior_dir = tmp_path / "runs" / "batches" / "attempt-1"
    launched["fail_next"] = ["b"]
    initial = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--wave",
            "wave-1",
            "--batch-dir",
            str(prior_dir),
        ],
    )
    assert initial.exit_code == 1
    manifest_path = prior_dir / BATCH_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failed_row = next(row for row in manifest["runs"] if row["run_id"] == "b")
    if drift == "cmd":
        failed_row["cmd"].append("--drift")
    elif drift == "failed-ids":
        manifest["failed_run_ids"] = []
    elif drift == "root":
        manifest["root"] = str((tmp_path / "other-root").resolve())
    elif drift == "batch-dir":
        manifest["batch_dir"] = str((tmp_path / "other-batch").resolve())
    elif drift == "timestamp":
        failed_row["started_at"] = "2026-07-09T23:59:59+00:00"
    else:
        failed_row["exit_code"] = 0
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    failed_root = tmp_path / "runs" / "b"
    result = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--retry-failed",
            str(manifest_path),
            "--batch-dir",
            str(tmp_path / "runs" / "batches" / "attempt-2"),
            "--delete-partial-roots",
        ],
    )
    assert result.exit_code != 0
    assert message in result.output
    assert failed_root.exists()
    assert launched["call_count"] == 1


def test_cli_wave_state_binds_exact_failed_ids_before_retry_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, batch_spec_path, plan_path, launched, _ = _sealed_cli_fixture(tmp_path, monkeypatch)
    prior_dir = tmp_path / "runs" / "batches" / "attempt-1"
    launched["fail_next"] = ["b"]
    initial = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--wave",
            "wave-1",
            "--batch-dir",
            str(prior_dir),
        ],
    )
    assert initial.exit_code == 1
    manifest_path = prior_dir / BATCH_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {row["run_id"]: row for row in manifest["runs"]}
    by_id["b"]["status"] = "succeeded"
    by_id["b"]["exit_code"] = 0
    by_id["a"]["status"] = "failed"
    by_id["a"]["exit_code"] = 1
    manifest["failed_run_ids"] = ["a"]
    manifest["passed"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--retry-failed",
            str(manifest_path),
            "--batch-dir",
            str(tmp_path / "runs" / "batches" / "attempt-2"),
            "--delete-partial-roots",
        ],
    )
    assert result.exit_code != 0
    assert "failed_run_ids differ" in result.output
    assert (tmp_path / "runs" / "a").exists()
    assert (tmp_path / "runs" / "b").exists()
    assert launched["call_count"] == 1


def test_cli_sealed_retry_rejects_missing_or_mismatched_provenance_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, batch_spec_path, plan_path, launched, _ = _sealed_cli_fixture(tmp_path, monkeypatch)
    failed_root = tmp_path / "runs" / "b"
    failed_root.mkdir(parents=True)
    prior_manifest_path = tmp_path / "runs" / "bad_manifest.json"
    prior_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    prior_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "company_twin.batch_manifest.v1",
                "batch_dir": str(tmp_path / "attempt-1"),
                "batch_spec_sha256": "0" * 64,
                "wave_id": "wave-1",
                "credit_guard": {
                    "minimum_credits": 6.0,
                    "abort_on_low_credits": True,
                    "require_available": True,
                },
                "failed_run_ids": ["b"],
                "runs": [{"run_id": "b"}],
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "run-batch",
            "--batch-spec",
            str(batch_spec_path),
            "--plan",
            str(plan_path),
            "--root",
            str(tmp_path),
            "--retry-failed",
            str(prior_manifest_path),
            "--batch-dir",
            str(tmp_path / "runs" / "batches" / "attempt-2"),
            "--delete-partial-roots",
        ],
    )
    assert result.exit_code != 0
    assert "SHA-256 does not match" in result.output
    assert failed_root.exists()
    assert not launched
