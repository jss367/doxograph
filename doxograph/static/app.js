'use strict';

// Appearance is deliberately a browser preference rather than corpus state:
// two people can view the same corpus without changing each other's display.
// Each color theme is a coordinated palette in light, dim, and dark variants,
// so switching it cannot accidentally leave text unreadable on its background.
// Dim is a softer dark mode: charcoal surfaces instead of near-black ones.
const THEME_STORAGE_KEY = 'doxograph-theme-v1';
const APPEARANCES = ['system', 'light', 'dim', 'dark'];
const DEFAULT_THEME = Object.freeze({ appearance: 'system', colors: 'slate' });
const THEME_PALETTES = {
  slate: {
    light: { ink: '#16181d', muted: '#5c6370', line: '#dfe2e8', bg: '#ffffff', panel: '#f6f7f9', accent: '#2f5d8a', warn: '#8a5a2f', ok: '#2f7a4f', sel: '#e8f0f8', 'accent-ink': '#ffffff' },
    dim: { ink: '#e3e5e9', muted: '#a2a8b3', line: '#454b56', bg: '#262931', panel: '#2e323b', accent: '#8db9e4', warn: '#dcab72', ok: '#8ccda5', sel: '#324152', 'accent-ink': '#121a24' },
    dark: { ink: '#e6e8ec', muted: '#99a0ad', line: '#343a44', bg: '#14161a', panel: '#1d2027', accent: '#7fb0e0', warn: '#d9a267', ok: '#7fc79b', sel: '#1f2a35', 'accent-ink': '#101820' },
  },
  forest: {
    light: { ink: '#17231b', muted: '#59695e', line: '#d7e2da', bg: '#fbfdfb', panel: '#f1f6f2', accent: '#2b6e48', warn: '#8a6028', ok: '#287447', sel: '#e2f1e7', 'accent-ink': '#ffffff' },
    dim: { ink: '#e2e9e4', muted: '#a1b0a5', line: '#43524a', bg: '#242c27', panel: '#2c352f', accent: '#82cc9f', warn: '#d9b074', ok: '#82cc9f', sel: '#2f4438', 'accent-ink': '#12241a' },
    dark: { ink: '#e4ece6', muted: '#98a99d', line: '#324239', bg: '#121914', panel: '#1a241d', accent: '#76c596', warn: '#d7aa68', ok: '#76c596', sel: '#1d3325', 'accent-ink': '#102018' },
  },
  plum: {
    light: { ink: '#241a27', muted: '#6c5e70', line: '#e2dbe5', bg: '#fefcff', panel: '#f7f2f8', accent: '#765183', warn: '#96603e', ok: '#477452', sel: '#f0e5f3', 'accent-ink': '#ffffff' },
    dim: { ink: '#ece6ee', muted: '#b3a6b7', line: '#55485a', bg: '#2b242e', panel: '#342c37', accent: '#c9a6d4', warn: '#dfa87d', ok: '#90c99b', sel: '#43354a', 'accent-ink': '#28182e' },
    dark: { ink: '#eee7f0', muted: '#ad9eb1', line: '#443848', bg: '#181319', panel: '#231c25', accent: '#c29bce', warn: '#dda071', ok: '#86c392', sel: '#34263a', 'accent-ink': '#24152a' },
  },
  sepia: {
    light: { ink: '#2d251d', muted: '#716556', line: '#ded3c2', bg: '#fcf8f1', panel: '#f4ecdf', accent: '#8a5138', warn: '#94621f', ok: '#52703c', sel: '#efe1d2', 'accent-ink': '#ffffff' },
    dim: { ink: '#ece4d7', muted: '#b6a893', line: '#5a4e41', bg: '#2e2822', panel: '#37302a', accent: '#dca383', warn: '#dfb676', ok: '#a2c388', sel: '#4a3a2f', 'accent-ink': '#2a1a11' },
    dark: { ink: '#eee5d7', muted: '#b1a28e', line: '#493e32', bg: '#1b1713', panel: '#28211b', accent: '#d99a78', warn: '#ddb06a', ok: '#99bd7d', sel: '#3b2b22', 'accent-ink': '#27170f' },
  },
  ocean: {
    light: { ink: '#132029', muted: '#556873', line: '#d3e0e6', bg: '#fafdfe', panel: '#eef5f8', accent: '#1f6f8b', warn: '#8d5f2a', ok: '#2f7a5f', sel: '#dfeff5', 'accent-ink': '#ffffff' },
    dim: { ink: '#e0e8ec', muted: '#9fb0b8', line: '#3e505a', bg: '#212b31', panel: '#293439', accent: '#6fc0dc', warn: '#d9ac70', ok: '#7fcbb0', sel: '#2c4250', 'accent-ink': '#0f2028' },
    dark: { ink: '#e2eaee', muted: '#94a7b0', line: '#2f414a', bg: '#0f181d', panel: '#172228', accent: '#63b6d3', warn: '#d6a565', ok: '#77c4a8', sel: '#193039', 'accent-ink': '#0b1a20' },
  },
  indigo: {
    light: { ink: '#181a2f', muted: '#5e627f', line: '#dddfeb', bg: '#fdfdff', panel: '#f3f4fa', accent: '#4a55a8', warn: '#8d5d2f', ok: '#3a7756', sel: '#e6e8f8', 'accent-ink': '#ffffff' },
    dim: { ink: '#e5e6ee', muted: '#a5a8bd', line: '#474b64', bg: '#262838', panel: '#2e3142', accent: '#9ea8ee', warn: '#dcab72', ok: '#8bcaa4', sel: '#353a5a', 'accent-ink': '#121530' },
    dark: { ink: '#e7e8f0', muted: '#9a9db4', line: '#35384f', bg: '#14151f', panel: '#1c1e2a', accent: '#939ee9', warn: '#d9a267', ok: '#7fc79b', sel: '#262a45', 'accent-ink': '#0f1128' },
  },
  rose: {
    light: { ink: '#2a1a1f', muted: '#726068', line: '#e8d9de', bg: '#fffbfc', panel: '#faf1f4', accent: '#a34a63', warn: '#95622a', ok: '#4b7a55', sel: '#f7e4ea', 'accent-ink': '#ffffff' },
    dim: { ink: '#efe6e9', muted: '#b5a3aa', line: '#584750', bg: '#2d2429', panel: '#372c32', accent: '#e397ad', warn: '#dfae76', ok: '#93c99a', sel: '#4a353f', 'accent-ink': '#2a141c' },
    dark: { ink: '#f0e6ea', muted: '#ad9aa2', line: '#47363d', bg: '#1a1316', panel: '#251c20', accent: '#dd8ba3', warn: '#dba46c', ok: '#8cc394', sel: '#3a2730', 'accent-ink': '#28121a' },
  },
  graphite: {
    light: { ink: '#1c1c1e', muted: '#66666b', line: '#e0e0e3', bg: '#ffffff', panel: '#f4f4f5', accent: '#9a5f10', warn: '#8a5a2f', ok: '#3d7a4a', sel: '#f5ebdc', 'accent-ink': '#ffffff' },
    dim: { ink: '#e6e6e8', muted: '#a6a6ab', line: '#4a4a4f', bg: '#28282b', panel: '#303034', accent: '#e0b060', warn: '#d9a267', ok: '#8fc79b', sel: '#3d3830', 'accent-ink': '#1e1608' },
    dark: { ink: '#e8e8ea', muted: '#9b9ba0', line: '#38383d', bg: '#161618', panel: '#1f1f22', accent: '#d8a852', warn: '#d9a267', ok: '#7fc79b', sel: '#332d22', 'accent-ink': '#1b1405' },
  },
};
const appearanceQuery = window.matchMedia('(prefers-color-scheme: dark)');
let themeSettings = readThemeSettings();

function readThemeSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(THEME_STORAGE_KEY) || '{}');
    return {
      appearance: APPEARANCES.includes(saved.appearance) ? saved.appearance : DEFAULT_THEME.appearance,
      colors: Object.hasOwn(THEME_PALETTES, saved.colors) ? saved.colors : DEFAULT_THEME.colors,
    };
  } catch (error) {
    return { ...DEFAULT_THEME };
  }
}

function applyThemeSettings(settings, persist = false) {
  themeSettings = settings;
  const mode = settings.appearance === 'system'
    ? (appearanceQuery.matches ? 'dark' : 'light')
    : settings.appearance;
  const root = document.documentElement;
  root.dataset.appearance = settings.appearance;
  root.dataset.colorTheme = settings.colors;
  root.style.colorScheme = mode === 'light' ? 'light' : 'dark';
  Object.entries(THEME_PALETTES[settings.colors][mode]).forEach(([name, value]) => {
    root.style.setProperty(`--${name}`, value);
  });
  // The map is painted pixels, not styled elements: unlike the rest of the
  // page it does not follow the variables, so repaint it. Deferred a frame
  // because the first call is made at load, before the map's state exists.
  requestAnimationFrame(() => { if (GRAPH.canvas) graphDraw(); });
  if (persist) {
    try { localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify(settings)); } catch (error) { /* preference remains for this page */ }
  }
}

applyThemeSettings(themeSettings);
appearanceQuery.addEventListener('change', () => {
  if (themeSettings.appearance === 'system') applyThemeSettings(themeSettings);
});

let S = { papers: [], claims: [], tags: [], tag_counts: {}, ledger: [], context: '', tensions: [], agreements: [], syntheses: [],
          kinds: [], strengths: [], relations: [], jobs: [], has_key: true };
