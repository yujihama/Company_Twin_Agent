"""Diegetic notice circulation (default-off experimental variable; MASTER_DESIGN.md
section 8.2/17.13/17.x, approved 2026-07-06).

Raw-data audit background (era-5, title-only): runtime-injected notice
documents (DFH-SAL-901/902/903, DFH-CUS-006 fill) were never read by any seat
in any campaign (attempts.jsonl/basis_records.jsonl carry zero references
across 5 contradict seeds plus clarify/dangling runs) -- mutations were filed
into the searchable corpus but nothing ever surfaced them, since M1 runtime
mutations receive no search ranking boost by design, and a title-only
announcement ("a notice exists, go look it up") was never enough to draw a
seat's attention to it. Title-only circulation was the unrealistic variant:
real-world 事務連絡 (internal notices) circulate WITH their body text.

This module tests both circulation designs:
- the full-text mechanism (current default once circulation is on, approved
  2026-07-06): the circulated inbox message carries the notice's own body
  after the header line, and EXPOSURE (holdout.py) is redefined so that
  DELIVERY of the circular to at least one seat counts as exposure directly
  -- the previous read_document/basis-citation evidence is retained as a
  secondary `content_read` field, reported but not required.
- the legacy title-only digest (`circulation_digest_text`, kept only for
  backward compatibility with older sealed era-5 bundles), which still only
  ANNOUNCES that a notice exists without repeating its substantive content,
  and whose bundles still fall back to the original read-based exposure
  definition.
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
    _run_exposure,
)
from company_twin.kernel import validate_inbox_message
from company_twin.mutations import (
    apply_corpus_mutations,
    circulation_digest_text,
    circulation_message_text,
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
        assert row["detail"] == announcement["message"]

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
    assert config["world"]["corpus"]["circulation"] == {"enabled": False, "mode": "full_text", "announcements": []}


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


# ---------------------------------------------------------------------------
# Full-text circulation (approved 2026-07-06, MASTER_DESIGN.md section 17.x):
# the circulated message carries the notice's own BODY after the header line,
# not just its title.
# ---------------------------------------------------------------------------


def test_circulation_message_text_carries_header_and_body_for_every_catalog_mutation() -> None:
    catalog = load_mutation_catalog(Path.cwd())
    for mutation_id in ALL_CATALOG_MUTATION_IDS:
        spec = catalog[mutation_id]
        raw_text = spec.get("text") or spec.get("append_text")
        subject = notice_subject(raw_text)
        message = circulation_message_text(raw_text)
        assert message.startswith(f"本日付の事務連絡を回覧します: 「{subject}」\n")
        # The full body -- the mutation's own world-visible catalog text --
        # must be present verbatim after the header line (this is the
        # substantive change from title-only: the body is now delivered).
        assert raw_text.strip() in message


def test_circulation_message_text_passes_world_leak_lint() -> None:
    catalog = load_mutation_catalog(Path.cwd())
    for mutation_id in ALL_CATALOG_MUTATION_IDS:
        spec = catalog[mutation_id]
        raw_text = spec.get("text") or spec.get("append_text")
        message = circulation_message_text(raw_text)
        assert lint_mutation_specs([{"mutation_id": mutation_id, "text": message}]) == []


def test_circulation_message_text_rejects_leaking_body() -> None:
    with pytest.raises(ValueError, match="circulation message leaks"):
        circulation_message_text("件名: AMB-02 probe experiment。本文。")


def test_circulation_message_text_size_sanity_for_every_catalog_mutation() -> None:
    """Circulated 事務連絡 are a few sentences (the longest catalog entry is
    ~120 characters); the assembled message (header + body) must stay well
    under the sanity ceiling that guards against an accidental
    whole-document/whole-corpus paste into a single inbox message."""
    from company_twin.mutations import MAX_CIRCULATION_MESSAGE_CHARS

    catalog = load_mutation_catalog(Path.cwd())
    for mutation_id in ALL_CATALOG_MUTATION_IDS:
        spec = catalog[mutation_id]
        raw_text = spec.get("text") or spec.get("append_text")
        message = circulation_message_text(raw_text)
        assert len(message) < 500, f"{mutation_id} circulation message unexpectedly long: {len(message)} chars"
        assert len(message) < MAX_CIRCULATION_MESSAGE_CHARS


def test_circulation_message_text_rejects_oversized_body() -> None:
    oversized_body = "件名: 通常の事務連絡。" + ("これは長い本文です。" * 300)
    with pytest.raises(ValueError, match="exceeding the .* sanity ceiling"):
        circulation_message_text(oversized_body)


def test_applied_mutation_entries_carry_circulation_message_and_digest() -> None:
    _, corpus = _design_and_corpus()
    specs = mutation_specs_from_values(Path.cwd(), ALL_CATALOG_MUTATION_IDS)
    result = apply_corpus_mutations(corpus, specs)

    by_mutation_id = {entry["mutation_id"]: entry for entry in result.applied}
    for mutation_id in ALL_CATALOG_MUTATION_IDS:
        entry = by_mutation_id[mutation_id]
        assert entry["circulation_message"].startswith("本日付の事務連絡を回覧します: ")
        assert "\n" in entry["circulation_message"]
        # Legacy title-only digest is still carried too, for backward
        # compatibility with any code path that still reads it.
        assert entry["circulation_digest"].startswith("本日付の事務連絡を回覧します: ")


def test_circulation_message_validates_against_inbox_whitelist() -> None:
    catalog = load_mutation_catalog(Path.cwd())
    for mutation_id in ALL_CATALOG_MUTATION_IDS:
        spec = catalog[mutation_id]
        raw_text = spec.get("text") or spec.get("append_text")
        message = circulation_message_text(raw_text)
        inbox_message = {"kind": "timed_notice", "tick": 1, "notice": "document_circulation", "detail": message}
        validate_inbox_message(inbox_message)  # must not raise


def test_circulation_on_delivers_full_text_body_and_config_records_mode(tmp_path: Path) -> None:
    """End-to-end: circulation ON delivers the assembled full-text message
    (header + body) to the inbox, config.json records mode=='full_text', and
    the delivered message passes both the world leak lint and the inbox
    whitelist gate."""
    design, corpus = _design_and_corpus()
    specs = mutation_specs_from_values(Path.cwd(), ["clarify_elderly_understanding_sales_only"])
    result = apply_corpus_mutations(corpus, specs)
    run_root = tmp_path / "s2_full_text_circulation"

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
    assert circulation["mode"] == "full_text"
    announcement = circulation["announcements"][0]
    assert "message" in announcement
    raw_text = mutation_specs_from_values(Path.cwd(), ["clarify_elderly_understanding_sales_only"])[0]["text"]
    assert raw_text.strip() in announcement["message"]

    delivered = _delivered_circulation_notices(run_root)
    assert delivered, "circulation message must be delivered"
    for row in delivered:
        assert row["detail"] == announcement["message"]
        assert raw_text.strip() in row["detail"]

    # The assembled delivered message must pass the world leak lint (it is
    # the same world-visible mutation text already linted at application
    # time, reassembled with a lint-checked header) and the inbox whitelist
    # gate every other timed_notice already passes.
    for row in delivered:
        assert lint_mutation_specs([{"mutation_id": "<delivered_message>", "text": row["detail"]}]) == []
    assert a03_inbox_whitelist(run_root).passed


def test_circulation_mode_recorded_full_text_even_when_disabled(tmp_path: Path) -> None:
    """mode is recorded regardless of whether circulation is turned on --
    an honest record of which design would apply if it were enabled, so an
    empty announcements list is never ambiguous about which era's design a
    disabled-circulation run belongs to."""
    from company_twin.world_config import build_world_config

    design, _ = _design_and_corpus()
    config_off = build_world_config(design, stage="S1", model=None, seed=0, ticks=6, circulate_notices=False)
    config_on = build_world_config(design, stage="S1", model=None, seed=0, ticks=6, circulate_notices=True)
    assert config_off["world"]["corpus"]["circulation"]["mode"] == "full_text"
    assert config_on["world"]["corpus"]["circulation"]["mode"] == "full_text"


# ---------------------------------------------------------------------------
# Exposure redefinition (MASTER_DESIGN.md section 17.x, approved 2026-07-06):
# for a config.json recording mode=='full_text', EXPOSURE = circular
# delivered to at least one seat -- true even with zero read_document hits.
# content_read is a secondary recorded field, reported but not required.
# ---------------------------------------------------------------------------


def _config_with_circulation(*, mode: str, enabled: bool, announcements: list[dict]) -> dict:
    return {
        "world": {
            "corpus": {
                "circulation": {"enabled": enabled, "mode": mode, "announcements": announcements},
            }
        }
    }


def _write_config(root: Path, config: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _write_ledger(root: Path, rows: list[dict]) -> None:
    (root / "world_ledger.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_exposure_true_via_circulation_delivery_with_zero_read_document_hits(tmp_path: Path) -> None:
    """Full-text mode: exposure is TRUE when the circular was delivered, even
    though attempts.jsonl/basis_records.jsonl have zero read/citation hits --
    delivery IS content exposure once the circular carries the notice's own
    body."""
    root = tmp_path / "run0"
    message = "本日付の事務連絡を回覧します: 「テスト通知」\n本文のテキストです。"
    config = _config_with_circulation(
        mode="full_text",
        enabled=True,
        announcements=[
            {
                "mutation_id": "clarify_elderly_understanding_sales_only",
                "doc_id": "DFH-SAL-902",
                "tick": 1,
                "visible_roles": ["sales"],
                "message": message,
                "digest": "本日付の事務連絡を回覧します: 「テスト通知」。詳細は文書一覧をご確認ください。",
            }
        ],
    )
    _write_config(root, config)
    _write_ledger(
        root,
        [
            {
                "tick": 1,
                "event_type": "inbox_delivered",
                "payload": {"to_seat": "emp-A", "message": {"kind": "timed_notice", "tick": 1, "notice": "document_circulation", "detail": message}},
            }
        ],
    )
    (root / "attempts.jsonl").write_text("", encoding="utf-8")

    exposure = _run_exposure(root, "DFH-SAL-902", mutation_id="clarify_elderly_understanding_sales_only")

    assert exposure["exposed"] is True
    assert exposure["mode"] == "full_text"
    assert exposure["basis"] == "circulation_delivery"
    assert exposure["circulation_delivery_hits"]
    assert exposure["circulation_delivery_hits"][0]["to_seat"] == "emp-A"
    # Secondary field: content_read is reported honestly as False (no
    # read_document/basis-citation evidence), but does NOT block exposure.
    assert exposure["content_read"] is False
    assert exposure["content_read_detail"]["read_document_hits"] == []
    assert exposure["content_read_detail"]["basis_citation_hits"] == []


def test_exposure_false_when_full_text_mode_but_no_delivery_recorded(tmp_path: Path) -> None:
    """Full-text mode with NO delivery in the ledger (e.g. the mutation was
    sealed but circulation never actually fired for this run) is NOT
    exposure -- delivery must actually be recorded, not merely configured."""
    root = tmp_path / "run0"
    config = _config_with_circulation(mode="full_text", enabled=True, announcements=[])
    _write_config(root, config)
    _write_ledger(root, [])
    (root / "attempts.jsonl").write_text("", encoding="utf-8")

    exposure = _run_exposure(root, "DFH-SAL-902", mutation_id="clarify_elderly_understanding_sales_only")

    assert exposure["exposed"] is False
    assert exposure["mode"] == "full_text"
    assert exposure["circulation_delivery_hits"] == []
    assert "document_circulation delivery" in exposure["detail"]


def test_exposure_content_read_secondary_field_true_when_also_read(tmp_path: Path) -> None:
    """Full-text mode: when a seat ALSO issued a successful read_document
    call for the same doc_id, content_read is correctly recorded True
    alongside delivery-based exposure (both are honestly reported)."""
    root = tmp_path / "run0"
    message = "本日付の事務連絡を回覧します: 「テスト通知」\n本文のテキストです。"
    config = _config_with_circulation(
        mode="full_text",
        enabled=True,
        announcements=[
            {
                "mutation_id": "clarify_elderly_understanding_sales_only",
                "doc_id": "DFH-SAL-902",
                "tick": 1,
                "visible_roles": ["sales"],
                "message": message,
                "digest": "digest-text",
            }
        ],
    )
    _write_config(root, config)
    _write_ledger(
        root,
        [
            {
                "tick": 1,
                "event_type": "inbox_delivered",
                "payload": {"to_seat": "emp-A", "message": {"kind": "timed_notice", "tick": 1, "notice": "document_circulation", "detail": message}},
            }
        ],
    )
    read_row = {
        "tool": "read_document",
        "success": True,
        "seat_id": "emp-A",
        "tick": 2,
        "args": {"doc_id": "DFH-SAL-902"},
    }
    (root / "attempts.jsonl").write_text(json.dumps(read_row) + "\n", encoding="utf-8")

    exposure = _run_exposure(root, "DFH-SAL-902", mutation_id="clarify_elderly_understanding_sales_only")

    assert exposure["exposed"] is True
    assert exposure["basis"] == "circulation_delivery"
    assert exposure["content_read"] is True
    assert exposure["content_read_detail"]["read_document_hits"]


def test_exposure_delivery_correlated_by_mutation_not_by_any_circular_in_run(tmp_path: Path) -> None:
    """A different mutation's circular being delivered in the same run must
    NOT count as exposure for THIS injection -- correlation is by
    mutation_id/doc_id/content match, not "any circular delivered"."""
    root = tmp_path / "run0"
    other_message = "本日付の事務連絡を回覧します: 「別件の通知」\n別件の本文です。"
    config = _config_with_circulation(
        mode="full_text",
        enabled=True,
        announcements=[
            {
                "mutation_id": "dangling_fill_search_key_stub",
                "doc_id": "DFH-CUS-006",
                "tick": 1,
                "visible_roles": ["sales"],
                "message": other_message,
                "digest": "digest-other",
            }
        ],
    )
    _write_config(root, config)
    _write_ledger(
        root,
        [
            {
                "tick": 1,
                "event_type": "inbox_delivered",
                "payload": {"to_seat": "emp-A", "message": {"kind": "timed_notice", "tick": 1, "notice": "document_circulation", "detail": other_message}},
            }
        ],
    )
    (root / "attempts.jsonl").write_text("", encoding="utf-8")

    exposure = _run_exposure(root, "DFH-SAL-902", mutation_id="clarify_elderly_understanding_sales_only")

    assert exposure["exposed"] is False


# ---------------------------------------------------------------------------
# Backward compatibility: title-only-era (legacy) bundles are still scoreable
# with the OLD (read-based) exposure semantics -- exposure falls back to
# read_document/basis-citation evidence when config mode is not "full_text".
# ---------------------------------------------------------------------------


def test_exposure_falls_back_to_read_based_for_legacy_title_only_mode(tmp_path: Path) -> None:
    """A legacy era-5 bundle recording mode=='title_only' (the fixture
    shape an older sealed campaign's config.json would carry) must NOT gain
    delivery-based exposure -- title-only delivery never carried the
    document's content, so a circular delivered under that mode is
    insufficient; exposure still requires the original read/citation
    evidence."""
    root = tmp_path / "run0"
    digest = "本日付の事務連絡を回覧します: 「営業部向け高齢のお客さま説明記録の連絡」。詳細は文書一覧をご確認ください。"
    config = _config_with_circulation(
        mode="title_only",
        enabled=True,
        announcements=[
            {
                "mutation_id": "clarify_elderly_understanding_sales_only",
                "doc_id": "DFH-SAL-902",
                "tick": 1,
                "visible_roles": ["sales"],
                "digest": digest,
            }
        ],
    )
    _write_config(root, config)
    _write_ledger(
        root,
        [
            {
                "tick": 1,
                "event_type": "inbox_delivered",
                "payload": {"to_seat": "emp-A", "message": {"kind": "timed_notice", "tick": 1, "notice": "document_circulation", "detail": digest}},
            }
        ],
    )
    # No read_document/basis-citation evidence at all -- this is exactly the
    # era-5 raw-data-audit finding: the title-only circular was delivered but
    # never read.
    (root / "attempts.jsonl").write_text("", encoding="utf-8")

    exposure = _run_exposure(root, "DFH-SAL-902", mutation_id="clarify_elderly_understanding_sales_only")

    assert exposure["exposed"] is False
    assert exposure["mode"] == "title_only"
    assert exposure["basis"] == "content_read"
    assert exposure["content_read"] is False


def test_exposure_legacy_bundle_with_no_circulation_recorded_at_all_falls_back(tmp_path: Path) -> None:
    """A bundle with no world.corpus.circulation block at all (pre-circulation
    era, or a minimal fixture) has mode=='' and must fall back to read-based
    exposure -- unaffected by this PR."""
    root = tmp_path / "run0"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({"world": {"corpus": {}}}), encoding="utf-8")
    read_row = {
        "tool": "read_document",
        "success": True,
        "seat_id": "emp-A",
        "tick": 2,
        "args": {"doc_id": "DFH-SAL-902"},
    }
    (root / "attempts.jsonl").write_text(json.dumps(read_row) + "\n", encoding="utf-8")

    exposure = _run_exposure(root, "DFH-SAL-902")

    assert exposure["exposed"] is True
    assert exposure["mode"] == ""
    assert exposure["basis"] == "content_read"
    assert exposure["content_read"] is True


def test_holdout_scoring_still_activates_legacy_title_only_bundle_via_read_evidence(tmp_path: Path) -> None:
    """Full end-to-end backward-compat check: a plan scored against a
    title-only-era-shaped bundle (config.json mode=='title_only') still
    activates correctly via the OLD read-based path -- old sealed bundles
    remain scoreable without any config migration."""
    from company_twin.holdout import compute_holdout_detection_rate

    plan = build_holdout_injection_plan(
        Path.cwd(), mutation_ids=["clarify_elderly_understanding_sales_only"], run_roots=["s2_holdout_0"]
    )
    injection = plan["injections"][0]
    run_root = tmp_path / "s2_holdout_0"
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "triage").mkdir(exist_ok=True)
    expected_types = list(injection.get("expected_finding_types") or [])
    rule_hit = {
        f"MON-{i}": {"finding_type": finding_type, "opportunity_count": 3, "hit_count": 1}
        for i, finding_type in enumerate(expected_types)
    }
    (run_root / "triage" / "metrics.json").write_text(
        json.dumps({"stage": "S2", "finding_types": {expected_types[0]: 1} if expected_types else {}, "rule_hit_rate": rule_hit, "detection_miss_rate": {}}),
        encoding="utf-8",
    )
    mutation_id = injection["mutation_id"]
    spec = load_mutation_catalog(Path.cwd())[mutation_id]
    mutation_entry = dict(spec)
    (run_root / "config.json").write_text(
        json.dumps(
            {
                "world": {
                    "corpus": {
                        "mutations": [mutation_entry],
                        "mutation_hash": world_json_hash([mutation_entry]),
                        "effective_corpus_hash": "test-hash",
                        "circulation": {"enabled": True, "mode": "title_only", "announcements": []},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (run_root / "meta.json").write_text(json.dumps({"stage": "S2", "mutation_ids": [mutation_id]}), encoding="utf-8")
    ledger_rows = [{"tick": tick, "event_type": "tick_committed"} for tick in range(1, 3)]
    (run_root / "world_ledger.jsonl").write_text("".join(json.dumps(row) + "\n" for row in ledger_rows), encoding="utf-8")
    target_doc_id = str(injection.get("target_doc_id") or "")
    read_row = {"tool": "read_document", "success": True, "seat_id": "emp-A", "tick": 2, "args": {"doc_id": target_doc_id}}
    (run_root / "attempts.jsonl").write_text(json.dumps(read_row) + "\n", encoding="utf-8")

    result = compute_holdout_detection_rate(
        tmp_path, plan, run_lookup={injection["injection_id"]: run_root}
    )
    row = result["per_injection"][0]
    assert row["activation_summary"]["any_activated"] is True
    assert row["activation"]["per_run"][0]["exposure"]["mode"] == "title_only"
    assert row["activation"]["per_run"][0]["exposure"]["basis"] == "content_read"
