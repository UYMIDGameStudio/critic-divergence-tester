"""Edit supported Word text without rebuilding its package or layout.

The review model omits empty paragraphs and normalizes outer whitespace.
Paragraph formatting, paragraph marks, and section boundaries are independent
of text: deleting text must never implicitly delete those structures.
"""
from __future__ import annotations

import difflib
from bisect import bisect_left, bisect_right
import hashlib
import io
import itertools
import re
import unicodedata
import zipfile
from xml.dom import minidom

from document_review_ingest import (IngestionLimits, IngestionError, _zip_safety, _docx_xml, _docx_story_revisions,
    _docx_tracking_enabled, _docx_grid_value, _docx_content_issues, DOCX_REVISION_TAGS,
    DOCX_CONTENT_WRAPPERS, DOCX_CONTENT_LAYOUT, DOCX_NOTE_LAYOUT, DOCX_TEXT_PROPERTIES, _docx_note_parts, _docx_note_id)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XMLNS = "http://www.w3.org/2000/xmlns/"
TEXT_KINDS = {"paragraph", "heading", "list_item"}
PROTECTED_KINDS = {"header", "footer", "image_placeholder", "page_break", "table"}
REVISION_TAGS = DOCX_REVISION_TAGS


class WordEditUnsupported(ValueError):
    pass


def _elements(node):
    return [n for n in node.childNodes if n.nodeType == n.ELEMENT_NODE]


def _children(node, name):
    return [n for n in _elements(node) if n.namespaceURI == W and n.localName == name]


def _all(node, name):
    return list(node.getElementsByTagNameNS(W, name))


def _content_nodes(parent, names):
    """Match the ingestion walk while retaining actual DOM parent nodes."""
    for child in _elements(parent):
        if child.namespaceURI != W:
            continue
        if child.localName in names:
            yield child
        elif child.localName == 'sdt':
            contents = _children(child, 'sdtContent')
            if len(contents) != 1:
                raise WordEditUnsupported('Word 内容控件结构无法可靠定位')
            yield from _content_nodes(contents[0], names)
        elif child.localName in DOCX_CONTENT_WRAPPERS:
            yield from _content_nodes(child, names)


def _wrapped_ancestor(node):
    while node is not None:
        if node.namespaceURI == W and node.localName in DOCX_CONTENT_WRAPPERS | {'sdt'}:
            return True
        node = node.parentNode
    return False


def _check_control_edit(node):
    text_properties = {'rPr', 'alias', 'id', 'tag', 'lock', 'text', 'richText', 'group', 'placeholder', 'showingPlcHdr'}
    while node is not None:
        if node.namespaceURI == W and node.localName == 'sdt':
            props = _children(node, 'sdtPr')
            if len(props) > 1:
                raise WordEditUnsupported('Word 内容控件属性重复，无法安全原位修改')
            if props and (_all(props[0], 'dataBinding') or any(
                    lock.getAttributeNS(W, 'val') not in {'unlocked', 'sdtLocked'} for lock in _all(props[0], 'lock'))):
                raise WordEditUnsupported('内容控件含数据绑定或内容锁定，无法安全原位修改')
            # Office extensions also define bindings and repeating controls;
            # do not treat an unknown extended property as permission to edit.
            if props and any(child.namespaceURI != W for child in _elements(props[0])):
                raise WordEditUnsupported('内容控件含暂不支持的扩展属性，无法安全原位修改')
            if props and any(child.localName not in text_properties for child in _elements(props[0])):
                raise WordEditUnsupported('内容控件含日期、选项或暂不支持的属性，无法安全原位修改')
            if props and any(flag.getAttributeNS(W, 'val').casefold() not in {'false', 'off', '0'}
                    for flag in _children(props[0], 'showingPlcHdr')):
                raise WordEditUnsupported('内容控件仍显示占位文本，需要先在 Word 中填写或转换为普通文本')
        node = node.parentNode


