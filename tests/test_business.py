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


def test_wording_follows_the_data(nw, shop_catalog):
    d, _ = nw
    # a partial last period that is still up must not be described as a fall
    found = insights(d, {"naive_change": {"period": "1998-Q2", "pct": 12.0}, "main_measure": "revenue"})
    assert found[0].headline == "1998-Q2 is incomplete" and "understates" in found[0].implication and "fall" not in found[0].implication
    found = insights(d, {"naive_change": {"period": "1998-Q2", "pct": -52.4}, "main_measure": "revenue"})
    assert "shows a fall that isn't real" in found[0].implication
    # duplicates are only "removed" when the analysis views really de-duplicate
    for mode, wording in (("validate", "2 duplicate row(s) found, listed in the duplicates step"),
                          ("auto", "2 duplicate row(s) found, 2 of them removed in the analysis views")):
        ds = analyse(shop_catalog, mode=mode)
        _, ms = run_cells(build_cells(ds), shop_catalog)
        assert wording in next(x for x in trust_score(ds, ms).dimensions if x.name == "Uniqueness").detail


def _rating_falls_with_revenue() -> dict:
    """Synthetic: the data starts mid-quarter; from 2023-Q2 order values drop 30% and ratings fall with them."""
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(3)
    n = 2400
    dates = pd.Timestamp("2021-02-20") + pd.to_timedelta(np.sort(rng.integers(0, 860, n)), unit="D")
    late = dates >= pd.Timestamp("2023-04-01")
    rating = np.where(late, rng.choice(["Bad", "Okay", "Good"], n, p=[.5, .3, .2]),
                      rng.choice(["Bad", "Okay", "Good", "Very Good"], n, p=[.1, .2, .4, .3]))
    return {"orders": pd.DataFrame({"order_id": range(n), "amount": (rng.gamma(2, 50, n) * np.where(late, 0.7, 1.0)).round(2),
                                    "order_date": dates.strftime("%Y-%m-%d"), "customer_feedback": rating})}


def test_a_falling_rating_is_a_candidate_cause_and_a_late_start_is_not_a_trend():
    from conftest import catalog_from
    catalog = catalog_from(_rating_falls_with_revenue())
    d = analyse(catalog)
    results, m = run_cells(build_cells(d), catalog)
    dv = m["driver_vs_symptom"]
    assert dv["driver"] == "avg_rating" and dv["driver_change_pct"] < -10 and dv["symptom_change_pct"] < 0 and dv["candidate"]
    found = {i.area + ": " + i.headline: i for i in insights(d, m)}
    ops = next(i for k, i in found.items() if k.startswith("Operations"))
    assert ops.priority == 1 and ops.confidence == "needs a test" and "service" in ops.action
    first = found["Data: 2021-Q1 is incomplete"]                                 # the extract starts on 20 Feb
    assert first.priority == 2 and "starts part-way" in first.evidence and first.action == "Start trends and year-over-year comparisons after 2021-Q1."
    drill = " ".join(n for r in results if r.cell_id == "drilldown" for n in r.notes)
    assert "the way that would explain it" in drill


def test_a_driver_that_improved_cannot_explain_a_fall(nw):
    d, _ = nw
    m = {"driver_vs_symptom": {"driver": "avg_ship_days", "driver_change_pct": -15.0, "symptom_change_pct": -12.0, "candidate": False},
         "main_measure": "revenue"}
    ops = next(i for i in insights(d, m) if i.area == "Operations")
    assert ops.headline == "ship days did not explain the change" and ops.priority == 3
