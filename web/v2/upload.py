import logging
import pandas as pd
import yaml
from google.api_core.exceptions import NotFound
from google.cloud import bigquery
import os

# папка, где лежит сам upload.py = web/v2/
HERE = os.path.dirname(os.path.abspath(__file__))
# корень проекта = на два уровня выше (web/v2/ -> web/ -> корень)
ROOT = os.path.dirname(os.path.dirname(HERE))

SCHEMA_YAML = os.path.join(HERE, "schema_ml_web_predictions.yaml")
DATA_PARQUET = os.path.join(ROOT, "reports", "web_v2", "table_C_cohort_funnel_utm_appsflyer.parquet")
logging.basicConfig(level=logging.INFO)
BQ_CLIENT = bigquery.Client()

PROJECT = "appsflyer-data-411716"
DATASET = "ad_hock_tables"
TABLE = "ml_web_predictions"

def get_schema_from_yaml(path):
    with open(path) as f:
        s = yaml.safe_load(f)
    return [bigquery.SchemaField(name=x["name"], field_type=x["type"], mode=x["mode"]) for x in s]

def cast_columns(df, name, typ):
    if name not in df.columns:
        df[name] = None
    if typ == "INTEGER":
        df[name] = df[name].fillna(0).astype(int)
    elif typ == "FLOAT":
        df[name] = df[name].astype(float)
    elif typ == "DATE":
        df[name] = pd.to_datetime(df[name], errors="coerce").dt.date
    elif typ == "BOOLEAN":
        df[name] = df[name].astype(bool)
    else:
        df[name] = df[name].astype(str)
    return df

schema = get_schema_from_yaml(SCHEMA_YAML)
df = pd.read_parquet(DATA_PARQUET)

# cast каждую колонку строго под схему
schema_yaml = yaml.safe_load(open(SCHEMA_YAML))
for field in schema_yaml:
    df = cast_columns(df, field["name"], field["type"])
df = df[[f["name"] for f in schema_yaml]]  # порядок и только схемные колонки

print(f"rows={len(df)}, cols={len(df.columns)}")
print(df.head(20).to_string())

# --- ПРОВЕРКА ПЕРЕД ЗАЛИВКОЙ: раскомментируй load только после того как глянул превью ---
table_id = f"{PROJECT}.{DATASET}.{TABLE}"
table_ref = BQ_CLIENT.dataset(DATASET).table(TABLE)
try:
    BQ_CLIENT.get_table(table_ref)
    print("table EXISTS")
except NotFound:
    print("table NOT found -> will be created")
    BQ_CLIENT.create_table(bigquery.Table(table_ref, schema=schema))

job_config = bigquery.LoadJobConfig(schema=schema, write_disposition="WRITE_TRUNCATE")
job = BQ_CLIENT.load_table_from_dataframe(df, table_id, job_config=job_config)
print(job.result())