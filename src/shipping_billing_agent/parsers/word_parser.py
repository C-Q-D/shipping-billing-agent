"""Read Word paragraphs and tables without a desktop Office installation."""

from collections.abc import Iterator
from io import BytesIO
from pathlib import PurePath

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from legacy_doc.exceptions import LegacyDocError
from legacy_doc.ole import OleReader
from legacy_doc.types import ExtractionOptions
from legacy_doc.word import _find_clx, _u16, _u32

from shipping_billing_agent.config.limits import ResourceLimits
from shipping_billing_agent.domain.models import ParsedTable

_FC_LCB_PAPX = 13
_FC_LCB_CLX = 33
_FKP_BYTES = 512
_BX_PAP_BYTES = 13
_IN_TABLE = 0x2416
_ROW_END = 0x2417
_EXTERNAL_PAPX = {0x6646, 0x646B}
_FIXED_OPERAND_LENGTHS = (1, 1, 2, 4, 2, 2, 0, 3)
_CELL_MARKER = "\x07"


class WordParseError(ValueError):
    pass


def _read_docx(data: bytes) -> tuple[str, list[ParsedTable]]:
    with BytesIO(data) as stream:
        doc = Document(stream)
    lines: list[str] = []
    tables: list[ParsedTable] = []

    def append_table(block: Table, location: str) -> None:
        parsed = ParsedTable(rows=[], source=f"[{location} | Table {len(tables) + 1}]")
        tables.append(parsed)
        # Keep the cell proxies alive while tracking XML identities across merged rows.
        grids = [(row.grid_cols_before, row.cells, row.grid_cols_after) for row in block.rows]
        tc_text_cache: dict[int, str] = {}
        for before, cells, after in grids:
            row_values = [""] * before
            row_line = row_values.copy()
            prev_cell_id = None
            for cell in cells:
                cell_id = id(cell._tc)
                if cell_id == prev_cell_id:
                    row_values.append("")
                    row_line.append("")
                    continue
                prev_cell_id = cell_id
                if (cached_text := tc_text_cache.get(cell_id)) is not None:
                    row_values.append(cached_text)
                    row_line.append(cached_text)
                    continue

                paragraphs: list[str] = []
                line_paragraphs: list[str] = []
                for child in cell.iter_inner_content():
                    if isinstance(child, Paragraph):
                        paragraphs.append(child.text)
                        line_paragraphs.append(child.text)
                    else:
                        row_line.append("\n".join(line_paragraphs))
                        if any(value.strip() for value in row_line):
                            lines.append("\t".join(row_line))
                        append_table(child, location)
                        row_line = [""] * len(row_values)
                        line_paragraphs.clear()
                cell_text = "\n".join(paragraphs)
                tc_text_cache[cell_id] = cell_text
                row_values.append(cell_text)
                row_line.append("\n".join(line_paragraphs))
            parsed.rows.append(row_values + [""] * after)
            lines.append("\t".join(row_line + [""] * after))

    def append_blocks(blocks: Iterator[Paragraph | Table], location: str) -> None:
        for block in blocks:
            if isinstance(block, Paragraph):
                if block.text.strip():
                    lines.append(block.text)
                continue
            append_table(block, location)

    for number, section in enumerate(doc.sections, 1):
        for header in (section.header, section.first_page_header, section.even_page_header):
            if not header.is_linked_to_previous:
                append_blocks(header.iter_inner_content(), f"Section {number} header")
    append_blocks(doc.iter_inner_content(), "Body")
    for number, section in enumerate(doc.sections, 1):
        for footer in (section.footer, section.first_page_footer, section.even_page_footer):
            if not footer.is_linked_to_previous:
                append_blocks(footer.iter_inner_content(), f"Section {number} footer")
    return "\n".join(lines), tables


def _fib_layout(word: bytes) -> tuple[int, int, int]:
    if _u16(word, 0) != 0xA5EC:
        raise WordParseError("WordDocument 头部无效")
    long_start = 0x24 + _u16(word, 0x20) * 2
    long_count = _u16(word, long_start - 2)
    if long_count < 4:
        raise WordParseError("WordDocument 缺少有效的正文长度")
    body_chars = _u32(word, long_start + 3 * 4)
    pair_count_offset = long_start + long_count * 4
    return body_chars, pair_count_offset + 2, _u16(word, pair_count_offset)


def _table_segment(word: bytes, table: bytes, start: int, count: int, index: int) -> bytes:
    if index >= count:
        return b""
    offset = start + index * 8
    fc, length = _u32(word, offset), _u32(word, offset + 4)
    if fc + length > len(table):
        raise LegacyDocError("Word table stream range is out of bounds")
    return table[fc : fc + length]


