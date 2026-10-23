"""AutoML Explorer - Streamlit UI.

All the logic lives in the ``automl`` package; this file only lays out the page:
upload → look at the data → see how the engine decided → read/run the cells →
understand the results.
"""
import pandas as pd
import streamlit as st

from automl import GLOSSARY, build_cells, decide, interpret, profile_dataset, resolve_target, run_cells, to_notebook

st.set_page_config(page_title="AutoML Explorer", layout="wide", initial_sidebar_state="expanded")


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


# ── Cached steps: only recomputed when the file (or target) changes ──────
@st.cache_data(show_spinner="Reading your file…", max_entries=3)
def load_data(file_id: str, name: str, _raw: bytes) -> pd.DataFrame:
    import io
    buf = io.BytesIO(_raw)
    df = pd.read_csv(buf, low_memory=False) if name.lower().endswith(".csv") else pd.read_excel(buf)
    # Generated code refers to columns by name, so names must be text and unique ("a" and "a " would collide).
    names, seen = [], {}
    for c in (str(c).strip() or "unnamed" for c in df.columns):
        seen[c] = seen.get(c, 0) + 1
        names.append(c if seen[c] == 1 else f"{c}_{seen[c]}")
    df.columns = names
    return df


@st.cache_data(show_spinner="Profiling columns…", max_entries=3)
def get_profile(file_id: str, _df: pd.DataFrame):
    return profile_dataset(_df)


@st.cache_data(show_spinner="Deciding the pipeline…", max_entries=10)
def get_decision(file_id: str, mode: str, target: str | None, _df: pd.DataFrame, _profile, _target_step):
    # (file, mode, target) fully determines the target step, so it is safe to leave that out of the cache key
    return decide(_df, _profile, target, _target_step)


ui_style()
st.markdown("<h1>AutoML Explorer.</h1>", unsafe_allow_html=True)
st.markdown("<p class='hero-subtitle'>Upload a dataset. Watch every decision get explained.</p>", unsafe_allow_html=True)

with st.sidebar:
    mode = st.radio("Explanation level", ["Beginner", "Expert"], horizontal=True,
                    help="Beginner shows the reasoning behind every step. Expert shows just the code and results.")
    beginner = mode == "Beginner"

file = st.file_uploader("Upload CSV or Excel", type=["csv", "xlsx", "xls"], label_visibility="collapsed")

if not file:
    st.markdown("#### How it works")
    c1, c2, c3 = st.columns(3)
    c1.markdown("**1. Profile**  \nWe inspect every column: is it a number, a category, a date, an ID?")
    c2.markdown("**2. Decide**  \nA rule-based engine picks the task and model - and writes down *why*.")
    c3.markdown("**3. Run & explain**  \nNotebook-style code cells run live, and the results are translated into plain English.")
    st.stop()

try:
    df = load_data(file.file_id, file.name, file.getvalue())
except Exception as error:  # empty, corrupt or password-protected files
    st.error(f"We couldn't read **{file.name}** ({type(error).__name__}: {error}). Check that it is a CSV or Excel file "
             "with a header row and at least one row of data, then upload it again.")
    st.stop()
if df.empty:
    st.error(f"**{file.name}** has column names but no rows of data. Upload a file with at least 30 rows.")
    st.stop()
profile = get_profile(file.file_id, df)

# ── Step 1: look at the data ──────────────────────────────────────────────
st.subheader("1. Your data")
st.dataframe(df.head(10), width="stretch")
with st.expander("Column profile - what role does each column play?", expanded=beginner):
    if beginner:
        st.caption("A column's *type* is how it is stored; its *role* is how the model should use it. "
                   "For example, a column of whole numbers can be a real quantity (age) or just a row number (an ID).")
    st.dataframe(profile.to_frame(), width="stretch", hide_index=True)

# ── Step 2: choose a target and let the engine decide ────────────────────
st.subheader("2. What should we predict?")
MODES = {
    "auto": "🤖 Let the engine decide",
    "manual": "🎯 I'll pick the target",
    "none": "🔍 No target: just find groups",
}
mode = st.radio(
    "How should we choose what to predict?", list(MODES), format_func=MODES.get, horizontal=True,
    key=f"mode-{file.file_id}",  # a new file starts again from "let the engine decide"
    help="The target is the column you want a model to predict. Let the engine look for one, pick it yourself, "
         "or skip prediction and look for natural groups instead.",
)
chosen = None
if mode == "manual":
    columns = list(df.columns)
    suggested = profile.suggested_target
    chosen = st.selectbox("Target column", columns, index=columns.index(suggested) if suggested else 0,
                          key=f"target-{file.file_id}")
