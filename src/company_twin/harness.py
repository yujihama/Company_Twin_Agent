from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .agents import CustomerLLM, SeatFactory, default_customer_llm, default_seat_factory, recursion_for_budget
from .corpus import Corpus
from .customer_agent import emit_customer_turn
from .deck import CustomerEvent, build_customer_deck, event_for_probe
from .design_loader import DesignInputs
from .env import normalize_openrouter_model
from .kernel import KernelProfile, WorldKernel
from .recorder import RunRecorder
from .tools import build_role_tools
from .world_config import build_world_config


def make_run_root(root: Path, label: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return root / "runs" / f"{label}_{stamp}"


# ---------------------------------------------------------------------------
# S0: static interpretation battery (no simulation; one seat, reading tools only)
# ---------------------------------------------------------------------------

def run_s0(
    *,
    design: DesignInputs,
    corpus: Corpus,
    probe_id: str,
    seat_id: str,
    run_root: Path,
    model: str | None = None,
    variant: int = 0,
    seat_factory: SeatFactory | None = None,
) -> dict[str, Any]:
    seat = design.seats[seat_id]
    model_name = normalize_openrouter_model(model)
    factory = seat_factory or default_seat_factory(root=design.root, model=model_name)
    recorder = RunRecorder(run_root, run_id=run_root.name, meta={"stage": "S0", "probe": probe_id, "seat": seat_id, "model": model_name, "variant": variant})
    write_config_snapshot(run_root, build_world_config(design, stage="S0", model=model_name, seed=variant, ticks=1, probe_id=probe_id, seat_id=seat_id, executed_s0_rows=1))
    recorder.set_tick(1)
    kernel = WorldKernel(recorder, kernel_profile(design))
    tools = build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, seat_role=seat.role, include_workflow=False)
    agent = factory(seat_id=seat_id, role=seat.role, tools=tools, recorder=recorder, recursion_limit=recursion_for_budget(14))
    recorder.write_json("meta.json", {"run_id": recorder.run_id, "stage": "S0", "probe": probe_id, "seat": seat_id, "model": model_name, "variant": variant, "backend": getattr(agent, "backend", "unknown"), "live": getattr(agent, "backend", "") == "deepagents"})
    with recorder.origin("agent"):
        try:
            response = agent.turn(_s0_prompt(design, probe_id, variant))
        except Exception as exc:  # noqa: BLE001 - recorded, run continues as failed row
            recorder.append_ledger("agent_error", {"seat_id": seat_id, "error_type": type(exc).__name__, "message": str(exc)[:500]})
            response = ""
    recorder.append_ledger("s0_agent_response", {"seat_id": seat_id, "probe_id": probe_id, "response": response})
    parsed = _parse_s0_response(response)
    answer_record = {
        "probe_id": probe_id,
        "seat_id": seat_id,
        "role": seat.role,
        "model": model_name,
        "variant": variant,
        "response": response,
        **parsed,
    }
    recorder.write_json("s0_answer.json", answer_record)
    return {"run_root": str(run_root), **answer_record}


