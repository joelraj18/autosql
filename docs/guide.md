# Guide: AutoML Explorer

How to present this project in a data analyst or product analytics interview.

**Where the numbers come from:** every number in this guide was measured on 3 October 2026, on this commit's code, in a
4-vCPU Linux container (Python 3.11, scikit-learn 1.9.1, pandas 3.0.6, Streamlit 1.64, LightGBM 4.7.0, XGBoost 3.2.0,
imbalanced-learn 0.14.2). Timings will differ on a laptop. Anything I could not measure is marked **not verified**. I also clicked through the demo script (section 9) in a real
browser on the hotel data: every label and number it quotes appeared, with 0 errors.

---

## 1. The 60-second pitch

> "AutoML Explorer is a Streamlit app. You upload a CSV or Excel file, and it decides what kind of analysis fits the data:
> classification, regression, or clustering. It writes the Python for that analysis as notebook cells, runs them live, and
> explains every step in plain English. Every decision comes from a transparent rule, not a black box, and every result is
> written by the code that produced it, so the explanation can't contradict the output.
>
> It's for analysts and students who know some pandas but not yet which model, metric or validation scheme to pick, and why.
>
> The problem it solves: most AutoML tools hand you a score with no reasoning, and most tutorials teach steps without the
> judgement behind them. This one shows the judgement.
>
> The stack is Python, pandas, scikit-learn, statsmodels, LightGBM / XGBoost, imbalanced-learn, matplotlib and Streamlit,
> tested with pytest.
>
> On a real dataset of 36,275 hotel bookings, it finds the target on its own. It also spots 10,275 near-duplicate rows hidden
> behind an ID column, and predicts cancellations with a test macro F1 of 0.884 and a ROC-AUC of 0.956, against a baseline of
> 0.402. The full run takes about 31 seconds."

**Three things to emphasise:**

1. **Honest evaluation.** Everything that learns from data happens on training rows only, inside a `Pipeline`: imputation,
   scaling, resampling, tuning and threshold choice. The test set is touched once. Near-duplicate rows get a separate "unseen
   rows" score: 0.841 vs 0.884 on the hotel data.
2. **Explainable decisions.** Every rule writes a three-part story: what we found, why it matters, what we do. Every generated
   cell then writes its findings from the real numbers with `note()`.
3. **Robustness as a feature.** I audited the app on 24 awkward datasets (70 runs) and fixed every crash with a regression
   test. Examples: column names with quotes, infinity values, ID columns as the target, and a native library that won't load
   on macOS. There are 45 tests, and they execute the generated code rather than just checking that it exists.

---

## 2. Architecture

| Layer | What | Why |
|---|---|---|
| UI | `app.py` (221 lines, Streamlit) | Page layout only, no logic. Caches the load, profile and decision (`app.py:36`, `app.py:50`, `app.py:55`) so that a widget click doesn't redo the work. Run results are kept in `st.session_state`. |
| Logic: profiling | `automl/profiling.py` | Gives every column a *role* (numeric, categorical, high-cardinality, datetime, id, constant, text), not just a dtype (`_profile_column`, `automl/profiling.py:94`). |
| Logic: decisions | `automl/decision.py` | A rule engine: quality gates → task → model → sampling. Each choice is appended as a `Step` (`decide`, `automl/decision.py:172`). |
| Logic: wording | `automl/narrative.py` | All the plain-English templates, the glossary, and `interpret()` (`automl/narrative.py:333`), which turns metrics into sentences. |
| Logic: code generation | `automl/codegen.py` | Turns a `Decision` into notebook cells, each with its own story (`build_cells`, `automl/codegen.py:1302`). Exports to `.ipynb` (`automl/codegen.py:1324`). |
| Execution | `automl/runner.py` | Runs the cells in one shared namespace, like a kernel. Captures stdout, figures and notes; skips later cells after an error (`automl/runner.py:54`). |
| Data | Uploaded CSV / Excel; sample: `examples/inn_hotels/INNHotelsGroup.csv` | No database. The file is read into a pandas DataFrame. |
| Storage | None (in memory) | Nothing is persisted on a server. The user downloads the notebook as `.ipynb`. |
| Tests | `tests/` (4 files, 45 tests, pytest) | They execute the generated code end to end on synthetic and real data. |
| Deployment | Local only: `streamlit run app.py` | There is no Dockerfile, CI workflow or hosted deployment in the repo. |

**Key design choice: generate code, don't just call a library.**
The engine never trains a model directly. It writes plain Python cells and then runs them. I chose this for three reasons:

- **What you see is what ran.** The app shows exactly the code it executed, and the notebook export contains the same code.
- **The user keeps the work.** They can open the `.ipynb` in Jupyter and keep going, so the tool is a starting point, not a
  dead end.
- **Explanations stay true.** The 📌 findings are produced by `note()` calls inside the generated code. They are computed from
  the same variables the output prints, so they can't drift from the output.

The cost is that generated code is a string, so I had to make it safe against odd column names. Section 3 has that bug story.

---

## 3. What was built and changed

All dates are 1–2 October 2026. The history has 14 commits across 5 merged pull requests.

| Round | Commit(s) | What changed |
|---|---|---|
| 0. Starting point | `164028c` | A single-file Streamlit AutoML app (5 files, 502 lines). |
| 1. Restructure (PR 1) | `2260e3e` | Split into the `automl` package (profiling / decision / narrative / codegen / runner). Added the three-part explanations, the decision trace, `.ipynb` export, caching and sampling for large files, and 21 tests. |
| 2. Real data and self-explaining cells (PR 2) | `65f6d30`, `1b383d4` | Added the hotel bookings worked example. The engine learned to suggest the target, find look-alike rows, and combine year / month / day columns. New cells: key drivers, model comparison and threshold tuning. Every cell gained the five-part story and `note()` findings. Added statistics: VIF, OLS inference, assumption tests, logit odds ratios, PCA and hierarchical clustering. 28 tests. |
| 3. Specification, audit and libraries (PR 3) | `80bd241`, `6f20071`, `f0762c6` | `docs/DECISION_TREE.md` (every rule and threshold). An audit on 10 datasets fixed 7 bugs. Added XGBoost and LightGBM candidates, the resampling comparison, and `docs/roadmap.html`. Added the three-way target choice: let the engine decide, pick it yourself, or no target. 38 tests. |
| 4. Thorough audit (PR 4) | `6b05b52` | 24 awkward datasets × target modes (70 runs), 5 exported notebooks run in Jupyter, and browser checks. Fixed code injection, infinity, ID targets, empty groups and concurrent runs; added a leakage warning. 43 tests. |
| 5. macOS crash and docs (PR 5) | `c73f052`, `93db59f` | Fixed a crash when LightGBM is installed but can't load (missing `libomp` on macOS). README: the six-part step format, setup, and what's included. 45 tests. |

