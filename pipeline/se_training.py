from pathlib import Path
import pyarrow.parquet as pq
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "ios_events.parquet"
OUT = ROOT / "data" / "se_training.parquet"

PAID      = {"trial_converted", "subscription_renewed"}
DUNNING   = {"billing_issue_detected", "entered_grace_period"}
VOLUNTARY = {"subscription_cancelled", "auto_renew_off", "auto_renew_off_subscription"}
RETURN_WINDOW_DAYS = 60   # платёж в этом окне = продолжение той же цепочки (отложенный возврат)

df = pq.read_table(SRC).to_pandas(ignore_metadata=True)
df = df[df["customer_user_id"].notna()].copy()
df["event_datetime"] = pd.to_datetime(df["event_datetime"], utc=True)
df["sub_id"] = df["customer_user_id"] + "|" + df["app_id"]
df = df.sort_values(["sub_id", "event_datetime"]).reset_index(drop=True)
T = df["event_datetime"].max()
print("snapshot:", T, "| строк:", len(df))

# лестница платежей с дедупом ~3 дня
paid = df[df["event_name"].isin(PAID)][["sub_id", "event_datetime"]].copy()
paid = paid.sort_values(["sub_id", "event_datetime"])
gap = paid.groupby("sub_id")["event_datetime"].diff().dt.total_seconds() / 86400
paid = paid[(gap.isna()) | (gap > 3)].copy()
paid["step_k"] = paid.groupby("sub_id").cumcount() + 1
paid["next_paid_ts"] = paid.groupby("sub_id")["event_datetime"].shift(-1)
paid["days_to_next"] = (paid["next_paid_ts"] - paid["event_datetime"]).dt.total_seconds() / 86400
print("ступенек:", len(paid))

# флаги дунинга/отмены в окне между платежом и следующим
ev = df[df["event_name"].isin(DUNNING | VOLUNTARY)][["sub_id", "event_datetime", "event_name"]]
m = ev.merge(paid[["sub_id", "step_k", "event_datetime", "next_paid_ts"]], on="sub_id", suffixes=("_ev", "_step"))
in_win = (m["event_datetime_ev"] > m["event_datetime_step"]) & (
    m["next_paid_ts"].isna() | (m["event_datetime_ev"] <= m["next_paid_ts"]))
m = m[in_win]
flags = m.groupby(["sub_id", "step_k"]).agg(
    had_issue=("event_name", lambda s: s.isin(DUNNING).any()),
    had_vol=("event_name", lambda s: s.isin(VOLUNTARY).any())).reset_index()

steps = paid.merge(flags, on=["sub_id", "step_k"], how="left")
steps[["had_issue", "had_vol"]] = steps[["had_issue", "had_vol"]].fillna(False)

# --- ГЛАВНАЯ ПРАВКА: возврат в окне <=60 дней = продолжение (recovered/renewed),
#     а не обрыв. Следующий платёж "считается", если он есть и в пределах окна ---
def outcome(r):
    has_next_in_window = pd.notna(r["next_paid_ts"]) and r["days_to_next"] <= RETURN_WINDOW_DAYS
    if has_next_in_window:
        # был дунинг ИЛИ задержка >10дн -> это возврат (recovered), иначе чистое продление
        if r["had_issue"] or r["days_to_next"] > 10:
            return "recovered"
        return "renewed"
    if r["had_vol"]:
        return "voluntary_cancel"
    if r["had_issue"]:
        return "billing_issue"
    return "churned"
steps["outcome"] = steps.apply(outcome, axis=1)
steps["state_from"] = "active"

# рычаги с первого события подписки
first = df.groupby("sub_id").agg(
    app_id=("app_id", "first"), geo=("country_code", "first"),
    media_source=("media_source", "first"), attribution_source=("attribution_source", "first"),
    install_date=("install_date", "first"), first_pay=("event_datetime", "min")).reset_index()
for c in ["geo", "media_source", "attribution_source"]:
    first[c] = first[c].fillna("(none)").replace("", "(none)")
first["cohort_month"] = pd.to_datetime(first["install_date"]).dt.to_period("M").astype(str)
first["weeks_obs"] = ((T - first["first_pay"]).dt.total_seconds() / (7*86400))

# интервал по медиане зазора (только разумные, <=60дн, чтобы возвраты не задирали)
gap_all = paid.groupby("sub_id")["event_datetime"].diff().dt.total_seconds() / 86400
gap_norm = gap_all[gap_all <= 60]
med = gap_norm.groupby(paid["sub_id"]).median()
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
first = first.merge(((tr.get("trial_converted") - tr.get("trial_started")).dt.days).rename("trial_days"),
                    on="sub_id", how="left")

mx = steps.merge(first, on="sub_id", how="left")
mx["billing_day_of_month"] = mx["event_datetime"].dt.day
mx["pay_ts"] = mx["event_datetime"]          # время платежа этой ступени (для календарного теста)
mx = mx[["sub_id", "step_k", "state_from", "outcome", "weeks_obs",
         "pay_ts", "days_to_next",
         "app_id", "geo", "media_source", "attribution_source",
         "plan_interval", "trial_days", "cohort_month", "billing_day_of_month"]]
mx.to_parquet(OUT)

print("\nМАТРИЦА:", mx.shape)
print("\nисходы:"); print(mx["outcome"].value_counts())
print("\nдоля выживших по ступеням 1-6 (renewed+recovered, censored знаменатель):")
chk = mx[(mx["step_k"] <= 6) & (mx["weeks_obs"] >= mx["step_k"])]
chk = chk.assign(surv=chk["outcome"].isin(["renewed", "recovered"]))
print(chk.groupby("step_k")["surv"].mean().round(3))