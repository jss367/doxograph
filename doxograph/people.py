"""People in the workspace: the authors of its papers, and notes on them.

Every author on a paper has a page whether or not anyone has written about
them. A record holds what the reader knows about a person, and the spellings
of their name that author lists use, so "J. Smith" and "John Smith" can be one
person when the reader says they are.
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Annotated, Any, Self

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

from . import config, storage, store
from .notebook import PaperKey

router = APIRouter(prefix="/api/people")
Token = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Name = Annotated[str, Field(min_length=1, max_length=300)]


APOSTROPHES = "['\u2019\u02bc]"


def name_key(name: str) -> str:
    """What makes two spellings of a name one: case, accents, and the
    punctuation and spacing between its parts. "Schölkopf, B." and
    "scholkopf b" are one name; "B. Schölkopf" and "Bernhard Schölkopf" are
    two until a record lists both."""
    letters = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    # An apostrophe joins the parts of a name rather than separating them:
    # "O'Neil" is "ONeil", not "O Neil".
    letters = re.sub(APOSTROPHES, "", letters)
    return store.label_key(" ".join(re.findall(r"[^\W_]+", letters)))


def author_keys(papers: list[dict[str, Any]]) -> dict[str, str]:
    """Each author spelling's key, for the page to use in place of its own, as
    `label_keys` does for labels."""
    return {author: name_key(author) for paper in papers for author in paper.get("authors") or []}


class PersonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    other_names: list[Name] = Field(default_factory=list, max_length=50)
    affiliation: str = Field(default="", max_length=1000)
    homepage: str = Field(default="", max_length=4000)
    notes: str = Field(default="", max_length=100000)
    labels: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=200)
    # Papers the person did not write but that belong on their page: one they
    # recommended, reviewed, or that is about their work.
    papers: list[PaperKey] = Field(default_factory=list, max_length=1000)

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not name_key(value):
            raise ValueError("Give this person a name.")
        return value.strip()

    @field_validator("other_names")
    @classmethod
    def nonblank_names(cls, values: list[str]) -> list[str]:
        if any(not name_key(value) for value in values):
            raise ValueError("Other names must not be blank.")
        spellings: dict[str, str] = {}
        for value in values:
            spellings.setdefault(name_key(value), value.strip())
        return list(spellings.values())

    @field_validator("labels")
    @classmethod
    def normalize_labels(cls, values: list[str]) -> list[str]:
        return store.normalize_labels(values)

    @model_validator(mode="after")
    def distinct(self) -> Self:
        self.other_names = [value for value in self.other_names if name_key(value) != name_key(self.name)]
        self.papers = list(dict.fromkeys(self.papers))
        return self


class PersonUpdate(PersonBody):
    revision: Token


class SavedPerson(PersonBody):
    model_config = ConfigDict(extra="allow", strict=True)
    id: Token
    revision: Token
    created: str
    updated: str


class SavedPeople(RootModel[list[SavedPerson]]):
    root: list[SavedPerson] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if len({item.id for item in self.root}) != len(self.root):
            raise ValueError("Person identities must be unique.")
        # Every reader takes a spelling to name one person. A file that gives
        # one spelling to two records is damaged, as two records with one id are.
        owners: dict[str, str] = {}
        for item in self.root:
            for key in {name_key(value) for value in [item.name, *item.other_names]}:
                if owners.setdefault(key, item.id) != item.id:
                    raise ValueError(f"Two people share the name “{key}”.")
        return self


def load() -> list[dict[str, Any]]:
    return [item.model_dump() for item in storage.read_model(config.data_dir() / "people.json", SavedPeople).root]


def names(person: dict[str, Any]) -> set[str]:
    return {name_key(value) for value in [person["name"], *person["other_names"]]}


def authored(person: dict[str, Any], papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The papers whose author lists name this person by any of their names."""
    keys = names(person)
    return [paper for paper in papers if any(name_key(author) in keys for author in paper.get("authors") or [])]


def find(items: list[dict[str, Any]], person_id: str) -> dict[str, Any]:
    item = next((item for item in items if item["id"] == person_id), None)
    if item is None:
        raise HTTPException(404, "This person's record no longer exists.")
    return item


def check_revision(item: dict[str, Any], revision: str) -> None:
    if item["revision"] != revision:
        raise HTTPException(409, "This person changed in another window. Your edits are still here. "
                                 "Reload the saved version before saving.")


def check_names(body: PersonBody, items: list[dict[str, Any]], person_id: str) -> None:
    """One spelling names one person, so an author on a paper has one page."""
    for other in items:
        if other["id"] == person_id:
            continue
        taken = names(other)
        clash = next((value for value in [body.name, *body.other_names] if name_key(value) in taken), None)
        if clash is not None:
            raise HTTPException(409, f"“{clash}” already names {other['name']}. Add it to one record only.")


def check_papers(body: PersonBody, previous: dict[str, Any] | None = None) -> None:
    """Reject new links to removed papers, but keep the ones already saved."""
    for key in set(body.papers) - set((previous or {}).get("papers", [])):
        try:
            store.load_paper(key)
        except KeyError:
            raise HTTPException(422, "A selected paper was removed. Remove its link before saving.") from None


@router.get("")
def list_people() -> dict[str, Any]:
    return {"people": load()}


@router.post("", status_code=201)
def create_person(body: PersonBody,
                  operation_id: Annotated[Token | None, Header(alias="Idempotency-Key")] = None) -> dict[str, Any]:
    with store.document_lock("people"):
        items = load()
        identifier = operation_id or uuid.uuid4().hex
        existing = next((item for item in items if item["id"] == identifier), None)
        if existing is not None:
            return existing
        check_names(body, items, identifier)
        check_papers(body)
        item = dict(body.model_dump(), id=identifier, revision=uuid.uuid4().hex,
                    created=store.now(), updated=store.now())
        items.append(item)
        store.write_json(config.data_dir() / "people.json", items)
        return item


@router.put("/{person_id}")
def update_person(person_id: Token, body: PersonUpdate) -> dict[str, Any]:
    with store.document_lock("people"):
        items = load()
        item = find(items, person_id)
        check_revision(item, body.revision)
        check_names(body, items, person_id)
        check_papers(body, item)
        item.update(body.model_dump(exclude={"revision"}), revision=uuid.uuid4().hex, updated=store.now())
        store.write_json(config.data_dir() / "people.json", items)
        return item


@router.delete("/{person_id}")
def delete_person(person_id: Token, revision: Annotated[Token, Query()]) -> dict[str, Any]:
    with store.document_lock("people"):
        items = load()
        check_revision(find(items, person_id), revision)
        store.write_json(config.data_dir() / "people.json", [item for item in items if item["id"] != person_id])
        return {"deleted": person_id}
