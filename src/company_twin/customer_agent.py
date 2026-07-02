from __future__ import annotations

import hashlib
from typing import Any

from .agents import CustomerLLM
from .deck import CustomerEvent
from .kernel import WorldKernel
from .recorder import RunRecorder


def emit_customer_turn(*, kernel: WorldKernel, recorder: RunRecorder, event: CustomerEvent, tick: int, customer_llm: CustomerLLM) -> None:
    """Deliver one customer arrival into the world.

    Latent truth is hash-committed to the experimenter plane; the world only
    ever sees the LLM-generated utterance plus whitelisted business fields.
    """
    recorder.append_ledger(
        "latent_truth_committed",
        {"event_id": event.event_id, "customer_id": event.customer_id, "latent_truth_hash": hashlib.sha256(event.latent_truth.encode("utf-8")).hexdigest()},
    )
    with recorder.origin("customer"):
        utterance = customer_llm(persona_prompt(event))
    message = world_visible_message(event, tick=tick, utterance=utterance)
    kernel.record_customer_event(
        {"event_id": event.event_id, "customer_id": event.customer_id, "application_id": event.application_id, "product": event.product, "utterance": utterance}
    )
    recorder.append_ledger("customer_utterance", {"event_id": event.event_id, "customer_id": event.customer_id, "utterance": utterance})
    kernel.enqueue_inbox(event.primary_seat, message)


def persona_prompt(event: CustomerEvent) -> str:
    deadline = deadline_display(event.trigger_tick, event.deadline_tick)
    return f"""あなたの状況（担当者にも見えている事実）:
{event.world_visible}
希望商品・サービス: {event.product} / 手続の希望期限: {deadline}

あなた自身しか知らない内心（発話ににじませてよいが、そのまま説明しない）:
{event.latent_truth}

この状況で、担当者に最初に伝える発話を生成してください。"""


def world_visible_message(event: CustomerEvent, *, tick: int, utterance: str) -> dict[str, Any]:
    return {
        "kind": "customer_utterance",
        "tick": tick,
        "event_id": event.event_id,
        "customer_id": event.customer_id,
        "application_id": event.application_id,
        "product": event.product,
        "deadline_display": deadline_display(tick, event.deadline_tick),
        "utterance": utterance,
    }


def deadline_display(now_tick: int, deadline_tick: int) -> str:
    remaining = max(deadline_tick - now_tick, 0)
    if remaining == 0:
        return "本日この半日中"
    days = remaining / 2
    if days <= 1:
        return "明日中"
    return f"約{days:.0f}営業日以内"
