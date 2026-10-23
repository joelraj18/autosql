"""AutoSQL Explorer: profile tables, prove their grain, decide the analysis, write and explain the SQL."""
from .codegen import Cell, Statement, assumptions, build_cells
from .decision import FOCUS_MODES, Decision, decide
from .dialect import DIALECTS, LABELS, SHOWN, Dialect
from .loading import Catalog, Table, build_database, read_files, read_folder
from .narrative import GLOSSARY, Step, interpret
from .profiling import CatalogProfile, profile_catalog
from .runner import CellResult, run_cells
from .schema import Schema, infer_schema, link_roles, to_dot, to_mermaid

__all__ = [
    "Catalog", "CatalogProfile", "Cell", "CellResult", "DIALECTS", "Decision", "Dialect", "FOCUS_MODES", "GLOSSARY", "LABELS",
    "SHOWN", "Schema", "Statement", "Step", "Table", "assumptions", "build_cells", "build_database", "decide", "infer_schema",
    "interpret", "link_roles", "profile_catalog", "read_files", "read_folder", "run_cells", "to_dot", "to_mermaid",
]
