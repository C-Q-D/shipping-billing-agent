from base64 import b64encode
from email import policy
from email.message import EmailMessage
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from struct import pack_into, unpack_from
from types import SimpleNamespace

import pytest
from docx import Document
from openpyxl import Workbook
from PIL import Image

from shipping_billing_agent.config.limits import ResourceLimits
from shipping_billing_agent.config.settings import AppSettings
from shipping_billing_agent.domain.models import (
    DocumentStatus,
    ExtractionChannel,
    OcrListItem,
    OcrMode,
    OcrStructuredPayload,
    SourceKind,
)
from shipping_billing_agent.gateways.local_ocr import RapidOcrFallback
from shipping_billing_agent.gateways.url_guard import validate_public_url

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


def _four_level_mail() -> EmailMessage:
    fourth = _mail("Level four")
    third = _mail("Level three")
    third.add_attachment(fourth, filename="fourth.eml")
    second = _mail("Level two")
    second.add_attachment(third, filename="third.eml")
    first = _mail("Level one")
    first.add_attachment(second, filename="second.eml")
    return first


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


def test_docx_keeps_the_merged_fee_name_on_both_amount_rows():
    from shipping_billing_agent.parsers.word_parser import parse_word

    word = Document()
    charges = word.add_table(rows=2, cols=2)
    charges.cell(0, 0).merge(charges.cell(1, 0)).text = "FREIGHT"
    charges.cell(0, 1).text = "65.00"
    charges.cell(1, 1).text = "310.42"
    with BytesIO() as stream:
        word.save(stream)
        text, (table,) = parse_word(stream.getvalue(), "freight.docx")

    expected_rows = [["FREIGHT", "65.00"], ["FREIGHT", "310.42"]]
    assert table.rows == expected_rows
    assert [line.split("\t") for line in text.splitlines()] == expected_rows


def test_docx_preserves_nested_table_and_cell_paragraph_order_through_a_merge():
    from shipping_billing_agent.parsers.word_parser import parse_word

    word = Document()
    word.add_paragraph("Statement")
    layout = word.add_table(rows=2, cols=2)
    cell = layout.cell(0, 0).merge(layout.cell(1, 1))
    cell.text = "Before charges"
    cell.add_table(rows=1, cols=2).rows[0].cells[0].text = "FREIGHT"
    cell.tables[0].cell(0, 1).text = "187.65"
    cell.paragraphs[-1].text = "After charges"
    word.add_paragraph("End of statement")
    with BytesIO() as stream:
        word.save(stream)
        text, tables = parse_word(stream.getvalue(), "nested.docx")

    assert [table.rows for table in tables] == [
        [["Before charges\nAfter charges", ""], ["Before charges\nAfter charges", ""]],
        [["FREIGHT", "187.65"]],
    ]
    assert [line.strip() for line in text.splitlines() if line.strip()] == [
        "Statement",
        "Before charges",
        "FREIGHT\t187.65",
        "After charges",
        "Before charges",
        "After charges",
        "End of statement",
    ]


@pytest.mark.parametrize("filename", ["empty.xlsx", "empty.docx"])
def test_parse_batch_reports_office_documents_with_only_blank_cells_as_failed(filename):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    if filename.endswith(".xlsx"):
        office = Workbook()
        office.active["A1"] = "  "
    else:
        office = Document()
        office.add_table(rows=1, cols=2).cell(0, 0).text = "  "
    with BytesIO() as stream:
        office.save(stream)
        (document,) = DocumentParserDispatcher().parse_batch([(filename, stream.getvalue())])

    assert document.status is DocumentStatus.FAILED
    assert document.extraction_channel is ExtractionChannel.FAILED
    assert document.error_message == "文档内容为空，未提取到任何文本或表格"


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


def test_doc_rejects_a_middle_empty_cell_without_papx_even_without_triple_markers():
    from shipping_billing_agent.parsers.word_parser import WordParseError, parse_word

    data = bytearray((FIXTURES / "b01_empty_columns.doc").read_bytes())
    # Fill the last cell without shifting the WordDocument stream or its offsets.
    start = data.index(b"65.25\x07\x07\x07")
    data[start : start + 8] = b"5.25\x07A\x07\x07"
    _, (table,) = parse_word(bytes(data), "middle-empty.doc")
    assert table.rows[1] == ["THC", "", "5.25", "A"]

    pack_into("<I", data, _word_stream_start(data) + 0x9A + 13 * 8 + 4, 0)

    with pytest.raises(WordParseError, match="缺少有效 PlcfBtePapx 行尾属性"):
        parse_word(bytes(data), "middle-empty.doc")


