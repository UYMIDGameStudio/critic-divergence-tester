"""Edit supported Word text without rebuilding its package or layout.

The review model omits empty paragraphs and normalizes outer whitespace.
Paragraph formatting, paragraph marks, and section boundaries are independent
of text: deleting text must never implicitly delete those structures.
"""
from __future__ import annotations

import difflib
import hashlib
import io
import itertools
import re
import zipfile
from xml.dom import minidom

from document_review_ingest import IngestionLimits, _zip_safety, _docx_xml

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XMLNS = "http://www.w3.org/2000/xmlns/"
TEXT_KINDS = {"paragraph", "heading", "list_item"}
PROTECTED_KINDS = {"header", "footer", "image_placeholder", "page_break", "table"}
REVISION_TAGS = {"ins", "del", "moveFrom", "moveTo", "pPrChange", "rPrChange", "sectPrChange", "tblPrChange", "tcPrChange", "trPrChange"}


class WordEditUnsupported(ValueError):
    pass


def _elements(node):
    return [n for n in node.childNodes if n.nodeType == n.ELEMENT_NODE]


def _children(node, name):
    return [n for n in _elements(node) if n.namespaceURI == W and n.localName == name]


def _all(node, name):
    return list(node.getElementsByTagNameNS(W, name))


def _text(node):
    parts = []
    for child in node.getElementsByTagName("*"):
        if child.namespaceURI != W:
            continue
        if child.localName == "t":
            parts.append("".join(c.data for c in child.childNodes if c.nodeType in {c.TEXT_NODE, c.CDATA_SECTION_NODE}))
        elif child.localName == "tab":
            parts.append("\t")
        elif child.localName in {"br", "cr"}:
            parts.append("\n")
    return "".join(parts)


def _element(document, name, **attrs):
    prefix = getattr(document, "word_prefix", "w")
    node = document.createElementNS(W, prefix + ":" + name)
    for key, value in attrs.items():
        node.setAttributeNS(W, prefix + ":" + key, str(value))
    return node


def _declare_word_prefix(document):
    declarations = [attribute for node in document.getElementsByTagName("*") for attribute in node.attributes.values() if attribute.namespaceURI == XMLNS]
    prefix = "w"
    if any(attribute.localName == "w" and attribute.value != W for attribute in declarations):
        existing = {attribute.localName for attribute in declarations}
        prefix = "drw"
        while prefix in existing:
            prefix += "x"
    document.word_prefix = prefix
    document.documentElement.setAttributeNS(XMLNS, "xmlns:" + prefix, W)


def _put(node, text):
    node.setAttributeNS("http://www.w3.org/XML/1998/namespace", "xml:space", "preserve")
    node.appendChild(node.ownerDocument.createTextNode(text))


def _editable(paragraph):
    """Allow plain formatted runs, including soft breaks; reject hidden payloads."""
    if paragraph.namespaceURI != W or paragraph.localName != "p":
        raise WordEditUnsupported("修改位置不是 Word 段落")
    for node in _elements(paragraph):
        if node.namespaceURI != W or node.localName not in {"pPr", "r"}:
            raise WordEditUnsupported("修改段落包含图片、域、链接、批注、书签或暂不支持的内联结构")
    if any(_all(paragraph, name) for name in REVISION_TAGS):
        raise WordEditUnsupported("修改位置包含已有修订")
    for run in _children(paragraph, "r"):
        for node in _elements(run):
            if node.namespaceURI != W or node.localName not in {"rPr", "t", "tab", "br", "cr"}:
                raise WordEditUnsupported("修改位置包含图片、域、脚注、符号或其他非文本内容")
            if node.localName == "br" and node.getAttributeNS(W, "type") not in {"", "textWrapping"}:
                raise WordEditUnsupported("修改位置包含分页或分栏符；请在 Word 中处理该位置")


