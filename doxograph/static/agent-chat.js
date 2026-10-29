'use strict';

// "Ask the library": a conversation with the selected subscription agent, which reads this
// workspace through read-only tools. Conversations are scoped by workspace and live only in
// this tab. Answers can be kept as workspace-local research notes.
// Loaded after app.js, which owns `$`, `api`, `esc`, `paragraphs`, and `S`.
const agentChats = new Map();
let activeAgentChat = null;

function agentChatFor(workspace) {
  if (!agentChats.has(workspace)) agentChats.set(workspace, { workspace, messages: [], draft: '', busy: false, error: '' });
  return agentChats.get(workspace);
}

function renderAgentChat() {
  const chat = activeAgentChat;
  if (!chat) return;
  const messages = chat.messages.map((m, index) => {
    const assistant = m.role === 'assistant';
    const message = document.createElement('div');
    message.className = `agent-message ${assistant ? 'agent-answer' : 'agent-question'}`;
    message.innerHTML = `<div class="agent-message-head"><span class="who">${assistant ? esc(m.assistant || 'Assistant') : 'You'}</span>
      ${assistant ? `<div class="agent-answer-actions">
        <button type="button" data-agent-action="${m.savedNote ? 'open' : 'save'}" data-message="${index}" ${m.saving ? 'disabled' : ''}>${m.savedNote ? 'Open saved note' : m.saving ? 'Saving…' : 'Save as research note'}</button>
        <span class="hint" role="status">${m.savedNote ? 'Saved in Research notes' : ''}</span>
      </div>` : ''}</div>
      ${m.saveError ? `<p class="warn" role="alert">${esc(m.saveError)}</p>` : ''}
      <div class="agent-content ${assistant ? 'note-prose' : ''}"></div>
      ${(m.activity || []).length ? `<details class="activity"><summary>Library lookups (${m.activity.length})</summary><ul>${m.activity.map(a => `<li>${esc(a)}</li>`).join('')}</ul></details>` : ''}`;
    const content = message.querySelector('.agent-content');
    if (assistant) content.replaceChildren(NoteWriting.render(m.content, chat.workspace));
    else content.innerHTML = paragraphs(m.content).map(p => `<p>${esc(p)}</p>`).join('');
    return message;
  });
  $('agent-messages').replaceChildren(...messages);
  $('agent-question').value = chat.draft;
  $('agent-question').disabled = chat.busy;
  $('agent-clear').disabled = chat.busy || chat.messages.some(m => m.saving);
  syncAgentChatControls();
}

function libraryAnswerNote(chat, index) {
  const message = chat.messages[index];
  const question = chat.messages[index - 1]?.content || 'Library synthesis';
  const title = Array.from(question.replace(/\s+/g, ' ').trim()).slice(0, 160).join('');
  const lookups = (message.activity || []).length
    ? `\n\n## Library lookups\n\n${message.activity.map(a => `- ${a}`).join('\n')}` : '';
  return {title, body: `## Question\n\n${question}\n\n## Answer\n\n${message.content}\n\n---\n\nSaved from Ask the library · ${message.assistant || 'Assistant'} · ${message.created || new Date().toISOString()}${lookups}`};
}

