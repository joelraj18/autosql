"""Small helpers the generated checks call: period labels, bounds and indexes.

This file is the single source of truth: the engine imports it, the runner executes its source in every check's namespace,
and the exported notebook includes it, so a check computes exactly the same thing in the app and in the notebook. It must
therefore stay self-contained (standard library only).

Periods are identified by (year, number): quarter 1-4, month 1-12, ISO week 1-53 (with its ISO year), or number 0 for a
whole year. ``period_index`` numbers consecutive periods 1 apart, which is what the SQL computes as ``period_index``.
"""
import datetime as _dt
import math as _math

PERIOD_UNITS = ("week", "month", "quarter", "year")
_WEEK_ZERO = _dt.date(1970, 1, 5)   # a Monday: ISO weeks are counted from here, so week indexes stay consecutive across years


def period_bounds(unit, year, number):
    """First and last calendar day of a period."""
    year, number = int(year), int(number)
    if unit == "week":
        start = _dt.date.fromisocalendar(year, number, 1)
        return start, start + _dt.timedelta(days=6)
    if unit == "quarter":
        start = _dt.date(year, 3 * number - 2, 1)
        end_month = 3 * number
    elif unit == "month":
        start, end_month = _dt.date(year, number, 1), number
    else:
        return _dt.date(year, 1, 1), _dt.date(year, 12, 31)
    following = _dt.date(year + (end_month == 12), end_month % 12 + 1, 1)
    return start, following - _dt.timedelta(days=1)


def period_label(unit, year, number):
    """'1998-Q1', '1998-03', '2022-W07' or '1998'."""
    year, number = int(year), int(number)
    if unit == "quarter":
        return f"{year}-Q{number}"
    if unit == "month":
        return f"{year}-{number:02d}"
    if unit == "week":
        return f"{year}-W{number:02d}"
    return str(year)


def period_index(unit, year, number):
    """A running number: consecutive periods differ by exactly 1 (the SQL's period_index)."""
    year, number = int(year), int(number)
    if unit == "quarter":
        return year * 4 + number
    if unit == "month":
        return year * 12 + number
    if unit == "week":
        return (_dt.date.fromisocalendar(year, number, 1) - _WEEK_ZERO).days // 7
    return year


def label_from_index(unit, index):
    """The label of the period with this period_index (used to name gaps)."""
    index = int(index)
    if unit == "quarter":
        year, number = divmod(index - 1, 4)
        return period_label(unit, year, number + 1)
    if unit == "month":
        year, number = divmod(index - 1, 12)
        return period_label(unit, year, number + 1)
    if unit == "week":
        year, number, _ = (_WEEK_ZERO + _dt.timedelta(weeks=index)).isocalendar()
        return period_label(unit, year, number)
    return str(index)


# ── Statistics: is a change bigger than normal variation? ──────────────────
# Two-sided Student t quantiles, t(0.975, df) and t(0.995, df), from the standard tables; df beyond the table uses the
# largest tabulated df not above it (a slightly wider, conservative interval), and the normal quantile past 120.
_T_DF = tuple(range(1, 31)) + (40, 60, 120)
T975 = dict(zip(_T_DF, (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
                        2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
                        2.021, 2.000, 1.980)))
T995 = dict(zip(_T_DF, (63.657, 9.925, 5.841, 4.604, 4.032, 3.707, 3.499, 3.355, 3.250, 3.169, 3.106, 3.055, 3.012, 2.977, 2.947,
                        2.921, 2.898, 2.878, 2.861, 2.845, 2.831, 2.819, 2.807, 2.797, 2.787, 2.779, 2.771, 2.763, 2.756, 2.750,
                        2.704, 2.660, 2.617)))
Z = {0.975: 1.959964, 0.995: 2.575829}


def t_quantile(prob, df):
    """t(prob, df) for prob 0.975 (95% intervals) or 0.995 (99%)."""
    table = T975 if prob == 0.975 else T995
    if df is None or df > 120:
        return Z[prob]
    usable = [k for k in _T_DF if k <= max(int(df), 1)]
    return table[usable[-1]]


def _interval(lo, hi):
    return {"lo": lo, "hi": hi, "significant": lo > 0 or hi < 0}


