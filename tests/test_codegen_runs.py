"""Generated code must actually run and beat the baseline."""
import json

import numpy as np
import pandas as pd
import pytest

from automl import build_cells, decide, interpret, profile_dataset, run_cells, to_notebook


def execute(df, target):
    d = decide(df, profile_dataset(df), target)
    cells = build_cells(d)
    results, metrics = run_cells(cells, df)
    errors = [(r.cell_id, r.stdout) for r in results if r.error]
    assert not errors, errors
    return d, cells, results, metrics


@pytest.mark.parametrize("fixture,target", [("classification_df", "churn"), ("regression_df", "price")])
def test_supervised_pipeline_runs_and_beats_baseline(request, fixture, target):
    df = request.getfixturevalue(fixture)
    _, cells, results, m = execute(df, target)
    assert m["test_score"] > m["baseline_score"] + 0.1
    assert m["top_features"]
    assert all(r.figures for r in results if r.cell_id in ("eda", "train", "explain"))
    assert all(c.code.startswith("# Why:") for c in cells)
    assert interpret(m, target)


def test_clustering_pipeline_runs(blobs_df):
    _, _, _, m = execute(blobs_df, None)  # messy columns: must still run
    assert m["k"] >= 2

    clean = blobs_df[["f1", "f2", "f3"]]  # clean blobs: must find the 3 groups
    _, _, _, m = execute(clean, None)
    assert m["k"] == 3 and m["silhouette"] > 0.5


def test_mixed_type_categories_and_unseen_values():
    rng = np.random.default_rng(0)
    n = 400
    df = pd.DataFrame({
        "x": rng.normal(size=n),
        "mixed": rng.choice([1, "two", 3.0, None], n).tolist(),  # mixed types used to crash OneHotEncoder
        "rare": [f"r{i % 60}" for i in range(n)],
    })
    df["y"] = df["x"] * 2 + rng.normal(size=n) * 0.1
    execute(df, "y")


def test_error_skips_later_cells(classification_df):
    d = decide(classification_df, profile_dataset(classification_df), "churn")
    cells = build_cells(d)
    broken = cells[:2] + [cells[2].__class__("boom", "Boom", cells[2].story, "raise ValueError('bad')")] + cells[3:]
    results, _ = run_cells(broken, classification_df)
    assert results[2].error == "ValueError: bad"
    assert all(r.skipped for r in results[3:])


def test_notebook_export_is_valid_json(classification_df):
    d = decide(classification_df, profile_dataset(classification_df), "churn")
    nb = json.loads(to_notebook(build_cells(d), d, "data.csv"))
    sources = ["".join(c["source"]) for c in nb["cells"]]
    assert any('pd.read_csv("data.csv")' in s for s in sources)
    assert any("What we found" in s for s in sources)


def test_each_plt_show_is_its_own_figure(classification_df):
    """Plots must not draw on top of each other (histograms, heatmap, class bars = 3 images)."""
    d = decide(classification_df, profile_dataset(classification_df), "churn")
    results, _ = run_cells(build_cells(d), classification_df)
    eda = next(r for r in results if r.cell_id == "eda")
    assert len(eda.figures) == 3


def test_every_cell_has_a_five_part_story_and_explains_its_result(regression_df, blobs_df):
    """Before: found / why / action / code. After running: at least one 📌 note."""
    for df, target in [(regression_df, "price"), (blobs_df[["f1", "f2", "f3"]], None)]:
        d = decide(df, profile_dataset(df), target)
        cells = build_cells(d)
        for c in cells:
            assert c.story.found and c.story.why and c.story.action and c.story.code, c.id
        results, _ = run_cells(cells, df)
        assert not [r.cell_id for r in results if r.error]
        silent = [r.cell_id for r in results if r.cell_id != "setup" and not r.notes]
        assert not silent, silent


