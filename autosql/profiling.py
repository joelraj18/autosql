"""Profiling: what each column *is*, and what one row of each table represents (its grain).

A column's type says how a value is stored; its *role* says how SQL should use it:

  identifier  - names a row of this or another table (``order_id``, ``customer_code``): join on it, never sum it
  measure     - a number you add up or average (``unit_price``, ``quantity``, ``freight``)
  rating      - a 1-5 / 1-10 score, or text on an ordered scale ("Very Bad" … "Very Good")
  datetime    - a date or timestamp
  date_part   - a year / quarter / month number stored on its own (checked against the real date)
  dimension   - a short list of labels to group by (``country``, ``category_name``)
  attribute   - descriptive text with many values (names, phones, addresses)
  text        - long free text (notes, descriptions)
  constant    - one value only

A table's *grain* comes from its primary key: the smallest set of columns (1 or 2) that is unique and never empty.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

import pandas as pd

from .loading import Catalog, Table

MAX_DIMENSION_VALUES = 50     # more distinct labels than this -> attribute, not something to group by
TEXT_MIN_AVG_LEN = 30         # average length above this (with spaces) -> free text
KEY_SCORE_MIN = 2             # a unique column needs at least this score to be the primary key
INTENDED_KEY_UNIQUE = 0.9     # a key-like column unique in >= 90% of rows is the key it was *meant* to be
MAX_KEY_CANDIDATES = 15       # columns tried in pairs for a composite key

KEY_NAME = re.compile(r"(^|_)(id|key|code|no|num|number|uuid|guid|sku)$|^id_", re.I)
NOT_KEY = re.compile(r"postal|zip|phone|fax|email|mail|address|url|path|area_code", re.I)
ATTRIBUTE_NAME = re.compile(r"name|title|phone|fax|email|address|postal|zip|url|path|description|notes?$|comment", re.I)
PRICE = re.compile(r"(^|_)(unit_)?(price|cost)(_|$)|_price$|^price", re.I)
QTY = re.compile(r"(^|_)(qty|quantity)(_|$)|^units?$|units_sold|^quantity", re.I)
DISCOUNT = re.compile(r"discount|markdown", re.I)
AMOUNT = re.compile(r"(^|_)(amount|amt|revenue|sales|total|subtotal|line_total|net|gross|turnover)(_|$)", re.I)
NOT_AMOUNT = re.compile(r"count|num|qty|_id$|pct|percent|rate|share|ratio", re.I)
CHARGE = re.compile(r"freight|shipping|postage|handling|tax|fee|charge", re.I)
CONTACT = re.compile(r"phone|fax|e_?mail|address|postal|zip|url|path|home_page|website|description|notes?$|comment", re.I)
RATING = re.compile(r"rating|score|stars|review|feedback|satisfaction|csat|nps", re.I)
DATE_PART = re.compile(r"(^|_)(year|quarter|month|week|day)(_number|_num|_no)?$|^(yr|qtr|mon)$", re.I)

# Ordered text scales -> 1..5. A column is a rating when >= 95% of its values belong to one scale.
SCALES: tuple[tuple[str, ...], ...] = (
    ("very bad", "bad", "okay", "good", "very good"),
    ("very poor", "poor", "average", "good", "excellent"),
    ("poor", "fair", "good", "very good", "excellent"),
    ("very dissatisfied", "dissatisfied", "neutral", "satisfied", "very satisfied"),
    ("strongly disagree", "disagree", "neutral", "agree", "strongly agree"),
    ("very unlikely", "unlikely", "neutral", "likely", "very likely"),
)
SCALE_SYNONYMS = {"ok": "okay", "average": "okay", "fair": "okay"}


@dataclass(frozen=True)
class ColumnProfile:
    table: str
    name: str
    original: str
    logical_type: str                 # integer | decimal | float | boolean | date | timestamp | text
    n_rows: int
    n_null: int
    n_unique: int
    role: str
    kind: str = ""                    # measures: price | quantity | discount | amount | charge | other
    reason: str = ""
    avg_len: float = 0.0
    max_len: int = 0
    minimum: object = None
    maximum: object = None
    scale: tuple[tuple[str, int], ...] = ()   # text ratings: (label, score) pairs, labels as they appear in the data
    unit: str = ""                            # '$', 'EUR', '%' or 'mixed', when the upload wrote it next to the numbers

    @property
    def null_frac(self) -> float:
        return self.n_null / self.n_rows if self.n_rows else 0.0

    @property
    def unique(self) -> bool:
        return self.n_null == 0 and self.n_rows > 0 and self.n_unique == self.n_rows

    @property
    def numeric(self) -> bool:
        return self.logical_type in ("integer", "decimal", "float")


@dataclass(frozen=True)
class TableProfile:
    name: str
    source: str
    n_rows: int
    columns: tuple[ColumnProfile, ...]
    primary_key: tuple[str, ...] = ()
    key_status: str = "none"          # single | composite | intended (duplicates) | none
    key_reason: str = ""
    duplicate_keys: int = 0           # key values that appear more than once
    extra_rows: int = 0               # rows beyond one per key value
    null_keys: int = 0
    exact_duplicates: int = 0         # rows identical in every column
    renamed: tuple[tuple[str, str], ...] = ()

    def col(self, name: str) -> ColumnProfile:
        return next(c for c in self.columns if c.name == name)

    def has(self, name: str) -> bool:
        return any(c.name == name for c in self.columns)

    def by_role(self, *roles: str) -> list[ColumnProfile]:
        return [c for c in self.columns if c.role in roles]

    @property
    def grain(self) -> str:
        keys = " × ".join(f"`{k}`" for k in self.primary_key)
        if self.key_status in ("single", "composite"):
            return f"one row per {keys}"
        if self.key_status == "intended":
            return f"meant to be one row per {keys}, but {self.duplicate_keys:,} value(s) appear more than once"
        return "no column (or pair of columns) identifies a row"

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame({
            "column": c.name,
            "renamed from": c.original if c.original != c.name else "",
            "type": c.logical_type,
            "role": c.role + (f" ({c.kind})" if c.kind else ""),
            "unique values": c.n_unique,
            "missing %": round(c.null_frac * 100, 1),
            "why this role": c.reason,
            "key": "🔑" if c.name in self.primary_key else "",
        } for c in self.columns)


@dataclass(frozen=True)
class CatalogProfile:
    tables: dict[str, TableProfile] = field(default_factory=dict)

    def __getitem__(self, name: str) -> TableProfile:
        return self.tables[name]

    def col(self, table: str, name: str) -> ColumnProfile:
        return self.tables[table].col(name)

    @property
    def total_rows(self) -> int:
        return sum(t.n_rows for t in self.tables.values())


def singular(name: str) -> str:
    """orders -> order, categories -> category, statuses -> status, addresses -> address."""
    if name.endswith("ies") and len(name) > 4:
        return name[:-3] + "y"
    if name.endswith(("sses", "xes", "ches", "shes", "uses")):
        return name[:-2]
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return name


def _norm(value: str, synonyms: bool) -> str:
    text = re.sub(r"\s+", " ", str(value).strip().lower())
    return SCALE_SYNONYMS.get(text, text) if synonyms else text


def match_scale(values: pd.Series) -> tuple[tuple[str, int], ...]:
    """(label, score) pairs when at least 95% of the text values follow one known ordered scale, else ().

    Works on the distinct values with their counts, most frequent first, and gives up on a scale as soon as more than 5% of
    the rows are outside it, so a million-row column costs no more than its few most common labels.
    """
    labels = values.dropna().astype(str)
    if labels.empty:
        return ()
    counts = labels.value_counts()
    budget = len(labels) * (1 - 0.95)               # rows allowed outside the scale
    for i, scale in enumerate(SCALES):
        index = {label: n + 1 for n, label in enumerate(scale)}
        inside, outside = {}, 0
        for value, n in counts.items():
            key = _norm(value, synonyms=i == 0)    # "ok", "average", "fair" stand for "okay" on the first scale only
            if key in index:
                inside[value] = index[key]
            else:
                outside += n
                if outside > budget:
                    break
        else:
            if len({_norm(v, synonyms=i == 0) for v in inside}) >= 3:
                return tuple(sorted(inside.items(), key=lambda p: (p[1], p[0])))
    return ()


def _measure_kind(name: str, values: pd.Series) -> str:
    if DISCOUNT.search(name):
        return "discount"
    if QTY.search(name):
        return "quantity"
    if PRICE.search(name):
        return "price"
    if CHARGE.search(name):
        return "charge"
    if AMOUNT.search(name) and not NOT_AMOUNT.search(name):
        return "amount"
    return "other"


def profile_column(table: Table, name: str) -> ColumnProfile:
    s = table.df[name]
    logical = table.types[name]
    n_rows = len(s)
    n_null = int(s.isna().sum())
    present = s.dropna()
    n_unique = int(present.nunique())
    original = table.column_map.get(name, name)
    base = dict(table=table.name, name=name, original=original, logical_type=logical, n_rows=n_rows, n_null=n_null, n_unique=n_unique,
                unit=table.units.get(name, ""))
    lengths = present.astype(str).str.len() if logical == "text" and not present.empty else pd.Series(dtype=float)
    if not lengths.empty:
        base.update(avg_len=float(lengths.mean()), max_len=int(lengths.max()))
    if logical in ("integer", "decimal", "float", "date", "timestamp") and not present.empty:
        base.update(minimum=present.min(), maximum=present.max())

    def make(role: str, reason: str, kind: str = "", scale=()) -> ColumnProfile:
        return ColumnProfile(**base, role=role, reason=reason, kind=kind, scale=scale)

    if n_unique <= 1:
        return make("constant", "only one distinct value" if n_unique else "always empty")
    if logical in ("date", "timestamp"):
        return make("datetime", "dates" if logical == "date" else "date and time")
    if logical == "boolean":
        return make("dimension", "true / false")
    keyish = bool(KEY_NAME.search(name)) and not NOT_KEY.search(name) and not DATE_PART.search(name)
    if logical in ("integer", "text") and keyish:
        return make("identifier", "named like an ID or code")
    if logical in ("integer", "decimal", "float"):
        if CONTACT.search(name) or re.search(r"extension|(^|_)ext$", name, re.I):
            return make("attribute", "a phone, postal or other code stored as a number")
        if RATING.search(name) and logical == "integer" and present.min() >= 0 and present.max() <= 10:
            top = 5 if present.max() <= 5 else 10
            return make("rating", f"a score from {int(present.min())} to {top}", kind=f"1-{top}")
        if DATE_PART.search(name) and logical == "integer":
            return make("date_part", "a year / quarter / month number")
        if logical == "integer" and n_unique == 2 and set(present.unique()) <= {0, 1}:
            return make("dimension", "0 / 1 flag")
        kind = _measure_kind(name, present)
        return make("measure", {"price": "a price", "quantity": "a quantity", "discount": "a discount",
                                "amount": "a money amount", "charge": "a charge such as freight or tax",
                                "other": "numbers"}[kind], kind=kind)
    # text
    scale = match_scale(present)
    if scale:
        return make("rating", "an ordered scale: " + " < ".join(dict.fromkeys(label for label, _ in scale)), kind="scale", scale=scale)
    sample = present.astype(str).head(2000)
    if base.get("avg_len", 0) > TEXT_MIN_AVG_LEN and sample.str.contains(" ").mean() > 0.5:
        return make("text", f"long free text (~{base['avg_len']:.0f} characters)")
    if CONTACT.search(name):
        return make("attribute", "contact, address or descriptive details")
    if n_unique > MAX_DIMENSION_VALUES:
        return make("attribute", f"{n_unique} different values: describes rows rather than grouping them")
    return make("dimension", f"{n_unique} labels to group by")


def _key_score(table: str, col: ColumnProfile, position: int) -> float:
    score = 0.0
    stem = singular(table)
    if col.name in (f"{stem}_id", f"{stem}_key", f"{stem}_code", f"{stem}_no", f"{table}_id") or (col.name == "id"):
        score += 4
    if col.role == "identifier":
        score += 3
    if position == 0:
        score += 2
    if col.logical_type == "integer":
        score += 1
    if ATTRIBUTE_NAME.search(col.name) or NOT_KEY.search(col.name):
        score -= 5
    return score


def find_primary_key(table: Table, cols: tuple[ColumnProfile, ...]) -> tuple[tuple[str, ...], str, str]:
    """(key columns, status, reason). Single column first; then pairs; else the key it was meant to have."""
    df = table.df
    usable = [c for c in cols if c.logical_type in ("integer", "text", "date", "timestamp") and c.role != "text"]
    single = [(c, _key_score(table.name, c, i)) for i, c in enumerate(cols) if c in usable and c.unique and c.logical_type in ("integer", "text")]
    single = [(c, s) for c, s in single if s >= KEY_SCORE_MIN]
    if single:
        best = max(single, key=lambda cs: (cs[1], -cols.index(cs[0])))[0]
        return (best.name,), "single", f"`{best.name}` is unique and never empty"

    def pair(pool: list[ColumnProfile]):
        for a, b in itertools.combinations(pool, 2):
            if not df.duplicated(subset=[a.name, b.name]).any():
                return (a.name, b.name), "composite", f"no single column is unique, but `{a.name}` + `{b.name}` together are"
        return None

    # 1) two ID columns together (order_id + product_id); 2) an ID that is *meant* to be unique but has duplicates;
    # 3) an ID or label together with a date or label (one row per store per day)
    keyish = [c for c in usable if c.role == "identifier" and c.n_null == 0][:MAX_KEY_CANDIDATES]
    found = pair(keyish)
    if found:
        return found
    if df.duplicated().any():  # two IDs that are unique once exact copies (a double load) are removed
        distinct = df.drop_duplicates()
        for a, b in itertools.combinations(keyish, 2):
            if not distinct.duplicated(subset=[a.name, b.name]).any():
                return (a.name, b.name), "intended", (f"`{a.name}` + `{b.name}` identify every row once the "
                                                      f"{int(df.duplicated().sum()):,} exact copies are removed")
    near = [c for c in cols if c.role == "identifier" and c.n_rows and c.n_unique / max(c.n_rows - c.n_null, 1) >= INTENDED_KEY_UNIQUE]
    if near:
        best = max(near, key=lambda c: (_key_score(table.name, c, cols.index(c)), c.n_unique))
        return (best.name,), "intended", f"`{best.name}` is named like a key, but some values repeat or are empty"
    groupish = [c for c in usable if c.role in ("identifier", "dimension", "datetime") and c.n_null == 0][:MAX_KEY_CANDIDATES]
    return pair(groupish) or ((), "none", "no column, or pair of columns, is unique")


def profile_table(table: Table) -> TableProfile:
    cols = tuple(profile_column(table, c) for c in table.df.columns)
    key, status, reason = find_primary_key(table, cols)
    df = table.df
    dup_keys = extra = null_keys = 0
    if key:
        counts = df.groupby(list(key), dropna=False).size()
        dup_keys = int((counts > 1).sum())
        extra = int(len(df) - len(counts))
        null_keys = int(df[list(key)].isna().any(axis=1).sum())
    try:
        exact = int(df.duplicated().sum())
    except TypeError:  # unhashable values
        exact = 0
    return TableProfile(table.name, table.source, len(df), cols, key, status, reason, dup_keys, extra, null_keys, exact,
                        tuple(table.renamed.items()))


def profile_catalog(catalog: Catalog) -> CatalogProfile:
    return CatalogProfile({name: profile_table(t) for name, t in catalog.tables.items()})

