"""Your own assumptions and KPIs: checked before anything uses them, kept apart from the engine's, computed on the same rows."""
import io
import json
import re
import zipfile

import duckdb
import nbformat
import pandas as pd
import pytest

from autosql import (Customization, CustomizationError, assumption_items, assumptions, build_cells, build_database, dry_run, run_cells)
from autosql.business import executive_summary, kpi_dictionary_markdown
from autosql.codegen import tagged_assumptions
from autosql.custom import CustomKPI, example
from autosql.exports import to_project_zip, to_sql_script
from autosql.report import to_business_brief
from autosql.runner import execute
from conftest import NORTHWIND, analyse
from htmltree import parse_tree
from test_codegen_runs import CUSTOM_NORTHWIND

BROKEN = {"kpis": [
    {"key": "typo", "name": "A typo", "definition": "Misspelt column.", "sql": "SUM(frieght)"},
    {"key": "rows", "name": "Not an aggregate", "definition": "Returns a row per order.", "sql": "revenue"},
    {"key": "fine", "name": "Fine", "definition": "Works.", "sql": "COUNT(*)", "unit": "number"},
]}


def kpi(sql, **kw):
    return {"key": "k", "name": "K", "definition": "d", "sql": sql, **kw}


def problems(data) -> str:
    with pytest.raises(CustomizationError) as error:
        Customization.from_dict(data)
    return "\n".join(error.value.problems)


# ── The customization itself: plain data, checked before any data is loaded ──────────────────────────────────────────
def test_a_customization_round_trips_through_json():
    custom = Customization.from_dict(CUSTOM_NORTHWIND)
    assert Customization.from_json(custom.to_json()) == custom
    kpis = custom.to_dict()["kpis"]
    assert kpis[0]["sql"] == "100.0 * SUM(freight) / NULLIF(SUM(revenue), 0)"          # one expression stays one string
    assert kpis[2]["sql"] == {"mysql": "DATEDIFF(MAX(order_date), MIN(order_date))", "postgres": "MAX(order_date) - MIN(order_date)"}
    assert "sql" not in kpis[3]                                                         # a definition only has none
    assert [k.key for k in custom.runnable_kpis] == ["freight_share", "big_orders", "days_covered"]
    assert Customization().empty and Customization.from_json("").empty and Customization.from_json(" \n").empty
    assert not custom.empty and hash(custom) == hash(Customization.from_json(custom.to_json()))   # frozen: it can key a cache


def test_a_list_of_statements_is_shorthand_for_add():
    assert Customization.from_dict({"assumptions": ["A.", "B."]}).assumptions_add == ("A.", "B.")


def test_everything_wrong_is_said_at_once_and_nothing_is_silently_ignored():
    found = problems({"assumption": [], "kpis": [
        {"key": "Bad Key", "name": "", "definition": 3, "unit": "euros", "direction": "sideways", "sqll": "x"}, "nope", {"key": "a"}]})
    for expected in ("Unknown key 'assumption'", "kpis #1 (Bad Key): key must be lower case", "name can't be empty", "definition must be text",
                     "unit must be one of number, money, pct, days, score", "direction must be one of up, down, context",
                     "has the key 'sqll'", "kpis #2 must be an object", "kpis #3 (a) is missing name, definition"):
        assert expected in found, (expected, found)
    assert "used twice" in problems({"kpis": [kpi("COUNT(*)"), kpi("SUM(a)")]})
    assert "limit is 600" in problems({"assumptions": {"add": ["x" * 601]}})
    assert "can't be empty" in problems({"assumptions": {"edit": {"revenue": "  "}}})
    assert "list of statements, or an object" in problems({"assumptions": "text"})
    assert "assumptions.add must be a list" in problems({"assumptions": {"add": "x"}})
    assert "keys are 'add' and 'edit'" in problems({"assumptions": {"edits": {}}})
    assert "kpis must be a list" in problems({"kpis": "x"})
    assert "A customization is an object" in problems([1])


def test_json_that_is_not_json_says_where():
    with pytest.raises(CustomizationError, match=r"not valid JSON: .*\(line 2, column \d+\)"):
        Customization.from_json('{\n  "kpis": [,]}')


