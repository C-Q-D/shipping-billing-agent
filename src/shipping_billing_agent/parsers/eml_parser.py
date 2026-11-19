"""Select an email's text and enumerate its immediate MIME attachments."""

import re
from base64 import b64decode
from collections.abc import Iterator
from dataclasses import dataclass, field
from email import policy
from email.message import Message
from email.parser import BytesHeaderParser
from mimetypes import guess_extension
from quopri import decodestring
from urllib.parse import unquote

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .filename_sanitizer import sanitize_filename

_HEADERS = ("Subject", "From", "To", "Cc", "Date", "Message-ID")
_TEXT_TYPES = {"text/plain", "text/html"}
_BILL_WORDS = re.compile(
    r"invoice|bill|charge|statement|freight|账单|帳單|发票|發票|费用|費用", re.I
)
_BILL_TAG_WORDS = re.compile(
    r"invoice|bill|debit\s*note|credit\s*note|charge|statement|账单|帳單|发票|發票|费用|費用",
    re.I,
)
_LOGO_WORDS = re.compile(
    r"\b(?:logo|icon|badge|avatar|banner|wechat|qrcode)\b|徽标|标志|图标", re.I
)
_BILL_AMOUNT_HINT = re.compile(r"\b(?:USD|EUR|GBP|CNY|RMB|HKD|JPY)\b|[$€£¥]\s*\d", re.I)
_CONTENT_LANDMARKS = {"article", "aside", "footer", "header", "main", "section"}
_FOOTER_UNITS = {"div", "p", "figure", "section", "article", "aside", "table", "ul", "ol"}
_SIGNATURE_BLOCK = re.compile(r"signature|foxmail", re.I)
_SIGNATURE_SEPARATOR = re.compile(r"^[ \t]*--[ \t]+\r?$", re.MULTILINE)
_SIGNATURE_CONTACT = re.compile(r"\b(?:tl|tel|web|email|fax)\s*[:：]", re.I)
_SIGNOFF = re.compile(r"\b(?:regards|sincerely)\b|此致|敬上|敬礼", re.I)
_DECORATIVE_IMAGE = re.compile(r"_foxmail\.|insertpic_", re.I)
_SIGNATURE_TAIL_SIZE = 8
_MIME_HEADER_END = re.compile(rb"\r\n\r\n|\n\n|\r\r")
_MIME_LINE_BREAK = re.compile(rb"\r\n|\r|\n")


class EmlParseError(ValueError):
    pass


def _content_id(value: str) -> str:
    return unquote(value).strip().strip("<>").casefold()


def _is_attachment(message: Message) -> bool:
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
class _MimePart:
    """Headers and offsets into the original wire bytes. Payloads are loaded on demand."""

    headers: Message
    source: bytes
    start: int
    body_start: int
    end: int

    @classmethod
    def from_wire(
        cls, source: bytes, start: int, end: int, *, default_type: str = "text/plain"
    ) -> "_MimePart":
        separator = _MIME_LINE_BREAK.match(source, start, end) or _MIME_HEADER_END.search(
            source, start, end
        )
        body_start = separator.end() if separator is not None else end
        headers = BytesHeaderParser(_class=Message, policy=policy.default).parsebytes(
            source[start:body_start]
        )
        headers.set_default_type(default_type)
        return cls(headers, source, start, body_start, end)

    @property
    def media_type(self) -> str:
        return self.headers.get_content_type()

    @property
    def content_id(self) -> str:
        return _content_id(self.headers.get("Content-ID", ""))

    def iter_parts(self) -> Iterator["_MimePart"]:
        default_type = "message/rfc822" if self.media_type == "multipart/digest" else "text/plain"
        for start, end in _wire_parts(self):
            yield _MimePart.from_wire(self.source, start, end, default_type=default_type)

    def filename(self, number: int) -> str:
        name = self.headers.get_filename()
        if name:
            return name
        extension = (
            ".eml"
            if self.media_type == "message/rfc822"
            else guess_extension(self.media_type, strict=False) or ".bin"
        )
        return f"attachment-{number}{extension}"

    def wire_bytes(self) -> bytes:
        return self.source[self.start : self.end]

    def read_bytes(self) -> bytes:
        if self.media_type == "message/rfc822":
            encoding = self.headers.get("Content-Transfer-Encoding", "").lower()
            return _decode_transfer(self.source[self.body_start : self.end], encoding)
        # Decode only this admitted leaf, without recursively parsing MIME structures.
        message = BytesHeaderParser(policy=policy.default).parsebytes(self.wire_bytes())
        return message.get_payload(decode=True) or b""


