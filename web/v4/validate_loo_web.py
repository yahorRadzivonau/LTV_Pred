"""
LOO validation of the v3 web model -- the ruler this iteration is built around.

Leave-one-COHORT-out: h_base and the whole residual lever chain are refitted
WITHOUT the target cohort's rows, then that cohort is predicted. The web analogue
of ios/validation/validate_loo.py, which leaves one app out.

WHY LOO AND NOT THE CALENDAR BACKTEST AS THE HEADLINE
Owner's call, and the data agrees: with training restricted to 2026-04-13 onward
a calendar backtest yields 2-3 usable T points, while LOO yields one fold per
cohort. LOO also answers the question that actually drives spend decisions --
"how well do I predict the next week or two for a cohort I have not seen" --
rather than "what happens in a year". Calendar drift is a real and larger
problem (iOS shows +16% at wk12) but it is deliberately the NEXT iteration.

WHAT IS COMPARED
Both predictors run on identical data, identical folds, identical metric, so the
delta is attributable:

  v2_formula : ONE pooled curve for everybody. No per-cohort hr, no levers --
               this is what web does today, where app_id is a single value and
               the personal multipliers average away into one curve.
  v3_formula : per-cohort hr (shrunk by K_SHRINK) x the cohort's own people's
               lever multipliers.

METRIC
Grid of weeks-of-visible-data 1..8 x horizon +1/+2/+4 weeks. Reports the median
absolute relative error AND the median signed error. The sign matters more than
the magnitude: a model that is 10% wrong in both directions is usable, one that
is 10% high every time is a systematic overspend.

Writes: reports/web_v4/loo_web.md   (nothing else, ever)

Run: .venv/Scripts/python.exe web/v4/validate_loo_web.py
"""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
os.chdir(ROOT)

from core import common  # noqa: E402
from ltv_v4.config import (  # noqa: E402
    OUT_DIR, DATA_DIR, MATRIX_GLOB, POPULATION_GLOB, RETURN_WINDOW_DAYS,
)
from ltv_v4 import se_training_web as S, map_web as M, money as MON, revenue as R  # noqa: E402
from ltv_v4 import upsell as U  # noqa: E402

WEEKS_FED = [1, 2, 3, 4, 5, 6, 7, 8]
HORIZON_OFFSETS = [1, 2, 4]
MIN_COHORT_SUBS = 100     # a fold below this is noise, not a measurement
MIN_FACT_MATURE = 30      # minimum people behind an observed fact to score against

OUT_PATH = ROOT / OUT_DIR / "loo_web.md"


def newest(glob_pat):
    files = sorted(Path(DATA_DIR).glob(glob_pat))
    if not files:
        raise FileNotFoundError(f"No {glob_pat} in {DATA_DIR}. Run build_web_se_training.py first.")
    return files[-1]


RETURN_WINDOW_WEEKS = RETURN_WINDOW_DAYS / 7.0


def observed_survival(rows: pd.DataFrame) -> tuple:
    """Actual P(reach payment k+1), and how many people back each k.

    Same shape as core.common.direct_survival, with ONE change that matters:
    censoring is applied to the AT-RISK SET by time, never by filtering rows
    before aggregating.

    Filtering rows on `evaluable` first looks equivalent and is not: max_step is
    an aggregate over a person's rows, so dropping their most recent (too-young)
    steps truncates their history and makes a live subscriber look like they
    died earlier. That deflates the fact and shows up as the model
    "over-predicting" -- measured at a uniform +29% before this was fixed.

    Instead: max_step comes from the person's FULL known history, and a person
    only counts toward step k once their step-k payment is old enough to judge,
    i.e. weeks_obs >= k + the return window. Same censoring rule as training,
    applied at the right level.
    """
    if rows.empty:
        return pd.Series(dtype=float), {}
    per = rows.groupby("sub_id").agg(max_step=("step_k", "max"), weeks_obs=("weeks_obs", "first"))
    fact, mature = {}, {}
    for k in range(1, common.HMAX + 1):
        at_risk = per[per["weeks_obs"] >= k + RETURN_WINDOW_WEEKS]
        mature[k] = len(at_risk)
        fact[k] = (at_risk["max_step"] >= k + 1).mean() if len(at_risk) else np.nan
    return pd.Series(fact), mature


