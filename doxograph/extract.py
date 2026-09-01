"""Read a paper with Claude and return its claims.

The PDF is uploaded once through the Files API and referenced by id, so the
request body stays small no matter how large the paper is (an inlined base64
PDF over about 24 MB exceeds the 32 MB request limit). It goes in first with a
cache breakpoint on it, so re-running extraction against a changed field list
or a grown tag vocabulary re-reads the paper from cache rather than paying for
it again.
"""

from __future__ import annotations

import json
from pathlib import Path

import anthropic

from . import config, store

SYSTEM = """\
You are building a doxography: a record of what each paper claims, arranged so \
that claims from different papers about the same question can be compared.

Extract the claims a paper makes. A claim is one assertion the paper puts \
forward and takes responsibility for. Most papers make between two and eight \
that are worth recording.

Rules that matter:

- One assertion per claim. Split conjunctions.
- Write each claim as a standalone sentence that is intelligible without the \
paper in front of you. Name the models, datasets, and conditions rather than \
writing "the model" or "our method".
- Preserve the paper's own scope and hedging. If a result holds for one model \
at one scale, say so in the claim. Do not upgrade a suggestive result into a \
demonstrated one.
- `quote` must be verbatim from the paper and must be the sentence that most \
directly supports the claim. Never paraphrase into the quote field.
- Record what the paper reports, including results that undercut its own \
framing and results reported only in an appendix.
- Do not record background, related work, or claims the paper attributes to \
others. Only what this paper asserts.
- Prefer tags from the supplied vocabulary. Propose a new tag only when no \
existing tag fits; a proposed tag should name a topic other papers could also \
be about, not a detail of this one paper.
"""

CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "The claim as a standalone sentence, in the paper's own scope.",
        },
        "kind": {
            "type": "string",
            "enum": config.CLAIM_KINDS,
            "description": (
                "finding: an empirical result. method: a technique or measurement the paper "
                "contributes. definition: a concept or framing it introduces. negative: something "
                "it shows does not hold or does not work. conjecture: something it argues without "
                "establishing."
            ),
        },
        "strength": {
            "type": "string",
            "enum": config.CLAIM_STRENGTHS,
            "description": (
                "headline: a claim in the abstract or the paper's stated contributions. "
                "supporting: a result in the body. aside: a remark, footnote, or appendix result."
            ),
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Topic tags, drawn from the supplied vocabulary where possible.",
        },
        "evidence": {
            "type": "string",
            "description": (
                "How the claim was established: models, datasets, sample sizes, metrics, "
                "and the effect size if one is reported. Empty for definitions."
            ),
        },
        "quote": {
            "type": "string",
            "description": "The verbatim sentence from the paper that most directly supports the claim.",
        },
        "locator": {
            "type": "string",
            "description": "Where in the paper, e.g. 'p. 4', 'Table 2', 'Sec. 3.1'.",
        },
        "ledger_links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string", "description": "The id of one of my own claims."},
                    "relation": {"type": "string", "enum": config.LEDGER_RELATIONS},
                    "note": {"type": "string", "description": "One sentence on how it bears on my claim."},
                },
                "required": ["claim", "relation", "note"],
                "additionalProperties": False,
            },
            "description": "Links to my own claims. Empty when the paper bears on none of them.",
        },
    },
    "required": ["text", "kind", "strength", "tags", "evidence", "quote", "locator", "ledger_links"],
    "additionalProperties": False,
}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One paragraph on what the paper does: the question, the method, the result.",
        },
        "relevance": {
            "type": "string",
            "description": "One sentence on why this paper matters to the research described below.",
        },
        "claims": {"type": "array", "items": CLAIM_SCHEMA},
        "proposed_tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "lowercase-hyphenated"},
                    "description": {"type": "string"},
                },
                "required": ["name", "description"],
                "additionalProperties": False,
            },
            "description": "New tags used above that are absent from the vocabulary.",
        },
    },
    "required": ["summary", "relevance", "claims", "proposed_tags"],
    "additionalProperties": False,
}

RETAG_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "tags"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["assignments"],
    "additionalProperties": False,
}


