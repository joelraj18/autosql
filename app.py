"""AutoSQL Explorer - Streamlit UI.

All the logic lives in the ``autosql`` package; this file only lays out the page:
upload tables → see their keys and links → choose what to measure → read/run the SQL → understand the results.
"""
import math
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from autosql import (GLOSSARY, LABELS, assumptions, build_cells, decide, infer_schema, interpret, link_roles, profile_catalog,
                     read_files, read_folder, run_cells, to_dot)
from autosql.business import (BUSINESS_QUESTIONS, KPI_DICTIONARY, TILE_SPECS, data_dictionary, data_dictionary_markdown,
                               executive_summary, kpi_dictionary_markdown)
from autosql.exports import to_notebook, to_project_zip, to_sql_script
from autosql.report import tile_value, to_business_brief

EXAMPLE = Path(__file__).resolve().parent / "examples" / "northwind"

st.set_page_config(page_title="AutoSQL Explorer", layout="wide", initial_sidebar_state="expanded")


def ui_style() -> None:
    st.markdown("""
    <style>
        html, body, [class*="css"] {
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, Helvetica, Arial, sans-serif !important;
        }
        header, footer { visibility: hidden; }
        h1 {
            font-size: 3.4rem !important; font-weight: 700 !important; letter-spacing: -0.04em !important;
            text-align: center; padding-bottom: 0 !important; margin-bottom: 0 !important;
        }
        h2, h3 { font-weight: 600 !important; letter-spacing: -0.02em !important; }
        .hero-subtitle { text-align: center; color: #86868b; font-size: 1.3rem; font-weight: 500; margin-bottom: 2.5rem; }
        .stButton > button, .stDownloadButton > button { border-radius: 980px !important; padding: 0.5rem 1.4rem !important; font-weight: 500 !important; }
        [data-testid="stFileUploaderDropzone"] { border-radius: 20px !important; padding: 2rem !important; }
        .stCodeBlock { border-radius: 16px !important; }
    </style>
    """, unsafe_allow_html=True)


# ── Cached steps: only recomputed when the files (or the choices) change ─
@st.cache_data(show_spinner="Reading your files…", max_entries=3)
def load_catalog(key: tuple, _files: list[tuple[str, bytes]] | None):
    return read_folder(EXAMPLE, "*.csv") if _files is None else read_files(_files)


@st.cache_data(show_spinner="Profiling tables and finding keys…", max_entries=3)
def get_profile_and_schema(key: tuple, _catalog):
    profile = profile_catalog(_catalog)
    schema = infer_schema(_catalog, profile)
    return link_roles(profile, schema), schema


@st.cache_data(show_spinner="Deciding the analysis…", max_entries=10)
def get_decision(key: tuple, mode: str, picks: tuple, ticked: tuple, _catalog, _profile, _schema):
    # (files, mode, picks, ticked links) fully determine the decision, so the objects can stay out of the cache key
    return decide(_catalog, _profile, _schema.with_accepted(ticked), mode, dict(picks) if picks else None)


ui_style()
st.markdown("<h1>AutoSQL Explorer.</h1>", unsafe_allow_html=True)
st.markdown("<p class='hero-subtitle'>Upload your tables. Watch every query get written, run and explained.</p>", unsafe_allow_html=True)

with st.sidebar:
    level = st.radio("Explanation level", ["Beginner", "Expert"], horizontal=True,
                     help="Beginner shows the reasoning behind every step. Expert shows just the SQL and results.")
    beginner = level == "Beginner"
    dialect = st.radio("SQL dialect", ["mysql", "postgres"], format_func=LABELS.get, horizontal=True,
                       help="The SQL is shown (and downloaded) in this dialect. Inside the app it runs on DuckDB, whose version is "
                            "under each cell. Switching needs no re-run: the results are the same.")

