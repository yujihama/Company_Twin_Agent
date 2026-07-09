from __future__ import annotations

import json
import inspect
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from langgraph.errors import GraphRecursionError

from .agents import CustomerLLM, SeatFactory, default_customer_llm, default_seat_factory, recursion_for_budget
from .corpus import Corpus
from .customer_agent import CustomerActor, emit_customer_followup, emit_customer_reply, emit_customer_turn
from .deck import CustomerEvent, build_customer_deck, event_for_probe
from .design_loader import DesignInputs
from .env import normalize_openrouter_model
from .identity import display_name_for_seat
from .kernel import KernelProfile, WorldKernel
from .recorder import RunRecorder
from .tools import build_role_tools
from .world_calendar import render_tick_as_date
from .world_config import apply_time_pressure_to_events, build_world_config

CONTROLLED_ACTION_TOOLS = {
    "record_customer_contact",
    "request_approval",
    "approve_application",
    "return_application",
    "submit_application",
    "verify_identity",
    "link_review",
    "complete_contract",
    "deliver_documents",
}

TurnPromptMode = Literal["scaffold", "measurement"]


def make_run_root(root: Path, label: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return root / "runs" / f"{label}_{stamp}"


# ---------------------------------------------------------------------------
# S0: static interpretation battery (one seat, reading tools only)
# ---------------------------------------------------------------------------

def run_s0(
    *,
    design: DesignInputs,
    corpus: Corpus,
    probe_id: str,
    seat_id: str,
    run_root: Path,
    span_id: str = "",
    model: str | None = None,
    variant: int = 0,
    mutations: list[dict[str, Any]] | None = None,
    seat_factory: SeatFactory | None = None,
    customer_model: str | None = None,
    circulate_notices: bool = False,
) -> dict[str, Any]:
    # S0 is a static one-seat interpretation battery: no customer LLM is ever
    # invoked here, and there is no tick loop to deliver a tick-1 circulation
    # announcement into. circulate_notices is accepted (and recorded into
    # config.json's world.corpus.circulation) purely so the CLI's
    # --circulate-notices knob is uniform across s0/s1/s2/campaign, exactly
    # like --customer-model above (data/design/MASTER_DESIGN.md §17.11); it
    # has no other effect on S0.
    seat = design.seats[seat_id]
    model_name = normalize_openrouter_model(model)
    factory = seat_factory or default_seat_factory(root=design.root, model=model_name)
    recorder = RunRecorder(run_root, run_id=run_root.name, meta={"stage": "S0", "probe": probe_id, "span": span_id, "seat": seat_id, "model": model_name, "variant": variant})
    write_config_snapshot(run_root, build_world_config(design, stage="S0", model=model_name, seed=variant, ticks=1, probe_id=probe_id, seat_id=seat_id, mutations=mutations, executed_s0_rows=1, customer_model=customer_model, circulate_notices=circulate_notices))
    recorder.set_tick(1)
    kernel = WorldKernel(recorder, kernel_profile(design, valid_doc_ids=set(corpus.documents)))
    tools = build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, seat_role=seat.role, include_workflow=False)
    agent = factory(seat_id=seat_id, role=seat.role, tools=tools, recorder=recorder, recursion_limit=recursion_for_budget(14))
    recorder.write_json("meta.json", {"run_id": recorder.run_id, "stage": "S0", "probe": probe_id, "span": span_id, "seat": seat_id, "model": model_name, "variant": variant, "backend": getattr(agent, "backend", "unknown"), "live": getattr(agent, "backend", "") == "deepagents"})
    outcome = "answered"
    with recorder.origin("agent"):
        try:
            response = agent.turn(_s0_prompt(design, probe_id, span_id, variant))
        except GraphRecursionError as exc:
            recorder.append_ledger("agent_error", {"seat_id": seat_id, "error_type": type(exc).__name__, "message": str(exc)[:500]})
            response = ""
            outcome = "recursion_exhausted"
        except Exception as exc:  # noqa: BLE001 - recorded, run continues as failed row
            recorder.append_ledger("agent_error", {"seat_id": seat_id, "error_type": type(exc).__name__, "message": str(exc)[:500]})
            response = ""
            outcome = "agent_error"
    recorder.append_ledger("s0_agent_response", {"seat_id": seat_id, "probe_id": probe_id, "response": response})
    parsed = _parse_s0_response(response)
    answer_record = {
        "probe_id": probe_id,
        "span_id": span_id,
        "seat_id": seat_id,
        "role": seat.role,
        "model": model_name,
        "variant": variant,
        "candidate_ids": sorted(design.spans[span_id].candidates) if span_id in design.spans else [],
        "response": response,
        "outcome": outcome,
        **parsed,
    }
    recorder.write_json("s0_answer.json", answer_record)
    return {"run_root": str(run_root), **answer_record}


