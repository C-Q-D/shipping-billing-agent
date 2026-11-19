import re
from base64 import b64encode
from email import policy
from email.message import EmailMessage
from hashlib import sha256
from io import BytesIO
from itertools import accumulate, pairwise
from math import ceil
from pathlib import Path
from struct import pack_into, unpack_from
from types import SimpleNamespace
from unittest.mock import patch

import pdfplumber
import pytest
from docx import Document
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont

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


def _two_page_pdf(
    first_page: bytes, second_page: bytes, *, page_size: tuple[int, int] = (300, 200)
) -> bytes:
    page = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
        b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 2 /Kids [4 0 R 6 0 R] >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for stream in (first_page, second_page):
        objects.append(page % (*page_size, len(objects) + 2))
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


def _vector_fee_pdf() -> bytes:
    # Draw the fee using PDF path operators, leaving only the page number as text.
    glyphs = {
        "F": b"0 0 m 0 23 l 12 23 l 0 12 m 10 12 l S",
        "R": b"0 0 m 0 23 l 6 23 l 15 23 15 12 6 12 c 0 12 l 6 12 m 12 0 l S",
        "E": b"12 23 m 0 23 l 0 0 l 12 0 l 0 12 m 10 12 l S",
        "I": b"0 0 m 5 0 l 2.5 0 m 2.5 23 l 0 23 m 5 23 l S",
        "G": b"12 19 m 8 27 0 23 0 15 c 0 6 l 0 -2 12 -2 12 5 c 12 10 l 7 10 l S",
        "H": b"0 0 m 0 23 l 12 0 m 12 23 l 0 12 m 12 12 l S",
        "T": b"0 23 m 12 23 l 6 23 m 6 0 l S",
        "U": b"0 23 m 0 7 l 0 -2 12 -2 12 7 c 12 23 l S",
        "S": b"12 20 m 4 28 -6 17 4 13 c 8 11 l 20 5 7 -5 0 3 c S",
        "D": b"0 0 m 0 23 l 4 23 l 16 23 16 0 4 0 c h S",
        "3": b"0 21 m 12 28 18 15 6 12 c 18 9 12 -5 0 2 c S",
        "1": b"0 18 m 4 23 l 4 0 l 0 0 m 8 0 l S",
        "0": b"6 23 m 15 23 15 0 6 0 c -2 0 -2 23 6 23 c S",
        ".": b"0 0 2.3 2.3 re f",
        "4": b"9 0 m 9 23 l 0 8 l 13 8 l S",
        "2": b"0 18 m 0 26 12 25 12 18 c 12 13 4 8 0 0 c 12 0 l S",
    }
    paths = bytearray(b"BT /F1 18 Tf 146 34 Td (2) Tj ET\n2.3 w 1 J 1 j\n")
    x = 24
    for char in "FREIGHT USD 310.42":
        if char == " ":
            x += 8
        else:
            paths.extend(b"q 1 0 0 1 %d 105 cm %s Q\n" % (x, glyphs[char]))
            x += {"I": 9, "1": 12, ".": 7}.get(char, 16)
    return _two_page_pdf(
        b"BT /F1 12 Tf 24 160 Td (Payment terms: net thirty days.) Tj ET\n", bytes(paths)
    )


_PAYMENT_TERMS_PAGE = b"BT /F1 12 Tf 24 160 Td (Payment terms: net thirty days.) Tj ET\n"


def _mixed_table_vector_fee_pdf() -> bytes:
    glyphs = {
        "F": [[(0, 0), (0, 23), (12, 23)], [(0, 12), (10, 12)]],
        "R": [[(0, 0), (0, 23), (8, 23), (13, 19), (13, 16), (8, 12), (0, 12)],
              [(6, 12), (12, 0)]],
        "E": [[(12, 23), (0, 23), (0, 0), (12, 0)], [(0, 12), (10, 12)]],
        "I": [[(0, 0), (5, 0)], [(2.5, 0), (2.5, 23)], [(0, 23), (5, 23)]],
        "G": [[(12, 20), (8, 23), (4, 23), (0, 19), (0, 4), (4, 0), (12, 0),
               (12, 10), (7, 10)]],
        "H": [[(0, 0), (0, 23)], [(12, 0), (12, 23)], [(0, 12), (12, 12)]],
        "T": [[(0, 23), (12, 23)], [(6, 23), (6, 0)]],
        "U": [[(0, 23), (0, 5), (3, 0), (9, 0), (12, 5), (12, 23)]],
        "S": [[(12, 20), (9, 23), (3, 23), (0, 19), (0, 15), (4, 12), (8, 11),
               (12, 8), (12, 4), (9, 0), (3, 0), (0, 3)]],
        "D": [[(0, 0), (0, 23), (6, 23), (12, 19), (12, 4), (6, 0), (0, 0)]],
        "3": [[(0, 21), (4, 23), (9, 23), (12, 19), (12, 16), (8, 12), (5, 12),
               (9, 12), (13, 8), (13, 4), (9, 0), (4, 0), (0, 2)]],
        "1": [[(0, 18), (4, 23), (4, 0)], [(0, 0), (8, 0)]],
        "0": [[(6, 23), (10, 22), (13, 18), (13, 5), (10, 1), (6, 0), (2, 1),
               (0, 5), (0, 18), (2, 22), (6, 23)]],
        "4": [[(9, 0), (9, 23), (0, 8), (13, 8)]],
        "2": [[(0, 18), (2, 22), (7, 23), (12, 20), (12, 16), (9, 12), (0, 0), (12, 0)]],
    }
    paths = bytearray(
        b"24 144 150 36 re S\n99 144 m 99 180 l S\n"
        b"BT /F1 12 Tf 36 158 Td (Fee) Tj ET\nBT /F1 18 Tf 146 34 Td (2) Tj ET\n"
        b"2.3 w 1 J\n"
    )
    x = 24
    for char in "FREIGHT USD 310.42":
        if char == " ":
            x += 8
            continue
        if char == ".":
            paths.extend(b"%d 85 2.3 2.3 re f\n" % x)
        else:
            # Stroke each segment separately so the PDF contains lines instead of curves.
            for stroke in glyphs[char]:
                for (x1, y1), (x2, y2) in pairwise(stroke):
                    paths.extend(b"%g %g m %g %g l S\n" % (x + x1, 85 + y1, x + x2, 85 + y2))
        x += {"I": 9, "1": 12, ".": 7}.get(char, 16)
    return _two_page_pdf(_PAYMENT_TERMS_PAGE, bytes(paths))


def _short_vector_amount_pdf() -> bytes:
    paths = bytearray(
        b"BT /F1 30 Tf 24 105 Td (USD) Tj ET\nBT /F1 18 Tf 146 34 Td (2) Tj ET\n"
        b"2.3 w 1 J\n"
    )
    for x in (93, 109, 131, 147):
        paths.extend(b"%d 128 m %d 128 l S\n%d 128 m %d 105 l S\n" % (x, x + 12, x + 12, x + 3))
    paths.extend(b"124 105 2.3 2.3 re f\n")
    return _two_page_pdf(_PAYMENT_TERMS_PAGE, bytes(paths))


def test_r5_f1_pdf_keeps_the_electronic_table_on_the_ocr_page():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=RapidOcrFallback()
    )
    (document,) = dispatcher.parse_batch([("mixed-vector.pdf", _mixed_table_vector_fee_pdf())])

    assert document.status is DocumentStatus.PARSED, document.error_message
    assert document.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    _, ocr_source, ocr_text = document.raw_text.partition("[Page 2 | OCR]\n")
    assert ocr_source, document.raw_text
    assert "FREIGHT USD 310.42" in ocr_text, ocr_text
    (table,) = document.tables
    assert table.source == "[Page 2 | Text]"
    assert table.rows == [["Fee", ""]]


def test_r5_f1_pdf_with_table_and_vector_fee_requires_ocr():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=None
    )
    (document,) = dispatcher.parse_batch([("mixed-vector.pdf", _mixed_table_vector_fee_pdf())])

    assert document.status is DocumentStatus.FAILED, document.raw_text
    assert document.error_message == "第 2 页为扫描图像页，未配置本地 OCR 引擎"


def test_r5_f2_pdf_reads_amount_drawn_with_nine_vector_objects():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=RapidOcrFallback()
    )
    (document,) = dispatcher.parse_batch([("short-vector.pdf", _short_vector_amount_pdf())])

    assert document.status is DocumentStatus.PARSED, document.error_message
    assert document.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    _, ocr_source, ocr_text = document.raw_text.partition("[Page 2 | OCR]\n")
    assert ocr_source, document.raw_text
    assert "USD" in ocr_text, ocr_text
    assert "77.77" in ocr_text, ocr_text


def test_r5_f2_pdf_short_vector_amount_requires_ocr():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=None
    )
    (document,) = dispatcher.parse_batch([("short-vector.pdf", _short_vector_amount_pdf())])

    assert document.status is DocumentStatus.FAILED, document.raw_text
    assert document.error_message == "第 2 页为扫描图像页，未配置本地 OCR 引擎"


def test_r5_f2_pdf_with_long_rule_keeps_short_electronic_text():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE,
        b"24 144 m 264 144 l S\nBT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n",
    )
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=RapidOcrFallback()
    )
    (document,) = dispatcher.parse_batch([("electronic.pdf", data)])

    assert document.status is DocumentStatus.PARSED, document.error_message
    assert document.extraction_channel is ExtractionChannel.LOCAL_PDF_TEXT_FALLBACK
    assert document.raw_text.endswith("[Page 2 | Text]\nUSD 245.00"), document.raw_text


@pytest.fixture
def r6_filled_rectangle_pdf() -> bytes:
    page_width, page_height = 1460, 410
    strip_height = 9
    with Image.new("L", (page_width, page_height), 255) as mask:
        font = ImageFont.load_default(size=230)
        draw = ImageDraw.Draw(mask)
        draw.text((60, 35), "USD 77.77", font=font, fill=0, stroke_width=5)
        pixels = mask.tobytes()

    paths = bytearray(b"BT /F1 18 Tf 720 30 Td (2) Tj ET\n")
    for top in range(0, page_height - strip_height + 1, strip_height):
        start = (top + strip_height // 2) * page_width
        row = pixels[start : start + page_width]
        y = page_height - top - strip_height
        for run in re.finditer(rb"\x00+", row):
            width = max(32, run.end() - run.start())
            x = (run.start() + run.end() - width) // 2
            paths.extend(b"%d %d %d %d re f\n" % (x, y, width, strip_height))

    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, bytes(paths), page_size=(page_width, page_height)
    )
    with BytesIO(data) as stream, pdfplumber.open(stream) as pdf:
        assert len(pdf.pages) == 2
        page = pdf.pages[1]
        assert page.extract_text() == "2"
        assert not (page.lines or page.curves or page.images)
        assert len(page.rects) > 100
        assert all(
            rect["fill"] and 32 <= rect["width"] <= 168 and rect["height"] == strip_height
            for rect in page.rects
        )
    return data


def test_r6_2_pdf_reads_amount_drawn_as_wide_filled_rectangles(r6_filled_rectangle_pdf):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=RapidOcrFallback()
    )
    (document,) = dispatcher.parse_batch([("filled-strips.pdf", r6_filled_rectangle_pdf)])

    assert document.status is DocumentStatus.PARSED, document.error_message
    _, ocr_source, ocr_text = document.raw_text.partition("[Page 2 | OCR]\n")
    assert ocr_source, document.raw_text
    assert document.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert "USD" in ocr_text, ocr_text
    assert "77.77" in ocr_text, ocr_text


def test_r6_2_pdf_with_wide_filled_rectangles_requires_ocr(r6_filled_rectangle_pdf):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=None
    )
    (document,) = dispatcher.parse_batch([("filled-strips.pdf", r6_filled_rectangle_pdf)])

    assert document.status is DocumentStatus.FAILED, document.raw_text
    assert document.error_message == "第 2 页为扫描图像页，未配置本地 OCR 引擎"


@pytest.fixture
def r8_stroked_amount_pdf(r6_filled_rectangle_pdf) -> bytes:
    paths = bytearray(b"BT /F1 18 Tf 720 30 Td (2) Tj ET\n9 w 0 J\n")
    with BytesIO(r6_filled_rectangle_pdf) as stream, pdfplumber.open(stream) as pdf:
        page = pdf.pages[1]
        page_size = int(page.width), int(page.height)
        for rect in page.rects:
            y = (rect["y0"] + rect["y1"]) / 2
            paths.extend(b"%g %g m %g %g l S\n" % (rect["x0"], y, rect["x1"], y))

    data = _two_page_pdf(_PAYMENT_TERMS_PAGE, bytes(paths), page_size=page_size)
    with BytesIO(data) as stream, pdfplumber.open(stream) as pdf:
        page = pdf.pages[1]
        assert page.extract_text() == "2"
        assert len(page.lines) > 100
        assert not (page.rects or page.curves or page.images)
        assert all(
            line["linewidth"] == 9 and 32 <= line["width"] <= 168 and line["height"] == 0
            for line in page.lines
        )
        rows = sorted({line["top"] for line in page.lines})
        assert {end - start for start, end in pairwise(rows)} == {9}
    return data


def test_r8_1_pdf_reads_amount_drawn_as_wide_horizontal_strokes(r8_stroked_amount_pdf):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=RapidOcrFallback()
    )
    (document,) = dispatcher.parse_batch([("stroked-amount.pdf", r8_stroked_amount_pdf)])

    assert document.status is DocumentStatus.PARSED, document.error_message
    _, ocr_source, ocr_text = document.raw_text.partition("[Page 2 | OCR]\n")
    assert ocr_source, document.raw_text
    assert document.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert "USD" in ocr_text, ocr_text
    assert "77.77" in ocr_text, ocr_text