### Notable fixes

| Problem | Fix | Result |
|---|---|---|
| Column or class names were pasted into generated f-strings. A target named `target {x}` raised `NameError`, and a quote broke the string. | Names now enter generated code only through `repr()` or runtime variables (`fill`, `automl/codegen.py:57`). | Test `test_column_names_never_break_the_generated_code`. |
| ±infinity in any numeric column crashed every mode. | Infinity is counted while profiling (`automl/profiling.py:176`), explained, and turned into a missing value in the prepare cell (`automl/codegen.py:110`). | Test `test_infinity_is_treated_as_missing`. |
| Duplicates hidden by an ID: the hand-made notebook reported 0 duplicates, because `Booking_ID` made every row unique. | Duplicates are checked with ID columns ignored (`automl/profiling.py:184`). Look-alike rows are kept, and the model is also scored on test rows with no twin in training (`automl/codegen.py:906`). | Finds 10,275 look-alike rows (28%). The test macro F1 on unseen rows is 0.841, vs 0.884 overall. |
| Dropping exact duplicates turned 300 category-only rows into 9. | Duplicates are dropped only when they are ≤ 5% of rows (`MAX_DUPLICATE_SHARE`, `automl/decision.py:35`); above that they are kept as real repeats. | Test `test_many_exact_duplicates_are_kept_not_dropped`. |
| Accuracy picked an "always no" model on 30/70 data. | Macro F1 is used whenever the minority class is under 40% (`automl/decision.py:348`). | The hotel data (32.8% cancelled) is judged by macro F1. |
| Tuning could adopt settings that were worse than the defaults. | New settings are kept only if they beat the default CV score (`automl/codegen.py:845`). | On the hotel data, tuning scored 0.859 vs 0.862 for the defaults, so the defaults were kept. |
| Tuning ran parallel trials on top of multi-core models, which stalled the run. | Trials run one after another; each model already uses every core (`automl/codegen.py:839`). | Test `test_tuning_never_makes_the_model_worse`. |
| Two users pressing Run at once could mix their charts, because matplotlib's figure list is global. | Runs take turns behind a lock (`automl/runner.py:45`). | Charts can no longer cross over between users. |
| macOS: `OSError: Library not loaded: @rpath/libomp.dylib` crashed the run. `find_spec` said LightGBM was installed. | `library_problem()` really imports the library once and caches the reason (`automl/decision.py:40`). The exported notebook wraps each optional model in its own `try` (`automl/codegen.py:668`). | Tests simulate the OSError. On the hotel data, the run continues without LightGBM and says `brew install libomp`. |
| The trace claimed SMOTE was applied when it never was (original app). | Resampling is now a real, measured step inside the CV folds (`automl/codegen.py:760`). | On the 5% fraud demo, random oversampling won with 0.820 vs 0.815 for class weights. |

---

## 4. Feature deep dives

### 4.1 Column profiling: "what is each column, really?"

**Business question:** before any analysis, which columns are measurements, which are labels, and which are noise (IDs, constants, free text)?

**How it works** (`_profile_column`, `automl/profiling.py:94`). These checks run in order, and the first match wins:

```text
n_unique <= 1                                   -> constant
datetime dtype, or text that parses as dates    -> datetime
integer, unique in >95% of rows, and (counts up 1,2,3 or named like an ID) -> id
text, avg length > 30 chars and mostly spaces   -> text
text, unique in >95% of rows                    -> id
> 50 categories                                 -> high_card_categorical
otherwise                                       -> numeric / categorical
```

**Why this method:** a dtype says how a value is *stored*; a role says how a model should *use* it. `customer_id` is an
integer, but feeding it to a model teaches it row numbers. Rules are cheap, deterministic and explainable, and every role
comes with a reason string. An ML type-detector would be harder to explain and to test.

**Limits:**
- An all-unique float column (a price) is kept as numeric, not as an ID (test `test_all_unique_float_is_a_quantity_not_an_id`).
- Text checks use a 2,000-row sample to stay fast, so a column whose format changes late in the file could be misread.

**Real numbers (hotel data):** 19 columns. `Booking_ID` is the only ID ("unique in 100% of rows"). There are 0 missing cells
and 0 exact duplicates, but 10,275 look-alike rows. `arrival_year` / `arrival_month` / `arrival_date` are recognised as one
date, with 37 impossible dates (29 February 2018). Profiling took 0.09 s.

**30-second demo:** upload the CSV and open **"Column profile - what role does each column play?"**. Point at
`Booking_ID → id` and its reason. *"The dtype is text; the role is ID, so it's dropped. That one rule is why the hand-made
notebook missed 28% look-alike rows."*

### 4.2 Target choice and task routing

**Business question:** what are we predicting, and is it a label, a number, or nothing (segmentation)?

**How it works:**
- `suggest_target` (`automl/profiling.py:130`) looks for a column *named* like an outcome:
  ```python
  re.compile(r"(status|target|label|class|churn|outcome|default|fraud|cancel|survived|result|response|^y$)", re.I)
  ```
  The column must also have 2–15 values. A numeric column with a name like that and many values also qualifies, and goes to
  regression. Otherwise, the last column is used if it is categorical.
- `resolve_target` (`automl/decision.py:136`) supports three modes: auto, manual, none.
- `decide` (`automl/decision.py:172`) routes:

  ```text
  no target                           -> clustering (KMeans; MiniBatchKMeans above 10k rows)
  numeric target with > 15 values     -> regression (log target if skew > 1 and all values >= 0)
  otherwise                           -> classification (binary / multiclass)
  model by size: < 1k rows linear, 1k-100k random forest, > 100k HistGradientBoosting
  ```

