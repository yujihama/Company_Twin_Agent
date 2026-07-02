from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .recorder import RunRecorder


CONTROLLED_TOOLS = {"request_approval", "submit_application", "complete_application", "record_customer_contact"}


@dataclass
class KernelProfile:
    name: str = "erp_standard"
    knobs: dict[str, bool] = field(default_factory=dict)

    def enabled(self, knob: str) -> bool:
        return bool(self.knobs.get(knob, False))


class WorldKernel:
    def __init__(self, recorder: RunRecorder, profile: KernelProfile | None = None):
        self.recorder = recorder
        self.profile = profile or KernelProfile()
        self.applications: dict[str, dict[str, Any]] = {}
        self.event_counter = 0

    def fire_timed_events(self, tick: int) -> None:
        self.recorder.set_tick(tick)
        if tick in {2, 4, 6}:
            self.recorder.append_ledger("daily_inbox_delivery", {"tick": tick})
        if self.profile.enabled("K-completion-gate") and tick == 4:
            self.recorder.append_ledger("completion_gate_active", {"knob": "K-completion-gate"})

    def send_chat(self, seat_id: str, to_seat: str, channel: str, body: str) -> dict[str, Any]:
        self.recorder.record_chat(from_seat=seat_id, to_seat=to_seat, channel=channel, body=body)
        self.recorder.record_attempt(seat_id=seat_id, tool="send_chat", args={"to_seat": to_seat, "channel": channel}, success=True, result={"sent": True})
        return {"sent": True}

    def record_customer_contact(self, seat_id: str, customer_id: str, channel: str, summary: str, basis: dict[str, Any]) -> dict[str, Any]:
        if not _valid_basis(basis):
            return self._denied(seat_id, "record_customer_contact", {"customer_id": customer_id}, "basis is required and must include retrieved, construal, decision")
        self.event_counter += 1
        event_id = f"EVT-{self.event_counter:06d}"
        payload = {"event_id": event_id, "customer_id": customer_id, "channel": channel, "summary": summary}
        self.recorder.append_ledger("customer_contact", payload)
        self.recorder.record_attempt(seat_id=seat_id, tool="record_customer_contact", args=payload, success=True, result=payload)
        return payload

    def request_approval(self, seat_id: str, application_id: str, approver_role: str, reason: str, basis: dict[str, Any]) -> dict[str, Any]:
        if not _valid_basis(basis):
            return self._denied(seat_id, "request_approval", {"application_id": application_id}, "basis is required and must include retrieved, construal, decision")
        payload = {"application_id": application_id, "approver_role": approver_role, "reason": reason, "status": "requested"}
        self.recorder.append_ledger("approval_requested", payload)
        self.recorder.record_attempt(seat_id=seat_id, tool="request_approval", args=payload, success=True, result=payload)
        return payload

    def submit_application(self, seat_id: str, application_id: str, customer_id: str, product: str, evidence: dict[str, Any], basis: dict[str, Any]) -> dict[str, Any]:
        if not _valid_basis(basis):
            return self._denied(seat_id, "submit_application", {"application_id": application_id}, "basis is required and must include retrieved, construal, decision")
        required_values = {"application_id": application_id, "customer_id": customer_id, "product": product}
        missing = [name for name, value in required_values.items() if not value]
        if missing:
            return self._denied(seat_id, "submit_application", {"application_id": application_id}, f"missing required fields: {', '.join(missing)}")
        material_version = str(evidence.get("material_version", ""))
        if self.profile.enabled("K-material-picker") and material_version.lower().startswith(("draft", "unapproved", "未承認")):
            return self._denied(seat_id, "submit_application", {"application_id": application_id}, "unapproved material version is blocked by K-material-picker")
        if self.profile.enabled("K-completion-gate"):
            required = {"consent_log_id", "recording_id", "material_version"}
            missing_evidence = sorted(required - set(k for k, v in evidence.items() if v))
            if missing_evidence:
                return self._denied(seat_id, "submit_application", {"application_id": application_id}, f"missing completion evidence: {', '.join(missing_evidence)}")
        payload = {
            "application_id": application_id,
            "customer_id": customer_id,
            "product": product,
            "evidence": evidence,
            "status": "application_received",
        }
        self.applications[application_id] = payload
        self.recorder.append_ledger("application_submitted", payload)
        self.recorder.record_attempt(seat_id=seat_id, tool="submit_application", args=payload, success=True, result=payload)
        return payload

    def _denied(self, seat_id: str, tool: str, args: dict[str, Any], reason: str) -> dict[str, Any]:
        result = {"success": False, "denied_reason": reason}
        self.recorder.record_attempt(seat_id=seat_id, tool=tool, args=args, success=False, result=result, denied_reason=reason)
        self.recorder.append_ledger("permission_denied", {"seat_id": seat_id, "tool": tool, "reason": reason, "args": args})
        return result


def parse_json_arg(value: str | dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"_raw": value}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _valid_basis(basis: dict[str, Any]) -> bool:
    if not basis:
        return False
    retrieved = basis.get("retrieved")
    return bool(retrieved) and bool(basis.get("construal")) and bool(basis.get("decision"))
