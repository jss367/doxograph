"""BibTeX export."""

from __future__ import annotations

import re

from . import store

# Characters that change how a .bib file parses. `%` starts a comment, so an
# unescaped one truncates the rest of the line including its closing brace, and
# percent-encoded URLs hit that constantly. A literal brace is worse: it
# unbalances the field and can swallow everything up to the next one. `\{` does
# not help, because BibTeX and biber count a brace whether or not a backslash
# comes before it, so a brace is written as a command with no brace of its own
# left over.
#
# Each character of the input is looked at once, so replacements that themselves
# contain a backslash or braces are not re-escaped.
ESCAPES = {
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    "\\": r"\textbackslash{}",
    "{": r"\textbraceleft{}", "}": r"\textbraceright{}",
}


def escape(value: str) -> str:
    return "".join(ESCAPES.get(char, char) for char in value or "")


def verbatim(value: str) -> str:
    """A url or doi, which biblatex and url.sty print character for character.

    A text escape there is printed too, so `\\_` reached the page as a backslash
    and an underscore, and the link stopped resolving. Inside a braced field
    only braces matter to BibTeX and biber, and they have to balance. `\\}`
    would print its backslash here, so they are percent-encoded instead, which a
    resolver reads as the same character.
    """
    return (value or "").replace("{", "%7B").replace("}", "%7D")


# BibTeX splits an author list on a bare "and" in any case, and reads the words
# of each name as first, von and last parts. An institutional author arrives as
# one string (ingest keeps Crossref's `name` whole, and nothing after it records
# that it was one), so "Research and Development Team" became three people and
# "ATLAS Collaboration" was cited as "Collaboration, A.". A name is braced,
# which BibTeX takes as a single last name printed as written, when it could not
# be a person's: it has a joining word no personal name has, or a word that
# names a body of people. A person is left unbraced so styles can still
# abbreviate and sort by surname. Von particles are lowercase too, which is why
# the joining words are listed rather than every lowercase word counted. Only
# words no one is named are listed: In, On, At and The are all given or family
# names (Sung-Jin In, Anh The Nguyen), and bracing one of those people printed
# the whole name as a surname.
JOINING_WORDS = {"and", "of", "for"}
BODY_WORDS = {
    "collaboration", "consortium", "team", "group", "committee", "council",
    "association", "society", "institute", "university", "laboratory",
    "foundation", "organization", "organisation", "network", "project",
    "initiative", "alliance", "commission", "agency", "corporation", "inc",
    "ltd", "llc", "investigators", "contributors",
}


def is_institution(name: str) -> bool:
    words = {word.lower() for word in re.findall(r"[^\W\d_]+", name)}
    return bool(words & (JOINING_WORDS | BODY_WORDS))


def author_field(authors: list[str] | None) -> str:
    # A blank entry is an author Crossref gave neither parts nor a name, and
    # joining it wrote `{ and }`, which BibTeX reads as two nameless people.
    names = [a.strip() for a in authors or [] if a and a.strip()]
    return " and ".join(
        f"{{{escape(a)}}}" if is_institution(a) else escape(a) for a in names
    ) or "Unknown"


def entry(paper: dict) -> str:
    source = paper.get("source") or {}
    fields = [
        ("title", escape(paper.get("title", ""))),
        ("author", author_field(paper.get("authors"))),
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
        fields.append(("doi", verbatim(paper["doi"])))
    if source.get("url"):
        fields.append(("url", verbatim(source["url"])))

    # Every value is wrapped the same way. Deciding by whether the value
    # happened to start with a brace was fragile once braces could be escaped
    # content rather than a wrapper.
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields if value)
    return f"@{kind}{{{paper['key']},\n{body}\n}}"


def render(papers: list[dict] | None = None) -> str:
    papers = papers if papers is not None else store.all_papers()
    papers = sorted(papers, key=lambda p: p["key"])
    return "\n\n".join(entry(p) for p in papers) + "\n"
