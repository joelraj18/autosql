"""The business brief: self-contained HTML, every chart from the metrics, a table view per chart, both page modes."""
import re
from html.parser import HTMLParser

import pytest

from autosql import build_cells, run_cells
from autosql.briefsql import highlight
from autosql.report import compact, nice_ticks, period_parts, to_business_brief
from conftest import analyse, catalog_from
from datasets import sweep_cases
from htmltree import parse_tree
from test_codegen_runs import build, business_rules_frames

# Which statements each chart is made from (the spec the page must follow): (step, statement label)
CHART_SQL = {
    "chart:trend": [("kpi_by_period", "kpi_by_period")],
    "chart:driver": [("kpi_by_period", "kpi_by_period")],
    "chart:waterfall": [("driver_tree", "tree")],
    "chart:segment:customer_country": [("driver_tree", "by_customer_country")],
    "chart:segment:category_name": [("driver_tree", "by_category_name")],
    "chart:segment:employee_last_name": [("driver_tree", "by_employee_last_name")],
    "chart:pareto": [("pareto", "curve_customers"), ("pareto", "abc_customers")],
    "chart:rfm": [("rfm", "rfm_view"), ("rfm", "rfm_scores"), ("rfm", "rfm_segments")],
    "chart:discount-bands": [("discounts", "discounts")],
    "chart:discount-units": [("discounts", "discounts")],
}