def client() -> anthropic.Anthropic:
    key = config.api_key()
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def vocabulary_block(tags: list[dict] | None = None) -> str:
    tags = store.load_tags() if tags is None else tags
    if not tags:
        return (
            "The tag vocabulary is empty; this is the first paper. Propose the tags you need, "
            "naming topics that later papers could also be about."
        )
    lines = [f"- {t['name']}: {t.get('description', '')}".rstrip() for t in tags]
    return "Tag vocabulary (prefer these):\n" + "\n".join(lines)


def ledger_block() -> str:
    claims = store.load_ledger()
    if not claims:
        return "I have no claims of my own recorded yet, so leave every ledger_links list empty."
    lines = [f"- {c['id']}: {c.get('text', '')}".rstrip() for c in claims]
    return (
        "My own claims, which this paper may bear on. Link a paper's claim to one of mine only "
        "when the connection is direct enough that I would cite the paper at that point:\n"
        + "\n".join(lines)
    )


def context_block() -> str:
    path = config.data_dir() / "context.md"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return "No research context has been recorded, so judge relevance broadly."


def _pdf_fingerprint(pdf: Path) -> dict:
    """What identifies the bytes an upload was made from."""
    st = pdf.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def upload_pdf(key: str, api: anthropic.Anthropic | None = None, force: bool = False) -> str:
    """The Files API id of the paper's PDF, uploading it if none is current.

    The id is kept on the paper with the size and mtime of the file it came
    from. A re-downloaded or replaced PDF then gets a fresh upload; the same
    file is never uploaded twice. `force` discards the stored id, for when the
    server no longer has the file.
    """
    pdf = store.pdf_path(key)
    if not pdf.exists():
        raise FileNotFoundError(f"no PDF stored for {key}; add one before extracting")
    fingerprint = _pdf_fingerprint(pdf)
    upload = store.load_paper(key).get("pdf_upload") or {}
    current = all(upload.get(k) == v for k, v in fingerprint.items())
    if upload.get("file_id") and current and not force:
        return upload["file_id"]
    api = api or client()
    uploaded = api.files.upload(file=pdf)
    with store.paper_lock(key):
        paper = store.load_paper(key)
        paper["pdf_upload"] = {"file_id": uploaded.id, **fingerprint}
        store.save_paper(paper)
    return uploaded.id


def _pdf_block(key: str, force_upload: bool = False) -> dict:
    return {
        "type": "document",
        "source": {"type": "file", "file_id": upload_pdf(key, force=force_upload)},
        # Cache the paper itself: re-extraction after a schema or vocabulary
        # change then re-reads it from cache instead of re-reading the file.
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }


def _instructions(paper: dict, tags: list[dict] | None = None) -> str:
    return f"""\
My research:

{context_block()}

{vocabulary_block(tags)}

{ledger_block()}

The attached PDF is:

  {paper.get('title') or paper['key']}
  {', '.join(paper.get('authors') or []) or 'authors unknown'} ({paper.get('year') or 'year unknown'})

Extract its claims."""


def claim_state(claim: dict) -> str:
    """Everything about a claim a person can change, as a comparable string."""
    return json.dumps({k: v for k, v in claim.items() if k != "id"}, sort_keys=True, default=str)


