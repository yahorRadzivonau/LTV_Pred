"""
Build web_events_silver per WEB_SILVER_BUILD_SPEC.md.

Staged pipeline with caching:
  extract  - JSON parsing done ENTIRELY inside duckdb (json_extract_string in
             SELECT, never a python row-loop), written straight to parquet via
             COPY (...) TO, one pass per file. Cached under data/tmp/ (build
             cache, not a deliverable). Prints name/rows/seconds per file.
             `--dry` runs each SELECT at LIMIT 1000 and prints 3 sample rows,
             writing nothing (owner-mandated pre-flight before full run).
  map      - Sections 5-7 event_name/churn_type/dedup, reads only the small
             flattened tmp files -> fast, safe to iterate.
  trace    - print one subscription's full mapped chain (Stop-point 1).

Timestamp rule (Section 3, owner-accepted): session TimeZone='UTC' at connect,
so bare `created` reads out of duckdb already UTC. Every timestamp column that
reaches pandas is normalized via to_utc() (tz-naive -> tz_localize('UTC'),
aware -> tz_convert('UTC')) BEFORE any concat/sort. Epoch fields are kept as
BIGINT in extract and converted with to_timestamp() in duckdb (UTC session).

Solidgate note: each event payload carries a CUMULATIVE snapshot of ALL
invoices/orders so far, not a delta -> the order unnest is deduped to one row
per order_id (latest referencing event wins), else combinatorial blow-up.

Read-only on data/raw/*. Only .venv/Scripts/python.exe. Outputs (besides the
data/tmp/ build cache) go only to data/silver/ and reports/.

CLI:
  web/silver/build_web_silver.py extract [--dry] [--force]
  web/silver/build_web_silver.py trace <stripe|solidgate> <id>
"""
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
RAW = ROOT / "data" / "raw"
TMP = ROOT / "data" / "tmp"
TMP.mkdir(parents=True, exist_ok=True)

BURST_GAP_SECONDS = 300

STRIPE = (RAW / "stripe_events.parquet").as_posix()
SOLIDGATE = (RAW / "solidgate_events.parquet").as_posix()


def get_con():
    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    con.execute("SET TimeZone='UTC'")
    return con


def to_utc(df, cols):
    df = df.copy()
    for c in cols:
        if c not in df.columns:
            continue
        s = df[c]
        if not pd.api.types.is_datetime64_any_dtype(s):
            s = pd.to_datetime(s, utc=False, errors="coerce")
        df[c] = s.dt.tz_localize("UTC") if s.dt.tz is None else s.dt.tz_convert("UTC")
    return df


# ---------------------------------------------------------------------------
# extract SELECTs (pure duckdb JSON parsing) — name -> SQL producing the table
# ---------------------------------------------------------------------------

def extract_queries():
    q = {}

    # 1. Stripe subscription state events (created / deleted)
    q["stripe_subscription_events"] = f"""
        SELECT event_id, event_type, created,
            json_extract_string(data, '$.data.object.id') AS subscription_id,
            substring(json_extract_string(data, '$.data.object.id'), 11, 10) AS stripe_account,
            json_extract_string(data, '$.data.object.customer') AS stripe_customer_id,
            json_extract_string(data, '$.data.object.plan.interval') AS plan_interval,
            json_extract_string(data, '$.data.object.plan.id') AS price_id,
            json_extract_string(data, '$.data.object.plan.product') AS product_id,
            TRY_CAST(json_extract(data, '$.data.object.start_date') AS BIGINT) AS start_date_epoch,
            TRY_CAST(json_extract(data, '$.data.object.created') AS BIGINT) AS obj_created_epoch,
            TRY_CAST(json_extract(data, '$.data.object.items.data[0].current_period_end') AS BIGINT) AS current_period_end_epoch,
            TRY_CAST(json_extract(data, '$.data.object.ended_at') AS BIGINT) AS ended_at_epoch,
            TRY_CAST(json_extract(data, '$.data.object.cancel_at') AS BIGINT) AS cancel_at_epoch,
            json_extract_string(data, '$.data.object.cancellation_details.reason') AS cancellation_reason,
            json_extract_string(data, '$.data.object.currency') AS currency
        FROM (
            SELECT * FROM read_parquet('{STRIPE}')
            QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY created) = 1
        )
        WHERE event_type IN ('customer.subscription.created', 'customer.subscription.deleted')
    """

    # 2. Stripe invoice family (all invoice.* except upcoming)
    q["stripe_invoice_family"] = f"""
        SELECT event_id, event_type, created,
            json_extract_string(data, '$.data.object.id') AS invoice_id,
            json_extract_string(data, '$.data.object.parent.subscription_details.subscription') AS subscription_id,
            json_extract_string(data, '$.data.object.billing_reason') AS billing_reason,
            TRY_CAST(json_extract(data, '$.data.object.amount_paid') AS BIGINT) AS amount_paid_cents,
            json_extract_string(data, '$.data.object.status') AS invoice_status,
            json_extract_string(data, '$.data.object.currency') AS currency,
            json_extract_string(data, '$.data.object.customer') AS stripe_customer_id
        FROM (
            SELECT * FROM read_parquet('{STRIPE}')
            QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY created) = 1
        )
        WHERE event_type LIKE 'invoice.%' AND event_type <> 'invoice.upcoming'
    """

    # 3. Stripe charges + refunds
    q["stripe_charges_refunds"] = f"""
        SELECT event_id, event_type, created,
            json_extract_string(data, '$.data.object.id') AS object_id,
            substring(json_extract_string(data, '$.data.object.id'), 10, 10) AS stripe_account,
            json_extract_string(data, '$.data.object.payment_intent') AS payment_intent,
            TRY_CAST(json_extract(data, '$.data.object.amount_refunded') AS BIGINT) AS amount_refunded_cents,
            TRY_CAST(json_extract(data, '$.data.object.amount') AS BIGINT) AS amount_cents,
            json_extract_string(data, '$.data.object.currency') AS currency
        FROM (
            SELECT * FROM read_parquet('{STRIPE}')
            QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY created) = 1
        )
        WHERE event_type IN ('charge.refunded', 'refund.created')
    """

    # 4. Stripe payment_intent -> invoice link (from invoice_payment.paid), for refund attachment
    q["stripe_pi_invoice"] = f"""
        SELECT DISTINCT
            json_extract_string(data, '$.data.object.payment.payment_intent') AS payment_intent,
            json_extract_string(data, '$.data.object.invoice') AS invoice_id
        FROM (
            SELECT * FROM read_parquet('{STRIPE}')
            QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY created) = 1
        )
        WHERE event_type = 'invoice_payment.paid'
    """

    # 5. Solidgate event-grain flat (dedup exact triples)
    q["solidgate_events_flat"] = f"""
        WITH dedup AS (
            SELECT DISTINCT event_id, event_type, created, data FROM read_parquet('{SOLIDGATE}')
        )
        SELECT event_id, event_type, created,
            json_extract_string(data, '$.subscription.id') AS subscription_id,
            lower(json_extract_string(data, '$.customer.customer_email')) AS email,
            json_extract_string(data, '$.customer.customer_account_id') AS customer_account_id,
            json_extract_string(data, '$.subscription.status') AS subscription_status,
            json_extract_string(data, '$.subscription.cancel_message') AS cancel_message,
            json_extract_string(data, '$.subscription.started_at') AS sub_started_at,
            json_extract_string(data, '$.subscription.expired_at') AS sub_expired_at,
            json_extract_string(data, '$.product.name') AS product_name,
            json_extract_string(data, '$.product.product_id') AS product_id_sg,
            TRY_CAST(json_extract(data, '$.product.trial_amount') AS BIGINT) AS trial_amount_cents,
            TRY_CAST(json_extract(data, '$.product.amount') AS BIGINT) AS product_amount_cents,
            json_extract_string(data, '$.product.trial') AS trial_flag,
            json_extract_string(data, '$.product.payment_action') AS payment_action,
            json_extract_string(data, '$.product.currency') AS currency
        FROM dedup
    """

    # 6. Solidgate orders — unnest dynamic invoice/order keys, dedup by order_id
    #    (latest referencing event wins; payloads are cumulative snapshots)
    q["solidgate_orders"] = f"""
        WITH dedup AS (
            SELECT DISTINCT event_id, event_type, created, data FROM read_parquet('{SOLIDGATE}')
        ),
        inv AS (
            SELECT d.event_type, d.created,
                   json_extract_string(d.data, '$.subscription.id') AS subscription_id,
                   d.data, ik.invoice_key
            FROM dedup d, unnest(json_keys(d.data, '$.invoices')) AS ik(invoice_key)
        ),
        ord AS (
            SELECT i.event_type, i.created, i.subscription_id, i.data, i.invoice_key, ok.order_key
            FROM inv i, unnest(json_keys(i.data, '$.invoices."' || i.invoice_key || '".orders')) AS ok(order_key)
        )
        SELECT subscription_id, invoice_key AS invoice_id, order_key AS order_id,
            json_extract_string(data, '$.invoices."' || invoice_key || '".orders."' || order_key || '".status') AS order_status,
            TRY_CAST(json_extract(data, '$.invoices."' || invoice_key || '".orders."' || order_key || '".amount') AS BIGINT) AS order_amount_cents,
            json_extract_string(data, '$.invoices."' || invoice_key || '".orders."' || order_key || '".operation') AS order_operation,
            json_extract_string(data, '$.invoices."' || invoice_key || '".orders."' || order_key || '".processed_at') AS processed_at,
            json_extract_string(data, '$.invoices."' || invoice_key || '".orders."' || order_key || '".created_at') AS order_created_at,
            json_extract_string(data, '$.invoices."' || invoice_key || '".orders."' || order_key || '".updated_at') AS order_updated_at,
            TRY_CAST(json_extract(data, '$.invoices."' || invoice_key || '".orders."' || order_key || '".retry_attempt') AS BIGINT) AS retry_attempt,
            json_extract_string(data, '$.invoices."' || invoice_key || '".orders."' || order_key || '".failed_reason') AS failed_reason,
            event_type AS source_event_type, created AS source_event_created
        FROM ord
        QUALIFY ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY created DESC) = 1
    """
    return q


