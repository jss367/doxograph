'use strict';

// Retries a chat request the server marked as a temporary provider failure.
// Anything else, including a lost connection, is not known to be safe to resend.
const ChatRetry = (() => {
  const DELAYS = [15000, 30000, 60000];
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

  async function run(send, { delays = DELAYS, sleep = wait, onRetry = () => {} } = {}) {
    for (let attempt = 0; ; attempt += 1) {
      try { return await send(); }
      catch (error) {
        if (!error.retryable || attempt >= delays.length) throw error;
        const delay = Math.max(delays[attempt], error.retryAfter ?? 0);
        onRetry(`The model service is busy. Retrying in ${Math.ceil(delay / 1000)} seconds (attempt ${attempt + 1} of ${delays.length})…`);
        await sleep(delay);
      }
    }
  }
  return { DELAYS, run };
})();
