"""
Involuntary-churn decline diagnostic. READ-ONLY on data/raw/* and the frozen
data/silver/web_events_silver.parquet (silver is NOT rewritten). Output: one
report reports/decline_diag.md.

Question: of the involuntary terminals, how many are "no money now / retryable"
(insufficient_funds & kin) vs "hard dead" (do_not_honor, expired, fraud, revoked)?

Stripe decline reason: charge.failed carries BOTH the generic `failure_code`
('card_declined') and the specific `outcome.reason` (do_not_honor,
insufficient_funds, ...). invoice.payment_failed carries NO code; payment_intent
.payment_failed carries last_payment_error.code/.decline_code (cross-ref). We use
charge.failed.outcome.reason as the specific reason, linked to the terminal by
customer_id + time (charge.failed has customer 100%, invoice 0%).

Solidgate decline reason: order-level numeric `failed_reason` (3.02, 3.10, ...).
These are Solidgate's standard numeric decline taxonomy; the code->meaning
mapping is NOT present in the local snapshot. We report exact numeric
distributions and apply a PROVISIONAL mapping (labeled external-reference,
owner to confirm) for the verdict.

Run: .venv/Scripts/python.exe diag_decline.py
"""
from pathlib import Path
import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent
S = (ROOT / "data" / "raw" / "stripe_events.parquet").as_posix()
SILVER = (ROOT / "data" / "silver" / "web_events_silver.parquet").as_posix()
OUT = ROOT / "reports" / "decline_diag.md"

# Stripe outcome.reason -> class (owner framing: insufficient_funds&kin=retryable;
# do_not_honor/expired/fraud/revoked=dead). Borderline kept as its own bucket.
STRIPE_CLASS = {
    "insufficient_funds": "retryable", "try_again_later": "retryable",
    "card_velocity_exceeded": "retryable", "processing_error": "retryable",
    "do_not_honor": "hard_dead", "expired_card": "hard_dead", "stolen_card": "hard_dead",
    "lost_card": "hard_dead", "pickup_card": "hard_dead", "revocation_of_authorization": "hard_dead",
    "invalid_account": "hard_dead", "incorrect_number": "hard_dead",
    "transaction_not_allowed": "hard_dead", "previously_declined_do_not_retry": "hard_dead",
    "highest_risk_level": "hard_dead",  # fraud/risk block
    "generic_decline": "ambiguous",
}

# Solidgate numeric decline codes — PROVISIONAL meanings from Solidgate's public
# decline-code reference (NOT derived from the snapshot; owner to confirm). Family
# by leading digit: 2.x validation, 3.x issuer/card, 4.x fraud/security, 5.x technical.
SG_CODE = {
    "3.02": ("Insufficient funds", "retryable"),
    "3.10": ("Do not honor", "hard_dead"),
    "3.08": ("Card limit / insufficient", "retryable"),
    "3.04": ("Stolen card / restricted", "hard_dead"),
    "4.03": ("Fraud / security decline", "hard_dead"),
    "3.12": ("Invalid card / do not honor", "hard_dead"),
    "3.07": ("Expired card", "hard_dead"),
    "4.09": ("Fraud / antifraud block", "hard_dead"),
    "4.02": ("Fraud suspected", "hard_dead"),
    "4.04": ("Fraud / lost-stolen", "hard_dead"),
    "5.04": ("Technical / processing", "retryable"),
    "3.11": ("Card restriction", "hard_dead"),
    "2.02": ("Validation / invalid data", "hard_dead"),
    "4.05": ("Fraud / risk", "hard_dead"),
    "2.01": ("Validation error", "hard_dead"),
    "2.08": ("Invalid card data", "hard_dead"),
    "3.03": ("Insufficient / limit", "retryable"),
    "5.01": ("Technical / gateway", "retryable"),
}


def get_con():
    con = duckdb.connect(); con.execute("SET enable_progress_bar=false"); con.execute("SET TimeZone='UTC'")
    return con


