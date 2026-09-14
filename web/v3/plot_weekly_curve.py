"""
Charts for the weekly curve table -- a visual sanity check on what it contains.

Four panels:
  1. retention by week for the biggest cells, so shapes can be compared
  2. rebills per week for one cell, with the fact/model handover marked
  3. cumulative revenue per payer
  4. upsell checkpoints, which are why rebills_ups is zero most weeks

Writes: reports/web_v3/plots/weekly_curve.png
Run: .venv/Scripts/python.exe web/v3/plot_weekly_curve.py
"""
import os
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
os.chdir(ROOT)

from ltv_v3.config import OUT_DIR  # noqa: E402

OUT_PNG = ROOT / OUT_DIR / "plots" / "weekly_curve.png"
KEY = ["cohort_date", "funnel", "utm_source"]


def main():
    src = sorted(Path(OUT_DIR).glob("weekly_curve_v3_*.csv"))[-1]
    w = pd.read_csv(src)
    print(f"source: {src} ({len(w)} rows)")

    sizes = w.groupby(KEY)["n_base_payer"].first().sort_values(ascending=False)
    top = sizes.head(6).index.tolist()

    fig, axes = plt.subplots(2, 2, figsize=(16, 11))
    colors = plt.cm.tab10(np.linspace(0, 1, 10))

    # ---------------- 1. retention shapes
    ax = axes[0][0]
    for i, key in enumerate(top):
        c = w[(w[KEY] == pd.Series(key, index=KEY)).all(axis=1)].sort_values("week")
        label = f"{key[0]} {key[1][:22]} ({int(c['n_base_payer'].iloc[0])})"
        ax.plot(c["week"], c["active_share"], lw=1.8, color=colors[i], label=label)
    ax.set_title("1. Доля ещё платящих, по неделям (6 крупнейших ячеек)", loc="left", fontsize=12)
    ax.set_xlabel("неделя"); ax.set_ylabel("доля от плательщиков ячейки")
    ax.set_xlim(0, 104); ax.set_ylim(0, 1); ax.grid(alpha=0.3); ax.legend(fontsize=7)

    # ---------------- 2. fact -> model handover, one cell
    ax = axes[0][1]
    key = top[0]
    c = w[(w[KEY] == pd.Series(key, index=KEY)).all(axis=1)].sort_values("week")
    fact, model = c[c["source"] == "fact"], c[c["source"] == "model"]
    ax.plot(model["week"], model["rebills_base"], lw=2, color="#1f77b4", label="model")
    ax.plot(fact["week"], fact["rebills_base"], lw=0, marker="s", ms=8,
            color="#2ca02c", label="fact (наблюдено)")
    if len(fact):
        ax.axvline(fact["week"].max(), color="#d62728", ls="--", lw=1.5)
        ax.text(fact["week"].max() + 1.5, ax.get_ylim()[1] * 0.8,
                "← факт | прогноз →", fontsize=9, color="#d62728")
    ax.set_title(f"2. Ребиллы в неделю: {key[0]} × {key[1][:26]}", loc="left", fontsize=12)
    ax.set_xlabel("неделя"); ax.set_ylabel("платежей в эту неделю")
    ax.set_xlim(0, 104); ax.grid(alpha=0.3); ax.legend(fontsize=9)

    # ---------------- 3. cumulative revenue per payer
    ax = axes[1][0]
    for i, k in enumerate(top):
        c = w[(w[KEY] == pd.Series(k, index=KEY)).all(axis=1)].sort_values("week")
        n = c["n_base_payer"].iloc[0]
        if n:
            ax.plot(c["week"], c["revenue_cum"] / n, lw=1.8, color=colors[i],
                    label=f"{k[0]} {k[1][:22]}")
    ax.set_title("3. Накопленная выручка на плательщика", loc="left", fontsize=12)
    ax.set_xlabel("неделя"); ax.set_ylabel("$ на плательщика")
    ax.set_xlim(0, 104); ax.grid(alpha=0.3); ax.legend(fontsize=7)

    # ---------------- 4. why rebills_ups is mostly zero
    ax = axes[1][1]
    c = w[(w[KEY] == pd.Series(top[0], index=KEY)).all(axis=1)].sort_values("week")
    ax.bar(c["week"], c["rebills_ups"], width=0.9, color="#ff7f0e", label="апселл (чекпоинты)")
    ax.plot(c["week"], c["rebills_base"], lw=1.5, color="#1f77b4", alpha=0.5, label="база (каждую неделю)")
    ax.set_title("4. Апселл списывается раз в 4 недели, база — каждую", loc="left", fontsize=12)
    ax.set_xlabel("неделя"); ax.set_ylabel("платежей")
    ax.set_xlim(0, 52); ax.grid(alpha=0.3); ax.legend(fontsize=9)

    fig.suptitle("Недельная кривая v3 — визуальная проверка таблицы", fontsize=13, y=0.995)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PNG, dpi=140)
    plt.close(fig)
    print(f"wrote {OUT_PNG}")

    # ---------------- what the table looks like for the plotted cell
    print(f"\nячейка на панелях 2 и 4: {top[0]}")
    print(c[c["week"] <= 12][["week", "rebills_base", "rebills_ups", "active_share", "source"]]
          .to_string(index=False))


if __name__ == "__main__":
    main()
