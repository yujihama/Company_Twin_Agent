"""Diegetic notice circulation (default-off experimental variable; MASTER_DESIGN.md
section 8.2/17.x, approved 2026-07-06).

Raw-data audit background: runtime-injected notice documents (DFH-SAL-901/
902/903, DFH-CUS-006 fill) were never read by any seat in any campaign
(attempts.jsonl/basis_records.jsonl carry zero references) -- mutations were
filed into the searchable corpus but nothing ever surfaced them, since M1
runtime mutations receive no search ranking boost by design. This module
tests the circulation mechanism that gives a mutated document a realistic
path into a seat's attention: an early-tick timed_notice inbox announcement
that a notice exists, addressed only to the mutation's own visible_roles,
without repeating the notice's substantive content (exposure -- actually
reading the document -- remains a behavioral outcome).
"""
import json
from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from company_twin.acceptance import a03_inbox_whitelist
from company_twin.cli import app
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design
from company_twin.harness import run_s1_episode, run_s2_world
from company_twin.holdout import (
    build_holdout_injection_plan,
    load_mutation_catalog,
    verify_holdout_bundles,
)
from company_twin.kernel import validate_inbox_message
from company_twin.mutations import (
    apply_corpus_mutations,
    circulation_digest_text,
    lint_mutation_specs,
    mutation_specs_from_values,
    notice_subject,
)
from company_twin.recorder import read_jsonl
from company_twin.world_config import _json_hash as world_json_hash
from conftest import fake_seat_factory
from test_world_runs import _LateBoundCustomer

ALL_CATALOG_MUTATION_IDS = [
    "clarify_elderly_understanding_all",
    "clarify_elderly_understanding_sales_only",
    "contradict_chat_approval_recorded",
    "dangling_fill_search_key_stub",
    "role_table_fix_quality_owner",
]

SALES_SEATS = {"emp-A", "emp-B", "emp-F", "emp-G"}
NON_SALES_SEATS = {"emp-C", "emp-M", "emp-Q", "audit-in-world"}


def _design_and_corpus():
    design = load_design(Path.cwd())
    return design, Corpus.from_design(design)


def _delivered_circulation_notices(run_root: Path) -> list[dict]:
    ledger = read_jsonl(run_root / "world_ledger.jsonl")
    rows = []
    for row in ledger:
        if row.get("event_type") != "inbox_delivered":
            continue
        message = (row.get("payload") or {}).get("message") or {}
        if message.get("kind") == "timed_notice" and message.get("notice") == "document_circulation":
            rows.append({"to_seat": (row.get("payload") or {}).get("to_seat"), **message})
    return rows


# ---------------------------------------------------------------------------
# Digest text derivation (all 5 catalog mutations)
# ---------------------------------------------------------------------------


def test_circulation_digest_derives_natural_text_for_every_catalog_mutation() -> None:
    catalog = load_mutation_catalog(Path.cwd())
    assert sorted(catalog) == sorted(ALL_CATALOG_MUTATION_IDS)
    for mutation_id in ALL_CATALOG_MUTATION_IDS:
        spec = catalog[mutation_id]
        raw_text = spec.get("text") or spec.get("append_text")
        subject = notice_subject(raw_text)
        digest = circulation_digest_text(raw_text)
        assert subject, mutation_id
        assert digest.startswith("本日付の事務連絡を回覧します: ")
        assert subject in digest
        assert "詳細は文書一覧をご確認ください。" in digest
        # The announcement must never repeat the notice's own substantive
        # content beyond its subject/label -- e.g. the clarify body text
        # about 理解度確認/同席者関与 must not leak into the digest.
        assert "理解度確認" not in digest or "理解度確認" in subject


def test_circulation_digest_passes_world_leak_lint() -> None:
    catalog = load_mutation_catalog(Path.cwd())
    for mutation_id in ALL_CATALOG_MUTATION_IDS:
        spec = catalog[mutation_id]
        raw_text = spec.get("text") or spec.get("append_text")
        digest = circulation_digest_text(raw_text)
        assert lint_mutation_specs([{"mutation_id": mutation_id, "text": digest}]) == []


def test_circulation_digest_rejects_leaking_subject() -> None:
    with pytest.raises(ValueError, match="circulation digest leaks"):
        circulation_digest_text("件名: AMB-02 probe experiment。本文。")


# ---------------------------------------------------------------------------
# Applied-mutation entries carry visible_roles + circulation_digest
# ---------------------------------------------------------------------------


