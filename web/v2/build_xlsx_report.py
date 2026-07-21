"""
Boss-facing Excel workbook for the ltv_v2 (appsflyer-source) LTV tables --
color-coded by confidence zone (fact / model / low_n / ups_projected).

Isolated: reads ONLY the already-built table_A/table_B CSVs under
reports/web_v2/. Does not touch ltv/, the golden pipeline, or its
reconcile.py gate.

Run: .venv/Scripts/python.exe web/v2/build_xlsx_report.py
"""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# Resolve all project-relative paths from the repository root, regardless of
# the working directory configured in PyCharm or the shell.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "web" / "golden"))
os.chdir(ROOT)

from ltv.config import HORIZONS_REPORT

OUT_DIR = "reports/web_v2"
TABLE_A_PATH = f"{OUT_DIR}/table_A_cohort_utm_appsflyer.csv"
TABLE_B_PATH = f"{OUT_DIR}/table_B_cohort_funnel_appsflyer.csv"
XLSX_PATH = f"{OUT_DIR}/LTV_v2_tables.xlsx"
HORIZONS = HORIZONS_REPORT

FONT_NAME = "Arial"

# ---------------------------------------------------------------- color palette
FACT_FILL = PatternFill("solid", fgColor="C6EFCE")       # green  -- real observed data
MODEL_FILL = PatternFill("solid", fgColor="FFEB9C")      # yellow -- projected, base cadence (reliable shape)
MODEL_UPS_FILL = PatternFill("solid", fgColor="F8CBAD")  # orange -- projected AND ups-specific (low confidence)
CURRENT_FILL = PatternFill("solid", fgColor="DCE6F1")    # blue   -- real revenue as of cohort's age today
NEUTRAL_FILL = PatternFill("solid", fgColor="F2F2F2")    # gray   -- metadata (counts)
MIXED_FILL = PatternFill("solid", fgColor="FFF2CC")      # pale yellow -- summary cell blending fact+model
MIXED_UPS_FILL = PatternFill("solid", fgColor="FBE5D6")  # pale orange -- summary ups cell blending fact+model

HEADER_FILL = PatternFill("solid", fgColor="305496")
HEADER_UPS_FILL = PatternFill("solid", fgColor="C55A11")
HEADER_CURRENT_FILL = PatternFill("solid", fgColor="1F4E78")

HEADER_FONT = Font(name=FONT_NAME, size=10, bold=True, color="FFFFFF")
BODY_FONT = Font(name=FONT_NAME, size=10)
LOWN_FONT = Font(name=FONT_NAME, size=9, italic=True, color="808080")
TITLE_FONT = Font(name=FONT_NAME, size=14, bold=True)
SECTION_FONT = Font(name=FONT_NAME, size=11, bold=True)
SMALL_FONT = Font(name=FONT_NAME, size=9, italic=True, color="595959")

THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP_CENTER = Alignment(wrap_text=True, vertical="center", horizontal="center")
WRAP_LEFT = Alignment(wrap_text=True, vertical="top", horizontal="left")

MONEY_FMT = '$#,##0.00'
PCT_FMT = '0.0%'


def build_column_meta(group_col):
    """Explicit (colname, kind, horizon) list in the EXACT order build_tables.py
    writes columns -- driving both header labels and cell fills by construction,
    not by pattern-matching column-name strings."""
    meta = [
        ("cohort_date", "label", None), (group_col, "label", None),
        ("n_attributed", "label", None), ("n_payers", "label", None),
        ("avg_age_weeks", "label", None), ("low_n", "label", None),
        ("ltv_per_attributed_current", "current", None),
        ("ltv_per_attributed_current_ups", "current", None),
        ("ltv_per_payer_current", "current", None),
        ("ltv_per_payer_current_ups", "current", None),
    ]
    for N in HORIZONS:
        meta += [
            (f"ltv_per_attributed_{N}", "base", N),
            (f"ltv_per_attributed_{N}_ups_factonly", "ups_factonly", N),
            (f"ltv_per_attributed_{N}_ups_projected", "ups_projected", N),
            (f"ltv_per_payer_{N}", "base", N),
            (f"ltv_per_payer_{N}_ups_factonly", "ups_factonly", N),
            (f"ltv_per_payer_{N}_ups_projected", "ups_projected", N),
            (f"ltv_{N}_source", "source_text", N),
            (f"ltv_{N}_n_mature", "neutral", N),
            (f"ltv_{N}_ups_projection_quality", "quality_text", N),
        ]
    return meta


def header_label(colname, kind):
    if kind == "ups_projected":
        return f"⚠ {colname}"
    if kind == "current":
        return f"{colname} (age@today)"
    return colname


def fill_for(kind, rowd, N):
    if kind == "label":
        return None
    if kind == "current":
        return CURRENT_FILL
    if kind == "neutral":
        return NEUTRAL_FILL
    if kind in ("base", "ups_factonly", "source_text"):
        return FACT_FILL if rowd[f"ltv_{N}_source"] == "fact" else MODEL_FILL
    if kind in ("ups_projected", "quality_text"):
        return FACT_FILL if rowd[f"ltv_{N}_ups_projection_quality"] == "fact" else MODEL_UPS_FILL
    raise ValueError(kind)


def write_detail_sheet(wb, sheet_name, df, group_col):
    ws = wb.create_sheet(sheet_name)
    meta = build_column_meta(group_col)
    assert [m[0] for m in meta] == list(df.columns), "column order mismatch -- meta must mirror the CSV exactly"

    for j, (colname, kind, N) in enumerate(meta, start=1):
        cell = ws.cell(row=1, column=j, value=header_label(colname, kind))
        cell.font = HEADER_FONT
        cell.alignment = WRAP_CENTER
        cell.border = BORDER
        cell.fill = {"ups_projected": HEADER_UPS_FILL, "current": HEADER_CURRENT_FILL}.get(kind, HEADER_FILL)

    money_kinds = {"current", "base", "ups_factonly", "ups_projected"}
    records = df.to_dict("records")
    for i, rowd in enumerate(records, start=2):
        is_low_n = bool(rowd.get("low_n"))
        font = LOWN_FONT if is_low_n else BODY_FONT
        for j, (colname, kind, N) in enumerate(meta, start=1):
            val = rowd[colname]
            if isinstance(val, float) and np.isnan(val):
                val = None
            cell = ws.cell(row=i, column=j, value=val)
            cell.font = font
            cell.border = BORDER
            if kind in money_kinds:
                cell.number_format = MONEY_FMT
            fill = fill_for(kind, rowd, N)
            if fill is not None:
                cell.fill = fill

    for j, (colname, kind, N) in enumerate(meta, start=1):
        letter = get_column_letter(j)
        if colname == "cohort_date":
            ws.column_dimensions[letter].width = 12
        elif colname == group_col:
            ws.column_dimensions[letter].width = 24
        elif kind in ("source_text", "quality_text"):
            ws.column_dimensions[letter].width = 13
        elif colname == "low_n":
            ws.column_dimensions[letter].width = 8
        else:
            ws.column_dimensions[letter].width = 12

    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(meta))}{len(df) + 1}"
    ws.sheet_view.showGridLines = False
    return {colname: j for j, (colname, kind, N) in enumerate(meta, start=1)}


def build_summary_sheet(wb, sheet_name_b, df_b, col_map_b):
    ws = wb.create_sheet("Summary")
    funnels = sorted(df_b["first_funnel"].unique().tolist())
    first_row, last_row = 2, len(df_b) + 1

    def rng(colname):
        letter = get_column_letter(col_map_b[colname])
        return f"'{sheet_name_b}'!${letter}${first_row}:${letter}${last_row}"

    funnel_rng = rng("first_funnel")
    n_attr_rng = rng("n_attributed")
    n_payers_rng = rng("n_payers")

    # ---- column layout: single-row cols 1-5, then paired (per attributed | per
    # payer) metric groups. col_idx -> (row1 group label, row2 sub-label or None, kind)
    SINGLE_COLS = [
        (1, "Funnel", None, "label"),
        (2, "n_attributed", None, "label"),
        (3, "n_payers", None, "label"),
        (4, "payer_rate", None, "pct"),
        (5, "avg_age_wk", None, "age"),
    ]
    ATTR_LABEL = "per attributed (для /CAC)"
    PAYER_LABEL = "per payer (юнит-экономика)"
    PAIR_GROUPS = [
        ("current_base", "ltv_per_attributed_current", "ltv_per_payer_current", "current", None),
        ("current_total (age@today, real)", "ltv_per_attributed_current_ups", "ltv_per_payer_current_ups", "current", None),
    ]
    for N in (12, 26, 52):
        PAIR_GROUPS.append((f"ltv_{N}_base", f"ltv_per_attributed_{N}", f"ltv_per_payer_{N}", "base", N))
        PAIR_GROUPS.append((f"⚠ ltv_{N}_ups_projected", f"ltv_per_attributed_{N}_ups_projected",
                             f"ltv_per_payer_{N}_ups_projected", "ups", N))
        PAIR_GROUPS.append((f"pct_mature@{N}", None, None, "pctmat", N))

    col = 6
    col_plan = []  # (col_idx, group_label, sub_label, kind, attr_colname, payer_colname, N)
    for group in PAIR_GROUPS:
        label, attr_col, payer_col, kind, N = group
        if kind == "pctmat":
            col_plan.append((col, label, None, kind, None, None, N))
            col += 1
        else:
            col_plan.append((col, label, ATTR_LABEL, kind, attr_col, None, N))
            col_plan.append((col + 1, label, PAYER_LABEL, kind, None, payer_col, N))
            col += 2
    total_cols = col - 1

    # ---- header row 1 (group labels, merged over the pair) + row 2 (attr/payer sub-labels)
    for c, label, _sub, _kind in SINGLE_COLS:
        cell = ws.cell(row=1, column=c, value=label)
        ws.merge_cells(start_row=1, end_row=2, start_column=c, end_column=c)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = WRAP_CENTER
        cell.border = BORDER
        ws.cell(row=2, column=c).border = BORDER

    seen_cols = set()
    for c, label, sub_label, kind, attr_col, payer_col, N in col_plan:
        if kind == "pctmat":
            cell = ws.cell(row=1, column=c, value=label)
            ws.merge_cells(start_row=1, end_row=2, start_column=c, end_column=c)
            cell.font, cell.fill = HEADER_FONT, HEADER_FILL
            cell.alignment, cell.border = WRAP_CENTER, BORDER
            ws.cell(row=2, column=c).border = BORDER
            continue
        group_start = c if sub_label == ATTR_LABEL else c - 1
        if group_start not in seen_cols:
            seen_cols.add(group_start)
            top = ws.cell(row=1, column=group_start, value=label)
            ws.merge_cells(start_row=1, end_row=1, start_column=group_start, end_column=group_start + 1)
            top.font = HEADER_FONT
            top.fill = HEADER_UPS_FILL if kind == "ups" else (HEADER_CURRENT_FILL if kind == "current" else HEADER_FILL)
            top.alignment, top.border = WRAP_CENTER, BORDER
            ws.cell(row=1, column=group_start + 1).border = BORDER
        sub = ws.cell(row=2, column=c, value=sub_label)
        sub.font = Font(name=FONT_NAME, size=9, italic=True, color="FFFFFF")
        sub.fill = HEADER_UPS_FILL if kind == "ups" else (HEADER_CURRENT_FILL if kind == "current" else HEADER_FILL)
        sub.alignment, sub.border = WRAP_CENTER, BORDER

    # Python-side pct_mature (population-weighted), computed straight from the
    # CSV -- used ONLY to pick a static fill color; the cell VALUE itself is a
    # live formula (see wavg_attr()/wavg_payer() below), per the xlsx skill's
    # "formulas, never hardcoded results" rule.
    grp = df_b.groupby("first_funnel").agg(
        n_attributed=("n_attributed", "sum"),
        **{f"n_mature_{N}": (f"ltv_{N}_n_mature", "sum") for N in (12, 26, 52)},
    )
    totals = {"n_attributed": df_b["n_attributed"].sum(),
              **{f"n_mature_{N}": df_b[f"ltv_{N}_n_mature"].sum() for N in (12, 26, 52)}}

    def pct_mature_bucket(funnel, N):
        if funnel == "TOTAL (all funnels)":
            attr, mat = totals["n_attributed"], totals[f"n_mature_{N}"]
        else:
            attr, mat = grp.loc[funnel, "n_attributed"], grp.loc[funnel, f"n_mature_{N}"]
        return mat / attr if attr else 0.0

    rows = funnels + ["TOTAL (all funnels)"]
    for i, funnel in enumerate(rows, start=3):
        is_total = funnel == "TOTAL (all funnels)"
        crit = None if is_total else f'"{funnel}"'
        name_cell = ws.cell(row=i, column=1, value=funnel)
        name_cell.font = Font(name=FONT_NAME, size=10, bold=is_total)
        if is_total:
            name_cell.fill = NEUTRAL_FILL

        def sumif(colname):
            r = rng(colname)
            return f"=SUM({r})" if crit is None else f"=SUMIF({funnel_rng},{crit},{r})"

        def wavg_attr(colname):
            r = rng(colname)
            if crit is None:
                num, den = f"SUMPRODUCT({n_attr_rng},{r})", f"SUM({n_attr_rng})"
            else:
                num = f"SUMPRODUCT(({funnel_rng}={crit})*{n_attr_rng},{r})"
                den = f"SUMIF({funnel_rng},{crit},{n_attr_rng})"
            return f"=IFERROR({num}/{den},0)"

        def wavg_payer(colname):
            # revenue/n_payers DIRECTLY off the By Funnel per-payer column, weighted
            # by n_payers -- NOT derived from the per-attributed cell / payer_rate.
            r = rng(colname)
            if crit is None:
                num, den = f"SUMPRODUCT({n_payers_rng},{r})", f"SUM({n_payers_rng})"
            else:
                num = f"SUMPRODUCT(({funnel_rng}={crit})*{n_payers_rng},{r})"
                den = f"SUMIF({funnel_rng},{crit},{n_payers_rng})"
            return f"=IFERROR({num}/{den},0)"

        def pct_mature_formula(N):
            r = rng(f"ltv_{N}_n_mature")
            if crit is None:
                return f"=IFERROR(SUM({r})/SUM({n_attr_rng}),0)"
            return f"=IFERROR(SUMIF({funnel_rng},{crit},{r})/SUMIF({funnel_rng},{crit},{n_attr_rng}),0)"

        vals = {
            2: (sumif("n_attributed"), None, None),
            3: (sumif("n_payers"), None, None),
            4: (f"=IFERROR(C{i}/B{i},0)", PCT_FMT, None),
            5: (wavg_attr("avg_age_weeks"), "0.0", None),
        }
        pct_cache = {N: pct_mature_bucket(funnel, N) for N in (12, 26, 52)}
        for c, label, sub_label, kind, attr_col, payer_col, N in col_plan:
            if kind == "pctmat":
                vals[c] = (pct_mature_formula(N), PCT_FMT, None)
            elif kind == "current":
                formula = wavg_attr(attr_col) if attr_col else wavg_payer(payer_col)
                vals[c] = (formula, MONEY_FMT, CURRENT_FILL)
            else:  # base / ups
                pct = pct_cache[N]
                if kind == "base":
                    fill = FACT_FILL if pct >= 0.99 else (MODEL_FILL if pct <= 0.01 else MIXED_FILL)
                else:
                    fill = FACT_FILL if pct >= 0.99 else (MODEL_UPS_FILL if pct <= 0.01 else MIXED_UPS_FILL)
                formula = wavg_attr(attr_col) if attr_col else wavg_payer(payer_col)
                vals[c] = (formula, MONEY_FMT, fill)

        for j in range(2, total_cols + 1):
            formula, fmt, fill = vals.get(j, (None, None, None))
            cell = ws.cell(row=i, column=j, value=formula)
            cell.font = Font(name=FONT_NAME, size=10, bold=is_total)
            cell.border = BORDER
            if fmt:
                cell.number_format = fmt
            if fill:
                cell.fill = fill
            elif is_total:
                cell.fill = NEUTRAL_FILL

    ws.column_dimensions["A"].width = 30
    for col_idx in range(2, total_cols + 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = 13
    ws.freeze_panes = "B3"
    ws.sheet_view.showGridLines = False
    return ws


def build_readme_sheet(wb, stats):
    ws = wb.create_sheet("Read me")
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 4
    ws.column_dimensions["B"].width = 34
    ws.column_dimensions["C"].width = 78

    r = 1

    def title(text, size=14):
        nonlocal r
        ws.merge_cells(f"A{r}:C{r}")
        c = ws.cell(row=r, column=1, value=text)
        c.font = Font(name=FONT_NAME, size=size, bold=True)
        r += 2

    def section(text):
        nonlocal r
        ws.merge_cells(f"A{r}:C{r}")
        c = ws.cell(row=r, column=1, value=text)
        c.font = SECTION_FONT
        r += 1

    def para(text):
        nonlocal r
        ws.merge_cells(f"A{r}:C{r}")
        c = ws.cell(row=r, column=1, value=text)
        c.font = BODY_FONT
        c.alignment = WRAP_LEFT
        ws.row_dimensions[r].height = 15 * max(1, (len(text) // 95 + 1))
        r += 1

    def swatch_row(fill, label, desc):
        nonlocal r
        sw = ws.cell(row=r, column=1)
        sw.fill = fill
        sw.border = BORDER
        lab = ws.cell(row=r, column=2, value=label)
        lab.font = Font(name=FONT_NAME, size=10, bold=True)
        lab.alignment = Alignment(vertical="center", wrap_text=True)
        d = ws.cell(row=r, column=3, value=desc)
        d.font = BODY_FONT
        d.alignment = WRAP_LEFT
        ws.row_dimensions[r].height = max(28, 15 * (len(desc) // 60 + 1))
        r += 1

    title("LTV v2 (appsflyer source) — reading guide")
    para("Source: appsflyer BigQuery (web_conversions). Isolated v2 pipeline (ltv_v2/, web_appsflyer_v2/) "
         "-- does not read from or write into the golden Stripe/Solidgate pipeline. Population: 5,406 people "
         "(appsflyer-native attribution, first_date >= 2026-04-13). Net revenue includes a temporary refund "
         "haircut (REFUND_HAIRCUT=0.00913) since appsflyer carries no refund events yet -- see ltv_v2/config.py.")
    r += 1

    section("Sheets in this workbook")
    para("By UTM -- cohort_date x utm_source (Table A). By Funnel -- cohort_date x first_funnel (Table B). "
         "Summary -- one row per funnel (all cohorts pooled), for a boss-level read without per-cohort micro-cells.")
    r += 1

    section("The three LTV levels, at every horizon (4/12/26/52/104 weeks)")
    para("ltv_per_*_N (no suffix) -- BASE ONLY, the $9.99/week subscription_started stream. Weekly-ratio "
         "projection is the correct cadence for a weekly charge -- reliable.")
    para("ltv_per_*_N_ups_factonly -- base + upsell, upsell counted ONLY from real observed data. Beyond the "
         "cohort's own age, upsell is frozen at today's value (assumed flat) -- a conservative floor, never overstates.")
    para("⚠ ltv_per_*_N_ups_projected -- base + upsell, upsell PROJECTED on a monthly cadence (the $11.99/month "
         "upsale_converted stream fires ~every 4 weeks, not every week -- using the weekly ratio here was the old "
         "\"+21%\" bug, now fixed). Still: only ~3 months of upsell history exist. Low confidence -- do not use "
         "for decisions without checking low_n and ltv_{N}_ups_projection_quality first.")
    r += 1

    section("Color key")
    swatch_row(FACT_FILL, "Green -- fact", "Real observed data (ltv_{N}_source = 'fact'). The cohort has already "
               "reached this horizon; no model involved.")
    swatch_row(MODEL_FILL, "Yellow -- model (base)", "Projected with the weekly retention shape. Correct cadence "
               "for the base $9.99/week stream -- the least risky kind of projection in this workbook.")
    swatch_row(MODEL_UPS_FILL, "Orange -- model (ups_projected)", "Projected upsell specifically, monthly-cadence "
               "ratio, ~3 months of upsell history behind it. The most uncertain number on any sheet.")
    swatch_row(CURRENT_FILL, "Blue -- current", "Real revenue realized as of the COHORT'S OWN CURRENT AGE today, "
               "not week 0. An older cohort's 'current' can legitimately exceed its own ltv_4 -- that is not a bug.")
    swatch_row(NEUTRAL_FILL, "Gray fill -- metadata", "Counts (n_mature) with no reliability meaning of their own.")
    lown_row = r
    ws.cell(row=lown_row, column=1).fill = PatternFill("solid", fgColor="D9D9D9")
    ws.cell(row=lown_row, column=1).border = BORDER
    lab = ws.cell(row=lown_row, column=2, value="Gray italic text -- low_n row")
    lab.font = LOWN_FONT
    lown_desc = ("n_payers < 40 for that cell. The fill colors above still show "
                 "fact vs. model, but the font is muted on purpose -- treat the whole row as noise, not a number "
                 "to plan around. A single low_n cell can show a large _ups_projected value purely from the "
                 "monthly multiplier hitting one or two people; always check n_payers before trusting it.")
    d = ws.cell(row=lown_row, column=3, value=lown_desc)
    d.font = BODY_FONT
    d.alignment = WRAP_LEFT
    ws.row_dimensions[lown_row].height = max(28, 15 * (len(lown_desc) // 60 + 1))
    r += 1
    r += 1

    section("Caveats (read before using this for decisions)")
    for line in stats["caveats"]:
        para(f"• {line}")
    r += 1

    section("Reliability snapshot (computed from the shipped CSVs, not hardcoded)")
    for line in stats["snapshot"]:
        para(f"• {line}")

    return ws


def main():
    df_a = pd.read_csv(TABLE_A_PATH)
    df_b = pd.read_csv(TABLE_B_PATH)
    print(f"loaded Table A: {len(df_a)} rows, {len(df_a.columns)} cols")
    print(f"loaded Table B: {len(df_b)} rows, {len(df_b.columns)} cols")

    caveats = [
        "ups_projected has ~3 months of upsell history behind it (upsale_converted started appearing recently) "
        "-- treat as directional, not precise.",
        "At horizons 26/52/104 weeks: 0% of cells in EITHER table have real (fact) data yet -- every cohort is "
        "too young (max age ~13 weeks). All 26/52/104 columns are 100% model/projected in both By UTM and By Funnel.",
        f"low_n (n_payers<40) covers {df_a['low_n'].mean():.1%} of By UTM cells and {df_b['low_n'].mean():.1%} of "
        "By Funnel cells -- most individual cells are small-sample. Use the Summary sheet for anything boss-facing.",
        "Refund haircut (0.00913) is a temporary stopgap dated 2026-07-12, derived from golden's refund rate on "
        "the symmetric window -- replace once appsflyer carries a real refund event.",
        "This workbook does not touch the golden (Stripe/Solidgate) pipeline or its reconcile.py gate.",
    ]
    snapshot = []
    for N in (4, 12, 26, 52, 104):
        fa = (df_a[f"ltv_{N}_source"] == "fact").mean()
        fb = (df_b[f"ltv_{N}_source"] == "fact").mean()
        snapshot.append(f"wk{N}: fact cells = {fa:.0%} (By UTM), {fb:.0%} (By Funnel)")

    wb = Workbook()
    wb.remove(wb.active)

    build_readme_sheet(wb, {"caveats": caveats, "snapshot": snapshot})
    col_map_a = write_detail_sheet(wb, "By UTM", df_a, "utm_source")
    col_map_b = write_detail_sheet(wb, "By Funnel", df_b, "first_funnel")
    build_summary_sheet(wb, "By Funnel", df_b, col_map_b)

    wb["Read me"].sheet_properties.tabColor = "808080"
    wb["By UTM"].sheet_properties.tabColor = "305496"
    wb["By Funnel"].sheet_properties.tabColor = "305496"
    wb["Summary"].sheet_properties.tabColor = "C55A11"

    wb.save(XLSX_PATH)
    print(f"wrote {XLSX_PATH}")
    print(f"sheets: {wb.sheetnames}")


if __name__ == "__main__":
    main()