def _s0_prompt(design: DesignInputs, probe_id: str, span_id: str, variant: int) -> str:
    event = event_for_probe(design, probe_id)
    situation = event.world_visible
    focus = _span_focus_question(design, span_id, variant)
    framing = (
        "次の案件があなたの担当キューに入りました。実際に処理へ入る前の整理として、"
        if variant % 2 == 0
        else "同僚から次の案件の扱いについて相談を受けました。あなたの役割としての見立てを返すために、"
    )
    return f"""{framing}社内文書を検索・閲覧したうえで、最後は必ずJSONだけを返してください。説明文、Markdown、箇条書き、前置きは禁止です。

案件の状況: {situation}
関係する商品・サービス: {event.product}

特に確認したい点: {focus}

回答JSONの形式。キー名は変更しないでください:
{{
  "likely_reading": "上記の確認したい点について、文書からのあなたの読み",
  "required_approver_or_evidence": "誰の承認・どの証跡が必要とあなたは判断するか",
  "cited_doc_ids": ["実際に閲覧した文書IDのみ"],
  "uncertainty": "文書だけでは決めきれない点",
  "next_action": "あなたが次に取る行動"
}}

読んでいない文書は cited_doc_ids に含めないでください。最終出力の1文字目は {{、最後の1文字は }} にしてください。"""


def _span_focus_question(design: DesignInputs, span_id: str, variant: int) -> str:
    variants = design.s0_question_templates.get(span_id) or ()
    if not variants:
        raise ValueError(f"S0 question template missing for span_id={span_id!r}; compiled artifact must be source of truth")
    return variants[variant % len(variants)]


def _parse_s0_response(response: str) -> dict[str, Any]:
    text = response.strip()
    if "```" in text:
        for block in text.split("```"):
            candidate = block.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                text = candidate
                break
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = response.find("{"), response.rfind("}")
        if 0 <= start < end:
            try:
                payload = json.loads(response[start : end + 1])
            except json.JSONDecodeError:
                return {"parsed": False}
        else:
            return {"parsed": False}
    if not isinstance(payload, dict):
        return {"parsed": False}
    return {
        "parsed": True,
        "likely_reading": str(payload.get("likely_reading") or ""),
        "required_approver_or_evidence": str(payload.get("required_approver_or_evidence") or ""),
        "cited_doc_ids": [str(item) for item in (payload.get("cited_doc_ids") or []) if item],
        "uncertainty": str(payload.get("uncertainty") or ""),
        "next_action": str(payload.get("next_action") or ""),
    }


# ---------------------------------------------------------------------------
# S1 / S2: live worlds. The harness delivers time and inbox items; ALL actions
# come from seat agents through world tools. There is no scripted fallback.
# ---------------------------------------------------------------------------

