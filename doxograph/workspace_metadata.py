"""Contracts for hand-edited workspace metadata and retired paper identities."""

import re
from typing import Self

from pydantic import ConfigDict, Field, JsonValue, RootModel, field_validator, model_validator

from .records import Document


class Metadata(Document):
    # Preserve future fields while rejecting YAML-only values that cannot be
    # returned in library JSON or safely carried through a read/write cycle.
    model_config = ConfigDict(extra="allow", strict=True, allow_inf_nan=False)
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)


class VocabularyTerm(Metadata):
    name: str
    description: str = ""

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("topic names must not be blank")
        return value


class Vocabulary(Metadata):
    tags: list[VocabularyTerm] | None = None
    retired_topics: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_names(self) -> Self:
        names = [term.name for term in self.tags or []]
        if len(names) != len(set(names)):
            raise ValueError("topic names must be unique")
        if any(not name.strip() for name in self.retired_topics):
            raise ValueError("retired topic names must not be blank")
        return self


class FacetDefinition(Metadata):
    name: str
    description: str = ""
    # A spelling's key, as `store.facet_value_key` makes it, mapped to the
    # value it stands for. An empty value drops the spelling wherever it
    # turns up, so a value removed by hand does not come back on the next
    # read of a paper.
    aliases: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def slug_name(cls, value: str) -> str:
        # The form `store.slugify` gives a name. The page writes a facet
        # filter as `name:value` and splits it at the first colon, so a name
        # written by hand with a colon, a space, or a capital would filter on
        # some other name.
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
            raise ValueError("facet names must be lowercase letters and digits joined by hyphens, "
                             "such as model or training-method")
        return value


class Facets(Metadata):
    facets: list[FacetDefinition] | None = None

    @model_validator(mode="after")
    def unique_names(self) -> Self:
        names = [facet.name for facet in self.facets or []]
        if len(names) != len(set(names)):
            raise ValueError("facet names must be unique")
        return self


class LedgerEntry(Metadata):
    id: str
    text: str = ""

    @field_validator("id")
    @classmethod
    def nonblank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("claim IDs must not be blank")
        return value


class ResearchLedger(Metadata):
    claims: list[LedgerEntry] | None = None

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        ids = [claim.id for claim in self.claims or []]
        if len(ids) != len(set(ids)):
            raise ValueError("claim IDs must be unique")
        return self


class RetiredKeys(RootModel[list[str]]):
    root: list[str] = Field(default_factory=list)

    @field_validator("root")
    @classmethod
    def nonblank_keys(cls, value: list[str]) -> list[str]:
        if any(not key.strip() for key in value):
            raise ValueError("retired keys must not be blank")
        return value
