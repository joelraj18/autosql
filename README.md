# AutoSQL Explorer

Upload your tables (CSV, Excel or a ZIP of them). AutoSQL Explorer then does five things:

1. **It learns the schema.** For every table it finds the key and the grain (what one row is: an order, an order line, a
   customer), every foreign key, and how many rows join to one.
2. **It validates before it analyses, and decides what to measure.**
   - Validation covers duplicates, NULLs, orphans, partial periods and join fan-out.
   - A transparent rule engine then picks the fact table, the revenue formula, the period, the drivers and the dimensions.
   - You choose how much it decides: **let the engine decide**, **choose yourself**, or **just validate the data**.
3. **It writes the SQL in MySQL 8 or PostgreSQL and runs it live.** The SQL is a set of small, testable CTE steps, following one
   method: schema & grain → validate → build with CTEs → KPIs → window functions → root cause → fast & correct.
4. **It explains every step and every result**, in plain English, from the actual numbers. Every change between two periods
   comes with a 95% confidence interval, so "+8% order value" is labelled *within normal variation* when it could be noise.
5. **It turns the results into a business brief**: a one-page infographic with the headline KPIs, prioritised
   recommendations (each with its evidence, a confidence level and the SQL step behind it), the charts that explain *why*
   revenue moved, and a data-trust score. It downloads as a single HTML file that works offline.

The app is built for learners and for anyone who has to trust a number. Every decision the engine makes comes with a three-part explanation:

> 🔍 **What we found:** `orders.freight` is stored once per `orders` row, but each `orders` row has up to 25 `order_details` rows.
> 💡 **Why it matters:** After joining, `freight` repeats on every line: its total would read 207,306.10 instead of 64,942.69 (3.19× too high).
> 🎯 **What we're doing now:** We roll `order_details` up to one row per order **before** joining, so that `freight` is counted once.

### How every step reads

Every **SQL cell** goes further and explains itself in six parts, always in this order:

1. 🔍 **What we found before**: the evidence that led to this step.
2. 💡 **Why it matters.**
3. 🎯 **What we're going to do**, and what that achieves.
4. 🛠 **What this SQL does**, in plain words.
5. **The SQL itself**, in MySQL 8 or PostgreSQL (sidebar switch), then its result tables and charts.
6. 📌 **What we found after running this**: written *by code* from the actual result, so it can't contradict the output.

A **Beginner / Expert** switch hides the explanations. Each cell also shows:
- 💼 the **business question** it answers and who usually asks it ("Who is about to churn?", customer success);
- 🔧 **technical notes**: the SQL techniques it uses (CTE, LAG, CUME_DIST, anti-join, RANGE frame…), every line that differs between
  MySQL 8 and PostgreSQL side by side, and rows and run time per statement;
- the DuckDB version that actually ran in the app, and the code that writes the 📌 findings.

## Run it

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then upload your files, or press **🧭 Try the Northwind example**.

## Your SQL method, step by step

