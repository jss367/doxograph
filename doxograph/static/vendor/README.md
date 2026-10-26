# Local PDF reader assets

PDF.js 6.3.289, from Mozilla's `pdfjs-dist` npm distribution:
https://github.com/mozilla/pdf.js

The browser modules, character maps, standard fonts, and WebAssembly helpers
are vendored unchanged so PDF rendering and text selection work offline.
The Apache 2.0 license is in `PDFJS-LICENSE`; supporting assets carry their
own license files. The reader's text-layer CSS is adapted from this version's
`web/pdf_viewer.css`.

To update, unpack a reviewed `pdfjs-dist` release and copy `build/pdf.min.mjs`,
`build/pdf.worker.min.mjs`, `cmaps`, `standard_fonts`, and `wasm` into this folder.
Keep the license files and run the PDF capture browser test after updating.

## Research note rendering

These pinned npm distributions are vendored unchanged for offline writing:

- Marked 18.1.2 (`lib/marked.umd.js`), https://github.com/markedjs/marked — MIT,
  license in `MARKED-LICENSE`.
- DOMPurify 3.4.17 (`dist/purify.min.js`), https://github.com/cure53/DOMPurify —
  Apache 2.0 or MPL 2.0, licenses in `DOMPURIFY-LICENSE`.
- KaTeX 0.19.1 (`dist/katex.min.js`, `dist/katex.min.css`, and `dist/fonts/`),
  https://github.com/KaTeX/KaTeX — MIT, license in `KATEX-LICENSE`.

The note editor escapes raw HTML, sanitizes rendered Markdown and math, and
only loads images from the selected workspace's local image endpoint. KaTeX
runs with `trust: false` and limits on macro expansion and size. When updating
these assets, retain licenses and run `tests/test_research_notes_browser.py`
in Chromium and WebKit, including its offline and sanitization checks.
