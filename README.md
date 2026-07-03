# Company Twin Agent

This branch is a live S0/S1 scaffold with guarded full-world gates for the DFH
sales-control Company Twin design.

It must not be presented as a completed full Company Twin world harness or as
Stage 9 experiment readiness until the full-world evidence below exists and the
readiness gate passes.

## Current Scope

- S0: live interpretation battery over schema-validated compiled artifacts.
- S1: live multi-seat episode entry.
- S2: live entry point and acceptance gates exist, but full-world evidence must
  be produced separately with `--with-s2`.
- Acceptance: harness-safety gates only.
- Readiness: Stage 9 gate exists and intentionally fails until the required
  evidence reports are generated and pass.

## Not Yet Claimed

- No attached live full-world S2 + anchor artifact is claimed by this branch.
- `grounding_g3_machine_heuristic_rate` is lexical/machine grounding, not the
  Stage 9 semantic entailment oracle.
- Candidate attribution and min-repro queues are generated, but confirmed
  findings require reproduction evidence.
- Stage 9 backcasting, SME blind review, and holdout reports are required before
  experiment-level conclusions.

## Typical Validation

```powershell
python -m compileall -q src tests
pytest -q
python -m company_twin.cli inspect
python -m company_twin.cli lint
```

The default live model is loaded from local environment settings and should
normalize to an OpenRouter Qwen model such as:

```text
openrouter:qwen/qwen3.6-flash
```

## Full-World Evidence Boundary

For a full-world claim, run a live campaign with S2 and then verify both gates:

```powershell
python -m company_twin.cli campaign --with-s2 --s2-k 1 --s2-ticks 40 --s0-model openrouter:qwen/qwen3.6-flash
python -m company_twin.cli acceptance --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS --scope full_world
python -m company_twin.cli readiness-reports --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS --overwrite
python -m company_twin.cli readiness --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS
```

`acceptance --scope full_world` requires a live anchor S2 bundle and a non-anchor
S2 bundle with month-end, customer utterances, agent-originated controlled
actions, action-bound basis, and ensemble artifacts. `readiness` is stricter and
requires routine smoke, retrieval audit, leak lint, semantic grounding,
backcasting, SME blind review, and holdout evidence.
