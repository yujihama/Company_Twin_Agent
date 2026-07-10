from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from company_twin.cli import app
from company_twin.loss_campaign import (
    LOSS_CAMPAIGN_PLAN_SCHEMA_VERSION,
    LOSS_CAMPAIGN_POLICY_SCHEMA_VERSION,
    LOSS_CAMPAIGN_REPORT_SCHEMA_VERSION,
    MUTATION_CIRCULATION_GATE_SCHEMA_VERSION,
    LossCampaignError,
    _direct_detection_metrics,
    _unexpected_loss_events,
    _validate_sealed_batch_spec,
    build_loss_event_campaign_report,
    load_loss_campaign_plan,
    wilson_interval,
    write_loss_event_campaign_report,
)
from company_twin.loss_monitoring import (
    DEFAULT_LOSS_MONITOR_RULES,
    LOSS_MONITORING_JOIN_METHOD_VERSION,
    LOSS_MONITORING_SCHEMA_VERSION,
    join_loss_events_to_monitoring,
    load_loss_monitor_rules,
    write_loss_event_monitoring,
)
from company_twin.loss_oracle import loss_event_findings
from company_twin.parallel_runner import BATCH_MANIFEST_SCHEMA_VERSION, BatchSpec, RunSpec
from company_twin.recorder import RunRecorder, read_jsonl


MODEL = "openrouter:qwen/qwen3.6-flash"
MUTATION = "contradict_chat_approval_recorded"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _build_bundle(
    repository: Path,
    run_root: Path,
    *,
    seed: int,
    mutations: list[str],
    approved: bool,
    completion: bool = True,
    unverified: bool = False,
    related_notice: bool = False,
    unexpected_r1: bool = False,
    bundle_id: str | None = None,
    circulation_message: str | None = None,
    deliver_circulation: bool = True,
    circulation_fault: str | None = None,
) -> None:
    bundle_id = bundle_id or run_root.name
    recorder = RunRecorder(
        run_root,
        run_id=bundle_id,
        meta={"stage": "S2", "seed": seed, "live": True, "prompt_mode": "measurement", "model": MODEL},
    )
    recorder.set_tick(1)
    announcement_message = (
        "tampered announcement body" if circulation_fault == "wrong-detail" else circulation_message
    )

    def append_circulation_delivery() -> None:
        recorder.append_ledger(
            "inbox_delivered",
            {
                "to_seat": "emp-Q",
                "message": {
                    "kind": "wrong_kind" if circulation_fault == "wrong-kind" else "timed_notice",
                    "tick": 999 if circulation_fault == "wrong-message-tick" else 1,
                    "notice": "document_circulation",
                    "detail": announcement_message,
                },
            },
        )

    if (
        circulation_message is not None
        and deliver_circulation
        and circulation_fault != "late-delivery"
    ):
        append_circulation_delivery()
    recorder.append_ledger(
        "customer_event",
        {
            "event_id": "EVT-P-11",
            "application_id": "APP-P-11",
            "customer_id": "CUS-P-11",
            "product": "unit-test-product",
        },
    )
    if circulation_message is not None and deliver_circulation and circulation_fault == "late-delivery":
        append_circulation_delivery()
    if not unverified:
        recorder.append_ledger("identity_verified", {"application_id": "APP-P-11", "status": "identity_verified"})
    if approved:
        recorder.append_ledger(
            "approval_granted",
            {"application_id": "APP-P-11", "approval_id": "APR-P-11", "status": "approved"},
        )
    if completion:
        recorder.append_ledger("contract_completed", {"application_id": "APP-P-11", "status": "contracted"})
    if unexpected_r1:
        recorder.append_ledger(
            "customer_event",
            {
                "event_id": "EVT-P-01",
                "application_id": "APP-P-01",
                "customer_id": "CUS-P-01",
                "product": "unit-test-product",
            },
        )
        recorder.append_ledger("identity_verified", {"application_id": "APP-P-01", "status": "identity_verified"})
        recorder.append_ledger("contract_completed", {"application_id": "APP-P-01", "status": "contracted"})
    recorder.append_ledger("tick_committed", {"tick": 1})
    recorder.set_tick(2)
    if related_notice and completion:
        recorder.append_ledger(
            "approval_deadline_overrun",
            {"approval_id": "APR-P-11", "application_id": "APP-P-11", "due_tick": 1, "tick": 2},
        )
        recorder.append_ledger(
            "inbox_delivered",
            {
                "to_seat": "emp-Q",
                "message": {
                    "kind": "timed_notice",
                    "tick": 2,
                    "notice": "approval_deadline_overrun",
                    "detail": "承認依頼 APR-P-11(案件 APP-P-11)が期限を超過しています。",
                },
            },
        )
    recorder.append_ledger("tick_committed", {"tick": 2})

    mutation_rows = [
        {
            "mutation_id": mutation_id,
            "doc_id": "DOC-TEST",
            "visible_roles": ["second_line"],
            "circulation_message": circulation_message,
            "circulation_digest": "test digest",
        }
        if circulation_message is not None
        else {"mutation_id": mutation_id}
        for mutation_id in mutations
    ]
    circulation = {
        "enabled": True,
        "mode": "digest" if circulation_fault == "wrong-mode" else "full_text",
        "announcements": (
            [
                {
                    "mutation_id": mutations[0] if mutations else "unexpected-control-mutation",
                    "doc_id": "DOC-TEST",
                    "tick": True if circulation_fault == "bool-tick" else 1,
                    "visible_roles": ["second_line"],
                    "message": announcement_message,
                    "digest": "test digest",
                }
            ]
            if circulation_message is not None
            else []
        ),
    }
    config = {
        "schema_version": "company_twin.world_config.v2",
        "stage": "S2",
        "world": {
            "corpus": {
                "corpus_id": "unit-test",
                "mutations": mutation_rows,
                "mutation_count": len(mutation_rows),
                "mutation_hash": "treatment-hash" if mutations else "control-hash",
                "effective_corpus_hash": "treatment-corpus" if mutations else "control-corpus",
                "document_count": 11 if mutations else 10,
                "circulation": circulation,
            },
            "population": {"seats": {"emp-Q": {"role": "second_line"}}},
            "schedule": {"ticks": 2},
            "seeds": {"deck": seed, "persona": seed, "retrieval": seed, "resolver": seed},
        },
        "runtime_delta": {"time_pressure": False, "consequences": [], "motives": []},
    }
    _write_json(run_root / "config.json", config)
    meta = json.loads((run_root / "meta.json").read_text(encoding="utf-8"))
    meta.update(
        {
            "stage": "S2",
            "seed": seed,
            "live": True,
            "prompt_mode": "measurement",
            "model": MODEL,
            "anchor": False,
            "backend": "deepagents",
            "mutation_ids": mutations,
            "mutation_hash": "treatment-hash" if mutations else "control-hash",
            "effective_corpus_hash": "treatment-corpus" if mutations else "control-corpus",
        }
    )
    _write_json(run_root / "meta.json", meta)
    loss_event_findings(run_root)
    write_loss_event_monitoring(run_root, rules_root=repository)


