"""Layer 3: branch execution (MASTER_DESIGN.md §17.37, owner approval #19,
docs/progress/option_branch_instrument_design_20260713.md).

Layer 3 answers a question neither the loss-event oracle (§17.26) nor the
action-distribution battery (§17.22) can: when a rarely-chosen (or
experimenter-injected) option actually happens, does the organization notice?
Direct detection coverage for R1-R4 is currently `uncovered` in every natural
run (§17.27), and true deviations occur in well under 1% of natural samples
(phase3_d1_replay_results_20260709.md), so there is structurally no chance to
exercise detection coverage under operating conditions without an artificial
occurrence.

This module never touches any live/replay run. It:

1. Rebuilds a REAL WorldKernel's application state (`WorldKernel.applications`)
   from one existing run's `world_ledger.jsonl`, up to a fork tick, after
   first validating that ledger's hash chain (fail closed on any break).
2. Injects exactly one experimenter-plane kernel tool call into that
   rebuilt kernel, under a new recorder origin scoped to branch bundles only.
3. Optionally continues the world with live seats for N ticks -- gated so the
   live path is unreachable unless a caller explicitly sets
   `allow_spend=True`, which nothing in this change (CLI or tests) ever does.
4. Runs the UNCHANGED loss-event oracle / monitoring join against the
   finalized bundle so detection coverage is measured by the same machinery
   that already exists, never a bespoke check invented for this instrument.

Design doc §5.3 resolution, followed literally here: `ALLOWED_ORIGINS` in
recorder.py stays untouched (a global whitelist every live/replay acceptance
check trusts); a new origin value is instead scoped to a `RunRecorder`
subclass used ONLY for branch bundles (`BranchRunRecorder` below), via
`ALLOWED_ORIGINS | {"experimenter_injection"}`.  The injected action is
executed by calling the same real kernel tool method a seat would call, under
that origin -- kernel logic never branches on who is calling it, and
world-visible text (`harness._render_inbox_message`, `harness._turn_prompt`)
never reads `origin`, so downstream seats see an ordinary business event.
The distinction survives only in `attempts.jsonl`'s `origin` field.

Every branch bundle's `meta.json` carries `run_class: "branch_injection"`
(§17.28 precedent: `campaign_role: "feasibility_pilot"` scopes pooling the
same way). `acceptance.py` and `loss_campaign.py` reject any bundle carrying
that run_class before doing any other work (see the fail-closed checks added
in those modules alongside this one).
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

from .agents import CustomerLLM, SeatFactory, default_customer_llm, default_seat_factory, recursion_for_budget
from .corpus import Corpus
from .customer_agent import CustomerActor, emit_customer_followup, emit_customer_reply, emit_customer_turn
from .deck import CustomerEvent
from .design_loader import DesignInputs, load_design
from .harness import _contact_directory_text, _instantiate_seat, _turn_prompt, kernel_profile as _harness_kernel_profile
from .kernel import CONTROLLED_TOOLS, WorldKernel
from .loss_monitoring import WORLD_CONFIG_SCHEMA_VERSION, write_loss_event_monitoring
from .loss_oracle import loss_event_findings
from .recorder import ALLOWED_ORIGINS, RunRecorder, read_jsonl
from .tools import build_role_tools

BRANCH_RUN_CLASS = "branch_injection"
BRANCH_CLAIM_LEVEL = "detection_coverage_probe"
INJECTION_ORIGIN = "experimenter_injection"

# Per-seat outstanding work queues, written at the end of every branch bundle
# so a fork taken from that bundle restores the exact state rather than an
# inferred one. See `reconstruct_pending_work` for why the inferred path still
# has to exist (a fork taken mid-run has no snapshot to read).
PENDING_WORK_FILE = "pending_work.json"

_ID_SUFFIX = re.compile(r"^[A-Z][A-Z0-9]*-(\d{6})$")

# Application-lifecycle ledger event types whose payloads carry enough state
# to rebuild `WorldKernel.applications` faithfully (kernel.py's own
# `APPLICATION_STATES` transitions, plus the two terminal non-completion
# outcomes and the approval sub-records).
_APPLICATION_STATE_EVENTS = frozenset(
    {
        "application_drafted",
        "application_submitted",
        "identity_verified",
        "review_linked",
        "contract_completed",
        "documents_delivered",
        "application_returned",
        "customer_withdrawal",
        "approval_requested",
        "approval_granted",
    }
)


class BranchExecutionError(ValueError):
    """Raised for any fail-closed branch-execution violation."""


class BranchRunRecorder(RunRecorder):
    """RunRecorder used only for branch-injection bundles.

    Design doc §5.3: `ALLOWED_ORIGINS` in recorder.py must stay untouched (it
    is the global whitelist `acceptance.py` enforces on every live/replay
    run), so the new `experimenter_injection` origin is scoped here instead,
    to instances of this subclass only. Overriding `origin()` (rather than
    mutating the shared frozenset) means every OTHER run in the system keeps
    exactly its current fail-closed origin whitelist.
    """

    _EXTRA_ALLOWED_ORIGINS = frozenset({INJECTION_ORIGIN})

    @contextmanager
    def origin(self, origin: str) -> Iterator[None]:
        allowed = ALLOWED_ORIGINS | self._EXTRA_ALLOWED_ORIGINS
        if origin not in allowed:
            raise ValueError(f"origin '{origin}' is not allowed; allowed={sorted(allowed)}")
        previous = self._origin
        self._origin = origin
        try:
            yield
        finally:
            self._origin = previous


def _validate_source_ledger_chain(ledger: list[dict[str, Any]]) -> None:
    """Fail closed on any hash-chain break BEFORE any state is reconstructed."""
    previous_hash = ""
    for ordinal, row in enumerate(ledger):
        if row.get("prev_hash") != previous_hash:
            raise BranchExecutionError(f"source world ledger hash chain breaks at ordinal {ordinal}")
        recorded_hash = str(row.get("hash") or "")
        canonical = {key: value for key, value in row.items() if key != "hash"}
        expected_hash = hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if not recorded_hash or recorded_hash != expected_hash:
            raise BranchExecutionError(f"source world ledger row hash is invalid at ordinal {ordinal}")
        previous_hash = recorded_hash


def _apply_ledger_event(applications: dict[str, dict[str, Any]], *, event_type: str, tick: int, payload: dict[str, Any]) -> None:
    """Mutate a `WorldKernel.applications`-shaped dict the same way the real
    kernel method that produced this ledger row would have mutated it.

    This is direct application-dict reconstruction (the design doc's second
    option), not a replay through the actual kernel tool methods: those
    methods re-validate role permissions, knobs, and a `basis` grounded in
    `read_document` calls recorded on the SAME recorder instance, none of
    which exist for a freshly attached branch recorder. `identity_verified`'s
    underlying eKYC/sanctions/consent values are never recorded anywhere in
    the ledger (the real kernel method doesn't put them in its own ledger
    payload either -- see kernel.py's `verify_identity`), so a successful
    verification is reconstructed with placeholder-but-truthy evidence: the
    KEY SET matches the source exactly, only the literal values differ.
    """
    if event_type not in _APPLICATION_STATE_EVENTS:
        return
    if event_type == "application_drafted":
        app_id = str(payload.get("application_id") or "")
        if not app_id:
            return
        app = dict(payload)
        app.setdefault("history", [])
        app.setdefault("evidence", {})
        applications[app_id] = app
        return
    app_id = str(payload.get("application_id") or "")
    if not app_id or app_id not in applications:
        return
    app = applications[app_id]
    if event_type == "application_submitted":
        if payload.get("customer_id"):
            app["customer_id"] = payload["customer_id"]
        if payload.get("product"):
            app["product"] = payload["product"]
        app["evidence"] = {**app.get("evidence", {}), **(payload.get("evidence") or {})}
        app["status"] = str(payload.get("status") or app.get("status") or "application_received")
        app.setdefault("history", []).append({"tick": tick, "state": app["status"], "reason": "application_submitted"})
    elif event_type == "identity_verified":
        app["evidence"] = {
            **app.get("evidence", {}),
            "ekyc_completed": True,
            "sanctions_non_hit": True,
            "consent_log_id": app.get("evidence", {}).get("consent_log_id") or f"RECONSTRUCTED-{app_id}",
        }
        app["status"] = str(payload.get("status") or "identity_verified")
        app.setdefault("history", []).append({"tick": tick, "state": app["status"], "reason": "identity_verified"})
    elif event_type == "review_linked":
        app["status"] = str(payload.get("status") or "review_linked")
        app.setdefault("history", []).append({"tick": tick, "state": app["status"], "reason": "review_linked"})
    elif event_type == "contract_completed":
        app["status"] = str(payload.get("status") or "contracted")
        app.setdefault("history", []).append({"tick": tick, "state": app["status"], "reason": "contract_completed"})
    elif event_type == "documents_delivered":
        app["status"] = str(payload.get("status") or "documents_delivered")
        app.setdefault("history", []).append({"tick": tick, "state": app["status"], "reason": "documents_delivered"})
    elif event_type == "application_returned":
        app["status"] = "returned"
        app.setdefault("history", []).append({"tick": tick, "state": "returned", "reason": str(payload.get("reason") or "application_returned")})
    elif event_type == "customer_withdrawal":
        app["status"] = "withdrawn"
        app.setdefault("history", []).append({"tick": tick, "state": "withdrawn", "reason": "customer withdrew after repeated unanswered follow-ups"})
    elif event_type in ("approval_requested", "approval_granted"):
        app.setdefault("approvals", []).append(dict(payload))
    app["last_staff_action_tick"] = tick
    if event_type in ("application_submitted", "identity_verified", "review_linked", "contract_completed", "documents_delivered"):
        app["progressed_tick"] = tick


def _apply_inbox_event(
    queues: dict[str, list[dict[str, Any]]], *, event_type: str, payload: dict[str, Any]
) -> None:
    """Track one ledger row's effect on the seats' outstanding work queues.

    Only three row types move work: a delivery adds an item, a completed seat
    turn removes exactly the items that turn was handed (`message_count` is
    recorded by the harness for precisely this reason), and a failed seat turn
    removes everything -- the pop happened before the agent raised, and the S1
    requeue path puts the items back as fresh `inbox_delivered` rows that this
    same loop then re-adds.
    """
    if event_type == "inbox_delivered":
        seat_id = str(payload.get("to_seat") or "")
        message = payload.get("message")
        if seat_id and isinstance(message, dict):
            queues.setdefault(seat_id, []).append(dict(message))
    elif event_type == "agent_response":
        seat_id = str(payload.get("seat_id") or "")
        handled = int(payload.get("message_count") or 0)
        if seat_id in queues and handled > 0:
            del queues[seat_id][:handled]
    elif event_type == "agent_error":
        seat_id = str(payload.get("seat_id") or "")
        if seat_id in queues:
            queues[seat_id] = []


def reconstruct_pending_work(ledger: list[dict[str, Any]], *, up_to_ordinal: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Rebuild every seat's outstanding work queue from a run's own ledger.

    A branch that starts with empty queues is not a copy of the source world:
    the AI employees only take a turn when something is waiting for them, so
    an empty start freezes the whole organization and makes "nobody noticed"
    an artifact of the fork rather than an observation about the controls.

    Known imprecision, deliberately left in: a seat pops its whole queue just
    before its turn, and only the completed turn is recorded, so a fork taken
    part-way through a seat's turn still shows that seat holding the items it
    was already working on. That seat may therefore handle them again after
    the fork. The error direction is more seat activity, not less -- it can
    make a branch look caught, never falsely uncaught -- which is why it is
    reported rather than guessed away.
    """
    rows = ledger if up_to_ordinal is None else ledger[: max(int(up_to_ordinal), 0)]
    queues: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        _apply_inbox_event(
            queues,
            event_type=str(row.get("event_type") or ""),
            payload=dict(row.get("payload") or {}),
        )
    return {seat_id: messages for seat_id, messages in queues.items() if messages}


def _highest_generated_id(payload: dict[str, Any]) -> int:
    """Largest `PREFIX-000123` counter appearing in one ledger payload."""
    highest = 0
    for value in payload.values():
        if not isinstance(value, str):
            continue
        match = _ID_SUFFIX.match(value)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest


def _read_pending_work_snapshot(source_run_root: Path) -> dict[str, Any] | None:
    path = Path(source_run_root) / PENDING_WORK_FILE
    if not path.exists():
        return None
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BranchExecutionError(f"pending-work snapshot is not readable: {path} ({exc})") from exc
    return snapshot if isinstance(snapshot, dict) else None


def rebuild_kernel_state(
    source_run_root: Path,
    up_to_tick: int,
    output_run_root: Path,
    *,
    design_root: Path | None = None,
    up_to_ordinal: int | None = None,
) -> tuple[WorldKernel, dict[str, Any]]:
    """Validate the source ledger's hash chain, then deterministically
    reconstruct `WorldKernel.applications` by replaying its lifecycle events
    up to `up_to_tick`, attaching a NEW `BranchRunRecorder` rooted at
    `output_run_root`.

    With `up_to_ordinal` set, the fork point is a LEDGER ROW instead of a
    tick boundary: rows with ordinal < up_to_ordinal are replayed, strictly
    in chain order, and `up_to_tick` is ignored. This is what makes an
    injection point exist inside a tick -- in the confirmatory v4 campaign
    every approval-required application completed within the same tick it
    was review-linked, so a tick-granularity fork has no valid injection
    point at all (§17.40 boundary)."""
    source_run_root = Path(source_run_root).resolve()
    output_run_root = Path(output_run_root).resolve()
    ledger_path = source_run_root / "world_ledger.jsonl"
    source_ledger = read_jsonl(ledger_path)
    if not source_ledger:
        raise BranchExecutionError(f"source world ledger is empty: {ledger_path}")
    _validate_source_ledger_chain(source_ledger)  # fail closed BEFORE any reconstruction
    source_ledger_sha256 = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    if up_to_ordinal is not None:
        if not isinstance(up_to_ordinal, int) or isinstance(up_to_ordinal, bool):
            raise BranchExecutionError("up_to_ordinal must be an integer ledger ordinal")
        if up_to_ordinal < 1 or up_to_ordinal > len(source_ledger):
            raise BranchExecutionError(
                f"up_to_ordinal {up_to_ordinal} is outside the source ledger (1..{len(source_ledger)})"
            )
        up_to_tick = int(source_ledger[up_to_ordinal - 1].get("tick") or 0)

    meta_path = source_run_root / "meta.json"
    source_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    config_path = source_run_root / "config.json"
    source_config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}

    design = load_design(Path(design_root) if design_root is not None else Path.cwd())
    schedule = ((source_config.get("world") or {}).get("schedule") or {})
    # A branch bundle's own schedule.ticks is just where ITS run stopped; the
    # real horizon of the world line is the original source world's, carried
    # through every generation via meta["source_horizon"]. The key's PRESENCE
    # is what matters: an explicit 0 means "the original never declared a
    # horizon" and must not fall back to the branch's own stop tick.
    if "source_horizon" in source_meta:
        source_horizon = int(source_meta.get("source_horizon") or 0)
    else:
        source_horizon = int(schedule.get("ticks") or 0)
    profile = _harness_kernel_profile(design, schedule=schedule, scc_switch_enabled=False, valid_doc_ids=set())

    recorder = BranchRunRecorder(
        output_run_root,
        run_id=output_run_root.name,
        meta={
            "run_class": BRANCH_RUN_CLASS,
            "claim_level": BRANCH_CLAIM_LEVEL,
            "source_run_root": str(source_run_root),
            "source_ledger_sha256": source_ledger_sha256,
            "fork_tick": int(up_to_tick),
            "fork_ordinal": up_to_ordinal,
            "stage": source_meta.get("stage"),
            "seed": source_meta.get("seed"),
        },
    )
    kernel = WorldKernel(recorder, profile)

    replayed_rows = 0
    pending_work: dict[str, list[dict[str, Any]]] = {}
    highest_id = 0
    for ordinal, row in enumerate(source_ledger):
        tick = int(row.get("tick") or 0)
        if up_to_ordinal is not None:
            if ordinal >= up_to_ordinal:
                break
        elif tick > int(up_to_tick):
            continue
        event_type = str(row.get("event_type") or "")
        payload = dict(row.get("payload") or {})
        recorder.set_tick(tick)
        recorder.append_ledger(event_type, payload)
        _apply_ledger_event(kernel.applications, event_type=event_type, tick=tick, payload=payload)
        _apply_inbox_event(pending_work, event_type=event_type, payload=payload)
        highest_id = max(highest_id, _highest_generated_id(payload))
        replayed_rows += 1
    recorder.set_tick(int(up_to_tick))
    pending_work = {seat_id: messages for seat_id, messages in pending_work.items() if messages}

    # Restore what each seat still had waiting. Prefer the exact snapshot the
    # source bundle wrote for itself; fall back to the ledger reconstruction
    # for any source that predates the snapshot or is forked mid-run. When
    # both exist they must agree -- a disagreement means the reconstruction
    # rule is wrong, and it is recorded rather than silently absorbed.
    snapshot = _read_pending_work_snapshot(source_run_root)
    snapshot_agrees: bool | None = None
    pending_work_source = "reconstructed_from_ledger"
    if snapshot is not None and int(snapshot.get("ordinal") or -1) == replayed_rows:
        snapshot_queues = {
            str(seat_id): [dict(message) for message in messages]
            for seat_id, messages in (snapshot.get("queues") or {}).items()
            if messages
        }
        snapshot_agrees = snapshot_queues == pending_work
        pending_work = snapshot_queues
        pending_work_source = "branch_snapshot"
    kernel.inbox = {seat_id: list(messages) for seat_id, messages in pending_work.items()}
    # Generated identifiers continue from where the source world left off, so
    # a branch cannot mint an id that already exists in the replayed history.
    kernel.event_counter = highest_id
    kernel.action_counter = highest_id

    # Carry the source world's own generation settings so a live
    # continuation runs the seats under the SAME conditions they had before
    # the fork. Without this the branch silently reverts to a pre-v4 world
    # (no contact directory, no identity tools, no worklist) and any
    # "nobody noticed" reading would be confounded by the downgrade.
    population = (source_config.get("world") or {}).get("population") or {}
    metadata = {
        "source_run_root": str(source_run_root),
        "source_ledger_sha256": source_ledger_sha256,
        "fork_tick": int(up_to_tick),
        "fork_ordinal": up_to_ordinal,
        "replayed_ledger_rows": replayed_rows,
        "application_count": len(kernel.applications),
        "stage": source_meta.get("stage") or "S2",
        "seed": source_meta.get("seed"),
        "workflow": dict(schedule.get("workflow") or {}),
        "tick_budget": dict(population.get("tick_budget") or {}),
        "model_binding": dict(population.get("binding") or {}),
        "prompt_mode": source_meta.get("prompt_mode") or "measurement",
        "source_ticks": source_horizon,
        # Customer machinery for the continuation: the full visit schedule is
        # persisted in every run's config, so a branch can host the SAME
        # customers, on the same days, with the same personas and seed.
        "deck_events": list(((source_config.get("world") or {}).get("deck") or {}).get("events") or []),
        "customer_model": str(((source_config.get("model") or {}).get("customer")) or ""),
        "absence": dict(population.get("absence") or {}),
        "pending_work": {
            "source": pending_work_source,
            "snapshot_agrees_with_ledger": snapshot_agrees,
            "item_count": sum(len(messages) for messages in pending_work.values()),
            "per_seat": {seat_id: len(messages) for seat_id, messages in sorted(pending_work.items())},
        },
    }
    return kernel, metadata


