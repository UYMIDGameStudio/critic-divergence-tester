"""DOCX omissions and unresolved revisions must reach the review boundary."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import document_review_ingest as ingest
from document_review_model import ReviewContext
from document_review_studio import DocumentReviewProject, ReviewStudioError
from document_review_word import WordEditUnsupported, preserve_docx
from project_lifecycle import _files

W = ingest.W_NS[1:-1]


def package(body='<w:p><w:r><w:t>Bounded claim.</w:t></w:r></w:p>', *, parts=None, tracking=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr('word/document.xml', f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
        if tracking is not None:
            attribute = '' if tracking == '' else f' w:val="{tracking}"'
            archive.writestr('word/settings.xml', f'<w:settings xmlns:w="{W}"><w:trackRevisions{attribute}/></w:settings>')
        for name, data in (parts or {}).items():
            archive.writestr(name, data)
    return output.getvalue()


class DocxReviewVisibilityTests(unittest.TestCase):
    def test_moves_ranges_and_formatting_revisions_are_not_clean_drafts(self):
        cases = {
            'move': '<w:p><w:moveFrom w:id="1"><w:r><w:t>Before</w:t></w:r></w:moveFrom><w:moveTo w:id="2"><w:r><w:t>After</w:t></w:r></w:moveTo></w:p>',
            'range': '<w:moveFromRangeStart w:id="1"/><w:p><w:r><w:t>Before</w:t></w:r></w:p><w:moveFromRangeEnd w:id="1"/>',
            'paragraph': '<w:p><w:pPr><w:pPrChange w:id="1"><w:pPr/></w:pPrChange></w:pPr><w:r><w:t>Text</w:t></w:r></w:p>',
            'run': '<w:p><w:r><w:rPr><w:rPrChange w:id="1"><w:rPr/></w:rPrChange></w:rPr><w:t>Text</w:t></w:r></w:p>',
            'table': '<w:tbl><w:tblPr><w:tblPrChange w:id="1"><w:tblPr/></w:tblPrChange></w:tblPr><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>',
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                raw = package(body)
                document = ingest.ingest_bytes('paper.docx', raw)
                warning = next(w for w in document.warnings if w.code == 'unaccepted-revisions')
                self.assertEqual(warning.severity, 'critical')
                self.assertTrue(document.metadata['revisions_present'])
                self.assertIn('word/document.xml', warning.details['parts'])
                # Legacy metadata must not bypass inspection of the actual ZIP.
                document.metadata['revisions_present'] = False
                with self.assertRaises(WordEditUnsupported):
                    preserve_docx(raw, document, document)

    def test_revisions_in_headers_notes_and_comments_are_recorded_and_native_export_refuses_them(self):
        for part, root, child in (('header1', 'hdr', ''), ('footer1', 'ftr', ''),
                                 ('footnotes', 'footnotes', 'footnote'), ('endnotes', 'endnotes', 'endnote'),
                                 ('comments', 'comments', 'comment')):
            text = '<w:p><w:r><w:rPr><w:rPrChange w:id="1"><w:rPr/></w:rPrChange></w:rPr><w:t>Auxiliary text</w:t></w:r></w:p>'
            if child:
                text = f'<w:{child} w:id="1">{text}</w:{child}>'
            raw = package(parts={f'word/{part}.xml': f'<w:{root} xmlns:w="{W}">{text}</w:{root}>'})
            with self.subTest(part=part):
                document = ingest.ingest_bytes('paper.docx', raw)
                self.assertEqual(document.metadata['revision_parts'][f'word/{part}.xml'], ['rPrChange'])
                document.metadata['revisions_present'] = False
                with self.assertRaises(WordEditUnsupported):
                    preserve_docx(raw, document, document)

    def test_disabled_change_tracking_is_allowed_and_real_markup_still_wins(self):
        for flag in ('false', '0', 'off'):
            with self.subTest(flag=flag):
                raw = package(tracking=flag)
                document = ingest.ingest_bytes('paper.docx', raw)
                self.assertFalse(document.metadata['revisions_present'])
                self.assertNotIn('unaccepted-revisions', [w.code for w in document.warnings])
                revised = deepcopy(document)
                revised.blocks[0].text = 'A clarified bounded claim.'
                output, _ = preserve_docx(raw, document, revised)
                self.assertIn('A clarified bounded claim.', ingest.ingest_bytes('output.docx', output).plain_text)
                changed = package('<w:p><w:ins w:id="1"><w:r><w:t>Inserted</w:t></w:r></w:ins></w:p>', tracking=flag)
                self.assertTrue(ingest.ingest_bytes('revised.docx', changed).metadata['revisions_present'])
        for flag in ('', 'true', '1', 'on'):
            with self.subTest(flag=flag):
                self.assertTrue(ingest.ingest_bytes('tracked.docx', package(tracking=flag)).metadata['revisions_present'])

    def test_omitted_notes_and_missing_parts_require_explicit_confirmation_and_reach_the_prompt(self):
        for kind in ('footnote', 'endnote'):
            for part_present in (False, True):
                with self.subTest(kind=kind, part_present=part_present), tempfile.TemporaryDirectory() as temp:
                    notes = {f'word/{kind}s.xml': f'<w:{kind}s xmlns:w="{W}"><w:{kind} w:id="1"><w:p><w:r><w:t>OMITTED-NOTE-CONTENT</w:t></w:r></w:p></w:{kind}></w:{kind}s>'} if part_present else {}
                    raw = package(f'<w:p><w:r><w:t>Bounded claim.</w:t><w:{kind}Reference w:id="1"/></w:r></w:p>', parts=notes)
                    project = DocumentReviewProject.create(temp, filename='paper.docx', content=raw)
                    warning = next(w for w in project.document().warnings if w.code == kind + 's-present')
                    self.assertEqual(warning.severity, 'high')
                    self.assertEqual(warning.details, {'part_present': part_present, 'reference_count': 1,
                        'note_count': int(part_present), 'content_extracted': False})
                    with self.assertRaises(ReviewStudioError):
                        project.confirm_extraction('confirm')
                    project.confirm_extraction('continue_with_warning')
                    project.confirm_context(ReviewContext('academic article', 'unknown', 'unknown', 'author', 'researchers',
                        review_profile='academic', discipline='humanities', research_type='theoretical').to_dict())
                    request = project.prepare_ai_audits(['academic_argument'], provider='fixture', model='not-invoked')[0]
                    contract = json.loads(request['prompt'].split('```json\n', 1)[1].split('\n```', 1)[0])
                    self.assertIn(warning.to_dict(), contract['source_visibility']['extraction_warnings'])
                    self.assertNotIn('OMITTED-NOTE-CONTENT', request['prompt'])
                    self.assertEqual((project.root / 'source/paper.docx').read_bytes(), raw)
                    self.assertEqual(project.integrity_errors(), [])

    def test_separator_only_parts_do_not_invent_missing_prose(self):
        raw = package(parts={'word/footnotes.xml': f'<w:footnotes xmlns:w="{W}"><w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote></w:footnotes>'})
        document = ingest.ingest_bytes('paper.docx', raw)
        self.assertNotIn('footnotes-present', [w.code for w in document.warnings])
        self.assertEqual(document.metadata['note_visibility']['footnote']['note_count'], 0)

    def test_auxiliary_xml_damage_and_encoded_dtd_are_not_ignored(self):
        samples = [b'<broken',
            ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE endnotes [<!ENTITY payload "not allowed">]>'
             f'<w:endnotes xmlns:w="{W}">&payload;</w:endnotes>').encode('utf-16')]
        for part in ('word/endnotes.xml', 'word/header1.xml'):
            for data in samples:
                with self.subTest(part=part, data=data[:12]), self.assertRaises(ingest.IngestionError):
                    ingest.ingest_bytes('paper.docx', package(parts={part: data}))

    def test_docx_block_limit_rejects_before_partial_ir_and_preserves_raw_project(self):
        raw = package('<w:p><w:r><w:t>First</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Second</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
        document = ingest.ingest_bytes('paper.docx', raw, limits=ingest.IngestionLimits(max_text_blocks=3))
        self.assertEqual(len(document.blocks), 3)
        limited = ingest.IngestionLimits(max_text_blocks=2)
        with self.assertRaises(ingest.IngestionError):
            ingest.ingest_bytes('paper.docx', raw, limits=limited)
        with tempfile.TemporaryDirectory() as temp:
            project = DocumentReviewProject.create(temp, filename='paper.docx', content=raw, limits=limited)
            self.assertEqual(project.state()['extraction_state'], 'blocked')
            self.assertFalse(project.document_path.exists())
            self.assertEqual((project.root / 'source/paper.docx').read_bytes(), raw)

    def test_reparse_preserves_old_recognition_and_adds_current_revision_warning(self):
        raw = package('<w:p><w:pPr><w:pPrChange w:id="1"><w:pPr/></w:pPrChange></w:pPr><w:r><w:t>Text</w:t></w:r></w:p>')
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(ingest, '_docx_story_revisions', return_value={}), patch.object(
                    ingest, 'DOCX_PARSER_VERSION', 'document-review-studio-v1'):
                old = DocumentReviewProject.create(temp, filename='paper.docx', content=raw)
            before = {name: path.read_bytes() for name, path in _files(old.root).items()}
            fresh = DocumentReviewProject.create(temp, filename='paper.docx', content=raw, new_project=True)
            self.assertFalse(old.document().metadata['revisions_present'])
            self.assertTrue(fresh.document().metadata['revisions_present'])
            self.assertEqual(old.document().parser_version, 'document-review-studio-v1')
            self.assertEqual(fresh.document().parser_version, ingest.DOCX_PARSER_VERSION)
            self.assertEqual([(block.block_id, block.text) for block in old.document().blocks],
                             [(block.block_id, block.text) for block in fresh.document().blocks])
            self.assertNotEqual(old.root, fresh.root)
            self.assertEqual(before, {name: path.read_bytes() for name, path in _files(old.root).items()})
