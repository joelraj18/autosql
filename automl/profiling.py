"""Column profiling: work out the *role* of every column, not just its dtype.

A dtype says how a value is stored; a role says how a model should use it.
An integer column can be a real quantity (``age``) or just a row number
(``customer_id``); a string column can be a category, a date or free text.
"""
from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_datetime64_any_dtype, is_integer_dtype, is_numeric_dtype

ROLES = ("numeric", "categorical", "high_card_categorical", "datetime", "id", "constant", "text")

MAX_CATEGORIES = 50       # more distinct values than this -> high-cardinality
ID_UNIQUE_RATIO = 0.95    # unique in at least 95% of rows -> candidate ID
TEXT_MIN_AVG_LEN = 30     # average string length above this -> free text
SAMPLE_ROWS = 2000        # rows inspected for the (slower) string checks

_TARGET_NAME = re.compile(
    r"(status|target|label|class|churn|outcome|default|fraud|cancel|survived|result|response|^y$)", re.IGNORECASE)
_ID_NAME = re.compile(r"(^|[_\s])(id|uuid|guid|key|index)$|^id[_\s]|^(id|index|unnamed: 0)$", re.IGNORECASE)


@dataclass(frozen=True)
class ColumnProfile:
    name: str
    dtype: str
    n_unique: int
    missing_frac: float
    role: str
    reason: str
    avg_len: float = 0.0


@dataclass(frozen=True)
class DateParts:
    """A date stored as three separate columns, e.g. arrival_year / arrival_month / arrival_date."""
    prefix: str
    year: str
    month: str
    day: str
    invalid: int  # rows whose combination is not a real date (e.g. 29 February 2018)


@dataclass(frozen=True)
class DatasetProfile:
    n_rows: int
    n_cols: int
    missing_frac: float
    duplicate_rows: int                   # rows identical in every column (true copies)
    columns: tuple[ColumnProfile, ...] = field(default_factory=tuple)
    lookalike_rows: int = 0               # extra rows identical once ID columns are ignored
    suggested_target: str | None = None
    target_reason: str = ""
    target_by_name: bool = False          # True = the column's *name* says it is an outcome (strong evidence)
    date_parts: tuple[DateParts, ...] = field(default_factory=tuple)
    infinite_cells: dict[str, int] = field(default_factory=dict)  # column -> how many ±infinity values it holds

    def get(self, name: str) -> ColumnProfile:
        return next(c for c in self.columns if c.name == name)

    def by_role(self, *roles: str) -> list[str]:
        return [c.name for c in self.columns if c.role in roles]

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "column": c.name,
                "type": c.dtype,
                "unique values": c.n_unique,
                "missing %": round(c.missing_frac * 100, 1),
                "role": c.role,
                "why this role": c.reason,
            }
            for c in self.columns
        )


def _looks_like_dates(s: pd.Series) -> bool:
    sample = s.dropna().astype(str).head(200)
    if sample.empty or pd.to_numeric(sample, errors="coerce").notna().mean() > 0.5:
        return False  # plain numbers stored as text are not dates
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parsed = pd.to_datetime(sample, errors="coerce", format="mixed")
    return parsed.notna().mean() >= 0.9


def _profile_column(name: str, s: pd.Series, n_rows: int) -> ColumnProfile:
    n_unique = int(s.nunique(dropna=True))
    missing = float(s.isna().mean()) if n_rows else 0.0
    unique_ratio = n_unique / max(int(s.notna().sum()), 1)
    dtype = str(s.dtype)

    def make(role: str, reason: str, avg_len: float = 0.0) -> ColumnProfile:
        return ColumnProfile(name, dtype, n_unique, missing, role, reason, avg_len)

    if n_unique <= 1:
        return make("constant", "only one distinct value")
    if is_datetime64_any_dtype(s):
        return make("datetime", "stored as a date/time")
    if is_bool_dtype(s):
        return make("categorical", "true/false values")
    if is_numeric_dtype(s):
        if is_integer_dtype(s) and unique_ratio > ID_UNIQUE_RATIO and n_rows >= 30:
            consecutive = int(s.max()) - int(s.min()) + 1 == n_unique
            if consecutive or _ID_NAME.search(str(name)):
                return make("id", f"unique in {unique_ratio:.0%} of rows and {'counts up 1, 2, 3…' if consecutive else 'named like an ID'}")
        return make("numeric", "numbers")

    # Strings / objects / categoricals: inspect a sample to keep this fast.
    sample = s.dropna().head(SAMPLE_ROWS).astype(str)
    avg_len = float(sample.str.len().mean()) if not sample.empty else 0.0
    if _looks_like_dates(s):
        return make("datetime", "text that parses as dates")
    if avg_len > TEXT_MIN_AVG_LEN and sample.str.contains(" ").mean() > 0.5:
        return make("text", f"long free text (~{avg_len:.0f} characters)", avg_len)
    if unique_ratio > ID_UNIQUE_RATIO and n_rows >= 30:
        return make("id", f"unique in {unique_ratio:.0%} of rows")
    if n_unique > MAX_CATEGORIES:
        return make("high_card_categorical", f"{n_unique} different categories")
    return make("categorical", f"{n_unique} categories")


