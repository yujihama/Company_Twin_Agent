from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .corpus import Corpus, CorpusDocument
from .design_loader import DocumentMeta
from .world_config import _json_hash


CATALOG_PATH = Path("data") / "compiled_data" / "mutation_operators_v1.json"

# The fixed role set the world corpus/retrieval profiles are defined over
# (world_config.default_retrieval_profiles / harness.kernel_profile). A
# document (or patch target) with visible_roles=None is readable by every
# role (corpus.Corpus.readable_by), so a circulation announcement for such a
# mutation is likewise addressed to every role -- this constant is what
# resolves that "everyone" case to a concrete recipient list.
ALL_ROLES: tuple[str, ...] = ("sales", "manager", "application", "second_line", "audit")

LEAK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bAMB-\d+[A-Za-z0-9_-]*\b", "seeded span id"),
    (r"\bCONTRA-\d+[A-Za-z0-9_-]*\b", "seeded span id"),
    (r"\bSTR-\d+[A-Za-z0-9_-]*\b", "seeded span id"),
    (r"\bSCC-\d+[A-Za-z0-9_-]*\b", "seeded span id"),
    (r"\bprobe\b", "probe vocabulary"),
    (r"\bspan\b", "span vocabulary"),
    (r"\bmutation\b", "mutation vocabulary"),
    (r"\bexperiment\b", "experiment vocabulary"),
    (r"\bfuzz", "fuzzing vocabulary"),
    (r"\bsearch\s+key\b", "search key vocabulary"),
)


@dataclass(frozen=True)
class MutationApplicationResult:
    corpus: Corpus
    applied: list[dict[str, Any]]
    before_hash: str
    after_hash: str
    mutation_hash: str


def load_mutation_catalog(root: Path) -> dict[str, dict[str, Any]]:
    path = root / CATALOG_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "company_twin.mutation_operators.v1":
        raise ValueError(f"unexpected mutation catalog schema: {payload.get('schema_version')!r}")
    entries = payload.get("operators")
    if not isinstance(entries, list):
        raise ValueError("mutation catalog operators must be a list")
    catalog: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("mutation catalog entries must be objects")
        mutation_id = str(entry.get("mutation_id") or "")
        if not mutation_id:
            raise ValueError("mutation catalog entry missing mutation_id")
        if mutation_id in catalog:
            raise ValueError(f"duplicate mutation_id: {mutation_id}")
        catalog[mutation_id] = entry
    return catalog


def mutation_specs_from_values(root: Path, values: list[str] | None) -> list[dict[str, Any]]:
    catalog = load_mutation_catalog(root)
    specs: list[dict[str, Any]] = []
    for value in values or []:
        value = value.strip()
        if not value:
            continue
        if value.startswith("{"):
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError("--mutation JSON must be an object")
            specs.append(parsed)
            continue
        if value not in catalog:
            raise ValueError(f"unknown mutation_id: {value}")
        specs.append(catalog[value])
    return specs


def apply_corpus_mutations(corpus: Corpus, specs: list[dict[str, Any]] | None) -> MutationApplicationResult:
    documents = {
        doc_id: CorpusDocument(meta=doc.meta, text=doc.text, visible_roles=doc.visible_roles)
        for doc_id, doc in corpus.documents.items()
    }
    before_hash = corpus_fingerprint(corpus)
    applied: list[dict[str, Any]] = []
    for spec in specs or []:
        entry = _apply_one(documents, spec)
        applied.append(entry)
    mutated = Corpus(documents, corpus.retrieval_profiles)
    after_hash = corpus_fingerprint(mutated)
    return MutationApplicationResult(
        corpus=mutated,
        applied=applied,
        before_hash=before_hash,
        after_hash=after_hash,
        mutation_hash=_json_hash(applied),
    )


def lint_mutation_specs(specs: list[dict[str, Any]]) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for spec in specs:
        surface = _visible_text(spec)
        mutation_id = str(spec.get("mutation_id") or "<unknown>")
        for pattern, label in LEAK_PATTERNS:
            if re.search(pattern, surface, flags=re.IGNORECASE):
                failures.append({"mutation_id": mutation_id, "label": label, "pattern": pattern})
    return failures


def lint_mutation_catalog(root: Path) -> list[dict[str, str]]:
    return lint_mutation_specs(list(load_mutation_catalog(root).values()))


def corpus_fingerprint(corpus: Corpus) -> str:
    rows = []
    for doc_id, doc in sorted(corpus.documents.items()):
        rows.append(
            {
                "doc_id": doc_id,
                "version": doc.meta.version,
                "kind": doc.meta.kind,
                "visible_roles": list(doc.visible_roles) if doc.visible_roles is not None else None,
                "sha256": _json_hash({"text": doc.text}),
            }
        )
    return _json_hash(rows)