class Checker(HTMLParser):
    """Collects tags and fails on any external resource."""
    def __init__(self):
        super().__init__()
        self.tags, self.external = [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for k, v in attrs:
            if k in ("src", "href") and v and not v.startswith("#"):
                self.external.append(v)


def parse(page: str) -> Checker:
    c = Checker()
    c.feed(page)
    return c


@pytest.fixture(scope="module")
def nw(northwind, northwind_catalog):
    _, metrics = run_cells(build_cells(northwind), northwind_catalog)
    return northwind, metrics


def test_northwind_brief(nw):
    d, m = nw
    page = to_business_brief(d, m, title="Northwind Traders", source="the Northwind example")
    c = parse(page)
    assert page.startswith("<!doctype html>") and not c.external                       # works offline
    assert "<title>Northwind Traders</title>" in page
    assert "Revenue reached 298,491.55 in 1998-Q1 (+64.3% on 1997-Q4, +115.8% year over year)." in page
    assert c.tags.count("svg") == 10 and page.count('class="tv"') >= 10                # every chart has a table view
    for section in ("data", "actions", "trend", "drivers", "customers", "pricing", "trust", "method"):
        assert f'id="{section}"' in page and f'href="#{section}"' in page
    assert "1998-Q2 is incomplete" in page and "Act now" in page                       # the partial-period warning leads
    assert "partial" in page and 'class="m-muted"' in page                             # and the chart greys that period
    assert "Top 20% → 60%" in page and "Beverages" in page and "Champions" in page
    assert "Data trust 99/100" in page                                                 # floored: 99.9 never shows as 100
    # every mark is focusable and carries its value for the tooltip
    marks = re.findall(r'<(?:path|circle) class="(?:m-|dot-)[^"]*"[^>]*>', page)
    assert marks and all('tabindex="0"' in x and "data-tip=" in x for x in marks)
    # the waterfall's steps add up: before + volume + value + both = after
    split = m["driver_split"]
    assert abs(split["volume"] + split["order_value"] - split["delta"]) < 0.01


def test_fragment_mode_for_hosts_that_add_the_skeleton(nw):
    d, m = nw
    frag = to_business_brief(d, m, title="Northwind Traders", standalone=False)
    assert frag.startswith("<title>Northwind Traders</title>")
    assert not re.search(r"<(!doctype|html|head|body)\b", frag, re.I)
    assert 'name="claude:viewport-fit-sides"' in frag
    assert "max(16px, env(safe-area-inset-left, 0px))" in frag and "max(16px, env(safe-area-inset-right, 0px))" in frag
    assert '@media (prefers-color-scheme: dark)' in frag and ':root[data-theme="dark"]' in frag


def test_tooltips_never_inject_html(nw):
    d, m = nw
    page = to_business_brief(d, m, title="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert 't.textContent = el.getAttribute("data-tip")' in page and "innerHTML" not in page and "insertAdjacentHTML" not in page


def test_every_chart_shows_the_engines_sql_in_both_dialects(nw):
    d, m = nw
    cells = {c.id: c for c in build_cells(d)}
    tree = parse_tree(to_business_brief(d, m, title="Northwind Traders"))
    on_page = {n.attrs["data-ev"] for n in tree.find_all("div", cls="card") if n.attrs.get("data-ev", "").startswith("chart:")}
    assert on_page == set(CHART_SQL)                                       # no chart without a spec, no spec without a chart
    for key, refs in CHART_SQL.items():
        card = tree.find("div", cls="card", data_ev=key)
        disclosure = card.find("details", cls="sqlv")
        assert disclosure is not None and card.find("details", cls="tv") is not None, key   # the SQL sits next to the table view
        panes = {dialect: disclosure.find("div", cls=f"sql-{dialect}") for dialect in ("mysql", "postgres")}
        for dialect, pane in panes.items():
            code = pane.find("code").text()
            for step, label in refs:
                statement = next(s for s in cells[step].statements if s.label == label)
                assert statement.sql[dialect] in code, (key, dialect, label)    # the full statement, character for character


def test_the_appendix_holds_every_step_the_page_points_at(nw):
    d, m = nw
    cells = {c.id: c for c in build_cells(d)}
    tree = parse_tree(to_business_brief(d, m, title="Northwind Traders"))
    steps = {n.attrs["data-step"]: n for n in tree.find_all("details", cls="step")}
    assert {"kpi_by_period", "driver_tree", "pareto", "rfm", "discounts", "kpis", "grain", "reconcile", "orphans"} <= set(steps)
    for step, node in steps.items():
        for dialect in ("mysql", "postgres"):
            code = node.find("div", cls=f"sql-{dialect}").find("code").text()
            assert all(st.sql[dialect] in code for st in cells[step].statements), (step, dialect)    # the whole step, not an excerpt
    links = tree.find_all("a", cls="sqllink")
    assert links
    for a in links:                                                                  # every card's SQL link leads somewhere
        targets = a.attrs["data-sql"].split()
        assert all(t in steps for t in targets) and a.attrs["href"] == f"#sql-{targets[0]}"
    facts = {n.attrs["data-ev"]: n for n in tree.find_all("div") if n.attrs.get("data-ev", "").startswith("fact:")}
    assert all(f.find("a", cls="sqllink") for f in facts.values()) and tree.find("div", cls="meters").find_all("a", cls="sqllink")


def test_sql_is_escaped_and_only_comments_are_dimmed():
    assert highlight("SELECT a < b -- why & how") == 'SELECT a &lt; b <span class="c">-- why &amp; how</span>'
    assert highlight("SELECT '--' AS dashes") == "SELECT &#x27;--&#x27; AS dashes"           # dashes inside a string are not a comment
    assert highlight("-- only a comment\nSELECT 1") == '<span class="c">-- only a comment</span>\nSELECT 1'


def test_the_sql_views_need_no_script_to_be_complete(nw):
    d, m = nw
    page = to_business_brief(d, m, title="Northwind Traders")
    tree = parse_tree(page)
    assert "<noscript><style>.sql-postgres { display: block !important; }</style></noscript>" in page.split("</head>")[0]   # both dialects without JS
    tabs, copies = tree.find_all("div", cls="dialect"), tree.find_all("button", cls="copy")
    assert tabs and copies and all("hidden" in n.attrs for n in tabs + copies)             # the script reveals them; until then they are not there
    assert all(p.find("span", cls="lbl") for p in tree.find_all("div", cls="sqlpane"))     # and each pane names its dialect in plain text
    assert tree.find("main", cls="page").attrs["data-dialect"] == "mysql"                    # MySQL first, as in the app


def _recommendation(tree, headline: str):
    article = next(a for a in tree.find_all("article", cls="action") if a.find("h3").text().startswith(headline))
    button = article.find("button", cls="evbtn")
    template = article.find("template", cls="figs")
    figures = {t.find("span", cls="label").text(): t.find("span", cls="value").text() for t in template.find_all("div", cls="tile")} if template else {}
    return button.attrs["data-cards"].split(), button.attrs["data-charts"].split(), button.attrs["data-sql"].split(), figures


def test_each_recommendation_opens_the_cards_charts_and_sql_it_was_made_from(nw):
    d, m = nw
    tree = parse_tree(to_business_brief(d, m, title="Northwind Traders"))
    assert len(tree.find_all("article", cls="action")) == 10                          # the brief shows every recommendation
    tiles = ["tile:revenue", "tile:orders", "tile:avg_order_value"]
    assert _recommendation(tree, "1998-Q2 is incomplete") == (
        ["fact:periods"], ["chart:trend"], ["date_range"],
        {"Days with data": "36 of 91", "Share of the period": "40%", "Compared naively": "-52.4%"})
    assert _recommendation(tree, "Revenue grew 64.3%") == (
        tiles, ["chart:trend"], ["kpi_by_period"],
        {"Change": "+64.3%", "95% interval": "+15.0% to +134.7%", "Year over year": "+115.8%"})
    assert _recommendation(tree, "Order volume accounts for 84%") == (
        tiles, ["chart:waterfall"], ["driver_tree"],
        {"Total change": "+116,810.09", "From the number of orders": "+97,776.34", "From order value": "+19,033.75",
         "Orders change": "+51.7% (real)", "Order value change": "+8.3% (within noise)"})
    assert _recommendation(tree, "Beverages drove 57%") == (
        [], ["chart:segment:category_name"], ["driver_tree"],
        {"Segment": "Beverages", "Its change": "+66,503.58", "Share of the total change": "57%", "Sliced by": "category name"})
    assert _recommendation(tree, "The top 20% of customers") == (
        [], ["chart:pareto"], ["pareto"],
        {"Top 20% of customers": "60% of revenue", "Class A customers": "34 (38% of them)", "Their share of revenue": "80%"})
    assert _recommendation(tree, "5 repeat customers are overdue") == (          # no chart exists for purchase gaps: key figures and SQL
        [], [], ["purchase_gaps"], {"Typical gap between orders": "59 days", "Overdue customers": "5 of 86",       # 86 have 3+ orders
                                    "Their share of repeat-customer revenue": "4%"})
    assert _recommendation(tree, "7 once-frequent customers") == (
        [], ["chart:rfm"], ["rfm"], {"At-risk customers": "7", "Their share of revenue": "4.6%"})
    assert _recommendation(tree, "Discounts cost 6.5%") == (
        [], ["chart:discount-bands", "chart:discount-units"], ["discounts"],
        {"Given away": "88,665.55", "Share of gross revenue": "6.5%", "Lines discounted": "39%",
         "Units per line, discounted vs full price": "27.1 vs 21.7"})
    assert _recommendation(tree, "ship days did not explain") == (
        ["tile:avg_ship_days"], ["chart:driver", "chart:trend"], ["drilldown"],
        {"Driver change": "-6.5%", "Revenue change": "+64.3%", "Correlation over the periods": "r = +0.02 (95% CI -0.74 to +0.76)"})


def test_the_popup_is_one_dialog_filled_without_html_injection(nw):
    d, m = nw
    page = to_business_brief(d, m, title="Northwind Traders")
    tree = parse_tree(page)
    dialog = tree.find("dialog", id="evidence")
    assert dialog is not None and dialog.attrs["aria-labelledby"] == "ev-title"
    assert dialog.find("button", cls="evclose").attrs["aria-label"] == "Close" and dialog.find("div", cls="tip") is not None   # a tooltip of its own
    assert "showModal" in page and "cloneNode" in page and "innerHTML" not in page and "outerHTML" not in page
    assert all("hidden" in b.attrs for b in tree.find_all("button", cls="evbtn"))      # shown by the script, not before
    assert len(tree.find_all("dialog")) == 1


def test_markdown_backticks_never_reach_the_page(nw):
    # the business layer writes `table.column` for the app and the notebook; on a page the backticks would show as characters
    d, m = nw
    pages = {"northwind": to_business_brief(d, m, title="Northwind Traders")}
    for name, frames in (("business rules", business_rules_frames()), ("returns", sweep_cases()["returns_negative"]),
                         ("gaps in a column", sweep_cases()["shop"])):
        catalog = catalog_from(frames)
        brief_d = analyse(catalog)
        _, brief_m = run_cells(build_cells(brief_d), catalog)
        pages[name] = to_business_brief(brief_d, brief_m, title=name)
    for name, page in pages.items():
        text = re.sub(r"\s+", " ", re.sub(r"<(style|script|code|pre)\b.*?</\1>", " ", page, flags=re.S))   # what a reader meets
        assert not re.findall(r".{0,50}`.{0,30}", text), f"{name}: {re.findall(r'.{0,50}`.{0,30}', text)[:2]}"


def test_new_wheels_shape_brief(new_wheels_catalog):
    d = analyse(new_wheels_catalog)
    _, m = run_cells(build_cells(d), new_wheels_catalog)
    page = to_business_brief(d, m, title="New Wheels")
    assert "Average ship days: the suspected driver" in page and 'class="line-driver"' in page
    assert "needs a test" in page and "controlled test" in page
    assert 'class="delta bad"' in page                                                 # revenue fell: shown as worse


def test_count_only_and_validation_briefs(northwind_catalog):
    catalog, d, cells = build("tickets_count_only", northwind_catalog)
    _, m = run_cells(cells, catalog)
    page = to_business_brief(d, m)
    assert "Orders per quarter" in page and "Why it moved" not in page and "Who brings the revenue" not in page
    catalog, d, cells = build("northwind_validate", northwind_catalog)
    _, m = run_cells(cells, catalog)
    page = to_business_brief(d, m, title="Validation")
    assert "<svg" not in page and "Can we trust these numbers?" in page and "Validation only" in page


def test_helpers():
    assert nice_ticks(298_491.55 * 1.08) == [0, 100_000, 200_000, 300_000, 400_000]
    assert nice_ticks(9.6, 6.9, 4)[0] <= 6.9 and nice_ticks(9.6, 6.9, 4)[-1] >= 9.6
    assert [compact(v, True) for v in (298_491.55, 1_640.06, 12_000_000, 0.4)] == ["298K", "1.6K", "12M", "0"]
    assert period_parts("1996-Q3") == ("Q3", "1996") and period_parts("2018-07") == ("Jul", "2018") and period_parts("1997") == ("1997", "")
    assert period_parts("2022-W07") == ("W07", "2022")


def test_committed_example_brief_is_current(nw):
    from pathlib import Path
    d, m = nw
    committed = (Path(__file__).resolve().parents[1] / "examples" / "northwind" / "business_brief.html").read_text(encoding="utf-8")
    assert committed == to_business_brief(d, m, title="Northwind Traders", source="the Northwind sample (8 CSV files)"), \
        "regenerate with: python examples/northwind/build_notebook.py"
