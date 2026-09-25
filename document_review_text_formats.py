"""Bounded standard-library readers for delimited text, HTML and RTF.

These readers extract reviewable content without fetching URLs or executing
embedded content. Source bytes and positions remain bound to the uploaded file.
RTF follows the control/group rules of Microsoft's published RTF specification;
unsupported destinations are reported rather than rendered as apparent prose.
"""
from __future__ import annotations

import codecs
import csv
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
import io
from pathlib import Path
import re

from document_review_model import (
    DocumentBlock, DocumentLocation, ExtractionWarning, QualitySignals,
    RawFileBinding, StructuredDocument, stable_id,
)

MAX_BLOCKS = 50_000
MAX_DEPTH = 128
MAX_TABLE_COLUMNS = 1_000
MAX_TABLE_ROWS = 50_000
MAX_SPAN = 1_000
PARSER_VERSION = "text-formats-v1"


def _fail(message):
    from document_review_ingest import IngestionError
    raise IngestionError(message)


@dataclass
class _Cell:
    text: str
    line: int
    rowspan: int = 1
    colspan: int = 1
    header: bool = False


class _DocumentBuilder:
    def __init__(self, source: RawFileBinding, parser: str):
        self.source = source
        self.parser = parser
        self.blocks = []
        self.mapping = []
        self.warnings = []
        self.metadata = {}
        self.title = ""
        self.table_slots = 0

    def warn(self, code, message, *, severity="medium", **details):
        if not any(w.code == code for w in self.warnings):
            self.warnings.append(ExtractionWarning(code, severity, message, details=details))

    def add(self, kind, text, *, line=None, level=None, table_id=None, row=None, column=None, attrs=None):
        from document_review_ingest import _block_id
        if len(self.blocks) >= MAX_BLOCKS:
            _fail("文档文本块或表格单元格过多，拒绝不完整截断")
        ordinal = len(self.blocks)
        block_id = _block_id(self.source.sha256, kind, ordinal, text, line, table_id, row, column)
        location = DocumentLocation(block_id, kind, paragraph=ordinal, table_id=table_id,
                                    row=row, column=column, source_path=self.source.original_name)
        properties = {**(attrs or {}), **({"source_line": line} if line is not None else {})}
        block = DocumentBlock(block_id, kind, text=text, level=level, location=location, attrs=properties)
        self.blocks.append(block)
        self.mapping.append({"block_id": block_id, "kind": kind, "source_line": line,
                             "table_id": table_id, "row": row, "column": column})
        return block

    def table(self, rows, *, line=1, caption=""):
        if not rows:
            return
        if caption:
            self.add("paragraph", caption, line=line, attrs={"html_tag": "caption"})
        table = self.add("table", "", line=line, attrs={"rows": [], "caption": caption})
        table.location = replace(table.location, table_id=table.block_id)
        self.mapping[-1]["table_id"] = table.block_id
        grid = []
        occupied = set()
        width = 0

        def check_extent(height, columns):
            # Padding ragged rows and sparse spans also allocates cells. Count
            # the complete rectangle, across all tables, before growing it.
            if self.table_slots + height * columns > MAX_BLOCKS:
                _fail("表格补齐后的总单元格数量超过安全上限")

        for row_index, cells in enumerate(rows):
            if row_index >= MAX_TABLE_ROWS:
                _fail("表格行数超过安全上限")
            column = 0
            check_extent(max(len(grid), row_index + 1), width)
            while len(grid) <= row_index:
                grid.append([])
            for cell in cells:
                while (row_index, column) in occupied:
                    column += 1
                if column + cell.colspan > MAX_TABLE_COLUMNS or row_index + cell.rowspan > MAX_TABLE_ROWS:
                    _fail("表格列数或跨行范围超过安全上限")
                if len(occupied) + cell.rowspan * cell.colspan > MAX_BLOCKS:
                    _fail("表格展开后的单元格过多")
                width = max(width, column + cell.colspan)
                check_extent(max(len(grid), row_index + cell.rowspan), width)
                for r in range(row_index, row_index + cell.rowspan):
                    while len(grid) <= r:
                        grid.append([])
                    while len(grid[r]) < column + cell.colspan:
                        grid[r].append("")
                    for c in range(column, column + cell.colspan):
                        if (r, c) in occupied:
                            _fail("表格合并单元格范围重叠，无法可靠定位")
                        occupied.add((r, c))
                grid[row_index][column] = cell.text
                block = self.add("table_cell", cell.text, line=cell.line, table_id=table.block_id,
                                 row=row_index, column=column,
                                 attrs={"row_span": cell.rowspan, "grid_span": cell.colspan, "header": cell.header})
                table.children.append(block.block_id)
                column += cell.colspan
        for row in grid:
            row.extend([""] * (width - len(row)))
        self.table_slots += len(grid) * width
        table.attrs["rows"] = grid

    def finish(self):
        tables = sum(block.kind == "table" for block in self.blocks)
        return StructuredDocument(
            stable_id("DOC", self.source.sha256),
            self.title or next((b.text for b in self.blocks if b.kind == "heading"), Path(self.source.original_name).stem),
            self.source, self.parser, PARSER_VERSION, self.blocks, self.warnings,
            QualitySignals(page_count=1, text_coverage=float(any(b.text.strip() for b in self.blocks)),
                           table_count=tables, tables_parsed=tables, requires_confirmation=True,
                           footnote_comment_revision_risk=[w.code for w in self.warnings if w.severity == "high"]),
            self.mapping, self.metadata,
        )