def suggest_target(columns: tuple[ColumnProfile, ...]) -> tuple[str | None, str, bool]:
    """Guess which column is the outcome to predict. Returns (column, reason, found_by_name)."""
    usable = [c for c in columns if c.role in ("categorical", "numeric") and 2 <= c.n_unique <= 15]
    # A name like "target" is strong evidence, so a NUMERIC column with many values qualifies too (-> regression).
    named = [c for c in columns if _TARGET_NAME.search(c.name)
             and (c in usable or (c.role == "numeric" and c.n_unique > 15))]
    if named:
        c = named[-1]
        kind = f"it has only {c.n_unique} values" if c in usable else f"it is numeric with {c.n_unique} values"
        return c.name, f"its name looks like an outcome and {kind}", True
    last = columns[-1] if columns else None
    if last is not None and last in usable and last.role == "categorical":
        return last.name, f"it is the last column and holds {last.n_unique} labels", False
    return None, "", False


def find_date_parts(df: pd.DataFrame, columns: tuple[ColumnProfile, ...]) -> tuple[DateParts, ...]:
    numeric = {c.name for c in columns if c.role == "numeric"}
    found = []
    for col in numeric:
        m = re.fullmatch(r"(.*?)[_ ]?year", col, re.IGNORECASE)
        if not m:
            continue
        prefix = m.group(1)
        sep = "_" if col[len(prefix):].startswith("_") else ""
        month = next((c for c in (f"{prefix}{sep}month",) if c in numeric), None)
        day = next((c for c in (f"{prefix}{sep}date", f"{prefix}{sep}day") if c in numeric), None)
        if month and day:
            parts = pd.DataFrame({"year": df[col], "month": df[month], "day": df[day]})
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                when = pd.to_datetime(parts, errors="coerce")
            invalid = int((when.isna() & parts.notna().all(axis=1)).sum())
            found.append(DateParts(prefix.rstrip("_ ") or "date", col, month, day, invalid))
    return tuple(found)


def profile_dataset(df: pd.DataFrame) -> DatasetProfile:
    n_rows, n_cols = df.shape
    cols = tuple(_profile_column(str(c), df[c], n_rows) for c in df.columns)
    exact = int(df.duplicated().sum())
    id_cols = [c.name for c in cols if c.role == "id"]
    rest = df.drop(columns=id_cols)
    ignoring_id = int(rest.duplicated().sum()) if id_cols and rest.shape[1] else exact
    target, reason, by_name = suggest_target(cols)
    numeric = df.select_dtypes("number")
    inf_counts = np.isinf(numeric.to_numpy(dtype=float, na_value=np.nan)).sum(axis=0)
    infinite = {str(c): int(n) for c, n in zip(numeric.columns, inf_counts) if n}
    return DatasetProfile(
        n_rows=n_rows,
        n_cols=n_cols,
        missing_frac=float(df.isna().to_numpy().mean()) if df.size else 0.0,
        duplicate_rows=exact,
        columns=cols,
        lookalike_rows=ignoring_id - exact,
        suggested_target=target,
        target_reason=reason,
        target_by_name=by_name,
        date_parts=find_date_parts(df, cols),
        infinite_cells=infinite,
    )
