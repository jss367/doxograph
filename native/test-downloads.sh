#!/bin/bash
# Exercise real WKWebView exports against a temporary local page on macOS.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT
swiftc -swift-version 5 -framework AppKit -framework WebKit \
  "$here/Sources/WebWindow.swift" "$here/Sources/DownloadController.swift" \
  "$here/Sources/Uploader.swift" "$here/Tests/DownloadProbe.swift" \
  -o "$test_dir/download-probe"
python3 - "$test_dir" <<'PY'
import http.server
from pathlib import Path
import subprocess
import sys
import threading


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        if self.path == "/interrupted":
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="interrupted.md"')
            self.send_header("Content-Length", "100000")
            self.end_headers()
            self.wfile.write(b"Incomplete export")
            self.close_connection = True
        else:
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<html><body>Research exports<script>
            function exportFile(name, content, type) {
                const a = document.createElement('a');
                a.href = URL.createObjectURL(new Blob([content], {type}));
                a.download = name;
                a.click();
                setTimeout(() => URL.revokeObjectURL(a.href), 1000);
            }
            </script></body></html>""")

    def log_message(self, *args):
        pass


folder = Path(sys.argv[1])
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    subprocess.run([str(folder / "download-probe"),
                    f"http://127.0.0.1:{server.server_port}/", str(folder)],
                   check=True, timeout=25)
finally:
    server.shutdown()
    server.server_close()
PY
