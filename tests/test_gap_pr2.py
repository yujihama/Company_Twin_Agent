import json
from pathlib import Path
from typing import Any

from company_twin.acceptance import a03_inbox_whitelist
from company_twin.campaign import default_s0_models
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.harness import run_s1_episode, run_s2_world
from company_twin.oracles import aggregate_ensemble_triage, write_triage
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


def test_detection_miss_rate_and_coverage_map_are_written(tmp_path: Path) -> None:
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
    assert metrics["detection_miss_rate"]["R-EVIDENCE-GAP"]["opportunity_count"] == 1
    assert metrics["detection_miss_rate"]["R-EVIDENCE-GAP"]["hit_count"] == 1
    assert metrics["detection_miss_rate"]["R-EVIDENCE-GAP"]["miss_rate"] == 0

    ensemble = aggregate_ensemble_triage(tmp_path)
    coverage = json.loads((tmp_path / "coverage_map.json").read_text(encoding="utf-8"))
    assert coverage["schema_version"] == "company_twin.coverage_map.v1"
    assert coverage["cell_counts"]["C5_evidence_skeleton"] >= 1
    assert ensemble["coverage_map"]["path"] == "coverage_map.json"