files = st.file_uploader("Upload CSV, Excel or ZIP files: one table per file (or per Excel sheet)", type=["csv", "tsv", "txt", "xlsx", "xls", "zip"],
                         accept_multiple_files=True, label_visibility="collapsed")
if not files:
    use_example = st.session_state.get("example", False)
    c1, c2 = st.columns([1, 3])
    if c1.button("🧭 Try the Northwind example", type="primary" if not use_example else "secondary"):
        st.session_state["example"] = use_example = True
    c2.caption("Northwind: Microsoft's 8-table sample shop (830 orders, 2,155 order lines). MIT licence; see examples/northwind.")
    if not use_example:
        st.markdown("#### How it works")
        a, b, c = st.columns(3)
        a.markdown("**1. Learn the schema**  \nEvery table's key and grain, every foreign key and how many rows join to one.")
        b.markdown("**2. Validate, then decide**  \nDuplicates, NULLs, orphans and partial periods are checked before any KPI; "
                   "a rule engine picks the fact table, revenue formula and period, and writes down *why*.")
        c.markdown("**3. Write, run & explain**  \nSQL cells in MySQL 8 or PostgreSQL run live, and every result is translated "
                   "into plain English.")
        st.stop()
    key, payload, source = ("northwind-example",), None, "the Northwind example (examples/northwind)"
else:
    st.session_state["example"] = False
    key = tuple(sorted((f.name, f.file_id) for f in files))
    payload = [(f.name, f.getvalue()) for f in files]
    source = ", ".join(f.name for f in files)

catalog = load_catalog(key, payload)
for name, why in catalog.skipped:
    st.warning(f"**{name}** {why}.")
if not catalog.tables:
    st.error("None of the files could be read as a table. Upload CSV or Excel files with a header row and at least one row of data.")
    st.stop()
profile, schema = get_profile_and_schema(key, catalog)

# ── Step 1: the tables, their keys and how they connect ─────────────────
st.subheader("1. Your tables")
cols = st.columns(4)
cols[0].metric("Tables", len(profile.tables))
cols[1].metric("Rows", f"{profile.total_rows:,}")
cols[2].metric("Foreign keys found", len(schema.accepted))
cols[3].metric("Links to confirm", len(schema.suggested))
tabs = st.tabs([f"{t.name} ({t.n_rows:,})" for t in profile.tables.values()])
for tab, t in zip(tabs, profile.tables.values()):
    with tab:
        st.markdown(f"**Grain:** {t.grain}. " + (f"*{t.key_reason}.*" if t.key_reason else "") + f"  \nSource: `{t.source}`")
        st.dataframe(catalog[t.name].df.head(10), width="stretch")
        with st.expander("Column profile: what role does each column play?", expanded=beginner):
            if beginner:
                st.caption("A column's *type* is how it is stored; its *role* is how SQL should use it: join on an identifier, add up a "
                           "measure, group by a dimension. 🔑 marks the primary key.")
            st.dataframe(t.to_frame(), width="stretch", hide_index=True)

# links the user confirmed (read before drawing, so the diagram reflects the click that triggered this rerun)
ticked: tuple = tuple(r.key for r in schema.suggested if st.session_state.get(f"link-{key}-{r.key}"))
if len(profile.tables) > 1:
    left, right = st.columns([3, 2])
    with left:
        st.markdown("**How the tables connect**")
        st.graphviz_chart(to_dot(profile, schema.with_accepted(ticked)), width="stretch")
    with right:
        rows = [{"link": r.key, "found by": {"name": "name + values", "self": "self-reference", "value": "values only"}[r.kind],
                 "values found": f"{r.containment:.0%}", "join": r.cardinality, "orphan rows": r.orphan_rows,
                 "used": "✅" if r.accepted else "❔"} for r in schema.relationships]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        if schema.suggested:
            st.markdown("**Confirm these links?** They are only suggested: the values match, but the names don't say so.")
            for r in schema.suggested:
                st.checkbox(r.key, key=f"link-{key}-{r.key}", help=r.reason)
        elif not rows:
            st.info("No relationships found between these tables: each one is analysed on its own.")

