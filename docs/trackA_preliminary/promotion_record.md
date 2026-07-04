# S1 promotion record

## Status

S1 promotion was not preregistered and S1 was not executed.

Reason: S0/S1 acceptance failed after the S0 battery. The failed bundle rate was 97 / 420 = 23.1%, exceeding the instruction's 10% interruption threshold. Per the instruction's trouble handling, the run was stopped and reported instead of proceeding to S1 ensemble or S2.

## Intended Criteria, Not Applied

If S0 acceptance is rerun successfully, the intended preregistration criteria from the instruction remain:

- Select by descending entropy.
- Require `parsed_rate >= 0.7`.
- Select at most 3 probes from distinct spans.
- Promote the corresponding probe on each selected span's coverage matrix row.
- S1 endpoint: observe whether S0 reading differences become action differences, such as approval route, evidence shape, and hold/defer behavior.
- Do not claim effect size from S1.

## Context Only

The reconstructed S0 divergence file exists at `runs\design_campaign_20260704_163819\s0_divergence.json`.

Top observed entropy cells before stopping:

| Rank | Span | Role | Primary probe | Entropy | Parsed rate |
| ---: | --- | --- | --- | ---: | ---: |
| 1 | `CONTRA-01` | `application` | `P-03` | 1.5567 | 0.8571 |
| 2 | `AMB-08` | `manager` | `P-01` | 1.5000 | 1.0000 |
| 3 | `CONTRA-01` | `manager` | `P-03` | 1.5000 | 1.0000 |

These are not promoted selections. They are recorded only to make the S0 stop state inspectable.

## Boundary

本書の全観察は実験解禁（Stage 9 readiness）前の探索的予備観察であり、confirmed所見・効果量の主張を含まない。