def _decode_transfer(wire: bytes, encoding: str) -> bytes:
    if encoding == "base64":
        return b64decode(b"".join(wire.split()), validate=True)
    if encoding == "quoted-printable":
        return decodestring(wire)
    return wire


@dataclass(slots=True)
class ParsedEmail:
    headers: dict[str, str]
    body: EmailBody | None = None
    attachments: list[_MimePart] = field(default_factory=list)
    signature_cids: set[str] = field(default_factory=set)
    signature_filenames: set[str] = field(default_factory=set)
    truncated: bool = False

    def is_signature_image(self, attachment: _MimePart, filename: str) -> bool:
        if _BILL_WORDS.search(filename):
            return False
        return (
            attachment.content_id in self.signature_cids
            or sanitize_filename(filename).casefold() in self.signature_filenames
        )


def _wire_parts(part: _MimePart) -> Iterator[tuple[int, int]]:
    boundary = part.headers.get_boundary()
    if not boundary:
        return
    # Keep the embedded mail's original line endings and header folding for SHA-256.
    # The line break immediately before a delimiter belongs to the MIME envelope.
    delimiter = re.compile(
        rb"(?:\A|(?<=[\r\n]))--" + re.escape(boundary.encode()) + rb"(--)?[ \t]*(?:\r\n|\n|\r|\Z)"
    )
    start = None
    for mark in delimiter.finditer(part.source, part.body_start, part.end):
        if start is not None:
            end = mark.start()
            if end - start >= 2 and part.source[end - 2 : end] == b"\r\n":
                end -= 2
            elif end > start and part.source[end - 1 : end] in {b"\n", b"\r"}:
                end -= 1
            yield start, end
        if mark[1]:
            return
        start = mark.end()
    if start is not None:
        yield start, part.end


def _related_root_start(part: _MimePart) -> int | None:
    start = _content_id(part.headers.get_param("start") or "")
    if not start:
        return next((offset for offset, _ in _wire_parts(part)), None)
    parts = part.iter_parts()
    first = next(parts, None)
    if first is None:
        return None
    if start != first.content_id:
        return next((child.start for child in parts if child.content_id == start), first.start)
    return first.start


_HtmlMarker = tuple[Tag | NavigableString, Tag | None]


def _is_bill_image_tag(image: Tag) -> bool:
    labels = [image.get(attribute) for attribute in ("alt", "title", "aria-label")]
    if (figure := image.find_parent("figure")) is not None:
        labels.append(figure.get("aria-label"))
        labels.extend(
            caption.get_text(" ", strip=True) for caption in figure.find_all("figcaption")
        )
    label_texts = [str(label) for label in labels if label]
    if any(_LOGO_WORDS.search(label) for label in label_texts):
        return False
    return any(_BILL_TAG_WORDS.search(label) for label in label_texts)


def _signature_scope(node: Tag | NavigableString, body: Tag) -> Tag | None:
    """Return the nearest content landmark, or None inside an explicit signature."""
    scope = body
    for parent in node.parents:
        if any(
            _SIGNATURE_BLOCK.search(str(parent.get(attribute, "")))
            for attribute in ("id", "class")
        ):
            return None
        if parent is body:
            break
        if scope is body and parent.name in _CONTENT_LANDMARKS:
            scope = parent
    return scope


def _signature_context(
    markers: list[_HtmlMarker],
    index: int,
    scope: Tag,
    body_scope: Tag,
) -> tuple[str, bool]:
    start = max(0, index - _SIGNATURE_TAIL_SIZE)
    separator_text = ""
    has_separator = False
    for previous_index in range(index - 1, -1, -1):
        node, node_scope = markers[previous_index]
        if node_scope is None:
            continue
        if node_scope is not scope and node_scope is not body_scope:
            # An intervening landmark bounds the preceding signature context.
            start = max(start, previous_index + 1)
            break
        if isinstance(node, NavigableString):
            parts = _SIGNATURE_SEPARATOR.split(node)
            if len(parts) == 1:
                continue
            if node_scope is scope and previous_index >= start:
                separator_text = parts[-1]
        elif node.name != "hr":
            continue
        start = max(start, previous_index + 1)
        has_separator = True
        break

    trailing_text = ""
    for next_index in range(index + 1, len(markers)):
        node, node_scope = markers[next_index]
        if node_scope is not None and node_scope is not scope and node_scope is not body_scope:
            break
        if isinstance(node, NavigableString):
            parts = _SIGNATURE_SEPARATOR.split(node, maxsplit=1)
            if len(parts) == 1:
                continue
            if node_scope is scope:
                trailing_text = parts[0]
        elif node.name != "hr":
            continue
        break
    else:
        next_index = len(markers)

    text = [separator_text] if separator_text else []
    text.extend(
        node
        for node, node_scope in markers[start:next_index]
        if node_scope is scope and isinstance(node, NavigableString)
    )
    if trailing_text:
        text.append(trailing_text)
    return "\n".join(text), has_separator


