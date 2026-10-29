import pandas as pd
import pytest

from autosql import read_files
from conftest import analyse, as_csv, catalog_from


def keys(d):
    return [s.key for s in d.steps]


def test_northwind_auto(northwind):
    d = northwind
    assert d.analysis == "kpi"
    assert (d.fact, d.line_level, d.header, d.order_key) == ("order_details", True, "orders", "order_id")
    rev = d.revenue
    assert (rev.kind, rev.price, rev.quantity, rev.discount, rev.discount_scale) == ("price_qty", "unit_price", "quantity", "discount", 1)
    assert d.date == ("orders", "order_date") and d.period == "quarter" and d.multi_year
    assert [d.label(p) for p in d.partial_periods] == ["1998-Q2"]
    assert [d.label(p) for p in d.compare] == ["1997-Q4", "1998-Q1"]
    assert [d.label(p) for p in d.yoy] == ["1997-Q1", "1998-Q1"]
    assert d.lead == ("orders", "order_date", "shipped_date") and d.required == "required_date"
    assert d.rating is None and d.customer == ("orders", "customer_id")
    assert [(x.table, x.column) for x in d.dimensions[:2]] == [("customers", "country"), ("categories", "category_name")]
    assert d.item[:2] == ("products", "product_name")
    assert d.null_trap["not_in"][0].child_col == "reports_to"
    assert d.null_trap["count"] == ("orders", "shipped_date")
    assert {"line_grain", "header_table", "partial_period", "lead_time", "late_flag", "fanout", "fk_self", "fk_suggested"} <= set(keys(d))
    assert d.skipped == {}


def test_ticking_a_suggested_link_adds_the_shipper_dimension(northwind_catalog):
    d = analyse(northwind_catalog, accept=["orders.ship_via → shippers.shipper_id"])
    assert ("shippers", "company_name", "fulfilment") in [(x.table, x.column, x.group) for x in d.dimensions]


def test_new_wheels_shape(new_wheels_catalog):
    d = analyse(new_wheels_catalog)
    assert (d.fact, d.line_level, d.header, d.order_key) == ("orders", False, None, "order_id")
    assert d.revenue.price == "vehicle_price" and d.revenue.discount == "discount"
    assert d.rating.name == "customer_feedback" and d.rating.scale
    assert d.lead == ("orders", "order_date", "ship_date") and d.period == "quarter" and not d.multi_year
    assert d.date_parts == [("orders", "quarter_number", "quarter")]
    assert {"cardinality", "orphans"} <= set(d.skipped)


def test_messy_shop(shop_catalog):
    d = analyse(shop_catalog)
    assert d.fact == "order_items" and d.header == "orders" and d.line_level
    assert "customers" in d.dedupe and "grain_intended" in keys(d)
    assert next(r for r in d.schema.accepted if r.child_table == "order_items" and r.child_col == "order_id").orphan_rows == 3


def test_validate_mode_skips_kpis(northwind_catalog):
    d = analyse(northwind_catalog, mode="validate")
    assert d.analysis == "validate" and "kpis" in d.skipped and "orphans" not in d.skipped
    assert keys(d)[1] == "focus_validate"


def test_auto_falls_back_to_validation_without_measure_or_date():
    people = pd.DataFrame({"person_id": range(50), "team": ["a", "b"] * 25, "name": [f"p{i}" for i in range(50)]})
    d = analyse(catalog_from({"people": people}))
    assert d.analysis == "validate" and "focus_auto_validate" in keys(d)


def test_manual_picks_and_bad_picks(northwind_catalog):
    d = analyse(northwind_catalog, mode="manual", picks={"fact": "order_details", "measure": "price_qty", "price": "unit_price",
                                                         "quantity": "quantity", "date": "orders.required_date"})
    assert d.analysis == "kpi" and d.revenue.discount is None and d.date == ("orders", "required_date")
    bad = analyse(northwind_catalog, mode="manual", picks={"fact": "order_details", "measure": "amount", "amount": "product_id_x"})
    assert bad.halted and bad.steps[-1].key == "halt_pick"
    bad_date = analyse(northwind_catalog, mode="manual", picks={"fact": "order_details", "date": "orders.ship_name"})
    assert bad_date.halted


def test_count_measure_and_percent_discount():
    df = pd.DataFrame({"ticket_id": range(200), "opened_at": pd.date_range("2023-01-01", periods=200, freq="D").strftime("%Y-%m-%d"),
                       "status": ["open", "closed"] * 100})
    d = analyse(catalog_from({"tickets": df}))
    assert d.revenue.kind == "count" and d.analysis == "kpi" and "rfm" in d.skipped
    sales = pd.DataFrame({"sale_id": range(100), "unit_price": [9.5, 12.25] * 50, "qty": [1, 2, 3, 4] * 25, "discount_pct": [0, 10, 25, 50] * 25,
                          "sold_on": pd.date_range("2023-01-01", periods=100, freq="W").strftime("%Y-%m-%d")})
    d2 = analyse(catalog_from({"sales": sales}))
    assert d2.revenue.discount_scale == 100 and "discount_percent" in keys(d2)


def test_empty_upload_halts():
    d = analyse(read_files([("empty.csv", b"a,b\n")]))
    assert d.halted and d.steps[-1].key == "halt_empty"


def test_unknown_mode():
    with pytest.raises(ValueError):
        analyse(read_files([("x.csv", as_csv(pd.DataFrame({"a": [1]})))]), mode="guess")
