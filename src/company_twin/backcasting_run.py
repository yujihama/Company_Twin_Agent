"""WP-14 follow-up: the LIVE re-simulation runner for backcasting.

`company_twin.backcasting` supplies the two offline halves of the §12
"backcasting" gate (extraction + scoring). This module supplies the missing
live middle: for a pre-registered sample of extracted cases, put the
*situation only* in front of a live seat (never the documented response,
never case ids or experimenter vocabulary -- see `backcasting_seat_prompt`),
then compare the seat's answer to the documented response with an explicit
judge, and write `backcasting_resimulation_results.json` for
`score_backcasting_reproduction()`/`write_backcasting_report()` to consume.

Two-plane separation (MASTER_DESIGN.md §12/§17, §14 "してはいけないこと"):
- The seat only ever sees `backcasting_seat_prompt(situation)`, which contains
  the situation text reframed as an ordinary business question. No case_id,
  no documented_response, no words like "backcasting", "probe", "case",
  "reproduction", "experiment" ever reach the seat.
- The judge is experimenter-plane: it MAY see the documented response, because
  its job is precisely to compare the seat's live answer against it.

Judge boundary (mirrors company_twin.semantic_grounding):
- `ReproductionJudge` protocol with an explicit `backend`/`model`.
- `LocalReproductionJudge` is a deterministic lexical-overlap proxy for
  offline tests; its output is always tagged non-`readiness_eligible` and can
  never be represented as live judge evidence.
- `OpenRouterReproductionJudge` requires an explicit `--judge-model`; only
  `backend == "openrouter"` is `readiness_eligible`.
- Judge results are cached by a hash that includes `JUDGE_PROMPT_VERSION`, so
  a prompt-version bump invalidates the cache instead of silently reusing
  stale labels.

Pre-registered sampling: `select_backcasting_sample()` is a pure function of
(cases, sample_size, sample_seed) using a stable sha256-of-case_id ordering,
so the same seed always yields the same sample and the full selection is
recorded in the results file (not just a summary count) -- post-hoc
cherry-picking of favorable cases is structurally impossible because every
selected case_id must appear in `results` (see `_run_one_case`: failures are
recorded as `reproduced: False` with `detail`, never dropped).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .env import normalize_openrouter_model, openrouter_slug
from .recorder import RunRecorder

BACKCASTING_RESULTS_SCHEMA_VERSION = "company_twin.backcasting_resimulation_results.v1"
JUDGE_PROMPT_VERSION = "backcasting-reproduction-judge-v1"
REPRODUCED = "reproduced"
NOT_REPRODUCED = "not_reproduced"
NOT_EVALUATED = "not_evaluated"
READINESS_ALLOWED_JUDGE_BACKENDS = frozenset({"openrouter"})


class BackcastingInputsError(RuntimeError):
    """backcasting_inputs.json is missing, unreadable, or has zero cases.

    Raised instead of writing an empty backcasting_resimulation_results.json:
    a silent no-op results file is indistinguishable from a completed live
    pass to anyone who only checks that the file exists."""


# ---------------------------------------------------------------------------
# Pre-registered deterministic sampling
# ---------------------------------------------------------------------------


def _sample_sort_key(case_id: str, seed: int) -> str:
    return hashlib.sha256(f"{seed}:{case_id}".encode("utf-8")).hexdigest()


def select_backcasting_sample(cases: list[dict[str, Any]], *, sample_size: int | None, sample_seed: int) -> dict[str, Any]:
    """Deterministically select up to `sample_size` cases from `cases`.

    Ordering is a stable hash of (sample_seed, case_id); the same
    (cases, sample_size, sample_seed) always yields the same selected
    case_ids in the same order, regardless of the incoming list order. This
    is what makes the sample pre-registerable: recording sample_size and
    sample_seed in the results file lets anyone recompute the exact same
    selection from backcasting_inputs.json and confirm nothing was dropped
    or swapped after the fact.
    """
    all_case_ids = [str(case["case_id"]) for case in cases]
    ordered = sorted(all_case_ids, key=lambda case_id: _sample_sort_key(case_id, sample_seed))
    n = len(ordered) if sample_size is None else max(0, min(int(sample_size), len(ordered)))
    selected = ordered[:n]
    return {
        "sample_size": n,
        "sample_seed": sample_seed,
        "population_size": len(all_case_ids),
        "selected_case_ids": selected,
    }


# ---------------------------------------------------------------------------
# Two-plane seat prompt: situation only, natural business question, S0-style
# ---------------------------------------------------------------------------


def backcasting_seat_prompt(situation: str) -> str:
    """Build the S0-style prompt sent to the live seat.

    Contains ONLY the situation, reframed as an ordinary business question.
    Must never contain the documented response, a case_id, or experimenter
    vocabulary (probe/span/backcasting/reproduction/oracle/experiment/mutation).
    """
    return f"""次の案件があなたの担当キューに入りました。実際に処理へ入る前の整理として、社内文書を検索・閲覧したうえで、最後は必ずJSONだけを返してください。説明文、Markdown、箇条書き、前置きは禁止です。

