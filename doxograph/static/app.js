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

// The paper whose PDF is shown beside the claims: the one selected, when it
// has a PDF and the reader has not put it away.
function readingPaper() {
  if (V.view !== 'claims' || !V.paper || V.pdfHidden) return null;
  const paper = S.papers.find((p) => p.key === V.paper);
  return paper && paper.has_pdf ? V.paper : null;
}

// Reading has its own layout. Library filters remain available on demand and
// keep their state when the user returns to the library.
let readingClaimsCollapsed = false;
function layoutPanes(reading) {
  const wasReading = document.body.classList.contains('reading-paper');
  document.body.classList.toggle('reading-paper', Boolean(reading));
  $('panes').classList.toggle('reading', Boolean(reading));
  if (Boolean(reading) !== wasReading) {
    $('main').scrollTop = 0;
    if (reading) $('paper-import').open = false;
    document.body.classList.remove('library-collapsed', 'reading-filters-open');
    const narrow = reading && window.matchMedia('(max-width: 800px)').matches;
    setReadingLibrary(Boolean(narrow));
  }
  document.body.classList.toggle('claims-collapsed', Boolean(reading) && readingClaimsCollapsed);
  fitClaimsWidth();
  const filtersOpen = document.body.classList.contains('reading-filters-open');
  const filtered = Boolean(V.q || V.kind || V.tag || V.label || V.unreviewed || V.unverified);
  $('btn-reader-filters').textContent = filtered ? 'Claim filters · active' : 'Claim filters';
  $('btn-reader-filters').setAttribute('aria-expanded', String(filtersOpen));
  ResearchTools.mountReader(reading);
}

function setReadingLibrary(collapsed) {
  if (collapsed && $('side').contains(document.activeElement)) $('btn-reader-library').focus();
  document.body.classList.toggle('library-collapsed', collapsed);
  $('btn-reader-library').textContent = collapsed ? 'Show library' : 'Hide library';
  $('btn-reader-library').setAttribute('aria-expanded', String(!collapsed));
  fitClaimsWidth();
}

function setReadingClaims(collapsed) {
  if (collapsed && $('claim-column').contains(document.activeElement)) $('btn-reader-claims').focus();
  readingClaimsCollapsed = collapsed;
  document.body.classList.toggle('claims-collapsed', document.body.classList.contains('reading-paper') && collapsed);
  $('btn-reader-claims').textContent = collapsed ? 'Show claims' : 'Hide claims';
  $('btn-reader-claims').setAttribute('aria-expanded', String(!collapsed));
  fitClaimsWidth();
}

function setReadingFilters(open) {
  document.body.classList.toggle('reading-filters-open', open);
  $('btn-reader-filters').setAttribute('aria-expanded', String(open));
}

function focusClaimSearch() {
  revealSidebar($('papers'));
  if (!['library', 'claims'].includes(V.view)) { showView('library'); syncHash(true); renderAll(); }
  if (readingPaper()) setReadingFilters(true);
  $('q').focus();
  $('q').select();
}

function focusPaperImport() {
  if (readingPaper()) setReadingLibrary(false);
  revealSidebar($('paper-import'));
  $('paper-import').open = true;
  $('refs').focus();
}

// A passage from the paper becomes the quote of a new claim, with its page as
// the locator. A draft already being written for this paper keeps its text
// and takes the passage.
function startClaimFromPassage(paper, passage) {
  if (refuseWhileRemoving(paper, 'a new claim')) return;
  setReadingClaims(false);
  captureOpenEditor();
  const held = V.newClaim && V.newClaim.paper === paper ? V.newClaim : blankClaim(paper);
  V.newClaim = { ...held, quote: passage.quote, locator: passage.locator, note: passage.note || held.note || '' };
  V.editing = NEW_CLAIM_ID;
  renderContent();
  const field = document.querySelector(`form[data-form="${NEW_CLAIM_ID}"] [name="text"]`);
  if (field) field.focus();
}

function blankClaim(paper) {
  return {
    id: NEW_CLAIM_ID, paper, text: '', kind: S.kinds[0] || 'finding',
    strength: 'supporting', tags: [], evidence: '', quote: '', locator: '',
    ledger_links: [], note: '', conditions: '', limitations: '', evidence_origin: '', evidence_relationship: 'unassessed', reviewed: true, paper_title: '', paper_authors: [], paper_year: null,
  };
}

function newClaimHasEdits(draft) {
  if (!draft) return false;
  const blank = blankClaim(draft.paper);
  return ['text', 'kind', 'strength', 'tags', 'evidence', 'quote', 'locator',
    'ledger_links', 'note', 'reviewed', 'conditions', 'limitations', 'evidence_origin', 'evidence_relationship'].some((field) =>
    JSON.stringify(draft[field]) !== JSON.stringify(blank[field]));
}

// Counts the claims the user picks by hand: j/k, n, a click on a card, a jump
// to a linked one. The `r` key reads it either side of its request, because a
// claim chosen while the review PATCH is in flight is a newer choice than the
// auto-advance and must not be overwritten by it. A count rather than a check
// of `V.selectedId`, which moves on its own: the redraw after a toggle drops
// the claim just reviewed out of an "only unreviewed" list and the selection
// lands somewhere else without the user touching anything.
let handPicked = 0;

function selectClaim(id) {
  V.selectedId = id;
  handPicked += 1;
  const row = S.claims.find((row) => row.id === id);
  if (row) rememberContinuation(row.paper, id);
}

// Claims with a save in flight. Their form is read-only until the request
// settles: text typed after Save was clicked is not in the request and would be
// thrown away by the redraw that follows it, and a second click would post the
// same new claim twice under two ids. Tracked by claim id rather than on the
// element, so a redraw during the request reapplies it.
const savingClaims = new Map();   // claim id -> requests in flight for it

function isSaving(id) {
  return (savingClaims.get(id) || 0) > 0;
}

function markSaving(id, busy) {
  // Counted, not a flag: a review toggle and a save can overlap, and the first
  // to finish must not unfreeze the form while the other is still running.
  const n = (savingClaims.get(id) || 0) + (busy ? 1 : -1);
  if (n > 0) savingClaims.set(id, n); else savingClaims.delete(id);
  applySavingState();
}

function applySavingState() {
  document.querySelectorAll('form[data-form]').forEach((form) => {
    const busy = isSaving(form.dataset.form);
    form.classList.toggle('saving', busy);
    form.querySelectorAll('input, textarea, select, button').forEach((field) => {
      field.disabled = busy;
    });
  });
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

// --- the width of the side pane -------------------------------------------
//
// How the window is divided between the lists and the claims is the reader's
// to set: drag the divider, or give it focus and use the arrow keys. A
// double-click puts it back where it started. Remembered in this browser like
// the other view preferences, since it is about the screen being read on.

const SIDE_WIDTH_KEY = 'doxograph-side-width';
const SIDE_WIDTH_MIN = 180;
// The 20rem the pane has always been, measured rather than written as 320px: a
// browser set to a larger default font size draws the lists larger too, and the
// width it starts at has to grow with them.
const sideWidthDefault = () =>
  20 * (parseFloat(getComputedStyle(document.documentElement).fontSize) || 16);
// Half the window is as far as the divider goes. Past that the side pane has
// stopped being a side pane and there is nothing left to read beside it. The
// floor gives way to that ceiling on a window too narrow to hold both.
const sideWidthMax = () => Math.round(window.innerWidth / 2);
const sideWidthMin = () => Math.min(SIDE_WIDTH_MIN, sideWidthMax());
const clampSideWidth = (px) => Math.min(sideWidthMax(), Math.max(sideWidthMin(), Math.round(px)));

function readSideWidth() {
  try {
    const saved = Number(localStorage.getItem(SIDE_WIDTH_KEY));
    return Number.isFinite(saved) && saved > 0 ? saved : sideWidthDefault();
  } catch (error) {
    return sideWidthDefault();
  }
}

// The width that was asked for, which is not always the width on screen: a
// window too narrow to hold it shows the widest pane it can. Kept here as well
// as in storage, so a browser that refuses to write still holds the choice for
// the rest of the session, and a window narrowed and widened again comes back
// to it rather than to the default.
let sideWidthWanted = readSideWidth();

// A drag or a key can ask past the ceiling; what is remembered is the most this
// window would have given it, not where the pointer went, so a drag that ran off
// the edge does not spring the pane open on a wider screen later.
function applySideWidth(px, persist = false) {
  return wantSideWidth(clampSideWidth(px), persist);
}

// The reset is the exception: Home and a double-click mean 20rem, whatever this
// window can show of it, so that is what is remembered and a wider window comes
// back to it.
function wantSideWidth(px, persist = false) {
  sideWidthWanted = Math.round(px);
  if (persist) storeSideWidth();
  return fitSideWidth();
}

// Put the pane at the width that was asked for, or as near as this window
// allows, without forgetting what was asked for.
function fitSideWidth() {
  const width = clampSideWidth(sideWidthWanted);
  $('layout').style.setProperty('--side-width', `${width}px`);
  const handle = $('side-resize');
  handle.setAttribute('aria-valuenow', String(width));
  handle.setAttribute('aria-valuemin', String(sideWidthMin()));
  handle.setAttribute('aria-valuemax', String(sideWidthMax()));
  return width;
}

function storeSideWidth() {
  try { localStorage.setItem(SIDE_WIDTH_KEY, String(sideWidthWanted)); }
  catch (error) { /* the pane keeps the width for this session */ }
}

const sideWidthNow = () => $('side').getBoundingClientRect().width;

fitSideWidth();

window.addEventListener('resize', fitSideWidth);

{
  const handle = $('side-resize');
  // The width the drag started from, not the pointer's distance from the
  // divider: the divider is 8px wide, so the two are not the same, and taking
  // the difference keeps the pane from jumping on the first move.
  let drag = null;
  // Two quick nudges of the divider inside the double-click interval make the
  // browser synthesize a dblclick out of them. Only a pair of presses that both
  // stayed still is a reset; two small drags are two small drags.
  let movedThisPress = false;
  let movedLastPress = false;
  handle.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    // Cancelling the press keeps the browser from selecting text under the
    // drag, but it also stops the divider taking focus, which would leave the
    // arrow keys on whatever was focused before. Give it focus by hand.
    event.preventDefault();
    handle.focus();
    movedLastPress = movedThisPress;
    movedThisPress = false;
    drag = { pointer: event.pointerId, from: event.clientX, width: sideWidthNow(), wanted: sideWidthWanted };
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add('resizing-side');
  });
  handle.addEventListener('pointermove', (event) => {
    if (!drag || event.pointerId !== drag.pointer) return;
    // A couple of pixels is a hand resting on the button, not a drag.
    if (Math.abs(event.clientX - drag.from) > 2) movedThisPress = true;
    applySideWidth(drag.width + event.clientX - drag.from);
  });
  const release = (event) => {
    if (!drag || event.pointerId !== drag.pointer) return;
    // A drag that left the divider where it found it is not a new width — it
    // was pushed into the floor or the ceiling, or brought back. Put back what
    // was asked for before it, so a pane this window is only showing part of
    // survives being pushed at.
    if (clampSideWidth(sideWidthWanted) === clampSideWidth(drag.wanted)) sideWidthWanted = drag.wanted;
    drag = null;
    document.body.classList.remove('resizing-side');
    storeSideWidth();   // write once, at the end of the drag
  };
  handle.addEventListener('pointerup', release);
  handle.addEventListener('pointercancel', release);
  handle.addEventListener('lostpointercapture', release);
  handle.addEventListener('dblclick', () => {
    if (!movedThisPress && !movedLastPress) wantSideWidth(sideWidthDefault(), true);
  });
  // A key that cannot move the divider — the pane is already against the floor
  // or the ceiling — leaves the remembered width where it is. Otherwise a press
  // that does nothing on a narrow window would quietly give up a wider pane the
  // window will be able to show again.
  const nudge = (px) => {
    if (clampSideWidth(px) !== clampSideWidth(sideWidthWanted)) applySideWidth(px, true);
  };
  handle.addEventListener('keydown', (event) => {
    const step = event.shiftKey ? 48 : 12;
    const at = clampSideWidth(sideWidthWanted);
    if (event.key === 'ArrowLeft') nudge(at - step);
    else if (event.key === 'ArrowRight') nudge(at + step);
    else if (event.key === 'Home') wantSideWidth(sideWidthDefault(), true);
    else return;
    event.preventDefault();
  });
}

// The claims column keeps its own width. ResizeObserver also refits it when
// the library opens or changes width, without replacing the mounted reader.
const CLAIMS_WIDTH_KEY = 'doxograph-claims-width';
let claimsWidthWanted = null;
try {
  const saved = Number(localStorage.getItem(CLAIMS_WIDTH_KEY));
  if (Number.isFinite(saved) && saved > 0) claimsWidthWanted = saved;
} catch (_) { /* use the responsive default */ }
const claimsWidthDefault = () => Math.max(
  (window.innerWidth <= 1100 ? 17 : 19) * (parseFloat(getComputedStyle(document.documentElement).fontSize) || 16),
  $('main').clientWidth * (window.innerWidth <= 1100 ? .34 : .3));
const claimsWidthMax = () => Math.max(0, $('main').clientWidth - 280);
const claimsWidthMin = () => Math.min(240, claimsWidthMax());
const clampClaimsWidth = px => Math.min(claimsWidthMax(), Math.max(claimsWidthMin(), Math.round(px)));

function fitClaimsWidth() {
  if (!document.body.classList.contains('reading-paper') || window.innerWidth <= 800) return;
  const width = clampClaimsWidth(claimsWidthWanted ?? claimsWidthDefault());
  $('panes').style.setProperty('--claims-width', `${width}px`);
  const handle = $('claims-resize');
  handle.setAttribute('aria-valuenow', String(width));
  handle.setAttribute('aria-valuemin', String(claimsWidthMin()));
  handle.setAttribute('aria-valuemax', String(claimsWidthMax()));
}
function storeClaimsWidth() {
  try {
    if (claimsWidthWanted === null) localStorage.removeItem(CLAIMS_WIDTH_KEY);
    else localStorage.setItem(CLAIMS_WIDTH_KEY, String(claimsWidthWanted));
  } catch (_) { /* retain the width for this session */ }
}
new ResizeObserver(fitClaimsWidth).observe($('main'));
window.addEventListener('resize', fitClaimsWidth);

{
  const handle = $('claims-resize');
  let drag = null, movedThisPress = false, movedLastPress = false;
  handle.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault();
    handle.focus();
    movedLastPress = movedThisPress;
    movedThisPress = false;
    drag = { pointer: event.pointerId, from: event.clientX,
      width: $('claim-column').getBoundingClientRect().width, wanted: claimsWidthWanted };
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add('resizing-claims');
  });
  handle.addEventListener('pointermove', event => {
    if (!drag || event.pointerId !== drag.pointer) return;
    if (Math.abs(event.clientX - drag.from) > 2) movedThisPress = true;
    // Moving the right divider left makes the claims column wider.
    claimsWidthWanted = clampClaimsWidth(drag.width + drag.from - event.clientX);
    fitClaimsWidth();
  });
  const release = event => {
    if (!drag || event.pointerId !== drag.pointer) return;
    if (clampClaimsWidth(claimsWidthWanted ?? claimsWidthDefault()) === Math.round(drag.width)) {
      claimsWidthWanted = drag.wanted;
    }
    drag = null;
    document.body.classList.remove('resizing-claims');
    storeClaimsWidth();
  };
  handle.addEventListener('pointerup', release);
  handle.addEventListener('pointercancel', release);
  handle.addEventListener('lostpointercapture', release);
  const reset = () => { claimsWidthWanted = null; storeClaimsWidth(); fitClaimsWidth(); };
  handle.addEventListener('dblclick', () => {
    if (!movedThisPress && !movedLastPress) reset();
  });
  handle.addEventListener('keydown', event => {
    const at = clampClaimsWidth(claimsWidthWanted ?? claimsWidthDefault());
    const step = event.shiftKey ? 48 : 12;
    if (event.key === 'Home') reset();
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      const width = clampClaimsWidth(at + (event.key === 'ArrowLeft' ? step : -step));
      if (width !== at) { claimsWidthWanted = width; storeClaimsWidth(); fitClaimsWidth(); }
    } else return;
    event.preventDefault();
  });
}

// --- notices and dialogs --------------------------------------------------
//
// Native alert, confirm and prompt block the page, cannot be styled, and hold
// the whole app still while they are up. They also cannot offer anything but
// OK and Cancel, which rules out the one answer a delete wants: Undo. What the
// app has to say goes through a notice; what it has to ask goes through one
// dialog, reused.

function toast(message, { actions = [], timeout = 6000, tone = '', onExpire = null } = {}) {
  const el = document.createElement('div');
  el.className = tone ? `toast ${tone}` : 'toast';
  const text = document.createElement('span');
  text.className = 'msg';
  text.textContent = message;
  el.appendChild(text);
  let timer = null;
  const handle = {
    // `expired` separates running out of being dismissed by an action. A
    // delete's notice commits the delete on the way out, and Undo is the one
    // way of closing it that must not.
    close(expired = false) {
      if (timer) clearTimeout(timer);
      timer = null;
      el.remove();
      if (expired && onExpire) onExpire();
    },
  };
  actions.forEach((action) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = action.label;
    button.addEventListener('click', () => { handle.close(); action.onClick(); });
    el.appendChild(button);
  });
  const dismiss = document.createElement('button');
  dismiss.type = 'button';
  dismiss.className = 'icon-button';
  dismiss.textContent = '×';
  dismiss.setAttribute('aria-label', `Dismiss: ${message}`);
  dismiss.addEventListener('click', () => handle.close(true));
  el.appendChild(dismiss);
  if (timeout) timer = setTimeout(() => handle.close(true), timeout);
  $('toasts').appendChild(el);
  return handle;
}

let askResolve = null;

function askDialog({ title, note = '', input = null, labelChoices = [], ok = 'OK', cancel = 'Cancel' }) {
  const dialog = $('ask');
  $('ask-title').textContent = title;
  $('ask-note').textContent = note;
  $('ask-note').hidden = !note;
  const field = $('ask-input');
  field.hidden = input === null;
  field.value = input ? (input.value || '') : '';
  field.placeholder = input ? (input.placeholder || '') : '';
  const choices = $('ask-label-choices');
  choices.hidden = labelChoices.length === 0;
  choices.innerHTML = labelChoices.map((name) => `<button type="button" class="label label-choice"
    data-label="${esc(name)}" style="${labelStyle(name)}" aria-label="${esc(name)}" aria-pressed="false">${esc(name)}</button>`).join('');
  syncAskLabels();
  $('ask-ok').textContent = ok;
  $('ask-cancel').textContent = cancel;
  dialog.showModal();
  if (input) { field.focus(); field.select(); } else $('ask-ok').focus();
  return new Promise((resolve) => { askResolve = resolve; });
}

// Every way out of the dialog lands here. The pending promise is taken first,
// so closing the element does not settle it a second time with a cancel.
function settleAsk(value) {
  const resolve = askResolve;
  askResolve = null;
  if ($('ask').open) $('ask').close();
  if (resolve) resolve(value);
}

$('ask-form').addEventListener('submit', (event) => {
  event.preventDefault();
  settleAsk($('ask-input').hidden ? true : $('ask-input').value);
});
$('ask-cancel').addEventListener('click', () => settleAsk(null));
$('ask').addEventListener('cancel', () => settleAsk(null));   // Escape
$('ask').addEventListener('close', () => {
  // close() queues this event. An answer can open the next question before
  // it arrives, so only cancel a question whose dialog is still closed.
  if (!$('ask').open) settleAsk(null);
});

const confirmDialog = (title, { note = '', ok = 'OK' } = {}) =>
  askDialog({ title, note, ok }).then((answer) => answer === true);
const askText = (title, { note = '', placeholder = '', value = '', labelChoices = [] } = {}) =>
  askDialog({ title, note, input: { placeholder, value }, labelChoices });

function normalizeLabel(name) {
  // Match Python str.split(): U+FEFF is preserved; U+001C–001F and U+0085
  // are separators. JavaScript's \s and trim() use a different set.
  return name.replace(/[\p{White_Space}\u001c-\u001f]+/gu, ' ').replace(/^ | $/g, '');
}

// Labels keep the case they were typed in but are one label in any case, as
// `store.label_key` decides on the server.
function labelKey(name) {
  return normalizeLabel(name).toLowerCase();
}

function compareText(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

// The spelling `labels` uses for `name`, or undefined when it has no such label.
function findLabel(labels, name) {
  const key = labelKey(name);
  return (labels || []).find((label) => labelKey(label) === key);
}

function parseLabels(value) {
  const labels = new Map();
  for (const label of value.split(',').map(normalizeLabel).filter(Boolean)) {
    if (!labels.has(labelKey(label))) labels.set(labelKey(label), label);
  }
  return [...labels.values()];
}

function syncAskLabels() {
  const selected = new Set(parseLabels($('ask-input').value).map(labelKey));
  $('ask-label-choices').querySelectorAll('button').forEach((button) => {
    button.setAttribute('aria-pressed', String(selected.has(labelKey(button.dataset.label))));
  });
}

$('ask-input').addEventListener('input', syncAskLabels);
$('ask-label-choices').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-label]');
  if (!button) return;
  const labels = parseLabels($('ask-input').value);
  const name = button.dataset.label;
  $('ask-input').value = (findLabel(labels, name) !== undefined
    ? labels.filter((label) => labelKey(label) !== labelKey(name)) : [...labels, name]).join(', ');
  syncAskLabels();
});

// --- deletions, undone by not sending them --------------------------------
//
// A delete waits out its notice before it is sent, so Undo is simply not
// sending it. Deleting at once and recreating on Undo would hand back a
// different claim: a new id, and the tensions, agreements and syntheses that
// cite the old one gone for good. Until it is sent, the row is kept out of
// what the page draws, so the wait is invisible.
const UNDO_MS = 8000;
const trash = new Map();   // "kind:id" -> the request that is waiting

function trashed(kind, id) {
  return trash.has(`${kind}:${id}`);
}

// `pruneTrashed` takes rows out of the state the page is holding, so that
// state no longer matches the tag the server gave it. Undoing a delete the
// server never heard about changes nothing on its side, and the next poll
// would be answered "not modified" and leave the row missing for good.
function forgetStateTag() {
  workspaceClient.invalidateState();
}

// Whether a claim still says what a synthesis's basis on file recorded it
// saying. The server writes each entry of that basis as a JSON list of the
// three fields the model was shown, so it is read back rather than built again
// here: building it would mean reproducing Python's spacing and its escaping of
// everything above ASCII, and what the comparison is about is the values.
// The heading a cross-paper prompt writes for a claim's paper, as
// `store.cite_head` writes it. The fingerprints below were made on the server
// and are compared here character for character, so the two have to agree: a
// blank author is skipped rather than shown, and a paper with none is named by
// its key.
function citeHead(claim) {
  const authors = claim.paper_authors || [];
  const key = claim.paper || '';
  let head = key;
  for (const author of authors) {
    const parts = String(author).split(/\s+/).filter(Boolean);
    if (parts.length) { head = parts[parts.length - 1]; break; }
  }
  if (authors.length > 1) head += ' et al.';
  return `${head} (${claim.paper_year || 'n.d.'}): ${claim.paper_title || key}`;
}

function basisMatches(fingerprint, claim) {
  let written;
  try { written = JSON.parse(fingerprint); } catch { return false; }
  return Array.isArray(written) && written.length === 4
    && written[0] === (claim.text || '') && written[1] === (claim.evidence || '')
    && written[2] === (claim.kind || 'finding') && written[3] === citeHead(claim);
}

// `synthesis_rows` reads `stale` off two comparisons: the topic's claims
// against the basis the text was written from, and the topic's tensions against
// the ones the model was shown. A held claim can settle either of them as
// easily as unsettle it — a synthesis reading stale only because a claim had
// been added since is current again once that claim is the one deleted — so the
// flag is worked out again rather than simply turned on. `tensions` is the
// pruned list: one citing a held claim is already out of it, as it is out of
// the answer the server would give.
function synthesisStale(row, live, tensions) {
  const basis = row.claims || {};
  if (Object.keys(basis).length !== live.length
      || !live.every((claim) => claim.id in basis && basisMatches(basis[claim.id], claim))) {
    return true;
  }
  const shown = (tensions || []).filter(
    (tension) => (tension.topics || []).includes(row.topic) && tension.status !== 'dismissed');
  const written = row.tensions || {};
  if (Object.keys(written).length !== shown.length) return true;
  // A record written before a field was part of the basis holds a shorter
  // list, which matches nothing here and leaves it reading stale, as it does
  // on the server: what the model was told is not known.
  return !shown.every((tension) => {
    const seen = written[tension.id];
    return Array.isArray(seen) && seen.length === 4
      && seen[0] === (tension.kind ?? null) && seen[1] === (tension.status ?? null)
      && seen[2] === Boolean(tension.stale) && seen[3] === (tension.note || '');
  });
}

// Takes the waiting rows out of `S`. Called wherever `S` is replaced, and
// written to be safe to run twice on the same state: the per-paper counts are
// recounted from the claims that are left rather than decremented.
function pruneTrashed() {
  if (!trash.size) return;
  const all = S.claims || [];
  const kept = all.filter((row) => !trashed('claim', row.id));
  if (kept.length !== all.length) {
    S.claims = kept;
    const counts = new Map();
    kept.forEach((row) => {
      const count = counts.get(row.paper) || { claims: 0, unreviewed: 0, unverified: 0 };
      count.claims += 1;
      if (!row.reviewed) count.unreviewed += 1;
      if (row.quote_verified === false) count.unverified += 1;
      counts.set(row.paper, count);
    });
    (S.papers || []).forEach((paper) => {
      const count = counts.get(paper.key) || { claims: 0, unreviewed: 0, unverified: 0 };
      paper.n_claims = count.claims;
      paper.n_unreviewed = count.unreviewed;
      paper.n_unverified = count.unverified;
      // The status dot is the last of the per-paper summary to be recounted.
      // `delete_claim` calls `refresh_status`, so the paper the server answers
      // with once the wait is over reads its status off the claims that are
      // left; holding the old one shows a paper with no new claims still
      // marked as waiting to be reviewed, or a paper with nothing left on it
      // still marked reviewed. Worked out the way `refresh_status` works it
      // out, which needs to know whether the paper was ever read by the model
      // for the case where the held claim was its last.
      paper.status = count.claims === 0 ? (paper.has_extraction ? 'extracted' : 'fetched')
        : count.unreviewed ? 'extracted' : 'reviewed';
    });
    // The topic sidebar draws `tag_counts` as the server sent it, so a held
    // claim would go on being counted under every topic it carries — and a
    // topic whose only claim is held would stay on the list at one, offering a
    // filter that draws nothing. Recounted from what is left, like the per-paper
    // counts above and in the server's own order — commonest first, ties by
    // name — so the list does not reshuffle itself for the length of the wait.
    const tags = new Map();
    kept.forEach((row) => (row.tags || []).forEach(
      (tag) => tags.set(tag, (tags.get(tag) || 0) + 1)));
    const counted = Object.fromEntries([...tags].sort(
      (a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)));
    // One exception to counting what is left: the topic being filtered on. Its
    // entry in the sidebar is the only way back out of that filter, so dropping
    // it for the length of the wait would leave the reader on a view with no
    // claims and nothing to click — and clearing `V.tag` instead would make
    // Undo a one-way trip, putting them back on the whole corpus rather than
    // on the topic they were reading. Kept at zero, and only if it was there
    // before the prune, so a topic named by a stale URL is still dropped by
    // `dropMissingFilters`. The delete going through takes the entry with it:
    // the trash is empty by then, this runs no more, and the filter goes with
    // the count the server sends.
    if (V.tag && !Object.hasOwn(counted, V.tag)
        && Object.hasOwn(S.tag_counts || {}, V.tag)) counted[V.tag] = 0;
    S.tag_counts = counted;
  }
  // The analysis views hold the server's own join: each tension and agreement
  // carries the claim rows it cites, not their ids. Taking a held claim out of
  // `S.claims` alone would leave those copies on screen, still open to a
  // decision or a quote, so they are filtered the way `tension_rows` and
  // `agreement_rows` filter them — a tension needs both of its claims, an
  // agreement needs members from two papers. A tension that keeps both of its
  // claims keeps its topics too: they are the ones both of them carry, and both
  // of them are still here.
  S.tensions = (S.tensions || []).filter(
    (row) => !(row.claims || []).some((claim) => trashed('claim', claim.id)));
  let regrouped = false;
  S.agreements = (S.agreements || [])
    .filter((row) => !trashed('agreement', row.id))
    .map((row) => {
      const members = (row.claims || []).filter((claim) => !trashed('claim', claim.id));
      if (members.length === (row.claims || []).length) return row;
      regrouped = true;
      // A group that loses a member can gain a topic. `agreement_rows` files
      // the topics under the agreement and then shows only the ones every
      // member still carries, so a topic that one member alone had dropped is
      // missing from the row the server sent; take that member out and the
      // topic is good for everyone left, which is what the server will say
      // once the delete lands. Keeping the filtered list would hide the group
      // from its own topic for the length of the wait and then hand it back at
      // the end. The page cannot un-filter a list, so the row carries the set
      // the server filtered from, `topics_on_file`, and it is filtered again
      // here against the members that are left.
      //
      // Stale, as on the server: the note and the decision on file were about
      // a group that is not the one on screen.
      const topics = (row.topics_on_file || row.topics || []).filter(
        (topic) => members.every((claim) => (claim.tags || []).includes(topic)));
      return { ...row, claims: members, topics,
               n_papers: new Set(members.map((c) => c.paper)).size, stale: true };
    })
    .filter((row) => new Set((row.claims || []).map((claim) => claim.paper)).size >= 2);
  if (regrouped) {
    // `agreement_rows` orders by status, then by how many papers are in the
    // group, then by when it was found. A group that lost a member to the wait
    // is smaller than the one the server sorted, so the cards would shuffle
    // themselves the moment the wait ended and the real answer arrived. Sorted
    // here on the same key, and stably, so rows the delete did not touch keep
    // the order they came in.
    const statuses = S.tension_statuses || ['open', 'confirmed', 'dismissed'];
    const rank = (row) => (statuses.indexOf(row.status) + 1 || 10) - 1;
    const found = (row) => row.found || '';
    S.agreements.sort((a, b) => rank(a) - rank(b) || b.n_papers - a.n_papers
      || (found(a) < found(b) ? -1 : found(a) > found(b) ? 1 : 0));
  }
  // A synthesis is the same kind of derived row: `synthesis_rows` works out the
  // counts and the stale flag from the claims its topic has now, not from the
  // ids on file, and drops a topic that has none left. Held claims were taken
  // out of only the syntheses deleted by topic, so a topic one of them carried
  // went on claiming more claims and papers than it still had and cited a claim
  // the page no longer knows — `citeHtml` leaves that bracket as written, a
  // bare `[paper-a-c1]`. So the rows a held claim touches are rebuilt the way
  // the server builds them: no claims left and the card goes, otherwise recount
  // and read the stale flag off the basis on file. Undo is still the delete
  // never being sent, so the next refresh brings the row back as the server
  // has it.
  const heldTopics = new Set();
  all.forEach((row) => {
    if (trashed('claim', row.id)) (row.tags || []).forEach((tag) => heldTopics.add(tag));
  });
  S.syntheses = (S.syntheses || [])
    .filter((row) => !trashed('synthesis', row.topic))
    .map((row) => {
      if (!heldTopics.has(row.topic)) return row;
      const live = kept.filter((claim) => (claim.tags || []).includes(row.topic));
      if (!live.length) return null;
      return { ...row, n_claims: live.length,
               n_papers: new Set(live.map((claim) => claim.paper)).size,
               stale: synthesisStale(row, live, S.tensions) };
    })
    .filter(Boolean);
}

