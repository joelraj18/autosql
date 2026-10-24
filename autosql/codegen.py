"""Turn a ``Decision`` into SQL cells that explain themselves.

Every cell carries a six-part narrative so a beginner can follow along:

  🔍 What we found before   - the evidence that led to this step        (Step.found)
  💡 Why it matters          - why that evidence changes what we do      (Step.why)
  🎯 What we're going to do  - the action and what it achieves           (Step.action)
  🛠 What this SQL does      - the SQL explained in plain words          (Step.code)
     The SQL itself, in MySQL 8 or PostgreSQL, then its result
  📌 What we found after     - written by the cell's check code (``note()``) from the actual result

Each statement is written once as a function of a ``Dialect`` and rendered for MySQL, PostgreSQL and DuckDB
(which runs it in the app). The cells follow the seven steps: schema & grain → validate → CTEs → KPIs →
window functions → root cause → fast and correct.
"""
from __future__ import annotations

import difflib
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .decision import (MIN_PERIODS_FOR_CORR, NTILE_BUCKETS, TOP_N, TREND_MIN_BASELINE, TREND_WINDOW, TREND_Z_LIMIT, Decision, Dimension,
                       Period)
from .dialect import DIALECTS, Dialect
from .narrative import Step
from .schema import Relationship

# Chart style shared by every check (and the notebook): one palette, hairline axes, no chart junk.
# Colours follow the reference data-viz palette: slot 1 blue for a single series, slot 2 orange for the suspected
# driver, a blue <-> red diverging pair for increases / decreases, a neutral ink for totals, a light grey for
# de-emphasised marks (partial periods), and a one-hue blue ramp for magnitude (heatmaps).
CHECK_HELPERS = (Path(__file__).with_name("checklib.py")).read_text(encoding="utf-8")   # period labels, statistics: see checklib.py

CHART_SETUP = """
import matplotlib as mpl
from matplotlib.colors import LinearSegmentedColormap

C_SERIES, C_DRIVER, C_TOTAL, C_MUTED = "#2a78d6", "#eb6834", "#52514e", "#c3c2b7"
C_UP, C_DOWN = "#2a78d6", "#e34948"
C_SEQ = LinearSegmentedColormap.from_list("autosql_blue", ["#cde2fb", "#86b6ef", "#3987e5", "#256abf", "#184f95", "#0d366b"])
mpl.rcParams.update({
    "axes.spines.top": False, "axes.spines.right": False, "axes.edgecolor": "#c3c2b7", "axes.linewidth": 0.8,
    "axes.grid": True, "axes.grid.axis": "y", "grid.color": "#e1e0d9", "grid.linewidth": 0.8, "axes.axisbelow": True,
    "axes.titlesize": 11, "axes.titleweight": "bold", "axes.labelcolor": "#52514e", "axes.titlecolor": "#0b0b0b",
    "xtick.color": "#52514e", "ytick.color": "#52514e", "font.size": 9.5, "lines.linewidth": 2, "figure.facecolor": "white",
})
""".strip()

SECTIONS = {
    1: "1 · Schema & grain", 2: "2 · Validate", 3: "3 · Build with CTEs", 4: "4 · KPIs",
    5: "5 · Window functions", 6: "6 · Root cause", 7: "7 · Fast & correct",
}
MAX_NULL_COLUMNS = 60      # more columns than this: the NULL profile lists only the columns that had NULLs
DISPLAY_LIMIT = 20         # rows listed by "show me examples" queries


# SQL techniques, detected in the generated MySQL text (keywords are always upper case there)
TECHNIQUES = (
    ("CTE (WITH)", r"\bWITH\b"), ("view", r"CREATE OR REPLACE VIEW"), ("CREATE TABLE + keys", r"CREATE TABLE"),
    ("window function", r"\bOVER\s*\("), ("LAG / LEAD", r"\b(LAG|LEAD)\("), ("ROW_NUMBER", r"\bROW_NUMBER\("),
    ("RANK / DENSE_RANK", r"\b(DENSE_)?RANK\("), ("NTILE", r"\bNTILE\("), ("CUME_DIST", r"\bCUME_DIST\("), ("window frame", r"\b(ROWS|RANGE) BETWEEN\b"),
    ("PARTITION BY", r"\bPARTITION\s+BY\b"), ("LEFT JOIN", r"\bLEFT\s+JOIN\b"), ("anti-join", r"\bNOT EXISTS\b|LEFT\s+JOIN[^;]*IS NULL"),
    ("conditional aggregation", r"\b(SUM|COUNT|AVG)\(\s*CASE\b"), ("CASE", r"\bCASE\b"), ("GROUP BY … HAVING", r"\bHAVING\b"),
    ("UNION ALL", r"\bUNION\s+ALL\b"), ("COUNT(DISTINCT)", r"COUNT\(DISTINCT\b"), ("NULLIF division guard", r"\bNULLIF\("),
    ("EXPLAIN", r"\bEXPLAIN\b"), ("CREATE INDEX", r"\bCREATE INDEX\b"),
)


@dataclass(frozen=True)
class Statement:
    label: str                 # key of the result in ``outputs`` (check code and notebook)
    title: str                 # shown above the SQL and its result
    sql: dict[str, str]        # dialect -> SQL text (no trailing semicolon)
    run: str = "query"         # query | scratch (run in an empty database) | show (never run: dialect-only demo)


@dataclass(frozen=True)
class Cell:
    id: str
    title: str
    section: str
    story: Step
    statements: tuple[Statement, ...]
    check: str                 # Python: reads ``outputs`` / ``result``, writes ``note()`` and ``metrics``

    def techniques(self) -> list[str]:
        """The SQL techniques the cell uses (comments ignored), for the app's technical notes."""
        sql = "\n".join(re.sub(r"--[^\n]*", "", s.sql["mysql"]) for s in self.statements)
        return [name for name, pattern in TECHNIQUES if re.search(pattern, sql)]

    def dialect_diff(self, a: str = "mysql", b: str = "postgres") -> list[tuple[str, str]]:
        """The lines that differ between two dialects, side by side: what changes when you move the query."""
        out = []
        for st in self.statements:
            la, lb = st.sql[a].splitlines(), st.sql[b].splitlines()
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, la, lb, autojunk=False).get_opcodes():
                if tag == "replace" and i2 - i1 == j2 - j1:          # line for line where the blocks align
                    out += [(x.strip(), y.strip()) for x, y in zip(la[i1:i2], lb[j1:j2])]
                elif tag != "equal":
                    out.append(("\n".join(x.strip() for x in la[i1:i2]), "\n".join(x.strip() for x in lb[j1:j2])))
        return out

    def code(self, dialect: str) -> str:
        """The cell's SQL in one dialect, with a 'Why' header and one numbered block per statement."""
        why = re.sub(r"[*`]", "", self.story.action)
        lines = [textwrap.fill(why, width=100, initial_indent="-- Why: ", subsequent_indent="--      ")]
        for i, st in enumerate(self.statements, 1):
            prefix = f"-- {i}) " if len(self.statements) > 1 else "-- "
            note = "  [not run here: shown for your database]" if st.run == "show" else ""
            lines.append(f"\n{prefix}{st.title}{note}\n{st.sql[dialect]};")
        return "\n".join(lines) + "\n"


def fill(template: str, **values) -> str:
    """Dedent a template and replace <<name>> placeholders (avoids escaping every brace)."""
    text = textwrap.dedent(template).strip("\n")
    for key, value in values.items():
        text = text.replace(f"<<{key}>>", str(value))
    assert "<<" not in text, text
    return text


def render(fn: Callable[[Dialect], str]) -> dict[str, str]:
    return {name: fn(Dialect(name)) for name in DIALECTS}


def stmt(label: str, title: str, fn: Callable[[Dialect], str], run: str = "query") -> Statement:
    return Statement(label, title, render(fn), run)


def union_all(parts: list[str]) -> str:
    return "\nUNION ALL\n".join(parts)


