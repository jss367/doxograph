'use strict';

// Claims: cards, the claim editor and its drafts, reviewing, and saving.

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
    strength: 'supporting', tags: [], facets: {}, evidence: '', quote: '', locator: '',
    ledger_links: [], note: '', conditions: '', limitations: '', evidence_origin: '', evidence_relationship: 'unassessed', reviewed: true, paper_title: '', paper_authors: [], paper_year: null,
  };
}

function newClaimHasEdits(draft) {
  if (!draft) return false;
  const blank = blankClaim(draft.paper);
  return ['text', 'kind', 'strength', 'tags', 'facets', 'evidence', 'quote', 'locator',
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

function claimCard(row, shown, group = '') {
  if (V.editing === row.id && shown && !shown.has(row.id)) {
    shown.add(row.id);
    const draft = V.drafts[row.id];
    return editForm(draft ? { ...row, ...draft } : row);
  }
  const cite = `${(row.paper_authors || [])[0] ? row.paper_authors[0].split(' ').pop() : row.paper}`
    + ` ${row.paper_year || ''}`;
  const tags = (row.tags || []).map((t) => `<span class="tag" data-tag="${esc(t)}">#${esc(t)}</span>`).join(' ');
  const facetChips = claimFacetValues(row).map(([name, value]) => `<span class="facet-value"
    data-facet="${esc(name)}" data-facet-value="${esc(value)}" title="Filter by ${esc(name)}: ${esc(value)}"><span class="facet-name">${esc(name)}</span> ${esc(value)}</span>`).join(' ');
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
      ${facetChips}
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
      ${(S.facets || []).length ? `<div class="pair facet-fields">${S.facets.map((facet, i) => `
        <div><label title="${esc(facet.description || '')}">${esc(facet.name)} (comma separated)</label>
          <input data-facet-input="${esc(facet.name)}" value="${esc((facetEntry(row.facets, facet.name) || []).join(', '))}"
            data-facet-base="${esc(JSON.stringify(facetEntry(row.facetsBase || row.facets, facet.name) || []))}"
            list="${esc(`${tagListId}-facet-${i}`)}" size="28"></div>`).join('')}</div>` : ''}
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
    ${(S.facets || []).map((facet, i) => `<datalist id="${esc(`${tagListId}-facet-${i}`)}">${Object.keys(facetEntry(S.facet_counts, facet.name) || {})
      .map((value) => `<option value="${esc(value)}">`).join('')}</datalist>`).join('')}
  </div>`;
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

// The facet inputs, as the `facets` a claim stores: a facet with no values is
// left out, as the store leaves it out. Nothing when no facet is defined, so
// a form without the inputs does not clear values it never showed.
//
// `facetsBase` is what each field showed when the editor opened, kept on the
// draft so a redraw does not move it. A save sends only the facets that
// differ from it: a fill landing while the form is open is newer than the
// values the form was drawn with, and an unrelated edit must not undo it.
function facetFields(form) {
  const inputs = [...form.querySelectorAll('[data-facet-input]')];
  if (!inputs.length) return {};
  const facets = {}, facetsBase = {};
  for (const input of inputs) {
    const values = input.value.split(',').map((v) => v.trim()).filter(Boolean);
    if (values.length) facets[input.dataset.facetInput] = values;
    const base = JSON.parse(input.dataset.facetBase || '[]');
    if (base.length) facetsBase[input.dataset.facetInput] = base;
  }
  return { facets, facetsBase };
}

// A form's patch as the server takes it: the facets narrowed to the ones the
// person changed, an emptied one sent as an empty list to clear it.
function claimRequestBody(patch) {
  const { facets, facetsBase, ...body } = patch;
  if (!facets) return body;
  const changed = {};
  for (const facet of S.facets || []) {
    const now = facetEntry(facets, facet.name) || [], base = facetEntry(facetsBase, facet.name) || [];
    if (JSON.stringify(now) !== JSON.stringify(base)) changed[facet.name] = now;
  }
  return Object.keys(changed).length ? { ...body, facets: changed } : body;
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
    ...facetFields(form),
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
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(claimRequestBody(patch)),
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
    await patchClaim(wrap.dataset.paper, wrap.dataset.claim, claimRequestBody(patch));
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
