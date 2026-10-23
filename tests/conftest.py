import numpy as np
import pandas as pd
import pytest
from sklearn.datasets import make_blobs, make_classification, make_regression


def add_messy_columns(df: pd.DataFrame, seed: int = 0) -> pd.DataFrame:
    """Columns real-world files often have: an ID, a date, a gappy category, a constant."""
    rng = np.random.default_rng(seed)
    df = df.copy()
    df.insert(0, "customer_id", range(1, len(df) + 1))
    df["signup"] = pd.date_range("2020-01-01", periods=len(df), freq="h").astype(str)
    df["city"] = rng.choice(["Pune", "Delhi", "Goa", None], len(df))
    df.loc[rng.random(len(df)) < 0.1, "f0"] = np.nan
    df["source"] = "web"
    return df


@pytest.fixture
def classification_df():
    X, y = make_classification(1500, 6, n_informative=4, weights=[0.85], random_state=0)
    df = pd.DataFrame(X, columns=[f"f{i}" for i in range(6)])
    df["churn"] = np.where(y == 1, "yes", "no")
    return add_messy_columns(df)


@pytest.fixture
def regression_df():
    X, y = make_regression(500, 5, noise=5, random_state=0)
    df = pd.DataFrame(X, columns=[f"f{i}" for i in range(5)])
    df["price"] = np.exp((y - y.min()) / 100)  # positive and right-skewed
    return add_messy_columns(df)


@pytest.fixture
def blobs_df():
    X, _ = make_blobs(600, 4, centers=3, random_state=0)
    return add_messy_columns(pd.DataFrame(X, columns=[f"f{i}" for i in range(4)]))