@pytest.mark.parametrize("sql", [
    "SUM(a); DROP TABLE t", "SUM(a) -- x", "SUM(a) /* x */", "SUM(`a`)", "SUM(a) \\ x", "SUM(CASE WHEN note = 'a;b' THEN 1 END)",   # one expression
    "SUM((a)", "SUM(a))", "SUM('a)", 'SUM("a)',                                                                                    # balanced
    "1 FROM order_facts", "SUM(a) UNION SELECT 1", "(SELECT 1) UNION (SELECT 2)", "SUM(a) WHERE b", "SUM(a) INTO OUTFILE 'x'",      # no second query
    "PG_SLEEP(5)", "SUM(a) + SLEEP(3)", "(SELECT COUNT(*) FROM read_csv('/etc/passwd'))", "LOAD_FILE('/etc/passwd')",              # nothing outside
])
def test_an_expression_that_is_not_one_expression_is_refused(sql):
    assert "sql (default)" in problems({"kpis": [kpi(sql)]})


@pytest.mark.parametrize("sql", [
    "100.0 * SUM(a) / NULLIF(SUM(b), 0)", "SUM(CASE WHEN a > 1 THEN 1 ELSE 0 END)", "SUM(a) / (SELECT COUNT(*) FROM order_facts)",
    'SUM("from")', "SUM(sleep_hours)", "COUNT(DISTINCT EXTRACT(YEAR FROM d))", "SUM(a) IS DISTINCT FROM SUM(b)",
    "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY a)", "COUNT(*) FILTER (WHERE a > 5)", "SUM(CASE WHEN a < 5 THEN 1 END)",
])
def test_ordinary_expressions_are_accepted(sql):
    assert Customization.from_dict({"kpis": [kpi(sql)]}).kpis[0].sql == (("default", sql),)


def test_wording_is_one_line_because_it_ends_up_in_sql_comments():
    for field in ("name", "definition", "why", "pitfall", "decision"):
        assert "one line of plain text" in problems({"kpis": [kpi("COUNT(*)", **{field: "x\nDROP TABLE t;"})]}), field
    assert "one line of plain text" in problems({"assumptions": {"add": ["x\ny"]}})
    assert Customization.from_dict({"kpis": [kpi("SUM(a)\n   / 2.0")]}).kpis[0].sql == (("default", "SUM(a) / 2.0"),)   # SQL may wrap


def test_sql_can_differ_by_dialect_and_duckdb_borrows():
    both = CustomKPI("k", "K", "d", {"mysql": "M", "postgres": "P"})
    assert (both.expression("mysql"), both.expression("postgres"), both.expression("duckdb")) == ("M", "P", "P")
    default = CustomKPI("k", "K", "d", {"default": "D", "mysql": "M"})
    assert (default.expression("mysql"), default.expression("postgres"), default.expression("duckdb")) == ("M", "D", "D")
    assert "needs a 'default'" in problems({"kpis": [kpi({"mysql": "SUM(a)"})]})
    assert "the keys are default, mysql, postgres, duckdb" in problems({"kpis": [kpi({"oracle": "x"})]})
    words = CustomKPI("k", "K", "d")
    assert not words.runnable and words.expression("mysql") is None and "no SQL" in words.formula()


def test_keys_that_would_collide_with_a_statement_label_and_too_many_kpis_are_refused():
    assert "can't end with _by_period" in problems({"kpis": [kpi("COUNT(*)", key="sales_by_period")]})
    many = [{"key": f"k{i}", "name": f"K{i}", "definition": "d"} for i in range(21)]
    assert "21 KPIs; the limit is 20" in problems({"kpis": many})
    assert len(Customization.from_dict({"kpis": many[:20]}).kpis) == 20


def test_the_traps_the_engine_avoids_are_named_for_your_sql():
    def warnings(sql):
        return CustomKPI("k", "K", "d", sql).warnings

    assert not warnings("100.0 * SUM(a) / NULLIF(SUM(b), 0)") and not warnings("SUM(a) / 2.0") and not warnings("SUM(CASE WHEN u = 'a/b' THEN 1 END)")
    assert "divides with /" in warnings("SUM(a) / COUNT(*)")[0] and warnings("SUM(a) / 2")
    assert "FILTER" in warnings("SUM(a) FILTER (WHERE b > 1)")[0]
    assert not warnings({"mysql": "SUM(a)", "postgres": "SUM(a) FILTER (WHERE b > 1)"})        # PostgreSQL has it
    assert "::" in warnings("SUM(a)::numeric")[0] and not warnings({"mysql": "SUM(a)", "postgres": "SUM(a)::numeric"})
    assert not example(True).kpis[0].warnings and not example(False).kpis[0].warnings
    assert Customization.from_json(example(True).to_json()) == example(True)


