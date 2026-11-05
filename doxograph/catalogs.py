"""Workspace catalogs: records of the things research names, such as datasets
and benchmarks. A record describes the thing and links the papers and claims
about it; claims also link themselves through a facet that names it."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from collections.abc import Callable
from typing import Annotated, Any, ClassVar, Self, TypeVar

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

from . import config, storage, store
from .notebook import PaperKey, Reference

Token = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class RecordField(BaseModel):
    """A detail the writer names, such as the model that generated the data."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    value: str = Field(default="", max_length=4000)

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Name each custom field.")
        return value.strip()


class RecordBody(BaseModel):
    """What every catalog record holds. A catalog with more, such as the roles
    a dataset plays, adds them in a subclass."""
    model_config = ConfigDict(extra="forbid")
    noun: ClassVar[str] = "record"
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20000)
    source: str = Field(default="", max_length=4000)
    version: str = Field(default="", max_length=300)
    license: str = Field(default="", max_length=1000)
    access_notes: str = Field(default="", max_length=20000)
    labels: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=200)
    facet_values: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(default_factory=list, max_length=200)
    papers: list[PaperKey] = Field(default_factory=list, max_length=1000)
    claims: list[Reference] = Field(default_factory=list, max_length=5000)
    custom_fields: list[RecordField] = Field(default_factory=list, max_length=100)

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(f"Give your {cls.noun} a name.")
        return value.strip()

    @field_validator("facet_values")
    @classmethod
    def nonblank_values(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError(f"{cls.noun.capitalize()} facet names must not be blank.")
        return list(dict.fromkeys(value.strip() for value in values))

    @field_validator("labels")
    @classmethod
    def normalize_labels(cls, values: list[str]) -> list[str]:
        return store.normalize_labels(values)

    @field_validator("custom_fields")
    @classmethod
    def distinct_field_names(cls, values: list[RecordField]) -> list[RecordField]:
        if len({store.label_key(field.name) for field in values}) != len(values):
            raise ValueError("Give each custom field a different name.")
        return values

    @model_validator(mode="after")
    def distinct_links(self) -> Self:
        self.papers = list(dict.fromkeys(self.papers))
        self.claims = list({(ref.paper, ref.claim): ref for ref in self.claims}.values())
        return self


class RecordUpdate(RecordBody):
    """An edit: the record as the writer left it, and the revision it started from."""
    revision: Token


Route = TypeVar("Route", bound=Callable[..., Any])


def _reading(model: type[BaseModel], route: Route) -> Route:
    """The route, with its body read as `model`."""
    route.__annotations__["body"] = model
    return route


class FacetNames(BaseModel):
    model_config = ConfigDict(extra="forbid")
    names: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(max_length=1000)


@dataclass
class Catalog:
    """One kind of record, stored in `<plural>.json` and served at `/api/<plural>`.
    Claims whose `facets` facet values name a record link to it."""
    noun: str
    plural: str
    body: type[RecordBody]
    facets: tuple[str, ...]

    def __post_init__(self) -> None:
        body, noun = self.body, self.noun

        class Update(body):  # type: ignore[valid-type, misc]
            revision: Token

        class Saved(body):  # type: ignore[valid-type, misc]
            model_config = ConfigDict(extra="allow", strict=True)
            id: Token
            # Random tokens also protect editors after deletion and backup restoration.
            revision: Token
            created: str
            updated: str

        class SavedList(RootModel[list[Saved]]):
            root: list[Saved] = Field(default_factory=list)

            @model_validator(mode="after")
            def unique_ids(self) -> Self:
                if len({item.id for item in self.root}) != len(self.root):
                    raise ValueError(f"{noun.capitalize()} identities must be unique.")
                return self

        self.Update, self.Saved, self.SavedList = Update, Saved, SavedList
        self.router = self._router()

    @property
    def filename(self) -> str:
        return self.plural + ".json"

    def load(self) -> list[dict[str, Any]]:
        return [item.model_dump() for item in storage.read_model(config.data_dir() / self.filename, self.SavedList).root]

    def restore(self, text: str) -> list[dict[str, Any]]:
        """A backup's records, with fresh revisions so open editors see the change."""
        restored: list[dict[str, Any]] = self.SavedList.model_validate_json(text).model_dump()
        for item in restored:
            item.update(revision=uuid.uuid4().hex, updated=store.now())
        return restored

    def matches(self, item: dict[str, Any], claim: dict[str, Any]) -> bool:
        """Whether one of the claim's facet values names the record or one of its other names."""
        names = {store.facet_value_key(value) for value in [item["name"], *item["facet_values"]]}
        return any(store.facet_value_key(value) in names for facet in self.facets
                   for value in claim.get("facets", {}).get(facet, []))

    def _find(self, items: list[dict[str, Any]], record_id: str) -> dict[str, Any]:
        item = next((item for item in items if item["id"] == record_id), None)
        if item is None:
            raise HTTPException(404, f"This {self.noun} record no longer exists.")
        return item

    def _check_revision(self, item: dict[str, Any], revision: str) -> None:
        if item["revision"] != revision:
            raise HTTPException(409, f"This {self.noun} changed in another window. Your edits are still here. "
                                     "Reload the saved version before saving.")

    def _write(self, items: list[dict[str, Any]]) -> None:
        store.write_json(config.data_dir() / self.filename, items)

    def create(self, body: RecordBody, operation_id: str | None = None) -> dict[str, Any]:
        """Store a new record. A retried create, with the same operation id, answers with the record it made."""
        with store.document_lock(self.plural):
            items = self.load()
            identifier = operation_id or uuid.uuid4().hex
            existing = next((item for item in items if item["id"] == identifier), None)
            if existing is not None:
                return existing
            validate_links(body)
            item = dict(body.model_dump(), id=identifier, revision=uuid.uuid4().hex,
                        created=store.now(), updated=store.now())
            items.append(item)
            self._write(items)
            return item

    def update(self, record_id: str, body: RecordUpdate) -> dict[str, Any]:
        with store.document_lock(self.plural):
            items = self.load()
            item = self._find(items, record_id)
            self._check_revision(item, body.revision)
            validate_links(body, item)
            item.update(body.model_dump(exclude={"revision"}), revision=uuid.uuid4().hex, updated=store.now())
            self._write(items)
            return item

    def _router(self) -> APIRouter:
        router = APIRouter(prefix="/api/" + self.plural)
        Body, Update = self.body, self.Update

        @router.get("", name=f"list_{self.plural}")
        def list_records() -> dict[str, Any]:
            return {self.plural: self.load()}

        def create_record(body: RecordBody,
                          operation_id: Annotated[Token | None, Header(alias="Idempotency-Key")] = None) -> dict[str, Any]:
            return self.create(body, operation_id)

        def update_record(record_id: Token, body: RecordUpdate) -> dict[str, Any]:
            return self.update(record_id, body)

        # FastAPI reads each body as its annotation says, so each takes this catalog's own model.
        router.post("", status_code=201, name=f"create_{self.noun}")(_reading(Body, create_record))
        router.put("/{record_id}", name=f"update_{self.noun}")(_reading(Update, update_record))

        @router.delete("/{record_id}", name=f"delete_{self.noun}")
        def delete_record(record_id: Token, revision: Annotated[Token, Query()]) -> dict[str, Any]:
            with store.document_lock(self.plural):
                items = self.load()
                self._check_revision(self._find(items, record_id), revision)
                self._write([item for item in items if item["id"] != record_id])
                return {"deleted": record_id}

        @router.post("/from-facets", name=f"add_{self.plural}_from_facets")
        def create_from_facets(body: FacetNames) -> dict[str, Any]:
            """A record for each name claims give in this catalog's facets that no
            record covers yet, so the names read from papers become records at once."""
            with store.document_lock(self.plural):
                items = self.load()
                covered = {store.facet_value_key(value) for item in items for value in [item["name"], *item["facet_values"]]}
                created = []
                for name in body.names:
                    key = store.facet_value_key(name)
                    if not key or key in covered:
                        continue
                    covered.add(key)
                    created.append(dict(Body(name=name).model_dump(), id=uuid.uuid4().hex, revision=uuid.uuid4().hex,
                                        created=store.now(), updated=store.now()))
                if created:
                    self._write(items + created)
                return {self.plural: created}

        return router


def validate_links(body: RecordBody, previous: dict[str, Any] | None = None) -> None:
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
