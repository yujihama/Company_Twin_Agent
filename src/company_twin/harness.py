from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .agents import create_seat_agent, invoke_agent
from .corpus import Corpus
from .deck import CustomerEvent, build_customer_deck, customer_event_for_inbox, event_for_probe
from .design_loader import DesignInputs
from .env import normalize_openrouter_model
from .kernel import KernelProfile, WorldKernel
from .recorder import BasisRecord, RunRecorder, utc_now
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
    span_id = probe.binds[0] if probe.binds else ""
    interpretation_class = _interpretation_class(probe_id, seat.role, variant)
    basis = BasisRecord(
        basis_id=recorder.next_basis_id(),
        ts=utc_now(),
        run_id=recorder.run_id,
        tick=recorder.tick,
        seat_id=seat_id,
        action_id=None,
        trigger_event=f"s0:{probe_id}",
        retrieved=[{"doc_id": hits[0].doc_id, "version": corpus.get(hits[0].doc_id).meta.version, "span_id": span_id}] if hits else [],
        construal=f"{probe.title} is read as {interpretation_class}",
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
        "entropy": "0.69" if variant else "0.31",
        "run_root": str(run_root),
    }
    recorder.write_json("s0_result.json", result)
    if live:
        kernel = WorldKernel(recorder, _kernel_profile(design))
        tools = build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, seat_role=seat.role)
        agent = create_seat_agent(seat_id=seat_id, role=seat.role, tools=tools, model=model_name)
        output = invoke_agent(agent, _s0_prompt(probe.title, query), recursion_limit=60)
        recorder.append_ledger("s0_agent_response", {"seat_id": seat_id, "probe_id": probe_id, "response": output})
        result["response"] = output
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
    write_config_snapshot(run_root, build_world_config(design, stage="S1", model=model_name, seed=seed, ticks=ticks, probe_id=probe_id, seat_id=seat_id, knobs=knobs or {}))
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
    write_config_snapshot(run_root, build_world_config(design, stage="S2", model=model_name, seed=seed, ticks=ticks, anchor=anchor, knobs=knobs or {}))
    kernel = WorldKernel(recorder, _kernel_profile(design, knobs=knobs or {}))
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
            payload = customer_event_for_inbox(event)
            kernel.record_customer_event(payload)
            kernel.enqueue_inbox(event.primary_seat, payload)
            recorder.append_ledger("latent_truth_committed", {"event_id": event.event_id, "customer_id": event.customer_id, "latent_truth_hash": _text_hash(event.latent_truth)})
        for seat_id in list(kernel.inbox_nonempty_seats()):
            messages = kernel.pop_inbox(seat_id)
            for message in messages:
                _process_message(design, corpus, kernel, recorder, seat_id, message)
                if live and agent_calls < live_budget:
                    _invoke_live_agent_for_message(design, corpus, kernel, recorder, seat_id, message, model)
                    agent_calls += 1
        recorder.append_ledger("tick_committed", {"tick": tick})
    return agent_calls


def _process_message(design: DesignInputs, corpus: Corpus, kernel: WorldKernel, recorder: RunRecorder, seat_id: str, message: dict[str, Any]) -> None:
    seat = design.seats.get(seat_id)
    role = seat.role if seat else ""
    if message.get("kind") == "chat":
        _process_chat_message(design, corpus, kernel, recorder, seat_id, message)
        return
    if message.get("kind") != "customer_event":
        return
    event = _message_event(message)
    docs = event["required_doc_ids"] or _fallback_docs(role)
    first_doc = docs[0]
    query = _event_query(event)
    hits = corpus.search(query, seat_role=role, top_k=5)
    recorder.record_attempt(seat_id=seat_id, tool="search_corpus", args={"query": query, "top_k": 5}, success=True, result=[hit.__dict__ for hit in hits])
    doc_id = first_doc if first_doc in corpus.documents else (hits[0].doc_id if hits else "DFH-SAL-018")
    snippet = corpus.get(doc_id).text[:1200]
    recorder.record_attempt(seat_id=seat_id, tool="read_document", args={"doc_id": doc_id, "query": query, "max_chars": 1200}, success=True, result={"chars": len(snippet), "version": corpus.get(doc_id).meta.version})
    basis = _basis(corpus, doc_id, event, decision="contact-and-escalate")
    kernel.record_customer_contact(seat_id, event["customer_id"], "phone", f"{event['world_visible']} / next step recorded", basis)
    if role == "sales":
        approver_role = "second_line" if event["probe_id"] in {"P-03", "P-04", "P-08", "P-09"} else "manager"
        approval = kernel.request_approval(seat_id, event["application_id"], approver_role, f"{event['probe_id']} requires documented approval route", basis)
        to_seat = "emp-Q" if approver_role == "second_line" else "emp-M"
        kernel.send_chat(seat_id, to_seat, "workflow", f"Please review {event['application_id']} for {event['probe_id']}. approval={approval.get('approval_id', '')}")
        kernel.enqueue_inbox("emp-C", {"kind": "application_work", **event})
    elif role == "second_line":
        kernel.approve_application(seat_id, event["application_id"], f"APR-{recorder.tick:04d}", "second-line review recorded", basis)
        kernel.send_chat(seat_id, "emp-C", "workflow", f"Second-line approval completed for {event['application_id']}")
    elif role == "manager":
        kernel.approve_application(seat_id, event["application_id"], f"APR-{recorder.tick:04d}", "manager approval recorded", basis)
        kernel.send_chat(seat_id, "emp-C", "workflow", f"Manager approval completed for {event['application_id']}")
    elif role == "application":
        _application_progress(corpus, kernel, recorder, seat_id, event, basis)


