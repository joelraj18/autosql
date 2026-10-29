# Guide: AutoSQL Explorer

A guide for presenting this project. It has a 60-second pitch, the architecture, deep dives into each step with **measured**
results, interview questions with model answers, and a 5-minute demo script.

Every Northwind number on this page is printed by the code: `tests/test_docs_claims.py` fails if a figure in section 3 does
not appear in what a run prints, and `tests/test_northwind.py` pins the important ones. Timings are measured by hand.

---

## 1. The 60-second pitch

> I write SQL that someone else can read, rerun and trust. AutoSQL Explorer turns that habit into a tool. You upload tables,
> and it learns the schema first: each table's key and grain, every foreign key, and how many rows join to one. It validates
> before it analyses: duplicates, NULLs, impossible values, orphans, partial periods and join fan-out. Then a rule engine
> decides what to measure: the fact table, the revenue formula, which rows are real sales, the period and the drivers. It
> writes the SQL as small CTE steps in MySQL 8 or PostgreSQL, runs it, and explains every step and every number in plain
> English.
>
> On Microsoft's Northwind sample it finds four classic traps on its own. Freight summed after joining order lines is
> 3.19× too high. The "latest quarter" looks like a 52% collapse, but it only has 36 days of data; the real change is +64%.
> The average line value understates average order value 2.6×. And NOT IN returns 0 rows where NOT EXISTS returns 7.
> Revenue reconciles to the cent between the raw table, both views and an independent pandas recomputation. Every change
> comes with a 95% confidence interval, so the +8.3% in order value is flagged as possible noise. CI runs the generated SQL
> on real PostgreSQL 16 and MySQL 8.4 on every push, and compares every result with the in-app engine.

**Where I have applied this.** On *New Wheels* (revenue and operations), I queried 1,000 orders from 984 customers across 49
U.S. states. I tracked quarterly revenue, orders and ratings, and linked a 39% revenue decline to a 60% rise in shipping time
over the same quarters (28 to 45 days). I proposed a 25-day shipping SLA, to be confirmed with a controlled test. The
`kpi_by_period` cell in this app is that query, generalised: the suspected driver sits next to the symptom, with `LAG` for
quarter-over-quarter change. `tests/test_new_wheels_shape.py` checks it on a synthetic table with the same shape.

---

## 2. Architecture

```
upload ─▶ loading.py ─▶ profiling.py ─▶ schema.py ─▶ decision.py ─▶ codegen.py ─▶ runner.py ─▶ narrative.interpret()
          text first,    roles, keys,    foreign keys   fact, revenue,  25 cells ×      DuckDB, then   plain-English
          then types;    grain           (1 or 2 cols), business rules, 3 dialects      each cell's    summary
          snake_case                     cardinality,   periods,        (dialect.py)    check → 📌           │
                                         fan-out        drivers, dims         │              ▲                ▼
                                                                              │        checklib.py: period labels, statistics
                                                                              │          business.py: trust score, recommendations,
                                                                              │          KPI and data dictionaries ─▶ report.py: brief
                                                                              └─▶ exports.py: .sql · project ZIP · notebook
```

Design choices worth defending:

- **One statement, three dialects.** Each statement is a Python function of a `Dialect` and is rendered for MySQL 8 and
  PostgreSQL (shown) and DuckDB (run). Only quoting, date arithmetic, rounding and `EXPLAIN` differ. Everything else uses
  constructs all three accept, and `BANNED` lists what is never generated: `FILTER`, `NULLS LAST`, `QUALIFY`, `FULL JOIN`,
  `PERCENTILE_CONT`… Transpiling with a library would drop the comments that document assumptions.
- **Run here, prove there.** DuckDB is in-process, so the app needs no server. Because DuckDB is not PostgreSQL (its `/`
  never truncates), the tests export the project, run it on real PostgreSQL and MySQL, and compare 645 query results per
  database with DuckDB. GitHub Actions does this on every push, and fails (rather than skips) when a database is missing.
- **Stories next to code.** Each cell's six-part story lives next to its SQL in `codegen.py`. The 📌 findings are written by a
  small Python check from the actual result, so the text can't drift from the numbers.
- **One source for the statistics.** `checklib.py` holds the period labels and every confidence interval. The engine imports
  it, and its source is pasted into the exported notebook, so the notebook computes exactly what the app shows.
- **Safety.** Every name goes through `snake_case` and `Dialect.ident()`; every value goes through `literal()`, which doubles
  quotes and, in MySQL, backslashes. The DuckDB connection is locked (`enable_external_access = false`,
  `lock_configuration = true`), so generated SQL can't read local files. Each statement has a 60-second timeout.

---

## 3. Feature deep dives

### 3.1 Loading: text first
- **Problem:** `pd.read_csv` turns the postal code `05021` into the number `5021`, and `7/4/1996` into a string. A
  spreadsheet export writes `$1,234.50`, `15%` or `(12.50)`, and one `-` among numbers makes the whole column text.
- **Approach:** every column is read as text, then typed:
  - integers only when no value has a leading zero;
  - dates only when ≥ 95% parse with **one** format;
  - `m/d` vs `d/m` resolved by days above 12, else flagged as ambiguous;
  - ISO with a time zone stored as UTC;
  - business numbers when ≥ 95% read: a currency sign or code used consistently is stripped and kept as the unit,
    thousands separators go (swapped for `1.234,50`), `15%` becomes 15 and `(12.50)` becomes `-12.50`. Markers such as `-`,
    `#N/A` or `unknown` become NULL. Two currencies in one column keep their numbers, with each row's currency in a new
    `<column>_currency` column.
