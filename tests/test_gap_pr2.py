import json
from pathlib import Path
from typing import Any

from company_twin.acceptance import a03_inbox_whitelist, a14_confirmed_requires_fresh_reproduction
from company_twin.campaign import default_s0_models
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.harness import run_s1_episode, run_s2_world
from company_twin.oracles import aggregate_ensemble_triage, execute_fresh_min_repro_confirmation, execute_min_repro_jobs, load_detection_rules, write_triage
from company_twin.recorder import RunRecorder, read_jsonl
from conftest import fake_seat_factory
from test_world_runs import _LateBoundCustomer


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def test_default_s0_models_include_two_distinct_cold_readers() -> None:
    models = default_s0_models("openrouter:qwen/qwen3.6-flash")

    assert models[0] == "openrouter:qwen/qwen3.6-flash"
    assert len(set(models)) >= 2


def test_timed_notice_and_scc_switch_are_schedule_driven(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s2"

    run_s2_world(
        design=design,
        corpus=corpus,
        run_root=run_root,
        seed=0,
        ticks=6,
        scc_switch_tick=3,
        seat_factory=fake_seat_factory(),
        customer_llm=_LateBoundCustomer(run_root),
    )

    ledger = read_jsonl(run_root / "world_ledger.jsonl")
    completion = [row for row in ledger if row["event_type"] == "completion_gate_active"]
    assert completion and completion[0]["tick"] == 3
    assert any(row["event_type"] == "campaign_deadline" and row["tick"] == 6 for row in ledger)
    notices = [
        (row.get("payload") or {}).get("message") or {}
        for row in ledger
        if row["event_type"] == "inbox_delivered"
    ]
    assert any(message.get("kind") == "timed_notice" and message.get("notice") == "campaign_deadline" for message in notices)
    assert a03_inbox_whitelist(run_root).passed
    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    assert config["world"]["schedule"]["approval_due_ticks"] == 2
    assert "emp-Q" in config["world"]["schedule"]["approval_notice_recipients"]


def test_s2_seats_subset_trims_world_population(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s2_subset"

    run_s2_world(
        design=design,
        corpus=corpus,
        run_root=run_root,
        seed=0,
        ticks=1,
        seats_subset=["emp-C"],
        seat_factory=fake_seat_factory(),
        customer_llm=_LateBoundCustomer(run_root),
    )

    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    assert sorted(config["world"]["population"]["seats"]) == ["emp-C"]
    assert sorted(config["world"]["population"]["binding"]) == ["emp-C"]
    assert config["runtime_delta"]["seats_subset"] == ["emp-C"]


def test_approval_deadline_overrun_delivers_timed_notice(tmp_path: Path) -> None:
    from company_twin.kernel import KernelProfile, WorldKernel

    recorder = RunRecorder(tmp_path, "approval-deadline")
    recorder.set_tick(1)
    kernel = WorldKernel(
        recorder,
        KernelProfile(
            seat_roles={"emp-A": "sales", "emp-M": "manager", "emp-Q": "second_line"},
            approval_due_ticks=1,
            approval_notice_recipients=("emp-Q",),
        ),
    )
    app = kernel._ensure_application("APP-1", customer_id="CUS-1", product="p")
    app["approvals"] = [{"approval_id": "APR-0001", "application_id": "APP-1", "requested_by": "emp-A", "approver_role": "manager", "status": "requested", "requested_tick": 1, "due_tick": 2}]

    kernel.fire_timed_events(3)

    ledger = read_jsonl(tmp_path / "world_ledger.jsonl")
    assert any(row["event_type"] == "approval_deadline_overrun" for row in ledger)
    notices = [(row.get("payload") or {}).get("message") or {} for row in ledger if row["event_type"] == "inbox_delivered"]
    recipients = [(row.get("payload") or {}).get("to_seat") for row in ledger if row["event_type"] == "inbox_delivered"]
    assert any(message.get("notice") == "approval_deadline_overrun" for message in notices)
    assert {"emp-A", "emp-M", "emp-Q"}.issubset(set(recipients))
    assert a03_inbox_whitelist(tmp_path).passed


def test_granted_approval_does_not_fire_deadline_overrun(tmp_path: Path) -> None:
    from company_twin.kernel import KernelProfile, WorldKernel

    recorder = RunRecorder(tmp_path, "approval-deadline-granted")
    recorder.set_tick(1)
    kernel = WorldKernel(
        recorder,
        KernelProfile(
            seat_roles={"emp-A": "sales", "emp-M": "manager", "emp-Q": "second_line"},
            approval_due_ticks=1,
            approval_notice_recipients=("emp-Q",),
        ),
    )
    app = kernel._ensure_application("APP-1", customer_id="CUS-1", product="p")
    # Grants are appended as separate entries while the original request entry
    # keeps status "requested"; an on-time grant must suppress the overdue notice.
    app["approvals"] = [
        {"approval_id": "APR-0001", "application_id": "APP-1", "requested_by": "emp-A", "approver_role": "manager", "status": "requested", "requested_tick": 1, "due_tick": 2},
        {"approval_id": "APR-0001", "application_id": "APP-1", "approved_by": "emp-M", "condition": "", "status": "approved", "action_id": "ACT-1"},
        {"approval_id": "APR-0003", "application_id": "APP-1", "requested_by": "emp-A", "approver_role": "manager", "status": "requested", "requested_tick": 1, "due_tick": 2},
    ]

    kernel.fire_timed_events(3)

    ledger = read_jsonl(tmp_path / "world_ledger.jsonl")
    overruns = [row for row in ledger if row["event_type"] == "approval_deadline_overrun"]
    # Only the never-granted APR-0003 may fire; granted APR-0001 must stay silent.
    assert [str((row.get("payload") or {}).get("approval_id")) for row in overruns] == ["APR-0003"]


def test_seat_model_binding_reaches_runtime_factory(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s1"
    captured: dict[str, str] = {}

    class SpySeat:
        backend = "test-fake"

        def __init__(self, *, seat_id: str, model: str, recorder: RunRecorder):
            self.seat_id = seat_id
            self.model = model
            self.recorder = recorder

        def turn(self, prompt: str) -> str:
            self.recorder.record_attempt(seat_id=self.seat_id, tool="llm_invoke", args={"backend": self.backend, "model": self.model}, success=True, result={})
            self.recorder.record_attempt(seat_id=self.seat_id, tool="llm_response", args={"backend": self.backend, "model": self.model}, success=True, result={})
            return "no-op"

    def spy_factory(*, seat_id: str, role: str, tools: list[Any], recorder: RunRecorder, recursion_limit: int, model: str) -> SpySeat:
        captured[seat_id] = model
        return SpySeat(seat_id=seat_id, model=model, recorder=recorder)

    run_s1_episode(
        design=design,
        corpus=corpus,
        probe_id="P-01",
        run_root=run_root,
        seed=0,
        ticks=1,
        model_bindings={"emp-A": "openrouter:qwen/qwen3.5-9b"},
        seat_factory=spy_factory,
        customer_llm=_LateBoundCustomer(run_root),
    )

    assert captured["emp-A"] == "openrouter:qwen/qwen3.5-9b"
    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    assert config["world"]["population"]["binding"]["emp-A"] == "openrouter:qwen/qwen3.5-9b"


# ---------------------------------------------------------------------------
# Customer-model knob (data/design/MASTER_DESIGN.md §17.11): the customer is
# world scenery, not the measurement subject (seats are), so upgrading its
# model quality is legitimate and must never touch seat model selection. See
# also company_twin.world_config.build_world_config's customer_model param
# and cli.py's --customer-model option on s0/s1/s2/campaign.
# ---------------------------------------------------------------------------


def test_customer_model_defaults_to_seat_model_when_unset(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s1_customer_default"

    run_s1_episode(
        design=design,
        corpus=corpus,
        probe_id="P-01",
        run_root=run_root,
        seed=0,
        ticks=1,
        model="openrouter:qwen/qwen3.6-flash",
        seat_factory=fake_seat_factory(),
        customer_llm=_LateBoundCustomer(run_root),
    )

    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    assert config["model"]["customer"] == "openrouter:qwen/qwen3.6-flash"
    assert config["model"]["customer"] == config["model"]["default"]


def test_customer_model_override_is_recorded_without_touching_seat_bindings(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s1_customer_override"

    run_s1_episode(
        design=design,
        corpus=corpus,
        probe_id="P-01",
        run_root=run_root,
        seed=0,
        ticks=1,
        model="openrouter:qwen/qwen3.6-flash",
        customer_model="openrouter:qwen/qwen3.5-9b",
        seat_factory=fake_seat_factory(),
        customer_llm=_LateBoundCustomer(run_root),
    )

    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    assert config["model"]["customer"] == "openrouter:qwen/qwen3.5-9b"
    # seats keep the ordinary --model default; --customer-model must never
    # change seat model selection.
    assert config["model"]["default"] == "openrouter:qwen/qwen3.6-flash"
    for seat_id, bound_model in config["world"]["population"]["binding"].items():
        assert bound_model == "openrouter:qwen/qwen3.6-flash", f"seat {seat_id} model was affected by --customer-model"


def test_customer_model_override_reaches_default_customer_llm_constructor(tmp_path: Path, monkeypatch) -> None:
    # When no explicit customer_llm is supplied, --customer-model must reach
    # the actual CustomerLLM constructor (default_customer_llm), not just the
    # recorded config -- otherwise the knob would be cosmetic.
    import company_twin.harness as harness_module

    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s1_customer_ctor"
    captured: dict[str, str] = {}

    def fake_default_customer_llm(*, model: str, recorder: RunRecorder):
        captured["model"] = model
        return _LateBoundCustomer(run_root)

    monkeypatch.setattr(harness_module, "default_customer_llm", fake_default_customer_llm)

    run_s1_episode(
        design=design,
        corpus=corpus,
        probe_id="P-01",
        run_root=run_root,
        seed=0,
        ticks=1,
        model="openrouter:qwen/qwen3.6-flash",
        customer_model="openrouter:qwen/qwen3.5-9b",
        seat_factory=fake_seat_factory(),
        # customer_llm intentionally omitted: exercises the default_customer_llm path.
    )

    assert captured["model"] == "openrouter:qwen/qwen3.5-9b"


def test_customer_model_recorded_for_s2_and_s0(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)

    s2_root = tmp_path / "s2_customer"
    run_s2_world(
        design=design,
        corpus=corpus,
        run_root=s2_root,
        seed=0,
        ticks=1,
        model="openrouter:qwen/qwen3.6-flash",
        customer_model="openrouter:qwen/qwen3.5-9b",
        seat_factory=fake_seat_factory(),
        customer_llm=_LateBoundCustomer(s2_root),
    )
    s2_config = json.loads((s2_root / "config.json").read_text(encoding="utf-8"))
    assert s2_config["model"]["customer"] == "openrouter:qwen/qwen3.5-9b"

    from company_twin.harness import run_s0

    s0_root = tmp_path / "s0_customer"
    run_s0(
        design=design,
        corpus=corpus,
        probe_id="P-01",
        seat_id="emp-A",
        run_root=s0_root,
        span_id=design.probes["P-01"].binds[0],
        model="openrouter:qwen/qwen3.6-flash",
        customer_model="openrouter:qwen/qwen3.5-9b",
        seat_factory=fake_seat_factory(),
    )
    s0_config = json.loads((s0_root / "config.json").read_text(encoding="utf-8"))
    assert s0_config["model"]["customer"] == "openrouter:qwen/qwen3.5-9b"


def test_customer_model_cli_option_plumbs_into_s1(tmp_path: Path, monkeypatch) -> None:
    import company_twin.cli as cli_module

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-used")
    monkeypatch.setattr(cli_module, "load_local_env", lambda root: None)
    captured: dict[str, Any] = {}

    def fake_run_s1_episode(**kwargs):
        captured.update(kwargs)
        (kwargs["run_root"]).mkdir(parents=True, exist_ok=True)
        return {"run_root": str(kwargs["run_root"])}

    monkeypatch.setattr(cli_module, "run_s1_episode", fake_run_s1_episode)

    from typer.testing import CliRunner

    runner = CliRunner()
    run_root = tmp_path / "s1_cli_customer"
    result = runner.invoke(
        cli_module.app,
        [
            "s1",
            "--root",
            str(Path.cwd()),
            "--run-root",
            str(run_root),
            "--customer-model",
            "openrouter:qwen/qwen3.5-9b",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured.get("customer_model") == "openrouter:qwen/qwen3.5-9b"


def test_rule_hit_rate_detection_miss_rate_and_coverage_map_are_written(tmp_path: Path) -> None:
    run_root = tmp_path / "s1_seed0"
    (run_root / "triage").mkdir(parents=True)
    (run_root / "meta.json").write_text(json.dumps({"stage": "S1", "probe": "P-04", "knobs": {}, "seed": 0}), encoding="utf-8")
    _write_jsonl(
        run_root / "attempts.jsonl",
        [
            {
                "tick": 1,
                "seat_id": "emp-C",
                "tool": "submit_application",
                "args": {"application_id": "APP-1", "evidence": {"material_version": "v1.1"}},
                "success": True,
                "result": {},
                "origin": "agent",
            },
            {
                "tick": 1,
                "seat_id": "emp-C",
                "tool": "read_document",
                "args": {"doc_id": "DFH-SAL-024"},
                "success": True,
                "result": {"version": "1.1", "citation_handle": "read:DFH-SAL-024:v1.1", "text": "x"},
                "origin": "agent",
            },
        ],
    )
    _write_jsonl(run_root / "basis_records.jsonl", [])
    _write_jsonl(run_root / "world_ledger.jsonl", [{"tick": 1, "event_type": "application_submitted", "payload": {"application_id": "APP-1"}}])
    _write_jsonl(run_root / "store_events.jsonl", [])

    triage = write_triage(run_root)
    metrics = triage["metrics"]
    assert metrics["rule_hit_rate"]["TRUTH-EVIDENCE-GAP"]["opportunity_count"] == 1
    assert metrics["rule_hit_rate"]["TRUTH-EVIDENCE-GAP"]["hit_count"] == 1
    assert metrics["rule_hit_rate"]["TRUTH-EVIDENCE-GAP"]["hit_rate"] == 1
    assert metrics["detection_miss_rate"]["evidence_gap"]["truth_count"] == 1
    assert metrics["detection_miss_rate"]["evidence_gap"]["detected_count"] == 1
    assert metrics["detection_miss_rate"]["evidence_gap"]["miss_rate"] == 0

    ensemble = aggregate_ensemble_triage(tmp_path)
    coverage = json.loads((tmp_path / "coverage_map.json").read_text(encoding="utf-8"))
    assert coverage["schema_version"] == "company_twin.coverage_map.v1"
    assert coverage["cell_counts"]["C5_evidence_skeleton"] >= 1
    assert coverage["cell_counts"]["C3_doc_contacts"] >= 1
    assert "C3_norm_contacts" not in coverage["cells"]
    assert ensemble["coverage_map"]["path"] == "coverage_map.json"
    assert "rule_hit_rate" in ensemble["groups"][0]


def test_coverage_c1_uses_class_ids_not_raw_wording(tmp_path: Path) -> None:
    run_root = tmp_path / "s0_000"
    run_root.mkdir()
    (run_root / "meta.json").write_text(json.dumps({"stage": "S0", "probe": "P-01", "span": "AMB-02", "seat": "emp-A"}), encoding="utf-8")
    (run_root / "s0_answer.json").write_text(
        json.dumps({"span_id": "AMB-02", "seat_id": "emp-A", "role": "sales", "likely_reading": "wording should not become a coverage cell"}),
        encoding="utf-8",
    )
    _write_jsonl(run_root / "attempts.jsonl", [])
    _write_jsonl(run_root / "basis_records.jsonl", [])
    _write_jsonl(run_root / "world_ledger.jsonl", [])

    aggregate_ensemble_triage(tmp_path)
    coverage = json.loads((tmp_path / "coverage_map.json").read_text(encoding="utf-8"))
    c1_cells = [row["cell"] for row in coverage["cells"]["C1_span_role_interpretation"]]

    assert c1_cells == ["AMB-02 | sales | novel_or_unclassified"]
    assert "wording should not" not in c1_cells[0]


def test_detection_rules_v1_is_rejected(tmp_path: Path) -> None:
    rules_dir = tmp_path / "data" / "compiled_data"
    rules_dir.mkdir(parents=True)
    (rules_dir / "detection_rules_v1.json").write_text(json.dumps({"schema_version": "company_twin.detection_rules.v1", "rules": []}), encoding="utf-8")

    try:
        load_detection_rules(tmp_path)
    except ValueError as exc:
        assert "company_twin.detection_rules.v2" in str(exc)
    else:
        raise AssertionError("v1 detection rules must be rejected")


def _write_min_repro_source(
    campaign_root: Path,
    name: str,
    *,
    seed: int,
    has_finding: bool,
    stage: str = "S1",
    probe: str | None = "P-04",
    finding_type: str = "evidence_gap",
    signature: str | None = None,
    seat_id: str = "emp-C",
) -> None:
    run_root = campaign_root / name
    (run_root / "triage").mkdir(parents=True)
    (run_root / "meta.json").write_text(json.dumps({"stage": stage, "probe": probe, "knobs": {}, "seed": seed, "anchor": False}), encoding="utf-8")
    (run_root / "triage" / "metrics.json").write_text(
        json.dumps({"controlled_actions_agent": 1, "finding_types": ({finding_type: 1} if has_finding else {})}),
        encoding="utf-8",
    )
    buckets = [
        {
            "signature": signature or f"sig-{seed}",
            "count": 1,
            "opportunity_denominator": 1,
            "rate": 1.0,
            "finding_type": finding_type,
            "seat_id": seat_id,
            "anchor_id": "submit_application",
            "phase": "application",
            "example": "missing consent_log_id,recording_id",
            "min_repro_status": "candidate",
        }
    ] if has_finding else []
    (run_root / "triage" / "buckets.json").write_text(json.dumps({"buckets": buckets}), encoding="utf-8")
    evidence = {"material_version": "v1.1"} if has_finding else {"material_version": "v1.1", "consent_log_id": "CONS-1", "recording_id": "REC-1"}
    _write_jsonl(
        run_root / "attempts.jsonl",
        [
            {
                "tick": seed + 1,
                "seat_id": "emp-C",
                "tool": "submit_application",
                "args": {"application_id": f"APP-{seed}", "evidence": evidence},
                "success": True,
                "result": {},
                "origin": "agent",
            }
        ],
    )
    _write_jsonl(run_root / "basis_records.jsonl", [])
    _write_jsonl(run_root / "world_ledger.jsonl", [])
    _write_jsonl(run_root / "store_events.jsonl", [])


def _write_fresh_min_repro_bundle(
    run_root: Path,
    seed: int,
    config: dict[str, Any],
    finding_type: str,
    *,
    signature: str,
) -> None:
    run_root.mkdir(parents=True)
    (run_root / "triage").mkdir()
    (run_root / "meta.json").write_text(
        json.dumps(
            {
                "stage": config["stage"],
                "probe": config["probe"],
                "knobs": config.get("knobs") or {},
                "seed": seed,
                "anchor": False,
                "live": True,
                "backend": "deepagents",
            }
        ),
        encoding="utf-8",
    )
    (run_root / "triage" / "metrics.json").write_text(
        json.dumps({"controlled_actions_agent": 1, "finding_types": {finding_type: 1}}),
        encoding="utf-8",
    )
    (run_root / "triage" / "buckets.json").write_text(
        json.dumps({"buckets": [{"signature": signature, "finding_type": finding_type, "seat_id": "emp-C", "count": 1}]}),
        encoding="utf-8",
    )
    _write_jsonl(
        run_root / "attempts.jsonl",
        [
            {"tick": 1, "seat_id": "emp-C", "tool": "llm_invoke", "args": {"backend": "deepagents"}, "success": True, "result": {}, "origin": "agent"},
            {"tick": 1, "seat_id": "emp-C", "tool": "submit_application", "args": {"evidence": {"material_version": "v1.1"}}, "success": True, "result": {}, "origin": "agent"},
        ],
    )
    _write_jsonl(run_root / "basis_records.jsonl", [])
    _write_jsonl(run_root / "world_ledger.jsonl", [])
    _write_jsonl(run_root / "store_events.jsonl", [])


def test_min_repro_collation_never_promotes_confirmed_findings(tmp_path: Path) -> None:
    for seed, has_finding in enumerate([True, True, False]):
        _write_min_repro_source(tmp_path, f"s1_P-04_seed{seed}", seed=seed, has_finding=has_finding)

    queued = aggregate_ensemble_triage(tmp_path)
    assert queued["min_repro_jobs"][0]["status"] == "pending"
    assert queued["min_repro_jobs"][0]["pre_registered_confirmation"] == {
        "min_rate": round(2 / 3, 6),
        "confirmation_seeds": 3,
        "basis": "queued_exploration_rate",
        "registered_at": "ensemble_triage_queue",
    }
    assert queued["finding_registry"]["confirmed_findings"] == []

    executed = execute_min_repro_jobs(tmp_path, min_rate=0.5, min_seeds=3)

    job = executed["jobs"][0]
    assert job["status"] == "evidence_collated"
    assert job["source_bundle_count"] == 2
    assert abs(job["evidence_rate"] - 2 / 3) < 1e-9
    manifest = json.loads((tmp_path / job["confirmation_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "evidence_collated"
    assert manifest["pre_registered_confirmation"]["min_rate"] == round(2 / 3, 6)
    assert manifest["reduction_trace"][1]["step"] == "deck_one_card"

    registry = json.loads((tmp_path / "finding_registry.json").read_text(encoding="utf-8"))
    assert registry["confirmed_findings"] == []
    assert registry["audit_hypothesis_cards"] == []
    updated_jobs = json.loads((tmp_path / "min_repro_jobs.json").read_text(encoding="utf-8"))["jobs"]
    assert updated_jobs[0]["status"] == "evidence_collated"
    assert a14_confirmed_requires_fresh_reproduction(tmp_path).passed


def test_min_repro_recollation_preserves_reproduced_jobs_and_registry(tmp_path: Path) -> None:
    for seed, has_finding in enumerate([True, True, False]):
        _write_min_repro_source(tmp_path, f"s1_P-04_seed{seed}", seed=seed, has_finding=has_finding)
    aggregate_ensemble_triage(tmp_path)
    execute_min_repro_jobs(tmp_path, min_rate=0.5, min_seeds=3)

    def write_confirmation_bundle(run_root: Path, seed: int, config: dict[str, Any], finding_type: str) -> None:
        _write_fresh_min_repro_bundle(run_root, seed, config, finding_type, signature="sig-0")

    payload = execute_fresh_min_repro_confirmation(
        tmp_path,
        finding_type="evidence_gap",
        confirmation_seeds=3,
        seed_start=100,
        min_rate=2 / 3,
        confirmation_bundle_runner=write_confirmation_bundle,
    )
    before_jobs = json.loads((tmp_path / "min_repro_jobs.json").read_text(encoding="utf-8"))["jobs"]
    before_job = before_jobs[0]
    before_manifest = json.loads((tmp_path / payload["manifest_path"]).read_text(encoding="utf-8"))
    preserved_fields = {
        key: before_job[key]
        for key in (
            "reproduction_rate",
            "reproduction_rate_basis",
            "type_confirmation_successes",
            "type_reproduction_rate",
            "type_reproduction_rate_wilson_95",
            "signature_confirmation_successes",
            "signature_reproduction_rate",
            "signature_reproduction_rate_wilson_95",
            "confirmation_successes",
        )
    }

    execute_min_repro_jobs(tmp_path, min_rate=0.5, min_seeds=3)

    after_jobs = json.loads((tmp_path / "min_repro_jobs.json").read_text(encoding="utf-8"))["jobs"]
    after_job = after_jobs[0]
    assert after_job["status"] == "reproduced"
    assert {key: after_job[key] for key in preserved_fields} == preserved_fields
    assert json.loads((tmp_path / payload["manifest_path"]).read_text(encoding="utf-8")) == before_manifest
    registry = json.loads((tmp_path / "finding_registry.json").read_text(encoding="utf-8"))
    assert registry["confirmed_findings"][0]["job_id"] == payload["job_id"]
    assert registry["confirmed_findings"][0]["signature_reproduction_rate"] == preserved_fields["signature_reproduction_rate"]
    assert registry["audit_hypothesis_cards"][0]["min_repro"]["job_id"] == payload["job_id"]
    assert registry["audit_hypothesis_cards"][0]["min_repro"]["signature_reproduction_rate"] == preserved_fields["signature_reproduction_rate"]


def test_fresh_min_repro_confirmation_promotes_reproduced_with_disjoint_live_seeds(tmp_path: Path) -> None:
    for seed, has_finding in enumerate([True, True, False]):
        _write_min_repro_source(tmp_path, f"s1_P-04_seed{seed}", seed=seed, has_finding=has_finding)
    aggregate_ensemble_triage(tmp_path)

    def write_confirmation_bundle(run_root: Path, seed: int, config: dict[str, Any], finding_type: str) -> None:
        run_root.mkdir(parents=True)
        (run_root / "triage").mkdir()
        (run_root / "meta.json").write_text(
            json.dumps(
                {
                    "stage": config["stage"],
                    "probe": config["probe"],
                    "knobs": config.get("knobs") or {},
                    "seed": seed,
                    "anchor": False,
                    "live": True,
                    "backend": "deepagents",
                }
            ),
            encoding="utf-8",
        )
        (run_root / "triage" / "metrics.json").write_text(
            json.dumps({"controlled_actions_agent": 1, "finding_types": {finding_type: 1}}),
            encoding="utf-8",
        )
        (run_root / "triage" / "buckets.json").write_text(
            json.dumps(
                {
                    "buckets": [
                        {
                            "signature": "sig-0",
                            "finding_type": finding_type,
                            "seat_id": "emp-C",
                            "count": 1,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        _write_jsonl(
            run_root / "attempts.jsonl",
            [
                    {"tick": 1, "seat_id": "emp-C", "tool": "llm_invoke", "args": {"backend": "deepagents"}, "success": True, "result": {}, "origin": "agent"},
                {"tick": 1, "seat_id": "emp-C", "tool": "submit_application", "args": {"evidence": {"material_version": "v1.1"}}, "success": True, "result": {}, "origin": "agent"},
            ],
        )
        _write_jsonl(run_root / "basis_records.jsonl", [])
        _write_jsonl(run_root / "world_ledger.jsonl", [])
        _write_jsonl(run_root / "store_events.jsonl", [])

    payload = execute_fresh_min_repro_confirmation(
        tmp_path,
        finding_type="evidence_gap",
        confirmation_seeds=3,
        seed_start=100,
        min_rate=2 / 3,
        confirmation_bundle_runner=write_confirmation_bundle,
    )

    assert payload["status"] == "reproduced"
    assert payload["confirmed_count"] == 1
    assert payload["source_bundle_count"] == 3
    manifest = json.loads((tmp_path / payload["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "reproduced"
    assert manifest["pre_registered_confirmation"]["min_rate"] == round(2 / 3, 6)
    assert manifest["threshold_override"]["enabled"] is False
    assert manifest["fresh_seeds"] == [100, 101, 102]
    assert manifest["expected_bucket_signatures"] == ["sig-0", "sig-1"]
    assert manifest["reproduction_rate_basis"] == "signature"
    assert manifest["type_confirmation_successes"] == 3
    assert manifest["type_reproduction_rate"] == 1.0
    assert manifest["type_reproduction_rate_wilson_95"] == [0.4385, 1.0]
    assert manifest["signature_confirmation_successes"] == 3
    assert manifest["signature_reproduction_rate"] == 1.0
    assert manifest["signature_reproduction_rate_wilson_95"] == [0.4385, 1.0]
    assert manifest["reproduction_rate_wilson_95"] == [0.4385, 1.0]
    assert all(bundle["run_root"].startswith(f"min_repro/{payload['job_id']}/runs/") for bundle in manifest["source_bundles"])

    registry = json.loads((tmp_path / "finding_registry.json").read_text(encoding="utf-8"))
    assert registry["confirmed_findings"][0]["status"] == "reproduced"
    assert registry["confirmed_findings"][0]["reproduction_rate_basis"] == "signature"
    assert registry["confirmed_findings"][0]["signature_reproduction_rate"] == 1.0
    assert registry["confirmed_findings"][0]["type_reproduction_rate"] == 1.0
    assert registry["confirmed_findings"][0]["reproduction_rate_wilson_95"] == [0.4385, 1.0]
    assert registry["audit_hypothesis_cards"][0]["reproduction_rate"] == 1.0
    assert registry["audit_hypothesis_cards"][0]["reproduction_rate_wilson_95"] == [0.4385, 1.0]
    assert registry["audit_hypothesis_cards"][0]["reproduction_rate_basis"] == "signature"
    assert registry["audit_hypothesis_cards"][0]["min_repro"]["reproduction_rate_wilson_95"] == [0.4385, 1.0]
    assert registry["audit_hypothesis_cards"][0]["min_repro"]["signature_reproduction_rate"] == 1.0
    assert registry["audit_hypothesis_cards"]
    assert a14_confirmed_requires_fresh_reproduction(tmp_path).passed


def test_fresh_min_repro_confirmation_rejects_threshold_mismatch(tmp_path: Path) -> None:
    for seed, has_finding in enumerate([True, True, False]):
        _write_min_repro_source(tmp_path, f"s1_P-04_seed{seed}", seed=seed, has_finding=has_finding)
    aggregate_ensemble_triage(tmp_path)

    try:
        execute_fresh_min_repro_confirmation(
            tmp_path,
            finding_type="evidence_gap",
            confirmation_seeds=3,
            seed_start=100,
            min_rate=0.5,
            confirmation_bundle_runner=lambda *_args: None,
        )
    except ValueError as exc:
        assert "pre_registered_confirmation" in str(exc)
    else:
        raise AssertionError("threshold mismatch must be rejected by default")


def test_fresh_min_repro_confirmation_requires_source_signature_match(tmp_path: Path) -> None:
    for seed, has_finding in enumerate([True, False, False]):
        _write_min_repro_source(tmp_path, f"s1_P-04_seed{seed}", seed=seed, has_finding=has_finding, signature="sig-source")
    aggregate_ensemble_triage(tmp_path)

    def write_confirmation_bundle(run_root: Path, seed: int, config: dict[str, Any], finding_type: str) -> None:
        run_root.mkdir(parents=True)
        (run_root / "triage").mkdir()
        (run_root / "meta.json").write_text(
            json.dumps({"stage": config["stage"], "probe": config["probe"], "knobs": {}, "seed": seed, "anchor": False, "live": True, "backend": "deepagents"}),
            encoding="utf-8",
        )
        (run_root / "triage" / "metrics.json").write_text(json.dumps({"finding_types": {finding_type: 1}}), encoding="utf-8")
        (run_root / "triage" / "buckets.json").write_text(
            json.dumps({"buckets": [{"signature": "sig-other", "finding_type": finding_type, "seat_id": "emp-C", "count": 1}]}),
            encoding="utf-8",
        )
        _write_jsonl(run_root / "attempts.jsonl", [{"tick": 1, "seat_id": "emp-C", "tool": "llm_invoke", "args": {"backend": "deepagents"}, "success": True, "result": {}, "origin": "agent"}])
        _write_jsonl(run_root / "basis_records.jsonl", [])
        _write_jsonl(run_root / "world_ledger.jsonl", [])
        _write_jsonl(run_root / "store_events.jsonl", [])

    payload = execute_fresh_min_repro_confirmation(
        tmp_path,
        finding_type="evidence_gap",
        confirmation_seeds=3,
        seed_start=100,
        min_rate=1 / 3,
        confirmation_bundle_runner=write_confirmation_bundle,
    )

    assert payload["status"] == "not_reproduced"
    assert payload["source_bundle_count"] == 0
    assert payload["reproduction_rate_basis"] == "signature"
    assert payload["type_confirmation_successes"] == 3
    assert payload["type_reproduction_rate"] == 1.0
    assert payload["signature_confirmation_successes"] == 0
    assert payload["signature_reproduction_rate"] == 0.0
    manifest = json.loads((tmp_path / payload["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["type_confirmation_successes"] == 3
    assert manifest["type_reproduction_rate"] == 1.0
    assert manifest["signature_confirmation_successes"] == 0
    assert manifest["signature_reproduction_rate"] == 0.0
    assert manifest["reproduction_rate"] == 0.0
    assert manifest["reproduction_rate_basis"] == "signature"
    assert manifest["runs"][0]["finding_count"] == 1
    assert manifest["runs"][0]["matched_signature_finding_count"] == 0
    registry = json.loads((tmp_path / "finding_registry.json").read_text(encoding="utf-8"))
    assert registry["confirmed_findings"] == []


def test_fresh_min_repro_confirmation_gates_on_signature_rate_not_type_rate(tmp_path: Path) -> None:
    for seed, has_finding in enumerate([True, True, False]):
        _write_min_repro_source(tmp_path, f"s1_P-04_seed{seed}", seed=seed, has_finding=has_finding)
    aggregate_ensemble_triage(tmp_path)

    def write_confirmation_bundle(run_root: Path, seed: int, config: dict[str, Any], finding_type: str) -> None:
        signature = "sig-0" if seed in {100, 101} else "sig-other"
        _write_fresh_min_repro_bundle(run_root, seed, config, finding_type, signature=signature)

    payload = execute_fresh_min_repro_confirmation(
        tmp_path,
        finding_type="evidence_gap",
        confirmation_seeds=3,
        seed_start=100,
        min_rate=0.7,
        allow_threshold_override=True,
        confirmation_bundle_runner=write_confirmation_bundle,
    )

    assert payload["status"] == "not_reproduced"
    assert payload["source_bundle_count"] == 2
    assert payload["type_reproduction_rate"] == 1.0
    assert payload["signature_reproduction_rate"] == 2 / 3
    manifest = json.loads((tmp_path / payload["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "not_reproduced"
    assert manifest["type_confirmation_successes"] == 3
    assert manifest["type_reproduction_rate"] == 1.0
    assert manifest["signature_confirmation_successes"] == 2
    assert manifest["signature_reproduction_rate"] == 2 / 3
    assert manifest["reproduction_rate"] == 2 / 3
    assert all(row["finding_count"] == 1 for row in manifest["runs"])
    assert [row["matched_signature_finding_count"] for row in manifest["runs"]] == [1, 1, 0]
    registry = json.loads((tmp_path / "finding_registry.json").read_text(encoding="utf-8"))
    assert registry["confirmed_findings"] == []


def test_s2_fresh_min_repro_confirmation_infers_seats_subset(tmp_path: Path) -> None:
    for seed in range(3):
        _write_min_repro_source(tmp_path, f"s2_seed{seed}", seed=seed, has_finding=True, stage="S2", probe=None, signature="sig-source", seat_id="emp-C")
    aggregate_ensemble_triage(tmp_path)
    observed_configs: list[dict[str, Any]] = []

    def write_confirmation_bundle(run_root: Path, seed: int, config: dict[str, Any], finding_type: str) -> None:
        observed_configs.append(dict(config))
        run_root.mkdir(parents=True)
        (run_root / "triage").mkdir()
        (run_root / "meta.json").write_text(
            json.dumps({"stage": "S2", "probe": None, "knobs": {}, "seed": seed, "anchor": False, "live": True, "backend": "deepagents"}),
            encoding="utf-8",
        )
        (run_root / "triage" / "metrics.json").write_text(json.dumps({"finding_types": {finding_type: 1}}), encoding="utf-8")
        (run_root / "triage" / "buckets.json").write_text(
            json.dumps({"buckets": [{"signature": "sig-source", "finding_type": finding_type, "seat_id": "emp-C", "count": 1}]}),
            encoding="utf-8",
        )
        _write_jsonl(run_root / "attempts.jsonl", [{"tick": 1, "seat_id": "emp-C", "tool": "llm_invoke", "args": {"backend": "deepagents"}, "success": True, "result": {}, "origin": "agent"}])
        _write_jsonl(run_root / "basis_records.jsonl", [])
        _write_jsonl(run_root / "world_ledger.jsonl", [])
        _write_jsonl(run_root / "store_events.jsonl", [])

    payload = execute_fresh_min_repro_confirmation(
        tmp_path,
        finding_type="evidence_gap",
        confirmation_seeds=3,
        seed_start=100,
        min_rate=1.0,
        confirmation_bundle_runner=write_confirmation_bundle,
    )

    assert payload["status"] == "reproduced"
    assert observed_configs[0]["seats_subset"] == ["emp-C"]
    manifest = json.loads((tmp_path / payload["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["seats_subset"] == ["emp-C"]
    assert manifest["reduction_trace"][3]["seats"] == ["emp-C"]


def test_a14_rejects_confirmed_findings_without_fresh_live_reproduction(tmp_path: Path) -> None:
    _write_min_repro_source(tmp_path, "s1_P-04_seed0", seed=0, has_finding=True)
    aggregate_ensemble_triage(tmp_path)
    job = json.loads((tmp_path / "min_repro_jobs.json").read_text(encoding="utf-8"))["jobs"][0]
    manifest_path = tmp_path / "min_repro" / job["job_id"] / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"status": "reproduced", "source_bundles": []}), encoding="utf-8")
    (tmp_path / "finding_registry.json").write_text(
        json.dumps({"confirmed_findings": [{**job, "status": "reproduced", "confirmation_path": f"min_repro/{job['job_id']}/manifest.json"}], "audit_hypothesis_cards": []}),
        encoding="utf-8",
    )

    result = a14_confirmed_requires_fresh_reproduction(tmp_path)

    assert not result.passed
    assert "source bundles missing" in result.detail


def test_a14_accepts_fresh_live_min_repro_bundle_with_disjoint_seed(tmp_path: Path) -> None:
    _write_min_repro_source(tmp_path, "s1_P-04_seed0", seed=0, has_finding=True)
    aggregate_ensemble_triage(tmp_path)
    job = json.loads((tmp_path / "min_repro_jobs.json").read_text(encoding="utf-8"))["jobs"][0]
    live_root = tmp_path / "min_repro" / job["job_id"] / "runs" / "s1_P-04_confirm_seed100"
    live_root.mkdir(parents=True)
    (live_root / "meta.json").write_text(json.dumps({"stage": "S1", "probe": "P-04", "knobs": {}, "seed": 100, "anchor": False, "live": True}), encoding="utf-8")
    _write_jsonl(
        live_root / "attempts.jsonl",
        [{"seat_id": "emp-C", "tool": "llm_invoke", "args": {"backend": "deepagents"}, "success": True, "result": {}, "origin": "agent"}],
    )
    _write_jsonl(live_root / "basis_records.jsonl", [])
    _write_jsonl(live_root / "world_ledger.jsonl", [])
    manifest_path = tmp_path / "min_repro" / job["job_id"] / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "status": "reproduced",
                "threshold": {"min_rate": 1.0, "confirmation_seeds": 3},
                "pre_registered_confirmation": job["pre_registered_confirmation"],
                "threshold_override": {"enabled": False},
                "source_bundles": [{"run_root": f"min_repro/{job['job_id']}/runs/{live_root.name}", "seed": 100}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "finding_registry.json").write_text(
        json.dumps({"confirmed_findings": [{**job, "status": "reproduced", "confirmation_path": f"min_repro/{job['job_id']}/manifest.json"}], "audit_hypothesis_cards": []}),
        encoding="utf-8",
    )

    assert a14_confirmed_requires_fresh_reproduction(tmp_path).passed


def test_a14_rejects_threshold_override_manifest(tmp_path: Path) -> None:
    _write_min_repro_source(tmp_path, "s1_P-04_seed0", seed=0, has_finding=True)
    aggregate_ensemble_triage(tmp_path)
    job = json.loads((tmp_path / "min_repro_jobs.json").read_text(encoding="utf-8"))["jobs"][0]
    live_root = tmp_path / "min_repro" / job["job_id"] / "runs" / "s1_P-04_confirm_seed100"
    live_root.mkdir(parents=True)
    (live_root / "meta.json").write_text(json.dumps({"stage": "S1", "probe": "P-04", "knobs": {}, "seed": 100, "anchor": False, "live": True}), encoding="utf-8")
    _write_jsonl(live_root / "attempts.jsonl", [{"seat_id": "emp-C", "tool": "llm_invoke", "args": {"backend": "deepagents"}, "success": True, "result": {}, "origin": "agent"}])
    _write_jsonl(live_root / "basis_records.jsonl", [])
    _write_jsonl(live_root / "world_ledger.jsonl", [])
    manifest_path = tmp_path / "min_repro" / job["job_id"] / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "status": "reproduced",
                "threshold": {"min_rate": 0.5, "confirmation_seeds": 1},
                "pre_registered_confirmation": job["pre_registered_confirmation"],
                "threshold_override": {"enabled": True},
                "source_bundles": [{"run_root": f"min_repro/{job['job_id']}/runs/{live_root.name}", "seed": 100}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "finding_registry.json").write_text(
        json.dumps({"confirmed_findings": [{**job, "status": "reproduced", "confirmation_path": f"min_repro/{job['job_id']}/manifest.json"}], "audit_hypothesis_cards": []}),
        encoding="utf-8",
    )

    result = a14_confirmed_requires_fresh_reproduction(tmp_path)

    assert not result.passed
    assert "threshold override" in result.detail