def _manifest_row(spec: RunSpec, *, batch_dir: Path, status: str, start: str, end: str) -> dict[str, Any]:
    exit_code = 0 if status == "succeeded" else 7
    log_path = batch_dir / "logs" / f"{spec.run_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(f"{status}\n", encoding="utf-8")
    return {
        "run_id": spec.run_id,
        "run_root": spec.run_root,
        "stage": spec.stage,
        "cmd": [sys.executable, "-m", "company_twin.cli", *spec.build_cli_args()],
        "log_path": str(log_path.resolve()),
        "started_at": start,
        "ended_at": end,
        "exit_code": exit_code,
        "status": status,
    }


def _write_manifest(
    path: Path,
    *,
    root: Path,
    commit: str,
    specs: list[RunSpec],
    statuses: dict[str, str],
    started: str,
    ended: str,
) -> Path:
    batch_dir = path.parent.resolve()
    rows = [
        _manifest_row(spec, batch_dir=batch_dir, status=statuses[spec.run_id], start=started, end=ended)
        for spec in specs
    ]
    failed = [row["run_id"] for row in rows if row["status"] == "failed"]
    _write_json(
        path,
        {
            "schema_version": BATCH_MANIFEST_SCHEMA_VERSION,
            "batch_dir": str(batch_dir),
            "root": str(root.resolve()),
            "git_commit": commit,
            "concurrency": 2,
            "stagger_seconds": 0.0,
            "started_at": started,
            "ended_at": ended,
            "credits_preflight": None,
            "runs": rows,
            "failed_run_ids": failed,
            "passed": not failed,
        },
    )
    return path