$('agent-messages').addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-agent-action]');
  const chat = activeAgentChat;
  if (!button || !chat) return;
  const index = Number(button.dataset.message);
  const message = chat.messages[index];
  if (message?.role !== 'assistant' || message.saving) return;
  if (button.dataset.agentAction === 'open') {
    message.saving = true;
    message.saveError = '';
    renderAgentChat();
    try {
      const {notes} = await api('/api/research-notes', {headers: {'X-Doxograph-Workspace': chat.workspace}});
      if (!notes.some(note => note.id === message.savedNote)) {
        message.savedNote = null;
        message.saveOperation = null;
        message.saveError = 'The saved note was deleted. You can save this answer again.';
        return;
      }
      // A saved answer always belongs to the workspace it was generated in.
      if (workspaceClient.workspaceId !== chat.workspace) {
        await switchWorkspace(chat.workspace);
        if (workspaceClient.workspaceId !== chat.workspace) return;
      }
      $('library-agent').close();
      await ResearchNotes.open(message.savedNote);
    } catch (error) { message.saveError = `Could not open this note: ${error.message}`; }
    finally {
      message.saving = false;
      if (activeAgentChat === chat) renderAgentChat();
    }
    return;
  }
  if (message.savedNote) return;
  message.saving = true;
  message.saveError = '';
  renderAgentChat();
  try {
    // Reuse the operation even when the server committed but its response was lost.
    message.saveOperation ||= Array.from(crypto.getRandomValues(new Uint8Array(16)),
      byte => byte.toString(16).padStart(2, '0')).join('');
    const note = await api('/api/research-notes', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Doxograph-Workspace': chat.workspace,
        'Idempotency-Key': message.saveOperation},
      body: JSON.stringify(libraryAnswerNote(chat, index)),
    });
    message.savedNote = note.id;
  } catch (error) { message.saveError = `Could not save this answer: ${error.message}`; }
  finally {
    message.saving = false;
    if (activeAgentChat === chat) renderAgentChat();
  }
});

function syncAgentChatControls() {
  const chat = activeAgentChat;
  if (!chat) return;
  const enabled = S.ai_enabled === true;
  const ready = ['claude_code', 'codex'].includes(S.ai_provider);
  const setup = [
    ...(!enabled ? ['Enable AI analysis in Settings.'] : []),
    ...(!ready ? ['Choose Claude subscription or ChatGPT subscription (Codex) in Settings → Model access.'] : []),
  ].join(' ');
  $('agent-send').disabled = chat.busy || !enabled || !ready;
  $('agent-settings').hidden = !setup;
  $('agent-codex-guidance').hidden = S.ai_provider !== 'codex';
  $('agent-status').textContent = chat.busy ? 'Looking through the library…' : setup || chat.error;
}

$('btn-agent').addEventListener('click', () => {
  activeAgentChat = agentChatFor(workspaceClient.workspaceId);
  renderAgentChat();
  $('library-agent').showModal();
  $('agent-question').focus();
});
$('agent-question').addEventListener('input', () => { if (activeAgentChat) activeAgentChat.draft = $('agent-question').value; });
$('agent-close').addEventListener('click', () => $('library-agent').close());
$('agent-settings').addEventListener('click', () => {
  $('library-agent').close();
  openSettings();
  $(S.ai_enabled === true ? 'ai-access' : 'ai-enabled').focus();
});
$('agent-clear').addEventListener('click', () => {
  const chat = activeAgentChat;
  if (!chat || chat.busy || chat.messages.some(m => m.saving)) return;
  chat.messages = []; chat.draft = ''; chat.error = '';
  renderAgentChat();
});
$('agent-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const chat = activeAgentChat;
  if (!chat || chat.busy || S.ai_enabled !== true || !['claude_code', 'codex'].includes(S.ai_provider) || !chat.draft.trim()) return;
  const messages = [...chat.messages, { role: 'user', content: chat.draft.trim() }];
  const lengths = messages.map((message) => Array.from(message.content).length);
  if (messages.length > 40 || lengths.some((length) => length > 32000)
      || lengths.reduce((total, length) => total + length, 0) > 80000) {
    chat.error = 'Start a new conversation; this one is too long.';
    renderAgentChat();
    return;
  }
  chat.busy = true; chat.error = '';
  renderAgentChat();
  try {
    const result = await api('/api/agent/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Doxograph-Workspace': chat.workspace },
      body: JSON.stringify({ messages: messages.map(({ role, content }) => ({ role, content })) }),
    });
    chat.messages = [...messages, { role: 'assistant', content: result.answer, assistant: result.assistant || 'Assistant',
      activity: result.activity || [], created: new Date().toISOString() }];
    chat.draft = '';
  } catch (error) { chat.error = error.message; }
  finally {
    chat.busy = false;
    if (activeAgentChat === chat) renderAgentChat();
  }
});
