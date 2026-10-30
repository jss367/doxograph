'use strict';

// Starts the library. Loaded after every feature script, because `boot`
// in app.js calls into them before its first await; agent-chat.js and
// workspace-recovery.js still load after this, as they did before.

AppStartup.run(boot);
