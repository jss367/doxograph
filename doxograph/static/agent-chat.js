'use strict';

// Shared Ask panel: direct paper input or scoped read-only retrieval.
// Conversations and their evidence stay in their originating workspace.
// Answers can be kept as workspace-local research notes.
// Loaded after app.js, which owns `$`, `api`, `esc`, and `S`, and papers.js, which owns `paragraphs` and `sendOnEnter`.
const agentChats = ChatThreads.create({
  prefix: 'agent',
  agents: () => ['api', 'claude_code', 'codex'],
  unified: true,
  render: renderAgentChat,
});
const activeAgentChat = () => agentChats.active;

function renderAgentChat(chat) {
  const messages = chat.messages.map((m, index) => {
    const assistant = m.role === 'assistant';
    const message = document.createElement('div');
    message.className = `agent-message ${assistant ? 'agent-answer' : 'agent-question'}`;
    message.innerHTML = `<div class="agent-message-head"><span class="who">${assistant ? esc(m.assistant || 'Assistant') : 'You'}</span>
      ${assistant ? `<div class="agent-answer-actions">
        <button type="button" data-agent-action="${m.saved_note ? 'open' : 'save'}" data-message="${index}" ${m.saving ? 'disabled' : ''}>${m.saved_note ? 'Open saved note' : m.saving ? 'Saving…' : 'Save as research note'}</button>
        <span class="hint" role="status">${m.saved_note ? 'Saved in Research notes' : ''}</span>
      </div>` : ''}</div>
      ${m.saveError ? `<p class="warn" role="alert">${esc(m.saveError)}</p>` : ''}
      <div class="agent-content ${assistant ? 'note-prose' : ''}"></div>
      ${(m.activity || []).length ? `<details class="activity"><summary>Library lookups (${m.activity.length})</summary><ul>${m.activity.map(a => `<li>${esc(a)}</li>`).join('')}</ul></details>` : ''}`;
    const content = message.querySelector('.agent-content');
    if (assistant) {
      content.replaceChildren(NoteWriting.render(answerMarkdown(m), agentChats.workspace));
      renderCitations(content, m);
      content.append(evidenceDetails(m));
    }
    else {
      content.innerHTML = paragraphs(m.content).map(p => `<p>${esc(p)}</p>`).join('');
      if (m.scope) {
        const label = document.createElement('p'); label.className = 'hint';
        const previous = chat.messages.slice(0, index).findLast(m => m.role === 'user' && m.scope)?.scope;
        const added = previous && m.scope.mode !== 'library' ? m.scope.papers.filter(p => !previous.papers.includes(p)) : [];
        label.textContent = previous && previous.mode !== 'library' && m.scope.mode === 'library' ? 'Expanded search to the library'
          : added.length ? `Added ${added.map(key => catalog().papers.find(p => p.key === key)?.title || key).join(', ')} · ${scopeLabel(m.scope)}` : scopeLabel(m.scope);
        content.prepend(label);
      }
    }
    content.insertAdjacentHTML('beforeend', `<div class="chat-attachments">${ChatThreads.attachmentsHTML(m.attachments || [])}</div>`);
    return message;
  });
  $('agent-messages').replaceChildren(...messages);
  $('agent-question').value = chat.draft;
  $('agent-question').disabled = chat.busy;
  renderAskScope(chat);
  syncAgentChatControls();
}

