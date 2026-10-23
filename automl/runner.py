"""Execute generated cells like a notebook: shared namespace, captured output."""
from __future__ import annotations

import contextlib
import io
import threading
import time
import traceback
import warnings
from dataclasses import dataclass, field
from typing import Callable

import matplotlib

matplotlib.use("Agg")  # render to images, never open windows
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from .codegen import Cell  # noqa: E402


@dataclass
class CellResult:
    cell_id: str
    stdout: str = ""
    figures: list[bytes] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # 📌 "what we found" lines written by the cell's note() calls
    error: str | None = None
    seconds: float = 0.0
    skipped: bool = False


def _grab_figures() -> list[bytes]:
    images = []
    for n in plt.get_fignums():
        buf = io.BytesIO()
        plt.figure(n).savefig(buf, format="png", dpi=110, bbox_inches="tight")
        images.append(buf.getvalue())
    plt.close("all")  # free memory right away
    return images


# matplotlib's pyplot keeps one global list of figures, and we temporarily replace plt.show: two users pressing
# "Run" at the same moment on a shared server would mix their charts. Runs therefore take turns.
_RUN_LOCK = threading.Lock()


def run_cells(*args, **kwargs) -> tuple[list[CellResult], dict]:
    """Run cells in order in one namespace (see `_run_cells`); only one run at a time per process."""
    with _RUN_LOCK:
        return _run_cells(*args, **kwargs)


def _run_cells(
    cells: list[Cell],
    df: pd.DataFrame,
    on_cell: Callable[[int, Cell], None] | None = None,
) -> tuple[list[CellResult], dict]:
    """Run cells in order in one namespace. After an error, later cells are skipped."""
    ns: dict = {"df": df}
    results: list[CellResult] = []
    failed = False
    plt.close("all")
    shown: list[bytes] = []
    real_show = plt.show
    # Like a notebook: plt.show() emits the figure and closes it, so the next plot starts fresh.
    plt.show = lambda *a, **k: shown.extend(_grab_figures())
    try:
        for i, cell in enumerate(cells):
            if failed:
                results.append(CellResult(cell.id, skipped=True))
                continue
            if on_cell:
                on_cell(i, cell)
            results.append(_run_one(cell, ns, shown))
            failed = results[-1].error is not None
    finally:
        plt.show = real_show
    metrics = ns.get("metrics", {})
    return results, dict(metrics) if isinstance(metrics, dict) else {}


def _run_one(cell: Cell, ns: dict, shown: list[bytes]) -> CellResult:
    res = CellResult(cell.id)
    out = io.StringIO()
    shown.clear()
    ns["note"] = res.notes.append  # the setup cell defines note() for Jupyter; in the app we collect the notes instead
    start = time.perf_counter()
    try:
        with contextlib.redirect_stdout(out), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            exec(compile(cell.code, f"<cell {cell.id}>", "exec"), ns)
    except Exception as exc:  # show the error in the UI instead of crashing the app
        res.error = f"{type(exc).__name__}: {exc}"
        tb = traceback.format_exc(limit=-2)
        res.stdout = out.getvalue() + ("\n" if out.getvalue() else "") + tb
    else:
        res.stdout = out.getvalue()
    res.seconds = time.perf_counter() - start
    res.figures = shown + _grab_figures()  # figures shown, plus any left open without plt.show()
    return res

