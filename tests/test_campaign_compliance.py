import json
from collections import Counter
from pathlib import Path

from company_twin.campaign import (
    WORLD_PROMPT_BANNED_TERMS,
    aggregate_s0_divergence,
    build_s0_matrix,
    classify_answer,
    entropy,
    static_world_surface_lint,
    _diverse_s0_rows,
    _promote_probe,
)
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design


def test_static_world_surface_lint_passes() -> None:
    design = load_design(Path.cwd())
    result = static_world_surface_lint(design)
    assert result["passed"] is True, result["failures"]


def test_s0_matrix_covers_roles_spans_and_variants() -> None:
    design = load_design(Path.cwd())
    matrix = build_s0_matrix(design, models=["openrouter:qwen/qwen3.6-flash"], variants=2)

    assert len(matrix) == 210  # 182 + P-05/P-10 bind repair (undefined spans replaced by STR-02r/STR-01)
    assert {row.variant for row in matrix} == {0, 1}
    assert {"emp-A", "emp-M", "emp-Q"}.issubset({row.seat_id for row in matrix})
    assert {"AMB-02", "AMB-04d", "CONTRA-01"}.issubset({row.span_id for row in matrix})


def test_contra01_registers_split_by_topic_candidate_for_next_batch() -> None:
    design = load_design(Path.cwd())

    assert design.spans["CONTRA-01"].candidates == {
        "split_by_topic": "商品説明事項の改定依頼と停止条件該当の一報を論点別に分割し、それぞれ別の主管部署へ接地して送る"
    }


def test_s0_limit_selects_one_probe_span_seat_cell() -> None:
    design = load_design(Path.cwd())
    matrix = build_s0_matrix(design, models=["openrouter:qwen/qwen3.6-flash", "openrouter:qwen/qwen3.5-9b"], variants=2)
    selected = _diverse_s0_rows(matrix, budget=4)

    assert len(selected) == 4
    assert len({(row.probe_id, row.span_id, row.seat_id) for row in selected}) == 1
    assert len({row.model for row in selected}) == 2
    assert {row.variant for row in selected} == {0, 1}


def test_classify_answer_uses_span_candidates_then_heuristics() -> None:
    candidates = {"C1": "要承認は管理者で完結する", "C2": "第二線への事前相談を要する"}
    assert classify_answer("文書からは第二線への事前相談が必要と読める", candidates) == "C2"
    assert classify_answer("管理者の確認で足りる", candidates) == "C1"
    assert classify_answer("録音と同意ログを先に揃える", {}) == "evidence_first"
    assert classify_answer("全く別の新しい読み", {}) == "novel_or_unclassified"


def test_entropy_is_cluster_derived() -> None:
    assert entropy(Counter()) == 0.0
    assert entropy(Counter({"a": 4})) == 0.0
    assert abs(entropy(Counter({"a": 2, "b": 2})) - 1.0) < 1e-9


