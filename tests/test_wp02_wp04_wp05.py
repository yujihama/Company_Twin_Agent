import json
from pathlib import Path

from company_twin.ab_testing import write_prompt_mode_ab_report
from company_twin.oracles import write_triage
from company_twin.readiness import _semantic_grounding_check
from company_twin.semantic_grounding import (
    G3_JUDGE_PROMPT_VERSION,
    G3_PROMPT_TRANSFORM,
    NEGATIVE_CALIBRATION_CATEGORIES,
    LocalSemanticJudge,
    _cache_key,
    _judge_prompt,
    evaluate_semantic_grounding_campaign,
    evaluate_semantic_grounding_run,
    export_g3_calibration_samples,
    load_g3_calibration_cases,
    score_g3_calibration_file,
    summarize_g3_calibration_scores,
)


class _FakeOpenRouterJudge:
    backend = "openrouter"
    model = "openrouter:test-g3"

    def judge(self, *, cited_text: str, construal: str, decision: str, evidence_plan: str) -> dict:
        return LocalSemanticJudge().judge(cited_text=cited_text, construal=construal, decision=decision, evidence_plan=evidence_plan)


class _FailingAfterFirstJudge:
    backend = "openrouter"
    model = "openrouter:test-flaky-g3"

    def __init__(self) -> None:
        self.calls = 0

    def judge(self, *, cited_text: str, construal: str, decision: str, evidence_plan: str) -> dict:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("transient connection failure")
        return {"label": "supported", "confidence": 0.9, "rationale": "first row ok"}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _write_g3_supported_run(run_root: Path) -> None:
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "meta.json").write_text(json.dumps({"stage": "S2", "prompt_mode": "measurement", "seed": 0}), encoding="utf-8")
    _write_jsonl(
        run_root / "attempts.jsonl",
        [
            {
                "tick": 1,
                "seat_id": "emp-A",
                "tool": "read_document",
                "args": {"doc_id": "DFH-SAL-021"},
                "success": True,
                "result": {
                    "version": "1.1",
                    "citation_handle": "read:DFH-SAL-021:v1.1:abc",
                    "text": "高齢者への販売では理解度を確認し、必要に応じて家族同席や管理者確認を記録する。",
                },
                "origin": "agent",
            },
            {
                "tick": 2,
                "seat_id": "emp-A",
                "tool": "record_customer_contact",
                "args": {"application_id": "APP-1"},
                "success": True,
                "result": {},
                "origin": "agent",
            },
        ],
    )
    _write_jsonl(
        run_root / "basis_records.jsonl",
        [
            {
                "basis_id": "BASIS-1",
                "tick": 2,
                "seat_id": "emp-A",
                "action_id": "CONTACT-1",
                "trigger_event": "record_customer_contact",
                "retrieved": [{"doc_id": "DFH-SAL-021", "version": "1.1", "citation_handle": "read:DFH-SAL-021:v1.1:abc"}],
                "construal": "年齢だけではなく理解度確認と管理者確認の要否を見る",
                "decision": "顧客説明を記録して必要なら管理者に確認する",
                "evidence_plan": "説明記録と確認結果を残す",
                "g1_citation_handle_exists": True,
                "g2_prior_read": True,
                "g3_machine_heuristic": "unsupported",
            }
        ],
    )
    _write_jsonl(run_root / "world_ledger.jsonl", [])
    _write_jsonl(run_root / "store_events.jsonl", [])


def test_g3_proxy_rate_is_materialized_without_canonical_semantic_rate(tmp_path: Path) -> None:
    run_root = tmp_path / "s2_seed0"
    _write_g3_supported_run(run_root)

    triage = write_triage(run_root)
    report = json.loads((run_root / "g3_semantic_grounding.json").read_text(encoding="utf-8"))

    assert triage["metrics"]["grounding_semantic_all3_rate"] is None
    assert triage["metrics"]["grounding_semantic_all3_rate_proxy"] == 1.0
    assert report["grounding_semantic_all3_rate"] is None
    assert report["grounding_semantic_all3_rate_proxy"] == 1.0
    assert report["judge"]["readiness_eligible"] is False
    assert report["semantic_all3_count"] == 1
    assert "AMB-" not in json.dumps(report, ensure_ascii=False)
    assert (run_root / "g3_entailment_cache.json").exists()