def extract_paper(key: str, keep_reviewed: bool = True) -> dict:
    """Run extraction and merge the result into the stored paper."""
    # A second re-read starting from the same snapshot cannot be distinguished
    # from claims a person adds while the first call runs: both appear as new
    # ids at merge time. Serialize only extractions across the model call. The
    # paper lock remains short-lived, so manual edits can still land meanwhile.
    with store.extraction_lock(key):
        paper = store.load_paper(key)
        # The claims as they stood when the prompt was built. A re-read is long
        # enough for somebody to correct a claim while it runs, and a correction
        # made after the model saw the paper is newer than anything it returns.
        claims_before = {c["id"]: claim_state(c) for c in paper.get("claims", [])}
        # One read of the vocabulary, used both to build the prompt and to record
        # what the model was shown. Reading it twice would let a tag added between
        # the two reads reach the prompt without reaching the snapshot, and the merge
        # would then be unable to tell it from a name the model invented.
        tags = store.load_tags()
        prompt_tags = {t["name"] for t in tags}
        api = client()
        instructions = {"type": "text", "text": _instructions(paper, tags)}

        def read(pdf_block: dict):
            return api.messages.create(
                model=config.MODEL,
                max_tokens=16000,
                system=SYSTEM,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "high",
                    "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
                },
                messages=[{"role": "user", "content": [pdf_block, instructions]}],
            )

        pdf_block = _pdf_block(key)
        try:
            response = read(pdf_block)
        except (anthropic.BadRequestError, anthropic.NotFoundError) as e:
            # The stored upload can vanish from the server (deleted from the
            # console, or the id belongs to another organization's key). When
            # the error names it, upload the PDF again and read once more.
            file_id = pdf_block.get("source", {}).get("file_id")
            if not file_id or file_id not in str(e):
                raise
            response = read(_pdf_block(key, force_upload=True))
        if response.stop_reason == "refusal":
            detail = getattr(response.stop_details, "explanation", "") or ""
            raise RuntimeError(f"extraction refused for {key}: {detail}")
        payload = json.loads(next(b.text for b in response.content if b.type == "text"))
        return merge_extraction(key, payload, response, keep_reviewed=keep_reviewed,
                                prompt_tags=prompt_tags, claims_before=claims_before)


def merge_extraction(key: str, payload: dict, response=None, keep_reviewed: bool = True,
                     prompt_tags: set[str] | None = None,
                     claims_before: dict[str, str] | None = None) -> dict:
    with store.paper_lock(key):
        return _merge_extraction(key, payload, response, keep_reviewed, prompt_tags, claims_before)


def _merge_extraction(key: str, payload: dict, response=None, keep_reviewed: bool = True,
                      prompt_tags: set[str] | None = None,
                      claims_before: dict[str, str] | None = None) -> dict:
    paper = store.load_paper(key)
    # Derive the id high-water mark from every claim, before the reviewed-only
    # filter below hides some of them. Deriving it from the kept subset would let
    # a discarded claim's id be issued to a new one, and an in-flight retag reply
    # or PATCH for the discarded claim is matched by id alone.
    store.ensure_claim_seq(paper)
    def changed_during_the_call(claim: dict) -> bool:
        # Written or corrected after the model was given the paper: a claim the
        # snapshot does not know, or one whose contents no longer match it.
        if claims_before is None:
            return False
        before = claims_before.get(claim["id"])
        return before is None or before != claim_state(claim)

    kept = [c for c in paper.get("claims", [])
            if (keep_reviewed and c.get("reviewed")) or changed_during_the_call(c)]
    known = set(store.tag_names())

    # Names that were in the vocabulary when the prompt was built and are not in
    # it now were renamed or deleted while the call ran. Writing them onto the
    # fresh claims would undo that mutation, so they are dropped — unless the
    # model listed the name in `proposed_tags`, which means it is putting the
    # name forward rather than echoing the vocabulary back.
    explicitly_proposed = {
        store.slugify(t.get("name", "")) for t in payload.get("proposed_tags", []) if t.get("name")
    }
    retired_during_call = (prompt_tags or set()) - known - explicitly_proposed

    fresh = []
    working = dict(paper, claims=kept)
    for raw in payload.get("claims", []):
        claim = store.new_claim(
            working,
            text=(raw.get("text") or "").strip(),
            kind=raw.get("kind") or "finding",
            strength=raw.get("strength") or "supporting",
            tags=sorted(
                {store.slugify(t) for t in raw.get("tags", []) if t} - retired_during_call
            ),
            evidence=(raw.get("evidence") or "").strip(),
            quote=(raw.get("quote") or "").strip(),
            locator=(raw.get("locator") or "").strip(),
            # Validated against the ledger as it is now, not as it was when the
            # prompt was built: the call is long and the file can change.
            ledger_links=store.clean_ledger_links(raw.get("ledger_links")),
        )
        if not claim["text"]:
            continue
        fresh.append(claim)
        working["claims"] = kept + fresh

    paper["claims"] = kept + fresh
    # `working` is where the ids were allocated; carry the high-water mark back
    # so a later hand-written claim cannot reuse one of them.
    paper["claim_seq"] = working.get("claim_seq", paper.get("claim_seq", 0))
    paper["summary"] = (payload.get("summary") or paper.get("summary") or "").strip()
    paper["relevance"] = (payload.get("relevance") or paper.get("relevance") or "").strip()

    used_tags = {t for claim in paper["claims"] for t in claim["tags"]}
    proposed = {}
    for tag in payload.get("proposed_tags", []):
        name = store.slugify(tag.get("name", ""))
        if name and name not in known:
            proposed[name] = (tag.get("description") or "").strip()
    # A tag used but never declared is still a proposal; surface it rather than
    # letting it become an undocumented tag.
    for name in used_tags - known:
        proposed.setdefault(name, "")
    paper["proposed_tags"] = [{"name": n, "description": d} for n, d in sorted(proposed.items())]

    usage = getattr(response, "usage", None) if response is not None else None
    paper["extraction"] = {
        "model": config.MODEL,
        "at": store.now(),
        "schema_version": config.SCHEMA_VERSION,
        "usage": {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
        } if usage is not None else None,
    }
    store.refresh_status(paper)
    store.save_paper(paper)
    return paper