def _footer_unit(node: Tag | NavigableString, footer: Tag) -> Tag | None:
    for parent in node.parents:
        if parent is footer:
            break
        if parent.parent is footer:
            return parent if parent.name in _FOOTER_UNITS else None
    return None


def _ends_footer_context(marker: _HtmlMarker, footer: Tag, body_scope: Tag) -> bool:
    node, scope = marker
    other_landmark = scope is not None and scope is not footer and scope is not body_scope
    return other_landmark or (isinstance(node, Tag) and node.name == "hr")


def _previous_footer_unit_text(
    markers: list[_HtmlMarker],
    index: int,
    win_start: int,
    body_scope: Tag,
    footer_tag: Tag,
    unit: Tag,
) -> str:
    prev_unit: Tag | None = None
    boundary: Tag | NavigableString | None = None
    texts: list[str] = []
    for idx in range(index - 1, -1, -1):
        marker = markers[idx]
        node, scope = marker
        if _ends_footer_context(marker, footer_tag, body_scope) or (
            isinstance(node, NavigableString) and _SIGNATURE_SEPARATOR.search(node)
        ):
            boundary = node
            break
        marker_unit = _footer_unit(node, footer_tag)
        if marker_unit is unit:
            continue
        if prev_unit is None:
            if scope is not footer_tag or marker_unit is None:
                return ""
            prev_unit = marker_unit
        elif marker_unit is not prev_unit:
            break
        if (
            idx >= win_start
            and scope is footer_tag
            and isinstance(node, NavigableString)
            and node.find_parent("figure") is None
        ):
            texts.append(node)

    if prev_unit is None or not texts:
        return ""
    # The whole unit matters here, including figures without text or image markers.
    nodes = prev_unit.descendants
    if boundary is not None and _footer_unit(boundary, footer_tag) is prev_unit:
        for node in nodes:
            if node is boundary:
                break
    if any(isinstance(node, Tag) and node.name in {"img", "figure"} for node in nodes):
        return ""
    return "\n".join(reversed(texts))


def _footer_signature_context(
    markers: list[_HtmlMarker], index: int, body_scope: Tag, footer_tag: Tag, image: Tag
) -> str:
    if (figure := image.find_parent("figure")) is not None:
        return "\n".join(
            node.strip()
            for node in figure.strings
            if node.strip() and node.find_parent("figure") is figure
        )

    unit = _footer_unit(image, footer_tag)
    win_start = max(0, index - _SIGNATURE_TAIL_SIZE)
    for idx in range(index - 1, win_start - 1, -1):
        marker = markers[idx]
        node, _ = marker
        if _ends_footer_context(marker, footer_tag, body_scope) or (
            isinstance(node, NavigableString) and _SIGNATURE_SEPARATOR.search(node)
        ):
            win_start = idx + 1
            break

    separator = None
    for win_end in range(index + 1, len(markers)):
        marker = markers[win_end]
        if _ends_footer_context(marker, footer_tag, body_scope):
            break
        node, _ = marker
        if isinstance(node, NavigableString) and (separator := _SIGNATURE_SEPARATOR.search(node)):
            win_end += 1
            break
    else:
        win_end = len(markers)

    unit_texts: list[tuple[int, str]] = []
    unit_image_indices: list[int] = []
    for idx in range(win_start, win_end):
        node, scope = markers[idx]
        if (
            scope is not footer_tag
            or _footer_unit(node, footer_tag) is not unit
            or node.find_parent("figure") is not None
        ):
            continue
        if isinstance(node, NavigableString):
            text = node[:separator.start()] if separator and idx == win_end - 1 else node
            if text.strip():
                unit_texts.append((idx, text))
        elif node.name == "img":
            unit_image_indices.append(idx)

    if not unit_texts:
        if unit is None:
            return ""
        return _previous_footer_unit_text(markers, index, win_start, body_scope, footer_tag, unit)

    unit_signoff_indices = [idx for idx, text in unit_texts if _SIGNOFF.search(text)]
    if unit_signoff_indices and index < unit_signoff_indices[0]:
        first_signoff = unit_signoff_indices[0]
        has_image_after_signoff = any(img_idx > first_signoff for img_idx in unit_image_indices)
        pre_signoff_text = "\n".join(text for idx, text in unit_texts if idx < first_signoff)
        if (
            has_image_after_signoff
            or _BILL_TAG_WORDS.search(pre_signoff_text)
            or _BILL_AMOUNT_HINT.search(pre_signoff_text)
        ):
            return pre_signoff_text
    return "\n".join(text for _, text in unit_texts)


