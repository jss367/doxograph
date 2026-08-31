'use strict';

let S = { papers: [], claims: [], tags: [], tag_counts: {}, ledger: [],
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
const V = { paper: null, tag: null, q: '', kind: '', unreviewed: false, group: true,
            editing: null, selectedId: null, newClaim: null, drafts: {}, error: null };

function blankClaim(paper) {
  return {
    id: NEW_CLAIM_ID, paper, text: '', kind: S.kinds[0] || 'finding',
    strength: 'supporting', tags: [], evidence: '', quote: '', locator: '',
    ledger_links: [], reviewed: true, paper_title: '', paper_authors: [], paper_year: null,
  };
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
  renderTags();
  if (!V.editing) renderContent();
  renderJobs();
}

// `renderAll` is for a view change the user asked for. The draft is captured
// first, so rebuilding the editor is safe, and the claim list has to be rebuilt
// or a filter would change the sidebar without changing what is listed.
function renderAll() {
  captureOpenEditor();
  renderStats();
  renderPapers();
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
  if (!S.has_key) bits.push('no API key found');
  $('stats').textContent = bits.join(' · ');
}

function renderPapers() {
  const all = `<li class="${V.paper === null ? 'active' : ''}" data-paper="">
    <span class="pt">All papers</span>
    <span class="pm">${S.claims.length} claims</span></li>`;
  $('papers').innerHTML = all + S.papers.map((p) => `
    <li class="${V.paper === p.key ? 'active' : ''}" data-paper="${esc(p.key)}">
      <span class="pt"><span class="dot ${esc(p.status)}"></span>${esc(p.title || p.key)}</span>
      <span class="pm">${esc((p.authors || [])[0] ? p.authors[0].split(' ').pop() : '?')}
        ${p.year ? esc(p.year) : ''} · ${p.n_claims} claims${p.n_unreviewed ? `, ${p.n_unreviewed} new` : ''}</span>
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

function renderContent() {
  const main = $('main');
  const scrollTop = main ? main.scrollTop : 0;
  const shown = new Set();
  const card = (row) => claimCard(row, shown);
  const rows = visibleClaims();
  if (!rows.some((row) => row.id === V.selectedId)) V.selectedId = rows.length ? rows[0].id : null;
  let html = V.paper ? paperHeader(V.paper) : '';
  if (V.error) html += `<p class="warn">${esc(V.error)}</p>`;
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
      html += `<div class="group"><h3>${esc(tag)} <span class="n hint">${group.length}</span></h3>`
        + (description ? `<p class="gd">${esc(description)}</p>` : '')
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
function captureOpenEditor() {
  const form = document.querySelector('form[data-form]');
  if (!form) return;
  const wrap = form.closest('[data-claim]');
  const patch = readForm(form);
  if (wrap.dataset.claim === NEW_CLAIM_ID) {
    V.newClaim = { ...V.newClaim, ...patch };
  } else {
    V.drafts[wrap.dataset.claim] = { ...V.drafts[wrap.dataset.claim], ...patch };
  }
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
  const patch = readForm(form);
  V.error = null;

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
      V.error = `Could not save the claim: ${error.message}`;
      renderContent();          // editor stays open, still holding the text
      return;
    }
    V.editing = null;
    V.newClaim = null;
    await refresh();
    return;
  }

  V.editing = null;
  try {
    await patchClaim(wrap.dataset.paper, wrap.dataset.claim, patch);
    delete V.drafts[wrap.dataset.claim];
  } catch (error) {
    // Keep what was typed so the save can be retried; the server row is stale.
    V.drafts[wrap.dataset.claim] = { ...V.drafts[wrap.dataset.claim], ...patch };
    V.error = `Could not save the claim: ${error.message}`;
    V.editing = wrap.dataset.claim;
    renderContent();
  }
});

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
      const row = S.claims.find((c) => c.id === claim);
      await patchClaim(paper, claim, { reviewed: !row.reviewed });
      return;
    }
    if (act === 'del') {
      if (!confirm('Delete this claim?')) return;
      await api(`/api/papers/${encodeURIComponent(paper)}/claims/${encodeURIComponent(claim)}`,
                { method: 'DELETE' });
      await refresh();
      return;
    }
    if (act === 'open-paper') {
      V.paper = paper; V.tag = null; V.selectedId = null;
      renderAll();
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
      if (!V.newClaim || !(V.newClaim.text || '').trim()) V.newClaim = blankClaim(paper);
      V.editing = NEW_CLAIM_ID;
      renderContent();
      return;
    }
    if (act === 'del-paper') {
      const p = S.papers.find((x) => x.key === paper);
      if (!confirm(`Remove ${p ? p.title || paper : paper} and its claims?`)) return;
      await api(`/api/papers/${encodeURIComponent(paper)}`, { method: 'DELETE' });
      V.paper = null;
      await refresh();
      return;
    }
    if (act === 'accept-tag' || act === 'reject-tag') {
      const field = act === 'accept-tag' ? 'accept' : 'discard';
      await api(`/api/papers/${encodeURIComponent(paper)}/proposed-tags`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [field]: [button.dataset.tag] }),
      });
      delete (window.__paperCache || {})[paper];
      await refresh();
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
  // Keep an existing claim's edits, but still abandon a new unsaved claim:
  // that one was never persisted and belongs to the paper being left.
  captureOpenEditor();
  V.paper = li.dataset.paper || null;
  V.selectedId = null;
  V.editing = null;
  V.newClaim = null;
  render();   // the editor is closed here, so render redraws the list anyway
});

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

// Each filter keeps whatever is typed in an open editor before redrawing.
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
  const tag = (event.target.tagName || '').toLowerCase();
  if (['input', 'textarea', 'select'].includes(tag)) {
    if (event.key === 'Escape' && V.editing) cancelEdit();
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
    if (row) await patchClaim(row.paper, row.id, { reviewed: !row.reviewed });
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
  ]);
}

async function boot() {
  await refresh();
  $('kind').innerHTML = '<option value="">every kind</option>'
    + S.kinds.map((k) => `<option value="${esc(k)}">${esc(k)}</option>`).join('');
  setInterval(async () => {
    if (document.hidden) return;
    const busy = (S.jobs || []).some((j) => ['queued', 'fetching', 'reading'].includes(j.state));
    if (!busy && V.editing) return;
    try {
      const next = await api('/api/state');
      const changed = stateSignature(next) !== stateSignature(S);
      S = next;
      renderJobs();
      if (!changed) return;
      renderStats();
      if (!V.editing) { renderPapers(); renderTags(); renderContent(); }
    } catch (e) { /* the server may be restarting; try again next tick */ }
  }, 2500);
}

boot().catch((error) => {
  document.getElementById('content').innerHTML =
    `<p class="warn">Could not reach the server: ${esc(error.message)}</p>`;
});
