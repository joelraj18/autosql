"""Build the Northwind CSVs from Microsoft's original install script.

    python examples/northwind/build_data.py            # download the script (pinned commit) and write the CSVs
    python examples/northwind/build_data.py --source instnwnd.sql   # use a local copy instead

Source: https://github.com/microsoft/sql-server-samples (samples/databases/northwind-pubs/instnwnd.sql), MIT licence.
The CSVs keep the original table and column names (``Order Details``, ``OrderID``) on purpose: renaming them to
``order_details`` / ``order_id`` is part of what AutoSQL Explorer does. Image columns (Categories.Picture,
Employees.Photo) are left out. Dates become ISO ``YYYY-MM-DD``; NULL becomes an empty field.

It also writes ``expected_fks.json``: the FOREIGN KEY constraints declared in the script, which the tests compare
with the relationships AutoSQL infers from the data alone.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sqlite3
import ssl
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMIT = "dac9747acaca4e54a3b5ea4c6de1c4be6c1b964a"
URL = f"https://raw.githubusercontent.com/microsoft/sql-server-samples/{COMMIT}/samples/databases/northwind-pubs/instnwnd.sql"
BLOBS = {("Categories", "Picture"), ("Employees", "Photo")}
DATE_COLUMNS = {"OrderDate", "RequiredDate", "ShippedDate", "BirthDate", "HireDate"}


def download() -> str:
    context = ssl.create_default_context()
    proxy_ca = Path("/root/.ccr/ca-bundle.crt")
    if proxy_ca.exists():  # sandboxed environments route HTTPS through a proxy with its own CA
        context.load_verify_locations(proxy_ca)
    return urllib.request.urlopen(URL, context=context, timeout=60).read().decode("utf-8-sig")


def table_columns(script: str) -> dict[str, list[str]]:
    """Column order from each CREATE TABLE "Name" ( "Col" type, ... CONSTRAINT ... )."""
    out = {}
    for name, body in re.findall(r'CREATE TABLE "([A-Za-z ]+)" \((.*?)\n\)\s*\nGO', script, re.S):
        out[name] = [c for c in re.findall(r'^\s*"(\w+)"\s+(?:"?\w+"?)', body, re.M)]
    return out


def foreign_keys(script: str) -> list[dict]:
    pattern = r'CONSTRAINT "FK_\w+" FOREIGN KEY\s*\(\s*"(\w+)"\s*\) REFERENCES "dbo"\."(\w+)" \(\s*"(\w+)"\s*\)'
    owners = {}
    for name, body in re.findall(r'CREATE TABLE "([A-Za-z ]+)" \((.*?)\n\)\s*\nGO', script, re.S):
        for col, parent, parent_col in re.findall(pattern, body):
            owners[(name, col)] = (parent, parent_col)
    return [{"child_table": t, "child_col": c, "parent_table": p, "parent_col": pc} for (t, c), (p, pc) in sorted(owners.items())]


def rows(script: str, columns: dict[str, list[str]]) -> dict[str, list[dict]]:
    """Evaluate each INSERT's VALUES list with SQLite (after removing T-SQL's N'' prefix and hex image data)."""
    con = sqlite3.connect(":memory:")
    out: dict[str, list[dict]] = {}
    insert = re.compile(r'INSERT\s+(?:INTO\s+)?"([A-Za-z ]+)"\s*(\([^)]*\))?\s*VALUES\s*\((.*?)\)\s*\n', re.S)
    for table, column_list, values in insert.findall(script):
        values = re.sub(r"\bN'", "'", values)
        values = re.sub(r"0x[0-9A-Fa-f]+", "NULL", values)
        names = re.findall(r'"(\w+)"', column_list) if column_list else columns[table]
        out.setdefault(table, []).append(dict(zip(names, con.execute("SELECT " + values).fetchone())))
    return out


def iso(value):
    return None if value is None else dt.datetime.strptime(value, "%m/%d/%Y").date().isoformat()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", help="local copy of instnwnd.sql")
    args = parser.parse_args()
    script = Path(args.source).read_text(encoding="utf-8-sig") if args.source else download()
    columns = table_columns(script)
    data = rows(script, columns)
    for table, records in data.items():
        keep = [c for c in columns[table] if (table, c) not in BLOBS]
        with open(HERE / f"{table}.csv", "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(keep)
            for record in records:
                writer.writerow(["" if record.get(c) is None else iso(record[c]) if c in DATE_COLUMNS else record[c] for c in keep])
        print(f"{table}.csv: {len(records)} rows")
    (HERE / "expected_fks.json").write_text(json.dumps(foreign_keys(script), indent=2) + "\n")
    print(f"expected_fks.json: {len(foreign_keys(script))} foreign keys")


if __name__ == "__main__":
    main()
