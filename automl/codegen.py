"""Turn a ``Decision`` into notebook-style code cells that explain themselves.

Every cell carries a five-part narrative so a beginner can follow along:

  🔍 What we found before   - the evidence that led to this step        (Step.found)
  💡 Why it matters          - why that evidence changes what we do      (Step.why)
  🎯 What we're going to do  - the action and what it achieves           (Step.action)
  🛠 What this code does     - the code explained in plain words         (Step.code)
  📌 What we found after     - written by the code itself via ``note()`` once it has run

The story lives right next to the code it describes, so the two stay in sync. The generated
code is plain, copy-pasteable Python: exactly what runs in the app and what the notebook export contains.
"""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass

from .decision import TEST_SIZE, Decision
from .narrative import Step

EDA_SAMPLE = 5_000
COMPARE_SAMPLE = 10_000
STATS_SAMPLE = 20_000
MAX_HIST_COLS = 12
MAX_CORR_COLS = 15
SCORING = {"R²": "r2", "Macro F1": "f1_macro", "Accuracy": "accuracy"}

# Search spaces for the tuning cell (RandomizedSearchCV), keyed by model name.
SEARCH_SPACES: dict[str, dict[str, list]] = {
    "LogisticRegression": {"C": [0.01, 0.1, 1.0, 10.0, 100.0]},
    "DecisionTreeClassifier": {"max_depth": [3, 5, 8, 12, None], "min_samples_leaf": [1, 5, 20, 50], "ccp_alpha": [0.0, 0.0001, 0.001]},
    "DecisionTreeRegressor": {"max_depth": [3, 5, 8, 12, None], "min_samples_leaf": [1, 5, 20, 50], "ccp_alpha": [0.0, 0.0001, 0.001]},
    "RandomForestClassifier": {"n_estimators": [100, 200, 400], "max_features": ["sqrt", 0.5, 1.0], "min_samples_leaf": [1, 2, 5]},
    "RandomForestRegressor": {"n_estimators": [100, 200, 400], "max_features": ["sqrt", 0.5, 1.0], "min_samples_leaf": [1, 2, 5]},
    "AdaBoostClassifier": {"n_estimators": [50, 100, 200], "learning_rate": [0.05, 0.1, 0.5, 1.0]},
    "AdaBoostRegressor": {"n_estimators": [50, 100, 200], "learning_rate": [0.05, 0.1, 0.5, 1.0]},
    "HistGradientBoostingClassifier": {"learning_rate": [0.03, 0.05, 0.1, 0.2], "max_leaf_nodes": [15, 31, 63], "l2_regularization": [0.0, 0.1, 1.0]},
    "HistGradientBoostingRegressor": {"learning_rate": [0.03, 0.05, 0.1, 0.2], "max_leaf_nodes": [15, 31, 63], "l2_regularization": [0.0, 0.1, 1.0]},
    # reg_alpha = α (L1) and reg_lambda = λ (L2) regularisation
    "XGBClassifier": {"n_estimators": [200, 400, 800], "learning_rate": [0.03, 0.1, 0.3], "max_depth": [3, 6, 9], "reg_alpha": [0.0, 0.1, 1.0], "reg_lambda": [1.0, 5.0, 10.0]},
    "XGBRegressor": {"n_estimators": [200, 400, 800], "learning_rate": [0.03, 0.1, 0.3], "max_depth": [3, 6, 9], "reg_alpha": [0.0, 0.1, 1.0], "reg_lambda": [1.0, 5.0, 10.0]},
    "LGBMClassifier": {"n_estimators": [200, 400, 800], "learning_rate": [0.03, 0.05, 0.1], "num_leaves": [15, 31, 63], "reg_alpha": [0.0, 0.1, 1.0], "reg_lambda": [0.0, 1.0, 5.0]},
    "LGBMRegressor": {"n_estimators": [200, 400, 800], "learning_rate": [0.03, 0.05, 0.1], "num_leaves": [15, 31, 63], "reg_alpha": [0.0, 0.1, 1.0], "reg_lambda": [0.0, 1.0, 5.0]},
}


@dataclass(frozen=True)
class Cell:
    id: str
    title: str
    story: Step
    code: str


def fill(template: str, **values) -> str:
    """Dedent a code template and replace <<name>> placeholders (avoids escaping every brace)."""
    text = textwrap.dedent(template).strip("\n")
    for key, value in values.items():
        text = text.replace(f"<<{key}>>", str(value))
    assert "<<" not in text, text
    return text


def _cell(cell_id: str, title: str, story: Step, *chunks: str) -> Cell:
    why = re.sub(r"[*`]", "", story.action)
    comment = textwrap.fill(why, width=88, initial_indent="# Why: ", subsequent_indent="#      ")
    body = "\n\n".join(c.strip("\n") for c in chunks if c and c.strip())
    return Cell(cell_id, title, story, f"{comment}\n{body}\n")


def _names(cols: list[str]) -> str:
    return ", ".join(f"`{c}`" for c in cols[:4]) + (f" and {len(cols) - 4} more" if len(cols) > 4 else "")


# ═════════════════════════════════════════════════════════════════════════
# Shared cells
# ═════════════════════════════════════════════════════════════════════════
def _setup_cell(d: Decision) -> Cell:
    story = Step(
        "setup",
        found="Your file is loaded into a table called `df`.",
        why="Every step below builds on the one before it, just like a Jupyter notebook, so we load the tools once, here.",
        action="We import the libraries and create two small helpers, so that every later cell can record its numbers and explain its results.",
        code="Imports pandas and numpy (tables and maths) and matplotlib and seaborn (charts). `metrics` is a dictionary where each "
             "cell stores its key numbers. `note()` writes a plain-English finding under a cell, which is the 📌 text you see after each step.",
    )
    return _cell("setup", "Setup", story, fill('''
        import numpy as np
        import pandas as pd
        import matplotlib.pyplot as plt
        import seaborn as sns

        metrics = {"task": <<task>>}  # every cell stores its key numbers here


        def note(text):
            """Write a plain-English finding under the cell."""
            try:
                from IPython.display import Markdown, display
                display(Markdown("📌 " + text))
            except ImportError:
                print("📌 " + text)
    ''', task=repr(d.task)))


def _prepare_cell(d: Decision) -> Cell:
    chunks = ["data = df.copy()  # work on a copy, so the original upload stays untouched"]
    if d.has_infinite:
        chunks.append("# Infinity is not a measurement (usually a division by zero): treat it as a missing value\n"
                      "data = data.replace([np.inf, -np.inf], np.nan)")
    if d.drop_cols:
        chunks.append(f"# Columns that can't help (IDs, constants, free text, mostly empty, or the answer itself)\n"
                      f"data = data.drop(columns={d.drop_cols!r})")
    for col in d.date_cols:
        chunks.append(fill('''
            # Split the date column <<col>> into numbers a model can use
            when = pd.to_datetime(data[<<col>>], errors="coerce", format="mixed")
            data[<<y>>] = when.dt.year
            data[<<m>>] = when.dt.month
            data[<<w>>] = when.dt.dayofweek
            data = data.drop(columns=<<col>>)
        ''', col=repr(col), y=repr(col + "_year"), m=repr(col + "_month"), w=repr(col + "_weekday")))
    for prefix, y, m, day in d.date_parts:
        chunks.append(fill('''
            # <<yr>> / <<mr>> / <<dr>> are one date split in three: combine them to get the weekday
            when = pd.to_datetime(pd.DataFrame({"year": data[<<yr>>], "month": data[<<mr>>], "day": data[<<dr>>]}), errors="coerce")
            data[<<w>>] = when.dt.dayofweek  # 0 = Monday; impossible dates stay blank and are filled later
        ''', yr=repr(y), mr=repr(m), dr=repr(day), w=repr(prefix + "_weekday")))
    if d.drop_duplicates:
        chunks.append("data = data.drop_duplicates()  # exact copies would be counted twice")
    if d.target:
        lines = [f"target = {d.target!r}", "data = data.dropna(subset=[target])  # rows without an answer can't teach anything"]
        if d.rare_classes:
            lines.append(f"data = data[~data[target].isin({d.rare_classes!r})]  # classes seen only once can't be split into train and test")
        chunks.append("\n".join(lines))
    if d.sample_rows:
        if d.task == "classification":
            chunks.append(fill('''
                # Keep a smaller sample with the same class mix, so training stays fast
                if len(data) > <<n>>:
                    from sklearn.model_selection import train_test_split
                    data, _ = train_test_split(data, train_size=<<n>>, stratify=data[target], random_state=42)
            ''', n=d.sample_rows))
        else:
            chunks.append(fill('''
                # Keep a random sample, so training stays fast
                if len(data) > <<n>>:
                    data = data.sample(n=<<n>>, random_state=42)
            ''', n=d.sample_rows))
    lines = [f"num_features = {d.num_features!r}", f"cat_features = {d.cat_features!r}"]
    if d.cat_features:
        lines.append("# Blanks in category columns become their own 'missing' category; categories are stored as text")
        lines.append('data[cat_features] = data[cat_features].fillna("missing").astype(str)')
    chunks.append("\n".join(lines))
    after = ['note(f"After cleaning: **{len(data):,} rows** and **{len(num_features) + len(cat_features)} input columns** "',
             '     f"({len(num_features)} numeric, {len(cat_features)} categorical).")']
    if d.task == "classification":
        after += ['shares = data[target].value_counts(normalize=True)',
                  'note("Target classes: " + ", ".join(f"`{k}` {v:.1%}" for k, v in shares.items()) + ".")']
    elif d.task == "regression":
        after += ['note(f"Target `{target}`: median {data[target].median():,.3g}, middle half between "',
                  '     f"{data[target].quantile(0.25):,.3g} and {data[target].quantile(0.75):,.3g}.")']
    chunks.append("\n".join(after))

    actions = []
    if d.drop_cols:
        actions.append(f"drop {_names(d.drop_cols)}")
    if d.date_cols or d.date_parts:
        actions.append("turn dates into year / month / weekday numbers")
    if d.drop_duplicates:
        actions.append("remove exact duplicate rows")
    if d.sample_rows:
        actions.append(f"keep a sample of {d.sample_rows:,} rows")
    found = ("The engine's decision trace (above) flagged columns to " + "; ".join(actions) + "."
             if actions else "The engine found every column usable as it is.")
    story = Step(
        "prepare", found,
        "Messy inputs (IDs, dates, blanks) either crash models or quietly make them worse.",
        "We apply those clean-up decisions, so that the model only sees useful, well-formed columns.",
        "Copies the table, drops or converts the flagged columns, removes rows with no target, and lists which inputs are "
        "numbers (`num_features`) and which are categories (`cat_features`). Then it reports what is left.",
    )
    return _cell("prepare", "Prepare the data", story, *chunks)


