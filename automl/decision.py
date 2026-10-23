"""The decision engine.

Reads the dataset profile plus the chosen target and decides, step by step:

  Stage 0  quality gates     - is the data usable at all? which columns to drop?
  Stage 1  task              - clustering, regression or classification?
  Stage 2  model             - which algorithm suits this size and shape of data?
  Stage 3  speed             - do we need to sample rows to stay fast?

Every choice is recorded as a narrative ``Step`` so the UI can explain it.
"""
from __future__ import annotations

import functools
import importlib
import importlib.util
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from pandas.api.types import is_numeric_dtype

from .narrative import Step, story
from .profiling import DatasetProfile

MIN_ROWS = 30
MAX_MISSING_TOTAL = 0.6        # halt if more than this share of all cells is empty
MAX_MISSING_COLUMN = 0.4       # drop a column if more than this share is empty
MAX_CLASSES_AS_LABEL = 15      # numeric target with <= this many values -> classification
SMALL_DATA = 1_000
LARGE_DATA = 100_000
TRAIN_SAMPLE = {"tree": 50_000, "hgb": 100_000, "linear": 100_000, "boost": 100_000}
IMBALANCE_RATIO = 0.25        # smallest / largest class below this -> imbalanced: class weights (+ resampling step)
UNEVEN_SHARE = 0.40           # binary minority under 40% (scaled for more classes) -> judge by macro F1, not accuracy
MAX_DUPLICATE_SHARE = 0.05     # up to 5% exact copies = probably accidental -> drop; more = real repeats -> keep
SMALL_SAMPLE_WARNING = 200     # below this many rows every score is noisy


@functools.lru_cache(maxsize=None)
def library_problem(module: str) -> str | None:
    """None when an optional library (xgboost, lightgbm, imblearn) really loads; otherwise a short reason.

    Checking that the files exist is not enough: on macOS, pip's LightGBM / XGBoost need the OpenMP runtime (libomp),
    and without it ``import lightgbm`` raises OSError. So we actually import the library, once, and remember the answer.
    """
    if importlib.util.find_spec(module) is None:
        return "not installed"
    try:
        importlib.import_module(module)
    except Exception as exc:  # OSError (missing libomp), ImportError (broken or mismatched build), ...
        first_line = (str(exc).splitlines() or [""])[0]
        return f"{type(exc).__name__}: {first_line[:160]}"
    return None


def installed(module: str) -> bool:
    """Optional libraries are used only when they are present AND load without error."""
    return library_problem(module) is None
CLUSTER_SAMPLE = 100_000
TEST_SIZE = 0.2