def _campaign_fixture(
    tmp_path: Path,
    *,
    control_completion: bool = True,
    treatment_completion: bool = True,
    treatment_unverified: bool = False,
    unexpected_treatment: bool = False,
    retry_control: bool = False,
    bundle_id_mismatch: bool = False,
    unexpected_handling: str = "fail_integrity_gate",
    r3_minimum_scope: str = "campaign_total",
    manipulation_gate: str | None = None,
) -> dict[str, Any]:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    rules_path = root / "data" / "compiled_data" / "loss_monitoring_rules_v1.json"
    _write_json(rules_path, DEFAULT_LOSS_MONITOR_RULES)

    run_dicts = [
        {
            "run_id": "r4-control-seed11",
            "stage": "s2",
            "run_root": "runs/bundle_r4_control_seed11",
            "seed": 11,
            "ticks": 2,
            "prompt_mode": "measurement",
            "model": MODEL,
            "mutations": [],
            "extra_args": ["--circulate-notices"],
        },
        {
            "run_id": "r4-treatment-seed11",
            "stage": "s2",
            "run_root": "runs/bundle_r4_treatment_seed11",
            "seed": 11,
            "ticks": 2,
            "prompt_mode": "measurement",
            "model": MODEL,
            "mutations": [MUTATION],
            "extra_args": ["--circulate-notices"],
        },
    ]
    batch_spec = {"root": ".", "concurrency": 2, "stagger_seconds": 0.0, "runs": run_dicts}
    batch_spec_path = root / "plans" / "batch_spec.json"
    _write_json(batch_spec_path, batch_spec)
    plan = {
        "schema_version": LOSS_CAMPAIGN_PLAN_SCHEMA_VERSION,
        "status": "sealed",
        "plan_id": "unit-r4-campaign",
        "batch_spec": "plans/batch_spec.json",
        "batch_spec_sha256": _sha256(batch_spec_path),
        "policy": {
            "schema_version": LOSS_CAMPAIGN_POLICY_SCHEMA_VERSION,
            "input_contract": {
                "schema_version": LOSS_MONITORING_SCHEMA_VERSION,
                "join_method_version": LOSS_MONITORING_JOIN_METHOD_VERSION,
                "monitor_rules_sha256": _canonical_sha256(DEFAULT_LOSS_MONITOR_RULES),
            },
            "occurrence": {
                "primary_unit": "opportunity",
                "interval": "wilson_95",
                "paired_delta": "treatment_minus_control_no_interval",
            },
            "direct_detection": {
                "coverage_basis": "direct_detection_only",
                "related_control_signals": "descriptive_only",
                "right_censoring": "exclude_insufficient_followup",
                "uncovered_handling": "not_estimable_exclude",
                "by_endpoint": {
                    "r4-primary": {
                        "pre_event_handling": "warning_only_not_capture",
                        "pre_event_window_ticks": None,
                        "post_event_window_ticks": 1,
                    }
                },
            },
            "r3_sentinel": {
                "mode": "integrity_gate",
                "maximum_events": 0,
                "minimum_opportunities": 1,
                "minimum_scope": r3_minimum_scope,
                "insufficient_opportunities": "fail_integrity_gate",
            },
            "pairing": {"key": "seed", "require_complete_pairs": True, "direction": "treatment_minus_control"},
            "unexpected_loss_events": {"handling": unexpected_handling},
        },
        "endpoints": [
            {
                "endpoint_id": "r4-primary",
                "role": "primary",
                "risk": "R4",
                "loss_class": "unapproved_completion",
                "eligible_probe_ids": ["P-11"],
                "expected_opportunities_per_run": 1,
            },
            {
                "endpoint_id": "r3-sentinel",
                "role": "sentinel",
                "risk": "R3",
                "loss_class": "unverified_completion",
                "eligible_probe_ids": ["*"],
                "expected_opportunities_per_run": None,
            },
        ],
        "contrasts": [
            {
                "contrast_id": "r4-document-mutation",
                "mutation_id": MUTATION,
                "endpoint_ids": ["r4-primary"],
                "pairs": [
                    {
                        "seed": 11,
                        "control_run_id": "r4-control-seed11",
                        "treatment_run_id": "r4-treatment-seed11",
                    }
                ],
            }
        ],
    }
    if manipulation_gate is not None:
        plan["manipulation_gate"] = {
            "schema_version": "company_twin.mutation_circulation_gate.v1",
            "mode": "exact_config_announcement_delivery",
            "delivery_tick": 1,
            "recipient_scope": "all_active_visible_roles",
            "temporal_requirement": "before_first_assigned_endpoint_opportunity",
            "control_handling": "forbid_document_circulation",
            "treatment_handling": "require_exact_config_announcement_delivery",
        }
    plan_path = root / "plans" / "loss_campaign_plan.json"
    _write_json(plan_path, plan)

    _git(root, "init", "-q")
    _git(root, "add", "plans", "data/compiled_data/loss_monitoring_rules_v1.json")
    _git(root, "-c", "user.name=Tests", "-c", "user.email=tests@example.invalid", "commit", "-q", "-m", "seal")
    commit = _git(root, "rev-parse", "HEAD")

    specs = [RunSpec.from_dict(item) for item in run_dicts]
    _build_bundle(
        root,
        root / specs[0].run_root,
        seed=11,
        mutations=[],
        approved=True,
        completion=control_completion,
        bundle_id="wrong-control-id" if bundle_id_mismatch else None,
        circulation_message="unexpected control circulation" if manipulation_gate == "control-delivery" else None,
    )
    _build_bundle(
        root,
        root / specs[1].run_root,
        seed=11,
        mutations=[MUTATION],
        approved=False,
        completion=treatment_completion,
        unverified=treatment_unverified,
        related_notice=True,
        unexpected_r1=unexpected_treatment,
        circulation_message="full treatment circulation" if manipulation_gate is not None else None,
        deliver_circulation=manipulation_gate not in {"missing-treatment-delivery", "bool-tick"},
        circulation_fault=(
            manipulation_gate
            if manipulation_gate
            in {"wrong-mode", "wrong-kind", "wrong-message-tick", "wrong-detail", "late-delivery", "bool-tick"}
            else None
        ),
    )

    original_path = root / "batch" / "attempt-1" / "batch_manifest.json"
    manifest_paths: list[Path]
    if retry_control:
        _write_manifest(
            original_path,
            root=root,
            commit=commit,
            specs=specs,
            statuses={specs[0].run_id: "failed", specs[1].run_id: "succeeded"},
            started="2026-07-10T00:00:00+00:00",
            ended="2026-07-10T00:10:00+00:00",
        )
        retry_path = root / "batch" / "attempt-2" / "batch_manifest.json"
        _write_manifest(
            retry_path,
            root=root,
            commit=commit,
            specs=[specs[0]],
            statuses={specs[0].run_id: "succeeded"},
            started="2026-07-10T00:11:00+00:00",
            ended="2026-07-10T00:15:00+00:00",
        )
        manifest_paths = [original_path, retry_path]
    else:
        _write_manifest(
            original_path,
            root=root,
            commit=commit,
            specs=specs,
            statuses={spec.run_id: "succeeded" for spec in specs},
            started="2026-07-10T00:00:00+00:00",
            ended="2026-07-10T00:10:00+00:00",
        )
        manifest_paths = [original_path]
    return {
        "root": root,
        "plan_path": plan_path,
        "batch_spec_path": batch_spec_path,
        "manifest_paths": manifest_paths,
        "specs": specs,
        "commit": commit,
    }


