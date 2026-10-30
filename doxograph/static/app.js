'use strict';

// Select the destination before Dock uploads, drops, or other writes can run.
let initialWorkspaceId = 'default';
try {
  initialWorkspaceId = new URLSearchParams(location.hash.slice(1)).get('ws')
    || localStorage.getItem('doxograph-workspace') || 'default';
} catch (_) { /* storage may be unavailable */ }
const workspaceClient = createWorkspaceClient({
  workspaceId: initialWorkspaceId,
  fetch: (...args) => fetch(...args),
  rememberWorkspace(id) {
    try { localStorage.setItem('doxograph-workspace', id); } catch (_) { /* optional */ }
  },
  onWorkspaceChange(id) { window.doxographWorkspaceId = id; },
});
window.doxographWorkspaceId = workspaceClient.workspaceId;
// The library keeps its optimistic view of the client's latest snapshot here.
// Feature modules access the client directly, not this rendering alias.
let S = workspaceClient.state;
// selectedId is a claim id rather than a render position: in grouped mode a
// claim with several topics is drawn once per topic, so positions do not map
// onto claims one-to-one. newClaim holds a claim being written by hand; it lives
// only in the browser until Save, so navigating away or filtering it out cannot
// leave a blank claim behind on the server.
// parkedNewClaims keeps new claims whose paper scope was cleared or whose save
// failed, keyed by their original paper so returning there can restore them.
const NEW_CLAIM_ID = '__new__';
// drafts holds unsaved editor contents, keyed by claim id: what was typed but
// not yet saved, whether because the save failed or because something redrew the
// list. Without it a re-render redraws from the unchanged server row and
// silently discards the edit. A map rather than one slot, so opening a second
// claim's editor does not throw away the first one's draft.
// view is 'library', 'claims', 'tensions', 'agreements', 'research', or 'graph'. Only
// claims has a claim editor, so switching away closes any open one (keeping
// its draft) and lets the background poll run. graph holds the map's display
// choices, reset with the rest when the workspace changes; the layout itself
// is in GRAPH below.
// tensionFocus narrows the tensions view to those involving one claim; it is set
// by the marker on a claim card and cleared by "show all".
// similar is the one claim whose lookalikes from other papers are open, in the
// same shape as quoteContext. Also one at a time, and for the same reason.
// textSearch is the last answer from the search over the papers' own text:
// { q, loading, papers, terms, error }. It is keyed by the query it was asked
// for, so an answer left over from an earlier one is not drawn under a later.
// quoteContext is the one claim whose quote is being shown in the paper, with
// the passage the check matched it against: { claim, paper, loading, error,
// data }. One at a time, since it is read in place of the PDF and two open at
// once would only push the claims apart.
// synthEditing is the topic whose synthesis is open for correction by hand, and
// synthDrafts what has been typed into each, by topic, kept across redraws and
// navigation like claim drafts. A map for the same reason `drafts` is: opening
// a second topic's editor, or leaving the page the first is on, must not throw
// away the first one's text. synthSaving is the topic whose hand save is in
// flight: its editor is frozen, as a claim form is while it saves, because
// success redraws from the server value and typing meanwhile would be lost.
// noteEditing, noteDrafts and noteSaving are the same three for the note a
// reader writes on a paper, keyed by paper key. A note is nobody's judgment
// but the reader's, so unlike a synthesis it is saved without flushing the
// held deletes: no claim is read to write one.
const PDF_HIDDEN_KEY = 'doxograph-pdf-hidden';   // before `V`, which reads it at startup
const V = { paper: null, tag: null, label: null, q: '', kind: '', unreviewed: false, unverified: false, group: true,
            editing: null, selectedId: null, newClaim: null, parkedNewClaims: {},
            drafts: {}, error: null, view: 'library', tensionStatus: '', tensionFocus: null, agreementStatus: '', agreementFocus: null,
            synthEditing: null, synthDrafts: {}, synthSaving: null, researchSaving: false, researchDraft: null, researchBase: null,
            noteEditing: null, noteDrafts: {}, noteSaving: null,
            graph: { xAxis: 'connections', topics: true, minShared: null, tensions: true, ledger: true, cites: true }, paperSort: null,
            quoteContext: null, textSearch: null, similar: null, pdfHidden: readPdfHidden() };

// Whether the paper's PDF is kept out of the claims view. A preference of this
// browser, like the side pane's width: it is about the screen, not the corpus.
function readPdfHidden() {
  try { return localStorage.getItem(PDF_HIDDEN_KEY) === '1'; } catch (error) { return false; }
}
function storePdfHidden() {
  try { localStorage.setItem(PDF_HIDDEN_KEY, V.pdfHidden ? '1' : '0'); } catch (error) { /* preference remains for this page */ }
}

// Papers whose removal is in flight. Their cards and their header stay on
// screen, and clickable, until the DELETE comes back: `removePaper` freezes the
// claims by id, but that reaches only the claims the server has already told
// the page about. A claim being written by hand has no id yet, and the
// paper-level actions write claims of their own — a re-read makes a fresh set,
// a retag and a quote check rewrite the ones on file, accepting a proposed
// topic writes the paper itself. Each of them races the removal: land first and
// the DELETE quietly throws the work away under a page that reported it saved,
// land second and it fails with a 404 over a paper the reader did ask to
// remove. Tracked by key, and asked before any of them goes out.
const removingPapers = new Set();

function isRemoving(paper) {
  return removingPapers.has(paper);
}

// True when the action must not go ahead, having said why. `what` names the
// work that would be lost, so the notice reads as a reason rather than a
// refusal: "a new claim would be removed with it".
function refuseWhileRemoving(paper, what) {
  if (!isRemoving(paper)) return false;
  toast(`That paper is being removed; ${what} would go with it.`, { tone: 'warn' });
  return true;
}

const $ = (id) => document.getElementById(id);

function revealSidebar(target) {
  const group = target?.closest('[data-sidebar-group]');
  if (group) group.open = true;
}

function syncSidebarFocus() {
  const open = [...document.querySelectorAll('[data-sidebar-group]')].filter(group => group.open);
  document.querySelectorAll('[data-sidebar-focus]').forEach(button => {
    button.setAttribute('aria-pressed', String(open.length === 1 && open[0].dataset.sidebarGroup === button.dataset.sidebarFocus));
  });
}

document.querySelectorAll('[data-sidebar-group]').forEach(group => {
  const key = `doxograph-sidebar:${group.dataset.sidebarGroup}`;
  try { group.open = localStorage.getItem(key) !== 'closed'; } catch (_) { /* optional */ }
  group.addEventListener('toggle', () => {
    try { localStorage.setItem(key, group.open ? 'open' : 'closed'); } catch (_) { /* optional */ }
    syncSidebarFocus();
  });
});
document.querySelectorAll('[data-sidebar-focus]').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('[data-sidebar-group]').forEach(group => {
      group.open = group.dataset.sidebarGroup === button.dataset.sidebarFocus;
    });
    syncSidebarFocus();
    $('side').scrollTop = 0;
  });
});
syncSidebarFocus();
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// --- the URL says what is on screen ---------------------------------------
//
// Without this a reload lands back on the whole corpus, Back leaves the app,
// and there is no way to hand someone a link to one topic. Navigation pushes a
// history entry; a filter or a selection replaces one, so Back does not have to
// walk through every keystroke.
const HASH_VIEWS = ['library', 'claims', 'tensions', 'agreements', 'research', 'graph'];

