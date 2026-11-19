"""Select an email's text and enumerate its immediate MIME attachments."""

import re
from base64 import b64decode
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from email import policy
from email.message import Message
from email.parser import BytesHeaderParser
from mimetypes import guess_extension
from quopri import decodestring
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from shipping_billing_agent.domain.currency import ACTIVE_ISO4217_CODES

from .filename_sanitizer import sanitize_filename

_HEADERS = ("Subject", "From", "To", "Cc", "Date", "Message-ID")
_TEXT_TYPES = {"text/plain", "text/html"}
_BILL_WORDS = re.compile(
    r"invoice|bill|charge|statement|freight|账单|帳單|发票|發票|费用|費用", re.I
)
_BILL_TAG_WORDS = re.compile(
    r"\b(?:invoice|invoices|bill|bills|billing|debit\s*note|credit\s*note|charges?"
    r"|statement|statements)\b|账单|帳單|发票|發票|费用|費用",
    re.I,
)
_CAPTION_CONTINUATION = re.compile(
    r"^(?:(?:please\s+)?(?:see\s+)?(?:attached|below|enclosed)"
    r"|attached\s+below|see\s+details|附件|见附件|详见附件|见下图|附图)[\s:：.!！]*$",
    re.I,
)
_ISO_CODES = "|".join(sorted((*ACTIVE_ISO4217_CODES, "RMB")))
_SIGN = r"[+\-\u2212]"
_BILL_AMOUNT_HINT = re.compile(
    rf"(?:(?:\b(?:{_ISO_CODES})|(?:US|HK|CA|AU)?\$|[€£¥])\s*(?:{_SIGN}\s*)?\d[\d,]*(?:\.\d+)?)"
    rf"|(?:(?:{_SIGN}\s*)?\d[\d,]*(?:\.\d+)?\s*(?:(?:{_ISO_CODES})\b|元))"
)
_CONTENT_LANDMARKS = {"article", "aside", "footer", "header", "main", "section"}
_FOOTER_CONTAINERS = {
    "div", "p", "section", "article", "aside", "table", "tr", "td", "th", "li", "address", "footer",
}
_INLINE_TAGS = frozenset({
    "a", "abbr", "audio", "b", "bdi", "bdo", "button", "canvas", "cite", "code", "data",
    "del", "dfn", "em", "embed", "font", "i", "iframe", "input", "ins", "kbd", "label",
    "mark", "meter", "object", "output", "picture", "progress", "q", "rp", "rt", "ruby", "s",
    "samp", "select", "small", "span", "strong", "sub", "sup", "textarea", "time", "u", "var",
    "video", "wbr",
})
_BLOCK_DISPLAYS = {"block", "flow-root", "flex", "grid", "list-item", "table"}
_PREFORMATTED_WHITE_SPACE = {"pre", "pre-wrap", "pre-line", "break-spaces"}
_CSS_IMPORTANT = re.compile(r"\s*!\s*important$")
_SIGNATURE_BLOCK = re.compile(r"signature|foxmail", re.I)
_SIGNATURE_SEPARATOR = re.compile(r"^[ \t]*--[ \t]+\r?$", re.MULTILINE)
_SIGNATURE_CONTACT = re.compile(r"\b(?:tl|tel|web|email|fax)\s*[:：]", re.I)
_SIGNOFF = re.compile(r"\b(?:regards|sincerely)\b|此致|敬上|敬礼", re.I)
_DECORATIVE_IMAGE = re.compile(r"_foxmail\.|insertpic_", re.I)
_SIGNATURE_TAIL_SIZE = 8
_MIME_HEADER_END = re.compile(rb"\r\n\r\n|\n\n|\r\r")
_MIME_LINE_BREAK = re.compile(rb"\r\n|\r|\n")
_RFC3986_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_PCT_ENCODED = re.compile(r"%([0-9a-fA-F]{2})")


class EmlParseError(ValueError):
    pass


def _content_id(value: str) -> str:
    return unquote(value).strip().strip("<>").casefold()


def _normalize_pct_encoding(value: str) -> str:
    def normalize_escape(match: re.Match[str]) -> str:
        hex_pair = match[1]
        character = chr(int(hex_pair, 16))
        return character if character in _RFC3986_UNRESERVED else f"%{hex_pair.upper()}"

    return _PCT_ENCODED.sub(normalize_escape, value)


