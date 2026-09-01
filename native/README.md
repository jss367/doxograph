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
`Contents/Resources/doxograph-path`. At runtime the app prefers, in order:
`$DOXOGRAPH_CMD`, a command you picked by hand, the recorded path, a short list
of usual locations, and finally whatever your login shell says `doxograph` is.
If none of them exist it offers to let you choose the command, and remembers it.

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
  minutes and dies with the server.
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
Sources/Menu.swift              the menu bar
tools/make-icon.swift           draws Doxograph.icns
```
