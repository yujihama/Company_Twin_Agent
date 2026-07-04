# WP-07b S0 Control-Pair Evidence

This folder records the first WP-07b S0 endpoint run for `clarify_elderly_understanding_all`.

## Scope

- Mutation under test: `clarify_elderly_understanding_all`
- Stage/probe/span/role: `S0` / `P-01` / `AMB-02` / `sales`
- Seat cell: `emp-A`
- Conditions: control `[]` vs treatment `["clarify_elderly_understanding_all"]`
- Observation bundle: seed `0`, two models, two paraphrase variants
- Models: `openrouter:qwen/qwen3.6-flash`, `openrouter:qwen/qwen3.6-plus`
- Endpoint: span-role interpretation entropy delta plus cluster-distribution shift
- Live backend: `deepagents`

Command:

```powershell
python -m company_twin.cli control-pair-campaign --manifest runs\wp07b_clarify_s0_pair_k1.json --stage S0 --probe P-01 --s0-span AMB-02 --s0-seat emp-A --s0-model openrouter:qwen/qwen3.6-flash --s0-model openrouter:qwen/qwen3.6-plus --s0-variants 2 --model openrouter:qwen/qwen3.6-flash
```

## Artifacts

- `live_execution_summary.json`: compact result and boundary note.
- `control_pair_campaign_summary.sanitized.json`: run rows with local paths and full answer text removed.
- `s0_attribution_table.sanitized.json`: entropy and cluster-shift attribution row.

## Result

The campaign completed `1 x 2 conditions x 2 models x 2 variants = 8` live S0 rows. The observation bundle matched across conditions.

Initial AMB-02 sales-cell readout:

- Control clusters: `C4=3`, `novel_or_unclassified=1`; entropy `0.8113`.
- Treatment clusters: `C4=4`; entropy `0.0`.
- Entropy delta: `-0.8113`.
- Cluster shift total variation: `0.25`.
- Dominant cluster stayed `C4`.
- Parsed rate was `0.75` on both sides.

This is the first endpoint aligned with the question "does clarify reduce interpretation branching?" For this small S0 cell, the clarification reduced machine-observed branching by removing the `novel_or_unclassified` tail while leaving the dominant interpretation unchanged. It is still S0 screening evidence only; it does not claim S1/S2 action conversion, confirmed findings, or Stage 9 readiness all-pass.
