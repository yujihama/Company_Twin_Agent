from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .agents import create_seat_agent, invoke_agent
from .corpus import Corpus
from .customer_agent import emit_customer_turn
from .deck import CustomerEvent, build_customer_deck, event_for_probe
from .design_loader import DesignInputs
from .env import normalize_openrouter_model
from .kernel import KernelProfile, WorldKernel
from .recorder import BasisRecord, RunRecorder, utc_now
from .seat_runtime import run_policy_seat_turn
from .tools import build_role_tools
from .world_config import build_world_config


def make_run_root(root: Path, label: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return root / "runs" / f"{label}_{stamp}"


def run_s0(
    *,
    design: DesignInputs,
    corpus: Corpus,
    probe_id: str,
    seat_id: str,
    run_root: Path,
    live: bool,
    model: str | None = None,
    variant: int = 0,
) -> dict[str, str]:
    probe = design.probes[probe_id]
    seat = design.seats[seat_id]
    model_name = normalize_openrouter_model(model)
    recorder = RunRecorder(run_root, run_id=run_root.name, meta={"stage": "S0", "probe": probe_id, "seat": seat_id, "live": live, "model": model_name, "variant": variant})
    write_config_snapshot(
        run_root,
        build_world_config(design, stage="S0", model=model_name, seed=variant, ticks=1, probe_id=probe_id, seat_id=seat_id, executed_s0_rows=1),
    )
    query = _s0_query(probe_id, probe.title, variant)
    hits = corpus.search(query, seat_role=seat.role, top_k=5)
    recorder.record_attempt(seat_id=seat_id, tool="search_corpus", args={"query": query, "top_k": 5}, success=True, result=[hit.__dict__ for hit in hits])
    if hits:
        recorder.record_attempt(seat_id=seat_id, tool="read_document", args={"doc_id": hits[0].doc_id, "query": query, "max_chars": 1600}, success=True, result={"chars": len(hits[0].snippet), "version": corpus.get(hits[0].doc_id).meta.version})
    live_response = ""
    if live:
        kernel = WorldKernel(recorder, _kernel_profile(design))
        tools = build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, seat_role=seat.role)
        agent = create_seat_agent(seat_id=seat_id, role=seat.role, tools=tools, model=model_name)
        with recorder.origin("agent"):
            live_response = invoke_agent(agent, _s0_prompt(probe.title, query), recursion_limit=60)
        recorder.append_ledger("s0_agent_response", {"seat_id": seat_id, "probe_id": probe_id, "response": live_response})
    span_id = probe.binds[0] if probe.binds else ""
    answer_records = _s0_answer_records(hits, probe.title, live_response=live_response)
    clusters = [_classify_s0_answer(answer["answer"], design.spans.get(span_id).candidates if span_id in design.spans else {}) for answer in answer_records]
    cluster_counts = Counter(clusters)
    interpretation_class = cluster_counts.most_common(1)[0][0] if cluster_counts else "unclassified"
    entropy = _entropy(cluster_counts)
    basis = BasisRecord(
        basis_id=recorder.next_basis_id(),
        ts=utc_now(),
        run_id=recorder.run_id,
        tick=recorder.tick,
        seat_id=seat_id,
        action_id=None,
        trigger_event=f"s0:{probe_id}",
        retrieved=[{"doc_id": hits[0].doc_id, "version": corpus.get(hits[0].doc_id).meta.version, "span_id": span_id}] if hits else [],
        construal=f"{probe.title} answer cluster majority is {interpretation_class}",
        decision=interpretation_class,
        evidence_plan="Use the cited document, then escalate through workflow if the customer event becomes actionable.",
        alternatives_considered="manager-only route; second-line route; hold and ask customer for more evidence",
        felt_constraints="customer deadline and evidence sufficiency",
        confidence=0.55 + (0.1 if variant else 0.0),
        grounded=bool(hits),
    )
    recorder.record_basis(seat_id, basis)
    result = {
        "mode": "live" if live else "deterministic",
        "probe_id": probe_id,
        "seat_id": seat_id,
        "span_id": span_id,
        "interpretation_class": interpretation_class,
        "doc_id": hits[0].doc_id if hits else "",
        "entropy": f"{entropy:.4f}",
        "cluster_counts": json.dumps(dict(cluster_counts), ensure_ascii=False),
        "answer_count": str(len(answer_records)),
        "run_root": str(run_root),
    }
    recorder.write_json("s0_result.json", result)
    recorder.write_json("s0_answers.json", {"answers": answer_records, "cluster_counts": dict(cluster_counts), "entropy": entropy})
    if live_response:
        result["response"] = live_response
    return result