def _read_sprms(properties: bytes) -> Iterator[tuple[int, bytes]]:
    offset = 0
    while offset + 2 < len(properties):
        code = _u16(properties, offset)
        offset += 2
        length = _FIXED_OPERAND_LENGTHS[code >> 13]
        if length == 0:
            if code == 0xD608:  # sprmTDefTable uses a two-byte size including one size byte.
                length = 1 + _u16(properties, offset)
            elif code == 0xC615 and properties[offset] == 255:  # sprmPChgTabs
                end_deleted = offset + 2 + 4 * properties[offset + 1]
                length = end_deleted - offset + 1 + 3 * properties[end_deleted]
            else:
                length = 1 + properties[offset]
        end = offset + length
        if end > len(properties):
            raise LegacyDocError("Invalid Word paragraph property size")
        yield code, properties[offset:end]
        offset = end


def _table_paragraph_properties(
    properties: bytes, data_stream: bytes, visited: frozenset[int] = frozenset()
) -> dict[int, int]:
    flags: dict[int, int] = {}
    for code, operand in _read_sprms(properties):
        if code in {_IN_TABLE, _ROW_END}:
            flags[code] = operand[0]
        elif code in _EXTERNAL_PAPX:
            start = _u32(operand, 0)
            if start in visited:
                raise LegacyDocError("Cyclic Word paragraph property reference")
            end = start + 2 + _u16(data_stream, start)
            if end > len(data_stream):
                raise LegacyDocError("Word paragraph properties exceed the Data stream")
            flags.update(
                _table_paragraph_properties(
                    data_stream[start + 2 : end], data_stream, visited | {start}
                )
            )
    return flags


def _papx_fcs(word: bytes, plc: bytes, data_stream: bytes) -> tuple[set[int], set[int]]:
    """Get row marker FCs and exclusive end FCs for ordinary table paragraphs."""
    if len(plc) < 12 or (len(plc) - 4) % 8:
        return set(), set()

    row_ends: set[int] = set()
    table_paragraph_ends: set[int] = set()
    page_count = (len(plc) - 4) // 8
    try:
        for i in range(page_count):
            page_number = _u32(plc, 4 * (page_count + 1 + i)) & 0x3FFFFF
            start = page_number * _FKP_BYTES
            page = word[start : start + _FKP_BYTES]
            if len(page) != _FKP_BYTES:
                raise LegacyDocError("PapxFkp is outside the WordDocument stream")
            count = page[-1]
            bx_start = (count + 1) * 4
            bx_end = bx_start + count * _BX_PAP_BYTES
            if not count or bx_end >= _FKP_BYTES - 1:
                raise LegacyDocError("Invalid PapxFkp paragraph count")
            for paragraph in range(count):
                papx = page[bx_start + paragraph * _BX_PAP_BYTES] * 2
                if papx == 0:
                    continue
                if not bx_end <= papx < _FKP_BYTES - 2:
                    raise LegacyDocError("Invalid PapxFkp property offset")
                size = page[papx]
                if size:
                    properties_start, end = papx + 3, papx + 2 * size
                else:
                    properties_start, end = papx + 4, papx + 2 + 2 * page[papx + 1]
                if not properties_start <= end < _FKP_BYTES:
                    raise LegacyDocError("Invalid PapxFkp property length")
                flags = _table_paragraph_properties(page[properties_start:end], data_stream)
                if flags.get(_ROW_END) == 1:
                    row_ends.add(_u32(page, 4 * paragraph))
                elif flags.get(_IN_TABLE) == 1:
                    table_paragraph_ends.add(_u32(page, 4 * (paragraph + 1)))
    except (LegacyDocError, IndexError, RecursionError):
        # Incomplete PAPX cannot prove positions in a run of empty cells.
        return set(), set()
    return row_ends, table_paragraph_ends


