"""Extract PDF text and tables, with optional page-level OCR."""

from io import BytesIO
from typing import Protocol

import pdfplumber
from pdfminer.pdfdocument import PDFEncryptionError
from pdfplumber.utils.exceptions import PdfminerException
from PIL import Image

from shipping_billing_agent.domain.models import ExtractionChannel, ParsedTable

_MIN_TEXT_CHARACTERS = 15
_OCR_RESOLUTION_DPI = 200


class PdfParseError(ValueError):
    pass


class PdfPageOcr(Protocol):
    def recognize_pil_image(self, image: Image.Image, page: int = 1) -> tuple[str, float]: ...


def parse_pdf(
    data: bytes, *, local_ocr: PdfPageOcr | None = None
) -> tuple[str, list[ParsedTable], ExtractionChannel]:
    sections: list[str] = []
    tables: list[ParsedTable] = []
    channel = ExtractionChannel.LOCAL_PDF_TEXT_FALLBACK
    try:
        with BytesIO(data) as stream, pdfplumber.open(stream) as pdf:
            if pdf.doc.encryption is not None:
                raise PdfParseError("不支持加密 PDF")
            if not pdf.pages:
                raise PdfParseError("PDF 中没有可读取的页面")
            for page in pdf.pages:
                page_number = page.page_number
                text = page.extract_text() or ""
                sparse_text = sum(not char.isspace() for char in text) < _MIN_TEXT_CHARACTERS
                if sparse_text and any(page.objects.values()):
                    if local_ocr is None:
                        raise PdfParseError(f"第 {page_number} 页为扫描图像页，未配置本地 OCR 引擎")
                    with page.to_image(resolution=_OCR_RESOLUTION_DPI).original as image:
                        text, _ = local_ocr.recognize_pil_image(image, page=page_number)
                    channel = ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
                    source = f"[Page {page_number} | OCR]"
                else:
                    source = f"[Page {page_number} | Text]"
                    tables.extend(
                        ParsedTable(
                            rows=[[cell or "" for cell in row] for row in grid], source=source
                        )
                        for grid in page.extract_tables()
                    )
                sections.append(f"{source}\n{text}" if text else source)
                page.close()
    except PdfParseError:
        raise
    except Exception as exc:
        cause = exc.args[0] if isinstance(exc, PdfminerException) and exc.args else exc
        if isinstance(cause, PDFEncryptionError):
            raise PdfParseError("不支持加密 PDF") from exc
        raise PdfParseError("无法读取 PDF 页面或识别扫描内容") from exc
    return "\n\n".join(sections), tables, channel
