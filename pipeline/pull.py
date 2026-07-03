from pathlib import Path
from google.cloud import bigquery

OUT = Path(__file__).resolve().parent.parent / "data" / "ios_events.parquet"

client = bigquery.Client(project="appsflyer-data-411716")

sql = """
SELECT
  customer_user_id, app_id, event_name, event_datetime,
  install_date, country_code, media_source, attribution_source, product_id
FROM `appsflyer-data-411716.silver_layer.conversions`
WHERE event_name IN (
  'trial_started','trial_converted','trial_cancelled','trial_expired',
  'subscription_renewed','subscription_cancelled','subscription_expired',
  'auto_renew_off','auto_renew_off_subscription','auto_renew_on',
  'billing_issue_detected','entered_grace_period','subscription_refunded'
)
"""

# --- ПРЕДОХРАНИТЕЛЬ 1: dry-run, узнаём объём БЕЗ выгрузки ---
dry = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True))
gb = dry.total_bytes_processed / 1024**3
print(f"Просканирует: {gb:.2f} ГБ")

# --- ПРЕДОХРАНИТЕЛЬ 2: обрыв, если больше лимита ---
LIMIT_GB = 2.0
if gb > LIMIT_GB:
    raise SystemExit(f"СТОП: {gb:.2f} ГБ больше лимита {LIMIT_GB} ГБ. Не качаю.")

# --- ПРЕДОХРАНИТЕЛЬ 3: спросить подтверждение вручную ---
if input(f"Качаю {gb:.2f} ГБ. Продолжить? (yes/no): ").strip().lower() != "yes":
    raise SystemExit("Отменено пользователем.")

df = client.query(sql).to_dataframe(create_bqstorage_client=True)
df.to_parquet(OUT)
print("Готово:", df.shape)