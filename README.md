# doxograph

Extract claims from papers and browse them by topic.

A doxography is a compilation of the recorded opinions of earlier thinkers,
arranged so you can see what everyone held about a given question. Doxograph
does that for a pile of papers. Each paper is decomposed into individual claims,
each claim carries topic tags and the verbatim quote that supports it, and the
views are organized by topic rather than by paper.

The unit of storage is the claim, not a row in a fixed-column table. Papers that
test unrelated things still land in the same structure, and a paper that
contributes one definition sits next to a paper that contributes six results
without either leaving empty cells behind.

## What it does

Paste an arXiv ID or drop a PDF. Doxograph fetches the paper and its metadata,
reads it with Claude, and records:

- a paragraph on what the paper does and a line on why it is in your pile
- each claim it makes, as a standalone sentence in the paper's own scope
- what kind of claim it is: a finding, a method, a definition, a negative
  result, or a conjecture
- how it was measured: models, sample sizes, metrics, effect sizes
- the verbatim sentence that supports it, and where in the paper it appears
- topic tags drawn from a vocabulary you control
- how it bears on your own claims: supports, contradicts, supplies a method
  for, refines, or is independent of them

Then you review. Extraction gets claims subtly wrong — wording that is too
strong, a result attributed to the wrong condition, a missing caveat — so every
claim starts unreviewed and the web app is built for correcting them quickly.

## Install

```
python -m venv .venv && .venv/bin/pip install -e .
```

Doxograph needs an Anthropic API key. It reads `ANTHROPIC_API_KEY`, then falls
back to an `ANTHROPIC_API_KEY=` line in `~/.credentials`, then to whatever the
Anthropic SDK resolves on its own (including an `ant auth login` profile).

## Use

```
doxograph serve                       # the web app on http://127.0.0.1:8765
doxograph add 2602.06941              # arXiv ID, arXiv URL, DOI, PDF URL, or a local PDF
doxograph add --no-extract paper.pdf  # fetch now, read later
doxograph extract                     # read every paper that has no claims yet
doxograph retag                       # reassign topics against the current vocabulary
doxograph list                        # what is in the corpus
doxograph tags                        # topics and their use counts
doxograph export --out notes.html     # one self-contained HTML file
doxograph bibtex --out refs.bib
```

In the web app: drop PDFs anywhere on the page, or paste references into the
box. `j` and `k` move between claims, `e` edits the selected one, `r` marks it
reviewed, `Escape` cancels. Clicking a topic filters to it.

"Add claim by hand" opens an editor on a blank draft; cancelling removes the
draft rather than leaving an empty claim behind. A proposed topic is added to
the vocabulary only by Accept — Discard just clears the proposal.

## The corpus on disk

Everything lives in `~/doxograph-data` by default; set `DOXOGRAPH_DATA` to move
it. The corpus is deliberately outside this repository, since it holds
downloaded PDFs and in-progress notes.

```
papers/<key>.json    one paper and its claims
pdfs/<key>.pdf       the paper itself
tags.yaml            the topic vocabulary
ledger.yaml          your own claims, for linking against
context.md           what your research is about, given to the extractor
export/              generated HTML
```

One file per paper, so every change to a claim is a readable diff. If you want
version history for the corpus, `git init` inside the data directory.

### tags.yaml

```yaml
tags:
  - name: activation-steering
    description: "Adding a direction to activations to change behavior."
```

Quote any description containing a colon.

The vocabulary is the part that compounds. Extraction is given the current tag
list and told to prefer it, and any tag it uses that you have not declared is
recorded as a *proposal* rather than silently becoming a tag. Accept the
proposals worth keeping in the web app. When you add a tag later, `doxograph
retag` reassigns topics on the papers you already have, which is much cheaper
than re-reading them and leaves hand-edited claim text alone.

### ledger.yaml

```yaml
claims:
  - id: L1
    text: >-
      Recovery under steering is a path-dependent outcome.
```

These are your own claims. Extraction links a paper's claims to them, which is
what turns the corpus into an answer to "what external evidence do I have for
this" rather than a pile of notes.

### context.md

Free text describing your research and what makes a paper relevant. It goes
into the extraction prompt and is the main lever on the quality of the
`relevance` line and the ledger links.

## Re-reading a paper

`doxograph extract --all` re-reads papers that already have claims. Claims you
have marked reviewed are kept by default and the rest are replaced; pass
`--replace-reviewed` to discard yours too.

The PDF is sent with a cache breakpoint on it, so a second read of the same
paper within the hour hits the prompt cache. On a 77k-token paper that is the
difference between paying for 77k input tokens and paying for about a tenth of
that.

## Model

`claude-opus-5` by default; set `DOXOGRAPH_MODEL` to change it. Extraction uses
adaptive thinking at high effort and a JSON schema, so the response is validated
structurally before it reaches the store.

## Tuning

The extraction prompt is `SYSTEM` in `doxograph/extract.py` and the field
descriptions are the schema right below it. Both are read by the model
verbatim, so that is where to push if claims come out too coarse, too numerous,
or too confidently worded. The retagger has its own, shorter prompt in
`retag_paper`; if it assigns a topic to nearly every claim, tighten that one.

## Tests

```
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

The suite is offline. It covers reference parsing, the store and its status
transitions, tag renaming and deletion across claims, the extraction merge
including the keep-reviewed path, HTML escaping in the export, and BibTeX.
Nothing in it calls the API.
