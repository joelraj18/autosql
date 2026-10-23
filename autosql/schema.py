"""Relationships between tables: which column points at which table's key, and how many rows join to one.

Three ways a foreign key is recognised, strongest first:

  name   the column is named like another table's key (``orders.customer_id`` -> ``customers.customer_id``)
         and >= 95% of its distinct values exist there.                                  -> accepted
  self   a column named like ``reports_to`` / ``manager_id`` / ``parent_id`` whose values are all keys of
         the same table (an org chart or a category tree).                               -> accepted
  value  no name match, but every value exists in another table's key AND (the two sets of values are equal OR
         the names share a stem, e.g. ``ship_via`` / ``shippers``).                       -> only *suggested*

Matching values alone is weak evidence: the codes {1, 2, 3} are also valid employee, category and product IDs.
So a value-only match is offered for the user to confirm, never used silently.
"""
from __future__ import annotations

import html
import re
from collections import deque
from dataclasses import dataclass, replace

import pandas as pd

from .loading import Catalog
from .profiling import CatalogProfile, ColumnProfile, singular

NAME_CONTAINMENT = 0.95       # a name-matched FK must find >= 95% of its distinct values in the parent key
VALUE_CONTAINMENT = 1.0       # a value-only match must find all of them
MIN_STEM = 4                  # shared prefix needed between a column and a table name ("ship" in ship_via / shippers)
SELF_NAME = re.compile(r"^(reports_to|manager(_id)?|parent(_id)?|parent_\w+_id|supervisor(_id)?|boss(_id)?|managed_by)$", re.I)


@dataclass(frozen=True)
class Relationship:
    child_table: str
    child_col: str
    parent_table: str
    parent_col: str
    kind: str                     # name | self | value
    containment: float            # share of distinct child values found in the parent key
    child_distinct: int
    orphan_values: int            # distinct child values with no parent
    orphan_rows: int              # child rows whose value has no parent
    child_nulls: int
    max_children: int             # most child rows for one parent
    avg_children: float           # child rows per parent (parents without children count as 0)
    childless_parents: int        # parents that no child row points at
    type_mismatch: bool = False
    accepted: bool = True
    reason: str = ""

    @property
    def key(self) -> str:
        return f"{self.child_table}.{self.child_col} → {self.parent_table}.{self.parent_col}"

    @property
    def cardinality(self) -> str:
        return "1:1" if self.max_children <= 1 else "1:N"

    def describe(self) -> str:
        return f"`{self.child_table}.{self.child_col}` → `{self.parent_table}.{self.parent_col}`"


@dataclass(frozen=True)
class FanOut:
    """A number stored once per parent row that would be repeated (and over-counted) after joining the child table."""
    parent_table: str
    column: str
    child_table: str
    via: str                      # child column used for the join
    total: float                  # SUM(column) on the parent table
    joined_total: float           # SUM(column) after joining the child rows

    @property
    def factor(self) -> float:
        return self.joined_total / self.total if self.total else float("nan")


@dataclass(frozen=True)
class Schema:
    relationships: tuple[Relationship, ...] = ()      # accepted + suggested
    fanouts: tuple[FanOut, ...] = ()

    @property
    def accepted(self) -> list[Relationship]:
        return [r for r in self.relationships if r.accepted]

    @property
    def suggested(self) -> list[Relationship]:
        return [r for r in self.relationships if not r.accepted]

    def with_accepted(self, keys: set[str] | list[str]) -> "Schema":
        """Accept the suggested relationships the user ticked (by ``Relationship.key``)."""
        keys = set(keys)
        return Schema(tuple(replace(r, accepted=True) if r.key in keys else r for r in self.relationships), self.fanouts)

    def parents(self, table: str) -> list[Relationship]:
        """Accepted links from ``table`` to the tables it points at (joining these never multiplies rows)."""
        return [r for r in self.accepted if r.child_table == table and r.parent_table != table]

    def children(self, table: str) -> list[Relationship]:
        return [r for r in self.accepted if r.parent_table == table and r.child_table != table]

    def paths_from(self, table: str, max_hops: int = 2) -> dict[str, list[Relationship]]:
        """Tables reachable by following foreign keys upwards (many-to-one), with the shortest path to each."""
        paths: dict[str, list[Relationship]] = {table: []}
        queue = deque([table])
        while queue:
            current = queue.popleft()
            if len(paths[current]) >= max_hops:
                continue
            for r in sorted(self.parents(current), key=lambda r: (r.parent_table, r.child_col)):
                if r.parent_table not in paths:
                    paths[r.parent_table] = paths[current] + [r]
                    queue.append(r.parent_table)
        return paths


def _as_text(s: pd.Series) -> pd.Series:
    """Compare keys as trimmed text, so that 7 (number) matches '7' (text) and 'ALFKI ' matches 'ALFKI'."""
    out = s.dropna()
    if pd.api.types.is_float_dtype(out):
        out = out[out == out.round()].astype("int64")
    return out.astype(str).str.strip()


