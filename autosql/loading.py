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
NUMBER_PARSE_SHARE = 0.95        # the same rule for numbers written the business way ($1,234.50, 15%, (12.50))
# Spreadsheet and export markers for "no value": read as empty in number and date columns (text columns keep them).
PLACEHOLDERS = frozenset({"-", "--", "---", "\u2014", "\u2013", "?", "??", ".", "#n/a", "#na", "#value!", "#div/0!", "#ref!", "#num!",
                          "#name?", "#null!", "n.a.", "n.a", "na.", "n/d", "nil", "none", "null", "missing", "unknown", "tbd",
                          "(blank)", "blank", "not available"})
MAX_ZIP_MEMBERS = 50
MAX_ZIP_BYTES = 500 * 1024 ** 2  # uncompressed
TABLE_SUFFIXES = (".csv", ".tsv", ".txt", ".xlsx", ".xls")

DATE_FORMATS = (
    "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%b %d, %Y", "%d-%b-%Y",
)
_INT = re.compile(r"^[+-]?[0-9]+$")   # ASCII digits only: "١٢٣" is not a number SQL can read
_NUM = re.compile(r"^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$")
_LEADING_ZERO = re.compile(r"^[+-]?0[0-9]")
_DECIMAL_COMMA = re.compile(r"^[+-]?([0-9]{1,3}(\.[0-9]{3})+|[0-9]+)(,[0-9]+)?$")  # 1.234,56 or 12,5 (European spreadsheets)
_UNIT_PREFIX = re.compile(r"^([$\u20ac\u00a3\u00a5\u20b9]|[A-Z]{3}(?=[\s0-9.,]))\s*")   # $ € £ ¥ ₹ or an ISO code (USD 12)
_UNIT_SUFFIX = re.compile(r"\s*(%|[$\u20ac\u00a3\u00a5\u20b9]|[A-Z]{3})$")            # 12 %, 12€, 12 EUR
# A number with optional thousands separators: '1,234.50' (or '1.234,50' / '1 234,50' in decimal-comma files)
_GROUPED = {False: re.compile(r"^(?:([0-9]{1,3}(?:([,'])[0-9]{3})(?:\2[0-9]{3})*)|[0-9]+)(\.[0-9]+)?$|^\.[0-9]+$"),
            True: re.compile(r"^(?:([0-9]{1,3}(?:([. \u00a0\u202f])[0-9]{3})(?:\2[0-9]{3})*)|[0-9]+)(,[0-9]+)?$|^,[0-9]+$")}
DELIMITERS = (",", ";", "\t", "|")


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
    unparsed_numbers: dict[str, int] = field(default_factory=dict)  # values in a number column that weren't numbers
    units: dict[str, str] = field(default_factory=dict)             # '$', 'EUR', '%' stripped from a number column; 'mixed'
    cleaned: dict[str, str] = field(default_factory=dict)           # how a column was normalised, with an example
    placeholders: dict[str, int] = field(default_factory=dict)      # '-', '#N/A', 'unknown' … read as empty

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


@dataclass
class Inferred:
    """One typed column, and what had to be done to it."""
    values: pd.Series
    logical: str                               # integer | decimal | float | boolean | date | timestamp | text
    date_format: str | None = None
    unparsed: int = 0                          # values that didn't parse as the column's type (dates or numbers)
    ambiguous: bool = False                    # day/month order could not be told apart
    unit: str | None = None                    # '$', 'EUR', '%' stripped from the values; 'mixed' when currencies differ
    example: str = ""                          # one normalised value, for the story: "'$1,234.50' → 1234.50"
    placeholders: int = 0                      # values such as '-' or '#N/A' read as empty
    currency: pd.Series | None = None          # the currency of each row, when the column mixes currencies


def _cast(series: pd.Series, dtype: str) -> pd.Series:
    """Text to numbers. Integers use Arrow's C++ cast when it reads every value (exact, and fast); everything else goes through
    pandas' to_numeric, whose float parsing is correctly rounded (Arrow's can differ in the 17th significant digit)."""
    if dtype == "Int64":
        try:
            return series.astype(dtype)
        except (ValueError, TypeError):
            pass
    numbers = pd.to_numeric(series.astype("object"), errors="coerce")
    return numbers.astype("float64") if dtype == "float64" else numbers.astype(dtype)


def _plain_number(s: str, decimal_comma: bool) -> str | None:
    """'1,234.50' → '1234.50' ('1.234,50' → '1234.50' in decimal-comma files); None if it is not a number."""
    m = _GROUPED[decimal_comma].match(s)
    if not m:
        return None
    sep = m.group(2)
    plain = s.replace(sep, "") if sep else s
    return plain.replace(",", ".") if decimal_comma else plain


