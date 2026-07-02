from pathlib import Path

from company_twin.agents import role_system_prompt
from company_twin.campaign import WORLD_PROMPT_BANNED_TERMS
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.harness import run_s1_episode, run_s2_world
from company_twin.recorder import read_jsonl
from company_twin.world_config import assert_world_config_complete


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


def test_s1_episode_has_customer_event_multi_seat_and_workflow(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s1"

    run_s1_episode(design=design, corpus=corpus, probe_id="P-04", seat_id="emp-A", run_root=run_root, live=False, seed=0)

    assert assert_world_config_complete(_json(run_root / "config.json")) == []
    ledger = read_jsonl(run_root / "world_ledger.jsonl")
    attempts = read_jsonl(run_root / "attempts.jsonl")
    tools = {row["tool"] for row in attempts}
    seats = {row["seat_id"] for row in attempts}

    assert any(row["event_type"] == "customer_event" for row in ledger)
    assert len(seats) >= 2
    assert len(tools & WORKFLOW_TOOLS) >= 3


def test_s2_world_has_deck_events_all_seat_activity_and_month_end(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    run_root = tmp_path / "s2"

    run_s2_world(design=design, corpus=corpus, run_root=run_root, live=False, seed=0)

    config = _json(run_root / "config.json")
    assert assert_world_config_complete(config) == []
    assert config["world"]["schedule"]["ticks"] == 40
    ledger = read_jsonl(run_root / "world_ledger.jsonl")
    attempts = read_jsonl(run_root / "attempts.jsonl")

    assert sum(1 for row in ledger if row["event_type"] == "customer_event") >= 10
    assert any(row["event_type"] == "month_end_close" for row in ledger)
    assert len({row["seat_id"] for row in attempts}) >= 4
    assert len({row["tool"] for row in attempts} & WORKFLOW_TOOLS) >= 5


def test_world_prompt_has_no_outside_plane_terms() -> None:
    prompt = role_system_prompt("emp-A", "sales")
    lower = prompt.lower()

    assert not [term for term in WORLD_PROMPT_BANNED_TERMS if term.lower() in lower]


def test_retrieval_audit_has_role_specific_expected_hits() -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)

    audit = corpus.audit_retrieval()

    assert audit["passed"] is True
    assert "DFH-SAL-021" in audit["sales_elderly_top_ids"]


def _json(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))
