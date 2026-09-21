# Doxograph.app

A window and a Dock icon for the local server. Drop a PDF on the icon and it
goes into the corpus; the window is the same web app `doxograph serve` has
always served, without a terminal or a browser tab in front of it.

## Build

```
python -m venv .venv && .venv/bin/pip install -e .   # if you have not already
native/build.sh --install                            # ~/Applications/Doxograph.app
```

`build.sh` on its own leaves the bundle in `native/build/` instead of
installing it. Both need the Xcode command line tools for `swiftc`.

## What it is

The app is a launcher, not a port. It runs the `doxograph` command you already
installed and points a `WKWebView` at it, so the app and the CLI share one
corpus and one copy of the code. Nothing is duplicated in Swift, and a change to
the Python shows up in the app on its next launch.

`build.sh` records the command it built against in
`Contents/Resources/doxograph-path`, and the commit it built from in
`Contents/Resources/doxograph-commit`. At runtime the app prefers, in order:
`$DOXOGRAPH_CMD`, a command you picked by hand, the recorded path, a short list
of usual locations, and finally whatever your login shell says `doxograph` is.
If none of them exist it offers to let you choose the command, and remembers it.

## Updating

**Doxograph → Update Doxograph…** fetches `origin/main` and fast-forwards the
checkout the command came from (found by walking up from the recorded path, or
asked for once and remembered). Naming `origin/main` explicitly makes the app
update correctly even when that checkout is on a local workspace branch with
no upstream. A checkout containing commits outside `origin/main` is refused so
the updater neither runs unreleased code nor discards local work. It reinstalls
with pip when `pyproject.toml` changed, and restarts the server so the new Python
is what the window shows. If anything under `native/` changed it also runs
`build.sh`, replaces the running bundle, and relaunches. A server the app adopted
rather than started is left alone; the alert says so. An update that fails after
the fast-forward is picked up again by the next one, from the last commit that
fully installed. The first update measures from the commit recorded in the
bundle at build time, so a checkout you updated by hand but never rebuilt still
gets its pip install and rebuild.

The same thing by hand is `git fetch origin main` followed by
`git merge --ff-only FETCH_HEAD`, then
`.venv/bin/pip install -e .` if `pyproject.toml` or anything under `doxograph/`
changed, then `native/build.sh --install` if anything under `native/` changed,
and then relaunching the app.

## What it adds

- **Dock drops.** PDFs dropped on the icon, or opened with Doxograph from
  Finder, are posted to `/api/upload`, the same endpoint the page uses. Dropping
  a paper on the icon while the app is closed launches it and adds the paper.
- **File → Add Papers…** for the same thing through an open panel.
- **Adopting a running server.** If a Doxograph is already answering the app
  uses it rather than starting a second one, so launching the app over a
  `doxograph serve` in a terminal is harmless. It leaves an adopted server
  running when it quits. The search walks 8765 upward through the whole range
  before deciding, so a server that had to fall back past something else on 8765
  is adopted even once that something else has gone away and left 8765 free. The
  app starts its own only when no Doxograph answers anywhere in the range, and
  then it uses the lowest port that was free.
