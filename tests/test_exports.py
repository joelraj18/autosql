import io
import zipfile

import nbformat
import pytest

from autosql import build_cells
from autosql.exports import load_script, to_notebook, to_project_zip, to_sql_script


@pytest.fixture(scope="module")
def cells(northwind):
    return build_cells(northwind)


def test_sql_script(northwind, cells):
    text = to_sql_script(cells, northwind, "mysql", "Northwind CSVs")
    assert text.startswith("-- AutoSQL Explorer - MySQL 8")
    assert "Revenue = unit_price × quantity × (1 − discount)" in text
    assert "1998-Q2 is partial" in text and "DATEDIFF(o.shipped_date, o.order_date)" in text
    assert text.count("-- Why:") == len(cells)


def test_project_zip_contents(northwind, northwind_catalog, cells):
    z = zipfile.ZipFile(io.BytesIO(to_project_zip(cells, northwind, northwind_catalog, "mysql", "Northwind")))
    names = set(z.namelist())
    assert {"README.md", "01_schema.sql", "02_load.sql", "03_validate.sql", "04_analysis.sql", "05_performance.sql",
            "analysis.ipynb", "data/order_details.csv", "data/orders.csv"} <= names
    orders = z.read("data/orders.csv").decode().splitlines()
    assert orders[0].startswith("order_id,customer_id,employee_id,order_date")
    assert ",1996-07-04,1996-08-01,1996-07-16," in orders[1]
    customers = z.read("data/customers.csv").decode()
    assert ",05021," in customers  # leading zeros survive the round trip
    load = z.read("02_load.sql").decode()
    assert load.index("data/customers.csv") < load.index("data/orders.csv") < load.index("data/order_details.csv")  # parents first
    assert "SET FOREIGN_KEY_CHECKS = 0;" in load and "NULLIF(@3, '')" in load and load.count("LOAD DATA LOCAL INFILE") == 8


def test_postgres_load_script(northwind_catalog):
    text = load_script(northwind_catalog, "postgres", ["customers", "orders"])
    assert "\\copy customers (customer_id, company_name" in text and "WITH (FORMAT csv, HEADER true)" in text
    assert text.index("customers") < text.index("\\copy orders")


def test_notebook_runs(northwind, northwind_catalog, cells, tmp_path):
    from nbclient import NotebookClient

    zipfile.ZipFile(io.BytesIO(to_project_zip(cells, northwind, northwind_catalog, "postgres", "Northwind"))).extractall(tmp_path)
    nb = nbformat.read(tmp_path / "analysis.ipynb", as_version=4)
    NotebookClient(nb, timeout=300, kernel_name="python3", resources={"metadata": {"path": str(tmp_path)}}).execute()
    text = "\n".join(str(o.get("data", {}).get("text/markdown", "")) + str(o.get("text", ""))
                     for c in nb.cells if c.cell_type == "code" for o in c.get("outputs", []))
    assert "1,265,793.04" in text and "NOT IN returns 0, NOT EXISTS returns 7" in text
    assert not [o for c in nb.cells if c.cell_type == "code" for o in c.get("outputs", []) if o.get("output_type") == "error"]


def test_notebook_json_is_valid(northwind, northwind_catalog, cells):
    nb = nbformat.reads(to_notebook(cells, northwind, northwind_catalog), as_version=4)
    nbformat.validate(nb)
    assert sum(c.cell_type == "code" for c in nb.cells) == len(cells) + 1
