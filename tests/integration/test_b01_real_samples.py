import re
from email import message_from_bytes, policy
from hashlib import sha256
from pathlib import Path

import pytest

from shipping_billing_agent.domain.models import (
    DocumentStatus,
    ExtractionChannel,
    OcrMode,
    SourceKind,
)
from shipping_billing_agent.gateways.local_ocr import RapidOcrFallback

SAMPLES = Path(__file__).resolve().parents[2] / "笔试题AIAGENT"


@pytest.fixture(scope="module")
def dispatcher():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    return DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK,
        local_ocr=RapidOcrFallback(),
    )


def test_original_eml_deduplicates_body_and_keeps_all_attachment_bytes(dispatcher):
    (sample,) = SAMPLES.glob("*.eml")
    data = sample.read_bytes()
    root, body, *attachments = dispatcher.parse_batch([(sample.name, data)])
    by_name = {doc.sanitized_filename: doc for doc in attachments}

    assert root.source_kind is SourceKind.EML
    assert root.status is DocumentStatus.PARSED, root.error_message
    assert root.content_sha256 == sha256(data).hexdigest()
    assert root.raw_text == ""
    assert body.source_kind is SourceKind.EML_BODY
    assert body.raw_text.count("SHA pay TAR") == 1
    assert body.parent_doc_id == root.doc_id
    assert len(attachments) == 3
    assert set(by_name) == {"InsertPic_C25B.jpg", "4055331281_MBL.pdf", "4055331281_HBL.pdf"}

    message = message_from_bytes(data, policy=policy.default)
    payloads = {
        part.get_filename(): part.get_payload(decode=True)
        for part in message.walk()
        if part.get_filename()
    }
    for attachment in attachments:
        original_payload = payloads[attachment.original_filename]
        assert attachment.parent_doc_id == root.doc_id
        assert attachment.raw_bytes == original_payload
        assert attachment.content_sha256 == sha256(original_payload).hexdigest()

    for suffix in ("MBL", "HBL"):
        pdf = by_name[f"4055331281_{suffix}.pdf"]
        assert pdf.original_filename == f"4055331281\xa0{suffix}.pdf"
        assert pdf.source_kind is SourceKind.PDF
        assert pdf.status is DocumentStatus.PARSED, pdf.error_message

    signature = by_name["InsertPic_C25B.jpg"]
    assert signature.status is DocumentStatus.SKIPPED_NOISE
    assert signature.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE
    forced = dispatcher.force_parse_skipped_document(
        signature.original_filename,
        signature.raw_bytes,
        existing_doc_id=signature.doc_id,
        parent_doc_id=signature.parent_doc_id,
    )
    assert forced.doc_id == signature.doc_id
    assert forced.parent_doc_id == root.doc_id
    assert forced.content_sha256 == signature.content_sha256
    assert forced.status is DocumentStatus.PARSED, forced.error_message
    assert forced.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK


def _parse_sample(dispatcher, filename):
    (document,) = dispatcher.parse_batch([(filename, (SAMPLES / filename).read_bytes())])
    assert document.status is DocumentStatus.PARSED, document.error_message
    return document


def test_original_pdf_invoice_two_preserves_ordered_line_amounts_and_total(dispatcher):
    doc = _parse_sample(dispatcher, "PDF 示例账单2.pdf")

    assert doc.extraction_channel is ExtractionChannel.LOCAL_PDF_TEXT_FALLBACK
    assert "DC-108478" in doc.raw_text
    assert re.search(r"65\.00.*1,310\.40.*35\.00.*1,410\.40", doc.raw_text, re.DOTALL)


def test_original_pdf_invoice_one_keeps_all_three_text_pages(dispatcher):
    doc = _parse_sample(dispatcher, "PDF示例账单1.pdf")

    assert doc.extraction_channel is ExtractionChannel.LOCAL_PDF_TEXT_FALLBACK
    assert re.findall(r"(?m)^\[Page (\d+) \| Text\]$", doc.raw_text) == ["1", "2", "3"]
    assert "CMDUCHN4053601" in doc.raw_text
    assert "245.0000" in doc.raw_text


def test_original_image_passes_its_amounts_through_real_rapidocr(dispatcher):
    doc = _parse_sample(dispatcher, "图片示例.png")

    assert doc.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert "ORFR1618731" in doc.raw_text
    assert set(re.findall(r"(?<![\d.])\d+\.\d{2}(?![\d.])", doc.raw_text)) >= {
        "5.00",
        "2.85",
        "21.90",
        "26.90",
    }