| Step | What the app does | Cells |
|---|---|---|
| **1. Learn the schema and the grain** | keys (single, composite, or meant-to-be-unique with duplicates), the grain sentence, foreign keys found by name (including two-column keys such as `region` + `store_no`), by self-reference (`reports_to`) or *suggested* by matching values, cardinality, an ER diagram | `schema` (CREATE TABLE with PK/FK, tested on an empty database), `grain` (`COUNT(*)` vs `COUNT(DISTINCT key)`), `cardinality` (children per parent + a fan-out demo) |
| **2. Validate before analysing** | duplicates with `GROUP BY … HAVING COUNT(*) > 1`; NULLs per column (`COUNT(*) - COUNT(col)`) and **impossible values** (a negative price, a discount out of range; negative quantities reported as likely returns); date ranges and **partial periods**; orphans in both directions (`LEFT JOIN … IS NULL`, `NOT EXISTS`); totals **reconciled** to the cent from the raw table, from each view and, independently, in pandas from the uploaded file; rows that are **not sales** (cancelled, returned, test orders, a second currency) counted as their own line of the reconciliation | `duplicates`, `nulls`, `date_range`, `orphans`, `reconcile` |
| **3. Build in readable steps with CTEs** | two views at their true grain: `order_facts` (one row per order; lines rolled up **before** order-level tables are joined) and `line_facts` (one row per line); duplicates removed with `ROW_NUMBER()` and the business rules applied first (`<table>_kept`: `col IS NULL OR col NOT IN (…)`, so a missing status never drops a row); business names; LEFT JOIN + `'Unknown'` so that nothing is dropped | `base` |
| **4. Turn tables into KPIs** | revenue, orders, **AOV = revenue ÷ COUNT(DISTINCT order)** (with the average *line* value next to it), the **median and 90th percentile** of order value and ship days (by rank, since MySQL has no `PERCENTILE_CONT`), repeat-customer rate, units per order, ship days, late % (an unshipped order past its date counts as late), rating; per quarter, month or ISO week with `LAG` QoQ (kept only for a *complete* calendar neighbour, so a missing or partial quarter never fakes a comparison) and YoY by joining on `period_index − 4` (your New Wheels query, generalised); **per day** when two periods differ in length; a **95% confidence interval** on every change; what discounts cost, by band | `kpis`, `kpi_by_period`, `discounts` |
| **5. Window functions for trends and ranking** | running totals (`ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`); a 3-period moving average and unusual periods (`RANGE` frames; flagged only outside the 99% prediction intervals of both the recent level and the recent trend, so steady growth is not "unusual"); top-N per group with `DENSE_RANK` next to `ROW_NUMBER` (ties reported); Pareto / ABC concentration; the latest record per customer with `ROW_NUMBER`; days between orders with `LAG` and overdue customers; RFM segments with `CEIL(5 × CUME_DIST())`, so customers with equal values always get equal scores | `running_total`, `trend_signals`, `top_n`, `pareto`, `latest_record`, `purchase_gaps`, `rfm` |
| **6. Drill down to the root cause** | the revenue change split exactly into orders × average order value (a Shapley split: each effect at the average of the other, so nothing is left over to assign) and customers × orders per customer, then by segment with conditional aggregation; the suspected driver next to the symptom, per period and per segment, with correlations on levels *and* on period-to-period changes, each with a 95% interval; cohorts | `driver_tree`, `drilldown`, `cohort` |
| **7. Keep it fast and correct** | `EXPLAIN` read for you; a non-sargable `YEAR(date) = …` vs a sargable range; `CREATE INDEX` proposals; `NOT IN` vs `NOT EXISTS` with a NULL, `COUNT(col)` skipping NULLs, integer division in PostgreSQL; every assumption written down | `explain`, `null_traps` |

## What you can download

| Download | Contents |
|---|---|
| **Business brief `.html`** | one self-contained page for a manager: headline, KPI tiles with deltas, what to do (Act now / Plan / Monitor), the revenue trend with the suspected driver on its own axis, an exact revenue waterfall (orders × order value), change by segment, revenue concentration, RFM and discount charts, the trust score and the method. Every chart has a table view and keyboard tooltips; light and dark themes; works offline. See [`examples/northwind/business_brief.html`](examples/northwind/business_brief.html) |
| `.sql` script | every step in MySQL 8 or PostgreSQL, with its explanation as comments and the assumptions at the top |
| **Project `.zip`** | `01_schema.sql` (CREATE TABLE with keys), `02_load.sql` (`LOAD DATA LOCAL INFILE … NULLIF(@col, '')` or `\copy`), `03_validate.sql`, `04_analysis.sql`, `05_performance.sql`, the cleaned `data/*.csv` (snake_case headers, ISO dates, empty = NULL), `analysis.ipynb`, `docs/data_dictionary.md`, `docs/kpi_dictionary.md`, the business brief (after a run) and a README. Rerunnable on a real database |
| Notebook `.ipynb` | the same steps in Python + DuckDB, with every explanation |
| Data dictionary `.md` | every column: original name, type, role, key, % filled, distinct values and what the analysis uses it for |
| KPI dictionary `.md` | every KPI: definition, SQL, which direction is good, why it matters, its pitfall and the decision it informs |

