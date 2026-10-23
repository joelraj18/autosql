"""Take the analysis out of the app: one .sql file, a project ZIP that loads into a real database, or a notebook.

The project ZIP is meant to be rerun by someone else:

    data/<table>.csv      cleaned copies of the uploads (snake_case headers, ISO dates, empty field = NULL)
    01_schema.sql         CREATE TABLE with primary and foreign keys
    02_load.sql           LOAD DATA LOCAL INFILE (MySQL) or \\copy (PostgreSQL / psql)
    03_validate.sql       grain, cardinality, duplicates, NULLs, dates, orphans
    04_analysis.sql       views, reconciliation, KPIs, window functions, root cause
    05_performance.sql    EXPLAIN, sargable filters, indexes, NULL traps
    analysis.ipynb        the same steps in Python + DuckDB, with every explanation
    README.md             how to run it
"""
from __future__ import annotations

import io
import json
import textwrap
import zipfile

import pandas as pd

from .codegen import CHART_SETUP, Cell, _topological, assumptions
from .decision import Decision
from .dialect import LABELS, Dialect
from .loading import Catalog

FILES = {
    "01_schema.sql": ("schema",),
    "03_validate.sql": ("grain", "cardinality", "duplicates", "nulls", "date_range", "orphans"),
    "04_analysis.sql": ("base", "reconcile", "kpis", "kpi_by_period", "running_total", "top_n", "latest_record", "rfm",
                        "driver_tree", "drilldown", "cohort"),
    "05_performance.sql": ("explain", "null_traps"),
}


def _comment_block(lines: list[str]) -> str:
    out = []
    for line in lines:
        out += textwrap.wrap(line, width=110, initial_indent="-- ", subsequent_indent="--   ") or ["--"]
    return "\n".join(out)


def header(d: Decision, dialect: str, source: str) -> str:
    lines = [f"AutoSQL Explorer - {LABELS[dialect]}", f"Generated from: {source}", "",
             "Every step below explains what was found, why it matters and what the SQL does.", "", "Assumptions:"]
    lines += [f"- {a.replace('`', '')}" for a in assumptions(d)]
    return _comment_block(lines)


def cell_text(cell: Cell, dialect: str, number: int) -> str:
    story = cell.story
    clean = lambda t: t.replace("**", "").replace("`", "")  # noqa: E731
    head = _comment_block([f"{'=' * 100}", f"{number}. {cell.title}  [{cell.section}]", "",
                           f"What we found before: {clean(story.found)}", f"Why it matters: {clean(story.why)}",
                           f"What we do: {clean(story.action)}", f"What this SQL does: {clean(story.code)}", "=" * 100])
    return head + "\n" + cell.code(dialect)


def to_sql_script(cells: list[Cell], d: Decision, dialect: str, source: str = "uploaded files") -> str:
    """All cells as one runnable script for MySQL or PostgreSQL (load the data first: see the project ZIP)."""
    parts = [header(d, dialect, source)]
    parts += [cell_text(cell, dialect, i) for i, cell in enumerate(cells, 1)]
    return "\n\n".join(parts) + "\n"


# ── Data files ───────────────────────────────────────────────────────────
def clean_csv(catalog: Catalog, name: str) -> bytes:
    """The table as loaded: snake_case headers, ISO dates, 1/0 booleans, empty field = NULL."""
    t = catalog[name]
    out = pd.DataFrame(index=t.df.index)
    for col in t.df.columns:
        s = t.df[col]
        kind = t.types[col]
        if kind == "date":
            out[col] = s.dt.strftime("%Y-%m-%d")
        elif kind == "timestamp":
            out[col] = s.dt.strftime("%Y-%m-%d %H:%M:%S")
        elif kind == "boolean":
            out[col] = s.map(lambda v: None if pd.isna(v) else int(bool(v)))
        else:
            out[col] = s
    buf = io.StringIO()
    out.to_csv(buf, index=False, lineterminator="\n")
    return buf.getvalue().encode("utf-8")