def _text(node):
    parts = []
    def visible_children(parent):
        for child in _elements(parent):
            if child.namespaceURI == W and child.localName in DOCX_TEXT_PROPERTIES:
                continue
            yield child
            yield from visible_children(child)
    for child in visible_children(node):
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


def _editable(paragraph, *, allow_references=False):
    """Allow plain formatted runs, including soft breaks; reject hidden payloads."""
    if paragraph.namespaceURI != W or paragraph.localName != "p":
        raise WordEditUnsupported("修改位置不是 Word 段落")
    runs = []
    for node in _elements(paragraph):
        if node.namespaceURI != W or node.localName not in {"pPr", "r", "hyperlink"}:
            raise WordEditUnsupported("修改段落包含图片、域、链接、批注、书签或暂不支持的内联结构")
        if node.localName == 'r':
            runs.append(node)
        elif node.localName == 'hyperlink':
            children = list(_elements(node))
            if any(child.namespaceURI != W or child.localName != 'r' for child in children):
                raise WordEditUnsupported('超链接包含域、书签、嵌套链接或暂不支持的内联结构')
            runs.extend(children)
    if any(_all(paragraph, name) for name in REVISION_TAGS):
        raise WordEditUnsupported("修改位置包含已有修订")
    for run in runs:
        for node in _elements(run):
            allowed = {"rPr", "t", "tab", "br", "cr"}
            if allow_references:
                allowed |= {'footnoteReference', 'endnoteReference', 'footnoteRef', 'endnoteRef'}
            if node.namespaceURI != W or node.localName not in allowed:
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
    if _text(paragraph) == text:
        return
    if not _children(paragraph, 'hyperlink'):
        return _replace_plain_text(paragraph, text, tracked, change_ids)
    _check_control_edit(paragraph)
    _editable(paragraph, allow_references=True)
    marker_names = ('footnoteReference', 'endnoteReference', 'footnoteRef', 'endnoteRef')
    if sum(len(_all(paragraph, name)) for name in marker_names) > 1024:
        raise WordEditUnsupported('单段注释标记过多，请在 Word 中核对并修改该位置')
    # Every link is one owner. Plain gaps also exist between adjacent links and
    # at the paragraph edges. Independent boundary insertions stay plain;
    # contiguous tails of an edited label are handled below.
    groups, runs = [], []
    for node in _elements(paragraph):
        if node.localName == 'r':
            runs.append(node)
        elif node.localName == 'hyperlink':
            groups.append({'link': None, 'runs': runs})
            groups.append({'link': node, 'runs': _children(node, 'r')})
            runs = []
    groups.append({'link': None, 'runs': runs})
    position, boundary_plain, boundary_links = 0, {}, {}
    for index, group in enumerate(groups):
        group['old'] = ''.join(_text(run) for run in group['runs'])
        group['start'], group['end'] = position, position + len(group['old'])
        group['pieces'] = []
        position = group['end']
        if group['link'] is None:
            boundary_plain.setdefault(group['start'], index)
            boundary_plain.setdefault(group['end'], index)
        else:
            boundary_links.setdefault(group['start'], []).append(index)
            boundary_links.setdefault(group['end'], []).append(index)
    starts, ends = [g['start'] for g in groups], [g['end'] for g in groups]
    old = ''.join(g['old'] for g in groups)
    if old != _text(paragraph):
        raise WordEditUnsupported('超链接段落含未映射的文字，无法安全保留链接范围')
    changes, edited_links = [], set()
    for opcode in _opcodes(old, text):
        tag, start, end, new_start, new_end = opcode
        owners = [i for i in range(bisect_right(ends, start), bisect_left(starts, end))
                  if groups[i]['start'] < groups[i]['end']] if start < end else []
        changes.append((opcode, owners))
        if tag != 'equal' and len(owners) == 1 and groups[owners[0]]['link'] is not None:
            edited_links.add(owners[0])
    def word_fragment(value):
        return bool(value) and all(char == '_' or unicodedata.category(char)[0] in 'LNM' for char in value)
    def insertion_group(position, fragment):
        owner = boundary_plain.get(position)
        candidates = list(dict.fromkeys(i for i in boundary_links.get(position, []) if i in edited_links))
        if len(candidates) == 1 and word_fragment(fragment):
            candidate = groups[candidates[0]]
            adjacent = candidate['old'][-1:] if position == candidate['end'] else candidate['old'][:1]
            if word_fragment(adjacent): owner = candidates[0]
        return bisect_right(ends, position) if owner is None else owner
    def character_link(position):
        return groups[bisect_right(ends, position)]['link']
    # A repeated passage can have several equivalent character alignments.
    # Refuse if sliding one edit across unchanged context changes link ownership.
    # This catches identical citation labels pointing at different sources.
    edits = [opcode for opcode, _ in changes if opcode[0] != 'equal']
    alignment_work = 0
    for index, (_, start, end, new_start, new_end) in enumerate(edits):
        left_old, left_new = (edits[index - 1][2], edits[index - 1][4]) if index else (0, 0)
        right_old, right_new = (edits[index + 1][1], edits[index + 1][3]) if index + 1 < len(edits) else (len(old), len(text))
        for direction in (-1, 1):
            a, b, c, d = start, end, new_start, new_end
            while ((a > left_old and c > left_new and old[b - 1] == text[d - 1]) if direction < 0
                   else (b < right_old and d < right_new and old[a] == text[c])):
                alignment_work += 1
                if alignment_work > 12000:
                    raise WordEditUnsupported('超链接重复文字的定位检查超过安全上限，请在 Word 中核对')
                if a == b:
                    first = groups[insertion_group(a, text[c:d])]['link']
                    second = groups[insertion_group(a + direction, text[c + direction:d + direction])]['link']
                elif direction < 0:
                    first, second = character_link(a - 1), character_link(b - 1)
                else:
                    first, second = character_link(a), character_link(b)
                if first is not second:
                    raise WordEditUnsupported('重复文字使超链接修改归属不明确；请在 Word 中核对该位置')
                a += direction; b += direction; c += direction; d += direction
    for (tag, start, end, new_start, new_end), owners in changes:
        if tag == 'equal':
            for i in owners:
                group = groups[i]
                group['pieces'].append(old[max(start, group['start']):min(end, group['end'])])
        elif new_end > new_start:
            if start == end:
                # Matching a short old suffix can split a label replacement
                # (CITATION -> CITIN + G). Keep a contiguous word tail with the
                # link already being edited, instead of producing a partial
                # clickable word. Standalone boundary insertions stay plain.
                owner = insertion_group(start, text[new_start:new_end])
            elif len(owners) == 1:
                owner = owners[0]
            else:
                raise WordEditUnsupported('修改跨越超链接边界，无法确认新文字的链接归属；请拆分修改或在 Word 中核对')
            groups[owner]['pieces'].append(text[new_start:new_end])
        # Deletions carry no new ownership and can span several containers.
    document = paragraph.ownerDocument
    change_ids = change_ids if change_ids is not None else itertools.count(1)
    result = []
    for group in groups:
        desired = ''.join(group['pieces'])
        link = group['link']
        if desired == group['old']:
            result.extend([link.cloneNode(True)] if link is not None else [run.cloneNode(True) for run in group['runs']])
            continue
        temporary = _element(document, 'p')
        for run in group['runs']:
            temporary.appendChild(run.cloneNode(True))
        _replace_plain_text(temporary, desired, tracked, change_ids)
        children = list(_elements(temporary))
        if link is not None:
            wrapper = link.cloneNode(False)
            for child in children:
                wrapper.appendChild(child)
            result.append(wrapper)
        else:
            result.extend(children)
    for node in list(_elements(paragraph)):
        if node.localName in {'r', 'hyperlink'}:
            paragraph.removeChild(node)
    for node in result:
        paragraph.appendChild(node)
    if _text(paragraph) != text:
        raise WordEditUnsupported('超链接修改后的文字与已批准文本不一致')


