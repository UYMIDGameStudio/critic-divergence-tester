"""Verify the split stores retain their identity and facade injection hooks."""
import importlib
import tempfile
import unittest
from unittest.mock import patch

import document_review_studio as studio


class ComponentBindingTests(unittest.TestCase):
    def test_store_metadata_survives_project_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            project = studio.DocumentReviewProject.create(directory, filename='draft.md', content=b'# Example\nBody')
            project.view()
            for name in ('base', 'ingestion', 'audits', 'revision', 'exports'):
                module = importlib.import_module('document_review_stores.' + name)
                self.assertEqual(module.__name__, 'document_review_stores.' + name)
                self.assertEqual(module.__spec__.name, module.__name__)
                self.assertTrue(module.__file__.endswith(name + '.py'))

    def test_facade_dependency_patch_is_seen_and_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            project = studio.DocumentReviewProject.create(directory, filename='draft.md', content=b'# Example\nBody')
            original = project.manifest()
            with patch.object(studio, '_read_json', return_value={'patched': True}):
                self.assertEqual(project.manifest(), {'patched': True})
            self.assertEqual(project.manifest(), original)


if __name__ == '__main__':
    unittest.main()