def test_doc_rejects_encrypted_flag():
    from shipping_billing_agent.parsers.word_parser import WordParseError, parse_word

    data = bytearray((FIXTURES / "b01_empty_columns.doc").read_bytes())
    offset = _word_stream_start(data) + 0x0A
    (flags,) = unpack_from("<H", data, offset)
    pack_into("<H", data, offset, flags | 0x0100)

    with pytest.raises(WordParseError, match="加密"):
        parse_word(bytes(data), "encrypted.doc")


def _two_page_text_pdf(first_line: bytes, second_line: bytes) -> bytes:
    page = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 2 /Kids [4 0 R 6 0 R] >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for text in (first_line, second_line):
        objects.append(page % (len(objects) + 2))
        stream = b"BT /F1 12 Tf 24 160 Td (" + text + b") Tj ET\n"
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream")

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(b"%d 0 obj\n" % number + obj + b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        pdf.extend(b"%010d 00000 n \n" % offset)
    pdf.extend(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref_offset)
    )
    return bytes(pdf)


def test_pdf_without_local_ocr_reads_the_complete_short_text_page():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    data = _two_page_text_pdf(b"Payment terms: net thirty days.", b"USD 245.00")
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=None
    )
    (document,) = dispatcher.parse_batch([("electronic.pdf", data)])

    assert document.status is DocumentStatus.PARSED, document.error_message
    assert document.extraction_channel is ExtractionChannel.LOCAL_PDF_TEXT_FALLBACK
    assert document.raw_text == (
        "[Page 1 | Text]\nPayment terms: net thirty days.\n\n[Page 2 | Text]\nUSD 245.00"
    )


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


def test_pdf_reads_second_page_amount_from_the_actual_raster():
    from shipping_billing_agent.parsers.pdf_parser import parse_pdf

    text, _, channel = parse_pdf(
        (FIXTURES / "b01_mixed.pdf").read_bytes(), local_ocr=RapidOcrFallback()
    )

    first_page, second_page = text.split("[Page 2 | OCR]\n")
    assert "[Page 1 | Text]\nPayment terms: net thirty days." in first_page
    assert "FREIGHT 187.65" in second_page
    assert channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK


def test_pdf_without_local_ocr_fails_at_the_scanned_page():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher
    from shipping_billing_agent.parsers.pdf_parser import PdfParseError, parse_pdf

    data = (FIXTURES / "b01_mixed.pdf").read_bytes()
    with pytest.raises(PdfParseError, match="第 2 页为扫描图像页，未配置本地 OCR 引擎"):
        parse_pdf(data, local_ocr=None)

    dispatcher = DocumentParserDispatcher(ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK)
    (document,) = dispatcher.parse_batch([("mixed.pdf", data)])
    assert document.status is DocumentStatus.FAILED
    assert document.extraction_channel is ExtractionChannel.FAILED
    assert document.error_message == "第 2 页为扫描图像页，未配置本地 OCR 引擎"


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
        url_validator=validate_public_url,
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
        url_validator=validate_public_url,
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
        remote_ocr=remote,
        relay_publisher=relay,
        public_relay_base_url="https://8.8.8.8",
        url_validator=validate_public_url,
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


def test_relay_publishes_with_the_configured_ttl():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    settings = AppSettings(_env_file=None, relay_presign_ttl_seconds=45)
    relay = RecordingRelay()
    dispatcher = DocumentParserDispatcher(
        remote_ocr=RecordingRemoteOcr(),
        relay_publisher=relay,
        public_relay_base_url="https://8.8.8.8",
        url_validator=validate_public_url,
        relay_presign_ttl_seconds=settings.relay_presign_ttl_seconds,
    )

    (document,) = dispatcher.parse_batch([("bill.pdf", b"synthetic invoice")])

    assert document.status is DocumentStatus.METADATA_ONLY, document.error_message
    assert relay.files == [(b"synthetic invoice", "bill.pdf", 45)]


@pytest.mark.parametrize("ttl_seconds", [0, 301])
def test_dispatcher_rejects_out_of_range_relay_ttl(ttl_seconds):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    with pytest.raises(ValueError, match="relay_presign_ttl_seconds"):
        DocumentParserDispatcher(relay_presign_ttl_seconds=ttl_seconds)


def test_batch_validation_requires_an_injected_url_validator_for_public_urls():
    from shipping_billing_agent.parsers.dispatcher import validate_batch_inputs

    with pytest.raises(ValueError, match="^必须注入 url_validator$"):
        validate_batch_inputs([], ["https://93.184.216.34/bill.pdf"])


