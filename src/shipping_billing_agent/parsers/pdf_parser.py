"""Extract PDF text and tables, with optional page-level OCR."""

from dataclasses import dataclass
from io import BytesIO
from itertools import chain, pairwise
from typing import Any, Protocol

import pdfplumber
from pdfminer.pdfdocument import PDFEncryptionError
from pdfplumber.utils.exceptions import PdfminerException
from PIL import Image

from shipping_billing_agent.domain.models import ExtractionChannel, ParsedTable

_MIN_TEXT_CHARACTERS = 15
_OCR_RESOLUTION_DPI = 200
_MAX_GLYPH_STROKE_PT = 30.0
_Point = tuple[float, float]


class PdfParseError(ValueError):
    pass


class PdfPageOcr(Protocol):
    def recognize_pil_image(self, image: Image.Image, page: int = 1) -> tuple[str, float]: ...


@dataclass(frozen=True, slots=True)
class _TableGrid:
    x0: float
    top: float
    x1: float
    bottom: float
    xs: set[float]
    ys: set[float]


def _table_grid(table: Any) -> _TableGrid:
    x0, top, x1, bottom = table.bbox
    xs, ys = {x0, x1}, {top, bottom}
    cells = (cell for row in table.rows for cell in row.cells if cell is not None)
    columns = (column.bbox for column in table.columns)
    for left, upper, right, lower in chain(cells, columns):
        xs.update((left, right))
        ys.update((upper, lower))
    return _TableGrid(x0, top, x1, bottom, xs, ys)


def _is_table_segment(
    p1: _Point, p2: _Point, table_grids: list[_TableGrid], tol: float = 2.0
) -> bool:
    (x1, y1), (x2, y2) = p1, p2
    dx, dy = abs(x1 - x2), abs(y1 - y2)
    if dx <= 0.5 and dy <= 0.5:
        return False
    if dy <= 0.5:
        x_lo, x_hi = min(x1, x2), max(x1, x2)
        y = (y1 + y2) / 2
        return any(
            x_lo >= grid.x0 - tol
            and x_hi <= grid.x1 + tol
            and any(abs(y - gy) <= tol for gy in grid.ys)
            for grid in table_grids
        )
    if dx <= 0.5:
        y_lo, y_hi = min(y1, y2), max(y1, y2)
        x = (x1 + x2) / 2
        return any(
            y_lo >= grid.top - tol
            and y_hi <= grid.bottom + tol
            and any(abs(x - gx) <= tol for gx in grid.xs)
            for grid in table_grids
        )
    return False


def _is_non_table_curve(curve: dict[str, Any], table_grids: list[_TableGrid]) -> bool:
    path = curve.get("path")
    if path is None:
        return any(
            not _is_table_segment(p1, p2, table_grids)
            for p1, p2 in pairwise(curve.get("pts") or ())
        )

    subpath_start: _Point | None = None
    current: _Point | None = None
    for operator, *points in path:
        if operator == "m":
            subpath_start = current = points[0]
            continue
        if operator == "l":
            end = points[0]
        elif operator == "h":
            end = subpath_start
            if current == end:
                continue
        else:
            return True
        if (
            current is not None
            and end is not None
            and not _is_table_segment(current, end, table_grids)
        ):
            return True
        current = end
    return False


def _line_is_glyph_stroke(line: dict[str, Any], table_grids: list[_TableGrid]) -> bool:
    points = line.get("pts")
    if points is None:
        p1, p2 = (line["x0"], line["top"]), (line["x1"], line["bottom"])
    else:
        p1, p2 = points[0], points[1]
    if _is_table_segment(p1, p2, table_grids):
        return False
    dx, dy = abs(p1[0] - p2[0]), abs(p1[1] - p2[1])
    return (dx > 0.5 and dy > 0.5) or max(dx, dy) <= _MAX_GLYPH_STROKE_PT


def _rect_is_glyph_stroke(rect: dict[str, Any], table_grids: list[_TableGrid]) -> bool:
    x0, top, x1, bottom = rect["x0"], rect["top"], rect["x1"], rect["bottom"]
    width, height = abs(x1 - x0), abs(bottom - top)
    if width <= 4.0 and height <= 4.0:
        return True
    if width <= 3.0:
        x = (x0 + x1) / 2
        points = ((x, top), (x, bottom))
    elif height <= 3.0:
        y = (top + bottom) / 2
        points = ((x0, y), (x1, y))
    else:
        points = ((x0, top), (x1, top), (x1, bottom), (x0, bottom), (x0, top))
    return max(width, height) <= _MAX_GLYPH_STROKE_PT and any(
        not _is_table_segment(p1, p2, table_grids) for p1, p2 in pairwise(points)
    )


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
                valid_tables: list[tuple[Any, list[list[str | None]]]] = []
                for table in page.find_tables() or []:
                    grid = table.extract()
                    if any((cell or "").strip() for row in grid for cell in row):
                        valid_tables.append((table, grid))
                table_grids = [_table_grid(table) for table, _ in valid_tables]

                has_non_table_curve = any(
                    _is_non_table_curve(curve, table_grids) for curve in page.curves
                )
                non_table_glyph_strokes = sum(
                    _line_is_glyph_stroke(line, table_grids) for line in page.lines
                ) + sum(
                    _rect_is_glyph_stroke(rect, table_grids) for rect in page.rects
                )
                has_glyph_strokes = has_non_table_curve or non_table_glyph_strokes >= 2
                has_visual_objects = bool(page.images or page.curves or page.rects or page.lines)
                needs_ocr = sparse_text and (
                    bool(page.images)
                    or has_glyph_strokes
                    or (not text.strip() and has_visual_objects)
                )
                text_source = f"[Page {page_number} | Text]"
                source = text_source
                if needs_ocr:
                    if local_ocr is None:
                        raise PdfParseError(f"第 {page_number} 页为扫描图像页，未配置本地 OCR 引擎")
                    with page.to_image(resolution=_OCR_RESOLUTION_DPI).original as image:
                        text, _ = local_ocr.recognize_pil_image(image, page=page_number)
                    channel = ExtractionChannel.LOCAL_RAPIDOCR_FALLBACK
                    source = f"[Page {page_number} | OCR]"
                tables.extend(
                    ParsedTable(
                        rows=[[cell or "" for cell in row] for row in grid], source=text_source
                    )
                    for _, grid in valid_tables
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