# ── Assumptions: the engine's, with yours marked ─────────────────────────────────────────────────────────────────────
def test_assumptions_have_stable_keys_and_are_unchanged_without_a_customization(northwind):
    items = assumption_items(northwind)
    keys = [k for k, _ in items]
    assert len(keys) == len(set(keys)) and keys[:2] == ["fact", "revenue"]
    assert assumptions(northwind) == [text for _, text in items]                       # nothing marked, nothing added
    assert northwind.custom is None and "custom" not in [s.key for s in northwind.steps]


def test_what_you_add_and_edit_is_marked_and_a_wrong_key_is_said_out_loud(northwind, northwind_catalog):
    custom = Customization.from_dict({"assumptions": {"add": ["Figures are in US dollars."],
                                                      "edit": {"revenue": "Revenue is net.", "revenu": "a typo"}}})
    d = analyse(northwind_catalog, custom=custom)
    plain, lines = assumptions(northwind), assumptions(d)
    assert lines[1] == "Revenue is net. (edited by you)" and lines[0] == plain[0] and lines[2:len(plain)] == plain[2:]
    assert lines[-2] == "Added by you: Figures are in US dollars."
    assert lines[-1].startswith("Not applied: no assumption has the key 'revenu'.") and "fact, revenue, " in lines[-1]
    assert [(k, o) for k, _, o in tagged_assumptions(d) if o != "engine"] == [("revenue", "edited"), ("added", "yours"), ("not_applied", "notice")]
    step = next(s for s in d.steps if s.key == "custom")
    assert "1 assumption(s) of your own and 2 edit(s) to the engine's assumptions" in step.short()   # the typo is counted: it is said out loud above


def test_every_assumption_key_the_engine_produces_is_documented(northwind_catalog):
    """The keys a customization can edit are listed in docs/DECISION_TREE.md; a key added to the code must be added there."""
    from pathlib import Path

    from test_codegen_runs import CASES, build

    doc = (Path(__file__).resolve().parents[1] / "docs" / "DECISION_TREE.md").read_text(encoding="utf-8")
    start = doc.index("### D8.")
    listed = doc[start:doc.index("## Stage C", start)]
    seen = set()
    for case in CASES:
        _, d, _ = build(case, northwind_catalog)
        seen |= {re.sub(r"^(partial|dedupe|filter):.*", lambda m: {"partial": "partial:<period>", "dedupe": "dedupe:<table>",
                                                                   "filter": "filter:<table>.<column>"}[m.group(1)], key)
                 for key, _ in assumption_items(d)}
    assert {"fact", "revenue", "partial:<period>", "dedupe:<table>", "filter:<table>.<column>", "validation_only"} <= seen
    assert not [key for key in sorted(seen) if f"`{key}`" not in listed], sorted(seen)


def test_the_cell_for_your_kpis_exists_only_when_there_is_sql_to_run(northwind_catalog):
    def ids(d):
        return [c.id for c in build_cells(d)]

    assert "custom_kpis" not in ids(analyse(northwind_catalog))
    words = Customization.from_dict({"kpis": [{"key": "a", "name": "A", "definition": "Words."}]})
    assert "custom_kpis" not in ids(analyse(northwind_catalog, custom=words))
    custom = Customization.from_dict(CUSTOM_NORTHWIND)
    order = ids(analyse(northwind_catalog, custom=custom))
    assert order[order.index("kpi_by_period") + 1] == "custom_kpis"
    validate = analyse(northwind_catalog, mode="validate", custom=custom)               # no KPI analysis: no view for them to read
    assert "custom_kpis" not in ids(validate) and validate.custom is custom


# ── Your KPIs, computed ──────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def run(northwind_catalog):
    d = analyse(northwind_catalog, custom=Customization.from_dict(CUSTOM_NORTHWIND))
    cells = build_cells(d)
    results, metrics = run_cells(cells, northwind_catalog)
    return d, cells, results, metrics


