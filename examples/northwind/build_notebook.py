"""Build (and execute) the Northwind worked example notebook.

    python examples/northwind/build_notebook.py          # build + run, saves outputs
    python examples/northwind/build_notebook.py --no-run # build only

The notebook has two parts:

1. "What a naive analysis gets wrong": four classic SQL mistakes, each run next to the correct query on the
   same data, so that the size of every error is printed rather than claimed.
2. The full AutoSQL Explorer analysis of the 8 Northwind tables (the same cells the app generates), with every
   step explained and the MySQL / PostgreSQL versions of each query.
3. The business reading: the headline, KPI tiles, recommendations and data-trust score computed from the metrics
   above, and `business_brief.html` written next to the notebook.
"""
from __future__ import annotations

import sys
from pathlib import Path

import nbformat
from nbformat.v4 import new_code_cell, new_markdown_cell

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from autosql import build_cells, decide, infer_schema, profile_catalog, read_folder  # noqa: E402
from autosql.codegen import CHART_SETUP  # noqa: E402
from autosql.exports import to_notebook  # noqa: E402

OUT = HERE / "Northwind_sales_analysis.ipynb"

SETUP = '''
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path.cwd().resolve().parents[1]))  # the repository root, so that `autosql` can be imported
from autosql import build_database, read_folder

metrics = {}  # every cell stores its key numbers here


def note(text):
    """Write a plain-English finding under the cell."""
    try:
        from IPython.display import Markdown, display
        display(Markdown("📌 " + text))
    except ImportError:
        print("📌 " + text)


catalog = read_folder(".", "*.csv")        # the 8 original CSVs; names become snake_case (OrderID -> order_id)
con = build_database(catalog)              # in-memory DuckDB, one table per CSV
source = {name: t.df for name, t in catalog.tables.items()}
scratch = duckdb.connect()                 # an empty database, for the CREATE TABLE check


def sql(query, db=None):
    cursor = (db or con).execute(query)
    return cursor.df() if cursor.description else pd.DataFrame()


print({name: len(df) for name, df in source.items()})
'''

INTRO = """
# Northwind: sales analysis in SQL

**The data.** Northwind is Microsoft's sample database of a small food importer: 8 tables, 91 customers, 830 orders and
2,155 order lines from July 1996 to May 1998. The CSVs in this folder are built from Microsoft's original install script
by `build_data.py` (MIT licence, see `NOTICE.md`), with the original table and column names.

**The questions.** How big is the business, how is it trending, what drove the last change, and can the numbers be trusted?

**How to read this notebook.** Part 1 runs four classic SQL mistakes next to the correct query, so that each error is
measured on this data. Part 2 is the full analysis AutoSQL Explorer generates, in its seven steps:
schema & grain → validate → build with CTEs → KPIs → window functions → root cause → fast & correct.
Every step explains 🔍 what we found before, 💡 why it matters, 🎯 what we're going to do and 🛠 what the SQL does;
after the code runs, 📌 says what we found, computed from the result so that the text can't contradict the output.
The SQL in the code cells is the DuckDB version that runs here; the MySQL 8 and PostgreSQL versions are in the
collapsed blocks under each step.
"""

TRAPS = [
    ("1. Summing an order-level number after joining order lines (fan-out)",
     "`orders.freight` is stored once per order, but `order_details` has 1 to 25 lines per order.",
     "Joining first repeats the freight on every line, so its total is multiplied by the number of lines.",
     '''
naive = sql("""
SELECT SUM(o.freight) AS freight
FROM   orders o
JOIN   order_details od ON od.order_id = o.order_id
""").iloc[0, 0]
correct = sql("SELECT SUM(freight) AS freight FROM orders").iloc[0, 0]
note(f"Naive: **{naive:,.2f}**. Correct: **{correct:,.2f}**. The join inflates freight **{naive / correct:.2f}×**.")
'''),
    ("2. Comparing a partial quarter with a full one",
     "The data ends on 6 May 1998, so 1998-Q2 has 36 of its 91 days.",
     "Any part-period looks like a collapse. Compare complete periods only, or the same period a year earlier.",
     '''
per_quarter = sql("""
SELECT EXTRACT(YEAR FROM o.order_date)::int AS order_year,
       EXTRACT(QUARTER FROM o.order_date)::int AS quarter_number,
       SUM(od.unit_price * od.quantity * (1 - od.discount)) AS revenue,
       MIN(o.order_date) AS first_day, MAX(o.order_date) AS last_day
FROM   order_details od
JOIN   orders o ON o.order_id = od.order_id
GROUP  BY 1, 2
ORDER  BY 1, 2
""")
display(per_quarter.tail(3))
naive = 100 * (per_quarter.revenue.iloc[-1] / per_quarter.revenue.iloc[-2] - 1)
correct = 100 * (per_quarter.revenue.iloc[-2] / per_quarter.revenue.iloc[-3] - 1)
note(f"Naive 'latest quarter': **{naive:+.1f}%**. Last two *complete* quarters (1997-Q4 → 1998-Q1): **{correct:+.1f}%**. "
     f"The data stops on {per_quarter.last_day.iloc[-1]:%d %B %Y}.")
'''),
    ("3. Averaging order lines instead of orders",
     "Revenue is stored per line; an order has 2.6 lines on average.",
     "Average order value is revenue ÷ the number of distinct orders. The average of lines counts each line as if it were an order.",
     '''
row = sql("""
SELECT AVG(unit_price * quantity * (1 - discount)) AS avg_of_lines,
       SUM(unit_price * quantity * (1 - discount)) / COUNT(DISTINCT order_id) AS avg_order_value
FROM   order_details
""").iloc[0]
note(f"Average of lines: **{row.avg_of_lines:,.2f}**. Average order value: **{row.avg_order_value:,.2f}** "
     f"({row.avg_order_value / row.avg_of_lines:.1f}× larger).")
'''),
    ("4. NOT IN when the sub-query contains a NULL",
     "`employees.reports_to` is NULL for the one employee who reports to nobody.",
     "`x NOT IN (…, NULL)` is never true, so NOT IN silently returns no rows. NOT EXISTS is safe.",
     '''
row = sql("""
SELECT (SELECT COUNT(*) FROM employees e
        WHERE  e.employee_id NOT IN (SELECT reports_to FROM employees)) AS not_in,
       (SELECT COUNT(*) FROM employees e
        WHERE  NOT EXISTS (SELECT 1 FROM employees m WHERE m.reports_to = e.employee_id)) AS not_exists
""").iloc[0]
note(f"Employees who manage nobody: NOT IN says **{row.not_in}**, NOT EXISTS says **{row.not_exists}**.")
'''),
]


