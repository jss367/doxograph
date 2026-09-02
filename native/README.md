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

**Doxograph → Update Doxograph…** runs `git pull --ff-only origin main` in the
checkout the command came from (found by walking up from the recorded path, or
asked for once and remembered). Naming `origin/main` explicitly makes the app
update correctly even when that checkout is on a local workspace branch with
no upstream. It reinstalls with pip when `pyproject.toml` changed, and restarts
the server so the new Python is what the window shows. If anything under
`native/` changed it also runs `build.sh`, replaces the running bundle, and
relaunches. A server the app adopted rather than started is left alone; the
alert says so. An update that fails after the pull is picked up again by the
next one, from the last commit that fully installed. The first update measures
from the commit recorded in the bundle at build time, so a checkout you pulled
by hand but never rebuilt still gets its pip install and rebuild.

The same thing by hand is `git pull --ff-only origin main`, then
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
