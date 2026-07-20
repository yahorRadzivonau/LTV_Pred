"""
Local-only comparison of the MAP forecast with a SQL-style empirical benchmark
for the 2026-05-04..2026-05-10 cohort.

No BigQuery export is required. Both the empirical fact and MAP use exactly the
same local first-payer subscription IDs.

Cohort approximation from local golden data:
  1. Stripe only, when payment_provider/provider is available.
  2. Invinci only, when app_id is available.
  3. Subscription start = earliest trial_started event; if missing, earliest
     event for the subscription.
  4. $9.99 weekly plan = exact Stripe price_id when available; otherwise infer
     from observed price_amount values. Inferred-price exclusions are reported.
  5. Denominator for retention/LTV comparison = first payers present in both
     golden paid events and golden_all_se_training.parquet.

Run:
    .venv/Scripts/python.exe compare_map_to_local_sql_style_may_04_10.py

Outputs:
    reports/local_sql_style_vs_map_may_04_10/
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from core import common, map_model
from core.common import HMAX


COHORT_START = pd.Timestamp("2026-05-04T00:00:00Z")
COHORT_END_EXCLUSIVE = pd.Timestamp("2026-05-11T00:00:00Z")
COHORT_WEEK = pd.Timestamp("2026-05-04")
SNAPSHOT_TS = pd.Timestamp("2026-07-07T00:00:00Z")
WEEKS_FED = [1, 2, 3, 4, 5, 6, 7, 8]

TARGET_STRIPE_PRICE_ID = "price_1RVWUuJzVYkL7XCuWyUpC9bH"
TARGET_WEEKLY_PRICE = 9.99
PRICE_TOLERANCE = 0.05
PAID_EVENTS = {"trial_converted", "subscription_renewed"}

GOLDEN_PATH = Path("data/golden/golden_all.parquet")
WEB_MATRIX_PATH = Path("data/golden/golden_all_se_training.parquet")
REPORTS_DIR = Path("reports/web_model/02_map_vs_sql_cohort")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def require_columns(df: pd.DataFrame, columns: set[str], label: str) -> None:
    missing = columns.difference(df.columns)
    if missing:
        raise ValueError(f"{label} is missing required columns: {sorted(missing)}")


def first_existing_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    return next((column for column in candidates if column in df.columns), None)


def load_golden() -> pd.DataFrame:
    if not GOLDEN_PATH.exists():
        raise FileNotFoundError(f"Golden file not found: {GOLDEN_PATH}")
    golden = pd.read_parquet(GOLDEN_PATH).copy()
    require_columns(
        golden,
        {"subscription_id", "event_name", "event_datetime", "price_amount"},
        "golden_all",
    )
    golden["subscription_id"] = golden["subscription_id"].astype("string")
    golden["event_datetime"] = pd.to_datetime(
        golden["event_datetime"], utc=True, errors="coerce"
    )
    golden["price_amount"] = pd.to_numeric(
        golden["price_amount"], errors="coerce"
    )
    golden = golden[
        golden["subscription_id"].notna() & golden["event_datetime"].notna()
    ].copy()
    return golden


def filter_provider_and_app(golden: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str]]:
    """Apply provider/app filters only when those fields exist locally."""
    filtered = golden.copy()
    metadata: dict[str, str] = {}

    provider_col = first_existing_column(
        filtered, ["payment_provider", "provider", "billing_provider"]
    )
    if provider_col:
        provider = filtered[provider_col].astype("string").str.lower()
        filtered = filtered[provider.eq("stripe")].copy()
        metadata["provider_filter"] = f"{provider_col}=stripe"
    else:
        metadata["provider_filter"] = "not applied: provider column unavailable"

    if "app_id" in filtered.columns:
        app = filtered["app_id"].astype("string").str.lower()
        filtered = filtered[app.eq("invinci")].copy()
        metadata["app_filter"] = "app_id=invinci"
    else:
        metadata["app_filter"] = "not applied: app_id unavailable"

    return filtered, metadata


def subscription_start_table(golden: pd.DataFrame) -> pd.DataFrame:
    """
    Approximate SQL customer.subscription.created locally.

    Prefer the normalized trial_started event. Fall back to the earliest known
    event for subscriptions without trial_started.
    """
    first_any = golden.groupby("subscription_id")["event_datetime"].min()
    trial_starts = (
        golden[golden["event_name"].eq("trial_started")]
        .groupby("subscription_id")["event_datetime"]
        .min()
    )
    starts = pd.DataFrame({"first_event_ts": first_any})
    starts["trial_started_ts"] = trial_starts
    starts["subscription_start_ts"] = starts["trial_started_ts"].fillna(
        starts["first_event_ts"]
    )
    starts["start_source"] = np.where(
        starts["trial_started_ts"].notna(), "trial_started", "first_local_event"
    )
    return starts.reset_index()


def choose_price_cohort(
    golden: pd.DataFrame,
    cohort_start_subs: pd.Index,
) -> tuple[pd.Index, pd.DataFrame, str]:
    """
    Select the $9.99 plan using exact price ID when possible. Otherwise infer
    plan price from locally observed amounts.

    In inference mode:
      - if a subscription has any amount >= $5, its most frequent rounded
        full-price amount is treated as recurring-plan price;
      - a subscription with only low paid-trial amounts remains unknown and is
        excluded rather than silently assigned to $9.99 or $11.99.
    """
    cohort_rows = golden[golden["subscription_id"].isin(cohort_start_subs)].copy()
    price_id_col = first_existing_column(
        cohort_rows,
        ["price_id", "stripe_price_id", "subscription_price_id", "plan_price_id"],
    )

    if price_id_col:
        exact_subs = pd.Index(
            cohort_rows.loc[
                cohort_rows[price_id_col].astype("string").eq(TARGET_STRIPE_PRICE_ID),
                "subscription_id",
            ].unique()
        )
        audit = pd.DataFrame({"subscription_id": cohort_start_subs})
        audit["price_filter_method"] = f"exact:{price_id_col}"
        audit["price_class"] = np.where(
            audit["subscription_id"].isin(exact_subs), "9.99", "other_or_unknown"
        )
        audit["in_price_cohort"] = audit["subscription_id"].isin(exact_subs)
        return exact_subs, audit, f"exact Stripe price ID from {price_id_col}"

    # Prefer all local rows with a price, because the normalized start row may
    # already carry the recurring price even before money is captured.
    priced = cohort_rows[
        cohort_rows["price_amount"].notna() & (cohort_rows["price_amount"] > 0)
    ].copy()
    priced["rounded_amount"] = priced["price_amount"].round(2)

    records: list[dict[str, object]] = []
    eligible: list[str] = []
    for sub_id in cohort_start_subs.astype("string"):
        amounts = priced.loc[priced["subscription_id"].eq(sub_id), "rounded_amount"]
        full_prices = amounts[amounts >= 5.0]
        if len(full_prices):
            modes = full_prices.mode()
            inferred = float(modes.iloc[0]) if len(modes) else float(full_prices.iloc[0])
            price_class = f"{inferred:.2f}"
            include = abs(inferred - TARGET_WEEKLY_PRICE) <= PRICE_TOLERANCE
            reason = "mode_of_observed_amounts_ge_5"
        elif len(amounts):
            inferred = np.nan
            price_class = "low_amount_only_unknown_plan"
            include = False
            reason = "cannot distinguish 9.99 vs 11.99 from paid-trial amount only"
        else:
            inferred = np.nan
            price_class = "no_local_price"
            include = False
            reason = "no positive local price observed"

        if include:
            eligible.append(str(sub_id))
        records.append(
            {
                "subscription_id": str(sub_id),
                "price_filter_method": "inferred_from_price_amount",
                "inferred_recurring_price": inferred,
                "price_class": price_class,
                "in_price_cohort": include,
                "price_filter_reason": reason,
            }
        )

    return (
        pd.Index(eligible, dtype="string"),
        pd.DataFrame(records),
        "inferred recurring price from local price_amount",
    )


def local_paid_events(golden: pd.DataFrame, subs: pd.Index) -> pd.DataFrame:
    """
    Only exact $9.99 payments count. Other amounts under the same price_id
    (8.49/5.99/3.50/0.99/...) are discount/promo artifacts per owner confirmation
    (there is no separate $11.99/week plan -- that was $9.99/week + a ~$2/month
    add-on charged separately) -- dropped here, not counted as this plan's revenue.
    """
    paid = golden[
        golden["subscription_id"].isin(subs)
        & golden["event_name"].isin(PAID_EVENTS)
        & golden["price_amount"].notna()
        & (golden["price_amount"] - TARGET_WEEKLY_PRICE).abs().le(PRICE_TOLERANCE)
        & (golden["event_datetime"] <= SNAPSHOT_TS)
    ].copy()

    # Prefer a real event/invoice ID if present. Otherwise protect against exact
    # duplicated normalized rows with the fields available in golden.
    event_id_col = first_existing_column(
        paid,
        ["invoice_id", "event_id", "provider_event_id", "transaction_id"],
    )
    if event_id_col:
        paid = paid.sort_values("event_datetime").drop_duplicates(event_id_col)
    else:
        paid = paid.drop_duplicates(
            ["subscription_id", "event_datetime", "event_name", "price_amount"]
        )

    paid = paid.sort_values(["subscription_id", "event_datetime"])
    paid["rebill_number"] = paid.groupby("subscription_id").cumcount()
    paid["payment_number"] = paid["rebill_number"] + 1
    return paid


def load_web_matrix() -> pd.DataFrame:
    if not WEB_MATRIX_PATH.exists():
        raise FileNotFoundError(f"Web se_training matrix not found: {WEB_MATRIX_PATH}")
    mx = pd.read_parquet(WEB_MATRIX_PATH).copy()
    require_columns(
        mx,
        {
            "sub_id",
            "step_k",
            "outcome",
            "weeks_obs",
            "pay_ts",
            "billing_day_of_month",
            "geo",
            "media_source",
        },
        "golden_all_se_training",
    )
    mx["sub_id"] = mx["sub_id"].astype("string")
    mx["pay_ts"] = pd.to_datetime(mx["pay_ts"], utc=True, errors="coerce")
    mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)
    mx["died"] = 1 - mx["survived"]
    return mx


def visible_at_week_n(matrix: pd.DataFrame, n: int) -> pd.DataFrame:
    """Keep the existing MAP walk-forward input convention unchanged."""
    first_pay = matrix.groupby("sub_id")["pay_ts"].transform("min")
    age_weeks = (matrix["pay_ts"] - first_pay).dt.total_seconds() / (7 * 86400)
    eligible_subs = matrix.loc[matrix["weeks_obs"] >= n, "sub_id"].unique()
    visible = matrix[
        matrix["sub_id"].isin(eligible_subs) & (age_weeks < n)
    ].copy()
    visible["weeks_obs"] = n
    return visible


def sql_style_summary(
    paid: pd.DataFrame,
    denominator_subs: pd.Index,
    max_mature_rebill: int,
) -> pd.DataFrame:
    """Reproduce the SQL output on one fixed local first-payer denominator."""
    denominator_subs = pd.Index(denominator_subs.astype("string").unique())
    denominator = len(denominator_subs)
    if denominator == 0:
        raise RuntimeError("First-payer denominator is empty")

    paid = paid[paid["subscription_id"].isin(denominator_subs)].copy()
    max_present = int(paid["rebill_number"].max()) if len(paid) else 0
    max_step = min(max_present, max_mature_rebill)

    output: list[dict[str, float | int]] = []
    cumulative_revenue = 0.0
    for rebill in range(max_step + 1):
        step = paid[paid["rebill_number"].eq(rebill)]
        active_users = int(step["subscription_id"].nunique())
        revenue = float(step["price_amount"].sum())
        cumulative_revenue += revenue
        output.append(
            {
                "rebill_number": rebill,
                "payment_number": rebill + 1,
                "payer_denominator": denominator,
                "active_users": active_users,
                "revenue": revenue,
                "rebill_percent": active_users / denominator,
                "arpu_at_payment": revenue / active_users if active_users else np.nan,
                "cumulative_revenue": cumulative_revenue,
                "payer_ltv": cumulative_revenue / denominator,
            }
        )
    return pd.DataFrame(output)


def raw_map_ltv(predicted_survival: pd.Series, arpu_by_rebill: pd.Series) -> pd.Series:
    """
    MAP[k] is P(reaching payment k+1). Payment 1 has already happened for every
    first payer, so it is included at 100% before applying MAP survival.
    """
    cumulative = float(arpu_by_rebill.loc[0])
    result: dict[int, float] = {0: cumulative}
    for rebill in range(1, HMAX + 1):
        survival = predicted_survival.get(rebill, np.nan)
        amount = arpu_by_rebill.get(rebill, np.nan)
        if pd.notna(survival) and pd.notna(amount):
            cumulative += float(survival) * float(amount)
        result[rebill] = cumulative
    return pd.Series(result, dtype=float)


def fit_web_ios_calibration(
    cohorts: list[dict],
    h_ios: pd.Series,
    reliability_n_threshold: int,
) -> tuple[float, float, int]:
    """
    Weighted log-linear calibration of the web hazard curve against the iOS
    map-model h_base: h_web_raw[k] ~= h_ios[k] * exp(alpha + beta*(k-1)).

    cohorts: list of {"max_mature_rebill": int, "N": int, "fact": DataFrame
             indexed by rebill_number with an "active_users" column} -- one
             entry per web cohort week, already filtered to cohorts that
             passed MIN_FIRST_PAYERS/MIN_MATURE_REBILL.
    h_ios: the iOS map-model's h_base series (index = week k).
    reliability_n_threshold: minimum N_at_risk for a week k to enter the fit.

    Returns (alpha, beta, k_max_reliable). Unified 2026-07 (Phase B) from two
    near-identical copies (build_tables.py/build_triple_report_fixed.py vs
    reconcile.py) that differed only in reliable-subset indexing style and
    whether k_max_reliable was cast to int -- canon here is .loc[mask]
    indexing + explicit int(), numerically verified identical across all
    three call sites (alpha=-0.3933196671, beta=0.0674853235,
    k_max_reliable=11).
    """
    max_k = max(c["max_mature_rebill"] for c in cohorts)
    rows = []
    for k in range(1, max_k + 1):
        at_risk, died = 0, 0
        for c in cohorts:
            if c["max_mature_rebill"] < k:
                continue
            fact = c["fact"]
            prev = c["N"] if k == 1 else fact.loc[k - 1, "active_users"]
            cur = fact.loc[k, "active_users"] if k in fact.index else np.nan
            if pd.isna(cur):
                continue
            at_risk += prev
            died += (prev - cur)
        h = died / at_risk if at_risk else np.nan
        rows.append({"k": k, "N_at_risk": at_risk, "h_web_raw": h})
    h_table = pd.DataFrame(rows).set_index("k")
    h_table["h_ios"] = [h_ios.get(k, np.nan) for k in h_table.index]
    h_table["reliable"] = h_table["N_at_risk"] >= reliability_n_threshold

    rel_mask = h_table["reliable"]
    k_max_reliable = int(h_table.index[rel_mask].max())
    rel = h_table.loc[rel_mask].copy()

    rel["log_ratio"] = np.log(rel["h_web_raw"] / rel["h_ios"])
    X = np.vstack([np.ones(len(rel)), (rel.index - 1).values]).T
    w = rel["N_at_risk"].values
    alpha, beta = np.linalg.lstsq(X * np.sqrt(w)[:, None], rel["log_ratio"].values * np.sqrt(w), rcond=None)[0]
    return alpha, beta, k_max_reliable


def main() -> None:
    golden = load_golden()
    filtered, filter_meta = filter_provider_and_app(golden)
    if filtered.empty:
        raise RuntimeError("No rows remain after local provider/app filters")

    starts = subscription_start_table(filtered)
    cohort_starts = starts[
        starts["subscription_start_ts"].ge(COHORT_START)
        & starts["subscription_start_ts"].lt(COHORT_END_EXCLUSIVE)
    ].copy()
    if cohort_starts.empty:
        raise RuntimeError("No local subscription starts found for 2026-05-04..2026-05-10")

    cohort_start_subs = pd.Index(cohort_starts["subscription_id"].unique())
    price_subs, price_audit, price_method = choose_price_cohort(
        filtered, cohort_start_subs
    )
    if len(price_subs) == 0:
        raise RuntimeError(
            "No $9.99 subscriptions could be selected locally. Review the generated "
            "price audit logic or expose a Stripe price_id column in golden_all."
        )

    paid = local_paid_events(filtered, price_subs)
    first_payers = pd.Index(
        paid.loc[paid["rebill_number"].eq(0), "subscription_id"].unique(),
        dtype="string",
    )
    if len(first_payers) == 0:
        raise RuntimeError("No first payers found in the local $9.99 cohort")

    web_all = load_web_matrix()
    matrix_subs = pd.Index(web_all["sub_id"].dropna().unique(), dtype="string")
    common_payers = first_payers.intersection(matrix_subs)
    missing_from_matrix = first_payers.difference(matrix_subs)
    if len(common_payers) == 0:
        raise RuntimeError("No overlap between local first payers and se_training")

    # Original SQL maturity rule, frozen to the same snapshot as se_training.
    cohort_age_weeks = int(
        (SNAPSHOT_TS.tz_localize(None) - COHORT_WEEK).days // 7
    )
    max_mature_rebill = max(0, cohort_age_weeks - 2)

    full_local_fact = sql_style_summary(paid, first_payers, max_mature_rebill)
    common_fact = sql_style_summary(paid, common_payers, max_mature_rebill)

    cohort_membership = cohort_starts.merge(
        price_audit, on="subscription_id", how="left"
    )
    cohort_membership["is_first_payer"] = cohort_membership[
        "subscription_id"
    ].isin(first_payers)
    cohort_membership["present_in_se_training"] = cohort_membership[
        "subscription_id"
    ].isin(matrix_subs)
    cohort_membership["in_common_comparison"] = cohort_membership[
        "subscription_id"
    ].isin(common_payers)

    coverage = pd.DataFrame(
        [
            {
                "local_start_subscriptions_before_price_filter": len(cohort_start_subs),
                "local_9_99_start_subscriptions": len(price_subs),
                "local_9_99_first_payers": len(first_payers),
                "trial_to_first_pay_rate": len(first_payers) / len(price_subs),
                "first_payers_present_in_se_training": len(common_payers),
                "first_payers_missing_from_se_training": len(missing_from_matrix),
                "matrix_coverage_of_first_payers": len(common_payers) / len(first_payers),
                "cohort_age_weeks_at_snapshot": cohort_age_weeks,
                "max_mature_rebill_by_sql_rule": max_mature_rebill,
                "price_selection_method": price_method,
                **filter_meta,
            }
        ]
    )

    coverage.to_csv(REPORTS_DIR / "cohort_coverage.csv", index=False)
    cohort_membership.to_csv(REPORTS_DIR / "cohort_membership_audit.csv", index=False)
    paid.to_csv(REPORTS_DIR / "local_numbered_payments.csv", index=False)
    full_local_fact.to_csv(REPORTS_DIR / "local_full_payer_fact.csv", index=False)
    common_fact.to_csv(REPORTS_DIR / "common_cohort_fact.csv", index=False)
    if len(missing_from_matrix):
        pd.DataFrame({"subscription_id": missing_from_matrix}).to_csv(
            REPORTS_DIR / "first_payers_missing_from_se_training.csv", index=False
        )

    cohort_mx = web_all[web_all["sub_id"].isin(common_payers)].copy()

    # Frozen reference MAP model, unchanged.
    ios_mx = common.load_matrix()
    ios_apps_info = common.select_apps(ios_mx)
    state = map_model.fit(ios_mx, ios_apps_info)

    # Empirical ARPU by ordinal payment on the exact common cohort. Extend the
    # last observed weekly price through the forecast horizon.
    arpu_by_rebill = common_fact.set_index("rebill_number")["arpu_at_payment"]
    arpu_by_rebill = arpu_by_rebill.reindex(range(0, HMAX + 1)).ffill()
    if pd.isna(arpu_by_rebill.loc[0]):
        raise RuntimeError("First-payment ARPU is unavailable")

    comparison = pd.DataFrame({"rebill_number": range(0, HMAX + 1)})
    comparison["payment_number"] = comparison["rebill_number"] + 1
    comparison = comparison.merge(
        common_fact[
            [
                "rebill_number",
                "payer_denominator",
                "active_users",
                "rebill_percent",
                "arpu_at_payment",
                "payer_ltv",
            ]
        ],
        on="rebill_number",
        how="left",
    )

    prediction_rows: list[dict[str, float | int]] = []
    ltv_curves: dict[int, pd.Series] = {}

    for weeks in WEEKS_FED:
        visible = visible_at_week_n(cohort_mx, weeks)
        if visible.empty:
            print(f"wk{weeks}: no eligible common-cohort rows; skipped")
            continue

        pred_survival = map_model.predict(state, visible, weeks)
        pred_ltv = raw_map_ltv(pred_survival, arpu_by_rebill)

        # Weekly product: MAP index/rebill k corresponds to payment k+1.
        anchor = common_fact[common_fact["rebill_number"].eq(weeks)]
        if anchor.empty:
            print(
                f"wk{weeks}: SQL-style fact not mature at rebill {weeks}; "
                "raw MAP is saved, anchored curve skipped"
            )
            scale = np.nan
            anchored_ltv = pd.Series(np.nan, index=pred_ltv.index)
            anchor_fact_ltv = np.nan
            raw_anchor_ltv = np.nan
        else:
            anchor_fact_ltv = float(anchor.iloc[0]["payer_ltv"])
            raw_anchor_ltv = float(pred_ltv.loc[weeks])
            scale = anchor_fact_ltv / raw_anchor_ltv if raw_anchor_ltv > 0 else 1.0
            anchored_ltv = pred_ltv * scale
            ltv_curves[weeks] = anchored_ltv

        mature_eval = common_fact[common_fact["rebill_number"].between(1, weeks)]
        joined = mature_eval.assign(
            map_survival_raw=mature_eval["rebill_number"].map(pred_survival)
        ).dropna(subset=["map_survival_raw"])
        mae = (
            float(np.mean(np.abs(joined["map_survival_raw"] - joined["rebill_percent"])))
            if len(joined)
            else np.nan
        )

        prediction_rows.append(
            {
                "vintage_weeks_fed": weeks,
                "common_payer_denominator": len(common_payers),
                "visible_rows": len(visible),
                "visible_subscribers": visible["sub_id"].nunique(),
                "anchor_rebill_number": weeks,
                "anchor_fact_payer_ltv": anchor_fact_ltv,
                "raw_map_payer_ltv_at_anchor": raw_anchor_ltv,
                "ltv_anchor_scale": scale,
                "raw_map_payer_ltv_wk52": float(pred_ltv.loc[HMAX]),
                "anchored_map_payer_ltv_wk52": (
                    float(anchored_ltv.loc[HMAX]) if pd.notna(scale) else np.nan
                ),
                "survival_mae_through_anchor": mae,
            }
        )

        comparison[f"map_survival_raw_wk{weeks}"] = comparison[
            "rebill_number"
        ].map(pred_survival)
        comparison[f"map_payer_ltv_raw_wk{weeks}"] = comparison[
            "rebill_number"
        ].map(pred_ltv)
        comparison[f"map_payer_ltv_anchored_wk{weeks}"] = comparison[
            "rebill_number"
        ].map(anchored_ltv)

    predictions = pd.DataFrame(prediction_rows)
    predictions.to_csv(REPORTS_DIR / "map_vintage_predictions.csv", index=False)
    comparison.to_csv(REPORTS_DIR / "local_fact_vs_map.csv", index=False)

    # Retention chart.
    fig, ax = plt.subplots(figsize=(10, 7))
    retention_fact = common_fact[common_fact["rebill_number"] >= 1]
    ax.plot(
        retention_fact["rebill_number"],
        retention_fact["rebill_percent"],
        marker="o",
        linewidth=2.5,
        label="Local SQL-style fact (common first payers)",
    )
    for weeks in WEEKS_FED:
        column = f"map_survival_raw_wk{weeks}"
        if column in comparison:
            ax.plot(
                comparison["rebill_number"],
                comparison[column],
                linestyle="--",
                linewidth=1.6,
                label=f"MAP raw, fed {weeks} week(s)",
            )
    ax.set_xlabel("Rebill number (1 = second successful payment)")
    ax.set_ylabel("Share of first payers")
    ax.set_xlim(1, HMAX)
    ax.set_ylim(0, 1.05)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    ax.set_title(
        "Local $9.99 cohort started 2026-05-04..2026-05-10\n"
        "SQL-style empirical retention vs MAP — identical payer IDs"
    )
    fig.tight_layout()
    fig.savefig(REPORTS_DIR / "retention_local_fact_vs_map.png", dpi=160)
    plt.close(fig)

    # LTV chart.
    fig, ax = plt.subplots(figsize=(10, 7))
    ax.plot(
        common_fact["rebill_number"],
        common_fact["payer_ltv"],
        marker="o",
        linewidth=2.5,
        label="Local SQL-style factual payer LTV",
    )
    for weeks, curve in ltv_curves.items():
        xs = list(range(weeks, HMAX + 1))
        ys = [curve.loc[x] for x in xs]
        ax.plot(
            xs,
            ys,
            linestyle="--",
            linewidth=1.7,
            label=f"MAP anchored at rebill {weeks}",
        )
        ax.annotate(
            f"${ys[-1]:.2f}",
            xy=(HMAX, ys[-1]),
            xytext=(-4, 4),
            textcoords="offset points",
            ha="right",
            fontsize=8,
        )
    for _, row in common_fact.iterrows():
        ax.annotate(
            f"${row['payer_ltv']:.2f}",
            (row["rebill_number"], row["payer_ltv"]),
            xytext=(0, 7),
            textcoords="offset points",
            ha="center",
            fontsize=7,
        )
    ax.set_xlabel("Rebill number (0 = first successful payment)")
    ax.set_ylabel("Cumulative LTV per first payer (USD)")
    ax.set_xlim(0, HMAX)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    ax.set_title(
        "Local $9.99 cohort started 2026-05-04..2026-05-10\n"
        "SQL-style factual payer LTV vs MAP 52-rebill forecast"
    )
    fig.tight_layout()
    fig.savefig(REPORTS_DIR / "ltv_local_fact_vs_map.png", dpi=160)
    plt.close(fig)

    print("\n=== Local cohort coverage ===")
    print(coverage.to_string(index=False))
    print("\n=== Common-cohort SQL-style fact ===")
    print(common_fact.to_string(index=False, float_format=lambda x: f"{x:.6f}"))
    print("\n=== MAP vintage predictions ===")
    print(predictions.to_string(index=False, float_format=lambda x: f"{x:.6f}"))
    print(f"\nWrote reports to: {REPORTS_DIR}")


if __name__ == "__main__":
    main()