def _decode_pieces(
    word: bytes,
    pieces: bytes,
    body_chars: int,
    row_end_fcs: set[int],
    table_paragraph_end_fcs: set[int],
) -> tuple[str, set[int], set[int]]:
    count = (len(pieces) - 4) // 12
    pcd_start = 4 * (count + 1)
    if _u32(pieces, 0) != 0 or body_chars > _u32(pieces, count * 4):
        raise WordParseError("Word 正文的 piece table 字符范围无效")

    chunks: list[str] = []
    row_ends: set[int] = set()
    table_paragraphs: set[int] = set()
    offset = 0
    for index in range(count):
        cp_start = _u32(pieces, 4 * index)
        if cp_start >= body_chars:
            break
        cp_end = min(_u32(pieces, 4 * (index + 1)), body_chars)
        encoded_fc = _u32(pieces, pcd_start + 8 * index + 2)
        compressed = bool(encoded_fc & 0x40000000)
        width = 1 if compressed else 2
        fc = (encoded_fc & 0x3FFFFFFF) * width // 2
        end = fc + (cp_end - cp_start) * width
        if end > len(word):
            raise WordParseError("Word 正文的 piece table 文件偏移无效")
        text = word[fc:end].decode("cp1252" if compressed else "utf-16-le", errors="replace")
        chunks.append(text)
        for i, character in enumerate(text):
            character_end = fc + (4 if ord(character) > 0xFFFF else width)
            if character == _CELL_MARKER and fc in row_end_fcs:
                row_ends.add(offset + i)
            if character == "\r" and character_end in table_paragraph_end_fcs:
                table_paragraphs.add(offset + i)
            fc = character_end
        offset += len(text)
    return "".join(chunks), row_ends, table_paragraphs


def _display_characters(text: str) -> Iterator[tuple[int, str]]:
    fields: list[bool] = []
    for index, character in enumerate(text):
        if character == "\x13":
            fields.append(False)
        elif character == "\x14":
            if fields:
                fields[-1] = True
        elif character == "\x15":
            if fields:
                fields.pop()
        elif all(fields):
            yield index, character


def _legacy_blocks(
    text: str, row_ends: set[int], table_paragraphs: set[int]
) -> tuple[str, list[ParsedTable]]:
    row_ends = {i for i in row_ends if i > 0 and text[i - 1] == _CELL_MARKER}
    if not row_ends and _CELL_MARKER in text:
        raise WordParseError("缺少有效 PlcfBtePapx 行尾属性，无法确认 .doc 表格列位")

    lines: list[str] = []
    tables: list[ParsedTable] = []
    rows: list[list[str]] = []
    cells: list[str] = []
    characters: list[str] = []

    def end_paragraph() -> None:
        if rows:
            tables.append(ParsedTable(rows=rows.copy(), source=f"[Table {len(tables) + 1}]"))
            rows.clear()
        paragraph = "".join(characters).strip()
        if paragraph:
            lines.append(paragraph)
        characters.clear()

    for index, character in _display_characters(text):
        if character == _CELL_MARKER:
            if index in row_ends:
                if not cells or characters:
                    raise WordParseError("Word 表格行结束标记与单元格不一致")
                rows.append(cells.copy())
                lines.append("\t".join(cells))
                cells.clear()
            else:
                cells.append("".join(characters).strip())
                characters.clear()
        elif character in {"\r", "\x0c"} and not cells and index not in table_paragraphs:
            end_paragraph()
        elif character in {"\r", "\x0b", "\x0c"}:
            characters.append("\n")
        elif character.isprintable() or character in {"\t", "\n", "\u00a0"}:
            characters.append(character)
    if cells:
        raise WordParseError("Word 表格缺少可确认的行结束标记")
    end_paragraph()
    return "\n".join(lines), tables


def _read_doc(data: bytes, max_file_bytes: int) -> tuple[str, list[ParsedTable]]:
    ole = OleReader(data, options=ExtractionOptions(max_file_bytes=max_file_bytes))
    if ole.has_stream("EncryptedPackage"):
        raise WordParseError("不支持加密 .doc")
    word = ole.read_stream("WordDocument")
    flags = _u16(word, 0x0A)
    if flags & 0x0100:
        raise WordParseError("不支持加密 .doc")
    table = ole.read_stream("1Table" if flags & 0x0200 else "0Table")
    body_chars, start, count = _fib_layout(word)
    clx = _table_segment(word, table, start, count, _FC_LCB_CLX)
    pieces = _find_clx(clx or table)
    try:
        plc = _table_segment(word, table, start, count, _FC_LCB_PAPX)
    except LegacyDocError:
        plc = b""
    data_stream = ole.try_read_stream("Data") or b""
    row_fcs, table_paragraph_fcs = _papx_fcs(word, plc, data_stream)
    text, row_ends, table_paragraphs = _decode_pieces(
        word, pieces, body_chars, row_fcs, table_paragraph_fcs
    )
    return _legacy_blocks(text, row_ends, table_paragraphs)


def parse_word(
    data: bytes, filename: str, *, max_file_bytes: int = ResourceLimits().max_file_bytes
) -> tuple[str, list[ParsedTable]]:
    try:
        if PurePath(filename).suffix.lower() == ".doc":
            return _read_doc(data, max_file_bytes)
        return _read_docx(data)
    except WordParseError:
        raise
    except Exception as exc:
        raise WordParseError("无法读取 Word 文档") from exc
