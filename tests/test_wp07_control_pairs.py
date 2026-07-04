import json
from pathlib import Path

from company_twin.campaign import run_control_pair_campaign
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.mutations import build_delta_one_pair_manifest
from company_twin.oracles import aggregate_ensemble_triage
from company_twin.recorder import read_jsonl
from conftest import fake_seat_factory
from test_world_runs import _LateBoundCustomer


MUTATION_ID = "clarify_elderly_understanding_all"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""), encoding="utf-8")


def _write_synthetic_bundle(
    campaign_root: Path,
    name: str,
    *,
    seed: int,
    mutation_ids: list[str],
    finding_types: dict[str, int],
    campaign_mode: str | None = "control_pairs",
) -> None:
    run_root = campaign_root / name
    (run_root / "triage").mkdir(parents=True)
    meta = {"stage": "S1", "probe": "P-01", "knobs": {}, "seed": seed, "anchor": False, "mutation_ids": mutation_ids}
    if campaign_mode:
        meta["campaign_mode"] = campaign_mode
    (run_root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    mutations = [{"mutation_id": mutation_id} for mutation_id in mutation_ids]
    (run_root / "config.json").write_text(
        json.dumps(
            {
                "world": {
                    "corpus": {
                        "mutations": mutations,
                        "mutation_hash": "hash-" + "-".join(mutation_ids),
                        "effective_corpus_hash": "effective-" + "-".join(mutation_ids),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (run_root / "triage" / "metrics.json").write_text(
        json.dumps({"controlled_actions_agent": 1, "finding_types": finding_types}),
        encoding="utf-8",
    )
    (run_root / "triage" / "buckets.json").write_text(json.dumps({"buckets": []}), encoding="utf-8")
    _write_jsonl(run_root / "attempts.jsonl", [])
    _write_jsonl(run_root / "basis_records.jsonl", [])
    _write_jsonl(run_root / "world_ledger.jsonl", [])
    _write_jsonl(run_root / "store_events.jsonl", [])


def test_mutation_delta_attribution_has_wilson_seed_check_icc_and_filter(tmp_path: Path) -> None:
    manifest = build_delta_one_pair_manifest(root=Path.cwd(), mutation_ids=[MUTATION_ID], seeds=2)
    (tmp_path / "control_pair_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for seed in range(2):
        _write_synthetic_bundle(tmp_path, f"pair_{MUTATION_ID}_seed{seed}_control", seed=seed, mutation_ids=[], finding_types={})
        _write_synthetic_bundle(tmp_path, f"pair_{MUTATION_ID}_seed{seed}_treatment", seed=seed, mutation_ids=[MUTATION_ID], finding_types={"evidence_gap": 1})
    _write_synthetic_bundle(tmp_path, "exploration_s1_seed99", seed=99, mutation_ids=[], finding_types={"evidence_gap": 1}, campaign_mode=None)

    payload = aggregate_ensemble_triage(tmp_path)

    assert payload["run_filter"]["mode"] == "control_pairs"
    assert payload["run_filter"]["included_run_count"] == 4
    assert len(payload["groups"]) == 2
    assert all(group["seeds"] == 2 for group in payload["groups"])
    assert payload["icc_summary"]["group_count"] == 2

    row = payload["attribution_table"][0]
    assert row["status"] == "candidate"
    assert row["delta"] == "world.corpus.mutations"
    assert row["right_value"] == [MUTATION_ID]
    assert row["seed_bundle_match"] is True
    assert row["left_seeds"] == [0, 1]
    assert row["right_seeds"] == [0, 1]
    assert row["left_wilson_95"] and row["right_wilson_95"]
    assert row["effect_delta"] == 1.0


def test_mutation_delta_attribution_keeps_zero_effect_row(tmp_path: Path) -> None:
    manifest = build_delta_one_pair_manifest(root=Path.cwd(), mutation_ids=[MUTATION_ID], seeds=1)
    (tmp_path / "control_pair_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_synthetic_bundle(tmp_path, f"pair_{MUTATION_ID}_seed0_control", seed=0, mutation_ids=[], finding_types={})
    _write_synthetic_bundle(tmp_path, f"pair_{MUTATION_ID}_seed0_treatment", seed=0, mutation_ids=[MUTATION_ID], finding_types={})

    payload = aggregate_ensemble_triage(tmp_path)

    rows = [row for row in payload["attribution_table"] if row["finding_type"] == "any_l0_finding"]
    assert len(rows) == 1
    assert rows[0]["delta"] == "world.corpus.mutations"
    assert rows[0]["effect_delta"] == 0.0
    assert rows[0]["left_wilson_95"] and rows[0]["right_wilson_95"]


def test_control_pair_campaign_executes_manifest_and_marks_bundles(tmp_path: Path) -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    manifest = build_delta_one_pair_manifest(root=Path.cwd(), mutation_ids=[MUTATION_ID], seeds=1)

    summary = run_control_pair_campaign(
        root=tmp_path,
        design=design,
        corpus=corpus,
        manifest=manifest,
        probe="P-01",
        ticks=1,
        seat_factory=fake_seat_factory(),
        customer_llm_factory=lambda run_root: _LateBoundCustomer(run_root),
    )

    campaign_root = Path(summary["campaign_root"])
    assert summary["schema_version"] == "company_twin.control_pair_campaign.v1"
    assert summary["condition_run_count"] == 2
    assert (campaign_root / "control_pair_manifest.json").exists()
    assert (campaign_root / "ensemble_triage.json").exists()
    metas = [json.loads(Path(row["run_root"]).joinpath("meta.json").read_text(encoding="utf-8")) for row in summary["runs"]]
    assert {meta["campaign_mode"] for meta in metas} == {"control_pairs"}
    assert {meta["control_pair"]["condition"] for meta in metas} == {"control", "treatment"}

    config = json.loads(Path(summary["runs"][0]["run_root"]).joinpath("config.json").read_text(encoding="utf-8"))
    assert config["world"]["schedule"]["timed_notice_recipients"] == []
    ledger = read_jsonl(Path(summary["runs"][0]["run_root"]) / "world_ledger.jsonl")
    deadline_notices = [
        row
        for row in ledger
        if row.get("event_type") == "inbox_delivered"
        and ((row.get("payload") or {}).get("message") or {}).get("notice") == "campaign_deadline"
    ]
    assert deadline_notices == []
