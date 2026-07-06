import pyarrow.parquet as pq
import pandas as pd
import numpy as np

SRC = r"C:\Users\yahor\PycharmProjects\LTV\ios_events.parquet"
OUT = r"C:\Users\yahor\PycharmProjects\LTV\se_training.parquet"

PAID = {"trial_converted", "subscription_renewed"}
DUNNING = {"billing_issue_detected", "entered_grace_period"}
VOLUNTARY = {"subscription_cancelled", "auto_renew_off", "auto_renew_off_subscription"}

# ---------- 1. загрузка + чистка ----------
df = pq.read_table(SRC).to_pandas(ignore_metadata=True)
df = df[df["customer_user_id"].notna()].copy()                       # -14% без ключа
df["event_datetime"] = pd.to_datetime(df["event_datetime"], utc=True)
df["sub_id"] = df["customer_user_id"] + "|" + df["app_id"]           # 1 sub = 1 цикл (переподписки 0.2% игнор)
df = df.sort_values(["sub_id", "event_datetime"]).reset_index(drop=True)
print("после чистки:", df.shape, "| подписок:", df["sub_id"].nunique())

# ---------- 2. лестница платежей с дедупом ~3 дня ----------
paid = df[df["event_name"].isin(PAID)][["sub_id", "event_datetime"]].copy()
paid = paid.sort_values(["sub_id", "event_datetime"])
gap = paid.groupby("sub_id")["event_datetime"].diff().dt.total_seconds() / 86400
paid = paid[(gap.isna()) | (gap > 3)].copy()                          # схлопываем дубль-биллинги
paid["step_k"] = paid.groupby("sub_id").cumcount() + 1
paid["next_paid_ts"] = paid.groupby("sub_id")["event_datetime"].shift(-1)
print("платёжных ступенек всего:", len(paid))

# ---------- 3. флаги дунинга/отмены между платежом и следующим ----------
ev = df[df["event_name"].isin(DUNNING | VOLUNTARY)][["sub_id", "event_datetime", "event_name"]].copy()
# приклеиваем к каждой ступеньке её окно [event_datetime, next_paid_ts)
m = ev.merge(paid[["sub_id", "step_k", "event_datetime", "next_paid_ts"]], on="sub_id", suffixes=("_ev", "_step"))
in_window = (m["event_datetime_ev"] > m["event_datetime_step"]) & (
    m["next_paid_ts"].isna() | (m["event_datetime_ev"] <= m["next_paid_ts"])
)
m = m[in_window]
flags = m.groupby(["sub_id", "step_k"]).agg(
    had_issue=("event_name", lambda s: s.isin(DUNNING).any()),
    had_voluntary=("event_name", lambda s: s.isin(VOLUNTARY).any()),
).reset_index()

steps = paid.merge(flags, on=["sub_id", "step_k"], how="left")
steps[["had_issue", "had_voluntary"]] = steps[["had_issue", "had_voluntary"]].fillna(False)

# ---------- 4. исход на каждой ступеньке ----------
def outcome(r):
    if pd.notna(r["next_paid_ts"]):
        return "recovered" if r["had_issue"] else "renewed"
    if r["had_voluntary"]:
        return "voluntary_cancel"
    if r["had_issue"]:
        return "billing_issue"
    return "churned"          # NB: последняя ступенька незрелых когорт = цензур, отделим при обучении
steps["outcome"] = steps.apply(outcome, axis=1)
steps["state_from"] = "active"

# ---------- 5. рычаги (с первого события подписки) + интервал по зазору ----------
first = df.groupby("sub_id").agg(
    app_id=("app_id", "first"),
    geo=("country_code", "first"),
    media_source=("media_source", "first"),
    attribution_source=("attribution_source", "first"),
    install_date=("install_date", "first"),
).reset_index()
for c in ["geo", "media_source", "attribution_source"]:
    first[c] = first[c].fillna("(none)").replace("", "(none)")
first["cohort_month"] = pd.to_datetime(first["install_date"]).dt.to_period("M").astype(str)

# интервал плана = медиана зазора между платежами
gap_all = paid.groupby("sub_id")["event_datetime"].diff().dt.total_seconds() / 86400
med = gap_all.groupby(paid["sub_id"]).median()
def interval(g):
    if pd.isna(g): return "unknown"
    if 5 <= g <= 10: return "week"
    if 25 <= g <= 35: return "month"
    if g >= 350: return "year"
    return "unknown"
first = first.merge(med.map(interval).rename("plan_interval"), on="sub_id", how="left")
first["plan_interval"] = first["plan_interval"].fillna("unknown")

# trial_days
tr = df[df["event_name"].isin(["trial_started", "trial_converted"])]
tr = tr.pivot_table(index="sub_id", columns="event_name", values="event_datetime", aggfunc="min")
first = first.merge(
    ((tr.get("trial_converted") - tr.get("trial_started")).dt.days).rename("trial_days"),
    on="sub_id", how="left"
)

# ---------- 6. финальная матрица ----------
mx = steps.merge(first, on="sub_id", how="left")
mx["billing_day_of_month"] = mx["event_datetime"].dt.day
mx = mx[[
    "sub_id", "step_k", "state_from", "outcome",
    "app_id", "geo", "media_source", "attribution_source",
    "plan_interval", "trial_days", "cohort_month", "billing_day_of_month",
]]
mx.to_parquet(OUT)

# ---------- проверки глазами ----------
print("\nМАТРИЦА:", mx.shape)
print("\nисходы:"); print(mx["outcome"].value_counts())
print("\nинтервалы:"); print(mx["plan_interval"].value_counts())
print("\nпервые 12 строк:")
print(mx.head(12).to_string(index=False))
print("\nисход по ступенькам 1-6 (доля renewed/recovered = выжил):")
chk = mx[mx["step_k"] <= 6].assign(survived=mx["outcome"].isin(["renewed", "recovered"]))
print(chk.groupby("step_k")["survived"].mean().round(3))