def _html_text(html: str, email: ParsedEmail) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "head"]):
        tag.decompose()
    body = soup.body or soup
    markers = [
        (node, _signature_scope(node, body))
        for node in soup.descendants
        if (isinstance(node, Tag) and node.name in {"img", "hr"})
        or (isinstance(node, NavigableString) and not isinstance(node, Comment) and node.strip())
    ]

    for index, (image, scope) in enumerate(markers):
        if not isinstance(image, Tag) or image.name != "img" or not image.get("src"):
            continue
        if _is_bill_image_tag(image):
            continue
        source = str(image["src"]).strip()
        if scope is not None:
            if len(markers) - index > _SIGNATURE_TAIL_SIZE:
                continue
            context, has_separator = _signature_context(markers, index, scope, body)
            if scope.name == "footer" and not has_separator:
                context = _footer_signature_context(markers, index, body, scope, image)
            has_contact = _SIGNATURE_CONTACT.search(context) is not None
            has_signoff = _SIGNOFF.search(context) is not None
            after_signoff = has_separator and has_contact
            footer_signature = scope.name == "footer" and has_contact and has_signoff
            decorative = _DECORATIVE_IMAGE.search(source) and (has_contact or has_signoff)
            if not (after_signoff or footer_signature or decorative):
                continue
        if source.lower().startswith("cid:"):
            if cid := _content_id(source[4:]):
                email.signature_cids.add(cid)
        else:
            name = source.split("?", 1)[0]
            email.signature_filenames.add(sanitize_filename(unquote(name)).casefold())
    return soup.get_text("\n", strip=True)


def _read_body(part: _MimePart, email: ParsedEmail) -> EmailBody:
    data = part.read_bytes()
    try:
        text = data.decode(part.headers.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    if part.media_type == "text/html":
        text = _html_text(text, email)
    return EmailBody(text, data, part.media_type)


def parse_eml(data: bytes, *, max_children: int) -> ParsedEmail:
    root = _MimePart.from_wire(data, 0, len(data))
    if not root.headers.keys():
        raise EmlParseError("邮件头或 MIME 分隔符无效")
    if root.headers.get_content_maintype() == "multipart" and next(_wire_parts(root), None) is None:
        raise EmlParseError("邮件头或 MIME 分隔符无效")
    headers = {name: str(root.headers[name]) for name in _HEADERS if name in root.headers}
    email = ParsedEmail(headers)
    slots = max_children
    body_reserved = False

    def collect(part: _MimePart, *, related_resource: bool = False) -> list[EmailBody]:
        nonlocal slots, body_reserved
        if related_resource or _is_attachment(part.headers):
            if slots == 0:
                email.truncated = True
            else:
                # Keep the wire range only. The dispatcher checks the shared budget again
                # before loading the payload, after any earlier nested mail has expanded.
                email.attachments.append(part)
                slots -= 1
            return []

        media_type = part.media_type
        if media_type in _TEXT_TYPES:
            body = _read_body(part, email)
            if not body.text.strip():
                return []
            if not body_reserved:
                if slots == 0:
                    email.truncated = True
                    return []
                body_reserved = True
                slots -= 1
            return [body]

        related_root = _related_root_start(part) if media_type == "multipart/related" else None
        groups: list[list[EmailBody]] = []
        for child in part.iter_parts():
            bodies = collect(child, related_resource=related_root not in {None, child.start})
            if bodies:
                groups.append(bodies)
            if email.truncated:
                break
        if groups and media_type == "multipart/alternative":
            plain_groups = (
                group for group in groups if all(body.media_type == "text/plain" for body in group)
            )
            return next(
                plain_groups,
                groups[0],
            )
        return [body for group in groups for body in group]

    bodies = collect(root)
    if len(bodies) == 1:
        email.body = bodies[0]
    elif bodies:
        email.body = EmailBody(
            "\n\n".join(body.text for body in bodies),
            b"\n\n".join(body.data for body in bodies),
            "text/plain",
        )
    return email