def raw_orders() -> pd.DataFrame:
    """One row per order, from the raw files: the oracle for the KPIs."""
    orders = pd.read_csv(NORTHWIND / "Orders.csv", parse_dates=["OrderDate"])
    lines = pd.read_csv(NORTHWIND / "Order Details.csv")
    lines["revenue"] = lines.UnitPrice * lines.Quantity * (1 - lines.Discount)
    orders = orders.merge(lines.groupby("OrderID", as_index=False).revenue.sum(), on="OrderID")
    orders["period"] = orders.OrderDate.dt.year.astype(str) + "-Q" + orders.OrderDate.dt.quarter.astype(str)
    return orders


def test_your_kpis_are_computed_on_the_same_rows_as_the_standard_ones(run):
    d, cells, results, metrics = run
    orders = raw_orders()
    by = {e["key"]: e for e in metrics["custom_kpis"]}
    assert list(by) == ["freight_share", "big_orders", "days_covered"]                 # the definition without SQL is not computed
    assert not [r.error for r in results if r.error]

    assert metrics["kpis"]["revenue"] == pytest.approx(orders.revenue.sum(), abs=0.01)  # the standard KPI is on these rows
    assert by["freight_share"]["value"] == pytest.approx(100 * orders.Freight.sum() / orders.revenue.sum())
    assert by["big_orders"]["value"] == (orders.revenue > 1000).sum()
    per = orders.groupby("period")
    expected = {"freight_share": 100 * per.Freight.sum() / per.revenue.sum(),
                "big_orders": per.revenue.apply(lambda s: (s > 1000).sum()),
                "days_covered": (per.OrderDate.max() - per.OrderDate.min()).dt.days}      # DuckDB ran the PostgreSQL expression
    for key, series in expected.items():
        assert {p["period"]: p["value"] for p in by[key]["periods"]} == pytest.approx(series.to_dict()), key
    status = {p["period"]: p["status"] for p in by["freight_share"]["periods"]}
    assert status.pop("1998-Q2") == "partial" and set(status.values()) == {"complete"}  # the partial period is marked, as in the standard KPIs

    cell = next(r for r in results if r.cell_id == "custom_kpis")
    assert [s.label for s in cell.statements] == ["custom_freight_share", "custom_freight_share_by_period", "custom_big_orders",
                                                  "custom_big_orders_by_period", "custom_days_covered", "custom_days_covered_by_period"]
    assert any("Freight share of revenue" in n and "1998-Q1" in n and "not tested for significance" in n for n in cell.notes)
    assert len(cell.figures) == 1                                                       # one chart, a panel per KPI


def test_a_kpi_that_fails_is_reported_next_to_it_and_nothing_else_changes(northwind_catalog):
    d = analyse(northwind_catalog, custom=Customization.from_dict(BROKEN))
    results, metrics = run_cells(build_cells(d), northwind_catalog)
    assert not [r.error for r in results if r.error] and not any(r.skipped for r in results)      # no cell failed, none was skipped
    by = {e["key"]: e for e in metrics["custom_kpis"]}
    assert "frieght" in by["typo"]["error"] and by["typo"]["value"] is None and by["typo"]["periods"] == []
    assert "830 rows, not one" in by["rows"]["error"]
    assert by["fine"]["error"] is None and by["fine"]["value"] == 830
    cell = next(r for r in results if r.cell_id == "custom_kpis")
    assert [s.label for s in cell.statements if s.error] == ["custom_typo", "custom_typo_by_period", "custom_rows_by_period"]
    assert sum("could not be computed" in n and "standard KPIs are not affected" in n for n in cell.notes) == 2
    plain_d = analyse(northwind_catalog)
    _, plain = run_cells(build_cells(plain_d), northwind_catalog)
    for key in ("kpis", "period_table", "significance", "revenue_total", "reconciled", "driver_split", "pareto", "rfm"):
        assert metrics[key] == plain[key], key                                          # the standard numbers are exactly the same
    assert "custom_kpis" not in plain


def test_your_sql_runs_where_files_and_the_network_are_out_of_reach(northwind_catalog):
    con = build_database(northwind_catalog)
    for attack in ("SELECT * FROM read_csv('/etc/passwd')", "COPY orders TO '/tmp/autosql_leak.csv'", "ATTACH '/tmp/autosql_leak.db'",
                   "SET enable_external_access = true"):
        with pytest.raises(duckdb.Error):
            execute(con, attack)


