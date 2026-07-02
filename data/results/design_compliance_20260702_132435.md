# Design Compliance Campaign Result

Run date: 2026-07-02

Workspace: `C:\Users\nyham\work\company_twin`

Model path: `openrouter:qwen/qwen3.6-flash`

## Commands

```powershell
python -m pytest -q
company-twin inspect --json
company-twin compliance
company-twin campaign --live --max-live-agent-calls 3
company-twin compliance --campaign-root runs\design_compliance_20260702_132435
```

## Results

- Unit tests: `7 passed` in final validation.
- Design inventory: `50` documents loaded, `12` spans, `10` probes, `8` seats.
- Static compliance: `passed=true`, `failure_count=0`.
- Live design-compliance campaign root: `runs\design_compliance_20260702_132435`.
- Live DeepAgent/OpenRouter/Qwen calls used: `3`.
- S0 matrix rows generated: `182`.
- Campaign compliance: `passed=true`, `failure_count=0`.

## Run Bundles Produced

- `anchor_s2_seed0`
- `s0_P-01_emp-A_seed0`
- `s1_P-04_emp-A_seed0`
- `s2_all_seats_seed0`

Each run bundle produced `config.json`, `meta.json`, `attempts.jsonl`, `basis_records.jsonl`, `chat_channel.jsonl`, `world_ledger.jsonl`, `oracle_l0.parquet`, and deterministic triage outputs under `triage/`.

## Campaign Summary

```json
{
  "campaign_root": "C:\\Users\\nyham\\work\\company_twin\\runs\\design_compliance_20260702_132435",
  "model": "openrouter:qwen/qwen3.6-flash",
  "live": true,
  "live_calls_used": 3,
  "s0_matrix_rows": 182,
  "compliance": {
    "passed": true,
    "failure_count": 0,
    "failures": []
  }
}
```
