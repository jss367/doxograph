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
reads it with your selected AI provider or a local model, and records:

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
in your own words, and Doxograph's built-in analysis never sends either to a model.
An external agent connected through the optional MCP server can read your notes
when it requests them.
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

Requires Python 3.13 or newer. Use a supported interpreter for `python3` below;
if needed, substitute its versioned command, such as `python3.14`.

```
python3 -m venv .venv && .venv/bin/pip install -e .
```

Choose model access in **Settings → Model access**:

- **API or local model** (the default): configure Anthropic, OpenAI, Ollama,
  or an OpenAI-compatible endpoint in the provider controls below. Anthropic reads `ANTHROPIC_API_KEY`, then an
  `ANTHROPIC_API_KEY=` line in `~/.credentials`, then whatever the Anthropic SDK
  resolves on its own (including an `ant auth login` profile).
- **Claude subscription (Claude Code)**: install the [Claude Code CLI](https://code.claude.com/docs/en/setup),
  run `claude auth login` in Terminal, and sign in with your Claude account.
  Extraction, retagging, synthesis, agreements, tensions, and paper chat then
  run through the installed CLI using that account. Usage follows your plan's
  limits and credits; this does not include a subscription with Doxograph.
  Doxograph ignores inherited API credentials for these requests and refuses
  to fall back to API billing. If Claude Code isn't on the app's PATH, set
  `DOXOGRAPH_CLAUDE_PATH` to its executable's absolute path.
- **ChatGPT subscription (Codex)**: install the [Codex CLI](https://learn.chatgpt.com/docs/non-interactive-mode)
  and run `codex login` in Terminal to sign in with ChatGPT. The same analysis
  features and paper chat use your saved ChatGPT login and its plan limits or
  credits. Doxograph ignores inherited API credentials and refuses to fall back
  to API billing. Set `DOXOGRAPH_CODEX_PATH` if the executable is installed in an
  unusual location. It uses Codex's default model unless `DOXOGRAPH_CODEX_MODEL`
  names another model available to your account.

The Claude subscription route sends PDFs directly to Claude Code, with a 7 MB PDF
limit and a combined input limit for the PDF and conversation. It runs without
file or shell tools, project instructions, hooks, or MCP servers, and does not
save Claude Code sessions. Removing a paper while using a subscription still requires API credentials
if that paper has older Anthropic uploads to delete.

The ChatGPT route sends extracted PDF text with page numbers, up to 600,000
characters including the conversation. Figures and scanned pages are unavailable;
a paper without readable text requires one of the Claude options. Codex runs in
a temporary directory with shell commands, browsing, hooks, plugins, apps, and
delegation disabled. User `config.toml` settings and project instructions are
excluded, and the run is ephemeral. **Codex still loads global instructions**
from `AGENTS.override.md` or `AGENTS.md` in its home directory and sends that
guidance along with the paper request. This is disclosed in Settings and paper
chat. Choose a Claude option if you want requests without global Codex guidance.
Neither subscription option includes a paid plan with Doxograph.

Choose a provider and model in **Settings → AI analysis**. Anthropic is the
default; OpenAI, Ollama, and OpenAI-compatible endpoints are also supported.
Ollama and local OpenAI-compatible servers can run without an API key.
See [Model](#model) for setup and the differences in PDF handling.

To turn off model use, open **Settings** and uncheck **Enable AI analysis**.
The preference survives restarts and applies to every workspace and the CLI
using the same data directory. While off, imports skip extraction, and
extraction, retagging, agreements, tensions, synthesis, and paper chat cannot start new
model calls or upload PDFs for analysis. Requests already sent may finish.
Browsing, manual editing, quote checks, and exports remain available. With
Anthropic or a subscription selected, removing a paper still deletes its
previously uploaded Anthropic copies. With another API or local provider
selected, deletion stays local; any earlier
Anthropic uploads must be removed from your Anthropic account separately.

## Chat about a paper

Open a paper with a stored PDF and select **Chat about paper**. Ask a question
and continue with follow-ups. The model receives that paper's PDF (or its
page-labeled text for ChatGPT) and the
conversation, and is asked to cite PDF page numbers. Personal Doxograph notes
and other papers are not included; the ChatGPT option also includes your global
Codex instructions as described above. Answers can be wrong; check them against
the source.

Conversations are separate for each paper and workspace, and last until the
page reloads. **New conversation** clears the current paper's conversation.
Failed questions keep their draft so you can retry. Chat uses the model access
selected in Settings and is disabled when AI analysis is off.

## Ask the library

**Ask the library** in the header opens a chat that answers across every paper in
the workspace, where paper chat reads one PDF. It needs **Claude subscription
(Claude Code)** selected in Settings → Model access. Claude looks things up
through the [MCP server](#query-doxograph-from-agents) below, started with
`--without-notes`: it can search papers, claims, and PDF text, read a paper,
list topics, and read agreements, tensions, and syntheses. The line under each
answer lists the lookups it made. Claude Code reaches the library through the
app that is serving the page, so the app has to be running, which it is when you
use this.

It has no file, shell, web, or write access, and it does not receive the PDFs.
`--without-notes` leaves out your paper and workspace notes, your claim notes, and
the links from claims to your own claims, and a search cannot match text inside
them. Your research context is not exposed by any tool. It can read every
workspace by ID, but it is asked to stay in the one you are viewing. Answers can
be wrong; check them against the papers.

## Use

```
doxograph serve                       # the web app on http://127.0.0.1:8765
doxograph add 2602.06941              # arXiv ID, arXiv URL, DOI, PDF URL, forum post, or a local PDF
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

In the web app, failed imports explain what went wrong and offer **Retry**
directly in the notification when trying again can help. Files that are not
PDFs must be replaced with a valid PDF. Retry reuses the original reference or
uploaded PDF for the current server session. Successful imports offer **Open paper**;
dismiss a notification with **×** when you are done with it.

A paper you already have is recognized from the reference itself, before any
lookup goes out: re-pasting an arXiv ID, a DOI or a PDF link answers at once
with the key it is already filed under, rather than querying arXiv or Crossref
to reach the same conclusion; the web app says so in a notice rather than
queuing a job. The exception is a paper whose PDF never arrived: that one is
fetched again, since running `add` on it is how the download is retried. A
dropped PDF is matched by its content as well as by what it says inside, so
the same file dropped twice is one paper even when nothing in it names an
arXiv ID or a DOI. A file already here is recognized without being parsed or
looked up, and says so on the job strip — which is where a PDF dropped on the
Mac app's Dock icon reports too.

A landing-page URL is identified from the page's own metadata (`citation_arxiv_id`,
the canonical link, `citation_doi`, `citation_pdf_url`). An arXiv link in a
bibliography is not treated as the page's identity, since that would ingest a
cited paper instead of the one you pasted. When nothing identifies the page,
paste the arXiv ID, the DOI, or a direct PDF link.

A post on LessWrong, the AI Alignment Forum or the EA Forum has no PDF, so
Doxograph makes one. It reads the post through the forum's API and sets its
title, authors, text, tables and images as a PDF, which is then read, checked
and shown like any other paper's. Formulas come through as text, and an image
that cannot be fetched or opened is left as a linked placeholder. An Alignment
Forum post is the same post as on LessWrong and is filed once under either link.
Each post shares a limit of 40 image attempts, 90 seconds of image work, and
32 MB each of received and decoded image data. Omitted figures remain linked
placeholders that explain when a post's image budget was exhausted.
If the available fonts cannot represent the post's characters, it is kept with
a rendering error rather than a PDF with missing text. Adding it again retries.

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
Use **Rename** beside the picker to change any workspace's name, including the
Default workspace. Its papers, claims, notes, and exports stay in place.
Papers dropped on the Mac app's Dock icon go to the workspace currently shown
in its window.

Export HTML writes the file and then offers to open it or to copy its path. A
reference that nothing identifies stays in the box and is named underneath it,
so it is clear which line of a pasted list is the problem.

The Tensions entry in the sidebar shows where papers disagree; `Escape`
returns to the claims. The Map entry draws the papers as a graph: each
paper is a circle sized by its claims and colored by its most frequent
topic, papers whose claims share a topic are joined, a paper that cites
another is joined to it by a blue arrow, disagreements are drawn
with bold red lines and labels, and your own claims appear as squares joined to the
papers that support, contradict, refine, or supply a method for them; a
link marked independent is not drawn. A slider hides topic
links below a chosen number of shared claims; it starts at the median so
a corpus on one subject is not a hairball. Drag nodes, scroll to zoom,
and click a paper to read it. Clicking a topic filters to it.

Press `⌘K` (`Ctrl+K` on Windows or Linux), or click **Jump to…**, to search
papers by title, author, year, or key; topics; evidence boards; and actions in
the current workspace. Use the arrow keys and Enter to open a result, or
Escape to close the palette. Jumping to a paper or topic clears other filters
so the destination is visible. Common actions include opening the map or reading
queue, adding papers, exporting, and changing settings.

Review runs from the keyboard. `j` and `k` move between claims, `e` edits the
selected one, `r` marks it reviewed and moves to the next, and `n` jumps to the
next claim nobody has reviewed, wrapping round the end. `/` puts the cursor in
the search box and `Escape` there clears it; `Escape` anywhere else cancels an
editor or leaves the view you are in. `?` lists all of this, and so does the
Shortcuts button in the header. A paper that arrives with a dozen claims you
have read in one go has "Mark N reviewed" in its header; the notice that
follows offers to take it back, and takes back only what that click changed.

**Focused review**, above the claims, puts one claim beside its source PDF.
It starts with the claims matching your current filters and keeps that set
throughout the session, so marking a claim reviewed does not remove your place
when **unreviewed only** is on. Progress shows your position and how many are
reviewed. The PDF opens at the locally matched quote (or its closest passage),
falling back to the recorded page when no match is available; page and zoom
controls let you check the surrounding material. Papers without a PDF can
still be reviewed. Use **Mark reviewed & next**, **Edit claim**, or the same
`j`, `k`, `r`, `e`, and `n` shortcuts; `Escape` exits. Unsaved edits are kept
for the current browser session when moving between claims or exiting review;
**Cancel** discards the current edit. Quote matching and PDF viewing run locally
and work with AI analysis disabled.

The divider between the lists and the claims can be dragged, so a session spent
reading papers can give the side pane more of the window and a session spent
reading claims can give it less. Focus the divider and the left and right arrow
keys move it too, with Shift for a bigger step and Home to put it back.
Double-clicking it also puts it back. It goes no wider than half the window,
it starts at 20rem so a browser set to a larger default font size gets a wider
pane, and the width is remembered in this browser.

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

All of these tools work with **Enable AI analysis** turned off. Your workspace
notes, boards, connections, reading progress, marks, and saved searches
belong to the selected workspace and are stored in `notebook.json`. They are
excluded from Doxograph's built-in model requests.
Long-form research notes are also private to the workspace, stored separately
in `research-notes.json`, and excluded from built-in model requests. The
[agent connection](#query-doxograph-from-agents) can read note text on request.
Images embedded in either
writing space are stored locally in `note-images/`.

- **Workspace notes** appears above the claims in the main workspace view.
  Keep your questions, working conclusions, and next steps here; use
  **Research notes** for longer documents such as arguments, reviews, and plans.
  Write in Markdown with equations, tables, links, and local images; choose
  Write, Split, or Preview and use the formatting toolbar. Upload, drop, or paste
  images to store them locally. Edit in place or collapse to a two-line preview. **Keep open while reading**
  places the same editor in the paper's notes column, beside its PDF when
  available. The sidebar's **Workspace notes** button brings the editor into
  view. Expansion and reading preferences are remembered for each workspace
  in this browser. Changes save automatically, with a visible saving or saved
  status. Notes also save when you collapse the editor or switch tools or workspaces.
  If saving fails, your text stays in the editor so you can retry. If another
  window changes the notes, copy your text before reloading the saved version.
- **Reading a paper.** Selecting a paper that has a PDF opens the paper itself
  in a dedicated reading view, with a rail on the right for your note,
  highlights, and claims. The PDF and the rail scroll independently. **Hide
  library** makes more room for the paper; **All papers** returns to the library.
  **Claim filters** reveals filters and selection tools, with an indicator when
  a filter is active. Less frequent commands live under **Paper actions**, and
  summaries and labels appear under **About this paper**. In narrow windows,
  notes appear below the PDF. The reader is offline; the PDF is drawn in the
  browser and never sent anywhere. Zoom defaults to the width of the column.
  **Paper actions → Hide PDF** lists the claims full width instead, and the
  choice is kept in this browser. A claim's **PDF p.N** link scrolls the open
  paper to that page.
- **Passages.** Select text on a page and a **Highlight** button appears under
  it. One click draws the highlight over the text, lists it in the rail with
  its page, and opens a note of your own on it, ready to type into. The
  highlight takes the color used last (yellow to begin with); the swatches on
  its rail entry change its color, and the color chosen becomes the one for the
  next mark. A highlight can be captured as a claim later or removed.
  **Capture as claim** on its entry opens the claim editor with the quote,
  page, and note filled in; write the claim in your own words and save. Quotes
  receive the same local verification as ordinary manual claims. Pages with no
  selectable text can still be read and transcribed by hand. Clicking a mark
  in the rail scrolls the paper to it.
- **Figures and questions.** **Mark a figure** in the reader's toolbar lets you
  drag a box around a figure, table, or any other region of the page, then
  click **Mark this figure**. A figure mark takes a note like a passage does
  but cannot become a claim. Ticking **Open question** on any mark turns its
  note into a question to come back to. Marks are stored in the workspace's
  `notebook.json`; the PDF is not changed.
- **Marks and questions**, in the sidebar, lists every mark in the workspace by
  paper, or only the open questions. **Open page** returns to the mark in the
  paper, and **Mark answered** clears the question but keeps the note.
- **Export annotated PDF**, in the rail's Marks heading or the Marks list,
  downloads a copy of the paper with its marks written in as standard PDF
  highlight and rectangle annotations, carrying your notes and questions, so
  other PDF readers show them. The stored PDF is never changed.
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
- **Research notes** are a separate writing space for arguments, literature
  reviews, derivations, and experiment plans. Open **Research notes** in the
  sidebar to create, search, rename, and write notes in Markdown with a live
  preview. Choose Write, Split, or Preview to suit the task. Headings, lists,
  blockquotes, code, and tables render alongside inline LaTeX (`$...$`) and
  display equations (`$$...$$`). The toolbar inserts common formatting,
  equations, tables, and images. Upload, drop, or paste PNG, JPEG, GIF, or WebP
  images up to 10 MB; images and equation fonts are stored locally, so writing
  and rendering work offline. Remote image links appear as placeholders until
  you upload the image.
  **Link note or paper** searches your notes and library and inserts a link
  at the cursor. Follow it in the preview, or use **Linked research** below
  the editor. **Backlinks** lists other saved notes that refer to the current
  note; removed destinations are marked as missing. Note links can be bookmarked
  and reopen the right note and workspace. Ordinary Markdown web links open
  in another tab.
  Save with **Save note** or Command/Ctrl+S. Leaving unsaved writing asks before
  discarding it. A conflicting save keeps your draft; **Reload saved version**
  lets you inspect the saved text before explicitly replacing your draft.
  **Export Markdown** downloads your current writing, including an unsaved
  draft. Image and library links use absolute URLs back to this Doxograph
  installation; this is an export, not two-way Markdown synchronization.
- **Reading queue** keeps an ordered list of papers with queued, reading, and
  finished states, independently of claim review. **Bookmark this page** in the
  reader puts the paper on the queue at that page; for a paper on the queue the
  reader saves your page position as you scroll, and the queue's buttons open
  the paper where you left it or at a bookmark.
  that paper, then the remaining corpus, even when current filters hide it.
  The last paper and review position are remembered per workspace in this browser.
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

## Query Doxograph from agents

Doxograph includes a read-only **Model Context Protocol (MCP)** server, so an
agent can discover your workspaces, find papers and claims, and read their
sources without learning the web API. It uses stdio: your agent client launches
`doxograph mcp`, which queries the running local app over HTTP. It does not
import papers, change your notes, generate analysis, or call a model.

Install or update Doxograph with `pip install -e .` in your Python environment,
then open the updated Mac app or run `doxograph serve`. Restart a server that
was running before the update so it loads the new query routes. Configure your
agent's MCP connection with the **absolute path** to the installed `doxograph`
executable and these arguments:

```text
command: /absolute/path/to/doxograph/.venv/bin/doxograph
arguments: mcp --url http://127.0.0.1:8765
transport: stdio
```

For clients that use an `mcpServers` JSON configuration:

```json
{
  "mcpServers": {
    "doxograph": {
      "command": "/absolute/path/to/doxograph/.venv/bin/doxograph",
      "args": ["mcp", "--url", "http://127.0.0.1:8765"]
    }
  }
}
```

Use the port the app is actually running on; the Mac launcher can choose
another port when 8765 is occupied. `--url` accepts local HTTP origins only,
and defaults to `http://127.0.0.1:8765`. No API key is needed. The adapter does
not open or initialize a separate corpus, so `DOXOGRAPH_DATA` belongs on the
HTTP server, not on the MCP command.

Add `--without-notes` to leave out the note tools and the personal fields of the
other tools: paper notes, claim notes, and links from claims to your own claims.
Searches then cannot match text inside them. The in-app **Ask the library** chat
runs the server this way.

| Tool | What it reads |
| --- | --- |
| `list_workspaces` | Workspace names and IDs |
| `search_papers` | Paper metadata, including papers without claims |
| `search_claims` | Claims, quotes, evidence, review flags, and claim notes |
| `search_paper_text` | Ranked matches in PDF text, with passages and page numbers |
| `get_paper` | One paper's metadata, summary, source, and personal notes |
| `list_topics` | Topic slugs, descriptions, and claim counts |
| `read_evidence` | Existing agreements, tensions, or topic syntheses |
| `list_notes` | Workspace and research note titles and short previews |
| `read_note` | A personal note's text in character chunks |

Start with `list_workspaces`. Every corpus tool requires a workspace ID,
including `default` for the Default workspace; the app's currently selected
workspace does not change it. List tools return up to 20 items by default
(maximum 100), with `total` and `next_offset` for pagination. PDF text search
returns the top matching papers with a maximum of 100. Note text is read in
8,000-character chunks by default, up to 20,000 per request.

Paper and claim searches require every query word, ignoring case and accents;
an empty query lists records. `search_claims` also accepts exact `topic` and
`paper` filters and a `reviewed` boolean. Quotes keep their original text,
locators, verification status, paper keys, and claim IDs. Saved analysis keeps
its review status and staleness flags; it is never regenerated by a read.
Syntheses return their text and source counts; use `search_claims` with the
topic to read supporting claims.
`storage_issues` identifies unreadable papers that were omitted from a result.
Personal notes returned to an external agent may enter that agent's model
context; they remain excluded from Doxograph's own analysis requests.

The same paginated reads are available without MCP under `/api/query/`:
`papers`, `papers/{key}`, `claims`, `topics`, `evidence?kind=agreements`
(or `tensions` or `syntheses`), `notes`, and `notes/{note_id}`. For example:

```bash
curl -sG http://127.0.0.1:8765/api/query/claims \
  -H 'X-Doxograph-Workspace:default' \
  --data-urlencode 'q=steering recovery' \
  --data-urlencode 'reviewed=true' \
  --data-urlencode 'limit=20'
```

The [interactive API documentation](http://127.0.0.1:8765/docs) lists the query
parameters. Unlike MCP tools, direct HTTP requests without a workspace header
continue to use the Default workspace.

## The corpus on disk

Everything lives in `~/doxograph-data` by default; set `DOXOGRAPH_DATA` to move
it. The corpus is deliberately outside this repository, since it holds
downloaded PDFs and in-progress notes.

For compatibility, the Default workspace uses the data-directory root shown
below. Named workspaces repeat that same structure under
`workspaces/<workspace-id>/`; `workspaces.json` records workspace display names,
including a custom name for the Default workspace when one is set.
Command-line commands operate on the Default workspace.

```text
workspaces.json       named workspace registry
workspaces/<id>/      an independent named corpus
papers/<key>.json    one paper, its labels, and its claims
pdfs/<key>.pdf       the paper itself
text/<key>.txt       its text, extracted once and reused (pages split by \f)
locks/               lock files, so two processes do not write at once
retired-keys.json    keys of removed papers, never issued again
pending-topic-change.json  unfinished topic rename or deletion, when recovery is needed
tags.yaml            the topic vocabulary
tensions.json        where papers disagree, and what you decided about each
syntheses.json       what the papers hold on each topic
ledger.yaml          your own claims, for linking against
context.md           what your research is about, given to the extractor
research-notes.json  your long-form Markdown notes, with per-note revisions
note-images/        local images embedded in workspace and research notes
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

If a paper's saved file cannot be read, the library shows a persistent warning
with the file path, and command-line listings report it on standard error.
The damaged file is left untouched. Repair it or restore a backup; the warning
clears when the library next refreshes.

The same protection applies to saved research tools, tensions, agreements,
syntheses, the topic vocabulary (`tags.yaml`), your claims (`ledger.yaml`), and
research context (`context.md`). If one of those documents cannot be read, the
library still shows the healthy papers and reports the damaged document's path. Its section stays
unavailable until repaired; saves refuse to overwrite it. Syntheses remain
visible but are marked stale while their supporting tensions cannot be read.

Topic renames and deletions record their intent in `pending-topic-change.json`
before changing any papers. If a write fails or the app stops, the next library
refresh, command-line operation, or vocabulary edit resumes the change. Recovery
preserves newer notes and review decisions. While recovery is blocked, the
library reports the problem and further vocabulary changes are refused. Keep
the pending file until recovery completes; it is removed automatically.
An open claim editor that still names a renamed or deleted topic keeps its draft
and asks you to update the topic list before saving. Removed names are remembered
in `tags.yaml`; explicitly adding a name to the vocabulary makes it usable again.

A removed paper's key is retired rather than reused, so a key names one paper for
all time. Re-adding a paper you deleted therefore gets a suffixed key. That is
the point: a slow operation carrying a key — an extraction job, a download, a
queued edit — never has to work out which paper it meant.
If `retired-keys.json` cannot be read, the library reports its path and refuses
new key reservations and paper deletions, including remote upload cleanup.
Repair the file or restore its history from a backup before retrying; replacing
it with an empty list would allow previously used keys to be issued again.

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

The map draws a citation as a blue curve with a large arrowhead pointing to
the paper cited. Reciprocal citations curve apart so both directions remain
visible. Hover over a connection to read the two titles and their relationship;
hover over a paper to see separate **Cites** and **Cited by** lists for the
visible citation layer. Shared-topic links stay faint gray, while disagreements
use bold red lines labeled **contradicts** or **disagrees with**. A paper that
contradicts one of your own claims gets a red dotted line labeled **contradicts**.
Arrowheads, line widths, and relationship labels stay the same screen size
as you zoom.

Citations come from the reference lists in the PDFs: the text after the first
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
excluded from Doxograph's built-in model requests. An external agent connected
through MCP can read notes when requested, as described above.

For questions and thoughts about the whole workspace, edit **Workspace notes**
in the main view. You get one document per workspace, saved automatically.
It supports Markdown, equations, tables, and local images, with Write, Split,
and Preview views. For multiple named documents, use **Research notes**.

A paper's note sits under the summary in its header, or at the top of the rail
beside the paper when its PDF is open. An empty note is ready to type into;
Save puts it on the paper, and it is there the next time you open it.
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

With Anthropic, the PDF is sent with a cache breakpoint on it, so a second read of the same
paper within the hour hits the prompt cache. On a 77k-token paper that is the
difference between paying for 77k input tokens and paying for about a tenth of
that.

## Model

The Claude subscription uses `claude-opus-5` by default; set `DOXOGRAPH_MODEL`
to change it. The ChatGPT option uses Codex's default model; set `DOXOGRAPH_CODEX_MODEL`
to choose another. Extraction requests high effort and a JSON schema, so the
response is validated structurally before it reaches the store. Switching
providers affects new work; queued and running operations keep the provider
selected when submitted. Retrying a failed import uses the current selection.

Open **Settings → AI analysis**, choose a provider, enter its exact model name,
and click **Save provider**. The selection survives restarts and applies to all
workspaces and the CLI. Each provider remembers its model and endpoint. Changes
apply to new operations; running operations finish with their original provider.

| Provider | Setup | Paper input |
| --- | --- | --- |
| Anthropic (default) | `ANTHROPIC_API_KEY`; defaults to `claude-opus-5` | Native PDF upload and prompt caching |
| OpenAI | `OPENAI_API_KEY`; enter a model supporting JSON Schema output | Page-numbered text extracted locally |
| Ollama (local) | Start Ollama, download a model, enter its name; defaults to `http://localhost:11434` | Page-numbered text extracted locally |
| OpenAI-compatible | Start a server such as LM Studio; enter model and API base URL, typically `http://localhost:1234/v1`; optional `DOXOGRAPH_API_KEY` | Page-numbered text extracted locally |

Keys come from the environment or matching `NAME=value` lines in
`~/.credentials` (override with `DOXOGRAPH_CREDENTIALS_FILE`). Anthropic also
supports the SDK's own authentication resolution. Keys are never included in
browser state or saved provider settings.

For local analysis, use a localhost endpoint and a downloaded local model.
Analysis sends paper text, research context, vocabulary, ledger claims, and
relevant extracted claims only to the selected endpoint. It never falls back
to a cloud provider. Importing a remote reference still downloads the paper;
use a local PDF when working offline. No model is bundled or downloaded by
Doxograph.

Text-based providers cannot read scanned PDFs without OCR, figures, or layout
that the PDF text extractor misses. Ollama uses a 65,536-token context budget
and rejects prompts that conservatively exceed it rather than sending a
partial paper. Local models need enough memory for that context. Other
endpoints must support the supplied JSON Schema and enough context for the
whole paper; unsupported requests fail without changing the paper's analysis.
Malformed, refused, and truncated responses are rejected before saving.
Model quality varies; extracted claims and quotes still need review.

For CLI-only setup, set `DOXOGRAPH_PROVIDER` (`anthropic`, `openai`, `ollama`,
or `openai-compatible`), `DOXOGRAPH_MODEL`, and optionally `DOXOGRAPH_BASE_URL`.
Saved settings take precedence over these defaults. For example, before saving
provider settings:

```bash
DOXOGRAPH_PROVIDER=ollama DOXOGRAPH_MODEL=your-downloaded-model doxograph extract
```

All five operations—extraction, retagging, agreements, tensions, and synthesis—
use the selected provider. Changing provider, endpoint, or model invalidates
cached topic passes. Extraction records its provider, endpoint, and model;
existing reviewed claims and manual edits retain their usual protections.

The adapters follow the [OpenAI chat completion API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
and [Ollama structured output API](https://docs.ollama.com/capabilities/structured-outputs).

## Tuning

The extraction prompt is `SYSTEM` in `doxograph/extract.py` and the field
descriptions are the schema right below it. Both are read by the model
verbatim, so that is where to push if claims come out too coarse, too numerous,
or too confidently worded. The retagger has its own, shorter prompt in
`retag_paper`; if it assigns a topic to nearly every claim, tighten that one.

## Tests

```
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/python -m playwright install chromium webkit
.venv/bin/python -m pytest
.venv/bin/python -m ruff check doxograph tests
.venv/bin/python -m mypy
node --test tests/frontend/*.test.cjs
```

CI runs these checks, JavaScript syntax checks, and macOS Swift type checking
and quit/relaunch tests. Strict type checking covers the shared storage reader,
saved research models, model request/response contracts, and all API, local,
and subscription provider adapters. The remaining application modules can be
added as their dictionary-based interfaces are narrowed.

`analysis_types.py` defines the common request and response contracts. Provider
responses are normalized into immutable text and usage records, then validated
against the requested JSON Schema before extraction can change saved research.
Refusals, incomplete responses, and unexpected stop reasons cannot be merged;
malformed optional usage counts are treated as unknown. SDK-specific objects
stay at the adapter boundary.

Saved papers and claims are validated on read and before writing. Older records
can omit newer optional fields, unknown author lists can remain null, and
existing note and claim-identifier migrations still apply. Validation preserves
unknown fields and does not fill unrelated defaults or rewrite files on read.
Malformed records are reported through the library's storage warnings, and an
invalid write leaves the saved paper and its history untouched.

`requirements-dev.lock` pins the development and runtime dependencies with
hashes for Python 3.13 and newer across platforms. To update it after editing
dependencies in `pyproject.toml`, run:

```
uv pip compile pyproject.toml --extra dev --python-version 3.13 --universal --generate-hashes --output-file requirements-dev.lock
```

Frontend request and navigation behavior is tested directly with Node.js 20 or
newer, without launching a browser. Those tests exercise request destinations,
ETag handling, responses arriving out of order, queued workspace switches, and
poll scheduling; the browser suite still verifies the full editing workflows.

Browser features live in separate assets under `doxograph/static/`: appearance
preferences in `appearance.js`, the paper map and citation loading in
`paper-map.js`, and the research tools, long-form notes, and focused review in
their corresponding scripts. The workspace client in `workspace-client.js`
owns requests, the latest corpus snapshot, the workspace registry, ETags, and
the count of writes in flight. Features receive that client through `connect`
instead of reading the library's state variables. Its `request` method reads
JSON and its `response` method preserves download bodies and headers; both
capture their destination before waiting for a response. `select` invalidates
older corpus refreshes even when navigating away and back to the same workspace.

`workspace-controller.js` serializes workspace changes through explicit hooks
for draft checks, pending deletes, and rendering. `poller.js` owns the timer and
prevents overlapping rounds; manual refreshes can wait for an older round to
finish before reading again. `app.js` supplies those hooks and keeps rendering,
editor drafts, and optimistic deletions. These are classic scripts loaded in
the order in `index.html`, so they need no JavaScript build step.

For the fast Python/API checks, browser executables do not need to be installed:

```
.venv/bin/python -m pytest -m "not browser" --durations=20
```

Run browser checks separately with `-m browser`. CI splits them across four
jobs; run the same selection locally with, for example,
`.venv/bin/python -m pytest -m browser --browser-shard=1/4 --durations=20`.
The shards cover every browser case exactly once. Each pytest process reuses
its Chromium and WebKit processes, while every scenario gets fresh browser
contexts and a temporary server and corpus. Update checks can invoke the
normal polling function directly; scheduling checks still exercise the real
background timer. The final **Python and browser tests** CI check passes only
when the Python job and every browser shard pass.

The suite is offline: its browser checks run against a temporary local
server and corpus. It covers reference parsing, the store and its status
transitions, tag renaming and deletion across claims, the extraction merge
including the keep-reviewed path, HTML escaping in the export, and BibTeX. In
the browser it covers the review keys, an undone delete, the URL through a
reload and the Back button, the notes written on a paper and on a claim, and
the notices that replaced the browser's own dialogs. Nothing in it calls the model, and nothing reaches the network:
the API tests drive the app itself through `TestClient`.