def test_the_dry_run_finds_what_is_wrong_without_reading_a_row(northwind_catalog):
    d = analyse(northwind_catalog, custom=Customization.from_dict(BROKEN))
    found = dry_run(build_cells(d), northwind_catalog)
    columns = dict(found.columns["order_facts"])
    assert list(columns)[:3] == ["order_id", "customer_id", "order_date"] and "freight" in columns and "line_facts" in found.columns
    assert set(found.errors) == {"custom_typo", "custom_typo_by_period", "custom_rows", "custom_rows_by_period"}
    assert "frieght" in found.errors["custom_typo"] and "not an aggregate" in found.errors["custom_rows"]
    empty = build_database(northwind_catalog, empty=True)
    assert execute(empty, "SELECT COUNT(*) AS n FROM orders").n[0] == 0 and execute(empty, "SELECT COUNT(*) AS n FROM order_details").n[0] == 0
    assert dry_run(build_cells(analyse(northwind_catalog)), northwind_catalog).errors == {}
    assert dry_run([], northwind_catalog).error                                         # no views to check against


def test_your_tiles_are_neutral_and_kept_apart(run, northwind):
    d, cells, results, metrics = run
    summary = executive_summary(d, metrics)
    assert summary.tiles == executive_summary(northwind, metrics).tiles                # the engine's tiles are untouched
    tiles = {t.key: t for t in summary.custom_tiles}
    assert list(tiles) == ["custom:freight_share", "custom:big_orders", "custom:days_covered"]
    by = {e["key"]: {p["period"]: p["value"] for p in e["periods"]} for e in metrics["custom_kpis"]}
    share, big = tiles["custom:freight_share"], tiles["custom:big_orders"]
    assert (share.custom, share.good, share.significant, share.ci, share.unit) == (True, None, None, None, "pct")
    assert share.value == pytest.approx(by["freight_share"]["1998-Q1"])
    assert share.delta_pct == pytest.approx(by["freight_share"]["1998-Q1"] - by["freight_share"]["1997-Q4"])       # points, for a percentage
    assert big.delta_pct == pytest.approx(100 * (by["big_orders"]["1998-Q1"] / by["big_orders"]["1997-Q4"] - 1))   # percent, for a count
    assert summary.period == "1998-Q1" and summary.compared_with == "1997-Q4"


def test_a_kpi_with_no_value_in_the_latest_period_shows_all_periods_instead():
    from autosql.business import custom_tiles
    metrics = {"custom_kpis": [{"key": "k", "name": "K", "unit": "number", "direction": "up", "value": 7.0, "error": None,
                                "periods": [{"period": "2022-Q1", "value": 1.0, "status": "complete"}]},
                               {"key": "e", "name": "E", "unit": "number", "direction": "up", "value": None, "error": "boom", "periods": []}]}
    (tile,) = custom_tiles(metrics, "2022-Q2", "2022-Q1")
    assert (tile.value, tile.delta_pct, tile.note) == (7.0, None, "all periods")      # and a failed KPI has no tile at all


