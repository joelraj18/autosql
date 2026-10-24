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


def tickets_frame():
    """No money column at all: the analysis counts rows (orders) per period."""
    rng = np.random.default_rng(8)
    return pd.DataFrame({"ticket_id": range(400), "customer_id": rng.integers(1, 60, 400),
                         "opened_at": pd.date_range("2022-01-01", periods=400, freq="D").strftime("%Y-%m-%d"),
                         "status": rng.choice(["open", "closed"], 400)})


def gap_quarter_frame():
    """No orders at all in 2022-Q3: LAG must not compare 2022-Q4 with 2022-Q2."""
    rng = np.random.default_rng(9)
    days = pd.date_range("2022-01-01", "2023-06-30", freq="D")
    days = days[~((days >= "2022-07-01") & (days < "2022-10-01"))]
    return pd.DataFrame({"order_id": range(len(days)), "customer_id": rng.integers(1, 80, len(days)),
                         "amount": rng.uniform(1, 9, len(days)).round(2), "order_date": days.strftime("%Y-%m-%d")})


def ten_weeks_frame():
    """Ten weeks of orders (5 Jan to 13 Mar 2022): too short for months, so the engine analyses ISO weeks."""
    rng = np.random.default_rng(11)
    n = 700
    day = pd.Timestamp("2022-01-05") + pd.to_timedelta(np.sort(rng.integers(0, 68, n)), unit="D")
    return pd.DataFrame({"order_id": range(1, n + 1), "customer_id": rng.integers(1, 120, n), "region": rng.choice(["North", "South", "East"], n),
                         "amount": rng.gamma(2, 40, n).round(2), "order_date": day.strftime("%Y-%m-%d"),
                         "ship_date": (day + pd.to_timedelta(rng.integers(1, 6, n), unit="D")).strftime("%Y-%m-%d")})


def double_loaded_frames():
    """The shop with five order lines loaded twice: exact copies the views must drop."""
    frames = shop_frames()
    frames["order_items"] = pd.concat([frames["order_items"], frames["order_items"].iloc[:5]])
    return frames


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
    "tickets_count_only": ({"tickets": tickets_frame}, "auto", ()),
    "gap_quarter": ({"orders": gap_quarter_frame}, "auto", ()),
    "double_loaded": (double_loaded_frames, "auto", ()),
    "ten_weeks": ({"orders": ten_weeks_frame}, "auto", ()),
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
            # a WHERE clause ends at GROUP / ORDER / LIMIT, at the ")" closing its CTE or sub-query, or at the next SELECT
            for where in re.findall(r"\bWHERE\b(.*?)(?=\bGROUP\b|\bORDER\b|\bLIMIT\b|\)\s*(?:AS\b|,|\n|\Z)|\bSELECT\b|\Z)", body, re.S):
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


def test_gaps_never_produce_a_wrong_period_comparison(northwind_catalog):
    catalog, d, cells = build("gap_quarter", northwind_catalog)
    assert d.missing_periods == ["2022-Q3"] and "missing_periods" in [s.key for s in d.steps]
    results, metrics = run_cells(cells, catalog)
    per = next(r for r in results if r.cell_id == "kpi_by_period").statements[0].frame.set_index(["order_year", "quarter_number"])
    assert pd.isna(per.loc[(2022, 4), "revenue_qoq_pct"])          # its LAG row is 2022-Q2: not a neighbour
    expected = 100 * (per.loc[(2023, 1), "revenue"] / per.loc[(2022, 1), "revenue"] - 1)
    assert abs(per.loc[(2023, 1), "revenue_yoy_pct"] - expected) < 0.1  # joined on the same quarter a year earlier
    assert metrics["missing_periods"] == ["2022-Q3"]


def test_double_loaded_lines_are_removed_before_revenue(northwind_catalog):
    catalog, d, cells = build("double_loaded", northwind_catalog)
    clean, _, clean_cells = build("shop", northwind_catalog)
    assert d.dedupe["order_items"][0] == ("order_id", "product_id")
    _, m = run_cells(cells, catalog)
    _, m_clean = run_cells(clean_cells, clean)
    assert m["reconciled"] and round(m["revenue_total"], 2) == round(m_clean["revenue_total"], 2)


