from __future__ import annotations

import json
from typing import Any

from .corpus import Corpus
from .kernel import WorldKernel, parse_json_arg
from .recorder import BasisRecord, RunRecorder, utc_now


# Single source of truth for "seat = role + tool bundle" (MASTER_DESIGN §6/§7).
# Exposure reflects what that role's system screens offer. submit_application is
# deliberately exposed to sales AND application: whether sales may actually
# submit is the K-sod-gate experiment knob, not a hard bundle restriction.
COMMON_TOOLS = ("search_corpus", "read_document", "record_interpretation_basis", "send_chat")
D4_TOOLS = ("note_to_self", "recall_notes")
ROLE_TOOL_BUNDLES: dict[str, tuple[str, ...]] = {
    "sales": COMMON_TOOLS + ("record_customer_contact", "request_approval", "submit_application"),
    "manager": COMMON_TOOLS + ("record_customer_contact", "request_approval", "approve_application", "return_application"),
    "second_line": COMMON_TOOLS + ("request_approval", "approve_application", "return_application"),
    "application": COMMON_TOOLS + ("submit_application", "verify_identity", "link_review", "complete_contract", "deliver_documents", "return_application"),
    "audit": COMMON_TOOLS,
}


def tools_for_role(role: str, *, d4_enabled: bool = True) -> tuple[str, ...]:
    bundle = ROLE_TOOL_BUNDLES.get(role, COMMON_TOOLS)
    return bundle + (D4_TOOLS if d4_enabled else ())


