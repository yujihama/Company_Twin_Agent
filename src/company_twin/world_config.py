from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .deck import build_customer_deck
from .design_loader import DesignInputs, stable_text_sha256
from .env import normalize_openrouter_model


class RuntimeWorldConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str = Field(pattern=r"^company_twin\.world_config\.v2$")
    stage: str
    anchor: bool
    world: dict[str, Any]
    runtime_delta: dict[str, Any]
    model: dict[str, Any]


DEFAULT_KNOBS = {
    "K-checksheet-gate": False,
    "K-qualification-gate": False,
    "K-sod-gate": False,
    "K-material-picker": False,
    "K-completion-gate": False,
}


def build_world_config(
    design: DesignInputs,
    *,
    stage: str,
    model: str | None,
    seed: int,
    ticks: int,
    anchor: bool = False,
    knobs: dict[str, bool] | None = None,
    probe_id: str | None = None,
    seat_id: str | None = None,
    mutations: list[dict[str, Any]] | None = None,
    executed_s0_rows: int | None = None,
    d4_enabled: bool = True,
    model_bindings: dict[str, str] | None = None,
    scc_switch_tick: int | None = None,
    timed_notice_recipients: list[str] | None = None,
    seats_subset: list[str] | None = None,
) -> dict[str, Any]:
    normalized_knobs = {**DEFAULT_KNOBS, **(knobs or {})}
    model_name = normalize_openrouter_model(model)
    seats = _seat_configs(design, model_name, d4_enabled=d4_enabled, model_bindings=model_bindings)
    requested_seats = _normalize_seats_subset(seats_subset)
    if requested_seats is not None:
        unknown = [seat_id_ for seat_id_ in requested_seats if seat_id_ not in seats]
        if unknown:
            raise ValueError(f"unknown seats in seats_subset: {', '.join(unknown)}")
        seats = {seat_id_: seats[seat_id_] for seat_id_ in requested_seats}
    deck = [event.to_dict() for event in build_customer_deck(design, include_routine=True)]
    normalized_mutations = list(mutations or [])
    raw_corpus_hash = _raw_corpus_hash(design)
    mutation_hash = _json_hash(normalized_mutations)
    effective_corpus_hash = raw_corpus_hash if not normalized_mutations else _json_hash({"raw_corpus_hash": raw_corpus_hash, "mutation_hash": mutation_hash})
    effective_scc_switch_tick = None if anchor else (scc_switch_tick if scc_switch_tick is not None else min(30, ticks))
    if effective_scc_switch_tick is not None:
        effective_scc_switch_tick = min(max(int(effective_scc_switch_tick), 1), ticks)
    deadline_tick = min(20, ticks)
    approval_due_ticks = 2
    absence_ticks = [tick for tick in [23, 24] if tick <= ticks]
    absence = {"emp-M": absence_ticks} if "emp-M" in seats else {}
    notice_source = timed_notice_recipients if timed_notice_recipients is not None else _default_timed_notice_recipients(design)
    notice_recipients = sorted(set(notice_source))
    approval_notice_recipients = sorted(set(_approval_notice_recipients(design)))
    config = {
        "schema_version": "company_twin.world_config.v2",
        "stage": stage,
        "anchor": anchor,
        "world": {
            "corpus": {
                "corpus_id": "dfh_sales_v2",
                "manifest_hash": design.compiled_artifact_hashes.get("manifest_v2.json") or _text_hash(_read(design.root / "data" / "compiled_data" / "00_corpus_manifest_v2.yaml")),
                "raw_corpus_hash": raw_corpus_hash,
                "span_registry_hash": design.compiled_artifact_hashes.get("span_registry_v2.json") or _text_hash(_read(design.root / "data" / "compiled_data" / "06_seeded_span_registry_v2.yaml")),
                "deck_artifact_hash": design.compiled_artifact_hashes.get("deck_v2.json", ""),
                "retrieval_profiles_hash": design.compiled_artifact_hashes.get("retrieval_profiles_v2.json", ""),
                "role_cards_hash": design.compiled_artifact_hashes.get("role_cards_v2.json", ""),
                "s0_question_templates_hash": design.compiled_artifact_hashes.get("s0_question_templates_v2.json", ""),
                "mutations": normalized_mutations,
                "mutation_count": len(normalized_mutations),
                "mutation_hash": mutation_hash,
                "effective_corpus_hash": effective_corpus_hash,
                "document_count": len(design.documents) + _document_delta(normalized_mutations),
            },
            "kernel_profile": {
                "name": "anchor_erp_standard" if anchor else "erp_standard",
                "knobs": normalized_knobs,
                "state_machine": ["draft", "application_received", "identity_verified", "review_linked", "contracted", "documents_delivered"],
                "hard_guards": ["ekyc_completed", "consent_log_id", "sanctions_non_hit"],
            },
            "population": {
                "seats": seats,
                "binding": {seat_id_: seat["model_binding"] for seat_id_, seat in seats.items()},
                "absence": absence,
                "tick_budget": {seat_id_: seat["tick_budget"] for seat_id_, seat in seats.items()},
            },
            "retrieval_profiles": design.retrieval_profiles or default_retrieval_profiles(),
            "deck": {
                "deck_id": "deck_v2",
                "deck_hash": _json_hash(deck),
                "events": deck,
                "routine_count": sum(1 for event in deck if event["routine"]),
                "probe_count": sum(1 for event in deck if not event["routine"]),
            },
            "schedule": {
                "tick_unit": "half_business_day",
                "ticks": ticks,
                "daily_inbox_ticks": list(range(1, ticks + 1)),
                "campaign_deadline_tick": deadline_tick,
                "manager_absence_ticks": absence_ticks,
                "month_end_tick": ticks,
                "scc_switch_enabled": not anchor,
                "scc_switch_tick": effective_scc_switch_tick,
                "timed_notice_recipients": notice_recipients,
                "approval_due_ticks": approval_due_ticks,
                "approval_notice_recipients": approval_notice_recipients,
            },
            "seeds": {
                "retrieval": seed,
                "deck": seed,
                "persona": seed,
                "resolver": seed,
            },
        },
        "runtime_delta": {
            "probe_id": probe_id,
            "seat_id": seat_id,
            "executed_s0_rows": executed_s0_rows,
            "d4_enabled": d4_enabled,
            "seats_subset": requested_seats,
        },
        "model": {
            "default": model_name,
            "provider": "openrouter",
            "family": "qwen",
        },
    }
    validate_world_config_schema(config)
    return config


