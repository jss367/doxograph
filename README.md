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
a third finds where they agree, and a fourth finds where they disagree. An
agreement is a group of claims from two or more papers that assert the same
finding, with a count of how many papers make it, which is the answer to "how
much evidence do I have for this". A tension is the opposite:
pairs of claims from different papers that pull against each other on the same
question, each with a note on what the disagreement is and what might account
for it. These are reviewed too, and the confirmed ones go into the export.

Then you review. Extraction gets claims subtly wrong — wording that is too
strong, a result attributed to the wrong condition, a missing caveat — so every
claim starts unreviewed and the web app is built for correcting them quickly.

What you think is yours to write. A paper takes a note and so does a claim,
in your own words, and nothing in the program reads either back to a model.
See [Notes of your own](#notes-of-your-own).

One error the machine can catch on its own: a quote that is not in the paper.
Every quote is checked against the PDF's text when it is extracted or edited,
on letters and digits alone so line breaks and hyphenation do not count against
it, and a quote that is not found is flagged on the claim. `doxograph verify`
runs the check over a corpus extracted before it existed.

The check knows where in the paper it looked, so the app can show you. Under
any quote, **in the paper** opens the passage the check matched it against:
the sentence as the paper writes it, the text either side, the page it is on,
and the words that differ from the quote on the claim. A quote the model
reworded is corrected with one click on **Use the paper's wording**, which is
an ordinary edit and re-runs the check. The page it was really found on sits
next to the locator the model wrote, so a claim pointing at the wrong page
says so. None of this calls the API.

## Install

```
python -m venv .venv && .venv/bin/pip install -e .
```

Doxograph needs an Anthropic API key. It reads `ANTHROPIC_API_KEY`, then falls
back to an `ANTHROPIC_API_KEY=` line in `~/.credentials`, then to whatever the
Anthropic SDK resolves on its own (including an `ant auth login` profile).

To turn off model use, open **Settings** and uncheck **Enable AI analysis**.
The preference survives restarts and applies to every workspace and the CLI
using the same data directory. While off, imports skip extraction, and
extraction, retagging, agreements, tensions, and synthesis cannot start new
model calls or upload PDFs for analysis. Requests already sent may finish.
Browsing, manual editing, quote checks, and exports remain available. Removing
a paper still deletes any previously uploaded copy from Anthropic.

## Use

```
doxograph serve                       # the web app on http://127.0.0.1:8765
doxograph add 2602.06941              # arXiv ID, arXiv URL, DOI, PDF URL, or a local PDF
doxograph add --no-extract paper.pdf  # fetch now, read later
doxograph extract                     # read every paper that has no claims yet
doxograph retag                       # reassign topics against the current vocabulary
doxograph verify                      # check that every quote is in its paper's PDF
doxograph search steering recovery    # find words in the papers' own text
doxograph cites                       # which papers cite which, from their reference lists
doxograph tensions                    # find claims from different papers that disagree
doxograph tensions --list             # show what has been found, without calling the model
doxograph tensions --force            # ask again about topics nothing has changed in
doxograph agreements                  # find claims from different papers that say the same thing
doxograph agreements --force          # ask again about topics nothing has changed in
doxograph synthesize                  # write what the papers hold on each topic
doxograph synthesize --force          # write again for topics nothing has changed in
doxograph synthesize --list           # show the syntheses on file
doxograph list                        # what is in the corpus
doxograph tags                        # topics and their use counts
doxograph label                       # labels in use, and how many papers carry each
doxograph label <key> open-source     # label one paper, replacing the labels it has
doxograph export --out notes.html     # one self-contained HTML file
doxograph bibtex --out refs.bib
```

`add`, `extract`, and `retag` exit nonzero if any reference or paper they were
asked to handle failed, so a script can tell a partial run from a clean one.
`extract`, `retag`, `tensions`, `agreements` and `synthesize` run four model
calls at a time; set `DOXOGRAPH_PASS_WORKERS` to change that, in the shell or
for the web app. A
paper that arrives without its PDF counts as a failure and says so; running
`add` on it again retries the download and then reads it.

A landing-page URL is identified from the page's own metadata (`citation_arxiv_id`,
the canonical link, `citation_doi`, `citation_pdf_url`). An arXiv link in a
bibliography is not treated as the page's identity, since that would ingest a
cited paper instead of the one you pasted. When nothing identifies the page,
paste the arXiv ID, the DOI, or a direct PDF link.

