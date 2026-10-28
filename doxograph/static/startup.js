'use strict';

// Installed before feature scripts so a missing or incompatible module still
// leaves a useful recovery action. Never reload over someone's work on a timer.
window.AppStartup = (() => {
  let phase = 'interface', ready = false, failed = false;
  function fail(error) {
    if (ready || failed) return;
    failed = true;
    const connection = phase === 'connection';
    document.getElementById('startup-title').textContent = connection
      ? 'The library could not connect' : 'The interface could not start';
    document.getElementById('startup-message').textContent = connection
      ? 'Check that Doxograph is running, then reload the library.'
      : 'Reload the library to load the latest interface. If this continues, relaunch Doxograph.';
    document.getElementById('startup-detail').textContent = error?.message || String(error);
    document.getElementById('startup-error').hidden = false;
  }
  document.getElementById('startup-reload').addEventListener('click', () => location.reload());
  window.addEventListener('error', event => {
    if (event.target instanceof HTMLScriptElement) {
      phase = 'interface';
      fail(new Error(`Could not load ${new URL(event.target.src).pathname}`));
    } else if (event.error) fail(event.error);
  }, true);
  return {
    phase(value) { phase = value; },
    async run(start) {
      try {
        if (failed) return;
        await start();
        ready = true;
      } catch (error) { fail(error); }
    },
  };
})();
