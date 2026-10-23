"""Read uploaded files into typed tables, then into an in-memory DuckDB database.

Every file is read as **text first** and typed afterwards, so that nothing is lost on the way in:
``05021`` stays a postal code instead of becoming the number 5021, and ``7/4/1996`` becomes a real DATE.
Column and table names become ``snake_case`` so that the generated SQL needs no quoting
(``OrderID`` → ``order_id``, ``Order Details`` → ``order_details``); the original names are kept for the profile.
"""
from __future__ import annotations

import io
import re
import unicodedata
import warnings
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from .dialect import Dialect

NA_VALUES = ["", "NULL", "null", "NA", "N/A", "n/a", "\\N", "None", "nan", "NaN"]
DATE_PARSE_SHARE = 0.95          # at least this share of values must parse with one format
MAX_ZIP_MEMBERS = 50
MAX_ZIP_BYTES = 500 * 1024 ** 2  # uncompressed
TABLE_SUFFIXES = (".csv", ".tsv", ".txt", ".xlsx", ".xls")

DATE_FORMATS = (
    "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%b %d, %Y", "%d-%b-%Y",
)
_INT = re.compile(r"^[+-]?\d+$")
_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_LEADING_ZERO = re.compile(r"^[+-]?0\d")


@dataclass
class Table:
    name: str                                  # SQL name, snake_case
    source: str                                # e.g. "orders.csv" or "book.xlsx › Sheet1"
    df: pd.DataFrame                           # typed values, snake_case columns
    column_map: dict[str, str]                 # sql name -> original header
    types: dict[str, str]                      # integer | decimal | float | boolean | date | timestamp | text
    date_formats: dict[str, str] = field(default_factory=dict)
    unparsed_dates: dict[str, int] = field(default_factory=dict)   # values that looked like dates but didn't parse
    ambiguous_dates: list[str] = field(default_factory=list)       # day/month order could not be told apart

    @property
    def renamed(self) -> dict[str, str]:
        return {new: old for new, old in self.column_map.items() if new != old}


@dataclass
class Catalog:
    tables: dict[str, Table] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (source, why it was skipped)

    def __getitem__(self, name: str) -> Table:
        return self.tables[name]

    @property
    def names(self) -> list[str]:
        return list(self.tables)


# ── Names ────────────────────────────────────────────────────────────────
def snake_case(name: str) -> str:
    """``OrderID`` → ``order_id``, ``Order Date`` → ``order_date``, ``HTTPStatus`` → ``http_status``."""
    text = unicodedata.normalize("NFKD", str(name).strip().replace("ß", "ss")).encode("ascii", "ignore").decode()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)       # orderId -> order_Id
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", text)     # HTTPStatus -> HTTP_Status
    text = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower()
    if not text:
        return "unnamed"
    return f"c_{text}" if text[0].isdigit() else text


def unique_names(names: list[str], taken: set[str] | None = None) -> list[str]:
    """Make names unique by adding _2, _3 … ("a" and "A" would otherwise collide after renaming)."""
    seen = set(taken or ())
    out = []
    for name in names:
        candidate, n = name, 1
        while candidate in seen:
            n += 1
            candidate = f"{name}_{n}"
        seen.add(candidate)
        out.append(candidate)
    return out


# ── Types ────────────────────────────────────────────────────────────────
def _parse_dates(values: pd.Series) -> tuple[str | None, bool]:
    """The date format that parses the most values (or None), and whether day/month order was ambiguous."""
    sample = values.head(500)
    rates = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for fmt in DATE_FORMATS:
            rates[fmt] = pd.to_datetime(sample, format=fmt, errors="coerce").notna().mean()
    best = max(rates, key=rates.get)
    if rates[best] < DATE_PARSE_SHARE:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            iso = pd.to_datetime(sample, format="ISO8601", errors="coerce", utc=True)  # e.g. 2022-01-01T10:00:00Z
        if iso.notna().mean() >= DATE_PARSE_SHARE and not sample.str.fullmatch(r"\d+").any():
            return "ISO8601", False
        return None, False
    swapped = best.replace("%m/%d", "%D/%M").replace("%d/%m", "%m/%d").replace("%D/%M", "%d/%m")
    ambiguous = swapped != best and rates.get(swapped, 0) >= rates[best]
    return best, ambiguous