def test_applied_mutation_entries_carry_circulation_digest_and_visible_roles() -> None:
    _, corpus = _design_and_corpus()
    specs = mutation_specs_from_values(Path.cwd(), ALL_CATALOG_MUTATION_IDS)
    result = apply_corpus_mutations(corpus, specs)

    by_mutation_id = {entry["mutation_id"]: entry for entry in result.applied}
    for mutation_id in ALL_CATALOG_MUTATION_IDS:
        entry = by_mutation_id[mutation_id]
        assert entry["circulation_digest"].startswith("本日付の事務連絡を回覧します: ")
        assert entry["visible_roles"]

    # role_table_fix patches DFH-SAL-045, which has visible_roles=None (readable
    # by everyone) -- circulation must resolve that to the full role list.
    assert set(by_mutation_id["role_table_fix_quality_owner"]["visible_roles"]) == {
        "sales",
        "manager",
        "application",
        "second_line",
        "audit",
    }
    # clarify_elderly_understanding_sales_only stays role-scoped to sales only.
    assert by_mutation_id["clarify_elderly_understanding_sales_only"]["visible_roles"] == ["sales"]


# ---------------------------------------------------------------------------
# Default off: no announcement, config records false
# ---------------------------------------------------------------------------


def test_circulation_defaults_off_no_announcement_and_config_records_false(tmp_path: Path) -> None:
    design, corpus = _design_and_corpus()
    specs = mutation_specs_from_values(Path.cwd(), ["clarify_elderly_understanding_sales_only"])
    result = apply_corpus_mutations(corpus, specs)
    run_root = tmp_path / "s2_no_circulation"

    run_s2_world(
        design=design,
        corpus=result.corpus,
        run_root=run_root,
        seed=0,
        ticks=2,
        seat_factory=fake_seat_factory(),
        customer_llm=_LateBoundCustomer(run_root),
        mutations=result.applied,
    )

    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    circulation = config["world"]["corpus"]["circulation"]
    assert circulation["enabled"] is False
    assert circulation["announcements"] == []
    assert _delivered_circulation_notices(run_root) == []


def test_circulation_defaults_off_for_s1_too(tmp_path: Path) -> None:
    design, corpus = _design_and_corpus()
    specs = mutation_specs_from_values(Path.cwd(), ["dangling_fill_search_key_stub"])
    result = apply_corpus_mutations(corpus, specs)
    run_root = tmp_path / "s1_no_circulation"

    run_s1_episode(
        design=design,
        corpus=result.corpus,
        probe_id="P-04",
        run_root=run_root,
        seed=0,
        ticks=2,
        seat_factory=fake_seat_factory(),
        customer_llm=_LateBoundCustomer(run_root),
        mutations=result.applied,
    )

    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    assert config["world"]["corpus"]["circulation"]["enabled"] is False
    assert _delivered_circulation_notices(run_root) == []


# ---------------------------------------------------------------------------
# On: delivered only to visible roles, at the right tick, recorded, lint-clean
# ---------------------------------------------------------------------------


def test_circulation_on_delivers_only_to_visible_roles_at_tick_one(tmp_path: Path) -> None:
    design, corpus = _design_and_corpus()
    specs = mutation_specs_from_values(Path.cwd(), ["clarify_elderly_understanding_sales_only"])
    result = apply_corpus_mutations(corpus, specs)
    run_root = tmp_path / "s2_circulation_on"

    run_s2_world(
        design=design,
        corpus=result.corpus,
        run_root=run_root,
        seed=0,
        ticks=2,
        seat_factory=fake_seat_factory(),
        customer_llm=_LateBoundCustomer(run_root),
        mutations=result.applied,
        circulate_notices=True,
    )

    config = json.loads((run_root / "config.json").read_text(encoding="utf-8"))
    circulation = config["world"]["corpus"]["circulation"]
    assert circulation["enabled"] is True
    assert len(circulation["announcements"]) == 1
    announcement = circulation["announcements"][0]
    assert announcement["mutation_id"] == "clarify_elderly_understanding_sales_only"
    assert announcement["doc_id"] == "DFH-SAL-902"
    assert announcement["tick"] == 1
    assert announcement["visible_roles"] == ["sales"]

    delivered = _delivered_circulation_notices(run_root)
    assert delivered, "circulation announcement must be delivered"
    delivered_seats = {row["to_seat"] for row in delivered}
    assert delivered_seats == SALES_SEATS
    assert delivered_seats.isdisjoint(NON_SALES_SEATS)
    for row in delivered:
        assert row["tick"] == 1
        assert row["notice"] == "document_circulation"
        assert row["detail"] == announcement["digest"]

    # inbox whitelist gate must accept the announcement kind (timed_notice,
    # kernel.INBOX_ALLOWED_KEYS) -- it is not a new inbox kind.
    assert a03_inbox_whitelist(run_root).passed


