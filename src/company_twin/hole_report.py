"""Hole report: the automatic end product of a deviation sweep (roadmap 1-1).

Input: any directory holding judged branch worlds -- a whole sweep root, one
decision point's output, or an archived `deviation_tree.json` summary. Two
layouts are read, preferring the richer one:

- world bundles: every `*/node.json`, merged with its `outcome_judged.json`
  (the external judge's verdict), `meta.json` (fork position, source world,
  the exact injected action as executed, seed) and `dossier.md` (the plain-
  language chronology, quoted as the record citations);
- summary files: every `deviation_tree.json` when no node files exist, so
  archived results remain reportable after their bundles are gone.

Output: one table of 操作×経路×止めたもの. Every world lands in exactly one
of four sections: stopped by the system, stopped by a person/notice, passed
through to the goal (the hole candidates -- these carry a reproduction recipe
and record citations), or undecided. Nothing is inferred: a world whose
verdict is missing is listed as awaiting judgment, never guessed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .deviation_tree import (
    DEVIATION_TREE_FILENAME,
    DOSSIER_FILENAME,
    JUDGED_FILENAME,
    NODE_FILENAME,
    OUTCOME_ACTION_REFUSED,
    OUTCOME_CAUGHT,
    OUTCOME_GOAL_REACHED,
    OUTCOME_INCONCLUSIVE,
    OUTCOME_UNJUDGED,
    candidate_label,
    candidate_steps,
)

HOLE_REPORT_SCHEMA_VERSION = "company_twin.hole_report.v1"
HOLE_REPORT_JSON = "hole_report.json"
HOLE_REPORT_MD = "hole_report.md"

# Fixed limitation wording (roadmap: 横断して守ること #3). Every rendered
# report carries this verbatim so no reader can mistake the scope.
LIMITATION_NOTE = (
    "この検査は「見つからなかった=無い」を意味しません(網羅の保証はしません)。"
    "また、実在の従業員がこの操作をするという予測でもありません(発生確率は別の問いです)。"
)
# Owner decision 2026-07-29: judge variability is not measured. Proportions
# must always carry this caveat; pass-through paths do not need it because
# each is an individual fact with its own record citations.
VARIABILITY_NOTE = (
    "結末の内訳は、確認役の判定のばらつき(同じ資料の再判定一致率)を測っていない数字です。"
    "素通り経路は判定のばらつきに関係なく、記録の引用と再現手順つきの個別の事実として読めます。"
)


def collect_worlds(root: Path) -> list[dict[str, Any]]:
    """Every judged world under `root`, one dict per world."""
    root = Path(root)
    worlds = [_world_from_bundle(node_file) for node_file in sorted(root.glob("**/" + NODE_FILENAME))]
    if worlds:
        return worlds
    for summary_file in sorted(root.glob("**/" + DEVIATION_TREE_FILENAME)):
        worlds.extend(worlds_from_summary(summary_file))
    if not worlds and root.is_file():
        worlds = worlds_from_summary(root)
    return worlds


def worlds_from_summary(summary_file: Path) -> list[dict[str, Any]]:
    """Worlds read from one archived `deviation_tree.json` (both the explore
    format and the judged format store a `nodes` list)."""
    summary_file = Path(summary_file)
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    source_run_root = summary.get("source_run_root")
    worlds = []
    for node in summary.get("nodes") or []:
        worlds.append(
            _world_record(
                node,
                judged=node.get("judged"),
                meta={"source_run_root": source_run_root} if source_run_root else {},
                dossier_lines=[],
                origin=str(summary_file),
            )
        )
    return worlds


def _world_from_bundle(node_file: Path) -> dict[str, Any]:
    node_dir = node_file.parent
    node = json.loads(node_file.read_text(encoding="utf-8"))
    judged_path = node_dir / JUDGED_FILENAME
    judged = json.loads(judged_path.read_text(encoding="utf-8")) if judged_path.exists() else None
    meta_path = node_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    dossier_path = node_dir / DOSSIER_FILENAME
    dossier_lines = (
        [line[2:] for line in dossier_path.read_text(encoding="utf-8").splitlines() if line.startswith("- ")]
        if dossier_path.exists()
        else []
    )
    return _world_record(node, judged=judged, meta=meta, dossier_lines=dossier_lines, origin=str(node_dir))


def _world_record(
    node: dict[str, Any],
    *,
    judged: dict[str, Any] | None,
    meta: dict[str, Any],
    dossier_lines: list[str],
    origin: str,
) -> dict[str, Any]:
    action = node.get("action") or {}
    outcome = str((judged or {}).get("outcome") or node.get("outcome") or OUTCOME_UNJUDGED)
    return {
        "node_id": node.get("node_id"),
        "depth": node.get("depth"),
        "operation": candidate_label(action) if action else "(不明)",
        "action": action,
        "application_id": node.get("application_id") or _application_from_meta(node, meta) or "(記録なし)",
        "seat_id": node.get("seat_id"),
        "outcome": outcome,
        "judged": judged,
        "evidence": node.get("evidence") or {},
        "meta": {
            "source_run_root": meta.get("source_run_root"),
            "fork_ordinal": meta.get("fork_ordinal", node.get("fork_ordinal")),
            "fork_tick": meta.get("fork_tick"),
            "seed": meta.get("seed"),
            "injected_action": meta.get("injected_action"),
        },
        "dossier_lines": dossier_lines,
        "origin": origin,
    }


def _application_from_meta(node: dict[str, Any], meta: dict[str, Any]) -> str | None:
    injected = meta.get("injected_action") or {}
    for step in injected.get("steps") or [injected]:
        application_id = (step.get("args") or {}).get("application_id") if isinstance(step, dict) else None
        if application_id:
            return str(application_id)
    return None


def _stopped_by(world: dict[str, Any]) -> dict[str, Any]:
    """What stopped this world, with its own record to quote."""
    if world["outcome"] == OUTCOME_ACTION_REFUSED:
        evidence = world["evidence"]
        return {
            "kind": "システムが実行を拒否",
            "detail": str(evidence.get("denied_reason") or evidence.get("error") or ""),
            "refused_step": evidence.get("refused_step"),
            "quotes": [],
        }
    judged = world.get("judged") or {}
    if world["outcome"] == OUTCOME_CAUGHT:
        quotes = [str(item) for item in judged.get("evidence") or []]
        if not quotes:
            # archived rule-based verdicts carry the stopping events instead
            quotes = [
                json.dumps(item, ensure_ascii=False)
                for item in (world.get("evidence") or {}).get("caught_by") or []
            ]
        return {
            "kind": "人・通知が止めた",
            "detail": str(judged.get("rationale") or ""),
            "quotes": quotes,
        }
    return {"kind": "止めたものなし", "detail": "", "quotes": []}


def _reproduction(world: dict[str, Any], *, continuation_ticks: int | None) -> dict[str, Any]:
    """Everything needed to re-run one pass-through path: rebuild the source
    world up to the fork, inject the same steps with the same arguments,
    continue for the same number of ticks. The executed arguments come from
    the bundle's own meta when available -- the report never re-derives them."""
    meta = world["meta"]
    injected = meta.get("injected_action") or world.get("action") or {}
    steps = (
        [{"tool": step.get("tool"), "args": step.get("args") or {}} for step in injected["steps"]]
        if isinstance(injected.get("steps"), list)
        else [{"tool": injected.get("tool"), "args": injected.get("args") or {}}]
    )
    return {
        "source_run_root": meta.get("source_run_root"),
        "fork_ordinal": meta.get("fork_ordinal"),
        "seat_id": world.get("seat_id"),
        "steps": steps,
        "continuation_ticks": continuation_ticks,
        "seed": meta.get("seed"),
        "world_bundle": world.get("origin"),
    }


