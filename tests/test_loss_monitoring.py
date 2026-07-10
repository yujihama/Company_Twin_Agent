from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from company_twin.cli import app
from company_twin.loss_monitoring import (
    DEFAULT_LOSS_MONITOR_RULES,
    LOSS_MONITORING_SCHEMA_VERSION,
    join_loss_events_to_monitoring,
    load_loss_monitor_rules,
    write_loss_event_monitoring,
)
from company_twin.loss_oracle import LOSS_RULES, loss_event_findings
from company_twin.recorder import RunRecorder, read_jsonl


def _build_bundle(
    tmp_path: Path,
    name: str,
    events: list[tuple[int, str, dict[str, Any]]],
    *,
    planned_ticks: int | None = None,
    committed_ticks: list[int] | None = None,
    post_commit_events: list[tuple[int, str, dict[str, Any]]] | None = None,
) -> Path:
    existing_customer_event_apps = {
        str(payload.get("application_id") or "")
        for _, event_type, payload in events
        if event_type == "customer_event"
    }
    probe_apps = {
        str(payload.get("application_id") or "")
        for _, event_type, payload in events
        if event_type in {"contract_completed", "documents_delivered"}
        and str(payload.get("application_id") or "").removeprefix("APP-") in LOSS_RULES
    }
    synthetic_customer_events = [
        (
            1,
            "customer_event",
            {
                "event_id": f"EVT-{app_id.removeprefix('APP-')}",
                "application_id": app_id,
                "customer_id": app_id.replace("APP-", "CUS-", 1),
                "product": "unit-test-product",
            },
        )
        for app_id in sorted(probe_apps - existing_customer_event_apps)
    ]
    events = [*synthetic_customer_events, *events]
    maximum_tick = max((tick for tick, _, _ in events), default=1)
    ticks = planned_ticks or maximum_tick
    committed = committed_ticks if committed_ticks is not None else list(range(1, ticks + 1))
    root = tmp_path / name
    recorder = RunRecorder(root, run_id=name, meta={"stage": "S2", "seed": 1, "live": True, "prompt_mode": "measurement"})
    events_by_tick: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    for tick, event_type, payload in events:
        events_by_tick.setdefault(tick, []).append((event_type, payload))
    for tick in range(1, ticks + 1):
        recorder.set_tick(tick)
        for event_type, payload in events_by_tick.get(tick, []):
            recorder.append_ledger(event_type, payload)
        if tick in committed:
            recorder.append_ledger("tick_committed", {"tick": tick})
    for tick, event_type, payload in post_commit_events or []:
        recorder.set_tick(tick)
        recorder.append_ledger(event_type, payload)
    config = {
        "schema_version": "company_twin.world_config.v2",
        "stage": "S2",
        "world": {
            "population": {
                "seats": {
                    "emp-Q": {"role": "second_line"},
                    "audit-in-world": {"role": "audit"},
                    "emp-A": {"role": "sales"},
                }
            },
            "schedule": {"ticks": ticks},
        },
    }
    (root / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    loss_event_findings(root)
    return root


def _identity(app_id: str) -> tuple[int, str, dict[str, Any]]:
    return (1, "identity_verified", {"application_id": app_id, "status": "identity_verified"})


def _completion(app_id: str, *, tick: int = 1) -> tuple[int, str, dict[str, Any]]:
    return (tick, "contract_completed", {"application_id": app_id, "status": "contracted"})


def _approval_notice(
    app_id: str,
    *,
    tick: int,
    recipients: tuple[str, ...] = ("emp-Q",),
    detail_app_id: str | None = None,
    detail_approval_id: str | None = None,
) -> list[tuple[int, str, dict[str, Any]]]:
    approval_id = f"APR-{app_id}"
    rows: list[tuple[int, str, dict[str, Any]]] = [
        (
            tick,
            "approval_deadline_overrun",
            {"approval_id": approval_id, "application_id": app_id, "due_tick": tick - 1, "tick": tick},
        )
    ]
    detail_id = detail_app_id or app_id
    rendered_approval_id = detail_approval_id or approval_id
    for recipient in recipients:
        rows.append(
            (
                tick,
                "inbox_delivered",
                {
                    "to_seat": recipient,
                    "message": {
                        "kind": "timed_notice",
                        "tick": tick,
                        "notice": "approval_deadline_overrun",
                        "detail": f"承認依頼 {rendered_approval_id}(案件 {detail_id})が期限を超過しています。",
                    },
                },
            )
        )
    return rows


def _event(report: dict[str, Any], loss_class: str) -> dict[str, Any]:
    return next(item for item in report["events"] if item["loss_class"] == loss_class)


def _join_with_synthetic_direct_rule(root: Path) -> dict[str, Any]:
    # Production deliberately classifies the approval notice as related-only.
    # This synthetic catalog exercises the generic direct-rule state machine.
    rules = json.loads(json.dumps(DEFAULT_LOSS_MONITOR_RULES))
    r4_coverage = next(entry for entry in rules["coverage"] if entry["risk"] == "R4")
    r4_coverage["direct_detection"] = "covered"
    rule = rules["rules"][0]
    rule["signal_class"] = "direct_detection"
    rule["counts_as_direct_detection"] = True
    return join_loss_events_to_monitoring(
        json.loads((root / "loss_events.json").read_text(encoding="utf-8")),
        read_jsonl(root / "world_ledger.jsonl"),
        meta=json.loads((root / "meta.json").read_text(encoding="utf-8")),
        config=json.loads((root / "config.json").read_text(encoding="utf-8")),
        rules=rules,
    )


def test_r4_notice_after_event_is_related_only_and_aggregates_recipient_deliveries(tmp_path: Path) -> None:
    app_id = "APP-P-11"
    root = _build_bundle(
        tmp_path,
        "r4-post",
        [
            _identity(app_id),
            _completion(app_id),
            *_approval_notice(app_id, tick=2, recipients=("emp-Q", "audit-in-world")),
        ],
        planned_ticks=2,
    )

    report = write_loss_event_monitoring(root)

    assert report["schema_version"] == LOSS_MONITORING_SCHEMA_VERSION
    event = _event(report, "unapproved_completion")
    assert event["direct_detection_coverage"] == "uncovered"
    assert event["direct_detection_status"] == "uncovered"
    assert event["direct_signals"] == []
    assert len(event["related_control_signals"]) == 1
    signal = event["related_control_signals"][0]
    assert signal["temporal_relation"] == "at_or_after_event"
    assert signal["latency_ticks"] == 1
    assert signal["recipient_seats"] == ["audit-in-world", "emp-Q"]
    assert signal["recipient_roles"] == ["audit", "second_line"]
    assert [delivery["seat_id"] for delivery in signal["deliveries"]] == ["emp-Q", "audit-in-world"]
    assert all(delivery["ledger_hash"] for delivery in signal["deliveries"])
    assert report["summary"]["events_with_related_control_signal"] == 1
    assert report["summary"]["direct_covered_event_count"] == 0
    r4_opportunity = next(item for item in report["opportunities"] if item["risk"] == "R4")
    assert r4_opportunity["application_id"] == app_id
    assert r4_opportunity["anchor"]["event_type"] == "customer_event"
    assert r4_opportunity["materialized_loss_event_id"] == event["loss_event_id"]
    assert report["sources"]["meta"]["sha256"]
    assert report["sources"]["config"]["sha256"]
    assert report["bundle"]["seed"] == 1
    assert report["bundle"]["live"] is True
    assert report["bundle"]["prompt_mode"] == "measurement"


def test_same_tick_ledger_order_classifies_pre_event_related_notice(tmp_path: Path) -> None:
    app_id = "APP-P-11"
    root = _build_bundle(
        tmp_path,
        "same-tick-pre",
        [
            _identity(app_id),
            *_approval_notice(app_id, tick=1),
            _completion(app_id),
        ],
    )

    signal = _event(write_loss_event_monitoring(root), "unapproved_completion")["related_control_signals"][0]
    assert signal["temporal_relation"] == "pre_event"
    assert signal["latency_ticks"] == 0
    assert max(delivery["ledger_ordinal"] for delivery in signal["deliveries"]) < _event(
        write_loss_event_monitoring(root), "unapproved_completion"
    )["completion"]["ledger_ordinal"]


def test_same_tick_notice_after_completion_is_at_or_after(tmp_path: Path) -> None:
    app_id = "APP-P-11"
    root = _build_bundle(
        tmp_path,
        "same-tick-post",
        [
            _identity(app_id),
            _completion(app_id),
            *_approval_notice(app_id, tick=1),
        ],
    )

    event = _event(write_loss_event_monitoring(root), "unapproved_completion")
    signal = event["related_control_signals"][0]
    assert signal["temporal_relation"] == "at_or_after_event"
    assert signal["latency_ticks"] == 0
    assert min(delivery["ledger_ordinal"] for delivery in signal["deliveries"]) > event["completion"]["ledger_ordinal"]


@pytest.mark.parametrize(
    ("name", "events", "expected_status"),
    [
        (
            "direct-pre",
            lambda app_id: [_identity(app_id), *_approval_notice(app_id, tick=1), _completion(app_id)],
            "pre_event_signal_only",
        ),
        (
            "direct-post",
            lambda app_id: [_identity(app_id), _completion(app_id), *_approval_notice(app_id, tick=1)],
            "at_or_after_event_signal",
        ),
        (
            "direct-silent",
            lambda app_id: [_identity(app_id), _completion(app_id)],
            "no_signal",
        ),
    ],
)
def test_synthetic_direct_rule_temporal_statuses(
    tmp_path: Path,
    name: str,
    events: Any,
    expected_status: str,
) -> None:
    app_id = "APP-P-11"
    root = _build_bundle(tmp_path, name, events(app_id))

    event = _event(_join_with_synthetic_direct_rule(root), "unapproved_completion")

    assert event["direct_detection_coverage"] == "covered"
    assert event["direct_detection_status"] == expected_status


def test_wrong_application_and_prefix_do_not_match(tmp_path: Path) -> None:
    app_id = "APP-P-11"
    root = _build_bundle(
        tmp_path,
        "wrong-app",
        [
            _identity(app_id),
            _completion(app_id),
            *_approval_notice(app_id, tick=2, detail_app_id="APP-P-1"),
        ],
        planned_ticks=2,
    )

    event = _event(write_loss_event_monitoring(root), "unapproved_completion")
    assert event["related_control_signals"] == []
    assert write_loss_event_monitoring(root)["summary"]["events_with_related_control_signal"] == 0


def test_approval_id_must_match_exactly_one_origin(tmp_path: Path) -> None:
    app_id = "APP-P-11"
    root = _build_bundle(
        tmp_path,
        "wrong-approval",
        [
            _identity(app_id),
            _completion(app_id),
            *_approval_notice(app_id, tick=2, detail_approval_id="APR-FAKE"),
        ],
        planned_ticks=2,
    )

    event = _event(write_loss_event_monitoring(root), "unapproved_completion")
    assert event["related_control_signals"] == []


def test_multiple_approvals_same_tick_bind_to_their_own_origins(tmp_path: Path) -> None:
    app_id = "APP-P-11"
    rows: list[tuple[int, str, dict[str, Any]]] = [_identity(app_id), _completion(app_id)]
    for approval_id in ("APR-ONE", "APR-TWO"):
        rows.extend(
            [
                (
                    2,
                    "approval_deadline_overrun",
                    {"approval_id": approval_id, "application_id": app_id, "due_tick": 1, "tick": 2},
                ),
                (
                    2,
                    "inbox_delivered",
                    {
                        "to_seat": "emp-Q",
                        "message": {
                            "kind": "timed_notice",
                            "tick": 2,
                            "notice": "approval_deadline_overrun",
                            "detail": f"承認依頼 {approval_id}(案件 {app_id})が期限を超過しています。",
                        },
                    },
                ),
            ]
        )
    root = _build_bundle(tmp_path, "two-approvals", rows, planned_ticks=2)

    signals = _event(write_loss_event_monitoring(root), "unapproved_completion")["related_control_signals"]

    assert [signal["approval_id"] for signal in signals] == ["APR-ONE", "APR-TWO"]
    assert len({signal["origin"]["ledger_hash"] for signal in signals}) == 2


def test_delivery_without_origin_and_non_quality_delivery_do_not_match(tmp_path: Path) -> None:
    app_id = "APP-P-11"
    delivery_without_origin = (
        2,
        "inbox_delivered",
        {
            "to_seat": "emp-Q",
            "message": {
                "kind": "timed_notice",
                "tick": 2,
                "notice": "approval_deadline_overrun",
                "detail": f"承認依頼 APR-NONE(案件 {app_id})が期限を超過しています。",
            },
        },
    )
    root = _build_bundle(
        tmp_path,
        "delivery-no-origin",
        [_identity(app_id), _completion(app_id), delivery_without_origin],
        planned_ticks=2,
    )
    assert _event(write_loss_event_monitoring(root), "unapproved_completion")["related_control_signals"] == []

    sales_only = _build_bundle(
        tmp_path,
        "sales-only",
        [_identity(app_id), _completion(app_id), *_approval_notice(app_id, tick=2, recipients=("emp-A",))],
        planned_ticks=2,
    )
    assert _event(write_loss_event_monitoring(sales_only), "unapproved_completion")["related_control_signals"] == []


def test_raw_internal_overrun_without_world_visible_delivery_does_not_match(tmp_path: Path) -> None:
    app_id = "APP-P-11"
    root = _build_bundle(
        tmp_path,
        "raw-only",
        [
            _identity(app_id),
            _completion(app_id),
            (
                2,
                "approval_deadline_overrun",
                {"approval_id": "APR-1", "application_id": app_id, "due_tick": 1, "tick": 2},
            ),
        ],
        planned_ticks=2,
    )

    event = _event(write_loss_event_monitoring(root), "unapproved_completion")
    assert event["related_control_signals"] == []


def test_r1_r2_generic_review_notice_remains_uncovered(tmp_path: Path) -> None:
    app_id = "APP-P-01"
    root = _build_bundle(
        tmp_path,
        "r1-uncovered",
        [
            _identity(app_id),
            _completion(app_id),
            (
                2,
                "inbox_delivered",
                {
                    "to_seat": "emp-Q",
                    "message": {
                        "kind": "timed_notice",
                        "tick": 2,
                        "notice": "evidence_check_review",
                        "detail": f"案件 {app_id} の証跡をご確認ください。",
                    },
                },
            ),
        ],
        planned_ticks=2,
    )

    event = _event(write_loss_event_monitoring(root), "unconfirmed_vulnerable_sale")
    assert event["risk"] == "R1/R2"
    assert event["direct_detection_status"] == "uncovered"
    assert event["applicable_related_rule_ids"] == []
    assert event["related_control_signals"] == []


def test_r3_kernel_diagnostics_are_not_world_visible_detection(tmp_path: Path) -> None:
    app_id = "APP-X"
    root = _build_bundle(
        tmp_path,
        "r3-uncovered",
        [
            _completion(app_id),
            (
                2,
                "state_transition_ignored",
                {"application_id": app_id, "from": "contracted", "to": "identity_verified"},
            ),
            (
                2,
                "permission_denied",
                {"seat_id": "emp-C", "tool": "verify_identity", "args": {"application_id": app_id}},
            ),
        ],
        planned_ticks=2,
    )

    event = _event(write_loss_event_monitoring(root), "unverified_completion")
    assert event["risk"] == "R3"
    assert event["direct_detection_status"] == "uncovered"
    assert event["direct_signals"] == []
    assert event["related_control_signals"] == []
    opportunity = next(item for item in write_loss_event_monitoring(root)["opportunities"] if item["risk"] == "R3")
    assert opportunity["materialized_loss_event_id"] == event["loss_event_id"]


def test_probe_opportunity_must_precede_first_completion(tmp_path: Path) -> None:
    app_id = "APP-P-11"
    root = _build_bundle(
        tmp_path,
        "late-opportunity",
        [
            _identity(app_id),
            _completion(app_id),
            (
                2,
                "customer_event",
                {
                    "event_id": "EVT-P-11",
                    "application_id": app_id,
                    "customer_id": "CUS-P-11",
                    "product": "unit-test-product",
                },
            ),
        ],
        planned_ticks=2,
    )

    with pytest.raises(ValueError, match="opportunity anchor is not before first completion"):
        write_loss_event_monitoring(root)


def test_late_probe_opportunity_is_rejected_even_without_materialized_loss(tmp_path: Path) -> None:
    app_id = "APP-P-11"
    root = _build_bundle(
        tmp_path,
        "late-opportunity-no-loss",
        [
            _identity(app_id),
            (1, "approval_granted", {"application_id": app_id, "approval_id": "APR-1", "status": "approved"}),
            _completion(app_id),
            (
                2,
                "customer_event",
                {
                    "event_id": "EVT-P-11",
                    "application_id": app_id,
                    "customer_id": "CUS-P-11",
                    "product": "unit-test-product",
                },
            ),
        ],
        planned_ticks=2,
    )

    with pytest.raises(ValueError, match="opportunity anchor is not before first completion"):
        write_loss_event_monitoring(root)


@pytest.mark.parametrize("mismatch", ["origin", "delivery"])
def test_related_notice_internal_tick_mismatch_fails_closed(tmp_path: Path, mismatch: str) -> None:
    app_id = "APP-P-11"
    notice_rows = _approval_notice(app_id, tick=2)
    if mismatch == "origin":
        notice_rows[0][2]["tick"] = 99
    else:
        notice_rows[1][2]["message"]["tick"] = 99
    root = _build_bundle(
        tmp_path,
        f"tick-mismatch-{mismatch}",
        [_identity(app_id), _completion(app_id), *notice_rows],
        planned_ticks=2,
    )

    with pytest.raises(ValueError, match="tick differs from ledger tick"):
        write_loss_event_monitoring(root)


def test_zero_loss_event_report_is_valid_and_has_no_synthetic_rate(tmp_path: Path) -> None:
    root = _build_bundle(tmp_path, "zero", [])

    report = write_loss_event_monitoring(root)

    assert report["summary"]["loss_event_count"] == 0
    assert report["summary"]["opportunity_count"] == 0
    assert report["summary"]["direct_covered_event_count"] == 0
    assert report["summary"]["direct_uncovered_event_count"] == 0
    assert report["summary"]["direct_status_counts"] == {}
    assert report["summary"]["events_with_related_control_signal"] == 0
    assert report["events"] == []
    assert not any("rate" in key for key in report["summary"])


def test_zero_loss_run_preserves_nonzero_probe_opportunities(tmp_path: Path) -> None:
    app_id = "APP-P-11"
    root = _build_bundle(
        tmp_path,
        "zero-loss-opportunities",
        [
            _identity(app_id),
            (1, "approval_granted", {"application_id": app_id, "approval_id": "APR-1", "status": "approved"}),
            _completion(app_id),
        ],
    )

    report = write_loss_event_monitoring(root)

    assert report["summary"]["loss_event_count"] == 0
    assert report["summary"]["opportunity_count"] == 2  # R4 seeded exposure + R3 completion sentinel
    assert {item["risk"] for item in report["opportunities"]} == {"R3", "R4"}
    assert all(item["materialized_loss_event_id"] is None for item in report["opportunities"])
    assert report["opportunity_inventory_basis"]["primary_occurrence_denominator"].startswith("not_decided")


def test_incomplete_run_and_invalid_loss_schema_fail_closed(tmp_path: Path) -> None:
    incomplete = _build_bundle(
        tmp_path,
        "incomplete",
        [],
        planned_ticks=2,
        committed_ticks=[1],
    )
    with pytest.raises(ValueError, match="run is incomplete"):
        write_loss_event_monitoring(incomplete)

    invalid = _build_bundle(tmp_path, "invalid-schema", [])
    loss_path = invalid / "loss_events.json"
    payload = json.loads(loss_path.read_text(encoding="utf-8"))
    payload["schema_version"] = "company_twin.loss_events.v1"
    loss_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        write_loss_event_monitoring(invalid)


def test_post_commit_append_and_broken_hash_fail_closed(tmp_path: Path) -> None:
    post_commit = _build_bundle(
        tmp_path,
        "post-commit",
        [],
        post_commit_events=[(1, "daily_inbox_delivery", {"tick": 1})],
    )
    with pytest.raises(ValueError, match="after tick 1 was committed"):
        write_loss_event_monitoring(post_commit)

    broken = _build_bundle(tmp_path, "broken-hash", [])
    ledger_path = broken / "world_ledger.jsonl"
    rows = read_jsonl(ledger_path)
    rows[-1]["hash"] = "0" * 64
    ledger_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(ValueError, match="row hash is invalid"):
        write_loss_event_monitoring(broken)


def test_hash_valid_nonmonotonic_ticks_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "nonmonotonic"
    recorder = RunRecorder(root, run_id="nonmonotonic", meta={"stage": "S2", "seed": 1, "live": True, "prompt_mode": "measurement"})
    recorder.set_tick(2)
    recorder.append_ledger("daily_inbox_delivery", {"tick": 2})
    recorder.set_tick(1)
    recorder.append_ledger("tick_committed", {"tick": 1})
    recorder.set_tick(2)
    recorder.append_ledger("tick_committed", {"tick": 2})
    config = {
        "schema_version": "company_twin.world_config.v2",
        "stage": "S2",
        "world": {"population": {"seats": {}}, "schedule": {"ticks": 2}},
    }
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    loss_event_findings(root)

    with pytest.raises(ValueError, match="ticks are not monotonic"):
        write_loss_event_monitoring(root)


def test_stale_cross_bundle_loss_report_fails_even_if_run_root_is_rewritten(tmp_path: Path) -> None:
    app_id = "APP-P-11"
    source = _build_bundle(tmp_path, "source-loss", [_identity(app_id), _completion(app_id)])
    target = _build_bundle(
        tmp_path,
        "target-approved",
        [
            _identity(app_id),
            (1, "approval_granted", {"application_id": app_id, "approval_id": "APR-1", "status": "approved"}),
            _completion(app_id),
        ],
    )
    copied = json.loads((source / "loss_events.json").read_text(encoding="utf-8"))
    copied["run_root"] = str(target.resolve())
    (target / "loss_events.json").write_text(json.dumps(copied), encoding="utf-8")

    with pytest.raises(ValueError, match="stale or does not match"):
        write_loss_event_monitoring(target)


def test_explicit_missing_or_inconsistent_rule_catalog_fails_closed(tmp_path: Path) -> None:
    root = _build_bundle(tmp_path, "rules", [])
    missing = tmp_path / "missing-rules-root"
    with pytest.raises(FileNotFoundError, match="rule catalog is missing"):
        write_loss_event_monitoring(root, rules_root=missing)

    rules_root = tmp_path / "bad-rules-root"
    catalog_path = rules_root / "data" / "compiled_data" / "loss_monitoring_rules_v1.json"
    catalog_path.parent.mkdir(parents=True)
    payload = json.loads(json.dumps(DEFAULT_LOSS_MONITOR_RULES))
    next(entry for entry in payload["coverage"] if entry["risk"] == "R4")["direct_detection"] = "covered"
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="coverage/direct-rule mismatch"):
        write_loss_event_monitoring(root, rules_root=rules_root)

    payload = json.loads(json.dumps(DEFAULT_LOSS_MONITOR_RULES))
    del payload["rules"][0]["capture_basis"]
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="requires capture_basis"):
        write_loss_event_monitoring(root, rules_root=rules_root)


