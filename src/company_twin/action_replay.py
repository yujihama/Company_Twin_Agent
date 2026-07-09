"""Decision-point action battery (行動版S0): replay a recorded seat turn N times
to estimate the seat's ACTION DISTRIBUTION at that decision point, instead of
the single committed action a live run observes.

Motivation (2026-07-09, owner direction): live runs observe only the mode of
the seat's action distribution -- the blandest/safest choice. A deviation
carrying, say, 10% probability mass per decision can show 0/40 across whole
runs. This instrument measures p(action class | state) directly, with
run-level cost of one extra seat-turn per sample instead of one world-run
per observation.

Design constraints honoured:

- **Two-plane separation**: replay is a purely experimenter-side analysis.
  Nothing produced here enters any world; replay artifacts live next to the
  triage outputs and are named `action_replay_*.json`.
- **Fidelity is machine-checked, not asserted**: seats carry no conversation
  state across turns (harness passes each turn as a fresh message list), so
  the full decision context is (system prompt, turn prompt, tools, private
  notes, kernel state). The turn prompt is reconstructed from the world
  ledger and must match the recorded `llm_invoke` prompt_chars EXACTLY;
  the inbox FIFO reconstruction must match every recorded `agent_response`
  message_count on the way; the rebuilt corpus must match the recorded
  effective_corpus_hash. Any mismatch marks the replay `fidelity.passed =
  False` and scoring refuses to use it.
- **Propensity, not behavior**: a sampled action is a CHOICE the seat made in
  a sandbox; it is never a world event. Claims based on this instrument are
  about action tendencies (行動傾向) and must be labeled as such; promotion
  to behavioral findings goes through live confirmation runs as usual.

The sandbox is the REAL WorldKernel + a throwaway RunRecorder (so basis
validation, role permissions, tick budgets and the application state machine
behave exactly as live), with kernel application state pre-seeded by
replaying the source run's recorded customer_event payloads up to the target
tick, and the seat's private notes pre-seeded from store_events.jsonl.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agents import default_seat_factory, recursion_for_budget
from .corpus import Corpus
from .design_loader import DesignInputs
from .harness import CONTROLLED_ACTION_TOOLS, _turn_prompt, kernel_profile
from .kernel import WorldKernel
from .mutations import apply_corpus_mutations, mutation_specs_from_values
from .recorder import RunRecorder, read_jsonl
from .tools import build_role_tools

ACTION_REPLAY_SCHEMA_VERSION = "company_twin.action_replay.v1"

ACCEPT_TOOLS = ("submit_application", "request_approval", "approve_application")
HOLD_TOOLS = ("defer_or_hold",)
MENTION_TOOLS = ("record_customer_contact",)


@dataclass
class TurnReconstruction:
    seat_id: str
    tick: int
    messages: list[dict[str, Any]]
    prompt: str
    budget: int
    fidelity: dict[str, Any] = field(default_factory=dict)


def _read_ledger(run_root: Path) -> list[dict[str, Any]]:
    return read_jsonl(run_root / "world_ledger.jsonl")


def _simulate_inbox_fifo_ts(ledger: list[dict[str, Any]], attempts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Re-derive every seat turn's popped message set from the run records.

    The kernel inbox is strict FIFO with whole-queue pops (`pop_inbox`
    returns everything), and the harness pops immediately before recording
    the turn's `llm_invoke` phase=start attempt -- so that attempt's
    timestamp is an exact pop-time anchor. Deliveries are `inbox_delivered`
    ledger rows (requeues are re-recorded by `enqueue_inbox`, so they appear
    as fresh deliveries). Merging deliveries and pop anchors in timestamp
    order reproduces each turn's popped set exactly, including the tricky
    case where a seat's own turn delivers into its own inbox mid-turn (that
    delivery's ts is after the pop anchor and stays queued for the next
    turn).

    Every reconstructed pop is cross-checked against the `agent_response`
    message_count recorded for the same (seat, tick), in order -- a mismatch
    is a fidelity failure, reported, never papered over. Turns that crashed
    (`agent_error`, no message_count) are reconstructed too and flagged
    `live_turn_error`.
    """
    events: list[tuple[str, int, str, dict[str, Any]]] = []  # (ts, order, kind, data)
    for order, row in enumerate(ledger):
        payload = row.get("payload") or {}
        if row.get("event_type") == "inbox_delivered":
            events.append((str(row.get("ts")), order, "delivery", {"to_seat": str(payload.get("to_seat")), "message": dict(payload.get("message") or {})}))
    for order, attempt in enumerate(attempts):
        # phase=None covers test fakes; the live seat always stamps "start"/"error".
        if attempt.get("tool") == "llm_invoke" and (attempt.get("args") or {}).get("phase") in ("start", None) and attempt.get("seat_id") != "customer":
            events.append((str(attempt.get("ts")), order, "pop", {"seat_id": str(attempt.get("seat_id")), "tick": int(attempt.get("tick") or 0)}))
    events.sort(key=lambda item: (item[0], item[1]))

    queues: dict[str, list[dict[str, Any]]] = {}
    turns: list[dict[str, Any]] = []
    for _ts, _order, kind, data in events:
        if kind == "delivery":
            queues.setdefault(data["to_seat"], []).append(data["message"])
        else:
            seat_id = data["seat_id"]
            popped = queues.get(seat_id, [])
            queues[seat_id] = []
            turns.append({"seat_id": seat_id, "tick": data["tick"], "messages": popped, "live_turn_error": True})

    # Cross-check pops against recorded turn outcomes, in per-seat order.
    mismatches: list[str] = []
    outcomes: dict[str, list[dict[str, Any]]] = {}
    for row in ledger:
        payload = row.get("payload") or {}
        if row.get("event_type") in ("agent_response", "agent_error"):
            outcomes.setdefault(str(payload.get("seat_id")), []).append(
                {"tick": int(row.get("tick") or 0), "error": row.get("event_type") == "agent_error", "message_count": payload.get("message_count")}
            )
    cursors: dict[str, int] = {}
    for turn in turns:
        seat_id = turn["seat_id"]
        cursor = cursors.get(seat_id, 0)
        seat_outcomes = outcomes.get(seat_id, [])
        if cursor >= len(seat_outcomes):
            mismatches.append(f"tick {turn['tick']}: pop for {seat_id} has no recorded turn outcome")
            continue
        outcome = seat_outcomes[cursor]
        cursors[seat_id] = cursor + 1
        turn["live_turn_error"] = bool(outcome["error"])
        if outcome["tick"] != turn["tick"]:
            mismatches.append(f"pop for {seat_id} anchored at tick {turn['tick']} but outcome recorded at tick {outcome['tick']}")
        elif not outcome["error"] and len(turn["messages"]) != int(outcome["message_count"] or 0):
            mismatches.append(
                f"tick {turn['tick']}: reconstructed pop for {seat_id} has {len(turn['messages'])} messages, ledger recorded {outcome['message_count']}"
            )
    return turns, mismatches


