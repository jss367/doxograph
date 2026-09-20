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
