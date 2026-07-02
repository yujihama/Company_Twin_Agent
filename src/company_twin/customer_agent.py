from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any

from .deck import CustomerEvent
from .kernel import WorldKernel
from .recorder import RunRecorder


def emit_customer_turn(*, kernel: WorldKernel, recorder: RunRecorder, event: CustomerEvent, tick: int) -> None:
    recorder.append_ledger(
        "latent_truth_committed",
        {"event_id": event.event_id, "customer_id": event.customer_id, "latent_truth_hash": hashlib.sha256(event.latent_truth.encode("utf-8")).hexdigest()},
    )
    visible_event = customer_message(event, tick=tick)
    kernel.record_customer_event({key: value for key, value in visible_event.items() if key not in {"kind", "utterance"}})
    recorder.append_ledger("customer_utterance", {"event_id": event.event_id, "customer_id": event.customer_id, "utterance": visible_event["utterance"]})
    kernel.enqueue_inbox(event.primary_seat, visible_event)


def customer_message(event: CustomerEvent, *, tick: int) -> dict[str, Any]:
    utterance = event.world_visible
    if "repeats questions" in event.latent_truth or "理解" in event.world_visible:
        utterance = f"{event.world_visible} 顧客は同じ説明箇所について再確認を求めている。"
    elif "deadline pressure" in event.latent_truth:
        utterance = f"{event.world_visible} 顧客は本日中に間に合うかを繰り返し確認している。"
    payload = asdict(event)
    payload.update({"kind": "customer_utterance", "tick": tick, "utterance": utterance, "world_visible": utterance})
    payload.pop("latent_truth", None)
    return payload