## Project layout

| File | What it does |
|---|---|
| `app.py` | Streamlit page layout only |
| `autosql/loading.py` | reads CSV / Excel / ZIP as text, types each column (leading zeros kept, dates parsed, `$1,234.50`, `15%` and `(12.50)` read as numbers), renames to snake_case, builds a locked in-memory DuckDB |
| `autosql/profiling.py` | gives each column a *role* (`identifier`, `measure`, `rating`, `datetime`, `date_part`, `dimension`, `attribute`, `text`, `constant`) and finds each table's primary key and grain |
| `autosql/schema.py` | foreign keys (one or two columns), cardinality, orphans, fan-out risks, the ER diagram (Graphviz and Mermaid) |
| `autosql/decision.py` | the decision engine: focus → fact table and grain → revenue → business rules → time → drivers → dimensions → correctness. Every choice is recorded as a `Step` |
| `autosql/checklib.py` | period labels and the statistics (t quantiles, confidence intervals, the trend prediction interval). One source, used by the engine, the app and the exported notebook |
| `autosql/dialect.py` | the places where MySQL 8, PostgreSQL and DuckDB differ (quoting, dates, rounding, division, types) and the constructs never generated |
| `autosql/codegen.py` | turns a decision into up to 24 self-explaining SQL cells, each rendered for the three dialects, with its techniques and dialect differences |
| `autosql/runner.py` | runs the cells on DuckDB (with a timeout), then their checks, and captures results, charts and findings |
| `autosql/business.py` | the business layer: KPI dictionary, the business question per step, the trust score, recommendations, the executive summary and the data dictionary |
| `autosql/report.py` | the business brief: one HTML page with inline SVG charts (no network, no chart library) |
| `autosql/exports.py` | the `.sql` script, the project ZIP and the notebook |
| `autosql/narrative.py` | all the wording: `TEMPLATES` (found / why / action), `GLOSSARY`, and `interpret()`, which turns results into sentences |

## The decision tree

**New to this?** Open [docs/roadmap.html](docs/roadmap.html) in a browser. It is a one-page map of every step and condition,
with example routes and ready-to-paste SQL for techniques the app doesn't automate. This is a simplified view; every
threshold is in [docs/DECISION_TREE.md](docs/DECISION_TREE.md).