def test_r8_1_pdf_with_wide_horizontal_strokes_requires_ocr(r8_stroked_amount_pdf):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=None
    )
    (document,) = dispatcher.parse_batch([("stroked-amount.pdf", r8_stroked_amount_pdf)])

    assert document.status is DocumentStatus.FAILED, document.raw_text
    assert document.error_message == "第 2 页为扫描图像页，未配置本地 OCR 引擎"


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
@pytest.mark.parametrize(
    "segments",
    [
        pytest.param([(24, 264)], id="single-segment"),
        pytest.param([(x, x + 24) for x in range(24, 264, 24)], id="continuous-10-segments"),
        pytest.param([(x, x + 15) for x in range(24, 264, 25)], id="dashed-10-segments"),
    ],
)
def test_r6_3_pdf_keeps_short_electronic_text_with_a_segmented_rule(segments, local_ocr):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    rule = b"".join(b"%d 144 m %d 144 l S\n" % (left, right) for left, right in segments)
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, rule + b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n"
    )
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=local_ocr
    )
    with patch.object(
        RapidOcrFallback,
        "recognize_pil_image",
        autospec=True,
        side_effect=RapidOcrFallback.recognize_pil_image,
    ) as recognize_page:
        (document,) = dispatcher.parse_batch([("electronic.pdf", data)])

    assert document.status is DocumentStatus.PARSED, document.error_message
    recognize_page.assert_not_called()
    assert document.extraction_channel is ExtractionChannel.LOCAL_PDF_TEXT_FALLBACK
    assert document.raw_text.endswith("[Page 2 | Text]\nUSD 245.00"), document.raw_text


@pytest.fixture(
    params=[
        pytest.param(
            (
                b"24 144 m 48 144 l 72 144 l 96 144 l 120 144 l 144 144 l "
                b"168 144 l 192 144 l 216 144 l 240 144 l 264 144 l S\n",
                1,
            ),
            id="a3-single-collinear-path",
        ),
        pytest.param(
            (
                b"24 44 m 264 44 l S\n24 70 m 264 70 l S\n"
                b"24 96 m 264 96 l S\n24 122 m 264 122 l S\n",
                0,
            ),
            id="a4a-spaced-parallel-rules",
        ),
        pytest.param((b"24 120 240 4 re f\n24 86 240 4 re f\n", 0), id="a4b-filled-bars"),
    ]
)
def t10_decorated_pdf(request) -> bytes:
    drawing, expected_curves = request.param
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, drawing + b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n"
    )
    with BytesIO(data) as stream, pdfplumber.open(stream) as pdf:
        page = pdf.pages[1]
        assert len(page.curves) == expected_curves
    return data


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
def test_t10_pdf_keeps_short_electronic_text_with_decorative_rules(t10_decorated_pdf, local_ocr):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=local_ocr
    )
    with patch.object(
        RapidOcrFallback,
        "recognize_pil_image",
        autospec=True,
        side_effect=RapidOcrFallback.recognize_pil_image,
    ) as recognize_page:
        (document,) = dispatcher.parse_batch([("electronic.pdf", t10_decorated_pdf)])

    assert document.status is DocumentStatus.PARSED, document.error_message
    recognize_page.assert_not_called()
    assert document.extraction_channel is ExtractionChannel.LOCAL_PDF_TEXT_FALLBACK
    assert document.raw_text.endswith("[Page 2 | Text]\nUSD 245.00"), document.raw_text


def _assert_decorated_pdf_keeps_electronic_text(
    data: bytes,
    local_ocr: RapidOcrFallback | None,
    *,
    expected_suffix: str = "[Page 2 | Text]\nUSD 245.00",
) -> None:
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=local_ocr
    )
    with patch.object(
        RapidOcrFallback,
        "recognize_pil_image",
        autospec=True,
        side_effect=RapidOcrFallback.recognize_pil_image,
    ) as recognize_page:
        (document,) = dispatcher.parse_batch([("electronic.pdf", data)])

    assert document.status is DocumentStatus.PARSED, document.error_message
    recognize_page.assert_not_called()
    assert document.extraction_channel is ExtractionChannel.LOCAL_PDF_TEXT_FALLBACK
    assert document.raw_text.endswith(expected_suffix), document.raw_text


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
def test_r8_4_pdf_keeps_electronic_text_with_repeated_collinear_vertices(local_ocr):
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE,
        b"24 144 m 48 144 l 72 144 l 96 144 l 96 144 l 120 144 l 144 144 l "
        b"168 144 l 192 144 l 192 144 l 216 144 l 240 144 l 264 144 l S\n"
        b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n",
    )
    with BytesIO(data) as stream, pdfplumber.open(stream) as pdf:
        assert len(pdf.pages[1].curves) == 1

    _assert_decorated_pdf_keeps_electronic_text(data, local_ocr)


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
def test_r8_5_pdf_keeps_electronic_text_with_closed_polyline_bars(local_ocr):
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE,
        b"24 120 m 144 120 l 264 120 l 264 124 l 144 124 l 24 124 l h f\n"
        b"24 86 m 144 86 l 264 86 l 264 90 l 144 90 l 24 90 l 24 86 l h f\n"
        b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n",
    )
    with BytesIO(data) as stream, pdfplumber.open(stream) as pdf:
        page = pdf.pages[1]
        assert len(page.curves) == 2
        assert not page.rects

    _assert_decorated_pdf_keeps_electronic_text(data, local_ocr)


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
def test_r9_f3_pdf_keeps_electronic_text_with_a_clipped_final_dash(local_ocr):
    rule = b"".join(
        b"%d 144 m %d 144 l S\n" % (left, min(left + 20, 264))
        for left in range(24, 264, 28)
    )
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, rule + b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n"
    )

    _assert_decorated_pdf_keeps_electronic_text(data, local_ocr)


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
def test_r10_f3_sub_point_trailing_dash(local_ocr):
    dash_length = 21.9
    period = dash_length + 8
    rule = b"".join(
        b"%.1f 144 m %.1f 144 l S\n" % (24 + index * period, 24 + index * period + dash_length)
        for index in range(8)
    )
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE,
        rule + b"263.2 144 m 264 144 l S\nBT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n",
    )

    _assert_decorated_pdf_keeps_electronic_text(data, local_ocr)


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
@pytest.mark.parametrize(
    ("dash_lengths", "rows"),
    [
        pytest.param([0.5] + [20.875] * 8 + [0.5], (144,), id="0.5pt-both-ends"),
        pytest.param([21.95] * 8 + [0.4], (144, 120), id="0.4pt-two-trailing-ends"),
    ],
)
def test_r11_f3_sub_half_point_dashed_rule_endpoints(dash_lengths, rows, local_ocr):
    # Each rule runs from x=24 to x=264 with 8 pt gaps.
    dash_starts = accumulate((length + 8 for length in dash_lengths[:-1]), initial=24.0)
    segments = tuple(zip(dash_starts, dash_lengths, strict=True))
    drawing = b"".join(
        b"%.3f %d m %.3f %d l S\n" % (left, y, left + length, y)
        for y in rows
        for left, length in segments
    )
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, drawing + b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n"
    )

    _assert_decorated_pdf_keeps_electronic_text(data, local_ocr)


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
@pytest.mark.parametrize(
    ("dash_lengths", "rows"),
    [
        pytest.param([0.5] + [20.875] * 8 + [0.5], (144,), id="0.5pt-both-ends"),
        pytest.param([21.95] * 8 + [0.4], (144, 120), id="0.4pt-two-trailing-ends"),
    ],
)
def test_r12_3_filled_rect_sub_half_point_dashed_rules(dash_lengths, rows, local_ocr):
    # Each rule runs from x=24 to x=264 with 8 pt gaps.
    dash_starts = accumulate((length + 8 for length in dash_lengths[:-1]), initial=24.0)
    segments = tuple(zip(dash_starts, dash_lengths, strict=True))
    drawing = b"".join(
        b"%.3f %d %.3f 1 re f\n" % (left, y, length)
        for y in rows
        for left, length in segments
    )
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, drawing + b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n"
    )
    with BytesIO(data) as stream, pdfplumber.open(stream) as pdf:
        page = pdf.pages[1]
        assert not page.lines and not page.curves and page.rects

    _assert_decorated_pdf_keeps_electronic_text(data, local_ocr)


@pytest.fixture(
    params=[
        ([0.5] + [20.875] * 8 + [0.5], (144,)),
        ([21.95] * 8 + [0.4], (144, 120)),
    ],
    ids=["0.5pt-both-ends", "0.4pt-two-trailing-ends"],
)
def r13_tiled_dashed_rule_pdf(request) -> bytes:
    dash_lengths, rows = request.param
    # Keep the original 240 pt span and 8 pt gaps while tiling each dash.
    dash_starts = accumulate((length + 8 for length in dash_lengths[:-1]), initial=24.0)
    segments = tuple(zip(dash_starts, dash_lengths, strict=True))
    drawing = b"".join(
        b"%.3f %d %.3f 1 re f\n" % (left + offset, y, min(4, length - offset))
        for y in rows
        for left, length in segments
        for offset in range(0, ceil(length), 4)
    )
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, drawing + b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n"
    )
    with BytesIO(data) as stream, pdfplumber.open(stream) as pdf:
        page = pdf.pages[1]
        assert not page.lines and not page.curves and page.rects
        assert all(rect["width"] <= 4.0 and rect["height"] == 1.0 for rect in page.rects)
    return data


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
def test_r13_3_tiled_small_filled_rect_dashed_rules(r13_tiled_dashed_rule_pdf, local_ocr):
    _assert_decorated_pdf_keeps_electronic_text(r13_tiled_dashed_rule_pdf, local_ocr)


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
def test_r14_3_implicitly_closed_tiles_keep_pdf_text(r13_tiled_dashed_rule_pdf, local_ocr):
    with pdfplumber.open(BytesIO(r13_tiled_dashed_rule_pdf)) as pdf:
        tiles = pdf.pages[1].rects
    drawing = b"".join(
        b"%.3f %.3f m %.3f %.3f l %.3f %.3f l %.3f %.3f l f\n"
        % (
            tile["x0"], tile["y0"], tile["x1"], tile["y0"],
            tile["x1"], tile["y1"], tile["x0"], tile["y1"],
        )
        for tile in tiles
    )
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, drawing + b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n"
    )
    with pdfplumber.open(BytesIO(data)) as pdf:
        page = pdf.pages[1]
        assert len(page.curves) == len(tiles)
        assert not page.lines and not page.rects

    _assert_decorated_pdf_keeps_electronic_text(data, local_ocr)


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
@pytest.mark.parametrize(
    "drawing",
    [
        pytest.param(
            b"24 115 m 24 125 l 24 125 l 24 115 l h f\n"
            b"84 85 m 84 95 l 84 95 l 84 85 l h f\n",
            id="explicitly-closed-zero-width-paths",
        ),
        pytest.param(
            b"24 115 m 24 115 l 24 125 l 24 125 l f\n"
            b"84 85 m 84 85 l 84 95 l 84 95 l f\n",
            id="implicitly-closed-zero-width-paths",
        ),
        pytest.param(b"24 115 0 10 re f\n84 85 0 10 re f\n", id="zero-width-rectangles"),
        pytest.param(b"24 115 10 0 re f\n84 85 10 0 re f\n", id="zero-height-rectangles"),
        pytest.param(b"24 115 m 29 120 l 34 125 l h f\n", id="collinear-diagonal"),
        pytest.param(b"24 115 m 24 125 l f\n84 85 m 84 95 l f\n", id="two-point-lines"),
        pytest.param(b"24 115 m f\n84 85 m f\n", id="move-only-paths"),
    ],
)
def test_r15_a2_pdf_keeps_electronic_text_with_degenerate_fill(drawing, local_ocr):
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, drawing + b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n"
    )

    _assert_decorated_pdf_keeps_electronic_text(data, local_ocr)


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
@pytest.mark.parametrize(
    "drawing",
    [
        pytest.param(b"24 115 m 24 125 l S\n84 85 m 84 95 l B\n", id="stroked-lines"),
        pytest.param(b"24 115 0 10 re S\n84 85 0 10 re B\n", id="stroked-zero-width-rectangles"),
        pytest.param(b"24 115 0.4 1 re f\n84 85 0.4 1 re f\n", id="narrow-filled-rectangles"),
    ],
)
def test_r15_a2_pdf_still_requires_ocr_for_visible_short_shapes(drawing, local_ocr):
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, drawing + b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n"
    )
    _assert_pdf_requires_ocr_on_second_page(data, local_ocr)


def _assert_pdf_requires_ocr_on_second_page(
    data: bytes, local_ocr: RapidOcrFallback | None
) -> None:
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=local_ocr
    )
    with patch.object(
        RapidOcrFallback,
        "recognize_pil_image",
        autospec=True,
        side_effect=RapidOcrFallback.recognize_pil_image,
    ) as recognize_page:
        (document,) = dispatcher.parse_batch([("electronic.pdf", data)])

    if local_ocr is None:
        assert document.status is DocumentStatus.FAILED, document.raw_text
        assert document.extraction_channel is ExtractionChannel.FAILED
        assert document.error_message == "第 2 页为扫描图像页，未配置本地 OCR 引擎"
        recognize_page.assert_not_called()
    else:
        assert document.status is DocumentStatus.PARSED, document.error_message
        assert document.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
        recognize_page.assert_called_once()
        assert recognize_page.call_args.kwargs["page"] == 2
        assert document.raw_text.endswith("[Page 2 | OCR]\n[Page 2]\nUSD 245.00"), document.raw_text


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
@pytest.mark.parametrize(
    "drawing",
    [
        pytest.param(b"24 115 m f\n", id="move-only"),
        pytest.param(b"24 115 m 24 115 l 24 125 l 24 125 l f\n", id="zero-width-fill"),
    ],
)
def test_r16_f3_pdf_keeps_an_empty_page_with_invisible_paths(drawing, local_ocr):
    data = _two_page_pdf(b"BT /F1 12 Tf 24 160 Td (Invoice total USD 245.00) Tj ET\n", drawing)

    _assert_decorated_pdf_keeps_electronic_text(
        data,
        local_ocr,
        expected_suffix="[Page 1 | Text]\nInvoice total USD 245.00\n\n[Page 2 | Text]",
    )


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
@pytest.mark.parametrize(
    "drawing",
    [
        pytest.param(b"24 115 m 29 120 34 125 39 130 c f\n", id="c"),
        pytest.param(b"24 115 m 34 125 39 130 v h f\n", id="v"),
        pytest.param(b"24 115 m 29 120 39 130 y f\n", id="y"),
    ],
)
def test_r16_f3_pdf_keeps_electronic_text_with_collinear_bezier_fill(drawing, local_ocr):
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, drawing + b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n"
    )

    _assert_decorated_pdf_keeps_electronic_text(data, local_ocr)


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
@pytest.mark.parametrize(
    "drawing",
    [
        pytest.param(b"24 115 m 29 120 34 125 39 130 c S\n", id="stroked-collinear-bezier"),
        pytest.param(b"24 115 m 24 135 39 135 39 115 c h f\n", id="filled-noncollinear-bezier"),
    ],
)
def test_r16_f3_pdf_still_requires_ocr_for_visible_bezier_paths(drawing, local_ocr):
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, drawing + b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n"
    )

    _assert_pdf_requires_ocr_on_second_page(data, local_ocr)


