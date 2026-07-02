from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .deck import build_customer_deck
from .design_loader import DesignInputs
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
) -> dict[str, Any]:
    normalized_knobs = {**DEFAULT_KNOBS, **(knobs or {})}
    model_name = normalize_openrouter_model(model)
    seats = _seat_configs(design, model_name)
    deck = [event.to_dict() for event in build_customer_deck(design, include_routine=True)]
    config = {
        "schema_version": "company_twin.world_config.v2",
        "stage": stage,
        "anchor": anchor,
        "world": {
            "corpus": {
                "corpus_id": "dfh_sales_v2",
                "manifest_hash": _text_hash(_read(design.root / "data" / "compiled_data" / "00_corpus_manifest_v2.yaml")),
                "raw_corpus_hash": _raw_corpus_hash(design),
                "span_registry_hash": _text_hash(_read(design.root / "data" / "compiled_data" / "06_seeded_span_registry_v2.yaml")),
                "mutations": mutations or [],
                "document_count": len(design.documents),
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
                "absence": {"emp-M": [23, 24]},
                "tick_budget": {seat_id_: seat["tick_budget"] for seat_id_, seat in seats.items()},
            },
            "retrieval_profiles": default_retrieval_profiles(),
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
                "campaign_deadline_tick": min(20, ticks),
                "manager_absence_ticks": [tick for tick in [23, 24] if tick <= ticks],
                "month_end_tick": ticks,
                "scc_switch_enabled": not anchor,
                "scc_switch_tick": None if anchor else min(30, ticks),
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
        },
        "model": {
            "default": model_name,
            "provider": "openrouter",
            "family": "qwen",
        },
    }
    validate_world_config_schema(config)
    return config


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


def _seat_configs(design: DesignInputs, model_name: str) -> dict[str, Any]:
    budgets = {"sales": 8, "manager": 7, "application": 8, "second_line": 7, "audit": 6}
    result: dict[str, Any] = {}
    for seat_id, seat in sorted(design.seats.items()):
        result[seat_id] = {
            "role": seat.role,
            "description": seat.description,
            "role_card": _role_card_entry(design.root, seat.role),
            "tick_budget": budgets.get(seat.role, 8),
            "model_binding": model_name,
            "store_enabled": seat.role in {"sales", "manager", "second_line"},
            "tools": _tools_for_role(seat.role),
        }
    return result


def _tools_for_role(role: str) -> list[str]:
    base = ["search_corpus", "read_document", "record_interpretation_basis", "note_to_self", "recall_private_memory", "send_chat"]
    if role == "sales":
        return base + ["record_customer_contact", "request_approval"]
    if role == "manager":
        return base + ["approve_application", "return_application"]
    if role == "application":
        return base + ["submit_application", "verify_identity", "link_review", "complete_contract", "deliver_documents"]
    if role == "second_line":
        return base + ["approve_application", "return_application"]
    return base


def _role_card_entry(root: Path, role: str) -> dict[str, str]:
    path = root / "data" / "design" / "role_cards" / f"{role}.md"
    text = path.read_text(encoding="utf-8") if path.exists() else f"役割: {role}"
    rel_path = path.relative_to(root).as_posix() if path.exists() else ""
    return {
        "role_card_id": role,
        "path": rel_path,
        "sha256": _text_hash(text),
        "text": text,
    }


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
    entries = []
    for doc_id, meta in sorted(design.documents.items()):
        digest = ""
        if meta.path and meta.path.exists():
            digest = hashlib.sha256(meta.path.read_bytes()).hexdigest()
        entries.append({"doc_id": doc_id, "version": meta.version, "sha256": digest})
    return _json_hash(entries)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