def build_role_tools(*, corpus: Corpus, kernel: WorldKernel, recorder: RunRecorder, seat_id: str, seat_role: str, include_workflow: bool = True, d4_enabled: bool = True):
    def search_corpus(query: str, top_k: int = 5) -> str:
        """Search world-visible control documents. Returns doc_id, title, score, and snippets."""
        if not recorder.consume_budget(seat_id, "search_corpus"):
            return json.dumps({"success": False, "denied_reason": "tick budget exceeded"}, ensure_ascii=False)
        hits_json = corpus.search_json(query, seat_role=seat_role, top_k=top_k)
        recorder.record_attempt(seat_id=seat_id, tool="search_corpus", args={"query": query, "top_k": top_k}, success=True, result=json.loads(hits_json))
        return hits_json

    def read_document(doc_id: str, query: str = "", max_chars: int = 4000) -> str:
        """Read a world-visible document by doc_id. Use search_corpus first when possible."""
        if not recorder.consume_budget(seat_id, "read_document"):
            return json.dumps({"success": False, "denied_reason": "tick budget exceeded"}, ensure_ascii=False)
        try:
            doc = corpus.get(doc_id)
        except KeyError:
            recorder.record_attempt(seat_id=seat_id, tool="read_document", args={"doc_id": doc_id}, success=False, result="", denied_reason="unknown doc_id")
            return f"unknown doc_id: {doc_id}"
        if not corpus.readable_by(doc_id, seat_role):
            recorder.record_attempt(seat_id=seat_id, tool="read_document", args={"doc_id": doc_id}, success=False, result="", denied_reason="document not in your library index")
            return json.dumps({"success": False, "denied_reason": "document not in your library index"}, ensure_ascii=False)
        text = doc.text
        if query:
            hits = corpus.search(query, seat_role=seat_role, top_k=10)
            for hit in hits:
                if hit.doc_id == doc_id:
                    text = hit.snippet
                    break
        result = text[:max_chars]
        recorder.record_attempt(seat_id=seat_id, tool="read_document", args={"doc_id": doc_id, "query": query, "max_chars": max_chars}, success=True, result={"chars": len(result)})
        return result

    def record_interpretation_basis(
        trigger_event: str,
        retrieved_json: str,
        construal: str,
        decision: str,
        evidence_plan: str,
        alternatives_considered: str = "",
        felt_constraints: str = "",
        confidence: float = 0.5,
    ) -> str:
        """Record structured basis for a control-relevant reading."""
        if not recorder.consume_budget(seat_id, "record_interpretation_basis"):
            return json.dumps({"success": False, "denied_reason": "tick budget exceeded"}, ensure_ascii=False)
        retrieved = _list_json(retrieved_json)
        basis = BasisRecord(
            basis_id=recorder.next_basis_id(),
            ts=utc_now(),
            run_id=recorder.run_id,
            tick=recorder.tick,
            seat_id=seat_id,
            action_id=None,
            trigger_event=trigger_event,
            retrieved=retrieved,
            construal=construal,
            decision=decision,
            evidence_plan=evidence_plan,
            alternatives_considered=alternatives_considered,
            felt_constraints=felt_constraints,
            confidence=confidence,
            grounded=True,
        )
        basis_id = recorder.record_basis(seat_id, basis)
        return json.dumps({"recorded": True, "basis_id": basis_id, "decision": decision}, ensure_ascii=False)

    def note_to_self(key: str, value: str) -> str:
        """Write a short private working note for yourself (only you can read it later)."""
        if not recorder.consume_budget(seat_id, "note_to_self"):
            return json.dumps({"success": False, "denied_reason": "tick budget exceeded"}, ensure_ascii=False)
        recorder.remember_private(seat_id=seat_id, key=key, value=value)
        return json.dumps({"noted": True, "key": key}, ensure_ascii=False)

    def recall_notes(limit: int = 5) -> str:
        """Read back your own recent private working notes (visible only to you)."""
        if not recorder.consume_budget(seat_id, "recall_notes"):
            return json.dumps({"success": False, "denied_reason": "tick budget exceeded"}, ensure_ascii=False)
        notes = recorder.read_private(seat_id=seat_id, limit=limit)
        return json.dumps({"notes": notes}, ensure_ascii=False)

    def send_chat(to_seat: str, channel: str, body: str) -> str:
        """Send a world-visible chat or email message to another seat."""
        if not recorder.consume_budget(seat_id, "send_chat"):
            return json.dumps({"success": False, "denied_reason": "tick budget exceeded"}, ensure_ascii=False)
        return json.dumps(kernel.send_chat(seat_id, to_seat, channel, body), ensure_ascii=False)

    def record_customer_contact(customer_id: str, channel: str, summary: str, basis_json: str) -> str:
        """Record a customer contact event. basis_json is required for control-relevant contacts."""
        if not recorder.consume_budget(seat_id, "record_customer_contact"):
            return json.dumps({"success": False, "denied_reason": "tick budget exceeded"}, ensure_ascii=False)
        result = kernel.record_customer_contact(seat_id, customer_id, channel, summary, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def request_approval(application_id: str, approver_role: str, reason: str, basis_json: str) -> str:
        """Request approval through the workflow. basis_json is required."""
        if not recorder.consume_budget(seat_id, "request_approval"):
            return json.dumps({"success": False, "denied_reason": "tick budget exceeded"}, ensure_ascii=False)
        result = kernel.request_approval(seat_id, application_id, approver_role, reason, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def submit_application(application_id: str, customer_id: str, product: str, evidence_json: str, basis_json: str) -> str:
        """Submit an application. Evidence may include consent_log_id, recording_id, and material_version."""
        if not recorder.consume_budget(seat_id, "submit_application"):
            return json.dumps({"success": False, "denied_reason": "tick budget exceeded"}, ensure_ascii=False)
        result = kernel.submit_application(seat_id, application_id, customer_id, product, parse_json_arg(evidence_json), parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def approve_application(application_id: str, approval_id: str, condition: str, basis_json: str) -> str:
        """Approve an application through a world-visible workflow record."""
        if not recorder.consume_budget(seat_id, "approve_application"):
            return json.dumps({"success": False, "denied_reason": "tick budget exceeded"}, ensure_ascii=False)
        result = kernel.approve_application(seat_id, application_id, approval_id, condition, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def return_application(application_id: str, reason: str, basis_json: str) -> str:
        """Return an application through the workflow when evidence or approval is insufficient."""
        if not recorder.consume_budget(seat_id, "return_application"):
            return json.dumps({"success": False, "denied_reason": "tick budget exceeded"}, ensure_ascii=False)
        result = kernel.return_application(seat_id, application_id, reason, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def verify_identity(application_id: str, ekyc_completed: bool, sanctions_non_hit: bool, consent_log_id: str, basis_json: str) -> str:
        """Record identity, consent, and sanctions checks before review linkage."""
        if not recorder.consume_budget(seat_id, "verify_identity"):
            return json.dumps({"success": False, "denied_reason": "tick budget exceeded"}, ensure_ascii=False)
        result = kernel.verify_identity(seat_id, application_id, ekyc_completed, sanctions_non_hit, consent_log_id, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def link_review(application_id: str, review_ticket_id: str, basis_json: str) -> str:
        """Link the application to review after hard-guard evidence is present."""
        if not recorder.consume_budget(seat_id, "link_review"):
            return json.dumps({"success": False, "denied_reason": "tick budget exceeded"}, ensure_ascii=False)
        result = kernel.link_review(seat_id, application_id, review_ticket_id, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def complete_contract(application_id: str, contract_id: str, basis_json: str) -> str:
        """Complete a contract after review linkage."""
        if not recorder.consume_budget(seat_id, "complete_contract"):
            return json.dumps({"success": False, "denied_reason": "tick budget exceeded"}, ensure_ascii=False)
        result = kernel.complete_contract(seat_id, application_id, contract_id, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def deliver_documents(application_id: str, delivery_id: str, basis_json: str) -> str:
        """Record required document delivery after contract completion."""
        if not recorder.consume_budget(seat_id, "deliver_documents"):
            return json.dumps({"success": False, "denied_reason": "tick budget exceeded"}, ensure_ascii=False)
        result = kernel.deliver_documents(seat_id, application_id, delivery_id, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    all_tools = {
        tool.__name__: tool
        for tool in (
            search_corpus, read_document, record_interpretation_basis, note_to_self, recall_notes,
            send_chat, record_customer_contact, request_approval, submit_application,
            approve_application, return_application, verify_identity, link_review,
            complete_contract, deliver_documents,
        )
    }
    if include_workflow:
        allowed = tools_for_role(seat_role, d4_enabled=d4_enabled)
    else:
        # S0: reading/basis tools only (no chat/workflow)
        allowed = ("search_corpus", "read_document", "record_interpretation_basis") + (D4_TOOLS if d4_enabled else ())
    return [all_tools[name] for name in allowed if name in all_tools]



def _list_json(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return [{"raw": value}]
    if isinstance(parsed, list):
        return [item if isinstance(item, dict) else {"value": item} for item in parsed]
    if isinstance(parsed, dict):
        return [parsed]
    return [{"value": parsed}]