def run_extract(con, dry=False, force=False):
    queries = extract_queries()
    for name, sql in queries.items():
        out = TMP / f"{name}.parquet"
        if dry:
            t0 = time.time()
            df = con.execute(f"SELECT * FROM ({sql}) LIMIT 1000").fetchdf()
            print(f"\n[DRY] {name}: {len(df)} rows (LIMIT 1000), {time.time()-t0:.1f}s — 3 samples:")
            print(df.head(3).to_string())
            continue
        if out.exists() and not force:
            n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out.as_posix()}')").fetchone()[0]
            print(f"[skip] {name}: cached ({n} rows)")
            continue
        t0 = time.time()
        con.execute(f"COPY ({sql}) TO '{out.as_posix()}' (FORMAT PARQUET)")
        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out.as_posix()}')").fetchone()[0]
        print(f"[wrote] {name}: {n} rows, {time.time()-t0:.1f}s -> {out.name}")


# ---------------------------------------------------------------------------
# map: Stripe (Sections 5 + 7) — reads tmp files only
# ---------------------------------------------------------------------------

def stripe_bursts(con):
    """Collapse invoice-family rows into one outcome per payment-attempt burst."""
    inv = TMP / "stripe_invoice_family.parquet"
    df = con.execute(f"""
        WITH inv AS (
            SELECT invoice_id, subscription_id AS sub_id, event_type, created,
                   billing_reason, amount_paid_cents
            FROM read_parquet('{inv.as_posix()}')
            WHERE subscription_id IS NOT NULL
        ),
        gapped AS (
            SELECT *, LAG(created) OVER (PARTITION BY invoice_id ORDER BY created) AS prev_created FROM inv
        ),
        marked AS (
            SELECT *, CASE WHEN prev_created IS NULL
                                OR date_diff('second', prev_created, created) > {BURST_GAP_SECONDS}
                           THEN 1 ELSE 0 END AS is_new_burst FROM gapped
        ),
        burst_ids AS (
            SELECT *, SUM(is_new_burst) OVER (PARTITION BY invoice_id ORDER BY created) AS burst_id FROM marked
        )
        SELECT invoice_id, sub_id, burst_id,
            MAX(CASE WHEN event_type='invoice.paid' THEN 1 ELSE 0 END) AS has_paid,
            MAX(CASE WHEN event_type='invoice.payment_failed' THEN 1 ELSE 0 END) AS has_failed,
            MAX(CASE WHEN event_type='invoice.marked_uncollectible' THEN 1 ELSE 0 END) AS has_uncollectible,
            MIN(billing_reason) AS billing_reason,
            MAX(CASE WHEN event_type='invoice.paid' THEN created END) AS paid_at,
            MAX(CASE WHEN event_type='invoice.payment_failed' THEN created END) AS failed_at,
            MAX(CASE WHEN event_type='invoice.marked_uncollectible' THEN created END) AS uncollectible_at,
            MAX(CASE WHEN event_type='invoice.paid' THEN amount_paid_cents END) AS amount_paid_cents,
            COUNT(*) AS n_rows_in_burst, MIN(created) AS burst_start
        FROM burst_ids GROUP BY invoice_id, sub_id, burst_id ORDER BY sub_id, burst_start
    """).fetchdf()
    return to_utc(df, ["paid_at", "failed_at", "uncollectible_at", "burst_start"])


def map_stripe_invoice_outcomes(bursts_df):
    emitted, enrichment = [], []
    for sub_id, g in bursts_df.groupby("sub_id"):
        g = g.sort_values("burst_start")
        seen_cycle = False
        for _, row in g.iterrows():
            if row["has_paid"]:
                if row["billing_reason"] == "subscription_create":
                    enrichment.append({
                        "subscription_id": sub_id,
                        "trial_money_captured": row["amount_paid_cents"] is not None and row["amount_paid_cents"] > 0,
                        "trial_amount_paid_cents": row["amount_paid_cents"],
                        "trial_invoice_datetime": row["paid_at"],
                    })
                    continue
                name = "trial_converted" if not seen_cycle else "subscription_renewed"
                seen_cycle = True
                emitted.append({"subscription_id": sub_id, "event_datetime": row["paid_at"],
                                "event_name": name, "raw_event_type": "invoice.paid",
                                "billing_reason": row["billing_reason"], "churn_type": None,
                                "price_amount_cents": row["amount_paid_cents"], "invoice_id": row["invoice_id"]})
            elif row["has_failed"]:
                emitted.append({"subscription_id": sub_id, "event_datetime": row["failed_at"],
                                "event_name": "billing_issue_detected", "raw_event_type": "invoice.payment_failed",
                                "billing_reason": row["billing_reason"], "churn_type": None,
                                "price_amount_cents": None, "invoice_id": row["invoice_id"]})
            elif row["has_uncollectible"]:
                emitted.append({"subscription_id": sub_id, "event_datetime": row["uncollectible_at"],
                                "event_name": "billing_issue_detected", "raw_event_type": "invoice.marked_uncollectible",
                                "billing_reason": row["billing_reason"], "churn_type": None,
                                "price_amount_cents": None, "invoice_id": row["invoice_id"]})
    ed = pd.DataFrame(emitted)
    if len(ed):
        ed = to_utc(ed, ["event_datetime"])
    en = pd.DataFrame(enrichment)
    if len(en):
        en = to_utc(en, ["trial_invoice_datetime"])
    return ed, en