def _opcodes(old, new):
    # Repetitive large paragraphs can make character-level matching quadratic.
    # Bound detailed comparison to the changed middle, keeping common edges.
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(old), len(new)) - prefix and old[-suffix - 1] == new[-suffix - 1]:
        suffix += 1
    old_end, new_end = len(old) - suffix, len(new) - suffix
    if prefix:
        yield "equal", 0, prefix, 0, prefix
    if old_end > prefix or new_end > prefix:
        if old_end - prefix + new_end - prefix <= 12000:
            for tag, start, end, new_start, new_stop in difflib.SequenceMatcher(None, old[prefix:old_end], new[prefix:new_end], autojunk=True).get_opcodes():
                yield tag, start + prefix, end + prefix, new_start + prefix, new_stop + prefix
        else:
            tag = "replace" if old_end > prefix and new_end > prefix else ("delete" if old_end > prefix else "insert")
            yield tag, prefix, old_end, prefix, new_end
    if suffix:
        yield "equal", old_end, len(old), new_end, len(new)


def _run(document, text, template=None, *, deleted=False):
    run = template.cloneNode(False) if template is not None else _element(document, "r")
    if template is not None:
        for props in _children(template, "rPr"):
            run.appendChild(props.cloneNode(True))
    for value in re.split(r"([\t\n])", text):
        if not value:
            continue
        if value in {"\t", "\n"}:
            leaf = _element(document, "tab" if value == "\t" else "br")
        else:
            leaf = _element(document, "delText" if deleted else "t")
            _put(leaf, value)
        run.appendChild(leaf)
    return run


def _run_slices(runs, start, end, *, deleted=False):
    offset = 0
    for run in runs:
        text = _text(run)
        left, right = max(0, start - offset), min(len(text), end - offset)
        if left < right:
            if left == 0 and right == len(text) and not deleted:
                yield run.cloneNode(True)
            else:
                yield _run(run.ownerDocument, text[left:right], run, deleted=deleted)
        offset += len(text)


def _template_at(runs, start):
    offset = 0
    for run in runs:
        offset += len(_text(run))
        if start < offset:
            return run
    return runs[-1] if runs else None


def _replace_text(paragraph, text, tracked=False, change_ids=None):
    old = _text(paragraph)
    if old == text:
        return
    _editable(paragraph)
    if any(not (c in "\t\n" or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF) for c in text):
        raise WordEditUnsupported("修改文本包含不能写入 Word 的控制字符")
    document = paragraph.ownerDocument
    runs = _children(paragraph, "r")
    result = []
    change_ids = change_ids if change_ids is not None else itertools.count(1)
    for tag, start, end, new_start, new_end in _opcodes(old, text):
        if tag == "equal":
            result.extend(_run_slices(runs, start, end))
            continue
        if tracked and end > start:
            deletion = _element(document, "del", id=next(change_ids), author="Document Review Studio")
            for run in _run_slices(runs, start, end, deleted=True):
                deletion.appendChild(run)
            result.append(deletion)
        if new_end > new_start:
            run = _run(document, text[new_start:new_end], _template_at(runs, start))
            if tracked:
                insertion = _element(document, "ins", id=next(change_ids), author="Document Review Studio")
                insertion.appendChild(run)
                result.append(insertion)
            else:
                result.append(run)
    for run in runs:
        paragraph.removeChild(run)
    for node in result:
        paragraph.appendChild(node)


def _checked_index(value, length, label):
    if type(value) is not int or not 0 <= value < length:
        raise WordEditUnsupported(label + "定位无效")
    return value


def _cell(table, location):
    if table.localName != "tbl":
        raise WordEditUnsupported("表格单元格绑定到非表格位置")
    rows = _children(table, "tr")
    row = rows[_checked_index(location.row, len(rows), "表格行")]
    if type(location.column) is not int or location.column < 0:
        raise WordEditUnsupported("表格列定位无效")
    column = 0
    for candidate in _children(row, "tc"):
        props = _children(candidate, "tcPr")
        spans = _children(props[0], "gridSpan") if props else []
        try:
            span = int(spans[0].getAttributeNS(W, "val")) if spans else 1
        except ValueError as exc:
            raise WordEditUnsupported("表格合并跨度无效") from exc
        if span < 1:
            raise WordEditUnsupported("表格合并跨度无效")
        if column == location.column:
            return candidate
        column += span
    raise WordEditUnsupported("表格单元格定位无效")


