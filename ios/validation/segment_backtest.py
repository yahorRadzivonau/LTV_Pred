"""
Задача D: сегментный бэктест — точность map ВНУТРИ аппа по гео-сегментам.
Настоящее назначение карты: различать сегменты внутри одного аппа, чего
empirical не умеет в принципе (одна кривая на весь апп).

Сегмент = пара (app_id, geo), гео — ПОСЛЕ схлопывания через
map_model._collapse_small (т.е. "other" тоже валидный сегмент). Порог
включения сегмента: >=300 подписчиков И mature>=100 на нед12.

Прогноз сегмента (map_segment) считается ЛОКАЛЬНО в этом файле, копией
шага 3.2/3.3 map_model.predict(), но с подменой hr: hr берётся от ВСЕГО
аппа (посчитан на полных app_rows), а личные множители — от юзеров
сегмента. Обоснование: hr сегмента впитал бы гео-эффект и обнулил бы
тест. models/map_model.py НЕ меняется — используются только его
fit()/personal_multiplier()/_prep()/_collapse_small() как есть (чтение).

Выход: reports/segment_backtest.md.
Запуск: python segment_backtest.py
"""
from pathlib import Path

import numpy as np
import pandas as pd

from core import common, map_model
from models import empirical

ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / "reports" / "segment_backtest.md"

PW = 4                      # компромисс объёма и раннести
SEG_MIN_SUBS = 300
SEG_MIN_MATURE12 = 100
SANITY_TOL = 0.02            # 2 п.п. на нед12
BET_THRESHOLD = 0.05         # |M_geo - 1| > 0.05 -> "явная ставка"
DIRECTION_OK = 0.60
TOP_N = 15
FLAG_APP = "id6744300418"    # самый текучий (см. reports/app_6744300418.md)


def app_hr(state, app_rows, weeks):
    """hr аппа — дословная копия Шага 3.2 map_model.predict() (без
    построения кривой). models/map_model.py не менялся, это отдельная
    read-only копия его формулы для подмены hr в сегментном прогнозе."""
    h_base = state["h_base"]
    combos = map_model.personal_multiplier(state, app_rows)
    subs = map_model._subs_with_levers(state, app_rows)
    subs = subs.merge(combos[map_model.MAP_LEVERS + ["mult"]], on=map_model.MAP_LEVERS, how="left")
    sub_mult = subs.set_index("sub_id")["mult"]

    obs = app_rows[app_rows["weeks_obs"] >= app_rows["step_k"] + 1].copy()
    obs = obs[obs["step_k"] <= weeks]
    n_seen = len(obs)
    if n_seen > 0:
        obs["mult"] = obs["sub_id"].map(sub_mult)
        obs["exp_die"] = (h_base.reindex(obs["step_k"]).values * obs["mult"].values)
        obs["exp_die"] = obs["exp_die"].clip(0.001, 0.999)
        g = obs.groupby("step_k").agg(o=("died", "mean"), e=("exp_die", "mean"), n=("died", "size"))
        g = g[(g["n"] >= 20) & (g["e"] > 0)]
        log_hr = np.average(np.log(np.clip(g["o"] / g["e"], 0.2, 5.0)), weights=g["n"]) if len(g) else 0.0
    else:
        log_hr = 0.0
    return float(np.exp(log_hr * (n_seen / (n_seen + common.K_SHRINK))))


def segment_curve(state, seg_rows, hr):
    """Кривая сегмента: личные множители СЕГМЕНТА x h_base x hr (внешний).
    Копия Шага 3.3 map_model.predict(), тот же средневзвешенный дискретный
    расчёт, единственная разница — hr передан аргументом, а не посчитан
    из seg_rows."""
    h_base = state["h_base"]
    combos = map_model.personal_multiplier(state, seg_rows)
    haz = np.clip(np.outer(combos["mult"].values, h_base.values) * hr, 0.001, 0.999)
    curves = np.cumprod(1 - haz, axis=1)
    w = combos["w"].values[:, None]
    curve = pd.Series((curves * w).sum(axis=0) / w.sum(), index=range(1, common.HMAX + 1))
    return curve, float(combos["w"].sum())


