import pyarrow.parquet as pq
import pandas as pd

df = pq.read_table(r"C:\Users\yahor\PycharmProjects\LTV\ios_events.parquet").to_pandas(ignore_metadata=True)
df = df[df["customer_user_id"].notna()].copy()
df["event_datetime"] = pd.to_datetime(df["event_datetime"], utc=True)

# ключ подписки = юзер + апп
df["sub_id"] = df["customer_user_id"] + "|" + df["app_id"]

# зазор до предыдущего события внутри подписки
df = df.sort_values(["sub_id", "event_datetime"])
df["gap_days"] = df.groupby("sub_id")["event_datetime"].diff().dt.days

# найдём подписки, где есть разрыв > 30 дней (кандидаты на переподписку)
resub = df[df["gap_days"] > 30]["sub_id"].unique()
print("подписок с разрывом >30 дней:", len(resub))

# посмотрим 5 таких цепочек целиком
for sid in resub[:5]:
    print("\n==== ", sid, " ====")
    print(df[df["sub_id"] == sid][["event_name","event_datetime","gap_days"]].to_string(index=False))