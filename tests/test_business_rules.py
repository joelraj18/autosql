"""Rows that are not sales (cancelled, returned, test orders, a second currency) are left out, and the totals still reconcile."""
import duckdb
import pandas as pd
import pytest

from autosql import build_cells, run_cells
from autosql.business import insights, trust_score
from autosql.codegen import Builder, assumptions
from autosql.dialect import Dialect
from conftest import analyse, catalog_from
from test_codegen_runs import business_rules_frames, signed_amounts_frame


@pytest.fixture(scope="module")
def shop():
    frames = business_rules_frames()
    catalog = catalog_from(frames)
    d = analyse(catalog)
    results, metrics = run_cells(build_cells(d), catalog)
    assert not [r.error for r in results if r.error]
    return frames, catalog, d, {r.cell_id: r for r in results}, metrics


def rules(d):
    return {(f.table, f.column): (f.kind, f.values, f.kept) for f in d.row_filters}


def test_rows_that_are_not_sales_are_found(shop):
    _, _, d, _, _ = shop
    assert rules(d) == {
        ("order_items", "line_status"): ("status", ("returned",), None),
        ("orders", "status"): ("status", ("Cancelled", "Returned"), None),    # 'Partially refunded' kept most of its revenue
        ("orders", "currency"): ("currency", ("EUR",), "USD"),
        ("orders", "is_test"): ("flag", ("Y",), None),
    }
    assert {"row_filter", "row_filter_currency"} <= {s.key for s in d.steps}
    assert not {"currency", "is_test"} & {dim.column for dim in d.dimensions}     # one value left: nothing to slice by
    said = " ".join(assumptions(d))
    assert "Only amounts in USD are counted" in said and "are not sales and are left out (with all their lines)" in said


def test_views_match_the_rules_computed_independently(shop):
    frames, _, d, results, m = shop
    orders = frames["orders"].drop_duplicates()
    out = orders["Status"].isin(["Cancelled", "Returned"]) | (orders["Currency"] == "EUR") | (orders["is_test"] == "Y")
    items = frames["order_items"]
    kept = items[(items["line_status"] != "returned") & ~items["OrderID"].isin(orders.loc[out, "OrderID"])]
    revenue = float((kept["Price"] * kept["Qty"]).sum())
    raw = float((items["Price"] * items["Qty"]).sum())
    assert m["reconciled"] is True
    assert abs(m["revenue_total"] - revenue) < 0.01 and abs(m["left_out"]["value"] - (raw - revenue)) < 0.01
    assert m["kpis"]["orders"] == kept["OrderID"].nunique()
    # an order with no status is kept: NULL is never left out (a bare NOT IN would drop it)
    no_status = orders.loc[orders["Status"].isna() & ~out, "OrderID"]
    assert len(no_status) and kept["OrderID"].isin(no_status).any()
    assert "The business rules leave out" in " ".join(results["reconcile"].notes)


def test_kept_and_left_out_split_every_row_once(shop):
    _, catalog, d, _, _ = shop
    b, D = Builder(d), Dialect("duckdb")
    con = duckdb.connect()
    con.register("orders", catalog.tables["orders"].df)
    total = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    kept = con.execute(f"SELECT COUNT(*) FROM orders x WHERE {' AND '.join(b.kept_where(D, 'x', 'orders'))}").fetchone()[0]
    out = con.execute(f"SELECT COUNT(*) FROM orders x WHERE {b.left_out(D, 'x', 'orders')}").fetchone()[0]
    assert kept + out == total
    status = con.execute("SELECT COUNT(*) FROM orders x WHERE x.status IS NULL OR x.status NOT IN ('Cancelled', 'Returned')").fetchone()[0]
    naive = con.execute("SELECT COUNT(*) FROM orders x WHERE x.status NOT IN ('Cancelled', 'Returned')").fetchone()[0]
    assert naive == status - int(catalog.tables["orders"].df["status"].isna().sum()) < status   # bare NOT IN drops every NULL status


def test_the_brief_says_what_was_left_out(shop):
    _, _, d, _, m = shop
    found = [i for i in insights(d, m) if "not sales" in i.headline]
    assert found and found[0].area == "Operations" and "Convert the other currencies to USD" in found[0].action
    assert "not sales are left out by the business rules (not penalised)" in trust_score(d, m).dimensions[-1].detail


def test_signed_amounts_keep_one_currency():
    frame = signed_amounts_frame()
    catalog = catalog_from({"orders": frame})
    d = analyse(catalog)
    assert rules(d) == {("orders", "order_status"): ("status", ("chargeback", "refunded"), None),
                        ("orders", "is_test"): ("flag", (True,), None),
                        ("orders", "amount_currency"): ("currency", ("€",), "$")}
    results, m = run_cells(build_cells(d), catalog)
    assert not [r.error for r in results if r.error]
    amount = frame["amount"].str.replace(r"[$€,]", "", regex=True).astype(float)
    kept = frame["amount"].str.startswith("$") & (frame["order_status"] == "completed") & (frame["is_test"] == "false")
    assert m["reconciled"] is True and abs(m["revenue_total"] - float(amount[kept].sum())) < 0.01


def test_keeping_every_row_is_a_choice():
    catalog = catalog_from({"orders": signed_amounts_frame()})
    d = analyse(catalog, mode="manual", picks={"row_filters": False})
    assert d.row_filters == [] and "row_filter_off" in {s.key for s in d.steps}
    results, m = run_cells(build_cells(d), catalog)
    assert m["reconciled"] is True and "left_out" not in m


def test_validate_mode_only_reports_them():
    d = analyse(catalog_from(business_rules_frames()), mode="validate")
    step = next(s for s in d.steps if s.key == "row_filter_validate")
    assert "orders.status" in step.found and d.row_filters == []


def test_plain_state_is_geography_not_a_status(new_wheels_catalog, northwind):
    assert analyse(new_wheels_catalog).row_filters == [] and northwind.row_filters == []
    frame = pd.DataFrame({"order_id": range(60), "state": ["Cancelled", "TX"] * 30, "amount": [9.5] * 60,
                          "order_date": pd.date_range("2023-01-01", periods=60, freq="D").strftime("%Y-%m-%d")})
    assert analyse(catalog_from({"orders": frame})).row_filters == []
