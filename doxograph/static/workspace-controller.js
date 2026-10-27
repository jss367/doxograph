'use strict';

// Serializes navigation without owning DOM, drafts, or application rendering.
function createWorkspaceController(client, {
  prepare, showCurrent, canLeave, confirmLeave, beforeLeave, beforeSelect = async () => true, enter,
}) {
  let pending = null, sequence = 0;

  async function switchTo(id) {
    if (id === client.workspaceId && !pending) return;
    if (!await prepare()) { showCurrent(); return; }
    const asked = ++sequence;
    // The native picker moves before the async operation; show the corpus
    // that still owns requests until held deletes and draft checks settle.
    showCurrent();
    while (pending) {
      await pending.catch(() => {});
      if (asked !== sequence) return;
    }
    if (id === client.workspaceId || !canLeave()) return;
    if (!await confirmLeave() || asked !== sequence) return;
    const run = (async () => {
      await beforeLeave();
      // Draining deletes can leave editors interactive for an entire request.
      // Flush edits made during that wait before changing their destination.
      if (!await beforeSelect()) { showCurrent(); return; }
      client.select(id);
      await enter();
    })();
    pending = run;
    try { await run; }
    finally { if (pending === run) pending = null; }
  }

  return Object.freeze({ switchTo, get pending() { return pending; } });
}
