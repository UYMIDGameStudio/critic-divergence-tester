"""Bounded, read-only text/structure extraction for modern Office packages.

No Office application, formula evaluator, macro interpreter or network client
is involved. Relationship targets determine workbook/presentation order; the
original package remains the immutable source, not an editable round-trip IR.
"""
from __future__ import annotations

import posixpath
import re
import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from document_review_model import (
    DocumentBlock, DocumentLocation, ExtractionWarning, QualitySignals,
    RawFileBinding, StructuredDocument, stable_id,
)

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
MANIFEST = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
XLINK = "http://www.w3.org/1999/xlink"
NS = {"s": S, "p": P, "a": A, "r": R, "o": OFFICE, "t": TEXT, "table": TABLE}
STRICT_NS = {
    "http://purl.oclc.org/ooxml/spreadsheetml/main": S,
    "http://purl.oclc.org/ooxml/presentationml/main": P,
    "http://purl.oclc.org/ooxml/drawingml/main": A,
    "http://purl.oclc.org/ooxml/officeDocument/relationships": R,
}


def _fail(message):
    from document_review_ingest import IngestionError
    raise IngestionError(message)


def _tag(namespace, name):
    return "{" + namespace + "}" + name


def _integer(value, label, *, minimum=0, maximum=None):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}", value):
        _fail(label + "必须是有效整数")
    number = int(value)
    if number < minimum or (maximum is not None and number > maximum):
        _fail(label + "超出安全限制")
    return number