# key -> (import line, constructor, family). ``{cw}`` is filled with class_weight when needed.
MODELS: dict[str, tuple[str, str, str]] = {
    "LogisticRegression": ("from sklearn.linear_model import LogisticRegression", "LogisticRegression(max_iter=1000{cw})", "linear"),
    "DecisionTreeClassifier": ("from sklearn.tree import DecisionTreeClassifier", "DecisionTreeClassifier(max_depth=8, min_samples_leaf=20, random_state=42{cw})", "tree"),
    "RandomForestClassifier": ("from sklearn.ensemble import RandomForestClassifier", "RandomForestClassifier(n_estimators=200, min_samples_leaf=2, oob_score=True, n_jobs=-1, random_state=42{cw})", "tree"),
    "AdaBoostClassifier": ("from sklearn.ensemble import AdaBoostClassifier", "AdaBoostClassifier(n_estimators=100, random_state=42)", "boost"),
    "HistGradientBoostingClassifier": ("from sklearn.ensemble import HistGradientBoostingClassifier", "HistGradientBoostingClassifier(random_state=42{cw})", "hgb"),
    # XGBoost only accepts classes numbered 0, 1, 2 ...: LabelEncoded (defined in the compare cell) translates text labels.
    "XGBClassifier": ("from xgboost import XGBClassifier", "LabelEncoded(XGBClassifier(n_estimators=300, learning_rate=0.1, max_depth=6, subsample=0.8, colsample_bytree=0.8, n_jobs=-1, random_state=42))", "boost"),
    "LGBMClassifier": ("from lightgbm import LGBMClassifier", "LGBMClassifier(n_estimators=300, learning_rate=0.05, num_leaves=31, subsample=0.8, subsample_freq=1, colsample_bytree=0.8, n_jobs=-1, verbose=-1, random_state=42{cw})", "boost"),
    "LinearRegression": ("from sklearn.linear_model import LinearRegression", "LinearRegression()", "linear"),
    "RidgeCV": ("from sklearn.linear_model import RidgeCV", "RidgeCV(alphas=np.logspace(-3, 3, 13))", "linear"),
    "LassoCV": ("from sklearn.linear_model import LassoCV", "LassoCV(cv=5, random_state=42)", "linear"),
    "DecisionTreeRegressor": ("from sklearn.tree import DecisionTreeRegressor", "DecisionTreeRegressor(max_depth=8, min_samples_leaf=20, random_state=42)", "tree"),
    "RandomForestRegressor": ("from sklearn.ensemble import RandomForestRegressor", "RandomForestRegressor(n_estimators=200, min_samples_leaf=2, oob_score=True, n_jobs=-1, random_state=42)", "tree"),
    "AdaBoostRegressor": ("from sklearn.ensemble import AdaBoostRegressor", "AdaBoostRegressor(n_estimators=100, random_state=42)", "boost"),
    "HistGradientBoostingRegressor": ("from sklearn.ensemble import HistGradientBoostingRegressor", "HistGradientBoostingRegressor(random_state=42)", "hgb"),
    "XGBRegressor": ("from xgboost import XGBRegressor", "XGBRegressor(n_estimators=300, learning_rate=0.1, max_depth=6, subsample=0.8, colsample_bytree=0.8, n_jobs=-1, random_state=42)", "boost"),
    "LGBMRegressor": ("from lightgbm import LGBMRegressor", "LGBMRegressor(n_estimators=300, learning_rate=0.05, num_leaves=31, subsample=0.8, subsample_freq=1, colsample_bytree=0.8, n_jobs=-1, verbose=-1, random_state=42)", "boost"),
    "KMeans": ("from sklearn.cluster import KMeans", "KMeans(n_clusters=k, n_init=10, random_state=42)", "cluster"),
    "MiniBatchKMeans": ("from sklearn.cluster import MiniBatchKMeans", "MiniBatchKMeans(n_clusters=k, n_init=3, batch_size=2048, random_state=42)", "cluster"),
}


@dataclass
class Decision:
    task: str                                  # "classification" | "regression" | "clustering" | "halt"
    target: str | None = None
    subtype: str = ""                          # "binary" | "multiclass" for classification
    model: str = ""
    class_weight: bool = False
    log_target: bool = False
    stratify: bool = False
    primary_metric: str = ""
    sample_rows: int | None = None
    drop_duplicates: bool = False
    drop_cols: list[str] = field(default_factory=list)
    date_cols: list[str] = field(default_factory=list)
    num_features: list[str] = field(default_factory=list)
    cat_features: list[str] = field(default_factory=list)
    rare_classes: list = field(default_factory=list)
    positive_class: object = None              # binary classification: the (rarer) class to catch
    candidates: list[str] = field(default_factory=list)   # models compared by cross-validation
    date_parts: list[tuple[str, str, str, str]] = field(default_factory=list)  # (prefix, year, month, day)
    lookalikes: bool = False                   # repeated rows exist (identical apart from an ID, or many exact copies)
    resample: bool = False                     # imbalanced + imblearn installed -> test over/under-sampling
    has_infinite: bool = False                 # ±infinity found -> replaced by missing values in the prepare cell
    k_range: tuple[int, int] = (2, 8)
    steps: list[Step] = field(default_factory=list)

    @property
    def halted(self) -> bool:
        return self.task == "halt"

    @property
    def family(self) -> str:
        return MODELS[self.model][2] if self.model else ""

    @property
    def scale_numeric(self) -> bool:
        # Linear and distance-based models need scaling; trees ignore it, so scaling is always safe.
        return True

    def model_import(self, name: str | None = None) -> str:
        return MODELS[name or self.model][0]

    def model_constructor(self, name: str | None = None) -> str:
        cw = ", class_weight='balanced'" if self.class_weight else ""
        return MODELS[name or self.model][1].format(cw=cw)


TARGET_MODES = ("auto", "manual", "none")  # let the engine decide / the user picks / no target (clustering)


