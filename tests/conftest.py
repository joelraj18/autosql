"""Fixtures: every catalog is built from CSV bytes, so the tests go through the same loading path as an upload."""
from __future__ import annotations

import io
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from autosql import decide, infer_schema, profile_catalog, read_files, read_folder

NORTHWIND = Path(__file__).resolve().parents[1] / "examples" / "northwind"
REQUIRE_LIVE = os.environ.get("AUTOSQL_REQUIRE_LIVE") == "1"   # CI: a live database that is missing fails the run instead of skipping


def live_unavailable(reason: str):
    """Skip a live-database test, or fail it when AUTOSQL_REQUIRE_LIVE=1, so that CI can't pass without running it."""
    (pytest.fail if REQUIRE_LIVE else pytest.skip)(reason)


def as_csv(df: pd.DataFrame) -> bytes:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode()


def catalog_from(frames: dict[str, pd.DataFrame]):
    return read_files([(f"{name}.csv", as_csv(df)) for name, df in frames.items()])


def analyse(catalog, mode="auto", picks=None, accept=()):
    profile = profile_catalog(catalog)
    schema = infer_schema(catalog, profile)
    if accept:
        schema = schema.with_accepted(accept)
    return decide(catalog, profile, schema, mode, picks)


def new_wheels_frame(seed: int = 0) -> pd.DataFrame:
    """SYNTHETIC, test-only: one denormalised orders table shaped like the New Wheels project (not its real data).

    Quarterly revenue falls while shipping time rises, so the engine has a real driver to find.
    """
    rng = np.random.default_rng(seed)
    n = 1000
    quarter = np.sort(rng.integers(1, 5, n))
    start = pd.to_datetime("2018-01-01") + pd.to_timedelta((quarter - 1) * 91 + rng.integers(0, 90, n), unit="D")
    ship_days = rng.normal(28 + (quarter - 1) * 6, 4).clip(5).round().astype(int)   # 28 -> 46 days
    feedback_p = {1: [0.05, 0.05, 0.2, 0.4, 0.3], 2: [0.1, 0.1, 0.3, 0.3, 0.2], 3: [0.2, 0.2, 0.3, 0.2, 0.1], 4: [0.3, 0.3, 0.2, 0.1, 0.1]}
    labels = ["Very Bad", "Bad", "Okay", "Good", "Very Good"]
    keep = rng.random(n) < np.choose(quarter - 1, [1.0, 0.85, 0.72, 0.6])               # fewer orders each quarter
    price = rng.uniform(15_000, 60_000, n).round(2)
    return pd.DataFrame({
        "order_id": [f"O{i:05d}" for i in range(n)],
        "customer_id": rng.integers(1, 985, n),
        "customer_state": rng.choice([f"S{i:02d}" for i in range(49)], n),
        "vehicle_maker": rng.choice(["Ford", "Chevrolet", "Toyota", "Honda", "Dodge"], n),
        "shipper_id": rng.integers(1, 6, n),
        "quantity": rng.integers(1, 3, n),
        "vehicle_price": price,
        "discount": rng.choice([0.0, 0.05, 0.1, 0.15], n),
        "order_date": start.strftime("%Y-%m-%d"),
        "ship_date": (start + pd.to_timedelta(ship_days, unit="D")).strftime("%Y-%m-%d"),
        "customer_feedback": [rng.choice(labels, p=feedback_p[q]) for q in quarter],
        "quarter_number": quarter,
    })[keep].reset_index(drop=True)


def shop_frames(seed: int = 1) -> dict[str, pd.DataFrame]:
    """A small normalised shop with the problems real extracts have."""
    rng = np.random.default_rng(seed)
    customers = pd.DataFrame({
        "CustomerID": range(1, 101),
        "Customer Name": [f"Customer {i}" for i in range(1, 101)],
        "Region": rng.choice(["North", "South", "East", "West"], 100),
        "Zip": [f"0{rng.integers(1000, 9999)}" for _ in range(100)],      # leading zeros must survive
        "group": rng.choice(["retail", "trade"], 100),                      # a reserved word as a column name
    })
    customers = pd.concat([customers, customers.iloc[[3, 7]]])          # two duplicated customer keys
    products = pd.DataFrame({"ProductID": range(1, 21), "Product": [f"P{i}" for i in range(1, 21)],
                             "Category": rng.choice(["Toys", "Books", "Garden"], 20), "UnitCost": rng.uniform(1, 9, 20).round(2)})
    days = rng.integers(0, 540, 400)
    ordered = pd.to_datetime("2022-01-01") + pd.to_timedelta(days, unit="D")
    shipped = ordered + pd.to_timedelta(rng.integers(1, 15, 400), unit="D")
    orders = pd.DataFrame({
        "OrderID": range(5001, 5401), "CustomerID": rng.integers(1, 101, 400),
        "OrderDate": ordered.strftime("%m/%d/%Y"),
        "ShipDate": pd.Series(shipped.strftime("%m/%d/%Y")).where(rng.random(400) > 0.05),
        "order": rng.choice(["web", "phone"], 400),                         # another reserved word
        "Freight": rng.uniform(5, 50, 400).round(2),
    })
    items = pd.DataFrame({"OrderID": rng.choice(orders.OrderID, 1000), "ProductID": rng.integers(1, 21, 1000)}).drop_duplicates()
    items["Qty"] = rng.integers(1, 6, len(items))
    items["Price"] = rng.uniform(2, 20, len(items)).round(2)
    orphans = pd.DataFrame({"OrderID": [9998, 9999, 9999], "ProductID": [1, 2, 3], "Qty": [1, 1, 1], "Price": [5.0, 6.0, 7.0]})
    return {"customers": customers, "products": products, "orders": orders, "order_items": pd.concat([items, orphans])}


@pytest.fixture(scope="session")
def northwind_catalog():
    return read_folder(NORTHWIND, "*.csv")


@pytest.fixture(scope="session")
def northwind(northwind_catalog):
    return analyse(northwind_catalog)


@pytest.fixture(scope="session")
def new_wheels_catalog():
    return catalog_from({"orders": new_wheels_frame()})


@pytest.fixture(scope="session")
def shop_catalog():
    return catalog_from(shop_frames())
