"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import bib, config, export, extract, ingest, store


def cmd_add(args) -> int:
    """Add papers. Exits nonzero if any requested reference did not go through."""
    config.ensure_dirs()
    failures = 0
    tokens: list[str] = []
    for value in args.refs:
        path = Path(value).expanduser()
        if path.is_file() and path.suffix.lower() == ".pdf":
            try:
                # Streamed into staging rather than read whole: a scanned PDF
                # can be hundreds of megabytes.
                with path.open("rb") as handle:
                    staged = ingest.stage_upload(handle, path.name)
                key, created = ingest.ingest_staged_pdf(staged, path.name)
            except Exception as exc:
                print(f"{path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
                failures += 1
                continue
            print(f"{'added' if created else 'already present'}: {key}")
            failures += _report_missing_pdf(key)
            if not args.no_extract and store.needs_extraction(key):
                failures += _read(key)
            continue
        tokens.append(value)

    refs, unknown = ingest.parse_refs(" ".join(tokens))
    for token in unknown:
        print(f"could not read reference: {token}", file=sys.stderr)
    failures += len(unknown)
    for ref in refs:
        try:
            key, created = ingest.ingest_ref(ref)
        except Exception as exc:
            print(f"{ref.value}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failures += 1
            continue
        print(f"{'added' if created else 'already present'}: {key}")
        failures += _report_missing_pdf(key)
        if not args.no_extract and store.needs_extraction(key):
            failures += _read(key)
    return 1 if failures else 0


def _report_missing_pdf(key: str) -> int:
    """A paper with no PDF is a partial result; say so and count it."""
    if store.pdf_path(key).exists():
        return 0
    try:
        note = store.load_paper(key).get("notes") or "no PDF available"
    except KeyError:
        print(f"  {key}: removed while it was being added.", file=sys.stderr)
        return 1
    print(f"  {key}: no PDF stored ({note}). Re-run add to retry the download.", file=sys.stderr)
    return 1


def _read(key: str) -> int:
    """Extract one paper. Returns 1 if it failed, so callers can count failures."""
    try:
        extract.extract_paper(key)
    except Exception as exc:
        print(f"  extraction failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"  read: {len(store.load_paper(key)['claims'])} claims")
    return 0


def cmd_extract(args) -> int:
    keys = args.keys or [
        p["key"] for p in store.all_papers()
        if args.all or not p.get("claims")
    ]
    failures = 0
    for key in keys:
        try:
            paper = extract.extract_paper(key, keep_reviewed=not args.replace_reviewed)
            print(f"{key}: {len(paper['claims'])} claims, {len(paper['proposed_tags'])} proposed topics")
        except Exception as exc:
            print(f"{key}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


def cmd_retag(args) -> int:
    keys = args.keys or [p["key"] for p in store.all_papers() if p.get("claims")]
    failures = 0
    for key in keys:
        try:
            extract.retag_paper(key)
            print(f"retagged {key}")
        except Exception as exc:
            print(f"{key}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


def cmd_tensions(args) -> int:
    """Find, or list, claims from different papers that disagree."""
    if not args.list:
        topics = args.topics or store.tension_topics()
        if not topics:
            print("no topic has claims from two papers yet", file=sys.stderr)
        failures = 0
        for topic in topics:
            try:
                result = extract.find_tensions(topic)
                print(f"{topic}: {result['returned']} returned, {result['added']} new"
                      + (f", {result['reopened']} reopened" if result["reopened"] else ""))
            except Exception as exc:
                print(f"{topic}: {type(exc).__name__}: {exc}", file=sys.stderr)
                failures += 1
        if failures:
            return 1
    rows = store.tension_rows()
    if args.topics:
        rows = [t for t in rows if set(t.get("topics", [])) & set(args.topics)]
    if not args.all:
        rows = [t for t in rows if t["status"] != "dismissed"]
    for tension in rows:
        a, b = tension["claims"]
        stale = " (a claim changed since)" if tension.get("stale") else ""
        print(f"{tension['id']:<5} {tension['status']:<9} {tension['kind']:<13} "
              f"#{' #'.join(tension.get('topics', []))}{stale}")
        for claim in (a, b):
            cite = (claim.get("paper_authors") or [claim["paper"]])[0].split()[-1]
            print(f"      [{cite} {claim.get('paper_year') or 'n.d.'}] {claim.get('text', '')}")
        if tension.get("note"):
            print(f"      {tension['note']}")
    open_count = sum(1 for t in rows if t["status"] == "open")
    print(f"\n{len(rows)} tensions, {open_count} open")
    return 0


def cmd_synthesize(args) -> int:
    """Write, or list, what the papers hold on each topic."""
    if not args.list:
        topics = args.topics or store.synthesis_topics()
        if not topics:
            print("no topic has claims from two papers yet; name a topic to synthesize it anyway",
                  file=sys.stderr)
        failures = 0
        for topic in topics:
            try:
                result = extract.synthesize_topic(topic)
                if result["written"]:
                    print(f"{topic}: written from {result['claims']} claims in {result['papers']} papers")
                else:
                    print(f"{topic}: no claims, nothing written", file=sys.stderr)
                    failures += 1
            except Exception as exc:
                print(f"{topic}: {type(exc).__name__}: {exc}", file=sys.stderr)
                failures += 1
        if failures:
            return 1
    rows = store.synthesis_rows()
    if args.topics:
        rows = [r for r in rows if r["topic"] in set(args.topics)]
    for row in rows:
        stale = " (claims or tensions changed since)" if row.get("stale") else ""
        print(f"## {row['topic']}  [{row['source']}, {row['written'][:10]}, "
              f"{row['n_claims']} claims in {row['n_papers']} papers]{stale}")
        print(row["text"])
        print()
    stale_count = sum(1 for r in rows if r.get("stale"))
    print(f"{len(rows)} syntheses, {stale_count} stale")
    return 0


def cmd_list(args) -> int:
    papers = store.all_papers()
    for paper in papers:
        summary = store.summarize(paper)
        print(f"{summary['status'][:3]:>3}  {summary['n_claims']:>3} claims  "
              f"{summary['key']:<32} {summary['title'][:70]}")
    rows = store.claim_rows(papers)
    print(f"\n{len(papers)} papers, {len(rows)} claims, "
          f"{sum(1 for r in rows if not r.get('reviewed'))} unreviewed")
    return 0


def cmd_tags(args) -> int:
    counts = store.tag_counts(store.claim_rows())
    declared = {t["name"]: t.get("description", "") for t in store.load_tags()}
    for name, count in counts.items():
        mark = " " if name in declared else "*"
        print(f"{count:>4} {mark}{name}  {declared.get(name, '')}")
    undeclared = [n for n in counts if n not in declared]
    if undeclared:
        print(f"\n* {len(undeclared)} topics are in use but not in the vocabulary", file=sys.stderr)
    return 0


def cmd_export(args) -> int:
    path = export.write(Path(args.out).expanduser() if args.out else None, title=args.title)
    print(path)
    return 0


def cmd_bibtex(args) -> int:
    text = bib.render()
    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8")
        print(args.out)
    else:
        sys.stdout.write(text)
    return 0


def cmd_serve(args) -> int:
    from .server import serve

    print(f"doxograph on http://{args.host}:{args.port}  (data in {config.data_dir()})")
    serve(host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="doxograph", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("add", help="add papers from arXiv IDs, DOIs, URLs, or PDF paths")
    p.add_argument("refs", nargs="+")
    p.add_argument("--no-extract", action="store_true", help="fetch without reading")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("extract", help="read papers and record their claims")
    p.add_argument("keys", nargs="*", help="paper keys; default is every paper with no claims")
    p.add_argument("--all", action="store_true", help="include papers that already have claims")
    p.add_argument("--replace-reviewed", action="store_true",
                   help="discard reviewed claims too (they are kept by default)")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("retag", help="reassign topics against the current vocabulary")
    p.add_argument("keys", nargs="*")
    p.set_defaults(func=cmd_retag)

    p = sub.add_parser("tensions", help="find claims from different papers that disagree")
    p.add_argument("topics", nargs="*", help="topics to check; default is every topic with two papers")
    p.add_argument("--list", action="store_true", help="show what is on file without calling the model")
    p.add_argument("--all", action="store_true", help="include dismissed tensions in the listing")
    p.set_defaults(func=cmd_tensions)

    p = sub.add_parser("synthesize", help="write what the papers hold on each topic")
    p.add_argument("topics", nargs="*", help="topics to write; default is every topic with two papers")
    p.add_argument("--list", action="store_true", help="show what is on file without calling the model")
    p.set_defaults(func=cmd_synthesize)

    p = sub.add_parser("list", help="list the corpus")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("tags", help="list topics and their use counts")
    p.set_defaults(func=cmd_tags)

    p = sub.add_parser("export", help="write the self-contained HTML view")
    p.add_argument("--out")
    p.add_argument("--title", default="Doxograph")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("bibtex", help="write BibTeX for the corpus")
    p.add_argument("--out")
    p.set_defaults(func=cmd_bibtex)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config.ensure_dirs()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
