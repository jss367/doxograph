"""Read a paper with Claude and return its claims.

The PDF is uploaded once through the Files API and referenced by id, so the
request body stays small no matter how large the paper is (an inlined base64
PDF over about 24 MB exceeds the 32 MB request limit). It goes in first with a
cache breakpoint on it, so re-running extraction against a changed field list
or a grown tag vocabulary re-reads the paper from cache rather than paying for
it again.
"""

from __future__ import annotations

import hashlib
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
    with pdf.open("rb") as contents:
        digest = hashlib.file_digest(contents, "sha256").hexdigest()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": digest}


def _delete_superseded_uploads(key: str, api: anthropic.Anthropic) -> None:
    """Delete old remote PDFs, retaining failures to retry on the next read."""
    pending = list((store.load_paper(key).get("pdf_upload") or {}).get("superseded_file_ids", []))
    deleted = []
    for file_id in pending:
        try:
            api.files.delete(file_id)
        except anthropic.NotFoundError:
            deleted.append(file_id)
            continue
        except anthropic.APIError:
            continue
        deleted.append(file_id)
    if not deleted:
        return
    with store.paper_lock(key):
        paper = store.load_paper(key)
        upload = paper.get("pdf_upload") or {}
        remaining = [file_id for file_id in upload.get("superseded_file_ids", [])
                     if file_id not in deleted]
        if remaining:
            upload["superseded_file_ids"] = remaining
        else:
            upload.pop("superseded_file_ids", None)
        paper["pdf_upload"] = upload
        store.save_paper(paper)


def upload_pdf(key: str, api: anthropic.Anthropic | None = None, force: bool = False) -> str:
    """The Files API id of the paper's PDF, uploading it if none is current.

    The id is kept on the paper with a fingerprint of the file it came from.
    A re-downloaded or replaced PDF then gets a fresh upload; the same file is
    never uploaded twice. `force` discards the stored id, for when the server
    no longer has the file.
    """
    pdf = store.pdf_path(key)
    if not pdf.exists():
        raise FileNotFoundError(f"no PDF stored for {key}; add one before extracting")
    fingerprint = _pdf_fingerprint(pdf)
    upload = store.load_paper(key).get("pdf_upload") or {}
    current = all(upload.get(k) == v for k, v in fingerprint.items())
    if upload.get("file_id") and current and not force:
        if upload.get("superseded_file_ids"):
            _delete_superseded_uploads(key, api or client())
        return upload["file_id"]
    api = api or client()
    uploaded = api.files.upload(file=pdf)
    with store.paper_lock(key):
        paper = store.load_paper(key)
        previous = paper.get("pdf_upload") or {}
        superseded = list(previous.get("superseded_file_ids", []))
        if previous.get("file_id") and previous["file_id"] != uploaded.id:
            superseded.append(previous["file_id"])
        pdf_upload = {"file_id": uploaded.id, **fingerprint}
        if superseded:
            pdf_upload["superseded_file_ids"] = list(dict.fromkeys(superseded))
        paper["pdf_upload"] = pdf_upload
        store.save_paper(paper)
    _delete_superseded_uploads(key, api)
    return uploaded.id


def delete_paper(key: str, api: anthropic.Anthropic | None = None) -> None:
    """Delete a paper's remote uploads before removing its local metadata."""
    with store.extraction_lock(key), store.paper_lock(key):
        try:
            paper = store.load_paper(key)
        except KeyError:
            store.delete_paper(key)
            return
        upload = paper.get("pdf_upload") or {}
        file_ids = list(dict.fromkeys([
            *upload.get("superseded_file_ids", []),
            *([upload["file_id"]] if upload.get("file_id") else []),
        ]))
        if file_ids:
            api = api or client()
        for file_id in file_ids:
            try:
                api.files.delete(file_id)
            except anthropic.NotFoundError:
                pass
        store.delete_paper(key)


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


# --- tensions between papers ---------------------------------------------

