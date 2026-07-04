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


def _write_min_repro_source(campaign_root: Path, name: str, *, seed: int, has_finding: bool) -> None:
    run_root = campaign_root / name
    (run_root / "triage").mkdir(parents=True)
    (run_root / "meta.json").write_text(json.dumps({"stage": "S1", "probe": "P-04", "knobs": {}, "seed": seed, "anchor": False}), encoding="utf-8")
    (run_root / "triage" / "metrics.json").write_text(
        json.dumps({"controlled_actions_agent": 1, "finding_types": ({"evidence_gap": 1} if has_finding else {})}),
        encoding="utf-8",
    )
    buckets = [
        {
            "signature": f"sig-{seed}",
            "count": 1,
            "opportunity_denominator": 1,
            "rate": 1.0,
            "finding_type": "evidence_gap",
            "seat_id": "emp-C",
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


def test_min_repro_collation_never_promotes_confirmed_findings(tmp_path: Path) -> None:
    for seed, has_finding in enumerate([True, True, False]):
        _write_min_repro_source(tmp_path, f"s1_P-04_seed{seed}", seed=seed, has_finding=has_finding)

    queued = aggregate_ensemble_triage(tmp_path)
    assert queued["min_repro_jobs"][0]["status"] == "pending"
    assert queued["finding_registry"]["confirmed_findings"] == []

    executed = execute_min_repro_jobs(tmp_path, min_rate=0.5, min_seeds=3)

    job = executed["jobs"][0]
    assert job["status"] == "evidence_collated"
    assert job["source_bundle_count"] == 2
    assert abs(job["evidence_rate"] - 2 / 3) < 1e-9
    manifest = json.loads((tmp_path / job["confirmation_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "evidence_collated"
    assert manifest["reduction_trace"][1]["step"] == "deck_one_card"

    registry = json.loads((tmp_path / "finding_registry.json").read_text(encoding="utf-8"))
    assert registry["confirmed_findings"] == []
    assert registry["audit_hypothesis_cards"] == []
    updated_jobs = json.loads((tmp_path / "min_repro_jobs.json").read_text(encoding="utf-8"))["jobs"]
    assert updated_jobs[0]["status"] == "evidence_collated"
    assert a14_confirmed_requires_fresh_reproduction(tmp_path).passed


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
                            "signature": f"{finding_type}:confirm:{seed}",
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
                {"tick": 1, "seat_id": "emp-C", "tool": "llm_response", "args": {"backend": "deepagents"}, "success": True, "result": {}, "origin": "agent"},
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
        min_rate=0.5,
        confirmation_bundle_runner=write_confirmation_bundle,
    )

    assert payload["status"] == "reproduced"
    assert payload["confirmed_count"] == 1
    assert payload["source_bundle_count"] == 3
    manifest = json.loads((tmp_path / payload["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "reproduced"
    assert manifest["fresh_seeds"] == [100, 101, 102]
    assert all(bundle["run_root"].startswith(f"min_repro/{payload['job_id']}/runs/") for bundle in manifest["source_bundles"])

    registry = json.loads((tmp_path / "finding_registry.json").read_text(encoding="utf-8"))
    assert registry["confirmed_findings"][0]["status"] == "reproduced"
    assert registry["audit_hypothesis_cards"]
    assert a14_confirmed_requires_fresh_reproduction(tmp_path).passed


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
        [{"seat_id": "emp-C", "tool": "llm_response", "args": {"backend": "deepagents"}, "success": True, "result": {}, "origin": "agent"}],
    )
    _write_jsonl(live_root / "basis_records.jsonl", [])
    _write_jsonl(live_root / "world_ledger.jsonl", [])
    manifest_path = tmp_path / "min_repro" / job["job_id"] / "manifest.json"
    manifest_path.write_text(
        json.dumps({"status": "reproduced", "source_bundles": [{"run_root": f"min_repro/{job['job_id']}/runs/{live_root.name}", "seed": 100}]}),
        encoding="utf-8",
    )
    (tmp_path / "finding_registry.json").write_text(
        json.dumps({"confirmed_findings": [{**job, "status": "reproduced", "confirmation_path": f"min_repro/{job['job_id']}/manifest.json"}], "audit_hypothesis_cards": []}),
        encoding="utf-8",
    )

    assert a14_confirmed_requires_fresh_reproduction(tmp_path).passed