async function deleteLater(kind, id, path, label, { paper = null } = {}) {
  const token = `${kind}:${id}`;
  if (trash.has(token)) return;
  const workspace = workspaceClient.workspaceId || 'default';
  let notice = null;
  // The entry stays in the trash until the request settles, so the row does not
  // flash back on screen between the notice fading and the server answering.
  // That leaves it visible to a second caller — the notice running out while an
  // export flushes, say — so the request itself is what is shared: one delete,
  // and no second one to come back 404 and leave a failure notice standing.
  // It lives on the entry rather than in here, because the page closing has to
  // see it too and has only the entry to look at.
  const send = () => {
    const entry = trash.get(token);
    if (!entry) return Promise.resolve();   // already sent, or undone
    if (entry.sending) return entry.sending;
    if (notice) notice.close();
    // Resolves to whether the row is really gone. A delete that failed leaves
    // it on file, and the callers waiting on this — an export, a retag, a model
    // pass — would go on to read a corpus that still holds a claim the reader
    // watched disappear. They are told, rather than left to assume it settled.
    entry.sending = (async () => {
      let sent = true;
      try {
        // Named rather than implied: `flushTrash` sends these before a switch,
        // and this makes that a guarantee rather than an ordering to preserve.
        //
        // `keepalive` because the page closing stands aside for this request
        // rather than sending a second one: an ordinary fetch can be abandoned
        // as the page goes, and the row would come back from a delete the
        // reader watched happen. A DELETE carries no body, so the size limit
        // that comes with it costs nothing.
        await api(path, {
          method: 'DELETE',
          keepalive: true,
          headers: { 'X-Doxograph-Workspace': workspace },
        });
      } catch (error) {
        sent = false;
        toast(`Could not delete ${label}: ${error.message}`, { tone: 'warn', timeout: 0 });
      } finally {
        trash.delete(token);
        forgetStateTag();
      }
      await refreshAll();
      return sent;
    })();
    return entry.sending;
  };
  trash.set(token, { path, workspace, paper, send, sending: null });
  forgetStateTag();
  pruneTrashed();
  // `pruneTrashed` only takes the row out of the state the page holds; what is
  // on screen stands until something draws it. That draw happens here, before
  // the refresh, because the refresh is a read from the server and a server
  // restarting under the click rejects it — and then nothing redraws at all.
  // The row would sit there under the "Deleted …" notice, still clickable and
  // still open to an edit or a second delete, until the timer sent the DELETE
  // out from under it.
  renderAll();
  // The redraw comes before the notice, so the row is off the screen before an
  // offer to put it back appears over it. The refresh can still fail, and the
  // notice is what carries the timer that finally sends the delete: raised only
  // on the way out of a refresh that worked, it would leave the entry sitting in
  // the trash with the row hidden, the DELETE never sent, and no Undo and no
  // error to say so. So it goes up either way, and the failure still reaches
  // the caller.
  try {
    await refreshAll();
  } finally {
    // The wait can end during that refresh — `flushTrash` sends what is waiting
    // — and a notice raised afterwards would offer to undo a delete the server
    // already has. The entry is still in the trash while its request is in
    // flight, which is what keeps the row off the screen, so `sending` is the
    // thing to ask: once it is set there is nothing left to undo.
    if (trash.get(token) && !trash.get(token).sending) {
      notice = toast(`Deleted ${label}.`, {
        timeout: UNDO_MS,
        onExpire: send,
        actions: [{
          label: 'Undo',
          onClick: async () => {
            // Sent between the notice going up and this being clicked. `send`
            // closes the notice, so this is all but unreachable — but an Undo
            // that quietly does nothing is worse than one that says so.
            if (trash.get(token)?.sending) {
              toast(`That delete has already been sent; ${label} is gone.`, { tone: 'warn' });
              return;
            }
            trash.delete(token);
            forgetStateTag();
            await refreshAll();
          },
        }],
      });
    }
  }
}

// A delete belongs to the workspace it was made in, so leaving one sends what
// is still waiting rather than carrying it across, where the same claim id can
// name a different claim. It is also what the server has to be told before it
// is asked to work anything out from the corpus: for those eight seconds the
// row is gone from the page but still on file, and an export or a model pass
// started meanwhile would take it as live.
// Settle the held deletes, then say which workspace the action was asked in.
// The flush ends in a read and a read is not counted as a change in flight, so
// the picker can move in the gap; anything sent afterwards has to name the
// corpus the reader was looking at, not the one they have since gone to.
// Null when a held delete came back an error: the row is still on file, the
// reader has already been told so, and whatever was waiting on the deletion
// must not go ahead against a corpus that still holds it. Every caller checks.
async function settleDeletes(wanted = null) {
  const workspace = workspaceClient.workspaceId;
  if (!await flushTrash(wanted)) return null;
  return { 'X-Doxograph-Workspace': workspace };
}

// True when everything it sent is really gone.
async function flushTrash(wanted = null) {
  // Drained rather than snapshotted: the page stays interactive while this
  // runs, so a row deleted during it has to go with the rest. A snapshot would
  // leave that one on file for the export or the model pass waiting on this.
  //
  // This workspace's rows, though. Each delete leaves the trash before its own
  // trailing read, and a read is not counted as a change in flight, so the
  // picker can move while the drain is between rounds. A delete made after
  // that belongs to the corpus the reader has gone to: taking it here would
  // send it under an Undo the page is still offering, and its failure would
  // call off an action in a workspace it has nothing to do with. It is left
  // to its own eight seconds, and to the flush that leaving that workspace
  // runs.
  const workspace = workspaceClient.workspaceId || 'default';
  let settled = true;
  for (;;) {
    const waiting = [...trash.values()]
      .filter((entry) => entry.workspace === workspace && (!wanted || wanted(entry)));
    if (!waiting.length) return settled;
    // `send` resolves to `undefined` for an entry that went while this was
    // deciding; only an outright `false` is a failure.
    const sent = await Promise.all(waiting.map((entry) => entry.send()));
    if (sent.some((ok) => ok === false)) settled = false;
  }
}

// Coming back to a page the browser froze rather than unloaded. `pagehide`
// sent the held deletes, but the DOM came back as it was: the notices on it
// offer to undo work that is finished, and their Undo would have nothing to
// take back. They go, and the corpus is read again from the server.
window.addEventListener('pageshow', (event) => {
  if (!event.persisted) return;
  $('toasts').innerHTML = '';
  forgetStateTag();
  refreshAll();
});

// A tab closed while a delete is still waiting sends it now rather than
// silently dropping it: the row is already gone from the page, and coming back
// to find it restored would be the app forgetting what it was told.
window.addEventListener('pagehide', () => {
  trash.forEach((entry) => {
    // One already on its way needs nothing: a second request would race it, and
    // whichever lost would come back 404 over a row that was deleted after all.
    if (entry.sending) return;
    const headers = { 'X-Doxograph-Workspace': entry.workspace };
    workspaceClient.response(entry.path, { method: 'DELETE', headers, keepalive: true })
      .catch(() => {}); // The page is leaving; there is no remaining UI for a failure.
  });
  trash.clear();
});

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

// The last visit is a browser preference, scoped to the workspace. PDF pages
// come from the existing notebook rather than a second copy of reading progress.
let continuation = null;
function continuationState() {
  if (continuation?.workspace === workspaceClient.workspaceId) return continuation;
  let saved = {}, dismissed = false;
  try { saved = JSON.parse(localStorage.getItem(`doxograph-continue:${workspaceClient.workspaceId}`)) || {}; } catch (_) { /* optional */ }
  try { dismissed = localStorage.getItem(`doxograph-continue-dismissed:${workspaceClient.workspaceId}`) === '1'; } catch (_) { /* optional */ }
  // A fresh deep link is a visit. On reload, the library behind a resumed PDF
  // has already been visited and must not displace the more recent reader.
  const reloading = performance.getEntriesByType('navigation')[0]?.type === 'reload';
  continuation = {workspace:workspaceClient.workspaceId, paper:saved.paper || null, claim:saved.claim || null,
    observedPaper:reloading ? V.paper : null, dismissed};
  return continuation;
}

function rememberContinuation(paper, claim = null) {
  const state = continuationState();
  if (state.paper !== paper) state.claim = null;
  state.paper = paper;
  if (claim) state.claim = claim;
  try { localStorage.setItem(`doxograph-continue:${workspaceClient.workspaceId}`, JSON.stringify({paper:state.paper, claim:state.claim})); } catch (_) { /* keep for this session */ }
}

function continuationTargets() {
  const state = continuationState();
  // Only a new paper visit changes the shortcut. A poll while reading a PDF
  // must not replace it with the paper still open behind the reader dialog.
  const visiting = V.view === 'claims' ? V.paper : null;
  if (visiting && (visiting !== state.observedPaper || !state.paper)) rememberContinuation(visiting);
  state.observedPaper = visiting;
  const reading = S.notebook?.reading || [];
  const paper = S.papers.find((p) => p.key === state.paper)
    || S.papers.find((p) => reading.some((r) => r.paper === p.key && r.status === 'reading'));
  const progress = reading.find((r) => r.paper === paper?.key);
  // Finish the last paper first, then continue through the rest of the corpus.
  const rows = paper ? S.claims.filter((c) => c.paper === paper.key) : [];
  const at = rows.findIndex((c) => c.id === state.claim);
  const next = rows.slice(at + 1).concat(rows.slice(0, at + 1)).find((c) => !c.reviewed)
    || S.claims.find((c) => !c.reviewed);
  return {paper, progress, next};
}

function renderContinuation() {
  const {paper, progress, next} = continuationTargets();
  const card = $('continue-reading');
  card.hidden = continuationState().dismissed || (!paper && !next);
  const html = `<h2 id="continue-title">Continue</h2>
    <div class="continue-items">
      ${paper ? `<div class="continue-item">
        <p class="continue-text" title="${esc(paper.title || paper.key)}">${esc(paper.title || paper.key)}</p>
        <div class="row"><button type="button" data-continue="paper">Open paper</button>
        ${paper.has_pdf ? `<button type="button" class="primary" data-tool="reader" data-paper="${esc(paper.key)}">Resume at page ${progress?.page || 1}</button>` : '<span class="hint">No PDF available</span>'}</div></div>` : ''}
      ${next ? `<div class="continue-item">
        <p class="continue-text" title="${esc(next.paper_title || next.paper)}: ${esc(next.text)}">${esc(next.text)}</p>
        <button type="button" data-continue="review">Next unreviewed claim</button></div>`
        : S.claims.length ? '<span class="hint">All claims reviewed.</span>' : ''}
    </div>
    <button type="button" class="continue-dismiss" data-continue="dismiss" aria-label="Dismiss continue where you left off" title="Dismiss">×</button>`;
  // Leave focused shortcuts intact across background refreshes.
  if (card.innerHTML !== html) card.innerHTML = html;
}

$('continue-reading').addEventListener('click', (event) => {
  const button = event.target.closest('[data-continue]');
  if (!button) return;
  if (button.dataset.continue === 'dismiss') {
    continuationState().dismissed = true;
    try { localStorage.setItem(`doxograph-continue-dismissed:${workspaceClient.workspaceId}`, '1'); } catch (_) { /* keep for this session */ }
    renderContinuation();
    return;
  }
  const {paper, next} = continuationTargets();
  const review = button.dataset.continue === 'review';
  const key = review ? next?.paper : paper?.key;
  if (!key) return;
  captureOpenEditor();
  parkNewClaimForNavigation(key);
  parkSynthEditor();
  parkNoteEditor();
  showView('claims');
  V.paper = key; V.tag = null; V.label = null;
  V.q = ''; V.kind = ''; V.unreviewed = false; V.unverified = false; V.editing = null;
  $('q').value = ''; $('kind').value = '';
  $('only-unreviewed').checked = false; $('only-unverified').checked = false;
  selectClaim(review ? next.id : null);
  rememberContinuation(key, review ? next.id : null);
  scheduleTextSearch();
  syncHash(true); renderAll();
  if (review) scrollToSelected();
  else $('main').scrollTop = 0;
});

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

// --- filtering ------------------------------------------------------------

const FILTER_DEFAULTS = { paper: null, tag: null, label: null, q: '', kind: '',
  unreviewed: false, unverified: false, tensionStatus: '', agreementStatus: '',
  tensionFocus: null, agreementFocus: null };
let activeFiltersHTML = null;

function renderActiveFilters() {
  const filters = [];
  const add = (key, label) => { if (V[key]) filters.push([key, label]); };
  add('paper', `Paper: ${S.papers.find((p) => p.key === V.paper)?.title || V.paper}`);
  add('tag', `Topic: ${V.tag}`);
  add('label', `Label: ${V.label}`);
  if (V.q.trim()) add('q', `Search: ${V.q.trim()}`);
  add('kind', `Kind: ${V.kind}`);
  add('unreviewed', 'Unreviewed');
  add('unverified', 'Quote not found');
  if (V.view === 'tensions' || V.view === 'agreements') {
    const prefix = V.view === 'tensions' ? 'tension' : 'agreement';
    add(`${prefix}Status`, `Status: ${V[`${prefix}Status`]}`);
    const focus = V[`${prefix}Focus`];
    add(`${prefix}Focus`, `Claim: ${S.claims.find((c) => c.id === focus)?.text || 'no longer available'}`);
  }
  const container = $('active-filters');
  const html = filters.map(([key, label]) => `<button type="button" class="filter-chip"
    data-clear-filter="${key}" aria-label="${esc(`Remove filter: ${label}`)}" title="${esc(label)}">
    <span class="filter-label">${esc(label)}</span><span class="filter-remove" aria-hidden="true">×</span>
  </button>`).join('') + (filters.length
    ? '<button type="button" class="clear-filters" data-clear-filter="all">Clear all</button>' : '');
  // Polls and selection changes should leave a focused chip in place.
  if (activeFiltersHTML !== html) {
    container.innerHTML = html;
    activeFiltersHTML = html;
  }
  container.hidden = !filters.length;
}

function clearFilters(key) {
  if (key !== 'all' && !Object.hasOwn(FILTER_DEFAULTS, key)) return;
  captureOpenEditor();
  if (key === 'all' || key === 'paper') parkNewClaimForNavigation(null, { retainUnsaved: true });
  if (key === 'all') Object.assign(V, FILTER_DEFAULTS);
  else V[key] = FILTER_DEFAULTS[key];
  $('q').value = V.q;
  $('kind').value = V.kind;
  $('only-unreviewed').checked = V.unreviewed;
  $('only-unverified').checked = V.unverified;
  if (key === 'all' || key === 'q') dropTextSearch();
  if (V.view === 'research' && V.researchSaving) {
    // The save owns the disabled form and inserts any failure beside it.
    // Update the filters and sidebar while keeping that form connected.
    render();
    syncHash();
  } else {
    renderAll();
  }
  // The activated button has gone; leave keyboard users on the next action.
  ($('active-filters').querySelector('button') || $('q')).focus();
}

$('active-filters').addEventListener('click', (event) => {
  const button = event.target.closest('[data-clear-filter]');
  if (button) clearFilters(button.dataset.clearFilter);
});

// The phrase if the corpus holds it, otherwise the words.
//
// One substring over the whole row could not find "recovery under steering"
// from "steering recovery", which is how anyone types a search. Every word in
// any order finds it — but it also makes "Paper A" match everything holding
// "paper" and a word starting with "a", and a title typed into the box should
// narrow to that title. Which was meant is decided by the corpus: if the
// phrase is in it, that is what was wanted, and otherwise the words are all
// there is to go on. Nothing that used to be findable stops being findable.
// The page has to fold exactly as the server does, or a query finds a paper
// by its text and hides the claim that says the same thing. JavaScript's
// `toLowerCase` is not `casefold`: it lowercases one character to one, so ß
// stays ß, and it knows where a Greek word ends where `casefold` does not.
// Both tables below are the differences, written out rather than guessed at —
// a narrowing that suited one script has been wrong for the next one twice.
//
// CASEFOLD: every code point whose `casefold` differs from its `lower`.
//   python -c "print([c for c in map(chr, range(0x110000)) if c.casefold() != c.lower()])"
// COMBINING: every code point with a combining class that is an accent — not
//   a nukta, a kana voicing mark or a virama, each of which changes the word
//   — which is what the server drops.
//   python -c "import unicodedata; print([c for c in map(chr, range(0x110000))
//              if unicodedata.combining(c) not in (0, 7, 8, 9)])"
const CASEFOLD = {'\u00b5':'\u03bc','\u00df':'\u0073\u0073','\u0149':'\u02bc\u006e','\u017f':'\u0073','\u01f0':'\u006a\u030c','\u0345':'\u03b9','\u0390':'\u03b9\u0308\u0301','\u03b0':'\u03c5\u0308\u0301','\u03c2':'\u03c3','\u03d0':'\u03b2','\u03d1':'\u03b8','\u03d5':'\u03c6','\u03d6':'\u03c0','\u03f0':'\u03ba','\u03f1':'\u03c1','\u03f5':'\u03b5','\u0587':'\u0565\u0582','\u13a0':'\u13a0','\u13a1':'\u13a1','\u13a2':'\u13a2','\u13a3':'\u13a3','\u13a4':'\u13a4','\u13a5':'\u13a5','\u13a6':'\u13a6','\u13a7':'\u13a7','\u13a8':'\u13a8','\u13a9':'\u13a9','\u13aa':'\u13aa','\u13ab':'\u13ab','\u13ac':'\u13ac','\u13ad':'\u13ad','\u13ae':'\u13ae','\u13af':'\u13af','\u13b0':'\u13b0','\u13b1':'\u13b1','\u13b2':'\u13b2','\u13b3':'\u13b3','\u13b4':'\u13b4','\u13b5':'\u13b5','\u13b6':'\u13b6','\u13b7':'\u13b7','\u13b8':'\u13b8','\u13b9':'\u13b9','\u13ba':'\u13ba','\u13bb':'\u13bb','\u13bc':'\u13bc','\u13bd':'\u13bd','\u13be':'\u13be','\u13bf':'\u13bf','\u13c0':'\u13c0','\u13c1':'\u13c1','\u13c2':'\u13c2','\u13c3':'\u13c3','\u13c4':'\u13c4','\u13c5':'\u13c5','\u13c6':'\u13c6','\u13c7':'\u13c7','\u13c8':'\u13c8','\u13c9':'\u13c9','\u13ca':'\u13ca','\u13cb':'\u13cb','\u13cc':'\u13cc','\u13cd':'\u13cd','\u13ce':'\u13ce','\u13cf':'\u13cf','\u13d0':'\u13d0','\u13d1':'\u13d1','\u13d2':'\u13d2','\u13d3':'\u13d3','\u13d4':'\u13d4','\u13d5':'\u13d5','\u13d6':'\u13d6','\u13d7':'\u13d7','\u13d8':'\u13d8','\u13d9':'\u13d9','\u13da':'\u13da','\u13db':'\u13db','\u13dc':'\u13dc','\u13dd':'\u13dd','\u13de':'\u13de','\u13df':'\u13df','\u13e0':'\u13e0','\u13e1':'\u13e1','\u13e2':'\u13e2','\u13e3':'\u13e3','\u13e4':'\u13e4','\u13e5':'\u13e5','\u13e6':'\u13e6','\u13e7':'\u13e7','\u13e8':'\u13e8','\u13e9':'\u13e9','\u13ea':'\u13ea','\u13eb':'\u13eb','\u13ec':'\u13ec','\u13ed':'\u13ed','\u13ee':'\u13ee','\u13ef':'\u13ef','\u13f0':'\u13f0','\u13f1':'\u13f1','\u13f2':'\u13f2','\u13f3':'\u13f3','\u13f4':'\u13f4','\u13f5':'\u13f5','\u13f8':'\u13f0','\u13f9':'\u13f1','\u13fa':'\u13f2','\u13fb':'\u13f3','\u13fc':'\u13f4','\u13fd':'\u13f5','\u1c80':'\u0432','\u1c81':'\u0434','\u1c82':'\u043e','\u1c83':'\u0441','\u1c84':'\u0442','\u1c85':'\u0442','\u1c86':'\u044a','\u1c87':'\u0463','\u1c88':'\ua64b','\u1e96':'\u0068\u0331','\u1e97':'\u0074\u0308','\u1e98':'\u0077\u030a','\u1e99':'\u0079\u030a','\u1e9a':'\u0061\u02be','\u1e9b':'\u1e61','\u1e9e':'\u0073\u0073','\u1f50':'\u03c5\u0313','\u1f52':'\u03c5\u0313\u0300','\u1f54':'\u03c5\u0313\u0301','\u1f56':'\u03c5\u0313\u0342','\u1f80':'\u1f00\u03b9','\u1f81':'\u1f01\u03b9','\u1f82':'\u1f02\u03b9','\u1f83':'\u1f03\u03b9','\u1f84':'\u1f04\u03b9','\u1f85':'\u1f05\u03b9','\u1f86':'\u1f06\u03b9','\u1f87':'\u1f07\u03b9','\u1f88':'\u1f00\u03b9','\u1f89':'\u1f01\u03b9','\u1f8a':'\u1f02\u03b9','\u1f8b':'\u1f03\u03b9','\u1f8c':'\u1f04\u03b9','\u1f8d':'\u1f05\u03b9','\u1f8e':'\u1f06\u03b9','\u1f8f':'\u1f07\u03b9','\u1f90':'\u1f20\u03b9','\u1f91':'\u1f21\u03b9','\u1f92':'\u1f22\u03b9','\u1f93':'\u1f23\u03b9','\u1f94':'\u1f24\u03b9','\u1f95':'\u1f25\u03b9','\u1f96':'\u1f26\u03b9','\u1f97':'\u1f27\u03b9','\u1f98':'\u1f20\u03b9','\u1f99':'\u1f21\u03b9','\u1f9a':'\u1f22\u03b9','\u1f9b':'\u1f23\u03b9','\u1f9c':'\u1f24\u03b9','\u1f9d':'\u1f25\u03b9','\u1f9e':'\u1f26\u03b9','\u1f9f':'\u1f27\u03b9','\u1fa0':'\u1f60\u03b9','\u1fa1':'\u1f61\u03b9','\u1fa2':'\u1f62\u03b9','\u1fa3':'\u1f63\u03b9','\u1fa4':'\u1f64\u03b9','\u1fa5':'\u1f65\u03b9','\u1fa6':'\u1f66\u03b9','\u1fa7':'\u1f67\u03b9','\u1fa8':'\u1f60\u03b9','\u1fa9':'\u1f61\u03b9','\u1faa':'\u1f62\u03b9','\u1fab':'\u1f63\u03b9','\u1fac':'\u1f64\u03b9','\u1fad':'\u1f65\u03b9','\u1fae':'\u1f66\u03b9','\u1faf':'\u1f67\u03b9','\u1fb2':'\u1f70\u03b9','\u1fb3':'\u03b1\u03b9','\u1fb4':'\u03ac\u03b9','\u1fb6':'\u03b1\u0342','\u1fb7':'\u03b1\u0342\u03b9','\u1fbc':'\u03b1\u03b9','\u1fbe':'\u03b9','\u1fc2':'\u1f74\u03b9','\u1fc3':'\u03b7\u03b9','\u1fc4':'\u03ae\u03b9','\u1fc6':'\u03b7\u0342','\u1fc7':'\u03b7\u0342\u03b9','\u1fcc':'\u03b7\u03b9','\u1fd2':'\u03b9\u0308\u0300','\u1fd3':'\u03b9\u0308\u0301','\u1fd6':'\u03b9\u0342','\u1fd7':'\u03b9\u0308\u0342','\u1fe2':'\u03c5\u0308\u0300','\u1fe3':'\u03c5\u0308\u0301','\u1fe4':'\u03c1\u0313','\u1fe6':'\u03c5\u0342','\u1fe7':'\u03c5\u0308\u0342','\u1ff2':'\u1f7c\u03b9','\u1ff3':'\u03c9\u03b9','\u1ff4':'\u03ce\u03b9','\u1ff6':'\u03c9\u0342','\u1ff7':'\u03c9\u0342\u03b9','\u1ffc':'\u03c9\u03b9','\uab70':'\u13a0','\uab71':'\u13a1','\uab72':'\u13a2','\uab73':'\u13a3','\uab74':'\u13a4','\uab75':'\u13a5','\uab76':'\u13a6','\uab77':'\u13a7','\uab78':'\u13a8','\uab79':'\u13a9','\uab7a':'\u13aa','\uab7b':'\u13ab','\uab7c':'\u13ac','\uab7d':'\u13ad','\uab7e':'\u13ae','\uab7f':'\u13af','\uab80':'\u13b0','\uab81':'\u13b1','\uab82':'\u13b2','\uab83':'\u13b3','\uab84':'\u13b4','\uab85':'\u13b5','\uab86':'\u13b6','\uab87':'\u13b7','\uab88':'\u13b8','\uab89':'\u13b9','\uab8a':'\u13ba','\uab8b':'\u13bb','\uab8c':'\u13bc','\uab8d':'\u13bd','\uab8e':'\u13be','\uab8f':'\u13bf','\uab90':'\u13c0','\uab91':'\u13c1','\uab92':'\u13c2','\uab93':'\u13c3','\uab94':'\u13c4','\uab95':'\u13c5','\uab96':'\u13c6','\uab97':'\u13c7','\uab98':'\u13c8','\uab99':'\u13c9','\uab9a':'\u13ca','\uab9b':'\u13cb','\uab9c':'\u13cc','\uab9d':'\u13cd','\uab9e':'\u13ce','\uab9f':'\u13cf','\uaba0':'\u13d0','\uaba1':'\u13d1','\uaba2':'\u13d2','\uaba3':'\u13d3','\uaba4':'\u13d4','\uaba5':'\u13d5','\uaba6':'\u13d6','\uaba7':'\u13d7','\uaba8':'\u13d8','\uaba9':'\u13d9','\uabaa':'\u13da','\uabab':'\u13db','\uabac':'\u13dc','\uabad':'\u13dd','\uabae':'\u13de','\uabaf':'\u13df','\uabb0':'\u13e0','\uabb1':'\u13e1','\uabb2':'\u13e2','\uabb3':'\u13e3','\uabb4':'\u13e4','\uabb5':'\u13e5','\uabb6':'\u13e6','\uabb7':'\u13e7','\uabb8':'\u13e8','\uabb9':'\u13e9','\uabba':'\u13ea','\uabbb':'\u13eb','\uabbc':'\u13ec','\uabbd':'\u13ed','\uabbe':'\u13ee','\uabbf':'\u13ef','\ufb00':'\u0066\u0066','\ufb01':'\u0066\u0069','\ufb02':'\u0066\u006c','\ufb03':'\u0066\u0066\u0069','\ufb04':'\u0066\u0066\u006c','\ufb05':'\u0073\u0074','\ufb06':'\u0073\u0074','\ufb13':'\u0574\u0576','\ufb14':'\u0574\u0565','\ufb15':'\u0574\u056b','\ufb16':'\u057e\u0576','\ufb17':'\u0574\u056d'};
const COMBINING = /[\u0300-\u034e\u0350-\u036f\u0483-\u0487\u0591-\u05bd\u05bf\u05c1-\u05c2\u05c4-\u05c5\u05c7\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e4\u06e7-\u06e8\u06ea-\u06ed\u0711\u0730-\u074a\u07eb-\u07f3\u07fd\u0816-\u0819\u081b-\u0823\u0825-\u0827\u0829-\u082d\u0859-\u085b\u0897-\u089f\u08ca-\u08e1\u08e3-\u08ff\u0951-\u0954\u09fe\u0c55-\u0c56\u0e38-\u0e39\u0e48-\u0e4b\u0eb8-\u0eb9\u0ec8-\u0ecb\u0f18-\u0f19\u0f35\u0f37\u0f39\u0f71-\u0f72\u0f74\u0f7a-\u0f7d\u0f80\u0f82-\u0f83\u0f86-\u0f87\u0fc6\u108d\u135d-\u135f\u17dd\u18a9\u1939-\u193b\u1a17-\u1a18\u1a75-\u1a7c\u1a7f\u1ab0-\u1abd\u1abf-\u1ace\u1b6b-\u1b73\u1cd0-\u1cd2\u1cd4-\u1ce0\u1ce2-\u1ce8\u1ced\u1cf4\u1cf8-\u1cf9\u1dc0-\u1dff\u20d0-\u20dc\u20e1\u20e5-\u20f0\u2cef-\u2cf1\u2de0-\u2dff\u302a-\u302f\ua66f\ua674-\ua67d\ua69e-\ua69f\ua6f0-\ua6f1\ua8e0-\ua8f1\ua92b-\ua92d\uaab0\uaab2-\uaab4\uaab7-\uaab8\uaabe-\uaabf\uaac1\ufb1e\ufe20-\ufe2f\u{101fd}\u{102e0}\u{10376}-\u{1037a}\u{10a0d}\u{10a0f}\u{10a38}-\u{10a3a}\u{10ae5}-\u{10ae6}\u{10d24}-\u{10d27}\u{10d69}-\u{10d6d}\u{10eab}-\u{10eac}\u{10efd}-\u{10eff}\u{10f46}-\u{10f50}\u{10f82}-\u{10f85}\u{11100}-\u{11102}\u{11366}-\u{1136c}\u{11370}-\u{11374}\u{1145e}\u{16af0}-\u{16af4}\u{16b30}-\u{16b36}\u{16ff0}-\u{16ff1}\u{1bc9e}\u{1d165}-\u{1d169}\u{1d16d}-\u{1d172}\u{1d17b}-\u{1d182}\u{1d185}-\u{1d18b}\u{1d1aa}-\u{1d1ad}\u{1d242}-\u{1d244}\u{1e000}-\u{1e006}\u{1e008}-\u{1e018}\u{1e01b}-\u{1e021}\u{1e023}-\u{1e024}\u{1e026}-\u{1e02a}\u{1e08f}\u{1e130}-\u{1e136}\u{1e2ae}\u{1e2ec}-\u{1e2ef}\u{1e4ec}-\u{1e4ef}\u{1e5ee}-\u{1e5ef}\u{1e8d0}-\u{1e8d6}\u{1e944}-\u{1e949}]/gu;
const CASEFOLD_RE = /[\u00b5\u00df\u0149\u017f\u01f0\u0345\u0390\u03b0\u03c2\u03d0\u03d1\u03d5\u03d6\u03f0\u03f1\u03f5\u0587\u13a0\u13a1\u13a2\u13a3\u13a4\u13a5\u13a6\u13a7\u13a8\u13a9\u13aa\u13ab\u13ac\u13ad\u13ae\u13af\u13b0\u13b1\u13b2\u13b3\u13b4\u13b5\u13b6\u13b7\u13b8\u13b9\u13ba\u13bb\u13bc\u13bd\u13be\u13bf\u13c0\u13c1\u13c2\u13c3\u13c4\u13c5\u13c6\u13c7\u13c8\u13c9\u13ca\u13cb\u13cc\u13cd\u13ce\u13cf\u13d0\u13d1\u13d2\u13d3\u13d4\u13d5\u13d6\u13d7\u13d8\u13d9\u13da\u13db\u13dc\u13dd\u13de\u13df\u13e0\u13e1\u13e2\u13e3\u13e4\u13e5\u13e6\u13e7\u13e8\u13e9\u13ea\u13eb\u13ec\u13ed\u13ee\u13ef\u13f0\u13f1\u13f2\u13f3\u13f4\u13f5\u13f8\u13f9\u13fa\u13fb\u13fc\u13fd\u1c80\u1c81\u1c82\u1c83\u1c84\u1c85\u1c86\u1c87\u1c88\u1e96\u1e97\u1e98\u1e99\u1e9a\u1e9b\u1e9e\u1f50\u1f52\u1f54\u1f56\u1f80\u1f81\u1f82\u1f83\u1f84\u1f85\u1f86\u1f87\u1f88\u1f89\u1f8a\u1f8b\u1f8c\u1f8d\u1f8e\u1f8f\u1f90\u1f91\u1f92\u1f93\u1f94\u1f95\u1f96\u1f97\u1f98\u1f99\u1f9a\u1f9b\u1f9c\u1f9d\u1f9e\u1f9f\u1fa0\u1fa1\u1fa2\u1fa3\u1fa4\u1fa5\u1fa6\u1fa7\u1fa8\u1fa9\u1faa\u1fab\u1fac\u1fad\u1fae\u1faf\u1fb2\u1fb3\u1fb4\u1fb6\u1fb7\u1fbc\u1fbe\u1fc2\u1fc3\u1fc4\u1fc6\u1fc7\u1fcc\u1fd2\u1fd3\u1fd6\u1fd7\u1fe2\u1fe3\u1fe4\u1fe6\u1fe7\u1ff2\u1ff3\u1ff4\u1ff6\u1ff7\u1ffc\uab70\uab71\uab72\uab73\uab74\uab75\uab76\uab77\uab78\uab79\uab7a\uab7b\uab7c\uab7d\uab7e\uab7f\uab80\uab81\uab82\uab83\uab84\uab85\uab86\uab87\uab88\uab89\uab8a\uab8b\uab8c\uab8d\uab8e\uab8f\uab90\uab91\uab92\uab93\uab94\uab95\uab96\uab97\uab98\uab99\uab9a\uab9b\uab9c\uab9d\uab9e\uab9f\uaba0\uaba1\uaba2\uaba3\uaba4\uaba5\uaba6\uaba7\uaba8\uaba9\uabaa\uabab\uabac\uabad\uabae\uabaf\uabb0\uabb1\uabb2\uabb3\uabb4\uabb5\uabb6\uabb7\uabb8\uabb9\uabba\uabbb\uabbc\uabbd\uabbe\uabbf\ufb00\ufb01\ufb02\ufb03\ufb04\ufb05\ufb06\ufb13\ufb14\ufb15\ufb16\ufb17]/gu;