def infer_column(raw: pd.Series) -> tuple[pd.Series, str, str | None, int, bool]:
    """Type one text column. Returns (values, logical type, date format, unparsed dates, ambiguous day/month)."""
    values = raw.dropna().astype(str).str.strip()
    values = values[values != ""]
    blank = raw.isna() | (raw.astype(str).str.strip() == "")
    if values.empty:
        return pd.Series([None] * len(raw), index=raw.index, dtype="object"), "text", None, 0, False
    text = raw.where(~blank).astype("object").map(lambda v: v.strip() if isinstance(v, str) else v)
    text = text.where(text.notna(), None)

    all_int = bool(values.str.match(_INT).all())
    leading_zero = bool(values.str.match(_LEADING_ZERO).any())  # "05021" is a code, not the number 5021
    if all_int and not leading_zero and values.str.len().max() <= 18:
        return pd.to_numeric(text, errors="coerce").astype("Int64"), "integer", None, 0, False
    if not all_int and values.str.match(_NUM).all() and not leading_zero:
        numbers = pd.to_numeric(text, errors="coerce").astype("float64")
        fraction = values.str.extract(r"\.(\d+)$")[0].dropna()
        if (fraction.str.strip("0") == "").all() and np.isclose(numbers.dropna() % 1, 0).all() and numbers.abs().max() < 1e15:
            return numbers.round().astype("Int64"), "integer", None, 0, False  # "5.0" (e.g. from Excel) is a whole number
        if not fraction.empty and fraction.str.len().max() <= 4 and not values.str.contains("[eE]").any():
            return numbers, "decimal", None, 0, False
        return numbers, "float", None, 0, False
    lowered = values.str.lower()
    if set(lowered.unique()) <= {"true", "false"}:
        return text.map(lambda v: None if v is None or pd.isna(v) else str(v).lower() == "true").astype("boolean"), "boolean", None, 0, False
    fmt, ambiguous = _parse_dates(values)
    if fmt:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if fmt == "ISO8601":  # time-zone aware: stored as UTC without the zone
                when = pd.to_datetime(text, format=fmt, errors="coerce", utc=True).dt.tz_convert(None)
            else:
                when = pd.to_datetime(text, format=fmt, errors="coerce")
        unparsed = int((when.isna() & ~blank).sum())
        times = when.dropna()
        is_date = bool((times == times.dt.normalize()).all())
        return when, "date" if is_date else "timestamp", fmt, unparsed, ambiguous
    return text.astype("object"), "text", None, 0, False


def make_table(name: str, source: str, raw: pd.DataFrame, taken: set[str]) -> Table:
    originals = [str(c) for c in raw.columns]
    # a name with no Latin letters or digits (e.g. 城市) becomes column_<position>; the original stays in the profile
    sql_names = unique_names([snake_case(c) if snake_case(c) != "unnamed" or not c.strip() else f"column_{i + 1}"
                              for i, c in enumerate(originals)])
    raw = raw.copy()
    raw.columns = sql_names
    df, types, formats, unparsed, ambiguous = pd.DataFrame(index=raw.index), {}, {}, {}, []
    for col in sql_names:
        values, logical, fmt, bad, amb = infer_column(raw[col])
        df[col] = values
        types[col] = logical
        if fmt:
            formats[col] = fmt
        if bad:
            unparsed[col] = bad
        if amb:
            ambiguous.append(col)
    table_name = unique_names([snake_case(name)], taken)[0]
    return Table(table_name, source, df.reset_index(drop=True), dict(zip(sql_names, originals)), types, formats, unparsed, ambiguous)


# ── Files ────────────────────────────────────────────────────────────────
def _read_text_table(data: bytes, filename: str) -> pd.DataFrame:
    sep = "\t" if filename.lower().endswith((".tsv", ".txt")) else ","
    last_error = None
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(data), sep=sep, dtype=str, keep_default_na=False, na_values=NA_VALUES,
                               encoding=encoding, low_memory=False)
        except UnicodeDecodeError as error:
            last_error = error
    raise last_error


