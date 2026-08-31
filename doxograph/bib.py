"""BibTeX export."""

from __future__ import annotations

from . import store

ESCAPES = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_"}


def escape(value: str) -> str:
    return "".join(ESCAPES.get(char, char) for char in value or "")


def author_field(authors: list[str]) -> str:
    return " and ".join(escape(a) for a in authors) or "Unknown"


def entry(paper: dict) -> str:
    source = paper.get("source") or {}
    fields = [
        ("title", f"{{{escape(paper.get('title', ''))}}}"),
        ("author", author_field(paper.get("authors", []))),
    ]
    if paper.get("year"):
        fields.append(("year", str(paper["year"])))

    if source.get("kind") == "arxiv":
        kind = "misc"
        fields += [("eprint", source.get("id", "")), ("archivePrefix", "arXiv")]
        if paper.get("venue") and paper["venue"] != "arXiv":
            fields.append(("note", escape(paper["venue"])))
    else:
        kind = "article" if paper.get("venue") else "misc"
        if paper.get("venue"):
            fields.append(("journal", escape(paper["venue"])))
    if paper.get("doi"):
        fields.append(("doi", paper["doi"]))
    if source.get("url"):
        fields.append(("url", source["url"]))

    body = ",\n".join(f"  {name} = {{{value}}}" if not value.startswith("{") else f"  {name} = {value}"
                      for name, value in fields if value)
    return f"@{kind}{{{paper['key']},\n{body}\n}}"


def render(papers: list[dict] | None = None) -> str:
    papers = papers if papers is not None else store.all_papers()
    papers = sorted(papers, key=lambda p: p["key"])
    return "\n\n".join(entry(p) for p in papers) + "\n"
