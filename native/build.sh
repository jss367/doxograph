#!/bin/bash
# Builds Doxograph.app, a window and a Dock icon for the local server.
#
#   native/build.sh [--install]
#
# The app does not bundle Python. It records the path of the `doxograph`
# command it was built against and runs that, so the app and the CLI always
# read the same corpus.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(dirname "$here")"
build="$here/build"
app="$build/Doxograph.app"
install=0
[[ "${1:-}" == "--install" ]] && install=1

# The command to launch: an explicit DOXOGRAPH_CMD, else this checkout's venv,
# else whatever is on PATH. Recorded in the bundle, and overridable at runtime.
command_path="${DOXOGRAPH_CMD:-}"
if [[ -z "$command_path" && -x "$repo/.venv/bin/doxograph" ]]; then
  command_path="$repo/.venv/bin/doxograph"
fi
if [[ -z "$command_path" ]]; then
  command_path="$(command -v doxograph || true)"
fi
if [[ -z "$command_path" ]]; then
  echo "build.sh: no doxograph command found." >&2
  echo "  Install it first (python -m venv .venv && .venv/bin/pip install -e .)," >&2
  echo "  or set DOXOGRAPH_CMD to the command you want the app to run." >&2
  exit 1
fi

rm -rf "${app:?}"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"

echo "building Doxograph.app against $command_path"
swiftc \
  -swift-version 5 \
  -O \
  -target "$(uname -m)-apple-macosx13.0" \
  -framework AppKit -framework WebKit \
  -o "$app/Contents/MacOS/Doxograph" \
  "$here"/Sources/*.swift

cp "$here/Info.plist" "$app/Contents/Info.plist"
printf '%s' "$command_path" > "$app/Contents/Resources/doxograph-path"

if [[ ! -f "$here/Doxograph.icns" || "$here/icon.png" -nt "$here/Doxograph.icns" ||
      "$here/tools/make-icon.swift" -nt "$here/Doxograph.icns" ]]; then
  echo "drawing the icon"
  swift "$here/tools/make-icon.swift" "$here/Doxograph.icns"
fi
cp "$here/Doxograph.icns" "$app/Contents/Resources/Doxograph.icns"

# Ad-hoc signing gives the bundle a stable identity across rebuilds. It is not a
# Developer ID signature: this is built to run on the machine that built it, not
# to be handed to anyone else.
codesign --force --sign - "$app" >/dev/null 2>&1 ||
  echo "note: could not sign the bundle; it will still run locally" >&2

# Let Launch Services notice the bundle, so the Dock accepts PDF drops.
lsregister=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[[ -x "$lsregister" ]] && "$lsregister" -f "$app" >/dev/null 2>&1 || true

if [[ $install -eq 1 ]]; then
  destination="$HOME/Applications/Doxograph.app"
  mkdir -p "$HOME/Applications"
  rm -rf "${destination:?}"
  cp -R "$app" "$destination"
  echo "installed $destination"
else
  echo "built $app"
  echo "run it with: open '$app'   (or native/build.sh --install)"
fi
