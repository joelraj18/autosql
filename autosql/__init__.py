"""AutoSQL Explorer: profile tables, prove their grain, decide the analysis, write and explain the SQL."""
from .codegen import Cell, Statement, assumption_items, assumptions, build_cells
from .custom import CustomKPI, Customization, CustomizationError
from .decision import FOCUS_MODES, Decision, decide
from .dialect import DIALECTS, LABELS, SHOWN, Dialect
from .loading import Catalog, Table, build_database, read_files, read_folder
from .narrative import GLOSSARY, Step, interpret
from .profiling import CatalogProfile, profile_catalog
from .runner import CellResult, DryRun, dry_run, run_cells
from .schema import Schema, infer_schema, link_roles, to_dot, to_mermaid

__all__ = [
    "Catalog", "CatalogProfile", "Cell", "CellResult", "CustomKPI", "Customization", "CustomizationError", "DIALECTS", "Decision",
    "Dialect", "DryRun", "FOCUS_MODES", "GLOSSARY", "LABELS", "SHOWN", "Schema", "Statement", "Step", "Table", "assumption_items",
    "assumptions", "build_cells", "build_database", "decide", "dry_run", "infer_schema", "interpret", "link_roles", "profile_catalog",
    "read_files", "read_folder", "run_cells", "to_dot", "to_mermaid",
]