_R17_BEZIER_ARC = b"24 115 m 24 135 39 135 39 115 c"
_R17_RETRACING_ARC = _R17_BEZIER_ARC + b" 39 135 24 135 24 115 c"


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
@pytest.mark.parametrize(
    "drawing",
    [
        pytest.param(_R17_RETRACING_ARC + b" f\n", id="retracing-fill"),
        pytest.param(b"1 1 1 rg 0 0 300 200 re f\n", id="white-page"),
        pytest.param(
            b"q 0 0 10 10 re W n " + _R17_BEZIER_ARC + b" h f Q\n", id="clipped-arc"
        ),
    ],
)
def test_r17_a1_pdf_retains_the_invoice_before_a_blank_page(drawing, local_ocr):
    data = _two_page_pdf(b"BT /F1 12 Tf 24 160 Td (Invoice total USD 245.00) Tj ET\n", drawing)

    _assert_decorated_pdf_keeps_electronic_text(
        data,
        local_ocr,
        expected_suffix="[Page 1 | Text]\nInvoice total USD 245.00\n\n[Page 2 | Text]",
    )


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
@pytest.mark.parametrize(
    "drawing",
    [
        pytest.param(_R17_RETRACING_ARC + b" h f\n", id="c"),
        pytest.param(b"24 115 m 39 135 39 115 v 39 135 24 115 y f\n", id="v-y"),
        pytest.param(
            b"14 115 m 24 115 l 24 135 39 135 39 115 c 39 135 24 135 24 115 c f\n",
            id="implicit-closing-edge",
        ),
    ],
)
def test_r17_a1_pdf_keeps_electronic_text_with_retracing_bezier_fill(drawing, local_ocr):
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, drawing + b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n"
    )

    _assert_decorated_pdf_keeps_electronic_text(data, local_ocr)


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
@pytest.mark.parametrize(
    "drawing",
    [
        pytest.param(_R17_BEZIER_ARC + b" h f\n", id="filled-arc"),
        pytest.param(_R17_RETRACING_ARC + b" S\n", id="stroked-retracing-arc"),
    ],
)
def test_r17_a1_pdf_still_uses_ocr_for_visible_beziers(drawing, local_ocr):
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, drawing + b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n"
    )

    _assert_pdf_requires_ocr_on_second_page(data, local_ocr)


def test_r17_a1_pdf_renders_the_visible_textless_page_once():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=RecordingLocalOcr()
    )
    data = _two_page_pdf(_PAYMENT_TERMS_PAGE, _R17_BEZIER_ARC + b" h f\n")

    with patch.object(
        pdfplumber.page.Page, "to_image", autospec=True, side_effect=pdfplumber.page.Page.to_image
    ) as render_page:
        (document,) = dispatcher.parse_batch([("scan.pdf", data)])

    assert document.status is DocumentStatus.PARSED, document.error_message
    assert document.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert document.raw_text.endswith("[Page 2 | OCR]\nFREIGHT 187.65"), document.raw_text
    render_page.assert_called_once()
    assert render_page.call_args.args[0].page_number == 2
    assert render_page.call_args.kwargs["resolution"] == 200


def _r18_bitmap_page(row_hex: bytes) -> bytes:
    return (
        b"q 300 0 0 200 0 0 cm\nBI /W 8 /H 8 /CS /G /BPC 1 /F /AHx ID\n"
        b"%s>\nEI\nQ\n" % (row_hex * 8)
    )


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
def test_r18_f1_pdf_keeps_hidden_page_number_on_white_bitmap(local_ocr):
    data = _two_page_pdf(
        b"BT /F1 12 Tf 24 160 Td (Invoice total USD 245.00) Tj ET\n",
        _r18_bitmap_page(b"FF") + b"BT /F1 12 Tf 3 Tr 24 20 Td (2) Tj ET\n",
    )

    _assert_decorated_pdf_keeps_electronic_text(
        data,
        local_ocr,
        expected_suffix="[Page 1 | Text]\nInvoice total USD 245.00\n\n[Page 2 | Text]\n2",
    )


@pytest.mark.parametrize(
    ("local_ocr", "expected_calls", "expected_error"),
    [
        pytest.param(None, 0, "第 2 页为扫描图像页，未配置本地 OCR 引擎", id="no-ocr"),
        pytest.param(RapidOcrFallback(), 1, "无法读取 PDF 页面或识别扫描内容", id="rapidocr"),
    ],
)
def test_r18_f1_pdf_sends_solid_black_bitmap_to_ocr(local_ocr, expected_calls, expected_error):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    data = _two_page_pdf(_PAYMENT_TERMS_PAGE, _r18_bitmap_page(b"00"))
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=local_ocr
    )
    with patch.object(
        RapidOcrFallback,
        "recognize_pil_image",
        autospec=True,
        side_effect=RapidOcrFallback.recognize_pil_image,
    ) as recognize_page:
        (document,) = dispatcher.parse_batch([("black-scan.pdf", data)])

    assert document.status is DocumentStatus.FAILED, document.raw_text
    assert document.extraction_channel is ExtractionChannel.FAILED
    assert document.error_message == expected_error
    assert [call.kwargs["page"] for call in recognize_page.call_args_list] == [2] * expected_calls


_R18_HALF_SPLIT_ARC = _R17_BEZIER_ARC + (
    b" 39 125 35.25 130 31.5 130 c 27.75 130 24 125 24 115 c"
)


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
@pytest.mark.parametrize(
    "drawing",
    [
        pytest.param(_R18_HALF_SPLIT_ARC + b" f\n", id="two-equal-return-segments"),
        pytest.param(
            # The return curve is split at t=1/4 and t=5/8.
            b"24 85 m 24 125 54 125 54 85 c 54 95 52.125 102.5 49.3125 107.5 c "
            b"45.09375 115 38.765625 116.875 33.4921875 113.125 c "
            b"28.21875 109.375 24 100 24 85 c f\n",
            id="three-unequal-return-segments",
        ),
    ],
)
def test_r18_f2_pdf_keeps_text_when_retracing_fill_changes_segmentation(drawing, local_ocr):
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, drawing + b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n"
    )

    _assert_decorated_pdf_keeps_electronic_text(data, local_ocr)


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
@pytest.mark.parametrize(
    "drawing",
    [
        pytest.param(_R18_HALF_SPLIT_ARC + b" S\n", id="stroked-retracing-arc"),
        pytest.param(
            b"84 110 m 74 101 74 119 84 110 c 94 101 94 119 84 110 c f\n",
            id="filled-figure-eight",
        ),
    ],
)
def test_r18_f2_pdf_still_requires_ocr_for_visible_curves(drawing, local_ocr):
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE, drawing + b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n"
    )

    _assert_pdf_requires_ocr_on_second_page(data, local_ocr)


@pytest.mark.parametrize(
    ("points", "expected"),
    [
        pytest.param([(0, 0), (0.2, 0), (0.2, 1), (0, 1)], (0, 0, 0.2, 1), id="narrow"),
        pytest.param([(0, 0), (1, 0), (1, 0.4), (0, 0.4)], (0, 0, 1, 0.4), id="short"),
        pytest.param(
            [(0, 0), (0.4, 0.005), (0.395, 1), (0.005, 0.995), (0.005, 0.005)],
            (0, 0, 0.4, 1),
            id="corner-tolerance",
        ),
        pytest.param([(0, 0), (0, 0), (0, 10), (0, 10)], None, id="zero-width"),
        pytest.param([(0, 0), (0.01, 0), (0.01, 1), (0, 1)], None, id="width-at-tolerance"),
        pytest.param([(0, 0), (0.4, 0), (0.4, 1), (0.02, 1)], None, id="slanted-closing-edge"),
        pytest.param([(0, 0), (0.4, 0), (0, 1), (0.4, 1)], None, id="diagonal-edges"),
    ],
)
def test_r14_3_rectangle_bounds_checks_all_four_sides(points, expected):
    from shipping_billing_agent.parsers.pdf_parser import _rectangle_bounds

    assert _rectangle_bounds(points) == expected


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
def test_r9_f4_pdf_keeps_electronic_text_with_implicitly_closed_bars(local_ocr):
    data = _two_page_pdf(
        _PAYMENT_TERMS_PAGE,
        b"24 120 m 264 120 l 264 124 l 24 124 l f\n"
        b"144 86 m 204 86 l 264 86 l 264 90 l 144 90 l 24 90 l 24 86 l f\n"
        b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n",
    )
    with BytesIO(data) as stream, pdfplumber.open(stream) as pdf:
        page = pdf.pages[1]
        assert len(page.curves) == 2
        assert not page.rects

    _assert_decorated_pdf_keeps_electronic_text(data, local_ocr)


def test_pdf_without_local_ocr_reads_the_complete_short_text_page():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    data = _two_page_pdf(
        b"BT /F1 12 Tf 24 160 Td (Payment terms: net thirty days.) Tj ET\n",
        b"BT /F1 12 Tf 24 160 Td (USD 245.00) Tj ET\n",
    )
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=None
    )
    (document,) = dispatcher.parse_batch([("electronic.pdf", data)])

    assert document.status is DocumentStatus.PARSED, document.error_message
    assert document.extraction_channel is ExtractionChannel.LOCAL_PDF_TEXT_FALLBACK
    assert document.raw_text == (
        "[Page 1 | Text]\nPayment terms: net thirty days.\n\n[Page 2 | Text]\nUSD 245.00"
    )


@pytest.mark.parametrize("local_ocr", [None, RapidOcrFallback()], ids=["no-ocr", "rapidocr"])
@pytest.mark.parametrize(
    "top_and_left",
    [
        pytest.param(b"24 96 m 24 152 l S\n24 152 m 264 152 l S\n", id="separate-borders"),
        pytest.param(b"24 96 m 24 152 l 264 152 l S\n", id="r5-f4-l-shaped-border"),
    ],
)
def test_pdf_keeps_the_sparse_electronic_table_page_as_text(local_ocr, top_and_left):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    data = _two_page_pdf(
        b"BT /F1 12 Tf 24 160 Td (Payment terms: net thirty days.) Tj ET\n",
        top_and_left + b"72 96 m 72 152 l S\n120 96 m 120 152 l S\n"
        b"168 96 m 168 152 l S\n264 96 m 264 152 l S\n"
        b"24 96 m 264 96 l S\n24 124 m 264 124 l S\n"
        b"BT /F1 12 Tf 30 134 Td (Fee) Tj ET\n"
        b"BT /F1 12 Tf 176 106 Td (USD 245.00) Tj ET\n",
    )
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=local_ocr
    )
    (document,) = dispatcher.parse_batch([("electronic.pdf", data)])

    assert document.status is DocumentStatus.PARSED, document.error_message
    assert document.raw_text.endswith("[Page 2 | Text]\nFee\nUSD 245.00"), document.raw_text
    assert document.extraction_channel is ExtractionChannel.LOCAL_PDF_TEXT_FALLBACK
    (table,) = document.tables
    assert table.source == "[Page 2 | Text]"
    assert table.rows == [["Fee", "", "", ""], ["", "", "", "USD 245.00"]]


def test_pdf_reads_vector_fee_and_page_number_with_real_ocr():
    from shipping_billing_agent.parsers.pdf_parser import parse_pdf

    text, _, channel = parse_pdf(_vector_fee_pdf(), local_ocr=RapidOcrFallback())

    first_page, source, second_page = text.partition("[Page 2 | OCR]\n")
    assert source, text
    assert first_page.strip() == "[Page 1 | Text]\nPayment terms: net thirty days."
    assert "FREIGHT USD 310.42" in second_page, second_page
    assert "2" in second_page.splitlines(), second_page
    assert channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK


def test_pdf_without_local_ocr_fails_at_the_vector_fee_page():
    from shipping_billing_agent.parsers.pdf_parser import PdfParseError, parse_pdf

    with pytest.raises(PdfParseError, match="^第 2 页为扫描图像页，未配置本地 OCR 引擎$"):
        parse_pdf(_vector_fee_pdf(), local_ocr=None)


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


@pytest.mark.parametrize(
    "html_body",
    [
        pytest.param(
            '<article><p>Body</p><footer>Balance due USD 310.42 <img src="cid:scan"></footer>'
            "</article>",
            id="unmarked-footer",
        ),
        pytest.param(
            '<div class="signature"><figure>'
            '<img src="cid:scan" alt="Scanned invoice with carrier logo">'
            "<figcaption>Invoice scan</figcaption></figure></div>",
            id="a2a-invoice-caption-with-logo-in-alt",
        ),
    ],
)
def test_eml_reads_bill_image_in_a_footer_or_signature(html_body):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    message = _mail("Body")
    message.add_alternative(html_body, subtype="html")
    message.get_payload()[-1].add_related(
        _png_with_text("USD 310.42"),
        maintype="image",
        subtype="png",
        cid="<scan>",
        filename="scan.png",
    )
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=RapidOcrFallback()
    )

    _, _, scan = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    assert scan.sanitized_filename == "scan.png"
    assert scan.status is DocumentStatus.PARSED, scan.error_message
    assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert "USD 310.42" in scan.raw_text, scan.raw_text


def test_eml_keeps_the_body_scan_before_the_next_signature_separator():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    message = _mail("Charges attached")
    message.add_alternative(
        '<body><div>Charges attached<hr><img src="cid:scan"></div>'
        '<hr><div>Best regards<br>ACME<br>Tel: 123456<img src="cid:logo"></div></body>',
        subtype="html",
    )
    with Image.new("RGB", (460, 140), "white") as image, BytesIO() as buffer:
        font = ImageFont.load_default(size=58)
        ImageDraw.Draw(image).text((24, 28), "USD 310.42", font=font, fill="black")
        image.save(buffer, format="PNG")
        scan_data = buffer.getvalue()
    html = message.get_payload()[-1]
    html.add_related(scan_data, maintype="image", subtype="png", cid="<scan>", filename="scan.png")
    html.add_related(
        b"synthetic signature logo",
        maintype="image",
        subtype="png",
        cid="<logo>",
        filename="logo.png",
    )
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=RapidOcrFallback()
    )

    _, _, scan, logo = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    assert scan.sanitized_filename == "scan.png"
    assert scan.status is DocumentStatus.PARSED, scan.error_message
    assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert "USD 310.42" in scan.raw_text, scan.raw_text
    assert logo.sanitized_filename == "logo.png"
    assert logo.status is DocumentStatus.SKIPPED_NOISE
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE


@pytest.mark.parametrize(
    "scan_block",
    [
        '<div><figure><img src="cid:scan"><figcaption>Invoice scan</figcaption></figure></div>',
        '<div><img src="cid:scan"></div>',
    ],
    ids=["invoice-figure", "unlabelled-scan"],
)
def test_r5_f3_eml_keeps_the_footer_scan_and_skips_the_signature_logo(scan_block):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    message = _mail("Charges attached")
    message.add_alternative(
        f'<footer>{scan_block}<div>Best regards<br>ACME<br>Tel: 123456'
        '<img src="cid:logo"></div></footer>',
        subtype="html",
    )
    with Image.new("RGB", (460, 140), "white") as image, BytesIO() as buffer:
        font = ImageFont.load_default(size=58)
        ImageDraw.Draw(image).text((24, 28), "USD 310.42", font=font, fill="black")
        image.save(buffer, format="PNG")
        scan_data = buffer.getvalue()
    html = message.get_payload()[-1]
    html.add_related(scan_data, maintype="image", subtype="png", cid="<scan>", filename="scan.png")
    html.add_related(
        b"synthetic signature logo",
        maintype="image",
        subtype="png",
        cid="<logo>",
        filename="logo.png",
    )
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=RapidOcrFallback()
    )

    _, _, scan, logo = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    assert scan.sanitized_filename == "scan.png"
    assert scan.status is DocumentStatus.PARSED, scan.error_message
    assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert "USD 310.42" in scan.raw_text, scan.raw_text
    assert logo.sanitized_filename == "logo.png"
    assert logo.status is DocumentStatus.SKIPPED_NOISE
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE


def _png_with_text(text: str) -> bytes:
    with Image.new("RGB", (460, 140), "white") as image, BytesIO() as buffer:
        font = ImageFont.load_default(size=58)
        ImageDraw.Draw(image).text((24, 28), text, font=font, fill="black")
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def _mail_with_inline_png(html_body: str, *, image_name: str, text: str) -> bytes:
    message = _mail("Body")
    message.add_alternative(html_body, subtype="html")
    message.get_payload()[-1].add_related(
        _png_with_text(text),
        maintype="image",
        subtype="png",
        cid=f"<{image_name}>",
        filename=f"{image_name}.png",
    )
    return message.as_bytes()


@pytest.fixture
def r8_eml_ocr():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=RapidOcrFallback()
    )
    with patch.object(
        RapidOcrFallback,
        "recognize_image_bytes",
        autospec=True,
        side_effect=RapidOcrFallback.recognize_image_bytes,
    ) as recognize_image:
        yield dispatcher, recognize_image


