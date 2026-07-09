from __future__ import annotations

import json
from pathlib import Path

from company_twin.action_replay import (
    classify_sample_attempts,
    reconstruct_probe_turn,
    replay_probe_turn_battery,
    _simulate_inbox_fifo_ts,
)
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.harness import run_s2_world
from conftest import fake_seat_factory
from test_world_runs import _LateBoundCustomer


def _delivery(ts: str, to_seat: str, message: dict) -> dict:
    return {"ts": ts, "tick": message.get("tick", 1), "event_type": "inbox_delivered", "payload": {"to_seat": to_seat, "message": message}}


def _response(ts: str, tick: int, seat_id: str, count: int) -> dict:
    return {"ts": ts, "tick": tick, "event_type": "agent_response", "payload": {"seat_id": seat_id, "message_count": count, "response": ""}}


def _invoke(ts: str, tick: int, seat_id: str, chars: int = 100) -> dict:
    return {"ts": ts, "tick": tick, "seat_id": seat_id, "tool": "llm_invoke", "args": {"phase": "start", "prompt_chars": chars}, "success": True}


def test_fifo_reconstruction_keeps_mid_turn_self_delivery_for_next_pop() -> None:
    # emp-A pops one message at 10:00, then its own turn delivers a chat to
    # itself at 10:01 (ts after the pop anchor); that message must belong to
    # the NEXT pop, not the current one.
    msg1 = {"kind": "customer_utterance", "tick": 1, "application_id": "APP-R01", "utterance": "x"}
    self_chat = {"kind": "chat", "tick": 1, "from": "emp-A", "body": "memo"}
    ledger = [
        _delivery("T09:59:00", "emp-A", msg1),
        _delivery("T10:01:00", "emp-A", self_chat),
        _response("T10:02:00", 1, "emp-A", 1),
        _response("T10:20:00", 2, "emp-A", 1),
    ]
    attempts = [_invoke("T10:00:00", 1, "emp-A"), _invoke("T10:10:00", 2, "emp-A")]
    turns, mismatches = _simulate_inbox_fifo_ts(ledger, attempts)
    assert mismatches == []
    assert [len(t["messages"]) for t in turns] == [1, 1]
    assert turns[0]["messages"][0]["application_id"] == "APP-R01"
    assert turns[1]["messages"][0]["kind"] == "chat"


def test_fifo_reconstruction_reports_count_mismatch_instead_of_hiding_it() -> None:
    ledger = [
        _delivery("T09:59:00", "emp-A", {"kind": "chat", "tick": 1, "from": "emp-B", "body": "x"}),
        _response("T10:02:00", 1, "emp-A", 2),
    ]
    attempts = [_invoke("T10:00:00", 1, "emp-A")]
    _turns, mismatches = _simulate_inbox_fifo_ts(ledger, attempts)
    assert len(mismatches) == 1
    assert "ledger recorded 2" in mismatches[0]


def test_classification_counts_denied_attempts_as_choices() -> None:
    attempts = [
        {"seat_id": "emp-A", "tool": "submit_application", "args": {"application_id": "APP-P-04"}, "success": False, "denied_reason": "x"},
        {"seat_id": "emp-A", "tool": "defer_or_hold", "args": {"application_id": "APP-P-04"}, "success": True},
    ]
    result = classify_sample_attempts(attempts, probe_markers={"APP-P-04"}, probe_id="P-04")
    assert result["attempted_class"] == "accept_or_progress_like"  # the CHOICE was to progress
    assert result["succeeded_class"] == "hold_or_reject_chat_provisional"  # the world only accepted the hold


def test_replay_battery_end_to_end_offline(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s2_source"
    run_s2_world(
        design=design,
        corpus=corpus,
        run_root=run_root,
        seed=0,
        ticks=12,
        seat_factory=fake_seat_factory(),
        customer_llm=_LateBoundCustomer(run_root),
    )
    reconstruction = reconstruct_probe_turn(run_root, probe_id="P-04")
    assert reconstruction.fidelity["passed"], reconstruction.fidelity

    report = replay_probe_turn_battery(
        design=design,
        run_root=run_root,
        probe_id="P-04",
        n_samples=2,
        sandbox_dir=tmp_path / "sandbox",
        seat_factory=fake_seat_factory(),
    )
    assert report["fidelity"]["passed"], report["fidelity"]
    assert report["n_samples"] == 2
    assert report["n_errors"] == 0
    assert report["claim_level"] == "action_propensity_sandbox"
    assert sum(report["attempted_class_counts"].values()) == 2
    # the replay must not have written anything into the source bundle
    assert not list(run_root.glob("action_replay_*.json"))


def test_replay_battery_flags_corpus_mismatch(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s2_source"
    run_s2_world(
        design=design,
        corpus=corpus,
        run_root=run_root,
        seed=0,
        ticks=12,
        seat_factory=fake_seat_factory(),
        customer_llm=_LateBoundCustomer(run_root),
    )
    config_path = run_root / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["world"]["corpus"]["effective_corpus_hash"] = "tampered"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    report = replay_probe_turn_battery(
        design=design,
        run_root=run_root,
        probe_id="P-04",
        n_samples=1,
        sandbox_dir=tmp_path / "sandbox",
        seat_factory=fake_seat_factory(),
    )
    assert report["fidelity"]["corpus_hash_match"] is False
    assert report["fidelity"]["passed"] is False
