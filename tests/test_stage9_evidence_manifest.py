"""Tests for the Stage 9 evidence manifest and two-level readiness split
(expert-review hardening pass, MASTER_DESIGN.md section 12/17.3).

All fixtures here are offline: no LLM/API call is made anywhere in this file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from company_twin.backcasting import extract_backcasting_cases, write_backcasting_inputs
from company_twin.backcasting_run import BACKCASTING_RESULTS_SCHEMA_VERSION, JUDGE_PROMPT_VERSION, select_backcasting_sample
from company_twin.cli import app
from company_twin.design_loader import load_design
from company_twin.evidence_manifest import (
    EVIDENCE_MANIFEST_FILENAME,
    EVIDENCE_MANIFEST_SCHEMA_VERSION,
    build_stage9_evidence_manifest,
    check_manifest_consistency,
    write_stage9_evidence_manifest,
)
from company_twin.holdout import build_holdout_injection_plan, write_holdout_inputs, write_holdout_report
from company_twin.readiness import build_external_claim_readiness_summary, run_readiness_gate
from company_twin.sme_blind_review import build_blind_review_packet, write_sme_blind_review_inputs, write_sme_blind_review_report


def _design():
    return load_design(Path.cwd())


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _fixture_run_bundle(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _write_jsonl(
        root / "chat_channel.jsonl",
        [
            {"body": "本日の申込、意向把握のメモが未記入なので確認してもらえますか。"},
            {"body": "承知しました、確認して午後に折り返します。"},
            {"body": "顧客への再説明は完了しました、記録も残しています。"},
            {"body": "解約希望のお客様には手続き案内を送付済みです。"},
            {"body": "月次の締め処理は問題なく完了しました。"},
        ],
    )
    _write_jsonl(root / "world_ledger.jsonl", [{"event_type": "month_end_close", "payload": {}}])
    (root / "attempts.jsonl").write_text("", encoding="utf-8")
    return root


def _write_backcasting_evidence(tmp_path: Path, *, judge_backend: str = "openrouter", sample_seed: int = 0) -> None:
    design = _design()
    extraction = extract_backcasting_cases(design)
    write_backcasting_inputs(tmp_path, extraction)
    sample = select_backcasting_sample(extraction["cases"], sample_size=5, sample_seed=sample_seed)
    case_ids = sample["selected_case_ids"]
    (tmp_path / "backcasting_resimulation_results.json").write_text(
        json.dumps(
            {
                "schema_version": BACKCASTING_RESULTS_SCHEMA_VERSION,
                "sample": sample,
                "judge": {"backend": judge_backend, "model": "fake-model", "prompt_version": JUDGE_PROMPT_VERSION, "readiness_eligible": judge_backend == "openrouter"},
                "results": [{"case_id": cid, "reproduced": True, "viewed_doc_ids": ["DFH-SAL-021"]} for cid in case_ids],
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# build_stage9_evidence_manifest
# ---------------------------------------------------------------------------


def test_build_manifest_records_git_commit_and_command_line(tmp_path: Path) -> None:
    manifest = build_stage9_evidence_manifest(tmp_path, command_line=["python", "-m", "company_twin.cli", "stage9-evidence-manifest"])

    assert manifest["schema_version"] == EVIDENCE_MANIFEST_SCHEMA_VERSION
    assert manifest["campaign_root"] == str(tmp_path.resolve())
    assert manifest["command_line"] == ["python", "-m", "company_twin.cli", "stage9-evidence-manifest"]
    # git_commit should be a real hex sha in this repo checkout, never empty/crash.
    assert manifest["git_commit"] and manifest["git_commit"] != ""


def test_build_manifest_records_absent_evidence_honestly(tmp_path: Path) -> None:
    manifest = build_stage9_evidence_manifest(tmp_path)

    assert manifest["evidence"]["backcasting"]["present"] is False
    assert manifest["evidence"]["sme_blind_review"]["present"] is False
    assert manifest["evidence"]["holdout"]["present"] is False
    assert manifest["evidence"]["g3_semantic_grounding"]["present"] is False


def test_build_manifest_binds_backcasting_provenance(tmp_path: Path) -> None:
    _write_backcasting_evidence(tmp_path)

    manifest = build_stage9_evidence_manifest(tmp_path)

    backcasting = manifest["evidence"]["backcasting"]
    assert backcasting["present"] is True
    assert backcasting["sample_size"] == 5
    assert backcasting["sample_seed"] == 0
    assert backcasting["judge"]["backend"] == "openrouter"
    assert backcasting["judge"]["prompt_version"] == JUDGE_PROMPT_VERSION
    assert backcasting["selected_case_ids_sha256"]


def test_build_manifest_binds_sme_provenance(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet, id_map = build_blind_review_packet([run_root], reviewer_type="human_sme")
    write_sme_blind_review_inputs(tmp_path, packet, id_map)

    manifest = build_stage9_evidence_manifest(tmp_path)

    sme = manifest["evidence"]["sme_blind_review"]
    assert sme["present"] is True
    assert sme["packet_hash"] == packet["packet_hash"]
    assert sme["reviewer_type"] == "human_sme"
    assert sme["dropped_count"] == 0


def test_build_manifest_binds_holdout_provenance(tmp_path: Path) -> None:
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"])
    write_holdout_inputs(tmp_path, plan)

    manifest = build_stage9_evidence_manifest(tmp_path)

    holdout = manifest["evidence"]["holdout"]
    assert holdout["present"] is True
    assert holdout["plan_hash"] == plan["plan_hash"]
    assert holdout["spec_hashes"] == {plan["injections"][0]["injection_id"]: plan["injections"][0]["spec_hash"]}


def test_world_versions_section_flags_heterogeneous_corpus_hashes(tmp_path: Path) -> None:
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"], run_roots=["s2_a"])
    write_holdout_inputs(tmp_path, plan)
    root_a = tmp_path / "s2_a"
    root_a.mkdir(parents=True, exist_ok=True)
    (root_a / "config.json").write_text(json.dumps({"world": {"corpus": {"effective_corpus_hash": "hash-A", "mutation_hash": "m-A"}}}), encoding="utf-8")
    (root_a / "meta.json").write_text(json.dumps({"stage": "S2"}), encoding="utf-8")

    manifest = build_stage9_evidence_manifest(tmp_path)

    # Only one hash observed so far -> not heterogeneous.
    assert manifest["world_versions"]["heterogeneous"] is False
    assert "hash-A" in manifest["world_versions"]["distinct_effective_corpus_hashes"]

    # Add a second holdout evidence run with a DIFFERENT corpus hash.
    plan2 = build_holdout_injection_plan(Path.cwd(), mutation_ids=["dangling_fill_search_key_stub"], run_roots=["s2_b"])
    merged_plan = {**plan, "injections": plan["injections"] + plan2["injections"]}
    write_holdout_inputs(tmp_path, merged_plan)
    root_b = tmp_path / "s2_b"
    root_b.mkdir(parents=True, exist_ok=True)
    (root_b / "config.json").write_text(json.dumps({"world": {"corpus": {"effective_corpus_hash": "hash-B", "mutation_hash": "m-B"}}}), encoding="utf-8")
    (root_b / "meta.json").write_text(json.dumps({"stage": "S2"}), encoding="utf-8")

    manifest2 = build_stage9_evidence_manifest(tmp_path)

    assert manifest2["world_versions"]["heterogeneous"] is True
    assert manifest2["world_versions"]["distinct_hash_count"] == 2
    assert set(manifest2["world_versions"]["distinct_effective_corpus_hashes"]) == {"hash-A", "hash-B"}


# ---------------------------------------------------------------------------
# check_manifest_consistency (the readiness-side consumer)
# ---------------------------------------------------------------------------


def test_check_manifest_consistency_fails_when_manifest_absent(tmp_path: Path) -> None:
    result = check_manifest_consistency(tmp_path)

    assert result["passed"] is False
    assert result["manifest_present"] is False


def test_check_manifest_consistency_passes_when_matching(tmp_path: Path) -> None:
    _write_backcasting_evidence(tmp_path)
    write_stage9_evidence_manifest(tmp_path)

    result = check_manifest_consistency(tmp_path)

    assert result["passed"] is True
    assert result["mismatches"] == []


def test_check_manifest_consistency_fails_on_sample_seed_drift(tmp_path: Path) -> None:
    """Ungameability: if the results file is regenerated with a different
    sample_seed after the manifest was written, the manifest is now stale
    and must be reported inconsistent."""
    _write_backcasting_evidence(tmp_path, sample_seed=0)
    write_stage9_evidence_manifest(tmp_path)

    # Re-run with a different sample_seed, overwriting the results file, but
    # the manifest still records the old seed/hash.
    _write_backcasting_evidence(tmp_path, sample_seed=42)

    result = check_manifest_consistency(tmp_path)

    assert result["passed"] is False
    assert any("backcasting.sample_seed" in m for m in result["mismatches"])


def test_check_manifest_consistency_fails_on_reviewer_type_drift(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet, id_map = build_blind_review_packet([run_root], reviewer_type="human_sme")
    write_sme_blind_review_inputs(tmp_path, packet, id_map)
    write_stage9_evidence_manifest(tmp_path)

    # Packet re-built as ai_proxy without regenerating the manifest.
    packet2, id_map2 = build_blind_review_packet([run_root], reviewer_type="ai_proxy")
    write_sme_blind_review_inputs(tmp_path, packet2, id_map2)

    result = check_manifest_consistency(tmp_path)

    assert result["passed"] is False
    assert any("sme_blind_review.reviewer_type" in m for m in result["mismatches"])


def test_check_manifest_consistency_fails_on_holdout_plan_hash_drift(tmp_path: Path) -> None:
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"])
    write_holdout_inputs(tmp_path, plan)
    write_stage9_evidence_manifest(tmp_path)

    plan2 = build_holdout_injection_plan(Path.cwd(), mutation_ids=["dangling_fill_search_key_stub"])
    write_holdout_inputs(tmp_path, plan2)

    result = check_manifest_consistency(tmp_path)

    assert result["passed"] is False
    assert any("holdout.plan_hash" in m for m in result["mismatches"])


# ---------------------------------------------------------------------------
# readiness.py integration: manifest absence/mismatch blocks the 10/10 gate
# ---------------------------------------------------------------------------


def test_readiness_gate_cannot_pass_without_manifest_even_if_all_ten_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate cannot reach full pass without stage9_evidence_manifest_consistent,
    regardless of how the other 10 items score."""
    gate = run_readiness_gate(tmp_path)

    manifest_check = next(check for check in gate["checks"] if check["check"] == "stage9_evidence_manifest_consistent")
    assert manifest_check["passed"] is False
    assert gate["passed"] is False
    assert gate["internal_readiness"]["passed"] is False


