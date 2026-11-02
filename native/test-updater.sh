#!/bin/bash
# Check the summary of an update's commits that the update alert shows.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT
mkdir "$test_dir/probe"
cp "$here/Tests/UpdaterProbe.swift" "$test_dir/probe/main.swift"
swiftc -swift-version 5 -framework AppKit \
  "$here/Sources/Updater.swift" "$here/Sources/Locate.swift" "$here/Sources/ServerController.swift" "$test_dir/probe/main.swift" \
  -o "$test_dir/updater-probe"
"$test_dir/updater-probe"
