'use strict';

// Cross-paper analysis: tensions, agreements, and topic syntheses.

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

// Tensions a claim takes part in, for the marker on its card. Dismissed ones
// are not marked: the reviewer has said there is nothing there.
function tensionsFor(claimId) {
  return (S.tensions || []).filter((t) => t.status !== 'dismissed'
    && t.claims.some((c) => c.id === claimId));
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