def _normalize_seats_subset(seats_subset: list[str] | None) -> list[str] | None:
    if seats_subset is None:
        return None
    normalized = sorted({str(seat_id).strip() for seat_id in seats_subset if str(seat_id).strip()})
    if not normalized:
        raise ValueError("seats_subset must include at least one seat")
    return normalized


def default_retrieval_profiles() -> dict[str, Any]:
    return {
        "sales": {
            "top_k": 5,
            "index_kinds": ["マニュアル", "商品別マニュアル", "ワークブック"],
            "boost_sections": {"現場FAQ": 8.0, "現場判断事例": 9.0},
            "authority_friction": {"規程": -1.0},
            "version_visibility": "current_plus_role_stale_021_045",
        },
        "manager": {
            "top_k": 5,
            "index_kinds": ["規程", "マニュアル", "ワークブック"],
            "boost_sections": {"承認": 5.0, "差戻": 4.0},
            "authority_friction": {},
            "version_visibility": "current",
        },
        "application": {
            "top_k": 5,
            "index_kinds": ["マニュアル", "ワークブック"],
            "boost_sections": {"申込": 6.0, "本人確認": 6.0},
            "authority_friction": {},
            "version_visibility": "current",
        },
        "second_line": {
            "top_k": 6,
            "index_kinds": ["規程", "マニュアル", "ワークブック"],
            "boost_sections": {"統制": 6.0, "第二線": 6.0},
            "authority_friction": {},
            "version_visibility": "current",
        },
        "audit": {
            "top_k": 8,
            "index_kinds": ["規程", "マニュアル", "ワークブック"],
            "boost_sections": {"証跡": 6.0, "モニタリング": 6.0},
            "authority_friction": {},
            "version_visibility": "current",
        },
    }