function libraryAnswerNote(chat, index) {
  const message = chat.messages[index];
  const question = chat.messages[index - 1]?.content || chat.messages[index - 1]?.attachments?.[0]?.name || 'Library synthesis';
  const title = Array.from(question.replace(/\s+/g, ' ').trim()).slice(0, 160).join('');
  const lookups = (message.activity || []).length
    ? `\n\n## Library lookups\n\n${message.activity.map(a => `- ${a}`).join('\n')}` : '';
  const evidence = (message.evidence || []).map(e => `- ${e.title || e.paper} · ${e.kind}${e.page ? ` · PDF page ${e.page}` : ''}${e.carried ? ` · from turn ${e.turn}` : ''}${e.stale ? ' · stale analysis' : ''}${e.status ? ` · ${e.status}` : ''}\n\n${e.text || e.quote || ''}`).join('\n\n');
  const scope = message.scope || chat.messages[index - 1]?.scope;
  const members = scope ? `\n\nAvailable papers at submission:\n${scope.papers.map(key => `- ${message.evidence?.find(e => e.paper === key)?.title || key} (${key})`).join('\n')}` : '';
  const provenance = `\n\n## Sources\n\n${scope ? scopeLabel(scope) : 'Evidence details unavailable for this older answer'}${members}\n\n${evidence}\n\nModel: ${message.model || 'Not recorded'}`;
  let answer = answerMarkdown(message);
  for (const c of message.citations || []) {
    const e = (message.evidence || []).find(e => e.ref === c.ref);
    const url = c.valid ? citationUrl(c, agentChats.workspace) : '';
    answer = answer.replaceAll(c.token, c.valid ? `[${e?.title || c.paper}${c.page ? `, PDF page ${c.page}` : ''}](${url})` : `${c.token} (unverified reference)`);
  }
  return {title, body: `## Question\n\n${question}\n\n## Answer\n\n${answer}\n\n---\n\nSaved from Ask the library · ${message.assistant || 'Assistant'} · ${message.created || new Date().toISOString()}${lookups}${provenance}`};
}

$('agent-messages').addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-agent-action]');
  const chat = activeAgentChat(), workspace = agentChats.workspace;
  if (!button || !chat) return;
  const index = Number(button.dataset.message);
  const message = chat.messages[index];
  if (message?.role !== 'assistant' || message.saving) return;
  if (button.dataset.agentAction === 'open') {
    message.saving = true;
    message.saveError = '';
    agentChats.sync();
    try {
      const {notes} = await api('/api/research-notes', {headers: {'X-Doxograph-Workspace': workspace}});
      if (!notes.some(note => note.id === message.saved_note)) {
        message.saved_note = null;
        message.note_operation = null;
        message.saveError = 'The saved note was deleted. You can save this answer again.';
        await agentChats.save(chat);
        return;
      }
      // A saved answer always belongs to the workspace it was generated in.
      if (workspaceClient.workspaceId !== workspace) {
        await switchWorkspace(workspace);
        if (workspaceClient.workspaceId !== workspace) return;
      }
      $('library-agent').close();
      await ResearchNotes.open(message.saved_note);
    } catch (error) { message.saveError = `Could not open this note: ${error.message}`; }
    finally {
      message.saving = false;
      agentChats.sync();
    }
    return;
  }
  if (message.saved_note) return;
  message.saving = true;
  message.saveError = '';
  agentChats.sync();
  try {
    // Reuse the operation even when the server committed but its response was lost. Post only once
    // the conversation holding it is on disk, so a retry after a reload also returns the committed note.
    message.note_operation ||= Array.from(crypto.getRandomValues(new Uint8Array(16)),
      byte => byte.toString(16).padStart(2, '0')).join('');
    if (!await agentChats.save(chat)) throw new Error('the conversation could not be saved first. Try again.');
    const note = await api('/api/research-notes', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Doxograph-Workspace': workspace,
        'Idempotency-Key': message.note_operation},
      body: JSON.stringify(libraryAnswerNote(chat, index)),
    });
    message.saved_note = note.id;
    await agentChats.save(chat);
  } catch (error) { message.saveError = `Could not save this answer: ${error.message}`; }
  finally {
    message.saving = false;
    agentChats.sync();
  }
});

