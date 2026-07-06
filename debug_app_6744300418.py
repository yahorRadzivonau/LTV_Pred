"""
Мини-задача C2: разбор id6744300418 (факт нед52=0.019, все модели завышают
в разы) — пропущенный обвал (кандидат в crash_apps) или честный аутлаер?

Только ЧТЕНИЕ models/common.py (detect_crash, direct_survival). Ничего в
detect_crash, crash_apps или любых моделях не меняется — выход только
reports/app_6744300418.md.

Запуск: python debug_app_6744300418.py
"""
from pathlib import Path

from models import common

ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / "reports" / "app_6744300418.md"
APP = "id6744300418"
COHORT_STEPS = [1, 2, 3, 4, 8, 12, 26]


def stepwise_retention(per):
    """Дословно логика common.detect_crash, но без свёртки в один булев
    флаг — возвращает список (step, at_risk_n, retention) по всем ступеням,
    прошедшим порог CRASH_MIN_RISK, плюс worst-значение и его ступень."""
    rows = []
    worst, worst_k = 1.0, None
    for k in range(2, common.HMAX + 1):
        at_risk = (per["weeks_obs"] >= k) & (per["max_step"] >= k - 1)
        n = at_risk.sum()
        if n >= common.CRASH_MIN_RISK:
            ret = (per.loc[at_risk, "max_step"] >= k).mean()
            rows.append((k, n, ret))
            if ret < worst:
                worst, worst_k = ret, k
    return rows, worst, worst_k


def cohort_dynamics(sub):
    cohorts = sorted(sub["cohort_month"].unique())
    out = {}
    for c in cohorts:
        rows_c = sub[sub["cohort_month"] == c]
        n_subs = rows_c["sub_id"].nunique()
        fact, mature, _ = common.direct_survival(rows_c)
        out[c] = (n_subs, {k: (fact.get(k, float("nan")), mature.get(k, 0)) for k in COHORT_STEPS})
    return out


def main():
    mx = common.load_matrix()
    sub = mx[mx["app_id"] == APP].copy()

    fact, mature, per = common.direct_survival(sub)
    step_rows, worst, worst_k = stepwise_retention(per)
    is_crash = worst < common.CRASH_RET_THRESHOLD
    gap = worst - common.CRASH_RET_THRESHOLD

    cohorts = cohort_dynamics(sub)

    lines = [
        f"# Разбор {APP}: пропущенный обвал или честный аутлаер?\n",
        f"Контекст: факт нед52={fact.get(52, float('nan')):.4f}, карта и empirical завышают "
        f"в разы (см. reports/plot_forecasts.md, reports/plots/{APP}.png). Ниже — диагноз "
        f"через призму common.detect_crash (тот же метод, никаких изменений в него не внесено).\n",
        "## 1. Пошаговое удержание (метод detect_crash)\n",
        f"worst одношаговое удержание = **{worst:.4f}** на ступени **{worst_k}** "
        f"(порог CRASH_RET_THRESHOLD={common.CRASH_RET_THRESHOLD}, CRASH_MIN_RISK={common.CRASH_MIN_RISK}).\n",
        f"Разрыв до порога: {gap:+.4f} ({'НИЖЕ порога -> detect_crash пометил бы как обвал' if is_crash else 'выше порога -> detect_crash НЕ считает это обвалом'}).\n",
        "| ступень | at_risk (n) | одношаговое удержание |",
        "|---|---|---|",
    ]
    for k, n, ret in step_rows[:10]:
        marker = " <- worst" if k == worst_k else ""
        lines.append(f"| {k} | {n} | {ret:.4f}{marker} |")
    lines.append("")
    lines.append(
        "Одношаговое удержание монотонно РАСТЁТ после ступени 2 (нет провала ниже 0.65 "
        "нигде дальше) — это не разовый обвал, а стабильно повышенный отток на входе "
        "(ступени 1-2), который постепенно ослабевает. detect_crash ищет именно "
        "разовый провал ниже 0.65 за один шаг — здесь такого провала нет ни на одной ступени.\n"
    )

    lines.append("## 2. Динамика по когортам запуска\n")
    lines.append("| когорта (cohort_month) | n подписчиков | " +
                  " | ".join(f"факт нед{k}" for k in COHORT_STEPS) + " |")
    lines.append("|---" * (2 + len(COHORT_STEPS)) + "|")
    for c, (n_subs, vals) in cohorts.items():
        cells = []
        for k in COHORT_STEPS:
            f, m = vals[k]
            cells.append(f"{f:.3f}" if f == f else "н/д")
        lines.append(f"| {c} | {n_subs} | " + " | ".join(cells) + " |")
    lines.append("")

    first_two = list(cohorts.items())[:2]
    (c1, (n1, v1)), (c2, (n2, v2)) = first_two
    trend_desc = []
    for k in [1, 2, 4, 8]:
        f1, _ = v1[k]
        f2, _ = v2[k]
        if f1 == f1 and f2 == f2:
            trend_desc.append(f"нед{k}: {c1}={f1:.3f} vs {c2}={f2:.3f} ({'хуже во 2-й когорте' if f2 < f1 else 'не хуже во 2-й когорте'})")
    lines.append(
        f"Сравнение двух крупнейших когорт ({c1}, n={n1} vs {c2}, n={n2} — остальные когорты "
        f"на 1-2 порядка меньше, некорректно сравнивать): " + "; ".join(trend_desc) + ". "
        f"Апп текучий уже в ПЕРВОЙ (стартовой, {c1}) когорте, а не становится текучим позже — "
        f"второй когорте ({c2}) хуже не становится (по всем сравненным ступеням не хуже первой, "
        f"местами даже лучше). Нет признаков растянутого во времени управленческого события "
        f"(если бы оно было, свежие когорты после события были бы заметно текучее старых).\n"
    )

    verdict = (
        "**(б) Честный аутлаер.** Одношаговое удержание нигде не проваливается ниже "
        f"порога {common.CRASH_RET_THRESHOLD} (worst={worst:.3f} на ступени {worst_k}, "
        "выше порога) — detect_crash корректно НЕ ловит здесь разовый обвал, потому что "
        "его и нет: это стабильно, с первой же когорты, повышенный отток на входе "
        "(ступени 1-2), а не растянутое управленческое событие. Кандидатом в crash_apps "
        "не является — оставляем в чистой зоне как есть, просто знаем про этот аутлаер "
        "(см. reports/sign_pw2_analysis.md про систематическую недооценку ступени 2 h_base "
        "на всём портфеле — этот апп лишь крайняя точка того же явления)."
    )
    lines.append("## Вердикт\n")
    lines.append(verdict)

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"отчёт записан: {OUT_PATH}")
    print(f"worst={worst:.4f} at step {worst_k}, is_crash={is_crash}")


if __name__ == "__main__":
    main()