案件の状況: {situation}

回答JSONの形式。キー名は変更しないでください:
{{
  "likely_reading": "この状況について、文書からのあなたの読み",
  "response": "あなたがこの場でとる対応（具体的に）",
  "required_approver_or_evidence": "誰の承認・どの証跡が必要とあなたは判断するか",
  "cited_doc_ids": ["実際に閲覧した文書IDのみ"]
}}

読んでいない文書は cited_doc_ids に含めないでください。最終出力の1文字目は {{、最後の1文字は }} にしてください。"""


_EXPERIMENTER_TERMS = ("backcasting", "reproduction", "probe", "span", "oracle", "experiment", "mutation", "case_id", "documented_response")


def assert_two_plane_clean(prompt: str) -> None:
    """Defense-in-depth check: raise if the seat prompt leaked experimenter
    vocabulary. Called by the runner before every live invocation."""
    lowered = prompt.lower()
    for term in _EXPERIMENTER_TERMS:
        if term in lowered:
            raise ValueError(f"two-plane violation: seat prompt contains experimenter-plane term {term!r}")


def _parse_seat_response(response: str) -> dict[str, Any]:
    text = response.strip()
    if "```" in text:
        for block in text.split("```"):
            candidate = block.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                text = candidate
                break
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            payload = json.loads(text[start : end + 1])
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass
    return {}


# ---------------------------------------------------------------------------
# Live seat invocation (single-shot, S0-style; llm_invoke evidence recorded)
# ---------------------------------------------------------------------------


class BackcastingSeat(Protocol):
    backend: str
    model: str

    def answer(self, prompt: str) -> str: ...


class OpenRouterBackcastingSeat:
    """Live single-shot seat used for the real re-simulation pass.

    Mirrors DeepAgentCustomer/OpenRouterSemanticJudge: a plain chat
    invocation (no world tools) is sufficient because the case situation is
    a self-contained business scenario extracted verbatim from the corpus,
    not a probe/span reference that needs live document search.
    """

    backend = "openrouter"

    def __init__(self, model: str | None = None):
        self.model = normalize_openrouter_model(model)

    def answer(self, prompt: str) -> str:
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"],
            model=openrouter_slug(self.model),
            base_url="https://openrouter.ai/api/v1",
            timeout=int(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "45")),
            max_retries=0,
            max_completion_tokens=int(os.getenv("COMPANY_TWIN_BACKCASTING_MAX_TOKENS", "800")),
        )
        response = llm.invoke([{"role": "user", "content": prompt}])
        content = getattr(response, "content", response)
        return str(content)


def default_backcasting_seat_factory(model: str | None) -> "BackcastingSeat":
    return OpenRouterBackcastingSeat(model)


# ---------------------------------------------------------------------------
# Reproduction judge (experimenter-plane; may see the documented response)
# ---------------------------------------------------------------------------


class ReproductionJudge(Protocol):
    backend: str
    model: str

    def judge(self, *, situation: str, documented_response: str, seat_answer: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class LocalReproductionJudge:
    """Deterministic lexical-overlap proxy for offline tests.

    Never readiness_eligible: see READINESS_ALLOWED_JUDGE_BACKENDS. This
    exists so the runner and its tests can execute end-to-end without a
    network call, matching LocalSemanticJudge in semantic_grounding.py.
    """

    backend: str = "local_reproduction_proxy"
    model: str = "deterministic-overlap-v1"

    def judge(self, *, situation: str, documented_response: str, seat_answer: str) -> dict[str, Any]:
        if not seat_answer.strip():
            return {"label": NOT_REPRODUCED, "confidence": 1.0, "rationale": "seat produced no answer"}
        documented_terms = set(_reproduction_terms(documented_response))
        answer_terms = set(_reproduction_terms(seat_answer))
        if not documented_terms or not answer_terms:
            return {"label": NOT_EVALUATED, "confidence": 0.0, "rationale": "missing documented response or seat answer terms"}
        overlap = documented_terms & answer_terms
        long_hits = [term for term in overlap if len(term) >= 3]
        coverage = len(overlap) / max(len(documented_terms), 1)
        if coverage >= 0.3 or len(long_hits) >= 2:
            confidence = round(min(0.95, 0.4 + coverage), 3)
            return {"label": REPRODUCED, "confidence": confidence, "rationale": f"shared terms={len(overlap)} coverage={coverage:.2f}"}
        return {"label": NOT_REPRODUCED, "confidence": round(0.5 + (1 - coverage) * 0.3, 3), "rationale": f"insufficient overlap coverage={coverage:.2f}"}


class OpenRouterReproductionJudge:
    backend = "openrouter"

    def __init__(self, model: str | None = None):
        self.model = normalize_openrouter_model(model)

    def judge(self, *, situation: str, documented_response: str, seat_answer: str) -> dict[str, Any]:
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"],
            model=openrouter_slug(self.model),
            base_url="https://openrouter.ai/api/v1",
            timeout=int(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "45")),
            max_retries=0,
            max_completion_tokens=int(os.getenv("COMPANY_TWIN_BACKCASTING_JUDGE_MAX_TOKENS", "400")),
        )
        prompt = _judge_prompt(situation=situation, documented_response=documented_response, seat_answer=seat_answer)
        response = llm.invoke([{"role": "user", "content": prompt}])
        content = getattr(response, "content", response)
        payload = _parse_json_object(str(content))
        label = str(payload.get("label") or "").strip().lower()
        if label not in {REPRODUCED, NOT_REPRODUCED, NOT_EVALUATED}:
            label = NOT_REPRODUCED
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


def _judge_prompt(*, situation: str, documented_response: str, seat_answer: str) -> str:
    return f"""You are judging whether a staff member's live response to a field situation reproduces a documented field-judgment precedent.

