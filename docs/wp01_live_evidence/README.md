# WP-01 Live Evidence

This directory contains sanitized evidence for the WP-01 live execution pass.
Raw run bundles remain under `runs/` and are intentionally not committed.

## Primary Run

- Campaign root: `runs/design_campaign_20260704_012445`
- Command:
  `python -m company_twin.cli campaign --with-s2 --s2-k 1 --s2-ticks 12 --s0-limit 4 --s1-k 1 --s1-probe P-01 --s0-variants 2 --s0-model openrouter:qwen/qwen3.6-flash --s0-model openrouter:qwen/qwen3.6-plus`
- Result: `acceptance_passed=true`
- Acceptance scope: `full_world`

## Validation

- Unit test baseline before docs: `pytest -q` -> `71 passed, 1 skipped`
- Unit test after docs: `pytest -q` -> `71 passed, 1 skipped`
- Full-world acceptance CLI: `passed=true`
- Acceptance pytest: `COMPANY_TWIN_ACCEPT_ROOT=... pytest tests/acceptance -q` -> `1 passed`
- Readiness gate: failed as expected; full-world harness acceptance passed, but Stage 9 readiness inputs are still missing.

## Model Finding

The default S0 cold-read pair exposed a live-model adherence issue:
`openrouter:qwen/qwen3.5-9b` returned empty S0 responses for both variants in
`runs/design_campaign_20260704_002346`, causing A-06 to fail
(`multimodel_cell=false`). The primary run therefore used
`openrouter:qwen/qwen3.6-plus` as the second cold-read model; both Qwen 3.6
models parsed at `2/2`.

## Files

- `campaign_summary.sanitized.json`: path-sanitized campaign summary plus command.
- `acceptance_report.sanitized.json`: path-sanitized full-world acceptance report.
- `ensemble_triage.sanitized.json`: aggregate triage counts without raw bodies.
- `representative_bundle_meta.json`: representative bundle metadata and metrics.
- `live_execution_summary.json`: one-file execution summary, model adherence observation, and scope boundary.

## Boundary

This evidence supports harness-safety full-world acceptance only. It does not
claim Stage 9 experiment readiness, semantic grounding oracle readiness,
backcasting, SME blind review, holdout validation, or confirmed audit findings.
