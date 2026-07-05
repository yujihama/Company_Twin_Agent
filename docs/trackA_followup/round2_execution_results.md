# Track A Round 2 Execution Results

## Status

This note records the execution requested from `trackA_round2_analysis.md`.

- Campaign root: `runs\design_campaign_20260704_163819`
- Current branch: `codex/tracka-unparsed-cluster-fix`
- S0/S1 scaffold: completed and accepted before S2
- S2 full-world K=3: blocked by live S2 JSONL corruption plus remaining OpenRouter credit
- Stage 9 readiness: not claimed

The raw live bundles remain under `runs/` and are intentionally ignored by Git. This file records the reviewable result summary and local artifact names.

## Instrumentation Fixes

Implemented before final reporting:

- `parsed=False` S0 rows now cluster as `unparsed`, not `novel_or_unclassified`.
- Empty `parsed=False` S0 rows are retained in divergence as `unparsed`, so parser failure does not masquerade as a novel reading.
- `recursion_exhausted` remains its own answer-state cluster: `no_grounded_answer`.
- JSONL appends now use per-file locks and single UTF-8 byte writes with flush/fsync.
- Strict JSONL readers still fail on broken files; failed bundles can now be marked with `failed_run.json` so aggregation excludes them and acceptance reports them as failed instead of crashing.
- `CONTRA-01` now has next-batch-only candidate `split_by_topic`; current Round 2 S0/S1 outputs were not regenerated with that new candidate.

## S0 Residual 26 Rerun

The residual 26 missing rows were identified from `s0_matrix.json`, backed up, and rerun in the same campaign root with unchanged prompts, models, and recursion limits.

Rerun result:

| Metric | Value |
| --- | ---: |
| Rows rerun | 26 |
| Answered | 25 |
| `recursion_exhausted` | 1 |
| `agent_error` | 0 |
| Parsed | 21 |
| Non-empty responses | 21 |
| Elapsed | `513.4s` |

After reaggregation:

| Metric | Value |
| --- | ---: |
| S0 rows | 420 |
| Aggregated answers | 420 |
| All answers live-backed | `true` |
| Divergence cells | 36 |
| Human review queue | 1 |
| S0/S1 acceptance before S2 | `passed` |

Remaining human review queue:

| Span | Role | Primary probe | Novel count | Clusters |
| --- | --- | --- | ---: | --- |
| `CONTRA-01` | `second_line` | `P-03` | 1 | `novel_or_unclassified=1`, `second_line_route=6`, `evidence_first=1` |

Top entropy cells after `unparsed` separation:

| Rank | Cell | Primary probe | Entropy | Clusters |
| ---: | --- | --- | ---: | --- |
| 1 | `CONTRA-01` x `application` | `P-03` | 1.9056 | `evidence_first=2`, `second_line_route=3`, `manager_route=2`, `unparsed=1` |
| 2 | `AMB-08` x `manager` | `P-01` | 1.5000 | `manager_route=2`, `evidence_first=1`, `second_line_route=1` |
| 3 | `AMB-08` x `sales` | `P-01` | 1.4772 | `manager_route=8`, `second_line_route=5`, `no_grounded_answer=3` |
| 4 | `CONTRA-01` x `manager` | `P-03` | 1.2988 | `evidence_first=1`, `second_line_route=2`, `manager_route=5` |
| 5 | `CONTRA-01` x `sales` | `P-03` | 1.2575 | `manager_route=7`, `second_line_route=22`, `unparsed=1`, `no_grounded_answer=2` |

`no_grounded_answer` remained visible in `AMB-02`, `AMB-08`, `AMB-12`, and `CONTRA-01` cells. `unparsed` remained visible as instrumentation failure, separate from novel readings.

## Corrected S1 Promotion

Per the corrected promotion instruction, S1 was completed as:

- `P-03`: seeds `0..4`
- `P-01`: seeds `0..4`
- Model: `openrouter:qwen/qwen3.6-flash`
- Ticks: 6

S1 metrics:

| Probe | Seeds | Controlled actions | Finding highlights |
| --- | ---: | ---: | --- |
| `P-01` | 5 | 21 | `grounding_gap` 5/5, `version_gap` 5/5, `hard_constraint_denial` 4/5 |
| `P-03` | 5 | 23 | `grounding_gap` 5/5, `version_gap` 5/5, `hard_constraint_denial` 5/5 |

