from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .agents import create_seat_agent, invoke_agent
from .corpus import Corpus
from .design_loader import DesignInputs
from .env import normalize_openrouter_model
from .kernel import KernelProfile, WorldKernel
from .recorder import RunRecorder
from .tools import build_role_tools


def make_run_root(root: Path, label: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return root / "runs" / f"{label}_{stamp}"


def run_s0(
    *,
    design: DesignInputs,
    corpus: Corpus,
    probe_id: str,
    seat_id: str,
    run_root: Path,
    live: bool,
    model: str | None = None,
) -> dict[str, str]:
    probe = design.probes[probe_id]
    seat = design.seats[seat_id]
    model_name = normalize_openrouter_model(model)
    recorder = RunRecorder(run_root, run_id=run_root.name, meta={"stage": "S0", "probe": probe_id, "seat": seat_id, "live": live, "model": model_name})
    write_config_snapshot(
        run_root,
        {
            "stage": "S0",
            "probe_id": probe_id,
            "seat_id": seat_id,
            "model": model_name,
            "world_config_hash": _text_hash(design.world_config_text),
            "anchor": False,
            "seed": 0,
        },
    )
    kernel = WorldKernel(recorder)
    prompt = _s0_prompt(design, probe_id)
    if not live:
        recorder.append_ledger("s0_prompt_generated", {"seat_id": seat_id, "probe_id": probe_id, "prompt": prompt})
        return {"mode": "dry-run", "prompt": prompt, "run_root": str(run_root)}
    tools = build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, seat_role=seat.role)
    agent = create_seat_agent(seat_id=seat_id, role=seat.role, tools=tools, model=model_name)
    output = invoke_agent(agent, prompt, recursion_limit=20)
    recorder.append_ledger("s0_agent_response", {"seat_id": seat_id, "probe_id": probe_id, "response": output})
    return {"mode": "live", "response": output, "run_root": str(run_root)}


def run_s1_episode(
    *,
    design: DesignInputs,
    corpus: Corpus,
    probe_id: str,
    seat_id: str,
    run_root: Path,
    live: bool,
    model: str | None = None,
    knobs: dict[str, bool] | None = None,
) -> dict[str, str]:
    probe = design.probes[probe_id]
    seat = design.seats[seat_id]
    model_name = normalize_openrouter_model(model)
    recorder = RunRecorder(run_root, run_id=run_root.name, meta={"stage": "S1", "probe": probe_id, "seat": seat_id, "live": live, "model": model_name, "knobs": knobs or {}})
    write_config_snapshot(
        run_root,
        {
            "stage": "S1",
            "probe_id": probe_id,
            "seat_id": seat_id,
            "model": model_name,
            "world_config_hash": _text_hash(design.world_config_text),
            "anchor": False,
            "seed": 0,
            "ticks": 6,
            "knobs": knobs or {},
        },
    )
    kernel = WorldKernel(recorder, KernelProfile(knobs=knobs or {}))
    for tick in range(1, 7):
        kernel.fire_timed_events(tick)
    prompt = _s1_prompt(probe_id, probe.title)
    if not live:
        recorder.append_ledger("s1_prompt_generated", {"seat_id": seat_id, "probe_id": probe_id, "prompt": prompt})
        return {"mode": "dry-run", "prompt": prompt, "run_root": str(run_root)}
    tools = build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, seat_role=seat.role)
    agent = create_seat_agent(seat_id=seat_id, role=seat.role, tools=tools, model=model_name)
    output = invoke_agent(agent, prompt, recursion_limit=30)
    recorder.append_ledger("s1_agent_response", {"seat_id": seat_id, "probe_id": probe_id, "response": output})
    return {"mode": "live", "response": output, "run_root": str(run_root)}