target, target_step = resolve_target(profile, mode, chosen)
if beginner:
    with st.container(border=True):
        st.markdown(target_step.markdown())
decision = get_decision(file.file_id, mode, target, df, profile, target_step)
if not decision.halted:
    outcome = f"{decision.task} ({decision.subtype})" if decision.subtype else decision.task
    st.caption(f"→ Target: **{target or 'none'}** · task: **{outcome}**")

with st.sidebar:
    st.subheader("🧠 Decision trace")
    for s in decision.steps:
        st.markdown(f"- {s.short()}")
    with st.expander("📚 Glossary"):
        for term, meaning in GLOSSARY.items():
            st.markdown(f"**{term}** - {meaning}")

if decision.halted:
    st.error(decision.steps[-1].markdown())
    st.stop()

summary = {
    "Task": decision.task.capitalize() + (f" ({decision.subtype})" if decision.subtype else ""),
    "Models compared": " · ".join(decision.candidates) if decision.candidates else decision.model,
    "Rows used": f"{min(len(df), decision.sample_rows or len(df)):,}",
    "Judged by": decision.primary_metric or "Silhouette score",
}
for col, (label, value) in zip(st.columns([1.1, 2.2, 0.7, 0.8]), summary.items()):
    col.caption(label)
    col.markdown(f"**{value}**")

if beginner:
    with st.expander("How the engine reached this decision", expanded=True):
        for s in decision.steps:
            with st.container(border=True):
                st.markdown(s.markdown())

# ── Step 3: the notebook ──────────────────────────────────────────────────
cells = build_cells(decision)
run_key = (file.file_id, target)
run = st.session_state.get("run")
if run and run["key"] != run_key:
    run = None

st.subheader("3. The generated notebook")
if beginner:
    st.caption("Each cell below does one job and explains itself in six parts: 🔍 what we found before, 💡 why it matters, "
               "🎯 what we're going to do, 🛠 what the code does, the code itself (then its output and charts), and, after you "
               "press **Run pipeline**, 📌 what we found.")

b1, b2 = st.columns([1, 4])
if b1.button("▶ Run pipeline", type="primary"):
    with st.status("Running pipeline…", expanded=False) as status:
        results, metrics = run_cells(
            cells, df, on_cell=lambda i, c: status.update(label=f"Running cell {i + 1}/{len(cells)}: {c.title}…"),
        )
        failed = any(r.error for r in results)
        status.update(label="Finished with an error - see below." if failed else "Pipeline finished.",
                      state="error" if failed else "complete")
    run = st.session_state["run"] = {"key": run_key, "results": results, "metrics": metrics}
b2.download_button(
    "⬇ Download as Jupyter notebook", to_notebook(cells, decision, file.name),
    file_name=f"{file.name.rsplit('.', 1)[0]}_automl.ipynb", mime="application/x-ipynb+json",
)

for i, cell in enumerate(cells):
    st.markdown(f"#### {i + 1}. {cell.title}")
    if beginner:
        with st.container(border=True):
            st.markdown(cell.story.markdown())
        st.markdown(f"🛠 **What this code does:** {cell.story.code}")
    st.code(cell.code, language="python")
    if not run:
        if beginner and i == 0:
            st.caption("📌 *What we found after running* appears under each cell once you press **Run pipeline**.")
        continue
    res = run["results"][i]
    if res.skipped:
        st.caption("⏭ Skipped because an earlier cell failed.")
        continue
    if res.stdout:
        st.code(res.stdout, language="text")
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
            st.caption("The message above names the problem. Common causes: a column with mixed types, or too few rows of some class.")
    st.caption(f"⏱ {res.seconds:.1f}s")

# ── Step 4: what it all means ────────────────────────────────────────────
if run:
    lines = interpret(run["metrics"], target)
    if lines:
        st.subheader("4. What the results mean")
        with st.container(border=True):
            for line in lines:
                st.markdown(f"- {line}")