def _eda_cell(d: Decision) -> Cell:
    hist_cols, corr_cols = d.num_features[:MAX_HIST_COLS], d.num_features[:MAX_CORR_COLS]
    chunks = [f"sample = data.sample(min(len(data), {EDA_SAMPLE}), random_state=42)  # plotting a sample is much faster"]
    if hist_cols:
        rows = (len(hist_cols) + 3) // 4
        chunks.append(fill('''
            # 1) How is each numeric column distributed? Look for skew (long tails) and outliers.
            sample[<<cols>>].hist(bins=30, figsize=(12, <<h>>), layout=(<<rows>>, 4), color="#0071e3", edgecolor="white")
            plt.suptitle("Distribution of numeric columns")
            plt.tight_layout(); plt.show()
            skew = sample[<<cols>>].skew().abs().sort_values(ascending=False)
            note(f"Most lopsided column: `{skew.index[0]}` (skew {skew.iloc[0]:.1f}) - "
                 + ("a long tail of unusually large values." if skew.iloc[0] > 1 else "fairly symmetric, no long tail."))
        ''', cols=repr(hist_cols), h=f"{2.5 * rows:.1f}", rows=rows))
    if len(corr_cols) >= 2:
        chunks.append(fill('''
            # 2) Which numeric columns move together? (+1 = together, -1 = opposite, 0 = unrelated)
            corr = sample[<<cols>>].corr()
            plt.figure(figsize=(8, 6))
            sns.heatmap(corr, annot=<<annot>>, fmt=".2f", cmap="coolwarm", vmin=-1, vmax=1)
            plt.title("Correlation between numeric columns")
            plt.tight_layout(); plt.show()
            pairs = corr.where(~np.eye(len(corr), dtype=bool)).abs().stack()
            if len(pairs):
                a, b = pairs.idxmax()
                note(f"Strongest link between two inputs: `{a}` and `{b}` (r = {corr.loc[a, b]:.2f}).")
        ''', cols=repr(corr_cols), annot=len(corr_cols) <= 10))
    if d.task == "classification":
        chunks.append(fill('''
            # 3) How many rows are in each class?
            sample[target].value_counts().head(20).plot.bar(color="#0071e3", figsize=(8, 4))
            plt.title(f"Rows per class of {target!r}"); plt.ylabel("rows")
            plt.tight_layout(); plt.show()
        '''))
    elif d.task == "regression":
        chunks.append(fill('''
            # 3) What does the target look like?
            sample[target].plot.hist(bins=40, color="#0071e3", edgecolor="white", figsize=(8, 4))
            plt.title(f"Distribution of {target!r}"); plt.xlabel(target)
            plt.tight_layout(); plt.show()
            note(f"Target skew = {data[target].skew():.2f} "
                 + ("- a long right tail, so a few very large values." if data[target].skew() > 1 else "- no strong tail."))
        '''))
    elif d.cat_features:
        chunks.append(fill('''
            # 3) Most common values of a category column
            sample[<<col>>].value_counts().head(15).plot.bar(color="#0071e3", figsize=(8, 4))
            plt.title("Most common values of " + <<col>>)
            plt.tight_layout(); plt.show()
        ''', col=repr(d.cat_features[0])))
    story = Step(
        "eda",
        "We know each column's type, but not what its values look like.",
        "Plots reveal skew, outliers and relationships that summary numbers hide, and they let you sanity-check the data yourself.",
        "We draw distributions and correlations, so that surprises are caught before any modelling.",
        "Takes a random sample of up to 5,000 rows to keep it fast. It draws a histogram per numeric column, a correlation heatmap "
        "(red = move together, blue = opposite) and the target's distribution, then notes the most lopsided column and the strongest link.",
    )
    return _cell("eda", "Explore the data", story, *chunks)


def _preprocess_code(d: Decision) -> str:
    return fill('''
        from sklearn.compose import ColumnTransformer
        from sklearn.impute import SimpleImputer
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import OneHotEncoder, StandardScaler

        numeric_steps = Pipeline([
            ("fill_gaps", SimpleImputer(strategy="median")),  # a gap becomes the column's median
            ("scale", StandardScaler()),                       # every number gets mean 0 and spread 1
        ])
        # One 0/1 column per category; rare categories are grouped into one 'infrequent' column
        category_steps = OneHotEncoder(handle_unknown="infrequent_if_exist", max_categories=20, sparse_output=False)

        preprocessor = ColumnTransformer([
            ("numbers", numeric_steps, num_features),
            ("categories", category_steps, cat_features),
        ])
    ''')


# ═════════════════════════════════════════════════════════════════════════
# Supervised cells
# ═════════════════════════════════════════════════════════════════════════
def _insights_cell(d: Decision) -> Cell:
    if d.task == "regression":
        outcome_code, label, fmt = "data[target]", f"average {d.target}", "{:,.3g}"
    else:
        outcome_code, label, fmt = f"data[target] == {d.positive_class!r}", f"rate of {d.positive_class}", "{:.1%}"
    code = fill('''
        outcome = <<outcome>>
        label = <<label_r>>  # e.g. "rate of Canceled"
        overall = outcome.mean()
        print(f"Overall {label}: " + <<fmt>>.format(overall))


        def group_outcome(col):
            """The outcome for each group of a column (numbers are cut into 5 equal-sized bands)."""
            values = data[col]
            if col in num_features and values.nunique() > 10:
                lowest = values.min()
                values = pd.qcut(values, q=5, duplicates="drop")
                values = values.cat.rename_categories(lambda band: f"{max(band.left, lowest):.4g} to {band.right:.4g}")
            table = outcome.groupby(values, observed=True).agg(["mean", "size"])
            return table[table["size"] >= 30]  # ignore tiny groups - they are too noisy


        groups = {col: group_outcome(col) for col in num_features + cat_features}
        groups = {col: t for col, t in groups.items() if len(t) >= 2}
        spread = pd.Series({col: t["mean"].max() - t["mean"].min() for col, t in groups.items()}).sort_values(ascending=False)
        top = spread.index[:4].tolist()
        metrics["overall_outcome"] = float(overall)
        metrics["drivers"] = {
            col: {"low": str(groups[col]["mean"].idxmin()), "low_value": float(groups[col]["mean"].min()),
                  "high": str(groups[col]["mean"].idxmax()), "high_value": float(groups[col]["mean"].max())}
            for col in top
        }

        if not top:
            note("No group of any column has at least 30 rows, so group-by-group rates would be noise. Skipped; the models "
                 "below still use every column.")
        print(f"Columns where the {label} differs most between groups:")
        for col in top:
            print(f"\\n{col}")
            print(groups[col]["mean"].rename_axis(None).map(<<fmt>>.format).to_string())

        if top:
            fig, axes = plt.subplots(1, len(top), figsize=(4 * len(top), 4), squeeze=False)
            for ax, col in zip(axes[0], top):
                groups[col]["mean"].plot.bar(ax=ax, color="#0071e3")
                ax.axhline(overall, color="grey", linestyle="--", linewidth=1)  # overall level for comparison
                ax.set_title(col); ax.set_xlabel(""); ax.tick_params(axis="x", labelrotation=45)
            axes[0][0].set_ylabel(label)
            plt.tight_layout(); plt.show()

        for col in top[:3]:
            info = metrics["drivers"][col]
            note(f"`{col}`: the {label} ranges from **" + <<fmt>>.format(info["low_value"]) + f"** (`{info['low']}`) to **"
                 + <<fmt>>.format(info["high_value"]) + f"** (`{info['high']}`), against " + <<fmt>>.format(overall) + " overall.")
    ''', outcome=outcome_code, fmt=repr(fmt), label_r=repr(label))
    story = Step(
        "insights",
        f"We know the target, but not yet which columns move the {label}.",
        "Tables like 'cancellation rate by market segment' are insights a business can act on directly. They also show what a model should be able to learn.",
        f"We compare the {label} across the groups of every column and chart the biggest differences, so that the key drivers are visible before any modelling.",
        "For each column, groups the rows (each category, or 5 equal-sized bands for numbers), computes the outcome per group and "
        "ignores groups smaller than 30 rows. It then ranks columns by the gap between their best and worst group, and charts the top 4.",
    )
    return _cell("insights", "Find the key drivers", story, code)


def _split_cell(d: Decision) -> Cell:
    strat = ", stratify=y" if d.stratify else ""
    code = fill('''
        from sklearn.model_selection import train_test_split

        X = data[num_features + cat_features]
        y = data[target]
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=<<test>>, random_state=42<<strat>>)
        print(f"Training rows: {len(X_train):,}   Test rows: {len(X_test):,}")
        note(f"**{len(X_train):,}** rows to learn from, **{len(X_test):,}** rows locked away for the final exam. "
             "Every choice from now on (model, settings, threshold) is made on training rows only.")
    ''', test=TEST_SIZE, strat=strat)
    story = Step(
        "split",
        "We need a fair way to measure how good the model is" + (", and the target is a set of classes." if d.stratify else "."),
        "Testing a model on rows it learned from is like grading a student on questions they've already seen."
        + (" A random split could also leave a rare class out of the test set." if d.stratify else ""),
        "We hide 20% of rows as a **test set**" + (", keeping the same class mix in both parts (stratified split)" if d.stratify else "")
        + ", so that the final score reflects performance on new, unseen data.",
        "`X` holds the inputs and `y` the answer. `train_test_split` shuffles the rows and puts 80% in training and 20% in test"
        + (" (`stratify=y` keeps the class proportions equal)" if d.stratify else "") + ". `random_state=42` makes the split repeatable.",
    )
    return _cell("split", "Split into training and test sets", story, code)


