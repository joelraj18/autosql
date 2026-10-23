# AutoML Explorer: decision tree specification

This page documents **exactly** how the engine decides what to do with an uploaded file. Every threshold below is a constant in the code:

- `automl/profiling.py`: column roles, target suggestion, split dates, duplicates
- `automl/decision.py`: quality gates, task, rule-of-thumb model, sampling, model constructors (`MODELS`)
- `automl/codegen.py`: the notebook cells each branch generates, tuning search spaces (`SEARCH_SPACES`)
- `automl/narrative.py`: wording thresholds used in the plain-English summary (`interpret`)

If you change a constant in the code, update this page too.

## Stage P: Profiling (runs once per file, cached)

### P1. Column roles (`_profile_column`), checked in this order, first match wins
| # | Condition | Role | What happens to it |
|---|---|---|---|
| 1 | distinct non-null values ≤ 1 | `constant` | dropped |
| 2 | dtype is datetime64 | `datetime` | expanded to `_year`, `_month`, `_weekday` |
| 3 | dtype is bool | `categorical` | one-hot |
| 4 | numeric **and** integer **and** unique ratio > 0.95 **and** rows ≥ 30 **and** (values are consecutive *or* the name matches the ID pattern¹) | `id` | dropped |
| 5 | any other numeric | `numeric` | median-imputed and standardised |
| 6 | text: ≥ 90% of a 200-row sample parses as dates, and ≤ 50% parses as plain numbers | `datetime` | expanded |
| 7 | text: average length > 30 characters **and** > 50% of values contain a space (2,000-row sample) | `text` | dropped |
| 8 | text: unique ratio > 0.95 **and** rows ≥ 30 | `id` | dropped |
| 9 | text: distinct values > 50 | `high_card_categorical` | one-hot, capped at 20 columns |
| 10 | otherwise | `categorical` | one-hot, capped at 20 columns |

¹ ID name pattern: `…_id`, `…_uuid`, `…_guid`, `…_key`, `…_index`, `id_…`, `id`, `index`, `Unnamed: 0` (case-insensitive).
The unique ratio is distinct values ÷ non-null values.

### P2. Dataset-level facts
- **Exact duplicates:** rows identical in every column (their share decides what happens; see Stage 0b).
- **Look-alike rows:** rows identical once the `id` columns are removed, minus the exact duplicates.
- **Missing fraction:** the share of all cells that are empty.

### P3. Choosing the target: three modes (`resolve_target` in `automl/decision.py`)
| Mode in the app | What happens | First line of the trace after the overview |
|---|---|---|
| 🤖 **Let the engine decide** (default) | uses the suggestion below; **if there is none, the engine chooses clustering** | `target_auto_found` or `target_auto_none` |
| 🎯 **I'll pick the target** | your column always wins; the trace notes when the engine would have picked a different one | `target_manual` |
| 🔍 **No target: just find groups** | clustering; a column *named* like an outcome is still left out of the inputs | `target_none` |

A manually picked target that can't be predicted (free text, a date, a single value) still stops with an explanation.

### P3b. Target suggestion (`suggest_target`), used by "Let the engine decide"
1. Usable columns are those with role `categorical` or `numeric` **and** 2–15 distinct values.
2. If a usable column, **or a numeric column with any number of values**, has a name matching `status | target | label | class | churn | outcome | default | fraud | cancel | survived | result | response | y`,
   the **last** such column is chosen. Reason: "its name looks like an outcome…".
3. Otherwise, if the **last column** is usable and categorical, it is chosen.
4. Otherwise no suggestion is made, and the default is clustering.

### P4. Split dates (`find_date_parts`)
- Applies to numeric columns named `<prefix>[_]year` that also have `<prefix>[_]month` and `<prefix>[_]date` or `<prefix>[_]day`.
- They are combined into a real date, and impossible combinations (e.g. 29 Feb 2018) are counted.

---

