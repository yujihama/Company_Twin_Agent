from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class DocumentMeta:
    doc_id: str
    kind: str = ""
    authority: int | None = None
    owner: str = ""
    scope: str = ""
    version: str = ""
    path: Path | None = None


@dataclass(frozen=True)
class SpanDefinition:
    span_id: str
    raw: str
    issue: str = ""
    candidates: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeDefinition:
    probe_id: str
    title: str
    binds: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeatDefinition:
    seat_id: str
    role: str
    description: str = ""


@dataclass
class DesignInputs:
    root: Path
    documents: dict[str, DocumentMeta]
    spans: dict[str, SpanDefinition]
    probes: dict[str, ProbeDefinition]
    seats: dict[str, SeatDefinition]
    world_config_text: str
    retrieval_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    role_cards: dict[str, dict[str, str]] = field(default_factory=dict)
    s0_question_templates: dict[str, tuple[str, ...]] = field(default_factory=dict)
    compiled_artifact_hashes: dict[str, str] = field(default_factory=dict)


class _Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str


class _DocumentItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(pattern=r"^DFH-SAL-\d{3}$")
    kind: str = ""
    authority: int | None = None
    owner: str = ""
    scope: str = ""
    version: str = ""
    path: str | None = None


class _ManifestArtifact(_Artifact):
    corpus_id: str
    documents: list[_DocumentItem]


class _SpanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    span_id: str = Field(pattern=r"^[A-Z]+-\d+[a-z]?$")
    raw: str
    issue: str = ""
    candidates: dict[str, str] = Field(default_factory=dict)


class _SpanRegistryArtifact(_Artifact):
    spans: list[_SpanItem]


class _ProbeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    probe_id: str = Field(pattern=r"^P-\d{2}$")
    title: str
    binds: list[str] = Field(default_factory=list)


class _DeckArtifact(_Artifact):
    probes: list[_ProbeItem]
    events: list[dict[str, Any]] = Field(default_factory=list)


class _SeatItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seat_id: str
    role: str
    description: str = ""


class _PopulationArtifact(_Artifact):
    seats: list[_SeatItem]


class _RetrievalProfilesArtifact(_Artifact):
    profiles: dict[str, dict[str, Any]]


class _RoleCardItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _RoleCardsArtifact(_Artifact):
    role_cards: list[_RoleCardItem]


class _S0TemplateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    span_id: str
    variants: list[str] = Field(min_length=2)


class _S0TemplatesArtifact(_Artifact):
    templates: list[_S0TemplateItem]


INLINE_FIELD_RE = re.compile(r"(?P<key>[A-Za-z_]+):\s*(?P<value>'[^']*'|[^,}]+)")
ID_RE = re.compile(r"id:\s*(DFH-SAL-\d{3})")
SPAN_ID_RE = re.compile(r"^\s*-\s+id:\s*([A-Z]+-\d+[a-z]?)", re.MULTILINE)
PROBE_RE = re.compile(r"^\s*(P-\d{2})\s+(.+):\s*(?:binds\s*\[([^\]]*)\]|(.+))$", re.MULTILINE)
EMP_RE = re.compile(r"(emp-[A-Z])(?:\(([^)]*)\))?")


def load_design(root: Path, *, allow_legacy: bool = False) -> DesignInputs:
    compiled = root / "data" / "compiled_data"
    if _schema_artifacts_available(compiled):
        return _load_schema_artifacts(root, compiled)
    missing = _missing_schema_artifacts(compiled)
    if not allow_legacy:
        raise FileNotFoundError(
            "schema JSON artifacts are required for normal execution; "
            f"missing: {missing}. Use load_legacy_design() only for legacy import."
        )
    return _load_legacy_design(root, compiled)