```
Upload (CSV / Excel sheets / ZIP)
  │
  ├─ Load ── read as text, then type: integers, decimals, dates (one format per column), codes with leading zeros stay text
  │          $1,234.50 · 1.234,50 € · 15% · (12.50) → numbers when ≥ 95% read; '-', '#N/A' → NULL
  │          names → snake_case (OrderID → order_id)
  │
  ├─ Profile ── column roles; primary key: single › two IDs together › an ID with duplicates (de-duplicated) › none
  │
  ├─ Relationships ── same name + ≥ 95% of values found      ──▶ used
  │                   both columns of a two-column key, ≥ 95% of pairs found ──▶ used (composite)
  │                   reports_to / manager_id / parent_id      ──▶ used (self-reference)
  │                   values only (equal sets or shared stem)  ──▶ suggested: you tick it
  │
  ├─ Gates ── no rows anywhere ──▶ stop and explain
  │
  ├─ 🔍 Just validate ─▶ schema · grain · cardinality · duplicates · NULLs · dates · orphans · EXPLAIN · NULL traps
  │
  └─ 🤖 / 🎯 Analyse
       fact table      = the table with price × quantity (or an amount) and the most rows
       order grain     = order key repeats? ──▶ line level: roll lines up to orders BEFORE joining order tables
       revenue         = price × quantity × (1 − discount)  │  SUM(amount)  │  row counts
       business rules  = status cancelled / refunded / returned / test…, is_test flags, a second currency ──▶ left out, reconciled
       period          = > 5 years: year │ ≥ 4 complete quarters: quarter │ ≥ 3 complete months: month │ ≥ 4 ISO weeks: week
                         first / last period with < 90% of its days ──▶ partial: shown, never compared
       comparisons     = last two complete periods, per day when their lengths differ by ≥ 3%, each with a 95% interval
       drivers         = ship date − order date (ship_days) · shipped after required date · rating (text scales mapped 1-5)
       dimensions      = ≤ 3: geography › product › channel › fulfilment › people (≤ 2 joins away, ≤ 30% NULL)
       no money and no date ──▶ falls back to validation
  │
  └─ 📄 Business brief
       trust score     = 25% completeness + 20% uniqueness + 20% integrity + 15% validity + 20% consistency
                         ≥ 90 High · ≥ 75 Good, with caveats · else Use with care
                         capped: totals that don't reconcile ──▶ Use with care; any check below 60 ──▶ at most Good
       recommendations = Act now (data problems, falls) › Plan (growth, drivers, churn, pricing) › Monitor
                         each with evidence, confidence (measured · indicative · needs a test) and its SQL step
```

How to read the brief, every KPI and every recommendation rule: [docs/business_guide.md](docs/business_guide.md).

## Worked example

[`examples/northwind/`](examples/northwind/) is Microsoft's Northwind sample (8 tables, 830 orders, 2,155 order lines,
MIT licence), built from the original install script. Its notebook first runs four classic mistakes next to the correct
query, then the full analysis:

| Trap | Naive answer | Correct answer |
|---|---|---|
| Sum `freight` after joining order lines | 207,306.10 | **64,942.69** (3.19× too high) |
| "Latest quarter" when the data ends on 6 May | −52.4% | **+64.3%** (last two complete quarters) |
| Average order value as the average of lines | 587.37 | **1,525.05** |
| Employees who manage nobody, with `NOT IN` | 0 | **7** (`NOT EXISTS`) |

And the business reading, from [its brief](examples/northwind/business_brief.html): revenue reached **298,491.55 in 1998-Q1
(+64.3% on 1997-Q4, +115.8% year over year)**, and 84% of that movement came from more orders (+51.7%, 95% CI +20.4% to
+91.0%), not bigger ones: the average order value rose 8.3%, but its 95% interval (−20.3% to +36.9%) includes zero, so that
part could be noise. Beverages drove 57% of the change. The top 20% of customers bring 60% of revenue. Discounts cost 6.5% of
gross revenue. Shipping time did **not** drive the change (9.2 → 8.6 days). The typical order is 943.25 (the median), well
below the 1,525.05 average. Data trust: **99/100, High**.

**Presenting this project?** [docs/guide.md](docs/guide.md) is an interview guide. It has a 60-second pitch, the architecture,
deep dives with measured results, SQL interview questions with answers, and a 5-minute demo script.

## Known limitations

What the engine does **not** do, so that nobody has to find out the hard way:

- **One fact table, one revenue formula.** Revenue is price × quantity (× (1 − discount)), an amount, or a row count. There
  is no cost or margin, no currency conversion and no second fact table (say, returns or targets) in the same analysis.
- **Business rules are found by name.** Status columns (`status`, `stage`, `order_state`…), flags (`is_test`, `cancelled`…) and
  currency columns are recognised by their names and values. A rule hidden in a code (`type = 7`) is missed, a partially
  refunded order stays in whole, and rows with no currency are assumed to be in the main one. In manual mode you can keep
  every row instead.
