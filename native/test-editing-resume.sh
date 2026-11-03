#!/bin/bash
# Inert native restoration probe: no application lifecycle, server or uploads.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT
python3 - "$here" "$test_dir" <<'PY'
import pathlib
import re
import sys
here, target = map(pathlib.Path, sys.argv[1:])
for source in (here / 'Sources').glob('*.swift'):
    if source.name == 'main.swift':
        continue
    text = source.read_text()
    if source.name == 'AppDelegate.swift':
        text = text.replace('private let window =', 'let window =').replace('private var ready =', 'var ready =')
    if source.name == 'WebWindow.swift':
        text = text.replace('webView = WKWebView(', 'configuration.websiteDataStore = .nonPersistent()\n        webView = ResumeProbeWebView(')
        # Only the stalled-restoration watchdog is accelerated; production save timeout stays intact.
        text = text.replace('.now() + 5, execute: checkRestoration', '.now() + 0.1, execute: checkRestoration')
        text = re.sub(r'(private func confirmNavigationWhileRestoringEditing\(\) -> Bool \{).*?\n    \}',
                      r'\1\n        return resumeProbeTimeoutChoice()\n    }', text, flags=re.S)
    (target / source.name).write_text(text)
PY
swiftc -swift-version 5 -framework AppKit -framework WebKit \
  "$test_dir"/*.swift "$here/Tests/EditingResumeProbe.swift" -o "$test_dir/resume-probe"
"$test_dir/resume-probe"
