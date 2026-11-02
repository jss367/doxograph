#!/bin/bash
# Exercise the native Edit-menu bridge with a real WKWebView and text undo.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT
swiftc -swift-version 5 -framework AppKit -framework WebKit \
  "$here/Sources/WebWindow.swift" "$here/Sources/DownloadController.swift" \
  "$here/Sources/Uploader.swift" "$here/Tests/UndoProbe.swift" \
  -o "$test_dir/undo-probe"
"$test_dir/undo-probe"