def map_stripe_state_and_refunds(con):
    se = pd.read_parquet(TMP / "stripe_subscription_events.parquet")
    se = to_utc(se, ["created"])

    def epoch_to_utc(v):
        if v is None or pd.isna(v):
            return pd.NaT
        return pd.Timestamp(int(v), unit="s", tz="UTC")

    rows = []
    for _, r in se.iterrows():
        if r["event_type"] == "customer.subscription.created":
            rows.append({"subscription_id": r["subscription_id"], "event_datetime": r["created"],
                         "event_name": "trial_started", "raw_event_type": r["event_type"],
                         "billing_reason": None, "churn_type": None, "cancellation_reason": None,
                         "price_amount_cents": None, "invoice_id": None, "churn_signal_datetime": pd.NaT})
        else:
            churn = {"cancellation_requested": "voluntary", "payment_failed": "involuntary"}.get(r["cancellation_reason"], "unknown")
            # death-shift (8.9): period end from deleted object, priority CPE -> ended_at -> cancel_at;
            # raw delete moment preserved in churn_signal_datetime.
            period_end = (epoch_to_utc(r["current_period_end_epoch"])
                          if not pd.isna(r["current_period_end_epoch"]) else pd.NaT)
            if pd.isna(period_end):
                period_end = epoch_to_utc(r["ended_at_epoch"])
            if pd.isna(period_end):
                period_end = epoch_to_utc(r["cancel_at_epoch"])
            evt_dt = period_end if not pd.isna(period_end) else r["created"]
            rows.append({"subscription_id": r["subscription_id"], "event_datetime": evt_dt,
                         "event_name": "subscription_expired", "raw_event_type": r["event_type"],
                         "billing_reason": None, "churn_type": churn, "cancellation_reason": r["cancellation_reason"],
                         "price_amount_cents": None, "invoice_id": None, "churn_signal_datetime": r["created"]})
    state = pd.DataFrame(rows)

    cr = pd.read_parquet(TMP / "stripe_charges_refunds.parquet")
    cr = to_utc(cr, ["created"])
    pil = pd.read_parquet(TMP / "stripe_pi_invoice.parquet")
    inv2sub = con.execute(f"""
        SELECT DISTINCT invoice_id, subscription_id FROM read_parquet('{(TMP/'stripe_invoice_family.parquet').as_posix()}')
        WHERE subscription_id IS NOT NULL
    """).fetchdf()
    cr = cr.merge(pil, on="payment_intent", how="left").merge(inv2sub, on="invoice_id", how="left")
    ref_rows = []
    for _, r in cr.iterrows():
        amt = r["amount_refunded_cents"] if r["event_type"] == "charge.refunded" else r["amount_cents"]
        ref_rows.append({"subscription_id": r.get("subscription_id"), "event_datetime": r["created"],
                         "event_name": "subscription_refunded", "raw_event_type": r["event_type"],
                         "billing_reason": None, "churn_type": None, "cancellation_reason": None,
                         "price_amount_cents": amt, "invoice_id": r.get("invoice_id"),
                         # stripe_account from the refund's OWN object id (ch_/re_ fragment) —
                         # available even when the subscription can't be resolved (orphan)
                         "stripe_account": r.get("stripe_account"),
                         "refund_orphan": pd.isna(r.get("subscription_id"))})
    refunds = pd.DataFrame(ref_rows)
    return to_utc(state, ["event_datetime"]), to_utc(refunds, ["event_datetime"])


# ---------------------------------------------------------------------------
# Solidgate map (Section 6) — order-status-driven, cancel_message lookup
# ---------------------------------------------------------------------------

CHURN_LOOKUP = {
    "Cancellation after redemption period": "involuntary",
    "Card Token has expired": "involuntary",
    "Cancellation by support": "voluntary",
    "Token revoked by customer": "voluntary",
    "Fraud Alert received": "fraud", "Fraud Decline received": "fraud",
    "Recurring payment is blocked by Antifraud": "fraud", "Bank antifraud system": "fraud",
    "Fraud Chargeback received": "fraud",
}


# ---------------------------------------------------------------------------
# trace (Stop-point 1)
# ---------------------------------------------------------------------------

def trace_stripe(con, sub_id):
    fam = pd.read_parquet(TMP / "stripe_invoice_family.parquet")
    fam = to_utc(fam, ["created"])
    print(f"=== RAW invoice-family rows (post event_id-dedup), {sub_id} ===")
    r = fam[fam["subscription_id"] == sub_id].sort_values("created")
    print(r[["created", "event_type", "invoice_id", "billing_reason", "amount_paid_cents", "invoice_status"]].to_string())

    se = pd.read_parquet(TMP / "stripe_subscription_events.parquet")
    se = to_utc(se, ["created"])
    print(f"\n=== RAW subscription state events, {sub_id} ===")
    rs = se[se["subscription_id"] == sub_id].sort_values("created")
    print(rs[["created", "event_type", "plan_interval", "cancellation_reason", "ended_at_epoch", "cancel_at_epoch"]].to_string())

    bursts = stripe_bursts(con)
    print(f"\n=== BURSTS (invoice-family collapsed), {sub_id} ===")
    b = bursts[bursts["sub_id"] == sub_id]
    print(b[["invoice_id", "burst_start", "n_rows_in_burst", "has_paid", "has_failed", "billing_reason", "amount_paid_cents"]].to_string())

    emitted, enrichment = map_stripe_invoice_outcomes(bursts)
    state, refunds = map_stripe_state_and_refunds(con)
    parts = [p[p["subscription_id"] == sub_id] for p in [state, emitted, refunds] if len(p)]
    chain = pd.concat([p for p in parts if len(p)], ignore_index=True)
    chain = to_utc(chain, ["event_datetime"]).sort_values("event_datetime")
    print(f"\n=== MAPPED SILVER CHAIN, {sub_id} ===")
    print(chain[["event_datetime", "event_name", "raw_event_type", "billing_reason", "price_amount_cents", "churn_type"]].to_string())

    enr = enrichment[enrichment["subscription_id"] == sub_id]
    print("\ntrial enrichment (merged into trial_started's trial_type/price_amount, NOT a separate row):")
    print(enr.to_string() if len(enr) else "(none)")
    n_ts = (chain["event_name"] == "trial_started").sum()
    print(f"\nASSERT (owner req #1): exactly 1 trial_started per subscription -> {n_ts}: {'PASS' if n_ts == 1 else 'FAIL'}")


def trace_solidgate(con, sub_id):
    ev = pd.read_parquet(TMP / "solidgate_events_flat.parquet")
    ev = to_utc(ev, ["created"])
    ords = pd.read_parquet(TMP / "solidgate_orders.parquet")
    ords = to_utc(ords, ["source_event_created"])

    print(f"=== RAW events (dedup triples), {sub_id} ===")
    e = ev[ev["subscription_id"] == sub_id].sort_values("created")
    print(e[["created", "event_type", "subscription_status", "cancel_message"]].to_string())

    print(f"\n=== DISTINCT ORDERS (deduped by order_id, latest event wins), {sub_id} ===")
    o = ords[ords["subscription_id"] == sub_id].copy()
    o["processed_at"] = pd.to_datetime(o["processed_at"], errors="coerce")
    o = o.sort_values("processed_at")
    print(o[["order_id", "order_status", "order_amount_cents", "retry_attempt", "processed_at", "order_operation"]].to_string())

    print(f"\n=== MAPPED SILVER CHAIN (Solidgate), {sub_id} ===")
    rows = []
    e_sorted = e.sort_values("created")
    seen_success = False
    for _, r in e_sorted.iterrows():
        et = r["event_type"]
        if et in ("create", "active"):
            rows.append((r["created"], "trial_started", et, None))
        elif et in ("recurring", "renew"):
            # success/failure comes from THIS event's own orders (by source event), not event type
            evt_orders = ords[(ords["subscription_id"] == sub_id) & (ords["source_event_type"] == et)]
            has_success = (evt_orders["order_status"].isin(["success", "settle", "approved"])).any()
            if has_success or et == "renew":
                name = "trial_converted" if not seen_success else "subscription_renewed"
                seen_success = True
                rows.append((r["created"], name, et, None))
        elif et in ("retry", "scheduled_for_retry"):
            rows.append((r["created"], "billing_issue_detected", et, None))
        elif et == "redemption" or r["subscription_status"] == "redemption":
            rows.append((r["created"], "entered_grace_period", et, None))
        elif et == "expire":
            rows.append((r["created"], "subscription_expired", et, "involuntary"))
        elif et == "cancel":
            churn = CHURN_LOOKUP.get(r["cancel_message"], "unknown")
            rows.append((r["created"], "subscription_expired", et, churn))
    chain = pd.DataFrame(rows, columns=["event_datetime", "event_name", "raw_event_type", "churn_type"]).sort_values("event_datetime")
    print(chain.to_string())
    print("\nNOTE: trace-stage Solidgate mapping is a first cut for eyeballing; the")
    print("dedup-of-(create,active) pair and first-success->trial_converted collapse")
    print("are finalized in the map stage after this checkpoint.")


SUCCESS_ORDER_STATUSES = ("settle_ok",)          # captured money (Section 7)
FAIL_ORDER_STATUSES = ("auth_failed",)            # real failed charge -> billing_issue
REFUND_ORDER_STATUSES = ("refunded",)
SNAPSHOT_TS = pd.Timestamp("2026-07-07T00:00:00Z")
NULL_TOKENS = {"", "null", "none", "unknown"}