**Why:** the user can always override the engine. In manual mode, the trace says what the engine *would* have picked. Size
rules give a sensible starting model, and the model comparison (4.4) then checks it against the data.

**Limits:** dates as targets (forecasting) and free text stop with an explanation, not a guess. An ID column picked as the
target stops with a clear reason; that was a bug fix. Multiclass targets get no key-drivers, logit or threshold cells.

**Real numbers:** on the hotel data, auto mode picks `booking_status` because "its name looks like an outcome and it has only
2 values". With **no target**, the outcome-like column is removed from the clustering inputs, so it can't leak into the
segments (test `test_clustering_leaves_out_the_outcome_column`).

**30-second demo:** in step 2, switch between 🤖 / 🎯 / 🔍. *"Same file, three questions. In 'no target' mode it does not
cluster on the answer column, which an earlier version did."*

### 4.3 Key drivers: "what moves the outcome?"

**Business question:** which segments cancel most? This is the table a product manager acts on before any model exists.

**How it works** (`group_outcome`, `automl/codegen.py:285`). For each column, rows are grouped (numbers into 5 quantile bands)
and the outcome rate is computed per group. Groups under 30 rows are ignored. Columns are then ranked by the spread between
their best and worst group:

```python
values = pd.qcut(data[col], q=5, duplicates="drop")          # numbers -> 5 equal-sized bands
table = outcome.groupby(values, observed=True).agg(["mean", "size"])
table = table[table["size"] >= 30]                              # too-small groups are noise
spread = max(rate) - min(rate)                                  # rank columns by this
```

The same idea in SQL (an illustration only: the project uses pandas, not a database):

```sql
WITH banded AS (
  SELECT NTILE(5) OVER (ORDER BY lead_time) AS lead_band,
         lead_time,
         CASE WHEN booking_status = 'Canceled' THEN 1 ELSE 0 END AS canceled
  FROM bookings
)
SELECT lead_band, MIN(lead_time) AS from_days, MAX(lead_time) AS to_days,
       COUNT(*) AS bookings, AVG(canceled) AS cancel_rate
FROM banded
GROUP BY lead_band
HAVING COUNT(*) >= 30
ORDER BY lead_band;
```

**Why this method:** it is univariate and descriptive, and anyone can read it. It is the analyst's first cut, and it later
cross-checks the model's importances. Quantile bands keep the groups balanced; equal-width bands would leave the long tail
nearly empty.

**Limits:** these are correlations, not effects. Lead time and price are linked, so each looks stronger alone than together.
That is why the logit cell (4.6) adds "holding everything else fixed" estimates. If no group reaches 30 rows, the cell is
skipped with an explanation (a bug fix).

**Real numbers (measured):**

| Driver | Lowest group | Highest group | Overall |
|---|---|---|---|
| `lead_time` | 10.7% (0–11 days) | **72.3%** (151–443 days) | 32.8% |
| `no_of_weekend_nights` | 30.2% (0) | 85.3% (5) | |
| `no_of_special_requests` | 0.0% (3+) | 43.2% (0) | |
| `market_segment_type` | 0.0% (Complementary) | 36.5% (Online) | |

**30-second demo:** run the pipeline and scroll to **"Find the key drivers"**. *"Bookings made more than 151 days ahead
cancel 72% of the time, against 11% for last-minute ones. They're 20% of bookings but 44% of cancellations. That's the first
lever I'd show a revenue team."*

### 4.4 Model comparison with cross-validation

**Business question:** which kind of model fits this data, judged by evidence rather than habit?

**How it works** (`_compare_cell`, `automl/codegen.py:667`):
- Candidates: linear, decision tree, random forest, AdaBoost, HistGradientBoosting, plus LightGBM / XGBoost when they load.
- Each is scored with 3-fold CV on up to 10,000 training rows, with preprocessing inside the pipeline (`automl/codegen.py:722`):
  ```python
  cross_val_score(Pipeline([("prep", preprocessor), ("model", m)]), X_check, y_check, cv=3, scoring="f1_macro")
  ```
- The winner is tuned with `RandomizedSearchCV` (8 combinations, 3-fold, `automl/codegen.py:837`). Tuned settings are kept
  only if they beat the defaults.
- The tuned winner is retrained on all training rows and scored once on the test set.

**Metric choice** (`automl/decision.py:348`):

```text
Macro F1 = mean over classes of F1_c,   F1_c = 2 * precision_c * recall_c / (precision_c + recall_c)
use Macro F1 if  min class share < 0.40 * (2 / n_classes)   or classes are imbalanced (smallest/largest < 0.25)
else Accuracy;   regression uses R²
```

**Why:**
- **Three folds, not five or ten:** that is a speed trade-off for an interactive app. The test set stays as the final judge.
- **Macro F1:** with a 33% minority class, accuracy rewards "always say Not_Canceled". The baseline makes that visible.
- **Preprocessing inside the pipeline:** the imputer's medians and the scaler's means come from training folds only, so
  nothing leaks.

**Limits:**
- The comparison uses a 10,000-row sample, so CV scores have some noise. On the hotel data the top 4 are within 0.008 of each
  other.
- Each model gets one default configuration before tuning, so a model that needs tuning to shine may lose early.
- When a simpler model is within 0.01 of the winner, the app says so and suggests preferring it for explainability.

**Real numbers (hotel data, macro F1):**

| Model | 3-fold CV |
|---|---|
| LGBMClassifier | **0.862** |
| RandomForestClassifier | 0.858 |
| XGBClassifier | 0.857 |
| HistGradientBoostingClassifier | 0.855 |
| DecisionTreeClassifier | 0.816 |
| AdaBoostClassifier | 0.776 |
| LogisticRegression | 0.761 |
| *Baseline (most common class)* | *0.402* |

Tuning tried 8 of 243 combinations. The best scored 0.859, below the default's 0.862, so the defaults were kept.

Final results on the 7,255 test rows:

| Measure | Value |
|---|---|
| Macro F1, test | **0.884** |
| Macro F1, train | 0.889 (gap 0.005) |
| ROC-AUC | 0.956 |
| Accuracy | 0.90 |
| `Canceled`: precision / recall | 0.88 / 0.81 |

The rule of thumb picked a random forest; the data picked LightGBM.

Note: my environment had XGBoost installed (`requirements-extras.txt`). With `requirements.txt` only, there are 6
candidates; LightGBM still wins, since XGBoost only came third.

**30-second demo:** scroll to **"Compare candidate models"** and point at the bar chart with the dashed baseline line.
*"Seven models, scored on training folds only. The test set hasn't been touched yet. Four of them are within a point of
each other, so I'd happily ship the random forest if explainability mattered more."*

### 4.5 Class imbalance: weights, resampling and threshold

**Business question:** when the thing we want to catch is rare (fraud, churn, cancellations), how do we stop the model from
ignoring it, and where do we draw the line?

**How it works:**
- **Imbalance rule:** if smallest class / largest class < 0.25 (`automl/decision.py:33`), the engine uses
  `class_weight="balanced"`. If `imbalanced-learn` loads, it also adds a resampling cell (`automl/decision.py:352`).
- **Resampling comparison** (`automl/codegen.py:760`): class weights vs random oversampling vs random undersampling vs SMOTE,
  each inside an `imblearn` pipeline so that only the training folds are resampled (`automl/codegen.py:785`).
- **Threshold tuning** (`automl/codegen.py:971`): out-of-fold probabilities on training rows, then the F1-maximising
  threshold:
  ```python
  oof = cross_val_predict(clone(pipe), X_check, y_check, cv=3, method="predict_proba")[:, pos]
  precision, recall, thresholds = precision_recall_curve(y_check == positive, oof)
  threshold = thresholds[argmax(2 * precision * recall / (precision + recall))]
  ```
  The test table then compares 0.50 with the tuned threshold.

**Why:**
- **Resampling inside the folds:** resampling before the split copies rare rows into the test folds and inflates the score.
- **The threshold from training folds:** picking it on the test set is leakage.
- **F1 as the default objective, framed as a business choice:** the app says "lower it if a missed case costs more than a
  false alarm".

**Limits:**
- SMOTE treats one-hot columns as numbers. SMOTENC would be better for categorical data; the roadmap has the code, but it is
  not automated.
- The F1-optimal threshold doesn't always carry over to the test set. The app says so when it doesn't (see the numbers below).
- A cost-based threshold (cost per missed case vs cost per false alarm) is **not built**.

**Real numbers:**

*Hotel data (32.8% cancelled, no resampling):* the ratio is 0.49, above the 0.25 cut, so there are no class weights and no
resampling cell. Threshold:

| | Precision | Recall | F1 |
|---|---|---|---|
| Default 0.50 | 0.877 | 0.808 | **0.841** |
| Tuned 0.44 | 0.854 | 0.821 | 0.837 |

The tuned threshold caught more cancellations but lost a little F1 on test, and the app tells the user to keep 0.50 unless
recall matters more.

*Synthetic fraud data (3,000 rows, 5% positive, `make_classification`):*

| Option | 3-fold CV macro F1 |
|---|---|
| Random oversampling | **0.820** (chosen) |
| Class weights | 0.815 |
| SMOTE | 0.808 |
| Random undersampling | 0.602 |

The test macro F1 was 0.898 (baseline 0.486) and the ROC-AUC 0.967. Undersampling threw away about 95% of the majority class
and lost badly. Train macro F1 was 1.000 vs 0.898 on test, which the app flags as a 0.10 gap (mild overfitting).

**30-second demo:** upload an imbalanced file and open **"Handle class imbalance (resampling)"**. *"Four ways to handle the
rare class, compared fairly. Undersampling throws away 95% of the data and it shows. Then the threshold cell: this is the
conversation I'd have with the business, because the model gives probabilities and they decide the trade-off."*

### 4.6 Explaining drivers: logistic regression, odds ratios, OLS

**Business question:** *how much* does each factor change the outcome, holding the others fixed, and is the effect real?

**How it works:**
- **Binary targets** (`_logit_cell`, `automl/codegen.py:454`): statsmodels `Logit` on up to 20,000 training rows. Each
  category is compared with its most common value.
  ```text
  log(p / (1 - p)) = β0 + β1 x1 + ... ;   odds ratio = exp(β);   % change in odds = (exp(β) - 1) * 100
  ```
- **Regression** (`automl/codegen.py:514`, `automl/codegen.py:554`, `automl/codegen.py:614`):
  - VIF = 1 / (1 − R²ⱼ); the worst column is dropped while any VIF > 10;
  - OLS with coefficients, 95% confidence intervals, p-values, R² / adjusted R² and the F-test;
  - assumption checks: RESET (linearity), Breusch-Pagan (equal spread), Jarque-Bera (normal residuals), Durbin-Watson
    (independence).

**Why:** tree models predict better, but managers ask "by how much?". Odds ratios with confidence intervals answer that, and
p-values separate real effects from noise. I kept both: the best model for prediction, a linear model for explanation.

**Limits:**
- **Perfect separation:** "Complementary" bookings never cancel, so the coefficient runs to −20.6 with an infinite CI. The
  app flags "did not fully converge" and tells the user to treat that odds ratio as unreliable.
- **Large samples:** with thousands of rows the assumption tests flag tiny deviations, so the app says to judge by the plots
  too.

**Real numbers (hotel data, 20,000-row sample):** pseudo R² 0.33; 19 of 28 inputs are significant (p < 0.05).

| Factor (vs reference) | Odds of cancelling | 95% CI of the odds ratio |
|---|---|---|
| Repeated guest | −87% | 0.046–0.348 |
| Needs a parking space | −84% | 0.115–0.222 |
| Offline segment (vs Online) | −83% | 0.155–0.195 |
| Each extra special request | −77% | 0.213–0.244 |
| Arrival year 2018 (vs 2017) | +53% | 1.34–1.75 |

*Synthetic regression demo (500 rows, skewed price, log target):* OLS R² 0.995 (adjusted 0.995), max VIF 1.4, Durbin-Watson
2.02. RESET and Jarque-Bera flagged violations, which is expected, because the target was built as an exponential.

