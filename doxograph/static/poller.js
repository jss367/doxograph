'use strict';

// A shared scheduler for background and explicitly requested refreshes.
function createPoller({ run, isVisible = () => true, interval = 2500,
  schedule = setInterval, cancel = clearInterval, onError = () => {} }) {
  let active = null, timer = null, completed = 0;
  function once() {
    if (active) return active;
    active = Promise.resolve().then(run).finally(() => { active = null; completed += 1; });
    return active;
  }
  async function afterCurrent() {
    if (active) await active.catch(() => {});
    return once();
  }
  return Object.freeze({
    once, afterCurrent,
    get running() { return active !== null; },
    get completed() { return completed; },
    start() {
      if (timer !== null) return;
      timer = schedule(() => {
        if (isVisible() && !active) once().catch(onError);
      }, interval);
    },
    stop() { if (timer !== null) cancel(timer); timer = null; },
  });
}