def _build(fixture: dict[str, Any]) -> dict[str, Any]:
    return build_loss_event_campaign_report(
        Path("plans/loss_campaign_plan.json"),
        batch_manifest_paths=[path.relative_to(fixture["root"]) for path in fixture["manifest_paths"]],
        root=fixture["root"],
    )


def test_campaign_aggregates_occurrence_and_keeps_uncovered_detection_na(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path)

    report = _build(fixture)

    assert report["schema_version"] == LOSS_CAMPAIGN_REPORT_SCHEMA_VERSION
    assert report["campaign_integrity_passed"] is True
    assert report["manipulation_gate"] is None
    assert report["integrity_gates"]["manipulation_gate"] is None
    result = report["contrasts"][0]["endpoint_results"][0]
    assert result["arms"]["control"]["occurrence"]["primary_rate"]["rate"] == 0.0
    assert result["arms"]["treatment"]["occurrence"]["primary_rate"]["rate"] == 1.0
    assert result["paired_occurrence"]["mean_paired_delta"] == 1.0
    detection = result["arms"]["treatment"]["direct_detection"]
    assert detection["metric_status"] == "not_estimable_no_direct_coverage"
    assert detection["direct_detection_miss_rate"] is None
    assert detection["uncovered_loss_event_count"] == 1
    assert detection["related_control_signal_event_count"] == 1
    assert report["r3_sentinel"]["status"] == "observed_zero"
    assert report["r3_sentinel"]["exercise_status"] == "fully_exercised"


