# WP-10b Version-Gap Confirmation Evidence

This folder records the first WP-10b operational confirmation pass from the WP-07 control-arm candidate queue.

## Scope

- Source campaign: `runs/control_pair_campaign_20260704_141958`
- Source PR stack: WP-07 control-pair evidence plus WP-07b/WP-10b runner
- Candidate job: `fba3117ae70d473c`
- Finding type: `version_gap`
- Stage/probe: `S1` / `P-01`
- Source arm: control, mutation ids `[]`
- Source bucket signature: `9b95d71285dd959e`
- Source positive seed: `3` of `0..4`
- Fresh confirmation seeds: `100..104`
- Threshold: `min_rate=0.2`, `confirmation_seeds=5`
- Pre-registration: `min_rate=0.2`, `confirmation_seeds=5`, derived from the queued exploration rate before confirmation
- Threshold override: `false`
- Tick trim: `--ticks 1`
- Live backend: `deepagents` / `openrouter:qwen/qwen3.6-flash`
- Matching rule: same finding type plus source bucket signature match

Command:

```powershell
python -m company_twin.cli min-repro-confirm --campaign-root runs\control_pair_campaign_20260704_141958 --job-id fba3117ae70d473c --confirmation-seeds 5 --seed-start 100 --min-rate 0.2 --ticks 1 --model openrouter:qwen/qwen3.6-flash
```

## Artifacts

- `live_execution_summary.json`: compact command/result summary.
- `min_repro_manifest.sanitized.json`: fresh confirmation manifest without local absolute paths.
- `finding_registry.sanitized.json`: confirmed finding and audit hypothesis card emitted by the registry.

## Result

The WP-10b pass completed `5` fresh live S1 confirmation runs. Seed `104` reproduced the same `version_gap` bucket signature (`9b95d71285dd959e`), yielding `source_bundle_count=1`, `reproduction_rate=0.2`, Wilson 95% interval `[0.04, 0.62]`, and `status=reproduced`.

The finding registry now contains one confirmed finding and one audit hypothesis card:

- Confirmed finding: `version_gap`, job `fba3117ae70d473c`
- Audit hypothesis card: `HYP-fba3117ae70d`
- Confirmation strength: `1/5`, Wilson 95% interval `[0.04, 0.62]`
- Divergence cell: `version_gap | basis | 9b95d71285dd959e`
- Reproduced bundle: `min_repro/fba3117ae70d473c/runs/s1_P-01_confirm_seed104`

This is a confirmed WP-10b audit hypothesis, not a Stage 9 readiness claim.
