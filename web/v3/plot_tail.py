"""
What the tail actually looks like: hazard, survival, and cumulative LTV out to
week 104, with the boundary between measurement and assumption drawn on.

The whole point is the vertical line at k_max_web. Left of it the curve is
fitted on web data; right of it it rides a tail borrowed from iOS and rescaled
to join without a step. Numbers at weeks 26/52/104 all live on the right side.

Writes: reports/web_v3/plots/tail.png
Run: .venv/Scripts/python.exe web/v3/plot_tail.py
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

from core import common  # noqa: E402
from ltv_v3.config import (  # noqa: E402
    OUT_DIR, DATA_DIR, MATRIX_GLOB, POPULATION_GLOB, H_EXT, BASE_PRICE,
    RETURN_WINDOW_DAYS,
)
from ltv_v3 import se_training_web as S, map_web as M, money as MON, revenue as R  # noqa: E402

OUT_PNG = ROOT / OUT_DIR / "plots" / "tail.png"
RETURN_WINDOW_WEEKS = RETURN_WINDOW_DAYS / 7.0


def newest(pat):
    return sorted(Path(DATA_DIR).glob(pat))[-1]


def main():
    mx = S.add_survived(pd.read_parquet(newest(MATRIX_GLOB)))
    train = S.training_rows(mx)
    pop = pd.read_parquet(newest(POPULATION_GLOB))
    events = R.load_events()
    max_week = int(pop["age_weeks_now"].max())
    cum = R.per_person_week_cumulative(events, pop, max_week)

    ios_h_raw = common.empirical_hbase(common.load_matrix())
    state = M.fit(train, ios_h_base=ios_h_raw)
    k_max = state["k_max_web"]
    h = state["h_base"]

    # web's own hazard, unspliced -- to show where it actually stops
    web_own = common.empirical_hbase(train).reindex(range(1, H_EXT + 1))

    ks = np.arange(1, H_EXT + 1)
    haz = np.array([h.get(k, h.iloc[-1]) for k in ks])
    surv = np.cumprod(1 - np.clip(haz, 0.001, 0.999))
    curve = pd.Series(surv, index=ks)

    base_people = pop.set_index("email")
    base_people = base_people[base_people["has_base"]]

    fact_w, fact_v = [], []
    for w in range(0, max_week + 1):
        mature = base_people[base_people["age_weeks_now"] >= w + RETURN_WINDOW_WEEKS]
        if len(mature) < 30:
            break
        fact_w.append(w)
        fact_v.append(float(cum.reindex(mature.index)[w].mean()))

    # ANCHOR the projection on the last solid fact, which is what the model
    # actually does in the tables. Projecting from zero instead would show a
    # curve nobody uses -- and it sits ~18% below the observed points, so it
    # would make the model look worse than it is for the wrong reason.
    anchor = fact_w[-1]
    anchor_obs = fact_v[-1]
    ltv = MON.ltv_curve(curve, observed_at_anchor=anchor_obs, anchor=anchor,
                        people=base_people, horizon=H_EXT)

    fig, axes = plt.subplots(3, 1, figsize=(12, 13))
    band = dict(color="#d62728", alpha=0.06)
    vline = dict(color="#d62728", ls="--", lw=1.6)

    # ---------------- 1. hazard
    ax = axes[0]
    ax.plot(ks, haz, lw=2, color="#1f77b4", label="h_base (используется моделью)")
    own = web_own.reindex(ks).values
    ax.plot(ks, own, lw=0, marker="o", ms=4, color="#2ca02c",
            label="свои веб-данные (эмпирика)")
    ax.axvspan(k_max, H_EXT, **band)
    ax.axvline(k_max, **vline)
    ax.text(k_max + 1.5, ax.get_ylim()[1] * 0.85,
            f"← измерено | заимствовано у iOS →", fontsize=10, color="#d62728")
    ax.set_title("1. Риск отвала по ступеням (h_base)", fontsize=13, loc="left")
    ax.set_ylabel("вероятность отвала")
    ax.set_xlim(1, H_EXT)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)

    # ---------------- 2. survival
    ax = axes[1]
    ax.plot(ks, surv, lw=2, color="#1f77b4")
    ax.axvspan(k_max, H_EXT, **band)
    ax.axvline(k_max, **vline)
    for k in (12, 26, 52, 104):
        ax.plot(k, curve.get(k), "o", color="#d62728", ms=6)
        ax.annotate(f"{curve.get(k):.1%}", (k, curve.get(k)), textcoords="offset points",
                    xytext=(6, 8), fontsize=9)
    ax.set_title("2. Дожитие: доля подписчиков, ещё платящих", fontsize=13, loc="left")
    ax.set_ylabel("доля дожития")
    ax.set_xlim(1, H_EXT)
    ax.set_ylim(0, 1)
    ax.grid(alpha=0.3)

    # ---------------- 3. cumulative LTV
    ax = axes[2]
    ax.plot(ltv.index, ltv.values, lw=2, color="#1f77b4",
            label=f"прогноз LTV/подписчика (якорь на неделе {anchor})")
    ax.plot(fact_w, fact_v, lw=0, marker="s", ms=6, color="#2ca02c", label="ФАКТ (наблюдено)")
    ax.axvspan(k_max, H_EXT, **band)
    ax.axvline(k_max, **vline)
    # The LTV series starts at the anchor, so a horizon behind it has no point to
    # label. As data accumulates the anchor moves right and swallows the earlier
    # horizons -- week 12 dropped out once observation reached week 13.
    for k in (12, 26, 52, 104):
        if k not in ltv.index:
            continue
        ax.plot(k, ltv[k], "o", color="#d62728", ms=6)
        ax.annotate(f"${ltv[k]:.0f}", (k, ltv[k]), textcoords="offset points",
                    xytext=(6, -12), fontsize=9)
    ax.set_title("3. Накопленное LTV на подписчика", fontsize=13, loc="left")
    ax.set_xlabel("неделя")
    ax.set_ylabel("$ на подписчика")
    ax.set_xlim(0, H_EXT)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)

    fig.suptitle(
        f"Веб v3: хвост прогноза. Красная линия — ступень {k_max}, где кончаются свои данные.\n"
        f"Всё правее — форма, заимствованная у iOS и сшитая по уровню. Нед 26/52/104 лежат там.",
        fontsize=12, y=0.995,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PNG, dpi=150)
    plt.close(fig)

    print(f"wrote {OUT_PNG}")
    print(f"k_max_web={k_max}")
    print("survival:", {k: round(float(curve.get(k)), 4) for k in (8, 12, 26, 52, 104)})
    # ltv starts at the anchor week, so horizons before it have no entry.
    print("LTV:     ", {k: round(float(ltv[k]), 2) for k in (12, 26, 52, 104) if k in ltv.index})
    print(f"anchor: week {anchor} at ${anchor_obs:.2f} (observed)")
    print("fact LTV:", {w: round(v, 2) for w, v in zip(fact_w, fact_v)})


if __name__ == "__main__":
    main()
