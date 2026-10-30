"""Word wrappers must preserve review content, source order and edit boundaries."""
from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import document_review_ingest as ingest
from document_review_model import DocumentBlock, ReviewContext
from document_review_studio import DocumentReviewProject, ReviewStudioError
from document_review_word import WordEditUnsupported, preserve_docx
from project_lifecycle import create_backup, restore_backup
from test.test_document_review_word import package, paragraph, xml, rendered_text, NS

OLD = '相关人员及时完成报名。'
NEW = '负责人周五前完成报名。'


def sdt(content, props=''):
    return '<w:sdt><w:sdtPr>' + props + '</w:sdtPr><w:sdtContent>' + content + '</w:sdtContent></w:sdt>'


def table(content):
    return '<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>' + content + '</w:tbl>'


class DocxContentContainerTests(unittest.TestCase):
    def test_nested_controls_and_custom_xml_keep_paragraph_order_and_headings(self):
        languages = ['English', '简体中文', '繁體中文', 'Deutsch Straße', 'Français été', '日本語', 'Русский', 'Latīna']
        body = paragraph('Before') + sdt('<w:customXml w:element="article">' + sdt(
            paragraph('Heading', '<w:pStyle w:val="Heading2"/>') + '<w:p/>' + ''.join(paragraph(t) for t in languages)
        ) + '</w:customXml>') + paragraph('After')
        document = ingest.ingest_bytes('controls.docx', package(body))
        blocks = [b for b in document.blocks if b.kind in {'paragraph', 'heading'}]
        self.assertEqual([b.text for b in blocks], ['Before', 'Heading', *languages, 'After'])
        self.assertEqual([b.location.paragraph for b in blocks], [0, 1, *range(3, 12)])
        self.assertEqual(blocks[1].level, 2)
        self.assertNotIn('docx-special-content', [w.code for w in document.warnings])
        self.assertEqual(document.metadata['docx_content_layout'], ingest.DOCX_CONTENT_LAYOUT)

    def test_native_text_edits_inside_unbound_controls_retain_container_and_other_parts(self):
        for wrapper in (lambda value: sdt(value, '<w:alias w:val="author"/>'),
                        lambda value: '<w:customXml w:element="argument">' + value + '</w:customXml>'):
            raw = package(paragraph('Same') + wrapper(paragraph('Same', run_props='<w:b/>')) + paragraph('Same'))
            original = ingest.ingest_bytes('controls.docx', raw)
            revised = deepcopy(original)
            revised.blocks[1].text = 'Edited inside'
            for tracked in (False, True):
                with self.subTest(tracked=tracked):
                    output, report = preserve_docx(raw, original, revised, tracked=tracked)
                    root = xml(output)
                    paragraphs = root.findall('.//w:body//w:p', NS)
                    self.assertEqual([rendered_text(p) for p in paragraphs], ['Same', 'Edited inside', 'Same'])
                    self.assertEqual(rendered_text(paragraphs[1], accept=False), 'Same' if tracked else 'Edited inside')
                    self.assertTrue(report['source_layout_preserved'])
                    with zipfile.ZipFile(io.BytesIO(raw)) as before, zipfile.ZipFile(io.BytesIO(output)) as after:
                        self.assertEqual(before.namelist(), after.namelist())
                        for name in before.namelist():
                            if name != 'word/document.xml':
                                self.assertEqual(before.read(name), after.read(name))

    def test_wrapped_rows_cells_and_cell_paragraphs_survive_approved_native_export(self):
        body = sdt(table(sdt('<w:tr><w:tc>' + paragraph('Neighbor') + '</w:tc>' +
            sdt('<w:tc>' + sdt(paragraph(OLD)) + '</w:tc>') + '</w:tr>')))
        raw = package(body)
        with tempfile.TemporaryDirectory() as temp:
            project = DocumentReviewProject.create(temp, filename='controls.docx', content=raw)
            project.confirm_extraction('confirm')
            project.confirm_context(ReviewContext('document', 'unknown', 'unknown', 'author', 'readers').to_dict())
            project.run_local_prechecks(['expression_ambiguity'])
            target = next(b for b in project.document().blocks if b.kind == 'table_cell' and b.text == OLD)
            self.assertEqual((target.location.paragraph, target.location.row, target.location.column), (0, 0, 1))
            selected = next(f.finding_id for f in project.findings() if f.location.block_id == target.block_id)
            for finding in project.findings():
                project.decide_finding(finding.finding_id, 'accept' if finding.finding_id == selected else 'reject', reason='Synthetic scope')
            action = project.prepare_revision_plan()['actions'][0]
            project.set_revision_action_operation(action['action_id'], 'replace_table_cell', reason='Only selected cell')
            hunk = project.propose_revision_hunk(action['action_id'], NEW, rationale='Specific actor and deadline')
            project.decide_revision_hunk(hunk['hunk_id'], 'approve', reason='Synthetic approval')
            project.finalize_revision()
            exported = project.export()
            native = (exported / '修改稿.docx').read_bytes()
            self.assertEqual([rendered_text(p) for p in xml(native).findall('.//w:body//w:p', NS)], ['Neighbor', NEW])
            self.assertEqual(len(xml(native).findall('.//w:sdt', NS)), 4)
            self.assertEqual(next(b.attrs['rows'] for b in ingest.ingest_bytes('native.docx', native).blocks if b.kind == 'table'), [['Neighbor', NEW]])
            self.assertEqual((project.root / 'source/controls.docx').read_bytes(), raw)
            self.assertEqual(project.integrity_errors(), [])

    def test_data_bound_locked_or_extended_controls_are_not_silently_rewritten(self):
        for props in ('<w:dataBinding w:xpath="/author"/>', '<w:lock w:val="contentLocked"/>',
                      '<w:lock w:val="sdtContentLocked"/>', '<w:lock/>',
                      '<w:date w:fullDate="2020-01-01T00:00:00Z"/>', '<w:dropDownList/>',
                      '<w:docPartObj/>', '<w:showingPlcHdr/>',
                      '<x:repeatingSection xmlns:x="http://schemas.microsoft.com/office/word/2012/wordml"/>'):
            raw = package(sdt(paragraph('Controlled'), props) + paragraph('Outside'))
            original = ingest.ingest_bytes('controls.docx', raw)
            revised = deepcopy(original)
            revised.blocks[0].text = 'Modified'
            with self.subTest(props=props), self.assertRaises(WordEditUnsupported):
                preserve_docx(raw, original, revised)
            # A lock on this control does not prohibit a separate body edit.
            revised = deepcopy(original)
            revised.blocks[1].text = 'Outside edited'
            output, _ = preserve_docx(raw, original, revised)
            self.assertEqual(rendered_text(xml(output).find('.//w:sdt', NS)), 'Controlled')
        for props in ('<w:lock w:val="unlocked"/>', '<w:lock w:val="sdtLocked"/>', '<w:showingPlcHdr w:val="false"/>'):
            raw = package(sdt(paragraph('Controlled'), props))
            original = ingest.ingest_bytes('controls.docx', raw)
            revised = deepcopy(original)
            revised.blocks[0].text = 'Text remains editable'
            output, _ = preserve_docx(raw, original, revised)
            self.assertEqual(rendered_text(xml(output).find('.//w:sdt', NS)), 'Text remains editable')

    def test_specialized_control_formal_export_is_explicitly_normalized(self):
        raw = package(sdt(paragraph(OLD), '<w:date w:fullDate="2020-01-01T00:00:00Z"/>'))
        with tempfile.TemporaryDirectory() as temp:
            project = DocumentReviewProject.create(temp, filename='date.docx', content=raw)
            project.confirm_extraction('confirm')
            project.confirm_context(ReviewContext('document', 'unknown', 'unknown', 'author', 'readers').to_dict())
            project.run_local_prechecks(['expression_ambiguity'])
            selected = project.findings()[0].finding_id
            for finding in project.findings():
                project.decide_finding(finding.finding_id, 'accept' if finding.finding_id == selected else 'reject', reason='Synthetic scope')
            for action in project.prepare_revision_plan()['actions']:
                project.set_revision_action_operation(action['action_id'], 'replace_block', reason='Clarify text')
                hunk = project.propose_revision_hunk(action['action_id'], NEW, rationale='Specific actor and deadline')
                project.decide_revision_hunk(hunk['hunk_id'], 'approve', reason='Synthetic approval')
            project.finalize_revision()
            exported = project.export()
            self.assertFalse((exported / '修改稿.docx').exists())
            normalized = (exported / '规范化修改稿.docx').read_bytes()
            self.assertEqual(rendered_text(xml(normalized).find('w:body/w:p', NS)), NEW)
            self.assertEqual(xml(normalized).findall('.//w:date', NS), [])
            report = json.loads((exported / 'track-changes-capability.json').read_text(encoding='utf-8'))
            self.assertTrue(report['normalization_fallback'])
            self.assertTrue(report['requires_layout_review'])
            self.assertEqual((project.root / 'source/date.docx').read_bytes(), raw)

    def test_structural_edits_do_not_move_text_outside_a_content_control(self):
        raw = package(sdt(paragraph('Inside')) + paragraph('Outside'))
        original = ingest.ingest_bytes('controls.docx', raw)
        source = original.blocks[0]
        for blocks in ([*original.blocks[1:]],
                       [source, DocumentBlock('new', 'paragraph', 'Split part', attrs={'split_from_block_id': source.block_id}), *original.blocks[1:]],
                       [DocumentBlock('new', 'paragraph', 'Before'), *original.blocks]):
            with self.subTest(blocks=len(blocks)), self.assertRaises(WordEditUnsupported):
                preserve_docx(raw, original, replace(original, blocks=blocks))

    def test_special_content_is_reported_without_combining_alternative_branches(self):
        alternatives = '<mc:AlternateContent xmlns:mc="' + ingest.MC_NS[1:-1] + '"><mc:Choice Requires="x" xmlns:x="urn:synthetic"><w:r><w:t>CHOICE</w:t></w:r></mc:Choice><mc:Fallback><w:r><w:t>FALLBACK</w:t></w:r></mc:Fallback></mc:AlternateContent>'
        math = '<m:oMath xmlns:m="' + ingest.MATH_NS[1:-1] + '"><m:r><m:t>E=mc²</m:t></m:r></m:oMath>'
        variants = [paragraph('Visible') + '<w:altChunk r:id="part"/>',
                    '<w:p><w:r><w:t>Visible</w:t></w:r>' + alternatives + '</w:p>',
                    '<w:p><w:r><w:t>Visible</w:t></w:r>' + math + '</w:p>',
                    paragraph('Visible') + '<w:unknown>' + paragraph('Unmapped') + '</w:unknown>']
        for body in variants:
            with self.subTest(body=body[:70]):
                raw = package(body, extra={'word/import.html': '<p>EMBEDDED-UNEXPANDED</p>'})
                document = ingest.ingest_bytes('special.docx', raw)
                warning = next(w for w in document.warnings if w.code == 'docx-special-content')
                self.assertEqual(warning.severity, 'high')
                self.assertEqual([b.text for b in document.blocks if b.kind == 'paragraph'], ['Visible'])
                self.assertTrue(warning.details['parts']['word/document.xml'])
                document.metadata.clear()  # Native protection uses actual source XML.
                with self.assertRaises(WordEditUnsupported):
                    preserve_docx(raw, document, document)

    def test_control_extraction_still_obeys_text_and_expanded_table_limits(self):
        raw = package(sdt(paragraph('One') + sdt(paragraph('Two'))))
        with self.assertRaises(ingest.IngestionError):
            ingest.ingest_bytes('controls.docx', raw, limits=replace(ingest.IngestionLimits(), max_text_blocks=1))
        raw = package(table(sdt('<w:tr><w:tc>' + paragraph('One') + '</w:tc></w:tr>') * 2))
        with self.assertRaises(ingest.IngestionError):
            ingest.ingest_bytes('controls.docx', raw, limits=replace(ingest.IngestionLimits(), max_office_rows=1))
        self.assertEqual(next(b.attrs['rows'] for b in ingest.ingest_bytes('controls.docx', raw).blocks if b.kind == 'table'), [['One', ''], ['One', '']])

    def test_special_content_requires_warning_confirmation_and_reaches_academic_protocol_and_backup(self):
        raw = package(sdt(paragraph('Visible contribution.')) + '<w:altChunk r:id="part"/>', extra={'word/import.html': '<p>EMBEDDED-UNEXPANDED</p>'})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = DocumentReviewProject.create(root / 'library', filename='special.docx', content=raw)
            with self.assertRaises(ReviewStudioError):
                project.confirm_extraction('confirm')
            project.confirm_extraction('continue_with_warning')
            project.confirm_context(ReviewContext('article', 'unknown', 'unknown', 'author', 'researchers', review_profile='academic').to_dict())
            request = project.prepare_ai_audits(['academic_argument'], provider='synthetic', model='not-invoked')[0]
            contract = json.loads(request['prompt'].split('```json\n', 1)[1].split('\n```', 1)[0])
            visibility = contract['source_visibility']
            self.assertIn('docx-special-content', [row['code'] for row in visibility['extraction_warnings']])
            self.assertEqual(visibility['docx_content_visibility']['coverage_measurement'], 'not-measured')
            self.assertNotIn('EMBEDDED-UNEXPANDED', request['prompt'])
            backup = create_backup(project.root, root / 'backup.zip')
            restored = DocumentReviewProject(restore_backup(backup, root / 'restored'))
            self.assertEqual(restored.document().to_dict(), project.document().to_dict())
            self.assertEqual((restored.root / 'source/special.docx').read_bytes(), raw)

    def test_legacy_unbound_content_cannot_be_misrepresented_by_native_export(self):
        raw = package(sdt(paragraph('Unseen')) + paragraph('Seen'))
        original = ingest.ingest_bytes('controls.docx', raw)
        original.metadata.pop('docx_content_layout')
        original.blocks = original.blocks[1:]
        original.blocks[0].location = replace(original.blocks[0].location, paragraph=0)
        with self.assertRaisesRegex(WordEditUnsupported, '重新解析'):
            preserve_docx(raw, original, original)
        raw = package(paragraph('Plain'))
        original = ingest.ingest_bytes('plain.docx', raw)
        original.metadata.pop('docx_content_layout')
        self.assertEqual(preserve_docx(raw, original, original)[0], raw)
