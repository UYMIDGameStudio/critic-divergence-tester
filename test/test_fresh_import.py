"""Explicit re-import runs today's parser without rewriting prior review records."""
import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import document_review_studio as studio
from document_review_ui import StudioApp
from project_lifecycle import _files
from studio_selftest import _fragmented_pdf_fixture
from test.test_delivery import ready


class FreshImportTests(unittest.TestCase):
    def test_fresh_copy_resets_review_and_retains_every_old_file(self):
        with tempfile.TemporaryDirectory() as temp:
            old = ready(Path(temp))
            old.decide_finding(old.findings()[0].finding_id, "defer", reason="Await author")
            source = old.manifest()["source"]
            raw = (old.root / source["relative_path"]).read_bytes()
            before = {name: path.read_bytes() for name, path in _files(old.root).items()}
            with patch.object(studio, "ingest_bytes", wraps=studio.ingest_bytes) as parse:
                reopened = studio.DocumentReviewProject.create(temp, filename=source["name"], content=raw)
                self.assertEqual(reopened.root, old.root)
                parse.assert_not_called()
                fresh = studio.DocumentReviewProject.create(temp, filename=source["name"], content=raw, new_project=True)
                parse.assert_called_once()
            self.assertNotEqual(fresh.root, old.root)
            self.assertNotEqual(fresh.manifest()["project_id"], old.manifest()["project_id"])
            self.assertEqual(fresh.manifest()["source"], source)
            self.assertEqual(fresh.manifest()["title"], "draft (2)")
            self.assertEqual(fresh.state()["extraction_state"], "unconfirmed")
            self.assertIsNone(fresh.context())
            self.assertFalse(list((fresh.root / "audits").glob("*/*.json")))
            self.assertEqual(fresh.integrity_errors(), [])
            self.assertEqual(before, {name: path.read_bytes() for name, path in _files(old.root).items()})
            another = studio.DocumentReviewProject.create(temp, filename=source["name"], content=raw, new_project=True)
            self.assertEqual(another.manifest()["title"], "draft (3)")
            self.assertEqual(len({old.manifest()["project_id"], fresh.manifest()["project_id"], another.manifest()["project_id"]}), 3)

    def test_pdf_reimport_uses_current_layout_parser(self):
        try:
            import pypdf
        except ImportError:
            self.skipTest("optional pypdf not installed")
        import document_review_ingest as ingest
        raw = _fragmented_pdf_fixture()
        with tempfile.TemporaryDirectory() as temp, patch.object(ingest, "_pdf_backend", return_value=("pypdf", pypdf)):
            # Simulate the previous parser's extraction, with intact provenance.
            with patch.object(ingest, "_recover_pdf_layout", side_effect=lambda page, plain: (plain, {"mode": "plain"})):
                old = studio.DocumentReviewProject.create(temp, filename="english.pdf", content=raw)
            previous = old.document_path.read_bytes()
            self.assertTrue(ingest._fragmented_pdf_lines(old.document().plain_text))
            fresh = studio.DocumentReviewProject.create(temp, filename="english.pdf", content=raw, new_project=True)
            self.assertFalse(ingest._fragmented_pdf_lines(fresh.document().plain_text))
            self.assertEqual(fresh.document().metadata["layout_recovered_pages"], [1])
            self.assertEqual(old.document_path.read_bytes(), previous)
            self.assertEqual(fresh.document().plain_text.split(), old.document().plain_text.split())

    def test_upload_option_is_explicit_and_strictly_boolean(self):
        with tempfile.TemporaryDirectory() as temp:
            app = StudioApp.create(temp)
            payload = {"filename": "draft.txt", "content_base64": base64.b64encode(b"Source text").decode()}
            old = app.upload(payload).project
            self.assertEqual(app.upload(payload).project.root, old.root)
            fresh = app.upload({**payload, "new_project": True, "title": "New review"}).project
            self.assertNotEqual(fresh.root, old.root)
            self.assertEqual(fresh.manifest()["title"], "New review")
            for invalid in ("false", 1, None, [], {}):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "boolean"):
                    app.upload({**payload, "new_project": invalid})


if __name__ == "__main__":
    unittest.main()
