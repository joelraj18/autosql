# Guide: AutoSQL Explorer

A guide for presenting this project. It has a 60-second pitch, the architecture, deep dives into each step with **measured**
results, interview questions with model answers, and a 5-minute demo script.

Every number on this page is printed by the code and checked by a test (`tests/test_northwind.py`,
`tests/test_new_wheels_shape.py`), unless it is marked as measured by hand (timings).

---

## 1. The 60-second pitch

> I write SQL that someone else can read, rerun and trust. AutoSQL Explorer turns that habit into a tool. You upload tables,
> and it learns the schema first: each table's key and grain, every foreign key, and how many rows join to one. It validates
> before it analyses: duplicates, NULLs, orphans, partial periods and join fan-out. Then a rule engine decides what to
> measure: the fact table, the revenue formula, the period and the drivers. It writes the SQL as small CTE steps in MySQL 8
> or PostgreSQL, runs it, and explains every step and every number in plain English.
>
> On Microsoft's Northwind sample it finds four classic traps on its own. Freight summed after joining order lines is
> 3.19× too high. The "latest quarter" looks like a 52% collapse, but it only has 36 days of data; the real change is +64%.
> The average line value understates average order value 2.6×. And NOT IN returns 0 rows where NOT EXISTS returns 7.
> Revenue reconciles to the cent three ways. The generated SQL is run on real PostgreSQL 16 and MySQL 8.4 in the tests, and
> every result is compared with the in-app engine.

**Where I have applied this.** On *New Wheels* (revenue and operations), I queried 1,000 orders from 984 customers across 49
U.S. states. I tracked quarterly revenue, orders and ratings, and linked a 39% revenue decline to a 60% rise in shipping time
over the same quarters (28 to 45 days). I proposed a 25-day shipping SLA, to be confirmed with a controlled test. The
`kpi_by_period` cell in this app is that query, generalised: the suspected driver sits next to the symptom, with `LAG` for
quarter-over-quarter change. `tests/test_new_wheels_shape.py` checks it on a synthetic table with the same shape.

---

## 2. Architecture

```
upload ─▶ loading.py ─▶ profiling.py ─▶ schema.py ─▶ decision.py ─▶ codegen.py ─▶ runner.py ─▶ narrative.interpret()
          text first,    roles, keys,    foreign keys,  fact, revenue,  20 cells ×      DuckDB, then   plain-English
          then types;    grain           cardinality,   periods,        3 dialects      each cell's    summary
          snake_case                     fan-out        drivers, dims   (dialect.py)    check → 📌
                                                                              │
                                                                              └─▶ exports.py: .sql · project ZIP · notebook
```

Design choices worth defending:

- **One statement, three dialects.** Each statement is a Python function of a `Dialect` and is rendered for MySQL 8 and
  PostgreSQL (shown) and DuckDB (run). Only quoting, date arithmetic, rounding and `EXPLAIN` differ. Everything else uses
  constructs all three accept, and `BANNED` lists what is never generated: `FILTER`, `NULLS LAST`, `QUALIFY`, `FULL JOIN`,
  `PERCENTILE_CONT`… Transpiling with a library would drop the comments that document assumptions.
- **Run here, prove there.** DuckDB is in-process, so the app needs no server. Because DuckDB is not PostgreSQL (its `/`
  never truncates), the tests export the project, run it on real PostgreSQL and MySQL, and compare about 200 query results
  with DuckDB.
- **Stories next to code.** Each cell's six-part story lives next to its SQL in `codegen.py`. The 📌 findings are written by a
  small Python check from the actual result, so the text can't drift from the numbers.
- **Safety.** Every name goes through `snake_case` and `Dialect.ident()`; every value goes through `literal()`, which doubles
  quotes and, in MySQL, backslashes. The DuckDB connection is locked (`enable_external_access = false`,
  `lock_configuration = true`), so generated SQL can't read local files. Each statement has a 60-second timeout.

---

## 3. Feature deep dives

### 3.1 Loading: text first
- **Problem:** `pd.read_csv` turns the postal code `05021` into the number 5021, and `7/4/1996` into a string.
- **Approach:** every column is read as text, then typed:
  - integers only when no value has a leading zero;
  - dates only when ≥ 95% parse with **one** format;
  - `m/d` vs `d/m` resolved by days above 12, else flagged as ambiguous;
  - ISO with a time zone stored as UTC.
- **Names:** become snake_case (`OrderID → order_id`), with the originals kept.
- **Result (Northwind):** 75 columns renamed; 18 postal codes kept their leading zeros; `order_date` became a real DATE
  (`1996-07-04`).

### 3.2 Keys and grain
- **Problem:** knowing what one row is (an order or an order line) is what stops a join from double-counting.
- **Single-column key:** unique and never empty, scored by name, position and type.
  - Northwind `customers`: `customer_id` beats `company_name`, which is also unique.
  - `shippers` has 3 rows and still gets its key: there is no minimum row count.
- **Otherwise, two ID columns together:** `order_details(order_id, product_id)`, one row per **order line**.
- **Otherwise, an ID unique in ≥ 90% of rows** is the key it was *meant* to be. Its duplicates are listed and removed with
  `ROW_NUMBER()`.
- **SQL proof:** `COUNT(*)` vs `COUNT(DISTINCT key)`; for a pair, `SELECT COUNT(*) FROM (SELECT DISTINCT a, b …)`, because
  PostgreSQL has no `COUNT(DISTINCT a, b)`.

### 3.3 Foreign keys: names first, values only as a suggestion
- **Rules:**
  - **name match** + ≥ 95% of values found → used;
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
| NULLs | `COUNT(*) - COUNT(col)` per column | `shipped_date` empty for 21 orders; `ship_region` 61.1% |
| dates | MIN/MAX, rows per period | 1996-07-04 to 1998-05-06; **1998-Q2 partial** (36/91 days) |
| orphans | `LEFT JOIN … WHERE parent IS NULL`, `NOT EXISTS` | 0 orphans; 2 customers never ordered (FISSA, PARIS) |
| reconciliation | raw table vs `order_facts` vs `line_facts` vs pandas on the upload | **1,265,793.04** everywhere |

### 3.6 Revenue and AOV at the right grain
- **Revenue** = `unit_price × quantity × (1 − discount)`, **net** of discount. Gross is 1,354,458.59; discounts 6.5%.
- **Price:** the line price, not the product's list price. They differ in **30.5%** of lines, and the reconciliation shows it.
- **AOV** = revenue ÷ `COUNT(DISTINCT order_id)` = **1,525.05**. The average *line* is 587.37 (2.6× smaller), and the KPI
  cell shows both.
- **Ship days** are averaged per **order** (8.49), not per line (8.35): big orders would otherwise count several times.

### 3.7 Trends without the partial-period trap
- **Rule:** the first and last period are partial when < 90% of their days have data. They are labelled `period_status =
  'partial'` and excluded from verdicts.
- **Measured:** naive "latest quarter" **−52.4%**; last two complete quarters (1997-Q4 → 1998-Q1) **+64.3%**; year over year
  (`LAG(revenue, 4)`) 1998-Q1 vs 1997-Q1 **+115.8%**.

### 3.8 Window functions
| Technique | Cell | Northwind |
|---|---|---|
| running total, `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` | `running_total` | half of all revenue reached by 1997-Q3 |
| top-N with `DENSE_RANK` next to `ROW_NUMBER` | `top_n` | Côte de Blaye leads Beverages (141,396.73); no ties |
| latest record with `ROW_NUMBER() … = 1` | `latest_record` | median 23 days since a customer's last order (to the last date in the data, not today) |
| `NTILE(5)` RFM | `rfm` | Champions: 22 of 89 customers, 52.2% of revenue |

### 3.9 Root cause: driver tree and drill-down
- **Arithmetic:** revenue = orders × AOV. For two complete periods, volume = ΔO·A₁, order value = O₁·ΔA, interaction = ΔO·ΔA.
  The three sum to the change exactly.
- **Measured:** +116,810.09 = **+93,868.75** (orders 120 → 182) + 15,126.16 (AOV 1,514.01 → 1,640.06) + 7,815.18. That is
  **about 80% volume**.
- **By segment:** conditional aggregation puts both periods side by side; Beverages = 57% of the change.
- **Drill-down:** puts the driver next to the symptom. Ship days moved −6.5% while revenue moved +64%, so shipping is **not**
  the cause here.
- **New Wheels-shaped test table** (synthetic): revenue falls 17% while ship days rise 15% (40 → 46). The app calls it a
  candidate cause and says to confirm it with a controlled test.

### 3.10 Fast and correct
| Point | Measured on Northwind |
|---|---|
| `NOT IN` vs `NOT EXISTS` (employees who manage nobody; `reports_to` has one NULL) | **0 vs 7** |
| `COUNT(*)` vs `COUNT(shipped_date)` | 830 vs 809: AVG skips 21 NULLs |
| `YEAR(order_date) = 1997` vs `order_date >= '1997-01-01' AND < '1998-01-01'` | both 408 rows; only the range can use an index |
| integer division | `2 / 3` = 0 in PostgreSQL (shown in the export; confirmed on PostgreSQL 16), 0.6667 in MySQL |
| `EXPLAIN` | Projection → Perfect Hash Group By → Hash Join → Seq Scan |

---

## 4. Data
- **Northwind** (`examples/northwind/`): built by `build_data.py` from Microsoft's `instnwnd.sql` at a pinned commit (MIT
  licence). Each INSERT is evaluated with SQLite after removing T-SQL's `N''` prefix and the hex image data. 8 tables, 3,202
  rows.
- **Test fixtures** (`tests/conftest.py`):
  - a **synthetic** New Wheels-shaped table (1,000 orders before thinning, 49 states, text feedback, a planted shipping
    slowdown);
  - a messy normalised shop (duplicate customer keys, 3 orphan lines, leading-zero zips, columns named `order` and `group`);
  - timestamps, list prices, a denormalised line table, and reserved-word names.

---

## 5. Performance and quality

### Tests
- `pytest -q`: about 80 tests.
- **Every generated cell runs** on 10 dataset shapes in 2 modes.
- Every MySQL / PostgreSQL statement **parses** (`sqlglot`).
- **Dialect rules are enforced:**
  - no banned construct;
  - no reserved alias;
  - every `/` guarded;
  - PostgreSQL `ROUND` on `::numeric`;
  - no `WHERE` on a LEFT-joined table.
- The exported **notebook executes**, and the **Streamlit page** is driven headlessly.
- **Live databases:** the exported project runs on **PostgreSQL 16** (`test_live_postgres.py`) and **MySQL 8.4 in Docker**
  (`test_live_mysql.py`) for 9 dataset shapes. About 200 query results are compared with DuckDB, and all match.

### Speed (measured by hand, this container)
| Data | Load | Profile + links | Decide + write SQL | Run all cells | Total |
|---|---|---|---|---|---|
| Northwind (8 tables, 3,202 rows) | 0.80 s | 0.35 s | 0.03 s | 1.47 s (20 cells) | 2.7 s |
| 1,000,000 orders (1 table) | 9.1 s | 6.8 s | 0.44 s | 1.47 s (18 cells) | 17.8 s |

DuckDB makes the SQL itself fast; at a million rows, reading and profiling the CSV in pandas dominates.

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
manage nobody, NOT IN 0 vs NOT EXISTS 7. Use `NOT EXISTS`, or filter NULLs out of the sub-query.

**Q: `RANK`, `DENSE_RANK`, `ROW_NUMBER`?**
On ties: RANK 1, 1, 3; DENSE_RANK 1, 1, 2; ROW_NUMBER 1, 2, 3. For "top 3 per group" use ROW_NUMBER when exactly 3 rows are
needed. DENSE_RANK can return more, and the app reports when it does.

**Q: How do you keep only the latest record per key?**
`ROW_NUMBER() OVER (PARTITION BY key ORDER BY updated_at DESC, id DESC)` in a CTE, then `WHERE row_num = 1` outside. The
tie-breaker makes it deterministic. MySQL has no `NULLS LAST`, so the portable form is `ORDER BY updated_at IS NULL, updated_at DESC`.

**Q: Why can't you filter on a window function in `WHERE`?**
`WHERE` runs before window functions are computed. Wrap the query in a CTE and filter outside it. `QUALIFY` does this in
DuckDB and Snowflake, but not in MySQL or PostgreSQL.

**Q: Average order value from an order-lines table?**
`SUM(revenue) / COUNT(DISTINCT order_id)`, not `AVG(line_revenue)`. Northwind: 1,525.05 vs 587.37.

**Q: Revenue dropped 50% this quarter. What do you check first?**
Whether the quarter is complete: MIN and MAX dates per period. Northwind's last quarter has 36 of 91 days, so the "−52.4%" is
missing days. Then compare complete periods (+64.3%) or the same quarter last year (+115.8%).

**Q: How do you find the root cause of a KPI change?**
Decompose it through its driver tree. Revenue = orders × AOV, and orders = customers × orders per customer. Then slice the
change by segment with conditional aggregation, and put the suspected driver next to the symptom in the same query. Northwind:
80% of the growth is order volume, 57% of it Beverages, and ship time didn't move. Correlation is not proof: confirm with a
controlled test.

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
`CREATE INDEX` for every foreign key and for the date filter.

**Q: Integer division?**
In PostgreSQL and SQL Server, `int / int` truncates: `37 / 809 = 0`. MySQL's `/` already returns a decimal. Write `100.0 * a / NULLIF(b, 0)`.
`NULLIF` also prevents division-by-zero errors.

**Q: `LEFT JOIN` then `WHERE right.col = 'x'`?**
That turns it into an inner join: unmatched rows have NULL there and fail the filter. Put the condition in the `ON` clause, or
test `IS NULL` deliberately (an anti-join). The app's tests forbid the accidental form.

**Q: CTE vs sub-query vs temporary table?**
A CTE names one step so the query reads top to bottom and each step can be tested alone. Modern MySQL 8 and PostgreSQL 12+
inline them like sub-queries. Use a temporary table or a view when a step is reused by many queries: here, the
`order_facts` view feeds 10 cells.

**Q: How do you reconcile?**
The same total, computed independently, must match. The app compares the raw table, both views, and pandas over the uploaded
rows: 1,265,793.04 four times. A mismatch points at orphans (dropped by inner joins) or duplicated keys (multiplied by joins).

**Q: MySQL vs PostgreSQL differences you hit?**
- `DATEDIFF(b, a)` vs `b - a`;
- `QUARTER(d)` vs `EXTRACT(QUARTER FROM d)`;
- `ROUND(double, n)` needs `::numeric` in PostgreSQL;
- backticks vs double quotes;
- MySQL has no `FILTER`, `NULLS LAST` or `FULL JOIN`;
- `LOAD DATA` needs `NULLIF(@col, '')` for empty fields;
- InnoDB checks foreign keys row by row while loading.

---

## 7. Next steps (not built yet)
- Profile large CSVs with DuckDB's own reader instead of pandas: loading and profiling dominate at a million rows.
- Composite foreign keys (e.g. `(region, store_no)`).
- A direct read-only connection to a live database, as well as uploads.
- Forecasting the next period, and statistical tests for "is this change significant?" (see the Statistics & A/B Testing
  project).

---

## 8. Five-minute demo script
1. **(0:00)** `streamlit run app.py` → **Try the Northwind example**. Point at the counters: 8 tables, 7 foreign keys, 1 link to confirm.
2. **(0:40)** The diagram and the relationships table. Tick `orders.ship_via → shippers`: "values alone could be a coincidence, so it asks."
3. **(1:20)** Step 2: the engine's choices. Fact table `order_details`, one row per order line, revenue net of discount,
   quarters, 1998-Q2 partial. Open "How the engine reached this decision".
4. **(2:00)** **Run all SQL**. Cardinality: freight 64,942.69 vs 207,306.10. Reconcile: 1,265,793.04 everywhere.
5. **(2:50)** KPIs per quarter: the grey partial bar, "+64.3%, not −52.4%". Switch the sidebar to PostgreSQL: `DATEDIFF`
   becomes `shipped_date - order_date`, with no re-run.
6. **(3:40)** Driver tree waterfall: 80% volume, Beverages 57%. Drill-down: ship days didn't move.
7. **(4:20)** NULL traps: NOT IN 0 vs NOT EXISTS 7. Step 4 summary.
8. **(4:45)** Download the project ZIP: schema, load, validate, analysis and performance scripts, cleaned data and a notebook.
   "The tests run this exact ZIP on PostgreSQL and MySQL."