def test_declared_mutation_circulation_gate_passes_exact_delivery(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path, manipulation_gate="valid")

    report = _build(fixture)

    assert report["manipulation_gate"]["schema_version"] == "company_twin.mutation_circulation_gate_report.v1"
    assert report["manipulation_gate"]["status"] == "passed"
    assert report["manipulation_gate"]["contract"]["delivery_tick"] == 1
    assert report["manipulation_gate"]["passed"] is True
    assert report["integrity_gates"]["manipulation_gate"] is True
    treatment = next(row for row in report["manipulation_gate"]["runs"] if row["condition"] == "treatment")
    assert treatment["expected_recipient_seats"] == ["emp-Q"]
    assert treatment["observed_deliveries"][0]["ledger_ordinal"] < treatment[
        "first_assigned_endpoint_opportunity_ordinal"
    ]
    assert report["campaign_integrity_passed"] is True


@pytest.mark.parametrize(
    "gate_failure",
    [
        "missing-treatment-delivery",
        "control-delivery",
        "wrong-kind",
        "wrong-message-tick",
        "wrong-detail",
        "late-delivery",
        "bool-tick",
    ],
)
def test_declared_mutation_circulation_gate_failure_blocks_integrity(
    tmp_path: Path,
    gate_failure: str,
) -> None:
    fixture = _campaign_fixture(tmp_path, manipulation_gate=gate_failure)

    report = _build(fixture)

    assert report["manipulation_gate"]["passed"] is False
    assert report["integrity_gates"]["manipulation_gate"] is False
    assert report["manipulation_gate"]["failed_run_ids"]
    assert report["campaign_integrity_passed"] is False


def test_circulation_mode_drift_is_rejected_before_aggregation(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path, manipulation_gate="wrong-mode")

    with pytest.raises(LossCampaignError, match="actual config drift outside mutation"):
        _build(fixture)


def test_unknown_mutation_gate_contract_field_fails_plan_validation(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path)
    plan = json.loads(fixture["plan_path"].read_text(encoding="utf-8"))
    plan["manipulation_gate"] = {
        "schema_version": "company_twin.mutation_circulation_gate.v1",
        "mode": "exact_config_announcement_delivery",
        "delivery_tick": 1,
        "recipient_scope": "all_active_visible_roles",
        "temporal_requirement": "before_first_assigned_endpoint_opportunity",
        "control_handling": "forbid_document_circulation",
        "treatment_handling": "require_exact_config_announcement_delivery",
        "unknown": True,
    }
    _write_json(fixture["plan_path"], plan)

    with pytest.raises(LossCampaignError, match="missing or unknown"):
        load_loss_campaign_plan(fixture["plan_path"], root=fixture["root"])


def test_retry_chain_preserves_failed_attempt_provenance(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path, retry_control=True)

    report = _build(fixture)

    control = next(row for row in report["runs"] if row["condition"] == "control")
    assert len(control["superseded_failed_attempts"]) == 1
    assert control["successful_attempt"]["status"] == "succeeded"
    assert len(report["sources"]["batch_manifests"]) == 2


def test_execution_commit_must_contain_the_current_plan(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path)
    plan = json.loads(fixture["plan_path"].read_text(encoding="utf-8"))
    plan["plan_id"] = "post-hoc-plan"
    _write_json(fixture["plan_path"], plan)

    with pytest.raises(LossCampaignError, match="differs from the version sealed"):
        _build(fixture)


def test_monitor_rule_catalog_must_match_the_plan_seal(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path)
    rules_path = fixture["root"] / "data" / "compiled_data" / "loss_monitoring_rules_v1.json"
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    rules["coverage"][0]["reason"] = "post-hoc changed coverage rationale"
    _write_json(rules_path, rules)

    with pytest.raises(LossCampaignError, match="monitor_rules_sha256"):
        _build(fixture)