# ── The brief ────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_brief_shows_what_is_yours_apart_from_what_is_the_engines(run):
    d, cells, results, metrics = run
    tree = parse_tree(to_business_brief(d, metrics, title="Mine"))
    yours = tree.find("section", id="yours")
    assert yours is not None and "Your KPIs" in yours.text() and tree.find("a", href="#yours") is not None
    tiles = {n.attrs["data-ev"]: n for n in yours.find_all("div", cls="tile")}
    assert set(tiles) == {"tile:custom:freight_share", "tile:custom:big_orders", "tile:custom:days_covered"}
    assert "not tested" in tiles["tile:custom:freight_share"].text() and "5.1%" in tiles["tile:custom:freight_share"].text()
    assert not [n for n in tree.find_all("div", cls="tile") if n.attrs.get("data-ev", "").startswith("tile:custom:") and n not in tiles.values()]
    cards = {n.attrs["data-ev"]: n for n in yours.find_all("div", cls="card") if n.attrs.get("data-ev")}
    assert set(cards) == {"chart:custom:freight_share", "chart:custom:big_orders", "chart:custom:days_covered"}
    for card in cards.values():                                                         # table view, and the SQL you wrote, in both dialects
        assert card.find("details", cls="tv") and card.find("details", cls="sqlv")
    code = {dialect: cards["chart:custom:days_covered"].find("div", cls=f"sql-{dialect}").find("code").text() for dialect in ("mysql", "postgres")}
    assert "DATEDIFF(MAX(order_date), MIN(order_date)) AS days_covered" in code["mysql"]
    assert "MAX(order_date) - MIN(order_date) AS days_covered" in code["postgres"]
    assert "Defined, not computed" in yours.text() and "Only a definition" in yours.text()
    appendix = tree.find("details", id="sql-custom_kpis")                               # the step is in the appendix with the rest
    assert appendix is not None and "Your KPI: Freight share of revenue" in appendix.find("div", cls="sql-mysql").text()

    method = tree.find("section", id="method")
    lis = {li.text().strip(): li for li in method.find_all("li")}
    assert any(t.startswith("Revenue = price × quantity × (1 − discount); freight is not included.") and t.endswith("edited by you") for t in lis)
    assert any(t.startswith("Figures are in US dollars.") and t.endswith("added by you") for t in lis)
    definitions = " ".join(td.text() for td in method.find_all("td"))
    assert "Freight share of revenue (yours)" in definitions and "100.0 * SUM(freight) / NULLIF(SUM(revenue), 0)" in definitions
    assert "(a definition only: no SQL)" in definitions
    # an engine KPI is not marked as yours
    assert "Revenue (yours)" not in definitions


def test_a_failed_kpi_is_a_card_with_its_error_and_its_sql(northwind_catalog):
    d = analyse(northwind_catalog, custom=Customization.from_dict(BROKEN))
    _, metrics = run_cells(build_cells(d), northwind_catalog)
    tree = parse_tree(to_business_brief(d, metrics))
    yours = tree.find("section", id="yours")
    failed = [c for c in yours.find_all("div", cls="card") if c.find("p", cls="problem")]
    assert [c.find("h3").text() for c in failed] == ["A typo", "Not an aggregate"]
    assert "Could not be computed" in failed[0].text() and "frieght" in failed[0].find("p", cls="problem").text()
    assert "SUM(frieght) AS typo" in failed[0].find("div", cls="sql-postgres").find("code").text()      # so that it can be fixed
    assert {n.attrs["data-ev"] for n in yours.find_all("div", cls="tile")} == {"tile:custom:fine"}      # only the one that worked is a tile


def test_a_run_that_stopped_before_your_kpis_says_so_instead_of_leaving_them_out(run):
    d, cells, results, metrics = run
    tree = parse_tree(to_business_brief(d, {k: v for k, v in metrics.items() if k != "custom_kpis"}))
    yours = tree.find("section", id="yours")
    failed = [c.find("h3").text() for c in yours.find_all("div", cls="card") if "the analysis stopped before this step" in c.text()]
    assert failed == ["Freight share of revenue", "Orders over 1,000", "Days between first and last order"]
    assert not yours.find_all("div", cls="tile")                                           # nothing was computed, so there is no tile


def test_your_words_cannot_inject_markup_or_sql_into_the_brief_or_the_scripts(northwind_catalog):
    hostile = '<img src=x onerror=alert(1)>'
    custom = Customization.from_dict({
        "assumptions": {"add": [f"{hostile} `code`"]},
        "kpis": [{"key": "evil", "name": f'{hostile} "quoted" `ticks`', "definition": f"</script><b>{hostile}", "sql": "SUM(CASE WHEN revenue < 5 THEN 1 END)"}]})
    d = analyse(northwind_catalog, custom=custom)
    cells = build_cells(d)
    _, metrics = run_cells(cells, northwind_catalog)
    page = to_business_brief(d, metrics)
    assert "<img src=x" not in page and "</script><b>" not in page and "&lt;img src=x onerror=alert(1)&gt;" in page
    assert "revenue &lt; 5" in page and "revenue < 5" not in page.replace("revenue &lt; 5", "")          # the SQL is escaped, once
    prose = re.sub(r"<(style|script|code|pre)\b.*?</\1>", " ", page, flags=re.S)
    assert "`" not in prose                                                                               # marks for the app, not for a page
    script = to_sql_script(cells, d, "mysql")
    comment_lines = [line for line in script.splitlines() if "onerror" in line]
    assert comment_lines and all(line.startswith("--") for line in comment_lines)                        # in a comment, never a statement