def run_s1_episode(
    *,
    design: DesignInputs,
    corpus: Corpus,
    probe_id: str,
    seat_id: str,
    run_root: Path,
    live: bool,
    model: str | None = None,
    knobs: dict[str, bool] | None = None,
    seed: int = 0,
    max_agent_calls: int | None = None,
) -> dict[str, str]:
    event = event_for_probe(design, probe_id)
    model_name = normalize_openrouter_model(model)
    ticks = 6
    recorder = RunRecorder(run_root, run_id=run_root.name, meta={"stage": "S1", "probe": probe_id, "seat": seat_id, "live": live, "model": model_name, "knobs": knobs or {}, "seed": seed})
    config = build_world_config(design, stage="S1", model=model_name, seed=seed, ticks=ticks, probe_id=probe_id, seat_id=seat_id, knobs=knobs or {})
    write_config_snapshot(run_root, config)
    recorder.configure_tick_budgets(config["world"]["population"]["tick_budget"])
    kernel = WorldKernel(recorder, _kernel_profile(design, knobs=knobs or {}))
    calls = _run_event_loop(
        design=design,
        corpus=corpus,
        kernel=kernel,
        recorder=recorder,
        events=[_event_for_s1(event)],
        ticks=ticks,
        live=live,
        model=model_name,
        max_agent_calls=max_agent_calls,
    )
    summary = {
        "mode": "live" if live else "deterministic",
        "run_root": str(run_root),
        "agent_calls": str(calls),
        "participant_seats": json.dumps(list(event.participant_seats), ensure_ascii=False),
        "ticks": str(ticks),
    }
    recorder.write_json("run_summary.json", summary)
    return summary


def run_s2_world(
    *,
    design: DesignInputs,
    corpus: Corpus,
    run_root: Path,
    live: bool,
    model: str | None = None,
    knobs: dict[str, bool] | None = None,
    seed: int = 0,
    max_agent_calls: int | None = None,
    anchor: bool = False,
) -> dict[str, str]:
    model_name = normalize_openrouter_model(model)
    ticks = 40
    recorder = RunRecorder(run_root, run_id=run_root.name, meta={"stage": "S2", "live": live, "model": model_name, "knobs": knobs or {}, "seed": seed, "anchor": anchor})
    config = build_world_config(design, stage="S2", model=model_name, seed=seed, ticks=ticks, anchor=anchor, knobs=knobs or {})
    write_config_snapshot(run_root, config)
    recorder.configure_tick_budgets(config["world"]["population"]["tick_budget"])
    kernel = WorldKernel(recorder, _kernel_profile(design, knobs=knobs or {}, scc_switch_enabled=not anchor))
    deck = build_customer_deck(design, include_routine=True)
    calls = _run_event_loop(
        design=design,
        corpus=corpus,
        kernel=kernel,
        recorder=recorder,
        events=deck,
        ticks=ticks,
        live=live,
        model=model_name,
        max_agent_calls=max_agent_calls,
    )
    summary = {"mode": "live" if live else "deterministic", "run_root": str(run_root), "agent_calls": str(calls), "events": str(len(deck)), "ticks": str(ticks), "anchor": str(anchor).lower()}
    recorder.write_json("run_summary.json", summary)
    return summary