def load_legacy_design(root: Path) -> DesignInputs:
    """Legacy importer for the pre-schema YAML-like compiled files.

    Normal harness and campaign execution must use schema JSON artifacts. This
    path exists only to migrate older design packs and should not be used in
    acceptance or campaign code.
    """
    return _load_legacy_design(root, root / "data" / "compiled_data")


def _load_legacy_design(root: Path, compiled: Path) -> DesignInputs:
    manifest = (compiled / "00_corpus_manifest_v2.yaml").read_text(encoding="utf-8")
    registry = (compiled / "06_seeded_span_registry_v2.yaml").read_text(encoding="utf-8")
    world_config = (compiled / "world_config_v2.yaml").read_text(encoding="utf-8")
    documents = _parse_manifest(manifest, root)
    spans = _parse_spans(registry)
    probes = _parse_probes(world_config)
    seats = _parse_seats(world_config)
    design = DesignInputs(
        root=root,
        documents=documents,
        spans=spans,
        probes=probes,
        seats=seats,
        world_config_text=world_config,
    )
    validate_design(design)
    return design


SCHEMA_ARTIFACTS = {
    "manifest_v2.json": ("company_twin.manifest.v2", _ManifestArtifact),
    "span_registry_v2.json": ("company_twin.span_registry.v2", _SpanRegistryArtifact),
    "deck_v2.json": ("company_twin.deck.v2", _DeckArtifact),
    "population_v2.json": ("company_twin.population.v2", _PopulationArtifact),
    "retrieval_profiles_v2.json": ("company_twin.retrieval_profiles.v2", _RetrievalProfilesArtifact),
    "role_cards_v2.json": ("company_twin.role_cards.v2", _RoleCardsArtifact),
    "s0_question_templates_v2.json": ("company_twin.s0_question_templates.v2", _S0TemplatesArtifact),
}


def _schema_artifacts_available(compiled: Path) -> bool:
    return all((compiled / filename).exists() for filename in SCHEMA_ARTIFACTS)


def _missing_schema_artifacts(compiled: Path) -> list[str]:
    return sorted(filename for filename in SCHEMA_ARTIFACTS if not (compiled / filename).exists())


def _load_schema_artifacts(root: Path, compiled: Path) -> DesignInputs:
    manifest: _ManifestArtifact = _load_artifact(compiled / "manifest_v2.json", _ManifestArtifact, "company_twin.manifest.v2")
    registry: _SpanRegistryArtifact = _load_artifact(compiled / "span_registry_v2.json", _SpanRegistryArtifact, "company_twin.span_registry.v2")
    deck: _DeckArtifact = _load_artifact(compiled / "deck_v2.json", _DeckArtifact, "company_twin.deck.v2")
    population: _PopulationArtifact = _load_artifact(compiled / "population_v2.json", _PopulationArtifact, "company_twin.population.v2")
    retrieval: _RetrievalProfilesArtifact = _load_artifact(compiled / "retrieval_profiles_v2.json", _RetrievalProfilesArtifact, "company_twin.retrieval_profiles.v2")
    role_cards: _RoleCardsArtifact = _load_artifact(compiled / "role_cards_v2.json", _RoleCardsArtifact, "company_twin.role_cards.v2")
    s0_templates: _S0TemplatesArtifact = _load_artifact(compiled / "s0_question_templates_v2.json", _S0TemplatesArtifact, "company_twin.s0_question_templates.v2")

    documents = {
        item.doc_id: DocumentMeta(
            doc_id=item.doc_id,
            kind=item.kind,
            authority=item.authority,
            owner=item.owner,
            scope=item.scope,
            version=item.version,
            path=(root / item.path) if item.path else None,
        )
        for item in manifest.documents
    }
    spans = {
        item.span_id: SpanDefinition(span_id=item.span_id, raw=item.raw, issue=item.issue, candidates=dict(item.candidates))
        for item in registry.spans
    }
    probes = {
        item.probe_id: ProbeDefinition(probe_id=item.probe_id, title=item.title, binds=tuple(item.binds))
        for item in deck.probes
    }
    seats = {
        item.seat_id: SeatDefinition(seat_id=item.seat_id, role=item.role, description=item.description)
        for item in population.seats
    }
    artifact_hashes = {filename: _file_sha256(compiled / filename) for filename in SCHEMA_ARTIFACTS}
    design = DesignInputs(
        root=root,
        documents=documents,
        spans=spans,
        probes=probes,
        seats=seats,
        world_config_text=(compiled / "world_config_v2.yaml").read_text(encoding="utf-8"),
        retrieval_profiles=retrieval.profiles,
        role_cards={item.role: {"path": item.path, "sha256": item.sha256} for item in role_cards.role_cards},
        s0_question_templates={item.span_id: tuple(item.variants) for item in s0_templates.templates},
        compiled_artifact_hashes=artifact_hashes,
    )
    validate_design(design)
    return design


