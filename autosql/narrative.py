"""Beginner-friendly explanations.

Every decision the engine makes is recorded as a ``Step``:

* found  - the evidence we saw in the data (with real numbers and names)
* why    - why that evidence matters, in plain English
* action - what we do about it, phrased as "We ..., so that ..."
* code   - (SQL cells only) what the SQL does, in plain words

The engine's decisions use ``TEMPLATES`` below; each SQL cell's story lives in ``codegen.py`` next to the SQL it
explains. After a cell runs, its check code writes "what we found" itself with ``note()``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Step:
    key: str
    found: str          # 🔍 what we found before this step (the evidence)
    why: str            # 💡 why that evidence matters
    action: str         # 🎯 what we're going to do, "so that ..."
    code: str = ""      # 🛠 what the SQL does, in plain words (cells only)

    def markdown(self) -> str:
        return (
            f"🔍 **What we found before:** {self.found}  \n"
            f"💡 **Why it matters:** {self.why}  \n"
            f"🎯 **What we're going to do:** {self.action}"
        )

    def short(self) -> str:
        """One-line version for the sidebar decision trace."""
        return f"{self.found} → {self.action}"


TEMPLATES: dict[str, dict[str, str]] = {
    # ── Loading ───────────────────────────────────────────────────────────
    "overview": {
        "found": "You uploaded {n_tables} table(s) with {rows:,} rows in total: {names}.",
        "why": "SQL questions are answered by joining tables, so we first need to know what each table holds and how they connect.",
        "action": "We profile every table and column first, so that every later query is built on checked facts instead of guesses.",
    },
    "renamed": {
        "found": "{n} column name(s) were not plain SQL names (e.g. {examples}).",
        "why": "Names with capitals, spaces or symbols must be quoted in every query, and PostgreSQL folds unquoted names to lower case, so `OrderID` and `orderid` would silently differ.",
        "action": "We renamed them to snake_case (`OrderID` → `order_id`), so that the SQL is readable and runs the same in MySQL and PostgreSQL. The column profile lists every original name.",
    },
    "skipped_file": {
        "found": "`{source}` {reason}.",
        "why": "A file we can't read can't be queried.",
        "action": "We left it out, so that the rest still works. Fix the file and upload it again to include it.",
    },
    "unparsed_dates": {
        "found": "In `{table}.{col}`, {n:,} value(s) look like dates but don't match the format the rest use ({fmt}).",
        "why": "A date SQL can't read becomes NULL, so those rows silently drop out of every date filter and every period.",
        "action": "We load them as NULL and count them in the NULL profile, so that you can see exactly how many rows lost their date.",
    },
    "ambiguous_dates": {
        "found": "In `{table}.{col}`, every value works as both month/day and day/month (no day is above 12).",
        "why": "03/04 could be 4 March or 3 April. Reading them the wrong way shifts every date and every period.",
        "action": "We read them as month/day (the US convention). If they are day/month, fix the file before trusting the periods.",
    },
    "number_format_cleaned": {
        "found": "`{table}.{col}` holds numbers written the way a spreadsheet shows them: {example}{unit}.",
        "why": "Read as text, the column could not be summed or averaged at all, so the measure would silently disappear "
               "from the analysis.",
        "action": "We strip currency signs, thousands separators and % signs, read (12.50) as −12.50, and store plain numbers, "
                  "so that SQL can add them up.",
    },
    "placeholders_empty": {
        "found": "In `{table}.{col}`, {n:,} value(s) are markers such as '-', '#N/A' or 'unknown' rather than values.",
        "why": "One such marker would otherwise turn a whole number or date column into text.",
        "action": "We read them as empty (NULL), so that they are counted in the NULL profile and the column keeps its type.",
    },
    "unparsed_numbers": {
        "found": "In `{table}.{col}`, {n:,} value(s) are not numbers, while at least 95% of the column is.",
        "why": "A value SQL can't read becomes NULL, so those rows drop out of every total and average.",
        "action": "We load them as NULL and count them against the data-trust score, so that you can fix them at the source.",
    },
    # ── Schema and grain ──────────────────────────────────────────────────
    "grain_single": {
        "found": "In `{table}` ({rows:,} rows), {reason}.",
        "why": "That column is the table's key: it names exactly one row, so the grain is {grain}. Knowing the grain is what stops a join from double-counting.",
        "action": "We treat `{key}` as the primary key, so that joins and counts are checked against it.",
    },
    "grain_composite": {
        "found": "In `{table}` ({rows:,} rows), {reason}.",
        "why": "So one row is one {grain_noun}: the same `{first}` appears on several rows. Summing a per-`{first}` value over these rows would repeat it.",
        "action": "We use (`{first}`, `{second}`) as the primary key, so that the grain is {grain} and order-level numbers are rolled up before they are joined.",
    },
    "grain_intended": {
        "found": "In `{table}`, `{key}` looks like the key, but {dups:,} value(s) appear more than once ({extra:,} extra rows){nulls}.",
        "why": "A duplicated key multiplies rows in every join that uses it, so totals come out too high.",
        "action": "We list the duplicates in the validation step and keep one row per `{key}` (the {keep}) when we build the analysis, so that joins stay one-to-one.",
    },
    "grain_none": {
        "found": "In `{table}`, no column or pair of columns is unique.",
        "why": "Without a key we can't say what one row represents, and we can't spot duplicates by key.",
        "action": "We count rows that are identical in every column instead, so that true copies are still caught.",
    },
    "fk_name": {
        "found": "{rel}: {reason}.",
        "why": "This is a foreign key: each `{child}` row points at one `{parent}` row ({max_children:,} at most per parent, {avg:.1f} on average).",
        "action": "We join on it as many-to-one, so that `{parent}` details can be added to `{child}` rows without multiplying them.",
    },
    "fk_self": {
        "found": "{rel}: {reason}.",
        "why": "A table that points at itself is a hierarchy. Its NULLs (the top of the tree) are exactly what breaks `NOT IN`.",
        "action": "We record the link and use it in the NULL-trap check, so that the classic `NOT IN` mistake is shown on your own data.",
    },
    "fk_suggested": {
        "found": "{rel}: {reason}",
        "why": "Values that happen to match prove little: small codes such as 1, 2, 3 exist in many tables.",
        "action": "We only suggest this link. Tick it under the diagram if it is real, so that it is checked and used like the others.",
    },
    "fk_low": {
        "found": "{rel}: {reason}.",
        "why": "Joining on it would drop or mismatch {orphans:,} row(s).",
        "action": "We don't use it until you confirm it, so that no rows disappear silently. The orphan check shows the missing values.",
    },
    "type_mismatch": {
        "found": "{rel} links a {child_type} column to a {parent_type} key.",
        "why": "Joining text to numbers forces a conversion on every row: slow in MySQL, an error in PostgreSQL, and '7' vs '07' never match.",
        "action": "We compare them as text in our checks and flag it, so that you can fix the column type at the source.",
    },
    "fanout": {
        "found": "`{parent}.{col}` is stored once per `{parent}` row, but each `{parent}` row has up to {max_children:,} `{child}` rows.",
        "why": "After joining, `{col}` repeats on every `{child}` row: its total would read {joined:,.2f} instead of {total:,.2f} ({factor:.2f}× too high). This is the classic join fan-out.",
        "action": "We roll `{child}` up to one row per `{parent}` **before** joining, so that `{col}` is counted once. The cardinality step shows both totals.",
    },
    # ── Focus ─────────────────────────────────────────────────────────────
    "focus_auto": {
        "found": "You let the engine decide what to measure.",
        "why": "Sales-shaped data (a table of lines or orders with prices or amounts and a date) supports a standard set of KPIs.",
        "action": "We look for the fact table, the revenue formula and the date, so that the analysis answers 'how much, how many, and how is it changing?'.",
    },
    "focus_manual": {
        "found": "You picked what to measure yourself{hint}.",
        "why": "You know the business question best, so your choice always wins over the engine's guess.",
        "action": "We build the analysis around your picks, so that every query answers your question.",
    },
    "focus_validate": {
        "found": "You chose to validate the data only.",
        "why": "Checking keys, NULLs, duplicates and orphans is worth doing before any KPI, and on its own when you are just receiving a new extract.",
        "action": "We run the schema, validation and performance steps and skip the KPIs, so that you get a data-quality report.",
    },
    "focus_auto_validate": {
        "found": "No table has both something to add up (a price, a quantity or an amount) and a date.",
        "why": "KPIs such as revenue per quarter need both. Inventing a measure would answer a question nobody asked.",
        "action": "We validate the data instead. Pick 'I'll choose' to count rows per period or to name a measure yourself.",
    },
    "fact_table": {
        "found": "`{table}` has {rows:,} rows{what}.",
        "why": "The table that records each sale (the *fact* table) is where revenue is computed; the other tables describe it.",
        "action": "We use `{table}` as the fact table, so that revenue is computed once, at its true grain.",
    },
    "revenue_price_qty": {
        "found": "`{table}` holds a price (`{price}`) and a quantity (`{qty}`){discount_note}.",
        "why": "Revenue per line is price × quantity{discount_why}. Stored totals are often missing or stale, so we compute it.",
        "action": "We define **revenue = SUM({formula})**, so that every KPI uses one documented formula.",
    },
    "revenue_parent_price": {
        "found": "`{table}` has a quantity but its price lives in `{parent}.{price}`.",
        "why": "That is usually today's *list* price, not the price paid at the time of each sale, so old revenue may be misstated.",
        "action": "We use it anyway and list it as an assumption, so that you know the revenue is approximate.",
    },
    "revenue_amount": {
        "found": "`{table}.{amount}` is a money amount.",
        "why": "When a table stores the amount of each row, adding it up is the revenue.",
        "action": "We define **revenue = SUM({amount})**, so that every KPI uses one documented formula.",
    },
    "revenue_count": {
        "found": "`{table}` has no price, quantity or amount column.",
        "why": "Without money we can still measure volume: how many rows (orders, bookings, tickets) per period and per group.",
        "action": "We measure **row counts** instead of revenue, so that trends and drivers are still visible.",
    },
    "discount_percent": {
        "found": "`{col}` runs up to {max:g}, so it is a percentage, not a fraction.",
        "why": "Multiplying by (1 - 15) instead of (1 - 0.15) would make revenue negative.",
        "action": "We divide it by 100 in the formula, so that revenue is right.",
    },
    "line_grain": {
        "found": "`{table}` has {rows:,} rows but only {orders:,} distinct `{order}` values: one row per order **line**.",
        "why": "Order-level KPIs (orders, average order value, shipping time) must be counted per order, not per line, or big orders count several times.",
        "action": "We roll lines up to one row per order first (`order_facts`), and keep a separate line-level view (`line_facts`) for product questions, so that each KPI uses its true grain.",
    },
    "header_table": {
        "found": "The order date{extra} is in `{header}`, one join away from `{fact}` (via `{key}`).",
        "why": "Dates, customers and shipping belong to the order, not to each line.",
        "action": "We join `{header}` after rolling the lines up, so that order-level columns are never repeated.",
    },
    "date_chosen": {
        "found": "`{table}.{col}` runs from {start} to {end}{nulls}.",
        "why": "{why}",
        "action": "We use it for every trend and group the data by **{period}**{multi}, so that periods are comparable.",
    },
    "no_date": {
        "found": "No date column was found in `{table}` or the tables it points at.",
        "why": "Trends, running totals, recency and cohorts all need a date.",
        "action": "We skip the time-based steps, so that only queries that make sense are run.",
    },
    "partial_period": {
        "found": "{label} only has data from {first} to {last}: {days} of {total} days ({pct:.0f}%).",
        "why": "A part-period always looks like a collapse. Comparing it with a full period would report a fall that is just missing days.",
        "action": "We show it, flag it as **partial**, and leave it out of every period-over-period verdict, so that changes compare like with like.",
    },
    "missing_periods": {
        "found": "{n} {unit}(s) have no rows at all: {labels}.",
        "why": "LAG() takes the previous *row*, not the previous {unit}: across a gap it would compare non-adjacent periods and call it '{unit}-over-{unit}'.",
        "action": "We number the periods (year × 4 + quarter) and only compare periods whose numbers differ by exactly 1, so that a gap shows as 'no comparison' instead of a wrong one.",
    },
    "lead_time": {
        "found": "`{end}` is a later date than `{start}` in {pct:.0f}% of rows.",
        "why": "The gap between them ({name}) is a classic operational driver: slower shipping often shows up later as lower ratings and repeat sales.",
        "action": "We compute **{name}** per order and put it next to revenue in every trend, so that a change in the symptom can be checked against this driver.",
    },
    "lead_nulls": {
        "found": "`{col}` is empty for {n:,} order(s) ({pct:.1f}%).",
        "why": "AVG() skips NULLs, so the average {name} describes only the orders that have a `{col}` value.",
        "action": "We say so next to every average and count those orders separately, so that the average isn't misread.",
    },
    "late_flag": {
        "found": "`{required}` holds the promised date.",
        "why": "Shipping after the promised date is what customers notice, more than the raw number of days.",
        "action": "We add **shipped_late** (1 = after `{required}`), so that the late share can be tracked per period and per group.",
    },
    "rating_numeric": {
        "found": "`{col}` is a score from {low} to {high}.",
        "why": "Ratings measure the customer's experience, so they belong next to revenue and shipping time.",
        "action": "We average it per period and group, so that the experience can be checked against the money.",
    },
    "rating_scale": {
        "found": "`{col}` holds text on an ordered scale ({labels}).",
        "why": "Text can't be averaged, but an ordered scale can be mapped to numbers.",
        "action": "We map it to **rating_score** 1-5 with a CASE expression, so that it can be averaged per period and group.",
    },
    "customer_key": {
        "found": "`{table}.{col}` identifies the customer.",
        "why": "Repeat-customer rate, recency, RFM segments and cohorts are all per customer.",
        "action": "We use it for the customer-level steps, so that each customer is counted once.",
    },
    "no_customer": {
        "found": "No column identifies a customer.",
        "why": "Repeat rate, RFM and cohorts need to know who placed each order.",
        "action": "We skip the customer-level steps, so that no per-customer numbers are invented.",
    },
    "dimensions": {
        "found": "The best columns to slice by are {names}.",
        "why": "When a KPI moves, splitting it by where (geography), what (product) and how (channel, shipper, rep) isolates the part that moved.",
        "action": "We use them in the drill-down, so that a change can be traced to one segment the business can act on.",
    },
    "dedupe": {
        "found": "`{table}` has repeated `{key}` values.",
        "why": "Joining a table with duplicate keys multiplies the rows on the other side.",
        "action": "We keep one row per `{key}` with ROW_NUMBER() ({keep}), so that joins stay many-to-one.",
    },
    "aov_grain": {
        "found": "The fact table has one row per order line.",
        "why": "The average of order *lines* is not the average order value: an order with 5 lines counts 5 times, and each line is only part of an order.",
        "action": "We compute **average order value = revenue ÷ COUNT(DISTINCT order)**, and show the average line value next to it so that the difference is visible.",
    },
    "indexes": {
        "found": "Queries join on {n_keys} foreign key column(s) and filter on `{date}`.",
        "why": "Without an index, every join and date filter reads the whole table. InnoDB (MySQL) indexes foreign keys automatically; PostgreSQL does not.",
        "action": "We propose CREATE INDEX statements for them, so that the same queries stay fast on a real database.",
    },
    "skipped_cell": {
        "found": "{reason}",
        "why": "A query without the data it needs would return nothing useful, or something misleading.",
        "action": "We skip **{title}**, so that every result shown means something.",
    },
    "small_tables": {
        "found": "The largest table has only {rows:,} rows.",
        "why": "SQL works at any size, but KPIs on a few dozen rows move a lot when one row changes.",
        "action": "We run every step anyway, and the results say how many rows each number rests on.",
    },
    # ── Halts ─────────────────────────────────────────────────────────────
    "halt_empty": {
        "found": "None of the uploaded tables has any rows.",
        "why": "There is nothing to query.",
        "action": "We stop here. Upload files with a header row and at least one row of data.",
    },
    "halt_pick": {
        "found": "{what}",
        "why": "{why}",
        "action": "We stop here. Please pick a different column.",
    },
}


def story(key: str, /, **values) -> Step:
    t = TEMPLATES[key]
    return Step(key, t["found"].format(**values), t["why"].format(**values), t["action"].format(**values))


GLOSSARY: dict[str, str] = {
    "Grain": "What one row of a table represents (an order, an order line, a customer). Get it wrong and joins double-count.",
    "Primary key": "The column(s) whose value is unique and never empty, so it names exactly one row.",
    "Composite key": "A primary key made of two columns, e.g. (order_id, product_id) for order lines.",
    "Foreign key": "A column that points at another table's primary key, e.g. orders.customer_id → customers.customer_id.",
    "Cardinality": "How many rows join to one: 1:1 (one each) or 1:N (one parent, many children).",
    "Fan-out": "When a join to a 1:N table repeats each parent row, so parent-level numbers (freight) are summed several times.",
    "Orphan": "A child row whose foreign key matches no parent row (an order line for an order that doesn't exist).",
    "Reconciliation": "Checking that a total computed one way equals the same total computed another way (or in the source).",
    "Fact table": "The table that records events (sales lines, orders); other tables describe them (dimension tables).",
    "Dimension": "A column you group or filter by: country, category, channel.",
    "CTE": "Common Table Expression: a named sub-query written with WITH name AS (…), so a query reads as small steps.",
    "View": "A saved query that behaves like a table. Here, order_facts and line_facts.",
    "Conditional aggregation": "SUM(CASE WHEN … THEN 1 ELSE 0 END): counting or adding only the rows that meet a condition.",
    "Window function": "A calculation over related rows that keeps every row, e.g. a running total or a rank: f() OVER (PARTITION BY … ORDER BY …).",
    "PARTITION BY": "Splits rows into groups for a window function, like GROUP BY but without collapsing the rows.",
    "Frame": "Which rows a window function sees, e.g. ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW for a running total.",
    "LAG / LEAD": "The value from the previous / next row in the window order: used for period-over-period change.",
    "RANK / DENSE_RANK / ROW_NUMBER": "RANK leaves gaps after ties (1,1,3), DENSE_RANK doesn't (1,1,2), ROW_NUMBER never ties (1,2,3).",
    "NTILE": "Splits ordered rows into n equal-sized buckets (NTILE(5) = quintiles). Used for RFM scores.",
    "RFM": "Recency, Frequency, Monetary: three 1-5 scores per customer used to segment them.",
    "Cohort": "Customers grouped by when they first bought, followed over time to measure retention.",
    "AOV": "Average order value = revenue ÷ number of distinct orders. Not the average of order lines.",
    "Repeat-customer rate": "The share of customers with two or more orders.",
    "QoQ / YoY": "Quarter-over-quarter / year-over-year change. YoY compares the same season, so seasonality cancels out.",
    "Partial period": "A period the data only partly covers (e.g. an extract that ends on 6 May). It always looks like a drop.",
    "Driver tree": "Breaking a KPI into its parts (revenue = orders × AOV) to see which part moved.",
    "NULL": "'Unknown'. Any comparison with NULL is unknown, so NOT IN (… NULL …) matches nothing, and COUNT(col) / AVG skip NULLs.",
    "Anti-join": "Rows with no match in another table: LEFT JOIN … WHERE right.key IS NULL, or NOT EXISTS.",
    "Sargable": "A filter an index can use: order_date >= '1997-01-01', not YEAR(order_date) = 1997.",
    "Index": "A sorted lookup structure on a column, so joins and filters don't read the whole table.",
    "EXPLAIN": "Shows the plan a database will use to run a query: which scans, joins and sorts.",
    "Dialect": "A database's flavour of SQL. MySQL writes DATEDIFF(b, a); PostgreSQL writes b - a.",
}


def interpret(metrics: dict) -> list[str]:
    """Turn the numbers produced by the generated checks into a short, ordered summary."""
    m, out = metrics, []
    main = m.get("main_measure", "revenue")
    show = (lambda v: f"{v:,.2f}") if main == "revenue" else (lambda v: f"{v:,.0f} rows")
    if "reconciled" in m:
        if m["reconciled"]:
            out.append(f"✅ **Trustworthy base:** {'revenue of' if main == 'revenue' else 'a row count of'} **{show(m['revenue_total'])}** "
                       "reconciles between the raw table, the analysis views and the uploaded file, so every KPI below is computed on "
                       "all the data, once.")
        else:
            out.append(f"⚠️ **Totals don't reconcile** (SQL {show(m['revenue_total'])} vs file {show(m['revenue_source'])}). Fix orphans or "
                       "duplicated keys before sharing any KPI.")
    elif "grain_ok" in m:
        out.append("✅ Every table's key is unique." if m["grain_ok"] else "⚠️ Some keys repeat: see the grain and duplicates steps.")
    fan = m.get("fanout")
    if fan:
        out.append(f"**Join trap:** summing `{fan['column']}` after joining to the line table gives {fan['joined']:,.2f} instead of "
                   f"{fan['total']:,.2f} ({fan['factor']:.2f}× too high). The views aggregate before joining.")
    for period in m.get("partial_periods", []):
        naive = m.get("naive_change")
        extra = f" A naive comparison would report {naive['pct']:+.1f}%." if naive and naive.get("period") == period else ""
        out.append(f"**{period} is a partial period** and is left out of every trend verdict.{extra}")
    q = m.get("qoq_last_complete")
    if q:
        out.append(f"**Trend:** from {q['from']} to {q['to']} (the last two complete periods), {main} changed **{q['pct']:+.1f}%**"
                   + (f"; year over year {m['yoy']['period']} is **{m['yoy']['pct']:+.1f}%**." if m.get("yoy") else "."))
    split = m.get("driver_split")
    if split and split.get("delta"):
        sizes = [abs(split["volume"]), abs(split["order_value"]), abs(split["interaction"])]
        main = "the number of orders" if sizes[0] >= sizes[1] else "the average order value"
        share = max(sizes[:2]) / (sum(sizes) or 1.0)
        out.append(f"**What moved it:** {main} accounts for about {share:.0%} of the {split['delta']:+,.2f} movement "
                   f"(orders {split['volume']:+,.2f}, order value {split['order_value']:+,.2f}, both {split['interaction']:+,.2f}).")
    for col, c in m.get("top_contributors", {}).items():
        out.append(f"By `{col}`, **{c['segment']}** contributed most ({c['change']:+,.2f}, {c['share']:.0f}% of the change).")
    dv = m.get("driver_vs_symptom")
    if dv:
        if abs(dv["driver_change_pct"]) >= 10:
            out.append(f"**Suspected driver:** `{dv['driver']}` moved {dv['driver_change_pct']:+.1f}% while {main} moved "
                       f"{dv['symptom_change_pct']:+.1f}%. That makes it a candidate cause; confirm it with a controlled test before acting.")
        else:
            out.append(f"`{dv['driver']}` barely moved ({dv['driver_change_pct']:+.1f}%), so it does not explain the change.")
    k = m.get("kpis") or {}
    if k.get("avg_line_value") and k.get("avg_order_value"):
        out.append(f"Average order value is **{k['avg_order_value']:,.2f}**; the average *line* ({k['avg_line_value']:,.2f}) would "
                   "understate it, which is why AOV divides by distinct orders.")
    rfm = m.get("rfm_top_share")
    if rfm:
        out.append(f"**Concentration:** the {rfm[0]} segment ({rfm[2]:,} customers) brings {rfm[1]:.1f}% of revenue.")
    if m.get("orphans"):
        out.append("⚠️ Orphan rows exist (" + ", ".join(f"{k}: {v:,}" for k, v in m["orphans"].items())
                   + "); they are kept with 'Unknown' labels so that no revenue is lost.")
    trap = m.get("null_trap")
    if trap and "using_not_in" in trap:
        out.append(f"**NULL trap:** NOT IN returns {trap['using_not_in']:,} rows where NOT EXISTS returns {trap['using_not_exists']:,}. "
                   "Use NOT EXISTS whenever the sub-query column can be NULL.")
    return out