def _normalize_location(value: str, *, base: str = "") -> str:
    value = value.strip()
    if not value:
        return ""
    base = base.strip()
    try:
        if base:
            value = urljoin(base, value)
        parsed = urlsplit(value)
    except ValueError:
        # A malformed URL cannot identify an attachment by location.
        return ""

    path = _normalize_pct_encoding(parsed.path)
    if parsed.scheme or parsed.netloc:
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))
    path = path.strip()
    return f"{path}?{parsed.query}" if path and parsed.query else path


def _classify_non_cid_source(source: str, *, base: str = "") -> tuple[str, str | None]:
    try:
        raw_parsed = urlsplit(source.strip())
        location = _normalize_location(source, base=base)
        resolved = urlsplit(location)
    except ValueError:
        return "", None

    raw_path = unquote(raw_parsed.path).strip()
    if (
        raw_parsed.scheme
        or raw_parsed.netloc
        or raw_path.startswith("/")
        or resolved.scheme
        or resolved.netloc
    ):
        return location, None
    filename = sanitize_filename(raw_path).casefold() if raw_path else None
    return location, filename


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
    base_url: str = ""

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

    @property
    def has_content_location(self) -> bool:
        return bool(self.headers.get("Content-Location", "").strip())

    def resolve_location(self, fallback_base: str = "") -> str:
        raw = self.headers.get("Content-Location", "").strip()
        if not raw:
            return ""
        base = self.headers.get("Content-Base", "").strip() or self.base_url or fallback_base
        return _normalize_location(raw, base=base)

    @property
    def content_location(self) -> str:
        return self.resolve_location()

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
    non_signature_cids: set[str] = field(default_factory=set)
    non_signature_filenames: set[str] = field(default_factory=set)
    signature_locations: set[str] = field(default_factory=set)
    non_signature_locations: set[str] = field(default_factory=set)
    html_bases: set[str] = field(default_factory=set)

    def is_signature_image(self, attachment: _MimePart, filename: str) -> bool:
        if _BILL_WORDS.search(filename):
            return False
        cid = attachment.content_id
        if cid and cid in self.non_signature_cids:
            return False

        locations: set[str] = set()
        if location := attachment.content_location:
            locations.add(location)
            if (
                not attachment.base_url
                and not attachment.headers.get("Content-Base", "").strip()
                and not urlsplit(location).scheme
            ):
                for html_base in self.html_bases:
                    if resolved := attachment.resolve_location(html_base):
                        locations.add(resolved)

        has_location_header = attachment.has_content_location
        normalized_name = sanitize_filename(filename).casefold()
        if not locations.isdisjoint(self.non_signature_locations):
            return False
        if not has_location_header and normalized_name in self.non_signature_filenames:
            return False
        if cid and cid in self.signature_cids:
            return True
        if not locations.isdisjoint(self.signature_locations):
            return True
        return not has_location_header and normalized_name in self.signature_filenames


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


def _is_bill_caption_text(text: str) -> bool:
    return bool(_BILL_TAG_WORDS.search(text) or _BILL_AMOUNT_HINT.search(text))


def _lines_end_with_bill_caption(lines: list[str]) -> bool:
    if not lines:
        return False
    if _is_bill_caption_text(lines[-1]):
        return True
    return (
        len(lines) >= 2
        and _CAPTION_CONTINUATION.fullmatch(lines[-1]) is not None
        and _is_bill_caption_text(lines[-2])
    )


def _lines_start_with_bill_caption(lines: list[str]) -> bool:
    if not lines:
        return False
    if _is_bill_caption_text(lines[0]):
        return True
    return (
        len(lines) >= 2
        and _CAPTION_CONTINUATION.fullmatch(lines[0]) is not None
        and _is_bill_caption_text(lines[1])
    )


def _is_bill_image_tag(image: Tag) -> bool:
    labels = [image.get(attribute) for attribute in ("alt", "title", "aria-label")]
    if (figure := image.find_parent("figure")) is not None:
        labels.append(figure.get("aria-label"))
        labels.extend(
            " ".join(_visual_lines(caption, reset_on_media=False))
            for caption in figure.find_all("figcaption")
            if caption.find_parent("figure") is figure
        )
    label_texts = [str(label) for label in labels if label]
    return any(_is_bill_caption_text(label) for label in label_texts)


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


