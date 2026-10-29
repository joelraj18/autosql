"""Run the generated cells like a notebook: one database, one shared namespace for the checks, captured output.

Each cell's statements run in order on a fresh in-memory DuckDB built from the uploaded tables (locked, so queries
can't read local files). Statements marked ``scratch`` run on a separate empty database (the CREATE TABLE check);
statements marked ``show`` are never run (dialect-only demos). Then the cell's Python check reads the results,
draws its charts and writes the 📌 findings with ``note()``.
"""
from __future__ import annotations

import contextlib
import io
import threading
import time
import traceback
import warnings
from dataclasses import dataclass, field
from typing import Callable

import duckdb
import matplotlib

matplotlib.use("Agg")  # render to images, never open windows
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .codegen import CHART_SETUP, CHECK_HELPERS, Cell  # noqa: E402
from .loading import Catalog, build_database  # noqa: E402

STATEMENT_TIMEOUT_S = 60


@dataclass
class StatementResult:
    label: str
    title: str
    frame: pd.DataFrame | None = None     # None for statements that were not run
    rows: int = 0
    seconds: float = 0.0
    run: str = "query"
    error: str | None = None              # only for a soft statement (your own SQL) that failed: the cell goes on without its result


@dataclass
class CellResult:
    cell_id: str
    statements: list[StatementResult] = field(default_factory=list)
    stdout: str = ""
    figures: list[bytes] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # 📌 "what we found" lines written by the check's note() calls
    error: str | None = None
    seconds: float = 0.0
    skipped: bool = False


@dataclass
class DryRun:
    """What your own SQL runs into, found on empty copies of the tables (nothing is read): the columns of the analysis views, and the
    error of each of your statements that fails."""
    columns: dict[str, list[tuple[str, str]]] = field(default_factory=dict)    # view -> [(column, type)]
    errors: dict[str, str] = field(default_factory=dict)                       # label of your statement -> why it fails
    error: str | None = None                                                   # the views themselves could not be built


def _grab_figures() -> list[bytes]:
    images = []
    for n in plt.get_fignums():
        buf = io.BytesIO()
        plt.figure(n).savefig(buf, format="png", dpi=110, bbox_inches="tight")
        images.append(buf.getvalue())
    plt.close("all")
    return images


def execute(con: duckdb.DuckDBPyConnection, sql: str, timeout: float = STATEMENT_TIMEOUT_S) -> pd.DataFrame:
    """Run one statement; a timer interrupts it after ``timeout`` seconds."""
    timer = threading.Timer(timeout, con.interrupt)
    timer.start()
    try:
        cursor = con.execute(sql)
        return cursor.df() if cursor.description else pd.DataFrame()
    except duckdb.InterruptException as exc:
        raise TimeoutError(f"the query took longer than {timeout:.0f} seconds") from exc
    finally:
        timer.cancel()


def dry_run(cells: list[Cell], catalog: Catalog) -> DryRun:
    """Build the analysis views and run your KPI statements on tables with the same columns and no rows: seconds, whatever the size of
    the data, and it finds every column that doesn't exist and every function the database doesn't know. What only the data can show
    (a division by a zero that is really there) appears in the real run, next to the KPI."""
    base = next((c for c in cells if c.id == "base"), None)
    if base is None:
        return DryRun(error="There are no analysis views to check against: the analysis was not built.")
    out, con = DryRun(), build_database(catalog, empty=True)
    try:
        try:
            for st in base.statements:
                if st.label in ("order_facts", "line_facts"):
                    execute(con, st.sql["duckdb"])
                    out.columns[st.label] = [(name, kind) for name, kind, *_ in con.execute(f"DESCRIBE {st.label}").fetchall()]
        except Exception as exc:
            out.error = _soft_error(exc)
            return out
        for cell in cells:
            for st in cell.statements:
                if st.soft:
                    try:
                        frame = execute(con, st.sql["duckdb"])
                    except Exception as exc:
                        out.errors[st.label] = _soft_error(exc)
                        continue
                    # an aggregate over no rows is still one row; an expression that isn't an aggregate gives none
                    if not st.label.endswith("_by_period") and len(frame) != 1:
                        out.errors[st.label] = ("the expression is not an aggregate: over empty tables it returned no row, where SUM, "
                                                "COUNT, AVG and the like return one")
    finally:
        con.close()
    return out


