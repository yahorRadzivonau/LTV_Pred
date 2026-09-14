"""
Weekly curve broken down by AD: one row per (cohort_date x ad_name x week 0..104).

Feeds ad_hock_tables.ml_web_weekly_curve_ad (loaded separately by
web/v4/upload_weekly_curve_ad.py -- nothing is written to BigQuery here).
ad_name is the dirtiest of the dimensions: ~30% of source rows carry no usable
value (empty / 'unknown' / unsubstituted macros like `{{ad.name}}` and
`%7Bad_name%7D`). Normalisation (ltv_v4/dims.py) URL-decodes the rest and
collapses all the garbage into ONE explicit '(missing)' bucket instead of six
flavours of it smeared across the table. All shared logic lives in
ltv_v4/weekly_curve.py.

Writes: reports/web_v4/weekly_curve_ad_v3_<today>.csv
Run: .venv/Scripts/python.exe web/v4/build_weekly_curve_ad.py
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

from ltv_v4.config import OUT_DIR  # noqa: E402
from ltv_v4 import weekly_curve as W  # noqa: E402

OUT_CSV = ROOT / OUT_DIR / f"weekly_curve_ad_v3_{date.today().isoformat()}.csv"

GROUP = ["cohort_date", "ad_name"]
DIM_COLS = ["cohort_date", "ad_name"]

ps = W.load_pipeline_state()
print(f"cells source: {len(ps['pop'])} people | dims {DIM_COLS} | k_max_web {ps['k_max_web']}")

out = W.build_cells(ps, GROUP)

OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
out.to_csv(OUT_CSV, index=False)
W.report(out, DIM_COLS, OUT_CSV)
