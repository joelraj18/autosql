import numpy as np
import pandas as pd

from automl.profiling import profile_dataset


def roles(df):
    return {c.name: c.role for c in profile_dataset(df).columns}


def test_messy_columns_get_the_right_roles(classification_df):
    r = roles(classification_df)
    assert r["customer_id"] == "id"
    assert r["signup"] == "datetime"
    assert r["city"] == "categorical"
    assert r["source"] == "constant"
    assert r["f0"] == "numeric"


def test_all_unique_float_is_a_quantity_not_an_id():
    df = pd.DataFrame({"price": np.random.default_rng(0).random(100) * 1000})
    assert roles(df)["price"] == "numeric"


def test_text_high_cardinality_and_string_ids():
    rng = np.random.default_rng(0)
    n = 200
    df = pd.DataFrame({
        "review": [f"this product was really quite good, I would buy it again {i}" for i in range(n)],
        "zip": rng.integers(0, 120, n).astype(str),
        "order_ref": [f"ORD-{i:05d}" for i in range(n)],
        "numbers_as_text": rng.integers(0, 5, n).astype(str),
    })
    r = roles(df)
    assert r["review"] == "text"
    assert r["zip"] == "high_card_categorical"
    assert r["order_ref"] == "id"
    assert r["numbers_as_text"] == "categorical"  # not mistaken for dates


def test_dataset_level_numbers():
    df = pd.DataFrame({"a": [1, 1, 2, np.nan], "b": ["x", "x", "y", "z"]})
    p = profile_dataset(df)
    assert (p.n_rows, p.n_cols, p.duplicate_rows) == (4, 2, 1)
    assert p.missing_frac == 1 / 8
    assert list(p.to_frame()["column"]) == ["a", "b"]
