from __future__ import annotations

import json
from pathlib import Path

from company_twin.option_enumeration import ELICITATION_VARIANTS


def _plan() -> dict:
    root = Path(__file__).resolve().parents[1]
    path = root / "docs" / "progress" / "option_enum_first_measurement_plan_20260727.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_first_enumeration_plan_is_sealed_and_internally_consistent() -> None:
    plan = _plan()
    assert plan["kind"] == "pre_execution_sealed_plan"
    # sealed scale must be arithmetically consistent
    runs = plan["target_runs"]
    assert runs["run_count"] == 20 == len(runs["ledger_sha256"])
    assert set(plan["probes"]) == {"P-11", "P-04"}
    assert plan["total_samples_stage_a"] == runs["run_count"] * len(plan["probes"]) * plan["n_samples_per_run_per_probe"]
    # every sealed bundle hash is a lowercase sha256
    for name, digest in runs["ledger_sha256"].items():
        assert len(digest) == 64 and digest == digest.lower(), name


def test_first_enumeration_plan_two_stage_design_matches_the_instrument() -> None:
    plan = _plan()
    elicitation = plan["elicitation"]
    # both sealed variants must exist in the instrument with the sealed claim
    # levels, so the plan cannot reference wording the code does not carry
    for stage in ("stage_a", "stage_b"):
        variant = elicitation[stage]["variant"]
        assert variant in ELICITATION_VARIANTS, variant
        assert ELICITATION_VARIANTS[variant]["claim_level"] == elicitation[stage]["claim_level"]
    # stage B is conditional and never pooled with stage A
    assert "ONLY if" in elicitation["stage_b"]["trigger"]
    assert "never pooled" in elicitation["stage_b"]["pooling_prohibition"]
    # Layer 3 stays explicitly deferred with the recorded mechanical reason
    layer3 = plan["layer3_disposition"]
    assert layer3["status"] == "deferred_with_recorded_reason"
    assert "tick" in layer3["mechanical_fact"]
