"""
What does each lever actually buy, and is the funnel lever worth having?

Runs the SAME LOO folds as validate_loo_web.py under several lever
configurations. Same cohorts, same horizons, same metric, same data -- only the
lever set changes, so the delta between rows is attributable to the levers and
nothing else.

TWO QUESTIONS

1. Does `funnel` earn its place? It is the one lever iOS does not have, so
   nothing about it is inherited -- it is fitted from scratch on ~8k web rows.
   Compared against the same model without it.

2. Does the residual-chain ORDER matter? Levers are fitted one after another on
   the residual left by the previous ones, so a lever placed first collects the
   raw effect and later ones only get what is left. funnel and utm_source are
   correlated (buyers run particular funnels), so the order decides who is
   credited for the shared variance. Both orders are run rather than argued.

Also writes the multiplier tables themselves, with n per category, because a
scoreboard cannot tell you whether "Brazil x1.3" is sensible and a human
glancing at the table can.

Writes: reports/web_v3/lever_study_web.md
        reports/web_v3/map_multipliers_web.md
(nothing else, ever)

Run: .venv/Scripts/python.exe web/v3/measure_levers_web.py
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

from ltv_v3.config import OUT_DIR, MAP_MIN_ROWS_WEB, SANITY_TOL  # noqa: E402
from ltv_v3 import map_web as M, se_training_web as S  # noqa: E402
import validate_loo_web as V  # noqa: E402

STUDY_PATH = ROOT / OUT_DIR / "lever_study_web.md"
MULT_PATH = ROOT / OUT_DIR / "map_multipliers_web.md"

CONFIGS = [
    ("без рычагов вообще", []),
    ("только geo+billday (как iOS умеет)", ["geo", "billday_bin"]),
    ("+ utm_source, без воронки", ["utm_source", "geo", "billday_bin"]),
    ("ПОЛНЫЙ: funnel -> utm -> geo -> billday", ["funnel", "utm_source", "geo", "billday_bin"]),
    ("порядок наоборот: utm -> funnel -> geo -> billday", ["utm_source", "funnel", "geo", "billday_bin"]),
]


def score(detail: pd.DataFrame) -> dict:
    """v3 column only -- the v2 baseline is lever-free by construction."""
    out = {}
    for label, prefix in (("surv", ""), ("ltv", "ltv_")):
        col = f"{prefix}err_v3_formula"
        sub = detail.dropna(subset=[col]) if col in detail else pd.DataFrame()
        out[f"{label}_abs"] = sub[col].abs().median() if len(sub) else np.nan
        out[f"{label}_sign"] = sub[col].median() if len(sub) else np.nan
        out[f"{label}_n"] = len(sub)
    return out


def main():
    data = V.load_inputs()
    print()

    results = []
    for name, levers in CONFIGS:
        detail = V.run_folds(data, levers=levers, verbose=False)
        row = {"config": name, "levers": ", ".join(levers) if levers else "—"}
        row.update(score(detail))
        results.append(row)
        print(f"{name:48s} surv {row['surv_abs']:.4f}  ltv {row['ltv_abs']:.4f}  n={row['surv_n']}")

    res = pd.DataFrame(results)
    write_study(res)
    write_multipliers(data)


def write_study(res: pd.DataFrame):
    full = res[res["config"].str.startswith("ПОЛНЫЙ")].iloc[0]
    nofunnel = res[res["config"].str.startswith("+ utm_source")].iloc[0]
    reverse = res[res["config"].str.startswith("порядок наоборот")].iloc[0]

    lines = [
        "# Вклад рычагов — веб v3",
        "",
        "Те же LOO-фолды, что в `loo_web.md`: те же когорты, горизонты, метрика и",
        "данные. Меняется ТОЛЬКО набор рычагов, поэтому дельта между строками",
        "относится к рычагам и ни к чему больше.",
        "",
        "Колонка `v3` из общего харнесса; baseline `v2` лишён рычагов по построению",
        "и здесь не повторяется.",
        "",
        "## Результат",
        "",
        "| конфигурация | рычаги | дожитие \\|ош\\| | дожитие знак | LTV \\|ош\\| | LTV знак | n |",
        "|---|---|---|---|---|---|---|",
    ]
    for _, r in res.iterrows():
        lines.append(
            f"| {r['config']} | `{r['levers']}` | {r['surv_abs']:.1%} | {r['surv_sign']:+.1%} | "
            f"{r['ltv_abs']:.1%} | {r['ltv_sign']:+.1%} | {int(r['surv_n'])} |"
        )

    d_surv = full["surv_abs"] - nofunnel["surv_abs"]
    d_ltv = full["ltv_abs"] - nofunnel["ltv_abs"]
    d_ord_s = reverse["surv_abs"] - full["surv_abs"]
    d_ord_l = reverse["ltv_abs"] - full["ltv_abs"]

    lines += [
        "",
        "## Вклад воронки",
        "",
        f"- дожитие: {nofunnel['surv_abs']:.1%} → {full['surv_abs']:.1%} "
        f"({d_surv:+.1%} п.п., {'воронка помогает' if d_surv < 0 else 'воронка не помогает'})",
        f"- LTV: {nofunnel['ltv_abs']:.1%} → {full['ltv_abs']:.1%} "
        f"({d_ltv:+.1%} п.п., {'воронка помогает' if d_ltv < 0 else 'воронка не помогает'})",
        "",
        "Воронка — единственный рычаг, которого нет в iOS, поэтому он не наследует",
        "ничего и учится с нуля на ~8k веб-строк.",
        "",
        "## Порядок в residual-цепочке",
        "",
        f"- `funnel → utm`: дожитие {full['surv_abs']:.1%}, LTV {full['ltv_abs']:.1%}",
        f"- `utm → funnel`: дожитие {reverse['surv_abs']:.1%}, LTV {reverse['ltv_abs']:.1%}",
        f"- разница: дожитие {d_ord_s:+.1%} п.п., LTV {d_ord_l:+.1%} п.п.",
        "",
        "Каждый следующий рычаг фитится на остатке после предыдущих, поэтому",
        "стоящий первым забирает сырой эффект, а остальным достаётся остаток.",
        "Воронка и источник скоррелированы (байеры крутят определённые воронки),",
        "так что порядок решает, кому достанется общая дисперсия.",
    ]
    STUDY_PATH.parent.mkdir(parents=True, exist_ok=True)
    STUDY_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {STUDY_PATH}")


def write_multipliers(data: dict):
    """The multiplier tables themselves -- for eyes, not for a scoreboard."""
    state = M.fit(data["train_all"], ios_h_base=data["ios_h"])
    sanity = M.sanity_check(state)

    lines = [
        "# Карта множителей — веб v3",
        "",
        "Фит на всей обучающей выборке (не LOO). Множитель = во сколько раз",
        "категория меняет риск отвала относительно ожидаемого ПОСЛЕ учёта",
        "предыдущих рычагов цепочки. Клип (0.4, 2.5).",
        "",
        f"Порог схлопывания в `other`: {MAP_MIN_ROWS_WEB} строк "
        f"(в iOS/v2 — 2000; при ~8k строк веб-матрицы такой порог оставил бы 2 воронки).",
        "",
        "## Санити",
        "",
        f"Взвешенное среднее множителей каждого рычага должно лежать в "
        f"({SANITY_TOL[0]}, {SANITY_TOL[1]}). Проверяется ДО любых замеров точности —",
        "урок бага `rr` в `hybrid.py`: рычаг может выглядеть прилично на табло и",
        "быть бессмыслицей.",
        "",
        "| рычаг | взвеш. среднее | в допуске | категорий | схлопнуто в other | строк |",
        "|---|---|---|---|---|---|",
    ]
    for _, r in sanity.iterrows():
        lines.append(
            f"| `{r['lever']}` | {r['weighted_mean']:.3f} | {'да' if r['ok'] else '**НЕТ**'} | "
            f"{int(r['categories'])} | {int(r['collapsed_to_other'])} | {int(r['rows'])} |"
        )

    lines += ["", f"`h_base` свои данные до ступени **{state['k_max_web']}**, "
                  f"дальше сращен хвост iOS.", ""]
    lines.append("h_base[1..12]: " + " ".join(f"{state['h_base'][k]:.3f}" for k in range(1, 13)))

    for lever in state["levers"]:
        g = state["support"][lever]
        mult = pd.Series({k: v for k, v in state["multipliers"][lever].items() if k != "__default__"})
        tbl = g.join(mult.rename("mult")).sort_values("n", ascending=False)
        lines += [
            "", f"## `{lever}`", "",
            "| категория | множитель | строк | доля | смысл |",
            "|---|---|---|---|---|",
        ]
        total = tbl["n"].sum()
        for cat, r in tbl.iterrows():
            m = r["mult"]
            if pd.isna(m):
                continue
            sense = "риск выше" if m > 1.05 else ("риск ниже" if m < 0.95 else "нейтрально")
            lines.append(f"| `{cat}` | **{m:.3f}** | {int(r['n'])} | {r['n']/total:.1%} | {sense} |")

    MULT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {MULT_PATH}")


if __name__ == "__main__":
    main()