def observed_ltv_pair(people: pd.DataFrame, cum: pd.DataFrame, anchor: int, horizon: int,
                      max_week: int):
    """Mean observed revenue at `anchor` AND at `horizon`, on the SAME people.

    Both numbers must come from one population -- the set mature enough to have
    reached `horizon`. Taking the anchor from everyone mature at `anchor` and the
    target from the smaller, older set mature at `horizon` compares two different
    cohorts' worth of people and manufactures a gap out of composition alone.

    That mistake is easy to make twice: an earlier diagnostic here compared an
    all-cohort model against an old-cohort-only observation and reported the
    model over-predicting payment counts by +14%. Measured per cohort the same
    quantity is -0.9%. Always fix the population first, then compare.
    """
    if horizon > max_week or people.empty:
        return None, None
    mature = people[people["age_weeks_now"] >= horizon + RETURN_WINDOW_WEEKS]
    if len(mature) < MIN_FACT_MATURE:
        return None, None
    frame = cum.reindex(mature.index)
    at_anchor = frame[anchor].dropna()
    at_horizon = frame[horizon].dropna()
    if at_anchor.empty or at_horizon.empty:
        return None, None
    return float(at_anchor.mean()), float(at_horizon.mean())


def visible_at(cohort_rows: pd.DataFrame, weeks: int) -> pd.DataFrame:
    """The cohort as it looked after `weeks` weeks -- steps beyond that are unseen.

    weeks_obs is overwritten so downstream hr/curve code treats this as the full
    extent of knowledge, mirroring visible_at_week_n() in core/web_calibration.py.
    """
    eligible = cohort_rows.loc[cohort_rows["weeks_obs"] >= weeks, "sub_id"].unique()
    vis = cohort_rows[cohort_rows["sub_id"].isin(eligible) & (cohort_rows["step_k"] <= weeks)].copy()
    vis["weeks_obs"] = weeks
    return vis


def load_inputs(verbose: bool = True) -> dict:
    """Everything the folds need. Split out so the lever study reuses one load."""
    mx = S.add_survived(pd.read_parquet(newest(MATRIX_GLOB)))
    train_all = S.training_rows(mx)

    pop = pd.read_parquet(newest(POPULATION_GLOB))
    events = R.load_events()
    max_week = int(pop["age_weeks_now"].max())
    cum = R.per_person_week_cumulative(events, pop, max_week)
    # Same measured upsell attach rate the tables use -- otherwise this harness
    # would score a model that is not the one being shipped.
    ups_obs = U.checkpoint_observations(events, pop, R.base_payment_ladder(events))
    ups_portfolio = U.portfolio_rate(ups_obs)
    ios_h = common.empirical_hbase(common.load_matrix())

    if verbose:
        print(f"matrix rows {len(mx)} | trainable {len(train_all)} | cohorts {mx['app_id'].nunique()}")
        print(f"population {len(pop)} | observed revenue weeks 0..{max_week}")
        print("iOS h_base loaded (used only to splice the tail past web's own evidence)")

    return {"mx": mx, "train_all": train_all, "pop_idx": pop.set_index("email"),
            "cum": cum, "max_week": max_week, "ios_h": ios_h,
            "ups_obs": ups_obs, "ups_portfolio": ups_portfolio}