def _replace_plain_text(paragraph, text, tracked=False, change_ids=None):
    old = _text(paragraph)
    if old == text:
        return
    _check_control_edit(paragraph)
    _editable(paragraph, allow_references=True)
    if any(not (c in "\t\n" or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF) for c in text):
        raise WordEditUnsupported("修改文本包含不能写入 Word 的控制字符")
    document = paragraph.ownerDocument
    runs = _children(paragraph, "r")
    markers, offset = {}, 0
    marker_count = 0
    for run in runs:
        for child in _elements(run):
            if child.namespaceURI == W and child.localName in {'footnoteReference', 'endnoteReference', 'footnoteRef', 'endnoteRef'}:
                marker_run = run.cloneNode(False)
                for props in _children(run, 'rPr'):
                    marker_run.appendChild(props.cloneNode(True))
                marker_run.appendChild(child.cloneNode(True))
                markers.setdefault(offset, []).append(marker_run)
                marker_count += 1
                if marker_count > 1024:
                    raise WordEditUnsupported('单段注释标记过多，请在 Word 中核对并修改该位置')
                run.removeChild(child)
            elif child.namespaceURI == W and child.localName == 't':
                offset += sum(len(c.data) for c in child.childNodes if c.nodeType in {c.TEXT_NODE, c.CDATA_SECTION_NODE})
            elif child.namespaceURI == W and child.localName in {'tab', 'br', 'cr'}:
                offset += 1
    result = []
    emitted = set()
    def emit_markers(position):
        if position not in emitted:
            result.extend(markers.get(position, []))
            emitted.add(position)
    change_ids = change_ids if change_ids is not None else itertools.count(1)
    for tag, start, end, new_start, new_end in _opcodes(old, text):
        emit_markers(start)
        if tag == "equal":
            cursor = start
            for boundary in [*sorted(position for position in markers if start < position < end), end]:
                result.extend(_run_slices(runs, cursor, boundary))
                emit_markers(boundary)
                cursor = boundary
            continue
        if any(start < position < end for position in markers):
            raise WordEditUnsupported('修改跨越注释引用，无法可靠保留其指向；请在 Word 中核对该位置')
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
        emit_markers(end)
    emit_markers(len(old))
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
    rows = list(_content_nodes(table, {'tr'}))
    row = rows[_checked_index(location.row, len(rows), "表格行")]
    if type(location.column) is not int or location.column < 0:
        raise WordEditUnsupported("表格列定位无效")
    props = _children(row, 'trPr')
    before = _children(props[0], 'gridBefore') if props else []
    after = _children(props[0], 'gridAfter') if props else []
    try:
        column = _docx_grid_value(before[0].getAttributeNS(W, 'val'), minimum=0,
            maximum=IngestionLimits.max_office_columns) if before else 0
        trailing = _docx_grid_value(after[0].getAttributeNS(W, 'val'), minimum=0,
            maximum=IngestionLimits.max_office_columns) if after else 0
    except IngestionError as error:
        raise WordEditUnsupported(str(error)) from error
    grid = _children(table, 'tblGrid')
    declared = len(_children(grid[0], 'gridCol')) if grid else 0
    if (column and column > declared) or (not declared and (column or trailing)):
        raise WordEditUnsupported('暂不支持超出声明网格的表格偏移，无法可靠定位')
    for candidate in _content_nodes(row, {'tc'}):
        props = _children(candidate, "tcPr")
        spans = _children(props[0], "gridSpan") if props else []
        try:
            span = _docx_grid_value(spans[0].getAttributeNS(W, 'val'), minimum=1,
                maximum=IngestionLimits.max_office_columns) if spans else 1
        except IngestionError as exc:
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
    _check_control_edit(cell)
    if _all(cell, "tbl") or any(node.namespaceURI != W or node.localName not in {"tcPr", "p", "sdt"} | DOCX_CONTENT_WRAPPERS for node in _elements(cell)):
        raise WordEditUnsupported("单元格包含嵌套表格或暂不支持的结构")
    paragraphs = list(_content_nodes(cell, {'p'}))
    if not paragraphs:
        raise WordEditUnsupported("Word 单元格缺少段落")
    values = [_text(node) for node in paragraphs]
    if ''.join(values) != _text(cell):
        raise WordEditUnsupported('单元格含未映射的内容，无法安全原位修改')
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
            owners = [i for i, value in enumerate(values)
                      if max(start, offsets[i]) < min(end, offsets[i + 1])]
            if len(owners) > 1 and any(_all(paragraphs[i], 'hyperlink') for i in owners):
                raise WordEditUnsupported('单元格修改跨越含超链接的段落，无法确认新文字的链接归属')
            index = next((i for i in range(len(values)) if offsets[i + 1] > start), len(values) - 1)
            outputs[index].append(desired[new_start:new_end])
    for paragraph, parts in zip(paragraphs, outputs):
        _replace_text(paragraph, "".join(parts), tracked, change_ids)


