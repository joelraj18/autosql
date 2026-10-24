"""The business brief: self-contained HTML, every chart from the metrics, a table view per chart, both page modes."""
import re
from html.parser import HTMLParser

import pytest

from autosql import build_cells, run_cells
from autosql.report import compact, nice_ticks, period_parts, to_business_brief
from conftest import analyse
from test_codegen_runs import build


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
    assert abs(split["volume"] + split["order_value"] + split["interaction"] - split["delta"]) < 0.01


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
    assert "tip.textContent" in page and "innerHTML" not in page


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