# ── Step 2: what to measure ───────────────────────────────────────────────
st.subheader("2. What should we measure?")
MODES = {"auto": "🤖 Let the engine decide", "manual": "🎯 I'll choose", "validate": "🔍 Just validate the data"}
mode = st.radio("How should we choose what to measure?", list(MODES), format_func=MODES.get, horizontal=True, key=f"mode-{key}",
                help="The engine finds the sales (fact) table, the revenue formula and the date. Choose them yourself, or only "
                     "check data quality.")
picks: tuple = ()
if mode == "manual":
    auto = get_decision(key, "auto", (), ticked, catalog, profile, schema.with_accepted(ticked))
    accepted = schema.with_accepted(ticked)
    names = list(profile.tables)
    c1, c2, c3 = st.columns(3)
    fact = c1.selectbox("Fact table (one row per sale)", names, index=names.index(auto.fact) if auto.fact in names else 0, key=f"fact-{key}")
    ft = profile[fact]
    numeric = [c.name for c in ft.columns if c.numeric]
    kinds = {"price_qty": "price × quantity", "amount": "an amount column", "count": "count rows"}
    default_kind = auto.revenue.kind if auto.revenue and auto.fact == fact else ("price_qty" if len(numeric) >= 2 else "count")
    kind = c2.radio("Revenue is…", list(kinds), format_func=kinds.get, index=list(kinds).index(default_kind), key=f"kind-{key}")
    chosen_picks = {"fact": fact, "measure": kind}

    def pick(label, options, default, slot, field, optional=False):
        opts = (["(none)"] if optional else []) + options
        index = opts.index(default) if default in opts else 0
        value = slot.selectbox(label, opts, index=index, key=f"{field}-{key}-{fact}")
        if value != "(none)":
            chosen_picks[field] = value

    same = auto.fact == fact and auto.revenue is not None
    if kind == "price_qty" and numeric:
        pick("Price", numeric, auto.revenue.price if same else None, c3, "price")
        pick("Quantity", numeric, auto.revenue.quantity if same else None, c3, "quantity")
        pick("Discount (optional)", numeric, auto.revenue.discount if same else None, c3, "discount", optional=True)
    elif kind == "amount" and numeric:
        pick("Amount", numeric, auto.revenue.amount if same else None, c3, "amount")
    order_tables = [fact] + [r.parent_table for r in accepted.parents(fact)]
    dates = [f"{t}.{c.name}" for t in order_tables for c in profile[t].by_role("datetime")]
    d1, d2, d3 = st.columns(3)
    pick("Date", dates, ".".join(auto.date) if auto.date else None, d1, "date", optional=True)
    pick("Order key", [c.name for c in ft.columns], auto.order_key if auto.fact == fact else None, d2, "order_key", optional=True)
    customers = [f"{t}.{c.name}" for t in order_tables for c in profile[t].columns if c.role == "identifier" and (c.name,) != profile[t].primary_key]
    pick("Customer", customers, ".".join(auto.customer) if auto.customer else None, d3, "customer", optional=True)
    reach = accepted.paths_from(fact, 2)
    dims = sorted({f"{t}.{c.name}" for t in reach for c in profile[t].by_role("dimension")} |
                  {f"{t}.{c.name}" for r in accepted.parents(fact) for t in accepted.paths_from(r.parent_table, 1) for c in profile[t].by_role("dimension")})
    default_dims = [f"{x.table}.{x.column}" for x in auto.dimensions if f"{x.table}.{x.column}" in dims]
    chosen_dims = st.multiselect("Slice by (up to 3)", dims, default=default_dims, max_selections=3, key=f"dims-{key}-{fact}")
    if chosen_dims:
        chosen_picks["dimensions"] = tuple(chosen_dims)
    if auto.row_filters and auto.fact == fact:
        rules = "; ".join(f.describe() for f in auto.row_filters)
        if not st.checkbox("Leave out rows that are not sales", value=True, key=f"rules-{key}-{fact}",
                           help=f"Cancelled, refunded or test orders and amounts in a second currency: {rules}."):
            chosen_picks["row_filters"] = False
    picks = tuple(sorted(chosen_picks.items()))