def test_aggregate_s0_divergence_builds_span_role_cells(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    rows = []
    for idx, (seat, reading) in enumerate([("emp-A", "管理者の確認で進める"), ("emp-A", "第二線へ相談する"), ("emp-M", "管理者が確認する")]):
        run_root = tmp_path / f"s0_{idx}"
        run_root.mkdir()
        (run_root / "meta.json").write_text(json.dumps({"live": True}), encoding="utf-8")
        rows.append(
            {
                "probe_id": "P-03",
                "span_id": "AMB-01",
                "seat_id": seat,
                "model": "m",
                "variant": idx % 2,
                "run_root": str(run_root),
                "response": reading,
                "likely_reading": reading,
                "required_approver_or_evidence": "",
                "next_action": "",
            }
        )
    payload = aggregate_s0_divergence(design, rows, campaign_root=tmp_path)

    assert payload["all_answers_live"] is True
    cells = {(cell["span_id"], cell["role"]): cell for cell in payload["cells"]}
    sales_cell = cells[("AMB-01", "sales")]
    assert sales_cell["answers"] == 2
    assert sales_cell["primary_probe_id"] == "P-03"
    assert sales_cell["entropy"] > 0  # two different readings must register as divergence
    promoted = _promote_probe(payload)
    assert promoted is not None
    assert promoted["probe_id"] == "P-03"
    assert promoted["reason"] in {"max_entropy", "novel_or_unclassified"}
    assert (tmp_path / "s0_divergence.json").exists()


def test_s0_divergence_marks_machine_novel_for_human_review(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    run_root = tmp_path / "s0"
    run_root.mkdir()
    (run_root / "meta.json").write_text(json.dumps({"live": True}), encoding="utf-8")
    payload = aggregate_s0_divergence(
        design,
        [
            {
                "probe_id": "P-03",
                "span_id": "AMB-01",
                "seat_id": "emp-A",
                "model": "m1",
                "variant": 0,
                "run_root": str(run_root),
                "response": "蜈ｨ縺丞挨縺ｮ譁ｰ縺励＞隱ｭ縺ｿ",
                "parsed": True,
            },
            {
                "probe_id": "P-03",
                "span_id": "AMB-01",
                "seat_id": "emp-A",
                "model": "m2",
                "variant": 1,
                "run_root": str(run_root),
                "response": "蜈ｨ縺丞挨縺ｮ譁ｰ縺励＞隱ｭ縺ｿ",
                "parsed": True,
            },
        ],
        campaign_root=tmp_path,
    )

    cell = payload["cells"][0]
    assert cell["novel_count"] == 2
    assert cell["human_confirmed_class"] is None
    assert payload["human_review_queue"][0]["primary_probe_id"] == "P-03"


def test_s0_divergence_separates_unparsed_from_novel_queue(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    run_root = tmp_path / "s0"
    run_root.mkdir()
    (run_root / "meta.json").write_text(json.dumps({"live": True}), encoding="utf-8")

    payload = aggregate_s0_divergence(
        design,
        [
            {
                "probe_id": "P-03",
                "span_id": "AMB-01",
                "seat_id": "emp-A",
                "model": "m1",
                "variant": 0,
                "run_root": str(run_root),
                "response": "not json",
                "parsed": False,
            },
            {
                "probe_id": "P-03",
                "span_id": "AMB-01",
                "seat_id": "emp-A",
                "model": "m2",
                "variant": 1,
                "run_root": str(run_root),
                "response": "",
                "parsed": False,
            },
        ],
        campaign_root=tmp_path,
    )

    cell = payload["cells"][0]
    assert cell["clusters"]["unparsed"] == 2
    assert cell["novel_count"] == 0
    assert payload["human_review_queue"] == []


def test_s0_entropy_excludes_unparsed_but_keeps_no_grounded(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    run_root = tmp_path / "s0"
    run_root.mkdir()
    (run_root / "meta.json").write_text(json.dumps({"live": True}), encoding="utf-8")

    payload = aggregate_s0_divergence(
        design,
        [
            {
                "probe_id": "P-10",
                "span_id": "STR-01",
                "seat_id": "emp-A",
                "model": "m1",
                "variant": 0,
                "run_root": str(run_root),
                "response": "{}",
                "parsed": True,
                "likely_reading": "\u7ba1\u7406\u8005\u306e\u78ba\u8a8d\u3067\u8db3\u308a\u308b",
            },
            {
                "probe_id": "P-10",
                "span_id": "STR-01",
                "seat_id": "emp-A",
                "model": "m2",
                "variant": 1,
                "run_root": str(run_root),
                "response": "",
                "parsed": False,
                "outcome": "recursion_exhausted",
            },
            {
                "probe_id": "P-10",
                "span_id": "STR-01",
                "seat_id": "emp-A",
                "model": "m3",
                "variant": 2,
                "run_root": str(run_root),
                "response": "",
                "parsed": False,
            },
        ],
        campaign_root=tmp_path,
    )

    cell = payload["cells"][0]
    assert cell["clusters"] == {"manager_route": 1, "no_grounded_answer": 1, "unparsed": 1}
    assert cell["entropy_clusters"] == {"manager_route": 1, "no_grounded_answer": 1}
    assert cell["entropy_excluded_clusters"] == {"unparsed": 1}
    assert cell["entropy"] == 1.0


def test_role_cards_do_not_copy_corpus_text() -> None:
    """Role cards may describe habits and tensions but must not smuggle normative
    document text into the seat prompt (MASTER_DESIGN P5)."""
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    corpus_text = "".join(doc.text for doc in corpus.documents.values())
    cards_dir = Path.cwd() / "data" / "design" / "role_cards"
    window = 20
    for card_path in sorted(cards_dir.glob("*.md")):
        text = "".join(card_path.read_text(encoding="utf-8").split())
        overlaps = []
        for start in range(0, max(len(text) - window, 0), 5):
            chunk = text[start : start + window]
            if len(chunk) == window and chunk in corpus_text:
                overlaps.append(chunk)
        assert not overlaps, f"{card_path.name} copies corpus text: {overlaps[:3]}"


def test_banned_terms_cover_probe_vocabulary() -> None:
    lowered = {term.lower() for term in WORLD_PROMPT_BANNED_TERMS}
    assert "probe" in lowered and "プローブ" in WORLD_PROMPT_BANNED_TERMS