def _inside_footer(node: Tag, body: Tag) -> bool:
    if node.name == "footer":
        return True
    for parent in node.parents:
        if parent is body:
            break
        if parent.name == "footer":
            return True
    return False


def _in_post_separator_signature(
    markers: list[_HtmlMarker], index: int, scope: Tag, body_scope: Tag
) -> bool:
    segment: list[str] = []
    for previous_index in range(index - 1, -1, -1):
        node, node_scope = markers[previous_index]
        if node_scope is None:
            continue
        if node_scope is not scope and node_scope is not body_scope:
            return False
        if isinstance(node, NavigableString):
            parts = _SIGNATURE_SEPARATOR.split(node)
            if len(parts) == 1:
                continue
            segment.append(parts[-1])
        elif node.name != "hr":
            continue
        sep_start = previous_index + 1
        break
    else:
        return False

    for next_index in range(sep_start, len(markers)):
        node, node_scope = markers[next_index]
        if node_scope is None:
            continue
        if node_scope is not scope and node_scope is not body_scope:
            break
        if isinstance(node, NavigableString):
            parts = _SIGNATURE_SEPARATOR.split(node, maxsplit=1)
            segment.append(parts[0])
            if len(parts) > 1:
                break
        elif node.name == "hr":
            break

    return _SIGNATURE_CONTACT.search("\n".join(segment)) is not None


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


def _nearest_container(node: Tag | NavigableString, footer: Tag) -> Tag:
    if node is not footer:
        for parent in node.parents:
            if parent is footer or parent.name in _FOOTER_CONTAINERS:
                return parent
    return footer


def _direct_child_containing(parent: Tag, node: Tag) -> Tag | None:
    child = node
    while child.parent is not None:
        if child.parent is parent:
            return child
        child = child.parent
    return None


def _has_text(slot: Tag | NavigableString) -> bool:
    if isinstance(slot, NavigableString):
        return not isinstance(slot, Comment) and bool(slot.strip())
    return any(
        isinstance(node, NavigableString) and not isinstance(node, Comment) and node.strip()
        for node in slot.descendants
    )


def _is_significant_child(child: Tag | NavigableString) -> bool:
    if isinstance(child, NavigableString):
        return _has_text(child)
    if child.name == "br":
        return False
    return child.name in {"img", "hr"} or _has_text(child) or child.find(["img", "hr"]) is not None


def _non_empty_children(tag: Tag) -> list[Tag | NavigableString]:
    return [child for child in tag.children if _is_significant_child(child)]


def _ends_footer_context(marker: _HtmlMarker, footer: Tag, body_scope: Tag) -> bool:
    node, scope = marker
    other_landmark = scope is not None and scope is not footer and scope is not body_scope
    return other_landmark or (isinstance(node, Tag) and node.name == "hr")


def _minimal_signature_boxes(
    footer: Tag, text_nodes: list[tuple[int, NavigableString, str]], *, require_signoff: bool = True
) -> Iterator[tuple[Tag, list[tuple[int, str]]]]:
    # Beautiful Soup compares tags by markup, so group by node identity.
    by_container: dict[int, tuple[Tag, list[tuple[int, str]]]] = {}
    for idx, node, text in text_nodes:
        for parent in node.parents:
            if parent is footer or parent.name in _FOOTER_CONTAINERS:
                by_container.setdefault(id(parent), (parent, []))[1].append((idx, text))
            if parent is footer:
                break

    candidates: dict[int, tuple[Tag, list[tuple[int, str]]]] = {}
    for box_id, (box, box_texts) in by_container.items():
        context = "\n".join(text for _, text in box_texts)
        if bool(_SIGNATURE_CONTACT.search(context)) and (
            not require_signoff or bool(_SIGNOFF.search(context))
        ):
            candidates[box_id] = (box, box_texts)

    non_minimal: set[int] = set()
    for box, _ in candidates.values():
        if box is footer:
            continue
        for parent in box.parents:
            if id(parent) in candidates:
                non_minimal.add(id(parent))
                break
            if parent is footer:
                break

    for box_id, candidate in candidates.items():
        if box_id not in non_minimal:
            yield candidate


