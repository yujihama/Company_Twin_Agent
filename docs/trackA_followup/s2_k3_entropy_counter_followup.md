# Track A S2 K=3 Entropy And Counter Follow-up

## Status

This note records the follow-up after PR #13 using the existing campaign root.

- Campaign root: `runs\design_campaign_20260704_163819`
- Branch: `codex/tracka-entropy-s2-counter-fix`
- Scope certified here: `full_world`
- Full-world acceptance: passed
- Stage 9 readiness: not claimed

Raw live bundles remain under `runs/`, which is intentionally ignored by Git.

## Fixes Applied

- S0 entropy now excludes instrumentation dropouts: `unparsed` remains visible in `clusters` but is omitted from `entropy_clusters`.
- `no_grounded_answer` remains an interpretation answer state and is still included in entropy.
- S0 records now persist `candidate_ids` snapshots; late opt-in candidates such as `CONTRA-01/split_by_topic` are not retroactively applied to historical S0 rows.
- S2 runtime tool counts now use `RunRecorder` in-memory counters instead of rereading active `attempts.jsonl`.
- G3 campaign aggregation now skips bundles marked with `failed_run.json`, matching ensemble and acceptance behavior.

## S0 Entropy Check

After regenerating `s0_divergence.json`, the key cell returned to the intended interpretation entropy:

| Cell | Entropy | Clusters | Entropy clusters | Excluded |
| --- | ---: | --- | --- | --- |
| `CONTRA-01` x `application` | 1.5567 | `evidence_first=2`, `second_line_route=3`, `manager_route=2`, `unparsed=1` | `evidence_first=2`, `second_line_route=3`, `manager_route=2` | `unparsed=1` |

This preserves the visible `unparsed` count without letting it inflate the interpretation ranking.

## S2 Non-anchor K=3 Completion

The old failed `s2_seed0` bundle remains preserved with `failed_run.json`. Replacement and remaining non-anchor runs were completed with unchanged prompt/model/tick settings.

| Run | Controlled actions | Basis records | Basis action bound | Store reads | LLM invocations | Customer events |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `s2_seed0_retry1` | 64 | 78 | 64 | 87 | 184 | 38 |
| `s2_seed1` | 54 | 64 | 54 | 70 | 156 | 38 |
| `s2_seed2` | 57 | 69 | 57 | 78 | 166 | 38 |

Both `s2_seed1` and `s2_seed2` crossed the previous JSONL failure area without `UnicodeDecodeError`.

## Aggregation And G3

`aggregate_ensemble_triage` completed with:

- Included run count: 434
- Excluded failed runs: `s2_seed0`

Local proxy G3 campaign aggregation completed with:

| Metric | Value |
| --- | ---: |
| Run count | 434 |
| Basis action bound | 265 |
| Supported count | 39 |
| Semantic all3 count | 39 |
| Proxy all3 rate | 0.1471698113 |
| Excluded failed runs | `s2_seed0` |

An OpenRouter judge G3 attempt was stopped after exposing the stale failed-bundle traversal. The code now skips `failed_run.json` bundles for G3 campaign aggregation, but readiness still requires a readiness-eligible judge run and is not claimed here.

## Acceptance And Readiness

Full-world acceptance:

| Item | Result |
| --- | --- |
| `passed` | `true` |
| Failed run bundles retained | 1 |
| Excluded failed bundle | `s2_seed0` |
| A-13 full-world evidence | passed |

Readiness remained false:

- `full_world_harness_acceptance`: passed
- `s0_divergence_sanity`: passed
- Missing reports: routine smoke, leak lint, retrieval audit, semantic grounding report, backcasting, SME blind review, holdout
- Semantic grounding threshold: failed because the available G3 metrics are local proxy, not readiness-eligible OpenRouter semantic metrics

Final OpenRouter credit check:

- `total_credits=65`
- `total_usage=57.100116685`

This follow-up closes the S2 K=3 execution gap and the two instrumentation fixes. It does not claim Stage 9 readiness or confirmed findings.