def test_r8_2_eml_reads_scan_with_a_caption_before_the_signoff(r8_eml_ocr):
    data = _mail_with_inline_png(
        '<footer><div><p>Invoice scan</p><div><img src="cid:scan"></div>'
        '<p>Best regards<br>ACME<br>Tel: 123456</p></div></footer>',
        image_name="scan",
        text="USD 310.42",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    assert scan.sanitized_filename == "scan.png"
    assert scan.status is DocumentStatus.PARSED, scan.error_message
    assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert "USD 310.42" in scan.raw_text, scan.raw_text
    recognize_image.assert_called_once()


@pytest.mark.parametrize("label", ["Ocean freight USD310.42", "Ocean freight CAD 310.42"])
def test_r8_3_eml_reads_images_with_amount_labels_inside_a_signature(label, r8_eml_ocr):
    data = _mail_with_inline_png(
        f'<div class="signature"><img src="cid:scan" alt="{label}"></div>',
        image_name="scan",
        text="USD 310.42",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    assert scan.sanitized_filename == "scan.png"
    assert scan.status is DocumentStatus.PARSED, scan.error_message
    assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert "USD 310.42" in scan.raw_text, scan.raw_text
    recognize_image.assert_called_once()


@pytest.mark.parametrize("label", ["Billund Freight logo", "ACME Freight logo (USD)"])
def test_r8_3_eml_skips_logos_with_company_names_or_bare_currency_labels(label, r8_eml_ocr):
    data = _mail_with_inline_png(
        f'<div class="signature"><img src="cid:logo" alt="{label}"></div>',
        image_name="logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    assert logo.sanitized_filename == "logo.png"
    assert logo.status is DocumentStatus.SKIPPED_NOISE, logo.raw_text
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE
    recognize_image.assert_not_called()


@pytest.mark.parametrize(
    "image_slot",
    ['<img src="cid:logo">', '<div><img src="cid:logo"></div>'],
    ids=["direct-logo", "wrapped-logo"],
)
def test_r8_6_eml_skips_logos_after_a_wrapped_paragraph_signoff(image_slot, r8_eml_ocr):
    data = _mail_with_inline_png(
        f"<footer><div><p>Best regards<br>ACME<br>Tel: 123456</p></div>{image_slot}</footer>",
        image_name="logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    assert logo.sanitized_filename == "logo.png"
    assert logo.status is DocumentStatus.SKIPPED_NOISE, logo.raw_text
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE
    recognize_image.assert_not_called()


def test_r9_f1_eml_reads_a_scan_with_a_caption_outside_the_signoff_wrapper(r8_eml_ocr):
    data = _mail_with_inline_png(
        '<footer><p>Invoice scan</p><div><div><img src="cid:scan"></div>'
        '<div><p>Best regards<br>ACME<br>Tel: 123456</p></div></div></footer>',
        image_name="scan",
        text="USD 310.42",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    assert scan.sanitized_filename == "scan.png"
    assert scan.status is DocumentStatus.PARSED, scan.error_message
    assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert "USD 310.42" in scan.raw_text, scan.raw_text
    recognize_image.assert_called_once()


def test_r10_f1_wrapped_previous_image_and_caption(r8_eml_ocr):
    message = _mail("Body")
    message.add_alternative(
        '<footer><div><img src="cid:bill1"><p>Invoice scan</p></div>'
        '<div><div><img src="cid:scan"></div>'
        '<div><p>Best regards<br>ACME<br>Tel: 123456</p></div></div></footer>',
        subtype="html",
    )
    amount = "USD 310.42"
    image = _png_with_text(amount)
    html = message.get_payload()[-1]
    html.add_related(image, maintype="image", subtype="png", cid="<bill1>", filename="bill1.png")
    html.add_related(image, maintype="image", subtype="png", cid="<scan>", filename="scan.png")
    dispatcher, recognize_image = r8_eml_ocr

    _, _, bill1, scan = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    assert bill1.sanitized_filename == "bill1.png"
    assert scan.sanitized_filename == "scan.png"
    assert bill1.status is DocumentStatus.PARSED, bill1.error_message
    assert scan.status is DocumentStatus.PARSED, scan.error_message
    assert bill1.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert amount in bill1.raw_text, bill1.raw_text
    assert amount in scan.raw_text, scan.raw_text
    assert recognize_image.call_count == 2


def test_r10_f1_hr_before_footer_caption(r8_eml_ocr):
    dispatcher, recognize_image = r8_eml_ocr
    logo_mail = _mail_with_inline_png(
        '<footer><hr><div>ACME<br>Tel: 123456</div><div><img src="cid:logo"></div></footer>',
        image_name="logo",
        text="ACME",
    )

    _, _, logo = dispatcher.parse_batch([("contact-only.eml", logo_mail)])

    assert logo.sanitized_filename == "logo.png"
    assert logo.status is DocumentStatus.SKIPPED_NOISE, logo.raw_text
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE
    recognize_image.assert_not_called()

    scan_mail = _mail_with_inline_png(
        '<footer><hr><p>Invoice scan</p><div><div><img src="cid:scan"></div>'
        '<div><p>Best regards<br>ACME<br>Tel: 123456</p></div></div></footer>',
        image_name="scan",
        text="USD 310.42",
    )

    _, _, scan = dispatcher.parse_batch([("mail.eml", scan_mail)])

    assert scan.sanitized_filename == "scan.png"
    assert scan.status is DocumentStatus.PARSED, scan.error_message
    assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert "USD 310.42" in scan.raw_text, scan.raw_text
    recognize_image.assert_called_once()


def test_r11_f1_shared_outer_div_after_hr_caption(r8_eml_ocr):
    amount = "USD 310.42"
    data = _mail_with_inline_png(
        '<footer><hr><p>Invoice scan</p><div><div><img src="cid:scan"></div>'
        'Best regards<br>ACME<br>Tel: 123456</div></footer>',
        image_name="scan",
        text=amount,
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    assert scan.sanitized_filename == "scan.png"
    assert (scan.status, recognize_image.call_count) == (
        DocumentStatus.PARSED,
        1,
    ), scan.error_message
    assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert amount in scan.raw_text, scan.raw_text


def test_r11_f1_segmented_contact_paragraphs_after_hr(r8_eml_ocr):
    data = _mail_with_inline_png(
        '<footer><hr><div><p>ACME</p><p>Tel: 123456</p></div>'
        '<div><img src="cid:logo"></div></footer>',
        image_name="logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    assert logo.sanitized_filename == "logo.png"
    assert (logo.status, recognize_image.call_count) == (
        DocumentStatus.SKIPPED_NOISE,
        0,
    ), logo.raw_text
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE


def test_r12_1_split_signoff_span_keeps_preceding_invoice_scan(r8_eml_ocr):
    amount = "USD 310.42"
    data = _mail_with_inline_png(
        '<footer><hr><p>Invoice scan</p><div><div><img src="cid:scan"></div>'
        'Best <span>regards</span><br>ACME<br>Tel: 123456</div></footer>',
        image_name="scan",
        text=amount,
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    assert scan.sanitized_filename == "scan.png"
    assert (scan.status, recognize_image.call_count) == (
        DocumentStatus.PARSED,
        1,
    ), scan.error_message
    assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert amount in scan.raw_text, scan.raw_text


def test_r12_2_hr_inside_segmented_contact_div_skips_logo(r8_eml_ocr):
    data = _mail_with_inline_png(
        '<footer><div><hr><p>ACME</p><p>Tel: 123456</p></div>'
        '<div><img src="cid:logo"></div></footer>',
        image_name="logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    assert logo.sanitized_filename == "logo.png"
    assert (logo.status, recognize_image.call_count) == (
        DocumentStatus.SKIPPED_NOISE,
        0,
    ), logo.raw_text
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE


def test_r13_1_inline_span_caption_before_br_keeps_scan(r8_eml_ocr):
    amount = "USD 310.42"
    data = _mail_with_inline_png(
        '<footer><div>Invoice <span>scan</span><br><img src="cid:scan">'
        '<br>Best regards<br>ACME<br>Tel: 123456</div></footer>',
        image_name="scan",
        text=amount,
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    assert scan.sanitized_filename == "scan.png"
    assert (scan.status, recognize_image.call_count) == (
        DocumentStatus.PARSED,
        1,
    ), scan.error_message
    assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert amount in scan.raw_text, scan.raw_text


def test_r13_2_nested_div_around_hr_contact_skips_logo(r8_eml_ocr):
    data = _mail_with_inline_png(
        '<footer><div><div><hr><p>ACME</p><p>Tel: 123456</p></div></div>'
        '<div><img src="cid:logo"></div></footer>',
        image_name="logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    assert logo.sanitized_filename == "logo.png"
    assert (logo.status, recognize_image.call_count) == (
        DocumentStatus.SKIPPED_NOISE,
        0,
    ), logo.raw_text
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE


def _assert_inline_image_ocr(document, recognize_image, *, expected_text: str | None) -> None:
    if expected_text is None:
        assert (document.status, recognize_image.call_count) == (
            DocumentStatus.SKIPPED_NOISE,
            0,
        ), document.raw_text
        assert document.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE
    else:
        assert (document.status, recognize_image.call_count) == (
            DocumentStatus.PARSED,
            1,
        ), document.error_message
        assert document.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
        assert expected_text in document.raw_text, document.raw_text


@pytest.mark.parametrize(
    ("caption", "expect_ocr"),
    [
        pytest.param("Invoice <output>scan</output>", True, id="output"),
        pytest.param("Invoice <ruby>scan<rp>(</rp><rt>scan</rt><rp>)</rp></ruby>", True, id="ruby"),
        pytest.param('Invoice <div style="display:inline">scan</div>', True, id="inline-div"),
        pytest.param('<div style="display:inline">Invoice</div> scan', True, id="inline-prefix"),
        pytest.param(
            'Invoice <div style="color:black; DISPLAY : Inline-Block ! IMPORTANT">scan</div>',
            True,
            id="inline-block-important",
        ),
        pytest.param('Invoice <div style="display:inline-flex">scan</div>', True, id="inline-flex"),
        pytest.param('Invoice <span style="display:block">Company logo</span>', False, id="block"),
        pytest.param(
            'Invoice <span style="display:grid !important; display:inline">Company logo</span>',
            False,
            id="grid-important",
        ),
        pytest.param(
            'Invoice <span style="display:inline; display:table-cell">Company logo</span>',
            False,
            id="table-cell",
        ),
    ],
)
def test_r14_1_display_controls_the_visual_caption_line(caption, expect_ocr, r8_eml_ocr):
    amount = "USD 310.42"
    data = _mail_with_inline_png(
        f'<footer><div>{caption}<br><img src="cid:scan">'
        '<br>Best regards<br>ACME<br>Tel: 123456</div></footer>',
        image_name="scan",
        text=amount if expect_ocr else "ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    assert scan.sanitized_filename == "scan.png"
    _assert_inline_image_ocr(scan, recognize_image, expected_text=amount if expect_ocr else None)


@pytest.mark.parametrize(
    ("layout", "expect_ocr"),
    [
        pytest.param(
            '<div id="wrap"><div><hr>{contact}</div>\n<!-- spacer --></div><div>{image}</div>',
            False,
            id="wrapped-contact",
        ),
        pytest.param(
            "<div>" * 8 + "<div><hr>{contact}</div>" + "</div>" * 8 + "{image}",
            False,
            id="deeply-wrapped-contact",
        ),
        pytest.param(
            '<div id="wrap"><div><hr>{contact}{image}</div></div>', False, id="image-in-box"
        ),
        pytest.param(
            "<div><div>{contact}<hr>General update</div></div>{image}",
            True,
            id="contact-before-hr",
        ),
        pytest.param(
            "<div><div><hr>{contact}Invoice scan</div></div>{image}", True, id="bill-caption"
        ),
        pytest.param(
            "<div><div><hr>{contact}</div><span>ACME Logistics</span></div>{image}",
            True,
            id="text-outside-box-even-if-identical",
        ),
        pytest.param(
            '<div><div><hr>{contact}</div><img src="other.png"></div>{image}',
            True,
            id="intervening-image",
        ),
        pytest.param(
            "<div><div><hr>{contact}</div><figure></figure></div>{image}",
            True,
            id="intervening-figure",
        ),
    ],
)
def test_r14_2_contact_after_a_nested_hr_skips_only_the_adjacent_signature_image(
    layout, expect_ocr, r8_eml_ocr
):
    html = layout.format(
        contact="ACME Logistics<br>Tel: +86 21 5555 6666<br>", image='<img src="cid:sig">'
    )
    data = _mail_with_inline_png(f"<footer>{html}</footer>", image_name="sig", text="ACME")
    dispatcher, recognize_image = r8_eml_ocr

    _, _, image = dispatcher.parse_batch([("mail.eml", data)])

    assert image.sanitized_filename == "sig.png"
    _assert_inline_image_ocr(image, recognize_image, expected_text="ACME" if expect_ocr else None)


def test_r9_f2_eml_reads_a_negative_amount_and_skips_bare_currency_logos(r8_eml_ocr):
    dispatcher, recognize_image = r8_eml_ocr
    logo_labels = {
        "hyphen.eml": "ACME Freight logo - USD",
        "parentheses.eml": "ACME Freight logo (USD)",
    }
    logo_mails = [
        (
            filename,
            _mail_with_inline_png(
                f'<div class="signature"><img src="cid:logo" alt="{label}"></div>',
                image_name="logo",
                text="ACME",
            ),
        )
        for filename, label in logo_labels.items()
    ]

    _, _, hyphen_logo, _, _, parentheses_logo = dispatcher.parse_batch(logo_mails)

    assert hyphen_logo.status is DocumentStatus.SKIPPED_NOISE, hyphen_logo.raw_text
    assert hyphen_logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE
    assert parentheses_logo.status is DocumentStatus.SKIPPED_NOISE, parentheses_logo.raw_text
    assert parentheses_logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE
    recognize_image.assert_not_called()

    data = _mail_with_inline_png(
        '<div class="signature"><img src="cid:scan" alt="Ocean freight USD -310.42"></div>',
        image_name="scan",
        text="USD -310.42",
    )

    _, _, scan = dispatcher.parse_batch([("credit.eml", data)])

    assert scan.sanitized_filename == "scan.png"
    assert scan.status is DocumentStatus.PARSED, scan.error_message
    assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert "-310.42" in scan.raw_text, scan.raw_text
    recognize_image.assert_called_once()


def test_r9_f5_eml_skips_a_logo_before_signoff_with_an_empty_sibling(r8_eml_ocr):
    message = _mail("Body")
    message.add_alternative(
        '<footer><div><figure><img src="cid:scan"><figcaption>Invoice scan</figcaption>'
        '</figure></div><div><span></span><div><img src="cid:logo"></div>'
        '<div>Best regards<br>ACME<br>Tel: 123456</div></div></footer>',
        subtype="html",
    )
    html = message.get_payload()[-1]
    html.add_related(
        _png_with_text("USD 310.42"), maintype="image", subtype="png",
        cid="<scan>", filename="scan.png",
    )
    html.add_related(
        _png_with_text("ACME"), maintype="image", subtype="png", cid="<logo>", filename="logo.png"
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan, logo = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    assert scan.sanitized_filename == "scan.png"
    assert scan.status is DocumentStatus.PARSED, scan.error_message
    assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert "USD 310.42" in scan.raw_text, scan.raw_text
    assert logo.sanitized_filename == "logo.png"
    assert logo.status is DocumentStatus.SKIPPED_NOISE, logo.raw_text
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE
    recognize_image.assert_called_once()


def test_r9_f5_eml_skips_a_logo_after_signoff_with_an_empty_sibling(r8_eml_ocr):
    data = _mail_with_inline_png(
        '<footer><div><p>Best regards<br>ACME<br>Tel: 123456</p><span></span></div>'
        '<img src="cid:logo"></footer>',
        image_name="logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    assert logo.sanitized_filename == "logo.png"
    assert logo.status is DocumentStatus.SKIPPED_NOISE, logo.raw_text
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE
    recognize_image.assert_not_called()


@pytest.mark.parametrize("empty_span", ["", "<span></span>"], ids=["plain", "empty-span"])
def test_r10_f5_wrapped_earlier_invoice_notice_before_company_logo(empty_span, r8_eml_ocr):
    data = _mail_with_inline_png(
        '<footer><div><p>For invoice inquiries, please contact Accounts.</p>'
        f'<p>Company logo</p>{empty_span}</div><div><div><img src="cid:logo"></div>'
        '<div><p>Best regards<br>ACME<br>Tel: 123456</p></div></div></footer>',
        image_name="logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    assert logo.sanitized_filename == "logo.png"
    assert logo.status is DocumentStatus.SKIPPED_NOISE, logo.raw_text
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE
    recognize_image.assert_not_called()


def test_r11_f5_table_with_explicit_tbody_before_logo(r8_eml_ocr):
    data = _mail_with_inline_png(
        '<footer><table><tbody><tr><td><p>For invoice inquiries, please contact Accounts.</p>'
        '<p>Company logo</p></td></tr></tbody></table><div><div><img src="cid:logo"></div>'
        '<div><p>Best regards<br>ACME<br>Tel: 123456</p></div></div></footer>',
        image_name="logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    assert logo.sanitized_filename == "logo.png"
    assert (logo.status, recognize_image.call_count) == (
        DocumentStatus.SKIPPED_NOISE,
        0,
    ), logo.raw_text
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE


def test_r12_4_shared_footer_table_with_earlier_invoice_notice_skips_logo(r8_eml_ocr):
    data = _mail_with_inline_png(
        '<footer><table><tbody><tr><td><p>For invoice inquiries, please contact Accounts.</p>'
        '<p>Company logo</p></td></tr></tbody></table><div><img src="cid:logo"></div>'
        'Best regards<br>ACME<br>Tel: 123456</footer>',
        image_name="logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    assert logo.sanitized_filename == "logo.png"
    assert (logo.status, recognize_image.call_count) == (
        DocumentStatus.SKIPPED_NOISE,
        0,
    ), logo.raw_text
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE


def test_r13_4_br_separated_invoice_notice_and_company_logo_skips_logo(r8_eml_ocr):
    data = _mail_with_inline_png(
        '<footer><table><tbody><tr><td><p>For invoice inquiries, please contact Accounts.'
        '<br>Company logo</p></td></tr></tbody></table><div><img src="cid:logo"></div>'
        'Best regards<br>ACME<br>Tel: 123456</footer>',
        image_name="logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    assert logo.sanitized_filename == "logo.png"
    assert (logo.status, recognize_image.call_count) == (
        DocumentStatus.SKIPPED_NOISE,
        0,
    ), logo.raw_text
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE


@pytest.mark.parametrize(
    ("caption", "expect_ocr"),
    [
        pytest.param("<pre>Invoice inquiries\nCompany logo</pre>", False, id="pre"),
        pytest.param(
            "<textarea>Invoice inquiries\r\nCompany logo</textarea>", False, id="textarea"
        ),
        pytest.param(
            '<div style=" WHITE-SPACE : pre-line !IMPORTANT">'
            "<span>Invoice inquiries\nCompany logo</span></div>",
            False,
            id="inherited-pre-line",
        ),
        pytest.param(
            '<div style="white-space: break-spaces">Invoice inquiries&#13;'
            "Company <span>logo</span></div>",
            False,
            id="break-spaces-with-lone-cr",
        ),
        pytest.param(
            '<div style="white-space: pre-wrap">Earlier line\n-- \n'
            "Invoice inquiries\nCompany logo</div>",
            False,
            id="pre-wrap-after-signature-separator",
        ),
        pytest.param(
            "<pre>Invoice inquiries&#13;-- &#13;Company logo</pre>",
            False,
            id="separator-with-lone-cr",
        ),
        pytest.param("<pre>Company logo\nInvoice scan</pre>", True, id="bill-on-last-line"),
        pytest.param(
            '<pre>Invoice <span style="white-space: normal">\nscan</span></pre>',
            True,
            id="normal-overrides-pre",
        ),
        pytest.param(
            '<textarea style="white-space: nowrap">Invoice\nscan</textarea>',
            True,
            id="nowrap-overrides-textarea",
        ),
    ],
)
def test_r14_4_preformatted_caption_uses_the_last_visual_line(caption, expect_ocr, r8_eml_ocr):
    amount = "USD 310.42"
    data = _mail_with_inline_png(
        f'<footer><table><tbody><tr><td>{caption}</td></tr></tbody></table>'
        '<div><img src="cid:logo"></div>Best regards<br>ACME<br>Tel: 123456</footer>',
        image_name="logo",
        text=amount if expect_ocr else "ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    assert logo.sanitized_filename == "logo.png"
    _assert_inline_image_ocr(logo, recognize_image, expected_text=amount if expect_ocr else None)


@pytest.mark.parametrize("with_caption", [True, False], ids=["bill-caption", "signature-only"])
@pytest.mark.parametrize(
    ("layout", "caption"),
    [
        pytest.param(
            '<div id="wrap"><div><hr>{contact}{caption}</div></div>{image}',
            "<br>In<b>voice</b> scan",
            id="wrapped-contact",
        ),
        pytest.param(
            "<div><hr>{contact}{caption}</div>{image}",
            "<br>Invoice scan",
            id="adjacent-plain-caption",
        ),
        pytest.param(
            "<div><hr>{contact}{caption}</div>{image}",
            "<br>In<b>voice</b> scan",
            id="adjacent-inline-caption",
        ),
        pytest.param(
            "<div><hr>{contact}{caption}</div>{image}",
            "<br>In<b>voice</b> scan<br>Attached",
            id="earlier-caption-in-contact-box",
        ),
        pytest.param(
            "<div><hr>{contact}{caption}<br>{image}</div>",
            "<br>In<b>voice</b> scan",
            id="inside-contact-box",
        ),
        pytest.param(
            "<div>{image}{caption}<br>Best regards<br>{contact}</div>",
            "<br>In<b>voice</b> scan",
            id="caption-between-image-and-signoff",
        ),
        pytest.param(
            '<div class="signature"><figure>{caption}{image}</figure></div>',
            "<figcaption>In<b>voice</b> scan</figcaption>",
            id="figure-caption",
        ),
    ],
)
def test_r15_a1_eml_keeps_bill_captions_in_signature_layouts(
    layout, caption, with_caption, r8_eml_ocr
):
    expected_text = "USD 310.42" if with_caption else None
    html = layout.format(
        contact="ACME<br>Tel: +86 21 5555 6666",
        caption=caption if with_caption else "",
        image='<img src="cid:scan">',
    )
    data = _mail_with_inline_png(
        f"<footer>{html}</footer>", image_name="scan", text=expected_text or "ACME"
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    assert scan.sanitized_filename == "scan.png"
    _assert_inline_image_ocr(scan, recognize_image, expected_text=expected_text)


@pytest.mark.parametrize(
    ("bill_src", "signature_src", "signature_first"),
    [
        pytest.param("cid:scan", "cid:scan", False, id="shared-cid-bill-first"),
        pytest.param("cid:scan", "cid:scan", True, id="shared-cid-signature-first"),
        pytest.param("cid:scan", "scan.png", False, id="bill-cid-signature-filename"),
        pytest.param(
            "assets/%73can.PNG?size=large", "cid:scan", False, id="bill-filename-signature-cid"
        ),
        pytest.param("scan.png", "scan.png", False, id="shared-filename"),
    ],
)
def test_r15_a3_eml_keeps_an_attachment_with_a_body_reference(
    bill_src, signature_src, signature_first, r8_eml_ocr
):
    bill = f'<figure><img src="{bill_src}"><figcaption>Invoice scan</figcaption></figure>'
    signature = f'<div class="signature"><img src="{signature_src}"></div>'
    html = signature + bill if signature_first else bill + signature
    amount = "USD 310.42"
    data = _mail_with_inline_png(html, image_name="scan", text=amount)
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    assert scan.sanitized_filename == "scan.png"
    _assert_inline_image_ocr(scan, recognize_image, expected_text=amount)


def test_r15_a3_eml_skips_repeated_signature_only_references(r8_eml_ocr):
    data = _mail_with_inline_png(
        '<div class="signature"><img src="cid:logo"><br><img src="cid:logo"></div>',
        image_name="logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    assert logo.sanitized_filename == "logo.png"
    _assert_inline_image_ocr(logo, recognize_image, expected_text=None)


@pytest.mark.parametrize(
    ("ancestors", "caption", "expect_ocr"),
    [
        pytest.param(
            '<body style="white-space:pre-line">{footer}</body>',
            "Invoice inquiries\nCompany logo",
            False,
            id="inherited-body-pre-line",
        ),
        pytest.param(
            '<body style="white-space:pre-line"><div style="white-space:normal">'
            "{footer}</div></body>",
            "Invoice\nscan",
            True,
            id="normal-overrides-body-pre-line",
        ),
        pytest.param(
            '<body style="white-space:pre-line"><div style="white-space:initial">'
            "{footer}</div></body>",
            "Invoice\nscan",
            True,
            id="initial-overrides-body-pre-line",
        ),
        pytest.param(
            "<pre>{footer}</pre>", "Invoice inquiries\nCompany logo", False, id="outer-pre"
        ),
    ],
)
def test_r15_a4_eml_footer_inherits_white_space_from_ancestors(
    ancestors, caption, expect_ocr, r8_eml_ocr
):
    expected_text = "USD 310.42" if expect_ocr else None
    footer = (
        f'<footer><div>{caption}</div><img src="cid:logo">'
        "<br>Best regards<br>ACME<br>Tel: 123456</footer>"
    )
    data = _mail_with_inline_png(
        ancestors.format(footer=footer), image_name="logo", text=expected_text or "ACME"
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    assert logo.sanitized_filename == "logo.png"
    _assert_inline_image_ocr(logo, recognize_image, expected_text=expected_text)


@pytest.mark.parametrize(
    ("caption", "expected_text"),
    [
        pytest.param("Invoice scan", "USD 310.42", id="bill"),
        pytest.param("Company logo", None, id="logo"),
    ],
)
def test_r16_f1_eml_keeps_caption_in_the_signoff_text_node(caption, expected_text, r8_eml_ocr):
    html = (
        '<body style="white-space:pre-line"><footer><div><img src="cid:scan">'
        f"{caption}\nBest regards\nACME\nTel: 123456</div></footer></body>"
    )
    data = _mail_with_inline_png(html, image_name="scan", text=expected_text or "ACME")
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    _assert_inline_image_ocr(scan, recognize_image, expected_text=expected_text)


@pytest.mark.parametrize(
    "layout",
    [
        pytest.param("<div>{contact}<br>{image}</div>", id="inside"),
        pytest.param("<div>{contact}</div>{image}", id="adjacent"),
        pytest.param("<div><div>{contact}</div></div>{image}", id="wrapped-adjacent"),
    ],
)
@pytest.mark.parametrize(
    ("tail", "expected_text"),
    [
        pytest.param("Invoice inquiries<br>Company logo", None, id="logo"),
        pytest.param("Invoice scan<br>Attached", "USD 310.42", id="bill"),
    ],
)
def test_r16_f2_eml_uses_the_same_caption_rule_for_signature_layouts(
    layout, tail, expected_text, r8_eml_ocr
):
    html = layout.format(
        contact=f"<hr>ACME<br>Tel: 123456<br>{tail}", image='<img src="cid:scan">'
    )
    data = _mail_with_inline_png(
        f"<footer>{html}</footer>", image_name="scan", text=expected_text or "ACME"
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    _assert_inline_image_ocr(scan, recognize_image, expected_text=expected_text)


@pytest.mark.parametrize("container", ["footer", "div"])
def test_r16_f4_eml_skips_a_logo_repeated_before_the_signature_tail(container, r8_eml_ocr):
    logo_ref = '<img src="cid:logo">'
    details = "".join(f"<br>Contact line {number}" for number in range(1, 10))
    html = (
        f"<{container}><hr><div>Best regards<br>Tel: 123456<br>{logo_ref}"
        f"{details}<br>Tel: 123456<br>{logo_ref}</div></{container}>"
    )
    data = _mail_with_inline_png(html, image_name="logo", text="ACME")
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    _assert_inline_image_ocr(logo, recognize_image, expected_text=None)


def test_r16_f4_eml_keeps_a_shared_cid_with_an_early_body_reference(r8_eml_ocr):
    scan_ref = '<img src="cid:scan">'
    details = "".join(f"<p>Shipment details {number}</p>" for number in range(1, 10))
    data = _mail_with_inline_png(
        f'<main>{scan_ref}{details}</main><div class="signature">{scan_ref}</div>',
        image_name="scan",
        text="USD 310.42",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    _assert_inline_image_ocr(scan, recognize_image, expected_text="USD 310.42")


@pytest.mark.parametrize(
    ("location", "content_base", "expected_text"),
    [
        pytest.param("https://example.com/signature/scan.png", None, None, id="different-url"),
        pytest.param("/signature/scan.png", None, None, id="root-relative-location"),
        pytest.param(None, None, None, id="no-location"),
        pytest.param("https://example.com/bills/scan.png", None, "USD 310.42", id="same-url"),
        pytest.param(
            "bills/%73can.png", "https://EXAMPLE.COM/mail.eml", "USD 310.42", id="content-base"
        ),
        pytest.param(
            "https://example.com/bills/scan.png?size=large", None, None, id="different-query"
        ),
        pytest.param("https://example.com/Bills/scan.png", None, None, id="different-path-case"),
        pytest.param("https://[invalid/signature/scan.png", None, None, id="malformed-location"),
    ],
)
def test_r16_f5_eml_matches_bill_images_by_location(
    location, content_base, expected_text, r8_eml_ocr
):
    message = _mail("Body")
    message.add_alternative(
        '<figure><img src="https://example.com/bills/scan.png">'
        '<figcaption>Invoice scan</figcaption></figure>'
        '<div class="signature"><img src="cid:logo"></div>',
        subtype="html",
    )
    html_part = message.get_payload()[-1]
    html_part.add_related(
        _png_with_text(expected_text or "ACME"),
        maintype="image",
        subtype="png",
        cid="<logo>",
        filename="scan.png",
    )
    attachment = html_part.get_payload()[-1]
    if location is not None:
        attachment["Content-Location"] = location
    if content_base is not None:
        attachment["Content-Base"] = content_base
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    assert scan.sanitized_filename == "scan.png"
    _assert_inline_image_ocr(scan, recognize_image, expected_text=expected_text)


@pytest.mark.parametrize(
    "bill_src",
    [
        pytest.param("/bills/scan.png", id="root-relative"),
        pytest.param("//example.com/bills/scan.png", id="protocol-relative"),
        pytest.param("https://[invalid/bills/scan.png", id="malformed-url"),
    ],
)
def test_r16_f5_eml_does_not_use_url_basename_for_attachment_matching(bill_src, r8_eml_ocr):
    data = _mail_with_inline_png(
        f'<figure><img src="{bill_src}"><figcaption>Invoice scan</figcaption></figure>'
        '<div class="signature"><img src="cid:scan"></div>',
        image_name="scan",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    _assert_inline_image_ocr(scan, recognize_image, expected_text=None)


@pytest.mark.parametrize("extra_lines", [0, 10], ids=["short", "long"])
def test_r17_a2_eml_keeps_a_body_scan_after_an_ordinary_hr(extra_lines, r8_eml_ocr):
    scan_ref = '<img src="cid:scan">'
    details = "<p>Shipment received</p>" * extra_lines
    data = _mail_with_inline_png(
        f'<main><hr>{scan_ref}{details}</main><div class="signature">{scan_ref}</div>',
        image_name="scan",
        text="USD 310.42",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    _assert_inline_image_ocr(scan, recognize_image, expected_text="USD 310.42")


def test_r17_a2_eml_skips_the_repeated_logo_inside_a_footer_section(r8_eml_ocr):
    logo_ref = '<img src="cid:logo">'
    details = "<p>Contact details</p>" * 10
    data = _mail_with_inline_png(
        f"<footer><hr><section><p>Best regards</p>{logo_ref}{details}"
        f'<p>Tel: 123456</p><div class="signature">{logo_ref}</div></section></footer>',
        image_name="logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    _assert_inline_image_ocr(logo, recognize_image, expected_text=None)


@pytest.mark.parametrize(
    ("base_source", "location"),
    [
        pytest.param("html-element", "https://example.com/bills/scan.png", id="html-base"),
        pytest.param("html-part", "https://example.com/bills/scan.png", id="html-location"),
        pytest.param("html-element", "../bills/scan.png", id="relative-attachment"),
        pytest.param("related-container", "../bills/scan.png", id="inherited-content-base"),
    ],
)
def test_r17_a3_eml_resolves_a_shared_resource_against_its_base(base_source, location, r8_eml_ocr):
    head = (
        '<head><base href="https://example.com/mail/"></head>'
        if base_source == "html-element"
        else ""
    )
    message = _mail("Body")
    message.add_alternative(
        f'{head}<main><img src="/bills/scan.png"></main>'
        '<div class="signature"><img src="cid:scan"></div>',
        subtype="html",
    )
    related = message.get_payload()[-1]
    related.add_related(
        _png_with_text("USD 310.42"),
        maintype="image",
        subtype="png",
        cid="<scan>",
        filename="scan.png",
    )
    html, attachment = related.get_payload()
    attachment["Content-Location"] = location
    if base_source == "html-part":
        html["Content-Location"] = "https://example.com/mail/index.html"
    elif base_source == "related-container":
        related["Content-Base"] = "https://example.com/mail/index.html"
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    _assert_inline_image_ocr(scan, recognize_image, expected_text="USD 310.42")


def test_r17_a3_eml_skips_the_logo_when_a_relative_src_has_an_absolute_base(r8_eml_ocr):
    data = _mail_with_inline_png(
        '<head><base href="https://example.com/mail/"></head>'
        '<main><img src="bills/scan.png"></main><div class="signature"><img src="cid:scan"></div>',
        image_name="scan",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    _assert_inline_image_ocr(logo, recognize_image, expected_text=None)


@pytest.mark.parametrize(
    ("bill_src", "location"),
    [
        pytest.param("/Bills/scan.png", "/bills/scan.png", id="path-case"),
        pytest.param(
            "https://example.com/bills%2Farchive/scan.png",
            "https://example.com/bills/archive/scan.png",
            id="encoded-path-separator",
        ),
    ],
)
def test_r17_a4_eml_preserves_distinct_resource_paths(bill_src, location, r8_eml_ocr):
    message = _mail("Body")
    message.add_alternative(
        f'<main><img src="{bill_src}"></main>'
        '<div class="signature"><img src="cid:logo"></div>',
        subtype="html",
    )
    related = message.get_payload()[-1]
    related.add_related(
        _png_with_text("ACME"), maintype="image", subtype="png", cid="<logo>", filename="scan.png"
    )
    related.get_payload()[-1]["Content-Location"] = location
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    _assert_inline_image_ocr(logo, recognize_image, expected_text=None)


def test_r17_a5_eml_does_not_match_malformed_attachment_location_by_filename(r8_eml_ocr):
    message = _mail("Body")
    message.add_alternative(
        '<main><img src="https://example.com/bills/scan.png"><img src="scan.png"></main>'
        '<div class="signature"><img src="cid:sig"></div>',
        subtype="html",
    )
    related = message.get_payload()[-1]
    for cid, text, location in (
        ("bill", "USD 310.42", "https://example.com/bills/scan.png"),
        ("sig", "ACME", "http://[invalid"),
    ):
        related.add_related(
            _png_with_text(text),
            maintype="image",
            subtype="png",
            cid=f"<{cid}>",
            filename="scan.png",
        )
        related.get_payload()[-1]["Content-Location"] = location
    dispatcher, recognize_image = r8_eml_ocr

    _, _, bill, logo = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    _assert_inline_image_ocr(bill, recognize_image, expected_text="USD 310.42")
    assert logo.status is DocumentStatus.SKIPPED_NOISE
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE


@pytest.mark.parametrize(
    ("caption", "expected_text"),
    [
        pytest.param("Company logo\nFor invoice inquiries, call our office.", None, id="logo"),
        pytest.param("Invoice scan", "USD 310.42", id="first-line"),
        pytest.param("Attached\nInvoice scan", "USD 310.42", id="continuation"),
    ],
)
def test_r17_a6_eml_check_starts_at_the_image_caption(caption, expected_text, r8_eml_ocr):
    data = _mail_with_inline_png(
        '<body style="white-space:pre-line"><footer><div><img src="cid:logo">'
        f"{caption}\nBest regards\nACME\nTel: 123456</div></footer></body>",
        image_name="logo",
        text=expected_text or "ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, image = dispatcher.parse_batch([("mail.eml", data)])

    _assert_inline_image_ocr(image, recognize_image, expected_text=expected_text)


def test_r17_b_linear_caption_scan():
    from shipping_billing_agent.parsers import eml_parser
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=RecordingLocalOcr()
    )
    call_counts = []
    for count in (12, 24):
        html = '<p>Shipment details</p><img src="cid:scan">' * count
        data = _mail_with_inline_png(html, image_name="scan", text="USD 310.42")
        with patch.object(
            eml_parser, "_is_inline_element", wraps=eml_parser._is_inline_element
        ) as is_inline:
            root, body, scan = dispatcher.parse_batch([("mail.eml", data)])

        assert all(document.status is DocumentStatus.PARSED for document in (root, body, scan))
        assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
        call_counts.append(is_inline.call_count)

    small, large = call_counts
    assert 0 < small and large <= 2.2 * small, f"12 images: {small}, 24 images: {large}"


@pytest.mark.parametrize(
    ("layout", "separator", "extra_lines"),
    [
        pytest.param("<main>{}</main>{}", "<hr>", 0, id="main-short"),
        pytest.param("<main>{}</main>{}", "<hr>", 10, id="main-long"),
        pytest.param("<main>{}</main>{}", "<pre>-- \n</pre>", 0, id="main-with-dash"),
        pytest.param("<body>{}{}</body>", "<hr>", 0, id="body-short"),
        pytest.param("<body>{}{}</body>", "<hr>", 10, id="body-long"),
    ],
)
def test_r18_f3_eml_keeps_a_scan_with_a_delivery_contact(
    layout, separator, extra_lines, r8_eml_ocr
):
    scan_ref = '<img src="cid:scan">'
    details = "<p>Shipment received</p>" * extra_lines
    body = f"{separator}{scan_ref}<p>Delivery appointment Tel: 123456</p>{details}"
    data = _mail_with_inline_png(
        layout.format(body, f'<div class="signature">{scan_ref}</div>'),
        image_name="scan",
        text="USD 310.42",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    _assert_inline_image_ocr(scan, recognize_image, expected_text="USD 310.42")


def _add_related_png(
    part: EmailMessage,
    *,
    filename: str,
    text: str,
    cid: str | None = None,
    location: str | None = None,
) -> None:
    part.add_related(
        _png_with_text(text), maintype="image", subtype="png", cid=cid, filename=filename
    )
    if location is not None:
        part.get_payload()[-1]["Content-Location"] = location


def _assert_invoice_and_skipped_logo(bill, logo, recognize_image):
    _assert_inline_image_ocr(bill, recognize_image, expected_text="USD 310.42")
    assert logo.status is DocumentStatus.SKIPPED_NOISE, logo.raw_text
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE


def _related_group_with_scan(base: str, html: str, text: str) -> EmailMessage:
    group = EmailMessage(policy=policy.SMTP)
    group.set_content(f'<head><base href="{base}"></head>{html}', subtype="html")
    _add_related_png(group, filename="scan.png", text=text, location="scan.png")
    return group


def test_r18_f4_eml_uses_only_the_base_from_the_attachments_related_group(r8_eml_ocr):
    message = _mail("Body")
    message.make_mixed()
    message.attach(
        _related_group_with_scan(
            "https://example.com/bills/", '<main><img src="scan.png"></main>', "USD 310.42"
        )
    )
    message.attach(
        _related_group_with_scan(
            "https://example.com/signature/",
            '<div class="signature"><img src="scan.png"></div>',
            "ACME",
        )
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, bill, logo = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    assert bill.sanitized_filename == logo.sanitized_filename == "scan.png"
    _assert_invoice_and_skipped_logo(bill, logo, recognize_image)


@pytest.mark.parametrize(
    ("bill_src", "location", "expected_text"),
    [
        pytest.param(
            "https://example.com/scan.png?id=%73can",
            "https://example.com/scan.png?id=scan",
            "USD 310.42",
            id="unreserved-query-escape",
        ),
        pytest.param("assets%2Fscan.png", None, None, id="encoded-slash"),
        pytest.param("assets%5cscan.png", None, None, id="encoded-backslash"),
        pytest.param(
            "https://Alice@example.com/scan.png",
            "https://alice@example.com/scan.png",
            None,
            id="userinfo-case",
        ),
        pytest.param(
            "https://example.com/scan.png?id=a%26b%3Dc",
            "https://example.com/scan.png?id=a&b=c",
            None,
            id="reserved-query-escapes",
        ),
    ],
)
def test_r18_f5_eml_matches_only_equivalent_locations(
    bill_src, location, expected_text, r8_eml_ocr
):
    message = _mail("Body")
    message.add_alternative(
        f'<main><img src="{bill_src}"></main><div class="signature"><img src="cid:logo"></div>',
        subtype="html",
    )
    _add_related_png(
        message.get_payload()[-1],
        filename="scan.png",
        text=expected_text or "ACME",
        cid="<logo>",
        location=location,
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    _assert_inline_image_ocr(scan, recognize_image, expected_text=expected_text)


def test_r18_f6_eml_empty_content_location_disables_filename_fallback(r8_eml_ocr):
    message = _mail("Body")
    message.add_alternative(
        '<main><img src="scan.png"></main><div class="signature"><img src="cid:logo"></div>',
        subtype="html",
    )
    related = message.get_payload()[-1]
    _add_related_png(related, filename="scan.png", text="USD 310.42")
    _add_related_png(related, filename="scan.png", text="ACME", cid="<logo>", location="")
    dispatcher, recognize_image = r8_eml_ocr

    _, _, bill, logo = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    assert bill.sanitized_filename == logo.sanitized_filename == "scan.png"
    _assert_invoice_and_skipped_logo(bill, logo, recognize_image)


def _mail_with_scan_and_logo(html: str) -> bytes:
    message = _mail("Body")
    message.add_alternative(html, subtype="html")
    related = message.get_payload()[-1]
    _add_related_png(related, filename="scan.png", text="USD 310.42", cid="<scan>")
    _add_related_png(related, filename="logo.png", text="ACME", cid="<logo>")
    return message.as_bytes()


@pytest.mark.parametrize("signoff_layout", ["<p>{}</p>", "{}"], ids=["separate-signoff", "shared"])
def test_r18_f7_eml_keeps_each_trailing_caption_with_its_own_image(signoff_layout, r8_eml_ocr):
    signoff = signoff_layout.format("Best regards<br>ACME<br>Tel: 123456")
    data = _mail_with_scan_and_logo(
        '<footer><div><p><img src="cid:scan">Invoice scan</p>'
        f'<p><img src="cid:logo">Company logo</p>{signoff}</div></footer>'
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, bill, logo = dispatcher.parse_batch([("mail.eml", data)])

    _assert_invoice_and_skipped_logo(bill, logo, recognize_image)


@pytest.mark.parametrize(
    "child_order", ["{scan}{nested}", "{nested}{scan}"], ids=["scan-first", "nested-first"]
)
def test_r18_f8_eml_does_not_inherit_outer_caption_in_nested_figure(child_order, r8_eml_ocr):
    nested = (
        '<figure><img src="cid:logo">'
        "<figcaption>Company logo<br>Best regards<br>ACME<br>Tel: 123456</figcaption></figure>"
    )
    images = child_order.format(scan='<img src="cid:scan">', nested=nested)
    data = _mail_with_scan_and_logo(
        f"<footer><figure><figcaption>Invoice scan</figcaption>{images}</figure></footer>"
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, bill, logo = dispatcher.parse_batch([("mail.eml", data)])

    _assert_invoice_and_skipped_logo(bill, logo, recognize_image)


def test_r18_b1_nested_sections_linear_scan(r8_eml_ocr):
    from shipping_billing_agent.parsers import eml_parser

    dispatcher, recognize_image = r8_eml_ocr
    call_counts = []
    for count in (12, 24, 48):
        html = '<section><p>Shipment details</p><img src="cid:scan">' * count
        data = _mail_with_inline_png(
            html + "</section>" * count, image_name="scan", text="USD 310.42"
        )
        with patch.object(
            eml_parser, "_is_inline_element", wraps=eml_parser._is_inline_element
        ) as is_inline:
            root, body, scan = dispatcher.parse_batch([("mail.eml", data)])

        assert root.status is body.status is scan.status is DocumentStatus.PARSED
        assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
        assert "USD 310.42" in scan.raw_text, scan.raw_text
        call_counts.append(is_inline.call_count)

    assert recognize_image.call_count == 3
    small, medium, large = call_counts
    assert 0 < small and medium <= small * 2.25 and large <= small * 4.5, (
        f"12 sections: {small}, 24 sections: {medium}, 48 sections: {large}"
    )


def test_r19_a2_main_real_signature_skips_logo(r8_eml_ocr):
    data = _mail_with_inline_png(
        '<main><p>Body</p><hr>Best regards<br>ACME<br>Tel: 123456<img src="cid:logo"></main>',
        image_name="logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    _assert_inline_image_ocr(logo, recognize_image, expected_text=None)


@pytest.mark.parametrize(
    ("bill_src", "location", "expected_text"),
    [
        pytest.param(
            "http://example.test:80/scan.png",
            "http://example.test/scan.png",
            "USD 310.42",
            id="http-default-port",
        ),
        pytest.param(
            "http://%65xample.test/scan.png",
            "http://example.test/scan.png",
            "USD 310.42",
            id="escaped-host",
        ),
        pytest.param(
            "https://example.test:443/scan.png",
            "https://example.test/scan.png",
            "USD 310.42",
            id="https-default-port",
        ),
        pytest.param(
            "http://example.test:8080/scan.png",
            "http://example.test/scan.png",
            None,
            id="non-default-port",
        ),
        pytest.param(
            "http://Alice@example.test:80/scan.png",
            "http://alice@example.test/scan.png",
            None,
            id="userinfo-case-with-default-port",
        ),
    ],
)
def test_r19_a3_default_port_and_escaped_host_match(bill_src, location, expected_text, r8_eml_ocr):
    message = _mail("Body")
    message.add_alternative(
        f'<main><img src="{bill_src}"></main><div class="signature"><img src="cid:logo"></div>',
        subtype="html",
    )
    _add_related_png(
        message.get_payload()[-1],
        filename="scan.png",
        text="USD 310.42",
        cid="<logo>",
        location=location,
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    _assert_inline_image_ocr(scan, recognize_image, expected_text=expected_text)


def test_r19_a6_nested_figure_inside_figcaption_does_not_leak(r8_eml_ocr):
    data = _mail_with_scan_and_logo(
        '<figure><img src="cid:logo"><figcaption>'
        "Company logo<br>Best regards<br>ACME<br>Tel: 123456"
        '<figure><img src="cid:scan"><figcaption>Invoice scan</figcaption></figure>'
        "</figcaption></figure>"
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan, logo = dispatcher.parse_batch([("mail.eml", data)])

    _assert_invoice_and_skipped_logo(scan, logo, recognize_image)


@pytest.mark.parametrize("extra_lines", [0, 10], ids=["inside-tail", "outside-tail"])
def test_r20_f1_main_scan_before_trailing_signoff_keeps_scan_and_skips_logo(
    extra_lines, r8_eml_ocr
):
    details = "<p>Shipment received</p>" * extra_lines
    html = (
        '<main><hr><p>Delivery Tel: 123456</p><img src="cid:scan">'
        f'{details}<p>Best regards</p><p>Sender Tel: 999</p><img src="cid:logo"></main>'
    )
    if extra_lines:
        # The body reference must override the signature copy outside the tail window.
        html += '<div class="signature"><img src="cid:scan"></div>'
    data = _mail_with_scan_and_logo(html)
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan, logo = dispatcher.parse_batch([("mail.eml", data)])

    _assert_invoice_and_skipped_logo(scan, logo, recognize_image)


@pytest.mark.parametrize(
    ("html_body", "expected_text"),
    [
        pytest.param(
            '<main>-- \n<img src="cid:InsertPic_001">'
            "<p>Delivery appointment Tel: 123456</p></main>",
            "USD 310.42",
            id="delivery-contact",
        ),
        pytest.param(
            "<main>-- \n<p>Best regards</p><p>ACME</p><p>Tel: 123456</p>"
            '<img src="cid:InsertPic_001"></main>',
            None,
            id="sender-signature",
        ),
    ],
)
def test_r20_f2_main_insertpic_respects_signoff_requirement(html_body, expected_text, r8_eml_ocr):
    message = _mail("Body")
    message.add_alternative(html_body, subtype="html")
    _add_related_png(
        message.get_payload()[-1], filename="scan.png", text="USD 310.42", cid="<InsertPic_001>"
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    _assert_inline_image_ocr(scan, recognize_image, expected_text=expected_text)


@pytest.mark.parametrize("image_cid", ["cid:InsertPic_001", "cid:logo"])
@pytest.mark.parametrize(
    "dash_signoff",
    ["-- \n<p>Best regards</p>", "-- \nBest regards"],
    ids=["separate-nodes", "same-node"],
)
def test_r21_a1_main_dash_long_signoff_skips_insertpic_and_logo(
    image_cid, dash_signoff, r8_eml_ocr
):
    html = (
        f"<main>{dash_signoff}"
        "<p>Alex Chen</p>"
        "<p>Operations Coordinator</p>"
        "<p>ACME</p>"
        "<p>Shanghai Branch</p>"
        "<p>Export Division</p>"
        "<p>Building A</p>"
        "<p>Logistics Team</p>"
        f'<p>Tel: 123456</p><img src="{image_cid}"></main>'
    )
    message = _mail("Body")
    message.add_alternative(html, subtype="html")
    _add_related_png(
        message.get_payload()[-1],
        filename="logo.png",
        text="ACME",
        cid=f"<{image_cid.removeprefix('cid:')}>",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    _assert_inline_image_ocr(logo, recognize_image, expected_text=None)


@pytest.mark.parametrize(
    ("html_body", "image_name"),
    [
        pytest.param(
            "<main><p>The carrier regards this shipment as urgent.</p><p>Tel: 123456</p>"
            '<img src="cid:InsertPic_001">-- \n<p>Sender information</p></main>',
            "InsertPic_001",
            id="regards-before-dash",
        ),
        pytest.param(
            "<footer><p>As regards the booking, see below:</p><p>Tel: 123456</p>"
            '<img src="cid:quote_001"><hr><p>Sender information</p></footer>',
            "quote_001",
            id="as-regards-before-hr",
        ),
        pytest.param(
            "<footer><p>We sincerely apologize for the surcharge adjustment.</p>"
            '<p>Tel: 123456</p><img src="cid:quote_001"></footer>',
            "quote_001",
            id="sincerely-in-footer",
        ),
        pytest.param(
            '<main><hr><p>As regards the booking, see below:</p><p>Tel: 123456</p>'
            '<img src="cid:quote_001">'
            + "<p>Shipment update</p>" * 8
            + '</main><div class="signature"><img src="cid:quote_001"></div>',
            "quote_001",
            id="as-regards-outside-tail",
        ),
    ],
)
def test_r22_f1_eml_reads_quotes_near_body_prose(html_body, image_name, r8_eml_ocr):
    amount = "USD 310.42"
    data = _mail_with_inline_png(html_body, image_name=image_name, text=amount)
    dispatcher, recognize_image = r8_eml_ocr

    _, _, quote = dispatcher.parse_batch([("mail.eml", data)])

    assert quote.sanitized_filename == f"{image_name}.png"
    _assert_inline_image_ocr(quote, recognize_image, expected_text=amount)


@pytest.mark.parametrize(
    "signoff",
    [
        pytest.param("Best regards,", id="best-regards"),
        pytest.param("Thank you, Warm regards,", id="warm-regards-after-comma"),
        pytest.param("Thank you; Kind regards!", id="kind-regards-after-semicolon"),
        pytest.param("Thank you — With best regards,", id="with-best-regards-after-dash"),
        pytest.param("Thanks &amp; regards,", id="thanks-and-regards"),
        pytest.param("Regards,", id="bare-regards"),
        pytest.param("Sincerely,", id="bare-sincerely"),
        pytest.param("Yours sincerely,", id="yours-sincerely"),
        pytest.param("Sincerely yours", id="sincerely-yours"),
        pytest.param("-- Best regards", id="double-dash"),
        pytest.param("Best <span>regards</span>", id="split-span"),
        pytest.param("此致", id="cizhi"),
        pytest.param("敬上", id="jingshang"),
        pytest.param("敬礼", id="jingli"),
    ],
)
def test_r22_f1_eml_still_skips_logos_after_true_signoffs(signoff, r8_eml_ocr):
    data = _mail_with_inline_png(
        f'<footer><div>{signoff}<br>ACME<br>Tel: 123456<img src="cid:sig_logo"></div></footer>',
        image_name="sig_logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    _assert_inline_image_ocr(logo, recognize_image, expected_text=None)


@pytest.mark.parametrize(
    "html_body",
    [
        pytest.param(
            '<footer><div><img src="cid:scan"><br>Invoice scan<br>'
            'Best <span>regards&nbsp;</span><br>Tel: 123456</div></footer>',
            id="br",
        ),
        pytest.param(
            '<body style="white-space:pre-line"><footer><div><img src="cid:scan">'
            'Invoice scan\nBest <span>regards&nbsp;</span>\nTel: 123456</div></footer></body>',
            id="pre-line-lf",
        ),
        pytest.param(
            '<body style="white-space:pre-line"><footer><div><img src="cid:scan">'
            'Invoice scan&#13;&#10;Best <span>regards&nbsp;</span>'
            '&#13;&#10;Tel: 123456</div></footer></body>',
            id="pre-line-crlf",
        ),
    ],
)
def test_r23_f1_eml_preserves_the_caption_before_a_split_signoff(html_body, r8_eml_ocr):
    amount = "USD 310.42"
    data = _mail_with_inline_png(html_body, image_name="scan", text=amount)
    dispatcher, recognize_image = r8_eml_ocr

    _, _, scan = dispatcher.parse_batch([("mail.eml", data)])

    assert scan.sanitized_filename == "scan.png"
    _assert_inline_image_ocr(scan, recognize_image, expected_text=amount)


@pytest.mark.parametrize(
    ("html_body", "image_name"),
    [
        pytest.param(
            '<footer><p>We <em>sincerely</em> apologize for the delay.</p>'
            '<p>Tel: 123456</p><img src="cid:quote_001"></footer>',
            "quote_001",
            id="footer-emphasis",
        ),
        pytest.param(
            '<footer><p>The carrier <span>regards</span> this shipment as urgent.</p>'
            '<p>Tel: 123456</p><img src="cid:quote_001"></footer>',
            "quote_001",
            id="footer-span",
        ),
        pytest.param(
            '<footer><p><span>The carrier</span>\n<span>regards</span>\n'
            '<span>this shipment as urgent.</span></p>'
            '<p>Tel: 123456</p><img src="cid:quote_001"></footer>',
            "quote_001",
            id="footer-space-between-spans",
        ),
        pytest.param(
            '<main><p>The carrier\nregards\nthis shipment as urgent.</p><p>Tel: 123456</p>'
            '<img src="cid:quote_001">-- \n<p>Sender info</p></main>',
            "quote_001",
            id="main-source-newlines",
        ),
        pytest.param(
            '<main><p>The carrier\nregards\nthis shipment as urgent.</p><p>Tel: 123456</p>'
            '<img src="cid:InsertPic_001">-- \n<p>Sender info</p></main>',
            "InsertPic_001",
            id="main-source-newlines-insertpic",
        ),
        pytest.param(
            '<body style="white-space:pre-line"><main>-- &#13;&#10;'
            'We thank you <em>sincerely</em>, and we will send the quote today.<br>'
            'Tel: 123456<img src="cid:quote_001"></main></body>',
            "quote_001",
            id="main-after-dash-crlf",
        ),
        pytest.param(
            '<main><hr><p>We <em>sincerely</em> apologize for the delay.</p><p>Tel: 123456</p>'
            '<img src="cid:quote_001">'
            + "<p>Shipment update</p>" * 8
            + '</main><div class="signature"><img src="cid:quote_001"></div>',
            "quote_001",
            id="main-after-hr-outside-tail",
        ),
        pytest.param(
            '<main><p>We thank you <em>sincerely</em>, and will send the quote today.</p>'
            '<p>Tel: 123456</p>'
            + "<p>Shipment update</p>" * 5
            + '<img src="cid:InsertPic_001"></main>',
            "InsertPic_001",
            id="main-tail-starts-inside-a-line",
        ),
        pytest.param(
            '<footer><figure><img src="cid:quote_001">'
            '<figcaption>We <em>sincerely</em> apologize for the delay.<br>Tel: 123456</figcaption>'
            '</figure></footer>',
            "quote_001",
            id="footer-figure",
        ),
    ],
)
def test_r23_f2_eml_preserves_body_prose_on_one_visual_line(html_body, image_name, r8_eml_ocr):
    amount = "USD 310.42"
    data = _mail_with_inline_png(html_body, image_name=image_name, text=amount)
    dispatcher, recognize_image = r8_eml_ocr

    _, _, quote = dispatcher.parse_batch([("mail.eml", data)])

    assert quote.sanitized_filename == f"{image_name}.png"
    _assert_inline_image_ocr(quote, recognize_image, expected_text=amount)


@pytest.mark.parametrize(
    "html_body",
    [
        pytest.param(
            '<footer><p>With regards to your booking, see below.</p><p>Tel: 123456</p>'
            '<img src="cid:quote_001"></footer>',
            id="footer",
        ),
        pytest.param(
            '<figure><figcaption>With regards to your booking, see below.<br>Tel: 123456'
            '</figcaption><img src="cid:quote_001"></figure>',
            id="figure",
        ),
        pytest.param(
            '<footer><figure><figcaption>With regards to your booking, see below.<br>Tel: 123456'
            '</figcaption><img src="cid:quote_001"></figure></footer>',
            id="footer-figure",
        ),
    ],
)
def test_r23_f3_eml_keeps_quotes_after_with_regards_to(html_body, r8_eml_ocr):
    amount = "USD 310.42"
    data = _mail_with_inline_png(html_body, image_name="quote_001", text=amount)
    dispatcher, recognize_image = r8_eml_ocr

    _, _, quote = dispatcher.parse_batch([("mail.eml", data)])

    assert quote.sanitized_filename == "quote_001.png"
    _assert_inline_image_ocr(quote, recognize_image, expected_text=amount)


@pytest.mark.parametrize("signoff", ["With regards,", "With regards"], ids=["comma", "end-of-line"])
def test_r23_f3_eml_skips_logo_after_with_regards_signoff(signoff, r8_eml_ocr):
    data = _mail_with_inline_png(
        f'<footer><div>{signoff}<br>ACME<br>Tel: 123456<img src="cid:sig_logo"></div></footer>',
        image_name="sig_logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    _assert_inline_image_ocr(logo, recognize_image, expected_text=None)


@pytest.mark.parametrize(
    "signoff",
    [
        pytest.param("Thanks and best regards,", id="thanks-and-best"),
        pytest.param("Thanks &amp; best regards,", id="thanks-ampersand-best"),
        pytest.param("Many thanks and best regards,", id="many-thanks-and-best"),
        pytest.param("Kindest regards,", id="kindest"),
        pytest.param("Warmest regards,", id="warmest"),
        pytest.param("&nbsp;Best regards,", id="leading-nbsp"),
        pytest.param("<span>Best</span> <span>regards</span>", id="space-between-spans"),
        pytest.param("Best <span>regards&nbsp;</span>", id="trailing-nbsp-in-span"),
    ],
)
def test_r23_f4_eml_skips_logos_after_common_signoffs(signoff, r8_eml_ocr):
    data = _mail_with_inline_png(
        f'<footer><div>{signoff}<br>ACME<br>Tel: 123456<img src="cid:sig_logo"></div></footer>',
        image_name="sig_logo",
        text="ACME",
    )
    dispatcher, recognize_image = r8_eml_ocr

    _, _, logo = dispatcher.parse_batch([("mail.eml", data)])

    _assert_inline_image_ocr(logo, recognize_image, expected_text=None)


@pytest.mark.parametrize(
    "html_body",
    [
        pytest.param(
            '<footer><div>Best regards<br>ACME<br>Tel: 123456<img src="cid:logo"></div>'
            '<div><img src="cid:scan"></div></footer>',
            id="r6-1a-scan-after-complete-signature",
        ),
        pytest.param(
            '<footer><div><img src="cid:scan"><br>Best regards<br>ACME<br>Tel: 123456'
            '<img src="cid:logo"></div></footer>',
            id="r6-1b-scan-before-signoff-in-same-unit",
        ),
        pytest.param(
            '<footer><div><figure><img src="cid:scan"><figcaption>Invoice scan</figcaption>'
            '</figure></div><div><div><img src="cid:logo"></div>'
            '<div>Best regards<br>ACME<br>Tel: 123456</div></div></footer>',
            id="r6-4-logo-before-signoff-in-sibling-div",
        ),
        pytest.param(
            '<p>Charges attached</p><img src="cid:scan">'
            '<div class="signature"><img src="cid:logo" alt="ACME Freight logo"></div>',
            id="r6-5-freight-company-logo",
        ),
        pytest.param(
            '<footer><div><div>Best regards<br>ACME<br>Tel: 123456<img src="cid:logo"></div>'
            '<div><img src="cid:scan"></div></div></footer>',
            id="a1a-nested-scan-after-complete-signature",
        ),
        pytest.param(
            '<footer><div><div><img src="cid:scan"></div>'
            '<div><img src="cid:logo">Best regards<br>ACME<br>Tel: 123456</div></div></footer>',
            id="a1b-nested-scan-before-complete-signature",
        ),
        pytest.param(
            '<div class="signature"><img src="cid:scan" alt="Ocean freight USD 310.42"></div>'
            '<div class="signature"><img src="cid:logo" alt="ACME Freight logo"></div>',
            id="a2b-amount-in-freight-alt",
        ),
    ],
)
def test_r6_eml_reads_the_bill_scan_and_skips_the_signature_logo(html_body):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    message = _mail("Charges attached")
    message.add_alternative(html_body, subtype="html")
    html = message.get_payload()[-1]
    scan_data = _png_with_text("USD 310.42")
    html.add_related(
        scan_data, maintype="image", subtype="png", cid="<scan>", filename="scan.png"
    )
    html.add_related(
        _png_with_text("ACME"), maintype="image", subtype="png", cid="<logo>", filename="logo.png"
    )
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=RapidOcrFallback()
    )

    _, _, scan, logo = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    assert scan.sanitized_filename == "scan.png"
    assert scan.status is DocumentStatus.PARSED, scan.error_message
    assert scan.extraction_channel is ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
    assert "USD 310.42" in scan.raw_text, scan.raw_text
    assert logo.sanitized_filename == "logo.png"
    assert logo.status is DocumentStatus.SKIPPED_NOISE, logo.raw_text
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE


@pytest.mark.parametrize(
    "image_slot",
    [
        pytest.param('<div><img src="cid:logo"></div>', id="r6-5-wrapped-logo"),
        pytest.param('<img src="cid:logo">', id="a5-direct-footer-logo"),
    ],
)
def test_r6_5_eml_skips_logo_after_text_only_footer_signature(image_slot):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    message = _mail("Body")
    message.add_alternative(
        f"<footer><div>Best regards<br>ACME<br>Tel: 123456</div>{image_slot}</footer>",
        subtype="html",
    )
    message.get_payload()[-1].add_related(
        _png_with_text("ACME"), maintype="image", subtype="png", cid="<logo>", filename="logo.png"
    )
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=RapidOcrFallback()
    )

    with patch.object(
        RapidOcrFallback,
        "recognize_image_bytes",
        autospec=True,
        side_effect=RapidOcrFallback.recognize_image_bytes,
    ) as recognize_image:
        _, _, logo = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    assert logo.sanitized_filename == "logo.png"
    assert logo.status is DocumentStatus.SKIPPED_NOISE, logo.raw_text
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE
    recognize_image.assert_not_called()


def test_eml_skips_a_logo_in_an_unmarked_footer_with_signoff_and_contact():
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    message = _mail("Body")
    message.add_alternative(
        '<footer>Best regards<br>ACME<br>Tel: 123456<img src="cid:logo"></footer>', subtype="html"
    )
    message.get_payload()[-1].add_related(
        b"synthetic signature logo",
        maintype="image",
        subtype="png",
        cid="<logo>",
        filename="logo.png",
    )
    local = RecordingLocalOcr()
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=local
    )

    _, _, logo = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    assert logo.sanitized_filename == "logo.png"
    assert logo.status is DocumentStatus.SKIPPED_NOISE
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE
    assert local.images == []


@pytest.mark.parametrize(
    "signoff",
    [
        "<hr><div>Best regards<br>ACME<br>Tel: 123456</div>",
        "<div>-- \nBest regards\nACME\nTel: 123456</div>",
    ],
    ids=["hr", "dash-line"],
)
def test_eml_skips_signature_logo_across_sibling_layout_divs(signoff):
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    message = _mail("Body")
    message.add_alternative(
        f'<body><p>Body</p>{signoff}<div><img src="cid:logo"></div></body>', subtype="html"
    )
    logo_data = b"synthetic signature logo"
    message.get_payload()[-1].add_related(
        logo_data, maintype="image", subtype="png", cid="<logo>", filename="logo.png"
    )
    local = RecordingLocalOcr()
    dispatcher = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK, local_ocr=local
    )

    _, _, logo = dispatcher.parse_batch([("mail.eml", message.as_bytes())])

    assert logo.sanitized_filename == "logo.png"
    assert logo.status is DocumentStatus.SKIPPED_NOISE
    assert logo.extraction_channel is ExtractionChannel.SKIPPED_SIGNATURE
    assert logo.raw_bytes == logo_data
    assert local.images == []


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
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    parser = DocumentParserDispatcher()
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

    depth, slots = parser.compute_retry_context(list(reversed(documents)), rejected.doc_id)
    assert (depth, slots) == (4, 113)
    assert parser.compute_retry_context(documents, documents[0].doc_id) == (1, 119)
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
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    limits = ResourceLimits(max_expanded_documents=3)
    dispatcher = DocumentParserDispatcher(limits=limits)
    parent_mail = _mail(" ")
    child_mail = _wide_mail((FIXTURES / "b01_merged.xls").read_bytes(), attachment_count=1)
    parent_mail.add_attachment(child_mail, filename="second.eml")
    documents = dispatcher.parse_batch([("first.eml", parent_mail.as_bytes())])
    parent, old_child, old_body = documents
    assert old_body.parent_doc_id == old_child.doc_id
    retry_target = dict(existing_doc_id=old_child.doc_id, parent_doc_id=parent.doc_id)
    with pytest.raises(ValueError, match="必须显式传入 remaining_expanded_slots"):
        dispatcher.retry_single_document(
            old_child.original_filename, old_child.raw_bytes, depth=2, **retry_target
        )

    depth, slots = dispatcher.compute_retry_context(documents, old_child.doc_id)
    assert (depth, slots) == (2, 1)
    retried = dispatcher.retry_single_document(
        old_child.original_filename,
        old_child.raw_bytes,
        depth=depth,
        remaining_expanded_slots=slots,
        **retry_target,
    )

    updated_documents = [parent, *retried]
    assert len(updated_documents) == 3
    root, body = retried
    assert (root.doc_id, root.parent_doc_id) == (old_child.doc_id, parent.doc_id)
    assert root.status is DocumentStatus.FAILED
    assert root.error_message == "单批展开文档数超过 3 项上限，部分子件已截断"
    assert body.parent_doc_id == root.doc_id
    assert body.status is DocumentStatus.PARSED
    assert body.raw_text.strip() == "Many charges"


def test_compute_retry_context_requires_explicit_limits_without_dispatcher():
    from shipping_billing_agent.parsers.dispatcher import (
        DocumentParserDispatcher,
        compute_retry_context,
    )

    documents = DocumentParserDispatcher().parse_batch([("mail.eml", _mail("Charges").as_bytes())])

    with pytest.raises(TypeError, match="limits"):
        compute_retry_context(documents, documents[0].doc_id)


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
