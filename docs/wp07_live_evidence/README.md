# WP-07 Live Control-Pair Evidence

This folder records the first WP-07 delta-one live control-pair execution.

## Scope

- Mutation under test: `clarify_elderly_understanding_all`
- Stage/probe: `S1` / `P-01`
- Conditions: control `[]` vs treatment `["clarify_elderly_understanding_all"]`
- Seeds: `0..4` shared across control and treatment
- Prompt mode: `measurement`
- D2 timed notices: fixed off with `timed_notice_recipients=[]`
- Live backend: `deepagents` / `openrouter:qwen/qwen3.6-flash`

Command:

```powershell
python -m company_twin.cli control-pair-campaign --manifest runs\wp07_clarify_elderly_control_pairs.json --probe P-01 --ticks 1 --model openrouter:qwen/qwen3.6-flash
```

## Artifacts

- `live_execution_summary.json`: compact evidence summary and boundary note.
- `control_pair_campaign_summary.sanitized.json`: run manifest, seeds, mutation hashes, and live run summaries with local paths removed.
- `attribution_table.sanitized.json`: WP-07 attribution rows with seed-bundle checks and Wilson intervals.
- `ensemble_triage.sanitized.json`: grouped finding rates, ICC proxy output, run filter, and candidate queues.

## Result

The campaign completed `5 x 2 = 10` live S1 condition runs. `ensemble_triage.run_filter.mode` is `control_pairs` and includes only those 10 runs. All attribution rows have matching seed bundles.

Initial candidate-level readout:

- `any_l0_finding`: control `0.2`, treatment `0.2`, `effect_delta=0.0`.
- `grounding_gap`: control `0.2`, treatment `0.0`, `effect_delta=-0.2`.
- `version_gap`: control `0.2`, treatment `0.0`, `effect_delta=-0.2`.

The ICC proxy is `0.0` for both configs, so these rows should be treated as low-stability candidate evidence, not confirmed findings. The conservative answer to the initial WP-07 question is: the clarification did not reduce overall L0 finding incidence in this first K=5 run, while it produced candidate evidence of fewer grounding/version gaps that needs confirmation before audit use.

This evidence does not claim Stage 9 readiness all-pass.
