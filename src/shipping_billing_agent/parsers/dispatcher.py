"""Validate uploads and route each document through an injected OCR or file parser."""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from mimetypes import guess_type
from pathlib import PurePath
from typing import Protocol
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from shipping_billing_agent.config.limits import ResourceLimits
from shipping_billing_agent.domain.models import (
    DocumentStatus,
    ExtractionChannel,
    InputValidationIssue,
    OcrMode,
    OcrStructuredPayload,
    ParsedDocument,
    SourceKind,
)

from .eml_parser import EmlParseError, ParsedEmail, parse_eml
from .excel_parser import parse_excel
from .filename_sanitizer import detect_source_kind, sanitize_filename
from .pdf_parser import PdfPageOcr, PdfParseError, parse_pdf
from .word_parser import WordParseError, parse_word

_RELAY_TTL_SECONDS = 300
_DEFAULT_LIMITS = ResourceLimits()
_URL_AUTHORITY = re.compile(r"(?:\[[^\[\]]+\]|[^:\[\]]+)(?::[0-9]+)?")


class _RemoteOcrResult(Protocol):
    @property
    def payload(self) -> OcrStructuredPayload: ...

    @property
    def formatted_text(self) -> str: ...


class RemoteOcrProtocol(Protocol):
    def recognize_url(self, url: str) -> _RemoteOcrResult: ...


class LocalOcrProtocol(PdfPageOcr, Protocol):
    def recognize_image_bytes(self, content: bytes, filename: str) -> tuple[str, float]: ...


class UrlValidatorProtocol(Protocol):
    def __call__(self, url: str, /) -> str: ...


class RelayPublisherProtocol(Protocol):
    def publish(self, content: bytes, filename: str, *, ttl_seconds: int) -> str: ...


class _DocumentError(ValueError):
    pass


def _check_url(url: str, validator: UrlValidatorProtocol | None) -> str:
    clean_url = url.strip()
    try:
        parts = urlsplit(clean_url)
        port = parts.port
    except ValueError as exc:
        raise ValueError("文件 URL 格式无效") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or "%" in parts.hostname
        or parts.username is not None
        or port == 0
        or not _URL_AUTHORITY.fullmatch(parts.netloc)
        or "\\" in clean_url
        or any(c.isspace() or ord(c) < 32 or c == "\x7f" for c in clean_url)
    ):
        raise ValueError("文件 URL 必须为有效且不含登录信息的 HTTP 或 HTTPS 地址")
    if validator is None:
        from shipping_billing_agent.gateways.url_guard import validate_public_url

        validator = validate_public_url
    return validator(clean_url)


def _url_issue(url: str, error: ValueError) -> InputValidationIssue:
    reference, query, _ = url.split("#", 1)[0].partition("?")
    reference = re.sub(r"(?<=//)[^/]*@", "[REDACTED]@", reference)
    if query:
        reference += "?[REDACTED]"
    blocked = getattr(error, "code", None) == "SSRF_BLOCKED"
    return InputValidationIssue(
        code="SSRF_BLOCKED" if blocked else "INVALID_URL",
        filename_or_url=reference,
        message="文件 URL 未通过公网安全校验" if blocked else "文件 URL 格式无效",
    )


def _file_issue(
    filename: str,
    data: bytes,
    kind: SourceKind | None,
    limits: ResourceLimits,
) -> InputValidationIssue | None:
    if not data:
        code, message = "EMPTY_FILE", "文件为空"
    elif len(data) > limits.max_file_bytes:
        code, message = "FILE_TOO_LARGE", f"文件超过 {limits.max_file_bytes} 字节"
    elif kind is None or kind is SourceKind.EML_ATTACHMENT:
        code, message = "UNSUPPORTED_EXTENSION", "文件扩展名不受支持"
    else:
        return None
    return InputValidationIssue(code=code, filename_or_url=filename, message=message)


