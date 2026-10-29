# AutoSQL Explorer: decision tree specification

This page documents **exactly** how the engine turns uploaded tables into SQL. Every threshold below is a constant in the code:

- `autosql/loading.py`: reading files, typing columns, names, the DuckDB database
- `autosql/profiling.py`: column roles, primary keys, grain
- `autosql/schema.py`: foreign keys, cardinality, fan-out, diagrams
- `autosql/decision.py`: focus, fact table, revenue, business rules, time, drivers, dimensions, NULL traps, indexes, skipped cells
- `autosql/checklib.py`: period labels and indexes, t quantiles, confidence intervals, the trend prediction interval
- `autosql/dialect.py`: how one statement differs between MySQL 8, PostgreSQL and DuckDB
- `autosql/codegen.py`: the cells each branch generates; `autosql/narrative.py`: wording thresholds in `interpret()`
- `autosql/business.py`, `autosql/report.py`: the trust score, recommendations and the business brief

If you change a constant in the code, update this page too.

## Stage L: Loading (once per upload, cached)

### L1. Files
| Input | Becomes |
|---|---|
| `.csv`, `.tsv`, `.txt` | one table (encoding `utf-8-sig`, then `latin-1`) |
| `.xlsx`, `.xls` | one table per non-empty sheet, named `<file>_<sheet>` when there are several |
| `.zip` | each CSV / Excel member as above. **Limits:** ≤ **50** members, ≤ **500 MB** unzipped; `__MACOSX/` and hidden files skipped; read in memory, never extracted |
| anything else, or unreadable | skipped, with a warning and a `skipped_file` story |

### L2. Types (every column is read as **text first**)
Blank, `NULL`, `NA`, `N/A`, `\N`, `None`, `nan` → NULL. Then, first match wins:

| # | Condition on the non-empty values | Type |
|---|---|---|
| 1 | all whole numbers, **no leading zero** (`05021` is a code), ≤ 18 digits | `integer` |
| 2 | all numbers, no leading zero; whole numbers written `5.0` | `integer` |
| 3 | all numbers with ≤ 4 decimals | `decimal` (money) |
| 4 | other numbers | `float` |
| 5 | only `true` / `false` | `boolean` |
| 6 | ≥ **95%** (`NUMBER_PARSE_SHARE`) are numbers written the business way: a currency sign or code used as a prefix or suffix (`$ € £ ¥ ₹`, `USD`, `EUR`…), thousands separators (`1,234.50`; `1.234,50` in a decimal-comma file), `15%`, accounting negatives `(12.50)` | `integer` / `decimal` as above. The sign is kept as the column's unit; two or more currencies keep their numbers and get a `<column>_currency` column; the rest become NULL and are counted (`unparsed_numbers`) |
| 7 | ≥ **95%** of a 500-value sample parses with **one** format (ISO, ISO with time, `m/d/Y`, `d/m/Y`, `Y/m/d`, `d-m-Y`, `d.m.Y`, `d Mon Y`, …), or as ISO 8601 with a time zone (stored as UTC) | `date` if every time is midnight, else `timestamp` |
| 8 | otherwise | `text` |

Before rows 1–7, spreadsheet markers for "no value" (`-`, `—`, `?`, `#N/A`, `#VALUE!`, `#DIV/0!`, `n.a.`, `nil`, `none`,
`missing`, `unknown`, `tbd`…, `PLACEHOLDERS`) are set aside: a number or date column becomes NULL there (`placeholders_empty`
story), and a text column keeps them. All tests run on the column's **distinct** values, then map back.

- If both `m/d` and `d/m` parse every value (no day above 12), `m/d` is used and an `ambiguous_dates` story warns.
- Values that look like dates but don't parse become NULL and are counted (`unparsed_dates`).
- Leading zeros, codes and IDs stay text, so `05021` round-trips unchanged into the exports.

### L3. Names
- Columns and tables become `snake_case`: `OrderID → order_id`, `Order Details → order_details`, `HTTPStatus → http_status`,
  `Größe → grosse`, `2nd value → c_2nd_value`.
- A name with no Latin letters or digits (`城市`) becomes `column_<position>`.
- Collisions get `_2`, `_3` (`Order ID`, `order_id`, `ORDER-ID` → `order_id`, `order_id_2`, `order_id_3`).
- The original names are kept (`renamed from` in the profile, `renamed` story, export header).

### L4. Database
- Each run builds a fresh in-memory DuckDB with one typed table per upload.
- Then `SET enable_external_access = false` and `SET lock_configuration = true`, so generated SQL can't read or write local files.
- Each statement times out after **60 s** (`con.interrupt()`).

---

## Stage P: Profiling

