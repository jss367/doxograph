"""A minimal PDF with real text, for tests that read a paper."""

from __future__ import annotations

import io


def minimal_pdf(text: str | list[str]) -> bytes:
    """A PDF in Helvetica carrying `text`, which pypdf extracts verbatim.

    One page per item when given a list, and one line per `\\n` within a page,
    laid out down the page so pypdf sees the line breaks.

    Only Latin-1 characters and no unbalanced parentheses: the text goes into
    PDF string literals as written.
    """
    pages = [text] if isinstance(text, str) else list(text)
    objects: list[bytes] = [b"", b""]        # catalog and page tree, filled in below
    kids: list[bytes] = []
    for page in pages:
        lines = page.split("\n")
        shows = " ".join(f"({line}) Tj 0 -16 Td" for line in lines)
        stream = f"BT /F1 12 Tf 72 720 Td {shows} ET".encode("latin-1")
        content = len(objects) + 2           # this page's object, then its stream
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents "
            + str(content).encode() + b" 0 R /Resources << /Font << /F1 "
            + str(len(pages) * 2 + 3).encode() + b" 0 R >> >> >>"
        )
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                       + stream + b"\nendstream")
        kids.append(str(content - 1).encode() + b" 0 R")
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = (b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count "
                  + str(len(pages)).encode() + b" >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
              f"startxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()