def _run_event_loop(
    *,
    design: DesignInputs,
    corpus: Corpus,
    kernel: WorldKernel,
    recorder: RunRecorder,
    events: list[CustomerEvent],
    ticks: int,
    live: bool,
    model: str,
    max_agent_calls: int | None,
) -> int:
    events_by_tick: dict[int, list[CustomerEvent]] = {}
    for event in events:
        events_by_tick.setdefault(min(event.trigger_tick, ticks), []).append(event)
    agent_calls = 0
    live_budget = max_agent_calls if max_agent_calls is not None else (999999 if live else 0)
    for tick in range(1, ticks + 1):
        kernel.fire_timed_events(tick)
        for event in events_by_tick.get(tick, []):
            emit_customer_turn(kernel=kernel, recorder=recorder, event=event, tick=tick)
        for seat_id in list(kernel.inbox_nonempty_seats()):
            messages = kernel.pop_inbox(seat_id)
            for message in messages:
                if live and agent_calls < live_budget:
                    before_workflow_attempts = _workflow_attempt_count(recorder.run_root)
                    _invoke_live_agent_for_message(design, corpus, kernel, recorder, seat_id, message, model)
                    agent_calls += 1
                    after_workflow_attempts = _workflow_attempt_count(recorder.run_root)
                    internal_inbox_ready = any(other != seat_id and other.startswith("emp-") for other in kernel.inbox_nonempty_seats())
                    if after_workflow_attempts == before_workflow_attempts or not internal_inbox_ready:
                        recorder.append_ledger("agent_turn_policy_handoff", {"seat_id": seat_id, "message_kind": message.get("kind"), "reason": "no workflow action or no internal handoff"})
                        run_policy_seat_turn(design=design, corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, message=message)
                else:
                    run_policy_seat_turn(design=design, corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, message=message)
        recorder.append_ledger("tick_committed", {"tick": tick})
    return agent_calls


def _invoke_live_agent_for_message(design: DesignInputs, corpus: Corpus, kernel: WorldKernel, recorder: RunRecorder, seat_id: str, message: dict[str, Any], model: str) -> None:
    seat = design.seats.get(seat_id)
    if not seat:
        return
    tools = build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, seat_role=seat.role)
    agent = create_seat_agent(seat_id=seat_id, role=seat.role, tools=tools, model=model)
    prompt = f"""You have one inbox item in the DFH workflow.

Inbox item:
{json.dumps(message, ensure_ascii=False)}

Process only this item through world-visible tools. Search and read relevant documents before any control-relevant action. Use workflow, chat, customer-contact, approval, or application tools only when your role can justify them from what you read.
"""
    with recorder.origin("agent"):
        try:
            output = invoke_agent(agent, prompt, recursion_limit=40)
        except Exception as exc:
            recorder.append_ledger("agent_error", {"seat_id": seat_id, "error_type": type(exc).__name__, "message": str(exc)[:500], "message_kind": message.get("kind")})
            return
    recorder.append_ledger("agent_response", {"seat_id": seat_id, "response": output, "message_kind": message.get("kind")})


def _basis(corpus: Corpus, doc_id: str, event: dict[str, Any], *, decision: str) -> dict[str, Any]:
    span_ids = event.get("span_ids") or []
    return {
        "trigger_event": event.get("event_id") or event.get("application_id"),
        "retrieved": [{"doc_id": doc_id, "version": corpus.get(doc_id).meta.version, "span_id": span_ids[0] if span_ids else ""}],
        "construal": f"{event.get('probe_id')} requires {decision} based on read document.",
        "decision": decision,
        "evidence_plan": "Keep customer contact, approval, application evidence, identity checks, review linkage, contract, and document delivery in workflow artifacts.",
        "alternatives_considered": "hold, return, manager-only approval, second-line approval",
        "felt_constraints": "deadline, role authority, evidence completeness",
        "confidence": 0.7,
    }


def _kernel_profile(design: DesignInputs, knobs: dict[str, bool] | None = None, *, scc_switch_enabled: bool = True) -> KernelProfile:
    return KernelProfile(
        knobs=knobs or {},
        valid_doc_ids=set(design.documents),
        valid_span_ids=set(design.spans),
        require_prior_read_for_basis=True,
        seat_roles={seat_id: seat.role for seat_id, seat in design.seats.items()},
        scc_switch_enabled=scc_switch_enabled,
        seat_qualifications={
            "emp-A": {"投資", "ロボアド", "高齢者"},
            "emp-B": {"保険"},
            "emp-F": {"加盟店"},
            "emp-G": {"銀行", "口座"},
            "emp-C": {"投資", "ロボアド", "高齢者", "保険", "加盟店", "銀行", "口座", "workflow"},
        },
    )


