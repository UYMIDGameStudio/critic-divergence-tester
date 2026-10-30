"""Owned note fixtures verify source association and independently inspect Word XML."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

import document_review_ingest as ingest
from document_review_model import ReviewContext, has_reviewable_text, model_to_markdown
from document_review_studio import DocumentReviewProject, ReviewStudioError
from document_review_word import WordEditUnsupported, preserve_docx
from test.test_document_review_word import package, paragraph, rendered_text, W, R, NS

OLD = '相关人员及时完成报名。'
NEW = '负责人周五前完成报名。'
LANGUAGES = ['English', '简体中文', '繁體中文', 'Deutsch Straße', 'Français été', '日本語', 'Русский', 'Latīna']


def note_package(*, body=None, notes=None, footpart='word/footnotes.xml'):
    if body is None:
        body = '<w:p><w:r><w:t>Bounded claim.</w:t><w:footnoteReference w:id="7"/><w:endnoteReference w:id="7"/></w:r></w:p>'
    if notes is None:
        notes = '<w:footnote w:id="7"><w:p><w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteRef/><w:t>' + OLD + '</w:t></w:r></w:p><w:p/><w:p><w:r><w:t>' + ' '.join(LANGUAGES) + '</w:t></w:r></w:p></w:footnote>'
    footnotes = f'<w:footnotes xmlns:w="{W}"><w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>{notes}</w:footnotes>'
    endnotes = f'<w:endnotes xmlns:w="{W}"><w:endnote w:id="7"><w:p><w:r><w:endnoteRef/><w:t>This claim excludes other settings.</w:t></w:r></w:p></w:endnote></w:endnotes>'
    target = footpart.removeprefix('word/') if footpart.startswith('word/') else '/' + footpart
    relations = f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="f" Type="{R}/footnotes" Target="{target}"/><Relationship Id="e" Type="{R}/endnotes" Target="endnotes.xml"/></Relationships>'
    return package(body, extra={footpart: footnotes, 'word/endnotes.xml': endnotes, 'word/_rels/document.xml.rels': relations})


def part_xml(raw, name):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        return ET.fromstring(archive.read(name))


class DocxNoteTests(unittest.TestCase):
    def test_multilingual_notes_are_distinct_from_body_and_references_resolve(self):
        raw = note_package()
        document = ingest.ingest_bytes('paper.docx', raw)
        notes = [b for b in document.blocks if b.attrs.get('docx_story')]
        self.assertEqual([b.text for b in notes], [OLD, ' '.join(LANGUAGES), 'This claim excludes other settings.'])
        self.assertEqual([b.location.paragraph for b in notes], [0, 2, 0])
        self.assertEqual(notes[0].attrs['docx_story'], {'kind':'footnote', 'id':'7', 'part':'word/footnotes.xml'})
        self.assertEqual(document.blocks[0].attrs['note_references'], [
            {'kind':'footnote', 'id':'7', 'block_id':notes[0].block_id, 'occurrences':1},
            {'kind':'endnote', 'id':'7', 'block_id':notes[2].block_id, 'occurrences':1}])
        self.assertFalse(any(w.severity in {'critical', 'high'} for w in document.warnings))
        self.assertTrue(document.metadata['note_visibility']['footnote']['content_extracted'])
        self.assertNotIn('separator', document.plain_text)
        self.assertIn('### Footnote · source ID 7', model_to_markdown(document))
        self.assertIn('Footnote source ID 7; Endnote source ID 7', model_to_markdown(document))
        self.assertEqual(preserve_docx(raw, document, document)[0], raw)

    def test_clean_and_tracked_note_edits_keep_reference_marks_styles_and_other_parts(self):
        raw = note_package()
        document = ingest.ingest_bytes('paper.docx', raw)
        revised = deepcopy(document)
        next(b for b in revised.blocks if b.text == OLD).text = NEW
        for tracked in (False, True):
            with self.subTest(tracked=tracked):
                result, report = preserve_docx(raw, document, revised, tracked=tracked)
                note = part_xml(result, 'word/footnotes.xml').find('w:footnote[@w:id="7"]', NS)
                p = note.find('w:p', NS)
                self.assertEqual(rendered_text(p), NEW)
                self.assertEqual(rendered_text(p, accept=False), OLD if tracked else NEW)
                self.assertEqual(len(p.findall('.//w:footnoteRef', NS)), 1)
                marker_run = next(run for run in p.findall('w:r', NS) if run.find('w:footnoteRef', NS) is not None)
                self.assertEqual(marker_run.find('w:rPr/w:rStyle', NS).get('{'+W+'}val'), 'FootnoteReference')
                self.assertEqual(len(note.findall('w:p', NS)), 3)
                self.assertTrue(report['source_layout_preserved'])
                with zipfile.ZipFile(io.BytesIO(raw)) as before, zipfile.ZipFile(io.BytesIO(result)) as after:
                    for name in before.namelist():
                        if name != 'word/footnotes.xml':
                            self.assertEqual(before.read(name), after.read(name), name)

    def test_tab_stop_properties_are_not_text_and_cannot_shift_edits_in_body_or_notes(self):
        props = '<w:tabs><w:tab w:val="left" w:pos="360"/></w:tabs>'
        notes = '<w:footnote w:id="7"><w:p><w:pPr>'+props+'</w:pPr><w:r><w:footnoteRef/><w:t>Actual note.</w:t></w:r><w:r><w:tab/><w:t>After real tab.</w:t></w:r></w:p></w:footnote>'
        raw = note_package(body=paragraph('Actual body.', props), notes=notes)
        original = ingest.ingest_bytes('paper.docx', raw)
        self.assertEqual(original.blocks[0].text, 'Actual body.')
        self.assertEqual(next(b.text for b in original.blocks if b.attrs.get('docx_story', {}).get('kind') == 'footnote'), 'Actual note.\tAfter real tab.')
        revised = deepcopy(original)
        revised.blocks[0].text = 'Corrected body.'
        note = next(b for b in revised.blocks if b.attrs.get('docx_story', {}).get('kind') == 'footnote')
        note.text = 'Corrected note.\tAfter real tab.'
        def visible(node, accept):
            if node.tag in {'{'+W+'}pPr', '{'+W+'}rPr', '{'+W+'}'+('del' if accept else 'ins')}:
                return ''
            if node.tag in {'{'+W+'}t', '{'+W+'}delText'}:
                return node.text or ''
            if node.tag == '{'+W+'}tab':
                return '\t'
            return ''.join(visible(child, accept) for child in node)
        for tracked in (False, True):
            result, _ = preserve_docx(raw, original, revised, tracked=tracked)
            for part, query, old, new in [('word/document.xml','w:body/w:p','Actual body.','Corrected body.'),
                 ('word/footnotes.xml','w:footnote[@w:id="7"]/w:p','Actual note.\tAfter real tab.',note.text)]:
                node = part_xml(result, part).find(query, NS)
                self.assertEqual(visible(node, True), new)
                self.assertEqual(visible(node, False), old if tracked else new)
                self.assertEqual(len(node.findall('w:pPr/w:tabs/w:tab', NS)), 1)

    def test_body_replacement_retains_citation_marks_and_ambiguous_crossing_refuses_native_edit(self):
        raw = note_package()
        document = ingest.ingest_bytes('paper.docx', raw)
        revised = deepcopy(document)
        revised.blocks[0].text = 'A carefully bounded claim.'
        for tracked in (False, True):
            result, _ = preserve_docx(raw, document, revised, tracked=tracked)
            p = part_xml(result, 'word/document.xml').find('w:body/w:p', NS)
            self.assertEqual(rendered_text(p), revised.blocks[0].text)
            self.assertEqual(rendered_text(p, accept=False), document.blocks[0].text if tracked else revised.blocks[0].text)
            self.assertEqual(len(p.findall('.//w:footnoteReference', NS)), 1)
            self.assertEqual(len(p.findall('.//w:endnoteReference', NS)), 1)
        raw = note_package(body='<w:p><w:r><w:t>abcd</w:t><w:footnoteReference w:id="7"/><w:t>efgh</w:t></w:r></w:p>')
        document = ingest.ingest_bytes('paper.docx', raw)
        revised = deepcopy(document)
        revised.blocks[0].text = 'ZZZ'
        with self.assertRaises(WordEditUnsupported):
            preserve_docx(raw, document, revised)

    def test_missing_notes_warn_without_fabrication_and_notes_cannot_stand_in_for_a_body(self):
        raw = note_package(body='<w:p><w:r><w:footnoteReference w:id="99"/></w:r></w:p>')
        document = ingest.ingest_bytes('paper.docx', raw)
        self.assertFalse(has_reviewable_text(document))
        warning = next(w for w in document.warnings if w.code == 'footnotes-incomplete')
        self.assertEqual(warning.details['missing_ids'], ['99'])
        self.assertIsNone(document.blocks[0].attrs['note_references'][0]['block_id'])
        self.assertEqual(document.metadata['note_visibility']['footnote']['unreferenced_ids'], ['7'])
        with self.assertRaises(WordEditUnsupported):
            preserve_docx(raw, document, document)

    def test_custom_part_paths_and_note_tables_retain_their_source_story(self):
        notes = '<w:footnote w:id="7"><w:tbl><w:tr><w:tc>' + paragraph('Source table cell') + '</w:tc></w:tr></w:tbl></w:footnote>'
        raw = note_package(notes=notes, footpart='notes/author.xml')
        document = ingest.ingest_bytes('paper.docx', raw)
        cell = next(b for b in document.blocks if b.kind == 'table_cell')
        self.assertEqual(cell.attrs['docx_story']['part'], 'notes/author.xml')
        revised = deepcopy(document)
        next(b for b in revised.blocks if b.block_id == cell.block_id).text = 'Corrected table cell'
        result, _ = preserve_docx(raw, document, revised)
        self.assertIn('Corrected table cell', rendered_text(part_xml(result, 'notes/author.xml')))
        self.assertEqual(part_xml(result, 'word/document.xml').tag, '{'+W+'}document')

    def test_duplicate_invalid_ids_and_note_resource_limits_fail_without_partial_ir(self):
        for notes in ('<w:footnote w:id="7"/><w:footnote w:id="+07"/>', '<w:footnote w:id="invalid"/>'):
            with self.subTest(notes=notes), self.assertRaises(ingest.IngestionError):
                ingest.ingest_bytes('paper.docx', note_package(notes=notes))
        with self.assertRaises(ingest.IngestionError):
            ingest.ingest_bytes('paper.docx', note_package(), limits=ingest.IngestionLimits(max_text_blocks=3))

    def test_repeated_references_use_a_bounded_entry_link_instead_of_expanding_the_note_each_time(self):
        notes = '<w:footnote w:id="7">' + ''.join(paragraph(f'Note paragraph {n}') for n in range(100)) + '</w:footnote>'
        body = '<w:p><w:r><w:t>Bounded claim.</w:t>' + '<w:footnoteReference w:id="7"/>'*100 + '</w:r></w:p>'
        document = ingest.ingest_bytes('paper.docx', note_package(body=body, notes=notes))
        references = document.blocks[0].attrs['note_references']
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]['occurrences'], 100)
        self.assertIsInstance(references[0]['block_id'], str)
        self.assertNotIn('block_ids', references[0])
        self.assertEqual(sum(b.attrs.get('docx_story', {}).get('kind') == 'footnote' for b in document.blocks), 100)

    def test_unsafe_external_or_unbound_note_relationships_cannot_masquerade_as_native_content(self):
        raw = note_package()
        for target in ('../../outside.xml', '%2e%2e/%2e%2e/outside.xml', 'https://example.invalid/notes', 'footnotes.xml?x=1'):
            with zipfile.ZipFile(io.BytesIO(raw)) as before:
                relations = before.read('word/_rels/document.xml.rels').decode().replace('Target="footnotes.xml"', f'Target="{target}"')
                output = io.BytesIO()
                with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as after:
                    for name in before.namelist():
                        after.writestr(name, relations if name == 'word/_rels/document.xml.rels' else before.read(name))
            with self.subTest(target=target), self.assertRaises(ingest.IngestionError):
                ingest.ingest_bytes('paper.docx', output.getvalue())
        for external in (True, False):
            with zipfile.ZipFile(io.BytesIO(raw)) as before:
                relations = before.read('word/_rels/document.xml.rels').decode()
                if external:
                    relations = relations.replace('Target="footnotes.xml"', 'Target="https://example.invalid/notes" TargetMode="External"')
                else:
                    relations = relations.replace(f'Type="{R}/footnotes"', f'Type="{R}/unrelated"')
                output = io.BytesIO()
                with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as after:
                    for name in before.namelist():
                        after.writestr(name, relations if name == 'word/_rels/document.xml.rels' else before.read(name))
            document = ingest.ingest_bytes('paper.docx', output.getvalue())
            code = 'footnotes-incomplete' if external else 'docx-note-relationship-inferred'
            self.assertIn(code, [w.code for w in document.warnings])
            with self.assertRaises(WordEditUnsupported):
                preserve_docx(output.getvalue(), document, document)

    def test_special_content_in_a_custom_note_part_reaches_visibility_and_cannot_bypass_native_checks(self):
        notes = '<w:footnote w:id="7">' + paragraph('Visible qualification.') + '<w:altChunk/></w:footnote>'
        raw = note_package(notes=notes, footpart='notes/author.xml')
        document = ingest.ingest_bytes('paper.docx', raw)
        warning = next(w for w in document.warnings if w.code == 'docx-special-content')
        self.assertIn('notes/author.xml', warning.details['parts'])
        with self.assertRaises(WordEditUnsupported):
            preserve_docx(raw, document, document)

    def test_real_project_note_edit_splits_in_its_note_and_saved_protocol_sees_qualifications(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = note_package()
            project = DocumentReviewProject.create(temp, filename='paper.docx', content=raw)
            project.confirm_extraction('confirm')
            project.confirm_context(ReviewContext('document', 'unknown', 'unknown', 'author', 'readers').to_dict())
            request = project.prepare_ai_audits(['expression_ambiguity'], provider='fixture', model='not-invoked')[0]
            contract = json.loads(request['prompt'].split('```json\n')[1].split('\n```')[0])
            self.assertTrue(contract['source_visibility']['note_visibility']['footnote']['content_extracted'])
            self.assertIn('Read relevant qualifications', contract['document_location_contract']['note_stories'])
            self.assertIn('This claim excludes other settings.', request['prompt'])
            project.run_local_prechecks(['expression_ambiguity'])
            target = next(b for b in project.document().blocks if b.text == OLD)
            for finding in project.findings():
                project.decide_finding(finding.finding_id, 'accept' if finding.location.block_id == target.block_id else 'reject', reason='Synthetic note scope')
            action = project.prepare_revision_plan()['actions'][0]
            project.set_revision_action_operation(action['action_id'], 'replace_block', reason='Synthetic note replacement')
            hunk = project.propose_revision_hunk(action['action_id'], NEW+'\n\nAdditional note qualification.', rationale='Synthetic note edit')
            project.decide_revision_hunk(hunk['hunk_id'], 'approve', reason='Synthetic approval')
            project.finalize_revision()
            exported = project.export()
            result = (exported/'修改稿.docx').read_bytes()
            note = part_xml(result, 'word/footnotes.xml').find('w:footnote[@w:id="7"]', NS)
            paragraphs = [rendered_text(p) for p in note.findall('w:p', NS)]
            self.assertEqual([text for text in paragraphs if text], [NEW, 'Additional note qualification.', ' '.join(LANGUAGES)])
            self.assertEqual(paragraphs.count(''), 1)
            self.assertEqual(rendered_text(part_xml(result, 'word/document.xml').find('w:body', NS)), 'Bounded claim.')
            self.assertEqual((project.root/'source/paper.docx').read_bytes(), raw)
            self.assertEqual(project.integrity_errors(), [])


if __name__ == '__main__':
    unittest.main()