The search box takes a phrase or a set of words. Two characters are enough to
ask the papers — `AI` and `RL` are words here — and a query in a script that
writes without spaces is asked whatever its length. If the corpus holds what you
typed as a phrase, that is what it finds; otherwise every word has to be there,
in any order and at the start of a word, so `steering recovery` finds a claim
that says "recovery under steering". Under the claims it matched, **In the
PDFs** lists papers whose own text holds every word, best first, with the
passages and their pages — which finds a word no claim mentions, and a paper
nothing has been read out of yet. `doxograph search <words>` does the same from
the shell. It reads the text already extracted for the quote checks; no model
and no network are involved.

In the web app: drop PDFs anywhere on the page, or paste references into the
box. Use the workspace picker in the header to keep unrelated research
separate. The existing corpus is the Default workspace; creating a workspace
makes an empty, independent corpus with its own papers, topics, research
context, ledger, tensions, syntheses, and exports. The last selected workspace
is restored when the page is reopened, unless the address names another one.
Papers dropped on the Mac app's Dock icon go to the workspace currently shown
in its window.

Export HTML writes the file and then offers to open it or to copy its path. A
reference that nothing identifies stays in the box and is named underneath it,
so it is clear which line of a pasted list is the problem.

The Tensions entry in the sidebar shows where papers disagree; `Escape`
returns to the claims. The Map entry draws the papers as a graph: each
paper is a circle sized by its claims and coloured by its most frequent
topic, papers whose claims share a topic are joined, a paper that cites
another is joined to it by an arrow, tensions are drawn
in the warning colour, and your own claims appear as squares joined to the
papers that support, contradict, refine, or supply a method for them; a
link marked independent is not drawn. A slider hides topic
links below a chosen number of shared claims; it starts at the median so
a corpus on one subject is not a hairball. Drag nodes, scroll to zoom,
and click a paper to read it. Clicking a topic filters to it.

Review runs from the keyboard. `j` and `k` move between claims, `e` edits the
selected one, `r` marks it reviewed and moves to the next, and `n` jumps to the
next claim nobody has reviewed, wrapping round the end. `/` puts the cursor in
the search box and `Escape` there clears it; `Escape` anywhere else cancels an
editor or leaves the view you are in. `?` lists all of this, and so does the
Shortcuts button in the header. A paper that arrives with a dozen claims you
have read in one go has "Mark N reviewed" in its header; the notice that
follows offers to take it back, and takes back only what that click changed.

Deleting a claim, an agreement, or a synthesis is not confirmed first. It is
undone instead: the row leaves the page at once and the request is held until
its notice fades, so Undo is the delete never being sent and the claim comes
back as itself, with the tensions and syntheses that cite it intact. Removing a
paper is the exception and still asks, because a removed paper's key is retired
for good. A claim carries a link to its own page of the PDF when its locator
names one, and a quote has a copy button, so checking a quote against the paper
does not mean going looking for the paper first.

The URL follows what is on screen: the workspace, the paper or topic you are
in, the view, the query, and the filters, including the status a tension or
agreement list is narrowed to. A reload lands where you left off,
Back walks the papers and topics you went through rather than every keystroke,
and a link to a topic is something you can keep or send.

The search box narrows the claims and the paper list together. It matches
claim text, evidence, quotes, topics, paper titles, authors, keys, and
years, so a word from a title finds the paper even when nothing has been
extracted from it yet, and the paper list says how many of the corpus
matched. A claim shows
its paper as a surname and a year; hovering that citation gives the title,
and clicking it opens the paper.