# ═════════════════════════════════════════════════════════════════════════
# Builder: names, aliases and shared SQL fragments for one decision
# ═════════════════════════════════════════════════════════════════════════
class Builder:
    def __init__(self, d: Decision):
        self.d = d
        self.p = d.profile
        self.aliases: dict[str, str] = {}
        used = [d.fact, d.header, d.customer_rel.parent_table if d.customer_rel else None]
        used += [r.parent_table for dim in d.dimensions for r in dim.path] + [d.item[0] if d.item else None]
        order = list(dict.fromkeys([t for t in used if t] + list(self.p.tables)))
        for t in order:
            self.alias(t)

    # ── names ────────────────────────────────────────────────────────────
    def alias(self, table: str) -> str:
        if table not in self.aliases:
            words = [w for w in table.split("_") if w]
            options = ["".join(w[0] for w in words) if len(words) > 1 else table[:1], table[:3], table[:4]]
            taken = set(self.aliases.values())
            from .dialect import RESERVED
            pick = next((a for a in options if a and a not in taken and a not in RESERVED and re.match(r"^[a-z_]", a)), None)
            n = 2
            while pick is None:
                cand = f"{options[0] or 't'}{n}"
                pick = cand if cand not in taken else None
                n += 1
            self.aliases[table] = pick
        return self.aliases[table]

    def deduped(self, D: Dialect, table: str) -> str:
        """The table itself, or its de-duplicated CTE."""
        return f"{table}_deduped" if table in self.d.dedupe else D.ident(table)

    def src(self, D: Dialect, table: str) -> str:
        """FROM-clause name of a table in the views: de-duplicated, then filtered by the business rules."""
        return f"{table}_kept" if table in self.filtered else self.deduped(D, table)

    # ── business rules (cancelled, test or other-currency rows) ──────────
    def filters_on(self, table: str) -> list:
        return [f for f in self.d.row_filters if f.table == table]

    @property
    def header_filtered(self) -> bool:
        return bool(self.d.header and self.filters_on(self.d.header) and self.d.header_rel)

    @property
    def filtered(self) -> set[str]:
        """Tables read through a ``<table>_kept`` CTE: the filtered ones, and the fact when its order table is filtered."""
        out = {f.table for f in self.d.row_filters}
        return out | ({self.d.fact} if self.header_filtered else set())

    def left_out(self, D: Dialect, alias: str, table: str) -> str | None:
        """The rows of ``table`` the rules leave out. NULL never is: NULL IN (...) is not true, so the row stays."""
        parts = []
        for f in self.filters_on(table):
            vals = ", ".join(D.literal(v) for v in f.values)
            parts.append(f"{D.col(alias, f.column)} = {vals}" if len(f.values) == 1 else f"{D.col(alias, f.column)} IN ({vals})")
        return " OR ".join(parts) or None

    def kept_where(self, D: Dialect, alias: str, table: str) -> list[str]:
        """The conditions a kept row of ``table`` meets: the exact complement of ``left_out``, with NULL spelled out."""
        conds = []
        for f in self.filters_on(table):
            c, vals = D.col(alias, f.column), ", ".join(D.literal(v) for v in f.values)
            conds.append(f"({c} IS NULL OR {c} <> {vals})" if len(f.values) == 1 else f"({c} IS NULL OR {c} NOT IN ({vals}))")
        if table == self.d.fact and self.header_filtered:
            conds.append("NOT " + self.header_left_out(D, alias, 23))
        return conds

    def header_left_out(self, D: Dialect, fact_alias: str, at: int) -> str:
        """EXISTS (…): the line's order is one the rules leave out. ``at`` is the column where the sub-query's SELECT starts."""
        pad = " " * at
        return (f"EXISTS (SELECT 1\n{pad}FROM   {self.deduped(D, self.d.header)} h\n{pad}WHERE  {self.header_match(D, 'h', fact_alias)}\n"
                f"{pad}  AND  ({self.left_out(D, 'h', self.d.header)}))")

    def header_match(self, D: Dialect, header_alias: str, fact_alias: str) -> str:
        r = self.d.header_rel
        a, b = D.col(header_alias, r.parent_col), D.col(fact_alias, r.child_col)
        return f"{D.as_text(a)} = {D.as_text(b)}" if r.type_mismatch else f"{a} = {b}"

    def rules_comment(self, table: str) -> str:
        rules = [f.short() for f in self.filters_on(table)]
        if table == self.d.fact and self.header_filtered:
            rules.append(f"every line of an order left out in {self.d.header}")
        return "; ".join(rules)

    def kept_ctes(self, D: Dialect, tables: list[str]) -> list[str]:
        out = []
        for t in tables:
            if t not in self.filtered:
                continue
            out.append(fill('''
                <<name>>_kept AS (               -- leave out what is not a sale: <<why>>
                    SELECT *
                    FROM   <<src>> x
                    WHERE  <<conds>>
                )''', name=t, why=self.rules_comment(t), src=self.deduped(D, t), conds="\n      AND  ".join(self.kept_where(D, "x", t))))
        return out

    def source_ctes(self, D: Dialect, tables: list[str]) -> list[str]:
        """The CTEs a query reading ``tables`` through ``src()`` needs: de-duplication first, then the business rules."""
        tables = list(dict.fromkeys(tables))
        deduped = tables + ([self.d.header] if self.d.fact in tables and self.header_filtered and self.d.header not in tables else [])
        return self.dedupe_ctes(D, deduped) + self.kept_ctes(D, tables)

    def dedupe_ctes(self, D: Dialect, tables: list[str]) -> list[str]:
        out = []
        for t in tables:
            if t not in self.d.dedupe:
                continue
            keys, order = self.d.dedupe[t]
            a = "x"
            part = ", ".join(D.col(a, k) for k in keys)
            by = f"{D.col(a, order)} IS NULL, {D.col(a, order)} DESC" if order else part
            keep = f"keep the latest by {order}" if order else "keep one copy (arbitrary: there is no date to choose by)"
            out.append(fill('''
                <<name>>_deduped AS (            -- <<table>> repeats its key: <<keep>>
                    SELECT *
                    FROM (SELECT x.*,
                                 ROW_NUMBER() OVER (PARTITION BY <<part>> ORDER BY <<by>>) AS row_num
                          FROM   <<src>> x) ranked
                    WHERE  row_num = 1
                )''', name=t, table=t, keep=keep, part=part, by=by, src=D.ident(t)))
        return out

    # ── shared columns ───────────────────────────────────────────────────
    @property
    def money(self) -> bool:
        return self.d.revenue.kind != "count"

    def line_revenue(self, D: Dialect, a: str, price_alias: str | None = None) -> str:
        rev = self.d.revenue
        if rev.kind == "price_qty":
            price = D.col(price_alias or a, rev.price)
            expr = f"{price} * {D.col(a, rev.quantity)}"
            if rev.discount:
                disc = D.col(a, rev.discount) + (" / 100.0" if rev.discount_scale == 100 else "")
                expr += f" * (1 - COALESCE({disc}, 0))"
            return expr
        if rev.kind == "amount":
            return D.col(a, rev.amount)
        return "1"

    def revenue_formula(self) -> str:
        rev = self.d.revenue
        if rev.kind == "price_qty":
            disc = f" × (1 − {rev.discount}{' / 100' if rev.discount_scale == 100 else ''})" if rev.discount else ""
            source = f"{rev.price_rel.parent_table}." if rev.price_rel else ""
            return f"{source}{rev.price} × {rev.quantity}{disc}"
        if rev.kind == "amount":
            return f"SUM({rev.amount})"
        return "row count"

    @property
    def order_key(self) -> str:
        return self.d.order_key or "row_num"

    @property
    def customer_col(self) -> str | None:
        return self.d.customer[1] if self.d.customer else None

    @property
    def date_col(self) -> str | None:
        return self.d.date[1] if self.d.date else None

    def period_exprs(self, D: Dialect, expr: str) -> list[str]:
        """The expressions a period is grouped by, in the order of ``d.period_cols``."""
        d = self.d
        if d.period == "week":
            return [D.isoyear(expr), D.isoweek(expr), D.week_index(expr, d.date_is_timestamp)]
        return [D.year(expr)] + ([D.period(d.period, expr)] if d.number_col else [])

    def period_select(self, D: Dialect, expr: str) -> list[str]:
        return [f"{e} AS {c}" for e, c in zip(self.period_exprs(D, expr), self.d.period_cols)]

    def period_where(self, p: Period, prefix: str = "") -> str:
        d = self.d
        cond = f"{prefix}{d.year_col} = {p.year}"
        if d.number_col:
            cond = f"({cond} AND {prefix}{d.number_col} = {p.number})"
        return cond

    def period_order(self, prefix: str = "") -> str:
        return ", ".join(prefix + c for c in self.d.period_cols)

    def partial_case(self) -> str | None:
        parts = [self.period_where(p) for p in self.d.partial_periods]
        if not parts:
            return None
        return f"CASE WHEN {' OR '.join(parts)} THEN 'partial' ELSE 'complete' END AS period_status"

    def rating_expr(self, D: Dialect, a: str) -> str | None:
        r = self.d.rating
        if r is None:
            return None
        if not r.scale:
            return D.col(a, r.name)
        whens = "\n".join(f"            WHEN {D.literal(label)} THEN {score}" for label, score in r.scale)
        return f"CASE {D.col(a, r.name)}\n{whens}\n       END"

    def dim_expr(self, D: Dialect, dim: Dimension, alias: str) -> str:
        col = D.col(alias, dim.column)
        if self.p.col(dim.table, dim.column).logical_type != "text":
            col = D.as_text(col)
        return f"COALESCE({col}, 'Unknown') AS {D.ident(dim.alias)}"

    def joins_for(self, D: Dialect, paths: list[tuple[Relationship, ...]], start_alias: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        """LEFT JOIN clauses following foreign-key paths (each table joined once). Returns (clauses, table -> alias)."""
        clauses, where = [], dict(start_alias)
        for path in paths:
            for r in path:
                if r.parent_table in where:
                    continue
                child = where.get(r.child_table)
                if child is None:
                    break
                pa = self.alias(r.parent_table)
                cond = f"{D.col(pa, r.parent_col)} = {D.col(child, r.child_col)}"
                if r.type_mismatch:
                    cond = f"{D.as_text(D.col(pa, r.parent_col))} = {D.as_text(D.col(child, r.child_col))}"
                clauses.append(f"LEFT   JOIN {self.src(D, r.parent_table)} {pa} ON {cond}")
                where[r.parent_table] = pa
        return clauses, where


# ═════════════════════════════════════════════════════════════════════════
# 1 · Schema & grain
# ═════════════════════════════════════════════════════════════════════════
def _topological(d: Decision) -> list[str]:
    parents = {t: {r.parent_table for r in d.schema.accepted if r.child_table == t and r.parent_table != t} for t in d.profile.tables}
    out, seen = [], set()

    def visit(t: str, stack: tuple = ()):
        if t in seen or t in stack:
            return
        for p in sorted(parents[t]):
            visit(p, stack + (t,))
        seen.add(t)
        out.append(t)
    for t in d.profile.tables:
        visit(t)
    return out


def _ddl(b: Builder, D: Dialect, table: str) -> str:
    d, t = b.d, b.p[table]
    money = {"price", "amount", "charge"}
    fk_cols = {r.child_col: r for r in d.schema.accepted if r.child_table == table}
    lines = []
    width = max(len(D.ident(c.name)) for c in t.columns)
    for c in t.columns:
        big = c.logical_type == "integer" and c.maximum is not None and abs(float(c.maximum)) > 2_147_483_647
        key = c.name in t.primary_key or c.name in fk_cols
        typ = D.type_name(c.logical_type, max_len=c.max_len, scale=2 if c.kind in money else 4, big=big, key=key)
        if key and c.logical_type == "integer" and not big:
            # a foreign key must have exactly the type of the key it points at
            r = fk_cols.get(c.name)
            if r is not None:
                pc = b.p.col(r.parent_table, r.parent_col)
                if pc.logical_type == "integer" and pc.maximum is not None and abs(float(pc.maximum)) > 2_147_483_647:
                    typ = D.type_name("integer", big=True)
        if c.logical_type == "text" and key:
            r = fk_cols.get(c.name)
            length = max(c.max_len, b.p.col(r.parent_table, r.parent_col).max_len if r else 0)
            typ = D.type_name("text", max_len=length, key=True)
        if c.name in t.primary_key and c.logical_type == "text":
            longest = max([c.max_len] + [b.p.col(r.child_table, r.child_col).max_len for r in d.schema.accepted
                                         if r.parent_table == table and r.parent_col == c.name])
            typ = D.type_name("text", max_len=longest, key=True)
        null = " NOT NULL" if c.n_null == 0 and c.n_rows else ""
        lines.append(f"    {D.ident(c.name).ljust(width)}  {typ}{null}")
    if t.key_status in ("single", "composite"):
        lines.append(f"    PRIMARY KEY ({', '.join(D.ident(k) for k in t.primary_key)})")
    elif t.key_status == "intended":
        lines.append(f"    -- PRIMARY KEY ({', '.join(t.primary_key)}) is not declared: it has duplicates (see the duplicates step)")
    for r in d.schema.accepted:
        if r.child_table != table:
            continue
        parent = b.p[r.parent_table]
        if parent.key_status not in ("single",) or r.orphan_rows:
            why = "has orphan rows (see the orphans step)" if r.orphan_rows else f"{r.parent_table} has no unique key"
            lines.append(f"    -- FOREIGN KEY ({r.child_col}) REFERENCES {r.parent_table} ({r.parent_col}): not declared, {why}")
            continue
        lines.append(f"    FOREIGN KEY ({D.ident(r.child_col)}) REFERENCES {D.ident(r.parent_table)} ({D.ident(r.parent_col)})")
    body = ",\n".join(l for l in lines if not l.lstrip().startswith("--"))
    comments = "\n".join(l for l in lines if l.lstrip().startswith("--"))
    return f"CREATE TABLE {D.ident(table)} (\n{body}{chr(10) + comments if comments else ''}\n)"


def _schema_cell(b: Builder) -> Cell:
    d = b.d
    order = _topological(d)
    statements = [stmt(f"create_{t}", f"`{t}`: {b.p[t].grain}", lambda D, t=t: _ddl(b, D, t), run="scratch") for t in order]
    n_fk = sum(1 for r in d.schema.accepted)
    story = Step(
        "schema",
        f"There are {len(order)} tables and {n_fk} trusted relationship(s) between them.",
        "A schema written down as CREATE TABLE statements, with its primary and foreign keys, is the map every later query relies on. "
        "It also documents types: dates as DATE, money as DECIMAL, codes as text so leading zeros survive.",
        "We write the CREATE TABLE statements, parents first, and test them on an empty database, so that the keys are declared exactly as the data supports.",
        "One CREATE TABLE per table, in an order where every referenced table already exists. Types come from the data "
        "(text length rounded up, NOT NULL when no value is missing). PRIMARY KEY is the key found during profiling; FOREIGN KEY "
        "is declared only for trusted links with no orphans. In the app the statements run on an empty database, as a syntax check.",
    )
    check = fill('''
        metrics["tables"] = <<n>>
        metrics["foreign_keys"] = <<fk>>
        note(f"All <<n>> CREATE TABLE statements ran on an empty database, with <<pk>> primary key(s) and <<fk_declared>> foreign key(s) declared.")
    ''', n=len(order), fk=n_fk, pk=sum(1 for t in order if b.p[t].key_status in ("single", "composite")),
        fk_declared=sum(1 for r in d.schema.accepted if b.p[r.parent_table].key_status == "single" and not r.orphan_rows))
    return Cell("schema", "Write down the schema", SECTIONS[1], story, tuple(statements), check)


def _key_count(D: Dialect, b: Builder, table: str) -> str:
    t = b.p[table]
    if len(t.primary_key) == 1:
        return f"COUNT(DISTINCT {D.ident(t.primary_key[0])})"
    cols = ", ".join(D.ident(k) for k in t.primary_key) if t.primary_key else "*"
    return f"(SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM {D.ident(table)}) k)"


def _grain_cell(b: Builder) -> Cell:
    def sql(D: Dialect) -> str:
        parts = []
        for t in b.p.tables.values():
            keys = t.primary_key
            null_test = " OR ".join(f"{D.ident(k)} IS NULL" for k in keys) if keys else "1 = 0"
            label = " + ".join(keys) if keys else "(whole row)"
            parts.append(fill('''
                SELECT <<name>> AS table_name,
                       <<key>> AS key_columns,
                       COUNT(*) AS row_count,
                       <<distinct>> AS distinct_keys,
                       SUM(CASE WHEN <<nulls>> THEN 1 ELSE 0 END) AS null_keys
                FROM   <<table>>''', name=D.literal(t.name), key=D.literal(label), distinct=_key_count(D, b, t.name),
                                     nulls=null_test, table=D.ident(t.name)))
        return union_all(parts)

    story = Step(
        "grain",
        "Profiling found a key for each table: " + "; ".join(f"`{t.name}`: {t.grain}" for t in list(b.p.tables.values())[:4])
        + ("…" if len(b.p.tables) > 4 else "") + ".",
        "If a key is really unique, COUNT(*) equals the number of distinct keys. If not, every join on it multiplies rows. "
        "COUNT(DISTINCT key) ignores NULLs, so empty keys are counted separately.",
        "We compare rows with distinct keys in every table, so that the grain is proven by SQL, not assumed.",
        "For each table: COUNT(*), the number of distinct key values (for a two-column key, the number of distinct pairs), and "
        "how many rows have an empty key. One SELECT per table, stacked with UNION ALL.",
    )
    check = fill('''
        bad = result[(result["row_count"] != result["distinct_keys"]) | (result["null_keys"] > 0)]
        metrics["grain_ok"] = bad.empty
        for row in result.itertuples():
            ok = row.row_count == row.distinct_keys and row.null_keys == 0
            metrics.setdefault("grain", {})[row.table_name] = {"rows": int(row.row_count), "distinct": int(row.distinct_keys), "ok": bool(ok)}
        if bad.empty:
            note(f"Every key is unique and never empty: in all {len(result)} tables, COUNT(*) equals the number of distinct keys.")
        for row in bad.itertuples():
            note(f"`{row.table_name}`: {int(row.row_count):,} rows but {int(row.distinct_keys):,} distinct `{row.key_columns}` "
                 f"and {int(row.null_keys):,} empty key(s). Joins on this key will repeat rows until it is de-duplicated.")
    ''')
    return Cell("grain", "Prove the grain", SECTIONS[1], story, (stmt("grain", "Rows vs distinct keys per table", sql),), check)


def _cardinality_cell(b: Builder) -> Cell:
    d = b.d
    rels = [r for r in d.schema.accepted]

    def per_parent(D: Dialect) -> str:
        parts = []
        for r in rels:
            pk, fk = D.ident(r.parent_col), D.ident(r.child_col)
            cond = f"c.{fk} = p.{pk}" if not r.type_mismatch else f"{D.as_text('c.' + fk)} = {D.as_text('p.' + pk)}"
            parts.append(fill('''
                SELECT <<label>> AS relationship,
                       COUNT(*) AS parents,
                       SUM(CASE WHEN children > 0 THEN 1 ELSE 0 END) AS parents_with_children,
                       MIN(children) AS min_children,
                       <<avg>> AS avg_children,
                       MAX(children) AS max_children
                FROM   (SELECT p.<<pk>>, COUNT(c.<<fk>>) AS children
                        FROM   <<parent>> p
                        LEFT   JOIN <<child>> c ON <<cond>>
                        GROUP  BY p.<<pk>>) per_parent''',
                label=D.literal(f"{r.child_table}.{r.child_col} -> {r.parent_table}"), avg=D.round("AVG(children)", 2),
                pk=pk, fk=fk, parent=D.ident(r.parent_table), child=D.ident(r.child_table), cond=cond))
        return union_all(parts)

    statements = [stmt("cardinality", "Child rows per parent row, for every relationship", per_parent)]
    for i, f in enumerate(d.fanouts[:2]):
        r = next(x for x in d.schema.accepted if x.parent_table == f.parent_table and x.child_table == f.child_table and x.child_col == f.via)

        def fan(D: Dialect, f=f, r=r) -> str:
            pa, ca = b.alias(f.parent_table), b.alias(f.child_table)
            if pa == ca:
                ca = ca + "2"
            return fill('''
                -- <<col>> is stored once per <<parent>> row; joining <<child>> repeats it on every <<child>> row.
                SELECT (SELECT SUM(<<colq>>) FROM <<parentq>>) AS total_counted_once,
                       (SELECT SUM(<<pa>>.<<colq>>)
                        FROM   <<parentq>> <<pa>>
                        JOIN   <<childq>> <<ca>> ON <<ca>>.<<fk>> = <<pa>>.<<pk>>) AS total_after_join''',
                        col=f.column, parent=f.parent_table, child=f.child_table, colq=D.ident(f.column), parentq=D.ident(f.parent_table),
                        childq=D.ident(f.child_table), pa=pa, ca=ca, fk=D.ident(r.child_col), pk=D.ident(r.parent_col))
        statements.append(stmt(f"fanout_{i + 1}", f"Fan-out demo: `{f.parent_table}.{f.column}` before and after joining `{f.child_table}`", fan))
    first = d.fanouts[0] if d.fanouts else None
    story = Step(
        "cardinality",
        f"There are {len(rels)} trusted relationship(s)" + (f", and `{first.parent_table}.{first.column}` sits on the 'one' side of a one-to-many join" if first else "") + ".",
        "A 1:N join repeats every parent row once per child. Anything stored on the parent (freight, a budget, a target) is then summed several times: "
        "the classic fan-out that silently inflates totals.",
        "We measure children per parent for every link" + (f" and show `{first.column}` summed before and after the join" if first else "")
        + ", so that the size of the trap is visible in your own numbers.",
        "For each relationship, a LEFT JOIN from parent to child counts the children of every parent (0 when none), then MIN / AVG / MAX "
        "summarise them. The fan-out demo sums the parent column once per parent, and again after an inner join to the child table.",
    )
    check = fill('''
        rel = outputs["cardinality"]
        for row in rel.itertuples():
            kind = "1:1" if row.max_children <= 1 else "1:N"
            metrics.setdefault("cardinality", {})[row.relationship] = {"kind": kind, "max": int(row.max_children), "avg": float(row.avg_children)}
        widest = rel.sort_values("max_children", ascending=False).iloc[0]
        note(f"Widest join: `{widest.relationship}`, up to {int(widest.max_children):,} child rows for one parent "
             f"({float(widest.avg_children):.2f} on average; {int(widest.parents - widest.parents_with_children):,} parent(s) have none).")
        for label, name in <<fans>>:
            row = outputs[label].iloc[0]
            once, joined = float(row.total_counted_once), float(row.total_after_join)
            factor = joined / once if once else float("nan")
            metrics.setdefault("fanout", {"column": name, "total": once, "joined": joined, "factor": factor})
            note(f"**Fan-out:** `{name}` totals **{once:,.2f}** counted once, but **{joined:,.2f}** after the join "
                 f"({factor:.2f}× too high). It must be summed before joining, or at its own grain.")
    ''', fans=repr([(f"fanout_{i + 1}", f"{f.parent_table}.{f.column}") for i, f in enumerate(d.fanouts[:2])]))
    return Cell("cardinality", "Join cardinality and fan-out", SECTIONS[1], story, tuple(statements), check)


# ═════════════════════════════════════════════════════════════════════════
# 2 · Validate
# ═════════════════════════════════════════════════════════════════════════
def _duplicates_cell(b: Builder) -> Cell:
    p = b.p

    def summary(D: Dialect) -> str:
        parts = []
        for t in p.tables.values():
            if t.primary_key:
                cols = ", ".join(D.ident(k) for k in t.primary_key)
                not_null = " AND ".join(f"{D.ident(k)} IS NOT NULL" for k in t.primary_key)
                label = " + ".join(t.primary_key)
            elif len(t.columns) <= 40:
                cols = ", ".join(D.ident(c.name) for c in t.columns)
                not_null, label = "1 = 1", "(whole row)"
            else:
                continue
            parts.append(fill('''
                SELECT <<name>> AS table_name,
                       <<label>> AS checked_columns,
                       COUNT(*) AS duplicated_values,
                       COALESCE(SUM(copies - 1), 0) AS extra_rows
                FROM   (SELECT <<cols>>, COUNT(*) AS copies
                        FROM   <<table>>
                        WHERE  <<nn>>
                        GROUP  BY <<cols>>
                        HAVING COUNT(*) > 1) dup''', name=D.literal(t.name), label=D.literal(label), cols=cols,
                                                     table=D.ident(t.name), nn=not_null))
        return union_all(parts)

    statements = [stmt("duplicates", "Duplicated keys per table (GROUP BY … HAVING COUNT(*) > 1)", summary)]
    for t in p.tables.values():
        if t.duplicate_keys and t.primary_key:
            def listing(D: Dialect, t=t) -> str:
                cols = ", ".join(D.ident(k) for k in t.primary_key)
                return fill('''
                    SELECT <<cols>>, COUNT(*) AS copies
                    FROM   <<table>>
                    WHERE  <<nn>>
                    GROUP  BY <<cols>>
                    HAVING COUNT(*) > 1
                    ORDER  BY copies DESC, <<cols>>
                    LIMIT  <<n>>''', cols=cols, table=D.ident(t.name), n=DISPLAY_LIMIT,
                            nn=" AND ".join(f"{D.ident(k)} IS NOT NULL" for k in t.primary_key))
            statements.append(stmt(f"duplicates_{t.name}", f"The most repeated keys in `{t.name}`", listing))
    found = [t for t in p.tables.values() if t.duplicate_keys]
    story = Step(
        "duplicates",
        ("Profiling found repeated keys in " + _names([t.name for t in found]) + ".") if found else
        "Profiling found no repeated key, but a profile is only as good as the query behind it.",
        "A duplicated key is either a double-loaded row or a sign that the table's grain is finer than it looks. Either way it inflates every count and sum that joins to it.",
        "We list duplicated keys with GROUP BY … HAVING COUNT(*) > 1, so that each one can be traced back to the source.",
        "Groups each table by its key and keeps only the groups with more than one row; a table without a key is grouped by all of its "
        "columns, which finds exact copies. The summary counts the duplicated values and the extra rows they add.",
    )
    check = fill('''
        dup = outputs["duplicates"]
        metrics["duplicate_keys"] = {row.table_name: int(row.duplicated_values) for row in dup.itertuples()}
        bad = dup[dup["duplicated_values"] > 0]
        if bad.empty:
            note("No duplicates: every key value appears exactly once in its table.")
        for row in bad.itertuples():
            note(f"`{row.table_name}`: {int(row.duplicated_values):,} `{row.checked_columns}` value(s) repeat, adding "
                 f"{int(row.extra_rows):,} extra row(s). The analysis keeps one row per key with ROW_NUMBER().")
    ''')
    return Cell("duplicates", "Find duplicates", SECTIONS[2], story, tuple(statements), check)


def _nulls_cell(b: Builder) -> Cell:
    p = b.p
    total = sum(len(t.columns) for t in p.tables.values())
    only_gappy = total > MAX_NULL_COLUMNS

    def sql(D: Dialect) -> str:
        parts = []
        for t in p.tables.values():
            for c in t.columns:
                if only_gappy and c.n_null == 0:
                    continue
                parts.append(f"SELECT {D.literal(t.name)} AS table_name, {D.literal(c.name)} AS column_name, "
                             f"COUNT(*) AS row_count, COUNT(*) - COUNT({D.ident(c.name)}) AS null_rows FROM {D.ident(t.name)}")
        if not parts:
            t = next(iter(p.tables.values()))
            parts.append(f"SELECT {D.literal(t.name)} AS table_name, {D.literal('(none)')} AS column_name, COUNT(*) AS row_count, "
                         f"0 AS null_rows FROM {D.ident(t.name)}")
        head = ("-- Only the columns that had NULLs when the data was profiled; add the others back to re-check new data.\n"
                if only_gappy else "")
        return head + "\nUNION ALL\n".join(parts)

    gappy = sorted((c for t in p.tables.values() for c in t.columns if c.n_null), key=lambda c: -c.null_frac)
    story = Step(
        "nulls",
        (f"{len(gappy)} column(s) have empty values, e.g. " + ", ".join(f"`{c.table}.{c.name}` ({c.null_frac:.0%})" for c in gappy[:3]) + ".")
        if gappy else "No column has empty values.",
        "NULL means unknown. COUNT(col), SUM and AVG silently skip it, so an average over a gappy column describes fewer rows than you think, "
        "and a NULL foreign key drops the row from an inner join.",
        "We count NULLs in every column, so that every average and join later can be read with the right caveat.",
        "For every column: COUNT(*) − COUNT(column). COUNT(*) counts rows; COUNT(column) counts only rows where the column has a value, "
        "so the difference is the number of NULLs. One line per column, stacked with UNION ALL.",
    )
    check = fill('''
        nulls = result.assign(null_pct=100 * result["null_rows"] / result["row_count"].clip(lower=1))
        gappy = nulls[nulls["null_rows"] > 0].sort_values("null_pct", ascending=False)
        metrics["nulls"] = {f"{r.table_name}.{r.column_name}": round(float(r.null_pct), 1) for r in gappy.itertuples()}
        if gappy.empty:
            note("No NULLs anywhere: every column is filled in every row.")
        else:
            top = gappy.head(12).iloc[::-1]
            plt.figure(figsize=(8, max(2.5, 0.35 * len(top))))
            plt.barh(top["table_name"] + "." + top["column_name"], top["null_pct"], color=C_SERIES, height=0.6)
            plt.xlabel("% of rows empty"); plt.title("Columns with NULLs")
            plt.tight_layout(); plt.show()
            note(f"{len(gappy)} column(s) have NULLs. Most empty: " + ", ".join(
                f"`{r.table_name}.{r.column_name}` {r.null_pct:.1f}%" for r in gappy.head(3).itertuples()) + ".")
        for table, col, meaning in <<watch>>:
            row = nulls[(nulls["table_name"] == table) & (nulls["column_name"] == col)]
            if len(row) and int(row["null_rows"].iloc[0]):
                note(f"`{table}.{col}` is empty in {int(row['null_rows'].iloc[0]):,} row(s): {meaning}")
    ''', watch=repr(_null_watch(b)))
    return Cell("nulls", "Profile NULLs", SECTIONS[2], story, (stmt("nulls", "NULLs per column: COUNT(*) − COUNT(column)", sql),), check)


def _null_watch(b: Builder) -> list[tuple[str, str, str]]:
    d, out = b.d, []
    if d.lead:
        out.append((d.lead[0], d.lead[2], f"those orders have no {d.lead_name}, so AVG({d.lead_name}) leaves them out."))
    if d.rating:
        out.append((d.rating.table, d.rating.name, "those orders have no rating, so the average rating describes only rated orders."))
    for r in d.schema.accepted:
        if r.child_nulls:
            out.append((r.child_table, r.child_col, f"those rows have no `{r.parent_table}` row, so an INNER JOIN would drop them."))
    return out


def _date_range_cell(b: Builder) -> Cell:
    d, p = b.d, b.p
    dates = [(t.name, c) for t in p.tables.values() for c in t.by_role("datetime")]

    def ranges(D: Dialect) -> str:
        return union_all([fill('''
            SELECT <<t>> AS table_name, <<c>> AS column_name,
                   MIN(<<col>>) AS earliest, MAX(<<col>>) AS latest,
                   COUNT(*) - COUNT(<<col>>) AS null_rows
            FROM   <<table>>''', t=D.literal(t), c=D.literal(c.name), col=D.ident(c.name), table=D.ident(t)) for t, c in dates])

    statements = [stmt("date_ranges", "Earliest and latest value of every date column", ranges)]
    if d.date and d.period:
        table, col = d.date

        def per_period(D: Dialect) -> str:
            cq = D.ident(col)
            sel = b.period_select(D, cq)
            group = b.period_exprs(D, cq)
            return fill('''
                SELECT <<sel>>,
                       COUNT(*) AS row_count,
                       MIN(<<c>>) AS first_date,
                       MAX(<<c>>) AS last_date
                FROM   <<t>>
                WHERE  <<c>> IS NOT NULL
                GROUP  BY <<group>>
                ORDER  BY <<order>>''', sel=",\n       ".join(sel), c=cq, t=D.ident(table), group=", ".join(group), order=b.period_order())
        statements.append(stmt("periods", f"Rows per {d.period}, with the first and last date seen", per_period))
    if d.lead:
        table, start, end = d.lead

        def lead(D: Dialect) -> str:
            days = D.days_between(D.ident(start), D.ident(end), _lead_is_timestamp(b))
            return fill('''
                SELECT COUNT(*) AS row_count,
                       COUNT(<<e>>) AS with_<<end>>,
                       SUM(CASE WHEN <<e>> < <<s>> THEN 1 ELSE 0 END) AS before_<<start>>,
                       SUM(CASE WHEN <<days>> > 365 THEN 1 ELSE 0 END) AS over_a_year,
                       MIN(<<days>>) AS min_days,
                       MAX(<<days>>) AS max_days
                FROM   <<t>>''', e=D.ident(end), s=D.ident(start), end=end, start=start, days=days, t=D.ident(table))
        statements.append(stmt("lead_sanity", f"Is `{end}` ever before `{start}`, or implausibly late?", lead))
    for table, col, unit in d.date_parts:
        base = d.date[1] if d.date and d.date[0] == table else p[table].by_role("datetime")[0].name

        def parts(D: Dialect, table=table, col=col, unit=unit, base=base) -> str:
            derived = {"year": D.year, "quarter": D.quarter, "month": D.month}[unit](D.ident(base))
            return fill('''
                SELECT COUNT(*) AS row_count,
                       SUM(CASE WHEN <<c>> <> <<derived>> THEN 1 ELSE 0 END) AS mismatched
                FROM   <<t>>
                WHERE  <<c>> IS NOT NULL AND <<b>> IS NOT NULL''', c=D.ident(col), derived=derived, t=D.ident(table), b=D.ident(base))
        statements.append(stmt(f"part_{table}_{col}", f"Does the stored `{col}` agree with `{base}`?", parts))

    partial = ", ".join(d.label(x) for x in d.partial_periods)
    story = Step(
        "date_range",
        (f"`{d.date[0]}.{d.date[1]}` runs from {p.col(*d.date).minimum:%Y-%m-%d} to {p.col(*d.date).maximum:%Y-%m-%d}"
         + (f"; {partial} is only partly covered" if partial else "") + ".") if d.date else
        f"{len(dates)} date column(s) found.",
        "An extract that stops mid-period makes the last period look like a collapse, and impossible dates (shipped before ordered, years in the future) "
        "point at loading errors. Both must be caught before any trend is reported.",
        "We check the earliest and latest date of every date column and count rows per period, so that partial periods are labelled instead of misread.",
        "MIN and MAX per date column; then rows per period with the first and last date seen in each, so that the check below can tell how much "
        "of each period the data covers" + ("; then shipping dates that come before the order or more than a year after it" if d.lead else "") + ".",
    )
    check = fill('''
        import datetime as _dt
        ranges = outputs["date_ranges"]
        for row in ranges.itertuples():
            metrics.setdefault("date_ranges", {})[f"{row.table_name}.{row.column_name}"] = (str(row.earliest)[:10], str(row.latest)[:10])
        if "periods" not in outputs:
            note("Date columns: " + "; ".join(f"`{r.table_name}.{r.column_name}` {str(r.earliest)[:10]} to {str(r.latest)[:10]}"
                 + (f" ({int(r.null_rows):,} empty)" if r.null_rows else "") for r in ranges.itertuples()) + ".")
        if "periods" in outputs:
            per = outputs["periods"]
            unit = <<unit>>
            keys = [(int(row[0]), int(row[1]) if unit != "year" else 0) for row in per.itertuples(index=False)]
            bounds = [period_bounds(unit, y, n) for y, n in keys]
            per = per.assign(period_start=[b[0] for b in bounds], period_end=[b[1] for b in bounds])
            first_seen = pd.to_datetime(per["first_date"]).dt.date
            last_seen = pd.to_datetime(per["last_date"]).dt.date
            covered = [((min(l, e) - max(f, s)).days + 1) / ((e - s).days + 1) if i in (0, len(per) - 1) else 1.0
                       for i, (f, l, s, e) in enumerate(zip(first_seen, last_seen, per["period_start"], per["period_end"]))]
            per = per.assign(coverage=covered)
            partial = [period_label(unit, y, n) for (y, n), cov in zip(keys, covered) if cov < <<cov>>]
            metrics["partial_periods"] = partial
            metrics["periods_seen"] = len(per)
            index = [period_index(unit, y, n) for y, n in keys]
            missing = sorted(set(range(min(index), max(index) + 1)) - set(index)) if index else []
            missing = [label_from_index(unit, i) for i in missing]
            metrics["missing_periods"] = missing
            if missing:
                note(f"⚠️ **No rows at all in {', '.join(missing[:6])}**. A gap is either a real stop or a loading problem; "
                     f"period-over-period changes are only computed between neighbouring {unit}s.")
            span = f"{str(ranges['earliest'].min())[:10]} to {str(ranges['latest'].max())[:10]}"
            note(f"{len(per)} {unit}s of data. " + (f"**Partial:** {', '.join(partial)} (less than {<<cov>>:.0%} of its days covered), so "
                 "it is labelled and left out of period-over-period verdicts." if partial else "The first and last periods are fully covered."))
        if "lead_sanity" in outputs:
            row = outputs["lead_sanity"].iloc[0]
            metrics["negative_lead"] = int(row.iloc[2] or 0)
            end_col, start_col = <<end>>, <<start>>
            note(f"{int(row.iloc[2] or 0):,} row(s) where `{end_col}` is before `{start_col}`, {int(row.iloc[3] or 0):,} more than a year later; "
                 f"lead times run from {row.min_days} to {row.max_days} days.")
        for label in [k for k in outputs if k.startswith("part_")]:
            row = outputs[label].iloc[0]
            metrics["date_part_mismatches"] = metrics.get("date_part_mismatches", 0) + int(row.mismatched or 0)
            note(f"{label[5:]}: {int(row.mismatched or 0):,} row(s) disagree with the date they were derived from.")
    ''', unit=repr(d.period or "quarter"), cov=0.9, end=repr(d.lead[2] if d.lead else ""), start=repr(d.lead[1] if d.lead else ""))
    return Cell("date_range", "Date ranges and partial periods", SECTIONS[2], story, tuple(statements), check)


def _orphans_cell(b: Builder) -> Cell:
    d = b.d
    rels = [r for r in d.schema.accepted if r.kind != "self"] + [r for r in d.schema.accepted if r.kind == "self"]

    def cond(D: Dialect, r: Relationship) -> str:
        if r.type_mismatch:
            return f"{D.as_text('p.' + D.ident(r.parent_col))} = {D.as_text('c.' + D.ident(r.child_col))}"
        return f"p.{D.ident(r.parent_col)} = c.{D.ident(r.child_col)}"

    def summary(D: Dialect) -> str:
        parts = []
        for r in rels:
            label = D.literal(f"{r.child_table}.{r.child_col} -> {r.parent_table}")
            parts.append(fill('''
                SELECT <<label>> AS relationship,
                       'child rows with no parent' AS problem,
                       COUNT(*) AS row_count
                FROM   <<child>> c
                LEFT   JOIN <<parent>> p ON <<cond>>
                WHERE  p.<<pk>> IS NULL
                  AND  c.<<fk>> IS NOT NULL''', label=label, child=D.ident(r.child_table), parent=D.ident(r.parent_table),
                                               cond=cond(D, r), pk=D.ident(r.parent_col), fk=D.ident(r.child_col)))
            parts.append(fill('''
                SELECT <<label>>,
                       'parents with no child rows (information)',
                       COUNT(*)
                FROM   <<parent>> p
                WHERE  NOT EXISTS (SELECT 1 FROM <<child>> c WHERE <<cond>>)''', label=label, child=D.ident(r.child_table),
                                                                              parent=D.ident(r.parent_table), cond=cond(D, r)))
        return union_all(parts)

    statements = [stmt("orphans", "Orphans in both directions, for every relationship", summary)]
    worst = max(rels, key=lambda r: r.orphan_rows, default=None)
    if worst is not None and worst.orphan_rows:
        def missing(D: Dialect, r=worst) -> str:
            return fill('''
                SELECT DISTINCT c.<<fk>> AS missing_<<name>>
                FROM   <<child>> c
                LEFT   JOIN <<parent>> p ON <<cond>>
                WHERE  p.<<pk>> IS NULL
                  AND  c.<<fk>> IS NOT NULL
                ORDER  BY 1
                LIMIT  <<n>>''', fk=D.ident(r.child_col), name=r.child_col, child=D.ident(r.child_table), parent=D.ident(r.parent_table),
                                 cond=cond(D, r), pk=D.ident(r.parent_col), n=DISPLAY_LIMIT)
        statements.append(stmt("orphan_examples", f"Values of `{worst.child_table}.{worst.child_col}` with no `{worst.parent_table}` row", missing))
    lonely = next((r for r in rels if r.childless_parents and r.kind != "self" and r.childless_parents < r.child_distinct + r.childless_parents), None)
    if lonely is not None:
        label = None
        from .decision import label_column
        label = label_column(b.p[lonely.parent_table])

        def childless(D: Dialect, r=lonely, label=label) -> str:
            cols = f"p.{D.ident(r.parent_col)}" + (f", p.{D.ident(label)}" if label and label != r.parent_col else "")
            return fill('''
                SELECT <<cols>>
                FROM   <<parent>> p
                WHERE  NOT EXISTS (SELECT 1 FROM <<child>> c WHERE <<cond>>)
                ORDER  BY 1
                LIMIT  <<n>>''', cols=cols, parent=D.ident(r.parent_table), child=D.ident(r.child_table), cond=cond(D, r), n=DISPLAY_LIMIT)
        statements.append(stmt("childless_examples", f"`{lonely.parent_table}` rows that no `{lonely.child_table}` row points at", childless))
    story = Step(
        "orphans",
        f"{len(rels)} trusted relationship(s); profiling counted {sum(r.orphan_rows for r in rels):,} orphan child row(s) in total.",
        "An orphan (a child whose parent is missing) vanishes from every INNER JOIN, so revenue quietly drops. Parents without children are not errors, "
        "but they matter for rates: a customer who never ordered still counts as a customer.",
        "We count orphans in both directions with an anti-join, so that every join later can be trusted or its losses explained.",
        "LEFT JOIN child to parent and keep the rows where the parent key came back NULL: those children have no parent. The second query "
        "uses NOT EXISTS to find parents that no child points at. Both are anti-joins; NOT EXISTS is safe when the child column has NULLs.",
    )
    check = fill('''
        orphans = outputs["orphans"]
        bad = orphans[(orphans["problem"] == "child rows with no parent") & (orphans["row_count"] > 0)]
        lonely = orphans[(orphans["problem"] != "child rows with no parent") & (orphans["row_count"] > 0)]
        metrics["orphans"] = {r.relationship: int(r.row_count) for r in bad.itertuples()}
        metrics["childless"] = {r.relationship: int(r.row_count) for r in lonely.itertuples()}
        if bad.empty:
            note("No orphans: every foreign key value points at an existing row, so no row will vanish from an inner join.")
        for r in bad.itertuples():
            note(f"`{r.relationship}`: **{int(r.row_count):,} orphan row(s)**. They are kept (LEFT JOIN, labelled 'Unknown') so that no revenue is lost.")
        for r in lonely.itertuples():
            note(f"`{r.relationship}`: {int(r.row_count):,} parent row(s) have no child rows.")
        if "childless_examples" in outputs:
            ex = outputs["childless_examples"]
            note("For example: " + ", ".join(" / ".join(str(v) for v in row) for row in ex.head(5).itertuples(index=False)) + ".")
    ''')
    return Cell("orphans", "Find orphans", SECTIONS[2], story, tuple(statements), check)


# ═════════════════════════════════════════════════════════════════════════
# 3 · Build with CTEs: the analysis views
# ═════════════════════════════════════════════════════════════════════════
def _lead_is_timestamp(b: Builder) -> bool:
    d = b.d
    return bool(d.lead) and any(b.p.col(d.lead[0], c).logical_type == "timestamp" for c in d.lead[1:])


def _order_facts_sql(b: Builder, D: Dialect) -> str:
    """One row per order. Line-level facts are rolled up (CTEs) BEFORE order-level tables are joined."""
    d, p = b.d, b.p
    fact, fa = d.fact, b.alias(d.fact)
    ha = b.alias(d.header) if d.header else None
    order_dims = [dim for dim in d.dimensions if dim.level == "order"]
    item = d.item if d.item and d.item[2].level == "order" else None
    used = [fact] + ([d.header] if d.header else []) + [r.parent_table for dim in order_dims for r in dim.path]
    if d.revenue.price_rel:
        used.append(d.revenue.price_rel.parent_table)
    ctes = b.source_ctes(D, used)
    ok = D.ident(b.order_key)
    # order-level columns: (table, column). In a line-level fact without an order table they are taken per order with MIN().
    order_cols = [x for x in ((d.customer and d.customer), d.date, (d.lead and (d.lead[0], d.lead[2])),
                              (d.required and (d.lead[0], d.required)), (d.rating and (d.rating.table, d.rating.name))) if x]
    in_fact = list(dict.fromkeys(c for t, c in order_cols if t == fact))

    if d.line_level:
        price_alias, price_join = None, ""
        if d.revenue.price_rel:
            r = d.revenue.price_rel
            price_alias = b.alias(r.parent_table)
            price_join = f"\n    LEFT   JOIN {b.src(D, r.parent_table)} {price_alias} ON {D.col(price_alias, r.parent_col)} = {D.col(fa, r.child_col)}"
        line_cols = [D.col(fa, b.order_key), f"{b.line_revenue(D, fa, price_alias)} AS revenue"]
        if d.revenue.quantity:
            line_cols.append(f"{D.col(fa, d.revenue.quantity)} AS units")
        line_cols += [D.col(fa, c) for c in in_fact]
        ctes.append(fill('''
            line_revenue AS (                -- 1) one row per <<fact>> line, with its revenue
                SELECT <<cols>>
                FROM   <<src>> <<fa>><<pj>>
            )''', fact=fact, cols=",\n           ".join(line_cols), src=b.src(D, fact), fa=fa, pj=price_join))
        totals = [ok, "SUM(revenue) AS revenue"] + (["SUM(units) AS units"] if d.revenue.quantity else []) + ["COUNT(*) AS line_count"]
        totals += [f"MIN({D.ident(c)}) AS {D.ident(c)}" for c in in_fact]
        note = ("\n    -- MIN(): order-level columns repeat on every line of an order (checked when reconciling)" if in_fact else "")
        ctes.append(fill('''
            order_totals AS (                -- 2) one row per order: roll the lines up BEFORE joining order-level tables<<note>>
                SELECT <<cols>>
                FROM   line_revenue
                GROUP  BY <<ok>>
            )''', cols=",\n           ".join(totals), ok=ok, note=note))
        where = {fact: "t"}
        from_lines = ["FROM   order_totals t"]
        if d.header:
            r = d.header_rel
            from_lines.append(f"LEFT   JOIN {b.src(D, d.header)} {ha} ON {D.col(ha, r.parent_col)} = t.{ok}")
            where[d.header] = ha
    else:
        joined = any(dim.path for dim in order_dims) or bool(item and d.customer_rel) or bool(d.revenue.price_rel)
        where = {fact: fa if joined else None}
        from_lines = [f"FROM   {b.src(D, fact)}" + (f" {fa}" if joined else "")]
    paths = [tuple(r for r in dim.path if r is not d.header_rel) for dim in order_dims]
    if item and d.customer_rel:
        paths.append((d.customer_rel,))
    if d.revenue.price_rel and not d.line_level:
        paths.append((d.revenue.price_rel,))
    joins, where = b.joins_for(D, paths, where)

    def oc(table: str, col: str) -> str:
        return D.col(where[table], col)

    select = [f"t.{ok}" if d.line_level else oc(fact, b.order_key)] if d.order_key else []
    if d.customer:
        select.append(oc(*d.customer))
    if d.date:
        dq = oc(*d.date)
        select += [dq] + b.period_select(D, dq)
    if d.line_level:
        select += ["t.revenue"] + (["t.units"] if d.revenue.quantity else []) + ["t.line_count"]
    else:
        if b.money:
            price_alias = where.get(d.revenue.price_rel.parent_table) if d.revenue.price_rel else None
            select.append(f"{b.line_revenue(D, where[fact], price_alias)} AS revenue")
        if d.revenue.quantity:
            select.append(f"{oc(fact, d.revenue.quantity)} AS units")
    if d.header:
        select += [D.col(ha, c.name) for c in p[d.header].by_role("measure") if c.kind in ("amount", "charge")]
    if d.lead:
        table, start, end = d.lead
        select.append(f"{D.days_between(oc(table, start), oc(table, end), _lead_is_timestamp(b))} AS {d.lead_name}")
        if d.required:
            select.append(fill('''
                CASE WHEN <<r>> IS NULL THEN NULL
                            WHEN <<e>> > <<r>> THEN 1
                            WHEN <<e>> IS NOT NULL THEN 0
                            WHEN <<r>> < (SELECT MAX(<<dc>>) FROM <<dt>>) THEN 1    -- not shipped, already past its date
                       END AS shipped_late''', e=oc(table, end), r=oc(table, d.required), dc=D.ident(d.date[1]), dt=D.ident(d.date[0])))
    if d.rating:
        rating = b.rating_expr(D, where[d.rating.table])
        select.append(f"{rating} AS rating_score")

    for dim in order_dims:
        select.append(b.dim_expr(D, dim, where.get(dim.table, where[fact])))
    if item:
        table, label, _ = item
        if table in where and label != (d.customer[1] if d.customer else None):
            col = D.col(where[table], label)
            if p.col(table, label).logical_type != "text":
                col = D.as_text(col)
            select.append(f"COALESCE({col}, 'Unknown') AS {D.ident(label)}")
    head = f"-- Grain: one row per order (`{b.order_key}`)." if d.order_key else f"-- Grain: one row per `{fact}` row."
    if d.line_level:
        head += " Lines are rolled up BEFORE joining order-level tables, so nothing is repeated."
    with_clause = ("WITH " + ",\n".join(c.strip() for c in ctes) + "\n") if ctes else ""
    body = "SELECT " + ",\n       ".join(select) + "\n" + "\n".join(from_lines + joins)
    return f"{head}\nCREATE OR REPLACE VIEW order_facts AS\n{with_clause}{body}"


def _line_facts_sql(b: Builder, D: Dialect) -> str:
    d, p = b.d, b.p
    fact, fa = d.fact, b.alias(d.fact)
    line_dims = [dim for dim in d.dimensions if dim.level == "line"]
    item = d.item if d.item and d.item[2].level == "line" else None
    tables = [fact] + ([d.header] if d.header else []) + [r.parent_table for dim in line_dims for r in dim.path]
    if d.revenue.price_rel:
        tables.append(d.revenue.price_rel.parent_table)
    ctes = b.source_ctes(D, tables)
    start = {fact: fa}
    paths = []
    if d.header:
        paths.append((d.header_rel,))
    if d.revenue.price_rel:
        paths.append((d.revenue.price_rel,))
    paths += [dim.path for dim in line_dims]
    if item:
        paths.append(item[2].path[:-1] if len(item[2].path) >= 2 else item[2].path)
    joins, where = b.joins_for(D, paths, start)
    order_alias = where.get(d.header, fa) if d.header else fa
    price_alias = where.get(d.revenue.price_rel.parent_table) if d.revenue.price_rel else None
    select = [D.col(fa, b.order_key)]
    select += [D.col(fa, k) for k in p[fact].primary_key if k != b.order_key]
    if d.customer:
        select.append(D.col(order_alias if d.customer[0] == d.header else fa, d.customer[1]))
    if d.date:
        dq = D.col(order_alias if d.date[0] == d.header else fa, d.date[1])
        select += [dq] + b.period_select(D, dq)
    select.append(f"{b.line_revenue(D, fa, price_alias)} AS revenue")
    if d.revenue.quantity:
        select.append(f"{D.col(fa, d.revenue.quantity)} AS units")
    if item:
        table, label, _ = item
        col = D.col(where.get(table, fa), label)
        if p.col(table, label).logical_type != "text":
            col = D.as_text(col)
        select.append(f"COALESCE({col}, 'Unknown') AS {D.ident(label)}")
    for dim in line_dims:
        select.append(b.dim_expr(D, dim, where.get(dim.table, fa)))
    with_clause = ("WITH " + ",\n".join(c.strip() for c in ctes) + "\n") if ctes else ""
    body = "SELECT " + ",\n       ".join(select) + "\n" + "\n".join([f"FROM   {b.src(D, fact)} {fa}"] + joins)
    return (f"-- Grain: one row per {fact} line. Use it for product-level questions; use order_facts for order-level KPIs.\n"
            f"CREATE OR REPLACE VIEW line_facts AS\n{with_clause}{body}")


def _base_cell(b: Builder) -> Cell:
    d = b.d
    statements = [stmt("order_facts", "View `order_facts`: one row per order", lambda D: _order_facts_sql(b, D))]
    if d.line_level:
        statements.append(stmt("line_facts", "View `line_facts`: one row per line", lambda D: _line_facts_sql(b, D)))

    def preview(D: Dialect) -> str:
        cols = ([D.ident(b.date_col)] if b.date_col else []) + ([D.ident(d.order_key)] if d.order_key else [])
        return f"SELECT *\nFROM   order_facts\nORDER  BY {', '.join(cols) or '1'}\nLIMIT  5"
    statements.append(stmt("preview", "The first five rows of `order_facts`", preview))
    joined = sorted({r.parent_table for dim in d.dimensions for r in dim.path} | ({d.header} if d.header else set()))
    rules = (" Before anything else, a `_kept` CTE leaves out the rows the business rules exclude ("
             + "; ".join(f.describe() for f in d.row_filters) + "), with `IS NULL OR … NOT IN (…)`: a bare NOT IN would also drop "
             "every row whose value is NULL." if d.row_filters else "")
    story = Step(
        "base",
        f"The revenue formula ({b.revenue_formula()}), the order grain and the tables to join ({_names(joined) or 'none'}) are known.",
        "Writing every KPI from raw tables repeats the same joins and invites the same mistakes each time. One tested view per grain fixes them once.",
        "We build " + ("two views, `order_facts` (one row per order) and `line_facts` (one row per line)," if d.line_level else "the view `order_facts`")
        + " from small CTEs that each do one job, so that every later query starts from checked, business-named columns.",
        ("`line_revenue` computes revenue per line; `order_totals` rolls lines up to one row per order; the final SELECT joins the order table "
         "and dimension tables with LEFT JOIN (so nothing is dropped; a missing match becomes 'Unknown'), and adds the period columns"
         + (f", {d.lead_name}" if d.lead else "") + (" and shipped_late" if d.required else "") + ". `line_facts` keeps one row per line for product questions."
         if d.line_level else
         "One SELECT over the fact table adds revenue, the period columns" + (f", {d.lead_name}" if d.lead else "")
         + " and the dimensions (LEFT JOIN, so nothing is dropped).") + rules,
    )
    check = fill('''
        preview = outputs["preview"]
        note(f"`order_facts` has these columns: " + ", ".join(f"`{c}`" for c in preview.columns) + ".")
    ''')
    return Cell("base", "Build the analysis views", SECTIONS[3], story, tuple(statements), check)


def _reconcile_cell(b: Builder) -> Cell:
    d = b.d
    fact, fa = d.fact, b.alias(d.fact)

    def totals(D: Dialect) -> str:
        okq = D.ident(b.order_key)
        price_join = ""
        price_alias = None
        if d.revenue.price_rel:
            r = d.revenue.price_rel
            price_alias = b.alias(r.parent_table)
            price_join = f"\nLEFT   JOIN {D.ident(r.parent_table)} {price_alias} ON {D.col(price_alias, r.parent_col)} = {D.col(fa, r.child_col)}"
        src_rows = "COUNT(*)"
        src_orders = f"COUNT(DISTINCT {D.col(fa, b.order_key)})" if d.order_key else "COUNT(*)"
        measure = (lambda expr: D.round(expr, 2)) if b.money else (lambda expr: "COUNT(*)")
        name = "revenue" if b.money else "row_count"
        fact_part = fill('''
            SELECT <<label>> AS computed_from,
                   <<rows>> AS rows_read,
                   <<orders>> AS orders,
                   <<rev>> AS <<name>>
            FROM   <<t>> <<fa>><<pj>>''', label=D.literal(f"{fact} (raw table" + (", before de-duplication)" if fact in d.dedupe else ")")),
                                      rows=src_rows, orders=src_orders, rev=measure(f"SUM({b.line_revenue(D, fa, price_alias)})"), name=name,
                                      t=D.ident(fact), fa=fa, pj=price_join)
        lines = "SUM(line_count)" if d.line_level else "COUNT(*)"
        parts, ctes = [fact_part], []
        if d.row_filters:
            # the rows the business rules leave out, read like the views read them (de-duplicated first)
            used = [fact] + ([d.header] if b.header_filtered else []) + ([d.revenue.price_rel.parent_table] if d.revenue.price_rel else [])
            ctes = b.dedupe_ctes(D, list(dict.fromkeys(used)))
            out = [f"({c})" for c in (b.left_out(D, fa, fact),) if c]
            if b.header_filtered:
                out.append(b.header_left_out(D, fa, 15))
            pj = (f"\nLEFT   JOIN {b.deduped(D, d.revenue.price_rel.parent_table)} {price_alias} "
                  f"ON {D.col(price_alias, d.revenue.price_rel.parent_col)} = {D.col(fa, d.revenue.price_rel.child_col)}"
                  if d.revenue.price_rel else "")
            parts.append(fill('''
                SELECT 'left out by the business rules',
                       <<rows>>,
                       <<orders>>,
                       <<rev>>
                FROM   <<t>> <<fa>><<pj>>
                WHERE  <<out>>''', rows=src_rows, orders=src_orders, rev=measure(f"SUM({b.line_revenue(D, fa, price_alias)})"),
                                     t=b.deduped(D, fact), fa=fa, pj=pj, out="\n   OR  ".join(out)))
        parts.append(fill('''
            SELECT 'order_facts (one row per order)',
                   <<lines>>,
                   COUNT(*),
                   <<rev>>
            FROM   order_facts''', lines=lines, rev=measure("SUM(revenue)") if b.money else lines))
        if d.line_level:
            parts.append(fill('''
                SELECT 'line_facts (one row per line)',
                       COUNT(*),
                       COUNT(DISTINCT <<ok>>),
                       <<rev>>
                FROM   line_facts''', ok=okq, rev=measure("SUM(revenue)")))
        with_clause = ("WITH " + ",\n".join(c.strip() for c in ctes) + "\n") if ctes else ""
        return with_clause + union_all(parts)

    statements = [stmt("totals", "The same totals computed from the raw table and from each view", totals)]
    rev = d.revenue
    if rev.kind == "price_qty" and rev.discount and not rev.price_rel:
        def gross(D: Dialect) -> str:
            g = f"{D.col(fa, rev.price)} * {D.col(fa, rev.quantity)}"
            ctes = b.source_ctes(D, [fact])        # the rows the views use: de-duplicated, business rules applied
            with_clause = ("WITH " + ",\n".join(c.strip() for c in ctes) + "\n") if ctes else ""
            return with_clause + fill('''
                SELECT <<g>> AS gross_revenue,
                       <<n>> AS net_revenue,
                       <<x>> AS discount_given
                FROM   <<t>> <<fa>>''', g=D.round(f"SUM({g})", 2), n=D.round(f"SUM({b.line_revenue(D, fa)})", 2),
                                        x=D.round(f"SUM({g} - {b.line_revenue(D, fa)})", 2), t=b.src(D, fact), fa=fa)
        statements.append(stmt("gross_net", "Gross vs net revenue (the formula is net of discount)", gross))
    if rev.kind == "price_qty" and not rev.price_rel:
        list_price = next(((r, c) for r in d.schema.parents(fact) for c in b.p[r.parent_table].by_role("measure")
                           if c.kind == "price" and "price" in c.name), None)
        if list_price:
            r, c = list_price

            def differs(D: Dialect, r=r, c=c) -> str:
                pa = b.alias(r.parent_table)
                return fill('''
                    SELECT COUNT(*) AS line_rows,
                           SUM(CASE WHEN <<fp>> <> <<pp>> THEN 1 ELSE 0 END) AS price_differs_from_list,
                           <<pct>> AS differs_pct
                    FROM   <<t>> <<fa>>
                    JOIN   <<pt>> <<pa>> ON <<pa>>.<<pk>> = <<fa>>.<<fk>>''', fp=D.col(fa, rev.price), pp=D.col(pa, c.name),
                            pct=D.pct(f"SUM(CASE WHEN {D.col(fa, rev.price)} <> {D.col(pa, c.name)} THEN 1 ELSE 0 END)", "COUNT(*)"),
                            t=D.ident(fact), fa=fa, pt=D.ident(r.parent_table), pa=pa, pk=D.ident(r.parent_col), fk=D.ident(r.child_col))
            statements.append(stmt("list_price", f"Is `{fact}.{rev.price}` the same as the list price `{r.parent_table}.{c.name}`?", differs))
    if d.line_level and not d.header and d.date:
        def conflicts(D: Dialect) -> str:
            return fill('''
                SELECT COUNT(*) AS orders_with_more_than_one_date
                FROM   (SELECT <<ok>>
                        FROM   <<t>>
                        GROUP  BY <<ok>>
                        HAVING COUNT(DISTINCT <<dt>>) > 1) x''', ok=D.ident(b.order_key), t=D.ident(fact), dt=D.ident(d.date[1]))
        statements.append(stmt("order_conflicts", "Do all lines of an order share one date?", conflicts))
    src_code = _pandas_revenue(b)
    story = Step(
        "reconcile",
        "The views are built; their totals have not been checked yet.",
        "A join that drops orphans, a duplicate that slips through or a filter in the wrong place all change the total. "
        "Reconciling against the source is the only proof that the views neither lose nor invent revenue.",
        "We compute rows, orders and revenue from the raw table, from each view, and independently in pandas from the uploaded file, "
        "so that every number shared later is traceable to the source.",
        "SELECTs stacked with UNION ALL compute the same totals from the raw table and from each view; they must agree to the cent. "
        + ("One of them adds up what the business rules leave out, so that raw = left out + kept is checked, not assumed. "
           if d.row_filters else "")
        + ("A second query splits revenue into gross and discount. " if rev.discount else "")
        + "The check below also recomputes revenue in pandas straight from the uploaded rows, outside SQL.",
    )
    check = fill('''
        totals = outputs["totals"]
        <<src>>
        measure = totals.columns[-1]                         # revenue, or row_count when there is no money column
        sql_values = [float(v) for v in totals[measure]]
        deduped = <<deduped>>                                # the raw table still holds the duplicates the views remove
        filtered = <<filtered>>                              # a row adds up what the business rules leave out
        raw, left_out = sql_values[0], (sql_values[1] if filtered else 0.0)
        views = sql_values[2:] if filtered else sql_values[1:]
        compared = views + [source_revenue] + ([] if deduped else [raw - left_out])   # raw = left out + kept, unless duplicates go too
        tolerance = max(0.01, 1e-9 * max(abs(v) for v in compared + [raw]))  # floating-point sums on big data
        agree = max(compared) - min(compared) <= tolerance and (not filtered or abs(left_out - source_left_out) <= tolerance)
        metrics["revenue_total"] = views[0]
        metrics["revenue_source"] = source_revenue
        metrics["reconciled"] = bool(agree)
        metrics["main_measure"] = "revenue" if measure == "revenue" else "orders"
        what = "revenue" if measure == "revenue" else "the row count"
        fmt = "{:,.2f}" if measure == "revenue" else "{:,.0f}"
        if filtered:
            rows_out = int(totals["rows_read"].iloc[1])
            metrics["left_out"] = {"rows": rows_out, "value": left_out, "share_pct": 100 * left_out / raw if raw else 0.0}
        if agree:
            note(f"**Reconciled:** {what} is **" + fmt.format(views[0]) + "** from "
                 + ("every view" if deduped else "the raw table" + (" less what the business rules leave out" if filtered else "") + ", from every view")
                 + ", and from the uploaded file (recomputed in pandas). Nothing was lost or double-counted.")
        else:
            note("⚠️ The totals differ: " + "; ".join(f"{name}: " + fmt.format(value) for name, value in zip(totals["computed_from"], sql_values))
                 + "; uploaded file: " + fmt.format(source_revenue) + ". Look for orphans or duplicated keys before trusting any KPI.")
        if filtered:
            rules = <<rules>>
            note(f"The business rules leave out {rows_out:,} row(s) holding " + fmt.format(left_out) + f" of {what} "
                 f"({metrics['left_out']['share_pct']:.1f}% of the raw table): {rules}.")
        if deduped:
            note("The raw table still contains the duplicated keys: " + fmt.format(raw - left_out - views[0])
                 + f" of {what} disappears when they are removed, which is the point of de-duplicating.")
        if "gross_net" in outputs:
            row = outputs["gross_net"].iloc[0]
            metrics["revenue_gross"] = float(row.gross_revenue)
            note(f"Gross revenue is {float(row.gross_revenue):,.2f}; discounts take {float(row.discount_given):,.2f} "
                 f"({float(row.discount_given) / float(row.gross_revenue):.1%}), leaving net {float(row.net_revenue):,.2f}.")
        if "list_price" in outputs:
            row = outputs["list_price"].iloc[0]
            metrics["price_differs_pct"] = float(row.differs_pct)
            note(f"In {float(row.differs_pct):.1f}% of lines the price paid differs from today's list price, which is why revenue uses the price on the line.")
        if "order_conflicts" in outputs:
            n = int(outputs["order_conflicts"].iloc[0, 0])
            note(f"{n:,} order(s) have lines with different dates" + (": the view takes the earliest." if n else "."))
    ''', src=src_code, deduped=repr(d.fact in d.dedupe), filtered=repr(bool(d.row_filters)),
        rules=repr("; ".join(f.describe().replace("`", "") for f in d.row_filters)))
    return Cell("reconcile", "Reconcile the totals", SECTIONS[2], story, tuple(statements), check)


def _pandas_revenue(b: Builder) -> str:
    """Python that recomputes revenue from the uploaded DataFrame (``source``), outside SQL."""
    d, rev = b.d, b.d.revenue
    lines = [f"src = source[{d.fact!r}]"]
    if d.fact in d.dedupe:
        keys, order = d.dedupe[d.fact]
        sort = f".sort_values({order!r}, na_position='first')" if order else ""
        lines.append(f"src = src{sort}.drop_duplicates(subset={list(keys)!r}, keep='last')  # the views keep one row per key")
    expr = None
    if rev.kind == "price_qty":
        if rev.price_rel:
            r = rev.price_rel
            lines.append(f"prices = source[{r.parent_table!r}].drop_duplicates({r.parent_col!r}).set_index({r.parent_col!r})[{rev.price!r}]")
            price = f"src[{r.child_col!r}].map(prices)"
        else:
            price = f"src[{rev.price!r}]"
        expr = f"{price} * src[{rev.quantity!r}]"
        if rev.discount:
            scale = " / 100" if rev.discount_scale == 100 else ""
            expr += f" * (1 - src[{rev.discount!r}].fillna(0){scale})"
    elif rev.kind == "amount":
        expr = f"src[{rev.amount!r}]"
    if not d.row_filters:
        lines.append(f"source_revenue = float(({expr}).sum())" if expr else "source_revenue = float(len(src))")
        return "\n".join(lines)
    lines.append(f"revenue = {expr}" if expr else "revenue = pd.Series(1.0, index=src.index)")
    own = [f"src[{f.column!r}].isin({list(f.values)!r})" for f in b.filters_on(d.fact)]
    lines.append("left_out = " + (" | ".join(own) if own else "pd.Series(False, index=src.index)") + "   # the business rules")
    if b.header_filtered:
        r, h = d.header_rel, d.header
        lines.append(f"head = source[{h!r}]")
        if h in d.dedupe:
            keys, order = d.dedupe[h]
            sort = f".sort_values({order!r}, na_position='first')" if order else ""
            lines.append(f"head = head{sort}.drop_duplicates(subset={list(keys)!r}, keep='last')")
        rule = " | ".join(f"head[{f.column!r}].isin({list(f.values)!r})" for f in b.filters_on(h))
        cast = ".astype(str)" if r.type_mismatch else ""
        lines.append(f"left_out = left_out | src[{r.child_col!r}]{cast}.isin(head.loc[{rule}, {r.parent_col!r}]{cast})   # lines of left-out orders")
    lines.append("left_out = left_out.astype(bool)")
    lines += ["source_revenue = float(revenue[~left_out].sum())", "source_left_out = float(revenue[left_out].sum())"]
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════
# 4 · KPIs
# ═════════════════════════════════════════════════════════════════════════
def _kpis_cell(b: Builder) -> Cell:
    d = b.d

    def sql(D: Dialect) -> str:
        summary = ["SUM(revenue) AS revenue"] if b.money else []
        summary.append("COUNT(*) AS orders")
        if d.customer:
            summary.append(f"COUNT(DISTINCT {D.ident(d.customer[1])}) AS customers")
        if d.revenue.quantity:
            summary.append("SUM(units) AS units")
        if d.lead:
            summary += [f"AVG({d.lead_name}) AS avg_{d.lead_name}", f"COUNT({d.lead_name}) AS orders_with_{d.lead_name}"]
        if d.required:
            summary += ["SUM(shipped_late) AS late_orders", "COUNT(shipped_late) AS checked_orders",
                        f"SUM(CASE WHEN {d.lead_name} IS NULL AND shipped_late = 1 THEN 1 ELSE 0 END) AS open_overdue"]
        if d.rating:
            summary.append("AVG(rating_score) AS avg_rating")
        ctes = [fill('''
            order_summary AS (
                SELECT <<cols>>
                FROM   order_facts
            )''', cols=",\n           ".join(summary))]
        joins = ["order_summary"]
        if d.customer:
            ctes.append(fill('''
                repeat_customers AS (
                    SELECT COUNT(*) AS repeat_customers
                    FROM   (SELECT <<c>>
                            FROM   order_facts
                            WHERE  <<c>> IS NOT NULL
                            GROUP  BY <<c>>
                            HAVING COUNT(*) >= 2) r
                )''', c=D.ident(d.customer[1])))
            joins.append("repeat_customers")
        if d.line_level and b.money:
            ctes.append(fill('''
                line_summary AS (
                    SELECT AVG(revenue) AS avg_line_value   -- NOT the average order value: shown for contrast
                    FROM   line_facts
                )'''))
            joins.append("line_summary")
        out = []
        if b.money:
            out += [f"{D.round('revenue', 2)} AS revenue"]
        out.append("orders")
        if b.money:
            out.append(f"{D.ratio('revenue', 'orders', 2)} AS avg_order_value")
        if d.line_level and b.money:
            out.append(f"{D.round('avg_line_value', 2)} AS avg_line_value")
        if d.customer:
            out += ["customers", f"{D.ratio('orders', 'customers', 2)} AS orders_per_customer",
                    f"{D.pct('repeat_customers', 'customers')} AS repeat_customer_pct"]
        if d.revenue.quantity:
            out.append(f"{D.ratio('units', 'orders', 2)} AS units_per_order")
        if d.lead:
            out += [f"{D.round(f'avg_{d.lead_name}', 2)} AS avg_{d.lead_name}", f"orders - orders_with_{d.lead_name} AS orders_without_{d.lead_name}"]
        if d.required:
            out += ["late_orders", "checked_orders", "open_overdue", f"{D.pct('late_orders', 'checked_orders')} AS late_pct"]
        if d.rating:
            out.append(f"{D.round('avg_rating', 2)} AS avg_rating")
        return ("WITH " + ",\n".join(c.strip() for c in ctes) + "\nSELECT " + ",\n       ".join(out)
                + "\nFROM   " + "\nCROSS  JOIN ".join(joins))

    story = Step(
        "kpis",
        "`order_facts` has one row per order and reconciles to the source.",
        "Headline KPIs answer 'how big is the business?' and become the baseline for every trend. Each must be computed at the right grain: "
        "average order value is revenue ÷ distinct orders, not the average of order lines.",
        "We compute the headline KPIs in one query, so that every later breakdown can be compared with the overall figure.",
        "One CTE summarises order_facts (SUM, COUNT, AVG); another counts customers with two or more orders (GROUP BY … HAVING COUNT(*) >= 2). "
        "The final SELECT divides with 1.0 * … / NULLIF(…, 0) and 100.0 * …, so that ratios stay decimal and never divide by zero."
        + (" The average *line* value is shown next to the AOV to make the grain difference visible." if d.line_level else ""),
    )
    check = fill('''
        k = result.iloc[0].to_dict()
        metrics["kpis"] = {key: (float(v) if v is not None and not pd.isna(v) else None) for key, v in k.items()}
        if "revenue" in k:
            note(f"Revenue **{float(k['revenue']):,.2f}** from **{int(k['orders']):,}** orders: average order value **{float(k['avg_order_value']):,.2f}**.")
        else:
            note(f"**{int(k['orders']):,}** orders in total.")
        if "avg_line_value" in k:
            note(f"The average *line* is only {float(k['avg_line_value']):,.2f}. Averaging lines would understate the order value "
                 f"{float(k['avg_order_value']) / float(k['avg_line_value']):.1f}×: the grain matters.")
        if "customers" in k:
            note(f"{int(k['customers']):,} customers place {float(k['orders_per_customer']):.2f} orders each; "
                 f"{float(k['repeat_customer_pct']):.1f}% ordered more than once.")
        lead = <<lead>>
        if lead and f"avg_{lead}" in k:
            missing = int(k[f"orders_without_{lead}"])
            note(f"Average {lead}: **{float(k[f'avg_{lead}']):.2f}**" + (f" (over the orders that have one; {missing:,} don't, and AVG skips them)." if missing else "."))
        if "late_pct" in k:
            open_overdue = int(k.get("open_overdue") or 0)
            note(f"{float(k['late_pct']):.1f}% of orders went out after the promised date"
                 + (f", counting {open_overdue:,} order(s) not shipped yet and already past it (leaving them out would flatter the figure)."
                    if open_overdue else "; no unshipped order is past its date yet."))
        if "avg_rating" in k:
            note(f"Average rating: {float(k['avg_rating']):.2f}.")
    ''', lead=repr(d.lead_name if d.lead else None))
    return Cell("kpis", "Headline KPIs", SECTIONS[4], story, (stmt("kpis", "Headline KPIs", sql),), check)


PERIOD_CTE = {"quarter": "quarterly", "month": "monthly", "year": "yearly", "week": "weekly"}


def _period_index(b: Builder) -> str:
    """year × 4 + quarter (or × 12 + month, or the year): consecutive periods differ by exactly 1, even across a gap."""
    d = b.d
    if d.period == "week":
        return "week_index"
    per_year = {"quarter": 4, "month": 12}.get(d.period)
    return f"{d.year_col} * {per_year} + {d.number_col}" if per_year else d.year_col


def _period_ctes(b: Builder, D: Dialect, extra: list[str], name: str | None = None, flag_partial: bool = False) -> str:
    d = b.d
    name = name or PERIOD_CTE[d.period]
    cols = list(d.period_cols) + [f"{_period_index(b)} AS period_index"]
    if flag_partial:   # 1 for a period with too few days of data: never the base of a comparison
        parts = [b.period_where(p) for p in d.partial_periods]
        cols.append(f"CASE WHEN {' OR '.join(parts)} THEN 1 ELSE 0 END AS is_partial" if parts else "0 AS is_partial")
    cols += extra
    return fill('''
        <<name>> AS (
            SELECT <<cols>>
            FROM   order_facts
            WHERE  <<y>> IS NOT NULL
            GROUP  BY <<group>>
        )''', name=name, cols=",\n           ".join(cols), y=d.year_col, group=", ".join(d.period_cols))


def _kpi_by_period_cell(b: Builder) -> Cell:
    d = b.d
    name = PERIOD_CTE[d.period]
    step = {"quarter": "qoq", "month": "mom", "year": "yoy", "week": "wow"}[d.period]
    year_lag = {"quarter": 4, "month": 12}.get(d.period)        # weeks: no year-over-year (52 or 53 weeks per ISO year)

    def sql(D: Dialect) -> str:
        # the spread (STDDEV_SAMP) and count of each average go along, so that the check can tell a real change from noise
        measures = ["SUM(revenue) AS revenue", "STDDEV_SAMP(revenue) AS order_value_sd"] if b.money else []
        measures.append("COUNT(*) AS orders")
        if d.customer:
            measures.append(f"COUNT(DISTINCT {D.ident(d.customer[1])}) AS customers")
        if d.lead:
            measures += [f"AVG({d.lead_name}) AS avg_{d.lead_name}", f"COUNT({d.lead_name}) AS {d.lead_name}_orders",
                         f"STDDEV_SAMP({d.lead_name}) AS {d.lead_name}_sd"]
        if d.required:
            measures += ["SUM(shipped_late) AS late_orders", "COUNT(shipped_late) AS checked_orders"]
        if d.rating:
            measures += ["AVG(rating_score) AS avg_rating", "COUNT(rating_score) AS rated_orders", "STDDEV_SAMP(rating_score) AS rating_sd"]
        main = "revenue" if b.money else "orders"
        lags = [f"CASE WHEN p.period_index - LAG(p.period_index) OVER (ORDER BY p.period_index) = 1\n"
                f"            AND LAG(p.is_partial) OVER (ORDER BY p.period_index) = 0\n"
                f"           THEN LAG(p.{main}) OVER (ORDER BY p.period_index)\n"
                f"           END AS prev_{main}"]
        if year_lag:
            lags.append(f"CASE WHEN y.is_partial = 0 THEN y.{main} END AS {main}_year_ago")
        out = list(d.period_cols)
        if d.partial_periods:
            out.append("CASE WHEN is_partial = 1 THEN 'partial' ELSE 'complete' END AS period_status")
        if b.money:
            out += [f"{D.round('revenue', 2)} AS revenue", "orders", f"{D.ratio('revenue', 'orders', 2)} AS avg_order_value",
                    f"{D.round('order_value_sd', 2)} AS order_value_sd"]
        else:
            out.append("orders")
        if d.customer:
            out.append("customers")
        if d.lead:
            out += [f"{D.round(f'avg_{d.lead_name}', 1)} AS avg_{d.lead_name}", f"{d.lead_name}_orders",
                    f"{D.round(f'{d.lead_name}_sd', 2)} AS {d.lead_name}_sd"]
        if d.required:
            out += ["late_orders", "checked_orders", f"{D.pct('late_orders', 'checked_orders')} AS late_pct"]
        if d.rating:
            out += [f"{D.round('avg_rating', 2)} AS avg_rating", "rated_orders", f"{D.round('rating_sd', 2)} AS rating_sd"]
        out.append(f"{D.pct_change(main, f'prev_{main}')} AS {main}_{step}_pct")
        if year_lag:
            out.append(f"{D.pct_change(main, f'{main}_year_ago')} AS {main}_yoy_pct")
        comments = []
        if d.multi_year:
            comments.append(f"-- The data spans several years, so periods are grouped and ordered by {b.period_order()}.")
        for p in d.partial_periods:
            comments.append(f"-- {d.label(p)} is partial (data from {max(p.first_seen, p.start)} to {min(p.last_seen, p.end)}): "
                            "its change is missing days, not a trend.")
        comments.append(f"-- period_index = {_period_index(b)}: LAG() takes the previous *row*, so prev_{main} is kept only when that row is "
                        f"the calendar neighbour (index - 1) and complete (is_partial = 0); otherwise it is NULL, not a wrong comparison.")
        if year_lag:
            comments.append(f"-- {main}_year_ago joins the same {d.period} a year earlier on period_index - {year_lag}, so a gap can't shift it, "
                            "and is NULL when that period is partial.")
        if d.lead:
            comments.append(f"-- The suspected driver ({d.lead_name}) sits next to the symptom ({main}).")
        return "\n".join(comments + [
            "WITH " + _period_ctes(b, D, measures, name, flag_partial=True).strip() + ",",
            fill('''
                with_prev AS (
                    SELECT p.*,
                           <<lags>>
                    FROM   <<name>> p<<join>>
                )
                SELECT <<out>>
                FROM   with_prev
                ORDER  BY <<order>>''', lags=",\n           ".join(lags), name=name, out=",\n       ".join(out), order=b.period_order(),
                 join=f"\n    LEFT   JOIN {name} y ON y.period_index = p.period_index - {year_lag}" if year_lag else "")])

    story = Step(
        "kpi_by_period",
        f"Orders run over {len(d.periods)} {d.period}s" + (f"; {', '.join(d.label(p) for p in d.partial_periods)} is partial" if d.partial_periods else "") + ".",
        "A KPI only means something next to its history. Putting the suspected driver in the same query as the symptom lets one table show "
        "whether they move together." + (" The partial period is labelled so that its drop is not mistaken for a trend." if d.partial_periods else ""),
        f"We compute the KPIs per {d.period} with LAG() for the change from the previous {d.period}"
        + (" and from the same period a year earlier" if year_lag else "") + ", so that trends are measured, not eyeballed.",
        f"The `{name}` CTE groups order_facts by {b.period_order()}; `with_prev` adds LAG() (the previous row's value in time order); the final "
        "SELECT turns them into % changes with 100.0 * (new − old) / NULLIF(old, 0)." + (" period_status marks partial periods." if d.partial_periods else ""),
    )
    check = fill('''
        per = result.copy()
        unit, main = <<unit>>, <<main>>
        cols = list(per.columns)
        per["period"] = [period_label(unit, row[cols[0]], row[cols[1]] if unit != "year" else 0) for _, row in per.iterrows()]
        status = per["period_status"] if "period_status" in per else pd.Series("complete", index=per.index)
        complete = per[status == "complete"]
        change = f"{main}_<<step>>_pct"
        metrics["periods"] = per[["period", main]].assign(status=status).to_dict("records")
        metrics["period_table"] = [{k: (None if pd.isna(v) else (str(v) if isinstance(v, str) else float(v))) for k, v in row.items()}
                                   for row in per.assign(period_status=status).to_dict("records")]
        driver = <<driver>>
        # Two stacked panels on one time axis (never two y-scales on one plot): the symptom on top, the driver below.
        panels = 2 if driver and driver in per else 1
        fig, axes = plt.subplots(panels, 1, figsize=(10, 2.9 * panels + 0.6), sharex=True, squeeze=False)
        ax = axes[0][0]
        ax.bar(per["period"], per[main].astype(float), width=0.6, color=[C_SERIES if s == "complete" else C_MUTED for s in status])
        ax.set_title(f"{main.capitalize()} per {unit}" + (" (grey = partial period)" if (status != "complete").any() else ""), loc="left")
        if panels == 2:
            axes[1][0].plot(per["period"], per[driver].astype(float), color=C_DRIVER, marker="o", markersize=5)
            axes[1][0].set_title(f"{driver.replace('_', ' ')} per {unit} (the suspected driver)", loc="left")
        axes[-1][0].tick_params(axis="x", labelrotation=45)
        plt.tight_layout(); plt.show()
        days = <<days>>   # days with data in each period: months and quarters differ in length
        if len(complete) >= 2:
            prev, last = complete.iloc[-2], complete.iloc[-1]
            pct = 100 * (float(last[main]) - float(prev[main])) / float(prev[main]) if float(prev[main]) else float("nan")
            d1, d2 = days.get(prev["period"]), days.get(last["period"])
            per_day = 100 * ((float(last[main]) / d2) / (float(prev[main]) / d1) - 1) if d1 and d2 and float(prev[main]) else pct
            metrics["qoq_last_complete"] = {"from": prev["period"], "to": last["period"], "pct": pct, "per_day_pct": per_day,
                                            "days_from": d1, "days_to": d2}
            show = (lambda v: f"{float(v):,.2f}") if main == "revenue" else (lambda v: f"{float(v):,.0f}")
            note(f"Last two complete {unit}s: {main} went from {show(prev[main])} ({prev['period']}) to {show(last[main])} "
                 f"({last['period']}), **{pct:+.1f}%**.")
            if d1 and d2 and abs(d2 / d1 - 1) >= 0.03:
                note(f"The two {unit}s differ in length ({d1} vs {d2} days with data), so compare per day: **{per_day:+.1f}%**. "
                     f"A shorter {unit} looks like a fall in totals even when every day is the same.")
            # Is each change bigger than normal variation? 95% intervals from checklib (counts: Poisson; totals: compound
            # Poisson; averages: Welch; shares: Newcombe). Per day when the periods differ in length, like the comparison above.
            t1, t2 = (d1, d2) if d1 and d2 and abs(d2 / d1 - 1) >= 0.03 else (1, 1)
            num = lambda row, col: None if col not in row.index or pd.isna(row[col]) else float(row[col])  # noqa: E731
            lead = <<lead>>
            sig = {"orders": ci_rate_change(num(prev, "orders"), t1, num(last, "orders"), t2)}
            if "customers" in per:
                sig["customers"] = ci_rate_change(num(prev, "customers"), t1, num(last, "customers"), t2)
            if "order_value_sd" in per:
                sig["revenue"] = ci_total_change(num(prev, "orders"), num(prev, "avg_order_value"), num(prev, "order_value_sd"), t1,
                                                 num(last, "orders"), num(last, "avg_order_value"), num(last, "order_value_sd"), t2)
                sig["avg_order_value"] = ci_mean_change(num(prev, "avg_order_value"), num(prev, "order_value_sd"), num(prev, "orders"),
                                                        num(last, "avg_order_value"), num(last, "order_value_sd"), num(last, "orders"))
            if lead:
                sig[f"avg_{lead}"] = ci_mean_change(num(prev, f"avg_{lead}"), num(prev, f"{lead}_sd"), num(prev, f"{lead}_orders"),
                                                    num(last, f"avg_{lead}"), num(last, f"{lead}_sd"), num(last, f"{lead}_orders"))
            if "late_orders" in per:
                sig["late_pct"] = ci_share_change(num(prev, "late_orders"), num(prev, "checked_orders"),
                                                  num(last, "late_orders"), num(last, "checked_orders"))
            if "rating_sd" in per:
                sig["avg_rating"] = ci_mean_change(num(prev, "avg_rating"), num(prev, "rating_sd"), num(prev, "rated_orders"),
                                                   num(last, "avg_rating"), num(last, "rating_sd"), num(last, "rated_orders"))
            sig = {k: v for k, v in sig.items() if v}
            metrics["significance"] = sig
            if sig:
                unit_of = lambda k: " pts" if k == "late_pct" else "%"  # noqa: E731
                note("Real change or noise? " + "; ".join(
                    f"{k.replace('avg_', 'average ').replace('_', ' ')} {v['change']:+.1f}{unit_of(k)} (95% CI {v['lo']:+.1f} to "
                    f"{v['hi']:+.1f}{unit_of(k)})" + ("" if v["significant"] else ", **within normal variation**") for k, v in sig.items())
                     + ". A change whose interval includes zero could be noise, however large it looks.")
        else:
            note(f"Only {len(complete)} complete {unit} so far: nothing to compare yet. The table still gives the level per {unit}; "
                 f"period-over-period verdicts start once a second complete {unit} arrives.")
        if (status != "complete").any():
            part = per[status != "complete"].iloc[-1]
            idx = per.index.get_loc(part.name)
            if idx > 0 and not pd.isna(part[change]):
                naive = float(part[change])
                metrics["naive_change"] = {"period": part["period"], "pct": naive}
                note(f"⚠️ A naive report would say {main} changed **{naive:+.1f}%** in {part['period']}, but that {unit} is partial: "
                     + ("the drop is missing days, not a trend." if naive < 0 else
                        f"it is up even with days missing, so the full {unit} will be higher still; compare it only once it is complete."))
        yoy = f"{main}_yoy_pct"
        if yoy in per and change != yoy and len(complete):
            last = complete.iloc[-1]                  # only the latest complete period: an older one would be a different headline
            if not pd.isna(last[yoy]):
                metrics["yoy"] = {"period": last["period"], "pct": float(last[yoy])}
                note(f"Year over year, {last['period']} is **{float(last[yoy]):+.1f}%** against the same {unit} a year earlier, which removes seasonality.")
            elif len(per) > <<year_lag>>:
                note(f"No year-over-year figure for {last['period']}: the same {unit} a year earlier is missing or partial, and comparing with "
                     "a partial period would overstate the change.")
        if driver and driver in per and len(complete) >= 2:
            note(f"{driver}: {float(complete.iloc[-2][driver]):.1f} → {float(complete.iloc[-1][driver]):.1f} over the same two {unit}s.")
    ''', unit=repr(d.period), main=repr("revenue" if b.money else "orders"), step=step,
        driver=repr(f"avg_{d.lead_name}" if d.lead else "avg_rating" if d.rating else None),
        days=repr({d.label(p): p.covered for p in d.periods}), year_lag=year_lag or 0, lead=repr(d.lead_name if d.lead else None))
    return Cell("kpi_by_period", f"KPIs per {d.period}, with the driver next to the symptom", SECTIONS[4], story,
                (stmt("kpi_by_period", f"KPIs per {d.period} with LAG()", sql),), check)


# ═════════════════════════════════════════════════════════════════════════
# 5 · Window functions
# ═════════════════════════════════════════════════════════════════════════
def _running_total_cell(b: Builder) -> Cell:
    d = b.d
    main = "revenue" if b.money else "orders"
    agg = "SUM(revenue)" if b.money else "COUNT(*)"

    def sql(D: Dialect) -> str:
        order = b.period_order()
        frame = "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"
        running = f"SUM({main}) OVER (ORDER BY {order}\n                             {frame})"
        return fill('''
            WITH <<cte>>
            SELECT <<cols>>,
                   <<main_r>> AS <<main>>,
                   <<run_r>> AS running_<<main>>,
                   <<pct>> AS running_pct_of_total
            FROM   per_period
            ORDER  BY <<order>>''', cte=_period_ctes(b, D, [f"{agg} AS {main}"], "per_period").strip(), cols=", ".join(d.period_cols),
                    main=main, main_r=D.round(main, 2), run_r=D.round(running, 2),
                    pct=D.pct(running, f"SUM({main}) OVER ()"), order=order)

    story = Step(
        "running_total",
        f"{main.capitalize()} per {d.period} is known.",
        "A running total shows how quickly the total builds up and when half of it was reached, which a per-period chart hides.",
        "We add a running total with a window frame, so that cumulative progress can be read directly.",
        "SUM(…) OVER (ORDER BY period ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) adds up every period so far, without collapsing "
        "the rows like GROUP BY would; SUM(…) OVER () is the grand total, so their ratio is the share reached so far.",
    )
    check = fill('''
        run = result
        cols = list(run.columns)
        unit = <<unit>>
        labels = [period_label(unit, row[cols[0]], row[cols[1]] if unit != "year" else 0) for _, row in run.iterrows()]
        half_at = int((run["running_pct_of_total"].astype(float) >= 50).to_numpy().argmax())
        half, when = run.iloc[half_at], labels[half_at]
        metrics["half_reached"] = when
        plt.figure(figsize=(9, 3.5))
        plt.plot(range(len(run)), run["running_<<main>>"].astype(float), color=C_SERIES, marker="o", markersize=5)
        plt.xticks(range(len(run)), labels, rotation=45)
        plt.title("Running total of <<main>>"); plt.tight_layout(); plt.show()
        note(f"Half of all <<main>> had been reached by **{when}** ({float(half['running_pct_of_total']):.1f}% at that point).")
    ''', unit=repr(d.period), main=main)
    return Cell("running_total", "Running total", SECTIONS[5], story, (stmt("running_total", "Running total with a window frame", sql),), check)


def _top_n_cell(b: Builder) -> Cell:
    d = b.d
    table, label, group = d.item
    view = "line_facts" if group.level == "line" else "order_facts"
    main = "revenue" if b.money else "orders"
    agg = "SUM(revenue)" if b.money else "COUNT(*)"

    def sql(D: Dialect) -> str:
        g, item = D.ident(group.alias), D.ident(label)
        return fill('''
            WITH item_totals AS (
                SELECT <<g>>,
                       <<item>>,
                       <<agg>> AS <<main>>
                FROM   <<view>>
                GROUP  BY <<g>>, <<item>>
            ),
            ranked AS (
                SELECT t.*,
                       DENSE_RANK() OVER (PARTITION BY <<g>> ORDER BY <<main>> DESC) AS <<main>>_rank,
                       ROW_NUMBER() OVER (PARTITION BY <<g>> ORDER BY <<main>> DESC, <<item>>) AS row_num
                FROM   item_totals t
            )
            SELECT <<g>>,
                   <<main>>_rank,
                   row_num,
                   <<item>>,
                   <<main_r>> AS <<main>>
            FROM   ranked
            WHERE  <<main>>_rank <= <<n>>   -- DENSE_RANK keeps ties, so a group can return more than <<n>> rows
            ORDER  BY <<g>>, <<main>>_rank, <<item>>''', g=g, item=item, agg=agg, main=main, view=view, n=TOP_N, main_r=D.round(main, 2))

    story = Step(
        "top_n",
        f"Each `{label}` belongs to one `{group.alias}`.",
        f"'Top {TOP_N} per group' is the classic window-function task. RANK and DENSE_RANK give tied rows the same number (so a group can return "
        f"more than {TOP_N}); ROW_NUMBER never ties, so it returns exactly {TOP_N}.",
        f"We rank `{label}` within each `{group.alias}` by {main}, so that the leaders of every group are visible side by side.",
        f"`item_totals` adds up {main} per group and item; `ranked` adds DENSE_RANK() and ROW_NUMBER() OVER (PARTITION BY the group ORDER BY {main} DESC). "
        "PARTITION BY restarts the numbering in every group. The outer query keeps the top ranks: a window result can't be filtered in the same "
        "SELECT's WHERE, which is why it sits in a CTE.",
    )
    check = fill('''
        top = result
        g, item, main = <<g>>, <<item>>, <<main>>
        sizes = top.groupby(g).size()
        ties = sizes[sizes > <<n>>]
        metrics["top_items"] = {str(k): v[item].astype(str).tolist() for k, v in top.groupby(g)}
        leader = top.sort_values(main, ascending=False).iloc[0]
        note(f"Biggest single item: `{leader[item]}` in `{leader[g]}` ({float(leader[main]):,.2f}).")
        if len(ties):
            note(f"Ties: {len(ties)} group(s) return more than <<n>> rows with DENSE_RANK; use ROW_NUMBER if exactly <<n>> are needed.")
        else:
            note("No ties: DENSE_RANK and ROW_NUMBER pick the same items here, but only ROW_NUMBER guarantees exactly <<n>> per group.")
    ''', g=repr(group.alias), item=repr(label), main=repr(main), n=TOP_N)
    return Cell("top_n", f"Top {TOP_N} `{label}` per `{group.alias}`", SECTIONS[5], story,
                (stmt("top_n", f"Top {TOP_N} per group with DENSE_RANK and ROW_NUMBER", sql),), check)


def _latest_record_cell(b: Builder) -> Cell:
    d = b.d
    cust, date = d.customer[1], d.date[1]

    def sql(D: Dialect) -> str:
        c, dt, ok = D.ident(cust), D.ident(date), D.ident(b.order_key)
        recency = D.days_between(f"r.{dt}", f"(SELECT MAX({dt}) FROM order_facts)", d.date_is_timestamp)
        return fill('''
            WITH ranked AS (
                SELECT <<c>>, <<ok>>, <<dt>><<rev>>,
                       ROW_NUMBER() OVER (PARTITION BY <<c>> ORDER BY <<dt>> DESC, <<ok>> DESC) AS row_num
                FROM   order_facts
                WHERE  <<c>> IS NOT NULL
                  AND  <<dt>> IS NOT NULL
            )
            SELECT r.<<c>>,
                   r.<<ok>> AS latest_<<okn>>,
                   r.<<dt>> AS latest_<<dtn>>,<<rev_out>>
                   <<recency>> AS days_since_last_order   -- measured to the last date in the data, not to today
            FROM   ranked r
            WHERE  r.row_num = 1
            ORDER  BY days_since_last_order DESC, r.<<c>>''', c=c, ok=ok, dt=dt, okn=re.sub(r"\W", "_", b.order_key),
                    dtn=re.sub(r"\W", "_", date), rev=", revenue" if b.money else "",
                    rev_out=f"\n       {D.round('r.revenue', 2)} AS latest_order_revenue," if b.money else "", recency=recency)

    story = Step(
        "latest_record",
        f"Customers place several orders each (`{cust}` repeats in order_facts).",
        "'The latest record per key' is how you de-duplicate history tables and how you measure recency. ROW_NUMBER() numbers each customer's "
        "orders from newest to oldest, so row 1 is the latest, exactly one per customer even when dates tie.",
        "We keep each customer's latest order and measure the days since it, so that lapsing customers stand out.",
        f"ROW_NUMBER() OVER (PARTITION BY {cust} ORDER BY {date} DESC, {b.order_key} DESC): the order_id tie-breaker makes the pick "
        "deterministic. Recency is measured to the latest date in the data, because measuring to today would make an old extract look like "
        "every customer had left.",
    )
    check = fill('''
        latest = result
        days = latest["days_since_last_order"].astype(float)
        metrics["recency_median"] = float(days.median())
        lapsed = int((days > 180).sum())
        note(f"{len(latest):,} customers (one row each). Median time since the last order: **{days.median():.0f} days**; "
             f"{lapsed:,} customer(s) have not ordered in over 180 days before the end of the data.")
    ''')
    return Cell("latest_record", "Each customer's latest order", SECTIONS[5], story,
                (stmt("latest_record", "The latest order per customer with ROW_NUMBER()", sql),), check)


def _rfm_cell(b: Builder) -> Cell:
    d = b.d
    cust, date = d.customer[1], d.date[1]
    n = NTILE_BUCKETS

    def view(D: Dialect) -> str:
        c, dt = D.ident(cust), D.ident(date)
        recency = D.days_between(f"MAX({dt})", f"(SELECT MAX({dt}) FROM order_facts)", d.date_is_timestamp)
        # CUME_DIST is the share of customers at or below this value, so equal values share a score; ROUND(…, 9) removes floating
        # noise before CEIL (5 × 0.4 can come out as 2.0000000000000004, which CEIL would turn into 3)
        score = lambda order: f"CEIL({D.round(f'<<n>> * CUME_DIST() OVER (ORDER BY {order})', 9)})"  # noqa: E731
        return fill('''
            CREATE OR REPLACE VIEW customer_rfm AS
            WITH customer_totals AS (
                SELECT <<c>>,
                       <<recency>> AS recency_days,
                       COUNT(*) AS frequency,
                       SUM(revenue) AS monetary
                FROM   order_facts
                WHERE  <<c>> IS NOT NULL
                  AND  <<dt>> IS NOT NULL
                GROUP  BY <<c>>
            ),
            scored AS (
                SELECT t.*,
                       <<r>> AS r_score,   -- <<n>> = most recent
                       <<f>> AS f_score,   -- <<n>> = most orders
                       <<m>> AS m_score    -- <<n>> = most revenue
                FROM   customer_totals t
            )
            SELECT s.*,
                   CASE WHEN r_score >= 4 AND f_score >= 4 THEN 'Champions'
                        WHEN f_score >= 4 THEN 'Loyal'
                        WHEN r_score <= 2 AND f_score >= 3 THEN 'At risk'
                        WHEN r_score >= 4 AND f_score <= 2 THEN 'Recent, few orders'
                        WHEN r_score <= 2 AND f_score <= 2 THEN 'Hibernating'
                        ELSE 'Needs attention'
                   END AS segment
            FROM   scored s''', c=c, dt=dt, recency=recency, r=score("recency_days DESC"), f=score("frequency"), m=score("monetary"),
                    n=n)

    def scores(D: Dialect) -> str:
        return fill('''
            SELECT f_score,
                   COUNT(*) AS customers,
                   MIN(frequency) AS fewest_orders,
                   MAX(frequency) AS most_orders
            FROM   customer_rfm
            GROUP  BY f_score
            ORDER  BY f_score''')

    def summary(D: Dialect) -> str:
        return fill('''
            SELECT segment,
                   COUNT(*) AS customers,
                   <<rev>> AS revenue,
                   <<pct>> AS revenue_pct,
                   <<rec>> AS avg_recency_days
            FROM   customer_rfm
            GROUP  BY segment
            ORDER  BY revenue DESC, segment''', rev=D.round("SUM(monetary)", 2),
                    pct=D.pct("SUM(monetary)", "(SELECT SUM(monetary) FROM customer_rfm)"), rec=D.round("AVG(recency_days)", 0))

    story = Step(
        "rfm",
        "Every customer has a last order date, an order count and a revenue total.",
        "RFM (recency, frequency, monetary) turns three numbers into a segment a marketer can act on. The usual NTILE(5) forces five equal-sized "
        "buckets, so customers with the same number of orders can land in different buckets depending on their id: that is arbitrary, and it "
        "decides who gets a campaign.",
        "We score every customer 1-5 on R, F and M and name the segments, so that revenue concentration and churn risk become visible.",
        "`customer_totals` aggregates order_facts per customer; `scored` turns each measure into a 1-5 score with CEIL(5 × CUME_DIST()), the "
        "share of customers at or below that value, so equal values always get equal scores (buckets are no longer forced to the same size); a "
        "CASE turns the scores into named segments. It is saved as the view customer_rfm, then summarised per score and per segment.",
    )
    check = fill('''
        fs = outputs["rfm_scores"]
        overlaps = int((fs["fewest_orders"].iloc[1:].to_numpy() <= fs["most_orders"].iloc[:-1].to_numpy()).sum())
        metrics["rfm_ties_split"] = overlaps                 # 0: no order count is split across two F scores
        note("Frequency scores by order count: " + "; ".join(f"{int(r.f_score)} = {int(r.fewest_orders)}" +
             (f"-{int(r.most_orders)}" if r.most_orders != r.fewest_orders else "") + f" orders ({int(r.customers)} customers)"
             for r in fs.itertuples()) + ". Customers with the same number of orders always share a score.")
        seg = outputs["rfm_segments"]
        metrics["rfm"] = {r.segment: {"customers": int(r.customers), "revenue_pct": float(r.revenue_pct)} for r in seg.itertuples()}
        top = seg.iloc[0]
        metrics["rfm_top_share"] = (str(top.segment), float(top.revenue_pct), int(top.customers))
        plt.figure(figsize=(8, 3.5))
        plt.barh(seg["segment"].iloc[::-1], seg["revenue_pct"].astype(float).iloc[::-1], color=C_SERIES, height=0.6)
        plt.xlabel("% of revenue"); plt.title("Revenue share by RFM segment"); plt.tight_layout(); plt.show()
        total = int(seg["customers"].sum())
        note(f"**{top.segment}** ({int(top.customers):,} of {total:,} customers) bring **{float(top.revenue_pct):.1f}%** of revenue.")
        risk = seg[seg["segment"] == "At risk"]
        if len(risk):
            note(f"At risk: {int(risk.customers.iloc[0]):,} customers who used to order often but not recently ({float(risk.revenue_pct.iloc[0]):.1f}% of revenue).")
    ''')
    return Cell("rfm", "RFM segments with CUME_DIST", SECTIONS[5], story,
                (stmt("rfm_view", "View `customer_rfm`: tie-safe 1-5 scores per customer", view),
                 stmt("rfm_scores", "Order counts behind each frequency score", scores),
                 stmt("rfm_segments", "Customers and revenue per segment", summary)), check)


# ═════════════════════════════════════════════════════════════════════════
# 6 · Root cause
# ═════════════════════════════════════════════════════════════════════════
def _driver_tree_cell(b: Builder) -> Cell:
    d = b.d
    before, after = d.compare
    lb, la = d.label(before), d.label(after)

    def per_period(D: Dialect) -> str:
        cols = list(d.period_cols) + [
            f"{D.round('SUM(revenue)', 2)} AS revenue", "COUNT(*) AS orders"]
        if d.customer:
            cols += [f"COUNT(DISTINCT {D.ident(d.customer[1])}) AS customers",
                     f"{D.ratio('COUNT(*)', f'COUNT(DISTINCT {D.ident(d.customer[1])})', 4)} AS orders_per_customer"]
        cols.append(f"{D.ratio('SUM(revenue)', 'COUNT(*)', 4)} AS avg_order_value")
        if d.revenue.quantity:
            cols += [f"{D.ratio('SUM(units)', 'COUNT(*)', 4)} AS units_per_order", f"{D.ratio('SUM(revenue)', 'SUM(units)', 4)} AS revenue_per_unit"]
        return fill('''
            -- revenue = orders × average order value<<tree>>
            SELECT <<cols>>
            FROM   order_facts
            WHERE  <<b>>
               OR  <<a>>
            GROUP  BY <<group>>
            ORDER  BY <<order>>''', tree=("; orders = customers × orders per customer" if d.customer else "")
                    + ("; AOV = units per order × revenue per unit" if d.revenue.quantity else ""),
                    cols=",\n       ".join(cols), b=b.period_where(before), a=b.period_where(after), group=", ".join(d.period_cols),
                    order=b.period_order())

    statements = [stmt("tree", f"The drivers of revenue in {lb} and {la}", per_period)]
    for i, dim in enumerate(d.dimensions):
        view = "line_facts" if dim.level == "line" else "order_facts"

        def by_dim(D: Dialect, dim=dim, view=view) -> str:
            g = D.ident(dim.alias)
            return fill('''
                WITH by_segment AS (
                    SELECT <<g>>,
                           SUM(CASE WHEN <<b>> THEN revenue ELSE 0 END) AS revenue_before,
                           SUM(CASE WHEN <<a>> THEN revenue ELSE 0 END) AS revenue_after
                    FROM   <<view>>
                    GROUP  BY <<g>>
                )
                SELECT <<g>>,
                       <<rb>> AS revenue_before,
                       <<ra>> AS revenue_after,
                       <<rc>> AS revenue_change,
                       <<share>> AS share_of_change_pct
                FROM   by_segment
                ORDER  BY ABS(revenue_after - revenue_before) DESC, <<g>>''', g=g, b=b.period_where(before), a=b.period_where(after), view=view,
                        rb=D.round("revenue_before", 2), ra=D.round("revenue_after", 2), rc=D.round("revenue_after - revenue_before", 2),
                        share=D.pct("(revenue_after - revenue_before)", "SUM(revenue_after - revenue_before) OVER ()"))
        statements.append(stmt(f"by_{dim.alias}", f"Change in revenue by `{dim.alias}` ({lb} → {la})", by_dim))
    story = Step(
        "driver_tree",
        f"The last two complete {d.period}s are {lb} and {la}" + (f" ({', '.join(d.label(p) for p in d.partial_periods)} is partial and left out)" if d.partial_periods else "") + ".",
        "'Revenue changed' is a symptom. Revenue = orders × average order value, so the change comes from more or fewer orders, bigger or smaller orders, "
        "or both, and then from particular segments. Splitting it is how a KPI move becomes a cause someone can act on.",
        f"We split the revenue change from {lb} to {la} into a volume effect and an order-value effect that add up exactly, and then by "
        + (_names([x.alias for x in d.dimensions]) if d.dimensions else "segment") + ", so that the change is traced to the part that moved.",
        "The first query computes each driver for both periods. The others use conditional aggregation (SUM(CASE WHEN period = … THEN revenue "
        "ELSE 0 END)) to put both periods side by side per segment, and a window SUM(…) OVER () to express each segment's change as a share of the total change.",
    )
    check = fill('''
        tree = outputs["tree"]
        before, after = tree.iloc[0], tree.iloc[-1]
        o1, o2 = float(before.orders), float(after.orders)
        r1, r2 = float(before.revenue), float(after.revenue)
        a1, a2 = r1 / o1, r2 / o2
        # Shapley split: each effect is measured at the average of the other, so the two add up to the change exactly
        volume, value = (o2 - o1) * (a1 + a2) / 2, (a2 - a1) * (o1 + o2) / 2
        classic = {"volume": (o2 - o1) * a1, "order_value": o1 * (a2 - a1), "interaction": (o2 - o1) * (a2 - a1)}
        delta = r2 - r1
        metrics["driver_split"] = {"from": <<lb>>, "to": <<la>>, "delta": delta, "volume": volume, "order_value": value, "classic": classic}
        steps = [("before", r1), ("more/fewer orders", volume), ("order value", value)]
        fig, ax = plt.subplots(figsize=(8, 3.5))
        level = 0.0
        for i, (name, amount) in enumerate(steps):
            bottom = 0 if i == 0 else level if amount >= 0 else level + amount
            ax.bar(name, abs(amount), bottom=bottom, width=0.6, color=C_TOTAL if i == 0 else C_UP if amount >= 0 else C_DOWN)
            level = amount if i == 0 else level + amount
        ax.bar("after", r2, width=0.6, color=C_TOTAL)
        ax.set_title(<<lb>> + " → " + <<la>> + ": what moved revenue"); plt.tight_layout(); plt.show()
        sizes = {"orders": abs(volume), "order value": abs(value)}
        main = "orders" if sizes["orders"] >= sizes["order value"] else "order value"
        share = sizes[main] / (sum(sizes.values()) or 1.0)   # share of the movement by size: never above 100%
        note(f"Revenue moved **{delta:+,.2f}** ({100 * delta / r1:+.1f}%): **{volume:+,.2f}** from the number of orders "
             f"({int(o1):,} → {int(o2):,}) and **{value:+,.2f}** from the average order value ({a1:,.2f} → {a2:,.2f}). "
             f"**{main.capitalize()}** accounts for about {share:.0%} of the movement.")
        note(f"How the split works: each effect is measured at the average of the other (a Shapley split), so the two add up to the change "
             f"exactly. The classic split (orders at the old order value, then order value at the new number of orders) gives "
             f"{classic['volume']:+,.2f} and {classic['order_value']:+,.2f}, and leaves {classic['interaction']:+,.2f} of 'both' to be assigned "
             "by convention; that leftover is why the classic shares depend on which effect is measured first.")
        if "orders_per_customer" in tree:
            note(f"Orders = customers × orders per customer: customers {int(before.customers):,} → {int(after.customers):,}, "
                 f"orders per customer {float(before.orders_per_customer):.2f} → {float(after.orders_per_customer):.2f}.")
        contributors = {}
        for label in [k for k in outputs if k.startswith("by_")]:
            seg = outputs[label]
            col = seg.columns[0]
            top = seg.iloc[0]
            contributors[col] = {"segment": str(top[col]), "change": float(top.revenue_change), "share": float(top.share_of_change_pct)}
            metrics.setdefault("segment_changes", {})[col] = [
                {"segment": str(r[0]), "before": float(r.revenue_before), "after": float(r.revenue_after), "change": float(r.revenue_change)}
                for r in seg.itertuples(index=False)]
            note(f"By `{col}`: **{top[col]}** moved most ({float(top.revenue_change):+,.2f}, {float(top.share_of_change_pct):.0f}% of the change).")
        metrics["top_contributors"] = contributors
    ''', lb=repr(lb), la=repr(la))
    return Cell("driver_tree", "Driver tree: what moved revenue", SECTIONS[6], story, tuple(statements), check)


def _drilldown_cell(b: Builder) -> Cell:
    d = b.d
    drivers = ([f"avg_{d.lead_name}"] if d.lead else []) + (["late_pct"] if d.required else []) + (["avg_rating"] if d.rating else [])
    seg = next((dim for dim in d.dimensions if dim.level == "order"), None)
    main = "revenue" if b.money else "orders"
    agg = "SUM(revenue)" if b.money else "COUNT(*)"

    def driver_cols(D: Dialect, cond: str | None = None, suffix: str = "") -> list[str]:
        out = []
        if d.lead:
            x = d.lead_name if cond is None else f"CASE WHEN {cond} THEN {d.lead_name} END"
            out.append(f"{D.round(f'AVG({x})', 1)} AS avg_{d.lead_name}{suffix}")
        if d.required:
            num = "shipped_late" if cond is None else f"CASE WHEN {cond} THEN shipped_late END"
            out.append(f"{D.pct(f'SUM({num})', f'COUNT({num})')} AS late_pct{suffix}")
        if d.rating:
            x = "rating_score" if cond is None else f"CASE WHEN {cond} THEN rating_score END"
            out.append(f"{D.round(f'AVG({x})', 2)} AS avg_rating{suffix}")
        return out

    def series(D: Dialect) -> str:
        cols = list(d.period_cols) + [f"{D.round(agg, 2)} AS {main}"] + driver_cols(D)
        where = [f"{d.year_col} IS NOT NULL"] + [f"NOT {b.period_where(p)}" for p in d.partial_periods]
        return fill('''
            -- Complete periods only: a partial period would show a fake drop in the symptom.
            SELECT <<cols>>
            FROM   order_facts
            WHERE  <<where>>
            GROUP  BY <<group>>
            ORDER  BY <<order>>''', cols=",\n       ".join(cols), where="\n  AND  ".join(where), group=", ".join(d.period_cols),
                    order=b.period_order())

    statements = [stmt("series", f"Symptom and driver per complete {d.period}", series)]
    if seg is not None and d.compare:
        before, after = d.compare
        cb, ca = b.period_where(before), b.period_where(after)

        def by_segment(D: Dialect) -> str:
            g = D.ident(seg.alias)
            m_b = f"SUM(CASE WHEN {cb} THEN revenue ELSE 0 END)" if b.money else f"SUM(CASE WHEN {cb} THEN 1 ELSE 0 END)"
            m_a = f"SUM(CASE WHEN {ca} THEN revenue ELSE 0 END)" if b.money else f"SUM(CASE WHEN {ca} THEN 1 ELSE 0 END)"
            cols = [g, f"{D.round(m_b, 2)} AS {main}_before", f"{D.round(m_a, 2)} AS {main}_after",
                    f"{D.pct_change(m_a, m_b)} AS {main}_change_pct"]
            cols += driver_cols(D, cb, "_before") + driver_cols(D, ca, "_after")
            return fill('''
                -- AVG(CASE WHEN … THEN x END) averages only the rows of that period: CASE without ELSE gives NULL, and AVG skips NULLs.
                SELECT <<cols>>
                FROM   order_facts
                WHERE  <<cb>>
                   OR  <<ca>>
                GROUP  BY <<g>>
                ORDER  BY ABS(<<m_a>> - <<m_b>>) DESC, <<g>>''', cols=",\n       ".join(cols), cb=cb, ca=ca, g=g, m_a=m_a, m_b=m_b)
        statements.append(stmt("by_segment", f"Symptom and driver per `{seg.alias}`, {d.label(before)} vs {d.label(after)}", by_segment))
    story = Step(
        "drilldown",
        ("The suspected driver(s) are " + _names(drivers) + "." if drivers else "No operational driver column was found; the drill-down uses segments only."),
        "A driver only explains a symptom if they move together, over time and across segments. Putting both in the same query, period by period "
        "and segment by segment, is how 'revenue fell because shipping got slower' is checked instead of assumed.",
        "We line the symptom up with the driver for every complete period" + (f" and for every `{seg.alias}`" if seg is not None and d.compare else "")
        + ", so that the data says whether they move together. Correlation is not proof: the next step is a controlled test.",
        "The first query groups order_facts per complete period and returns the symptom next to the driver. "
        + ("The second compares the last two complete periods per segment with conditional aggregation, so that the segment where the symptom "
           "moved can be checked for a matching move in the driver." if seg is not None and d.compare else ""),
    )
    check = fill('''
        s = outputs["series"].copy()
        main, drivers = <<main>>, <<drivers>>
        drivers = [x for x in drivers if x in s]
        if not drivers and len(s):
            note(f"No operational driver column (shipping time, rating) to line up with {main}; over the {len(s)} complete period(s), "
                 f"{main} went from {float(s[main].iloc[0]):,.2f} to {float(s[main].iloc[-1]):,.2f}. The segment view shows where it moved.")
        if drivers and len(s) >= <<min_n>>:
            for x in drivers:
                a, b = s[main].astype(float), s[x].astype(float)
                levels, changes = ci_correlation(a.corr(b), len(s)), ci_correlation(a.diff().corr(b.diff()), len(s) - 1)
                metrics.setdefault("driver_corr", {})[x] = {"levels": levels, "changes": changes}
                said = []
                for name, c in (("levels", levels), ("period-to-period changes", changes)):
                    if c:
                        said.append(f"{name} r = **{c['r']:+.2f}** (95% CI {c['lo']:+.2f} to {c['hi']:+.2f})")
                verdict = ("so the data shows a relationship" if changes and changes["significant"] else
                           "but only on levels: two series that both trend correlate even when unrelated" if levels and levels["significant"] else
                           "and neither interval excludes zero, so this many periods can't show a relationship")
                note(f"{main} and {x} over {len(s)} complete periods: " + "; ".join(said) + f", {verdict}.")
        elif drivers:
            note(f"Only {len(s)} complete periods: too few for a meaningful correlation (at least <<min_n>> are needed), so compare the columns side by side.")
        if drivers and len(s) >= 2:
            first, last = s.iloc[-2], s.iloc[-1]
            x = drivers[0]
            change_main = 100 * (float(last[main]) - float(first[main])) / float(first[main]) if float(first[main]) else float("nan")
            change_x = 100 * (float(last[x]) - float(first[x])) / float(first[x]) if float(first[x]) else float("nan")
            moved = abs(change_x) >= 10
            # more days or more late orders is worse; a higher rating is better. A candidate cause got worse while the symptom fell,
            # or better while it grew; a driver that moved the other way cannot explain the change.
            worse = change_x < 0 if x.endswith("rating") else change_x > 0
            real = (metrics.get("significance") or {}).get(x, {}).get("significant", True)    # from the KPI-per-period step
            candidate = moved and worse == (change_main < 0) and real
            metrics["driver_vs_symptom"] = {"driver": x, "driver_change_pct": change_x, "symptom_change_pct": change_main, "candidate": candidate}
            note(f"Last two complete periods: {main} **{change_main:+.1f}%**, {x} **{change_x:+.1f}%** "
                 f"({float(first[x]):.1f} → {float(last[x]):.1f}). "
                 + ("The driver moved the way that would explain it: a candidate cause, to confirm with a controlled test "
                    "(e.g. a service-level target for a random half of orders)." if candidate else
                    f"{x} moved, but within normal variation, so it can't be called a cause." if moved and worse == (change_main < 0) else
                    f"{x} moved, but in the direction that would push {main} the other way, so it does not explain this change."
                    if moved else f"{x} barely moved, so it does not explain this change; look at the segments in the driver tree instead."))
        if "by_segment" in outputs:
            seg = outputs["by_segment"]
            top = seg.iloc[0]
            col = seg.columns[0]
            note(f"Segment that moved most: **{top[col]}** ({main} {float(top[f'{main}_change_pct']):+.1f}%)" +
                 "".join(f", {x} {top[x + '_before']} → {top[x + '_after']}" for x in drivers if x + "_before" in seg) + ".")
    ''', main=repr(main), drivers=repr(drivers), min_n=MIN_PERIODS_FOR_CORR)
    return Cell("drilldown", "Drill down: symptom next to driver", SECTIONS[6], story, tuple(statements), check)


def _cohort_cell(b: Builder) -> Cell:
    d = b.d
    cust, date = d.customer[1], d.date[1]
    per_year = {"quarter": 4, "month": 12, "year": 1}.get(d.period)
    unit = d.period

    def sql(D: Dialect) -> str:
        c, dt = D.ident(cust), D.ident(date)
        index = lambda y, n: f"{y} * {per_year} + {n}" if d.number_col else y  # noqa: E731
        names = ["cohort_year", "cohort_number", "cohort_index"][:len(d.period_cols)]
        cohort_cols = [f"{e} AS {n}" for e, n in zip(b.period_exprs(D, "first_date"), names)]
        act = ", ".join(d.period_cols)
        if unit == "week":
            since = "a.week_index - c.cohort_index"
        else:
            since = f"({index('a.' + d.year_col, 'a.' + (d.number_col or ''))}) - ({index('c.cohort_year', 'c.cohort_number')})"
        group = [f"c.{n}" for n in names]
        return fill('''
            WITH first_orders AS (
                SELECT <<c>>, MIN(<<dt>>) AS first_date
                FROM   order_facts
                WHERE  <<c>> IS NOT NULL AND <<dt>> IS NOT NULL
                GROUP  BY <<c>>
            ),
            cohorts AS (
                SELECT <<c>>,
                       <<cohort_cols>>
                FROM   first_orders
            ),
            activity AS (
                SELECT DISTINCT <<c>>, <<act>>
                FROM   order_facts
                WHERE  <<y>> IS NOT NULL
            )
            SELECT <<group>>,
                   <<since>> AS <<unit>>s_since_first,
                   COUNT(*) AS active_customers
            FROM   cohorts c
            JOIN   activity a ON a.<<c>> = c.<<c>>
            GROUP  BY <<group>>, <<since>>
            ORDER  BY <<group>>, <<unit>>s_since_first''', c=c, dt=dt, cohort_cols=",\n           ".join(cohort_cols), act=act,
                    y=d.year_col, group=", ".join(group), since=since, unit=unit)

    story = Step(
        "cohort",
        "Each customer has a first order date.",
        "Averages mix old and new customers. A cohort table follows each group of customers from their first order, so you can see whether newer "
        "customers come back as often as older ones did.",
        f"We group customers by the {unit} of their first order and count how many are active in each later {unit}, so that retention is measured per cohort.",
        f"`first_orders` finds each customer's first order (MIN), `cohorts` turns it into a {unit}, `activity` lists the {unit}s in which each customer "
        f"ordered, and the join counts active customers by cohort and by {unit}s since the first order ("
        + ("the difference of two week indexes" if unit == "week" else f"year × {per_year} + {unit} arithmetic") + ", which works in every dialect).",
    )
    check = fill('''
        coh = result.copy()
        cols = list(coh.columns)
        unit = <<unit>>
        coh["cohort"] = [period_label(unit, row[cols[0]], row[cols[1]] if unit != "year" else 0) for _, row in coh.iterrows()]
        grid = coh.pivot_table(index="cohort", columns=cols[-2], values="active_customers", aggfunc="sum").sort_index()
        size = grid[0]
        retention = grid.div(size, axis=0) * 100
        metrics["cohort_sizes"] = {str(k): int(v) for k, v in size.items()}
        plt.figure(figsize=(9, max(3, 0.4 * len(grid))))
        plt.imshow(retention.to_numpy(dtype=float), aspect="auto", cmap=C_SEQ, vmin=0, vmax=100)
        plt.colorbar(label="% of cohort active")
        plt.yticks(range(len(grid)), grid.index); plt.xlabel(cols[-2]); plt.title("Retention by cohort")
        plt.tight_layout(); plt.show()
        biggest = size.idxmax()
        note(f"Largest cohort: **{biggest}** with {int(size.max()):,} new customers. " + (
             f"A period after their first order, {retention[1].mean():.0f}% of a cohort is active again on average." if 1 in retention else ""))
    ''', unit=repr(unit))
    return Cell("cohort", f"Cohorts by first-order {unit}", SECTIONS[6], story, (stmt("cohort", f"Active customers by cohort and {unit}s since first order", sql),), check)


# ═════════════════════════════════════════════════════════════════════════
# 7 · Fast & correct
# ═════════════════════════════════════════════════════════════════════════
def _explain_cell(b: Builder) -> Cell:
    d = b.d
    statements = []
    if d.kpi and d.date:
        def target(D: Dialect) -> str:
            return D.explain(fill('''
                SELECT <<cols>>, SUM(revenue) AS revenue, COUNT(*) AS orders
                FROM   order_facts
                GROUP  BY <<cols>>''', cols=", ".join(d.period_cols)) if b.money else fill('''
                SELECT <<cols>>, COUNT(*) AS orders
                FROM   order_facts
                GROUP  BY <<cols>>''', cols=", ".join(d.period_cols)))
        what = f"the KPI-per-{d.period} query"
    elif d.schema.accepted:
        r = next((x for x in d.schema.accepted if x.kind != "self"), d.schema.accepted[0])

        def target(D: Dialect, r=r) -> str:
            return D.explain(fill('''
                SELECT COUNT(*)
                FROM   <<child>> c
                LEFT   JOIN <<parent>> p ON p.<<pk>> = c.<<fk>>
                WHERE  p.<<pk>> IS NULL''', child=D.ident(r.child_table), parent=D.ident(r.parent_table), pk=D.ident(r.parent_col),
                                            fk=D.ident(r.child_col)))
        what = f"the orphan check on `{r.child_table}`"
    else:
        t = next(iter(b.p.tables))

        def target(D: Dialect, t=t) -> str:
            return D.explain(f"SELECT COUNT(*) FROM {D.ident(t)}")
        what = f"a count of `{t}`"
    statements.append(stmt("plan", f"The plan for {what}", target))
    year = None
    if d.date and d.periods:
        counts: dict[int, int] = {}
        for p in d.periods:
            counts[p.year] = counts.get(p.year, 0) + p.rows
        year = max(counts, key=counts.get)
        table, col = d.date

        def sarg(D: Dialect, table=table, col=col, year=year) -> str:
            c, t = D.ident(col), D.ident(table)
            return fill('''
                SELECT (SELECT COUNT(*) FROM <<t>> WHERE <<yr>> = <<year>>) AS function_on_column,
                       (SELECT COUNT(*) FROM <<t>>
                        WHERE  <<c>> >= <<start>> AND <<c>> < <<end>>) AS sargable_range''',
                        t=t, yr=D.year(c), year=year, c=c, start=D.date_literal(f"{year}-01-01"), end=D.date_literal(f"{year + 1}-01-01"))
        statements.append(stmt("sargable", f"Same rows, two filters: YEAR({col}) = {year} vs a date range", sarg))
    if d.indexes:
        def indexes(D: Dialect) -> str:
            out = []
            for table, cols, why in d.indexes:
                name = f"idx_{table}_{'_'.join(cols)}"
                out.append(f"-- {why}\nCREATE INDEX {D.ident(name)} ON {D.ident(table)} ({', '.join(D.ident(c) for c in cols)})")
            head = ("-- InnoDB already indexes foreign key columns; these are listed for completeness.\n" if D.name == "mysql" else
                    "-- PostgreSQL does not index foreign key columns automatically.\n" if D.name == "postgres" else "")
            return head + ";\n".join(out)
        statements.append(stmt("indexes", "Indexes for the join and filter columns", indexes, run="show"))
    story = Step(
        "explain",
        f"The analysis joins on {len(d.schema.accepted)} key(s)" + (f" and filters on `{d.date[1]}`" if d.date else "") + ".",
        "On a real database, the same SQL can take milliseconds or minutes depending on indexes and on whether filters are *sargable* "
        "(able to use an index). EXPLAIN shows the plan before you wait for it.",
        "We read the plan, show that a function wrapped around a column returns the same rows as a range filter but blocks the index, and "
        "propose indexes, so that the queries stay fast on large tables.",
        "EXPLAIN prints the operators the engine will use (scans, joins, aggregations). "
        + (f"The second query counts the same {year} rows twice: YEAR({d.date[1]}) = {year} must compute YEAR() for every row, while the range "
           f"{d.date[1]} >= '{year}-01-01' AND < '{year + 1}-01-01' can seek an index. " if year else "")
        + "The CREATE INDEX statements are for your MySQL / PostgreSQL database; the in-memory engine here does not need them.",
    )
    check = fill('''
        import re as _re
        plan = outputs["plan"]
        text = "\\n".join(str(v) for v in plan.iloc[:, -1]) if len(plan.columns) else ""
        ops = [op.strip() for op in _re.findall(r"[╭┌]─+\\s*([A-Za-z_][A-Za-z_ ]*?)\\s*─", text)]
        metrics["plan_ops"] = ops
        if ops:
            note("Plan operators, top to bottom: " + " → ".join(dict.fromkeys(ops)) + ". Full scans are normal here: the query reads whole "
                 "tables to aggregate them. Indexes matter for selective lookups and joins on large tables.")
        if "sargable" in outputs:
            row = outputs["sargable"].iloc[0]
            same = int(row.function_on_column) == int(row.sargable_range)
            metrics["sargable_equal"] = same
            note(f"Both filters return **{int(row.sargable_range):,}** rows" + (" (identical)" if same else " (they differ: check time zones or timestamps)")
                 + ". Only the range version can use an index on the date column.")
    ''')
    return Cell("explain", "Read the plan; keep filters sargable", SECTIONS[7], story, tuple(statements), check)


def _null_traps_cell(b: Builder) -> Cell:
    d = b.d
    statements = []
    trap = d.null_trap.get("not_in")
    if trap:
        r, injected = trap

        def not_in(D: Dialect, r=r, injected=injected) -> str:
            parent, child = D.ident(r.parent_table), D.ident(r.child_table)
            pk, fk = D.ident(r.parent_col), D.ident(r.child_col)
            if r.type_mismatch:
                cmp_in, sub = D.as_text(f"p.{pk}"), D.as_text(f"c.{fk}")
                cond = f"{sub} = {cmp_in}"
            else:
                cmp_in, sub, cond = f"p.{pk}", f"c.{fk}", f"c.{fk} = p.{pk}"
            if injected:
                return fill('''
                    -- <<fk_name>> has no NULL today. One NULL arriving tomorrow is enough to break NOT IN:
                    SELECT (SELECT COUNT(*) FROM <<parent>> p
                            WHERE  <<cmp>> NOT IN (SELECT <<sub>> FROM <<child>> c)) AS not_in_today,
                           (SELECT COUNT(*) FROM <<parent>> p
                            WHERE  <<cmp>> NOT IN (SELECT <<sub>> FROM <<child>> c UNION ALL SELECT NULL)) AS not_in_after_one_null,
                           (SELECT COUNT(*) FROM <<parent>> p
                            WHERE  NOT EXISTS (SELECT 1 FROM <<child>> c WHERE <<cond>>)) AS not_exists''',
                            fk_name=f"{r.child_table}.{r.child_col}", parent=parent, child=child, cmp=cmp_in, sub=sub, cond=cond)
            return fill('''
                -- Which <<pname>> rows does no <<cname>>.<<fk_name>> point at? <<fk_name>> contains NULLs.
                SELECT (SELECT COUNT(*) FROM <<parent>> p
                        WHERE  <<cmp>> NOT IN (SELECT <<sub>> FROM <<child>> c)) AS using_not_in,
                       (SELECT COUNT(*) FROM <<parent>> p
                        WHERE  NOT EXISTS (SELECT 1 FROM <<child>> c WHERE <<cond>>)) AS using_not_exists''',
                        pname=r.parent_table, cname=r.child_table, fk_name=r.child_col, parent=parent, child=child, cmp=cmp_in, sub=sub, cond=cond)
        statements.append(stmt("not_in", "NOT IN vs NOT EXISTS when the sub-query can return NULL", not_in))
    count = d.null_trap.get("count")
    if count:
        table, col = count
        cp = b.p.col(table, col)

        def counts(D: Dialect, table=table, col=col, cp=cp) -> str:
            c = D.ident(col)
            extra = ""
            if d.lead and (table, col) == (d.lead[0], d.lead[2]):
                days = D.days_between(D.ident(d.lead[1]), c, _lead_is_timestamp(b))
                extra = f",\n       {D.round(f'AVG({days})', 2)} AS avg_{d.lead_name}_skips_nulls"
            elif cp.numeric:
                extra = f",\n       {D.round(f'AVG({c})', 2)} AS avg_skips_nulls,\n       {D.round(f'SUM({c}) * 1.0 / COUNT(*)', 2)} AS sum_over_all_rows"
            return fill('''
                SELECT COUNT(*) AS all_rows,
                       COUNT(<<c>>) AS rows_with_<<name>>,
                       COUNT(*) - COUNT(<<c>>) AS null_<<name>><<extra>>
                FROM   <<t>>''', c=c, name=re.sub(r"\W", "_", col), extra=extra, t=D.ident(table))
        statements.append(stmt("count_nulls", f"COUNT(*) vs COUNT({col}): aggregates skip NULLs", counts))
    def division(D: Dialect) -> str:
        return fill('''
            -- Integer ÷ integer truncates in PostgreSQL and SQL Server (MySQL's / and DuckDB's / are already decimal):
            SELECT 2 / 3 AS integer_division,           -- 0 in PostgreSQL, 0.6667 in MySQL
                   100.0 * 2 / 3 AS percentage          -- 66.67 everywhere''')
    statements.append(stmt("division", "Integer division (shown for your database; this engine never truncates)", division, run="show"))
    story = Step(
        "null_traps",
        "NULLs were found in the NULL profile" + (f", including in `{trap[0].child_table}.{trap[0].child_col}`" if trap and not trap[1] else "") + ".",
        "NULL means unknown, and SQL's three-valued logic makes `x NOT IN (…, NULL)` unknown for every row, so NOT IN silently returns nothing. "
        "COUNT(column) and AVG skip NULLs. Integer division truncates in some engines. None of these raise an error; they just give wrong numbers.",
        "We run each trap on your own data next to the safe version, so that the difference is visible and the safe habit sticks.",
        "The first query counts the same thing with NOT IN and with NOT EXISTS. NOT EXISTS checks for a matching row, so NULLs can't poison it. "
        "The second compares COUNT(*) with COUNT(column). The last block shows integer division for PostgreSQL; here it is not run because this engine never truncates.",
    )
    check = fill('''
        if not outputs:
            note("No foreign key and no nullable column here, so NOT IN and COUNT(column) can't go wrong on this data yet. "
                 "The integer-division trap above still applies in PostgreSQL.")
        if "not_in" in outputs:
            row = outputs["not_in"].iloc[0]
            values = {k: int(v) for k, v in row.items()}
            metrics["null_trap"] = values
            if "using_not_in" in values:
                note(f"**NOT IN returns {values['using_not_in']:,}, NOT EXISTS returns {values['using_not_exists']:,}.** "
                     "One NULL in the sub-query makes every NOT IN comparison unknown. Use NOT EXISTS (or filter out NULLs).")
            else:
                note(f"Today NOT IN finds {values['not_in_today']:,} row(s); after a single NULL arrives it finds **{values['not_in_after_one_null']:,}**, "
                     f"while NOT EXISTS keeps finding {values['not_exists']:,}. That is why NOT EXISTS is the safe habit.")
        if "count_nulls" in outputs:
            row = outputs["count_nulls"].iloc[0]
            metrics["count_trap"] = {k: (float(v) if v is not None else None) for k, v in row.items()}
            note(f"COUNT(*) = {int(row.iloc[0]):,} but COUNT(column) = {int(row.iloc[1]):,}: {int(row.iloc[2]):,} NULL(s) are skipped by COUNT, SUM and AVG.")
    ''')
    return Cell("null_traps", "NULL traps: NOT IN, COUNT(column), integer division", SECTIONS[7], story, tuple(statements), check)


def _names(items) -> str:
    items = list(items)
    return ", ".join(f"`{x}`" for x in items[:4]) + (f" and {len(items) - 4} more" if len(items) > 4 else "")


# ═════════════════════════════════════════════════════════════════════════
# Deeper analyses: discounts, moving average and outliers, concentration, purchase gaps
# ═════════════════════════════════════════════════════════════════════════
DISCOUNT_BANDS = ((0.05, "1-5%"), (0.10, "6-10%"), (0.20, "11-20%"))


def _discounts_cell(b: Builder) -> Cell:
    d, rev = b.d, b.d.revenue
    fact, fa = d.fact, b.alias(d.fact)

    def sql(D: Dialect) -> str:
        disc = D.col(fa, rev.discount) + (" / 100.0" if rev.discount_scale == 100 else "")
        gross = f"{D.col(fa, rev.price)} * {D.col(fa, rev.quantity)}"
        bands = "\n".join(f"                WHEN {disc} <= {limit} THEN {D.literal(label)}" for limit, label in DISCOUNT_BANDS)
        order = "\n".join(f"                WHEN {disc} <= {limit} THEN {i + 1}" for i, (limit, _) in enumerate(DISCOUNT_BANDS))
        last = f"over {int(DISCOUNT_BANDS[-1][0] * 100)}%"
        sources = "".join(c.strip() + ",\n" for c in b.source_ctes(D, [fact]))   # the rows the views use
        return fill('''
            -- Discount bands (the discount is a fraction of the line price); 'no discount' is its own band.
            WITH <<sources>>banded AS (
                SELECT CASE WHEN COALESCE(<<disc>>, 0) = 0 THEN 'no discount'
            <<bands>>
                            ELSE <<last>>
                       END AS discount_band,
                       CASE WHEN COALESCE(<<disc>>, 0) = 0 THEN 0
            <<order>>
                            ELSE <<n>>
                       END AS band_order,
                       <<gross>> AS gross_revenue,
                       <<net>> AS net_revenue,
                       <<qty>> AS quantity
                FROM   <<src>> <<fa>>
            )
            SELECT discount_band,
                   COUNT(*) AS line_rows,
                   <<pct_lines>> AS lines_pct,
                   <<g>> AS gross_revenue,
                   <<dg>> AS discount_given,
                   <<dpct>> AS discount_pct_of_gross,
                   <<avg_q>> AS avg_quantity_per_line
            FROM   banded
            GROUP  BY discount_band, band_order
            ORDER  BY band_order''', sources=sources, disc=disc, bands=bands, order=order, last=D.literal(last), n=len(DISCOUNT_BANDS) + 1,
                    gross=gross, net=b.line_revenue(D, fa), qty=D.col(fa, rev.quantity), src=b.src(D, fact), fa=fa,
                    pct_lines=D.pct("COUNT(*)", "SUM(COUNT(*)) OVER ()"), g=D.round("SUM(gross_revenue)", 2),
                    dg=D.round("SUM(gross_revenue - net_revenue)", 2), dpct=D.pct("SUM(gross_revenue - net_revenue)", "SUM(gross_revenue)"),
                    avg_q=D.ratio("SUM(quantity)", "COUNT(*)", 2))

    story = Step(
        "discounts",
        f"Revenue is net of `{rev.discount}`, so every discount is revenue given away.",
        "A discount is only worth it if it buys something: more units, bigger orders or a customer kept. Measuring what each discount level "
        "costs, and whether discounted lines are larger, is the first question of any pricing review.",
        "We band the lines by discount and compare what each band costs with the quantity it sells, so that discount policy is judged on evidence.",
        "A CASE expression puts each line in a discount band; a second CASE gives each band a sort order. Grouping by band adds up gross "
        "revenue and the discount given (gross − net); SUM(COUNT(*)) OVER () is a window over the grouped rows, so each band's share of lines "
        "needs no second query.",
    )
    check = fill('''
        bands = result.copy()
        total_gross = float(bands["gross_revenue"].sum())
        total_given = float(bands["discount_given"].sum())
        none = bands[bands["discount_band"] == "no discount"]
        some = bands[bands["discount_band"] != "no discount"]
        metrics["discounts"] = {
            "given": total_given, "gross": total_gross, "pct_of_gross": 100 * total_given / total_gross if total_gross else 0.0,
            "discounted_lines_pct": float(some["lines_pct"].sum()),
            "qty_discounted": float((some["avg_quantity_per_line"] * some["line_rows"]).sum() / some["line_rows"].sum()) if len(some) else None,
            "qty_full_price": float(none["avg_quantity_per_line"].iloc[0]) if len(none) else None,
        }
        plt.figure(figsize=(8, 3.2))
        plt.bar(bands["discount_band"], bands["discount_given"].astype(float), width=0.6, color=C_SERIES)
        plt.title("Revenue given away, by discount band", loc="left"); plt.tight_layout(); plt.show()
        dm = metrics["discounts"]
        dm["bands"] = [{"band": str(r.discount_band), "lines_pct": float(r.lines_pct), "given": float(r.discount_given),
                        "qty": float(r.avg_quantity_per_line)} for r in bands.itertuples()]
        note(f"Discounts gave away **{total_given:,.2f}** ({dm['pct_of_gross']:.1f}% of gross revenue) on {dm['discounted_lines_pct']:.1f}% of lines.")
        if dm["qty_discounted"] is not None and dm["qty_full_price"] is not None:
            lift = 100 * (dm["qty_discounted"] / dm["qty_full_price"] - 1) if dm["qty_full_price"] else float("nan")
            note(f"Discounted lines sell **{dm['qty_discounted']:.1f}** units on average vs {dm['qty_full_price']:.1f} at full price ({lift:+.0f}%). "
                 + ("Discounts go with bigger lines, though this alone doesn't show they cause them." if lift > 10 else
                    "Discounts don't come with noticeably bigger lines here: worth reviewing the policy."))
    ''')
    return Cell("discounts", "What discounts cost", SECTIONS[4], story, (stmt("discounts", "Discount bands: cost and quantity", sql),), check)


def _trend_signals_cell(b: Builder) -> Cell:
    d = b.d
    main = "revenue" if b.money else "orders"
    agg = "SUM(revenue)" if b.money else "COUNT(*)"
    partial = [b.period_where(p, "p.") for p in d.partial_periods]

    def sql(D: Dialect) -> str:
        z = D.round(f"(p.{main} - p.trailing_avg) / NULLIF(p.trailing_sd, 0)", 2)
        window = f"ORDER BY period_index RANGE BETWEEN {TREND_WINDOW} PRECEDING AND 1 PRECEDING"
        return fill('''
            -- RANGE frames count period_index values, not rows, so a missing period is skipped instead of pulling an older one in.
            -- Partial periods are shown but never enter a baseline: baseline_value is NULL for them, and AVG / STDDEV / COUNT skip NULLs.
            WITH <<cte>>,
            marked AS (
                SELECT p.*, <<baseline>> AS baseline_value
                FROM   per_period p
            ),
            framed AS (
                SELECT m.*,
                       AVG(<<main>>) OVER (ORDER BY period_index RANGE BETWEEN 2 PRECEDING AND CURRENT ROW) AS moving_avg_3,
                       AVG(baseline_value) OVER (<<window>>) AS trailing_avg,
                       STDDEV_SAMP(baseline_value) OVER (<<window>>) AS trailing_sd,
                       COUNT(baseline_value) OVER (<<window>>) AS baseline_periods
                FROM   marked m
            )
            SELECT <<cols>>,
                   <<main_r>> AS <<main>>,
                   <<ma>> AS moving_avg_3,
                   <<ta>> AS trailing_avg,
                   p.baseline_periods,
                   CASE WHEN p.baseline_periods >= <<min_base>> THEN <<z>> END AS z_score
            FROM   framed p
            ORDER  BY <<order>>''', cte=_period_ctes(b, D, [f"{agg} AS {main}"], "per_period").strip(), main=main, window=window,
                    baseline=f"CASE WHEN {' OR '.join(partial)} THEN NULL ELSE p.{main} END" if partial else f"p.{main}",
                    cols=", ".join(f"p.{c}" for c in d.period_cols), main_r=D.round(f"p.{main}", 2), ma=D.round("p.moving_avg_3", 2),
                    ta=D.round("p.trailing_avg", 2), z=z, min_base=TREND_MIN_BASELINE, order=", ".join(f"p.{c}" for c in d.period_cols))

    story = Step(
        "trend_signals",
        f"There are {len(d.periods)} {d.period}s of {main}.",
        "One period up or down proves little. A moving average shows the underlying direction, and comparing each period with the spread of the "
        "previous ones (a z-score) separates an unusual period from normal noise. A fixed limit such as |z| > 2 is wrong on short series: a "
        "spread measured on 4 points is so noisy that it is crossed by chance in about one period in six. Frames matter too: ROWS counts rows, "
        "RANGE counts values, so RANGE stays correct when a period is missing.",
        f"We add a 3-{d.period} moving average and flag a {d.period} only when it falls outside the 99% prediction interval of up to "
        f"{TREND_WINDOW} earlier complete {d.period}s (at least {TREND_MIN_BASELINE}), so that normal noise is flagged about once in a hundred "
        f"{d.period}s whatever the baseline length.",
        "AVG(…) OVER (ORDER BY period_index RANGE BETWEEN 2 PRECEDING AND CURRENT ROW) is the moving average. The window "
        f"'{TREND_WINDOW} PRECEDING AND 1 PRECEDING' over baseline_value (NULL for partial periods, which AVG, STDDEV_SAMP and COUNT skip) "
        "gives the trailing average, its spread and how many periods it rests on; the z-score is (value − trailing average) ÷ trailing "
        f"standard deviation, guarded with NULLIF and left NULL below {TREND_MIN_BASELINE} baseline periods. The check compares |z| with "
        "t(0.995, n − 1) × √(1 + 1/n) for a baseline of n periods (6.53 for 4, 4.36 for 6, 3.71 for 8).",
    )
    check = fill('''
        sig = result.copy()
        cols = list(sig.columns)
        n = <<n>>
        sig["period"] = [period_label(<<unit_name>>, row[cols[0]], row[cols[1]] if n > 1 else 0) for _, row in sig.iterrows()]
        partial = set(<<partial>>)
        sig["partial"] = sig["period"].isin(partial)
        z = sig["z_score"].astype(float)
        limits = <<limits>>   # 99% prediction interval: t(0.995, n - 1) * sqrt(1 + 1/n) for a baseline of n periods
        sig["z_limit"] = [limits.get(int(k)) if pd.notna(k) else None for k in sig["baseline_periods"]]
        level_flag = (z.abs() >= sig["z_limit"].astype(float)) & ~sig["partial"]
        # Trend test: a straight line through the same baseline (5+ complete periods) and its 99% prediction interval, so that a
        # steadily growing or shrinking series is not flagged just for continuing its trend
        index = [period_index(<<unit_name>>, row[cols[0]], row[cols[1]] if n > 1 else 0) for _, row in sig.iterrows()]
        values = sig["<<main>>"].astype(float).tolist()
        expected, trend_flag = [None] * len(sig), []
        for i in range(len(sig)):
            base = [(index[j], values[j]) for j in range(len(sig)) if not sig["partial"].iloc[j] and index[i] - <<window>> <= index[j] < index[i]]
            fit = trend_prediction([x for x, _ in base], [y for _, y in base], index[i]) if not sig["partial"].iloc[i] else None
            if fit:
                expected[i] = fit[0]
            trend_flag.append(fit is None or abs(values[i] - fit[0]) > fit[1])
        sig["trend_expected"] = expected
        unusual = sig[level_flag & pd.Series(trend_flag, index=sig.index)]
        metrics["unusual_periods"] = [{"period": r.period, "z": float(r.z_score), "value": float(r.<<main>>),
                                       "baseline": int(r.baseline_periods), "limit": float(r.z_limit),
                                       "expected": None if pd.isna(r.trend_expected) else float(r.trend_expected)} for r in unusual.itertuples()]
        fig, ax = plt.subplots(figsize=(10, 3.4))
        ax.bar(sig["period"], sig["<<main>>"].astype(float), width=0.6, color=[C_MUTED if p else C_SERIES for p in sig["partial"]])
        ax.plot(sig["period"], sig["moving_avg_3"].astype(float), color=C_TOTAL, marker="o", markersize=4)
        ax.set_title("<<main>> per <<unit>> (bars) and its 3-<<unit>> moving average (line)", loc="left")
        ax.tick_params(axis="x", labelrotation=45); plt.tight_layout(); plt.show()
        last = sig[~sig["partial"]].iloc[-1]
        judged = int(z[~sig["partial"]].notna().sum())
        note(f"Moving average at the last complete <<unit>> ({last.period}): **{float(last.moving_avg_3):,.2f}**.")
        dropped = int((level_flag & ~pd.Series(trend_flag, index=sig.index)).sum())
        if len(unusual):
            note(f"Unusual <<unit>>s (outside the 99% prediction intervals of both the level and the trend of up to <<window>> earlier "
                 f"complete <<unit>>s): " + ", ".join(
                     f"**{r.period}** (z = {r.z_score:+.1f} against a limit of {r.z_limit:.2f}"
                     + (f"; the trend predicted {r.trend_expected:,.2f}" if pd.notna(r.trend_expected) else "") + ")" for r in unusual.itertuples())
                 + ". Look for a one-off event (a big order, a promotion, a loading problem) before reading a trend into it.")
        elif judged:
            note(f"None of the {judged} <<unit>>s with a baseline of <<min_base>>+ earlier periods falls outside its 99% prediction interval: "
                 "the movement is within normal noise.")
        else:
            note(f"No <<unit>> has <<min_base>> earlier complete <<unit>>s yet, so no z-score is computed; the moving average still shows the direction.")
        if dropped:
            note(f"{dropped} <<unit>>(s) stood out from the recent average but follow the recent trend, so they are not flagged.")
    ''', n=len(d.period_cols), unit_name=repr(d.period), partial=repr([d.label(p) for p in d.partial_periods]), main=main,
        unit=d.period, limits=repr(TREND_Z_LIMIT), window=TREND_WINDOW, min_base=TREND_MIN_BASELINE)
    return Cell("trend_signals", "Moving average and unusual periods", SECTIONS[5], story,
                (stmt("trend_signals", f"Moving average and z-score per {d.period} (RANGE frames)", sql),), check)


def _pareto_cell(b: Builder) -> Cell:
    d = b.d
    entities = [("customers", "order_facts", d.customer[1])]
    if d.item and d.item[2].level == "line":
        entities.append(("items", "line_facts", d.item[1]))

    def ranked(D: Dialect, view: str, key: str) -> str:
        k = D.ident(key)
        return fill('''
            entity_revenue AS (
                SELECT <<k>>, SUM(revenue) AS revenue
                FROM   <<view>>
                WHERE  <<k>> IS NOT NULL
                GROUP  BY <<k>>
            ),
            ranked AS (
                SELECT e.*,
                       ROW_NUMBER() OVER (ORDER BY revenue DESC, <<k>>) AS revenue_rank,
                       COUNT(*) OVER () AS entities,
                       SUM(revenue) OVER (ORDER BY revenue DESC, <<k>> ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_revenue,
                       SUM(revenue) OVER () AS total_revenue
                FROM   entity_revenue e
            ),
            classed AS (
                SELECT r.*,
                       CASE WHEN running_revenue - revenue < 0.80 * total_revenue THEN 'A'
                            WHEN running_revenue - revenue < 0.95 * total_revenue THEN 'B'
                            ELSE 'C'
                       END AS abc_class
                FROM   ranked r
            )''', k=k, view=view)

    def curve(D: Dialect, view: str, key: str) -> str:
        return ("-- ABC: an entity is A while the revenue before it is under 80% of the total, B under 95%, C after that.\nWITH "
                + ranked(D, view, key).strip() + "\n" + fill('''
            SELECT <<k>>,
                   revenue_rank,
                   <<rev>> AS revenue,
                   <<share>> AS entity_pct,
                   <<cum>> AS cumulative_revenue_pct,
                   abc_class
            FROM   classed
            ORDER  BY revenue_rank''', k=D.ident(key), rev=D.round("revenue", 2), share=D.pct("revenue_rank", "entities"),
                    cum=D.pct("running_revenue", "total_revenue")))

    def classes(D: Dialect, view: str, key: str) -> str:
        return "WITH " + ranked(D, view, key).strip() + "\n" + fill('''
            SELECT abc_class,
                   COUNT(*) AS entities,
                   <<share_e>> AS entities_pct,
                   <<rev>> AS revenue,
                   <<share_r>> AS revenue_pct
            FROM   classed
            GROUP  BY abc_class
            ORDER  BY abc_class''', share_e=D.pct("COUNT(*)", "SUM(COUNT(*)) OVER ()"), rev=D.round("SUM(revenue)", 2),
                    share_r=D.pct("SUM(revenue)", "SUM(SUM(revenue)) OVER ()"))

    statements = []
    for name, view, key in entities:
        statements.append(stmt(f"curve_{name}", f"Cumulative revenue share of {name} (`{key}`), largest first", lambda D, v=view, k=key: curve(D, v, k)))
        statements.append(stmt(f"abc_{name}", f"ABC classes of {name}", lambda D, v=view, k=key: classes(D, v, k)))
    story = Step(
        "pareto",
        "Revenue is known per customer" + (" and per item" if len(entities) > 1 else "") + ".",
        "Revenue is rarely spread evenly: often about 20% of customers bring about 80% of it (the Pareto pattern). How concentrated it is decides "
        "where account management, stock and service effort should go, and how exposed the business is if a few large customers leave.",
        "We rank " + " and ".join(name for name, _, _ in entities) + " by revenue, add a running share with a window SUM, and class them A / B / C, "
        "so that concentration is a number, not an impression.",
        "SUM(revenue) OVER (ORDER BY revenue DESC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) is the running total from the biggest down; "
        "dividing by SUM(revenue) OVER () gives the cumulative share. ABC classes come from a CASE on the share *before* each entity, so the "
        "entity that crosses 80% is still A. The class summary uses SUM(SUM(revenue)) OVER (): a window over grouped rows.",
    )
    check = fill('''
        import numpy as _np
        for name in <<names>>:
            cur = outputs[f"curve_{name}"]
            x = cur["entity_pct"].astype(float).to_numpy()
            y = cur["cumulative_revenue_pct"].astype(float).to_numpy()
            top20 = float(_np.interp(20, _np.r_[0, x], _np.r_[0, y]))
            cls = outputs[f"abc_{name}"].set_index("abc_class")
            a = cls.loc["A"] if "A" in cls.index else None
            metrics.setdefault("pareto", {})[name] = {
                "entities": int(len(cur)), "top20_revenue_pct": top20,
                "a_entities": int(a["entities"]) if a is not None else 0, "a_entities_pct": float(a["entities_pct"]) if a is not None else 0.0,
                "a_revenue_pct": float(a["revenue_pct"]) if a is not None else 0.0,
            }
            p = metrics["pareto"][name]
            step = max(1, len(x) // 60)                       # up to ~60 points for the curve in the business brief
            p["curve"] = [(0.0, 0.0)] + [(float(a), float(b)) for a, b in zip(x[::step], y[::step])] + [(float(x[-1]), float(y[-1]))]
            note(f"The top 20% of {name} bring **{top20:.0f}%** of revenue. Class A ({p['a_entities']:,} {name}, {p['a_entities_pct']:.0f}% of them) "
                 f"brings {p['a_revenue_pct']:.0f}%.")
        first = <<names>>[0]
        cur = outputs[f"curve_{first}"]
        plt.figure(figsize=(6.5, 4))
        plt.plot(_np.r_[0, cur["entity_pct"].astype(float)], _np.r_[0, cur["cumulative_revenue_pct"].astype(float)], color=C_SERIES)
        plt.plot([0, 100], [0, 100], color=C_MUTED, linewidth=1)
        plt.axhline(80, color=C_MUTED, linewidth=1); plt.axvline(20, color=C_MUTED, linewidth=1)
        plt.xlabel(f"% of {first}, largest first"); plt.ylabel("% of revenue")
        plt.title(f"Revenue concentration across {first}", loc="left"); plt.tight_layout(); plt.show()
    ''', names=repr([n for n, _, _ in entities]))
    return Cell("pareto", "Revenue concentration (Pareto / ABC)", SECTIONS[5], story, tuple(statements), check)


def _purchase_gaps_cell(b: Builder) -> Cell:
    d = b.d
    cust, date = d.customer[1], d.date[1]

    def ordered(D: Dialect) -> str:
        c, dt, ok = D.ident(cust), D.ident(date), D.ident(b.order_key)
        rev = ", revenue" if b.money else ""
        return fill('''
            customer_orders AS (
                SELECT <<c>>, <<ok>>, <<dt>><<rev>>,
                       LAG(<<dt>>) OVER (PARTITION BY <<c>> ORDER BY <<dt>>, <<ok>>) AS previous_order_date,
                       LEAD(<<dt>>) OVER (PARTITION BY <<c>> ORDER BY <<dt>>, <<ok>>) AS next_order_date
                FROM   order_facts
                WHERE  <<c>> IS NOT NULL
                  AND  <<dt>> IS NOT NULL
            )''', c=c, dt=dt, ok=ok, rev=rev)

    def gaps(D: Dialect) -> str:
        days = D.days_between("previous_order_date", D.ident(date), d.date_is_timestamp)
        return "WITH " + ordered(D).strip() + ",\n" + fill('''
            gaps AS (
                SELECT <<days>> AS days_since_previous
                FROM   customer_orders
                WHERE  previous_order_date IS NOT NULL      -- a customer's first order has no previous one
            )
            SELECT COUNT(*) AS repeat_orders,
                   <<avg>> AS avg_days_between_orders,
                   MIN(days_since_previous) AS min_days,
                   MAX(days_since_previous) AS max_days,
                   <<w30>> AS within_30_days_pct,
                   <<w90>> AS within_90_days_pct
            FROM   gaps''', days=days, avg=D.round("AVG(days_since_previous)", 1),
                    w30=D.pct("SUM(CASE WHEN days_since_previous <= 30 THEN 1 ELSE 0 END)", "COUNT(*)"),
                    w90=D.pct("SUM(CASE WHEN days_since_previous <= 90 THEN 1 ELSE 0 END)", "COUNT(*)"))

    def overdue(D: Dialect) -> str:
        c, dt = D.ident(cust), D.ident(date)
        gap = D.days_between("previous_order_date", dt, d.date_is_timestamp)
        since = D.days_between("last_order_date", f"(SELECT MAX({dt}) FROM order_facts)", d.date_is_timestamp)
        rev = ",\n           SUM(revenue) AS revenue" if b.money else ""
        rev_out = f",\n       {D.round('revenue', 2)} AS revenue" if b.money else ""
        order = "revenue DESC" if b.money else "orders DESC"
        return "WITH " + ordered(D).strip() + ",\n" + fill('''
            per_customer AS (
                SELECT <<c>>,
                       COUNT(*) AS orders,
                       MAX(<<dt>>) AS last_order_date,
                       AVG(<<gap>>) AS typical_gap_days<<rev>>    -- AVG skips the first order's NULL gap
                FROM   customer_orders
                GROUP  BY <<c>>
            ),
            scored AS (
                SELECT p.*,
                       <<since>> AS days_since_last_order
                FROM   per_customer p
                WHERE  orders >= 3                       -- at least two gaps, so 'typical' means something
            )
            SELECT <<c>>,
                   orders,
                   last_order_date,
                   <<typ>> AS typical_gap_days,
                   days_since_last_order,
                   CASE WHEN days_since_last_order > 2 * typical_gap_days THEN 'overdue' ELSE 'on track' END AS status<<rev_out>>
            FROM   scored
            ORDER  BY CASE WHEN days_since_last_order > 2 * typical_gap_days THEN 0 ELSE 1 END, <<order>>, <<c>>''',
                    c=c, dt=dt, gap=gap, rev=rev, since=since, typ=D.round("typical_gap_days", 0), rev_out=rev_out, order=order)

    story = Step(
        "purchase_gaps",
        f"Customers order repeatedly (`{cust}` repeats in order_facts).",
        "How long customers take to come back is the rhythm of the business. A customer who has been silent for more than twice their own usual gap "
        "is the earliest sign of churn, long before they are 'lost' by any fixed rule like 180 days.",
        "We measure the days between consecutive orders with LAG and list customers who are overdue against their own rhythm, so that a "
        "win-back list exists before they are gone.",
        f"LAG({date}) OVER (PARTITION BY {cust} ORDER BY {date}, {b.order_key}) is each customer's previous order date (LEAD is the next one); the "
        "difference is the gap. Per customer, AVG of the gaps is their typical rhythm (the first order's NULL gap is skipped by AVG); a customer "
        "is overdue when the days since their last order exceed twice that rhythm. Only customers with 3+ orders are judged.",
    )
    check = fill('''
        g = outputs["gaps"].iloc[0]
        od = outputs["overdue"]
        late = od[od["status"] == "overdue"]
        metrics["purchase_gaps"] = {"avg_days": float(g.avg_days_between_orders) if g.avg_days_between_orders is not None else None,
                                    "within_90_pct": float(g.within_90_days_pct or 0), "judged": int(len(od)), "overdue": int(len(late))}
        if "revenue" in od and len(od):
            metrics["purchase_gaps"]["overdue_revenue_pct"] = 100 * float(late["revenue"].sum()) / float(od["revenue"].sum())
        note(f"Repeat orders come **{float(g.avg_days_between_orders):.0f} days** apart on average; {float(g.within_90_days_pct):.0f}% within 90 days.")
        if len(od):
            share = metrics["purchase_gaps"].get("overdue_revenue_pct")
            note(f"**{len(late):,} of {len(od):,}** repeat customers are overdue (silent for more than twice their usual gap)"
                 + (f"; they brought {share:.0f}% of these customers' revenue: the win-back list." if share is not None else "."))
    ''')
    return Cell("purchase_gaps", "Time between orders, and overdue customers", SECTIONS[5], story,
                (stmt("gaps", "Days between consecutive orders (LAG / LEAD)", gaps),
                 stmt("overdue", "Customers silent for more than twice their usual gap", overdue)), check)


# ═════════════════════════════════════════════════════════════════════════
# Public API
# ═════════════════════════════════════════════════════════════════════════
BUILDERS: dict[str, Callable[[Builder], Cell]] = {
    "schema": _schema_cell, "grain": _grain_cell, "cardinality": _cardinality_cell,
    "duplicates": _duplicates_cell, "nulls": _nulls_cell, "date_range": _date_range_cell, "orphans": _orphans_cell,
    "base": _base_cell, "reconcile": _reconcile_cell,
    "kpis": _kpis_cell, "discounts": _discounts_cell, "kpi_by_period": _kpi_by_period_cell,
    "running_total": _running_total_cell, "trend_signals": _trend_signals_cell, "top_n": _top_n_cell, "pareto": _pareto_cell,
    "latest_record": _latest_record_cell, "purchase_gaps": _purchase_gaps_cell, "rfm": _rfm_cell,
    "driver_tree": _driver_tree_cell, "drilldown": _drilldown_cell, "cohort": _cohort_cell,
    "explain": _explain_cell, "null_traps": _null_traps_cell,
}
ORDER = tuple(BUILDERS)


def build_cells(d: Decision) -> list[Cell]:
    if d.halted:
        return []
    b = Builder(d)
    return [BUILDERS[cid](b) for cid in ORDER if cid not in d.skipped]


def assumptions(d: Decision) -> list[str]:
    """Everything a reader must know to trust the numbers, in plain words (app step 4 and the .sql header)."""
    if not d.kpi:
        return ["Validation only: no KPI was computed."]
    b = Builder(d)
    out = [f"Fact table: `{d.fact}` ({d.profile[d.fact].grain})."]
    out.append(f"Revenue = {b.revenue_formula()}" + (" (net of discount; gross is shown in the reconciliation)." if d.revenue.discount else "."))
    if d.revenue.price_rel:
        out.append(f"The price comes from `{d.revenue.price_rel.parent_table}` (a list price), not from the sale itself.")
    money_col = d.revenue.amount or d.revenue.price
    if money_col:
        table = d.revenue.price_rel.parent_table if d.revenue.price_rel and not d.revenue.amount else d.revenue.table
        unit = d.profile.col(table, money_col).unit
        if unit and unit not in ("%", "mixed"):
            out.append(f"Amounts are in {unit}, as written next to the numbers in `{money_col}`.")
    if d.line_level:
        out.append(f"`order_facts` has one row per order (`{d.order_key}`); `line_facts` one row per line. AOV = revenue ÷ distinct orders.")
    if d.date:
        out.append(f"Periods are {'ISO weeks (Monday to Sunday)' if d.period == 'week' else d.period + 's'} of `{d.date[0]}.{d.date[1]}`.")
    for p in d.partial_periods:
        out.append(f"{d.label(p)} is partial ({p.coverage:.0%} of its days have data) and is excluded from period-over-period verdicts.")
    if d.lead:
        out.append(f"{d.lead_name} = {d.lead[2]} − {d.lead[1]}, in days; orders without `{d.lead[2]}` are left out of its averages.")
    if d.rating and d.rating.scale:
        out.append("rating_score maps " + ", ".join(f"'{label}' = {score}" for label, score in d.rating.scale) + ".")
    if d.customer:
        out.append("Recency is measured to the last date in the data, not to today.")
    for t, (keys, order) in d.dedupe.items():
        out.append(f"`{t}` keeps one row per {', '.join(keys)} ({'latest by ' + order if order else 'an arbitrary copy'}).")
    for f in d.row_filters:
        if f.kind == "currency":
            out.append(f"Only amounts in {f.kept} are counted: {f.rows:,} row(s) of `{f.table}` in {f.shown()} are left out; "
                       f"rows with no `{f.column}` are assumed to be {f.kept}.")
        else:
            whole = " (with all their lines)" if f.table == d.header and d.line_level else ""
            out.append(f"Rows where `{f.table}.{f.column}` is {f.shown()} are not sales and are left out{whole}: {f.rows:,} of {f.total:,}.")
    if d.dimensions:
        out.append("Dimensions are joined with LEFT JOIN; a missing match is labelled 'Unknown'.")
    return out
