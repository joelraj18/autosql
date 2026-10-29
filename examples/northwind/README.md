# Northwind: sales analysis in SQL

Microsoft's Northwind sample: a small food importer with **8 tables**, 91 customers, 77 products, **830 orders** and
**2,155 order lines** from 4 July 1996 to 6 May 1998.

| File | What it is |
|---|---|
| `Northwind_sales_analysis.ipynb` | **The worked example**, already run so the outputs and charts are saved. Part 1 runs four classic SQL mistakes next to the correct query. Part 2 is the full AutoSQL Explorer analysis, with the MySQL 8 and PostgreSQL version of every query. Part 3 is the business reading: headline, KPI tiles, recommendations and the data-trust score |
| `business_brief.html` | **The one-page business brief** the notebook (and the app) writes: open it in any browser, offline |
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
| How big? | 830 orders, average order value **1,525.05** (the average *line* is only 587.37; the median order is 943.25 and 90% of orders are below 3,160.60), 89 customers, 98.9% of them ordered more than once |
| Trend | last two complete quarters (1997-Q4 → 1998-Q1): **+64.3%** (95% CI +15.0% to +134.7%, so real); year over year 1998-Q1: **+115.8%**. A naive "latest quarter" says **−52.4%**, but that quarter has only 36 days of data |
| What drove it | of the +116,810.09 change, **+97,776.34** comes from more orders (120 → 182) and +19,033.75 from a higher order value (1,514.01 → 1,640.06), each measured at the average of the other: **84% volume**. Orders +51.7% is real; order value +8.3% is within normal variation (95% CI −20.3% to +36.9%) |
| Where | Beverages contributed 57% of the change; by country, the USA 21% |
| Shipping | ship days moved 9.2 → 8.6 (−6.5%) while revenue rose 64%: shipping time does **not** explain this change. Over 7 complete quarters the two show no relationship either (levels r = +0.02, 95% CI −0.74 to +0.76) |
| Customers | Champions (24 of 89 customers) bring 54.4% of revenue; 43 customers first ordered in 1996-Q3 |
| Correctness | `NOT IN` finds 0 employees who manage nobody; `NOT EXISTS` finds 7, because `reports_to` is NULL for the top manager |

## The business reading

From [`business_brief.html`](business_brief.html), ordered by urgency:

| Priority | Recommendation | Evidence | Next step |
|---|---|---|---|
| Act now | 1998-Q2 is incomplete | 36 of 91 days; a naive report would show −52.4% | report it only once complete, or against the same days a year earlier |
| Plan | Revenue grew 64.3% in 1998-Q1 | 95% CI +15.0% to +134.7%; +115.8% year over year, so not seasonal | headline the last complete quarter; plan on the year-over-year figure |
| Plan | 1998-Q1 is far above its recent level and trend | z = +4.8 against 6 earlier quarters (99% limit 4.36); their trend predicted 196,668.16 | check the largest orders of the quarter before planning on it |
| Plan | Order volume accounts for 84% of the movement | orders +51.7% (real); order value +8.3% (within normal variation) | plan capacity for the volume |
| Plan | Beverages drove 57% of the change | +66,503.58 by category | find out whether it can last |
| Plan | The top 20% of customers bring 60% of revenue | class A: 34 customers (38% of them) = 80% of revenue | named account owners for class A |
| Plan | 5 repeat customers are overdue | silent for more than twice their usual gap (59 days) | win-back calls, largest first |
| Plan | 7 once-frequent customers are slipping | RFM "At risk": 4.6% of revenue | reactivation offer |
| Plan | Discounts cost 6.5% of gross revenue | 88,665.55 on 39% of lines; discounted lines are 25% bigger | test discount levels before changing the policy |
| Monitor | Ship days did not explain the change | −6.5% while revenue +64.3% | look at the driver tree instead |

**Data trust: 99/100, High.** The only deduction: `orders.shipped_date` is 2.5% empty (21 unshipped orders).

Every number above is printed by the notebook: `tests/test_docs_claims.py` fails if one isn't, and `tests/test_northwind.py` and
`tests/test_business.py` pin the important ones. The exported project gives the same results on PostgreSQL 16 and MySQL 8.4
(`tests/test_live_postgres.py`, `tests/test_live_mysql.py`, run by CI on every push).