**30-second demo:** open **"Explain with logistic regression (odds ratios)"**. *"Each special request cuts the odds of
cancelling by about three quarters, and the interval is tight. Combined with the key-driver table, that suggests a product
experiment: prompt guests to add a request at booking. It's correlation, so I'd A/B test it, not assume it."*

### 4.7 Honest scoring: baseline, overfitting gap, unseen rows, leakage warning

**Business question:** can we trust the headline number?

**How it works:**
- **Baseline** (`automl/codegen.py:386`): `DummyClassifier(strategy="most_frequent")` or `DummyRegressor(strategy="mean")`.
- **Overfitting verdict** (`automl/narrative.py:321`):

  ```text
  train − test > 0.15  → overfitting
  0.05–0.15           → mild
  < 0.05              → generalises
  ```
- **Unseen rows** (`automl/codegen.py:906`): each row is hashed, and the model is scored only on test rows whose exact inputs
  never appear in training.
- **Leakage warning** (`automl/codegen.py:889`): a test score ≥ 0.99 with a baseline < 0.9 triggers a "check that no input
  gives away the answer" note.

**Why:** a model scored on rows that also sit in training is graded on questions it has already seen. The unseen-row score
is the number I'd quote to a stakeholder.

**Real numbers:** 65% of the hotel test rows have no twin in training. On those rows, macro F1 is **0.841**, vs 0.884 on all
test rows. The train/test gap is 0.005.

**30-second demo:** in **"Train and evaluate the winner"**, read the second 📌 line. *"0.884 is the headline, but 35% of
test rows have an identical twin in training. On genuinely new bookings, expect about 0.84."*

### 4.8 Readable rules and permutation importance

**Business question:** what's the simplest rule a manager could use, and what does the model actually rely on?

**How it works:**
- **A 3-level decision tree in real units, without scaling** (`automl/codegen.py:1034`), compared with the full model.
- **Permutation importance** on up to 2,000 test rows, 3 repeats (`automl/codegen.py:1096`):
  importance = score − score with that column shuffled.

**Why:** permutation importance works for any model and uses the original column names, not one-hot pieces. Impurity
importance favours high-cardinality columns.

**Limits:** correlated columns share credit. A column scoring about 0 may still matter on its own, and the app says so.

**Real numbers:**
- The tree's first question is `lead_time ≤ 151.5`.
- 3 questions score 0.768, vs 0.884 for LightGBM and 0.402 for the baseline.
- Top features: `lead_time` (shuffling it costs 0.220 macro F1), `no_of_special_requests`, `avg_price_per_room`.

**30-second demo:** show the tree plot. *"Three questions get you from 0.40 to 0.77. The full model adds another 0.12. That
gap is the value of the complex model."*

### 4.9 Clustering: PCA, choosing k, hierarchical cross-check

**Business question:** with no outcome column, what natural groups (segments) exist?

**How it works:**
- **Scaling:** every feature is scaled first.
- **PCA** (`automl/codegen.py:1146`): reports how many components keep 80% of the variance.
- **Choosing k** (`automl/codegen.py:1184`): K-Means for k = 2…8, keeping the best silhouette:
  ```text
  silhouette(i) = (b − a) / max(a, b)
  a = mean distance to own cluster, b = mean distance to the nearest other cluster
  ```
- **Cross-check** (`automl/codegen.py:1260`): ward hierarchical clustering on 1,500 rows, compared with K-Means using the
  adjusted Rand index.

**Why:** the elbow is subjective, and the silhouette makes the choice objective. A second method that agrees shows the
segments are real, not an artefact of K-Means.

**Limits:**
- K-Means assumes round, similar-sized clusters.
- Silhouette can prefer fewer, coarser clusters. In my run on 3 synthetic blobs, it chose **k = 2** (silhouette 0.43),
  probably because two blobs sit close together; I haven't proven that. It's a good example of why the elbow chart is shown
  as well.
- Hierarchical clustering is capped at 1,500 rows because its cost is O(n²).

**Real numbers (600-row synthetic blobs):** k = 2, silhouette 0.43. PCA needs 3 components for 80% of the variance. K-Means
and hierarchical agree at an adjusted Rand index of 0.99. The full run took 2.1 s.

**30-second demo:** choose 🔍 **No target** and run. *"Notice it picked 2, not the 3 I generated. Silhouette rewards
well-separated groups, and two of mine overlap. I'd show the elbow chart and let domain knowledge decide."*

### 4.10 Self-explaining cells and notebook export

**Business question:** can a non-expert follow, trust and reuse the analysis?

**How it works:** each cell has a `Step` story (`automl/narrative.py:20`) in six parts, always in this order:

1. 🔍 what we found before
2. 💡 why it matters
3. 🎯 what we're going to do
4. 🛠 what this code does
5. the code itself, then its output and charts
6. 📌 what we found after running

The 📌 lines come from `note()` calls inside the code (`automl/runner.py:87`). The export (`automl/codegen.py:1324`) turns the
stories into Markdown cells.

**Limits:** the explanations are templated English. They are accurate, but they don't adapt their style to the reader.

**30-second demo:** toggle **Beginner / Expert** in the sidebar, then click **⬇ Download as Jupyter notebook**. *"Same
analysis, with or without the teaching layer, and the user walks away with runnable code."*

### 4.11 Optional-library resilience

**Business question:** will it run on my laptop?

**How it works:** `library_problem()` (`automl/decision.py:40`) imports each optional library once. It returns `None`,
`"not installed"`, or the first line of the error. Broken libraries are skipped, with a decision-trace step telling a Mac
user to run `brew install libomp`.

**Why:** `importlib.util.find_spec` only checks that the files exist. The real failure happens when the library's native code
loads.

**Real numbers:** verified with a simulated `OSError` in tests, and in a browser run on the hotel data with LightGBM blocked.
That run had 0 errors and showed the `brew install libomp` advice. I have not tested on a physical Mac (**not verified**).

---

## 5. Data

