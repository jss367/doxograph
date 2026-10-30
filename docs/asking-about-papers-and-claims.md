# Asking about papers and claims

Proposed product design for Doxograph.

Doxograph should have one Ask experience, available wherever a research question arises. A user can ask about a paper, several papers, a claim, or the whole library without copying titles or identifiers. The conversation shows what the question is about, where the assistant may look for evidence, and what it actually consulted.

The design rests on one distinction: the **focus** of a question is separate from the **evidence** the assistant may use to answer it. A claim can stay in focus while the user widens the evidence from its originating paper to the library. Everything else follows from keeping those two things visible and keeping the evidence boundary enforced by the server rather than by instructions.

## Where things stand

There are two evidence paths today.

- [Paper chat](../doxograph/chat.py) sends one paper, as a PDF or extracted text, to the configured model. It works with API keys, local models, and both subscriptions.
- [Library chat](../doxograph/agent.py) runs Claude Code or Codex against the read-only [MCP server](../doxograph/mcp_server.py). It requires a subscription. Its optional paper focus is a sentence in the system prompt; nothing stops the agent from reading other papers.

[Saved conversations](../doxograph/chats.py) record a paper key or none, an agent, and alternating messages. Each library answer records a list of human-readable lookup lines ("Searched claims attention") derived from the tool calls the agent made, not from what the tools returned. Library answers can be saved as research notes; paper answers cannot.

The MCP server already enforces one boundary: `--workspace` binds the connection to a workspace and rejects queries for any other. Paper scope should be enforced the same way.

## Starting a conversation

Every entry point opens the same conversation panel with a prepared scope. Opening it sends nothing.

| Starting point | Action | Focus | Evidence |
| --- | --- | --- | --- |
| Paper overview | Ask about this paper | That paper | That paper |
| Claim card | Ask about this claim | The claim and its paper | Its originating paper |
| Selected papers | Ask about selection | Selected papers | Selected papers |
| Selected claims | Ask about selection | Selected claims | Their originating papers |
| Library toolbar | Ask the library | None | Current workspace |

A mixed selection of papers and claims uses the union of the papers and the claims' originating papers. Claims stay individually visible even when several come from one paper; their papers are deduplicated in the evidence list.

An explicit selection wins over the currently open paper. Opening another paper or changing a library filter during a conversation does not change its scope. Filtering the library never implicitly selects every matching paper.

Contextual actions start a new conversation. Earlier conversations for the same paper or selection are reachable through the existing conversation list, not attached to silently.

## Showing and changing sources

The scope sits directly above the question box. A paper comparison shows **Sources: Paper A, Paper B** with **Add sources** and a remove control on each chip. A claim shows its text and paper under **About**, then **Evidence: originating paper**. A library question shows **Search: entire library · 42 papers**. Large selections collapse to a few chips and **Show all 12**.

The source picker searches titles, authors, claim text, and topics, and marks each result as a paper or a claim. Adding a claim visibly adds its originating paper to the evidence. Removing the last source leaves an empty selection with Send disabled; it never falls back to the whole library.

For claim questions, **Search the rest of the library** sits beside the evidence setting. It keeps the claim in focus and widens the evidence. The hint explains that finding conflicting evidence from other papers requires this.

The picker, chips, and scope controls work from the keyboard, and a chip's state is readable without color or hover.

## Keeping follow-ups predictable

Ordinary follow-ups keep both focus and evidence. "What about the limitations?" refers to the same selection. Naming another paper in a question offers **Add this paper** but does not by itself let the assistant read it.

Before the first question, scope edits just change the draft. After an answer:

- **Adding** evidence, or widening to the library, applies to the next question and inserts a visible line in the transcript: **Added Paper C** or **Expanded search to the library**. Earlier answers keep their original scope.
- **Removing** evidence, including narrowing from the library to a selection, offers **Start a new conversation with these sources**. The draft and the sources carry over. The old transcript does not, because earlier answers may contain evidence from the removed sources.
- Changing only the focus within the same evidence continues the conversation with a visible focus line.

The scope, workspace, and provider are captured when Send is pressed and stored on that message. Changes made while a request is running apply to the next question. An answer always lands in the conversation that asked it, even if the user has moved to another paper or workspace.

If a selected paper is deleted, the conversation shows **Source unavailable** on its chip and requires the selection to be repaired before another question. Earlier answers and their citations stay as they were, with the unavailable destination marked.

## Making answers useful and verifiable

Answers stay conversational. A comparison table is appropriate when methods, conditions, or findings line up side by side. Suggested starters fill the draft; they never send.

