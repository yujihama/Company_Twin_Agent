"""Unit tests for the unfakeable acceptance gates.

Each gate is verified in BOTH directions on synthetic bundles: a compliant
bundle passes, and the specific violation the gate exists for is caught.
"""
import json
from pathlib import Path

from company_twin.acceptance import (
    a01_no_scripted_origin,
    a02_live_required,
    a03_inbox_whitelist,
    a04_basis_authorship,
    a07_stale_content_differs,
    a08_customer_is_agent,
)
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _attempt(seat: str, tool: str, *, origin: str = "agent", args: dict | None = None, success: bool = True) -> dict:
    return {"ts": "", "run_id": "r", "tick": 1, "seat_id": seat, "tool": tool, "args": args or {}, "success": success, "result": {}, "denied_reason": None, "origin": origin}


def _bundle(tmp_path: Path, name: str, *, attempts: list[dict], basis: list[dict] | None = None, ledger: list[dict] | None = None, meta: dict | None = None) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "meta.json").write_text(json.dumps(meta or {"stage": "S1", "live": True}), encoding="utf-8")
    _write_jsonl(root / "attempts.jsonl", attempts)
    _write_jsonl(root / "basis_records.jsonl", basis or [])
    _write_jsonl(root / "world_ledger.jsonl", ledger or [])
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