def _next_uncommitted_tick(recorder: RunRecorder) -> int:
    committed = {
        int(row.get("tick") or 0)
        for row in read_jsonl(recorder.run_root / "world_ledger.jsonl")
        if row.get("event_type") == "tick_committed"
    }
    tick = max(recorder.tick, 1)
    if tick not in committed:
        return tick
    while tick in committed:
        tick += 1
    return tick


def inject_branch_action(kernel: WorldKernel, action_spec: dict[str, Any]) -> dict[str, Any]:
    """Execute exactly one real kernel tool call under the experimenter-plane
    injection origin. `action_spec` is `{"tool": <kernel method name>,
    "args": {...}}`; the tool must be one of kernel.py's CONTROLLED_TOOLS --
    the same set every live seat is restricted to -- so injection can never
    reach an internal/private kernel method."""
    tool_name = str(action_spec.get("tool") or "")
    if tool_name not in CONTROLLED_TOOLS:
        raise BranchExecutionError(f"branch injection tool must be one of {sorted(CONTROLLED_TOOLS)}, got {tool_name!r}")
    method = getattr(kernel, tool_name, None)
    if method is None:
        raise BranchExecutionError(f"kernel has no tool method {tool_name!r}")
    recorder = kernel.recorder
    if not isinstance(recorder, BranchRunRecorder):
        raise BranchExecutionError("inject_branch_action requires a kernel built by rebuild_kernel_state (BranchRunRecorder)")
    args = dict(action_spec.get("args") or {})
    recorder.set_tick(_next_uncommitted_tick(recorder))
    with recorder.origin(INJECTION_ORIGIN):
        result = method(**args)
    return result if isinstance(result, dict) else {"value": result}


