import json
from pathlib import Path

from typer.testing import CliRunner

from company_twin.cli import app
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.harness import run_s1_episode
from company_twin.mutations import (
    apply_corpus_mutations,
    build_delta_one_pair_manifest,
    lint_mutation_catalog,
    mutation_specs_from_values,
)
from company_twin.world_config import build_world_config
from conftest import fake_seat_factory
from test_world_runs import _LateBoundCustomer


def _design_and_corpus() -> tuple[object, Corpus]:
    design = load_design(Path.cwd())
    return design, Corpus.from_design(design)


def test_mutation_catalog_applies_all_wp06_operator_shapes() -> None:
    _, corpus = _design_and_corpus()
    specs = mutation_specs_from_values(
        Path.cwd(),
        [
            "clarify_elderly_understanding_all",
            "clarify_elderly_understanding_sales_only",
            "contradict_chat_approval_recorded",
            "dangling_fill_search_key_stub",
            "role_table_fix_quality_owner",
        ],
    )

    result = apply_corpus_mutations(corpus, specs)

    assert {entry["operator"] for entry in result.applied} == {"clarify", "contradict", "dangling_fill", "role_table_fix"}
    assert len(result.corpus.documents) == len(corpus.documents) + 4
    assert result.before_hash != result.after_hash
    assert result.mutation_hash
    assert result.corpus.get("DFH-SAL-045").text != corpus.get("DFH-SAL-045").text
    assert any(hit.doc_id == "DFH-SAL-901" for hit in result.corpus.search("elderly_understanding", seat_role="audit", top_k=10))
    assert any(hit.doc_id == "DFH-SAL-903" for hit in result.corpus.search("chat_approval_recorded", seat_role="manager", top_k=10))
    assert any(hit.doc_id == "DFH-SAL-904" for hit in result.corpus.search("source_key_reconciliation", seat_role="application", top_k=10))


def test_sales_only_mutation_visibility_is_role_scoped() -> None:
    _, corpus = _design_and_corpus()
    specs = mutation_specs_from_values(Path.cwd(), ["clarify_elderly_understanding_sales_only"])

    result = apply_corpus_mutations(corpus, specs)

    assert result.corpus.readable_by("DFH-SAL-902", "sales") is True
    assert result.corpus.readable_by("DFH-SAL-902", "second_line") is False
    assert any(hit.doc_id == "DFH-SAL-902" for hit in result.corpus.search("sales_understanding_notice", seat_role="sales", top_k=10))
    assert not any(hit.doc_id == "DFH-SAL-902" for hit in result.corpus.search("sales_understanding_notice", seat_role="second_line", top_k=10))


def test_mutation_catalog_visible_text_passes_leak_lint() -> None:
    assert lint_mutation_catalog(Path.cwd()) == []


def test_world_config_records_reconstructable_mutation_hashes() -> None:
    design, corpus = _design_and_corpus()
    specs = mutation_specs_from_values(Path.cwd(), ["dangling_fill_search_key_stub"])
    result = apply_corpus_mutations(corpus, specs)

    config = build_world_config(design, stage="S1", model=None, seed=7, ticks=6, mutations=result.applied)
    corpus_config = config["world"]["corpus"]

    assert corpus_config["mutations"] == result.applied
    assert corpus_config["mutation_count"] == 1
    assert corpus_config["mutation_hash"] == result.mutation_hash
    assert corpus_config["effective_corpus_hash"] != corpus_config["raw_corpus_hash"]
    assert corpus_config["document_count"] == len(design.documents) + 1


def test_run_bundle_config_and_basis_accept_mutated_docs(tmp_path: Path) -> None:
    design, corpus = _design_and_corpus()
    specs = mutation_specs_from_values(Path.cwd(), ["clarify_elderly_understanding_all"])
    mutation_result = apply_corpus_mutations(corpus, specs)
    run_root = tmp_path / "s1_mutated"

    run_s1_episode(
        design=design,
        corpus=mutation_result.corpus,
        probe_id="P-04",
        run_root=run_root,
        seed=0,
        ticks=1,
        seat_factory=fake_seat_factory(),
        customer_llm=_LateBoundCustomer(run_root),
        mutations=mutation_result.applied,
    )

    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    assert config["world"]["corpus"]["mutations"] == mutation_result.applied
    assert config["world"]["corpus"]["mutation_hash"] == mutation_result.mutation_hash


def test_control_pair_manifest_is_delta_one_and_seed_shared(tmp_path: Path) -> None:
    payload = build_delta_one_pair_manifest(root=Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"], seeds=5)

    assert payload["schema_version"] == "company_twin.control_pairs.v1"
    assert payload["pair_count"] == 5
    for idx, pair in enumerate(payload["pairs"]):
        assert pair["delta"] == "world.corpus.mutations"
        assert pair["control"]["mutations"] == []
        assert pair["treatment"]["mutations"] == ["clarify_elderly_understanding_all"]
        assert pair["control"]["seed"] == idx
        assert pair["treatment"]["seed"] == idx
        assert len(set(pair["shared"].values())) == 1

    output = tmp_path / "pairs.json"
    runner = CliRunner()
    result = runner.invoke(app, ["control-pairs", "--mutation", "clarify_elderly_understanding_all", "--k", "5", "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert output.exists()
