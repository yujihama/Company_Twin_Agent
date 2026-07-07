from __future__ import annotations

import json
from pathlib import Path

import typer.main

from company_twin.acceptance import a03_inbox_whitelist
from company_twin.agents import recursion_for_budget
from company_twin.campaign import WORLD_PROMPT_BANNED_TERMS
from company_twin.cli import app
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.harness import run_s1_episode
from company_twin.recorder import read_jsonl
from company_twin.world_config import build_world_config
from conftest import fake_seat_factory
from test_world_runs import _LateBoundCustomer


def _design():
    return load_design(Path.cwd())


def test_time_pressure_defaults_off_preserves_ordinary_schedule() -> None:
    design = _design()
    config = build_world_config(design, stage="S2", model=None, seed=0, ticks=40)
    schedule = config["world"]["schedule"]

    assert schedule["time_pressure"]["enabled"] is False
    assert schedule["campaign_deadline_tick"] == 20
    assert schedule["approval_due_ticks"] == 2
    assert schedule["month_end_tick"] == 40
    assert schedule["scc_switch_tick"] == 30
    assert config["runtime_delta"]["time_pressure"] is False

    seats = config["world"]["population"]["seats"]
    assert seats["emp-A"]["tick_budget"] == 14
    assert "ordinary_tick_budget" not in seats["emp-A"]


def test_time_pressure_compresses_deck_schedule_and_budgets_without_dropping_events() -> None:
    design = _design()
    ordinary = build_world_config(design, stage="S2", model=None, seed=0, ticks=40)
    pressured = build_world_config(design, stage="S2", model=None, seed=0, ticks=40, time_pressure=True)

    schedule = pressured["world"]["schedule"]
    pressure = schedule["time_pressure"]
    assert pressure["enabled"] is True
    assert pressure["mode"] == "compressed_horizon_v1"
    assert pressure["compressed_horizon_tick"] == 27
    assert schedule["campaign_deadline_tick"] == 13
    notices = pressure["notices"]
    ticks_by_notice = {row["notice"]: row["tick"] for row in notices}
    assert ticks_by_notice["workload_pressure_start"] <= ticks_by_notice["workload_pressure_midpoint"]
    assert ticks_by_notice["workload_pressure_midpoint"] <= ticks_by_notice["workload_pressure_deadline"]
    assert ticks_by_notice["workload_pressure_deadline"] == schedule["campaign_deadline_tick"]
    assert schedule["approval_due_ticks"] == 1
    assert schedule["month_end_tick"] == 27
    assert schedule["scc_switch_tick"] == 20
    assert config_event_count(pressured) == config_event_count(ordinary)

    pressured_events = pressured["world"]["deck"]["events"]
    assert max(event["trigger_tick"] for event in pressured_events) <= 27
    ordinary_p04 = next(event for event in ordinary["world"]["deck"]["events"] if event["probe_id"] == "P-04")
    pressured_p04 = next(event for event in pressured_events if event["probe_id"] == "P-04")
    assert pressured_p04["trigger_tick"] < ordinary_p04["trigger_tick"]
    assert pressured_p04["trigger_tick"] in schedule["manager_absence_ticks"]

    seats = pressured["world"]["population"]["seats"]
    assert seats["emp-A"]["ordinary_tick_budget"] == 14
    assert seats["emp-A"]["tick_budget"] == 9
    assert pressured["world"]["population"]["tick_budget"]["emp-A"] == 9
    assert pressured["runtime_delta"]["time_pressure"] is True


def config_event_count(config: dict) -> int:
    return len(config["world"]["deck"]["events"])


def test_time_pressure_delivers_natural_timed_notices_and_keeps_inbox_clean(tmp_path: Path) -> None:
    design = _design()
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s1_time_pressure"

    run_s1_episode(
        design=design,
        corpus=corpus,
        probe_id="P-04",
        run_root=run_root,
        seed=0,
        ticks=6,
        seat_factory=fake_seat_factory(),
        customer_llm=_LateBoundCustomer(run_root),
        time_pressure=True,
    )

    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    assert config["world"]["schedule"]["time_pressure"]["enabled"] is True

    ledger = read_jsonl(run_root / "world_ledger.jsonl")
    assert any(row["event_type"] == "time_pressure_notice" for row in ledger)
    delivered = []
    for row in ledger:
        if row.get("event_type") != "inbox_delivered":
            continue
        message = (row.get("payload") or {}).get("message") or {}
        if str(message.get("notice") or "").startswith("workload_pressure"):
            delivered.append(message)
    assert delivered, "D1 workload notices must be delivered through the ordinary inbox path"
    for message in delivered:
        detail = str(message["detail"])
        lowered = detail.lower()
        for term in WORLD_PROMPT_BANNED_TERMS:
            assert term.lower() not in lowered

    assert a03_inbox_whitelist(run_root).passed


def test_time_pressure_keeps_recursion_headroom_while_compressing_tool_budget(tmp_path: Path) -> None:
    design = _design()
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s1_time_pressure_recursion"
    captured_limits: dict[str, int] = {}
    base_factory = fake_seat_factory()

    def capturing_factory(*, seat_id: str, role: str, tools: list, recorder, recursion_limit: int, model: str = "fake:unit"):
        captured_limits[seat_id] = recursion_limit
        return base_factory(seat_id=seat_id, role=role, tools=tools, recorder=recorder, recursion_limit=recursion_limit, model=model)

    run_s1_episode(
        design=design,
        corpus=corpus,
        probe_id="P-04",
        run_root=run_root,
        seed=0,
        ticks=2,
        seat_factory=capturing_factory,
        customer_llm=_LateBoundCustomer(run_root),
        time_pressure=True,
    )

    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    assert config["world"]["population"]["tick_budget"]["emp-A"] == 9
    assert captured_limits["emp-A"] == recursion_for_budget(14)


def test_cli_exposes_time_pressure_on_world_run_commands() -> None:
    click_group = typer.main.get_command(app)
    for command in ("s1", "s2", "campaign", "control-pair-campaign"):
        names = set()
        for param in click_group.commands[command].params:
            names.update(param.opts)
            names.update(getattr(param, "secondary_opts", []) or [])
        assert "--time-pressure" in names, command
        assert "--no-time-pressure" in names, command
