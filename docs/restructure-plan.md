# Restructure plan

Doxograph is the paper and claim tool. Planning, contacts, experiment notes and
the theory of change live in the HTML index outside it; tasks live in Linear.
Inside Doxograph, claims stay the unit of storage and the model keeps working
on them, but the reader sees findings first and claims on demand.

## Target shape

Three pages in the header and one pane:

- **Papers.** The library list and the reader. The reader shows the PDF, the
  summary, why the paper is here, and the reader's note. Claims are a drawer
  under the PDF with a count, closed by default. Datasets become artifacts on
  the paper's side panel with a filter chip on the list.
- **Findings.** The landing page. One section per topic in vocabulary order:
  a short synthesis, then agreements, then tensions as single rows. A row
  opens to show its note and the claims it rests on. One button runs
  tensions, agreements and syntheses in sequence. A topic is marked stale
  when papers changed under it.
- **Workspace.** Research context, the ledger, the topic vocabulary, labels,
  settings and exports.
- **Ask.** A right pane that stays open across pages, resizable, with a scope
  chip that follows what is on screen (this paper, this topic, the workspace)
  and a switch to the whole library. One conversation per workspace. Answers
  cite findings as well as claims.

## Order of work

1. Findings page, additive. Combined analysis job on the server. Shorter
   synthesis prompt. Header button. *(this PR)*
2. Ask pane: persistent across views, scope chip, merged with per-paper chat.
3. Reader: claims drawer, side panel with summary, relevance, note, artifacts.
4. Header navigation, Workspace page, Findings as the landing page, and the
   cuts: facets, map, saved searches, evidence boards, research questions,
   marks and questions, claim connections, compare and organize, research
   notes, the workspace notes editor on the library page, the datasets sheet.
5. Run analysis automatically after extraction through the job queue.
