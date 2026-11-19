"""Select an email's text and enumerate its immediate MIME attachments."""

import re
from base64 import b64decode
from collections.abc import Iterator
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage
from mimetypes import guess_extension
from quopri import decodestring
from urllib.parse import unquote

from bs4 import BeautifulSoup

from .filename_sanitizer import sanitize_filename

_HEADERS = ("Subject", "From", "To", "Cc", "Date", "Message-ID")
_TEXT_TYPES = {"text/plain", "text/html"}
_BILL_WORDS = re.compile(
    r"invoice|bill|charge|statement|freight|账单|帳單|发票|發票|费用|費用", re.I
)
_SIGNATURE_BLOCK = re.compile(r"signature|foxmail", re.I)
_SIGNATURE_SEPARATOR = re.compile(r"^\s*--[ \t]+\s*$")
_MIME_HEADER_END = re.compile(rb"\r\n\r\n|\n\n|\r\r")


class EmlParseError(ValueError):
    pass


def _content_id(value: str) -> str:
    return unquote(value).strip().strip("<>").casefold()


def _related_root(message: EmailMessage, parts: list[EmailMessage]) -> EmailMessage:
    start = _content_id(message.get_param("start") or "")
    if start:
        for part in parts:
            if _content_id(part.get("Content-ID", "")) == start:
                return part
    return parts[0]


def _is_attachment(message: EmailMessage) -> bool:
    return (
        message.get_content_disposition() == "attachment"
        or message.get_filename() is not None
        or not (
            message.get_content_type() in _TEXT_TYPES
            or message.get_content_maintype() == "multipart"
        )
    )


@dataclass(frozen=True, slots=True)
class EmailBody:
    text: str
    data: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class EmailAttachment:
    message: EmailMessage
    in_related: bool
    source: bytes | None

    @property
    def media_type(self) -> str:
        return self.message.get_content_type()

    def filename(self, number: int) -> str:
        name = self.message.get_filename()
        if name:
            return name
        extension = (
            ".eml"
            if self.media_type == "message/rfc822"
            else guess_extension(self.media_type, strict=False) or ".bin"
        )
        return f"attachment-{number}{extension}"

    def read_bytes(self) -> bytes:
        if self.media_type == "message/rfc822":
            encoding = self.message.get("Content-Transfer-Encoding", "").lower()
            if self.source is not None and (header_end := _MIME_HEADER_END.search(self.source)):
                return _decode_transfer(self.source[header_end.end() :], encoding)
            if not self.message.is_multipart():
                return self.message.get_payload(decode=True) or b""
            enclosed, *rest = self.message.get_payload()
            if rest:
                raise EmlParseError("message/rfc822 附件包含多个邮件载荷")
            if encoding == "base64":
                return _decode_transfer(enclosed.get_payload(decode=True) or b"", encoding)
            wire = enclosed.as_bytes(policy=policy.SMTP.clone(refold_source="none"))
            return _decode_transfer(wire, encoding)
        return self.message.get_payload(decode=True) or b""


def _decode_transfer(wire: bytes, encoding: str) -> bytes:
    if encoding == "base64":
        return b64decode(b"".join(wire.split()), validate=True)
    if encoding == "quoted-printable":
        return decodestring(wire)
    return wire


@dataclass(slots=True)
class ParsedEmail:
    message: EmailMessage
    source: bytes
    headers: dict[str, str]
    body: EmailBody | None
    image_cids: set[str] = field(default_factory=set)
    signature_filenames: set[str] = field(default_factory=set)

    def attachments(self) -> Iterator[EmailAttachment]:
        yield from _attachments(self.message, self.source)

    def is_signature_image(self, attachment: EmailAttachment, filename: str) -> bool:
        cid = _content_id(attachment.message.get("Content-ID", ""))
        if cid and cid in self.image_cids:
            return True
        lower_name = filename.casefold()
        return (
            (lower_name.startswith("insertpic_") or "_foxmail." in lower_name)
            and (attachment.in_related or lower_name in self.signature_filenames)
            and not _BILL_WORDS.search(lower_name)
        )


