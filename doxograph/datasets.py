"""Workspace dataset catalog. References describe research; files stay at their source."""
from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import loupe
from .catalogs import Catalog, RecordBody, RecordField

Role = Literal["training", "evaluation", "reference", "project-output"]
DatasetField = RecordField


class DatasetBody(RecordBody):
    noun = "dataset"
    roles: list[Role] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def distinct_roles(self) -> Self:
        self.roles = list(dict.fromkeys(self.roles))
        return self


catalog = Catalog("dataset", "datasets", DatasetBody, facets=("dataset", "datasets"))
DatasetUpdate, SavedDataset, SavedDatasets = catalog.Update, catalog.Saved, catalog.SavedList
load = catalog.load
create_dataset, update_dataset = catalog.create, catalog.update
router = catalog.router


class LoupeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=4000)


@router.post("/loupe")
def open_in_loupe(body: LoupeBody) -> dict[str, str]:
    return {"url": loupe.open_dataset(body.source)}


def list_datasets() -> dict[str, Any]:
    return {"datasets": load()}
