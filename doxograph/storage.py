"""Validated reads of saved research, shared by readers and writers.

Only a missing file has an empty default. Damaged files raise an error carrying
their path so the library can report them without letting a writer replace them.
"""

from pathlib import Path

from pydantic import BaseModel, ValidationError


class StorageReadError(ValueError):
    def __init__(self, path: Path, detail: str):
        self.path = path
        super().__init__(f"Cannot read {path}: {detail}. The saved file has been left untouched; "
                         "repair it or restore it from a backup.")


def read_model[T: BaseModel](path: Path, model: type[T]) -> T:
    """Validate a document without coercing malformed field types."""
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return model()
    except (OSError, UnicodeError) as exc:
        raise StorageReadError(path, str(exc)) from exc
    try:
        return model.model_validate_json(content, strict=True)
    except ValidationError as exc:
        error = exc.errors(include_input=False, include_url=False)[0]
        if error["type"] == "json_invalid":
            detail = "not valid JSON"
        elif error["type"] == "model_type":
            detail = "should hold an object"
        else:
            location = ".".join(str(part) for part in error["loc"]) or "document"
            detail = f"{location}: {error['msg']}"
        raise StorageReadError(path, detail) from exc
