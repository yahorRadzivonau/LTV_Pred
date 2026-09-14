"""
Pull fresh captured-revenue events for the appsflyer-source pipeline (ltv_v2) from
BigQuery, replacing the previous ad-hoc (unscripted) pull that produced
data/raw/appsflyer_captured_events_with_trial_2026-07-20.parquet.

Same filter as the frozen file (see ltv_v2/config.py "captured-revenue rule" and
"trial revenue rule"): app_name IN APP_NAMES, event_date >= WINDOW_START,
event_type IN {subscription_started, upsale_converted} plus real (non-placeholder)
trial_started rows only (amount NOT NULL AND != TRIAL_PLACEHOLDER_AMOUNT).

Three safety guards, same pattern as pipeline/pull.py:
  1. dry-run to learn the scanned bytes BEFORE downloading anything
  2. hard stop if that exceeds LIMIT_GB
  3. manual (yes/no) confirmation before the real (billed) query runs

Output: data/raw/appsflyer_captured_events_with_trial_<today>.parquet (old freezes
are left on disk untouched, per the existing rollback convention). After running,
update RAW_EVENTS_PATH and SNAPSHOT_DATE in ltv_v2/config.py by hand to point at it.

Run: .venv/Scripts/python.exe web/v2/pull_ltv_v2_raw.py
"""
import os
import sys
from datetime import date
from pathlib import Path

from google.cloud import bigquery

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
os.chdir(ROOT)

from ltv_v2.config import (  # noqa: E402
    APP_NAMES, BASE_EVENT_TYPE, UPS_EVENT_TYPE, TRIAL_EVENT_TYPE,
    TRIAL_PLACEHOLDER_AMOUNT, WINDOW_START,
)

OUT = ROOT / "data" / "raw" / f"appsflyer_captured_events_with_trial_{date.today().isoformat()}.parquet"
SOURCE = "appsflyer-data-411716.silver_layer.web_conversions"

app_names_sql = ", ".join(f"'{a}'" for a in APP_NAMES)
sql = f"""
SELECT
  LOWER(email) AS email,
  UNIX_SECONDS(event_date) AS ts,
  event_type,
  transaction_amount_usd AS amt,
  app_name
FROM `{SOURCE}`
WHERE event_date >= TIMESTAMP('{WINDOW_START.date().isoformat()}')
  AND app_name IN ({app_names_sql})
  AND email IS NOT NULL
  AND (
    event_type IN ('{BASE_EVENT_TYPE}', '{UPS_EVENT_TYPE}')
    OR (event_type = '{TRIAL_EVENT_TYPE}' AND transaction_amount_usd IS NOT NULL
        AND transaction_amount_usd != {TRIAL_PLACEHOLDER_AMOUNT})
  )
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
df.to_parquet(OUT)
print("Готово:", df.shape, "->", OUT)
print("\nevent_type counts:")
print(df["event_type"].value_counts())
print(f"\nНапоминание: обновить в ltv_v2/config.py вручную:\n"
      f'  RAW_EVENTS_PATH = "data/raw/{OUT.name}"\n'
      f'  SNAPSHOT_DATE = "{date.today().isoformat()}"')