def test_count_only_data_is_measured_in_rows(northwind_catalog):
    catalog, d, cells = build("tickets_count_only", northwind_catalog)
    _, m = run_cells(cells, catalog)
    assert d.revenue.kind == "count" and m["main_measure"] == "orders" and m["reconciled"]
    assert any("orders changed" in line for line in __import__("autosql").interpret(m))


def test_technical_notes(northwind, new_wheels_catalog):
    cells = {c.id: c for c in build_cells(northwind)}
    assert {"CTE (WITH)", "window function", "LAG / LEAD", "LEFT JOIN", "NULLIF division guard"} <= set(cells["kpi_by_period"].techniques())
    assert "anti-join" in cells["orphans"].techniques() and "CUME_DIST" in cells["rfm"].techniques()
    assert ("ROUND(revenue, 2) AS revenue,", "ROUND((revenue)::numeric, 2) AS revenue,") in cells["kpi_by_period"].dialect_diff()
    assert cells["grain"].dialect_diff() == []                                   # fully portable SQL: nothing to translate
    base = {c.id: c for c in build_cells(analyse(new_wheels_catalog))}["base"]
    assert any("DATEDIFF(ship_date, order_date)" in a and "ship_date - order_date" in b for a, b in base.dialect_diff())


def _monthly_orders(seed: int, spike: str | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = 4000
    dates = pd.Timestamp("2020-01-01") + pd.to_timedelta(np.sort(rng.integers(0, 1096, n)), unit="D")
    amount = rng.gamma(2, 50, n).round(2)
    if spike:
        amount = np.where(dates.strftime("%Y-%m") == spike, amount * 3, amount)
    return pd.DataFrame({"order_id": range(n), "amount": amount, "order_date": dates.strftime("%Y-%m-%d")})


def test_trend_signals_ignore_noise_and_find_a_real_break():
    flagged = 0
    for seed in range(6):                                   # three years of i.i.d. monthly revenue: nothing is unusual
        catalog = catalog_from({"orders": _monthly_orders(seed)})
        d = analyse(catalog, "manual", {"fact": "orders", "measure": "amount", "amount": "amount", "date": "orders.order_date"})
        _, m = run_cells([c for c in build_cells(d) if c.id in ("base", "trend_signals")], catalog)
        flagged += len(m.get("unusual_periods", []))
    assert flagged <= 1                                      # ~48 quarters judged; |z| >= 2 on a 4-period baseline would flag ~8
    catalog = catalog_from({"orders": _monthly_orders(0, spike="2021-08")})
    d = analyse(catalog)
    results, m = run_cells([c for c in build_cells(d) if c.id in ("base", "trend_signals")], catalog)
    assert [u["period"] for u in m["unusual_periods"]] == ["2021-Q3"] and m["unusual_periods"][0]["baseline"] >= 4
    sql = {c.id: c for c in build_cells(d)}["trend_signals"].statements[0].sql["mysql"]
    assert "RANGE BETWEEN 8 PRECEDING AND 1 PRECEDING" in sql and "COUNT(baseline_value) OVER" in sql


def test_short_uploads_are_analysed_by_iso_week(northwind_catalog):
    catalog, d, cells = build("ten_weeks", northwind_catalog)
    assert d.period == "week" and d.period_cols == ["order_isoyear", "week_number", "week_index"]
    assert [d.label(p) for p in d.partial_periods] == ["2022-W01"] and d.label(d.complete_periods[-1]) == "2022-W10"
    base = {c.id: c for c in cells}["base"].statements[0].sql
    assert "YEARWEEK(order_date, 3) DIV 100 AS order_isoyear" in base["mysql"] and "WEEK(order_date, 3) AS week_number" in base["mysql"]
    assert "FLOOR(DATEDIFF(order_date, DATE '1970-01-05') / 7.0) AS week_index" in base["mysql"]
    assert "EXTRACT(ISOYEAR FROM order_date)::int AS order_isoyear" in base["postgres"]
    assert "FLOOR((order_date - DATE '1970-01-05') / 7.0)::int AS week_index" in base["postgres"]
    results, m = run_cells(cells, catalog)
    assert m["qoq_last_complete"]["from"] == "2022-W09" and m["qoq_last_complete"]["to"] == "2022-W10"
    assert "yoy" not in m and m["partial_periods"] == ["2022-W01"]
    assert any("ISO weeks (Monday to Sunday)" in a for a in __import__("autosql").assumptions(d))
