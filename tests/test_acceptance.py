"""Unit tests for the unfakeable acceptance gates.

Each gate is verified in BOTH directions on synthetic bundles: a compliant
bundle passes, and the specific violation the gate exists for is caught.
"""
import json
import hashlib
from pathlib import Path

from company_twin.acceptance import (
    a01_no_scripted_origin,
    a02_live_required,
    a03_inbox_whitelist,
    a04_basis_authorship,
    a07_stale_content_differs,
    a08_customer_is_agent,
    a11_role_tool_bundle_enforced,
    a12_role_card_snapshot_matches_prompt,
    a13_d4_store_has_read_path,
    run_acceptance,
)
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _attempt(seat: str, tool: str, *, origin: str = "agent", args: dict | None = None, success: bool = True) -> dict:
    return {"ts": "", "run_id": "r", "tick": 1, "seat_id": seat, "tool": tool, "args": args or {}, "success": success, "result": {}, "denied_reason": None, "origin": origin}


def _bundle(
    tmp_path: Path,
    name: str,
    *,
    attempts: list[dict],
    basis: list[dict] | None = None,
    ledger: list[dict] | None = None,
    meta: dict | None = None,
    config: dict | None = None,
    store: list[dict] | None = None,
    triage: dict | None = None,
) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "meta.json").write_text(json.dumps(meta or {"stage": "S1", "live": True}), encoding="utf-8")
    (root / "config.json").write_text(json.dumps(config or {}, ensure_ascii=False), encoding="utf-8")
    _write_jsonl(root / "attempts.jsonl", attempts)
    _write_jsonl(root / "basis_records.jsonl", basis or [])
    _write_jsonl(root / "world_ledger.jsonl", ledger or [])
    _write_jsonl(root / "store_events.jsonl", store or [])
    if triage is not None:
        (root / "triage").mkdir()
        (root / "triage" / "metrics.json").write_text(json.dumps(triage, ensure_ascii=False), encoding="utf-8")
    return root


LIVE_CALL = _attempt("emp-A", "llm_invoke", args={"backend": "deepagents", "model": "openrouter:x"})


def test_a01_flags_scripted_origin(tmp_path: Path) -> None:
    good = _bundle(tmp_path, "good", attempts=[LIVE_CALL])
    assert a01_no_scripted_origin(good).passed

    bad = _bundle(tmp_path, "bad", attempts=[LIVE_CALL, _attempt("emp-A", "send_chat", origin="agent_policy")])
    result = a01_no_scripted_origin(bad)
    assert not result.passed and "agent_policy" in result.detail


def test_a02_requires_deepagents_llm_invoke_and_meta(tmp_path: Path) -> None:
    good = _bundle(tmp_path, "good", attempts=[LIVE_CALL])
    assert a02_live_required(good).passed

    fake_backend = _bundle(tmp_path, "fake", attempts=[_attempt("emp-A", "llm_invoke", args={"backend": "test-fake"})])
    assert not a02_live_required(fake_backend).passed

    meta_false = _bundle(tmp_path, "metafalse", attempts=[LIVE_CALL], meta={"stage": "S1", "live": False})
    assert not a02_live_required(meta_false).passed


def test_a03_inbox_whitelist_on_recorded_ledger(tmp_path: Path) -> None:
    ok_message = {"kind": "customer_utterance", "tick": 1, "event_id": "E", "customer_id": "C", "application_id": "A", "product": "p", "deadline_display": "本日中", "utterance": "u"}
    good = _bundle(tmp_path, "good", attempts=[LIVE_CALL], ledger=[{"event_type": "inbox_delivered", "payload": {"to_seat": "emp-A", "message": ok_message}}])
    assert a03_inbox_whitelist(good).passed

    leak = dict(ok_message, probe_id="P-01")
    bad = _bundle(tmp_path, "bad", attempts=[LIVE_CALL], ledger=[{"event_type": "inbox_delivered", "payload": {"to_seat": "emp-A", "message": leak}}])
    result = a03_inbox_whitelist(bad)
    assert not result.passed and "probe_id" in result.detail


def test_a04_basis_requires_llm_authorship(tmp_path: Path) -> None:
    good = _bundle(
        tmp_path,
        "good",
        attempts=[LIVE_CALL, _attempt("emp-A", "record_interpretation_basis", args={"basis_id": "BASIS-000001"})],
        basis=[{"basis_id": "BASIS-000001", "seat_id": "emp-A"}],
    )
    assert a04_basis_authorship(good).passed

    before_llm = _bundle(
        tmp_path,
        "before",
        attempts=[_attempt("emp-A", "record_interpretation_basis", args={"basis_id": "BASIS-000001"}), LIVE_CALL],
        basis=[{"basis_id": "BASIS-000001", "seat_id": "emp-A"}],
    )
    assert not a04_basis_authorship(before_llm).passed

    orphan = _bundle(tmp_path, "orphan", attempts=[LIVE_CALL], basis=[{"basis_id": "BASIS-000009", "seat_id": "emp-A"}])
    result = a04_basis_authorship(orphan)
    assert not result.passed and "fabrication" in result.detail