function fold(text) {
  // Lowercased, then folded, then decomposed and stripped of the marks that
  // are accents, then lowercased again: a compatibility capital has no
  // lowercase of its own, and NFKD turns it into a plain uppercase letter the
  // first pass never saw.
  // Folded on both sides of the decomposition: a compatibility character can
  // decompose into one that folds — mathematical bold final sigma comes out
  // as ς, which is σ to `casefold` and itself to `toLowerCase`.
  // A soft hyphen goes with them: it says where a word may be broken, and a
  // word carrying one is the word, as the server reads it too.
  return String(text ?? '').toLowerCase()
    .replace(/\u00ad/g, '')
    .replace(CASEFOLD_RE, (ch) => CASEFOLD[ch])
    .normalize('NFKD')
    .replace(CASEFOLD_RE, (ch) => CASEFOLD[ch])
    .replace(COMBINING, '')
    .toLowerCase();
}

function queryMatcher() {
  const query = fold(V.q.trim());
  if (!query) return () => true;
  const phrase = (text) => fold(text).includes(query);
  if (S.claims.some((row) => phrase(haystack(row)))
      || S.papers.some((paper) => phrase(paperHaystack(paper)))) {
    return phrase;
  }
  const patterns = queryPatterns(query);
  // A query of punctuation alone has no words to look for, and `every` over
  // nothing is true: it would show the whole corpus for a query the phrase
  // pass has already failed to find. Nothing matches it.
  if (!patterns.length) return () => false;
  // Both sides folded, so the patterns need no case flag of their own.
  return (text) => { const folded = fold(text); return patterns.every((p) => p.test(folded)); };
}

// Scripts that do not put spaces between their words, as `search.py` has
// them. A term in one of these is looked for wherever it falls: every
// character around it is a letter, so a word start never comes.
const UNSEGMENTED = /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af\u0e00-\u0eff\u1780-\u17ff\u0f00-\u0fff\u1000-\u109f\ua980-\ua9df\u1b00-\u1b7f\u1b80-\u1bbf\u1a20-\u1aaf\uaa00-\uaa5f]/u;

// A term matches from the start of a word, where words have starts. `\b` in
// JavaScript knows only ASCII and would never match a query written in
// another script, and a lookbehind is not available: the app supports macOS
// 13.0, whose WKWebView has none, and the SyntaxError would take the whole
// filter down. What is left is to consume the character before the word, and
// to allow the start of the string in its place.
function queryPatterns(query) {
  // Marks count as part of the word they sit on: in a script whose vowels are
  // marks, splitting on them cuts one written word into its consonants, and
  // the server does not.
  return (fold(query).match(/[\p{L}\p{N}_\p{M}]+/gu) || [])
    .map((term) => (UNSEGMENTED.test(term)
      ? new RegExp(term, 'u')
      // The underscore is a word character to the server's `\w`, so it is one
      // here: `bar` does not start a word in `foo_bar` on either side. So is
      // a mark the folding kept — a vowel sign is part of the word it sits
      // in, and a boundary inside किताब would find ताब there.
      : new RegExp(`(?:^|[^\\p{L}\\p{N}_\\p{M}])${term}`, 'u')));
}

// A claim's searchable text. It carries its paper's key and year as well as
// the title, so that every query matching a paper also matches that paper's
// claims: listing a paper in the sidebar and then showing it as empty when
// clicked would be worse than not listing it at all.
function haystack(row) {
  return [row.text, row.evidence, row.quote, row.note, row.paper, row.paper_title,
          row.paper_year, (row.paper_authors || []).join(' '),
          paperSearchTextFor(row.paper), (row.tags || []).join(' '),
          (row.paper_labels || []).join(' ')]
    .join(' ').toLowerCase();
}

// Paper summaries and notes, for their claims' haystacks: a query matching only
// those fields must not list the paper and then show it as empty, which is the one
// thing `haystack` carries the paper's fields to prevent.
//
// Cached against the array `S.papers` currently is. That array is replaced
// wholesale whenever the corpus is read and never edited in place, so the
// identity check rebuilds the map exactly when it has to.
let paperSearchText = { from: null, byKey: new Map() };

function paperSearchTextFor(key) {
  if (paperSearchText.from !== S.papers) {
    paperSearchText = {
      from: S.papers,
      byKey: new Map((S.papers || []).map((p) => [p.key, [p.summary, p.notes].join(' ')])),
    };
  }
  return paperSearchText.byKey.get(key) || '';
}

// A paper's own text, for the query. `haystack` covers a paper through its
// claims; this covers one that has none yet, which is exactly the paper a
// title search is most likely to be looking for.
function paperHaystack(paper) {
  return [paper.title, paper.key, (paper.authors || []).join(' '), paper.year, paper.summary,
          paper.notes, (paper.labels || []).join(' ')]
    .join(' ').toLowerCase();
}

// The papers' own text is searched on the server, because that is where it is:
// every claim is already in the page, but the PDFs never are. Debounced, since
// this runs on a keystroke, and short queries are not sent at all — every paper
// holds "a", and reading the corpus to prove it helps nobody.
let textSearchTimer = null;
let textSearchSeq = 0;
// Two characters: AI, RL and R2 are what a reader of this corpus searches
// for, and one character is every paper in it.
const TEXT_SEARCH_MIN = 2;
// Three letters of a Latin query says little; two characters of Chinese or
// Japanese is a whole word. A query carrying anything outside the Latin
// scripts is asked whatever its length.
const COMPACT_SCRIPT = /[^\p{Script=Latin}\p{N}\p{P}\p{Z}\p{C}]/u;

function worthAsking(query) {
  return query.length >= TEXT_SEARCH_MIN || (query.length > 0 && COMPACT_SCRIPT.test(query));
}

function scheduleTextSearch() {
  clearTimeout(textSearchTimer);
  const query = V.q.trim();
  if (!worthAsking(query)) { V.textSearch = null; return; }
  textSearchTimer = setTimeout(() => runTextSearch(query), 250);
}

// Strand a debounce that has not fired and any answer still in flight. Called
// when the workspace changes: the same query in the new corpus is a different
// question, and the old corpus's papers must not be drawn under it.
function dropTextSearch() {
  clearTimeout(textSearchTimer);
  textSearchTimer = null;
  textSearchSeq += 1;
  V.textSearch = null;
}

// A paper imported since the answer came back holds the query's words as much
// as any other, and one that has just been given its PDF is searchable for the
// first time. Filtering the answer against the corpus can drop a hit that has
// gone but cannot add one that has arrived, so the question is asked again.
function rerunTextSearch() {
  if (V.textSearch && V.textSearch.q === V.q.trim()) runTextSearch(V.textSearch.q, true);
}

// `quiet` keeps what is on screen until the new answer lands, for a re-run
// nobody asked for: a corpus change would otherwise blink the results back to
// "reading the papers" every time a paper is imported.
async function runTextSearch(query, quiet = false) {
  const seq = ++textSearchSeq;
  const workspace = workspaceClient.workspaceId;
  const library = V.view === 'library';
  if (!quiet) {
    captureOpenEditor();   // the redraw rebuilds any open form from state
    V.textSearch = { q: query, loading: true, papers: [], terms: [], error: null };
    drawSearchProgress();
  }
  let next;
  try {
    const papers = new Map();
    let offset = 0, found;
    do {
      found = await api(`/api/search?q=${encodeURIComponent(query)}&limit=${library ? 100 : 20}&offset=${offset}`);
      if (seq !== textSearchSeq || workspace !== workspaceClient.workspaceId) return;
      for (const hit of found.papers || []) papers.set(hit.key, hit);
      if (found.next_offset != null) {
        if (!Number.isInteger(found.next_offset) || found.next_offset <= offset) throw new Error('Search returned an invalid next page.');
        offset = found.next_offset;
      }
    } while (library && found.next_offset != null);
    next = { q: query, loading: false, papers: [...papers.values()], terms: found.terms || [], complete: found.next_offset == null, error: null };
  } catch (error) {
    next = { q: query, loading: false, papers: [], terms: [], error: error.message };
  }
  // An answer to a query that is no longer the one on screen is dropped: the
  // requests are not guaranteed to come back in the order they went out, and
  // the corpus can have changed under the one still running.
  if (seq !== textSearchSeq || workspace !== workspaceClient.workspaceId) return;
  V.textSearch = next;
  captureOpenEditor();
  drawSearchProgress();
}

// The results live under the claims, so the research form never shows them —
// and redrawing it would replace a form frozen mid-save with an enabled one,
// which is how typing into the replacement gets thrown away when the save
// lands. `render` stands off the research view for the same reason.
function drawSearchProgress() {
  if (V.view === 'library') renderPapers();
  if (V.view !== 'research') renderContent();
}

// The papers the query matches: directly, or through a claim of theirs. The
// claim match ignores the selected paper, so narrowing to one paper does not
// empty the list you would use to leave it.
function matchingPapers() {
  const labelled = labelledPapers();
  const within = labelled ? S.papers.filter((p) => labelled.has(p.key)) : S.papers;
  if (!V.q.trim()) return within;
  const matches = queryMatcher();
  const owners = new Set(S.claims
    .filter((row) => matches(haystack(row)))
    .map((row) => row.paper));
  if (V.view === 'library' && V.textSearch?.q === V.q.trim()) {
    for (const hit of V.textSearch.papers || []) owners.add(hit.key);
  }
  return within.filter((p) => owners.has(p.key) || matches(paperHaystack(p)));
}

// A paper appears once, even when several of its claims match. Papers without
// extracted claims still belong in the library and can match their metadata.
function visiblePapers() {
  const claimFilters = V.tag || V.kind || V.unreviewed || V.unverified;
  const owners = claimFilters ? new Set(S.claims.filter(row =>
    (!V.tag || (row.tags || []).includes(V.tag))
    && (!V.kind || row.kind === V.kind)
    && (!V.unreviewed || !row.reviewed)
    && (!V.unverified || row.quote_verified === false)).map(row => row.paper)) : null;
  const matched = new Set(matchingPapers().map(p => p.key));
  return sortedPapers(S.papers.filter(p =>
    (!V.label || findLabel(p.labels, V.label) !== undefined)
    && (!owners || owners.has(p.key))
    && matched.has(p.key)), V.paperSort);
}

let libraryHTML = null;
function renderLibrary() {
  const papers = visiblePapers();
  const topics = new Map();
  for (const claim of S.claims) {
    if (!topics.has(claim.paper)) topics.set(claim.paper, new Set());
    for (const tag of claim.tags || []) topics.get(claim.paper).add(tag);
  }
  const progress = new Map((S.notebook?.reading || []).map(entry => [entry.paper, entry]));
  const statuses = {queued: 'Queued', reading: 'Reading', finished: 'Finished'};
  const found = V.textSearch?.q === V.q.trim() ? V.textSearch : null;
  const hits = new Map((found?.papers || []).map(hit => [hit.key, hit]));
  const cards = papers.map(p => {
    const entry = progress.get(p.key);
    const status = statuses[entry?.status] || 'Unread';
    const tags = [...(topics.get(p.key) || [])].sort((a, b) => a.localeCompare(b));
    const params = new URLSearchParams({ws: workspaceClient.workspaceId, paper: p.key});
    const passages = (hits.get(p.key)?.passages || []).slice(0, 2);
    return `<article class="library-paper" data-library-paper="${esc(p.key)}">
      <div class="library-paper-heading">
        ${ResearchTools.paperCheckbox(p)}
        <h3><a class="library-paper-link" href="#${esc(params.toString())}" data-act="library-open-paper" data-paper="${esc(p.key)}">${esc(p.title || p.key)}</a></h3>
        <span class="reading-status ${esc(entry?.status || 'unread')}">${status}</span>
      </div>
      <p class="library-paper-meta">${esc((p.authors || []).join(', ') || 'Authors unknown')} · ${esc(p.year || 'Year unknown')}${entry?.status === 'reading' && entry.page ? ` · Page ${esc(entry.page)}` : ''}</p>
      <p class="library-paper-summary${p.summary ? '' : ' hint'}">${esc(p.summary || 'No summary yet.')}</p>
      <div class="library-paper-topics" aria-label="Topics">${tags.length
        ? tags.map(tag => `<button type="button" class="tag" data-tag="${esc(tag)}">${esc(tag)}</button>`).join(' ')
        : '<span class="hint">No topics yet</span>'}</div>
      ${passages.map(passage => `<p class="library-paper-passage"><span class="hint">PDF · p. ${esc(passage.page)}</span> …${mark(passage)}…</p>`).join('')}
    </article>`;
  }).join('');
  const searchStatus = !found ? '' : found.loading ? 'Searching paper text…'
    : found.error ? `Could not search paper text: ${found.error}` : '';
  const html = `<section class="paper-library" aria-labelledby="paper-library-title">
    <div class="paper-library-heading"><h2 id="paper-library-title">${V.tag ? esc(V.tag) : 'All papers'}</h2>
      <span class="hint">${papers.length} ${papers.length === 1 ? 'paper' : 'papers'}${papers.length < S.papers.length ? ` of ${S.papers.length}` : ''}</span></div>
    ${V.error ? `<p class="warn">${esc(V.error)}</p>` : ''}
    ${searchStatus ? `<p class="hint" role="status">${esc(searchStatus)}</p>` : ''}
    ${cards || `<p class="empty">${S.papers.length ? 'No papers match these filters.' : 'Your library is empty. Paste an arXiv ID or drop a PDF to add your first paper.'}</p>`}
  </section>`;
  // Keep focused links and checkboxes intact across unchanged background polls.
  if (libraryHTML === html && $('content').querySelector('.paper-library')) return;
  libraryHTML = html;
  const focused = document.activeElement;
  const key = focused.closest('[data-library-paper]')?.dataset.libraryPaper;
  const selector = focused.matches('[data-select-paper]') ? '[data-select-paper]'
    : focused.matches('[data-tag]') ? `[data-tag="${CSS.escape(focused.dataset.tag)}"]` : '.library-paper-link';
  $('content').innerHTML = html;
  if (key) $('content').querySelector(`[data-library-paper="${CSS.escape(key)}"] ${selector}`)?.focus({preventScroll: true});
}

// A destination the label filter excludes arrives empty: a paper's header
// with nothing under it, or a citation that selects some other card instead of
// the one that was clicked. Every path that opens a paper drops the filter
// when the paper it lands on does not carry the label, and keeps it when it
// does — the filter says which papers are in view, and this one now is.
function keepLabelFor(key) {
  const paper = key && S.papers.find((p) => p.key === key);
  if (V.label && paper && findLabel(paper.labels, V.label) === undefined) V.label = null;
}

// The keys the label filter allows, or null when no label is chosen. A label
// belongs to the paper, so it narrows the claims by way of the papers that
// carry it rather than by anything on the claim itself.
function labelledPapers() {
  if (!V.label) return null;
  return new Set(S.papers.filter((p) => findLabel(p.labels, V.label) !== undefined).map((p) => p.key));
}