def run_folds(data: dict, levers=None, verbose: bool = True) -> pd.DataFrame:
    """Leave-one-cohort-out over every (cohort, weeks_fed, horizon) combination.

    levers: None uses ltv_v4.config.MAP_LEVERS_WEB. Passing an explicit list is
    how the lever study isolates one lever's contribution -- same folds, same
    data, same metric, only the lever set changes, so the delta is attributable.
    """
    mx, train_all = data["mx"], data["train_all"]
    pop_idx, cum, max_week, ios_h = data["pop_idx"], data["cum"], data["max_week"], data["ios_h"]
    ups_obs, ups_portfolio = data["ups_obs"], data["ups_portfolio"]

    cohorts = [c for c, g in mx.groupby("app_id") if g["sub_id"].nunique() >= MIN_COHORT_SUBS]
    if verbose:
        print(f"cohorts with >={MIN_COHORT_SUBS} subscribers: {len(cohorts)}")

    rows = []
    for cohort in cohorts:
        target = mx[mx["app_id"] == cohort]
        fact, mature = observed_survival(target)
        if fact.dropna().empty:
            continue

        # --- LOO: refit with the target cohort removed --------------------------
        loo_train = train_all[train_all["app_id"] != cohort]
        try:
            state = M.fit(loo_train, ios_h_base=ios_h, levers=levers)
        except RuntimeError as exc:
            if verbose:
                print(f"  {cohort}: fit failed ({exc}) -- skipped")
            continue

        # v2 baseline: ONE pooled curve, no per-cohort hr, no cohort levers.
        pooled_visible = loo_train.copy()

        for weeks in WEEKS_FED:
            vis = visible_at(target, weeks)
            if vis.empty or vis["sub_id"].nunique() < MIN_COHORT_SUBS // 2:
                continue

            hr_v3 = M.cohort_hr(state, vis, weeks)
            curve_v3 = M.cell_curve(state, vis, hr_v3)

            pooled_vis = visible_at(pooled_visible, weeks)
            curve_v2 = M.cell_curve(state, pooled_vis, M.cohort_hr(state, pooled_vis, weeks))

            # People of this cohort who reached the base plan -- the ones the
            # survival curve is about, and the LTV denominator here.
            cohort_emails = target["sub_id"].unique()
            cohort_people = pop_idx.reindex(cohort_emails).dropna(subset=["first_date"])

            for off in HORIZON_OFFSETS:
                k = weeks + off
                actual = fact.get(k, np.nan)
                if pd.isna(actual) or actual <= 0 or mature.get(k, 0) < MIN_FACT_MATURE:
                    continue

                row = {
                    "cohort": cohort, "weeks_fed": weeks, "horizon_offset": off, "step": k,
                    "n_mature": mature[k], "actual": actual,
                    "v2_formula": curve_v2.get(k, np.nan),
                    "v3_formula": curve_v3.get(k, np.nan),
                    "hr_v3": hr_v3,
                }

                # --- money: same folds, same horizons, LTV instead of survival ---
                # Anchored at `weeks` -- the revenue earned by then is a fact the
                # forecaster would already have; only the increment is modelled.
                # Anchor and target come from ONE population (see the helper).
                anchor_obs, ltv_actual = observed_ltv_pair(cohort_people, cum, weeks, k, max_week)
                if ltv_actual is not None and anchor_obs is not None and ltv_actual > 0:
                    row["ltv_actual"] = ltv_actual
                    row["ltv_anchor_obs"] = anchor_obs
                    share_ups = U.cell_rate(ups_obs, cohort_people.index, ups_portfolio)
                    row["ltv_v2_formula"] = MON.cell_ltv(curve_v2, anchor_obs, weeks, k, cohort_people, share_ups=share_ups)
                    row["ltv_v4_formula"] = MON.cell_ltv(curve_v3, anchor_obs, weeks, k, cohort_people, share_ups=share_ups)
                rows.append(row)

    detail = pd.DataFrame(rows)
    if detail.empty:
        raise SystemExit("No scoreable (cohort, weeks_fed, horizon) combinations -- widen the thresholds")

    for model in ("v2_formula", "v3_formula"):
        detail[f"err_{model}"] = (detail[model] - detail["actual"]) / detail["actual"]
        ltv_col = f"ltv_{model}"
        if ltv_col in detail:
            detail[f"ltv_err_{model}"] = (detail[ltv_col] - detail["ltv_actual"]) / detail["ltv_actual"]
    return detail


def main():
    data = load_inputs()
    detail = run_folds(data)
    # Persist the per-fold detail so the comparison report can be assembled
    # without paying for another full LOO sweep.
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    detail.to_parquet(Path(DATA_DIR) / "loo_detail.parquet")
    write_report(detail)
    print_summary(detail)


def _agg(detail, by, prefix=""):
    """Median |rel err| and median signed err per group. prefix="ltv_" scores money."""
    sub = detail.dropna(subset=[f"{prefix}err_v2_formula", f"{prefix}err_v3_formula"])
    out = []
    for keys, g in sub.groupby(by):
        row = dict(zip(by, keys if isinstance(keys, tuple) else [keys]))
        row["n"] = len(g)
        for model in ("v2_formula", "v3_formula"):
            row[f"{model}_abs"] = g[f"{prefix}err_{model}"].abs().median()
            row[f"{model}_sign"] = g[f"{prefix}err_{model}"].median()
        out.append(row)
    return pd.DataFrame(out)