def _preprocess_cell(d: Decision) -> Cell:
    code = _preprocess_code(d) + "\n\n" + fill('''
        preprocessor.fit(X_train)  # learn medians, scales and categories from TRAINING rows only
        n_out = len(preprocessor.get_feature_names_out())
        note(f"The recipe turns **{len(num_features) + len(cat_features)}** input columns into **{n_out}** model columns "
             "(each category becomes its own 0/1 column).")
    ''')
    story = Step(
        "preprocess",
        "The inputs mix numbers and categories, and some cells may be empty.",
        "Models only understand complete tables of numbers. If test data influenced the cleaning (e.g. its medians), the test score would be "
        "optimistic: this is called data leakage.",
        "We build one preprocessing recipe and put it *inside* every model pipeline, so that exactly the same steps run on training and "
        "test data and nothing leaks.",
        "`SimpleImputer` fills gaps (median for numbers), `StandardScaler` puts numbers on one scale (needed by linear and distance-based "
        "models, harmless for trees) and `OneHotEncoder` turns each category into a 0/1 column. `ColumnTransformer` applies each part "
        "to the right columns.",
    )
    return _cell("preprocess", "Build the preprocessing recipe", story, code)


def _baseline_cell(d: Decision) -> Cell:
    if d.task == "regression":
        guess, code = "the average value", fill('''
            from sklearn.dummy import DummyRegressor
            from sklearn.metrics import r2_score


            def score(y_true, y_pred):
                return r2_score(y_true, y_pred)


            metrics["metric_name"] = "R²"
            baseline = DummyRegressor(strategy="mean").fit(X_train, y_train)
        ''')
    else:
        fn, imp = (('f1_score(y_true, y_pred, average="macro")', "from sklearn.metrics import f1_score")
                   if d.primary_metric == "Macro F1" else ("accuracy_score(y_true, y_pred)", "from sklearn.metrics import accuracy_score"))
        guess, code = "the most common class", fill('''
            from sklearn.dummy import DummyClassifier
            <<imp>>


            def score(y_true, y_pred):
                return <<fn>>


            metrics["metric_name"] = <<metric>>
            baseline = DummyClassifier(strategy="most_frequent").fit(X_train, y_train)
        ''', imp=imp, fn=fn, metric=repr(d.primary_metric))
    code += "\n" + fill('''
        metrics["baseline_score"] = score(y_test, baseline.predict(X_test))
        print(f"Baseline {metrics['metric_name']}: {metrics['baseline_score']:.3f}")
        note(f"Always guessing <<guess>> scores **{metrics['baseline_score']:.3f}** ({metrics['metric_name']}). "
             "This is the bar every real model has to clear.")
    ''', guess=guess)
    story = Step(
        "baseline",
        "A score like 0.80 means nothing on its own.",
        "We need a reference point: what would a 'dumb' strategy that ignores every input score?",
        f"We score a baseline that always guesses {guess}, so that we can tell whether a real model actually learned something.",
        f"Defines `score()`, the yardstick used everywhere below ({d.primary_metric}). A `Dummy` model then always predicts {guess} "
        "and is scored on the test set.",
    )
    return _cell("baseline", "Set a baseline to beat", story, code)


_DESIGN = '''
    import statsmodels.api as sm

    stats_rows = X_train.sample(min(len(X_train), <<n>>), random_state=42).index  # statsmodels is slower: use a sample
    reference = {col: X_train[col].mode()[0] for col in cat_features}  # each category is compared with the most common one


    def design(X, columns=None):
        """statsmodels needs plain numbers: gaps filled, each category a 0/1 column (except the reference)."""
        table = X[num_features].fillna(X_train[num_features].median())
        for col in cat_features:
            common = X_train[col].value_counts().index[:10]  # 10 most common categories, the rest become "other"
            values = X[col].where(X[col].isin(common), "other")
            dummies = pd.get_dummies(values, prefix=col, prefix_sep="=", dtype=float)
            table = table.join(dummies.drop(columns=f"{col}={reference[col]}", errors="ignore"))
        table = table.astype(float)
        if columns is None:
            return table.loc[:, table.std() > 0]  # drop columns that never change
        return table.reindex(columns=columns, fill_value=0.0)
'''


def _logit_cell(d: Decision) -> Cell:
    code = fill(_DESIGN, n=STATS_SAMPLE) + "\n\n" + fill('''
        positive = <<pos>>
        X_sm = design(X_train.loc[stats_rows])
        y_sm = (y_train.loc[stats_rows] == positive).astype(float)  # 1 = the class we want to catch
        try:
            logit = sm.Logit(y_sm, sm.add_constant(X_sm)).fit(disp=False, maxiter=300)
        except Exception as error:  # e.g. two columns that carry exactly the same information
            logit = None
            note(f"statsmodels could not fit the logistic regression ({type(error).__name__}); see the model comparison below instead.")

        if logit is not None:
            ci = np.exp(logit.conf_int())
            odds = pd.DataFrame({"coefficient": logit.params, "odds ratio": np.exp(logit.params),
                                 "95% CI low": ci[0], "95% CI high": ci[1], "p-value": logit.pvalues}).drop(index="const")
            odds = odds.reindex(odds["coefficient"].abs().sort_values(ascending=False).index)
            print(odds.round(3).head(20).to_string())
            significant = odds[odds["p-value"] < 0.05]
            if len(significant):
                change = ((significant["odds ratio"].head(12) - 1) * 100).sort_values()
                change.plot.barh(color=["#d1495b" if v > 0 else "#0071e3" for v in change], figsize=(8, 5))
                plt.axvline(0, color="black", linewidth=1); plt.xlabel(f"% change in the odds of {positive!r}")
                plt.title("What raises (red) or lowers (blue) the odds"); plt.tight_layout(); plt.show()
            elif odds["p-value"].isna().any():
                note("statsmodels could not compute p-values: the inputs separate the classes (almost) perfectly, so the "
                     "uncertainty can't be estimated. Use the odds-ratio directions only as a rough guide.")
            else:
                note("No single input is statistically significant on its own. The signal may come from combinations of columns, "
                     "which the tree models below can capture.")

            metrics["logit_pseudo_r2"] = float(logit.prsquared)
            converged = logit.mle_retvals["converged"]
            note(f"Fitted on {len(X_sm):,} training rows: pseudo R² = **{logit.prsquared:.2f}**, and **{len(significant)} of {len(odds)}** "
                 "inputs are statistically significant (p < 0.05)."
                 + ("" if converged else " ⚠️ The fit did not fully converge. This usually means a category that is (almost) always one "
                    "class ('perfect separation'), so treat its huge odds ratio as unreliable."))
            for name, row in significant.head(4).iterrows():
                pct = (row["odds ratio"] - 1) * 100
                if "=" in name:
                    col, value = name.split("=", 1)
                    note(f"`{col}` = **{value}** (vs {reference[col]}): odds of {positive!r} **{pct:+.0f}%**.")
                elif set(X_sm[name].unique()) <= {0.0, 1.0}:
                    note(f"`{name}` = 1 (vs 0): odds of {positive!r} **{pct:+.0f}%** (holding everything else fixed).")
                else:
                    note(f"Each extra unit of `{name}`: odds of {positive!r} **{pct:+.0f}%** (holding everything else fixed).")
    ''', pos=repr(d.positive_class))
    story = Step(
        "logit",
        f"The target has two classes, and we want to know *how much* each input changes the chance of `{d.positive_class}`.",
        "Logistic regression turns a straight-line score into a probability between 0 and 1 (the sigmoid). Its coefficients become "
        "**odds ratios**, the most widely used way to explain a binary outcome, and statsmodels adds p-values to show which effects are real.",
        "We fit a logistic regression with statsmodels and read its odds ratios and p-values, so that each driver gets a size, a direction "
        "and a confidence level.",
        "`design()` builds a plain numeric table: gaps are filled and each category becomes a 0/1 column, compared with its most common "
        "value. `sm.Logit(...).fit()` estimates the log-odds coefficients. `np.exp` turns them into odds ratios: 1.5 means 50% higher "
        "odds, 0.5 means half. p < 0.05 means the effect is unlikely to be chance.",
    )
    return _cell("logit", "Explain with logistic regression (odds ratios)", story, code)


def _vif_cell(d: Decision) -> Cell:
    code = fill(_DESIGN, n=STATS_SAMPLE) + "\n\n" + fill('''
        from statsmodels.stats.outliers_influence import variance_inflation_factor

        X_sm = design(X_train.loc[stats_rows])


        def vif_table(cols):
            sample = sm.add_constant(X_sm[cols].sample(min(len(X_sm), 5000), random_state=42))
            return pd.Series([variance_inflation_factor(sample.values, i) for i in range(1, sample.shape[1])], index=cols)


        ols_cols, removed = list(X_sm.columns), []
        vif = vif_table(ols_cols)
        while len(ols_cols) > 1 and vif.max() > 10:  # VIF above 10 = the column is mostly a copy of others
            worst = vif.idxmax()
            removed.append(f"`{worst}` (VIF {vif.max():,.0f})" if vif.max() < 1e6 else f"`{worst}` (VIF ∞: an exact copy of other columns)")
            ols_cols.remove(worst)
            vif = vif_table(ols_cols)
        print(vif.sort_values(ascending=False).round(2).head(15).to_string())
        metrics["vif_max"] = float(vif.max())
        if removed:
            note("Removed for multicollinearity, one at a time: " + ", ".join(removed) + ".")
        note(f"Largest remaining VIF: **{vif.max():.1f}** (`{vif.idxmax()}`). "
             + ("Below 5 means the coefficients below can be trusted." if vif.max() < 5 else "Between 5 and 10 is acceptable, but read that coefficient with care."))
    ''')
    story = Step(
        "vif",
        "Before we read regression coefficients, we must check that no input is just a copy of other inputs.",
        "When columns overlap (e.g. a total and its parts), the regression can't tell which one deserves the credit. Coefficients then swing "
        "wildly and p-values become meaningless. This is **multicollinearity**.",
        "We compute the **Variance Inflation Factor** of every input and drop the worst one while any VIF is above 10, so that the next "
        "cell's coefficients are stable.",
        "`design()` builds a plain numeric table, comparing each category with its most common value. For each column, VIF = 1 / (1 − R²) "
        "of predicting that column from all the others: 1 means independent, above 10 means mostly redundant. The loop drops the worst "
        "offender and recomputes.",
    )
    return _cell("vif", "Check multicollinearity (VIF)", story, code)


