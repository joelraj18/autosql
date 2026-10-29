"""Time every stage of the pipeline on a synthetic upload.

    python scripts/benchmark.py              # 1,000,000 orders
    python scripts/benchmark.py 200000       # another size
    python scripts/benchmark.py northwind    # the worked example (8 tables)

The data: one orders table with an id, a customer, an amount, a date and a region, written to CSV first so that loading
goes through exactly the same path as an upload (or the Northwind CSV files, read as an upload).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autosql import build_cells, decide, infer_schema, profile_catalog, read_files, run_cells  # noqa: E402
from autosql.report import to_business_brief  # noqa: E402


def synthetic_orders(n: int, seed: int = 0) -> bytes:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({"order_id": range(n), "customer_id": rng.integers(1, 50_000, n), "amount": rng.uniform(1, 99, n).round(2),
                       "order_date": (pd.Timestamp("2019-01-01") + pd.to_timedelta(rng.integers(0, 1460, n), unit="D")).strftime("%Y-%m-%d"),
                       "region": rng.choice(list("NSEW"), n)})
    return df.to_csv(index=False).encode()


NORTHWIND = Path(__file__).resolve().parents[1] / "examples" / "northwind"


def main(files: list[tuple[str, bytes]]) -> dict[str, float]:
    t, times = time.perf_counter(), {}

    def lap(name: str) -> None:
        nonlocal t
        now = time.perf_counter()
        times[name] = now - t
        t = now

    catalog = read_files(files); lap("load")
    profile = profile_catalog(catalog); lap("profile")
    schema = infer_schema(catalog, profile); lap("relationships")
    d = decide(catalog, profile, schema); cells = build_cells(d); lap("decide + write SQL")
    _, metrics = run_cells(cells, catalog); lap(f"run {len(cells)} cells")
    to_business_brief(d, metrics); lap("brief")
    times["total"] = sum(times.values())
    return times


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "1000000"
    files = ([(p.name, p.read_bytes()) for p in sorted(NORTHWIND.glob("*.csv"))] if arg == "northwind" else
             [("orders.csv", synthetic_orders(int(arg)))])
    for name, seconds in main(files).items():
        print(f"{name:22} {seconds:7.2f} s")
