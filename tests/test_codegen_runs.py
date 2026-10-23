"""Every generated cell must run, and every MySQL / PostgreSQL statement must respect the dialect rules."""
import re

import numpy as np
import pandas as pd
import pytest
import sqlglot

from autosql import build_cells, run_cells
from autosql.dialect import BANNED, RESERVED
from conftest import analyse, catalog_from, new_wheels_frame, shop_frames


def timestamps_frame():
    rng = np.random.default_rng(3)
    start = pd.Timestamp("2021-01-01") + pd.to_timedelta(rng.integers(0, 600 * 24, 300), unit="h")
    return pd.DataFrame({"order_id": range(300), "customer_id": rng.integers(1, 40, 300),
                         "created_at": start.strftime("%Y-%m-%d %H:%M:%S"),
                         "delivered_at": (start + pd.to_timedelta(rng.integers(20, 200, 300), unit="h")).strftime("%Y-%m-%d %H:%M:%S"),
                         "amount": rng.uniform(5, 500, 300).round(2), "channel": rng.choice(["app", "web"], 300),
                         "rating": rng.integers(1, 6, 300)})


def list_price_frames():
    """Quantity in the fact table, price only in the product table (list price)."""
    rng = np.random.default_rng(4)
    products = pd.DataFrame({"product_id": range(1, 11), "product_name": [f"Item {i}" for i in range(1, 11)],
                             "category": rng.choice(["A", "B"], 10), "list_price": rng.uniform(1, 50, 10).round(2)})
    days = pd.Timestamp("2022-01-01") + pd.to_timedelta(rng.integers(0, 400, 500), unit="D")
    sales = pd.DataFrame({"sale_id": range(500), "product_id": rng.integers(1, 11, 500), "quantity": rng.integers(1, 4, 500),
                          "sale_date": days.strftime("%Y-%m-%d"), "store": rng.choice(["North", "South"], 500)})
    return {"products": products, "sales": sales}


def denormalised_lines():
    """One table of order lines with no separate orders table: order-level columns repeat on every line."""
    rng = np.random.default_rng(5)
    orders = pd.DataFrame({"invoice_no": range(1000, 1200), "customer_id": rng.integers(1, 50, 200),
                           "invoice_date": (pd.Timestamp("2010-12-01") + pd.to_timedelta(rng.integers(0, 365, 200), unit="D")).strftime("%Y-%m-%d"),
                           "country": rng.choice(["UK", "France", "Spain"], 200)})
    lines = orders.loc[orders.index.repeat(rng.integers(1, 5, 200))].reset_index(drop=True)
    lines["stock_code"] = rng.integers(100, 140, len(lines))
    lines["quantity"] = rng.integers(1, 10, len(lines))
    lines["unit_price"] = rng.uniform(0.5, 9, len(lines)).round(2)
    return {"online_retail": lines.drop_duplicates(["invoice_no", "stock_code"])}


def awkward_names_frames():
    """Reserved words, capitals, symbols and quotes in names and values."""
    rng = np.random.default_rng(6)
    return {"Order": pd.DataFrame({
        "Order ID": range(1, 121), "select": rng.choice(["it's", 'say "hi"', "back\\slash", "{x}"], 120),
        "Group": rng.choice(["x", "y"], 120), "Date": pd.date_range("2022-01-05", periods=120, freq="5D").strftime("%Y-%m-%d"),
        "Amount": rng.uniform(1, 9, 120).round(2), "user": rng.integers(1, 20, 120)})}


CASES = {
    "northwind": (None, "auto", ()),
    "northwind_validate": (None, "validate", ()),
    "northwind_ticked": (None, "auto", ("orders.ship_via → shippers.shipper_id",)),
    "new_wheels": ({"orders": new_wheels_frame}, "auto", ()),
    "shop": (shop_frames, "auto", ()),
    "shop_validate": (shop_frames, "validate", ()),
    "timestamps": ({"deliveries": timestamps_frame}, "auto", ()),
    "list_price": (list_price_frames, "auto", ()),
    "denormalised_lines": (denormalised_lines, "auto", ()),
    "awkward_names": (awkward_names_frames, "auto", ()),
}


