"""Small helpers the generated checks call: period labels, bounds and indexes.

This file is the single source of truth: the engine imports it, the runner executes its source in every check's namespace,
and the exported notebook includes it, so a check computes exactly the same thing in the app and in the notebook. It must
therefore stay self-contained (standard library only).

Periods are identified by (year, number): quarter 1-4, month 1-12, ISO week 1-53 (with its ISO year), or number 0 for a
whole year. ``period_index`` numbers consecutive periods 1 apart, which is what the SQL computes as ``period_index``.
"""
import datetime as _dt

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