### P1. Column roles (`profile_column`), first match wins
| # | Condition | Role |
|---|---|---|
| 1 | ≤ 1 distinct non-null value | `constant` |
| 2 | type `date` / `timestamp` | `datetime` |
| 3 | type `boolean` | `dimension` |
| 4 | integer or text **and** name matches `KEY`¹ **and** not `NOT_KEY`² **and** not a date part | `identifier` |
| 5 | number named like a phone / postal / extension | `attribute` |
| 6 | integer 0–10 named like `rating`, `score`, `stars`, `review`, `feedback`, `satisfaction`, `csat`, `nps` | `rating` (`1-5` or `1-10`) |
| 7 | integer named `year`, `quarter`, `month`, `week`, `day` (`_number` / `_num` / `_no`) | `date_part` |
| 8 | integer with exactly the values 0 and 1 | `dimension` (flag) |
| 9 | any other number | `measure`, with a **kind**: `discount` › `quantity` › `price` › `charge` (freight, shipping, tax, fee…) › `amount` (amount, revenue, sales, total, net, gross…, but not names containing count / num / qty / id / pct / rate / share) › `other`. A discount is **in percent** (5 = 5%) when it was written with `%`, or when most of its non-zero values are above 1, so one mistyped `1.5` among fractions doesn't rescale the column |
| 10 | text where ≥ 95% of values belong to one ordered scale³ (≥ 3 of its labels present) | `rating` (`scale`, mapped to 1–5) |
| 11 | text averaging > **30** characters with spaces in > 50% of values | `text` |
| 12 | text named like contact or descriptive details (phone, fax, email, address, postal, zip, url, description, notes, comment) | `attribute` |
| 13 | text with > **50** distinct values | `attribute` |
| 14 | otherwise | `dimension` |

¹ `KEY`: names ending in `id`, `key`, `code`, `no`, `num`, `number`, `uuid`, `guid`, `sku`, or starting with `id_`.
² `NOT_KEY`: names containing postal, zip, phone, fax, email, address, url, path, area_code.
³ Scales: very bad…very good (with `ok` / `average` / `fair` read as `okay`), very poor…excellent, poor…excellent,
very dissatisfied…very satisfied, strongly disagree…strongly agree, very unlikely…very likely.

### P2. Primary key and grain (`find_primary_key`), checked in this order
There is **no minimum row count**: a 3-row lookup table still gets its key.

1. **Single column:** unique and never empty, integer or text, with a score ≥ **2**. The score is:
   - **+4** named `<table_singular>_id` / `_key` / `_code` / `_no` or `id`;
   - **+3** identifier role;
   - **+2** first column;
   - **+1** integer;
   - **−5** named like a name, title, phone, email, address, postal code or description.

   The best score wins; ties go to the leftmost column. Northwind `customers`: `customer_id` (score 9) beats `company_name`,
   which is unique too.
2. **Two identifier columns together** (≤ **15** candidates, every pair) → `composite`. Example: `order_details(order_id, product_id)`.
3. When the table has **exact copies** (a double load): one identifier unique once they are removed (score ≥ 2, as in 1),
   else two together → `intended`. A table of orders loaded twice keeps `order_id`, so its order lines still link to it.
4. An identifier unique in ≥ **90%** of its non-empty rows → `intended`. It is the key the table was meant to have; its
   duplicates are listed and removed with `ROW_NUMBER()`.
5. An identifier, dimension or date together with another → `composite` (one row per store per day).
6. Otherwise `none`: exact duplicate rows are counted instead.

The **grain sentence** comes from the key: "one row per `order_id` × `product_id`".

---

## Stage S: Relationships (`infer_schema`)

Single-column keys (`single` or `intended`) and two-column `composite` keys can be pointed at.

| Kind | Condition | Result |
|---|---|---|
| `name` | child column = the parent key's name, or `<parent_singular>_<key>`, or `<parent>_<key>` (and `<parent_singular>_id` when the key is `id`); containment ≥ **95%** of distinct values | **accepted** |
| `name` | as above but containment < 95% | suggested only (`fk_low`): "possibly a different code list" |
| `name`, composite | the parent is keyed by two columns (`stores`: `region` + `store_no`) and the child holds both, under the same names, as integer or text, without being keyed by the same pair; ≥ **95%** of its distinct **pairs** exist in the parent | **accepted**, joined on both columns (`Relationship.on()`). Below 95%: suggested only |
| `self` | name `reports_to`, `manager(_id)`, `parent(_id)`, `parent_*_id`, `supervisor(_id)`, `boss(_id)`, `managed_by`; same type; every value is a key of the same table | **accepted** |
| `value` | no name match; child is an identifier **or** shares a stem of ≥ **4** characters with the parent table name; **every** value exists in the parent key; **and** the two value sets are equal **or** the stem matches. Best candidate only (set equality › stem length › smaller parent) | **suggested only**: the user ticks it |