def _reconciled_text(node, block, final):
    old = _text(node)
    if old.strip() != block.text:
        raise WordEditUnsupported("Word 原文与已确认审查文本不一致，拒绝错误定位")
    # Ingestion normalizes outer whitespace; keep the original Word spacing.
    start = len(old) - len(old.lstrip())
    stop = len(old.rstrip())
    if not old.strip():
        return final.text
    return old[:start] + final.text + old[stop:]


def _replace_cell(cell, desired, tracked, change_ids):
    if _all(cell, "tbl") or any(node.namespaceURI != W or node.localName not in {"tcPr", "p"} for node in _elements(cell)):
        raise WordEditUnsupported("单元格包含嵌套表格或暂不支持的结构")
    paragraphs = _children(cell, "p")
    if not paragraphs:
        raise WordEditUnsupported("Word 单元格缺少段落")
    values = [_text(node) for node in paragraphs]
    offsets = list(itertools.accumulate([0] + [len(value) for value in values]))
    outputs = [[] for _ in values]
    # The IR concatenates cell paragraphs. Map the character diff back across
    # those spans so original paragraph properties and paragraph marks remain.
    for tag, start, end, new_start, new_end in _opcodes("".join(values), desired):
        if tag == "equal":
            for i, value in enumerate(values):
                left, right = max(0, start - offsets[i]), min(len(value), end - offsets[i])
                if left < right:
                    outputs[i].append(value[left:right])
        elif new_end > new_start:
            index = next((i for i in range(len(values)) if offsets[i + 1] > start), len(values) - 1)
            outputs[index].append(desired[new_start:new_end])
    for paragraph, parts in zip(paragraphs, outputs):
        _replace_text(paragraph, "".join(parts), tracked, change_ids)


def _bind_blocks(body, original, revised):
    originals = {b.block_id: b for b in original.blocks}
    finals = {b.block_id: b for b in revised.blocks}
    if len(originals) != len(original.blocks) or len(finals) != len(revised.blocks):
        raise WordEditUnsupported("文档包含重复文本块标识")
    if [b.block_id for b in original.blocks if b.block_id in finals] != [b.block_id for b in revised.blocks if b.block_id in originals]:
        raise WordEditUnsupported("原有文本块发生重排，无法按原定位输出")
    source_nodes = [n for n in _elements(body) if n.namespaceURI == W and n.localName in {"p", "tbl"}]
    nodes, text_nodes = {}, set()
    for block in original.blocks:
        final = finals.get(block.block_id)
        if final is not None and (final.kind != block.kind or final.level != block.level or final.children != block.children):
            raise WordEditUnsupported("原有文本块的类型、层级或表格结构发生变化")
        if block.kind in PROTECTED_KINDS:
            if final is None or final.text != block.text:
                raise WordEditUnsupported("页眉页脚、图片、分页或表格容器不能作为文本替换")
            if block.kind not in {"image_placeholder", "table"}:
                continue
        elif block.kind not in TEXT_KINDS | {"table_cell"}:
            raise WordEditUnsupported("原文包含无法安全定位的文本块类型")
        location = block.location
        if location is None:
            raise WordEditUnsupported("Word 原文缺少定位")
        node = source_nodes[_checked_index(location.paragraph, len(source_nodes), "Word 正文")]
        expected = "tbl" if block.kind in {"table", "table_cell"} else "p"
        if node.localName != expected:
            raise WordEditUnsupported("Word 原文定位类型不一致")
        if block.kind == "table_cell":
            container = originals.get(location.table_id)
            if container is None or container.kind != "table" or container.location is None or container.location.paragraph != location.paragraph or block.block_id not in container.children:
                raise WordEditUnsupported("表格单元格与容器绑定不一致")
            node = _cell(node, location)
            if final is None:
                raise WordEditUnsupported("不能删除单个表格单元格")
        if block.kind not in PROTECTED_KINDS:
            if node in text_nodes:
                raise WordEditUnsupported("多个文本块重复绑定到同一 Word 位置")
            text_nodes.add(node)
            if _text(node).strip() != block.text:
                raise WordEditUnsupported("Word 原文与已确认审查文本不一致，拒绝错误定位")
        nodes[block.block_id] = node
    return originals, finals, nodes


