"""Owned packages expose destinations separately from visible document text."""
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

import document_review_ingest as ingest
from document_review_model import ReviewContext, document_location_contract
from document_review_studio import DocumentReviewProject
from document_review_word import WordEditUnsupported, preserve_docx
from test.test_document_review_word import package, paragraph, W, R

P = 'http://schemas.openxmlformats.org/package/2006/relationships'


def relationships(items):
    return f'<Relationships xmlns="{P}">' + ''.join(
        f'<Relationship Id="{identifier}" Type="{R}/{kind}" Target="{target}" TargetMode="{mode}"/>'
        for identifier, kind, target, mode in items) + '</Relationships>'


def link(identifier='same', label='Source', attrs=''):
    rid = f'r:id="{identifier}"' if identifier is not None else ''
    return f'<w:hyperlink {rid} {attrs}><w:r><w:t>{label}</w:t></w:r></w:hyperlink>'


def linked_package():
    body = '<w:p>' + link(label='English 简体中文 繁體中文 Deutsch Français 日本語 Русский Latīna') + '<w:r><w:footnoteReference w:id="7"/></w:r></w:p>'
    body += '<w:tbl><w:tr><w:tc><w:p>' + link(None, 'Appendix', 'w:anchor="appendix"') + '</w:p></w:tc></w:tr></w:tbl>'
    body += paragraph('Editable body.')
    return package(body, extra={
        'word/_rels/document.xml.rels': relationships([
            ('same', 'hyperlink', 'https://example.org/paper?x=1&amp;y=2', 'External'),
            ('notes', 'footnotes', 'footnotes.xml', 'Internal')]),
        'word/footnotes.xml': f'<w:footnotes xmlns:w="{W}" xmlns:r="{R}"><w:footnote w:id="7"><w:p>{link(label="See here")}</w:p></w:footnote></w:footnotes>',
        'word/_rels/footnotes.xml.rels': relationships([('same', 'hyperlink', 'https://example.org/note', 'External')]),
        'word/header1.xml': f'<w:hdr xmlns:w="{W}" xmlns:r="{R}"><w:p>{link(label="Header source")}</w:p></w:hdr>',
        'word/_rels/header1.xml.rels': relationships([('same', 'hyperlink', '../appendix.xml', 'Internal')]),
    })