- Keys are compared as trimmed text, so `7` matches `'7'`; a type mismatch adds a `type_mismatch` story, and the generated joins
  cast both sides to text.
- When both tables are keyed by the column (1:1), the bigger table is the parent.

A composite link is used for the DDL (`FOREIGN KEY (a, b) REFERENCES t (a, b)`), cardinality, orphans, fan-out, dimension
joins, index proposals, diagrams and the data dictionary. It is **not** used for the `NOT IN` demonstration, a list price or
the order table, which compare one column: `Relationship.child_col` raises on a composite link, so no code can join it on one
column by mistake.

For each relationship the engine computes:
- **containment**;
- **orphan rows** (child values with no parent);
- **child NULLs**;
- **children per parent** (max, average, parents without children);
- **cardinality** (`1:1` when max ≤ 1, else `1:N`).

**Fan-out risks** (`find_fanouts`): numeric non-price measures of the parent in a `1:N` link, excluding link columns. Each records
its total, and its total after the join (value × children per parent). They are sorted amount › charge › quantity › other, then
by factor.

**Northwind:**
- 7 accepted: 6 by name + `employees.reports_to` (self).
- 1 suggested: `orders.ship_via → shippers.shipper_id`. The value sets {1, 2, 3} are equal, and the names share "ship". The
  same values are also valid employee, category, product and supplier keys, which is why it is only suggested.
- Accepted + ticked = the 8 FOREIGN KEY constraints of Microsoft's script (`examples/northwind/expected_fks.json`, checked by
  `tests/test_schema.py`).

---

## Stage D: Decision (`decide`)

### D0. Gates
| Condition | Result |
|---|---|
| no table, or every table empty | **HALT** (`halt_empty`) |
| the largest table has < **100** rows | warning (`small_tables`), everything still runs |
| a manual pick that isn't a numeric column / a date of the order tables / a table | **HALT** (`halt_pick`) with the reason |

### D1. Focus (three modes, `FOCUS_MODES`)
| Mode in the app | What happens |
|---|---|
| 🤖 **Let the engine decide** (default) | stages D2–D7. **If the fact table has no money measure and no date, it falls back to validation** (`focus_auto_validate`) |
| 🎯 **I'll choose** | you pick the fact table; revenue as price × quantity (× (1 − discount)), an amount, or a row count; the date; the order key; the customer; up to 3 dimensions. Everything you don't pick is decided as in auto |
| 🔍 **Just validate the data** | schema, grain, cardinality, duplicates, NULLs, dates, orphans, EXPLAIN and NULL traps; all KPI cells are skipped |

### D2. Fact table and grain
- **Fact table:** the highest score of (sales measures, has a date itself or one join away, is a child in a link, rows). The
  sales-measure score is 3 for price + quantity, 2 for an amount, 1 for price or quantity, 0 otherwise. A single table is its
  own fact table.
- **Order key**, first match:
  1. a column named like `order|invoice|transaction|txn|receipt|basket|booking|sale|purchase` + `id|key|no|num|number|code`;
  2. the single-column key;
  3. the part of a composite key that points at a table with a date.
- **Line level** when the order key repeats in the fact table (the order table must be linked by that one column). Then:
  - the **order table** (`header`) is the parent the order key points at;
  - lines are rolled up to orders **before** any order-level table is joined;
  - without an order table, order-level columns are taken per order with `MIN()`, and the reconciliation counts orders whose
    lines disagree on the date.

### D3. Revenue
| Fact table has | Revenue |
|---|---|
| price **and** quantity (+ optional discount) | `SUM(price × quantity × (1 − COALESCE(discount, 0)))`: **net** of discount; gross is shown in the reconciliation |
| a discount in percent (written with `%`, or most non-zero values above 1) | `discount / 100.0` (`discount_percent` story) |
| quantity only, price in a parent table | that parent's price (a **list price**, with a warning and an assumption) |
| an amount column | `SUM(amount)` |
| none of these | **row counts** (orders per period); RFM and the driver tree are skipped |

The price on the line wins over a parent's list price: in Northwind 30.5% of lines differ from today's list price.

### D3b. Business rules: rows that are not sales (`find_row_filters`)
Looked for in the fact table and the order table, in every mode (validate mode only reports them, `row_filter_validate`):

