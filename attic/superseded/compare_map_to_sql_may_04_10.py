"""
Compare the MAP prediction with a SQL-compatible empirical benchmark on exactly
one common set of subscriptions.

The SQL result is NOT treated as a second predictive model. It is the factual
cohort curve used to validate MAP.

Workflow
--------
1. Run export_sql_cohort_may_04_10.sql in BigQuery.
2. Save its result to data/sql/sql_cohort_may_04_10_rows.csv.
3. Run:
       .venv/Scripts/python.exe compare_map_to_sql_may_04_10.py

Outputs are written under reports/sql_vs_map_may_04_10/.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from models import common, map_model


HMAX = 52
SNAPSHOT_DATE = pd.Timestamp("2026-07-07T00:00:00Z")
COHORT_WEEK = pd.Timestamp("2026-05-04")
WEEKS_FED = [1, 2, 4, 8]

SQL_EXPORT = Path("data/sql/sql_cohort_may_04_10_rows.csv")
WEB_MATRIX = Path("data/golden/golden_all_se_training.parquet")
REPORTS_DIR = Path("reports/sql_vs_map_may_04_10")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def require_columns(df: pd.DataFrame, required: set[str], label: str) -> None:
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"{label} is missing required columns: {sorted(missing)}")


def load_sql_export(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"SQL export not found: {path}\n"
            "Run export_sql_cohort_may_04_10.sql in BigQuery and save its result "
            "to this path."
        )

    if path.suffix.lower() == ".parquet":
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path)

    require_columns(
        df,
        {
            "sub_id",
            "subscription_start_date",
            "cohort_week",
            "amount",
            "rebill_number",
        },
        "SQL export",
    )

    df = df.copy()
    df["sub_id"] = df["sub_id"].astype("string")
    df["subscription_start_date"] = pd.to_datetime(
        df["subscription_start_date"], errors="coerce"
    )
    df["cohort_week"] = pd.to_datetime(df["cohort_week"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["rebill_number"] = pd.to_numeric(
        df["rebill_number"], errors="coerce"
    ).astype("Int64")

    # One row per real invoice/payment. The SQL query already deduplicates by
    # invoice_id, but this protects the comparison from accidental CSV duplication.
    if "invoice_id" in df.columns:
        paid = df[df["invoice_id"].notna()].drop_duplicates("invoice_id")
        no_pay = df[df["invoice_id"].isna()].drop_duplicates("sub_id")
        df = pd.concat([paid, no_pay], ignore_index=True, sort=False)
    else:
        paid = df[df["rebill_number"].notna()].drop_duplicates(
            ["sub_id", "rebill_number"]
        )
        no_pay = df[df["rebill_number"].isna()].drop_duplicates("sub_id")
        df = pd.concat([paid, no_pay], ignore_index=True, sort=False)

    return df


def load_web_matrix(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Web se_training matrix not found: {path}")
    mx = pd.read_parquet(path).copy()
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
        "Web se_training matrix",
    )
    mx["sub_id"] = mx["sub_id"].astype("string")
    mx["pay_ts"] = pd.to_datetime(mx["pay_ts"], utc=True, errors="coerce")
    mx["survived"] = mx["outcome"].isin(["renewed", "recovered"]).astype(int)
    mx["died"] = 1 - mx["survived"]
    return mx


def visible_at_week_n(matrix: pd.DataFrame, n: int) -> pd.DataFrame:
    """Reproduce the existing walk-forward input without changing MAP itself."""
    first_pay = matrix.groupby("sub_id")["pay_ts"].transform("min")
    age_weeks = (matrix["pay_ts"] - first_pay).dt.total_seconds() / (7 * 86400)
    eligible_subs = matrix.loc[matrix["weeks_obs"] >= n, "sub_id"].unique()
    visible = matrix[
        matrix["sub_id"].isin(eligible_subs) & (age_weeks < n)
    ].copy()
    visible["weeks_obs"] = n
    return visible


def sql_summary(rows: pd.DataFrame, denominator_subs: pd.Index) -> pd.DataFrame:
    """Build the SQL-style factual curve on a supplied, fixed denominator."""
    denominator_subs = pd.Index(denominator_subs.astype("string").unique())
    denominator = len(denominator_subs)
    if denominator == 0:
        raise RuntimeError("Common first-payer denominator is empty")

    paid = rows[
        rows["sub_id"].isin(denominator_subs)
        & rows["rebill_number"].notna()
        & rows["amount"].notna()
        & (rows["amount"] > 0)
    ].copy()
    paid["rebill_number"] = paid["rebill_number"].astype(int)

    # Original SQL maturity convention, but frozen to the se_training snapshot.
    cohort_age_weeks = int((SNAPSHOT_DATE.tz_localize(None) - COHORT_WEEK).days // 7)
    max_observed_rebill = int(paid["rebill_number"].max()) if len(paid) else 0
    max_mature_rebill = min(max_observed_rebill, cohort_age_weeks - 2)

    rows_out: list[dict[str, float | int]] = []
    cumulative_revenue = 0.0
    for rebill in range(0, max_mature_rebill + 1):
        step = paid[paid["rebill_number"] == rebill]
        active_users = int(step["sub_id"].nunique())
        revenue = float(step["amount"].sum())
        cumulative_revenue += revenue
        rows_out.append(
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
                "is_mature_by_sql_rule": True,
            }
        )
    return pd.DataFrame(rows_out)


def raw_map_ltv(
    predicted_survival: pd.Series,
    arpu_by_rebill: pd.Series,
) -> pd.Series:
    """
    MAP index k means P(reaching payment k+1): hazard at step_k describes death
    after payment k. Therefore:
      rebill 0 / payment 1 is already observed for every payer;
      MAP[1] is multiplied by ARPU of payment 2;
      MAP[2] is multiplied by ARPU of payment 3; etc.
    """
    out: dict[int, float] = {}
    cumulative = float(arpu_by_rebill.loc[0])
    out[0] = cumulative
    for rebill in range(1, HMAX + 1):
        survival = predicted_survival.get(rebill, np.nan)
        amount = arpu_by_rebill.get(rebill, np.nan)
        if pd.notna(survival) and pd.notna(amount):
            cumulative += float(survival) * float(amount)
        out[rebill] = cumulative
    return pd.Series(out, dtype=float)


def main() -> None:
    sql_rows = load_sql_export(SQL_EXPORT)
    web_all = load_web_matrix(WEB_MATRIX)

    all_sql_starts = pd.Index(sql_rows["sub_id"].dropna().unique())
    sql_first_payers = pd.Index(
        sql_rows.loc[sql_rows["rebill_number"] == 0, "sub_id"].dropna().unique()
    )
    matrix_subs = pd.Index(web_all["sub_id"].dropna().unique())
    common_payers = sql_first_payers.intersection(matrix_subs)
    missing_from_matrix = sql_first_payers.difference(matrix_subs)

    coverage = pd.DataFrame(
        [
            {
                "sql_all_started_subscriptions": len(all_sql_starts),
                "sql_first_payers": len(sql_first_payers),
                "sql_trial_to_first_pay_rate": (
                    len(sql_first_payers) / len(all_sql_starts)
                    if len(all_sql_starts)
                    else np.nan
                ),
                "first_payers_present_in_web_matrix": len(common_payers),
                "first_payers_missing_from_web_matrix": len(missing_from_matrix),
                "matrix_coverage_of_sql_first_payers": (
                    len(common_payers) / len(sql_first_payers)
                    if len(sql_first_payers)
                    else np.nan
                ),
            }
        ]
    )
    coverage.to_csv(REPORTS_DIR / "cohort_coverage.csv", index=False)

    if len(common_payers) == 0:
        raise RuntimeError(
            "No overlap between SQL first payers and golden_all_se_training. "
            "The two data sources cannot yet be compared."
        )

    if len(missing_from_matrix):
        pd.DataFrame({"sub_id": missing_from_matrix}).to_csv(
            REPORTS_DIR / "sql_first_payers_missing_from_matrix.csv", index=False
        )

    # Two factual summaries:
    # 1) full SQL payer cohort, reproducing the business query;
    # 2) common cohort, used for the actual MAP comparison.
    full_sql_fact = sql_summary(sql_rows, sql_first_payers)
    common_fact = sql_summary(sql_rows, common_payers)
    full_sql_fact.to_csv(REPORTS_DIR / "sql_full_payer_fact.csv", index=False)
    common_fact.to_csv(REPORTS_DIR / "common_cohort_fact.csv", index=False)

    cohort_mx = web_all[web_all["sub_id"].isin(common_payers)].copy()

    # Train the frozen MAP state on the same iOS reference matrix as before.
    ios_mx = common.load_matrix()
    ios_apps_info = common.select_apps(ios_mx)
    state = map_model.fit(ios_mx, ios_apps_info)

    # Empirical price per ordinal payment on the exact common cohort.
    arpu_by_rebill = common_fact.set_index("rebill_number")["arpu_at_payment"]
    arpu_by_rebill = arpu_by_rebill.reindex(range(0, HMAX + 1)).ffill()
    if pd.isna(arpu_by_rebill.loc[0]):
        raise RuntimeError("First-payment ARPU is unavailable")

    comparison = common_fact[
        [
            "rebill_number",
            "payment_number",
            "payer_denominator",
            "active_users",
            "rebill_percent",
            "arpu_at_payment",
            "payer_ltv",
        ]
    ].copy()

    prediction_rows = []
    curves_for_plot: dict[int, pd.Series] = {}

    for weeks in WEEKS_FED:
        visible = visible_at_week_n(cohort_mx, weeks)
        if visible.empty:
            print(f"wk{weeks}: no eligible common-cohort rows; skipped")
            continue

        pred_survival = map_model.predict(state, visible, weeks)
        pred_ltv = raw_map_ltv(pred_survival, arpu_by_rebill)

        anchor_row = common_fact[common_fact["rebill_number"] == weeks]
        if anchor_row.empty:
            print(f"wk{weeks}: SQL fact is not mature at rebill {weeks}; skipped")
            continue

        anchor_fact_ltv = float(anchor_row.iloc[0]["payer_ltv"])
        raw_anchor_ltv = float(pred_ltv.loc[weeks])
        scale = anchor_fact_ltv / raw_anchor_ltv if raw_anchor_ltv > 0 else 1.0

        # Preserve the old chart methodology: cumulative model LTV is scaled to
        # the cohort's observed factual LTV at the vintage anchor.
        anchored_ltv = pred_ltv * scale
        curves_for_plot[weeks] = anchored_ltv

        # Direct, unanchored survival comparison against SQL fact.
        mature_eval = common_fact[common_fact["rebill_number"].between(1, weeks)]
        joined = mature_eval.assign(
            map_survival_raw=mature_eval["rebill_number"].map(pred_survival)
        ).dropna(subset=["map_survival_raw"])
        mae = float(
            np.mean(np.abs(joined["map_survival_raw"] - joined["rebill_percent"]))
        ) if len(joined) else np.nan

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
                "anchored_map_payer_ltv_wk52": float(anchored_ltv.loc[HMAX]),
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
    comparison.to_csv(REPORTS_DIR / "sql_fact_vs_map.csv", index=False)

    # Retention comparison chart: SQL fact vs raw MAP survival.
    fig, ax = plt.subplots(figsize=(10, 7))
    retention_fact = common_fact[common_fact["rebill_number"] >= 1]
    ax.plot(
        retention_fact["rebill_number"],
        retention_fact["rebill_percent"],
        marker="o",
        linewidth=2.5,
        label="SQL fact (common first-payer cohort)",
    )
    for weeks in WEEKS_FED:
        col = f"map_survival_raw_wk{weeks}"
        if col in comparison:
            ax.plot(
                comparison["rebill_number"],
                comparison[col],
                linestyle="--",
                linewidth=1.6,
                label=f"MAP raw, fed {weeks} week(s)",
            )
    ax.set_xlabel("Rebill number (1 = second successful payment)")
    ax.set_ylabel("Share of first payers")
    ax.set_ylim(0, 1.05)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    ax.set_title(
        "Stripe $9.99 cohort created 2026-05-04..2026-05-10\n"
        "SQL empirical retention vs MAP prediction — identical payer denominator"
    )
    fig.tight_layout()
    fig.savefig(REPORTS_DIR / "retention_sql_fact_vs_map.png", dpi=160)
    plt.close(fig)

    # LTV comparison chart: SQL factual payer LTV vs anchored MAP projections.
    fig, ax = plt.subplots(figsize=(10, 7))
    ax.plot(
        common_fact["rebill_number"],
        common_fact["payer_ltv"],
        marker="o",
        linewidth=2.5,
        label="SQL fact payer LTV",
    )
    for weeks, curve in curves_for_plot.items():
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
        "Stripe $9.99 cohort created 2026-05-04..2026-05-10\n"
        "SQL factual payer LTV vs MAP 52-rebill projection"
    )
    fig.tight_layout()
    fig.savefig(REPORTS_DIR / "ltv_sql_fact_vs_map.png", dpi=160)
    plt.close(fig)

    print("\n=== Cohort coverage ===")
    print(coverage.to_string(index=False))
    print("\n=== Common-cohort SQL fact ===")
    print(common_fact.to_string(index=False, float_format=lambda x: f"{x:.6f}"))
    print("\n=== MAP vintage predictions ===")
    print(predictions.to_string(index=False, float_format=lambda x: f"{x:.6f}"))
    print(f"\nWrote reports to: {REPORTS_DIR}")


if __name__ == "__main__":
    main()