def _ols_cell(d: Decision) -> Cell:
    log = d.log_target
    pct = "f\"{(np.exp(row['{col}']) - 1) * 100:+.1f}%\""
    raw = "f\"{row['{col}']:+,.3g}\""
    fmt = pct if log else raw
    code = fill('''
        from sklearn.metrics import r2_score

        y_sm = <<y_train>>
        ols = sm.OLS(y_sm, sm.add_constant(X_sm[ols_cols])).fit()
        print(ols.summary())

        ci = ols.conf_int()
        coef = pd.DataFrame({"coefficient": ols.params, "95% CI low": ci[0], "95% CI high": ci[1],
                             "t": ols.tvalues, "p-value": ols.pvalues}).drop(index="const")
        coef = coef.reindex(coef["t"].abs().sort_values(ascending=False).index)
        significant = coef[coef["p-value"] < 0.05]
        test_r2 = r2_score(<<y_test>>, ols.predict(sm.add_constant(design(X_test, ols_cols), has_constant="add")))
        metrics.update(ols_r2=float(ols.rsquared), ols_adj_r2=float(ols.rsquared_adj), ols_f_pvalue=float(ols.f_pvalue),
                       ols_test_r2=float(test_r2), ols_significant=len(significant), ols_inputs=len(coef))

        note(f"R² = **{ols.rsquared:.2f}** (adjusted **{ols.rsquared_adj:.2f}**): the straight-line model explains {ols.rsquared:.0%} of the "
             f"variation in <<what>> on training rows, and {test_r2:.0%} on test rows.")
        note(f"F-test p-value = {ols.f_pvalue:.2g}: "
             + ("the inputs *together* clearly explain the target." if ols.f_pvalue < 0.05 else "no evidence the inputs explain the target at all."))
        not_significant = [c for c in coef.index if c not in significant.index]
        note(f"**{len(significant)} of {len(coef)}** inputs are significant (p < 0.05)."
             + (" Not significant: " + ", ".join(f"`{c}`" for c in not_significant[:6]) + "." if not_significant else ""))
        for name, row in significant.head(4).iterrows():
            effect, low, high = <<effect>>, <<low>>, <<high>>
            if "=" in name:
                col, value = name.split("=", 1)
                note(f"`{col}` = **{value}** (vs {reference[col]}): <<unit>> **{effect}** (95% CI {low} to {high}).")
            elif set(X_sm[name].unique()) <= {0.0, 1.0}:
                note(f"`{name}` = 1 (vs 0): <<unit>> **{effect}** (95% CI {low} to {high}), holding everything else fixed.")
            else:
                note(f"Each extra unit of `{name}`: <<unit>> **{effect}** (95% CI {low} to {high}), holding everything else fixed.")
    ''',
        y_train="np.log1p(y_train.loc[stats_rows])  # the target is skewed, so we model log(target)" if log else "y_train.loc[stats_rows]",
        y_test="np.log1p(y_test)" if log else "y_test",
        what="log({target})" if log else "`{target}`",
        effect=fmt.replace("{col}", "coefficient"),
        low=fmt.replace("{col}", "95% CI low"),
        high=fmt.replace("{col}", "95% CI high"),
        unit="`{target}` changes by about" if log else "`{target}` changes by",
    )
    story = Step(
        "ols",
        "The inputs are no longer redundant (previous cell), so the coefficients can be read safely.",
        "Ordinary Least Squares (OLS) fits y = β₀ + β₁x₁ + β₂x₂ + … and statsmodels tests every β: is it really different from zero? "
        "This tells you which drivers are real and how big they are, not just how well the model predicts.",
        "We fit OLS and read R² and adjusted R², the F-test, each coefficient with its 95% confidence interval, and p-values, so that we "
        "know which predictors truly drive the target." + (" Because the target is skewed, we model log(target), so effects read as % changes." if log else ""),
        "`sm.OLS(y, X).fit()` estimates the intercept β₀ (`const`) and slopes. `summary()` prints standard errors, t-statistics and p-values. "
        "R² is the share of variation explained, and adjusted R² penalises useless inputs. The F-test asks whether all inputs together beat "
        "no inputs. A coefficient with p < 0.05 is statistically significant.",
    )
    return _cell("ols", "Statistical inference with OLS", story, code)


def _assumptions_cell(d: Decision) -> Cell:
    code = fill('''
        from scipy import stats
        from statsmodels.stats.diagnostic import het_breuschpagan, linear_reset
        from statsmodels.stats.stattools import durbin_watson

        resid, fitted = ols.resid, ols.fittedvalues
        show = resid.sample(min(len(resid), 3000), random_state=42).index
        fig, axes = plt.subplots(1, 3, figsize=(16, 4.5))
        axes[0].scatter(fitted[show], resid[show], alpha=0.3, s=8, color="#0071e3")
        axes[0].axhline(0, color="black", linewidth=1)
        axes[0].set_xlabel("predicted"); axes[0].set_ylabel("residual (actual - predicted)"); axes[0].set_title("Residuals vs predicted")
        sns.histplot(resid, kde=True, ax=axes[1], color="#0071e3"); axes[1].set_title("Residual distribution")
        sm.qqplot(resid[show], line="s", ax=axes[2]); axes[2].set_title("Q-Q plot (points on the line = normal)")
        plt.tight_layout(); plt.show()

        checks = {
            "Linearity (RESET test)": linear_reset(ols, power=2, use_f=True).pvalue,
            "Equal spread / homoscedasticity (Breusch-Pagan)": het_breuschpagan(resid, ols.model.exog)[1],
            "Normal residuals (Jarque-Bera)": stats.jarque_bera(resid).pvalue,
        }
        dw = durbin_watson(resid)
        print(pd.Series(checks, name="p-value").round(4).to_string())
        print(f"Durbin-Watson: {dw:.2f}")
        metrics["assumptions"] = {name: bool(p >= 0.05) for name, p in checks.items()}
        metrics["durbin_watson"] = float(dw)

        advice = {
            "Linearity (RESET test)": "some relationships are curved: tree models or squared terms may fit better",
            "Equal spread / homoscedasticity (Breusch-Pagan)": "errors grow or shrink with the prediction, so trust p-values less (robust standard errors, or log the target)",
            "Normal residuals (Jarque-Bera)": "residuals are not bell-shaped, so confidence intervals are approximate (predictions are still fine)",
        }
        for name, p in checks.items():
            note(f"{name}: p = {p:.3g} → " + ("✅ looks fine." if p >= 0.05 else f"⚠️ violated: {advice[name]}."))
        note(f"Independence (Durbin-Watson = {dw:.2f}): "
             + ("✅ close to 2, so neighbouring rows' errors are unrelated." if 1.5 <= dw <= 2.5 else "⚠️ far from 2, so rows may depend on each other (e.g. time order)."))
        if len(resid) > 5000:
            note("With thousands of rows these tests flag even tiny deviations. Judge by the plots too: a shapeless cloud around 0 and points "
                 "hugging the Q-Q line are what matter.")
    ''')
    story = Step(
        "assumptions",
        "OLS has given us coefficients and p-values.",
        "Those p-values and intervals are only valid if the model's assumptions hold: a **linear** relationship, **equal spread** of errors "
        "(homoscedasticity), **normal** residuals and **independent** observations.",
        "We check each assumption with a plot and a formal test, so that you know how far to trust the statistics above, and what to do if one fails.",
        "Residuals are actual − predicted. The left plot should be a shapeless cloud; a curve suggests non-linearity, a funnel suggests "
        "unequal spread. The middle plot should look bell-shaped, and the Q-Q plot should follow its line. RESET, Breusch-Pagan and "
        "Jarque-Bera test each assumption (p < 0.05 means violated). Durbin-Watson near 2 means the errors are independent.",
    )
    return _cell("assumptions", "Check the regression assumptions", story, code)


