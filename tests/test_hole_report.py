"""Hole report (roadmap 1-1). Zero spend: fixture bundles on disk stand in
for a finished sweep, so the whole report -- grouping, reproduction recipes,
record citations, the fixed limitation wording -- is verified for free."""
from __future__ import annotations

import json
from pathlib import Path

from company_twin.hole_report import (
    LIMITATION_NOTE,
    VARIABILITY_NOTE,
    build_hole_report,
    render_hole_report_md,
    write_hole_report,
)


def _world_bundle(
    root: Path,
    node_id: str,
    *,
    outcome: str,
    judged: dict | None = None,
    evidence: dict | None = None,
    steps: list[dict] | None = None,
) -> Path:
    node_dir = root / node_id
    node_dir.mkdir(parents=True)
    action = (
        {"steps": [{"tool": s["tool"], "args": {}} for s in steps], "why": "試験用"}
        if steps and len(steps) > 1
        else {"tool": (steps or [{"tool": "complete_contract"}])[0]["tool"], "args": {}, "why": "試験用"}
    )
    node = {
        "node_id": node_id,
        "parent_id": "root",
        "depth": 0,
        "action": action,
        "application_id": "APP-H-01",
        "seat_id": "emp-C",
        "fork_ordinal": 12,
        "outcome": outcome if judged is None else "awaiting_judgement",
        "run_root": str(node_dir),
        "evidence": evidence or {},
    }
    (node_dir / "node.json").write_text(json.dumps(node, ensure_ascii=False), encoding="utf-8")
    if judged is not None:
        (node_dir / "outcome_judged.json").write_text(json.dumps(judged, ensure_ascii=False), encoding="utf-8")
    (node_dir / "meta.json").write_text(
        json.dumps(
            {
                "source_run_root": "/runs/source_world",
                "fork_ordinal": 12,
                "seed": 7,
                "injected_action": (
                    {"steps": [{"tool": s["tool"], "args": s.get("args", {"application_id": "APP-H-01"})} for s in steps], "why": "試験用"}
                    if steps and len(steps) > 1
                    else {"tool": (steps or [{"tool": "complete_contract"}])[0]["tool"], "args": {"application_id": "APP-H-01", "contract_id": "CT-1"}}
                ),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (node_dir / "dossier.md").write_text(
        "# 分岐世界の記録(案件 APP-H-01)\n\n"
        "- 3コマ目: 案件 APP-H-01: 申込が提出された\n"
        "- 4コマ目: 案件 APP-H-01: 契約が完了した\n"
        "- 4コマ目: 案件 APP-H-01: 書面が交付された\n",
        encoding="utf-8",
    )
    return node_dir


def test_every_world_lands_in_exactly_one_section(tmp_path: Path) -> None:
    root = tmp_path / "sweep"
    _world_bundle(
        root, "p1-d0b00", outcome="goal_reached",
        judged={"outcome": "goal_reached", "rationale": "誰も止めず書面交付まで到達した。",
                "evidence": ["4コマ目: 案件 APP-H-01: 契約が完了した"], "challenge_notes": ["承認は?という指摘のみ"]},
    )
    _world_bundle(
        root, "p1-d0b01", outcome="caught",
        judged={"outcome": "caught", "rationale": "管理部門が規程を引用して保留した。",
                "evidence": ["5コマ目: 保留(理由: 証跡不足)"], "challenge_notes": []},
    )
    _world_bundle(root, "p1-d0b02", outcome="action_refused_by_system",
                  evidence={"denied_reason": "approval requires manager role"})
    _world_bundle(
        root, "p1-d0b03", outcome="inconclusive",
        judged={"outcome": "inconclusive", "rationale": "期間内に決着しなかった。", "evidence": []},
    )

    report = build_hole_report(root)
    assert report["world_count"] == 4
    assert report["outcome_counts"] == {
        "goal_reached": 1, "caught": 1, "action_refused_by_system": 1, "inconclusive": 1,
    }
    assert len(report["holes"]) == 1
    assert len(report["stopped"]) == 1
    assert report["refused_reasons"] == {"approval requires manager role": 1}
    assert len(report["undecided"]) == 1


def test_hole_entry_carries_reproduction_and_citations(tmp_path: Path) -> None:
    root = tmp_path / "sweep"
    _world_bundle(
        root, "p2-d0b00", outcome="goal_reached",
        judged={"outcome": "goal_reached", "rationale": "到達。", "evidence": ["4コマ目: 案件 APP-H-01: 書面が交付された"]},
        steps=[{"tool": "record_customer_contact", "args": {"customer_id": "CUS-H-01"}},
               {"tool": "submit_application", "args": {"application_id": "APP-H-01"}}],
    )
    report = build_hole_report(root, continuation_ticks=10)
    hole = report["holes"][0]
    # the recipe replays the same fork with the same EXECUTED arguments
    repro = hole["reproduction"]
    assert repro["source_run_root"] == "/runs/source_world"
    assert repro["fork_ordinal"] == 12
    assert [step["tool"] for step in repro["steps"]] == ["record_customer_contact", "submit_application"]
    assert repro["continuation_ticks"] == 10 and repro["seed"] == 7
    # citations: judge's quotes first, then the case's own lifecycle records
    assert "書面が交付された" in hole["citations"][0]
    assert any("契約が完了した" in quote for quote in hole["citations"])
    assert hole["operation"] == "record_customer_contact → submit_application(2手)"


def test_report_reads_archived_summaries_when_bundles_are_gone(tmp_path: Path) -> None:
    summary = {
        "schema_version": "company_twin.deviation_tree.v1",
        "source_run_root": "/runs/old_source",
        "nodes": [
            {"node_id": "root-d0b00", "depth": 0,
             "action": {"tool": "complete_contract", "args": {}, "why": "急ぐ"},
             "outcome": "goal_reached", "run_root": None,
             "evidence": {"goal_at": {"ordinal": 30, "tick": 9, "event": "contract_completed"}}},
            {"node_id": "root-d0b01", "depth": 0,
             "action": {"tool": "deliver_documents", "args": {}, "why": "先に"},
             "outcome": "action_refused_by_system", "run_root": None,
             "evidence": {"denied_reason": "document delivery requires contracted state"}},
        ],
    }
    path = tmp_path / "deviation_tree.json"
    path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    report = build_hole_report(path)
    assert report["world_count"] == 2
    assert len(report["holes"]) == 1
    # with no judge and no dossier, the mechanical goal fact is still quoted
    assert "contract_completed" in report["holes"][0]["citations"][0]
    assert report["holes"][0]["reproduction"]["source_run_root"] == "/runs/old_source"


def test_rendered_report_carries_the_fixed_notes_and_recipe(tmp_path: Path) -> None:
    root = tmp_path / "sweep"
    _world_bundle(
        root, "p3-d0b00", outcome="goal_reached",
        judged={"outcome": "goal_reached", "rationale": "到達した。", "evidence": ["4コマ目: 契約が完了した"]},
    )
    paths = write_hole_report(root, continuation_ticks=10)
    text = Path(paths["md"]).read_text(encoding="utf-8")
    assert LIMITATION_NOTE in text
    assert VARIABILITY_NOTE in text  # proportions never appear without the caveat
    assert "再現手順" in text and "12 行目まで複製" in text
    assert "素通りした経路 = 設計上の穴の候補(1件)" in text
    assert json.loads(Path(paths["json"]).read_text(encoding="utf-8"))["world_count"] == 1


def test_unjudged_worlds_are_listed_never_guessed(tmp_path: Path) -> None:
    root = tmp_path / "sweep"
    _world_bundle(root, "p4-d0b00", outcome="awaiting_judgement")
    report = build_hole_report(root)
    assert report["outcome_counts"] == {"awaiting_judgement": 1}
    assert report["holes"] == [] and report["stopped"] == []
    assert report["undecided"][0]["outcome"] == "awaiting_judgement"
    assert "判定待ち" in render_hole_report_md(report)