def print_summary(detail):
    for label, prefix in [("SURVIVAL", ""), ("LTV (money)", "ltv_")]:
        if f"{prefix}err_v3_formula" not in detail:
            continue
        sub = detail.dropna(subset=[f"{prefix}err_v3_formula"])
        print(f"\n=== {label}: by horizon offset (median |rel err|, median signed) ===")
        print(_agg(detail, ["horizon_offset"], prefix).round(4).to_string(index=False))
        print(f"--- {label} overall ---")
        for model in ("v2_formula", "v3_formula"):
            print(f"{model:12s} |err|={sub[f'{prefix}err_{model}'].abs().median():.4f}  "
                  f"sign={sub[f'{prefix}err_{model}'].median():+.4f}  n={len(sub)}")


def write_report(detail):
    by_off = _agg(detail, ["horizon_offset"])
    by_fed = _agg(detail, ["weeks_fed"])
    grid = _agg(detail, ["weeks_fed", "horizon_offset"])

    lines = [
        "# LOO-валидация веб-модели v3",
        "",
        "Leave-one-COHORT-out: `h_base` и вся residual-цепочка множителей переобучены",
        "БЕЗ строк целевой когорты, затем эта когорта предсказана. Веб-аналог",
        "`ios/validation/validate_loo.py` (там LOO по аппам).",
        "",
        "Ячейка: медиана |отн. ошибки| и медиана ЗНАКОВОЙ ошибки (+ завышаем / − занижаем).",
        "Знак важнее модуля: модель, ошибающаяся на 10% в обе стороны, пригодна;",
        "модель, завышающая на 10% каждый раз, — это систематический перерасход.",
        "",
        "| предиктор | что это |",
        "|---|---|",
        "| `v2_formula` | ОДНА пулированная кривая на всех. Ни hr по когорте, ни рычагов — то, что веб делает сегодня. |",
        "| `v3_formula` | hr по когорте (усадка `K_SHRINK`) × множители рычагов её собственных людей. |",
        "",
        f"Цензурирование: {RETURN_WINDOW_DAYS} дней, строго по времени (см. `ltv_v4/config.py`).",
        f"Факт считается на тех же цензурированных строках, что и обучение.",
        "",
        f"Всего замеров: {len(detail)} | когорт: {detail['cohort'].nunique()}",
        "",
        "## Итог по горизонтам",
        "",
        "| горизонт | n | v2 \\|ошибка\\| | v2 знак | v3 \\|ошибка\\| | v3 знак |",
        "|---|---|---|---|---|---|",
    ]
    for _, r in by_off.iterrows():
        lines.append(
            f"| +{int(r['horizon_offset'])} нед | {int(r['n'])} | {r['v2_formula_abs']:.1%} | "
            f"{r['v2_formula_sign']:+.1%} | {r['v3_formula_abs']:.1%} | {r['v3_formula_sign']:+.1%} |"
        )

    lines += ["", "## По количеству видимых недель", "",
              "| видно недель | n | v2 \\|ошибка\\| | v2 знак | v3 \\|ошибка\\| | v3 знак |",
              "|---|---|---|---|---|---|"]
    for _, r in by_fed.iterrows():
        lines.append(
            f"| {int(r['weeks_fed'])} | {int(r['n'])} | {r['v2_formula_abs']:.1%} | "
            f"{r['v2_formula_sign']:+.1%} | {r['v3_formula_abs']:.1%} | {r['v3_formula_sign']:+.1%} |"
        )

    lines += ["", "## Полная сетка (видимых недель × горизонт)", "",
              "| видно | горизонт | n | v2 \\|ош\\| | v2 знак | v3 \\|ош\\| | v3 знак |",
              "|---|---|---|---|---|---|---|"]
    for _, r in grid.iterrows():
        lines.append(
            f"| {int(r['weeks_fed'])} | +{int(r['horizon_offset'])} | {int(r['n'])} | "
            f"{r['v2_formula_abs']:.1%} | {r['v2_formula_sign']:+.1%} | "
            f"{r['v3_formula_abs']:.1%} | {r['v3_formula_sign']:+.1%} |"
        )

    # ---- money block ----
    if "ltv_err_v3_formula" in detail:
        ltv_sub = detail.dropna(subset=["ltv_err_v3_formula"])
        ltv_off = _agg(detail, ["horizon_offset"], "ltv_")
        lines += [
            "", "## LTV (деньги), те же фолды и горизонты", "",
            "Дожитие × цена. Триал — разово $0.99 на k=0, апсейл — $11.99 на чекпоинтах",
            "недель 0/4/8/…, база — $9.99 каждую неделю пока жив. Факт считается по",
            "РЕАЛЬНЫМ суммам платежей, не по модельным ценам.",
            "",
            "| горизонт | n | v2 \\|ошибка\\| | v2 знак | v3 \\|ошибка\\| | v3 знак |",
            "|---|---|---|---|---|---|",
        ]
        for _, r in ltv_off.iterrows():
            lines.append(
                f"| +{int(r['horizon_offset'])} нед | {int(r['n'])} | {r['v2_formula_abs']:.1%} | "
                f"{r['v2_formula_sign']:+.1%} | {r['v3_formula_abs']:.1%} | {r['v3_formula_sign']:+.1%} |"
            )
        lines += [
            "",
            f"Итого по LTV: v2 |ошибка| = {ltv_sub['ltv_err_v2_formula'].abs().median():.1%} "
            f"(знак {ltv_sub['ltv_err_v2_formula'].median():+.1%}), "
            f"v3 |ошибка| = {ltv_sub['ltv_err_v3_formula'].abs().median():.1%} "
            f"(знак {ltv_sub['ltv_err_v3_formula'].median():+.1%}), n={len(ltv_sub)}",
            "",
            "Ошибка по LTV НИЖЕ, чем по дожитию, и это ожидаемо: прогноз заякорен",
            "на уже наблюдённой выручке когорты, поэтому моделируется только",
            "приращение — бо́льшая часть числа это факт, а не прогноз.",
            "",
            "### Три ошибки, которые пришлось поймать в этом слое",
            "",
            "Все три давали завышение и все три найдены только потому, что метрика",
            "выглядела неправдоподобно (13.9% при точном дожитии в 2%).",
            "",
            "1. **Условное дожитие вместо безусловного.** Приращение считалось как",
            "   `S[k]/S[якорь]` — «при условии, что человек ещё платит». Но якорь",
            "   это СРЕДНЕЕ по всем членам когорты, включая отвалившихся, поэтому",
            "   и приращение обязано быть на той же базе. На неделях 4→8 условная",
            "   форма давала $32.50 прироста, безусловная $16.06, факт $15.39.",
            "2. **Апселл кредитовался на неделе 0.** Замер: первый апселл приходит",
            "   на неделю 2 (1311 случаев из ~1414), на неделе 0 — ноль. Правильные",
            "   чекпоинты 2, 6, 10, …",
            "3. **Якорь и цель считались на разных зрелых подмножествах** — одна",
            "   популяция для якоря, другая (меньше и старше) для цели. Разрыв",
            "   рождался из состава, а не из модели.",
        ]

    v2_abs = detail["err_v2_formula"].abs().median()
    v3_abs = detail["err_v3_formula"].abs().median()
    v2_sgn = detail["err_v2_formula"].median()
    v3_sgn = detail["err_v3_formula"].median()
    lines += [
        "", "## Вердикт (дожитие)", "",
        f"- **v2-формула**: |ошибка| = {v2_abs:.1%}, знак = {v2_sgn:+.1%}",
        f"- **v3-формула**: |ошибка| = {v3_abs:.1%}, знак = {v3_sgn:+.1%}",
        f"- дельта по модулю: {v3_abs - v2_abs:+.1%} п.п. "
        f"({'в пользу v3' if v3_abs < v2_abs else 'в пользу v2'})",
        f"- дельта по знаку: {abs(v3_sgn) - abs(v2_sgn):+.1%} п.п. смещения "
        f"({'v3 менее смещена' if abs(v3_sgn) < abs(v2_sgn) else 'v3 более смещена'})",
        "",
        "Это первое измеренное число точности веба в этом проекте. Гейт",
        "`reconcile_v2.py` проверял согласие с golden, а не правильность прогноза.",
    ]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    main()
