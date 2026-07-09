from __future__ import annotations

import json
from pathlib import Path

from company_twin.acceptance import a03_inbox_whitelist
from company_twin.campaign import WORLD_PROMPT_BANNED_TERMS
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.harness import kernel_profile, run_s1_episode
from company_twin.kernel import WorldKernel
from company_twin.recorder import RunRecorder, read_jsonl
from company_twin.world_config import build_world_config
from conftest import fake_seat_factory
from test_world_runs import _LateBoundCustomer


def _design():
    return load_design(Path.cwd())


def test_consequences_default_off_keeps_ordinary_schedule() -> None:
    design = _design()
    config = build_world_config(design, stage="S2", model=None, seed=0, ticks=40)
    schedule = config["world"]["schedule"]["consequences"]
    assert schedule["enabled"] is False
    assert schedule["mode"] == "off"
    assert config["runtime_delta"]["consequences"] == "off"


def test_consequences_mode_is_validated() -> None:
    design = _design()
    try:
        build_world_config(design, stage="S2", model=None, seed=0, ticks=40, consequences="bogus")
    except ValueError as exc:
        assert "bogus" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for unknown consequences mode")


def _kernel(tmp_path: Path, *, mode: str, deadline_tick: int = 20) -> WorldKernel:
    design = _design()
    recorder = RunRecorder(tmp_path / f"kernel_{mode}", run_id="unit", meta={})
    profile = kernel_profile(
        design,
        schedule={
            "campaign_deadline_tick": deadline_tick,
            "timed_notice_recipients": ["emp-A", "emp-M"],
            "approval_notice_recipients": ["emp-M"],
            "consequences": {"mode": mode, "stall_after_ticks": 3},
        },
        valid_doc_ids=set(),
    )
    return WorldKernel(recorder, profile)


def _notices(recorder: RunRecorder, notice: str) -> list[dict]:
    rows = read_jsonl(recorder.run_root / "world_ledger.jsonl")
    hits = []
    for row in rows:
        if row["event_type"] != "inbox_delivered":
            continue
        message = (row["payload"] or {}).get("message") or {}
        if message.get("notice") == notice:
            hits.append(message)
    return hits


def test_delay_mode_escalates_stalled_case_and_lists_it_on_deadline(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, mode="delay", deadline_tick=8)
    kernel.recorder.set_tick(1)
    kernel.record_customer_event({"event_id": "EVT-P-04", "customer_id": "CUS-P-04", "application_id": "APP-P-04", "product": "投資", "primary_seat": "emp-A"})

    for tick in range(2, 9):
        kernel.recorder.set_tick(tick)
        kernel.fire_timed_events(tick)

    ledger = read_jsonl(kernel.recorder.run_root / "world_ledger.jsonl")
    due = [row["payload"] for row in ledger if row["event_type"] == "consequence_followup_due"]
    assert [d["level"] for d in due] == [1, 2]
    assert all(d["application_id"] == "APP-P-04" for d in due)

    stalled_notices = _notices(kernel.recorder, "stalled_case_review")
    assert stalled_notices, "level-2 stall must deliver a stalled_case_review notice"
    unresolved = _notices(kernel.recorder, "unresolved_cases_review")
    assert unresolved and "APP-P-04" in unresolved[0]["detail"]

    banned = [term for term in WORLD_PROMPT_BANNED_TERMS for msg in (*stalled_notices, *unresolved) if term.lower() in str(msg["detail"]).lower()]
    assert not banned, f"world-visible notice leaked banned terms: {banned}"


def test_delay_mode_touch_resets_stall_clock(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, mode="delay")
    kernel.recorder.set_tick(1)
    kernel.record_customer_event({"event_id": "EVT-P-04", "customer_id": "CUS-P-04", "application_id": "APP-P-04", "product": "投資", "primary_seat": "emp-A"})
    kernel.recorder.set_tick(3)
    kernel.defer_or_hold("emp-A", "APP-P-04", "確認待ち", "管理者確認")
    for tick in range(4, 6):
        kernel.recorder.set_tick(tick)
        kernel.fire_timed_events(tick)
    ledger = read_jsonl(kernel.recorder.run_root / "world_ledger.jsonl")
    assert not [row for row in ledger if row["event_type"] == "consequence_followup_due"]


def test_speed_mode_notices_progressed_case_once(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, mode="speed")
    kernel.recorder.set_tick(1)
    kernel.record_customer_event({"event_id": "EVT-R01", "customer_id": "CUS-R01", "application_id": "APP-R01", "product": "投資", "primary_seat": "emp-A"})
    basis = {
        "retrieved": [{"doc_id": "DFH-SAL-021@v1.0", "version": "1.0", "citation_handle": "read:DFH-SAL-021@v1.0:v1.0"}],
        "construal": "読み",
        "decision": "受付",
        "evidence_plan": "記録",
        "confidence": 0.6,
    }
    kernel.recorder.record_attempt(
        seat_id="emp-B",
        tool="read_document",
        args={"doc_id": "DFH-SAL-021@v1.0"},
        success=True,
        result={"version": "1.0", "citation_handle": "read:DFH-SAL-021@v1.0:v1.0", "text": "受付要領"},
    )
    result = kernel.submit_application("emp-B", "APP-R01", "CUS-R01", "投資", {"material_version": "v1.0"}, basis)
    assert result.get("status"), result
    for tick in range(2, 5):
        kernel.recorder.set_tick(tick)
        kernel.fire_timed_events(tick)
    notices = _notices(kernel.recorder, "evidence_check_review")
    # fired exactly once (a single tick), delivered to the primary seat and
    # the approval-notice recipient -- one inbox message per recipient
    assert {message["tick"] for message in notices} == {2}
    assert len(notices) == 2
    assert all("APP-R01" in message["detail"] for message in notices)


def test_delay_mode_delivers_customer_followup_through_harness(tmp_path: Path) -> None:
    design = _design()
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s1_consequences"
    run_s1_episode(
        design=design,
        corpus=corpus,
        probe_id="P-04",
        run_root=run_root,
        seed=0,
        ticks=8,
        seat_factory=fake_seat_factory(),
        customer_llm=_LateBoundCustomer(run_root),
        consequences="delay",
    )
    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    assert config["world"]["schedule"]["consequences"]["enabled"] is True
    assert config["runtime_delta"]["consequences"] == "delay"
    assert a03_inbox_whitelist(run_root).passed
    meta = json.loads((run_root / "meta.json").read_text(encoding="utf-8"))
    assert meta["consequences"] == "delay"