| Focus | Example starters |
| --- | --- |
| One paper | Explain the main result · What are the limitations? |
| Several papers | Compare methods · Where do their findings differ? |
| A claim | What supports this claim? · What qualifications does the paper give? |
| Library | What does this library say about…? · What remains unresolved? |

For claims, **Find conflicting evidence** previews the widening to the library when the evidence is a single paper. It never searches elsewhere silently, and an absence of conflict within one paper is reported as exactly that.

Each answer has an expandable **Evidence used** section listing evidence retrieved for that answer, documents supplied directly, and evidence carried forward from earlier turns, with the lookup lines underneath. Its coverage line comes from the application's records, not from the model: **Retrieved passages from 3 of 8 selected papers** or **Supplied the full paper as extracted text**. Making a paper available, searching its text, and supplying its full document are different things and the wording keeps them apart. Carried-forward evidence is labeled with its original turn and is not counted as a new lookup.

Citations are clickable and sit beside the statements they support. A citation opens the claim, or the paper at the cited page, without losing the conversation or the draft. The assistant emits citations as structured references that the application resolves against the evidence available to that answer, including directly supplied documents and recorded evidence carried forward from earlier turns. A reference outside the current scope, or without a matching evidence record, renders as plain text with a visible warning rather than as a link. For a supplied document, validate page references against its page inventory; supplying the document does not establish that an arbitrary claim identifier was supplied too. A resolving link establishes traceability, not that the source supports the sentence.

The assistant distinguishes what a paper reports from its own interpretation, and carries forward the qualifications the library already records: an unreviewed claim, an unverified quote, or a stale synthesis does not gain authority by being repeated. Several claims from one paper are not several studies.

When evidence is insufficient, the answer says what is missing and offers a next step: add a paper, widen the search, attach the missing PDF, or look at a figure. "I found no conflicting evidence in the retrieved passages" is the right wording; "there is no conflict" is not.

## Saving and resuming

**Save as research note** appears on every answer, including paper answers. The note records the question, the answer, the scope at submission, the citations, the evidence record, the provider and model, and the time. Saving twice opens the existing note. Editing the note does not change the conversation, and deleting the conversation does not delete the note. This is how library answers already behave; the change is to extend it to paper answers and to include the scope.

Conversations belong to a workspace and are listed from their papers and from the library. Failed requests keep the draft and the scope, with an explicit retry.

## Two evidence paths, one interface

The interface is unified. The evidence paths are not, and the difference stays visible.

- A single paper with an API key or local model uses paper chat: the whole PDF or extracted text goes to the model. This keeps today's capability for users without a subscription.
- A single paper with a subscription may use either path. Default to paper chat for parity with other providers; the retrieval path is available when the user widens the scope.
- Several papers, a claim with widened evidence, or the library use the retrieval path and require a subscription.

When a scope change crosses into retrieval and the selected provider cannot do it, the panel says so, keeps the draft, and offers the supported providers. It never switches provider or billing mode on its own.

A claim question uses its originating paper's path with the claim text supplied as focus. When a paper has no readable PDF, the panel offers **Use extracted claims only** if claims exist and a retrieval provider is available. A paper with neither text nor claims can only contribute its metadata, and the panel says so.

Figure and scanned-page access depends on the provider and the input supplied. Native PDF inputs to Claude can include page images. Codex and the current text-provider paths receive extracted text and cannot inspect figures or unreadable scanned pages. Retrieval supplies text and claims, not page images. The panel describes the active provider's input format and limitations rather than promising image access for all paper chat.

Personal notes, research notes, and the user's own research context stay out of both paths in this design, as they do today. Web search and other workspaces are out of scope.

## Enforcing scope

Source selection is a retrieval boundary enforced by the MCP server, not a request in the system prompt. The pattern already exists for workspaces: the server is started with `--workspace` and rejects every call for another workspace. Add `--papers` with the same shape.

With a paper list bound:

- `get_paper` rejects any other key.
- `search_claims` and `search_papers` pass the bound keys to their query handlers, which restrict the candidate records before computing results.
- `search_paper_text` needs a paper filter on `/api/search`, which does not exist yet. Add one and pass the bound keys.
- `list_topics` is unavailable under a paper scope in the first release. The assistant can use the topic labels on selected claims. A later scoped version may return topics and counts derived only from selected papers, without library-wide descriptions or counts.
- `read_evidence` returns an agreement or tension only when every claim it references is in a bound paper. Syntheses carry source counts rather than claim references, so they are omitted entirely under a paper scope. The assistant can reason from the selected claims instead.