function visibleClaims() {
  const matches = queryMatcher();
  const labelled = labelledPapers();
  return S.claims.filter((row) =>
    (!V.paper || row.paper === V.paper)
    && (!labelled || labelled.has(row.paper))
    && (!V.tag || (row.tags || []).includes(V.tag))
    && (!V.kind || row.kind === V.kind)
    && (!V.unreviewed || !row.reviewed)
    && (!V.unverified || row.quote_verified === false)
    && matches(haystack(row)));
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

// The server sends papers newest-added first. The other orders are a view
// preference, so they are applied here and remembered per browser, not per
// workspace: the choice is about how the person likes to scan the list.
const PAPER_SORT_KEY = 'doxograph-paper-sort';
const PAPER_SORTS = ['added-desc', 'added-asc', 'year-desc', 'year-asc', 'title'];

function readPaperSort() {
  try {
    const saved = localStorage.getItem(PAPER_SORT_KEY);
    return PAPER_SORTS.includes(saved) ? saved : PAPER_SORTS[0];
  } catch (error) {
    return PAPER_SORTS[0];
  }
}
V.paperSort = readPaperSort();

function sortedPapers(papers, order) {
  const byTitle = (a, b) => (a.title || a.key).localeCompare(b.title || b.key, undefined, { sensitivity: 'base' });
  const byAdded = (a, b) => (a.added || '').localeCompare(b.added || '');
  const list = [...papers];
  switch (order) {
    case 'added-asc': return list.sort((a, b) => byAdded(a, b) || byTitle(a, b));
    case 'title': return list.sort(byTitle);
    // A paper with no year sinks to the bottom in either direction: it is
    // unknown, not ancient or brand new.
    case 'year-desc': return list.sort((a, b) => ((b.year ?? -Infinity) - (a.year ?? -Infinity)) || byTitle(a, b));
    case 'year-asc': return list.sort((a, b) => ((a.year ?? Infinity) - (b.year ?? Infinity)) || byTitle(a, b));
    default: return list.sort((a, b) => byAdded(b, a) || byTitle(a, b));
  }
}

function addedLabel(p) {
  if (!p.added) return '';
  const day = p.added.slice(0, 10);
  return `<time datetime="${esc(p.added)}" title="added ${esc(p.added)}">${esc(day)}</time>`;
}

let importWorkspace = null, importPaperCount = 0;
function renderPapers() {
  if (!$('refs').value.trim()) {
    if (importWorkspace !== workspaceClient.workspaceId) $('paper-import').open = S.papers.length === 0;
    else if (!importPaperCount && S.papers.length) $('paper-import').open = false;
  }
  importWorkspace = workspaceClient.workspaceId;
  importPaperCount = S.papers.length;
  const claims = V.view === 'claims';
  const byAdded = V.paperSort.startsWith('added');
  const matched = matchingPapers();
  // The selected paper is listed whether or not it matches, or there would be
  // no way back out of it. It is not counted as a match, though: the count is
  // about the query, and navigation state must not inflate it.
  const keys = new Set(matched.map((p) => p.key));
  const shown = V.paper !== null && !keys.has(V.paper)
    ? S.papers.filter((p) => keys.has(p.key) || p.key === V.paper)
    : matched;
  // The count doubles as the reason the list got shorter: a filtered sidebar
  // with no explanation reads as papers having gone missing.
  const meta = matched.length < S.papers.length
    ? `${matched.length} of ${S.papers.length} match`
    : `${S.papers.length} ${S.papers.length === 1 ? 'paper' : 'papers'}`;
  const all = `<li class="${(claims || V.view === 'library') && V.paper === null ? 'active' : ''}" data-paper="">
    <span class="pt">All papers</span>
    <span class="pm">${meta}</span></li>`;
  $('papers').innerHTML = all + sortedPapers(shown, V.paperSort).map((p) => `
    <li class="${claims && V.paper === p.key ? 'active' : ''}" data-paper="${esc(p.key)}">
      ${ResearchTools.paperCheckbox(p)}<span class="pt"><span class="dot ${esc(p.status)}"></span>${esc(p.title || p.key)}</span>
      <span class="pm">${esc((p.authors || [])[0] ? p.authors[0].split(' ').pop() : '?')}
        ${p.year ? esc(p.year) : ''} · ${p.n_claims} claims · ${esc(paperReadiness(p))}${p.n_unreviewed ? `, ${p.n_unreviewed} new` : ''}${byAdded ? ` · ${addedLabel(p)}` : ''}</span>
      ${(p.labels || []).length ? `<span class="labels" aria-label="Paper labels">${p.labels.map((label) => `
        <button type="button" class="label" data-label="${esc(label)}" style="${labelStyle(label)}"
          aria-pressed="${Boolean(V.label) && labelKey(V.label) === labelKey(label)}" title="Filter by label: ${esc(label)}">${esc(label)}</button>`).join('')}</span>` : ''}
      <button type="button" class="pmenu" data-menu="${esc(p.key)}"
        aria-label="Actions for ${esc(p.title || p.key)}" title="Actions">⋯</button>
    </li>`).join('');
}

function renderTensionsNav() {
  const all = S.tensions || [];
  const count = (status) => all.filter((t) => t.status === status).length;
  const parts = [];
  if (count('open')) parts.push(`${count('open')} open`);
  if (count('confirmed')) parts.push(`${count('confirmed')} confirmed`);
  if (count('dismissed')) parts.push(`${count('dismissed')} dismissed`);
  $('tensions-nav').innerHTML = `<li class="${V.view === 'tensions' ? 'active' : ''}" data-view="tensions">
    <span class="pt">Where papers disagree</span>
    <span class="pm">${all.length ? esc(parts.join(' · ')) : 'none found yet'}</span></li>`;
}

function renderAgreementsNav() {
  const all = S.agreements || [];
  const count = (status) => all.filter((a) => a.status === status).length;
  const parts = [];
  if (count('open')) parts.push(`${count('open')} open`);
  if (count('confirmed')) parts.push(`${count('confirmed')} confirmed`);
  if (count('dismissed')) parts.push(`${count('dismissed')} dismissed`);
  $('agreements-nav').innerHTML = `<li class="${V.view === 'agreements' ? 'active' : ''}" data-view="agreements">
    <span class="pt">Where papers agree</span>
    <span class="pm">${all.length ? esc(parts.join(' · ')) : 'none found yet'}</span></li>`;
}

// Agreements a claim is part of, for the marker on its card.
function agreementsFor(claimId) {
  return (S.agreements || []).filter((a) => a.status !== 'dismissed'
    && a.claims.some((c) => c.id === claimId));
}

function renderResearchNav() {
  const n = (S.ledger || []).length;
  const bits = [];
  bits.push(S.context ? 'context written' : 'no context yet');
  bits.push(n ? `${n} claims of my own` : 'no claims of my own');
  if (V.view !== 'research' && researchFormDirty()) bits.push('unsaved edits');
  $('research-nav').innerHTML = `<li class="${V.view === 'research' ? 'active' : ''}" data-view="research">
    <span class="pt">What I am studying</span>
    <span class="pm">${esc(bits.join(' · '))}</span></li>`;
}

// Tensions a claim takes part in, for the marker on its card. Dismissed ones
// are not marked: the reviewer has said there is nothing there.
function tensionsFor(claimId) {
  return (S.tensions || []).filter((t) => t.status !== 'dismissed'
    && t.claims.some((c) => c.id === claimId));
}

// Derive colors from names so existing and new labels keep the same color
// across papers, reloads, and changes to the corpus, without extra metadata.
function labelStyle(name) {
  let hash = 2166136261;
  for (const char of labelKey(name)) {
    hash = Math.imul(hash ^ char.codePointAt(0), 16777619);
  }
  return `--label-hue:${(hash >>> 0) % 360}`;
}

function labelChip(name) {
  return `<span class="label" data-label="${esc(name)}" style="${labelStyle(name)}">${esc(name)}</span>`;
}

// Hide the section until at least one paper has a label.
function renderLabels() {
  const entries = Object.entries(S.label_counts || {});
  $('labels-sec').hidden = entries.length === 0;
  $('labels').innerHTML = entries.map(([name, count]) => `
    <li class="${V.label && labelKey(V.label) === labelKey(name) ? 'active' : ''}" data-label="${esc(name)}" style="${labelStyle(name)}">
      <span class="label-dot" aria-hidden="true"></span>
      <span>${esc(name)}</span>
      <span class="n">${count}</span>
    </li>`).join('');
}

function renderTags() {
  const entries = Object.entries(S.tag_counts);
  const declared = new Set(S.tags.map((t) => t.name));
  $('tags').innerHTML = (entries.length ? '' : '<li class="hint">No topics yet.</li>')
    + entries.map(([name, count]) => `
      <li class="${V.tag === name ? 'active' : ''}" data-tag="${esc(name)}"
          title="${esc((S.tags.find((t) => t.name === name) || {}).description || '')}">
        <span>${esc(name)}${declared.has(name) ? '' : ' *'}</span>
        <span class="n">${count}</span>
      </li>`).join('');
}

function paperHeader(key, reading = false) {
  const p = S.papers.find((x) => x.key === key);
  if (!p) return '';
  const url = (p.source || {}).url;
  const title = url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(p.title || key)}</a>`
                    : esc(p.title || key);
  return readingHeader(p, title, reading);
}

// Both paper layouts share the same reading actions. Analysis and maintenance
// commands stay reachable in a disclosure, with native keyboard navigation.
function readingHeader(p, title, reading) {
  const key = esc(p.key);
  const existing = $('paperhead').querySelector('.paper-actions');
  const open = existing?.dataset.paper === p.key && existing.open;
  const ai = S.ai_enabled === true ? '' : 'disabled';
  const progress = (S.notebook?.reading || []).find(r => r.paper === p.key);
  return `<div class="paperhead reading-head">
    <div class="reading-title"><h2>${title}</h2>
      <p class="pm">${esc((p.authors || []).join(', ') || 'Authors unknown')}${p.year ? ` · ${esc(p.year)}` : ''}${p.venue ? ` · ${esc(p.venue)}` : ''}</p></div>
    <div class="reading-actions" data-paper="${key}">
      <button type="button" class="primary" data-tool="reader" data-paper="${key}" ${p.has_pdf ? '' : 'disabled'}
        ${p.has_pdf ? '' : 'title="Add a PDF to read this paper"'}>Read</button>
      <button type="button" data-paper-chat="${key}" data-ai-action ${p.has_pdf ? ai : 'disabled'}
        ${p.has_pdf ? '' : 'title="Add a PDF to ask about this paper"'}>Ask about this paper</button>
      <button type="button" data-tool="${progress ? 'reading' : 'queue-paper'}" data-paper="${key}">${progress ? 'View reading queue' : 'Add to reading queue'}</button>
      <details class="paper-actions" data-paper="${key}" ${open ? 'open' : ''}>
        <summary>Paper actions</summary>
        <div class="paper-actions-menu">
          <button type="button" data-act="add-claim" data-paper="${key}">Add claim by hand</button>
          ${p.has_pdf ? `<button type="button" data-act="toggle-pdf" data-paper="${key}">${reading ? 'Hide PDF' : 'Show PDF'}</button>
          <button type="button" data-act="verify" data-paper="${key}">Check quotes</button>` : ''}
          ${p.n_unreviewed ? `<button type="button" data-act="review-all" data-paper="${key}">Mark ${p.n_unreviewed} reviewed</button>` : ''}
          <button type="button" data-act="labels" data-paper="${key}">Labels</button>
          <button type="button" data-tool="history" data-paper="${key}">Edit history</button>
          <hr>
          <button type="button" data-act="reextract" data-paper="${key}" data-ai-action ${ai}>Re-read paper</button>
          <button type="button" data-act="retag-one" data-paper="${key}" data-ai-action ${ai}>Retag claims</button>
          ${p.has_pdf ? `<a href="/pdf/${encodeURIComponent(p.key)}?${workspaceQuery()}" target="_blank" rel="noopener">Open original PDF ↗</a>` : ''}
          <button type="button" data-reader-command="btn-export">Export HTML</button>
          <button type="button" data-reader-command="btn-bib">BibTeX</button>
          <button type="button" data-reader-command="btn-help">Keyboard shortcuts</button>
          <hr><button type="button" class="danger" data-act="del-paper" data-paper="${key}">Remove paper</button>
        </div>
      </details>
    </div>
  </div>`;
}

function paperReadingProgress(key) {
  const progress = (S.notebook?.reading || []).find(r => r.paper === key);
  if (!progress) return 'Not started';
  const status = {queued: 'In reading queue', reading: 'Reading', finished: 'Finished'}[progress.status];
  return `${status}${progress.status !== 'queued' ? ` · Page ${progress.page}` : ''}`;
}

// Page saves can finish while a note editor holds off redraws. Update only the
// progress text, leaving the editor and mounted PDF in place.
function renderPaperReadingProgress() {
  document.querySelectorAll('[data-paper-progress]').forEach(node => {
    node.textContent = paperReadingProgress(node.dataset.paperProgress);
  });
}

function paperOverview(paper) {
  return `<section class="paper-overview" aria-labelledby="paper-overview-title">
    <h3 id="paper-overview-title">Paper overview</h3>
    <div class="paper-reading-progress"><span class="who">Reading progress</span>
      <p data-paper-progress="${esc(paper.key)}">${esc(paperReadingProgress(paper.key))}</p></div>
    ${(paper.labels || []).length ? `<div class="labels">${paper.labels.map(labelChip).join('')}</div>` : ''}
    <div class="paper-summary"><h4>Summary</h4>
      ${paper.summary ? `<p>${esc(paper.summary)}</p>` : '<p class="hint">No summary yet. You can read the paper and add your own notes.</p>'}</div>
    ${paper.relevance ? `<div class="paper-relevance"><h4>Why it matters</h4><p>${esc(paper.relevance)}</p></div>` : ''}
    ${paperNote(paper.key)}
    ${proposedPanel(paper.key)}
  </section>`;
}

// A note is shown as the reader typed it: blank lines make paragraphs and
// nothing else is interpreted. The same in the export.
function paragraphs(text) {
  return String(text || '').split(/\n\s*\n/).filter((p) => p.trim());
}

function noteHtml(text, label, actions = '') {
  return `<div class="note">
    <span class="who">${esc(label)}</span>
    ${paragraphs(text).map((p) => `<p>${esc(p)}</p>`).join('')}
    ${actions}
  </div>`;
}

// The reader's own note on the paper, under the model's summary of it. No
// pass reads it and no pass writes it, which is the point: it is the place
// for what you think that no extraction could have told you.
function paperNote(key) {
  const paper = S.papers.find((x) => x.key === key);
  if (!paper) return '';
  const draft = V.noteDrafts[key];
  // Empty notes are ready to type into. Merely showing the field must not
  // hold background updates or count as an unsaved edit on workspace changes.
  if (draft !== undefined && !isRemoving(key)) V.noteEditing = key;
  if (V.noteEditing === key || (!(paper.notes || '').trim() && !isRemoving(key))) {
    const text = draft !== undefined ? draft : (paper.notes || '');
    // Frozen from state rather than from the elements, so a redraw during the
    // save cannot hand back an editor whose request is still running.
    const busy = V.noteSaving === key ? ' disabled' : '';
    return `<div class="note editing${busy ? ' saving' : ''}">
      <label class="who" for="paper-note-${esc(key)}">my note</label>
      <textarea id="paper-note-${esc(key)}" data-note="${esc(key)}" rows="4"${busy}
        placeholder="What you make of this paper: what to read it for, what it gets wrong, what to do about it.">${esc(text)}</textarea>
      <div class="smeta">
        <span class="hint">Yours. Nothing sends it to a model.</span>
        <span class="cact" style="margin-left:auto">
          <button type="button" data-act="save-note" data-paper="${esc(key)}" class="primary"${busy}>Save</button>
          <button type="button" data-act="cancel-note" data-paper="${esc(key)}"${busy}>Cancel</button>
        </span>
      </div>
    </div>`;
  }
  if (!(paper.notes || '').trim()) return '';
  return noteHtml(paper.notes, 'my note', `<div class="smeta">
    <span class="cact" style="margin-left:auto">
      <button type="button" data-act="edit-note" data-paper="${esc(key)}">edit</button>
    </span></div>`);
}

// Keep what was typed, close the editor — as `parkSynthEditor` does, and for
// the same reason: the header goes with the paper, so leaving it must not
// leave `V.noteEditing` pointing at a textarea nobody can see, which would
// also stop the background poll.
function parkNoteEditor() {
  if (!V.noteEditing) return;
  const field = document.querySelector(`textarea[data-note="${CSS.escape(V.noteEditing)}"]`);
  if (field) V.noteDrafts[V.noteEditing] = field.value;
  V.noteEditing = null;
}

function cancelNoteEdit() {
  // A claim editor open alongside holds its text in the DOM until it is read,
  // and the redraw below rebuilds it from the draft on file.
  captureOpenEditor();
  if (V.noteEditing) delete V.noteDrafts[V.noteEditing];   // discard only this paper's draft
  V.noteEditing = null;
  renderContent();
}

async function saveNote(key) {
  if (V.noteSaving) {
    // One at a time, as a synthesis is. Another paper's Save is drawn enabled
    // — the freeze is keyed to the paper being written — so it says why
    // rather than doing nothing.
    if (V.noteSaving !== key) toast('Wait for the note being saved to finish.', { tone: 'warn' });
    return;
  }
  // The header stands until the removal comes back, so this is still
  // clickable. The PATCH landing first would report a note saved that the
  // DELETE behind it throws away; landing second it is a 404.
  if (refuseWhileRemoving(key, 'the note')) return;
  const field = document.querySelector(`textarea[data-note="${CSS.escape(key)}"]`);
  const text = field ? field.value : (V.noteDrafts[key] || '');
  V.error = null;
  captureOpenEditor();        // as on cancel: a claim editor's text is only in the DOM
  V.noteDrafts[key] = text;   // the redraw draws the editor from the draft
  V.noteSaving = key;
  renderContent();
  try {
    await api(`/api/papers/${encodeURIComponent(key)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes: text }),
    });
    // Only this paper's editor. The save is slow enough to move to another
    // paper and open its note meanwhile, and closing that one would take the
    // reader out of a note they are still writing.
    if (V.noteEditing === key) V.noteEditing = null;
    delete V.noteDrafts[key];
  } catch (error) {
    V.noteDrafts[key] = text;   // keep what was typed so the save can be retried
    V.error = `Could not save the note: ${error.message}`;
  } finally {
    // In the `finally`, as the synthesis save is: a failure leaves the editor
    // frozen otherwise, with nothing left to hand it back.
    V.noteSaving = null;
    await refreshAllShowingError();
  }
}

