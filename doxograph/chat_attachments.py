"""Portable chat attachments, stored with the conversation rather than in the paper library."""
from __future__ import annotations

import base64
import binascii
import io
from typing import Any, Literal

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pypdf import PdfReader

from .model_errors import InputTooLargeError, ModelInputError

FILE_LIMIT = 4 * 1024 * 1024
CONVERSATION_LIMIT = 6 * 1024 * 1024


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255, pattern=r"^[^/\\\x00-\x1f]+$")
    media_type: Literal["image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "text/plain"]
    data: str = Field(min_length=1, max_length=4 * ((FILE_LIMIT + 2) // 3))

    @model_validator(mode="after")
    def valid_file(self) -> Attachment:
        try:
            raw = base64.b64decode(self.data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Attachment data must be base64 encoded.") from exc
        if not raw or len(raw) > FILE_LIMIT:
            raise ValueError("Each attachment must be nonempty and at most 4 MB.")
        if self.media_type.startswith("image/"):
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    multi_picture = image.format == "MPO"
                    detected_format = "JPEG" if multi_picture else image.format or ""
                    if Image.MIME.get(detected_format) != self.media_type:
                        raise ValueError("The image format does not match its file type.")
                    image.verify()
                # Verification checks structure/checksums but some decoders defer pixel errors.
                # Reopen after verify(), which may close the source, and decode all sent frames.
                with Image.open(io.BytesIO(raw)) as image:
                    for picture in range(getattr(image, "n_frames", 1)):
                        image.seek(picture)
                        if image.width > 8000 or image.height > 8000 or image.width * image.height > 25_000_000:
                            raise ValueError("Images must be at most 8000 pixels per side and 25 megapixels.")
                        image.load()
            except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
                raise ValueError("This image could not be read. Use PNG, JPEG, GIF, or WebP.") from exc
        elif self.media_type == "application/pdf":
            try:
                pdf = PdfReader(io.BytesIO(raw))
                if pdf.is_encrypted or not 1 <= len(pdf.pages) <= 100:
                    raise ValueError("Use an unencrypted PDF with 1–100 pages.")
            except Exception as exc:
                raise ValueError("Use a readable, unencrypted PDF with 1–100 pages.") from exc
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError("Text attachments must use UTF-8 encoding.") from exc
            if "\x00" in text:
                raise ValueError("This file is binary. Attach an image, PDF, or UTF-8 text file.")
        return self


def check_attachments(messages: list[Any]) -> None:
    if any(m.attachments and m.role != "user" for m in messages):
        raise ValueError("Only user messages may have attachments.")
    if sum(len(base64.b64decode(a.data)) for m in messages for a in m.attachments) > CONVERSATION_LIMIT:
        raise ValueError("Attachments in a conversation may total at most 6 MB. Start a new conversation.")
    if any(not m.content.strip() and not m.attachments for m in messages):
        raise ValueError("Each message needs text or an attachment.")


def conversation_blocks(messages: list[dict[str, Any]], *, text_pdfs: bool = False) -> list[dict[str, Any]]:
    """Label files beside their original turn; never serialize base64 as prompt text."""
    blocks: list[dict[str, Any]] = []
    for number, message in enumerate(messages, 1):
        blocks.append({"type": "text", "text": f"Message {number} ({message['role']}):\n{message['content']}"})
        for attachment in message.get("attachments", []):
            name, media = attachment["name"], attachment["media_type"]
            blocks.append({"type": "text", "text": f"Attachment to message {number}: {name}"})
            if media == "text/plain":
                text = base64.b64decode(attachment["data"]).decode("utf-8-sig")
                blocks.append({"type": "text", "text": text})
            elif media == "application/pdf" and text_pdfs:
                try:
                    pdf = PdfReader(io.BytesIO(base64.b64decode(attachment["data"])))
                    text_pages = [page.extract_text() or "" for page in pdf.pages]
                    pages = [f"[Page {i}]\n{text}" for i, text in enumerate(text_pages, 1)]
                    if not any(text.strip() for text in text_pages):
                        raise ModelInputError(f"{name} has no readable text. Use an OCR-processed PDF or choose Claude.")
                    blocks.append({"type": "text", "text": "\n\n".join(pages)})
                except ModelInputError:
                    raise
                except Exception as exc:
                    raise ModelInputError(f"Could not read {name}. Attach a readable PDF.") from exc
            else:
                blocks.append({"type": "image" if media.startswith("image/") else "document",
                               "source": {"type": "base64", "media_type": media, "data": attachment["data"]}})
    if sum(len(block.get("text", "")) for block in blocks) > 600_000:
        raise InputTooLargeError("The conversation and attachments contain too much text. Use smaller files or start a new conversation.")
    return blocks
