'use strict';

// "Ask the library": a conversation with the Claude subscription agent, which reads this
// workspace through read-only tools. Conversations are scoped by workspace and live only in
// this tab. Loaded after app.js, which owns `$`, `api`, `esc`, `paragraphs`, and `S`.
const agentChats = new Map();
let activeAgentChat = null;

function agentChatFor(workspace) {
  if (!agentChats.has(workspace)) agentChats.set(workspace, { workspace, messages: [], draft: '', busy: false, error: '' });
  return agentChats.get(workspace);
}

function renderAgentChat() {
  const chat = activeAgentChat;
  if (!chat) return;
  $('agent-messages').innerHTML = chat.messages.map((m) =>
    `<div class="note"><span class="who">${m.role === 'user' ? 'You' : esc(m.assistant || 'Claude')}</span>
      ${paragraphs(m.content).map((p) => `<p>${esc(p)}</p>`).join('')}
      ${(m.activity || []).length ? `<p class="activity">${m.activity.map(esc).join(' · ')}</p>` : ''}</div>`).join('');
  $('agent-question').value = chat.draft;
  $('agent-question').disabled = chat.busy;
  $('agent-clear').disabled = chat.busy;
  $('agent-send').disabled = chat.busy || S.ai_enabled !== true;
  $('agent-status').textContent = chat.busy ? 'Looking through the library…' : chat.error || '';
}

$('btn-agent').addEventListener('click', () => {
  activeAgentChat = agentChatFor(workspaceClient.workspaceId);
  renderAgentChat();
  $('library-agent').showModal();
  $('agent-question').focus();
});
$('agent-question').addEventListener('input', () => { if (activeAgentChat) activeAgentChat.draft = $('agent-question').value; });
$('agent-close').addEventListener('click', () => $('library-agent').close());
$('agent-clear').addEventListener('click', () => {
  const chat = activeAgentChat;
  if (!chat || chat.busy) return;
  chat.messages = []; chat.draft = ''; chat.error = '';
  renderAgentChat();
});
$('agent-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const chat = activeAgentChat;
  if (!chat || chat.busy || S.ai_enabled !== true || !chat.draft.trim()) return;
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
    chat.messages = [...messages, { role: 'assistant', content: result.answer, assistant: result.assistant || 'Claude',
      activity: result.activity || [] }];
    chat.draft = '';
  } catch (error) { chat.error = error.message; }
  finally {
    chat.busy = false;
    if (activeAgentChat === chat) renderAgentChat();
  }
});