def _wire_parts(data: bytes | None, boundary: str | None) -> Iterator[bytes]:
    if data is None or not boundary:
        return
    # Keep the embedded mail's original line endings and header folding for SHA-256.
    # The line break immediately before a delimiter belongs to the MIME envelope.
    delimiter = re.compile(
        rb"(?:\A|(?<=[\r\n]))--" + re.escape(boundary.encode()) + rb"(--)?[ \t]*(?:\r\n|\n|\r|\Z)"
    )
    start = None
    for mark in delimiter.finditer(data):
        if start is not None:
            end = mark.start()
            if data[end - 2 : end] == b"\r\n":
                end -= 2
            elif data[end - 1 : end] in {b"\n", b"\r"}:
                end -= 1
            yield data[start:end]
        if mark[1]:
            return
        start = mark.end()
    if start is not None:
        yield data[start:]


def _attachments(
    message: EmailMessage, source: bytes | None, in_related: bool = False
) -> Iterator[EmailAttachment]:
    if _is_attachment(message):
        yield EmailAttachment(message, in_related, source)
    elif message.is_multipart():
        parts = list(message.iter_parts())
        related = message.get_content_type() == "multipart/related" and bool(parts)
        root = _related_root(message, parts) if related else None
        wire_parts = _wire_parts(source, message.get_boundary())
        for part in parts:
            wire = next(wire_parts, None)
            if related and part is not root:
                yield EmailAttachment(part, in_related=True, source=wire)
            else:
                yield from _attachments(part, wire, in_related or related)


def _html_text(html: str, cids: set[str], signature_names: set[str]) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "head"]):
        tag.decompose()
    for image in soup.find_all("img", src=True):
        source = str(image["src"]).strip()
        if source.lower().startswith("cid:"):
            cids.add(_content_id(source[4:]))
        if (
            image.find_parent(id=_SIGNATURE_BLOCK)
            or image.find_parent(class_=_SIGNATURE_BLOCK)
            or image.find_previous(string=_SIGNATURE_SEPARATOR)
        ):
            name = source.removeprefix("cid:").split("?", 1)[0]
            signature_names.add(sanitize_filename(unquote(name)).casefold())
    return soup.get_text("\n", strip=True)


def _body_parts(
    message: EmailMessage, cids: set[str], signature_names: set[str]
) -> list[EmailBody]:
    if _is_attachment(message):
        return []
    media_type = message.get_content_type()
    if message.is_multipart():
        parts = list(message.iter_parts())
        if media_type == "multipart/related" and parts:
            return _body_parts(_related_root(message, parts), cids, signature_names)
        alternatives = [_body_parts(part, cids, signature_names) for part in parts]
        if media_type == "multipart/alternative":
            # Inspect every HTML representation for CID references before choosing the text.
            nonempty = [part for part in alternatives if part]
            if not nonempty:
                return []
            return next(
                (
                    part
                    for part in nonempty
                    if all(body.media_type == "text/plain" for body in part)
                ),
                nonempty[0],
            )
        return [body for group in alternatives for body in group]
    if media_type not in _TEXT_TYPES:
        return []
    data = message.get_payload(decode=True) or b""
    try:
        text = data.decode(message.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    if media_type == "text/html":
        text = _html_text(text, cids, signature_names)
    return [EmailBody(text, data, media_type)] if text.strip() else []


def parse_eml(data: bytes) -> ParsedEmail:
    message = message_from_bytes(data, policy=policy.default)
    if not message.keys() or (
        message.get_content_maintype() == "multipart" and not message.is_multipart()
    ):
        raise EmlParseError("邮件头或 MIME 分隔符无效")
    headers = {name: str(message[name]) for name in _HEADERS if name in message}
    cids: set[str] = set()
    signature_names: set[str] = set()
    parts = _body_parts(message, cids, signature_names)
    body = parts[0] if parts else None
    if len(parts) > 1:
        body = EmailBody(
            "\n\n".join(part.text for part in parts),
            b"\n\n".join(part.data for part in parts),
            "text/plain",
        )
    return ParsedEmail(message, data, headers, body, cids, signature_names)
