"""
Шаг 2 из MAP_MODEL_SPEC.md — санити множителей MAP (ДО любого бэктеста).

Запуск (из корня репозитория):
    python -m models.map_sanity

2.1 — ассерт: взвешенное (по n строк категории в чистой зоне) среднее
множителей каждого рычага должно лежать в SANITY_TOL. Полная таблица
категорий/множителей/n печатается ДО ассерта — если он упадёт, диагностика
уже будет на экране (падать, не подгонять).

2.2 — пишет reports/map_multipliers.md: по каждому рычагу все категории
(множитель, n, доля чистой зоны), топ-10 по |множитель-1| среди категорий
с n>=10000, число категорий, ушедших в "other".

2.3 — согласие с hybrid_v2: для двух аппов-выбросов по rr (найдены на
Задаче 1b: id6760619107, id6761661333) считает средний личный множитель
их пользователей по карте (models.map_model.personal_multiplier — механика
Шага 3.1, вызванная заранее только ради этого сравнения) и печатает рядом
с их же rr_hybrid_v2.
"""
from pathlib import Path

import numpy as np

from models import common, hybrid_v2, map_model

ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = ROOT / "reports" / "map_multipliers.md"

OUTLIER_APPS = ["id6760619107", "id6761661333"]  # найдены hybrid_v2 по rr (Task1b)


def _collapsed_clean_zone(state, mx):
    """Пересобирает ту же чистую зону, что видел fit(), и схлопывает те же
    мелкие категории в "other" (map_model._collapse_small, по
    state["small_categories"]), чтобы n по категориям совпадало с ключами
    state["multipliers"]. Не пересчитывает сами множители — они уже в state."""
    mx_prepped = map_model._prep(mx)
    cz = map_model._clean_zone(mx_prepped, state["crash_apps"])
    cz = map_model._collapse_small(cz, state["small_categories"])
    return cz


def build_lever_tables(state, cz):
    """{lever: DataFrame[категория, n, mult]}"""
    tables = {}
    for lever in map_model.MAP_LEVERS:
        g = cz.groupby(lever).size().rename("n").reset_index()
        mult_map = state["multipliers"][lever]
        g["mult"] = g[lever].map(mult_map)
        tables[lever] = g
    return tables


def sanity_check(tables):
    """2.1 — печатает таблицу каждого рычага, потом ассертит допуск SANITY_TOL."""
    weighted_means = {}
    for lever in map_model.MAP_LEVERS:
        g = tables[lever]
        w_mean = (g["mult"] * g["n"]).sum() / g["n"].sum()
        weighted_means[lever] = w_mean
        print(f"--- {lever}: взвешенное среднее множителей = {w_mean:.4f} "
              f"(допуск {map_model.SANITY_TOL}) ---")
        print(g.sort_values("n", ascending=False).to_string(index=False))
        print()

    for lever, w_mean in weighted_means.items():
        assert map_model.SANITY_TOL[0] <= w_mean <= map_model.SANITY_TOL[1], (
            f"{lever}: взвешенное среднее множителей {w_mean:.4f} вне допуска {map_model.SANITY_TOL}"
        )
    print("Все ассерты 2.1 пройдены (взвешенное среднее каждого рычага в допуске).\n")


