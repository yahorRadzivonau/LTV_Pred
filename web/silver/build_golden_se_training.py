"""
Step D of the golden-build task: reformat data/golden/golden_all.parquet and
golden_good.parquet (event-grain web silver rows) into the se_training schema
(same columns as data/se_training.parquet, the iOS golden matrix).

PAID events = event_name IN ('trial_converted','subscription_renewed') -- already
defined by real captured money in web silver (spec Sections 5/6), so no extra
"trial_converted by real payment" derivation needed here.

Run: .venv/Scripts/python.exe web/silver/build_golden_se_training.py
"""
import os
from pathlib import Path

import pandas as pd
import numpy as np

# Resolve all project-relative paths from the repository root, regardless of
# the working directory configured in PyCharm or the shell.
ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(ROOT)

SNAPSHOT_TS = pd.Timestamp("2026-07-07T00:00:00Z")
PAID_EVENTS = {"trial_converted", "subscription_renewed"}
DUNNING_EVENTS = {"billing_issue_detected", "entered_grace_period"}


def build_se_training(golden_df, label):
    df = golden_df.copy()
    df["event_datetime"] = pd.to_datetime(df["event_datetime"], utc=True)

    rows_out = []
    for sub_id, g in df.groupby("subscription_id"):
        g = g.sort_values("event_datetime")
        paid = g[g["event_name"].isin(PAID_EVENTS)].sort_values("event_datetime")
        if len(paid) == 0:
            continue

        first = g.iloc[0]
        geo = first["country_code"] if pd.notna(first["country_code"]) and first["country_code"] != "" else "(none)"
        media_source = first["media_source"] if pd.notna(first["media_source"]) and first["media_source"] != "" else "(none)"
        attribution_source = first["media_source_step"] if pd.notna(first["media_source_step"]) else "(none)"
        plan_interval = first["interval_unit"] if pd.notna(first["interval_unit"]) else "unknown"
        cohort_month = str(first["install_date"])[:7] if pd.notna(first["install_date"]) else "(none)"

        trial_started_rows = g[g["event_name"] == "trial_started"]
        trial_started_ts = trial_started_rows["event_datetime"].min() if len(trial_started_rows) else pd.NaT

        first_pay_ts = paid.iloc[0]["event_datetime"]
        weeks_obs = (SNAPSHOT_TS - first_pay_ts).total_seconds() / (7 * 86400)
        trial_days = (first_pay_ts - trial_started_ts).total_seconds() / 86400 if pd.notna(trial_started_ts) else np.nan

        terminal_rows = g[g["event_name"] == "subscription_expired"]
        terminal_ts = terminal_rows["event_datetime"].max() if len(terminal_rows) else pd.NaT
        terminal_churn_type = terminal_rows.iloc[-1]["churn_type"] if len(terminal_rows) else None

        paid_list = paid.to_dict("records")
        n_paid = len(paid_list)
        for i, prow in enumerate(paid_list):
            step_k = i + 1
            pay_ts = prow["event_datetime"]
            if i + 1 < n_paid:
                next_ts = paid_list[i + 1]["event_datetime"]
                days_to_next = (next_ts - pay_ts).total_seconds() / 86400
                between = g[(g["event_datetime"] > pay_ts) & (g["event_datetime"] < next_ts)]
                had_dunning = between["event_name"].isin(DUNNING_EVENTS).any()
                outcome = "recovered" if had_dunning else "renewed"
            else:
                days_to_next = np.nan
                if pd.notna(terminal_ts) and terminal_ts > pay_ts:
                    if terminal_churn_type == "voluntary":
                        outcome = "voluntary_cancel"
                    elif terminal_churn_type == "involuntary":
                        outcome = "billing_issue"
                    else:
                        outcome = "churned"
                else:
                    outcome = "renewed"  # still alive / right-censored, no evidence of death yet

            rows_out.append({
                "sub_id": sub_id,
                "step_k": step_k,
                "state_from": "active",
                "outcome": outcome,
                "weeks_obs": weeks_obs,
                "pay_ts": pay_ts,
                "days_to_next": days_to_next,
                "app_id": prow["app_id"],
                "geo": geo,
                "media_source": media_source,
                "attribution_source": attribution_source,
                "plan_interval": plan_interval,
                "trial_days": trial_days,
                "cohort_month": cohort_month,
                "billing_day_of_month": pay_ts.day,
            })

    out = pd.DataFrame(rows_out)
    out["billing_day_of_month"] = out["billing_day_of_month"].astype("int32")
    print(f"[{label}] input golden rows: {len(golden_df)}, distinct subscriptions: {golden_df['subscription_id'].nunique()}")
    print(f"[{label}] se_training rows (payment steps): {len(out)}, distinct sub_id: {out['sub_id'].nunique()}")
    return out


golden_all = pd.read_parquet("data/golden/golden_all.parquet")
golden_good = pd.read_parquet("data/golden/golden_good.parquet")

se_all = build_se_training(golden_all, "golden_all")
se_good = build_se_training(golden_good, "golden_good")

se_all.to_parquet("data/golden/golden_all_se_training.parquet", index=False)
se_good.to_parquet("data/golden/golden_good_se_training.parquet", index=False)
print("\nwrote data/golden/golden_all_se_training.parquet")
print("wrote data/golden/golden_good_se_training.parquet")

print("\n=== 5 golden_good se_training rows (sample) ===")
print(se_good.sample(min(5, len(se_good)), random_state=42).to_string())