def retag_paper(key: str) -> dict:
    """Reassign tags for a paper's existing claims against the current vocabulary.

    Used to backfill after adding a tag. Cheaper than a full re-extraction
    because it sends the claim texts rather than the PDF, and it leaves every
    other field, including hand-edited claim text, untouched.
    """
    paper = store.load_paper(key)
    claims = paper.get("claims", [])
    if not claims:
        return paper
    listing = "\n".join(
        f"- {c['id']}: {c.get('text', '')}" + (f" [evidence: {c['evidence']}]" if c.get("evidence") else "")
        for c in claims
    )
    # One read, feeding both the prompt and the snapshot the merge compares
    # against, so a tag added or renamed between the two cannot be mistaken for
    # one the model invented.
    prompt_vocabulary = store.load_tags()
    def state_of(claim: dict) -> tuple:
        # Everything the answer depends on: the text and evidence the model was
        # shown, and the tags its answer would replace.
        return (claim.get("text", ""), claim.get("evidence", ""), sorted(claim.get("tags", [])))

    # Each claim as the prompt described it. Anything that differs afterwards
    # was changed by somebody else while the model thought.
    claims_before = {c["id"]: state_of(c) for c in claims}
    api = client()
    response = api.messages.create(
        model=config.MODEL,
        max_tokens=8000,
        system=(
            "You assign topic tags to research claims. Use only tags from the supplied "
            "vocabulary. Assign every tag that genuinely applies and no others."
        ),
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": RETAG_SCHEMA},
        },
        messages=[{
            "role": "user",
            "content": (
                f"{vocabulary_block(prompt_vocabulary)}\n\n"
                f"Claims from \"{paper.get('title') or key}\":\n{listing}\n\n"
                "Return the tags for each claim id."
            ),
        }],
    )
    payload = json.loads(next(b.text for b in response.content if b.type == "text"))
    by_id = {a["id"]: a for a in payload.get("assignments", [])}

    # Reload before saving: the model call takes long enough that a claim may
    # have been edited or reviewed meanwhile, and saving the object loaded
    # before the wait would discard those corrections. Apply only the tags.
    #
    # The vocabulary is read here rather than before the call, and under its own
    # lock: a rename or delete during the call would otherwise let this save put
    # a removed tag back on a claim. Vocabulary before paper, as everywhere.
    with store.vocab_lock(), store.paper_lock(key):
        known = set(store.tag_names())
        paper = store.load_paper(key)
        for claim in paper.get("claims", []):
            assignment = by_id.get(claim["id"])
            if not assignment:
                continue
            # A claim that changed during the call is left alone. Its tags may
            # have been set by somebody else — a person editing them, or a
            # rename or delete rewriting them — and that decision is newer than
            # this one. Its text may have been rewritten too, in which case the
            # answer describes a claim that no longer exists.
            if state_of(claim) != claims_before.get(claim["id"]):
                continue
            claim["tags"] = sorted(
                {store.slugify(t) for t in assignment.get("tags", []) if store.slugify(t) in known}
            )
        store.save_paper(paper)
    return paper
