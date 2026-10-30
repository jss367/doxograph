"""Routes for tags, tensions, agreements, syntheses, the ledger, and the research context."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import job_queue, storage, store

router = APIRouter()


class TagBody(BaseModel):
    name: str
    description: str = ""


class RenameBody(BaseModel):
    name: str


class RetagBody(BaseModel):
    keys: list[str] | None = None


class FacetBody(BaseModel):
    name: str = Field(max_length=60)
    description: str = Field(default="", max_length=2000)


class FacetDescriptionBody(BaseModel):
    description: str = Field(max_length=2000)


class FacetValuesBody(BaseModel):
    """Rename or merge values into `into`, or remove them with an empty one."""

    values: list[str] = Field(min_length=1, max_length=200)
    into: str = Field(default="", max_length=200)


class FillFacetsBody(BaseModel):
    keys: list[str] | None = None
    # Ask about every facet on every paper, stale or not.
    force: bool = False


class TensionsBody(BaseModel):
    topics: list[str] | None = None
    # A topic nothing has changed in is not asked about again. This asks anyway.
    force: bool = False


class TensionStatusBody(BaseModel):
    status: str


class AgreementsBody(BaseModel):
    topics: list[str] | None = None
    # A topic nothing has changed in is not asked about again. This asks anyway.
    force: bool = False


class AgreementStatusBody(BaseModel):
    status: str


class SynthesesBody(BaseModel):
    topics: list[str] | None = None
    # A topic nothing has changed in is not asked about again. This asks anyway.
    force: bool = False


class SynthesisTextBody(BaseModel):
    text: str


class LedgerClaim(BaseModel):
    id: str
    text: str = ""


class LedgerBody(BaseModel):
    claims: list[LedgerClaim]


class ContextBody(BaseModel):
    text: str


@router.post("/api/retag")
def retag(body: RetagBody) -> dict[str, Any]:
    job_queue._require_analysis()
    keys = body.keys or [p["key"] for p in store.all_papers() if p.get("claims")]
    if not keys:
        return {"queued": 0}
    job = job_queue._new_job(f"retag {len(keys)} papers")
    job_queue._submit_job(job_queue._run_retag, job, keys)
    return {"queued": len(keys)}


@router.post("/api/tags")
def create_tag(body: TagBody) -> dict[str, Any]:
    return {"tags": store.add_tag(body.name, body.description)}


@router.patch("/api/tags/{name}")
def patch_tag(name: str, body: RenameBody) -> dict[str, Any]:
    try:
        store.rename_tag(name, body.name)
    except storage.StorageReadError:
        raise
    except store.PaperReadError:
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"tags": store.load_tags()}


@router.delete("/api/tags/{name}")
def remove_tag(name: str) -> dict[str, Any]:
    store.delete_tag(name)
    return {"tags": store.load_tags()}


@router.post("/api/facets")
def create_facet(body: FacetBody) -> dict[str, Any]:
    try:
        return {"facets": store.add_facet(body.name, body.description)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.patch("/api/facets/{name}")
def patch_facet(name: str, body: FacetDescriptionBody) -> dict[str, Any]:
    try:
        return {"facets": store.describe_facet(name, body.description)}
    except KeyError:
        raise HTTPException(404, f"no facet {name}") from None


@router.delete("/api/facets/{name}")
def remove_facet(name: str) -> dict[str, Any]:
    try:
        return {"facets": store.delete_facet(name)}
    except KeyError:
        raise HTTPException(404, f"no facet {name}") from None


@router.post("/api/facets/{name}/values")
def rewrite_facet_values(name: str, body: FacetValuesBody) -> dict[str, Any]:
    """Also answers with the value the claims now carry: `into` resolved
    through the aliases, so a merge into a spelling an earlier rename
    replaced names what it became, and "" for a removal."""
    try:
        facets = store.rewrite_facet_values(name, body.values, body.into)
    except KeyError:
        raise HTTPException(404, f"no facet {name}") from None
    facet = next(facet for facet in facets if facet["name"] == name)
    value = store.resolve_facet_value(store.facet_value_text(body.into), store.facet_aliases(facet))
    return {"facets": facets, "value": value}


@router.post("/api/facets/fill")
def fill_facets(body: FillFacetsBody) -> dict[str, Any]:
    job_queue._require_analysis()
    facets = store.load_facets()
    keys = body.keys or [p["key"] for p in store.all_papers()
                         if p.get("claims") and (body.force or store.stale_facets(p, facets))]
    if not facets or not keys:
        return {"queued": 0}
    job = job_queue._new_job(f"fill facets on {len(keys)} papers")
    job_queue._submit_job(job_queue._run_fill_facets, job, keys, body.force)
    return {"queued": len(keys)}


@router.post("/api/tensions")
def find_tensions(body: TensionsBody) -> dict[str, Any]:
    """Queue a pass over every topic where two papers could disagree.

    Topics named in the body are deduplicated and kept only where a tension is
    possible; a topic with claims from one paper has nothing to find, so it
    would cost a model call to learn nothing.
    """
    job_queue._require_analysis()
    possible = store.tension_topics()
    topics = [t for t in possible if t in set(body.topics)] if body.topics else possible
    if not topics:
        return {"queued": 0}
    job = job_queue._new_job(f"tensions in {len(topics)} topics")
    job_queue._submit_job(job_queue._run_tensions, job, topics, body.force)
    return {"queued": len(topics)}


@router.patch("/api/tensions/{tension_id}")
def patch_tension(tension_id: str, body: TensionStatusBody) -> dict[str, Any]:
    try:
        return store.set_tension_status(tension_id, body.status)
    except storage.StorageReadError:
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except KeyError:
        raise HTTPException(404, f"no tension {tension_id}") from None


@router.delete("/api/tensions/{tension_id}")
def remove_tension(tension_id: str) -> dict[str, Any]:
    try:
        store.delete_tension(tension_id)
    except KeyError:
        raise HTTPException(404, f"no tension {tension_id}") from None
    return {"deleted": tension_id}


@router.post("/api/agreements")
def find_agreements(body: AgreementsBody) -> dict[str, Any]:
    """Queue a pass over every topic where two papers could agree: the same
    topics a tension is possible in."""
    job_queue._require_analysis()
    possible = store.tension_topics()
    topics = [t for t in possible if t in set(body.topics)] if body.topics else possible
    if not topics:
        return {"queued": 0}
    job = job_queue._new_job(f"agreements in {len(topics)} topics")
    job_queue._submit_job(job_queue._run_agreements, job, topics, body.force)
    return {"queued": len(topics)}


@router.patch("/api/agreements/{agreement_id}")
def patch_agreement(agreement_id: str, body: AgreementStatusBody) -> dict[str, Any]:
    try:
        return store.set_agreement_status(agreement_id, body.status)
    except storage.StorageReadError:
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except KeyError:
        raise HTTPException(404, f"no agreement {agreement_id}") from None


@router.delete("/api/agreements/{agreement_id}")
def remove_agreement(agreement_id: str) -> dict[str, Any]:
    try:
        store.delete_agreement(agreement_id)
    except KeyError:
        raise HTTPException(404, f"no agreement {agreement_id}") from None
    return {"deleted": agreement_id}


@router.post("/api/syntheses")
def synthesize(body: SynthesesBody) -> dict[str, Any]:
    """Queue a synthesis for each topic.

    Named topics are deduplicated and kept only where they have a claim; with
    none named, every topic with claims from two papers is written, since one
    paper's claims add up to little on their own.
    """
    job_queue._require_analysis()
    if body.topics:
        rows = store.claim_rows()
        topics = sorted({t for t in body.topics if store.topic_claims(t, rows)})
    else:
        topics = store.synthesis_topics()
    if not topics:
        return {"queued": 0}
    job = job_queue._new_job(f"synthesis of {len(topics)} topics")
    job_queue._submit_job(job_queue._run_syntheses, job, topics, body.force)
    return {"queued": len(topics)}


@router.patch("/api/syntheses/{topic}")
def patch_synthesis(topic: str, body: SynthesisTextBody) -> dict[str, Any]:
    try:
        return store.set_synthesis_text(topic, body.text)
    except storage.StorageReadError:
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except KeyError:
        raise HTTPException(404, f"no synthesis for {topic}") from None


@router.delete("/api/syntheses/{topic}")
def remove_synthesis(topic: str) -> dict[str, Any]:
    try:
        store.delete_synthesis(topic)
    except KeyError:
        raise HTTPException(404, f"no synthesis for {topic}") from None
    return {"deleted": topic}


@router.put("/api/ledger")
def put_ledger(body: LedgerBody) -> dict[str, Any]:
    """Replace the ledger: the user's own claims, which extraction links to.

    Ids must be present and unique, since a link names a claim by id alone.
    Links on paper claims that name a removed id are left in place; the card
    shows the bare id, and the user can take the link off or restore the claim.
    """
    claims = []
    seen = set()
    for item in body.claims:
        claim_id = item.id.strip()
        if not claim_id:
            raise HTTPException(422, "every ledger claim needs an id")
        if claim_id in seen:
            raise HTTPException(422, f"ledger id {claim_id!r} is used twice")
        seen.add(claim_id)
        claims.append({"id": claim_id, "text": item.text.strip()})
    store.save_ledger(claims)
    return {"ledger": store.load_ledger()}


@router.put("/api/context")
def put_context(body: ContextBody) -> dict[str, Any]:
    """Replace the research context given to every model pass."""
    store.save_context(body.text)
    return {"context": store.load_context()}