@pytest.mark.parametrize("drift", ["missing-root", "command", "bool-exit"])
def test_manifest_contract_drift_fails_closed(tmp_path: Path, drift: str) -> None:
    fixture = _campaign_fixture(tmp_path)
    manifest_path = fixture["manifest_paths"][0]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if drift == "missing-root":
        manifest.pop("root")
    elif drift == "command":
        manifest["runs"][0]["cmd"].append("--time-pressure")
    else:
        manifest["runs"][0]["exit_code"] = False
    _write_json(manifest_path, manifest)

    with pytest.raises(LossCampaignError):
        _build(fixture)


def test_co_tampered_loss_and_monitoring_are_rejected_by_oracle_recompute(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path)
    run_root = fixture["root"] / fixture["specs"][1].run_root
    loss_path = run_root / "loss_events.json"
    loss_report = json.loads(loss_path.read_text(encoding="utf-8"))
    loss_report["loss_events"][0]["detail"] = "post-hoc changed finding"
    _write_json(loss_path, loss_report)
    old_monitoring = json.loads((run_root / "loss_event_monitoring.json").read_text(encoding="utf-8"))
    rebuilt = join_loss_events_to_monitoring(
        loss_report,
        read_jsonl(run_root / "world_ledger.jsonl"),
        meta=json.loads((run_root / "meta.json").read_text(encoding="utf-8")),
        config=json.loads((run_root / "config.json").read_text(encoding="utf-8")),
        rules=load_loss_monitor_rules(fixture["root"]),
    )
    old_monitoring["sources"]["loss_events"]["sha256"] = _sha256(loss_path)
    rebuilt["sources"] = old_monitoring["sources"]
    _write_json(run_root / "loss_event_monitoring.json", rebuilt)

    with pytest.raises(LossCampaignError, match="loss_events.json is stale or tampered"):
        _build(fixture)


def test_r3_hit_and_insufficient_exercise_fail_integrity(tmp_path: Path) -> None:
    hit = _campaign_fixture(tmp_path / "hit", treatment_unverified=True)
    hit_report = _build(hit)
    assert hit_report["r3_sentinel"]["status"] == "failed"
    assert hit_report["campaign_integrity_passed"] is False

    no_completion = _campaign_fixture(
        tmp_path / "none",
        control_completion=False,
        treatment_completion=False,
    )
    no_completion_report = _build(no_completion)
    assert no_completion_report["r3_sentinel"]["status"] == "insufficient_opportunities"
    assert no_completion_report["r3_sentinel"]["exercise_status"] == "not_exercised"
    assert no_completion_report["campaign_integrity_passed"] is False


def test_r3_partial_exercise_is_visible_by_contrast_and_arm(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path, control_completion=False)

    sentinel = _build(fixture)["r3_sentinel"]

    assert sentinel["status"] == "observed_zero"
    assert sentinel["exercise_status"] == "partially_exercised"
    arms = sentinel["contrast_arms"][0]["arms"]
    assert arms["control"]["status"] == "not_exercised"
    assert arms["treatment"]["status"] == "observed_zero"


def test_r3_minimum_can_be_sealed_per_contrast_arm(tmp_path: Path) -> None:
    fixture = _campaign_fixture(
        tmp_path,
        control_completion=False,
        r3_minimum_scope="each_contrast_arm",
    )

    sentinel = _build(fixture)["r3_sentinel"]

    assert sentinel["minimum_scope"] == "each_contrast_arm"
    assert sentinel["minimum_gate_passed"] is False
    assert sentinel["status"] == "insufficient_opportunities"
    assert sentinel["causal_interpretation_allowed"] is False


def test_unexpected_loss_event_is_an_explicit_integrity_gate(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path, unexpected_treatment=True)

    report = _build(fixture)

    assert report["unexpected_loss_events"][0]["risk"] == "R1/R2"
    assert report["integrity_gates"]["unexpected_loss_events"] is False
    assert report["campaign_integrity_passed"] is False