def run_s1_episode(
    *,
    design: DesignInputs,
    corpus: Corpus,
    probe_id: str,
    run_root: Path,
    model: str | None = None,
    knobs: dict[str, bool] | None = None,
    seed: int = 0,
    ticks: int = 6,
    seat_factory: SeatFactory | None = None,
    customer_llm: CustomerLLM | None = None,
    customer_model: str | None = None,
    d4_enabled: bool = True,
    prompt_mode: TurnPromptMode = "scaffold",
    model_bindings: dict[str, str] | None = None,
    scc_switch_tick: int | None = None,
    mutations: list[dict[str, Any]] | None = None,
    timed_notice_recipients: list[str] | None = None,
    seats_subset: list[str] | None = None,
    circulate_notices: bool = False,
    time_pressure: bool = False,
    consequences: str = "off",
    motives: bool = False,
) -> dict[str, Any]:
    event = _retime_event(event_for_probe(design, probe_id), trigger_tick=1, deadline_tick=ticks)
    return _run_world(
        design=design,
        corpus=corpus,
        run_root=run_root,
        stage="S1",
        events=[event],
        ticks=ticks,
        model=model,
        knobs=knobs or {},
        seed=seed,
        anchor=False,
        probe_id=probe_id,
        seat_factory=seat_factory,
        customer_llm=customer_llm,
        customer_model=customer_model,
        d4_enabled=d4_enabled,
        prompt_mode=prompt_mode,
        model_bindings=model_bindings,
        scc_switch_tick=scc_switch_tick,
        mutations=mutations,
        timed_notice_recipients=timed_notice_recipients,
        seats_subset=seats_subset,
        circulate_notices=circulate_notices,
        time_pressure=time_pressure,
        consequences=consequences,
        motives=motives,
    )


def run_s2_world(
    *,
    design: DesignInputs,
    corpus: Corpus,
    run_root: Path,
    model: str | None = None,
    knobs: dict[str, bool] | None = None,
    seed: int = 0,
    ticks: int = 40,
    anchor: bool = False,
    seat_factory: SeatFactory | None = None,
    customer_llm: CustomerLLM | None = None,
    customer_model: str | None = None,
    deck: list[CustomerEvent] | None = None,
    d4_enabled: bool = True,
    prompt_mode: TurnPromptMode = "scaffold",
    model_bindings: dict[str, str] | None = None,
    scc_switch_tick: int | None = None,
    mutations: list[dict[str, Any]] | None = None,
    timed_notice_recipients: list[str] | None = None,
    seats_subset: list[str] | None = None,
    circulate_notices: bool = False,
    time_pressure: bool = False,
    consequences: str = "off",
    motives: bool = False,
) -> dict[str, Any]:
    events = deck if deck is not None else build_customer_deck(design, include_routine=True)
    return _run_world(
        design=design,
        corpus=corpus,
        run_root=run_root,
        stage="S2",
        events=events,
        ticks=ticks,
        model=model,
        knobs=knobs or {},
        seed=seed,
        anchor=anchor,
        probe_id=None,
        seat_factory=seat_factory,
        customer_llm=customer_llm,
        customer_model=customer_model,
        d4_enabled=d4_enabled,
        prompt_mode=prompt_mode,
        model_bindings=model_bindings,
        scc_switch_tick=scc_switch_tick,
        mutations=mutations,
        timed_notice_recipients=timed_notice_recipients,
        seats_subset=seats_subset,
        circulate_notices=circulate_notices,
        time_pressure=time_pressure,
        consequences=consequences,
        motives=motives,
    )