def geo_groups(state, app_rows):
    """app_rows после _prep + _collapse_small, сгруппированные по geo
    (ПОСЛЕ схлопывания — 'other' тоже валидная группа)."""
    prepped = map_model._prep(app_rows)
    collapsed = map_model._collapse_small(prepped, state["small_categories"])
    return {g: rows for g, rows in collapsed.groupby("geo")}


def sanity_check(mx, clean_apps, map_state):
    """Взвешенная по юзерам сумма прогнозов map-сегментов (ВСЕ гео-группы
    аппа, без порога 300/mature — полное покрытие) должна восстанавливать
    прогноз map всего аппа на нед12. Допуск 2 п.п. — иначе баг в подмене hr."""
    worst, worst_app = 0.0, None
    for app in clean_apps:
        app_rows = mx[mx["app_id"] == app]
        hr = app_hr(map_state, app_rows, PW)
        groups = geo_groups(map_state, app_rows)
        total_w, weighted12 = 0.0, 0.0
        for geo, rows in groups.items():
            curve, w = segment_curve(map_state, rows, hr)
            weighted12 += curve.get(12) * w
            total_w += w
        recon12 = weighted12 / total_w
        full12 = map_model.predict(map_state, app_rows, PW).get(12)
        diff = abs(recon12 - full12)
        if diff > worst:
            worst, worst_app = diff, app
    return worst, worst_app


def build_segments(mx, clean_apps, apps_facts, map_state, emp_state):
    segments = []
    for app in clean_apps:
        app_rows = mx[mx["app_id"] == app]
        hr = app_hr(map_state, app_rows, PW)
        pred_app = map_model.predict(map_state, app_rows, PW)
        emp_app = empirical.predict(emp_state, app_rows, PW)
        groups = geo_groups(map_state, app_rows)
        for geo, rows in groups.items():
            n_subs = rows["sub_id"].nunique()
            if n_subs < SEG_MIN_SUBS:
                continue
            fact, mature, _ = common.direct_survival(rows)
            if mature.get(12, 0) < SEG_MIN_MATURE12:
                continue
            curve, w = segment_curve(map_state, rows, hr)
            m_geo = map_state["multipliers"]["geo"].get(geo, map_state["multipliers"]["geo"]["__default__"])
            segments.append({
                "app": app, "geo": geo, "n_subs": n_subs,
                "mature12": mature.get(12, 0), "mature26": mature.get(26, 0),
                "fact12": fact.get(12), "fact26": fact.get(26),
                "map12": curve.get(12), "map26": curve.get(26),
                "emp12": emp_app.get(12), "emp26": emp_app.get(26),
                "app_fact12": apps_facts[app].get(12), "app_fact26": apps_facts[app].get(26),
                "pred_app12": pred_app.get(12), "pred_app26": pred_app.get(26),
                "m_geo": m_geo,
            })
    return segments


def metric_a(segments):
    lines = []
    for h in (12, 26):
        map_key, emp_key, fact_key = f"map{h}", f"emp{h}", f"fact{h}"
        map_errs, emp_errs = [], []
        for s in segments:
            f = s[fact_key]
            if f is None or (isinstance(f, float) and np.isnan(f)) or f <= 0:
                continue
            map_errs.append(abs(s[map_key] - f) / f)
            emp_errs.append(abs(s[emp_key] - f) / f)
        lines.append((h, np.median(map_errs) * 100 if map_errs else float("nan"),
                      np.median(emp_errs) * 100 if emp_errs else float("nan"), len(map_errs)))
    return lines


def metric_b(segments):
    lines = []
    for h in (12, 26):
        map_key, emp_key, fact_key = f"map{h}", f"emp{h}", f"fact{h}"
        wins, n = 0, 0
        for s in segments:
            f = s[fact_key]
            if f is None or (isinstance(f, float) and np.isnan(f)) or f <= 0:
                continue
            n += 1
            if abs(s[map_key] - f) < abs(s[emp_key] - f):
                wins += 1
        lines.append((h, wins, n, (wins / n * 100) if n else float("nan")))
    return lines