def _citations(world: dict[str, Any]) -> list[str]:
    """Record citations for one pass-through path: the judge's quoted
    evidence first, then the dossier lines showing the case's own lifecycle
    (submission through delivery), which are the world's primary record."""
    quotes = [str(item) for item in (world.get("judged") or {}).get("evidence") or []]
    application_id = str(world.get("application_id") or "")
    for line in world.get("dossier_lines") or []:
        if application_id and application_id in line and ("案件" in line or "承認" in line):
            if line not in quotes:
                quotes.append(line)
    if not quotes:
        goal_at = (world.get("evidence") or {}).get("goal_at")
        if goal_at:
            quotes.append(json.dumps(goal_at, ensure_ascii=False))
    return quotes[:12]


def build_hole_report(root: Path, *, continuation_ticks: int | None = None) -> dict[str, Any]:
    """The 操作×経路×止めたもの table for every judged world under `root`."""
    worlds = collect_worlds(Path(root))
    outcome_counts: dict[str, int] = {}
    per_operation: dict[str, dict[str, int]] = {}
    holes: list[dict[str, Any]] = []
    stopped: list[dict[str, Any]] = []
    refused: dict[str, int] = {}
    undecided: list[dict[str, Any]] = []

    for world in worlds:
        outcome = world["outcome"]
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        per_operation.setdefault(world["operation"], {})
        per_operation[world["operation"]][outcome] = per_operation[world["operation"]].get(outcome, 0) + 1
        stopped_by = _stopped_by(world)
        row = {
            "node_id": world["node_id"],
            "operation": world["operation"],
            "application_id": world["application_id"],
            "seat_id": world["seat_id"],
            "source_run_root": world["meta"].get("source_run_root"),
            "stopped_by": stopped_by,
        }
        if outcome == OUTCOME_GOAL_REACHED:
            holes.append(
                {
                    **row,
                    "rationale": str((world.get("judged") or {}).get("rationale") or ""),
                    "challenge_notes": [str(x) for x in (world.get("judged") or {}).get("challenge_notes") or []],
                    "citations": _citations(world),
                    "reproduction": _reproduction(world, continuation_ticks=continuation_ticks),
                }
            )
        elif outcome == OUTCOME_CAUGHT:
            stopped.append(row)
        elif outcome == OUTCOME_ACTION_REFUSED:
            key = stopped_by["detail"] or "(理由の記録なし)"
            refused[key] = refused.get(key, 0) + 1
        else:
            undecided.append({**row, "outcome": outcome})

    return {
        "schema_version": HOLE_REPORT_SCHEMA_VERSION,
        "data_root": str(root),
        "world_count": len(worlds),
        "outcome_counts": outcome_counts,
        "per_operation": {
            operation: counts for operation, counts in sorted(per_operation.items())
        },
        "holes": holes,
        "stopped": stopped,
        "refused_reasons": dict(sorted(refused.items(), key=lambda kv: -kv[1])),
        "undecided": undecided,
        "notes": {"limitations": LIMITATION_NOTE, "variability": VARIABILITY_NOTE},
    }


