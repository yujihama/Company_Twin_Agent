"""Answer check: planted defects vs the holes the sweep actually found
(roadmap 1-5).

The simulated company's documents carry twelve deliberately planted defects
(data/design/RCM.md, layer 3). Because the environment is hand-built, they
are the ground truth this harness can be scored against: how many of the
twelve does a sweep re-discover as a pass-through path?

The matching judgment itself is NOT mechanical -- whether a found path's
mechanism IS a given defect takes reading. It belongs to the external
confirmation agent, one defect at a time, same discipline as the per-world
verdicts: a fixed prompt, a JSON verdict, fail-closed storage, citations
required for any positive claim. This module builds the prompts, validates
and stores the verdicts, and assembles the scoring document; it never
decides a match itself.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ANSWER_CHECK_SCHEMA_VERSION = "company_twin.answer_check.v1"

# The twelve planted defects, from data/design/RCM.md (layer 3). The ids are
# file locators into the design docs; the descriptions are what the verifier
# reads. `risk` is the loss event the defect amplifies, in plain words.
PLANTED_DEFECTS: tuple[dict[str, str], ...] = (
    {
        "defect_id": "AMB-01",
        "description": "「要承認」と「例外承認」の定義が互いを参照して循環しており、どちらの承認が要るのか規程から決められない",
        "risk": "承認のないまま取引が実行される",
    },
    {
        "defect_id": "AMB-02",
        "description": "高齢のお客様の年齢の定義が規程に存在しない(目標値だけが存在する)",
        "risk": "高齢者保護の手続を踏まないまま契約に至る・適合しない商品が販売される",
    },
    {
        "defect_id": "AMB-04d",
        "description": "口頭やチャットでの承認が認められるかどうかが、3つの文書で食い違っている",
        "risk": "承認のないまま取引が実行される",
    },
    {
        "defect_id": "AMB-08",
        "description": "お客様への確認項目の初期値が文書間で食い違っている",
        "risk": "適合しない商品が販売される",
    },
    {
        "defect_id": "AMB-09",
        "description": "代わりの承認者の資格・権限がどの規程にも定義されていない",
        "risk": "承認のないまま取引が実行される",
    },
    {
        "defect_id": "AMB-10",
        "description": "資料選択機能に従う義務があるのか、助言に過ぎないのかが決められていない",
        "risk": "(参考)誤った資料で契約する",
    },
    {
        "defect_id": "AMB-11",
        "description": "新旧の名称が記録・検索の中で混在しており、記録のたどり先が分かれる",
        "risk": "(参考)証跡の追跡が分かれる",
    },
    {
        "defect_id": "AMB-12",
        "description": "「〜の場合は」の「場合」がどんな場合を指すのか定義されていない",
        "risk": "適合しない商品が販売される",
    },
    {
        "defect_id": "CONTRA-01",
        "description": "商品をどの部署が主管するのかが規程間で分裂している",
        "risk": "承認のないまま取引が実行される",
    },
    {
        "defect_id": "SCC-01",
        "description": "手続の完了条件が月の途中で変更されている",
        "risk": "(参考)証跡が揃わない",
    },
    {
        "defect_id": "STR-01",
        "description": "実在しない文書への参照が延べ83箇所ある(参照先を確認できない)",
        "risk": "適合しない商品の販売・高齢者保護の手続の不履行",
    },
    {
        "defect_id": "STR-02r",
        "description": "統制ポイントの記述の粒度が粗く、個々に何を確認するのかが特定できない",
        "risk": "承認のないまま取引が実行される",
    },
)

REDISCOVERY_VERDICTS = ("rediscovered", "partially_rediscovered", "not_rediscovered")

ANSWER_CHECK_PROMPT_TEMPLATE = """あなたは、模擬的な金融会社に意図して仕込まれた規程の欠陥が、
自動検査で再発見されたかどうかを照合する担当です。

対象の欠陥:
- 欠陥の内容: {description}
- この欠陥が増幅するリスク: {risk}

添付の資料は、検査が見つけた「素通りした経路」(誰にも止められず完了まで到達した操作)の
一覧です。資料だけを根拠に、次の問いに答えてください。

問い: 資料の中に、上の欠陥【そのもの】を通り道として使った素通り経路がありますか?
つまり、その経路が素通りできた理由が、この欠陥の内容(定義の不在・文書間の食い違い等)に
よって説明できますか?

判定は次の3択です:
- "rediscovered": この欠陥を通り道とする素通り経路が資料にある
- "partially_rediscovered": 関係する経路はあるが、素通りの理由がこの欠陥だけでは説明できない
- "not_rediscovered": この欠陥を通り道とする経路は資料にない

注意:
- 資料にない事実を推測で補わないこと。迷う場合は "not_rediscovered" に倒すこと
- "rediscovered"・"partially_rediscovered" の場合は、該当する経路(世界の識別名)と、
  資料からの引用を必ず添えること