def test_readiness_gate_manifest_check_passes_when_consistent(tmp_path: Path) -> None:
    _write_backcasting_evidence(tmp_path)
    write_stage9_evidence_manifest(tmp_path)

    gate = run_readiness_gate(tmp_path)

    manifest_check = next(check for check in gate["checks"] if check["check"] == "stage9_evidence_manifest_consistent")
    assert manifest_check["passed"] is True


def test_readiness_report_json_contains_internal_and_external_blocks(tmp_path: Path) -> None:
    gate = run_readiness_gate(tmp_path)

    assert "internal_readiness" in gate
    assert "external_claim_readiness" in gate
    on_disk = json.loads((tmp_path / "readiness_report.json").read_text(encoding="utf-8"))
    assert "internal_readiness" in on_disk
    assert "external_claim_readiness" in on_disk


# ---------------------------------------------------------------------------
# external_claim_readiness: expected mostly false, honest about why
# ---------------------------------------------------------------------------


def test_external_claim_readiness_false_by_default(tmp_path: Path) -> None:
    summary = build_external_claim_readiness_summary(tmp_path)

    assert summary["passed"] is False
    item_names = {item["item"] for item in summary["items"]}
    assert {"human_sme_review", "g3_negative_calibration_recorded", "holdout_with_positive_and_negative_controls", "single_post_fix_world_version"} == item_names
    assert all(item["passed"] is False for item in summary["items"])