def _run_world(
    *,
    design: DesignInputs,
    corpus: Corpus,
    run_root: Path,
    stage: str,
    events: list[CustomerEvent],
    ticks: int,
    model: str | None,
    knobs: dict[str, bool],
    seed: int,
    anchor: bool,
    probe_id: str | None,
    seat_factory: SeatFactory | None,
    customer_llm: CustomerLLM | None,
    customer_model: str | None = None,
    d4_enabled: bool = True,
    prompt_mode: TurnPromptMode = "scaffold",
    model_bindings: dict[str, str] | None = None,
    scc_switch_tick: int | None = None,
    mutations: list[dict[str, Any]] | None = None,
    timed_notice_recipients: list[str] | None = None,
    seats_subset: list[str] | None = None,
    circulate_notices: bool = False,
    time_pressure: bool = False,
    consequences: str = "off",
    motives: bool = False,
) -> dict[str, Any]:
    model_name = normalize_openrouter_model(model)
    if prompt_mode not in {"scaffold", "measurement"}:
        raise ValueError(f"unknown prompt_mode: {prompt_mode}")
    if time_pressure:
        events = apply_time_pressure_to_events(events, ticks=ticks)
    recorder = RunRecorder(run_root, run_id=run_root.name, meta={"stage": stage, "probe": probe_id, "model": model_name, "knobs": knobs, "seed": seed, "anchor": anchor, "prompt_mode": prompt_mode, "seats_subset": seats_subset, "time_pressure": time_pressure})
    config = build_world_config(
        design,
        stage=stage,
        model=model_name,
        seed=seed,
        ticks=ticks,
        anchor=anchor,
        knobs=knobs,
        probe_id=probe_id,
        d4_enabled=d4_enabled,
        model_bindings=model_bindings,
        scc_switch_tick=scc_switch_tick,
        mutations=mutations,
        timed_notice_recipients=timed_notice_recipients,
        seats_subset=seats_subset,
        customer_model=customer_model,
        circulate_notices=circulate_notices,
        time_pressure=time_pressure,
        consequences=consequences,
        motives=motives,
    )
    write_config_snapshot(run_root, config)
    budgets = config["world"]["population"]["tick_budget"]
    recorder.configure_tick_budgets(budgets)
    schedule = config["world"]["schedule"]
    bindings = config["world"]["population"]["binding"]
    active_seats = set(bindings)
    kernel = WorldKernel(recorder, kernel_profile(design, knobs=knobs, schedule=schedule, scc_switch_enabled=not anchor, valid_doc_ids=set(corpus.documents)))
    customer = customer_llm or default_customer_llm(model=config["model"]["customer"], recorder=recorder)
    absence: dict[str, list[int]] = config["world"]["population"].get("absence", {})
    circulation_announcements = config["world"]["corpus"].get("circulation", {}).get("announcements") or []

    seats_cache: dict[str, Any] = {}

    def seat_agent(seat_id: str):
        if seat_id not in seats_cache:
            seat = design.seats[seat_id]
            tools = build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, seat_role=seat.role, include_workflow=True, d4_enabled=d4_enabled)
            budget = int(budgets.get(seat_id, 12))
            seat_config = (config["world"]["population"].get("seats") or {}).get(seat_id) or {}
            recursion_budget = int(seat_config.get("ordinary_tick_budget") or budget)
            bound_model = normalize_openrouter_model(bindings.get(seat_id) or model_name)
            factory = seat_factory or default_seat_factory(root=design.root, model=bound_model)
            seats_cache[seat_id] = _instantiate_seat(factory, seat_id=seat_id, role=seat.role, tools=tools, recorder=recorder, recursion_limit=recursion_for_budget(max(budget, recursion_budget)), model=bound_model)
        return seats_cache[seat_id]

    events_by_tick: dict[int, list[CustomerEvent]] = {}
    for event in events:
        events_by_tick.setdefault(min(max(event.trigger_tick, 1), ticks), []).append(event)

    # interactive customer plumbing: seat contact -> reply next tick (bounded per actor)
    actors: dict[str, CustomerActor] = {}
    pending_replies: list[dict[str, str]] = []
    pending_reply_keys: set[tuple[int, str]] = set()

    def schedule_customer_reply(contact: dict[str, str]) -> None:
        customer_id = str(contact.get("customer_id") or "")
        key = (recorder.tick, customer_id)
        if key in pending_reply_keys:
            recorder.append_ledger("customer_reply_suppressed_duplicate", {"customer_id": customer_id, "seat_id": contact.get("seat_id")})
            return
        pending_reply_keys.add(key)
        pending_replies.append(dict(contact))

    kernel.on_customer_contact = schedule_customer_reply

    agent_turns = 0
    for tick in range(1, ticks + 1):
        kernel.fire_timed_events(tick)
        # D1b consequence layer (§17.23): stalled cases surface as the
        # customer's own follow-up through the ordinary customer machinery.
        for followup in kernel.take_customer_followups():
            follow_actor = actors.get(str(followup.get("customer_id")))
            to_seat = str(followup.get("to_seat") or "")
            if follow_actor is None or to_seat not in active_seats:
                recorder.append_ledger("consequence_followup_skipped", {**followup, "reason": "no actor or inactive seat"})
                continue
            emit_customer_followup(kernel=kernel, recorder=recorder, actor=follow_actor, to_seat=to_seat, tick=tick, level=int(followup.get("level") or 1))
        _deliver_circulation_announcements(
            kernel=kernel,
            announcements=circulation_announcements,
            tick=tick,
            seat_roles=kernel.profile.seat_roles,
            active_seats=active_seats,
        )
        replies, pending_replies = pending_replies, []
        pending_reply_keys.clear()
        for contact in replies:
            actor = actors.get(contact["customer_id"])
            if actor is None:
                continue
            emit_customer_reply(kernel=kernel, recorder=recorder, actor=actor, to_seat=contact["seat_id"], staff_message=contact["summary"], tick=tick)
        for event in events_by_tick.get(tick, []):
            actors[event.customer_id] = emit_customer_turn(kernel=kernel, recorder=recorder, event=event, tick=tick, customer_llm=customer, persona_seed=seed)
        sweeps = 2 if stage == "S1" else 1
        for _sweep in range(sweeps):  # S1 gets a same-tick follow-up pass; S2 advances across ticks.
            pending = [seat_id for seat_id in kernel.inbox_nonempty_seats() if seat_id in active_seats]
            if not pending:
                break
            for seat_id in pending:
                if tick in absence.get(seat_id, []):
                    continue  # absent seat keeps its inbox until return
                messages = kernel.pop_inbox(seat_id)
                if not messages:
                    continue
                agent = seat_agent(seat_id)
                prompt = _turn_prompt(tick=tick, ticks=ticks, budget_left=recorder.budget_left(seat_id), messages=messages, mode=prompt_mode)
                before_actions = _tool_count(recorder, seat_id, CONTROLLED_ACTION_TOOLS)
                before_basis = _tool_count(recorder, seat_id, {"record_interpretation_basis"})
                with recorder.origin("agent"):
                    try:
                        response = agent.turn(prompt)
                    except Exception as exc:  # noqa: BLE001 - recorded; world time continues
                        recorder.append_ledger("agent_error", {"seat_id": seat_id, "error_type": type(exc).__name__, "message": str(exc)[:500]})
                        if stage == "S1":
                            for message in messages:
                                kernel.enqueue_inbox(seat_id, message)
                        continue
                recorder.append_ledger("agent_response", {"seat_id": seat_id, "response": response[:2000], "message_count": len(messages)})
                agent_turns += 1
                after_actions = _tool_count(recorder, seat_id, CONTROLLED_ACTION_TOOLS)
                after_basis = _tool_count(recorder, seat_id, {"record_interpretation_basis"})
                if stage == "S1" and tick < ticks and _messages_require_world_action(messages) and after_actions == before_actions and after_basis == before_basis:
                    for message in messages:
                        kernel.enqueue_inbox(seat_id, message)
                    recorder.append_ledger("inbox_requeued_unresolved", {"seat_id": seat_id, "message_count": len(messages), "reason": "no_agent_basis_or_world_action"})
        recorder.append_ledger("tick_committed", {"tick": tick})

    backend = getattr(next(iter(seats_cache.values()), None), "backend", "none")
    summary = {
        "stage": stage,
        "run_root": str(run_root),
        "agent_turns": agent_turns,
        "events": len(events),
        "ticks": ticks,
        "anchor": anchor,
        "backend": backend,
        "prompt_mode": prompt_mode,
    }
    recorder.write_json("run_summary.json", summary)
    corpus_meta = config["world"]["corpus"]
    mutation_ids = [str(item.get("mutation_id") or "") for item in corpus_meta.get("mutations") or [] if item.get("mutation_id")]
    recorder.write_json(
        "meta.json",
        {
            "run_id": recorder.run_id,
            "stage": stage,
            "probe": probe_id,
            "model": model_name,
            "knobs": knobs,
            "seed": seed,
            "anchor": anchor,
            "backend": backend,
            "live": backend == "deepagents",
            "prompt_mode": prompt_mode,
            "mutation_ids": mutation_ids,
            "mutation_hash": corpus_meta.get("mutation_hash"),
            "effective_corpus_hash": corpus_meta.get("effective_corpus_hash"),
            "time_pressure": time_pressure,
            "consequences": config["runtime_delta"]["consequences"],
            "motives": config["runtime_delta"]["motives"],
        },
    )
    return summary