decision = get_decision(key, mode, picks, ticked, catalog, profile, schema)
if beginner:
    focus = next((s for s in decision.steps if s.key.startswith("focus_")), None)
    if focus:
        with st.container(border=True):
            st.markdown(focus.markdown())

with st.sidebar:
    st.subheader("🧠 Decision trace")
    for s in decision.steps:
        st.markdown(f"- {s.short()}")
    with st.expander("📚 Glossary"):
        for term, meaning in GLOSSARY.items():
            st.markdown(f"**{term}** - {meaning}")
    with st.expander("📐 KPI dictionary"):
        for k in KPI_DICTIONARY:
            better = {"up": "higher is better", "down": "lower is better"}.get(k.direction, "context, not a target")
            st.markdown(f"**{k.name}**: {k.definition} *({better})*  \n`{k.formula}`")

if decision.halted:
    st.error(decision.steps[-1].markdown())
    st.stop()

if decision.kpi:
    from autosql.codegen import Builder
    summary = {
        "Fact table": f"`{decision.fact}`",
        "Grain": profile[decision.fact].grain.replace("one row per ", "per "),
        "Revenue": Builder(decision).revenue_formula(),
        "Period": f"{decision.period} ({len(decision.complete_periods)} complete)" if decision.period else "none",
    }
else:
    summary = {"Analysis": "data validation", "Tables": str(len(profile.tables)), "Foreign keys": str(len(decision.schema.accepted)),
               "Rows": f"{profile.total_rows:,}"}
for col, (label, value) in zip(st.columns([0.9, 1.3, 2, 1]), summary.items()):
    col.caption(label)
    col.markdown(f"**{value}**")

with st.expander("📖 Data dictionary: every column, its role, and what the analysis uses it for"):
    dictionary = data_dictionary(decision)
    st.dataframe(dictionary, hide_index=True, width="stretch")
    st.download_button("⬇ Data dictionary (.md)", data_dictionary_markdown(decision), file_name="data_dictionary.md", mime="text/markdown")

if beginner:
    with st.expander("How the engine reached this decision", expanded=False):
        for s in decision.steps:
            with st.container(border=True):
                st.markdown(s.markdown())

# ── Step 3: the SQL ───────────────────────────────────────────────────────
cells = build_cells(decision)
run_key = (key, mode, picks, ticked)
run = st.session_state.get("run")
if run and run["key"] != run_key:
    run = None

st.subheader("3. The generated SQL")
if beginner:
    st.caption("Each cell does one job and explains itself in six parts: 🔍 what we found before, 💡 why it matters, 🎯 what we're going "
               f"to do, 🛠 what the SQL does, the SQL itself in {LABELS[dialect]} (then its results and charts), and, after you press "
               "**Run**, 📌 what we found.")
if decision.skipped:
    with st.expander(f"{len(decision.skipped)} step(s) skipped, and why"):
        for cell_id, why in decision.skipped.items():
            st.markdown(f"- **{cell_id}**: {why}")

b1, b2, b3, b4 = st.columns([1, 1.2, 1.4, 1.3])
if b1.button("▶ Run all SQL", type="primary"):
    with st.status("Running the SQL…", expanded=False) as status:
        results, metrics = run_cells(cells, catalog,
                                     on_cell=lambda i, c: status.update(label=f"Running cell {i + 1}/{len(cells)}: {c.title}…"))
        failed = any(r.error for r in results)
        status.update(label="Finished with an error - see below." if failed else "All SQL ran.", state="error" if failed else "complete")
    run = st.session_state["run"] = {"key": run_key, "results": results, "metrics": metrics}
