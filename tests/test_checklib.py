"""The helpers every generated check calls: period arithmetic, and the statistics behind each confidence interval."""
import datetime as dt
import math

import pytest

from autosql.checklib import (T975, T995, ci_correlation, ci_mean_change, ci_rate_change, ci_share_change, ci_total_change, label_from_index,
                              period_bounds, period_index, period_label, t_quantile)


def _t_cdf(x: float, df: int, steps: int = 20000) -> float:
    """Student t CDF by numerical integration of the density (Simpson's rule): an independent check of the tables."""
    c = math.gamma((df + 1) / 2) / (math.sqrt(df * math.pi) * math.gamma(df / 2))
    f = lambda u: c * (1 + u * u / df) ** (-(df + 1) / 2)  # noqa: E731
    h = x / steps
    total = f(0) + f(x) + sum((4 if i % 2 else 2) * f(i * h) for i in range(1, steps))
    return 0.5 + total * h / 3


@pytest.mark.parametrize("df", [1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 40, 60, 120])
def test_t_tables_match_the_distribution(df):
    assert abs(_t_cdf(T975[df], df) - 0.975) < 2e-4
    assert abs(_t_cdf(T995[df], df) - 0.995) < 1e-4


def test_t_quantile_is_conservative_between_table_rows():
    assert t_quantile(0.975, 35.7) == T975[30] and t_quantile(0.975, 500) == pytest.approx(1.96, abs=1e-3)
    assert t_quantile(0.995, 0.4) == T995[1]


def test_period_arithmetic():
    assert period_label("quarter", 1998, 1) == "1998-Q1" and period_label("week", 2022, 7) == "2022-W07"
    assert period_bounds("quarter", 1998, 2) == (dt.date(1998, 4, 1), dt.date(1998, 6, 30))
    assert period_bounds("month", 2024, 2) == (dt.date(2024, 2, 1), dt.date(2024, 2, 29))
    assert period_bounds("week", 2020, 53) == (dt.date(2020, 12, 28), dt.date(2021, 1, 3))
    for unit, a, b in (("quarter", (1997, 4), (1998, 1)), ("month", (1997, 12), (1998, 1)), ("week", (2020, 53), (2021, 1))):
        assert period_index(unit, *b) - period_index(unit, *a) == 1                        # neighbours across a year boundary
        assert label_from_index(unit, period_index(unit, *b)) == period_label(unit, *b)


def test_intervals_on_northwind():
    # 1997-Q4 -> 1998-Q1: 120 -> 182 orders, average order value 1,514.01 -> 1,640.06 (sd 1,461.61 / 2,377.59)
    orders = ci_rate_change(120, 1, 182, 1)
    assert round(orders["change"], 1) == 51.7 and orders["significant"] and round(orders["lo"]) == 20
    revenue = ci_total_change(120, 1514.012196, 1461.606098, 1, 182, 1640.063478, 2377.588132, 1)
    assert round(revenue["change"], 1) == 64.3 and revenue["significant"] and (round(revenue["lo"]), round(revenue["hi"])) == (15, 135)
    aov = ci_mean_change(1514.012196, 1461.606098, 120, 1640.063478, 2377.588132, 182)
    assert round(aov["change"], 1) == 8.3 and not aov["significant"] and (round(aov["lo"]), round(aov["hi"])) == (-20, 37)


def test_share_and_correlation_intervals():
    late = ci_share_change(8, 120, 8, 182)
    assert not late["significant"] and late["lo"] < -2.3 < late["hi"]
    assert ci_share_change(5, 100, 40, 100)["significant"]                                  # 5% -> 40% is real
    assert not ci_correlation(0.6, 7)["significant"] and ci_correlation(0.95, 30)["significant"]
    assert ci_correlation(0.5, 3) is None and ci_rate_change(0, 1, 5, 1) is None


def test_trend_prediction():
    from autosql.checklib import trend_prediction
    pred, half = trend_prediction([0, 1, 2, 3, 4], [1, 3, 5, 7, 9], 5)            # an exact line: y = 2x + 1
    assert pred == pytest.approx(11) and half == pytest.approx(0, abs=1e-9)
    pred, half = trend_prediction([0, 1, 2, 3, 4, 5], [79728.57, 128355.40, 138288.93, 143177.05, 153937.77, 181681.46], 6)
    assert round(pred) == 196668 and 298491.55 - pred > half                       # Northwind 1998-Q1 is beyond its trend too
    assert trend_prediction([0, 1, 2, 3], [1, 2, 3, 4], 4) is None                 # fewer than 5 points: no trend test
