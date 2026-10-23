"""The decision engine.

Reads the profile and the schema, plus the user's focus, and decides step by step:

  Stage 0  gates        - is there any data at all?
  Stage 1  grain        - what is each table's key, and which foreign keys can be trusted?
  Stage 2  fact         - which table records the sales, and at what grain (lines or orders)?
  Stage 3  revenue      - price × quantity (× (1 - discount)), an amount column, or row counts
  Stage 4  time         - which date, which period (month / quarter / year), which periods are partial?
  Stage 5  drivers      - shipping time, late shipments, rating; the customer; the dimensions to slice by
  Stage 6  correctness  - fan-out, duplicates to drop, NULL traps, indexes; which cells to skip

Every choice is recorded as a narrative ``Step`` so the UI can explain it.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

import pandas as pd

from .loading import Catalog
from .narrative import Step, story
from .profiling import CatalogProfile, ColumnProfile, TableProfile, singular
from .schema import FanOut, Relationship, Schema

MAX_HOPS = 2                  # dimensions are looked for at most two joins away from the order
MAX_DIM_NULL = 0.30           # a dimension may be empty in at most 30% of rows
MAX_DIMENSIONS = 3
PERIOD_COVERAGE = 0.90        # a period with data on < 90% of its days is partial
MIN_COMPLETE_QUARTERS = 4     # fewer complete quarters -> months
YEARS_FOR_YEARLY = 5          # more than 5 years of data -> years
MIN_LEAD_SHARE = 0.90         # a "ship" date must be on/after the order date in >= 90% of rows
SMALL_TABLE = 100             # largest table smaller than this -> warn that KPIs are fragile
TOP_N = 3
NTILE_BUCKETS = 5
MIN_PERIODS_FOR_CORR = 6      # a correlation across fewer periods is noise

ORDER_KEY = re.compile(r"(^|_)(order|invoice|transaction|txn|receipt|basket|booking|sale|purchase)_?(id|key|no|num|number|code)$", re.I)
CUSTOMER_KEY = re.compile(r"(^|_)(customer|client|user|account|buyer|member|shopper|guest|patient)_?(id|key|no|num|number|code)?$", re.I)
ORDER_DATE = re.compile(r"(order|purchase|invoice|transaction|created|booking|sale|placed|date$|^date)", re.I)
SHIP = re.compile(r"ship|dispatch|deliver|fulfil|arriv|receiv|sent", re.I)
REQUIRED = re.compile(r"required|due|promised|expected|sla|deadline", re.I)
GROUPS: tuple[tuple[str, re.Pattern], ...] = (
    ("geography", re.compile(r"country|state|region|city|territory|zone|market|province|county|continent", re.I)),
    ("product", re.compile(r"category|segment|type|class|brand|line|department|family|maker|make|model|genre|group", re.I)),
    ("channel", re.compile(r"channel|source|status|tier|plan|payment|card|method|gender|age_band|device|platform", re.I)),
    ("fulfilment", re.compile(r"shipper|carrier|warehouse|courier|ship_mode|shipping_mode|ship_via", re.I)),
    ("people", re.compile(r"employee|salesperson|seller|staff|rep(_|$)|agent|manager", re.I)),
)
LABEL_NAME = re.compile(r"(^|_)(name|title|label)$", re.I)


@dataclass(frozen=True)
class Dimension:
    table: str
    column: str
    alias: str                                  # column name in the views
    group: str                                  # geography | product | channel | fulfilment | people | other
    path: tuple[Relationship, ...]              # joins from the fact (or header) table
    level: str                                  # "order": one value per order; "line": varies within an order
    n_values: int


@dataclass(frozen=True)
class Period:
    year: int
    number: int                                 # quarter 1-4 or month 1-12; 0 for whole years
    start: dt.date
    end: dt.date
    first_seen: dt.date
    last_seen: dt.date
    rows: int

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def covered(self) -> int:
        return (min(self.last_seen, self.end) - max(self.first_seen, self.start)).days + 1

    @property
    def coverage(self) -> float:
        return self.covered / self.days

    @property
    def complete(self) -> bool:
        return self.coverage >= PERIOD_COVERAGE

    def label(self, unit: str) -> str:
        return {"quarter": f"{self.year}-Q{self.number}", "month": f"{self.year}-{self.number:02d}"}.get(unit, str(self.year))


@dataclass
class Revenue:
    kind: str                                   # price_qty | amount | count
    table: str
    price: str | None = None
    quantity: str | None = None
    discount: str | None = None
    discount_scale: int = 1                     # 100 when the discount is a percentage
    amount: str | None = None
    price_rel: Relationship | None = None       # set when the price comes from a parent table (list price)


@dataclass
class Decision:
    mode: str                                   # auto | manual | validate
    analysis: str = "halt"                      # kpi | validate | halt
    profile: CatalogProfile | None = None
    schema: Schema | None = None
    fact: str | None = None
    line_level: bool = False
    header: str | None = None                   # table holding order-level columns when the fact has one row per line
    header_rel: Relationship | None = None
    order_key: str | None = None                # column of the fact table
    customer: tuple[str, str] | None = None     # (table, column): table is the fact or the header
    customer_rel: Relationship | None = None    # link to the customers table, when there is one
    revenue: Revenue | None = None
    date: tuple[str, str] | None = None         # (table, column): fact or header
    date_is_timestamp: bool = False
    period: str | None = None                   # quarter | month | year
    multi_year: bool = False
    periods: list[Period] = field(default_factory=list)
    lead: tuple[str, str, str] | None = None    # (table, start column, end column)
    lead_name: str = "ship_days"
    required: str | None = None                 # column in the lead table
    rating: ColumnProfile | None = None
    dimensions: list[Dimension] = field(default_factory=list)
    item: tuple[str, str, Dimension] | None = None   # top-N: (table, label column, group dimension)
    fanouts: list[FanOut] = field(default_factory=list)
    dedupe: dict[str, tuple[tuple[str, ...], str | None]] = field(default_factory=dict)
    indexes: list[tuple[str, tuple[str, ...], str]] = field(default_factory=list)
    null_trap: dict = field(default_factory=dict)
    date_parts: list[tuple[str, str, str]] = field(default_factory=list)  # (table, column, unit) stored next to the date
    skipped: dict[str, str] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)

    @property
    def halted(self) -> bool:
        return self.analysis == "halt"

    @property
    def kpi(self) -> bool:
        return self.analysis == "kpi"

    @property
    def complete_periods(self) -> list[Period]:
        return [p for p in self.periods if p.complete]

    @property
    def partial_periods(self) -> list[Period]:
        return [p for p in self.periods if not p.complete]

    @property
    def compare(self) -> tuple[Period, Period] | None:
        """The last two complete periods, when they are consecutive."""
        done = self.complete_periods
        if len(done) >= 2 and self.periods.index(done[-1]) - self.periods.index(done[-2]) == 1:
            return done[-2], done[-1]
        return None

    @property
    def yoy(self) -> tuple[Period, Period] | None:
        """The last complete period and the same period a year earlier (quarters and months only)."""
        done = self.complete_periods
        if self.period not in ("quarter", "month") or not done:
            return None
        last = done[-1]
        earlier = next((p for p in done if p.year == last.year - 1 and p.number == last.number), None)
        return (earlier, last) if earlier else None

    @property
    def year_col(self) -> str:
        stem = re.sub(r"(_date|_at|_on|_time|_timestamp|_dt)$", "", self.date[1]) if self.date else "period"
        return f"{stem}_year" if stem not in ("date", self.date[1] if self.date else "") else "period_year"

    @property
    def number_col(self) -> str | None:
        return {"quarter": "quarter_number", "month": "month_number"}.get(self.period or "")

    @property
    def period_cols(self) -> list[str]:
        return [self.year_col] + ([self.number_col] if self.number_col else [])

    def label(self, p: Period) -> str:
        return p.label(self.period or "year")


TARGET_MODES = FOCUS_MODES = ("auto", "manual", "validate")


# ── Helpers ──────────────────────────────────────────────────────────────
def _names(items, limit: int = 4) -> str:
    items = list(items)
    text = ", ".join(f"`{x}`" for x in items[:limit])
    return text + (f" and {len(items) - limit} more" if len(items) > limit else "")


def label_column(t: TableProfile) -> str | None:
    """The column that names a row in words: <table>_name, name, title, … else None."""
    texts = [c for c in t.columns if c.logical_type == "text" and c.role in ("dimension", "attribute", "identifier") and c.n_null < c.n_rows]
    stem = singular(t.name)
    by_name = {c.name: c for c in texts}
    for name in (f"{stem}_name", "name", stem, "company_name", "full_name", "last_name", f"{stem}_title", "title"):
        if name in by_name:
            return name
    named = [c for c in texts if LABEL_NAME.search(c.name) or "name" in c.name]
    if named:
        return max(named, key=lambda c: c.n_unique).name
    return None


def compute_periods(dates: pd.Series, unit: str) -> list[Period]:
    dates = pd.to_datetime(dates.dropna())
    if dates.empty:
        return []
    frame = pd.DataFrame({"d": dates, "y": dates.dt.year})
    if unit == "quarter":
        frame["n"] = dates.dt.quarter
    elif unit == "month":
        frame["n"] = dates.dt.month
    else:
        frame["n"] = 0
    out = []
    for (y, n), g in frame.groupby(["y", "n"]):
        y, n = int(y), int(n)
        if unit == "quarter":
            start = dt.date(y, 3 * n - 2, 1)
            end = (pd.Timestamp(start) + pd.offsets.QuarterEnd(0)).date()
        elif unit == "month":
            start = dt.date(y, n, 1)
            end = (pd.Timestamp(start) + pd.offsets.MonthEnd(0)).date()
        else:
            start, end = dt.date(y, 1, 1), dt.date(y, 12, 31)
        out.append(Period(y, n, start, end, g["d"].min().date(), g["d"].max().date(), len(g)))
    # only the first and last periods can be cut off by the extract; inner ones are complete by definition
    for i, p in enumerate(out):
        if 0 < i < len(out) - 1:
            out[i] = Period(p.year, p.number, p.start, p.end, p.start, p.end, p.rows)
    return out


def _halt(d: Decision, key: str, **values) -> Decision:
    d.analysis = "halt"
    d.steps.append(story(key, **values))
    return d


# ── Engine ───────────────────────────────────────────────────────────────
def decide(catalog: Catalog, profile: CatalogProfile, schema: Schema, mode: str = "auto", picks: dict | None = None) -> Decision:
    """``picks`` (manual mode): fact, measure (price_qty | amount | count), price, quantity, discount, amount,
    date ("table.column"), order_key, customer ("table.column"), dimensions (["table.column", ...])."""
    if mode not in FOCUS_MODES:
        raise ValueError(f"mode must be one of {FOCUS_MODES}, not {mode!r}")
    picks = picks or {}
    d = Decision(mode=mode, profile=profile, schema=schema)
    tables = profile.tables

    # ── Stage 0: gates ────────────────────────────────────────────────────
    d.steps.append(story("overview", n_tables=len(tables), rows=profile.total_rows, names=_names(tables, 6)))
    for source, reason in catalog.skipped:
        d.steps.append(story("skipped_file", source=source, reason=reason))
    if not tables or profile.total_rows == 0:
        return _halt(d, "halt_empty")
    renamed = [(t.name, new, old) for t in tables.values() for new, old in t.renamed]
    if renamed:
        d.steps.append(story("renamed", n=len(renamed), examples=", ".join(f"`{old}` → `{new}`" for _, new, old in renamed[:3])))
    for t in catalog.tables.values():
        for col, n in t.unparsed_dates.items():
            d.steps.append(story("unparsed_dates", table=t.name, col=col, n=n, fmt=t.date_formats.get(col, "")))
        for col in t.ambiguous_dates:
            d.steps.append(story("ambiguous_dates", table=t.name, col=col))
    biggest = max(t.n_rows for t in tables.values())
    if biggest < SMALL_TABLE:
        d.steps.append(story("small_tables", rows=biggest))

    # ── Stage 1: grain and keys ───────────────────────────────────────────
    for t in tables.values():
        if t.n_rows == 0:
            continue
        if t.key_status == "single":
            d.steps.append(story("grain_single", table=t.name, rows=t.n_rows, reason=t.key_reason, grain=t.grain, key=t.primary_key[0]))
        elif t.key_status == "composite":
            first, second = t.primary_key
            noun = "line" if ORDER_KEY.search(first) else f"`{first}` × `{second}` combination"
            d.steps.append(story("grain_composite", table=t.name, rows=t.n_rows, reason=t.key_reason, grain=t.grain,
                                 grain_noun=noun, first=first, second=second))
        elif t.key_status == "intended":
            keep = _dedupe_order(t)
            nulls = f", and {t.null_keys:,} row(s) have none" if t.null_keys else ""
            d.steps.append(story("grain_intended", table=t.name, key=t.primary_key[0], dups=t.duplicate_keys, extra=t.extra_rows,
                                 nulls=nulls, keep=f"latest by `{keep}`" if keep else "first one found"))
        else:
            d.steps.append(story("grain_none", table=t.name))
    for r in schema.relationships:
        values = dict(rel=r.describe(), reason=r.reason, child=r.child_table, parent=r.parent_table,
                      max_children=r.max_children, avg=r.avg_children, orphans=r.orphan_rows)
        if r.kind == "self":
            d.steps.append(story("fk_self", **values))
        elif r.accepted:
            d.steps.append(story("fk_name", **values))
        elif r.kind == "value":
            d.steps.append(story("fk_suggested", **values))
        else:
            d.steps.append(story("fk_low", **values))
        if r.type_mismatch:
            d.steps.append(story("type_mismatch", rel=r.describe(), child_type=profile.col(r.child_table, r.child_col).logical_type,
                                 parent_type=profile.col(r.parent_table, r.parent_col).logical_type))
    d.fanouts = list(schema.fanouts)
    for f in d.fanouts[:2]:
        rel = next(r for r in schema.accepted if r.parent_table == f.parent_table and r.child_table == f.child_table and r.child_col == f.via)
        d.steps.append(story("fanout", parent=f.parent_table, col=f.column, child=f.child_table, max_children=rel.max_children,
                             joined=f.joined_total, total=f.total, factor=f.factor))

    for t in tables.values():
        if t.key_status == "intended":
            d.dedupe[t.name] = (t.primary_key, _dedupe_order(t))
    d.date_parts = [(t.name, c.name, _part_unit(c.name)) for t in tables.values() for c in t.by_role("date_part")
                    if t.by_role("datetime") and _part_unit(c.name)]

    if mode == "validate":
        _choose_null_trap(d)
        _choose_indexes(d)
        d.analysis = "validate"
        d.steps.insert(1, story("focus_validate"))
        _skip_kpi_cells(d, "You chose to validate the data only.")
        return d

    # ── Stage 2: fact table and grain ─────────────────────────────────────
    d.steps.append(story("focus_auto") if mode == "auto" else story("focus_manual", hint=_manual_hint(picks)))
    fact = picks.get("fact") or _choose_fact(profile, schema)
    if fact not in tables:
        return _halt(d, "halt_pick", what=f"`{fact}` is not one of the uploaded tables.", why="The analysis needs a table to start from.")
    d.fact = fact
    ft = tables[fact]

    revenue = _choose_revenue(d, ft, picks)
    if isinstance(revenue, Step):
        d.analysis = "halt"
        d.steps.append(revenue)
        return d
    d.revenue = revenue

    order_key = picks.get("order_key") or _choose_order_key(profile, ft, schema)
    if order_key and not ft.has(order_key):
        return _halt(d, "halt_pick", what=f"`{order_key}` is not a column of `{fact}`.", why="The order key must be in the fact table.")
    d.order_key = order_key
    if order_key:
        col = ft.col(order_key)
        d.line_level = col.n_unique < ft.n_rows
        header = next((r for r in schema.parents(fact) if r.child_col == order_key), None)
        if header:
            d.header, d.header_rel = header.parent_table, header
    if d.line_level:
        d.steps.append(story("line_grain", table=fact, rows=ft.n_rows, orders=ft.col(order_key).n_unique, order=order_key))
    what = (f" and holds {_names([c for c in (revenue.price, revenue.quantity, revenue.discount, revenue.amount) if c])}"
            if revenue.kind != "count" else "")
    d.steps.insert(len(d.steps) - (1 if d.line_level else 0), story("fact_table", table=fact, rows=ft.n_rows, what=what))
    _revenue_story(d, ft)

    # ── Stage 3: time ─────────────────────────────────────────────────────
    order_tables = [fact] + ([d.header] if d.header else [])
    date = _pick_date(d, picks, order_tables)
    if isinstance(date, Step):
        d.analysis = "halt"
        d.steps.append(date)
        return d
    if date:
        d.date = date
        dcol = profile.col(*date)
        d.date_is_timestamp = dcol.logical_type == "timestamp"
        if d.header and date[0] == d.header:
            d.steps.append(story("header_table", header=d.header, fact=fact, key=order_key,
                                 extra=" (and the other order-level columns)" if len(tables[d.header].columns) > 2 else ""))
        _choose_period(d, catalog, dcol)
    else:
        d.steps.append(story("no_date", table=fact))

    # ── Stage 4: drivers, customer, dimensions ────────────────────────────
    if d.date:
        _choose_lead(d, catalog)
    _choose_rating(d, order_tables)
    _choose_customer(d, picks, order_tables)
    _choose_dimensions(d, picks)
    _choose_item(d)
    _choose_null_trap(d)
    _choose_indexes(d)
    if d.line_level:
        d.steps.append(story("aov_grain"))
    for name, (key, keep) in d.dedupe.items():
        if name in _used_tables(d):
            d.steps.append(story("dedupe", table=name, key=", ".join(key),
                                 keep=f"the latest by `{keep}`" if keep else "an arbitrary one of the copies"))
    if d.indexes:
        d.steps.append(story("indexes", n_keys=len([i for i in d.indexes if i[2] == "foreign key"]),
                             date=d.date[1] if d.date else "the date"))
    if mode == "auto" and revenue.kind == "count" and not d.date:
        d.steps.append(story("focus_auto_validate"))
        d.analysis = "validate"
        d.indexes = [i for i in d.indexes if i[2] != "date filter"]
        _skip_kpi_cells(d, "There is no measure and no date to build KPIs from.")
        return d
    d.analysis = "kpi"
    _skip_cells(d)
    return d


def _manual_hint(picks: dict) -> str:
    parts = [f"{k} = `{v}`" for k, v in picks.items() if v and k in ("fact", "measure", "date")]
    return f" ({', '.join(parts)})" if parts else ""


def _dedupe_order(t: TableProfile) -> str | None:
    dates = t.by_role("datetime")
    preferred = [c for c in dates if re.search(r"updated|modified|changed|loaded|created|timestamp", c.name, re.I)]
    return (preferred or dates or [None])[0].name if (preferred or dates) else None


def _part_unit(name: str) -> str | None:
    for unit in ("year", "quarter", "month"):
        if unit in name.lower():
            return unit
    return None


def _choose_fact(profile: CatalogProfile, schema: Schema) -> str:
    def score(t: TableProfile):
        kinds = {c.kind for c in t.by_role("measure")}
        sales = 3 if {"price", "quantity"} <= kinds else 2 if "amount" in kinds else 1 if kinds & {"price", "quantity"} else 0
        is_child = any(r.child_table == t.name for r in schema.accepted)
        has_date = bool(t.by_role("datetime")) or any(profile[r.parent_table].by_role("datetime") for r in schema.parents(t.name))
        return (sales, has_date, is_child, t.n_rows)
    return max(profile.tables.values(), key=score).name


def _choose_order_key(profile: CatalogProfile, ft: TableProfile, schema: Schema) -> str | None:
    named = [c for c in ft.columns if ORDER_KEY.search(c.name) and c.logical_type in ("integer", "text")]
    if named:
        return named[0].name
    if ft.key_status in ("single", "intended") and len(ft.primary_key) == 1:
        return ft.primary_key[0]
    for k in ft.primary_key:  # composite key: the part that points at a dated parent table is the order
        rel = next((r for r in schema.parents(ft.name) if r.child_col == k), None)
        if rel and profile[rel.parent_table].by_role("datetime"):
            return k
    return None


def _choose_revenue(d: Decision, ft: TableProfile, picks: dict) -> Revenue | Step:
    measures = {c.kind: c for c in reversed(ft.by_role("measure"))}  # first column of each kind wins
    kind = picks.get("measure")
    if kind:
        cols = [picks.get(k) for k in ("price", "quantity", "discount", "amount") if picks.get(k)]
        for c in cols:
            if not ft.has(c) or not ft.col(c).numeric:
                return story("halt_pick", what=f"`{c}` is not a numeric column of `{ft.name}`.",
                             why="Revenue is computed from numbers: a price, a quantity, a discount or an amount.")
        if kind == "price_qty":
            if not (picks.get("price") and picks.get("quantity")):
                return story("halt_pick", what="Price × quantity needs both a price and a quantity column.", why="Revenue = price × quantity.")
            return _with_discount(Revenue("price_qty", ft.name, price=picks["price"], quantity=picks["quantity"],
                                          discount=picks.get("discount") or None), ft)
        if kind == "amount":
            if not picks.get("amount"):
                return story("halt_pick", what="Pick the amount column.", why="Revenue = SUM(amount).")
            return Revenue("amount", ft.name, amount=picks["amount"])
        return Revenue("count", ft.name)
    if "price" in measures and "quantity" in measures:
        return _with_discount(Revenue("price_qty", ft.name, price=measures["price"].name, quantity=measures["quantity"].name,
                                      discount=measures["discount"].name if "discount" in measures else None), ft)
    if "amount" in measures:
        return Revenue("amount", ft.name, amount=measures["amount"].name)
    if "quantity" in measures:  # price may live in a parent (a product list price)
        for r in d.schema.parents(ft.name):
            parent = d.profile[r.parent_table]
            price = next((c for c in parent.by_role("measure") if c.kind == "price"), None)
            if price:
                return Revenue("price_qty", ft.name, price=price.name, quantity=measures["quantity"].name, price_rel=r)
    return Revenue("count", ft.name)


def _with_discount(rev: Revenue, ft: TableProfile) -> Revenue:
    if rev.discount:
        top = ft.col(rev.discount).maximum
        if top is not None and 1 < float(top) <= 100:
            rev.discount_scale = 100
    return rev


def _revenue_story(d: Decision, ft: TableProfile) -> None:
    rev = d.revenue
    if rev.kind == "price_qty" and rev.price_rel:
        d.steps.append(story("revenue_parent_price", table=ft.name, parent=rev.price_rel.parent_table, price=rev.price))
    elif rev.kind == "price_qty":
        disc = rev.discount
        formula = f"{rev.price} × {rev.quantity}" + (f" × (1 - {disc}{' / 100' if rev.discount_scale == 100 else ''})" if disc else "")
        d.steps.append(story("revenue_price_qty", table=ft.name, price=rev.price, qty=rev.quantity, formula=formula,
                             discount_note=f" and a discount (`{disc}`)" if disc else "",
                             discount_why=" minus the discount: revenue here is **net** of discount" if disc else ""))
        if disc and rev.discount_scale == 100:
            d.steps.append(story("discount_percent", col=disc, max=float(ft.col(disc).maximum)))
    elif rev.kind == "amount":
        d.steps.append(story("revenue_amount", table=ft.name, amount=rev.amount))
    else:
        d.steps.append(story("revenue_count", table=ft.name))


def _pick_date(d: Decision, picks: dict, order_tables: list[str]) -> tuple[str, str] | Step | None:
    profile = d.profile
    if picks.get("date"):
        table, _, col = picks["date"].partition(".")
        if table not in order_tables or not profile[table].has(col) or profile.col(table, col).role != "datetime":
            return story("halt_pick", what=f"`{picks['date']}` is not a date column of {_names(order_tables)}.",
                         why="Periods are cut from a real date, stored on the order.")
        return table, col
    candidates = []
    for i, table in enumerate(order_tables):
        for c in profile[table].by_role("datetime"):
            score = (2 if ORDER_DATE.search(c.name) else 0) - (3 if SHIP.search(c.name) or REQUIRED.search(c.name) else 0)
            candidates.append((score, -c.null_frac, -i, table, c.name))
    if not candidates:
        return None
    best = max(candidates)
    return best[3], best[4]


def _choose_period(d: Decision, catalog: Catalog, dcol: ColumnProfile) -> None:
    dates = catalog[dcol.table].df[dcol.name]
    first, last = pd.Timestamp(dcol.minimum), pd.Timestamp(dcol.maximum)
    years = (last - first).days / 365.25
    quarters = compute_periods(dates, "quarter")
    if years > YEARS_FOR_YEARLY:
        d.period = "year"
        why = f"The data spans {years:.0f} years, so quarters would be too many points to read."
    elif sum(p.complete for p in quarters) >= MIN_COMPLETE_QUARTERS:
        d.period = "quarter"
        why = f"It covers {sum(p.complete for p in quarters)} complete quarters: enough for quarter-over-quarter and, with 5 or more, year-over-year."
    else:
        d.period = "month"
        why = "It covers fewer than 4 complete quarters, so months give enough points to see a trend."
    d.periods = compute_periods(dates, d.period)
    d.multi_year = len({p.year for p in d.periods}) > 1 and d.period != "year"
    nulls = f" (empty in {dcol.n_null:,} rows)" if dcol.n_null else ""
    multi = " (year *and* quarter, because the data spans more than one year)" if d.multi_year and d.period == "quarter" else \
            " (year and month)" if d.multi_year else ""
    d.steps.append(story("date_chosen", table=dcol.table, col=dcol.name, start=first.date(), end=last.date(), nulls=nulls,
                         why=why, period=d.period, multi=multi))
    for p in d.partial_periods:
        d.steps.append(story("partial_period", label=d.label(p), first=max(p.first_seen, p.start), last=min(p.last_seen, p.end),
                             days=p.covered, total=p.days, pct=p.coverage * 100))


def _choose_lead(d: Decision, catalog: Catalog) -> None:
    table, start = d.date
    df = catalog[table].df
    t = d.profile[table]
    for c in t.by_role("datetime"):
        if c.name == start or not SHIP.search(c.name) or REQUIRED.search(c.name):
            continue
        both = df[[start, c.name]].dropna()
        if both.empty:
            continue
        share = float((both[c.name] >= both[start]).mean())
        if share < MIN_LEAD_SHARE:
            continue
        d.lead = (table, start, c.name)
        d.lead_name = ("ship_days" if re.search(r"ship|dispatch|sent", c.name, re.I) else
                       "delivery_days" if re.search(r"deliver|arriv|receiv", c.name, re.I) else "lead_days")
        d.steps.append(story("lead_time", start=start, end=c.name, pct=share * 100, name=d.lead_name))
        if c.n_null:
            d.steps.append(story("lead_nulls", col=c.name, n=c.n_null, pct=c.null_frac * 100, name=d.lead_name))
        required = next((r for r in t.by_role("datetime") if REQUIRED.search(r.name)), None)
        if required:
            d.required = required.name
            d.steps.append(story("late_flag", required=required.name))
        return


def _choose_rating(d: Decision, order_tables: list[str]) -> None:
    for table in order_tables:
        rating = next(iter(d.profile[table].by_role("rating")), None)
        if rating:
            d.rating = rating
            if rating.scale:
                labels = " < ".join(dict.fromkeys(label for label, _ in rating.scale))
                d.steps.append(story("rating_scale", col=rating.name, labels=labels))
            else:
                d.steps.append(story("rating_numeric", col=rating.name, low=rating.minimum, high=rating.maximum))
            return


def _choose_customer(d: Decision, picks: dict, order_tables: list[str]) -> None:
    if picks.get("customer"):
        table, _, col = picks["customer"].partition(".")
        if table in order_tables and d.profile[table].has(col):
            d.customer = (table, col)
    else:
        for table in order_tables:
            t = d.profile[table]
            col = next((c for c in t.columns if CUSTOMER_KEY.search(c.name) and c.logical_type in ("integer", "text")
                        and (c.name,) != t.primary_key), None)
            if col is not None:
                d.customer = (table, col.name)
                break
    if d.customer:
        d.customer_rel = next((r for r in d.schema.parents(d.customer[0]) if r.child_col == d.customer[1]), None)
        d.steps.append(story("customer_key", table=d.customer[0], col=d.customer[1]))
    else:
        d.steps.append(story("no_customer"))


def _dimension_alias(d: Decision, table: str, col: str, taken: set[str]) -> str:
    stem = singular(table)
    if table in (d.fact, d.header) or col.startswith(stem):
        alias = col
    else:
        alias = f"{stem}_{col}"
    base, n = alias, 1
    while alias in taken:
        n += 1
        alias = f"{base}_{n}"
    return alias


def _choose_dimensions(d: Decision, picks: dict) -> None:
    profile, schema = d.profile, d.schema
    starts = [d.fact] + ([d.header] if d.header else [])
    reach: dict[str, tuple[tuple[Relationship, ...], str]] = {}
    for start in starts:
        for table, path in schema.paths_from(start, MAX_HOPS).items():
            full = tuple(([d.header_rel] if start == d.header and d.header_rel else []) + path)
            level = "line" if d.line_level and start == d.fact and table != d.fact and (not path or path[0] != d.header_rel) else "order"
            if table == d.fact:
                level = "line" if d.line_level else "order"
            if table not in reach or len(full) < len(reach[table][0]):
                reach[table] = (full, level)
    customer_table = d.customer_rel.parent_table if d.customer_rel else None
    keys = {d.order_key} | ({d.customer[1]} if d.customer else set())
    candidates = []
    for table, (path, level) in reach.items():
        t = profile[table]
        for c in t.columns:
            if c.role != "dimension" or c.null_frac > MAX_DIM_NULL or c.name in keys or not (2 <= c.n_unique <= 50):
                continue
            group = next((g for g, rx in GROUPS if rx.search(c.name) or (g in ("people", "fulfilment") and rx.search(table))), "other")
            if group == "people" and c.name != label_column(t):
                continue  # one column per person table: its name
            if group == "other" and c.name != label_column(t):
                continue
            prefer_customer = 0 if (group == "geography" and table == customer_table) else 1
            candidates.append((group, prefer_customer, len(path), c.null_frac, abs(c.n_unique - 10), table, c, path, level))
    order = {g: i for i, (g, _) in enumerate(GROUPS)} | {"other": len(GROUPS)}
    chosen: list[Dimension] = []
    taken: set[str] = set()
    wanted = picks.get("dimensions")
    if wanted:
        for ref in wanted:
            match = next((cand for cand in candidates if f"{cand[5]}.{cand[6].name}" == ref), None)
            if match:
                group, _, _, _, _, table, c, path, level = match
                alias = _dimension_alias(d, table, c.name, taken)
                taken.add(alias)
                chosen.append(Dimension(table, c.name, alias, group, path, level, c.n_unique))
    else:
        for group, *_rest in sorted(candidates, key=lambda x: (order[x[0]],) + x[1:5]):
            if len(chosen) >= MAX_DIMENSIONS or any(dim.group == group for dim in chosen):
                continue
            _, _, _, _, _, table, c, path, level = next(x for x in sorted(candidates, key=lambda x: (order[x[0]],) + x[1:5]) if x[0] == group)
            alias = _dimension_alias(d, table, c.name, taken)
            taken.add(alias)
            chosen.append(Dimension(table, c.name, alias, group, path, level, c.n_unique))
    d.dimensions = chosen
    if chosen:
        d.steps.append(story("dimensions", names=", ".join(f"`{x.table}.{x.column}` ({x.group}, {x.n_values} values)" for x in chosen)))


def _choose_item(d: Decision) -> None:
    """Top-N: the items inside the first product-like dimension (products per category), else customers per region."""
    for dim in d.dimensions:
        if dim.level != "line" or not dim.path:
            continue
        # products per category: the category sits in its own table (two joins away) or in the product table itself
        item_table = dim.path[-1].child_table if len(dim.path) >= 2 else dim.table
        if item_table == d.fact:
            continue
        label = label_column(d.profile[item_table]) or (d.profile[item_table].primary_key or (None,))[0]
        if label and label != dim.column:
            d.item = (item_table, label, dim)
            return
    if d.customer and d.dimensions:
        dim = next((x for x in d.dimensions if x.level == "order"), None)
        if dim:
            table = d.customer_rel.parent_table if d.customer_rel else d.customer[0]
            label = (label_column(d.profile[table]) if d.customer_rel else None) or d.customer[1]
            d.item = (table, label, dim)


def _used_tables(d: Decision) -> set[str]:
    used = {d.fact} | ({d.header} if d.header else set())
    for dim in d.dimensions:
        used |= {r.parent_table for r in dim.path}
    if d.customer_rel:
        used.add(d.customer_rel.parent_table)
    if d.item:
        used.add(d.item[0])
    return {u for u in used if u}


def _choose_null_trap(d: Decision) -> None:
    rels = [r for r in d.schema.accepted if r.child_nulls]
    rels.sort(key=lambda r: (r.kind != "self", r.child_table))
    if rels:
        d.null_trap["not_in"] = (rels[0], False)
    elif d.schema.accepted:
        d.null_trap["not_in"] = (d.schema.accepted[0], True)  # no NULL in the data: inject one to show the trap
    nullable = [(c.table, c.name) for t in d.profile.tables.values() for c in t.columns
                if c.n_null and c.role in ("datetime", "measure", "rating") and c.name not in {r.child_col for r in d.schema.relationships}]
    if d.lead and d.profile.col(d.lead[0], d.lead[2]).n_null:
        d.null_trap["count"] = (d.lead[0], d.lead[2])   # e.g. shipped_date: AVG(ship days) silently skips unshipped orders
    elif d.rating and d.rating.n_null:
        d.null_trap["count"] = (d.rating.table, d.rating.name)
    elif nullable:
        d.null_trap["count"] = nullable[0]


def _choose_indexes(d: Decision) -> None:
    for r in d.schema.accepted:
        if r.kind != "self" and (r.child_col,) != d.profile[r.child_table].primary_key[:1]:
            d.indexes.append((r.child_table, (r.child_col,), "foreign key"))
        elif r.kind == "self":
            d.indexes.append((r.child_table, (r.child_col,), "foreign key"))
    if d.date:
        d.indexes.append((d.date[0], (d.date[1],), "date filter"))


KPI_CELLS = ("base", "reconcile", "kpis", "kpi_by_period", "running_total", "top_n", "latest_record", "rfm",
             "driver_tree", "drilldown", "cohort")
CELL_TITLES = {
    "base": "Build the analysis views", "reconcile": "Reconcile the totals", "kpis": "Headline KPIs",
    "kpi_by_period": "KPIs per period, with the driver", "running_total": "Running total", "top_n": "Top N per group",
    "latest_record": "Each customer's latest order", "rfm": "RFM segments", "driver_tree": "Driver tree: what moved revenue",
    "drilldown": "Drill down: symptom next to driver", "cohort": "Cohorts", "cardinality": "Join cardinality and fan-out",
    "orphans": "Orphans", "date_range": "Date ranges and partial periods",
}


def _skip_kpi_cells(d: Decision, reason: str) -> None:
    for cell in KPI_CELLS:
        d.skipped[cell] = reason
    _skip_validation_cells(d)


def _skip_validation_cells(d: Decision) -> None:
    if not d.schema.accepted:
        for cell in ("cardinality", "orphans"):
            d.skipped[cell] = "There is no relationship between the tables to check."
    if not any(t.by_role("datetime") for t in d.profile.tables.values()):
        d.skipped["date_range"] = "No table has a date column."


def _skip_cells(d: Decision) -> None:
    _skip_validation_cells(d)
    complete = len(d.complete_periods)
    if not d.date:
        for cell in ("kpi_by_period", "running_total", "latest_record", "rfm", "cohort", "driver_tree", "drilldown"):
            d.skipped[cell] = "There is no date column."
    if not d.customer:
        for cell in ("latest_record", "rfm", "cohort"):
            d.skipped.setdefault(cell, "No column identifies the customer.")
    if d.date and complete < 2:
        d.skipped.setdefault("driver_tree", "There are fewer than two complete periods to compare.")
        d.skipped.setdefault("drilldown", "There are fewer than two complete periods to compare.")
    if d.date and not d.compare:
        d.skipped.setdefault("driver_tree", "The last two complete periods are not consecutive.")
    if d.date and not (d.lead or d.rating or d.dimensions):
        d.skipped.setdefault("drilldown", "There is no driver (shipping time, rating) or dimension to drill into.")
    if not d.item:
        d.skipped["top_n"] = "There is no group-and-item pair to rank (e.g. products within a category)."
    if d.revenue.kind == "count":
        d.skipped.setdefault("rfm", "RFM needs a money measure for the M (monetary) score.")
        d.skipped.setdefault("driver_tree", "Splitting revenue into orders × average order value needs a money measure.")
    for cell, reason in list(d.skipped.items()):
        if cell in CELL_TITLES:
            d.steps.append(story("skipped_cell", reason=reason, title=CELL_TITLES[cell]))