TENSION_SYSTEM = """\
You look for disagreements between research claims drawn from different papers.

You are given every claim in one topic, grouped by paper. Return the pairs of
claims, from two different papers, that pull against each other on the same
question.

A "contradiction" is a pair that cannot both be true as stated. A "tension" is
a pair that point in opposite directions but could be reconciled by a
difference in setup: a different model, scale, task, metric, or condition.
Name that difference in the note when you can see it.

Do not flag claims that merely address different aspects of the topic, differ
in emphasis, or report different numbers for different things. Two papers
measuring different quantities are not in tension. A paper that refines or
extends another is not in tension with it. When in doubt, leave the pair out:
an empty list is a good answer for a topic where everyone agrees.

Each note is one or two sentences, in plain language, saying what the two
claims disagree about and, for a tension, what might account for it. Refer to
papers by author and year, not by claim id."""

TENSION_SCHEMA = {
    "type": "object",
    "properties": {
        "tensions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claims": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "The ids of the two claims, from different papers.",
                    },
                    "kind": {"type": "string", "enum": store.TENSION_KINDS},
                    "note": {
                        "type": "string",
                        "description": "What they disagree about, and what might account for it.",
                    },
                },
                "required": ["claims", "kind", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tensions"],
    "additionalProperties": False,
}


def _tension_listing(rows: list[dict], mark_unreviewed: bool = False) -> str:
    """Claims grouped by paper, as the cross-paper passes show them. With
    `mark_unreviewed`, a claim nobody has checked is labelled so the model can
    weigh it accordingly; the fingerprint the merges compare does not include
    that flag, so reviewing a claim does not make a tension stale."""
    by_paper: dict[str, list[dict]] = {}
    for row in rows:
        by_paper.setdefault(row["paper"], []).append(row)
    blocks = []
    for key, claims in by_paper.items():
        first = claims[0]
        authors = first.get("paper_authors") or []
        head = f"{authors[0].split()[-1] if authors else key}"
        if len(authors) > 1:
            head += " et al."
        head += f" ({first.get('paper_year') or 'n.d.'}): {first.get('paper_title') or key}"
        lines = [f"## {head}"]
        for claim in claims:
            line = f"- {claim['id']} [{claim.get('kind', 'finding')}]: {claim.get('text', '')}"
            if mark_unreviewed and not claim.get("reviewed"):
                line += " (unreviewed extraction)"
            if claim.get("evidence"):
                line += f"\n  evidence: {claim['evidence']}"
            lines.append(line)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def find_tensions(topic: str, rows: list[dict] | None = None) -> dict:
    """Ask the model which claims in `topic` disagree, and record the answer.

    Cheap in the way retag is cheap: it sends claim text rather than PDFs. One
    call per topic, and only topics with claims from at least two papers are
    worth a call; `store.tension_topics` lists them.
    """
    rows = [r for r in (rows if rows is not None else store.claim_rows()) if topic in r.get("tags", [])]
    papers = {r["paper"] for r in rows}
    if len(papers) < 2:
        return {"added": 0, "reopened": 0, "kept": 0, "returned": 0}
    description = next((t.get("description", "") for t in store.load_tags() if t["name"] == topic), "")
    # The claims as the prompt shows them, keyed by id. The merge uses this to
    # drop ids the model invented; staleness against later edits is judged
    # separately, from the fingerprints the merge records.
    shown = {r["id"]: r for r in rows}
    api = client()
    response = api.messages.create(
        model=config.MODEL,
        max_tokens=8000,
        system=TENSION_SYSTEM,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": TENSION_SCHEMA},
        },
        messages=[{
            "role": "user",
            "content": (
                f"My research:\n\n{context_block()}\n\n"
                f"Topic: {topic}" + (f" — {description}" if description else "") + "\n\n"
                f"Claims, by paper:\n\n{_tension_listing(rows)}\n\n"
                "Return the pairs of claims from different papers that are in tension."
            ),
        }],
    )
    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "explanation", "") or ""
        raise RuntimeError(f"tension pass refused for {topic}: {detail}")
    payload = json.loads(next(b.text for b in response.content if b.type == "text"))
    found = payload.get("tensions", [])
    result = store.record_tensions(topic, found, shown)
    result["returned"] = len(found)
    return result


# --- what the papers hold, by topic ---------------------------------------