def test_dispatcher_requires_an_injected_url_validator_for_batch_and_retry_urls():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    dispatcher = DocumentParserDispatcher(remote_ocr=RecordingRemoteOcr())
    url = "https://93.184.216.34/bill.pdf"

    with pytest.raises(ValueError, match="^必须注入 url_validator$"):
        dispatcher.parse_batch([], [url])
    with pytest.raises(ValueError, match="^必须注入 url_validator$"):
        dispatcher.retry_single_document("bill.pdf", existing_doc_id="doc-1", public_url=url)


def test_dispatcher_requires_an_injected_url_validator_for_relay_publisher():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    with pytest.raises(ValueError, match="^必须注入 url_validator$"):
        DocumentParserDispatcher(relay_publisher=RecordingRelay())


def test_public_url_metadata_only_keeps_no_content_hash():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    remote = RecordingRemoteOcr()
    url = "https://8.8.8.8/download?signature=fixture"
    dispatcher = DocumentParserDispatcher(remote_ocr=remote, url_validator=validate_public_url)
    (document,) = dispatcher.parse_batch([], [url])

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


@pytest.mark.parametrize(
    ("filename", "html"),
    [
        ("invoice.png", '<p>Charges</p><img src="cid:bill-img"><p>Amount due 246.00</p>'),
        ("scan.png", '<p>Charges</p><img src="cid:bill-img"><p>Amount due 246.00</p>'),
        ("InsertPic_scan.png", '<p>Charges</p><img src="cid:bill-img"><p>Amount due 246.00</p>'),
        ("账单.jpg", '<div class="signature"><img src="cid:bill-img"></div>'),
    ],
)
def test_eml_parses_inline_bill_images_instead_of_skipping_them(filename, html):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    image_data = b"synthetic invoice image"
    message = _mail("Charges attached")
    message.add_alternative(html, subtype="html")
    message.get_payload()[-1].add_related(
        image_data, maintype="image", subtype="png", cid="<bill-img>", filename=filename
    )
    local = RecordingLocalOcr()
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=local
    )

    root, body, image = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    assert body.raw_text.strip() == "Charges attached"
    assert image.parent_doc_id == root.doc_id
    assert image.status is DocumentStatus.PARSED, image.error_message
    assert image.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert image.raw_text == "Image 246.00"
    assert local.images == [(image_data, filename)]


def test_eml_footer_contact_does_not_skip_a_cid_image_in_the_main_body():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    message = _mail("Charges attached")
    message.add_alternative(
        '<main><p>Charges attached</p><hr><img src="cid:scan"></main>'
        '<footer class="signature">Tel: 123456<img src="cid:logo"></footer>',
        subtype="html",
    )
    html = message.get_payload()[-1]
    scan_data = b"synthetic invoice scan"
    logo_data = b"synthetic signature logo"
    html.add_related(scan_data, maintype="image", subtype="png", cid="<scan>", filename="scan.png")
    html.add_related(logo_data, maintype="image", subtype="png", cid="<logo>", filename="logo.png")
    local = RecordingLocalOcr()
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=local
    )

    _, _, scan, logo = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    assert scan.sanitized_filename == "scan.png"
    assert scan.status is DocumentStatus.PARSED
    assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert scan.raw_text == "Image 246.00"
    assert local.images == [(scan_data, "scan.png")]
    assert logo.sanitized_filename == "logo.png"
    assert logo.status is DocumentStatus.SKIPPED_NOISE
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE
    assert logo.raw_bytes == logo_data


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

    documents = DocumentParserDispatcher().parse_batch(
        [("first.eml", _four_level_mail().as_bytes())]
    )

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