class DocxHyperlinkTests(unittest.TestCase):
    def test_part_scopes_table_bookmark_and_text_identity(self):
        document = ingest.ingest_bytes('paper.docx', linked_package())
        body, cell, header, note = [b for b in document.blocks if b.attrs.get('hyperlink_references')]
        self.assertEqual(body.attrs['hyperlink_references'][0]['target'], 'https://example.org/paper?x=1&y=2')
        self.assertEqual(note.attrs['hyperlink_references'][0]['target'], 'https://example.org/note')
        self.assertEqual(header.attrs['hyperlink_references'][0]['target'], '../appendix.xml')
        self.assertEqual(header.attrs['hyperlink_references'][0]['target_mode'], 'Internal')
        self.assertEqual(cell.attrs['hyperlink_references'][0]['anchor'], 'appendix')
        self.assertIsNone(cell.attrs['hyperlink_references'][0]['target'])
        self.assertEqual(cell.text, 'Appendix')
        self.assertEqual(note.text, 'See here')
        self.assertEqual(body.attrs['hyperlink_references'][0]['label'], body.text)
        self.assertNotIn('https://', document.plain_text)
        self.assertEqual(document.metadata['docx_hyperlink_visibility']['reference_count'], 4)
        self.assertFalse(document.metadata['docx_hyperlink_visibility']['fetched'])

    def test_unresolved_and_duplicate_relations_do_not_guess_from_label_or_anchor(self):
        cases = [([], 'missing-relationship'),
                 ([('same', 'image', 'image.png', 'Internal')], 'wrong-relationship-type'),
                 ([('same', 'hyperlink', 'one', 'External'), ('same', 'hyperlink', 'two', 'External')], 'ambiguous-relationship'),
                 ([('same', 'hyperlink', 'one', 'Unknown')], 'invalid-relationship')]
        for relations, reason in cases:
            with self.subTest(reason=reason):
                document = ingest.ingest_bytes('paper.docx', package('<w:p>' + link(label='https://label.example', attrs='w:anchor="fallback"') + '</w:p>', extra={
                    'word/_rels/document.xml.rels': relationships(relations)}))
                reference = document.blocks[0].attrs['hyperlink_references'][0]
                self.assertEqual(reference['status'], 'unresolved')
                self.assertEqual(reference['reason'], reason)
                self.assertEqual(reference['anchor'], 'fallback')
                self.assertTrue(any(w.code == 'docx-hyperlinks-unresolved' for w in document.warnings))

    def test_dangerous_targets_are_retained_as_untrusted_data(self):
        for target in ('javascript:alert(1)', 'file:///C:/private/paper.docx', '//server/share/paper', 'https://example.org/#part'):
            with self.subTest(target=target):
                raw = package('<w:p>' + link(attrs='w:anchor="separate-fragment" w:docLocation="location"') + '</w:p>', extra={
                    'word/_rels/document.xml.rels': relationships([('same', 'hyperlink', target, 'External')])})
                reference = ingest.ingest_bytes('p.docx', raw).blocks[0].attrs['hyperlink_references'][0]
                self.assertEqual(reference['target'], target)
                self.assertEqual(reference['anchor'], 'separate-fragment')
                self.assertEqual(reference['doc_location'], 'location')

    def test_fields_and_excluded_branches_have_explicit_limits(self):
        body = '<w:p><w:fldSimple w:instr="HYPERLINK &quot;https://field.example&quot;"><w:r><w:t>Cached result</w:t></w:r></w:fldSimple></w:p>'
        body += '<w:p><w:r><w:fldChar w:fldCharType="begin"/><w:instrText> HYPERLINK "https://complex.example" </w:instrText><w:fldChar w:fldCharType="separate"/><w:t>Other result</w:t><w:fldChar w:fldCharType="end"/></w:r></w:p>'
        body += '<w:p><w:rPr>' + link() + '</w:rPr><mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"><mc:Fallback>' + link() + '</mc:Fallback></mc:AlternateContent></w:p>'
        document = ingest.ingest_bytes('p.docx', package(body))
        self.assertEqual([b.text for b in document.blocks if b.kind == 'paragraph'], ['Cached result', 'Other result'])
        self.assertFalse(document.metadata['hyperlinks_present'])
        self.assertEqual(document.metadata['docx_hyperlink_visibility']['field_code_blocks'], 2)
        self.assertTrue(any(w.code == 'docx-fields-not-evaluated' for w in document.warnings))

    def test_long_label_is_explicit_preview_and_final_ir_budget_still_applies(self):
        text = '字' * 700
        raw = package('<w:p>' + link(None, text, 'w:anchor="bookmark"') + '</w:p>')
        document = ingest.ingest_bytes('p.docx', raw)
        reference = document.blocks[0].attrs['hyperlink_references'][0]
        self.assertEqual(document.blocks[0].text, text)
        self.assertEqual(len(reference['label']), 512)
        self.assertTrue(reference['label_truncated'])
        with self.assertRaises(ingest.IngestionError):
            ingest.ingest_bytes('p.docx', raw, limits=ingest.IngestionLimits(max_ir_json_bytes=500))

    def test_new_relationship_parts_keep_xml_security_and_source_preservation(self):
        for relations in ('<wrong/>', '<!DOCTYPE x [<!ENTITY injected "bad">]><Relationships/>', '<broken'):
            with self.subTest(relations=relations), tempfile.TemporaryDirectory() as temporary:
                raw = package(paragraph('Body'), extra={
                    'word/header1.xml': f'<w:hdr xmlns:w="{W}" xmlns:r="{R}"><w:p>{link()}</w:p></w:hdr>',
                    'word/_rels/header1.xml.rels': relations})
                project = DocumentReviewProject.create(Path(temporary), filename='p.docx', content=raw)
                self.assertIsNone(project.document())
                self.assertEqual((project.root / 'source/p.docx').read_bytes(), raw)
                self.assertFalse(project.integrity_errors())

    def test_original_relations_survive_native_edit_and_linked_text_is_protected(self):
        raw = linked_package()
        document = ingest.ingest_bytes('p.docx', raw)
        revised = deepcopy(document)
        next(b for b in revised.blocks if b.text == 'Editable body.').text = 'Changed body.'
        result, _ = preserve_docx(raw, document, revised)
        with zipfile.ZipFile(io.BytesIO(raw)) as before, zipfile.ZipFile(io.BytesIO(result)) as after:
            for name in before.namelist():
                if name != 'word/document.xml':
                    self.assertEqual(before.read(name), after.read(name), name)
        revised.blocks[0].text = 'Changed linked text.'
        with self.assertRaises(WordEditUnsupported):
            preserve_docx(raw, document, revised)

    def test_academic_request_keeps_destinations_and_historical_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = DocumentReviewProject.create(Path(temporary), filename='p.docx', content=linked_package())
            project.confirm_extraction('confirm')
            project.confirm_context(ReviewContext(document_type='paper', review_profile='academic', jurisdiction='unknown',
                effective_date='unknown', publisher_type='author', audience='researchers', discipline='humanities', research_type='theoretical').to_dict())
            request = project.prepare_ai_audits(['academic_argument'], provider='test', model='not-invoked')[0]
            self.assertIn('https://example.org/note', request['prompt'])
            self.assertIn('not independent verification', request['prompt'])
            document = project.document()
            document.metadata['revision_of'] = 'old'
            self.assertIn('historical', document_location_contract(document)['hyperlinks'])


if __name__ == '__main__':
    unittest.main()