// Clicking an existing label edits the same draft as typing; only OK saves it.
async function editLabels(key) {
  const paper = S.papers.find((x) => x.key === key);
  if (!paper) return;
  // The paper's own spelling of a label wins over the sidebar's.
  const inUse = [...(paper.labels || []), ...Object.keys(S.label_counts || {})
    .filter((name) => findLabel(paper.labels, name) === undefined)]
    .sort((a, b) => compareText(labelKey(a), labelKey(b)) || compareText(a, b));
  const answer = await askText(`Labels for ${paper.title || key}`, {
    note: (inUse.length ? 'Click a label to add or remove it. ' : '')
      + 'Type new labels separated by commas. Spaces are allowed within a label.',
    placeholder: 'open source, reading list',
    value: (paper.labels || []).join(', '),
    labelChoices: inUse,
  });
  if (answer === null) return;   // cancelled: not the same as clearing them
  const labels = parseLabels(answer);
  try {
    await api(`/api/papers/${encodeURIComponent(key)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ labels }),
    });
  } catch (e) {
    toast(`Could not save the labels: ${e.message}`);
    return;
  }
  // `refreshAll` rather than `refresh`: the chips are part of the paper's
  // header, which `render` leaves alone while a claim editor is open, and the
  // labels would not appear until that editor was closed. The draft in it is
  // kept across the rebuild.
  await refreshAll();
}

function paperCache() {
  window.__paperCache = window.__paperCache || {};
  const workspace = workspaceClient.workspaceId || 'default';
  window.__paperCache[workspace] = window.__paperCache[workspace] || {};
  return window.__paperCache[workspace];
}

function proposedPanel(key) {
  const paper = S.papers.find((x) => x.key === key);
  if (!paper || !paper.n_proposed_tags) return '';
  // Keyed by the paper's updated timestamp, so a re-read that replaces the
  // proposals invalidates the cache instead of showing names the server will
  // no longer accept.
  const entry = paperCache()[key];
  const fresh = entry && entry !== 'loading' && entry.updated === paper.updated;
  const proposed = fresh ? entry.proposed_tags : null;
  if (!proposed) {
    loadProposed(key, paper.updated);
    return `<div class="proposed">Loading proposed topics…</div>`;
  }
  if (!proposed.length) return '';
  return `<div class="proposed">
    <div><strong>Proposed topics</strong> — accept the ones worth keeping in the vocabulary.</div>
    ${proposed.map((t) => `<div class="row">
      <span class="pn">${esc(t.name)}</span>
      <span class="hint" style="flex:1">${esc(t.description)}</span>
      <button type="button" data-act="accept-tag" data-paper="${esc(key)}" data-tag="${esc(t.name)}">Accept</button>
      <button type="button" data-act="reject-tag" data-paper="${esc(key)}" data-tag="${esc(t.name)}">Discard</button>
    </div>`).join('')}
  </div>`;
}

async function loadProposed(key, wanted) {
  const cache = paperCache();
  if (cache[key] === 'loading') return;
  cache[key] = 'loading';
  try {
    const paper = await api(`/api/papers/${encodeURIComponent(key)}`);
    cache[key] = { updated: paper.updated, proposed_tags: paper.proposed_tags || [] };
    if (wanted && paper.updated !== wanted) delete cache[key];  // changed again mid-flight
    if (!V.editing) renderContent();
  } catch (e) { delete cache[key]; }
}

// `shown` carries the claim ids already drawn as an editor this pass. In grouped
// mode a claim appears once per topic, and emitting a form for each occurrence
// would put several elements under one `data-form`, so `captureOpenEditor` would
// read the first while the user typed into another.
// `group` is which drawing of the claim this is: the topic heading it sits
// under, or the tension it belongs to. A claim with several tags is drawn
// under each of them, and a passage belongs beside the copy whose button was
// pressed rather than the first one on the page.
const EVIDENCE_RELATIONSHIPS = {unassessed:'Independence not assessed', original:'Original experiment', independent_replication:'Independent replication (your assessment)', shared_data:'Uses shared data or experiments'};
function evidenceQualityHtml(row) {
  const quote = row.quote_verified === true ? 'Quote found in PDF' : row.quote_verified === false ? 'Quote not found in PDF' : 'Quote not checked';
  return `<div class="evidence-quality"><span>${row.reviewed ? 'Reviewed by you' : 'Needs human review'}</span><span>${quote}</span><span>${esc(EVIDENCE_RELATIONSHIPS[row.evidence_relationship || 'unassessed'])}</span></div>
    ${row.conditions ? `<p class="cev"><strong>Conditions:</strong> ${esc(row.conditions)}</p>` : ''}
    ${row.limitations ? `<p class="cev"><strong>Limitations:</strong> ${esc(row.limitations)}</p>` : ''}
    ${row.evidence_origin ? `<p class="hint">Evidence origin: ${esc(row.evidence_origin)}</p>` : ''}`;
}
function paperReadiness(paper) {
  const active = S.jobs.find(j => j.key === paper.key && ['queued','fetching','reading'].includes(j.state));
  if (active) return active.state === 'reading' ? 'Analyzing' : 'Import in progress';
  if (!paper.has_pdf) return paper.error ? 'PDF unavailable — retry import' : 'No PDF — add a source';
  if (paper.n_unreviewed) return `${paper.n_unreviewed} to review`;
  if (paper.n_claims) return 'Claims reviewed';
  if (paper.has_extraction) return 'No claims found — read manually';
  return S.ai_enabled === true ? 'Ready to read or analyze' : 'Ready to read — AI off';
}

function claimCard(row, shown, group = '') {
  if (V.editing === row.id && shown && !shown.has(row.id)) {
    shown.add(row.id);
    const draft = V.drafts[row.id];
    return editForm(draft ? { ...row, ...draft } : row);
  }
  const cite = `${(row.paper_authors || [])[0] ? row.paper_authors[0].split(' ').pop() : row.paper}`
    + ` ${row.paper_year || ''}`;
  const tags = (row.tags || []).map((t) => `<span class="tag" data-tag="${esc(t)}">#${esc(t)}</span>`).join(' ');
  const links = (row.ledger_links || []).map((l) => {
    const own = S.ledger.find((c) => c.id === l.claim);
    return `<div class="link"><span class="rel">${esc(l.relation)}</span>
      ${esc(own ? own.text : l.claim)}${l.note ? ' — ' + esc(l.note) : ''}</div>`;
  }).join('');
  return `<div class="claim ${esc(row.strength)} ${row.reviewed ? '' : 'unreviewed'} ${row.id === V.selectedId ? 'sel' : ''}"
       data-claim="${esc(row.id)}" data-paper="${esc(row.paper)}" data-group="${esc(group)}">
    ${ResearchTools.claimCheckbox(row)}
    <p class="ctext"><span class="kind ${esc(row.kind)}">${esc(row.kind)}</span> ${esc(row.text)}</p>
    <div class="cmeta">
      ${tags}
      <span data-act="open-paper" data-paper="${esc(row.paper)}" style="cursor:pointer"
        title="${esc(row.paper_title || row.paper)}">${esc(cite)}</span>
      ${row.locator ? '· ' + esc(row.locator) : ''}
      ${claimPdfLink(row)}
      ${tensionMarker(row.id)}
      ${agreementMarker(row.id)}
      <span class="cact">
        <button type="button" data-act="review" data-claim="${esc(row.id)}" data-paper="${esc(row.paper)}">
          ${row.reviewed ? 'reviewed' : 'mark reviewed'}</button>
        <button type="button" data-act="edit" data-claim="${esc(row.id)}">edit</button>
        <button type="button" data-act="similar" data-claim="${esc(row.id)}" data-paper="${esc(row.paper)}"
          data-group="${esc(group)}"
          title="Claims from other papers that use the same words">${similarOpen(row.id, group) ? 'hide alike' : 'alike'}</button>
        <button type="button" data-tool="history" data-paper="${esc(row.paper)}" data-claim="${esc(row.id)}">history</button>
        <button type="button" data-act="del" data-claim="${esc(row.id)}" data-paper="${esc(row.paper)}">delete</button>
      </span>
    </div>
    ${evidenceQualityHtml(row)}
    ${row.evidence ? `<p class="cev">${esc(row.evidence)}</p>` : ''}
    ${quoteHtml(row, group)}
    ${(row.note || '').trim() ? noteHtml(row.note, 'my note') : ''}
    ${similarHtml(row, group)}
    ${links}
  </div>`;
}

// Checking a quote means reading the paper, so the paper is one click from the
// claim, at the page its locator names when it names one.
function claimPdfLink(row) {
  const paper = S.papers.find((p) => p.key === row.paper);
  if (!paper || !paper.has_pdf) return '';
  const page = pdfPage(row.locator);
  const href = `/pdf/${encodeURIComponent(row.paper)}?${workspaceQuery()}&inline=1${page ? `#page=${page}` : ''}`;
  return `<a class="pdflink" href="${esc(href)}" target="_blank" rel="noopener"
    data-paper="${esc(row.paper)}" data-page="${esc(page || '')}"
    title="${page ? `Open the PDF at page ${page}` : 'Open the PDF'}">PDF${page ? ' p.' + esc(page) : ''}</a>`;
}

// 'p. 4' and 'pp. 4-5' name a page. 'Table 2' and 'Sec. 3.1' do not, and a
// section number followed as a page number would land the reader somewhere
// else in the paper with nothing to say it had gone wrong.
function pdfPage(locator) {
  const match = /(?:^|[^a-z])p{1,2}\.?\s*(\d{1,4})|(?:^|[^a-z])pages?\s*(\d{1,4})/i.exec(locator || '');
  return match ? (match[1] || match[2]) : null;
}

// The quote, flagged when it was not found in the paper's text. A quote the
// model paraphrased or invented is the commonest extraction error, and this is
// the one error the machine can catch on its own.
function quoteHtml(row, group = '') {
  if (!row.quote) return '';
  const flag = row.quote_verified === false
    ? '<span class="qflag" title="This quote was not found in the PDF text. Check it against the paper.">not found in PDF</span> '
    : '';
  // Beside the copy whose button was pressed. Grouped by topic a claim with
  // several tags is drawn under each of them, and in the tensions view it
  // appears in every pair it is part of; without the group the passage would
  // open under all of them, and keyed on the first drawn it would open
  // somewhere the reader is not looking.
  const open = Boolean(V.quoteContext && V.quoteContext.claim === row.id
    && (V.quoteContext.group || '') === group);
  const show = `<button type="button" class="qshow" data-act="quote-context"
    data-claim="${esc(row.id)}" data-paper="${esc(row.paper)}" data-group="${esc(group)}"
    title="Read this quote where it sits in the PDF">${open ? 'hide the paper' : 'in the paper'}</button>`;
  const copy = `<button type="button" class="copy" data-act="copy-quote" data-claim="${esc(row.id)}"
    title="Copy the quote">copy</button>`;
  return `<blockquote>${flag}${esc(row.quote)} ${show}${copy}</blockquote>
    ${open ? quoteContextHtml(row) : ''}`;
}

// The passage of the PDF the quote was matched against, read out of the text
// already extracted for the check. A quote the model reworded is the commonest
// extraction error and the slowest to correct by hand, so the paper's own
// sentence is offered as a replacement rather than left to be copied out of
// the PDF.
function quoteContextHtml(row) {
  const ctx = V.quoteContext;
  if (ctx.loading) return '<div class="qctx">Reading the PDF…</div>';
  if (ctx.error) return `<div class="qctx"><span class="qflag">${esc(ctx.error)}</span></div>`;
  const found = ctx.data;
  if (!found.available) return `<div class="qctx">${esc(found.reason)}</div>`;
  // The passage was worked out from the quote and locator the server held when
  // it was asked. If they have moved since — another tab, another process, the
  // CLI — it describes a claim that is no longer there, and offering to write
  // its suggestion back would undo that edit.
  if (found.quote !== row.quote || (found.locator || '') !== (row.locator || '')) {
    return '<div class="qctx">This claim changed while the passage was open. Open it again.</div>';
  }
  if (!found.suggestion) {
    return '<div class="qctx">No passage in this PDF resembles this quote.</div>';
  }
  const where = found.page
    ? `${found.found ? 'Found on' : 'Closest passage,'} page ${found.page} of ${found.pages}`
    : (found.found ? 'Found in the PDF' : 'Closest passage');
  // The locator is the model's own answer to the same question, and it is
  // wrong often enough to be worth showing side by side with the real page.
  // Unless the quote is in the paper more than once: then the page shown is
  // the first of them, and the locator naming another one is no disagreement.
  const elsewhere = found.repeated
    ? '<span class="qlocator" title="The passage shown is the first of them.">appears more than once</span>'
    : (found.locator_page && found.page && found.locator_page !== found.page
      ? `<span class="qlocator" title="The claim's locator names a different page.">locator says ${esc(row.locator)}</span>`
      : '');
  const diff = found.diff.length
    ? `<p class="qdiff">${found.diff.map((part) => {
        if (part.op === 'quote') return `<del>${esc(part.text)}</del>`;
        if (part.op === 'paper') return `<ins>${esc(part.text)}</ins>`;
        return esc(part.text);
      }).join(' ')}</p>`
    : '';
  const replace = found.suggestion === row.quote ? '' : `<button type="button" class="primary"
    data-act="use-wording" data-claim="${esc(row.id)}" data-paper="${esc(row.paper)}"
    title="Replace the quote with the sentence as the paper writes it">Use the paper's wording</button>`;
  return `<div class="qctx">
    <div class="qwhere">${esc(where)} ${elsewhere}</div>
    <p class="qpassage">${esc(found.before)} <mark>${esc(found.suggestion)}</mark> ${esc(found.after)}</p>
    ${diff}
    <div class="qacts">${replace}</div>
  </div>`;
}

// Drop the alike panel, taking it off the screen even when no redraw will
// come: `render` leaves the content alone while an editor is open, and a
// panel left standing there shows matches worked out from claims that have
// since moved, under a button that says "hide alike" and would start a new
// lookup because the state says it is closed.
function closeSimilar() {
  if (!V.similar) return;
  V.similar = null;
  document.querySelectorAll('#content .alike').forEach((node) => node.remove());
  document.querySelectorAll('#content [data-act="similar"]').forEach((button) => {
    button.textContent = 'alike';
  });
}

function similarOpen(claimId, group = '') {
  return Boolean(V.similar && V.similar.claim === claimId
    && (V.similar.group || '') === group);
}

// Claims from other papers that use the same words as this one. Worked out
// here rather than by the model, so it costs nothing and says nothing about
// what the two claims mean: the reader decides whether they bear on each
// other, and the tensions and agreements passes are not filtered by it.
function similarHtml(row, group = '') {
  // Beside the copy whose button was pressed, as the passage is: a claim with
  // several tags is drawn under each of them, and in the tensions view it
  // appears in every pair it is part of.
  if (!similarOpen(row.id, group)) return '';
  const found = V.similar;
  if (found.loading) return '<div class="alike">Comparing the claims…</div>';
  if (found.error) return `<div class="alike"><span class="qflag">${esc(found.error)}</span></div>`;
  if (!found.rows.length) {
    return '<div class="alike">No claim from another paper is worded much like this one.</div>';
  }
  return `<div class="alike">${found.rows.map((hit) => {
    const cite = `${(hit.paper_authors || [])[0] ? hit.paper_authors[0].split(' ').pop() : hit.paper} ${hit.paper_year || ''}`;
    return `<div class="alikerow" data-act="goto-claim" data-claim="${esc(hit.claim)}">
      <span class="pt">${esc(cite)}</span> ${esc(hit.text)}</div>`;
  }).join('')}</div>`;
}

function tensionMarker(claimId) {
  if (V.view === 'tensions') return '';   // the card is already inside a tension
  const involved = tensionsFor(claimId);
  if (!involved.length) return '';
  const confirmed = involved.every((t) => t.status === 'confirmed');
  const label = involved.length === 1 ? 'in tension with 1 claim' : `in tension with ${involved.length} claims`;
  return `<span class="tmark ${confirmed ? 'confirmed' : ''}" data-act="tension-focus"
    data-claim="${esc(claimId)}" title="Show the tensions this claim is part of">⚡ ${esc(label)}</span>`;
}

function agreementMarker(claimId) {
  if (V.view === 'agreements') return '';
  const involved = agreementsFor(claimId);
  if (!involved.length) return '';
  const papers = new Set(involved.flatMap((a) => a.claims.map((c) => c.paper)));
  papers.delete((S.claims.find((c) => c.id === claimId) || {}).paper);
  const confirmed = involved.every((a) => a.status === 'confirmed');
  const label = `${papers.size} other ${papers.size === 1 ? 'paper agrees' : 'papers agree'}`;
  return `<span class="amark ${confirmed ? 'confirmed' : ''}" data-act="agreement-focus"
    data-claim="${esc(claimId)}" title="Show the agreements this claim is part of">≈ ${esc(label)}</span>`;
}

function agreementCard(a) {
  const topics = (a.topics || []).map((x) => `<span class="tag" data-tag="${esc(x)}">#${esc(x)}</span>`).join(' ');
  const actions = [];
  if (a.stale || a.status !== 'confirmed') actions.push(`<button type="button" data-act="agreement-status" data-agreement="${esc(a.id)}" data-status="confirmed">Confirm</button>`);
  if (a.stale || a.status !== 'dismissed') actions.push(`<button type="button" data-act="agreement-status" data-agreement="${esc(a.id)}" data-status="dismissed">Dismiss</button>`);
  if (a.status !== 'open') actions.push(`<button type="button" data-act="agreement-status" data-agreement="${esc(a.id)}" data-status="open">Reopen</button>`);
  actions.push(`<button type="button" data-act="agreement-delete" data-agreement="${esc(a.id)}">delete</button>`);
  return `<div class="tcard ${esc(a.status)}" data-agreement="${esc(a.id)}">
    <div class="thead">
      <span class="kind agreement">${a.n_papers} papers</span>
      <span class="st ${esc(a.status)}">${esc(a.status)}</span>
      ${topics}
      <span class="cact">${actions.join('')}</span>
    </div>
    ${a.note ? `<p class="tnote">${esc(a.note)}</p>` : ''}
    <div class="tgroup">${a.claims.map((row) => tensionClaimCard(row, a.id)).join('')}</div>
    ${a.stale ? '<p class="stale">A claim here was edited or removed after this was found. Re-run Find agreements to re-judge it, or decide it yourself.</p>' : ''}
  </div>`;
}

function visibleAgreements() {
  return (S.agreements || []).filter((a) =>
    (!V.agreementStatus || a.status === V.agreementStatus)
    && (!V.tag || (a.topics || []).includes(V.tag))
    && (!V.agreementFocus || a.claims.some((c) => c.id === V.agreementFocus)));
}

function renderAgreements() {
  const main = $('main');
  const scrollTop = main ? main.scrollTop : 0;
  const rows = visibleAgreements();
  const statuses = S.tension_statuses || ['open', 'confirmed', 'dismissed'];
  // Drawn off `V`, as in `renderTensions`: the claim a focus names can go, and
  // the way out of the focus must not go with it.
  const focus = V.agreementFocus ? S.claims.find((c) => c.id === V.agreementFocus) : null;
  let html = `<div class="paperhead">
    <h2>Where papers agree</h2>
    <p class="ps">Findings that several papers make: the same question, answered the same way.
      The count is how many papers make it, which is the answer to "how much evidence do I
      have for this". Confirm the ones that hold up, dismiss the rest.</p>
    <div class="row">
      <select id="agreement-status">
        <option value="" ${V.agreementStatus ? '' : 'selected'}>every status</option>
        ${statuses.map((st) => `<option value="${esc(st)}" ${V.agreementStatus === st ? 'selected' : ''}>${esc(st)}</option>`).join('')}
      </select>
      ${V.tag ? `<span class="hint">in #${esc(V.tag)}</span>` : ''}
      ${V.agreementFocus ? `<span class="hint">${focus
          ? `involving: <em>${esc(focus.text.slice(0, 80))}${focus.text.length > 80 ? '…' : ''}</em>`
          : 'involving a claim that is no longer here'}</span>
                 <button type="button" data-act="agreement-unfocus">show all</button>` : ''}
      <button type="button" data-act="find-agreements" data-ai-action ${S.ai_enabled === true ? '' : 'disabled'} style="margin-left:auto">Find agreements</button>
    </div>
  </div>`;
  if (V.error) html += `<p class="warn">${esc(V.error)}</p>`;
  if (!rows.length) {
    html += (S.agreements || []).length
      ? '<p class="empty">No agreements match these filters.</p>'
      : '<p class="empty">Nothing found yet. Find agreements asks the model, topic by topic, which claims from different papers assert the same finding.</p>';
  } else {
    html += rows.map(agreementCard).join('');
  }
  $('content').innerHTML = html;
  if (main) main.scrollTop = scrollTop;
}

async function findAgreements() {
  V.error = null;
  // The pass reads the corpus, so it must not read a claim the reader has
  // deleted — and it must read the corpus they were looking at.
  const workspace = await settleDeletes();
  if (!workspace) return;   // the delete failed; the pass would read the claim
  try {
    const result = await api('/api/agreements', {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...workspace }, body: '{}',
    });
    if (!result.queued) {
      V.error = 'No topic has claims from two papers yet, so there is nothing to compare.';
    }
  } catch (error) {
    V.error = `Could not start the pass: ${error.message}`;
  }
  await refreshShowingError();
}

// A claim as it appears inside a tension: the same card, minus the review and
// edit controls. Editing belongs to the claims view, where the editor's
// lifecycle is handled; a click on the citation goes there.
// `group` is the tension or agreement this drawing of the claim belongs to;
// one claim can be in several, and a passage opens beside the one it was
// asked for. See `claimCard`.
function tensionClaimCard(row, group = '') {
  const cite = `${(row.paper_authors || [])[0] ? row.paper_authors[0].split(' ').pop() : row.paper}`
    + ` ${row.paper_year || ''}`;
  return `<div class="claim ${esc(row.strength)} ${row.reviewed ? '' : 'unreviewed'}" data-tclaim="${esc(row.id)}">
    <p class="ctext"><span class="kind ${esc(row.kind)}">${esc(row.kind)}</span> ${esc(row.text)}</p>
    <div class="cmeta">
      <span data-act="open-paper" data-paper="${esc(row.paper)}" style="cursor:pointer"
        title="${esc(row.paper_title || row.paper)}">${esc(cite)}</span>
      ${row.locator ? '· ' + esc(row.locator) : ''}
      ${row.reviewed ? '' : '· <span class="hint">unreviewed</span>'}
    </div>
    ${evidenceQualityHtml(row)}
    ${row.evidence ? `<p class="cev">${esc(row.evidence)}</p>` : ''}
    ${quoteHtml(row, group)}
    ${(row.note || '').trim() ? noteHtml(row.note, 'my note') : ''}
  </div>`;
}

function tensionCard(t) {
  const topics = (t.topics || []).map((x) => `<span class="tag" data-tag="${esc(x)}">#${esc(x)}</span>`).join(' ');
  // A stale tension keeps both decisions on offer whatever its status: deciding
  // it again against the current text is what clears the stale mark, and a
  // reviewer who still agrees should not have to reopen it first.
  const actions = [];
  if (t.stale || t.status !== 'confirmed') actions.push(`<button type="button" data-act="tension-status" data-tension="${esc(t.id)}" data-status="confirmed">Confirm</button>`);
  if (t.stale || t.status !== 'dismissed') actions.push(`<button type="button" data-act="tension-status" data-tension="${esc(t.id)}" data-status="dismissed">Dismiss</button>`);
  if (t.status !== 'open') actions.push(`<button type="button" data-act="tension-status" data-tension="${esc(t.id)}" data-status="open">Reopen</button>`);
  return `<div class="tcard ${esc(t.status)}" data-tension="${esc(t.id)}">
    <div class="thead">
      <span class="kind ${esc(t.kind)}">${esc(t.kind)}</span>
      <span class="st ${esc(t.status)}">${esc(t.status)}</span>
      ${topics}
      <span class="cact">${actions.join('')}</span>
    </div>
    <div class="tpair">${t.claims.map((row) => tensionClaimCard(row, t.id)).join('')}</div>
    ${t.note ? `<p class="tnote">${esc(t.note)}</p>` : ''}
    ${t.stale ? '<p class="stale">A claim here was edited after this was found. Re-run Find tensions to re-judge it, or decide it yourself.</p>' : ''}
  </div>`;
}

function visibleTensions() {
  return (S.tensions || []).filter((t) =>
    (!V.tensionStatus || t.status === V.tensionStatus)
    && (!V.tag || (t.topics || []).includes(V.tag))
    && (!V.tensionFocus || t.claims.some((c) => c.id === V.tensionFocus)));
}

function renderTensions() {
  const main = $('main');
  const scrollTop = main ? main.scrollTop : 0;
  const rows = visibleTensions();
  const statuses = S.tension_statuses || ['open', 'confirmed', 'dismissed'];
  // The claim a focus names can go while this view is standing: a delete made
  // in another window, or a re-read of its paper landing behind a poll. The
  // focus goes on filtering either way — every tension citing it has gone with
  // it, so the list is empty — which is why "show all" is drawn off `V` rather
  // than off the row it found. Keyed to the row, it would disappear at exactly
  // the moment it is the only thing left to click.
  const focus = V.tensionFocus ? S.claims.find((c) => c.id === V.tensionFocus) : null;
  let html = `<div class="paperhead">
    <h2>Where papers disagree</h2>
    <p class="ps">Pairs of claims from different papers that pull against each other on one
      question. A <span class="kind contradiction">contradiction</span> cannot have both sides
      true; a <span class="kind tension">tension</span> might be explained by a difference in
      setup. Confirm the ones that hold up, dismiss the rest.</p>
    <div class="row">
      <select id="tension-status">
        <option value="" ${V.tensionStatus ? '' : 'selected'}>every status</option>
        ${statuses.map((st) => `<option value="${esc(st)}" ${V.tensionStatus === st ? 'selected' : ''}>${esc(st)}</option>`).join('')}
      </select>
      ${V.tag ? `<span class="hint">in #${esc(V.tag)}</span>` : ''}
      ${V.tensionFocus ? `<span class="hint">${focus
          ? `involving: <em>${esc(focus.text.slice(0, 80))}${focus.text.length > 80 ? '…' : ''}</em>`
          : 'involving a claim that is no longer here'}</span>
                 <button type="button" data-act="tension-unfocus">show all</button>` : ''}
      <button type="button" data-act="find-tensions" data-ai-action ${S.ai_enabled === true ? '' : 'disabled'} style="margin-left:auto">Find tensions</button>
    </div>
  </div>`;
  if (V.error) html += `<p class="warn">${esc(V.error)}</p>`;
  if (!rows.length) {
    html += (S.tensions || []).length
      ? '<p class="empty">No tensions match these filters.</p>'
      : '<p class="empty">Nothing found yet. Find tensions asks the model, topic by topic, which claims from different papers disagree.</p>';
  } else {
    html += rows.map(tensionCard).join('');
  }
  $('content').innerHTML = html;
  if (main) main.scrollTop = scrollTop;
}

// --- research: the context and the ledger, edited in place -----------------
//
// context.md and ledger.yaml are the two inputs that most shape what
// extraction returns, and both used to need a text editor outside the app.
// One form edits both; Save writes both.

function ledgerRow(claim, i) {
  return `<div class="linkrow" data-ledger-row="${i}">
    <input name="ledger-id" value="${esc(claim.id || '')}" placeholder="L${i + 1}" size="6" aria-label="Claim id">
    <input name="ledger-text" value="${esc(claim.text || '')}" placeholder="One of my own claims, as a sentence" aria-label="Claim text">
    <button type="button" data-act="drop-ledger" title="Remove this claim">×</button>
  </div>`;
}

// The research form's edits live only in the DOM until Save, and every other
// redraw (a filter, a paper click, the topic list) rebuilds `#content`. Read
// the form into `V.researchDraft` before any of that, and draw from the draft
// when there is one, so navigating away and back finds the text as it was.
function captureResearchDraft() {
  if (!$('research-form')) return;
  const current = readResearchForm();
  // A form nobody has typed into is not a draft. It is judged against what
  // it was drawn from, not against `S`: a poll can replace `S` while the form
  // is open, and an untouched form would then look edited, be kept, and
  // later write the old values over the change made elsewhere.
  V.researchDraft = differs(current, V.researchBase || storedResearch()) ? current : null;
}

function storedResearch() {
  return {
    context: S.context || '',
    claims: (S.ledger || []).map((c) => ({ id: c.id || '', text: c.text || '' })),
  };
}

function differs(a, b) {
  return a.context.trim() !== b.context.trim()
    || JSON.stringify(a.claims) !== JSON.stringify(b.claims);
}

function renderResearch() {
  captureResearchDraft();
  const main = $('main');
  const scrollTop = main ? main.scrollTop : 0;
  const draft = V.researchDraft;
  // Drawn from the server, the form remembers what it was drawn from, so a
  // later poll cannot make an untouched form look edited. A draft keeps the
  // base it was typed against.
  if (!draft) V.researchBase = storedResearch();
  const context = draft ? draft.context : (S.context || '');
  const ledger = draft ? draft.claims : (S.ledger || []);
  let html = `<div class="paperhead">
    <h2>What I am studying</h2>
    <p class="ps">The context goes into every model pass and is the main lever on the
      <em>why it is here</em> line. Your own claims are what a paper's claims are linked
      against: supports, contradicts, supplies a method for, refines.</p>
  </div>`;
  if (V.error) html += `<p class="warn">${esc(V.error)}</p>`;
  html += `<form class="edit research" id="research-form">
    <div><label for="research-context">Research context</label>
      <textarea id="research-context" name="context" rows="8"
        placeholder="What the research is about, and what makes a paper relevant to it.">${esc(context)}</textarea></div>
    <div><label>My own claims</label>
      <div class="links" id="ledger-rows">${ledger.map(ledgerRow).join('')}</div>
      <div class="row"><button type="button" data-act="add-ledger">Add a claim</button></div></div>
    <div class="row right">
      <button type="button" data-act="cancel-research">Cancel</button>
      <button type="submit" class="primary">Save</button>
    </div>
  </form>`;
  $('content').innerHTML = html;
  if (main) main.scrollTop = scrollTop;
}

// Whether the research form on screen differs from what the server holds. The
// form is the only place its edits live until Save, so leaving the workspace
// with it dirty is leaving a draft behind.
function researchFormDirty() {
  if ($('research-form')) return differs(readResearchForm(), V.researchBase || storedResearch());
  return Boolean(V.researchDraft);
}

function readResearchForm() {
  const form = $('research-form');
  const claims = [...form.querySelectorAll('[data-ledger-row]')].map((row) => ({
    id: row.querySelector('[name="ledger-id"]').value.trim(),
    text: row.querySelector('[name="ledger-text"]').value.trim(),
  })).filter((c) => c.id || c.text);
  return { context: form.querySelector('[name="context"]').value, claims };
}

function setResearchSaving(form, busy) {
  if (!form.isConnected) return;   // a finished save has already left the view
  form.classList.toggle('saving', busy);
  form.querySelectorAll('input, textarea, button').forEach((field) => { field.disabled = busy; });
}

async function saveResearch() {
  if (V.researchSaving) return;   // a save is in flight: one ledger/context pair at a time
  const form = $('research-form');
  const { context, claims } = readResearchForm();
  V.error = null;
  V.researchSaving = true;
  setResearchSaving(form, true);
  // Only what was edited is written, judged against what the form was drawn
  // from: a field left alone must not carry the value the form opened with
  // over a change made elsewhere while it was open.
  const base = V.researchBase || storedResearch();
  const ledgerChanged = JSON.stringify(claims) !== JSON.stringify(base.claims);
  const contextChanged = context.trim() !== base.context.trim();
  try {
    // The ledger goes first: it is the one of the two the server can refuse
    // (a missing or repeated id), so nothing is written unless both will be.
    if (ledgerChanged) {
      await api('/api/ledger', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ claims }),
      });
    }
    if (contextChanged) {
      await api('/api/context', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: context }),
      });
    }
  } catch (error) {
    // Show the error over the form as typed; redrawing from S would put the
    // saved values back and lose the edit that just failed.
    V.error = `Could not save: ${error.message}`;
    let warn = form.previousElementSibling;
    if (!warn || !warn.classList.contains('warn')) {
      form.insertAdjacentHTML('beforebegin', '<p class="warn"></p>');
      warn = form.previousElementSibling;
    }
    warn.textContent = V.error;
    return;
  } finally {
    V.researchSaving = false;
    setResearchSaving(form, false);
  }
  showView('claims');       // captures the form on the way out, so clear after
  V.researchDraft = null;
  V.researchBase = null;
  syncHash(true);           // as with Cancel: leaving the form is a move
  await refreshAll();
}

$('panes').addEventListener('submit', async (event) => {
  if (event.target.id !== 'research-form') return;
  event.preventDefault();
  await saveResearch();
});

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

function editForm(row, tagListId = 'taglist') {
  const options = (values, current) => values.map((v) =>
    `<option value="${esc(v)}" ${v === current ? 'selected' : ''}>${esc(v)}</option>`).join('');
  const linkRow = (link, i) => `<div class="linkrow" data-link="${i}">
      <select name="link-claim">
        <option value="">(no link)</option>
        ${S.ledger.map((c) => `<option value="${esc(c.id)}" ${c.id === link.claim ? 'selected' : ''}>
          ${esc(c.id)} — ${esc((c.text || '').slice(0, 70))}</option>`).join('')}
      </select>
      <select name="link-relation">${options(S.relations, link.relation)}</select>
      <input name="link-note" value="${esc(link.note || '')}" placeholder="how it bears on my claim">
      <button type="button" data-act="drop-link">×</button>
    </div>`;
  const links = (row.ledger_links || []).concat([{ claim: '', relation: S.relations[0], note: '' }]);
  return `<div class="claim edit-wrap" data-claim="${esc(row.id)}" data-paper="${esc(row.paper)}">
    <form class="edit" data-form="${esc(row.id)}">
      <div><label>Claim</label><textarea name="text" rows="3">${esc(row.text)}</textarea></div>
      <div class="pair">
        <div><label>Kind</label><select name="kind">${options(S.kinds, row.kind)}</select></div>
        <div><label>Strength</label><select name="strength">${options(S.strengths, row.strength)}</select></div>
        <div><label>Locator</label><input name="locator" value="${esc(row.locator || '')}" size="12"></div>
        <div><label>Topics (space separated)</label>
          <input name="tags" value="${esc((row.tags || []).join(' '))}" list="${esc(tagListId)}" size="34"></div>
      </div>
      <div><label>Evidence</label><textarea name="evidence" rows="2">${esc(row.evidence || '')}</textarea></div>
      <details class="evidence-assessment" ${row.conditions || row.limitations || row.evidence_origin || (row.evidence_relationship && row.evidence_relationship !== 'unassessed') ? 'open' : ''}><summary>Your evidence assessment</summary>
        <p class="hint">Your assessment is kept locally and preserved when the paper is re-read.</p>
        <label>Conditions (models, sample sizes, metrics)<textarea name="conditions" rows="2">${esc(row.conditions || '')}</textarea></label>
        <label>Limitations<textarea name="limitations" rows="2">${esc(row.limitations || '')}</textarea></label>
        <label>Evidence origin (dataset or experimental run)<input name="evidence_origin" maxlength="300" value="${esc(row.evidence_origin || '')}"></label>
        <label>Relationship to earlier evidence<select name="evidence_relationship">${Object.entries(EVIDENCE_RELATIONSHIPS).map(([key,label]) => `<option value="${key}" ${(row.evidence_relationship || 'unassessed') === key ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
      </details>
      <div><label>Quote (verbatim from the paper)</label>
        <textarea name="quote" rows="2">${esc(row.quote || '')}</textarea></div>
      <div><label>Bearing on my own claims</label>
        <div class="links">${links.map(linkRow).join('')}</div></div>
      <div><label>My note — what you make of it. No model reads it.</label>
        <textarea name="note" rows="2">${esc(row.note || '')}</textarea></div>
      <div class="row right">
        <label class="hint"><input type="checkbox" name="reviewed" ${row.reviewed ? 'checked' : ''}> reviewed</label>
        <button type="button" data-act="cancel">Cancel</button>
        <button type="submit" class="primary">Save</button>
      </div>
    </form>
    <datalist id="${esc(tagListId)}">${S.tags.map((t) => `<option value="${esc(t.name)}">`).join('')}</datalist>
  </div>`;
}

// --- syntheses: what the papers hold on a topic ---------------------------

function synthesisFor(tag) {
  return (S.syntheses || []).find((s) => s.topic === tag) || null;
}

// Turns `[claim-id]` and `[id, id]` in a synthesis into author-year markers
// that jump to the claim. A bracket holding anything else is left as written.
// The text is escaped first; ids and brackets survive escaping unchanged.
function citeHtml(text) {
  const byId = new Map(S.claims.map((c) => [c.id, c]));
  return esc(text).replace(/\[([^\[\]]+)\]/g, (whole, inner) => {
    const ids = inner.trim().split(/[,\s]+/).filter(Boolean);
    if (!ids.length || !ids.every((id) => byId.has(id))) return whole;
    return '[' + ids.map((id) => {
      const row = byId.get(id);
      const who = (row.paper_authors || [])[0] ? row.paper_authors[0].split(' ').pop() : row.paper;
      return `<span class="cite" data-act="goto-claim" data-claim="${esc(id)}" title="${esc(row.text)}">${esc(who)} ${esc(row.paper_year || 'n.d.')}</span>`;
    }).join(', ') + ']';
  });
}

function synthesisBlock(tag) {
  const synth = synthesisFor(tag);
  if (V.synthEditing === tag) {
    const draft = V.synthDrafts[tag];
    const text = draft !== undefined ? draft : (synth ? synth.text : '');
    // Drawn from state rather than toggled on the elements, so a redraw during
    // the request keeps the editor frozen.
    const busy = V.synthSaving === tag ? ' disabled' : '';
    return `<div class="synth${busy ? ' saving' : ''}" data-topic="${esc(tag)}">
      <textarea data-synth="${esc(tag)}"${busy}>${esc(text)}</textarea>
      <div class="smeta">
        <span class="hint">Cite claims as [claim-id]; they become links.</span>
        <span class="cact" style="margin-left:auto">
          <button type="button" data-act="save-synth" data-topic="${esc(tag)}" class="primary"${busy}>Save</button>
          <button type="button" data-act="cancel-synth" data-topic="${esc(tag)}"${busy}>Cancel</button>
        </span>
      </div>
    </div>`;
  }
  if (!synth) return '';
  const paragraphs = synth.text.split(/\n\s*\n/).filter((p) => p.trim());
  return `<div class="synth" data-topic="${esc(tag)}">
    ${paragraphs.map((p) => `<p>${citeHtml(p)}</p>`).join('')}
    <div class="smeta">
      <span>${synth.source === 'hand' ? 'written by hand' : 'written by the model'} · ${esc((synth.written || '').slice(0, 10))}
        · ${synth.n_claims} claims in ${synth.n_papers} papers</span>
      ${synth.stale ? '<span class="stale">claims or tensions have changed since</span>' : ''}
      <span class="cact" style="margin-left:auto">
        <button type="button" data-act="synthesize" data-force="1" data-ai-action ${S.ai_enabled === true ? '' : 'disabled'} data-topic="${esc(tag)}">Rewrite</button>
        <button type="button" data-act="edit-synth" data-topic="${esc(tag)}">edit</button>
        <button type="button" data-act="del-synth" data-topic="${esc(tag)}">delete</button>
      </span>
    </div>
  </div>`;
}

// The button in a topic heading when no synthesis exists yet.
function synthesizeButton(tag) {
  if (synthesisFor(tag) || V.synthEditing === tag) return '';
  return `<button type="button" class="mini" data-act="synthesize" data-ai-action ${S.ai_enabled === true ? '' : 'disabled'} data-topic="${esc(tag)}"
    title="Ask the model what the papers hold on this topic">synthesize</button>`;
}

// `force` is for Rewrite, which is an instruction and not a request: a topic
// nothing has changed in is skipped by the pass, so without it the button
// would report a finished job and leave the text exactly as it was. Writing a
// synthesis that does not exist yet, and the pass over the whole corpus, both
// mean "where it is worth it" and are left alone.
async function synthesize(topics, force = false) {
  V.error = null;
  const workspace = await settleDeletes();
  if (!workspace) return;   // the delete failed; the pass would read the claim
  try {
    const result = await api('/api/syntheses', {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...workspace },
      body: JSON.stringify({ ...(topics ? { topics } : {}), ...(force ? { force: true } : {}) }),
    });
    if (!result.queued) {
      V.error = topics
        ? 'That topic has no claims to synthesize.'
        : 'No topic has claims from two papers yet. Use the synthesize button on a topic to write one anyway.';
    }
  } catch (error) {
    V.error = `Could not start the synthesis: ${error.message}`;
  }
  await refreshShowingError();
  if (V.error) renderContent();
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

// What the query found in the papers themselves, under the claims it found.
// A paper can hold a word no claim of it mentions, and a paper nothing has
// been extracted from yet holds all of them.
function textSearchBlock() {
  const found = V.textSearch;
  if (!found || found.q !== V.q.trim()) return '';
  const head = '<h3>In the PDFs</h3>';
  if (found.loading) return `<div class="pdfhits">${head}<p class="hint">Reading the papers…</p></div>`;
  if (found.error) {
    return `<div class="pdfhits">${head}<p class="hint">Could not search the papers: ${esc(found.error)}</p></div>`;
  }
  if (!found.papers.length) {
    return `<div class="pdfhits">${head}<p class="hint">No paper's text holds every word of that.</p></div>`;
  }
  // Drawn against the corpus as it stands, not as it stood when the answer
  // came back: a paper removed since would otherwise stay listed here and
  // clicking it would select a paper that is not there.
  const live = new Set(S.papers.map((paper) => paper.key));
  const papers = found.papers.filter((hit) => live.has(hit.key));
  if (!papers.length) {
    return `<div class="pdfhits">${head}<p class="hint">No paper's text holds every word of that.</p></div>`;
  }
  const hits = papers.map((hit) => {
    const cite = `${(hit.authors || [])[0] ? hit.authors[0].split(' ').pop() : hit.key} ${hit.year || ''}`;
    const passages = (hit.passages || []).map((passage) =>
      `<p class="pp"><span class="hint">p. ${passage.page}</span> …${mark(passage)}…</p>`).join('');
    return `<div class="pdfhit">
      <div class="ph">
        <button type="button" class="pt" data-act="open-paper" data-paper="${esc(hit.key)}"
          title="${esc(hit.title || hit.key)}">${esc(cite)}</button>
        <span class="hint">${esc(hit.title || '')}</span>
        <span class="hint" style="margin-left:auto">${hit.occurrences} ${hit.occurrences === 1 ? 'mention' : 'mentions'}</span>
      </div>${passages}</div>`;
  }).join('');
  return `<div class="pdfhits">${head}${hits}</div>`;
}

// A passage as the server cut it: it is the one that knows how the text was
// folded to find the terms, and a browser's own case-insensitive matching
// cannot expand ß to ss, so it would find nothing to mark in a passage that
// was found for exactly that reason. Pieces rather than offsets, since an
// offset into a Python string is not an offset into a JavaScript one.
function mark(passage) {
  const parts = passage.parts;
  if (!parts || !parts.length) return esc(passage.text || '');
  return parts.map((part) => (part.mark ? `<mark>${esc(part.text)}</mark>` : esc(part.text))).join('');
}

