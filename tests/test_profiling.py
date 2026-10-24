import pandas as pd

from autosql import profile_catalog
from conftest import catalog_from


def test_northwind_keys_and_grain(northwind_catalog):
    p = profile_catalog(northwind_catalog)
    keys = {name: t.primary_key for name, t in p.tables.items()}
    assert keys == {
        "categories": ("category_id",), "customers": ("customer_id",), "employees": ("employee_id",),
        "order_details": ("order_id", "product_id"), "orders": ("order_id",), "products": ("product_id",),
        "shippers": ("shipper_id",), "suppliers": ("supplier_id",),
    }
    assert p["order_details"].key_status == "composite"
    assert p["shippers"].n_rows == 3  # tiny tables still get a key
    assert p["customers"].col("company_name").role == "attribute"  # unique too, but a name is not a key
    roles = {c.name: (c.role, c.kind) for c in p["order_details"].columns}
    assert roles["unit_price"] == ("measure", "price") and roles["quantity"] == ("measure", "quantity")
    assert roles["discount"] == ("measure", "discount")
    assert p["orders"].col("freight").kind == "charge"
    assert p["customers"].col("postal_code").logical_type == "text"


def test_rating_scale_date_parts_and_duplicates():
    df = pd.DataFrame({
        "id": list(range(1, 20)) + [19],                  # one duplicated id in 20 rows
        "feedback": ["Very Bad", "good", "Okay", "Very Good", "Bad"] * 4,
        "stars": [1, 5, 3, 4, 2] * 4,
        "order_date": ["2020-01-01"] * 20,
        "quarter_number": [1] * 20,
        "country": ["A", "B", "A", "B", "A"] * 4,
    })
    t = profile_catalog(catalog_from({"t": df}))["t"]
    fb = t.col("feedback")
    assert fb.role == "rating" and dict(fb.scale)["good"] == 4 and dict(fb.scale)["Very Bad"] == 1
    assert t.col("stars").role == "rating"
    assert t.col("quarter_number").role in ("date_part", "constant")
    assert t.col("country").role == "dimension"
    assert t.key_status == "intended" and t.primary_key == ("id",) and t.duplicate_keys == 1


def test_a_table_loaded_twice_keeps_its_single_key():
    df = pd.DataFrame({"order_id": range(1, 41), "customer_id": [1, 2, 3, 4] * 10, "amount": [9.5] * 40})
    t = profile_catalog(catalog_from({"orders": pd.concat([df, df.iloc[:3]])}))["orders"]
    assert t.key_status == "intended" and t.primary_key == ("order_id",)    # not order_id + customer_id: lines must still link
    assert t.exact_duplicates == 3 and "3 exact copies" in t.key_reason


def test_no_key_table():
    df = pd.DataFrame({"a": ["x", "x", "y"], "b": [1, 1, 2]})
    t = profile_catalog(catalog_from({"t": df}))["t"]
    assert t.key_status == "none" and t.exact_duplicates == 1


def test_every_rating_scale_is_recognised():
    from autosql.profiling import SCALES, match_scale
    for scale in SCALES:
        values = pd.Series([label.title() for label in scale] * 3)
        pairs = dict(match_scale(values))
        assert [pairs[label.title()] for label in scale] == [1, 2, 3, 4, 5], scale
    assert dict(match_scale(pd.Series(["Bad", "OK", "Good", "Very Good", "Very Bad"])))["OK"] == 3
    assert match_scale(pd.Series(["red", "green", "blue"])) == ()