- **Names:** become snake_case (`OrderID → order_id`), with the originals kept.
- **Result (Northwind):** 75 columns renamed; 18 postal codes kept their leading zeros; `order_date` became a real DATE
  (`1996-07-04`).

### 3.2 Keys and grain
- **Problem:** knowing what one row is (an order or an order line) is what stops a join from double-counting.
- **Single-column key:** unique and never empty, scored by name, position and type.
  - Northwind `customers`: `customer_id` beats `company_name`, which is also unique.
  - `shippers` has 3 rows and still gets its key: there is no minimum row count.
- **Otherwise, two ID columns together:** `order_details(order_id, product_id)`, one row per **order line**.
- **A table loaded twice** keeps its key: `order_id` is unique once the exact copies are removed, and the copies are
  dropped with `ROW_NUMBER()` in the views.
- **Otherwise, an ID unique in ≥ 90% of rows** is the key it was *meant* to be. Its duplicates are listed and removed with
  `ROW_NUMBER()`.
- **SQL proof:** `COUNT(*)` vs `COUNT(DISTINCT key)`; for a pair, `SELECT COUNT(*) FROM (SELECT DISTINCT a, b …)`, because
  PostgreSQL has no `COUNT(DISTINCT a, b)`.

### 3.3 Foreign keys: names first, values only as a suggestion
- **Rules:**
  - **name match** + ≥ 95% of values found → used;
  - **both columns of a two-column key** (a `stores` table keyed by `region` + `store_no`) + ≥ 95% of the pairs found →
    used, joined on both columns;
  - `reports_to` / `manager_id` / `parent_id` that all exist in the same table → used (self-reference);
  - **values only** → suggested, never used silently.
- **Why:** `orders.ship_via` holds {1, 2, 3}, and those values are also valid employee, category, product and supplier IDs.
  A value-only link must have equal value sets or a shared name stem ("ship" in `ship_via` / `shippers`), and the user ticks it.
- **Measured:** 7 relationships found and 1 suggested. Found + ticked = **exactly the 8 FOREIGN KEY constraints** in
  Microsoft's install script (`tests/test_schema.py` compares them).

### 3.4 Join fan-out
- **Rule:** for every 1:N link, numbers stored on the parent are summed alone and after the join.
- **Measured:** `orders.freight` = 64,942.69, but **207,306.10** after joining `order_details` (**3.19×**).
- **Fix built in:** `order_facts` rolls lines up to orders in CTEs **before** joining `orders`.

### 3.5 Validation
| Check | SQL | Northwind result |
|---|---|---|
| duplicates | `GROUP BY key HAVING COUNT(*) > 1` | none |
| NULLs | `COUNT(*) - COUNT(col)` per column | `shipped_date` empty for 21 orders |
| impossible values | `SUM(CASE WHEN price < 0 …)`: negative prices or charges, discounts out of range, zero or negative quantities | none |
| dates | MIN/MAX, rows per period | 1996-07-04 to 1998-05-06; **1998-Q2 partial** (36 of 91 days) |
| orphans | `LEFT JOIN … WHERE parent IS NULL`, `NOT EXISTS` | 0 orphans; 2 customers never ordered (FISSA, PARIS) |
| reconciliation | raw table vs `order_facts` vs `line_facts` vs pandas on the upload | **1,265,793.04** everywhere |

### 3.6 Revenue and AOV at the right grain
- **Revenue** = `unit_price × quantity × (1 − discount)`, **net** of discount. Gross is 1,354,458.59; discounts 6.5%.
- **Price:** the line price, not the product's list price. They differ in **30.5%** of lines, and the reconciliation shows it.
- **AOV** = revenue ÷ `COUNT(DISTINCT order_id)` = **1,525.05**. The average *line* is 587.37 (2.6× smaller), and the KPI
  cell shows both.
- **The typical order is smaller than the average:** the median order is **943.25** and 90% of orders are below 3,160.60, so
  a target set on the average is really set on a handful of large orders. MySQL has no `PERCENTILE_CONT`, so the median is
  computed by rank: `ROW_NUMBER()` and `COUNT(*) OVER ()`, then the middle rank `FLOOR((cnt + 1) / 2.0)`.
- **Ship days** are averaged per **order** (8.49), not per line: big orders would otherwise count several times.

### 3.7 Trends without the partial-period trap
- **Rule:** the first and last period are partial when < 90% of their days have data. They are labelled `period_status =
  'partial'`, and no comparison ever starts from one: the previous value and the year-ago value are kept only when that
  period is complete.
- **Measured:** naive "latest quarter" **−52.4%**; last two complete quarters (1997-Q4 → 1998-Q1) **+64.3%**; year over year
  1998-Q1 vs 1997-Q1 **+115.8%**.
- **Gap-safe:** each period gets `period_index = year × 4 + quarter`. `LAG(revenue)` is kept only when
  `period_index − LAG(period_index) = 1`, and year over year joins on `period_index − 4` instead of `LAG(revenue, 4)`. With a
  quarter missing, `LAG` would silently compare with the quarter *before* the gap; the `gap_quarter` test case proves it doesn't.
- **Calendar length:** when two periods differ in length by 3% or more (February vs January), the change is also computed
  **per day**, and the verdict uses it: identical daily sales must not read as "orders fell 9.7%".
- **Short uploads:** ≥ 3 complete months are compared by month; less than that, by ISO week (`2022-W07`), indexed by Mondays
  since `1970-01-05` so that a 53-week year never breaks `LAG`.