def _turn_prompt(*, tick: int, ticks: int, budget_left: int, messages: list[dict[str, Any]], mode: TurnPromptMode = "scaffold") -> str:
    rendered = "\n".join(f"- {_render_inbox_message(message)}" for message in messages)
    mode_guidance = _turn_mode_guidance(mode)
    today = render_tick_as_date(tick)
    return f"""本日は{today}です。この半日で使えるツール呼び出し残数はおよそ {budget_left} 回です。

あなたの受信箱:
{rendered}

これらをあなたの役割として処理してください。このturnでは、受信箱の先頭案件または同一申込IDの関連メッセージだけを処理し、ツール呼び出しは原則5回以内に収めてください。

ツール選択の注意:
- 顧客本人にはsend_chatを使わない。顧客への説明・確認・折返しはrecord_customer_contactを使う。
- 社内の同僚への相談、承認依頼の補足、申込担当への引継ぎだけsend_chatを使う。
- 統制に関わる行為（顧客接触、承認依頼、承認、差戻し、申込受付、本人確認、審査連携、契約、書面交付）の前にはsearch_corpusとread_documentを行い、実際に読んだdoc_id/version/citation_handleをbasis_jsonに含める。
- basis_jsonの最小形は {{"retrieved":[{{"doc_id":"実際に読んだdoc_id","version":"実際に読んだversion","citation_handle":"read_documentが返したhandle"}}],"construal":"読んだ文書からの解釈","decision":"選んだ行為","evidence_plan":"残す証跡","confidence":0.6}} です。値は実際に読んだ文書とhandleに合わせて変える。
- 顧客からの連絡を受けた販売担当は、読んだ文書に基づきrecord_customer_contactを残し、必要なら申込担当または営業管理者への確認をsend_chatで行う。
- 同僚からの連絡を受けた管理者・第二線は、読んだ文書に基づきapprove_applicationまたはreturn_applicationを選ぶ。
- 同僚からの連絡を受けた申込担当は、証跡が足りる場合だけsubmit_application以降の自分の役割の手続を進め、不足する場合はreturn_applicationまたは照会を選ぶ。

{mode_guidance}

過去に自分用メモを書いた可能性がある場合は、統制に関わる行為の前にrecall_notesで確認してください。この半日で完了できない事項は、保留の判断と相手への連絡を自分で選んでください。

業務記録の書き方は「事務連絡: 業務記録の作成要領」に従い、社内の記録・連絡は通常の業務用語で書いてください。"""


