"""Word export regressions using package parts, runs, sections, and real tables."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from document_review_ingest import ingest_bytes
from document_review_model import DocumentBlock
from document_review_word import W, WordEditUnsupported, preserve_docx
from document_review_studio import DocumentReviewProject

NS = {"w": W}
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def paragraph(text, props="", run_props=""):
    return f'<w:p>{"<w:pPr>" + props + "</w:pPr>" if props else ""}<w:r>{"<w:rPr>" + run_props + "</w:rPr>" if run_props else ""}<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def package(body, *, prefix="w", extra=None):
    document = f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>'
    if prefix != "w":
        document = document.replace("xmlns:w=", f"xmlns:{prefix}=").replace("w:", prefix + ":")
    entries = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/><Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/></Types>',
        "_rels/.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{R}/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": document,
        "word/_rels/document.xml.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rHeader" Type="{R}/header" Target="header1.xml"/><Relationship Id="rStyles" Type="{R}/styles" Target="styles.xml"/><Relationship Id="rNum" Type="{R}/numbering" Target="numbering.xml"/></Relationships>',
        "word/header1.xml": f'<w:hdr xmlns:w="{W}">{paragraph("Confidential")}</w:hdr>',
        "word/styles.xml": f'<w:styles xmlns:w="{W}"><w:style w:type="paragraph" w:styleId="BodyText"><w:name w:val="Body Text"/><w:pPr><w:spacing w:after="120"/></w:pPr></w:style></w:styles>',
        "word/numbering.xml": f'<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum><w:num w:numId="7"><w:abstractNumId w:val="0"/></w:num></w:numbering>',
    }
    entries.update(extra or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return output.getvalue()


def xml(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return ET.fromstring(archive.read("word/document.xml"))


def body_paragraphs(data):
    return xml(data).findall("w:body/w:p", NS)


def rendered_text(node, *, accept=True):
    if node.tag == f"{{{W}}}{'del' if accept else 'ins'}":
        return ""
    if node.tag in {f"{{{W}}}t", f"{{{W}}}delText"}:
        return node.text or ""
    if node.tag == f"{{{W}}}tab":
        return "\t"
    if node.tag in {f"{{{W}}}br", f"{{{W}}}cr"}:
        return "\n"
    return "".join(rendered_text(child, accept=accept) for child in node)


def formatted_characters(node, *, accept=True):
    if node.tag == f"{{{W}}}{'del' if accept else 'ins'}":
        return []
    if node.tag == f"{{{W}}}r":
        props = node.find("w:rPr", NS)
        signature = ET.tostring(props) if props is not None else b""
        return [(c, signature) for c in rendered_text(node, accept=accept)]
    return [entry for child in node for entry in formatted_characters(child, accept=accept)]


class WordPreservationTests(unittest.TestCase):
    def change(self, raw, old, new, *, tracked=False):
        original = ingest_bytes("source.docx", raw)
        target = next(block for block in original.blocks if block.text == old)
        revised = replace(original, blocks=[replace(block, text=new) if block.block_id == target.block_id else block for block in original.blocks])
        return preserve_docx(raw, original, revised, tracked=tracked)

    def finalize_project(self, root, raw, after):
        project = DocumentReviewProject.create(root, filename="source.docx", content=raw)
        project.confirm_extraction("confirm")
        project.confirm_context({"document_type": "活动策划案", "jurisdiction": "unknown", "effective_date": "unknown", "publisher_type": "作者", "audience": "读者", "publication_status": "internal-draft", **{"involves_" + key: False for key in ("contract", "fees", "intellectual_property", "minors", "personal_information", "sponsorship")}})
        project.run_local_prechecks(["expression_ambiguity"])
        target = next(f for f in project.findings() if f.location.block_id == project.document().blocks[0].block_id)
        for finding in project.findings():
            project.decide_finding(finding.finding_id, "accept" if finding.finding_id == target.finding_id else "reject", reason="scope")
        action = project.prepare_revision_plan()["actions"][0]
        project.set_revision_action_operation(action["action_id"], "replace_block", reason="clarify")
        hunk = project.propose_revision_hunk(action["action_id"], after, rationale="主体、截止时间和流程")
        project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="checked")
        return project, project.finalize_revision()

    def test_deleting_section_text_keeps_page_size_header_reference_and_break(self):
        section = '<w:sectPr><w:headerReference w:type="default" r:id="rHeader"/><w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/><w:type w:val="nextPage"/></w:sectPr>'
        raw = package(paragraph("first") + paragraph("remove", section) + paragraph("last"))
        original = ingest_bytes("source.docx", raw)
        revised = replace(original, blocks=[b for b in original.blocks if b.text != "remove"])
        output, report = preserve_docx(raw, original, revised)
        paragraphs = body_paragraphs(output)
        self.assertEqual([rendered_text(p) for p in paragraphs], ["first", "", "last"])
        self.assertEqual(ET.tostring(body_paragraphs(raw)[1].find("w:pPr/w:sectPr", NS)), ET.tostring(paragraphs[1].find("w:pPr/w:sectPr", NS)))
        self.assertEqual(report["retained_section_boundaries"], 1)
        with zipfile.ZipFile(io.BytesIO(raw)) as before, zipfile.ZipFile(io.BytesIO(output)) as after:
            for name in before.namelist():
                if name != "word/document.xml":
                    self.assertEqual(before.read(name), after.read(name), name)

    def test_tracked_changes_accept_to_clean_and_reject_to_original_with_mixed_styles(self):
        raw = package('<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Alpha </w:t></w:r><w:r><w:rPr><w:i/></w:rPr><w:t>BETA</w:t></w:r><w:r><w:rPr><w:u w:val="single"/></w:rPr><w:t> omega</w:t></w:r></w:p>')
        for replacement in ["Alpha GAMMA omega", "Almond OMEGA", "Alpha BETA plus omega"]:
            with self.subTest(replacement=replacement):
                clean, _ = self.change(raw, "Alpha BETA omega", replacement)
                tracked, _ = self.change(raw, "Alpha BETA omega", replacement, tracked=True)
                paragraph_xml = body_paragraphs(tracked)[0]
                self.assertEqual(rendered_text(paragraph_xml), replacement)
                self.assertEqual(formatted_characters(paragraph_xml), formatted_characters(body_paragraphs(clean)[0]))
                self.assertEqual(formatted_characters(paragraph_xml, accept=False), formatted_characters(body_paragraphs(raw)[0]))
                self.assertTrue(paragraph_xml.findall("w:ins", NS))

    def test_whitespace_tabs_and_soft_breaks_survive_clean_and_tracked_output(self):
        raw = package('<w:p><w:r><w:t xml:space="preserve">  Alpha</w:t><w:tab/><w:t>BETA</w:t><w:br/><w:t xml:space="preserve">omega  </w:t></w:r></w:p>')
        for tracked in [False, True]:
            output, _ = self.change(raw, "Alpha\tBETA\nomega", "Alpha\tGAMMA\nomega\nnew", tracked=tracked)
            paragraph_xml = body_paragraphs(output)[0]
            self.assertEqual(rendered_text(paragraph_xml), "  Alpha\tGAMMA\nomega\nnew  ")
            self.assertTrue(paragraph_xml.findall(".//w:tab", NS))
            self.assertTrue(paragraph_xml.findall(".//w:br", NS))
            if tracked:
                self.assertEqual(rendered_text(paragraph_xml, accept=False), "  Alpha\tBETA\nomega  ")

    def test_multi_paragraph_merged_cell_retains_paragraphs_and_other_cells(self):
        raw = package('<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid><w:gridCol w:w="1000"/><w:gridCol w:w="1000"/><w:gridCol w:w="1000"/></w:tblGrid><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>' + paragraph("First.", '<w:jc w:val="center"/>', '<w:b/>') + paragraph("Second.", '<w:spacing w:before="80"/>', '<w:i/>') + '</w:tc><w:tc>' + paragraph("Neighbor") + '</w:tc></w:tr></w:tbl>')
        for tracked in [False, True]:
            output, _ = self.change(raw, "First.Second.", "First.Updated.", tracked=tracked)
            cells = xml(output).findall("w:body/w:tbl/w:tr/w:tc", NS)
            paragraphs = cells[0].findall("w:p", NS)
            self.assertEqual([rendered_text(p) for p in paragraphs], ["First.", "Updated."])
            self.assertEqual(paragraphs[0].find("w:pPr/w:jc", NS).get(f"{{{W}}}val"), "center")
            self.assertEqual(paragraphs[1].find("w:pPr/w:spacing", NS).get(f"{{{W}}}before"), "80")
            self.assertEqual(cells[0].find("w:tcPr/w:gridSpan", NS).get(f"{{{W}}}val"), "2")
            self.assertEqual(rendered_text(cells[1]), "Neighbor")
            if tracked:
                self.assertEqual(rendered_text(cells[0], accept=False), "First.Second.")

    def test_change_in_later_cell_paragraph_preserves_untouched_hyperlink(self):
        raw = package('<w:tbl><w:tr><w:tc><w:p><w:hyperlink w:anchor="target"><w:r><w:t>Link.</w:t></w:r></w:hyperlink></w:p>' + paragraph("Edit.") + '</w:tc></w:tr></w:tbl>')
        output, _ = self.change(raw, "Link.Edit.", "Link.Done.")
        self.assertEqual(rendered_text(xml(output).find("w:body/w:tbl/w:tr/w:tc", NS)), "Link.Done.")
        self.assertIsNotNone(xml(output).find(".//w:hyperlink", NS))

    def test_generated_paragraphs_keep_order_before_table_and_after_table(self):
        raw = package(paragraph("before", '<w:pStyle w:val="BodyText"/>') + '<w:tbl><w:tr><w:tc>' + paragraph("cell") + '</w:tc></w:tr></w:tbl>' + paragraph("after"))
        original = ingest_bytes("source.docx", raw)
        first, table, cell, last, header = original.blocks
        generated = [DocumentBlock(f"new-{i}", "paragraph", str(i)) for i in range(3)]
        revised = replace(original, blocks=[first, generated[0], generated[1], table, cell, generated[2], last, header])
        output, _ = preserve_docx(raw, original, revised)
        body = xml(output).find("w:body", NS)
        self.assertEqual([rendered_text(n) for n in body if n.tag != f"{{{W}}}sectPr"], ["before", "0", "1", "cell", "2", "after"])
        self.assertEqual(body_paragraphs(output)[1].find("w:pPr/w:pStyle", NS).get(f"{{{W}}}val"), "BodyText")

    def test_generated_list_item_inherits_numbering_not_section_boundary(self):
        props = '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="7"/></w:numPr><w:sectPr><w:type w:val="continuous"/></w:sectPr>'
        raw = package(paragraph("One", props))
        original = ingest_bytes("source.docx", raw)
        generated = DocumentBlock("new-list", "list_item", "Two")
        revised = replace(original, blocks=[original.blocks[0], generated] + original.blocks[1:])
        output, _ = preserve_docx(raw, original, revised)
        inserted = body_paragraphs(output)[1]
        self.assertEqual(inserted.find("w:pPr/w:numPr/w:numId", NS).get(f"{{{W}}}val"), "7")
        self.assertIsNone(inserted.find("w:pPr/w:sectPr", NS))

    def test_section_moves_to_last_split_paragraph_but_not_insert_after(self):
        section = '<w:sectPr><w:headerReference w:type="default" r:id="rHeader"/><w:type w:val="nextPage"/></w:sectPr>'
        raw = package(paragraph("section end", section, '<w:b/>') + paragraph("next section"))
        original = ingest_bytes("source.docx", raw)
        anchor = original.blocks[0]
        for splitting in [False, True]:
            with self.subTest(splitting=splitting):
                attrs = {"split_from_block_id": anchor.block_id} if splitting else {}
                added = [DocumentBlock(f"new-{i}", "paragraph", f"part {i}", attrs=attrs) for i in range(2)]
                revised = replace(original, blocks=[anchor, *added, *original.blocks[1:]])
                output, report = preserve_docx(raw, original, revised)
                paragraphs = body_paragraphs(output)
                self.assertEqual([i for i, p in enumerate(paragraphs) if p.find("w:pPr/w:sectPr", NS) is not None], [2 if splitting else 0])
                self.assertEqual(report["moved_section_boundaries"], int(splitting))
                self.assertIsNotNone(paragraphs[1].find("w:r/w:rPr/w:b", NS))

    def test_approved_split_revision_exports_section_in_correct_position(self):
        section = '<w:sectPr><w:headerReference w:type="default" r:id="rHeader"/><w:type w:val="nextPage"/></w:sectPr>'
        raw = package(paragraph("相关人员及时完成报名。", section) + paragraph("下一节。"))
        with tempfile.TemporaryDirectory() as temp:
            project, revision = self.finalize_project(Path(temp), raw, "项目负责人于周五完成报名。\n\n具体流程见附件。")
            model = json.loads((revision / "document.json").read_text(encoding="utf-8"))
            self.assertEqual(model["blocks"][1]["attrs"]["split_from_block_id"], model["blocks"][0]["block_id"])
            output = project.export()
            paragraphs = body_paragraphs((output / "修改稿.docx").read_bytes())
            self.assertEqual([rendered_text(p) for p in paragraphs], ["项目负责人于周五完成报名。", "具体流程见附件。", "下一节。"])
            self.assertIsNone(paragraphs[0].find("w:pPr/w:sectPr", NS))
            self.assertIsNotNone(paragraphs[1].find("w:pPr/w:sectPr", NS))
            self.assertEqual(project.integrity_errors(), [])

    def test_unsupported_word_edit_exports_clearly_named_normalized_copy(self):
        raw = package('<w:p><w:r><w:t>相关人员及时完成报名。</w:t><w:sym w:font="Symbol" w:char="F020"/></w:r></w:p>')
        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.finalize_project(Path(temp), raw, "项目负责人于周五完成报名。")
            output = project.export()
            self.assertTrue((output / "规范化修改稿.docx").is_file())
            self.assertFalse((output / "修改稿.docx").exists())
            self.assertFalse((output / "Word修订标记.docx").exists())
            capability = json.loads((output / "track-changes-capability.json").read_text(encoding="utf-8"))
            self.assertFalse(capability["source_layout_preserved"])
            self.assertTrue(capability["requires_layout_review"])
            self.assertEqual(capability["output_name"], "规范化修改稿.docx")
            self.assertIn("符号", capability["fallback_reason"])
            self.assertIn("人工核对版式", (output / "Word导出说明.md").read_text(encoding="utf-8"))
            entry = next(item for row in project.export_summary() for item in row["files"] if item["name"] == "规范化修改稿.docx")
            self.assertIn("需人工核对版式", entry["label"])
            self.assertEqual((project.root / project.document().source.relative_path).read_bytes(), raw)
            self.assertEqual(project.integrity_errors(), [])

    def test_unknown_inline_payload_cannot_disappear_during_edit(self):
        for inline in ['<w:object/>', '<w:sym w:font="Symbol" w:char="F020"/>', '<w:footnoteReference w:id="4"/>', '<w:br w:type="page"/>']:
            with self.subTest(inline=inline):
                raw = package('<w:p><w:r><w:t>Text</w:t>' + inline + '</w:r></w:p>')
                with self.assertRaises(WordEditUnsupported):
                    self.change(raw, "Text", "Replacement")

    def test_nested_table_untouched_is_preserved_but_ambiguous_edit_is_rejected(self):
        raw = package('<w:tbl><w:tr><w:tc>' + paragraph("outer") + '<w:tbl><w:tr><w:tc>' + paragraph("inner") + '</w:tc></w:tr></w:tbl><w:p/></w:tc></w:tr></w:tbl>' + paragraph("editable"))
        output, _ = self.change(raw, "editable", "changed")
        self.assertEqual(len(xml(output).findall(".//w:tbl", NS)), 2)
        with self.assertRaises(WordEditUnsupported):
            self.change(raw, "outerinner", "modified")

    def test_wrong_source_and_invalid_or_duplicate_locations_are_rejected(self):
        raw = package(paragraph("same") + paragraph("same"))
        original = ingest_bytes("source.docx", raw)
        with self.assertRaisesRegex(WordEditUnsupported, "摘要"):
            preserve_docx(package(paragraph("other")), original, original)
        for location in [None, replace(original.blocks[0].location, paragraph=-1), replace(original.blocks[0].location, paragraph=True)]:
            corrupted = replace(original, blocks=[replace(original.blocks[0], location=location)] + original.blocks[1:])
            with self.subTest(location=location), self.assertRaises(WordEditUnsupported):
                preserve_docx(raw, corrupted, corrupted)
        corrupted = replace(original, blocks=[original.blocks[0], replace(original.blocks[1], location=original.blocks[0].location)] + original.blocks[2:])
        with self.assertRaisesRegex(WordEditUnsupported, "重复绑定"):
            preserve_docx(raw, corrupted, corrupted)

    def test_duplicate_ids_reorder_and_generated_inside_table_are_rejected(self):
        raw = package(paragraph("first") + '<w:tbl><w:tr><w:tc>' + paragraph("cell") + '</w:tc></w:tr></w:tbl>' + paragraph("last"))
        original = ingest_bytes("source.docx", raw)
        first, table, cell, last, header = original.blocks
        for blocks in [[first, first, table, cell, last, header], [last, first, table, cell, header], [first, table, DocumentBlock("new", "paragraph", "inside"), cell, last, header]]:
            with self.subTest(blocks=[b.block_id for b in blocks]), self.assertRaises(WordEditUnsupported):
                preserve_docx(raw, original, replace(original, blocks=blocks))

    def test_new_paragraph_cannot_split_image_placeholder_from_same_paragraph_text(self):
        raw = package('<w:p><w:r><w:drawing/></w:r><w:r><w:t>caption</w:t></w:r></w:p>')
        original = ingest_bytes("source.docx", raw)
        placeholder, caption, header = original.blocks
        revised = replace(original, blocks=[placeholder, DocumentBlock("new", "paragraph", "between"), caption, header])
        with self.assertRaisesRegex(WordEditUnsupported, "同一图片段落"):
            preserve_docx(raw, original, revised)

    def test_nonstandard_namespace_prefix_and_unique_revision_ids(self):
        raw = package(paragraph("A one B two C") + paragraph("D three E four F"), prefix="x")
        original = ingest_bytes("source.docx", raw)
        revised = replace(original, blocks=[replace(b, text=b.text.replace("one", "1").replace("two", "2").replace("three", "3").replace("four", "4")) for b in original.blocks])
        output, _ = preserve_docx(raw, original, revised, tracked=True)
        root = xml(output)
        changes = root.findall(".//w:ins", NS) + root.findall(".//w:del", NS)
        identifiers = [node.get(f"{{{W}}}id") for node in changes]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertEqual([rendered_text(p) for p in body_paragraphs(output)], ["A 1 B 2 C", "D 3 E 4 F"])
        self.assertEqual([rendered_text(p, accept=False) for p in body_paragraphs(output)], ["A one B two C", "D three E four F"])

    def test_generated_nodes_never_rebind_an_unrelated_namespace(self):
        document = f'<x:document xmlns:x="{W}" xmlns:w="urn:custom"><x:body><w:custom w:flag="preserve"/><x:p><x:r><x:t>Old</x:t></x:r></x:p><x:sectPr/></x:body></x:document>'
        raw = package("", extra={"word/document.xml": document})
        output, _ = self.change(raw, "Old", "New", tracked=True)
        custom = xml(output).find("w:body/{urn:custom}custom", NS)
        self.assertIsNotNone(custom)
        self.assertEqual(custom.get("{urn:custom}flag"), "preserve")
        self.assertEqual(rendered_text(body_paragraphs(output)[0]), "New")

    def test_actual_property_revision_cannot_bypass_metadata_check(self):
        raw = package(paragraph("text", '<w:pPrChange w:id="9" w:author="Other"><w:pPr/></w:pPrChange>'))
        original = ingest_bytes("source.docx", raw)
        original.metadata["revisions_present"] = False
        with self.assertRaisesRegex(WordEditUnsupported, "既有修订"):
            preserve_docx(raw, original, original)

    def test_invalid_xml_characters_fail_with_capability_error(self):
        raw = package(paragraph("text"))
        for value in ["invalid\x00", "invalid\ud800", "invalid\ufffe", "invalid\rreturn"]:
            with self.subTest(value=repr(value)), self.assertRaises(WordEditUnsupported):
                self.change(raw, "text", value)

    def test_no_edits_return_exact_original_package(self):
        raw = package(paragraph("untouched"))
        original = ingest_bytes("source.docx", raw)
        output, report = preserve_docx(raw, original, original)
        self.assertEqual(output, raw)
        self.assertEqual(report["changed_elements"], 0)

    def test_large_repetitive_paragraph_has_exact_accept_reject_roundtrip(self):
        old = "prefix " + "ab" * 10000 + " suffix"
        new = "prefix " + "cd" * 10000 + " suffix"
        raw = package(paragraph(old))
        output, _ = self.change(raw, old, new, tracked=True)
        node = body_paragraphs(output)[0]
        self.assertEqual(rendered_text(node), new)
        self.assertEqual(rendered_text(node, accept=False), old)


if __name__ == "__main__":
    unittest.main()
