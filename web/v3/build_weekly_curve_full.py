"""
Weekly curve broken down by EVERYTHING: one row per (cohort_date x funnel x
utm_source x country x campaign_name x ad_name x week 0..104).

Feeds ad_hock_tables.ml_web_weekly_curve_full (loaded separately by
web/v3/upload_weekly_curve_full.py -- nothing is written to BigQuery here).

THIS TABLE IS A DEMONSTRATION, deliberately (docs/BQ_TABLES_WEEKLY_CURVE_V2.md):
slicing ~8k base payers by six dimensions at once yields thousands of cells at
~2 people each, so nearly every row lands sample_grade = insufficient. It
exists to make "just break it down by everything at once" visibly not work,
not to be read cell by cell. The K_SHRINK shrinkage keeps the tiny cells riding the
portfolio curve instead of amplifying their own noise, so the numbers are sane
-- just not individually meaningful. All shared logic lives in
ltv_v3/weekly_curve.py.

Writes: reports/web_v3/weekly_curve_full_v3_<today>.csv
Run: .venv/Scripts/python.exe web/v3/build_weekly_curve_full.py
"""
import os
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
os.chdir(ROOT)

from ltv_v3.config import OUT_DIR  # noqa: E402
from ltv_v3 import weekly_curve as W  # noqa: E402

OUT_CSV = ROOT / OUT_DIR / f"weekly_curve_full_v3_{date.today().isoformat()}.csv"

GROUP = ["cohort_date", "first_funnel", "utm_source", "geo", "campaign_name", "ad_name"]
RENAME = {"first_funnel": "funnel", "geo": "country"}
DIM_COLS = ["cohort_date", "funnel", "utm_source", "country", "campaign_name", "ad_name"]

ps = W.load_pipeline_state()
print(f"cells source: {len(ps['pop'])} people | dims {DIM_COLS} | k_max_web {ps['k_max_web']}")

out = W.build_cells(ps, GROUP, RENAME)

OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
out.to_csv(OUT_CSV, index=False)
W.report(out, DIM_COLS, OUT_CSV)