def _render_inbox_message(message: dict[str, Any]) -> str:
    """Render one inbox message as a natural business-record line for the
    seat prompt, instead of dumping raw JSON (kind/tick/from as literal
    keys, seat_id values like "emp-B"). A blind SME review flagged raw
    tool/JSON vocabulary and symbolic seat ids surfacing in generated
    records; since the seat LLM tends to echo the phrasing it is shown, this
    rendering is the first place that phrasing is set."""
    kind = str(message.get("kind") or "")
    tick = message.get("tick")
    when = render_tick_as_date(int(tick)) if isinstance(tick, int) else ""
    if kind == "customer_utterance":
        product = message.get("product") or ""
        application_id = message.get("application_id") or ""
        deadline = message.get("deadline_display") or ""
        utterance = message.get("utterance") or ""
        parts = [f"[{when}] 顧客連絡（案件{application_id}・{product}）"]
        if deadline:
            parts.append(f"希望期限: {deadline}")
        parts.append(f"内容: {utterance}")
        return " / ".join(parts)
    if kind == "chat":
        sender = display_name_for_seat(str(message.get("from") or ""))
        channel = message.get("channel") or ""
        body = message.get("body") or ""
        return f"[{when}] {sender}からの連絡（{channel}）: {body}"
    if kind == "timed_notice":
        notice = message.get("notice") or ""
        detail = message.get("detail") or ""
        return f"[{when}] 社内通知（{notice}）: {detail}"
    # Defensive fallback for any future inbox kind: still natural-language,
    # never a raw key/value dump.
    return f"[{when}] 連絡事項: {message.get('detail') or message.get('body') or message.get('utterance') or ''}".strip()


