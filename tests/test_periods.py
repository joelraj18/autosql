"""Period comparisons an interviewer would try to break: a partial year-ago period, and months of different length."""
import numpy as np
import pandas as pd

from autosql import build_cells, run_cells
from autosql.business import executive_summary, insights
from conftest import analyse, catalog_from


def _run(frames):
    catalog = catalog_from(frames)
    d = analyse(catalog)
    results, metrics = run_cells(build_cells(d), catalog)
    return d, results, metrics


def test_year_over_year_never_compares_with_a_partial_period():
    # flat sales from 20 Feb 2021 to 31 Mar 2022: the year-ago quarter (2021-Q1) has only 40 of its 90 days
    rng = np.random.default_rng(1)
    n = 1500
    dates = pd.Timestamp("2021-02-20") + pd.to_timedelta(np.sort(rng.integers(0, 405, n)), unit="D")
    d, results, m = _run({"orders": pd.DataFrame({"order_id": range(n), "amount": rng.gamma(2, 50, n).round(2),
                                                  "order_date": dates.strftime("%Y-%m-%d")})})
    assert [d.label(p) for p in d.partial_periods] == ["2021-Q1"]
    assert "yoy" not in m                                          # it used to report +187.8% year over year
    s = executive_summary(d, m)
    assert "year over year" not in s.headline and s.headline.startswith("Revenue reached 35,475.68 in 2022-Q1 (+7.6% on 2021-Q4)")
    notes = " ".join(n for r in results if r.cell_id == "kpi_by_period" for n in r.notes)
    assert "No year-over-year figure for 2022-Q1" in notes
    sql = {c.id: c for c in build_cells(d)}["kpi_by_period"].statements[0].sql["mysql"]
    assert "AND LAG(p.is_partial) OVER (ORDER BY p.period_index) = 0" in sql and "CASE WHEN y.is_partial = 0 THEN y.revenue END" in sql


def test_a_shorter_month_is_compared_per_day():
    # exactly ten orders every day: February only looks like a fall because it has 28 days
    days = pd.date_range("2021-12-01", "2022-02-28", freq="D")
    d, results, m = _run({"orders": pd.DataFrame({"order_id": range(len(days) * 10), "amount": 50.0,
                                                  "order_date": [x.strftime("%Y-%m-%d") for x in days for _ in range(10)]})})
    q = m["qoq_last_complete"]
    assert (round(q["pct"], 1), round(q["per_day_pct"], 1), q["days_from"], q["days_to"]) == (-9.7, 0.0, 31, 28)
    s = executive_summary(d, m)
    assert s.headline == "280 orders in 2022-02 (-9.7% on 2022-01, +0.0% per day). That is within normal variation (95% CI -15% to +18%)."
    growth = next(i for i in insights(d, m) if i.area == "Growth")
    assert growth.headline == "Orders fell 9.7% in 2022-02; per day it was flat" and growth.priority == 3
    tile = next(t for t in s.tiles if t.key == "orders")
    assert round(tile.delta_pct, 1) == 0.0 and tile.note == "per day"


def test_quarters_of_nearly_equal_length_are_not_adjusted(northwind, northwind_catalog):
    _, m = run_cells(build_cells(northwind), northwind_catalog)
    q = m["qoq_last_complete"]
    assert (q["days_from"], q["days_to"]) == (92, 90) and round(q["per_day_pct"], 1) == 67.9   # reported, but 2.2% apart: no switch
    s = executive_summary(northwind, m)
    assert "per day" not in s.headline and all(t.note == "" for t in s.tiles)
