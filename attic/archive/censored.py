import pyarrow.parquet as pq
import pandas as pd
import numpy as np

EVENTS = r"C:\Users\yahor\PycharmProjects\LTV\ios_events.parquet"
MX     = r"C:\Users\yahor\PycharmProjects\LTV\se_training.parquet"
OUT    = r"C:\Users\yahor\PycharmProjects\LTV\se_training_cens.parquet"

# --- дата выгрузки и first_pay по каждой подписке ---
df = pq.read_table(EVENTS).to_pandas(ignore_metadata=True)
df = df[df["customer_user_id"].notna()].copy()
df["event_datetime"] = pd.to_datetime(df["event_datetime"], utc=True)
df["sub_id"] = df["customer_user_id"] + "|" + df["app_id"]

T_snapshot = df["event_datetime"].max()          # момент "сейчас" в данных
print("дата выгрузки (snapshot):", T_snapshot)

first_pay = (df[df["event_name"].isin(["trial_converted","subscription_renewed"])]
             .groupby("sub_id")["event_datetime"].min().rename("first_pay"))

# --- матрица ---
mx = pd.read_parquet(MX)
mx = mx.merge(first_pay, on="sub_id", how="left")

# сколько недель максимально могло пройти у этого юзера к моменту выгрузки
mx["weeks_observable"] = ((T_snapshot - mx["first_pay"]).dt.total_seconds() / (7*86400)).astype(float)

# ступень k наблюдаема, если k <= weeks_observable (примерно: k-я неделя успела наступить)
# censored = ступень за пределом наблюдения
mx["censored"] = mx["step_k"] > mx["weeks_observable"]

print("\nдоля censored строк:", round(mx["censored"].mean(), 3))
print("censored по ступенькам (доля):")
print(mx.groupby("step_k")["censored"].mean().round(2).head(20).to_string())

mx.to_parquet(OUT)
print("\nсохранил:", OUT, mx.shape)