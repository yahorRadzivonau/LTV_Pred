import pyarrow.parquet as pq
import pandas as pd

df = pq.read_table(r"C:\Users\yahor\PycharmProjects\LTV\ios_events.parquet").to_pandas(ignore_metadata=True)
df = df[df["customer_user_id"].notna()].copy()
df["event_datetime"] = pd.to_datetime(df["event_datetime"], utc=True)
df["sub_id"] = df["customer_user_id"] + "|" + df["app_id"]

# сколько раз у одной пары юзер+апп встречается trial_started
trials_per_sub = df[df["event_name"]=="trial_started"].groupby("sub_id").size()
print("пар юзер+апп всего:", df["sub_id"].nunique())
print("из них с >1 trial_started (кандидаты на переподписку):", (trials_per_sub>1).sum())
print("доля:", round((trials_per_sub>1).mean()*100,1), "%")

# сколько раз trial_converted (повторная оплата = новый цикл точнее)
conv_per_sub = df[df["event_name"]=="trial_converted"].groupby("sub_id").size()
print("\nс >1 trial_converted:", (conv_per_sub>1).sum())

print("\nраспределение числа trial_started на подписку:")
print(trials_per_sub.value_counts().sort_index().head(10))