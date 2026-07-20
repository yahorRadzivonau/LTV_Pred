"""
Календарный бэктест — симуляция прода: встаём в прошлую дату T, обучаемся
ТОЛЬКО на том, что случилось до T, предсказываем аппы, стартовавшие
незадолго до T, сверяем с фактом после T (в отличие от степенного
бэктеста compare.py, где модель предсказывает аппы 2024-2025, обучаясь в
том числе на их современниках — смешение эпох).

Шаг 1 — дизайн-чек (inventory/print_inventory): хватает ли данных на
разрезы. Владелец подтвердил все 4 даты 2026-07-06, включая две тонкие
(2025-07-01, 2025-10-01 — по 2 оцениваемых аппа) — двигать некуда
(раньше портфель меньше, позже окна перекроются), 12 оценок суммарно
достаточно для направления.

Шаг 2 — прогон (fit_state_for_T, evaluate): для каждой T обучение ТОЛЬКО
на pay_ts<T, прогноз аппа по данным, обрезанным до T, сверка с фактом по
ПОЛНЫМ данным. КЛЮЧЕВОЙ момент корректности: колонка weeks_obs в
исходной матрице посчитана относительно снапшота извлечения данных
(~2026-07, см. pipeline/se_training.py: weeks_obs = (T_snapshot -
first_pay)/7дней) — это будущее относительно любого T из CUTOFFS. Если
не пересчитать её относительно T, censoring-логика внутри
common.direct_survival/empirical_hbase/map_model (везде, где сравнивают
weeks_obs>=step_k) будет думать, что у каждого подписчика было МНОГО
больше времени на дожитие, чем было на самом деле в момент T — причём
это ломает не только прогноз аппа, но и ОБУЧЕНИЕ (h_base/множители на
mx_train), потому что pay_ts<T обрезает СТРОКИ, но исходный weeks_obs
остался бы "из будущего" и создал бы фиктivный обрыв на границе T,
который выглядел бы как повальный обвал у всех аппов сразу. Поэтому
weeks_obs_at() пересчитывает эту колонку ПОДПИСЧИК-ЗА-ПОДПИСЧИКОМ
((T - его первый платёж)/7дней) и применяется и к mx_train (обучение),
и к app_rows_visible (прогноз) — модели models/*.py при этом не
меняются, это чисто входные данные.

Отдельно: "weeks" (аргумент predict()) — это СКАЛЯР на весь апп ("сколько
календарных недель апп прожил к T", min(8, floor((T-первый_платёж_аппа)/7))),
не то же самое, что weeks_obs (он же per-subscriber, для censoring внутри
моделей) — их не следует путать, оба нужны и считаются раздельно.

Шаг 3 — отчёт reports/calendar_backtest.md (write_report).

Кэш фитов по T в reports/calendar_cache/ (fingerprint как в
validate_loo.py, здесь дополнительно включает сам этот файл — логика
пересчёта weeks_obs живёт тут, а не в models/*.py).

Запуск: python calendar_backtest.py
"""
import hashlib
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from core import common, map_model
from models import empirical

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "reports" / "calendar_cache"
FP_PATH = CACHE_DIR / "_fingerprint.txt"
OUT_PATH = ROOT / "reports" / "calendar_backtest.md"
DETAIL_PATH = ROOT / "reports" / "comparison_detail.csv"

PROTECTED_FILES = ("models/map_model.py", "models/common.py", "models/empirical.py", "calendar_backtest.py")

CUTOFFS = ["2025-07-01", "2025-10-01", "2026-01-01", "2026-04-01"]
YOUNG_WINDOW_START_WEEKS = 8   # "молодой на T": первый платёж в [T-8нед, T-1нед]
YOUNG_WINDOW_END_WEEKS = 1
MIN_EVALUABLE = 3              # ниже — владелец решает сам, сдвигать ли даты (решено: не двигать)
MIN_TOTAL_EVAL = 8             # < этого -> крупное предупреждение в шапке отчёта
CAL_HORIZONS = (12, 26)        # 52 не берём — молодые аппы не дожили
STEP_TEST_PW = 4               # срез "степенного теста" для сравнения (compare.py, pw=4 нед)


