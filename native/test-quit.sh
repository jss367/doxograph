#!/bin/bash
# Run the quit/relaunch regression on macOS without starting a library server.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT
swiftc -swift-version 5 -framework AppKit \
  "$here/Sources/ServerController.swift" "$here/Sources/Locate.swift" \
  "$here/Tests/QuitProbe.swift" -o "$test_dir/quit-probe"
"$test_dir/quit-probe"
