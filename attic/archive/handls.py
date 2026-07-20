import pandas as pd, numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss
from sklearn.preprocessing import OneHotEncoder
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline

mx = pd.read_parquet(r"C:\Users\yahor\PycharmProjects\LTV\se_training.parquet")

# таргет: выжил на этой ступеньке
mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)

# --- ГЛАВНОЕ: step как КАТЕГОРИЯ (1..12 по отдельности, дальше "13+") ---
mx["step_bin"] = mx["step_k"].clip(upper=13).astype(int).astype(str)
mx["step_bin"] = mx["step_bin"].replace("13", "13+")

# топ-категории, мелочь -> "other"
for col in ["geo", "media_source", "attribution_source", "app_id"]:
    top = mx[col].value_counts().nlargest(30).index
    mx[col] = mx[col].where(mx[col].isin(top), "other")

mx["trial_days"] = mx["trial_days"].fillna(-1)

# step_k больше НЕ числовой — он в step_bin (категория)
num = ["billing_day_of_month", "trial_days"]
cat = ["step_bin", "geo", "media_source", "attribution_source", "plan_interval", "app_id", "cohort_month"]
X = mx[num + cat]
y = mx["survived"]

Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, random_state=42, stratify=y)

pre = ColumnTransformer([
    ("c", OneHotEncoder(handle_unknown="ignore", min_frequency=50), cat),
    ("n", "passthrough", num),
])
model = Pipeline([("pre", pre),
                  ("lr", LogisticRegression(max_iter=1000, C=1.0))])
model.fit(Xtr, ytr)

p = model.predict_proba(Xte)[:, 1]
base = yte.mean()
print(f"baseline (всегда 'выжил'): acc={max(base,1-base):.3f}")
print(f"AUC:       {roc_auc_score(yte, p):.4f}")
print(f"log-loss:  {log_loss(yte, p):.4f}  (baseline {log_loss(yte, np.full_like(p, base)):.4f})")
print(f"Brier:     {brier_score_loss(yte, p):.4f}")

# калибровка
bins = pd.qcut(p, 10, duplicates="drop")
cal = pd.DataFrame({"pred": p, "real": yte.values, "bin": bins}).groupby("bin", observed=True).agg(
    pred=("pred", "mean"), real=("real", "mean"), n=("real", "size"))
print("\nкалибровка (pred должно ≈ real):")
print(cal.round(3).to_string())

# --- веса по ступенькам: вот тут увидим, что step реально заработал ---
feat = model.named_steps["pre"].get_feature_names_out()
coef = model.named_steps["lr"].coef_[0]
w = pd.Series(coef, index=feat)
print("\nвеса по ступенькам (step_bin) — должны падать от 1 к старшим:")
print(w[[f for f in feat if "step_bin" in f]].sort_index().round(3).to_string())