def _business_number(text: str, decimal_comma: bool) -> tuple[str | None, str | None]:
    """A number as a business export writes it, as a plain number string and its unit.

    '$1,234.50' → ('1234.50', '$'), '(12.50)' → ('-12.50', None), '15%' → ('15', '%'), '12.50 EUR' → ('12.50', 'EUR'),
    '12.50-' → ('-12.50', None). (None, None) when it isn't a number.
    """
    s = text.strip().replace("\u2212", "-")
    negative, unit = False, None
    if len(s) > 2 and s[0] == "(" and s[-1] == ")":            # accounting negative
        negative, s = True, s[1:-1].strip()
    for _ in range(2):                                           # a sign and a currency in either order: -$12, $-12
        if s[:1] in ("+", "-"):
            negative ^= s[0] == "-"
            s = s[1:].lstrip()
        m = _UNIT_PREFIX.match(s)
        if m and unit is None:
            unit, s = m.group(1), s[m.end():]
    m = _UNIT_SUFFIX.search(s)
    if m:
        if unit is not None:
            return None, None
        unit, s = m.group(1), s[:m.start()]
    if s.endswith("-") and len(s) > 1:                           # trailing minus (SAP-style exports)
        negative, s = not negative, s[:-1].rstrip()
    plain = _plain_number(s, decimal_comma)
    if plain is None:
        return None, None
    return ("-" + plain if negative else plain), unit


def _type_numbers(strings: pd.Series, numbers: pd.Series) -> tuple[pd.Series, str]:
    """Integer, decimal or float, from the plain number strings (no blanks) and their float values (the whole column)."""
    strings = pd.Series(np.asarray(strings.unique(), dtype=object))   # every test below only needs the distinct strings
    if strings.str.match(_INT).all() and strings.str.len().max() <= 18:
        return numbers.round().astype("Int64"), "integer"
    fraction = strings.str.extract(r"\.(\d+)$")[0].dropna()
    if (fraction.str.strip("0") == "").all() and np.isclose(numbers.dropna() % 1, 0).all() and numbers.abs().max() < 1e15:
        return numbers.round().astype("Int64"), "integer"  # "5.0" (e.g. from Excel) is a whole number
    if not fraction.empty and fraction.str.len().max() <= 4 and not strings.str.contains("[eE]").any():
        return numbers, "decimal"
    return numbers, "float"


def _as_business_numbers(text: pd.Series, values: pd.Series, decimal_comma: bool) -> Inferred | None:
    """Numbers written the business way, with placeholders read as empty, if at least 95% of the other values parse."""
    is_placeholder = values.str.lower().isin(PLACEHOLDERS)
    real = values[~is_placeholder]
    if real.empty:
        return None
    counts = real.value_counts()
    if pd.Series(counts.index, dtype=object).astype(str).str.match(_LEADING_ZERO).any():
        return None
    parsed = {v: _business_number(v, decimal_comma) for v in counts.index}
    good = [v for v, (plain, _) in parsed.items() if plain is not None]
    if not good or counts[good].sum() / counts.sum() < NUMBER_PARSE_SHARE:
        return None
    units = {parsed[v][1] for v in good if parsed[v][1]}
    currencies = units - {"%"}
    if "%" in units and currencies:
        return None                                              # percentages and money in one column: not one measure
    mapping = {v: parsed[v][0] for v in good}
    clean = text.map(mapping)                                    # placeholders, junk and blanks become NaN
    numbers = pd.to_numeric(clean, errors="coerce").astype("float64")
    typed, logical = _type_numbers(clean.dropna().astype(str), numbers)
    unit = "%" if "%" in units else (next(iter(currencies)) if len(currencies) == 1 else "mixed" if currencies else None)
    changed = next((v for v in good if mapping[v] != v), None)
    example = f"'{changed}' → {mapping[changed]}" if changed else ""
    currency = text.map({v: parsed[v][1] for v in good}) if unit == "mixed" else None
    return Inferred(typed, logical, unparsed=int(counts.drop(good).sum()), unit=unit, example=example,
                    placeholders=int(is_placeholder.sum()), currency=currency)


