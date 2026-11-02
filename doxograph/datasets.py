"""Workspace dataset catalog. References describe research; files stay at their source."""
from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

from . import config, storage, store
from .notebook import PaperKey, Reference

router = APIRouter(prefix="/api/datasets")
Token = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Role = Literal["training", "evaluation", "reference", "project-output"]


class DatasetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20000)
    source: str = Field(default="", max_length=4000)
    version: str = Field(default="", max_length=300)
    license: str = Field(default="", max_length=1000)
    access_notes: str = Field(default="", max_length=20000)
    roles: list[Role] = Field(default_factory=list, max_length=4)
    labels: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=200)
    facet_values: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(default_factory=list, max_length=200)
    papers: list[PaperKey] = Field(default_factory=list, max_length=1000)
    claims: list[Reference] = Field(default_factory=list, max_length=5000)

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Give your dataset a name.")
        return value.strip()

    @field_validator("facet_values")
    @classmethod
    def nonblank_values(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("Dataset facet names must not be blank.")
        return list(dict.fromkeys(value.strip() for value in values))

    @field_validator("labels")
    @classmethod
    def normalize_labels(cls, values: list[str]) -> list[str]:
        return store.normalize_labels(values)

    @model_validator(mode="after")
    def distinct_links(self) -> Self:
        self.roles = list(dict.fromkeys(self.roles))
        self.papers = list(dict.fromkeys(self.papers))
        self.claims = list({(ref.paper, ref.claim): ref for ref in self.claims}.values())
        return self


class DatasetUpdate(DatasetBody):
    revision: Token


class SavedDataset(DatasetBody):
    model_config = ConfigDict(extra="allow", strict=True)
    id: Token
    # Random tokens also protect editors after deletion and backup restoration.
    revision: Token
    created: str
    updated: str


class SavedDatasets(RootModel[list[SavedDataset]]):
    root: list[SavedDataset] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if len({item.id for item in self.root}) != len(self.root):
            raise ValueError("Dataset identities must be unique.")
        return self


def load() -> list[dict[str, Any]]:
    return [item.model_dump() for item in storage.read_model(config.data_dir() / "datasets.json", SavedDatasets).root]


def find(items: list[dict[str, Any]], dataset_id: str) -> dict[str, Any]:
    item = next((item for item in items if item["id"] == dataset_id), None)
    if item is None:
        raise HTTPException(404, "This dataset record no longer exists.")
    return item


def check_revision(item: dict[str, Any], revision: str) -> None:
    if item["revision"] != revision:
        raise HTTPException(409, "This dataset changed in another window. Your edits are still here. Reload the saved version before saving.")


def validate_links(body: DatasetBody, previous: dict[str, Any] | None = None) -> None:
    """Reject new broken links, but retain existing references to removed sources."""
    previous = previous or {}
    old_claims = {(ref["paper"], ref["claim"]) for ref in previous.get("claims", [])}
    new_papers = set(body.papers) - set(previous.get("papers", []))
    new_claims = {(ref.paper, ref.claim) for ref in body.claims} - old_claims
    for key in new_papers | {paper for paper, _ in new_claims}:
        try:
            paper = store.load_paper(key)
        except KeyError:
            raise HTTPException(422, "A selected paper was removed. Remove its link before saving.") from None
        ids = {claim["id"] for claim in paper.get("claims", [])}
        if any(claim not in ids for owner, claim in new_claims if owner == key):
            raise HTTPException(422, "A selected claim was removed. Remove its link before saving.")


@router.get("")
def list_datasets() -> dict[str, Any]:
    return {"datasets": load()}


@router.post("", status_code=201)
def create_dataset(body: DatasetBody,
                   operation_id: Annotated[Token | None, Header(alias="Idempotency-Key")] = None) -> dict[str, Any]:
    with store.document_lock("datasets"):
        items = load()
        identifier = operation_id or uuid.uuid4().hex
        existing = next((item for item in items if item["id"] == identifier), None)
        if existing is not None:
            return existing
        validate_links(body)
        item = dict(body.model_dump(), id=identifier, revision=uuid.uuid4().hex,
                    created=store.now(), updated=store.now())
        items.append(item)
        store.write_json(config.data_dir() / "datasets.json", items)
        return item


@router.put("/{dataset_id}")
def update_dataset(dataset_id: Token, body: DatasetUpdate) -> dict[str, Any]:
    with store.document_lock("datasets"):
        items = load()
        item = find(items, dataset_id)
        check_revision(item, body.revision)
        validate_links(body, item)
        item.update(body.model_dump(exclude={"revision"}), revision=uuid.uuid4().hex, updated=store.now())
        store.write_json(config.data_dir() / "datasets.json", items)
        return item


@router.delete("/{dataset_id}")
def delete_dataset(dataset_id: Token, revision: Annotated[Token, Query()]) -> dict[str, Any]:
    with store.document_lock("datasets"):
        items = load()
        check_revision(find(items, dataset_id), revision)
        store.write_json(config.data_dir() / "datasets.json", [item for item in items if item["id"] != dataset_id])
        return {"deleted": dataset_id}