| Column | Found by | Rows left out |
|---|---|---|
| status | a text column named `status`, `stage`, `…_status`, `order_state`, `payment_state`… (**never** plain `state`, which is a geography far more often) | values matching cancel, void, refund, return, test, fraud, reject, declin, fail, abandon, chargeback, delet or `lost`, unless they say partial or not (`Partially refunded` keeps most of its revenue) |
| flag | named `is_test`, `cancelled`, `is_void`, `deleted`, `fraud`, `refunded`, `returned`…, holding true/false, 0/1 or Y/N | the true values |
| currency | a text column named `currency`, `ccy`, `curr` (`…_code`), including the `<column>_currency` the loader splits off, with ≥ 2 currencies (case-insensitive) | every currency but the most common one; a row with no currency is assumed to be in it. Skipped when revenue is a row count |

- A rule that would leave out **every** row is not applied (the column means something else).
- The views read each such table through `<table>_kept` (after de-duplication): `col IS NULL OR col NOT IN (…)`, the exact
  complement of `col IN (…)`, so a NULL is never left out. A line of an order left out goes with it
  (`NOT EXISTS (… h.order_id = x.order_id AND <left out>)`).
- The reconciliation adds a `left out by the business rules` row and checks raw = left out + kept; pandas applies the same
  rules. The discount bands and gross vs net read the same rows as the views.
- A filtered column left with one value (one currency, flag = false) is not offered as a dimension.
- Manual mode can keep every row (`row_filters = False`, `row_filter_off` story).
### D4. Time
- **Date:** among the date columns of the fact and order tables, the highest of: +2 named like an order/sale/created date, −3
  named like a ship or required date; then fewest NULLs; then the fact table first.
- **Period:**
  - **year** when the data spans > **5** years;
  - otherwise **quarter** when there are ≥ **4** complete quarters;
  - otherwise **month** when there are ≥ **3** complete months;
  - otherwise **ISO week** (Monday to Sunday, labelled `2022-W07`, numbered within its ISO year) when there are ≥ **4**
    complete weeks; `week_index` counts Mondays since 1970-01-05, so a 53-week year never breaks `LAG`;
  - otherwise **month**.

  When the data spans more than one calendar year, periods are grouped and ordered by year *and* quarter (or month).
- **Partial periods:** only the first and the last period can be cut off. A period with data on < **90%** of its days is
  `partial`. It is shown and labelled, but excluded from every period-over-period verdict.
- **Comparisons:** the last two **complete** periods, when they are consecutive. Year over year: the last complete quarter (or
  month) vs the same one a year earlier, only when that one is complete too, and never for another period as a fallback.
  In SQL, `prev_<value>` and `<value>_year_ago` are kept only when that period's `is_partial = 0`.
- **Calendar length:** when the two compared periods' days with data differ by ≥ **3%** (`CALENDAR_GAP`; 28 vs 31 days),
  the change in revenue, orders and customers is also computed **per day**, and the verdict, the headline and its priority use
  it.
- **Real change or noise?** Each change between the two compared periods gets a 95% interval (`checklib.py`): orders and
  customers by a Poisson rate ratio, revenue as a compound Poisson total (Var = n(sd² + mean²)), average order value, ship
  days and rating by Welch's interval (Welch–Satterthwaite degrees of freedom), late % by Newcombe's Wilson-score interval,
  in points. `significant` = the interval excludes zero.
- **Date parts:** a `year` / `quarter` / `month` column stored next to a real date is checked against it.

### D5. Drivers and the customer
| Driver | Condition | Column in the views |
|---|---|---|
| lead time | a date in the same table named `ship|dispatch|deliver|fulfil|arriv|receiv|sent` (not required/due) that is on or after the order date in ≥ **90%** of rows | `ship_days` / `delivery_days` / `lead_days`. MySQL `DATEDIFF(end, start)`; PostgreSQL `end - start` (or `end::date - start::date` for timestamps) |
| late flag | a date named `required|due|promised|expected|sla|deadline` | `shipped_late` = 1 when shipped after it, or not shipped and already past it at the last date in the data; 0 when shipped on time; NULL when there is no promised date, or the order is not shipped and not yet due |
| rating | a `rating` column in the fact or order table | `rating_score`, mapped with `CASE` for text scales |

- **Customer:** a column named like `customer|client|user|account|buyer|member|shopper|guest|patient` (+ optional
  `id|key|no|code`) in the fact or order table, other than that table's own key.
- An empty driver value (e.g. 21 unshipped orders) is reported, because AVG() skips NULLs.

### D6. Dimensions (at most **3**)
- **Candidates:** `dimension` columns with 2–50 values and ≤ **30%** NULL, in tables reachable along accepted links within **2**
  joins of the fact or order table.
