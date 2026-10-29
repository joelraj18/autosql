"""The docs quote numbers from the Northwind run; every one of them must be something the code actually prints.

A figure in the docs is checked when it has three or more digits (1,265,793.04, 64.3%, 1998), outside code spans and code
blocks (formulas and SQL). It must appear, without its sign, in what a Northwind run prints: the cell findings, the
decision stories, the assumptions, the headline and recommendations, the plain-English summary and the business brief.
When the code changes a number, this test names the doc that still quotes the old one.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

from autosql import build_cells, interpret, run_cells
from autosql.business import executive_summary, insights, trust_score
from autosql.codegen import assumptions
from autosql.report import to_business_brief

ROOT = Path(__file__).resolve().parents[1]
NUMBER = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?")
# Figures that are not Northwind outputs, with the reason they may appear anyway.
NOT_PRINTED = {
    "0.6667": "MySQL's 2 / 3 in the integer-division demo, which is shown in the export, not run",
}


def numbers(text: str) -> set[str]:
    return {n for n in NUMBER.findall(text) if sum(ch.isdigit() for ch in n) >= 3}


def markdown_prose(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"^(#+) [0-9.]+ ", r"\1 ", text, flags=re.M)      # "### 3.10 Real change…": a section number, not a figure
    return re.sub(r"`[^`]*`", " ", text)


def section(text: str, heading: str) -> str:
    """From a markdown heading to the next heading of the same or a higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    start = text.index(heading)
    rest = text[start + len(heading):]
    end = re.search(rf"^#{{1,{level}}} ", rest, flags=re.M)
    return heading + (rest[:end.start()] if end else rest)


def html_prose(fragment: str) -> str:
    fragment = re.sub(r"<(code|pre)>.*?</\1>", " ", fragment, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment))


def quoted() -> dict[str, str]:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "guide.md").read_text(encoding="utf-8")
    business = (ROOT / "docs" / "business_guide.md").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs" / "roadmap.html").read_text(encoding="utf-8")
    cards = re.findall(r'<p class="ex" data-ex="nw">(.*?)</p>', roadmap, flags=re.S)
    return {
        "README.md, Worked example": markdown_prose(section(readme, "## Worked example")),
        "docs/guide.md, section 3": markdown_prose(section(guide, "## 3. Feature deep dives")),
        "examples/northwind/README.md": markdown_prose((ROOT / "examples" / "northwind" / "README.md").read_text(encoding="utf-8")),
        "docs/business_guide.md, read aloud": markdown_prose(section(business, "## Northwind: the brief, read aloud")),
        "docs/roadmap.html, Northwind cards": " ".join(html_prose(c) for c in cards),
    }


@pytest.fixture(scope="module")
def printed(northwind, northwind_catalog) -> set[str]:
    d = northwind
    results, metrics = run_cells(build_cells(d), northwind_catalog)
    assert not [r.error for r in results if r.error]
    summary = executive_summary(d, metrics, "Northwind")
    parts = [note for r in results for note in r.notes]
    parts += [f"{s.found} {s.why} {s.action}" for s in d.steps]
    parts += assumptions(d) + interpret(metrics) + [summary.headline]
    parts += [f"{i.headline} {i.evidence} {i.implication} {i.action}" for i in insights(d, metrics)]
    parts += [f"{x.name} {x.score} {x.detail}" for x in trust_score(d, metrics).dimensions]
    parts.append(html_prose(to_business_brief(d, metrics, title="Northwind")))
    return numbers(" ".join(parts))


@pytest.mark.parametrize("where", list(quoted()))
def test_every_northwind_figure_in_the_docs_is_printed_by_the_code(where, printed):
    text = quoted()[where]
    assert numbers(text), f"{where}: no figures found; has the section been renamed?"
    missing = sorted(numbers(text) - printed - set(NOT_PRINTED))
    assert not missing, f"{where} quotes figures the code does not print: {missing}"


def test_counts_quoted_in_the_docs_match_the_test_suite():
    from datasets import sweep_cases
    from test_codegen_runs import CASES
    live = len([c for c in CASES if not c.endswith("_validate")]) + 1      # + shop_validate, as the live suites run them
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "guide.md").read_text(encoding="utf-8")
    for doc in (readme, guide):
        assert f"on {len(CASES)} cases" in doc and f"for {live} cases" in doc
    assert f"runs {len(sweep_cases())} more awkward datasets" in readme and f"{len(sweep_cases())} more awkward shapes" in guide