# matplotlib's pyplot keeps one global list of figures, and we temporarily replace plt.show: two users pressing
# "Run" at the same moment on a shared server would mix their charts. Runs therefore take turns.
_RUN_LOCK = threading.Lock()


def run_cells(*args, **kwargs) -> tuple[list[CellResult], dict]:
    """Run cells in order (see `_run_cells`); only one run at a time per process."""
    with _RUN_LOCK:
        return _run_cells(*args, **kwargs)


def _run_cells(cells: list[Cell], catalog: Catalog, on_cell: Callable[[int, Cell], None] | None = None) -> tuple[list[CellResult], dict]:
    con = build_database(catalog)
    ns: dict = {"pd": pd, "np": np, "plt": plt, "metrics": {}, "source": {name: t.df for name, t in catalog.tables.items()},
                "previous": {}}
    exec(CHART_SETUP, ns)  # the palette and axis style every check's charts use
    exec(CHECK_HELPERS, ns)  # period labels and statistics the checks call (autosql/checklib.py)
    results: list[CellResult] = []
    failed = False
    plt.close("all")
    shown: list[bytes] = []
    real_show = plt.show
    plt.show = lambda *a, **k: shown.extend(_grab_figures())
    try:
        for i, cell in enumerate(cells):
            if failed:
                results.append(CellResult(cell.id, skipped=True))
                continue
            if on_cell:
                on_cell(i, cell)
            results.append(_run_one(cell, con, ns, shown))
            failed = results[-1].error is not None
    finally:
        plt.show = real_show
        con.close()
    metrics = ns.get("metrics", {})
    return results, dict(metrics) if isinstance(metrics, dict) else {}


def _soft_error(exc: Exception) -> str:
    """What went wrong in SQL somebody wrote, in one line: DuckDB's message, minus the echo of the whole query."""
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return f"{type(exc).__name__}: {first}"


def _run_one(cell: Cell, con: duckdb.DuckDBPyConnection, ns: dict, shown: list[bytes]) -> CellResult:
    res = CellResult(cell.id)
    shown.clear()
    start = time.perf_counter()
    outputs: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}           # soft statements that failed: label -> why (the check reports them)
    scratch = None
    current = None
    try:
        for st in cell.statements:
            current = st
            sr = StatementResult(st.label, st.title, run=st.run)
            if st.run != "show":
                t0 = time.perf_counter()
                target = con
                if st.run == "scratch":
                    scratch = scratch or duckdb.connect(":memory:")
                    target = scratch
                try:
                    frame = execute(target, st.sql["duckdb"])
                except Exception as exc:
                    if not st.soft:
                        raise
                    sr.error = errors[st.label] = _soft_error(exc)
                    res.statements.append(sr)
                    continue
                sr.frame, sr.rows, sr.seconds = frame, len(frame), time.perf_counter() - t0
                outputs[st.label] = frame
            res.statements.append(sr)
        current = None
        ns["outputs"], ns["errors"] = outputs, errors
        ns["result"] = next((outputs[st.label] for st in reversed(cell.statements) if st.label in outputs), pd.DataFrame())
        ns["note"] = res.notes.append
        out = io.StringIO()
        with contextlib.redirect_stdout(out), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            exec(compile(cell.check, f"<check {cell.id}>", "exec"), ns)
        res.stdout = out.getvalue()
        ns["previous"][cell.id] = outputs
    except Exception as exc:  # show the error in the UI instead of crashing the app
        where = f" in “{current.title}”" if current is not None else " in the check code"
        res.error = f"{type(exc).__name__}{where}: {exc}"
        res.stdout = traceback.format_exc(limit=-3)
    finally:
        if scratch is not None:
            scratch.close()
    res.seconds = time.perf_counter() - start
    res.figures = shown + _grab_figures()
    return res