- **Level:** `line` when the column varies within an order (product side), `order` otherwise.
- **Groups**, at most one each, in this order: geography (country, state, region, city…), product (category, segment, type,
  brand, maker…), channel / status, fulfilment (shipper, carrier, warehouse), people (employee, salesperson, rep), other.
- **Ranking within a group:**
  1. the customer table first, for geography;
  2. fewer joins;
  3. fewer NULLs;
  4. closest to 10 values.

  People and "other" groups only use the table's **label column** (`<table>_name`, `name`, the table's singular, `company_name`,
  `full_name`, `last_name`, `title`).
- **Top-N item:** the label of the table inside the first line-level dimension (products in a category); otherwise customers
  within the first order-level dimension.
- **Aliases in the views:**
  - columns of the fact / order table keep their names;
  - columns already starting with the table's singular keep theirs (`category_name`);
  - others become `<singular>_<column>` (`customer_country`, `employee_last_name`).

**Northwind:** `customers.country` (geography), `categories.category_name` (product), `employees.last_name` (people). Ticking
the `ship_via` link replaces the person with `shippers.company_name` (fulfilment).

### D7. Correctness and speed
- **De-duplication:** every table with an `intended` key keeps one row per key with
  `ROW_NUMBER() OVER (PARTITION BY key ORDER BY ts IS NULL, ts DESC)`. `ts` is a column named updated/modified/loaded/created,
  else any date; without one the copy kept is arbitrary, and that is stated.
- **NOT IN trap** (single-column links only):
  - prefer an accepted link whose child column has NULLs (self-references first);
  - otherwise inject a NULL into the first link (`UNION ALL SELECT NULL`) to show what one NULL would do.
- **COUNT trap:** the lead-time end column if it has NULLs, else the rating, else the first nullable measure or date.
- **Indexes proposed:** every foreign key (both columns of a composite one) that is not the leading part of its table's key,
  plus the analysis date.
- **Skipped cells** and why:

  | Cell | Skipped when |
  |---|---|
  | cardinality, orphans | no relationships |
  | date range | no date anywhere |
  | per-period cells, running total, recency, RFM, cohorts, driver tree, drill-down | no date |
  | recency, RFM, cohorts | no customer |
  | driver tree, drill-down | fewer than 2 complete periods, or the last two aren't consecutive |
  | drill-down | no driver and no dimension |
  | top N | no group-and-item pair |
  | RFM, driver tree, Pareto | no money measure |
  | Pareto, purchase gaps | no customer (purchase gaps also need a date) |
  | discounts | no discount column |
  | trend signals | fewer than **5** periods (`MIN_PERIODS_FOR_SIGNALS`) |

### D8. Your own assumptions and KPIs (`decide(…, custom=Customization)`, `custom.py`)
Optional. Nothing here changes what the engine decided; it is kept apart and marked as yours wherever it appears.
- **Assumptions:** `assumption_items(d)` gives the engine's statements with stable keys (`fact`, `revenue`, `list_price`,
  `amount_unit`, `line_level`, `periods`, `partial:<period>`, `lead`, `rating`, `recency`, `dedupe:<table>`,
  `filter:<table>.<column>`, `dimensions`, and `validation_only` when no KPI is computed). `edit` rewords the statement with that key ("… (edited by you)"), `add` appends
  ("Added by you: …"), and a key that matches nothing becomes a "Not applied" notice listing the keys.
- **KPIs:** `key` (lower case, ≤ 40), `name`, `definition`, optional `sql`, `unit` (number, money, pct, days, score), `direction`
  (up, down, context), `why`, `pitfall`, `decision`. Without `sql` a KPI is a definition only.
- **`sql`** is one aggregate expression over `order_facts`, or an object with `default` and/or `mysql` / `postgres` / `duckdb`
  (it needs a `default`, or both `mysql` and `postgres`; DuckDB uses its own, then `default`, then PostgreSQL's, then MySQL's).
- **Checked first, every problem at once:** unknown keys, wrong types, duplicate keys, text over 600 characters (SQL: 1,200),
  control characters in wording (it ends up in SQL comments), and for the expression: no `;`, `--`, `/* */`, backtick or
  backslash; quotes and parentheses balance; no `SELECT`, `FROM`, `UNION`, `INTO`, `JOIN`, `WHERE`, `HAVING`, `LIMIT`, `OFFSET`,
  `FETCH`, `WINDOW`, `RETURNING`, `GROUP` or `ORDER` outside parentheses (`IS DISTINCT FROM` and `WITHIN GROUP` excepted); no
  call to `PG_SLEEP`, `SLEEP`, `BENCHMARK`, `LOAD_FILE`, `PG_READ_FILE`, `DBLINK`, `NEXTVAL`, `READ_CSV` and the like.
