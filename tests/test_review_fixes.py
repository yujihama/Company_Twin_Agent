"""Tests for the second-review fixes (role bundles, span-specific S0, D4 read,
interactive customer, config/prompt consistency, stale visibility, scoped acceptance)."""
import hashlib
import json
from pathlib import Path

import pytest

from company_twin.acceptance import a05_grounding_population, a09_anchor_is_live, a10_tool_bundle_role_scoped, a11_stale_visibility
from company_twin.agents import load_role_card
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.harness import _s0_prompt, run_s1_episode
from company_twin.kernel import WorldKernel, KernelProfile
from company_twin.recorder import RunRecorder, read_jsonl
from company_twin.tools import ROLE_TOOL_BUNDLES, build_role_tools
from company_twin.world_config import build_world_config
from conftest import fake_seat_factory
from test_world_runs import _LateBoundCustomer


def _design_corpus():
    design = load_design(Path.cwd())
    return design, Corpus.from_design(design)


# ── Blocker 2: role tool bundles enforced at runtime ─────────────────────────

def test_role_tool_bundles_are_enforced_at_build(tmp_path: Path) -> None:
    design, corpus = _design_corpus()
    recorder = RunRecorder(tmp_path, "bundle")
    kernel = WorldKernel(recorder)
    names = lambda role: {t.__name__ for t in build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id="x", seat_role=role)}
    sales = names("sales")
    assert {"verify_identity", "link_review", "complete_contract", "deliver_documents", "approve_application"}.isdisjoint(sales)
    assert "submit_application" in sales  # K-sod-gate knob keeps governing this
    manager = names("manager")
    assert {"submit_application", "verify_identity", "deliver_documents"}.isdisjoint(manager)
    assert {"approve_application", "return_application"}.issubset(manager)


def test_kernel_hard_role_permission_last_defense(tmp_path: Path) -> None:
    design, _ = _design_corpus()
    recorder = RunRecorder(tmp_path, "hard")
    kernel = WorldKernel(recorder, KernelProfile(seat_roles={"emp-A": "sales"}, valid_doc_ids={"DFH-SAL-024"}))
    basis = {"retrieved": [{"doc_id": "DFH-SAL-024"}], "construal": "x", "decision": "y"}
    result = kernel.verify_identity("emp-A", "APP-1", True, True, "CONS-1", basis)
    assert result["success"] is False and "requires role" in result["denied_reason"]


# ── Blocker 3: span-specific S0 ──────────────────────────────────────────────

def test_s0_prompts_differ_per_span_same_probe() -> None:
    design, _ = _design_corpus()
    p_amb02 = _s0_prompt(design, "P-01", "AMB-02", 0)
    p_amb08 = _s0_prompt(design, "P-01", "AMB-08", 0)
    assert p_amb02 != p_amb08
    for prompt in (p_amb02, p_amb08):
        assert "AMB-" not in prompt and "span" not in prompt.lower()  # experimenter vocabulary never leaks
    assert _s0_prompt(design, "P-01", "AMB-02", 0) != _s0_prompt(design, "P-01", "AMB-02", 1)  # paraphrase variants


# ── Blocker 4: D4 store read path ────────────────────────────────────────────

def test_private_store_write_then_read_across_ticks(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, "store")
    recorder.set_tick(1)
    recorder.remember_private(seat_id="emp-A", key="k1", value="v1")
    recorder.set_tick(2)
    notes = recorder.read_private(seat_id="emp-A", limit=5)
    assert notes and notes[-1]["value"] == "v1"
    assert recorder.read_private(seat_id="emp-B") == []  # seat-private
    ops = [row.get("op") for row in read_jsonl(tmp_path / "store_events.jsonl")]
    assert "write" in ops and "read" in ops


def test_d4_disabled_removes_store_tools(tmp_path: Path) -> None:
    design, corpus = _design_corpus()
    recorder = RunRecorder(tmp_path, "d4off")
    kernel = WorldKernel(recorder)
    names = {t.__name__ for t in build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id="x", seat_role="sales", d4_enabled=False)}
    assert {"note_to_self", "recall_notes"}.isdisjoint(names)


# ── Blocker 5: interactive customer ─────────────────────────────────────────

def test_customer_replies_to_contact_next_tick(tmp_path: Path) -> None:
    design, corpus = _design_corpus()
    run_root = tmp_path / "s1"
    run_s1_episode(design=design, corpus=corpus, probe_id="P-01", run_root=run_root, seed=0, ticks=6, seat_factory=fake_seat_factory(), customer_llm=_LateBoundCustomer(run_root))
    ledger = read_jsonl(run_root / "world_ledger.jsonl")
    utterances = [row for row in ledger if row["event_type"] == "customer_utterance"]
    assert any(row["payload"].get("reply") is True for row in utterances), "customer never replied to staff contact"
    # replies are bounded (max 2 per actor) even though fake sales re-contacts on every utterance
    assert sum(1 for row in utterances if row["payload"].get("reply")) <= 2


# ── Blocker 7: config snapshot matches actual role card ─────────────────────

