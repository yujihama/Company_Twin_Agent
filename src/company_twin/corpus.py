from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from openpyxl import load_workbook

from .design_loader import DesignInputs, DocumentMeta


@dataclass(frozen=True)
class SearchHit:
    doc_id: str
    title: str
    score: float
    snippet: str
    path: str


@dataclass
class CorpusDocument:
    meta: DocumentMeta
    text: str

    @property
    def title(self) -> str:
        if self.meta.path:
            return self.meta.path.stem
        return self.meta.doc_id


class Corpus:
    def __init__(self, documents: dict[str, CorpusDocument]):
        self.documents = documents

    @classmethod
    def from_design(cls, design: DesignInputs) -> "Corpus":
        docs: dict[str, CorpusDocument] = {}
        for doc_id, meta in design.documents.items():
            text = ""
            if meta.path:
                text = extract_text(meta.path)
            docs[doc_id] = CorpusDocument(meta=meta, text=text)
        return cls(docs)

    def get(self, doc_id: str) -> CorpusDocument:
        return self.documents[doc_id]

    def search(self, query: str, *, seat_role: str = "", top_k: int = 5) -> list[SearchHit]:
        terms = _terms(query)
        hits: list[SearchHit] = []
        for doc in self.documents.values():
            if not doc.text:
                continue
            haystack = doc.text.lower()
            score = sum(haystack.count(term.lower()) for term in terms)
            score += _role_boost(doc, seat_role, terms)
            if score <= 0:
                continue
            snippet = _best_snippet(doc.text, terms)
            hits.append(
                SearchHit(
                    doc_id=doc.meta.doc_id,
                    title=doc.title,
                    score=float(score),
                    snippet=snippet,
                    path=str(doc.meta.path or ""),
                )
            )
        hits.sort(key=lambda hit: (-hit.score, hit.doc_id))
        return hits[:top_k]

    def search_json(self, query: str, *, seat_role: str = "", top_k: int = 5) -> str:
        return json.dumps([hit.__dict__ for hit in self.search(query, seat_role=seat_role, top_k=top_k)], ensure_ascii=False)


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return extract_docx_text(path)
    if suffix in {".xlsx", ".xlsm"}:
        return extract_xlsx_text(path)
    return path.read_text(encoding="utf-8", errors="ignore")


def extract_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        parts = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def extract_xlsx_text(path: Path) -> str:
    workbook = load_workbook(path, read_only=True, data_only=True)
    lines: list[str] = []
    try:
        for sheet in workbook.worksheets:
            lines.append(f"# sheet: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                values = [str(value).strip() for value in row if value is not None and str(value).strip()]
                if values:
                    lines.append("\t".join(values))
    finally:
        workbook.close()
    return "\n".join(lines)


def _terms(query: str) -> list[str]:
    terms = re.findall(r"[A-Za-z0-9_-]+|[\u3040-\u30ff\u3400-\u9fff]{2,}", query)
    return [term for term in terms if len(term) >= 2]


def _role_boost(doc: CorpusDocument, seat_role: str, terms: list[str]) -> float:
    title = doc.title
    text = doc.text
    boost = 0.0
    if seat_role == "sales" and ("現場FAQ" in text or "現場判断事例" in text):
        boost += 2.0
    if seat_role in {"second_line", "audit"} and doc.meta.kind == "規程":
        boost += 1.5
    if any(term in title for term in terms):
        boost += 3.0
    return boost


def _best_snippet(text: str, terms: list[str], width: int = 700) -> str:
    if not text:
        return ""
    lower = text.lower()
    positions = [lower.find(term.lower()) for term in terms if lower.find(term.lower()) >= 0]
    start = max(min(positions) - width // 4, 0) if positions else 0
    snippet = text[start : start + width]
    return re.sub(r"\s+", " ", snippet).strip()