- **Warnings** (not errors; they appear in the app, the brief and as a `-- Check:` comment in the SQL): a `/` not followed by
  `NULLIF(` or a decimal literal; for the `default` and `mysql` expressions, every construct in `BANNED` and `::`.
- **Cell:** `custom_kpis` after `kpi_by_period`, built only when at least one KPI has `sql` and the views exist (not in validate
  mode). Two **soft** statements per KPI (all periods; per period). A statement that fails is reported next to the KPI; the
  runner goes on.
- **Dry run** (`runner.dry_run`): the views and the soft statements on empty copies of the tables. Finds missing columns and
  unknown functions; an aggregate over no rows returns one row, so an expression that returns none is not an aggregate.

---

## Stage C: The generated cells (`codegen.py`)

Every cell has a six-part story and one or more statements, each rendered for MySQL 8, PostgreSQL and DuckDB, and a Python check
that writes the 📌 findings from the result.

| # | Cell | Section | SQL pattern |
|---|---|---|---|
| 1 | `schema` | Schema & grain | `CREATE TABLE` parents first; `NOT NULL` when no NULLs; `PRIMARY KEY`; inline `FOREIGN KEY` only for trusted links with no orphans and a unique parent. Run on an empty database |
| 2 | `grain` | Schema & grain | `COUNT(*)` vs `COUNT(DISTINCT key)` (pairs: `SELECT COUNT(*) FROM (SELECT DISTINCT a, b …)`), NULL keys; `UNION ALL` |
| 3 | `cardinality` | Schema & grain | children per parent with `LEFT JOIN … GROUP BY`; fan-out demo: a parent column summed alone vs after joining (first 2 risks) |
| 4 | `duplicates` | Validate | `GROUP BY key HAVING COUNT(*) > 1`; listing of the most repeated keys (top **20**) |
| 5 | `nulls` | Validate | `COUNT(*) - COUNT(col)` per column; with > **60** columns, only those that had NULLs. "Missing and impossible values": `SUM(CASE WHEN … THEN 1 ELSE 0 END)` for negative prices and charges and discounts outside 0–1 (0–100 in percent), which count against Validity, and zero or negative quantities and negative amounts, reported as likely returns |
| 6 | `date_range` | Validate | MIN/MAX per date; rows, first and last date per period (coverage computed in the check); lead-time sanity (negative, > 365 days); date-part consistency |
| 7 | `orphans` | Validate | `LEFT JOIN … WHERE parent.key IS NULL` and `NOT EXISTS` (both directions, every key column); examples of missing values and of childless parents |
| 8 | `base` | Build with CTEs | `<table>_deduped` and `<table>_kept` CTEs first, then `order_facts` (one row per order: `line_revenue → order_totals → LEFT JOIN`s, `COALESCE(…, 'Unknown')`) and `line_facts` (one row per line) |
| 9 | `reconcile` | Validate | rows, orders and revenue from the raw table, the rows the business rules leave out, and each view (`UNION ALL`) + pandas from the uploaded rows; gross vs net; price paid vs list price |
| 10 | `kpis` | KPIs | revenue, orders, AOV = revenue ÷ orders, average line value (contrast), customers, orders per customer, repeat-customer %, units per order, average lead time, late % (with open overdue orders), rating; then the **median and P90** of order value and lead time by rank: `ROW_NUMBER()` + `COUNT(*) OVER ()`, median = the mean of ranks `FLOOR((cnt + 1) / 2.0)` and `FLOOR((cnt + 2) / 2.0)`, P90 = the first rank ≥ `CEIL(0.9 * cnt)` |
| 11 | `discounts` | KPIs | lines per discount band (`CASE`), gross vs net revenue, discount given, units per line; each band's share of lines with `SUM(COUNT(*)) OVER ()` |
| 12 | `kpi_by_period` | KPIs | `quarterly` / `monthly` / `weekly` CTE with `period_index = year × 4 + quarter` (× 12 + month; `week_index` for weeks) and `is_partial` → `with_prev`: `CASE WHEN period_index − LAG(period_index) = 1 AND LAG(is_partial) = 0 THEN LAG(revenue) …` (the previous value only when it is a complete calendar neighbour), year over year by `LEFT JOIN … ON y.period_index = p.period_index − 4` (complete year-ago periods only) → `% change` with `100.0 * (new − old) / NULLIF(old, 0)`; per period also `STDDEV_SAMP` of order value, lead time and rating and the late counts, for the 95% intervals |
| 12b | `custom_kpis` | KPIs | only when you define a KPI with SQL (see D8): per KPI, `SELECT <your expression> AS key FROM order_facts`, then the same with `GROUP BY` the period columns; both **soft** (an error is reported, not fatal) |
| 13 | `running_total` | Window functions | `SUM() OVER (ORDER BY … ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)`, share of `SUM() OVER ()` |
| 14 | `trend_signals` | Window functions | 3-period moving average (`RANGE BETWEEN 2 PRECEDING AND CURRENT ROW` on `period_index`); baseline `AVG` / `STDDEV_SAMP` / `COUNT` over `RANGE BETWEEN 8 PRECEDING AND 1 PRECEDING` of `baseline_value` (NULL for partial periods); z only with ≥ **4** baseline periods; in the check, a straight line through the same baseline (≥ **5** periods) and its prediction interval |
| 15 | `top_n` | Window functions | `DENSE_RANK()` and `ROW_NUMBER()` `OVER (PARTITION BY group ORDER BY revenue DESC)`, filtered in an outer query (top **3**) |
| 16 | `pareto` | Window functions | `ROW_NUMBER()` by revenue, cumulative share with `SUM() OVER (ORDER BY … ROWS UNBOUNDED PRECEDING)` ÷ `SUM() OVER ()`; ABC classes at **80%** / **95%** of revenue |
| 17 | `latest_record` | Window functions | `ROW_NUMBER() OVER (PARTITION BY customer ORDER BY date DESC, order DESC) = 1`; recency measured to the last date in the data |
| 18 | `purchase_gaps` | Window functions | `LAG(date) OVER (PARTITION BY customer ORDER BY date, order)` → days between orders; overdue = silent for more than **2 ×** the customer's own average gap (3+ orders) |
| 19 | `rfm` | Window functions | view `customer_rfm`: `CEIL(ROUND(5 * CUME_DIST() OVER (ORDER BY …), 9))` for recency / frequency / monetary, so equal values get equal scores; `CASE` segments; the order counts behind each F score; summary per segment |
| 20 | `driver_tree` | Root cause | drivers for the two compared periods; Δ per member of each dimension with conditional aggregation and `SUM() OVER ()` |
| 21 | `drilldown` | Root cause | symptom and drivers per complete period (correlations if ≥ **6** periods, on levels and on period-to-period changes, each with a Fisher-z 95% interval); per segment, before vs after, `AVG(CASE WHEN … THEN x END)` |
| 22 | `cohort` | Root cause | first-order period × periods since (year × 4 + quarter arithmetic, no date spine) |
| 23 | `explain` | Fast & correct | `EXPLAIN` (`EXPLAIN FORMAT=TREE` in MySQL); `YEAR(col) = y` vs a sargable date range; `CREATE INDEX` proposals (shown, not run) |
| 24 | `null_traps` | Fast & correct | NOT IN vs NOT EXISTS; `COUNT(*)` vs `COUNT(col)` vs `AVG`; integer division (shown, not run) |

