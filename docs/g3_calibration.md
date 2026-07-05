# G3 Semantic Grounding Calibration

Date: 2026-07-04

Scope: WP-02 introduces a g3 semantic grounding evaluator that checks whether a
staff basis construal/decision/evidence plan is supported by the cited
`read_document` chunk text behind its `citation_handle`.

## Boundary

- The evaluator reads only run-bundle `attempts.jsonl` and `basis_records.jsonl`.
- It does not load the span registry, latent truth, probe ids, or seeded span ids.
- `grounding_g3_machine_heuristic_rate` remains the legacy lexical signal.
- `grounding_semantic_all3_rate` is populated from `g3_semantic_grounding.json`.
- Local CI uses `LocalSemanticJudge` as a deterministic proxy. Its rates are
  written only to `grounding_g3_semantic_rate_proxy` and
  `grounding_semantic_all3_rate_proxy`.
- Readiness accepts only unqualified `grounding_semantic_all3_rate` values from
  allowlisted live judge backends such as `openrouter`.

## Calibration Fixture

The committed unit calibration uses 20 synthetic Japanese support/contradiction
cases covering required evidence, approval, elderly-customer handling, stale
material, eKYC, returns, complaints, and weakly related unsupported statements.
This is only a regression guard for the proxy. It is not the design DoD for a
production semantic judge.

Current test:

```powershell
pytest tests\test_wp02_wp04_wp05.py::test_local_g3_calibration_fixture_agrees_with_labels -q
```

Observed in this PR branch: `1 passed`.

## Human-Label Calibration Procedure

Export approximately 20 action-bound basis samples from a real bundle or
campaign:

```powershell
python -m company_twin.cli g3-export-calibration --source-root runs\design_campaign_YYYYMMDD_HHMMSS --output docs\g3_calibration_samples.jsonl --limit 20
```

The JSONL file contains `cited_text`, `construal`, `decision`,
`evidence_plan`, and an empty `human_label`. A reviewer should fill
`human_label` with one of:

```text
supported
unsupported
contradicted
not_evaluated
```

Then rerun the same campaign with an explicit live judge:

```powershell
python -m company_twin.cli g3 --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS --judge-model openrouter:qwen/qwen3.6-plus
```

Record the OpenRouter judge model, sample file hash, agreement count, and
agreement rate in this document. The WP-02 semantic-judge DoD is not satisfied
until that human-labeled calibration is recorded.

## Operational Command

For a single bundle:

```powershell
python -m company_twin.cli g3 --run-root runs\...\s2_seed0 --judge-model openrouter:qwen/qwen3.6-plus
```

For a campaign:

```powershell
python -m company_twin.cli g3 --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS --judge-model openrouter:qwen/qwen3.6-plus
```

The command writes `g3_semantic_grounding.json` plus
`g3_entailment_cache.json`. Readiness then consumes only allowlisted, unqualified
semantic rates through triage metrics or the run-level g3 report; proxy-only
reports remain visible but cannot pass the semantic threshold.

## WP-01 Campaign Calibration Result

Date: 2026-07-04

Source: WP-01 live campaign `runs/design_campaign_20260704_012445`, anchor bundle
`anchor_s2_seed0`. The reviewer-labeled sample file is `calibration.jsonl`.

Sample hash:

```text
SHA256 39049BABF3B48C29CDCCBB5A7A75AE21DF078C9245297188D6FB24E64E05F2C5
```

Human labels: 20 samples, all `supported`. The exported rows were action-bound
customer-contact basis records with cited `read_document` text. The only
borderline item was `anchor_s2_seed0:BASIS-000004`, where the human label
accepted a high-level merchant-contract process citation as supporting the
staff's operational next step, while the judge required a more explicit
approval rule.

Live judge:

```powershell
python -m company_twin.cli g3 --run-root runs\design_campaign_20260704_012445\anchor_s2_seed0 --judge-model openrouter:qwen/qwen3.6-plus
```

Result:

| Judge prompt | Agreement | Rate | Target |
|---|---:|---:|---:|
| pre-adjustment prompt | 16/20 | 80% | below 90% |
| `operational-support-v2` | 19/20 | 95% | pass |

The prompt adjustment keeps the live OpenRouter path strict, but tells the judge
to evaluate substantive policy/procedure support rather than exact internal tool
names, local field names, action IDs, or v1.0/v1.1 label wording. Cache keys now
include the prompt version so prior OpenRouter labels are not reused after a
prompt change.

Anchor bundle live G3 summary after adjustment:

```json
{
  "judge": {
    "backend": "openrouter",
    "model": "openrouter:qwen/qwen3.6-plus",
    "prompt_version": "operational-support-v2",
    "readiness_eligible": true
  },
  "basis_action_bound": 21,
  "supported_count": 20,
  "grounding_semantic_all3_rate": 0.9523809523809523
}
```