stem = "northwind" if payload is None else Path(files[0].name).stem
brief_title = "Northwind Traders" if payload is None else stem.replace("_", " ").title()
b2.download_button(f"⬇ {LABELS[dialect]} script (.sql)", to_sql_script(cells, decision, dialect, source),
                   file_name=f"{stem}_autosql_{dialect}.sql", mime="text/x-sql")
b3.download_button("⬇ Project: SQL files + data (.zip)",
                   to_project_zip(cells, decision, catalog, dialect, source, run["metrics"] if run else None, brief_title),
                   file_name=f"{stem}_autosql_{dialect}.zip", mime="application/zip",
                   help="CREATE TABLE, load, validation, analysis and performance scripts, the cleaned CSVs, a notebook, the data and KPI "
                        "dictionaries and (after a run) the business brief.")
b4.download_button("⬇ Notebook (.ipynb)", to_notebook(cells, decision, catalog, source), file_name=f"{stem}_autosql.ipynb",
                   mime="application/x-ipynb+json", help="Runs on DuckDB; expects the cleaned CSVs in data/ (they are in the project ZIP).")

section = None
for i, cell in enumerate(cells):
    if cell.section != section:
        section = cell.section
        st.markdown(f"### {section}")
    st.markdown(f"#### {i + 1}. {cell.title}")
    question, asked_by = BUSINESS_QUESTIONS.get(cell.id, ("", ""))
    if question:
        st.caption(f"💼 **Business question:** {question} · asked by {asked_by}")
    if beginner:
        with st.container(border=True):
            st.markdown(cell.story.markdown())
        st.markdown(f"🛠 **What this SQL does:** {cell.story.code}")
    st.code(cell.code(dialect), language="sql")
    with st.expander("🔧 Technical notes: techniques, MySQL vs PostgreSQL, run time"):
        st.markdown("**Techniques:** " + " · ".join(f"`{t}`" for t in cell.techniques()))
        diff = cell.dialect_diff()
        if diff:
            st.markdown(f"**{len(diff)} line(s) differ between MySQL 8 and PostgreSQL:**")
            st.dataframe(pd.DataFrame(diff[:15], columns=["MySQL 8", "PostgreSQL"]), hide_index=True, width="stretch")
        else:
            st.markdown("**Identical in MySQL 8 and PostgreSQL**: only portable SQL is used.")
        if run and not run["results"][i].skipped:
            st.dataframe(pd.DataFrame([{"statement": sr.title, "ran on": {"query": "DuckDB", "scratch": "empty DuckDB (DDL check)",
                                                                          "show": "not run"}[sr.run],
                                        "rows": sr.rows, "seconds": round(sr.seconds, 3)} for sr in run["results"][i].statements]),
                         hide_index=True, width="stretch")
    with st.expander("What ran here (DuckDB) · how the 📌 findings are computed"):
        st.code("\n\n".join(f"-- {s.title}\n{s.sql['duckdb']};" for s in cell.statements if s.run != "show"), language="sql")
        st.code(cell.check, language="python")
    if not run:
        if beginner and i == 0:
            st.caption("Results and 📌 *what we found* appear under each cell once you press **Run all SQL**.")
        continue
    res = run["results"][i]
    if res.skipped:
        st.caption("⏭ Skipped because an earlier cell failed.")
        continue
    for sr in res.statements:
        if sr.run == "show":
            st.caption(f"*{sr.title}*: not run here; this one is for your {LABELS[dialect]} database.")
        elif sr.frame is not None and len(sr.frame.columns):
            label = f"**{sr.title}**: {sr.rows:,} row(s)" + (" (first 200 shown)" if sr.rows > 200 else "")
            label += " · ran on an empty database (syntax check)" if sr.run == "scratch" else ""
            st.markdown(label)
            st.dataframe(sr.frame.head(200), width="stretch", hide_index=True)
        elif sr.run == "scratch":
            st.caption(f"✅ {sr.title}: created on an empty database.")
    for img in res.figures:
        st.image(img)
    if res.notes:
        with st.container(border=True):
            st.markdown("📌 **What we found after running this:**")
            for line in res.notes:
                st.markdown(f"- {line}")
    if res.error:
        st.error(f"This cell failed: {res.error}")
        if beginner:
            st.caption("The message names the statement and the problem. Common causes: a column with mixed types, or a date that didn't parse.")
    st.caption(f"⏱ {res.seconds:.2f}s")