def _load_artifact(path: Path, model: type[BaseModel], schema_version: str):
    payload = model.model_validate_json(path.read_text(encoding="utf-8"))
    if getattr(payload, "schema_version") != schema_version:
        raise ValueError(f"{path.name} schema_version must be {schema_version}, got {payload.schema_version}")
    return payload


KNOWN_ROLES = {"sales", "manager", "application", "second_line", "audit", "unknown"}


def validate_design(design: DesignInputs) -> None:
    """Hard validation of executable compiled inputs.

    Normal execution reads schema-validated JSON artifacts. The regex parsers
    below are retained only as a legacy importer when those artifacts are absent.
    """
    problems: list[str] = []
    for probe_id, probe in design.probes.items():
        for span_id in probe.binds:
            if span_id not in design.spans:
                problems.append(f"probe {probe_id} binds undefined span {span_id}")
    missing = [doc_id for doc_id, meta in design.documents.items() if meta.path is None]
    if missing:
        problems.append(f"documents without raw files: {missing[:5]}")
    for seat_id, seat in design.seats.items():
        if seat.role not in KNOWN_ROLES:
            problems.append(f"seat {seat_id} has unknown role {seat.role}")
    for span_id, span in design.spans.items():
        if not span.raw.strip():
            problems.append(f"span {span_id} has empty registry block")
    if design.retrieval_profiles:
        missing_profiles = sorted({seat.role for seat in design.seats.values()} - set(design.retrieval_profiles))
        if missing_profiles:
            problems.append(f"retrieval profiles missing roles: {missing_profiles}")
    if design.role_cards:
        missing_cards = sorted({seat.role for seat in design.seats.values()} - set(design.role_cards))
        if missing_cards:
            problems.append(f"role cards missing roles: {missing_cards}")
        for role, meta in design.role_cards.items():
            path = design.root / meta.get("path", "")
            if not path.exists():
                problems.append(f"role card for {role} missing: {meta.get('path')}")
                continue
            actual = _file_sha256(path)
            if actual != meta.get("sha256"):
                problems.append(f"role card hash mismatch for {role}")
    if design.s0_question_templates:
        missing_templates = sorted(set(design.spans) - set(design.s0_question_templates))
        extra_templates = sorted(set(design.s0_question_templates) - set(design.spans))
        if missing_templates:
            problems.append(f"s0 templates missing spans: {missing_templates}")
        if extra_templates:
            problems.append(f"s0 templates reference unknown spans: {extra_templates}")
        for span_id, variants in design.s0_question_templates.items():
            if len(variants) < 2 or any(not variant.strip() for variant in variants):
                problems.append(f"s0 template {span_id} must have at least two non-empty variants")
    if problems:
        raise ValueError("compiled design inputs failed validation: " + "; ".join(problems))


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_manifest(text: str, root: Path) -> dict[str, DocumentMeta]:
    raw_paths = _index_raw_paths(root)
    docs: dict[str, DocumentMeta] = {}
    for line in text.splitlines():
        if not line.lstrip().startswith("- {id:"):
            continue
        doc_id_match = ID_RE.search(line)
        if not doc_id_match:
            continue
        doc_id = doc_id_match.group(1)
        fields = {match.group("key"): _clean_value(match.group("value")) for match in INLINE_FIELD_RE.finditer(line)}
        authority = None
        if fields.get("authority", "").isdigit():
            authority = int(fields["authority"])
        docs[doc_id] = DocumentMeta(
            doc_id=doc_id,
            kind=fields.get("kind", ""),
            authority=authority,
            owner=fields.get("owner", ""),
            scope=fields.get("scope", ""),
            version=fields.get("ver", ""),
            path=raw_paths.get(doc_id),
        )
    return docs


