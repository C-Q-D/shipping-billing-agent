from base64 import b64encode
from email import policy
from email.message import EmailMessage
from hashlib import sha256
from pathlib import Path
from struct import pack_into, unpack_from
from types import SimpleNamespace

import pytest
from PIL import Image

from shipping_billing_agent.config.limits import ResourceLimits
from shipping_billing_agent.domain.models import (
    DocumentStatus,
    ExtractionChannel,
    OcrListItem,
    OcrMode,
    OcrStructuredPayload,
    SourceKind,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class RecordingLocalOcr:
    def __init__(self):
        self.pages = []
        self.images = []

    def recognize_pil_image(self, image: Image.Image, page: int = 1) -> tuple[str, float]:
        self.pages.append((page, image.size))
        return "FREIGHT 187.65", 0.98

    def recognize_image_bytes(self, content: bytes, filename: str) -> tuple[str, float]:
        self.images.append((content, filename))
        return "Image 246.00", 0.99


class RecordingRemoteOcr:
    def __init__(self):
        self.urls = []

    def recognize_url(self, url: str):
        self.urls.append(url)
        return SimpleNamespace(
            formatted_text="BL001",
            payload=OcrStructuredPayload(
                items=[OcrListItem(mbl_number="BL001")],
                has_charges=False,
                has_metadata=True,
            ),
        )


class RecordingRelay:
    def __init__(self):
        self.files = []

    def publish(self, content: bytes, filename: str, *, ttl_seconds: int) -> str:
        self.files.append((content, filename, ttl_seconds))
        return "https://8.8.8.8/files/bill.pdf?signature=fixture"


def _parse_fixture(filename):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    data = (FIXTURES / filename).read_bytes()
    (document,) = DocumentParserDispatcher().parse_batch([(filename, data)])
    assert document.status is DocumentStatus.PARSED, document.error_message
    return document


def _mail(body: str) -> EmailMessage:
    message = EmailMessage(policy=policy.SMTP)
    message["Subject"] = "Fixture shipment"
    message["From"] = "shipper@example.test"
    message["To"] = "billing@example.test"
    message.set_content(body)
    return message


def _word_stream_start(data: bytes | bytearray) -> int:
    entry = data.index("WordDocument\0".encode("utf-16-le"))
    (sector,) = unpack_from("<I", data, entry + 116)
    return 512 * (sector + 1)


def test_sanitize_filename_removes_paths_controls_and_bounds_length():
    from shipping_billing_agent.parsers.filename_sanitizer import (
        detect_source_kind,
        sanitize_filename,
    )

    clean_name = sanitize_filename("C:\\drop/../..\\4055331281\xa0 \tMBL\x00\x1b.pdf")
    assert clean_name == "4055331281_MBL.pdf"
    long_name = sanitize_filename("航" * 300 + ".XLSX")
    assert len(long_name.encode("utf-8")) <= 255
    assert long_name.endswith(".XLSX")
    assert detect_source_kind(long_name) is SourceKind.EXCEL
    assert detect_source_kind("file.exe") is None
    assert detect_source_kind("https://example.test/download", is_url=True) is SourceKind.URL


@pytest.mark.parametrize("filename", ["b01_merged.xlsx", "b01_merged.xls"])
def test_excel_retains_merged_charge_name_on_both_distinct_amount_rows(filename):
    document = _parse_fixture(filename)

    assert document.extraction_channel is ExtractionChannel.EXCEL_PARSER
    (table,) = document.tables
    assert table.source == "[Sheet: Sea Costs | Row 3]"
    assert table.rows == [["Fee", "Amount"], ["THC", "65.25"], ["THC", "310.42"]]
    assert "[Sheet: Sea Costs | Row 1] Ocean invoice" in document.raw_text
    assert document.raw_text.splitlines()[-3:] == [
        "[Sheet: Sea Costs | Row 3] Fee\tAmount",
        "[Sheet: Sea Costs | Row 4] THC\t65.25",
        "[Sheet: Sea Costs | Row 5] THC\t310.42",
    ]


def test_docx_reads_interleaved_paragraphs_and_tables_with_header_footer():
    document = _parse_fixture("b01_interleaved.docx")

    assert document.extraction_channel is ExtractionChannel.WORD_PARSER
    assert [table.rows for table in document.tables] == [
        [["Fee", "Memo", "Amount"], ["THC", "", "65.25"]],
        [["Filing", "310.42"]],
    ]
    assert document.raw_text.splitlines() == [
        "Billing header",
        "First paragraph",
        "Fee\tMemo\tAmount",
        "THC\t\t65.25",
        "Between tables",
        "Filing\t310.42",
        "Last paragraph",
        "Billing footer",
    ]


def test_doc_uses_row_offsets_to_keep_middle_and_trailing_empty_cells():
    document = _parse_fixture("b01_empty_columns.doc")

    assert document.extraction_channel is ExtractionChannel.WORD_PARSER
    (table,) = document.tables
    assert table.rows == [
        ["Fee", "Memo", "Amount", "Ref"],
        ["THC", "", "65.25", ""],
        ["THC", "Export", "310.42", "BK41"],
    ]
    assert document.raw_text.splitlines() == [
        "Reference\tBill 081",
        "Fee\tMemo\tAmount\tRef",
        "THC\t\t65.25\t",
        "THC\tExport\t310.42\tBK41",
        "End of charges.",
    ]


def test_doc_uses_byte_offsets_in_utf16_text_and_retains_cell_paragraphs():
    document = _parse_fixture("table-merges.doc")

    (table,) = document.tables
    # The four-column second row ends before the blank first cell of the third row.
    assert table.rows == [["A", "B"], ["C", "D", "E", "F"], ["", "G", "H", "I\nJ"], ["K"]]


def test_doc_rejects_ambiguous_cells_without_valid_papx():
    from shipping_billing_agent.parsers.word_parser import WordParseError, parse_word

    data = bytearray((FIXTURES / "b01_empty_columns.doc").read_bytes())
    # FibRgFcLcb97 index 13, lcbPlcfBtePapx.
    pack_into("<I", data, _word_stream_start(data) + 0x9A + 13 * 8 + 4, 0)

    with pytest.raises(WordParseError, match="PlcfBtePapx"):
        parse_word(bytes(data), "ambiguous.doc")


def test_doc_rejects_encrypted_flag():
    from shipping_billing_agent.parsers.word_parser import WordParseError, parse_word

    data = bytearray((FIXTURES / "b01_empty_columns.doc").read_bytes())
    offset = _word_stream_start(data) + 0x0A
    (flags,) = unpack_from("<H", data, offset)
    pack_into("<H", data, offset, flags | 0x0100)

    with pytest.raises(WordParseError, match="加密"):
        parse_word(bytes(data), "encrypted.doc")


def test_pdf_renders_only_sparse_scan_page_and_keeps_text_page_tables():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    local = RecordingLocalOcr()
    data = (FIXTURES / "b01_mixed.pdf").read_bytes()
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK,
        local_ocr=local,
    )
    (document,) = dispatcher.parse_batch([("mixed.pdf", data)])

    assert document.status is DocumentStatus.PARSED, document.error_message
    assert document.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert local.pages == [(2, (945, 612))]
    assert document.raw_text.startswith("[Page 1 | Text]\nPayment terms: net thirty days.")
    assert document.raw_text.endswith("[Page 2 | OCR]\nFREIGHT 187.65")
    (table,) = document.tables
    assert table.source == "[Page 1 | Text]"
    assert table.rows == [["Fee", "Amount"], ["Filing", "14.21"]]


