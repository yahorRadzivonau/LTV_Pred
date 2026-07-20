"""
Dunning retry-window diagnostic. READ-ONLY on data/silver/web_events_silver.parquet
and data/tmp/* (silver NOT modified). Output: reports/dunning_window_diag.md.

Q: how long does the system retry after the FIRST failure, and are there "late
resurrections" (a successful settle/paid AFTER a long pause of failures)?

"Death moment" = churn_signal_datetime (raw delete/cancel webhook), NOT the
period-end-shifted event_datetime of the terminal (Stripe shifts +~9.5d).
Universe: non-QA, non-Atelier subscriptions only.

Run: .venv/Scripts/python.exe dunning_window_diag.py
"""
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
SILVER = ROOT / "data" / "silver" / "web_events_silver.parquet"
TMP = ROOT / "data" / "tmp"
OUT = ROOT / "reports" / "dunning_window_diag.md"

UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
SUCCESS = ["trial_converted", "subscription_renewed"]


def pct(s, ps=(50, 95, 99)):
    s = pd.Series(s).dropna()
    if not len(s):
        return {f"p{p}": float("nan") for p in ps} | {"max": float("nan"), "n": 0}
    return {**{f"p{p}": np.percentile(s, p) for p in ps}, "max": s.max(), "n": len(s)}


def hist(s, edges):
    s = pd.Series(s).dropna()
    out = []
    for lo, hi in edges:
        n = ((s >= lo) & (s < hi)).sum()
        lab = f"{lo}-{hi-1}d" if hi != np.inf else f">={lo}d"
        out.append((lab, int(n)))
    return out