def _seat_configs(design: DesignInputs, model_name: str, *, d4_enabled: bool = True, model_bindings: dict[str, str] | None = None) -> dict[str, Any]:
    from .tools import tools_for_role  # local import: avoids world_config<->tools<->corpus cycle

    budgets = {"sales": 14, "manager": 10, "application": 12, "second_line": 10, "audit": 8}
    result: dict[str, Any] = {}
    for seat_id, seat in sorted(design.seats.items()):
        card = _role_card_meta(design.root, seat.role)
        bound_model = normalize_openrouter_model((model_bindings or {}).get(seat_id) or model_name)
        result[seat_id] = {
            "role": seat.role,
            "description": seat.description,
            "role_card_path": card["path"],
            "role_card_sha256": card["sha256"],
            "tick_budget": budgets.get(seat.role, 8),
            "model_binding": bound_model,
            "store_enabled": d4_enabled,
            "tools": list(tools_for_role(seat.role, d4_enabled=d4_enabled)),
        }
    return result


def _default_timed_notice_recipients(design: DesignInputs) -> list[str]:
    notice_roles = {"sales", "application", "second_line"}
    return [seat_id for seat_id, seat in sorted(design.seats.items()) if seat.role in notice_roles]


def _approval_notice_recipients(design: DesignInputs) -> list[str]:
    quality_roles = {"second_line", "audit"}
    return [seat_id for seat_id, seat in sorted(design.seats.items()) if seat.role in quality_roles]


def _document_delta(mutations: list[dict[str, Any]]) -> int:
    return sum(int(item.get("document_delta") or 0) for item in mutations)


def _role_card_meta(root: Path, role: str) -> dict[str, str]:
    path = root / "data" / "design" / "role_cards" / f"{role}.md"
    if not path.exists():
        return {"path": "", "sha256": ""}
    return {"path": str(path.relative_to(root)), "sha256": stable_text_sha256(path)}


def assert_world_config_complete(config: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    try:
        validate_world_config_schema(config)
    except ValueError as exc:
        failures.append(str(exc))
    world = config.get("world") or {}
    required_world = ["corpus", "kernel_profile", "population", "retrieval_profiles", "deck", "schedule", "seeds"]
    for key in required_world:
        if key not in world:
            failures.append(f"missing world.{key}")
    if not (world.get("deck") or {}).get("events"):
        failures.append("world.deck.events is empty")
    if not (world.get("population") or {}).get("seats"):
        failures.append("world.population.seats is empty")
    schedule = world.get("schedule") or {}
    for key in ("ticks", "daily_inbox_ticks", "campaign_deadline_tick", "month_end_tick", "scc_switch_tick"):
        if key not in schedule:
            failures.append(f"missing world.schedule.{key}")
    seeds = world.get("seeds") or {}
    for key in ("retrieval", "deck", "persona", "resolver"):
        if key not in seeds:
            failures.append(f"missing world.seeds.{key}")
    return failures


def validate_world_config_schema(config: dict[str, Any]) -> RuntimeWorldConfig:
    try:
        return RuntimeWorldConfig.model_validate(config)
    except Exception as exc:
        raise ValueError(f"runtime world_config schema validation failed: {exc}") from exc


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _raw_corpus_hash(design: DesignInputs) -> str:
    from .corpus import RECORD_STANDARD_DOC_ID, RECORD_STANDARD_TEXT  # local import: avoids corpus<->world_config cycle

    entries = []
    for doc_id, meta in sorted(design.documents.items()):
        digest = ""
        if meta.path and meta.path.exists():
            digest = hashlib.sha256(meta.path.read_bytes()).hexdigest()
        entries.append({"doc_id": doc_id, "version": meta.version, "sha256": digest})
    # The record-writing-standard document (Corpus._record_standard_document)
    # is injected at the Corpus layer, not design.documents, so it would
    # otherwise never move this hash even though it is real content every
    # world now contains. Folding its content hash in here keeps
    # raw_corpus_hash an honest fingerprint of "what a seat can actually
    # read" without disturbing the manifest-tracked document count.
    entries.append({"doc_id": RECORD_STANDARD_DOC_ID, "version": "1.1", "sha256": hashlib.sha256(RECORD_STANDARD_TEXT.encode("utf-8")).hexdigest()})
    return _json_hash(entries)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
