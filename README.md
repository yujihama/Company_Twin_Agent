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
- Experiment controls: S0 campaigns default to multiple cold-read models, S1/S2
  can bind models per seat, and SCC completion-gate switch timing is
  config-driven.
- Runtime corpus mutations: WP-06 M1 operators are catalogued in
  `data/compiled_data/mutation_operators_v1.json` and can be applied with
  `--mutation <mutation_id>` on S0/S1/S2/campaign commands. Run bundles record
  the applied mutation entries, `mutation_hash`, and effective corpus hash.
  Mutation documents are not search-boosted; salience must be modeled
  explicitly if it becomes an experiment variable.
- Observability: ensemble triage writes candidate attribution, min-repro queues,
  min-repro evidence-collation manifests, rule hit rates, detection-miss rates,
  g3 semantic grounding reports, prompt-mode A/B reports, and a deterministic
  behavior coverage map.
- Acceptance: harness-safety gates only.
- Evidence: sanitized WP-01 live evidence is committed under
  `docs/wp01_live_evidence`; sanitized WP-05 prompt-mode A/B evidence is
  committed under `docs/wp05_live_evidence`; raw run bundles remain under
  ignored `runs/`.
- Readiness: Stage 9 gate exists and intentionally fails until the required
  evidence reports are generated and pass.

## Not Yet Claimed

- Raw live full-world S2 + anchor bundles are not committed; only sanitized
  WP-01 evidence is attached.
- `grounding_g3_machine_heuristic_rate` is lexical/machine grounding. The g3
  semantic evaluator is implemented separately and writes
  `g3_semantic_grounding.json`; Stage 9 should use a reviewed/live judge run,
  not the legacy machine heuristic.
- The local deterministic g3 proxy writes only `*_proxy` rates. Readiness
  accepts only unqualified semantic rates produced by allowlisted live judges.
- Candidate attribution and default `min-repro` output remain exploratory.
  Confirmed findings require fresh live confirmation bundles with
  `status=reproduced`; same-campaign evidence collation is not enough.
- WP-05 prompt-mode A/B K>=5x2 evidence exists for the scoped S1/P-04/tick=1
  method-freeze comparison. It is not a scaled S2 or Stage 9 readiness claim.
- WP-06 runtime mutation support does not by itself prove attribution. Use
  `company-twin control-pairs` only to generate delta-one shared-seed manifests;
  attribution still requires fresh live paired runs.
- Stage 9 backcasting, SME blind review, and holdout reports are required before
  experiment-level conclusions.

## Typical Validation

```powershell
python -m compileall -q src tests
pytest -q
python -m company_twin.cli inspect
python -m company_twin.cli lint
python -m company_twin.cli g3 --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS
python -m company_twin.cli g3-export-calibration --source-root runs\design_campaign_YYYYMMDD_HHMMSS --output docs\g3_calibration_samples.jsonl
python -m company_twin.cli prompt-ab-report --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS
python -m company_twin.cli mutation-catalog
python -m company_twin.cli control-pairs --mutation clarify_elderly_understanding_all --k 5 --output runs\control_pairs.json
python -m company_twin.cli min-repro --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS
```

The default live model is loaded from local environment settings and should
normalize to an OpenRouter Qwen model such as:

```text
openrouter:qwen/qwen3.6-flash
```

## Loss-Event Campaign Aggregation

After every sealed S2 run has produced `loss_events.json` and
`loss_event_monitoring.json`, aggregate only the runs named by the sealed plan:

```powershell
python -m company_twin.cli loss-event-campaign `
  --root . `
  --plan docs\progress\loss_campaign_plan.json `
  --batch-manifest runs\m3_batch\batch_manifest.json `
  --output runs\m3_batch\loss_event_campaign.json
```

Repeat `--batch-manifest` for each retry attempt, preserving the original full
manifest first; retries must use a distinct `--batch-dir` so history is not
overwritten. The command verifies that the plan and batch spec existed at the
execution commit, revalidates every run artifact, and never updates readiness.
If the sealed plan declares a mutation-circulation manipulation gate, the
report also requires exact config-derived full-text delivery before the first
assigned endpoint opportunity; any mismatch makes the CLI exit non-zero.
For the current R1-R4 catalog, direct detection coverage is `uncovered`, so
direct miss rates are N/A rather than 100%; occurrence rates remain estimable.

## Full-World Evidence Boundary

For a full-world claim, run a live campaign with S2 and then verify both gates:

```powershell
python -m company_twin.cli campaign --with-s2 --s2-k 1 --s2-ticks 40 --s0-model openrouter:qwen/qwen3.6-flash
python -m company_twin.cli g3 --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS --judge-model openrouter:qwen/qwen3.6-plus
python -m company_twin.cli min-repro --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS
python -m company_twin.cli acceptance --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS --scope full_world
python -m company_twin.cli readiness-reports --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS --overwrite
python -m company_twin.cli stage9-evidence-manifest --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS
python -m company_twin.cli readiness --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS
```

`acceptance --scope full_world` requires a live anchor S2 bundle and a non-anchor
S2 bundle with month-end, customer utterances, agent-originated controlled
actions, action-bound basis, and ensemble artifacts. `ensemble_triage.json`
links to `coverage_map.json`, and run metrics include `rule_hit_rate` plus
`detection_miss_rate` from two-population
`data/compiled_data/detection_rules_v2.json`. `readiness` is stricter and
requires routine smoke, retrieval audit, leak lint, semantic grounding,
backcasting, SME blind review, and holdout evidence.

### Two-level readiness (2026-07-05)

`readiness` reports two levels, both written into `readiness_report.json`:

- `internal_readiness`: the pre-existing 10-item gate plus
  `stage9_evidence_manifest_consistent` (requires `stage9-evidence-manifest`
  to have been run and to match the current report files). This accepts
  `ai_proxy` SME review and single-seed holdout evidence -- it certifies
  internally self-consistent evidence, not an external human-reviewed claim.
  `readiness`'s exit code and top-level `passed` field track this level.
- `external_claim_readiness`: a stricter, informational-but-honest summary
  requiring human_sme review (not ai_proxy), a machine-checkable
  `g3_negative_calibration.json` (specificity on known-clean cases), holdout
  evidence with both positive and negative (no-mutation) controls, and all
  evidence from a single post-fix world version (no `effective_corpus_hash`
  heterogeneity in the evidence manifest). This block is expected mostly
  `false` today and never gates `internal_readiness` or the CLI exit code.

Run `stage9-evidence-manifest` after generating the individual evidence
reports and before `readiness`, so the manifest reflects the report files the
gate is about to check:

```powershell
python -m company_twin.cli backcasting-report --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS
python -m company_twin.cli holdout-score --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS
python -m company_twin.cli sme-score --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS
python -m company_twin.cli stage9-evidence-manifest --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS
python -m company_twin.cli readiness --campaign-root runs\design_campaign_YYYYMMDD_HHMMSS
```
