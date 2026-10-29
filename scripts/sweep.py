"""Stress sweep: run the whole pipeline on every awkward dataset in tests/datasets.py, in auto and validate mode.

    python scripts/sweep.py          # add --big for a 200,000-row table

For each dataset it prints the decision, the number of cells, errors, cells that ran without explaining their result, the
trust score and the headline, and finally every problem found. It also writes each business brief to scripts/briefs/.
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from autosql import build_cells, run_cells  # noqa: E402
from autosql.business import data_dictionary_markdown, executive_summary  # noqa: E402
from autosql.exports import to_notebook, to_project_zip, to_sql_script  # noqa: E402
from autosql.report import to_business_brief  # noqa: E402
from conftest import analyse, catalog_from  # noqa: E402
from datasets import sweep_cases  # noqa: E402


def main(big: bool) -> int:
    out = Path(__file__).resolve().parent / "briefs"
    out.mkdir(exist_ok=True)
    problems = []
    for name, frames in sweep_cases(big).items():
        t0 = time.time()
        try:
            catalog = catalog_from(frames)
        except Exception as error:  # noqa: BLE001  (a sweep reports every failure)
            problems.append((name, "load", repr(error)))
            continue
        for mode in ("auto", "validate"):
            try:
                d = analyse(catalog, mode=mode)
                if d.halted:
                    print(f"{name:22} {mode:8} HALT: {d.steps[-1].key}")
                    continue
                cells = build_cells(d)
                results, metrics = run_cells(cells, catalog)
                errors = [(r.cell_id, r.error) for r in results if r.error]
                silent = [r.cell_id for r in results if not r.notes and not r.error and not r.skipped]
                to_sql_script(cells, d, "mysql")
                to_project_zip(cells, d, catalog, "postgres", metrics=metrics)
                to_notebook(cells, d, catalog)
                summary = executive_summary(d, metrics, name)
                data_dictionary_markdown(d)
                (out / f"{name}_{mode}.html").write_text(to_business_brief(d, metrics, title=name), encoding="utf-8")
                print(f"{name:22} {mode:8} {d.analysis:8} cells={len(cells):2} errors={len(errors)} silent={silent} "
                      f"trust={summary.trust.score:.0f} {time.time() - t0:.1f}s\n    {summary.headline[:150]}")
                problems += [(name, mode, e) for e in errors]
                problems += [(name, mode, f"silent: {c}") for c in silent]
            except Exception:  # noqa: BLE001
                problems.append((name, mode, traceback.format_exc()[-1500:]))
    print(f"\nPROBLEMS: {len(problems)}")
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main("--big" in sys.argv))
