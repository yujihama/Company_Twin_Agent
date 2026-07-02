"""Tests for the second-review fixes (role bundles, span-specific S0, D4 read,
interactive customer, config/prompt consistency, stale visibility, scoped acceptance)."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from company_twin.acceptance import a05_grounding_population, a09_anchor_is_live, a10_tool_bundle_role_scoped, a11_stale_visibility, a12_d4_store_read_before_action, a13_full_world_evidence
from company_twin.agents import load_role_card
from company_twin.campaign import static_world_surface_lint
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.harness import _s0_prompt, run_s1_episode
from company_twin.kernel import WorldKernel, KernelProfile
from company_twin.recorder import RunRecorder, read_jsonl
from company_twin.readiness import REPORT_SCHEMA_VERSION, run_readiness_gate, write_readiness_reports
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


def test_send_chat_is_seat_to_seat_only_customer_contact_uses_contact_tool(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, "chat-boundary")
    recorder.set_tick(1)
    kernel = WorldKernel(recorder, KernelProfile(seat_roles={"emp-A": "sales", "emp-M": "manager"}))

    denied = kernel.send_chat("emp-A", "CUS-001", "inbox", "顧客への説明")
    allowed = kernel.send_chat("emp-A", "emp-M", "workflow", "承認相談")

    assert denied["success"] is False
    assert "record_customer_contact" in denied["denied_reason"]
    assert allowed["sent"] is True
    ledger = read_jsonl(tmp_path / "world_ledger.jsonl")
    assert any(row["event_type"] == "permission_denied" for row in ledger)
    assert any((row["payload"] or {}).get("to_seat") == "emp-M" for row in ledger if row["event_type"] == "inbox_delivered")


class _NoActionSeat:
    backend = "test-fake"
    model = "fake:unit"

    def __init__(self, *, seat_id: str, recorder: RunRecorder):
        self.seat_id = seat_id
        self.recorder = recorder

    def turn(self, prompt: str) -> str:
        self.recorder.record_attempt(
            seat_id=self.seat_id,
            tool="llm_invoke",
            args={"backend": self.backend, "model": self.model, "prompt_chars": len(prompt)},
            success=True,
            result={"response_chars": 12},
        )
        return "確認中です"


def test_unresolved_inbox_is_requeued_without_fabricating_action(tmp_path: Path) -> None:
    design, corpus = _design_corpus()
    run_root = tmp_path / "noaction"

    def no_action_factory(**_ignored):
        def factory(*, seat_id: str, recorder: RunRecorder, **_kwargs) -> _NoActionSeat:
            return _NoActionSeat(seat_id=seat_id, recorder=recorder)

        return factory

    run_s1_episode(
        design=design,
        corpus=corpus,
        probe_id="P-01",
        run_root=run_root,
        seed=0,
        ticks=2,
        seat_factory=no_action_factory(),
        customer_llm=_LateBoundCustomer(run_root),
    )

    ledger = read_jsonl(run_root / "world_ledger.jsonl")
    attempts = read_jsonl(run_root / "attempts.jsonl")
    assert any(row["event_type"] == "inbox_requeued_unresolved" for row in ledger)
    assert not [row for row in attempts if row["tool"] in {"record_customer_contact", "submit_application", "approve_application"}]


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

def test_s0_prompt_requires_artifact_template() -> None:
    design, _ = _design_corpus()
    without_template = replace(
        design,
        s0_question_templates={key: value for key, value in design.s0_question_templates.items() if key != "AMB-02"},
    )

    with pytest.raises(ValueError, match="S0 question template missing"):
        _s0_prompt(without_template, "P-01", "AMB-02", 0)


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


def test_standalone_basis_is_not_grounding_coverage(tmp_path: Path) -> None:
    design, corpus = _design_corpus()
    recorder = RunRecorder(tmp_path, "basis-only")
    kernel = WorldKernel(recorder)
    tools = {tool.__name__: tool for tool in build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id="emp-A", seat_role="sales", include_workflow=False)}
    tools["record_interpretation_basis"]("s0_reading", '[{"doc_id":"DFH-SAL-021","version":"1.1"}]', "整理", "次に確認する", "読む")
    rows = read_jsonl(tmp_path / "basis_records.jsonl")
    assert rows and rows[0]["action_id"] is None
    assert rows[0]["grounded"] is None


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


def test_static_lint_blocks_legacy_policy_symbols() -> None:
    design, _ = _design_corpus()
    result = static_world_surface_lint(design)
    assert result["passed"], result["failures"]


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


def test_a12_requires_d4_read_before_s2_action(tmp_path: Path) -> None:
    root = tmp_path / "s2"
    root.mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"stage": "S2", "live": True}), encoding="utf-8")
    (root / "config.json").write_text(json.dumps({"runtime_delta": {"d4_enabled": True}}), encoding="utf-8")
    (root / "attempts.jsonl").write_text("", encoding="utf-8")
    (root / "basis_records.jsonl").write_text("", encoding="utf-8")
    (root / "world_ledger.jsonl").write_text("", encoding="utf-8")
    (root / "triage").mkdir(exist_ok=True)
    (root / "triage" / "metrics.json").write_text(json.dumps({"store_reads_agent": 0, "controlled_actions_after_store_read": 0}), encoding="utf-8")
    assert not a12_d4_store_read_before_action(root).passed
    (root / "triage" / "metrics.json").write_text(json.dumps({"store_reads_agent": 1, "controlled_actions_after_store_read": 1}), encoding="utf-8")
    assert a12_d4_store_read_before_action(root).passed


# ── Major 4 (partial): ensemble incidence rates with Wilson intervals ────────

def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _s2_bundle(root: Path, *, anchor: bool, month_end: bool = True) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "meta.json").write_text(json.dumps({"stage": "S2", "live": True, "anchor": anchor}), encoding="utf-8")
    events = [{"event_type": "customer_utterance"}]
    if month_end:
        events.append({"event_type": "month_end_close"})
    _write_jsonl(root / "world_ledger.jsonl", events)
    _write_jsonl(root / "attempts.jsonl", [{"tool": "llm_invoke", "args": {"backend": "deepagents"}, "origin": "agent"}])
    _write_jsonl(root / "basis_records.jsonl", [])
    (root / "triage").mkdir(exist_ok=True)
    (root / "triage" / "metrics.json").write_text(json.dumps({"controlled_actions_agent": 1, "basis_action_bound": 1}), encoding="utf-8")


def test_a13_full_world_evidence_rejects_anchor_only_and_requires_month_end(tmp_path: Path) -> None:
    _s2_bundle(tmp_path / "anchor_s2_seed0", anchor=True)
    for filename in ("ensemble_triage.json", "attribution_table.json", "min_repro_jobs.json", "finding_registry.json"):
        (tmp_path / filename).write_text("{}", encoding="utf-8")

    result = a13_full_world_evidence(tmp_path)
    assert not result.passed and "non-anchor" in result.detail

    _s2_bundle(tmp_path / "s2_seed0", anchor=False, month_end=False)
    result = a13_full_world_evidence(tmp_path)
    assert not result.passed and "month_end_close" in result.detail

    _s2_bundle(tmp_path / "s2_seed0", anchor=False, month_end=True)
    assert a13_full_world_evidence(tmp_path).passed


def test_stage9_readiness_is_stricter_than_harness_acceptance(tmp_path: Path) -> None:
    (tmp_path / "acceptance_report.json").write_text(json.dumps({"scope": "full_world", "passed": True}), encoding="utf-8")
    _s2_bundle(tmp_path / "s2_seed0", anchor=False)

    payload = run_readiness_gate(tmp_path)

    assert payload["passed"] is False
    failed = {check["check"] for check in payload["checks"] if not check["passed"]}
    assert "routine_smoke_passed" in failed
    assert "s0_divergence_sanity" in failed
    assert "leak_lint_passed" in failed
    assert "semantic_grounding_all3_threshold" in failed
    assert "holdout_passed" in failed
    assert (tmp_path / "readiness_report.json").exists()


def test_readiness_reports_are_schema_backed_and_do_not_fake_stage9_pass(tmp_path: Path) -> None:
    _, corpus = _design_corpus()

    manifest = write_readiness_reports(tmp_path, corpus=corpus, lint_payload={"passed": True, "failures": []})

    assert "retrieval_audit.json" in manifest["reports"]
    assert "backcasting_report.json" in manifest["blocked_reports"]
    for filename in (
        "routine_smoke_report.json",
        "retrieval_audit.json",
        "leak_lint_report.json",
        "semantic_grounding_report.json",
        "backcasting_report.json",
        "sme_blind_review.json",
        "holdout_report.json",
    ):
        payload = json.loads((tmp_path / filename).read_text(encoding="utf-8"))
        assert payload["schema_version"] == REPORT_SCHEMA_VERSION
        assert "passed" in payload

    readiness = run_readiness_gate(tmp_path)
    assert readiness["passed"] is False
    failed = {check["check"] for check in readiness["checks"] if not check["passed"]}
    assert "backcasting_passed" in failed
    assert "semantic_grounding_all3_threshold" in failed


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
    assert (tmp_path / "min_repro_jobs.json").exists()
    assert payload["min_repro_jobs"][0]["status"] == "pending"
    assert (tmp_path / "finding_registry.json").exists()
    assert payload["finding_registry"]["confirmed_findings"] == []
    assert payload["finding_registry"]["audit_hypothesis_cards"] == []
    assert payload["finding_registry"]["exploratory_buckets"]


def test_ensemble_triage_emits_delta_one_attribution_candidates(tmp_path: Path) -> None:
    from company_twin.oracles import aggregate_ensemble_triage

    configs = [
        ("off", {}, {"evidence_gap": 1}),
        ("on", {"K-completion-gate": True}, {}),
    ]
    for label, knobs, finding_types in configs:
        root = tmp_path / f"s2_{label}_seed0"
        (root / "triage").mkdir(parents=True)
        (root / "meta.json").write_text(json.dumps({"stage": "S2", "probe": "", "knobs": knobs, "seed": 0}), encoding="utf-8")
        (root / "triage" / "metrics.json").write_text(json.dumps({"controlled_actions_agent": 3, "finding_types": finding_types}), encoding="utf-8")

    payload = aggregate_ensemble_triage(tmp_path)

    assert (tmp_path / "attribution_table.json").exists()
    assert payload["attribution_table"]
    row = payload["attribution_table"][0]
    assert row["status"] == "candidate"
    assert row["delta_knob"] == "K-completion-gate"
