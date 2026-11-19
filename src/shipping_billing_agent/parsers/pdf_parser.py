"""Extract PDF text and tables, with optional page-level OCR."""

from collections.abc import Iterator
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
_AXIS_TOL_PT = 0.5
_DUPLICATE_VERTEX_TOL_PT = 1e-4
_COLLINEAR_TOL_PT = 1.0
_RULE_INTERVAL_TOL_PT = 1.0
_LONG_RULE_SPAN_PT = 40.0
_THIN_RULE_MAX_THICKNESS_PT = 3.0
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
    if dx <= _AXIS_TOL_PT and dy <= _AXIS_TOL_PT:
        return False
    if dy <= _AXIS_TOL_PT:
        x_lo, x_hi = min(x1, x2), max(x1, x2)
        y = (y1 + y2) / 2
        return any(
            x_lo >= grid.x0 - tol
            and x_hi <= grid.x1 + tol
            and any(abs(y - gy) <= tol for gy in grid.ys)
            for grid in table_grids
        )
    if dx <= _AXIS_TOL_PT:
        y_lo, y_hi = min(y1, y2), max(y1, y2)
        x = (x1 + x2) / 2
        return any(
            y_lo >= grid.top - tol
            and y_hi <= grid.bottom + tol
            and any(abs(x - gx) <= tol for gx in grid.xs)
            for grid in table_grids
        )
    return False


def _classify_rect(
    x0: float,
    top: float,
    x1: float,
    bottom: float,
    *,
    filled: bool,
    table_grids: list[_TableGrid],
    horiz_segments: list[_RuleSegment],
    vert_segments: list[_RuleSegment],
) -> int:
    width, height = abs(x1 - x0), abs(bottom - top)
    if width <= 4.0 and height <= 4.0:
        return 1
    if width <= _THIN_RULE_MAX_THICKNESS_PT or (
        width <= _BAR_RULE_MAX_THICKNESS_PT
        and height > _LONG_RULE_SPAN_PT
        and height / max(width, 1e-6) >= _BAR_RULE_MIN_ASPECT
    ):
        x = (x0 + x1) / 2
        if not _is_table_segment((x, top), (x, bottom), table_grids):
            vert_segments.append((x, min(top, bottom), max(top, bottom)))
        return 0
    if height <= _THIN_RULE_MAX_THICKNESS_PT or (
        height <= _BAR_RULE_MAX_THICKNESS_PT
        and width > _LONG_RULE_SPAN_PT
        and width / max(height, 1e-6) >= _BAR_RULE_MIN_ASPECT
    ):
        y = (top + bottom) / 2
        if not _is_table_segment((x0, y), (x1, y), table_grids):
            horiz_segments.append((y, min(x0, x1), max(x0, x1)))
        return 0

    corners = ((x0, top), (x1, top), (x1, bottom), (x0, bottom), (x0, top))
    if all(_is_table_segment(p1, p2, table_grids) for p1, p2 in pairwise(corners)):
        return 0
    if not filled and min(width, height) > _LONG_RULE_SPAN_PT:
        return 0
    return 1


def _line_width(obj: dict[str, Any]) -> float:
    linewidth = float(obj.get("linewidth") or 1.0)
    return linewidth if linewidth >= 0 else 1.0


def _classify_straight_segment(
    p1: _Point,
    p2: _Point,
    table_grids: list[_TableGrid],
    horiz_segments: list[_RuleSegment],
    vert_segments: list[_RuleSegment],
    *,
    linewidth: float = 1.0,
) -> int:
    if _is_table_segment(p1, p2, table_grids):
        return 0
    (x0, y0), (x1, y1) = p1, p2
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    if dx <= _AXIS_TOL_PT and dy <= _AXIS_TOL_PT:
        return 1
    if dy <= _AXIS_TOL_PT:
        y = (y0 + y1) / 2
        left, right = min(x0, x1), max(x0, x1)
        if linewidth > _THIN_RULE_MAX_THICKNESS_PT:
            return _classify_rect(
                left,
                y - linewidth / 2,
                right,
                y + linewidth / 2,
                filled=True,
                table_grids=table_grids,
                horiz_segments=horiz_segments,
                vert_segments=vert_segments,
            )
        horiz_segments.append((y, left, right))
        return 0
    if dx <= _AXIS_TOL_PT:
        x = (x0 + x1) / 2
        top, bottom = min(y0, y1), max(y0, y1)
        if linewidth > _THIN_RULE_MAX_THICKNESS_PT:
            return _classify_rect(
                x - linewidth / 2,
                top,
                x + linewidth / 2,
                bottom,
                filled=True,
                table_grids=table_grids,
                horiz_segments=horiz_segments,
                vert_segments=vert_segments,
            )
        vert_segments.append((x, top, bottom))
        return 0
    return 1


def _same_vertex(p1: _Point, p2: _Point) -> bool:
    return (
        abs(p1[0] - p2[0]) <= _DUPLICATE_VERTEX_TOL_PT
        and abs(p1[1] - p2[1]) <= _DUPLICATE_VERTEX_TOL_PT
    )


def _straight_subpaths(path: list[tuple[Any, ...]]) -> Iterator[tuple[list[_Point], bool]]:
    points: list[_Point] = []
    closed_start: _Point | None = None
    for operator, *vertices in path:
        if operator == "m":
            if points:
                yield points, False
            points = [vertices[0]]
            closed_start = None
        elif operator == "h" and points:
            closed_start = points[0]
            if len(points) > 1 and _same_vertex(closed_start, points[-1]):
                points.pop()
            yield points, True
            points = []
        elif operator == "l":
            if not points:
                if closed_start is None:
                    continue
                # A line after closepath starts from the closed subpath's first vertex.
                points = [closed_start]
            if not _same_vertex(points[-1], vertices[0]):
                points.append(vertices[0])

    if points:
        yield points, False


