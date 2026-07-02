from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class RunRecorder:
    def __init__(self, run_root: Path, run_id: str, meta: dict[str, Any] | None = None):
        self.run_root = run_root
        self.run_id = run_id
        self.tick = 0
        self._prev_hash = ""
        self._basis_counter = 0
        self._read_docs: dict[str, set[str]] = {}
        run_root.mkdir(parents=True, exist_ok=True)
        self.write_json("meta.json", {"run_id": run_id, "created_at": utc_now(), **(meta or {})})
        for name in ("attempts.jsonl", "basis_records.jsonl", "chat_channel.jsonl", "world_ledger.jsonl"):
            (self.run_root / name).touch(exist_ok=True)

    def set_tick(self, tick: int) -> None:
        self.tick = tick

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
        )
        self.append_jsonl("attempts.jsonl", asdict(record))
        if tool == "read_document" and success:
            doc_id = str((args or {}).get("doc_id") or "")
            if doc_id:
                self._read_docs.setdefault(seat_id, set()).add(doc_id)
        return record

    def next_basis_id(self) -> str:
        self._basis_counter += 1
        return f"BASIS-{self._basis_counter:06d}"

    def has_read_doc(self, seat_id: str, doc_id: str) -> bool:
        return doc_id in self._read_docs.get(seat_id, set())

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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows
