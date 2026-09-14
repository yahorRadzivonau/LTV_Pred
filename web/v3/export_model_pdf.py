"""
Export the prediction model's source to a 4-page PDF, comments and docstrings
stripped, 11pt monospace.

Included files are the model itself and nothing else -- the data pull, table
builders and uploaders are deliberately left out.

Run: .venv/Scripts/python.exe web/v3/export_model_pdf.py
"""
import ast
import io
import os
import sys
import tokenize
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
os.chdir(ROOT)

OUT_PDF = ROOT / "reports" / "web_v3" / "ltv_model_code.pdf"

# The two model modules in full, plus the one core helper they call. 258 stripped
# lines against a 4-page budget of ~276 at 11pt, so nothing has to be
# cherry-picked out of them.
# Left out on purpose: se_training_web.py (builds the matrix -- data prep, not
# prediction), upsell.py (measures the attach rate that feeds share_ups), and
# every pull/build/upload script.
FILES = [
    ("core/common.py", ["empirical_hbase"]),
    ("web/v3/ltv_v3/map_web.py", None),
    ("web/v3/ltv_v3/money.py", None),
]

FONT_SIZE = 11
LINES_PER_PAGE = 69
PAGES = 4
WRAP_AT = 78          # chars that fit across A4 at 11pt monospace
LINESPACING = 1.08


def strip_comments(src: str) -> str:
    """Remove # comments via tokenize, then docstrings via the AST."""
    out, prev_end = [], (1, 0)
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        ttype, tstr, start, end, _ = tok
        if ttype == tokenize.COMMENT:
            prev_end = end
            continue
        # NEWLINE/NL carry their own "\n"; adding the line-gap padding on top of
        # them double-spaces the whole file, which silently doubled every line
        # count this script reported.
        if ttype in (tokenize.NEWLINE, tokenize.NL):
            out.append("\n")
            prev_end = (start[0] + 1, 0)
            continue
        if start[0] > prev_end[0]:
            out.append("\n" * (start[0] - prev_end[0]))
            out.append(" " * start[1])
        elif start[1] > prev_end[1]:
            out.append(" " * (start[1] - prev_end[1]))
        out.append(tstr)
        prev_end = end
    text = "".join(out)

    tree = ast.parse(text)
    drop = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                drop.update(range(body[0].lineno, body[0].end_lineno + 1))
    lines = [l for i, l in enumerate(text.splitlines(), 1) if i not in drop]

    cleaned, blank = [], 0
    for l in lines:
        if not l.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        cleaned.append(l.rstrip())
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    return "\n".join(cleaned)


def extract(path: str, names):
    src = Path(path).read_text(encoding="utf-8")
    if names:
        tree = ast.parse(src)
        keep = [n for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.name in names]
        src = "\n\n".join(ast.get_source_segment(src, n) for n in keep)
    return strip_comments(src)


def wrap(line: str, width: int = WRAP_AT):
    """Fold an over-wide line, continuing at the original indent + 4."""
    if len(line) <= width:
        return [line]
    indent = " " * (len(line) - len(line.lstrip()) + 4)
    out, rest = [], line
    while len(rest) > width:
        cut = rest.rfind(" ", 0, width)
        if cut <= len(indent):
            cut = width
        out.append(rest[:cut])
        rest = indent + rest[cut:].lstrip()
    out.append(rest)
    return out


def main():
    blocks = []
    for path, names in FILES:
        code = extract(path, names)
        blocks.append(f"# ==== {path} ====")
        for l in code.splitlines():
            blocks.extend(wrap(l))
        blocks.append("")
    lines = blocks

    total = LINES_PER_PAGE * PAGES
    if len(lines) > total:
        print(f"WARNING: {len(lines)} lines do not fit in {PAGES} pages "
              f"({total} slots) -- trailing lines dropped")
        print("         drop a file from FILES or raise LINES_PER_PAGE")
        lines = lines[:total]
    else:
        print(f"{len(lines)} lines over {PAGES} pages ({total} slots), "
              f"{total - len(lines)} spare")

    longest = max((len(l) for l in lines), default=0)
    print(f"longest line: {longest} chars")

    OUT_PDF.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(OUT_PDF) as pdf:
        for p in range(PAGES):
            chunk = lines[p * LINES_PER_PAGE:(p + 1) * LINES_PER_PAGE]
            fig = plt.figure(figsize=(8.27, 11.69))  # A4
            fig.text(0.05, 0.982, "\n".join(chunk), family="DejaVu Sans Mono",
                     fontsize=FONT_SIZE, va="top", ha="left", linespacing=LINESPACING)
            pdf.savefig(fig)
            plt.close(fig)
    print(f"wrote {OUT_PDF}")


if __name__ == "__main__":
    main()