def test_pdf_reports_corrupt_data():
    from shipping_billing_agent.parsers.pdf_parser import PdfParseError, parse_pdf

    with pytest.raises(PdfParseError, match="PDF"):
        parse_pdf(b"broken PDF")


def test_pdf_reports_encryption_instead_of_a_generic_parse_error():
    from shipping_billing_agent.parsers.pdf_parser import PdfParseError, parse_pdf

    with pytest.raises(PdfParseError, match="加密"):
        parse_pdf((FIXTURES / "b01_encrypted.pdf").read_bytes())


def test_batch_validation_rejects_empty_input_and_too_many_top_level_files():
    from shipping_billing_agent.parsers.dispatcher import validate_batch_inputs

    (empty,) = validate_batch_inputs([], [])
    assert empty.code == "EMPTY_INPUT"
    (too_many,) = validate_batch_inputs([("invoice.pdf", b"input")] * 51, [])
    assert too_many.code == "TOO_MANY_FILES"


def test_batch_validation_rejects_invalid_files_and_checks_real_ssrf_guard():
    from shipping_billing_agent.parsers.dispatcher import validate_batch_inputs

    issues = validate_batch_inputs(
        [("empty.pdf", b""), ("large.xlsx", b"12345"), ("unknown.bin", b"data")],
        ["http://127.0.0.1/a.pdf", "not a URL"],
        limits=ResourceLimits(max_file_bytes=4),
    )

    assert {item.filename_or_url: item.code for item in issues} == {
        "empty.pdf": "EMPTY_FILE",
        "large.xlsx": "FILE_TOO_LARGE",
        "unknown.bin": "UNSUPPORTED_EXTENSION",
        "http://127.0.0.1/a.pdf": "SSRF_BLOCKED",
        "not a URL": "INVALID_URL",
    }


