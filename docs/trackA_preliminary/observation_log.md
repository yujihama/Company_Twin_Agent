# Track A preliminary observation log

## Status

Track A was stopped after S0, per the instruction's trouble rule: if the failure rate exceeds 10%, interrupt and report.

- Campaign root: `runs\design_campaign_20260704_163819`
- Latest `main`: `29ab3e7`
- Branch: `codex/tracka-preliminary-observation`
- S0 command requested: 2 models x 2 variants, with `--s1-k 1`
- Actual latest-main S0 matrix: 420 rows, not the instruction document's 840-row estimate
- S0 rows with `s0_answer.json`: 420 / 420
- S0 rows included in reconstructed `s0_results.json`: 420 / 420
- S0 answers with non-empty response text: 304 / 420
- `s0_divergence.json`: generated
- `all_answers_live`: `true`
- S0/S1 acceptance: failed
- Failed bundles: 97 / 420 = 23.1%
- Failed gates: `A-02 live_required` = 97, `A-04 basis_authorship` = 2
- Stop decision: S1 ensemble, S2, G3, full-world acceptance, and readiness were not executed.

## Commands And Timing

| Step | Command | Start (JST) | Elapsed | Result | Cost note |
| --- | --- | ---: | ---: | --- | --- |
| precheck | `pytest -q` | 2026-07-04 16:37:01 | 54.51s | `90 passed, 1 skipped`; instruction expected `96 passed, 1 skipped` | local, no API cost |
| precheck | `python -m company_twin.cli inspect --json` | 2026-07-04 16:38:05 | 1.65s | `documents=50`, `spans=12`, `probes=10`, `seats=8` | local |
| precheck | `python -m company_twin.cli lint` | 2026-07-04 16:38:06 | 1.07s | `passed=true` | local |
| S0 initial | `python -m company_twin.cli campaign --s0-model openrouter:qwen/qwen3.6-flash --s0-model openrouter:qwen/qwen3.6-plus --s0-variants 2 --s1-k 1 --model openrouter:qwen/qwen3.6-flash` | 2026-07-04 16:38:17 | 10355.49s | stopped at `s0_256_P-06_emp-B_v0` due malformed JSONL line | OpenRouter |
| S0 repair | sanitize `s0_256` `attempts.jsonl`, keeping `attempts.raw_corrupt.jsonl`, then run `triage` | 2026-07-04 19:32:40 | 2.15s | dropped one invalid fragment line: `54+00:00"}` | local |
| S0 resume | single-row `s0` loop for indices 257..285 | 2026-07-04 19:33:05 | 1267.10s | stopped at `s0_285_P-08_emp-B_v1` due malformed JSONL line | OpenRouter |
| S0 repair | sanitize `s0_285` `attempts.jsonl`, keeping `attempts.raw_corrupt.jsonl`, then run `triage` | 2026-07-04 19:54:12 | ~1s | dropped one invalid fragment line: `8+00:00"}` | local |
| S0 resume | single-row `s0` loop for indices 286..419 | 2026-07-04 19:54:45 | 2901.85s | completed | OpenRouter |
| S0 aggregate | reconstruct `s0_results.json` and run existing `aggregate_s0_divergence` | 2026-07-04 20:43:39 | 1.07s | 420 rows reconstructed; `answer_total=304`; `cell_count=32` | local |
| S0 acceptance | `python -m company_twin.cli acceptance --campaign-root runs\design_campaign_20260704_163819` | 2026-07-04 20:43:47 | 3.98s | failed; 97 bundles failed `A-02 live_required` | local |
| pricing | `https://openrouter.ai/api/v1/models` for the two Qwen models | 2026-07-04 20:44:41 | 0.50s | prices retrieved | no model generation |

Per-row resume timing is in:

- `runs\design_campaign_20260704_163819\manual_resume_s0_257_419.log`
- `runs\design_campaign_20260704_163819\manual_resume_s0_286_419.log`

## Cost Proxy

The local run artifacts do not contain provider token usage or billed cost. They contain only `prompt_chars` and `response_chars`, so this is not an invoice-grade estimate.

Using OpenRouter prices retrieved at 2026-07-04 20:44 JST and a rough `chars / 4` token proxy:

| Model | LLM invokes | LLM responses | Prompt chars | Response chars | Proxy cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| `openrouter:qwen/qwen3.6-flash` | 210 | 161 | 113,547 | 116,810 | ~$0.0382 |
| `openrouter:qwen/qwen3.6-plus` | 210 | 162 | 113,547 | 148,967 | ~$0.0818 |
| Total | 420 | 323 | 227,094 | 265,777 | ~$0.1200 |

