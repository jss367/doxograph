'use strict';

// Saved conversations for a chat dialog: each paper's, and the library's, per workspace.
// Each conversation keeps its own agent. The page asks the model, then saves the whole
// conversation with the revision it last saw. Unsaved answers stay in this tab until a save works.
// If another window changed or deleted the conversation first, this tab's version becomes its own.
// Loaded before app.js, which owns `$`, `api`, `esc`, and `S`, and dialogs.js, which owns `confirmDialog`;
// these run only after they load.
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

  const KEPT = 'This conversation changed or was deleted in another window, so this version was kept as a separate conversation.';

  function title(thread) {
    const question = (thread.messages[0]?.content || thread.pending || '').replace(/\s+/g, ' ').trim();
    if (!question) return 'New conversation';
    return Array.from(question).length > 80 ? `${Array.from(question).slice(0, 79).join('').trimEnd()}…` : question;
  }

  function create({ prefix, agents, render }) {
    const boxes = new Map();
    let box = null;
    // Orders list requests against finished saves: a list can only include saves finished before it began.
    let clock = 0;

    const defaultAgent = () => agents().includes(S.ai_provider) ? S.ai_provider : agents()[0];
    const blank = () => ({ id: operation(), revision: 0, agent: defaultAgent(), messages: [],
      draft: '', busy: false, error: '', dirty: false, unconfirmed: [] });
    const headers = (extra = {}) => ({ 'X-Doxograph-Workspace': box.workspace, ...extra });
    const writing = (thread) => thread.busy || thread.saving || thread.messages.some(m => m.saving);
    // Whether a dirty thread still extends what is on disk: nothing newer was saved, or the newest
    // save is one of this tab's whose response was lost.
    const ahead = (thread, saved) => saved.revision === thread.revision || thread.unconfirmed.includes(saved.operation);

    function fork(thread) {
      Object.assign(thread, { id: operation(), revision: 0, unconfirmed: [], dirty: true, kept: true, error: KEPT });
    }

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
      const started = ++clock;
      target.listing = started;
      const response = await api(`/api/chats${target.paper ? `?paper=${encodeURIComponent(target.paper)}` : ''}`,
        { headers: { 'X-Doxograph-Workspace': target.workspace } });
      // A later list already answered or will answer; this one may predate what it loaded.
      if (target.listing !== started) return;
      const local = new Map(target.threads.map(t => [t.id, t]));
      // Keep what this tab is still writing, has not saved, or holds as new as disk (a list that
      // began loading before a save finished is older). Take newer versions from disk, and keep an
      // unsaved version that another window replaced or deleted as its own conversation.
      const threads = [], kept = [];
      for (const saved of response.chats) {
        // A list read before this tab deleted a conversation must not bring it back.
        if (target.deleted.has(saved.id)) continue;
        let thread = local.get(saved.id);
        if (thread && (writing(thread) || (thread.dirty ? ahead(thread, saved) : saved.revision <= thread.revision))) {
          threads.push(thread);
          continue;
        }
        if (thread?.dirty) {
          fork(thread);
          kept.push(thread);
          thread = null;
        }
        threads.push(Object.assign(thread || { draft: '', busy: false, error: '', dirty: false, unconfirmed: [] },
          { id: saved.id, revision: saved.revision, agent: saved.agent, messages: saved.messages }));
      }
      for (const thread of target.threads) {
        if (threads.includes(thread) || kept.includes(thread)) continue;
        // Saved after this list began, so it is new rather than deleted in another window.
        if (thread.savedAt > started) threads.unshift(thread);
        // Deleted elsewhere: keep an unsaved answer or an unsent draft, with its context, as its own conversation.
        else if ((thread.dirty || thread.draft.trim()) && thread.revision && !writing(thread)) {
          fork(thread);
          kept.push(thread);
        } else if (writing(thread) || thread.dirty) threads.push(thread);
      }
      target.threads = [...kept, ...threads];
      kept.forEach(thread => save(target, thread));
      target.issues = response.issues || [];
      // Reopen the latest conversation, unless a question was started while the list loaded.
      if (!target.loaded && !target.active.draft && !target.active.busy && !target.active.messages.length) target.active = threads[0] || target.active;
      else if (!threads.includes(target.active) && target.active.revision) target.active = blank();
      target.loaded = true;
    }

    async function open(workspace, paper) {
      const id = JSON.stringify([workspace, paper]);
      if (!boxes.has(id)) boxes.set(id, { workspace, paper, threads: [], active: null, loaded: false, issues: [], deleted: new Set() });
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
    // Resolves to whether this save reached disk.
    function save(target, thread) {
      if (!target.threads.includes(thread)) target.threads.unshift(thread);
      thread.saves = (thread.saves || Promise.resolve()).then(async () => {
        const id = thread.id;
        const saved = await write(target, thread);
        // A conflict kept this version under a new ID; list the other window's version beside it.
        if (thread.id !== id && !thread.dirty) {
          try { await refresh(target); } catch (_) { /* the other version appears on the next open */ }
          if (box === target) sync();
        }
        return saved;
      });
      return thread.saves;
    }

    async function write(target, thread, retried = false) {
      const id = operation();
      thread.saving = true;
      try {
        const saved = await api(`/api/chats/${thread.id}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-Doxograph-Workspace': target.workspace },
          body: JSON.stringify({ paper: target.paper, agent: thread.agent, revision: thread.revision,
            operation: id, unconfirmed: thread.unconfirmed,
            messages: thread.messages.map(({ role, content, assistant, model, activity, created, saved_note, note_operation }) =>
              ({ role, content, assistant, model, activity, created, saved_note, note_operation })) }),
        });
        thread.revision = saved.revision;
        thread.savedAt = ++clock;
        thread.unconfirmed = [];
        if (thread.dirty) thread.error = thread.kept ? KEPT : '';
        thread.dirty = thread.kept = false;
        return true;
      } catch (error) {
        if (!retried && (error.status === 409 || (error.status === 404 && thread.revision))) {
          fork(thread);
          thread.saving = false;
          return write(target, thread, true);
        }
        // The save may have landed with only its response lost; the next save names it.
        thread.unconfirmed = [...thread.unconfirmed, id].slice(-20);
        thread.dirty = true;
        thread.error = `This conversation is not saved yet: ${error.message}`;
        return false;
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
        target.deleted.add(thread.id);
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
