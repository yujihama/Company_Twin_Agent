from pathlib import Path

from company_twin.campaign import WORLD_PROMPT_BANNED_TERMS, build_s0_matrix, check_design_compliance
from company_twin.design_loader import load_design


def test_design_compliance_static_checks_pass() -> None:
    design = load_design(Path.cwd())
    result = check_design_compliance(campaign_root=None, design=design, run_roots=[])

    assert result["passed"] is True
    assert result["s0_matrix_rows"] > 0


def test_s0_matrix_covers_roles_spans_and_variants() -> None:
    design = load_design(Path.cwd())
    matrix = build_s0_matrix(design, models=["openrouter:qwen/qwen3.6-flash"], variants=2)

    assert len(matrix) == 182
    assert {row.variant for row in matrix} == {0, 1}
    assert {"emp-A", "emp-M", "emp-Q"}.issubset({row.seat_id for row in matrix})
    assert {"AMB-02", "AMB-04d", "CONTRA-01"}.issubset({row.span_id for row in matrix})