function hashParams() {
  return new URLSearchParams((location.hash || '').replace(/^#/, ''));
}

function currentHash() {
  const params = new URLSearchParams();
  if (workspaceClient.workspaceId && workspaceClient.workspaceId !== 'default') params.set('ws', workspaceClient.workspaceId);
  params.set('view', V.view);
  if (V.paper) params.set('paper', V.paper);
  if (V.tag) params.set('tag', V.tag);
  if (V.label) params.set('label', V.label);
  if (V.q.trim()) params.set('q', V.q);
  if (V.kind) params.set('kind', V.kind);
  if (V.unreviewed) params.set('unreviewed', '1');
  if (V.unverified) params.set('unverified', '1');
  if (!V.group) params.set('group', '0');
  if (V.view === 'claims' && V.selectedId) params.set('sel', V.selectedId);
  if (V.tensionStatus) params.set('tstatus', V.tensionStatus);
  if (V.agreementStatus) params.set('astatus', V.agreementStatus);
  return params.toString();
}

// Set while a popped entry is being restored. Everything between the pop and
// the redraw — the workspace reset, its refresh, the render inside it — would
// otherwise write the state it is passing through back into the URL being read
// from, so an entry naming another workspace would lose its paper and filters
// before `applyHash` ever saw them.
let restoringHistory = false;

// A route can change away and back while a reader request waits. Record
// each transition, including history traversal, rather than comparing only
// the final paper/view. Background claim selection is not navigation.
let navigationSeq = 0, navigationRoute = null;
function navigationVersion() {
  const route = JSON.stringify([workspaceClient.workspaceId, V.view, V.paper, V.tag, V.label,
    V.q, V.kind, V.unreviewed, V.unverified, V.pdfHidden]);
  if (route !== navigationRoute) { navigationRoute = route; navigationSeq++; }
  return navigationSeq;
}

function syncHash(push = false) {
  navigationVersion();
  if (restoringHistory) return;
  const next = currentHash();
  if (next === hashParams().toString()) return;
  const url = `${location.pathname}${location.search}#${next}`;
  if (push) history.pushState(null, '', url);
  else history.replaceState(null, '', url);
}

// A status out of a URL is checked against the ones that exist, so a hand-edited
// address cannot leave a view filtered to nothing with no way to see why.
const STATUSES = ['open', 'confirmed', 'dismissed'];

function readStatus(value) {
  return STATUSES.includes(value) ? value : '';
}

// A paper, a topic or a kind named by a URL may be gone: at boot from a stale
// bookmark, or on Back to an entry from before it was removed. A paper's key is
// retired and will never come back; a topic can be renamed away; a kind can be
// hand-typed into the address or left over from an older version. Any of them
// would empty the list with nothing on screen to say why — and a topic the
// sidebar cannot list leaves nothing to click to get out of it, short of
// editing the address. The kind is worse still: the select has no option to
// match it, so it shows blank while filtering every claim away.
//
// Runs once the corpus is in `S`, which is why it is not part of `applyHash`:
// at boot the URL is read before the first read of the corpus comes back. Every
// read runs it — see `pull` — so a filter is dropped whenever the corpus stops
// holding what it names, not only when the reader arrives at one.
function dropMissingFilters() {
  if (V.paper && !S.papers.some((paper) => paper.key === V.paper)) V.paper = null;
  if (V.tag && !Object.hasOwn(S.tag_counts || {}, V.tag)) V.tag = null;
  // A label is dropped the same way, and for the same reason: the last paper
  // carrying it can be removed, or renamed out from under a bookmark, and the
  // sidebar hides the section entirely when no label is in use — which would
  // leave a filter on with nothing on screen to turn it off.
  // The filter follows the label to the spelling the sidebar shows now.
  if (V.label) V.label = findLabel(Object.keys(S.label_counts || {}), V.label) || null;
  if (V.kind && !(S.kinds || []).includes(V.kind)) {
    V.kind = '';
    $('kind').value = '';
  }
  // A paper on screen and a label filter that excludes it: the header would
  // stand there with every one of its claims filtered away. It happens when
  // the label is taken off the paper being read — here or in another window —
  // and from a hand-written address naming both. The paper wins, as it does on
  // every other path that opens one.
  keepLabelFor(V.paper);
}

// Sets the view from the URL without drawing it. The controls are set here too:
// they are the same state, and a filter the page has forgotten to tick is worse
// than no filter at all.
function applyHash() {
  const params = hashParams();
  // Preserve older links into claim review while making the library home.
  const legacyClaims = ['paper', 'sel', 'tag', 'q', 'label', 'kind', 'unreviewed', 'unverified', 'group'].some(key => params.has(key));
  const view = params.get('view') || (legacyClaims ? 'claims' : 'library');
  V.view = HASH_VIEWS.includes(view) ? view : 'library';
  V.paper = V.view === 'library' ? null : params.get('paper') || null;
  V.tag = params.get('tag') || null;
  V.label = params.get('label') || null;
  V.q = params.get('q') || '';
  V.kind = params.get('kind') || '';
  V.unreviewed = params.get('unreviewed') === '1';
  V.unverified = params.get('unverified') === '1';
  V.group = params.get('group') !== '0';
  V.selectedId = params.get('sel') || null;
  V.tensionStatus = readStatus(params.get('tstatus'));
  V.agreementStatus = readStatus(params.get('astatus'));
  V.editing = null;
  // The focus is a drill-down from a claim's marker rather than a filter the
  // reader chose, so it is not carried; the status they chose is.
  V.tensionFocus = null;
  V.agreementFocus = null;
  // A new claim belongs to the paper it was started on. Arriving at another
  // paper has to park it, exactly as clicking that paper in the list does, or
  // it is offered as a draft under a header it does not belong to and saving
  // it posts to the paper that is no longer on screen.
  parkNewClaimForNavigation(V.paper);
  $('q').value = V.q;
  // Typing is not the only way to arrive at a query: a bookmark or Back can
  // put one on screen, and the search over the papers' own text belongs to the
  // query rather than to the keystroke that happened to produce it.
  scheduleTextSearch();
  $('kind').value = V.kind;
  $('only-unreviewed').checked = V.unreviewed;
  $('only-unverified').checked = V.unverified;
  $('group-by-tag').checked = V.group;
}

// Restoring an entry can wait on a workspace switch, and Back or Forward held
// down arrives faster than that. Each pop takes a number and the newest one
// wins: an older one that wakes up afterwards has nothing left to say, and
// must not draw its entry over the newer one or clear the guard it is using.
let popSeq = 0;

window.addEventListener('popstate', async () => {
  navigationSeq++;
  const seq = ++popSeq;
  // A question still on screen belongs to the move this one supersedes —
  // Forward pressed while Back was asking about unsaved edits. Answering it
  // afterwards would switch the corpus for a move that is no longer where the
  // reader is, so it is withdrawn as a cancel: drafts kept, nothing switched.
  if (askResolve) settleAsk(null);
  // Moving through history is a view change like any other, and the editors
  // keep their text across it: `applyHash` writes straight to `V`, so the
  // bookkeeping `showView` would have done is done here.
  if (V.view === 'research') captureResearchDraft();
  captureOpenEditor();
  parkSynthEditor();
  parkNoteEditor();
  const wanted = hashParams().get('ws') || 'default';
  restoringHistory = true;
  try {
    // The workspace reset clears the filters; the URL being moved to puts back
    // whatever it holds, which is the whole point of going back to it.
    if (wanted !== workspaceClient.workspaceId) {
      if (workspaceClient.workspaces.some((w) => w.id === wanted)) await switchWorkspace(wanted);
      if (seq !== popSeq) return;
      // The switch can be refused — a change still in flight, or the reader
      // keeping their drafts — and a workspace this page does not know is
      // never attempted at all. The entry then describes a corpus the page is
      // not in, and applying its paper and filters to the one it is in would
      // be worse than ignoring it: a key that exists in both corpora would
      // open the wrong paper, which `dropMissingFilters` cannot catch.
      if (workspaceClient.workspaceId !== wanted) return;
    } else if (workspaceController.pending) {
      // A switch is running and this entry belongs to where the page is now —
      // but `workspaceClient.workspaceId` only changes once that switch's own flush is
      // done, so the switch may be on its way somewhere else entirely. Wait
      // for it, then ask again where it left us.
      await workspaceController.pending;
      if (seq !== popSeq) return;
      if (workspaceClient.workspaceId !== wanted) return;
    }
    applyHash();
    dropMissingFilters();
  } finally {
    // Every exit redraws, including the refused one, and the redraw writes the
    // URL back to what is on screen: an entry the page did not take must not
    // be left standing as the address. A superseded pop does neither — the one
    // that overtook it owns both.
    if (seq === popSeq) {
      restoringHistory = false;
      renderAll();
    }
  }
});

const api = (path, options) => workspaceClient.request(path, options);

async function pull() {
  const workspace = workspaceClient.workspaceId;
  let result;
  try {
    result = await workspaceClient.pull();
  } catch (error) {
    if (libraryInitialized && workspace === workspaceClient.workspaceId) {
      $('sync-message').textContent = `Library updates paused: ${error.message} Retrying automatically.`;
      $('sync-status').hidden = false;
    }
    throw error;
  }
  // Obsolete workspace reads must not render a destination still loading.
  // A newer ready snapshot in the same workspace can satisfy a forced redraw.
  if (!result.applied) return result.workspaceChanged || workspaceClient.state.workspace?.id !== workspace ? null : false;
  $('sync-status').hidden = true;
  S = workspaceClient.state;
  if (result.registryChanged) renderWorkspacePicker();
  pruneTrashed();   // a delete that has not been sent yet is already gone here
  // And the view is checked against the corpus that is left, which is where a
  // filter naming something that has gone is dropped. Here rather than only at
  // boot and on Back, because the corpus can lose a paper or a topic under a
  // page that is sitting still: the poll picking up a rename or a removal made
  // in another window, or the refresh that follows this page's own delete
  // finally going out — the topic whose last claim it took is gone from the
  // count, the sidebar entry the hold kept alive goes with it, and the filter
  // has to go too or it outlives the Undo it was being kept for.
  dropMissingFilters();
  // A paper deleted in another window can never be reopened. Do not leave
  // its unreachable drafts counted as unsaved edits on workspace switches.
  const papers = new Set(S.papers.map((paper) => paper.key));
  for (const key of Object.keys(V.parkedNewClaims)) {
    if (!papers.has(key)) delete V.parkedNewClaims[key];
  }
  return result.changed;
}

// What has to be asked again when the corpus moves. Every path that pulls
// state calls this, not just the poll: a drop or an import refreshes through
// its own handler, which consumes the change and commits its ETag, so the
// next poll is told nothing happened.
function corpusChanged() {
  window.FocusedReview?.sync();
  rerunTextSearch();     // a paper imported since holds the query's words too
  closeSimilar();        // and the claims it was compared against have moved
  // A PDF that arrived while the map is open cites what it cites; the papers
  // are redrawn from state but the arrows are fetched on their own.
  if (V.view === 'graph') loadCitations();
}

async function refresh() {
  const requestedWorkspace = workspaceClient.workspaceId;
  const changed = await pull();
  if (changed === null || requestedWorkspace !== workspaceClient.workspaceId) return;
  if (changed) corpusChanged();
  render();
}

// `refresh` goes through `render`, which leaves an open editor — and therefore
// the whole claim list — alone. Anything that adds or removes a row has to
// rebuild the list even while an editor is open, or the deleted row stays on
// screen and stays clickable. This keeps the open editor's draft across it.
async function refreshAll() {
  captureOpenEditor();
  const requestedWorkspace = workspaceClient.workspaceId;
  const changed = await pull();
  if (changed === null || requestedWorkspace !== workspaceClient.workspaceId) return;
  if (changed) corpusChanged();
  renderAll();
}

// The redraw after an action whose refusal is in `V.error`. A server that
// refused the action may refuse the read after it as well, and a refresh that
// throws leaves before its redraw: the warning would never be drawn and the
// click would end in an unhandled rejection. The poll brings the corpus.
async function refreshAllShowingError() {
  try {
    await refreshAll();
  } catch (error) {
    renderAll();
  }
}

// The same for an action that redraws through `render`.
async function refreshShowingError() {
  try {
    await refresh();
  } catch (error) {
    render();
  }
}

function currentWorkspace() {
  return workspaceClient.currentWorkspace();
}

function workspaceQuery() {
  return `workspace=${encodeURIComponent(workspaceClient.workspaceId || 'default')}`;
}

function renderWorkspacePicker() {
  $('workspace').innerHTML = workspaceClient.workspaces.map((workspace) =>
    `<option value="${esc(workspace.id)}">${esc(workspace.name)}</option>`).join('');
  $('workspace').value = workspaceClient.workspaceId || 'default';
  const selected = currentWorkspace();
  $('btn-workspace-rename').disabled = !selected;
  $('btn-workspace-delete').disabled = !selected || selected.id === 'default';
  $('btn-workspace-delete').title = selected?.id === 'default' ? 'The default workspace cannot be deleted.' : '';
  document.title = selected ? `${selected.name} — Doxograph` : 'Doxograph';
  window.doxographWorkspaceId = workspaceClient.workspaceId || 'default';
}

function resetWorkspaceView() {
  S = workspaceClient.state;
  $('sync-status').hidden = true;
  Object.assign(V, {
    paper: null, tag: null, label: null, q: '', kind: '', unreviewed: false, unverified: false, group: true,
    editing: null, selectedId: null, newClaim: null, parkedNewClaims: {}, drafts: {},
    error: null, view: 'library', tensionStatus: '', tensionFocus: null, agreementStatus: '', agreementFocus: null,
    synthEditing: null, synthDrafts: {}, synthSaving: null, researchSaving: false, researchDraft: null, researchBase: null,
    noteEditing: null, noteDrafts: {}, noteSaving: null,
    graph: { xAxis: 'connections', topics: true, minShared: null, tensions: true, ledger: true, cites: true },
    quoteContext: null, textSearch: null, similar: null,
  });
  savingClaims.clear();
  dropTextSearch();
  CITATIONS = [];
  graphReset();
  $('q').value = '';
  $('kind').value = '';
  $('only-unreviewed').checked = false;
  $('only-unverified').checked = false;
  $('group-by-tag').checked = true;
  $('import-status').hidden = true;
  closePaperMenu();
}

function hasWorkspaceDrafts() {
  return Boolean(V.editing || V.synthEditing || V.noteEditing || V.newClaim
    || Object.keys(V.parkedNewClaims).length || Object.keys(V.drafts).length
    || Object.keys(V.synthDrafts).length || Object.keys(V.noteDrafts).length || researchFormDirty());
}

function canRestoreSavedVersion() {
  return !hasWorkspaceDrafts() && !workspaceClient.pendingMutations && !savingClaims.size
    && !V.synthSaving && !V.noteSaving && !V.researchSaving && !trash.size
    && !WorkspaceNotes.hasUnsavedChanges() && !ResearchNotes.hasUnsavedChanges() && !ResearchTools.hasUnsavedChanges();
}

const workspaceController = createWorkspaceController(workspaceClient, {
  async prepare() {
    return await WorkspaceNotes.beforeWorkspaceSwitch()
      && await ResearchNotes.beforeWorkspaceSwitch() && await ResearchTools.beforeWorkspaceSwitch();
  },
  showCurrent: renderWorkspacePicker,
  canLeave() {
    if (deletingWorkspace) return false;
    if (workspaceClient.pendingMutations || savingClaims.size || V.synthSaving || V.noteSaving || V.researchSaving) {
      toast('Wait for the current change to finish before switching workspaces.', { tone: 'warn' });
      return false;
    }
    return true;
  },
  async confirmLeave() {
    return !hasWorkspaceDrafts() || await confirmDialog('Switch workspaces and discard unsaved edits in this workspace?',
                                           { ok: 'Discard and switch' });
  },
  beforeLeave: flushTrash,
  beforeSelect: () => WorkspaceNotes.beforeWorkspaceSwitch(),
  async enter() {
    resetWorkspaceView();
    // Selection cleared the client snapshot. Retire the previous workspace's
    // editor before the destination read can pause or fail.
    WorkspaceNotes.sync({view:V.view, paper:V.paper});
    renderWorkspacePicker();
    syncHash(true);
    await refresh();
    $('kind').innerHTML = '<option value="">every kind</option>'
      + S.kinds.map(kind => `<option value="${esc(kind)}">${esc(kind)}</option>`).join('');
  },
});
const switchWorkspace = id => workspaceController.switchTo(id);

async function loadWorkspaces() {
  await workspaceClient.loadWorkspaces();
  S = workspaceClient.state;
  renderWorkspacePicker();
}

// --- rendering ------------------------------------------------------------

// `render` leaves an open editor alone: it is what the background poll calls,
// and rebuilding under the cursor would move focus.
function render() {
  renderStats();
  renderPapers();
  renderTensionsNav();
  renderAgreementsNav();
  renderResearchNav();
  renderGraphNav();
  renderTags();
  renderLabels();
  renderActiveFilters();
  if (!editorHolds()) renderContent();
  renderJobs();
  syncAnalysisControls();
  ResearchTools.sync();
  WorkspaceNotes.sync({view:V.view, paper:V.paper});
  CommandPalette.sync();
}

// Whether something is being typed into the content pane. A poll redrawing it
// would rebuild the form under the cursor and drop focus, so every background
// path asks this — one function rather than a condition written out at each
// of them, which is how the poll in `boot` came to know about one editor
// fewer than `render` did.
function editorHolds() {
  return Boolean(V.editing || V.synthEditing || V.noteEditing) || V.view === 'research' || ResearchTools.holds();
}

// `renderAll` is for a view change the user asked for. The draft is captured
// first, so rebuilding the editor is safe, and the claim list has to be rebuilt
// or a filter would change the sidebar without changing what is listed.
function renderAll() {
  captureOpenEditor();
  renderStats();
  renderPapers();
  renderTensionsNav();
  renderAgreementsNav();
  renderResearchNav();
  renderGraphNav();
  renderTags();
  renderLabels();
  renderContent();
  renderJobs();
  syncAnalysisControls();
  ResearchTools.sync();
  WorkspaceNotes.sync({view:V.view, paper:V.paper});
  CommandPalette.sync();
  syncHash();
}

function renderStats() {
  renderStorageWarnings();
  renderContinuation();
  renderPaperReadingProgress();
  const unreviewed = S.claims.filter((c) => !c.reviewed).length;
  const proposed = S.papers.reduce((n, p) => n + (p.n_proposed_tags || 0), 0);
  const bits = [
    `${S.papers.length} papers`,
    `${S.claims.length} claims`,
    `${Object.keys(S.tag_counts).length} topics`,
  ];
  if (unreviewed) bits.push(`${unreviewed} unreviewed`);
  const unverified = S.claims.filter((c) => c.quote_verified === false).length;
  if (unverified) bits.push(`${unverified} quotes not found`);
  if (proposed) bits.push(`${proposed} proposed topics`);
  const openTensions = (S.tensions || []).filter((t) => t.status === 'open').length;
  if (openTensions) bits.push(`${openTensions} open tensions`);
  const openAgreements = (S.agreements || []).filter((a) => a.status === 'open').length;
  if (openAgreements) bits.push(`${openAgreements} open agreements`);
  const staleSyntheses = (S.syntheses || []).filter((s) => s.stale).length;
  if (staleSyntheses) bits.push(`${staleSyntheses} stale syntheses`);
  if (S.ai_enabled === false) bits.push('AI analysis off');
  else if (S.ai_provider === 'claude_code') bits.push('Claude subscription');
  else if (S.ai_provider === 'codex') bits.push('ChatGPT subscription');
  else if (!S.has_key && !['ollama', 'openai-compatible'].includes(S.provider)) bits.push('no API key found');
  $('stats').textContent = bits.join(' · ');
}

function renderStorageWarnings() {
  const box = $('storage-warning');
  const issues = S.storage_issues || [];
  const signature = JSON.stringify(issues);
  if (box.dataset.issues === signature) return;
  box.dataset.issues = signature;
  box.hidden = !issues.length;
  box.innerHTML = issues.length ? `<strong>Some stored research needs attention</strong>
    <ul>${issues.map((issue) => `<li>${issue.paper
      ? `Paper ${esc(issue.paper)} could not be loaded. Its saved file has been left untouched; repair it or restore it from a backup.`
      : esc(issue.detail)}
      <details><summary>File details</summary><code>${esc(issue.path)}</code>
        ${issue.paper ? `<p>${esc(issue.detail)}</p>` : ''}</details></li>`).join('')}</ul>` : '';
}

function showView(view) {
  const destination = {library: 'papers', claims: 'papers', graph: 'graph-nav', research: 'research-nav',
    tensions: 'tensions-nav', agreements: 'agreements-nav'}[view];
  if (destination) revealSidebar($(destination));
  if (view === V.view) return;
  if (V.view === 'research') captureResearchDraft();
  // Neither the tensions view nor the map has an editor. Park any open one
  // rather than leaving `V.editing` set on a form that is no longer on screen,
  // which would also stop background content updates. The same for a synthesis editor.
  captureOpenEditor();
  V.editing = null;
  parkSynthEditor();
  parkNoteEditor();
  V.error = null;
  if (view === 'library') {
    parkNewClaimForNavigation(null);
    V.paper = null;
    V.selectedId = null;
  }
  V.view = view;
  // A query started in claim review may only have fetched its first PDF page.
  if (view === 'library' && V.textSearch && !V.textSearch.complete) rerunTextSearch();
  if (view !== 'tensions') V.tensionFocus = null;
  if (view !== 'agreements') V.agreementFocus = null;
}

// Every path through the draw settles `V` first — the fallback selection, the
// parked editors — so the URL is written once the drawing is done rather than
// once per branch.
function renderContent() {
  // A redraw replaces the note's textarea with a fresh one built from the
  // draft, so the text survives but the cursor does not — and a redraw can
  // arrive from a text search, a lookalike or a passage that was asked for
  // before the note was opened. Standing those off would keep answers the
  // reader did ask for off the screen, so the cursor is put back instead.
  const cursor = noteCursor();
  const actionFocus = paperActionCursor();
  drawContent();
  const recovery = ResearchTools.recoveryHtml();
  if (recovery) $('content').insertAdjacentHTML('afterbegin', recovery);
  renderActiveFilters();
  ResearchTools.syncSelectionControls();
  restoreNoteCursor(cursor);
  restorePaperActionCursor(actionFocus);
  renderContinuation();
  WorkspaceNotes.sync({view:V.view, paper:V.paper});
  syncHash();
}

// Polling can refresh the header. Keep keyboard focus on the same reading
// action or menu command, rather than dropping it back onto the document.
function paperActionCursor() {
  const field = document.activeElement;
  const actions = field?.closest('.reading-actions');
  if (!actions) return null;
  const attribute = ['data-act', 'data-tool', 'data-paper-chat', 'data-reader-command', 'href']
    .find(name => field.hasAttribute(name));
  return {paper: actions.dataset.paper,
    selector: attribute ? `[${attribute}="${CSS.escape(field.getAttribute(attribute))}"]` : 'summary'};
}
function restorePaperActionCursor(cursor) {
  if (!cursor) return;
  const actions = document.querySelector(`.reading-actions[data-paper="${CSS.escape(cursor.paper)}"]`);
  (actions?.querySelector(cursor.selector) || actions?.querySelector('summary'))?.focus({preventScroll: true});
}

function noteCursor() {
  const field = document.activeElement;
  if (!field || !field.matches || !field.matches('textarea[data-note]')) return null;
  return { key: field.dataset.note, start: field.selectionStart, end: field.selectionEnd };
}

function restoreNoteCursor(cursor) {
  if (!cursor) return;
  const field = document.querySelector(`textarea[data-note="${CSS.escape(cursor.key)}"]`);
  if (!field) return;
  field.focus();
  field.setSelectionRange(cursor.start, cursor.end);
}

function drawContent() {
  const reading = readingPaper();
  layoutPanes(reading);
  const library = V.view === 'library';
  document.body.classList.toggle('paper-library-view', library);
  $('library-view-switch').hidden = Boolean(V.paper) || !['library', 'claims'].includes(V.view);
  $('btn-library-view').setAttribute('aria-pressed', String(library));
  $('btn-claims-view').setAttribute('aria-pressed', String(V.view === 'claims'));
  $('q').placeholder = library ? 'Search papers…' : 'Search claims, quotes, and papers…';
  if (V.view !== 'claims') $('paperhead').innerHTML = '';
  if (V.view === 'graph') { renderGraph(); return; }
  graphStop();   // leaving the map, or never on it: no animation loop off screen
  if (library) { renderLibrary(); return; }
  if (V.view === 'tensions') { renderTensions(); return; }
  if (V.view === 'research') { renderResearch(); return; }
  if (V.view === 'agreements') { renderAgreements(); return; }
  const main = $('main');
  const scrollTop = main ? main.scrollTop : 0;
  const shown = new Set();
  const card = (row, group = '') => claimCard(row, shown, group);
  const rows = visibleClaims();
  // An editor for a claim the current filters exclude cannot be drawn, and
  // leaving `V.editing` set would also stop background content updates, which
  // stand aside whenever an editor is open. Park it: keep the draft, close
  // the editor. The text is read from the form that is still on screen.
  if (V.editing && V.editing !== NEW_CLAIM_ID && !rows.some((row) => row.id === V.editing)) {
    captureOpenEditor();
    V.editing = null;
  }
  // Likewise a synthesis editor whose topic heading is not about to be drawn:
  // under a paper there are no topic headings, and a filter can empty a group.
  if (V.synthEditing && !synthesisTopicsOnScreen(rows).has(V.synthEditing)) parkSynthEditor();
  // A note editor lives in the header of the paper it belongs to, so leaving
  // that paper — or the paper being removed — takes it off screen.
  if (V.noteEditing && V.noteEditing !== V.paper) parkNoteEditor();
  if (!rows.some((row) => row.id === V.selectedId)) V.selectedId = rows.length ? rows[0].id : null;
  $('paperhead').innerHTML = V.paper ? paperHeader(V.paper, Boolean(reading)) : '';
  let html = '';
  if (V.error) html += `<p class="warn">${esc(V.error)}</p>`;
  // Filtered to one topic without grouping, the synthesis heads the list; in
  // grouped mode each topic's sits under its own heading below.
  if (V.tag && !V.paper && !(V.group && rows.length)) {
    html += `<div class="group"><h3>${esc(V.tag)} ${synthesizeButton(V.tag)}</h3>${synthesisBlock(V.tag)}</div>`;
  }
  if (V.newClaim && V.editing === NEW_CLAIM_ID) {
    shown.add(NEW_CLAIM_ID);
    html += editForm(V.newClaim);
  } else if (newClaimHasEdits(V.newClaim)) {
    // Held but not open — opening another claim's editor moved `V.editing`.
    // Without this the draft is unreachable: Add claim would overwrite it and
    // cancelling the other editor would drop it, losing the text silently.
    html += `<div class="proposed">
      <span>Unsaved new claim: <em>${esc(V.newClaim.text.slice(0, 80) || 'Details entered; claim text is still empty.')}${V.newClaim.text.length > 80 ? '…' : ''}</em></span>
      <span style="margin-left:auto"></span>
      <button type="button" data-act="resume-new">Resume</button>
      <button type="button" data-act="discard-new">Discard</button>
    </div>`;
  }
  // Keep the overview and personal notes together in either paper layout.
  // Highlights stay listed with the PDF put away; clicking one reopens it.
  if (V.paper) {
    const paper = S.papers.find((p) => p.key === V.paper);
    if (paper) html += paperOverview(paper);
    const marked = (S.notebook?.highlights || []).some((h) => h.paper === V.paper);
    if (paper && (paper.has_pdf || marked)) html += ResearchTools.highlightsHtml(V.paper);
  }

  if (!rows.length) {
    if (!V.newClaim) {
      html += S.papers.length
        ? `<p class="empty">${reading && !S.claims.some(c => c.paper === V.paper) ? 'Capture a passage from the paper to start collecting claims.' : 'No claims match these filters.'}</p>`
        : '<p class="empty">Nothing here yet. Paste an arXiv ID or drop a PDF to start.</p>';
    }
    $('content').innerHTML = html + textSearchBlock();
  applySavingState();
    if (main) main.scrollTop = scrollTop;
    return;
  }

  if (V.group && !V.paper) {
    const byTag = new Map();
    const untagged = [];
    rows.forEach((row) => {
      if (!(row.tags || []).length) { untagged.push(row); return; }
      row.tags.forEach((tag) => {
        if (!byTag.has(tag)) byTag.set(tag, []);
        byTag.get(tag).push(row);
      });
    });
    const ordered = [...byTag.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
    for (const [tag, group] of ordered) {
      const description = (S.tags.find((t) => t.name === tag) || {}).description || '';
      html += `<div class="group"><h3>${esc(tag)} <span class="n hint">${group.length}</span>${synthesizeButton(tag)}</h3>`
        + (description ? `<p class="gd">${esc(description)}</p>` : '')
        + synthesisBlock(tag)
        + group.map((row) => card(row, tag)).join('') + '</div>';
    }
    if (untagged.length) {
      html += `<div class="group"><h3>untagged <span class="n hint">${untagged.length}</span></h3>`
        + untagged.map((row) => card(row, 'untagged')).join('') + '</div>';
    }
  } else {
    // Not `rows.map(card)`: the index `map` passes would land in `group`, and
    // the buttons that carry it back as a string would never match it again.
    html += rows.map((row) => card(row)).join('');
  }
  $('content').innerHTML = html + textSearchBlock();
  applySavingState();
  if (main) main.scrollTop = scrollTop;
}

$('panes').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-act]');
  if (button) {
    const act = button.dataset.act;
    const paper = button.dataset.paper;
    const claim = button.dataset.claim;
    if (act === 'edit') {
      // Opening another claim's editor redraws the list, so keep whatever is in
      // the one currently open. Drafts are per claim, so both survive.
      captureOpenEditor();
      V.editing = claim;
      renderContent();
      return;
    }
    if (act === 'cancel') { cancelEdit(); return; }
    if (act === 'add-ledger') {
      const rows = $('ledger-rows');
      const count = rows.querySelectorAll('[data-ledger-row]').length;
      // The first id nobody uses, not the row count: L1 and L3 get L2, not a second L3.
      const taken = new Set([...rows.querySelectorAll('[name="ledger-id"]')].map((f) => f.value.trim()));
      let n = 1;
      while (taken.has(`L${n}`)) n += 1;
      rows.insertAdjacentHTML('beforeend', ledgerRow({ id: `L${n}`, text: '' }, count));
      rows.lastElementChild.querySelector('[name="ledger-text"]').focus();
      return;
    }
    if (act === 'drop-ledger') { button.closest('[data-ledger-row]').remove(); return; }
    if (act === 'cancel-research') {
      V.error = null;
      showView('claims');
      V.researchDraft = null;   // Cancel is the one way out that drops the edits
      V.researchBase = null;
      syncHash(true);   // leaving the form is a move; Back goes back to it
      renderAll();
      return;
    }
    if (act === 'drop-link') {
      button.closest('.linkrow').querySelector('[name="link-claim"]').value = '';
      button.closest('.linkrow').style.display = 'none';
      return;
    }
    if (act === 'review') {
      await toggleReviewed(S.claims.find((c) => c.id === claim));
      return;
    }
    if (act === 'similar') {
      const group = button.dataset.group || '';
      if (similarOpen(claim, group)) { closeSimilar(); captureOpenEditor(); renderContent(); return; }
      await showSimilar(paper, claim, group);
      return;
    }
    if (act === 'copy-quote') {
      const row = S.claims.find((c) => c.id === claim);
      if (row) await copyText(row.quote, 'Quote copied.');
      return;
    }
    if (act === 'quote-context') {
      const group = button.dataset.group || '';
      if (V.quoteContext && V.quoteContext.claim === claim
          && (V.quoteContext.group || '') === group) {
        V.quoteContext = null;
        captureOpenEditor();
        renderContent();
        return;
      }
      await showQuoteContext(paper, claim, group);
      return;
    }
    if (act === 'use-wording') {
      await usePaperWording(paper, claim);
      return;
    }
    if (act === 'review-all') {
      // Covered by the frozen claim ids below as well — the button is only
      // drawn when the paper has an unreviewed claim, and that claim is one of
      // them — but said here too, so the reason the reader is given is the
      // removal rather than a change in flight they did not make.
      if (refuseWhileRemoving(paper, 'the reviews it writes')) return;
      await reviewWholePaper(paper);
      return;
    }
    if (act === 'del') {
      // Frozen means a request for this claim is already in flight — a save, a
      // review toggle, or the removal of the paper it belongs to. Holding a
      // delete for it now would put a row in the trash that the request coming
      // back will contradict: an Undo with nothing to restore, and a DELETE
      // sent eight seconds later against a claim that has since gone.
      if (isSaving(claim)) {
        toast('Wait for the change in flight to finish, then delete the claim.',
              { tone: 'warn' });
        return;
      }
      // The freeze above names the claims the paper had when its removal
      // settled its held deletes. A claim the poll has brought in since — a
      // re-read that finished while the DELETE was in flight — is not among
      // them, and holding a delete for it would leave a notice offering to
      // undo a claim that its paper is about to take anyway.
      if (refuseWhileRemoving(paper, 'the delete it would hold')) return;
      // In grouped mode the same claim can be an editor in one topic group and
      // a plain card with a Delete button in another. Leaving `V.editing` set
      // would keep the deleted claim's form on screen, because `render()` skips
      // the content while an editor is open, and every Save would 404.
      delete V.drafts[claim];
      if (V.editing === claim) { V.editing = null; V.error = null; }
      if (V.selectedId === claim) V.selectedId = null;
      // `deleteLater` rebuilds the list: without it the deleted card stays
      // visible and clickable even when the open editor is a different claim's.
      await deleteLater('claim', claim,
                        `/api/papers/${encodeURIComponent(paper)}/claims/${encodeURIComponent(claim)}`,
                        'the claim', { paper });
      return;
    }
    if (act === 'library-open-paper') {
      // Preserve the anchor's native open-in-new-tab behavior.
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      V.pdfHidden = false;
      storePdfHidden();
      V.q = ''; V.kind = ''; V.unreviewed = false; V.unverified = false;
      $('q').value = ''; $('kind').value = '';
      $('only-unreviewed').checked = false; $('only-unverified').checked = false;
      dropTextSearch();
      openPaper(paper);
      $('main').scrollTop = 0;
      return;
    }
    if (act === 'open-paper') {
      openPaper(paper);
      return;
    }
    if (act === 'tension-focus') {
      showView('tensions');
      V.tensionFocus = claim;
      V.tensionStatus = '';
      syncHash(true);
      renderAll();
      return;
    }
    if (act === 'tension-unfocus') {
      V.tensionFocus = null;
      renderContent();
      return;
    }
    if (act === 'tension-status') {
      V.error = null;
      try {
        await api(`/api/tensions/${encodeURIComponent(button.dataset.tension)}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: button.dataset.status }),
        });
      } catch (error) {
        V.error = `Could not update the tension: ${error.message}`;
      }
      await refreshAllShowingError();
      return;
    }
    if (act === 'agreement-focus') {
      showView('agreements');
      V.agreementFocus = claim;
      V.agreementStatus = '';
      syncHash(true);
      renderAll();
      return;
    }
    if (act === 'agreement-unfocus') { V.agreementFocus = null; renderContent(); return; }
    if (act === 'agreement-status') {
      V.error = null;
      // Deciding an agreement rewrites the record from the claims on file:
      // `set_agreement_status` drops the members that have gone and
      // fingerprints the ones that are left, "what the reviewer saw and
      // judged". A claim waiting out its undo window is off the card but
      // still on file, so it would be written back into the group as a member
      // of a decision nobody made about it, and the moment the timer sent the
      // delete the agreement would read stale again with a deleted id in its
      // fingerprints. A group that keeps two papers without the held claim
      // stays on screen and stays clickable, so this is reachable; a tension
      // needs both of its claims and leaves the page entirely, which is why
      // `tension-status` above has nothing to settle.
      const headers = await settleDeletes();
      if (!headers) return;   // the delete failed; the record would name the claim
      try {
        await api(`/api/agreements/${encodeURIComponent(button.dataset.agreement)}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json', ...headers },
          body: JSON.stringify({ status: button.dataset.status }),
        });
      } catch (error) {
        V.error = `Could not update the agreement: ${error.message}`;
      }
      await refreshAllShowingError();
      return;
    }
    if (act === 'agreement-delete') {
      const id = button.dataset.agreement;
      await deleteLater('agreement', id, `/api/agreements/${encodeURIComponent(id)}`, 'the agreement');
      return;
    }
    if (act === 'find-agreements') { await findAgreements(); return; }
    if (act === 'find-tensions') {
      await findTensions();
      return;
    }
    if (act === 'synthesize') {
      await synthesize([button.dataset.topic], button.dataset.force === '1');
      return;
    }
    if (act === 'edit-synth') {
      // Opening one topic's synthesis editor while another's is open parks
      // that one, as opening a second claim's editor keeps the first's draft.
      captureOpenEditor();
      parkSynthEditor();
      V.synthEditing = button.dataset.topic;
      renderContent();
      return;
    }
    if (act === 'edit-note') {
      // As Add claim does: opening the box would invite writing that the
      // removal behind it is about to discard.
      if (refuseWhileRemoving(button.dataset.paper, 'a note')) return;
      // As with a synthesis: opening one note's editor parks any other, and a
      // claim editor's text lives in the DOM until it is read.
      captureOpenEditor();
      parkNoteEditor();
      V.noteEditing = button.dataset.paper;
      renderContent();
      const field = document.querySelector(`textarea[data-note="${CSS.escape(V.noteEditing)}"]`);
      if (field) field.focus();
      return;
    }
    if (act === 'cancel-note') { cancelNoteEdit(); return; }
    if (act === 'save-note') { await saveNote(button.dataset.paper); return; }
    if (act === 'cancel-synth') { cancelSynthEdit(); return; }
    if (act === 'save-synth') {
      if (V.synthSaving) return;   // a save is in flight
      const topic = button.dataset.topic;
      const field = document.querySelector(`textarea[data-synth="${CSS.escape(topic)}"]`);
      const text = field ? field.value : (V.synthDrafts[topic] || '');
      V.error = null;
      captureOpenEditor();
      // A correction by hand is a judgment about the claims as they stand, and
      // `set_synthesis_text` fingerprints the ones on file rather than the ones
      // on screen. A claim waiting out its undo window is still on file, so the
      // basis would record a claim the reader cannot see and the synthesis
      // would go stale — with a deleted id in its basis — the moment the timer
      // sent the delete. The held deletes go first, as they do before a
      // rewrite, a retag or an export.
      //
      // The settle ends in a read and a redraw, so what was typed is parked
      // first: the textarea is drawn from the draft, and the value in the DOM
      // would otherwise be replaced by the text on file.
      V.synthDrafts[topic] = text;
      // Frozen from here rather than from the request: the settle is a wait
      // like any other, and a second click during it would send the flush and
      // the PATCH twice over.
      V.synthSaving = topic;
      renderContent();
      try {
        const headers = await settleDeletes();
        // The held delete failed. The claim is still on file, the reader has
        // been told so, and a basis recorded against it would be a judgment
        // about a corpus nobody can see.
        if (!headers) return;
        await api(`/api/syntheses/${encodeURIComponent(topic)}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json', ...headers },
          body: JSON.stringify({ text }),
        });
        V.synthEditing = null;
        delete V.synthDrafts[topic];
      } catch (error) {
        V.synthDrafts[topic] = text;   // keep what was typed so the save can be retried
        V.error = `Could not save the synthesis: ${error.message}`;
      } finally {
        // In the `finally` so the editor is handed back on the way out of a
        // settle that failed too: that path leaves without a request, and a
        // redraw it did not run would leave the editor frozen with nothing
        // left to unfreeze it.
        V.synthSaving = null;
        await refreshAllShowingError();
      }
      return;
    }
    if (act === 'del-synth') {
      const topic = button.dataset.topic;
      // The draft goes with it, open or parked. Kept, it would come back under
      // a synthesis written later and overwrite it with text belonging to the
      // one that was deleted.
      if (V.synthEditing === topic) V.synthEditing = null;
      delete V.synthDrafts[topic];
      await deleteLater('synthesis', topic, `/api/syntheses/${encodeURIComponent(topic)}`,
                        `the synthesis of #${topic}`);
      return;
    }
    if (act === 'goto-claim') {
      // A citation is a link to its claim. A filter that hides the claim would
      // make `renderContent` fall back to the first visible card, so any
      // filter excluding it is cleared first; the others are kept. The paper
      // filter never applies, since no synthesis is drawn under one.
      const row = S.claims.find((c) => c.id === claim);
      if (row && !visibleClaims().some((c) => c.id === claim)) {
        if (V.paper && row.paper !== V.paper) V.paper = null;
        if (V.tag && !(row.tags || []).includes(V.tag)) V.tag = null;
        keepLabelFor(row.paper);
        if (V.kind && row.kind !== V.kind) { V.kind = ''; $('kind').value = ''; }
        if (V.unreviewed && row.reviewed) { V.unreviewed = false; $('only-unreviewed').checked = false; }
        if (V.unverified && row.quote_verified !== false) { V.unverified = false; $('only-unverified').checked = false; }
        if (V.q.trim() && !queryMatcher()(haystack(row))) { V.q = ''; $('q').value = ''; }
      }
      selectClaim(claim);
      syncHash(true);
      renderAll();   // a cleared topic or paper filter changes the sidebar too
      scrollToSelected();
      return;
    }
    if (act === 'reextract') {
      if (refuseWhileRemoving(paper, 'the claims it reads out')) return;
      const workspace = await settleDeletes();
      if (!workspace) return;   // the delete failed; the pass would read the claim
      // The flush is a wait, and the header stays live across it: a removal
      // answered during it holds the paper from before its own flush, and the
      // check above is too old to have seen it. Asked again, against the guard
      // as it stands now.
      if (refuseWhileRemoving(paper, 'the claims it reads out')) return;
      V.error = null;
      try {
        await api(`/api/papers/${encodeURIComponent(paper)}/extract`, {
          method: 'POST', headers: workspace,
        });
      } catch (error) {
        V.error = `Could not re-read the paper: ${error.message}`;
      }
      await refreshAllShowingError();
      return;
    }
    if (act === 'verify') {
      if (refuseWhileRemoving(paper, 'the check it writes on each quote')) return;
      V.error = null;
      try {
        await api(`/api/papers/${encodeURIComponent(paper)}/verify`, { method: 'POST' });
      } catch (error) {
        V.error = `Could not check the quotes: ${error.message}`;
      }
      await refreshAllShowingError();
      return;
    }
    if (act === 'retag-one') {
      if (refuseWhileRemoving(paper, 'the topics it writes on the claims')) return;
      const workspace = await settleDeletes();
      if (!workspace) return;   // the delete failed; the pass would read the claim
      // Asked again on the far side of the flush, as the re-read does: a
      // removal answered while it ran took the paper after the check above.
      if (refuseWhileRemoving(paper, 'the topics it writes on the claims')) return;
      V.error = null;
      try {
        await api('/api/retag', {
          method: 'POST', headers: { 'Content-Type': 'application/json', ...workspace },
          body: JSON.stringify({ keys: [paper] }),
        });
      } catch (error) {
        V.error = `Could not retag the claims: ${error.message}`;
      }
      await refreshAllShowingError();
      return;
    }
    if (act === 'resume-new') {
      captureOpenEditor();
      V.editing = NEW_CLAIM_ID;
      renderContent();
      return;
    }
    if (act === 'discard-new') {
      // An existing claim's editor may be open alongside the banner; keep what
      // is typed in it rather than redrawing from the older snapshot.
      captureOpenEditor();
      V.newClaim = null;
      if (V.editing === NEW_CLAIM_ID) V.editing = null;
      renderContent();
      return;
    }
    if (act === 'add-claim') {
      // The header stays on screen for the length of the removal, so this is
      // still clickable. Opening the form would invite a claim the DELETE
      // behind it is about to discard.
      if (refuseWhileRemoving(paper, 'a new claim')) return;
      setReadingClaims(false);
      captureOpenEditor();
      // Resume a held draft rather than overwriting what was typed into it.
      if (!V.newClaim || V.newClaim.paper !== paper || !newClaimHasEdits(V.newClaim)) {
        V.newClaim = blankClaim(paper);
      }
      V.editing = NEW_CLAIM_ID;
      renderContent();
      return;
    }
    if (act === 'toggle-pdf') {
      V.pdfHidden = !V.pdfHidden;
      storePdfHidden();
      renderContent();
      return;
    }
    if (act === 'edit-title') {
      await editPaperTitle(paper);
      return;
    }
    if (act === 'labels') {
      // Nothing to guard against a removal in flight: the dialog is modal, and
      // a PATCH landing on a paper that has gone comes back 404 and says so.
      await editLabels(paper);
      return;
    }
    if (act === 'del-paper') {
      // The button goes with the header, which stands until the DELETE comes
      // back. A second removal would ask the question again and send a second
      // request, to come back 404 over a removal that is working.
      if (refuseWhileRemoving(paper, 'a second removal')) return;
      await removePaper(paper);
      return;
    }
    if (act === 'accept-tag' || act === 'reject-tag') {
      // The proposals are written back onto the paper, so this is the same
      // race even though it writes no claim: accepting also puts the name into
      // the vocabulary, and a topic accepted from a paper that then goes would
      // be left declared with nothing under it.
      if (refuseWhileRemoving(paper, 'the answer to its proposed topics')) return;
      const field = act === 'accept-tag' ? 'accept' : 'discard';
      V.error = null;
      try {
        await api(`/api/papers/${encodeURIComponent(paper)}/proposed-tags`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ [field]: [button.dataset.tag] }),
        });
      } catch (error) {
        V.error = `Could not ${field} the proposed topic: ${error.message}`;
      }
      delete paperCache()[paper];
      await refreshAllShowingError();
      return;
    }
  }
  // The edit form lives inside a `.claim` wrapper, so a click on one of its
  // fields bubbles down to the card-selection branch below. Re-rendering there
  // replaces the form, drops focus, and redraws from the stored row, which
  // makes the editor unusable with a mouse.
  if (event.target.closest('form[data-form], #research-form')) return;

  const tagEl = event.target.closest('[data-tag]');
  if (tagEl && !tagEl.dataset.act) {
    V.tag = V.tag === tagEl.dataset.tag ? null : tagEl.dataset.tag;
    syncHash(true);
    renderAll();
    return;
  }
  const labelEl = event.target.closest('[data-label]');
  if (labelEl && !labelEl.dataset.act) {
    chooseLabel(labelEl.dataset.label);
    return;
  }
  const card = event.target.closest('.claim[data-claim]');
  if (card && card.dataset.claim !== V.selectedId) {
    // Selecting another claim redraws the list, which would rebuild an open
    // editor from the server row; keep what is typed in it first.
    captureOpenEditor();
    selectClaim(card.dataset.claim);
    renderContent();
  }
});

