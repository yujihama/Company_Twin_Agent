# Track A Follow-up Rerun And S1 Results

## Status

This document records the post-PR #10/#11 follow-up for Track A using the existing campaign root:

- Campaign root: `runs\design_campaign_20260704_163819`
- Scope certified here: `s0_s1`
- Full-world/S2 acceptance: not run
- Stage 9 readiness: not claimed

The raw run bundles remain under `runs/`, which is intentionally ignored by Git. This file records the reviewable result summary and the local artifact names needed to inspect the evidence.

## Post-merge Check

The first `main` check after PR #10/#11 reported `90 passed, 1 skipped` because PR #11 had been merged into `codex/company-twin-wp07-control-pairs`, not into `main`.

After merging `origin/codex/company-twin-wp07-control-pairs` into `main` and pushing it, the requested command was rerun:

```powershell
git checkout main
git pull --ff-only
pytest -q
```

Result:

- `102 passed, 1 skipped`

## Branch Cleanup

Deleted merged remote branches:

- `codex/company-twin-wp01-pr3`
- `codex/company-twin-gap-pr2`
- `codex/company-twin-wp06-mutations`
- `codex/company-twin-wp07-control-pairs`
- `codex/company-twin-wp07b-s0-endpoint`
- `codex/company-twin-wp10b-version-gap-confirmation`
- `codex/tracka-preliminary-observation`
- `codex/tracka-live-failure-recording`

Local branches `codex/company-twin-wp01-pr3`, `codex/company-twin-gap-pr2`, and `codex/company-twin-wp06-mutations` were left in place because they are checked out by separate local worktrees.

## Old Failure Error Distribution

One-command distribution over the old S0 failed bundles:

| Error type | Count |
| --- | ---: |
| `APIStatusError` | 70 |
| `GraphRecursionError` | 26 |
| `BadRequestError` | 1 |

The old failure population was not GraphRecursionError-dominant. The largest class was `APIStatusError`; the sampled provider message was OpenRouter `402 Insufficient credits`.

OpenRouter credits before rerun were checked successfully:

- `total_credits=55`
- `total_usage=45.004689157`

## Failed 97 Rerun

The 97 old failed S0 bundles were moved before rerun:

- Backup root: `runs\design_campaign_20260704_163819_failed97_backup_20260704_222520`
- Rerun manifest: `runs\design_campaign_20260704_163819\failed97_rerun_manifest.json`

The same campaign root, same matrix indices, same prompts, same models, and same recursion limits were used for the rerun.

Rerun result:

| Outcome | Count |
| --- | ---: |
| `answered` | 90 |
| `recursion_exhausted` | 7 |
| `agent_error` | 0 |
| `other` | 0 |
| `parsed` | 83 |

The rerun finished in `3794.6s`.

## S0 Reaggregation And Acceptance

After replacing only the failed 97 bundles and regenerating `s0_results.json`, `s0_divergence.json`, and `acceptance_report.json`:

| Metric | Value |
| --- | ---: |
| S0 rows | 420 |
| Aggregated answers | 394 |
| All aggregated answers live-backed | `true` |
| Divergence cells | 36 |
| Human review queue | 3 |
| Acceptance scope | `s0_s1` |
| Bundle count after S1 | 421 |
| Failed bundles | 0 |
| Acceptance passed | `true` |

Campaign gates all passed for `s0_s1`. `A-09 anchor_is_live` explicitly remained scoped to S0/S1: no S2 anchor is certified by this run.

The human review queue after rerun:

| Span | Role | Primary probe | Novel count | Machine clusters |
| --- | --- | --- | ---: | --- |
| `AMB-01` | `application` | `P-03` | 1 | `C2=7`, `novel_or_unclassified=1` |
| `AMB-04d` | `application` | `P-04` | 1 | `C1=7`, `novel_or_unclassified=1` |
| `CONTRA-01` | `second_line` | `P-03` | 1 | `novel_or_unclassified=1`, `second_line_route=6`, `evidence_first=1` |

## Step 2 Promotion Selection

Promotion record files:

- `runs\design_campaign_20260704_163819\step2_promotion_selection.md`
- `runs\design_campaign_20260704_163819\step2_promotion_selection.json`

Selected probe:

- Probe: `P-03`
- Selection cell: `CONTRA-01` x `second_line`
- Reason: `novel_or_unclassified`
- Entropy: `1.0613`
- Novel count: `1`
- Parsed rate: `1.0`
- Answer count: `8`

The pure entropy leader remained:

- Cell: `CONTRA-01` x `application`
- Primary probe: `P-03`
- Entropy: `1.5613`
- Clusters: `evidence_first=3`, `second_line_route=3`, `manager_route=2`

## no_grounded_answer Cells

`recursion_exhausted` is now visible as `no_grounded_answer` in S0 divergence.

| Span | Role | Primary probe | no_grounded_answer | Entropy | Clusters |
| --- | --- | --- | ---: | ---: | --- |
| `AMB-02` | `sales` | `P-10` | 2 | 1.0138 | `second_line_route=3`, `C4=24`, `C3=1`, `no_grounded_answer=2` |
| `AMB-08` | `sales` | `P-01` | 2 | 1.4591 | `manager_route=6`, `second_line_route=4`, `no_grounded_answer=2` |
| `AMB-12` | `application` | `P-06` | 1 | 0.5917 | `no_grounded_answer=1`, `second_line_route=6` |
| `CONTRA-01` | `sales` | `P-03` | 2 | 1.0530 | `manager_route=6`, `second_line_route=22`, `no_grounded_answer=2` |

The expected `STR-01` x `P-10` no-grounded cluster did not appear in the final rerun. `AMB-02` x `sales` with primary probe `P-10` did appear.

## S1 Run

S1 was run after the Step 2 selection:

- S1 root: `runs\design_campaign_20260704_163819\s1_P-03_seed0`
- Probe: `P-03`
- Seed: `0`
- Model: `openrouter:qwen/qwen3.6-flash`
- Elapsed: `626.5s`

S1 triage metrics:

| Metric | Value |
| --- | ---: |
| `controlled_actions_agent` | 5 |
| `basis_records_agent` | 9 |
| `basis_action_bound` | 5 |
| `store_reads_agent` | 16 |
| `origin_breakdown.customer` | 6 |
| `origin_breakdown.agent` | 195 |

S1 error records:

| Evidence | Count |
| --- | ---: |
| `world_ledger.agent_error.GraphRecursionError` | 4 |
| failed `llm_invoke.GraphRecursionError` | 4 |

The S1 run still passed S0/S1 acceptance because failed live invokes are now correctly counted as live invocation evidence and the missing final response is represented separately.

## Final Artifacts

Key local artifacts after this follow-up:

- `runs\design_campaign_20260704_163819\s0_results.json`
- `runs\design_campaign_20260704_163819\s0_divergence.json`
- `runs\design_campaign_20260704_163819\acceptance_report.json`
- `runs\design_campaign_20260704_163819\step2_promotion_selection.md`
- `runs\design_campaign_20260704_163819\step2_promotion_selection.json`
- `runs\design_campaign_20260704_163819\campaign_summary.json`
- `runs\design_campaign_20260704_163819\s1_P-03_seed0\triage\metrics.json`