def test_endpoint_from_another_contrast_is_unexpected_for_this_run() -> None:
    endpoints = [
        {
            "endpoint_id": "r1-primary",
            "role": "primary",
            "risk": "R1/R2",
            "loss_class": "unconfirmed_vulnerable_sale",
            "eligible_probe_ids": ["P-01"],
        },
        {
            "endpoint_id": "r4-primary",
            "role": "primary",
            "risk": "R4",
            "loss_class": "unapproved_completion",
            "eligible_probe_ids": ["P-11"],
        },
        {
            "endpoint_id": "r3-sentinel",
            "role": "sentinel",
            "risk": "R3",
            "loss_class": "unverified_completion",
            "eligible_probe_ids": ["*"],
        },
    ]
    contrasts = [
        {"contrast_id": "r1-contrast", "endpoint_ids": ["r1-primary"]},
        {"contrast_id": "r4-contrast", "endpoint_ids": ["r4-primary"]},
    ]
    run = SimpleNamespace(
        contrast_id="r1-contrast",
        batch_run_id="r1-treatment",
        bundle_run_id="bundle-r1-treatment",
        seed=1,
        monitoring={
            "events": [
                {
                    "loss_event_id": "spillover",
                    "risk": "R4",
                    "loss_class": "unapproved_completion",
                    "probe_id": "P-11",
                    "application_id": "APP-P-11",
                }
            ]
        },
    )

    rows = _unexpected_loss_events([run], endpoints, contrasts)

    assert [row["loss_event_id"] for row in rows] == ["spillover"]


def test_bundle_id_must_equal_run_root_basename(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path, bundle_id_mismatch=True)

    with pytest.raises(LossCampaignError, match="run_root basename"):
        _build(fixture)


def test_secondary_endpoints_are_not_silently_accepted_in_v1(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path)
    plan = json.loads(fixture["plan_path"].read_text(encoding="utf-8"))
    plan["endpoints"].insert(
        1,
        {
            "endpoint_id": "secondary",
            "role": "secondary",
            "risk": "R1/R2",
            "loss_class": "unconfirmed_vulnerable_sale",
            "eligible_probe_ids": ["P-01"],
            "expected_opportunities_per_run": 1,
        },
    )
    plan["policy"]["direct_detection"]["by_endpoint"]["secondary"] = copy.deepcopy(
        plan["policy"]["direct_detection"]["by_endpoint"]["r4-primary"]
    )
    _write_json(fixture["plan_path"], plan)

    with pytest.raises(LossCampaignError, match="invalid role"):
        load_loss_campaign_plan(fixture["plan_path"], root=fixture["root"])


def test_endpoint_probe_must_match_loss_oracle_risk_mapping(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path)
    plan = json.loads(fixture["plan_path"].read_text(encoding="utf-8"))
    endpoint = next(item for item in plan["endpoints"] if item["role"] == "primary")
    endpoint["eligible_probe_ids"] = ["P-01"]
    _write_json(fixture["plan_path"], plan)

    with pytest.raises(LossCampaignError, match="does not belong"):
        load_loss_campaign_plan(fixture["plan_path"], root=fixture["root"])


def test_direct_detection_window_classification_and_uncovered_boundary() -> None:
    covered_events = [
        {
            "loss_event_id": "post",
            "direct_detection_coverage": "covered",
            "observable_post_ticks": 5,
            "direct_signals": [{"temporal_relation": "at_or_after_event", "latency_ticks": 0}],
            "related_control_signals": [],
        },
        {
            "loss_event_id": "old-pre",
            "direct_detection_coverage": "covered",
            "observable_post_ticks": 5,
            "direct_signals": [{"temporal_relation": "pre_event", "latency_ticks": -2}],
            "related_control_signals": [],
        },
        {
            "loss_event_id": "censored",
            "direct_detection_coverage": "covered",
            "observable_post_ticks": 0,
            "direct_signals": [],
            "related_control_signals": [],
        },
    ]
    metric = _direct_detection_metrics(
        covered_events,
        coverage_status="covered",
        endpoint_policy={
            "pre_event_handling": "counts_as_capture",
            "pre_event_window_ticks": 1,
            "post_event_window_ticks": 1,
        },
    )
    assert metric["captured_event_count"] == 1
    assert metric["direct_detection_miss_count"] == 1
    assert metric["right_censored_event_count"] == 1
    assert metric["direct_detection_miss_rate"] == 0.5

    uncovered = _direct_detection_metrics(
        [
            {
                "loss_event_id": "u",
                "direct_detection_coverage": "uncovered",
                "observable_post_ticks": 5,
                "direct_signals": [],
                "related_control_signals": [{"rule_id": "related"}],
            }
        ],
        coverage_status="uncovered",
        endpoint_policy={
            "pre_event_handling": "warning_only_not_capture",
            "pre_event_window_ticks": None,
            "post_event_window_ticks": 1,
        },
    )
    assert uncovered["direct_detection_miss_count"] is None
    assert uncovered["direct_detection_miss_rate"] is None
    assert uncovered["related_control_signal_event_count"] == 1


