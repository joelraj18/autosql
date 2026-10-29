"""Golden snapshot: what loading, profiling and schema inference produce for every test dataset.

Speed work on the loader and the profiler must change how fast they run, never what they return. This snapshot records,
for every table and column: the type, formats and units, every count the trust score uses, a hash of all values, the
column role, the keys and grain, and every relationship. Regenerate it only for an intended change:

    python tests/test_golden.py --write
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from autosql import infer_schema, profile_catalog, read_folder  # noqa: E402
from conftest import NORTHWIND, catalog_from  # noqa: E402
from datasets import sweep_cases  # noqa: E402

GOLDEN = HERE / "golden" / "snapshot.json"


def _canonical(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA or v is pd.NaT:
        return ""
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, float):
        return repr(v)
    return str(v)


def _hash(series: pd.Series) -> str:
    text = "\n".join(_canonical(v) for v in series.astype(object).tolist())
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def snapshot(catalog) -> dict:
    profile = profile_catalog(catalog)
    schema = infer_schema(catalog, profile)
    out = {"skipped": [list(x) for x in catalog.skipped], "tables": {}, "relationships": [], "fanouts": []}
    for name, t in catalog.tables.items():
        tp = profile[name]
        out["tables"][name] = {
            "source": t.source, "column_map": t.column_map, "types": t.types, "date_formats": t.date_formats,
            "unparsed_dates": t.unparsed_dates, "ambiguous_dates": t.ambiguous_dates, "unparsed_numbers": t.unparsed_numbers,
            "units": t.units, "cleaned": t.cleaned, "placeholders": t.placeholders,
            "rows": len(t.df), "hashes": {c: _hash(t.df[c]) for c in t.df.columns},
            "primary_key": list(tp.primary_key), "key_status": tp.key_status, "grain": tp.grain,
            "duplicate_keys": tp.duplicate_keys, "extra_rows": tp.extra_rows, "exact_duplicates": tp.exact_duplicates,
            "columns": {c.name: [c.role, c.kind, c.n_null, c.n_unique, c.unit, [list(x) for x in c.scale], round(c.avg_len, 6), c.max_len,
                                 _canonical(c.minimum), _canonical(c.maximum), c.reason] for c in tp.columns},
        }
    for r in schema.relationships:
        out["relationships"].append([r.key, r.kind, r.accepted, round(r.containment, 6), r.orphan_rows, r.child_nulls, r.max_children,
                                     round(r.avg_children, 6), r.childless_parents, r.type_mismatch])
    for f in schema.fanouts:
        out["fanouts"].append([f.parent_table, f.column, f.child_table, round(f.total, 4), round(f.joined_total, 4)])
    return out


def all_snapshots() -> dict:
    from test_codegen_runs import CASES
    shots = {"northwind": snapshot(read_folder(NORTHWIND, "*.csv"))}
    for name, (frames, *_) in CASES.items():
        if frames is None or name.endswith(("_validate", "_custom")):      # your own KPIs change no table, type or key
            continue
        made = frames() if callable(frames) else {n: make() for n, make in frames.items()}
        shots[f"case:{name}"] = snapshot(catalog_from(made))
    for name, frames in sweep_cases().items():
        shots[f"sweep:{name}"] = snapshot(catalog_from(frames))
    return shots


def test_loading_and_profiling_match_the_golden_snapshot():
    expected = json.loads(GOLDEN.read_text())
    got = json.loads(json.dumps(all_snapshots(), default=str))
    assert got.keys() == expected.keys()
    for name in expected:
        assert got[name] == expected[name], f"{name} changed: regenerate only for an intended change (python tests/test_golden.py --write)"


if __name__ == "__main__" and "--write" in sys.argv:
    GOLDEN.parent.mkdir(exist_ok=True)
    GOLDEN.write_text(json.dumps(all_snapshots(), indent=1, sort_keys=True, default=str, ensure_ascii=False))
    print(f"wrote {GOLDEN.relative_to(HERE.parent)}")
