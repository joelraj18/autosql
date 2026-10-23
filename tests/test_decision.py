import numpy as np
import pandas as pd

from automl.decision import decide
from automl.profiling import profile_dataset


def run(df, target):
    return decide(df, profile_dataset(df), target)


def keys(d):
    return [s.key for s in d.steps]


def test_messy_columns_are_dropped_or_converted(classification_df):
    d = run(classification_df, "churn")
    assert set(d.drop_cols) == {"customer_id", "source"}
    assert d.date_cols == ["signup"]
    assert "signup_year" in d.num_features and "city" in d.cat_features
    assert {"id_column", "constant_column", "datetime_column", "missing_values"} <= set(keys(d))


def test_imbalanced_binary_classification(classification_df):
    d = run(classification_df, "churn")
    assert (d.task, d.subtype, d.model) == ("classification", "binary", "RandomForestClassifier")
    assert d.class_weight and d.stratify and d.primary_metric == "Macro F1"
    assert "class_weight='balanced'" in d.model_constructor()
    assert "imbalanced" in keys(d)


def test_numeric_target_with_few_values_is_classification():
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"x": rng.random(300), "grade": rng.integers(1, 6, 300)})
    d = run(df, "grade")
    assert d.task == "classification" and d.subtype == "multiclass"
    assert d.model == "LogisticRegression"  # small data -> simple model


def test_skewed_regression_uses_log_target(regression_df):
    d = run(regression_df, "price")
    assert d.task == "regression" and d.log_target
    assert d.model == "LinearRegression" and d.scale_numeric


def test_collinear_small_regression_uses_ridge():
    rng = np.random.default_rng(0)
    a = rng.normal(size=200)
    df = pd.DataFrame({"a": a, "a_copy": a + rng.normal(scale=0.01, size=200), "y": a * 3 + rng.normal(size=200)})
    assert run(df, "y").model == "RidgeCV"


def test_clustering_without_target(blobs_df):
    d = run(blobs_df, None)
    assert d.task == "clustering" and d.model == "KMeans" and d.scale_numeric


def test_large_data_is_sampled_and_uses_fast_model():
    rng = np.random.default_rng(0)
    n = 120_000
    df = pd.DataFrame({"x": rng.random(n), "y": rng.random(n) * 100})
    d = run(df, "y")
    assert d.model == "HistGradientBoostingRegressor"
    assert d.sample_rows == 100_000 and "sampled" in keys(d)  # 5 candidate models are compared, so cap the rows
    d = run(pd.DataFrame({"x": rng.random(60_000), "y": rng.random(60_000) * 100}), "y")
    assert d.model == "RandomForestRegressor" and d.sample_rows == 50_000


def test_rare_classes_are_removed():
    rng = np.random.default_rng(0)
    labels = ["a"] * 100 + ["b"] * 100 + ["only_once"]
    df = pd.DataFrame({"x": rng.random(201), "label": labels})
    d = run(df, "label")
    assert d.rare_classes == ["only_once"] and d.subtype == "binary"


def test_quality_gates():
    assert keys(run(pd.DataFrame({"a": range(10), "b": range(10)}), None))[-1] == "halt_small"
    assert run(pd.DataFrame({"a": range(100)}), None).halted
    empty = pd.DataFrame({"a": [np.nan] * 99 + [1.0], "b": [np.nan] * 99 + [2.0]})
    assert keys(run(empty, None))[-1] == "halt_missing"
    dates = pd.DataFrame({"x": range(50), "when": pd.date_range("2024-01-01", periods=50)})
    assert keys(run(dates, "when"))[-1] == "halt_target_type"


def test_every_step_reads_as_a_sentence(classification_df):
    for s in run(classification_df, "churn").steps:
        assert s.found and s.why and s.action
        assert "{" not in s.markdown()  # no unfilled template placeholders


def test_metric_choice_follows_class_balance():
    """Accuracy hides failure on a minority class, so it is only used when classes are close to even."""
    rng = np.random.default_rng(0)
    x = rng.random(1000)
    even = pd.DataFrame({"x": x, "y": np.where(x > 0.5, "a", "b")})                      # 50 / 50
    uneven = pd.DataFrame({"x": x, "y": np.where(x > 0.7, "yes", "no")})                 # 30 / 70
    rare = pd.DataFrame({"x": x, "y": np.where(x > 0.9, "yes", "no")})                   # 10 / 90
    assert run(even, "y").primary_metric == "Accuracy"
    d = run(uneven, "y")
    assert d.primary_metric == "Macro F1" and not d.class_weight and "uneven" in keys(d)
    d = run(rare, "y")
    assert d.primary_metric == "Macro F1" and d.class_weight


# ── Choosing the target: let the engine decide / pick it / no target ─────────
def test_auto_mode_finds_an_outcome_column():
    from pathlib import Path
    from automl.decision import resolve_target
    hotels = pd.read_csv(Path(__file__).resolve().parents[1] / "examples" / "inn_hotels" / "INNHotelsGroup.csv")
    p = profile_dataset(hotels)
    target, step = resolve_target(p, "auto")
    assert target == "booking_status" and step.key == "target_auto_found"
    d = decide(hotels, p, target, step)
    assert d.task == "classification" and keys(d)[1] == "target_auto_found"   # second line of the trace

    target, step = resolve_target(p, "none")                                   # same file, user says "no target"
    d = decide(hotels, p, target, step)
    assert target is None and d.task == "clustering" and "booking_status" in d.drop_cols


def test_auto_mode_chooses_clustering_when_nothing_looks_like_an_outcome():
    from automl.decision import resolve_target
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"height": rng.normal(170, 10, 200), "weight": rng.normal(70, 8, 200)})
    p = profile_dataset(df)
    target, step = resolve_target(p, "auto")
    assert target is None and step.key == "target_auto_none"
    assert decide(df, p, target, step).task == "clustering"


def test_manual_mode_respects_the_user_and_mentions_the_engines_suggestion():
    from automl.decision import resolve_target
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"x": rng.random(300), "price": rng.random(300) * 100, "status": rng.choice(["a", "b"], 300),
                       "when": pd.date_range("2024-01-01", periods=300).astype(str)})
    p = profile_dataset(df)
    target, step = resolve_target(p, "manual", "price")
    assert target == "price" and "`status`" in step.found                     # engine would have picked status
    assert decide(df, p, target, step).task == "regression"
    _, same = resolve_target(p, "manual", "status")
    assert "suggests the same column" in same.found
    target, step = resolve_target(p, "manual", "when")                       # a date can't be predicted here
    assert keys(decide(df, p, target, step))[-1] == "halt_target_type"
    for bad in [("manual", "nope"), ("guess", None)]:
        try:
            resolve_target(p, *bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_auto_mode_finds_a_numeric_outcome_by_name():
    from sklearn.datasets import load_diabetes
    from automl.decision import resolve_target
    df = load_diabetes(as_frame=True).frame        # outcome column is literally called "target", 214 values
    p = profile_dataset(df)
    target, step = resolve_target(p, "auto")
    assert target == "target" and "numeric with 214 values" in step.found
    assert decide(df, p, target, step).task == "regression"


def test_an_id_column_cannot_be_the_target():
    df = pd.DataFrame({"Booking_ID": [f"B{i:05d}" for i in range(200)], "x": np.random.default_rng(0).normal(size=200)})
    d = run(df, "Booking_ID")
    assert d.halted and keys(d)[-1] == "halt_target_type" and "names rows" in d.steps[-1].why