出力は次のJSONのみ(説明文は付けない):
{{"defect_id": "{defect_id}", "verdict": "rediscovered|partially_rediscovered|not_rediscovered", "matching_paths": ["世界の識別名", "..."], "rationale": "判定理由(2〜4文)", "evidence": ["資料からの引用", "..."]}}

----- 資料ここから -----
{holes_document}
----- 資料ここまで -----
"""


def defect_by_id(defect_id: str) -> dict[str, str]:
    for defect in PLANTED_DEFECTS:
        if defect["defect_id"] == defect_id:
            return defect
    raise KeyError(f"unknown planted defect: {defect_id!r}")


def answer_check_prompt(defect_id: str, holes_document: str) -> str:
    """The exact prompt the external verifier receives for one defect."""
    defect = defect_by_id(defect_id)
    return ANSWER_CHECK_PROMPT_TEMPLATE.format(
        defect_id=defect["defect_id"],
        description=defect["description"],
        risk=defect["risk"],
        holes_document=holes_document,
    )


def record_answer_check(output_dir: Path, verdict: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist one defect's verdict. Fail-closed, mirroring the
    per-world judgments: an unknown defect or verdict value is rejected, and
    a positive claim without at least one matching path AND one citation is
    rejected -- a re-discovery that cannot point at its evidence is not one."""
    defect_id = str(verdict.get("defect_id") or "")
    defect_by_id(defect_id)  # raises on unknown
    value = str(verdict.get("verdict") or "")
    if value not in REDISCOVERY_VERDICTS:
        raise ValueError(f"verdict must be one of {REDISCOVERY_VERDICTS}, got {value!r}")
    if not str(verdict.get("rationale") or "").strip():
        raise ValueError("an answer-check verdict requires a non-empty rationale")
    matching_paths = [str(item) for item in verdict.get("matching_paths") or []]
    evidence = [str(item) for item in verdict.get("evidence") or []]
    if value in ("rediscovered", "partially_rediscovered") and (not matching_paths or not evidence):
        raise ValueError("a positive verdict requires at least one matching path and one citation")
    stored = {
        "defect_id": defect_id,
        "verdict": value,
        "matching_paths": matching_paths,
        "rationale": str(verdict.get("rationale")),
        "evidence": evidence,
        "judge": str(verdict.get("judge") or "subagent"),
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"answer_check_{defect_id}.json").write_bytes(
        (json.dumps(stored, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    )
    return stored


def assemble_answer_check(output_dir: Path) -> dict[str, Any]:
    """All twelve defects against whatever verdicts exist. A defect with no
    stored verdict is listed as unjudged -- never counted on either side."""
    output_dir = Path(output_dir)
    per_defect: list[dict[str, Any]] = []
    counts = {value: 0 for value in REDISCOVERY_VERDICTS}
    unjudged: list[str] = []
    for defect in PLANTED_DEFECTS:
        path = output_dir / f"answer_check_{defect['defect_id']}.json"
        if not path.exists():
            unjudged.append(defect["defect_id"])
            per_defect.append({**defect, "verdict": "unjudged"})
            continue
        stored = json.loads(path.read_text(encoding="utf-8"))
        counts[stored["verdict"]] = counts.get(stored["verdict"], 0) + 1
        per_defect.append({**defect, **stored})
    return {
        "schema_version": ANSWER_CHECK_SCHEMA_VERSION,
        "defect_count": len(PLANTED_DEFECTS),
        "counts": counts,
        "unjudged": unjudged,
        "per_defect": per_defect,
    }


_VERDICT_LABELS = {
    "rediscovered": "再発見された",
    "partially_rediscovered": "関係する経路はあるが単独では説明できない",
    "not_rediscovered": "再発見されなかった",
    "unjudged": "未照合",
}


def render_answer_check_md(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# 答え合わせ: 仕込んだ欠陥12箇所と検査結果の照合")
    lines.append("")
    lines.append(
        f"- 再発見 {summary['counts']['rediscovered']} / 部分的 {summary['counts']['partially_rediscovered']}"
        f" / 再発見されず {summary['counts']['not_rediscovered']} / 未照合 {len(summary['unjudged'])}"
        f"(全{summary['defect_count']}箇所)"
    )
    lines.append("")
    lines.append("| 欠陥 | 内容 | 照合結果 | 該当経路 |")
    lines.append("|---|---|---|---|")
    for row in summary["per_defect"]:
        paths = ", ".join(row.get("matching_paths") or []) or "—"
        lines.append(
            f"| {row['defect_id']} | {row['description']} | {_VERDICT_LABELS[row['verdict']]} | {paths} |"
        )
    lines.append("")
    for row in summary["per_defect"]:
        if row["verdict"] == "unjudged":
            continue
        lines.append(f"## {row['defect_id']}: {_VERDICT_LABELS[row['verdict']]}")
        lines.append("")
        lines.append(f"- 判定理由: {row.get('rationale', '')}")
        for quote in row.get("evidence") or []:
            lines.append(f"- 引用: {quote}")
        lines.append("")
    return "\n".join(lines) + "\n"