This likely undercounts actual provider tokens because hidden/system framing and provider-side tokenization are not available in the local logs.

## S0 Divergence Snapshot

- Cells: 32
- Cells with `parsed_rate < 0.7`: 0
- Human review queue count: 3
- Human review queue:
  - `AMB-01` / `application` / `P-03`: `novel_or_unclassified=1`, `C2=7`
  - `AMB-04d` / `application` / `P-04`: `novel_or_unclassified=1`, `C1=7`
  - `CONTRA-01` / `second_line` / `P-03`: `novel_or_unclassified=1`, `second_line_route=2`

Top entropy cells, for context only:

| Rank | Span | Role | Primary probe | Entropy | Parsed rate | Clusters |
| ---: | --- | --- | --- | ---: | ---: | --- |
| 1 | `CONTRA-01` | `application` | `P-03` | 1.5567 | 0.8571 | `evidence_first=2`, `second_line_route=3`, `manager_route=2` |
| 2 | `AMB-08` | `manager` | `P-01` | 1.5000 | 1.0000 | `manager_route=2`, `evidence_first=1`, `second_line_route=1` |
| 3 | `CONTRA-01` | `manager` | `P-03` | 1.5000 | 1.0000 | `evidence_first=1`, `second_line_route=2`, `manager_route=1` |

The campaign implementation's automatic promotion logic would have selected `P-03` / `CONTRA-01` / `second_line`, but S1 was not executed because S0 acceptance failed above the 10% stop threshold.

## Failure Distribution

Acceptance failure was concentrated in `A-02 live_required`: `meta.live=True` but no completed `llm_response` record.

| Dimension | Counts |
| --- | --- |
| By probe | `P-01=10`, `P-03=2`, `P-04=4`, `P-05=5`, `P-06=4`, `P-09=16`, `P-10=56` |
| By model | `openrouter:qwen/qwen3.6-flash=49`, `openrouter:qwen/qwen3.6-plus=48` |
| By notable span | `AMB-02=33`, `STR-01=28`, `CONTRA-01=18` |

Because this is 97 / 420 bundles, the failure rate is 23.1%, which exceeds the instruction's 10% interruption threshold.

## Local Artifacts For Follow-Up

- `runs\design_campaign_20260704_163819\s0_divergence.json`
- `runs\design_campaign_20260704_163819\s0_results.json`
- `runs\design_campaign_20260704_163819\acceptance_report.json`
- `runs\design_campaign_20260704_163819\trackA_s0_summary.json`
- `runs\design_campaign_20260704_163819\s0_reconstruction_summary.json`
- `runs\design_campaign_20260704_163819\s0_256_P-06_emp-B_v0\attempts.raw_corrupt.jsonl`
- `runs\design_campaign_20260704_163819\s0_285_P-08_emp-B_v1\attempts.raw_corrupt.jsonl`

Not produced due to the stop condition:

- `ensemble_triage.json`
- `attribution_table.json`
- `g3_semantic_grounding.json`
- S1 representative bundles
- S2 anchor bundle
- `readiness_report.json`

## Min-repro Confirmation Preregistration

Registered before fresh confirmation execution on 2026-07-05, after Track A full-world acceptance passed in follow-up runs.

Confirmation plan:

- Campaign root: `runs\design_campaign_20260704_163819`
- Source population: S1/P-03 jobs with 5/5 exploratory reproduction
- Confirmation seeds: `5`
- Fresh seed start: `100`
- Pre-registered `min_rate`: `0.6`
- Model: `openrouter:qwen/qwen3.6-flash`
- Ticks: `6`
- Prompt mode: `measurement`

| Finding type | Job id | S1 source | Exploratory rate | Pre-registered min_rate | Confirmation seeds |
| --- | --- | --- | ---: | ---: | ---: |
| `version_gap` | `d7397b5f60e8086c` | `P-03` | 5/5 | 0.6 | 5 |
| `grounding_gap` | `8a60821bbf9ce1a9` | `P-03` | 5/5 | 0.6 | 5 |
| `hard_constraint_denial` | `7a432e778f36e726` | `P-03` | 5/5 | 0.6 | 5 |

Rationale: the S1 exploration rate was 5/5 for these three bundles, so a high-rate phenomenon should use a stricter confirmation threshold. `tacit_chat_to_action` is deferred because its source evidence is S2-derived and heavier to shrink.

## Boundary

本書の全観察は実験解禁（Stage 9 readiness）前の探索的予備観察であり、confirmed所見・効果量の主張を含まない。