# ── Exports ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_exports_carry_your_assumptions_and_kpis(run, northwind_catalog):
    d, cells, results, metrics = run
    mysql, postgres = to_sql_script(cells, d, "mysql"), to_sql_script(cells, d, "postgres")
    for text in (mysql, postgres):
        assert "Added by you: Figures are in US dollars." in text and "(edited by you)" in text
        assert "-- 1) Your KPI: Freight share of revenue, all periods" in text and "SUM(freight)" in text
    assert "DATEDIFF(MAX(order_date), MIN(order_date)) AS days_covered" in mysql and "MAX(order_date) - MIN(order_date) AS days_covered" in postgres
    z = zipfile.ZipFile(io.BytesIO(to_project_zip(cells, d, northwind_catalog, "postgres", "Northwind", metrics)))
    assert Customization.from_json(z.read("customization.json").decode()) == d.custom      # load it back, or pass it to decide(..., custom=)
    assert "| **Freight share of revenue** (yours) |" in z.read("docs/kpi_dictionary.md").decode()
    assert "`customization.json`" in z.read("README.md").decode() and "Added by you" in z.read("README.md").decode()
    assert "Your KPI: Orders over 1,000, per quarter" in z.read("04_analysis.sql").decode()
    plain = zipfile.ZipFile(io.BytesIO(to_project_zip(build_cells(analyse(northwind_catalog)), analyse(northwind_catalog), northwind_catalog,
                                                      "postgres", "Northwind")))
    assert "customization.json" not in plain.namelist() and "(yours)" not in plain.read("docs/kpi_dictionary.md").decode()


def test_the_kpi_dictionary_lists_yours_after_the_engines():
    custom = Customization.from_dict(CUSTOM_NORTHWIND)
    lines = kpi_dictionary_markdown(custom).splitlines()
    assert lines[:-4] == kpi_dictionary_markdown().splitlines()                        # the engine's rows come first, unchanged
    assert lines[-4].startswith("| **Freight share of revenue** (yours) | Freight charged as a share of revenue. | `100.0 * SUM(")
    assert "↓ lower" in lines[-4] and "—" in lines[-4]                                  # no why / pitfall / decision given
    assert lines[-3].startswith("| **Orders over 1,000** (yours) |") and "Large orders carry the revenue." in lines[-3]
    assert "mysql: DATEDIFF(" in lines[-2] and "postgres: MAX(order_date) - MIN(order_date)" in lines[-2]
    assert "(a definition only: no SQL)" in lines[-1] and "`(a definition" not in lines[-1]


def test_a_sql_that_would_differ_between_databases_says_so_in_the_script(northwind_catalog):
    custom = Customization.from_dict({"kpis": [{"key": "per_order", "name": "Units per order", "definition": "Units over orders.",
                                                "sql": "SUM(units) / COUNT(*)"}]})
    d = analyse(northwind_catalog, custom=custom)
    cells = build_cells(d)
    statement = next(st for c in cells for st in c.statements if st.label == "custom_per_order")
    assert "-- Check: It divides with / by something other than NULLIF(x, 0) or a decimal number" in statement.sql["mysql"]
    results, metrics = run_cells(cells, northwind_catalog)
    assert not [r.error for r in results if r.error] and metrics["custom_kpis"][0]["error"] is None   # it still runs: a warning, not an error
    page = to_business_brief(d, metrics)
    assert "Check the SQL:" in parse_tree(page).find("section", id="yours").text()