@pytest.mark.parametrize("depth_args", [{}, {"depth": 1}], ids=["omitted", "too-shallow"])
def test_eml_retry_inherits_depth_without_expanding_the_fourth_mail(depth_args):
    from shipping_billing_agent.parsers import dispatcher

    parser = dispatcher.DocumentParserDispatcher()
    documents = parser.parse_batch([("first.eml", _four_level_mail().as_bytes())])
    rejected = documents[-1]
    retry_target = dict(existing_doc_id=rejected.doc_id, parent_doc_id=rejected.parent_doc_id)
    with pytest.raises(ValueError, match="重试嵌套子文档时必须显式传入原父链 depth"):
        parser.retry_single_document(
            rejected.original_filename,
            rejected.raw_bytes,
            remaining_expanded_slots=113,
            **retry_target,
            **depth_args,
        )

    depth, slots = dispatcher.compute_retry_context(list(reversed(documents)), rejected.doc_id)
    assert (depth, slots) == (4, 113)
    assert dispatcher.compute_retry_context(documents, documents[0].doc_id) == (1, 119)
    (retried,) = parser.retry_single_document(
        rejected.original_filename,
        rejected.raw_bytes,
        depth=depth,
        remaining_expanded_slots=slots,
        **retry_target,
    )

    assert retried.doc_id == rejected.doc_id
    assert retried.parent_doc_id == rejected.parent_doc_id
    assert retried.status is DocumentStatus.FAILED
    assert retried.extraction_channel is ExtractionChannel.FAILED
    assert retried.error_message == "邮件嵌套深度超过 3 层"


def test_eml_retry_inherits_remaining_expansion_slots():
    from shipping_billing_agent.parsers import dispatcher

    limits = ResourceLimits(max_expanded_documents=3)
    parser = dispatcher.DocumentParserDispatcher(limits=limits)
    parent_mail = _mail(" ")
    child_mail = _wide_mail((FIXTURES / "b01_merged.xls").read_bytes(), attachment_count=1)
    parent_mail.add_attachment(child_mail, filename="second.eml")
    documents = parser.parse_batch([("first.eml", parent_mail.as_bytes())])
    parent, old_child, old_body = documents
    assert old_body.parent_doc_id == old_child.doc_id
    retry_target = dict(existing_doc_id=old_child.doc_id, parent_doc_id=parent.doc_id)
    with pytest.raises(ValueError, match="必须显式传入 remaining_expanded_slots"):
        parser.retry_single_document(
            old_child.original_filename, old_child.raw_bytes, depth=2, **retry_target
        )

    depth, slots = dispatcher.compute_retry_context(documents, old_child.doc_id, limits)
    assert (depth, slots) == (2, 1)
    root, body = parser.retry_single_document(
        old_child.original_filename,
        old_child.raw_bytes,
        depth=depth,
        remaining_expanded_slots=slots,
        **retry_target,
    )

    assert (root.doc_id, root.parent_doc_id) == (old_child.doc_id, parent.doc_id)
    assert root.status is DocumentStatus.FAILED
    assert root.error_message == "单批展开文档数超过 3 项上限，部分子件已截断"
    assert body.parent_doc_id == root.doc_id
    assert body.status is DocumentStatus.PARSED
    assert body.raw_text.strip() == "Many charges"


@pytest.mark.parametrize(
    ("filename", "media_type"),
    [("mail.eml", None), ("forward.bin", "message/rfc822")],
)
def test_top_level_eml_retry_requires_remaining_expansion_slots(filename, media_type):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    with pytest.raises(ValueError, match="必须显式传入 remaining_expanded_slots"):
        DocumentParserDispatcher().retry_single_document(
            filename, _mail("Charges").as_bytes(), existing_doc_id="mail", media_type=media_type
        )


def test_force_parse_requires_original_depth_for_nested_signature_images():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    parser = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=RecordingLocalOcr()
    )
    with pytest.raises(ValueError, match="重试嵌套子文档时必须显式传入原父链 depth"):
        parser.force_parse_skipped_document(
            "logo.png", b"skipped signature", existing_doc_id="logo", parent_doc_id="mail"
        )


def _wide_mail(attachment_data: bytes, attachment_count: int) -> EmailMessage:
    message = _mail("Many charges")
    for index in range(attachment_count):
        message.add_attachment(
            attachment_data,
            maintype="application",
            subtype="octet-stream",
            filename=f"bill-{index}.xls",
        )
    return message


@pytest.mark.parametrize(
    ("attachment_count", "expected_status"),
    [
        (117, DocumentStatus.PARSED),
        (118, DocumentStatus.PARSED),
        (119, DocumentStatus.FAILED),
    ],
)
def test_eml_batch_limit_accepts_the_last_slot_and_only_flags_a_real_overflow(
    attachment_count, expected_status
):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    data = (FIXTURES / "b01_merged.xls").read_bytes()
    message = _wide_mail(data, attachment_count)
    root, body, *attachments = DocumentParserDispatcher().parse_batch(
        [("wide.eml", message.as_bytes())]
    )

    assert len(attachments) + 2 == min(attachment_count + 2, 120)
    assert root.status is expected_status
    if expected_status is DocumentStatus.FAILED:
        assert root.extraction_channel is ExtractionChannel.FAILED
        assert root.error_message == "单批展开文档数超过 120 项上限，部分子件已截断"
    assert body.status is DocumentStatus.PARSED
    assert all(doc.status is DocumentStatus.PARSED for doc in attachments)
    assert [doc.sanitized_filename for doc in attachments] == [
        f"bill-{index}.xls" for index in range(min(attachment_count, 118))
    ]
    assert attachments[-1].content_sha256 == sha256(data).hexdigest()


