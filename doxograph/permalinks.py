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


def derived(paper: str, claim_id: str) -> bool:
    """Whether a claim id is its paper's key and a sequence number, which names
    the paper by itself. Older and restored ids need not, and two papers can
    hold the same one."""
    return re.fullmatch(re.escape(paper) + r"-c\d+", claim_id) is not None


def claim_ref(paper: str, claim_id: str) -> str:
    """A claim's identity across the workspace: its id when that names the
    paper, and the paper's key with it otherwise."""
    return claim_id if derived(paper, claim_id) else f"{paper}:{claim_id}"


def path(kind: str, identifier: str, workspace: str | None = None, paper: str | None = None) -> str:
    """The permanent path to one paper (`paper`), claim (`claim`), or ledger
    entry (`ledger`). A claim whose id does not name its paper carries the
    paper too. The default workspace is left out of the address."""
    selected = config.workspace_id() if workspace is None else workspace
    params = {}
    if paper is not None and not derived(paper, identifier):
        params["paper"] = paper
    if selected != config.DEFAULT_WORKSPACE_ID:
        params["workspace"] = selected
    query = "?" + urlencode(params) if params else ""
    return f"/{kind}/{quote(identifier, safe='')}{query}"


def _open(**params: str) -> RedirectResponse:
    # Always name the workspace: without it the page opens the one last used
    # in this browser, which need not be the one the link was made in.
    fragment = urlencode({"ws": config.workspace_id(), "view": "claims", **params})
    return RedirectResponse(f"/#{fragment}", status_code=303, headers={"Cache-Control": "no-store"})


def _missing(what: str, *, holders: list[str] | None = None, claim_id: str = "") -> HTMLResponse:
    home = "/#" + urlencode({"ws": config.workspace_id()})
    if holders:
        # An old link to an id several papers hold cannot say which it meant,
        # so the reader is offered each of them.
        choices = "".join(
            f"<li><a href='{html.escape(path('claim', claim_id, paper=key))}'>{html.escape(key)}</a></li>"
            for key in holders)
        message = f"<p>{html.escape(what)} is on more than one paper:</p><ul>{choices}</ul>"
    else:
        message = f"<p>{html.escape(what)} is not in this workspace. It may have been removed.</p>"
    return HTMLResponse(
        "<!DOCTYPE html><meta charset='utf-8'><title>Not found · Doxograph</title>"
        f"{message}<p><a href='{html.escape(home)}'>Open the library</a></p>",
        status_code=404, headers={"Cache-Control": "no-store"},
    )


def _holds(key: str, claim_id: str) -> bool:
    return _has_paper(key) and any(c["id"] == claim_id for c in store.load_paper(key).get("claims", []))


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


# A path converter, because a hand-written or restored id may hold a slash.
@router.get("/claim/{claim_id:path}", include_in_schema=False)
def claim(claim_id: str, paper: str | None = None) -> Response:
    if paper is not None:
        return _open(paper=paper, sel=claim_id, show="claim") if _holds(paper, claim_id) else _missing(f"Claim {claim_id}")
    # A claim id is normally its paper's key and a sequence number, so the
    # paper is read alone. Other ids are found by a scan.
    owner = re.fullmatch(r"(.+)-c\d+", claim_id)
    if owner and _holds(owner.group(1), claim_id):
        return _open(paper=owner.group(1), sel=claim_id, show="claim")
    holders = [p["key"] for p in store.all_papers()
               if any(c["id"] == claim_id for c in p.get("claims", []))]
    if len(holders) != 1:
        return _missing(f"Claim {claim_id}", holders=holders, claim_id=claim_id)
    return _open(paper=holders[0], sel=claim_id, show="claim")


@router.get("/ledger/{ledger_id:path}", include_in_schema=False)
def ledger(ledger_id: str) -> Response:
    if not any(entry["id"] == ledger_id for entry in store.load_ledger()):
        return _missing(f"Your claim {ledger_id}")
    return _open(ledger=ledger_id)
