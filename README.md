# AutoSQL Explorer

Upload your tables (CSV, Excel or a ZIP of them). AutoSQL Explorer then does four things:

1. **It learns the schema.** For every table it finds the key and the grain (what one row is: an order, an order line, a
   customer), every foreign key, and how many rows join to one.
2. **It validates before it analyses, and decides what to measure.**
   - Validation covers duplicates, NULLs, orphans, partial periods and join fan-out.
   - A transparent rule engine then picks the fact table, the revenue formula, the period, the drivers and the dimensions.
   - You choose how much it decides: **let the engine decide**, **choose yourself**, or **just validate the data**.
3. **It writes the SQL in MySQL 8 or PostgreSQL and runs it live.** The SQL is a set of small, testable CTE steps, following one
   method: schema & grain → validate → build with CTEs → KPIs → window functions → root cause → fast & correct.
4. **It explains every step and every result**, in plain English, from the actual numbers.

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

A **Beginner / Expert** switch hides the explanations. Under each cell you can also see the DuckDB version that actually ran in
the app, and the code that writes the 📌 findings.

## Run it

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then upload your files, or press **🧭 Try the Northwind example**.

## Your SQL method, step by step

| Step | What the app does | Cells |
|---|---|---|
| **1. Learn the schema and the grain** | keys (single, composite, or meant-to-be-unique with duplicates), the grain sentence, foreign keys found by name, by self-reference (`reports_to`) or *suggested* by matching values, cardinality, an ER diagram | `schema` (CREATE TABLE with PK/FK, tested on an empty database), `grain` (`COUNT(*)` vs `COUNT(DISTINCT key)`), `cardinality` (children per parent + a fan-out demo) |
| **2. Validate before analysing** | duplicates with `GROUP BY … HAVING COUNT(*) > 1`; NULLs per column (`COUNT(*) - COUNT(col)`); date ranges and **partial periods**; orphans in both directions (`LEFT JOIN … IS NULL`, `NOT EXISTS`); totals **reconciled** three ways, including against the uploaded file | `duplicates`, `nulls`, `date_range`, `orphans`, `reconcile` |
| **3. Build in readable steps with CTEs** | two views at their true grain: `order_facts` (one row per order; lines rolled up **before** order-level tables are joined) and `line_facts` (one row per line); business names; LEFT JOIN + `'Unknown'` so that nothing is dropped | `base` |
| **4. Turn tables into KPIs** | revenue, orders, **AOV = revenue ÷ COUNT(DISTINCT order)** (with the average *line* value next to it), repeat-customer rate, units per order, ship days, late %, rating; per period with `LAG` QoQ and YoY % (your New Wheels query, generalised) | `kpis`, `kpi_by_period` |
| **5. Window functions for trends and ranking** | running totals (`ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`); top-N per group with `DENSE_RANK` next to `ROW_NUMBER` (ties reported); the latest record per customer with `ROW_NUMBER`; RFM segments with `NTILE(5)` | `running_total`, `top_n`, `latest_record`, `rfm` |
| **6. Drill down to the root cause** | the revenue change split into orders × average order value (and customers × orders per customer), then by segment with conditional aggregation; the suspected driver next to the symptom, per period and per segment; cohorts | `driver_tree`, `drilldown`, `cohort` |
| **7. Keep it fast and correct** | `EXPLAIN` read for you; a non-sargable `YEAR(date) = …` vs a sargable range; `CREATE INDEX` proposals; `NOT IN` vs `NOT EXISTS` with a NULL, `COUNT(col)` skipping NULLs, integer division in PostgreSQL; every assumption written down | `explain`, `null_traps` |

## What you can download

| Download | Contents |
|---|---|
| `.sql` script | every step in MySQL 8 or PostgreSQL, with its explanation as comments and the assumptions at the top |
| **Project `.zip`** | `01_schema.sql` (CREATE TABLE with keys), `02_load.sql` (`LOAD DATA LOCAL INFILE … NULLIF(@col, '')` or `\copy`), `03_validate.sql`, `04_analysis.sql`, `05_performance.sql`, the cleaned `data/*.csv` (snake_case headers, ISO dates, empty = NULL), `analysis.ipynb` and a README. Rerunnable on a real database |
| Notebook `.ipynb` | the same steps in Python + DuckDB, with every explanation |

## Project layout

| File | What it does |
|---|---|
| `app.py` | Streamlit page layout only |
| `autosql/loading.py` | reads CSV / Excel / ZIP as text, types each column (leading zeros kept, dates parsed), renames to snake_case, builds a locked in-memory DuckDB |
| `autosql/profiling.py` | gives each column a *role* (`identifier`, `measure`, `rating`, `datetime`, `date_part`, `dimension`, `attribute`, `text`, `constant`) and finds each table's primary key and grain |
| `autosql/schema.py` | foreign keys, cardinality, orphans, fan-out risks, the ER diagram (Graphviz and Mermaid) |
| `autosql/decision.py` | the decision engine: focus → fact table and grain → revenue → time → drivers → dimensions → correctness. Every choice is recorded as a `Step` |
| `autosql/dialect.py` | the places where MySQL 8, PostgreSQL and DuckDB differ (quoting, dates, rounding, division, types) and the constructs never generated |
| `autosql/codegen.py` | turns a decision into 20 self-explaining SQL cells, each rendered for the three dialects |
| `autosql/runner.py` | runs the cells on DuckDB (with a timeout), then their checks, and captures results, charts and findings |
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
  │          names → snake_case (OrderID → order_id)
  │
  ├─ Profile ── column roles; primary key: single › two IDs together › an ID with duplicates (de-duplicated) › none
  │
  ├─ Relationships ── same name + ≥ 95% of values found      ──▶ used
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
       period          = > 5 years: year │ ≥ 4 complete quarters: quarter │ else month
                         first / last period with < 90% of its days ──▶ partial: shown, never compared
       drivers         = ship date − order date (ship_days) · shipped after required date · rating (text scales mapped 1-5)
       dimensions      = ≤ 3: geography › product › channel › fulfilment › people (≤ 2 joins away, ≤ 30% NULL)
       no money and no date ──▶ falls back to validation
```

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

**Presenting this project?** [docs/guide.md](docs/guide.md) is an interview guide. It has a 60-second pitch, the architecture,
deep dives with measured results, SQL interview questions with answers, and a 5-minute demo script.

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

The tests:
- check loading, column roles, keys and every decision branch;
- prove that the relationships inferred for Northwind match the 8 FOREIGN KEY constraints in Microsoft's script;
- **run every generated cell** on 10 dataset shapes (including reserved-word names, timestamps, list prices, orphans and
  duplicates);
- parse every MySQL and PostgreSQL statement with `sqlglot`;
- enforce the dialect rules;
- execute the exported notebook;
- drive the Streamlit page headlessly.

Two suites run the **exported project on real databases** and compare every query result with DuckDB:

```bash
pytest tests/test_live_postgres.py   # starts a throw-away PostgreSQL from a local install (or uses AUTOSQL_PG_URL)
docker run -d --name autosql-mysql -e MYSQL_ROOT_PASSWORD=autosql mysql:8.4 --local-infile=1
AUTOSQL_MYSQL_CONTAINER=autosql-mysql pytest tests/test_live_mysql.py
```