**Driver-tree arithmetic** (in the check). For the compared periods, with O = orders and A = average order value, the
two-effect **Shapley** split measures each effect at the average of the other:

- volume = (O₂ − O₁) · (A₁ + A₂) / 2
- order value = (A₂ − A₁) · (O₁ + O₂) / 2

The two add up to the revenue change exactly, with no interaction term. The classic split (volume = ΔO · A₁, order value =
O₁ · ΔA, interaction = ΔO · ΔA) is kept in `split["classic"]` for the teaching note. "How much of the movement" is each
effect's share **by size** (|effect| ÷ Σ|effects|), so the shares never exceed 100% when effects pull in opposite directions.
The headline names the larger effect only when its change is significant.

**Unusual periods** (in the check). A complete period is flagged when **both**:

1. |z| ≥ t(0.995, n − 1) × √(1 + 1/n) for a baseline of n earlier complete periods: the edge of the 99% prediction interval
   for a new value (`TREND_Z_LIMIT`: 6.53 for 4, 5.04 for 5, 4.36 for 6, 3.96 for 7, 3.71 for 8); and
2. it is outside the 99% prediction interval of a least-squares line through the same baseline,
   t(0.995, n − 2) · s · √(1 + 1/n + (x₀ − x̄)² / Sxx), needing ≥ 5 baseline periods.

A fixed |z| ≥ 2 on 4 periods would flag pure noise in about 1 period in 6, and a level test alone would flag a series for
growing steadily.