- **Two currencies are never added up.** The most common one is kept and the others are left out, with their share in the
  reconciliation: convert them before uploading to include them.
- **Composite keys have two columns, with the same names on both sides.** They are not used for the `NOT IN` demonstration,
  a list price or the order table, which compare one column.
- **Calendar periods only:** years, quarters, months or ISO weeks. No fiscal calendars or 4-4-5 retail weeks, and a partial
  period is judged by calendar days with data, not trading days.
- **The confidence intervals rest on assumptions:** orders arrive independently (Poisson), and periods with very few orders
  make the normal approximations rough. A few very large orders widen the revenue interval, which is the honest answer. Each
  KPI is tested on its own, with no correction for testing six tiles at once.
- **Correlation is not causation.** A driver that moved the right way is a *candidate* cause, labelled "needs a test"; the
  app never claims more than that, and with fewer than 6 complete periods it reports no correlation at all.
- **Dates use one format per column** (95% must parse); `03/04` with no day above 12 is read as month/day, with a warning.
- **Uploads, not connections.** Files are read into memory with pandas, so the practical limit is the machine's memory;
  1,000,000 rows take about 6 seconds end to end.

## Adding a new explanation or rule

1. Add a template to `TEMPLATES` in `autosql/narrative.py`, with `found`, `why` and `action`.
2. In `autosql/decision.py`, append `story("your_key", ...)` to `d.steps` where the rule fires.
3. If the rule changes the SQL, read the new `Decision` field in `autosql/codegen.py`. Write the statement once as a function of
   a `Dialect`, and use `D.ident()`, `D.days_between()`, `D.round()` and `D.pct()` wherever MySQL and PostgreSQL differ.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The tests (220, about 5 minutes with the live databases):
- check loading, column roles, keys and every decision branch, and a **golden snapshot** of what loading and profiling
  return for every test dataset, so speed work can't change a result;
- prove that the relationships inferred for Northwind match the 8 FOREIGN KEY constraints in Microsoft's script;
- **run every generated cell** on 20 cases (17 datasets, some also in validate mode or with a suggested link ticked),
  including reserved-word names, timestamps, list prices, orphans, duplicates, a missing quarter, ten weeks of data,
  count-only data, double-loaded rows, a two-column key, cancelled / test / euro orders and impossible values;
- check the statistics against known values (t quantiles by numerical integration, each confidence interval on a worked
  case) and that every number quoted in the Northwind docs appears in what the code prints (`tests/test_docs_claims.py`);
- check the business layer: the headline, tiles, trust score and recommendations on Northwind and the New Wheels shape,
  wording that follows the sign of the data, and that pure noise is not flagged as an unusual period;
- check the brief: self-contained (no external resources), a table view per chart, every mark focusable, escaped text;
- parse every MySQL and PostgreSQL statement with `sqlglot`;
- enforce the dialect rules;
- execute the exported notebook;
- drive the Streamlit page headlessly.

Two suites run the **exported project on real databases** for 19 cases and compare every query result with DuckDB
(549 results per database):

```bash
pytest tests/test_live_postgres.py   # starts a throw-away PostgreSQL from a local install (or uses AUTOSQL_PG_URL)
docker run -d --name autosql-mysql -e MYSQL_ROOT_PASSWORD=autosql mysql:8.4 --local-infile=1
AUTOSQL_MYSQL_CONTAINER=autosql-mysql pytest tests/test_live_mysql.py
```

GitHub Actions runs all of it on every push ([`.github/workflows/tests.yml`](.github/workflows/tests.yml)): PostgreSQL 16 as
a service, MySQL 8.4 in Docker, and `AUTOSQL_REQUIRE_LIVE=1`, so a database that fails to start fails the run instead of
being skipped.

`python scripts/sweep.py` runs 24 more awkward datasets in auto and validate mode and reports any error or silent cell;
`python scripts/benchmark.py` (or `… northwind`) times every stage.