$('panes').addEventListener('input', (e) => {
  if (e.target.matches('textarea[data-synth]')) V.synthDrafts[e.target.dataset.synth] = e.target.value;
  if (e.target.matches('textarea[data-note]')) {
    V.noteEditing = e.target.dataset.note;
    V.noteDrafts[V.noteEditing] = e.target.value;
  }
});

// Each filter keeps whatever is typed in an open editor before redrawing.
$('panes').addEventListener('change', (e) => {
  if (e.target.matches('[data-graph-opt]')) { graphOption(e.target); return; }
  if (e.target.id === 'tension-status') { V.tensionStatus = e.target.value; renderContent(); }
  if (e.target.id === 'agreement-status') { V.agreementStatus = e.target.value; renderContent(); }
});
$('panes').addEventListener('input', (e) => {
  if (e.target.matches('[data-graph-opt="minShared"]')) graphOption(e.target);
});

function graphOption(field) {
  const name = field.dataset.graphOpt;
  if (name === 'xAxis') {
    V.graph.xAxis = field.value;
    GRAPH.autofit = true;
  } else if (name === 'minShared') {
    V.graph.minShared = Math.max(1, parseInt(field.value, 10) || 1);
    const label = $('content').querySelector('[data-graph-min]');
    if (label) label.textContent = String(V.graph.minShared);
  } else {
    V.graph[name] = field.checked;
  }
  if (V.view === 'graph') renderGraph();
  if (V.view === 'graph' && name === 'xAxis') graphFit();
}

