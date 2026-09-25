"""Portable source names must fail before any project or device is opened."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from argument_app import _safe_upload_name, create_uploaded_project
from argument_workbench import WorkbenchError
from document_review_ingest import IngestionError, safe_upload_name
from document_review_studio import DocumentReviewProject
from project_lifecycle import _child, create_backup, restore_backup


UNSAFE = (
    'report:stream.txt', 'bad?.txt', 'bad*.txt', 'bad<name.txt', 'bad>name.txt',
    'bad|name.txt', 'bad"name.txt', 'bad\\name.txt', 'bad/name.txt',
    'NUL.txt', 'con.md', 'COM1.txt', 'Lpt9.txt', 'COM¹.txt', 'LPT².txt',
    'COM³.more.txt', 'NUL .txt', 'CONIN$.txt', 'CONOUT$.md',
    'bad\x7fname.txt', 'bad\ud800name.txt', 'draft.txt ', 'draft.txt.',
)
SAFE = ('English.txt', '简体中文.txt', '繁體中文.txt', 'Grüße.txt', 'Français.txt',
        '日本語.txt', 'Русский.txt', 'Latīna.txt', '原文 🧪.txt', 'COM10.txt',
        'null.txt', 'NUL-result.txt', 'notes.con.txt', 'plan².txt', '.hidden.txt')


class DocumentFileNameTests(unittest.TestCase):
    def test_both_upload_entrypoints_reject_unsafe_names_without_touching_disk(self):
        for name in UNSAFE:
            with self.subTest(name=repr(name)):
                with self.assertRaises(IngestionError):
                    safe_upload_name(name)
                with self.assertRaises(WorkbenchError):
                    _safe_upload_name(name)

    def test_project_creation_rejects_before_creating_storage(self):
        for name in UNSAFE:
            with self.subTest(name=repr(name)), patch.object(Path, 'mkdir', side_effect=AssertionError('storage touched')):
                with self.assertRaises(IngestionError):
                    DocumentReviewProject.create('unused', filename=name, content=b'Original.')
                with self.assertRaises(WorkbenchError):
                    create_uploaded_project('unused', filename=name, content=b'Original.')

    def test_multilingual_names_are_returned_verbatim(self):
        for name in SAFE:
            with self.subTest(name=name):
                self.assertEqual(safe_upload_name(name), name)
                self.assertEqual(_safe_upload_name(name), name)

    def test_backup_paths_reject_device_names_in_every_component(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            for name in ('COM¹.txt', 'LPT².txt', 'COM³.more.txt', 'NUL .txt', 'CONIN$.txt', 'CONOUT$.txt'):
                for relative in (name, 'source/' + name, name + '/safe.txt'):
                    with self.subTest(relative=relative), self.assertRaises(ValueError):
                        _child(root, relative)

    def test_unicode_source_names_survive_backup_restore_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            name = '論文 · Grüße · 日本語 · Русский · Latīna 🧪.txt'
            raw = '中文與 multilingual evidence.'.encode('utf-8')
            project = DocumentReviewProject.create(root / 'library', filename=name, content=raw)
            archive = create_backup(project.root, root / 'backup.zip')
            restored = DocumentReviewProject(restore_backup(archive, root / 'restored'))
            self.assertEqual(restored.document().source.original_name, name)
            self.assertEqual((restored.root / 'source' / name).read_bytes(), raw)
            self.assertEqual(restored.integrity_errors(), [])
