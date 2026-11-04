'use strict';

// The findings page: what the papers hold on each topic, where they agree,
// and where they pull against each other, with the claims behind each row a
// click away. Shares the library state declared in app.js and the claim and
// synthesis drawings in analysis.js.

// Rows the reader has opened stay open across redraws: a poll, a status
// change or a synthesis edit all redraw the page, and a row closing under
// the reader's eyes would lose their place.
const findingsOpen = new Set();

function findingRow(row, kind) {
  const open = findingsOpen.has(row.id);
  const sentence = FindingsModel.firstSentence(row.note) || `${kind} between ${FindingsModel.papersOf(row).join(' and ')}`;
  const papers = FindingsModel.papersOf(row).join(' · ');
  const unreviewed = FindingsModel.unreviewedCount(row.claims);
  const status = row.status === 'open' ? '' : `<span class="st ${esc(row.status)}">${esc(row.status)}</span>`;
  const act = kind === 'tension' ? 'tension-status' : 'agreement-status';
  const ref = kind === 'tension' ? `data-tension="${esc(row.id)}"` : `data-agreement="${esc(row.id)}"`;
  const actions = [];
  if (row.stale || row.status !== 'confirmed') actions.push(`<button type="button" data-act="${act}" ${ref} data-status="confirmed">Confirm</button>`);
  if (row.stale || row.status !== 'dismissed') actions.push(`<button type="button" data-act="${act}" ${ref} data-status="dismissed">Dismiss</button>`);
  if (row.status !== 'open') actions.push(`<button type="button" data-act="${act}" ${ref} data-status="open">Reopen</button>`);
  actions.push(`<button type="button" data-ask-finding="${esc(row.id)}" data-ask-kind="${esc(kind)}" data-ai-action ${S.ai_enabled === true ? '' : 'disabled'}
    title="Ask about these claims, with their papers as the sources">Ask about this</button>`);
  return `<details class="frow ${esc(kind)} ${esc(row.status)}${row.stale ? ' is-stale' : ''}" data-finding="${esc(row.id)}"${open ? ' open' : ''}>
    <summary>
      <span class="ftag ${esc(kind)}">${esc(kind === 'tension' ? (row.kind || 'tension') : 'agreement')}</span>
      <span class="fsent">${esc(sentence)}</span>
      ${status}
      <span class="fwho">${esc(papers)}</span>
    </summary>
    <div class="fbody">
      ${row.note && row.note !== sentence ? `<p class="fnote">${esc(row.note)}</p>` : ''}
      ${row.stale ? '<p class="stale">A claim here was edited after this was found. Decide it again against the current text, or update the findings.</p>' : ''}
      ${unreviewed ? `<p class="frests">Rests on ${unreviewed} unreviewed claim${unreviewed === 1 ? '' : 's'}.</p>` : ''}
      <div class="${kind === 'tension' ? 'tpair' : 'tgroup'}">${(row.claims || []).map((claim) => tensionClaimCard(claim, row.id)).join('')}</div>
      <div class="cact">${actions.join('')}</div>
    </div>
  </details>`;
}

function findingsSynthesis(section) {
  const synth = section.synthesis;
  const editing = V.synthEditing === section.name;
  if (!synth && !editing) {
    return `<p class="flead empty">No synthesis yet. ${synthesizeButton(section.name)}</p>`;
  }
  const open = editing || findingsOpen.has(`synth:${section.name}`);
  const lead = FindingsModel.lead(synth);
  return `<details class="fsynth" data-synth-topic="${esc(section.name)}"${open ? ' open' : ''}>
    <summary><span class="flead">${esc(lead)}</span> <span class="fmore">Read the full synthesis</span></summary>
    ${synthesisBlock(section.name)}
  </details>`;
}

