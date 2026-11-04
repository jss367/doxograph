# Restructure plan

Doxograph is the paper and claim tool. Planning, contacts, experiment notes and
the theory of change live in the HTML index outside it; tasks live in Linear.
Inside Doxograph, claims stay the unit of storage and the model keeps working
on them, but the reader sees findings first and claims on demand.

## Target shape

Three pages in the header and one pane:

- **Papers.** The library list and the reader. The reader shows the PDF with
  its claims in a drawer under it, closed by default, whose handle counts
  them, the unreviewed ones, and the findings they feed. The column beside
  the PDF holds the summary, why the paper is here, the reader's note, marks,
  and code links. Datasets become artifacts in that column with a filter
  chip on the list.
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
   synthesis prompt. Header button.
2. Ask pane: persistent across views, scope chip, merged with per-paper chat.
3. Reader: claims drawer under the PDF; the column beside it holds the
   overview, the note and code links.
4. Header navigation, Workspace page, Findings as the landing page, datasets
   as artifacts in the reader's column, and the
   cuts: facets, map, saved searches, evidence boards, research questions,
   marks and questions, claim connections, compare and organize, research
   notes, the workspace notes editor on the library page, the datasets sheet.
5. Run analysis automatically after extraction through the job queue.
