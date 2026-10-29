"""The SQL behind the business brief: the engine's own statements, shown as readable code next to what they produced.

Nothing here is written for the page: every block is the text of a ``Statement`` that ``build_cells`` generated, in both
dialects the project supports, so what a reader copies is what the figure was computed from. A step (one cell) is the unit of
the appendix; a chart, a tile row, a fact or a data-quality check points at the statements it was made from.
"""
from __future__ import annotations

import html

from .business import BUSINESS_QUESTIONS
from .codegen import Cell, Statement
from .dialect import LABELS, SHOWN

Ref = tuple[str, "str | None"]      # (step id, statement label); a label of None means every statement of the step


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def _comment_at(line: str) -> int:
    """Where a ``--`` comment starts on a line of SQL, ignoring dashes inside a string literal (-1: none)."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == "'":
            quoted = not quoted
        elif not quoted and ch == "-" and line.startswith("--", i):
            return i
    return -1


def highlight(sql: str) -> str:
    """Escaped SQL with its comments dimmed: the comments are where this project explains itself."""
    out = []
    for line in sql.split("\n"):
        at = _comment_at(line)
        out.append(esc(line) if at < 0 else f'{esc(line[:at])}<span class="c">{esc(line[at:])}</span>')
    return "\n".join(out)


def sql_text(statements: list[Statement], dialect: str) -> str:
    """The statements as a script in one dialect: a numbered title above each, a semicolon after each."""
    parts = []
    for i, st in enumerate(statements, 1):
        prefix = f"-- {i}) " if len(statements) > 1 else "-- "
        note = "  [not run here: shown for your database]" if st.run == "show" else ""
        parts.append(f"{prefix}{st.title}{note}\n{st.sql[dialect]};")
    return "\n\n".join(parts)


def code_block(statements: list[Statement]) -> str:
    """One pane per dialect; the page shows the one the reader picked (MySQL until they choose).

    The tabs and the copy button are made by the page's script, so they start hidden; without a script the static label
    names each pane and both dialects are shown.
    """
    tabs = ('<div class="dialect" role="group" aria-label="SQL dialect" hidden>'
            + "".join(f'<button type="button" data-dialect="{x}" aria-pressed="false">{esc(LABELS[x])}</button>' for x in SHOWN) + "</div>")
    panes = []
    for dialect in SHOWN:
        panes.append(f'<div class="sqlpane sql-{dialect}"><div class="sqlbar"><span class="lbl">{esc(LABELS[dialect])}</span>{tabs}'
                     f'<button type="button" class="copy" hidden>Copy</button></div>'
                     f'<pre tabindex="0"><code>{highlight(sql_text(statements, dialect))}</code></pre></div>')
    return f'<div class="sqlcode">{"".join(panes)}</div>'


class SqlLibrary:
    """The cells of one analysis, and which of their steps the page has pointed at (those go into the appendix)."""

    def __init__(self, cells: list[Cell]):
        self.cells = {c.id: c for c in cells}
        self.order = [c.id for c in cells]
        self.used: set[str] = set()

    def has(self, step: str) -> bool:
        return step in self.cells

    def statements(self, refs: list[Ref]) -> list[Statement]:
        """The statements the references name, once each, in the order of the references; unknown steps are ignored."""
        out: list[Statement] = []
        for step, label in refs:
            cell = self.cells.get(step)
            if cell is None:
                continue
            self.used.add(step)
            for st in cell.statements:
                if (label is None or st.label == label) and st not in out:
                    out.append(st)
        return out

    def details(self, refs: list[Ref], label: str = "SQL") -> str:
        """A disclosure with the full SQL of the statements; empty when none of them exists in this analysis."""
        found = self.statements(refs)
        if not found:
            return ""
        many = f" ({len(found)} queries)" if len(found) > 1 else ""
        return f'<details class="sqlv"><summary>{esc(label)}{many}</summary>{code_block(found)}</details>'

    def link(self, steps: list[str], text: str = "SQL", title: str = "") -> str:
        """A small link from a card to the full code of the steps it comes from: the appendix, or a popup when scripts run."""
        steps = [s for s in steps if s in self.cells]
        if not steps:
            return ""
        self.used.update(steps)
        return (f'<a class="sqllink" href="#sql-{esc(steps[0])}" data-sql="{esc(" ".join(steps))}" data-title="{esc(title)}" '
                f'aria-label="SQL behind {esc(title or "this card")}">{esc(text)}</a>')

    def step_body(self, cell: Cell) -> str:
        """What the appendix (and the popups) show for one step: the question it answers, then every statement."""
        question, asked_by = BUSINESS_QUESTIONS.get(cell.id, ("", ""))
        head = f'<p class="q">{esc(question)} <span class="by">Asked by {esc(asked_by)}.</span></p>' if question else ""
        return f'<div class="sqlbody">{head}{code_block(list(cell.statements))}</div>'

    def appendix(self) -> str:
        """Every step the page used, in the order the analysis runs them, each as a closed disclosure with its full code."""
        steps = []
        for step in self.order:
            if step not in self.used:
                continue
            cell = self.cells[step]
            steps.append(f'<details class="step" id="sql-{esc(step)}" data-step="{esc(step)}">'
                         f'<summary><b>{esc(cell.title)}</b> <span class="sec">{esc(cell.section)}</span></summary>'
                         f'{self.step_body(cell)}</details>')
        return "".join(steps)
