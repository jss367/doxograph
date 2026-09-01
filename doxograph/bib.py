"""BibTeX export."""

from __future__ import annotations

from . import store

# Characters that change how a .bib file parses. `%` starts a comment, so an
# unescaped one truncates the rest of the line including its closing brace, and
# percent-encoded URLs hit that constantly. A literal brace is worse: it
# unbalances the field and can swallow everything up to the next one.
#
# Each character of the input is looked at once, so replacements that themselves
# contain a backslash or braces are not re-escaped.
ESCAPES = {
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "~": r"\textasciitilde{}",
    "\\": r"\textbackslash{}",
    "{": r"\{", "}": r"\}",
}


def escape(value: str) -> str:
    return "".join(ESCAPES.get(char, char) for char in value or "")


def author_field(authors: list[str]) -> str:
    return " and ".join(escape(a) for a in authors) or "Unknown"


def entry(paper: dict) -> str:
    source = paper.get("source") or {}
    fields = [
        ("title", escape(paper.get("title", ""))),
        ("author", author_field(paper.get("authors", []))),
    ]
    if paper.get("year"):
        fields.append(("year", str(paper["year"])))

    if source.get("kind") == "arxiv":
        kind = "misc"
        fields += [("eprint", escape(source.get("id", ""))), ("archivePrefix", "arXiv")]
        if paper.get("venue") and paper["venue"] != "arXiv":
            fields.append(("note", escape(paper["venue"])))
    else:
        kind = "article" if paper.get("venue") else "misc"
        if paper.get("venue"):
            fields.append(("journal", escape(paper["venue"])))
    if paper.get("doi"):
        fields.append(("doi", escape(paper["doi"])))
    if source.get("url"):
        fields.append(("url", escape(source["url"])))

    # Every value is escaped and wrapped the same way. Deciding by whether the
    # value happened to start with a brace was fragile once braces could be
    # escaped content rather than a wrapper.
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields if value)
    return f"@{kind}{{{paper['key']},\n{body}\n}}"


def render(papers: list[dict] | None = None) -> str:
    papers = papers if papers is not None else store.all_papers()
    papers = sorted(papers, key=lambda p: p["key"])
    return "\n\n".join(entry(p) for p in papers) + "\n"