def _inline_style_value(tag: Tag, property_name: str) -> str:
    style = tag.get("style")
    if not isinstance(style, str):
        return ""

    value = ""
    is_important = False
    for declaration in style.lower().split(";"):
        name, _, raw_value = declaration.partition(":")
        if name.strip() != property_name:
            continue
        raw_value = raw_value.strip()
        priority = _CSS_IMPORTANT.search(raw_value)
        if priority is not None:
            raw_value = raw_value[:priority.start()]
        if raw_value and (priority is not None or not is_important):
            value = " ".join(raw_value.split())
            is_important = priority is not None
    return value


def _is_inline_element(tag: Tag) -> bool:
    display = _inline_style_value(tag, "display")
    keywords = display.split()
    if "inline" in keywords or display.startswith("inline-"):
        return True
    if "block" in keywords or display in _BLOCK_DISPLAYS or display.startswith("table-"):
        return False
    return tag.name in _INLINE_TAGS


def _node_preformatted(tag: Tag, inherited: bool) -> bool:
    white_space = _inline_style_value(tag, "white-space")
    if white_space in {"normal", "nowrap", "initial"}:
        return False
    if tag.name in {"pre", "textarea"} or white_space in _PREFORMATTED_WHITE_SPACE:
        return True
    return inherited


def _initial_preformatted(root: Tag) -> bool:
    preformatted = False
    for ancestor in reversed([parent for parent in root.parents if isinstance(parent, Tag)]):
        preformatted = _node_preformatted(ancestor, preformatted)
    return preformatted


class _VisualLineBuffer:
    def __init__(self, *, max_lines: int | None = None) -> None:
        self.lines: deque[str] = deque(maxlen=max_lines)
        self.current_line: list[str] = []

    def flush(self) -> None:
        normalized = " ".join("".join(self.current_line).split())
        if normalized:
            self.lines.append(normalized)
        self.current_line.clear()

    def reset(self) -> None:
        self.current_line.clear()
        self.lines.clear()

    def append_text(self, raw: str, preformatted: bool) -> None:
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        parts = _SIGNATURE_SEPARATOR.split(raw)
        if len(parts) > 1:
            self.reset()
            raw = parts[-1]
        if preformatted:
            *head_lines, raw = raw.split("\n")
            for line in head_lines:
                self.current_line.append(line)
                self.flush()
        if raw:
            self.current_line.append(raw)


def _visual_lines(
    root: Tag,
    *,
    start_after: Tag | NavigableString | None = None,
    stop_at: Tag | NavigableString | None = None,
    stop_offset: int | None = None,
    reset_on_media: bool = True,
) -> list[str]:
    buffer = _VisualLineBuffer()
    started = start_after is None
    stopped = False
    stop_ancestors = (
        {id(ancestor) for ancestor in stop_at.parents} if stop_at is not None else set()
    )
    start_ancestors = (
        {id(ancestor) for ancestor in start_after.parents} if start_after is not None else set()
    )
    boundary_ancestors = stop_ancestors | start_ancestors

    def walk(node: Tag, preformatted: bool) -> None:
        nonlocal started, stopped
        preformatted = _node_preformatted(node, preformatted)

        for child in node.children:
            if stop_at is not None and child is stop_at:
                if (
                    stop_offset is not None
                    and stop_offset > 0
                    and started
                    and isinstance(child, NavigableString)
                ):
                    buffer.append_text(str(child)[:stop_offset], preformatted)
                stopped = True
                return
            if isinstance(child, Comment):
                continue
            if start_after is not None and child is start_after:
                started = True
                buffer.reset()
                continue
            if isinstance(child, NavigableString):
                if started:
                    buffer.append_text(str(child), preformatted)
            elif isinstance(child, Tag):
                if (
                    reset_on_media
                    and child.name in {"hr", "img", "figure"}
                    and not (child.name == "figure" and id(child) in boundary_ancestors)
                ):
                    if started:
                        buffer.reset()
                    continue
                if child.name == "br":
                    if started:
                        buffer.flush()
                    continue
                is_block = not _is_inline_element(child)
                if is_block and started:
                    buffer.flush()
                walk(child, preformatted)
                if stopped:
                    return
                if is_block and started:
                    buffer.flush()

    walk(root, _initial_preformatted(root))
    buffer.flush()
    return list(buffer.lines)


