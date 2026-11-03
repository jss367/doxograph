#!/bin/bash
# Inert actual-AppDelegate callback probe: no update, server, relaunch or app lifecycle.
set -euo pipefail
ulimit -c 0
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT
python3 - "$here" "$test_dir" <<'PY'
import pathlib
import re
import sys
here,target=map(pathlib.Path,sys.argv[1:])
for source in (here/'Sources').glob('*.swift'):
    if source.name=='main.swift':
        continue
    text=source.read_text()
    if source.name=='AppDelegate.swift':
        for field in ('window','ready','updating','relaunchAfterQuit'):
            text=text.replace('private let '+field+' =','let '+field+' =').replace('private var '+field+' =','var '+field+' =')
        text=text.replace('private func finishUpdate(', 'func finishUpdate(')
        dispatch='''        DispatchQueue.global().async { [weak self] in
            let command = Locate.doxographCommand()
            DispatchQueue.main.async { self?.update(with: command) }
        }'''
        assert text.count(dispatch)==1
        text=text.replace(dispatch,'        updateStatusProbeStarted()')
        presentation='''            if window.window.isVisible {
                alert.beginSheetModal(for: window.window, completionHandler: handle)
            } else {
                handle(alert.runModal())
            }'''
        assert text.count(presentation)==1
        text=text.replace(presentation,'            updateStatusProbePresent(handle)')
        text=text.replace('Updater.run(', 'updateStatusProbeForbiddenUpdater(')
        text=text.replace('NSApp.terminate(nil)','updateStatusProbeTerminate()')
        text,count=re.subn(r'    private func startServer\(\) \{.*?\n    \}',
            '    private func startServer() { fatalError("Inert probe attempted server startup") }',text,count=1,flags=re.S)
        assert count==1
    if source.name=='WebWindow.swift':
        text=text.replace('private let status:', 'let status:').replace('private let statusBackground:', 'let statusBackground:')
        text=text.replace('webView = WKWebView(', 'configuration.websiteDataStore = .nonPersistent()\n        webView = WKWebView(')
        text=text.replace('window.setFrameAutosaveName("DoxographMain")','// No persistent frame settings in this inert probe.')
    (target/source.name).write_text(text)
PY
swiftc -module-cache-path "$test_dir/module-cache" -swift-version 5 -framework AppKit -framework WebKit \
  "$test_dir"/*.swift "$here/Tests/UpdateStatusProbe.swift" -o "$test_dir/update-status-probe"
"$test_dir/update-status-probe"