def _read_excel(data: bytes) -> dict[str, pd.DataFrame]:
    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str, keep_default_na=False, na_values=NA_VALUES)
    return {name: df for name, df in sheets.items() if not df.empty or len(df.columns)}


def _iter_files(files: list[tuple[str, bytes]], skipped: list):
    """Yield (filename, bytes); ZIP archives are opened in memory (never extracted to disk) with size limits."""
    for filename, data in files:
        if not filename.lower().endswith(".zip"):
            yield filename, data
            continue
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            skipped.append((filename, "not a valid ZIP file"))
            continue
        members = [m for m in archive.infolist() if not m.is_dir() and not m.filename.startswith("__MACOSX/")
                   and not Path(m.filename).name.startswith(".") and m.filename.lower().endswith(TABLE_SUFFIXES)]
        if len(members) > MAX_ZIP_MEMBERS:
            skipped.append((filename, f"has {len(members)} files; the limit is {MAX_ZIP_MEMBERS}"))
            continue
        if sum(m.file_size for m in members) > MAX_ZIP_BYTES:
            skipped.append((filename, f"is larger than {MAX_ZIP_BYTES // 1024 ** 2} MB once unzipped"))
            continue
        for m in members:
            yield f"{filename} › {m.filename}", archive.read(m)


def read_files(files: list[tuple[str, bytes]]) -> Catalog:
    """Read CSV / TSV / Excel / ZIP uploads. Each CSV is one table; each non-empty Excel sheet is one table."""
    catalog = Catalog()
    for filename, data in _iter_files(files, catalog.skipped):
        base = Path(filename.split(" › ")[-1]).name
        stem = base.rsplit(".", 1)[0]
        try:
            if base.lower().endswith((".xlsx", ".xls")):
                sheets = _read_excel(data)
                frames = [(stem if len(sheets) == 1 else f"{stem}_{sheet}", f"{filename} › {sheet}", df)
                          for sheet, df in sheets.items()]
            elif base.lower().endswith((".csv", ".tsv", ".txt")):
                frames = [(stem, filename, _read_text_table(data, base))]
            else:
                catalog.skipped.append((filename, "is not a CSV, TSV, Excel or ZIP file"))
                continue
        except Exception as error:  # empty, corrupt or password-protected files
            catalog.skipped.append((filename, f"could not be read ({type(error).__name__}: {str(error)[:120]})"))
            continue
        for name, source, raw in frames:
            if raw.shape[1] == 0:
                catalog.skipped.append((source, "has no columns"))
                continue
            table = make_table(name, source, raw, set(catalog.tables))
            catalog.tables[table.name] = table
    return catalog


def read_folder(folder: str | Path, pattern: str = "*") -> Catalog:
    paths = sorted(p for p in Path(folder).glob(pattern) if p.suffix.lower() in TABLE_SUFFIXES + (".zip",))
    return read_files([(p.name, p.read_bytes()) for p in paths])


# ── Database ─────────────────────────────────────────────────────────────
_DUCK_TYPES = {"integer": "BIGINT", "decimal": "DOUBLE", "float": "DOUBLE", "boolean": "BOOLEAN",
               "date": "DATE", "timestamp": "TIMESTAMP", "text": "VARCHAR"}


def build_database(catalog: Catalog) -> duckdb.DuckDBPyConnection:
    """A fresh in-memory DuckDB with one table per uploaded table, locked so that queries can't read local files."""
    con = duckdb.connect(":memory:")
    duck = Dialect("duckdb")
    for table in catalog.tables.values():
        con.register("_upload", table.df)
        columns = ", ".join(f"CAST({duck.ident(c)} AS {_DUCK_TYPES[table.types[c]]}) AS {duck.ident(c)}" for c in table.df.columns)
        con.execute(f"CREATE TABLE {duck.ident(table.name)} AS SELECT {columns} FROM _upload")
        con.unregister("_upload")
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con