**Source:** `examples/inn_hotels/INNHotelsGroup.csv`, a public hotel-bookings dataset used in a course case study. It is not
generated by this project, and I don't know its original collection method (**not verified**).

**Size:** 36,275 rows × 19 columns, 3.2 MB. No missing cells.

| Column | Type | Notes |
|---|---|---|
| `Booking_ID` | text | Unique per row → dropped as an ID |
| `no_of_adults`, `no_of_children` | int | 139 bookings have 0 adults |
| `no_of_weekend_nights`, `no_of_week_nights` | int | Mean stay 3.0 nights; 78 bookings have 0 nights |
| `type_of_meal_plan` | category (4) | Meal Plan 1 is 77% of rows |
| `required_car_parking_space` | 0/1 | 3% need parking |
| `room_type_reserved` | category (7) | Room_Type 1 is 78% of rows |
| `lead_time` | int (days) | Median 57, max 443 |
| `arrival_year`, `arrival_month`, `arrival_date` | int | Combined into a date and a weekday; 37 impossible dates (29 February 2018) |
| `market_segment_type` | category (5) | Online 64%, Offline 29%, Corporate 5.6%, Complementary 1.1%, Aviation 0.3% |
| `repeated_guest` | 0/1 | 2.6% |
| `no_of_previous_cancellations`, `no_of_previous_bookings_not_canceled` | int | Very skewed (skew 26.6 for the first) |
| `avg_price_per_room` | float | Median 99.45; 545 rows at 0 (354 Complementary, 191 Online) |
| `no_of_special_requests` | int 0–5 | 55% have none |
| `booking_status` | target | Canceled 32.8% (11,885) / Not_Canceled 67.2% |

**Cleaning the engine does:**
- drops `Booking_ID`;
- builds `arrival_weekday` from the three date parts (impossible dates are left blank, then filled with the median);
- keeps the 10,275 look-alike rows and reports an extra unseen-row score;
- imputes inside the pipeline (median for numbers, a "missing" category for text) and one-hot encodes categories, with rare
  ones grouped (max 20 per column).

The result is 18 input columns, which become 31 model columns.

**Patterns worth finding in an interview** (all computed with pandas from the file):

- **Lead time is the biggest lever.** Bookings more than 151 days ahead are 19.7% of bookings but **43.5% of cancellations**.
- **Engagement signals loyalty.** Cancellation is 43.2% with 0 special requests, 23.8% with 1, 14.6% with 2, and **0%**
  with 3 or more. Online bookings with no requests cancel 60.7% of the time (9,251 bookings).
- **Repeat guests almost never cancel:** 1.7% vs 33.6%. The same holds for parking: 10.1% vs 33.5%.
- **Segment:** Online 36.5%, Offline 29.9%, Corporate 10.9%, Complementary 0%. Complementary rooms have price 0, which is
  why the logit separates them perfectly.
- **Seasonality:** cancellations are lowest in January (2.4%) and December (13.3%), and highest in July (45.0%).
- **A trap in the year column.** 2017 only has July–December arrivals (6,514 rows), while 2018 has the full year. 2017's
  cancellation rate is 14.8% vs 36.7% in 2018, but month by month 2017 is far lower from August onward (e.g. September
  11.0% vs 45.8%). Either behaviour changed or data collection did. I would ask before trusting a "year" effect, and I would
  not use the year column to predict future bookings.
- **Revenue at risk.** Price × nights sums to about 11.35 million (currency not stated in the data). Cancelled bookings
  account for **37.9%** of it, slightly more than their 32.8% share of bookings, so the average cancelled booking is worth more than
  the average kept one.

---

## 6. Performance and quality

### Tests

45 tests, all passing, in **3 min 34 s** (`pytest -q`, 4 vCPU).

| File | Tests | What it covers |
|---|---|---|
| `tests/test_codegen_runs.py` | 21 | Executes the generated cells end to end: regression and classification beat the baseline, clustering, notebook export is valid JSON, one figure per `plt.show`, the six-part story on every cell, imbalanced data with resampling, odd column names, infinity, empty groups, the leakage warning, an unloadable LightGBM in the engine and in an exported notebook |
| `tests/test_decision.py` | 16 | Every decision branch: messy columns, imbalance, numeric-as-classes, log target, Ridge/Lasso rules, sampling, rare classes, quality gates, metric choice, the three target modes, ID-as-target |
| `tests/test_inn_hotels.py` | 4 | The real dataset: target suggestion, 10,275 look-alikes, 37 bad dates, clustering excludes the outcome, the full pipeline with ROC-AUC > 0.9 and tuned F1 ≥ 0.80 |
| `tests/test_profiling.py` | 4 | Column roles |

The slowest tests are the end-to-end ones: imbalanced resampling 35 s, the full hotel pipeline 29 s, the classification
pipeline 28 s.

### Speed (measured)

Hotel data (36,275 rows):

| Stage | Time |
|---|---|
| Read the CSV | 0.12 s |
| Profile | 0.09 s |
| Decide (includes importing LightGBM / XGBoost once) | 0.98 s |
| Run all 14 cells | **30.6 s** (36.7 s from the button click to the results in a real browser) |

The slowest cells are `tune` (15.7 s) and `compare` (7.1 s); every other cell takes under 2 s.

Scale test (synthetic churn data with messy columns, 10 features, 30% positive):

| Rows | Profile | Full run (14 cells) | Rows used for training | Rule-of-thumb model | Winner | Test macro F1 (baseline 0.41) |
|---|---|---|---|---|---|---|
| 1,000 | 0.02 s | 9.9 s | 1,000 | RandomForest | LGBM | 0.892 |
| 10,000 | 0.04 s | 36.9 s | 10,000 | RandomForest | LGBM | 0.924 |
| 100,000 | 0.38 s | 39.5 s | 50,000 (sampled) | RandomForest | LGBM | 0.935 |
| 200,000 | 0.64 s | 39.6 s | 100,000 (sampled) | HistGradientBoosting | LGBM | 0.952 |

From 10,000 to 200,000 rows the run time stays at about 40 s, because comparison and tuning always use a 10,000-row sample
and training is capped. Tuning (14–16 s) and comparison (9–11 s) dominate. 0 errors at every size. I did not test beyond
200,000 rows or files wider than about 20 columns (**not verified**).