def _event_from_dict(raw: dict[str, Any]) -> CustomerEvent | None:
    """Rebuild a CustomerEvent from the dict shape config.json stores."""
    try:
        return CustomerEvent(
            event_id=str(raw["event_id"]),
            probe_id=str(raw.get("probe_id") or ""),
            customer_id=str(raw["customer_id"]),
            application_id=str(raw["application_id"]),
            product=str(raw.get("product") or ""),
            trigger_tick=int(raw.get("trigger_tick") or 0),
            deadline_tick=int(raw.get("deadline_tick") or 0),
            primary_seat=str(raw.get("primary_seat") or ""),
            participant_seats=tuple(raw.get("participant_seats") or ()),
            required_doc_ids=tuple(raw.get("required_doc_ids") or ()),
            span_ids=tuple(raw.get("span_ids") or ()),
            world_visible=str(raw.get("world_visible") or ""),
            latent_truth=str(raw.get("latent_truth") or ""),
            routine=bool(raw.get("routine")),
            customer_stage=str(raw.get("customer_stage") or "application_intent"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _run_live_continuation(
    kernel: WorldKernel,
    *,
    design: DesignInputs,
    corpus: Corpus,
    fork_tick: int,
    ticks: int,
    seat_factory: SeatFactory | None,
    model: str | None,
    prompt_mode: str,
    workflow: dict[str, Any] | None = None,
    budgets: dict[str, int] | None = None,
    model_binding: dict[str, str] | None = None,
    horizon: int | None = None,
    deck_events: list[dict[str, Any]] | None = None,
    customer_model: str | None = None,
    customer_llm: CustomerLLM | None = None,
    seed: int | None = None,
    absence: dict[str, list[int]] | None = None,
) -> int:
    """Full live-continuation plumbing, reachable only when a caller passes
    `allow_spend=True` to `run_branch_continuation`. Nothing in this change
    (CLI or tests) ever sets that flag -- live continuation is a separate,
    sealed-plan approval per option_branch_instrument_design_20260713.md §9 --
    so this function has no test coverage by design; it exists so a later
    change can wire the flag on without re-deriving this loop."""
    recorder = kernel.recorder
    active_seats = set(kernel.profile.seat_roles)
    seats_cache: dict[str, Any] = {}
    final_tick = fork_tick
    workflow = dict(workflow or {})
    budgets = dict(budgets or {})
    model_binding = dict(model_binding or {})
    absence = {seat_id: list(map(int, ticks_)) for seat_id, ticks_ in dict(absence or {}).items()}
    if budgets:
        recorder.configure_tick_budgets(budgets)
    # Same prompt blocks the source generation had; each collapses to an
    # empty string when its flag is off, so a pre-v4 source stays identical.
    contact_directory = (
        _contact_directory_text(dict(kernel.profile.seat_roles)) if workflow.get("contact_directory") else ""
    )
    ticks_horizon = int(horizon or (fork_tick + ticks))

    # ------------------------------------------------------------------
    # Customer machinery, same parts the ordinary harness uses: scheduled
    # visits still ahead of the fork happen on their scheduled day, contacted
    # customers answer on the next tick, and stalled customers send their own
    # follow-ups (when the source world had that layer on). Without this a
    # branch world starves once the restored pending work is handled.
    # ------------------------------------------------------------------
    events = [event for event in (
        _event_from_dict(raw) for raw in (deck_events or [])
    ) if event is not None]
    events_by_tick: dict[int, list[CustomerEvent]] = {}
    events_by_customer: dict[str, CustomerEvent] = {}
    for event in events:
        events_by_customer[event.customer_id] = event
        if event.trigger_tick > fork_tick:
            events_by_tick.setdefault(event.trigger_tick, []).append(event)
    customer_cache: dict[str, CustomerLLM] = {}

    def customer_model_llm() -> CustomerLLM:
        # Built lazily: a continuation with no customer activity never needs
        # (or pays for) a customer model at all.
        if "llm" not in customer_cache:
            customer_cache["llm"] = customer_llm or default_customer_llm(model=customer_model or model, recorder=recorder)
        return customer_cache["llm"]

    persona_seed = int(seed or 0)
    actors: dict[str, CustomerActor] = {}

    def actor_for(customer_id: str) -> CustomerActor | None:
        """Actor for a customer already in the world before the fork.

        Known approximation, recorded in the bundle: the pre-fork
        conversation memory is not restored, so such a customer answers from
        their persona and the staff message alone. Post-fork arrivals have
        full memory (their whole conversation happens in this branch).
        """
        if customer_id not in actors:
            event = events_by_customer.get(customer_id)
            if event is None:
                return None
            actors[customer_id] = CustomerActor(event, customer_model_llm(), persona_seed=persona_seed)
            recorder.append_ledger(
                "branch_customer_actor_reconstructed",
                {"customer_id": customer_id, "note": "pre-fork conversation memory not restored"},
            )
        return actors[customer_id]

    pending_replies: list[dict[str, str]] = []
    pending_reply_keys: set[tuple[int, str]] = set()

    def schedule_customer_reply(contact: dict[str, str]) -> None:
        customer_id = str(contact.get("customer_id") or "")
        key = (recorder.tick, customer_id)
        if key in pending_reply_keys:
            recorder.append_ledger(
                "customer_reply_suppressed_duplicate",
                {"customer_id": customer_id, "seat_id": contact.get("seat_id")},
            )
            return
        pending_reply_keys.add(key)
        pending_replies.append(dict(contact))

    kernel.on_customer_contact = schedule_customer_reply

    def seat_agent(seat_id: str):
        if seat_id not in seats_cache:
            seat = design.seats[seat_id]
            tools = build_role_tools(
                corpus=corpus,
                kernel=kernel,
                recorder=recorder,
                seat_id=seat_id,
                seat_role=seat.role,
                include_workflow=True,
                identity_tools=bool(workflow.get("identity_check_tool")),
                worklist_tool=bool(workflow.get("pending_worklist_tool")),
            )
            bound_model = model_binding.get(seat_id) or model or ""
            factory = seat_factory or default_seat_factory(root=design.root, model=bound_model)
            budget = int(budgets.get(seat_id, 12))
            seats_cache[seat_id] = _instantiate_seat(
                factory,
                seat_id=seat_id,
                role=seat.role,
                tools=tools,
                recorder=recorder,
                recursion_limit=recursion_for_budget(budget),
                model=bound_model,
            )
        return seats_cache[seat_id]

    for tick in range(fork_tick + 1, fork_tick + ticks + 1):
        recorder.set_tick(tick)
        kernel.fire_timed_events(tick)
        # Stalled-case follow-ups from the customers themselves (only fires
        # when the source world's consequence layer is on in the profile).
        for followup in kernel.take_customer_followups():
            follow_actor = actor_for(str(followup.get("customer_id") or ""))
            to_seat = str(followup.get("to_seat") or "")
            if follow_actor is None or to_seat not in active_seats:
                recorder.append_ledger("consequence_followup_skipped", {**followup, "reason": "no actor or inactive seat"})
                continue
            emit_customer_followup(
                kernel=kernel, recorder=recorder, actor=follow_actor, to_seat=to_seat,
                tick=tick, level=int(followup.get("level") or 1),
            )
        # Replies to yesterday's staff contacts.
        replies = list(pending_replies)
        pending_replies.clear()
        pending_reply_keys.clear()
        for contact in replies:
            reply_actor = actor_for(str(contact.get("customer_id") or ""))
            if reply_actor is None:
                continue
            emit_customer_reply(
                kernel=kernel, recorder=recorder, actor=reply_actor,
                to_seat=str(contact.get("seat_id") or ""), staff_message=str(contact.get("summary") or ""), tick=tick,
            )
        # Scheduled arrivals still ahead of the fork.
        for event in events_by_tick.get(tick, []):
            actors[event.customer_id] = emit_customer_turn(
                kernel=kernel, recorder=recorder, event=event, tick=tick,
                customer_llm=customer_model_llm(), persona_seed=persona_seed,
            )
        for seat_id in sorted(kernel.inbox_nonempty_seats()):
            if seat_id not in active_seats:
                continue
            if tick in absence.get(seat_id, []):
                continue  # absent seat keeps its queue until return, same as the ordinary harness
            messages = kernel.pop_inbox(seat_id)
            if not messages:
                continue
            agent = seat_agent(seat_id)
            prompt = _turn_prompt(
                tick=tick,
                ticks=ticks_horizon,
                budget_left=recorder.budget_left(seat_id),
                messages=messages,
                mode=prompt_mode,  # type: ignore[arg-type]
                contact_directory=contact_directory,
                customer_id_in_inbox=bool(workflow.get("customer_id_in_inbox")),
                sales_direct_submission=bool(workflow.get("sales_direct_submission_guidance")),
                identity_tools=bool(workflow.get("identity_check_tool")),
                progression_guidance=bool(workflow.get("progression_guidance")),
            )
            with recorder.origin("agent"):
                try:
                    response = agent.turn(prompt)
                except Exception as exc:  # noqa: BLE001 - recorded; continuation proceeds
                    recorder.append_ledger("agent_error", {"seat_id": seat_id, "error_type": type(exc).__name__, "message": str(exc)[:500]})
                    continue
            # Same row the ordinary harness writes. Without it a branch's own
            # record cannot show that these items were taken off the seat's
            # desk, and a fork from this bundle would count them as still
            # outstanding.
            recorder.append_ledger("agent_response", {"seat_id": seat_id, "response": response[:2000], "message_count": len(messages)})
        recorder.append_ledger("tick_committed", {"tick": tick})
        final_tick = tick
    return final_tick


def _write_pending_work_snapshot(kernel: WorldKernel) -> dict[str, Any]:
    """Record what each seat still has waiting, keyed to this bundle's own
    ledger length, so a fork taken from this bundle resumes from the exact
    state instead of re-deriving it."""
    recorder = kernel.recorder
    ordinal = len(read_jsonl(recorder.run_root / "world_ledger.jsonl"))
    queues = {
        seat_id: [dict(message) for message in messages]
        for seat_id, messages in sorted(kernel.inbox.items())
        if messages
    }
    snapshot = {
        "ordinal": ordinal,
        "tick": int(recorder.tick),
        "queues": queues,
        "item_count": sum(len(messages) for messages in queues.values()),
    }
    recorder.write_json(PENDING_WORK_FILE, snapshot)
    return snapshot


def _finalize_bundle(
    recorder: RunRecorder,
    *,
    stage: str,
    seat_roles: dict[str, str],
    final_tick: int,
    injected_action: dict[str, Any] | None,
    allow_spend: bool,
    source_horizon: int = 0,
    metadata: dict[str, Any] | None = None,
) -> None:
    schedule: dict[str, Any] = {"ticks": final_tick}
    population: dict[str, Any] = {
        "seats": {seat_id: {"role": role} for seat_id, role in sorted(seat_roles.items())}
    }
    config: dict[str, Any] = {
        "schema_version": WORLD_CONFIG_SCHEMA_VERSION,
        "stage": stage,
        "world": {"schedule": schedule, "population": population},
    }
    if metadata is not None:
        # Persist the continuation conditions in the SAME shape
        # rebuild_kernel_state reads them from an original world's config, so
        # a deeper fork taken from this bundle runs its seats and customers
        # under the same conditions instead of silently reverting to a
        # pre-v4 world (the downgrade the rebuild comment warns about).
        schedule["workflow"] = dict(metadata.get("workflow") or {})
        population["binding"] = dict(metadata.get("model_binding") or {})
        population["tick_budget"] = dict(metadata.get("tick_budget") or {})
        population["absence"] = dict(metadata.get("absence") or {})
        config["world"]["deck"] = {"events": list(metadata.get("deck_events") or [])}
        config["model"] = {"customer": str(metadata.get("customer_model") or "")}
    recorder.write_json("config.json", config)
    meta_path = recorder.run_root / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta.update(
        {
            "stage": stage,
            "final_tick": final_tick,
            "live": False,
            "allow_spend": bool(allow_spend),
            "source_horizon": int(source_horizon),
        }
    )
    if metadata is not None:
        meta["prompt_mode"] = str(metadata.get("prompt_mode") or "measurement")
    if injected_action is not None:
        meta["injected_action"] = injected_action
    recorder.write_json("meta.json", meta)


def run_branch_continuation(
    kernel: WorldKernel,
    *,
    metadata: dict[str, Any],
    design: DesignInputs | None = None,
    corpus: Corpus | None = None,
    ticks: int = 0,
    allow_spend: bool = False,
    seat_factory: SeatFactory | None = None,
    model: str | None = None,
    prompt_mode: str = "measurement",
    injected_action: dict[str, Any] | None = None,
    customer_llm: CustomerLLM | None = None,
) -> dict[str, Any]:
    """Finalize a branch bundle. With `allow_spend=False` (the only path any
    caller in this change ever takes -- no CLI flag exists to set it True)
    this stops immediately after injection: it commits the tick the
    injection landed on and writes `config.json`/`meta.json` so the bundle is
    a complete, self-contained artifact for the detection hook. The live
    path (real seats continuing the world for `ticks` more ticks) hard-
    requires `allow_spend=True`; see `_run_live_continuation`."""
    recorder = kernel.recorder
    if not isinstance(recorder, BranchRunRecorder):
        raise BranchExecutionError("run_branch_continuation requires a kernel built by rebuild_kernel_state (BranchRunRecorder)")
    fork_tick = int(metadata.get("fork_tick") if metadata.get("fork_tick") is not None else recorder.tick)

    if allow_spend:
        if design is None or corpus is None:
            raise BranchExecutionError("allow_spend=True requires design and corpus for live seat tool-building")
        if ticks <= 0:
            raise BranchExecutionError("allow_spend=True requires ticks > 0")
        # Never run past the source world's own end: pass a large `ticks` to
        # mean "observe until the world's horizon". The cap that bit is
        # visible in the summary (requested vs executed).
        source_ticks = int(metadata.get("source_ticks") or 0)
        if source_ticks > 0:
            ticks = max(min(ticks, source_ticks - fork_tick), 0)
        if ticks <= 0:
            raise BranchExecutionError(
                f"no observation window left: fork_tick={fork_tick} is at or past the source horizon {source_ticks}"
            )
        final_tick = _run_live_continuation(
            kernel,
            design=design,
            corpus=corpus,
            fork_tick=fork_tick,
            ticks=ticks,
            seat_factory=seat_factory,
            model=model,
            prompt_mode=str(metadata.get("prompt_mode") or prompt_mode),
            workflow=metadata.get("workflow"),
            budgets=metadata.get("tick_budget"),
            model_binding=metadata.get("model_binding"),
            horizon=int(metadata.get("source_ticks") or 0) or None,
            deck_events=metadata.get("deck_events"),
            customer_model=str(metadata.get("customer_model") or "") or None,
            customer_llm=customer_llm,
            seed=metadata.get("seed"),
            absence=metadata.get("absence"),
        )
    else:
        final_tick = _next_uncommitted_tick(recorder)
        recorder.set_tick(final_tick)
        recorder.append_ledger("tick_committed", {"tick": final_tick})

    pending_work = _write_pending_work_snapshot(kernel)
    _finalize_bundle(
        recorder,
        stage=str(metadata.get("stage") or "S2"),
        seat_roles=dict(kernel.profile.seat_roles),
        final_tick=final_tick,
        injected_action=injected_action,
        allow_spend=allow_spend,
        source_horizon=int(metadata.get("source_ticks") or 0),
        metadata=metadata,
    )
    summary = {
        "run_root": str(recorder.run_root),
        "fork_tick": fork_tick,
        "final_tick": final_tick,
        "continuation_ticks_executed": max(final_tick - fork_tick, 0),
        "allow_spend": bool(allow_spend),
        "run_class": BRANCH_RUN_CLASS,
        "claim_level": BRANCH_CLAIM_LEVEL,
        "pending_work_restored": dict(metadata.get("pending_work") or {}),
        "pending_work_remaining": pending_work["item_count"],
    }
    recorder.write_json("branch_summary.json", summary)
    return summary


def run_branch_detection(output_run_root: Path, *, rules_root: Path | None = None) -> dict[str, Any]:
    """Run the UNCHANGED loss-event oracle (and, where its preconditions
    allow, the UNCHANGED monitoring join) against a finalized branch bundle.
    Nothing here is a bespoke detector: this instrument's entire point is
    that the injected act is measured by the same machinery that scores
    natural runs."""
    output_run_root = Path(output_run_root).resolve()
    loss_report = loss_event_findings(output_run_root)
    result: dict[str, Any] = {"loss_events": loss_report, "monitoring": None, "monitoring_error": None}
    try:
        result["monitoring"] = write_loss_event_monitoring(output_run_root, rules_root=rules_root)
    except (FileNotFoundError, ValueError) as exc:
        result["monitoring_error"] = str(exc)
    return result