def test_external_claim_readiness_ai_proxy_sme_does_not_satisfy_human_sme_item(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet, id_map = build_blind_review_packet([run_root], reviewer_type="ai_proxy")
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    write_sme_blind_review_inputs(tmp_path, packet, id_map)
    write_sme_blind_review_report(tmp_path)

    summary = build_external_claim_readiness_summary(tmp_path)

    human_sme_item = next(item for item in summary["items"] if item["item"] == "human_sme_review")
    assert human_sme_item["passed"] is False
    assert "ai_proxy" in human_sme_item["detail"]


def test_external_claim_readiness_human_sme_satisfies_item(tmp_path: Path) -> None:
    run_root = _fixture_run_bundle(tmp_path / "run1")
    packet, id_map = build_blind_review_packet([run_root], reviewer_type="human_sme")
    for item in packet["items"]:
        item["response"] = {"plausible_workplace_scene": 5, "internally_consistent": 5, "no_artificial_markers": "no"}
    write_sme_blind_review_inputs(tmp_path, packet, id_map)
    write_sme_blind_review_report(tmp_path)

    summary = build_external_claim_readiness_summary(tmp_path)

    human_sme_item = next(item for item in summary["items"] if item["item"] == "human_sme_review")
    assert human_sme_item["passed"] is True


def test_external_claim_readiness_g3_negative_calibration_artifact_recognized(tmp_path: Path) -> None:
    (tmp_path / "g3_negative_calibration.json").write_text(
        json.dumps({"schema_version": "company_twin.g3_negative_calibration.v1", "specificity": 0.92, "known_clean_case_count": 20}),
        encoding="utf-8",
    )

    summary = build_external_claim_readiness_summary(tmp_path)

    item = next(item for item in summary["items"] if item["item"] == "g3_negative_calibration_recorded")
    assert item["passed"] is True
    assert item["specificity"] == 0.92


def test_external_claim_readiness_holdout_requires_controls_section(tmp_path: Path) -> None:
    plan = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_all"], run_roots=["s2_holdout_0"])
    write_holdout_inputs(tmp_path, plan)
    injection = plan["injections"][0]
    root = tmp_path / "s2_holdout_0"
    root.mkdir(parents=True, exist_ok=True)
    (root / "triage").mkdir(exist_ok=True)
    (root / "triage" / "metrics.json").write_text(
        json.dumps({"stage": "S2", "finding_types": {injection["expected_finding_types"][0]: 1}, "rule_hit_rate": {}, "detection_miss_rate": {}}),
        encoding="utf-8",
    )
    (root / "config.json").write_text(
        json.dumps({"world": {"corpus": {"mutations": [dict(load_mutation_catalog_spec(injection["mutation_id"]))], "effective_corpus_hash": "h"}}}),
        encoding="utf-8",
    )
    (root / "meta.json").write_text(json.dumps({"stage": "S2", "mutation_ids": [injection["mutation_id"]]}), encoding="utf-8")
    (root / "world_ledger.jsonl").write_text("".join(json.dumps({"tick": t}) + "\n" for t in range(1, 5)), encoding="utf-8")

    # No controls supplied -> external item stays false even though the holdout report passes.
    write_holdout_report(tmp_path, run_lookup={injection["injection_id"]: root})
    summary_no_controls = build_external_claim_readiness_summary(tmp_path)
    holdout_item = next(item for item in summary_no_controls["items"] if item["item"] == "holdout_with_positive_and_negative_controls")
    assert holdout_item["passed"] is False

    # With a controls section (even an empty/no-anomaly one) present, the item passes.
    write_holdout_report(tmp_path, run_lookup={injection["injection_id"]: root}, control_run_roots=["s2_control"])
    (tmp_path / "s2_control" / "triage").mkdir(parents=True, exist_ok=True)
    (tmp_path / "s2_control" / "triage" / "metrics.json").write_text(json.dumps({"finding_types": {}, "rule_hit_rate": {}}), encoding="utf-8")
    write_holdout_report(tmp_path, run_lookup={injection["injection_id"]: root}, control_run_roots=["s2_control"])
    summary_with_controls = build_external_claim_readiness_summary(tmp_path)
    holdout_item2 = next(item for item in summary_with_controls["items"] if item["item"] == "holdout_with_positive_and_negative_controls")
    assert holdout_item2["passed"] is True


def load_mutation_catalog_spec(mutation_id: str) -> dict[str, Any]:
    from company_twin.mutations import load_mutation_catalog

    return load_mutation_catalog(Path.cwd())[mutation_id]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_stage9_evidence_manifest_cli_writes_manifest_file(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["stage9-evidence-manifest", "--campaign-root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert (tmp_path / EVIDENCE_MANIFEST_FILENAME).exists()
    payload = json.loads((tmp_path / EVIDENCE_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert payload["schema_version"] == EVIDENCE_MANIFEST_SCHEMA_VERSION
    assert payload["git_commit"]