**Why it stays fast:**
- Every stage has a cap:
  - training rows: 50,000 for forests, 100,000 for boosting and linear models (`automl/decision.py:32`);
  - comparison and tuning: 10,000 rows (`automl/codegen.py:24`);
  - EDA: 5,000 rows;
  - statsmodels: 20,000 rows;
  - clustering: 100,000 rows;
  - hierarchical clustering: 1,500 rows.
- Profiling uses 2,000-row samples for the text checks.
- Streamlit caches the load, profile and decision.

### Bugs found and fixed (good interview stories)

The two audits were 10 datasets (21 runs) and then 24 datasets (70 runs, 5 notebooks executed in Jupyter, plus browser checks).
Section 3 lists the fixes; the best stories are:

1. **Code injection through column names.** A generated-code tool has to treat data as data.
2. **Hidden duplicates** inflating the score: 0.884 → 0.841 on unseen rows.
3. **The macOS native-library crash:** "installed" ≠ "loads".
4. **Tuning that made things worse:** now only adopted if it beats the defaults.
5. **Shared matplotlib state** between concurrent users: fixed with a lock.

---

## 7. Likely interview questions with model answers

**Technical: statistics and metrics**

1. **Why macro F1 and not accuracy?**
   With 33% cancellations, always saying "not cancelled" is 67% accurate and useless. Macro F1 averages F1 over both classes,
   so the minority class counts equally. The baseline makes this visible: it scores 0.402 macro F1, against 0.67 accuracy.

2. **What does a ROC-AUC of 0.956 mean?**
   If I pick one cancelled and one kept booking at random, the model gives the cancelled one the higher probability 95.6% of
   the time. It is threshold-free, which is why I pair it with precision and recall at the chosen threshold.

3. **Precision vs recall: which matters here?**
   It depends on the action. If a predicted cancellation triggers a cheap reminder email, I favour recall. If it triggers
   overbooking a room, a false positive means a guest with no room, so I favour precision. The app shows both at 0.50 and at
   the tuned threshold, and leaves the choice to the business.

4. **How did you avoid data leakage?**
   - Preprocessing sits inside the `Pipeline`, so medians and scales come from training folds.
   - Resampling uses `imblearn`'s pipeline, so it only touches training folds.
   - Tuning and threshold choice use cross-validation on training rows.
   - The test set is used once.
   - I also score rows that have no duplicate in training.

5. **What's a p-value, and what did you use it for?**
   It is the probability of seeing an effect at least this large if the true effect were zero. In the logit, 19 of 28 inputs
   had p < 0.05. I use it to separate real drivers from noise, never as a measure of effect size; the odds ratio and its CI
   give the size.

6. **Interpret an odds ratio of 0.228 for special requests.**
   Each extra request multiplies the odds of cancelling by 0.228, a 77% drop, holding the other inputs fixed. The 95% CI is
   0.213–0.244, so the effect is precise. It is still an association, not a cause.

7. **What is multicollinearity and how do you handle it?**
   When inputs overlap, the regression can't attribute credit, and coefficients swing. I compute VIF = 1/(1−R²) per column and
   drop the worst while any VIF is above 10. It doesn't hurt prediction much, but it breaks interpretation.

8. **Why cross-validation and a test set?**
   CV is for choices: model, settings, threshold. The test set is the final exam, and using it for choices would make it
   optimistic. I use 3 folds because the app is interactive.