- **Unusual periods:** a 3-period moving average with `RANGE` frames. A period is flagged only when it is outside the 99%
  prediction interval of up to 8 earlier complete periods, `t(0.995, n − 1) × √(1 + 1/n)`, **and** outside the 99%
  prediction interval of a straight line through them, so steady growth is not "unusual". Northwind 1998-Q1: z = +4.8
  against a limit of 4.36 (6 earlier quarters), and the trend predicted 196,668.16.

### 3.8 Window functions
| Technique | Cell | Northwind |
|---|---|---|
| running total, `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` | `running_total` | half of all revenue reached by 1997-Q3 |
| top-N with `DENSE_RANK` next to `ROW_NUMBER` | `top_n` | Côte de Blaye leads Beverages; no ties |
| latest record with `ROW_NUMBER() … = 1` | `latest_record` | median 23 days since a customer's last order (to the last date in the data, not today) |
| RFM with `CEIL(5 × CUME_DIST())` | `rfm` | Champions: 24 of 89 customers, 54.4% of revenue |

`NTILE(5)` forces five equal buckets, so customers with the same number of orders landed in different buckets depending on
their id: 34 of the 89 Northwind customers got their F score that way. `CUME_DIST` gives equal values equal scores.

### 3.9 Root cause: driver tree and drill-down
- **Arithmetic:** revenue = orders × AOV. Between two complete periods, the **Shapley split** measures each effect at the
  average of the other: volume = ΔO · (A₁ + A₂) / 2, order value = ΔA · (O₁ + O₂) / 2. The two add up to the change exactly,
  with nothing left over to assign by convention.
- **Measured:** +116,810.09 = **+97,776.34** from orders (120 → 182) + **+19,033.75** from order value
  (1,514.01 → 1,640.06): **84% volume**. The textbook split (+93,868.75, +15,126.16 and +7,815.18 of "both") is shown
  as a teaching note: its shares depend on which effect is measured first.
- **By segment:** conditional aggregation puts both periods side by side; Beverages = 57% of the change.
- **Drill-down:** puts the driver next to the symptom. Ship days moved −6.5% while revenue moved +64.3%, so shipping is
  **not** the cause here. Over 7 complete quarters, revenue and ship days correlate at r = +0.02 (95% CI −0.74 to +0.76) on
  levels and −0.37 on quarter-to-quarter changes: neither interval excludes zero, so the app says this many periods can't
  show a relationship.
- **New Wheels-shaped test table** (synthetic): revenue falls 17% while ship days rise 15% (40 → 46). The app calls it a
  candidate cause and says to confirm it with a controlled test.

### 3.10 Real change or noise?
Every change between the two compared periods carries a 95% confidence interval (`checklib.py`, no SciPy):

| KPI | Method | Northwind 1997-Q4 → 1998-Q1 |
|---|---|---|
| orders | Poisson rate ratio (log scale) | +51.7% (95% CI +20.4% to +91.0%): real |
| revenue | compound Poisson: Var = n(sd² + mean²) | +64.3% (95% CI +15.0% to +134.7%): real |
| average order value | Welch, with Welch–Satterthwaite degrees of freedom | +8.3% (95% CI −20.3% to +36.9%): **within noise** |
| customers | Poisson rate ratio | +20.0% (95% CI −14.8% to +69.0%): within noise |
| ship days | Welch | −6.5% (95% CI −25.4% to +12.3%): within noise |
| late % | Newcombe (Wilson score), in points | −2.3 pts (95% CI −8.6 to +2.9 pts): within noise |

The brief shows a muted "within noise" chip instead of green or red, the app turns the delta colour off, and the headline
names a driver only when its change is real.

### 3.11 Rows that are not sales
- **Problem:** cancelled, refunded, returned, failed or test orders count as revenue, and amounts in two currencies get
  added up into a number in no currency.
- **Rule:** status columns (`status`, `stage`, `order_state`…; plain `state` is a geography), flags (`is_test`, `cancelled`,
  in true/false, 0/1 or Y/N) and currency columns are found in the fact and order tables. The views read every table through
  `<table>_kept` with `col IS NULL OR col NOT IN (…)`: a bare `NOT IN` would drop every row whose status is empty. Lines of a
  left-out order go with it (`NOT EXISTS`).
- **Proof:** the reconciliation gains a "left out by the business rules" row and checks raw = left out + kept, and pandas
  applies the same rules independently. Northwind has no such column, so nothing is left out.

### 3.12 The business layer: from query results to a decision
- **Trust score** (0–100): Completeness 25%, Uniqueness 20%, Integrity 20%, Validity 15%, Consistency 20%. Northwind:
  **99/100, High** (the only deduction: `orders.shipped_date` is 2.5% empty, 21 unshipped orders). A weighted average can
  hide a failure, so the grade is **capped**: totals that don't reconcile mean "Use with care" whatever the average, and any
  check below 60 means at most "Good, with caveats".
- **Recommendations:** rules over the metrics, each with evidence, an implication, a next step, a priority (Act now / Plan /
  Monitor), a confidence (measured / indicative / needs a test) and the SQL step behind it. Northwind's first one is a data
  warning: 1998-Q2 is incomplete. Every rule is listed in [business_guide.md](business_guide.md#recommendation-rules).
- **Wording follows the data.** A driver is a "candidate cause" only if it moved the way that would explain the change: more
  ship days while revenue fell, or a falling rating while revenue fell. Effect shares are computed by size, so they never read
  as more than all of the change.
- **The brief:** one offline HTML page with inline SVG charts (no chart library): KPI tiles, the revenue waterfall, change by
  segment, the concentration curve, RFM and discount bands, the trust meters. One y-axis per chart, a table view per chart,
  keyboard tooltips, light and dark themes.
- **The SQL under every figure.** Each chart, tile row, fact and trust meter points at the statements it was computed from
  (`briefsql.py`), shown in MySQL 8 and PostgreSQL with a copy button and a tab the reader's choice sticks to; the full code of
  every step used is in an appendix. Nothing is written for the page: a block is the text of a `Statement` that `build_cells`
  generated, so what you copy is what produced the number (a test compares them character for character).
- **A recommendation opens what it was made from.** Each one names the cards and charts that carry its numbers (`Insight.shows`)
  and the figures that appear nowhere else on the page (`Insight.figures`); the page's own nodes are copied into a popup, so
  nothing is drawn twice. A test pins the evidence of every recommendation for Northwind, re-derived from the raw CSVs, and a
  contract test on every case checks that a recommendation never names a chart the page doesn't have.

### 3.13 Your own assumptions and KPIs
A user knows things the engine can't (the currency, a definition of their own), and wants a KPI the dictionary doesn't have.
The answer is data, not a plugin: a `Customization` (JSON a person can write by hand) passed to `decide(..., custom=…)`.
- **Kept apart from what was decided.** The engine's assumptions get stable keys (`fact`, `revenue`, `periods`, …); `edit`
  rewords the one with that key, `add` appends. Everything you changed says so wherever it appears (*edited by you*, *added by
  you*), and an edit whose key matches nothing is a visible notice, never a silent drop. A KPI of yours is a tile with its own
  chart in a section of its own, and its change is shown **without a verdict**: no significance test was run on it, so the page
  doesn't claim "better" or "worse".
- **Computed on the same rows.** Your `sql` is one aggregate expression. The engine writes the statement around it
  (`SELECT <yours> AS key FROM order_facts`, then the same with `GROUP BY` the period columns), so it sees exactly the orders,
  revenue, business rules and periods the standard KPIs do. It is a cell of its own (`custom_kpis`) in the scripts and the
  notebook, built only when there is SQL to run.
- **Soft statements.** SQL somebody else wrote may fail. Normally a failing statement fails its cell and the runner skips
  everything after it; yours are marked `soft`: the error is kept next to the KPI (the notebook catches it too) and nothing
  else changes. A test runs a misspelt column and a non-aggregate beside a good KPI and compares every standard number with a
  run without them.
- **A dry run finds mistakes without reading a row.** The views and your statements run on tables with the same columns and no
  rows. A misspelt column or an unknown function fails at once; and an expression that isn't an aggregate is caught by a
  property of SQL: an aggregate over no rows is still one row, anything else is none.
- **Checked before it runs.** One expression: no `;`, `--`, `/* */`, backtick or backslash; quotes and parentheses balance; no
  `SELECT`, `FROM`, `UNION`, `WHERE`… outside parentheses (a sub-query inside them is fine, and so are `WITHIN GROUP (ORDER BY …)`
  and `IS DISTINCT FROM`); no call that waits, writes or reads files. Names and definitions are one line, because they end up in
  SQL comments, where a newline would start a statement. This is a guard against mistakes; the sandbox that matters is the
  locked in-memory DuckDB (`enable_external_access = false`), which a test attacks with `read_csv`, `COPY` and `ATTACH`.
- **The traps the engine avoids in its own SQL are named in yours.** The number shown here comes from DuckDB; your database will
  run the text later. `/` without `NULLIF(…, 0)` or a decimal stops PostgreSQL on a zero divisor and divides whole numbers
  differently (5 / 2 is 2.5 in MySQL, 2 in PostgreSQL); `FILTER (WHERE …)` and `::` don't exist in MySQL. These are warnings in
  the app, the brief and a `-- Check:` comment in the script, because the expression still runs. `sql` can also be an object with
  one expression per dialect (DuckDB uses PostgreSQL's), which the tests run on real PostgreSQL.
- **Proof:** each KPI is compared with a pandas calculation on the raw CSVs (per quarter and overall); the three customized
  cases run through every contract test and the live PostgreSQL and MySQL suites; the notebook executes with a failing KPI in it.

### 3.14 Fast and correct
| Point | Measured on Northwind |
|---|---|
| `NOT IN` vs `NOT EXISTS` (employees who manage nobody; `reports_to` has one NULL) | **0 vs 7** |
| `COUNT(*)` vs `COUNT(shipped_date)` | 830 vs 809: AVG skips 21 NULLs |
| `YEAR(order_date) = 1997` vs `order_date >= '1997-01-01' AND < '1998-01-01'` | both 408 rows; only the range can use an index |
| integer division | `2 / 3` = 0 in PostgreSQL (shown in the export; confirmed on PostgreSQL 16), 0.6667 in MySQL |
| `EXPLAIN` | read into its operators, including the hash join |

---

## 4. Data
- **Northwind** (`examples/northwind/`): built by `build_data.py` from Microsoft's `instnwnd.sql` at a pinned commit (MIT
  licence). Each INSERT is evaluated with SQLite after removing T-SQL's `N''` prefix and the hex image data. 8 tables, 3,202
  rows.
- **Test fixtures** (`tests/conftest.py`, `tests/test_codegen_runs.py`, `tests/datasets.py`):
  - a **synthetic** New Wheels-shaped table (1,000 orders before thinning, 49 states, text feedback, a planted shipping
    slowdown);
  - a messy normalised shop (duplicate customer keys, 3 orphan lines, leading-zero zips, columns named `order` and `group`);
  - timestamps, list prices, a denormalised line table, reserved-word names, ten weeks of orders, a missing quarter,
    double-loaded rows, a two-column store key, cancelled / test / euro orders, signed amounts in two currencies and
    impossible values;
  - 24 more awkward shapes in the sweep (`scripts/sweep.py`): a single row, one column, 7 years, day/month dates, a table
    with no key, Unicode values, a zero-revenue quarter…

---

## 5. Performance and quality

### Tests
- `pytest -q`: over 200 tests, about 4 minutes with the live databases.
- **Every generated cell runs** on 23 cases: 17 datasets, some also in validate mode, with a suggested link ticked, or with
  assumptions and KPIs of your own.
- **A golden snapshot** records what loading, profiling and schema inference return for every test dataset (types, a hash
  of every column's values, roles, keys, relationships). Speed work must leave it unchanged.
- **Statistics are tested:** the t tables against a numerical integration of the t density, each interval on a worked case,
  pure noise not flagged as an unusual period, a planted spike found.
- Every MySQL / PostgreSQL statement **parses** (`sqlglot`).
- **Dialect rules are enforced:**
  - no banned construct;
  - no reserved alias;
  - every `/` guarded;
  - PostgreSQL `ROUND` on `::numeric`;
  - no `WHERE` on a LEFT-joined table.
- The exported **notebook executes**, and the **Streamlit page** is driven headlessly.
- **Live databases:** the exported project runs on **PostgreSQL 16** (`test_live_postgres.py`) and **MySQL 8.4 in Docker**
  (`test_live_mysql.py`) for 22 cases: 645 query results per database are compared with DuckDB, and all match. GitHub
  Actions runs both on every push.
- **The docs are tested:** every Northwind figure with three or more digits in this guide's section 3, the README's worked
  example, the example README, the business guide's read-aloud and the roadmap's Northwind cards must appear in what the
  code prints (`tests/test_docs_claims.py`).
- The **business brief** is tested for being self-contained (no external resources), a table view per chart, focusable marks
  and escaped text, and it was checked in a browser in light and dark themes, at desktop and phone width.

### Speed (measured by hand with `scripts/benchmark.py`, this container)
| Data | Load | Profile + links | Decide + write SQL | Run all cells | Total |
|---|---|---|---|---|---|
| Northwind (8 tables, 3,202 rows) | 0.80 s | 0.16 s | 0.02 s | 1.81 s (24 cells) | 2.8 s |
| 1,000,000 orders (1 table) | 2.83 s | 0.54 s | 0.47 s | 2.09 s (21 cells) | 5.9 s |

Writing the business brief takes under 10 ms. The million-row upload took 16.1 s before the speed work: profiling ran a
regular expression on every row of every text column, and loading stripped each value in Python. Both now work on the
**distinct** values and map the answers back, so a column with four regions costs four regex calls, not a million.

### Bugs found and fixed (good interview stories)
| Bug | How it was caught | Fix |
|---|---|---|
| `lines` used as an alias: reserved in MySQL (`LOAD DATA … LINES`) | the reserved-alias rule in `test_codegen_runs.py` | renamed `line_rows`; the reserved list covers MySQL 8 and PostgreSQL |
| a trailing `-- comment` swallowed the next comma in a SELECT list | MySQL / PostgreSQL parse test on a denormalised table | comments moved to their own line |
| `ROUND(double, 1)` does not exist in PostgreSQL | live PostgreSQL run | `ROUND((x)::numeric, k)` in the PostgreSQL dialect |
| `count / count` is 0 in PostgreSQL but a fraction in DuckDB | design review; now a test rule | every ratio is `1.0 * a / NULLIF(b, 0)` or `100.0 * …` |
| loading `order_details` before `orders` broke the foreign key | live PostgreSQL run | load in parent-first (topological) order |
| an org chart can't load into InnoDB (row-by-row FK checks on a self-reference) | design review for MySQL | `SET FOREIGN_KEY_CHECKS = 0` while loading; the orphans step re-checks afterwards |
| MySQL client output in latin-1 broke `Côte de Blaye` | live MySQL run | `--default-character-set=utf8mb4` in the README commands |
| `quarter_number` classified as an ID (it ends in `_number`) | the date-part consistency check never fired | date-part names are never identifiers |
| "average" and "fair" mapped to "okay" broke the Poor…Excellent scales | a test of every scale | synonyms only apply to the first scale |
| a unit *cost* treated as a list price | the shop fixture said 100% of prices differ | the list-price check needs "price" in the name |
| ISO timestamps with a `Z` stayed text | the awkward-dataset sweep | ISO 8601 parsed as UTC |
| with a quarter missing, `LAG` compared across the gap | a test case with a deleted quarter | `period_index` adjacency, and a year-ago join |
| a fact table loaded twice doubled revenue silently | a test case with every line twice | an ID pair unique after `drop_duplicates()` is an intended key, de-duplicated in the views |
| "145% of the change came from volume" | reading the New Wheels-shaped summary | effect shares by size |
| the unusual-period rule flagged pure noise | a simulation: 20 noise datasets | a baseline of up to 8 periods and a 99% prediction-interval limit |
| a falling rating with falling revenue was "not the cause" | a test where both fall | direction-aware verdicts: up is bad for days, good for ratings |
| `orders.ship_via` (1, 2, 3) profiled as a measure | the data dictionary showed it | a column the schema links to a key is an identifier |
| flat sales reported as "+187.8% year over year" | an interviewer-style audit: data starting on 20 February | the year-ago quarter had 40 of 90 days; year-ago values now come from complete periods only |
| identical daily sales read as "orders fell 9.7%" in February | the same audit | per-day comparison when period lengths differ by 3% or more |
| `$1,234.50`, `15%` and `(12.50)` loaded as text, so the measure vanished; one `-` did the same to a whole column | the audit's spreadsheet-export dataset | business number formats and placeholder markers, with the same 95% rule as dates |
| order value +8.3% coloured green, though its 95% interval runs from −20% to +37% | the audit | a confidence interval on every change, and a "within noise" chip |
| "80% from volume" depended on which effect was measured first | the audit | the Shapley split: 84%, exact, nothing left to assign |
| `NTILE(5)` gave 34 of 89 customers an F score decided by their id | the audit | `CEIL(5 × CUME_DIST())`: equal values, equal scores |
| r > 0.5 over 7 quarters called "move together" | the audit | Fisher-z intervals, on levels and on changes |
| "far from its recent trend" computed against a flat average | the audit | a trend prediction interval next to the level one |
| unshipped orders already past their due date left out of late % | the audit | they count as late at the extract date |
| cancelled and test orders counted as revenue; two currencies added up | the audit | business-rule filters, reconciled as their own row |
| totals that didn't reconcile still scored 88, "Good, with caveats" | the audit | grade caps |
| a table loaded twice got a two-column key, which broke its order lines' link | the cancelled-orders fixture | a single ID unique once the copies are removed is the key |
| the discount bands read a de-duplicated CTE they never defined, and gross vs net counted the duplicates | Northwind with four lines loaded twice | both read the same rows as the views |
| one mistyped `1.5` among fractions turned every discount into a percentage | the impossible-values fixture | percent only when most non-zero values are above 1; the `1.5` is reported as impossible |
| `AUTOSQL_PG_URL` ran every live case in one database | writing the CI workflow | one database per case on that server |
| a float cast through Arrow changed the last digit of some values | the golden snapshot | Arrow casts integers only |
| a segment that moved against the total read "moved most (−22% of the change)" | writing the docs, on the New Wheels-shaped drill-down | a negative share is never printed: the note says "against the overall fall" |
| the driver tree's note said the textbook split values order value "at the new number of orders", but its numbers (15,126.16 ≈ 120 × 126.05) use the old one | checking the note's arithmetic by hand | the note says "each effect at the old value of the other" |
| `table.column` marks written for the app showed as literal backticks in the HTML brief ("most gaps in \`orders.shipped_date\`") | opening the brief in a browser | `prose()` drops them from every sentence on the page, and a test reads four briefs for stray ones |
| the Northwind notebook never showed a chart: its setup imports `autosql`, which selects matplotlib's non-interactive Agg backend, so all nine `plt.show()` calls drew nothing | counting the image outputs of the committed notebook | `%matplotlib inline` after the import; the notebooks the app exports don't import `autosql` and always had their charts |

---

## 6. Likely interview questions with model answers

**Q: What is the grain of a table, and why does it matter?**
What one row represents: an order, an order line, a customer per day. Joining a 1:N table repeats the "one" side, so anything
stored there is summed several times. In Northwind, freight is 3.19× too high after joining order lines. I check grain with
`COUNT(*)` vs `COUNT(DISTINCT key)` before any KPI.

**Q: `COUNT(*)` vs `COUNT(col)` vs `COUNT(DISTINCT col)`?**
`COUNT(*)` counts rows; `COUNT(col)` skips NULLs; `COUNT(DISTINCT col)` skips NULLs and duplicates. Northwind: 830 orders,
809 with a ship date. That is why AVG(ship days) describes 809 orders.

**Q: `WHERE` vs `HAVING`?**
`WHERE` filters rows before grouping; `HAVING` filters groups after aggregation. `HAVING COUNT(*) > 1` is the duplicate check.
Filter early with `WHERE` when you can: it is cheaper.

**Q: Why is `NOT IN` dangerous?**
`x NOT IN (a, NULL)` means `x <> a AND x <> NULL`, and `x <> NULL` is unknown, so no row qualifies. Northwind: employees who
manage nobody, NOT IN 0 vs NOT EXISTS 7. Use `NOT EXISTS`, or filter NULLs out of the sub-query. The same logic is why the
business rules are written `status IS NULL OR status NOT IN ('Cancelled', …)`: without the `IS NULL`, every order with no
status would vanish.

**Q: `RANK`, `DENSE_RANK`, `ROW_NUMBER`?**
On ties: RANK 1, 1, 3; DENSE_RANK 1, 1, 2; ROW_NUMBER 1, 2, 3. For "top 3 per group" use ROW_NUMBER when exactly 3 rows are
needed. DENSE_RANK can return more, and the app reports when it does.

**Q: Why `CUME_DIST` and not `NTILE` for RFM?**
`NTILE(5)` cuts the ranked rows into five equal-sized buckets, so two customers with the same number of orders can get
different scores, decided by whatever breaks the tie (here, the customer id): 34 of 89 Northwind customers. `CUME_DIST` is
the share of rows at or below a value, so equal values share a score, at the price of unequal bucket sizes.
`ROUND(…, 9)` before `CEIL` stops 5 × 0.4 = 2.0000000000000004 from becoming 3.

**Q: How do you keep only the latest record per key?**
`ROW_NUMBER() OVER (PARTITION BY key ORDER BY updated_at DESC, id DESC)` in a CTE, then `WHERE row_num = 1` outside. The
tie-breaker makes it deterministic. MySQL has no `NULLS LAST`, so the portable form is `ORDER BY updated_at IS NULL, updated_at DESC`.

**Q: Why can't you filter on a window function in `WHERE`?**
`WHERE` runs before window functions are computed. Wrap the query in a CTE and filter outside it. `QUALIFY` does this in
DuckDB and Snowflake, but not in MySQL or PostgreSQL.

**Q: How do you compute a median in MySQL?**
There is no `PERCENTILE_CONT`. Number the rows with `ROW_NUMBER() OVER (ORDER BY v)` and count them with `COUNT(*) OVER ()`,
then average the rows at ranks `FLOOR((cnt + 1) / 2.0)` and `FLOOR((cnt + 2) / 2.0)`: one middle row for an odd count, two
for an even one. The `2.0` matters: MySQL's `/` returns a decimal (5 / 2 = 2.5) and PostgreSQL's integer `/` truncates
(5 / 2 = 2), so `FLOOR(… / 2.0)` is the form that gives the same rank in both. Northwind's median order is 943.25.

**Q: Average order value from an order-lines table?**
`SUM(revenue) / COUNT(DISTINCT order_id)`, not `AVG(line_revenue)`. Northwind: 1,525.05 vs 587.37.

**Q: Revenue dropped 50% this quarter. What do you check first?**
Whether the quarter is complete: MIN and MAX dates per period. Northwind's last quarter has 36 of 91 days, so the "−52.4%" is
missing days. Then compare complete periods (+64.3%) or the same quarter last year (+115.8%), and check that the year-ago
quarter is complete too: an incomplete one turns flat sales into "+188%".

**Q: Is the +8.3% in average order value real?**
Not shown by this data. Order values vary a lot from order to order, so with 120 and 182 orders the 95% interval of the change
runs from −20.3% to +36.9%, and it includes zero. The order count's change (+51.7%, 95% CI +20.4% to +91.0%) is real. So the
honest sentence is "more orders; order value about the same", and the brief shows the AOV tile as "within noise".

**Q: How do you find the root cause of a KPI change?**
Decompose it through its driver tree. Revenue = orders × AOV, and orders = customers × orders per customer. Then slice the
change by segment with conditional aggregation, and put the suspected driver next to the symptom in the same query.
Northwind: 84% of the growth is order volume, 57% of it Beverages, and ship time didn't move. Correlation is not proof:
confirm with a controlled test.

**Q: Why a Shapley split rather than the textbook one?**
The textbook split values each effect at the old value of the other (volume at the old order value, order value at the old
number of orders), and leaves an interaction term ("both") that someone has to assign. Measure volume first and Northwind reads 80% volume;
measure order value first and it reads 87%. Measuring each effect at the average of the other is the Shapley value of
the two-factor product: the two effects add up to the change exactly (+97,776.34 + 19,033.75 = +116,810.09) and the
answer doesn't depend on the order.

**Q: What makes a predicate sargable?**
The engine can use an index for it: the column stays bare. `order_date >= '1997-01-01' AND order_date < '1998-01-01'` is
sargable; `YEAR(order_date) = 1997` computes `YEAR()` for every row. Both return 408 rows on Northwind.

**Q: How do you read an `EXPLAIN` plan?**
- Look for full scans of large tables where you expected an index lookup.
- Look at the join algorithm and which side is the build side.
- Look at row estimates that are wildly off.
- Look at sorts and temporary tables.

In MySQL, `EXPLAIN FORMAT=TREE` (or `EXPLAIN ANALYZE` for actual timings); in PostgreSQL, `EXPLAIN (ANALYZE, BUFFERS)`.

**Q: Which columns do you index?**
Join keys and selective filter columns. InnoDB indexes foreign keys automatically; PostgreSQL does not. The app proposes
`CREATE INDEX` for every foreign key (both columns of a two-column one) and for the date filter.

**Q: Integer division?**
In PostgreSQL and SQL Server, `int / int` truncates: `37 / 809 = 0`. MySQL's `/` already returns a decimal. Write `100.0 * a / NULLIF(b, 0)`.
`NULLIF` also prevents division-by-zero errors.

**Q: `LEFT JOIN` then `WHERE right.col = 'x'`?**
That turns it into an inner join: unmatched rows have NULL there and fail the filter. Put the condition in the `ON` clause, or
test `IS NULL` deliberately (an anti-join). The app's tests forbid the accidental form.

**Q: CTE vs sub-query vs temporary table?**
A CTE names one step so the query reads top to bottom and each step can be tested alone. Modern MySQL 8 and PostgreSQL 12+
inline them like sub-queries. Use a temporary table or a view when a step is reused by many queries: here, the
`order_facts` view feeds most of the analysis cells.

**Q: How do you reconcile?**
The same total, computed independently, must match. The app compares the raw table, both views, and pandas over the uploaded
rows: 1,265,793.04 four times. A mismatch points at orphans (dropped by inner joins) or duplicated keys (multiplied by joins).
When business rules leave rows out, they get their own line, and raw = left out + kept is checked, not assumed.

**Q: Cancelled and test orders?**
They are not sales, so they are left out of every KPI, where rows enter the views, and the reconciliation shows what was left
out and its share of revenue. The rule is found from the data (a status, a flag, a currency column), stated in the
assumptions, and can be switched off in manual mode. A partially refunded order stays in: most of its revenue stands.

**Q: How do you load `$1,234.50`?**
Read every column as text first. If ≥ 95% of the values are numbers once a consistent currency sign, thousands separators,
`%` and accounting brackets are removed, store the numbers and keep the sign as the column's unit; markers like `-` or
`#N/A` become NULL and the rest are counted as unparsed. Two currencies in one column keep their numbers and get a
`_currency` column, and the analysis keeps one currency rather than adding them up.

**Q: MySQL vs PostgreSQL differences you hit?**
- `DATEDIFF(b, a)` vs `b - a`;
- `QUARTER(d)` vs `EXTRACT(QUARTER FROM d)`, `YEARWEEK(d, 3)` vs `EXTRACT(ISOYEAR FROM d)`;
- `ROUND(double, n)` needs `::numeric` in PostgreSQL;
- backticks vs double quotes;
- MySQL has no `FILTER`, `NULLS LAST`, `FULL JOIN` or `PERCENTILE_CONT`;
- `LOAD DATA` needs `NULLIF(@col, '')` for empty fields;
- InnoDB checks foreign keys row by row while loading.

**Q: Revenue jumped this quarter. How do you tell a manager whether it's real?**
Four checks, in order. Is the quarter complete (Northwind's last quarter has 36 of 91 days)? Does it hold year over year, so
it isn't seasonality (+115.8%)? Which lever moved: orders or order value (84% orders)? And is the change bigger than normal
variation? For that, a confidence interval on each change (revenue +64.3%, 95% CI +15.0% to +134.7%: real), and for a single
period against its history, a prediction interval from up to 8 earlier quarters and from their trend, not a fixed "2 sigma".
Then one sentence: the level, the change, the driver, the caveat.

**Q: Your dashboard says shipping time and revenue moved together. Is shipping the cause?**
Not proven. First check the direction: shipping can explain a fall only if it got *worse* while revenue fell. If it improved,
it can't be the cause, and the app says so. If the direction fits, it is a candidate cause, labelled "needs a test". The next
step is a controlled test, such as a faster shipping promise for a random half of orders. New Wheels was exactly this case: a
60% rise in shipping time alongside a 39% revenue fall, and I proposed a 25-day SLA with a test.

**Q: A user wants to add their own KPI. How do you let them without letting them break the analysis?**
Treat it as data, check it before it runs, and run it where it can only fail by itself. It is one aggregate expression that
the engine wraps in its own `SELECT … FROM order_facts`, so it sees the same orders and rules as every other KPI. The text is
validated (one expression, balanced, no second query, no call that waits or reads files), it runs in a locked in-memory
database, and its statements are *soft*: if it fails, the error shows next to that KPI and every other number is unchanged,
which a test proves by comparing a run with a broken KPI to a run without. It is labelled as theirs and its change is shown
without a verdict, because nobody tested it. And since the number is computed in DuckDB but the SQL is shipped to MySQL or
PostgreSQL, I warn about the traps I avoid in my own SQL: a division without `NULLIF`, `FILTER`, `::`.

**Q: Can a user change the assumptions?**
The wording, yes: `assumptions.edit` rewords any statement by key, `add` appends one, and both are marked as theirs. What the
numbers *are* (the fact table, the revenue formula, the date, the rows left out) they change by choosing it in manual mode,
and the assumptions follow, because the assumptions are written from the decision. Letting a free-text edit change a formula
would make the page say one thing and the SQL another.

**Q: How do you make a KPI trustworthy for people who can't read SQL?**
Write down the definition, the formula, which direction is good and the classic pitfall (the KPI dictionary). Reconcile the
total to the source file. Show a trust score that explains every deduction, and never let an average hide a failed check.
Say which changes are within noise. And trace every recommendation to the query that produced it, so anyone can rerun it.

---

## 7. Next steps (not built yet)
- Profile large CSVs with DuckDB's own reader instead of pandas, to go past the memory of one machine.
- A direct read-only connection to a live database, as well as uploads.
- Fiscal calendars (4-4-5 weeks, a year starting in April) and exchange rates, so that a second currency can be converted
  instead of left out.
- Forecasting the next period, and a correction for testing several KPIs at once.

---

## 8. Five-minute demo script
1. **(0:00)** `streamlit run app.py` → **Try the Northwind example**. Point at the counters: 8 tables, 7 foreign keys, 1 link to confirm.
2. **(0:40)** The diagram and the relationships table. Tick `orders.ship_via → shippers`: "values alone could be a coincidence, so it asks."
3. **(1:20)** Step 2: the engine's choices. Fact table `order_details`, one row per order line, revenue net of discount,
   quarters, 1998-Q2 partial. Open "How the engine reached this decision".
4. **(2:00)** **Run all SQL**. Cardinality: freight 64,942.69 vs 207,306.10. Reconcile: 1,265,793.04 everywhere.
5. **(2:50)** KPIs per quarter: the grey partial bar, "+64.3%, not −52.4%", and "Real change or noise?": orders real, order
   value within noise. Switch the sidebar to PostgreSQL: `DATEDIFF` becomes `shipped_date - order_date`, with no re-run.
6. **(3:40)** Driver tree waterfall: 84% volume, Beverages 57%. Drill-down: ship days didn't move.
7. **(4:00)** NULL traps: NOT IN 0 vs NOT EXISTS 7. Open a cell's 🔧 technical notes: the MySQL vs PostgreSQL lines side by side.
8. **(4:20)** Step 4: the headline, the KPI tiles (the AOV tile says "within noise"), "What to do" (the partial quarter comes
   first), trust 99/100. Open the brief preview: "this is what a manager gets".
9. **(4:45)** Download the project ZIP: schema, load, validate, analysis and performance scripts, cleaned data, a notebook,
   the data and KPI dictionaries and the brief. "CI runs this exact ZIP on PostgreSQL and MySQL on every push."
