"""The Streamlit page, driven headlessly with streamlit.testing (no browser)."""
from pathlib import Path

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP = str(Path(__file__).resolve().parents[1] / "app.py")


def button(at, text):
    return next(b for b in at.button if text in b.label)


def test_northwind_end_to_end():
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()
    assert not at.exception
    button(at, "Northwind").click().run()
    assert not at.exception
    headers = [s.value for s in at.subheader]
    assert headers[:3] == ["1. Your tables", "2. What should we measure?", "3. The generated SQL"]
    assert any("DATEDIFF(" in c.value for c in at.code)                       # MySQL is the default dialect
    button(at, "Run all SQL").click().run()
    assert not at.exception
    assert "4. What the results mean" in [s.value for s in at.subheader]
    text = " ".join(m.value for m in at.markdown)
    assert "1,265,793.04" in text and "NOT IN returns 0, NOT EXISTS returns 7" in text
    # the business layer: headline, KPI tiles with deltas, recommendations, trust score, brief download
    assert "Revenue reached 298,491.55 in 1998-Q1" in text and "What to do" in text and "Act now · 1998-Q2 is incomplete" in text
    tiles = {m.label: m for m in at.metric}
    assert tiles["Revenue"].value == "298K" and tiles["Days to ship"].value == "8.6 days"
    assert "Trust score **99/100: High**" in text
    assert any("Business brief" in b.label for b in at.get("download_button"))
    assert any("Business question:" in c.value for c in at.caption)
    assert any("ROUND((revenue)::numeric, 2)" in str(df.value.values) for df in at.dataframe)   # MySQL vs PostgreSQL line diff
    at.sidebar.radio[1].set_value("postgres").run()                           # switching dialect keeps the results
    assert not at.exception and "4. What the results mean" in [s.value for s in at.subheader]
    assert any("o.shipped_date - o.order_date" in c.value for c in at.code)


def test_ticking_a_link_and_other_modes():
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()
    button(at, "Northwind").click().run()
    box = next(c for c in at.checkbox if "ship_via" in c.label)
    box.check().run()
    assert not at.exception
    assert "shipper_company_name" in " ".join(c.value for c in at.code)
    at.radio(key='mode-(\'northwind-example\',)').set_value("validate").run()
    assert not at.exception and "data validation" in " ".join(m.value for m in at.markdown)
    at.radio(key='mode-(\'northwind-example\',)').set_value("manual").run()
    assert not at.exception
    assert at.selectbox(key="fact-('northwind-example',)").value == "order_details"


CUSTOM = '{"assumptions": {"add": ["Figures are in US dollars."], "edit": {"revenue": "Revenue is net."}}, "kpis": [' \
         '{"key": "freight_share", "name": "Freight share", "definition": "Freight as a share of revenue.", ' \
         '"sql": "100.0 * SUM(freight) / NULLIF(SUM(revenue), 0)", "unit": "pct", "direction": "down"}, ' \
         '{"key": "typo", "name": "A typo", "definition": "Misspelt.", "sql": "SUM(frieght)"}]}'


def test_customizing_assumptions_and_kpis():
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()
    button(at, "Northwind").click().run()
    box = "custom-('northwind-example',)"
    assert any("Customize" in e.label for e in at.expander)
    button(at, "Insert an example").click().run()                                       # a valid starting point, applied as it stands
    assert not at.exception and "big_order_share" in at.text_area(key=box).value
    assert any("**Share of big orders**" in m.value and "✅" in m.value for m in at.markdown)

    at.text_area(key=box).set_value("{ not json").run()                                 # nothing is applied, and the page says why
    assert not at.exception and any("Not applied yet" in e.value and "not valid JSON" in e.value for e in at.error)
    assert "Revenue is net." not in " ".join(m.value for m in at.markdown)

    at.text_area(key=box).set_value(CUSTOM).run()
    assert not at.exception and not at.error
    marks = " ".join(m.value for m in at.markdown)
    assert "✅ **Freight share** runs on the shape of your data" in marks and "❌ **A typo**" in marks and "frieght" in marks
    assert any("You added 1 assumption(s) of your own, 1 edit(s) to the engine's assumptions and 2 KPI definition(s), 2 of them computed "
               "from your SQL" in m.value for m in at.sidebar.markdown)                           # the decision trace says so
    assert any("Your KPI: Freight share, all periods" in c.value for c in at.code)                   # the SQL you wrote is a cell like the others

    button(at, "Run all SQL").click().run()
    assert not at.exception
    tiles = {m.label: m for m in at.metric}
    assert tiles["Freight share (yours)"].value == "5.1%" and tiles["Freight share (yours)"].delta.startswith("-0.3 pts")
    assert "Revenue" in tiles and "A typo (yours)" not in tiles                                         # the engine's tiles stay; a failed KPI has none
    text = " ".join(m.value for m in at.markdown)
    assert "Revenue is net. (edited by you)" in text and "Added by you: Figures are in US dollars." in text
    assert any("**A typo** (yours) could not be computed" in w.value for w in at.warning)
    assert any("Your KPI **Freight share**" in m.value for m in at.markdown)                           # the 📌 line under its cell
