"""Your own assumptions and KPIs, as data: what the engine can't know, checked before anything uses it.

A customization is plain data (a Python object, or JSON that a person can write by hand)::

    {
      "assumptions": {
        "add":  ["Figures are in US dollars."],
        "edit": {"revenue": "Revenue = price x quantity x (1 - discount); freight is not included."}
      },
      "kpis": [
        {"key": "freight_share", "name": "Freight share of revenue",
         "definition": "Freight charged as a share of revenue.",
         "sql": "100.0 * SUM(freight) / NULLIF(SUM(revenue), 0)", "unit": "pct", "direction": "down"}
      ]
    }

* ``assumptions.add`` appends statements to the assumptions list; ``assumptions.edit`` rewrites the engine's own statement with that
  key (the keys are listed by ``assumption_items``, and a statement you changed is marked "edited by you" wherever it appears).
  A list instead of the object is shorthand for ``add``. The text is documentation: to change what the numbers *are* (the fact
  table, the revenue formula, the date, the rules that leave rows out) pick them in manual mode, and the assumptions follow.
* Each KPI's ``sql`` is one aggregate expression over the analysis view ``order_facts`` (one row per order). The engine runs it for
  all periods and for each period, on the same rows as the standard KPIs, and shows the result next to them, labelled as yours.
  Without ``sql`` a KPI is a definition only. ``sql`` may also be an object with a ``default`` expression and one for ``mysql``,
  ``postgres`` or ``duckdb`` where the dialects differ.

Nothing here imports the rest of the package, so a customization can be read, checked and shown before any data is loaded.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .dialect import BANNED

KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
UNITS = ("number", "money", "pct", "days", "score")
DIRECTIONS = ("up", "down", "context")
SQL_KEYS = ("default", "mysql", "postgres", "duckdb")
MAX_TEXT = 600
MAX_SQL = 1200
MAX_KPIS = 20       # each one is two statements on the whole data
# One expression, not a script: no second statement, no comment that could swallow the rest, nothing that means different things in
# MySQL and PostgreSQL (backticks, backslash escapes)
NOT_ONE_EXPRESSION = re.compile(r";|--|/\*|\*/|`|\\")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")        # 'text' ('' inside) or "identifier": data, not SQL
INNERMOST = re.compile(r"\([^()]*\)")
# A division that is neither by NULLIF(x, 0) nor by a decimal number: PostgreSQL stops with an error on a zero divisor, and integers divide
# differently (5 / 2 is 2.5 in MySQL and DuckDB, 2 in PostgreSQL). The engine's own SQL never writes one (tests/test_codegen_runs.py).
BARE_DIVISION = re.compile(r"/(?!\s*(?:NULLIF\s*\(|\d+\.\d))", re.I)
# Words that start another query. Inside parentheses they are fine (a sub-query, EXTRACT(YEAR FROM x), ... WITHIN GROUP (ORDER BY x));
# outside, the expression would be a second query. FROM is allowed after DISTINCT (IS DISTINCT FROM), GROUP after WITHIN.
CLAUSES = frozenset("select union intersect except into join where having limit offset fetch window returning from group order".split())
# Functions that wait, write or read what is outside the tables. A guard against accident, not a security boundary: the app runs your SQL
# on a locked, in-memory copy of your own files; this keeps the scripts you export from carrying such calls to a real database.
OUTSIDE = frozenset("""pg_sleep sleep benchmark load_file pg_read_file pg_read_binary_file pg_ls_dir pg_terminate_backend pg_cancel_backend
lo_import lo_export dblink nextval setval set_config get_lock release_lock read_csv read_parquet read_json glob""".split())


class CustomizationError(ValueError):
    """Everything that is wrong with a customization, one problem per line, in words for the person who wrote it."""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("\n".join(self.problems))


def _text(value, what: str, limit: int = MAX_TEXT, required: bool = True, multiline: bool = False) -> tuple[str, list[str]]:
    """Clean text and what is wrong with it. Wording goes into SQL comments and table cells, so it is one line of printable characters."""
    if not isinstance(value, str):
        return "", [f"{what} must be text, not {type(value).__name__}."] if (value is not None or required) else []
    value = value.strip()
    if not value:
        return "", [f"{what} can't be empty."] if required else []
    if len(value) > limit:
        return value[:limit], [f"{what} is {len(value):,} characters; the limit is {limit:,}."]
    if CONTROL.search(value.replace("\n", " ").replace("\t", " ") if multiline else value):
        return value, [f"{what} must be {'plain text' if multiline else 'one line of plain text'}, without control characters."]
    return value, []


def _expression_problems(text: str, what: str) -> list[str]:
    """Is this one scalar expression? Quotes and parentheses balance, no clause of another query stands outside parentheses, and no
    call reaches out of the tables."""
    if NOT_ONE_EXPRESSION.search(text):
        return [f"{what} must be one expression: no semicolons, comments, backticks or backslashes."]
    bare = QUOTED.sub(" ", text)
    if "'" in bare or '"' in bare:
        return [f"{what} has a quote that is never closed."]
    top, depth = bare, 0
    for ch in bare:
        depth += (ch == "(") - (ch == ")")
        if depth < 0:
            return [f"{what} has a closing parenthesis with no opening one."]
    if depth:
        return [f"{what} has an opening parenthesis that is never closed."]
    while INNERMOST.search(top):
        top = INNERMOST.sub(" ", top)
    words = [w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", top)]
    for i, word in enumerate(words):
        previous = words[i - 1] if i else ""
        if word in CLAUSES and not (word == "from" and previous == "distinct") and not (word == "group" and previous == "within"):
            return [f"{what} must be one expression: {word.upper()} starts another query (put a sub-query in parentheses)."]
    found = sorted({w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", bare)} & OUTSIDE)
    return [f"{what} calls {', '.join(f.upper() for f in found)}, which reaches outside the tables." ] if found else []


@dataclass(frozen=True)
class CustomKPI:
    """One KPI of your own. ``sql`` is the expression text, or a mapping dialect -> text; once built it is a sorted tuple of pairs."""
    key: str
    name: str
    definition: str
    sql: "str | Mapping[str, str] | tuple[tuple[str, str], ...] | None" = None
    unit: str = "number"
    direction: str = "context"
    why: str = ""
    pitfall: str = ""
    decision: str = ""

    def __post_init__(self):
        problems = []
        if not isinstance(self.key, str) or not KEY.match(self.key):
            problems.append("key must be lower case letters, digits and underscores, starting with a letter (at most 40), e.g. freight_share.")
        elif self.key.endswith("_by_period"):
            problems.append("key can't end with _by_period: that is how the per-period statement of a KPI is named.")
        for field_name in ("name", "definition"):
            problems += _text(getattr(self, field_name), field_name)[1]
        for field_name in ("why", "pitfall", "decision"):
            problems += _text(getattr(self, field_name), field_name, required=False)[1]
        if self.unit not in UNITS:
            problems.append(f"unit must be one of {', '.join(UNITS)}, not {self.unit!r}.")
        if self.direction not in DIRECTIONS:
            problems.append(f"direction must be one of {', '.join(DIRECTIONS)} (which way is good), not {self.direction!r}.")
        pairs, sql_problems = self._expressions(self.sql)
        problems += sql_problems
        if problems:
            raise CustomizationError(problems)
        object.__setattr__(self, "sql", pairs)
        for field_name in ("name", "definition", "why", "pitfall", "decision"):
            object.__setattr__(self, field_name, (getattr(self, field_name) or "").strip())

    @staticmethod
    def _expressions(sql) -> tuple[tuple[tuple[str, str], ...], list[str]]:
        if sql is None or sql == () or sql == {}:
            return (), []
        if isinstance(sql, str):
            sql = {"default": sql}
        elif isinstance(sql, tuple):
            sql = dict(sql)
        if not isinstance(sql, Mapping):
            return (), [f"sql must be an expression or an object of expressions, not {type(sql).__name__}."]
        problems, out = [], {}
        for dialect, text in sql.items():
            if dialect not in SQL_KEYS:
                problems.append(f"sql has the key {dialect!r}; the keys are {', '.join(SQL_KEYS)}.")
                continue
            text, found = _text(text, f"sql ({dialect})", MAX_SQL, multiline=True)
            problems += found
            if text and not found:
                problems += _expression_problems(text, f"sql ({dialect})")
            out[dialect] = " ".join(text.split())
        if not problems and "default" not in out and not {"mysql", "postgres"} <= set(out):
            problems.append("sql needs a 'default' expression, or one each for 'mysql' and 'postgres'.")
        return tuple(sorted(out.items())), problems

    @property
    def runnable(self) -> bool:
        return bool(self.sql)

    @property
    def warnings(self) -> tuple[str, ...]:
        """What would make the number differ between the database you run it on and the one that shows it here (DuckDB), or fail there.
        Not errors: the expression still runs; these are the traps the engine's own SQL avoids, said out loud for yours."""
        out = []
        for dialect, text in self.sql:
            bare = QUOTED.sub(" ", text)                       # a / inside a string is data
            if dialect != "duckdb" and BARE_DIVISION.search(bare):
                out.append("It divides with / by something other than NULLIF(x, 0) or a decimal number: PostgreSQL stops with an error "
                           "when the divisor is zero, and whole numbers divide differently (5 / 2 is 2.5 in MySQL, 2 in PostgreSQL). "
                           "Write 100.0 * a / NULLIF(b, 0).")
            if dialect in ("default", "mysql"):               # run on MySQL: what MySQL lacks matters
                shape = re.sub(r"\s*\(\s*", "(", " ".join(bare.upper().split()))
                out += [f"{why}." for construct, why in BANNED.items() if re.sub(r"\s*\(\s*", "(", construct) in shape]
                if "::" in bare:
                    out.append("`::` casts are PostgreSQL only: write CAST(x AS type), which every database reads.")
        return tuple(dict.fromkeys(out))

    def expression(self, dialect: str) -> str | None:
        """The expression for a dialect: its own, else the default. DuckDB (which runs it here) then borrows PostgreSQL's, then MySQL's."""
        found = dict(self.sql)
        order = {"duckdb": ("duckdb", "default", "postgres", "mysql")}.get(dialect, (dialect, "default"))
        return next((found[k] for k in order if k in found), None)

    def formula(self) -> str:
        """The expression as text for a table: one expression, or one per dialect, or a note that there is no SQL."""
        found = dict(self.sql)
        if not found:
            return "(a definition only: no SQL)"
        if list(found) == ["default"]:
            return found["default"]
        return "; ".join(f"{dialect}: {text}" for dialect, text in found.items())

    def to_dict(self) -> dict:
        found = dict(self.sql)
        out: dict = {"key": self.key, "name": self.name, "definition": self.definition}
        if found:
            out["sql"] = found["default"] if list(found) == ["default"] else found
        out.update(unit=self.unit, direction=self.direction)
        out.update({k: getattr(self, k) for k in ("why", "pitfall", "decision") if getattr(self, k)})
        return out


