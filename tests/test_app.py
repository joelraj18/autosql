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