def build(case, northwind_catalog):
    frames, mode, accept = CASES[case]
    if frames is None:
        catalog = northwind_catalog
    elif callable(frames):
        catalog = catalog_from(frames())
    else:
        catalog = catalog_from({name: make() for name, make in frames.items()})
    d = analyse(catalog, mode=mode, accept=accept)
    return catalog, d, build_cells(d)


@pytest.fixture(scope="module", params=list(CASES))
def case(request, northwind_catalog):
    catalog, d, cells = build(request.param, northwind_catalog)
    results, metrics = run_cells(cells, catalog)
    return request.param, d, cells, results, metrics


def test_every_cell_runs(case):
    name, d, cells, results, metrics = case
    assert not d.halted
    errors = [(r.cell_id, r.error, r.stdout[-800:]) for r in results if r.error]
    assert not errors, errors
    assert all(r.notes for r in results), [r.cell_id for r in results if not r.notes]  # every cell explains its result
    assert all(c.code("mysql").startswith("-- Why:") for c in cells)
    if d.kpi:
        assert metrics.get("reconciled") is True


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def _left_join_filters(sql: str) -> list[str]:
    """Columns of a LEFT-joined table used in a WHERE condition other than `IS NULL` (that turns the join into an inner join)."""
    bad = []
    for body in re.split(r"\bUNION ALL\b", _strip_comments(sql)):
        for alias in re.findall(r"LEFT\s+JOIN\s+\S+\s+(\w+)\s+ON", body):
            for where in re.findall(r"\bWHERE\b(.*?)(?=\bGROUP\b|\bORDER\b|\)\s*AS|\Z)", body, re.S):
                for m in re.finditer(rf"\b{alias}\.\w+", where):
                    if not re.match(r"\s+IS\s+(NOT\s+)?NULL", where[m.end():]):
                        bad.append(m.group())
    return bad


def test_dialect_rules(case):
    name, d, cells, *_ = case
    for cell in cells:
        for st in cell.statements:
            for dialect in ("mysql", "postgres", "duckdb"):
                sql = st.sql[dialect]
                body = _strip_comments(sql).upper()
                for construct, why in BANNED.items():
                    assert construct not in body, f"{cell.id}/{st.label}/{dialect}: {construct} ({why})"
                aliases = re.findall(r"\bAS\s+([a-z_][a-z0-9_]*)\b", _strip_comments(sql))
                assert not [a for a in aliases if a in RESERVED], (cell.id, st.label, aliases)
                assert not _left_join_filters(sql), (cell.id, st.label, _left_join_filters(sql))
                if st.run != "show":  # the integer-division demo divides integers on purpose
                    for m in re.finditer(r"/\s*", _strip_comments(sql)):
                        after = _strip_comments(sql)[m.end():m.end() + 12]
                        assert after.startswith("NULLIF(") or re.match(r"\d+\.\d", after), (cell.id, st.label, dialect, after)
            assert "::" not in st.sql["mysql"], (cell.id, st.label)
            pg = _strip_comments(st.sql["postgres"])
            assert all(pg[m.end():].startswith("(") for m in re.finditer(r"ROUND\(", pg)), (cell.id, st.label)  # ROUND((x)::numeric, n)


def test_mysql_and_postgres_parse(case):
    name, d, cells, *_ = case
    failures = []
    for cell in cells:
        for st in cell.statements:
            for dialect in ("mysql", "postgres"):
                try:
                    sqlglot.parse(st.sql[dialect], read=dialect)
                except sqlglot.errors.ParseError as error:
                    failures.append((cell.id, st.label, dialect, str(error)[:200]))
    assert not failures, failures


def test_error_skips_later_cells(northwind_catalog):
    _, d, cells = build("northwind", northwind_catalog)
    broken = cells[:2] + [cells[2].__class__("boom", "Boom", "x", cells[2].story, cells[2].statements, "raise ValueError('bad')")] + cells[3:]
    results, _ = run_cells(broken, northwind_catalog)
    assert results[2].error.startswith("ValueError") and all(r.skipped for r in results[3:])
