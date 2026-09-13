"""Extraction corrections retain immutable parents and a usable project."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import document_review_studio as studio
from document_review_stores.ingestion import IngestionState
from project_lifecycle import _files
from test.test_delivery import ready


class ExtractionCorrectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.library = Path(temporary.name)
        self.project = studio.DocumentReviewProject.create(self.library, filename="draft.txt", content=b"Original document.")

    def snapshot(self):
        return {relative: path.read_bytes() for relative, path in _files(self.project.root).items()}

    def test_correction_keeps_original_source_and_integrity(self):
        previous = self.project.document_path.read_bytes()
        self.project.confirm_extraction("correct", corrected_text="Corrected document.")
        self.assertEqual(self.project.integrity_errors(), [])
        self.assertFalse(self.project.state()["read_only"])
        self.assertIn("Corrected document.", self.project.document().plain_text)
        self.assertEqual((self.project.root / "source" / "draft.txt").read_bytes(), b"Original document.")
        snapshots = list((self.project.root / "extraction" / "snapshots").glob("*/document.json"))
        self.assertTrue(any(path.read_bytes() == previous for path in snapshots))

    def test_confirmed_extraction_supports_multiple_corrections_without_rewriting_history(self):
        self.project.confirm_extraction("confirm")
        first_decision = next((self.project.root / "extraction-decisions").glob("*.json"))
        first_bytes = first_decision.read_bytes()
        first_receipt = studio._integrity_receipt_path(first_decision).read_bytes()
        self.project.confirm_extraction("correct", corrected_text="First correction.")
        first_correction = next((self.project.root / "extraction" / "corrections").glob("*/human-correction.md"))
        corrected_bytes = first_correction.read_bytes()
        self.project.confirm_extraction("correct", corrected_text="Second correction.")
        self.assertEqual(self.project.integrity_errors(), [])
        self.assertEqual(first_decision.read_bytes(), first_bytes)
        self.assertEqual(studio._integrity_receipt_path(first_decision).read_bytes(), first_receipt)
        self.assertEqual(first_correction.read_bytes(), corrected_bytes)
        self.assertEqual(len(list((self.project.root / "extraction" / "corrections").glob("*/human-correction.md"))), 2)
        self.assertEqual(len(self.project._extraction_decision_records()), 3)
        self.assertIn("Second correction.", self.project.document().plain_text)

    def test_correction_failure_rolls_back_snapshots_and_document(self):
        before = self.snapshot()
        original = IngestionState._append_event

        def fail_event(project, event, payload):
            if event == "extraction_corrected":
                raise OSError("write failed after corrected document")
            return original(project, event, payload)

        with patch.object(IngestionState, "_append_event", autospec=True, side_effect=fail_event):
            with self.assertRaises(OSError):
                self.project.confirm_extraction("correct", corrected_text="Corrected document.")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.project.integrity_errors(), [])

    def test_reviewed_document_correction_is_rejected_before_rewriting_source(self):
        project = ready(self.library / "reviewed")
        before = {name: path.read_bytes() for name, path in _files(project.root).items()}
        with self.assertRaisesRegex(ValueError, "上下文|审查"):
            project.confirm_extraction("correct", corrected_text="Wrong replacement for reviewed source.")
        self.assertEqual({name: path.read_bytes() for name, path in _files(project.root).items()}, before)
        self.assertEqual(project.integrity_errors(), [])

    def test_legacy_current_parent_decision_is_refused_without_modifying_history(self):
        self.project.confirm_extraction("confirm")
        decision = next((self.project.root / "extraction-decisions").glob("*.json"))
        receipt = json.loads(studio._integrity_receipt_path(decision).read_text(encoding="utf-8"))
        receipt["parents"] = [studio._parent_ref(self.project.root, self.project.document_path, role="current-structured-document")]
        read_json = studio._read_json

        def old_receipt(path):
            if path == studio._integrity_receipt_path(decision):
                return receipt
            return read_json(path)

        before = self.snapshot()
        with patch.object(studio, "_read_json", side_effect=old_receipt):
            with self.assertRaisesRegex(ValueError, "旧版.*新建项目"):
                self.project.confirm_extraction("correct", corrected_text="Corrected document.")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