def test_a07_stale_v1_0_content_really_differs() -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    result = a07_stale_content_differs(design, corpus)
    assert result.passed, result.detail
    stale = corpus.get("DFH-SAL-021@v1.0").text
    current = corpus.get("DFH-SAL-021").text
    assert stale and stale != current and "stale index copy" not in stale


def test_a08_customer_utterances_need_customer_llm(tmp_path: Path) -> None:
    utterance_event = {"event_type": "customer_utterance", "payload": {"event_id": "E"}}
    customer_call = _attempt("customer", "llm_invoke", origin="customer", args={"backend": "deepagents", "role": "customer"})
    good = _bundle(tmp_path, "good", attempts=[customer_call, LIVE_CALL], ledger=[utterance_event])
    assert a08_customer_is_agent(good).passed

    template_only = _bundle(tmp_path, "template", attempts=[LIVE_CALL], ledger=[utterance_event])
    assert not a08_customer_is_agent(template_only).passed


def test_full_world_scope_requires_s2_and_anchor(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    (campaign / "s0_divergence.json").write_text(
        json.dumps({"all_answers_live": True, "cells": [{"answers": 2, "entropy": 0.2}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)

    s0_s1 = run_acceptance(campaign_root=campaign, design=design, corpus=corpus, scope="s0_s1_only")
    assert s0_s1["passed"], s0_s1

    full = run_acceptance(campaign_root=campaign, design=design, corpus=corpus, scope="full_world")
    failed = {gate["gate"]: gate["detail"] for gate in full["campaign_gates"] if not gate["passed"]}
    assert not full["passed"]
    assert "A-09 anchor_is_live" in failed
    assert "A-10 s2_full_world_evidence" in failed


def test_a11_flags_role_bundle_escape(tmp_path: Path) -> None:
    config = {
        "world": {
            "population": {
                "seats": {
                    "emp-A": {
                        "role": "sales",
                        "tools": ["search_corpus", "read_document", "send_chat", "submit_application"],
                    }
                }
            }
        }
    }
    bundle = _bundle(
        tmp_path,
        "badtools",
        attempts=[LIVE_CALL, _attempt("emp-A", "submit_application")],
        config=config,
    )
    result = a11_role_tool_bundle_enforced(bundle)
    assert not result.passed
    assert "sales has application tools" in result.detail or "outside role bundle" in result.detail


def test_a12_matches_role_card_hash_to_llm_invoke(tmp_path: Path) -> None:
    text = "responsibility only"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    config = {"world": {"population": {"seats": {"emp-A": {"role": "sales", "tools": [], "role_card": {"text": text, "sha256": digest}}}}}}
    good = _bundle(
        tmp_path,
        "goodcard",
        attempts=[_attempt("emp-A", "llm_invoke", args={"backend": "deepagents", "role_card_hash": digest})],
        config=config,
    )
    assert a12_role_card_snapshot_matches_prompt(good).passed

    bad = _bundle(
        tmp_path,
        "badcard",
        attempts=[_attempt("emp-A", "llm_invoke", args={"backend": "deepagents", "role_card_hash": "bad"})],
        config=config,
    )
    result = a12_role_card_snapshot_matches_prompt(bad)
    assert not result.passed and "mismatch" in result.detail


def test_a13_requires_store_read_when_d4_store_is_enabled(tmp_path: Path) -> None:
    config = {"world": {"population": {"seats": {"emp-A": {"role": "sales", "tools": [], "store": {"enabled": True}}}}}}
    write_only = _bundle(
        tmp_path,
        "writeonly",
        attempts=[LIVE_CALL],
        meta={"stage": "S1", "live": True},
        config=config,
        store=[{"op": "write", "seat_id": "emp-A"}],
    )
    assert not a13_d4_store_has_read_path(write_only).passed

    read_write = _bundle(
        tmp_path,
        "readwrite",
        attempts=[LIVE_CALL],
        meta={"stage": "S1", "live": True},
        config=config,
        store=[{"op": "write", "seat_id": "emp-A"}, {"op": "read", "seat_id": "emp-A"}],
    )
    assert a13_d4_store_has_read_path(read_write).passed
