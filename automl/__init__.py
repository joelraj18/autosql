"""AutoML Explorer: profile a dataset, decide a pipeline, explain every step."""
from .codegen import Cell, build_cells, to_notebook
from .decision import TARGET_MODES, Decision, decide, resolve_target
from .narrative import GLOSSARY, Step, interpret
from .profiling import DatasetProfile, profile_dataset
from .runner import CellResult, run_cells

__all__ = [
    "Cell", "CellResult", "DatasetProfile", "Decision", "GLOSSARY", "Step", "TARGET_MODES",
    "build_cells", "decide", "interpret", "profile_dataset", "resolve_target", "run_cells", "to_notebook",
]