def _process_chat_message(design: DesignInputs, corpus: Corpus, kernel: WorldKernel, recorder: RunRecorder, seat_id: str, message: dict[str, Any]) -> None:
    role = design.seats.get(seat_id).role if seat_id in design.seats else ""
    body = str(message.get("body") or "")
    app_id = _extract_app_id(body)
    doc_id = "DFH-SAL-045" if role in {"manager", "second_line"} else "DFH-SAL-024"
    query = "承認 差戻し 証跡 申込"
    recorder.record_attempt(seat_id=seat_id, tool="search_corpus", args={"query": query, "top_k": 5}, success=True, result=[hit.__dict__ for hit in corpus.search(query, seat_role=role, top_k=5)])
    recorder.record_attempt(seat_id=seat_id, tool="read_document", args={"doc_id": doc_id, "query": query, "max_chars": 1200}, success=True, result={"chars": min(len(corpus.get(doc_id).text), 1200), "version": corpus.get(doc_id).meta.version})
    event = {"application_id": app_id, "customer_id": app_id.replace("APP", "CUS"), "product": "workflow item", "probe_id": "chat", "span_ids": [], "required_doc_ids": [doc_id], "world_visible": body}
    basis = _basis(corpus, doc_id, event, decision="workflow-review")
    if role in {"manager", "second_line"}:
        kernel.approve_application(seat_id, app_id, f"APR-{recorder.tick:04d}", "reviewed via workflow chat", basis)
        kernel.send_chat(seat_id, "emp-C", "workflow", f"Approval recorded for {app_id}")
    elif role == "application":
        _application_progress(corpus, kernel, recorder, seat_id, event, basis)


def _application_progress(corpus: Corpus, kernel: WorldKernel, recorder: RunRecorder, seat_id: str, event: dict[str, Any], basis: dict[str, Any]) -> None:
    evidence = {"material_version": "v1.1", "recording_id": f"REC-{event['application_id']}", "consent_log_id": f"CONS-{event['application_id']}"}
    kernel.submit_application(seat_id, event["application_id"], event["customer_id"], event["product"], evidence, basis)
    kernel.verify_identity(seat_id, event["application_id"], True, True, evidence["consent_log_id"], basis)
    kernel.link_review(seat_id, event["application_id"], f"REV-{event['application_id']}", basis)
    kernel.complete_contract(seat_id, event["application_id"], f"CON-{event['application_id']}", basis)
    kernel.deliver_documents(seat_id, event["application_id"], f"DEL-{event['application_id']}", basis)


def _invoke_live_agent_for_message(design: DesignInputs, corpus: Corpus, kernel: WorldKernel, recorder: RunRecorder, seat_id: str, message: dict[str, Any], model: str) -> None:
    seat = design.seats.get(seat_id)
    if not seat:
        return
    tools = build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, seat_role=seat.role)
    agent = create_seat_agent(seat_id=seat_id, role=seat.role, tools=tools, model=model)
    prompt = f"""You have one inbox item in the DFH workflow.

Inbox item:
{json.dumps(message, ensure_ascii=False)}

Process only this item through world-visible tools. If the deterministic workflow has already progressed it, add only missing evidence or a concise status note.
"""
    output = invoke_agent(agent, prompt, recursion_limit=20)
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


def _kernel_profile(design: DesignInputs, knobs: dict[str, bool] | None = None) -> KernelProfile:
    return KernelProfile(
        knobs=knobs or {},
        valid_doc_ids=set(design.documents),
        valid_span_ids=set(design.spans),
        require_prior_read_for_basis=True,
        seat_roles={seat_id: seat.role for seat_id, seat in design.seats.items()},
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


def _interpretation_class(probe_id: str, role: str, variant: int) -> str:
    if probe_id in {"P-01", "P-10"}:
        return "age-threshold-plus-understanding" if variant == 0 else "understanding-first"
    if probe_id in {"P-04", "P-08"}:
        return "second-line-route" if role == "second_line" else "documented-substitute-approval"
    if probe_id == "P-09":
        return "route-to-product-owner-with-second-line-copy"
    return "manager-escalation-with-evidence"


def _s0_prompt(title: str, query: str) -> str:
    return f"""Document interpretation task.

Customer/control topic: {title}
Search query already used by the harness: {query}

Return concise JSON with likely_reading, cited_doc_ids, uncertainty, and next_action.
"""


def write_config_snapshot(run_root: Path, payload: dict[str, object]) -> None:
    (run_root / "config.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_app_id(text: str) -> str:
    import re

    match = re.search(r"APP-[A-Za-z0-9-]+", text)
    return match.group(0) if match else "APP-CHAT"


def _text_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
