from __future__ import annotations

import json
import re
from typing import Any, Callable

from .corpus import Corpus
from .design_loader import DesignInputs
from .kernel import WorldKernel
from .recorder import RunRecorder
from .tools import build_role_tools


ToolMap = dict[str, Callable[..., str]]


def run_policy_seat_turn(*, design: DesignInputs, corpus: Corpus, kernel: WorldKernel, recorder: RunRecorder, seat_id: str, message: dict[str, Any]) -> None:
    seat = design.seats.get(seat_id)
    if not seat:
        return
    tools = _tool_map(build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, seat_role=seat.role))
    with recorder.origin("agent_policy"):
        if message.get("kind") == "customer_utterance":
            _handle_customer_utterance(tools, corpus, seat.role, seat_id, message)
        elif message.get("kind") == "chat":
            _handle_chat(tools, corpus, seat.role, seat_id, message)
        elif message.get("kind") == "application_work":
            _handle_application_work(tools, corpus, seat_id, message)
        if seat.role in {"sales", "manager", "second_line"}:
            recorder.remember_private(seat_id=seat_id, key=f"tick-{recorder.tick}-{message.get('kind', 'item')}", value=f"processed {message.get('event_id') or message.get('application_id') or message.get('kind')}")


def _handle_customer_utterance(tools: ToolMap, corpus: Corpus, role: str, seat_id: str, message: dict[str, Any]) -> None:
    event = _message_event(message)
    query = f"{event['world_visible']} {event['product']} 承認 証跡 申込"
    hits = _parse_json(tools["search_corpus"](query, 5), [])
    doc_id = _choose_doc(event, hits, role)
    tools["read_document"](doc_id, query, 1200)
    basis = _basis(corpus, doc_id, event, decision="contact-and-escalate")
    tools["record_customer_contact"](event["customer_id"], "phone", f"{event['world_visible']} / customer response logged", json.dumps(basis, ensure_ascii=False))
    if role == "sales":
        approver_role = "second_line" if event["probe_id"] in {"P-03", "P-04", "P-08", "P-09"} else "manager"
        approval = _parse_json(tools["request_approval"](event["application_id"], approver_role, f"{event['probe_id']} requires documented approval route", json.dumps(basis, ensure_ascii=False)), {})
        to_seat = "emp-Q" if approver_role == "second_line" else "emp-M"
        tools["send_chat"](to_seat, "workflow", f"Please review {event['application_id']} for {event['probe_id']}. approval={approval.get('approval_id', '')}")
        tools["send_chat"]("emp-C", "workflow", f"Please prepare application evidence for {event['application_id']} after approval.")


def _handle_chat(tools: ToolMap, corpus: Corpus, role: str, seat_id: str, message: dict[str, Any]) -> None:
    body = str(message.get("body") or "")
    app_id = _extract_app_id(body)
    doc_id = "DFH-SAL-045" if role in {"manager", "second_line"} else "DFH-SAL-024"
    query = "承認 差戻し 証跡 申込"
    tools["search_corpus"](query, 5)
    tools["read_document"](doc_id, query, 1200)
    event = {"application_id": app_id, "customer_id": app_id.replace("APP", "CUS"), "product": "workflow item", "probe_id": "chat", "span_ids": [], "required_doc_ids": [doc_id], "world_visible": body}
    basis = _basis(corpus, doc_id, event, decision="workflow-review")
    if role in {"manager", "second_line"}:
        tools["approve_application"](app_id, f"APR-{message.get('tick', 0):04d}", "reviewed via workflow chat", json.dumps(basis, ensure_ascii=False))
        tools["send_chat"]("emp-C", "workflow", f"Approval recorded for {app_id}")
    elif role == "application":
        _handle_application_work(tools, corpus, seat_id, event)


def _handle_application_work(tools: ToolMap, corpus: Corpus, seat_id: str, message: dict[str, Any]) -> None:
    event = _message_event(message)
    doc_id = "DFH-SAL-024"
    query = "申込 本人確認 同意ログ 審査連携 書面交付"
    tools["search_corpus"](query, 5)
    tools["read_document"](doc_id, query, 1200)
    basis = _basis(corpus, doc_id, event, decision="application-processing")
    basis_json = json.dumps(basis, ensure_ascii=False)
    evidence = {
        "material_version": "v1.1",
        "recording_id": f"REC-{event['application_id']}",
        "consent_log_id": f"CONS-{event['application_id']}",
        "checksheet_status": "completed",
    }
    tools["submit_application"](event["application_id"], event["customer_id"], event["product"], json.dumps(evidence, ensure_ascii=False), basis_json)
    tools["verify_identity"](event["application_id"], True, True, evidence["consent_log_id"], basis_json)
    tools["link_review"](event["application_id"], f"REV-{event['application_id']}", basis_json)
    tools["complete_contract"](event["application_id"], f"CON-{event['application_id']}", basis_json)
    tools["deliver_documents"](event["application_id"], f"DEL-{event['application_id']}", basis_json)


def _tool_map(tools: list[Callable[..., str]]) -> ToolMap:
    return {tool.__name__: tool for tool in tools}


def _message_event(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": message.get("event_id") or message.get("application_id"),
        "probe_id": message.get("probe_id") or "",
        "customer_id": message.get("customer_id") or "",
        "application_id": message.get("application_id") or _extract_app_id(str(message.get("body") or "")),
        "product": message.get("product") or "workflow item",
        "deadline_tick": message.get("deadline_tick") or 0,
        "world_visible": message.get("world_visible") or message.get("utterance") or str(message.get("body") or ""),
        "required_doc_ids": list(message.get("required_doc_ids") or []),
        "span_ids": list(message.get("span_ids") or []),
    }


def _choose_doc(event: dict[str, Any], hits: list[dict[str, Any]], role: str) -> str:
    for doc_id in event.get("required_doc_ids") or []:
        if doc_id:
            return doc_id
    if hits:
        return str(hits[0].get("doc_id") or "DFH-SAL-018")
    if role == "application":
        return "DFH-SAL-024"
    if role in {"manager", "second_line"}:
        return "DFH-SAL-045"
    return "DFH-SAL-018"


def _basis(corpus: Corpus, doc_id: str, event: dict[str, Any], *, decision: str) -> dict[str, Any]:
    span_ids = event.get("span_ids") or []
    doc = corpus.get(doc_id)
    return {
        "trigger_event": event.get("event_id") or event.get("application_id"),
        "retrieved": [{"doc_id": doc_id, "version": doc.meta.version, "span_id": span_ids[0] if span_ids else ""}],
        "construal": f"{event.get('probe_id')} requires {decision} based on read document.",
        "decision": decision,
        "evidence_plan": "Keep customer contact, approval, application evidence, identity checks, review linkage, contract, and document delivery in workflow artifacts.",
        "alternatives_considered": "hold, return, manager-only approval, second-line approval",
        "felt_constraints": "deadline, role authority, evidence completeness",
        "confidence": 0.7,
    }


def _parse_json(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _extract_app_id(text: str) -> str:
    match = re.search(r"APP-[A-Za-z0-9-]+", text)
    return match.group(0) if match else "APP-CHAT"
