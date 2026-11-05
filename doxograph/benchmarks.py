"""Workspace benchmark catalog: the evaluations results are measured on, and
the claims measured on each, matched through the benchmark facet."""
from __future__ import annotations

from typing import Any

from .catalogs import Catalog, RecordBody


class BenchmarkBody(RecordBody):
    noun = "benchmark"


catalog = Catalog("benchmark", "benchmarks", BenchmarkBody, facets=("benchmark", "benchmarks"))
SavedBenchmarks = catalog.SavedList
load = catalog.load
router = catalog.router


def list_benchmarks() -> dict[str, Any]:
    return {"benchmarks": load()}
