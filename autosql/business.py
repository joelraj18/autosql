"""The business layer: what each number means to the business, how far it can be trusted, and what to do about it.

Everything here reads the ``Decision`` and the ``metrics`` the generated SQL produced; nothing is estimated
outside the data. The pieces:

* ``KPI_DICTIONARY``      - every KPI: definition, SQL formula, good direction, why it matters, pitfalls, the decision it informs
* ``BUSINESS_QUESTIONS``  - the question each SQL step answers, and who asks it
* ``trust_score()``       - a 0-100 data-trust score from five data-quality dimensions
* ``insights()``          - rule-based findings, each with evidence, the business implication and a recommended action
* ``executive_summary()`` - a headline, KPI tiles with period-over-period deltas, the top insights and the trust score
* ``data_dictionary()``   - one row per column: type, role, keys, completeness and how the analysis uses it
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .decision import Decision


# ═════════════════════════════════════════════════════════════════════════
# KPI dictionary and business questions
# ═════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class KPI:
    key: str
    name: str
    definition: str
    formula: str          # SQL, against the analysis views
    direction: str        # up | down | context  (which way is good)
    why: str
    pitfall: str
    decision: str         # what a manager decides with it


KPI_DICTIONARY: tuple[KPI, ...] = (
    KPI("revenue", "Revenue", "Money from sales in the period, net of discounts.", "SUM(price * quantity * (1 - discount))", "up",
        "The top line every plan and target starts from.", "Gross vs net (before or after discounts) must be stated; a partial period looks like a fall.",
        "Targets, budgets, investor reporting."),
    KPI("orders", "Orders", "Distinct orders placed in the period.", "COUNT(DISTINCT order_id)", "up",
        "Volume: what operations, stock and service must handle.", "Count orders, not order lines; cancelled or test orders may need excluding.",
        "Capacity planning, staffing."),
    KPI("aov", "Average order value (AOV)", "Revenue per order.", "SUM(revenue) / COUNT(DISTINCT order_id)", "up",
        "With orders, it is one of the two levers of revenue (revenue = orders × AOV).",
        "The average of order lines is not AOV: it understates it by the number of lines per order.",
        "Pricing, bundles, free-shipping thresholds."),
    KPI("customers", "Active customers", "Distinct customers who ordered in the period.", "COUNT(DISTINCT customer_id)", "up",
        "The reach of the business.", "A customer with no orders in the period is not active, even if they exist in the customer table.",
        "Acquisition and retention budgets."),
    KPI("orders_per_customer", "Orders per customer", "Orders ÷ active customers.", "COUNT(*) / COUNT(DISTINCT customer_id)", "up",
        "Loyalty and frequency: growth without new customers.", "Depends on the period length; compare like with like.",
        "Loyalty programmes, reorder reminders."),
    KPI("repeat_customer_pct", "Repeat-customer rate", "Share of customers with two or more orders.",
        "100.0 * COUNT(customers with >= 2 orders) / COUNT(DISTINCT customer_id)", "up",
        "Customers who come back are cheaper to serve than new ones.", "Rises naturally with a longer data window.", "Retention strategy."),
    KPI("discount_pct", "Discount rate", "Discounts given as a share of gross revenue.", "100.0 * SUM(gross - net) / SUM(gross)", "context",
        "Revenue given away; worth it only if it buys volume or loyalty.", "A discount that comes with bigger orders doesn't prove it caused them.",
        "Discount policy, sales incentives."),
    KPI("avg_ship_days", "Average days to ship", "Days from order to shipment, averaged over shipped orders.", "AVG(shipped_date - order_date)", "down",
        "Slow fulfilment shows up later as lower ratings and fewer repeat orders.",
        "AVG skips unshipped orders (NULL ship date); report how many there are.", "Fulfilment investment, shipping SLAs."),
    KPI("late_pct", "Late shipments", "Share of shipped orders sent after the promised date.", "100.0 * SUM(shipped_late) / COUNT(shipped_late)", "down",
        "A broken promise is what customers remember.", "Needs a promised / required date; orders not shipped yet are excluded.",
        "Carrier choice, promise dates."),
    KPI("avg_rating", "Average rating", "Mean customer score (text scales mapped to 1-5).", "AVG(rating_score)", "up",
        "The customer's own view of the experience.", "Unrated orders are excluded; small samples move a lot.", "Service and product fixes."),
    KPI("top20_share", "Revenue concentration", "Share of revenue from the top 20% of customers.",
        "SUM(revenue) OVER (ORDER BY revenue DESC ...) / SUM(revenue) OVER ()", "context",
        "High concentration means a few departures hurt a lot.", "Depends on how 'customer' is defined (account vs site).",
        "Key-account management, credit limits."),
    KPI("days_between_orders", "Days between orders", "Average gap between a customer's consecutive orders.",
        "AVG(order_date - LAG(order_date) OVER (PARTITION BY customer_id ORDER BY order_date))", "down",
        "The rhythm of repeat business; the yardstick for 'overdue'.", "Needs customers with 2+ orders; seasonality stretches it.",
        "Reorder reminders, churn alerts."),
    KPI("overdue_customers", "Overdue customers", "Repeat customers silent for more than twice their own usual gap.",
        "days_since_last_order > 2 * AVG(gap)", "down", "The earliest churn signal, per customer.",
        "Judged only for customers with 3+ orders.", "Win-back campaigns."),
    KPI("qoq", "Period-over-period change", "Change from the previous complete period.",
        "100.0 * (x - LAG(x) OVER (ORDER BY period_index)) / NULLIF(LAG(x) ..., 0)", "up",
        "The latest direction of travel.", "Never against a partial period; LAG must skip gaps.", "Short-term course corrections."),
    KPI("yoy", "Year-over-year change", "Change from the same period a year earlier.", "join on period_index - 4", "up",
        "Removes seasonality: the cleanest growth number.", "Needs at least five quarters of data.", "Annual plans, investor updates."),
)
KPI_BY_KEY = {k.key: k for k in KPI_DICTIONARY}

# The question each generated step answers, and who usually asks it.
BUSINESS_QUESTIONS: dict[str, tuple[str, str]] = {
    "schema": ("How are the tables connected?", "Data / BI"),
    "grain": ("What does one row mean in each table?", "Data / BI"),
    "cardinality": ("Can these tables be joined without double-counting?", "Data / BI"),
    "duplicates": ("Is anything counted twice?", "Finance, Data"),
    "nulls": ("What is missing, and which numbers does that affect?", "Data"),
    "date_range": ("Which periods are complete enough to report?", "Finance"),
    "orphans": ("Does every record link to what it should?", "Data"),
    "base": ("One agreed source for every KPI", "Data / BI"),
    "reconcile": ("Do the numbers tie back to the source?", "Finance, Audit"),
    "kpis": ("How big is the business?", "Leadership, Finance"),
    "discounts": ("What do discounts cost, and do they pay off?", "Pricing, Sales"),
    "kpi_by_period": ("Are we growing, and is operations keeping up?", "Leadership, Operations"),
    "running_total": ("How fast does the year build up?", "Finance"),
    "trend_signals": ("Was the last period normal, or a break?", "Finance, Analytics"),
    "top_n": ("What leads in each group?", "Category / account managers"),
    "pareto": ("How dependent are we on a few customers or products?", "Sales, Risk"),
    "latest_record": ("Who hasn't ordered recently?", "Customer success"),
    "purchase_gaps": ("Who is about to churn?", "Customer success, CRM"),
    "rfm": ("Who are the best customers, and who is slipping?", "Marketing"),
    "driver_tree": ("What exactly moved revenue?", "Leadership, Finance"),
    "drilldown": ("Is operations (shipping, service) the cause?", "Operations"),
    "cohort": ("Do new customers come back?", "Marketing, Growth"),
    "explain": ("Will this run fast on the real database?", "Data engineering"),
    "null_traps": ("Are any numbers silently wrong?", "Analytics"),
}


def kpi_dictionary_markdown() -> str:
    lines = ["| KPI | Definition | SQL | Good direction | Why it matters | Pitfall | Informs |", "|---|---|---|---|---|---|---|"]
    arrow = {"up": "↑ higher", "down": "↓ lower", "context": "depends"}
    for k in KPI_DICTIONARY:
        lines.append(f"| **{k.name}** | {k.definition} | `{k.formula}` | {arrow[k.direction]} | {k.why} | {k.pitfall} | {k.decision} |")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════
# Trust score
# ═════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class TrustDimension:
    name: str
    score: float          # 0-100
    weight: float
    detail: str


@dataclass(frozen=True)
class TrustScore:
    score: float
    grade: str            # High | Good, with caveats | Use with care
    dimensions: tuple[TrustDimension, ...]


TRUST_WEIGHTS = {"Completeness": 0.25, "Uniqueness": 0.20, "Integrity": 0.20, "Validity": 0.15, "Consistency": 0.20}


def _used_columns(d: Decision) -> list[tuple[str, str]]:
    cols: list[tuple[str, str]] = []
    if d.kpi:
        rev = d.revenue
        cols += [(rev.table, c) for c in (rev.price if not rev.price_rel else None, rev.quantity, rev.discount, rev.amount) if c]
        if d.order_key:
            cols.append((d.fact, d.order_key))
        for x in (d.customer, d.date):
            if x:
                cols.append(x)
        if d.lead:
            cols += [(d.lead[0], d.lead[2])] + ([(d.lead[0], d.required)] if d.required else [])
        if d.rating:
            cols.append((d.rating.table, d.rating.name))
        cols += [(x.table, x.column) for x in d.dimensions]
    else:
        cols = [(t.name, c.name) for t in d.profile.tables.values() for c in t.columns if c.role in ("identifier", "measure", "datetime")]
    return list(dict.fromkeys(cols))


def trust_score(d: Decision, metrics: dict) -> TrustScore:
    """Five data-quality dimensions, each 0-100, weighted into one score. Every deduction is explained."""
    p = d.profile
    dims = []

    used = _used_columns(d)
    if used:
        filled = [1 - p.col(t, c).null_frac for t, c in used]
        worst = min(used, key=lambda tc: 1 - p.col(*tc).null_frac)
        score = 100 * sum(filled) / len(filled)
        detail = f"{len(used)} columns the analysis uses are {score:.1f}% filled"
        if p.col(*worst).n_null:
            detail += f"; most gaps in `{worst[0]}.{worst[1]}` ({p.col(*worst).null_frac:.1%} empty)"
        dims.append(TrustDimension("Completeness", score, TRUST_WEIGHTS["Completeness"], detail + "."))

    tables = [t for t in p.tables.values() if t.n_rows]
    clean = [t for t in tables if t.key_status in ("single", "composite") and not t.exact_duplicates]
    dup_rows = sum(t.extra_rows for t in tables if t.key_status == "intended") + sum(t.exact_duplicates for t in tables if t.key_status == "none")
    score = 100 * len(clean) / len(tables) if tables else 100.0
    detail = (f"{len(clean)} of {len(tables)} tables have a unique key and no copied rows"
              + (f"; {dup_rows:,} duplicate row(s) found and removed in the analysis" if dup_rows else "") + ".")
    dims.append(TrustDimension("Uniqueness", score, TRUST_WEIGHTS["Uniqueness"], detail))

    rels = [r for r in d.schema.accepted]
    if rels:
        child_rows = sum(p[r.child_table].n_rows for r in rels)
        orphan_rows = sum(r.orphan_rows for r in rels)
        score = 100 * (1 - orphan_rows / child_rows) if child_rows else 100.0
        bad = [r for r in rels if r.orphan_rows]
        detail = (f"all {len(rels)} relationships resolve, no orphan rows" if not bad else
                  f"{orphan_rows:,} orphan row(s) in {len(bad)} of {len(rels)} relationships (kept as 'Unknown')")
        if d.schema.suggested:
            detail += f"; {len(d.schema.suggested)} link(s) left for you to confirm"
        dims.append(TrustDimension("Integrity", score, TRUST_WEIGHTS["Integrity"], detail + "."))

    # validity: one point per bad value (capped), ten per column-level warning
    issues, penalty = [], 0.0
    checks = ((d.quality.get("unparsed_dates", 0), "date value(s) that didn't parse", 1),
              (d.quality.get("ambiguous_dates", 0), "date column(s) where day and month can't be told apart", 10),
              (d.quality.get("type_mismatches", 0), "link(s) joining text to numbers", 10),
              (metrics.get("negative_lead") or 0, "shipment(s) dated before their order", 1),
              (metrics.get("date_part_mismatches") or 0, "stored year / quarter / month value(s) that disagree with the date", 1))
    for n, what, weight in checks:
        if n:
            issues.append(f"{n:,} {what}")
            penalty += min(30, weight * n)
    dims.append(TrustDimension("Validity", max(0.0, 100 - penalty), TRUST_WEIGHTS["Validity"],
                               ("; ".join(issues) if issues else "dates parse, and no impossible values (e.g. shipped before ordered)") + "."))

    if "reconciled" in metrics:
        score = 100.0 if metrics["reconciled"] else 40.0
        detail = ("totals agree between the raw table, the views and the uploaded file" if metrics["reconciled"] else
                  "totals do not reconcile between the views and the uploaded file")
    else:
        score, detail = 100.0, "no KPI totals to reconcile (validation only)"
    gaps = metrics.get("missing_periods") or d.missing_periods
    partial = [d.label(x) for x in d.partial_periods]
    if gaps:
        score -= 20
        detail += f"; no rows at all in {', '.join(gaps[:4])}"
    if partial:
        detail += f"; {', '.join(partial)} is partial (labelled, not penalised)"
    dims.append(TrustDimension("Consistency", max(score, 0.0), TRUST_WEIGHTS["Consistency"], detail + "."))

    total_weight = sum(x.weight for x in dims)
    overall = sum(x.score * x.weight for x in dims) / total_weight if total_weight else 0.0
    grade = "High" if overall >= 90 else "Good, with caveats" if overall >= 75 else "Use with care"
    return TrustScore(overall, grade, tuple(dims))


# ═════════════════════════════════════════════════════════════════════════
# Insights and recommendations
# ═════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Insight:
    area: str             # Growth | Drivers | Customers | Pricing | Operations | Data
    headline: str
    evidence: str         # the numbers behind it
    implication: str      # so what, for the business
    action: str           # the recommended next step
    priority: int         # 1 = act now, 2 = plan, 3 = monitor
    confidence: str       # measured | indicative | needs a test
    source: str           # the SQL step that produced the evidence


def effect_shares(split: dict) -> dict[str, float]:
    """Each effect's share of the total movement, by size: volume, order value and interaction always add up to 100%
    (a share of the *net* change can exceed 100% when effects pull in opposite directions)."""
    sizes = {k: abs(split[k]) for k in ("volume", "order_value", "interaction")}
    total = sum(sizes.values()) or 1.0
    return {k: v / total for k, v in sizes.items()}


def _money(v: float) -> str:
    return f"{v:,.2f}" if abs(v) < 1e6 else f"{v / 1e6:,.2f}M"


def insights(d: Decision, metrics: dict) -> list[Insight]:
    m, out = metrics, []
    main = m.get("main_measure", "revenue")

    for label in [d.label(x) for x in d.partial_periods][-1:]:
        naive = m.get("naive_change") or {}
        p = next(x for x in d.partial_periods if d.label(x) == label)
        out.append(Insight(
            "Data", f"{label} is incomplete", f"only {p.covered} of {p.days} days have data"
            + (f"; a naive report would show {naive['pct']:+.1f}%" if naive.get("period") == label else ""),
            "Any report that includes it will show a fall that is just missing days.",
            f"Report {label} only once it is complete, or compare it with the same days a year earlier.", 1, "measured", "date_range"))
    if m.get("missing_periods"):
        out.append(Insight("Data", "Periods with no data at all", ", ".join(m["missing_periods"][:6]),
                           "Either the business stopped or the extract is incomplete; trends across the gap are unreliable.",
                           "Confirm with the data owner whether these periods were really empty.", 1, "measured", "date_range"))

    q = m.get("qoq_last_complete")
    if q:
        yoy = m.get("yoy")
        direction = "grew" if q["pct"] >= 0 else "fell"
        evidence = f"{q['from']} → {q['to']}: {q['pct']:+.1f}%" + (f"; year over year {yoy['pct']:+.1f}%" if yoy else "")
        if yoy and (yoy["pct"] >= 0) == (q["pct"] >= 0):
            implication = "The change also holds against the same period a year earlier, so it is not just seasonality."
        elif yoy:
            implication = "The year-over-year change points the other way: part of this move is likely seasonal."
        else:
            implication = "Without a year of history, seasonality can't be ruled out yet."
        out.append(Insight("Growth", f"{main.capitalize()} {direction} {abs(q['pct']):.1f}% in {q['to']}", evidence, implication,
                           "Use the last complete period as the headline, and the year-over-year figure for plans."
                           if q["pct"] >= 0 else "Find which part of the business fell (see the driver tree) before cutting costs.",
                           1 if q["pct"] < -10 else 2, "measured", "kpi_by_period"))

    split = m.get("driver_split")
    if split and split.get("delta"):
        shares = effect_shares(split)
        main_driver = "order volume" if shares["volume"] >= shares["order_value"] else "order value"
        up = split["delta"] >= 0
        if main_driver == "order volume":
            implication = ("Growth comes from more orders, so fulfilment, stock and service have to scale with it."
                           if up else "Fewer orders, not smaller ones, explain the fall: an acquisition or retention problem.")
            action = ("Plan capacity for the higher volume; treat order value (bundles, thresholds) as a separate lever."
                      if up else "Check new vs returning customers and the overdue list before changing prices.")
        else:
            implication = ("Customers spend more per order: price, mix or larger baskets."
                           if up else "Orders got smaller: price, mix or a shift towards smaller customers.")
            action = "Split order value into units per order and price per unit to see which moved."
        out.append(Insight("Drivers", f"{main_driver.capitalize()} accounts for {shares['volume' if main_driver == 'order volume' else 'order_value']:.0%} "
                           "of the movement",
                           f"{split['from']} → {split['to']}: {_money(split['delta'])} = volume {_money(split['volume'])} + order value "
                           f"{_money(split['order_value'])} + both {_money(split['interaction'])}", implication, action, 2, "measured", "driver_tree"))
    for col, c in list((m.get("top_contributors") or {}).items())[:2]:
        if abs(c["share"]) >= 30:
            out.append(Insight("Drivers", f"{c['segment']} drove {c['share']:.0f}% of the change", f"by {col}: {_money(c['change'])}",
                               "One segment carries the result; if it reverses, the total reverses with it.",
                               f"Find out what changed in {c['segment']} (customers, products, prices) and whether it can last.", 2, "measured",
                               "driver_tree"))

    dv = m.get("driver_vs_symptom")
    if dv:
        if abs(dv["driver_change_pct"]) >= 10 and (dv["driver_change_pct"] > 0) != (dv["symptom_change_pct"] > 0):
            out.append(Insight("Operations", f"{dv['driver'].replace('avg_', '').replace('_', ' ')} moved {dv['driver_change_pct']:+.0f}% "
                               f"while {main} moved {dv['symptom_change_pct']:+.0f}%",
                               "last two complete periods", "Operations is a candidate cause of the change, but correlation is not proof.",
                               "Run a controlled test: e.g. a faster shipping promise for a random half of orders, then compare.", 1,
                               "needs a test", "drilldown"))
        else:
            out.append(Insight("Operations", f"{dv['driver'].replace('avg_', '').replace('_', ' ')} did not explain the change",
                               f"{dv['driver_change_pct']:+.1f}% vs {main} {dv['symptom_change_pct']:+.1f}%",
                               "The cause lies elsewhere (volume, mix, segments).", "Keep monitoring it; look at the driver tree instead.",
                               3, "measured", "drilldown"))
    k = m.get("kpis") or {}
    if k.get("late_pct") is not None and k["late_pct"] >= 10:
        out.append(Insight("Operations", f"{k['late_pct']:.0f}% of orders ship late", "shipped after the promised date",
                           "Late deliveries erode repeat business.", "Review carriers and promise dates for the worst segments.", 2,
                           "measured", "kpis"))

    pareto = (m.get("pareto") or {}).get("customers")
    if pareto:
        share = pareto["top20_revenue_pct"]
        out.append(Insight("Customers", f"The top 20% of customers bring {share:.0f}% of revenue",
                           f"class A: {pareto['a_entities']} customers ({pareto['a_entities_pct']:.0f}%) = {pareto['a_revenue_pct']:.0f}% of revenue",
                           "High dependence on a few accounts." if share >= 60 else "Revenue is fairly spread; no single group dominates.",
                           f"Give the {pareto['a_entities']} class-A customers named account owners and watch their order gaps."
                           if share >= 50 else "Keep broad-based retention; concentration is not a risk here.", 2 if share >= 60 else 3,
                           "measured", "pareto"))
    gaps = m.get("purchase_gaps")
    if gaps and gaps.get("overdue"):
        share = gaps.get("overdue_revenue_pct")
        out.append(Insight("Customers", f"{gaps['overdue']} repeat customers are overdue",
                           f"silent for more than twice their usual gap (typical gap {gaps['avg_days']:.0f} days)"
                           + (f"; {share:.0f}% of repeat-customer revenue" if share is not None else ""),
                           "These are the earliest churn signals, before any fixed '180 days' rule fires.",
                           "Contact them now (win-back), largest first; the list is in the purchase-gaps step.",
                           1 if (share or 0) >= 10 else 2, "measured", "purchase_gaps"))
    rfm = m.get("rfm") or {}
    if "At risk" in rfm and rfm["At risk"]["customers"]:
        r = rfm["At risk"]
        out.append(Insight("Customers", f"{r['customers']} once-frequent customers are slipping",
                           f"RFM 'At risk': {r['revenue_pct']:.1f}% of revenue", "They used to order often and have gone quiet.",
                           "Reactivation offer or account call; compare with the overdue list.", 2, "measured", "rfm"))

    disc = m.get("discounts")
    if disc:
        lift = None
        if disc.get("qty_discounted") and disc.get("qty_full_price"):
            lift = 100 * (disc["qty_discounted"] / disc["qty_full_price"] - 1)
        out.append(Insight("Pricing", f"Discounts cost {disc['pct_of_gross']:.1f}% of gross revenue",
                           f"{_money(disc['given'])} on {disc['discounted_lines_pct']:.0f}% of lines"
                           + (f"; discounted lines are {lift:+.0f}% larger" if lift is not None else ""),
                           ("Discounts coincide with bigger lines, which may justify them." if lift is not None and lift > 10 else
                            "Discounts don't come with bigger lines: much of it may be margin given away."),
                           "Test discount levels with a controlled experiment before changing the policy." if lift is not None and lift > 10 else
                           "Cap or target discounts; measure the effect on volume.", 2, "indicative", "discounts"))

    for u in (m.get("unusual_periods") or [])[-1:]:
        out.append(Insight("Growth", f"{u['period']} is far from its recent trend", f"z = {u['z']:+.1f} against the previous four periods",
                           "A break like this is either a new level or a one-off (a large order, a promotion, a data issue).",
                           "Check the largest orders of that period before planning on it.", 2, "indicative", "trend_signals"))

    if m.get("orphans"):
        n = sum(m["orphans"].values())
        out.append(Insight("Data", f"{n:,} records point at nothing", ", ".join(f"{k}: {v:,}" for k, v in m["orphans"].items()),
                           "They are kept as 'Unknown' in the analysis, but reports by customer or product will miss them.",
                           "Fix the missing parent records at the source.", 2, "measured", "orphans"))
    if m.get("reconciled") is False:
        out.append(Insight("Data", "Totals don't reconcile", "SQL vs uploaded file", "KPIs may be inflated or incomplete.",
                           "Resolve duplicates and orphans before sharing any number.", 1, "measured", "reconcile"))
    out.sort(key=lambda i: (i.priority, ["Data", "Growth", "Drivers", "Customers", "Operations", "Pricing"].index(i.area)))
    return out


# ═════════════════════════════════════════════════════════════════════════
# Executive summary
# ═════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Tile:
    key: str
    label: str
    value: float
    unit: str             # money | count | pct | days | score
    delta_pct: float | None = None    # % change; for percentage tiles, the change in percentage points
    good: bool | None = None          # is the change good for the business?
    note: str = ""


@dataclass
class Summary:
    title: str
    period: str                       # the period the tiles describe
    compared_with: str
    headline: str
    tiles: list[Tile] = field(default_factory=list)
    insights: list[Insight] = field(default_factory=list)
    trust: TrustScore | None = None
    coverage: str = ""


TILE_SPECS = (("revenue", "Revenue", "money", "up"), ("orders", "Orders", "count", "up"), ("avg_order_value", "Average order value", "money", "up"),
              ("customers", "Active customers", "count", "up"), ("avg_ship_days", "Days to ship", "days", "down"),
              ("avg_delivery_days", "Days to deliver", "days", "down"), ("late_pct", "Late shipments", "pct", "down"),
              ("avg_rating", "Average rating", "score", "up"))


def executive_summary(d: Decision, metrics: dict, title: str = "Business brief") -> Summary:
    m = metrics
    rows = [r for r in (m.get("period_table") or []) if r.get("period_status", "complete") == "complete"]
    trust = trust_score(d, m)
    found = insights(d, m)
    coverage = ""
    if d.date:
        lo, hi = d.profile.col(*d.date).minimum, d.profile.col(*d.date).maximum
        coverage = f"{pd.Timestamp(lo):%d %b %Y} – {pd.Timestamp(hi):%d %b %Y}"
    if not rows:
        k = m.get("kpis") or {}
        tiles = [Tile(key, label, float(k[key]), unit) for key, label, unit, _ in TILE_SPECS if k.get(key) is not None]
        return Summary(title, "all data", "", "Validation only: no KPI per period." if not d.kpi else "KPIs over the whole period.",
                       tiles, found, trust, coverage)
    last, prev = rows[-1], (rows[-2] if len(rows) >= 2 else None)
    tiles = []
    for key, label, unit, good_dir in TILE_SPECS:
        if last.get(key) is None:
            continue
        delta = None
        if prev and prev.get(key) is not None and unit == "pct":
            delta = last[key] - prev[key]                    # percentage points
        elif prev and prev.get(key) not in (None, 0):
            delta = 100 * (last[key] - prev[key]) / prev[key]
        good = None if delta is None or abs(delta) < 0.05 else (delta > 0) == (good_dir == "up")
        tiles.append(Tile(key, label, float(last[key]), unit, delta, good))
    main = m.get("main_measure", "revenue")
    q, yoy, split = m.get("qoq_last_complete"), m.get("yoy"), m.get("driver_split")
    parts = [f"Revenue reached {_money(last.get(main, 0))} in {last['period']}" if main == "revenue" else
             f"{last.get(main, 0):,.0f} orders in {last['period']}"]
    if q:
        parts.append(f"{q['pct']:+.1f}% on {q['from']}")
    if yoy:
        parts.append(f"{yoy['pct']:+.1f}% year over year")
    headline = parts[0] + (" (" + ", ".join(parts[1:]) + ")" if len(parts) > 1 else "") + "."
    if split and split.get("delta"):
        shares = effect_shares(split)
        if shares["volume"] >= shares["order_value"]:
            driver = "more orders" if split["volume"] > 0 else "fewer orders"
        else:
            driver = "bigger orders" if split["order_value"] > 0 else "smaller orders"
        headline += f" Most of the movement came from {driver}."
    return Summary(title, last["period"], prev["period"] if prev else "", headline, tiles, found, trust, coverage)


# ═════════════════════════════════════════════════════════════════════════
# Data dictionary
# ═════════════════════════════════════════════════════════════════════════
def data_dictionary(d: Decision) -> pd.DataFrame:
    p, s = d.profile, d.schema
    fks = {(r.child_table, r.child_col): r for r in s.relationships}
    uses: dict[tuple[str, str], list[str]] = {}

    def use(t: str | None, c: str | None, what: str) -> None:
        if t and c:
            uses.setdefault((t, c), []).append(what)

    if d.kpi:
        rev = d.revenue
        for c, what in ((rev.price, "revenue: price"), (rev.quantity, "revenue: quantity"), (rev.discount, "revenue: discount"),
                        (rev.amount, "revenue: amount")):
            use(rev.price_rel.parent_table if c == rev.price and rev.price_rel else rev.table, c, what)
        use(d.fact, d.order_key, "order key (AOV, order counts)")
        if d.customer:
            use(*d.customer, "customer key (repeat rate, RFM, cohorts)")
        if d.date:
            use(*d.date, f"analysis date ({d.period}s)")
        if d.lead:
            use(d.lead[0], d.lead[2], d.lead_name)
            use(d.lead[0], d.required, "late flag (promised date)")
        if d.rating:
            use(d.rating.table, d.rating.name, "rating_score")
        for x in d.dimensions:
            use(x.table, x.column, f"dimension `{x.alias}`")
    rows = []
    for t in p.tables.values():
        for c in t.columns:
            key = []
            if c.name in t.primary_key:
                key.append("PK" if t.key_status in ("single", "composite") else "PK (has duplicates)")
            r = fks.get((t.name, c.name))
            if r:
                key.append(("FK → " if r.accepted else "FK? → ") + f"{r.parent_table}.{r.parent_col}")
            rows.append({
                "table": t.name, "column": c.name, "original name": c.original, "type": c.logical_type,
                "role": c.role + (f" ({c.kind})" if c.kind else ""), "key": ", ".join(key),
                "filled %": round(100 * (1 - c.null_frac), 1), "distinct values": c.n_unique,
                "used for": "; ".join(uses.get((t.name, c.name), [])),
            })
    return pd.DataFrame(rows)


def data_dictionary_markdown(d: Decision) -> str:
    df = data_dictionary(d)
    out = ["# Data dictionary", ""]
    for table, g in df.groupby("table", sort=False):
        t = d.profile[table]
        out += [f"## `{table}`", "", f"{t.n_rows:,} rows · {t.grain} · source: `{t.source}`", "",
                "| Column | Original | Type | Role | Key | Filled % | Distinct | Used for |", "|---|---|---|---|---|---|---|---|"]
        for r in g.itertuples(index=False):
            out.append(f"| `{r.column}` | {r[2]} | {r.type} | {r.role} | {r.key} | {r[6]} | {r[7]:,} | {r[8]} |")
        out.append("")
    return "\n".join(out)