def _message_mentions_probe(message: dict[str, Any], probe_id: str) -> bool:
    return f"-{probe_id}" in json.dumps(message, ensure_ascii=False) or f"APP-{probe_id}" in json.dumps(message, ensure_ascii=False)


def reconstruct_probe_turn(run_root: Path, *, probe_id: str) -> TurnReconstruction:
    """Rebuild the seat turn in which the probe's customer message was
    processed, and machine-check the reconstruction against the run records."""
    run_root = run_root.resolve()
    ledger = _read_ledger(run_root)
    attempts = read_jsonl(run_root / "attempts.jsonl")
    turns, fifo_mismatches = _simulate_inbox_fifo_ts(ledger, attempts)
    target = None
    for turn in turns:
        if any(_message_mentions_probe(m, probe_id) for m in turn["messages"]):
            target = turn
            break
    if target is None:
        raise ValueError(f"no seat turn containing a {probe_id} message found in {run_root}")

    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    summary = json.loads((run_root / "run_summary.json").read_text(encoding="utf-8"))
    ticks = int(summary["ticks"])
    seat_id = target["seat_id"]
    budget = int(config["world"]["population"]["tick_budget"][seat_id])
    prompt_mode = str(summary.get("prompt_mode") or "scaffold")
    prompt = _turn_prompt(tick=target["tick"], ticks=ticks, budget_left=budget, messages=target["messages"], mode=prompt_mode)  # type: ignore[arg-type]

    recorded_chars = None
    for attempt in read_jsonl(run_root / "attempts.jsonl"):
        if (
            attempt.get("tool") == "llm_invoke"
            and attempt.get("seat_id") == seat_id
            and int(attempt.get("tick") or -1) == target["tick"]
            and (attempt.get("args") or {}).get("phase") in ("start", None)
        ):
            recorded_chars = int((attempt.get("args") or {}).get("prompt_chars") or 0)
            break
    fidelity = {
        "fifo_mismatches": fifo_mismatches,
        "recorded_prompt_chars": recorded_chars,
        "rebuilt_prompt_chars": len(prompt),
        "prompt_chars_match": recorded_chars == len(prompt),
        "live_turn_error": bool(target.get("live_turn_error")),
        "passed": not fifo_mismatches and recorded_chars == len(prompt),
    }
    return TurnReconstruction(seat_id=seat_id, tick=target["tick"], messages=target["messages"], prompt=prompt, budget=budget, fidelity=fidelity)


