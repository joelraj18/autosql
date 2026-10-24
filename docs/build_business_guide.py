"""Write docs/business_guide.md from the live definitions: the KPI dictionary, the business question per SQL step, the trust
weights and the statistical limits, so that the guide can't drift from the code (tests/test_business.py checks it).

    python docs/build_business_guide.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from autosql.business import BUSINESS_QUESTIONS, TRUST_WEIGHTS, kpi_dictionary_markdown  # noqa: E402
from autosql.decision import TREND_MIN_BASELINE, TREND_WINDOW, TREND_Z_LIMIT  # noqa: E402

q_rows = "\n".join(f"| `{k}` | {q} | {who} |" for k, (q, who) in BUSINESS_QUESTIONS.items())
limits = " · ".join(f"{n} periods: **{v}**" for n, v in TREND_Z_LIMIT.items())
w = {k: f"{v:.0%}" for k, v in TRUST_WEIGHTS.items()}

text = f'''# Business guide

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
| **Headline** | What happened, in one sentence | The level in the latest *complete* period, its change on the previous complete period and on the same period a year earlier, and whether volume or order value drove it. A partial period never becomes the headline. |
| **Data trust stamp** | Can I quote these numbers? | ✓ 90+ High · ! 75–90 Good, with caveats · ✕ below 75 Use with care. Details are under *Can we trust these numbers?* |
| **Key figures** | How big, and which way | The hero number is revenue (or orders when there is no money column). Tiles show the change against the previous complete period. **Green means better for the business, red means worse**, so fewer days to ship is green even though the number fell. Percentages change in *points* (4.4% → 6.7% is +2.3 pts), never in percent of a percent. |
| **What this data is** | What am I looking at? | Tables and links, what one row is (the grain), orders and customers, the revenue formula and how many periods are complete. |
| **What to do** | What should I act on? | Recommendations ordered by urgency: **Act now** (red dot), **Plan** (amber), **Monitor** (green). Each has the evidence (numbers), the implication (so what) and a next step, plus tags: the business area, the confidence and the SQL step. |
| **What happened** | The trend | Revenue per period; a grey bar is a partial period, shown but never compared. Next to it, the *suspected driver* (days to ship, late %, or rating) on its **own** axis: the two are never drawn on one chart with two scales, which would let the choice of scales invent a relationship. |
| **Why it moved** | The cause, in money | A waterfall from the previous period to the latest: the effect of the number of orders, of the average order value, and of both together. The three steps add up exactly to the change. Then the change per segment (country, category, salesperson): blue added revenue, red removed it. |
| **Who brings the revenue** | Dependence and churn | The concentration curve: how much revenue the top x% of customers bring (the diagonal is a perfectly even spread). RFM segments: Champions, Loyal, Needs attention, At risk, Hibernating. Above them, how many repeat customers are overdue. |
| **What discounts cost** | Margin given away | Revenue given away per discount band, and units per line per band: do bigger discounts come with bigger orders? |
| **Can we trust these numbers?** | Data quality | Five meters, each with an icon, a score, a weight and the reason for any deduction. |
| **How the numbers were made** | Method | Every assumption (grain, revenue formula, partial periods, recency date, de-duplication) and the KPI definitions with their SQL. |

## KPI dictionary

{kpi_dictionary_markdown()}
## The business question behind every SQL step

Each cell in the app shows this question above its SQL, so a reader knows why the query exists before reading it.

| SQL step | Business question | Usually asked by |
|---|---|---|
{q_rows}

## The data-trust score

Five dimensions, each scored 0–100, are combined into one weighted score. A dimension that does not apply (for example
Integrity, when the upload is a single table) is left out and the weights are rescaled.

| Dimension | Weight | Score | Typical deduction |
|---|---|---|---|
| **Completeness** | {w["Completeness"]} | average % filled across the columns the analysis uses (keys, measures, dates) | `orders.shipped_date` is 2.5% empty (unshipped orders) |
| **Uniqueness** | {w["Uniqueness"]} | % of tables with a unique key and no copied rows | a fact table loaded twice; duplicates are listed, and removed in the analysis views when a de-duplication key exists |
| **Integrity** | {w["Integrity"]} | 100 × (1 − orphan rows ÷ child rows) over the accepted relationships | order lines whose product doesn't exist |
| **Validity** | {w["Validity"]} | 100 minus penalties: 1 point per unparsed date, shipment dated before its order, or stored year / quarter / month that disagrees with the date; 10 per ambiguous date column or text-to-number join; each kind capped at 30 | `03/04/2021`, where day and month can't be told apart |
| **Consistency** | {w["Consistency"]} | 100 when the SQL totals reconcile with the uploaded file (40 when they don't), minus 20 when whole periods have no rows | a missing month in the middle of the data |

**Grades:** 90 or more is **High**; 75 to 90 is **Good, with caveats** (read the details before quoting a number); below
75 is **Use with care**. Scores are rounded *down* in the brief and the app, so 99.9 shows as 99, never as a perfect 100.

A partial period is labelled, not penalised: it is a property of the extract date, not an error in the data.

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
| Growth | always, for the last complete period: grew or fell, with year over year when a year of history exists (same sign: not seasonal; opposite sign: likely seasonal) | Act now if it fell more than 10%, else Plan | measured |
| Growth | a period falls outside its baseline's 99% prediction interval (see below) | Plan | indicative |
| Drivers | the change splits into orders × order value; the larger effect, by size, is named | Plan | measured |
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

**Revenue = orders × average order value, split exactly.** Between two periods,
`Δrevenue = Δorders × AOV₀ + orders₀ × ΔAOV + Δorders × ΔAOV`: a volume effect, an order-value effect and their
interaction. The three always add up to the change. "How much of the movement" is each effect's share **by size**, so the
shares add up to 100% even when the effects pull in opposite directions. A share of the *net* change can exceed 100%, which
reads as nonsense to a manager.

**Unusual periods: a prediction interval, not a fixed z.** A period is compared with up to {TREND_WINDOW} earlier complete
periods (at least {TREND_MIN_BASELINE}; partial periods never enter the baseline). With a baseline of *n* periods, the period
is flagged only when |z| exceeds `t(0.995, n − 1) × √(1 + 1/n)`, the edge of the 99% prediction interval for a new value.
The limits are: {limits}. A fixed "|z| > 2 against the last 4 periods" sounds reasonable, but a spread measured on 4 points
is so noisy that normal variation crosses it in about one period in six. The test suite checks the rule on pure-noise data
(at most one flag across six datasets) and on a planted spike (found).

**Correlation needs enough periods.** The drilldown reports a correlation between revenue and the driver only with 6 or more
complete periods, and always adds that correlation is not proof.

**Overdue customers.** Overdue means silent for more than twice the customer's *own* average gap between orders (computed
with `LAG`). This adapts to each customer's rhythm, unlike a fixed "no order in 180 days" rule. Only customers with 3 or more
orders are judged.

**RFM.** Recency, frequency and monetary value are each scored 1–5 with `NTILE(5)`. Recency is measured to the last date in
the data, not to today, so old data doesn't make every customer look lost. `NTILE` splits ties arbitrarily, so a customer on
a boundary can land in either bucket.

## Northwind: the brief, read aloud

From [`examples/northwind/business_brief.html`](../examples/northwind/business_brief.html):

> Revenue reached **298,491.55 in 1998-Q1**, **+64.3%** on 1997-Q4 and **+115.8%** year over year, so the growth is not
> seasonal. **80%** of the movement came from more orders (120 → 182), the rest mostly from slightly bigger ones (AOV +8.3%).
> **Beverages** drove 57% of the change. Shipping time is **not** the cause: it improved slightly (9.2 → 8.6 days) while
> revenue rose. The top 20% of customers bring 60% of revenue, and 34 customers make up class A. 5 repeat customers are
> overdue and 8 once-frequent customers are slipping. Discounts cost 6.5% of gross revenue, on 39% of lines, and discounted
> lines are 25% bigger. **1998-Q2 is incomplete** (36 of 91 days): a naive report would show −52.4%.
> Data trust: **99/100, High**.
'''
(ROOT / "docs" / "business_guide.md").write_text(text, encoding="utf-8")
print(f"wrote docs/business_guide.md ({text.count(chr(10))} lines)")