def _rectangle_bounds(points: list[_Point]) -> tuple[float, float, float, float] | None:
    corners = points
    while len(corners) >= 4:
        reduced = []
        for idx, (x, y) in enumerate(corners):
            prev_x, prev_y = corners[idx - 1]
            next_x, next_y = corners[(idx + 1) % len(corners)]
            same_x = abs(prev_x - x) <= _AXIS_TOL_PT and abs(x - next_x) <= _AXIS_TOL_PT
            same_y = abs(prev_y - y) <= _AXIS_TOL_PT and abs(y - next_y) <= _AXIS_TOL_PT
            if not (same_x or same_y):
                reduced.append((x, y))
        if len(reduced) == len(corners):
            break
        corners = reduced

    if len(corners) != 4:
        return None
    xs, ys = zip(*corners, strict=True)
    x0, top, x1, bottom = min(xs), min(ys), max(xs), max(ys)
    if x1 - x0 <= _AXIS_TOL_PT or bottom - top <= _AXIS_TOL_PT:
        return None
    if set(corners) != {(x0, top), (x1, top), (x1, bottom), (x0, bottom)}:
        return None
    if any(
        start[0] != end[0] and start[1] != end[1]
        for start, end in pairwise(chain(corners, corners[:1]))
    ):
        return None
    return x0, top, x1, bottom


def _process_curve(
    curve: dict[str, Any],
    table_grids: list[_TableGrid],
    horiz_segments: list[_RuleSegment],
    vert_segments: list[_RuleSegment],
) -> tuple[bool, int]:
    path = curve.get("path")
    if path is not None and any(operator not in {"m", "l", "h"} for operator, *_ in path):
        return True, 0

    linewidth = _line_width(curve)
    if path is None:
        added = sum(
            _classify_straight_segment(
                p1, p2, table_grids, horiz_segments, vert_segments, linewidth=linewidth
            )
            for p1, p2 in pairwise(curve.get("pts") or ())
        )
        return False, added

    added = 0
    filled = bool(curve.get("fill"))
    for points, closed in _straight_subpaths(path):
        if not closed and filled and len(points) >= 3:
            # PDF fills implicitly close open subpaths.
            if _same_vertex(points[0], points[-1]):
                points.pop()
            closed = True
        if len(points) == 1:
            added += 1
            continue
        if closed and len(points) >= 4 and (bounds := _rectangle_bounds(points)) is not None:
            added += _classify_rect(
                *bounds,
                filled=filled,
                table_grids=table_grids,
                horiz_segments=horiz_segments,
                vert_segments=vert_segments,
            )
            continue
        vertices = chain(points, points[:1]) if closed else points
        for p1, p2 in pairwise(vertices):
            added += _classify_straight_segment(
                p1, p2, table_grids, horiz_segments, vert_segments, linewidth=linewidth
            )
    return False, added


def _merge_rule_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1] + _RULE_INTERVAL_TOL_PT:
            prev_lo, prev_hi = merged[-1]
            merged[-1] = (prev_lo, max(prev_hi, hi))
        else:
            merged.append((lo, hi))
    return merged


def _is_long_rule(merged: list[tuple[float, float]]) -> bool:
    if merged[-1][1] - merged[0][0] <= _LONG_RULE_SPAN_PT:
        return False
    if len(merged) == 1:
        return True

    lengths = [hi - lo for lo, hi in merged]
    gaps = [next_lo - hi for (_, hi), (next_lo, _) in pairwise(merged)]
    widest_gap = max(gaps)
    if widest_gap - min(gaps) > _RULE_INTERVAL_TOL_PT:
        return False
    if len(merged) == 2:
        shortest, longest = min(lengths), max(lengths)
        return (
            shortest >= 3.0
            and longest - shortest <= _RULE_INTERVAL_TOL_PT
            and widest_gap <= longest * 2.5 + 2.0
        )

    core = lengths[1:-1]
    shortest_core, longest_core = min(core), max(core)
    max_end_length = longest_core + _RULE_INTERVAL_TOL_PT
    return (
        shortest_core >= 3.0
        and longest_core - shortest_core <= _RULE_INTERVAL_TOL_PT
        and 1.0 <= lengths[0] <= max_end_length
        and 1.0 <= lengths[-1] <= max_end_length
        and widest_gap <= longest_core * 2.5 + 2.0
    )


def _count_rule_clusters(segments: list[_RuleSegment]) -> tuple[int, list[float]]:
    clusters: list[tuple[float, list[tuple[float, float]]]] = []
    for coord, lo, hi in sorted(segments, key=lambda segment: segment[0]):
        if not clusters or coord - clusters[-1][0] > _COLLINEAR_TOL_PT:
            clusters.append((coord, []))
        clusters[-1][1].append((lo, hi))

    short_rules = 0
    long_coords: list[float] = []
    for anchor_coord, intervals in clusters:
        merged = _merge_rule_intervals(intervals)
        if _is_long_rule(merged):
            long_coords.append(anchor_coord)
        else:
            short_rules += len(merged)
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
            p1, p2, table_grids, horiz_segments, vert_segments, linewidth=_line_width(line)
        )

    for rect in page.rects:
        glyph_elements += _classify_rect(
            rect["x0"],
            rect["top"],
            rect["x1"],
            rect["bottom"],
            filled=bool(rect.get("fill")),
            table_grids=table_grids,
            horiz_segments=horiz_segments,
            vert_segments=vert_segments,
        )

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