function syncAgentChatControls() {
  const chat = activeAgentChat();
  if (!chat) return;
  const enabled = S.ai_enabled === true;
  const setup = enabled ? '' : 'Enable AI analysis in Settings.';
  const problem = scopeProblem(chat);
  $('agent-send').disabled = chat.busy || chat.reading || !enabled || Boolean(problem);
  // Polls refresh availability without replacing focused scope controls.
  $('agent-claims-only').hidden = !claimsFallback(chat);
  $('agent-settings').hidden = !setup;
  $('agent-codex-guidance').hidden = chat.agent !== 'codex';
  $('agent-status').textContent = chat.busy ? (chat.scope.mode === 'paper' ? 'Reading the paper…' : 'Looking through selected sources…') : chat.reading ? 'Reading attachments…' : setup || chat.error || problem || agentChats.issues.join(' ');
}

$('btn-agent').addEventListener('click', () => {
  openAsk(null);
});
sendOnEnter($('agent-question'));
$('agent-question').addEventListener('input', () => { if (activeAgentChat()) activeAgentChat().draft = $('agent-question').value; });
$('agent-close').addEventListener('click', () => $('library-agent').close());
$('agent-settings').addEventListener('click', () => {
  $('library-agent').close();
  openSettings();
  $('ai-enabled').focus();
});
$('agent-form').addEventListener('submit', (event) => {
  event.preventDefault();
  if (S.ai_enabled !== true) return;
  const workspace = agentChats.workspace;
  if (scopeProblem(activeAgentChat())) return;
  agentChats.ask(async (chat, messages, scope) => {
    const result = await api('/api/ask', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Doxograph-Workspace': workspace },
      body: JSON.stringify({ messages, agent: chat.agent, scope, conversation_id: chat.id }),
    });
    return { content: result.answer, assistant: result.assistant || 'Assistant', model: result.model || '',
      activity: result.activity || [], scope: result.scope, evidence: result.evidence, citations: result.citations || [] };
  });
});

