"""
Weekly curve broken down by CAMPAIGN: one row per (cohort_date x campaign_name
x week 0..104).

Feeds ad_hock_tables.ml_web_weekly_curve_campaign (loaded separately by
web/v4/upload_weekly_curve_campaign.py -- nothing is written to BigQuery here).
campaign_name is normalised at population-build time (ltv_v4/dims.py):
URL-decoded (which merges real duplicates like `...WP%3Aredirect...` vs
`...WP:redirect...`), macro leftovers and 'unknown' collapsed into
'(missing)'. All shared logic lives in ltv_v4/weekly_curve.py.

Writes: reports/web_v4/weekly_curve_campaign_v3_<today>.csv
Run: .venv/Scripts/python.exe web/v4/build_weekly_curve_campaign.py
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

OUT_CSV = ROOT / OUT_DIR / f"weekly_curve_campaign_v3_{date.today().isoformat()}.csv"

GROUP = ["cohort_date", "campaign_name"]
DIM_COLS = ["cohort_date", "campaign_name"]

ps = W.load_pipeline_state()
print(f"cells source: {len(ps['pop'])} people | dims {DIM_COLS} | k_max_web {ps['k_max_web']}")

out = W.build_cells(ps, GROUP)

OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
out.to_csv(OUT_CSV, index=False)
W.report(out, DIM_COLS, OUT_CSV)
