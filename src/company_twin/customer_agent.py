from __future__ import annotations

import hashlib
from typing import Any

from .agents import CustomerLLM
from .deck import CustomerEvent
from .kernel import WorldKernel
from .recorder import RunRecorder


class CustomerActor:
    """Per-customer conversational state.

    The actor never sees the corpus. Its inputs are the persona (world-visible
    situation + latent acting instructions) and the staff's world-visible
    messages. Latent truth manifests only through generated behavior
    (repeated questions, hesitation, family consultation, ...).
    """

    def __init__(self, event: CustomerEvent, customer_llm: CustomerLLM, *, max_replies: int = 2):
        self.event = event
        self._llm = customer_llm
        self._history: list[tuple[str, str]] = []
        self._replies_left = max_replies

    def initial_utterance(self) -> str:
        utterance = self._llm(persona_prompt(self.event))
        self._history.append(("customer", utterance))
        return utterance

    def reply_to(self, staff_message: str) -> str | None:
        if self._replies_left <= 0:
            return None
        self._replies_left -= 1
        self._history.append(("staff", staff_message))
        utterance = self._llm(reply_prompt(self.event, self._history))
        self._history.append(("customer", utterance))
        return utterance


def emit_customer_turn(*, kernel: WorldKernel, recorder: RunRecorder, event: CustomerEvent, tick: int, customer_llm: CustomerLLM, actor: CustomerActor | None = None) -> CustomerActor:
    """Deliver a customer's arrival into the world and return its actor.

    Latent truth is hash-committed to the experimenter plane; the world only
    ever sees the LLM-generated utterance plus whitelisted business fields.
    """
    recorder.append_ledger(
        "latent_truth_committed",
        {"event_id": event.event_id, "customer_id": event.customer_id, "latent_truth_hash": hashlib.sha256(event.latent_truth.encode("utf-8")).hexdigest()},
    )
    actor = actor or CustomerActor(event, customer_llm)
    with recorder.origin("customer"):
        utterance = actor.initial_utterance()
    message = world_visible_message(event, tick=tick, utterance=utterance)
    kernel.record_customer_event(
        {"event_id": event.event_id, "customer_id": event.customer_id, "application_id": event.application_id, "product": event.product, "utterance": utterance}
    )
    recorder.append_ledger("customer_utterance", {"event_id": event.event_id, "customer_id": event.customer_id, "utterance": utterance, "reply": False})
    kernel.enqueue_inbox(event.primary_seat, message)
    return actor


def emit_customer_reply(*, kernel: WorldKernel, recorder: RunRecorder, actor: CustomerActor, to_seat: str, staff_message: str, tick: int) -> bool:
    with recorder.origin("customer"):
        utterance = actor.reply_to(staff_message)
    if utterance is None:
        return False
    event = actor.event
    message = world_visible_message(event, tick=tick, utterance=utterance)
    recorder.append_ledger("customer_utterance", {"event_id": event.event_id, "customer_id": event.customer_id, "utterance": utterance, "reply": True})
    kernel.enqueue_inbox(to_seat, message)
    return True


def persona_prompt(event: CustomerEvent) -> str:
    deadline = deadline_display(event.trigger_tick, event.deadline_tick)
    return f"""あなたの状況（担当者にも見えている事実）:
{event.world_visible}
希望商品・サービス: {event.product} / 手続の希望期限: {deadline}

あなた自身しか知らない内心（発話ににじませてよいが、そのまま説明しない）:
{event.latent_truth}

この状況で、担当者に最初に伝える発話を生成してください。"""


def reply_prompt(event: CustomerEvent, history: list[tuple[str, str]]) -> str:
    rendered = "\n".join(f"- {'担当者' if who == 'staff' else 'あなた'}: {text}" for who, text in history[-6:])
    return f"""あなたの状況: {event.world_visible}
あなた自身しか知らない内心（そのまま説明せず、言動としてにじませる）: {event.latent_truth}

ここまでのやり取り:
{rendered}

担当者の直近の説明・質問に対するあなたの返答を1つ生成してください。内心に照らして、理解できていなければ同じ点を別の言い方でもう一度尋ね、納得していれば次の手続きへ進む意思を伝えてください。2〜3文。"""


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
