from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Only these origins may ever appear on records. Removed scripted seat paths are
# intentionally NOT represented here: any attempt to record under a non-world
# origin must fail loudly instead of polluting measurements.
ALLOWED_ORIGINS = frozenset({"system", "agent", "customer"})


@dataclass
class AttemptRecord:
    ts: str
    run_id: str
    tick: int
    seat_id: str
    tool: str
    args: dict[str, Any]
    success: bool
    result: Any
    denied_reason: str | None = None
    origin: str = "system"


@dataclass
class BasisRecord:
    basis_id: str
    ts: str
    run_id: str
    tick: int
    seat_id: str
    action_id: str | None
    trigger_event: str
    retrieved: list[dict[str, Any]]
    construal: str
    decision: str
    evidence_plan: str
    alternatives_considered: str = ""
    felt_constraints: str = ""
    confidence: float | None = None
    grounded: bool | None = None
    g1_span_exists: bool | None = None
    g1_citation_handle_exists: bool | None = None
    g2_prior_read: bool | None = None
    g3_entailment: str = "not_evaluated"
    g3_machine_heuristic: str = "not_evaluated"


class RunRecorder:
    def __init__(self, run_root: Path, run_id: str, meta: dict[str, Any] | None = None):
        self.run_root = run_root
        self.run_id = run_id
        self.tick = 0
        self._prev_hash = ""
        self._basis_counter = 0
        self._read_docs: dict[str, set[str]] = {}
        self._read_handles: dict[str, dict[str, dict[str, Any]]] = {}
        self._origin = "system"
        self._tick_budgets: dict[str, int] = {}
        self._private_store: dict[str, list[dict[str, Any]]] = {}
        self._tick_usage: dict[tuple[int, str], int] = {}
        run_root.mkdir(parents=True, exist_ok=True)
        self.write_json("meta.json", {"run_id": run_id, "created_at": utc_now(), **(meta or {})})
        for name in ("attempts.jsonl", "basis_records.jsonl", "chat_channel.jsonl", "world_ledger.jsonl", "store_events.jsonl"):
            (self.run_root / name).touch(exist_ok=True)

    def set_tick(self, tick: int) -> None:
        self.tick = tick

    @contextmanager
    def origin(self, origin: str) -> Iterator[None]:
        if origin not in ALLOWED_ORIGINS:
            raise ValueError(f"origin '{origin}' is not allowed; allowed={sorted(ALLOWED_ORIGINS)}")
        previous = self._origin
        self._origin = origin
        try:
            yield
        finally:
            self._origin = previous

    def configure_tick_budgets(self, budgets: dict[str, int]) -> None:
        self._tick_budgets = {seat_id: int(value) for seat_id, value in budgets.items()}

    def consume_budget(self, seat_id: str, tool: str) -> bool:
        budget = self._tick_budgets.get(seat_id)
        if budget is None:
            return True
        key = (self.tick, seat_id)
        used = self._tick_usage.get(key, 0)
        if used >= budget:
            self.record_attempt(
                seat_id=seat_id,
                tool=tool,
                args={"tick_budget": budget, "used": used},
                success=False,
                result={"success": False, "denied_reason": "tick budget exceeded"},
                denied_reason="tick budget exceeded",
            )
            self.append_ledger("permission_denied", {"seat_id": seat_id, "tool": tool, "reason": "tick budget exceeded", "budget": budget, "used": used})
            return False
        self._tick_usage[key] = used + 1
        return True

    def budget_left(self, seat_id: str) -> int:
        budget = self._tick_budgets.get(seat_id)
        if budget is None:
            return 999
        return max(budget - self._tick_usage.get((self.tick, seat_id), 0), 0)

    def record_attempt(
        self,
        *,
        seat_id: str,
        tool: str,
        args: dict[str, Any],
        success: bool,
        result: Any,
        denied_reason: str | None = None,
    ) -> AttemptRecord:
        record = AttemptRecord(
            ts=utc_now(),
            run_id=self.run_id,
            tick=self.tick,
            seat_id=seat_id,
            tool=tool,
            args=args,
            success=success,
            result=result,
            denied_reason=denied_reason,
            origin=self._origin,
        )
        self.append_jsonl("attempts.jsonl", asdict(record))
        if tool == "read_document" and success:
            doc_id = str((args or {}).get("doc_id") or "")
            if doc_id:
                self._read_docs.setdefault(seat_id, set()).add(doc_id)
            result_dict = result if isinstance(result, dict) else {}
            citation_handle = str(result_dict.get("citation_handle") or "")
            if citation_handle:
                self._read_handles.setdefault(seat_id, {})[citation_handle] = {
                    "doc_id": doc_id,
                    "version": str(result_dict.get("version") or ""),
                    "text": str(result_dict.get("text") or result_dict.get("snippet") or ""),
                    "tick": self.tick,
                }
        return record

    def next_basis_id(self) -> str:
        self._basis_counter += 1
        return f"BASIS-{self._basis_counter:06d}"

    def has_read_doc(self, seat_id: str, doc_id: str) -> bool:
        return doc_id in self._read_docs.get(seat_id, set())

    def has_citation_handle(self, seat_id: str, citation_handle: str) -> bool:
        return citation_handle in self._read_handles.get(seat_id, {})

    def read_for_handle(self, seat_id: str, citation_handle: str) -> dict[str, Any] | None:
        return self._read_handles.get(seat_id, {}).get(citation_handle)

    def record_basis(self, seat_id: str, basis: BasisRecord) -> str:
        self.append_jsonl("basis_records.jsonl", asdict(basis))
        self.record_attempt(
            seat_id=seat_id,
            tool="record_interpretation_basis",
            args={"basis_id": basis.basis_id, "trigger_event": basis.trigger_event, "action_id": basis.action_id},
            success=True,
            result={"basis_id": basis.basis_id, "decision": basis.decision, "confidence": basis.confidence, "grounded": basis.grounded},
        )
        return basis.basis_id

    def record_chat(self, *, from_seat: str, to_seat: str, channel: str, body: str) -> None:
        payload = {"ts": utc_now(), "run_id": self.run_id, "tick": self.tick, "from": from_seat, "to": to_seat, "channel": channel, "body": body}
        self.append_jsonl("chat_channel.jsonl", payload)
        self.append_ledger("chat_message", payload)

    def record_inbox(self, *, to_seat: str, message: dict[str, Any]) -> None:
        payload = {"to_seat": to_seat, "message": message}
        self.append_ledger("inbox_delivered", payload)

    def remember_private(self, *, seat_id: str, key: str, value: str) -> None:
        self._private_store.setdefault(seat_id, []).append({"tick": self.tick, "key": key, "value": value})
        payload = {"ts": utc_now(), "run_id": self.run_id, "tick": self.tick, "seat_id": seat_id, "op": "write", "key": key, "value": value, "origin": self._origin}
        self.append_jsonl("store_events.jsonl", payload)
        self.append_ledger("private_store_write", {"seat_id": seat_id, "key": key})

    def read_private(self, *, seat_id: str, limit: int = 5) -> list[dict[str, Any]]:
        notes = self._private_store.get(seat_id, [])[-max(int(limit), 1):]
        payload = {"ts": utc_now(), "run_id": self.run_id, "tick": self.tick, "seat_id": seat_id, "op": "read", "returned": len(notes), "origin": self._origin}
        self.append_jsonl("store_events.jsonl", payload)
        self.append_ledger("private_store_read", {"seat_id": seat_id, "returned": len(notes)})
        return notes

    def append_ledger(self, event_type: str, payload: dict[str, Any]) -> str:
        base = {"ts": utc_now(), "run_id": self.run_id, "tick": self.tick, "event_type": event_type, "payload": payload, "prev_hash": self._prev_hash}
        event_hash = hashlib.sha256(json.dumps(base, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        base["hash"] = event_hash
        self._prev_hash = event_hash
        self.append_jsonl("world_ledger.jsonl", base)
        return event_hash

    def write_json(self, name: str, payload: dict[str, Any]) -> None:
        (self.run_root / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def append_jsonl(self, name: str, payload: dict[str, Any]) -> None:
        with (self.run_root / name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows
