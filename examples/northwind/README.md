# Northwind: sales analysis in SQL

Microsoft's Northwind sample: a small food importer with **8 tables**, 91 customers, 77 products, **830 orders** and
**2,155 order lines** from 4 July 1996 to 6 May 1998.

| File | What it is |
|---|---|
| `Northwind_sales_analysis.ipynb` | **The worked example**, already run so the outputs are saved. Part 1 runs four classic SQL mistakes next to the correct query. Part 2 is the full AutoSQL Explorer analysis, with the MySQL 8 and PostgreSQL version of every query |
| `build_notebook.py` | regenerates and runs the notebook: `python examples/northwind/build_notebook.py` (needs `requirements-dev.txt`) |
| `build_data.py` | rebuilds the CSVs from Microsoft's install script at a pinned commit |
| `*.csv` | the 8 tables, with the original names (`Order Details.csv`, `OrderID`), so that the app's renaming is exercised |
| `expected_fks.json` | the 8 FOREIGN KEY constraints declared in Microsoft's script: the ground truth for the relationship tests |
| `NOTICE.md` | source and MIT licence |

## What the engine finds on its own

| Step | Result |
|---|---|
| Keys | every table has a single-column key except `order_details`: (`order_id`, `product_id`), one row per order line |
| Relationships | **7 found** (6 by name + `employees.reports_to`, a self-reference); `orders.ship_via → shippers.shipper_id` is **suggested only**, because the codes {1, 2, 3} are also valid IDs in four other tables. Found + ticked = the 8 constraints in Microsoft's script |
| Fan-out | `orders.freight` totals 64,942.69, but **207,306.10** after joining order lines (3.19×) |
| Orphans | none; but **2 customers never ordered** (FISSA, PARIS) |
| NULLs | `shipped_date` is empty for **21** orders (not shipped yet), so average ship days covers 809 orders |
| Periods | quarters from 1996-Q3 to 1998-Q2; **1998-Q2 is partial** (36 of 91 days) |
| Revenue | `unit_price × quantity × (1 − discount)` = **1,265,793.04**, identical in the raw table, both views and the uploaded file (gross 1,354,458.59; discounts 6.5%). The line price is used because it differs from today's list price in 30.5% of lines |

## What the analysis shows

| Question | Answer |
|---|---|
| How big? | 830 orders, average order value **1,525.05** (the average *line* is only 587.37), 89 customers, 98.9% of them ordered more than once |
| Trend | last two complete quarters (1997-Q4 → 1998-Q1): **+64.3%**; year over year 1998-Q1: **+115.8%**. A naive "latest quarter" says **−52.4%**, but that quarter has only 36 days of data |
| What drove it | of the +116,810.09 change, **+93,868.75** comes from more orders (120 → 182), +15,126.16 from a higher order value (1,514.01 → 1,640.06), +7,815.18 from both: **about 80% volume** |
| Where | Beverages contributed 57% of the change; by country, the USA 21% |
| Shipping | ship days moved 9.2 → 8.6 (−6.5%) while revenue rose 64%: shipping time does **not** explain this change |
| Customers | Champions (22 of 89 customers) bring 52.2% of revenue; 43 customers first ordered in 1996-Q3 |
| Correctness | `NOT IN` finds 0 employees who manage nobody; `NOT EXISTS` finds 7, because `reports_to` is NULL for the top manager |

Every number above is printed by the notebook, and checked by `tests/test_northwind.py`. The exported project gives the same
results on PostgreSQL 16 and MySQL 8.4 (`tests/test_live_postgres.py`, `tests/test_live_mysql.py`).
