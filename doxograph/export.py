"""Render the corpus as one self-contained HTML file.

No external assets and no network calls, so the file can be read anywhere and
stored alongside other notes. Claims are grouped by topic, because the point of
the export is to see what several papers say about the same question.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from . import config, store

CSS = """
:root {
  --ink: #16181d; --muted: #5c6370; --line: #dfe2e8; --bg: #ffffff;
  --panel: #f6f7f9; --accent: #2f5d8a; --warn: #8a5a2f;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
main { max-width: 60rem; margin: 0 auto; padding: 2.5rem 1.5rem 6rem; }
h1 { font-size: 1.75rem; margin: 0 0 .35rem; }
h2 { font-size: 1.2rem; margin: 2.5rem 0 .75rem; padding-bottom: .3rem;
  border-bottom: 1px solid var(--line); }
h3 { font-size: 1rem; margin: 1.75rem 0 .5rem; }
.sub { color: var(--muted); margin: 0 0 1.5rem; font-size: .9rem; }
.controls { position: sticky; top: 0; background: var(--bg); padding: .75rem 0;
  border-bottom: 1px solid var(--line); z-index: 5; display: flex; gap: .5rem; flex-wrap: wrap; }
.controls input, .controls select { font: inherit; padding: .35rem .5rem;
  border: 1px solid var(--line); border-radius: 4px; background: var(--bg); color: var(--ink); }
.controls input { flex: 1 1 16rem; }
.claim { border-left: 3px solid var(--line); padding: .5rem 0 .5rem .85rem; margin: .85rem 0; }
.claim.headline { border-left-color: var(--accent); }
.claim .text { margin: 0 0 .3rem; }
.claim .meta { font-size: .82rem; color: var(--muted); }
.claim .meta a { color: var(--accent); }
.claim .evidence { font-size: .88rem; margin: .35rem 0 0; }
.claim blockquote { margin: .4rem 0 0; padding-left: .7rem; border-left: 2px solid var(--line);
  color: var(--muted); font-size: .86rem; }
.kind { display: inline-block; font-size: .72rem; text-transform: uppercase;
  letter-spacing: .04em; padding: .05rem .35rem; border: 1px solid var(--line);
  border-radius: 3px; margin-right: .4rem; color: var(--muted); }
.kind.negative, .kind.conjecture, .kind.contradiction { color: var(--warn); border-color: var(--warn); }
.tag { font-size: .78rem; color: var(--accent); margin-right: .4rem; white-space: nowrap; }
.paper { padding: .9rem 0; border-bottom: 1px solid var(--line); }
.paper .title { font-weight: 600; }
.paper .authors, .paper .rel { font-size: .87rem; color: var(--muted); }
.paper .summary { font-size: .92rem; margin: .4rem 0 0; }
.ledger { background: var(--panel); padding: .85rem 1rem; border-radius: 5px; margin: .85rem 0; }
.ledger .own { font-weight: 600; margin: 0 0 .5rem; }
.rel-tag { font-size: .74rem; text-transform: uppercase; letter-spacing: .03em;
  color: var(--muted); margin-right: .35rem; }
.count { color: var(--muted); font-weight: 400; font-size: .85rem; }
.empty { color: var(--muted); font-style: italic; }
@media print { .controls { display: none; } }
@media (prefers-color-scheme: dark) {
  :root { --ink: #e6e8ec; --muted: #99a0ad; --line: #343a44; --bg: #14161a;
    --panel: #1d2027; --accent: #7fb0e0; --warn: #d9a267; }
}
"""

SCRIPT = """
const data = window.__DOXOGRAPH__;
const q = document.getElementById('q');
const kindSel = document.getElementById('kind');
const tagSel = document.getElementById('tag');

function apply() {
  const needle = q.value.trim().toLowerCase();
  const kind = kindSel.value;
  const tag = tagSel.value;
  document.querySelectorAll('[data-claim]').forEach(node => {
    const hay = node.dataset.hay;
    const ok = (!needle || hay.includes(needle))
      && (!kind || node.dataset.kind === kind)
      && (!tag || node.dataset.tags.split(' ').includes(tag));
    node.hidden = !ok;
  });
  // A tension is one comparison, not two claims: it shows both sides or
  // neither, and it shows when either side matches.
  document.querySelectorAll('[data-tension]').forEach(pair => {
    const sides = pair.querySelectorAll('[data-claim]');
    const shown = Array.from(sides).some(side => !side.hidden);
    sides.forEach(side => { side.hidden = !shown; });
    pair.hidden = !shown;
  });
  document.querySelectorAll('[data-group]').forEach(group => {
    const shown = group.querySelectorAll('[data-claim]:not([hidden])').length;
    group.hidden = shown === 0;
    const badge = group.querySelector('.count');
    if (badge) badge.textContent = shown === group.querySelectorAll('[data-claim]').length
      ? badge.dataset.total : shown + ' of ' + badge.dataset.total;
  });
}
[q, kindSel, tagSel].forEach(el => el.addEventListener('input', apply));
apply();
"""


def _e(value) -> str:
    return html.escape(str(value or ""))


def _authors(authors: list[str]) -> str:
    if not authors:
        return ""
    if len(authors) > 3:
        return f"{authors[0]} et al."
    return ", ".join(authors)


def _claim_html(row: dict, ledger_by_id: dict[str, dict]) -> str:
    hay = " ".join([
        row.get("text", ""), row.get("evidence", ""), row.get("quote", ""),
        row.get("paper_title", ""), " ".join(row.get("paper_authors", [])),
        " ".join(row.get("tags", [])),
    ]).lower()
    cite = f"{_authors(row.get('paper_authors', []))} ({row.get('paper_year') or 'n.d.'})"
    tags = "".join(f'<span class="tag">#{_e(t)}</span>' for t in row.get("tags", []))
    parts = [
        f'<div class="claim {_e(row.get("strength"))}" data-claim '
        f'data-kind="{_e(row.get("kind"))}" data-tags="{_e(" ".join(row.get("tags", [])))}" '
        f'data-hay="{_e(hay)}">',
        f'<p class="text"><span class="kind {_e(row.get("kind"))}">{_e(row.get("kind"))}</span>'
        f'{_e(row.get("text"))}</p>',
        f'<div class="meta">{tags}{_e(cite)}'
        + (f' · {_e(row.get("locator"))}' if row.get("locator") else "")
        + "</div>",
    ]
    if row.get("evidence"):
        parts.append(f'<p class="evidence">{_e(row["evidence"])}</p>')
    if row.get("quote"):
        parts.append(f"<blockquote>{_e(row['quote'])}</blockquote>")
    for link in row.get("ledger_links", []):
        own = ledger_by_id.get(link.get("claim", ""), {})
        label = own.get("text") or link.get("claim", "")
        parts.append(
            f'<div class="meta"><span class="rel-tag">{_e(link.get("relation"))}</span>'
            f'{_e(label)}' + (f' — {_e(link["note"])}' if link.get("note") else "") + "</div>"
        )
    parts.append("</div>")
    return "".join(parts)


def render(title: str = "Doxograph") -> str:
    papers = store.all_papers()
    rows = store.claim_rows(papers)
    tags = store.load_tags()
    tag_descriptions = {t["name"]: t.get("description", "") for t in tags}
    counts = store.tag_counts(rows)
    ledger = store.load_ledger()
    ledger_by_id = {c["id"]: c for c in ledger}
    tensions = [t for t in store.tension_rows(rows) if t.get("status") != "dismissed"]

    body = [
        f"<h1>{_e(title)}</h1>",
        f'<p class="sub">{len(papers)} papers · {len(rows)} claims · {len(counts)} topics '
        f"· generated {_e(store.now())}</p>",
        '<div class="controls">',
        '<input id="q" type="search" placeholder="Filter claims, papers, quotes">',
        '<select id="kind"><option value="">every kind</option>'
        + "".join(f'<option value="{_e(k)}">{_e(k)}</option>' for k in config.CLAIM_KINDS)
        + "</select>",
        '<select id="tag"><option value="">every topic</option>'
        + "".join(f'<option value="{_e(t)}">{_e(t)} ({n})</option>' for t, n in counts.items())
        + "</select>",
        "</div>",
    ]

    body.append("<h2>Claims by topic</h2>")
    if not rows:
        body.append('<p class="empty">No claims yet.</p>')
    for tag, count in counts.items():
        body.append(f'<section data-group><h3>{_e(tag)} '
                    f'<span class="count" data-total="{count}">{count}</span></h3>')
        if tag_descriptions.get(tag):
            body.append(f'<p class="sub">{_e(tag_descriptions[tag])}</p>')
        for row in rows:
            if tag in row.get("tags", []):
                body.append(_claim_html(row, ledger_by_id))
        body.append("</section>")

    untagged = [r for r in rows if not r.get("tags")]
    if untagged:
        body.append(f'<section data-group><h3>untagged '
                    f'<span class="count" data-total="{len(untagged)}">{len(untagged)}</span></h3>')
        body.extend(_claim_html(r, ledger_by_id) for r in untagged)
        body.append("</section>")

    if tensions:
        body.append("<h2>Where the papers disagree</h2>")
        body.append('<p class="sub">Pairs of claims from different papers that pull against each '
                    'other. Confirmed ones have been checked by hand; open ones have not. One '
                    'judged against earlier text has had a claim edited since, so its verdict '
                    'may no longer fit.</p>')
        for tension in tensions:
            topics = "".join(f'<span class="tag">#{_e(t)}</span>' for t in tension.get("topics", []))
            status = "" if tension.get("status") == "confirmed" else \
                '<span class="rel-tag">open</span>'
            stale = '<span class="rel-tag">judged against earlier text</span>' if tension.get("stale") else ""
            body.append('<div class="ledger" data-tension>')
            body.append(f'<p class="own"><span class="kind {_e(tension.get("kind"))}">'
                        f'{_e(tension.get("kind"))}</span>{status}{stale}{topics}</p>')
            body.extend(_claim_html(r, ledger_by_id) for r in tension["claims"])
            if tension.get("note"):
                body.append(f'<p class="sub">{_e(tension["note"])}</p>')
            body.append("</div>")

    if ledger:
        body.append("<h2>Bearing on my own claims</h2>")
        for own in ledger:
            linked = [r for r in rows if any(l.get("claim") == own["id"] for l in r.get("ledger_links", []))]
            body.append('<div class="ledger">')
            body.append(f'<p class="own">{_e(own["id"])} · {_e(own.get("text"))}</p>')
            if linked:
                body.extend(_claim_html(r, ledger_by_id) for r in linked)
            else:
                body.append('<p class="empty">Nothing in the corpus is linked to this claim yet.</p>')
            body.append("</div>")

    body.append("<h2>Papers</h2>")
    for paper in sorted(papers, key=lambda p: (p.get("year") or 0, p.get("title") or ""), reverse=True):
        source = paper.get("source") or {}
        link = source.get("url") or ""
        heading = _e(paper.get("title") or paper["key"])
        if link:
            heading = f'<a href="{_e(link)}">{heading}</a>'
        body.append(
            '<div class="paper">'
            f'<div class="title">{heading}</div>'
            f'<div class="authors">{_e(_authors(paper.get("authors", [])))}'
            + (f' · {_e(paper.get("year"))}' if paper.get("year") else "")
            + (f' · {_e(paper.get("venue"))}' if paper.get("venue") else "")
            + f' · <code>{_e(paper["key"])}</code></div>'
            + (f'<p class="summary">{_e(paper.get("summary"))}</p>' if paper.get("summary") else "")
            + (f'<p class="rel">Why it is here: {_e(paper.get("relevance"))}</p>' if paper.get("relevance") else "")
            + "</div>"
        )

    payload = json.dumps({"papers": len(papers), "claims": len(rows)}).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>{CSS}</style></head>
<body><main>{''.join(body)}</main>
<script>window.__DOXOGRAPH__ = {payload};{SCRIPT}</script>
</body></html>
"""


def write(path: Path | None = None, title: str = "Doxograph") -> Path:
    path = Path(path) if path else config.export_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(title=title), encoding="utf-8")
    return path
