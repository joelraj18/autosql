"""Run the exported MySQL project on a real MySQL 8 server (in Docker) and compare every result with DuckDB.

Opt-in, because it needs a running container:

    docker run -d --name autosql-mysql -e MYSQL_ROOT_PASSWORD=autosql mysql:8.4 --local-infile=1
    AUTOSQL_MYSQL_CONTAINER=autosql-mysql pytest tests/test_live_mysql.py

With AUTOSQL_REQUIRE_LIVE=1 (CI), a missing container fails the run instead of skipping it.
"""
import io
import os
import shutil
import subprocess
import zipfile

import pandas as pd
import pytest

from autosql import run_cells
from autosql.exports import to_project_zip
from conftest import live_unavailable
from test_codegen_runs import CASES, build
from test_live_postgres import _same

CONTAINER = os.environ.get("AUTOSQL_MYSQL_CONTAINER")
PASSWORD = os.environ.get("AUTOSQL_MYSQL_PASSWORD", "autosql")


@pytest.fixture(scope="module", autouse=True)
def mysql_server():
    if not CONTAINER or not shutil.which("docker"):
        live_unavailable("set AUTOSQL_MYSQL_CONTAINER to a running MySQL 8 container (see the module docstring)")
    if subprocess.run(["docker", "exec", CONTAINER, "mysqladmin", "ping", "-uroot", f"-p{PASSWORD}", "--silent"],
                      capture_output=True).returncode != 0:
        live_unavailable(f"the MySQL container {CONTAINER!r} is not running or not ready")


def mysql(db: str, sql: str | None = None, workdir: str | None = None, file: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["docker", "exec", "-i"] + (["-w", workdir] if workdir else []) + [CONTAINER, "sh", "-c",
           f"mysql --local-infile=1 --default-character-set=utf8mb4 --batch -uroot -p{PASSWORD} {db}" + (f" < {file}" if file else "")]
    return subprocess.run(cmd, input=sql, capture_output=True, text=True)


@pytest.mark.parametrize("case", [c for c in CASES if not c.endswith("_validate")] + ["shop_validate"])
def test_exported_project_runs_on_mysql_and_matches_duckdb(case, northwind_catalog, tmp_path):
    catalog, d, cells = build(case, northwind_catalog)
    db, remote = f"autosql_{case}", f"/tmp/autosql_{case}"
    assert mysql("", f"DROP DATABASE IF EXISTS {db}; CREATE DATABASE {db};").returncode == 0
    zipfile.ZipFile(io.BytesIO(to_project_zip(cells, d, catalog, "mysql", case))).extractall(tmp_path)
    subprocess.run(["docker", "exec", CONTAINER, "rm", "-rf", remote], check=True)
    subprocess.run(["docker", "cp", str(tmp_path), f"{CONTAINER}:{remote}"], check=True, capture_output=True)
    for f in ("01_schema.sql", "02_load.sql", "03_validate.sql", "04_analysis.sql", "05_performance.sql"):
        out = mysql(db, workdir=remote, file=f)
        assert out.returncode == 0, f"{f}: {out.stderr[-1500:]}"

    results, _ = run_cells(cells, catalog)
    duck = {(r.cell_id, s.label): s.frame for r in results for s in r.statements if s.frame is not None}
    mismatches = []
    for cell in cells:
        for st in cell.statements:
            if st.run != "query" or st.label in ("plan", "preview") or not len(duck.get((cell.id, st.label), [])):
                continue
            if st.sql["mysql"].lstrip().upper().startswith(("CREATE", "-- GRAIN")):
                continue
            out = mysql(db, st.sql["mysql"] + ";")
            assert out.returncode == 0, f"{cell.id}/{st.label}: {out.stderr[-800:]}"
            got = pd.read_csv(io.StringIO(out.stdout), sep="\t", keep_default_na=False, na_values=["NULL"], quoting=3)
            problems = _same(got, duck[(cell.id, st.label)].reset_index(drop=True))
            if problems:
                mismatches.append((cell.id, st.label, problems[:3]))
    assert not mismatches, mismatches