@dataclass(frozen=True)
class Customization:
    assumptions_add: tuple[str, ...] = ()
    assumptions_edit: tuple[tuple[str, str], ...] = ()      # (key of the engine's statement, your text)
    kpis: tuple[CustomKPI, ...] = field(default_factory=tuple)

    @property
    def empty(self) -> bool:
        return not (self.assumptions_add or self.assumptions_edit or self.kpis)

    @property
    def edits(self) -> dict[str, str]:
        return dict(self.assumptions_edit)

    @property
    def runnable_kpis(self) -> tuple[CustomKPI, ...]:
        return tuple(k for k in self.kpis if k.runnable)

    @classmethod
    def from_dict(cls, data) -> "Customization":
        """Check everything and say everything that is wrong at once; nothing is silently ignored (a typo in a key is an error)."""
        if not isinstance(data, dict):
            raise CustomizationError("A customization is an object with 'assumptions' and/or 'kpis'.")
        problems = [f"Unknown key {k!r}: the keys are 'assumptions' and 'kpis'." for k in data if k not in ("assumptions", "kpis")]

        raw = data.get("assumptions") or {}
        add_raw, edit_raw = [], {}
        if isinstance(raw, list):
            add_raw = raw
        elif isinstance(raw, dict):
            problems += [f"assumptions has the key {k!r}: the keys are 'add' and 'edit'." for k in raw if k not in ("add", "edit")]
            add_raw, edit_raw = raw.get("add") or [], raw.get("edit") or {}
        else:
            problems.append("assumptions must be a list of statements, or an object with 'add' and 'edit'.")
        if not isinstance(add_raw, list):
            problems.append("assumptions.add must be a list of statements.")
            add_raw = []
        if not isinstance(edit_raw, dict):
            problems.append("assumptions.edit must be an object: the key of an assumption, then your text.")
            edit_raw = {}
        add = []
        for i, text in enumerate(add_raw, 1):
            clean, found = _text(text, f"assumptions.add #{i}")
            problems += found
            add.append(clean)
        edit = []
        for key, text in edit_raw.items():
            clean, found = _text(text, f"assumptions.edit[{key!r}]")
            problems += found
            if not str(key).strip():
                problems.append("assumptions.edit has an empty key.")
            edit.append((str(key).strip(), clean))

        kpis, seen = [], set()
        raw_kpis = data.get("kpis") or []
        if not isinstance(raw_kpis, list):
            problems.append("kpis must be a list of KPI objects.")
            raw_kpis = []
        elif len(raw_kpis) > MAX_KPIS:
            problems.append(f"There are {len(raw_kpis)} KPIs; the limit is {MAX_KPIS}: each one is run on all the data, twice.")
            raw_kpis = raw_kpis[:MAX_KPIS]
        for i, item in enumerate(raw_kpis, 1):
            where = f"kpis #{i}" + (f" ({item['key']})" if isinstance(item, dict) and isinstance(item.get("key"), str) else "")
            if not isinstance(item, dict):
                problems.append(f"{where} must be an object with a key, a name, a definition and, to compute it, a sql expression.")
                continue
            extra = [k for k in item if k not in ("key", "name", "definition", "sql", "unit", "direction", "why", "pitfall", "decision")]
            problems += [f"{where} has the key {k!r}, which isn't one of key, name, definition, sql, unit, direction, why, pitfall, decision." for k in extra]
            missing = [k for k in ("key", "name", "definition") if k not in item]
            if missing:
                problems.append(f"{where} is missing {', '.join(missing)}.")
                continue
            try:
                kpi = CustomKPI(**{k: v for k, v in item.items() if k not in extra})
            except CustomizationError as error:
                problems += [f"{where}: {p}" for p in error.problems]
                continue
            if kpi.key in seen:
                problems.append(f"{where}: the key {kpi.key!r} is used twice.")
            seen.add(kpi.key)
            kpis.append(kpi)
        if problems:
            raise CustomizationError(problems)
        return cls(tuple(add), tuple(edit), tuple(kpis))

    @classmethod
    def from_json(cls, text: str) -> "Customization":
        if not text or not text.strip():
            return cls()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise CustomizationError(f"This is not valid JSON: {error.msg} (line {error.lineno}, column {error.colno}).") from error
        return cls.from_dict(data)

    def to_dict(self) -> dict:
        out: dict = {}
        if self.assumptions_add or self.assumptions_edit:
            out["assumptions"] = {k: v for k, v in (("add", list(self.assumptions_add)), ("edit", dict(self.assumptions_edit))) if v}
        if self.kpis:
            out["kpis"] = [k.to_dict() for k in self.kpis]
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


def example(revenue: bool) -> Customization:
    """A starting point that is valid for any analysis and true as it stands: one assumption of your own, and one KPI over columns
    that every ``order_facts`` has (revenue when the analysis has money, else the order count). Edit it, or replace it."""
    kpi = (CustomKPI("big_order_share", "Share of big orders",
                     "Orders worth more than twice the average order, as a share of all orders: how much the business depends on large orders.",
                     "100.0 * SUM(CASE WHEN revenue > 2 * (SELECT AVG(revenue) FROM order_facts) THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0)",
                     "pct", "context", why="A rising share means a few large orders carry more of the revenue.",
                     pitfall="The average itself is pulled up by large orders, so the share is only a rough guide.")
           if revenue else
           CustomKPI("order_count", "Orders (again)", "Orders placed in the period: the simplest KPI of your own.", "COUNT(*)", "number", "up"))
    return Customization(("Amounts are shown as they are in the source data: nothing is converted between currencies.",), (), (kpi,))
