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

Once several papers share a topic, a second pass writes what they hold on it,
and a third finds where they disagree:
pairs of claims from different papers that pull against each other on the same
question, each with a note on what the disagreement is and what might account
for it. These are reviewed too, and the confirmed ones go into the export.

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
doxograph tensions                    # find claims from different papers that disagree
doxograph tensions --list             # show what has been found, without calling the model
doxograph synthesize                  # write what the papers hold on each topic
doxograph synthesize --list           # show the syntheses on file
doxograph list                        # what is in the corpus
doxograph tags                        # topics and their use counts
doxograph export --out notes.html     # one self-contained HTML file
doxograph bibtex --out refs.bib
```

`add`, `extract`, and `retag` exit nonzero if any reference or paper they were
asked to handle failed, so a script can tell a partial run from a clean one. A
paper that arrives without its PDF counts as a failure and says so; running
`add` on it again retries the download and then reads it.

A landing-page URL is identified from the page's own metadata (`citation_arxiv_id`,
the canonical link, `citation_doi`, `citation_pdf_url`). An arXiv link in a
bibliography is not treated as the page's identity, since that would ingest a
cited paper instead of the one you pasted. When nothing identifies the page,
paste the arXiv ID, the DOI, or a direct PDF link.

In the web app: drop PDFs anywhere on the page, or paste references into the
box. The Tensions entry in the sidebar shows where papers disagree; `Escape`
returns to the claims. `j` and `k` move between claims, `e` edits the selected one, `r` marks it
reviewed, `Escape` cancels. Clicking a topic filters to it.

"Add claim by hand" opens an editor that exists only in the browser; the claim
is created when you save it, so cancelling, filtering it away, or switching
papers leaves nothing behind. If you open another claim's editor while writing
one, the draft is held and a line at the top of the list offers to resume or
discard it. Leaving the paper abandons it, since the draft belongs to that paper. A proposed topic is added to the vocabulary only
by Accept; Discard just clears the proposal.

## The Mac app

```
native/build.sh --install     # ~/Applications/Doxograph.app
```

A window and a Dock icon for the same server: drop a paper on the icon and it
goes into the corpus, with no terminal and no browser tab. The app runs the
`doxograph` you installed rather than bundling its own copy, so both read one
corpus and there is no second implementation to keep in step. It needs the Xcode
command line tools to build, and it adopts a `doxograph serve` that is already
running instead of starting a second one. See `native/README.md`.

Launched from the Dock it inherits none of your shell environment, so put the
API key in `~/.credentials` rather than exporting it in `.zshrc`.

## The corpus on disk

Everything lives in `~/doxograph-data` by default; set `DOXOGRAPH_DATA` to move
it. The corpus is deliberately outside this repository, since it holds
downloaded PDFs and in-progress notes.

```
papers/<key>.json    one paper and its claims
pdfs/<key>.pdf       the paper itself
locks/               lock files, so two processes do not write at once
retired-keys.json    keys of removed papers, never issued again
tags.yaml            the topic vocabulary
tensions.json        where papers disagree, and what you decided about each
syntheses.json       what the papers hold on each topic
ledger.yaml          your own claims, for linking against
context.md           what your research is about, given to the extractor
export/              generated HTML
```

One file per paper, so every change to a claim is a readable diff. If you want
version history for the corpus, `git init` inside the data directory.

Writes are serialized per paper and keys are claimed atomically, so the upload
pool and the review UI can touch the corpus at the same time without one
overwriting the other. The serialization spans processes as well as threads, via
lock files under `locks/`, so running `doxograph extract` in a shell while
`doxograph serve` is up is safe. Locking needs `fcntl` or `msvcrt`; on a platform
with neither, doxograph refuses to run rather than pretend the corpus is
protected. The Windows path is implemented but untested.

A removed paper's key is retired rather than reused, so a key names one paper for
all time. Re-adding a paper you deleted therefore gets a suffixed key. That is
the point: a slow operation carrying a key — an extraction job, a download, a
queued edit — never has to work out which paper it meant.

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

## Where papers disagree

A doxography is only interesting where the recorded opinions differ, so
Doxograph looks for that directly. `doxograph tensions`, or Find tensions in the
web app, takes each topic with claims from at least two papers and asks the
model which pairs of claims pull against each other. It sends claim text, not
PDFs, so the pass is about as cheap as a retag.

Each pair is a *contradiction* (both cannot be true as stated) or a *tension*
(they point opposite ways but a difference in model, scale, task, or metric
might reconcile them), with a note saying what the disagreement is. They start
open. Confirm the ones that hold up and dismiss the rest; a claim card marks
the tensions it is part of, and clicking the mark shows them.

Tensions live in `tensions.json`. A repeat run leaves a decision alone: a pair
you dismissed stays dismissed however many times the model proposes it. If you
edit either claim afterward, the tension is marked as judged against old text;
a later run that returns the same pair re-judges it and sets it back to open,
and you can also confirm or dismiss it yourself against the new text. Deleting a claim
removes its tensions. Open and confirmed tensions appear in the HTML export;
dismissed ones do not.

## What the papers hold

Where tensions record the disagreements, a synthesis records the rest: for one
topic, a short account of what the papers, taken together, hold. `doxograph
synthesize`, or Synthesize topics in the web app, takes each topic with claims
from at least two papers and asks the model for one to three paragraphs on
where the papers agree, where they differ and why, and what none of them
settles. Only the claims are sent, along with the tensions already noted for
the topic, so the pass costs about what a retag does. Name a topic to write one
for a topic with a single paper.

Each sentence cites the claims it rests on. In the web app the citations link
to the claim cards; in the export they show the cited claim on hover. The
model is told to stay inside the claims, and which of them nobody has reviewed
yet, so a synthesis is only as good as the review behind it.

Syntheses live in `syntheses.json`, one per topic. Edit one in the web app to
correct it by hand. A synthesis goes stale when a claim in its topic is added,
removed, or edited, or a tension in it is found, confirmed, or dismissed; it
stays on view marked as such until you rewrite it or edit it yourself, either
of which is a judgment against the current claims and tensions. Marking a
claim reviewed does not stale it: the review changes nothing the claim says.
An edit or deletion made while a rewrite is still running stands: the answer
that arrives afterwards is discarded rather than written over it.
Renaming a topic carries its synthesis along; deleting the topic deletes it.
Syntheses appear under their topic in the HTML export.

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
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest
```

The suite is offline: its browser smoke test runs against a temporary local
server and corpus. It covers reference parsing, the store and its status
transitions, tag renaming and deletion across claims, the extraction merge
including the keep-reviewed path, HTML escaping in the export, and BibTeX.
Nothing in it calls the API.