def _stem(column: str, table: str) -> int:
    """Longest shared prefix between any word of the column name and the (singular) table name."""
    target = singular(table)
    best = 0
    for word in column.split("_"):
        n = 0
        for a, b in zip(word, target):
            if a != b:
                break
            n += 1
        best = max(best, n)
    return best


def _measure(catalog: Catalog, child: str, col: str, parent: str, pcol: str, kind: str, child_prof: ColumnProfile,
             parent_prof: ColumnProfile, accepted: bool, reason: str) -> Relationship:
    child_values = _as_text(catalog[child].df[col])
    parent_keys = set(_as_text(catalog[parent].df[pcol]))
    distinct = set(child_values)
    found = len(distinct & parent_keys)
    containment = found / len(distinct) if distinct else 0.0
    orphan_mask = ~child_values.isin(parent_keys)
    counts = child_values.value_counts()
    per_parent = counts.reindex(sorted(parent_keys), fill_value=0)
    return Relationship(
        child, col, parent, pcol, kind, containment, len(distinct), len(distinct) - found, int(orphan_mask.sum()),
        int(catalog[child].df[col].isna().sum()), int(per_parent.max()) if len(per_parent) else 0,
        float(per_parent.mean()) if len(per_parent) else 0.0, int((per_parent == 0).sum()),
        type_mismatch=child_prof.logical_type != parent_prof.logical_type, accepted=accepted, reason=reason,
    )


def infer_schema(catalog: Catalog, profile: CatalogProfile) -> Schema:
    keys = {t.name: t.primary_key[0] for t in profile.tables.values() if len(t.primary_key) == 1 and t.key_status in ("single", "intended")}
    found: list[Relationship] = []
    linked: set[tuple[str, str]] = set()

    # 1) name matches
    for child in profile.tables.values():
        for c in child.columns:
            if c.role in ("text", "constant", "datetime", "rating") or c.logical_type not in ("integer", "text"):
                continue
            for parent, pcol in keys.items():
                if parent == child.name:
                    continue
                names = {pcol, f"{singular(parent)}_{pcol}", f"{parent}_{pcol}"}
                if pcol == "id":
                    names |= {f"{singular(parent)}_id", f"{parent}_id"}
                if c.name not in names:
                    continue
                if child.primary_key == (c.name,) and len(child.primary_key) == 1:
                    # both tables are keyed by this column (1:1): the bigger table is the parent
                    if (profile[parent].n_rows, parent) < (child.n_rows, child.name):
                        continue
                rel = _measure(catalog, child.name, c.name, parent, pcol, "name", c, profile.col(parent, pcol), True, "")
                if rel.containment >= NAME_CONTAINMENT:
                    rel = replace(rel, reason=f"named like the key of `{parent}`, and {rel.containment:.0%} of its values exist there")
                else:
                    rel = replace(rel, accepted=False, reason=f"named like the key of `{parent}`, but only {rel.containment:.0%} of its "
                                  "values exist there: possibly a different code list, so it is not used until you confirm it")
                found.append(rel)
                linked.add((child.name, c.name))

    # 2) self references (org charts, category trees)
    for name, pcol in keys.items():
        table = profile[name]
        for c in table.columns:
            if SELF_NAME.match(c.name) and c.logical_type == table.col(pcol).logical_type and (name, c.name) not in linked:
                rel = _measure(catalog, name, c.name, name, pcol, "self", c, table.col(pcol), True, "")
                if rel.containment == 1.0 and rel.child_distinct:
                    found.append(replace(rel, reason=f"named like a link to another row of `{name}` (an org chart / tree), and every value is a `{pcol}`"))
                    linked.add((name, c.name))

    # 3) value-only matches -> suggestions
    for child in profile.tables.values():
        for c in child.columns:
            if (child.name, c.name) in linked or c.logical_type not in ("integer", "text") or c.n_unique < 2:
                continue
            if child.primary_key == (c.name,):
                continue
            options = []
            for parent, pcol in keys.items():
                if parent == child.name or profile.col(parent, pcol).logical_type != c.logical_type:
                    continue
                stem = _stem(c.name, parent)
                if c.role != "identifier" and stem < MIN_STEM:
                    continue
                child_set = set(_as_text(catalog[child.name].df[c.name]))
                parent_set = set(_as_text(catalog[parent].df[pcol]))
                if not child_set or not child_set <= parent_set:
                    continue
                equal = child_set == parent_set
                if not (equal or stem >= MIN_STEM):
                    continue
                options.append((equal, stem, -profile[parent].n_rows, parent, pcol))
            if options:
                equal, stem, _, parent, pcol = max(options)
                why = ("its values are exactly the keys of" if equal else "every value is a key of") + f" `{parent}`"
                why += f" and the names share '{c.name.split('_')[0][:stem]}'" if stem >= MIN_STEM else ""
                rel = _measure(catalog, child.name, c.name, parent, pcol, "value", c, profile.col(parent, pcol), False,
                               why + ". Matching values alone can be a coincidence, so tick it to use it.")
                found.append(rel)

    schema = Schema(tuple(found))
    return Schema(schema.relationships, tuple(find_fanouts(catalog, profile, schema)))