Return only JSON with keys: label, confidence, rationale.
Allowed label values: reproduced, not_reproduced, not_evaluated.

Decision rules:
- Mark reproduced when the staff member's response reaches the same substantive
  judgment/action as the documented response (hold vs proceed, which approval
  or evidence is required, escalate vs handle directly), even if wording,
  tool names, or level of detail differ.
- Mark not_reproduced when the staff member's response reaches a materially
  different judgment/action, omits a required step present in the documented
  response, or contradicts it.
- Mark not_evaluated only when the staff member's response is unusable
  (empty, off-topic, or does not address the situation at all).

SITUATION:
{situation[:2000]}

DOCUMENTED_RESPONSE (ground truth for this comparison only):
{documented_response[:2000]}

STAFF_LIVE_RESPONSE:
{seat_answer[:3000]}
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


def _reproduction_terms(text: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z0-9_-]+|[぀-ヿ㐀-鿿]{2,}", text or ""):
        token = token.lower()
        if re.fullmatch(r"[A-Za-z0-9_-]+", token):
            if len(token) >= 3:
                terms.append(token)
            continue
        terms.append(token)
        terms.extend(token[idx : idx + 2] for idx in range(0, max(len(token) - 1, 0)))
    stop = {"する", "した", "ため", "こと", "もの", "この", "その", "ある", "いる", "から", "ので", "ます", "です"}
    return [term for term in terms if len(term) >= 2 and term not in stop]


