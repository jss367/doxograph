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


class LedgerLink(Document):
    claim: str
    relation: str = "independent"
    note: str = ""


class Claim(Document):
    id: str
    text: str = ""
    kind: str = "finding"
    strength: str = "supporting"
    tags: list[str] = Field(default_factory=list)
    evidence: str = ""
    quote: str = ""
    locator: str = ""
    ledger_links: list[LedgerLink] = Field(default_factory=list)
    note: str = ""
    reviewed: bool = False
    quote_verified: bool | None = None
    quote_page: int | None = Field(default=None, ge=1)
    added: str | None = None


class Source(Document):
    kind: str = ""
    id: str = ""
    url: str | None = None
    pdf_url: str | None = None


class HistoryEntry(Document):
    id: str
    at: str
    claim: str | None = None
    fields: dict[str, Any] | None = None


class PDFFile(Document):
    size: int | None = Field(default=None, ge=0)
    mtime_ns: int | None = None
    sha256: str = ""


class Usage(Document):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)


class Extraction(Document):
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    at: str | None = None
    schema_version: int | None = None
    usage: Usage | None = None


class Paper(Document):
    key: str
    title: str = ""
    # Older imports can record unknown authors as null; existing citation
    # readers deliberately treat that as an empty list.
    authors: list[str] | None = None
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    abstract: str = ""
    source: Source | None = None
    pdf_file: PDFFile = Field(default_factory=PDFFile)
    summary: str | None = None
    relevance: str | None = None
    status: str = "fetched"
    extraction: Extraction | None = None
    proposed_tags: list["Topic"] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    history: list[HistoryEntry] = Field(default_factory=list)
    claim_seq: int = Field(default=0, ge=0)
    notes: str = ""
    error: str = ""
    added: str | None = None
    updated: str | None = None


class Topic(Document):
    name: str
    description: str = ""


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