// --- boot -----------------------------------------------------------------

let libraryInitialized = false;
const poller = createPoller({ run: updateFromPoll, isVisible: () => !document.hidden });
// Manual reads wait out an older round so they see changes made after it began.
const pollOnce = () => poller.afterCurrent();
$('btn-refresh-library').addEventListener('click', () => {
  if (libraryInitialized) pollOnce().catch(() => {});
});

// One round of the background poll. Keeps settings current while editing; the
// content guard preserves the editor DOM, draft text, focus, and selection.
async function updateFromPoll() {
  const changed = await pull();
  if (changed === null) return;
  renderJobs();
  if (!changed) {
    if (citationsFailed && V.view === 'graph') loadCitations();
    return;
  }
  corpusChanged();
  // An asking that failed is asked again while the map is open, whether or
  // not anything in the corpus has moved.
  if (citationsFailed && V.view === 'graph') loadCitations();
  renderStats();
  renderPapers(); renderTensionsNav(); renderAgreementsNav(); renderResearchNav(); renderGraphNav(); renderTags(); renderLabels();
  renderActiveFilters();
  if (!editorHolds()) renderContent();
  syncHash();
  syncAnalysisControls();
  ResearchTools.sync();
  WorkspaceNotes.sync({view:V.view, paper:V.paper});
  CommandPalette.sync();
}