def test_regression_runs_statistical_inference(regression_df):
    d = decide(regression_df, profile_dataset(regression_df), "price")
    assert [c.id for c in build_cells(d)][7:10] == ["vif", "ols", "assumptions"]
    _, _, _, m = execute(regression_df, "price")
    assert 0 < m["ols_r2"] <= 1 and m["ols_f_pvalue"] < 0.05
    assert set(m["assumptions"]) == {"Linearity (RESET test)", "Equal spread / homoscedasticity (Breusch-Pagan)",
                                     "Normal residuals (Jarque-Bera)"}
    assert len(m["cv_scores"]) == len(d.candidates) >= 5 and "rules_score" in m


def test_clustering_runs_pca_and_hierarchical(blobs_df):
    _, cells, _, m = execute(blobs_df[["f1", "f2", "f3"]], None)
    assert [c.id for c in cells][-4:] == ["pca", "choose_k", "cluster_fit", "hierarchical"]
    assert m["pca_n80"] <= 3 and m["cluster_agreement"] > 0.6


def test_imbalanced_data_compares_resampling_and_never_crashes():
    """Regression test: the logit cell crashed when no input was significant; resampling must stay inside the folds."""
    pytest.importorskip("imblearn")
    from sklearn.datasets import make_classification
    X, y = make_classification(1500, 6, n_informative=3, weights=[0.95], random_state=1)
    df = pd.DataFrame(X, columns=[f"f{i}" for i in range(6)])
    df["fraud"] = y
    d, cells, results, m = execute(df, "fraud")
    assert d.class_weight and d.resample and "resample" in [c.id for c in cells]
    assert set(m["balance_scores"]) >= {"class weights (current)", "random oversampling", "random undersampling"}
    assert m["balance_method"] in m["balance_scores"]


def test_tuning_never_makes_the_model_worse(classification_df):
    _, _, results, m = execute(classification_df, "churn")
    if "tuned_cv" in m:
        assert m["tuned_cv"] > m["cv_scores"][m["best_model"]]
    assert next(r for r in results if r.cell_id == "tune").notes


def test_many_exact_duplicates_are_kept_not_dropped():
    """300 rows of 3 small category columns repeat naturally; dropping copies used to leave only 9 rows."""
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"colour": rng.choice(list("RGB"), 300), "size": rng.choice(list("SML"), 300), "shop": rng.choice(list("ab"), 300)})
    d = decide(df, profile_dataset(df), None)
    assert not d.drop_duplicates and "shop" not in d.drop_cols  # 'shop' is only the last column, not named like an outcome
    results, _ = run_cells(build_cells(d), df)
    prepare = next(r for r in results if r.cell_id == "prepare")
    assert "**300 rows**" in prepare.notes[0]


def test_xgboost_handles_text_labels_when_installed():
    pytest.importorskip("xgboost")
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"x": rng.normal(size=400), "z": rng.normal(size=400)})
    df["label"] = np.where(df["x"] + rng.normal(scale=0.5, size=400) > 0, "yes", "no")
    d = decide(df, profile_dataset(df), "label")
    assert "XGBClassifier" in d.candidates
    _, _, _, m = execute(df, "label")
    assert m["cv_scores"]["XGBClassifier"] > 0.7


def test_every_test_row_having_a_twin_does_not_crash():
    """Regression test: with few input combinations, no test row is 'unseen'; scoring an empty set used to crash."""
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"colour": rng.choice(list("RGB"), 300), "size": rng.choice(list("SML"), 300),
                       "label": rng.choice(["a", "b"], 300)})
    d, _, results, m = execute(df, "label")
    assert d.lookalikes and "test_score_unseen" not in m
    train = next(r for r in results if r.cell_id == "train")
    assert any("Every test row has an identical twin" in n for n in train.notes)


# ── Regression tests from the thorough audit ──────────────────────────────────
def test_column_names_never_break_the_generated_code():
    """A target named with braces and quotes used to crash (f-string injection: `{x}` was executed)."""
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"weird col (€)": rng.normal(size=300), "kategorie ü": rng.choice(list("xyz"), 300)})
    df['target {x} "q"'] = df["weird col (€)"] * 3 + rng.normal(size=300)
    _, cells, results, _ = execute(df, 'target {x} "q"')
    assert {"insights", "ols"} <= {c.id for c in cells}
    assert all(r.notes for r in results if r.cell_id != "setup")