def test_circulation_on_role_table_fix_reaches_every_role(tmp_path: Path) -> None:
    design, corpus = _design_and_corpus()
    specs = mutation_specs_from_values(Path.cwd(), ["role_table_fix_quality_owner"])
    result = apply_corpus_mutations(corpus, specs)
    run_root = tmp_path / "s2_role_table_fix"

    run_s2_world(
        design=design,
        corpus=result.corpus,
        run_root=run_root,
        seed=0,
        ticks=2,
        seat_factory=fake_seat_factory(),
        customer_llm=_LateBoundCustomer(run_root),
        mutations=result.applied,
        circulate_notices=True,
    )

    delivered = _delivered_circulation_notices(run_root)
    delivered_seats = {row["to_seat"] for row in delivered}
    assert delivered_seats == SALES_SEATS | NON_SALES_SEATS


def test_circulation_announcement_validates_against_inbox_whitelist() -> None:
    catalog = load_mutation_catalog(Path.cwd())
    for mutation_id in ALL_CATALOG_MUTATION_IDS:
        spec = catalog[mutation_id]
        raw_text = spec.get("text") or spec.get("append_text")
        digest = circulation_digest_text(raw_text)
        message = {"kind": "timed_notice", "tick": 1, "notice": "document_circulation", "detail": digest}
        validate_inbox_message(message)  # must not raise


def test_circulation_flag_recorded_false_by_default_in_world_config_schema(tmp_path: Path) -> None:
    from company_twin.world_config import build_world_config

    design, _ = _design_and_corpus()
    config = build_world_config(design, stage="S1", model=None, seed=0, ticks=6)
    assert config["world"]["corpus"]["circulation"] == {"enabled": False, "announcements": []}


def test_circulation_flag_true_with_no_mutations_still_records_enabled(tmp_path: Path) -> None:
    from company_twin.world_config import build_world_config

    design, _ = _design_and_corpus()
    config = build_world_config(design, stage="S1", model=None, seed=0, ticks=6, circulate_notices=True)
    circulation = config["world"]["corpus"]["circulation"]
    assert circulation["enabled"] is True
    assert circulation["announcements"] == []


# ---------------------------------------------------------------------------
# CLI: --circulate-notices flag on s1/s2
# ---------------------------------------------------------------------------


def _command_option_names(command_name: str) -> set[str]:
    """Introspect a Typer/Click command's registered option flags directly,
    rather than parsing rendered --help text -- the rich help panel wraps at
    terminal width and can split a long flag/help string across lines in a
    narrow sandboxed terminal, making substring checks on rendered output
    flaky independent of whether the CLI is actually wired correctly."""
    click_group = typer.main.get_command(app)
    click_command = click_group.commands[command_name]
    names: set[str] = set()
    for param in click_command.params:
        names.update(param.opts)
        names.update(getattr(param, "secondary_opts", []) or [])
    return names


def test_cli_s2_circulate_notices_flag_wired() -> None:
    # s2 requires live execution (_require_live) so this asserts the CLI
    # wiring (flag exists, correctly named, defaults off) rather than
    # invoking a live run; live delivery/recording is covered by
    # test_circulation_on_delivers_only_to_visible_roles_at_tick_one via the
    # harness function s2's CLI command calls directly.
    click_group = typer.main.get_command(app)
    param = next(p for p in click_group.commands["s2"].params if p.name == "circulate_notices")
    assert "--circulate-notices" in param.opts
    assert "--no-circulate-notices" in param.secondary_opts
    assert param.default is False


def test_cli_s1_and_campaign_and_s0_expose_circulate_notices_flag() -> None:
    for command in ("s0", "s1", "s2", "campaign"):
        assert "--circulate-notices" in _command_option_names(command), command


# ---------------------------------------------------------------------------
# Holdout integration: --require-circulation seals the plan; bundle
# verification fails without circulation on.
# ---------------------------------------------------------------------------


def test_holdout_plan_require_circulation_seals_plan_hash() -> None:
    plan_off = build_holdout_injection_plan(Path.cwd(), mutation_ids=["clarify_elderly_understanding_sales_only"])
    plan_on = build_holdout_injection_plan(
        Path.cwd(), mutation_ids=["clarify_elderly_understanding_sales_only"], require_circulation=True
    )

    assert plan_off["circulation_required"] is False
    assert plan_on["circulation_required"] is True
    assert plan_off["plan_hash"] != plan_on["plan_hash"]

    recomputed_on = world_json_hash(
        {
            "injections": plan_on["injections"],
            "control_run_roots": plan_on["control_run_roots"],
            "circulation_required": plan_on["circulation_required"],
        }
    )
    assert recomputed_on == plan_on["plan_hash"]