def _index_raw_paths(root: Path) -> dict[str, Path]:
    data_root = root / "data" / "raw_data"
    if not data_root.exists():
        return {}
    paths: dict[str, Path] = {}
    for path in data_root.rglob("*"):
        if not path.is_file():
            continue
        match = re.match(r"(DFH-SAL-\d{3})_", path.name)
        if match:
            paths[match.group(1)] = path
    return paths


def _parse_spans(text: str) -> dict[str, SpanDefinition]:
    matches = list(SPAN_ID_RE.finditer(text))
    spans: dict[str, SpanDefinition] = {}
    for idx, match in enumerate(matches):
        span_id = match.group(1)
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        spans[span_id] = SpanDefinition(
            span_id=span_id,
            raw=block,
            issue=_first_field(block, "issue") or _first_field(block, "finding"),
            candidates=_parse_candidates(block),
        )
    return spans


def _parse_candidates(block: str) -> dict[str, str]:
    line = _first_field(block, "candidates")
    if not line:
        return {}
    inner = line.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1]
    parts = re.split(r",\s*(?=C\d+:)", inner)
    parsed: dict[str, str] = {}
    for part in parts:
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _first_field(block: str, field_name: str) -> str:
    pattern = re.compile(rf"^\s*{re.escape(field_name)}:\s*(.+)$", re.MULTILINE)
    match = pattern.search(block)
    return match.group(1).strip() if match else ""


def _parse_probes(text: str) -> dict[str, ProbeDefinition]:
    probes: dict[str, ProbeDefinition] = {}
    for match in PROBE_RE.finditer(text):
        probe_id = match.group(1)
        title = match.group(2).strip()
        binds_text = match.group(3) or ""
        binds = tuple(_normalize_bind_id(item) for item in _split_csvish(binds_text) if item.strip())
        probes[probe_id] = ProbeDefinition(probe_id=probe_id, title=title, binds=binds)
    return probes


def _parse_seats(text: str) -> dict[str, SeatDefinition]:
    seats: dict[str, SeatDefinition] = {}
    role_by_emp = {
        "emp-A": "sales",
        "emp-B": "sales",
        "emp-F": "sales",
        "emp-G": "sales",
        "emp-C": "application",
        "emp-M": "manager",
        "emp-Q": "second_line",
    }
    for match in EMP_RE.finditer(text):
        seat_id = match.group(1)
        seats[seat_id] = SeatDefinition(
            seat_id=seat_id,
            role=role_by_emp.get(seat_id, "unknown"),
            description=match.group(2) or "",
        )
    seats.setdefault("audit-in-world", SeatDefinition("audit-in-world", "audit", "world-visible audit actor"))
    return seats


def _split_csvish(value: str) -> Iterable[str]:
    current: list[str] = []
    depth = 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        if char == "," and depth == 0:
            yield "".join(current).strip()
            current = []
        else:
            current.append(char)
    if current:
        yield "".join(current).strip()


def _normalize_bind_id(value: str) -> str:
    value = value.strip()
    value = re.sub(r"\(.+\)$", "", value).strip()
    return value


def _clean_value(value: str) -> str:
    return value.strip().strip("'").strip('"').strip()