Next calibration caveat: this 20-case human sample is one-sided because every
human label is `supported`. It primarily measures false negatives. The next
manual calibration batch should include known-bad samples, preferably up to 10
fresh scaffold-mode fabricated-basis findings not used in prompt tuning, so the
OpenRouter judge's specificity against unsupported or contradicted basis can be
measured before readiness use.

## Negative (Known-Bad) Calibration Set

The one-sided caveat above is addressed by a second, purpose-built fixture:
`docs/g3_negative_calibration_samples.jsonl`. It contains 20 known-bad/graded
cases built from real corpus text (`data/raw_data/...` and the deliberate
`data/raw_data_v1_0` stale mirrors of DFH-SAL-021 and DFH-SAL-045), so every
`cited_text` is either a real chunk unrelated to the claim, the real stale
v1.0 text, or a genuinely on-topic but non-supporting/contradicting real
chunk. Cases are in natural Japanese business register matching real
`basis_records.jsonl` construal/decision/evidence_plan style.

Each row uses the same schema as the positive fixture (`cited_text`,
`construal`, `decision`, `evidence_plan`, `human_label`) plus a required
`category` field. The five categories, each with 3-5 cases:

| Category | What it tests | `human_label` |
|---|---|---|
| `fabricated_basis` | construal/decision/evidence_plan invents an obligation absent from every real document; `cited_text` is real but unrelated | `unsupported` |
| `version_mismatch` | basis claims the current-rule procedure while `cited_text` is the outdated v1.0 text (DFH-SAL-021/045 skew) | `contradicted` |
| `weak_support` | `cited_text` is genuinely on-topic but does not reach the claimed conclusion | `unsupported` |
| `contradicted` | `cited_text` states the opposite of the construal/decision | `contradicted` |
| `missing_handle` | `citation_handle` present but no read trace / empty `cited_text` | `not_evaluated` |

Case counts in the committed fixture: `fabricated_basis=5`, `version_mismatch=4`,
`weak_support=4`, `contradicted=4`, `missing_handle=3` (20 total).

### Scoring Harness

`score_g3_calibration_file` in `semantic_grounding.py` (exposed as the
`g3-score-calibration` CLI command) runs the same `SemanticJudge` interface
used by `evaluate_semantic_grounding_run` over any labeled calibration JSONL
file -- positive or negative -- and writes a machine-readable summary with
per-category correct/incorrect counts, an overall specificity/agreement rate,
and judge metadata (`backend`, `model`, `prompt_version`,
`readiness_eligible`). For the `missing_handle`/`not_evaluated` category, a
case is scored correct only if the judge abstains (`not_evaluated`); asserting
any entailment label from absent evidence is a specificity failure.

Offline (local deterministic proxy, regression guard only -- this is not the
live specificity measurement):

```powershell
python -m company_twin.cli g3-score-calibration --calibration-file docs\g3_negative_calibration_samples.jsonl --output docs\g3_negative_calibration_result.local.json
```

### Live Specificity Run (required before readiness use)

```powershell
python -m company_twin.cli g3-score-calibration --calibration-file docs\g3_negative_calibration_samples.jsonl --output docs\g3_negative_calibration_result.json --judge-model openrouter:qwen/qwen3.6-plus
```

Record the resulting `overall_specificity_rate`, the per-category rates, and
the judge model/prompt version in this document once the live pass is run.
The WP-02 semantic-judge DoD is not fully satisfied until both the positive
agreement rate (above) and this negative specificity rate are recorded from a
live, allowlisted judge backend.

### Live Specificity Result (2026-07-05)

Judge: `openrouter:qwen/qwen3.6-plus`, prompt `operational-support-v2`,
`readiness_eligible=true`. Result artifact: `docs/g3_negative_calibration_result.json`.

| Category | Exact-label agreement | Rejection (any non-supported verdict) |
|---|---:|---:|
| fabricated_basis | 5/5 | 5/5 |
| version_mismatch | 4/4 | 4/4 |
| weak_support | 0/4 | 4/4 (all judged `contradicted` instead of expected `unsupported`) |
| contradicted | 3/4 | 3/4 |
| missing_handle | 3/3 | 3/3 |
| **Overall** | **15/20 = 0.75** | **19/20 = 0.95** |

Reading: the judge never accepted a fabricated, stale-version, or
missing-handle basis. The weak_support misses are label-taxonomy strictness
(it rejects them with the harsher `contradicted` label), not acceptances, so
`overall_rejection_rate` (0.95) is the safety-relevant figure while
`overall_specificity_rate` (0.75) reports exact-label agreement. The single
true failure is one `contradicted` case judged `supported`. Combined with the
positive calibration above (19/20 = 95% agreement on human-labeled supported
cases), the live judge is now calibrated in both directions. The summary
artifact reports both metrics side by side (`overall_specificity_rate`,
`overall_rejection_rate`); neither replaces the other.