def validate_batch_inputs(
    files: Sequence[tuple[str, bytes]],
    public_urls: Sequence[str],
    limits: ResourceLimits = _DEFAULT_LIMITS,
    url_validator: UrlValidatorProtocol | None = None,
) -> list[InputValidationIssue]:
    """Check (original filename, bytes) pairs and URLs before creating the batch.

    A None validator uses the project's public URL guard, including its DNS checks.
    """
    total = len(files) + len(public_urls)
    if total == 0:
        return [
            InputValidationIssue(code="EMPTY_INPUT", filename_or_url="", message="请提供文件或 URL")
        ]
    if total > limits.max_batch_files:
        return [
            InputValidationIssue(
                code="TOO_MANY_FILES",
                filename_or_url="",
                message=f"单批最多 {limits.max_batch_files} 项输入",
            )
        ]
    issues: list[InputValidationIssue] = []
    for name, data in files:
        issue = _file_issue(name, data, detect_source_kind(sanitize_filename(name)), limits)
        if issue:
            issues.append(issue)
    for url in public_urls:
        try:
            _check_url(url, url_validator)
        except ValueError as exc:
            issues.append(_url_issue(url, exc))
    return issues


@dataclass(slots=True)
class _Batch:
    child_slots: int
    documents: list[ParsedDocument] = field(default_factory=list)


def _new_file(
    filename: str,
    data: bytes,
    *,
    existing_doc_id: str | None = None,
    parent_doc_id: str | None = None,
    media_type: str | None = None,
    body: bool = False,
) -> ParsedDocument:
    clean_name = sanitize_filename(filename)
    if body:
        kind = SourceKind.EML_BODY
    elif media_type == "message/rfc822":
        kind = SourceKind.EML
    else:
        kind = detect_source_kind(clean_name) or SourceKind.EML_ATTACHMENT
    return ParsedDocument(
        doc_id=existing_doc_id if existing_doc_id is not None else str(uuid4()),
        parent_doc_id=parent_doc_id,
        original_filename=filename,
        sanitized_filename=clean_name,
        media_type=media_type or guess_type(clean_name)[0],
        source_kind=kind,
        content_sha256=sha256(data).hexdigest(),
        extraction_channel=ExtractionChannel.FAILED,
        status=DocumentStatus.FAILED,
        raw_bytes=data,
    )


def _parsed(document: ParsedDocument, channel: ExtractionChannel) -> None:
    document.extraction_channel = channel
    document.status = DocumentStatus.PARSED