SYNTHESIS_SYSTEM = """\
You write the state of one question from research claims drawn from several
papers.

You are given every claim in one topic, grouped by paper, and any disagreements
between them that have already been noted. Write what the papers, taken
together, hold on the topic: where they agree, where they differ and what might
account for that, and what none of them settles. Say what the evidence is, not
merely that some exists: name the models, scales, metrics and effect sizes that
carry the weight.

Stay inside the claims you are given. Do not add findings from your own
knowledge, do not soften or sharpen a claim beyond how it is written, and do not
present a claim marked as an unreviewed extraction as settled. Refer to papers
by author and year in the prose, and cite the claims each sentence rests on by
putting their ids in square brackets at the end of the sentence, like
[doe2026recovery-c3] or [doe2026recovery-c3, li2025steer-c1]. Every sentence
that states a finding carries at least one citation.

One to three short paragraphs, in plain language, with no headings and no
bullet points. Where the papers are few or thin, say less."""

SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "The synthesis, one to three paragraphs separated by blank lines, "
                           "with claim ids in square brackets as citations.",
        },
    },
    "required": ["text"],
    "additionalProperties": False,
}


def _tension_block(topic: str, rows: list[dict]) -> str:
    """The disagreements already on file for this topic, for the synthesis
    prompt. Dismissed ones are left out: the reviewer has said there is
    nothing there. The status is given so a confirmed one carries more weight
    than one nobody has looked at."""
    noted = [t for t in store.tension_rows(rows)
             if topic in t.get("topics", []) and t.get("status") != "dismissed"]
    if not noted:
        return "No disagreements between these claims have been noted yet."
    lines = ["Disagreements already noted, with whether a reviewer has confirmed them:"]
    for t in noted:
        a, b = t["claims"]
        lines.append(f"- [{t['status']}] {t['kind']} between {a['id']} and {b['id']}: {t.get('note', '')}")
    return "\n".join(lines)


def synthesize_topic(topic: str, rows: list[dict] | None = None) -> dict:
    """Ask the model what the papers hold on `topic`, and record the answer.

    Cheap in the way the tensions pass is cheap: one call per topic, claim
    text rather than PDFs. Any topic with a claim can be synthesized; the
    default set, `store.synthesis_topics`, is those with two papers or more,
    since one paper's claims add up to little.

    Returns `{"written": bool, "claims": n, "papers": n}`. `written` is False
    when the topic had no claims, or when it lost them all (or its name) while
    the model was thinking, or when its synthesis was corrected by hand or
    deleted meanwhile: that decision is newer than the answer and stands.
    """
    all_rows = rows if rows is not None else store.claim_rows()
    rows = store.topic_claims(topic, all_rows)
    papers = {r["paper"] for r in rows}
    if not rows:
        return {"written": False, "claims": 0, "papers": 0}
    description = next((t.get("description", "") for t in store.load_tags() if t["name"] == topic), "")
    shown = {r["id"]: r for r in rows}
    # The record on file as the call starts, so the write can tell whether a
    # reviewer edited or deleted it while the model was thinking.
    before = store.load_syntheses().get(topic)
    api = client()
    response = api.messages.create(
        model=config.MODEL,
        max_tokens=8000,
        system=SYNTHESIS_SYSTEM,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": SYNTHESIS_SCHEMA},
        },
        messages=[{
            "role": "user",
            "content": (
                f"My research:\n\n{context_block()}\n\n"
                f"Topic: {topic}" + (f" — {description}" if description else "") + "\n\n"
                f"Claims, by paper:\n\n{_tension_listing(rows, mark_unreviewed=True)}\n\n"
                f"{_tension_block(topic, all_rows)}\n\n"
                "Write what these papers, taken together, hold on the topic."
            ),
        }],
    )
    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "explanation", "") or ""
        raise RuntimeError(f"synthesis refused for {topic}: {detail}")
    payload = json.loads(next(b.text for b in response.content if b.type == "text"))
    record = store.record_synthesis(topic, payload.get("text", ""), shown, before=before)
    return {"written": record is not None, "claims": len(rows), "papers": len(papers)}