const pendingJobActions = new Set();
const jobTimeouts = new Map();

function jobTimeoutPaused(card) {
  return document.hidden || card.matches(':hover') || card.contains(document.activeElement);
}

function syncJobTimeouts() {
  const workspace = workspaceClient.workspaceId;
  const visible = new Set();
  for (const card of $('jobs').querySelectorAll('[data-auto-dismiss]')) {
    const key = card.dataset.jobKey;
    if (!key.startsWith(`${workspace}:`)) continue;
    visible.add(key);
    if (!jobTimeouts.has(key)) {
      jobTimeouts.set(key, { remaining: 8000, timer: null, expired: false });
    }
    const entry = jobTimeouts.get(key);
    entry.card = card;
    if (jobTimeoutPaused(card)) {
      if (entry.timer !== null) {
        clearTimeout(entry.timer);
        entry.remaining = Math.max(0, entry.remaining - (performance.now() - entry.started));
        entry.timer = null;
      }
    } else if (entry.timer === null && !entry.expired) {
      entry.started = performance.now();
      entry.timer = setTimeout(() => {
        entry.timer = null;
        entry.remaining = 0;
        if (workspace !== workspaceClient.workspaceId || !entry.card.isConnected || !jobTimeouts.has(key)) return;
        if (jobTimeoutPaused(entry.card)) return;
        // Use the same persisted dismissal as the close button. On failure,
        // leave the card available for a manual retry instead of looping.
        entry.expired = true;
        entry.card.querySelector('.dismiss-job').click();
      }, entry.remaining);
    }
    card.onmouseenter = card.onmouseleave = syncJobTimeouts;
  }
  // Hidden cards get their full reading time when they next become visible,
  // including after a workspace switch or a backlog of imports clears.
  for (const [key, entry] of jobTimeouts) {
    if (visible.has(key)) continue;
    clearTimeout(entry.timer);
    jobTimeouts.delete(key);
  }
}

for (const type of ['focusin', 'focusout']) {
  $('jobs').addEventListener(type, () => queueMicrotask(syncJobTimeouts));
}
document.addEventListener('visibilitychange', syncJobTimeouts);

function openPaper(paper) {
  captureOpenEditor();
  closeEditorsNotBelongingTo(paper);
  showView('claims');
  // Papers opened from another view may be outside the current label filter.
  keepLabelFor(paper);
  V.paper = paper; V.tag = null; V.selectedId = null;
  syncHash(true);
  renderAll();
}

function renderJobs() {
  const expanded = new Set([...$('jobs').querySelectorAll('.job-details[open]')]
    .map((details) => details.dataset.jobId));
  const active = (S.jobs || []).filter((j) => j.state !== 'done' || j.detail || j.kind === 'import');
  // Successful imports remain available to open without crowding out failures.
  active.sort((a, b) => Number(a.state === 'done') - Number(b.state === 'done'));
  const importStates = { queued: 'Waiting to import…', fetching: 'Importing…', reading: 'Analyzing…',
                         done: 'Imported', error: 'Import failed' };
  const next = document.createElement('div');
  next.innerHTML = (S.job_recovery_issues || []).map(issue => `<div class="job error"><span class="lbl">Previous imports</span><p class="job-explanation">${esc(issue)}</p></div>`).join('') + active.slice(0, 6).map((j) => {
    const disabled = pendingJobActions.has(`${workspaceClient.workspaceId}:${j.id}`) ? 'disabled' : '';
    const explained = Boolean(j.explanation);
    const label = S.papers.find((paper) => paper.key === j.key)?.title || j.label;
    const status = j.kind === 'import' ? (importStates[j.state] || j.state) : j.state;
    return `
    <div class="job ${j.state === 'error' ? 'error' : ''}" data-job-key="${esc(workspaceClient.workspaceId)}:${j.id}"
      ${j.state === 'done' && j.kind === 'import' ? 'data-auto-dismiss' : ''}>
      <span class="lbl" title="${esc(label)}">${esc(label)}</span>
      <span class="st">${esc(status)}${j.detail && !explained ? ': ' + esc(j.detail) : ''}</span>
      ${explained ? `<p class="job-explanation">${esc(j.explanation)}</p>` : ''}
      ${explained && j.state === 'error' ? `
        <details class="job-details" data-job-id="${j.id}" ${expanded.has(String(j.id)) ? 'open' : ''}>
          <summary>Technical details</summary>${esc(j.detail)}</details>` : ''}
      ${j.state === 'error' && j.retryable ? `<div class="job-actions">
        <button type="button" class="retry-job" data-job-id="${j.id}" ${disabled}>${j.interrupted ? 'Resume import' : 'Retry'}</button></div>` : ''}
      ${j.state === 'done' && j.kind === 'import' && j.key ? `<div class="job-actions">
        <button type="button" class="open-job" data-job-id="${j.id}" data-paper="${esc(j.key)}" ${disabled}>Open paper</button></div>` : ''}
      ${['done', 'error'].includes(j.state) ? `<button type="button" class="dismiss-job" data-job-id="${j.id}"
        aria-label="Dismiss notification for ${esc(label)}" title="Dismiss notification" ${disabled}>×</button>` : ''}
    </div>`;
  }).join('');
  // Keep unchanged cards in place so polling and other jobs completing do not
  // interrupt keyboard focus or the hover that pauses a successful import.
  const container = $('jobs');
  const existing = new Map([...container.children].filter(card => card.dataset.jobKey)
    .map(card => [card.dataset.jobKey, card]));
  const cards = [...next.children].map(card => {
    const old = existing.get(card.dataset.jobKey);
    return old && old.outerHTML === card.outerHTML ? old : card;
  });
  for (const card of [...container.children]) {
    if (!cards.includes(card)) card.remove();
  }
  cards.forEach((card, index) => {
    if (container.children[index] !== card) container.insertBefore(card, container.children[index] || null);
  });
  syncJobTimeouts();
}

$('jobs').addEventListener('click', async (event) => {
  const button = event.target.closest('.dismiss-job, .retry-job, .open-job');
  if (!button || button.disabled) return;
  const workspace = workspaceClient.workspaceId;
  const actionKey = `${workspace}:${button.dataset.jobId}`;
  // Selection changes before the destination read retires the previous DOM.
  if (button.closest('.job').dataset.jobKey !== actionKey) return;
  if (pendingJobActions.has(actionKey)) return;
  pendingJobActions.add(actionKey);
  button.disabled = true;
  const retry = button.classList.contains('retry-job');
  const open = button.classList.contains('open-job');
  try {
    if (open) {
      if (S.papers.some((paper) => paper.key === button.dataset.paper)) openPaper(button.dataset.paper);
      else toast('This paper is no longer in this workspace.', { tone: 'warn' });
    }
    await api(`/api/jobs/${button.dataset.jobId}${retry ? '/retry' : ''}`, {
      method: retry ? 'POST' : 'DELETE', headers: { 'X-Doxograph-Workspace': workspace },
    });
    await refresh();
  } catch (error) {
    toast(`Could not ${retry ? 'retry import' : 'dismiss notification'}: ${error.message}`, { tone: 'warn' });
  } finally {
    pendingJobActions.delete(actionKey);
    if (workspace === workspaceClient.workspaceId) renderJobs();
  }
});

// --- actions --------------------------------------------------------------

function cancelEdit() {
  if (V.editing) delete V.drafts[V.editing];   // discard only this claim's draft
  // Only the new-claim editor's own Cancel drops the held new claim. Cancelling
  // an existing claim while one is held must leave it reachable via Resume.
  if (V.editing === NEW_CLAIM_ID) V.newClaim = null;
  V.editing = null;
  V.error = null;
  renderContent();
}

// The rule: every handler that changes what is displayed calls this before
// redrawing. Field edits live only in the DOM until submit, so any rebuild that
// does not read them back first discards them. The exceptions are deliberate —
// `cancelEdit` and a successful save drop the draft on purpose.
function closeEditorsNotBelongingTo(paper) {
  // An editor for a claim on another paper would keep `V.editing` set while
  // being invisible, which suppresses redraws; a new claim would render under
  // the wrong header and save to the paper it was started on.
  if (V.editing && V.editing !== NEW_CLAIM_ID) {
    const row = S.claims.find((c) => c.id === V.editing);
    if (row && row.paper !== paper) V.editing = null;
  }
  parkNewClaimForNavigation(paper);
}

function parkNewClaimForNavigation(paper, { retainUnsaved = false } = {}) {
  // Navigation normally abandons an ordinary new claim. Clearing paper scope
  // keeps it, like a failed submission: park it under its original paper so
  // it can be restored without offering a save to a hidden destination.
  if (V.newClaim && V.newClaim.paper !== paper) {
    if ((retainUnsaved && newClaimHasEdits(V.newClaim)) || V.newClaim.saveFailed) {
      V.parkedNewClaims[V.newClaim.paper] = V.newClaim;
    }
    V.newClaim = null;
    if (V.editing === NEW_CLAIM_ID) V.editing = null;
  }
  if (!V.newClaim && paper && V.parkedNewClaims[paper]) {
    V.newClaim = V.parkedNewClaims[paper];
    delete V.parkedNewClaims[paper];
  }
}

// The topics whose heading, and so whose synthesis, `renderContent` is about
// to draw: every tag in grouped mode, the one filtered to otherwise, none
// while reading a paper.
function synthesisTopicsOnScreen(rows) {
  if (V.paper) return new Set();
  if (V.group && rows.length) return new Set(rows.flatMap((row) => row.tags || []));
  return new Set(V.tag ? [V.tag] : []);
}

// The synthesis editor's counterpart to parking a claim editor: keep what was
// typed, keyed by topic, and close the editor. Left open off screen it would
// hold down the background poll, and opening another topic's editor would
// take over the slot; opening this topic's editor again resumes the text. The
// input listener keeps the map current, but the field is read once more here
// so nothing rests on that.
function parkSynthEditor() {
  if (!V.synthEditing) return;
  const field = document.querySelector(`textarea[data-synth="${CSS.escape(V.synthEditing)}"]`);
  if (field) V.synthDrafts[V.synthEditing] = field.value;
  V.synthEditing = null;
}

function cancelSynthEdit() {
  captureOpenEditor();   // a claim editor open alongside keeps its text through the redraw
  if (V.synthEditing) delete V.synthDrafts[V.synthEditing];   // discard only this topic's draft
  V.synthEditing = null;
  renderContent();
}

function captureOpenEditor() {
  // Keyed off `V.editing` rather than whatever form is in the DOM. The DOM lags
  // the state — `renderAll` captures before redrawing, so a form for an editor
  // that was just closed is still present — and capturing from it resurrected a
  // cleared new-claim draft without its `paper`, which then saved to the wrong
  // paper or rendered under the wrong header.
  if (!V.editing) return;
  const form = document.querySelector(`form[data-form="${V.editing}"]`);
  if (!form) return;
  const patch = readForm(form);
  if (V.editing === NEW_CLAIM_ID) {
    if (!V.newClaim) return;   // never recreate a draft that was discarded
    V.newClaim = { ...V.newClaim, ...patch };
  } else {
    V.drafts[V.editing] = { ...V.drafts[V.editing], ...patch };
  }
}

// Answers whether the review was actually taken, which is what the `r` key
// needs: moving to the next claim after a toggle that did not happen would
// leave this one unreviewed and nothing on screen to say so.
async function toggleReviewed(row) {
  // Shared by the review button — including the duplicate cards a claim gets in
  // grouped mode — and the `r` key, so both keep an open editor in step.
  if (isSaving(row.id)) return false;   // a request for it is in flight
  const reviewed = !row.reviewed;
  if (V.editing === row.id) captureOpenEditor();
  // Freeze the claim's form for the toggle too. A full-form save started
  // during it carries the old checkbox and would land afterwards, putting the
  // review flag back where it was.
  markSaving(row.id, true);
  try {
    await patchClaim(row.paper, row.id, { reviewed });
  } catch (error) {
    toast(`Could not ${reviewed ? 'mark the claim reviewed' : 'take the review back'}: ${error.message}`,
          { tone: 'warn' });
    return false;
  } finally {
    markSaving(row.id, false);
  }

  // The request is long enough for the user to type in the form, or to open a
  // different claim's editor. Recapturing the DOM only makes sense in the first
  // case, but this claim's stored draft has to be corrected either way — it
  // holds the pre-toggle value, and saving it later would undo the toggle.
  const stillOpen = V.editing === row.id;
  if (stillOpen) captureOpenEditor();
  if (V.drafts[row.id]) V.drafts[row.id] = { ...V.drafts[row.id], reviewed };
  if (stillOpen) renderContent();
  return true;
}

// Reviewing is the app's main work and a paper arrives with a dozen claims at
// once, so it is worth one request and one undo rather than a dozen clicks.
// The undo unreviews only what this actually changed: a claim reviewed
// earlier is not part of the decision being taken back.
async function reviewWholePaper(paper) {
  captureOpenEditor();
  V.error = null;
  // A claim already saving has a full-form PATCH on its way carrying the old
  // checkbox. Freezing it here only stops a second submission; it cannot stop
  // the first from landing after this write and taking the review back off
  // again. So wait for it, rather than overwrite it and report success.
  if (S.claims.some((row) => row.paper === paper && isSaving(row.id))) {
    toast('Wait for the change in flight to finish, then mark the paper reviewed.',
          { tone: 'warn' });
    return;
  }
  // The notice outlives the picker, so the undo carries the workspace the
  // decision was taken in rather than whichever one is selected when it is
  // clicked: the same paper imported twice has the same claim ids in both.
  const workspace = workspaceClient.workspaceId;
  // An open editor on this paper is frozen for the length of the request, as
  // one is during a single toggle: a full-form Save started meanwhile carries
  // the old checkbox, and landing after the bulk write would put it back.
  const frozen = S.claims.filter((row) => row.paper === paper).map((row) => row.id);
  frozen.forEach((id) => markSaving(id, true));
  // The claims it names, not "all of them". A claim waiting out a delete is
  // gone from the page and from the count on the button, but still on file:
  // reviewing it would be a decision about something nobody can see, and
  // undoing the delete would bring it back reviewed.
  const wanted = S.claims.filter((row) => row.paper === paper && !row.reviewed).map((row) => row.id);
  let changed = [];
  try {
    const result = await api(`/api/papers/${encodeURIComponent(paper)}/review`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reviewed: true, claims: wanted }),
    });
    changed = result.changed || [];
  } catch (error) {
    V.error = `Could not mark the claims reviewed: ${error.message}`;
    renderContent();
    return;
  } finally {
    frozen.forEach((id) => markSaving(id, false));
  }
  syncDraftReviews(changed, true);
  await refreshAll();
  if (!changed.length) return;

  // A save already on its way carries the old checkbox and cannot be recalled,
  // so the undo waits for it rather than being overtaken by it — and the offer
  // comes back rather than being spent, since the reader did ask to undo.
  async function undo() {
    const mine = workspaceClient.workspaceId === workspace;
    if (mine && changed.some((id) => isSaving(id))) {
      toast('Wait for the change in flight to finish, then undo.', { tone: 'warn' });
      offerUndo();
      return;
    }
    // Frozen for the length of the undo as they were for the review itself: an
    // open form still holds the ticked box, and a Save landing after the undo
    // would put the review back with nothing left on screen to say it had.
    const held = mine ? changed : [];
    held.forEach((id) => markSaving(id, true));
    try {
      await api(`/api/papers/${encodeURIComponent(paper)}/review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Doxograph-Workspace': workspace },
        body: JSON.stringify({ reviewed: false, claims: changed }),
      });
      // Only where the decision was taken. An editor open on the same claim id
      // in another corpus is a different claim, and correcting its checkbox
      // would unreview it when that form is saved.
      if (workspaceClient.workspaceId === workspace) syncDraftReviews(changed, false);
    } catch (error) {
      toast(`Could not undo: ${error.message}`, { tone: 'warn' });
    } finally {
      held.forEach((id) => markSaving(id, false));
    }
    await refreshAll();
  }

  function offerUndo() {
    toast(`${changed.length} ${changed.length === 1 ? 'claim' : 'claims'} marked reviewed.`,
          { actions: [{ label: 'Undo', onClick: undo }] });
  }

  offerUndo();
}

// An open editor holds the review flag as it was when the editor was opened.
// Saving it afterwards would put the flag back, undoing the bulk decision
// without anyone asking for that, so the flag moves with it.
//
// The form on screen is corrected as well as the stored draft: every redraw
// reads the open form back first, so a draft corrected on its own is
// overwritten from the stale checkbox before it is ever used.
function syncDraftReviews(ids, reviewed) {
  ids.forEach((id) => {
    if (V.drafts[id]) V.drafts[id] = { ...V.drafts[id], reviewed };
    const box = document.querySelector(`form[data-form="${CSS.escape(id)}"] [name="reviewed"]`);
    if (box) box.checked = reviewed;
  });
}

async function showSimilar(paper, claim, group = '') {
  // Matched on the request itself, as the passage is: a claim id is unique
  // per corpus and not across workspaces, and switching is not held back by a
  // read, so the answer to one workspace's question could land in another.
  const pending = { claim, paper, group, loading: true, error: null, rows: [] };
  captureOpenEditor();
  V.similar = pending;
  renderContent();
  let next;
  try {
    const found = await api(
      `/api/papers/${encodeURIComponent(paper)}/claims/${encodeURIComponent(claim)}/similar`);
    next = { claim, paper, group, loading: false, error: null, rows: found.similar || [] };
  } catch (error) {
    next = { claim, paper, group, loading: false, error: `Could not compare the claims: ${error.message}`, rows: [] };
  }
  if (V.similar !== pending) return;   // closed, or another opened, while it ran
  V.similar = next;
  captureOpenEditor();
  renderContent();
}

async function showQuoteContext(paper, claim, group = '') {
  // The pending request itself is what a late answer has to match, not the
  // claim id: claim ids are unique per corpus and not across workspaces, so
  // opening the same id in another workspace before the first read finishes
  // would otherwise be answered with the other corpus's passage — and "use
  // the paper's wording" would write it into this one.
  const pending = { claim, paper, group, loading: true, error: null, data: null };
  // Another claim's editor can be open on the same screen, holding text that
  // exists only in the DOM. Every redraw has to read it first or the passage
  // opening throws away what somebody was typing.
  captureOpenEditor();
  V.quoteContext = pending;
  renderContent();
  try {
    const data = await api(
      `/api/papers/${encodeURIComponent(paper)}/claims/${encodeURIComponent(claim)}/quote-context`);
    if (V.quoteContext === pending) {
      V.quoteContext = { claim, paper, group, loading: false, error: null, data };
    }
  } catch (error) {
    if (V.quoteContext === pending) {
      V.quoteContext = { claim, paper, group, loading: false, error: `Could not read the PDF: ${error.message}`, data: null };
    }
  }
  captureOpenEditor();   // an editor may have been opened while the PDF was read
  renderContent();
}

// Take the paper's wording for a quote. The claim's editor is never open while
// the passage is on screen — the card is replaced by the form — but a draft
// from an earlier edit can be, and it holds the old quote: saving it later
// would put the model's version back.
async function usePaperWording(paper, claim) {
  const found = V.quoteContext && V.quoteContext.data;
  const wording = found && found.suggestion;
  if (!wording || isSaving(claim)) return;
  // The passage on screen can be older than the claim: while an editor is
  // open the poll leaves the content alone, so a quote edited elsewhere
  // refreshes `S` under a button still offering the old suggestion. Checked
  // again here, against the row as it stands, rather than trusted because it
  // is drawn.
  const row = S.claims.find((c) => c.id === claim);
  if (!row || found.quote !== row.quote || (found.locator || '') !== (row.locator || '')) {
    V.quoteContext = null;
    V.error = 'This claim changed while the passage was open; nothing was written.';
    captureOpenEditor();
    renderContent();
    return;
  }
  V.error = null;
  markSaving(claim, true);
  try {
    await patchClaim(paper, claim, { quote: wording });
    if (V.drafts[claim]) V.drafts[claim] = { ...V.drafts[claim], quote: wording };
    V.quoteContext = null;
  } catch (error) {
    V.error = `Could not replace the quote: ${error.message}`;
  } finally {
    markSaving(claim, false);
  }
  captureOpenEditor();   // another claim's editor may be open and unsaved
  renderContent();
}

async function patchClaim(paper, claim, patch) {
  // An open passage was worked out from the quote and locator as they were.
  // Saving either makes it describe a claim that no longer exists, and "use
  // the paper's wording" would then write the old suggestion over the new
  // quote. Closed rather than refetched: the reviewer just decided what the
  // quote should say, and reopening it under them would be a surprise.
  if (('quote' in patch || 'locator' in patch) && V.quoteContext && V.quoteContext.claim === claim) {
    V.quoteContext = null;
  }
  await api(`/api/papers/${encodeURIComponent(paper)}/claims/${encodeURIComponent(claim)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  // The write has landed by here. A failure only to read it back must not reach
  // the callers, who would report a save that happened as refused — and the `r`
  // key would stay on a claim the server has marked reviewed. The poll reads
  // the corpus again within a few seconds. The redraw the refresh would have
  // made still happens: a saved claim's form is only taken off the screen by
  // it, and left there it would take typing that nothing keeps.
  try {
    await refresh();
  } catch (error) {
    render();
  }
}

function readForm(form) {
  const value = (name) => (form.querySelector(`[name="${name}"]`) || {}).value || '';
  const links = [...form.querySelectorAll('.linkrow')].map((rowEl) => ({
    claim: rowEl.querySelector('[name="link-claim"]').value,
    relation: rowEl.querySelector('[name="link-relation"]').value,
    note: rowEl.querySelector('[name="link-note"]').value.trim(),
  })).filter((l) => l.claim);
  return {
    text: value('text').trim(),
    kind: value('kind'),
    strength: value('strength'),
    locator: value('locator').trim(),
    evidence: value('evidence').trim(),
    quote: value('quote').trim(),
    conditions: value('conditions').trim(), limitations: value('limitations').trim(),
    evidence_origin: value('evidence_origin').trim(), evidence_relationship: value('evidence_relationship') || 'unassessed',
    tags: value('tags').split(/[\s,]+/).map((t) => t.trim().toLowerCase()).filter(Boolean),
    ledger_links: links,
    // `[name="note"]` is the claim's own; a link's is `link-note`, read above.
    note: value('note').trim(),
    reviewed: form.querySelector('[name="reviewed"]').checked,
  };
}

$('panes').addEventListener('submit', async (event) => {
  const form = event.target.closest('form[data-form]');
  if (!form) return;
  event.preventDefault();
  const wrap = form.closest('[data-claim]');
  if (isSaving(wrap.dataset.claim)) return;   // a request for it is in flight
  // Which paper the save would land on: the one the card names, or for a claim
  // being written by hand the one the draft was started on. A removal in
  // flight for it takes the save, whichever way the race goes — the POST
  // landing first makes a claim the DELETE behind it discards, and landing
  // second it is a 404 over a claim the reader was told was saved.
  const onPaper = wrap.dataset.claim === NEW_CLAIM_ID
    ? (V.newClaim || {}).paper : wrap.dataset.paper;
  if (refuseWhileRemoving(onPaper, 'what is typed here')) return;
  const patch = readForm(form);
  V.error = null;
  markSaving(wrap.dataset.claim, true);
  try {
    await saveClaim(wrap, patch);
  } finally {
    markSaving(wrap.dataset.claim, false);
  }
});

async function saveClaim(wrap, patch) {
  if (wrap.dataset.claim === NEW_CLAIM_ID) {
    const paper = V.newClaim.paper;
    // Keep what was typed on the draft, so a failed save can be retried
    // instead of losing the text.
    V.newClaim = { ...V.newClaim, ...patch };
    if (!patch.text) { V.editing = null; V.newClaim = null; renderContent(); return; }
    try {
      await api(`/api/papers/${encodeURIComponent(paper)}/claims`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
      });
    } catch (error) {
      // As on the success path: an existing claim's editor may have been
      // opened while the POST ran, and its text is only in the DOM.
      captureOpenEditor();
      // Moving to another paper abandons an unsaved new claim, so the draft
      // can be gone by now. The user did click Save on this text, so put it
      // back rather than losing it to a failure they did not choose.
      const failedDraft = { ...blankClaim(paper), ...patch, saveFailed: true };
      if (V.paper === paper &&
          (!V.newClaim || V.newClaim.paper === paper)) {
        V.newClaim = failedDraft;
      } else {
        V.parkedNewClaims[paper] = failedDraft;
      }
      V.error = V.paper === paper
        ? `Could not save the claim: ${error.message}`
        : `Could not save a new claim in ${paper}: ${error.message}. `
          + 'What you typed is kept — open that paper to retry.';
      renderContent();          // the draft is still held, ready to retry
      return;
    }
    // The user may have opened an existing claim's editor while the POST was
    // running. Read it before the redraw, and leave it open — only the new
    // claim's own editor is finished with.
    captureOpenEditor();
    if (V.editing === NEW_CLAIM_ID) V.editing = null;
    V.newClaim = null;
    delete V.parkedNewClaims[paper];
    await refreshAll();          // a row appeared, so the list has to be rebuilt
    return;
  }

  V.editing = null;
  try {
    await patchClaim(wrap.dataset.paper, wrap.dataset.claim, patch);
    delete V.drafts[wrap.dataset.claim];
    // `render` leaves the content alone while another editor is open — a
    // synthesis, a note — so the saved claim's form would stay on screen with
    // nothing tracking it, and text typed into it afterwards would be thrown
    // away by the next redraw. Their drafts are kept current by the input
    // listener, so redrawing here loses nothing; a claim editor opened while
    // the request ran has already redrawn the form away, so leave it be.
    // In the claims view alone: that is where the saved form is, and
    // `editorHolds` is true for the research form too, which has its own
    // save to finish and must not be rebuilt — enabled — from under it.
    if (V.view === 'claims' && editorHolds() && !V.editing) renderContent();
  } catch (error) {
    // Only this form was frozen during the request, so the open editor may now
    // belong to a different claim, holding text that exists only in the DOM.
    // Read it before reopening this one redraws it away.
    captureOpenEditor();
    // Keep what was typed so the save can be retried; the server row is stale.
    const id = wrap.dataset.claim;
    V.drafts[id] = { ...V.drafts[id], ...patch };
    // Reopen this claim's editor only where it can actually be seen. The user
    // may have moved to another paper or filter while the request ran, and an
    // editor the list cannot render is an editor nobody can reach — it would
    // also stop the background poll, which stands down whenever one is open.
    const onScreen = V.view === 'claims' && visibleClaims().some((row) => row.id === id);
    if (onScreen && (!V.editing || V.editing === id)) V.editing = id;
    V.error = onScreen
      ? `Could not save the claim: ${error.message}`
      : `Could not save a claim in ${wrap.dataset.paper}: ${error.message}. `
        + 'What you typed is kept — open that paper to retry.';
    renderContent();
  }
}

// Shared by the Remove button in the paper header and the right-click menu in
// the paper list, so the menu can remove a paper that is not the one on screen.
async function removePaper(paper) {
  const p = S.papers.find((x) => x.key === paper);
  // The one delete with no undo: a removed paper's key is retired for good, so
  // adding it again would make a different paper. Hence a question first.
  const draft = ResearchTools.markDraftOn(paper);
  const go = await confirmDialog(`Remove ${p ? p.title || paper : paper} and its claims?`, {
    note: 'The PDF and every claim on it go with it, and the key is never reissued.'
      + (draft ? ' The note you are writing on one of its marks goes too.' : ''),
    ok: 'Remove',
  });
  if (!go) return;
  // The question is a wait like any other, and the header and the paper list
  // stay live across it: a second Remove could have been answered first, and
  // this one would ask the server for a paper that is already going.
  if (refuseWhileRemoving(paper, 'a second removal')) return;
  // A write already on its way cannot be recalled. It carries no claim id this
  // could freeze — a re-read, a quote check, a retag and an accepted topic all
  // write the paper itself — and whichever way it races the DELETE it ends
  // badly: landing first it is thrown away under a page that reported it
  // saved, landing second it is a 404 over a removal the reader did ask for.
  // The guard below stops the next one; this is the one already in flight.
  if (workspaceClient.pendingMutations) {
    toast('Wait for the change in flight to finish, then remove the paper.', { tone: 'warn' });
    return;
  }
  // Nothing else may be written to this paper from here on, and "here" is
  // before the flush below rather than after it: the flush ends in a read of
  // the whole corpus, long enough for a claim to be saved, a re-read started
  // or a topic accepted, and the guard is no use to the DELETE if it goes up
  // only once that window has closed. It is dropped in the `finally` at the
  // end, so it covers the whole attempt and not just the request.
  removingPapers.add(paper);
  // Frozen claim ids, filled in once the flush has settled and the corpus is
  // known. Declared out here so the `finally` can hand them back on the way
  // out of a flush that failed too.
  let frozen = [];
  try {
    // A claim of this paper still waiting out its notice has to go first. Left
    // waiting, its Undo would have nothing to restore and its request would
    // arrive under a paper that no longer exists, failing in the reader's face
    // over a deletion they got what they asked for. Only this paper's, though:
    // a claim of another paper, or a synthesis, was promised its own eight
    // seconds and removing something else is no reason to take them away.
    //
    // That flush ends in a read, and nothing counts a read as a change in
    // flight, so the picker can move in the gap before the paper's own request
    // goes. It names the workspace it was asked in: the same paper imported
    // into two corpora has the same key in both, and removing the wrong one is
    // not something an undo could fix.
    const headers = await settleDeletes((entry) => entry.paper === paper);
    // That claim's delete failed. Removing the paper would take it anyway,
    // under a notice that said the claim could not be deleted — so nothing
    // happens. The `finally` drops the guard, handing the paper back to the
    // reader who still has it on screen.
    if (!headers) return;
    const workspace = headers['X-Doxograph-Workspace'];
    // The paper's cards stay on screen, and clickable, until this request comes
    // back and the redraw takes them away. A claim deleted in that window is
    // held for its own eight seconds under a paper that is already gone: the
    // flush above has finished, so nothing sends it early, its Undo would have
    // nothing to restore, and letting the notice run out sends a DELETE under a
    // paper the server no longer has — a failure notice over a deletion the
    // reader did ask for. They are frozen for the length of the removal, the
    // same freeze a save or a bulk review puts on the claims it is writing.
    //
    // Worked out after the flush rather than before it, because the flush ends
    // in a read and the claims the paper has once that lands are the ones the
    // DELETE is about to take. Nothing can be clicked in between — there is no
    // await between the read and this freeze — and the guard above held the
    // paper for the whole of the flush itself.
    frozen = S.claims.filter((c) => c.paper === paper).map((c) => c.id);
    // A claim being written by hand is not among them: it has no id on the
    // server, only the `__new__` the form is drawn under. Frozen by that name
    // when it belongs to this paper, so the form goes read-only like the rest
    // rather than staying open over a paper that is going. The set below is what
    // actually refuses the save — the draft can be parked and restored by moving
    // between papers, and a form restored after this was worked out would not be
    // covered by it.
    if (V.newClaim && V.newClaim.paper === paper) frozen.push(NEW_CLAIM_ID);
    frozen.forEach((id) => markSaving(id, true));
    try {
      await api(`/api/papers/${encodeURIComponent(paper)}`, { method: 'DELETE', headers });
    } catch (error) {
      // The paper stays, cards and all, so the reader is told rather than left
      // with a request that failed in silence.
      toast(`Could not remove the paper: ${error.message}`, { tone: 'warn' });
      return;
    }
    // Moved on meanwhile: the view belongs to another corpus now, and the drafts
    // and selection this would tidy up went with `resetWorkspaceView`. A note
    // being written there on a mark of the same key is that corpus's own.
    if (workspaceClient.workspaceId !== workspace) {
      await refreshAll();
      return;
    }
    // The note being written on one of its marks goes with the paper, and only
    // now: a removal refused or failed above leaves the paper, and the note.
    ResearchTools.dropMarkDraft(paper);
    // Close an editor that belonged to the deleted paper, so its form is not
    // captured as a draft for a claim that no longer exists. An editor on some
    // other paper's claim stays open, which is why this ends in `refreshAll`:
    // `refresh` would leave the claim list alone while an editor is open and
    // the deleted paper's cards would stay on screen with buttons that 404.
    S.claims.filter((c) => c.paper === paper).forEach((c) => delete V.drafts[c.id]);
    if (V.editing && V.editing !== NEW_CLAIM_ID) {
      const row = S.claims.find((c) => c.id === V.editing);
      if (!row || row.paper === paper) V.editing = null;
    }
    if (V.newClaim && V.newClaim.paper === paper) {
      V.newClaim = null;
      if (V.editing === NEW_CLAIM_ID) V.editing = null;
    }
    delete V.parkedNewClaims[paper];
    // The note draft goes the same way as the claim drafts. The key is retired
    // for good, so a draft kept under it can never be reopened or cancelled —
    // and every entry in `noteDrafts` counts as an unsaved edit, so one left
    // behind would ask about discarding it on every later workspace switch.
    if (V.noteEditing === paper) V.noteEditing = null;
    delete V.noteDrafts[paper];
    // Removing a paper from the list while reading another one keeps that one
    // open; only a paper that was itself on screen falls back to "All papers".
    if (V.paper === paper) V.paper = null;
    const selected = S.claims.find((c) => c.id === V.selectedId);
    if (!selected || selected.paper === paper) V.selectedId = null;
    V.error = null;
    await refreshAll();
  } finally {
    // In a `finally` so a removal that fails — or one called off by a held
    // delete that would not go — does not leave the paper frozen with nothing
    // left to unfreeze it: the cards are still there and the reader has to be
    // able to work on them again.
    frozen.forEach((id) => markSaving(id, false));
    removingPapers.delete(paper);
  }
}

$('btn-reader-library').addEventListener('click', () => {
  setReadingLibrary(!document.body.classList.contains('library-collapsed'));
});
$('btn-reader-claims').addEventListener('click', () => {
  setReadingClaims(!readingClaimsCollapsed);
});
window.matchMedia('(max-width: 800px)').addEventListener('change', event => {
  if (readingPaper()) setReadingLibrary(event.matches);
});
$('btn-reader-filters').addEventListener('click', () => {
  setReadingFilters(!document.body.classList.contains('reading-filters-open'));
});
$('btn-back-library').addEventListener('click', () => {
  // Use the existing navigation path so every editor parks its draft.
  $('papers').querySelector('[data-paper=""]').click();
  $('q').focus({preventScroll: true});
});
document.addEventListener('click', event => {
  const command = event.target.closest('[data-reader-command]');
  if (command) $(command.dataset.readerCommand).click();
  document.querySelectorAll('.paper-actions[open]').forEach(menu => {
    if (!menu.contains(event.target) || event.target.closest('button, a')) {
      menu.open = false;
      if (menu.contains(document.activeElement)) menu.querySelector('summary').focus();
    }
  });
});

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

$('papers').addEventListener('click', (event) => {
  const label = event.target.closest('[data-label]');
  if (label) {
    chooseLabel(label.dataset.label);
    return;
  }
  // The same menu the right-click opens, for the people who never try one.
  const menuButton = event.target.closest('[data-menu]');
  if (menuButton) {
    const box = menuButton.getBoundingClientRect();
    openPaperMenu(menuButton.dataset.menu, box.left, box.bottom + 4, menuButton);
    return;
  }
  const li = event.target.closest('[data-paper]');
  if (!li) return;
  const next = li.dataset.paper || null;
  const view = next ? 'claims' : 'library';
  // Clicking the paper already selected is not navigation. It used to run the
  // whole abandon path anyway, which threw away a new claim being written.
  // From the tensions view it is navigation: back to that paper's claims.
  if (next === V.paper && V.view === view) return;
  if (next === V.paper) { showView(view); syncHash(true); renderAll(); return; }
  // Keep an existing claim's edits, but still abandon a new unsaved claim:
  // that one was never persisted and belongs to the paper being left. A
  // synthesis being edited is parked too: `render` skips the list while one
  // is open, and its topic heading is not shown under a paper anyway.
  captureOpenEditor();
  parkNewClaimForNavigation(next);
  parkSynthEditor();
  // The note editor belongs to the paper being left, and `render` below skips
  // the list while one is open, so it would never be drawn away.
  parkNoteEditor();
  showView(view);
  V.paper = next;
  V.selectedId = null;
  V.editing = null;
  syncHash(true);   // going to a paper is navigation: Back comes back here
  render();   // the editors are closed here, so render redraws the list anyway
});

$('tensions-nav').addEventListener('click', (event) => {
  if (!event.target.closest('[data-view]')) return;
  showView('tensions');
  syncHash(true);
  renderAll();
});

$('agreements-nav').addEventListener('click', (event) => {
  if (!event.target.closest('[data-view]')) return;
  showView('agreements');
  syncHash(true);
  renderAll();
});

$('btn-agreements').addEventListener('click', async () => {
  showView('agreements');
  syncHash(true);
  renderAll();
  await findAgreements();
});

$('research-nav').addEventListener('click', (event) => {
  if (!event.target.closest('[data-view]')) return;
  // Clicking the item already shown is not navigation. Redrawing here would
  // rebuild the form from `S` and throw away whatever has been typed into it.
  if (V.view === 'research') return;
  showView('research');
  syncHash(true);
  renderAll();
});

async function findTensions() {
  V.error = null;
  const workspace = await settleDeletes();
  if (!workspace) return;   // the delete failed; the pass would read the claim
  try {
    const result = await api('/api/tensions', {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...workspace }, body: '{}',
    });
    if (!result.queued) {
      V.error = 'No topic has claims from two papers yet, so there is nothing to compare.';
    }
  } catch (error) {
    V.error = `Could not start the pass: ${error.message}`;
  }
  await refreshShowingError();
}

$('btn-tensions').addEventListener('click', async () => {
  showView('tensions');
  syncHash(true);
  renderAll();
  await findTensions();
});

$('btn-synth').addEventListener('click', async () => {
  showView('claims');
  V.group = true;
  $('group-by-tag').checked = true;
  syncHash(true);
  renderAll();
  await synthesize(null);
});

// --- settings ------------------------------------------------------------

let savingAnalysisSetting = false;
let savingProviderSetting = false;
let savingCodexModel = false;
let loadingCodexModels = false;
let codexModelsLoaded = false;
let codexModels = [];
let codexModelEdited = false;
let readOnArrival = $('auto-extract').checked;
$('auto-extract').addEventListener('change', () => { readOnArrival = $('auto-extract').checked; });

function syncAnalysisControls() {
  const enabled = S.ai_enabled === true;
  const toggle = $('ai-enabled');
  toggle.disabled = savingAnalysisSetting || typeof S.ai_enabled !== 'boolean';
  if (!savingAnalysisSetting) toggle.checked = enabled;
  $('ai-access').disabled = savingProviderSetting || savingCodexModel || !S.ai_provider;
  if (!savingProviderSetting) $('ai-access').value = S.ai_provider || 'api';
  const providerHelp = {
    api: 'Uses the API or local provider configured below.',
    claude_code: 'Uses your signed-in Claude Code account and its plan limits or credits. Run claude auth login in Terminal first. API billing is never used as a fallback.',
    codex: 'Uses your ChatGPT account through Codex and its plan limits or credits. Run codex login in Terminal first. Includes your global Codex instructions along with extracted PDF text; figures and scanned pages are unavailable. API billing is never used as a fallback.',
  };
  $('ai-access-help').textContent = providerHelp[S.ai_provider] || providerHelp.api;
  $('chat-access-help').textContent = S.ai_provider === 'codex'
    ? 'ChatGPT receives page-labeled PDF text and your global Codex instructions. Choose Claude to omit that global guidance. Figures and scanned pages are unavailable.'
    : S.ai_provider === 'api' && S.provider !== 'anthropic'
      ? 'The selected API or local model receives page-labeled PDF text; images are unavailable.'
      : 'Claude receives the PDF, including its page images.';
  $('ai-provider-form').hidden = S.ai_provider !== 'api';
  $('codex-model-form').hidden = S.ai_provider !== 'codex';
  $('codex-model').disabled = savingCodexModel;
  $('codex-custom-model').disabled = savingCodexModel;
  $('codex-model-save').disabled = savingCodexModel || savingProviderSetting;
  $('codex-model-refresh').disabled = loadingCodexModels;
  $('codex-current-model').textContent = `Current model: ${S.codex_model || 'Codex default (automatic)'}`;
  $('chat-send').disabled = !enabled || Boolean(activeChat && activeChat.busy);
  $('ai-action-status').hidden = enabled;
  $('auto-extract').disabled = !enabled;
  $('auto-extract').checked = enabled && readOnArrival;
  $('import-analysis-status').hidden = S.ai_enabled !== false;
  document.querySelectorAll('[data-ai-action]').forEach((button) => {
    button.disabled = !enabled || (button.hasAttribute('data-paper-chat')
      && !S.papers.some(p => p.key === button.dataset.paperChat && p.has_pdf));
  });
  if (typeof syncAgentChatControls === 'function') syncAgentChatControls();
}

$('ai-enabled').addEventListener('change', async () => {
  const enabled = $('ai-enabled').checked;
  savingAnalysisSetting = true;
  syncAnalysisControls();
  $('ai-settings-status').textContent = 'Saving…';
  try {
    const settings = await api('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ai_enabled: enabled }),
    });
    S.ai_enabled = settings.ai_enabled;
    workspaceClient.invalidateState();
    $('ai-settings-status').textContent = settings.ai_enabled ? 'AI analysis enabled.' : 'AI analysis disabled.';
  } catch (error) {
    $('ai-settings-status').textContent = `Could not save: ${error.message}`;
  } finally {
    savingAnalysisSetting = false;
    syncAnalysisControls();
    renderStats();
  }
});