@pytest.mark.parametrize("later_has_body", [True, False])
def test_eml_with_zero_remaining_slots_reports_cutoff_and_retains_later_top_level_file(
    later_has_body,
):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    data = (FIXTURES / "b01_merged.xls").read_bytes()
    later = _mail("Fee statement") if later_has_body else EmailMessage(policy=policy.SMTP)
    if not later_has_body:
        later["Subject"] = "Attachment only"
        later.add_attachment(
            data, maintype="application", subtype="octet-stream", filename="charges.xls"
        )
    files = [
        ("first.eml", _wide_mail(data, 1).as_bytes()),
        ("later.eml", later.as_bytes()),
        ("last.xls", data),
    ]
    parsed = DocumentParserDispatcher(limits=ResourceLimits(max_expanded_documents=5))

    root, body, kept, later_root, last = parsed.parse_batch(files)

    assert later_root.status is DocumentStatus.FAILED
    assert later_root.extraction_channel is ExtractionChannel.FAILED
    assert later_root.error_message == "单批展开文档数超过 5 项上限，部分子件已截断"
    assert root.status is DocumentStatus.PARSED
    assert all(doc.status is DocumentStatus.PARSED for doc in (body, kept))
    assert kept.sanitized_filename == "bill-0.xls"
    assert last.sanitized_filename == "last.xls"
    assert last.status is DocumentStatus.PARSED


@pytest.mark.parametrize("limit", [120, 121])
@pytest.mark.parametrize(
    ("subtype", "content"),
    [("html", "<html><body><div> </div></body></html>"), ("plain", "  ")],
)
def test_eml_empty_body_does_not_report_truncation_at_capacity(limit, subtype, content):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    data = (FIXTURES / "b01_merged.xls").read_bytes()
    empty = _mail("")
    empty.set_content(content, subtype=subtype, cte="base64")
    dispatcher = DocumentParserDispatcher(limits=ResourceLimits(max_expanded_documents=limit))

    documents = dispatcher.parse_batch(
        [
            ("wide.eml", _wide_mail(data, attachment_count=117).as_bytes()),
            ("empty.eml", empty.as_bytes()),
        ]
    )

    assert len(documents) == 120
    assert documents[0].status is DocumentStatus.PARSED, documents[0].error_message
    assert documents[-1].sanitized_filename == "empty.eml"
    assert documents[-1].status is DocumentStatus.PARSED, documents[-1].error_message
    assert documents[-1].error_message is None


def test_eml_budget_stops_before_building_or_decoding_overflow_mime_payloads(monkeypatch):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    wire = _wide_mail((FIXTURES / "b01_merged.xls").read_bytes(), 5).as_bytes()
    materialized = []
    decoded = []
    set_payload = EmailMessage.set_payload
    get_payload = EmailMessage.get_payload

    def record_payload(message, payload, *args, **kwargs):
        if message.get_content_type() == "application/octet-stream" and payload:
            materialized.append(message.get_filename())
        return set_payload(message, payload, *args, **kwargs)

    def record_decode(message, i=None, decode=False):
        if message.get_content_type() == "application/octet-stream" and decode:
            decoded.append(message.get_filename())
        return get_payload(message, i, decode)

    monkeypatch.setattr(EmailMessage, "set_payload", record_payload)
    monkeypatch.setattr(EmailMessage, "get_payload", record_decode)
    dispatcher = DocumentParserDispatcher(limits=ResourceLimits(max_expanded_documents=3))

    root, body, attachment = dispatcher.parse_batch([("wide.eml", wire)])

    assert materialized == ["bill-0.xls"]
    assert decoded == ["bill-0.xls"]
    assert root.status is DocumentStatus.FAILED
    assert body.status is DocumentStatus.PARSED
    assert attachment.status is DocumentStatus.PARSED
    assert attachment.sanitized_filename == "bill-0.xls"


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
        depth=2,
    )
    assert retried.doc_id == broken.doc_id
    assert retried.parent_doc_id == root.doc_id
    assert retried.status is DocumentStatus.PARSED
    assert retried.content_sha256 == sha256(workbook).hexdigest()
