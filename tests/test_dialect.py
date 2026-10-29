import pytest

from autosql.dialect import Dialect

MY, PG, DK = Dialect("mysql"), Dialect("postgres"), Dialect("duckdb")


def test_identifiers_are_quoted_only_when_needed():
    assert MY.ident("order_id") == "order_id"
    assert MY.ident("order") == "`order`" and PG.ident("order") == '"order"'
    assert MY.ident("rank") == "`rank`"  # reserved in MySQL 8
    assert MY.ident("we`ird") == "`we``ird`" and PG.ident('we"ird') == '"we""ird"'
    assert PG.ident("Mixed") == '"Mixed"'


def test_literals_escape_quotes_and_mysql_backslashes():
    assert PG.literal("O'Brien") == "'O''Brien'"
    assert MY.literal("a\\b") == "'a\\\\b'" and PG.literal("a\\b") == "'a\\b'"
    assert MY.literal(None) == "NULL" and MY.literal(3) == "3"


def test_dates_and_numbers():
    assert MY.days_between("order_date", "ship_date") == "DATEDIFF(ship_date, order_date)"
    assert PG.days_between("order_date", "ship_date") == "ship_date - order_date"
    assert PG.days_between("a", "b", timestamps=True) == "b::date - a::date"
    assert DK.days_between("a", "b", timestamps=True) == "date_diff('day', a, b)"
    assert MY.quarter("d") == "QUARTER(d)" and PG.quarter("d") == "EXTRACT(QUARTER FROM d)::int"
    assert PG.round("x", 1) == "ROUND((x)::numeric, 1)"  # PostgreSQL has no ROUND(double, int)
    assert MY.pct("a", "b") == "ROUND(100.0 * a / NULLIF(b, 0), 1)"
    assert "1.0 *" in PG.ratio("a", "b")  # never integer division
    assert MY.explain("SELECT 1").startswith("EXPLAIN FORMAT=TREE")


def test_types():
    assert MY.type_name("text", max_len=5, key=True) == "VARCHAR(10)"
    assert MY.type_name("text", max_len=5000) == "TEXT"
    assert MY.type_name("text", max_len=5000, key=True) == "VARCHAR(1000)"  # keys are never TEXT
    assert PG.type_name("decimal", scale=2) == "NUMERIC(18, 2)"
    assert MY.type_name("timestamp") == "DATETIME" and PG.type_name("float") == "DOUBLE PRECISION"
    with pytest.raises(ValueError):
        Dialect("oracle")