def test_infinity_is_treated_as_missing():
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"x": rng.normal(size=400), "ratio": np.where(rng.random(400) < 0.05, np.inf, rng.normal(size=400))})
    df["y"] = df["x"] + rng.normal(size=400)
    d, cells, _, _ = execute(df, "y")
    assert d.has_infinite and "replace([np.inf, -np.inf], np.nan)" in cells[1].code


def test_insights_with_no_group_large_enough_is_skipped_not_crashed():
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"a": rng.normal(size=31), "b": rng.normal(size=31), "c": rng.choice(list("xyz"), 31)})
    df["label"] = ["p", "q"] * 15 + ["p"]
    _, _, results, _ = execute(df, "label")
    insights = next(r for r in results if r.cell_id == "insights")
    assert any("No group of any column has at least 30 rows" in n for n in insights.notes)


def test_near_perfect_score_warns_about_leakage():
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"a": rng.normal(size=500), "b": rng.normal(size=500)})
    df["y"] = df["a"] * 2
    df["y_copy"] = df["y"]                       # the answer, hiding under another name
    _, _, results, _ = execute(df, "y")
    train = next(r for r in results if r.cell_id == "train")
    assert any("near-perfect score" in n for n in train.notes)


# ── A boosting library that is installed but will not load (macOS without libomp) ─
LIBOMP_ERROR = "dlopen(lib_lightgbm.dylib, 0x0006): Library not loaded: @rpath/libomp.dylib"


@pytest.fixture
def lightgbm_cannot_load(monkeypatch):
    """Make `import lightgbm` fail the way it does on a Mac without libomp, for the engine and for generated code."""
    import builtins
    import importlib
    import sys

    from automl.decision import library_problem

    real_import, real_import_module = builtins.__import__, importlib.import_module

    def broken(name, *args, **kwargs):
        if name == "lightgbm" or name.startswith("lightgbm."):
            raise OSError(LIBOMP_ERROR)
        return real_import(name, *args, **kwargs)

    def broken_module(name, *args, **kwargs):
        if name == "lightgbm":
            raise OSError(LIBOMP_ERROR)
        return real_import_module(name, *args, **kwargs)

    for mod in [m for m in sys.modules if m == "lightgbm" or m.startswith("lightgbm.")]:
        monkeypatch.delitem(sys.modules, mod)
    monkeypatch.setattr(builtins, "__import__", broken)
    monkeypatch.setattr(importlib, "import_module", broken_module)
    library_problem.cache_clear()
    yield
    library_problem.cache_clear()


def test_an_unloadable_lightgbm_is_skipped_and_explained(lightgbm_cannot_load, classification_df):
    """Regression test: on macOS without libomp, `import lightgbm` raised OSError and stopped the whole run."""
    d, _, results, m = execute(classification_df, "churn")
    assert "LGBMClassifier" not in d.candidates and "LGBMClassifier" not in m["cv_scores"]
    step = next(s for s in d.steps if s.key == "boosting_libs_broken")
    assert "libomp" in step.found and "brew install libomp" in step.action


def test_an_exported_notebook_survives_a_library_that_will_not_load(classification_df, monkeypatch):
    """The notebook may run on another computer: built where LightGBM works, run where it does not."""
    pytest.importorskip("lightgbm")
    d = decide(classification_df, profile_dataset(classification_df), "churn")
    cells = build_cells(d)
    assert "LGBMClassifier" in d.candidates
    import builtins
    real_import = builtins.__import__

    def broken(name, *args, **kwargs):
        if name == "lightgbm":
            raise OSError(LIBOMP_ERROR)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken)
    results, m = run_cells(cells, classification_df)
    assert not [r.cell_id for r in results if r.error]
    compare = next(r for r in results if r.cell_id == "compare")
    assert any("Skipped LGBMClassifier" in n and "brew install libomp" in n for n in compare.notes)
    assert "LGBMClassifier" not in m["cv_scores"]