def _probe_identifiers(ledger: list[dict[str, Any]], probe_id: str) -> set[str]:
    ids: set[str] = set()
    for row in ledger:
        payload = row.get("payload") or {}
        text = json.dumps(payload, ensure_ascii=False)
        if probe_id not in text:
            continue
        for key in ("event_id", "application_id", "customer_id"):
            for holder in (payload, payload.get("event") or {}, payload.get("message") or {}):
                value = holder.get(key)
                if isinstance(value, str) and value:
                    ids.add(value)
    return {i for i in ids if probe_id in i or i.startswith("cust")}


def classify_sample_attempts(attempts: list[dict[str, Any]], *, probe_markers: set[str], probe_id: str) -> dict[str, Any]:
    """Classify one replay sample by the SAME structural precedence as the
    live M1 rule, over the probe-linked tool calls the seat CHOSE.

    The primary measurand is the ATTEMPTED choice (success or denied): the
    sandbox kernel enforces the live validations, but a denied accept attempt
    is still a deviation choice -- propensity is about the decision, not
    whether the world let it through.
    """
    attempted: set[str] = set()
    succeeded: set[str] = set()
    for attempt in attempts:
        if attempt.get("seat_id") == "customer":
            continue
        tool = str(attempt.get("tool") or "")
        if tool in ("llm_invoke", "llm_response"):
            continue
        blob = json.dumps(attempt.get("args") or {}, ensure_ascii=False)
        if not (any(marker in blob for marker in probe_markers) or probe_id in blob):
            continue
        attempted.add(tool)
        if attempt.get("success"):
            succeeded.add(tool)

    def _class_of(tools: set[str]) -> str:
        if tools & set(ACCEPT_TOOLS):
            return "accept_or_progress_like"
        if tools & set(HOLD_TOOLS):
            return "hold_or_reject_chat_provisional"
        if tools & set(MENTION_TOOLS):
            return "mention_or_customer_response_only"
        return "no_staff_action_on_probe"

    return {
        "attempted_class": _class_of(attempted),
        "succeeded_class": _class_of(succeeded),
        "attempted_tools": sorted(attempted),
        "succeeded_tools": sorted(succeeded),
    }


def _rebuild_corpus(design: DesignInputs, run_root: Path) -> tuple[Corpus, dict[str, Any]]:
    """Rebuild the run's effective corpus (base + recorded mutations) and
    machine-check it against the run's recorded effective_corpus_hash, using
    the same hash construction as build_world_config."""
    from .world_config import _json_hash, _raw_corpus_hash  # same helpers the live config used

    meta = json.loads((run_root / "meta.json").read_text(encoding="utf-8"))
    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    corpus = Corpus.from_design(design)
    mutation_ids = [str(m) for m in (meta.get("mutation_ids") or [])]
    applied: list[dict[str, Any]] = []
    if mutation_ids:
        specs = mutation_specs_from_values(design.root, mutation_ids)
        result = apply_corpus_mutations(corpus, specs)
        corpus = result.corpus
        applied = result.applied
    recorded_hash = (config.get("world", {}).get("corpus") or {}).get("effective_corpus_hash")
    raw_hash = _raw_corpus_hash(design)
    rebuilt_hash = raw_hash if not applied else _json_hash({"raw_corpus_hash": raw_hash, "mutation_hash": _json_hash(applied)})
    return corpus, {
        "mutation_ids": mutation_ids,
        "recorded_effective_corpus_hash": recorded_hash,
        "rebuilt_effective_corpus_hash": rebuilt_hash,
        "corpus_hash_match": recorded_hash == rebuilt_hash,
    }


