"""Addresses for a paper, a claim, or one of your own claims that another
document can keep.

The page's own address is its fragment, which follows whatever is on screen
and changes shape as the app does. These paths name the thing instead, by
identifiers that are never reissued: a paper key is retired with its paper,
and a claim id is never reused on its paper. Each one redirects to the view
that shows it today, so a link written into a brief keeps working after the
fragment changes.

The workspace is the `workspace` query parameter, as on the PDF and BibTeX
links, so `SelectWorkspace` has already bound the request to it.
"""

from __future__ import annotations

import html
import re
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import config, store

router = APIRouter(dependencies=[Depends(store.recover_storage)])

KEY = re.compile(r"[A-Za-z0-9_-]{1,200}")


def path(kind: str, identifier: str, workspace: str | None = None) -> str:
    """The permanent path to one paper (`paper`), claim (`claim`), or ledger
    entry (`ledger`). The default workspace is left out of the address."""
    selected = config.workspace_id() if workspace is None else workspace
    query = "" if selected == config.DEFAULT_WORKSPACE_ID else "?" + urlencode({"workspace": selected})
    return f"/{kind}/{quote(identifier, safe='')}{query}"


def _open(**params: str) -> RedirectResponse:
    # Always name the workspace: without it the page opens the one last used
    # in this browser, which need not be the one the link was made in.
    fragment = urlencode({"ws": config.workspace_id(), "view": "claims", **params})
    return RedirectResponse(f"/#{fragment}", status_code=303, headers={"Cache-Control": "no-store"})


def _missing(what: str) -> HTMLResponse:
    home = "/#" + urlencode({"ws": config.workspace_id()})
    return HTMLResponse(
        "<!DOCTYPE html><meta charset='utf-8'><title>Not found · Doxograph</title>"
        f"<p>{html.escape(what)} is not in this workspace. It may have been removed.</p>"
        f"<p><a href='{html.escape(home)}'>Open the library</a></p>",
        status_code=404, headers={"Cache-Control": "no-store"},
    )


def _has_paper(key: str) -> bool:
    if not KEY.fullmatch(key):
        return False
    try:
        store.load_paper(key)
    except KeyError:
        return False
    return True


@router.get("/paper/{key}", include_in_schema=False)
def paper(key: str) -> Response:
    if not _has_paper(key):
        return _missing(f"Paper {key}")
    return _open(paper=key)


@router.get("/claim/{claim_id}", include_in_schema=False)
def claim(claim_id: str) -> Response:
    # A claim id is its paper's key and a sequence number, so the paper is
    # read alone. Older ids that do not follow that form are found by a scan,
    # and only when exactly one paper holds them.
    owner = re.fullmatch(r"(.+)-c\d+", claim_id)
    key = owner.group(1) if owner else ""
    if not (_has_paper(key) and any(c["id"] == claim_id for c in store.load_paper(key).get("claims", []))):
        holders = [p["key"] for p in store.all_papers()
                   if any(c["id"] == claim_id for c in p.get("claims", []))]
        if len(holders) != 1:
            return _missing(f"Claim {claim_id}")
        key = holders[0]
    return _open(paper=key, sel=claim_id)


@router.get("/ledger/{ledger_id:path}", include_in_schema=False)
def ledger(ledger_id: str) -> Response:
    if not any(entry["id"] == ledger_id for entry in store.load_ledger()):
        return _missing(f"Your claim {ledger_id}")
    return _open(ledger=ledger_id)
