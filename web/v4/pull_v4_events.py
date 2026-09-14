"""
Pull the web event stream for the per-person pipeline (ltv_v4) from BigQuery.

ONE query serves both the revenue side and the population side, so the two can
never disagree about a snapshot. It replaces the two separate v2 pulls
(pull_ltv_v2_raw.py + pull_ltv_v2_population.py).

Three differences from the v2 pulls, each deliberate:

1. ALL trial variants are pulled, not just the real $0.99 charge. v2 filtered
   `amt != 1.00` because it only needed money; v3's denominator is per-trial
   (owner's definition: everyone who started a trial of ANY kind, plus everyone
   who paid for base), so placeholder ($1.00 card-validation hold) and free
   (NULL) trials must be present. They enter the population, never the revenue
   -- ltv_v4.revenue does that split.

2. Lever columns come along in the same rows: funnel_name, country,
   utm_source, campaign_name, ad_name. v2 read them from
   data/raw/web_conversions.parquet, a local snapshot frozen around 2026-07-09
   and by now stale. campaign_name/ad_name were added 2026-08-06 for the
   ml_web_weekly_curve_{geo,campaign,ad,full} breakdown tables -- both are raw
   here (URL-encoded values, macro leftovers and all); normalisation happens
   downstream in ltv_v4/dims.py where it is measured and documented.

2b. State-signal events (billing_issue, sub_cancelled) are pulled alongside the
   money events. They are NOT revenue and ltv_v4.revenue never counts them --
   they exist to LABEL outcomes, the way ios/pipeline/se_training.py uses its
   DUNNING/VOLUNTARY sets. Without a dunning signal "recovered" degrades to the
   days_to_next > 10 proxy alone, and the return-window censoring fix has
   nothing to key on.

3. The pull floor (PULL_FLOOR_DATE, 2025-10-01) is EARLIER than the training
   window (WINDOW_START, 2026-04-13). A person whose trial started 2026-04-10
   and whose first base payment landed 2026-04-20 would be assigned the wrong
   first_date -- and therefore the wrong cohort -- if the pull itself were cut
   at WINDOW_START. WINDOW_START is applied downstream, when the population and
   the training matrix are built. Training still uses 2026-04-13 onward only.

4. BOTH amount columns come along: transaction_amount_usd AS amt (GROSS) and
   net_revenue AS amt_net (after the acquirer's cut). They are NOT
   interchangeable, and the split is the whole design. Gross identifies WHICH
   product a row is -- trial 0.99, placeholder hold 1.00, product B 4.99, base
   9.99 -- while net is what we actually earned. Every classifier in
   ltv_v4.revenue keys on gross; only the money sums use amt_net. Putting the
   classifiers on net would make their thresholds depend on the acquirer's fee
   schedule AND on which provider happened to charge the row (solidgate and
   stripe take different cuts), so the same $0.99 trial would land in different
   buckets depending on who processed it. See ltv_v4/config.py, "net prices".

5. PULL_CEILING_DATE, when set, caps the pull. It exists because trial_started
   broke upstream on 2026-09-02 -- see that constant's comment in config.py.

6. customer_user_id comes along, aliased to subscription_key. This is the ONE
   new column v4 needs and the reason the pull had to change at all.

   ltv_v3/config.py rejects customer_user_id, correctly, as an IDENTITY key:
   "Solidgate reissues customer_account_id on resubscribe/retry, so one person
   shows up under 2+ ids". That makes it useless for deciding WHO someone is --
   and it is exactly what makes it right for deciding WHICH SUBSCRIPTION a
   payment belongs to. It is not a broken identity key, it is a key at a
   different grain: email = person, customer_user_id = subscription.

   Measured (reports/web_v4/session_rule_findings.md), on people with 2+ ids,
   gaps between consecutive base payments:
       partitioned by email              39.8% of gaps <= 3 days, median 4.0d
       partitioned by (email, this key)   1.7% of gaps <= 3 days, p25=med=p75=7.0d
   Partitioning by this column turns what looks like noise into a textbook
   weekly subscription. 996 people (8.3% of base payers) run two subscriptions
   at overlapping times; without this column their payments interleave into one
   chain and the 3-day dedup in revenue.base_payment_ladder silently deletes
   4,672 real payments ($46,190) from the survival ladder while the revenue
   anchor still counts them.

   DO NOT use it to join people. Use LOWER(email) for that, as v3 does.

Three safety guards, same pattern as web/v2/pull_ltv_v2_raw.py:
  1. dry-run to learn the scanned bytes BEFORE downloading anything
  2. hard stop if that exceeds LIMIT_GB
  3. manual (yes/no) confirmation before the real (billed) query runs

Writes: data/web_v4/events_<today>.parquet   (nothing else, ever)

Run: .venv/Scripts/python.exe web/v4/pull_v4_events.py
"""
import os
import sys
from datetime import date
from pathlib import Path