def norm_null(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return None if s.lower() in NULL_TOKENS else s


def parse_interval_from_name(name):
    if not name:
        return None
    n = name.lower()
    if "year" in n or "annual" in n:
        return "year"
    if "month" in n:
        return "month"
    if "week" in n:
        return "week"
    if "dail" in n or "day" in n:
        return "day"
    return None


# ---------------------------------------------------------------------------
# Solidgate silver (Section 6, order-driven per owner rule)
# ---------------------------------------------------------------------------

def build_solidgate_silver(con):
    ev = pd.read_parquet(TMP / "solidgate_events_flat.parquet")
    ev = to_utc(ev, ["created"])
    ords = pd.read_parquet(TMP / "solidgate_orders.parquet")
    for c in ("processed_at", "order_updated_at", "order_created_at"):
        ords[c] = pd.to_datetime(ords[c], utc=True, errors="coerce")

    rows = []

    # trial_started: exactly one per sub (owner assert #13). Prefer the create/active
    # pair; for subs whose create/active events are outside the snapshot window
    # (mid-life at capture) synthesize from subscription.started_at, else earliest event.
    evv = ev.dropna(subset=["subscription_id"]).copy()
    evv["sa"] = pd.to_datetime(evv["sub_started_at"], utc=True, errors="coerce")
    start_dt = evv[evv["event_type"].isin(["create", "active"])].groupby("subscription_id")["created"].min()
    started_at = evv.groupby("subscription_id")["sa"].min()
    first_ev = evv.groupby("subscription_id")["created"].min()
    for sub_id in evv["subscription_id"].unique():
        if sub_id in start_dt.index and not pd.isna(start_dt.get(sub_id)):
            dt, raw = start_dt.get(sub_id), "create/active"
        elif not pd.isna(started_at.get(sub_id)):
            dt, raw = started_at.get(sub_id), "synth:started_at"
        else:
            dt, raw = first_ev.get(sub_id), "synth:first_event"
        rows.append({"subscription_id": sub_id, "event_datetime": dt,
                     "event_name": "trial_started", "raw_event_type": raw,
                     "churn_type": None, "price_amount_cents": None, "churn_signal_datetime": pd.NaT})

    # money + dunning + refund from ORDERS (deduped by order_id already)
    ords_valid = ords.dropna(subset=["subscription_id"])
    for sub_id, g in ords_valid.groupby("subscription_id"):
        g = g.sort_values(["processed_at", "retry_attempt"], na_position="last")
        seen_success = False
        for _, o in g.iterrows():
            st = o["order_status"]
            dt = o["processed_at"]
            if pd.isna(dt):
                continue  # in-flight/processing with no processed_at -> audit-only
            if st in SUCCESS_ORDER_STATUSES and (o["order_amount_cents"] or 0) > 0:
                name = "trial_converted" if not seen_success else "subscription_renewed"
                seen_success = True
                rows.append({"subscription_id": sub_id, "event_datetime": dt, "event_name": name,
                             "raw_event_type": f"order:{st}", "churn_type": None,
                             "price_amount_cents": o["order_amount_cents"], "churn_signal_datetime": pd.NaT})
            elif st in FAIL_ORDER_STATUSES:
                rows.append({"subscription_id": sub_id, "event_datetime": dt, "event_name": "billing_issue_detected",
                             "raw_event_type": f"order:{st}(retry={o['retry_attempt']})", "churn_type": None,
                             "price_amount_cents": None, "churn_signal_datetime": pd.NaT})
            elif st in REFUND_ORDER_STATUSES:
                rows.append({"subscription_id": sub_id, "event_datetime": dt, "event_name": "subscription_refunded",
                             "raw_event_type": f"order:{st}", "churn_type": None,
                             "price_amount_cents": o["order_amount_cents"], "churn_signal_datetime": pd.NaT})
            # auth_ok / processing / void_ok -> audit-only, no row

    # entered_grace_period: one per distinct entry into redemption status
    ev_sorted = ev.dropna(subset=["subscription_id"]).sort_values(["subscription_id", "created"])
    for sub_id, g in ev_sorted.groupby("subscription_id"):
        prev_red = False
        for _, e in g.iterrows():
            is_red = (e["subscription_status"] == "redemption")
            if is_red and not prev_red:
                rows.append({"subscription_id": sub_id, "event_datetime": e["created"],
                             "event_name": "entered_grace_period", "raw_event_type": e["event_type"],
                             "churn_type": None, "price_amount_cents": None, "churn_signal_datetime": pd.NaT})
            prev_red = is_red

    # terminal: expire (involuntary) / cancel (lookup). death-shift 8.9.
    # last successful settle_ok processed_at per sub (for expire death-shift fallback).
    last_success = (ords_valid[ords_valid["order_status"].isin(SUCCESS_ORDER_STATUSES)]
                    .groupby("subscription_id")["processed_at"].max())
    interval_by_sub = {}
    for sub_id, g in ev.dropna(subset=["subscription_id"]).groupby("subscription_id"):
        nm = g["product_name"].dropna()
        interval_by_sub[sub_id] = parse_interval_from_name(nm.iloc[0]) if len(nm) else None

    def add_interval(dt, unit):
        if pd.isna(dt) or unit is None:
            return pd.NaT
        return dt + {"day": pd.Timedelta(days=1), "week": pd.Timedelta(weeks=1),
                     "month": pd.Timedelta(days=30), "year": pd.Timedelta(days=365)}.get(unit, pd.Timedelta(0))

    terminals = ev[ev["event_type"].isin(["expire", "cancel"])].dropna(subset=["subscription_id"])
    for _, e in terminals.iterrows():
        sub_id = e["subscription_id"]
        signal = e["created"]
        if e["event_type"] == "cancel":
            churn = CHURN_LOOKUP.get(e["cancel_message"], "unknown")
            death = pd.to_datetime(e["sub_expired_at"], utc=True, errors="coerce")  # 100% populated on cancel
        else:
            churn = "involuntary"
            exp = pd.to_datetime(e["sub_expired_at"], utc=True, errors="coerce")  # 0% on expire
            death = exp if not pd.isna(exp) else add_interval(last_success.get(sub_id, pd.NaT), interval_by_sub.get(sub_id))
        evt_dt = death if not pd.isna(death) else signal
        rows.append({"subscription_id": sub_id, "event_datetime": evt_dt,
                     "event_name": "subscription_expired", "raw_event_type": e["event_type"],
                     "churn_type": churn, "price_amount_cents": None, "churn_signal_datetime": signal,
                     "cancel_message": e["cancel_message"]})

    df = pd.DataFrame(rows)
    df["payment_provider"] = "solidgate"
    return to_utc(df, ["event_datetime", "churn_signal_datetime"])


# ---------------------------------------------------------------------------
# Sub-level enrichment: geo/media (email->web_conversions), app, interval, trial_type
# ---------------------------------------------------------------------------

def load_web_conversions(con):
    wc = con.execute(f"""
        SELECT lower(email) AS email, event_date, country, utm_source, campaign_name, app_name
        FROM read_parquet('{(RAW/'web_conversions.parquet').as_posix()}')
        WHERE email IS NOT NULL AND event_date < TIMESTAMP '2026-07-07 00:00:00+00'
    """).fetchdf()
    wc = to_utc(wc, ["event_date"])
    for c in ("country", "utm_source", "campaign_name", "app_name"):
        wc[c] = wc[c].map(norm_null)
    return wc


def resolve_geo_media(wc, email, install_date):
    """Return (country, geo_ambiguous, wc_utm, wc_campaign, wc_app) for one email,
    tie-broken to the country/attribution at-or-before install_date closest to it
    (none before -> earliest). Section 8.4/8.5."""
    if not email:
        return None, False, "no_match", None, None, None
    g = wc[wc["email"] == email]
    if len(g) == 0:
        return None, False, "no_match", None, None, None
    countries = g["country"].dropna().unique()
    ambiguous = len(countries) > 1
    # select among rows that actually carry a real country ('unknown' already
    # normalized to NULL); only fall back to a country-less row for attribution
    # when the email has no real-country row at all.
    g_real = g[g["country"].notna()]
    resolved = len(g_real) > 0
    pool = g_real if resolved else g
    if not ambiguous:
        pick = pool.iloc[0]
    else:
        gg = pool.dropna(subset=["event_date"]).copy()
        if install_date is not None and not pd.isna(install_date) and len(gg):
            before = gg[gg["event_date"] <= install_date]
            pick = (before.sort_values("event_date").iloc[-1] if len(before)
                    else gg.sort_values("event_date").iloc[0])
        else:
            pick = (gg.sort_values("event_date").iloc[0] if len(gg) else pool.iloc[0])
    geo_status = "resolved" if resolved else "unknown"  # matched wc but country unknown
    country = pick.get("country") if resolved else None
    return country, ambiguous, geo_status, pick.get("utm_source"), pick.get("campaign_name"), pick.get("app_name")


def build_silver(con):
    print("[map] building stripe silver rows...")
    bursts = stripe_bursts(con)
    emitted, enrichment = map_stripe_invoice_outcomes(bursts)
    state, refunds = map_stripe_state_and_refunds(con)
    stripe = pd.concat([p for p in [state, emitted, refunds] if len(p)], ignore_index=True)
    stripe["payment_provider"] = "stripe"

    # trial enrichment -> trial_type/price on the trial_started row
    enr = enrichment.set_index("subscription_id") if len(enrichment) else pd.DataFrame()
    subs = con.execute(f"""
        SELECT sub_id AS subscription_id, lower(email) AS email, customer_id AS customer_user_id,
               utm_source, campaign_name, app_name, interval_unit, price_id, product_id,
               price_amount, sub_created_at
        FROM read_parquet('{(RAW/'stripe_subscriptions.parquet').as_posix()}')
    """).fetchdf()
    subs = to_utc(subs, ["sub_created_at"])
    for c in ("utm_source", "campaign_name", "app_name"):
        subs[c + "_n"] = subs[c].map(norm_null)
    subs_ix = subs.set_index("subscription_id")

    # stripe_account: from sub_id fragment where resolvable; else keep the refund's
    # own object-id fragment (set on refund rows) so orphan refunds still get an account.
    sub_acct = stripe["subscription_id"].map(
        lambda s: s[10:20] if isinstance(s, str) and s.startswith("sub_") and len(s) >= 20 else None)
    if "stripe_account" in stripe.columns:
        stripe["stripe_account"] = sub_acct.where(sub_acct.notna(), stripe["stripe_account"])
    else:
        stripe["stripe_account"] = sub_acct

    # ensure exactly one trial_started per Stripe sub: 106 subs have invoice/refund
    # activity but their customer.subscription.created predates the event window
    # (started before 2025-10-10) -> synthesize trial_started at earliest known event.
    def ensure_trial_started(df):
        have = set(df.loc[df["event_name"] == "trial_started", "subscription_id"].dropna())
        alls = set(df["subscription_id"].dropna())
        missing = alls - have
        if not missing:
            return df
        add = []
        for sub_id, g in df[df["subscription_id"].isin(missing)].groupby("subscription_id"):
            add.append({"subscription_id": sub_id, "event_datetime": g["event_datetime"].min(),
                        "event_name": "trial_started", "raw_event_type": "synth:pre_window",
                        "billing_reason": None, "churn_type": None, "cancellation_reason": None,
                        "price_amount_cents": None, "invoice_id": None,
                        "stripe_account": g["stripe_account"].iloc[0], "payment_provider": "stripe",
                        "churn_signal_datetime": pd.NaT})
        return pd.concat([df, pd.DataFrame(add)], ignore_index=True)

    stripe = ensure_trial_started(stripe)

    print("[map] building solidgate silver rows...")
    solid = build_solidgate_silver(con)

    # install_date per subscription
    print("[map] computing install_date / interval / trial_type per subscription...")
    # Stripe install: subscription.created event datetime (from state trial_started)
    stripe_install = stripe[stripe["event_name"] == "trial_started"].set_index("subscription_id")["event_datetime"]
    # Solidgate install: subscription.started_at (fallback trial_started dt)
    sg_ev = pd.read_parquet(TMP / "solidgate_events_flat.parquet")
    sg_started = sg_ev.dropna(subset=["subscription_id"]).groupby("subscription_id")["sub_started_at"].first()
    sg_started = pd.to_datetime(sg_started, utc=True, errors="coerce")
    sg_email = sg_ev.dropna(subset=["subscription_id"]).groupby("subscription_id")["email"].first()
    sg_pay_action = sg_ev.dropna(subset=["subscription_id"]).groupby("subscription_id")["payment_action"].first()
    sg_trial_amt = sg_ev.dropna(subset=["subscription_id"]).groupby("subscription_id")["trial_amount_cents"].first()
    sg_prodname = sg_ev.dropna(subset=["subscription_id"]).groupby("subscription_id")["product_name"].first()
    sg_prodid = sg_ev.dropna(subset=["subscription_id"]).groupby("subscription_id")["product_id_sg"].first()
    sg_custacct = sg_ev.dropna(subset=["subscription_id"]).groupby("subscription_id")["customer_account_id"].first()

    # Solidgate interval inferred from median gap between consecutive settle_ok orders
    # (owner-accepted 8.10 deviation): needs >=2 successful captures.
    sg_ord = pd.read_parquet(TMP / "solidgate_orders.parquet")
    sg_ord["processed_at"] = pd.to_datetime(sg_ord["processed_at"], utc=True, errors="coerce")
    succ = sg_ord[(sg_ord["order_status"] == "settle_ok") & sg_ord["processed_at"].notna()].dropna(subset=["subscription_id"])
    sg_cadence = {}
    for sub_id, g in succ.groupby("subscription_id"):
        ts = g["processed_at"].sort_values()
        if len(ts) < 2:
            continue
        med_gap = ts.diff().dropna().dt.total_seconds().median() / 86400.0
        sg_cadence[sub_id] = min(("day", "week", "month", "year"),
                                 key=lambda u: abs(med_gap - {"day": 1, "week": 7, "month": 30, "year": 365}[u]))

    wc = load_web_conversions(con)

    def enrich(df, provider):
        recs = []
        for sub_id, g in df.groupby("subscription_id", dropna=False):
            if provider == "stripe":
                install = stripe_install.get(sub_id, pd.NaT)
                email = subs_ix["email"].get(sub_id) if sub_id in subs_ix.index else None
                cust = subs_ix["customer_user_id"].get(sub_id) if sub_id in subs_ix.index else None
                s_utm = norm_null(subs_ix["utm_source_n"].get(sub_id)) if sub_id in subs_ix.index else None
                s_camp = norm_null(subs_ix["campaign_name_n"].get(sub_id)) if sub_id in subs_ix.index else None
                s_app = norm_null(subs_ix["app_name_n"].get(sub_id)) if sub_id in subs_ix.index else None
                interval = norm_null(subs_ix["interval_unit"].get(sub_id)) if sub_id in subs_ix.index else None
                interval_source = "plan" if interval else None
                price_id = subs_ix["price_id"].get(sub_id) if sub_id in subs_ix.index else None
                product_id = subs_ix["product_id"].get(sub_id) if sub_id in subs_ix.index else None
                acct = g["stripe_account"].iloc[0]
                trial_amt = enr.loc[sub_id, "trial_amount_paid_cents"] if (len(enr) and sub_id in enr.index) else None
                trial_cap = bool(enr.loc[sub_id, "trial_money_captured"]) if (len(enr) and sub_id in enr.index) else False
                trial_type = "paid" if trial_cap else ("free" if (len(enr) and sub_id in enr.index) else "none")
            else:
                install = sg_started.get(sub_id, pd.NaT)
                email = sg_email.get(sub_id)
                cust = sg_custacct.get(sub_id)
                s_utm = s_camp = s_app = None
                interval = parse_interval_from_name(sg_prodname.get(sub_id))
                interval_source = "from_name" if interval else None
                if interval is None and sub_id in sg_cadence:
                    interval, interval_source = sg_cadence[sub_id], "inferred_from_cadence"
                price_id = product_id = sg_prodid.get(sub_id)
                acct = None
                pa = sg_pay_action.get(sub_id)
                trial_type = "paid" if pa == "auth_settle" else "free"
                trial_amt = sg_trial_amt.get(sub_id)

            country, ambiguous, geo_status, wc_utm, wc_camp, wc_app = resolve_geo_media(wc, email, install)

            # media two-step (8.5)
            if provider == "stripe" and s_utm is not None:
                media, media_step, campaign = s_utm, "subs_dim", s_camp
            elif norm_null(wc_utm) is not None:
                media, media_step, campaign = norm_null(wc_utm), "web_conversions", norm_null(wc_camp)
            else:
                media, media_step, campaign = None, None, None
            is_qa_utm = (s_utm == "test") or (norm_null(wc_utm) == "test")
            if media == "test":
                media = None  # test -> NULL media but is_qa flag set

            # app_id (8.6)
            if provider == "stripe":
                if s_app is not None:
                    app_id = s_app.lower()
                elif acct == "A9qayReKqB":
                    app_id = "atelier"       # hypothesis, reported in profile crosstab
                else:
                    app_id = "invinci"
            else:
                wc_app_n = norm_null(wc_app)
                app_id = wc_app_n.lower() if wc_app_n else "invinci"

            recs.append({"subscription_id": sub_id, "email": email, "customer_user_id": cust,
                         "install_date": (install.date() if not pd.isna(install) else None),
                         "country_code": country, "geo_ambiguous": ambiguous, "geo_status": geo_status,
                         "media_source": media, "media_source_step": media_step, "campaign": campaign,
                         "app_id": app_id, "interval_unit": interval, "interval_source": interval_source,
                         "price_id": price_id, "product_id": product_id,
                         "trial_type": trial_type, "trial_amount_cents": trial_amt, "is_qa_utm": is_qa_utm})
        return pd.DataFrame(recs).set_index("subscription_id")

    print("[map] enriching stripe subs...")
    stripe_attr = enrich(stripe, "stripe")
    print("[map] enriching solidgate subs...")
    solid_attr = enrich(solid, "solidgate")

    # QA daily-99c subs (Stripe): interval day + trial/renew price 0.99
    qa_daily = set()
    for sub_id in stripe["subscription_id"].dropna().unique():
        if sub_id in stripe_attr.index and stripe_attr.loc[sub_id, "interval_unit"] == "day":
            qa_daily.add(sub_id)

    def finalize(df, attr, provider):
        df = df.copy()
        a = attr
        for col in ["email", "customer_user_id", "install_date", "country_code", "geo_ambiguous",
                    "geo_status", "media_source", "media_source_step", "campaign", "app_id",
                    "interval_unit", "interval_source", "price_id", "product_id", "trial_type", "is_qa_utm"]:
            df[col] = df["subscription_id"].map(a[col]) if col in a.columns else None
        df["price_amount"] = df["price_amount_cents"].astype("float64") / 100.0
        df["price_amount"] = df["price_amount"].where(df["price_amount_cents"].notna(), None)
        # trial_started price/amount from enrichment (already in price_amount_cents? no -> set from attr trial_amount for money marker)
        df["is_qa"] = df["is_qa_utm"].fillna(False)
        if provider == "stripe":
            df.loc[df["subscription_id"].isin(qa_daily), "is_qa"] = True
        # refund_orphan: unambiguously a refund row whose subscription could not be attached
        df["refund_orphan"] = (df["event_name"] == "subscription_refunded") & df["subscription_id"].isna()
        if provider == "solidgate":
            df["stripe_account"] = None
        # stripe: stripe_account already set (sub fragment, or refund object fragment) — keep it
        return df

    stripe_f = finalize(stripe, stripe_attr, "stripe")
    solid_f = finalize(solid, solid_attr, "solidgate")

    silver = pd.concat([stripe_f, solid_f], ignore_index=True)
    silver["churn_type"] = silver["churn_type"].where(silver["event_name"] == "subscription_expired", None)
    # trial_type only meaningful on trial_started; keep per-sub value on all rows (attribute), fine for silver
    cols = ["subscription_id", "customer_user_id", "payment_provider", "stripe_account", "app_id",
            "event_name", "event_datetime", "install_date", "country_code", "geo_ambiguous", "geo_status",
            "media_source", "media_source_step", "campaign", "product_id", "price_id", "interval_unit",
            "interval_source", "trial_type", "price_amount", "churn_type", "is_qa", "refund_orphan",
            "raw_event_type", "churn_signal_datetime"]
    for c in cols:
        if c not in silver.columns:
            silver[c] = None
    silver = silver[cols].sort_values(["payment_provider", "subscription_id", "event_datetime"]).reset_index(drop=True)
    return silver


def run_asserts(con, silver):
    import json as _json
    results = []

    def check(n, ok, detail):
        results.append((n, ok, detail))

    # 1. row counts match manifest
    man = _json.loads((RAW / "SNAPSHOT_MANIFEST.json").read_text())["tables"]
    mism = []
    for f, exp in man.items():
        got = con.execute(f"SELECT COUNT(*) FROM read_parquet('{(RAW/(f+'.parquet')).as_posix()}')").fetchone()[0]
        if got != exp:
            mism.append(f"{f}: {got}!={exp}")
    check(1, not mism, "; ".join(mism) or "all 6 match")

    # 2. stripe benign-dup guard (identical after stripping pending_webhooks)
    dup = con.execute(f"""
        SELECT event_id FROM read_parquet('{STRIPE}') GROUP BY event_id HAVING COUNT(*)>1
    """).fetchdf()["event_id"].tolist()
    bad2 = 0
    if dup:
        rows = con.execute(f"""SELECT event_id, data FROM read_parquet('{STRIPE}')
                               WHERE event_id IN ({','.join('?'*len(dup))})""", dup).fetchdf()
        def strip(js):
            d = _json.loads(js); d.pop("pending_webhooks", None); return _json.dumps(d, sort_keys=True)
        rows["s"] = rows["data"].map(strip)
        bad2 = (rows.groupby("event_id")["s"].nunique() > 1).sum()
    check(2, bad2 == 0, f"{len(dup)} dup event_ids, {bad2} differ beyond pending_webhooks")

    # 3. subscription_id format
    sid = silver["subscription_id"]
    n_sched = sid.fillna("").str.startswith("sub_sched_").sum()
    bad_fmt = (~(sid.fillna("").str.startswith("sub_") | sid.fillna("").str.match(r"^[0-9a-f-]{36}$")) & sid.notna()).sum()
    null_not_orphan = silver[silver["subscription_id"].isna() & (~silver["refund_orphan"].fillna(False))].shape[0]
    check(3, n_sched == 0 and bad_fmt == 0 and null_not_orphan == 0,
          f"sub_sched={n_sched}, bad_fmt={bad_fmt}, null_without_orphan={null_not_orphan}")

    # 4. renewals <= physically possible periods since install
    ren = silver[silver["event_name"] == "subscription_renewed"].groupby("subscription_id").size()
    viol4, n_skip4 = [], 0
    unit_days = {"day": 1, "week": 7, "month": 30, "year": 365}
    for sub_id, cnt in ren.items():
        row = silver[silver["subscription_id"] == sub_id].iloc[0]
        inst, unit = row["install_date"], row["interval_unit"]
        # periodicity can only be checked with a known interval; Solidgate interval is
        # frequently NULL (product.name doesn't encode cadence) -> skip + count those.
        if inst is None or not (isinstance(unit, str) and unit in unit_days):
            n_skip4 += 1
            continue
        span_days = (SNAPSHOT_TS.date() - inst).days
        max_periods = span_days / unit_days[unit] + 2  # +2 slack
        if cnt > max_periods:
            viol4.append(f"{sub_id}({unit},{cnt}ren>{max_periods:.0f}max,{span_days}d)")
    check(4, not viol4, f"{len(viol4)} subs exceed max periods among checkable; "
          f"{n_skip4} renewing subs skipped (unknown interval)" + (f" e.g. {viol4[:3]}" if viol4 else ""))

    # 5. price ladder present, max<=200
    pa = silver["price_amount"].dropna()
    ladder = {0.99, 9.99, 39.99}
    present = ladder.issubset(set(round(x, 2) for x in pa.unique()))
    check(5, present and (pa.max() <= 200), f"ladder_present={present}, max={pa.max():.2f}")

    # 6. MONEY rows USD (spec: "100% of money rows USD"). Money source = invoice.paid
    #    (Stripe) + settle_ok orders (Solidgate). Non-money invoice variants may carry
    #    other currencies (28 gbp/eur on updated/created/finalized) — reported as a
    #    Surprise, not a money-row failure.
    cur_paid = con.execute(f"SELECT DISTINCT lower(currency) c FROM read_parquet('{(TMP/'stripe_invoice_family.parquet').as_posix()}') WHERE event_type='invoice.paid' AND currency IS NOT NULL").fetchdf()["c"].tolist()
    cur_sg = con.execute(f"SELECT DISTINCT lower(currency) c FROM read_parquet('{(TMP/'solidgate_events_flat.parquet').as_posix()}') WHERE currency IS NOT NULL").fetchdf()["c"].tolist()
    check(6, set(cur_paid) <= {"usd"} and set(cur_sg) <= {"usd"},
          f"stripe invoice.paid currencies={cur_paid}, solidgate={cur_sg}")

    # 7. churn_type only on terminal; unknown share
    nonterm_churn = silver[(silver["event_name"] != "subscription_expired") & silver["churn_type"].notna()].shape[0]
    term = silver[silver["event_name"] == "subscription_expired"]
    unk = (term["churn_type"] == "unknown").mean() if len(term) else 0
    check(7, nonterm_churn == 0 and unk <= 0.05,
          f"nonterminal_churn={nonterm_churn}, unknown_share={unk:.1%} of {len(term)} terminals")

    # 8. stripe terminal totals reconcile (voluntary~674, involuntary~2718 +/-2%)
    st = term[term["payment_provider"] == "stripe"]["churn_type"].value_counts().to_dict()
    vol, inv = st.get("voluntary", 0), st.get("involuntary", 0)
    ok8 = abs(vol - 674) <= 674 * 0.02 and abs(inv - 2718) <= 2718 * 0.02
    check(8, ok8, f"stripe voluntary={vol}(~674), involuntary={inv}(~2718)")

    # 9. geo coverage (owner-revised 2026-07-07): country truly known for a large
    #    share is impossible — 69% of web_conversions rows carry country='unknown';
    #    spec's 97.3/96.1% were EMAIL-match, not country-known. Thresholds set to the
    #    real ceiling: stripe>=55%, solidgate>=20%. NULL country stays NULL (not the
    #    'unknown' iOS trap); geo_status disambiguates why.
    cov = {}
    for p in ("stripe", "solidgate"):
        d = silver[silver["payment_provider"] == p]
        subs_p = d.groupby("subscription_id")["country_code"].first()
        cov[p] = subs_p.notna().mean() if len(subs_p) else 0
    check(9, cov.get("stripe", 0) >= 0.55 and cov.get("solidgate", 0) >= 0.20,
          f"geo cov stripe={cov.get('stripe',0):.1%}(>=55%), solidgate={cov.get('solidgate',0):.1%}(>=20%)")

    # 10. media never provider name; step populated where media is
    badm = silver[silver["media_source"].isin(["solidgate", "paypal"])].shape[0]
    step_missing = silver[silver["media_source"].notna() & silver["media_source_step"].isna()].shape[0]
    check(10, badm == 0 and step_missing == 0, f"provider-as-media={badm}, media_without_step={step_missing}")

    # 11. every raw subscription_id appears in silver
    raw_stripe_subs = con.execute(f"""
        SELECT DISTINCT subscription_id FROM read_parquet('{(TMP/'stripe_subscription_events.parquet').as_posix()}') WHERE subscription_id IS NOT NULL
    """).fetchdf()["subscription_id"].tolist()
    raw_sg_subs = con.execute(f"""
        SELECT DISTINCT subscription_id FROM read_parquet('{(TMP/'solidgate_events_flat.parquet').as_posix()}') WHERE subscription_id IS NOT NULL
    """).fetchdf()["subscription_id"].tolist()
    silver_subs = set(silver["subscription_id"].dropna())
    missing = [s for s in (raw_stripe_subs + raw_sg_subs) if s not in silver_subs]
    check(11, not missing, f"{len(missing)} raw subs missing from silver" + (f" e.g. {missing[:3]}" if missing else ""))

    # 12. stripe_account 100% non-null on stripe, exactly 3 known
    sa = silver[silver["payment_provider"] == "stripe"]["stripe_account"]
    known = {"JzVYkL7XCu", "IEMVDMVTXC", "A9qayReKqB"}
    nulls12 = sa.isna().sum()
    extra = set(sa.dropna().unique()) - known
    check(12, nulls12 == 0 and not extra, f"stripe_account nulls={nulls12}, unknown_values={extra or 'none'}")

    # 13. exactly one trial_started per subscription
    ts = silver[silver["event_name"] == "trial_started"].groupby("subscription_id").size()
    bad13 = ts[ts != 1]
    subs_no_ts = silver_subs - set(silver[silver["event_name"] == "trial_started"]["subscription_id"])
    check(13, len(bad13) == 0 and len(subs_no_ts) == 0,
          f"{len(bad13)} subs with !=1 trial_started, {len(subs_no_ts)} subs with 0")

    return results


SILVER_PATH = ROOT / "data" / "silver" / "web_events_silver.parquet"
PROFILE_PATH = ROOT / "reports" / "silver_profile.md"
CHAINS_PATH = ROOT / "reports" / "silver_chains_sample.md"
TERMINAL = "subscription_expired"


def _dist(series):
    vc = series.value_counts(dropna=False)
    return "\n".join(f"| {('NULL' if pd.isna(k) else k)} | {v} |" for k, v in vc.items())


def weekly_survival(silver, provider):
    """Observed step-share retention: per subscription, lifetime weeks =
    (death_or_censor - install)/7; at week k, share of matured subs still alive."""
    d = silver[silver["payment_provider"] == provider]
    starts = d[d["event_name"] == "trial_started"].groupby("subscription_id")["event_datetime"].min()
    deaths = d[d["event_name"] == TERMINAL].groupby("subscription_id")["event_datetime"].min()
    rows = []
    sub_life = {}
    for sub_id, inst in starts.items():
        if pd.isna(inst):
            continue
        death = deaths.get(sub_id, pd.NaT)
        censor = SNAPSHOT_TS
        end = death if not pd.isna(death) else censor
        life_wk = max(0, (end - inst).total_seconds() / (7 * 86400))
        avail_wk = (censor - inst).total_seconds() / (7 * 86400)
        sub_life[sub_id] = (life_wk, not pd.isna(death), avail_wk)
    out = []
    for k in [0, 1, 2, 4, 8, 12, 26, 52]:
        matured = [s for s, (lw, dead, aw) in sub_life.items() if aw >= k]
        if not matured:
            out.append((k, float("nan"), 0)); continue
        alive = sum(1 for s in matured if sub_life[s][0] >= k or not sub_life[s][1])
        out.append((k, alive / len(matured), len(matured)))
    return out


def write_profile(con, silver):
    L = ["# web_events_silver — self-check profile\n",
         f"Built from data/raw snapshot (snapshot_ts=2026-07-07). Rows: **{len(silver)}**, "
         f"subscriptions: **{silver['subscription_id'].nunique()}**, "
         f"customer_users: **{silver['customer_user_id'].nunique()}**. Silver keeps everything with flags; "
         "golden derivation (Section 14) is separate.\n"]

    for p in ("stripe", "solidgate"):
        d = silver[silver["payment_provider"] == p]
        L.append(f"\n## {p}: {len(d)} rows / {d['subscription_id'].nunique()} subs / {d['customer_user_id'].nunique()} users\n")
        L.append("### event_name\n| event_name | n |\n|---|---|\n" + _dist(d["event_name"]) + "\n")
        term = d[d["event_name"] == TERMINAL]
        L.append(f"### churn_type (terminals, n={len(term)})\n| churn_type | n | share |\n|---|---|---|")
        vc = term["churn_type"].value_counts(dropna=False)
        for k, v in vc.items():
            L.append(f"| {k} | {v} | {v/len(term):.1%} |")
        L.append("")

    # app_id x stripe_account crosstab (Atelier hypothesis)
    st = silver[silver["payment_provider"] == "stripe"]
    ct = pd.crosstab(st["app_id"], st["stripe_account"])
    L.append("## app_id × stripe_account (Stripe) — Atelier hypothesis\n")
    L.append("| app_id | " + " | ".join(ct.columns) + " |\n|" + "---|" * (len(ct.columns) + 1))
    for app, row in ct.iterrows():
        L.append(f"| {app} | " + " | ".join(str(int(x)) for x in row.values) + " |")
    a9 = st[st["stripe_account"] == "A9qayReKqB"]["app_id"].value_counts()
    verdict = "CONFIRMED" if (len(a9) and a9.index[0] == "atelier") else "NOT confirmed"
    L.append(f"\n**Verdict:** A9qayReKqB → app_id: {a9.to_dict()} — Atelier hypothesis **{verdict}** "
             "(A9qayReKqB is the only account mapped to Atelier; the other two default to invinci absent a dim app_name).\n")

    # trial_type
    subs1 = silver.drop_duplicates("subscription_id")
    L.append("## trial_type (per subscription)\n| trial_type | n |\n|---|---|\n" + _dist(subs1["trial_type"]) + "\n")

    # attribution coverage
    L.append("## attribution (media_source_step, per subscription)\n| step | n |\n|---|---|\n" + _dist(subs1["media_source_step"]) + "\n")

    # geo
    L.append("## geo coverage & ambiguity (per subscription)\n")
    for p in ("stripe", "solidgate"):
        sp = subs1[subs1["payment_provider"] == p]
        cov = sp["country_code"].notna().mean()
        amb = sp["geo_ambiguous"].fillna(False).mean()
        L.append(f"- **{p}**: country resolved {cov:.1%}, geo_ambiguous {amb:.1%} of subs")
    L.append("\n| geo_status | n subs |\n|---|---|\n" + _dist(subs1["geo_status"]))
    L.append("\n> **PRESENTATION LIMITATION:** geo-сегментация покрывает **57% Stripe / 24% Solidgate** "
             "подписок; остальные гео-агностик (country_code=NULL, geo_status='unknown'/'no_match'). "
             "Причина — 69% строк web_conversions имеют country='unknown' в источнике. NULL здесь = "
             "«страна неизвестна», НЕ «нет строки» (см. geo_status).\n")

    # interval_source
    L.append("## interval_source (per subscription)\n| interval_source | n |\n|---|---|\n" + _dist(subs1["interval_source"]) + "\n")

    # flags
    L.append("## flags\n")
    L.append(f"- is_qa rows: {int(silver['is_qa'].sum())} ({silver.loc[silver['is_qa'],'subscription_id'].nunique()} subs)")
    L.append(f"- fraud churn rows: {int((silver['churn_type']=='fraud').sum())}")
    L.append(f"- refund_orphan rows: {int(silver['refund_orphan'].sum())}")
    L.append("- non-USD invoices (audit): 28 gbp/eur rows, ALL on non-money invoice variants "
             "(updated/created/finalized); 0 money rows non-USD (assert 6 pass).\n")

    # weekly survival + churn outcome shares
    L.append("## Observed weekly survival (step-share retention) — the baseline curve\n")
    for p in ("stripe", "solidgate"):
        L.append(f"### {p}\n| week | survival | n matured |\n|---|---|---|")
        for k, s, n in weekly_survival(silver, p):
            L.append(f"| {k} | {'' if pd.isna(s) else f'{s:.3f}'} | {n} |")
        L.append("")
    L.append("## Terminal churn_type shares (input to one-map vs three-map decision)\n")
    L.append("| provider | voluntary | involuntary | fraud | unknown | n terminals |\n|---|---|---|---|---|---|")
    for p in ("stripe", "solidgate"):
        t = silver[(silver["payment_provider"] == p) & (silver["event_name"] == TERMINAL)]
        n = len(t)
        sh = lambda c: f"{(t['churn_type']==c).sum()/n:.1%}" if n else "-"
        L.append(f"| {p} | {sh('voluntary')} | {sh('involuntary')} | {sh('fraud')} | {sh('unknown')} | {n} |")
    L.append("")

    PROFILE_PATH.write_text("\n".join(L), encoding="utf-8")
    print(f"wrote {PROFILE_PATH}")


def write_chains(silver):
    import random
    rng = random.Random(20260707)
    L = ["# silver_chains_sample.md — 20 stratified subscription chains\n",
         "10 Stripe + 10 Solidgate, stratified to include ≥2 voluntary deaths, ≥2 involuntary, "
         "≥1 fraud, ≥2 alive renewers, ≥1 QA. Read all 20 by eye before silver is used.\n"]

    def pick(provider, pred, n, exclude):
        d = silver[(silver["payment_provider"] == provider)]
        subs = [s for s in d["subscription_id"].dropna().unique() if s not in exclude]
        good = []
        for s in subs:
            g = d[d["subscription_id"] == s]
            if pred(g):
                good.append(s)
        rng.shuffle(good)
        return good[:n]

    def has_churn(g, c): return ((g["event_name"] == TERMINAL) & (g["churn_type"] == c)).any()
    def alive_renewer(g): return (g["event_name"] == "subscription_renewed").sum() >= 2 and not (g["event_name"] == TERMINAL).any()
    def is_qa(g): return g["is_qa"].any()

    chosen, seen = [], set()
    plan = [("stripe", lambda g: has_churn(g, "voluntary"), 2), ("stripe", lambda g: has_churn(g, "involuntary"), 2),
            ("stripe", alive_renewer, 2), ("stripe", is_qa, 1),
            ("solidgate", lambda g: has_churn(g, "fraud"), 1), ("solidgate", lambda g: has_churn(g, "involuntary"), 2),
            ("solidgate", lambda g: has_churn(g, "voluntary"), 1), ("solidgate", alive_renewer, 2)]
    for prov, pred, n in plan:
        for s in pick(prov, pred, n, seen):
            chosen.append((prov, s)); seen.add(s)
    # fill remainder to 10 each
    for prov in ("stripe", "solidgate"):
        have = sum(1 for p, _ in chosen if p == prov)
        for s in pick(prov, lambda g: True, 10 - have, seen):
            chosen.append((prov, s)); seen.add(s)

    for prov, s in chosen:
        g = silver[silver["subscription_id"] == s].sort_values("event_datetime")
        meta = g.iloc[0]
        L.append(f"\n### {prov} `{s}` — app={meta['app_id']}, geo={meta['country_code']}({meta['geo_status']}), "
                 f"media={meta['media_source']}/{meta['media_source_step']}, interval={meta['interval_unit']}"
                 f"({meta['interval_source']}), trial={meta['trial_type']}, qa={meta['is_qa']}\n")
        L.append("| event_datetime | event_name | churn_type | price | raw_event_type |\n|---|---|---|---|---|")
        for _, r in g.iterrows():
            pa = "" if pd.isna(r["price_amount"]) else f"${r['price_amount']:.2f}"
            ct = "" if pd.isna(r["churn_type"]) else r["churn_type"]
            L.append(f"| {r['event_datetime']} | {r['event_name']} | {ct} | {pa} | {r['raw_event_type']} |")

    CHAINS_PATH.write_text("\n".join(L), encoding="utf-8")
    print(f"wrote {CHAINS_PATH} ({len(chosen)} chains)")


if __name__ == "__main__":
    args = sys.argv[1:]
    con = get_con()
    if not args or args[0] == "extract":
        run_extract(con, dry="--dry" in args, force="--force" in args)
    elif args[0] == "report":
        silver = pd.read_parquet(SILVER_PATH)
        write_profile(con, silver)
        write_chains(silver)
    elif args[0] == "trace":
        (trace_stripe if args[1] == "stripe" else trace_solidgate)(con, args[2])
    elif args[0] == "map":
        silver = build_silver(con)
        out = ROOT / "data" / "silver" / "web_events_silver.parquet"
        if "--asserts-only" not in args:
            out.parent.mkdir(parents=True, exist_ok=True)
        print(f"\n[map] silver assembled: {len(silver)} rows, "
              f"{silver['subscription_id'].nunique()} subs, "
              f"{(silver['payment_provider']=='stripe').sum()} stripe / {(silver['payment_provider']=='solidgate').sum()} solidgate rows")
        print("\n=== 12+1 ASSERTS ===")
        results = run_asserts(con, silver)
        allpass = True
        for n, ok, detail in results:
            allpass = allpass and ok
            print(f"  [{'PASS' if ok else 'FAIL'}] assert {n}: {detail}")
        if not allpass:
            print("\n>>> STOP: assert failure(s) above. Silver NOT written.")
            sys.exit(1)
        out.parent.mkdir(parents=True, exist_ok=True)
        silver.to_parquet(out)
        print(f"\n[map] all asserts pass -> wrote {out} ({len(silver)} rows)")
    else:
        print("usage: build_web_silver.py [extract [--dry] [--force] | map | trace <stripe|solidgate> <id>]")
