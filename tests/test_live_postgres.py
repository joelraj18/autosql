"""Run the exported PostgreSQL project on a real PostgreSQL server and compare every result with DuckDB.

Uses $AUTOSQL_PG_URL when it is set (a server where the user may create databases: each case gets its own); otherwise starts a
throw-away server from the local PostgreSQL install (``initdb`` + ``pg_ctl``; as root, through ``runuser -u postgres``).
Skipped when neither is available, or failed when AUTOSQL_REQUIRE_LIVE=1 (CI).
"""
import glob
import io
import os
import shutil
import socket
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import pytest

from autosql import run_cells
from autosql.exports import to_project_zip
from conftest import live_unavailable
from test_codegen_runs import CASES, build

PG_BIN = next(iter(sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)), None)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def psql():
    url = os.environ.get("AUTOSQL_PG_URL")
    if url:
        if not shutil.which("psql"):
            live_unavailable("AUTOSQL_PG_URL is set but psql is not installed")
        parts = urlsplit(url)
        # the database named in the URL is only used to connect: every case runs in its own database on that server
        yield lambda db, *args, **kw: subprocess.run(["psql", urlunsplit(parts._replace(path="/" + db)), *args],
                                                     capture_output=True, text=True, **kw)
        return
    if not PG_BIN or not shutil.which("psql"):
        live_unavailable("no PostgreSQL server: set AUTOSQL_PG_URL or install PostgreSQL")
    as_postgres = ["runuser", "-u", "postgres", "--"] if os.geteuid() == 0 else []
    if as_postgres and not shutil.which("runuser"):
        live_unavailable("running as root without runuser: initdb refuses to run as root")
    root = Path(tempfile.mkdtemp(prefix="autosql-pg-"))
    if as_postgres:
        shutil.chown(root, "postgres")
    port = _free_port()
    subprocess.run(as_postgres + [f"{PG_BIN}/initdb", "-D", str(root / "data"), "-A", "trust", "-U", "postgres"],
                   check=True, capture_output=True)
    subprocess.run(as_postgres + [f"{PG_BIN}/pg_ctl", "-D", str(root / "data"), "-l", str(root / "log"), "-w",
                                  "-o", f"-k {root} -p {port} -c listen_addresses=''", "start"], check=True, capture_output=True)
    base = ["psql", "-h", str(root), "-p", str(port), "-U", "postgres"]
    try:
        yield lambda db, *args, **kw: subprocess.run(base + ["-d", db, *args], capture_output=True, text=True, **kw)
    finally:
        subprocess.run(as_postgres + [f"{PG_BIN}/pg_ctl", "-D", str(root / "data"), "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


def _same(pg: pd.DataFrame, duck: pd.DataFrame) -> list[str]:
    problems = []
    if len(pg) != len(duck):
        return [f"{len(pg)} rows in PostgreSQL vs {len(duck)} in DuckDB"]
    for i, col in enumerate(duck.columns):
        a, b = pg.iloc[:, i], duck.iloc[:, i]
        an, bn = pd.to_numeric(a, errors="coerce"), pd.to_numeric(b, errors="coerce")
        if an.notna().sum() == a.notna().sum() and a.notna().sum():
            diff = (an.fillna(0) - bn.astype(float).fillna(0)).abs()
            if (diff > 0.011 + 1e-6 * bn.astype(float).abs().fillna(0)).any():
                problems.append(f"{col}: {an.tolist()[:5]} vs {bn.tolist()[:5]}")
        elif a.astype(str).str[:10].tolist() != b.astype(str).str[:10].replace({"nan": "", "None": "", "NaT": ""}).tolist() and \
                a.fillna("").astype(str).str[:10].tolist() != b.fillna("").astype(str).str[:10].tolist():
            problems.append(f"{col}: {a.tolist()[:3]} vs {b.tolist()[:3]}")
    return problems


@pytest.mark.parametrize("case", [c for c in CASES if not c.endswith("_validate")] + ["shop_validate"])
def test_exported_project_runs_on_postgres_and_matches_duckdb(case, psql, northwind_catalog, tmp_path):
    catalog, d, cells = build(case, northwind_catalog)
    db = f"autosql_{case}"
    psql("postgres", "-c", f"DROP DATABASE IF EXISTS {db}")
    assert psql("postgres", "-c", f"CREATE DATABASE {db}").returncode == 0
    zipfile.ZipFile(io.BytesIO(to_project_zip(cells, d, catalog, "postgres", case))).extractall(tmp_path)
    for f in ("01_schema.sql", "02_load.sql", "03_validate.sql", "04_analysis.sql", "05_performance.sql"):
        out = psql(db, "-v", "ON_ERROR_STOP=1", "-q", "-f", f, cwd=tmp_path)
        assert out.returncode == 0, f"{f}: {out.stderr[-1500:]}"

    results, _ = run_cells(cells, catalog)
    duck = {(r.cell_id, s.label): s.frame for r in results for s in r.statements if s.frame is not None}
    mismatches = []
    for cell in cells:
        for st in cell.statements:
            if st.run != "query" or st.label in ("plan", "preview") or not len(duck.get((cell.id, st.label), [])):
                continue
            if st.sql["postgres"].lstrip().upper().startswith(("CREATE", "-- GRAIN")):
                continue
            out = psql(db, "--csv", "-c", st.sql["postgres"])
            assert out.returncode == 0, f"{cell.id}/{st.label}: {out.stderr[-800:]}"
            pg = pd.read_csv(io.StringIO(out.stdout), keep_default_na=False, na_values=[""])
            problems = _same(pg, duck[(cell.id, st.label)].reset_index(drop=True))
            if problems:
                mismatches.append((cell.id, st.label, problems[:3]))
    assert not mismatches, mismatches