def load_script(catalog: Catalog, dialect: str, order: list[str] | None = None) -> str:
    """Load statements, parents before children so that foreign keys are satisfied."""
    D = Dialect(dialect)
    order = [n for n in (order or []) if n in catalog.tables] + [n for n in catalog.names if n not in (order or [])]
    if dialect == "mysql":
        parts = [_comment_block([
            "Load the cleaned CSVs (run from the folder that contains data/).",
            "Needs local_infile enabled on the server (SET GLOBAL local_infile = 1) and the client (mysql --local-infile=1).",
            "NULLIF(@col, '') turns an empty field into NULL: without it MySQL loads '' into dates and numbers as 0 or an error.",
            "InnoDB checks foreign keys row by row, so a row pointing at a later row of the same table (an org chart) would fail: "
            "the checks are switched off while loading; the orphans step in 03_validate.sql checks the links afterwards.",
        ]), "SET FOREIGN_KEY_CHECKS = 0;"]
        for name in order:
            t = catalog[name]
            cols = list(t.df.columns)
            parts.append(textwrap.dedent(f"""\
                LOAD DATA LOCAL INFILE 'data/{name}.csv'
                INTO TABLE {D.ident(name)}
                CHARACTER SET utf8mb4
                FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' ESCAPED BY ''
                LINES TERMINATED BY '\\n'
                IGNORE 1 LINES
                """) + "(" + ", ".join(f"@{i}" for i in range(len(cols))) + ")\nSET " +
                ",\n    ".join(f"{D.ident(c)} = NULLIF(@{i}, '')" for i, c in enumerate(cols)) + ";")
        parts.append("SET FOREIGN_KEY_CHECKS = 1;")
        return "\n\n".join(parts) + "\n"
    parts = [_comment_block(["Load the cleaned CSVs with psql (run from the folder that contains data/): psql -f 02_load.sql",
                             "\\copy reads the file on your computer; an unquoted empty field becomes NULL."])]
    for name in order:
        cols = ", ".join(D.ident(c) for c in catalog[name].df.columns)
        parts.append(f"\\copy {D.ident(name)} ({cols}) FROM 'data/{name}.csv' WITH (FORMAT csv, HEADER true)")
    return "\n".join(parts) + "\n"


def readme(d: Decision, dialect: str, source: str) -> str:
    run = ("mysql --local-infile=1 --default-character-set=utf8mb4 -u USER -p DATABASE < 01_schema.sql\n"
           "mysql --local-infile=1 --default-character-set=utf8mb4 -u USER -p DATABASE < 02_load.sql\n"
           "mysql --default-character-set=utf8mb4 -u USER -p DATABASE < 03_validate.sql   # then 04_analysis.sql and 05_performance.sql"
           if dialect == "mysql" else
           "psql -d DATABASE -v ON_ERROR_STOP=1 -f 01_schema.sql\npsql -d DATABASE -v ON_ERROR_STOP=1 -f 02_load.sql\n"
           "psql -d DATABASE -v ON_ERROR_STOP=1 -f 03_validate.sql   # then 04_analysis.sql and 05_performance.sql")
    return "\n".join([
        f"# AutoSQL Explorer project ({LABELS[dialect]})", "", f"Generated from {source}.", "",
        "## Run it", "", "```bash", run, "```", "",
        "`analysis.ipynb` runs the same steps with Python and DuckDB (`pip install duckdb pandas matplotlib`), no database needed.", "",
        "## Assumptions", "", *[f"- {a}" for a in assumptions(d)], "",
        "## Files", "", "| File | What it does |", "|---|---|",
        "| `data/*.csv` | the uploaded tables, cleaned: snake_case headers, ISO dates, empty field = NULL |",
        "| `01_schema.sql` | CREATE TABLE with primary and foreign keys |",
        "| `02_load.sql` | loads `data/*.csv` |",
        "| `03_validate.sql` | grain, cardinality and fan-out, duplicates, NULLs, date ranges, orphans |",
        "| `04_analysis.sql` | the analysis views, reconciliation, KPIs, window functions, root cause |",
        "| `05_performance.sql` | EXPLAIN, sargable filters, indexes, NULL traps |", ""])


def to_project_zip(cells: list[Cell], d: Decision, catalog: Catalog, dialect: str, source: str = "uploaded files") -> bytes:
    by_id = {c.id: (i, c) for i, c in enumerate(cells, 1)}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", readme(d, dialect, source))
        for name in catalog.names:
            z.writestr(f"data/{name}.csv", clean_csv(catalog, name))
        z.writestr("02_load.sql", load_script(catalog, dialect, _topological(d)))
        for filename, ids in FILES.items():
            parts = [header(d, dialect, source)] + [cell_text(by_id[i][1], dialect, by_id[i][0]) for i in ids if i in by_id]
            z.writestr(filename, "\n\n".join(parts) + "\n")
        z.writestr("analysis.ipynb", to_notebook(cells, d, catalog, source))
    return buf.getvalue()


