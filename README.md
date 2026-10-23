# AutoML Explorer

Upload a CSV or Excel file. AutoML Explorer then does four things:

1. It profiles every column: number, category, date, ID, or free text?
   You then choose what to predict in one of three ways: **let the engine decide** (it finds the outcome column, or chooses to
   look for groups when there isn't one), **pick the target yourself**, or **skip prediction** and just find groups.
2. It decides the machine-learning task and model with a transparent rule engine.
3. It generates notebook-style Python cells and runs them live.
4. It explains every step and every result.

The app is built for beginners. Every decision the engine makes comes with a three-part explanation:

> 🔍 **What we found:** `customer_id` has a different value in 100% of rows - it looks like an ID.
> 💡 **Why it matters:** An ID only names a row; it says nothing about the outcome. A model would just memorise it.
> 🎯 **What we're doing now:** We drop `customer_id`, so that the model learns real patterns instead of row numbers.

### How every step reads

Every **notebook cell** goes further and explains itself in six parts, always in this order:

1. 🔍 **What we found before**: the evidence that led to this step.
2. 💡 **Why it matters.**
3. 🎯 **What we're going to do**, and what that achieves.
4. 🛠 **What this code does**: the code below, in plain words.
5. **The code itself**, then its printed output and charts.
6. 📌 **What we found after running this**: written *by the code* (with `note()`) from the actual results, so it can't contradict the output.

A **Beginner / Expert** switch in the sidebar shows or hides these explanations. The whole notebook can also be downloaded as a `.ipynb` file, with the explanations included as markdown cells.

## Run it

```bash
brew install libomp                      # macOS only: LightGBM and XGBoost need this OpenMP runtime
pip install -r requirements.txt          # or requirements-extras.txt to also get XGBoost (~200 MB)
streamlit run app.py
```

- `requirements.txt` already includes **LightGBM** and **imbalanced-learn** (over/undersampling). `requirements-extras.txt` adds **XGBoost**.
- **On a Mac without `libomp`** you would see `Library not loaded: @rpath/libomp.dylib`. The app does not stop: it skips
  LightGBM / XGBoost and explains why in the decision trace. Run `brew install libomp` and **restart the app**, and they are included again.
- Libraries are checked once when the app starts, so restart it after installing anything.

## What's included

| Topic | What the app does |
|---|---|
| Boosting | `AdaBoost`, `HistGradientBoosting` and **LightGBM** are compared by default; **XGBoost** joins automatically when installed |
| Class imbalance | Macro F1 instead of accuracy, `class_weight="balanced"`, a tuned decision threshold, **and** a comparison of **random oversampling, random undersampling and SMOTE** (via `imblearn`), applied only inside the training folds so the test score stays honest |
| Missing or broken library | That model or step is skipped, never crashed, and the trace says what to install |

## Project layout

| File | What it does |
|---|---|
| `app.py` | Streamlit page layout only |
| `automl/profiling.py` | Gives each column a *role* (`numeric`, `categorical`, `high_card_categorical`, `datetime`, `id`, `constant`, `text`) and a reason for it |
| `automl/decision.py` | The decision engine: quality gates → task → model → sampling. Every choice is recorded as a `Step` |
| `automl/narrative.py` | All the beginner wording: `TEMPLATES` (found / why / action), per-cell stories, `GLOSSARY`, and `interpret()`, which turns metrics into sentences |
| `automl/codegen.py` | Turns a decision into code cells, and exports them to a Jupyter notebook |
| `automl/runner.py` | Runs the cells in one shared namespace and captures printed output, figures and errors |

## The decision tree

**New to this?** Open [docs/roadmap.html](docs/roadmap.html) in a browser: a one-page map of every step and condition, with example routes and ready-to-paste code for techniques the app doesn't automate.

This is a simplified view. The full specification, with every threshold, model setting and search space, is in [docs/DECISION_TREE.md](docs/DECISION_TREE.md).

```
Upload
  │
  ├─ Quality gates ── < 30 rows / < 2 columns / > 60% empty ──▶ stop and explain why
  │
  ├─ Target suggestion ── a column named like an outcome (status, label, churn…) is pre-selected
  │
  ├─ Column clean-up
  │    ID-like, constant, free-text, > 40% empty  ──▶ drop
  │    dates                                       ──▶ year / month / weekday
  │    year + month + day columns                  ──▶ combined, weekday added, impossible dates flagged
  │    exact duplicate rows                        ──▶ drop
  │    rows identical apart from the ID            ──▶ keep, but also score on "unseen" test rows
  │    remaining gaps                              ──▶ median / 'missing' category
  │
  ├─ No target ─▶ CLUSTERING (any outcome-like column is left out of the inputs)
  │                 ≤ 10k rows: KMeans        > 10k rows: MiniBatchKMeans
  │                 k chosen by best silhouette score (k = 2…8, on a sample)
  │
  └─ Target chosen
       ├─ numeric and > 15 distinct values ─▶ REGRESSION
       │     skewed and ≥ 0  ─▶ learn log(target)
       │     < 1k rows       ─▶ LinearRegression (LassoCV if > 30 features, RidgeCV if two features correlate > 0.9)
       │     1k–100k rows    ─▶ RandomForestRegressor
       │     > 100k rows     ─▶ HistGradientBoostingRegressor
       │
       └─ otherwise ─▶ CLASSIFICATION (binary / multiclass)
             classes seen once        ─▶ dropped
             smallest/largest < 0.25  ─▶ class_weight='balanced', judged by macro F1
             < 1k rows                ─▶ LogisticRegression
             1k–100k rows             ─▶ RandomForestClassifier
             > 100k rows              ─▶ HistGradientBoostingClassifier
```

Every supervised run also does the following:
- a **key drivers** table: the outcome rate or average for each group of each column, e.g. "cancel rate by segment"
- a stratified train/test split, and a **baseline** model to beat
- a **model comparison**: the rule-based pick above becomes one of three candidates (linear, decision tree, forest or boosting), scored by 3-fold cross-validation on the training rows, and the winner is used
- train-vs-test scores, so overfitting shows up
- for binary targets, a **threshold tuned for the rarer class**, using out-of-fold predictions, never the test set
- permutation feature importance on the original column names

Large files are sampled so that the app stays fast: 50k rows for random forests, 200k for linear and boosting models, and 100k for clustering.

## Course topics covered

| Topic | Where it appears in the generated notebook |
|---|---|
| Linear regression (OLS via `statsmodels`, `LinearRegression`) | **VIF** (multicollinearity, auto-drops VIF > 10) → **OLS inference**: β₀ and β coefficients, standard errors, t, p-values, 95% CIs, R² / adjusted R², F-test → **assumption checks**: residuals vs fitted, Q-Q plot, RESET (linearity), Breusch-Pagan (homoscedasticity), Jarque-Bera (normality), Durbin-Watson (independence) |
| Logistic regression | statsmodels `Logit`: odds ratios with CIs and p-values, a perfect-separation warning, ROC-AUC curve, precision/recall threshold tuning |
| Decision trees | a candidate model (tuned `max_depth`, `min_samples_leaf`, `ccp_alpha` pruning), plus a readable 3-level `plot_tree` with Gini impurity in real units |
| Bagging | `RandomForest` with out-of-bag score |
| Boosting | `AdaBoost`, `HistGradientBoosting`, `LightGBM` and, when installed, `XGBoost` (tuned over learning rate, depth and α / λ regularisation) |
| Tuning and validation | `cross_val_score` model comparison, `RandomizedSearchCV` on the winner, all preprocessing inside a `Pipeline` to prevent leakage |
| Class imbalance and cost-sensitive use | macro F1 whenever classes are uneven; for strong imbalance, `class_weight="balanced"` **and** a resampling comparison (random over-/under-sampling, SMOTE, inside the CV folds via `imblearn.Pipeline`); threshold chosen from out-of-fold predictions; cost-based threshold code in the roadmap |
| K-Means | scaled features, elbow (inertia) + silhouette to choose k, centroids on a PCA map, plain-English cluster profiles |
| Hierarchical clustering and PCA | ward dendrogram, `AgglomerativeClustering` vs K-Means agreement (adjusted Rand); PCA explained variance and loadings |

## Worked example

[`examples/inn_hotels/`](examples/inn_hotels/) is a full project on 36k hotel bookings. It includes a hand-checked, fully explained notebook (EDA, statsmodels, pruned trees, ensembles, business recommendations) and a comparison with what AutoML produces.

**Presenting this project?** [docs/guide.md](docs/guide.md) is an interview guide with a 60-second pitch, the architecture, feature deep dives with measured results, likely questions with answers, and a 5-minute demo script.

## Adding a new explanation or rule

1. Add a template to `TEMPLATES` in `automl/narrative.py`, with `found`, `why` and `action`.
2. In `automl/decision.py`, append `story("your_key", ...)` to `d.steps` where the rule fires.
3. If the rule changes the code, read the new `Decision` field in `automl/codegen.py`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The tests check column roles and every decision branch. They also **execute the generated code** on synthetic data that includes IDs, dates, gaps and mixed types, and they assert that the model beats the baseline.
