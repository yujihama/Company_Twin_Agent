"""Answer check (roadmap 1-5): planted defects vs found holes. Zero spend:
the verifier is scripted, so the prompt discipline, the fail-closed storage
and the scoring document are all verified for free."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from company_twin.answer_check import (
    PLANTED_DEFECTS,
    answer_check_prompt,
    assemble_answer_check,
    defect_by_id,
    record_answer_check,
    render_answer_check_md,
)


def test_the_twelve_planted_defects_match_the_design_map() -> None:
    assert len(PLANTED_DEFECTS) == 12
    assert {d["defect_id"] for d in PLANTED_DEFECTS} == {
        "AMB-01", "AMB-02", "AMB-04d", "AMB-08", "AMB-09", "AMB-10",
        "AMB-11", "AMB-12", "CONTRA-01", "SCC-01", "STR-01", "STR-02r",
    }
    with pytest.raises(KeyError):
        defect_by_id("AMB-99")


def test_prompt_shows_the_defect_and_the_holes_document_only() -> None:
    prompt = answer_check_prompt("AMB-02", "## 素通りした経路\n- 高齢の顧客の申込が…")
    assert "高齢のお客様の年齢の定義" in prompt
    assert "素通りした経路" in prompt
    assert "not_rediscovered" in prompt and "資料ここから" in prompt
    # fail-closed instruction: doubt resolves to not-rediscovered
    assert "迷う場合" in prompt


def test_storage_is_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        record_answer_check(tmp_path, {"defect_id": "AMB-99", "verdict": "rediscovered", "rationale": "x"})
    with pytest.raises(ValueError):
        record_answer_check(tmp_path, {"defect_id": "AMB-02", "verdict": "banana", "rationale": "x"})
    with pytest.raises(ValueError):
        record_answer_check(tmp_path, {"defect_id": "AMB-02", "verdict": "rediscovered", "rationale": " "})
    # a positive claim must point at its evidence
    with pytest.raises(ValueError):
        record_answer_check(
            tmp_path, {"defect_id": "AMB-02", "verdict": "rediscovered", "rationale": "ある", "matching_paths": [], "evidence": []}
        )
    stored = record_answer_check(
        tmp_path,
        {"defect_id": "AMB-02", "verdict": "rediscovered", "rationale": "高齢定義の不在を通り道にした経路がある。",
         "matching_paths": ["p7-d0b01"], "evidence": ["世界 p7-d0b01: 高齢の顧客の契約が当日完了"]},
    )
    assert stored["judge"] == "subagent"
    assert (tmp_path / "answer_check_AMB-02.json").exists()


def test_summary_counts_unjudged_defects_on_neither_side(tmp_path: Path) -> None:
    record_answer_check(
        tmp_path,
        {"defect_id": "AMB-02", "verdict": "rediscovered", "rationale": "r",
         "matching_paths": ["w1"], "evidence": ["q"]},
    )
    record_answer_check(tmp_path, {"defect_id": "STR-01", "verdict": "not_rediscovered", "rationale": "資料に該当なし。"})
    summary = assemble_answer_check(tmp_path)
    assert summary["defect_count"] == 12
    assert summary["counts"] == {"rediscovered": 1, "partially_rediscovered": 0, "not_rediscovered": 1}
    assert len(summary["unjudged"]) == 10
    text = render_answer_check_md(summary)
    assert "再発見 1" in text and "未照合" in text
    assert "AMB-02" in text and "高齢のお客様の年齢の定義" in text
    # verify the stored file round-trips
    on_disk = json.loads((tmp_path / "answer_check_STR-01.json").read_text(encoding="utf-8"))
    assert on_disk["verdict"] == "not_rediscovered"
