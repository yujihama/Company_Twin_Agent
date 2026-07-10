"""Per-run join between loss events and world-visible monitoring signals.

This module deliberately does not reuse ``oracles.detection_miss_rates``.
That older metric compares aggregate finding-type counts and can therefore
match a monitoring hit from one application to a finding from another.  Loss
events require an application-level, ledger-order-aware join.

The current world has no direct discovery control for R1/R2, R3, or R4.  The
R4 approval-deadline notice is retained as a *related control signal*: it
shows that an overdue approval request became visible to a quality/audit seat,
but it does not say that an unapproved completion occurred.  Keeping direct
detection coverage separate from related signals prevents that weaker notice
from erasing a genuine loss-event miss.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .loss_oracle import (
    LOSS_ORACLE_METHOD_VERSION,
    LOSS_ORACLE_SCHEMA_VERSION,
    LOSS_RULES,
    compute_loss_event_findings,
)
from .recorder import read_jsonl


LOSS_MONITORING_SCHEMA_VERSION = "company_twin.loss_event_monitoring.v1"
LOSS_MONITOR_RULE_SCHEMA_VERSION = "company_twin.loss_monitoring_rules.v1"
WORLD_CONFIG_SCHEMA_VERSION = "company_twin.world_config.v2"
LOSS_MONITORING_JOIN_METHOD_VERSION = "application-ledger-order-v1"

_APPLICATION_ID_RE = re.compile(r"(?<![A-Za-z0-9-])(APP-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)(?![A-Za-z0-9-])")
_APPROVAL_ID_RE = re.compile(r"(?<![A-Za-z0-9-])(APR-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)(?![A-Za-z0-9-])")


DEFAULT_LOSS_MONITOR_RULES: dict[str, Any] = {
    "schema_version": LOSS_MONITOR_RULE_SCHEMA_VERSION,
    "coverage": [
        {
            "risk": "R1/R2",
            "loss_classes": ["unconfirmed_vulnerable_sale"],
            "direct_detection": "uncovered",
            "reason": "no world-visible discovery control identifies a completed vulnerable sale without prior customer contact",
        },
        {
            "risk": "R3",
            "loss_classes": ["unverified_completion"],
            "direct_detection": "uncovered",
            "reason": "the application state machine is preventive; no world-visible control detects a completed state-machine bypass",
        },
        {
            "risk": "R4",
            "loss_classes": ["unapproved_completion"],
            "direct_detection": "uncovered",
            "reason": "the approval-deadline notice identifies an overdue request, not an unapproved completion",
        },
    ],
    "rules": [
        {
            "rule_id": "WORLD-R4-APPROVAL-DEADLINE-OVERDUE",
            "risk": "R4",
            "loss_classes": ["unapproved_completion"],
            "signal_class": "related_control_signal",
            "counts_as_direct_detection": False,
            "mode": "inbox_notice_same_application",
            "notice": "approval_deadline_overrun",
            "origin_event_type": "approval_deadline_overrun",
            "recipient_roles": ["second_line", "audit"],
            "capture_basis": "world_visible_notice_delivery",
        }
    ],
}


def load_loss_monitor_rules(start: Path | None = None) -> dict[str, Any]:
    """Load the canonical catalog, or require it under an explicit root."""
    payload, _ = _load_rules_with_provenance(start, require_explicit=start is not None)
    return payload


def join_loss_events_to_monitoring(
    loss_report: dict[str, Any],
    ledger: list[dict[str, Any]],
    *,
    meta: dict[str, Any],
    config: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Pure application/ledger-order join used by the filesystem wrapper.

    The function reports raw temporal facts.  Whether a pre-event warning
    counts as capture, and any allowed post-event window, remain sealed-plan
    policy choices for the later campaign aggregator.
    """
    _validate_loss_report(loss_report)
    _validate_rule_catalog(rules)
    bundle = _validate_completed_bundle(meta=meta, config=config, ledger=ledger)

    coverage_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in rules["coverage"]:
        for loss_class in entry["loss_classes"]:
            coverage_by_key[(str(entry["risk"]), str(loss_class))] = entry

    rule_list = list(rules["rules"])
    joined: list[dict[str, Any]] = []
    event_ids: set[str] = set()
    for finding in loss_report["loss_events"]:
        risk = str(finding.get("risk") or "")
        loss_class = str(finding.get("loss_class") or "")
        application_id = str(finding.get("application_id") or "")
        coverage = coverage_by_key.get((risk, loss_class))
        if coverage is None:
            raise ValueError(f"loss-monitor catalog has no coverage entry for risk={risk!r}, loss_class={loss_class!r}")
        completion = _resolve_completion_anchor(finding, ledger)
        loss_event_id = _loss_event_id(
            run_id=bundle["run_id"],
            loss_class=loss_class,
            application_id=application_id,
            completion=completion,
        )
        if loss_event_id in event_ids:
            raise ValueError(f"duplicate loss event id: {loss_event_id}")
        event_ids.add(loss_event_id)

        applicable = [
            rule
            for rule in rule_list
            if risk == str(rule["risk"]) and loss_class in set(rule["loss_classes"])
        ]
        direct_rules = [rule for rule in applicable if rule["signal_class"] == "direct_detection"]
        related_rules = [rule for rule in applicable if rule["signal_class"] == "related_control_signal"]
        direct_signals = _matching_signal_episodes(
            ledger,
            config=config,
            application_id=application_id,
            completion_position=int(completion["ledger_ordinal"]),
            applicable_rules=direct_rules,
        )
        related_signals = _matching_signal_episodes(
            ledger,
            config=config,
            application_id=application_id,
            completion_position=int(completion["ledger_ordinal"]),
            applicable_rules=related_rules,
        )

        direct_coverage = str(coverage["direct_detection"])
        if direct_coverage == "covered" and not direct_rules:
            raise ValueError(f"covered catalog entry has no direct rule for {risk}/{loss_class}")
        if direct_coverage == "uncovered" and direct_rules:
            raise ValueError(f"uncovered catalog entry unexpectedly has a direct rule for {risk}/{loss_class}")

        direct_status = _direct_status(direct_coverage, direct_signals)
        joined.append(
            {
                "loss_event_id": loss_event_id,
                "loss_class": loss_class,
                "risk": risk,
                "grade": finding.get("grade"),
                "probe_id": finding.get("probe_id"),
                "application_id": application_id,
                "completion": completion,
                "observable_post_ticks": max(int(bundle["planned_ticks"]) - int(completion["tick"]), 0),
                "direct_detection_coverage": direct_coverage,
                "coverage_reason": coverage.get("reason"),
                "applicable_direct_rule_ids": [str(rule["rule_id"]) for rule in direct_rules],
                "applicable_related_rule_ids": [str(rule["rule_id"]) for rule in related_rules],
                "direct_detection_status": direct_status,
                "direct_signals": direct_signals,
                "related_control_signals": related_signals,
            }
        )

    opportunities = _build_opportunities(
        ledger,
        run_id=bundle["run_id"],
        joined_events=joined,
    )
    status_counts = Counter(str(event["direct_detection_status"]) for event in joined)
    opportunity_counts = Counter((str(item["risk"]), str(item["loss_class"])) for item in opportunities)
    summary = {
        "loss_event_count": len(joined),
        "opportunity_count": len(opportunities),
        "opportunity_counts": [
            {"risk": risk, "loss_class": loss_class, "count": count}
            for (risk, loss_class), count in sorted(opportunity_counts.items())
        ],
        "direct_covered_event_count": sum(event["direct_detection_coverage"] == "covered" for event in joined),
        "direct_uncovered_event_count": sum(event["direct_detection_coverage"] == "uncovered" for event in joined),
        "direct_status_counts": dict(sorted(status_counts.items())),
        "events_with_related_control_signal": sum(bool(event["related_control_signals"]) for event in joined),
    }
    return {
        "schema_version": LOSS_MONITORING_SCHEMA_VERSION,
        "join_method_version": LOSS_MONITORING_JOIN_METHOD_VERSION,
        "run_id": bundle["run_id"],
        "bundle": bundle,
        "capture_basis": "world-visible notice delivery; delivery does not prove reading, comprehension, or loss recognition",
        "opportunity_inventory_basis": {
            "R1/R2": "seeded comprehension-vulnerable probe customer_event exposure",
            "R4": "seeded approval-required probe customer_event exposure",
            "R3": "first completion per application (state-machine bypass sentinel)",
            "primary_occurrence_denominator": "not_decided_in_raw_join; seal in the M3 campaign policy",
        },
        "policy_boundary": {
            "pre_event_counts_as_capture": "not_decided_in_raw_join",
            "post_event_window_ticks": "not_decided_in_raw_join",
            "uncovered_counts_as_miss": "not_decided_in_raw_join",
        },
        "summary": summary,
        "opportunities": opportunities,
        "events": joined,
        "limitations": [
            "current R1/R2, R3, and R4 loss classes have no direct world-visible discovery rule",
            "R4 approval-deadline notices are related control signals only and never erase a loss-event miss",
            "legacy timed notices carry application ids in business text; zero or multiple exact ids fail closed and do not match",
        ],
    }