def find_fanouts(catalog: Catalog, profile: CatalogProfile, schema: Schema) -> list[FanOut]:
    """Measures of a parent table that a 1:N join would repeat, with the totals before and after the join."""
    out = []
    link_cols = {(r.child_table, r.child_col) for r in schema.relationships}
    for r in schema.accepted:
        if r.kind == "self" or r.cardinality != "1:N":
            continue
        parent = profile[r.parent_table]
        for c in parent.by_role("measure"):
            if c.kind == "price" or (parent.name, c.name) in link_cols:
                continue  # a list price is not summed; a link column is not a measure
            values = catalog[r.parent_table].df[[r.parent_col, c.name]].dropna(subset=[c.name])
            per_parent = _as_text(catalog[r.child_table].df[r.child_col]).value_counts()
            keys = _as_text(values[r.parent_col])
            weights = keys.map(per_parent).fillna(0).to_numpy()
            total = float(values[c.name].sum())
            joined = float((values[c.name].to_numpy(dtype=float) * weights).sum())
            if total and abs(joined - total) > 1e-9 * abs(total):
                out.append((c.kind, FanOut(r.parent_table, c.name, r.child_table, r.child_col, total, joined)))
    order = {"amount": 0, "charge": 1, "quantity": 2}
    return [f for _, f in sorted(out, key=lambda kf: (order.get(kf[0], 3), -kf[1].factor))]


# ── Diagrams ─────────────────────────────────────────────────────────────
def to_dot(profile: CatalogProfile, schema: Schema, max_columns: int = 8) -> str:
    """Graphviz DOT for st.graphviz_chart: one box per table (🔑 key, → foreign key), one arrow per accepted link."""
    fk_cols = {(r.child_table, r.child_col) for r in schema.accepted}
    lines = ["digraph schema {", "  rankdir=LR; bgcolor=transparent;",
             '  node [shape=plain, fontname="Helvetica", fontsize=11];', '  edge [color="#86868b", fontname="Helvetica", fontsize=9];']
    for t in profile.tables.values():
        rows = [f'<tr><td bgcolor="#0071e3" align="left"><font color="white"><b>{html.escape(t.name)}</b> '
                f'({t.n_rows:,} rows)</font></td></tr>']
        shown = [c for c in t.columns if c.name in t.primary_key or (t.name, c.name) in fk_cols]
        shown += [c for c in t.columns if c not in shown][: max(0, max_columns - len(shown))]
        for c in shown:
            mark = "🔑 " if c.name in t.primary_key else "→ " if (t.name, c.name) in fk_cols else ""
            rows.append(f'<tr><td align="left">{mark}{html.escape(c.name)}</td></tr>')
        if len(t.columns) > len(shown):
            rows.append(f'<tr><td align="left"><i>… {len(t.columns) - len(shown)} more</i></td></tr>')
        lines.append(f'  "{t.name}" [label=<<table border="0" cellborder="1" cellspacing="0" cellpadding="4">{"".join(rows)}</table>>];')
    for r in schema.accepted:
        style = ", style=dashed" if r.kind == "value" else ""
        lines.append(f'  "{r.child_table}" -> "{r.parent_table}" [label="{html.escape(r.child_col)} (N:1)"{style}];')
    lines.append("}")
    return "\n".join(lines)


def to_mermaid(profile: CatalogProfile, schema: Schema) -> str:
    """Mermaid erDiagram (renders on GitHub and in JupyterLab markdown)."""
    def entity(name: str) -> str:
        return re.sub(r"\W", "_", name).upper()

    lines = ["erDiagram"]
    for r in schema.accepted:
        one = "|o" if r.child_nulls else "||"  # a child row with an empty key has no parent
        lines.append(f"    {entity(r.parent_table)} {one}--o{{ {entity(r.child_table)} : \"{r.child_col}\"")
    for t in profile.tables.values():
        lines.append(f"    {entity(t.name)} {{")
        for c in t.columns[:12]:
            tag = " PK" if c.name in t.primary_key else " FK" if any(r.child_table == t.name and r.child_col == c.name for r in schema.accepted) else ""
            lines.append(f"        {c.logical_type} {re.sub(r'[^0-9A-Za-z_]', '_', c.name)}{tag}")
        lines.append("    }")
    return "\n".join(lines)