def _judge_cache_key(*, situation: str, documented_response: str, seat_answer: str, backend: str, model: str) -> str:
    payload = {
        "schema_version": BACKCASTING_RESULTS_SCHEMA_VERSION,
        "prompt_version": JUDGE_PROMPT_VERSION,
        "backend": backend,
        "model": model,
        "situation": situation,
        "documented_response": documented_response,
        "seat_answer": seat_answer,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _run_one_case(
    case: dict[str, Any],
    *,
    campaign_root: Path,
    seat: BackcastingSeat,
    judge: ReproductionJudge,
    judge_cache: dict[str, Any],
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    situation = str(case.get("situation") or "")
    documented_response = str(case.get("documented_response") or "")
    run_root = campaign_root / "backcasting_runs" / case_id
    recorder = RunRecorder(
        run_root,
        run_id=f"backcasting_{case_id}",
        meta={"stage": "backcasting_resimulation", "case_id": case_id, "seat_backend": seat.backend, "seat_model": seat.model},
    )
    prompt = backcasting_seat_prompt(situation)
    try:
        assert_two_plane_clean(prompt)
    except ValueError as exc:
        # This should be structurally impossible (the prompt builder never
        # touches documented_response/case_id), but fail the case honestly
        # rather than silently sending a leaked prompt if it ever happens.
        recorder.append_ledger("two_plane_violation", {"case_id": case_id, "error": str(exc)})
        return {
            "case_id": case_id,
            "reproduced": False,
            "probe_id": "backcasting_resimulation",
            "run_root": str(run_root),
            "detail": f"two_plane_violation: {exc}",
            "seat_answer": "",
            "judge_label": NOT_EVALUATED,
        }

    recorder.record_attempt(
        seat_id="backcasting_seat",
        tool="llm_invoke",
        args={"backend": seat.backend, "model": seat.model, "prompt_chars": len(prompt), "phase": "start"},
        success=True,
        result={"phase": "start"},
    )
    try:
        with recorder.origin("agent"):
            response = seat.answer(prompt)
    except Exception as exc:  # noqa: BLE001 - recorded as an honest failed row, never dropped
        recorder.record_attempt(
            seat_id="backcasting_seat",
            tool="llm_invoke",
            args={"backend": seat.backend, "model": seat.model, "prompt_chars": len(prompt), "phase": "error", "error_type": type(exc).__name__},
            success=False,
            result={"error_type": type(exc).__name__, "message": str(exc)[:500]},
        )
        recorder.write_json("backcasting_case.json", {"case_id": case_id, "situation": situation, "outcome": "seat_call_failed"})
        return {
            "case_id": case_id,
            "reproduced": False,
            "probe_id": "backcasting_resimulation",
            "run_root": str(run_root),
            "detail": f"seat call failed: {type(exc).__name__}: {str(exc)[:300]}",
            "seat_answer": "",
            "judge_label": NOT_EVALUATED,
        }

    recorder.record_attempt(
        seat_id="backcasting_seat",
        tool="llm_response",
        args={"backend": seat.backend, "model": seat.model, "prompt_chars": len(prompt)},
        success=True,
        result={"response_chars": len(response)},
    )
    parsed = _parse_seat_response(response)
    seat_answer_text = "\n".join(
        str(parsed.get(key) or "") for key in ("likely_reading", "response", "required_approver_or_evidence") if parsed.get(key)
    ).strip() or response.strip()

    cache_key = _judge_cache_key(situation=situation, documented_response=documented_response, seat_answer=seat_answer_text, backend=judge.backend, model=judge.model)
    if cache_key in judge_cache:
        judgment = dict(judge_cache[cache_key])
    else:
        judgment = judge.judge(situation=situation, documented_response=documented_response, seat_answer=seat_answer_text)
        judge_cache[cache_key] = judgment

    recorder.write_json(
        "backcasting_case.json",
        {
            "case_id": case_id,
            "situation": situation,
            "documented_response": documented_response,
            "seat_prompt": prompt,
            "seat_response_raw": response,
            "seat_answer_parsed": parsed,
            "outcome": "answered",
            "judge": {"backend": judge.backend, "model": judge.model, "prompt_version": JUDGE_PROMPT_VERSION, **judgment},
        },
    )
    reproduced = judgment.get("label") == REPRODUCED
    detail = f"judge={judge.backend}/{judge.model} label={judgment.get('label')} rationale={judgment.get('rationale', '')[:200]}"
    return {
        "case_id": case_id,
        "reproduced": reproduced,
        "probe_id": "backcasting_resimulation",
        "run_root": str(run_root),
        "detail": detail,
        "seat_answer": seat_answer_text,
        "judge_label": judgment.get("label"),
        "judge_confidence": judgment.get("confidence"),
        "judge_rationale": judgment.get("rationale"),
    }


def run_backcasting_resimulation(
    campaign_root: Path,
    *,
    seat: BackcastingSeat,
    judge: ReproductionJudge,
    sample_size: int | None,
    sample_seed: int = 0,
    write: bool = True,
) -> dict[str, Any]:
    """Execute the live re-simulation pass and write
    backcasting_resimulation_results.json.

    Every case_id selected by select_backcasting_sample() appears exactly
    once in `results`, whether the seat call succeeded or failed -- honest
    failure is recorded as `reproduced: False` with a `detail` string, never
    silently dropped from the results file. This, plus recording the full
    selected_case_ids list (not just a count), is what makes post-hoc
    cherry-picking of the sample structurally impossible: any downstream
    reader can recompute select_backcasting_sample() from
    backcasting_inputs.json and confirm the recorded sample matches.

    Raises BackcastingInputsError (and writes nothing) when
    backcasting_inputs.json is missing, unreadable, or has zero cases.
    """
    campaign_root = campaign_root.resolve()
    inputs_path = campaign_root / "backcasting_inputs.json"
    extraction = _read_json(inputs_path)
    cases = extraction.get("cases") or []
    if not cases:
        if not inputs_path.exists():
            reason = f"{inputs_path} does not exist"
        elif not extraction:
            reason = f"{inputs_path} is not a readable JSON object"
        else:
            reason = f"{inputs_path} has zero cases"
        raise BackcastingInputsError(
            f"cannot run the backcasting re-simulation: {reason}. "
            f"Run `backcasting-extract --campaign-root {campaign_root}` first. "
            "Refusing to write an empty backcasting_resimulation_results.json -- "
            "a silent no-op could be mistaken for a completed live pass."
        )

    cases_by_id = {str(case["case_id"]): case for case in cases}
    sample = select_backcasting_sample(cases, sample_size=sample_size, sample_seed=sample_seed)

    cache_path = campaign_root / "backcasting_judge_cache.json"
    judge_cache = _read_json(cache_path)

    results: list[dict[str, Any]] = []
    for case_id in sample["selected_case_ids"]:
        case = cases_by_id.get(case_id)
        if case is None:
            # Structurally shouldn't happen (selection is drawn from `cases`
            # itself), but record honestly rather than silently skip.
            results.append({"case_id": case_id, "reproduced": False, "probe_id": "backcasting_resimulation", "run_root": "", "detail": "case_id missing from backcasting_inputs.json at run time"})
            continue
        results.append(_run_one_case(case, campaign_root=campaign_root, seat=seat, judge=judge, judge_cache=judge_cache))

    readiness_eligible = judge.backend in READINESS_ALLOWED_JUDGE_BACKENDS
    payload = {
        "schema_version": BACKCASTING_RESULTS_SCHEMA_VERSION,
        "campaign_root": str(campaign_root),
        "sample": sample,
        "seat": {"backend": seat.backend, "model": seat.model},
        "judge": {"backend": judge.backend, "model": judge.model, "prompt_version": JUDGE_PROMPT_VERSION, "readiness_eligible": readiness_eligible},
        "results": results,
    }
    if write:
        cache_path.write_text(json.dumps(judge_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        (campaign_root / "backcasting_resimulation_results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