"Add claim by hand" opens an editor that exists only in the browser; the claim
is created when you save it, so cancelling, filtering it away, or switching
papers leaves nothing behind. If you open another claim's editor while writing
one, the draft is held and a line at the top of the list offers to resume or
discard it. Leaving the paper abandons it, since the draft belongs to that paper. A proposed topic is added to the vocabulary only
by Accept; Discard just clears the proposal.

## Research without AI

All of these tools work with **Enable AI analysis** turned off. Your boards,
connections, reading progress, and saved searches belong to the selected workspace
and are stored in `notebook.json`. They are never sent to a model.

- **Read & capture**, in a paper's header, opens an offline PDF reader. Select a
  passage on the page and choose **Capture selected passage** to fill the quote
  and page fields. Write the claim in your own words, optionally add a note, and
  save. Quotes receive the same local verification as ordinary manual claims.
  Pages with no selectable text can still be read and transcribed by hand.
- **Compare & organize** uses the checkboxes on claim cards and beside paper
  titles. Selections stay pinned across filters and navigation, and survive a
  reload in the same browser tab. Compare sources, quotes, evidence, and notes
  side by side. Clear or unpin items when finished.
- **Claim connections** lets you connect two selected claims as agreeing,
  contradicting, or qualifying one another and write your explanation. A
  qualification runs from the first claim to the second. These manual judgments
  are separate from model-generated agreements and tensions.
- **Evidence boards** collect selected claims in an order you choose. Add
  headings and commentary, move entries with the arrow buttons, and save the
  board. Source claims remain linked to their papers; if a source is removed,
  the board marks it as missing and keeps your commentary.
- **Saved searches** remember the current query, paper, topic, label, claim kind,
  review and quote-verification filters, and grouping. Opening one runs those
  filters against the current corpus, so new matching claims appear naturally.
- **Reading queue** keeps an ordered list of papers with queued, reading, and
  finished states, independently of claim review. The reader saves your page
  position; bookmark pages and resume them from the queue.
- **Bulk organization**, under Compare & organize, adds, removes, or replaces
  topics on selected claims and labels on their papers. Explicitly selected
  papers can have topics changed across all their claims. New topics must first
  be added to the vocabulary. Replacing values asks before removing existing ones.
- **Export Markdown** and **Export CSV** download the selected claims or papers,
  or a whole evidence board, with citations, quotes, locations, evidence, and
  notes. Board exports retain entry order, headings, and commentary. HTML and
  BibTeX export remain in the main toolbar.
- **Edit history**, on a paper, and **history**, on a claim, show earlier claim
  wording and paper notes alongside their current versions. Restore an earlier
  version without changing other claims. History starts with edits made in this
  version of the app; it is stored with the paper and removed with that paper.
  Restoring omits historical topics that are no longer in the vocabulary and
  reports which ones were omitted; existing claims keep their current review decision.
  Restoring a deleted claim also keeps its deletion in history, so the restoration
  can itself be undone.

Open research-tool forms keep unsaved writing until you save or explicitly
choose to discard it. If another window changes the same workspace's research
tools, a save reports the conflict and keeps your form. **Reload saved version**
loads that window's changes after asking about your unsaved work.

## The Mac app

```
native/build.sh --install     # ~/Applications/Doxograph.app
```

A window and a Dock icon for the same server: drop a paper on the icon and it
goes into the corpus, with no terminal and no browser tab. The app runs the
`doxograph` you installed rather than bundling its own copy, so both read one
corpus and there is no second implementation to keep in step. It needs the Xcode
command line tools to build, and it adopts a `doxograph serve` that is already
running instead of starting a second one — unless that server reports a different
version, which it asks about rather than running on. See `native/README.md`.

