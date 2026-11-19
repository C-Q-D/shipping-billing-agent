"""Normalize upload names before using them to route document bytes."""

import ntpath
import re
import unicodedata

from shipping_billing_agent.config.limits import ALLOWED_UPLOAD_EXTENSIONS
from shipping_billing_agent.domain.models import SourceKind

_MAX_FILENAME_BYTES = 255
_SOURCE_BY_EXTENSION = {
    ".eml": SourceKind.EML,
    ".pdf": SourceKind.PDF,
    ".jpg": SourceKind.IMAGE,
    ".jpeg": SourceKind.IMAGE,
    ".png": SourceKind.IMAGE,
    ".tif": SourceKind.IMAGE,
    ".tiff": SourceKind.IMAGE,
    ".xls": SourceKind.EXCEL,
    ".xlsx": SourceKind.EXCEL,
    ".doc": SourceKind.WORD,
    ".docx": SourceKind.WORD,
}


def sanitize_filename(raw_name: str) -> str:
    name = ntpath.basename(raw_name)
    name = "".join(
        char
        for char in name
        if char.isspace() or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )
    name = re.sub(r'\s+|[<>:"|?*]+', "_", name.strip()).strip(".") or "unnamed"
    stem, suffix = ntpath.splitext(name)
    suffix_size = len(suffix.encode("utf-8"))
    if suffix_size >= _MAX_FILENAME_BYTES:
        stem, suffix, suffix_size = name, "", 0
    return (
        stem.encode("utf-8")[: _MAX_FILENAME_BYTES - suffix_size].decode("utf-8", errors="ignore")
        + suffix
    )


def detect_source_kind(filename: str, *, is_url: bool = False) -> SourceKind | None:
    if is_url:
        return SourceKind.URL
    extension = ntpath.splitext(filename)[1].lower()
    if extension in ALLOWED_UPLOAD_EXTENSIONS:
        return _SOURCE_BY_EXTENSION[extension]
    return None