## Dialect rules (`dialect.py`; enforced by `tests/test_codegen_runs.py`)
| Construct | MySQL 8 | PostgreSQL | DuckDB (runs in the app) |
|---|---|---|---|
| quoted identifier | `` `order` `` | `"order"` | `"order"` |
| string literal | `'O''Brien'`, backslash doubled | `'O''Brien'` | `'O''Brien'` |
| days between dates | `DATEDIFF(b, a)` | `b - a` (`b::date - a::date` for timestamps) | `b - a` / `date_diff('day', a, b)` |
| year / quarter | `YEAR(d)`, `QUARTER(d)` | `EXTRACT(QUARTER FROM d)::int` | as PostgreSQL |
| rounding | `ROUND(x, k)` | `ROUND((x)::numeric, k)` | `ROUND(x, k)` |
| ratio / percentage | `ROUND(1.0 * a / NULLIF(b, 0), k)`, `ROUND(100.0 * a / NULLIF(b, 0), 1)` | same, `::numeric` before rounding | same |
| plan | `EXPLAIN FORMAT=TREE` | `EXPLAIN` | `EXPLAIN` |
| keys | never `TEXT` (MySQL can't index it without a prefix) | | |

- **Never generated:** `FILTER (WHERE …)`, `NULLS FIRST/LAST`, `QUALIFY`, `FULL [OUTER] JOIN`, `ILIKE`, `generate_series`,
  `PERCENTILE_CONT`, `COUNT(DISTINCT a, b)`, a reserved word as an alias, `::` in MySQL, a division without `NULLIF` or a
  decimal literal, a `WHERE` filter on a LEFT-joined table other than `IS NULL`.
- **Your own SQL** (D8) is not generated, so none of this is enforced on it; the constructs above, an unguarded `/` and `::` are
  named in a warning instead.
- **Verified on real servers:** `tests/test_live_postgres.py` (PostgreSQL 16) and `tests/test_live_mysql.py` (MySQL 8.4) run the
  exported project for 22 cases and compare every query result with DuckDB (645 per database). GitHub Actions runs both on
  every push, with `AUTOSQL_REQUIRE_LIVE=1` so that a missing server fails the run.

## Stage I: plain-English summary (`interpret`)
| Wording | Threshold |
|---|---|
| reconciled | all totals within **0.01** |
| a driver is a candidate cause | it moved ≥ **10%** between the two compared periods **in the direction that would explain the change** (more days to ship / late orders while revenue fell, fewer while it grew; a rating that fell while revenue fell). Otherwise: "moved the other way" or "barely moved" |
| correlation described as a relationship | the 95% Fisher-z interval of the period-to-period changes excludes 0 ("the data shows a relationship"); levels alone: "two series that both trend correlate even when unrelated"; neither: "this many periods can't show a relationship". Only with ≥ **6** complete periods |
| customers silent for over 180 days | a count in the latest-record step only; the churn signal used in recommendations is *overdue*: silent for more than **2 ×** the customer's own average gap (3+ orders) |
| the main driver of a revenue change | the larger of the volume and order-value effects; its share by size is reported |

## Stage B: the business layer (`business.py`, `report.py`)

| Output | Rule |
|---|---|
| trust score | weighted average of Completeness **25%**, Uniqueness **20%**, Integrity **20%**, Validity **15%**, Consistency **20%** (dimensions that don't apply are left out and the weights rescaled); **≥ 90** High, **≥ 75** Good with caveats, else Use with care; shown rounded down. **Caps** (`TrustScore.caps`): totals that don't reconcile → Use with care; any dimension below **60** (`TRUST_FLOOR`) → at most Good, with caveats |
| tiles | the last two complete periods; a change in a percentage is in points; "good" follows each KPI's direction (fewer days to ship is good); a change whose 95% interval includes zero is shown as "within noise", not green or red; flows (revenue, orders, customers) per day when the periods differ in length |
| headline | level of the last complete period, change on the previous one (and per day) and year over year (same period only), "within normal variation" when the interval includes zero, and the larger driver effect when that effect is significant |
| recommendations | one rule per area (Data, Growth, Drivers, Customers, Operations, Pricing) with priority 1–3 and confidence measured / indicative / needs a test; every rule and threshold is listed in [business_guide.md](business_guide.md#recommendation-rules) |
| brief charts | one y-axis per chart (the driver gets its own chart), partial periods grey, increases blue and decreases red, totals neutral, a table view per chart |
| SQL in the brief | every chart, tile row, fact and trust meter links the statements it was computed from (`briefsql.SqlLibrary`); a block is the text of a generated `Statement`, per dialect; the steps used form the appendix |
| recommendation popups | `Insight.shows` names the `data-ev` cards and charts a recommendation was made from, `Insight.figures` the figures that appear nowhere else; the page copies its own nodes into one `<dialog>`; only evidence the page has is named |
| your KPIs | tiles and cards in their own section, marked yours; the change is shown without a verdict (`Tile.custom`, no significance test); a failed KPI is a card with its error and its SQL |