def _new_paragraph(doc, block, neighbors, nodes):
    if block.kind not in TEXT_KINDS:
        raise WordEditUnsupported("新增内容不是支持的段落类型")
    paragraph = _element(doc, "p")
    template = next((nodes[b.block_id] for b in neighbors if b.block_id in nodes and b.kind == block.kind and b.level == block.level and nodes[b.block_id].localName == "p"), None)
    if template is not None:
        for props in _children(template, "pPr"):
            copy = props.cloneNode(True)
            for section in _children(copy, "sectPr"):
                copy.removeChild(section)
            paragraph.appendChild(copy)
    if block.kind == "heading" and template is None:
        level = block.level or 1
        if type(level) is not int or not 1 <= level <= 9:
            raise WordEditUnsupported("新增标题层级必须为 1–9")
        props = _element(doc, "pPr")
        props.appendChild(_element(doc, "pStyle", val="Heading" + str(level)))
        props.appendChild(_element(doc, "outlineLvl", val=level - 1))
        paragraph.appendChild(props)
    if block.kind == "list_item" and not _all(paragraph, "numPr"):
        raise WordEditUnsupported("新增列表项缺少可继承的 Word 编号定义")
    if template is not None:
        runs = _children(template, "r")
        if runs:
            paragraph.appendChild(_run(doc, "", runs[0]))
    _replace_text(paragraph, block.text)
    return paragraph


def _move_split_sections(revised, originals, nodes):
    """A split carries the old paragraph mark to its final new paragraph.

    This relies on explicit provenance from revision generation. An ordinary
    insert_after remains in the next section when its anchor ends a section.
    """
    groups = {}
    for index, block in enumerate(revised.blocks):
        source_id = block.attrs.get("split_from_block_id")
        if not source_id or block.block_id in originals:
            continue
        if not isinstance(source_id, str) or source_id not in originals or originals[source_id].kind not in TEXT_KINDS:
            raise WordEditUnsupported("拆分段落缺少有效的来源段落")
        previous = revised.blocks[index - 1] if index else None
        if previous is None or not (previous.block_id == source_id or previous.attrs.get("split_from_block_id") == source_id):
            raise WordEditUnsupported("拆分段落必须紧随其来源段落")
        groups[source_id] = block.block_id
    moved = 0
    for source_id, last_id in groups.items():
        source = nodes[source_id]
        target = nodes[last_id]
        props = _children(source, "pPr")
        sections = _children(props[0], "sectPr") if props else []
        if not sections:
            continue
        target_props = _children(target, "pPr")
        if not target_props:
            target.insertBefore(_element(target.ownerDocument, "pPr"), target.firstChild)
            target_props = _children(target, "pPr")
        for section in sections:
            props[0].removeChild(section)
            target_props[0].appendChild(section)
            moved += 1
    return moved


def _body_anchor(node, body):
    while node is not None and node.parentNode is not body:
        node = node.parentNode
    return node