async function boot() {
  WorkspaceNotes.connect(workspaceClient);
  ResearchNotes.connect(workspaceClient);
  ResearchTools.connect(workspaceClient);
  window.FocusedReview.connect(workspaceClient);
  connectPaperMap(workspaceClient);
  AppStartup.phase('connection');
  await loadWorkspaces();
  // A stale saved workspace may have fallen back to default. Only open chats
  // after that selection is final; their setup help is available with AI off.
  $('btn-agent').disabled = false;
  applyHash();
  const changed = await pull();
  AppStartup.phase('interface');
  if (changed) corpusChanged();
  render();
  $('kind').innerHTML = '<option value="">every kind</option>'
    + S.kinds.map((k) => `<option value="${esc(k)}">${esc(k)}</option>`).join('');
  // A filter the URL named that the corpus does not have is already gone: the
  // read above went through `pull`, which drops them once the corpus is in `S`
  // and before anything is drawn from it.
  $('kind').value = V.kind;   // the kinds arrive with the corpus, after the URL was read
  // `refresh` went through `render`, whose editor guard leaves the content pane
  // alone whenever the view is Research: the poll must not rebuild the form
  // under the cursor. At boot there is no form yet, so that guard would leave a
  // URL naming Research — a bookmark, or a reload of the page while on it —
  // with an active nav entry and a blank main pane. Draw it once here.
  if (V.view === 'research') renderContent();
  const noteId = new URLSearchParams(location.search).get('note');
  if (noteId) await ResearchNotes.open(noteId);
  libraryInitialized = true;
  $('btn-refresh-library').disabled = false;
  poller.start();
  // WKWebView can suspend timers while the Mac app is in the background.
  // Catch up as soon as the window returns, without rebuilding open editors.
  const resume = () => { if (!document.hidden) poller.once().catch(() => {}); };
  window.addEventListener('focus', resume);
  document.addEventListener('visibilitychange', resume);
}
