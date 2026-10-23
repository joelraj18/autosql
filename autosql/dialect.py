"""SQL dialects: the few places where MySQL 8, PostgreSQL and DuckDB disagree.

Every generated statement is written once, as a function of a ``Dialect``, and rendered three times:

* ``mysql``    - shown to the user (MySQL 8)
* ``postgres`` - shown to the user (PostgreSQL)
* ``duckdb``   - what actually runs inside the app (DuckDB is PostgreSQL-like)

Everything else in the generated SQL uses only constructs that all three accept: CTEs, window functions,
``CASE``, ``NULLIF``, ``COALESCE``. Constructs that one of them lacks are never emitted (see ``BANNED``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

DIALECTS = ("mysql", "postgres", "duckdb")
SHOWN = ("mysql", "postgres")
LABELS = {"mysql": "MySQL 8", "postgres": "PostgreSQL", "duckdb": "DuckDB"}

# Constructs at least one of the three dialects rejects. The tests assert none of them is ever generated.
BANNED = {
    "FILTER (WHERE": "MySQL has no aggregate FILTER clause; use SUM(CASE WHEN … THEN 1 ELSE 0 END)",
    "NULLS LAST": "MySQL has no NULLS LAST; use ORDER BY col IS NULL, col",
    "NULLS FIRST": "MySQL has no NULLS FIRST",
    "QUALIFY": "MySQL and PostgreSQL have no QUALIFY; filter the window result in an outer query",
    "FULL JOIN": "MySQL has no FULL OUTER JOIN",
    "FULL OUTER JOIN": "MySQL has no FULL OUTER JOIN",
    "ILIKE": "MySQL has no ILIKE",
    "GENERATE_SERIES": "MySQL has no generate_series",
    "PERCENTILE_CONT": "MySQL has no PERCENTILE_CONT",
}

# Reserved words in MySQL 8 or PostgreSQL (or both). An identifier on this list is always quoted, and generated
# aliases never use one (e.g. ``revenue_rank``, not ``rank``).
RESERVED = frozenset("""
accessible add all alter analyze and array as asc asensitive authorization before between bigint binary blob both by
call cascade case cast change char character check collate collation column concurrently condition constraint continue
convert create cross cube cume_dist current current_catalog current_date current_role current_schema current_time
current_timestamp current_user cursor database databases date day_hour day_microsecond day_minute day_second dec decimal
declare default deferrable delayed delete dense_rank desc describe deterministic distinct distinctrow div do double drop
dual each else elseif empty enclosed end escaped except exists exit explain false fetch first_value float float4 float8
for force foreign freeze from full fulltext function generated get grant group grouping groups having high_priority
hour_microsecond hour_minute hour_second if ignore ilike in index infile initially inner inout insensitive insert int int1
int2 int3 int4 int8 integer intersect interval into io_after_gtids io_before_gtids is isnull iterate join json_table key
keys kill lag last_value lateral lead leading leave left like limit linear lines load localtime localtimestamp lock long
longblob longtext loop low_priority master_bind master_ssl_verify_server_cert match maxvalue mediumblob mediumint
mediumtext middleint minute_microsecond minute_second mod modifies natural not notnull no_write_to_binlog nth_value ntile
null numeric of offset on only optimize optimizer_costs option optionally or order out outer outfile over overlaps
partition percent_rank placing precision primary procedure purge range rank read reads read_write real recursive
references regexp release rename repeat replace require resignal restrict return returning revoke right rlike row rows
row_number schema schemas second_microsecond select sensitive separator set show signal similar smallint some spatial
specific sql sqlexception sqlstate sqlwarning sql_big_result sql_calc_found_rows sql_small_result ssl starting stored
straight_join symmetric system table tablesample terminated then tinyblob tinyint tinytext to trailing trigger true undo
union unique unlock unsigned update usage use user using utc_date utc_time utc_timestamp values varbinary varchar
varcharacter variadic varying verbose virtual when where while window with write xor year_month zerofill
""".split())

_PLAIN = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class Dialect:
    name: str

    def __post_init__(self):
        if self.name not in DIALECTS:
            raise ValueError(f"dialect must be one of {DIALECTS}, not {self.name!r}")

    @property
    def label(self) -> str:
        return LABELS[self.name]

    # ── Names and values ─────────────────────────────────────────────────
    def ident(self, name: str) -> str:
        """Quote an identifier only when it needs it (reserved word, capitals, spaces, symbols)."""
        name = str(name)
        if _PLAIN.match(name) and name not in RESERVED:
            return name
        if self.name == "mysql":
            return "`" + name.replace("`", "``") + "`"
        return '"' + name.replace('"', '""') + '"'

    def col(self, alias: str | None, name: str) -> str:
        return f"{alias}.{self.ident(name)}" if alias else self.ident(name)

    def literal(self, value) -> str:
        """A SQL literal. MySQL also treats a backslash inside quotes as an escape, so it is doubled there."""
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return repr(value)
        text = str(value).replace("'", "''")
        if self.name == "mysql":
            text = text.replace("\\", "\\\\")
        return f"'{text}'"

    def date_literal(self, iso: str) -> str:
        return f"DATE '{iso}'"  # standard SQL; accepted by all three

    def as_text(self, expr: str) -> str:
        return f"CAST({expr} AS {'CHAR' if self.name == 'mysql' else 'TEXT' if self.name == 'postgres' else 'VARCHAR'})"

    # ── Dates ────────────────────────────────────────────────────────────
    def days_between(self, start: str, end: str, timestamps: bool = False) -> str:
        """Whole days from ``start`` to ``end`` (positive when end is later)."""
        if self.name == "mysql":
            return f"DATEDIFF({end}, {start})"  # MySQL ignores the time part
        if not timestamps:
            return f"{end} - {start}"           # DATE - DATE is a whole number of days
        if self.name == "postgres":
            return f"{end}::date - {start}::date"  # timestamp - timestamp would be an INTERVAL
        return f"date_diff('day', {start}, {end})"

    def year(self, expr: str) -> str:
        return f"YEAR({expr})" if self.name == "mysql" else f"EXTRACT(YEAR FROM {expr})::int"

    def quarter(self, expr: str) -> str:
        return f"QUARTER({expr})" if self.name == "mysql" else f"EXTRACT(QUARTER FROM {expr})::int"

    def month(self, expr: str) -> str:
        return f"MONTH({expr})" if self.name == "mysql" else f"EXTRACT(MONTH FROM {expr})::int"

    def period(self, unit: str, expr: str) -> str:
        return {"quarter": self.quarter, "month": self.month}[unit](expr)

    # ── Numbers ──────────────────────────────────────────────────────────
    def round(self, expr: str, digits: int = 2) -> str:
        """PostgreSQL has no ROUND(double precision, int), so the value is cast to numeric first."""
        if self.name == "postgres":
            return f"ROUND(({expr})::numeric, {digits})"
        return f"ROUND({expr}, {digits})"

    def ratio(self, num: str, den: str, digits: int = 2) -> str:
        """num / den as a decimal. ``1.0 *`` stops PostgreSQL from truncating integer division."""
        return self.round(f"1.0 * {num} / NULLIF({den}, 0)", digits)

    def pct(self, num: str, den: str, digits: int = 1) -> str:
        """100 * num / den. ``100.0`` (not 100) keeps the ratio decimal in engines that truncate integer division."""
        return self.round(f"100.0 * {num} / NULLIF({den}, 0)", digits)

    def pct_change(self, new: str, old: str, digits: int = 1) -> str:
        return self.round(f"100.0 * ({new} - {old}) / NULLIF({old}, 0)", digits)

    # ── Statements ───────────────────────────────────────────────────────
    def explain(self, sql: str) -> str:
        return ("EXPLAIN FORMAT=TREE\n" if self.name == "mysql" else "EXPLAIN\n") + sql

    def type_name(self, logical: str, *, max_len: int = 0, scale: int = 0, big: bool = False, key: bool = False) -> str:
        """Column type for CREATE TABLE. Keys are never TEXT: MySQL can't index TEXT without a prefix length."""
        if logical == "integer":
            return "BIGINT" if big else ("INT" if self.name == "mysql" else "INTEGER")
        if logical == "decimal":
            return f"DECIMAL(18, {scale})" if self.name == "mysql" else f"NUMERIC(18, {scale})" if self.name == "postgres" else f"DECIMAL(18, {scale})"
        if logical == "float":
            return "DOUBLE PRECISION" if self.name == "postgres" else "DOUBLE"
        if logical == "boolean":
            return "BOOLEAN"
        if logical == "date":
            return "DATE"
        if logical == "timestamp":
            return "DATETIME" if self.name == "mysql" else "TIMESTAMP"
        size = next((n for n in (10, 20, 50, 100, 255, 1000) if max_len <= n), None)
        if size is None and not key:
            return "TEXT"
        return f"VARCHAR({size or 1000})"