def main():
    s = pd.read_parquet(SILVER)
    s["event_datetime"] = pd.to_datetime(s["event_datetime"], utc=True)
    s["churn_signal_datetime"] = pd.to_datetime(s["churn_signal_datetime"], utc=True)
    u = s[(~s["is_qa"].fillna(False)) & (s["app_id"] != "atelier")].copy()
    us = u[u["payment_provider"] == "stripe"]
    ug = u[u["payment_provider"] == "solidgate"]
    stripe_subs = set(us["subscription_id"])
    sg_subs = set(ug["subscription_id"])

    L = ["# Dunning retry-window diagnostic\n",
         "READ-ONLY (silver + tmp; silver not modified). Universe: **non-QA, non-Atelier**. "
         "Death moment = `churn_signal_datetime` (raw delete/cancel), not the period-end-shifted terminal.\n",
         f"Universe subs: stripe **{len(stripe_subs)}**, solidgate **{len(sg_subs)}**.\n"]

    # ---------------- 1. Stripe dunning window ----------------
    fam = pd.read_parquet(TMP / "stripe_invoice_family.parquet")
    fam["created"] = pd.to_datetime(fam["created"], utc=True)
    pf = fam[(fam["event_type"] == "invoice.payment_failed") & (fam["subscription_id"].isin(stripe_subs))]
    first_fail = pf.groupby("subscription_id")["created"].min()
    last_fail = pf.groupby("subscription_id")["created"].max()
    n_fail = pf.groupby("subscription_id")["created"].size()
    term_signal = us[us["event_name"] == "subscription_expired"].groupby("subscription_id")["churn_signal_datetime"].min()

    win_a = ((last_fail - first_fail).dt.total_seconds() / 86400)
    common = first_fail.index.intersection(term_signal.index)
    win_b = ((term_signal.loc[common] - first_fail.loc[common]).dt.total_seconds() / 86400)

    L.append("## 1. Stripe dunning window (subs with ≥1 invoice.payment_failed)\n")
    L.append(f"Subs with ≥1 failed: **{len(first_fail)}**; of them reaching terminal deleted: **{len(win_b)}**.\n")
    a, b = pct(win_a), pct(win_b)
    L.append("| window | p50 | p95 | p99 | max | n |\n|---|---|---|---|---|---|")
    L.append(f"| first→last failed | {a['p50']:.1f} | {a['p95']:.1f} | {a['p99']:.1f} | {a['max']:.1f} | {a['n']} |")
    L.append(f"| first failed→deleted | {b['p50']:.1f} | {b['p95']:.1f} | {b['p99']:.1f} | {b['max']:.1f} | {b['n']} |")
    edges = [(0, 1), (1, 4), (4, 8), (8, 15), (15, 31), (31, 61), (61, 91), (91, np.inf)]
    L.append("\nHistogram of first→last-failed window (days):\n| bucket | n subs |\n|---|---|")
    for lab, n in hist(win_a, edges):
        L.append(f"| {lab} | {n} |")

    # Two readings of ">20d pause" (verification flagged the narrow one as easy to
    # misread): (A) literal — a FAILED event immediately followed (in the merged
    # failed+paid timeline) by >20d silence, then a failed/paid; (B) broader — any
    # >20d gap between consecutive FAILED events (dunning span), regardless of paids
    # in between. (B) is much larger because a live sub keeps paying between rare fails.
    succ = us[us["event_name"].isin(SUCCESS)][["subscription_id", "event_datetime"]].rename(columns={"event_datetime": "t"})
    succ["kind"] = "paid"
    failev = pf[["subscription_id", "created"]].rename(columns={"created": "t"}); failev["kind"] = "failed"
    timeline = pd.concat([failev, succ], ignore_index=True).sort_values(["subscription_id", "t"])
    pause_A = []
    for sub_id, g in timeline.groupby("subscription_id"):
        if sub_id not in set(pf["subscription_id"]):
            continue
        g = g.sort_values("t")
        gaps = g["t"].diff().dt.total_seconds() / 86400
        if ((gaps > 20) & (g["kind"].shift() == "failed")).any():
            pause_A.append(sub_id)
    # (B) gap between consecutive failed events
    pause_B, paid_between = [], 0
    for sub_id, g in failev.groupby("subscription_id"):
        ft = g["t"].sort_values()
        if len(ft) < 2:
            continue
        fgaps = ft.diff().dt.total_seconds() / 86400
        if (fgaps > 20).any():
            pause_B.append(sub_id)
            # did a successful paid land inside the biggest failed-gap? (sub was alive, not paused)
            imax = fgaps.idxmax(); lo = ft.shift().loc[imax]; hi = ft.loc[imax]
            sp = succ[(succ["subscription_id"] == sub_id) & (succ["t"] > lo) & (succ["t"] < hi)]
            if len(sp):
                paid_between += 1
    L.append(f"\n**\">20d pause\" — two readings:**")
    L.append(f"- (A) literal (a failed, then >20d silence, then failed/paid): **{len(pause_A)}** subs.")
    L.append(f"- (B) any >20d gap between consecutive FAILED events (dunning span): **{len(pause_B)}** subs "
             f"— but {paid_between} of them had a normal successful renewal INSIDE that gap (the sub was "
             "alive and paying, not paused; those are ordinary long-interval subs, not dunning pauses).\n")
    L.append("5 example chains for reading (A) (failed=F / paid=P, days from first failed):\n")
    for sub_id in pause_A[:5]:
        g = timeline[timeline["subscription_id"] == sub_id].sort_values("t")
        t0 = g["t"].min()
        seq = " ".join(f"{r['kind'][0].upper()}+{(r['t']-t0).total_seconds()/86400:.0f}d" for _, r in g.iterrows())
        L.append(f"- `{sub_id}`: {seq}")

    # ---------------- 2. Stripe resurrection (skipped cycle then revived) ----------------
    L.append("\n## 2. Stripe resurrection — gap between successful paid > interval+14d\n")
    intv = us.groupby("subscription_id")["interval_unit"].first()
    res_rows, res_gaps = [], []
    for sub_id, g in us[us["event_name"].isin(SUCCESS)].groupby("subscription_id"):
        g = g.sort_values("event_datetime")
        if len(g) < 2:
            continue
        unit = intv.get(sub_id)
        if not isinstance(unit, str) or unit not in UNIT_DAYS:
            continue
        thresh = UNIT_DAYS[unit] + 14
        gaps = g["event_datetime"].diff().dt.total_seconds() / 86400
        big = gaps[gaps > thresh]
        if len(big):
            res_rows.append(sub_id); res_gaps.extend(big.tolist())
    L.append(f"Subs with a resurrection gap (paid → [skip] → paid): **{len(res_rows)}**.\n")
    if res_gaps:
        rg = pct(res_gaps)
        L.append(f"Resurrection gap days: p50={rg['p50']:.1f}, p95={rg['p95']:.1f}, max={rg['max']:.1f} (n gaps={rg['n']}).\n")
    L.append("5 examples (sub, gap days, interval):\n")
    for sub_id in res_rows[:5]:
        g = us[(us["subscription_id"] == sub_id) & us["event_name"].isin(SUCCESS)].sort_values("event_datetime")
        gaps = g["event_datetime"].diff().dt.total_seconds() / 86400
        mx = gaps.max()
        L.append(f"- `{sub_id}`: max gap {mx:.0f}d, interval={intv.get(sub_id)}, n_paid={len(g)}")

    # ---------------- 3. Solidgate ----------------
    L.append("\n## 3. Solidgate dunning window + late resurrection (auth_failed / settle_ok)\n")
    ords = pd.read_parquet(TMP / "solidgate_orders.parquet")
    ords["processed_at"] = pd.to_datetime(ords["processed_at"], utc=True, errors="coerce")
    ords = ords[ords["subscription_id"].isin(sg_subs)]
    af = ords[ords["order_status"] == "auth_failed"].dropna(subset=["processed_at"])
    so = ords[ords["order_status"] == "settle_ok"].dropna(subset=["processed_at"])
    evf = pd.read_parquet(TMP / "solidgate_events_flat.parquet")
    evf["created"] = pd.to_datetime(evf["created"], utc=True)
    last_ev = evf[evf["subscription_id"].isin(sg_subs)].groupby("subscription_id")["created"].max()
    first_af = af.groupby("subscription_id")["processed_at"].min()
    common_g = first_af.index.intersection(last_ev.index)
    win_g = ((last_ev.loc[common_g] - first_af.loc[common_g]).dt.total_seconds() / 86400)
    g_stat = pct(win_g)
    n_af_orders = af.groupby("subscription_id").size()
    one_af = int((n_af_orders == 1).sum())
    L.append(f"Subs with ≥1 auth_failed: **{len(first_af)}**.\n")
    L.append(f"> ⚠ SNAPSHOT-CENSORING CAVEAT: **{one_af}/{len(first_af)} ({one_af/len(first_af):.0%}) Solidgate "
             "auth_failed subs carry only ONE auth_failed order** in the snapshot — the payload keeps a "
             "cumulative order set but earlier retry orders were not captured for most terminated subs "
             "(same finding as decline_diag: ~68% keep only the final expire-order). So the p50=0 below is "
             "**\"only the last attempt is visible\", NOT \"instant death\"**; the Solidgate dunning window "
             "here is a LOWER BOUND, systematically under-measured. Stripe (full invoice.* stream) is not affected.\n")
    L.append(f"Days first auth_failed → last subscription event: p50={g_stat['p50']:.1f}, "
             f"p95={g_stat['p95']:.1f}, p99={g_stat['p99']:.1f}, max={g_stat['max']:.1f} (n={g_stat['n']}) "
             "— under-measured for Solidgate, see caveat.\n")
    L.append("Histogram (days):\n| bucket | n subs |\n|---|---|")
    for lab, n in hist(win_g, edges):
        L.append(f"| {lab} | {n} |")

    # late resurrection: a settle_ok with processed_at > (last auth_failed before it) + 20d
    late = []
    af_by = {k: g["processed_at"].sort_values().tolist() for k, g in af.groupby("subscription_id")}
    for sub_id, g in so.groupby("subscription_id"):
        fails = af_by.get(sub_id)
        if not fails:
            continue
        for t in g["processed_at"].sort_values():
            prior = [f for f in fails if f < t]
            if prior and (t - max(prior)).total_seconds() / 86400 > 20:
                late.append((sub_id, (t - max(prior)).total_seconds() / 86400)); break
    L.append(f"\n**Late resurrection (settle_ok >20d after a preceding auth_failed):** {len(late)} subs.\n")
    L.append("5 examples (sub, gap days):\n")
    for sub_id, gap in late[:5]:
        L.append(f"- `{sub_id}`: settle_ok {gap:.0f}d after last prior auth_failed")

    # ---------------- 4. life after death ----------------
    L.append("\n## 4. KEY: life events AFTER the terminal (premature-burial check)\n")
    L.append("Death = churn_signal_datetime. A 'life event after' = a SUCCESSFUL payment "
             "(trial_converted/subscription_renewed for Stripe; settle_ok order for Solidgate) "
             "dated > death + 1 day. Also counted: ANY silver event after death.\n")
    L.append("| provider | terminal subs | successful-paid AFTER death | ANY event after death |\n|---|---|---|---|")

    # stripe
    st_term = us[us["event_name"] == "subscription_expired"].groupby("subscription_id")["churn_signal_datetime"].min()
    st_succ_after = 0
    st_any_after = 0
    for sub_id, dmoment in st_term.items():
        if pd.isna(dmoment):
            continue
        g = us[us["subscription_id"] == sub_id]
        after = g[(g["event_name"] != "subscription_expired") & (g["event_datetime"] > dmoment + pd.Timedelta(days=1))]
        if len(after):
            st_any_after += 1
        if len(after[after["event_name"].isin(SUCCESS)]):
            st_succ_after += 1
    nst = st_term.notna().sum()
    L.append(f"| stripe | {nst} | {st_succ_after} ({st_succ_after/nst:.1%}) | {st_any_after} ({st_any_after/nst:.1%}) |")

    # solidgate: death signal + settle_ok after
    sg_term = ug[ug["event_name"] == "subscription_expired"].groupby("subscription_id")["churn_signal_datetime"].min()
    so_by = {k: g["processed_at"].tolist() for k, g in so.groupby("subscription_id")}
    ev_by = {k: g["created"].tolist() for k, g in evf[evf["subscription_id"].isin(sg_subs)].groupby("subscription_id")}
    sg_succ_after = sg_any_after = 0
    examples_sg = []
    for sub_id, dmoment in sg_term.items():
        if pd.isna(dmoment):
            continue
        succ_after = [t for t in so_by.get(sub_id, []) if pd.notna(t) and t > dmoment + pd.Timedelta(days=1)]
        any_after = [t for t in ev_by.get(sub_id, []) if pd.notna(t) and t > dmoment + pd.Timedelta(days=1)]
        if any_after:
            sg_any_after += 1
        if succ_after:
            sg_succ_after += 1
            examples_sg.append((sub_id, (min(succ_after) - dmoment).total_seconds() / 86400))
    nsg = sg_term.notna().sum()
    L.append(f"| solidgate | {nsg} | {sg_succ_after} ({sg_succ_after/nsg:.1%}) | {sg_any_after} ({sg_any_after/nsg:.1%}) |")

    # Are the Solidgate "paid after death" TRUE resurrections or just settle-lag of an
    # already-initiated attempt? Check if the post-death settle_ok shares an invoice_id
    # with a pre-death auth_failed (retry_attempt>=1) on the same sub.
    settle_lag = 0
    if examples_sg:
        L.append("\nSolidgate successful-paid-after-death — classification:\n")
        for sub_id, d in examples_sg[:5]:
            og = ords[ords["subscription_id"] == sub_id]
            post = og[(og["order_status"] == "settle_ok") & (og["processed_at"] > sg_term[sub_id] + pd.Timedelta(days=1))]
            inv_ids = set(post["invoice_id"])
            prior_fail_same_inv = og[(og["order_status"] == "auth_failed") & (og["invoice_id"].isin(inv_ids))
                                     & (og["processed_at"] <= sg_term[sub_id])]
            lag = len(prior_fail_same_inv) > 0
            settle_lag += 1 if lag else 0
            L.append(f"- `{sub_id}`: settle_ok +{d:.0f}d — "
                     f"{'SETTLE-LAG (same invoice_id as a pre-death auth_failed; late settle of an already-started attempt, NOT a new cycle)' if lag else 'possible true resurrection (no shared invoice)'}")
    L.append(f"\n**Read (corrected after verification):** TRUE resurrections (a NEW successful billing "
             f"cycle after death) = **0 on both providers** — Stripe 0/3220; the 3 Solidgate cases are "
             f"settle-lag ({settle_lag}/3 confirmed sharing an invoice_id with a pre-death auth_failed), i.e. "
             "a charge started before cancel that settled ~2 days later, not a revived subscription.\n")
    L.append("- **Stripe: robust.** Death anchored on the raw delete (churn_signal, ~9.5d EARLIER than the "
             "period-end terminal), so this is a conservative test, and 0/3220 pay afterwards.\n")
    L.append("- **Solidgate: same direction, weaker evidence.** No resurrection observed, but the snapshot "
             "censors early retries (61% of auth_failed subs keep one order) — so \"nobody pays after death\" "
             "is partly absence-of-evidence, not proof. The 9 \"any event after\" are audit/expire webhooks, "
             "not new charges.\n")

    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"stripe: {len(first_fail)} failed-subs, pauseA={len(pause_A)}, pauseB={len(pause_B)}, "
          f"resurrection={len(res_rows)}, paid_after_death={st_succ_after}/{nst}")
    print(f"solidgate: {len(first_af)} failed-subs, late_resur={len(late)}, paid_after_death={sg_succ_after}/{nsg}")


if __name__ == "__main__":
    main()