## Stage 0: Quality gates (any failure → **HALT**, with an explanation)
| Gate | Threshold |
|---|---|
| Too few rows | rows < **30** |
| Too few columns | columns < **2** |
| Too empty | > **60%** of all cells missing |
| Bad target | target role is `text`, `datetime`, `constant` or `id` (a different value in almost every row) |
| No usable inputs (checked after clean-up) | 0 features, or **< 2** features when there is no target |
| *(warning, not a stop)* Small data | fewer than **200 rows** → a story explains that one test row moves the score a lot, so cross-validated scores are steadier |
| Single class (classification, after removing rare classes) | fewer than 2 classes left |

## Stage 0b: Clean-up (applied to every non-target column, in this order)
1. When **no target** is chosen, the suggested target column is **excluded** from the clustering inputs, but **only if it was
   suggested because of its name** (P3 step 2). Being the last column is too weak a reason to drop a real feature.
2. Role `id` → drop.
3. Role `constant` → drop.
4. Role `text` → drop.
5. Missing in **> 40%** of rows → drop.
6. Role `datetime` → `_year`, `_month`, `_weekday` (0 = Monday).
7. Role `numeric` → numeric feature.
8. Role `categorical` or `high_card_categorical` → categorical feature.

Then:
- **Exact duplicates:** if they are **≤ 5%** of rows they look like accidental double entries and are **dropped**.
  Above 5% they are real repeats (e.g. many identical bookings, or category-only data) and are **kept**. Supervised runs then also
  report the unseen-row score.
- **Look-alike rows** are kept, but flagged (supervised only), and an extra score on unseen rows is reported.
- **Split dates** (P4) add `<prefix>_weekday`.
- **±Infinity** (usually a division by zero upstream) is counted while profiling and replaced by a missing value in the prepare cell.
- Remaining gaps are filled with the **median** (numbers) or a **"missing"** category.

---

## Stage 1: Task
```
target chosen?
├─ NO  → CLUSTERING (Branch A)
└─ YES → drop rows where the target is empty
         target numeric (not bool) AND distinct values > 15 ?
         ├─ YES → REGRESSION (Branch B1)
         └─ NO  → CLASSIFICATION (Branch B2)
```

### Branch A: Clustering
| Setting | Rule |
|---|---|
| Algorithm | rows > 10,000 → `MiniBatchKMeans(n_init=3, batch_size=2048)`; otherwise `KMeans(n_init=10)`; both use k-means++ and Euclidean distance |
| k range | 2 … max(2, min(8, rows ÷ 10)) |
| Sampling | rows > 100,000 → random sample of 100,000 |
| Choosing k | highest **silhouette** on a 5,000-row sample; inertia (elbow) is shown alongside |

Generated cells: setup → prepare → EDA → scale/one-hot → **PCA** (components for 80% of the variance, plus loadings) → **choose k** (elbow and
silhouette) → **fit + profiles** (PCA map with centroids; per-cluster means as z-scores) → **hierarchical**.

The hierarchical cell uses ward linkage on 1,500 rows and draws a dendrogram cut at k. It compares `AgglomerativeClustering` with K-Means by
silhouette and adjusted Rand index: ARI > 0.6 means "robust".

### Branch B1: Regression
| Decision | Rule |
|---|---|
| Log target | \|skew\| > 1 **and** min ≥ 0 → `TransformedTargetRegressor(log1p / expm1)` for every candidate; OLS on log1p(y) |
| Rule-of-thumb model, < 1,000 rows | inputs > 30 → `LassoCV(cv=5)`; otherwise max \|r\| between numeric inputs > 0.9 (5k-row sample, first 50 columns) → `RidgeCV(α = 10⁻³…10³, 13 values)`; otherwise `LinearRegression` |
| Rule-of-thumb model, 1,000–100,000 rows | `RandomForestRegressor` |
| Rule-of-thumb model, > 100,000 rows | `HistGradientBoostingRegressor` |
| Candidates actually compared | [the linear model above, or `LinearRegression`], `DecisionTreeRegressor`, `RandomForestRegressor`, `AdaBoostRegressor`, `HistGradientBoostingRegressor`, plus `XGBRegressor` and `LGBMRegressor` **when installed** |
| Metric | R² (and MAE reported) |