def _compare_cell(d: Decision) -> Cell:
    optional = [n for n in d.candidates if not d.model_import(n).startswith("from sklearn")]  # XGBoost, LightGBM
    core = [n for n in d.candidates if n not in optional]
    imports = "\n".join(sorted({d.model_import(n) for n in core}))
    entries = "\n".join(f"    {n!r}: {d.model_constructor(n)}," for n in core)
    # XGBoost / LightGBM are compiled libraries that can be installed yet fail to load (macOS without libomp). The engine
    # already skips them then, but a notebook may run on another computer, so each one is added inside its own try.
    extras = "".join("\n" + fill('''

        try:  # optional boosting library
            <<imp>>
            candidates[<<name>>] = <<ctor>>
        except Exception as exc:  # e.g. OSError "Library not loaded: libomp.dylib" on macOS
            note(f"Skipped <<label>>: its library could not be loaded ({type(exc).__name__}). "
                 "On macOS, `brew install libomp` usually fixes this.")
    ''', imp=d.model_import(n), name=repr(n), label=n, ctor=d.model_constructor(n)) + "\n" for n in optional)
    wrap = fill('''
        # Every candidate learns log(target) and converts its predictions back
        from sklearn.compose import TransformedTargetRegressor
        candidates = {name: TransformedTargetRegressor(regressor=m, func=np.log1p, inverse_func=np.expm1)
                      for name, m in candidates.items()}
    ''') if d.log_target else ""
    label_wrapper = fill('''
        from sklearn.base import BaseEstimator, ClassifierMixin, clone


        class LabelEncoded(ClassifierMixin, BaseEstimator):
            """XGBoost only accepts classes numbered 0, 1, 2 ...: translate text labels in, and back out."""

            def __init__(self, model):
                self.model = model

            def fit(self, X, y):
                self.classes_, codes = np.unique(y, return_inverse=True)
                self.model_ = clone(self.model).fit(X, codes)
                return self

            def predict(self, X):
                return self.classes_[self.model_.predict(X)]

            def predict_proba(self, X):
                return self.model_.predict_proba(X)
    ''') + "\n\n\n" if "XGBClassifier" in d.candidates else ""
    head = "from sklearn.model_selection import cross_val_score\n" + imports + "\n\n" + label_wrapper
    code = head + "candidates = {\n" + entries + "\n}\n" + extras + "\n" + wrap + "\n" + fill('''


        def make_pipe(model):
            """Preprocessing + model as one object, so every cross-validation fold repeats exactly the same steps."""
            return Pipeline([("prep", preprocessor), ("model", model)])


        check = X_train.sample(min(len(X_train), <<n>>), random_state=42)  # a sample keeps this quick
        cv_scores = {}
        for name, candidate in candidates.items():
            cv_scores[name] = float(cross_val_score(make_pipe(candidate), check, y_train.loc[check.index], cv=3, scoring=<<scoring>>).mean())
            print(f"{name:32s} {metrics['metric_name']} = {cv_scores[name]:.3f}")

        best_name = max(cv_scores, key=cv_scores.get)
        metrics["cv_scores"] = cv_scores
        metrics["best_model"] = best_name

        pd.Series(cv_scores).sort_values().plot.barh(color="#0071e3", figsize=(7, 3.5))
        plt.axvline(metrics["baseline_score"], color="grey", linestyle="--", label="baseline")
        plt.xlabel(f"cross-validated {metrics['metric_name']} (higher = better)"); plt.legend()
        plt.title("Which kind of model fits this data best?")
        plt.tight_layout(); plt.show()

        ranked = sorted(cv_scores.items(), key=lambda kv: kv[1], reverse=True)
        note(f"Winner: **{best_name}** with {ranked[0][1]:.3f}, ahead of {ranked[1][0]} ({ranked[1][1]:.3f}). "
             f"Baseline: {metrics['baseline_score']:.3f}.")
        simple = <<simple>>
        if best_name != simple and cv_scores[best_name] - cv_scores[simple] < 0.01:
            note(f"The simple {simple} is within 0.01 of the winner. If explaining the model matters more than the last bit of accuracy, prefer it.")
    ''', n=COMPARE_SAMPLE, scoring=repr(SCORING[d.primary_metric]), simple=repr(d.candidates[0]))
    story = Step(
        "compare",
        f"Rules of thumb suggested **{d.model}**, but rules can be wrong for a particular dataset.",
        "Models learn in different ways. **Linear** models draw a straight line or plane. A **decision tree** asks if/else questions. "
        "**Bagging** (random forest) averages many trees grown on random bootstrap samples to cut variance. **Boosting** (AdaBoost, "
        "gradient boosting, and XGBoost / LightGBM when installed) adds trees one by one, each fixing the previous errors. No single "
        "kind wins every time.",
        "We score every candidate with 3-fold cross-validation on training rows only, so that the data, not a guess, picks the winner "
        "and the test set stays untouched.",
        "`candidates` lists one model of each kind"
        + (" (`LabelEncoded` lets XGBoost read text labels such as 'yes'/'no')" if "XGBClassifier" in d.candidates else "")
        + ". `make_pipe` joins preprocessing and model, and `cross_val_score` grades each candidate: "
        "the training sample is cut into 3 parts, and each part is predicted by a model trained on the other two. The average of the "
        "3 scores is the model's grade.",
    )
    return _cell("compare", "Compare candidate models", story, code)


def _resample_cell(d: Decision) -> Cell:
    code = fill('''
        from imblearn.over_sampling import SMOTE, RandomOverSampler
        from imblearn.pipeline import Pipeline as ImbPipeline
        from imblearn.under_sampling import RandomUnderSampler
        from sklearn.base import clone


        def without_class_weight(model):
            """Resampling already balances the classes; class weights on top would correct twice."""
            model = clone(model)
            if "class_weight" in model.get_params():
                model.set_params(class_weight=None)
            return model


        winner = candidates[best_name]
        samplers = {
            "random oversampling": RandomOverSampler(random_state=42),   # copy rare-class rows
            "random undersampling": RandomUnderSampler(random_state=42), # drop common-class rows
            "SMOTE": SMOTE(random_state=42),                             # invent new rare rows between real neighbours
        }
        options = {"class weights (current)": make_pipe(winner)}
        for label, sampler in samplers.items():
            # imblearn's Pipeline resamples ONLY while fitting, i.e. only inside the training folds, never the rows being scored
            options[label] = ImbPipeline([("prep", preprocessor), ("resample", sampler), ("model", without_class_weight(winner))])

        balance_scores = {}
        for label, option in options.items():
            try:
                balance_scores[label] = float(cross_val_score(option, check, y_train.loc[check.index], cv=3, scoring=<<scoring>>).mean())
            except ValueError as error:  # e.g. SMOTE needs at least 6 rare-class rows per fold
                note(f"{label} could not run here ({error}).")
            if label in balance_scores:
                print(f"{label:26s} {metrics['metric_name']} = {balance_scores[label]:.3f}")

        best_balance = max(balance_scores, key=balance_scores.get)
        metrics["balance_scores"], metrics["balance_method"] = balance_scores, best_balance
        pd.Series(balance_scores).sort_values().plot.barh(color="#0071e3", figsize=(7, 3))
        plt.xlabel(f"cross-validated {metrics['metric_name']}"); plt.title(f"Ways to handle the rare class ({best_name})")
        plt.tight_layout(); plt.show()

        if best_balance != "class weights (current)":
            chosen_sampler = samplers[best_balance]
            candidates[best_name] = without_class_weight(winner)

            def make_pipe(model):  # from now on every pipeline resamples its training rows first
                return ImbPipeline([("prep", preprocessor), ("resample", chosen_sampler), ("model", model)])

        gain = balance_scores[best_balance] - balance_scores["class weights (current)"]
        note(f"Best way to handle the imbalance: **{best_balance}** ({balance_scores[best_balance]:.3f}"
             + (f", {gain:+.3f} vs class weights)." if gain else ")."))
        note("Resampling only ever touches training rows. The test set keeps its real class mix, so the final score stays honest.")
    ''', scoring=repr(SCORING[d.primary_metric]))
    story = Step(
        "resample",
        "The classes are very uneven, and so far we handle that with class weights: mistakes on the rare class count more.",
        "There are other well-known fixes. **Oversampling** copies rare rows, **undersampling** drops common rows, and **SMOTE** "
        "invents new rare rows between real neighbours. Which works best depends on the data.",
        "We compare the four approaches on the winning model with cross-validation, and keep the best one, so that the rare class gets "
        "the attention it deserves without cheating.",
        "`imblearn`'s `Pipeline` adds a `resample` step that only runs while fitting. Inside cross-validation it therefore resamples "
        "only the training folds, never the rows being scored. `without_class_weight` avoids correcting twice. If a sampler wins, "
        "`make_pipe` is redefined so every later step (tuning, training, threshold) uses it.",
    )
    return _cell("resample", "Handle class imbalance (resampling)", story, code)


def _tune_cell(d: Decision) -> Cell:
    def prefix(name: str) -> str:  # where a model's settings live inside the pipeline (wrappers add a level)
        return "model__" + ("regressor__" if d.log_target else "") + ("model__" if name == "XGBClassifier" else "")
    spaces = "\n".join(f"    {n!r}: {{{', '.join(f'{prefix(n) + k!r}: {v!r}' for k, v in SEARCH_SPACES[n].items())}}},"
                       for n in d.candidates if n in SEARCH_SPACES)
    code = "from sklearn.model_selection import RandomizedSearchCV\n\nsearch_spaces = {\n" + spaces + "\n}\n" + fill('''
        space = search_spaces.get(best_name, {})
        if space:
            n_combos = int(np.prod([len(v) for v in space.values()]))
            search = RandomizedSearchCV(make_pipe(candidates[best_name]), space,
                                        n_iter=min(8, n_combos), cv=3, scoring=<<scoring>>, random_state=42)
            # Trials run one after another on purpose: each model already uses every CPU core, and running trials in
            # parallel on top (n_jobs=-1 here) would put cores x cores threads on the machine and crawl to a halt.
            search.fit(check, y_train.loc[check.index])
            print(pd.DataFrame(search.cv_results_)[["params", "mean_test_score"]].sort_values("mean_test_score", ascending=False)
                  .head(5).to_string(index=False))
            gain = search.best_score_ - cv_scores[best_name]
            if gain > 0:  # only adopt new settings if they beat the defaults the comparison already measured
                tuned_model = search.best_estimator_.named_steps["model"]
                settings = {k.split("__")[-1]: v for k, v in search.best_params_.items()}
                metrics.update(tuned_settings=settings, tuned_cv=float(search.best_score_))
                note(f"Tried {min(8, n_combos)} of {n_combos} setting combinations. Best: " + ", ".join(f"`{k}={v}`" for k, v in settings.items())
                     + f" → cross-validated {metrics['metric_name']} {cv_scores[best_name]:.3f} → **{search.best_score_:.3f}** ({gain:+.3f}).")
            else:
                tuned_model = candidates[best_name]
                note(f"Tried {min(8, n_combos)} of {n_combos} setting combinations, but none beat the default settings "
                     f"({search.best_score_:.3f} vs {cv_scores[best_name]:.3f}), so we keep the defaults. Defaults are often well chosen.")
        else:
            tuned_model = candidates[best_name]
            note(f"{best_name} has no settings worth tuning here (it finds its best fit directly), so we use it as is.")
    ''', scoring=repr(SCORING[d.primary_metric]))
    story = Step(
        "tune",
        "We know which kind of model wins, but it ran with default settings (hyperparameters such as tree depth or learning rate).",
        "Defaults are rarely best for a specific dataset. Tuning must also use cross-validation on training data only; picking settings "
        "by looking at the test set would leak it.",
        "We try several setting combinations for the winner with **RandomizedSearchCV** (3-fold cross-validation, preprocessing inside the "
        "pipeline), so that the model is tuned without ever seeing the test set.",
        "`search_spaces` lists the settings to try for each kind of model. `RandomizedSearchCV` samples up to 8 combinations, scores each "
        "with 3-fold cross-validation and keeps the best. `tuned_model` is that best version, which the next cell retrains on all training rows.",
    )
    return _cell("tune", "Tune the winner's settings", story, code)


