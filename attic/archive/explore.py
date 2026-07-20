import pyarrow.parquet as pq
import pandas as pd

# читаем parquet через Arrow
table = pq.read_table(r"C:\Users\yahor\PycharmProjects\LTV\ios_events.parquet")

# конвертируем в DataFrame, игнорируя старые pandas metadata
df = table.to_pandas(ignore_metadata=True)

# приводим даты к нормальному виду
df["event_datetime"] = pd.to_datetime(df["event_datetime"], utc=True)
df["install_date"] = pd.to_datetime(df["install_date"])

print("форма:", df.shape)

print("\nсобытия:")
print(df["event_name"].value_counts())

print("\nпропуски по колонкам:")
print(df.isna().sum())

print(
    "\nдиапазон дат:",
    df["event_datetime"].min(),
    "→",
    df["event_datetime"].max()
)

print("\nуникальных юзеров:", df["customer_user_id"].nunique())
print("уникальных аппов:", df["app_id"].nunique())

# пример истории одного пользователя
user_id = df["customer_user_id"].iloc[0]

print("\nпример одного пользователя:")
print(
    df[df["customer_user_id"] == user_id]
    .sort_values("event_datetime")[
        ["event_name", "event_datetime", "app_id"]
    ]
)