def _bind_blocks(body, original, revised, note_nodes=None):
    originals = {b.block_id: b for b in original.blocks}
    finals = {b.block_id: b for b in revised.blocks}
    if len(originals) != len(original.blocks) or len(finals) != len(revised.blocks):
        raise WordEditUnsupported("文档包含重复文本块标识")
    if [b.block_id for b in original.blocks if b.block_id in finals] != [b.block_id for b in revised.blocks if b.block_id in originals]:
        raise WordEditUnsupported("原有文本块发生重排，无法按原定位输出")
    layout = original.metadata.get('docx_content_layout')
    if layout is not None and layout != DOCX_CONTENT_LAYOUT:
        raise WordEditUnsupported('Word 正文容器定位版本不受支持')
    if layout == DOCX_CONTENT_LAYOUT:
        source_nodes = list(_content_nodes(body, {'p', 'tbl'}))
    else:
        if any(_all(body, name) for name in DOCX_CONTENT_WRAPPERS | {'sdt'}):
            raise WordEditUnsupported('旧版识别未绑定 Word 内容容器，请重新解析为新项目')
        source_nodes = [n for n in _elements(body) if n.namespaceURI == W and n.localName in {"p", "tbl"}]
    nodes, text_nodes, note_source_nodes = {}, set(), {}
    split_sources = {block.attrs.get('split_from_block_id') for block in revised.blocks if block.attrs.get('split_from_block_id')}
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
        story = block.attrs.get('docx_story')
        sources = source_nodes
        if story is not None:
            if (original.metadata.get('docx_note_layout') != DOCX_NOTE_LAYOUT or not isinstance(story, dict)
                    or set(story) != {'kind', 'id', 'part'}
                    or any(not isinstance(value, str) for value in story.values())):
                raise WordEditUnsupported('Word 注释定位版本或字段无效')
            key = (story['kind'], story['id'], story['part'])
            if key not in (note_nodes or {}):
                raise WordEditUnsupported('Word 注释位置与原件不一致')
            if key not in note_source_nodes:
                note_source_nodes[key] = list(_content_nodes(note_nodes[key], {'p', 'tbl'}))
            sources = note_source_nodes[key]
        if final is not None and final.attrs.get('docx_story') != story:
            raise WordEditUnsupported('原有文本块的 Word 部件归属发生变化')
        if block.attrs.get('note_references') and block.block_id in split_sources:
            raise WordEditUnsupported('含注释引用的段落拆分需要在 Word 中核对引用位置')
        node = sources[_checked_index(location.paragraph, len(sources), "Word 正文或注释")]
        expected = "tbl" if block.kind in {"table", "table_cell"} else "p"
        if node.localName != expected:
            raise WordEditUnsupported("Word 原文定位类型不一致")
        if block.kind == "table_cell":
            container = originals.get(location.table_id)
            if container is None or container.kind != "table" or container.location is None or container.location.paragraph != location.paragraph or container.attrs.get('docx_story') != story or block.block_id not in container.children:
                raise WordEditUnsupported("表格单元格与容器绑定不一致")
            node = _cell(node, location)
            if final is None:
                raise WordEditUnsupported("不能删除单个表格单元格")
        if block.block_id in split_sources and _all(node, 'hyperlink'):
            raise WordEditUnsupported('含超链接的段落拆分需要在 Word 中核对链接归属')
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
    try:
        return _preserve_docx(source_bytes, original, revised, tracked=tracked)
    except IngestionError as exc:
        raise WordEditUnsupported(str(exc)) from exc