def _train_cell(d: Decision) -> Cell:
    chunks = [fill('''
        from sklearn.base import clone

        pipe = make_pipe(clone(tuned_model))
        pipe.fit(X_train, y_train)
        y_pred = pipe.predict(X_test)

        metrics["train_score"] = score(y_train, pipe.predict(X_train))
        metrics["test_score"] = score(y_test, y_pred)
        print(f"{best_name}  {metrics['metric_name']}  train: {metrics['train_score']:.3f}   test: {metrics['test_score']:.3f}")
        gap = metrics["train_score"] - metrics["test_score"]
        verdict = ("⚠️ large: the model memorises training detail (overfitting)." if gap > 0.15 else
                   "🟡 moderate: mild overfitting, common for tree ensembles and usually acceptable." if gap > 0.05 else
                   "✅ small: it generalises to new rows.")
        note(f"Test {metrics['metric_name']} = **{metrics['test_score']:.3f}** vs baseline {metrics['baseline_score']:.3f}. "
             f"Train/test gap {gap:.3f}: {verdict}")
        if metrics["test_score"] >= 0.99 and metrics["baseline_score"] < 0.9:
            note("⚠️ A near-perfect score is rare on real data. Check that no input gives away the answer, for example a "
                 "column recorded *after* the outcome, or a copy of the target under another name. The 'Explain' step shows "
                 "which column the model leans on.")
        if len(X_test) < 100:
            note(f"The test set has only {len(X_test)} rows, so each row is worth {100 / len(X_test):.1f} percentage points. Treat this "
                 "score as rough; the cross-validated scores above are steadier.")
        fitted = pipe.named_steps["model"]
        fitted = getattr(fitted, "regressor_", fitted)
        if getattr(fitted, "oob_score_", None) is not None:
            oob_metric = "R²" if metrics["task"] == "regression" else "accuracy"
            note(f"Out-of-bag {oob_metric} = {fitted.oob_score_:.3f}: each tree is checked on the rows its bootstrap sample left "
                 "out, giving a free extra validation score (always accuracy or R², whatever the main metric).")
    ''')]
    if d.lookalikes:
        chunks.append(fill('''
            # Honest check: test rows with an identical twin in training are 'easy'. Score the rest on their own.
            row_key = pd.util.hash_pandas_object(X, index=False)
            unseen = ~row_key.loc[X_test.index].isin(set(row_key.loc[X_train.index]))
            metrics["unseen_share"] = float(unseen.mean())
            if unseen.sum() == 0:
                note(f"Every test row has an identical twin in training: the inputs only take {row_key.nunique()} different "
                     "combinations. There are no genuinely new rows to check, so the test score is as honest as it gets here.")
            else:
                metrics["test_score_unseen"] = score(y_test[unseen], y_pred[unseen.to_numpy()])
                note(f"On the {unseen.mean():.0%} of test rows with no identical twin in training: **{metrics['test_score_unseen']:.3f}**. "
                     "Expect about this on genuinely new data."
                     + (f" (Only {unseen.sum()} such rows, so treat it as rough.)" if unseen.sum() < 30 else ""))
        '''))
    if d.task == "regression":
        chunks.append(fill('''
            from sklearn.metrics import mean_absolute_error
            metrics["mae"] = mean_absolute_error(y_test, y_pred)
            note(f"On average a prediction is off by **{metrics['mae']:,.3g}** (MAE, in the units of `{target}`).")

            # Points close to the dashed line are good predictions
            plt.figure(figsize=(6, 6))
            plt.scatter(y_test, y_pred, alpha=0.4, color="#0071e3")
            lims = [min(y_test.min(), y_pred.min()), max(y_test.max(), y_pred.max())]
            plt.plot(lims, lims, "k--", linewidth=1)
            plt.xlabel("actual"); plt.ylabel("predicted"); plt.title("Predicted vs actual (test set)")
            plt.tight_layout(); plt.show()
        '''))
    else:
        chunks.append(fill('''
            from sklearn.metrics import ConfusionMatrixDisplay, classification_report
            print(classification_report(y_test, y_pred, zero_division=0))

            # Diagonal = correct predictions; off-diagonal = which classes get confused
            if y.nunique() <= 20:
                fig, ax = plt.subplots(figsize=(6, 5))
                ConfusionMatrixDisplay.from_predictions(y_test, y_pred, ax=ax, cmap="Blues", colorbar=False, xticks_rotation=45)
                ax.set_title("Confusion matrix (test set)")
                plt.tight_layout(); plt.show()
        '''))
    if d.positive_class is not None:
        chunks.append(fill('''
            from sklearn.metrics import RocCurveDisplay, roc_auc_score
            positive = <<pos>>
            proba = pipe.predict_proba(X_test)[:, list(pipe.classes_).index(<<pos>>)]
            metrics["roc_auc"] = float(roc_auc_score(y_test == <<pos>>, proba))
            RocCurveDisplay.from_predictions(y_test == <<pos>>, proba, name=best_name)
            plt.plot([0, 1], [0, 1], "k--", linewidth=1); plt.title("ROC curve (test set)")
            plt.tight_layout(); plt.show()
            note(f"ROC-AUC = **{metrics['roc_auc']:.3f}**: pick one `{positive}` row and one other row at random, and the model gives "
                 f"the `{positive}` row the higher probability {metrics['roc_auc']:.0%} of the time (0.5 = coin flip, 1.0 = perfect).")
        ''', pos=repr(d.positive_class)))
    story = Step(
        "train",
        "The comparison picked a winner and the tuning found its best settings.",
        "Until now each model saw only part of the training data. The final model should learn from all of it, then sit the 'final "
        "exam' on the untouched test set.",
        "We retrain the tuned winner on every training row and score it on training and test rows, so that we get the honest final "
        "score and can spot overfitting (great on train, poor on test).",
        "`clone()` makes a fresh copy of the tuned model, the `Pipeline` attaches the preprocessing, and `fit` learns from all training rows. "
        + ("A classification report gives precision and recall per class, the confusion matrix shows the mix-ups, and the ROC curve shows "
           "the trade-off at every threshold." if d.task == "classification" else
           "MAE is the average error in the target's units, and the scatter compares predictions with reality."),
    )
    return _cell("train", "Train and evaluate the winner", story, *chunks)


def _threshold_cell(d: Decision) -> Cell:
    code = fill('''
        from sklearn.metrics import f1_score, precision_recall_curve, precision_score, recall_score
        from sklearn.model_selection import cross_val_predict

        positive = <<pos>>
        pos_col = list(pipe.classes_).index(positive)

        # Out-of-fold probabilities: each training row is scored by a model that never saw it
        check = X_train.sample(min(len(X_train), 20000), random_state=42)
        y_check = y_train.loc[check.index] == positive
        oof = cross_val_predict(clone(pipe), check, y_train.loc[check.index], cv=3, method="predict_proba")[:, pos_col]
        precision, recall, thresholds = precision_recall_curve(y_check, oof)
        f1 = 2 * precision * recall / (precision + recall + 1e-12)
        threshold = float(thresholds[int(np.argmax(f1[:-1]))])

        # Final check on the untouched test set
        test_proba = pipe.predict_proba(X_test)[:, pos_col]
        is_pos = y_test == positive
        table = {}
        for label, t in [("default 0.50", 0.5), (f"tuned {threshold:.2f}", threshold)]:
            pred = test_proba >= t
            table[label] = {"precision": precision_score(is_pos, pred, zero_division=0),
                            "recall": recall_score(is_pos, pred), "F1": f1_score(is_pos, pred)}
        table = pd.DataFrame(table).T
        print(f"Test-set results for {positive!r}:")
        print(table.round(3).to_string())
        metrics.update(positive=positive, threshold=threshold,
                       f1_default=float(table["F1"].iloc[0]), f1_tuned=float(table["F1"].iloc[1]),
                       recall_default=float(table["recall"].iloc[0]), recall_tuned=float(table["recall"].iloc[1]),
                       precision_default=float(table["precision"].iloc[0]), precision_tuned=float(table["precision"].iloc[1]))

        plt.figure(figsize=(7, 4))
        plt.plot(thresholds, precision[:-1], label="precision")
        plt.plot(thresholds, recall[:-1], label="recall")
        plt.plot(thresholds, f1[:-1], label="F1")
        plt.axvline(threshold, color="grey", linestyle="--", label=f"chosen {threshold:.2f}")
        plt.xlabel(f"threshold: say {positive!r} when the model is at least this sure"); plt.legend()
        plt.title("The precision / recall trade-off")
        plt.tight_layout(); plt.show()

        note(f"Best threshold on training folds: **{threshold:.2f}**. On the test set, recall for {positive!r} goes "
             f"{metrics['recall_default']:.2f} → **{metrics['recall_tuned']:.2f}** and precision {metrics['precision_default']:.2f} → "
             f"**{metrics['precision_tuned']:.2f}** (F1 {metrics['f1_default']:.2f} → **{metrics['f1_tuned']:.2f}**).")
        if metrics["f1_tuned"] < metrics["f1_default"]:
            note("On this test set the default 0.50 did slightly better: a threshold picked on training rows doesn't always carry over. "
                 "Keep 0.50 unless catching more cases (recall) matters more to you than the extra false alarms.")
        note(f"Which threshold to use is a business choice: lower it if missing a {positive!r} costs more than a false alarm.")
    ''', pos=repr(d.positive_class))
    story = Step(
        "threshold",
        f"By default a model only says `{d.positive_class}` when it is more than 50% sure.",
        f"50% is arbitrary. A lower threshold catches more `{d.positive_class}` cases (higher **recall**) but raises more false alarms "
        "(lower **precision**). When the two kinds of mistake cost differently, this choice matters more than the model itself.",
        f"We pick the threshold with the best F1 for `{d.positive_class}` from cross-validated predictions on training rows, so that "
        "the test set stays untouched for the final check.",
        "`cross_val_predict` gives every training row a probability from a model that never saw it. `precision_recall_curve` lists "
        "precision and recall at every possible threshold, and we keep the one with the highest F1 (their balance). The test table "
        "then compares 0.50 with the tuned value.",
    )
    return _cell("threshold", "Tune the decision threshold", story, code)


