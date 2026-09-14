"""
Pull a fresh population/attribution snapshot for the appsflyer-source pipeline
(ltv_v2), replacing the previous undocumented ad-hoc pull that produced
data/raw/bq_appsflyer_person_dim_2026-07-11.parquet (no pull script for it existed
anywhere in the repo -- see docs/REPO_AUDIT.md section 9).

Methodology reverse-engineered and verified against the frozen 07-11 file (see
session notes): for each email, first_date/first_funnel come from that person's
EARLIEST captured-revenue event (same event-type scope as pull_ltv_v2_raw.py:
subscription_started, upsale_converted, real trial_started), scoped to
app_name IN APP_NAMES -- NOT the raw first-ever web_conversions touch (which
includes non-revenue events from unrelated funnels/apps and over-counts ~5x).
Verified: this query's funnel-value distribution and order-of-magnitude match the
frozen file closely; the funnel whitelist/organic-sentinel filter and the
POP_START_DATE cutoff are applied downstream in
web/golden/ltv/cohorts.py:build_population_5406(), not here -- this script
reproduces the RAW (unfiltered-by-whitelist) dim file, same as the original.

Same three safety guards as pull_ltv_v2_raw.py (dry-run size check, hard GB limit,
manual yes/no confirmation).

Output: data/raw/bq_appsflyer_person_dim_<today>.parquet, columns
[person_key, first_date, first_funnel] -- same shape as the frozen file. After
running, pass person_dim_path="data/raw/bq_appsflyer_person_dim_<today>.parquet"
explicitly from build_tables.py's cohorts.build_population_5406() call (do not
change the shared default in web/golden/ltv/cohorts.py -- that default is also used
by the golden pipeline).

Run: .venv/Scripts/python.exe web/v2/pull_ltv_v2_population.py
"""
import os
from datetime import date
from pathlib import Path

import pandas as pd
from google.cloud import bigquery

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
os.chdir(ROOT)

from ltv_v2.config import (  # noqa: E402
    APP_NAMES, BASE_EVENT_TYPE, UPS_EVENT_TYPE, TRIAL_EVENT_TYPE, TRIAL_PLACEHOLDER_AMOUNT,
)

OUT = ROOT / "data" / "raw" / f"bq_appsflyer_person_dim_{date.today().isoformat()}.parquet"
SOURCE = "appsflyer-data-411716.silver_layer.web_conversions"

app_names_sql = ", ".join(f"'{a}'" for a in APP_NAMES)
sql = f"""
SELECT
  LOWER(email) AS person_key,
  ARRAY_AGG(
    STRUCT(event_date, funnel_name)
    ORDER BY event_date ASC LIMIT 1
  )[OFFSET(0)].event_date AS first_date,
  ARRAY_AGG(
    STRUCT(event_date, funnel_name)
    ORDER BY event_date ASC LIMIT 1
  )[OFFSET(0)].funnel_name AS first_funnel
FROM `{SOURCE}`
WHERE email IS NOT NULL
  AND app_name IN ({app_names_sql})
  AND (
    event_type IN ('{BASE_EVENT_TYPE}', '{UPS_EVENT_TYPE}')
    OR (event_type = '{TRIAL_EVENT_TYPE}' AND transaction_amount_usd IS NOT NULL
        AND transaction_amount_usd != {TRIAL_PLACEHOLDER_AMOUNT})
  )
GROUP BY person_key
"""

client = bigquery.Client(project="appsflyer-data-411716")

# --- ПРЕДОХРАНИТЕЛЬ 1: dry-run, узнаём объём БЕЗ выгрузки ---
dry = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True))
gb = dry.total_bytes_processed / 1024**3
print(f"Source: {SOURCE}")
print(f"Просканирует: {gb:.4f} ГБ")

# --- ПРЕДОХРАНИТЕЛЬ 2: обрыв, если больше лимита ---
LIMIT_GB = 2.0
if gb > LIMIT_GB:
    raise SystemExit(f"СТОП: {gb:.2f} ГБ больше лимита {LIMIT_GB} ГБ. Не качаю.")

# --- ПРЕДОХРАНИТЕЛЬ 3: спросить подтверждение вручную ---
if input(f"Качаю {gb:.4f} ГБ -> {OUT.name}. Продолжить? (yes/no): ").strip().lower() != "yes":
    raise SystemExit("Отменено пользователем.")

df = client.query(sql).to_dataframe(create_bqstorage_client=True)
# plain YYYY-MM-DD strings, matching the frozen 07-11 file's column exactly
# (build_population_5406 parses this with plain pd.to_datetime, no tz/time component)
df["first_date"] = pd.to_datetime(df["first_date"]).dt.strftime("%Y-%m-%d")
df["first_funnel"] = df["first_funnel"].fillna("")
df.to_parquet(OUT)
print("Готово:", df.shape, "->", OUT)

old = ROOT / "data" / "raw" / "bq_appsflyer_person_dim_2026-07-11.parquet"
if old.exists():
    old_df = pd.read_parquet(old)
    print(f"\nСравнение с прошлым фризом (2026-07-11): {len(old_df)} -> {len(df)} строк "
          f"({(len(df) - len(old_df)) / len(old_df):+.1%})")

print("\nfirst_funnel value counts (top 20):")
print(df["first_funnel"].value_counts(dropna=False).head(20))
print(f"\nНапоминание: передать новый путь явным аргументом из build_tables.py:\n"
      f'  cohorts.build_population_5406(person_dim_path="data/raw/{OUT.name}")')