**Technical: SQL and data modelling** (the project itself uses pandas; these are how I'd do the same in SQL)

9. **Write SQL for the cancellation rate by market segment, and only show segments with at least 30 bookings.**

   ```sql
   SELECT market_segment_type,
          COUNT(*) AS bookings,
          AVG(CASE WHEN booking_status = 'Canceled' THEN 1.0 ELSE 0 END) AS cancel_rate
   FROM bookings
   GROUP BY market_segment_type
   HAVING COUNT(*) >= 30
   ORDER BY cancel_rate DESC;
   ```

10. **How would you find duplicates hidden behind an ID column in SQL?**
    Group by every column except the ID and count:

    ```sql
    SELECT SUM(n - 1) AS lookalike_rows
    FROM (SELECT COUNT(*) AS n
          FROM bookings
          GROUP BY no_of_adults, no_of_children, lead_time, arrival_year, arrival_month,
                   arrival_date, market_segment_type, avg_price_per_room /* … every non-ID column */) g;
    ```

    For the hotel data, the answer is 10,275.

11. **How would you model this data in a warehouse?**
    A `fact_booking` table at one row per booking, holding lead time, nights, price, special requests and status. Dimension
    tables for date (the arrival date, which also fixes the split year/month/day), room type, meal plan, market segment and
    guest (repeat flag, history). Cancellation would ideally be an event with a timestamp, not just a final status.

12. **The data has 29 February 2018. What do you do?**
    It is not a real date, so I'd raise it with the data owner. In the app, those 37 rows get a blank weekday that is imputed,
    and the trace reports it. I don't silently move them to 1 March.

**Product sense**

13. **Cancellations rose 10% last month. What do you do?**
    1. Check that it's real: the definition, logging changes, and whether "month" means booking month or arrival month.
    2. Segment it: market segment, lead-time band, room type, channel, and new vs repeat guests.
    3. Check the mix vs the rate: if more long-lead online bookings came in, the overall rate rises even if each segment is
       flat.
    4. Look at external causes: a price change, a competitor promotion, or seasonality (July runs at 45% here).

    The key-driver tables are exactly this decomposition.

14. **What would you build from these findings?**
    I'd use the model's probability to rank upcoming bookings by cancellation risk, and test interventions on the high-risk
    group. Examples: a deposit for bookings more than 150 days out, a prompt to add special requests, or a reconfirmation
    email. Then I'd measure it with an A/B test on the realised cancellation rate and the net revenue.

15. **How would you A/B test "prompt guests to add a special request"?**
    - **Design:** randomise at booking, so the treatment group sees the prompt.
    - **Primary metric:** cancellation rate.
    - **Guardrails:** booking conversion and the time it takes to complete a booking.
    - **Sample size:** a 33% base rate and a 3-point minimum detectable effect need roughly 3,800 bookings per arm (α = 0.05,
      80% power). I would compute this properly before launch.
    - **Watch for:** the correlation in the data may be selection. Engaged guests both add requests and show up, so the effect
      of the prompt may be much smaller than 0% vs 43%.

16. **Which metric would you put on a dashboard for this?**
    - **North star:** realised occupancy revenue.
    - **Driver metrics:** cancellation rate by lead-time band and segment, and revenue at risk (price × nights of bookings
      above the risk threshold).
    - **Model health:** precision and recall at the operating threshold, measured each week on bookings that have resolved.

**About the project**

17. **What was the hardest bug?**
    The macOS crash. LightGBM showed as installed (`find_spec` found it), but importing it failed because the OpenMP runtime
    was missing. The fix had two layers:
    - The engine really imports each optional library once and caches the reason it failed.
    - The exported notebook wraps each optional model in its own `try`, because the notebook may run on a different machine.

    I reproduced it on Linux by monkeypatching `import`, so it's covered by tests.

18. **What would you change if you started again?**
    I'd separate "what to run" from "how to render it" earlier. Code generation as strings works, but it caused the injection
    bug. I'd also store results (a run ID, metrics, decisions) so runs can be compared over time.

19. **How would it scale to 10 million rows?**
    Today it loads everything into pandas in memory and caps training rows. For 10M rows I'd:
    - aggregate in SQL or the warehouse (key drivers are just `GROUP BY` queries);
    - sample for modelling, or use a distributed or out-of-core trainer;
    - run jobs off the web process with a queue;
    - store results in a database.

    The rules and explanations wouldn't change.

20. **Why not just use an existing AutoML library?**
    They optimise the score and hide the reasoning. My goal was to teach the reasoning and produce code people keep. The score
    side holds up: LightGBM at 0.862 CV, with random forest and XGBoost within 0.005.

21. **How do you know the explanations are correct?**
    The 📌 lines are produced by the code, from the same variables it prints, so they can't contradict the output. A test
    checks that every cell has all story parts and writes at least one finding.

22. **How do you test code that generates code?**
    By running it. The tests build the cells for synthetic and real data, execute them, and assert on the results: no errors,
    beats the baseline, ROC-AUC > 0.9 on the hotel data, and the exported notebook runs even when a library won't load.

23. **What's a limitation you'd tell a user about?**
    It's correlational. The key drivers and odds ratios say what goes with cancellation, not what causes it. Also, the 2017 data
    covers only July–December, so the "year" effect is probably a data artefact.

---

## 8. Next steps (not built yet)

| Idea | Why it's worth discussing |
|---|---|
| **Cost-based threshold in the UI** (cost of a missed cancellation vs cost of a false alarm) | Turns the model into a business decision. Code exists in `docs/roadmap.html`, but it is not automated. |
| **Uplift / A/B test analysis** | Moves from "who will cancel" to "who will respond to an intervention". |
| **SQL / warehouse source** | Read from a database instead of uploads; push key-driver `GROUP BY`s down to SQL. |
| **Run history** | Store metrics and decisions per run (SQLite or Postgres) to compare models and detect drift. |
| **Deployment** | Docker image plus a hosted Streamlit app, and CI running the 45 tests on each push. None of this exists yet. |
| **SHAP explanations** | Per-booking explanations ("why is *this* booking high risk?") for operations teams. |
| **Time-aware validation** | Split by arrival date instead of randomly, which matters for forecasting real future bookings. |
| **SMOTENC, early stopping, more classes** | Better handling of categorical resampling, and threshold / driver cells for multiclass targets. |
| **Calibration** | Check that "70% risk" means 70% before using probabilities for revenue planning. |

---

## 9. Five-minute live demo script

Setup beforehand:

```bash
brew install libomp   # macOS only
pip install -r requirements.txt
streamlit run app.py
```

Then keep `examples/inn_hotels/INNHotelsGroup.csv` ready to upload.

| Time | Click | Say |
|---|---|---|
| 0:00 | Open the app | "Upload any CSV; it decides the analysis, writes the code, runs it and explains it." |
| 0:20 | Drag in `INNHotelsGroup.csv` | "36,000 hotel bookings." |
| 0:35 | Expand **Column profile** | "It found `Booking_ID` is an ID and should be dropped. A dtype doesn't tell you that." |
| 1:00 | Step 2 stays on 🤖 **Let the engine decide**; open **How the engine reached this decision** | "It picked `booking_status`, and found 10,275 look-alike rows hidden by the ID and 37 bookings on 29 February 2018. Every decision has a reason." |
| 1:40 | Click **▶ Run pipeline** (36.7 s in my browser test) | While it runs: "Fourteen cells: cleaning, EDA, key drivers, baseline, logistic regression, seven-model comparison, tuning, final test, threshold, rules, importance." |
| 2:15 | Scroll to **Find the key drivers** | "Long-lead bookings cancel 72% of the time vs 11% for last-minute ones. No special requests, 43%; three or more, 0%." |
| 2:50 | **Compare candidate models** | "Six or seven models (seven with XGBoost installed) on training folds. LightGBM wins at 0.862; the baseline is 0.40." |
| 3:15 | **Train and evaluate the winner** | "Test macro F1 0.884, gap 0.005, ROC-AUC 0.956. On test rows with no twin in training: 0.841. That's the number I'd quote." |
| 3:45 | **Tune the decision threshold** | "Lowering the threshold catches more cancellations at the cost of false alarms. That's a business call, and here 0.50 actually did slightly better on test." |
| 4:15 | **Explain with logistic regression** | "Each special request cuts the odds of cancelling by about 77%. It's a correlation, so the next step is an A/B test." |
| 4:40 | Toggle **Expert**, click **⬇ Download as Jupyter notebook** | "Same analysis without the teaching text, and you keep the code." |
| 5:00 | | "45 tests run the generated code end to end, and two audits (10, then 24 messy datasets) shaped the robustness." |

**Backup if the live run is slow:** open `examples/inn_hotels/INNHotels_cancellation_prediction.ipynb`, which is already
executed and saved with its outputs.
