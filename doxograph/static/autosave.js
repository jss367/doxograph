/* Saves an editor's draft shortly after typing stops. One request runs at a
   time, and text typed during it is saved by the next. Each round belongs to
   the draft it started on: replacing the draft ends the round and drops its
   failure, so a draft the writer moved past is never written.

   A write that fails may still have reached the server, so its draft keeps
   needing a save, even once it matches what was last known to be stored,
   until a later write is acknowledged. */
function createAutosave({current, dirty, blocked = () => '', write, changed, delay = 600}) {
  let timer = null, held = false, saving = null, failure = '', failed = null, edits = 0;
  let unconfirmed = null;
  const unsaved = target => dirty() || (Boolean(target) && unconfirmed === target);
  function arm() {
    clearTimeout(timer);
    timer = setTimeout(() => { timer = null; void flush(); }, delay);
  }
  async function flush() {
    clearTimeout(timer); timer = null; held = false;
    // Waiting callers observe this round's failure; retry needs a new flush.
    while (saving) if (!await saving) return false;
    const target = current();
    if (!target || !unsaved(target)) return true;
    const reason = blocked();
    if (reason) { failure = reason; failed = target; changed(); return false; }
    failure = ''; failed = null;
    saving = (async () => {
      while (current() === target && unsaved(target)) {
        // A server that normalizes what it stores can leave the draft looking
        // edited. Only writing since the request goes out earns another.
        const seen = edits;
        unconfirmed = target;
        await write(target);
        if (unconfirmed === target) unconfirmed = null;
        if (edits === seen) break;
      }
      return current() === target;
    })().catch(error => {
      if (current() === target) { failure = error.message; failed = target; }
      return false;
    }).finally(() => { saving = null; changed(); });
    changed();
    return saving;
  }
  return {
    flush,
    schedule() { edits++; arm(); changed(); },
    // Hold a pending save and wait for one under way, without starting one.
    // An editor frozen for an action settles first, then resumes after it.
    async settle() {
      if (timer) held = true;
      clearTimeout(timer); timer = null;
      while (saving) await saving;
    },
    resume() { if (held && unsaved(current())) arm(); held = false; },
    get generation() { return edits; },
    get saving() { return Boolean(saving); },
    // Whether the draft's last write went out without an answer.
    get unconfirmed() { const target = current(); return Boolean(target) && unconfirmed === target; },
    get failure() { return failed && failed === current() ? failure : ''; },
    // What the editor's status line says about its draft.
    describe() {
      const reason = this.failure;
      return reason ? `Couldn’t save. ${reason}` : saving ? 'Saving…' : unsaved(current()) ? 'Waiting to save…' : 'Saved';
    },
  };
}
