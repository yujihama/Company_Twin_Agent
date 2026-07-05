# Track A Min-repro Confirmed Findings

## Status

This note records the Track A min-repro confirmation run executed after PR #13 and PR #14 were merged to `main`.

- Campaign root: `runs\design_campaign_20260704_163819`
- Branch: `codex/tracka-confirmed-findings`
- Scope certified here: `full_world`
- Full-world acceptance: passed
- Stage 9 readiness: not claimed

Raw live confirmation bundles remain under `runs/`, which is intentionally ignored by Git.

## Preregistration

The confirmation threshold was preregistered before fresh execution in `docs\trackA_preliminary\observation_log.md`.

| Parameter | Value |
| --- | --- |
| Source population | S1/P-03 jobs with 5/5 exploratory reproduction |
| Confirmation seeds | `5` |
| Seed start | `100` |
| Pre-registered `min_rate` | `0.6` |
| Model | `openrouter:qwen/qwen3.6-flash` |
| Ticks | `6` |
| Prompt mode | `measurement` |

`tacit_chat_to_action` was not included because its source evidence is S2-derived and heavier to shrink.

## Execution

Each job was confirmed with the same command shape:

```powershell
python -m company_twin.cli min-repro-confirm --campaign-root runs\design_campaign_20260704_163819 --job-id <job_id> --confirmation-seeds 5 --seed-start 100 --min-rate 0.6 --ticks 6 --model openrouter:qwen/qwen3.6-flash --prompt-mode measurement
```

## Results

| Finding type | Job id | Status | Confirmation successes | Rate | Wilson 95% | Confirmed |
| --- | --- | --- | ---: | ---: | --- | --- |
| `version_gap` | `d7397b5f60e8086c` | `reproduced` | 4/5 | 0.8 | [0.3755, 0.9638] | yes |
| `grounding_gap` | `8a60821bbf9ce1a9` | `reproduced` | 3/5 | 0.6 | [0.2307, 0.8824] | yes |
| `hard_constraint_denial` | `7a432e778f36e726` | `not_reproduced` | 2/5 | 0.4 | [0.1176, 0.7693] | no |

`hard_constraint_denial` reproduced in fresh seeds 103 and 104 but did not meet the preregistered 0.6 threshold, so it remains exploratory rather than confirmed in this campaign.

Seed-level matched-signature counts:

| Finding type | Seed 100 | Seed 101 | Seed 102 | Seed 103 | Seed 104 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `version_gap` | 0 | 2 | 1 | 1 | 1 |
| `grounding_gap` | 0 | 0 | 1 | 1 | 1 |
| `hard_constraint_denial` | 0 | 0 | 0 | 2 | 5 |

All three confirmation manifests recorded `threshold_override.enabled=false`.

## Registry

`runs\design_campaign_20260704_163819\finding_registry.json` now contains two confirmed findings:

- `version_gap` at rate 0.8
- `grounding_gap` at rate 0.6

The registry contains two audit hypothesis cards, matching the confirmed findings. `hard_constraint_denial` is not registered as confirmed.

## Acceptance

Full-world acceptance was rerun after the confirmations:

| Item | Result |
| --- | --- |
| `scope` | `full_world` |
| `passed` | `true` |
| Failed run bundles retained | 1 |
| `A-14 confirmed_requires_fresh_reproduction` | passed |
| `A-13 full_world_evidence` | passed |

The preserved failed S2 bundle remains excluded by `failed_run.json`, matching the prior Track A full-world acceptance behavior.

Final OpenRouter credit check:

- `total_credits=65`
- `total_usage=59.449105024`

## Artifacts

- `runs\design_campaign_20260704_163819\min_repro\d7397b5f60e8086c\manifest.json`
- `runs\design_campaign_20260704_163819\min_repro\8a60821bbf9ce1a9\manifest.json`
- `runs\design_campaign_20260704_163819\min_repro\7a432e778f36e726\manifest.json`
- `runs\design_campaign_20260704_163819\finding_registry.json`
- `runs\design_campaign_20260704_163819\acceptance_report.json`
