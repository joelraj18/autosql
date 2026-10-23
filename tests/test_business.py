"""The business layer: summary, tiles, trust score, insights, data dictionary."""
import pytest

from autosql import build_cells, run_cells
from autosql.business import (BUSINESS_QUESTIONS, KPI_DICTIONARY, data_dictionary, data_dictionary_markdown, effect_shares,
                              executive_summary, insights, kpi_dictionary_markdown, trust_score)
from autosql.codegen import ORDER
from conftest import analyse
from test_codegen_runs import build


@pytest.fixture(scope="module")
def nw(northwind, northwind_catalog):
    _, metrics = run_cells(build_cells(northwind), northwind_catalog)
    return northwind, metrics


def test_northwind_summary(nw):
    d, m = nw
    s = executive_summary(d, m, "Northwind")
    assert (s.period, s.compared_with) == ("1998-Q1", "1997-Q4")
    assert s.headline.startswith("Revenue reached 298,491.55 in 1998-Q1 (+64.3% on 1997-Q4, +115.8% year over year).")
    assert "more orders" in s.headline
    tiles = {t.key: t for t in s.tiles}
    assert round(tiles["revenue"].delta_pct, 1) == 64.3 and tiles["revenue"].good
    assert tiles["orders"].value == 182 and tiles["avg_order_value"].value == 1640.06
    assert tiles["avg_ship_days"].good is True                  # fewer days is good
    assert round(tiles["late_pct"].delta_pct, 1) == -2.3         # percentage points, not a percent of a percent
    assert s.coverage == "04 Jul 1996 – 06 May 1998"


def test_northwind_trust_and_insights(nw):
    d, m = nw
    t = trust_score(d, m)
    assert t.grade == "High" and t.score > 95
    assert {x.name for x in t.dimensions} == {"Completeness", "Uniqueness", "Integrity", "Validity", "Consistency"}
    found = insights(d, m)
    assert found[0].area == "Data" and found[0].headline == "1998-Q2 is incomplete" and found[0].priority == 1
    heads = " | ".join(i.headline for i in found)
    assert "Revenue grew 64.3% in 1998-Q1" in heads and "Order volume accounts for 80% of the movement" in heads
    assert "Beverages drove 57% of the change" in heads and "The top 20% of customers bring 60% of revenue" in heads
    assert "Discounts cost 6.5% of gross revenue" in heads and "5 repeat customers are overdue" in heads
    ops = next(i for i in found if i.area == "Operations")
    assert ops.priority == 3 and "did not explain" in ops.headline   # ship days stayed flat
    assert all(i.source in ORDER for i in found)


def test_new_wheels_shape_flags_shipping_for_a_controlled_test(new_wheels_catalog):
    d = analyse(new_wheels_catalog)
    _, m = run_cells(build_cells(d), new_wheels_catalog)
    s = executive_summary(d, m)
    assert s.headline.startswith("Revenue reached") and "-17.0% on 2018-Q3" in s.headline and "fewer orders" in s.headline
    ops = next(i for i in s.insights if i.area == "Operations")
    assert ops.priority == 1 and ops.confidence == "needs a test" and "controlled test" in ops.action
    tiles = {t.key: t for t in s.tiles}
    assert tiles["revenue"].good is False and tiles["avg_ship_days"].good is False and tiles["avg_rating"].good is False


def test_effect_shares_never_exceed_100_percent():
    shares = effect_shares({"volume": -2.47, "order_value": 1.01, "interaction": -0.25, "delta": -1.71})
    assert abs(sum(shares.values()) - 1) < 1e-9 and max(shares.values()) < 1


def test_count_only_and_validation_summaries(northwind_catalog):
    catalog, d, cells = build("tickets_count_only", northwind_catalog)
    _, m = run_cells(cells, catalog)
    s = executive_summary(d, m)
    assert s.headline.startswith("92 orders in 2022-Q4 (+0.0% on 2022-Q3")
    assert not any(t.unit == "money" for t in s.tiles)
    catalog, d, cells = build("northwind_validate", northwind_catalog)
    _, m = run_cells(cells, catalog)
    s = executive_summary(d, m)
    assert s.trust.score > 90 and s.tiles == []


def test_dictionaries(nw):
    d, _ = nw
    df = data_dictionary(d)
    row = df[(df.table == "order_details") & (df.column == "unit_price")].iloc[0]
    assert row["used for"] == "revenue: price" and row["original name"] == "UnitPrice"
    fk = df[(df.table == "orders") & (df.column == "ship_via")].iloc[0]
    assert fk["key"] == "FK? → shippers.shipper_id"            # suggested, not confirmed
    assert "## `order_details`" in data_dictionary_markdown(d)
    assert kpi_dictionary_markdown().count("\n") == len(KPI_DICTIONARY) + 1
    assert set(BUSINESS_QUESTIONS) == set(ORDER)
