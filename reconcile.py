"""
Reconciliation gate for the LTV pipeline refactor.

Recomputes the frozen reference outputs (anchor 05-04, calibration alpha/beta,
deliverable CSV hashes) from the CURRENT code + data and compares them to
reports/reconcile_baseline.json. Every refactor step must leave this PASS.

Run: .venv/Scripts/python.exe reconcile.py
Exit code 0 = all PASS, 1 = any FAIL.
"""
import hashlib
import json
import sys

import numpy as np
import pandas as pd

BASELINE = json.load(open("reports/reconcile_baseline.json", encoding="utf-8"))
TOL = BASELINE["tolerance"]["float_abs"]
results = []  # (name, ok, expected, got)


def check(name, expected, got, exact=False):
    if exact:
        ok = expected == got
    else:
        ok = abs(float(expected) - float(got)) <= TOL
    results.append((name, ok, expected, got))
    return ok


def md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()


# ---- 1. per-sub anchor + calibration via the compare/calibration path ----
def recompute_anchor_and_calibration():
    from core.web_calibration import (
        load_golden, filter_provider_and_app, subscription_start_table,
        local_paid_events, load_web_matrix, sql_style_summary, fit_web_ios_calibration,
        SNAPSHOT_TS, TARGET_STRIPE_PRICE_ID,
    )
    from core import common, map_model
    from ltv.config import (
        MIN_FIRST_PAYERS, MIN_MATURE_REBILL, RELIABILITY_N_THRESHOLD as RELIABILITY_N,
    )

    golden = load_golden()
    filtered, _ = filter_provider_and_app(golden)
    starts = subscription_start_table(filtered)
    starts["cohort_week"] = (
        starts["subscription_start_ts"] - pd.to_timedelta(starts["subscription_start_ts"].dt.weekday, unit="D")
    ).dt.normalize()
    target = pd.Index(filtered.loc[filtered.price_id.eq(TARGET_STRIPE_PRICE_ID), "subscription_id"].unique())
    starts9 = starts[starts.subscription_id.isin(target)]
    paid_all = local_paid_events(filtered, target)
    web_all = load_web_matrix()
    matrix_subs = pd.Index(web_all.sub_id.dropna().unique(), dtype="string")

    # per-sub anchor, cohort 2026-05-04
    cw = pd.Timestamp("2026-05-04", tz="UTC")
    cohort_subs = starts9.loc[starts9.cohort_week.eq(cw), "subscription_id"].unique()
    cohort_paid = paid_all[paid_all.subscription_id.isin(cohort_subs)]
    first_payers = pd.Index(cohort_paid.loc[cohort_paid.rebill_number.eq(0), "subscription_id"].unique(), dtype="string")
    common_payers = first_payers.intersection(matrix_subs)
    s = sql_style_summary(cohort_paid, common_payers, 7)
    ltv7 = float(s.loc[s["rebill_number"] == 7, "payer_ltv"].iloc[0])

    # calibration alpha/beta (live lstsq, same as web_person_level_tables_v2.py)
    mx = common.load_matrix(); state_ios = map_model.fit(mx, common.select_apps(mx))

    def bcd(cwk):
        css = pd.Index(starts9.loc[starts9.cohort_week.eq(cwk), "subscription_id"].unique())
        cp = paid_all[paid_all.subscription_id.isin(css)]
        fp = pd.Index(cp.loc[cp.rebill_number.eq(0), "subscription_id"].unique(), dtype="string")
        if len(fp) < MIN_FIRST_PAYERS:
            return None
        cpay = fp.intersection(matrix_subs)
        age = int((SNAPSHOT_TS.tz_localize(None) - cwk.tz_localize(None)).days // 7)
        mmr = max(0, age - 2)
        if mmr < MIN_MATURE_REBILL:
            return None
        return {"N": len(cpay), "max_mature_rebill": mmr, "fact": sql_style_summary(cp, cpay, mmr).set_index("rebill_number")}

    cohorts = [c for c in (bcd(cw2) for cw2 in sorted(starts9.cohort_week.unique())) if c]
    alpha, beta, kmr = fit_web_ios_calibration(cohorts, state_ios["h_base"], RELIABILITY_N)
    return ltv7, float(alpha), float(beta), kmr


# ---- 2. per-payer anchor from the per-person fixed data ----
def recompute_per_payer_anchor():
    pop = pd.read_parquet("data/raw/_tmp_pop_fixed.parquet")
    m = pd.read_csv("data/raw/_tmp_cohort0504_emails.csv")
    a = pop[pop["email"].isin(set(m["email"]))]
    payer = float(a.loc[a["is_payer"], "total"].mean())
    attr = float(a["total"].mean())
    return payer, attr, len(a), int(a["is_payer"].sum())


print("Recomputing reference outputs from current code + data ...")
ltv7, alpha, beta, kmr = recompute_anchor_and_calibration()
payer, attr, n_matched, n_payers = recompute_per_payer_anchor()

A = BASELINE["anchor_cohort_2026_05_04"]
C = BASELINE["calibration"]
check("anchor per-sub payer_ltv @ rebill7", A["per_sub_payer_ltv_rebill7"], ltv7)
check("anchor per-payer base+ups", A["per_payer_base_plus_ups"], payer)
check("anchor per-attributed base+ups", A["per_attributed_base_plus_ups"], attr)
check("anchor n_matched", A["n_matched_into_pop"], n_matched, exact=True)
check("anchor n_payers", A["n_payers"], n_payers, exact=True)
check("calibration alpha", C["alpha"], alpha)
check("calibration beta", C["beta"], beta)
check("calibration k_max_reliable", C["k_max_reliable"], kmr, exact=True)

for path, want in BASELINE["deliverable_csv_md5"].items():
    try:
        check(f"md5 {path}", want, md5(path), exact=True)
    except FileNotFoundError:
        results.append((f"md5 {path}", False, want, "FILE MISSING"))

print("\n{:<45} {:>8}  {}".format("CHECK", "STATUS", "expected -> got"))
print("-" * 100)
n_fail = 0
for name, ok, exp, got in results:
    if not ok:
        n_fail += 1
    status = "PASS" if ok else "FAIL"
    exp_s = f"{exp:.6f}" if isinstance(exp, float) else str(exp)
    got_s = f"{got:.6f}" if isinstance(got, float) else str(got)
    print("{:<45} {:>8}  {} -> {}".format(name[:45], status, exp_s, got_s))

print("-" * 100)
if n_fail == 0:
    print(f"ALL {len(results)} CHECKS PASS -- outputs reproduce the frozen baseline bit-for-bit.")
    sys.exit(0)
else:
    print(f"{n_fail} of {len(results)} CHECKS FAILED -- refactor changed an output. STOP.")
    sys.exit(1)
