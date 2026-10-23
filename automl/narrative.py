"""Beginner-friendly explanations.

Every decision the engine makes is recorded as a ``Step``:

* found  - the evidence we saw in the data (with real numbers / column names)
* why    - why that evidence matters, in plain English
* action - what we do about it, phrased as "We ..., so that ..."
* code   - (notebook cells) what the code does, in plain words

The engine's decisions use ``TEMPLATES`` below; each notebook cell's story lives in
``codegen.py`` next to the code it explains. After a cell runs, its code writes
"what we found" itself with ``note()``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Step:
    key: str
    found: str          # 🔍 what we found before this step (the evidence)
    why: str            # 💡 why that evidence matters
    action: str         # 🎯 what we're going to do, "so that ..."
    code: str = ""      # 🛠 what the code does, in plain words (notebook cells only)

    def markdown(self) -> str:
        return (
            f"🔍 **What we found before:** {self.found}  \n"
            f"💡 **Why it matters:** {self.why}  \n"
            f"🎯 **What we're going to do:** {self.action}"
        )

    def short(self) -> str:
        """One-line version for the sidebar decision trace."""
        return f"{self.found} → {self.action}"


TEMPLATES: dict[str, dict[str, str]] = {
    # ── Data quality ──────────────────────────────────────────────────────
    "overview": {
        "found": "Your file has {rows:,} rows and {cols} columns ({n_num} numeric, {n_cat} text/category).",
        "why": "The size of the data and the type of each column decide which tools are safe to use.",
        "action": "We profile every column first, so that each later decision is based on evidence instead of guesses.",
    },
    "id_column": {
        "found": "`{col}` has a different value in {pct:.0f}% of rows - it looks like an ID.",
        "why": "An ID only names a row; it says nothing about the outcome. A model would just memorise it.",
        "action": "We drop `{col}`, so that the model learns real patterns instead of row numbers.",
    },
    "constant_column": {
        "found": "`{col}` has the same value in every row.",
        "why": "A column that never changes can't help tell rows apart.",
        "action": "We drop `{col}`, so that it doesn't waste time or memory.",
    },
    "text_column": {
        "found": "`{col}` contains long free text (about {avg_len:.0f} characters per row).",
        "why": "Free text needs special language tools; treating each sentence as a category would create thousands of useless columns.",
        "action": "We set `{col}` aside for now, so that the pipeline stays fast and simple.",
    },
    "missing_column": {
        "found": "`{col}` is empty in {pct:.0f}% of rows.",
        "why": "When most of a column is missing, any value we fill in is mostly invented.",
        "action": "We drop `{col}`, so that the model isn't trained on made-up numbers.",
    },
    "infinite_values": {
        "found": "{n:,} cell(s) hold infinity (e.g. in {examples}), usually from a division by zero upstream.",
        "why": "Infinity is not a real measurement, and it breaks averages, charts and every model.",
        "action": "We treat infinite values as missing, so that they are filled like any other gap instead of crashing the pipeline.",
    },
    "datetime_column": {
        "found": "`{col}` holds dates.",
        "why": "Models can't read a date directly, but the year, month and weekday often carry real signal (e.g. seasons, weekends).",
        "action": "We split `{col}` into year / month / weekday numbers, so that the model can use the time information.",
    },
    "high_card_column": {
        "found": "`{col}` is a category column with {n_unique} different values.",
        "why": "Turning every rare value into its own column would make the data huge and noisy.",
        "action": "We keep only its most common values and group the rest as 'other', so that the data stays compact.",
    },
    "duplicates": {
        "found": "{n:,} row(s) ({pct:.1f}%) are exact copies of other rows - few enough to look like accidental double entries.",
        "why": "Duplicates can land in both the training and test sets, which makes scores look better than they really are.",
        "action": "We remove the duplicate rows, so that the test score is honest.",
    },
    "duplicates_kept": {
        "found": "{n:,} rows ({pct:.0f}%) are exact copies of other rows - far too many to be typing mistakes.",
        "why": "That many copies usually means the same real situation happens again and again (e.g. many identical bookings). Deleting them would throw away real information.",
        "action": "We keep them{extra}.",
    },
    "small_data": {
        "found": "The file has only {rows} rows, so the test set will hold about {n_test}.",
        "why": "With so few test rows, one wrong prediction moves the test score by about {pct:.0f} percentage points, and flexible models can simply memorise the data.",
        "action": "We still run every step, but trust the cross-validated scores more than the single test score, so that one lucky or unlucky split doesn't mislead you.",
    },
    "boosting_libs_missing": {
        "found": "{libs} is not installed.",
        "why": "These are popular boosting libraries. scikit-learn's HistGradientBoosting uses the same idea (it is modelled on LightGBM), so the comparison is still fair without them.",
        "action": "We compare the models that are available. Run `pip install {pip}` and they will be added automatically.",
    },
    "boosting_libs_broken": {
        "found": "{libs} is installed but could not be loaded (`{err}`).",
        "why": "XGBoost and LightGBM are compiled programs that need the OpenMP runtime (`libomp`). On a Mac it is not there by default, "
               "so importing them fails. scikit-learn's HistGradientBoosting uses the same boosting idea, so the comparison is still fair without them.",
        "action": "We skip {libs} instead of crashing. To include it: on macOS run `brew install libomp` (or `pip uninstall {pip}` to silence this), "
                  "then restart the app.",
    },
    "lookalike_rows": {
        "found": "{n:,} rows ({pct:.0f}%) are identical in every column except the ID {ids}.",
        "why": "They may be genuine repeat records, so deleting them would be wrong. But a test row with an identical twin in training is an 'easy question', which can make the test score look better than it really is.",
        "action": "We keep them, and also report the score on test rows that have no exact twin in training, so that you get an honest number.",
    },
    "date_parts": {
        "found": "`{year}`, `{month}` and `{day}` together form one date ({invalid} rows are impossible dates, like 29 February in a non-leap year).",
        "why": "Stored separately, they can't tell the model the day of the week - and weekend vs weekday often matters.",
        "action": "We combine them into a real date and add `{prefix}_weekday`; impossible dates are left blank and filled like any other gap.",
    },
    "target_like_excluded": {
        "found": "`{col}` looks like an outcome column ({reason}).",
        "why": "Using the answer as a clustering input would just group rows by that answer.",
        "action": "We leave `{col}` out of the clustering, so that the groups come from the other columns. Pick it as the target if you want to predict it instead.",
    },
    "suggested_target": {
        "found": "`{col}` looks like the outcome column: {reason}.",
        "why": "Most tables are collected to predict one thing. Predicting it gives a model with a clear, checkable score.",
        "action": "We pre-selected `{col}` as the target, so that we predict it. Choose 'Nothing' if you'd rather look for groups.",
    },
    "compare_models": {
        "found": "Rules of thumb point to **{rule_model}** for data of this size and shape.",
        "why": "Rules of thumb can be wrong for a particular dataset.",
        "action": "We test {n} different kinds of model ({names}) with cross-validation, so that the data - not a guess - picks the winner.",
    },
    "positive_class": {
        "found": "The rarer class is `{pos}` ({pct:.1f}% of rows).",
        "why": "In most business problems the rare outcome (a cancellation, a fraud, a churn) is the one worth catching.",
        "action": "We treat `{pos}` as the class to catch and tune the model's decision threshold for it, so that it finds more of them.",
    },
    "missing_values": {
        "found": "{n_cols} feature column(s) still have some missing values (e.g. {examples}).",
        "why": "Most models crash or behave badly when they see an empty cell.",
        "action": "We fill gaps with the median (numbers) or a 'missing' category (categories), so that no rows have to be thrown away.",
    },
    # ── Task choice ───────────────────────────────────────────────────────
    "target_auto_found": {
        "found": "You let the engine decide, and `{col}` looks like the outcome: {reason}.",
        "why": "Most tables are collected to predict one thing. A target gives the model a right answer to learn from and a score you can check.",
        "action": "We predict `{col}`, so that you get a model and a measurable score. Switch to 'I'll pick the target' if you meant a different column.",
    },
    "target_auto_none": {
        "found": "You let the engine decide, and no column looks like an outcome: none is named like status / label / churn / target, and the last column isn't a text category with 2-15 values (number codes such as 1-4 are too ambiguous to guess).",
        "why": "Guessing a target would mean predicting something nobody asked for. Without a right-answer column, the honest question is what groups exist.",
        "action": "We look for natural groups instead (clustering), so that you still learn the data's structure. Pick a target yourself if you had one in mind.",
    },
    "target_manual": {
        "found": "You chose `{col}` as the target.{hint}",
        "why": "You know your business question best, so your choice always wins over the engine's guess.",
        "action": "We predict `{col}`, so that the whole pipeline answers your question.",
    },
    "target_none": {
        "found": "You chose not to predict anything.",
        "why": "Without a right-answer column there is nothing to score predictions against, but there is still structure to find.",
        "action": "We look for natural groups (clustering), so that rows that behave alike are put together.",
    },
    "task_clustering": {
        "found": "There is no target column.",
        "why": "Without a 'right answer' column we can't train a model to predict something - but we can still look for natural groups.",
        "action": "We run clustering, so that rows that look alike are grouped together.",
    },
    "task_regression": {
        "found": "`{target}` is numeric with {n_unique} different values.",
        "why": "A target with many possible numbers is a quantity (like a price), not a label.",
        "action": "We treat this as **regression**, so that the model predicts a number.",
    },
    "task_classification": {
        "found": "`{target}` has {n_unique} distinct values{numeric_note}.",
        "why": "A target with a small set of values is a label (like yes/no or a type), not a quantity.",
        "action": "We treat this as **{subtype} classification**, so that the model predicts which group each row belongs to.",
    },
    "target_missing": {
        "found": "{n:,} row(s) have no value for the target `{target}`.",
        "why": "A row without an answer can't teach the model anything.",
        "action": "We drop those rows, so that every training example has a known answer.",
    },
    "rare_classes": {
        "found": "{n_classes} class(es) in `{target}` appear only once: {examples}.",
        "why": "A class seen once can't appear in both the training and the test set.",
        "action": "We drop those {n_rows} row(s), so that the train/test split works.",
    },
    "uneven": {
        "found": "The classes are uneven: the smallest is {min_pct:.1f}% of rows vs {max_pct:.1f}% for the largest.",
        "why": "Accuracy would flatter a lazy model: one that always guesses the big class is already {max_pct:.0f}% 'accurate' while catching none of the small class.",
        "action": "We judge every model by **macro F1** (the average F1 over the classes), so that the small class counts as much as the big one.",
    },
    "imbalanced": {
        "found": "Classes are uneven: the smallest class is {min_pct:.1f}% of rows vs {max_pct:.1f}% for the largest.",
        "why": "A model can score high accuracy by always guessing the big class while ignoring the small one.",
        "action": "We weight the rare classes more heavily and judge the model by **macro F1**, so that every class counts equally.{extra}",
    },
    "skewed_target": {
        "found": "`{target}` is skewed (skew = {skew:.2f}): a few very large values and many small ones.",
        "why": "Models chase the big values and do worse on typical rows.",
        "action": "We train on log(`{target}`) and convert predictions back, so that errors on small and large values are treated fairly.",
    },
    # ── Model choice ──────────────────────────────────────────────────────
    "model_small": {
        "found": "The data is small ({rows:,} rows).",
        "why": "Complex models tend to memorise small datasets (overfitting). Simple linear models are more reliable here and easy to interpret.",
        "action": "We use **{model}**, so that the result is stable and explainable.",
    },
    "model_medium": {
        "found": "The data is medium-sized ({rows:,} rows).",
        "why": "There is enough data for a model that captures curves and interactions between columns.",
        "action": "We use **{model}** (many decision trees voting together), so that non-linear patterns are captured without much tuning.",
    },
    "model_large": {
        "found": "The data is large ({rows:,} rows).",
        "why": "Random forests get slow on big data; histogram-based gradient boosting is built for speed at this scale.",
        "action": "We use **{model}**, so that training stays fast without losing accuracy.",
    },
    "many_features": {
        "found": "There are {n_features} feature columns for only {rows:,} rows.",
        "why": "With many columns and few rows, a linear model can fit noise.",
        "action": "We use **LassoCV**, which switches off unhelpful columns, so that only useful features are kept.",
    },
    "collinear": {
        "found": "`{a}` and `{b}` move almost together (correlation {r:.2f}).",
        "why": "When two inputs say the same thing, a plain linear model gets confused about how much credit to give each.",
        "action": "We use **RidgeCV**, which shares credit between similar columns, so that the coefficients stay stable.",
    },
    "sampled": {
        "found": "The data has {rows:,} rows.",
        "why": "Training on every row would be slow, and beyond a point extra rows add very little accuracy.",
        "action": "We train on a random sample of {n:,} rows, so that results arrive in seconds.",
    },
    "clustering_model": {
        "found": "There are {rows:,} rows and {n_features} usable feature columns.",
        "why": "{reason}",
        "action": "We use **{model}**, so that grouping is {benefit}.",
    },
    # ── Halts ─────────────────────────────────────────────────────────────
    "halt_small": {
        "found": "The file has only {rows} rows.",
        "why": "With fewer than 30 rows any model result would mostly be luck.",
        "action": "We stop here. Please upload a larger dataset, so that the results mean something.",
    },
    "halt_columns": {
        "found": "The file has only {cols} column(s).",
        "why": "We need at least one input column to learn from.",
        "action": "We stop here. Please upload data with more columns.",
    },
    "halt_missing": {
        "found": "{pct:.0f}% of all cells are empty.",
        "why": "With most of the data missing, anything we learn would be built on guesses.",
        "action": "We stop here. Please clean the data first, so that there is real information to learn from.",
    },
    "halt_target_type": {
        "found": "`{target}` is a {role} column.",
        "why": "{reason}",
        "action": "We stop here. Please pick a different target column.",
    },
    "halt_no_features": {
        "found": "After cleaning, no usable feature columns are left.",
        "why": "A model needs at least one input column to learn from.",
        "action": "We stop here. Please check the column profile to see why each column was dropped.",
    },
}


def story(key: str, **values) -> Step:
    t = TEMPLATES[key]
    return Step(key, t["found"].format(**values), t["why"].format(**values), t["action"].format(**values))


GLOSSARY: dict[str, str] = {
    "Feature": "An input column the model uses to make a prediction.",
    "Target": "The column we want to predict (the 'answer').",
    "Classification": "Predicting a label or group, e.g. spam / not spam.",
    "Regression": "Predicting a number, e.g. a house price.",
    "Clustering": "Grouping similar rows together when there is no answer column.",
    "Train / test split": "Learning from one part of the data and checking on another part the model has never seen.",
    "Overfitting": "When a model memorises the training data and does poorly on new data.",
    "Baseline": "The score of a trivial strategy (e.g. always guess the most common class). A real model must beat it.",
    "Accuracy": "The share of predictions that are exactly right.",
    "Macro F1": "The average F1 score over all classes, so small classes count as much as big ones. 1.0 is perfect.",
    "R²": "How much of the variation in the target the model explains. 1.0 is perfect, 0 is no better than guessing the average.",
    "MAE": "Mean absolute error: on average, how far predictions are from the truth, in the target's own units.",
    "Silhouette score": "How well separated clusters are, from -1 to 1. Above 0.5 is strong, 0.25-0.5 is reasonable, below 0.25 is weak.",
    "One-hot encoding": "Turning a category column into several 0/1 columns, one per category.",
    "Scaling": "Rescaling numeric columns to a similar range so no column dominates just because its numbers are bigger.",
    "Imputation": "Filling in missing values with a sensible guess, e.g. the median.",
    "Permutation importance": "How much the score drops when one column is randomly shuffled. A big drop means the model relies on that column.",
    "PCA": "Principal Component Analysis: new axes that capture the most variation, so many columns can be summarised (and drawn) with a few.",
    "Cross-validation": "Splitting the training data into k parts and letting each part be the 'test' once, so a score doesn't depend on one lucky split.",
    "Hyperparameter": "A setting chosen before training (tree depth, learning rate). Tuned with cross-validation, never on the test set.",
    "Data leakage": "When information from the test set (or the answer) sneaks into training, making scores look better than reality.",
    "OLS": "Ordinary Least Squares: linear regression that picks the line with the smallest squared errors. y = β₀ + β₁x₁ + …",
    "Coefficient (β)": "How much the target changes when one input rises by 1 unit, holding the others fixed. β₀ is the intercept (constant).",
    "p-value": "How surprising an effect would be if the true effect were zero. p < 0.05 is the usual bar for 'statistically significant'.",
    "Confidence interval": "The range that contains the true value 95% of the time. If it excludes 0, the effect is significant.",
    "Adjusted R²": "R² with a penalty for each extra input, so adding useless columns can't make it look better.",
    "F-statistic": "Tests whether all inputs together explain the target better than no inputs at all.",
    "VIF": "Variance Inflation Factor: how much a column is just a copy of the others. Above 10 = serious multicollinearity.",
    "Homoscedasticity": "The errors have the same spread for small and large predictions. Its opposite (a funnel shape) makes p-values unreliable.",
    "Residual": "Actual value minus predicted value: the error on one row.",
    "Odds ratio": "In logistic regression: how many times the odds of the outcome are multiplied when an input rises by 1 (1 = no effect).",
    "ROC-AUC": "The chance that the model ranks a random positive row above a random negative one. 0.5 = coin flip, 1 = perfect.",
    "Precision / Recall": "Precision: of the rows flagged, how many were right. Recall: of the true cases, how many were caught.",
    "Gini impurity": "How mixed the classes are in a tree node: 0 = all one class (pure).",
    "Bagging": "Training many models on random bootstrap samples and averaging them (random forest). Reduces variance / overfitting.",
    "Boosting": "Training models one after another, each focusing on the previous one's mistakes (AdaBoost, gradient boosting).",
    "Out-of-bag score": "A random forest's free validation score: each tree is tested on the rows its bootstrap sample left out.",
    "Elbow method": "Plotting inertia against k and picking the k where adding more clusters stops helping much.",
    "Inertia": "Total squared distance from each row to its cluster centre (lower = tighter clusters).",
    "Dendrogram": "A tree diagram of hierarchical clustering: how groups merge, and at what distance.",
}


OVERFIT_SMALL, OVERFIT_LARGE = 0.05, 0.15  # train-test gaps: below the first = fine, above the second = overfitting


def overfit_verdict(train: float, test: float) -> str:
    """One sentence on the train/test gap. The generated train cell uses the same thresholds."""
    gap = train - test
    if gap > OVERFIT_LARGE:
        return (f"⚠️ Training score ({train:.2f}) is much higher than test score ({test:.2f}), a gap of {gap:.2f}: a sign of "
                "**overfitting**. More data, or simpler settings, would help.")
    if gap > OVERFIT_SMALL:
        return (f"🟡 Training ({train:.2f}) is somewhat above test ({test:.2f}), a gap of {gap:.2f}: **mild overfitting**. This is "
                "common for tree ensembles and usually acceptable; the test score is the honest one.")
    return f"✅ Training ({train:.2f}) and test ({test:.2f}) scores are close, so the model **generalises** well to new rows."


def interpret(metrics: dict, target: str | None) -> list[str]:
    """Turn the numbers produced by the generated code into plain sentences."""
    task = metrics.get("task")
    out: list[str] = []

    if task == "clustering":
        k, sil = metrics.get("k"), metrics.get("silhouette")
        if k is None or sil is None:
            return out
        strength = "strong" if sil >= 0.5 else "reasonable" if sil >= 0.25 else "weak"
        out.append(f"The data splits best into **{k} groups**, with a silhouette score of **{sil:.2f}** - that is **{strength}** separation.")
        if strength == "weak":
            out.append("Weak separation means the groups overlap a lot; treat them as rough tendencies rather than clear-cut types.")
        if "pca_n80" in metrics:
            out.append(f"PCA: {metrics['pca_n80']} principal component(s) hold 80% of the information.")
        if "cluster_agreement" in metrics:
            agree = metrics["cluster_agreement"]
            out.append(f"Hierarchical clustering agrees with K-Means at **{agree:.2f}** (adjusted Rand), so the segments are "
                       + ("**robust**." if agree > 0.6 else "**soft**: treat them as tendencies."))
        out.append("Use the cluster profiles above to name each group in business terms.")
        return out

    name = metrics.get("metric_name")
    test, train, base = metrics.get("test_score"), metrics.get("train_score"), metrics.get("baseline_score")
    if test is None or name is None:
        return out

    cv = metrics.get("cv_scores")
    if cv and len(cv) > 1:
        ranked = sorted(cv.items(), key=lambda kv: kv[1], reverse=True)
        others = ", ".join(f"{n} {v:.2f}" for n, v in ranked[1:])
        out.append(f"We compared {len(cv)} kinds of model; **{ranked[0][0]}** won cross-validation with {ranked[0][1]:.2f} (vs {others}).")

    if name == "R²":
        out.append(f"**R² = {test:.2f}** on the test set: the model explains about **{max(test, 0) * 100:.0f}%** of the variation in `{target}`.")
        if "mae" in metrics:
            out.append(f"On average its predictions are off by **{metrics['mae']:,.3g}** (MAE, in the units of `{target}`).")
    else:
        out.append(f"**{name} = {test:.2f}** on the test set (1.0 would be perfect).")

    if base is not None:
        gain = test - base
        if gain <= 0.01:
            out.append(f"⚠️ The baseline scores {base:.2f}, so the model is **not doing better than guessing**. The inputs may not contain enough signal about `{target}`.")
        elif gain < 0.1:
            out.append(f"The baseline scores {base:.2f}, so the model adds a **small but real** improvement.")
        else:
            out.append(f"The baseline scores {base:.2f}, so the model is **clearly learning** something useful.")

    if train is not None:
        out.append(overfit_verdict(train, test))

    if "balance_method" in metrics:
        out.append(f"Class imbalance: **{metrics['balance_method']}** worked best among class weights, oversampling, undersampling "
                   "and SMOTE.")
    if "tuned_settings" in metrics:
        out.append(f"Tuning (RandomizedSearchCV) set " + ", ".join(f"`{k}={v}`" for k, v in metrics["tuned_settings"].items())
                   + f", giving a cross-validated score of {metrics['tuned_cv']:.3f}.")
    if "roc_auc" in metrics:
        out.append(f"ROC-AUC = **{metrics['roc_auc']:.3f}**: the model separates the two classes "
                   + ("very well." if metrics["roc_auc"] >= 0.9 else "reasonably." if metrics["roc_auc"] >= 0.75 else "only weakly."))
    if "ols_r2" in metrics:
        out.append(f"OLS (the explainable straight-line model): R² {metrics['ols_r2']:.2f}, adjusted {metrics['ols_adj_r2']:.2f}, with "
                   f"{metrics['ols_significant']} of {metrics['ols_inputs']} inputs significant (p < 0.05).")
    failed = [name.split(" (")[0] for name, ok in metrics.get("assumptions", {}).items() if not ok]
    if "assumptions" in metrics:
        out.append("All regression assumptions look fine, so the p-values and intervals can be trusted." if not failed else
                   "Regression assumptions to keep in mind: " + ", ".join(failed) + ". Read the OLS p-values with some caution.")
    if "logit_pseudo_r2" in metrics:
        out.append(f"Logistic regression (the explainable model) reaches pseudo R² {metrics['logit_pseudo_r2']:.2f}. Its odds ratios "
                   "show the direction and size of each driver.")

    unseen = metrics.get("test_score_unseen")
    if unseen is not None:
        share = metrics.get("unseen_share", 0)
        gap = test - unseen
        verdict = ("so the duplicates make the headline score look a little better than it really is"
                   if gap > 0.02 else "so the duplicates are not inflating the score")
        out.append(f"On the {share:.0%} of test rows with no identical twin in training, {name} is **{unseen:.2f}** "
                   f"(vs {test:.2f} overall), {verdict}.")

    if "threshold" in metrics:
        pos, t = metrics["positive"], metrics["threshold"]
        out.append(f"For `{pos}`, moving the decision threshold from 0.50 to **{t:.2f}** changes recall "
                   f"{metrics['recall_default']:.2f} → {metrics['recall_tuned']:.2f}, precision "
                   f"{metrics['precision_default']:.2f} → {metrics['precision_tuned']:.2f} and F1 "
                   f"{metrics['f1_default']:.2f} → **{metrics['f1_tuned']:.2f}**. Pick the threshold that matches the "
                   f"business cost: a missed `{pos}` vs a false alarm.")

    drivers = metrics.get("drivers")
    if drivers:
        pct = "overall_outcome" in metrics and metrics.get("task") == "classification"
        show = (lambda v: f"{v:.0%}") if pct else (lambda v: f"{v:,.3g}")
        for col, info in list(drivers.items())[:2]:
            out.append(f"Key driver `{col}`: the outcome ranges from {show(info['low_value'])} (`{info['low']}`) "
                       f"to {show(info['high_value'])} (`{info['high']}`).")

    top = metrics.get("top_features")
    if top:
        out.append("The most influential columns are " + ", ".join(f"`{c}`" for c in top[:3]) + ".")
    return out