def resolve_target(profile: DatasetProfile, mode: str, chosen: str | None = None) -> tuple[str | None, Step]:
    """Turn the user's choice into a target column (or None for clustering) plus a Step explaining who chose it and why."""
    if mode not in TARGET_MODES:
        raise ValueError(f"mode must be one of {TARGET_MODES}, not {mode!r}")
    suggested = profile.suggested_target
    if mode == "auto":
        if suggested:
            return suggested, story("target_auto_found", col=suggested, reason=profile.target_reason)
        return None, story("target_auto_none")
    if mode == "none":
        return None, story("target_none")
    if chosen not in {c.name for c in profile.columns}:
        raise ValueError(f"{chosen!r} is not a column of this file")
    hint = (f" (The engine would have suggested `{suggested}`.)" if suggested and suggested != chosen else
            " (The engine suggests the same column.)" if suggested == chosen else "")
    return chosen, story("target_manual", col=chosen, hint=hint)


def _halt(d: Decision, key: str, **values) -> Decision:
    d.task = "halt"
    d.steps.append(story(key, **values))
    return d


def _max_correlation(df: pd.DataFrame, cols: list[str]) -> tuple[float, str, str]:
    """Strongest absolute correlation between two different feature columns."""
    if len(cols) < 2:
        return 0.0, "", ""
    sample = df[cols[:50]].sample(min(len(df), 5_000), random_state=0)
    corr = sample.corr().abs().to_numpy(copy=True)
    np.fill_diagonal(corr, 0)
    corr = np.nan_to_num(corr)
    i, j = np.unravel_index(np.argmax(corr), corr.shape)
    return float(corr[i, j]), cols[i], cols[j]


