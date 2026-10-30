'use strict';

// Saved conversations for a chat dialog: each paper's, and the library's, per workspace.
// Each conversation keeps its own agent. The page asks the model, then saves the whole
// conversation with the revision it last saw. Unsaved answers stay in this tab until a save works.
// Loaded before app.js, which owns `$`, `api`, `esc`, `confirmDialog`, and `S`; these run only after it loads.
const ChatThreads = (() => {
  const operation = () => Array.from(crypto.getRandomValues(new Uint8Array(16)),
    byte => byte.toString(16).padStart(2, '0')).join('');

  function agentLabel(agent) {
    if (agent === 'claude_code') return 'Claude subscription';
    if (agent === 'codex') return `ChatGPT subscription · ${S.codex_model || 'Codex default'}`;
    const provider = S.provider || 'anthropic';
    const name = $('ai-provider').querySelector(`option[value="${provider}"]`)?.textContent || provider;
    const model = S.providers?.[provider]?.model || '';
    return `${name}${['anthropic', 'openai'].includes(provider) ? ' API' : ''}${model ? ` · ${model}` : ''}`;
  }

  function title(thread) {
    const question = (thread.messages[0]?.content || thread.pending || '').replace(/\s+/g, ' ').trim();
    if (!question) return 'New conversation';
    return Array.from(question).length > 80 ? `${Array.from(question).slice(0, 79).join('').trimEnd()}…` : question;
  }

  function create({ prefix, agents, render }) {
    const boxes = new Map();
    let box = null;

    const defaultAgent = () => agents().includes(S.ai_provider) ? S.ai_provider : agents()[0];
    const blank = () => ({ id: operation(), revision: 0, agent: defaultAgent(), messages: [],
      draft: '', busy: false, error: '', dirty: false });
    const headers = (extra = {}) => ({ 'X-Doxograph-Workspace': box.workspace, ...extra });
    const writing = (thread) => thread.busy || thread.saving || thread.messages.some(m => m.saving);

    function sync() {
      if (!box) return;
      const thread = box.active;
      const listed = box.threads.includes(thread) ? box.threads : [thread, ...box.threads];
      $(`${prefix}-thread`).innerHTML = listed.map(t => `<option value="${t.id}">${esc(title(t))}${t.busy ? ' (answering…)' : ''}</option>`).join('');
      $(`${prefix}-thread`).value = thread.id;
      $(`${prefix}-thread`).disabled = listed.length < 2;
      $(`${prefix}-agent`).innerHTML = agents().map(a => `<option value="${a}">${esc(agentLabel(a))}</option>`).join('');
      $(`${prefix}-agent`).value = thread.agent;
      $(`${prefix}-agent`).disabled = thread.busy || thread.saving;
      $(`${prefix}-delete`).hidden = !thread.revision;
      $(`${prefix}-delete`).disabled = writing(thread);
      $(`${prefix}-clear`).disabled = !thread.messages.length && !thread.busy;
      render(thread);
    }

    async function refresh(target) {
      const response = await api(`/api/chats${target.paper ? `?paper=${encodeURIComponent(target.paper)}` : ''}`,
        { headers: { 'X-Doxograph-Workspace': target.workspace } });
      const local = new Map(target.threads.map(t => [t.id, t]));
      // Keep what this tab is still writing, or could not save for a reason other than
      // a newer version from another window; take everything else from disk.
      const threads = response.chats.map(saved => {
        const thread = local.get(saved.id);
        if (thread && (writing(thread) || (thread.dirty && thread.revision === saved.revision))) return thread;
        return Object.assign(thread || { draft: '', busy: false, error: '', dirty: false },
          { id: saved.id, revision: saved.revision, agent: saved.agent, messages: saved.messages });
      });
      threads.push(...target.threads.filter(t => !threads.includes(t) && (writing(t) || (t.dirty && !t.revision))));
      target.threads = threads;
      target.issues = response.issues || [];
      // Reopen the latest conversation, unless a question was started while the list loaded.
      if (!target.loaded && !target.active.draft && !target.active.busy) target.active = threads[0] || target.active;
      else if (!threads.includes(target.active) && target.active.revision) target.active = blank();
      target.loaded = true;
    }

    async function open(workspace, paper) {
      const id = JSON.stringify([workspace, paper]);
      if (!boxes.has(id)) boxes.set(id, { workspace, paper, threads: [], active: null, loaded: false, issues: [] });
      box = boxes.get(id);
      box.active ||= blank();
      sync();
      const target = box;
      try { await refresh(target); }
      catch (error) { target.active.error ||= `Could not load saved conversations: ${error.message}`; }
      if (box === target) sync();
    }

    // Write the whole conversation, adding it to the list on its first save. One tab's saves
    // of a conversation run in order, so each sends the revision the previous one returned.
    function save(target, thread) {
      if (!target.threads.includes(thread)) target.threads.unshift(thread);
      thread.saves = (thread.saves || Promise.resolve()).then(() => write(target, thread));
      return thread.saves;
    }

    async function write(target, thread) {
      thread.saving = true;
      try {
        const saved = await api(`/api/chats/${thread.id}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-Doxograph-Workspace': target.workspace },
          body: JSON.stringify({ paper: target.paper, agent: thread.agent, revision: thread.revision,
            messages: thread.messages.map(({ role, content, assistant, model, activity, created, saved_note }) =>
              ({ role, content, assistant, model, activity, created, saved_note })) }),
        });
        thread.revision = saved.revision;
        if (thread.dirty) thread.error = '';
        thread.dirty = false;
      } catch (error) {
        thread.dirty = true;
        thread.error = `This conversation is not saved yet: ${error.message}`;
      } finally {
        thread.saving = false;
      }
    }

    // Ask through `request(thread, messages)`, which returns the answer message to append.
    async function ask(request) {
      const target = box;
      const thread = target?.active;
      if (!thread || thread.busy || !thread.draft.trim()) return;
      const messages = [...thread.messages, { role: 'user', content: thread.draft.trim(), created: new Date().toISOString() }];
      const lengths = messages.map((message) => Array.from(message.content).length);
      if (messages.length > 40 || lengths.some((length) => length > 32000)
          || lengths.reduce((total, length) => total + length, 0) > 80000) {
        thread.error = 'Start a new conversation; this one is too long.';
        sync();
        return;
      }
      thread.busy = true; thread.error = ''; thread.pending = messages[0].content;
      if (!target.threads.includes(thread)) target.threads.unshift(thread);
      sync();
      try {
        const answer = await request(thread, messages.map(({ role, content }) => ({ role, content })));
        thread.messages = [...messages, { ...answer, role: 'assistant', created: new Date().toISOString() }];
        thread.draft = '';
        await save(target, thread);
      } catch (error) {
        thread.error = error.message;
        if (!thread.messages.length) target.threads = target.threads.filter(t => t !== thread);
      } finally {
        thread.busy = false;
        delete thread.pending;
        if (box === target) sync();
      }
    }

    $(`${prefix}-thread`).addEventListener('change', () => {
      const thread = box?.threads.find(t => t.id === $(`${prefix}-thread`).value);
      if (thread) { box.active = thread; sync(); }
    });
    $(`${prefix}-agent`).addEventListener('change', async () => {
      const target = box;
      const thread = target?.active;
      if (!thread || thread.busy || thread.saving) return;
      thread.agent = $(`${prefix}-agent`).value;
      thread.error = '';
      if (thread.messages.length) {
        sync();
        await save(target, thread);
      }
      if (box === target) sync();
    });
    $(`${prefix}-clear`).addEventListener('click', () => {
      if (!box) return;
      box.active = blank();
      sync();
      $(`${prefix}-question`).focus();
    });
    $(`${prefix}-delete`).addEventListener('click', async () => {
      const target = box;
      const thread = target?.active;
      if (!thread || !thread.revision || writing(thread)) return;
      if (!await confirmDialog(`Delete “${title(thread)}”?`, { note: 'This conversation is removed from this workspace.', ok: 'Delete conversation' })) return;
      thread.saving = true;
      sync();
      try {
        await api(`/api/chats/${thread.id}?revision=${thread.revision}`, { method: 'DELETE', headers: headers() });
        target.threads = target.threads.filter(t => t !== thread);
        if (target.active === thread) target.active = target.threads[0] || blank();
      } catch (error) { thread.error = `Could not delete this conversation: ${error.message}`; }
      finally {
        thread.saving = false;
        if (box === target) sync();
      }
    });

    return {
      open, ask, sync,
      // Save into the chat the conversation belongs to, even after the dialog moved on.
      save: (thread) => save([...boxes.values()].find(b => b.threads.includes(thread)) || box, thread),
      get active() { return box?.active || null; },
      get workspace() { return box?.workspace; },
      get issues() { return box?.issues || []; },
    };
  }

  return { create, agentLabel };
})();
