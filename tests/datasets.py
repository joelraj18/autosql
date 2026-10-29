"""Awkward datasets used by the stress sweep (``scripts/sweep.py``) and the golden snapshot (``tests/test_golden.py``).

Every dataset is a dict of table name -> DataFrame of text, loaded through ``catalog_from`` like an upload.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from conftest import new_wheels_frame, shop_frames


def _dates(n: int, start: str = "2021-01-01", freq: str = "D") -> pd.Index:
    return pd.date_range(start, periods=n, freq=freq).strftime("%Y-%m-%d")


def sweep_cases(big: bool = False) -> dict[str, dict[str, pd.DataFrame]]:
    """Shapes a real upload can have: one column, no money, duplicates, odd names, odd dates, very wide, very long."""
    rng = np.random.default_rng(7)
    cases: dict[str, dict[str, pd.DataFrame]] = {}
    cases["one_column"] = {"t": pd.DataFrame({"a": range(50)})}
    cases["text_only"] = {"t": pd.DataFrame({"name": [f"n{i}" for i in range(60)], "city": rng.choice(["x", "y"], 60)})}
    cases["all_null_col"] = {"t": pd.DataFrame({"id": range(40), "empty": [None] * 40, "amount": rng.uniform(1, 9, 40), "sold": _dates(40)})}
    cases["wide"] = {"t": pd.DataFrame({f"c{i}": rng.integers(0, 5, 100) for i in range(70)}).assign(
        id=range(100), sale_date=_dates(100), amount=rng.uniform(1, 5, 100))}
    cases["float_ids"] = {"orders": pd.DataFrame({"order_id": [f"{i}.0" for i in range(100)], "amount": rng.uniform(1, 9, 100),
                                                  "order_date": _dates(100)})}
    cases["returns_negative"] = {"sales": pd.DataFrame({"sale_id": range(200), "qty": rng.integers(-3, 6, 200),
                                                        "price": rng.uniform(1, 9, 200).round(2), "sale_date": _dates(200)})}
    cases["three_months"] = {"sales": pd.DataFrame({"id": range(90), "sales": rng.uniform(1, 9, 90), "date": _dates(90, "2023-01-01")})}
    cases["seven_years"] = {"sales": pd.DataFrame({"id": range(400), "revenue": rng.uniform(1, 9, 400),
                                                   "order_date": _dates(400, "2015-01-01", "7D")})}
    cases["one_date_value"] = {"t": pd.DataFrame({"id": range(30), "amount": rng.uniform(1, 9, 30), "order_date": ["2020-01-01"] * 30})}
    cases["two_unrelated"] = {"a": pd.DataFrame({"x": range(10)}), "b": pd.DataFrame({"y": ["p", "q"] * 5})}
    cases["customer_no_table"] = {"orders": pd.DataFrame({"order_id": range(300), "customer_id": rng.integers(1, 30, 300),
                                                          "total": rng.uniform(1, 99, 300), "order_date": _dates(300)})}
    cases["exact_dupes_no_key"] = {"log": pd.DataFrame({"event": ["a", "a", "b", "c"] * 20, "value": [1, 1, 2, 3] * 20})}
    cases["single_row"] = {"t": pd.DataFrame({"id": [1], "amount": [5.0], "order_date": ["2020-01-01"]})}
    cases["dmy_dates"] = {"t": pd.DataFrame({"id": range(60), "amount": rng.uniform(1, 9, 60),
                                             "order_date": pd.date_range("2022-01-01", periods=60, freq="7D").strftime("%d/%m/%Y")})}
    cases["zero_revenue_quarter"] = {"t": pd.DataFrame({"id": range(400), "amount": np.r_[np.zeros(100), rng.uniform(1, 9, 300)],
                                                        "order_date": _dates(400)})}
    cases["self_ref_only"] = {"employees": pd.DataFrame({"employee_id": range(1, 21), "manager_id": [None] + list(rng.integers(1, 10, 19)),
                                                         "dept": rng.choice(["a", "b"], 20)})}
    cases["colliding_names"] = {"t": pd.DataFrame({"Order ID": range(50), "order_id": range(50, 100), "ORDER-ID": range(100, 150),
                                                   "amount": rng.uniform(1, 2, 50), "order_date": _dates(50)})}
    cases["unicode_values"] = {"t": pd.DataFrame({"id": range(80), "城市": rng.choice(["北京", "上海", "Zürich"], 80),
                                                  "amount": rng.uniform(1, 9, 80), "order_date": _dates(80)})}
    cases["rating_1_10"] = {"t": pd.DataFrame({"id": range(200), "nps_score": rng.integers(0, 11, 200), "amount": rng.uniform(1, 9, 200),
                                               "order_date": _dates(200)})}
    cases["composite_fk"] = {"stores": pd.DataFrame({"region": ["N", "N", "S"], "store_no": [1, 2, 1], "manager": ["a", "b", "c"]}),
                             "sales": pd.DataFrame({"sale_id": range(60), "region": rng.choice(["N", "S"], 60),
                                                    "store_no": rng.integers(1, 3, 60), "amount": rng.uniform(1, 5, 60),
                                                    "sale_date": _dates(60)})}
    cases["timestamps_tz"] = {"t": pd.DataFrame({"id": range(50), "amount": rng.uniform(1, 9, 50),
                                                 "created_at": [f"2022-01-{1 + i % 28:02d}T10:00:00Z" for i in range(50)]})}
    cases["new_wheels"] = {"orders": new_wheels_frame()}
    cases["shop"] = shop_frames()
    cases["business_formats"] = {"orders": pd.DataFrame({
        "order_id": range(1, 41), "amount": [f"${v:,.2f}" for v in rng.uniform(5, 5000, 40)], "discount": [f"{v}%" for v in rng.integers(0, 20, 40)],
        "refund": ["-"] * 5 + [f"({v:.2f})" for v in rng.uniform(1, 9, 35)], "order_date": _dates(40, "2023-03-01", "3D")})}
    if big:
        n = 200_000
        orders = pd.DataFrame({"order_id": range(n), "customer_id": rng.integers(1, 5000, n), "amount": rng.uniform(1, 99, n).round(2),
                               "order_date": pd.to_datetime("2020-01-01") + pd.to_timedelta(rng.integers(0, 1000, n), unit="D"),
                               "region": rng.choice(list("NSEW"), n)})
        orders["order_date"] = orders["order_date"].dt.strftime("%Y-%m-%d")
        cases["big_200k"] = {"orders": orders}
    return cases