def metric_c(segments):
    out = {}
    for h in (12, 26):
        fact_key, map_key, app_fact_key, pred_app_key = f"fact{h}", f"map{h}", f"app_fact{h}", f"pred_app{h}"
        all_match, all_total = 0, 0
        bet_match, bet_total = 0, 0
        for s in segments:
            f, af = s[fact_key], s[app_fact_key]
            if f is None or af is None or (isinstance(f, float) and np.isnan(f)) or (isinstance(af, float) and np.isnan(af)):
                continue
            sign_fact = np.sign(f - af)
            sign_pred = np.sign(s[map_key] - s[pred_app_key])
            if sign_fact == 0 or sign_pred == 0:
                continue
            match = sign_fact == sign_pred
            all_total += 1
            all_match += match
            if abs(s["m_geo"] - 1) > BET_THRESHOLD:
                bet_total += 1
                bet_match += match
        out[h] = {
            "all": (all_match, all_total, all_match / all_total * 100 if all_total else float("nan")),
            "bet": (bet_match, bet_total, bet_match / bet_total * 100 if bet_total else float("nan")),
        }
    return out


def write_report(segments, sanity_worst, sanity_app, met_a, met_b, met_c):
    lines = [
        "# Сегментный бэктест: точность map ВНУТРИ аппа по гео\n",
        "Вопрос: правильно ли карта различает сегменты внутри одного аппа — то, "
        "чего empirical не умеет в принципе (одна кривая на апп). models/map_model.py "
        "не менялся, подмена hr реализована локально в segment_backtest.py "
        f"(см. app_hr()/segment_curve()). Прогнозы на pw={PW} нед данных, сегмент = "
        f"(app_id, geo после _collapse_small), порог >= {SEG_MIN_SUBS} подписчиков и "
        f"mature >= {SEG_MIN_MATURE12} на нед12.\n",
        f"**Санити (п.5)**: худшее расхождение взвешенной суммы map-сегментов "
        f"(все гео-группы аппа, без порога) против прогноза всего аппа на нед12 = "
        f"{sanity_worst * 100:.3f} п.п. (апп {sanity_app}), допуск {SANITY_TOL * 100:.0f} п.п. — "
        f"{'ПРОЙДЕНО' if sanity_worst <= SANITY_TOL else 'ПРОВАЛЕНО'}.\n",
        f"Всего сегментов, прошедших порог: **{len(segments)}**.\n",
    ]

    lines.append("## а) Медиана |отн.ошибки|: map vs empirical\n")
    lines.append("| горизонт | map | empirical | n |")
    lines.append("|---|---|---|---|")
    for h, m, e, n in met_a:
        lines.append(f"| нед{h} | {m:.1f}% | {e:.1f}% | {n} |")
    lines.append("")

    lines.append("## б) Доля сегментов, где map ближе к факту, чем empirical\n")
    lines.append("| горизонт | map ближе | n | доля |")
    lines.append("|---|---|---|---|")
    for h, wins, n, pct in met_b:
        lines.append(f"| нед{h} | {wins} | {n} | {pct:.1f}% |")
    lines.append("")

    lines.append("## в) Тест направления (главный)\n")
    lines.append("Знак (факт_сегмента - факт_аппа) vs знак (прогноз_map_сегмента - прогноз_map_аппа).\n")
    lines.append(f"50% = карта внутри аппа бесполезна (монетка); >{DIRECTION_OK*100:.0f}% на явных "
                  f"ставках (|M_geo-1|>{BET_THRESHOLD}) = карта работает.\n")
    lines.append("| горизонт | все сегменты (совпадений/n, доля) | явные ставки (совпадений/n, доля) |")
    lines.append("|---|---|---|")
    for h in (12, 26):
        am, at, ap = met_c[h]["all"]
        bm, bt, bp = met_c[h]["bet"]
        ap_s = f"{am}/{at} ({ap:.1f}%)" if at else "n/д"
        bp_s = f"{bm}/{bt} ({bp:.1f}%)" if bt else "n/д"
        lines.append(f"| нед{h} | {ap_s} | {bp_s} |")
    lines.append("")

    lines.append(f"## г) Топ-{TOP_N} пар по объёму (подписчиков)\n")
    lines.append(f"| app | geo | n_subs | факт12 | map12 | emp12 | кто ближе | флаг {FLAG_APP} |")
    lines.append("|---|---|---|---|---|---|---|---|")
    top = sorted(segments, key=lambda s: -s["n_subs"])[:TOP_N]
    for s in top:
        f12 = s["fact12"]
        closer = "н/д"
        if f12 is not None and not (isinstance(f12, float) and np.isnan(f12)) and f12 > 0:
            closer = "map" if abs(s["map12"] - f12) < abs(s["emp12"] - f12) else "empirical"
        flag = "да" if s["app"] == FLAG_APP else "-"
        f12_s = f"{f12:.4f}" if f12 == f12 else "н/д"
        lines.append(f"| {s['app']} | {s['geo']} | {s['n_subs']} | {f12_s} | "
                      f"{s['map12']:.4f} | {s['emp12']:.4f} | {closer} | {flag} |")
    lines.append("")

    am_all, at_all, ap_all = met_c[12]["all"]
    bm_all, bt_all, bp_all = met_c[12]["bet"]
    if bt_all >= 5:
        verdict = (f"**Карта работает внутри аппа**: на явных гео-ставках (нед12) совпадение направления "
                    f"{bp_all:.0f}% ({bm_all}/{bt_all}) {'>' if bp_all > DIRECTION_OK*100 else '<='} порога "
                    f"{DIRECTION_OK*100:.0f}%." if bp_all > DIRECTION_OK * 100 else
                    f"**Карта НЕ подтвердила пользу внутри аппа**: на явных гео-ставках (нед12) совпадение "
                    f"направления {bp_all:.0f}% ({bm_all}/{bt_all}) не превышает порог {DIRECTION_OK*100:.0f}%.")
    else:
        verdict = (f"**Недостаточно явных гео-ставок** (нед12: {bt_all} сегментов с |M_geo-1|>{BET_THRESHOLD}) "
                    f"для надёжного вердикта по главному критерию; на всех сегментах направление совпало "
                    f"{ap_all:.0f}% ({am_all}/{at_all}).")
    lines.append("## д) Вердикт\n")
    lines.append(verdict)

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"отчёт записан: {OUT_PATH}")


