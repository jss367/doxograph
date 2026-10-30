'use strict';

// "Ask the library": conversations with a subscription agent, which reads this workspace
// through read-only tools. Conversations are saved per workspace, each with its own agent.
// Answers can be kept as workspace-local research notes.
// Loaded after app.js, which owns `$`, `api`, `esc`, and `S`, and papers.js, which owns `paragraphs` and `sendOnEnter`.
const agentChats = ChatThreads.create({
  prefix: 'agent',
  agents: () => ['claude_code', 'codex'],
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
    if (assistant) content.replaceChildren(NoteWriting.render(m.content, agentChats.workspace));
    else content.innerHTML = paragraphs(m.content).map(p => `<p>${esc(p)}</p>`).join('');
    content.insertAdjacentHTML('beforeend', `<div class="chat-attachments">${ChatThreads.attachmentsHTML(m.attachments || [])}</div>`);
    return message;
  });
  $('agent-messages').replaceChildren(...messages);
  $('agent-question').value = chat.draft;
  $('agent-question').disabled = chat.busy;
  syncAgentChatControls();
}

function libraryAnswerNote(chat, index) {
  const message = chat.messages[index];
  const question = chat.messages[index - 1]?.content || chat.messages[index - 1]?.attachments?.[0]?.name || 'Library synthesis';
  const title = Array.from(question.replace(/\s+/g, ' ').trim()).slice(0, 160).join('');
  const lookups = (message.activity || []).length
    ? `\n\n## Library lookups\n\n${message.activity.map(a => `- ${a}`).join('\n')}` : '';
  return {title, body: `## Question\n\n${question}\n\n## Answer\n\n${message.content}\n\n---\n\nSaved from Ask the library · ${message.assistant || 'Assistant'} · ${message.created || new Date().toISOString()}${lookups}`};
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
  $('agent-send').disabled = chat.busy || chat.reading || !enabled;
  $('agent-settings').hidden = !setup;
  $('agent-codex-guidance').hidden = chat.agent !== 'codex';
  $('agent-status').textContent = chat.busy ? 'Looking through the library…' : chat.reading ? 'Reading attachments…' : setup || chat.error || agentChats.issues.join(' ');
}

$('btn-agent').addEventListener('click', () => {
  agentChats.open(workspaceClient.workspaceId, null);
  $('library-agent').showModal();
  $('agent-question').focus();
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
  agentChats.ask(async (chat, messages) => {
    const result = await api('/api/agent/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Doxograph-Workspace': workspace },
      body: JSON.stringify({ messages, agent: chat.agent }),
    });
    return { content: result.answer, assistant: result.assistant || 'Assistant', model: result.model || '',
      activity: result.activity || [] };
  });
});
