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
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

from .agents import SeatFactory, default_seat_factory, recursion_for_budget
from .corpus import Corpus
from .design_loader import DesignInputs, load_design
from .harness import _turn_prompt, kernel_profile as _harness_kernel_profile
from .kernel import CONTROLLED_TOOLS, WorldKernel
from .loss_monitoring import WORLD_CONFIG_SCHEMA_VERSION, write_loss_event_monitoring
from .loss_oracle import loss_event_findings
from .recorder import ALLOWED_ORIGINS, RunRecorder, read_jsonl
from .tools import build_role_tools

BRANCH_RUN_CLASS = "branch_injection"
BRANCH_CLAIM_LEVEL = "detection_coverage_probe"
INJECTION_ORIGIN = "experimenter_injection"

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


def rebuild_kernel_state(
    source_run_root: Path,
    up_to_tick: int,
    output_run_root: Path,
    *,
    design_root: Path | None = None,
) -> tuple[WorldKernel, dict[str, Any]]:
    """Validate the source ledger's hash chain, then deterministically
    reconstruct `WorldKernel.applications` by replaying its lifecycle events
    up to `up_to_tick`, attaching a NEW `BranchRunRecorder` rooted at
    `output_run_root`."""
    source_run_root = Path(source_run_root).resolve()
    output_run_root = Path(output_run_root).resolve()
    ledger_path = source_run_root / "world_ledger.jsonl"
    source_ledger = read_jsonl(ledger_path)
    if not source_ledger:
        raise BranchExecutionError(f"source world ledger is empty: {ledger_path}")
    _validate_source_ledger_chain(source_ledger)  # fail closed BEFORE any reconstruction
    source_ledger_sha256 = hashlib.sha256(ledger_path.read_bytes()).hexdigest()

    meta_path = source_run_root / "meta.json"
    source_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    config_path = source_run_root / "config.json"
    source_config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}

    design = load_design(Path(design_root) if design_root is not None else Path.cwd())
    schedule = ((source_config.get("world") or {}).get("schedule") or {})
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
            "stage": source_meta.get("stage"),
            "seed": source_meta.get("seed"),
        },
    )
    kernel = WorldKernel(recorder, profile)

    replayed_rows = 0
    for row in source_ledger:
        tick = int(row.get("tick") or 0)
        if tick > int(up_to_tick):
            continue
        event_type = str(row.get("event_type") or "")
        payload = dict(row.get("payload") or {})
        recorder.set_tick(tick)
        recorder.append_ledger(event_type, payload)
        _apply_ledger_event(kernel.applications, event_type=event_type, tick=tick, payload=payload)
        replayed_rows += 1
    recorder.set_tick(int(up_to_tick))

    metadata = {
        "source_run_root": str(source_run_root),
        "source_ledger_sha256": source_ledger_sha256,
        "fork_tick": int(up_to_tick),
        "replayed_ledger_rows": replayed_rows,
        "application_count": len(kernel.applications),
        "stage": source_meta.get("stage") or "S2",
        "seed": source_meta.get("seed"),
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

    def seat_agent(seat_id: str):
        if seat_id not in seats_cache:
            seat = design.seats[seat_id]
            tools = build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id=seat_id, seat_role=seat.role, include_workflow=True)
            factory = seat_factory or default_seat_factory(root=design.root, model=model or "")
            seats_cache[seat_id] = factory(seat_id=seat_id, role=seat.role, tools=tools, recorder=recorder, recursion_limit=recursion_for_budget(12))
        return seats_cache[seat_id]

    for tick in range(fork_tick + 1, fork_tick + ticks + 1):
        recorder.set_tick(tick)
        kernel.fire_timed_events(tick)
        for seat_id in sorted(kernel.inbox_nonempty_seats()):
            if seat_id not in active_seats:
                continue
            messages = kernel.pop_inbox(seat_id)
            if not messages:
                continue
            agent = seat_agent(seat_id)
            prompt = _turn_prompt(tick=tick, ticks=fork_tick + ticks, budget_left=recorder.budget_left(seat_id), messages=messages, mode=prompt_mode)
            with recorder.origin("agent"):
                try:
                    agent.turn(prompt)
                except Exception as exc:  # noqa: BLE001 - recorded; continuation proceeds
                    recorder.append_ledger("agent_error", {"seat_id": seat_id, "error_type": type(exc).__name__, "message": str(exc)[:500]})
        recorder.append_ledger("tick_committed", {"tick": tick})
        final_tick = tick
    return final_tick


def _finalize_bundle(
    recorder: RunRecorder,
    *,
    stage: str,
    seat_roles: dict[str, str],
    final_tick: int,
    injected_action: dict[str, Any] | None,
    allow_spend: bool,
) -> None:
    config = {
        "schema_version": WORLD_CONFIG_SCHEMA_VERSION,
        "stage": stage,
        "world": {
            "schedule": {"ticks": final_tick},
            "population": {"seats": {seat_id: {"role": role} for seat_id, role in sorted(seat_roles.items())}},
        },
    }
    recorder.write_json("config.json", config)
    meta_path = recorder.run_root / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta.update(
        {
            "stage": stage,
            "final_tick": final_tick,
            "live": False,
            "allow_spend": bool(allow_spend),
        }
    )
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
        final_tick = _run_live_continuation(
            kernel,
            design=design,
            corpus=corpus,
            fork_tick=fork_tick,
            ticks=ticks,
            seat_factory=seat_factory,
            model=model,
            prompt_mode=prompt_mode,
        )
    else:
        final_tick = _next_uncommitted_tick(recorder)
        recorder.set_tick(final_tick)
        recorder.append_ledger("tick_committed", {"tick": final_tick})

    _finalize_bundle(
        recorder,
        stage=str(metadata.get("stage") or "S2"),
        seat_roles=dict(kernel.profile.seat_roles),
        final_tick=final_tick,
        injected_action=injected_action,
        allow_spend=allow_spend,
    )
    summary = {
        "run_root": str(recorder.run_root),
        "fork_tick": fork_tick,
        "final_tick": final_tick,
        "continuation_ticks_executed": max(final_tick - fork_tick, 0),
        "allow_spend": bool(allow_spend),
        "run_class": BRANCH_RUN_CLASS,
        "claim_level": BRANCH_CLAIM_LEVEL,
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