function findingsSection(section) {
  const counts = [`${section.papers} paper${section.papers === 1 ? '' : 's'}`,
    `${section.agreements.length} agreement${section.agreements.length === 1 ? '' : 's'}`,
    `${section.tensions.length} tension${section.tensions.length === 1 ? '' : 's'}`];
  const pill = section.stale
    ? '<span class="pill stale" title="A claim or tension under this topic changed after these were written">stale</span>'
    : section.synthesis ? '<span class="pill fresh">current</span>'
      : '<span class="pill fresh" title="Existing findings reflect the claims they cite.">findings current</span>';
  return `<section class="ftopic" id="finding-topic-${esc(section.name)}" data-topic="${esc(section.name)}">
    <h2><span class="tag" data-tag="${esc(section.name)}">#${esc(section.name)}</span> ${pill}
      <span class="fcounts">${esc(counts.join(' · '))}</span></h2>
    ${section.description ? `<p class="fdesc">${esc(section.description)}</p>` : ''}
    ${findingsSynthesis(section)}
    ${section.agreements.map((row) => findingRow(row, 'agreement')).join('')}
    ${section.tensions.map((row) => findingRow(row, 'tension')).join('')}
  </section>`;
}

function renderFindings() {
  const main = $('main');
  const scrollTop = main ? main.scrollTop : 0;
  const sections = FindingsModel.topics(S);
  const papers = S.papers.length;
  const agreements = (S.agreements || []).length;
  const tensions = (S.tensions || []).length;
  const ai = S.ai_enabled === true ? '' : 'disabled';
  let html = `<div class="paperhead findings-head">
    <h2>Findings</h2>
    <p class="pm">${papers} paper${papers === 1 ? '' : 's'} · ${agreements} agreement${agreements === 1 ? '' : 's'} · ${tensions} tension${tensions === 1 ? '' : 's'}</p>
    <p class="ps">What the papers hold on each topic, where they agree, and where they pull against each other.
      Open a row to see the claims it rests on.</p>
    <div class="row">
      <div class="ftoc">${sections.map((s) => `<button type="button" class="chip${s.stale ? ' is-stale' : ''}" data-act="findings-topic" data-topic="${esc(s.name)}">${esc(s.name)} · ${s.count}</button>`).join('')}</div>
      <button type="button" class="primary" data-act="update-findings" data-ai-action ${ai} style="margin-left:auto"
        title="Find tensions, then agreements, then write each topic's synthesis">Update findings</button>
    </div>
  </div>`;
  if (V.error) html += `<p class="warn">${esc(V.error)}</p>`;
  if (!sections.length) {
    html += S.ai_enabled === true
      ? '<p class="empty">Nothing written yet. Update findings asks the model, topic by topic, where the papers disagree, where they agree, and what they hold together.</p>'
      : '<p class="empty">Nothing written yet. Findings are written by the model; enable AI analysis in Settings to update them.</p>';
  } else {
    html += sections.map(findingsSection).join('');
  }
  $('content').innerHTML = html;
  if (main) main.scrollTop = scrollTop;
}

async function updateFindings() {
  V.error = null;
  // The passes read the corpus, so a claim the reader has deleted must be
  // gone first, and they must read the corpus the reader was looking at.
  const workspace = await settleDeletes();
  if (!workspace) return;
  try {
    const result = await api('/api/findings', {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...workspace }, body: '{}',
    });
    if (!result.queued) V.error = 'No topic has claims from two papers yet, so there is nothing to compare.';
  } catch (error) {
    V.error = `Could not start the update: ${error.message}`;
  }
  await refreshShowingError();
}

function openFindings() {
  if (V.view !== 'findings') showView('findings');
  syncHash(true);
  renderAll();
}

// The findings page with one row open and on screen: from a paper's panel.
function openFindingRow(id) {
  findingsOpen.add(id);
  openFindings();
  const row = document.querySelector(`.frow[data-finding="${CSS.escape(id)}"]`);
  row?.scrollIntoView({block: 'center'});
  row?.querySelector('summary')?.focus({preventScroll: true});
}

$('btn-findings').addEventListener('click', openFindings);

// A row's open state is the reader's, kept across the redraws listed above.
$('content').addEventListener('toggle', (event) => {
  const row = event.target;
  if (!(row instanceof HTMLDetailsElement)) return;
  const key = row.dataset.finding || (row.dataset.synthTopic ? `synth:${row.dataset.synthTopic}` : null);
  if (!key) return;
  if (row.open) findingsOpen.add(key); else findingsOpen.delete(key);
}, true);
