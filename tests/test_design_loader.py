from pathlib import Path

from company_twin.design_loader import load_design


def test_load_design_extracts_core_inputs() -> None:
    design = load_design(Path.cwd())

    assert len(design.documents) == 50
    assert "DFH-SAL-001" in design.documents
    assert "AMB-02" in design.spans
    assert design.probes["P-01"].binds[:2] == ("AMB-02", "AMB-08")
    assert "18:50" in design.probes["P-04"].title
    assert "emp-A" in design.seats
