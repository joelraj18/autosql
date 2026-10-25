# Business guide

AutoSQL Explorer writes SQL for analysts, but its results are read by people who decide things: a sales director, a finance
lead, an operations manager. This guide explains the business layer: what the brief shows, how to read every KPI, how the
data-trust score is built and the exact rules that turn query results into recommendations.

Everything here is computed from the results of the generated SQL (`autosql/business.py`, `autosql/report.py`). Nothing is
typed in by hand, and every recommendation names the SQL step that produced its evidence, so any number can be traced back
to a query you can rerun on MySQL 8 or PostgreSQL.

- [The business brief, section by section](#the-business-brief-section-by-section)
- [KPI dictionary](#kpi-dictionary)
- [The business question behind every SQL step](#the-business-question-behind-every-sql-step)
- [The data-trust score](#the-data-trust-score)
- [Recommendation rules](#recommendation-rules)
- [Statistics used, and why](#statistics-used-and-why)
- [Northwind: the brief, read aloud](#northwind-the-brief-read-aloud)

## The business brief, section by section

The brief is one HTML file (`business_brief.html` in the project ZIP, or **⬇ Business brief** in step 4 of the app). It needs
no internet connection and no software beyond a browser. Every chart has a **Table view** with the exact numbers. Hover over
a mark, or tab to it with the keyboard, to read its value.

| Section | What it answers | How to read it |
|---|---|---|
| **Headline** | What happened, in one sentence | The level in the latest *complete* period, its change on the previous complete period (and per day, when the two differ in length) and on the same period a year earlier, and whether volume or order value drove it. When the change's 95% interval includes zero, the headline says it is within normal variation. A partial period never becomes the headline. |
| **Data trust stamp** | Can I quote these numbers? | ✓ High · ! Good, with caveats · ✕ Use with care: the grade, after its caps (see below). Details are under *Can we trust these numbers?* |
| **Key figures** | How big, and which way | The hero number is revenue (or orders when there is no money column). Tiles show the change against the previous complete period. **Green means better for the business, red means worse**, so fewer days to ship is green even though the number fell. A change that could be noise (its 95% interval includes zero) gets a grey **within noise** chip instead; hover to read the interval. Percentages change in *points* (4.4% → 6.7% is +2.3 pts), never in percent of a percent. |
| **What this data is** | What am I looking at? | Tables and links, what one row is (the grain), orders and customers, the typical (median) order next to the average, the revenue formula, the rows left out as not sales, and how many periods are complete. |
| **What to do** | What should I act on? | Recommendations ordered by urgency: **Act now** (red dot), **Plan** (amber), **Monitor** (green). Each has the evidence (numbers), the implication (so what) and a next step, plus tags: the business area, the confidence and the SQL step. |
| **What happened** | The trend | Revenue per period; a grey bar is a partial period, shown but never compared. Next to it, the *suspected driver* (days to ship, late %, or rating) on its **own** axis: the two are never drawn on one chart with two scales, which would let the choice of scales invent a relationship. |
| **Why it moved** | The cause, in money | A waterfall from the previous period to the latest: the effect of the number of orders and the effect of the average order value, each measured at the average of the other, so the two steps add up exactly to the change. Then the change per segment (country, category, salesperson): blue added revenue, red removed it. |
| **Who brings the revenue** | Dependence and churn | The concentration curve: how much revenue the top x% of customers bring (the diagonal is a perfectly even spread). RFM segments: Champions, Loyal, Needs attention, At risk, Hibernating. Above them, how many repeat customers are overdue. |
| **What discounts cost** | Margin given away | Revenue given away per discount band, and units per line per band: do bigger discounts come with bigger orders? |
| **Can we trust these numbers?** | Data quality | Five meters, each with an icon, a score, a weight and the reason for any deduction, and above them any cap on the grade. |
| **How the numbers were made** | Method | Every assumption (grain, revenue formula, rows left out as not sales, partial periods, recency date, de-duplication) and the KPI definitions with their SQL. |

## KPI dictionary

| KPI | Definition | SQL | Good direction | Why it matters | Pitfall | Informs |
|---|---|---|---|---|---|---|
| **Revenue** | Money from sales in the period, net of discounts. | `SUM(price * quantity * (1 - discount))` | ↑ higher | The top line every plan and target starts from. | Gross vs net (before or after discounts) must be stated; a partial period looks like a fall. | Targets, budgets, investor reporting. |
| **Orders** | Distinct orders placed in the period. | `COUNT(DISTINCT order_id)` | ↑ higher | Volume: what operations, stock and service must handle. | Count orders, not order lines; cancelled, returned and test orders are left out (the assumptions list the rules). | Capacity planning, staffing. |
| **Average order value (AOV)** | Revenue per order. | `SUM(revenue) / COUNT(DISTINCT order_id)` | ↑ higher | With orders, it is one of the two levers of revenue (revenue = orders × AOV). | The average of order lines is not AOV: it understates it by the number of lines per order. | Pricing, bundles, free-shipping thresholds. |
| **Median order value** | The order in the middle: half of the orders are smaller, half are larger. | `AVG(revenue) WHERE rn IN (FLOOR((cnt + 1) / 2.0), FLOOR((cnt + 2) / 2.0))` | ↑ higher | The typical order. A few very large orders pull the average up; the median doesn't move with them. | When the average is well above the median, a target set on the average is really set on a handful of large orders. | Pricing tiers, free-shipping thresholds, sales targets. |
| **Active customers** | Distinct customers who ordered in the period. | `COUNT(DISTINCT customer_id)` | ↑ higher | The reach of the business. | A customer with no orders in the period is not active, even if they exist in the customer table. | Acquisition and retention budgets. |
| **Orders per customer** | Orders ÷ active customers. | `COUNT(*) / COUNT(DISTINCT customer_id)` | ↑ higher | Loyalty and frequency: growth without new customers. | Depends on the period length; compare like with like. | Loyalty programmes, reorder reminders. |
| **Repeat-customer rate** | Share of customers with two or more orders. | `100.0 * COUNT(customers with >= 2 orders) / COUNT(DISTINCT customer_id)` | ↑ higher | Customers who come back are cheaper to serve than new ones. | Rises naturally with a longer data window. | Retention strategy. |
| **Discount rate** | Discounts given as a share of gross revenue. | `100.0 * SUM(gross - net) / SUM(gross)` | depends | Revenue given away; worth it only if it buys volume or loyalty. | A discount that comes with bigger orders doesn't prove it caused them. | Discount policy, sales incentives. |
| **Average days to ship** | Days from order to shipment, averaged over shipped orders. | `AVG(shipped_date - order_date)` | ↓ lower | Slow fulfilment shows up later as lower ratings and fewer repeat orders. | AVG skips unshipped orders (NULL ship date); report how many there are. | Fulfilment investment, shipping SLAs. |
| **Late shipments** | Share of orders shipped after the promised date, or not shipped and already past it. | `100.0 * SUM(shipped_late) / COUNT(shipped_late)` | ↓ lower | A broken promise is what customers remember. | Needs a promised / required date; an order not shipped yet counts as late once past its date (leaving it out flatters the figure). | Carrier choice, promise dates. |
| **Average rating** | Mean customer score (text scales mapped to 1-5). | `AVG(rating_score)` | ↑ higher | The customer's own view of the experience. | Unrated orders are excluded; small samples move a lot. | Service and product fixes. |
| **Revenue concentration** | Share of revenue from the top 20% of customers. | `SUM(revenue) OVER (ORDER BY revenue DESC ...) / SUM(revenue) OVER ()` | depends | High concentration means a few departures hurt a lot. | Depends on how 'customer' is defined (account vs site). | Key-account management, credit limits. |
| **Days between orders** | Average gap between a customer's consecutive orders. | `AVG(order_date - LAG(order_date) OVER (PARTITION BY customer_id ORDER BY order_date))` | ↓ lower | The rhythm of repeat business; the yardstick for 'overdue'. | Needs customers with 2+ orders; seasonality stretches it. | Reorder reminders, churn alerts. |
| **Overdue customers** | Repeat customers silent for more than twice their own usual gap. | `days_since_last_order > 2 * AVG(gap)` | ↓ lower | The earliest churn signal, per customer. | Judged only for customers with 3+ orders. | Win-back campaigns. |
| **Period-over-period change** | Change from the previous complete period. | `100.0 * (x - LAG(x) OVER (ORDER BY period_index)) / NULLIF(LAG(x) ..., 0)` | ↑ higher | The latest direction of travel. | Never against a partial period; LAG must skip gaps. | Short-term course corrections. |
| **Year-over-year change** | Change from the same period a year earlier. | `join on period_index - 4` | ↑ higher | Removes seasonality: the cleanest growth number. | Needs at least five quarters of data. | Annual plans, investor updates. |
## The business question behind every SQL step

Each cell in the app shows this question above its SQL, so a reader knows why the query exists before reading it.

| SQL step | Business question | Usually asked by |
|---|---|---|
| `schema` | How are the tables connected? | Data / BI |
| `grain` | What does one row mean in each table? | Data / BI |
| `cardinality` | Can these tables be joined without double-counting? | Data / BI |
| `duplicates` | Is anything counted twice? | Finance, Data |
| `nulls` | What is missing, and which numbers does that affect? | Data |
| `date_range` | Which periods are complete enough to report? | Finance |
| `orphans` | Does every record link to what it should? | Data |
| `base` | One agreed source for every KPI | Data / BI |
| `reconcile` | Do the numbers tie back to the source? | Finance, Audit |
| `kpis` | How big is the business? | Leadership, Finance |
| `discounts` | What do discounts cost, and do they pay off? | Pricing, Sales |
| `kpi_by_period` | Are we growing, and is operations keeping up? | Leadership, Operations |
| `running_total` | How fast does the year build up? | Finance |
| `trend_signals` | Was the last period normal, or a break? | Finance, Analytics |
| `top_n` | What leads in each group? | Category / account managers |
| `pareto` | How dependent are we on a few customers or products? | Sales, Risk |
| `latest_record` | Who hasn't ordered recently? | Customer success |
| `purchase_gaps` | Who is about to churn? | Customer success, CRM |
| `rfm` | Who are the best customers, and who is slipping? | Marketing |
| `driver_tree` | What exactly moved revenue? | Leadership, Finance |
| `drilldown` | Is operations (shipping, service) the cause? | Operations |
| `cohort` | Do new customers come back? | Marketing, Growth |
| `explain` | Will this run fast on the real database? | Data engineering |
| `null_traps` | Are any numbers silently wrong? | Analytics |

## The data-trust score

Five dimensions, each scored 0–100, are combined into one weighted score. A dimension that does not apply (for example
Integrity, when the upload is a single table) is left out and the weights are rescaled.

| Dimension | Weight | Score | Typical deduction |
|---|---|---|---|
| **Completeness** | 25% | average % filled across the columns the analysis uses (keys, measures, dates) | `orders.shipped_date` is 2.5% empty (unshipped orders) |
| **Uniqueness** | 20% | % of tables with a unique key and no copied rows | a fact table loaded twice; duplicates are listed, and removed in the analysis views when a de-duplication key exists |
| **Integrity** | 20% | 100 × (1 − orphan rows ÷ child rows) over the accepted relationships | order lines whose product doesn't exist |
| **Validity** | 15% | 100 minus penalties: 1 point per unparsed date, value in a number column that isn't a number, shipment dated before its order, stored year / quarter / month that disagrees with the date, or impossible value (a negative price or charge, a discount out of range); 10 per ambiguous date column or text-to-number join; each kind capped at 30 | `03/04/2021`, where day and month can't be told apart |
| **Consistency** | 20% | 100 when the SQL totals reconcile with the uploaded file (40 when they don't), minus 20 when whole periods have no rows | a missing month in the middle of the data |

**Grades:** 90 or more is **High**; 75 to 90 is **Good, with caveats** (read the details before quoting a number); below
75 is **Use with care**. Scores are rounded *down* in the brief and the app, so 99.9 shows as 99, never as a perfect 100.

**Caps.** A weighted average can hide a failure: totals that don't reconcile used to average 88, "Good, with caveats". So the
grade is capped, whatever the average. Totals that don't reconcile mean **Use with care**, and any dimension below
60 means at most **Good, with caveats**. The brief and the app say which cap applied.

A partial period is labelled, not penalised: it is a property of the extract date, not an error in the data. Rows left out
as not sales (cancelled, test, another currency) are a business rule, not a quality problem, so they are reported, not
penalised either.

## Recommendation rules

Every rule below is in `insights()` in `autosql/business.py`. **Confidence** says how strong the evidence is:

- **measured**: a fact the SQL counted (a total, a share, a count).
- **indicative**: a pattern worth checking, such as an unusual period or the volume that comes with discounts.
- **needs a test**: a candidate cause. Two series moving together is not proof, so the next step is a controlled test.

| Area | Fires when | Priority | Confidence |
|---|---|---|---|
| Data | the last period is partial; the naive change is quoted, with wording that follows its sign | Act now | measured |
| Data | the *first* period is partial (the data starts part-way through it): start trends after it | Plan | measured |
| Data | whole periods have no rows | Act now | measured |
| Data | totals don't reconcile with the uploaded file | Act now | measured |
| Data | orphan records exist | Plan | measured |
| Data | impossible values (a negative price or charge, a discount out of range) | Plan | measured |
| Data | zero or negative quantities, or negative amounts: they look like returns | Monitor | measured |
| Operations (Data for a currency only) | the business rules left rows out: their share of the raw revenue, and what to do (track cancellations and returns as a KPI, convert other currencies) | Plan if 5% or more, else Monitor | measured |
| Growth | always, for the last complete period: grew or fell (per day when the periods differ in length), with its 95% interval, and year over year for that same period when a year of history exists (same sign: not seasonal; opposite sign: likely seasonal) | Act now if it fell more than 10% and the fall is real; Monitor if it is within normal variation; else Plan | measured, or indicative when within normal variation |
| Growth | a period falls outside the 99% prediction intervals of both its baseline's level and its trend (see below) | Plan | indicative |
| Drivers | the change splits into orders × order value; the larger effect, by size, is named, with whether each effect is real or within normal variation | Plan | measured, or indicative when that effect is within normal variation |
| Drivers | one segment carries 30% or more of the change | Plan | measured |
| Operations | the suspected driver moved 10% or more **in the direction that would explain the change**: more days to ship or more late orders while revenue fell (or fewer while it grew), or a falling rating while revenue fell | Act now | needs a test |
| Operations | otherwise: the driver did not explain the change | Monitor | measured |
| Operations | 10% or more of orders ship late | Plan | measured |
| Customers | concentration: the top 20% of customers' share of revenue, as displayed (rounded): 60%+ high dependence, 40–59% moderate (named account owners for class A), below 40% spread | Plan if 60%+, else Monitor | measured |
| Customers | repeat customers silent for more than twice their own usual gap | Act now if they hold 10%+ of repeat revenue, else Plan | measured |
| Customers | RFM 'At risk' customers exist | Plan | measured |
| Pricing | discounts exist: their cost as a share of gross revenue, and whether discounted lines are more than 10% bigger | Plan | indicative |

Recommendations are sorted by priority, then by area. Data comes first, because a data problem changes how to read everything
else.

## Statistics used, and why

Every formula below is in `autosql/checklib.py`, written in plain Python (no SciPy), and the same code runs in the app and
in the exported notebook.

**Revenue = orders × average order value, split exactly.** Between two periods, the volume effect is the change in orders
valued at the *average* of the two order values, and the order-value effect is the change in order value times the
*average* of the two order counts: `Δorders × (AOV₀ + AOV₁) / 2` and `ΔAOV × (orders₀ + orders₁) / 2`. This is the Shapley
split of a product of two factors: the two effects add up to the change exactly, with no "both" left over. The textbook
split (each effect at the *old* value of the other) leaves an interaction term, and its shares then depend on which effect
is measured first. "How much of the movement" is each effect's share **by size**, so the shares add up to 100% even when
the effects pull in opposite directions. A share of the *net* change can exceed 100%, which reads as nonsense to a manager.

**Real change or noise: a 95% interval on every change.** Each KPI's change between the two compared periods gets a 95%
confidence interval, and a change is called real only when the interval excludes zero:

| KPI | Interval | Assumes |
|---|---|---|
| orders, customers | ratio of two Poisson rates, on the log scale | orders arrive independently |
| revenue | compound Poisson: the variance of a period's total is n × (sd² + mean²) of its order values | the same, with any order size |
| average order value, days to ship, rating | Welch's interval for a difference of means, with Welch–Satterthwaite degrees of freedom (normal above 30) | orders are independent |
| late % and other shares | Newcombe's hybrid Wilson-score interval, in points | it stays accurate with few late orders |

Big orders make order value noisy, so a large change in AOV can still be within noise: Northwind's +8.3% has an interval
from −20.3% to +36.9%. Each KPI is tested on its own, with no correction for testing several at once.

**Periods of different length are compared per day.** When the two periods' days with data differ by 3% or
more (February against January), revenue, orders and customers are also compared per day, and the verdict uses that.
Identical sales every day would otherwise read as "orders fell 9.7%" in February.

**Unusual periods: two prediction intervals, not a fixed z.** A period is compared with up to 8 earlier complete
periods (at least 4; partial periods never enter the baseline). It is flagged only when it is far from
both their **level** and their **trend**:

- level: |z| exceeds `t(0.995, n − 1) × √(1 + 1/n)`, the edge of the 99% prediction interval for a new value from a baseline
  of *n* periods. The limits are: 4 periods: **6.53** · 5 periods: **5.04** · 6 periods: **4.36** · 7 periods: **3.96** · 8 periods: **3.71**;
- trend: outside the 99% prediction interval of a least-squares line through the same baseline (5 or more periods), so a
  series that grows steadily is not flagged just for growing.

A fixed "|z| > 2 against the last 4 periods" sounds reasonable, but a spread measured on 4 points is so noisy that normal
variation crosses it in about one period in six. The test suite checks the rule on pure-noise data (at most one flag across
six datasets) and on a planted spike (found).

**Correlation needs enough periods, and an interval.** The drilldown reports a correlation between revenue and the driver
only with 6 or more complete periods, on the levels and on the period-to-period changes, each with a 95% interval (Fisher's
z). Two series that both trend correlate on levels even when they are unrelated, so only the changes can show a
relationship, and even then correlation is not proof.

**Typical values.** Next to every average, the median and the 90th percentile, found by rank so that MySQL (which has no
`PERCENTILE_CONT`) gives the same answer as PostgreSQL. When the average is well above the median, a few large orders pull it
up, and a target set on the average is really set on them.

**Overdue customers.** Overdue means silent for more than twice the customer's *own* average gap between orders (computed
with `LAG`). This adapts to each customer's rhythm, unlike a fixed "no order in 180 days" rule. Only customers with 3 or more
orders are judged.

**RFM.** Recency, frequency and monetary value are each scored 1–5 with `CEIL(5 × CUME_DIST())`: the share of customers at or
below a value, so customers with equal values always get equal scores (`NTILE(5)` would split them by id, and decide who
gets a campaign arbitrarily). Recency is measured to the last date in the data, not to today, so old data doesn't make
every customer look lost.

## Northwind: the brief, read aloud

From [`examples/northwind/business_brief.html`](../examples/northwind/business_brief.html):

> Revenue reached **298,491.55 in 1998-Q1**, **+64.3%** on 1997-Q4 (95% CI +15.0% to +134.7%) and **+115.8%** year over
> year, so the growth is real and not seasonal. **84%** of the movement came from more orders (120 → 182, +51.7%); order
> value rose 8.3%, but that is within normal variation (95% CI −20.3% to +36.9%). **Beverages** drove 57% of the change.
> Shipping time is **not** the cause: it improved slightly (9.2 → 8.6 days) while revenue rose. The typical order is
> 943.25, well below the 1,525.05 average. The top 20% of customers bring 60% of revenue, and 34 customers make up class A.
> 5 repeat customers are overdue and 7 once-frequent customers are slipping. Discounts cost 6.5% of gross revenue, on 39% of
> lines, and discounted lines are 25% bigger. **1998-Q2 is incomplete** (36 of 91 days): a naive report would show −52.4%.
> Data trust: **99/100, High**.
