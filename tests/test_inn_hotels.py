"""INNHotels is the real dataset behind examples/inn_hotels. The engine must handle it on its own."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from automl import build_cells, decide, interpret, profile_dataset, run_cells
from automl.profiling import suggest_target

CSV = Path(__file__).resolve().parents[1] / "examples" / "inn_hotels" / "INNHotelsGroup.csv"


@pytest.fixture(scope="module")
def hotels():
    return pd.read_csv(CSV)


def test_profile_spots_target_lookalikes_and_split_dates(hotels):
    p = profile_dataset(hotels)
    assert p.suggested_target == "booking_status"
    assert p.duplicate_rows == 0 and p.lookalike_rows == 10_275  # duplicates hidden behind Booking_ID
    (dp,) = p.date_parts
    assert (dp.year, dp.month, dp.day, dp.invalid) == ("arrival_year", "arrival_month", "arrival_date", 37)


def test_clustering_leaves_out_the_outcome_column(hotels):
    d = decide(hotels, profile_dataset(hotels), None)
    assert d.task == "clustering"
    assert "booking_status" in d.drop_cols and "booking_status" not in d.cat_features


def test_full_pipeline_beats_the_hand_built_notebook(hotels):
    d = decide(hotels, profile_dataset(hotels), "booking_status")
    assert (d.task, d.subtype, d.positive_class) == ("classification", "binary", "Canceled")
    assert "arrival_weekday" in d.num_features and d.lookalikes

    cells = build_cells(d)
    assert [c.id for c in cells] == ["setup", "prepare", "eda", "insights", "split", "preprocess", "baseline", "logit",
                                     "compare", "tune", "train", "threshold", "rules", "explain"]  # balanced enough: no resampling
    results, m = run_cells(cells, hotels)
    assert not [r.cell_id for r in results if r.error]
    assert all(r.notes for r in results if r.cell_id != "setup")  # every step explains what it found

    assert m["best_model"] in d.candidates and len(m["cv_scores"]) == len(d.candidates)
    assert m["roc_auc"] > 0.9
    assert m["f1_tuned"] >= 0.80            # the hand-built notebook's best tree reached 0.81 with test leakage
    assert m["test_score_unseen"] <= m["test_score"]   # rows with training twins are easier
    assert "lead_time" in m["drivers"]
    text = " ".join(interpret(m, "booking_status"))
    assert "threshold" in text and "no identical twin" in text


def test_target_suggestion_falls_back_to_a_last_label_column():
    df = pd.DataFrame({"a": np.arange(100) * 1.5, "b": np.random.default_rng(0).random(100), "kind": ["x", "y"] * 50})
    assert suggest_target(profile_dataset(df).columns)[0] == "kind"
    assert suggest_target(profile_dataset(df[["a", "b"]]).columns)[0] is None