def _turn_mode_guidance(mode: TurnPromptMode) -> str:
    if mode == "measurement":
        return """この半日の判断では、world actionを無理に発生させる必要はありません。あなたの役割として、進める、追加確認する、何もしない、保留する、次ティックへ持ち越す、のいずれも自然な選択肢です。
ただし、保留・持ち越しを選ぶ場合は defer_or_hold で理由と次の一手を記録してください。顧客や他座席へ連絡が必要な場合だけ record_customer_contact または send_chat を使ってください。"""
    return """この半日では、「記録します」「残します」「確認します」と文章で宣言するだけで終了しない。必要な文書を読んだら、最終応答の前に record_customer_contact / request_approval / submit_application / approve_application / return_application のいずれか実際のworld toolを呼び出してください。"""


def _messages_require_world_action(messages: list[dict[str, Any]]) -> bool:
    return any(str(message.get("kind") or "") in {"customer_utterance", "chat"} for message in messages)


def _deliver_circulation_announcements(
    *,
    kernel: WorldKernel,
    announcements: list[dict[str, Any]],
    tick: int,
    seat_roles: dict[str, str],
    active_seats: set[str],
) -> None:
    """Diegetic notice circulation (default-off; MASTER_DESIGN.md section
    8.2/17.13/17.x): deliver each sealed announcement (world_config._circulation_announcements)
    whose tick matches the current tick, to every seat in this run whose role
    is in the announcement's visible_roles. Delivered as an ordinary
    `timed_notice` inbox message (kernel.enqueue_inbox validates it against
    the existing two-plane whitelist -- see kernel.INBOX_ALLOWED_KEYS -- so
    this mechanism cannot smuggle any experimenter-plane field into the
    world).

    Full-text delivery (approved 2026-07-06): the delivered text is
    announcement["message"] -- the header line plus the notice's own BODY
    text (mutations.circulation_message_text) -- not just its title. An
    era-5 raw-data audit found title-only circulation (announcement["digest"])
    never drew a single seat to read the underlying document across 5
    contradict seeds plus clarify/dangling runs, so delivery now IS content
    exposure; whether a seat ACTS on or cites the delivered content remains
    entirely behavioral."""
    for announcement in announcements:
        if int(announcement.get("tick") or 0) != tick:
            continue
        message = str(announcement.get("message") or "")
        if not message:
            continue
        visible_roles = set(str(role) for role in (announcement.get("visible_roles") or []))
        recipients = sorted(
            seat_id for seat_id in active_seats if seat_id in seat_roles and seat_roles[seat_id] in visible_roles
        )
        for seat_id in recipients:
            kernel.enqueue_inbox(seat_id, {"kind": "timed_notice", "tick": tick, "notice": "document_circulation", "detail": message})