### Branch B2: Classification
| Decision | Rule |
|---|---|
| Rare classes | classes with **< 2** rows are dropped |
| Subtype | 2 classes → binary; otherwise multiclass |
| Stratified split | if ⌊rows × 0.2⌋ ≥ number of classes |
| Imbalance | smallest class share ÷ largest < **0.25** → `class_weight="balanced"` (on LR, tree, RF, HGB and LightGBM; AdaBoost and XGBoost have none), and the metric becomes **Macro F1**. If `imbalanced-learn` is installed, a **resampling** step also compares oversampling, undersampling and SMOTE |
| Metric | **Macro F1** if the smallest class is under 40% × (2 ÷ number of classes) (e.g. under 40% for binary, under 20% with 4 classes) or the data are imbalanced; otherwise **Accuracy**. Reason: accuracy flatters a model that always predicts the big class |
| Positive class (binary only) | the **rarer** class. It drives the insights, logit odds ratios, ROC and threshold tuning |
| Rule-of-thumb model | < 1,000 rows → `LogisticRegression`; 1,000–100,000 → `RandomForestClassifier`; > 100,000 → `HistGradientBoostingClassifier` |
| Candidates actually compared | `LogisticRegression`, `DecisionTreeClassifier`, `RandomForestClassifier`, `AdaBoostClassifier`, `HistGradientBoostingClassifier`, plus `XGBClassifier` (wrapped in `LabelEncoded`, because XGBoost needs classes numbered 0, 1, 2…) and `LGBMClassifier` **when installed** |
| Optional libraries | "Installed" means the library **imports without error**, not just that its files exist. On macOS without `libomp`, `import lightgbm` raises `OSError`: the engine skips it with the `boosting_libs_broken` step ("run `brew install libomp`"). The exported notebook also adds each XGBoost/LightGBM model inside its own `try`, so a notebook built on one computer still runs on another computer where the library won't load |

> The rule-of-thumb model is the engine's *first guess*. It is explained in the decision trace and sets the sampling limit, but the
> **model that is actually used is the cross-validation winner** among the five candidates.

---

## Stage 3: Speed (supervised)
The row cap depends on the family of the rule-of-thumb model:
- tree (random forest) → **50,000**
- linear, boosting or HGB → **100,000**

Classification samples are stratified.

---

## Fixed model specifications (`MODELS` in `automl/decision.py`)
| Model | Constructor |
|---|---|
| LogisticRegression | `max_iter=1000` (+ `class_weight` if imbalanced) |
| DecisionTree (Clf/Reg) | `max_depth=8, min_samples_leaf=20, random_state=42` |
| RandomForest (Clf/Reg) | `n_estimators=200, min_samples_leaf=2, oob_score=True, n_jobs=-1, random_state=42` |
| AdaBoost (Clf/Reg) | `n_estimators=100, random_state=42` |
| XGBoost (Clf/Reg), optional | `n_estimators=300, learning_rate=0.1, max_depth=6, subsample=0.8, colsample_bytree=0.8, n_jobs=-1, random_state=42` |
| LightGBM (Clf/Reg), optional | `n_estimators=300, learning_rate=0.05, num_leaves=31, subsample=0.8, subsample_freq=1, colsample_bytree=0.8, n_jobs=-1, verbose=-1, random_state=42` (+ `class_weight`) |
| HistGradientBoosting (Clf/Reg) | `random_state=42` (defaults otherwise) |
| LinearRegression / RidgeCV / LassoCV | see B1 |
| KMeans / MiniBatchKMeans | see Branch A |