from google.cloud import bigquery

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
os.chdir(ROOT)

from ltv_v4.config import (  # noqa: E402
    SOURCE, APP_NAMES, TRIAL_EVENT_TYPE, CAPTURED_EVENT_TYPES, STATE_EVENT_TYPES,
    PULL_FLOOR_DATE, PULL_CEILING_DATE, WINDOW_START, DATA_DIR,
)

PULLED_EVENT_TYPES = CAPTURED_EVENT_TYPES + STATE_EVENT_TYPES

OUT_DIR = ROOT / DATA_DIR
OUT = OUT_DIR / f"events_{date.today().isoformat()}.parquet"
LIMIT_GB = 2.0

app_names_sql = ", ".join(f"'{a}'" for a in APP_NAMES)
event_types_sql = ", ".join(f"'{e}'" for e in PULLED_EVENT_TYPES)
ceiling_sql = (
    f"  AND event_date < TIMESTAMP('{PULL_CEILING_DATE.date().isoformat()}')"
    if PULL_CEILING_DATE is not None else ""
)
sql = f"""
SELECT
  LOWER(email)            AS email,
  UNIX_SECONDS(event_date) AS ts,
  event_type,
  transaction_amount_usd  AS amt,
  net_revenue             AS amt_net,
  customer_user_id        AS subscription_key,
  payment_provider,
  app_name,
  funnel_name,
  country,
  utm_source,
  campaign_name,
  ad_name
FROM `{SOURCE}`
WHERE event_date >= TIMESTAMP('{PULL_FLOOR_DATE.date().isoformat()}')
{ceiling_sql}
  AND app_name IN ({app_names_sql})
  AND email IS NOT NULL
  AND event_type IN ({event_types_sql})
"""

client = bigquery.Client(project=SOURCE.split(".")[0])

# --- ПРЕДОХРАНИТЕЛЬ 1: dry-run, узнаём объём БЕЗ выгрузки ---
dry = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True))
gb = dry.total_bytes_processed / 1024**3
print(f"Source: {SOURCE}")
print(f"Pull floor: {PULL_FLOOR_DATE.date()} (training window starts {WINDOW_START.date()})")
if PULL_CEILING_DATE is not None:
    print(f"Pull CEILING: {PULL_CEILING_DATE.date()}  <- TEMPORARY, trial_started broken upstream since 2026-09-02")
print(f"Просканирует: {gb:.4f} ГБ")

# --- ПРЕДОХРАНИТЕЛЬ 2: обрыв, если больше лимита ---
if gb > LIMIT_GB:
    raise SystemExit(f"СТОП: {gb:.2f} ГБ больше лимита {LIMIT_GB} ГБ. Не качаю.")

# --- ПРЕДОХРАНИТЕЛЬ 3: спросить подтверждение вручную ---
if input(f"Качаю {gb:.4f} ГБ -> {OUT.name}. Продолжить? (yes/no): ").strip().lower() != "yes":
    raise SystemExit("Отменено пользователем.")

df = client.query(sql).to_dataframe(create_bqstorage_client=True)

