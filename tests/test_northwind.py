"""The worked example: every number quoted in the docs, checked against a real run."""
import pytest

from autosql import build_cells, run_cells


@pytest.fixture(scope="module")
def run(northwind, northwind_catalog):
    cells = build_cells(northwind)
    results, metrics = run_cells(cells, northwind_catalog)
    assert not [r.error for r in results if r.error]
    return {r.cell_id: r for r in results}, metrics


def test_revenue_reconciles_three_ways(run):
    _, m = run
    assert m["reconciled"] is True
    assert round(m["revenue_total"], 2) == 1265793.04 == round(m["revenue_source"], 2)
    assert round(m["revenue_gross"], 2) == 1354458.59
    assert round(m["price_differs_pct"], 1) == 30.5


def test_validation_traps(run):
    results, m = run
    assert m["grain_ok"] is True and m["duplicate_keys"] == {k: 0 for k in m["duplicate_keys"]}
    fan = m["fanout"]
    assert fan["column"] == "orders.freight" and round(fan["total"], 2) == 64942.69 and round(fan["joined"], 2) == 207306.10
    assert round(fan["factor"], 2) == 3.19
    assert m["partial_periods"] == ["1998-Q2"]
    assert m["orphans"] == {}
    assert m["childless"]["orders.customer_id -> customers"] == 2
    assert "FISSA" in " ".join(results["orphans"].notes) and "PARIS" in " ".join(results["orphans"].notes)
    assert m["negative_lead"] == 0


def test_kpis(run):
    _, m = run
    k = m["kpis"]
    assert (k["orders"], k["customers"]) == (830, 89)
    assert k["avg_order_value"] == 1525.05 and k["avg_line_value"] == 587.37
    assert k["repeat_customer_pct"] == 98.9 and k["avg_ship_days"] == 8.49 and k["orders_without_ship_days"] == 21
    assert k["late_pct"] == 4.6


def test_periods_and_root_cause(run):
    _, m = run
    q = m["qoq_last_complete"]
    assert (q["from"], q["to"], round(q["pct"], 1)) == ("1997-Q4", "1998-Q1", 64.3)
    assert m["naive_change"] == {"period": "1998-Q2", "pct": -52.4}
    assert m["yoy"] == {"period": "1998-Q1", "pct": 115.8}
    split = m["driver_split"]
    assert round(split["delta"], 2) == 116810.09
    # Shapley split: each effect at the average of the other, so the two add up exactly (no interaction term to assign)
    assert round(split["volume"], 2) == 97776.34 and round(split["order_value"], 2) == 19033.75
    assert abs(split["volume"] + split["order_value"] - split["delta"]) < 1e-6 and 0.83 < split["volume"] / split["delta"] < 0.84
    classic = split["classic"]                                         # the textbook split, kept for the teaching note
    assert (round(classic["volume"], 2), round(classic["order_value"], 2), round(classic["interaction"], 2)) == (93868.75, 15126.16, 7815.18)
    assert m["top_contributors"]["category_name"]["segment"] == "Beverages"
    assert m["driver_vs_symptom"]["driver"] == "avg_ship_days" and abs(m["driver_vs_symptom"]["driver_change_pct"]) < 10
    assert m["cohort_sizes"]["1996-Q3"] == 43


def test_windows_and_traps(run):
    _, m = run
    assert m["top_items"]["Beverages"][0] == "Côte de Blaye"
    assert m["null_trap"] == {"using_not_in": 0, "using_not_exists": 7}
    assert m["count_trap"]["all_rows"] == 830 and m["count_trap"]["rows_with_shipped_date"] == 809
    assert m["sargable_equal"] is True
    assert "Hash Join" in m["plan_ops"]


def test_rfm_scores_never_split_equal_customers(run):
    _, m = run
    assert m["rfm_ties_split"] == 0                       # with NTILE(5), 34 of 89 customers got their F score from their id
    assert m["rfm"]["Champions"] == {"customers": 24, "revenue_pct": 54.4}