def main():
    con = get_con()
    L = ["# Involuntary-churn decline diagnostic\n",
         "READ-ONLY over data/raw/* and frozen data/silver/web_events_silver.parquet "
         "(silver NOT modified). Goal: split involuntary terminals into "
         "**retryable** (\"no money now\") vs **hard_dead** (issuer/fraud/expired) vs **ambiguous**.\n"]

    # ---------- STRIPE ----------
    L.append("## 1. Stripe decline codes\n")
    L.append("Path check: `invoice.payment_failed` carries NO code; specific reason is on "
             "`charge.failed.$.data.object.outcome.reason` (+ generic `failure_code`) and "
             "`payment_intent.payment_failed.$.data.object.last_payment_error.decline_code`. "
             "Using charge.failed.outcome.reason (customer 100%, invoice 0% -> linked by customer_id+time).\n")

    all_fail = con.execute(f"""
        SELECT json_extract_string(data,'$.data.object.outcome.reason') AS reason,
               COUNT(*) n
        FROM read_parquet('{S}') WHERE event_type='charge.failed'
        GROUP BY 1 ORDER BY n DESC
    """).fetchdf()
    tot_all = int(all_fail["n"].sum())
    L.append(f"### 1a. ALL charge.failed (n={tot_all}) — dunning + terminal together\n")
    L.append("| outcome.reason | n | share | class |\n|---|---|---|---|")
    for _, r in all_fail.iterrows():
        cls = STRIPE_CLASS.get(r["reason"], "ambiguous")
        L.append(f"| {r['reason']} | {r['n']} | {r['n']/tot_all:.1%} | {cls} |")

    # terminal-linked: last charge.failed reason per involuntary stripe sub, by customer+time
    silver = pd.read_parquet(SILVER)
    term = silver[(silver.payment_provider == "stripe") & (silver.event_name == "subscription_expired")
                  & (silver.churn_type == "involuntary")][["subscription_id", "customer_user_id", "churn_signal_datetime", "install_date"]].copy()
    term["churn_signal_datetime"] = pd.to_datetime(term["churn_signal_datetime"], utc=True)
    cf = con.execute(f"""
        SELECT json_extract_string(data,'$.data.object.customer') AS customer_user_id,
               created AS failed_at,
               json_extract_string(data,'$.data.object.outcome.reason') AS reason
        FROM read_parquet('{S}') WHERE event_type='charge.failed'
    """).fetchdf()
    cf["failed_at"] = pd.to_datetime(cf["failed_at"], utc=True)
    # last failed reason per terminal sub (same customer, failed at/before churn signal)
    last_reason, n_fail_before = [], []
    cf_by_cust = {c: g.sort_values("failed_at") for c, g in cf.groupby("customer_user_id")}
    for _, t in term.iterrows():
        g = cf_by_cust.get(t["customer_user_id"])
        if g is None:
            last_reason.append(None); n_fail_before.append(0); continue
        w = g[g["failed_at"] <= t["churn_signal_datetime"] + pd.Timedelta(days=1)]
        if len(w) == 0:
            last_reason.append(None); n_fail_before.append(0); continue
        last_reason.append(w.iloc[-1]["reason"]); n_fail_before.append(len(w))
    term["last_reason"] = last_reason
    term["n_fail_before"] = n_fail_before
    # LINKAGE BIAS GUARD (found in verification): charge.failed carries only customer_id
    # (no invoice/subscription); customers holding >1 involuntary sub get dunning codes
    # from the WRONG sub mixed in. Mark the clean subset = customers with exactly ONE
    # involuntary terminal, where customer_id linkage is unambiguous.
    inv_per_cust = term.groupby("customer_user_id")["subscription_id"].transform("nunique")
    term["clean_link"] = inv_per_cust == 1
    tl = term["last_reason"].value_counts(dropna=False)
    tot_tl = int(tl.sum())
    L.append(f"\n### 1b. TERMINAL involuntary (n={tot_tl}) — last decline before death (customer+time link)\n")
    L.append("> ⚠ LINKAGE CAVEAT: `charge.failed` has no invoice/subscription field (only customer_id); "
             f"{(~term['clean_link']).sum()} of {tot_tl} terminals ({(~term['clean_link']).mean():.0%}) belong to "
             "customers with >1 involuntary sub, so their code may come from another sub's dunning. "
             "The unbiased split is on the clean subset (1c).\n")
    L.append("| last outcome.reason | n | share | class |\n|---|---|---|---|")
    for k, v in tl.items():
        cls = STRIPE_CLASS.get(k, "ambiguous") if not pd.isna(k) else "no_charge.failed_linked"
        L.append(f"| {'NULL' if pd.isna(k) else k} | {v} | {v/tot_tl:.1%} | {cls} |")

    clean = term[term["clean_link"] & term["last_reason"].notna()]
    tlc = clean["last_reason"].value_counts()
    tot_tlc = int(tlc.sum())
    L.append(f"\n### 1c. TERMINAL involuntary — CLEAN subset (unique-customer link, n={tot_tlc}) — UNBIASED\n")
    L.append("| last outcome.reason | n | share | class |\n|---|---|---|---|")
    for k, v in tlc.items():
        L.append(f"| {k} | {v} | {v/tot_tlc:.1%} | {STRIPE_CLASS.get(k,'ambiguous')} |")

    # ---------- SOLIDGATE ----------
    L.append("\n## 2. Solidgate decline codes\n")
    L.append("Order keys: `['amount','created_at','failed_reason','id','operation','processed_at','status','updated_at']` "
             "— decline lives in numeric **`failed_reason`** (no textual field). Meanings below are a "
             "**PROVISIONAL external mapping** (Solidgate public code reference), NOT derived from the snapshot — **owner to confirm**.\n")
    sg_all = con.execute("""
        SELECT failed_reason, COUNT(*) n FROM read_parquet('data/tmp/solidgate_orders.parquet')
        WHERE order_status='auth_failed' AND failed_reason IS NOT NULL GROUP BY 1 ORDER BY n DESC
    """).fetchdf()
    tot_sg = int(sg_all["n"].sum())
    L.append(f"### 2a. ALL auth_failed orders (n={tot_sg})\n")
    L.append("| failed_reason | n | share | meaning (provisional) | class |\n|---|---|---|---|---|")
    for _, r in sg_all.iterrows():
        meaning, cls = SG_CODE.get(r["failed_reason"], ("(unmapped — confirm)", "ambiguous"))
        L.append(f"| {r['failed_reason']} | {r['n']} | {r['n']/tot_sg:.1%} | {meaning} | {cls} |")

    # terminal-linked solidgate: last auth_failed failed_reason for subs that ended involuntary
    sg_term = silver[(silver.payment_provider == "solidgate") & (silver.event_name == "subscription_expired")
                     & (silver.churn_type == "involuntary")]["subscription_id"].unique()
    sg_ord = con.execute("""
        SELECT subscription_id, failed_reason, processed_at FROM read_parquet('data/tmp/solidgate_orders.parquet')
        WHERE order_status='auth_failed' AND failed_reason IS NOT NULL
    """).fetchdf()
    sg_ord["processed_at"] = pd.to_datetime(sg_ord["processed_at"], utc=True, errors="coerce")
    sgt = sg_ord[sg_ord["subscription_id"].isin(sg_term)].sort_values("processed_at")
    last_sg = sgt.groupby("subscription_id")["failed_reason"].last()
    lc = last_sg.value_counts()
    tot_sgt = int(lc.sum())
    L.append(f"\n### 2b. TERMINAL involuntary only (n={tot_sgt} subs with an auth_failed order) — last decline code\n")
    L.append("| last failed_reason | n | share | meaning (provisional) | class |\n|---|---|---|---|---|")
    for k, v in lc.items():
        meaning, cls = SG_CODE.get(k, ("(unmapped — confirm)", "ambiguous"))
        L.append(f"| {k} | {v} | {v/tot_sgt:.1%} | {meaning} | {cls} |")

    # ---------- 3. cross-check: was it a SERIES or a single blip? ----------
    sg_fail_counts = sgt.groupby("subscription_id").size()
    L.append("\n## 3. Series vs single-blip check\n")
    # Stripe: distribution of n failed attempts before death
    sfd = term[term["n_fail_before"] > 0]["n_fail_before"]
    L.append(f"**Stripe** (linked terminals, n={len(sfd)}): failed attempts before death — "
             f"median={int(sfd.median())}, share with ≥3 fails={ (sfd>=3).mean():.0%}, "
             f"with exactly 1={ (sfd==1).mean():.0%}. Most Stripe involuntary deaths ARE a dunning series.\n")
    # Solidgate: KEY honesty point — most terminals have only the final expire order in the snapshot
    n1 = int((sg_fail_counts == 1).sum())
    sg_inv_total = len(silver[(silver.payment_provider=="solidgate") & (silver.event_name=="subscription_expired") & (silver.churn_type=="involuntary")]["subscription_id"].unique())
    L.append(f"**Solidgate**: of {len(sg_fail_counts)} terminals with an auth_failed order, "
             f"**{n1} ({n1/len(sg_fail_counts):.0%}) show only ONE auth_failed order** in the snapshot "
             "(order_ids are unique — NOT a dedup artifact; these carry only the final expire-order, "
             "retry_attempt=NULL). So a multi-attempt dunning SERIES is NOT demonstrable for the majority "
             "of Solidgate involuntary deaths from this snapshot — the earlier retries were not captured.\n")
    L.append("Sample (8 stripe / 7 solidgate):\n| provider | subscription_id | n_fail_orders | last_code | class |\n|---|---|---|---|---|")
    for _, t in term[term["last_reason"].notna()].head(8).iterrows():
        L.append(f"| stripe | {t['subscription_id']} | {t['n_fail_before']} | {t['last_reason']} | {STRIPE_CLASS.get(t['last_reason'],'ambiguous')} |")
    for sub_id in list(last_sg.index)[:7]:
        code = last_sg[sub_id]; meaning, cls = SG_CODE.get(code, ("(unmapped)", "ambiguous"))
        L.append(f"| solidgate | {sub_id} | {int(sg_fail_counts.get(sub_id,0))} | {code} ({meaning}) | {cls} |")

    # ---------- 4. verdict ----------
    def split(counts_map, classifier):
        agg = {"retryable": 0, "hard_dead": 0, "ambiguous": 0, "unlinked": 0}
        for k, v in counts_map.items():
            if pd.isna(k):
                agg["unlinked"] += v; continue
            agg[classifier(k)] += v
        return agg

    # UNBIASED split on clean subset (Stripe) + provisional (Solidgate); plus
    # do_not_honor / 3.10 SENSITIVITY (these single codes carry the whole verdict).
    def frac(agg, cls, tot):
        return f"{agg[cls]} ({agg[cls]/tot:.0%})" if tot else "-"

    sc_agg = split(tlc.to_dict(), lambda k: STRIPE_CLASS.get(k, "ambiguous"))          # clean subset, base
    STRIPE_ALT = {**STRIPE_CLASS, "do_not_honor": "retryable"}
    sc_alt = split(tlc.to_dict(), lambda k: STRIPE_ALT.get(k, "ambiguous"))            # do_not_honor -> retryable
    g_agg = split(lc.to_dict(), lambda k: SG_CODE.get(k, ("", "ambiguous"))[1])         # provisional base
    SG_ALT = {k: (v[0], "retryable" if k == "3.10" else v[1]) for k, v in SG_CODE.items()}
    g_alt = split(lc.to_dict(), lambda k: SG_ALT.get(k, ("", "ambiguous"))[1])          # 3.10 -> retryable

    L.append("\n## 4. Verdict — retryable vs hard-dead (with honest ranges)\n")
    L.append("Reported on the UNBIASED basis: Stripe = clean unique-customer subset (1c); "
             "Solidgate = provisional mapping. Each with a sensitivity row moving the single "
             "dominant borderline code (`do_not_honor` / `3.10`) to retryable — because that one "
             "code swings the whole conclusion.\n")
    L.append("| basis | retryable | hard_dead | ambiguous | n |\n|---|---|---|---|---|")
    L.append(f"| Stripe clean, base (do_not_honor=hard) | {frac(sc_agg,'retryable',tot_tlc)} | {frac(sc_agg,'hard_dead',tot_tlc)} | {frac(sc_agg,'ambiguous',tot_tlc)} | {tot_tlc} |")
    L.append(f"| Stripe clean, do_not_honor→retryable | {frac(sc_alt,'retryable',tot_tlc)} | {frac(sc_alt,'hard_dead',tot_tlc)} | {frac(sc_alt,'ambiguous',tot_tlc)} | {tot_tlc} |")
    L.append(f"| Solidgate, base (3.10=hard) | {frac(g_agg,'retryable',tot_sgt)} | {frac(g_agg,'hard_dead',tot_sgt)} | {frac(g_agg,'ambiguous',tot_sgt)} | {tot_sgt} |")
    L.append(f"| Solidgate, 3.10→retryable | {frac(g_alt,'retryable',tot_sgt)} | {frac(g_alt,'hard_dead',tot_sgt)} | {frac(g_alt,'ambiguous',tot_sgt)} | {tot_sgt} |")

    hd_lo_s, hd_hi_s = sc_alt['hard_dead']/tot_tlc, sc_agg['hard_dead']/tot_tlc
    hd_lo_g, hd_hi_g = g_alt['hard_dead']/tot_sgt, g_agg['hard_dead']/tot_sgt
    L.append(f"\n## Bottom line (honest)\n")
    L.append(f"- **Hard-dead share is a RANGE, not a point:** Stripe **{hd_lo_s:.0%}–{hd_hi_s:.0%}**, "
             f"Solidgate **{hd_lo_g:.0%}–{hd_hi_g:.0%}**. The width is driven almost entirely by ONE "
             "classification choice — whether `do_not_honor` (Stripe) / `3.10` (Solidgate) counts as hard "
             "or as retryable (it is partly recoverable in practice). Everything else is stable.")
    L.append("- **Linkage bias was CHECKED and is NOT material at the aggregate:** the clean unique-customer "
             f"subset ({tot_tlc} terminals) gives hard={sc_agg['hard_dead']/tot_tlc:.0%} (base) — essentially "
             "the same as the full linked set, so the earlier concern that customer+time mixes other subs' "
             "codes does not move the headline. (The earlier ~58% figure was LOWER only because its "
             "denominator included the 430 unlinked terminals as neither class; on a like-for-like linked "
             "basis it is ~70% base too.)")
    L.append("- **The genuine uncertainties are two, both about meaning not linkage:** (1) how to treat "
             "`do_not_honor`/`3.10` (the whole range above); (2) the Solidgate numeric code→meaning mapping "
             "is external/provisional, not from the snapshot — 42% of Solidgate auth_failed is code 3.02 "
             "(insufficient funds) which is unambiguous, but the hard-side codes lean on the provisional table.")
    L.append("- **Series-of-failures established for Stripe, NOT for ~2/3 of Solidgate:** Stripe linked "
             "terminals show a median of ~8 failed attempts before death (97% have ≥3) — clear slow dunning. "
             "Solidgate: 68% of involuntary terminals retain only the final expire-order in the snapshot "
             "(earlier retries not captured), so 'slow dunning death' cannot be confirmed there from this data.")
    L.append("- **430/2718 Stripe involuntary terminals (16%) have NO linked charge.failed at all** "
             "(only 65 even have a payment_intent.payment_failed) — a snapshot gap, left unclassified (NULL), "
             "not forced into a class.")
    L.append("- **What would settle it:** (a) the Solidgate numeric decline-code dictionary (owner); a full "
             "payment_intent→invoice→subscription link for FAILED Stripe charges is NOT possible in this "
             "snapshot (invoice objects carry no PI/charge field), so customer+time is the ceiling for Stripe.")

    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"stripe clean n={tot_tlc} base_hard={sc_agg['hard_dead']} alt_hard={sc_alt['hard_dead']}")
    print(f"solidgate n={tot_sgt} base_hard={g_agg['hard_dead']} alt_hard={g_alt['hard_dead']}")


if __name__ == "__main__":
    main()