def _tree_rules_cell(d: Decision) -> Cell:
    is_clf = d.task == "classification"
    code = fill('''
        from sklearn.compose import ColumnTransformer
        from sklearn.impute import SimpleImputer
        from sklearn.preprocessing import OneHotEncoder
        from sklearn.tree import <<tree>>, plot_tree

        # Same steps as before but WITHOUT scaling, so the split values are in real units (days, euros, ...)
        readable_prep = ColumnTransformer([
            ("numbers", SimpleImputer(strategy="median"), num_features),
            ("categories", OneHotEncoder(handle_unknown="infrequent_if_exist", max_categories=20, sparse_output=False), cat_features),
        ], verbose_feature_names_out=False)
        rules = Pipeline([("prep", readable_prep), ("tree", <<tree>>(max_depth=3, random_state=42))]).fit(X_train, y_train)
        names = list(rules.named_steps["prep"].get_feature_names_out())
        tree = rules.named_steps["tree"]

        plt.figure(figsize=(20, 8))
        plot_tree(tree, feature_names=names, <<classes>>filled=True, rounded=True, fontsize=9)
        plt.title("A 3-question decision tree: read from the top; left branch = the condition in the box is true")
        plt.show()

        rules_score = score(y_test, rules.predict(X_test))
        metrics["rules_score"] = float(rules_score)
        root, cut = names[tree.tree_.feature[0]], tree.tree_.threshold[0]
        category = next((c for c in cat_features if root.startswith(c + "_")), None)  # a one-hot column like "plan_basic"
        question = (f"is `{category}` = **{root[len(category) + 1:]}**? (no → left branch, yes → right)" if category
                    else f"is `{root}` ≤ **{cut:,.4g}**? (yes → left branch, no → right)")
        note("The tree's first and most important question: " + question)
        if rules_score <= metrics["baseline_score"] + 0.01:
            verdict = ("no better than the baseline: the pattern is spread over many columns, so 3 simple questions can't capture it "
                       "and the full model is needed")
        elif rules_score >= metrics["test_score"] - 0.005:
            verdict = ("as good as (or better than) the full model: these 3 simple rules are enough here, and much easier "
                       "to explain and use")
        elif rules_score >= metrics["test_score"] - 0.05:
            verdict = "so these simple rules capture **most** of what the full model knows"
        else:
            verdict = "so these simple rules capture **part** of what the full model knows"
        note(f"With just 3 questions the tree scores **{rules_score:.3f}** on test rows, against {metrics['test_score']:.3f} for "
             f"{best_name} and {metrics['baseline_score']:.3f} for the baseline: {verdict}.")
    ''', tree="DecisionTreeClassifier" if is_clf else "DecisionTreeRegressor",
       classes="class_names=[str(c) for c in rules.classes_], " if is_clf else "")
    story = Step(
        "rules",
        "The best model is accurate but often hard to read (an ensemble can contain hundreds of trees).",
        "Managers act on rules they can understand. A shallow decision tree turns the data into a few if/else questions, which is "
        "transparent decision-making.",
        "We grow a tree only 3 questions deep and draw it, so that you can see the main splits in plain units and how much accuracy simple rules keep.",
        "The same preprocessing but without scaling, so split values stay in real units. `max_depth=3` stops after 3 questions, "
        + ("and each box shows the split, its Gini impurity (0 = pure), the number of rows (samples) and the class mix (value)."
           if is_clf else "and each box shows the split, its squared error, the number of rows (samples) and the average target (value).")
        + " `score()` compares it with the full model.",
    )
    return _cell("rules", "Read the main rules (shallow decision tree)", story, code)


def _explain_cell(d: Decision) -> Cell:
    code = fill('''
        from sklearn.inspection import permutation_importance

        sample_rows = X_test.sample(min(len(X_test), 2000), random_state=42)  # a sample keeps this quick
        result = permutation_importance(pipe, sample_rows, y_test.loc[sample_rows.index], scoring=<<scoring>>, n_repeats=3, random_state=42)
        importance = pd.Series(result.importances_mean, index=sample_rows.columns).sort_values(ascending=False)
        metrics["top_features"] = importance.index[:5].tolist()

        importance.head(15).sort_values().plot.barh(color="#0071e3", figsize=(8, 5))
        plt.title("Which columns matter most?"); plt.xlabel("drop in score when the column is shuffled")
        plt.tight_layout(); plt.show()
        note("The model relies most on " + ", ".join(f"`{c}`" for c in importance.index[:3])
             + f". Shuffling `{importance.index[0]}` alone costs {importance.iloc[0]:.3f} {metrics['metric_name']}.")
        useless = importance[importance <= 0].index.tolist()
        if useless:
            note(f"{len(useless)} column(s) add nothing measurable to *this* model (e.g. " + ", ".join(f"`{c}`" for c in useless[:3])
                 + "). That doesn't mean they don't matter: the model may get the same information from other columns, or the "
                 "column may be rare. The key-driver table and the odds ratios judge each column on its own.")
    ''', scoring=repr(SCORING[d.primary_metric]))
    story = Step(
        "explain",
        f"We have a trained {d.task} model, but not yet *why* it predicts what it does.",
        "Knowing which columns matter builds trust, catches mistakes (e.g. a column that leaks the answer) and often teaches you something about the problem.",
        "We shuffle one column at a time and measure how much the test score drops (**permutation importance**), so that we see which "
        "inputs the model really relies on.",
        "`permutation_importance` scrambles each original column in turn (on up to 2,000 test rows), re-scores the model 3 times and "
        "averages the damage. A big drop means the model leans on that column; around zero means it ignores it.",
    )
    return _cell("explain", "Explain the model", story, code)


# ═════════════════════════════════════════════════════════════════════════
# Clustering cells
# ═════════════════════════════════════════════════════════════════════════
def _cluster_prep_cell(d: Decision) -> Cell:
    code = _preprocess_code(d) + "\n\n" + fill('''
        X = preprocessor.fit_transform(data[num_features + cat_features])
        names = [n.split("__", 1)[1] for n in preprocessor.get_feature_names_out()]
        rng = np.random.default_rng(42)
        X_small = X[rng.choice(len(X), size=min(len(X), 5000), replace=False)]  # a sample for the slower steps
        note(f"Clustering **{X.shape[0]:,} rows** described by **{X.shape[1]}** prepared columns, all on the same scale.")
    ''')
    story = Step(
        "cluster_prep",
        "Clustering groups rows by how 'far apart' they are, using Euclidean distance.",
        "If one column is in thousands and another in fractions, the big one would decide every distance on its own. K-Means "
        "requires scaled features.",
        "We fill gaps, scale every numeric column to mean 0 / spread 1 and turn categories into 0/1 columns, so that every feature has a fair say.",
        "The same recipe as for supervised models: `SimpleImputer` fills gaps, `StandardScaler` standardises and `OneHotEncoder` handles "
        "categories. `X` is the prepared table, and `X_small` is a 5,000-row sample for the slower steps.",
    )
    return _cell("preprocess", "Prepare features for clustering", story, code)


def _pca_cell(d: Decision) -> Cell:
    code = fill('''
        from sklearn.decomposition import PCA

        pca = PCA(random_state=42).fit(X_small)
        ratio = pca.explained_variance_ratio_
        n80 = int(np.searchsorted(ratio.cumsum(), 0.80) + 1)
        metrics["pca_n80"] = n80

        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar(range(1, len(ratio) + 1), ratio * 100, color="#0071e3", label="this component")
        ax.plot(range(1, len(ratio) + 1), ratio.cumsum() * 100, "o-", color="#d1495b", label="cumulative")
        ax.axhline(80, color="grey", linestyle="--", linewidth=1)
        ax.set_xlabel("principal component"); ax.set_ylabel("% of variance explained"); ax.legend()
        ax.set_title("How much information each principal component carries")
        plt.tight_layout(); plt.show()

        loadings = pd.DataFrame(pca.components_[:2].T, index=names, columns=["PC1", "PC2"])
        for pc in ["PC1", "PC2"]:
            top = loadings[pc].abs().sort_values(ascending=False).index[:3]
            print(f"{pc} is mostly made of: " + ", ".join(f"{c} ({loadings.loc[c, pc]:+.2f})" for c in top))
        note(f"**{n80} of {len(ratio)}** components keep 80% of the information. The first two alone keep "
             f"{ratio[:2].sum():.0%}, which is how faithful the 2-D cluster pictures below are.")
        note("PC1 mostly combines " + ", ".join(f"`{c}`" for c in loadings["PC1"].abs().sort_values(ascending=False).index[:3]) + ".")
    ''')
    story = Step(
        "pca",
        "The data has many columns, and we can't draw more than two dimensions on a screen.",
        "Many columns move together, so the data often lives in far fewer 'real' dimensions. **Principal Component Analysis** finds those "
        "directions, which helps with visualisation and noise reduction.",
        "We run PCA to see how many components are needed to keep 80% of the information and which columns build the first two, so "
        "that the 2-D cluster pictures can be read correctly.",
        "`PCA().fit` finds new axes (principal components) ordered by how much variation they capture. `explained_variance_ratio_` is "
        "each axis's share (the bars) and the line is the running total. Loadings show how much each original column contributes to an axis.",
    )
    return _cell("pca", "Reduce dimensions with PCA", story, code)