def main():
    mx = common.load_matrix()
    apps, facts, matures, crash_flag, crash_apps = common.select_apps(mx)
    apps_info = (apps, facts, matures, crash_flag, crash_apps)
    clean_apps = [a for a in apps if not crash_flag[a]]
    print(f"чистых аппов: {len(clean_apps)}")

    map_state = map_model.fit(mx, apps_info)
    emp_state = empirical.fit(mx)

    print("санити (п.5)...")
    worst, worst_app = sanity_check(mx, clean_apps, map_state)
    print(f"худшее расхождение реконструкции: {worst*100:.4f} п.п. (апп {worst_app})")
    if worst > SANITY_TOL:
        raise SystemExit(
            f"САНИТИ ПРОВАЛЕНО: расхождение {worst*100:.3f} п.п. > допуска {SANITY_TOL*100:.0f} п.п. "
            f"(апп {worst_app}) — похоже на баг в подмене hr. СТОП, отчёт не пишется."
        )

    print("строю сегменты...")
    segments = build_segments(mx, clean_apps, facts, map_state, emp_state)
    print(f"сегментов, прошедших порог ({SEG_MIN_SUBS} подписчиков, mature>={SEG_MIN_MATURE12} нед12): {len(segments)}")

    met_a = metric_a(segments)
    met_b = metric_b(segments)
    met_c = metric_c(segments)

    write_report(segments, worst, worst_app, met_a, met_b, met_c)


if __name__ == "__main__":
    main()
