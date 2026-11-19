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
from shipping_billing_agent.gateways.text_sanitizer import sanitize_for_llm

SAMPLES = Path(__file__).resolve().parents[2] / "笔试题AIAGENT"


@pytest.fixture(scope="module")
def dispatcher():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    if not SAMPLES.is_dir():
        pytest.skip("当前检出未提供笔试题AIAGENT原题目录")
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

    expected_text = {
        "MBL": ("SUN TRACK EXPRESS", "HZMHK331281"),
        "HBL": ("TARGETING A INC", "FREIGHT PREPAID", "OOLU4055331281"),
    }
    for suffix, phrases in expected_text.items():
        pdf = by_name[f"4055331281_{suffix}.pdf"]
        assert pdf.original_filename == f"4055331281\xa0{suffix}.pdf"
        assert pdf.source_kind is SourceKind.PDF
        assert pdf.status is DocumentStatus.PARSED, pdf.error_message
        for phrase in phrases:
            assert phrase in pdf.raw_text, f"{suffix} 原文缺少 {phrase}"

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


def test_original_pdf_invoice_one_layout_masks_all_remittance_numbers(dispatcher):
    doc = _parse_sample(dispatcher, "PDF示例账单1.pdf")
    fake_account = "000123456"
    sample_text, replacements = re.subn(
        r"(\b(?:ABA #|ABA/ROUTING WIRE#:|ACH:|Account Number|ACCT #:)[ \t]+)\d+",
        rf"\g<1>{fake_account}",
        doc.raw_text,
    )
    assert replacements == 6

    sanitized = sanitize_for_llm(sample_text)

    assert sanitized.count("[REDACTED_BANK_ACCT]") >= 6
    assert fake_account not in sanitized
    assert "245.0000" in sanitized
    assert "CMDUCHN4053601" in sanitized


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