## Preprocessing (inside every pipeline, so nothing leaks)
- **Numbers:** `SimpleImputer(median)` → `StandardScaler`.
- **Categories:** `OneHotEncoder(handle_unknown="infrequent_if_exist", max_categories=20, sparse_output=False)`.
- The readable-rules tree uses the same steps **without** scaling, so its thresholds are in real units.

## Supervised evaluation pipeline (generated cells, in order)
| # | Cell | Specification |
|---|---|---|
| 1 | Setup | `metrics` dictionary; `note()` |
| 2 | Prepare | the clean-up above, plus the sample |
| 3 | EDA | 5,000-row sample; histograms (≤ 12 columns), correlation heatmap (≤ 15), target plot |
| 4 | Insights (regression, or binary classification) | outcome by group: categories as-is, numbers with > 10 distinct values cut into 5 quantile bands; groups under 30 rows hidden; top 4 columns by spread |
| 5 | Split | `test_size=0.2, random_state=42`, stratified if allowed |
| 6 | Preprocess | fitted on training rows |
| 7 | Baseline | `DummyRegressor(mean)` or `DummyClassifier(most_frequent)` |
| 8 (regression) | VIF | 20,000-row statsmodels sample; reference category = mode; top-10 categories (rest = "other"); VIF on 5,000 rows; drop the max while VIF > 10 |
| 9 (regression) | OLS | `sm.OLS` (log target if skewed); R², adjusted R², F p-value, coefficients with 95% CI and p-values; test R² |
| 10 (regression) | Assumptions | RESET (power 2, F), Breusch-Pagan, Jarque-Bera: p ≥ 0.05 = OK; Durbin-Watson in 1.5–2.5 = OK; residual, histogram and Q-Q plots |
| 8 (binary) | Logit | `sm.Logit(maxiter=300)` on 20,000 rows; odds ratios with 95% CI and p-values; warns if it doesn't converge (perfect separation) |
| – | Compare | 10,000-row training sample, `cross_val_score(cv=3)` per candidate; winner = highest mean |
| – | Resample (imbalanced + `imblearn`) | the winner under class weights vs `RandomOverSampler`, `RandomUnderSampler`, `SMOTE` (class weights switched off for the samplers), 3-fold CV, using imblearn's `Pipeline`, so resampling happens **only inside training folds**; if a sampler wins, `make_pipe` is redefined so tuning, training and threshold all use it |
| – | Tune | `RandomizedSearchCV(n_iter=min(8, grid size), cv=3)` on the same sample, trials run **one after another** (each model already uses all cores; parallel trials on top deadlocked); spaces below. **New settings are adopted only if they beat the defaults' CV score**; otherwise the defaults are kept |
| – | Train | refit the tuned winner on **all** training rows; train/test gap ≤ 0.05 "generalises", ≤ 0.15 "mild overfitting", > 0.15 "overfitting"; test set < 100 rows → noise warning; test score ≥ 0.99 with a baseline under 0.9 → **leakage warning** (check for a column that gives away the answer); OOB score for forests; unseen-row score when look-alike rows exist; MAE and predicted-vs-actual (regression); report, confusion matrix and ROC-AUC (classification) |
| – | Threshold (binary) | out-of-fold probabilities (`cv=3`, ≤ 20,000 training rows); threshold = argmax F1 on the precision-recall curve; test table at 0.50 vs tuned |
| – | Rules | `DecisionTree(max_depth=3)` on unscaled inputs; `plot_tree`; the root split phrased as a question (a one-hot column becomes "is `plan` = basic?"); score ≤ baseline + 0.01 → "no better than the baseline"; within 0.005 of the winner (or better) → "as good as the full model: these rules are enough"; within 0.05 → "most"; else "part" |
| – | Explain | `permutation_importance(n_repeats=3)` on ≤ 2,000 test rows, original columns |