def _collect_preceding_bill_captions(
    scope: Tag, target_image_ids: set[int], target_ancestor_ids: set[int]
) -> dict[int, bool]:
    buffer = _VisualLineBuffer(max_lines=2)
    result: dict[int, bool] = {}

    def walk(node: Tag, preformatted: bool) -> None:
        preformatted = _node_preformatted(node, preformatted)
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                buffer.append_text(str(child), preformatted)
                continue
            if child.name == "img":
                if id(child) in target_image_ids:
                    buffer.flush()
                    result[id(child)] = _lines_end_with_bill_caption(list(buffer.lines))
                buffer.reset()
                continue
            if child.name in {"figure", "hr"}:
                if child.name == "figure" and id(child) in target_ancestor_ids:
                    buffer.flush()
                    walk(child, preformatted)
                buffer.reset()
                continue
            if child.name == "br":
                buffer.flush()
                continue
            is_block = not _is_inline_element(child)
            if is_block:
                buffer.flush()
            walk(child, preformatted)
            if is_block:
                buffer.flush()

    walk(scope, _initial_preformatted(scope))
    return result


def _preceded_by_bill_caption(
    image: Tag, scope: Tag, *, preceded_bill: dict[int, bool] | None = None
) -> bool:
    if preceded_bill is not None:
        return preceded_bill.get(id(image), False)
    return _lines_end_with_bill_caption(_visual_lines(scope, stop_at=image))


def _can_climb_signature_wrapper(parent: Tag, box: Tag) -> bool:
    descendants = list(parent.descendants)
    last_hr = max(
        (
            idx
            for idx, node in enumerate(descendants)
            if isinstance(node, Tag) and node.name == "hr"
        ),
        default=-1,
    )
    tail = descendants[last_hr + 1 :]
    if any(isinstance(node, Tag) and node.name in {"img", "figure"} for node in tail):
        return False
    tail_text_nodes = [
        node
        for node in tail
        if isinstance(node, NavigableString) and not isinstance(node, Comment) and node.strip()
    ]
    box_after_hr = any(node is box for node in tail)
    if not box_after_hr:
        if last_hr == -1 or not any(ancestor is box for ancestor in descendants[last_hr].parents):
            return False
        # With the separator inside the box, all post-separator text must belong to it.
        if any(not any(ancestor is box for ancestor in node.parents) for node in tail_text_nodes):
            return False

    last_hr_tag = descendants[last_hr] if last_hr != -1 else None
    tail_lines = _visual_lines(parent, start_after=last_hr_tag)
    tail_text = "\n".join(tail_lines)
    return not _lines_end_with_bill_caption(tail_lines) and (
        box_after_hr or _SIGNATURE_CONTACT.search(tail_text) is not None
    )


def _image_in_adjacent_signature_slot(
    box: Tag, image: Tag, footer: Tag, *, preceded_bill: dict[int, bool] | None = None
) -> bool:
    if box.find(["img", "figure"]) is not None:
        return False

    slot_b = box
    while (
        isinstance(slot_b.parent, Tag)
        and slot_b.parent is not footer
        and _can_climb_signature_wrapper(slot_b.parent, box)
    ):
        slot_b = slot_b.parent
    parent = slot_b.parent
    if not isinstance(parent, Tag) or (
        parent is not footer and not any(ancestor is footer for ancestor in parent.parents)
    ):
        return False
    img_slot = _direct_child_containing(parent, image)
    if (
        img_slot is None
        or img_slot is slot_b
        or img_slot.name == "figure"
        or img_slot.find("figure") is not None
        or _has_text(img_slot)
    ):
        return False

    siblings = _non_empty_children(parent)
    pos_b = next(idx for idx, sibling in enumerate(siblings) if sibling is slot_b)
    pos_img = next(idx for idx, sibling in enumerate(siblings) if sibling is img_slot)
    if pos_img == pos_b + 1:
        return not _preceded_by_bill_caption(image, footer, preceded_bill=preceded_bill)
    return (
        pos_img == pos_b - 1
        and parent is not footer
        and slot_b.find("hr") is None
        and pos_img == 0
        and parent.find("figure") is None
        and all(
            img is img_slot or any(ancestor is img_slot for ancestor in img.parents)
            for img in parent.find_all("img")
        )
        and not _preceded_by_bill_caption(image, footer, preceded_bill=preceded_bill)
    )