// The picker uses the workspace captured by the conversation, even if another
// workspace is now open behind the dialog. Never include personal note fields.
const askCatalogs = new Map();
let picker = null;
function catalog() {
  if (agentChats.workspace === workspaceClient.workspaceId) return S;
  return askCatalogs.get(agentChats.workspace) || {papers: [], claims: []};
}
function scopeLabel(scope) {
  const focus = (scope.focus || []).filter(f => f.claim).map(f => f.text || catalog().claims.find(c => c.id === f.claim && c.paper === f.paper)?.text || f.claim).join('; ');
  const about = focus ? ` · About: ${focus}` : '';
  if (scope.mode === 'library') return `Search: entire library · ${scope.papers.length} papers${about}`;
  const titles = scope.papers.map(key => catalog().papers.find(p => p.key === key)?.title || key);
  return `${scope.mode === 'paper' ? 'Paper' : 'Sources'}: ${titles.join(', ') || 'No sources selected'}${about}`;
}
function claimsFallback(chat) {
  return chat.scope.mode === 'paper' && catalog().claims.some(c => c.paper === chat.scope.papers[0])
    && (!catalog().papers.find(p => p.key === chat.scope.papers[0])?.has_pdf || /no readable|PDF.*unavailable/i.test(chat.error || ''));
}
function scopeProblem(chat) {
  if (!chat) return '';
  const scope = chat.scope;
  if (scope.mode !== 'library' && scope.papers.length > 200) return 'Select up to 200 papers, or search the library.';
  if (scope.mode !== 'library' && !scope.papers.length) return 'Add a source before asking a question.';
  if (scope.mode !== 'library' && scope.papers.some(key => !catalog().papers.some(p => p.key === key))) return 'Source unavailable. Repair the selection before asking another question.';
  if ((scope.focus || []).some(f => f.claim && !catalog().claims.some(c => c.paper === f.paper && c.id === f.claim))) return 'A focused claim is unavailable. Repair the selection.';
  if (scope.mode !== 'paper' && chat.agent === 'api') return 'Retrieval needs Claude or ChatGPT subscription. Choose a supported agent above; your draft is kept.';
  if (scope.mode === 'paper' && !catalog().papers.find(p => p.key === scope.papers[0])?.has_pdf) return 'No PDF is available. Choose Use extracted claims only if this paper has claims.';
  return '';
}
function renderAskScope(chat) {
  const scope = chat.scope;
  $('library-agent-title').textContent = scope.mode === 'library' ? 'Ask the library' : 'Ask about sources';
  const papers = catalog().papers;
  const chips = scope.papers.map(key => {
    const paper = papers.find(p => p.key === key);
    return `<span class="ask-chip">${esc(paper?.title || key)}${paper ? '' : ' · Source unavailable'} <button type="button" data-remove-source="${esc(key)}" aria-label="Remove ${esc(paper?.title || key)}">×</button></span>`;
  });
  const focus = (scope.focus || []).filter(f => f.claim).map(f => {
    const claim = catalog().claims.find(c => c.paper === f.paper && c.id === f.claim);
    return `<details><summary>About: ${esc(f.text || claim?.text || 'Claim unavailable')}</summary><p>${esc(papers.find(p => p.key === f.paper)?.title || f.paper)}</p><p>${esc(claim?.quote || '')}</p><p class="hint">${esc(claim?.locator || '')} · ${claim?.reviewed ? 'Reviewed' : 'Unreviewed'}</p></details>`;
  }).join('');
  $('agent-scope').innerHTML = focus + (scope.mode === 'library'
    ? `<p>Search: entire library · ${papers.length} papers</p>`
    : `<p>${scope.focus?.some(f => f.claim) ? 'Evidence from originating papers' : 'Selected sources'}</p><div class="ask-chips">${chips.slice(0, 3).join('')}</div>${chips.length > 3 ? `<details><summary>Show all ${chips.length} sources</summary><div class="ask-chips">${chips.slice(3).join('')}</div></details>` : ''}`);
  $('agent-expand').hidden = scope.mode === 'library';
  $('agent-expand').disabled = false;
  $('agent-add').disabled = false;
  $('agent-add').textContent = scope.mode === 'library' ? 'Choose specific sources' : 'Add sources';
  $('agent-claims-only').hidden = !claimsFallback(chat);
  $('agent-claims-only').disabled = false;
  const images = chat.agent === 'claude_code' || (chat.agent === 'api' && (!S.provider || S.provider === 'anthropic'));
  $('agent-input-format').textContent = scope.mode === 'paper'
    ? images ? 'Sends the full PDF, including page images, to the chosen provider.' : 'Sends extracted paper text. Figures and unreadable scanned pages are unavailable.'
    : 'Retrieves claims and text passages as needed. Figures and page images are unavailable; a search is not an exhaustive review.';
  const starters = scope.focus?.some(f => f.claim) ? ['What supports this claim?', 'What qualifications does the paper give?', 'Find conflicting evidence']
    : scope.mode === 'library' ? ['What does this library say about…?', 'What remains unresolved?']
    : scope.papers.length > 1 ? ['Compare methods', 'Where do their findings differ?'] : ['Explain the main result', 'What are the limitations?'];
  $('agent-starters').innerHTML = chat.messages.length ? '' : starters.map(text => `<button type="button" data-starter="${esc(text)}" ${chat.busy ? 'disabled' : ''}>${esc(text)}</button>`).join('');
}
async function openAsk(scope) {
  askCatalogs.set(workspaceClient.workspaceId, {papers: structuredClone(S.papers), claims: S.claims.map(c => ({paper:c.paper, id:c.id, text:c.text, quote:c.quote, locator:c.locator, tags:c.tags, reviewed:c.reviewed}))});
  const opening = agentChats.open(workspaceClient.workspaceId, null, scope ? {fresh: true, scope} : {});
  if (!$('library-agent').open) $('library-agent').showModal();
  $('agent-question').focus();
  await opening;
}
async function changeAskScope(scope) {
  const chat = activeAgentChat();
  if (!chat) return false;
  const previous = chat.scope;
  const narrowed = previous.mode === 'library' ? scope.mode !== 'library'
    : scope.mode !== 'library' && previous.papers.some(p => !scope.papers.includes(p));
  if ((chat.messages.length || chat.busy) && narrowed) {
    if (!await confirmDialog('Start a new conversation with these sources?', {note: 'Your draft carries over. Earlier answers may contain evidence from removed sources, so the old transcript stays in its original conversation.', ok: 'Start new conversation'})) return false;
    agentChats.newScope(scope);
  } else {
    chat.scope = scope;
    agentChats.sync();
  }
  return true;
}
$('agent-scope').addEventListener('click', event => {
  const button = event.target.closest('[data-remove-source]');
  if (!button) return;
  const scope = structuredClone(activeAgentChat().scope);
  scope.papers = scope.papers.filter(p => p !== button.dataset.removeSource);
  scope.focus = scope.focus.filter(f => f.paper !== button.dataset.removeSource);
  if (scope.mode === 'paper') scope.mode = 'selected';
  changeAskScope(scope);
});
$('agent-expand').addEventListener('click', () => changeAskScope({...structuredClone(activeAgentChat().scope), mode:'library'}));
$('agent-claims-only').addEventListener('click', () => changeAskScope({...structuredClone(activeAgentChat().scope), mode:'selected'}));
$('agent-starters').addEventListener('click', async event => {
  const text = event.target.closest('[data-starter]')?.dataset.starter;
  if (!text) return;
  if (text === 'Find conflicting evidence' && activeAgentChat().scope.mode !== 'library') {
    if (!await confirmDialog('Search the rest of the library for conflicting evidence?', {note:'The claim stays in focus; evidence expands to this workspace.', ok:'Expand search'})) return;
    await changeAskScope({...structuredClone(activeAgentChat().scope), mode:'library'});
  }
  activeAgentChat().draft = text; agentChats.sync(); $('agent-question').focus();
});
document.addEventListener('click', event => {
  const button = event.target.closest('[data-ask-claim]');
  if (button) openAsk({mode:'paper', papers:[button.dataset.paper], focus:[{paper:button.dataset.paper, claim:button.dataset.askClaim}]});
});
$('ask-selection').addEventListener('click', () => {
  const selection = ResearchTools.selection();
  if (selection.papers.length) openAsk({mode: selection.papers.length === 1 ? 'paper' : 'selected', ...selection});
});
function renderSourcePicker() {
  const q = $('source-search').value.trim().toLocaleLowerCase();
  const data = catalog();
  const matches = (...values) => !q || values.join(' ').toLocaleLowerCase().includes(q);
  const results = [];
  for (const paper of data.papers) {
    const tags = data.claims.filter(c => c.paper === paper.key).flatMap(c => c.tags || []);
    if (matches(paper.title, ...(paper.authors || []), ...tags)) results.push(`<label class="source-result"><input type="checkbox" data-source-paper="${esc(paper.key)}" ${picker.papers.has(paper.key) ? 'checked' : ''}> Paper · ${esc(paper.title || paper.key)}</label>`);
  }
  for (const c of data.claims) {
    const p = data.papers.find(p => p.key === c.paper);
    if (matches(c.text, p?.title, ...(p?.authors || []), ...(c.tags || []))) results.push(`<label class="source-result"><input type="checkbox" data-source-claim="${esc(c.id)}" data-source-parent="${esc(c.paper)}" ${picker.focus.some(f => f.paper === c.paper && f.claim === c.id) ? 'checked' : ''}> Claim · ${esc(c.text)} <span class="hint">${esc(p?.title || c.paper)}</span></label>`);
  }
  $('source-results').innerHTML = results.slice(0, 100).join('') || '<p>No matching sources.</p>';
  $('source-selection-count').textContent = `${picker.papers.size} papers selected · ${picker.focus.length} claims in focus${results.length > 100 ? ' · Showing first 100 results; narrow your search' : ''}`;
}
$('agent-add').addEventListener('click', () => {
  const scope = activeAgentChat().scope;
  picker = {papers: new Set(scope.mode === 'library' ? [] : scope.papers), focus: structuredClone(scope.focus || []).filter(f => f.claim)};
  picker.focus.forEach(f => picker.papers.add(f.paper));
  $('source-search').value = ''; renderSourcePicker(); $('source-picker').showModal(); $('source-search').focus();
});
$('source-search').addEventListener('input', renderSourcePicker);
$('source-results').addEventListener('change', event => {
  const input = event.target;
  if (input.dataset.sourcePaper) {
    const paper = input.dataset.sourcePaper;
    if (input.checked) picker.papers.add(paper);
    else { picker.papers.delete(paper); picker.focus = picker.focus.filter(f => f.paper !== paper); }
  } else if (input.dataset.sourceClaim) {
    const paper = input.dataset.sourceParent, claim = input.dataset.sourceClaim;
    picker.focus = picker.focus.filter(f => !(f.paper === paper && f.claim === claim));
    if (input.checked) { picker.focus.push({paper, claim}); picker.papers.add(paper); }
  }
  // Preserve keyboard focus while updating the paired paper/claim checkboxes.
  document.querySelectorAll('[data-source-paper]').forEach(c => { c.checked = picker.papers.has(c.dataset.sourcePaper); });
  document.querySelectorAll('[data-source-claim]').forEach(c => { c.checked = picker.focus.some(f => f.paper === c.dataset.sourceParent && f.claim === c.dataset.sourceClaim); });
  $('source-selection-count').textContent = `${picker.papers.size} papers selected · ${picker.focus.length} claims in focus`;
});
$('source-cancel').addEventListener('click', () => $('source-picker').close());
$('source-apply').addEventListener('click', async () => {
  const scope = {mode:picker.papers.size === 1 ? 'paper' : 'selected', papers:[...picker.papers], focus:picker.focus};
  $('source-picker').close(); await changeAskScope(scope); $('agent-question').focus();
});
function citationUrl(c, workspace) {
  if (c.page) return `/pdf/${encodeURIComponent(c.paper)}?workspace=${encodeURIComponent(workspace)}#page=${c.page}`;
  return `/#${new URLSearchParams({ws:workspace, paper:c.paper, ...(c.claim ? {sel:c.claim} : {})})}`;
}
// New answers use validated evidence references for links. A raw Markdown link
// from the model must not bypass scope/citation checks, including in saved notes.
function answerMarkdown(message) {
  if (message.evidence == null) return message.content; // Legacy answers keep their original rendering.
  let body = message.content;
  const tokens = NoteWriting.markdown.lexer(body);
  NoteWriting.markdown.walkTokens(tokens, token => {
    if (token.type === 'link') {
      const label = String(token.text || 'Reference').replaceAll('`', "'");
      body = body.replaceAll(token.raw, `Unverified link: \`${label}\``);
    }
  });
  return body;
}
function renderCitations(container, message) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    if (!node.textContent.includes('[[e:') || node.parentElement.closest('code, pre')) continue;
    const pieces = node.textContent.split(/(\[\[e:[^\]\n]{1,250}\]\])/g);
    const fragment = document.createDocumentFragment();
    for (const piece of pieces) {
      if (!piece.startsWith('[[e:')) { fragment.append(document.createTextNode(piece)); continue; }
      const c = (message.citations || []).find(c => c.token === piece);
      const e = (message.evidence || []).find(e => e.ref === c?.ref);
      if (!c?.valid || !e) {
        const warning = document.createElement('span'); warning.className = 'citation-warning';
        warning.textContent = `${piece} (unverified reference)`; fragment.append(warning); continue;
      }
      const button = document.createElement('button'); button.type = 'button'; button.className = 'citation-link';
      button.textContent = `${e.title || e.paper}${c.page ? ` · p. ${c.page}` : e.claim ? ' · claim' : ''}`;
      button.addEventListener('click', () => showCitation(c, e, message)); fragment.append(button);
    }
    node.replaceWith(fragment);
  }
}
function showCitation(c, e, message) {
  const paper = catalog().papers.find(p => p.key === e.paper);
  const available = paper && (!c.claim || catalog().claims.some(claim => claim.paper === e.paper && claim.id === c.claim));
  const snapshot = e.kind === 'document' && c.page ? (message.evidence || []).find(p => p.kind === 'passage' && p.paper === e.paper && p.page === c.page)?.text : e.text;
  $('citation-body').innerHTML = `<p>${esc(e.title || e.paper)}</p><p class="hint">${e.carried ? `From turn ${e.turn}` : 'Supplied for this answer'} · ${esc(e.kind)}${c.page ? ` · PDF page ${c.page}` : ''}</p>
    ${snapshot ? `<h3>Evidence preserved with the answer</h3><blockquote>${esc(snapshot)}</blockquote>` : '<p>No historical excerpt is available. The link opens the current document.</p>'}
    ${e.quote ? `<blockquote>${esc(e.quote)}</blockquote>` : ''}
    ${e.kind === 'analysis' ? `<p>${e.stale ? 'Stale analysis · ' : ''}${esc(e.status)} · Based on ${(e.sources || []).map(p => esc(catalog().papers.find(x => x.key === p)?.title || p)).join(', ')}</p>` : ''}
    ${available ? `<a href="${esc(citationUrl(c, agentChats.workspace))}" target="_blank" rel="noopener">Open current ${c.page ? 'PDF page' : c.claim ? 'claim' : 'paper'} ↗</a>` : '<p>Source unavailable. The preserved evidence remains here.</p>'}`;
  $('citation-detail').showModal();
}
$('citation-close').addEventListener('click', () => $('citation-detail').close());
function evidenceDetails(message) {
  const details = document.createElement('details'); details.className = 'activity';
  const summary = document.createElement('summary'); summary.textContent = 'Evidence used'; details.append(summary);
  if (!message.evidence) { const p = document.createElement('p'); p.textContent = 'Evidence details unavailable for this older answer'; details.append(p); return details; }
  const fresh = message.evidence.filter(e => !e.carried);
  const retrieved = new Set(fresh.filter(e => e.kind === 'passage' && !e.format).map(e => e.paper));
  const doc = fresh.find(e => e.kind === 'document');
  const p = document.createElement('p');
  p.textContent = doc ? `Supplied the full paper as ${doc.format}.` : `Retrieved passages from ${retrieved.size} of ${message.scope?.papers.length || 0} available papers. Claims and metadata are listed separately.`;
  details.append(p);
  for (const e of message.evidence) {
    const item = document.createElement('div'); item.className = 'evidence-item';
    const label = document.createElement('p');
    label.textContent = `${e.title || e.paper} · ${e.kind}${e.page ? ` · PDF page ${e.page}` : ''}${e.carried ? ` · from turn ${e.turn}` : ''}${e.kind === 'claim' ? ` · ${e.reviewed ? 'Reviewed' : 'Unreviewed'} · ${e.quote_verified === true ? 'Quote matched' : 'Quote unverified'}` : ''}${e.stale ? ' · Stale analysis' : ''}${e.status ? ` · ${e.status}` : ''}`;
    item.append(label);
    if (e.text || e.quote) { const excerpt = document.createElement('blockquote'); excerpt.textContent = e.text || e.quote; item.append(excerpt); }
    details.append(item);
  }
  return details;
}