let workspaces = [];
let currentWorkspaceId = null;
// The workspace change in flight, if any: its corpus is still on its way.
let workspaceSwitch = null;
let pendingMutations = 0;
// The page can be asked to write before the workspace registry has loaded: a
// Finder or Dock drop in the native app, a paste into the box, an Export. Take
// the workspace from the address, or from what this browser was last on, before
// any of that is possible — a request sent with no workspace named goes to the
// Default corpus, which is the one thing a link naming another must never do.
// `loadWorkspaces` corrects this if the name turns out not to exist.
try {
  const named = new URLSearchParams((location.hash || '').replace(/^#/, '')).get('ws');
  currentWorkspaceId = named || localStorage.getItem('doxograph-workspace') || 'default';
} catch (e) {
  currentWorkspaceId = 'default';
}
window.doxographWorkspaceId = currentWorkspaceId;
// selectedId is a claim id rather than a render position: in grouped mode a
// claim with several topics is drawn once per topic, so positions do not map
// onto claims one-to-one. newClaim holds a claim being written by hand; it lives
// only in the browser until Save, so navigating away or filtering it out cannot
// leave a blank claim behind on the server.
const NEW_CLAIM_ID = '__new__';
// drafts holds unsaved editor contents, keyed by claim id: what was typed but
// not yet saved, whether because the save failed or because something redrew the
// list. Without it a re-render redraws from the unchanged server row and
// silently discards the edit. A map rather than one slot, so opening a second
// claim's editor does not throw away the first one's draft.
// view is 'claims', 'tensions', 'agreements', 'research', or 'graph'. Only
// the first has a claim editor, so switching away closes any open one (keeping
// its draft) and lets the background poll run. graph holds the map's display
// choices, reset with the rest when the workspace changes; the layout itself
// is in GRAPH below.
// tensionFocus narrows the tensions view to those involving one claim; it is set
// by the marker on a claim card and cleared by "show all".
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
const V = { paper: null, tag: null, q: '', kind: '', unreviewed: false, unverified: false, group: true,
            editing: null, selectedId: null, newClaim: null, failedNewClaims: {},
            drafts: {}, error: null, view: 'claims', tensionStatus: '', tensionFocus: null, agreementStatus: '', agreementFocus: null,
            synthEditing: null, synthDrafts: {}, synthSaving: null, researchSaving: false, researchDraft: null, researchBase: null,
            graph: { topics: true, minShared: null, tensions: true, ledger: true }, paperSort: null,
            quoteContext: null, textSearch: null };

function blankClaim(paper) {
  return {
    id: NEW_CLAIM_ID, paper, text: '', kind: S.kinds[0] || 'finding',
    strength: 'supporting', tags: [], evidence: '', quote: '', locator: '',
    ledger_links: [], reviewed: true, paper_title: '', paper_authors: [], paper_year: null,
  };
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

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

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

function askDialog({ title, note = '', input = null, ok = 'OK', cancel = 'Cancel' }) {
  const dialog = $('ask');
  $('ask-title').textContent = title;
  $('ask-note').textContent = note;
  $('ask-note').hidden = !note;
  const field = $('ask-input');
  field.hidden = input === null;
  field.value = input ? (input.value || '') : '';
  field.placeholder = input ? (input.placeholder || '') : '';
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
$('ask').addEventListener('close', () => settleAsk(null));

const confirmDialog = (title, { note = '', ok = 'OK' } = {}) =>
  askDialog({ title, note, ok }).then((answer) => answer === true);
const askText = (title, { note = '', placeholder = '', value = '' } = {}) =>
  askDialog({ title, note, input: { placeholder, value } });

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
  stateEtag = null;
}

// Takes the waiting rows out of `S`. Called wherever `S` is replaced, and
// written to be safe to run twice on the same state: the per-paper counts are
// recounted from the claims that are left rather than decremented.
function pruneTrashed() {
  if (!trash.size) return;
  const kept = (S.claims || []).filter((row) => !trashed('claim', row.id));
  if (kept.length !== (S.claims || []).length) {
    S.claims = kept;
    const counts = new Map();
    kept.forEach((row) => {
      const count = counts.get(row.paper) || { claims: 0, unreviewed: 0 };
      count.claims += 1;
      if (!row.reviewed) count.unreviewed += 1;
      counts.set(row.paper, count);
    });
    (S.papers || []).forEach((paper) => {
      const count = counts.get(paper.key) || { claims: 0, unreviewed: 0 };
      paper.n_claims = count.claims;
      paper.n_unreviewed = count.unreviewed;
    });
  }
  S.agreements = (S.agreements || []).filter((row) => !trashed('agreement', row.id));
  S.syntheses = (S.syntheses || []).filter((row) => !trashed('synthesis', row.topic));
}

async function deleteLater(kind, id, path, label, { paper = null } = {}) {
  const token = `${kind}:${id}`;
  if (trash.has(token)) return;
  const workspace = currentWorkspaceId || 'default';
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
  await refreshAll();
  // The wait can end during that refresh — `flushTrash` sends what is waiting
  // — and a notice raised afterwards would offer to undo a delete the server
  // already has. The entry is still in the trash while its request is in
  // flight, which is what keeps the row off the screen, so `sending` is the
  // thing to ask: once it is set there is nothing left to undo.
  if (!trash.get(token) || trash.get(token).sending) return;
  notice = toast(`Deleted ${label}.`, {
    timeout: UNDO_MS,
    onExpire: send,
    actions: [{
      label: 'Undo',
      onClick: async () => {
        // Sent between the notice going up and this being clicked. `send`
        // closes the notice, so this is all but unreachable — but an Undo that
        // quietly does nothing is worse than one that says so.
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
  const workspace = currentWorkspaceId;
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
  const workspace = currentWorkspaceId || 'default';
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
    try { fetch(entry.path, { method: 'DELETE', headers, keepalive: true }); } catch (e) { /* leaving anyway */ }
  });
  trash.clear();
});

// --- the URL says what is on screen ---------------------------------------
//
// Without this a reload lands back on the whole corpus, Back leaves the app,
// and there is no way to hand someone a link to one topic. Navigation pushes a
// history entry; a filter or a selection replaces one, so Back does not have to
// walk through every keystroke.
const HASH_VIEWS = ['claims', 'tensions', 'agreements', 'research', 'graph'];

function hashParams() {
  return new URLSearchParams((location.hash || '').replace(/^#/, ''));
}

function currentHash() {
  const params = new URLSearchParams();
  if (currentWorkspaceId && currentWorkspaceId !== 'default') params.set('ws', currentWorkspaceId);
  if (V.view !== 'claims') params.set('view', V.view);
  if (V.paper) params.set('paper', V.paper);
  if (V.tag) params.set('tag', V.tag);
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

function syncHash(push = false) {
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
// at boot the URL is read before the first read of the corpus comes back.
function dropMissingFilters() {
  if (V.paper && !S.papers.some((paper) => paper.key === V.paper)) V.paper = null;
  if (V.tag && !Object.hasOwn(S.tag_counts || {}, V.tag)) V.tag = null;
  if (V.kind && !(S.kinds || []).includes(V.kind)) {
    V.kind = '';
    $('kind').value = '';
  }
}

// Sets the view from the URL without drawing it. The controls are set here too:
// they are the same state, and a filter the page has forgotten to tick is worse
// than no filter at all.
function applyHash() {
  const params = hashParams();
  const view = params.get('view') || 'claims';
  V.view = HASH_VIEWS.includes(view) ? view : 'claims';
  V.paper = params.get('paper') || null;
  V.tag = params.get('tag') || null;
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
  const wanted = hashParams().get('ws') || 'default';
  restoringHistory = true;
  try {
    // The workspace reset clears the filters; the URL being moved to puts back
    // whatever it holds, which is the whole point of going back to it.
    if (wanted !== currentWorkspaceId) {
      if (workspaces.some((w) => w.id === wanted)) await switchWorkspace(wanted);
      if (seq !== popSeq) return;
      // The switch can be refused — a change still in flight, or the reader
      // keeping their drafts — and a workspace this page does not know is
      // never attempted at all. The entry then describes a corpus the page is
      // not in, and applying its paper and filters to the one it is in would
      // be worse than ignoring it: a key that exists in both corpora would
      // open the wrong paper, which `dropMissingFilters` cannot catch.
      if (currentWorkspaceId !== wanted) return;
    } else if (workspaceSwitch) {
      // A switch is running and this entry belongs to where the page is now —
      // but `currentWorkspaceId` only changes once that switch's own flush is
      // done, so the switch may be on its way somewhere else entirely. Wait
      // for it, then ask again where it left us.
      await workspaceSwitch;
      if (seq !== popSeq) return;
      if (currentWorkspaceId !== wanted) return;
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

async function api(path, options = {}) {
  const request = { ...options };
  const headers = new Headers(request.headers || {});
  // A caller that named a workspace meant that one. An undo offered in a
  // notice can outlive the picker, and finishing it against whatever is
  // selected by then would write to the wrong corpus — the same paper
  // imported twice carries the same key and claim ids in both.
  if (!headers.has('X-Doxograph-Workspace') && currentWorkspaceId) {
    headers.set('X-Doxograph-Workspace', currentWorkspaceId);
  }
  request.headers = headers;
  const method = (request.method || 'GET').toUpperCase();
  const mutating = !['GET', 'HEAD', 'OPTIONS'].includes(method);
  if (mutating) pendingMutations += 1;
  try {
    const response = await fetch(path, request);
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json()).detail || detail; } catch (e) { /* keep statusText */ }
      throw new Error(detail);
    }
    return response.status === 204 ? null : await response.json();
  } finally {
    if (mutating) pendingMutations -= 1;
  }
}

// The corpus is fetched with the ETag of the last answer applied, so a poll
// that finds nothing changed costs the server a directory listing and costs
// the page nothing at all: `fetchState` returns null on a 304 and `S` is left
// alone. The tag is committed only once the payload has been applied to `S`,
// in `pull`. Committing it on receipt would, if the jobs request beside it
// failed, leave the page presenting a tag for a corpus it never showed and
// getting 304s against it until the next write.
let stateEtag = null;

async function fetchState() {
  const headers = new Headers();
  if (currentWorkspaceId) headers.set('X-Doxograph-Workspace', currentWorkspaceId);
  if (stateEtag) headers.set('If-None-Match', stateEtag);
  const response = await fetch('/api/state', { headers });
  if (response.status === 304) return null;
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (e) { /* keep statusText */ }
    throw new Error(detail);
  }
  return { state: await response.json(), etag: response.headers.get('ETag') };
}

async function fetchJobs() {
  const result = await api('/api/jobs');
  return result.jobs || [];
}

// Pulls the corpus and the jobs, keeping `S.jobs` across a state answer that
// did not change. Returns whether the corpus changed.
async function pull() {
  const requestedWorkspace = currentWorkspaceId;
  const [next, jobs] = await Promise.all([fetchState(), fetchJobs()]);
  if (requestedWorkspace !== currentWorkspaceId) return false;
  if (next) {
    S = { ...next.state, jobs };
    stateEtag = next.etag;
  } else {
    S.jobs = jobs;
  }
  pruneTrashed();   // a delete that has not been sent yet is already gone here
  return Boolean(next);
}

async function refresh() {
  const requestedWorkspace = currentWorkspaceId;
  const changed = await pull();
  if (requestedWorkspace !== currentWorkspaceId) return;
  if (changed) rerunTextSearch();
  render();
}

// `refresh` goes through `render`, which leaves an open editor — and therefore
// the whole claim list — alone. Anything that adds or removes a row has to
// rebuild the list even while an editor is open, or the deleted row stays on
// screen and stays clickable. This keeps the open editor's draft across it.
async function refreshAll() {
  captureOpenEditor();
  const requestedWorkspace = currentWorkspaceId;
  const changed = await pull();
  if (requestedWorkspace !== currentWorkspaceId) return;
  if (changed) rerunTextSearch();
  renderAll();
}

function currentWorkspace() {
  return workspaces.find((workspace) => workspace.id === currentWorkspaceId) || null;
}

function workspaceQuery() {
  return `workspace=${encodeURIComponent(currentWorkspaceId || 'default')}`;
}

function renderWorkspacePicker() {
  $('workspace').innerHTML = workspaces.map((workspace) =>
    `<option value="${esc(workspace.id)}">${esc(workspace.name)}</option>`).join('');
  $('workspace').value = currentWorkspaceId || 'default';
  const selected = currentWorkspace();
  document.title = selected ? `${selected.name} — Doxograph` : 'Doxograph';
  window.doxographWorkspaceId = currentWorkspaceId || 'default';
}

function resetWorkspaceView() {
  S = { papers: [], claims: [], tags: [], tag_counts: {}, ledger: [], context: '', tensions: [], agreements: [], syntheses: [],
        kinds: [], strengths: [], relations: [], jobs: [], has_key: true };
  Object.assign(V, {
    paper: null, tag: null, q: '', kind: '', unreviewed: false, unverified: false, group: true,
    editing: null, selectedId: null, newClaim: null, failedNewClaims: {}, drafts: {},
    error: null, view: 'claims', tensionStatus: '', tensionFocus: null, agreementStatus: '', agreementFocus: null,
    synthEditing: null, synthDrafts: {}, synthSaving: null, researchSaving: false, researchDraft: null, researchBase: null,
    graph: { topics: true, minShared: null, tensions: true, ledger: true },
    quoteContext: null, textSearch: null,
  });
  savingClaims.clear();
  dropTextSearch();
  graphReset();
  $('q').value = '';
  $('kind').value = '';
  $('only-unreviewed').checked = false;
  $('only-unverified').checked = false;
  $('group-by-tag').checked = true;
  closePaperMenu();
}

// Bumped by every selection, so a switch that stopped to wait for an earlier
// one can tell it has since been overtaken and stand down rather than load a
// workspace the reader has already moved on from.
let switchSeq = 0;

async function switchWorkspace(workspaceId) {
  if (workspaceId === currentWorkspaceId && !workspaceSwitch) return;
  const seq = ++switchSeq;
  // A switch spans several awaits, and the first of them sends the held
  // deletes — a flush that ends in a read, which is not counted as a change in
  // flight. The picker stays live for that stretch, so a second selection can
  // arrive mid-switch. Two loads running together write `currentWorkspaceId`
  // and `S` in whatever order their requests land, and the newer one can be
  // overwritten by the older one finishing behind it: the page settles in the
  // workspace that was superseded. So the switch already running finishes
  // first, and only the newest selection goes on from there.
  while (workspaceSwitch) {
    // A switch that failed still ends this one's wait; its own caller reports it.
    await workspaceSwitch.catch(() => {});
    // Overtaken while waiting. The selection that overtook this one owns the
    // picker from here, including putting it back if it refuses too.
    if (seq !== switchSeq) return;
  }
  // The switch that just finished may have landed where this one was headed.
  if (workspaceId === currentWorkspaceId) {
    renderWorkspacePicker();
    return;
  }
  if (pendingMutations || savingClaims.size || V.synthSaving || V.researchSaving) {
    toast('Wait for the current change to finish before switching workspaces.', { tone: 'warn' });
    renderWorkspacePicker();
    return;
  }
  const hasDraft = V.editing || V.synthEditing || V.newClaim
    || Object.keys(V.failedNewClaims).length || Object.keys(V.drafts).length
    || Object.keys(V.synthDrafts).length || researchFormDirty();
  if (hasDraft && !await confirmDialog('Switch workspaces and discard unsaved edits in this workspace?',
                                       { ok: 'Discard and switch' })) {
    renderWorkspacePicker();
    return;
  }
  // The dialog is another await the picker is live across.
  if (seq !== switchSeq) return;
  const run = loadWorkspace(workspaceId);
  workspaceSwitch = run;
  try {
    await run;
  } finally {
    if (workspaceSwitch === run) workspaceSwitch = null;
  }
}

// The half of the switch that replaces the corpus, kept separate so that
// anyone who arrives while it is running can wait for it. `currentWorkspaceId`
// changes at the start and `S` only when the answer lands, so code that reads
// both in between would be judging the new workspace by the old corpus.
async function loadWorkspace(workspaceId) {
  await flushTrash();
  currentWorkspaceId = workspaceId;
  stateEtag = null;   // the tag belongs to the other workspace's corpus
  try { localStorage.setItem('doxograph-workspace', workspaceId); } catch (e) { /* optional */ }
  resetWorkspaceView();
  renderWorkspacePicker();
  syncHash(true);   // a no-op while a popped entry is being restored
  await refresh();
  $('kind').innerHTML = '<option value="">every kind</option>'
    + S.kinds.map((kind) => `<option value="${esc(kind)}">${esc(kind)}</option>`).join('');
}

async function loadWorkspaces() {
  const response = await fetch('/api/workspaces');
  if (!response.ok) throw new Error('Could not load workspaces');
  workspaces = (await response.json()).workspaces || [];
  // `currentWorkspaceId` was taken from the address or from this browser's last
  // choice before the page could send anything anywhere. This is where it is
  // checked against the corpora that actually exist.
  const wanted = currentWorkspaceId;
  currentWorkspaceId = workspaces.some((workspace) => workspace.id === wanted) ? wanted : 'default';
  try { localStorage.setItem('doxograph-workspace', currentWorkspaceId); } catch (e) { /* optional */ }
  renderWorkspacePicker();
}

// --- filtering ------------------------------------------------------------

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
  return [row.text, row.evidence, row.quote, row.paper, row.paper_title,
          row.paper_year, (row.paper_authors || []).join(' '),
          (row.tags || []).join(' ')]
    .join(' ').toLowerCase();
}

// A paper's own text, for the query. `haystack` covers a paper through its
// claims; this covers one that has none yet, which is exactly the paper a
// title search is most likely to be looking for.
function paperHaystack(paper) {
  return [paper.title, paper.key, (paper.authors || []).join(' '), paper.year]
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
  const workspace = currentWorkspaceId;
  if (!quiet) {
    captureOpenEditor();   // the redraw rebuilds any open form from state
    V.textSearch = { q: query, loading: true, papers: [], terms: [], error: null };
    drawSearchProgress();
  }
  let next;
  try {
    const found = await api(`/api/search?q=${encodeURIComponent(query)}`);
    next = { q: query, loading: false, papers: found.papers || [], terms: found.terms || [], error: null };
  } catch (error) {
    next = { q: query, loading: false, papers: [], terms: [], error: error.message };
  }
  // An answer to a query that is no longer the one on screen is dropped: the
  // requests are not guaranteed to come back in the order they went out, and
  // the corpus can have changed under the one still running.
  if (seq !== textSearchSeq || workspace !== currentWorkspaceId) return;
  V.textSearch = next;
  captureOpenEditor();
  drawSearchProgress();
}

// The results live under the claims, so the research form never shows them —
// and redrawing it would replace a form frozen mid-save with an enabled one,
// which is how typing into the replacement gets thrown away when the save
// lands. `render` stands off the research view for the same reason.
function drawSearchProgress() {
  if (V.view !== 'research') renderContent();
}

// The papers the query matches: directly, or through a claim of theirs. The
// claim match ignores the selected paper, so narrowing to one paper does not
// empty the list you would use to leave it.
function matchingPapers() {
  if (!V.q.trim()) return S.papers;
  const matches = queryMatcher();
  const owners = new Set(S.claims
    .filter((row) => matches(haystack(row)))
    .map((row) => row.paper));
  return S.papers.filter((p) => owners.has(p.key) || matches(paperHaystack(p)));
}

function visibleClaims() {
  const matches = queryMatcher();
  return S.claims.filter((row) =>
    (!V.paper || row.paper === V.paper)
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
  // The research form is an editor too: a poll must not redraw it under the cursor.
  if (!V.editing && !V.synthEditing && V.view !== 'research') renderContent();
  renderJobs();
  syncAnalysisControls();
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
  renderContent();
  renderJobs();
  syncAnalysisControls();
  syncHash();
}

function renderStats() {
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
  else if (!S.has_key) bits.push('no API key found');
  $('stats').textContent = bits.join(' · ');
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

function renderPapers() {
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
    : `${S.claims.length} claims`;
  const all = `<li class="${claims && V.paper === null ? 'active' : ''}" data-paper="">
    <span class="pt">All papers</span>
    <span class="pm">${meta}</span></li>`;
  $('papers').innerHTML = all + sortedPapers(shown, V.paperSort).map((p) => `
    <li class="${claims && V.paper === p.key ? 'active' : ''}" data-paper="${esc(p.key)}">
      <span class="pt"><span class="dot ${esc(p.status)}"></span>${esc(p.title || p.key)}</span>
      <span class="pm">${esc((p.authors || [])[0] ? p.authors[0].split(' ').pop() : '?')}
        ${p.year ? esc(p.year) : ''} · ${p.n_claims} claims${p.n_unreviewed ? `, ${p.n_unreviewed} new` : ''}${byAdded ? ` · ${addedLabel(p)}` : ''}</span>
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

function paperHeader(key) {
  const p = S.papers.find((x) => x.key === key);
  if (!p) return '';
  const url = (p.source || {}).url;
  const title = url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(p.title || key)}</a>`
                    : esc(p.title || key);
  return `<div class="paperhead">
    <h2>${title}</h2>
    <div class="pm">${esc((p.authors || []).join(', ') || 'authors unknown')}
      ${p.year ? '· ' + esc(p.year) : ''} ${p.venue ? '· ' + esc(p.venue) : ''}
      · <code>${esc(key)}</code>
      ${p.has_pdf ? `· <a href="/pdf/${esc(key)}?${workspaceQuery()}" target="_blank" rel="noopener">PDF</a>` : '· no PDF'}</div>
    ${p.summary ? `<p class="ps">${esc(p.summary)}</p>` : ''}
    ${p.relevance ? `<p class="ps"><em>Why it is here:</em> ${esc(p.relevance)}</p>` : ''}
    <div class="row">
      <button type="button" data-act="reextract" data-ai-action ${S.ai_enabled === true ? '' : 'disabled'} data-paper="${esc(key)}">Re-read paper</button>
      <button type="button" data-act="retag-one" data-ai-action ${S.ai_enabled === true ? '' : 'disabled'} data-paper="${esc(key)}">Retag claims</button>
      ${p.has_pdf ? `<button type="button" data-act="verify" data-paper="${esc(key)}" title="Check every quote against the PDF text">Check quotes</button>` : ''}
      ${p.n_unreviewed ? `<button type="button" data-act="review-all" data-paper="${esc(key)}"
        title="Mark every claim on this paper reviewed">Mark ${p.n_unreviewed} reviewed</button>` : ''}
      <button type="button" data-act="add-claim" data-paper="${esc(key)}">Add claim by hand</button>
      <button type="button" data-act="del-paper" data-paper="${esc(key)}" style="margin-left:auto">Remove</button>
    </div>
    ${proposedPanel(key)}
  </div>`;
}

function paperCache() {
  window.__paperCache = window.__paperCache || {};
  const workspace = currentWorkspaceId || 'default';
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
        <button type="button" data-act="del" data-claim="${esc(row.id)}" data-paper="${esc(row.paper)}">delete</button>
      </span>
    </div>
    ${row.evidence ? `<p class="cev">${esc(row.evidence)}</p>` : ''}
    ${quoteHtml(row, group)}
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
      ${focus ? `<span class="hint">involving: <em>${esc(focus.text.slice(0, 80))}${focus.text.length > 80 ? '…' : ''}</em></span>
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
  await refresh();
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
    ${row.evidence ? `<p class="cev">${esc(row.evidence)}</p>` : ''}
    ${quoteHtml(row, group)}
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
      ${focus ? `<span class="hint">involving: <em>${esc(focus.text.slice(0, 80))}${focus.text.length > 80 ? '…' : ''}</em></span>
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

$('content').addEventListener('submit', async (event) => {
  if (event.target.id !== 'research-form') return;
  event.preventDefault();
  await saveResearch();
});

function showView(view) {
  if (view === V.view) return;
  if (V.view === 'research') captureResearchDraft();
  // Neither the tensions view nor the map has an editor. Park any open one
  // rather than leaving `V.editing` set on a form that is no longer on screen,
  // which would also stop background content updates. The same for a synthesis editor.
  captureOpenEditor();
  V.editing = null;
  parkSynthEditor();
  V.error = null;
  V.view = view;
  if (view !== 'tensions') V.tensionFocus = null;
  if (view !== 'agreements') V.agreementFocus = null;
}

function editForm(row) {
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
          <input name="tags" value="${esc((row.tags || []).join(' '))}" list="taglist" size="34"></div>
      </div>
      <div><label>Evidence</label><textarea name="evidence" rows="2">${esc(row.evidence || '')}</textarea></div>
      <div><label>Quote (verbatim from the paper)</label>
        <textarea name="quote" rows="2">${esc(row.quote || '')}</textarea></div>
      <div><label>Bearing on my own claims</label>
        <div class="links">${links.map(linkRow).join('')}</div></div>
      <div class="row right">
        <label class="hint"><input type="checkbox" name="reviewed" ${row.reviewed ? 'checked' : ''}> reviewed</label>
        <button type="button" data-act="cancel">Cancel</button>
        <button type="submit" class="primary">Save</button>
      </div>
    </form>
    <datalist id="taglist">${S.tags.map((t) => `<option value="${esc(t.name)}">`).join('')}</datalist>
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
        <button type="button" data-act="synthesize" data-ai-action ${S.ai_enabled === true ? '' : 'disabled'} data-topic="${esc(tag)}">Rewrite</button>
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

async function synthesize(topics) {
  V.error = null;
  const workspace = await settleDeletes();
  if (!workspace) return;   // the delete failed; the pass would read the claim
  try {
    const result = await api('/api/syntheses', {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...workspace },
      body: JSON.stringify(topics ? { topics } : {}),
    });
    if (!result.queued) {
      V.error = topics
        ? 'That topic has no claims to synthesize.'
        : 'No topic has claims from two papers yet. Use the synthesize button on a topic to write one anyway.';
    }
  } catch (error) {
    V.error = `Could not start the synthesis: ${error.message}`;
  }
  await refresh();
  if (V.error) renderContent();
}

// Every path through the draw settles `V` first — the fallback selection, the
// parked editors — so the URL is written once the drawing is done rather than
// once per branch.
function renderContent() {
  drawContent();
  syncHash();
}

function drawContent() {
  if (V.view === 'graph') { renderGraph(); return; }
  graphStop();   // leaving the map, or never on it: no animation loop off screen
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
  if (!rows.some((row) => row.id === V.selectedId)) V.selectedId = rows.length ? rows[0].id : null;
  let html = V.paper ? paperHeader(V.paper) : '';
  if (V.error) html += `<p class="warn">${esc(V.error)}</p>`;
  // Filtered to one topic without grouping, the synthesis heads the list; in
  // grouped mode each topic's sits under its own heading below.
  if (V.tag && !V.paper && !(V.group && rows.length)) {
    html += `<div class="group"><h3>${esc(V.tag)} ${synthesizeButton(V.tag)}</h3>${synthesisBlock(V.tag)}</div>`;
  }
  if (V.newClaim && V.editing === NEW_CLAIM_ID) {
    shown.add(NEW_CLAIM_ID);
    html += editForm(V.newClaim);
  } else if (V.newClaim && (V.newClaim.text || '').trim()) {
    // Held but not open — opening another claim's editor moved `V.editing`.
    // Without this the draft is unreachable: Add claim would overwrite it and
    // cancelling the other editor would drop it, losing the text silently.
    html += `<div class="proposed">
      <span>Unsaved new claim: <em>${esc(V.newClaim.text.slice(0, 80))}${V.newClaim.text.length > 80 ? '…' : ''}</em></span>
      <span style="margin-left:auto"></span>
      <button type="button" data-act="resume-new">Resume</button>
      <button type="button" data-act="discard-new">Discard</button>
    </div>`;
  }

  if (!rows.length) {
    if (!V.newClaim) {
      html += S.papers.length
        ? '<p class="empty">No claims match these filters.</p>'
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
    html += rows.map(card).join('');
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

function renderJobs() {
  const active = (S.jobs || []).filter((j) => j.state !== 'done' || j.detail);
  $('jobs').innerHTML = active.slice(0, 6).map((j) => `
    <div class="job ${j.state === 'error' ? 'error' : ''}">
      <span class="lbl">${esc(j.label)}</span>
      <span class="st">${esc(j.state)}${j.detail ? ': ' + esc(j.detail) : ''}</span>
      ${['done', 'error'].includes(j.state) ? `<button type="button" class="dismiss-job" data-job-id="${j.id}" aria-label="Dismiss notification for ${esc(j.label)}" title="Dismiss notification">×</button>` : ''}
    </div>`).join('');
}

$('jobs').addEventListener('click', async (event) => {
  const button = event.target.closest('.dismiss-job');
  if (!button || button.disabled) return;
  button.disabled = true;
  try {
    await api(`/api/jobs/${button.dataset.jobId}`, { method: 'DELETE' });
    await refresh();
  } catch (error) {
    button.disabled = false;
    toast(`Could not dismiss notification: ${error.message}`, { tone: 'warn' });
  }
});

// --- the map: papers as nodes ----------------------------------------------

// A force layout on a canvas, with no library behind it. Nodes are papers;
// squares are the ledger's own claims. Three kinds of edge: two papers whose
// claims share a topic, a tension between two papers, and a paper bearing on
// one of my claims. Everything is computed here from `S`; the server knows
// nothing about the map.
//
// Layout state lives outside `V`. Positions are kept across redraws so the
// poll noticing a change does not reshuffle the map, but they are not a view
// choice, and they are dropped with the rest of a workspace's state.
const GRAPH = {
  nodes: new Map(),        // id -> node
  edges: [],
  alpha: 0,                // simulation temperature; 0 means at rest
  frame: 0,                // requestAnimationFrame handle, 0 when stopped
  hover: null, drag: null, pan: null, moved: false,
  zoom: 1, tx: 0, ty: 0,   // world -> screen: centre + (world * zoom) + (tx, ty)
  autofit: true,           // keep everything in view until the user takes over
  canvas: null, ctx: null, observer: null, signature: '', maxShared: 1, minShared: 1,
};

function graphReset() {
  graphStop();
  GRAPH.nodes.clear();
  GRAPH.edges = [];
  GRAPH.hover = GRAPH.drag = GRAPH.pan = null;
  GRAPH.zoom = 1; GRAPH.tx = GRAPH.ty = 0; GRAPH.autofit = true;
  GRAPH.canvas = GRAPH.ctx = null;
  GRAPH.signature = '';
  if (GRAPH.observer) { GRAPH.observer.disconnect(); GRAPH.observer = null; }
}

function graphStop() {
  if (GRAPH.frame) cancelAnimationFrame(GRAPH.frame);
  GRAPH.frame = 0;
  GRAPH.alpha = 0;
  if (GRAPH.observer) { GRAPH.observer.disconnect(); GRAPH.observer = null; }
  GRAPH.canvas = GRAPH.ctx = null;
}

function renderGraphNav() {
  const papers = S.papers.length;
  $('graph-nav').innerHTML = `<li class="${V.view === 'graph' ? 'active' : ''}" data-view="graph">
    <span class="pt">How the papers connect</span>
    <span class="pm">${papers ? `${papers} papers as a map` : 'nothing to map yet'}</span></li>`;
}

function graphCite(p) {
  const who = (p.authors || [])[0] ? p.authors[0].split(' ').pop() : p.key;
  return `${who} ${p.year || ''}`.trim();
}

// The nodes and edges the current options call for, computed fresh from `S`.
// `maxShared` is the heaviest topic edge before the threshold is applied, so
// the slider's range can follow the corpus.
function graphData() {
  const opts = V.graph;
  const byPaper = new Map(S.papers.map((p) => [p.key, p]));
  const tagCount = new Map();     // paper -> Map(tag -> claims with it)
  const tagPapers = new Map();    // tag -> Set(paper)
  for (const c of S.claims) {
    if (!byPaper.has(c.paper)) continue;
    for (const t of c.tags || []) {
      if (!tagCount.has(c.paper)) tagCount.set(c.paper, new Map());
      const counts = tagCount.get(c.paper);
      counts.set(t, (counts.get(t) || 0) + 1);
      if (!tagPapers.has(t)) tagPapers.set(t, new Set());
      tagPapers.get(t).add(c.paper);
    }
  }
  const nodes = [];
  for (const p of S.papers) {
    const counts = tagCount.get(p.key) || new Map();
    const ranked = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    nodes.push({
      id: `p:${p.key}`, type: 'paper', key: p.key, label: p.title || p.key, cite: graphCite(p),
      n: p.n_claims || 0, topic: ranked.length ? ranked[0][0] : null, tags: new Set(counts.keys()),
      r: 6 + 3 * Math.sqrt(p.n_claims || 0),
    });
  }
  const edges = [];
  let maxShared = 1;
  let minShared = opts.minShared || 1;
  // The weights are worked out whether or not topic edges are drawn, so the
  // slider keeps its range while the layer is off and the threshold survives
  // turning it back on.
  {
    const pairs = new Map();
    for (const [tag, set] of tagPapers) {
      const list = [...set].sort();
      for (let i = 0; i < list.length; i += 1) {
        for (let j = i + 1; j < list.length; j += 1) {
          const key = `${list[i]}|${list[j]}`;
          if (!pairs.has(key)) pairs.set(key, { type: 'topic', a: `p:${list[i]}`, b: `p:${list[j]}`, w: 0, tags: [] });
          const pair = pairs.get(key);
          pair.w += Math.min(tagCount.get(list[i]).get(tag), tagCount.get(list[j]).get(tag));
          pair.tags.push(tag);
        }
      }
    }
    // With no threshold chosen, the median weight: a corpus on one subject
    // shares something between nearly every pair, and drawing all of it hides
    // the structure. The slider shows the value picked.
    const weights = [...pairs.values()].map((e) => e.w).sort((a, b) => a - b);
    maxShared = weights.length ? Math.max(1, weights[weights.length - 1]) : 1;
    minShared = opts.minShared || (weights.length ? weights[Math.floor(weights.length / 2)] : 1);
    // A threshold chosen for a bigger corpus can exceed every weight here,
    // which would hide every topic link while the label promised otherwise.
    // The clamp is written back to the choice, so what the slider shows is
    // what later recomputations start from, rather than a value it once had.
    minShared = Math.min(minShared, maxShared);
    if (opts.minShared && opts.minShared > maxShared) opts.minShared = maxShared;
    if (opts.topics) for (const pair of pairs.values()) if (pair.w >= minShared) edges.push(pair);
  }
  if (opts.tensions) {
    const pairs = new Map();
    for (const t of S.tensions || []) {
      if (t.status === 'dismissed' || !t.claims || t.claims.length !== 2) continue;
      const [a, b] = [t.claims[0].paper, t.claims[1].paper].sort();
      if (a === b || !byPaper.has(a) || !byPaper.has(b)) continue;
      const key = `${a}|${b}`;
      if (!pairs.has(key)) pairs.set(key, { type: 'tension', a: `p:${a}`, b: `p:${b}`, n: 0, open: 0, kinds: new Set() });
      const pair = pairs.get(key);
      pair.n += 1;
      if (t.status === 'open') pair.open += 1;
      pair.kinds.add(t.kind);
    }
    // A tension is found within a shared topic, so the topic edge between the
    // same two papers says nothing the tension does not, and drawn together
    // the solid topic stroke would show through the dashes of an open one.
    // Dropped here rather than at draw time, so the link count agrees with
    // what is on screen.
    for (let i = edges.length - 1; i >= 0; i -= 1) {
      if (edges[i].type === 'topic' && pairs.has(`${edges[i].a.slice(2)}|${edges[i].b.slice(2)}`)) edges.splice(i, 1);
    }
    edges.push(...pairs.values());
  }
  if (opts.ledger) {
    const own = new Map((S.ledger || []).map((c) => [c.id, c]));
    const pairs = new Map();
    const used = new Set();
    for (const c of S.claims) {
      if (!byPaper.has(c.paper)) continue;
      for (const link of c.ledger_links || []) {
        if (!own.has(link.claim)) continue;
        // `independent` records that a paper does not bear on the claim; a
        // line would say the opposite, so it is left off the map.
        if (link.relation === 'independent') continue;
        used.add(link.claim);
        const key = `${c.paper}|${link.claim}|${link.relation}`;
        if (!pairs.has(key)) pairs.set(key, { type: 'ledger', a: `p:${c.paper}`, b: `l:${link.claim}`, relation: link.relation, n: 0 });
        pairs.get(key).n += 1;
      }
    }
    for (const id of [...used].sort()) {
      nodes.push({ id: `l:${id}`, type: 'claim', key: id, label: own.get(id).text || id, cite: id, n: 0, r: 7, tags: new Set() });
    }
    edges.push(...pairs.values());
  }
  return { nodes, edges, maxShared, minShared };
}

// Merge fresh data into the layout: nodes that were already placed keep their
// position, a new node lands beside a neighbour it is joined to or on a ring
// around the middle, and nodes that are gone are dropped. Returns whether the
// set of nodes or edges changed, which is what warrants reheating.
function graphSync() {
  const { nodes, edges, maxShared, minShared } = graphData();
  const signature = JSON.stringify([nodes.map((n) => [n.id, n.n, n.topic]),
    edges.map((e) => [e.type, e.a, e.b, e.w, e.n, e.open, e.relation])]);
  const changed = signature !== GRAPH.signature;
  GRAPH.signature = signature;
  const keep = new Set(nodes.map((n) => n.id));
  for (const id of [...GRAPH.nodes.keys()]) if (!keep.has(id)) GRAPH.nodes.delete(id);
  const spread = 40 + 22 * Math.sqrt(nodes.length);
  nodes.forEach((fresh, i) => {
    const had = GRAPH.nodes.get(fresh.id);
    if (had) { Object.assign(had, fresh); return; }
    const near = edges.map((e) => (e.a === fresh.id ? e.b : e.b === fresh.id ? e.a : null))
      .map((id) => id && GRAPH.nodes.get(id)).find((n) => n && Number.isFinite(n.x));
    const angle = (i * 2.399963) % (2 * Math.PI);   // golden angle: spaced, not clumped
    const x = near ? near.x + 30 * Math.cos(angle) : spread * Math.cos(angle) * (0.4 + 0.6 * Math.random());
    const y = near ? near.y + 30 * Math.sin(angle) : spread * Math.sin(angle) * (0.4 + 0.6 * Math.random());
    GRAPH.nodes.set(fresh.id, { ...fresh, x, y, vx: 0, vy: 0, fixed: false });
  });
  GRAPH.edges = edges.map((e) => ({ ...e, source: GRAPH.nodes.get(e.a), target: GRAPH.nodes.get(e.b) }))
    .filter((e) => e.source && e.target);
  GRAPH.maxShared = maxShared;
  GRAPH.minShared = minShared;
  if (GRAPH.hover && !GRAPH.nodes.has(GRAPH.hover.id)) GRAPH.hover = null;
  return changed;
}

// One step of the simulation: pairwise repulsion, springs along edges, a pull
// to the middle, then damped integration. Forces scale with `alpha`, which
// cools each step; the loop stops once it is near zero.
function graphTick() {
  const nodes = [...GRAPH.nodes.values()];
  const alpha = GRAPH.alpha;
  for (let i = 0; i < nodes.length; i += 1) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j += 1) {
      const b = nodes[j];
      let dx = b.x - a.x, dy = b.y - a.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 1) { dx = (Math.random() - 0.5); dy = (Math.random() - 0.5); d2 = 1; }
      if (d2 > 250000) continue;   // beyond 500 units nothing pushes, so a loner is not flung off
      const f = (900 * alpha) / d2;
      a.vx -= dx * f; a.vy -= dy * f;
      b.vx += dx * f; b.vy += dy * f;
    }
  }
  for (const e of GRAPH.edges) {
    const { source: a, target: b } = e;
    const dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
    const rest = e.type === 'topic' ? Math.max(70, 150 - 10 * Math.min(e.w, 8)) : e.type === 'tension' ? 110 : 90;
    const k = e.type === 'topic' ? 0.04 : 0.08;
    const f = ((d - rest) / d) * k * alpha;
    a.vx += dx * f; a.vy += dy * f;
    b.vx -= dx * f; b.vy -= dy * f;
  }
  for (const n of nodes) {
    // My own claims hang off one or two papers each; a stronger pull keeps
    // them among the papers rather than orbiting the whole map.
    const pull = n.type === 'claim' ? 0.05 : 0.015;
    n.vx -= n.x * pull * alpha;
    n.vy -= n.y * pull * alpha;
    n.vx *= 0.6; n.vy *= 0.6;
    if (!n.fixed) { n.x += n.vx; n.y += n.vy; }
  }
  GRAPH.alpha = alpha * 0.975;
  if (GRAPH.alpha < 0.003) GRAPH.alpha = 0;
}

function graphColors() {
  const style = getComputedStyle(document.documentElement);
  const read = (name) => style.getPropertyValue(`--${name}`).trim();
  return { ink: read('ink'), muted: read('muted'), line: read('line'), bg: read('bg'), panel: read('panel'),
           accent: read('accent'), warn: read('warn'), ok: read('ok'),
           dark: document.documentElement.style.colorScheme !== 'light' };
}

// One hue per topic, from a hash of its name, so a topic keeps its colour
// however the vocabulary around it changes; a paper changes colour only when
// its dominant topic does.
function topicColor(topic, dark) {
  if (!topic) return dark ? '#7a8090' : '#9aa0ab';
  let h = 2166136261;
  for (const ch of topic) { h ^= ch.codePointAt(0); h = Math.imul(h, 16777619) >>> 0; }
  const hue = h % 360;
  return dark ? `hsl(${hue} 50% 62%)` : `hsl(${hue} 55% 46%)`;
}

function graphRelationColor(relation, colors) {
  if (relation === 'contradicts') return colors.warn;
  if (relation === 'supports') return colors.ok;
  return colors.accent;
}

// Zoom and offset that show every node with a margin. Applied on each frame
// until the user zooms or pans, at which point the view is theirs.
function graphAutofit(w, h) {
  const nodes = [...GRAPH.nodes.values()];
  if (!nodes.length) return;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of nodes) {
    x0 = Math.min(x0, n.x - n.r); y0 = Math.min(y0, n.y - n.r);
    x1 = Math.max(x1, n.x + n.r); y1 = Math.max(y1, n.y + n.r);
  }
  const pad = 48;
  const zoom = Math.min(2, (w - 2 * pad) / Math.max(x1 - x0, 1), (h - 2 * pad) / Math.max(y1 - y0, 1));
  GRAPH.zoom = Math.max(0.2, zoom);
  GRAPH.tx = -((x0 + x1) / 2) * GRAPH.zoom;
  GRAPH.ty = -((y0 + y1) / 2) * GRAPH.zoom;
}

function graphDraw() {
  const { canvas, ctx } = GRAPH;
  if (!canvas || !ctx) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.width / dpr, h = canvas.height / dpr;
  if (GRAPH.autofit) graphAutofit(w, h);
  const colors = graphColors();
  ctx.save();
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.translate(w / 2 + GRAPH.tx, h / 2 + GRAPH.ty);
  ctx.scale(GRAPH.zoom, GRAPH.zoom);
  const dim = (n) => V.tag && n.type === 'paper' && !n.tags.has(V.tag);
  const hover = GRAPH.hover;
  const linked = new Set();
  if (hover) GRAPH.edges.forEach((e) => { if (e.source === hover) linked.add(e.target); if (e.target === hover) linked.add(e.source); });

  // Several ledger relations between one paper and one claim would be drawn
  // along one line, the later hiding the earlier; they are fanned out side by
  // side. (A topic edge under a tension is dropped in graphData instead.)
  const pairKey = (e) => `${e.a}|${e.b}`;
  const fan = new Map();
  for (const e of GRAPH.edges) {
    if (e.type !== 'ledger') continue;
    const key = pairKey(e);
    if (!fan.has(key)) fan.set(key, []);
    fan.get(key).push(e);
  }
  for (const e of GRAPH.edges) {
    const { source: a, target: b } = e;
    const touching = hover && (a === hover || b === hover);
    const faded = (hover && !touching) || dim(a) || dim(b);
    ctx.globalAlpha = faded ? 0.12 : 1;
    ctx.setLineDash([]);
    let ox = 0, oy = 0;
    if (e.type === 'ledger') {
      const siblings = fan.get(pairKey(e));
      if (siblings.length > 1) {
        const at = siblings.indexOf(e) - (siblings.length - 1) / 2;
        const d = Math.max(Math.hypot(b.x - a.x, b.y - a.y), 1);
        ox = (-(b.y - a.y) / d) * 4 * at; oy = ((b.x - a.x) / d) * 4 * at;
      }
    }
    if (e.type === 'topic') {
      ctx.strokeStyle = colors.muted;
      ctx.globalAlpha *= 0.25 + 0.09 * Math.min(e.w, 8);
      ctx.lineWidth = 0.8 + 0.25 * Math.min(e.w, 8);
    } else if (e.type === 'tension') {
      ctx.strokeStyle = colors.warn;
      ctx.lineWidth = 1.6 + 0.4 * Math.min(e.n, 4);
      if (e.open) ctx.setLineDash([6, 4]);
    } else {
      ctx.strokeStyle = graphRelationColor(e.relation, colors);
      ctx.lineWidth = 1.4;
      if (e.relation === 'contradicts') ctx.setLineDash([2, 3]);
    }
    ctx.beginPath(); ctx.moveTo(a.x + ox, a.y + oy); ctx.lineTo(b.x + ox, b.y + oy); ctx.stroke();
  }
  ctx.setLineDash([]);

  const showLabels = GRAPH.nodes.size <= 60;
  ctx.font = `${11 / GRAPH.zoom}px system-ui, sans-serif`;   // screen-constant label size
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (const n of GRAPH.nodes.values()) {
    const faded = (hover && n !== hover && !linked.has(n)) || dim(n);
    ctx.globalAlpha = faded ? 0.25 : 1;
    if (n.type === 'paper') {
      ctx.fillStyle = topicColor(n.topic, colors.dark);
      ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, 2 * Math.PI); ctx.fill();
      if (n === hover || (V.paper === n.key && !faded)) {
        ctx.strokeStyle = colors.ink; ctx.lineWidth = 2; ctx.stroke();
      }
    } else {
      ctx.fillStyle = colors.bg;
      ctx.strokeStyle = colors.ink;
      ctx.lineWidth = n === hover ? 2 : 1.2;
      ctx.beginPath(); ctx.rect(n.x - n.r, n.y - n.r, 2 * n.r, 2 * n.r); ctx.fill(); ctx.stroke();
    }
    if (showLabels || n === hover || linked.has(n)) {
      ctx.fillStyle = faded ? colors.muted : colors.ink;
      const text = n.type === 'paper' ? n.cite : n.key;
      ctx.fillText(text, n.x, n.y + n.r + 3);
    }
  }
  ctx.restore();
}

function graphLoop() {
  GRAPH.frame = 0;
  if (!GRAPH.canvas) return;
  if (GRAPH.alpha > 0) graphTick();
  graphDraw();
  if (GRAPH.alpha > 0 || GRAPH.drag) GRAPH.frame = requestAnimationFrame(graphLoop);
}

function graphHeat(alpha) {
  GRAPH.alpha = Math.max(GRAPH.alpha, alpha);
  if (!GRAPH.frame && GRAPH.canvas) GRAPH.frame = requestAnimationFrame(graphLoop);
}

// Screen -> world, in the canvas's CSS pixel space.
function graphWorld(event) {
  const rect = GRAPH.canvas.getBoundingClientRect();
  const sx = event.clientX - rect.left, sy = event.clientY - rect.top;
  return { x: (sx - rect.width / 2 - GRAPH.tx) / GRAPH.zoom, y: (sy - rect.height / 2 - GRAPH.ty) / GRAPH.zoom, sx, sy };
}

function graphNodeAt(x, y) {
  let best = null, bestD = Infinity;
  for (const n of GRAPH.nodes.values()) {
    const d = Math.hypot(n.x - x, n.y - y);
    const reach = n.r + 4 / GRAPH.zoom;
    if (d <= reach && d < bestD) { best = n; bestD = d; }
  }
  return best;
}

function graphTip(node, sx, sy) {
  const tip = $('content').querySelector('.graph-tip');
  if (!tip) return;
  if (!node) { tip.hidden = true; return; }
  if (node.type === 'paper') {
    const p = S.papers.find((x) => x.key === node.key) || {};
    const topics = [...node.tags].sort().slice(0, 6).map((t) => `#${t}`).join(' ');
    tip.innerHTML = `<div>${esc(node.label)}</div>
      <div class="pm">${esc((p.authors || []).join(', ') || 'authors unknown')}${p.year ? ' · ' + esc(p.year) : ''}
        · ${node.n} claims${topics ? ' · ' + esc(topics) : ''}</div>`;
  } else {
    tip.innerHTML = `<div><span class="kind">my claim</span> ${esc(node.label)}</div><div class="pm"><code>${esc(node.key)}</code></div>`;
  }
  tip.hidden = false;
  const wrap = tip.parentElement.getBoundingClientRect();
  const { width, height } = tip.getBoundingClientRect();
  tip.style.left = `${Math.max(0, Math.min(sx + 14, wrap.width - width - 4))}px`;
  tip.style.top = `${Math.max(0, Math.min(sy + 14, wrap.height - height - 4))}px`;
}

function graphOpenPaper(key) {
  captureOpenEditor();
  closeEditorsNotBelongingTo(key);
  showView('claims');
  V.paper = key; V.tag = null; V.selectedId = null;
  syncHash(true);   // leaving the map is navigation: Back returns to it
  renderAll();
}

function graphBindCanvas(canvas) {
  canvas.addEventListener('mousedown', (event) => {
    if (event.button !== 0) return;
    const { x, y, sx, sy } = graphWorld(event);
    const node = graphNodeAt(x, y);
    GRAPH.moved = false;
    if (node) {
      GRAPH.drag = { node, dx: node.x - x, dy: node.y - y };
      node.fixed = true;
      GRAPH.autofit = false;   // or each frame refits the map under the pointer
      graphHeat(0.3);
    } else {
      GRAPH.pan = { sx, sy, tx: GRAPH.tx, ty: GRAPH.ty };
      GRAPH.autofit = false;
    }
    canvas.classList.add('drag');
    event.preventDefault();
  });
  canvas.addEventListener('mousemove', (event) => {
    const { x, y, sx, sy } = graphWorld(event);
    if (GRAPH.drag) {
      const n = GRAPH.drag.node;
      n.x = x + GRAPH.drag.dx; n.y = y + GRAPH.drag.dy;
      n.vx = n.vy = 0;
      GRAPH.moved = true;
      graphHeat(0.3);
      graphTip(null);
      return;
    }
    if (GRAPH.pan) {
      GRAPH.tx = GRAPH.pan.tx + (sx - GRAPH.pan.sx);
      GRAPH.ty = GRAPH.pan.ty + (sy - GRAPH.pan.sy);
      GRAPH.moved = true;
      graphDraw();
      return;
    }
    const node = graphNodeAt(x, y);
    if (node !== GRAPH.hover) {
      GRAPH.hover = node;
      canvas.classList.toggle('hover', !!node);
      graphDraw();
    }
    graphTip(node, sx, sy);
  });
  const release = () => {
    if (GRAPH.drag) { GRAPH.drag.node.fixed = false; GRAPH.drag = null; graphHeat(0.1); }
    GRAPH.pan = null;
    canvas.classList.remove('drag');
  };
  canvas.addEventListener('mouseup', release);
  canvas.addEventListener('mouseleave', () => {
    release();
    if (GRAPH.hover) { GRAPH.hover = null; canvas.classList.remove('hover'); graphDraw(); }
    graphTip(null);
  });
  canvas.addEventListener('click', (event) => {
    if (GRAPH.moved) return;   // a drag that ended on the node is not a click
    const { x, y } = graphWorld(event);
    const node = graphNodeAt(x, y);
    if (node && node.type === 'paper') graphOpenPaper(node.key);
  });
  canvas.addEventListener('wheel', (event) => {
    event.preventDefault();
    const { sx, sy } = graphWorld(event);
    const rect = canvas.getBoundingClientRect();
    const factor = Math.exp(-event.deltaY * 0.0015);
    const next = Math.min(6, Math.max(0.2, GRAPH.zoom * factor));
    GRAPH.autofit = false;
    // Zoom about the cursor: the world point under it stays put.
    const cx = sx - rect.width / 2, cy = sy - rect.height / 2;
    GRAPH.tx = cx - (cx - GRAPH.tx) * (next / GRAPH.zoom);
    GRAPH.ty = cy - (cy - GRAPH.ty) * (next / GRAPH.zoom);
    GRAPH.zoom = next;
    graphDraw();
  }, { passive: false });
}

function graphFit() {
  const wrap = $('content').querySelector('.graph-wrap');
  const canvas = GRAPH.canvas;
  if (!wrap || !canvas) return;
  const main = $('main');
  // Fill what is left of the pane below the header, so the map is on screen
  // whole rather than scrolling; never so short that it is useless.
  const top = wrap.getBoundingClientRect().top - main.getBoundingClientRect().top;
  const height = Math.max(288, main.clientHeight - top - 16);
  wrap.style.height = `${height}px`;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(wrap.clientWidth * dpr);
  canvas.height = Math.round(height * dpr);
  graphDraw();
}

function graphHeader() {
  const opts = V.graph;
  return `<div class="paperhead">
    <h2>How the papers connect</h2>
    <p class="ps">Each circle is a paper, sized by how many claims it makes and coloured by the
      topic it speaks to most. Squares are your own claims. Drag a node to move it, scroll to
      zoom, drag the background to pan, and click a paper to read it.</p>
    <div class="graph-legend">
      <span><i></i>claims share a topic</span>
      <span><i class="tension"></i>papers disagree (dashed while open)</span>
      <span><i class="supports"></i>supports my claim</span>
      <span><i class="contradicts"></i>contradicts it</span>
      <span><i class="other"></i>refines it or supplies a method</span>
      <span>(links marked independent are not drawn)</span>
    </div>
    <div class="row graph-controls">
      <label><input type="checkbox" data-graph-opt="topics" ${opts.topics ? 'checked' : ''}> shared topics</label>
      <label title="Hide topic edges thinner than this">at least
        <input type="range" min="1" max="${Math.max(1, GRAPH.maxShared || 1)}" value="${GRAPH.minShared || 1}" data-graph-opt="minShared">
        <span data-graph-min>${GRAPH.minShared || 1}</span> shared</label>
      <label><input type="checkbox" data-graph-opt="tensions" ${opts.tensions ? 'checked' : ''}> tensions</label>
      <label><input type="checkbox" data-graph-opt="ledger" ${opts.ledger ? 'checked' : ''}> my claims</label>
      <span class="hint" data-graph-count style="margin-left:auto"></span>
    </div>
  </div>`;
}

function graphStatus() {
  const count = $('content').querySelector('[data-graph-count]');
  if (!count) return;
  const papers = [...GRAPH.nodes.values()].filter((n) => n.type === 'paper').length;
  const bits = [`${papers} papers`, `${GRAPH.edges.length} links`];
  if (V.tag) bits.push(`highlighting #${V.tag}`);
  count.textContent = bits.join(' · ');
  const range = $('content').querySelector('[data-graph-opt="minShared"]');
  if (range) {
    range.max = String(Math.max(1, GRAPH.maxShared || 1));
    if (!V.graph.minShared) range.value = String(GRAPH.minShared || 1);
  }
  const label = $('content').querySelector('[data-graph-min]');
  if (label) label.textContent = String(GRAPH.minShared || 1);
}

// Drawn in two steps so the poll can update the map in place. The first call
// builds the header, controls and canvas; later calls only refresh the data,
// keeping the layout, zoom, and whatever the pointer is doing.
function renderGraph() {
  const content = $('content');
  let wrap = content.querySelector('.graph-wrap');
  if (!wrap || !GRAPH.canvas) {
    graphStop();
    GRAPH.signature = '';
    graphSync();
    content.innerHTML = graphHeader()
      + (S.papers.length ? '' : '<p class="empty">Nothing to map yet. Add a paper or two and come back.</p>')
      + '<div class="graph-wrap"><canvas></canvas><div class="graph-tip" hidden></div></div>';
    wrap = content.querySelector('.graph-wrap');
    GRAPH.canvas = wrap.querySelector('canvas');
    GRAPH.ctx = GRAPH.canvas.getContext('2d');
    graphBindCanvas(GRAPH.canvas);
    // Watch the pane, not the wrapper: graphFit sets the wrapper's height
    // itself, so a vertical-only window resize changes the pane and leaves the
    // wrapper's box as it was, which would never fire an observer on it.
    GRAPH.observer = new ResizeObserver(() => graphFit());
    GRAPH.observer.observe($('main'));
    GRAPH.autofit = true;
    graphFit();
    graphHeat(1);
  } else if (graphSync()) {
    graphHeat(0.4);
  } else {
    graphDraw();
  }
  graphStatus();
}

$('graph-nav').addEventListener('click', (event) => {
  if (!event.target.closest('[data-view]')) return;
  showView('graph');
  syncHash(true);
  renderAll();
});

// For the browser tests and anyone poking at the console: where things are.
window.doxographGraph = () => ({
  nodes: [...GRAPH.nodes.values()].map((n) => ({ id: n.id, type: n.type, key: n.key, x: n.x, y: n.y, r: n.r, topic: n.topic })),
  edges: GRAPH.edges.map((e) => ({ type: e.type, a: e.a, b: e.b, w: e.w, n: e.n, relation: e.relation })),
  zoom: GRAPH.zoom, tx: GRAPH.tx, ty: GRAPH.ty, alpha: GRAPH.alpha,
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

function parkNewClaimForNavigation(paper) {
  // Ordinary unsaved drafts are intentionally abandoned when leaving their
  // paper. A failed submission is different: the user already clicked Save,
  // so keep it outside the active-paper slot and restore it when they return.
  if (V.newClaim && V.newClaim.paper !== paper) {
    if (V.newClaim.saveFailed) {
      V.failedNewClaims[V.newClaim.paper] = V.newClaim;
    }
    V.newClaim = null;
    if (V.editing === NEW_CLAIM_ID) V.editing = null;
  }
  if (!V.newClaim && paper && V.failedNewClaims[paper]) {
    V.newClaim = V.failedNewClaims[paper];
    delete V.failedNewClaims[paper];
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
  const workspace = currentWorkspaceId;
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
    const mine = currentWorkspaceId === workspace;
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
      if (currentWorkspaceId === workspace) syncDraftReviews(changed, false);
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
  await refresh();
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
    tags: value('tags').split(/[\s,]+/).map((t) => t.trim().toLowerCase()).filter(Boolean),
    ledger_links: links,
    reviewed: form.querySelector('[name="reviewed"]').checked,
  };
}

$('content').addEventListener('submit', async (event) => {
  const form = event.target.closest('form[data-form]');
  if (!form) return;
  event.preventDefault();
  const wrap = form.closest('[data-claim]');
  if (isSaving(wrap.dataset.claim)) return;   // a request for it is in flight
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
      if ((!V.paper || V.paper === paper) &&
          (!V.newClaim || V.newClaim.paper === paper)) {
        V.newClaim = failedDraft;
      } else {
        V.failedNewClaims[paper] = failedDraft;
      }
      V.error = (!V.paper || V.paper === paper)
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
    delete V.failedNewClaims[paper];
    await refreshAll();          // a row appeared, so the list has to be rebuilt
    return;
  }

  V.editing = null;
  try {
    await patchClaim(wrap.dataset.paper, wrap.dataset.claim, patch);
    delete V.drafts[wrap.dataset.claim];
    // `render` leaves the content alone while a synthesis editor is open, so
    // the saved claim's form would stay on screen with nothing tracking it.
    // The synthesis draft is in V.synthDrafts, kept current by the input
    // listener, so redrawing here loses nothing; a claim editor opened while
    // the request ran has already redrawn the form away, so leave it be.
    if (V.synthEditing && !V.editing) renderContent();
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
    const onScreen = visibleClaims().some((row) => row.id === id);
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
  const go = await confirmDialog(`Remove ${p ? p.title || paper : paper} and its claims?`, {
    note: 'The PDF and every claim on it go with it, and the key is never reissued.',
    ok: 'Remove',
  });
  if (!go) return;
  // A claim of this paper still waiting out its notice has to go first. Left
  // waiting, its Undo would have nothing to restore and its request would
  // arrive under a paper that no longer exists, failing in the reader's face
  // over a deletion they got what they asked for. Only this paper's, though:
  // a claim of another paper, or a synthesis, was promised its own eight
  // seconds and removing something else is no reason to take them away.
  //
  // That flush ends in a read, and nothing counts a read as a change in
  // flight, so the picker can move in the gap before the paper's own request
  // goes. It names the workspace it was asked in: the same paper imported into
  // two corpora has the same key in both, and removing the wrong one is not
  // something an undo could fix.
  const headers = await settleDeletes((entry) => entry.paper === paper);
  // That claim's delete failed. Removing the paper would take it anyway, under
  // a notice that said the claim could not be deleted — so nothing happens.
  if (!headers) return;
  const workspace = headers['X-Doxograph-Workspace'];
  await api(`/api/papers/${encodeURIComponent(paper)}`, { method: 'DELETE', headers });
  // Moved on meanwhile: the view belongs to another corpus now, and the drafts
  // and selection this would tidy up went with `resetWorkspaceView`.
  if (currentWorkspaceId !== workspace) {
    await refreshAll();
    return;
  }
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
  delete V.failedNewClaims[paper];
  // Removing a paper from the list while reading another one keeps that one
  // open; only a paper that was itself on screen falls back to "All papers".
  if (V.paper === paper) V.paper = null;
  const selected = S.claims.find((c) => c.id === V.selectedId);
  if (!selected || selected.paper === paper) V.selectedId = null;
  V.error = null;
  await refreshAll();
}

$('content').addEventListener('click', async (event) => {
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
      await reviewWholePaper(paper);
      return;
    }
    if (act === 'del') {
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
    if (act === 'open-paper') {
      captureOpenEditor();
      closeEditorsNotBelongingTo(paper);
      showView('claims');
      V.paper = paper; V.tag = null; V.selectedId = null;
      syncHash(true);
      renderAll();
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
      await refreshAll();
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
      try {
        await api(`/api/agreements/${encodeURIComponent(button.dataset.agreement)}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: button.dataset.status }),
        });
      } catch (error) {
        V.error = `Could not update the agreement: ${error.message}`;
      }
      await refreshAll();
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
      await synthesize([button.dataset.topic]);
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
    if (act === 'cancel-synth') { cancelSynthEdit(); return; }
    if (act === 'save-synth') {
      if (V.synthSaving) return;   // a save is in flight
      const topic = button.dataset.topic;
      const field = document.querySelector(`textarea[data-synth="${CSS.escape(topic)}"]`);
      const text = field ? field.value : (V.synthDrafts[topic] || '');
      V.error = null;
      // Freeze the editor until the answer comes back. Success redraws from
      // the server value, so anything typed meanwhile would be lost.
      captureOpenEditor();
      V.synthSaving = topic;
      renderContent();
      try {
        await api(`/api/syntheses/${encodeURIComponent(topic)}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
        });
        V.synthEditing = null;
        delete V.synthDrafts[topic];
      } catch (error) {
        V.synthDrafts[topic] = text;   // keep what was typed so the save can be retried
        V.error = `Could not save the synthesis: ${error.message}`;
      } finally {
        V.synthSaving = null;
      }
      await refreshAll();
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
        if (V.kind && row.kind !== V.kind) { V.kind = ''; $('kind').value = ''; }
        if (V.unreviewed && row.reviewed) { V.unreviewed = false; $('only-unreviewed').checked = false; }
        if (V.unverified && row.quote_verified !== false) { V.unverified = false; $('only-unverified').checked = false; }
        if (V.q.trim() && !queryMatcher()(haystack(row))) { V.q = ''; $('q').value = ''; }
      }
      V.selectedId = claim;
      syncHash(true);
      renderAll();   // a cleared topic or paper filter changes the sidebar too
      scrollToSelected();
      return;
    }
    if (act === 'reextract') {
      const workspace = await settleDeletes();
      if (!workspace) return;   // the delete failed; the pass would read the claim
      await api(`/api/papers/${encodeURIComponent(paper)}/extract`, {
        method: 'POST', headers: workspace,
      });
      await refresh();
      return;
    }
    if (act === 'verify') {
      V.error = null;
      try {
        await api(`/api/papers/${encodeURIComponent(paper)}/verify`, { method: 'POST' });
      } catch (error) {
        V.error = `Could not check the quotes: ${error.message}`;
      }
      await refreshAll();
      return;
    }
    if (act === 'retag-one') {
      const workspace = await settleDeletes();
      if (!workspace) return;   // the delete failed; the pass would read the claim
      await api('/api/retag', {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...workspace },
        body: JSON.stringify({ keys: [paper] }),
      });
      await refresh();
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
      captureOpenEditor();
      // Resume a held draft rather than overwriting what was typed into it.
      if (!V.newClaim || V.newClaim.paper !== paper || !(V.newClaim.text || '').trim()) {
        V.newClaim = blankClaim(paper);
      }
      V.editing = NEW_CLAIM_ID;
      renderContent();
      return;
    }
    if (act === 'del-paper') {
      await removePaper(paper);
      return;
    }
    if (act === 'accept-tag' || act === 'reject-tag') {
      const field = act === 'accept-tag' ? 'accept' : 'discard';
      await api(`/api/papers/${encodeURIComponent(paper)}/proposed-tags`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [field]: [button.dataset.tag] }),
      });
      delete paperCache()[paper];
      await refreshAll();
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
  const card = event.target.closest('.claim[data-claim]');
  if (card && card.dataset.claim !== V.selectedId) {
    // Selecting another claim redraws the list, which would rebuild an open
    // editor from the server row; keep what is typed in it first.
    captureOpenEditor();
    V.selectedId = card.dataset.claim;
    renderContent();
  }
});

$('papers').addEventListener('click', (event) => {
  // The same menu the right-click opens, for the people who never try one.
  const menuButton = event.target.closest('[data-menu]');
  if (menuButton) {
    const box = menuButton.getBoundingClientRect();
    openPaperMenu(menuButton.dataset.menu, box.left, box.bottom + 4);
    return;
  }
  const li = event.target.closest('[data-paper]');
  if (!li) return;
  const next = li.dataset.paper || null;
  // Clicking the paper already selected is not navigation. It used to run the
  // whole abandon path anyway, which threw away a new claim being written.
  // From the tensions view it is navigation: back to that paper's claims.
  if (next === V.paper && V.view === 'claims') return;
  if (next === V.paper) { showView('claims'); syncHash(true); renderAll(); return; }
  // Keep an existing claim's edits, but still abandon a new unsaved claim:
  // that one was never persisted and belongs to the paper being left. A
  // synthesis being edited is parked too: `render` skips the list while one
  // is open, and its topic heading is not shown under a paper anyway.
  captureOpenEditor();
  parkNewClaimForNavigation(next);
  parkSynthEditor();
  showView('claims');
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
  await refresh();
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
let readOnArrival = $('auto-extract').checked;
$('auto-extract').addEventListener('change', () => { readOnArrival = $('auto-extract').checked; });

function syncAnalysisControls() {
  const enabled = S.ai_enabled === true;
  const toggle = $('ai-enabled');
  toggle.disabled = savingAnalysisSetting || typeof S.ai_enabled !== 'boolean';
  if (!savingAnalysisSetting) toggle.checked = enabled;
  $('auto-extract').disabled = !enabled;
  $('auto-extract').checked = enabled && readOnArrival;
  document.querySelectorAll('[data-ai-action]').forEach((button) => {
    button.disabled = !enabled;
  });
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
    stateEtag = null;
    $('ai-settings-status').textContent = settings.ai_enabled ? 'AI analysis enabled.' : 'AI analysis disabled.';
  } catch (error) {
    $('ai-settings-status').textContent = `Could not save: ${error.message}`;
  } finally {
    savingAnalysisSetting = false;
    syncAnalysisControls();
    renderStats();
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
  syncThemeControls();
  syncAnalysisControls();
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

// --- paper context menu --------------------------------------------------

function openPaperMenu(paper, x, y) {
  const p = S.papers.find((row) => row.key === paper);
  const menu = $('ctxmenu');
  menu.innerHTML = `
    <li class="mh">${esc(p ? p.title || paper : paper)}</li>
    ${p && p.has_pdf ? `<li><button type="button" data-act="open-pdf" data-paper="${esc(paper)}">Open the PDF</button></li>` : ''}
    <li><button type="button" data-act="del-paper" data-paper="${esc(paper)}">Remove paper</button></li>`;
  menu.hidden = false;
  // Measure after showing, then keep the whole menu inside the window.
  const { width, height } = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(0, Math.min(x, window.innerWidth - width - 4))}px`;
  menu.style.top = `${Math.max(0, Math.min(y, window.innerHeight - height - 4))}px`;
}

function closePaperMenu() {
  $('ctxmenu').hidden = true;
}

$('papers').addEventListener('contextmenu', (event) => {
  const li = event.target.closest('[data-paper]');
  if (!li || !li.dataset.paper) return;   // "All papers" has nothing to remove
  event.preventDefault();
  openPaperMenu(li.dataset.paper, event.clientX, event.clientY);
});

$('ctxmenu').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-act]');
  if (!button) return;
  closePaperMenu();
  const paper = button.dataset.paper;
  if (button.dataset.act === 'del-paper') await removePaper(paper);
  if (button.dataset.act === 'open-pdf') {
    window.open(`/pdf/${encodeURIComponent(paper)}?${workspaceQuery()}&inline=1`, '_blank');
  }
});

// A press anywhere outside the menu dismisses it. Right-clicking another paper
// also lands here first, then the contextmenu handler above reopens it there.
document.addEventListener('mousedown', (event) => {
  if (!$('ctxmenu').contains(event.target)) closePaperMenu();
});
// Escape is handled in the keyboard section below, ahead of the editor keys,
// so closing the menu does not also cancel an open editor.
$('side').addEventListener('scroll', closePaperMenu);
window.addEventListener('resize', closePaperMenu);

$('tags').addEventListener('click', (event) => {
  const li = event.target.closest('[data-tag]');
  if (!li) return;
  V.tag = V.tag === li.dataset.tag ? null : li.dataset.tag;
  syncHash(true);
  renderAll();
});

$('workspace').addEventListener('change', (event) => switchWorkspace(event.target.value));

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
    workspaces = result.workspaces || workspaces;
    renderWorkspacePicker();
    await switchWorkspace(result.workspace.id);
  } catch (error) {
    toast(`Could not create workspace: ${error.message}`, { tone: 'warn' });
  }
});

async function addReferences() {
  const text = $('refs').value.trim();
  if (!text) return;
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
    return;
  }
  const unknown = result.unknown || [];
  $('refs').value = unknown.join('\n');
  // Which lines failed, not how many: a count leaves the reader to work out
  // for themselves which of what they pasted is the problem.
  showRefWarning(unknown);
  await refresh();
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
  await api('/api/tags', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, description: '' }),
  });
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
  await api('/api/retag', {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...workspace }, body: '{}',
  });
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

$('content').addEventListener('input', (e) => {
  if (e.target.matches('textarea[data-synth]')) V.synthDrafts[e.target.dataset.synth] = e.target.value;
});

// Each filter keeps whatever is typed in an open editor before redrawing.
$('content').addEventListener('change', (e) => {
  if (e.target.matches('[data-graph-opt]')) { graphOption(e.target); return; }
  if (e.target.id === 'tension-status') { V.tensionStatus = e.target.value; renderContent(); }
  if (e.target.id === 'agreement-status') { V.agreementStatus = e.target.value; renderContent(); }
});
$('content').addEventListener('input', (e) => {
  if (e.target.matches('[data-graph-opt="minShared"]')) graphOption(e.target);
});

function graphOption(field) {
  const name = field.dataset.graphOpt;
  if (name === 'minShared') {
    V.graph.minShared = Math.max(1, parseInt(field.value, 10) || 1);
    const label = $('content').querySelector('[data-graph-min]');
    if (label) label.textContent = String(V.graph.minShared);
  } else {
    V.graph[name] = field.checked;
  }
  if (V.view === 'graph') renderGraph();
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
});
$('paper-sort').value = V.paperSort;
$('only-unreviewed').addEventListener('change', (e) => { captureOpenEditor(); V.unreviewed = e.target.checked; renderContent(); });
$('only-unverified').addEventListener('change', (e) => { captureOpenEditor(); V.unverified = e.target.checked; renderContent(); });
$('group-by-tag').addEventListener('change', (e) => { captureOpenEditor(); V.group = e.target.checked; renderContent(); });

// --- keyboard -------------------------------------------------------------

function selectedRow(rows) {
  return rows.find((row) => row.id === V.selectedId) || rows[0] || null;
}

function moveSelection(rows, step) {
  if (!rows.length) return;
  captureOpenEditor();
  const at = rows.findIndex((row) => row.id === V.selectedId);
  const next = at < 0 ? 0 : Math.min(Math.max(at + step, 0), rows.length - 1);
  V.selectedId = rows[next].id;
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
  V.selectedId = next.id;
  renderContent();
  scrollToSelected();
}

function openHelp() {
  closePaperMenu();
  closeSettings();
  if (!$('help').open) $('help').showModal();
}

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
  if (event.key === 'Escape' && !$('settings-menu').hidden) {
    closeSettings({ restoreFocus: true });
    return;
  }
  // While the paper menu is open Escape belongs to it. Falling through to the
  // editor branches would also run `cancelEdit` and drop a draft the user
  // only meant to keep by dismissing the menu.
  if (event.key === 'Escape' && !$('ctxmenu').hidden) { closePaperMenu(); return; }
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
    } else if (V.editing) {
      cancelEdit();
    }
    return;
  }
  // The shortcuts nobody can see are the ones nobody uses, so the list is a
  // keystroke away from anywhere.
  if (event.key === '?') { event.preventDefault(); openHelp(); return; }
  if (event.key === '/') {
    // The box filters the claims, so it belongs to that view: reaching for it
    // from the map or the tensions is a way of asking to go back.
    event.preventDefault();
    if (V.view !== 'claims') { showView('claims'); syncHash(true); renderAll(); }
    $('q').focus();
    $('q').select();
    return;
  }
  if (V.view !== 'claims') {
    if (event.key === 'Escape') { showView('claims'); syncHash(true); renderAll(); }
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
    if (row) { captureOpenEditor(); V.editing = row.id; renderContent(); }
  } else if (event.key === 'r') {
    const row = selectedRow(rows);
    if (!row) return;
    // Marking one reviewed moves to the next, so a paper is reviewed by
    // holding one key rather than alternating between two. Taking a review
    // back does not move: that is a correction, and it is made where it is.
    const marking = !row.reviewed;
    const following = rows[rows.findIndex((other) => other.id === row.id) + 1];
    const took = await toggleReviewed(row);
    if (took && marking && following && visibleClaims().some((other) => other.id === following.id)) {
      V.selectedId = following.id;
      renderContent();
      scrollToSelected();
    }
  } else if (event.key === 'Escape') {
    cancelEdit();
  }
});

function scrollToSelected() {
  const el = document.querySelector('.claim.sel');
  if (el) el.scrollIntoView({ block: 'nearest' });
}

// --- drag and drop --------------------------------------------------------

['dragenter', 'dragover'].forEach((type) => document.addEventListener(type, (event) => {
  event.preventDefault();
  document.body.classList.add('dragging');
}));
['dragleave', 'drop'].forEach((type) => document.addEventListener(type, (event) => {
  if (type === 'dragleave' && event.relatedTarget) return;
  document.body.classList.remove('dragging');
}));

document.addEventListener('drop', async (event) => {
  event.preventDefault();
  const files = [...(event.dataTransfer.files || [])].filter((f) => f.type === 'application/pdf'
    || f.name.toLowerCase().endsWith('.pdf'));
  const text = event.dataTransfer.getData('text/plain');
  if (files.length) {
    const body = new FormData();
    files.forEach((file) => body.append('files', file, file.name));
    await api(`/api/upload?extract_now=${$('auto-extract').checked}`, { method: 'POST', body });
  } else if (text && text.trim()) {
    await api('/api/ingest', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, extract: $('auto-extract').checked }),
    });
  }
  await refresh();
});

// --- boot -----------------------------------------------------------------

async function boot() {
  await loadWorkspaces();
  applyHash();
  await refresh();
  $('kind').innerHTML = '<option value="">every kind</option>'
    + S.kinds.map((k) => `<option value="${esc(k)}">${esc(k)}</option>`).join('');
  $('kind').value = V.kind;   // the kinds arrive with the corpus, after the URL was read
  const before = [V.paper, V.tag, V.kind];
  dropMissingFilters();
  if (V.paper !== before[0] || V.tag !== before[1] || V.kind !== before[2]) renderAll();
  // `refresh` went through `render`, whose editor guard leaves the content pane
  // alone whenever the view is Research: the poll must not rebuild the form
  // under the cursor. At boot there is no form yet, so that guard would leave a
  // URL naming Research — a bookmark, or a reload of the page while on it —
  // with an active nav entry and a blank main pane. Draw it once here.
  else if (V.view === 'research') renderContent();
  setInterval(async () => {
    if (document.hidden) return;
    // Keep settings current while editing; the content guard below preserves
    // the editor DOM, draft text, focus, and selection.
    try {
      const changed = await pull();
      renderJobs();
      if (!changed) return;
      rerunTextSearch();     // a paper imported since holds the query's words too
      renderStats();
      renderPapers(); renderTensionsNav(); renderAgreementsNav(); renderResearchNav(); renderGraphNav(); renderTags();
      if (!V.editing && !V.synthEditing && V.view !== 'research') renderContent();
      syncAnalysisControls();
    } catch (e) { /* the server may be restarting; try again next tick */ }
  }, 2500);
}

boot().catch((error) => {
  document.getElementById('content').innerHTML =
    `<p class="warn">Could not reach the server: ${esc(error.message)}</p>`;
});
