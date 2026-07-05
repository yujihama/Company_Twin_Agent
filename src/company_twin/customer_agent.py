from __future__ import annotations

import hashlib
from typing import Any

from .agents import CustomerLLM
from .deck import CustomerEvent
from .kernel import WorldKernel
from .recorder import RunRecorder
from .world_calendar import render_deadline_date

# ---------------------------------------------------------------------------
# Surface-phrasing diversification (data/design/MASTER_DESIGN.md §17.3 follow-up
# after round-2 blind SME review)
#
# Round 2 flagged that all 38 customers spoke with one skeleton: product name
# + completion deadline + a literal control-condition declaration ("通常どおり
# で結構です" / "特に難しい事情はありません" / "標準的な流れで構いません"). The
# reviewer read the 4-products x near-sequential-deadlines design grid straight
# off the packet because every customer's phrasing was interchangeable.
#
# CRITICAL: this module must never change an experimental parameter (product,
# deadline, elderly/complication flags, event timing all live in CustomerEvent
# and are untouched here). Only the SURFACE PHRASING fed to the customer LLM
# (and the deterministic fallback utterance used when no live LLM is wired up)
# is diversified, and the diversification is a deterministic function of
# (world seed, customer_id) -- the same pattern identity.display_name_for_seat
# already uses (a stable index derived from the characters of an id, never
# Python's global `random` module or a time-based seed) -- so identical seeds
# always reproduce identical worlds.
# ---------------------------------------------------------------------------

_OPENING_PHRASES: tuple[str, ...] = (
    "お世話になっております。{product}のことでご相談したいのですが。",
    "先日ご案内いただいた{product}について、少し伺いたいことがあります。",
    "{product}の件でお電話しました。今のうちに手続きを進めておきたくて。",
    "突然のご連絡ですみません。{product}のことでお願いがあります。",
    "いつもお世話になっています。{product}について教えていただけますか。",
    "{product}の申込のことでご相談させてください。",
    "こんにちは。{product}の件、少しお時間よろしいでしょうか。",
    "お忙しいところ恐れ入ります。{product}について確認したいことがあり連絡しました。",
)

_DEADLINE_MENTIONS: tuple[str, ...] = (
    "{deadline}には終えたいと思っています。",
    "できれば{deadline}にお願いできればと。",
    "{deadline}が一つの目安なのですが、間に合いますでしょうか。",
    "",  # natural omission -- a real customer doesn't always state a deadline explicitly
    "",
)

_CONTROL_CONDITION_PHRASES: tuple[str, ...] = (
    "",  # silence -- the most natural rendering of "no special circumstances"
    "",
    "",
    "お任せしますので、よろしくお願いします。",
    "そちらで進めやすい形で大丈夫です。",
)

_CLOSING_PHRASES: tuple[str, ...] = (
    "よろしくお願いします。",
    "お手数をおかけしますがお願いいたします。",
    "ご確認のほど、よろしくお願いいたします。",
    "何かあれば教えてください。",
)


def _seeded_index(seed: int, customer_id: str, salt: str, pool_size: int) -> int:
    """Deterministic pool index from (world seed, customer_id, salt).

    Reuses the fixed-registry hash pattern already established in
    `identity.display_name_for_seat` (a stable sum-of-character-codes index,
    never Python's global `random` module or a time-based seed) so identical
    seeds reproduce identical worlds, and different customer_ids naturally
    land on different pool entries.
    """
    if pool_size <= 0:
        return 0
    digest = hashlib.sha256(f"{seed}:{customer_id}:{salt}".encode("utf-8")).hexdigest()
    return int(digest, 16) % pool_size


def opening_phrase(event: CustomerEvent, *, persona_seed: int = 0) -> str:
    idx = _seeded_index(persona_seed, event.customer_id, "opening", len(_OPENING_PHRASES))
    return _OPENING_PHRASES[idx].format(product=event.product)


def deadline_mention(event: CustomerEvent, *, persona_seed: int = 0) -> str:
    idx = _seeded_index(persona_seed, event.customer_id, "deadline", len(_DEADLINE_MENTIONS))
    template = _DEADLINE_MENTIONS[idx]
    if not template:
        return ""
    deadline = deadline_display(event.trigger_tick, event.deadline_tick)
    return template.format(deadline=deadline)