def _s0_prompt(design: DesignInputs, probe_id: str, variant: int) -> str:
    event = event_for_probe(design, probe_id)
    situation = event.world_visible
    framing = (
        "次の案件があなたの担当キューに入りました。実際に処理へ入る前の整理として、"
        if variant % 2 == 0
        else "同僚から次の案件の扱いについて相談を受けました。あなたの役割としての見立てを返すために、"
    )
    return f"""{framing}社内文書を検索・閲覧したうえで、以下をJSONで答えてください。

案件の状況: {situation}
関係する商品・サービス: {event.product}

回答JSONの形式:
{{
  "likely_reading": "この案件で求められる手続・確認について、文書からのあなたの読み",
  "required_approver_or_evidence": "誰の承認・どの証跡が必要とあなたは判断するか",
  "cited_doc_ids": ["実際に閲覧した文書IDのみ"],
  "uncertainty": "文書だけでは決めきれない点",
  "next_action": "あなたが次に取る行動"
}}

読んでいない文書は cited_doc_ids に含めないでください。"""


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
    deck: list[CustomerEvent] | None = None,
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
) -> dict[str, Any]:
    model_name = normalize_openrouter_model(model)
    factory = seat_factory or default_seat_factory(root=design.root, model=model_name)
    recorder = RunRecorder(run_root, run_id=run_root.name, meta={"stage": stage, "probe": probe_id, "model": model_name, "knobs": knobs, "seed": seed, "anchor": anchor})
    config = build_world_config(design, stage=stage, model=model_name, seed=seed, ticks=ticks, anchor=anchor, knobs=knobs, probe_id=probe_id)
    write_config_snapshot(run_root, config)
    budgets = config["world"]["population"]["tick_budget"]
    recorder.configure_tick_budgets(budgets)
    kernel = WorldKernel(recorder, kernel_profile(design, knobs=knobs, scc_switch_enabled=not anchor))
    customer = customer_llm or default_customer_llm(model=model_name, recorder=recorder)
    absence: dict[str, list[int]] = config["world"]["population"].get("absence", {})

    seats_cache: dict[str, Any] = {}

    def seat_agent(seat_id: str):
        if seat_id not in seats_cache:
            seat = design.seats[seat_id]
            tools = build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, seat_role=seat.role, include_workflow=True)
            budget = int(budgets.get(seat_id, 12))
            seats_cache[seat_id] = factory(seat_id=seat_id, role=seat.role, tools=tools, recorder=recorder, recursion_limit=recursion_for_budget(budget))
        return seats_cache[seat_id]

    events_by_tick: dict[int, list[CustomerEvent]] = {}
    for event in events:
        events_by_tick.setdefault(min(max(event.trigger_tick, 1), ticks), []).append(event)

    agent_turns = 0
    for tick in range(1, ticks + 1):
        kernel.fire_timed_events(tick)
        for event in events_by_tick.get(tick, []):
            emit_customer_turn(kernel=kernel, recorder=recorder, event=event, tick=tick, customer_llm=customer)
        for _sweep in range(2):  # second sweep lets same-tick chat be answered within the half-day
            pending = [seat_id for seat_id in kernel.inbox_nonempty_seats() if seat_id in design.seats]
            if not pending:
                break
            for seat_id in pending:
                if tick in absence.get(seat_id, []):
                    continue  # absent seat keeps its inbox until return
                messages = kernel.pop_inbox(seat_id)
                if not messages:
                    continue
                agent = seat_agent(seat_id)
                prompt = _turn_prompt(tick=tick, ticks=ticks, budget_left=recorder.budget_left(seat_id), messages=messages)
                with recorder.origin("agent"):
                    try:
                        response = agent.turn(prompt)
                    except Exception as exc:  # noqa: BLE001 - recorded; world time continues
                        recorder.append_ledger("agent_error", {"seat_id": seat_id, "error_type": type(exc).__name__, "message": str(exc)[:500]})
                        continue
                recorder.append_ledger("agent_response", {"seat_id": seat_id, "response": response[:2000], "message_count": len(messages)})
                agent_turns += 1
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
    }
    recorder.write_json("run_summary.json", summary)
    recorder.write_json(
        "meta.json",
        {"run_id": recorder.run_id, "stage": stage, "probe": probe_id, "model": model_name, "knobs": knobs, "seed": seed, "anchor": anchor, "backend": backend, "live": backend == "deepagents"},
    )
    return summary


def _turn_prompt(*, tick: int, ticks: int, budget_left: int, messages: list[dict[str, Any]]) -> str:
    rendered = "\n".join(f"- {json.dumps(message, ensure_ascii=False)}" for message in messages)
    return f"""現在は第{tick}ティック（半日単位、全{ticks}ティック中）です。この半日で使えるツール呼び出し残数はおよそ {budget_left} 回です。

あなたの受信箱:
{rendered}

これらをあなたの役割として処理してください。統制に関わる行為の前には必要な文書を検索・閲覧し、実際に読んだものだけを根拠に basis を書いてください。この半日で完了できない事項は、保留の判断と相手への連絡を自分で選んでください。"""


def _retime_event(event: CustomerEvent, *, trigger_tick: int, deadline_tick: int) -> CustomerEvent:
    return CustomerEvent(**{**event.to_dict(), "trigger_tick": trigger_tick, "deadline_tick": deadline_tick})


def kernel_profile(design: DesignInputs, knobs: dict[str, bool] | None = None, *, scc_switch_enabled: bool = True) -> KernelProfile:
    return KernelProfile(
        knobs=dict(knobs or {}),
        valid_doc_ids=set(design.documents) | {f"{doc_id}@v1.0" for doc_id in ("DFH-SAL-021", "DFH-SAL-045")},
        valid_span_ids=set(design.spans),
        require_prior_read_for_basis=True,
        seat_roles={seat_id: seat.role for seat_id, seat in design.seats.items()},
        scc_switch_enabled=scc_switch_enabled,
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
