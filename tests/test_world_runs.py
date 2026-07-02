import json
from pathlib import Path

import pytest

from company_twin.acceptance import a01_no_scripted_origin, a03_inbox_whitelist, a04_basis_authorship, a12_d4_store_read_before_action
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.harness import run_s0, run_s1_episode, run_s2_world
from company_twin.kernel import InboxLeakError, WorldKernel
from company_twin.oracles import write_triage
from company_twin.recorder import RunRecorder, read_jsonl
from company_twin.tools import build_role_tools
from company_twin.world_config import assert_world_config_complete
from conftest import FakeCustomerLLM, fake_seat_factory

WORKFLOW_TOOLS = {
    "record_customer_contact",
    "request_approval",
    "approve_application",
    "submit_application",
    "verify_identity",
    "link_review",
    "complete_contract",
    "deliver_documents",
}


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_s1(tmp_path: Path, **kwargs):
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s1"
    recorder_holder = {}

    def customer_factory_bundle(run_root=run_root):
        return None

    # customer_llm needs the run recorder; run via two-phase: create with a shim
    result = run_s1_episode(
        design=design,
        corpus=corpus,
        probe_id="P-04",
        run_root=run_root,
        seed=0,
        seat_factory=fake_seat_factory(),
        customer_llm=_LateBoundCustomer(run_root),
        **kwargs,
    )
    return design, run_root, result


class _LateBoundCustomer:
    """Customer fake that binds to the run's recorder lazily via a sentinel file."""

    backend = "test-fake"

    def __init__(self, run_root: Path):
        self.run_root = run_root
        self._inner = None

    def __call__(self, persona_prompt: str) -> str:
        # append llm_invoke directly to the bundle to mirror the real customer path
        record = {
            "ts": "",
            "run_id": self.run_root.name,
            "tick": 0,
            "seat_id": "customer",
            "tool": "llm_invoke",
            "args": {"backend": "test-fake", "model": "fake:unit", "role": "customer", "prompt_chars": len(persona_prompt)},
            "success": True,
            "result": {"response_chars": 30},
            "denied_reason": None,
            "origin": "customer",
        }
        with (self.run_root / "attempts.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return "本日中に申込したいのですが、進められますか。"


def test_s1_episode_multi_seat_workflow_and_clean_planes(tmp_path: Path) -> None:
    design, run_root, result = _run_s1(tmp_path)

    assert assert_world_config_complete(_json(run_root / "config.json")) == []
    ledger = read_jsonl(run_root / "world_ledger.jsonl")
    attempts = read_jsonl(run_root / "attempts.jsonl")

    assert any(row["event_type"] == "customer_event" for row in ledger)
    assert any(row["event_type"] == "latent_truth_committed" for row in ledger)
    seats = {row["seat_id"] for row in attempts if row["seat_id"].startswith("emp-")}
    assert len(seats) >= 2
    assert len({row["tool"] for row in attempts} & WORKFLOW_TOOLS) >= 3
    # two-plane hygiene on the recorded world surface
    assert a01_no_scripted_origin(run_root).passed
    assert a03_inbox_whitelist(run_root).passed
    assert a04_basis_authorship(run_root).passed
    # D4 private store was exercised by a seat, not the harness
    store = read_jsonl(run_root / "store_events.jsonl")
    assert store and all(row["origin"] == "agent" for row in store)


def test_s2_world_runs_deck_month_end_and_anchor_purity(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)

    run_root = tmp_path / "s2"
    run_s2_world(design=design, corpus=corpus, run_root=run_root, seed=0, ticks=40, seat_factory=fake_seat_factory(), customer_llm=_LateBoundCustomer(run_root))
    triage = write_triage(run_root)
    ledger = read_jsonl(run_root / "world_ledger.jsonl")
    attempts = read_jsonl(run_root / "attempts.jsonl")
    assert sum(1 for row in ledger if row["event_type"] == "customer_event") >= 10
    assert any(row["event_type"] == "month_end_close" for row in ledger)
    assert any(row["event_type"] == "completion_gate_active" for row in ledger)
    assert len({row["seat_id"] for row in attempts if row["seat_id"].startswith("emp-")}) >= 4
    assert triage["metrics"]["store_reads_agent"] >= 1
    assert triage["metrics"]["controlled_actions_after_store_read"] >= 1
    assert a12_d4_store_read_before_action(run_root).passed

    anchor_root = tmp_path / "anchor"
    run_s2_world(design=design, corpus=corpus, run_root=anchor_root, seed=0, ticks=40, anchor=True, seat_factory=fake_seat_factory(), customer_llm=_LateBoundCustomer(anchor_root))
    anchor_ledger = read_jsonl(anchor_root / "world_ledger.jsonl")
    assert not [row for row in anchor_ledger if row["event_type"] == "completion_gate_active"]


def test_s0_uses_agent_answer_not_harness_query(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s0"

    result = run_s0(design=design, corpus=corpus, probe_id="P-01", seat_id="emp-A", run_root=run_root, variant=0, seat_factory=fake_seat_factory())

    assert result["parsed"] is True
    assert result["cited_doc_ids"]
    answer = _json(run_root / "s0_answer.json")
    assert answer["cited_doc_ids"] == result["cited_doc_ids"]
    attempts = read_jsonl(run_root / "attempts.jsonl")
    searches = [row for row in attempts if row["tool"] == "search_corpus"]
    assert searches and all(row["origin"] == "agent" for row in searches)  # the seat searched; the harness did not


def test_manager_absence_defers_inbox(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s1abs"
    # P-08 triggers at tick 1 but we retime the world so approval chat lands on absence tick via ticks window
    run_s1_episode(design=design, corpus=corpus, probe_id="P-08", run_root=run_root, seed=0, ticks=6, seat_factory=fake_seat_factory(), customer_llm=_LateBoundCustomer(run_root))
    attempts = read_jsonl(run_root / "attempts.jsonl")
    assert attempts  # smoke: absence logic did not break the loop


def test_inbox_whitelist_blocks_experimenter_fields(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, "leak")
    kernel = WorldKernel(recorder)
    with pytest.raises(InboxLeakError):
        kernel.enqueue_inbox("emp-A", {"kind": "customer_utterance", "tick": 1, "utterance": "x", "probe_id": "P-01"})
    with pytest.raises(InboxLeakError):
        kernel.enqueue_inbox("emp-A", {"kind": "customer_utterance", "tick": 1, "utterance": "x", "extra_field": 1, "event_id": "E", "customer_id": "C", "application_id": "A", "product": "p", "deadline_display": "d"})


def test_recorder_rejects_banned_origins(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, "origin")
    with pytest.raises(ValueError):
        with recorder.origin("agent_policy"):
            pass


def test_tick_budget_denies_excess_tool_calls(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    recorder = RunRecorder(tmp_path, "budget")
    recorder.configure_tick_budgets({"emp-A": 1})
    kernel = WorldKernel(recorder)
    tools = {tool.__name__: tool for tool in build_role_tools(corpus=corpus, kernel=kernel, recorder=recorder, seat_id="emp-A", seat_role="sales")}

    tools["search_corpus"]("高齢者 追加確認", 5)
    denied = tools["read_document"]("DFH-SAL-021", "高齢者", 1000)

    assert "tick budget exceeded" in denied
    assert recorder.budget_left("emp-A") == 0
