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

_DEFAULT_LIMITS = ResourceLimits()


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


def _require_url_validator(validator: UrlValidatorProtocol | None) -> UrlValidatorProtocol:
    if validator is None:
        raise ValueError("必须注入 url_validator")
    return validator


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
    """Check uploads and use the injected validator for any public URLs."""
    if public_urls:
        url_validator = _require_url_validator(url_validator)
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
    if url_validator is not None:
        for url in public_urls:
            try:
                url_validator(url)
            except ValueError as exc:
                issues.append(_url_issue(url, exc))
    return issues


def compute_retry_context(
    existing_documents: Sequence[ParsedDocument],
    target_doc_id: str,
    limits: ResourceLimits = _DEFAULT_LIMITS,
) -> tuple[int, int]:
    """Return the target's depth and child slots after releasing its old descendants."""
    parents = {doc.doc_id: doc.parent_doc_id for doc in existing_documents}
    if target_doc_id not in parents:
        raise ValueError("目标文档不在当前批次中")
    if len(parents) != len(existing_documents):
        raise ValueError("当前批次包含重复的文档 ID")

    parent_chain = {target_doc_id}
    parent_id = parents[target_doc_id]
    while parent_id is not None:
        if parent_id in parent_chain or parent_id not in parents:
            raise ValueError("目标文档父链无效")
        parent_chain.add(parent_id)
        parent_id = parents[parent_id]

    children_by_parent: dict[str, list[str]] = {}
    for doc_id, parent_id in parents.items():
        if parent_id is not None:
            children_by_parent.setdefault(parent_id, []).append(doc_id)
    descendants = list(children_by_parent.get(target_doc_id, ()))
    kept_count = len(parents)
    while descendants:
        descendant_id = descendants.pop()
        kept_count -= 1
        descendants.extend(children_by_parent.get(descendant_id, ()))
    return len(parent_chain), max(0, limits.max_expanded_documents - kept_count)


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
    """Route local files and public URLs through their injected OCR and URL validators."""

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
        relay_presign_ttl_seconds: int = 300,
    ) -> None:
        if relay_publisher is not None:
            _require_url_validator(url_validator)
        if not 1 <= relay_presign_ttl_seconds <= 300:
            raise ValueError("relay_presign_ttl_seconds 必须在 1 到 300 秒之间")
        self._ocr_mode = ocr_mode
        self._limits = limits
        self._remote_ocr = remote_ocr
        self._local_ocr = local_ocr
        self._url_validator = url_validator
        self._relay_publisher = relay_publisher if public_relay_base_url.strip() else None
        self._relay_presign_ttl_seconds = relay_presign_ttl_seconds

    def parse_batch(
        self, files: Sequence[tuple[str, bytes]], public_urls: Sequence[str] = ()
    ) -> list[ParsedDocument]:
        """Parse validated top-level inputs in order, with email descendants after each parent."""
        if public_urls:
            _require_url_validator(self._url_validator)
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
        depth: int | None = None,
        remaining_expanded_slots: int | None = None,
    ) -> list[ParsedDocument]:
        """Reparse in place, requiring the original depth for children and child slots for mail."""
        if parent_doc_id is not None and (depth is None or depth < 2):
            raise ValueError("重试嵌套子文档时必须显式传入原父链 depth（>= 2）")
        if depth is None:
            depth = 1
        if depth < 1:
            raise ValueError("depth 必须从 1 开始")
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
        document = _new_file(
            filename,
            data,
            existing_doc_id=existing_doc_id,
            parent_doc_id=parent_doc_id,
            media_type=media_type,
        )
        if remaining_expanded_slots is None:
            if document.source_kind is SourceKind.EML:
                raise ValueError(
                    "重试可能展开子件的邮件文档时必须显式传入 remaining_expanded_slots"
                )
            child_slots = 0
        else:
            if remaining_expanded_slots < 0:
                raise ValueError("remaining_expanded_slots 不能为负数")
            max_child_slots = max(0, self._limits.max_expanded_documents - 1)
            child_slots = min(max_child_slots, remaining_expanded_slots)
        batch = _Batch(child_slots)
        self._parse_file(document, data, batch, depth=depth)
        return batch.documents

    def force_parse_skipped_document(
        self,
        filename: str,
        data: bytes,
        *,
        existing_doc_id: str,
        parent_doc_id: str | None = None,
        media_type: str | None = None,
        depth: int | None = None,
    ) -> ParsedDocument:
        """Reparse a skipped image from its retained bytes outside MIME signature detection."""
        return self.retry_single_document(
            filename,
            data,
            existing_doc_id=existing_doc_id,
            parent_doc_id=parent_doc_id,
            media_type=media_type,
            depth=depth,
        )[0]

    def _parse_file(self, doc: ParsedDocument, data: bytes, batch: _Batch, *, depth: int) -> None:
        batch.documents.append(doc)
        issue = _file_issue(doc.original_filename, data, doc.source_kind, self._limits)
        if issue:
            doc.error_message = issue.message
            return
        try:
            if doc.source_kind is SourceKind.EML:
                if depth > self._limits.max_eml_recursion_depth:
                    raise _DocumentError(
                        f"邮件嵌套深度超过 {self._limits.max_eml_recursion_depth} 层"
                    )
                email = parse_eml(data, max_children=batch.child_slots)
                doc.metadata = email.headers.copy()
                self._expand_eml(doc, email, batch, depth)
                _parsed(doc, ExtractionChannel.EML_TEXT_PARSER)
            elif doc.source_kind in {SourceKind.EXCEL, SourceKind.WORD}:
                self._parse_office(doc, data)
            else:
                self._parse_pdf_or_image(doc, data)
        except (EmlParseError, WordParseError, PdfParseError, _DocumentError) as exc:
            doc.error_message = str(exc)
        except Exception as exc:
            doc.error_message = f"无法解析文件（{type(exc).__name__}）"

    def _parse_office(self, doc: ParsedDocument, data: bytes) -> None:
        if doc.source_kind is SourceKind.EXCEL:
            doc.raw_text, doc.tables = parse_excel(data, doc.sanitized_filename)
            channel = ExtractionChannel.EXCEL_PARSER
        else:
            doc.raw_text, doc.tables = parse_word(
                data, doc.sanitized_filename, max_file_bytes=self._limits.max_file_bytes
            )
            channel = ExtractionChannel.WORD_PARSER
        if not doc.raw_text.strip() and not any(
            cell.strip() for table in doc.tables for row in table.rows for cell in row
        ):
            raise _DocumentError("文档内容为空，未提取到任何文本或表格")
        _parsed(doc, channel)

    def _expansion_limit_error(self) -> _DocumentError:
        return _DocumentError(
            f"单批展开文档数超过 {self._limits.max_expanded_documents} 项上限，部分子件已截断"
        )

    def _take_child_slot(self, batch: _Batch) -> None:
        if batch.child_slots <= 0:
            raise self._expansion_limit_error()
        batch.child_slots -= 1

    def _expand_eml(
        self, parent: ParsedDocument, email: ParsedEmail, batch: _Batch, depth: int
    ) -> None:
        if email.body is not None:
            self._take_child_slot(batch)
            name = f"{PurePath(parent.sanitized_filename).stem}.body.txt"
            body = _new_file(
                name,
                email.body.data,
                parent_doc_id=parent.doc_id,
                media_type=email.body.media_type,
                body=True,
            )
            body.metadata = email.headers.copy()
            body.raw_text = email.body.text
            _parsed(body, ExtractionChannel.EML_TEXT_PARSER)
            batch.documents.append(body)

        for index, attachment in enumerate(email.attachments, 1):
            self._take_child_slot(batch)
            name = f"attachment-{index}.bin"
            media_type = None
            decode_error = None
            try:
                name = attachment.filename(index)
                media_type = attachment.media_type
                data = attachment.read_bytes()
            except Exception as exc:
                data = attachment.wire_bytes()
                decode_error = f"邮件附件编码无效（{type(exc).__name__}）"
            child = _new_file(name, data, parent_doc_id=parent.doc_id, media_type=media_type)
            if decode_error:
                child.error_message = decode_error
            elif child.source_kind is SourceKind.IMAGE and email.is_signature_image(
                attachment, name
            ):
                child.status = DocumentStatus.SKIPPED_NOISE
                child.extraction_channel = ExtractionChannel.SKIPPED_SIGNATURE
            else:
                self._parse_file(child, data, batch, depth=depth + 1)
                continue
            batch.documents.append(child)
        if email.truncated:
            raise self._expansion_limit_error()

    def _parse_url(
        self,
        url: str,
        *,
        existing_doc_id: str | None = None,
        parent_doc_id: str | None = None,
        filename: str | None = None,
    ) -> ParsedDocument:
        url_validator = _require_url_validator(self._url_validator)
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
            extraction_channel=ExtractionChannel.FAILED,
            status=DocumentStatus.FAILED,
        )
        try:
            clean_url = url_validator(url)
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
            url_validator = _require_url_validator(self._url_validator)
            try:
                signed_url = self._relay_publisher.publish(
                    data, doc.sanitized_filename, ttl_seconds=self._relay_presign_ttl_seconds
                )
                self._recognize_url(doc, url_validator(signed_url))
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
