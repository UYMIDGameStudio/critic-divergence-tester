"""Empty extraction and destructive revisions cannot masquerade as review results."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import document_review_studio as studio
from document_review_ingest import ingest_bytes
from document_review_model import DocumentBlock, ReviewContext, has_reviewable_text
from project_lifecycle import _files
from test.test_document_review_word import package, paragraph

OLD = '相关人员及时完成报名。'
NEW = '负责人周五前完成报名。'


def snapshot(project):
    return {name: file.read_bytes() for name, file in _files(project.root).items()}


class ReviewableTextGateTests(unittest.TestCase):
    def prepare(self, root):
        project = studio.DocumentReviewProject.create(root, filename='draft.txt', content=OLD.encode())
        project.confirm_extraction('confirm')
        project.confirm_context(ReviewContext('document', 'unknown', 'unknown', 'author', 'readers').to_dict())
        project.run_local_prechecks(['expression_ambiguity'])
        selected = project.findings()[0].finding_id
        for finding in project.findings():
            project.decide_finding(finding.finding_id, 'accept' if finding.finding_id == selected else 'reject', reason='Synthetic scope')
        action = project.prepare_revision_plan()['actions'][0]
        return project, action['action_id']

    def propose(self, project, action, operation, text):
        project.set_revision_action_operation(action, operation, reason='Explicit synthetic operation')
        hunk = project.propose_revision_hunk(action, text, rationale='Synthetic proposal')
        project.decide_revision_hunk(hunk['hunk_id'], 'approve', reason='Explicit synthetic approval')
        return hunk

    def test_whitespace_empty_grids_and_auxiliary_content_cannot_be_confirmed(self):
        cases = [('blank.txt', b' \n\t'), ('empty.csv', b',\n,\n'),
                 ('marks.txt', '\u200b\ufeff\u2060\u200e'.encode()),
                 ('image.docx', package('<w:p><w:r><w:drawing/></w:r></w:p>')),
                 ('header.docx', package('<w:p/>'))]
        for name, raw in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                project = studio.DocumentReviewProject.create(temp, filename=name, content=raw, encoding='utf-8' if name.endswith('.txt') else None)
                before = snapshot(project)
                for choice in ('confirm', 'continue_with_warning'):
                    with self.assertRaisesRegex(studio.ReviewStudioError, '没有可审查正文'):
                        project.confirm_extraction(choice)
                    self.assertEqual(snapshot(project), before)
                view = project.view()
                self.assertFalse(view['extraction']['reviewable'])
                self.assertTrue(view['extraction']['correction_available'])
                self.assertFalse(view['can_review'])
                self.assertEqual((project.root / 'source' / name).read_bytes(), raw)

    def test_no_word_count_or_language_threshold_is_invented(self):
        for text in ('A', '文', '體', 'ü', 'é', 'あ', 'я', 'æ', '123', '∑'):
            with self.subTest(text=text):
                self.assertTrue(has_reviewable_text(ingest_bytes('short.txt', text.encode(), encoding='utf-8')))
        document = ingest_bytes('table.csv', b'Value\n123\n')
        self.assertTrue(has_reviewable_text(document))
        for kind in ('header', 'footer', 'table', 'page_break', 'image_placeholder'):
            document.blocks = [DocumentBlock('aux', kind, '[Visible auxiliary label]')]
            self.assertFalse(has_reviewable_text(document))

    def test_format_only_correction_is_atomic_and_real_correction_recovers(self):
        with tempfile.TemporaryDirectory() as temp:
            project = studio.DocumentReviewProject.create(temp, filename='blank.txt', content=b' \n')
            before = snapshot(project)
            with self.assertRaisesRegex(studio.ReviewStudioError, '没有可审查正文'):
                project.confirm_extraction('correct', corrected_text='\u200b\u2060')
            self.assertEqual(snapshot(project), before)
            project.confirm_extraction('correct', corrected_text=OLD)
            project.confirm_context(ReviewContext('document', 'unknown', 'unknown', 'author', 'readers').to_dict())
            self.assertTrue(project.can_review()[0])
            self.assertTrue(project.run_local_prechecks(['expression_ambiguity']))
            self.assertEqual((project.root / 'source/blank.txt').read_bytes(), b' \n')
            self.assertEqual(project.integrity_errors(), [])

    def test_legacy_empty_review_cannot_create_new_requests_or_formal_exports(self):
        with tempfile.TemporaryDirectory() as temp:
            project = studio.DocumentReviewProject.create(temp, filename='blank.txt', content=b' \n')
            # Reproduce previously permitted confirmation, zero findings and
            # export, with valid receipts instead of tampering with history.
            with patch.object(studio, 'has_reviewable_text', return_value=True):
                project.confirm_extraction('confirm')
                project.confirm_context(ReviewContext('document', 'unknown', 'unknown', 'author', 'readers').to_dict())
                project.run_local_prechecks(['expression_ambiguity'])
                historical = project.export()
            view = project.view()
            self.assertFalse(view['can_review'])
            self.assertFalse(view['extraction']['correction_available'])
            self.assertEqual(view['workflow'][0]['status'], 'not_started')
            before = snapshot(project)
            for action in (lambda: project.confirm_context(project.context().to_dict()),
                           lambda: project.run_local_prechecks(['expression_ambiguity']),
                           lambda: project.prepare_ai_audits(['expression_ambiguity'], provider='fixture', model='not-invoked'),
                           project.prepare_revision_bridge, project.prepare_revision_plan,
                           lambda: project.prompt('expression_ambiguity'), project.export):
                with self.assertRaisesRegex(studio.ReviewStudioError, '没有可审查正文'):
                    action()
                self.assertEqual(snapshot(project), before)
            self.assertTrue(historical.is_dir())
            self.assertEqual(project.integrity_errors(), [])

    def test_followup_gate_checks_the_current_document(self):
        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.prepare(temp)
            current = deepcopy(project.document())
            current.blocks = []
            with patch('document_review_stores.ingestion.IngestionState.current_review_round', return_value=(Path(temp), {})), \
                    patch('document_review_stores.ingestion.IngestionState._review_document_record', return_value=(Path(temp), current)):
                self.assertFalse(project.can_review()[0])

    def test_empty_revision_is_atomic_and_can_be_replaced_with_a_new_approved_hunk(self):
        for operation, text in (('delete_block', ''), ('replace_block', '\u200b\u2060')):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp:
                project, action = self.prepare(temp)
                old_hunk = self.propose(project, action, operation, text)
                old_records = {name: data for name, data in snapshot(project).items()
                               if name.startswith(('revision-hunks/', 'hunk-decisions/'))}
                before = snapshot(project)
                with self.assertRaisesRegex(studio.ReviewStudioError, '修改后没有可审查正文'):
                    project.finalize_revision()
                self.assertEqual(snapshot(project), before)
                project.set_revision_action_operation(action, 'replace_block', reason='Keep actual body text')
                new_hunk = project.propose_revision_hunk(action, NEW, rationale='Recover an actual body')
                self.assertNotEqual(new_hunk['hunk_id'], old_hunk['hunk_id'])
                self.assertFalse(project.revision_workspace()['ready_to_finalize'])
                with self.assertRaises(studio.ReviewStudioError):
                    project.finalize_revision()
                project.decide_revision_hunk(new_hunk['hunk_id'], 'approve', reason='Reapprove the new proposal')
                project.finalize_revision()
                self.assertIn(NEW, (project.export() / '修改稿.md').read_text(encoding='utf-8'))
                for name, data in old_records.items():
                    self.assertEqual((project.root / name).read_bytes(), data)
                self.assertEqual(project.integrity_errors(), [])

    def test_historical_empty_revision_is_not_reexported_as_reviewed(self):
        with tempfile.TemporaryDirectory() as temp:
            project, action = self.prepare(temp)
            self.propose(project, action, 'delete_block', '')
            with patch.object(studio, 'has_reviewable_text', return_value=True):
                historical = project.finalize_revision()
            before = snapshot(project)
            with self.assertRaisesRegex(studio.ReviewStudioError, '修改稿没有可审查正文'):
                project.export()
            self.assertEqual(snapshot(project), before)
            self.assertTrue((historical / '修改稿.md').is_file())

