"""The research context and the ledger of your own claims.

Part of the store: import it through `doxograph.store`, which re-exports
every name here and says why the rest of the store is reached as
`store.X` rather than imported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import config, storage, store, workspace_metadata


# --- your own claims, and what the research is about ----------------------

def context_path() -> Path:
    return config.data_dir() / "context.md"


def load_context() -> str:
    """The free-text description of the research, given to every model pass."""
    return storage.read_text(context_path()).strip()


def save_context(text: str) -> None:
    with store.document_lock("context"):
        load_context()
        store.write_atomic(context_path(), (text or "").strip() + "\n")


def _ledger() -> dict[str, Any]:
    return storage.read_yaml_model(
        config.ledger_path(), workspace_metadata.ResearchLedger,
    ).model_dump(exclude_unset=True)


def load_ledger() -> list[dict[str, Any]]:
    return _ledger().get("claims") or []


def save_ledger(claims: list[dict[str, Any]]) -> None:
    with store.document_lock("ledger"):
        payload = _ledger()
        existing = {claim["id"]: claim for claim in payload.get("claims") or []}
        payload["claims"] = [{**existing.get(claim["id"], {}), **claim} for claim in claims]
        workspace_metadata.ResearchLedger.model_validate(payload, strict=True)
        store.write_atomic(
            config.ledger_path(),
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100),
        )