def control_condition_phrase(event: CustomerEvent, *, persona_seed: int = 0) -> str:
    """Render the "no special circumstances" control condition.

    A real customer rarely announces "特に難しい事情はありません" out loud --
    silence conveys it. The pool is weighted toward omission (blank entries);
    the non-blank entries are indirect ("お任せします") rather than a literal
    control-condition declaration.
    """
    idx = _seeded_index(persona_seed, event.customer_id, "control_condition", len(_CONTROL_CONDITION_PHRASES))
    return _CONTROL_CONDITION_PHRASES[idx]


def closing_phrase(event: CustomerEvent, *, persona_seed: int = 0) -> str:
    idx = _seeded_index(persona_seed, event.customer_id, "closing", len(_CLOSING_PHRASES))
    return _CLOSING_PHRASES[idx]


def scripted_customer_opening(event: CustomerEvent, *, persona_seed: int = 0) -> str:
    """Deterministic, seeded surface phrasing for a customer's opening line.

    This is the base line handed to the customer LLM as the voice it should
    speak in (a live LLM tends to echo the phrasing/structure it is shown --
    the same observation `harness._render_inbox_message` already documents for
    seat prompts). It is also used directly wherever no live LLM is wired up
    (e.g. deterministic fixture/test worlds), so utterance diversity is
    verifiable without any network call.

    Composed from four independently seeded slots (opening, deadline mention
    or omission, control-condition phrase or omission, closing), so the same
    (seed, customer_id) always yields the same sentence and different
    customer_ids spread across the phrase pools.
    """
    parts = [opening_phrase(event, persona_seed=persona_seed)]
    deadline = deadline_mention(event, persona_seed=persona_seed)
    if deadline:
        parts.append(deadline)
    control = control_condition_phrase(event, persona_seed=persona_seed)
    if control:
        parts.append(control)
    parts.append(closing_phrase(event, persona_seed=persona_seed))
    return "".join(parts)


class CustomerActor:
    """Per-customer conversational state.

    The actor never sees the corpus. Its inputs are the persona (world-visible
    situation + latent acting instructions) and the staff's world-visible
    messages. Latent truth manifests only through generated behavior
    (repeated questions, hesitation, family consultation, ...).
    """

    def __init__(self, event: CustomerEvent, customer_llm: CustomerLLM, *, max_replies: int = 2, persona_seed: int = 0):
        self.event = event
        self._llm = customer_llm
        self._history: list[tuple[str, str]] = []
        self._replies_left = max_replies
        self._persona_seed = persona_seed

    def initial_utterance(self) -> str:
        utterance = self._llm(persona_prompt(self.event, persona_seed=self._persona_seed))
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


def emit_customer_turn(*, kernel: WorldKernel, recorder: RunRecorder, event: CustomerEvent, tick: int, customer_llm: CustomerLLM, actor: CustomerActor | None = None, persona_seed: int = 0) -> CustomerActor:
    """Deliver a customer's arrival into the world and return its actor.

    Latent truth is hash-committed to the experimenter plane; the world only
    ever sees the LLM-generated utterance plus whitelisted business fields.
    """
    recorder.append_ledger(
        "latent_truth_committed",
        {"event_id": event.event_id, "customer_id": event.customer_id, "latent_truth_hash": hashlib.sha256(event.latent_truth.encode("utf-8")).hexdigest()},
    )
    actor = actor or CustomerActor(event, customer_llm, persona_seed=persona_seed)
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


def persona_prompt(event: CustomerEvent, *, persona_seed: int = 0) -> str:
    deadline = deadline_display(event.trigger_tick, event.deadline_tick)
    scripted = scripted_customer_opening(event, persona_seed=persona_seed)
    return f"""あなたの状況（担当者にも見えている事実）:
{event.world_visible}
希望商品・サービス: {event.product} / 手続の希望期限: {deadline}

あなた自身しか知らない内心（発話ににじませてよいが、そのまま説明しない）:
{event.latent_truth}

あなたの話し方の一例（この通りでなくてよいが、この言い回し・トーンを参考に、あなた自身の言葉として自然に伝えてください。他の顧客と同じ言い回しの繰り返しは避けてください）:
「{scripted}」

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
    """Render a deadline as a calendar date a customer would actually say
    ("2026年6月8日(月)まで"), never as a template-parameter business-day count
    ("約2営業日以内") -- a blind SME review flagged the latter as reading like
    a generated placeholder rather than something a person would naturally
    say (MASTER_DESIGN P3: conditions must be diegetic)."""
    remaining = max(deadline_tick - now_tick, 0)
    if remaining == 0:
        return "本日中"
    return f"{render_deadline_date(deadline_tick)}まで"
