"""Secure, provider-neutral ingestion adapters for Document Review Studio.

The parsers return a :class:`StructuredDocument` plus warnings.  They never
replace the uploaded bytes and they refuse to turn an unavailable or unsafe
parser into an apparently successful extraction.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol
from xml.etree import ElementTree as ET

from document_review_model import (
    DocumentBlock,
    DocumentLocation,
    ExtractionWarning,
    QualitySignals,
    RawFileBinding,
    StructuredDocument,
    SUPPORTED_EXTENSIONS,
    UNSUPPORTED_EXTENSIONS,
    stable_id,
)


MEDIA_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
}
PARSER_VERSION = "document-review-studio-v1"
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


class IngestionError(ValueError):
    """Raised when extraction cannot be trusted or the upload is unsafe."""


class ParserUnavailable(IngestionError):
    pass


@dataclass(frozen=True)
class IngestionLimits:
    max_file_bytes: int = 32 * 1024 * 1024
    max_pdf_pages: int = 250
    max_pdf_stream_uncompressed_bytes: int = 16 * 1024 * 1024
    max_pdf_total_uncompressed_bytes: int = 64 * 1024 * 1024
    max_docx_entries: int = 5000
    max_docx_uncompressed_bytes: int = 128 * 1024 * 1024
    max_docx_compression_ratio: int = 1000
    max_images: int = 500
    max_ocr_seconds_per_page: int = 45


class OCRAdapter(Protocol):
    name: str
    version: str

    def available(self) -> tuple[bool, str]: ...

    def recognize_pdf_page(self, page_bytes: bytes, *, page_number: int, language: str) -> dict[str, Any]: ...


def safe_upload_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise IngestionError("文件名无效")
    candidate = Path(name).name
    suffix = Path(candidate).suffix.casefold()
    if candidate != name or "\\" in name or not candidate or any(ord(c) < 32 or ord(c) == 127 for c in candidate):
        raise IngestionError("文件名包含路径穿越或控制字符")
    if suffix in UNSUPPORTED_EXTENSIONS:
        raise IngestionError(f"不支持 {suffix}：请先转换为 .docx、.md、.txt 或文本型 PDF")
    if suffix not in SUPPORTED_EXTENSIONS:
        raise IngestionError("只支持 .md、.txt、.docx 和 .pdf")
    return candidate


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_text(data: bytes) -> str:
    if b"\x00" in data:
        raise IngestionError("文本包含二进制控制字节，拒绝作为文本读取")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IngestionError(f"文件不是 UTF-8 文本：{exc}") from exc


def _binding(name: str, data: bytes) -> RawFileBinding:
    suffix = Path(name).suffix.casefold()
    return RawFileBinding(name, suffix, MEDIA_TYPES[suffix], len(data), _sha256(data), f"source/{name}")


def _block_id(source_hash: str, kind: str, ordinal: int, text: str, *location: object) -> str:
    return stable_id("B", source_hash, kind, ordinal, text, *location)


def _text_blocks(text: str, source: RawFileBinding, *, parser: str, warnings: list[ExtractionWarning] | None = None) -> StructuredDocument:
    warnings = list(warnings or [])
    blocks: list[DocumentBlock] = []
    mapping: list[dict[str, Any]] = []
    paragraph = 0
    offset = 0
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        start = offset
        offset += len(line) + 1
        if not line.strip():
            i += 1
            continue
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        list_item = re.match(r"^\s*(?:(\d+)[.)]|[-*+])\s+(.+)$", line)
        table = "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1])
        kind = "paragraph"
        level = None
        value = line.strip()
        attrs: dict[str, Any] = {"source_line": i + 1}
        if heading:
            kind, level, value = "heading", len(heading.group(1)), heading.group(2).strip()
        elif list_item:
            kind = "list_item"
            value = list_item.group(2).strip()
            attrs["ordered"] = bool(list_item.group(1))
        elif line.lstrip().startswith(">"):
            kind, value = "blockquote", line.lstrip()[1:].strip()
        if table:
            rows: list[list[str]] = []
            j = i
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                cells = [cell.strip() for cell in lines[j].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                    rows.append(cells)
                offset += len(lines[j]) + 1 if j != i else 0
                j += 1
            table_id = _block_id(source.sha256, "table", len(blocks), "|".join("|".join(row) for row in rows), i + 1)
            table_block = DocumentBlock(
                table_id,
                "table",
                text=" ".join(" ".join(row) for row in rows),
                location=DocumentLocation(table_id, "table", paragraph=paragraph, source_path=source.original_name),
                attrs={"rows": rows, "source_line": i + 1},
            )
            blocks.append(table_block)
            for row_index, row in enumerate(rows):
                for col_index, cell_text in enumerate(row):
                    cell_id = _block_id(source.sha256, "table_cell", len(blocks), cell_text, table_id, row_index, col_index)
                    cell = DocumentBlock(
                        cell_id,
                        "table_cell",
                        text=cell_text,
                        location=DocumentLocation(cell_id, "table_cell", paragraph=paragraph, table_id=table_id, row=row_index, column=col_index, source_path=source.original_name),
                        attrs={"table_id": table_id, "row": row_index, "column": col_index},
                    )
                    blocks.append(cell)
                    table_block.children.append(cell_id)
                    mapping.append({"source_line": i + row_index + 1, "block_id": cell_id, "kind": "table_cell"})
            mapping.append({"source_line": i + 1, "block_id": table_id, "kind": "table"})
            paragraph += 1
            i = j
            continue
        block_id = _block_id(source.sha256, kind, len(blocks), value, i + 1)
        block = DocumentBlock(
            block_id,
            kind,
            text=value,
            level=level,
            location=DocumentLocation(block_id, kind, paragraph=paragraph, char_start=start, char_end=start + len(line), source_path=source.original_name),
            attrs=attrs,
        )
        blocks.append(block)
        mapping.append({"source_line": i + 1, "block_id": block_id, "char_start": start, "char_end": start + len(line)})
        paragraph += 1
        i += 1
    quality = QualitySignals(page_count=1, text_coverage=min(1.0, 1.0 if text.strip() else 0.0), requires_confirmation=True)
    return StructuredDocument(
        document_id=stable_id("DOC", source.sha256),
        title=next((b.text for b in blocks if b.kind == "heading"), Path(source.original_name).stem),
        source=source,
        parser_name=parser,
        parser_version=PARSER_VERSION,
        blocks=blocks,
        warnings=warnings,
        quality=quality,
        source_to_block=mapping,
        metadata={"line_count": len(lines), "encoding": "utf-8"},
    )


def _xml_text(element: ET.Element, *, include_deleted: bool = False) -> str:
    parts: list[str] = []
    for child in element.iter():
        if child.tag in {W_NS + "t", W_NS + "delText"}:
            parent = child
            if not include_deleted and child.tag == W_NS + "delText":
                continue
            parts.append(child.text or "")
        elif child.tag == W_NS + "tab":
            parts.append("\t")
        elif child.tag in {W_NS + "br", W_NS + "cr"}:
            parts.append("\n")
    return "".join(parts).strip()


@dataclass(frozen=True)
class _VerifiedZipArchive:
    entries: dict[str, bytes]

    def read(self, name: str) -> bytes:
        try:
            return self.entries[name]
        except KeyError:
            raise KeyError(name) from None

    def namelist(self) -> list[str]:
        return list(self.entries)


def _zip_safety(data: bytes, limits: IngestionLimits) -> _VerifiedZipArchive:
    archive = None
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        infos = archive.infolist()
        if len(infos) > limits.max_docx_entries:
            raise IngestionError("DOCX ZIP 条目过多，疑似 ZIP bomb")
        total = 0
        extracted: dict[str, bytes] = {}
        for info in infos:
            name = info.filename.replace("\\", "/")
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts or any(part == "" for part in parts):
                raise IngestionError("DOCX 包含不安全路径")
            if name in extracted:
                raise IngestionError("DOCX 包含重复路径，无法安全解析")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise IngestionError("DOCX 包含符号链接")
            if info.flag_bits & 0x1:
                raise IngestionError("DOCX 受到密码保护，无法安全读取")

            chunks: list[bytes] = []
            entry_total = 0
            with archive.open(info, "r") as source:
                while True:
                    chunk = source.read(min(64 * 1024, limits.max_docx_uncompressed_bytes - total + 1))
                    if not chunk:
                        break
                    entry_total += len(chunk)
                    total += len(chunk)
                    if total > limits.max_docx_uncompressed_bytes:
                        raise IngestionError("DOCX 解压后体积过大，疑似 ZIP bomb")
                    if entry_total > max(1, info.compress_size) * limits.max_docx_compression_ratio:
                        raise IngestionError("DOCX 单个条目压缩率异常，疑似 ZIP bomb")
                    chunks.append(chunk)
            extracted[name] = b"".join(chunks)
    except (OSError, RuntimeError, zipfile.BadZipFile, zlib.error, EOFError) as exc:
        raise IngestionError(f"DOCX 解压失败，无法安全读取：{exc}") from exc
    finally:
        if archive is not None:
            archive.close()
    return _VerifiedZipArchive(extracted)


class _SafeDocxTreeBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        # Parser-level rejection also covers UTF-16/32 declarations that a
        # byte-level ASCII scan cannot recognize.
        raise IngestionError("DOCX XML 包含外部实体声明或 DTD")


def _docx_xml(archive: _VerifiedZipArchive, name: str) -> ET.Element | None:
    try:
        raw = archive.read(name)
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise IngestionError(f"DOCX XML 包含外部实体声明：{name}")
        return ET.fromstring(raw, parser=ET.XMLParser(target=_SafeDocxTreeBuilder()))
    except KeyError:
        return None
    except (ET.ParseError, LookupError) as exc:
        raise IngestionError(f"DOCX XML 损坏：{name}: {exc}") from exc


def _parse_docx(data: bytes, source: RawFileBinding, limits: IngestionLimits) -> StructuredDocument:
    archive = _zip_safety(data, limits)
    document_xml = _docx_xml(archive, "word/document.xml")
    if document_xml is None:
        raise IngestionError("DOCX 缺少 word/document.xml")
    styles_xml = _docx_xml(archive, "word/styles.xml")
    styles: dict[str, str] = {}
    if styles_xml is not None:
        for style in styles_xml.findall(".//" + W_NS + "style"):
            style_id = style.attrib.get(W_NS + "styleId")
            name = style.find(W_NS + "name")
            if style_id and name is not None:
                styles[style_id] = name.attrib.get(W_NS + "val", "")
    blocks: list[DocumentBlock] = []
    mapping: list[dict[str, Any]] = []
    warnings: list[ExtractionWarning] = []
    revisions = bool(document_xml.findall(".//" + W_NS + "ins") or document_xml.findall(".//" + W_NS + "del"))
    settings = _docx_xml(archive, "word/settings.xml")
    if settings is not None and settings.find(W_NS + "trackRevisions") is not None:
        revisions = True
    comments = "word/comments.xml" in archive.namelist()
    footnotes = "word/footnotes.xml" in archive.namelist()
    hyperlinks = bool(document_xml.findall(".//" + W_NS + "hyperlink"))
    images = [name for name in archive.namelist() if name.startswith("word/media/")]
    if len(images) > limits.max_images:
        raise IngestionError("DOCX 图片数量超过安全限制")
    if revisions:
        warnings.append(ExtractionWarning("unaccepted-revisions", "critical", "文档包含 w:ins/w:del 或启用修订跟踪；未确认前禁止进入审查"))
    if comments:
        warnings.append(ExtractionWarning("comments-present", "medium", "DOCX 含批注；正文审查不会假装已包含批注语义"))
    if footnotes:
        warnings.append(ExtractionWarning("footnotes-present", "medium", "DOCX 含脚注或尾注；已记录存在状态，引用语义需人工确认"))
    if hyperlinks:
        warnings.append(ExtractionWarning("hyperlinks-present", "low", "DOCX 含超链接；链接目标存在状态已记录"))

    def add_block(kind: str, text: str, paragraph: int, *, level: int | None = None, attrs: dict[str, Any] | None = None, page: int | None = None, table_id: str | None = None, row: int | None = None, column: int | None = None) -> DocumentBlock:
        block_id = _block_id(source.sha256, kind, len(blocks), text, paragraph, table_id, row, column)
        block = DocumentBlock(block_id, kind, text=text, level=level, location=DocumentLocation(block_id, kind, page=page, paragraph=paragraph, table_id=table_id, row=row, column=column, source_path=source.original_name), attrs=attrs or {})
        blocks.append(block)
        mapping.append({"paragraph": paragraph, "block_id": block_id, "kind": kind, "table_id": table_id, "row": row, "column": column})
        return block

    paragraph_index = 0
    body = document_xml.find(".//" + W_NS + "body")
    if body is None:
        raise IngestionError("DOCX 缺少正文 body")
    for child in list(body):
        if child.tag == W_NS + "p":
            text = _xml_text(child)
            ppr = child.find(W_NS + "pPr")
            style_id = ppr.find(W_NS + "pStyle").attrib.get(W_NS + "val") if ppr is not None and ppr.find(W_NS + "pStyle") is not None else ""
            style_name = styles.get(style_id, style_id)
            outline = ppr.find(W_NS + "outlineLvl") if ppr is not None else None
            heading_match = re.search(r"heading\s*([1-9])", (style_name or "").casefold())
            level = int(heading_match.group(1)) if heading_match else (int(outline.attrib.get(W_NS + "val", "0")) + 1 if outline is not None else None)
            num_pr = ppr.find(W_NS + "numPr") if ppr is not None else None
            attrs = {"style_id": style_id, "style_name": style_name, "has_hyperlink": bool(child.findall(".//" + W_NS + "hyperlink")), "has_drawing": bool(child.findall(".//" + W_NS + "drawing"))}
            if num_pr is not None:
                attrs["list"] = True
                ilvl = num_pr.find(W_NS + "ilvl")
                num_id = num_pr.find(W_NS + "numId")
                attrs["list_level"] = int(ilvl.attrib.get(W_NS + "val", "0")) if ilvl is not None else 0
                attrs["num_id"] = num_id.attrib.get(W_NS + "val") if num_id is not None else None
            if attrs["has_drawing"]:
                add_block("image_placeholder", "[图片]", paragraph_index, attrs={**attrs, "description": text})
            if text:
                add_block("heading" if level else ("list_item" if attrs.get("list") else "paragraph"), text, paragraph_index, level=level, attrs=attrs)
            paragraph_index += 1
        elif child.tag == W_NS + "tbl":
            table_id = _block_id(source.sha256, "table", len(blocks), "", paragraph_index)
            rows: list[list[str]] = []
            table_block = add_block("table", "", paragraph_index, attrs={"rows": rows, "source": "docx"})
            table_block.location = DocumentLocation(table_id, "table", paragraph=paragraph_index, table_id=table_id, source_path=source.original_name)
            table_block.block_id = table_id
            mapping[-1]["block_id"] = table_id
            mapping[-1]["table_id"] = table_id
            table_cells = child.findall("./" + W_NS + "tr")
            for row_index, row_xml in enumerate(table_cells):
                row_values: list[str] = []
                column = 0
                for cell_xml in row_xml.findall("./" + W_NS + "tc"):
                    cell_text = _xml_text(cell_xml)
                    tcpr = cell_xml.find(W_NS + "tcPr")
                    grid_span = tcpr.find(W_NS + "gridSpan") if tcpr is not None else None
                    v_merge = tcpr.find(W_NS + "vMerge") if tcpr is not None else None
                    attrs = {"grid_span": int(grid_span.attrib.get(W_NS + "val", "1")) if grid_span is not None else 1, "v_merge": v_merge.attrib.get(W_NS + "val", "continue") if v_merge is not None else None, "nested_tables": len(cell_xml.findall(".//" + W_NS + "tbl"))}
                    row_values.append(cell_text)
                    cell = add_block("table_cell", cell_text, paragraph_index, attrs=attrs, table_id=table_id, row=row_index, column=column)
                    table_block.children.append(cell.block_id)
                    column += attrs["grid_span"]
                rows.append(row_values)
            table_block.attrs["rows"] = rows
            paragraph_index += 1
    for part in sorted(name for name in archive.namelist() if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)):
        kind = "header" if "/header" in part else "footer"
        root = _docx_xml(archive, part)
        if root is not None:
            text = _xml_text(root)
            if text:
                add_block(kind, text, paragraph_index, attrs={"part": part})
                paragraph_index += 1
    quality = QualitySignals(page_count=1, text_coverage=1.0 if any(b.text.strip() for b in blocks) else 0.0, table_count=sum(b.kind == "table" for b in blocks), tables_parsed=sum(b.kind == "table" for b in blocks), footnote_comment_revision_risk=[w.code for w in warnings], requires_confirmation=True)
    return StructuredDocument(stable_id("DOC", source.sha256), next((b.text for b in blocks if b.kind == "heading"), Path(source.original_name).stem), source, "docx-xml", PARSER_VERSION, blocks, warnings, quality, mapping, {"revisions_present": revisions, "comments_present": comments, "footnotes_present": footnotes, "hyperlinks_present": hyperlinks, "image_count": len(images), "nested_tables_present": any(b.attrs.get("nested_tables", 0) for b in blocks)})


def _pdf_backend() -> tuple[str, Any] | None:
    try:
        import fitz  # type: ignore
        return "pymupdf", fitz
    except ImportError:
        pass
    try:
        import pypdf  # type: ignore
        return "pypdf", pypdf
    except ImportError:
        return None


def _fallback_pdf_text(data: bytes, source: RawFileBinding, limits: IngestionLimits) -> StructuredDocument:
    """Parse the small, uncompressed text-PDF subset without a dependency.

    This is deliberately conservative: it accepts only PDFs with explicit
    page objects and literal text operators.  Anything more complex is sent
    to the optional pypdf/PyMuPDF adapters instead of being mislabelled as a
    successful extraction.
    """
    if not data.startswith(b"%PDF-"):
        raise IngestionError("文件不是 PDF")
    page_count = len(re.findall(rb"/Type\s*/Page(?:\s|/|>)", data))
    if not page_count:
        raise IngestionError("PDF 缺少可识别的页面对象；未生成伪审查文本")
    if page_count > limits.max_pdf_pages:
        raise IngestionError("PDF 页数超过安全限制")
    streams = re.findall(rb"stream\r?\n(.*?)\r?\nendstream", data, flags=re.S)
    page_texts: list[str] = []
    expanded_total = 0
    for stream in streams:
        raw = stream
        try:
            import zlib
            decompressor = zlib.decompressobj()
            raw = decompressor.decompress(stream, limits.max_pdf_stream_uncompressed_bytes + 1)
            if len(raw) > limits.max_pdf_stream_uncompressed_bytes or decompressor.unconsumed_tail:
                raise IngestionError("PDF 内容流解压后超过安全上限")
            raw += decompressor.flush()
            if len(raw) > limits.max_pdf_stream_uncompressed_bytes:
                raise IngestionError("PDF 内容流解压后超过安全上限")
        except IngestionError:
            raise
        except zlib.error:
            pass
        expanded_total += len(raw)
        if expanded_total > limits.max_pdf_total_uncompressed_bytes:
            raise IngestionError("PDF 内容流累计解压体积超过安全上限")
        chunks: list[str] = []
        for match in re.finditer(rb"\((?:\\.|[^)])*\)\s*Tj|\[(.*?)\]\s*TJ", raw, flags=re.S):
            value = match.group(0)
            if value.startswith(b"["):
                value = match.group(1) or b""
                parts = re.findall(rb"\((?:\\.|[^)])*\)", value)
            else:
                parts = [value.split(b")", 1)[0][1:]]
            for part in parts:
                part = part.replace(rb"\\(", b"(").replace(rb"\\)", b")").replace(rb"\\n", b"\n")
                chunks.append(part.decode("utf-8", errors="replace"))
        page_texts.append("".join(chunks).strip())
    while len(page_texts) < page_count:
        page_texts.append("")
    blocks: list[DocumentBlock] = []
    mapping: list[dict[str, Any]] = []
    blank_pages: list[int] = []
    for page_number, text in enumerate(page_texts[:page_count], start=1):
        if not text:
            blank_pages.append(page_number)
            continue
        block_id = _block_id(source.sha256, "pdf_text_block", len(blocks), text, page_number)
        blocks.append(DocumentBlock(block_id, "paragraph", text=text, location=DocumentLocation(block_id, "pdf_text_block", page=page_number, paragraph=len(blocks), source_path=source.original_name), attrs={"page": page_number, "reading_order": 0, "coordinates_available": False}))
        mapping.append({"page": page_number, "block_id": block_id, "reading_order": 0})
    warnings = [ExtractionWarning("pdf-coordinates-unavailable", "medium", "使用保守内置 PDF 文本适配器；只保存页码，没有可靠坐标、表格或阅读顺序语义")]
    if all(not text for text in page_texts[:page_count]):
        warnings.append(ExtractionWarning("scan-pages-detected", "high", "PDF 没有可提取的文字，疑似扫描件；需要 OCR"))
    quality = QualitySignals(page_count=page_count, blank_pages=blank_pages, text_coverage=min(1.0, sum(len(text) for text in page_texts) / max(1, page_count * 1200)), requires_confirmation=True)
    return StructuredDocument(stable_id("DOC", source.sha256), Path(source.original_name).stem, source, "builtin-pdf-text", PARSER_VERSION, blocks, warnings, quality, mapping, {"pdf_kind": "scanned" if not blocks else "text", "coordinates_available": False})


def _pdf_text(data: bytes, source: RawFileBinding, limits: IngestionLimits) -> StructuredDocument:
    backend = _pdf_backend()
    if backend is None:
        return _fallback_pdf_text(data, source, limits)
    name, library = backend
    blocks: list[DocumentBlock] = []
    warnings: list[ExtractionWarning] = []
    mapping: list[dict[str, Any]] = []
    page_texts: list[str] = []
    blank_pages: list[int] = []
    suspected_order = False
    if name == "pymupdf":
        try:
            pdf = library.open(stream=data, filetype="pdf")
            if pdf.is_encrypted:
                raise IngestionError("PDF 已加密，未提供密码，拒绝伪造解析结果")
            if pdf.page_count > limits.max_pdf_pages:
                raise IngestionError("PDF 页数超过安全限制")
            for page_number, page in enumerate(pdf, start=1):
                page_dict = page.get_text("dict")
                blocks_data = page_dict.get("blocks", [])
                texts: list[str] = []
                for block_data in blocks_data:
                    if block_data.get("type") != 0:
                        continue
                    text = "".join(span.get("text", "") for line in block_data.get("lines", []) for span in line.get("spans", [])).strip()
                    if not text:
                        continue
                    texts.append(text)
                    bbox = tuple(float(value) for value in block_data.get("bbox", (0, 0, 0, 0)))
                    block_id = _block_id(source.sha256, "pdf_text_block", len(blocks), text, page_number, bbox)
                    block = DocumentBlock(block_id, "paragraph", text=text, location=DocumentLocation(block_id, "pdf_text_block", page=page_number, paragraph=len(blocks), bbox=bbox, source_path=source.original_name), attrs={"page": page_number, "reading_order": len(texts) - 1})
                    blocks.append(block)
                    mapping.append({"page": page_number, "block_id": block_id, "bbox": list(bbox), "reading_order": len(texts) - 1})
                page_text = "\n".join(texts)
                page_texts.append(page_text)
                if not page_text.strip():
                    blank_pages.append(page_number)
        except IngestionError:
            raise
        except Exception as exc:
            raise IngestionError(f"PDF 解析器失败，未生成审查输入：{exc}") from exc
    else:
        try:
            reader = library.PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted:
                raise IngestionError("PDF 已加密，未提供密码，拒绝伪造解析结果")
            if len(reader.pages) > limits.max_pdf_pages:
                raise IngestionError("PDF 页数超过安全限制")
            for page_number, page in enumerate(reader.pages, start=1):
                text = (page.extract_text() or "").strip()
                page_texts.append(text)
                if not text:
                    blank_pages.append(page_number)
                if text:
                    block_id = _block_id(source.sha256, "pdf_text_block", len(blocks), text, page_number)
                    blocks.append(DocumentBlock(block_id, "paragraph", text=text, location=DocumentLocation(block_id, "pdf_text_block", page=page_number, paragraph=len(blocks), source_path=source.original_name), attrs={"page": page_number, "reading_order": 0, "coordinates_available": False}))
                    mapping.append({"page": page_number, "block_id": block_id, "reading_order": 0})
        except IngestionError:
            raise
        except Exception as exc:
            raise IngestionError(f"PDF 解析器失败，未生成审查输入：{exc}") from exc
    page_count = len(page_texts)
    text_chars = sum(len(value) for value in page_texts)
    # A short page is not a scanned page. OCR routing is allowed only when the
    # selected text backend extracted no text at all for that page.
    scanned_page_numbers = [index + 1 for index, value in enumerate(page_texts) if not value.strip()]
    scanned_pages = len(scanned_page_numbers)
    if scanned_pages:
        warnings.append(ExtractionWarning("scan-pages-detected", "high", f"检测到 {scanned_pages} 个无可提取文本页；需要 OCR 或人工确认", details={"pages": scanned_page_numbers}))
    low_text_pages = [index + 1 for index, value in enumerate(page_texts) if value.strip() and len(value.strip()) < 20]
    if low_text_pages:
        warnings.append(ExtractionWarning("short-text-pages", "low", "部分页面文本较短；这不是扫描件判据，请在内容预览中确认", details={"pages": low_text_pages}))
    if name == "pypdf":
        warnings.append(ExtractionWarning("pdf-coordinates-unavailable", "medium", "当前使用 pypdf；已保存页码但没有可靠字符坐标"))
    quality = QualitySignals(page_count=page_count, blank_pages=blank_pages, text_coverage=min(1.0, text_chars / max(1, page_count * 1200)), suspected_reading_order=suspected_order, parser_available=True, ocr_available=None, requires_confirmation=True)
    if page_count and scanned_pages == page_count:
        quality.text_coverage = 0.0
    return StructuredDocument(stable_id("DOC", source.sha256), Path(source.original_name).stem, source, name, PARSER_VERSION, blocks, warnings, quality, mapping, {"pdf_kind": "scanned" if scanned_pages == page_count else ("mixed" if scanned_pages else "text"), "page_text_lengths": [len(value) for value in page_texts], "coordinates_available": name == "pymupdf"})


class TesseractOCR:
    """Small subprocess adapter; the domain layer never imports Tesseract."""

    name = "tesseract"
    version = "unknown"

    def __init__(self, executable: str | None = None, *, renderer: Any | None = None, timeout_seconds: int = 45):
        self.executable = executable or shutil.which("tesseract")
        self.renderer = renderer
        self.timeout_seconds = max(1, int(timeout_seconds))
        if self.executable:
            try:
                output = subprocess.run([self.executable, "--version"], capture_output=True, text=True, timeout=5, check=False).stdout.splitlines()
                self.version = output[0].strip() if output else "unknown"
            except (OSError, subprocess.SubprocessError):
                self.version = "unknown"

    def available(self) -> tuple[bool, str]:
        if not self.executable:
            return False, "未发现 tesseract；安装 Tesseract 5.x 及 chi_sim、chi_tra、eng 语言包"
        try:
            langs = subprocess.run([self.executable, "--list-langs"], capture_output=True, text=True, timeout=5, check=False).stdout.splitlines()
            installed = {line.strip() for line in langs if line.strip() and not line.casefold().startswith("list of available")}
            missing = sorted({"chi_sim", "chi_tra", "eng"} - installed)
            if missing:
                return False, "Tesseract 已安装但缺少语言包：" + ", ".join(missing)
        except (OSError, subprocess.SubprocessError):
            return False, "无法读取 Tesseract 语言包状态"
        return True, f"{self.name} {self.version}"

    def recognize_pdf_page(self, page_bytes: bytes, *, page_number: int, language: str) -> dict[str, Any]:
        available, detail = self.available()
        if not available:
            raise ParserUnavailable(detail)
        with tempfile.TemporaryDirectory(prefix="document-review-ocr-") as temp:
            input_path = Path(temp) / "page.png"
            output_base = Path(temp) / "ocr"
            input_path.write_bytes(page_bytes)
            command = [self.executable or "tesseract", str(input_path), str(output_base), "--psm", "3", "-l", language, "tsv"]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout_seconds, check=False)
            except subprocess.TimeoutExpired as exc:
                raise IngestionError(f"OCR 第 {page_number} 页超时") from exc
            if result.returncode != 0:
                raise IngestionError(f"OCR 第 {page_number} 页失败：{result.stderr.strip()[:300]}")
            tsv_path = Path(str(output_base) + ".tsv")
            if not tsv_path.is_file():
                raise IngestionError(f"OCR 第 {page_number} 页未生成结构化结果")
            lines = tsv_path.read_text(encoding="utf-8", errors="replace").splitlines()
            words: list[str] = []
            confidences: list[float] = []
            for line in lines[1:]:
                cells = line.split("\t")
                if len(cells) < 12 or not cells[11].strip():
                    continue
                words.append(cells[11].strip())
                try:
                    confidences.append(float(cells[10]))
                except ValueError:
                    pass
            text = " ".join(words)
            low = sum(value < 60 for value in confidences)
            return {"page": page_number, "text": text, "low_confidence_words": low, "word_count": len(words), "confidence": min(confidences) if confidences else 0.0, "engine": self.name, "engine_version": self.version, "language": language}


def ingest_bytes(name: str, data: bytes, *, limits: IngestionLimits | None = None, ocr: OCRAdapter | None = None, ocr_language: str = "chi_sim+chi_tra+eng") -> StructuredDocument:
    limits = limits or IngestionLimits()
    safe_name = safe_upload_name(name)
    if not isinstance(data, bytes):
        raise IngestionError("上传内容必须是原始字节")
    if not data:
        raise IngestionError("文件不能为空")
    if len(data) > limits.max_file_bytes:
        raise IngestionError(f"文件超过 {limits.max_file_bytes // (1024 * 1024)} MiB 安全上限")
    source = _binding(safe_name, data)
    suffix = source.extension
    if suffix in {".md", ".txt"}:
        return _text_blocks(_decode_text(data), source, parser="plain-text")
    if suffix == ".docx":
        return _parse_docx(data, source, limits)
    if suffix == ".pdf":
        document = _pdf_text(data, source, limits)
        scan_pages = [warning for warning in document.warnings if warning.code == "scan-pages-detected"]
        if scan_pages:
            scan_page_numbers = {page for warning in scan_pages for page in warning.details.get("pages", [])}
            adapter = ocr or TesseractOCR(timeout_seconds=limits.max_ocr_seconds_per_page)
            available, detail = adapter.available()
            document.quality.ocr_available = available
            if not available:
                document.warnings.append(ExtractionWarning("ocr-unavailable", "critical", detail))
                document.quality.requires_confirmation = True
                return document
            # The adapter is intentionally page-oriented. PyMuPDF is used only
            # when available to render pages; a missing renderer is a hard stop.
            try:
                import fitz  # type: ignore
            except ImportError as exc:
                document.warnings.append(ExtractionWarning("pdf-renderer-unavailable", "critical", "扫描 PDF 需要 PyMuPDF 进行页渲染；请安装 pymupdf"))
                document.quality.ocr_available = False
                return document
            pdf = fitz.open(stream=data, filetype="pdf")
            ocr_blocks: list[DocumentBlock] = []
            recognized_pages: set[int] = set()
            low_confidence = 0
            for page_number, page in enumerate(pdf, start=1):
                if scan_page_numbers and page_number not in scan_page_numbers:
                    continue
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                result = adapter.recognize_pdf_page(pix.tobytes("png"), page_number=page_number, language=ocr_language)
                text = str(result.get("text", "")).strip()
                low_confidence += int(result.get("low_confidence_words", 0))
                if not text:
                    if page_number not in document.quality.blank_pages:
                        document.quality.blank_pages.append(page_number)
                    continue
                recognized_pages.add(page_number)
                block_id = _block_id(source.sha256, "ocr_block", len(document.blocks) + len(ocr_blocks), text, page_number)
                block = DocumentBlock(block_id, "paragraph", text=text, location=DocumentLocation(block_id, "ocr_block", page=page_number, paragraph=len(document.blocks) + len(ocr_blocks), source_path=source.original_name), attrs={"ocr": True, "confidence": result.get("confidence", 0), "engine": result.get("engine", adapter.name), "engine_version": result.get("engine_version", adapter.version), "language": result.get("language", ocr_language)})
                ocr_blocks.append(block)
                document.source_to_block.append({"page": page_number, "block_id": block_id, "ocr": True, "confidence": result.get("confidence", 0)})
            document.blocks = [block for block in document.blocks if block.location and block.location.page not in scan_page_numbers and block.location.page not in document.quality.blank_pages] + ocr_blocks
            document.quality.blank_pages = sorted(page for page in document.quality.blank_pages if page not in recognized_pages)
            document.quality.ocr_low_confidence_blocks = low_confidence
            document.quality.text_coverage = min(1.0, sum(len(block.text) for block in document.blocks) / max(1, document.quality.page_count * 1200))
            document.metadata["ocr"] = {"engine": adapter.name, "version": adapter.version, "language": ocr_language, "human_corrected": False}
            document.warnings.append(ExtractionWarning("ocr-used", "medium", f"扫描页使用 {adapter.name}；低置信词数 {low_confidence}"))
        return document
    raise IngestionError("未实现的文件类型")


_REPAIRABLE_PYTHON_PACKAGES = {
    "pypdf": "pypdf>=5.0,<7.0",
    "pymupdf": "pymupdf>=1.24,<2.0",
}


def doctor_dependencies() -> list[dict[str, Any]]:
    """Return dependency status plus safe, user-facing repair metadata."""
    rows: list[dict[str, Any]] = []
    for package, label, optional, purpose, license_name in (
        ("pypdf", "pypdf", True, "PDF 文本页解析", "BSD-3-Clause"),
        ("fitz", "pymupdf", True, "PDF 坐标、扫描页渲染", "AGPL-3.0-or-later / commercial"),
    ):
        repair_spec = _REPAIRABLE_PYTHON_PACKAGES[label]
        base = {"name": label, "available": False, "optional": optional, "purpose": purpose, "license": license_name, "repairable": True, "repair_key": label, "install": f"python -m pip install {repair_spec}"}
        try:
            module = __import__(package)
            base.update({"available": True, "version": getattr(module, "__version__", "installed")})
            base.pop("install", None)
        except ImportError:
            pass
        rows.append(base)
    ocr = TesseractOCR()
    available, detail = ocr.available()
    rows.append({"name": "tesseract", "available": available, "optional": True, "purpose": "扫描 PDF OCR（chi_sim/chi_tra/eng）", "license": "Apache-2.0 engine; language-data terms vary", "detail": detail, "repairable": False, "repair_key": "tesseract", "repair_hint": "需要在操作系统中安装 Tesseract 5.x 及 chi_sim、chi_tra、eng 语言包；应用不会静默安装系统软件"})
    return rows


def repair_dependency(name: str) -> list[dict[str, Any]]:
    """Install one supported Python adapter and return refreshed diagnostics.

    System OCR engines are intentionally excluded: installing them requires a
    platform package manager and language-data consent outside this app.
    """
    package = _REPAIRABLE_PYTHON_PACKAGES.get(name)
    if package is None:
        raise IngestionError(f"依赖 {name} 不支持应用内自动修复；请按环境提示处理")
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", package]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise IngestionError(f"自动安装 {name} 失败：{exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "无安装器输出").strip()[-1200:]
        raise IngestionError(f"自动安装 {name} 失败：{detail}")
    importlib.invalidate_caches()
    module_name = "fitz" if name == "pymupdf" else name
    try:
        importlib.import_module(module_name)
    except ImportError as exc:
        raise IngestionError(f"{name} 安装命令已完成，但当前 Python 仍无法导入；请重启应用后重试") from exc
    return doctor_dependencies()


def repair_dependencies(names: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Repair all requested/missing Python adapters, then re-run the doctor."""
    selected = list(names) if names is not None else [row["repair_key"] for row in doctor_dependencies() if not row["available"] and row.get("repairable")]
    for name in selected:
        repair_dependency(name)
    return doctor_dependencies()


__all__ = [
    "IngestionError", "IngestionLimits", "OCRAdapter", "ParserUnavailable", "TesseractOCR",
    "doctor_dependencies", "repair_dependency", "repair_dependencies", "ingest_bytes", "safe_upload_name",
]