def infer_column(raw: pd.Series, decimal_comma: bool = False) -> Inferred:
    """Type one text column.

    ``decimal_comma``: the file uses ';' between fields, so '12,5' and '1.234,56' are numbers written the European way.
    """
    if decimal_comma:
        present = raw.dropna().astype(str).str.strip()
        present = present[present != ""]
        if len(present) and present.str.contains(",").any() and present.str.match(_DECIMAL_COMMA).all():
            raw = raw.map(lambda v: v if not isinstance(v, str) else v.strip().replace(".", "").replace(",", "."))
    if isinstance(raw.dtype, pd.StringDtype):        # Arrow-backed text from read_csv: strip in C++
        stripped = raw.str.strip()
    else:
        stripped = raw.astype("object").where(raw.notna(), None).astype(str).str.strip().where(raw.notna(), None)
    blank = stripped.isna() | (stripped == "")
    present = stripped.where(~blank)
    values = present.dropna()
    if values.empty:
        return Inferred(pd.Series([None] * len(raw), index=raw.index, dtype="object"), "text")
    distinct = pd.Series(np.asarray(values.unique(), dtype=object))   # pattern tests only need the distinct values

    all_int = bool(distinct.str.match(_INT).all())
    leading_zero = bool(distinct.str.match(_LEADING_ZERO).any())  # "05021" is a code, not the number 5021
    if all_int and not leading_zero and distinct.str.len().max() <= 18:
        return Inferred(_cast(present, "Int64"), "integer")
    if not all_int and distinct.str.match(_NUM).all() and not leading_zero:
        typed, logical = _type_numbers(distinct, _cast(present, "float64"))
        return Inferred(typed, logical)
    text = present.astype("object").where(present.notna(), None)
    lowered = values.str.lower()
    if set(distinct.str.lower()) <= {"true", "false"}:
        return Inferred(text.map(lambda v: None if v is None or pd.isna(v) else str(v).lower() == "true").astype("boolean"), "boolean")
    business = _as_business_numbers(text, values, decimal_comma)
    if business is not None:
        return business
    placeholder = lowered.isin(PLACEHOLDERS)
    real = values[~placeholder]
    fmt, ambiguous = _parse_dates(real) if not real.empty else (None, False)
    if fmt:
        dated = text.where(~text.astype(str).str.lower().isin(PLACEHOLDERS), None)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if fmt == "ISO8601":  # time-zone aware: stored as UTC without the zone
                when = pd.to_datetime(dated, format=fmt, errors="coerce", utc=True).dt.tz_convert(None)
            else:
                when = pd.to_datetime(dated, format=fmt, errors="coerce")
        unparsed = int((when.isna() & dated.notna()).sum())
        times = when.dropna()
        is_date = bool((times == times.dt.normalize()).all())
        return Inferred(when, "date" if is_date else "timestamp", fmt, unparsed, ambiguous, placeholders=int(placeholder.sum()))
    return Inferred(text.astype("object"), "text")


def make_table(name: str, source: str, raw: pd.DataFrame, taken: set[str], decimal_comma: bool = False) -> Table:
    originals = [str(c) for c in raw.columns]
    # a name with no Latin letters or digits (e.g. 城市) becomes column_<position>; the original stays in the profile
    sql_names = unique_names([snake_case(c) if snake_case(c) != "unnamed" or not c.strip() else f"column_{i + 1}"
                              for i, c in enumerate(originals)])
    raw = raw.copy()
    raw.columns = sql_names
    df = pd.DataFrame(index=raw.index)
    table = Table(unique_names([snake_case(name)], taken)[0], source, df, dict(zip(sql_names, originals)), {})
    extra = []
    for col in sql_names:
        got = infer_column(raw[col], decimal_comma)
        df[col] = got.values
        table.types[col] = got.logical
        if got.date_format:
            table.date_formats[col] = got.date_format
        if got.unparsed:
            (table.unparsed_dates if got.logical in ("date", "timestamp") else table.unparsed_numbers)[col] = got.unparsed
        if got.ambiguous:
            table.ambiguous_dates.append(col)
        if got.unit:
            table.units[col] = got.unit
        if got.example:
            table.cleaned[col] = got.example
        if got.placeholders:
            table.placeholders[col] = got.placeholders
        if got.currency is not None:
            extra.append((col, got.currency))
    for col, currency in extra:      # a column mixing currencies keeps its numbers; the currency of each row goes next to it
        name_ = unique_names([f"{col}_currency"], set(df.columns))[0]
        df[name_] = currency.astype("object").where(currency.notna(), None)
        table.types[name_] = "text"
        table.column_map[name_] = f"{table.column_map[col]} (currency)"
    table.df = df.reset_index(drop=True)
    return table


# ── Files ────────────────────────────────────────────────────────────────
def sniff_delimiter(data: bytes, filename: str) -> str:
    """The field separator: the candidate that appears the same, non-zero number of times on the first lines."""
    if filename.lower().endswith(".tsv"):
        return "\t"
    head = data[:20_000].decode("utf-8", errors="ignore").splitlines()[:20]
    head = [line for line in head if line.strip()]
    best, best_count = ",", 0
    for sep in DELIMITERS:
        counts = [line.count(sep) for line in head]
        if counts and counts[0] > 0 and len(set(counts[: min(len(counts), 10)])) == 1 and counts[0] > best_count:
            best, best_count = sep, counts[0]
    return best


def _read_text_table(data: bytes, filename: str) -> tuple[pd.DataFrame, str]:
    sep = sniff_delimiter(data, filename)
    last_error = None
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(data), sep=sep, dtype=str, keep_default_na=False, na_values=NA_VALUES,
                               encoding=encoding, low_memory=False), sep
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
                frames = [(stem if len(sheets) == 1 else f"{stem}_{sheet}", f"{filename} › {sheet}", df, False)
                          for sheet, df in sheets.items()]
            elif base.lower().endswith((".csv", ".tsv", ".txt")):
                df, sep = _read_text_table(data, base)
                frames = [(stem, filename, df, sep == ";")]
            else:
                catalog.skipped.append((filename, "is not a CSV, TSV, Excel or ZIP file"))
                continue
        except Exception as error:  # empty, corrupt or password-protected files
            catalog.skipped.append((filename, f"could not be read ({type(error).__name__}: {str(error)[:120]})"))
            continue
        for name, source, raw, decimal_comma in frames:
            if raw.shape[1] == 0:
                catalog.skipped.append((source, "has no columns"))
                continue
            table = make_table(name, source, raw, set(catalog.tables), decimal_comma)
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
