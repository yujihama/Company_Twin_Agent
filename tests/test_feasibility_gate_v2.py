"""Feasibility gate v2 (MASTER_DESIGN §17.36, owner approval #18).

The completion requirement moves from per-run all-or-nothing to a
campaign-total threshold plus a per-contrast floor. v1-contract plans must
keep validating and evaluating unchanged.
"""
from __future__ import annotations

import pytest

from company_twin.loss_campaign import (
    LOSS_FEASIBILITY_GATE_SCHEMA_VERSION,
    LOSS_FEASIBILITY_GATE_V2_SCHEMA_VERSION,
    LossCampaignError,
    _evaluate_v2_campaign_criteria,
    _validate_pilot_gate_contract,
)

V1_GATE = {
    "schema_version": LOSS_FEASIBILITY_GATE_SCHEMA_VERSION,
    "effect_estimation": "forbidden_exclude_from_confirmatory",
    "minimum_assigned_endpoint_opportunities_per_run": 1,
    "minimum_r3_opportunities_per_run": 1,
    "maximum_r3_events": 0,
    "require_manipulation_gate": True,
}

V2_GATE = {
    "schema_version": LOSS_FEASIBILITY_GATE_V2_SCHEMA_VERSION,
    "effect_estimation": "forbidden_exclude_from_confirmatory",
    "minimum_assigned_endpoint_opportunities_per_run": 1,
    "campaign_minimum_r3_opportunities": 3,
    "per_contrast_minimum_r3_opportunities": 1,
    "maximum_r3_events": 0,
    "require_manipulation_gate": True,
}


def _row(contrast_id: str, r3: int) -> dict:
    return {"contrast_id": contrast_id, "r3_opportunity_count": r3}


def test_v1_contract_still_validates_exactly() -> None:
    _validate_pilot_gate_contract(V1_GATE)
    broken = {**V1_GATE, "minimum_r3_opportunities_per_run": 2}
    with pytest.raises(LossCampaignError):
        _validate_pilot_gate_contract(broken)


def test_v2_contract_validates_exactly() -> None:
    _validate_pilot_gate_contract(V2_GATE)

    with pytest.raises(LossCampaignError):
        _validate_pilot_gate_contract({**V2_GATE, "campaign_minimum_r3_opportunities": 2})
    with pytest.raises(LossCampaignError):
        _validate_pilot_gate_contract({**V2_GATE, "extra_field": True})
    # v2 must not carry the v1 per-run key
    with pytest.raises(LossCampaignError):
        _validate_pilot_gate_contract({**V2_GATE, "minimum_r3_opportunities_per_run": 1})
    # booleans must not satisfy int-typed fields
    with pytest.raises(LossCampaignError):
        _validate_pilot_gate_contract({**V2_GATE, "per_contrast_minimum_r3_opportunities": True})


def test_v2_criteria_pass_on_total_and_floor() -> None:
    rows = [_row("c1", 2), _row("c1", 0), _row("c2", 1), _row("c2", 0)]
    crit = _evaluate_v2_campaign_criteria(rows, V2_GATE)
    assert crit["campaign_r3_opportunity_total"] == 3
    assert crit["campaign_minimum_met"] is True
    assert crit["per_contrast_r3_opportunities"] == {"c1": 2, "c2": 1}
    assert crit["failed_contrast_ids"] == []


def test_v2_criteria_fail_when_total_short() -> None:
    rows = [_row("c1", 1), _row("c1", 0), _row("c2", 1), _row("c2", 0)]
    crit = _evaluate_v2_campaign_criteria(rows, V2_GATE)
    assert crit["campaign_r3_opportunity_total"] == 2
    assert crit["campaign_minimum_met"] is False
    assert crit["failed_contrast_ids"] == []


def test_v2_criteria_fail_when_a_contrast_is_dry() -> None:
    # total meets the threshold but one contrast has zero completions:
    # the floor must catch it (both mutation families must stay observable)
    rows = [_row("c1", 3), _row("c1", 1), _row("c2", 0), _row("c2", 0)]
    crit = _evaluate_v2_campaign_criteria(rows, V2_GATE)
    assert crit["campaign_minimum_met"] is True
    assert crit["failed_contrast_ids"] == ["c2"]


def test_repilot3_v1_outcome_would_be_judged_identically_under_v1() -> None:
    # Sanity anchor: the v3 re-pilot facts (r3 opportunities [0,0,0,1]) fail
    # the v2 campaign criteria too — the gate change is not retroactive and
    # would not have flipped the recorded no_go anyway.
    rows = [
        _row("clarify", 0),
        _row("clarify", 0),
        _row("contradict", 1),
        _row("contradict", 0),
    ]
    crit = _evaluate_v2_campaign_criteria(rows, V2_GATE)
    assert crit["campaign_minimum_met"] is False
    assert crit["failed_contrast_ids"] == ["clarify"]
