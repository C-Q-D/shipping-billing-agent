"""Extract PDF text and tables, with optional page-level OCR."""

from dataclasses import dataclass
from io import BytesIO
from itertools import chain, pairwise
from typing import Any, Protocol

import pdfplumber
from pdfminer.pdfdocument import PDFEncryptionError
from pdfplumber.page import Page
from pdfplumber.utils.exceptions import PdfminerException
from PIL import Image

from shipping_billing_agent.domain.models import ExtractionChannel, ParsedTable

_MIN_TEXT_CHARACTERS = 15
_OCR_RESOLUTION_DPI = 200
_COLLINEAR_TOL_PT = 1.0
_LONG_RULE_SPAN_PT = 40.0
_BAR_RULE_MAX_THICKNESS_PT = 6.0
_BAR_RULE_MIN_ASPECT = 15.0
_SCANLINE_MAX_GAP_PT = 6.0
_SCANLINE_MIN_RUN = 4
_Point = tuple[float, float]
_RuleSegment = tuple[float, float, float]


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


def _classify_straight_segment(
    p1: _Point,
    p2: _Point,
    table_grids: list[_TableGrid],
    horiz_segments: list[_RuleSegment],
    vert_segments: list[_RuleSegment],
) -> int:
    if _is_table_segment(p1, p2, table_grids):
        return 0
    (x0, y0), (x1, y1) = p1, p2
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    if dx <= 0.5 and dy <= 0.5:
        return 1
    if dy <= 0.5:
        horiz_segments.append(((y0 + y1) / 2, min(x0, x1), max(x0, x1)))
        return 0
    if dx <= 0.5:
        vert_segments.append(((x0 + x1) / 2, min(y0, y1), max(y0, y1)))
        return 0
    return 1


def _process_curve(
    curve: dict[str, Any],
    table_grids: list[_TableGrid],
    horiz_segments: list[_RuleSegment],
    vert_segments: list[_RuleSegment],
) -> tuple[bool, int]:
    path = curve.get("path")
    if path is None:
        added = sum(
            _classify_straight_segment(p1, p2, table_grids, horiz_segments, vert_segments)
            for p1, p2 in pairwise(curve.get("pts") or ())
        )
        return False, added

    added = 0
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
            return True, 0
        if current is not None and end is not None:
            added += _classify_straight_segment(
                current, end, table_grids, horiz_segments, vert_segments
            )
        current = end
    return False, added


def _count_rule_clusters(segments: list[_RuleSegment]) -> tuple[int, list[float]]:
    if not segments:
        return 0, []

    ordered = iter(sorted(segments, key=lambda segment: segment[0]))
    anchor_coord, min_pos, max_pos = next(ordered)
    short_rules = 0
    long_coords: list[float] = []
    for coord, lo, hi in ordered:
        if abs(coord - anchor_coord) <= _COLLINEAR_TOL_PT:
            min_pos, max_pos = min(min_pos, lo), max(max_pos, hi)
            continue
        if max_pos - min_pos > _LONG_RULE_SPAN_PT:
            long_coords.append(anchor_coord)
        else:
            short_rules += 1
        anchor_coord, min_pos, max_pos = coord, lo, hi

    if max_pos - min_pos > _LONG_RULE_SPAN_PT:
        long_coords.append(anchor_coord)
    else:
        short_rules += 1
    return short_rules, long_coords


def _has_dense_parallel_rules(long_coords: list[float]) -> bool:
    run = 1
    for previous, coord in pairwise(long_coords):
        run = run + 1 if coord - previous <= _SCANLINE_MAX_GAP_PT else 1
        if run >= _SCANLINE_MIN_RUN:
            return True
    return False


def _has_glyph_strokes(page: Page, table_grids: list[_TableGrid]) -> bool:
    glyph_elements = 0
    horiz_segments: list[_RuleSegment] = []
    vert_segments: list[_RuleSegment] = []

    for line in page.lines:
        points = line.get("pts") or ()
        if len(points) >= 2:
            p1, p2 = points[0], points[1]
        else:
            p1, p2 = (line["x0"], line["top"]), (line["x1"], line["bottom"])
        glyph_elements += _classify_straight_segment(
            p1, p2, table_grids, horiz_segments, vert_segments
        )

    for rect in page.rects:
        x0, top, x1, bottom = rect["x0"], rect["top"], rect["x1"], rect["bottom"]
        width, height = abs(x1 - x0), abs(bottom - top)
        if width <= 4.0 and height <= 4.0:
            glyph_elements += 1
        elif width <= 3.0 or (
            width <= _BAR_RULE_MAX_THICKNESS_PT
            and height > _LONG_RULE_SPAN_PT
            and height / max(width, 1e-6) >= _BAR_RULE_MIN_ASPECT
        ):
            x = (x0 + x1) / 2
            if not _is_table_segment((x, top), (x, bottom), table_grids):
                vert_segments.append((x, min(top, bottom), max(top, bottom)))
        elif height <= 3.0 or (
            height <= _BAR_RULE_MAX_THICKNESS_PT
            and width > _LONG_RULE_SPAN_PT
            and width / max(height, 1e-6) >= _BAR_RULE_MIN_ASPECT
        ):
            y = (top + bottom) / 2
            if not _is_table_segment((x0, y), (x1, y), table_grids):
                horiz_segments.append((y, min(x0, x1), max(x0, x1)))
        else:
            corners = ((x0, top), (x1, top), (x1, bottom), (x0, bottom), (x0, top))
            if all(_is_table_segment(p1, p2, table_grids) for p1, p2 in pairwise(corners)):
                continue
            if not rect.get("fill") and min(width, height) > _LONG_RULE_SPAN_PT:
                continue
            glyph_elements += 1

    has_bezier_curve = False
    for curve in page.curves:
        has_bezier_curve, added = _process_curve(curve, table_grids, horiz_segments, vert_segments)
        glyph_elements += added
        if has_bezier_curve:
            break

    short_h, long_h_coords = _count_rule_clusters(horiz_segments)
    short_v, long_v_coords = _count_rule_clusters(vert_segments)
    return (
        has_bezier_curve
        or glyph_elements + short_h + short_v >= 2
        or _has_dense_parallel_rules(long_h_coords)
        or _has_dense_parallel_rules(long_v_coords)
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

                has_glyph_strokes = _has_glyph_strokes(page, table_grids)
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
