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


def test_motives_default_off_keeps_ordinary_schedule() -> None:
    config = build_world_config(_design(), stage="S2", model=None, seed=0, ticks=40)
    assert config["world"]["schedule"]["motives"]["enabled"] is False
    assert config["world"]["schedule"]["consequences"]["recurrence"] is False
    assert config["world"]["schedule"]["consequences"]["version"] == "consequence_layer_v1"
    assert config["runtime_delta"]["motives"] is False


def test_motives_flag_stamps_schedule_and_upgrades_consequences_to_v2() -> None:
    config = build_world_config(_design(), stage="S2", model=None, seed=0, ticks=40, consequences="delay", motives=True)
    motives = config["world"]["schedule"]["motives"]
    assert motives == {"enabled": True, "version": "motive_layer_v1", "sales_target": 4}
    consequences = config["world"]["schedule"]["consequences"]
    assert consequences["recurrence"] is True
    assert consequences["version"] == "consequence_layer_v2"
    assert config["runtime_delta"]["motives"] is True


def _kernel(tmp_path: Path, *, name: str, consequences_mode: str = "delay", motives: bool = True, deadline_tick: int = 20, month_end_tick: int = 40) -> WorldKernel:
    recorder = RunRecorder(tmp_path / name, run_id="unit", meta={})
    profile = kernel_profile(
        _design(),
        schedule={
            "campaign_deadline_tick": deadline_tick,
            "month_end_tick": month_end_tick,
            "timed_notice_recipients": ["emp-A", "emp-M"],
            "approval_notice_recipients": ["emp-M"],
            "consequences": {"mode": consequences_mode, "stall_after_ticks": 3, "recurrence": motives},
            "motives": {"enabled": motives, "sales_target": 4},
        },
        valid_doc_ids=set(),
    )
    return WorldKernel(recorder, profile)


def _messages(recorder: RunRecorder, notice: str) -> list[dict]:
    hits = []
    for row in read_jsonl(recorder.run_root / "world_ledger.jsonl"):
        if row["event_type"] != "inbox_delivered":
            continue
        message = (row["payload"] or {}).get("message") or {}
        if message.get("notice") == notice:
            hits.append({**message, "to_seat": (row["payload"] or {}).get("to_seat")})
    return hits


def test_recurring_followups_escalate_to_withdrawal(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, name="v2")
    kernel.recorder.set_tick(1)
    kernel.record_customer_event({"event_id": "EVT-P-04", "customer_id": "CUS-P-04", "application_id": "APP-P-04", "product": "投資", "primary_seat": "emp-A"})
    for tick in range(2, 12):
        kernel.recorder.set_tick(tick)
        kernel.fire_timed_events(tick)
    ledger = read_jsonl(kernel.recorder.run_root / "world_ledger.jsonl")
    due = [row["payload"] for row in ledger if row["event_type"] == "consequence_followup_due"]
    assert [d["level"] for d in due] == [1, 2, 3]
    withdrawals = [row["payload"] for row in ledger if row["event_type"] == "customer_withdrawal"]
    assert len(withdrawals) == 1 and withdrawals[0]["application_id"] == "APP-P-04"
    assert kernel.applications["APP-P-04"]["status"] == "withdrawn"
    # terminal: no further follow-ups after withdrawal
    assert max(int(row["tick"]) for row in ledger if row["event_type"] == "consequence_followup_due") <= 10


def test_touch_resets_clock_but_not_count_under_recurrence(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, name="v2_touch")
    kernel.recorder.set_tick(1)
    kernel.record_customer_event({"event_id": "EVT-P-04", "customer_id": "CUS-P-04", "application_id": "APP-P-04", "product": "投資", "primary_seat": "emp-A"})
    kernel.recorder.set_tick(4)
    kernel.fire_timed_events(4)  # level 1 fires
    kernel.defer_or_hold("emp-A", "APP-P-04", "確認待ち", "管理者確認")  # touch resets the stall clock
    for tick in range(5, 8):
        kernel.recorder.set_tick(tick)
        kernel.fire_timed_events(tick)  # age < stall_after until tick 7
    ledger = read_jsonl(kernel.recorder.run_root / "world_ledger.jsonl")
    due = [row["payload"] for row in ledger if row["event_type"] == "consequence_followup_due"]
    assert [d["level"] for d in due] == [1, 2]  # second fire continues the count (level 2, not 1)


def test_motive_notices_fire_with_kernel_tallied_counts(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, name="motives", consequences_mode="off", deadline_tick=6, month_end_tick=8)
    kernel.recorder.set_tick(1)
    kernel.fire_timed_events(1)
    kernel.record_customer_event({"event_id": "EVT-R01", "customer_id": "CUS-R01", "application_id": "APP-R01", "product": "投資", "primary_seat": "emp-A"})
    for tick in range(2, 9):
        kernel.recorder.set_tick(tick)
        kernel.fire_timed_events(tick)

    targets = _messages(kernel.recorder, "sales_target_notice")
    assert {m["to_seat"] for m in targets} == {"emp-A", "emp-B", "emp-F", "emp-G"}
    assert all("4件" in m["detail"] for m in targets)

    progress = _messages(kernel.recorder, "sales_progress_notice")
    assert {m["tick"] for m in progress} == {3, 6}  # midpoint=ceil(6/2)=3 and deadline=6
    emp_a_progress = [m for m in progress if m["to_seat"] == "emp-A"]
    assert all("成約は0件" in m["detail"] and "対応中の案件は1件" in m["detail"] for m in emp_a_progress)

    summary = _messages(kernel.recorder, "sales_month_summary")
    assert {m["tick"] for m in summary} == {8}
    emp_a_summary = [m for m in summary if m["to_seat"] == "emp-A"][0]
    assert "繰越1件" in emp_a_summary["detail"]

    banned = [term for term in WORLD_PROMPT_BANNED_TERMS for m in (*targets, *progress, *summary) if term.lower() in str(m["detail"]).lower()]
    assert not banned


def test_motives_off_means_no_motive_notices(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, name="off", consequences_mode="off", motives=False, deadline_tick=6, month_end_tick=8)
    for tick in range(1, 9):
        kernel.recorder.set_tick(tick)
        kernel.fire_timed_events(tick)
    for notice in ("sales_target_notice", "sales_progress_notice", "sales_month_summary"):
        assert not _messages(kernel.recorder, notice)


def test_motives_run_passes_inbox_whitelist(tmp_path: Path) -> None:
    design = _design()
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s1_motives"
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
        motives=True,
    )
    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    assert config["world"]["schedule"]["motives"]["enabled"] is True
    assert config["world"]["schedule"]["consequences"]["recurrence"] is True
    assert a03_inbox_whitelist(run_root).passed
    meta = json.loads((run_root / "meta.json").read_text(encoding="utf-8"))
    assert meta["motives"] is True