Launched from the Dock it inherits none of your shell environment, so put the
API key in `~/.credentials` rather than exporting it in `.zshrc`.

## The corpus on disk

Everything lives in `~/doxograph-data` by default; set `DOXOGRAPH_DATA` to move
it. The corpus is deliberately outside this repository, since it holds
downloaded PDFs and in-progress notes.

For compatibility, the Default workspace uses the data-directory root shown
below. Named workspaces repeat that same structure under
`workspaces/<workspace-id>/`; `workspaces.json` records their display names.
Command-line commands operate on the Default workspace.

```text
workspaces.json       named workspace registry
workspaces/<id>/      an independent named corpus
papers/<key>.json    one paper, its labels, and its claims
pdfs/<key>.pdf       the paper itself
text/<key>.txt       its text, extracted once and reused (pages split by \f)
locks/               lock files, so two processes do not write at once
retired-keys.json    keys of removed papers, never issued again
tags.yaml            the topic vocabulary
tensions.json        where papers disagree, and what you decided about each
syntheses.json       what the papers hold on each topic
ledger.yaml          your own claims, for linking against
context.md           what your research is about, given to the extractor
export/              generated HTML
```

The text under `text/` is a cache: delete it and the next quote check writes
it again from the PDF. It is what the quote checks, the passage shown beside a
claim, the page numbers and the search over the papers are all read out of, so
a paper is parsed once rather than once per claim. It is written when a PDF
arrives, so the first search after an import does not have to wait for it.

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

### Labels on papers

A topic is about a claim and comes out of the model. A label is about the
paper and is put on by hand: that it is open source, that a model of it is on
HuggingFace, that it is worth rereading. **Labels** in a paper's header opens
a field for them, separated by spaces or commas, and they appear as a Labels
list in the sidebar that filters the corpus to the papers carrying one.

They are deliberately not part of the topic vocabulary. The vocabulary goes
into the extraction prompt as the list of things to look for in a paper's
text, and nothing in a paper's text says it is on your reading list. So no
pass writes a label, `retag` cannot take one off, and labelling a paper does
not stale its syntheses. Labels are slugs: several words are written with
hyphens, as topics are.

Labels are searched along with everything else, so `huggingface` in the search
box finds the papers carrying it and their claims. `doxograph label` reads and
sets them from the shell. They live on the paper in `papers/<key>.json`,
under `labels`.

### ledger.yaml

```yaml
claims:
  - id: L1
    text: >-
      Recovery under steering is a path-dependent outcome.
```

These are your own claims. Extraction links a paper's claims to them, which is
what turns the corpus into an answer to "what external evidence do I have for
this" rather than a pile of notes. The file can be edited by hand or from the
web app, under *What I am studying* in the sidebar, which edits the research
context in the same form.

### context.md

Free text describing your research and what makes a paper relevant. It goes
into the extraction prompt and is the main lever on the quality of the
`relevance` line and the ledger links.

## Which papers cite which

The map draws a citation as an arrow from a paper to the paper it cites. They
come from the reference lists in the PDFs: the text after the first
"References" heading — a paper with a supplement has two, and the article's own
is the first — is searched for every other paper's arXiv id, DOI, or title, and
a hit is an edge. Where one title sits inside another, only the longer counts,
so a reference to "Attention is all you need for image restoration" is not also
read as a citation of "Attention is all you need". A title has to be at least twenty letters long to
be looked for, since a short one turns up in prose that is not a citation of
it. Nothing is fetched from arXiv or Crossref, and a paper no model has read
is linked like any other.

A citation is a stronger link than a shared tag, so the topic line between two
papers gives way to the arrow between them. Turn the layer off with
**citations** above the map. `doxograph cites` lists the same thing in the
shell.

A numbered list is read one entry at a time, and an entry names one work: the
longest title in it wins, so a paper whose title contains another's is cited
rather than both, and an identifier beats a title, so a preprint and its
published version are told apart by the DOI or arXiv id the entry carries. A
list that does not number its entries is read whole.