def _xml(archive, name, *, required=True):
    from document_review_ingest import _docx_xml
    root = _docx_xml(archive, name)
    if root is None:
        if required:
            _fail("Office 包缺少必要部件：" + name)
        return None
    # Strict OOXML uses alternate namespace URIs, with equivalent read-only
    # text structures. Normalize the parsed tree, never the uploaded bytes.
    def normalized(name):
        if name.startswith("{"):
            namespace, local = name[1:].split("}", 1)
            return _tag(STRICT_NS.get(namespace, namespace), local)
        return name
    pending = [(root, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 128:
            _fail("Office XML 嵌套深度超出安全限制")
        node.tag = normalized(node.tag)
        node.attrib = {normalized(key): value for key, value in node.attrib.items()}
        pending.extend((child, depth + 1) for child in node)
    return root


@dataclass(frozen=True)
class _Limits:
    rows: int
    columns: int
    cells: int
    slides: int
    sheets: int
    text_chars: int

    @classmethod
    def from_ingestion(cls, limits):
        fields = {"rows": 10000, "columns": 1000, "cells": 50000, "slides": 1000, "sheets": 200, "text_chars": 8000000}
        values = {}
        for name, default in fields.items():
            value = getattr(limits, "max_office_" + name, default)
            if type(value) is not int or value < 1:
                _fail("Office 资源限制必须为正整数：" + name)
            values[name] = value
        return cls(**values)


class _Document:
    def __init__(self, source, limits, parser, *, identity_sha256=None):
        self.source, self.limits, self.parser = source, limits, parser
        self.identity_sha256 = identity_sha256 if identity_sha256 is not None else source.sha256
        self.blocks, self.mapping, self.warnings = [], [], []
        self.text_chars, self.cell_count = 0, 0
        self.metadata = {"extraction_only": True, "original_format_export_supported": False,
                         "macros_executed": False, "formulas_recalculated": False, "external_links_fetched": False}
        self.warn("office-text-extraction", "仅提取可审查文本和结构定位；原格式排版、图表和交互内容不保证完整，导出为规范化文本副本。", "medium")

    def warn(self, code, message, severity="medium", *, location=None):
        existing = next((item for item in self.warnings if item.code == code), None)
        if existing:
            existing.details["occurrences"] += 1
            return
        self.warnings.append(ExtractionWarning(code, severity, message, location, {"occurrences": 1}))

    def add(self, kind, text, *, level=None, page=None, attrs=None, table_id=None, row=None, column=None):
        self.text_chars += len(text)
        if self.text_chars > self.limits.text_chars:
            _fail("Office 文本展开量超出安全限制")
        if len(self.blocks) >= self.limits.cells + self.limits.slides + self.limits.sheets:
            _fail("Office 文本块数量超出安全限制")
        ordinal = len(self.blocks)
        identifier = stable_id("B", self.identity_sha256, kind, ordinal, text, table_id, row, column)
        location = DocumentLocation(identifier, kind, page=page, paragraph=ordinal, table_id=table_id, row=row, column=column, source_path=self.source.original_name)
        block = DocumentBlock(identifier, kind, text, level, location, attrs or {})
        self.blocks.append(block)
        self.mapping.append(location.to_dict())
        return block

    def check_table(self, rows, columns):
        if rows > self.limits.rows or columns > self.limits.columns or self.cell_count + rows * columns > self.limits.cells:
            _fail("Office 表格展开行数、列数或单元格数量超出安全限制")

    def table(self, cells, rows, columns, *, page=None, attrs=None):
        self.check_table(rows, columns)
        self.cell_count += rows * columns
        values = [[cells.get((row, column), ("", {}))[0] for column in range(columns)] for row in range(rows)]
        table = self.add("table", "", page=page, attrs={**(attrs or {}), "rows": values})
        table.location = DocumentLocation(table.block_id, "table", page=page, paragraph=table.location.paragraph, table_id=table.block_id, source_path=self.source.original_name)
        self.mapping[-1] = table.location.to_dict()
        for row, values_row in enumerate(values):
            for column, text in enumerate(values_row):
                cell_attrs = cells.get((row, column), ("", {"empty_padding": True}))[1]
                block = self.add("table_cell", text, page=page, attrs={**(attrs or {}), **cell_attrs, "row": row, "column": column}, table_id=table.block_id, row=row, column=column)
                table.children.append(block.block_id)
        return table

    def finish(self, *, pages=0, reading_order=False):
        table_count = sum(block.kind == "table" for block in self.blocks)
        quality = QualitySignals(page_count=pages, text_coverage=1.0 if any(block.text.strip() for block in self.blocks) else 0.0,
                                 table_count=table_count, tables_parsed=table_count, suspected_reading_order=reading_order,
                                 requires_confirmation=True)
        title = next((block.text for block in self.blocks if block.kind == "heading"), Path(self.source.original_name).stem)
        return StructuredDocument(stable_id("DOC", self.identity_sha256), title, self.source, self.parser, "office-package-v1",
                                  self.blocks, self.warnings, quality, self.mapping, self.metadata)


def _relationships(archive, part, document):
    name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels") if part else "_rels/.rels"
    root = _xml(archive, name, required=False)
    if root is None:
        return {}
    if root.tag != _tag(PKG, "Relationships"):
        _fail("Office 关系部件结构无效：" + name)
    result = {}
    for relation in root:
        if relation.tag != _tag(PKG, "Relationship"):
            continue
        identifier, target, relation_type = relation.get("Id"), relation.get("Target"), relation.get("Type", "")
        if not identifier or identifier in result or not target:
            _fail("Office 关系标识重复或目标缺失：" + name)
        if len(result) >= 20000:
            _fail("Office 关系数量超出安全限制")
        external = relation.get("TargetMode", "Internal") == "External"
        if external:
            document.warn("external-links-not-fetched", "文档包含外部链接；已保留静态文本，未访问链接或更新外部数据。")
            resolved = None
        else:
            if relation.get("TargetMode", "Internal") != "Internal":
                _fail("Office 关系目标模式无效")
            decoded = unquote(target)
            url = urlsplit(decoded)
            if url.scheme or url.netloc or url.query or url.fragment or "\\" in decoded or any(ord(c) < 32 for c in decoded):
                _fail("Office 内部关系包含不安全路径")
            resolved = posixpath.normpath(decoded.lstrip("/") if decoded.startswith("/") else posixpath.join(posixpath.dirname(part), decoded))
            if resolved in {"", ".", ".."} or resolved.startswith("../"):
                _fail("Office 内部关系路径超出文档包")
        result[identifier] = {"type": relation_type.rsplit("/", 1)[-1], "target": resolved, "external": external}
    return result


def _target(relations, identifier, expected):
    relation = relations.get(identifier)
    if not relation or relation["type"] != expected or relation["external"]:
        _fail("Office 必要部件关系缺失、类型错误或指向外部：" + expected)
    return relation["target"]


def _main_part(archive, document):
    relations = _relationships(archive, "", document)
    identifiers = [identifier for identifier, relation in relations.items() if relation["type"] == "officeDocument"]
    if len(identifiers) != 1:
        _fail("Office 包必须包含唯一的主文档关系")
    return _target(relations, identifiers[0], "officeDocument")


def _xlsx_string(node):
    if node is None:
        return ""
    # Phonetic guide text (rPh) is an annotation, not a second copy of a name.
    text = "".join(child.text or "" for child in node.findall("s:t", NS))
    if not text:
        text = "".join(child.text or "" for child in node.findall("s:r/s:t", NS))
    text = re.sub(r"_x([0-9a-fA-F]{4})_", lambda match: chr(int(match.group(1), 16)), text)
    try:
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    except UnicodeError:
        _fail("Excel 文本包含不完整的 Unicode 字符")


def _coordinate(reference, limits):
    match = re.fullmatch(r"([A-Za-z]{1,7})([1-9][0-9]{0,11})", reference or "")
    if not match:
        _fail("Excel 单元格坐标无效")
    column = 0
    for char in match[1].upper():
        column = column * 26 + ord(char) - ord("A") + 1
    row = int(match[2])
    if row > limits.rows or column > limits.columns:
        _fail("Excel 稀疏单元格行列位置超出安全限制")
    return row - 1, column - 1


def _spreadsheet(archive, document):
    part = _main_part(archive, document)
    workbook = _xml(archive, part)
    if workbook.tag != _tag(S, "workbook"):
        _fail("文件不是有效的 Excel 工作簿")
    relations = _relationships(archive, part, document)
    if any(relation["type"] == "externalLink" for relation in relations.values()):
        document.warn("external-links-not-fetched", "工作簿包含外部数据引用；未加载或更新外部工作簿。")
    shared = []
    for relation in relations.values():
        if relation["type"] == "sharedStrings" and not relation["external"]:
            root = _xml(archive, relation["target"])
            if root.tag != _tag(S, "sst"):
                _fail("Excel 共享字符串表结构无效")
            items = root.findall("s:si", NS)
            if len(items) > document.limits.cells:
                _fail("Excel 共享字符串数量超出安全限制")
            shared = [_xlsx_string(item) for item in items]
            if sum(map(len, shared)) > document.limits.text_chars:
                _fail("Excel 共享字符串总长度超出安全限制")
    sheets = workbook.findall("s:sheets/s:sheet", NS)
    if not sheets or len(sheets) > document.limits.sheets:
        _fail("Excel 工作表数量为空或超出安全限制")
    names, targets = set(), set()
    for index, sheet in enumerate(sheets):
        name = sheet.get("name", "")
        if not name or name.casefold() in names:
            _fail("Excel 工作表名称缺失或重复")
        names.add(name.casefold())
        target = _target(relations, sheet.get(_tag(R, "id")), "worksheet")
        if target in targets:
            _fail("Excel 多个工作表重复指向同一内容")
        targets.add(target)
        root = _xml(archive, target)
        if root.tag != _tag(S, "worksheet"):
            _fail("Excel 工作表部件类型错误")
        _relationships(archive, target, document)
        hidden = sheet.get("state", "visible") != "visible"
        attrs = {"source": "xlsx", "sheet_name": name, "sheet_index": index + 1, "part": target, "hidden": hidden}
        document.add("heading", name, level=1, attrs=attrs)
        if hidden:
            document.warn("hidden-sheet-included", "隐藏工作表的静态单元格也已提取；请根据工作表标记确认审查范围。", "low")
        cells, seen_rows, warning_locations = {}, set(), []
        rows, columns, last_row = 0, 0, 0
        for row_node in root.findall("s:sheetData/s:row", NS):
            row_number = _integer(row_node.get("r", str(last_row + 1)), "Excel 行号", minimum=1, maximum=document.limits.rows)
            if row_number in seen_rows:
                _fail("Excel 工作表包含重复行")
            seen_rows.add(row_number)
            last_row, last_column = row_number, 0
            for cell in row_node.findall("s:c", NS):
                if cell.get("r"):
                    row, column = _coordinate(cell.get("r"), document.limits)
                    if row != row_number - 1:
                        _fail("Excel 单元格坐标与所在行不一致")
                else:
                    row, column = row_number - 1, last_column
                last_column = column + 1
                rows, columns = max(rows, row + 1), max(columns, column + 1)
                document.check_table(rows, columns)
                if (row, column) in cells:
                    _fail("Excel 工作表包含重复单元格")
                value_node, formula = cell.find("s:v", NS), cell.find("s:f", NS)
                raw_value = value_node.text if value_node is not None and value_node.text is not None else None
                value, cell_type = raw_value or "", cell.get("t", "n")
                cell_attrs = {"cell_reference": cell.get("r"), "value_type": cell_type, "raw_value": raw_value, "style_index": cell.get("s")}
                if cell_type == "s" and raw_value is not None:
                    shared_index = _integer(raw_value, "Excel 共享字符串索引", maximum=len(shared) - 1)
                    value = shared[shared_index]
                elif cell_type == "inlineStr":
                    value = _xlsx_string(cell.find("s:is", NS))
                elif cell_type == "b" and raw_value is not None:
                    if raw_value not in {"0", "1"}:
                        _fail("Excel 布尔缓存值无效")
                    value = "TRUE" if raw_value == "1" else "FALSE"
                elif cell_type not in {"n", "str", "e", "d", "s", "b"}:
                    _fail("Excel 单元格类型不受支持：" + cell_type)
                if formula is not None:
                    cell_attrs.update({"formula": formula.text or "", "formula_attributes": dict(formula.attrib), "formula_cache_present": raw_value is not None, "calculated_by_parser": False})
                    if raw_value is None:
                        value = ""
                        document.warn("formula-cache-missing", f"工作表 {name} 存在没有缓存结果的公式（如 {cell.get('r') or row_number}）；对应结果留空，未运行公式。", "high")
                        warning_locations.append(("formula-cache-missing", row, column))
                    else:
                        document.warn("formula-cache-only", "公式结果来自文件内上次保存的缓存，可能过期；未重算公式或访问外部工作簿。")
                if cell_type == "e":
                    document.warn("spreadsheet-error-value", "工作簿包含错误结果，已按原错误值保留，请核对相关单元格。", "high")
                if cell.get("s") not in {None, "0"} and cell_type == "n":
                    document.warn("spreadsheet-number-format", "数字按文件中的原始值提取，未模拟 Excel 日期、百分比、货币等显示格式；样式索引已保留，需对照原工作簿确认。")
                cells[(row, column)] = (value, cell_attrs)
        for merged in root.findall("s:mergeCells/s:mergeCell", NS):
            match = (merged.get("ref") or "").split(":")
            if len(match) != 2:
                _fail("Excel 合并区域无效")
            top, left = _coordinate(match[0], document.limits)
            bottom, right = _coordinate(match[1], document.limits)
            if bottom < top or right < left:
                _fail("Excel 合并区域顺序无效")
            rows, columns = max(rows, bottom + 1), max(columns, right + 1)
            document.check_table(rows, columns)
            value, cell_attrs = cells.get((top, left), ("", {}))
            cells[(top, left)] = (value, {**cell_attrs, "grid_span": right - left + 1, "row_span": bottom - top + 1})
        table = document.table(cells, rows, columns, attrs=attrs)
        for code, row, column in warning_locations:
            warning = next(item for item in document.warnings if item.code == code)
            if warning.location is None:
                warning.location = document.blocks[table.location.paragraph + 1 + row * columns + column].location
        if root.find("s:drawing", NS) is not None or root.find("s:legacyDrawing", NS) is not None:
            document.warn("spreadsheet-drawings-not-extracted", "工作表中的图片、图表或批注图形未转换为正文；请对照原工作簿核查。")
    document.metadata.update({"sheet_count": len(sheets), "sheet_order": [sheet.get("name") for sheet in sheets], "cell_count": document.cell_count})
    return document.finish()


def _drawing_text(paragraph):
    parts = []
    for child in paragraph:
        if child.tag == _tag(A, "br"):
            parts.append("\n")
        elif child.tag in {_tag(A, "r"), _tag(A, "fld")}:
            parts.extend(node.text or "" for node in child.findall("a:t", NS))
    return "".join(parts)


def _slide_table(node, document, page, attrs):
    row_nodes = node.findall("a:tr", NS)
    columns = max([len(node.findall("a:tblGrid/a:gridCol", NS))] + [len(row.findall("a:tc", NS)) for row in row_nodes])
    document.check_table(len(row_nodes), columns)
    cells = {}
    for row, row_node in enumerate(row_nodes):
        for column, cell in enumerate(row_node.findall("a:tc", NS)):
            text = "\n".join(_drawing_text(p) for p in cell.findall("a:txBody/a:p", NS))
            cell_attrs = {"grid_span": _integer(cell.get("gridSpan", "1"), "PPT 合并列数", minimum=1, maximum=document.limits.columns),
                          "row_span": _integer(cell.get("rowSpan", "1"), "PPT 合并行数", minimum=1, maximum=document.limits.rows),
                          "horizontal_merge": cell.get("hMerge") in {"1", "true"}, "vertical_merge": cell.get("vMerge") in {"1", "true"}}
            cells[(row, column)] = (text, cell_attrs)
    document.table(cells, len(row_nodes), columns, page=page, attrs=attrs)


def _slide_shapes(tree, document, page, attrs, *, notes=False):
    for shape in tree:
        if shape.tag == _tag(P, "grpSp"):
            _slide_shapes(shape, document, page, attrs, notes=notes)
        elif shape.tag == _tag(P, "sp"):
            placeholder = shape.find("p:nvSpPr/p:nvPr/p:ph", NS)
            role = placeholder.get("type", "body") if placeholder is not None else "body"
            if notes and role in {"sldImg", "sldNum", "dt", "ftr", "hdr"}:
                continue
            shape_attrs = {**attrs, "placeholder": role, "role": "speaker-notes" if notes else "slide-content"}
            for paragraph in shape.findall("p:txBody/a:p", NS):
                text = _drawing_text(paragraph)
                if not text.strip():
                    continue
                props = paragraph.find("a:pPr", NS)
                list_item = props is not None and any(props.find("a:" + tag, NS) is not None for tag in ("buChar", "buAutoNum", "buBlip"))
                kind = "heading" if role in {"title", "ctrTitle"} and not notes else ("list_item" if list_item else "paragraph")
                if props is not None and props.get("lvl") is not None:
                    shape_attrs = {**shape_attrs, "list_level": _integer(props.get("lvl"), "PPT 列表层级", maximum=8)}
                document.add(kind, "[演讲者备注] " + text if notes else text, level=1 if kind == "heading" else None, page=page, attrs=shape_attrs)
        elif shape.tag == _tag(P, "graphicFrame"):
            table = shape.find("a:graphic/a:graphicData/a:tbl", NS)
            if table is not None:
                _slide_table(table, document, page, attrs)
            else:
                document.warn("presentation-graphic-not-extracted", "幻灯片的图表、SmartArt 或嵌入对象未转换为正文，请对照原演示文稿核查。")
        elif shape.tag == _tag(P, "pic") and not notes:
            properties = shape.find("p:nvPicPr/p:cNvPr", NS)
            description = (properties.get("descr") or properties.get("title") or "") if properties is not None else ""
            document.add("image_placeholder", "[图片]" + (" " + description if description else ""), page=page, attrs={**attrs, "description": description, "ocr_performed": False})
            document.warn("presentation-images-not-ocr", "幻灯片图片仅保留占位与可用描述，未执行 OCR。")


def _presentation(archive, document):
    part = _main_part(archive, document)
    presentation = _xml(archive, part)
    if presentation.tag != _tag(P, "presentation"):
        _fail("文件不是有效的 PowerPoint 演示文稿")
    relations = _relationships(archive, part, document)
    slides = presentation.findall("p:sldIdLst/p:sldId", NS)
    if not slides or len(slides) > document.limits.slides:
        _fail("PowerPoint 幻灯片数量为空或超出安全限制")
    seen, order = set(), []
    for index, slide_id in enumerate(slides):
        target = _target(relations, slide_id.get(_tag(R, "id")), "slide")
        if target in seen:
            _fail("PowerPoint 演示顺序包含重复幻灯片关系")
        seen.add(target)
        order.append(target)
        slide = _xml(archive, target)
        if slide.tag != _tag(P, "sld"):
            _fail("PowerPoint 幻灯片部件类型错误")
        tree = slide.find("p:cSld/p:spTree", NS)
        if tree is None:
            _fail("PowerPoint 幻灯片缺少形状树")
        attrs = {"source": "pptx", "slide_index": index + 1, "part": target, "hidden": slide.get("show") in {"0", "false"}}
        if attrs["hidden"]:
            document.warn("hidden-slide-included", "隐藏幻灯片也按演示顺序提取，已标记隐藏状态。", "low")
        _slide_shapes(tree, document, index + 1, attrs)
        slide_relations = _relationships(archive, target, document)
        for relation in slide_relations.values():
            if relation["type"] != "notesSlide" or relation["external"]:
                continue
            notes = _xml(archive, relation["target"])
            if notes.tag != _tag(P, "notes"):
                _fail("PowerPoint 备注部件类型错误")
            notes_tree = notes.find("p:cSld/p:spTree", NS)
            if notes_tree is not None:
                _slide_shapes(notes_tree, document, index + 1, {**attrs, "part": relation["target"]}, notes=True)
                document.warn("speaker-notes-included", "演讲者备注作为明确标注的独立文本提取，不属于幻灯片可见正文。", "low")
    document.warn("slide-shape-reading-order", "幻灯片按实际演示顺序提取；同页文字按形状树顺序读取，复杂排版的视觉阅读顺序需要人工确认。")
    document.metadata.update({"slide_count": len(slides), "slide_order": order, "notes_labeled": True})
    return document.finish(pages=len(slides), reading_order=True)


def _odt_text(node, document):
    parts = []
    expanded = 0
    def append(text):
        nonlocal expanded
        expanded += len(text)
        if expanded > document.limits.text_chars:
            _fail("ODT 单段文本展开量超出安全限制")
        parts.append(text)
    def walk(element):
        if element.text:
            append(element.text)
        for child in element:
            if child.tag == _tag(TEXT, "s"):
                count = _integer(child.get(_tag(TEXT, "c"), "1"), "ODT 重复空格", minimum=1, maximum=document.limits.text_chars)
                if expanded + count > document.limits.text_chars:
                    _fail("ODT 重复空格展开量超出安全限制")
                append(" " * count)
            elif child.tag == _tag(TEXT, "tab"):
                append("\t")
            elif child.tag == _tag(TEXT, "line-break"):
                append("\n")
            elif child.tag in {_tag(TEXT, "note"), _tag(OFFICE, "annotation"), _tag(DRAW, "frame")}:
                document.warn("odt-inline-objects-not-extracted", "ODT 中的脚注、批注或内联图形没有混入正文；需对照原件检查这些内容。")
            else:
                if child.get(_tag(XLINK, "href")):
                    document.warn("external-links-not-fetched", "文档包含外部链接；仅提取静态文本，未访问链接。")
                walk(child)
            if child.tail:
                append(child.tail)
    walk(node)
    if sum(map(len, parts)) > document.limits.text_chars:
        _fail("ODT 单段文本展开量超出安全限制")
    return "".join(parts)


def _odt_rows(table):
    for child in table:
        if child.tag == _tag(TABLE, "table-row"):
            yield child
        elif child.tag in {_tag(TABLE, "table-header-rows"), _tag(TABLE, "table-rows"), _tag(TABLE, "table-row-group")}:
            yield from _odt_rows(child)


def _odt_columns(table, document):
    count = 0
    for child in table:
        if child.tag == _tag(TABLE, "table-column"):
            count += _integer(child.get(_tag(TABLE, "number-columns-repeated"), "1"), "ODT 声明列数", minimum=1, maximum=document.limits.columns)
        elif child.tag in {_tag(TABLE, "table-column-group"), _tag(TABLE, "table-columns"), _tag(TABLE, "table-header-columns")}:
            count += _odt_columns(child, document)
        if count > document.limits.columns:
            _fail("ODT 声明列数超出安全限制")
    return count


def _odt_cell_paragraphs(cell):
    for child in cell:
        if child.tag in {_tag(TEXT, "p"), _tag(TEXT, "h")}:
            yield child
        elif child.tag in {_tag(TEXT, "list"), _tag(TEXT, "list-item"), _tag(TEXT, "list-header"), _tag(TEXT, "section")}:
            yield from _odt_cell_paragraphs(child)


def _odt_table(table, document):
    cells, rows, columns = {}, 0, _odt_columns(table, document)
    for row in _odt_rows(table):
        repeat_rows = _integer(row.get(_tag(TABLE, "number-rows-repeated"), "1"), "ODT 重复行数", minimum=1, maximum=document.limits.rows)
        next_row = rows + repeat_rows
        document.check_table(next_row, max(columns, 1))
        row_cells, column = {}, 0
        for cell in row:
            if cell.tag not in {_tag(TABLE, "table-cell"), _tag(TABLE, "covered-table-cell")}:
                continue
            repeat_columns = _integer(cell.get(_tag(TABLE, "number-columns-repeated"), "1"), "ODT 重复列数", minimum=1, maximum=document.limits.columns)
            document.check_table(next_row, max(columns, column + repeat_columns))
            covered = cell.tag == _tag(TABLE, "covered-table-cell")
            values = [_odt_text(paragraph, document) for paragraph in _odt_cell_paragraphs(cell)]
            text = "" if covered else "\n".join(values)
            value_type = cell.get(_tag(OFFICE, "value-type"))
            if not text and not covered:
                text = next((cell.get(_tag(OFFICE, key)) for key in ("string-value", "date-value", "time-value", "boolean-value", "value") if cell.get(_tag(OFFICE, key)) is not None), "")
            attrs = {"covered": covered, "value_type": value_type,
                     "grid_span": _integer(cell.get(_tag(TABLE, "number-columns-spanned"), "1"), "ODT 合并列数", minimum=1, maximum=document.limits.columns),
                     "row_span": _integer(cell.get(_tag(TABLE, "number-rows-spanned"), "1"), "ODT 合并行数", minimum=1, maximum=document.limits.rows)}
            formula = cell.get(_tag(TABLE, "formula"))
            if formula is not None:
                attrs.update({"formula": formula, "formula_cache_present": bool(text), "calculated_by_parser": False})
                document.warn("formula-cache-only" if text else "formula-cache-missing", "ODT 表格公式仅提取已保存结果，未计算公式；缺少结果时留空。", "medium" if text else "high")
            if cell.find("table:table", NS) is not None:
                document.warn("odt-nested-table-not-extracted", "ODT 包含嵌套表格；父单元格未扁平混入嵌套内容，需对照原文核查。", "high")
            for repeated in range(repeat_columns):
                row_cells[column + repeated] = (text, attrs)
            column += repeat_columns
        columns = max(columns, column)
        document.check_table(next_row, columns)
        for repeated in range(repeat_rows):
            cells.update({(rows + repeated, column): value for column, value in row_cells.items()})
        rows = next_row
    document.table(cells, rows, columns, attrs={"source": "odt", "table_name": table.get(_tag(TABLE, "name"), "")})


def _odt_blocks(parent, document, *, list_level=None):
    for node in parent:
        if node.tag in {_tag(TEXT, "p"), _tag(TEXT, "h")}:
            text = _odt_text(node, document)
            if not text.strip():
                continue
            heading = node.tag == _tag(TEXT, "h")
            level = _integer(node.get(_tag(TEXT, "outline-level"), "1"), "ODT 标题层级", minimum=1, maximum=10) if heading else None
            kind = "heading" if heading else ("list_item" if list_level is not None else "paragraph")
            document.add(kind, text, level=level, attrs={"source": "odt", "list_level": list_level, "style_name": node.get(_tag(TEXT, "style-name"))})
        elif node.tag == _tag(TABLE, "table"):
            _odt_table(node, document)
        elif node.tag == _tag(TEXT, "list"):
            next_level = 0 if list_level is None else list_level + 1
            if next_level > 100:
                _fail("ODT 列表嵌套超过安全限制")
            _odt_blocks(node, document, list_level=next_level)
        elif node.tag in {_tag(TEXT, "list-item"), _tag(TEXT, "list-header"), _tag(TEXT, "section")}:
            _odt_blocks(node, document, list_level=list_level)
        elif node.tag == _tag(DRAW, "frame"):
            document.add("image_placeholder", "[图形或嵌入对象]", attrs={"source": "odt", "ocr_performed": False})
            document.warn("odt-graphics-not-extracted", "ODT 图形和嵌入对象未转为正文，也未执行其中内容。")
        elif node.tag not in {_tag(TEXT, "sequence-decls"), _tag(TEXT, "variable-decls"), _tag(TEXT, "user-field-decls")}:
            document.warn("odt-unsupported-body-structure", "ODT 存在暂未转换的正文结构，需对照原件核查。")


def _opendocument(archive, document):
    try:
        mimetype = archive.read("mimetype").decode("ascii").strip()
    except (KeyError, UnicodeError):
        _fail("ODT 缺少有效的 mimetype 声明")
    if mimetype != "application/vnd.oasis.opendocument.text":
        _fail("文件的 OpenDocument 类型不是 ODT 文本文档")
    manifest = _xml(archive, "META-INF/manifest.xml", required=False)
    if manifest is not None and manifest.find(".//" + _tag(MANIFEST, "encryption-data")) is not None:
        _fail("ODT 内容已加密，无法读取")
    root = _xml(archive, "content.xml")
    if root.tag != _tag(OFFICE, "document-content"):
        _fail("ODT content.xml 结构无效")
    body = root.find("o:body/o:text", NS)
    if body is None:
        _fail("ODT 缺少文本正文")
    if root.find("o:scripts", NS) is not None:
        document.warn("macros-not-executed", "ODT 脚本未执行；仅提取静态正文。")
    _odt_blocks(body, document)
    return document.finish()


def parse_office(data: bytes, source: RawFileBinding, limits, *, identity_sha256: str | None = None) -> StructuredDocument:
    """Parse ODT/XLSX/XLSM/PPTX/PPTM using only bounded local package XML."""
    from document_review_ingest import _block_identity_sha, _zip_safety
    suffix = source.extension.casefold()
    parsers = {".odt": ("odt-xml", _opendocument), ".xlsx": ("xlsx-xml", _spreadsheet),
               ".xlsm": ("xlsx-xml", _spreadsheet), ".pptx": ("pptx-xml", _presentation), ".pptm": ("pptx-xml", _presentation)}
    if suffix not in parsers:
        _fail("此 Office 解析器不支持该格式：" + suffix)
    if len(data) > limits.max_file_bytes:
        _fail("Office 原件大小超出安全限制")
    if len(data) != source.byte_size or hashlib.sha256(data).hexdigest() != source.sha256:
        _fail("Office 原件与不可变来源绑定不一致")
    identity = _block_identity_sha(source, identity_sha256)
    archive = _zip_safety(data, limits)
    parser_name, parser = parsers[suffix]
    document = _Document(source, _Limits.from_ingestion(limits), parser_name, identity_sha256=identity)
    if suffix in {".xlsm", ".pptm"} or any(name.lower().endswith("vbaproject.bin") for name in archive.namelist()):
        document.warn("macros-not-executed", "包含宏的 Office 文件仅静态读取 XML；未执行宏或嵌入代码。")
    return parser(archive, document)