def _tool_count(recorder: RunRecorder, seat_id: str, tools: set[str]) -> int:
    return recorder.successful_attempt_count(seat_id=seat_id, tools=tools, origin="agent")


def _retime_event(event: CustomerEvent, *, trigger_tick: int, deadline_tick: int) -> CustomerEvent:
    return CustomerEvent(**{**event.to_dict(), "trigger_tick": trigger_tick, "deadline_tick": deadline_tick})


def _instantiate_seat(factory: SeatFactory, *, seat_id: str, role: str, tools: list[Any], recorder: RunRecorder, recursion_limit: int, model: str):
    kwargs = {"seat_id": seat_id, "role": role, "tools": tools, "recorder": recorder, "recursion_limit": recursion_limit}
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        accepts_model = False
    else:
        accepts_model = "model" in signature.parameters or any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
    if accepts_model:
        kwargs["model"] = model
    return factory(**kwargs)


def kernel_profile(
    design: DesignInputs,
    knobs: dict[str, bool] | None = None,
    *,
    schedule: dict[str, Any] | None = None,
    scc_switch_enabled: bool = True,
    valid_doc_ids: set[str] | None = None,
) -> KernelProfile:
    schedule = schedule or {}
    doc_ids = set(design.documents) | {f"{doc_id}@v1.0" for doc_id in ("DFH-SAL-021", "DFH-SAL-045")}
    doc_ids.update(valid_doc_ids or set())
    return KernelProfile(
        knobs=dict(knobs or {}),
        valid_doc_ids=doc_ids,
        require_prior_read_for_basis=True,
        seat_roles={seat_id: seat.role for seat_id, seat in design.seats.items()},
        scc_switch_enabled=scc_switch_enabled,
        campaign_deadline_tick=int(schedule.get("campaign_deadline_tick") or 20),
        manager_absence_ticks=tuple(int(tick) for tick in (schedule.get("manager_absence_ticks") or [23, 24])),
        scc_switch_tick=schedule.get("scc_switch_tick"),
        month_end_tick=int(schedule.get("month_end_tick") or 40),
        timed_notice_recipients=tuple(str(seat_id) for seat_id in (schedule.get("timed_notice_recipients") or [])),
        approval_due_ticks=int(schedule.get("approval_due_ticks") or 2),
        approval_notice_recipients=tuple(str(seat_id) for seat_id in (schedule.get("approval_notice_recipients") or [])),
        time_pressure_notices=tuple(dict(item) for item in ((schedule.get("time_pressure") or {}).get("notices") or [])),
        consequences_mode=str((schedule.get("consequences") or {}).get("mode") or "off"),
        stall_after_ticks=int((schedule.get("consequences") or {}).get("stall_after_ticks") or 3),
        followup_recurrence=bool((schedule.get("consequences") or {}).get("recurrence")),
        motives_enabled=bool((schedule.get("motives") or {}).get("enabled")),
        sales_target=int((schedule.get("motives") or {}).get("sales_target") or 4),
        seat_qualifications={
            "emp-A": {"投資", "ロボアド"},
            "emp-B": {"保険"},
            "emp-F": {"加盟店"},
            "emp-G": {"銀行", "口座"},
            "emp-C": {"投資", "ロボアド", "保険", "加盟店", "銀行", "口座", "workflow"},
        },
    )


def write_config_snapshot(run_root: Path, payload: dict[str, object]) -> None:
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "config.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