$('ai-access').addEventListener('change', async () => {
  const provider = $('ai-access').value;
  savingProviderSetting = true;
  syncAnalysisControls();
  $('ai-settings-status').textContent = 'Saving…';
  try {
    const settings = await api('/api/settings/provider', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ai_provider: provider }),
    });
    S.ai_provider = settings.ai_provider;
    // The open Settings panel already owns the draft, including custom-mode
    // intent. Returning to Codex reveals it without replacing it with saved state.
    if (S.ai_provider === 'codex' && !codexModelsLoaded) loadCodexModels();
    workspaceClient.invalidateState();
    $('ai-settings-status').textContent = 'Model access saved.';
  } catch (error) {
    $('ai-settings-status').textContent = `Could not save: ${error.message}`;
  } finally {
    savingProviderSetting = false;
    syncAnalysisControls();
    renderStats();
  }
});

function selectedCodexModel() {
  return $('codex-model').value === 'custom' ? $('codex-custom-model').value : $('codex-model').value;
}

function showCodexModelFields(model = S.codex_model || '',
  custom = Boolean(model && !codexModels.some((item) => item.id === model))) {
  const picker = $('codex-model');
  picker.innerHTML = '<option value="">Codex default (automatic)</option>'
    + codexModels.map((item) => `<option value="${esc(item.id)}">${esc(item.name)} (${esc(item.id)})</option>`).join('')
    + '<option value="custom">Custom model…</option>';
  picker.value = custom ? 'custom' : model;
  $('codex-custom-model').value = custom ? model : '';
  syncCodexCustomField();
}

function syncCodexCustomField() {
  const custom = $('codex-model').value === 'custom';
  $('codex-custom-model-row').hidden = !custom;
  $('codex-custom-model').required = custom;
}

async function loadCodexModels() {
  if (loadingCodexModels) return;
  loadingCodexModels = true;
  $('codex-models-status').textContent = 'Loading models…';
  syncAnalysisControls();
  try {
    const result = await api('/api/settings/codex/models');
    codexModels = result.models;
    codexModelsLoaded = true;
    // Keep a draft typed or selected while discovery was in flight.
    showCodexModelFields(selectedCodexModel(),
      codexModelEdited && $('codex-model').value === 'custom' ? true : undefined);
    $('codex-models-status').textContent = codexModels.length ? '' : 'No models listed. You can enter a custom model ID.';
  } catch (error) {
    $('codex-models-status').textContent = `${error.message} You can still select Codex default or enter a custom model ID.`;
  } finally {
    loadingCodexModels = false;
    syncAnalysisControls();
  }
}

$('codex-model').addEventListener('change', () => {
  codexModelEdited = true;
  syncCodexCustomField();
});
$('codex-custom-model').addEventListener('input', () => { codexModelEdited = true; });
$('codex-model-refresh').addEventListener('click', loadCodexModels);
$('codex-model-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const model = selectedCodexModel().trim();
  savingCodexModel = true;
  syncAnalysisControls();
  $('ai-settings-status').textContent = 'Saving…';
  try {
    const settings = await api('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ codex_model: model }),
    });
    Object.assign(S, settings);
    workspaceClient.invalidateState();
    $('ai-settings-status').textContent = 'Model saved. New analysis uses this selection.';
    renderStats();
  } catch (error) {
    $('ai-settings-status').textContent = `Could not save: ${error.message}`;
  } finally {
    savingCodexModel = false;
    syncAnalysisControls();
  }
});

// Conversations are scoped by workspace and paper, and live only in this tab.
const paperChats = new Map();
let activeChat = null;
function renderPaperChat() {
  if (!activeChat) return;
  $('chat-messages').innerHTML = activeChat.messages.map((m) =>
    `<div class="note"><span class="who">${m.role === 'user' ? 'You' : esc(m.assistant || 'Claude')}</span>
      ${paragraphs(m.content).map((p) => `<p>${esc(p)}</p>`).join('')}</div>`).join('');
  $('chat-question').value = activeChat.draft;
  $('chat-question').disabled = activeChat.busy;
  $('chat-clear').disabled = activeChat.busy;
  $('chat-status').textContent = activeChat.busy ? 'Reading the paper…' : activeChat.error || '';
  syncAnalysisControls();
}
document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-paper-chat]');
  if (!button || button.disabled) return;
  const key = button.dataset.paperChat;
  const id = JSON.stringify([workspaceClient.workspaceId, key]);
  if (!paperChats.has(id)) paperChats.set(id, { key, workspace: workspaceClient.workspaceId, messages: [], draft: '', busy: false, error: '' });
  activeChat = paperChats.get(id);
  $('chat-paper-title').textContent = (S.papers.find((p) => p.key === key) || {}).title || key;
  renderPaperChat();
  $('paper-chat').showModal();
  $('chat-question').focus();
});
// Chat boxes send on Enter and take Shift+Enter as a new line, like other chat apps.
// Enter that confirms an IME composition belongs to the composition, not the form;
// WebKit reports that Enter with isComposing false and keyCode 229. A held key sends once.
function sendOnEnter(field) {
  field.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.shiftKey || event.repeat) return;
    if (event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    field.form.requestSubmit();
  });
}
sendOnEnter($('chat-question'));
$('chat-question').addEventListener('input', () => { if (activeChat) activeChat.draft = $('chat-question').value; });
$('chat-close').addEventListener('click', () => $('paper-chat').close());
$('chat-clear').addEventListener('click', () => {
  if (!activeChat || activeChat.busy) return;
  activeChat.messages = []; activeChat.draft = ''; activeChat.error = '';
  renderPaperChat();
});
$('chat-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const chat = activeChat;
  if (!chat || chat.busy || S.ai_enabled !== true || !chat.draft.trim()) return;
  const question = chat.draft.trim();
  const messages = [...chat.messages, { role: 'user', content: question }];
  const lengths = messages.map((message) => Array.from(message.content).length);
  if (messages.length > 40 || lengths.some((length) => length > 32000)
      || lengths.reduce((total, length) => total + length, 0) > 80000) {
    chat.error = 'Start a new conversation; this one is too long.';
    renderPaperChat();
    return;
  }
  chat.busy = true; chat.error = '';
  renderPaperChat();
  try {
    const result = await api(`/api/papers/${encodeURIComponent(chat.key)}/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Doxograph-Workspace': chat.workspace },
      body: JSON.stringify({ messages: messages.map(({ role, content }) => ({ role, content })) }),
    });
    chat.messages = [...messages, { role: 'assistant', content: result.answer, assistant: result.assistant || 'Claude' }];
    chat.draft = '';
  } catch (error) { chat.error = error.message; }
  finally {
    chat.busy = false;
    if (activeChat === chat) renderPaperChat();
  }
});

let providerDrafts = {};

function showProviderFields() {
  const provider = $('ai-provider').value;
  const profile = providerDrafts[provider] || S.providers?.[provider] || {};
  $('ai-model').value = profile.model || '';
  $('ai-base-url').value = profile.base_url || '';
  $('ai-endpoint-row').hidden = ['anthropic', 'openai'].includes(provider);
  $('ai-base-url').required = !['anthropic', 'openai'].includes(provider);
  const help = {
    anthropic: 'Uses ANTHROPIC_API_KEY from your environment or ~/.credentials. Sends PDFs to Anthropic.',
    openai: 'Uses OPENAI_API_KEY from your environment or ~/.credentials. Sends paper text to OpenAI. Choose a model that supports structured output.',
    ollama: 'Start Ollama and download a model first. Paper text goes only to this endpoint. Use a localhost URL and a locally downloaded model to keep analysis on your Mac. Long papers may exceed the local context limit.',
    'openai-compatible': 'For LM Studio or another endpoint supporting chat completions with JSON Schema. Include /v1 in the URL when required. Uses DOXOGRAPH_API_KEY if set; local servers can run without a key. Paper text goes to this endpoint.',
  };
  $('ai-provider-help').textContent = help[provider] || '';
}

let editingProvider = 'anthropic';
$('ai-provider').addEventListener('change', () => {
  providerDrafts[editingProvider] = { model: $('ai-model').value, base_url: $('ai-base-url').value };
  editingProvider = $('ai-provider').value;
  showProviderFields();
});
$('ai-provider-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('ai-provider-save');
  button.disabled = true;
  $('ai-settings-status').textContent = 'Saving…';
  try {
    const settings = await api('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: $('ai-provider').value, model: $('ai-model').value,
        base_url: $('ai-base-url').value }),
    });
    Object.assign(S, settings);
    workspaceClient.invalidateState();
    providerDrafts = {};
    $('ai-settings-status').textContent = 'Provider saved. New analysis uses this selection.';
    renderStats();
  } catch (error) {
    $('ai-settings-status').textContent = `Could not save: ${error.message}`;
  } finally {
    button.disabled = false;
  }
});

function syncThemeControls() {
  const appearance = document.querySelector(`input[name="appearance"][value="${themeSettings.appearance}"]`);
  const colors = document.querySelector(`input[name="color-theme"][value="${themeSettings.colors}"]`);
  if (appearance) appearance.checked = true;
  if (colors) colors.checked = true;
}

function closeSettings({ restoreFocus = false } = {}) {
  $('settings-menu').hidden = true;
  $('btn-settings').setAttribute('aria-expanded', 'false');
  if (restoreFocus) $('btn-settings').focus();
}

function openSettings() {
  closePaperMenu();
  closeActionDisclosures();
  syncThemeControls();
  syncAnalysisControls();
  providerDrafts = {};
  editingProvider = S.provider || 'anthropic';
  $('ai-provider').value = editingProvider;
  showProviderFields();
  codexModelEdited = false;
  showCodexModelFields();
  if (S.ai_provider === 'codex' && !codexModelsLoaded) loadCodexModels();
  $('settings-menu').hidden = false;
  $('btn-settings').setAttribute('aria-expanded', 'true');
}

$('btn-settings').addEventListener('click', () => {
  if ($('settings-menu').hidden) openSettings(); else closeSettings();
});
$('btn-close-settings').addEventListener('click', () => closeSettings({ restoreFocus: true }));
$('settings-menu').addEventListener('change', (event) => {
  if (event.target.name === 'appearance') {
    applyThemeSettings({ ...themeSettings, appearance: event.target.value }, true);
  } else if (event.target.name === 'color-theme') {
    applyThemeSettings({ ...themeSettings, colors: event.target.value }, true);
  }
});
$('btn-reset-theme').addEventListener('click', () => {
  applyThemeSettings({ ...DEFAULT_THEME }, true);
  syncThemeControls();
});

document.addEventListener('mousedown', (event) => {
  if (!$('settings-menu').contains(event.target) && !$('btn-settings').contains(event.target)) closeSettings();
});

// Shell disclosures share dismissal behavior without consuming Escape in an
// editor behind them. Native details keeps keyboard and expanded state intact.
function closeActionDisclosures({ except = null, restoreFocus = false } = {}) {
  for (const menu of document.querySelectorAll('.action-disclosure[open]')) {
    if (menu === except) continue;
    if (restoreFocus && menu.contains(document.activeElement)) menu.querySelector('summary').focus();
    menu.open = false;
  }
}
document.addEventListener('mousedown', (event) => {
  closeActionDisclosures({ except: event.target.closest('.action-disclosure') });
});
$('workspace-actions').addEventListener('click', (event) => {
  if (event.target.closest('button')) closeActionDisclosures({ restoreFocus: true });
});
$('btn-close-filters').addEventListener('click', () => closeActionDisclosures({ restoreFocus: true }));
document.querySelectorAll('.action-disclosure').forEach(menu => {
  menu.addEventListener('toggle', () => {
    if (menu.open) {
      closeActionDisclosures({ except: menu });
      closeSettings();
    }
  });
});

// --- paper context menu --------------------------------------------------

let paperMenuTrigger = null;
let paperMenuScroll = null;

function restorePaperMenuFocus(trigger) {
  if (!trigger || trigger.workspace !== workspaceClient.workspaceId) return;
  $('papers').querySelector(`[data-menu="${CSS.escape(trigger.paper)}"]`)?.focus({ preventScroll: true });
}

function openPaperMenu(paper, x, y, trigger = null) {
  // Keep the identity rather than the node: polling and label saves rebuild
  // the list while the menu or its editor is open.
  paperMenuTrigger = trigger ? { paper, workspace: workspaceClient.workspaceId } : null;
  paperMenuScroll = new Map([$('side'), $('papers')].map(node =>
    [node, { top: node.scrollTop, left: node.scrollLeft }]));
  const p = S.papers.find((row) => row.key === paper);
  const menu = $('ctxmenu');
  menu.innerHTML = `
    <li class="mh">${esc(p ? p.title || paper : paper)}</li>
    ${p && p.has_pdf ? `<li><button type="button" data-act="open-pdf" data-paper="${esc(paper)}">Open the PDF</button></li>` : ''}
    <li><button type="button" data-act="labels" data-paper="${esc(paper)}">${p?.labels?.length ? 'Edit labels…' : 'Add labels…'}</button></li>
    <li><button type="button" data-act="del-paper" data-paper="${esc(paper)}">Remove paper</button></li>`;
  menu.hidden = false;
  // Measure after showing, then keep the whole menu inside the window.
  const { width, height } = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(0, Math.min(x, window.innerWidth - width - 4))}px`;
  menu.style.top = `${Math.max(0, Math.min(y, window.innerHeight - height - 4))}px`;
  if (trigger) menu.querySelector('button')?.focus({ preventScroll: true });
}

function closePaperMenu({ restoreFocus = false } = {}) {
  $('ctxmenu').hidden = true;
  const trigger = paperMenuTrigger;
  paperMenuTrigger = null;
  paperMenuScroll = null;
  if (restoreFocus) restorePaperMenuFocus(trigger);
  return trigger;
}

$('papers').addEventListener('contextmenu', (event) => {
  const li = event.target.closest('[data-paper]');
  if (!li || !li.dataset.paper) return;   // "All papers" has no paper actions
  event.preventDefault();
  openPaperMenu(li.dataset.paper, event.clientX, event.clientY, li.querySelector('[data-menu]'));
});

$('ctxmenu').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const trigger = closePaperMenu({ restoreFocus: true });
  const paper = button.dataset.paper;
  if (button.dataset.act === 'labels') {
    try {
      await editLabels(paper);
    } finally {
      // A save replaces the restored button. Recover focus only if it fell
      // back to the document, preserving any new focus chosen during the save.
      if (document.activeElement === document.body) restorePaperMenuFocus(trigger);
    }
  }
  // The paper stays in the list until its DELETE comes back, so the menu can
  // be opened on it again and asked to remove it a second time.
  if (button.dataset.act === 'del-paper') {
    if (refuseWhileRemoving(paper, 'a second removal')) return;
    await removePaper(paper);
  }
  if (button.dataset.act === 'open-pdf') {
    window.open(`/pdf/${encodeURIComponent(paper)}?${workspaceQuery()}&inline=1`, '_blank');
  }
});