def test_the_notebook_survives_a_kpi_that_fails(northwind_catalog, tmp_path):
    from nbclient import NotebookClient

    d = analyse(northwind_catalog, custom=Customization.from_dict(BROKEN))
    cells = build_cells(d)
    zipfile.ZipFile(io.BytesIO(to_project_zip(cells, d, northwind_catalog, "postgres", "Northwind"))).extractall(tmp_path)
    nb = nbformat.read(tmp_path / "analysis.ipynb", as_version=4)
    NotebookClient(nb, timeout=300, kernel_name="python3", resources={"metadata": {"path": str(tmp_path)}}).execute()
    outputs = [o for c in nb.cells if c.cell_type == "code" for o in c.get("outputs", [])]
    assert not [o for o in outputs if o.get("output_type") == "error"]
    text = "\n".join(str(o.get("data", {}).get("text/markdown", "")) + str(o.get("text", "")) for o in outputs)
    assert "Your KPI **A typo** could not be computed" in text and "Your KPI **Fine**: **830** over all periods" in text
    assert json.loads((tmp_path / "customization.json").read_text())["kpis"][0]["key"] == "typo"


def test_the_example_customization_in_the_docs_loads_and_runs(northwind_catalog):
    custom = Customization.from_json((NORTHWIND / "customization.json").read_text(encoding="utf-8"))
    assert [k.key for k in custom.kpis if not k.runnable] == ["gross_margin"]           # defined, waiting for a cost column
    results, metrics = run_cells(build_cells(analyse(northwind_catalog, custom=custom)), northwind_catalog)
    by = {e["key"]: e for e in metrics["custom_kpis"]}
    assert list(by) == ["freight_share", "big_orders", "days_covered"] and not any(e["error"] for e in by.values())
    assert round(by["freight_share"]["value"], 1) == 5.1 and by["big_orders"]["value"] == 403


def test_a_kpi_that_divides_by_zero_has_no_value_instead_of_breaking_the_page(northwind_catalog):
    """DuckDB answers 1.0 / 0 with infinity, not NULL: it must not reach a chart axis."""
    custom = Customization.from_dict({"kpis": [{"key": "ratio", "name": "Ratio", "definition": "Divides by nothing.",
                                                "sql": "SUM(revenue) / SUM(CASE WHEN revenue < 0 THEN 1 ELSE 0 END)"},
                                               {"key": "fine", "name": "Fine", "definition": "Works.", "sql": "COUNT(*)", "unit": "number"}]})
    d = analyse(northwind_catalog, custom=custom)
    _, metrics = run_cells(build_cells(d), northwind_catalog)
    ratio = next(e for e in metrics["custom_kpis"] if e["key"] == "ratio")
    assert ratio["value"] is None and all(p["value"] is None for p in ratio["periods"]) and ratio["error"] is None
    page = to_business_brief(d, metrics)
    assert "no value" in parse_tree(page).find("section", id="yours").text()                  # the table says so; no chart is drawn from nothing


def test_a_chart_of_a_kpi_that_goes_below_zero_keeps_its_negatives():
    from autosql.report import period_line

    rows = [{"period": f"2022-Q{i + 1}", "value": v, "period_status": "complete"} for i, v in enumerate([-5.0, -2.0, 3.0, 4.0])]
    tree = parse_tree(period_line(rows, "value", "number", "Net"))
    ys = [float(c.attrs["cy"]) for c in tree.find_all("circle")]
    assert len(ys) == 4 and all(18 <= y <= 184 for y in ys) and ys[0] > ys[-1]                  # inside the plot, lowest value lowest
    ticks = [t.text() for t in tree.find_all("text", cls="tick") if t.attrs.get("text-anchor") == "end"]
    assert any(t.startswith("-") for t in ticks) and "0" in ticks


def test_your_kpis_run_on_every_awkward_dataset():
    """Single rows, one column, seven years, day/month dates, no customer, no money: a KPI of yours never breaks a run."""
    from conftest import catalog_from
    from datasets import sweep_cases

    custom = Customization.from_dict({"kpis": [{"key": "rows", "name": "Rows", "definition": "How many.", "sql": "COUNT(*)", "unit": "number"}]})
    ran = 0
    for name, frames in sweep_cases().items():
        catalog = catalog_from(frames)
        d = analyse(catalog, custom=custom)
        cells = build_cells(d)
        results, metrics = run_cells(cells, catalog)
        assert not [r.error for r in results if r.error], name
        if any(c.id == "custom_kpis" for c in cells):
            ran += 1
            (entry,) = metrics["custom_kpis"]
            assert entry["error"] is None and entry["value"] is not None, (name, entry)
            assert to_business_brief(d, metrics), name
        else:
            assert d.halted or "base" in d.skipped, name                                      # only when there is no view for it to read
    assert ran >= 15