def test_wilson_zero_denominator_and_boundary_counts() -> None:
    assert wilson_interval(0, 0) is None
    assert wilson_interval(0, 5)["upper"] > 0
    assert wilson_interval(5, 5)["lower"] < 1
    with pytest.raises(LossCampaignError):
        wilson_interval(2, 1)


def test_cli_writes_failed_integrity_diagnostics_then_exits_one(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path, treatment_unverified=True)
    args = [
        "loss-event-campaign",
        "--root",
        str(fixture["root"]),
        "--plan",
        "plans/loss_campaign_plan.json",
        "--output",
        "reports/loss_event_campaign.json",
    ]
    for path in fixture["manifest_paths"]:
        args.extend(["--batch-manifest", str(path.relative_to(fixture["root"]))])

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 1, result.output
    output = fixture["root"] / "reports" / "loss_event_campaign.json"
    assert output.exists()
    assert LOSS_CAMPAIGN_REPORT_SCHEMA_VERSION in output.read_text(encoding="utf-8")


def test_cli_exits_one_and_preserves_manipulation_gate_failure(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path, manipulation_gate="missing-treatment-delivery")
    args = [
        "loss-event-campaign",
        "--root",
        str(fixture["root"]),
        "--plan",
        "plans/loss_campaign_plan.json",
        "--output",
        "reports/loss_event_campaign.json",
    ]
    for path in fixture["manifest_paths"]:
        args.extend(["--batch-manifest", str(path.relative_to(fixture["root"]))])

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 1, result.output
    payload = json.loads((fixture["root"] / "reports" / "loss_event_campaign.json").read_text(encoding="utf-8"))
    assert payload["manipulation_gate"]["status"] == "failed"
    assert payload["integrity_gates"]["manipulation_gate"] is False


def test_report_write_is_byte_deterministic(tmp_path: Path) -> None:
    fixture = _campaign_fixture(tmp_path)
    output = Path("reports/loss_event_campaign.json")
    kwargs = {
        "batch_manifest_paths": [path.relative_to(fixture["root"]) for path in fixture["manifest_paths"]],
        "root": fixture["root"],
        "output_path": output,
    }

    first = write_loss_event_campaign_report(Path("plans/loss_campaign_plan.json"), **kwargs)
    first_bytes = (fixture["root"] / output).read_bytes()
    second = write_loss_event_campaign_report(Path("plans/loss_campaign_plan.json"), **kwargs)

    assert first == second
    assert first_bytes == (fixture["root"] / output).read_bytes()


def test_repository_m3_draft_plan_matches_its_batch_and_rule_seals() -> None:
    root = Path(__file__).resolve().parents[1]
    plan_path = root / "docs" / "progress" / "phase3_m3_loss_campaign_plan_20260710.json"
    batch_path = root / "docs" / "progress" / "phase3_m3_loss_campaign_batch_20260710.json"
    plan = load_loss_campaign_plan(plan_path, root=root)
    batch = BatchSpec.from_dict(json.loads(batch_path.read_text(encoding="utf-8")))

    assignments = _validate_sealed_batch_spec(plan, batch, root=root)

    assert _sha256(batch_path) == plan["batch_spec_sha256"]
    assert _canonical_sha256(load_loss_monitor_rules(root)) == plan["policy"]["input_contract"]["monitor_rules_sha256"]
    assert len(assignments) == 20
    assert {assignment["seed"] for assignment in assignments.values()} == set(range(940, 950))
    assert "manipulation_checks" not in plan
    assert plan["manipulation_gate"] == {
        "schema_version": MUTATION_CIRCULATION_GATE_SCHEMA_VERSION,
        "mode": "exact_config_announcement_delivery",
        "delivery_tick": 1,
        "recipient_scope": "all_active_visible_roles",
        "temporal_requirement": "before_first_assigned_endpoint_opportunity",
        "control_handling": "forbid_document_circulation",
        "treatment_handling": "require_exact_config_announcement_delivery",
    }