$('ctxmenu').addEventListener('keydown', (event) => {
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  const buttons = [...$('ctxmenu').querySelectorAll('button')];
  if (!buttons.length) return;
  const current = buttons.indexOf(document.activeElement);
  const index = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
    : (current + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
  event.preventDefault();
  event.stopPropagation();
  buttons[index].focus({ preventScroll: true });
});

// A press anywhere outside the menu dismisses it. Right-clicking another paper
// also lands here first, then the contextmenu handler above reopens it there.
document.addEventListener('mousedown', (event) => {
  if (!$('ctxmenu').contains(event.target)) closePaperMenu();
});
// Escape is handled in the keyboard section below, ahead of the editor keys,
// so closing the menu does not also cancel an open editor.
// Paper lists scroll independently; scroll does not bubble from descendants.
$('side').addEventListener('scroll', event => {
  if ($('ctxmenu').hidden) return;
  // Opening by pointer can follow a scroll in the same frame. Ignore that
  // queued event when its position was already accounted for on opening.
  const start = paperMenuScroll?.get(event.target);
  if (start && start.top === event.target.scrollTop && start.left === event.target.scrollLeft) return;
  closePaperMenu({ restoreFocus: true });
}, { capture: true });
window.addEventListener('resize', () => closePaperMenu({ restoreFocus: true }));

// Choosing a label from the sidebar or from a paper's own header. A label is
// a property of the paper, so it is read as "show me the papers like this
// one": a paper open on screen that does not carry the label is left, rather
// than sitting there unchanged while the list behind it narrows to papers the
// reader cannot see.
function chooseLabel(name) {
  // A chip spelled differently on its paper filters by the sidebar's spelling.
  const next = V.label && labelKey(V.label) === labelKey(name) ? null
    : findLabel(Object.keys(S.label_counts || {}), name) || name;
  const open = V.paper && S.papers.find((p) => p.key === V.paper);
  if (next && open && findLabel(open.labels, next) === undefined) {
    captureOpenEditor();
    parkNewClaimForNavigation(null);
    parkSynthEditor();
    V.paper = null;
    V.selectedId = null;
    V.editing = null;
  }
  V.label = next;
  syncHash(true);
  renderAll();
}

$('labels').addEventListener('click', (event) => {
  const li = event.target.closest('[data-label]');
  if (!li) return;
  chooseLabel(li.dataset.label);
});

$('tags').addEventListener('click', (event) => {
  const li = event.target.closest('[data-tag]');
  if (!li) return;
  V.tag = V.tag === li.dataset.tag ? null : li.dataset.tag;
  syncHash(true);
  renderAll();
});

$('workspace').addEventListener('change', (event) => switchWorkspace(event.target.value));

let deletingWorkspace = false;
$('btn-workspace-delete').addEventListener('click', async () => {
  const workspace = currentWorkspace();
  if (!workspace || workspace.id === 'default' || deletingWorkspace || workspaceController.pending) return;
  const confirmed = await confirmDialog(`Delete “${workspace.name}”?`, {
    note: 'This permanently deletes this workspace and all of its papers, PDFs, claims, notes, and exports, including unsaved edits. This cannot be undone. You will return to the default workspace.',
    ok: 'Delete workspace',
  });
  if (!confirmed || workspace.id !== workspaceClient.workspaceId) return;
  if (workspaceClient.pendingMutations) {
    toast('Wait for the current change to finish before deleting this workspace.', { tone: 'warn' });
    return;
  }
  deletingWorkspace = true;
  // Keep editors and navigation still until the destructive request settles.
  // On failure, the current view and all drafts remain available.
  document.body.inert = true;
  const notice = toast('Deleting workspace…', { timeout: 0 });
  try {
    await api(`/api/workspaces/${encodeURIComponent(workspace.id)}`, { method: 'DELETE' });
    workspaceClient.select('default');
    // A fresh page also retires autosave timers, held paper deletions, and
    // feature editors whose data belonged to the removed corpus.
    // Deletion already confirmed discarding these editors' unsaved changes.
    window.addEventListener('beforeunload', event => event.stopImmediatePropagation(), { capture: true, once: true });
    location.replace('/#ws=default');
    location.reload();
  } catch (error) {
    toast(`Could not delete workspace: ${error.message}`, { tone: 'warn' });
  } finally {
    notice.close();
    document.body.inert = false;
    deletingWorkspace = false;
  }
});

$('btn-workspace-rename').addEventListener('click', async () => {
  const workspace = currentWorkspace();
  if (!workspace) return;
  const name = await askDialog({
    title: 'Rename workspace',
    note: 'Your papers, claims, and notes stay in this workspace.',
    input: { value: workspace.name }, ok: 'Rename',
  });
  if (name === null || name === workspace.name) return;
  try {
    const result = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    workspaceClient.setWorkspaces(result.workspaces);
    renderWorkspacePicker();
    await refresh();
  } catch (error) {
    toast(`Could not rename workspace: ${error.message}`, { tone: 'warn' });
  }
});

$('btn-workspace-add').addEventListener('click', async () => {
  const name = await askText('Name this workspace', {
    note: 'A workspace is an independent corpus: its own papers, topics, and exports.',
    placeholder: 'Consciousness, or Animal locomotion',
  });
  if (!name || !name.trim()) return;
  try {
    const result = await api('/api/workspaces', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    workspaceClient.setWorkspaces(result.workspaces || workspaceClient.workspaces);
    renderWorkspacePicker();
    await switchWorkspace(result.workspace.id);
  } catch (error) {
    toast(`Could not create workspace: ${error.message}`, { tone: 'warn' });
  }
});

let addingReferences = false;

async function addReferences() {
  if (addingReferences) return;
  const text = $('refs').value.trim();
  if (!text) return;
  const workspace = workspaceClient.workspaceId;
  addingReferences = true;
  $('btn-add').disabled = true;
  $('btn-add').textContent = 'Adding…';
  $('import-status').hidden = false;
  $('import-status').textContent = 'Submitting references…';
  let result;
  try {
    result = await api('/api/ingest', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, extract: $('auto-extract').checked }),
    });
  } catch (error) {
    // What was pasted stays in the box either way: a refused request is a
    // reason to try again, not a reason to lose the list.
    toast(`Could not add these references: ${error.message}`, { tone: 'warn' });
    $('import-status').textContent = `Could not add these references: ${error.message}`;
    return;
  } finally {
    addingReferences = false;
    $('btn-add').disabled = false;
    $('btn-add').textContent = 'Add';
  }
  // A request can finish after a workspace switch. Its notices belong to the
  // workspace it was submitted in, as do the references it would clear.
  if (workspace !== workspaceClient.workspaceId) return;
  const unknown = result.unknown || [];
  if ($('refs').value.trim() === text) $('refs').value = unknown.join('\n');
  $('import-status').hidden = !result.queued;
  if (result.queued) $('import-status').textContent =
    `Import started for ${result.queued} ${result.queued === 1 ? 'paper' : 'papers'}. Progress appears in the notifications.`;
  // Which lines failed, not how many: a count leaves the reader to work out
  // for themselves which of what they pasted is the problem.
  showRefWarning(unknown);
  reportKnown(result);
  try {
    await refresh();
  } catch (error) {
    toast(`References submitted, but the library could not refresh: ${error.message}`, { tone: 'warn' });
  }
}

// References the server recognised without fetching anything. They never
// become jobs, so the strip never mentions them and this is the only place
// they are reported.
function reportKnown(result) {
  const known = (result && result.known) || [];
  if (!known.length) return;
  const rest = known.length > 1 ? `, and ${known.length - 1} more` : '';
  toast(`Already in the corpus: ${known[0].key}${rest}.`);
}

function showRefWarning(unknown) {
  const warning = $('ref-warn');
  warning.hidden = !unknown.length;
  if (!unknown.length) return;
  const shown = unknown.slice(0, 3).map((line) => `“${line}”`).join(', ');
  const rest = unknown.length > 3 ? `, and ${unknown.length - 3} more` : '';
  warning.textContent = `Nothing identifies ${shown}${rest}. `
    + 'They are still in the box; paste an arXiv ID, a DOI, or a direct PDF link.';
}

$('btn-add').addEventListener('click', addReferences);

// The box is where references are typed, so it takes the usual way of saying
// "done": the button is a long way from the cursor otherwise.
$('refs').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    addReferences();
  }
});

$('btn-tag').addEventListener('click', async () => {
  const name = $('new-tag').value.trim();
  if (!name) return;
  try {
    await api('/api/tags', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, description: '' }),
    });
  } catch (error) {
    // The name stays in the box, as a refused reference list does.
    toast(`Could not add the topic: ${error.message}`, { tone: 'warn' });
    return;
  }
  $('new-tag').value = '';
  await refresh();
});

$('btn-retag').addEventListener('click', async () => {
  const go = await confirmDialog('Reassign topics on every paper?', {
    note: 'Every paper with claims is sent to the model again against the current vocabulary. '
      + 'Claim text you have edited is left alone.',
    ok: 'Retag all',
  });
  if (!go) return;
  const workspace = await settleDeletes();
  if (!workspace) return;   // the delete failed; the pass would read the claim
  try {
    await api('/api/retag', {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...workspace }, body: '{}',
    });
  } catch (error) {
    toast(`Could not retag the papers: ${error.message}`, { tone: 'warn' });
    return;
  }
  await refresh();
});

$('btn-export').addEventListener('click', async () => {
  const selected = currentWorkspace();
  // As with the review undo: the notice can outlive the picker, and Open must
  // hand back the file this export wrote, not whatever the workspace selected
  // by then last exported.
  // The file is written from what is on file, so a delete still waiting out
  // its notice would otherwise be exported as a live claim.
  const headers = await settleDeletes();
  // The delete failed, so the claim is still on file and the export would
  // carry it. The notice about the delete is the one to answer first.
  if (!headers) return;
  const workspace = headers['X-Doxograph-Workspace'] || 'default';
  let result;
  try {
    result = await api('/api/export', {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...headers },
      body: JSON.stringify({ title: selected ? selected.name : 'Doxograph' }),
    });
  } catch (error) {
    toast(`Could not export: ${error.message}`, { tone: 'warn' });
    return;
  }
  // The path on its own is something to go and find. What the reader wants is
  // the file, or the path where they can paste it.
  toast(`Exported to ${result.path}`, {
    timeout: 12000,
    actions: [
      { label: 'Open', onClick: () => window.open(`/export?workspace=${encodeURIComponent(workspace)}`, '_blank') },
      { label: 'Copy path', onClick: () => copyText(result.path, 'Path copied.') },
    ],
  });
});

async function copyText(text, done) {
  try {
    await navigator.clipboard.writeText(text);
    toast(done, { timeout: 2500 });
  } catch (error) {
    toast(`Could not copy: ${error.message}`, { tone: 'warn' });
  }
}

$('btn-bib').addEventListener('click', () => window.open(`/api/bibtex?${workspaceQuery()}`, '_blank'));

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
function applyQuery(value) {
  captureOpenEditor();
  V.q = value;
  scheduleTextSearch();
  renderPapers();   // the query filters the paper list as well as the claims
  renderContent();
}

function clearQuery() {
  applyQuery('');
}

$('q').addEventListener('input', (e) => applyQuery(e.target.value));
$('kind').addEventListener('change', (e) => { captureOpenEditor(); V.kind = e.target.value; renderContent(); });
$('paper-sort').addEventListener('change', (e) => {
  V.paperSort = PAPER_SORTS.includes(e.target.value) ? e.target.value : PAPER_SORTS[0];
  try { localStorage.setItem(PAPER_SORT_KEY, V.paperSort); } catch (error) { /* preference remains for this page */ }
  renderPapers();
  if (V.view === 'library') renderContent();
});
$('paper-sort').value = V.paperSort;
$('only-unreviewed').addEventListener('change', (e) => { captureOpenEditor(); V.unreviewed = e.target.checked; renderContent(); });
$('only-unverified').addEventListener('change', (e) => { captureOpenEditor(); V.unverified = e.target.checked; renderContent(); });
$('group-by-tag').addEventListener('change', (e) => { captureOpenEditor(); V.group = e.target.checked; renderContent(); });

for (const [id, view] of [['btn-library-view', 'library'], ['btn-claims-view', 'claims']]) {
  $(id).addEventListener('click', () => {
    showView(view);
    V.selectedId = null;
    syncHash(true);
    renderAll();
  });
}

// --- keyboard -------------------------------------------------------------

function selectedRow(rows) {
  return rows.find((row) => row.id === V.selectedId) || rows[0] || null;
}

function moveSelection(rows, step) {
  if (!rows.length) return;
  captureOpenEditor();
  const at = rows.findIndex((row) => row.id === V.selectedId);
  const next = at < 0 ? 0 : Math.min(Math.max(at + step, 0), rows.length - 1);
  selectClaim(rows[next].id);
  renderContent();
  scrollToSelected();
}

// The next claim nobody has reviewed, wrapping round the end: review runs to
// the bottom of the list and then wants the ones passed over on the way.
function selectNextUnreviewed(rows) {
  if (!rows.length) return;
  const at = rows.findIndex((row) => row.id === V.selectedId);
  const ordered = rows.slice(at + 1).concat(rows.slice(0, at + 1));
  const next = ordered.find((row) => !row.reviewed);
  if (!next) {
    toast('Every claim on screen has been reviewed.', { timeout: 2500 });
    return;
  }
  captureOpenEditor();
  selectClaim(next.id);
  renderContent();
  scrollToSelected();
}

function openHelp() {
  closePaperMenu();
  closeSettings();
  if (!$('help').open) $('help').showModal();
}

// The palette reads the same workspace snapshot as the sidebar. Opening a
// destination clears filters so a previous search cannot hide what was chosen.
const CommandPalette = (() => {
  const dialog = $('command-palette'), query = $('command-query'), list = $('command-results');
  let results = [], active = 0, sourceWorkspace = null, signature = '';
  const normalize = value => String(value || '').normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();

  function library(paper = null, tag = null) {
    captureOpenEditor();
    parkNewClaimForNavigation(paper);
    parkSynthEditor();
    parkNoteEditor();
    showView(paper || tag ? 'claims' : 'library');
    Object.assign(V, {paper, tag, label: null, q: '', kind: '', unreviewed: false,
      unverified: false, editing: null, selectedId: null});
    $('q').value = ''; $('kind').value = '';
    $('only-unreviewed').checked = false; $('only-unverified').checked = false;
    dropTextSearch();
    syncHash(true);
    renderAll();
    $('main').scrollTop = 0;
  }

  function catalog() {
    const entries = [];
    const add = (id, kind, title, detail, run, keywords = '') => entries.push({id, kind, title, detail, run,
      search: normalize(`${title} ${detail} ${keywords} ${kind}`), name: normalize(title)});
    S.papers.forEach(p => add(`paper:${p.key}`, 'Paper', p.title || p.key,
      [...(p.authors || []), p.year, p.key].filter(Boolean).join(' · '), () => library(p.key)));
    const topics = new Map(S.tags.map(t => [t.name, t.description || '']));
    Object.keys(S.tag_counts).forEach(name => { if (!topics.has(name)) topics.set(name, ''); });
    topics.forEach((description, name) => add(`topic:${name}`, 'Topic', name, description, () => library(null, name)));
    (S.notebook?.boards || []).forEach(b => add(`board:${b.id}`, 'Board', b.title,
      `${b.entries.length} entries · Evidence board`, () => ResearchTools.openBoard(b.id)));
    add('all', 'Action', 'Show all papers', 'Browse every claim in this workspace', () => library(), 'library home');
    const click = (id, title, keywords = '') => {
      if (!$(id).disabled) add(id, 'Action', title, '', () => {
        revealSidebar($(id));
        $(id).click();
      }, keywords);
    };
    const view = (id, title, next) => add(id, 'Action', title, '', () => {
      if (V.view === next) return;
      showView(next); syncHash(true); renderAll();
      if (next === 'graph') loadCitations();
    });
    view('map', 'Open paper map', 'graph');
    view('tensions', 'Browse tensions', 'tensions');
    view('agreements', 'Browse agreements', 'agreements');
    view('research', 'Edit research context and personal claims', 'research');
    ['questions', 'boards', 'reading', 'marks', 'searches', 'connections', 'compare'].forEach(tool => {
      const titles = {questions: 'Open research questions', boards: 'Open evidence boards', reading: 'Open reading queue', marks: 'Open marks and questions', searches: 'Open saved searches',
        connections: 'Open claim connections', compare: 'Compare selected claims and papers'};
      add(`tool:${tool}`, 'Action', titles[tool], '', () => {
        const target = document.querySelector(`[data-tool="${tool}"]`);
        revealSidebar(target);
        target.click();
      });
    });
    add('import', 'Action', 'Add papers', 'Paste references or drop PDFs', focusPaperImport, 'import');
    add('search', 'Action', 'Search claims and PDF text', '', focusClaimSearch);
    click('btn-workspace-add', 'Create workspace');
    click('btn-export', 'Export HTML');
    click('btn-bib', 'Export BibTeX');
    click('btn-workspace-archive', 'Download portable workspace archive');
    click('btn-workspace-backups', 'Browse previous document versions');
    click('btn-settings', 'Open settings', 'appearance theme color AI');
    click('btn-help', 'Show keyboard shortcuts', 'help');
    click('btn-retag', 'Retag all papers');
    click('btn-tensions', 'Find tensions');
    click('btn-agreements', 'Find agreements');
    click('btn-synth', 'Synthesize topics');
    return entries;
  }

  function score(entry, words, text) {
    if (!words.every(word => entry.search.includes(word))) return -1;
    if (entry.name === text) return 0;
    if (entry.name.startsWith(text)) return 1;
    if (entry.name.includes(text)) return 2;
    return 3;
  }

  function select(index, scroll = true) {
    active = index;
    [...list.children].forEach((row, i) => row.setAttribute('aria-selected', String(i === active)));
    const row = list.children[active];
    if (row) {
      query.setAttribute('aria-activedescendant', row.id);
      if (scroll) row.scrollIntoView({block: 'nearest'});
    } else query.removeAttribute('aria-activedescendant');
  }

  function sync(force = false) {
    if (!dialog.open) return;
    // A workspace can also change through Back while this modal is open.
    if (sourceWorkspace !== workspaceClient.workspaceId || workspaceController.pending || S.workspace?.id !== workspaceClient.workspaceId) {
      dialog.close(); return;
    }
    const entries = catalog();
    const nextSignature = JSON.stringify(entries.map(({id, title, detail}) => [id, title, detail]));
    if (!force && signature === nextSignature) return;
    signature = nextSignature;
    const previous = results[active]?.id;
    const text = normalize(query.value.trim()), words = text.split(/\s+/).filter(Boolean);
    const matched = entries.map(entry => ({...entry, score: score(entry, words, text)})).filter(e => e.score >= 0);
    if (text) matched.sort((a, b) => a.score - b.score || a.title.localeCompare(b.title));
    const counts = {};
    results = matched.filter(e => {
      counts[e.kind] = (counts[e.kind] || 0) + 1;
      return text || e.kind === 'Action' || counts[e.kind] <= 6;
    }).slice(0, 60);
    list.innerHTML = results.map((e, i) => `<li id="command-result-${i}" role="option" data-command="${i}" aria-selected="false">
      <span class="command-kind">${esc(e.kind)}</span><span class="command-label"><strong>${esc(e.title)}</strong>
      ${e.detail ? `<small>${esc(e.detail)}</small>` : ''}</span></li>`).join('');
    $('command-status').textContent = !results.length ? 'No matches. Try a different title, topic, board, or action.'
      : `${results.length < matched.length ? `${results.length} of ${matched.length}` : results.length} results in ${currentWorkspace()?.name || 'this workspace'}${results.length < matched.length ? ' · Type to narrow your search' : ''}`;
    select(force ? 0 : Math.max(0, results.findIndex(e => e.id === previous)), false);
  }

  function open() {
    if (dialog.open) { query.focus(); return; }
    if (document.querySelector('dialog[open]')) return;
    if (workspaceController.pending || S.workspace?.id !== workspaceClient.workspaceId) return;
    closePaperMenu(); closeSettings();
    sourceWorkspace = workspaceClient.workspaceId;
    query.value = ''; signature = '';
    dialog.showModal(); sync(true); query.focus();
  }

  async function activate() {
    const entry = results[active];
    if (!entry) return;
    if (sourceWorkspace !== workspaceClient.workspaceId || workspaceController.pending) { dialog.close(); return; }
    // Recheck the live catalog: a background refresh may have removed a paper,
    // board, topic, or disabled an AI action since it was highlighted.
    const live = catalog().find(e => e.id === entry.id);
    if (!live) { sync(true); return; }
    dialog.close();
    try { await live.run(); }
    catch (error) { toast(`Could not open ${entry.title}: ${error.message}`, {tone: 'warn'}); }
  }

  $('btn-command-palette').addEventListener('click', open);
  $('btn-close-command-palette').addEventListener('click', () => dialog.close());
  query.addEventListener('input', () => sync(true));
  dialog.addEventListener('click', event => {
    const row = event.target.closest('[data-command]');
    if (row) { select(Number(row.dataset.command), false); activate(); }
    if (event.target === dialog) {
      const box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
    }
  });
  list.addEventListener('mousedown', event => event.preventDefault());
  dialog.addEventListener('keydown', event => {
    if (event.isComposing) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (results.length) select((active + (event.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length);
    } else if (event.key === 'Enter' && event.target === query) {
      event.preventDefault(); activate();
    }
  });
  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'k') {
      if (document.querySelector('dialog[open]') && !dialog.open) return;
      event.preventDefault();
      if (!event.repeat) { if (dialog.open) dialog.close(); else open(); }
    }
  });
  return {sync};
})();

$('btn-help').addEventListener('click', openHelp);
$('btn-close-help').addEventListener('click', () => $('help').close());
// A click on the backdrop is the element itself: the sheet is something to
// glance at, so anywhere off it puts it away.
$('help').addEventListener('click', (event) => {
  if (event.target === $('help')) $('help').close();
});

document.addEventListener('keydown', async (event) => {
  // A modal owns the keyboard while it is up, Escape included: the dialog
  // closes itself, and the editor branches below must not also fire.
  if (document.querySelector('dialog[open]')) return;
  if (event.key === 'Escape' && document.querySelector('.action-disclosure[open]')) {
    closeActionDisclosures({ restoreFocus: true });
    event.preventDefault();
    return;
  }
  if (event.key === 'Escape' && !$('settings-menu').hidden) {
    closeSettings({ restoreFocus: true });
    return;
  }
  // While the paper menu is open Escape belongs to it. Falling through to the
  // editor branches would also run `cancelEdit` and drop a draft the user
  // only meant to keep by dismissing the menu.
  if (event.key === 'Escape' && !$('ctxmenu').hidden) {
    closePaperMenu({ restoreFocus: true });
    event.preventDefault();
    return;
  }
  // This independent editor owns its keyboard. Escape here must not reach
  // the claim-editor fallback and discard a draft elsewhere in the view.
  if (event.target.closest?.('#workspace-notes-panel')) return;
  if (event.key === 'Escape') {
    const menu = document.querySelector('.paper-actions[open]');
    if (menu) {
      menu.open = false;
      menu.querySelector('summary').focus();
      event.preventDefault();
      return;
    }
  }
  const tag = (event.target.tagName || '').toLowerCase();
  if (['input', 'textarea', 'select'].includes(tag)) {
    if (event.key !== 'Escape') return;
    // In the search box Escape belongs to the search: clear it, or leave it if
    // it is already empty. It is the way back to the whole corpus, and it must
    // not reach past the box and cancel an editor further down the page.
    if (event.target.id === 'q') {
      if (V.q) { $('q').value = ''; clearQuery(); } else $('q').blur();
      return;
    }
    // Only the editor holding the cursor is cancelled. With a claim editor and
    // a synthesis editor open together, cancelling both would drop a draft
    // the user never meant to give up.
    if (event.target.closest('.synth')) {
      if (V.synthEditing && !V.synthSaving) cancelSynthEdit();
    } else if (event.target.closest('.note')) {
      if (V.noteEditing && !V.noteSaving) cancelNoteEdit();
    } else if (V.editing && event.target.closest('form[data-form]')) {
      // And only from inside the claim's own form. Escape in the new-topic
      // box, the add-papers textarea, the reader's page field or a mark's
      // note is a way out of that field, not a verdict on a draft elsewhere.
      cancelEdit();
    }
    return;
  }
  // A held Command, Control or Option is the browser's or the system's: Cmd+R
  // is a reload, not a review, and taking it here would flip a claim's flag on
  // the way out of the page.
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  // The shortcuts nobody can see are the ones nobody uses, so the list is a
  // keystroke away from anywhere.
  if (event.key === '?') { event.preventDefault(); openHelp(); return; }
  if (event.key === '/') {
    // The box filters the claims, so it belongs to that view: reaching for it
    // from the map or the tensions is a way of asking to go back.
    event.preventDefault();
    focusClaimSearch();
    return;
  }
  if (V.view !== 'claims') {
    if (event.key === 'Escape' && V.view !== 'library') { showView('library'); syncHash(true); renderAll(); }
    return;
  }
  const rows = visibleClaims();
  if (event.key === 'j' || event.key === 'ArrowDown') {
    moveSelection(rows, 1);
  } else if (event.key === 'k' || event.key === 'ArrowUp') {
    moveSelection(rows, -1);
  } else if (event.key === 'n') {
    selectNextUnreviewed(rows);
  } else if (event.key === 'e') {
    const row = selectedRow(rows);
    if (row) { setReadingClaims(false); captureOpenEditor(); V.editing = row.id; renderContent(); }
  } else if (event.key === 'r') {
    const row = selectedRow(rows);
    if (!row) return;
    // Marking one reviewed moves to the next, so a paper is reviewed by
    // holding one key rather than alternating between two. Taking a review
    // back does not move: that is a correction, and it is made where it is.
    const marking = !row.reviewed;
    const following = rows[rows.findIndex((other) => other.id === row.id) + 1];
    // The request is slow enough to press j again or click another claim while
    // it runs, and that is the later decision: advancing on top of it would
    // pull the selection back to where this review started.
    const picked = handPicked;
    const took = await toggleReviewed(row);
    if (took && marking && following && handPicked === picked
        && visibleClaims().some((other) => other.id === following.id)) {
      V.selectedId = following.id;
      renderContent();
      scrollToSelected();
    }
  } else if (event.key === 'Escape') {
    // The page's own Escape: from inside the claim's form, or with nothing in
    // particular focused. With focus on some other control — a sidebar
    // button, the divider — Escape is about that control, not a verdict on
    // a draft elsewhere on the page.
    if (event.target === document.body || event.target.closest('form[data-form]')) cancelEdit();
  }
});

function scrollToSelected() {
  const el = document.querySelector('.claim.sel');
  if (el) { setReadingClaims(false); el.scrollIntoView({ block: 'nearest' }); }
}

// --- drag and drop --------------------------------------------------------

// A text drag over a field is the browser's: a sentence selected in the reader
// and dragged into the quote box must land there, not be read as papers to
// add. Files are the page's wherever they are dropped.
const TEXT_INPUT_TYPES = new Set(['', 'text', 'search', 'url', 'email', 'tel', 'password', 'number']);

function dropBelongsToField(event) {
  const types = [...(event.dataTransfer?.types || [])];
  if (types.includes('Files')) return false;
  const field = event.target.closest?.('input, textarea, [contenteditable]:not([contenteditable="false"])');
  if (!field) return false;
  // A checkbox, a range slider or a file picker takes no text, so a drop on
  // one is a drop on the page.
  return field.tagName !== 'INPUT' || TEXT_INPUT_TYPES.has((field.getAttribute('type') || '').toLowerCase());
}

['dragenter', 'dragover'].forEach((type) => document.addEventListener(type, (event) => {
  if (dropBelongsToField(event)) return;
  event.preventDefault();
  document.body.classList.add('dragging');
}));
['dragleave', 'drop'].forEach((type) => document.addEventListener(type, (event) => {
  if (type === 'dragleave' && event.relatedTarget) return;
  document.body.classList.remove('dragging');
}));

document.addEventListener('drop', async (event) => {
  if (dropBelongsToField(event)) return;
  event.preventDefault();
  const files = [...(event.dataTransfer.files || [])].filter((f) => f.type === 'application/pdf'
    || f.name.toLowerCase().endsWith('.pdf'));
  const text = event.dataTransfer.getData('text/plain');
  try {
    if (files.length) {
      const body = new FormData();
      files.forEach((file) => body.append('files', file, file.name));
      await api(`/api/upload?extract_now=${$('auto-extract').checked}`, { method: 'POST', body });
    } else if (text && text.trim()) {
      reportKnown(await api('/api/ingest', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, extract: $('auto-extract').checked }),
      }));
    }
  } catch (error) {
    toast(`Could not add what was dropped: ${error.message}`, { tone: 'warn' });
  }
  await refresh();
});

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

AppStartup.run(boot);
