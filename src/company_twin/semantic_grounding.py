from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .env import normalize_openrouter_model, openrouter_slug
from .recorder import read_jsonl


G3_SCHEMA_VERSION = "company_twin.g3_semantic_grounding.v1"
G3_JUDGE_PROMPT_VERSION = "operational-support-v2"
DEFAULT_G3_CITED_TEXT_MAX_CHARS = 2200
SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
CONTRADICTED = "contradicted"
NOT_EVALUATED = "not_evaluated"
READINESS_ALLOWED_JUDGE_BACKENDS = frozenset({"openrouter"})

G3_NEGATIVE_CALIBRATION_SCHEMA_VERSION = "company_twin.g3_negative_calibration_result.v1"
# A negative-set case is "correct" when the judge's label matches the human
# label, or -- for the not_evaluated/missing-handle category -- when the
# judge abstains rather than asserting an unsupported entailment relation.
NEGATIVE_CALIBRATION_CATEGORIES = frozenset(
    {"fabricated_basis", "version_mismatch", "weak_support", "contradicted", "missing_handle"}
)


class SemanticJudge(Protocol):
    backend: str
    model: str

    def judge(self, *, cited_text: str, construal: str, decision: str, evidence_plan: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class LocalSemanticJudge:
    """Deterministic semantic proxy used for tests and offline triage.

    The production live path can use OpenRouterSemanticJudge. This local judge
    exists so run bundles can be triaged without silently falling back to None.
    """

    backend: str = "local_semantic_proxy"
    model: str = "deterministic-overlap-v1"

    def judge(self, *, cited_text: str, construal: str, decision: str, evidence_plan: str) -> dict[str, Any]:
        hypothesis = " ".join(part for part in (construal, decision, evidence_plan) if part)
        cited_terms = set(_semantic_terms(cited_text))
        hypothesis_terms = set(_semantic_terms(hypothesis))
        if not cited_terms or not hypothesis_terms:
            return {"label": NOT_EVALUATED, "confidence": 0.0, "rationale": "missing cited text or hypothesis terms"}
        if _has_polarity_conflict(cited_text, hypothesis):
            return {"label": CONTRADICTED, "confidence": 0.7, "rationale": "polarity conflict between cited text and basis"}
        overlap = hypothesis_terms & cited_terms
        long_hits = [term for term in overlap if len(term) >= 3]
        if len(overlap) >= 2 or long_hits:
            confidence = min(0.95, 0.45 + 0.08 * len(overlap) + 0.05 * len(long_hits))
            return {"label": SUPPORTED, "confidence": round(confidence, 3), "rationale": f"shared semantic terms={len(overlap)}"}
        return {"label": UNSUPPORTED, "confidence": 0.65, "rationale": "insufficient semantic support in cited text"}


class OpenRouterSemanticJudge:
    backend = "openrouter"

    def __init__(self, model: str | None = None):
        self.model = normalize_openrouter_model(model or os.getenv("COMPANY_TWIN_G3_MODEL"))
        self.max_retries = int(os.getenv("COMPANY_TWIN_G3_MAX_RETRIES", "3"))
        self.retry_sleep_seconds = float(os.getenv("COMPANY_TWIN_G3_RETRY_SLEEP_SECONDS", "2"))
        self._llm: Any | None = None

    def judge(self, *, cited_text: str, construal: str, decision: str, evidence_plan: str) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._judge_once(cited_text=cited_text, construal=construal, decision=decision, evidence_plan=evidence_plan)
            except Exception as exc:
                last_exc = exc
                if attempt >= self.max_retries or not _is_retryable_openrouter_error(exc):
                    raise
                time.sleep(self.retry_sleep_seconds * (attempt + 1))
        raise last_exc or RuntimeError("OpenRouter semantic judge failed")

    def _judge_once(self, *, cited_text: str, construal: str, decision: str, evidence_plan: str) -> dict[str, Any]:
        prompt = _judge_prompt(cited_text=cited_text, construal=construal, decision=decision, evidence_plan=evidence_plan)
        response = self._client().invoke([{"role": "user", "content": prompt}])
        content = getattr(response, "content", response)
        payload = _parse_json_object(str(content))
        label = str(payload.get("label") or "").strip().lower()
        if label not in {SUPPORTED, UNSUPPORTED, CONTRADICTED, NOT_EVALUATED}:
            label = UNSUPPORTED
        confidence = payload.get("confidence", 0.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        return {
            "label": label,
            "confidence": max(0.0, min(confidence, 1.0)),
            "rationale": str(payload.get("rationale") or "")[:500],
        }

    def _client(self) -> Any:
        if self._llm is None:
            from langchain_openai import ChatOpenAI

            self._llm = ChatOpenAI(
                api_key=os.environ["OPENROUTER_API_KEY"],
                model=openrouter_slug(self.model),
                base_url="https://openrouter.ai/api/v1",
                timeout=int(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "45")),
                max_retries=int(os.getenv("COMPANY_TWIN_G3_HTTP_MAX_RETRIES", "1")),
                max_completion_tokens=int(os.getenv("COMPANY_TWIN_G3_MAX_TOKENS", "500")),
            )
        return self._llm


def evaluate_semantic_grounding_run(
    run_root: Path,
    *,
    judge: SemanticJudge | None = None,
    cache_path: Path | None = None,
    write: bool = True,
) -> dict[str, Any]:
    run_root = run_root.resolve()
    judge = judge or LocalSemanticJudge()
    attempts = read_jsonl(run_root / "attempts.jsonl")
    basis_rows = read_jsonl(run_root / "basis_records.jsonl")
    reads = _reads_by_handle(attempts)
    cache_path = cache_path or (run_root / "g3_entailment_cache.json")
    cache = _read_cache(cache_path)

    rows: list[dict[str, Any]] = []
    for basis in basis_rows:
        if not basis.get("action_id"):
            continue
        row = _evaluate_basis_row(basis, reads=reads, judge=judge, cache=cache)
        rows.append(row)
        if write:
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    action_bound = len(rows)
    evaluated = [row for row in rows if row["label"] != NOT_EVALUATED]
    supported = [row for row in rows if row["label"] == SUPPORTED]
    all3 = [row for row in rows if row["g1"] is True and row["g2"] is True and row["label"] == SUPPORTED]
    readiness_eligible = judge.backend in READINESS_ALLOWED_JUDGE_BACKENDS
    g3_rate = (len(supported) / action_bound) if action_bound else None
    all3_rate = (len(all3) / action_bound) if action_bound else None
    payload = {
        "schema_version": G3_SCHEMA_VERSION,
        "run_root": str(run_root),
        "judge": {"backend": judge.backend, "model": judge.model, "prompt_version": G3_JUDGE_PROMPT_VERSION, "readiness_eligible": readiness_eligible},
        "basis_action_bound": action_bound,
        "evaluated_count": len(evaluated),
        "supported_count": len(supported),
        "semantic_all3_count": len(all3),
        "grounding_g3_semantic_rate": g3_rate if readiness_eligible else None,
        "grounding_semantic_all3_rate": all3_rate if readiness_eligible else None,
        "grounding_g3_semantic_rate_proxy": None if readiness_eligible else g3_rate,
        "grounding_semantic_all3_rate_proxy": None if readiness_eligible else all3_rate,
        "rows": rows,
        "cache_path": str(cache_path),
    }
    if write:
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        (run_root / "g3_semantic_grounding.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _is_retryable_openrouter_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    non_retryable_markers = (
        "insufficient credits",
        "authentication",
        "api key",
        "unauthorized",
        "badrequest",
        "bad request",
        "invalid_request",
        "permission",
        "401",
        "402",
        "403",
    )
    return not any(marker in text for marker in non_retryable_markers)


def evaluate_semantic_grounding_campaign(
    campaign_root: Path,
    *,
    judge: SemanticJudge | None = None,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    campaign_root = campaign_root.resolve()
    judge = judge or LocalSemanticJudge()
    shared_cache = cache_path or (campaign_root / "g3_entailment_cache.json")
    run_reports = []
    excluded_failed_run_ids = []
    for run_root in sorted(path for path in campaign_root.iterdir() if path.is_dir()):
        if (run_root / "failed_run.json").exists():
            excluded_failed_run_ids.append(run_root.name)
            continue
        if (run_root / "basis_records.jsonl").exists():
            run_reports.append(evaluate_semantic_grounding_run(run_root, judge=judge, cache_path=shared_cache, write=True))
    action_bound = sum(int(report.get("basis_action_bound") or 0) for report in run_reports)
    supported = sum(int(report.get("supported_count") or 0) for report in run_reports)
    all3 = sum(int(report.get("semantic_all3_count") or 0) for report in run_reports)
    readiness_eligible = judge.backend in READINESS_ALLOWED_JUDGE_BACKENDS
    g3_rate = (supported / action_bound) if action_bound else None
    all3_rate = (all3 / action_bound) if action_bound else None
    payload = {
        "schema_version": G3_SCHEMA_VERSION,
        "campaign_root": str(campaign_root),
        "judge": {"backend": judge.backend, "model": judge.model, "prompt_version": G3_JUDGE_PROMPT_VERSION, "readiness_eligible": readiness_eligible},
        "run_count": len(run_reports),
        "basis_action_bound": action_bound,
        "supported_count": supported,
        "semantic_all3_count": all3,
        "grounding_g3_semantic_rate": g3_rate if readiness_eligible else None,
        "grounding_semantic_all3_rate": all3_rate if readiness_eligible else None,
        "grounding_g3_semantic_rate_proxy": None if readiness_eligible else g3_rate,
        "grounding_semantic_all3_rate_proxy": None if readiness_eligible else all3_rate,
        "excluded_failed_run_ids": excluded_failed_run_ids,
        "run_reports": [
            {
                "run_root": report["run_root"],
                "grounding_semantic_all3_rate": report["grounding_semantic_all3_rate"],
                "grounding_semantic_all3_rate_proxy": report.get("grounding_semantic_all3_rate_proxy"),
                "judge": report.get("judge"),
            }
            for report in run_reports
        ],
    }
    (campaign_root / "g3_semantic_grounding.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def export_g3_calibration_samples(source_root: Path, output_path: Path, *, limit: int = 20) -> dict[str, Any]:
    source_root = source_root.resolve()
    output_path = output_path.resolve()
    samples = []
    for run_root in _iter_run_roots(source_root):
        attempts = read_jsonl(run_root / "attempts.jsonl")
        basis_rows = read_jsonl(run_root / "basis_records.jsonl")
        reads = _reads_by_handle(attempts)
        meta = _read_json(run_root / "meta.json")
        for basis in basis_rows:
            if not basis.get("action_id"):
                continue
            cited_texts = []
            retrieved = []
            for item in basis.get("retrieved") or []:
                handle = str((item or {}).get("citation_handle") or "")
                read = reads.get((str(basis.get("seat_id") or ""), handle)) if handle else None
                cited_text = str((read or {}).get("text") or "")
                retrieved.append(
                    {
                        "doc_id": str((item or {}).get("doc_id") or ""),
                        "version": str((item or {}).get("version") or ""),
                        "citation_handle": handle,
                        "cited_text": cited_text[:1600],
                    }
                )
                if cited_text:
                    cited_texts.append(cited_text)
            samples.append(
                {
                    "sample_id": f"{run_root.name}:{basis.get('basis_id')}",
                    "run_root": str(run_root.relative_to(source_root) if run_root != source_root and source_root in run_root.parents else run_root.name),
                    "stage": meta.get("stage"),
                    "prompt_mode": meta.get("prompt_mode"),
                    "basis_id": basis.get("basis_id"),
                    "seat_id": basis.get("seat_id"),
                    "action_id": basis.get("action_id"),
                    "trigger_event": basis.get("trigger_event"),
                    "retrieved": retrieved,
                    "construal": str(basis.get("construal") or ""),
                    "decision": str(basis.get("decision") or ""),
                    "evidence_plan": str(basis.get("evidence_plan") or ""),
                    "human_label": None,
                    "allowed_labels": [SUPPORTED, UNSUPPORTED, CONTRADICTED, NOT_EVALUATED],
                    "labeling_note": "Set human_label after blind review; do not use seeded span ids or latent truth.",
                }
            )
            if len(samples) >= limit:
                break
        if len(samples) >= limit:
            break
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(json.dumps(sample, ensure_ascii=False) + "\n" for sample in samples), encoding="utf-8")
    return {
        "schema_version": "company_twin.g3_calibration_samples.v1",
        "source_root": str(source_root),
        "output_path": str(output_path),
        "sample_count": len(samples),
        "limit": limit,
        "note": "Fill human_label manually, then run an OpenRouter g3 judge and record agreement in docs/g3_calibration.md.",
    }


def load_g3_calibration_cases(path: Path) -> list[dict[str, Any]]:
    """Read a calibration JSONL fixture (positive or negative) into row dicts.

    Validates the minimal schema shared by docs/g3_calibration_samples.jsonl
    (human_label filled after blind review) and
    docs/g3_negative_calibration_samples.jsonl (adds a required category).
    """
    from .recorder import read_jsonl

    rows = read_jsonl(path)
    if not rows:
        raise ValueError(f"no calibration cases found in {path}")
    required = {"cited_text", "construal", "decision", "evidence_plan", "human_label"}
    for index, row in enumerate(rows):
        missing = required - set(row)
        if missing:
            raise ValueError(f"calibration case {index} in {path} is missing required keys: {sorted(missing)}")
        if row.get("human_label") not in {SUPPORTED, UNSUPPORTED, CONTRADICTED, NOT_EVALUATED}:
            raise ValueError(f"calibration case {index} in {path} has invalid human_label={row.get('human_label')!r}")
        if "category" in row and row["category"] not in NEGATIVE_CALIBRATION_CATEGORIES:
            raise ValueError(f"calibration case {index} in {path} has unknown category={row['category']!r}")
    return rows


def _case_is_correct(*, human_label: str, judge_label: str) -> bool:
    if human_label == NOT_EVALUATED:
        # A missing-handle / unusable-evidence case is scored correct when the
        # judge abstains. Asserting supported/unsupported/contradicted from no
        # evidence is a specificity failure even though it is not literally a
        # false "supported" call.
        return judge_label == NOT_EVALUATED
    return judge_label == human_label


def score_g3_calibration_file(
    path: Path,
    *,
    judge: SemanticJudge | None = None,
    output_path: Path | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Score a committed calibration JSONL fixture with a live or local judge.

    This is the harness the design DoD calls a "specificity" measurement: it
    runs the same judge interface used by evaluate_semantic_grounding_run over
    a fixture of already-labeled cases and reports per-category and overall
    agreement with the human label. It has no dependency on a run bundle, so
    it works equally over the positive fixture (docs/g3_calibration_samples.jsonl)
    and the negative fixture (docs/g3_negative_calibration_samples.jsonl).
    """
    path = path.resolve()
    judge = judge or LocalSemanticJudge()
    cases = load_g3_calibration_cases(path)

    rows: list[dict[str, Any]] = []
    for case in cases:
        result = judge.judge(
            cited_text=str(case.get("cited_text") or ""),
            construal=str(case.get("construal") or ""),
            decision=str(case.get("decision") or ""),
            evidence_plan=str(case.get("evidence_plan") or ""),
        )
        judge_label = str(result.get("label", NOT_EVALUATED))
        human_label = str(case.get("human_label"))
        correct = _case_is_correct(human_label=human_label, judge_label=judge_label)
        rows.append(
            {
                "sample_id": case.get("sample_id"),
                "category": case.get("category"),
                "human_label": human_label,
                "judge_label": judge_label,
                "confidence": result.get("confidence", 0.0),
                "rationale": result.get("rationale", ""),
                "correct": correct,
            }
        )

    payload = summarize_g3_calibration_scores(rows, judge=judge, source_path=path)
    if write and output_path is not None:
        output_path = output_path.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["output_path"] = str(output_path)
    return payload


def summarize_g3_calibration_scores(rows: list[dict[str, Any]], *, judge: SemanticJudge, source_path: Path | str | None = None) -> dict[str, Any]:
    """Build the specificity summary artifact shape from already-scored rows.

    Split out from score_g3_calibration_file so the aggregation logic itself
    (per-category correct/incorrect counts, overall specificity) can be unit
    tested against a synthetic mini-set without a judge or fixture file.
    """
    by_category: dict[str, dict[str, int]] = {}
    for row in rows:
        category = str(row.get("category") or "uncategorized")
        bucket = by_category.setdefault(category, {"total": 0, "correct": 0, "incorrect": 0, "rejected": 0})
        bucket["total"] += 1
        if row.get("correct"):
            bucket["correct"] += 1
        else:
            bucket["incorrect"] += 1
        # A known-bad case counts as "rejected" whenever the judge did not
        # accept it as supported, even when its rejection label differs from
        # the fixture's expected label (e.g. weak_support expected
        # "unsupported" but judged "contradicted"). Exact-label agreement and
        # rejection are reported side by side; neither replaces the other.
        if str(row.get("judge_label") or "") != SUPPORTED:
            bucket["rejected"] += 1
    for bucket in by_category.values():
        bucket["specificity_rate"] = (bucket["correct"] / bucket["total"]) if bucket["total"] else None
        bucket["rejection_rate"] = (bucket["rejected"] / bucket["total"]) if bucket["total"] else None

    total = len(rows)
    correct_total = sum(1 for row in rows if row.get("correct"))
    rejected_total = sum(1 for row in rows if str(row.get("judge_label") or "") != SUPPORTED)
    readiness_eligible = judge.backend in READINESS_ALLOWED_JUDGE_BACKENDS
    return {
        "schema_version": G3_NEGATIVE_CALIBRATION_SCHEMA_VERSION,
        "source_path": str(source_path) if source_path is not None else None,
        "judge": {
            "backend": judge.backend,
            "model": judge.model,
            "prompt_version": G3_JUDGE_PROMPT_VERSION,
            "readiness_eligible": readiness_eligible,
        },
        "case_count": total,
        "correct_count": correct_total,
        "incorrect_count": total - correct_total,
        "overall_specificity_rate": (correct_total / total) if total else None,
        "rejected_count": rejected_total,
        "overall_rejection_rate": (rejected_total / total) if total else None,
        "by_category": by_category,
        "rows": rows,
    }


def _evaluate_basis_row(basis: dict[str, Any], *, reads: dict[tuple[str, str], dict[str, Any]], judge: SemanticJudge, cache: dict[str, Any]) -> dict[str, Any]:
    seat_id = str(basis.get("seat_id") or "")
    cited_texts = []
    missing_handles = []
    for item in basis.get("retrieved") or []:
        handle = str((item or {}).get("citation_handle") or "")
        read = reads.get((seat_id, handle)) if handle else None
        if read is None:
            missing_handles.append(handle or "<missing>")
            continue
        text = str(read.get("text") or "")
        if text:
            cited_texts.append(text)
    cited_text = "\n\n".join(cited_texts)
    construal = str(basis.get("construal") or "")
    decision = str(basis.get("decision") or "")
    evidence_plan = str(basis.get("evidence_plan") or "")
    key = _cache_key(cited_text=cited_text, construal=construal, decision=decision, evidence_plan=evidence_plan, model=judge.model, backend=judge.backend)
    if not cited_text or missing_handles:
        result = {"label": NOT_EVALUATED, "confidence": 0.0, "rationale": f"missing read text for handles={missing_handles}"}
    elif key in cache:
        result = dict(cache[key])
    else:
        result = judge.judge(cited_text=cited_text, construal=construal, decision=decision, evidence_plan=evidence_plan)
        cache[key] = result
    return {
        "basis_id": basis.get("basis_id"),
        "seat_id": seat_id,
        "action_id": basis.get("action_id"),
        "trigger_event": basis.get("trigger_event"),
        "g1": _g1_value(basis),
        "g2": basis.get("g2_prior_read") is True,
        "label": result.get("label", NOT_EVALUATED),
        "confidence": result.get("confidence", 0.0),
        "rationale": result.get("rationale", ""),
        "retrieved_count": len(basis.get("retrieved") or []),
        "missing_handles": missing_handles,
        "cache_key": key,
    }


def _reads_by_handle(attempts: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    reads: dict[tuple[str, str], dict[str, Any]] = {}
    for row in attempts:
        if row.get("tool") != "read_document" or not row.get("success"):
            continue
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        handle = str((result or {}).get("citation_handle") or "")
        if not handle:
            continue
        reads[(str(row.get("seat_id") or ""), handle)] = {
            "doc_id": str((row.get("args") or {}).get("doc_id") or result.get("doc_id") or ""),
            "version": str(result.get("version") or ""),
            "text": str(result.get("text") or result.get("snippet") or ""),
            "tick": row.get("tick"),
        }
    return reads


def _read_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _iter_run_roots(source_root: Path) -> list[Path]:
    if (source_root / "basis_records.jsonl").exists():
        return [source_root]
    return [path for path in sorted(source_root.iterdir()) if path.is_dir() and (path / "basis_records.jsonl").exists()]


def _cache_key(*, cited_text: str, construal: str, decision: str, evidence_plan: str, model: str, backend: str) -> str:
    payload = {
        "schema_version": G3_SCHEMA_VERSION,
        "prompt_version": G3_JUDGE_PROMPT_VERSION,
        "backend": backend,
        "model": model,
        "cited_text": cited_text,
        "construal": construal,
        "decision": decision,
        "evidence_plan": evidence_plan,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _prompt_cited_text(cited_text: str) -> str:
    try:
        max_chars = int(os.getenv("COMPANY_TWIN_G3_CITED_TEXT_MAX_CHARS", str(DEFAULT_G3_CITED_TEXT_MAX_CHARS)))
    except ValueError:
        max_chars = DEFAULT_G3_CITED_TEXT_MAX_CHARS
    if max_chars <= 0 or len(cited_text) <= max_chars:
        return cited_text
    marker = "\n\n[... cited text truncated for G3 prompt ...]\n\n"
    if max_chars <= len(marker):
        return cited_text[:max_chars]
    budget = max_chars - len(marker)
    head_chars = max(1, int(budget * 0.7))
    tail_chars = max(0, budget - head_chars)
    tail = cited_text[-tail_chars:] if tail_chars else ""
    return f"{cited_text[:head_chars]}{marker}{tail}"


def _g1_value(row: dict[str, Any]) -> bool | None:
    if row.get("g1_citation_handle_exists") is not None:
        return bool(row.get("g1_citation_handle_exists"))
    if row.get("g1_span_exists") is not None:
        return bool(row.get("g1_span_exists"))
    return None


def _semantic_terms(text: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z0-9_-]+|[\u3040-\u30ff\u3400-\u9fff]{2,}", text or ""):
        token = token.lower()
        if re.fullmatch(r"[A-Za-z0-9_-]+", token):
            if len(token) >= 3:
                terms.append(token)
            continue
        terms.append(token)
        terms.extend(token[idx : idx + 2] for idx in range(0, max(len(token) - 1, 0)))
    stop = {"する", "した", "ため", "こと", "もの", "この", "その", "ある", "いる", "から", "ので", "ます", "です"}
    return [term for term in terms if len(term) >= 2 and term not in stop]


def _has_polarity_conflict(cited_text: str, hypothesis: str) -> bool:
    cited_negative = any(term in cited_text for term in ("不要", "禁止", "できない", "認めない"))
    cited_required = any(term in cited_text for term in ("必要", "必須", "行う", "取得", "記録", "承認"))
    hyp_negative = any(term in hypothesis for term in ("不要", "省略", "なしでよい", "必要ない"))
    hyp_affirmative = any(term in hypothesis for term in ("必要", "必須", "行う", "取得", "記録", "承認", "使ってよい", "案内する", "実施する"))
    return (cited_required and hyp_negative) or (cited_negative and hyp_affirmative)


def _judge_prompt(*, cited_text: str, construal: str, decision: str, evidence_plan: str) -> str:
    cited_excerpt = _prompt_cited_text(cited_text)
    return f"""You are judging semantic grounding for a control-process audit trace.

Return only JSON with keys: label, confidence, rationale.
Allowed label values: supported, unsupported, contradicted, not_evaluated.

Question: does the cited text support the staff member's construal/decision/evidence plan?

Decision rules:
- Mark supported when the cited text substantively supports the operational
  policy/procedure reading, even if the staff uses local tool names, action ids,
  application ids, customer ids, or field names that do not appear verbatim.
- Mark supported when the staff concretely operationalizes a cited requirement
  such as identity confirmation, evidence capture, understanding checks,
  approval, return handling, or escalation.
- Do not penalize version-label wording such as v1.0/v1.1 when the cited text
  itself is the relevant procedure and no contradiction is present.
- Mark unsupported when the cited text is merely related but does not establish
  the needed requirement, permission, or next step.
- Mark contradicted only when the cited text and staff reading are materially
  inconsistent.
- Mark not_evaluated only when the cited text or staff reading is unusable.

CITED_TEXT:
{cited_excerpt}

CONSTRUAL:
{construal}

DECISION:
{decision}

EVIDENCE_PLAN:
{evidence_plan}
"""


def _parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        parts = [part.strip() for part in text.split("```") if part.strip()]
        for part in parts:
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                payload = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
        else:
            return {}
    return payload if isinstance(payload, dict) else {}