# ── Notebook ─────────────────────────────────────────────────────────────
def _py_string(text: str) -> str:
    if '"""' not in text and not text.endswith("\\"):
        return 'r"""\n' + text + '\n"""'
    return repr(text)


SETUP = '''
import duckdb
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

metrics = {}  # every cell stores its key numbers here


def note(text):
    """Write a plain-English finding under the cell."""
    try:
        from IPython.display import Markdown, display
        display(Markdown("📌 " + text))
    except ImportError:
        print("📌 " + text)


# Column types found when the files were uploaded (text stays text, so '05021' keeps its leading zero).
TYPES = __TYPES__
PANDAS = {"integer": "Int64", "decimal": "float64", "float": "float64", "boolean": "boolean", "text": "string"}
DUCK = {"integer": "BIGINT", "decimal": "DOUBLE", "float": "DOUBLE", "boolean": "BOOLEAN", "date": "DATE",
        "timestamp": "TIMESTAMP", "text": "VARCHAR"}

con = duckdb.connect()
source = {}
for name, types in TYPES.items():
    dates = [c for c, t in types.items() if t in ("date", "timestamp")]
    df = pd.read_csv(f"data/{name}.csv", dtype={c: PANDAS[t] for c, t in types.items() if t in PANDAS},
                     parse_dates=dates, keep_default_na=False, na_values=[""])
    source[name] = df
    con.register("_upload", df)
    cols = ", ".join(f'CAST("{c}" AS {DUCK[t]}) AS "{c}"' for c, t in types.items())
    con.execute(f'CREATE TABLE "{name}" AS SELECT {cols} FROM _upload')
    con.unregister("_upload")
scratch = duckdb.connect()  # an empty database, for the CREATE TABLE check


def sql(query, db=None):
    cursor = (db or con).execute(query)
    return cursor.df() if cursor.description else pd.DataFrame()


print({name: len(df) for name, df in source.items()})
'''


def setup_code(catalog: Catalog) -> str:
    spec = {name: t.types for name, t in catalog.tables.items()}
    return SETUP.strip("\n").replace("__TYPES__", json.dumps(spec, indent=4)) + "\n\n" + CHART_SETUP


def to_notebook(cells: list[Cell], d: Decision, catalog: Catalog, source: str = "uploaded files") -> str:
    """The cells as a Jupyter notebook that runs on DuckDB from data/*.csv (as in the project ZIP)."""
    import nbformat
    from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

    nb = [new_markdown_cell(
        "# AutoSQL Explorer notebook\n\n"
        f"Generated from {source}. Run it from the folder that contains `data/` (the project ZIP has both).\n\n"
        "Every step explains **what we found before**, **why it matters**, **what we're going to do** and **what the SQL does**. "
        "The SQL below is the DuckDB version that runs here; the MySQL and PostgreSQL versions are in the collapsed blocks. "
        "After a cell runs, the 📌 lines say **what we found**.\n\n## Assumptions\n\n"
        + "\n".join(f"- {a}" for a in assumptions(d)) + "\n\n## Decision trace\n\n" + "\n".join(f"- {s.short()}" for s in d.steps)),
        new_markdown_cell("## Setup"), new_code_cell(setup_code(catalog))]
    for i, cell in enumerate(cells, 1):
        shown = "".join(f"\n\n<details><summary>{LABELS[x]}</summary>\n\n```sql\n{cell.code(x)}```\n</details>" for x in ("mysql", "postgres"))
        nb.append(new_markdown_cell(f"## {i}. {cell.title}\n\n*{cell.section}*\n\n{cell.story.markdown()}\n\n"
                                    f"🛠 **What this SQL does:** {cell.story.code}{shown}"))
        lines = ["outputs = {}"]
        for st in cell.statements:
            if st.run == "show":
                continue
            db = ", scratch" if st.run == "scratch" else ""
            lines.append(f"# {st.title}\noutputs[{st.label!r}] = sql({_py_string(st.sql['duckdb'])}{db})")
            if st.run == "query":
                lines.append(f"display(outputs[{st.label!r}].head(20))")
        lines.append("result = list(outputs.values())[-1] if outputs else pd.DataFrame()")
        lines.append("# 📌 What we found\n" + cell.check)
        nb.append(new_code_cell("\n".join(lines)))
    return nbformat.writes(new_notebook(cells=nb))