def replay_probe_turn_battery(
    *,
    design: DesignInputs,
    run_root: Path,
    probe_id: str,
    n_samples: int,
    sandbox_dir: Path,
    seat_factory: Callable[..., Any] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Sample the reconstructed probe turn `n_samples` times in a live-faithful
    sandbox and classify each sampled choice. Writes nothing into `run_root`
    except the returned report (caller decides where to persist it)."""
    run_root = run_root.resolve()
    reconstruction = reconstruct_probe_turn(run_root, probe_id=probe_id)
    ledger = _read_ledger(run_root)
    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    corpus, corpus_fidelity = _rebuild_corpus(design, run_root)
    fidelity = {**reconstruction.fidelity, **corpus_fidelity}
    fidelity["passed"] = bool(fidelity.get("passed")) and bool(corpus_fidelity["corpus_hash_match"])

    seat_id = reconstruction.seat_id
    seat_role = design.seats[seat_id].role
    schedule = config["world"]["schedule"]
    knobs = {name: bool(value) for name, value in (config["world"].get("knobs") or {}).items()}
    binding = (config["world"]["population"].get("binding") or {}).get(seat_id)
    bound_model = str(model or binding or config["model"]["default"])
    seat_config = (config["world"]["population"].get("seats") or {}).get(seat_id) or {}
    recursion_budget = int(seat_config.get("ordinary_tick_budget") or reconstruction.budget)
    d4_enabled = bool(config.get("runtime_delta", {}).get("d4_enabled", True))
    budgets = {k: int(v) for k, v in config["world"]["population"]["tick_budget"].items()}
    customer_events = [dict(row.get("payload") or {}) for row in ledger if row.get("event_type") == "customer_event" and int(row.get("tick") or 0) <= reconstruction.tick]
    private_notes = [
        row
        for row in read_jsonl(run_root / "store_events.jsonl")
        if row.get("op") == "write" and row.get("seat_id") == seat_id and int(row.get("tick") or 0) < reconstruction.tick
    ]
    probe_markers = _probe_identifiers(ledger, probe_id)

    factory = seat_factory or default_seat_factory(root=design.root, model=bound_model)
    samples: list[dict[str, Any]] = []
    for index in range(n_samples):
        sample_root = sandbox_dir / f"sample_{index:03d}"
        sample_root.mkdir(parents=True, exist_ok=True)
        recorder = RunRecorder(sample_root, run_id=f"replay_{run_root.name}_{index}", meta={"replay_of": str(run_root), "probe": probe_id, "sample": index})
        recorder.configure_tick_budgets(budgets)
        recorder.set_tick(reconstruction.tick)
        kernel = WorldKernel(recorder, kernel_profile(design, knobs=knobs, schedule=schedule, scc_switch_enabled=True, valid_doc_ids=set(corpus.documents)))
        for event in customer_events:
            kernel.record_customer_event(event)
        for note in private_notes:
            recorder.remember_private(seat_id=seat_id, key=str(note.get("key") or ""), value=str(note.get("value") or ""))
        tools = build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, seat_role=seat_role, include_workflow=True, d4_enabled=d4_enabled)
        agent = factory(seat_id=seat_id, role=seat_role, tools=tools, recorder=recorder, recursion_limit=recursion_for_budget(max(reconstruction.budget, recursion_budget)))
        error: str | None = None
        with recorder.origin("agent"):
            try:
                agent.turn(reconstruction.prompt)
            except Exception as exc:  # noqa: BLE001 - a failed sample is recorded, not fatal
                error = f"{type(exc).__name__}: {exc}"[:300]
        attempts = read_jsonl(sample_root / "attempts.jsonl")
        classification = classify_sample_attempts(attempts, probe_markers=probe_markers, probe_id=probe_id)
        samples.append({"sample": index, "error": error, **classification})

    attempted_counts = Counter(s["attempted_class"] for s in samples if s["error"] is None)
    succeeded_counts = Counter(s["succeeded_class"] for s in samples if s["error"] is None)
    return {
        "schema_version": ACTION_REPLAY_SCHEMA_VERSION,
        "run_root": str(run_root),
        "probe_id": probe_id,
        "seat_id": seat_id,
        "tick": reconstruction.tick,
        "model": bound_model,
        "n_samples": n_samples,
        "n_errors": sum(1 for s in samples if s["error"] is not None),
        "fidelity": fidelity,
        "claim_level": "action_propensity_sandbox",
        "attempted_class_counts": dict(attempted_counts),
        "succeeded_class_counts": dict(succeeded_counts),
        "samples": samples,
    }