# ---------------------------------------------------------------------------
# fingerprint кэша
# ---------------------------------------------------------------------------

def _compute_fingerprint():
    h = hashlib.sha256()
    for rel in PROTECTED_FILES:
        h.update((ROOT / rel).read_bytes())
    h.update(str((ROOT / "data" / "se_training.parquet").stat().st_mtime).encode())
    return h.hexdigest()


def check_fingerprint():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fp = _compute_fingerprint()
    if FP_PATH.exists():
        old = FP_PATH.read_text(encoding="utf-8").strip()
        if old != fp:
            raise SystemExit(
                "кэш устарел, почисти reports/calendar_cache/ "
                f"(fingerprint {' + '.join(PROTECTED_FILES)} + mtime data/se_training.parquet "
                "изменился с прошлого запуска)"
            )
        print(f"fingerprint кэша совпадает ({fp[:12]}...) — переиспользую.")
    else:
        FP_PATH.write_text(fp, encoding="utf-8")
        print(f"fingerprint кэша установлен впервые ({fp[:12]}...).")


# ---------------------------------------------------------------------------
# Шаг 1 — дизайн-чек (принято владельцем 2026-07-06, все 4 даты остаются)
# ---------------------------------------------------------------------------

def inventory(mx):
    """Для каждой T: сколько аппов 'молодых на T' (кандидаты), у скольких
    из них есть факт для сверки (mature>=200 на нед12 по ПОЛНЫМ данным,
    не-crash по ПОЛНОЙ истории), и сколько строк для обучения (pay_ts<T)."""
    first_pay = mx.groupby("app_id")["pay_ts"].min()
    rows = []
    for t_str in CUTOFFS:
        T = pd.Timestamp(t_str, tz="UTC")
        window_start = T - pd.Timedelta(weeks=YOUNG_WINDOW_START_WEEKS)
        window_end = T - pd.Timedelta(weeks=YOUNG_WINDOW_END_WEEKS)
        young_apps = first_pay[(first_pay >= window_start) & (first_pay <= window_end)].index.tolist()

        with_fact, evaluable = [], []
        for app in young_apps:
            app_rows_full = mx[mx["app_id"] == app]
            fact, mature, per = common.direct_survival(app_rows_full)
            if mature.get(12, 0) >= common.MIN_MATURE:
                with_fact.append(app)
                if not common.detect_crash(per):
                    evaluable.append(app)

        train_rows = int((mx["pay_ts"] < T).sum())
        rows.append({
            "T": t_str,
            "young_candidates": len(young_apps),
            "with_fact_mature200": len(with_fact),
            "evaluable_non_crash": len(evaluable),
            "train_rows": train_rows,
            "evaluable_apps": evaluable,
        })
    return rows


def print_inventory(rows):
    print(f"{'T':<12} {'кандидатов':>11} {'с фактом(mature>=200)':>23} {'оцениваемых(non-crash)':>23} {'строк обучения':>15}")
    for r in rows:
        print(f"{r['T']:<12} {r['young_candidates']:>11} {r['with_fact_mature200']:>23} "
              f"{r['evaluable_non_crash']:>23} {r['train_rows']:>15}")


# ---------------------------------------------------------------------------
# Шаг 2 — прогон
# ---------------------------------------------------------------------------

def weeks_obs_at(df, T):
    """Пересчитывает weeks_obs относительно даты отсечки T (не относительно
    снапшота извлечения данных ~2026-07, см. докстринг модуля) — по
    подписчику: (T - его первый платёж среди строк df) / 7 дней."""
    df = df.copy()
    first_pay = df.groupby("sub_id")["pay_ts"].transform("min")
    df["weeks_obs"] = (T - first_pay).dt.total_seconds() / (7 * 86400)
    return df