def build_delta_one_pair_manifest(*, root: Path, mutation_ids: list[str], seeds: int = 5) -> dict[str, Any]:
    if seeds < 1:
        raise ValueError("seeds must be >= 1")
    specs = mutation_specs_from_values(root, mutation_ids)
    pairs: list[dict[str, Any]] = []
    for spec in specs:
        mutation_id = str(spec["mutation_id"])
        for seed in range(seeds):
            pair_id = f"pair_{mutation_id}_seed{seed}"
            pairs.append(
                {
                    "pair_id": pair_id,
                    "delta": "world.corpus.mutations",
                    "seed": seed,
                    "control": {"seed": seed, "mutations": [], "knobs": {}},
                    "treatment": {"seed": seed, "mutations": [mutation_id], "knobs": {}},
                    "shared": {"deck_seed": seed, "persona_seed": seed, "retrieval_seed": seed, "resolver_seed": seed},
                }
            )
    return {
        "schema_version": "company_twin.control_pairs.v1",
        "note": "Manifest only: execution remains live-only through s1/s2/campaign commands.",
        "k": seeds,
        "pair_count": len(pairs),
        "pairs": pairs,
    }


def _apply_one(documents: dict[str, CorpusDocument], spec: dict[str, Any]) -> dict[str, Any]:
    mutation_id = _required(spec, "mutation_id")
    operator = _required(spec, "operator")
    action = _required(spec, "action")
    if action == "inject_document":
        return _inject_document(documents, spec, mutation_id=mutation_id, operator=operator)
    if action == "patch_document":
        return _patch_document(documents, spec, mutation_id=mutation_id, operator=operator)
    raise ValueError(f"unsupported mutation action for {mutation_id}: {action}")


def _inject_document(documents: dict[str, CorpusDocument], spec: dict[str, Any], *, mutation_id: str, operator: str) -> dict[str, Any]:
    doc_id = _required(spec, "doc_id")
    if doc_id in documents:
        raise ValueError(f"mutation {mutation_id} would overwrite existing doc_id {doc_id}")
    text = _required(spec, "text")
    visible_roles = tuple(str(role) for role in spec.get("visible_roles") or [])
    if not visible_roles:
        raise ValueError(f"mutation {mutation_id} must declare visible_roles")
    _raise_on_leak(spec)
    meta = DocumentMeta(
        doc_id=doc_id,
        kind=str(spec.get("kind") or "runtime_notice"),
        authority=spec.get("authority"),
        owner=str(spec.get("owner") or "sales_control"),
        scope=str(spec.get("scope") or "runtime"),
        version=str(spec.get("version") or "1.1"),
        path=None,
    )
    documents[doc_id] = CorpusDocument(meta=meta, text=text, visible_roles=visible_roles)
    return {
        "mutation_id": mutation_id,
        "operator": operator,
        "action": "inject_document",
        "doc_id": doc_id,
        "visible_roles": list(visible_roles),
        "content_sha256": _json_hash({"text": text}),
        "document_delta": 1,
        "circulation_digest": circulation_digest_text(text),
        "circulation_message": circulation_message_text(text),
    }


def _patch_document(documents: dict[str, CorpusDocument], spec: dict[str, Any], *, mutation_id: str, operator: str) -> dict[str, Any]:
    target_doc_id = _required(spec, "target_doc_id")
    if target_doc_id not in documents:
        raise ValueError(f"mutation {mutation_id} target missing: {target_doc_id}")
    append_text = _required(spec, "append_text")
    _raise_on_leak(spec)
    original = documents[target_doc_id]
    patched_text = f"{original.text.rstrip()}\n\n{append_text.strip()}\n"
    documents[target_doc_id] = CorpusDocument(meta=original.meta, text=patched_text, visible_roles=original.visible_roles)
    # A patch target with visible_roles=None (e.g. DFH-SAL-045) is readable by
    # every role (corpus.Corpus.readable_by); resolve that to the concrete
    # role list here so a circulation announcement has an explicit recipient
    # set to work from, exactly like an injected document already has.
    visible_roles = list(original.visible_roles) if original.visible_roles is not None else list(ALL_ROLES)
    return {
        "mutation_id": mutation_id,
        "operator": operator,
        "action": "patch_document",
        "doc_id": target_doc_id,
        "visible_roles": visible_roles,
        "content_sha256": _json_hash({"append_text": append_text}),
        "before_sha256": _json_hash({"text": original.text}),
        "after_sha256": _json_hash({"text": patched_text}),
        "document_delta": 0,
        "circulation_digest": circulation_digest_text(append_text),
        "circulation_message": circulation_message_text(append_text),
    }


def _required(spec: dict[str, Any], key: str) -> str:
    value = str(spec.get(key) or "")
    if not value:
        raise ValueError(f"mutation spec missing {key}")
    return value


def _visible_text(spec: dict[str, Any]) -> str:
    return "\n".join(str(spec.get(key) or "") for key in ("text", "append_text"))


def _raise_on_leak(spec: dict[str, Any]) -> None:
    failures = lint_mutation_specs([spec])
    if failures:
        first = failures[0]
        raise ValueError(f"world-visible mutation text leaks {first['label']} in {first['mutation_id']}")


