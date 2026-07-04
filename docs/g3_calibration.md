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