def _choose_k_cell(d: Decision) -> Cell:
    k_min, k_max = d.k_range
    code = d.model_import() + "\n" + fill('''
        from sklearn.metrics import silhouette_score

        scores, inertia = {}, {}
        for k in range(<<kmin>>, <<kmax>> + 1):
            km = <<ctor>>.fit(X_small)
            scores[k] = silhouette_score(X_small, km.labels_)
            inertia[k] = km.inertia_
            print(f"k={k}: silhouette = {scores[k]:.3f}   inertia = {km.inertia_:,.0f}")
        best_k = max(scores, key=scores.get)

        fig, axes = plt.subplots(1, 2, figsize=(13, 4))
        axes[0].plot(list(inertia), list(inertia.values()), "o-", color="#0071e3")
        axes[0].set_title("Elbow method: look for the bend"); axes[0].set_xlabel("k"); axes[0].set_ylabel("inertia (lower = tighter)")
        axes[1].plot(list(scores), list(scores.values()), "o-", color="#0071e3")
        axes[1].axvline(best_k, color="grey", linestyle="--")
        axes[1].set_title("Silhouette: higher = better separated"); axes[1].set_xlabel("k")
        plt.tight_layout(); plt.show()
        note(f"Silhouette is highest at **k = {best_k}** ({scores[best_k]:.2f}). Inertia always falls as k grows, so we look for the "
             "'elbow' where it stops falling fast; the silhouette makes that choice objective.")
    ''', kmin=k_min, kmax=k_max, ctor=d.model_constructor())
    story = Step(
        "choose_k",
        f"Our features are prepared and PCA shows the data's shape. We don't know how many groups (k) exist; we'll try k = {k_min}…{k_max}.",
        "K-Means needs k in advance. Too few clusters lumps different customers together, and too many splits real groups apart.",
        "We fit K-Means for every k and read two charts, the **elbow** (inertia) and the **silhouette score**, so that k is chosen from evidence.",
        "For each k, K-Means (k-means++ start, Euclidean distance) assigns rows to the nearest centre. **Inertia** is the total squared "
        "distance to the centres. The **silhouette** compares each row's distance to its own cluster with the next nearest one, from "
        "-1 to 1. We keep the k with the best silhouette.",
    )
    return _cell("choose_k", "Choose the number of clusters", story, code)


def _cluster_fit_cell(d: Decision) -> Cell:
    code = fill('''
        k = best_k
        model = <<ctor>>
        data["cluster"] = model.fit_predict(X)
        metrics.update(k=int(k), silhouette=float(scores[k]))

        # Draw the clusters on the first two principal components (and their centres as crosses)
        pca2 = PCA(n_components=2, random_state=42).fit(X_small)
        points, centres = pca2.transform(X_small), pca2.transform(model.cluster_centers_)
        plt.figure(figsize=(7, 6))
        plt.scatter(points[:, 0], points[:, 1], c=model.predict(X_small), cmap="tab10", alpha=0.5, s=10)
        plt.scatter(centres[:, 0], centres[:, 1], marker="X", s=200, c="black")
        plt.xlabel("PC1"); plt.ylabel("PC2"); plt.title(f"{k} clusters (2-D view, X = centre)")
        plt.tight_layout(); plt.show()

        sizes = data["cluster"].value_counts(normalize=True).sort_index()
        if num_features:
            profile = data.groupby("cluster")[num_features].mean()
            print("Average of each numeric column per cluster:")
            print(profile.round(2).to_string())
            z = (profile - data[num_features].mean()) / data[num_features].std().replace(0, 1)
            for c in profile.index:
                top = z.loc[c].abs().sort_values(ascending=False).index[:2]
                traits = " and ".join(f"{'higher' if z.loc[c, t] > 0 else 'lower'} `{t}` ({profile.loc[c, t]:,.3g})" for t in top)
                note(f"Cluster {c} ({sizes[c]:.0%} of rows): {traits} than average.")
        else:
            note("Cluster sizes: " + ", ".join(f"{c}: {s:.0%}" for c, s in sizes.items()) + ".")
    ''', ctor=d.model_constructor())
    story = Step(
        "cluster_fit",
        "The silhouette and elbow charts pointed to the best number of clusters.",
        "Groups are only useful if we can see them and describe them in business terms (e.g. 'price-sensitive families').",
        "We fit the final K-Means model, draw the clusters on the first two principal components, and describe each cluster by how its "
        "averages differ from the overall average, so that each group gets a clear profile.",
        "`fit_predict` assigns every row to its nearest centroid. PCA projects rows and centroids to 2-D for the plot. The profile table "
        "averages each numeric column per cluster, and the notes name the two columns where each cluster stands out most (in standard deviations).",
    )
    return _cell("cluster_fit", "Fit clusters and describe them", story, code)


def _hierarchical_cell(d: Decision) -> Cell:
    code = fill('''
        from scipy.cluster.hierarchy import dendrogram, linkage
        from sklearn.cluster import AgglomerativeClustering
        from sklearn.metrics import adjusted_rand_score

        X_h = X_small[:1500]  # hierarchical clustering compares every pair of rows: keep it small
        tree = linkage(X_h, method="ward")
        cut = tree[-(k - 1), 2]  # the height at which the tree splits into k groups
        plt.figure(figsize=(12, 4.5))
        dendrogram(tree, truncate_mode="lastp", p=30, color_threshold=cut, no_labels=True)
        plt.axhline(cut, color="grey", linestyle="--", label=f"cut into {k} clusters")
        plt.title("Dendrogram: groups merging from bottom to top (long vertical lines = natural cut points)")
        plt.ylabel("merge distance (ward)"); plt.legend(); plt.tight_layout(); plt.show()

        agg = AgglomerativeClustering(n_clusters=k, linkage="ward").fit_predict(X_h)
        km_labels = model.predict(X_h)
        sil_agg, sil_km = silhouette_score(X_h, agg), silhouette_score(X_h, km_labels)
        agreement = adjusted_rand_score(km_labels, agg)
        metrics.update(hier_silhouette=float(sil_agg), cluster_agreement=float(agreement))
        note(f"Hierarchical clustering into {k} groups: silhouette **{sil_agg:.2f}** vs {sil_km:.2f} for K-Means on the same rows.")
        note(f"Agreement between the two methods (adjusted Rand index): **{agreement:.2f}** (1 = identical groups, 0 = random). "
             + ("The groups are robust: two different methods find them." if agreement > 0.6 else
                "The methods disagree, so the groups are soft. Treat them as tendencies, not hard segments."))
    ''')
    story = Step(
        "hierarchical",
        "K-Means found clusters, but it assumes round, similar-sized groups and needs k up front.",
        "**Hierarchical clustering** makes no such assumption. It merges the closest groups step by step, and the **dendrogram** shows "
        "where natural cuts are. If both methods agree, the segments are real; if not, they are fuzzy.",
        "We build a ward-linkage hierarchy on a sample, cut it into the same k groups and compare it with K-Means, so that we know how "
        "trustworthy the segments are.",
        "`linkage(..., 'ward')` repeatedly merges the two groups whose union adds the least variance. `dendrogram` draws those merges, "
        "and the dashed line is where we cut. `AgglomerativeClustering` gives the labels, and the adjusted Rand index measures agreement "
        "with K-Means. It is limited to 1,500 rows because the cost grows with the square of the number of rows.",
    )
    return _cell("hierarchical", "Cross-check with hierarchical clustering", story, code)


# ═════════════════════════════════════════════════════════════════════════
# Public API
# ═════════════════════════════════════════════════════════════════════════
def build_cells(d: Decision) -> list[Cell]:
    if d.halted:
        return []
    cells = [_setup_cell(d), _prepare_cell(d), _eda_cell(d)]
    if d.task == "clustering":
        return cells + [_cluster_prep_cell(d), _pca_cell(d), _choose_k_cell(d), _cluster_fit_cell(d), _hierarchical_cell(d)]
    if d.task == "regression" or d.positive_class is not None:
        cells.append(_insights_cell(d))
    cells += [_split_cell(d), _preprocess_cell(d), _baseline_cell(d)]
    if d.task == "regression":
        cells += [_vif_cell(d), _ols_cell(d), _assumptions_cell(d)]
    elif d.positive_class is not None:
        cells.append(_logit_cell(d))
    cells.append(_compare_cell(d))
    if d.resample:
        cells.append(_resample_cell(d))
    cells += [_tune_cell(d), _train_cell(d)]
    if d.positive_class is not None:
        cells.append(_threshold_cell(d))
    return cells + [_tree_rules_cell(d), _explain_cell(d)]


def to_notebook(cells: list[Cell], decision: Decision, filename: str) -> str:
    """Export the cells, with their full stories, as a .ipynb JSON string."""
    import nbformat
    from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

    reader = "read_csv" if filename.lower().endswith(".csv") else "read_excel"
    nb_cells = [new_markdown_cell(
        "# AutoML Explorer notebook\n\n"
        f"Generated from `{filename}`. Every step explains **what we found before**, **why it matters**, **what we're going to do** "
        "and **what the code does**. After it runs, the 📌 lines say **what we found**.\n\n## Decision trace\n\n"
        + "\n".join(f"- {s.short()}" for s in decision.steps)
    )]
    for i, cell in enumerate(cells, 1):
        nb_cells.append(new_markdown_cell(f"## {i}. {cell.title}\n\n{cell.story.markdown()}\n\n🛠 **What this code does:** {cell.story.code}"))
        code = cell.code
        if cell.id == "setup":
            code += f'\n\ndf = pd.{reader}("{filename}")  # make sure the file is next to this notebook\n'
        nb_cells.append(new_code_cell(code))
    return nbformat.writes(new_notebook(cells=nb_cells))