A heading is built out of heading words and has to say one of the words that
names a bibliography, so "References and Notes", "Selected Bibliography" and
"Works Cited" are all found, while "References to Figure 2 show" is prose. A paper whose PDF has no
"References" heading at all contributes no citations. That
is the conservative reading: guessing where the list starts would invent
links.

## Asking again

A pass over a topic is one model call, and running `doxograph tensions`,
`agreements`, or `synthesize` again over a corpus you have not touched used to
pay for every one of them to arrive at what is already on file. Each pass now
records what it was asked — which claims were in the topic and what each one
said, along with the topic's description and your research context — and skips
a topic whose answer could not have changed. Add a paper, edit a claim, rename
a topic, or rewrite the research context and that topic is asked about again.
Changing `DOXOGRAPH_MODEL` counts too: a different model is a different answer.
`--force` asks regardless, and is also how to rewrite a synthesis after editing
the research context, which is not part of what makes one stale. **Rewrite** on
a synthesis forces it too: it is an instruction, not a request. Bumping
`PASS_VERSION` in `config.py` after changing one of these prompts puts every
topic back in the queue once.

This costs nothing in findings: a pair the model does not return is kept
anyway, so a repeat call over unchanged claims could only re-derive what is
there. Skipping also leaves a synthesis you corrected by hand alone, where a
rerun used to write over it.

## Notes of your own

A claim is the machine's sentence about the paper. A note is yours, and it is
the only thing here no model writes and no model reads.

A paper's note sits under the summary in its header: **Write a note** opens a
box, Save puts it on the paper, and it is there the next time you open it.
Write what the paper is for, what to read it for, what is wrong with it, what
to do about it — whatever the extraction could not have told you. A claim's
note is a field in its editor, under the links to your own claims: press `e`,
write it, Save. It shows under the claim on its card, marked *my note*, so
what you wrote is never mistaken for what the model read out.

Both are searched along with everything else, so a word only you wrote finds
the claim or the paper it is on. Both go into the HTML export under the claim
and the paper they belong to. Neither is part of what makes a topic stale, so
writing one never puts a topic back in the queue for a tensions, agreements or
synthesis pass: a note costs nothing and changes nothing the model was asked.

A paper also has an `error`, which is the other thing `notes` used to hold:
why the ingest has no PDF. That one is the program's to write, so a note means
one thing. In a corpus written before the split, an old `notes` that begins
with the ingest's own `PDF download failed:` is read as the `error` it was;
anything else there is left as the note it is, since the API accepted `notes`
then and a corpus driven from a script may hold a real one.

## Claims worded alike

**alike** on a claim lists claims from other papers that use the same words:
each claim is its content words weighted by how rare they are across the
corpus, and the ones that overlap most come back first. It is a reading aid
and nothing else. It does not decide what the tensions and agreements passes
are shown — two claims can contradict each other in words that have nothing in
common, "recovers in 46% of rollouts" against "almost never returns to the
task", and filtering the prompt by word overlap would quietly throw those
away.

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
`--replace-reviewed` to discard yours too. A claim you have written a note on
is kept either way: `--replace-reviewed` asks for your review decisions to be
thrown out, not your writing, and a note is the one thing on a claim a re-read
cannot produce again. Delete the claim to be rid of it.

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
.venv/bin/python -m playwright install chromium webkit
.venv/bin/python -m pytest
```

The suite is offline: its browser smoke test runs against a temporary local
server and corpus. It covers reference parsing, the store and its status
transitions, tag renaming and deletion across claims, the extraction merge
including the keep-reviewed path, HTML escaping in the export, and BibTeX. In
the browser it covers the review keys, an undone delete, the URL through a
reload and the Back button, the notes written on a paper and on a claim, and
the notices that replaced the browser's own dialogs. Nothing in it calls the model, and nothing reaches the network:
the API tests drive the app itself through `TestClient`.