def run_s2_world(
    *,
    design: DesignInputs,
    corpus: Corpus,
    run_root: Path,
    live: bool,
    model: str | None = None,
    knobs: dict[str, bool] | None = None,
    seed: int = 0,
    max_agent_calls: int | None = None,
) -> dict[str, str]:
    model_name = normalize_openrouter_model(model)
    seat_ids = sorted(seat_id for seat_id in design.seats if seat_id.startswith("emp-"))
    recorder = RunRecorder(run_root, run_id=run_root.name, meta={"stage": "S2", "live": live, "model": model_name, "knobs": knobs or {}, "seed": seed})
    write_config_snapshot(
        run_root,
        {
            "stage": "S2",
            "seat_ids": seat_ids,
            "model": model_name,
            "world_config_hash": _text_hash(design.world_config_text),
            "anchor": False,
            "seed": seed,
            "ticks": 40,
            "knobs": knobs or {},
            "max_agent_calls": max_agent_calls,
        },
    )
    kernel = WorldKernel(recorder, KernelProfile(knobs=knobs or {}))
    calls = 0
    responses: list[dict[str, str]] = []
    for tick in range(1, 41):
        kernel.fire_timed_events(tick)
        recorder.append_ledger("tick_committed", {"tick": tick})
        if not live:
            continue
        for seat_id in seat_ids:
            if max_agent_calls is not None and calls >= max_agent_calls:
                continue
            seat = design.seats[seat_id]
            tools = build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, seat_role=seat.role)
            agent = create_seat_agent(seat_id=seat_id, role=seat.role, tools=tools, model=model_name)
            prompt = _s2_tick_prompt(tick)
            output = invoke_agent(agent, prompt, recursion_limit=20)
            recorder.append_ledger("s2_agent_response", {"seat_id": seat_id, "tick": tick, "response": output})
            responses.append({"seat_id": seat_id, "tick": str(tick), "response": output})
            calls += 1
    summary = {"mode": "live" if live else "dry-run", "run_root": str(run_root), "agent_calls": str(calls), "responses": json.dumps(responses, ensure_ascii=False)}
    recorder.write_json("run_summary.json", summary)
    return summary


def _s0_prompt(design: DesignInputs, probe_id: str) -> str:
    probe = design.probes[probe_id]
    topic = _public_probe_topic(probe.title)
    return f"""Document interpretation task.

Scenario: {probe.title}
Operational topic: {topic}

Task:
1. Call search_corpus exactly once. A good query is: 高齢者 脆弱顧客 追加確認 チェックシート
2. Call read_document exactly once for the most relevant doc_id.
3. Call record_interpretation_basis exactly once.
4. Return concise JSON with keys: scenario, likely_reading, cited_doc_ids, uncertainty, next_action.

Use only world-visible document IDs in citations.
"""


def _public_probe_topic(title: str) -> str:
    if "高齢者" in title or "旧版021" in title:
        return "elderly or vulnerable customer confirmation, supporting evidence, and checklist handling"
    if "管理者不在" in title or "代替承認" in title:
        return "approval route, substitute approver, and workflow evidence"
    if "商品説明事項" in title or "停止条件" in title:
        return "product-owner escalation route and documented notice"
    if "チャット" in title or "CP最終日" in title:
        return "campaign deadline, temporary approval, and searchable approval records"
    return "sales process control interpretation and required evidence"


def _s1_prompt(probe_id: str, title: str) -> str:
    return f"""Transaction episode.

Customer event: {title}

Handle one transaction episode as your seat.

Required protocol:
1. Call search_corpus once for the main control topic.
2. Call read_document once for the most relevant doc_id.
3. Call record_interpretation_basis once.
4. If an approval, customer contact, or application action is clearly needed, call exactly one workflow tool with basis_json. If not, do not call a workflow tool.

End with concise JSON: action_taken, application_or_event_id, evidence_recorded, unresolved_questions.
"""


def _s2_tick_prompt(tick: int) -> str:
    return f"""Daily workflow check.

Current tick: {tick}

Your queue has no customer transaction requiring action in this tick. Do not call any tool. Return concise JSON only: {{"action_taken": "none", "reason": "no queue item requiring action", "tick": {tick}}}.
"""


def write_config_snapshot(run_root: Path, payload: dict[str, object]) -> None:
    (run_root / "config.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _text_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