@pytest.mark.parametrize("relay_base_configured", [False, True])
def test_remote_only_never_uses_local_pdf_or_image_ocr_without_ready_relay(relay_base_configured):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    pdf = (FIXTURES / "b01_mixed.pdf").read_bytes()
    local = RecordingLocalOcr()
    remote = RecordingRemoteOcr()
    relay = RecordingRelay()
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_ONLY,
        remote_ocr=remote,
        local_ocr=local,
        public_relay_base_url="https://8.8.8.8" if relay_base_configured else "",
        relay_publisher=None if relay_base_configured else relay,
    )
    documents = dispatcher.parse_batch([("mixed.pdf", pdf), ("image.png", b"image")])

    assert [(doc.status, doc.extraction_channel) for doc in documents] == [
        (DocumentStatus.FAILED, ExtractionChannel.FAILED),
        (DocumentStatus.FAILED, ExtractionChannel.FAILED),
    ]
    assert [doc.content_sha256 for doc in documents] == [
        sha256(pdf).hexdigest(),
        sha256(b"image").hexdigest(),
    ]
    assert not (remote.urls or local.pages or local.images or relay.files)


def test_remote_with_ready_relay_preserves_the_local_file_hash():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    remote = RecordingRemoteOcr()
    relay = RecordingRelay()
    dispatcher = DocumentParserDispatcher(
        remote_ocr=remote, relay_publisher=relay, public_relay_base_url="https://8.8.8.8"
    )
    data = (FIXTURES / "b01_mixed.pdf").read_bytes()
    (document,) = dispatcher.parse_batch([("../invoice.pdf", data)])

    assert document.status is DocumentStatus.METADATA_ONLY, document.error_message
    assert document.extraction_channel is ExtractionChannel.PLTPLT_OCR
    assert document.original_filename == "../invoice.pdf"
    assert document.sanitized_filename == "invoice.pdf"
    assert document.content_sha256 == sha256(data).hexdigest()
    assert document.public_url is None
    ((sent_data, sent_name, ttl),) = relay.files
    assert (sent_data, sent_name) == (data, "invoice.pdf")
    assert ttl > 0
    assert remote.urls == ["https://8.8.8.8/files/bill.pdf?signature=fixture"]


def test_public_url_metadata_only_keeps_no_content_hash():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    remote = RecordingRemoteOcr()
    url = "https://8.8.8.8/download?signature=fixture"
    (document,) = DocumentParserDispatcher(remote_ocr=remote).parse_batch([], [url])

    assert remote.urls == [url]
    assert document.source_kind is SourceKind.URL
    assert document.status is DocumentStatus.METADATA_ONLY
    assert document.extraction_channel is ExtractionChannel.PLTPLT_OCR
    assert document.content_sha256 is None
    assert document.public_url == url
    assert document.raw_text == "BL001"
    assert document.ocr_payload.items[0].mbl_number == "BL001"


def test_eml_alternative_uses_html_only_when_plain_is_empty():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    message = _mail("  ")
    message.add_alternative("<p>Pay 98.35</p>", subtype="html")
    root, body = DocumentParserDispatcher().parse_batch([("email.eml", message.as_bytes())])

    assert root.metadata["Subject"] == "Fixture shipment"
    assert root.raw_text == ""
    assert body.source_kind is SourceKind.EML_BODY
    assert body.parent_doc_id == root.doc_id
    assert body.raw_text.strip() == "Pay 98.35"


@pytest.mark.parametrize("encoded", [False, True])
def test_eml_preserves_exact_bytes_of_nested_rfc822(encoded):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    nested = b"Subject: Forwarded\nX-Note: first\n second\n\nPay 39.25\n"
    transfer = b"base64" if encoded else b"8bit"
    wire = (
        b'Subject: Envelope\r\nContent-Type: multipart/mixed; boundary="carrier"\r\n\r\n'
        b"--carrier\r\nContent-Type: message/rfc822\r\n"
        b'Content-Disposition: attachment; filename="forward.eml"\r\n'
        b"Content-Transfer-Encoding: "
        + transfer
        + b"\r\n\r\n"
        + (b64encode(nested) if encoded else nested)
        + b"\r\n--carrier--\r\n"
    )

    parent, child, body = DocumentParserDispatcher().parse_batch([("mail.eml", wire)])

    assert child.status is DocumentStatus.PARSED, child.error_message
    assert child.parent_doc_id == parent.doc_id
    assert child.raw_bytes == nested
    assert child.content_sha256 == sha256(nested).hexdigest()
    assert body.parent_doc_id == child.doc_id
    assert body.raw_text.strip() == "Pay 39.25"