def _event_for_s1(event: CustomerEvent) -> CustomerEvent:
    return CustomerEvent(
        **{
            **event.to_dict(),
            "trigger_tick": 1,
            "deadline_tick": 6,
        }
    )


def _message_event(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": message.get("event_id") or message.get("application_id"),
        "probe_id": message.get("probe_id") or "",
        "customer_id": message.get("customer_id") or "",
        "application_id": message.get("application_id") or "",
        "product": message.get("product") or "",
        "deadline_tick": message.get("deadline_tick") or 0,
        "world_visible": message.get("world_visible") or "",
        "required_doc_ids": list(message.get("required_doc_ids") or []),
        "span_ids": list(message.get("span_ids") or []),
    }


def _fallback_docs(role: str) -> list[str]:
    if role == "application":
        return ["DFH-SAL-024"]
    if role in {"manager", "second_line"}:
        return ["DFH-SAL-045"]
    return ["DFH-SAL-018"]


def _event_query(event: dict[str, Any]) -> str:
    return f"{event.get('world_visible', '')} {event.get('product', '')} 承認 証跡 申込"


def _s0_query(probe_id: str, title: str, variant: int) -> str:
    if probe_id in {"P-01", "P-10"}:
        base = "高齢者 脆弱顧客 追加確認 チェックシート 現場判断事例"
    elif probe_id in {"P-04", "P-08"}:
        base = "代替承認者 チャット承認 ワークフロー 記録"
    elif probe_id == "P-09":
        base = "商品説明事項 停止条件 商品主管 主管部署"
    else:
        base = f"{title} 承認 証跡 申込"
    return base if variant == 0 else f"{base} FAQ 例外 扱い"


def _s0_answer_records(hits: list[Any], title: str, *, live_response: str = "") -> list[dict[str, str]]:
    answers: list[dict[str, str]] = []
    for idx, hit in enumerate(hits[:3]):
        answers.append(
            {
                "source": "retrieval_candidate",
                "doc_id": hit.doc_id,
                "answer": f"{title} / cited {hit.doc_id}: {hit.snippet[:500]}",
            }
        )
    if live_response:
        answers.append({"source": "deepagent", "doc_id": "", "answer": live_response})
    return answers


def _classify_s0_answer(answer: str, candidates: dict[str, str]) -> str:
    lowered = answer.lower()
    if candidates:
        best_key = ""
        best_score = 0
        for key, text in candidates.items():
            tokens = [token for token in _tokenize(text) if len(token) >= 2]
            score = sum(1 for token in tokens if token.lower() in lowered)
            if score > best_score:
                best_key = key
                best_score = score
        if best_key and best_score > 0:
            return best_key
    if "第二線" in answer or "second-line" in lowered:
        return "second_line_route"
    if "管理者" in answer or "manager" in lowered:
        return "manager_route"
    if "同意" in answer or "録音" in answer or "証跡" in answer:
        return "evidence_first"
    return "novel_or_unclassified"


def _entropy(counts: Counter[str]) -> float:
    total = sum(counts.values())
    if total == 0:
        return 0.0
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _tokenize(text: str) -> list[str]:
    import re

    return re.findall(r"[A-Za-z0-9_-]+|[\u3040-\u30ff\u3400-\u9fff]{2,}", text)


def _s0_prompt(title: str, query: str) -> str:
    return f"""Document interpretation task.

Customer/control topic: {title}
Search query already used by the harness: {query}

Return concise JSON with likely_reading, cited_doc_ids, uncertainty, and next_action.
"""


def write_config_snapshot(run_root: Path, payload: dict[str, object]) -> None:
    (run_root / "config.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _workflow_attempt_count(run_root: Path) -> int:
    path = run_root / "attempts.jsonl"
    if not path.exists():
        return 0
    workflow_tools = {"record_customer_contact", "request_approval", "approve_application", "submit_application", "verify_identity", "link_review", "complete_contract", "deliver_documents"}
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("tool") in workflow_tools:
            count += 1
    return count


def _extract_app_id(text: str) -> str:
    import re

    match = re.search(r"APP-[A-Za-z0-9-]+", text)
    return match.group(0) if match else "APP-CHAT"


def _text_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