def ci_rate_change(n1, t1, n2, t2):
    """Change in a count per unit of time (orders per day), in %, with a 95% interval (Poisson counts, log rate ratio)."""
    if not (n1 and n2 and t1 and t2):
        return None
    ratio = (n2 / t2) / (n1 / t1)
    half = Z[0.975] * _math.sqrt(1 / n1 + 1 / n2)
    return dict(change=100 * (ratio - 1), **_interval(100 * (ratio * _math.exp(-half) - 1), 100 * (ratio * _math.exp(half) - 1)))


def ci_total_change(n1, mean1, sd1, t1, n2, mean2, sd2, t2):
    """Change in a total built from n orders of varying value (revenue), in %, with a 95% interval.

    Compound Poisson: orders arrive at random and their values vary, so Var(total) = n * (sd² + mean²) and the relative
    standard error of a total is sqrt((1 + cv²) / n), cv = sd / mean.
    """
    if not (n1 and n2 and t1 and t2 and mean1 and mean2) or n1 < 2 or n2 < 2 or sd1 is None or sd2 is None:
        return None
    ratio = (n2 * mean2 / t2) / (n1 * mean1 / t1)
    se = _math.sqrt((1 + (sd1 / mean1) ** 2) / n1 + (1 + (sd2 / mean2) ** 2) / n2)
    half = Z[0.975] * se
    return dict(change=100 * (ratio - 1), **_interval(100 * (ratio * _math.exp(-half) - 1), 100 * (ratio * _math.exp(half) - 1)))


def ci_mean_change(mean1, sd1, n1, mean2, sd2, n2, relative=True):
    """Change in an average (order value, days to ship, rating) with a 95% Welch interval: in % of the first mean, or in
    the measure's own units when ``relative`` is False."""
    if not (n1 and n2) or n1 < 2 or n2 < 2 or sd1 is None or sd2 is None or (relative and not mean1):
        return None
    v1, v2 = sd1 ** 2 / n1, sd2 ** 2 / n2
    se = _math.sqrt(v1 + v2)
    df = (v1 + v2) ** 2 / (v1 ** 2 / (n1 - 1) + v2 ** 2 / (n2 - 1)) if v1 + v2 > 0 else None   # Welch-Satterthwaite
    half = t_quantile(0.975, df) * se
    diff = mean2 - mean1
    scale = 100 / mean1 if relative else 1
    return dict(change=diff * scale, **_interval((diff - half) * scale, (diff + half) * scale))


def _wilson(x, n, z):
    p = x / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * _math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return centre - half, centre + half


def ci_share_change(x1, n1, x2, n2):
    """Change in a share (late orders), in percentage points, with a 95% Newcombe interval built from two Wilson intervals,
    which stays accurate when the counts are small."""
    if not (n1 and n2):
        return None
    p1, p2 = x1 / n1, x2 / n2
    l1, u1 = _wilson(x1, n1, Z[0.975])
    l2, u2 = _wilson(x2, n2, Z[0.975])
    diff = p2 - p1
    lo = diff - _math.sqrt((p2 - l2) ** 2 + (u1 - p1) ** 2)
    hi = diff + _math.sqrt((u2 - p2) ** 2 + (p1 - l1) ** 2)
    return dict(change=100 * diff, **_interval(100 * lo, 100 * hi))


def ci_correlation(r, n):
    """95% interval for a correlation from n pairs (Fisher z); None below 4 pairs."""
    if r is None or n is None or n < 4 or r != r:
        return None
    r = max(min(r, 0.999999), -0.999999)
    z, half = _math.atanh(r), Z[0.975] / _math.sqrt(n - 3)
    return dict(r=r, **_interval(_math.tanh(z - half), _math.tanh(z + half)))


def trend_prediction(xs, ys, x0, prob=0.995):
    """Fit a straight line through (xs, ys) by least squares and predict x0, with the half-width of its prediction interval:
    t(prob, n - 2) * s * sqrt(1 + 1/n + (x0 - mean x)^2 / Sxx). Returns (predicted, half_width), or None below 5 points."""
    n = len(xs)
    if n < 5:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    s = _math.sqrt(sum((y - intercept - slope * x) ** 2 for x, y in zip(xs, ys)) / (n - 2))
    return intercept + slope * x0, t_quantile(prob, n - 2) * s * _math.sqrt(1 + 1 / n + (x0 - mx) ** 2 / sxx)
