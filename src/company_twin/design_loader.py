from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


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


INLINE_FIELD_RE = re.compile(r"(?P<key>[A-Za-z_]+):\s*(?P<value>'[^']*'|[^,}]+)")
ID_RE = re.compile(r"id:\s*(DFH-SAL-\d{3})")
SPAN_ID_RE = re.compile(r"^\s*-\s+id:\s*([A-Z]+-\d+[a-z]?)", re.MULTILINE)
PROBE_RE = re.compile(r"^\s*(P-\d{2})\s+(.+):\s*(?:binds\s*\[([^\]]*)\]|(.+))$", re.MULTILINE)
EMP_RE = re.compile(r"(emp-[A-Z])(?:\(([^)]*)\))?")


def load_design(root: Path) -> DesignInputs:
    compiled = root / "data" / "compiled_data"
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


KNOWN_ROLES = {"sales", "manager", "application", "second_line", "audit", "unknown"}


def validate_design(design: DesignInputs) -> None:
    """Hard validation of compiled inputs (partial answer to the schema-artifact
    reviewer blocker): undefined binds, pathless docs, and unknown roles fail
    loudly instead of being silently dropped downstream."""
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
    if problems:
        raise ValueError("compiled design inputs failed validation: " + "; ".join(problems))


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