def _preserve_docx(source_bytes, original, revised, *, tracked=False):
    if hashlib.sha256(source_bytes).hexdigest() != original.source.sha256:
        raise WordEditUnsupported("Word 原件摘要与审查文档不一致")
    archive = _zip_safety(source_bytes, IngestionLimits())
    document_xml = _docx_xml(archive, "word/document.xml")
    if document_xml is None:
        raise WordEditUnsupported("Word 原件缺少正文")
    if original.quality.human_corrected or original.metadata.get("revisions_present"):
        raise WordEditUnsupported("原件含既有修订或识别全文经过人工替换，无法安全绑定原位 Word 修改")
    note_parts = _docx_note_parts(archive)
    note_roots = {data['part']: data['root'] for data in note_parts.values() if data['root'] is not None}
    if _docx_story_revisions(archive, document_xml=document_xml, note_parts=note_roots):
        raise WordEditUnsupported("原件含既有修订，无法安全绑定原位 Word 修改")
    for kind, data in note_parts.items():
        if data['notes'] and not data['relationship_bound']:
            raise WordEditUnsupported('Word 注释部件缺少明确关系，无法确认原位修改归属')
        if data['external'] or any(_docx_note_id(ref.get('{'+W+'}id')) not in data['notes']
                                  for ref in document_xml.findall('.//{'+W+'}'+kind+'Reference')):
            raise WordEditUnsupported('Word 原件存在无法读取的注释引用，请先核对原件')
    if _docx_content_issues(document_xml) or any(_docx_content_issues(root) for root in note_roots.values()):
        raise WordEditUnsupported('Word 原件含尚未完整识别的特殊内容，无法安全保格式导出')
    del document_xml
    doc = minidom.parseString(archive.read("word/document.xml"))
    if any(_all(doc, name) for name in REVISION_TAGS):
        raise WordEditUnsupported("原件含既有修订，无法安全绑定原位 Word 修改")
    settings = _docx_xml(archive, "word/settings.xml")
    if _docx_tracking_enabled(settings):
        raise WordEditUnsupported("原件已启用修订跟踪，无法安全绑定原位 Word 修改")
    body_nodes = _children(doc.documentElement, "body")
    if doc.documentElement.namespaceURI != W or doc.documentElement.localName != "document" or len(body_nodes) != 1:
        raise WordEditUnsupported("Word 正文结构无效")
    # Do not rebind a source prefix used by another namespace, even in an
    # unrelated part of document.xml. New nodes use a collision-free prefix.
    _declare_word_prefix(doc)
    body = body_nodes[0]
    documents = {'word/document.xml': doc}
    note_nodes = {}
    for kind, data in note_parts.items():
        if data['root'] is None:
            continue
        note_doc = minidom.parseString(archive.read(data['part']))
        _declare_word_prefix(note_doc)
        documents[data['part']] = note_doc
        for node in _children(note_doc.documentElement, kind):
            if node.getAttributeNS(W, 'type') in {'', 'normal'}:
                note_nodes[(kind, _docx_note_id(node.getAttributeNS(W, 'id')), data['part'])] = node
    originals, finals, nodes = _bind_blocks(body, original, revised, note_nodes)
    existing_ids = [int(node.getAttributeNS(W, "id")) for document in documents.values()
                    for node in document.getElementsByTagName("*") if node.getAttributeNS(W, "id").isdigit()]
    change_ids = itertools.count(max(existing_ids, default=0) + 1)
    changed, structural, retained_sections, linked_changes = 0, False, 0, 0
    changed_documents = set()
    for block in original.blocks:
        final = finals.get(block.block_id)
        if block.kind in PROTECTED_KINDS or (final is not None and final.text == block.text):
            continue
        node = nodes[block.block_id]
        linked_changes += bool(_all(node, 'hyperlink'))
        changed_documents.add(node.ownerDocument)
        changed += 1
        if final is None:
            if tracked:
                raise WordEditUnsupported("本轮含段落删除；可导出保格式净稿，原生修订副本仅支持段内替换")
            if _wrapped_ancestor(node):
                raise WordEditUnsupported('内容容器内的段落增删或拆分需要人工核对版式')
            note_marker = any(_all(node, name) for name in ('footnoteRef', 'endnoteRef'))
            if not note_marker:
                _editable(node)
            if _all(node, "sectPr") or note_marker:
                # A section break belongs to the paragraph mark, not the text.
                # Keep its empty paragraph so header/footer/page layout survive.
                _replace_text(node, "")
                retained_sections += bool(_all(node, 'sectPr'))
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
        story = block.attrs.get('docx_story')
        if story:
            if not isinstance(story, dict) or set(story) != {'kind', 'id', 'part'} or any(not isinstance(value, str) for value in story.values()):
                raise WordEditUnsupported('新增段落的 Word 注释归属无效')
            parent = note_nodes.get((story['kind'], story['id'], story['part']))
            if parent is None:
                raise WordEditUnsupported('不能通过正文修改新增未绑定的 Word 注释')
        else:
            parent = body
        # Table cells form one indivisible body node. A body paragraph between
        # a container and its cells cannot represent the reviewed order.
        following = [b for b in revised.blocks[index + 1:] if b.attrs.get('docx_story') == story]
        preceding = [b for b in revised.blocks[:index] if b.attrs.get('docx_story') == story]
        next_original = next((b for b in following if b.block_id in originals), None)
        previous_original = next((b for b in reversed(preceding) if b.block_id in originals), None)
        if any(_wrapped_ancestor(nodes.get(b.block_id)) for b in (next_original, previous_original) if b):
            raise WordEditUnsupported('内容容器附近的段落增删或拆分需要人工核对版式')
        previous_anchor = _body_anchor(nodes.get(previous_original.block_id), parent) if previous_original else None
        next_anchor = _body_anchor(nodes.get(next_original.block_id), parent) if next_original else None
        if next_original is not None and (next_original.kind == "table_cell" or (previous_anchor is not None and previous_anchor is next_anchor)):
            raise WordEditUnsupported("不能将正文段落插入表格单元格或同一图片段落内部")
        next_node = next((nodes[b.block_id] for b in following if b.block_id in nodes and nodes[b.block_id].parentNode == parent), None)
        if next_node is None:
            sections = _children(parent, "sectPr")
            next_node = sections[0] if sections else None
        neighbors = list(reversed(preceding)) + following
        node = _new_paragraph(parent.ownerDocument, block, neighbors, nodes)
        parent.insertBefore(node, next_node)
        changed_documents.add(parent.ownerDocument)
        nodes[block.block_id] = node
        changed += 1
        structural = True
    moved_sections = _move_split_sections(revised, originals, nodes)
    report = {"source_layout_preserved": True, "native_track_changes": tracked, "changed_elements": changed, "structural_edits": structural, "unchanged_parts_preserved": True, "retained_section_boundaries": retained_sections, "moved_section_boundaries": moved_sections,
              'changed_blocks_with_hyperlinks': linked_changes}
    if not changed:
        return source_bytes, report
    serialized = {part: document.toxml(encoding='utf-8') for part, document in documents.items() if document in changed_documents}
    for data in serialized.values():
        minidom.parseString(data)
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as output:
        for name in archive.namelist():
            output.writestr(name, serialized.get(name, archive.read(name)))
    return result.getvalue(), report
