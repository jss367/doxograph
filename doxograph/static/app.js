'use strict';

let S = { papers: [], claims: [], tags: [], tag_counts: {}, ledger: [], tensions: [], syntheses: [],
          kinds: [], strengths: [], relations: [], jobs: [], has_key: true };
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
// view is 'claims' or 'tensions'. The tensions view has no editor, so switching
// to it closes any open one (keeping its draft) and lets the background poll run.
// tensionFocus narrows the tensions view to those involving one claim; it is set
// by the marker on a claim card and cleared by "show all".
// synthEditing is the topic whose synthesis is open for correction by hand, and
// synthDraft what has been typed into it, kept across redraws like claim drafts.
const V = { paper: null, tag: null, q: '', kind: '', unreviewed: false, group: true,
            editing: null, selectedId: null, newClaim: null, failedNewClaims: {},
            drafts: {}, error: null, view: 'claims', tensionStatus: '', tensionFocus: null,
            synthEditing: null, synthDraft: null };

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

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (e) { /* keep statusText */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

async function refresh() {
  S = await api('/api/state');
  render();
}

// `refresh` goes through `render`, which leaves an open editor — and therefore
// the whole claim list — alone. Anything that adds or removes a row has to
// rebuild the list even while an editor is open, or the deleted row stays on
// screen and stays clickable. This keeps the open editor's draft across it.
async function refreshAll() {
  captureOpenEditor();
  S = await api('/api/state');
  renderAll();
}

// --- filtering ------------------------------------------------------------

function haystack(row) {
  return [row.text, row.evidence, row.quote, row.paper_title,
          (row.paper_authors || []).join(' '), (row.tags || []).join(' ')]
    .join(' ').toLowerCase();
}

function visibleClaims() {
  const needle = V.q.trim().toLowerCase();
  return S.claims.filter((row) =>
    (!V.paper || row.paper === V.paper)
    && (!V.tag || (row.tags || []).includes(V.tag))
    && (!V.kind || row.kind === V.kind)
    && (!V.unreviewed || !row.reviewed)
    && (!needle || haystack(row).includes(needle)));
}

// --- rendering ------------------------------------------------------------

// `render` leaves an open editor alone: it is what the background poll calls,
// and rebuilding under the cursor would move focus.
function render() {
  renderStats();
  renderPapers();
  renderTensionsNav();
  renderTags();
  if (!V.editing && !V.synthEditing) renderContent();
  renderJobs();
}

// `renderAll` is for a view change the user asked for. The draft is captured
// first, so rebuilding the editor is safe, and the claim list has to be rebuilt
// or a filter would change the sidebar without changing what is listed.
function renderAll() {
  captureOpenEditor();
  renderStats();
  renderPapers();
  renderTensionsNav();
  renderTags();
  renderContent();
  renderJobs();
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
  if (proposed) bits.push(`${proposed} proposed topics`);
  const openTensions = (S.tensions || []).filter((t) => t.status === 'open').length;
  if (openTensions) bits.push(`${openTensions} open tensions`);
  const staleSyntheses = (S.syntheses || []).filter((s) => s.stale).length;
  if (staleSyntheses) bits.push(`${staleSyntheses} stale syntheses`);
  if (!S.has_key) bits.push('no API key found');
  $('stats').textContent = bits.join(' · ');
}

function renderPapers() {
  const claims = V.view === 'claims';
  const all = `<li class="${claims && V.paper === null ? 'active' : ''}" data-paper="">
    <span class="pt">All papers</span>
    <span class="pm">${S.claims.length} claims</span></li>`;
  $('papers').innerHTML = all + S.papers.map((p) => `
    <li class="${claims && V.paper === p.key ? 'active' : ''}" data-paper="${esc(p.key)}">
      <span class="pt"><span class="dot ${esc(p.status)}"></span>${esc(p.title || p.key)}</span>
      <span class="pm">${esc((p.authors || [])[0] ? p.authors[0].split(' ').pop() : '?')}
        ${p.year ? esc(p.year) : ''} · ${p.n_claims} claims${p.n_unreviewed ? `, ${p.n_unreviewed} new` : ''}</span>
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
      ${p.has_pdf ? `· <a href="/pdf/${esc(key)}" target="_blank" rel="noopener">PDF</a>` : '· no PDF'}</div>
    ${p.summary ? `<p class="ps">${esc(p.summary)}</p>` : ''}
    ${p.relevance ? `<p class="ps"><em>Why it is here:</em> ${esc(p.relevance)}</p>` : ''}
    <div class="row">
      <button type="button" data-act="reextract" data-paper="${esc(key)}">Re-read paper</button>
      <button type="button" data-act="retag-one" data-paper="${esc(key)}">Retag claims</button>
      <button type="button" data-act="add-claim" data-paper="${esc(key)}">Add claim by hand</button>
      <button type="button" data-act="del-paper" data-paper="${esc(key)}" style="margin-left:auto">Remove</button>
    </div>
    ${proposedPanel(key)}
  </div>`;
}

function proposedPanel(key) {
  const paper = S.papers.find((x) => x.key === key);
  if (!paper || !paper.n_proposed_tags) return '';
  // Keyed by the paper's updated timestamp, so a re-read that replaces the
  // proposals invalidates the cache instead of showing names the server will
  // no longer accept.
  const entry = (window.__paperCache || {})[key];
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
  window.__paperCache = window.__paperCache || {};
  if (window.__paperCache[key] === 'loading') return;
  window.__paperCache[key] = 'loading';
  try {
    const paper = await api(`/api/papers/${encodeURIComponent(key)}`);
    window.__paperCache[key] = { updated: paper.updated, proposed_tags: paper.proposed_tags || [] };
    if (wanted && paper.updated !== wanted) delete window.__paperCache[key];  // changed again mid-flight
    if (!V.editing) renderContent();
  } catch (e) { delete window.__paperCache[key]; }
}

// `shown` carries the claim ids already drawn as an editor this pass. In grouped
// mode a claim appears once per topic, and emitting a form for each occurrence
// would put several elements under one `data-form`, so `captureOpenEditor` would
// read the first while the user typed into another.
function claimCard(row, shown) {
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
       data-claim="${esc(row.id)}" data-paper="${esc(row.paper)}">
    <p class="ctext"><span class="kind ${esc(row.kind)}">${esc(row.kind)}</span> ${esc(row.text)}</p>
    <div class="cmeta">
      ${tags}
      <span data-act="open-paper" data-paper="${esc(row.paper)}" style="cursor:pointer">${esc(cite)}</span>
      ${row.locator ? '· ' + esc(row.locator) : ''}
      ${tensionMarker(row.id)}
      <span class="cact">
        <button type="button" data-act="review" data-claim="${esc(row.id)}" data-paper="${esc(row.paper)}">
          ${row.reviewed ? 'reviewed' : 'mark reviewed'}</button>
        <button type="button" data-act="edit" data-claim="${esc(row.id)}">edit</button>
        <button type="button" data-act="del" data-claim="${esc(row.id)}" data-paper="${esc(row.paper)}">delete</button>
      </span>
    </div>
    ${row.evidence ? `<p class="cev">${esc(row.evidence)}</p>` : ''}
    ${row.quote ? `<blockquote>${esc(row.quote)}</blockquote>` : ''}
    ${links}
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

// A claim as it appears inside a tension: the same card, minus the review and
// edit controls. Editing belongs to the claims view, where the editor's
// lifecycle is handled; a click on the citation goes there.
function tensionClaimCard(row) {
  const cite = `${(row.paper_authors || [])[0] ? row.paper_authors[0].split(' ').pop() : row.paper}`
    + ` ${row.paper_year || ''}`;
  return `<div class="claim ${esc(row.strength)} ${row.reviewed ? '' : 'unreviewed'}" data-tclaim="${esc(row.id)}">
    <p class="ctext"><span class="kind ${esc(row.kind)}">${esc(row.kind)}</span> ${esc(row.text)}</p>
    <div class="cmeta">
      <span data-act="open-paper" data-paper="${esc(row.paper)}" style="cursor:pointer">${esc(cite)}</span>
      ${row.locator ? '· ' + esc(row.locator) : ''}
      ${row.reviewed ? '' : '· <span class="hint">unreviewed</span>'}
    </div>
    ${row.evidence ? `<p class="cev">${esc(row.evidence)}</p>` : ''}
    ${row.quote ? `<blockquote>${esc(row.quote)}</blockquote>` : ''}
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
    <div class="tpair">${t.claims.map(tensionClaimCard).join('')}</div>
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
      <button type="button" data-act="find-tensions" style="margin-left:auto">Find tensions</button>
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

function showView(view) {
  if (view === V.view) return;
  // The tensions view has no editor. Park any open one rather than leaving
  // `V.editing` set on a form that is no longer on screen, which would also
  // stop the background poll.
  captureOpenEditor();
  V.editing = null;
  V.error = null;
  V.view = view;
  if (view !== 'tensions') V.tensionFocus = null;
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
    const text = V.synthDraft !== null ? V.synthDraft : (synth ? synth.text : '');
    return `<div class="synth" data-topic="${esc(tag)}">
      <textarea data-synth="${esc(tag)}">${esc(text)}</textarea>
      <div class="smeta">
        <span class="hint">Cite claims as [claim-id]; they become links.</span>
        <span class="cact" style="margin-left:auto">
          <button type="button" data-act="save-synth" data-topic="${esc(tag)}" class="primary">Save</button>
          <button type="button" data-act="cancel-synth">Cancel</button>
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
      ${synth.stale ? '<span class="stale">claims have changed since</span>' : ''}
      <span class="cact" style="margin-left:auto">
        <button type="button" data-act="synthesize" data-topic="${esc(tag)}">Rewrite</button>
        <button type="button" data-act="edit-synth" data-topic="${esc(tag)}">edit</button>
        <button type="button" data-act="del-synth" data-topic="${esc(tag)}">delete</button>
      </span>
    </div>
  </div>`;
}

// The button in a topic heading when no synthesis exists yet.
function synthesizeButton(tag) {
  if (synthesisFor(tag) || V.synthEditing === tag) return '';
  return `<button type="button" class="mini" data-act="synthesize" data-topic="${esc(tag)}"
    title="Ask the model what the papers hold on this topic">synthesize</button>`;
}

async function synthesize(topics) {
  V.error = null;
  try {
    const result = await api('/api/syntheses', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
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

function renderContent() {
  if (V.view === 'tensions') { renderTensions(); return; }
  const main = $('main');
  const scrollTop = main ? main.scrollTop : 0;
  const shown = new Set();
  const card = (row) => claimCard(row, shown);
  const rows = visibleClaims();
  // An editor for a claim the current filters exclude cannot be drawn, and
  // leaving `V.editing` set would also hold down the background poll, which
  // stands aside whenever an editor is open. Park it: keep the draft, close
  // the editor. The text is read from the form that is still on screen.
  if (V.editing && V.editing !== NEW_CLAIM_ID && !rows.some((row) => row.id === V.editing)) {
    captureOpenEditor();
    V.editing = null;
  }
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
    $('content').innerHTML = html;
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
        + group.map(card).join('') + '</div>';
    }
    if (untagged.length) {
      html += `<div class="group"><h3>untagged <span class="n hint">${untagged.length}</span></h3>`
        + untagged.map(card).join('') + '</div>';
    }
  } else {
    html += rows.map(card).join('');
  }
  $('content').innerHTML = html;
  applySavingState();
  if (main) main.scrollTop = scrollTop;
}

function renderJobs() {
  const active = (S.jobs || []).filter((j) => j.state !== 'done' || j.detail);
  $('jobs').innerHTML = active.slice(0, 6).map((j) => `
    <div class="job ${j.state === 'error' ? 'error' : ''}">
      <span class="lbl">${esc(j.label)}</span>
      <span class="st">${esc(j.state)}${j.detail ? ': ' + esc(j.detail) : ''}</span>
    </div>`).join('');
}

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

async function toggleReviewed(row) {
  // Shared by the review button — including the duplicate cards a claim gets in
  // grouped mode — and the `r` key, so both keep an open editor in step.
  if (isSaving(row.id)) return;   // a request for it is in flight
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
}

async function patchClaim(paper, claim, patch) {
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
  if (!confirm(`Remove ${p ? p.title || paper : paper} and its claims?`)) return;
  await api(`/api/papers/${encodeURIComponent(paper)}`, { method: 'DELETE' });
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
    if (act === 'drop-link') {
      button.closest('.linkrow').querySelector('[name="link-claim"]').value = '';
      button.closest('.linkrow').style.display = 'none';
      return;
    }
    if (act === 'review') {
      await toggleReviewed(S.claims.find((c) => c.id === claim));
      return;
    }
    if (act === 'del') {
      if (!confirm('Delete this claim?')) return;
      await api(`/api/papers/${encodeURIComponent(paper)}/claims/${encodeURIComponent(claim)}`,
                { method: 'DELETE' });
      // In grouped mode the same claim can be an editor in one topic group and
      // a plain card with a Delete button in another. Leaving `V.editing` set
      // would keep the deleted claim's form on screen, because `render()` skips
      // the content while an editor is open, and every Save would 404.
      delete V.drafts[claim];
      if (V.editing === claim) { V.editing = null; V.error = null; }
      if (V.selectedId === claim) V.selectedId = null;
      // Deleting any claim changes the list, including when the open editor
      // belongs to a different one; without a content rebuild the deleted card
      // stays visible and clickable and the next action on it 404s.
      await refreshAll();
      return;
    }
    if (act === 'open-paper') {
      captureOpenEditor();
      closeEditorsNotBelongingTo(paper);
      showView('claims');
      V.paper = paper; V.tag = null; V.selectedId = null;
      renderAll();
      return;
    }
    if (act === 'tension-focus') {
      showView('tensions');
      V.tensionFocus = claim;
      V.tensionStatus = '';
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
    if (act === 'find-tensions') {
      await findTensions();
      return;
    }
    if (act === 'synthesize') {
      await synthesize([button.dataset.topic]);
      return;
    }
    if (act === 'edit-synth') {
      captureOpenEditor();
      V.synthEditing = button.dataset.topic;
      V.synthDraft = null;
      renderContent();
      return;
    }
    if (act === 'cancel-synth') {
      V.synthEditing = null;
      V.synthDraft = null;
      renderContent();
      return;
    }
    if (act === 'save-synth') {
      const topic = button.dataset.topic;
      const field = document.querySelector(`textarea[data-synth="${CSS.escape(topic)}"]`);
      const text = field ? field.value : (V.synthDraft || '');
      V.error = null;
      try {
        await api(`/api/syntheses/${encodeURIComponent(topic)}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
        });
        V.synthEditing = null;
        V.synthDraft = null;
      } catch (error) {
        V.synthDraft = text;
        V.error = `Could not save the synthesis: ${error.message}`;
      }
      await refreshAll();
      return;
    }
    if (act === 'del-synth') {
      if (!confirm('Delete this synthesis?')) return;
      await api(`/api/syntheses/${encodeURIComponent(button.dataset.topic)}`, { method: 'DELETE' });
      await refreshAll();
      return;
    }
    if (act === 'goto-claim') {
      V.selectedId = claim;
      renderContent();
      scrollToSelected();
      return;
    }
    if (act === 'reextract') {
      await api(`/api/papers/${encodeURIComponent(paper)}/extract`, { method: 'POST' });
      await refresh();
      return;
    }
    if (act === 'retag-one') {
      await api('/api/retag', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
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
      delete (window.__paperCache || {})[paper];
      await refreshAll();
      return;
    }
  }
  // The edit form lives inside a `.claim` wrapper, so a click on one of its
  // fields bubbles down to the card-selection branch below. Re-rendering there
  // replaces the form, drops focus, and redraws from the stored row, which
  // makes the editor unusable with a mouse.
  if (event.target.closest('form[data-form]')) return;

  const tagEl = event.target.closest('[data-tag]');
  if (tagEl && !tagEl.dataset.act) {
    V.tag = V.tag === tagEl.dataset.tag ? null : tagEl.dataset.tag;
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
  const li = event.target.closest('[data-paper]');
  if (!li) return;
  const next = li.dataset.paper || null;
  // Clicking the paper already selected is not navigation. It used to run the
  // whole abandon path anyway, which threw away a new claim being written.
  // From the tensions view it is navigation: back to that paper's claims.
  if (next === V.paper && V.view === 'claims') return;
  if (next === V.paper) { showView('claims'); renderAll(); return; }
  // Keep an existing claim's edits, but still abandon a new unsaved claim:
  // that one was never persisted and belongs to the paper being left.
  captureOpenEditor();
  parkNewClaimForNavigation(next);
  showView('claims');
  V.paper = next;
  V.selectedId = null;
  V.editing = null;
  render();   // the editor is closed here, so render redraws the list anyway
});