class _CharsetSniffer(HTMLParser):
    def __init__(self):
        super().__init__()
        self.encoding = None

    def handle_starttag(self, tag, attrs):
        if tag != "meta" or self.encoding:
            return
        values = dict(attrs)
        self.encoding = values.get("charset")
        if not self.encoding and (values.get("http-equiv") or "").lower() == "content-type":
            match = re.search(r"charset\s*=\s*([\w-]+)", values.get("content") or "", re.I)
            if match:
                self.encoding = match.group(1)


def _decode(data, builder, encoding, *, html=False):
    from document_text_encoding import decode_document_text, normalize_encoding, TextDecodingError
    try:
        encoding = normalize_encoding(encoding)
    except TextDecodingError as exc:
        _fail(str(exc))
    declared = None
    if html and encoding is None and not data.startswith((codecs.BOM_UTF8, codecs.BOM_UTF16_LE,
                                                        codecs.BOM_UTF16_BE, codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        sniffer = _CharsetSniffer()
        sniffer.feed(data[:8192].decode("latin-1"))
        declared = sniffer.encoding
    try:
        decoded = decode_document_text(data, encoding=encoding or declared)
    except TextDecodingError as exc:
        _fail(str(exc))
    builder.metadata.update(encoding=decoded.encoding, encoding_ambiguous=decoded.ambiguous,
                            encoding_candidates=list(decoded.candidates))
    if declared:
        builder.metadata["declared_html_encoding"] = declared
    if decoded.ambiguous:
        builder.warn("text-encoding-ambiguous", "文件存在多种有效字符编码，请核对正文并明确选择编码后重新解析。",
                     severity="high", candidates=list(decoded.candidates), selected=decoded.encoding)
    return decoded.text


def _parse_delimited(data, source, encoding):
    builder = _DocumentBuilder(source, "delimited-text")
    text = _decode(data, builder, encoding)
    delimiter = "\t" if source.extension.lower() == ".tsv" else ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    rows = []
    cells = 0
    try:
        while True:
            start_line = reader.line_num + 1
            try:
                row = next(reader)
            except StopIteration:
                break
            cells += len(row)
            if cells >= MAX_BLOCKS or len(rows) >= MAX_TABLE_ROWS or len(row) > MAX_TABLE_COLUMNS:
                _fail("分隔文本表格超过行、列或单元格安全上限")
            rows.append([_Cell(value, start_line) for value in row])
    except csv.Error as exc:
        _fail(f"分隔文本格式无效或单元格过长（第 {reader.line_num} 行）：{exc}")
    widths = {len(row) for row in rows}
    if len(widths) > 1:
        builder.warn("delimited-ragged-rows", "表格各行列数不一致，缺少的位置保留为空；请核对原始行。")
    builder.table(rows)
    builder.metadata.update(delimiter=delimiter, row_count=len(rows), source_line_count=reader.line_num)
    return builder.finish()


_HTML_HIDDEN = {"head", "title", "script", "style", "template", "noscript"}
_HTML_EMBEDDED = {"object", "embed", "iframe", "svg", "canvas", "audio", "video"}
_HTML_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_HTML_BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "address", "blockquote", "pre", "li", "ul", "ol", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6"}


@dataclass
class _HTMLTable:
    line: int
    rows: list = field(default_factory=list)
    cells: list = field(default_factory=list)
    cell: _Cell | None = None
    text: list = field(default_factory=list)
    caption: list = field(default_factory=list)
    depth: int = 1

    def end_cell(self):
        if self.cell is not None:
            self.cell.text = re.sub(r"[ \t\r\f\v]+", " ", "".join(self.text)).strip()
            self.cells.append(self.cell)
        self.cell, self.text = None, []

    def end_row(self):
        self.end_cell()
        if self.cells:
            self.rows.append(self.cells)
        self.cells = []


class _HTMLReader(HTMLParser):
    def __init__(self, builder):
        super().__init__(convert_charrefs=True)
        self.builder = builder
        self.stack = []
        self.pending = []
        self.line = 1
        self.table = None
        self.title_parts = []
        self.elements = 0

    @property
    def hidden(self):
        return bool(self.stack and self.stack[-1][1])

    def flush(self):
        text = "".join(self.pending)
        self.pending = []
        tags = [tag for tag, _ in self.stack]
        preserve = "pre" in tags
        text = text.strip() if preserve else re.sub(r"[^\S\n]+", " ", text).strip()
        if not text:
            return
        block = next((tag for tag in reversed(tags) if tag in _HTML_BLOCK), "p")
        kind = "heading" if re.fullmatch("h[1-6]", block) else "list_item" if block == "li" else "blockquote" if block == "blockquote" else "paragraph"
        ordered = next((tag == "ol" for tag in reversed(tags) if tag in {"ol", "ul"}), False)
        self.builder.add(kind, text, line=self.line, level=int(block[1]) if kind == "heading" else None,
                         attrs={"html_tag": block, "preserve_whitespace": preserve, "ordered": ordered})

    def handle_starttag(self, tag, attrs):
        # HTML permits omitted end tags. Close common optional elements at
        # structural boundaries instead of accumulating fictitious nesting.
        if (tag == "body" and any(name == "head" for name, _ in self.stack)
                and not any(name in (_HTML_HIDDEN - {"head", "title"}) | _HTML_EMBEDDED for name, _ in self.stack)):
            self.handle_endtag("head")
        if not self.hidden:
            if tag in _HTML_BLOCK | {"table", "hr"} and any(name == "p" for name, _ in self.stack):
                self.handle_endtag("p")
            alternatives = {"li": ({"li"}, {"ul", "ol"}), "dt": ({"dt", "dd"}, {"dl"}),
                            "dd": ({"dt", "dd"}, {"dl"}), "tr": ({"tr"}, {"table"}),
                            "td": ({"td", "th"}, {"tr", "table"}), "th": ({"td", "th"}, {"tr", "table"})}
            if tag in alternatives:
                candidates, boundaries = alternatives[tag]
                for name, _ in reversed(self.stack):
                    if name in boundaries:
                        break
                    if name in candidates:
                        self.handle_endtag(name)
                        break
        self.elements += 1
        if self.elements > MAX_BLOCKS * 4 or len(self.stack) >= MAX_DEPTH:
            _fail("HTML 元素数量或嵌套深度超过安全上限")
        values = dict(attrs)
        style = re.sub(r"/\*.*?\*/", "", values.get("style") or "", flags=re.S)
        hidden = (self.hidden or tag in _HTML_HIDDEN or tag in _HTML_EMBEDDED or "hidden" in values
                  or (values.get("aria-hidden") or "").lower() == "true"
                  or bool(re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\b", style, re.I)))
        if tag in _HTML_EMBEDDED:
            self.builder.warn("html-embedded-omitted", "HTML 中的嵌入对象、画布或媒体内容未执行或解析；请另行提供需要审查的正文。", severity="high")
        if not hidden:
            self._start_visible(tag, values)
        if tag not in _HTML_VOID:
            self.stack.append((tag, hidden))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _HTML_VOID:
            self.handle_endtag(tag)

    def _start_visible(self, tag, attrs):
        if tag == "table":
            if self.table:
                self.table.depth += 1
                self.builder.warn("html-nested-table-linearized", "嵌套 HTML 表格已在外层单元格中按文本展开，内部表格定位未保留。", severity="high")
            else:
                self.flush()
                self.table = _HTMLTable(self.getpos()[0])
        elif self.table:
            table = self.table
            if table.depth == 1 and tag == "tr":
                table.end_row()
            elif table.depth == 1 and tag in {"td", "th"}:
                table.end_cell()
                def span(name):
                    value = attrs.get(name) or "1"
                    if len(value) > 4 or not value.isdecimal() or not 1 <= int(value) <= MAX_SPAN:
                        _fail("HTML 表格跨行或跨列值无效或过大")
                    return int(value)
                table.cell = _Cell("", self.getpos()[0], span("rowspan"), span("colspan"), tag == "th")
            elif tag in _HTML_BLOCK or tag in {"br", "tr", "td", "th"}:
                table.text.append("\n")
        elif tag in _HTML_BLOCK or tag == "hr":
            self.flush()
            self.line = self.getpos()[0]
        elif tag == "br":
            self.pending.append("\n")
        if tag == "img":
            self.builder.warn("html-images-omitted", "HTML 图片未下载或识别，仅保留已有替代文字；图片正文需要另行提供。", severity="high")
            if attrs.get("alt"):
                self.handle_data("[图片：" + attrs["alt"] + "]")

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if index is None:
            return
        if self.hidden:
            boundaries = [i for i, (name, hidden) in enumerate(self.stack)
                          if name in (_HTML_HIDDEN - {"head", "title"}) | _HTML_EMBEDDED
                          or (hidden and (i == 0 or not self.stack[i - 1][1]))]
            if boundaries and index < max(boundaries):
                return
        hidden = self.stack[index][1]
        if not hidden:
            if self.table:
                if tag == "table":
                    self.table.depth -= 1
                    if self.table.depth == 0:
                        self.table.end_row()
                        self.builder.table(self.table.rows, line=self.table.line,
                                           caption=re.sub(r"\s+", " ", "".join(self.table.caption)).strip())
                        self.table = None
                elif self.table.depth == 1 and tag in {"td", "th"}:
                    self.table.end_cell()
                elif self.table.depth == 1 and tag == "tr":
                    self.table.end_row()
                elif tag in _HTML_BLOCK:
                    self.table.text.append("\n")
            elif tag in _HTML_BLOCK:
                self.flush()
        del self.stack[index:]

    def handle_data(self, text):
        tags = {tag for tag, _ in self.stack}
        if (self.stack and self.stack[-1][0] == "title"
                and not any(hidden and tag != "head" for tag, hidden in self.stack[:-1])):
            self.title_parts.append(text)
            return
        if self.hidden:
            return
        if "pre" not in tags:
            text = re.sub(r"\s+", " ", text)
        if self.table:
            if self.table.cell is not None:
                self.table.text.append(text)
            elif any(tag == "caption" for tag, _ in self.stack):
                self.table.caption.append(text)
        else:
            if not self.pending:
                self.line = self.getpos()[0]
            self.pending.append(text)

    def finish(self):
        self.close()
        if self.table:
            self.table.end_row()
            self.builder.table(self.table.rows, line=self.table.line)
            self.builder.warn("html-unclosed-table", "HTML 表格没有完整闭合，已按可识别的行和单元格提取，请核对。")
        self.flush()
        self.builder.title = re.sub(r"\s+", " ", "".join(self.title_parts)).strip()


def _parse_html(data, source, encoding):
    builder = _DocumentBuilder(source, "html-text")
    text = _decode(data, builder, encoding, html=True)
    reader = _HTMLReader(builder)
    reader.feed(text)
    reader.finish()
    builder.warn("html-static-extraction", "已提取静态 HTML 正文；脚本、样式、隐藏模板和外部资源不执行或加载，页面布局不作为审查依据。")
    builder.metadata.update(network_access=False, rendered_layout=False)
    return builder.finish()


_RTF_DESTINATIONS = {
    "fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "objdata", "datastore",
    "themedata", "colorschememapping", "listtable", "listoverridetable", "rsidtbl", "generator",
    "xmlnstbl", "header", "headerl", "headerr", "headerf", "footer", "footerl", "footerr", "footerf",
    "footnote", "annotation", "atnauthor", "atndate", "atnid", "fldinst", "datafield", "nonshppict",
    "shppict", "shp", "shpinst", "shprslt", "xmlopen", "xmlclose", "htmltag", "filetbl", "revtbl",
}
_RTF_CONTENT_OMITTED = {"pict", "object", "objdata", "shp", "shppict", "nonshppict", "header", "headerl", "headerr", "headerf", "footer", "footerl", "footerr", "footerf", "footnote", "annotation"}
_RTF_CHARSETS = {0: None, 1: None, 2: None, 77: "mac_roman", 128: "cp932", 129: "cp949", 130: "cp1361", 134: "gbk", 136: "big5", 161: "cp1253", 162: "cp1254", 163: "cp1258", 177: "cp1255", 178: "cp1256", 186: "cp1257", 204: "cp1251", 222: "cp874", 238: "cp1250", 255: "cp437"}
_RTF_CHARACTERS = {"par": "\n", "line": "\n", "page": "\n\n", "tab": "\t", "cell": "\t", "row": "\n", "emdash": "—", "endash": "–", "bullet": "•", "lquote": "‘", "rquote": "’", "ldblquote": "“", "rdblquote": "”", "emspace": "\u2003", "enspace": "\u2002"}


@dataclass
class _RTFState:
    skip: bool = False
    destination: str = ""
    hidden: bool = False
    uc: int = 1
    encoding: str = "cp1252"
    font: int | None = None
    ignorable: bool = False
    alternate: bool = False
    children: int = 0
    unicode_destination: bool = False


class _RTFReader:
    def __init__(self, builder, encoding=None):
        self.builder = builder
        self.state = _RTFState()
        self.stack = []
        self.parts = []
        self.bytes = bytearray()
        self.fallback = 0
        self.fonts = {}
        self.default_font = None
        self.default_encoding = "cp1252"
        from document_text_encoding import normalize_encoding, TextDecodingError
        try:
            self.override_encoding = normalize_encoding(encoding)
        except TextDecodingError as exc:
            _fail(str(exc))
        self.encodings = set()
        self.omitted = set()
        self.tokens = 0
        if self.override_encoding:
            self.default_encoding = self.state.encoding = self.override_encoding

    @staticmethod
    def _codec(value):
        try:
            codec = codecs.lookup(value).name
            # Only character encodings are useful for byte runs; reject transform codecs.
            b"A".decode(codec)
            return codec
        except (LookupError, UnicodeError, TypeError):
            _fail(f"RTF 字符编码不可用：{value}")

    def flush(self):
        if self.bytes:
            try:
                self.parts.append(self.bytes.decode(self.state.encoding))
            except UnicodeDecodeError:
                _fail(f"RTF 文本字节不符合 {self.state.encoding} 编码，请选择正确编码重新解析")
            self.encodings.add(self.state.encoding)
            self.bytes.clear()

    def byte(self, value):
        if self.fallback:
            self.fallback -= 1
        elif not self.state.skip and not self.state.hidden:
            self.bytes.append(value)

    def text(self, value):
        self.flush()
        if not self.state.skip and not self.state.hidden:
            self.parts.append(value)

    def control(self, word, number):
        state = self.state
        if state.ignorable:
            if not (word == "ud" and state.unicode_destination):
                state.skip = True
                if word not in _RTF_DESTINATIONS:
                    self.omitted.add(word)
            state.ignorable = False
        if word in _RTF_DESTINATIONS:
            state.destination, state.skip = word, True
            if word in _RTF_CONTENT_OMITTED:
                self.omitted.add(word)
        if word in {"f", "deff"} and number is not None:
            state.font = number
            if word == "deff":
                self.default_font = number
            if not state.skip and not self.override_encoding:
                state.encoding = self.fonts.get(number, self.default_encoding)
        elif word == "fcharset" and state.destination == "fonttbl" and number is not None:
            if number not in _RTF_CHARSETS or number == 2:
                self.builder.warn("rtf-font-charset", "RTF 包含无法可靠映射的字体字符集，请核对符号字形。", severity="high")
            self.fonts[state.font] = _RTF_CHARSETS.get(number) or self.default_encoding
        elif word in {"ansicpg", "cpg"} and number is not None:
            if number not in {437, 850, 874, 932, 936, 949, 950, 1361, 10000, 65001, *range(1250, 1259)}:
                _fail(f"RTF 声明了暂不支持的代码页 {number}，请转换为 Unicode RTF 或文本")
            codec = self._codec({65001: "utf-8", 10000: "mac_roman"}.get(number, f"cp{number}"))
            if word == "cpg" and state.destination == "fonttbl":
                self.fonts[state.font] = codec
            elif not self.override_encoding:
                self.default_encoding = state.encoding = codec
        elif word in {"ansi", "mac", "pc", "pca"} and not self.override_encoding:
            self.default_encoding = state.encoding = {"ansi": "cp1252", "mac": "mac_roman", "pc": "cp437", "pca": "cp850"}[word]
        elif word == "uc":
            if number is None or not 0 <= number <= 32:
                _fail("RTF Unicode 回退长度无效或过大")
            state.uc = number
        elif word == "u":
            if number is None or not -32768 <= number <= 65535:
                _fail("RTF Unicode 控制值无效")
            self.text(chr(number & 0xFFFF))
            self.fallback = state.uc
        elif word == "upr":
            state.alternate, state.children = True, 0
        elif word == "plain":
            state.hidden = False
            state.font = self.default_font
            state.encoding = self.override_encoding or self.fonts.get(self.default_font, self.default_encoding)
        elif word in {"v", "deleted"}:
            state.hidden = number != 0
            if word == "deleted":
                self.builder.warn("rtf-revisions", "RTF 包含修订删除文本；正文提取忽略删除内容，请另行核对修订历史。", severity="high")
        elif word in {"trowd", "cell", "row", "nestcell", "nestrow"}:
            self.builder.warn("rtf-table-linearized", "RTF 表格按行和单元格分隔提取文本，表格结构与合并定位未恢复。", severity="high")
            if word in _RTF_CHARACTERS:
                self.text(_RTF_CHARACTERS[word])
        elif word in _RTF_CHARACTERS:
            self.text(_RTF_CHARACTERS[word])

    def parse(self, data):
        data = data.lstrip(b" \t\r\n")
        if not re.match(rb"\{\\rtf1(?=[\\ \r\n])", data):
            _fail("文件不是有效的 RTF 1 文档")
        i = 0
        closed = False
        while i < len(data):
            value = data[i]
            if closed:
                if data[i:].strip():
                    _fail("RTF 根组结束后仍有额外内容")
                break
            if value in (123, 125):
                self.tokens += 1
                if self.tokens > MAX_BLOCKS * 4:
                    _fail("RTF 控制符或分组数量超过安全上限")
                self.flush()
                self.fallback = 0
                if value == 123:
                    if len(self.stack) >= MAX_DEPTH:
                        _fail("RTF 分组嵌套超过安全上限")
                    parent = self.state
                    parent.children += 1
                    self.stack.append(parent)
                    self.state = replace(parent, children=0, alternate=False, ignorable=False,
                                         unicode_destination=parent.alternate and parent.children == 2,
                                         skip=parent.skip or (parent.alternate and parent.children == 1))
                else:
                    if not self.stack:
                        _fail("RTF 分组闭合无效")
                    self.state = self.stack.pop()
                    if not self.state.skip and not self.override_encoding and self.state.font in self.fonts:
                        self.state.encoding = self.fonts[self.state.font]
                    closed = not self.stack
                i += 1
                continue
            if value != 92:
                if value not in (10, 13):
                    if value < 32 and value != 9:
                        _fail("RTF 正文含未声明的二进制字节")
                    # A raw DBCS character can contain a trailing backslash or
                    # brace byte; consume its complete code unit before lexing.
                    if value >= 128 and not self.state.skip:
                        decoder = codecs.getincrementaldecoder(self.state.encoding)(errors="strict")
                        length = 0
                        try:
                            while i + length < len(data) and length < 4:
                                length += 1
                                if decoder.decode(data[i + length - 1:i + length], final=False):
                                    break
                            else:
                                _fail("RTF 多字节字符被截断")
                        except UnicodeDecodeError:
                            _fail(f"RTF 文本字节不符合 {self.state.encoding} 编码")
                        for byte in data[i:i + length]:
                            self.byte(byte)
                        i += length
                        continue
                    self.byte(value)
                i += 1
                continue
            i += 1
            self.tokens += 1
            if self.tokens > MAX_BLOCKS * 4:
                _fail("RTF 控制符或分组数量超过安全上限")
            if i >= len(data):
                _fail("RTF 控制序列不完整")
            char = data[i]
            if char == 39:
                if i + 2 >= len(data) or not re.fullmatch(rb"[a-fA-F0-9]{2}", data[i + 1:i + 3]):
                    _fail("RTF 十六进制字符转义无效")
                self.byte(int(data[i + 1:i + 3], 16))
                i += 3
                continue
            if char in (92, 123, 125):
                self.byte(char)
                i += 1
                continue
            self.flush()
            if not (65 <= char <= 90 or 97 <= char <= 122):
                i += 1
                if self.fallback:
                    self.fallback -= 1
                elif char == 42:
                    self.state.ignorable = True
                elif char in (126, 95, 45):
                    self.text({126: "\u00a0", 95: "\u2011", 45: "\u00ad"}[char])
                elif char in (10, 13):
                    self.text("\n")
                continue
            start = i
            while i < len(data) and (65 <= data[i] <= 90 or 97 <= data[i] <= 122):
                i += 1
            if i - start > 32:
                _fail("RTF 控制字过长")
            word = data[start:i].decode("ascii")
            start = i
            if i < len(data) and data[i] == 45:
                i += 1
            while i < len(data) and 48 <= data[i] <= 57:
                i += 1
            numeric = data[start:i]
            if len(numeric) > 11 or numeric == b"-":
                _fail("RTF 控制字参数无效")
            number = int(numeric) if numeric else None
            if i < len(data) and data[i] == 32:
                i += 1
            if word == "bin":
                if number is None or number < 0 or i + number > len(data):
                    _fail("RTF 二进制区长度无效或内容截断")
                if self.fallback:
                    self.fallback -= 1
                else:
                    self.omitted.add("binary")
                i += number
            elif self.fallback:
                self.fallback -= 1
            else:
                self.control(word, number)
        if self.stack or not closed:
            _fail("RTF 分组不完整，拒绝把截断正文当作成功解析")
        self.flush()
        try:
            text = "".join(self.parts).encode("utf-16-le", "surrogatepass").decode("utf-16-le")
        except UnicodeError:
            _fail("RTF Unicode 代理字符不完整，拒绝乱码正文")
        if re.search(r"[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f\ufffe\uffff]", text):
            _fail("RTF 提取正文包含不可显示的控制字符")
        if self.omitted:
            self.builder.warn("rtf-content-omitted", "RTF 图片、对象、页眉页脚、注释、二进制区域或不支持的目标组中存在未提取内容，请检查遗漏并单独提供需要审查的材料。",
                              severity="high", destinations=sorted(self.omitted))
        self.builder.metadata.update(encoding=self.default_encoding, encoding_ambiguous=False,
                                     encoding_candidates=[], rtf_text_encodings=sorted(self.encodings),
                                     omitted_destinations=sorted(self.omitted))
        return text


def _parse_rtf(data, source, encoding):
    builder = _DocumentBuilder(source, "rtf-text")
    text = _RTFReader(builder, encoding).parse(data)
    for line, value in enumerate(text.splitlines(), 1):
        if value.strip():
            builder.add("paragraph", value.strip(), attrs={"extracted_line": line})
    builder.warn("rtf-text-layout", "RTF 已提取正文和字符，字体、分页、复杂版式不保留；原文件保持不变。")
    return builder.finish()


def parse_text_format(data, source, limits, *, encoding=None) -> StructuredDocument:
    """Parse CSV/TSV, HTML or RTF without evaluating formulas or active content."""
    if not isinstance(data, bytes) or len(data) > limits.max_file_bytes:
        _fail("文件字节数超过解析安全上限")
    extension = source.extension.lower()
    if extension in {".csv", ".tsv"}:
        return _parse_delimited(data, source, encoding)
    if extension in {".html", ".htm"}:
        return _parse_html(data, source, encoding)
    if extension == ".rtf":
        return _parse_rtf(data, source, encoding)
    _fail(f"文本格式解析器不支持 {extension}")


__all__ = ["parse_text_format"]