def test_eml_stops_at_the_fourth_nested_mail_and_preserves_parent_links():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    fourth = _mail("Level four")
    third = _mail("Level three")
    third.add_attachment(fourth, filename="fourth.eml")
    second = _mail("Level two")
    second.add_attachment(third, filename="third.eml")
    first = _mail("Level one")
    first.add_attachment(second, filename="second.eml")
    documents = DocumentParserDispatcher().parse_batch([("first.eml", first.as_bytes())])

    root, first_body, child, second_body, grandchild, third_body, rejected = documents
    assert [doc.raw_text.strip() for doc in (first_body, second_body, third_body)] == [
        "Level one",
        "Level two",
        "Level three",
    ]
    assert [doc.parent_doc_id for doc in documents] == [
        None,
        root.doc_id,
        root.doc_id,
        child.doc_id,
        child.doc_id,
        grandchild.doc_id,
        grandchild.doc_id,
    ]
    assert rejected.original_filename == "fourth.eml"
    assert rejected.status is DocumentStatus.FAILED
    assert rejected.extraction_channel is ExtractionChannel.FAILED
    assert "3" in rejected.error_message
    assert rejected.content_sha256 == sha256(rejected.raw_bytes).hexdigest()


def _wide_mail(attachment_data: bytes) -> EmailMessage:
    message = _mail("Many charges")
    for index in range(130):
        message.add_attachment(
            attachment_data,
            maintype="application",
            subtype="octet-stream",
            filename=f"bill-{index}.xls",
        )
    return message


def test_eml_batch_limit_records_one_truncation_and_still_parses_later_top_level_file():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    data = (FIXTURES / "b01_merged.xls").read_bytes()
    files = [("wide.eml", _wide_mail(data).as_bytes()), ("last.xls", data)]
    documents = DocumentParserDispatcher().parse_batch(files)

    assert len(documents) == 120
    root, _, first_attachment, *_, rejected, last = documents
    assert first_attachment.sanitized_filename == "bill-0.xls"
    assert first_attachment.status is DocumentStatus.PARSED
    assert [doc for doc in documents if doc.status is DocumentStatus.FAILED] == [rejected]
    assert rejected.parent_doc_id == root.doc_id
    assert "120" in rejected.error_message
    assert rejected.content_sha256 == sha256(data).hexdigest()
    assert last.sanitized_filename == "last.xls"
    assert last.status is DocumentStatus.PARSED


def test_eml_bad_attachment_is_isolated_from_sibling_and_later_top_level_file():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    workbook = (FIXTURES / "b01_merged.xlsx").read_bytes()
    message = _mail("Fees attached")
    message.add_attachment(
        b"invalid", maintype="application", subtype="octet-stream", filename="broken.xlsx"
    )
    message.add_attachment(
        workbook, maintype="application", subtype="octet-stream", filename="../same\xa0fees.xlsx"
    )
    dispatcher = DocumentParserDispatcher()
    root, _, broken, good, last = dispatcher.parse_batch(
        [("mail.eml", message.as_bytes()), ("single.xlsx", workbook)]
    )

    assert broken.status is DocumentStatus.FAILED
    assert broken.extraction_channel is ExtractionChannel.FAILED
    assert broken.parent_doc_id == root.doc_id
    assert broken.content_sha256 == sha256(b"invalid").hexdigest()
    assert (good.status, last.status) == (DocumentStatus.PARSED, DocumentStatus.PARSED)
    assert good.source_kind is SourceKind.EXCEL
    assert good.parent_doc_id == root.doc_id
    assert good.original_filename == "../same\xa0fees.xlsx"
    assert good.sanitized_filename == "same_fees.xlsx"
    assert last.parent_doc_id is None
    (retried,) = dispatcher.retry_single_document(
        broken.original_filename,
        workbook,
        existing_doc_id=broken.doc_id,
        parent_doc_id=broken.parent_doc_id,
    )
    assert retried.doc_id == broken.doc_id
    assert retried.parent_doc_id == root.doc_id
    assert retried.status is DocumentStatus.PARSED
    assert retried.content_sha256 == sha256(workbook).hexdigest()