def fit_state_for_T(t_str, mx):
    cache_map = CACHE_DIR / f"map_{t_str}.pkl"
    cache_emp = CACHE_DIR / f"emp_{t_str}.pkl"
    if cache_map.exists() and cache_emp.exists():
        with open(cache_map, "rb") as f:
            map_state = pickle.load(f)
        with open(cache_emp, "rb") as f:
            emp_state = pickle.load(f)
        print(f"[{t_str}] state из кэша")
        return map_state, emp_state

    T = pd.Timestamp(t_str, tz="UTC")
    mx_train = mx[mx["pay_ts"] < T].copy()
    mx_train = weeks_obs_at(mx_train, T)
    apps_info_train = common.select_apps(mx_train)
    print(f"[{t_str}] обучаю на {len(mx_train)} строках "
          f"({len(apps_info_train[0])} аппов в портфеле, {len(apps_info_train[4])} crash)...")
    map_state = map_model.fit(mx_train, apps_info_train)
    emp_state = empirical.fit(mx_train)
    with open(cache_map, "wb") as f:
        pickle.dump(map_state, f)
    with open(cache_emp, "wb") as f:
        pickle.dump(emp_state, f)
    print(f"[{t_str}] фит завершён, закэширован")
    return map_state, emp_state


def evaluate(mx, inv_rows):
    """Возвращает список строк (T, app, недель_жизни_к_T, факт/прогноз/знак
    map и empirical на нед12 и нед26) — по одной на каждую оцениваемую пару
    (T, app)."""
    per_app_rows = []
    for r in inv_rows:
        t_str = r["T"]
        T = pd.Timestamp(t_str, tz="UTC")
        map_state, emp_state = fit_state_for_T(t_str, mx)

        for app in r["evaluable_apps"]:
            app_rows_visible_raw = mx[(mx["app_id"] == app) & (mx["pay_ts"] < T)].copy()
            assert (app_rows_visible_raw["pay_ts"] < T).all(), f"утечка будущего: {app} @ {t_str}"
            app_rows_visible = weeks_obs_at(app_rows_visible_raw, T)

            first_pay_app = app_rows_visible_raw["pay_ts"].min()
            weeks = min(8, (T - first_pay_app).days // 7)
            assert weeks >= 1, f"{app} @ {t_str}: weeks={weeks} < 1 — нарушено окно инвентаризации Шага 1"

            pred_map = map_model.predict(map_state, app_rows_visible, weeks)
            pred_emp = empirical.predict(emp_state, app_rows_visible, weeks)

            fact, mature, _ = common.direct_survival(mx[mx["app_id"] == app])  # ПОЛНЫЕ данные — так меряем факт

            row = {"T": t_str, "app": app, "weeks_lived": int(weeks)}
            for h in CAL_HORIZONS:
                f = fact.get(h, np.nan)
                m, e = pred_map.get(h), pred_emp.get(h)
                row[f"fact{h}"] = f
                row[f"map{h}"] = m
                row[f"emp{h}"] = e
                if not np.isnan(f) and f > 0:
                    row[f"map_signed{h}"] = (m - f) / f * 100
                    row[f"emp_signed{h}"] = (e - f) / f * 100
                    row[f"map_abs{h}"] = abs(m - f) / f * 100
                    row[f"emp_abs{h}"] = abs(e - f) / f * 100
                else:
                    row[f"map_signed{h}"] = row[f"emp_signed{h}"] = np.nan
                    row[f"map_abs{h}"] = row[f"emp_abs{h}"] = np.nan
            per_app_rows.append(row)
    return per_app_rows


# ---------------------------------------------------------------------------
# Шаг 3 — отчёт
# ---------------------------------------------------------------------------

def aggregate_by_T(per_app_rows):
    """T x horizon x model -> (медиана|abs%|, медиана signed%, n). Плюс
    T='все T' — пул всех оценок вместе."""
    out = {}
    groups = {t: [r for r in per_app_rows if r["T"] == t] for t in CUTOFFS}
    groups["все T"] = per_app_rows
    for t_key, rows in groups.items():
        out[t_key] = {}
        for h in CAL_HORIZONS:
            for model in ("map", "emp"):
                abs_vals = [r[f"{model}_abs{h}"] for r in rows if not np.isnan(r[f"{model}_abs{h}"])]
                sgn_vals = [r[f"{model}_signed{h}"] for r in rows if not np.isnan(r[f"{model}_signed{h}"])]
                out[t_key][(h, model)] = (
                    np.median(abs_vals) if abs_vals else float("nan"),
                    np.median(sgn_vals) if sgn_vals else float("nan"),
                    len(abs_vals),
                )
    return out


def step_test_baseline():
    """Те же метрики (медиана |abs%|, медиана signed%, n) из обычного
    (степенного) бэктеста compare.py, срез pw=4 нед — источник
    reports/comparison_detail.csv (уже посчитан, без пересчёта)."""
    detail = pd.read_csv(DETAIL_PATH)
    out = {}
    for h in CAL_HORIZONS:
        for model, key in (("map", "map"), ("empirical", "emp")):
            d = detail[(detail["model"] == model) & (detail["pw_weeks"] == STEP_TEST_PW) & (detail["horizon"] == h)]
            out[(h, key)] = (
                d["abs_err_pct"].median() if len(d) else float("nan"),
                d["signed_err_pct"].median() if len(d) else float("nan"),
                d["app_id"].nunique(),
            )
    return out


def write_report(inv_rows, per_app_rows, agg, baseline):
    total_eval = len(per_app_rows)
    lines = [
        "# Календарный бэктест: симуляция прода\n",
        "Встаём в прошлую дату T, обучаемся ТОЛЬКО на том, что случилось до T "
        "(pay_ts<T, включая пересчёт weeks_obs относительно T — см. докстринг "
        "calendar_backtest.py про утечку будущего через censoring), предсказываем "
        "аппы, стартовавшие незадолго до T (окно первого платежа "
        f"[T-{YOUNG_WINDOW_START_WEEKS}нед, T-{YOUNG_WINDOW_END_WEEKS}нед]), сверяем с фактом "
        "по ПОЛНЫМ (сегодняшним) данным. models/*.py и data/ не менялись — "
        "только чтение fit()/predict().\n",
    ]

    if total_eval < MIN_TOTAL_EVAL:
        lines.append(
            f"## ⚠ ВЫБОРКА МАЛА (n={total_eval} < {MIN_TOTAL_EVAL}): "
            "НАПРАВЛЕНИЕ ВАЖНЕЕ ЦИФР. Не делать выводов о точной величине эффекта, "
            "только о его знаке.\n"
        )
    else:
        lines.append(f"Всего оценок (T, апп): {total_eval} — выборка скромная, но выше порога "
                      f"{MIN_TOTAL_EVAL}, владелец решил не двигать даты (отбор аппов не менять).\n")

    lines.append("## Шаг 1 (для полноты): инвентаризация\n")
    lines.append("| T | кандидатов | с фактом (mature≥200) | оцениваемых (non-crash) | строк обучения |")
    lines.append("|---|---|---|---|---|")
    for r in inv_rows:
        lines.append(f"| {r['T']} | {r['young_candidates']} | {r['with_fact_mature200']} | "
                      f"{r['evaluable_non_crash']} | {r['train_rows']} |")
    lines.append("")

    lines.append("## Шаг 3.1 — результаты по T\n")
    for h in CAL_HORIZONS:
        lines.append(f"### Горизонт нед{h}\n")
        lines.append("| T | map (медиана\\|ошибки\\|, зн, n) | empirical (медиана\\|ошибки\\|, зн, n) |")
        lines.append("|---|---|---|")
        for t_key in CUTOFFS + ["все T"]:
            m_med, m_sgn, m_n = agg[t_key][(h, "map")]
            e_med, e_sgn, e_n = agg[t_key][(h, "emp")]
            m_cell = f"{m_med:.1f}% (зн{m_sgn:+.1f}%, n={m_n})" if m_n else "н/д"
            e_cell = f"{e_med:.1f}% (зн{e_sgn:+.1f}%, n={e_n})" if e_n else "н/д"
            lines.append(f"| {t_key} | {m_cell} | {e_cell} |")
        lines.append("")

    lines.append("## Шаг 3.2 — ключевое сравнение: степенной тест vs календарный (цена дрейфа эпох)\n")
    lines.append(f"Степенной тест = reports/comparison.md, срез pw={STEP_TEST_PW} нед данных "
                  "(тот же снимок отчёта, не пересчитан). Календарный = строка 'все T' выше.\n")
    lines.append("| горизонт | модель | степенной \\|ошибка\\| (n) | календарный \\|ошибка\\| (n) | дельта, п.п. | степенной зн | календарный зн |")
    lines.append("|---|---|---|---|---|---|---|")
    for h in CAL_HORIZONS:
        for model, key in (("map", "map"), ("empirical", "emp")):
            step_med, step_sgn, step_n = baseline[(h, key)]
            cal_med, cal_sgn, cal_n = agg["все T"][(h, key)]
            delta = cal_med - step_med if not (np.isnan(cal_med) or np.isnan(step_med)) else float("nan")
            delta_s = f"{delta:+.1f}" if not np.isnan(delta) else "н/д"
            lines.append(f"| нед{h} | {model} | {step_med:.1f}% (n={step_n}) | "
                          f"{cal_med:.1f}% (n={cal_n}) | {delta_s} | {step_sgn:+.1f}% | {cal_sgn:+.1f}% |")
    lines.append("")

    lines.append("## Шаг 3.3 — разбор знака\n")
    lines.append(
        "Гипотеза тикета: если завышение (+) в степенном тесте — артефакт смешения эпох "
        "(обучение видит современников/будущее предсказываемого аппа), то в календарном "
        "тесте (без этой утечки) знак должен либо ослабнуть/смениться на занижение, либо, "
        "если завышение — не артефакт, а структурный эффект, остаться тем же или усилиться. "
        "Ниже — что вышло по фактическим числам (без интерпретации сверх них):\n"
    )
    for h in CAL_HORIZONS:
        step_sgn = baseline[(h, "map")][1]
        cal_sgn = agg["все T"][(h, "map")][1]
        cal_n = agg["все T"][(h, "map")][2]
        if cal_n == 0 or np.isnan(cal_sgn):
            lines.append(f"- **map, нед{h}**: степенной зн={step_sgn:+.1f}%, календарный зн=н/д (n=0).")
            continue
        if np.sign(cal_sgn) == np.sign(step_sgn) and abs(cal_sgn) > abs(step_sgn):
            direction = "УСИЛИЛОСЬ (тот же знак, больше по модулю)"
        elif np.sign(cal_sgn) == np.sign(step_sgn):
            direction = "тот же знак, ослабло по модулю"
        elif cal_sgn == 0:
            direction = "обнулилось"
        else:
            direction = "СМЕНИЛО ЗНАК"
        lines.append(f"- **map, нед{h}**: степенной зн={step_sgn:+.1f}% -> календарный зн={cal_sgn:+.1f}% "
                      f"(n={cal_n}) — {direction}.")
    lines.append("")

    lines.append("## По-апповая таблица всех оценок (доп. по запросу владельца)\n")
    lines.append("| T | app_id | недель жизни к T | факт12 | map12 | emp12 | зн.map12 | зн.emp12 | "
                  "факт26 | map26 | emp26 | зн.map26 | зн.emp26 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for row in sorted(per_app_rows, key=lambda r: (r["T"], r["app"])):
        def cell(key, fmt="{:.4f}"):
            v = row.get(key)
            return fmt.format(v) if v is not None and not (isinstance(v, float) and np.isnan(v)) else "н/д"

        lines.append(
            f"| {row['T']} | {row['app']} | {row['weeks_lived']} | "
            f"{cell('fact12')} | {cell('map12')} | {cell('emp12')} | "
            f"{cell('map_signed12', '{:+.1f}%')} | {cell('emp_signed12', '{:+.1f}%')} | "
            f"{cell('fact26')} | {cell('map26')} | {cell('emp26')} | "
            f"{cell('map_signed26', '{:+.1f}%')} | {cell('emp_signed26', '{:+.1f}%')} |"
        )

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nотчёт записан: {OUT_PATH}")


def main():
    check_fingerprint()

    mx = common.load_matrix()
    inv_rows = inventory(mx)
    print_inventory(inv_rows)
    total_evaluable = sum(r["evaluable_non_crash"] for r in inv_rows)
    print(f"\nвсего оцениваемых пар (T, апп): {total_evaluable}")

    print("\nШаг 2: прогон по T...")
    per_app_rows = evaluate(mx, inv_rows)

    print("\nШаг 3: агрегация и отчёт...")
    agg = aggregate_by_T(per_app_rows)
    baseline = step_test_baseline()
    write_report(inv_rows, per_app_rows, agg, baseline)


if __name__ == "__main__":
    main()
