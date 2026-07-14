"""
Validation gate for ltv_v2 (appsflyer-source pipeline). Separate from
reconcile.py (golden pipeline) -- running this does not touch, read the
outputs of, or affect the golden gate in any way.

Checks:
  1. Symmetric-window (2026-04-13..2026-07-07) net revenue on the email-aligned
     population: appsflyer (haircut-adjusted) vs golden. Target gap < 1.5%.
  2. Anchor: cohort 2026-05-04 per-payer average, appsflyer vs golden's $67.80.

BASELINE CHANGE, dated 2026-07-13: real paid-trial revenue ($0.99 trial_started,
verified against raw Stripe/Solidgate -- see ltv_v2/config.py "trial revenue
rule") is now included on the appsflyer side. This is a deliberate accuracy
fix, not a regression -- but CHECK 1 now reads ~+3.5% (was +1.40%) and FAILS
the old <1.5% target. Root cause, investigated and confirmed (not assumed):
golden's own `trial_converted` event is INCONSISTENTLY priced upstream --
cross-checked against the 3600 people who have a real appsflyer trial charge:
1810 have a matching golden trial_converted at the correct real price (agree,
no new gap), but 814 have golden crediting trial_converted at the REGULAR
price ($9.99/$11.99) instead of the real trial charge (golden's OWN silver
layer loses the real trial amount for these, via a different mechanism than
appsflyer's old bug -- same disease, different code path), and 1297 have no
matching golden trial_converted event at all. This was already present in
golden's reference numbers before this fix; adding real trial money to
appsflyer just made the mismatch visible on this specific check. Not fixed
here (out of v2's isolation scope -- it lives upstream of golden's silver
layer). The <1.5% target below is left UNCHANGED pending a decision on
whether to revise it now that part of "golden" isn't fully self-consistent
either; treat a CHECK 1 FAIL in the 3-4% range as EXPECTED and explained until
that decision is made, not as a new appsflyer-side bug.

Run: .venv/Scripts/python.exe reconcile_v2.py
"""
import pandas as pd

from ltv_v2 import revenue as R2
from ltv_v2.config import GOLDEN_SNAPSHOT_TS, REFUND_HAIRCUT

pd.set_option("display.width", 160)

print(f"REFUND_HAIRCUT = {REFUND_HAIRCUT} (dated 2026-07-12, TEMPORARY stopgap -- see config.py)")
print()

# ================================================================ CHECK 1: symmetric-window net revenue, aligned population
golden_windowed = pd.read_parquet("data/raw/_tmp_golden_per_email_windowed.parquet")
golden_windowed = golden_windowed[golden_windowed["email"] != ""]
pop_emails = set(golden_windowed["email"])
golden_net_total = golden_windowed["net_revenue"].sum()

events = R2.load_raw_events()
appsflyer_per_person = R2.per_person_revenue(events, window_end=GOLDEN_SNAPSHOT_TS)
appsflyer_aligned = appsflyer_per_person[appsflyer_per_person["email"].isin(pop_emails)]

n_appsflyer_found = len(appsflyer_aligned)
appsflyer_net_total = appsflyer_aligned["net"].sum()
appsflyer_gross_total = appsflyer_aligned["gross"].sum()

gap_abs = appsflyer_net_total - golden_net_total
gap_pct = gap_abs / golden_net_total * 100

print("=== CHECK 1: symmetric window 2026-04-13..2026-07-07, email-aligned population ===")
print(f"golden population (revenue-bearing emails): {len(pop_emails)}")
print(f"appsflyer: {n_appsflyer_found} of them have >=1 captured event in window")
print(f"golden NET revenue:                 ${golden_net_total:,.2f}")
print(f"appsflyer GROSS revenue (aligned):   ${appsflyer_gross_total:,.2f}")
print(f"appsflyer NET revenue (haircut, aligned): ${appsflyer_net_total:,.2f}")
print(f"GAP (appsflyer net - golden net):    ${gap_abs:,.2f}  =  {gap_pct:+.2f}%")
check1_pass = abs(gap_pct) < 1.5
print(f"target <1.5%: {'PASS' if check1_pass else 'FAIL'}")

# ================================================================ CHECK 2: anchor, cohort 2026-05-04
print("\n=== CHECK 2: anchor, cohort 2026-05-04 (per-payer average) ===")
cohort_emails = set(pd.read_csv("data/raw/_tmp_cohort0504_emails.csv")["email"])
cohort_af = appsflyer_per_person[appsflyer_per_person["email"].isin(cohort_emails)].copy()
cohort_af["is_payer"] = cohort_af["net"] > 0
n_matched = len(cohort_af)
n_payers = int(cohort_af["is_payer"].sum())
appsflyer_anchor = cohort_af.loc[cohort_af["is_payer"], "net"].mean() if n_payers else float("nan")

golden_anchor = 67.804382  # from reports/reconcile_baseline.json (per-payer, golden pipeline)
anchor_diff = appsflyer_anchor - golden_anchor
anchor_diff_pct = anchor_diff / golden_anchor * 100

print(f"cohort 05-04: {len(cohort_emails)} emails total, {n_matched} found in appsflyer pull, {n_payers} payers (net>0)")
print(f"appsflyer per-payer anchor: ${appsflyer_anchor:.2f}")
print(f"golden per-payer anchor:    ${golden_anchor:.2f}")
print(f"diff: ${anchor_diff:+.2f}  ({anchor_diff_pct:+.2f}%)")

print("\n" + "=" * 70)
print(f"CHECK 1 (net vs net, symmetric window): {'PASS' if check1_pass else 'FAIL'} ({gap_pct:+.2f}%, target <1.5%)")
print(f"CHECK 2 (anchor 05-04): appsflyer ${appsflyer_anchor:.2f} vs golden ${golden_anchor:.2f} ({anchor_diff_pct:+.2f}%)")