def write_multipliers_report(state, tables, cz_len):
    lines = [
        "# Множители карты (models/map_model.py) — Шаг 2 санити\n",
        f"Рычаги (порядок residual-цепочки): {', '.join(map_model.MAP_LEVERS)}. "
        f"Чистая зона (step_k<={map_model.CLEAN_STEP_MAX}, weeks_obs>=step_k, "
        f"без crash-аппов): {cz_len} строк.\n",
    ]

    lines.append("## Множители по рычагам\n")
    for lever in map_model.MAP_LEVERS:
        g = tables[lever].copy()
        g["share_cz_pct"] = (g["n"] / cz_len * 100).round(3)
        lines.append(f"### {lever}\n")
        lines.append("| категория | множитель | n | доля чистой зоны |")
        lines.append("|---|---|---|---|")
        for _, row in g.sort_values("n", ascending=False).iterrows():
            lines.append(f"| {row[lever]} | {row['mult']:.4f} | {int(row['n'])} | {row['share_cz_pct']:.3f}% |")
        n_other = len(state["small_categories"][lever])
        lines.append(f"\nКатегорий ушло в \"other\" (n < {map_model.MAP_MIN_ROWS} строк в чистой зоне): {n_other}\n")

        if lever == "media_source":
            lines.append(
                "> **Сноска:** эффект безатрибуционных юзеров учтён в `geo='(none)'` "
                "(этот рычаг идёт первым в residual-цепочке, до media_source). "
                "Множитель `unknown` показан ПОСЛЕ вычета этого эффекта (residual) — "
                "он получился ≈1.0000 не потому что unknown-трафик сам по себе "
                "нормальный, а потому что `geo='(none)'` уже забрал весь этот сигнал "
                "на себя раньше в цепочке: `geo='(none)'` и `media_source='unknown'` — "
                "практически одна и та же подгруппа строк (14.12% матрицы, пересечение "
                "375 847 из 2 662 142 строк, см. диагностику в models/map_preflight.py). "
                "НЕ читать эту таблицу как \"unknown-трафик нормальный\" — правильное "
                "чтение: \"эффект безатрибуционных юзеров есть, он уже учтён в geo\".\n"
            )

    lines.append("## Топ-10 по |множитель - 1| среди категорий с n >= 10000\n")
    lines.append("(категория \"other\" исключена из ранжирования — это сборная категория мелких хвостов, не отдельный рычаг)\n")
    all_rows = []
    for lever in map_model.MAP_LEVERS:
        g = tables[lever]
        g = g[(g["n"] >= 10000) & (g[lever] != "other")]
        for _, row in g.iterrows():
            all_rows.append((lever, row[lever], row["mult"], row["n"]))
    all_rows.sort(key=lambda r: abs(r[2] - 1.0), reverse=True)
    lines.append("| рычаг | категория | множитель | n |")
    lines.append("|---|---|---|---|")
    for lever, cat, mult, n in all_rows[:10]:
        lines.append(f"| {lever} | {cat} | {mult:.4f} | {int(n)} |")

    lines.append("\n## Известное ограничение: plan_interval исключён из рычагов\n")
    lines.append(
        "plan_interval был в модели на Шаге 1 и убран после находки владельца: он "
        "выводится из медианы зазора между платежами (`pipeline/se_training.py`), "
        "поэтому категория `unknown` в значительной мере означает \"умер после "
        "первого платежа\" — утечка таргета, не содержательный рычаг. "
        "См. `MAP_MODEL_SPEC.md`, раздел \"Известные ловушки\".\n\n"
        "**Открытый вопрос (не проверялся отдельным прогоном, фиксируется как есть):** "
        "`models/logreg.py` и `models/hybrid_v2.py` используют plan_interval как "
        "обычную фичу/рычаг и НЕ были исправлены (их логику менять нельзя по "
        "правилам проекта) — их точность на ранних горизонтах (нед12), особенно "
        "при прогнозе по 1 неделе данных, может быть слегка приукрашена той же "
        "утечкой (по plan_interval легко угадать, дожил ли юзер вообще до 2-го "
        "платежа). Величина эффекта не измерена — это не ошибка их чисел, а "
        "предупреждение о том, что сравнение map vs logreg/hybrid_v2 на нед12 "
        "не совсем на равных условиях.\n"
    )

    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"отчёт записан: {REPORT_PATH}")


def outlier_comparison(mx, state, hybrid_state):
    print("=== 2.3 Согласие с hybrid_v2: аппы-выбросы по rr ===")
    for app in OUTLIER_APPS:
        app_rows = mx[mx["app_id"] == app]
        if len(app_rows) == 0:
            print(f"{app}: не найден в матрице (пропускаю)")
            continue

        combos = map_model.personal_multiplier(state, app_rows)
        map_mean_mult = float(np.average(combos["mult"].values, weights=combos["w"].values))

        _, rr, _ = hybrid_v2._precompute_app(hybrid_state, app_rows)

        print(f"{app}: rr_hybrid_v2={rr:.3f}  map_mean_mult={map_mean_mult:.3f}")
    print()


def main():
    mx = common.load_matrix()
    apps_info = common.select_apps(mx)
    crash_apps = apps_info[4]

    state = map_model.fit(mx, apps_info)
    cz = _collapsed_clean_zone(state, mx)
    tables = build_lever_tables(state, cz)

    print("=== 2.1 Санити множителей ===")
    sanity_check(tables)

    print("=== 2.2 Отчёт map_multipliers.md ===")
    write_multipliers_report(state, tables, len(cz))
    print()

    mx_clean = mx[~mx["app_id"].isin(crash_apps)].copy()
    hybrid_state = hybrid_v2.fit(mx_clean)
    outlier_comparison(mx, state, hybrid_state)


if __name__ == "__main__":
    main()