_OUTCOME_LABELS = {
    OUTCOME_ACTION_REFUSED: "システムが実行を拒否",
    OUTCOME_GOAL_REACHED: "誰にも止められず完了まで到達",
    OUTCOME_CAUGHT: "止められた",
    OUTCOME_INCONCLUSIVE: "期間内に決着せず",
    OUTCOME_UNJUDGED: "判定待ち",
}


def _steps_text(steps: list[dict[str, Any]]) -> str:
    parts = []
    for step in steps:
        args = step.get("args") or {}
        key_args = {
            name: args[name]
            for name in ("application_id", "customer_id", "contract_id", "approval_id", "delivery_id")
            if args.get(name)
        }
        rendered = ", ".join(f"{name}={value}" for name, value in key_args.items())
        parts.append(f"{step.get('tool')}({rendered})" if rendered else str(step.get("tool")))
    return " → ".join(parts)


def render_hole_report_md(report: dict[str, Any]) -> str:
    """The report as one plain-language document."""
    lines: list[str] = []
    lines.append("# 統制の穴のレポート(操作×経路×止めたもの)")
    lines.append("")
    lines.append(f"- 対象データ: `{report['data_root']}`")
    lines.append(f"- 世界数: {report['world_count']}")
    lines.append("")
    lines.append(f"> {report['notes']['limitations']}")
    lines.append("")
    lines.append("## 結末の内訳")
    lines.append("")
    lines.append("| 結末 | 世界数 |")
    lines.append("|---|---|")
    for outcome, count in sorted(report["outcome_counts"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {_OUTCOME_LABELS.get(outcome, outcome)} | {count} |")
    lines.append("")
    lines.append(f"> {report['notes']['variability']}")
    lines.append("")
    lines.append("## 操作ごとの結末")
    lines.append("")
    lines.append("| 操作 | " + " | ".join(_OUTCOME_LABELS.values()) + " |")
    lines.append("|---|" + "---|" * len(_OUTCOME_LABELS))
    for operation, counts in report["per_operation"].items():
        cells = " | ".join(str(counts.get(outcome, 0)) for outcome in _OUTCOME_LABELS)
        lines.append(f"| {operation} | {cells} |")
    lines.append("")

    lines.append(f"## 素通りした経路 = 設計上の穴の候補({len(report['holes'])}件)")
    lines.append("")
    if not report["holes"]:
        lines.append("(該当なし)")
        lines.append("")
    for index, hole in enumerate(report["holes"], start=1):
        repro = hole["reproduction"]
        lines.append(f"### 穴の候補 {index}: {hole['operation']}(案件 {hole['application_id']}、世界 {hole['node_id']})")
        lines.append("")
        if hole["rationale"]:
            lines.append(f"- 確認役の判定理由: {hole['rationale']}")
        if hole["challenge_notes"]:
            lines.append("- 止まらなかったが出ていた指摘:")
            for note in hole["challenge_notes"]:
                lines.append(f"  - {note}")
        lines.append("- 記録の引用:")
        for quote in hole["citations"] or ["(引用可能な記録が見つからない)"]:
            lines.append(f"  - {quote}")
        lines.append("- 再現手順:")
        lines.append(f"  1. 分岐元の世界 `{repro['source_run_root']}` を、記録の {repro['fork_ordinal']} 行目まで複製する")
        lines.append(f"  2. 席 {repro['seat_id']} の操作として {_steps_text(repro['steps'])} を差し込む")
        ticks = repro.get("continuation_ticks")
        lines.append(
            f"  3. お客様も動く状態で継続する" + (f"({ticks}コマ)" if ticks else "(元の実行と同じコマ数)")
        )
        lines.append(f"  - 実行済みの世界一式: `{repro['world_bundle']}`")
        lines.append("")

    lines.append(f"## 止められた経路({len(report['stopped'])}件)")
    lines.append("")
    for row in report["stopped"]:
        detail = row["stopped_by"]["detail"]
        lines.append(f"- {row['operation']}(案件 {row['application_id']}、世界 {row['node_id']}): {detail}")
        for quote in row["stopped_by"]["quotes"]:
            lines.append(f"  - 引用: {quote}")
    if not report["stopped"]:
        lines.append("(該当なし)")
    lines.append("")

    lines.append("## システムが実行を拒否した操作(理由別)")
    lines.append("")
    for reason, count in report["refused_reasons"].items():
        lines.append(f"- {count}件: {reason}")
    if not report["refused_reasons"]:
        lines.append("(該当なし)")
    lines.append("")

    lines.append(f"## 決着しなかった・判定待ちの世界({len(report['undecided'])}件)")
    lines.append("")
    for row in report["undecided"]:
        lines.append(f"- {row['operation']}(案件 {row['application_id']}、世界 {row['node_id']}): {_OUTCOME_LABELS.get(row['outcome'], row['outcome'])}")
    if not report["undecided"]:
        lines.append("(該当なし)")
    lines.append("")
    return "\n".join(lines) + "\n"


def write_hole_report(
    root: Path, *, output_dir: Path | None = None, continuation_ticks: int | None = None
) -> dict[str, str]:
    """Build and persist both artifacts; returns their paths."""
    root = Path(root)
    output_dir = Path(output_dir) if output_dir is not None else (root if root.is_dir() else root.parent)
    report = build_hole_report(root, continuation_ticks=continuation_ticks)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / HOLE_REPORT_JSON
    md_path = output_dir / HOLE_REPORT_MD
    json_path.write_bytes((json.dumps(report, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    md_path.write_text(render_hole_report_md(report), encoding="utf-8")
    return {"json": str(json_path), "md": str(md_path)}