def test_completion_anchor_mismatch_fails_closed(tmp_path: Path) -> None:
    app_id = "APP-P-11"
    root = _build_bundle(tmp_path, "anchor-mismatch", [_identity(app_id), _completion(app_id)])
    loss_path = root / "loss_events.json"
    payload = json.loads(loss_path.read_text(encoding="utf-8"))
    finding = next(item for item in payload["loss_events"] if item["loss_class"] == "unapproved_completion")
    finding["completion_tick"] = 99
    loss_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="stale or does not match"):
        write_loss_event_monitoring(root)


def test_rule_file_matches_builtin_and_output_is_byte_deterministic(tmp_path: Path) -> None:
    assert load_loss_monitor_rules() == DEFAULT_LOSS_MONITOR_RULES
    root = _build_bundle(tmp_path, "deterministic", [])

    first = write_loss_event_monitoring(root)
    first_bytes = (root / "loss_event_monitoring.json").read_bytes()
    second = write_loss_event_monitoring(root)
    second_bytes = (root / "loss_event_monitoring.json").read_bytes()

    assert first == second
    assert first_bytes == second_bytes


def test_cli_writes_monitoring_report(tmp_path: Path) -> None:
    root = _build_bundle(tmp_path, "cli", [])

    result = CliRunner().invoke(app, ["loss-event-monitoring", "--run-root", str(root)])

    assert result.exit_code == 0, result.output
    assert (root / "loss_event_monitoring.json").exists()
    assert LOSS_MONITORING_SCHEMA_VERSION in result.output