class DocumentParserDispatcher:
    """Route bytes without constructing or managing OCR clients.

    Missing remote OCR or relay disables the remote path for local documents. With
    local fallback permitted, a missing local OCR client allows PDF text only.
    A missing URL validator selects the same lazy default as validate_batch_inputs.
    """

    def __init__(
        self,
        *,
        ocr_mode: OcrMode = OcrMode.REMOTE_ONLY,
        limits: ResourceLimits = _DEFAULT_LIMITS,
        remote_ocr: RemoteOcrProtocol | None = None,
        local_ocr: LocalOcrProtocol | None = None,
        url_validator: UrlValidatorProtocol | None = None,
        relay_publisher: RelayPublisherProtocol | None = None,
        public_relay_base_url: str = "",
    ) -> None:
        self._ocr_mode = ocr_mode
        self._limits = limits
        self._remote_ocr = remote_ocr
        self._local_ocr = local_ocr
        self._url_validator = url_validator
        self._relay_publisher = relay_publisher if public_relay_base_url.strip() else None

    def parse_batch(
        self, files: Sequence[tuple[str, bytes]], public_urls: Sequence[str] = ()
    ) -> list[ParsedDocument]:
        """Parse validated top-level inputs in order, with email descendants after each parent."""
        top_level = len(files) + len(public_urls)
        if top_level > min(self._limits.max_batch_files, self._limits.max_expanded_documents):
            raise ValueError("顶层输入数量超过单批上限")
        batch = _Batch(self._limits.max_expanded_documents - top_level)
        for name, data in files:
            self._parse_file(_new_file(name, data), data, batch, depth=1)
        batch.documents.extend(self._parse_url(url) for url in public_urls)
        return batch.documents

    def retry_single_document(
        self,
        filename: str,
        data: bytes | None = None,
        *,
        existing_doc_id: str,
        parent_doc_id: str | None = None,
        media_type: str | None = None,
        public_url: str | None = None,
    ) -> list[ParsedDocument]:
        """Reparse one source. Retried emails can return a new subtree under the same root ID."""
        if public_url is not None:
            return [
                self._parse_url(
                    public_url,
                    existing_doc_id=existing_doc_id,
                    parent_doc_id=parent_doc_id,
                    filename=filename,
                )
            ]
        if data is None:
            raise ValueError("重试本地文件需要原始字节")
        batch = _Batch(max(0, self._limits.max_expanded_documents - 1))
        document = _new_file(
            filename,
            data,
            existing_doc_id=existing_doc_id,
            parent_doc_id=parent_doc_id,
            media_type=media_type,
        )
        self._parse_file(document, data, batch, depth=1)
        return batch.documents

    def force_parse_skipped_document(
        self,
        filename: str,
        data: bytes,
        *,
        existing_doc_id: str,
        parent_doc_id: str | None = None,
        media_type: str | None = None,
    ) -> ParsedDocument:
        """Reparse a skipped image from its retained bytes outside MIME signature detection."""
        return self.retry_single_document(
            filename,
            data,
            existing_doc_id=existing_doc_id,
            parent_doc_id=parent_doc_id,
            media_type=media_type,
        )[0]

    def _parse_file(self, doc: ParsedDocument, data: bytes, batch: _Batch, *, depth: int) -> None:
        batch.documents.append(doc)
        issue = _file_issue(doc.original_filename, data, doc.source_kind, self._limits)
        if issue:
            doc.error_message = issue.message
            return
        try:
            if doc.source_kind is SourceKind.EML:
                email = parse_eml(data)
                doc.metadata = email.headers.copy()
                self._expand_eml(doc, email, batch, depth)
                _parsed(doc, ExtractionChannel.EML_TEXT_PARSER)
            elif doc.source_kind is SourceKind.EXCEL:
                doc.raw_text, doc.tables = parse_excel(data, doc.sanitized_filename)
                _parsed(doc, ExtractionChannel.EXCEL_PARSER)
            elif doc.source_kind is SourceKind.WORD:
                doc.raw_text, doc.tables = parse_word(
                    data, doc.sanitized_filename, max_file_bytes=self._limits.max_file_bytes
                )
                _parsed(doc, ExtractionChannel.WORD_PARSER)
            else:
                self._parse_pdf_or_image(doc, data)
        except (EmlParseError, WordParseError, PdfParseError, _DocumentError) as exc:
            doc.error_message = str(exc)
        except Exception as exc:
            doc.error_message = f"无法解析文件（{type(exc).__name__}）"

    def _take_child_slot(self, batch: _Batch, child: ParsedDocument) -> bool:
        batch.child_slots -= 1
        if batch.child_slots == 0:
            # Top-level slots are reserved separately. The final expansion slot records truncation.
            limit = self._limits.max_expanded_documents
            child.error_message = f"单批展开文档数达到 {limit} 项上限"
            batch.documents.append(child)
            return False
        return True

    def _expand_eml(
        self, parent: ParsedDocument, email: ParsedEmail, batch: _Batch, depth: int
    ) -> None:
        if batch.child_slots == 0:
            return
        if email.body is not None:
            name = f"{PurePath(parent.sanitized_filename).stem}.body.txt"
            body = _new_file(
                name,
                email.body.data,
                parent_doc_id=parent.doc_id,
                media_type=email.body.media_type,
                body=True,
            )
            if not self._take_child_slot(batch, body):
                return
            body.metadata = email.headers.copy()
            body.raw_text = email.body.text
            _parsed(body, ExtractionChannel.EML_TEXT_PARSER)
            batch.documents.append(body)

        for index, attachment in enumerate(email.attachments(), 1):
            if batch.child_slots == 0:
                return
            name = f"attachment-{index}.bin"
            media_type = None
            decode_error = None
            try:
                name = attachment.filename(index)
                media_type = attachment.media_type
                data = attachment.read_bytes()
            except Exception as exc:
                data = attachment.source
                if data is None:
                    data = attachment.message.as_bytes()
                decode_error = f"邮件附件编码无效（{type(exc).__name__}）"
            child = _new_file(name, data, parent_doc_id=parent.doc_id, media_type=media_type)
            if not self._take_child_slot(batch, child):
                return
            if decode_error:
                child.error_message = decode_error
            elif (
                child.source_kind is SourceKind.EML
                and depth >= self._limits.max_eml_recursion_depth
            ):
                child.error_message = f"邮件嵌套深度超过 {self._limits.max_eml_recursion_depth} 层"
            elif child.source_kind is SourceKind.IMAGE and email.is_signature_image(
                attachment, child.sanitized_filename
            ):
                child.status = DocumentStatus.SKIPPED_NOISE
                child.extraction_channel = ExtractionChannel.SKIPPED_SIGNATURE
            else:
                self._parse_file(child, data, batch, depth=depth + 1)
                continue
            batch.documents.append(child)

    def _parse_url(
        self,
        url: str,
        *,
        existing_doc_id: str | None = None,
        parent_doc_id: str | None = None,
        filename: str | None = None,
    ) -> ParsedDocument:
        if filename is None:
            try:
                filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "download"
            except ValueError:
                filename = "download"
        doc = ParsedDocument(
            doc_id=existing_doc_id if existing_doc_id is not None else str(uuid4()),
            parent_doc_id=parent_doc_id,
            original_filename=filename,
            sanitized_filename=sanitize_filename(filename),
            source_kind=SourceKind.URL,
            public_url=url,
            extraction_channel=ExtractionChannel.FAILED,
            status=DocumentStatus.FAILED,
        )
        try:
            clean_url = _check_url(url, self._url_validator)
        except ValueError as exc:
            doc.error_message = _url_issue(url, exc).message
            return doc
        doc.public_url = clean_url
        try:
            self._recognize_url(doc, clean_url)
        except _DocumentError as exc:
            doc.error_message = str(exc)
        except Exception as exc:
            doc.error_message = f"远端 OCR 失败（{type(exc).__name__}）"
        return doc

    def _recognize_url(self, doc: ParsedDocument, url: str) -> None:
        if self._remote_ocr is None:
            raise _DocumentError("未配置远端 OCR 客户端")
        result = self._remote_ocr.recognize_url(url)
        payload = result.payload
        if not (payload.has_charges or payload.has_metadata):
            raise _DocumentError("远端 OCR 未返回费用或单据元数据")
        text = result.formatted_text
        doc.ocr_payload = payload
        doc.raw_text = text
        doc.extraction_channel = ExtractionChannel.PLTPLT_OCR
        doc.status = DocumentStatus.PARSED if payload.has_charges else DocumentStatus.METADATA_ONLY

    def _parse_pdf_or_image(self, doc: ParsedDocument, data: bytes) -> None:
        if self._relay_publisher is not None and self._remote_ocr is not None:
            try:
                signed_url = self._relay_publisher.publish(
                    data, doc.sanitized_filename, ttl_seconds=_RELAY_TTL_SECONDS
                )
                self._recognize_url(doc, _check_url(signed_url, self._url_validator))
                return
            except Exception as exc:
                if self._ocr_mode is not OcrMode.REMOTE_WITH_LOCAL_FALLBACK:
                    raise _DocumentError("REMOTE_ONLY 模式下远端 OCR 或文件中转失败") from exc
                doc.metadata["remote_ocr_error"] = type(exc).__name__
        elif self._ocr_mode is not OcrMode.REMOTE_WITH_LOCAL_FALLBACK:
            raise _DocumentError("REMOTE_ONLY 模式需要远端 OCR 客户端及受控文件中转")

        if doc.source_kind is SourceKind.PDF:
            doc.raw_text, doc.tables, channel = parse_pdf(data, local_ocr=self._local_ocr)
            _parsed(doc, channel)
        elif self._local_ocr is not None:
            doc.raw_text, confidence = self._local_ocr.recognize_image_bytes(
                data, doc.sanitized_filename
            )
            doc.metadata["ocr_confidence"] = confidence
            _parsed(doc, ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK)
        else:
            raise _DocumentError("未配置本地图像 OCR")