def test_world_config_role_card_hash_matches_prompt_source() -> None:
    design, _ = _design_corpus()
    config = build_world_config(design, stage="S1", model=None, seed=0, ticks=6)
    seats = config["world"]["population"]["seats"]
    for seat_id, seat in seats.items():
        role = seat["role"]
        card_text = load_role_card(design.root, role)
        if not seat["role_card_path"]:
            assert card_text == ""
            continue
        actual = hashlib.sha256((design.root / seat["role_card_path"]).read_bytes()).hexdigest()
        assert seat["role_card_sha256"] == actual
        assert (design.root / seat["role_card_path"]).read_text(encoding="utf-8") == card_text
    assert seats["emp-A"]["tools"] == list(ROLE_TOOL_BUNDLES["sales"]) + ["note_to_self", "recall_notes"]


# ── Major 6: stale visibility enforcement ────────────────────────────────────

def test_stale_v1_0_readable_only_by_sales(tmp_path: Path) -> None:
    design, corpus = _design_corpus()
    recorder = RunRecorder(tmp_path, "stale")
    kernel = WorldKernel(recorder)
    read_as = lambda role: {t.__name__: t for t in build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id=f"emp-{role}", seat_role=role)}["read_document"]
    ok = read_as("sales")("DFH-SAL-021@v1.0", "", 300)
    assert "denied" not in ok
    denied = read_as("second_line")("DFH-SAL-021@v1.0", "", 300)
    assert "not in your library index" in denied


# ── Blocker 1 / Major 1: scoped acceptance and strengthened A-05 ────────────

def test_a09_full_world_scope_requires_anchor(tmp_path: Path) -> None:
    (tmp_path / "s0_000_x").mkdir()
    assert a09_anchor_is_live(tmp_path, scope="s0_s1").passed
    result = a09_anchor_is_live(tmp_path, scope="full_world")
    assert not result.passed and "anchor" in result.detail


def test_a05_world_stage_requires_agent_actions(tmp_path: Path) -> None:
    root = tmp_path / "b"
    (root / "triage").mkdir(parents=True)
    (root / "triage" / "metrics.json").write_text(json.dumps({"controlled_actions_agent": 0, "basis_records_agent": 0, "origin_breakdown": {"agent": 1}}), encoding="utf-8")
    assert a05_grounding_population(root, stage="S0").passed
    result = a05_grounding_population(root, stage="S1")
    assert not result.passed and "controlled actions" in result.detail


def _attempt(seat: str, tool: str, *, success: bool = True, args: dict | None = None) -> dict:
    return {"seat_id": seat, "tool": tool, "args": args or {}, "success": success, "origin": "agent"}


def _mini_bundle(tmp_path: Path, attempts: list[dict]) -> Path:
    root = tmp_path / "mb"
    root.mkdir(parents=True)
    (root / "meta.json").write_text("{}", encoding="utf-8")
    (root / "attempts.jsonl").write_text("".join(json.dumps(a) + "\n" for a in attempts), encoding="utf-8")
    (root / "basis_records.jsonl").write_text("", encoding="utf-8")
    (root / "world_ledger.jsonl").write_text("", encoding="utf-8")
    return root


def test_a10_flags_wrong_role_success_but_allows_denials(tmp_path: Path) -> None:
    roles = {"emp-A": "sales", "emp-C": "application"}
    ok = _mini_bundle(tmp_path / "ok", [_attempt("emp-C", "verify_identity"), _attempt("emp-A", "verify_identity", success=False)])
    assert a10_tool_bundle_role_scoped(ok, roles).passed
    bad = _mini_bundle(tmp_path / "bad", [_attempt("emp-A", "deliver_documents")])
    result = a10_tool_bundle_role_scoped(bad, roles)
    assert not result.passed and "sales" in result.detail


def test_a11_flags_non_sales_stale_read(tmp_path: Path) -> None:
    roles = {"emp-Q": "second_line", "emp-A": "sales"}
    ok = _mini_bundle(tmp_path / "ok", [_attempt("emp-A", "read_document", args={"doc_id": "DFH-SAL-021@v1.0"})])
    assert a11_stale_visibility(ok, roles).passed
    bad = _mini_bundle(tmp_path / "bad", [_attempt("emp-Q", "read_document", args={"doc_id": "DFH-SAL-021@v1.0"})])
    assert not a11_stale_visibility(bad, roles).passed


# ── Major 4 (partial): ensemble incidence rates with Wilson intervals ────────

def test_ensemble_triage_groups_by_config_across_seeds(tmp_path: Path) -> None:
    from company_twin.oracles import aggregate_ensemble_triage, wilson_interval

    for seed, has_finding in enumerate([True, True, False]):
        root = tmp_path / f"s1_P-04_seed{seed}"
        (root / "triage").mkdir(parents=True)
        (root / "meta.json").write_text(json.dumps({"stage": "S1", "probe": "P-04", "knobs": {}, "seed": seed}), encoding="utf-8")
        (root / "triage" / "metrics.json").write_text(json.dumps({"controlled_actions_agent": 3, "finding_types": ({"evidence_gap": 2} if has_finding else {})}), encoding="utf-8")
    payload = aggregate_ensemble_triage(tmp_path)
    assert len(payload["groups"]) == 1
    group = payload["groups"][0]
    assert group["seeds"] == 3
    rate = group["finding_rates"]["evidence_gap"]
    assert rate["seeds_with_finding"] == 2 and abs(rate["rate"] - 2 / 3) < 1e-9
    low, high = rate["wilson_95"]
    assert 0.0 <= low < 2 / 3 < high <= 1.0
    assert wilson_interval(0, 0) == (0.0, 0.0)
