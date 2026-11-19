"""Read worksheet grids and keep the original row references."""

import logging
from collections.abc import Iterable
from io import BytesIO
from pathlib import PurePath

import openpyxl
import xlrd

from shipping_billing_agent.domain.models import ParsedTable

_logger = logging.getLogger(__name__)
_MergedRange = tuple[int, int, int, int]  # Zero-based top, bottom, left, right; ends exclusive.


def _format_sheets(
    sheets: Iterable[tuple[str, list[list[str]], Iterable[_MergedRange]]],
) -> tuple[str, list[ParsedTable]]:
    lines: list[str] = []
    tables: list[ParsedTable] = []
    for name, grid, merged_ranges in sheets:
        # A single unmerged value above a multi-column header is usually the sheet title.
        populated_rows = [
            (i, sum(bool(cell.strip()) for cell in row))
            for i, row in enumerate(grid)
            if any(cell.strip() for cell in row)
        ]
        if not populated_rows:
            continue
        header = next((i for i, count in populated_rows if count >= 2), populated_rows[0][0])
        for top, bottom, left, right in merged_ranges:
            value = grid[top][left]
            for i in range(top, bottom):
                grid[i][left:right] = [value] * (right - left)

        table = ParsedTable(rows=[], source=f"[Sheet: {name} | Row {header + 1}]")
        for i, row in enumerate(grid):
            if not any(cell.strip() for cell in row):
                continue
            lines.append(f"[Sheet: {name} | Row {i + 1}] " + "\t".join(row))
            if i >= header:
                table.rows.append(row)
        tables.append(table)
    return "\n".join(lines), tables


def _parse_xlsx(data: bytes) -> tuple[str, list[ParsedTable]]:
    with BytesIO(data) as stream:
        book = openpyxl.load_workbook(stream, data_only=True, read_only=False)
        try:
            return _format_sheets(
                (
                    sheet.title,
                    [
                        ["" if value is None else str(value) for value in row]
                        for row in sheet.iter_rows(values_only=True)
                    ],
                    [
                        (r.min_row - 1, r.max_row, r.min_col - 1, r.max_col)
                        for r in sheet.merged_cells.ranges
                    ],
                )
                for sheet in book.worksheets
            )
        finally:
            book.close()


def _xls_cell_text(cell: xlrd.sheet.Cell, datemode: int) -> str:
    if cell.ctype == xlrd.XL_CELL_DATE:
        return str(xlrd.xldate_as_datetime(cell.value, datemode))
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return str(bool(cell.value))
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(cell.value, "#ERROR")
    return str(cell.value)


def _parse_xls(data: bytes) -> tuple[str, list[ParsedTable]]:
    try:
        book = xlrd.open_workbook(file_contents=data, formatting_info=True)
    except NotImplementedError:
        _logger.warning("此旧版 XLS 不支持读取合并格式，继续读取单元格值")
        book = xlrd.open_workbook(file_contents=data, formatting_info=False)
    try:
        return _format_sheets(
            (
                sheet.name,
                [
                    [_xls_cell_text(cell, book.datemode) for cell in sheet.row(i)]
                    for i in range(sheet.nrows)
                ],
                sheet.merged_cells,
            )
            for sheet in book.sheets()
        )
    finally:
        book.release_resources()


def parse_excel(data: bytes, filename: str) -> tuple[str, list[ParsedTable]]:
    if PurePath(filename).suffix.lower() == ".xls":
        return _parse_xls(data)
    return _parse_xlsx(data)