OUT_DIR.mkdir(parents=True, exist_ok=True)
df.to_parquet(OUT)
print("Готово:", df.shape, "->", OUT)

# ---------------------------------------------------------------- sanity print
print("\nevent_type counts:")
print(df["event_type"].value_counts().to_string())

tr = df[df["event_type"] == TRIAL_EVENT_TYPE]
print(f"\ntrial_started breakdown ({len(tr)} rows):")
print(f"  real (amt not null, != 1.00): {int((tr['amt'].notna() & (tr['amt'].round(2) != 1.00)).sum())}")
print(f"  placeholder (amt == 1.00):    {int((tr['amt'].round(2) == 1.00).sum())}")
print(f"  free (amt is null):           {int(tr['amt'].isna().sum())}")
print(f"  zero-charge (amt == 0):       {int((tr['amt'].round(2) == 0.00).sum())}  <- classified FREE, see config")

# net_revenue landed upstream in 2026-09. Check it here, at the only point where
# the raw column is still visible -- everything downstream sees amt_net already
# cleaned, so a silent NULL column would surface as a mysteriously small LTV.
money_rows = df[df["event_type"].isin(CAPTURED_EVENT_TYPES) & df["amt"].notna() & (df["amt"] > 0)]
print(f"\nnet_revenue: {df['amt_net'].notna().mean():.1%} non-null over all {len(df)} pulled rows")
if len(money_rows):
    print(f"  net/gross on {len(money_rows)} captured money rows: "
          f"{money_rows['amt_net'].sum() / money_rows['amt'].sum():.5f}  "
          f"(expect ~0.954: base 0.957 / ups 0.958 / trial 0.906, mixed)")
    miss = money_rows[money_rows["amt_net"].isna()]
    print(f"  rows with amt > 0 but amt_net NULL: {len(miss)} (${miss['amt'].sum():,.2f}, "
          f"{miss['amt'].sum() / money_rows['amt'].sum():.3%} of captured gross)")
    if len(miss):
        print(f"    by provider: {miss['payment_provider'].value_counts().to_dict()}")
        print(f"    KNOWN: paypal has no net_revenue formula upstream and is unused since 2026-05.")
        print(f"    Anything else here is a NEW hole -- revenue.load_events will refuse to build.")

state = df[df["event_type"].isin(STATE_EVENT_TYPES)]
print(f"\nstate-signal rows (label-only, never revenue): {len(state)}")

# subscription_key is load-bearing for v4: every downstream grain (session,
# cohort, ladder, anchor, upsell attribution) is derived from it. A NULL here
# does not fail loudly downstream -- it silently collapses those rows into one
# anonymous bucket -- so the hole has to be visible right here, at the pull.
print("\nsubscription_key (customer_user_id):")
print(f"  {df['subscription_key'].notna().mean():.2%} non-null over all {len(df)} rows")
_sk_null = df[df["subscription_key"].isna()]
if len(_sk_null):
    print(f"  {len(_sk_null)} rows WITHOUT a subscription_key, by event_type: "
          f"{_sk_null['event_type'].value_counts().to_dict()}")
    print("  ^ these rows cannot be assigned to a subscription. Check before building.")
_per_person = df.groupby("email")["subscription_key"].nunique()
print(f"  distinct keys per person: 1 -> {int((_per_person == 1).sum())}, "
      f"2 -> {int((_per_person == 2).sum())}, "
      f"3+ -> {int((_per_person >= 3).sum())}")
print(f"  (people with 2+ keys run 2+ subscriptions -- that is the v4 change, "
      f"not an anomaly)")

print("\nlever coverage (share non-null):")
for c in ["funnel_name", "country", "utm_source", "campaign_name", "ad_name"]:
    print(f"  {c}: {df[c].notna().mean():.1%} non-null, {df[c].nunique()} distinct")

print(f"\nСледующий шаг: .venv/Scripts/python.exe web/v4/build_web_se_training.py")
