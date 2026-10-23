import io
import zipfile

import pandas as pd

from autosql.loading import MAX_ZIP_MEMBERS, build_database, read_files, snake_case, unique_names
from conftest import as_csv


def test_snake_case_names():
    assert snake_case("OrderID") == "order_id"
    assert snake_case("Order Details") == "order_details"
    assert snake_case("HTTPStatus") == "http_status"
    assert snake_case("Größe") == "grosse"
    assert snake_case("2nd value") == "c_2nd_value"
    assert snake_case("   ") == "unnamed"
    assert unique_names(["a", "a", "a_2"]) == ["a", "a_2", "a_2_2"]


def test_text_first_typing_keeps_codes_and_parses_dates():
    df = pd.DataFrame({
        "Zip": ["05021", "12209", "01307"],                  # leading zeros: must stay text
        "Qty": ["1", "2", ""],                               # empty -> NULL, still an integer column
        "Price": ["9.5", "10", "3.25"],
        "Whole": ["1.0", "2.0", "3.0"],                      # Excel-style whole numbers
        "When": ["7/4/1996", "7/16/1996", "12/31/1996"],     # m/d/Y
        "Stamp": ["2020-01-01 10:00:00", "2020-01-02 00:00:00", None],
        "Flag": ["true", "FALSE", "true"],
    })
    t = read_files([("Sales Data.csv", as_csv(df))])["sales_data"]
    assert t.types == {"zip": "text", "qty": "integer", "price": "decimal", "whole": "integer", "when": "date",
                       "stamp": "timestamp", "flag": "boolean"}
    assert t.df["zip"].tolist() == ["05021", "12209", "01307"]
    assert t.df["qty"].isna().sum() == 1
    assert t.df["when"].iloc[1] == pd.Timestamp("1996-07-16")
    assert t.column_map["zip"] == "Zip"


def test_ambiguous_and_unparsed_dates_are_reported():
    df = pd.DataFrame({"d": ["01/02/2020", "03/04/2020", "05/06/2020"] * 10 + ["not a date"]})
    t = read_files([("x.csv", as_csv(df))])["x"]
    assert t.types["d"] == "date"
    assert t.ambiguous_dates == ["d"]
    assert t.unparsed_dates == {"d": 1}


def test_zip_and_excel_and_bad_files():
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as xl:
        pd.DataFrame({"a": [1, 2]}).to_excel(xl, sheet_name="First", index=False)
        pd.DataFrame({"b": ["x", "y"]}).to_excel(xl, sheet_name="Second", index=False)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("folder/orders.csv", "id,v\n1,2\n")
        z.writestr("__MACOSX/._orders.csv", "junk")
        z.writestr("book.xlsx", buf.getvalue())
    catalog = read_files([("bundle.zip", archive.getvalue()), ("broken.zip", b"nope"), ("notes.pdf", b"%PDF")])
    assert set(catalog.names) == {"orders", "book_first", "book_second"}
    assert {s for s, _ in catalog.skipped} == {"broken.zip", "notes.pdf"}

    many = io.BytesIO()
    with zipfile.ZipFile(many, "w") as z:
        for i in range(MAX_ZIP_MEMBERS + 1):
            z.writestr(f"t{i}.csv", "a\n1\n")
    assert read_files([("many.zip", many.getvalue())]).skipped


def test_database_is_typed_and_locked():
    df = pd.DataFrame({"order": ["1", "2"], "d": ["2020-01-01", "2020-02-01"]})  # a reserved word as a column name
    catalog = read_files([("t.csv", as_csv(df))])
    con = build_database(catalog)
    assert con.execute('SELECT typeof(d) FROM t LIMIT 1').fetchone()[0] == "DATE"
    assert con.execute('SELECT SUM("order") FROM t').fetchone()[0] == 3
    try:
        con.execute("SELECT * FROM read_csv('/etc/hostname')").fetchall()
        raise AssertionError("local files must not be readable")
    except Exception as error:
        assert "disabled" in str(error) or "Permission" in type(error).__name__


def test_iso_timestamps_with_zones_and_non_latin_names():
    df = pd.DataFrame({"城市": ["北京", "上海"] * 15, "created_at": [f"2022-01-{1 + i % 28:02d}T10:00:00Z" for i in range(30)]})
    t = read_files([("t.csv", as_csv(df))])["t"]
    assert list(t.df.columns) == ["column_1", "created_at"] and t.column_map["column_1"] == "城市"
    assert t.types["created_at"] == "timestamp" and t.df["created_at"].iloc[0] == pd.Timestamp("2022-01-01 10:00:00")


def test_semicolon_files_with_decimal_commas():
    text = "id;amount;big;code\n" + "\n".join(f"{i};{i},5;1.234,{i:02d};0{i}" for i in range(30))
    t = read_files([("eu.csv", text.encode())])["eu"]
    assert t.types == {"id": "integer", "amount": "decimal", "big": "decimal", "code": "text"}
    assert t.df["amount"].iloc[1] == 1.5 and t.df["big"].iloc[1] == 1234.01 and t.df["code"].iloc[0] == "00"
    quoted = read_files([("q.csv", b'name,city\n"Smith; John",Paris\n"Doe; Jane",Rome\n')])["q"]
    assert list(quoted.df.columns) == ["name", "city"]  # a separator inside quotes doesn't count
    assert list(read_files([("p.csv", b"a|b\n1|x\n2|y\n")])["p"].df.columns) == ["a", "b"]
