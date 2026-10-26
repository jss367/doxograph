"""Contracts for saved cross-paper analysis.

Older optional fields stay optional, and unknown fields survive a read/write
cycle. Validation protects the shapes used by the store without migrating data.
"""

from typing import Any, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class StorageIssue(TypedDict):
    path: str
    detail: str
    paper: NotRequired[str]


class Document(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)


class ReviewRecord(Document):
    id: str
    claims: list[str]
    topics: list[str] = Field(default_factory=list)
    note: str = ""
    status: str = "open"
    found: str | None = None
    fingerprints: dict[str, str] | None = None


class TensionRecord(ReviewRecord):
    kind: str = "tension"


class Tensions(Document):
    seq: int = Field(default=0, ge=0)
    tensions: list[TensionRecord] = Field(default_factory=list)
    passes: dict[str, str] = Field(default_factory=dict)


class Agreements(Document):
    seq: int = Field(default=0, ge=0)
    agreements: list[ReviewRecord] = Field(default_factory=list)
    passes: dict[str, str] = Field(default_factory=dict)


class SynthesisRecord(Document):
    text: str
    topic: str | None = None
    source: str = "model"
    written: str | None = None
    prompt_basis: str | None = None
    claims: dict[str, str] | None = None
    # The judgment basis changed shape over time; older lists remain readable
    # and are marked stale by the store's comparison against the current basis.
    tensions: dict[str, list[Any]] | None = None


class Syntheses(Document):
    syntheses: dict[str, SynthesisRecord] = Field(default_factory=dict)