$('tensions-nav').addEventListener('click', (event) => {
  if (!event.target.closest('[data-view]')) return;
  showView('tensions');
  renderAll();
});

async function findTensions() {
  V.error = null;
  try {
    const result = await api('/api/tensions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
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
  renderAll();
  await findTensions();
});

$('btn-synth').addEventListener('click', async () => {
  showView('claims');
  V.group = true;
  $('group-by-tag').checked = true;
  renderAll();
  await synthesize(null);
});

// --- paper context menu --------------------------------------------------

function openPaperMenu(paper, x, y) {
  const p = S.papers.find((row) => row.key === paper);
  const menu = $('ctxmenu');
  menu.innerHTML = `
    <li class="mh">${esc(p ? p.title || paper : paper)}</li>
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
  if (button.dataset.act === 'del-paper') await removePaper(button.dataset.paper);
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
  renderAll();
});

$('btn-add').addEventListener('click', async () => {
  const text = $('refs').value.trim();
  if (!text) return;
  const result = await api('/api/ingest', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, extract: $('auto-extract').checked }),
  });
  $('refs').value = (result.unknown || []).join('\n');
  if (result.unknown && result.unknown.length) {
    alert(`Could not read ${result.unknown.length} reference(s); they are still in the box.`);
  }
  await refresh();
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
  if (!confirm('Reassign topics on every paper against the current vocabulary?')) return;
  await api('/api/retag', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
  });
  await refresh();
});

$('btn-export').addEventListener('click', async () => {
  const result = await api('/api/export', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: 'Doxograph' }),
  });
  alert(`Written to ${result.path}`);
});

$('btn-bib').addEventListener('click', () => window.open('/api/bibtex', '_blank'));

$('content').addEventListener('input', (e) => {
  if (e.target.matches('textarea[data-synth]')) V.synthDraft = e.target.value;
});

// Each filter keeps whatever is typed in an open editor before redrawing.
$('content').addEventListener('change', (e) => {
  if (e.target.id !== 'tension-status') return;
  V.tensionStatus = e.target.value;
  renderContent();
});
$('q').addEventListener('input', (e) => { captureOpenEditor(); V.q = e.target.value; renderContent(); });
$('kind').addEventListener('change', (e) => { captureOpenEditor(); V.kind = e.target.value; renderContent(); });
$('only-unreviewed').addEventListener('change', (e) => { captureOpenEditor(); V.unreviewed = e.target.checked; renderContent(); });
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

document.addEventListener('keydown', async (event) => {
  // While the paper menu is open Escape belongs to it. Falling through to the
  // editor branches would also run `cancelEdit` and drop a draft the user
  // only meant to keep by dismissing the menu.
  if (event.key === 'Escape' && !$('ctxmenu').hidden) { closePaperMenu(); return; }
  const tag = (event.target.tagName || '').toLowerCase();
  if (['input', 'textarea', 'select'].includes(tag)) {
    if (event.key === 'Escape' && V.editing) cancelEdit();
    if (event.key === 'Escape' && V.synthEditing) { V.synthEditing = null; V.synthDraft = null; renderContent(); }
    return;
  }
  if (V.view !== 'claims') {
    if (event.key === 'Escape') { showView('claims'); renderAll(); }
    return;
  }
  const rows = visibleClaims();
  if (event.key === 'j' || event.key === 'ArrowDown') {
    moveSelection(rows, 1);
  } else if (event.key === 'k' || event.key === 'ArrowUp') {
    moveSelection(rows, -1);
  } else if (event.key === 'e') {
    const row = selectedRow(rows);
    if (row) { captureOpenEditor(); V.editing = row.id; renderContent(); }
  } else if (event.key === 'r') {
    const row = selectedRow(rows);
    if (row) await toggleReviewed(row);
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

function stateSignature(state) {
  return JSON.stringify([
    (state.papers || []).map((p) => [p.key, p.status, p.n_claims, p.n_proposed_tags, p.updated]),
    (state.claims || []).map((c) => [c.id, c.reviewed, c.updated || c.added, (c.tags || []).join(',')]),
    (state.tags || []).map((t) => t.name),
    (state.ledger || []).map((c) => c.id),
    (state.tensions || []).map((t) => [t.id, t.status, t.stale, t.found, (t.topics || []).join(',')]),
    (state.syntheses || []).map((s) => [s.topic, s.written, s.stale, s.source]),
  ]);
}

async function boot() {
  await refresh();
  $('kind').innerHTML = '<option value="">every kind</option>'
    + S.kinds.map((k) => `<option value="${esc(k)}">${esc(k)}</option>`).join('');
  setInterval(async () => {
    if (document.hidden) return;
    const busy = (S.jobs || []).some((j) => ['queued', 'fetching', 'reading'].includes(j.state));
    if (!busy && (V.editing || V.synthEditing)) return;
    try {
      const next = await api('/api/state');
      const changed = stateSignature(next) !== stateSignature(S);
      S = next;
      renderJobs();
      if (!changed) return;
      renderStats();
      if (!V.editing && !V.synthEditing) { renderPapers(); renderTensionsNav(); renderTags(); renderContent(); }
    } catch (e) { /* the server may be restarting; try again next tick */ }
  }, 2500);
}

boot().catch((error) => {
  document.getElementById('content').innerHTML =
    `<p class="warn">Could not reach the server: ${esc(error.message)}</p>`;
});