def preserve_docx(source_bytes, original, revised, *, tracked=False):
    if hashlib.sha256(source_bytes).hexdigest() != original.source.sha256:
        raise WordEditUnsupported("Word 原件摘要与审查文档不一致")
    archive = _zip_safety(source_bytes, IngestionLimits())
    if _docx_xml(archive, "word/document.xml") is None:
        raise WordEditUnsupported("Word 原件缺少正文")
    if original.quality.human_corrected or original.metadata.get("revisions_present"):
        raise WordEditUnsupported("原件含既有修订或识别全文经过人工替换，无法安全绑定原位 Word 修改")
    doc = minidom.parseString(archive.read("word/document.xml"))
    if any(_all(doc, name) for name in REVISION_TAGS):
        raise WordEditUnsupported("原件含既有修订，无法安全绑定原位 Word 修改")
    settings = _docx_xml(archive, "word/settings.xml")
    if settings is not None and settings.find(f"{{{W}}}trackRevisions") is not None:
        raise WordEditUnsupported("原件已启用修订跟踪，无法安全绑定原位 Word 修改")
    body_nodes = _children(doc.documentElement, "body")
    if doc.documentElement.namespaceURI != W or doc.documentElement.localName != "document" or len(body_nodes) != 1:
        raise WordEditUnsupported("Word 正文结构无效")
    # Do not rebind a source prefix used by another namespace, even in an
    # unrelated part of document.xml. New nodes use a collision-free prefix.
    _declare_word_prefix(doc)
    body = body_nodes[0]
    originals, finals, nodes = _bind_blocks(body, original, revised)
    existing_ids = [int(node.getAttributeNS(W, "id")) for node in doc.getElementsByTagName("*") if node.getAttributeNS(W, "id").isdigit()]
    change_ids = itertools.count(max(existing_ids, default=0) + 1)
    changed, structural, retained_sections = 0, False, 0
    for block in original.blocks:
        final = finals.get(block.block_id)
        if block.kind in PROTECTED_KINDS or (final is not None and final.text == block.text):
            continue
        node = nodes[block.block_id]
        changed += 1
        if final is None:
            if tracked:
                raise WordEditUnsupported("本轮含段落删除；可导出保格式净稿，原生修订副本仅支持段内替换")
            _editable(node)
            if _all(node, "sectPr"):
                # A section break belongs to the paragraph mark, not the text.
                # Keep its empty paragraph so header/footer/page layout survive.
                _replace_text(node, "")
                retained_sections += 1
            else:
                node.parentNode.removeChild(node)
            structural = True
        else:
            desired = _reconciled_text(node, block, final)
            if block.kind == "table_cell":
                _replace_cell(node, desired, tracked, change_ids)
            else:
                _replace_text(node, desired, tracked, change_ids)
    for index, block in enumerate(revised.blocks):
        if block.block_id in originals:
            continue
        if tracked:
            raise WordEditUnsupported("本轮含新增段落；原生修订副本仅支持段内替换")
        # Table cells form one indivisible body node. A body paragraph between
        # a container and its cells cannot represent the reviewed order.
        following = revised.blocks[index + 1:]
        next_original = next((b for b in following if b.block_id in originals), None)
        previous_original = next((b for b in reversed(revised.blocks[:index]) if b.block_id in originals), None)
        previous_anchor = _body_anchor(nodes.get(previous_original.block_id), body) if previous_original else None
        next_anchor = _body_anchor(nodes.get(next_original.block_id), body) if next_original else None
        if next_original is not None and (next_original.kind == "table_cell" or (previous_anchor is not None and previous_anchor is next_anchor)):
            raise WordEditUnsupported("不能将正文段落插入表格单元格或同一图片段落内部")
        next_node = next((nodes[b.block_id] for b in following if b.block_id in nodes and nodes[b.block_id].parentNode == body), None)
        if next_node is None:
            sections = _children(body, "sectPr")
            next_node = sections[0] if sections else None
        neighbors = list(reversed(revised.blocks[:index])) + following
        node = _new_paragraph(doc, block, neighbors, nodes)
        body.insertBefore(node, next_node)
        nodes[block.block_id] = node
        changed += 1
        structural = True
    moved_sections = _move_split_sections(revised, originals, nodes)
    report = {"source_layout_preserved": True, "native_track_changes": tracked, "changed_elements": changed, "structural_edits": structural, "unchanged_parts_preserved": True, "retained_section_boundaries": retained_sections, "moved_section_boundaries": moved_sections}
    if not changed:
        return source_bytes, report
    main = doc.toxml(encoding="utf-8")
    minidom.parseString(main)
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as output:
        for name in archive.namelist():
            output.writestr(name, main if name == "word/document.xml" else archive.read(name))
    return result.getvalue(), report
