"""Publication dates retain the precision supplied by the source."""

import re
from datetime import date
from typing import Annotated

from pydantic import AfterValidator


def validate(value: str) -> str:
    """Accept calendar dates as YYYY, YYYY-MM, or YYYY-MM-DD."""
    if not re.fullmatch(r"[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?", value):
        raise ValueError("publication_date must be YYYY, YYYY-MM, or YYYY-MM-DD")
    parts = [int(part) for part in value.split("-")]
    date(*parts, *([1] * (3 - len(parts))))
    return value


PublicationDate = Annotated[str, AfterValidator(validate)]


def from_timestamp(value: str) -> str | None:
    """Use the source's calendar date, never an import or revision timestamp."""
    try:
        return validate(value.split("T", 1)[0])
    except ValueError:
        return None


def from_parts(value: object) -> str | None:
    """Read Crossref date-parts without filling in an unknown month or day."""
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        return None
    if any(type(part) is not int for part in value):
        return None
    text = "-".join(f"{part:0{4 if i == 0 else 2}d}" for i, part in enumerate(value))
    try:
        return validate(text)
    except ValueError:
        return None