Add one more tool, `read_passage`, that returns a bounded span of a paper's text around a page or locator. Without it a selected-paper question has to guess search terms to see context. It respects the same boundary.

The MCP server binds the permitted keys outside model-controlled arguments and passes them to query handlers. Scope filtering happens before ranking, counts, limits, and pagination; filtering an already paginated response is insufficient. Totals and continuation offsets describe only the scoped results, and response metadata must not disclose excluded papers. Cache entries include the workspace and scope in their keys. The system prompt still describes the scope, but the prompt is not what keeps it inside.

## Recording evidence

Today the activity lines record which tools were called with which arguments. Coverage and citations need what the tools returned or what was supplied directly. Capture successful retrieval results at the tool boundary, correlated with the request and turn; provider events can supply this record only where they preserve the complete result. Record paper keys, claim identifiers, page numbers, and whether the result was metadata, a claim, or a passage. For paper chat, record the document supplied, its format, and its page inventory. A directly supplied focus claim also gets an evidence entry.

Persist the evidence record on the assistant message. Keep a compact snapshot of returned claim text and bounded passages so later citations can retain the wording supplied at the time. For directly supplied documents, retain the cited excerpt when available from the supplied text; do not manufacture an excerpt from a later file or treat a model-generated quote as verified. Preserve cited snapshots with saved research notes. This does not require archiving every PDF or introducing a full source-versioning system; document references without an excerpt remain links to the current document, not historical snapshots.

For a follow-up, carry forward recorded evidence only while it remains within the current scope, and supply the relevant evidence entries alongside the conversation. Reuse their original turn references instead of counting them as fresh retrievals. If the necessary evidence is no longer present in the model's input, retrieve it again before treating it as available for citation. Old assistant prose alone is not an evidence record. Starting a new conversation after narrowing the scope carries forward neither the transcript nor its evidence records.

The conversation record gains a scope on each user message: focus, evidence mode (paper chat, selected papers, library), and the paper keys. Existing conversations load as before: a conversation with a paper key is that paper, a conversation without one is the library, and messages without an evidence record show **Evidence details unavailable for this older answer**.

Paper deletion currently deletes that paper's conversations. Under this design a conversation can span papers, so deletion marks the source unavailable instead and leaves the conversation in place.

## Shipping it

Build it in three releases, each useful on its own.

**Release 1: scope that holds.** The scope model on conversations and messages, `--papers` enforcement in the MCP server and query handlers, filtering before pagination, bounded `read_passage`, source chips with add and remove, and the paper, claim, and selection entry points. Library chat keeps working as it does. Paper chat retains API and local-provider support. The result is that asking about a claim or a selection is one click and retrieval is restricted to the selected papers.

Ready when:

1. Selecting two papers and asking a comparison makes only those papers reachable, including through agreements, tensions, direct key lookups, and text search.
2. Asking about a claim without copying its text uses its paper as evidence, and widening to the library keeps the claim in focus.
3. Adding a paper shows where it joined; removing one starts a fresh conversation without the old transcript.
4. Switching workspaces while a request runs leaves the answer in the conversation that asked it.
5. An unsupported provider, missing PDF, or failed request explains itself and keeps the draft.
6. The picker and chips work from the keyboard.
7. Selected papers remain discoverable even when unselected papers would fill the first unfiltered page. Counts and continuation offsets describe only selected results; library-wide topic data is unavailable.
8. The assistant can read surrounding text through `read_passage` for a selected paper, and the same request for an excluded paper is rejected.

**Release 2: evidence you can see.** Record retrieved evidence and directly supplied documents, retain compact claim and passage snapshots, and track evidence carried forward across turns. Show **Evidence used** and the coverage line, and extend **Save as research note** to paper answers with the scope included. Ready when both evidence paths produce accurate records, follow-ups distinguish reused evidence from new lookups, and editing a source claim leaves its saved evidence text unchanged.

**Release 3: citations.** Structured citation references in the system prompt, resolution against the evidence record, clickable links to claims and pages, and the warning rendering for references that do not resolve. Ready when citations resolve for retrieval, directly supplied documents, and carried-forward evidence; excluded sources, unknown identifiers, and invalid page references receive warnings. Citation details distinguish the preserved excerpt from the current source.

Deferred without a date: automatic detection of source edits, full source versioning or document fingerprints, including personal notes as a selectable source, saved source collections, live topic-based selections, web search, and a separate exhaustive literature-review workflow. Compact evidence snapshots are included above; automatic change detection and full document history would further improve verification of older answers but are not prerequisites for the first release.
