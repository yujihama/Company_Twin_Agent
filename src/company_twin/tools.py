from __future__ import annotations

import json
from typing import Any

from .corpus import Corpus
from .kernel import WorldKernel, parse_json_arg
from .recorder import BasisRecord, RunRecorder, utc_now


def build_role_tools(*, corpus: Corpus, kernel: WorldKernel, recorder: RunRecorder, seat_id: str, seat_role: str):
    def search_corpus(query: str, top_k: int = 5) -> str:
        """Search world-visible control documents. Returns doc_id, title, score, and snippets."""
        hits_json = corpus.search_json(query, seat_role=seat_role, top_k=top_k)
        recorder.record_attempt(seat_id=seat_id, tool="search_corpus", args={"query": query, "top_k": top_k}, success=True, result=json.loads(hits_json))
        return hits_json

    def read_document(doc_id: str, query: str = "", max_chars: int = 4000) -> str:
        """Read a world-visible document by doc_id. Use search_corpus first when possible."""
        try:
            doc = corpus.get(doc_id)
        except KeyError:
            recorder.record_attempt(seat_id=seat_id, tool="read_document", args={"doc_id": doc_id}, success=False, result="", denied_reason="unknown doc_id")
            return f"unknown doc_id: {doc_id}"
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

    def send_chat(to_seat: str, channel: str, body: str) -> str:
        """Send a world-visible chat or email message to another seat."""
        return json.dumps(kernel.send_chat(seat_id, to_seat, channel, body), ensure_ascii=False)

    def record_customer_contact(customer_id: str, channel: str, summary: str, basis_json: str) -> str:
        """Record a customer contact event. basis_json is required for control-relevant contacts."""
        result = kernel.record_customer_contact(seat_id, customer_id, channel, summary, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def request_approval(application_id: str, approver_role: str, reason: str, basis_json: str) -> str:
        """Request approval through the workflow. basis_json is required."""
        result = kernel.request_approval(seat_id, application_id, approver_role, reason, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def submit_application(application_id: str, customer_id: str, product: str, evidence_json: str, basis_json: str) -> str:
        """Submit an application. Evidence may include consent_log_id, recording_id, and material_version."""
        result = kernel.submit_application(seat_id, application_id, customer_id, product, parse_json_arg(evidence_json), parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def approve_application(application_id: str, approval_id: str, condition: str, basis_json: str) -> str:
        """Approve an application through a world-visible workflow record."""
        result = kernel.approve_application(seat_id, application_id, approval_id, condition, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def return_application(application_id: str, reason: str, basis_json: str) -> str:
        """Return an application through the workflow when evidence or approval is insufficient."""
        result = kernel.return_application(seat_id, application_id, reason, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def verify_identity(application_id: str, ekyc_completed: bool, sanctions_non_hit: bool, consent_log_id: str, basis_json: str) -> str:
        """Record identity, consent, and sanctions checks before review linkage."""
        result = kernel.verify_identity(seat_id, application_id, ekyc_completed, sanctions_non_hit, consent_log_id, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def link_review(application_id: str, review_ticket_id: str, basis_json: str) -> str:
        """Link the application to review after hard-guard evidence is present."""
        result = kernel.link_review(seat_id, application_id, review_ticket_id, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def complete_contract(application_id: str, contract_id: str, basis_json: str) -> str:
        """Complete a contract after review linkage."""
        result = kernel.complete_contract(seat_id, application_id, contract_id, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    def deliver_documents(application_id: str, delivery_id: str, basis_json: str) -> str:
        """Record required document delivery after contract completion."""
        result = kernel.deliver_documents(seat_id, application_id, delivery_id, parse_json_arg(basis_json))
        return json.dumps(result, ensure_ascii=False)

    return [
        search_corpus,
        read_document,
        record_interpretation_basis,
        send_chat,
        record_customer_contact,
        request_approval,
        submit_application,
        approve_application,
        return_application,
        verify_identity,
        link_review,
        complete_contract,
        deliver_documents,
    ]


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