### Tuning search spaces (`SEARCH_SPACES` in `automl/codegen.py`)
| Model | Space |
|---|---|
| LogisticRegression | C ∈ {0.01, 0.1, 1, 10, 100} |
| DecisionTree | max_depth ∈ {3, 5, 8, 12, None}, min_samples_leaf ∈ {1, 5, 20, 50}, ccp_alpha ∈ {0, 1e-4, 1e-3} |
| RandomForest | n_estimators ∈ {100, 200, 400}, max_features ∈ {sqrt, 0.5, 1.0}, min_samples_leaf ∈ {1, 2, 5} |
| AdaBoost | n_estimators ∈ {50, 100, 200}, learning_rate ∈ {0.05, 0.1, 0.5, 1.0} |
| HistGradientBoosting | learning_rate ∈ {0.03, 0.05, 0.1, 0.2}, max_leaf_nodes ∈ {15, 31, 63}, l2_regularization ∈ {0, 0.1, 1} |
| LinearRegression / RidgeCV / LassoCV | not tuned (they fit their own α, or have none) |

## Interpretation thresholds used in the plain-English text
| Measure | Threshold and meaning |
|---|---|
| Model vs baseline | gain ≤ 0.01: "not better than guessing"; < 0.10: "small but real"; otherwise "clearly learning" |
| Train − test gap | ≤ 0.05 generalises; ≤ 0.15 mild overfitting; > 0.15 overfitting (same thresholds in the train cell and the summary) |
| Tuned threshold | if it lowers test F1, the note recommends keeping 0.50 |
| Silhouette | ≥ 0.5 strong; ≥ 0.25 reasonable; otherwise weak |
| ROC-AUC | ≥ 0.9 "very well"; ≥ 0.75 "reasonably"; otherwise "weakly" |
| Unseen-row gap | > 0.02 → "duplicates flatter the score" |
| VIF | < 5 safe; 5–10 "read with care"; > 10 dropped |
| p-values | < 0.05 significant |

## Worked trace: INN Hotels (`booking_status`)
- **Profile:** 36,275 rows; `Booking_ID` is an ID (100% unique text); 10,275 look-alike rows; arrival year/month/date detected as a split date, with 37 impossible dates.
- **Target:** `booking_status` was suggested by name.
- **Task:** 2 classes → binary; share ratio 0.33 ÷ 0.67 = 0.49 is not < 0.25, so no class weights, but 32.8% < 40%, so the metric is
  **Macro F1** (baseline 0.402); the positive class is `Canceled`.
- **Model:** the rule-of-thumb pick is RandomForest (1,000–100,000 rows), so the cap is 50,000 and no sampling is needed.
- **Cells:** logit odds → compare 7 (with XGBoost and LightGBM installed), by CV macro F1: LightGBM 0.862 > random forest 0.858 >
  XGBoost 0.857 > gradient boosting 0.855 > tree 0.816 > AdaBoost 0.776 > logistic 0.761 → tuning tried 8 combinations, none beat
  the defaults (0.859 vs 0.862), so the defaults are kept → test macro F1 0.884, ROC-AUC 0.956, unseen rows 0.841 → threshold 0.44
  lifts recall for Canceled 0.81 → 0.82, but its F1 dips 0.841 → 0.837, so the note recommends keeping 0.50.

## Known limits / quirks worth knowing
- `n_features > 30` (the Lasso rule) counts input columns *before* one-hot encoding.
- The collinearity check (Ridge rule) only looks at original numeric columns, not derived date features.
- Multiclass targets get no insights, logit or threshold cells; only the comparison, report and confusion matrix.
- Column and class names enter the generated code only as quoted values (`repr`), never pasted into code text, so names with quotes or braces are safe.
- Runs in the app take turns (one lock per server process), because matplotlib's chart list is shared by everyone using the same server.
- Hierarchical clustering is limited to 1,500 rows, and the statsmodels cells to 20,000 rows (sampled).
- Plain SMOTE treats one-hot columns as numbers. For data with many categories, SMOTENC is better; the beginner roadmap has
  the code.
- Early stopping with a validation-loss curve, `BaggingClassifier`, and cost-based thresholds are not automated; the roadmap
  ("Going further") gives ready-to-paste code.
