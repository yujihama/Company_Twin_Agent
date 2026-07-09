"""Sealed D1b analysis (docs/progress/phase3_d1b_plan_20260709.json, B1-B3).

Computes the pre-registered live-world measurements over the 12-run D1b set;
B2 (propensity) comes from the replay battery and is merged by the caller.

Usage: python scripts/phase3_d1b_analysis.py [--runs-dir runs/phase3_d1b_20260709]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

CONDITIONS = ("control_off", "control_delay", "pressure_off", "pressure_delay")
SEEDS = (920, 921, 922)
ACCEPT_TOOLS = {"submit_application", "request_approval", "approve_application"}


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def run_facts(run_root: Path) -> dict:
    ledger = read_jsonl(run_root / "world_ledger.jsonl")
    apps: dict[str, dict] = {}
    holds_by_app: Counter = Counter()
    escalations_by_app: Counter = Counter()
    contacts_by_customer: Counter = Counter()
    followup_due: list[dict] = []
    followup_delivered = 0
    stall_notices = 0
    for row in ledger:
        payload = row.get("payload") or {}
        event_type = row.get("event_type")
        if event_type == "application_drafted":
            apps[str(payload.get("application_id"))] = {"customer_id": payload.get("customer_id")}
        elif event_type == "defer_or_hold":
            holds_by_app[str(payload.get("application_id"))] += 1
        elif event_type in ("approval_requested", "application_returned"):
            escalations_by_app[str(payload.get("application_id"))] += 1
        elif event_type == "customer_contact":
            contacts_by_customer[str(payload.get("customer_id"))] += 1
        elif event_type == "consequence_followup_due":
            followup_due.append(payload)
        elif event_type == "customer_utterance" and payload.get("follow_up_level"):
            followup_delivered += 1
        elif event_type == "inbox_delivered":
            if ((payload.get("message") or {}).get("notice")) == "stalled_case_review":
                stall_notices += 1

    total_apps = len(apps)
    apps_with_hold = sum(1 for app_id in apps if holds_by_app.get(app_id, 0) > 0)
    apps_with_hold_or_escalation = sum(
        1 for app_id in apps if holds_by_app.get(app_id, 0) > 0 or escalations_by_app.get(app_id, 0) > 0
    )
    # silent stall (sealed B1): stalled (level>=1 fired in delay runs) with no
    # staff action afterward on that application/customer. For off runs, the
    # comparable quantity is applications with NO staff action at all.
    stalled_app_ids = {str(p.get("application_id")) for p in followup_due}
    silent = 0
    for app_id, info in apps.items():
        touched = holds_by_app.get(app_id, 0) > 0 or escalations_by_app.get(app_id, 0) > 0 or contacts_by_customer.get(str(info.get("customer_id")), 0) > 0
        if not touched:
            silent += 1
    return {
        "total_apps": total_apps,
        "apps_with_hold": apps_with_hold,
        "hold_rate": round(apps_with_hold / total_apps, 4) if total_apps else None,
        "hold_or_escalate_rate": round(apps_with_hold_or_escalation / total_apps, 4) if total_apps else None,
        "no_staff_action_apps": silent,
        "no_staff_action_rate": round(silent / total_apps, 4) if total_apps else None,
        "followup_due_count": len(followup_due),
        "followup_due_levels": dict(Counter(p.get("level") for p in followup_due)),
        "followup_delivered": followup_delivered,
        "stalled_case_review_notices": stall_notices,
        "stalled_app_count": len(stalled_app_ids),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-dir", default="runs/phase3_d1b_20260709")
    args = parser.parse_args()
    runs_dir = Path(args.runs_dir)

    facts = {c: {s: run_facts(runs_dir / f"{c}_seed{s}") for s in SEEDS} for c in CONDITIONS}
    report: dict = {"schema": "company_twin.phase3_d1b_live_analysis.v1", "runs_dir": str(runs_dir)}

    # B3 manifestation: every delay run must show followups due AND delivered
    b3_rows = {}
    for cond in ("control_delay", "pressure_delay"):
        b3_rows[cond] = {
            str(s): {
                "followup_due": facts[cond][s]["followup_due_count"],
                "followup_delivered": facts[cond][s]["followup_delivered"],
                "stall_notices": facts[cond][s]["stalled_case_review_notices"],
            }
            for s in SEEDS
        }
    b3_pass = all(
        row["followup_due"] > 0 and row["followup_delivered"] > 0
        for cond_rows in b3_rows.values()
        for row in cond_rows.values()
    )
    off_clean = all(
        facts[cond][s]["followup_due_count"] == 0 for cond in ("control_off", "pressure_off") for s in SEEDS
    )
    report["B3_manifestation"] = {"per_run": b3_rows, "all_delay_runs_manifested": b3_pass, "off_runs_clean": off_clean, "passed": b3_pass and off_clean}

    # B1 hold discipline: condition-level rates + seed pairs
    per_condition = {}
    for cond in CONDITIONS:
        rows = facts[cond]
        per_condition[cond] = {
            "hold_rate_per_seed": {str(s): rows[s]["hold_rate"] for s in SEEDS},
            "hold_or_escalate_rate_per_seed": {str(s): rows[s]["hold_or_escalate_rate"] for s in SEEDS},
            "no_staff_action_rate_per_seed": {str(s): rows[s]["no_staff_action_rate"] for s in SEEDS},
        }
    report["B1_condition_rates"] = per_condition

    pairs = []
    for s in SEEDS:
        pd = facts["pressure_delay"][s]
        po = facts["pressure_off"][s]
        pairs.append({
            "seed": s,
            "hold_or_escalate_delay_minus_off": round(pd["hold_or_escalate_rate"] - po["hold_or_escalate_rate"], 4),
            "no_action_delay_minus_off": round(pd["no_staff_action_rate"] - po["no_staff_action_rate"], 4),
        })
    positive = sum(1 for p in pairs if p["hold_or_escalate_delay_minus_off"] > 0 and p["no_action_delay_minus_off"] < 0)
    report["B1_pressure_stratum_pairs"] = {"pairs": pairs, "restoring_pairs": f"{positive}/3", "passes_2_of_3": positive >= 2}

    report["per_run_facts"] = {c: {str(s): facts[c][s] for s in SEEDS} for c in CONDITIONS}
    out = Path(str(runs_dir) + "_live_analysis.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}")
    print(json.dumps({k: report[k] for k in ("B3_manifestation", "B1_condition_rates", "B1_pressure_stratum_pairs")}, ensure_ascii=False, indent=1)[:4500])


if __name__ == "__main__":
    main()