# ── Step 4: what it all means ─────────────────────────────────────────────
if run:
    metrics = run["metrics"]
    summary = executive_summary(decision, metrics, brief_title)
    GOOD_DIRECTION = {key: direction for key, _, _, direction in TILE_SPECS}
    st.subheader("4. What the results mean")
    st.markdown(f"##### {summary.headline}")
    if summary.tiles:
        st.caption(f"Latest complete period: **{summary.period}**" + (f", compared with {summary.compared_with}." if summary.compared_with else "."))
        for row in range(0, len(summary.tiles), 4):
            for col, t in zip(st.columns(4), summary.tiles[row:row + 4]):
                delta = None if t.delta_pct is None else ((f"{t.delta_pct:+.1f} pts" if t.unit == "pct" else f"{t.delta_pct:+.1f}%")
                                                          + (f" {t.note}" if t.note else ""))
                noise = t.significant is False
                col.metric(t.label, tile_value(t) + (" days" if t.unit == "days" else ""), (delta + " · within noise") if delta and noise else delta,
                           delta_color="off" if noise else "inverse" if GOOD_DIRECTION[t.key] == "down" else "normal", border=True,
                           help=(f"95% confidence interval of the change: {t.ci[0]:+.1f} to {t.ci[1]:+.1f}"
                                 + (" pts" if t.unit == "pct" else "%") + (". It includes zero, so the change could be noise." if noise else ".")
                                 if t.ci else None))

    st.markdown("#### What to do")
    names = {1: "🔴 Act now", 2: "🟠 Plan", 3: "🟢 Monitor"}
    for ins in summary.insights:
        with st.container(border=True):
            st.markdown(f"**{names[ins.priority]} · {ins.headline}**  \n{ins.evidence}. {ins.implication}  \n**Next step:** {ins.action}")
            st.caption(f"{ins.area} · confidence: {ins.confidence} · from SQL step `{ins.source}`")

    t = summary.trust
    st.markdown(f"#### Can we trust these numbers? Trust score **{math.floor(t.score)}/100: {t.grade}**")
    for dim in t.dimensions:
        st.progress(min(1.0, dim.score / 100), text=f"**{dim.name}** {math.floor(dim.score)}/100: {dim.detail}")

    brief = to_business_brief(decision, metrics, title=brief_title, source=source)
    c1, c2, c3 = st.columns(3)
    c1.download_button("⬇ Business brief (.html)", brief, file_name=f"{stem}_business_brief.html", mime="text/html", type="primary",
                       help="One self-contained page with the KPIs, recommendations, charts and data trust. Opens offline in any browser.")
    c2.download_button("⬇ KPI dictionary (.md)", "# KPI dictionary\n\n" + kpi_dictionary_markdown(), file_name="kpi_dictionary.md",
                       mime="text/markdown")
    c3.download_button("⬇ Data dictionary (.md)", data_dictionary_markdown(decision), file_name="data_dictionary.md", mime="text/markdown",
                       key="dd-step4")
    with st.expander("📄 Preview the business brief", expanded=False):
        components.html(brief, height=900, scrolling=True)

    with st.expander("🔬 Technical reading of every result", expanded=beginner):
        for line in interpret(metrics):
            st.markdown(f"- {line}")
    with st.expander("Assumptions behind these numbers", expanded=beginner):
        for line in assumptions(decision):
            st.markdown(f"- {line}")