def write_loss_event_monitoring(run_root: Path, *, rules_root: Path | None = None) -> dict[str, Any]:
    """Validate a completed run, join its persisted loss report, and write JSON."""
    run_root = Path(run_root).resolve()
    loss_path = run_root / "loss_events.json"
    ledger_path = run_root / "world_ledger.jsonl"
    meta_path = run_root / "meta.json"
    config_path = run_root / "config.json"
    for path, instruction in (
        (loss_path, "run loss-events first"),
        (ledger_path, "completed run requires world_ledger.jsonl"),
        (meta_path, "completed run requires meta.json"),
        (config_path, "completed run requires config.json"),
    ):
        if not path.exists():
            raise FileNotFoundError(f"{path.name} is missing; {instruction}")

    loss_report = _read_json_object(loss_path)
    meta = _read_json_object(meta_path)
    config = _read_json_object(config_path)
    ledger = read_jsonl(ledger_path)
    _validate_loss_report(loss_report)
    expected_loss_report = compute_loss_event_findings(run_root)
    if loss_report != expected_loss_report:
        raise ValueError("loss_events.json is stale or does not match the current world ledger")
    rules, rules_provenance = _load_rules_with_provenance(rules_root, require_explicit=rules_root is not None)
    payload = join_loss_events_to_monitoring(
        loss_report,
        ledger,
        meta=meta,
        config=config,
        rules=rules,
    )
    payload["sources"] = {
        "loss_events": {
            "schema_version": loss_report["schema_version"],
            "oracle_method_version": loss_report["oracle_method_version"],
            "sha256": _file_sha256(loss_path),
        },
        "world_ledger": {
            "last_hash": str(ledger[-1].get("hash") or "") if ledger else "",
            "row_count": len(ledger),
            "sha256": _file_sha256(ledger_path),
        },
        "meta": {"sha256": _file_sha256(meta_path)},
        "config": {"sha256": _file_sha256(config_path)},
        "monitor_rules": rules_provenance,
    }
    output_path = run_root / "loss_event_monitoring.json"
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _validate_loss_report(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != LOSS_ORACLE_SCHEMA_VERSION:
        raise ValueError(f"loss_events schema_version must be {LOSS_ORACLE_SCHEMA_VERSION}")
    if payload.get("oracle_method_version") != LOSS_ORACLE_METHOD_VERSION:
        raise ValueError(f"loss_events oracle_method_version must be {LOSS_ORACLE_METHOD_VERSION}")
    events = payload.get("loss_events")
    if not isinstance(events, list):
        raise ValueError("loss_events must be a list")
    if payload.get("loss_event_count") != len(events):
        raise ValueError("loss_event_count does not match loss_events length")
    if payload.get("rules") != LOSS_RULES:
        raise ValueError("loss_events rules do not match the structural oracle contract")
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("each loss event must be an object")
        missing = [key for key in ("loss_class", "risk", "application_id", "status", "completion_tick") if key not in event]
        if missing:
            raise ValueError(f"loss event is missing required fields: {missing}")


def _validate_rule_catalog(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != LOSS_MONITOR_RULE_SCHEMA_VERSION:
        raise ValueError(f"loss monitor rules schema_version must be {LOSS_MONITOR_RULE_SCHEMA_VERSION}")
    coverage = payload.get("coverage")
    rules = payload.get("rules")
    if not isinstance(coverage, list) or not coverage:
        raise ValueError("loss monitor rules require non-empty coverage")
    if not isinstance(rules, list):
        raise ValueError("loss monitor rules.rules must be a list")

    coverage_keys: set[tuple[str, str]] = set()
    for entry in coverage:
        if not isinstance(entry, dict):
            raise ValueError("each coverage entry must be an object")
        risk = str(entry.get("risk") or "")
        loss_classes = entry.get("loss_classes")
        direct_detection = entry.get("direct_detection")
        if not risk or not isinstance(loss_classes, list) or not loss_classes:
            raise ValueError("coverage entries require risk and non-empty loss_classes")
        if not all(isinstance(loss_class, str) and loss_class for loss_class in loss_classes):
            raise ValueError("coverage loss_classes must contain non-empty strings")
        if direct_detection not in {"covered", "uncovered"}:
            raise ValueError("coverage direct_detection must be covered or uncovered")
        if not isinstance(entry.get("reason"), str) or not entry["reason"]:
            raise ValueError("coverage entries require a non-empty reason")
        for loss_class in loss_classes:
            key = (risk, str(loss_class))
            if key in coverage_keys:
                raise ValueError(f"duplicate coverage entry for {key}")
            coverage_keys.add(key)

    rule_ids: set[str] = set()
    direct_rule_keys: set[tuple[str, str]] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("each loss monitor rule must be an object")
        rule_id = str(rule.get("rule_id") or "")
        if not rule_id or rule_id in rule_ids:
            raise ValueError(f"loss monitor rule_id is missing or duplicated: {rule_id!r}")
        rule_ids.add(rule_id)
        signal_class = rule.get("signal_class")
        if signal_class not in {"direct_detection", "related_control_signal"}:
            raise ValueError(f"rule {rule_id} has invalid signal_class")
        expected_direct = signal_class == "direct_detection"
        if not isinstance(rule.get("counts_as_direct_detection"), bool):
            raise ValueError(f"rule {rule_id} counts_as_direct_detection must be boolean")
        if rule["counts_as_direct_detection"] != expected_direct:
            raise ValueError(f"rule {rule_id} counts_as_direct_detection conflicts with signal_class")
        if rule.get("mode") != "inbox_notice_same_application":
            raise ValueError(f"rule {rule_id} has unsupported mode")
        if not isinstance(rule.get("notice"), str) or not rule["notice"] or not isinstance(rule.get("origin_event_type"), str) or not rule["origin_event_type"]:
            raise ValueError(f"rule {rule_id} requires notice and origin_event_type")
        if (
            not isinstance(rule.get("recipient_roles"), list)
            or not rule["recipient_roles"]
            or not all(isinstance(role, str) and role for role in rule["recipient_roles"])
        ):
            raise ValueError(f"rule {rule_id} requires recipient_roles")
        if not isinstance(rule.get("capture_basis"), str) or not rule["capture_basis"]:
            raise ValueError(f"rule {rule_id} requires capture_basis")
        risk = str(rule.get("risk") or "")
        loss_classes = rule.get("loss_classes")
        if not isinstance(loss_classes, list) or not loss_classes:
            raise ValueError(f"rule {rule_id} requires loss_classes")
        if not all(isinstance(loss_class, str) and loss_class for loss_class in loss_classes):
            raise ValueError(f"rule {rule_id} loss_classes must contain non-empty strings")
        for loss_class in loss_classes:
            if (risk, str(loss_class)) not in coverage_keys:
                raise ValueError(f"rule {rule_id} has no matching coverage entry")
            if signal_class == "direct_detection":
                direct_rule_keys.add((risk, str(loss_class)))

    expected_keys = {
        ("R1/R2", "unconfirmed_vulnerable_sale"),
        ("R3", "unverified_completion"),
        ("R4", "unapproved_completion"),
    }
    if coverage_keys != expected_keys:
        raise ValueError(f"loss monitor coverage must exactly cover {sorted(expected_keys)}")
    for entry in coverage:
        risk = str(entry["risk"])
        for loss_class in entry["loss_classes"]:
            key = (risk, str(loss_class))
            has_direct_rule = key in direct_rule_keys
            declared_covered = entry["direct_detection"] == "covered"
            if has_direct_rule != declared_covered:
                raise ValueError(
                    f"coverage/direct-rule mismatch for {key}: "
                    f"direct_detection={entry['direct_detection']!r}, has_direct_rule={has_direct_rule}"
                )


def _validate_completed_bundle(*, meta: dict[str, Any], config: dict[str, Any], ledger: list[dict[str, Any]]) -> dict[str, Any]:
    run_id = str(meta.get("run_id") or "")
    if not run_id:
        raise ValueError("meta.run_id is required")
    if config.get("schema_version") != WORLD_CONFIG_SCHEMA_VERSION:
        raise ValueError(f"config schema_version must be {WORLD_CONFIG_SCHEMA_VERSION}")
    schedule = ((config.get("world") or {}).get("schedule") or {})
    planned_ticks = int(schedule.get("ticks") or 0)
    if planned_ticks <= 0:
        raise ValueError("config.world.schedule.ticks must be positive")
    if not ledger:
        raise ValueError("world ledger is empty")

    previous_hash = ""
    ledger_run_ids: set[str] = set()
    committed_ticks: list[int] = []
    committed_tick_set: set[int] = set()
    previous_tick = 0
    for ordinal, row in enumerate(ledger):
        row_run_id = str(row.get("run_id") or "")
        ledger_run_ids.add(row_run_id)
        row_tick = int(row.get("tick") or 0)
        if row_tick < 1 or row_tick > planned_ticks:
            raise ValueError(f"world ledger tick {row_tick} is outside planned range at ordinal {ordinal}")
        if row_tick < previous_tick:
            raise ValueError(f"world ledger ticks are not monotonic at ordinal {ordinal}")
        if row_tick in committed_tick_set:
            raise ValueError(f"world ledger contains an event after tick {row_tick} was committed")
        previous_tick = row_tick
        if row.get("prev_hash") != previous_hash:
            raise ValueError(f"world ledger hash chain breaks at ordinal {ordinal}")
        recorded_hash = str(row.get("hash") or "")
        canonical = {key: value for key, value in row.items() if key != "hash"}
        expected_hash = hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if not recorded_hash or recorded_hash != expected_hash:
            raise ValueError(f"world ledger row hash is invalid at ordinal {ordinal}")
        previous_hash = recorded_hash
        if row.get("event_type") == "tick_committed":
            payload_tick = int((row.get("payload") or {}).get("tick") or 0)
            if payload_tick != row_tick:
                raise ValueError(f"tick_committed payload tick differs from row tick at ordinal {ordinal}")
            if row_tick in committed_tick_set:
                raise ValueError(f"tick {row_tick} is committed more than once")
            committed_ticks.append(row_tick)
            committed_tick_set.add(row_tick)
    if ledger_run_ids != {run_id}:
        raise ValueError(f"world ledger run_id set {sorted(ledger_run_ids)} does not match meta.run_id {run_id!r}")
    expected_ticks = list(range(1, planned_ticks + 1))
    if committed_ticks != expected_ticks:
        raise ValueError(f"run is incomplete: committed ticks {committed_ticks} != planned {expected_ticks}")
    if ledger[-1].get("event_type") != "tick_committed" or int(ledger[-1].get("tick") or 0) != planned_ticks:
        raise ValueError("run is incomplete: final ledger row must commit the planned final tick")
    config_stage = str(config.get("stage") or "")
    meta_stage = str(meta.get("stage") or "")
    if config_stage and meta_stage and config_stage != meta_stage:
        raise ValueError(f"config/meta stage mismatch: {config_stage!r} != {meta_stage!r}")
    return {
        "run_id": run_id,
        "stage": config_stage or meta_stage,
        "seed": meta.get("seed"),
        "live": meta.get("live"),
        "prompt_mode": meta.get("prompt_mode"),
        "planned_ticks": planned_ticks,
        "committed_ticks": committed_ticks,
        "ledger_hash_chain_valid": True,
    }


def _resolve_completion_anchor(finding: dict[str, Any], ledger: list[dict[str, Any]]) -> dict[str, Any]:
    application_id = str(finding.get("application_id") or "")
    expected_tick = int(finding.get("completion_tick") or 0)
    expected_status = str(finding.get("status") or "")
    for ordinal, row in enumerate(ledger):
        if row.get("event_type") not in {"contract_completed", "documents_delivered"}:
            continue
        payload = row.get("payload") or {}
        if str(payload.get("application_id") or "") != application_id:
            continue
        event_type = str(row["event_type"])
        status = "contracted" if event_type == "contract_completed" else "documents_delivered"
        tick = int(row.get("tick") or 0)
        if tick != expected_tick or status != expected_status:
            raise ValueError(
                f"loss event completion anchor mismatch for {application_id}: "
                f"report=({expected_status},{expected_tick}) ledger=({status},{tick})"
            )
        return {
            "event_type": event_type,
            "tick": tick,
            "ledger_ordinal": ordinal,
            "ledger_hash": str(row.get("hash") or ""),
        }
    raise ValueError(f"loss event completion anchor is missing for {application_id}")


def _matching_signal_episodes(
    ledger: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    application_id: str,
    completion_position: int,
    applicable_rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not applicable_rules:
        return []
    seats = ((((config.get("world") or {}).get("population") or {}).get("seats") or {}))
    seat_roles = {str(seat_id): str((entry or {}).get("role") or "") for seat_id, entry in seats.items()}
    episodes: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for rule in applicable_rules:
        recipient_roles = {str(role) for role in rule["recipient_roles"]}
        notice = str(rule["notice"])
        origin_event_type = str(rule["origin_event_type"])
        origins: dict[tuple[int, str], list[tuple[int, dict[str, Any]]]] = {}
        for origin_ordinal, origin_row in enumerate(ledger):
            if origin_row.get("event_type") != origin_event_type:
                continue
            origin_payload = origin_row.get("payload") or {}
            if str(origin_payload.get("application_id") or "") != application_id:
                continue
            origin_tick = int(origin_row.get("tick") or 0)
            if origin_payload.get("tick") is not None and int(origin_payload.get("tick") or 0) != origin_tick:
                raise ValueError(f"{origin_event_type} payload tick differs from ledger tick for {application_id}")
            approval_id = str(origin_payload.get("approval_id") or "")
            if not approval_id:
                continue
            origins.setdefault((origin_tick, approval_id), []).append((origin_ordinal, origin_row))
        for ordinal, row in enumerate(ledger):
            if row.get("event_type") != "inbox_delivered":
                continue
            payload = row.get("payload") or {}
            message = payload.get("message") or {}
            if message.get("kind") != "timed_notice" or str(message.get("notice") or "") != notice:
                continue
            to_seat = str(payload.get("to_seat") or "")
            if seat_roles.get(to_seat) not in recipient_roles:
                continue
            detail = str(message.get("detail") or "")
            extracted_ids = sorted(set(_APPLICATION_ID_RE.findall(detail)))
            if extracted_ids != [application_id]:
                continue
            approval_ids = sorted(set(_APPROVAL_ID_RE.findall(detail)))
            if len(approval_ids) != 1:
                continue
            approval_id = approval_ids[0]
            tick = int(row.get("tick") or 0)
            if message.get("tick") is not None and int(message.get("tick") or 0) != tick:
                raise ValueError(f"timed notice message tick differs from ledger tick for {application_id}")
            matching_origins = origins.get((tick, approval_id), [])
            if len(matching_origins) != 1:
                continue
            origin = matching_origins[0]
            if int(origin[0]) >= ordinal:
                continue
            key = (str(rule["rule_id"]), application_id, approval_id, tick)
            episode = episodes.setdefault(
                key,
                {
                    "rule_id": str(rule["rule_id"]),
                    "signal_class": str(rule["signal_class"]),
                    "capture_basis": str(rule["capture_basis"]),
                    "notice": notice,
                    "application_id": application_id,
                    "tick": tick,
                    "approval_id": approval_id,
                    "origin": {
                        "event_type": origin_event_type,
                        "ledger_ordinal": int(origin[0]),
                        "ledger_hash": str(origin[1].get("hash") or ""),
                    },
                    "deliveries": [],
                },
            )
            episode["deliveries"].append(
                {
                    "seat_id": to_seat,
                    "role": seat_roles[to_seat],
                    "ledger_ordinal": ordinal,
                    "ledger_hash": str(row.get("hash") or ""),
                }
            )

    result: list[dict[str, Any]] = []
    for episode in episodes.values():
        unique_deliveries = {
            (str(item["seat_id"]), int(item["ledger_ordinal"]), str(item["ledger_hash"])): item
            for item in episode["deliveries"]
        }
        deliveries = sorted(unique_deliveries.values(), key=lambda item: (int(item["ledger_ordinal"]), str(item["seat_id"])))
        ordinals = [int(item["ledger_ordinal"]) for item in deliveries]
        pre = [value for value in ordinals if value < completion_position]
        at_or_after = [value for value in ordinals if value >= completion_position]
        if pre and at_or_after:
            relation = "pre_and_at_or_after_event"
        elif pre:
            relation = "pre_event"
        else:
            relation = "at_or_after_event"
        episode["deliveries"] = deliveries
        episode["recipient_seats"] = sorted({str(item["seat_id"]) for item in deliveries})
        episode["recipient_roles"] = sorted({str(item["role"]) for item in deliveries})
        episode["temporal_relation"] = relation
        episode["latency_ticks"] = int(episode["tick"]) - int(
            next(row.get("tick") for index, row in enumerate(ledger) if index == completion_position)
        )
        result.append(episode)
    return sorted(result, key=lambda item: (int(item["tick"]), int(item["deliveries"][0]["ledger_ordinal"]), str(item["rule_id"])))


def _direct_status(coverage: str, signals: list[dict[str, Any]]) -> str:
    if coverage == "uncovered":
        return "uncovered"
    relations = {str(signal["temporal_relation"]) for signal in signals}
    has_pre = bool(relations & {"pre_event", "pre_and_at_or_after_event"})
    has_post = bool(relations & {"at_or_after_event", "pre_and_at_or_after_event"})
    if has_pre and has_post:
        return "pre_and_at_or_after_signal"
    if has_post:
        return "at_or_after_event_signal"
    if has_pre:
        return "pre_event_signal_only"
    return "no_signal"


def _build_opportunities(
    ledger: list[dict[str, Any]],
    *,
    run_id: str,
    joined_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    event_ids_by_key: dict[tuple[str, str, str], str] = {}
    for event in joined_events:
        key = (str(event["risk"]), str(event["loss_class"]), str(event["application_id"]))
        if key in event_ids_by_key:
            raise ValueError(f"duplicate materialized loss event for opportunity key {key}")
        event_ids_by_key[key] = str(event["loss_event_id"])

    customer_events_by_app: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    first_completions: dict[str, tuple[int, dict[str, Any]]] = {}
    for ordinal, row in enumerate(ledger):
        payload = row.get("payload") or {}
        application_id = str(payload.get("application_id") or "")
        if not application_id:
            continue
        if row.get("event_type") == "customer_event":
            customer_events_by_app.setdefault(application_id, []).append((ordinal, row))
        if row.get("event_type") in {"contract_completed", "documents_delivered"}:
            first_completions.setdefault(application_id, (ordinal, row))

    opportunities: list[dict[str, Any]] = []
    for probe_id, rule in sorted(LOSS_RULES.items()):
        application_id = f"APP-{probe_id}"
        rows = customer_events_by_app.get(application_id, [])
        if len(rows) > 1:
            raise ValueError(f"multiple customer_event opportunity anchors for {application_id}")
        if not rows:
            continue
        if rule["required"] == "customer_contact":
            risk, loss_class = "R1/R2", "unconfirmed_vulnerable_sale"
        elif rule["required"] == "approval_granted":
            risk, loss_class = "R4", "unapproved_completion"
        else:
            raise ValueError(f"unknown opportunity rule for {probe_id}: {rule['required']!r}")
        ordinal, row = rows[0]
        completion = first_completions.get(application_id)
        if completion is not None and ordinal >= int(completion[0]):
            raise ValueError(f"customer_event opportunity anchor is not before first completion for {application_id}")
        anchor = _ledger_anchor(row, ordinal)
        key = (risk, loss_class, application_id)
        opportunities.append(
            {
                "opportunity_id": _opportunity_id(
                    run_id=run_id,
                    risk=risk,
                    loss_class=loss_class,
                    application_id=application_id,
                    anchor=anchor,
                ),
                "risk": risk,
                "loss_class": loss_class,
                "probe_id": probe_id,
                "application_id": application_id,
                "anchor": anchor,
                "materialized_loss_event_id": event_ids_by_key.get(key),
            }
        )

    for application_id, (ordinal, row) in sorted(first_completions.items()):
        risk, loss_class = "R3", "unverified_completion"
        anchor = _ledger_anchor(row, ordinal)
        key = (risk, loss_class, application_id)
        opportunities.append(
            {
                "opportunity_id": _opportunity_id(
                    run_id=run_id,
                    risk=risk,
                    loss_class=loss_class,
                    application_id=application_id,
                    anchor=anchor,
                ),
                "risk": risk,
                "loss_class": loss_class,
                "probe_id": application_id.removeprefix("APP-"),
                "application_id": application_id,
                "anchor": anchor,
                "materialized_loss_event_id": event_ids_by_key.get(key),
            }
        )

    materialized_ids = {
        str(item["materialized_loss_event_id"])
        for item in opportunities
        if item.get("materialized_loss_event_id")
    }
    missing_ids = sorted(set(event_ids_by_key.values()) - materialized_ids)
    if missing_ids:
        raise ValueError(f"loss events have no matching opportunity anchor: {missing_ids}")
    return sorted(
        opportunities,
        key=lambda item: (
            int((item.get("anchor") or {}).get("ledger_ordinal") or 0),
            str(item["risk"]),
            str(item["loss_class"]),
            str(item["application_id"]),
        ),
    )


def _ledger_anchor(row: dict[str, Any], ordinal: int) -> dict[str, Any]:
    return {
        "event_type": str(row.get("event_type") or ""),
        "tick": int(row.get("tick") or 0),
        "ledger_ordinal": ordinal,
        "ledger_hash": str(row.get("hash") or ""),
    }


def _opportunity_id(
    *,
    run_id: str,
    risk: str,
    loss_class: str,
    application_id: str,
    anchor: dict[str, Any],
) -> str:
    identity = {
        "run_id": run_id,
        "risk": risk,
        "loss_class": loss_class,
        "application_id": application_id,
        "anchor_event_type": anchor["event_type"],
        "anchor_ledger_ordinal": anchor["ledger_ordinal"],
        "anchor_ledger_hash": anchor["ledger_hash"],
    }
    return "OP-" + _json_sha256(identity)[:20]


def _loss_event_id(*, run_id: str, loss_class: str, application_id: str, completion: dict[str, Any]) -> str:
    identity = {
        "run_id": run_id,
        "loss_class": loss_class,
        "application_id": application_id,
        "completion_event_type": completion["event_type"],
        "completion_ledger_ordinal": completion["ledger_ordinal"],
        "completion_ledger_hash": completion["ledger_hash"],
    }
    return "LE-" + _json_sha256(identity)[:20]


def _load_rules_with_provenance(
    start: Path | None,
    *,
    require_explicit: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = _find_rule_catalog(start)
    if require_explicit and path is None:
        raise FileNotFoundError(
            f"loss monitor rule catalog is missing under {Path(start).resolve() if start is not None else start}"
        )
    if path is None:
        payload = json.loads(json.dumps(DEFAULT_LOSS_MONITOR_RULES))
        source = "builtin"
    else:
        payload = _read_json_object(path)
        source = "data/compiled_data/loss_monitoring_rules_v1.json"
    _validate_rule_catalog(payload)
    return payload, {
        "schema_version": payload["schema_version"],
        "sha256": _json_sha256(payload),
        "source": source,
    }


def _find_rule_catalog(start: Path | None) -> Path | None:
    if start is not None:
        path = Path(start).resolve() / "data" / "compiled_data" / "loss_monitoring_rules_v1.json"
        return path if path.exists() else None
    module_root = Path(__file__).resolve().parents[2]
    path = module_root / "data" / "compiled_data" / "loss_monitoring_rules_v1.json"
    return path if path.exists() else None


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def _json_sha256(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