def decide(df: pd.DataFrame, profile: DatasetProfile, target: str | None, target_step: Step | None = None) -> Decision:
    """`target_step` (from `resolve_target`) records who chose the target and why; it appears second in the trace."""
    d = Decision(task="halt", target=target)
    rows = profile.n_rows

    # ── Stage 0: quality gates ────────────────────────────────────────────
    n_num = len(profile.by_role("numeric"))
    d.steps.append(story("overview", rows=rows, cols=profile.n_cols, n_num=n_num, n_cat=profile.n_cols - n_num))
    if target_step is not None:
        d.steps.append(target_step)
    if rows < MIN_ROWS:
        return _halt(d, "halt_small", rows=rows)
    if profile.n_cols < 2:
        return _halt(d, "halt_columns", cols=profile.n_cols)
    if profile.missing_frac > MAX_MISSING_TOTAL:
        return _halt(d, "halt_missing", pct=profile.missing_frac * 100)

    if target is not None:
        tp = profile.get(target)
        if tp.role in ("text", "datetime", "constant", "id"):
            reason = {
                "text": "Free text has too many unique values to predict as a label or a number.",
                "datetime": "Predicting a date is a forecasting problem, which this tool doesn't cover yet.",
                "constant": "It has only one value, so there is nothing to predict.",
                "id": "It has a different value in (almost) every row: it names rows rather than describing an outcome, so there is nothing to learn.",
            }[tp.role]
            return _halt(d, "halt_target_type", target=target, role=tp.role, reason=reason)

    if profile.infinite_cells:
        d.has_infinite = True
        d.steps.append(story("infinite_values", n=sum(profile.infinite_cells.values()),
                             examples=", ".join(f"`{c}`" for c in list(profile.infinite_cells)[:3])))
    if profile.duplicate_rows:
        share = profile.duplicate_rows / rows
        if share <= MAX_DUPLICATE_SHARE:
            d.drop_duplicates = True
            d.steps.append(story("duplicates", n=profile.duplicate_rows, pct=share * 100))
        else:
            d.lookalikes = target is not None
            extra = (", and also score the model on test rows that have no exact twin in training, so that the score stays honest"
                     if target is not None else ", so that common patterns keep their real weight in the groups")
            d.steps.append(story("duplicates_kept", n=profile.duplicate_rows, pct=share * 100, extra=extra))
    if profile.lookalike_rows and target is not None:
        d.lookalikes = True
        ids = ", ".join(f"`{c}`" for c in profile.by_role("id"))
        d.steps.append(story("lookalike_rows", n=profile.lookalike_rows, pct=profile.lookalike_rows / rows * 100, ids=ids))

    # Only a column whose NAME says "outcome" is kept out of clustering; being the last column is too weak a reason.
    target_like = profile.suggested_target if target is None and profile.target_by_name else None

    for c in profile.columns:
        if c.name == target:
            continue
        if c.name == target_like:
            d.drop_cols.append(c.name)
            d.steps.append(story("target_like_excluded", col=c.name, reason=profile.target_reason))
        elif c.role == "id":
            d.drop_cols.append(c.name)
            d.steps.append(story("id_column", col=c.name, pct=min(c.n_unique / max(rows * (1 - c.missing_frac), 1), 1) * 100))
        elif c.role == "constant":
            d.drop_cols.append(c.name)
            d.steps.append(story("constant_column", col=c.name))
        elif c.role == "text":
            d.drop_cols.append(c.name)
            d.steps.append(story("text_column", col=c.name, avg_len=c.avg_len))
        elif c.missing_frac > MAX_MISSING_COLUMN:
            d.drop_cols.append(c.name)
            d.steps.append(story("missing_column", col=c.name, pct=c.missing_frac * 100))
        elif c.role == "datetime":
            d.date_cols.append(c.name)
            d.num_features += [f"{c.name}_year", f"{c.name}_month", f"{c.name}_weekday"]
            d.steps.append(story("datetime_column", col=c.name))
        elif c.role == "numeric":
            d.num_features.append(c.name)
        else:  # categorical / high_card_categorical
            d.cat_features.append(c.name)
            if c.role == "high_card_categorical":
                d.steps.append(story("high_card_column", col=c.name, n_unique=c.n_unique))

    for dp in profile.date_parts:
        if all(c in d.num_features for c in (dp.year, dp.month, dp.day)):
            d.date_parts.append((dp.prefix, dp.year, dp.month, dp.day))
            d.num_features.append(f"{dp.prefix}_weekday")
            d.steps.append(story("date_parts", year=dp.year, month=dp.month, day=dp.day, invalid=dp.invalid, prefix=dp.prefix))

    n_features = len(d.num_features) + len(d.cat_features)
    if n_features == 0 or (target is None and n_features < 2):
        return _halt(d, "halt_no_features")

    gappy = [c.name for c in profile.columns if c.name != target and c.name not in d.drop_cols and c.missing_frac > 0]
    if gappy:
        d.steps.append(story("missing_values", n_cols=len(gappy), examples=", ".join(f"`{c}`" for c in gappy[:3])))

    # ── Branch A: no target -> clustering ─────────────────────────────────
    if target is None:
        d.task = "clustering"
        d.steps.append(story("task_clustering"))
        if rows > 10_000:
            d.model = "MiniBatchKMeans"
            reason, benefit = "Standard KMeans looks at every row on every pass, which is slow on big data.", "fast even on large data"
        else:
            d.model = "KMeans"
            reason, benefit = "KMeans is the classic, easy-to-understand clustering method and is fast at this size.", "simple and easy to interpret"
        d.steps.append(story("clustering_model", rows=rows, n_features=n_features, model=d.model, reason=reason, benefit=benefit))
        d.k_range = (2, max(2, min(8, rows // 10)))
        if rows > CLUSTER_SAMPLE:
            d.sample_rows = CLUSTER_SAMPLE
            d.steps.append(story("sampled", rows=rows, n=CLUSTER_SAMPLE))
        return d

    # ── Branch B: supervised ──────────────────────────────────────────────
    if rows < SMALL_SAMPLE_WARNING:
        n_test = max(int(rows * TEST_SIZE), 1)
        d.steps.append(story("small_data", rows=rows, n_test=n_test, pct=100 / n_test))
    y = df[target].dropna()
    n_missing_target = rows - len(y)
    if n_missing_target:
        d.steps.append(story("target_missing", n=n_missing_target, target=target))
    n_unique = int(y.nunique())
    is_num = is_numeric_dtype(y) and not pd.api.types.is_bool_dtype(y)

    if is_num and n_unique > MAX_CLASSES_AS_LABEL:
        # B1: regression
        d.task = "regression"
        d.primary_metric = "R²"
        d.steps.append(story("task_regression", target=target, n_unique=n_unique))
        skew = float(y.skew())
        if abs(skew) > 1 and y.min() >= 0:
            d.log_target = True
            d.steps.append(story("skewed_target", target=target, skew=skew))
        if rows < SMALL_DATA:
            r, a, b = _max_correlation(df, [c for c in d.num_features if c in df.columns])
            if n_features > 30:
                d.model = "LassoCV"
                d.steps.append(story("many_features", n_features=n_features, rows=rows))
            elif r > 0.9:
                d.model = "RidgeCV"
                d.steps.append(story("collinear", a=a, b=b, r=r))
            else:
                d.model = "LinearRegression"
                d.steps.append(story("model_small", rows=rows, model=d.model))
        elif rows <= LARGE_DATA:
            d.model = "RandomForestRegressor"
            d.steps.append(story("model_medium", rows=rows, model=d.model))
        else:
            d.model = "HistGradientBoostingRegressor"
            d.steps.append(story("model_large", rows=rows, model=d.model))
        linear = d.model if d.family == "linear" else "LinearRegression"
        d.candidates = [linear, "DecisionTreeRegressor", "RandomForestRegressor", "AdaBoostRegressor", "HistGradientBoostingRegressor"]
        d.candidates += [m for m, lib in (("XGBRegressor", "xgboost"), ("LGBMRegressor", "lightgbm")) if installed(lib)]
    else:
        # B2: classification
        d.task = "classification"
        counts = y.value_counts()
        rare = counts[counts < 2]
        if len(rare):
            d.rare_classes = rare.index.tolist()
            d.steps.append(story(
                "rare_classes", target=target, n_classes=len(rare), n_rows=int(rare.sum()),
                examples=", ".join(f"`{v}`" for v in d.rare_classes[:5]),
            ))
            counts = counts[counts >= 2]
        if len(counts) < 2:
            return _halt(d, "halt_target_type", target=target, role="single-class",
                         reason="After removing rare values only one class is left, so there is nothing to tell apart.")
        n_classes = len(counts)
        d.subtype = "binary" if n_classes == 2 else "multiclass"
        numeric_note = f" (numeric, but only {n_unique} values - so they act as labels)" if is_num else ""
        d.steps.append(story("task_classification", target=target, n_unique=n_classes, subtype=d.subtype, numeric_note=numeric_note))

        # Stratifying needs at least one test row per class.
        d.stratify = int(counts.sum() * TEST_SIZE) >= n_classes
        share = counts / counts.sum()
        ratio = share.min() / share.max()
        d.class_weight = bool(ratio < IMBALANCE_RATIO)
        # Accuracy hides failure on a minority class: with 30% "yes", a model that always says "no" is 70% accurate.
        d.primary_metric = "Macro F1" if share.min() < UNEVEN_SHARE * (2 / n_classes) or d.class_weight else "Accuracy"
        if d.primary_metric == "Macro F1" and not d.class_weight:
            d.steps.append(story("uneven", min_pct=share.min() * 100, max_pct=share.max() * 100))
        if d.class_weight:
            d.resample = installed("imblearn")
            extra = (" A later step also tests over- and under-sampling and keeps whichever works best." if d.resample else
                     " (Install `imbalanced-learn` to also test over- and under-sampling.)")
            d.steps.append(story("imbalanced", min_pct=share.min() * 100, max_pct=share.max() * 100, extra=extra))
        if n_classes == 2:
            d.positive_class = share.index.tolist()[-1]  # value_counts sorts descending -> rarer class last
            d.steps.append(story("positive_class", pos=d.positive_class, pct=share.min() * 100))

        if rows < SMALL_DATA:
            d.model = "LogisticRegression"
            d.steps.append(story("model_small", rows=rows, model=d.model))
        elif rows <= LARGE_DATA:
            d.model = "RandomForestClassifier"
            d.steps.append(story("model_medium", rows=rows, model=d.model))
        else:
            d.model = "HistGradientBoostingClassifier"
            d.steps.append(story("model_large", rows=rows, model=d.model))
        d.candidates = ["LogisticRegression", "DecisionTreeClassifier", "RandomForestClassifier",
                        "AdaBoostClassifier", "HistGradientBoostingClassifier"]
        d.candidates += [m for m, lib in (("XGBClassifier", "xgboost"), ("LGBMClassifier", "lightgbm")) if installed(lib)]

    d.steps.append(story("compare_models", rule_model=d.model, n=len(d.candidates), names=", ".join(d.candidates)))
    problems = {lib: library_problem(lib) for lib in ("xgboost", "lightgbm")}
    missing = [lib for lib, why in problems.items() if why == "not installed"]
    broken = {lib: why for lib, why in problems.items() if why and why != "not installed"}
    if missing:
        d.steps.append(story("boosting_libs_missing", libs=" and ".join(missing), pip=" ".join(missing)))
    if broken:
        d.steps.append(story("boosting_libs_broken", libs=" and ".join(broken), pip=" ".join(broken),
                             err="; ".join(f"{lib}: {why}" for lib, why in broken.items())))

    # ── Stage 3: speed ────────────────────────────────────────────────────
    limit = TRAIN_SAMPLE[d.family]
    if rows > limit:
        d.sample_rows = limit
        d.steps.append(story("sampled", rows=rows, n=limit))
    return d