def test_readiness_rejects_proxy_only_semantic_grounding(tmp_path: Path) -> None:
    run_root = tmp_path / "s2_seed0"
    _write_g3_supported_run(run_root)
    write_triage(run_root)

    result = _semantic_grounding_check(tmp_path, semantic_threshold=0.8)

    assert not result["passed"]
    assert "disallowed_backend_metrics=1" in result["detail"]


def test_readiness_accepts_allowlisted_openrouter_semantic_grounding(tmp_path: Path) -> None:
    run_root = tmp_path / "s2_seed0"
    _write_g3_supported_run(run_root)
    evaluate_semantic_grounding_run(run_root, judge=_FakeOpenRouterJudge())
    triage = write_triage(run_root)

    assert triage["metrics"]["grounding_semantic_all3_rate"] == 1.0
    assert triage["metrics"]["grounding_semantic_all3_rate_proxy"] is None
    result = _semantic_grounding_check(tmp_path, semantic_threshold=0.8)
    assert result["passed"]


def test_g3_flushes_cache_between_live_judge_rows(tmp_path: Path) -> None:
    run_root = tmp_path / "s2_seed0"
    _write_g3_supported_run(run_root)
    _write_jsonl(
        run_root / "attempts.jsonl",
        [
            {
                "tick": 1,
                "seat_id": "emp-A",
                "tool": "read_document",
                "args": {"doc_id": "DFH-SAL-021"},
                "success": True,
                "result": {
                    "version": "1.1",
                    "citation_handle": "read:DFH-SAL-021:v1.1:first",
                    "text": "本人確認ではeKYCと制裁リスト非該当を確認する。",
                },
                "origin": "agent",
            },
            {
                "tick": 2,
                "seat_id": "emp-A",
                "tool": "read_document",
                "args": {"doc_id": "DFH-SAL-022"},
                "success": True,
                "result": {
                    "version": "1.1",
                    "citation_handle": "read:DFH-SAL-022:v1.1:second",
                    "text": "高齢者への販売では理解度確認を記録する。",
                },
                "origin": "agent",
            },
        ],
    )
    _write_jsonl(
        run_root / "basis_records.jsonl",
        [
            {
                "basis_id": "BASIS-1",
                "tick": 3,
                "seat_id": "emp-A",
                "action_id": "CONTACT-1",
                "trigger_event": "record_customer_contact",
                "retrieved": [{"doc_id": "DFH-SAL-021", "version": "1.1", "citation_handle": "read:DFH-SAL-021:v1.1:first"}],
                "construal": "eKYCと制裁リスト確認が必要",
                "decision": "本人確認を進める",
                "evidence_plan": "確認結果を残す",
                "g1_citation_handle_exists": True,
                "g2_prior_read": True,
            },
            {
                "basis_id": "BASIS-2",
                "tick": 4,
                "seat_id": "emp-A",
                "action_id": "CONTACT-2",
                "trigger_event": "record_customer_contact",
                "retrieved": [{"doc_id": "DFH-SAL-022", "version": "1.1", "citation_handle": "read:DFH-SAL-022:v1.1:second"}],
                "construal": "理解度確認を記録する",
                "decision": "高齢顧客の説明記録を残す",
                "evidence_plan": "理解度確認欄を更新する",
                "g1_citation_handle_exists": True,
                "g2_prior_read": True,
            },
        ],
    )

    try:
        evaluate_semantic_grounding_run(run_root, judge=_FailingAfterFirstJudge())
        assert False, "expected judge failure on second row"
    except RuntimeError as exc:
        assert "transient connection failure" in str(exc)

    cache = json.loads((run_root / "g3_entailment_cache.json").read_text(encoding="utf-8"))
    assert len(cache) == 1
    assert next(iter(cache.values()))["label"] == "supported"


