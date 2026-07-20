"""
Step 4 of the golden-build task: apply the iOS MAP model AS-IS (h_base, multipliers,
hr -- no refit, no web calibration) to the web golden se_training sets, and run a
single calendar-backtest slice on the last 8 weeks to sanity-check it before charting.

Run: .venv/Scripts/python.exe web_baseline_backtest.py
"""
import numpy as np
import pandas as pd
from models import common, map_model

SNAPSHOT_TS_WEB = pd.Timestamp("2026-07-07", tz="UTC")
T = SNAPSHOT_TS_WEB - pd.Timedelta(weeks=8)
YOUNG_START = T - pd.Timedelta(weeks=8)
YOUNG_END = T - pd.Timedelta(weeks=1)
HORIZONS = [1, 4, 12]

print(f"T (cutoff) = {T.date()}, young window = [{YOUNG_START.date()}, {YOUNG_END.date()}]")

# ============================================================
# Fit the iOS model ONCE on iOS data. Frozen from here on -- never refit on web.
# ============================================================
mx_ios = common.load_matrix()
apps_info_ios = common.select_apps(mx_ios)
state_ios = map_model.fit(mx_ios, apps_info_ios)
print(f"iOS state (frozen): h_base weeks 1..{len(state_ios['h_base'])}, "
      f"levers {list(state_ios['multipliers'].keys())}")


def weeks_obs_at(df, cutoff):
    df = df.copy()
    first_pay = df.groupby("sub_id")["pay_ts"].transform("min")
    df["weeks_obs"] = (cutoff - first_pay).dt.total_seconds() / (7 * 86400)
    return df


def run_backtest(path, label):
    mx_web = pd.read_parquet(path)
    mx_web["survived"] = mx_web["outcome"].isin(["renewed", "recovered"]).astype(int)
    mx_web["died"] = 1 - mx_web["survived"]

    first_pay = mx_web.groupby("sub_id")["pay_ts"].min()
    young_subs = first_pay[(first_pay >= YOUNG_START) & (first_pay <= YOUNG_END)].index
    print(f"\n[{label}] young-at-T subs (first pay in window): {len(young_subs)}")
    if len(young_subs) == 0:
        print(f"[{label}] SKIP: 0 young subs, backtest impossible for this T")
        return None

    cohort_full = mx_web[mx_web["sub_id"].isin(young_subs)]
    cohort_visible_raw = mx_web[(mx_web["sub_id"].isin(young_subs)) & (mx_web["pay_ts"] < T)].copy()
    assert (cohort_visible_raw["pay_ts"] < T).all(), "future leakage"
    cohort_visible = weeks_obs_at(cohort_visible_raw, T)

    first_pay_cohort = cohort_visible_raw["pay_ts"].min()
    weeks = max(1, min(8, (T - first_pay_cohort).days // 7))
    print(f"[{label}] weeks (visible-history scalar for hr) = {weeks}")

    pred = map_model.predict(state_ios, cohort_visible, weeks)
    fact, mature, _ = common.direct_survival(cohort_full)

    rows = []
    for h in HORIZONS:
        f = fact.get(h, np.nan)
        m = pred.get(h, np.nan)
        n_mature = mature.get(h, 0)
        if pd.notna(f) and f > 0:
            signed = (m - f) / f * 100
            absval = abs(m - f) / f * 100
        else:
            signed = absval = np.nan
        rows.append({"horizon": h, "fact": f, "pred_map": m, "n_mature": n_mature,
                     "signed_pct_err": signed, "abs_pct_err": absval})
    out = pd.DataFrame(rows)
    print(f"[{label}] backtest (fact vs frozen-iOS-MAP prediction):")
    print(out.to_string(index=False))
    return out


res_all = run_backtest("data/golden/golden_all_se_training.parquet", "golden_all")
res_good = run_backtest("data/golden/golden_good_se_training.parquet", "golden_good")