PART3 = """
# Part 3: the business reading

Every step above stored its key numbers in `metrics`. The business layer turns them into what a manager reads: one headline,
KPI tiles for the last complete quarter, recommendations ordered by urgency (each with its evidence, a confidence level and
the SQL step it comes from) and a data-trust score. The same page is written to `business_brief.html` next to this notebook:
open it in any browser. How to read it, and every rule behind it: `docs/business_guide.md`.
"""

BUSINESS = '''
from autosql import decide, infer_schema, profile_catalog
from autosql.business import executive_summary
from autosql.report import to_business_brief

profile = profile_catalog(catalog)
decision = decide(catalog, profile, infer_schema(catalog, profile))
summary = executive_summary(decision, metrics, "Northwind Traders")
note(f"**{summary.headline}**")
tiles = pd.DataFrame([{"KPI": t.label, summary.period: round(t.value, 2),
                       f"vs {summary.compared_with}": (f"{t.delta_pct:+.1f} pts" if t.unit == "pct" else f"{t.delta_pct:+.1f}%")
                       if t.delta_pct is not None else "",
                       "for the business": {True: "better", False: "worse", None: ""}[t.good]} for t in summary.tiles])
display(tiles)
names = {1: "Act now", 2: "Plan", 3: "Monitor"}
display(pd.DataFrame([{"priority": names[i.priority], "recommendation": i.headline, "evidence": i.evidence, "next step": i.action,
                       "confidence": i.confidence, "SQL step": i.source} for i in summary.insights]))
note(f"Data trust: **{int(summary.trust.score)}/100, {summary.trust.grade}**. "
     + " ".join(f"{x.name} {int(x.score)}: {x.detail}" for x in summary.trust.dimensions))
Path("business_brief.html").write_text(to_business_brief(decision, metrics, title="Northwind Traders",
                                                         source="the Northwind sample (8 CSV files)"), encoding="utf-8")
note("Wrote `business_brief.html`: the one-page brief with the charts.")
'''


def build() -> nbformat.NotebookNode:
    catalog = read_folder(HERE, "*.csv")
    profile = profile_catalog(catalog)
    schema = infer_schema(catalog, profile)
    decision = decide(catalog, profile, schema)
    nb = nbformat.reads(to_notebook(build_cells(decision), decision, catalog, "the Northwind CSVs in this folder"), as_version=4)
    generated = nb.cells[3:]  # after the generated title, the "Setup" heading and its code cell
    cells = [new_markdown_cell(INTRO.strip()), new_markdown_cell("## Setup"), new_code_cell(SETUP.strip() + "\n\n" + CHART_SETUP),
             new_markdown_cell("# Part 1: what a naive analysis gets wrong")]
    for title, found, why, code in TRAPS:
        cells.append(new_markdown_cell(f"### {title}\n\n🔍 **What we found before:** {found}  \n💡 **Why it matters:** {why}"))
        cells.append(new_code_cell(code.strip()))
    cells.append(new_markdown_cell("# Part 2: the AutoSQL Explorer analysis\n\n" + nb.cells[0].source.split("\n\n", 1)[1]))
    nb.cells = cells + generated + [new_markdown_cell(PART3.strip()), new_code_cell(BUSINESS.strip())]
    return nb


def main() -> None:
    nb = build()
    if "--no-run" not in sys.argv:
        from nbclient import NotebookClient
        NotebookClient(nb, timeout=300, kernel_name="python3", resources={"metadata": {"path": str(HERE)}}).execute()
    nbformat.write(nb, OUT)
    print(f"wrote {OUT.relative_to(ROOT)} ({len(nb.cells)} cells)")


if __name__ == "__main__":
    main()