def test_g3_prompt_truncates_long_cited_text(monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_TWIN_G3_CITED_TEXT_MAX_CHARS", "120")

    prompt = _judge_prompt(
        cited_text="A" * 300 + "MIDDLE" + "Z" * 300,
        construal="read policy",
        decision="do action",
        evidence_plan="save evidence",
    )

    assert "[... cited text truncated for G3 prompt ...]" in prompt
    assert "MIDDLE" not in prompt
    assert "read policy" in prompt
    assert "do action" in prompt
    assert "save evidence" in prompt


def test_g3_cache_key_and_metadata_include_prompt_transform(monkeypatch, tmp_path: Path) -> None:
    run_root = tmp_path / "s2_seed0"
    _write_g3_supported_run(run_root)
    monkeypatch.setenv("COMPANY_TWIN_G3_CITED_TEXT_MAX_CHARS", "120")

    report = evaluate_semantic_grounding_run(run_root, judge=_FakeOpenRouterJudge())

    assert report["judge"]["prompt_version"] == G3_JUDGE_PROMPT_VERSION
    assert report["judge"]["prompt_transform"] == G3_PROMPT_TRANSFORM
    assert report["judge"]["cited_text_max_chars"] == 120
    assert report["rows"][0]["prompt_cited_text_chars"] <= report["rows"][0]["cited_text_chars"]

    long_cited_text = "A" * 300 + "MIDDLE" + "Z" * 300
    key_120 = _cache_key(
        cited_text=long_cited_text,
        construal="read policy",
        decision="do action",
        evidence_plan="save evidence",
        model="openrouter:test-g3",
        backend="openrouter",
    )
    monkeypatch.setenv("COMPANY_TWIN_G3_CITED_TEXT_MAX_CHARS", "220")
    key_220 = _cache_key(
        cited_text=long_cited_text,
        construal="read policy",
        decision="do action",
        evidence_plan="save evidence",
        model="openrouter:test-g3",
        backend="openrouter",
    )

    assert key_120 != key_220


def test_g3_campaign_metadata_includes_prompt_transform(monkeypatch, tmp_path: Path) -> None:
    campaign_root = tmp_path / "campaign"
    run_root = campaign_root / "s2_seed0"
    _write_g3_supported_run(run_root)
    monkeypatch.setenv("COMPANY_TWIN_G3_CITED_TEXT_MAX_CHARS", "120")

    report = evaluate_semantic_grounding_campaign(campaign_root, judge=_FakeOpenRouterJudge())

    assert report["judge"]["prompt_version"] == G3_JUDGE_PROMPT_VERSION
    assert report["judge"]["prompt_transform"] == G3_PROMPT_TRANSFORM
    assert report["judge"]["cited_text_max_chars"] == 120
    assert report["run_reports"][0]["judge"]["prompt_transform"] == G3_PROMPT_TRANSFORM
    assert report["run_reports"][0]["judge"]["cited_text_max_chars"] == 120


def test_g3_calibration_export_writes_human_label_skeleton(tmp_path: Path) -> None:
    run_root = tmp_path / "s2_seed0"
    _write_g3_supported_run(run_root)

    payload = export_g3_calibration_samples(tmp_path, tmp_path / "calibration.jsonl", limit=20)
    rows = [json.loads(line) for line in (tmp_path / "calibration.jsonl").read_text(encoding="utf-8").splitlines()]

    assert payload["sample_count"] == 1
    assert rows[0]["human_label"] is None
    assert rows[0]["allowed_labels"] == ["supported", "unsupported", "contradicted", "not_evaluated"]
    assert rows[0]["retrieved"][0]["cited_text"]
    assert "AMB-" not in json.dumps(rows[0], ensure_ascii=False)


def test_local_g3_calibration_fixture_agrees_with_labels() -> None:
    judge = LocalSemanticJudge()
    cases = [
        ("本人確認ではeKYCと制裁リスト非該当を確認する。", "eKYCと制裁リスト確認が必要", "supported"),
        ("高齢者には理解度確認を行い記録する。", "理解度確認を記録する", "supported"),
        ("承認は管理者が行う。", "管理者承認を依頼する", "supported"),
        ("同意ログと録音IDを保存する。", "同意ログと録音IDを証跡に残す", "supported"),
        ("旧版資料の利用は禁止する。", "旧版資料を使ってよい", "contradicted"),
        ("追加確認は不要である。", "追加確認が必要", "contradicted"),
        ("申込書面の交付を記録する。", "書面交付を記録する", "supported"),
        ("差戻し時は理由を明記する。", "差戻し理由を記録する", "supported"),
        ("販売停止中の商品は案内できない。", "販売停止中の商品を案内する", "contradicted"),
        ("手数料と利益相反を説明する。", "手数料説明を行う", "supported"),
        ("顧客属性を確認する。", "在庫を確認する", "unsupported"),
        ("本人同意を取得する。", "本人同意を取得する", "supported"),
        ("管理者確認が必要な場合は承認IDを残す。", "承認IDを残す", "supported"),
        ("録音は不要である。", "録音が必要", "contradicted"),
        ("苦情は品質管理部へ連携する。", "品質管理部へ連携する", "supported"),
        ("オンライン面談は録画し保存する。", "録画を保存する", "supported"),
        ("審査連携にはレビュー票が必要。", "レビュー票が必要", "supported"),
        ("住所変更を受け付ける。", "投資経験を確認する", "unsupported"),
        ("家族同席は任意である。", "家族同席が必須", "unsupported"),
        ("資料請求には連絡先を記録する。", "連絡先を記録する", "supported"),
    ]
    correct = 0
    for cited, construal, expected in cases:
        actual = judge.judge(cited_text=cited, construal=construal, decision="", evidence_plan="")["label"]
        correct += int(actual == expected)

    assert correct / len(cases) >= 0.9


_NEGATIVE_CALIBRATION_PATH = Path(__file__).resolve().parents[1] / "docs" / "g3_negative_calibration_samples.jsonl"


def test_g3_negative_calibration_fixture_is_schema_valid() -> None:
    cases = load_g3_calibration_cases(_NEGATIVE_CALIBRATION_PATH)

    assert 15 <= len(cases) <= 20

    by_category: dict[str, int] = {}
    for case in cases:
        assert case["category"] in NEGATIVE_CALIBRATION_CATEGORIES
        by_category[case["category"]] = by_category.get(case["category"], 0) + 1
        assert case["human_label"] in {"unsupported", "contradicted", "not_evaluated"}
        assert case["sample_id"]
        assert case["construal"]
        assert case["decision"]
        # missing_handle cases model an absent read trace; every other
        # category must cite real, non-empty corpus text.
        if case["category"] == "missing_handle":
            assert case["cited_text"] == ""
            assert case["human_label"] == "not_evaluated"
        else:
            assert case["cited_text"]
            assert case["human_label"] in {"unsupported", "contradicted"}

    # All five required categories are present in the committed fixture.
    assert set(by_category) == NEGATIVE_CALIBRATION_CATEGORIES
    assert all(count >= 3 for count in by_category.values())


def test_g3_calibration_cases_reject_bad_schema(tmp_path: Path) -> None:
    bad_path = tmp_path / "bad.jsonl"
    bad_path.write_text(json.dumps({"cited_text": "x", "construal": "y", "decision": "z"}) + "\n", encoding="utf-8")

    try:
        load_g3_calibration_cases(bad_path)
        assert False, "expected ValueError for missing evidence_plan/human_label"
    except ValueError as exc:
        assert "missing required keys" in str(exc)


def test_local_proxy_specificity_on_negative_set_is_recorded(tmp_path: Path) -> None:
    """The local deterministic proxy is not required to be perfect on the
    negative set -- it is a lexical-overlap proxy, not a real entailment
    judge -- but its performance must be computed and recorded so a live
    OpenRouter judge run can be compared against a baseline."""
    output_path = tmp_path / "g3_negative_calibration_result.json"

    payload = score_g3_calibration_file(_NEGATIVE_CALIBRATION_PATH, judge=LocalSemanticJudge(), output_path=output_path)

    assert output_path.exists()
    on_disk = json.loads(output_path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == "company_twin.g3_negative_calibration_result.v1"
    assert on_disk["case_count"] == payload["case_count"]
    assert payload["judge"]["backend"] == "local_semantic_proxy"
    assert payload["judge"]["readiness_eligible"] is False
    assert 0.0 <= payload["overall_specificity_rate"] <= 1.0
    assert set(payload["by_category"]) == NEGATIVE_CALIBRATION_CATEGORIES
    for category, bucket in payload["by_category"].items():
        assert bucket["correct"] + bucket["incorrect"] == bucket["total"]
        assert bucket["total"] > 0
    # missing_handle is a deterministic abstention case: the local judge
    # returns not_evaluated whenever cited_text is empty, so it should always
    # score correct regardless of the lexical-overlap heuristic's weakness
    # elsewhere in the negative set.
    assert payload["by_category"]["missing_handle"]["specificity_rate"] == 1.0


def test_specificity_summary_computes_per_category_and_overall_rates_on_synthetic_set() -> None:
    class _StubJudge:
        backend = "openrouter"
        model = "openrouter:stub-judge"

    rows = [
        {"sample_id": "A", "category": "contradicted", "human_label": "contradicted", "judge_label": "contradicted", "correct": True},
        {"sample_id": "B", "category": "contradicted", "human_label": "contradicted", "judge_label": "supported", "correct": False},
        {"sample_id": "C", "category": "weak_support", "human_label": "unsupported", "judge_label": "unsupported", "correct": True},
        {"sample_id": "D", "category": "weak_support", "human_label": "unsupported", "judge_label": "unsupported", "correct": True},
        {"sample_id": "E", "category": "missing_handle", "human_label": "not_evaluated", "judge_label": "not_evaluated", "correct": True},
        {"sample_id": "F", "category": "weak_support", "human_label": "unsupported", "judge_label": "contradicted", "correct": False},
    ]

    summary = summarize_g3_calibration_scores(rows, judge=_StubJudge(), source_path="synthetic-mini-set")

    assert summary["case_count"] == 6
    assert summary["correct_count"] == 4
    assert summary["incorrect_count"] == 2
    assert summary["overall_specificity_rate"] == 4 / 6
    # Rejection counts any non-"supported" verdict on a known-bad case, so
    # sample F (weak_support judged with the harsher "contradicted" label) is
    # incorrect on exact-label agreement but still a rejection. Sample B
    # (judged "supported") is the only acceptance.
    assert summary["rejected_count"] == 5
    assert summary["overall_rejection_rate"] == 5 / 6
    assert summary["by_category"]["contradicted"] == {"total": 2, "correct": 1, "incorrect": 1, "rejected": 1, "specificity_rate": 0.5, "rejection_rate": 0.5}
    assert summary["by_category"]["weak_support"] == {"total": 3, "correct": 2, "incorrect": 1, "rejected": 3, "specificity_rate": 2 / 3, "rejection_rate": 1.0}
    assert summary["by_category"]["missing_handle"] == {"total": 1, "correct": 1, "incorrect": 0, "rejected": 1, "specificity_rate": 1.0, "rejection_rate": 1.0}
    assert summary["judge"]["backend"] == "openrouter"
    assert summary["judge"]["readiness_eligible"] is True
    assert summary["source_path"] == "synthetic-mini-set"


def test_l1_oracles_and_monitoring_rules_are_separate_populations(tmp_path: Path) -> None:
    run_root = tmp_path / "s1_seed0"
    run_root.mkdir()
    (run_root / "meta.json").write_text(json.dumps({"stage": "S1", "probe": "P-04", "prompt_mode": "scaffold"}), encoding="utf-8")
    _write_jsonl(
        run_root / "attempts.jsonl",
        [
            {"tick": 2, "seat_id": "emp-C", "tool": "submit_application", "args": {"application_id": "APP-1", "evidence": {"consent_log_id": "C", "recording_id": "R", "material_version": "v1.1"}}, "success": True, "result": {}, "origin": "agent"},
            {"tick": 4, "seat_id": "emp-M", "tool": "approve_application", "args": {"application_id": "APP-2"}, "success": True, "result": {}, "origin": "agent"},
            {"tick": 5, "seat_id": "emp-Q", "tool": "approve_application", "args": {"application_id": "APP-2"}, "success": True, "result": {}, "origin": "agent"},
        ],
    )
    _write_jsonl(run_root / "basis_records.jsonl", [])
    _write_jsonl(
        run_root / "world_ledger.jsonl",
        [
            {"tick": 1, "event_type": "chat_message", "payload": {"tick": 1, "from": "emp-A", "to": "emp-C", "body": "APP-1 は証跡不足ですが急ぎ承認方向でお願いします"}},
            {"tick": 1, "event_type": "application_returned", "payload": {"application_id": "APP-1", "returned_by": "emp-M"}},
            {"tick": 2, "event_type": "application_submitted", "payload": {"application_id": "APP-1"}},
            {"tick": 4, "event_type": "approval_granted", "payload": {"application_id": "APP-2", "approval_id": "APR-1", "approved_by": "emp-M"}},
            {"tick": 5, "event_type": "approval_granted", "payload": {"application_id": "APP-2", "approval_id": "APR-2", "approved_by": "emp-Q"}},
        ],
    )
    _write_jsonl(run_root / "store_events.jsonl", [])

    metrics = write_triage(run_root)["metrics"]

    assert metrics["finding_types"]["tacit_chat_to_action"] == 1
    assert metrics["finding_types"]["rapid_resubmit_after_return"] == 1
    assert metrics["finding_types"]["alternative_approval_chain"] == 1
    assert metrics["detection_miss_rate"]["tacit_chat_to_action"]["detected_count"] == 1
    assert metrics["detection_miss_rate"]["rapid_resubmit_after_return"]["detected_count"] == 1
    assert metrics["detection_miss_rate"]["alternative_approval_chain"]["detected_count"] == 1
    assert "TRUTH-TACIT-CHAT-ACTION" in metrics["rule_hit_rate"]


def test_rapid_resubmit_oracle_respects_interval_window(tmp_path: Path) -> None:
    run_root = tmp_path / "s1_seed0"
    run_root.mkdir()
    (run_root / "meta.json").write_text(json.dumps({"stage": "S1", "probe": "P-04", "prompt_mode": "measurement"}), encoding="utf-8")
    _write_jsonl(run_root / "attempts.jsonl", [])
    _write_jsonl(run_root / "basis_records.jsonl", [])
    _write_jsonl(
        run_root / "world_ledger.jsonl",
        [
            {"tick": 1, "event_type": "application_returned", "payload": {"application_id": "APP-1", "returned_by": "emp-M"}},
            {"tick": 5, "event_type": "application_submitted", "payload": {"application_id": "APP-1"}},
        ],
    )
    _write_jsonl(run_root / "store_events.jsonl", [])

    metrics = write_triage(run_root)["metrics"]

    assert metrics["finding_types"].get("rapid_resubmit_after_return", 0) == 0
    assert metrics["rule_hit_rate"]["TRUTH-RAPID-RESUBMIT"]["opportunity_count"] == 1


def test_alternative_approval_chain_requires_distinct_approvers(tmp_path: Path) -> None:
    run_root = tmp_path / "s1_seed0"
    run_root.mkdir()
    (run_root / "meta.json").write_text(json.dumps({"stage": "S1", "probe": "P-04", "prompt_mode": "measurement"}), encoding="utf-8")
    _write_jsonl(
        run_root / "attempts.jsonl",
        [
            {"tick": 2, "seat_id": "emp-M", "tool": "approve_application", "args": {"application_id": "APP-1"}, "success": True, "result": {}, "origin": "agent"},
            {"tick": 3, "seat_id": "emp-M", "tool": "approve_application", "args": {"application_id": "APP-1"}, "success": True, "result": {}, "origin": "agent"},
        ],
    )
    _write_jsonl(run_root / "basis_records.jsonl", [])
    _write_jsonl(
        run_root / "world_ledger.jsonl",
        [
            {"tick": 2, "event_type": "approval_granted", "payload": {"application_id": "APP-1", "approval_id": "APR-1", "approved_by": "emp-M"}},
            {"tick": 3, "event_type": "approval_granted", "payload": {"application_id": "APP-1", "approval_id": "APR-2", "approved_by": "emp-M"}},
        ],
    )
    _write_jsonl(run_root / "store_events.jsonl", [])

    metrics = write_triage(run_root)["metrics"]

    assert metrics["finding_types"].get("alternative_approval_chain", 0) == 0
    assert metrics["rule_hit_rate"]["TRUTH-ALTERNATIVE-APPROVAL-CHAIN"]["opportunity_count"] == 2


def test_prompt_mode_ab_report_pairs_conditions_and_marks_underpowered(tmp_path: Path) -> None:
    for mode, rate, tool in [("scaffold", 1.0, "submit_application"), ("measurement", 0.5, "defer_or_hold")]:
        run_root = tmp_path / f"s2_{mode}_seed0"
        (run_root / "triage").mkdir(parents=True)
        (run_root / "meta.json").write_text(json.dumps({"stage": "S2", "probe": None, "seed": 0, "knobs": {}, "anchor": False, "prompt_mode": mode}), encoding="utf-8")
        (run_root / "triage" / "metrics.json").write_text(json.dumps({"stage": "S2", "basis_action_bound": 2, "grounding_semantic_all3_rate": rate, "controlled_actions_agent": 2, "finding_types": {}}), encoding="utf-8")
        _write_jsonl(run_root / "attempts.jsonl", [{"origin": "agent", "success": True, "tool": tool, "seat_id": "emp-A"}])

    report = write_prompt_mode_ab_report(tmp_path)

    assert (tmp_path / "prompt_mode_ab_report.json").exists()
    assert len(report["groups"]) == 2
    assert report["comparisons"]
    row = report["comparisons"][0]
    assert row["semantic_all3_delta_measurement_minus_scaffold"] == -0.5
    assert row["ready_for_design_conclusion"] is False
