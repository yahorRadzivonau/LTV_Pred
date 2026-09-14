"""
Pull the web event stream for the per-person pipeline (ltv_v3) from BigQuery.

ONE query serves both the revenue side and the population side, so the two can
never disagree about a snapshot. It replaces the two separate v2 pulls
(pull_ltv_v2_raw.py + pull_ltv_v2_population.py).

Three differences from the v2 pulls, each deliberate:

1. ALL trial variants are pulled, not just the real $0.99 charge. v2 filtered
   `amt != 1.00` because it only needed money; v3's denominator is per-trial
   (owner's definition: everyone who started a trial of ANY kind, plus everyone
   who paid for base), so placeholder ($1.00 card-validation hold) and free
   (NULL) trials must be present. They enter the population, never the revenue
   -- ltv_v3.revenue does that split.

2. Lever columns come along in the same rows: funnel_name, country,
   utm_source, campaign_name, ad_name. v2 read them from
   data/raw/web_conversions.parquet, a local snapshot frozen around 2026-07-09
   and by now stale. campaign_name/ad_name were added 2026-08-06 for the
   ml_web_weekly_curve_{geo,campaign,ad,full} breakdown tables -- both are raw
   here (URL-encoded values, macro leftovers and all); normalisation happens
   downstream in ltv_v3/dims.py where it is measured and documented.

2b. State-signal events (billing_issue, sub_cancelled) are pulled alongside the
   money events. They are NOT revenue and ltv_v3.revenue never counts them --
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
   ltv_v3.revenue keys on gross; only the money sums use amt_net. Putting the
   classifiers on net would make their thresholds depend on the acquirer's fee
   schedule AND on which provider happened to charge the row (solidgate and
   stripe take different cuts), so the same $0.99 trial would land in different
   buckets depending on who processed it. See ltv_v3/config.py, "net prices".

5. PULL_CEILING_DATE, when set, caps the pull. It exists because trial_started
   broke upstream on 2026-09-02 -- see that constant's comment in config.py.

Three safety guards, same pattern as web/v2/pull_ltv_v2_raw.py:
  1. dry-run to learn the scanned bytes BEFORE downloading anything
  2. hard stop if that exceeds LIMIT_GB
  3. manual (yes/no) confirmation before the real (billed) query runs

Writes: data/web_v3/events_<today>.parquet   (nothing else, ever)

Run: .venv/Scripts/python.exe web/v3/pull_v3_events.py
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

from ltv_v3.config import (  # noqa: E402
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

print("\nlever coverage (share non-null):")
for c in ["funnel_name", "country", "utm_source", "campaign_name", "ad_name"]:
    print(f"  {c}: {df[c].notna().mean():.1%} non-null, {df[c].nunique()} distinct")

print(f"\nСледующий шаг: .venv/Scripts/python.exe web/v3/build_web_se_training.py")