- **Only a server running the code that is there now.** A Doxograph answering
  from somewhere other than the current code is not adopted silently: the app
  offers to restart it, to use it anyway, or to quit. Without that check a server
  orphaned by a crash or a force quit — which the app never stops, having never
  started it — is adopted by every launch afterwards and upgraded by none, so the
  window shows code that can be releases behind the app around it for as long as
  the process lives. The pid to signal comes from `lsof`, since a server old
  enough to be the problem is too old to have been taught to report its own.

  Two things are compared, and they catch orphans at two different ages. The
  release in `/api/health` is checked against the bundle's
  `CFBundleShortVersionString`, which catches a server that has outlived a
  release. That alone misses one that has outlived a morning: `Info.plist` and
  `doxograph/__init__.py` move together and only on a release, so a server
  orphaned earlier in the same release reports exactly what the bundle reports.

  So `/api/code` is asked as well. It returns two digests over the code the
  server is made of: `running`, taken while the process was importing its
  modules, and `onDisk`, taken now. Python reads those files once and keeps them,
  so the two differ exactly when the code has been edited, pulled or reinstalled
  under a server that had already loaded it — which is the question worth asking,
  since it is the same as asking whether restarting would change anything.

  Both halves of what it runs are counted. The package's own `.py` files go in by
  contents, so a branch switch that restores bytes the checkout already held is
  not reported as a change. Everything imported from outside the package —
  FastAPI, Pydantic, HTTPX and what they pull in — goes in by size and time,
  because contents there run to tens of megabytes; that half is what catches
  `pip install -e .` on a changed `pyproject.toml`, which upgrades a library
  without touching a single `doxograph` source file. That list is read fresh on
  every request rather than fixed at import, because a process goes on
  importing: Uvicorn arrives after `server.py` is done and pypdf only when a PDF
  does. A reading is taken when the app module is imported, again from the app's
  lifespan once the server is built and before it accepts anything, and again
  beside each later import site — so the file remembered for a module is the file
  that module was loaded from. The lifespan reading is what catches what Uvicorn
  loads on its own way up: `uvicorn.run` builds a config and loads it, which
  imports the event loop and HTTP protocol implementations, and those arrive
  after `import uvicorn` has returned. The app module rather than `serve()`,
  because `--reload` hands uvicorn a module string and the worker it spawns never
  calls `serve()`.
  Recording it later instead would remember an upgrade that landed in between as
  the original, and the server would read as current forever while running code
  nobody has on disk. `static/` is left out on purpose: it is served per request, so editing
  `app.js` reaches the next reload without anything going stale. An empty digest,
  from a package the server cannot read back, is read as no answer and refuses
  nothing.

  The two endpoints are two requests, and a port is not a promise between them:
  the server that answers the first can exit before the second and leave its
  successor to answer that one. Both report an `instance` — a token this process
  made when it started — and a pair naming two different ones is thrown away
  rather than combined into a description of a server that never existed. The
  release is checked too, for a server too old to have an instance, but it is the
  weaker of the two: two servers of one release are exactly what it cannot tell
  apart. The pair is asked for twice, and a port changing hands twice in a row
  settles for the health read alone — one server's answer, and the release check
  this had before the digests existed.

  The same token is what says, after the alert has been answered, that the server
  on the port is still the one the question described.

  An unanswered `/api/code` is adoptable but not *confirmed*, and the two paths
  treat that differently. The port walk adopts either way: it is choosing where
  to start, and refusing a server over a request that did not come back would put
  up an alert about a slow moment. Restarting a stale server may not, because
  there the user has asked for something and reporting it done is a claim — so an
  unconfirmed answer goes ahead with the restart rather than quietly reporting
  success without one.

  A server that answers `/api/health` as Doxograph and then 404s on `/api/code`
  is refused rather than adopted, even when the release matches. Having the route
  is also part of who a server is: when the alert has been answered and the port
  is described again, one description naming a digest and another having no such
  route are two different processes, not one that went quiet, so the question is
  asked again rather than the wrong process being signalled. This app's own
  code serves that route, so a Doxograph without it is provably not running this
  app's code — and it is the very case the digest would otherwise be blindest to,
  since the server too old to answer is the one orphaned before the answer
  existed. A request that fails some other way says nothing and adopts.

  What is still not covered is a server started from a *different* installation
  than the one this app would launch. Each half reports on itself, so an
  up-to-date server of some other checkout looks the same as this one's.
  When the alert has been answered, whether the work in flight is the *same*
  work decides whether the agreement still holds. `/api/health` reports `taken`,
  a count of everything the server has ever accepted — once each, since a paper
  arrives as a request and becomes a job, and counting both phases would ask the
  user again about a paper they had already agreed to lose. It only goes up, and
  comparing the gauges alone would miss a second paper starting beside the first,
  and would miss a first finishing as a second starts, which leaves the totals
  identical over entirely different work.
- **A warning before quitting mid-extraction**, because reading a paper takes
  minutes and dies with the server. A server that does not answer the question
  gets the warning too, worded for not knowing: a silent server may be a busy
  one. A refused connection does not, since a server that is already gone has
  nothing left to lose. Quitting anyway is always on offer.
- **Native dialogs** for the page's `confirm()` and `alert()` calls, which a web
  view otherwise answers "no" to without asking.
- **External links go to the browser.** arXiv and DOI links open in your default
  browser; PDFs and BibTeX open in their own window.

## Configuration

The app is not sandboxed and is signed ad-hoc, which is enough to run on the
machine that built it. Handing it to another Mac means signing it with a
Developer ID and notarizing it.

Two settings, both read at launch:

```
defaults write com.jss367.doxograph DoxographCommand -string /path/to/.venv/bin/doxograph
defaults write com.jss367.doxograph DoxographPort -int 8765
```

`DoxographPort` has to be a port — a whole number from 1 to 65535. Anything
else, including a number too large to be a port and a value that is not a
number at all, is ignored and the app starts its search at 8765. The search
still walks upward from wherever it starts, and stops at 65535.

A Dock launch inherits none of your shell environment, so `ANTHROPIC_API_KEY`
exported in `.zshrc` is invisible to it. Put the key in `~/.credentials` as
`ANTHROPIC_API_KEY=...`, which is where the server looks next.

## Layout

```
Sources/AppDelegate.swift       launch, drops, quitting
Sources/ServerController.swift  starting, adopting and stopping the server
Sources/WebWindow.swift         the window, link handling, page dialogs
Sources/Uploader.swift          posting dropped PDFs to /api/upload
Sources/Locate.swift            finding the doxograph command
Sources/Updater.swift           pulling the checkout and rebuilding
Sources/Menu.swift              the menu bar
icon.png                        the logo, source art for the icon
tools/make-icon.swift           renders icon.png as Doxograph.icns
```