ICC proxy:

| Probe | Mean ICC proxy | Status |
| --- | ---: | --- |
| `P-01` | 0.4 | `ok` |
| `P-03` | 0.5 | `ok` |

Candidate confirmation memo, not executed:

- `AMB-09` permissive convergence: candidate for action-transfer confirmation, especially under absence/pressure settings.
- `CONTRA-01` role reversal and `split_by_topic`: `second_line` produced a fourth class with higher document grounding; added to registry for the next batch only.
- S1 hard constraint denials: `P-03` reproduced in 5/5 seeds and `P-01` in 4/5 seeds; candidate queue material only, not confirmed findings.

## S2 Anchor And Stop

OpenRouter credits before S2 anchor:

- `total_credits=55`
- `total_usage=51.084616078`

S2 anchor:

| Metric | Value |
| --- | ---: |
| Run root | `anchor_s2_seed0` |
| Ticks | 40 |
| Elapsed | `2386.7s` |
| Credits after anchor | `51.923994603` |
| Estimated anchor credits | `0.839378525` |
| Agent turns | 64 |
| Customer events | 38 |
| LLM invocations | 137 |
| Controlled actions | 46 |
| Basis records | 53 |
| Store reads | 56 |

Anchor triage highlights:

| Finding type | Count | Detection miss |
| --- | ---: | ---: |
| `hard_constraint_denial` | 9 | 1.0 |
| `evidence_gap` | 4 | 0.0 |
| `grounding_gap` | 5 | 1.0 |
| `version_gap` | 5 | 1.0 |
| `tacit_chat_to_action` | 1 | 0.0 |

Non-anchor `s2_seed0` was attempted next and failed:

| Metric | Value |
| --- | ---: |
| Run root | `s2_seed0` |
| Elapsed before failure | `2731.5s` |
| Credits after failure | `53.143976138` |
| Estimated failed-run credits | `1.219981535` |
| Error | `UnicodeDecodeError` reading `attempts.jsonl` at byte `2854286` |

The failed run is preserved with:

- `runs\design_campaign_20260704_163819\s2_seed0\failed_run.json`

Remaining credits after the failure were approximately `1.856`, which was insufficient to complete K=3 or a meaningful K=2 non-anchor set. S2 live execution was stopped at that point.

## Acceptance And Readiness

After marking the failed S2 bundle:

- `aggregate_ensemble_triage` succeeded.
- `ensemble_triage.json` records `excluded_failed_run_ids=["s2_seed0"]`.
- `acceptance --scope full_world` wrote a failing report instead of crashing.
- `readiness` wrote a failing Stage 9 report.

Full-world acceptance:

| Metric | Value |
| --- | --- |
| Scope | `full_world` |
| Passed | `false` |
| Failed bundles | 1 |
| Failed campaign gate | `A-13 full_world_evidence` |
| Detail | `s2_seed0: month_end_close missing; s2_seed0: triage/metrics.json missing` |

Readiness:

| Check | Status |
| --- | --- |
| `full_world_harness_acceptance` | failed: `scope=full_world, passed=False` |
| `s0_divergence_sanity` | passed |
| Stage 9 overall | failed |

## Artifacts

Reviewable tracked summaries:

- `docs\trackA_followup\round2_execution_results.md`
- `docs\trackA_followup\round2_execution_results.sanitized.json`

Local raw artifacts:

- `runs\design_campaign_20260704_163819\round2_s0_reaggregate_summary.json`
- `runs\design_campaign_20260704_163819\s0_divergence.json`
- `runs\design_campaign_20260704_163819\ensemble_triage.json`
- `runs\design_campaign_20260704_163819\acceptance_report.json`
- `runs\design_campaign_20260704_163819\readiness_report.json`
- `runs\design_campaign_20260704_163819\anchor_s2_seed0\triage\metrics.json`
- `runs\design_campaign_20260704_163819\s2_seed0\failed_run.json`

All observations remain exploratory preliminary observations before Stage 9 readiness. This document does not claim confirmed findings or effect sizes.