def _verified_s2_bundle_with_circulation(
    root: Path,
    *,
    injection: dict,
    circulation_enabled: bool,
    planned_ticks: int = 2,
) -> None:
    """Minimal verified-shape S2 bundle (mirrors test_wp14_calibration's
    _verified_s2_bundle) with a config.json world.corpus.circulation block."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "triage").mkdir(exist_ok=True)
    (root / "triage" / "metrics.json").write_text(
        json.dumps({"stage": "S2", "finding_types": {}, "rule_hit_rate": {}, "detection_miss_rate": {}}),
        encoding="utf-8",
    )
    mutation_id = injection["mutation_id"]
    spec = load_mutation_catalog(Path.cwd())[mutation_id]
    mutation_entry = dict(spec)
    (root / "config.json").write_text(
        json.dumps(
            {
                "world": {
                    "corpus": {
                        "mutations": [mutation_entry],
                        "mutation_hash": world_json_hash([mutation_entry]),
                        "effective_corpus_hash": "test-hash",
                        "circulation": {"enabled": circulation_enabled, "announcements": []},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (root / "meta.json").write_text(json.dumps({"stage": "S2", "mutation_ids": [mutation_id]}), encoding="utf-8")
    ledger_rows = [{"tick": tick, "event_type": "tick_committed"} for tick in range(1, planned_ticks + 1)]
    (root / "world_ledger.jsonl").write_text("".join(json.dumps(row) + "\n" for row in ledger_rows), encoding="utf-8")
    (root / "attempts.jsonl").write_text("", encoding="utf-8")


def test_verify_holdout_bundles_fails_without_circulation_when_required(tmp_path: Path) -> None:
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["clarify_elderly_understanding_sales_only"],
        auto_run_roots=True,
        planned_ticks=2,
        require_circulation=True,
    )
    injection = plan["injections"][0]
    run_root = tmp_path / injection["planned_run_roots"][0]
    _verified_s2_bundle_with_circulation(run_root, injection=injection, circulation_enabled=False)

    verification = verify_holdout_bundles(tmp_path, plan)

    assert verification["circulation_required"] is True
    assert verification["all_verified"] is False
    row = verification["per_injection"][0]
    assert row["verified"] is False
    assert "circulation_required=true" in row["detail"]
    assert row["runs"][0]["circulation_enabled"] is False
    assert row["runs"][0]["circulation_ok"] is False


def test_verify_holdout_bundles_passes_with_circulation_when_required(tmp_path: Path) -> None:
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["clarify_elderly_understanding_sales_only"],
        auto_run_roots=True,
        planned_ticks=2,
        require_circulation=True,
    )
    injection = plan["injections"][0]
    run_root = tmp_path / injection["planned_run_roots"][0]
    _verified_s2_bundle_with_circulation(run_root, injection=injection, circulation_enabled=True)

    verification = verify_holdout_bundles(tmp_path, plan)

    assert verification["all_verified"] is True
    row = verification["per_injection"][0]
    assert row["verified"] is True
    assert row["runs"][0]["circulation_ok"] is True


def test_verify_holdout_bundles_ignores_circulation_when_not_required(tmp_path: Path) -> None:
    plan = build_holdout_injection_plan(
        Path.cwd(),
        mutation_ids=["clarify_elderly_understanding_sales_only"],
        auto_run_roots=True,
        planned_ticks=2,
        require_circulation=False,
    )
    injection = plan["injections"][0]
    run_root = tmp_path / injection["planned_run_roots"][0]
    _verified_s2_bundle_with_circulation(run_root, injection=injection, circulation_enabled=False)

    verification = verify_holdout_bundles(tmp_path, plan)

    assert verification["circulation_required"] is False
    assert verification["all_verified"] is True


def test_holdout_plan_cli_require_circulation_flag(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "holdout-plan",
            "--campaign-root",
            str(tmp_path),
            "--mutation",
            "clarify_elderly_understanding_sales_only",
            "--require-circulation",
        ],
    )
    assert result.exit_code == 0, result.output
    plan = json.loads((tmp_path / "holdout_inputs.json").read_text(encoding="utf-8"))
    assert plan["circulation_required"] is True