def _footer_signature_context(
    markers: list[_HtmlMarker],
    index: int,
    body_scope: Tag,
    footer_tag: Tag,
    image: Tag,
    *,
    require_signoff: bool = True,
    preceded_bill: dict[int, bool] | None = None,
) -> str:
    if (figure := image.find_parent("figure")) is not None:
        return "\n".join(
            node.strip()
            for node in figure.strings
            if node.strip() and node.find_parent("figure") is figure
        )

    sep_start = 0
    for idx in range(index - 1, -1, -1):
        marker = markers[idx]
        node, _ = marker
        if _ends_footer_context(marker, footer_tag, body_scope) or (
            isinstance(node, NavigableString) and _SIGNATURE_SEPARATOR.search(node)
        ):
            sep_start = idx + 1
            break

    separator = None
    for sep_end in range(index + 1, len(markers)):
        marker = markers[sep_end]
        if _ends_footer_context(marker, footer_tag, body_scope):
            break
        node, _ = marker
        if isinstance(node, NavigableString) and (separator := _SIGNATURE_SEPARATOR.search(node)):
            sep_end += 1
            break
    else:
        sep_end = len(markers)

    text_nodes: list[tuple[int, NavigableString, str]] = []
    images: list[tuple[int, Tag]] = []
    for idx in range(sep_start, sep_end):
        node, scope = markers[idx]
        if scope is not footer_tag or node.find_parent("figure") is not None:
            continue
        if isinstance(node, NavigableString):
            text = node[:separator.start()] if separator and idx == sep_end - 1 else node
            if text.strip():
                text_nodes.append((idx, node, text))
        elif node.name == "img":
            images.append((idx, node))

    image_parent_ids = {id(parent) for parent in image.parents}
    for box, box_texts in _minimal_signature_boxes(
        footer_tag, text_nodes, require_signoff=require_signoff
    ):
        if id(box) in image_parent_ids:
            if _preceded_by_bill_caption(image, footer_tag, preceded_bill=preceded_bill):
                return ""
            first_sig_marker, sig_offset = next(
                (
                    (idx, match.start())
                    for idx, text in box_texts
                    if (match := _SIGNOFF.search(text))
                ),
                (None, None),
            )
            if first_sig_marker is None and not require_signoff:
                first_sig_marker, sig_offset = next(
                    (
                        (idx, match.start())
                        for idx, text in box_texts
                        if (match := _SIGNATURE_CONTACT.search(text))
                    ),
                    (None, None),
                )
            if first_sig_marker is not None and index < first_sig_marker:
                has_img_after = any(
                    idx > first_sig_marker and any(parent is box for parent in img.parents)
                    for idx, img in images
                )
                between_lines = _visual_lines(
                    box,
                    start_after=image,
                    stop_at=markers[first_sig_marker][0],
                    stop_offset=sig_offset,
                )
                if has_img_after or _lines_start_with_bill_caption(between_lines):
                    return ""
        elif not _image_in_adjacent_signature_slot(
            box, image, footer_tag, preceded_bill=preceded_bill
        ):
            continue
        return "\n".join(text for _, text in box_texts)

    image_container = _nearest_container(image, footer_tag)
    return "\n".join(
        text
        for _, node, text in text_nodes
        if _nearest_container(node, footer_tag) is image_container
    )