# ---------------------------------------------------------------------------
# Diegetic notice circulation (MASTER_DESIGN section 8.2/17.13/17.x): a
# natural-business-language circulation of a runtime-injected/patched
# document, derived from the mutation's OWN catalog text -- never authored
# fresh, so there is nothing here that isn't already lint-checked catalog
# content.
#
# `circulation_digest_text` (era-5, title-only, kept for backward
# compatibility with older sealed bundles) only ANNOUNCES that a notice
# exists ("回覧します: 「<件名>」"); it never repeats the notice's substantive
# content. A raw-data audit across five era-5 contradict seeds plus
# clarify/dangling runs found this was never enough to draw a single seat to
# read the underlying document (zero read_document/basis-citation hits for
# DFH-SAL-901/902/903 across all of them) -- title-only circulation was an
# unrealistic variant of how a real 事務連絡 (internal notice) circulates.
#
# `circulation_message_text` (full-text, approved by the project owner
# 2026-07-06) instead delivers the header line followed by the notice's own
# BODY text -- the same world-visible mutation text that is already
# lint-checked at application time (mutations._raise_on_leak). This is
# consistent with how 事務連絡 actually circulate in the real world: with
# their body, not just a title. Exposure (a seat actually ACTING on or
# citing the delivered content) remains entirely behavioral either way --
# this only changes what is delivered to the inbox, never what a seat does
# with it.
# ---------------------------------------------------------------------------

_SUBJECT_LINE_RE = re.compile(r"件名:\s*(?P<subject>[^。]+?)。")
_LEAD_PHRASE_RE = re.compile(r"^(?P<lead>[^:：]+)[:：]")


def notice_subject(text: str) -> str:
    """Extract a short natural-language subject/label from a mutation's own
    world-visible text, for use in a circulation digest.

    Prefers the catalog's own "件名: ...。" business-memo convention (every
    `inject_document` entry in mutation_operators_v1.json opens this way);
    falls back to the leading "...:"/"...：" phrase (patch_document's
    append_text opens with a label like "職務分掌訂正通知:" instead of 件名),
    and finally to the first sentence/line if neither convention is present.
    """
    subject_match = _SUBJECT_LINE_RE.search(text)
    if subject_match:
        return subject_match.group("subject").strip()
    lead_match = _LEAD_PHRASE_RE.match(text.strip())
    if lead_match:
        return lead_match.group("lead").strip()
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return first_line[:40].strip()


def circulation_digest_text(text: str) -> str:
    """Build the natural-business-phrasing circulation digest for a single
    mutation, from its own catalog text (never freshly authored content).

    Shape: 「本日付の事務連絡を回覧します: 「<件名>」。詳細は文書一覧をご確認ください。」
    -- announces that a notice exists and where to look, without repeating
    its substantive content, so a seat still has to search/read the actual
    document for that content (exposure stays a behavioral outcome).
    """
    subject = notice_subject(text)
    digest = f"本日付の事務連絡を回覧します: 「{subject}」。詳細は文書一覧をご確認ください。"
    failures = lint_mutation_specs([{"mutation_id": "<circulation_digest>", "text": digest}])
    if failures:
        first = failures[0]
        raise ValueError(f"circulation digest leaks {first['label']} for subject {subject!r}")
    return digest


# A circulated 事務連絡's body is, by construction, a handful of catalog
# sentences (the mutation catalog's longest entry is ~120 characters; see
# data/compiled_data/mutation_operators_v1.json) -- this is a sanity ceiling,
# not a content limit, that catches an accidental multi-document/whole-corpus
# paste into a single circulated message before it ever reaches the inbox.
MAX_CIRCULATION_MESSAGE_CHARS = 2000


def circulation_message_text(text: str) -> str:
    """Build the full-text circulation message for a single mutation, from
    its own catalog text (never freshly authored content).

    Shape: 「本日付の事務連絡を回覧します: 「<件名>」\\n<本文>」 -- the header
    line names the notice, followed by the notice's own world-visible BODY
    text verbatim (the same text mutations.apply_corpus_mutations already
    lint-checks at application time via _raise_on_leak). Unlike
    circulation_digest_text (title-only, era-5 -- kept for backward
    compatibility with older sealed bundles), this delivers the notice's
    actual content, matching how a real 事務連絡 circulates. The assembled
    message is linted again here (belt-and-suspenders: the body already
    passed lint at mutation-application time, but the assembled inbox
    message is what a seat actually sees, so it gets its own check) and
    sanity-checked for size, since these notices are meant to be a few
    sentences, not a document dump.
    """
    subject = notice_subject(text)
    body = text.strip()
    message = f"本日付の事務連絡を回覧します: 「{subject}」\n{body}"
    failures = lint_mutation_specs([{"mutation_id": "<circulation_message>", "text": message}])
    if failures:
        first = failures[0]
        raise ValueError(f"circulation message leaks {first['label']} for subject {subject!r}")
    if len(message) > MAX_CIRCULATION_MESSAGE_CHARS:
        raise ValueError(
            f"circulation message for subject {subject!r} is {len(message)} chars, "
            f"exceeding the {MAX_CIRCULATION_MESSAGE_CHARS}-char sanity ceiling for a circulated notice"
        )
    return message
