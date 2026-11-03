#!/bin/bash
# Inert actual-handler probe: memory HTML, intercepted health, no registered app delegate.
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
        text = text.replace('NSApp.reply(toApplicationShouldTerminate:', 'consentProbeReply(')
        text = text.replace('self.saveDraftsAgainBeforeQuitting(acceptedStarts: startsAsked, acceptedWriting: acceptedWriting)', 'consentProbeHealthReturned()\n            self.saveDraftsAgainBeforeQuitting(acceptedStarts: startsAsked, acceptedWriting: acceptedWriting)')
        text = re.sub(r'(private func confirmQuitWithUnsavedWriting\(\) -> Bool \{).*?\n    \}',
                      r'\1\n        return consentProbeWritingQuestion()\n    }', text, flags=re.S)
        text = re.sub(r'(private func confirmInterruption\(_ action: Interruption, uploading: Int, reading: Int\) -> Bool \{).*?\n    \}',
                      r'\1\n        return consentProbeUploadQuestion()\n    }', text, flags=re.S)
    if source.name == 'Uploader.swift':
        text = text.replace('private static var inFlight', 'static var inFlight')
    if source.name == 'WebWindow.swift':
        text = text.replace('webView = WKWebView(', 'configuration.websiteDataStore = .nonPersistent()\n        webView = WKWebView(')
    (target / source.name).write_text(text)
PY
swiftc -module-cache-path "$test_dir/module-cache" -swift-version 5 -framework AppKit -framework WebKit \
  "$test_dir"/*.swift "$here/Tests/QuitWritingConsentProbe.swift" -o "$test_dir/consent-probe"
"$test_dir/consent-probe"