def _html_text(html: str, email: ParsedEmail, *, base_url: str = "") -> str:
    soup = BeautifulSoup(html, "html.parser")
    effective_base = base_url
    if (base_tag := soup.find("base", href=True)) is not None:
        href = str(base_tag["href"]).strip()
        if href and (resolved := _normalize_location(href, base=base_url)):
            effective_base = resolved
    if effective_base:
        email.html_bases.add(effective_base)
    for tag in soup.find_all(["script", "style", "head"]):
        tag.decompose()
    body = soup.body or soup
    markers = [
        (node, _signature_scope(node, body))
        for node in soup.descendants
        if (isinstance(node, Tag) and node.name in {"img", "hr"})
        or (isinstance(node, NavigableString) and not isinstance(node, Comment) and node.strip())
    ]

    targets_by_scope: dict[int, tuple[Tag, set[int], set[int]]] = {}
    for node, scope in markers:
        if (
            scope is None
            or not isinstance(node, Tag)
            or node.name != "img"
            or not node.has_attr("src")
        ):
            continue
        _, image_ids, ancestor_ids = targets_by_scope.setdefault(id(scope), (scope, set(), set()))
        image_ids.add(id(node))
        for parent in node.parents:
            if parent is scope or id(parent) in ancestor_ids:
                break
            ancestor_ids.add(id(parent))

    preceded_bill: dict[int, bool] = {}
    for scope, image_ids, ancestor_ids in targets_by_scope.values():
        preceded_bill.update(_collect_preceding_bill_captions(scope, image_ids, ancestor_ids))

    for index, (image, scope) in enumerate(markers):
        if not isinstance(image, Tag) or image.name != "img":
            continue
        src = image.get("src")
        if src is None:
            continue
        source = str(src).strip()
        in_footer = scope is not None and (scope.name == "footer" or _inside_footer(image, body))
        if _is_bill_image_tag(image):
            classification = "body"
        elif scope is None:
            classification = "signature"
        elif preceded_bill.get(id(image), False):
            classification = "body"
        elif len(markers) - index <= _SIGNATURE_TAIL_SIZE:
            context, has_separator = _signature_context(markers, index, scope, body)
            if in_footer:
                context = _footer_signature_context(
                    markers,
                    index,
                    body,
                    scope,
                    image,
                    require_signoff=not has_separator,
                    preceded_bill=preceded_bill,
                )
            has_contact = _SIGNATURE_CONTACT.search(context) is not None
            has_signoff = _SIGNOFF.search(context) is not None
            after_signoff = has_separator and has_contact
            footer_signature = in_footer and has_contact and has_signoff
            decorative = (
                _DECORATIVE_IMAGE.search(source) is not None and (has_contact or has_signoff)
            )
            if after_signoff or footer_signature or decorative:
                classification = "signature"
            elif not in_footer and (
                not has_separator or not _in_post_separator_signature(markers, index, scope, body)
            ):
                classification = "body"
            else:
                classification = "undetermined"
        elif not in_footer and not _in_post_separator_signature(markers, index, scope, body):
            classification = "body"
        else:
            classification = "undetermined"

        if classification == "undetermined":
            continue
        is_signature = classification == "signature"
        if source.lower().startswith("cid:"):
            if cid := _content_id(source[4:]):
                cids = email.signature_cids if is_signature else email.non_signature_cids
                cids.add(cid)
            continue

        location, filename = _classify_non_cid_source(source, base=effective_base)
        if location:
            locations = email.signature_locations if is_signature else email.non_signature_locations
            locations.add(location)
        if filename:
            filenames = email.signature_filenames if is_signature else email.non_signature_filenames
            filenames.add(filename)
    return soup.get_text("\n", strip=True)


def _read_body(part: _MimePart, email: ParsedEmail, *, base_url: str) -> EmailBody:
    data = part.read_bytes()
    try:
        text = data.decode(part.headers.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    if part.media_type == "text/html":
        text = _html_text(text, email, base_url=base_url)
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

    def collect(
        part: _MimePart, *, related_resource: bool = False, inherited_base: str = ""
    ) -> list[EmailBody]:
        nonlocal slots, body_reserved
        if related_resource or _is_attachment(part.headers):
            if slots == 0:
                email.truncated = True
            else:
                # Keep the wire range only. The dispatcher checks the shared budget again
                # before loading the payload, after any earlier nested mail has expanded.
                email.attachments.append(replace(part, base_url=inherited_base))
                slots -= 1
            return []

        candidate = (
            part.headers.get("Content-Base", "").strip()
            or part.headers.get("Content-Location", "").strip()
        )
        part_base = _normalize_location(candidate, base=inherited_base) or inherited_base
        media_type = part.media_type
        if media_type in _TEXT_TYPES:
            body = _read_body(part, email, base_url=part_base)
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
            bodies = collect(
                child,
                related_resource=related_root not in {None, child.start},
                inherited_base=part_base,
            )
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
    email.signature_cids.difference_update(email.non_signature_cids)
    email.signature_filenames.difference_update(email.non_signature_filenames)
    email.signature_locations.difference_update(email.non_signature_locations)
    return email
