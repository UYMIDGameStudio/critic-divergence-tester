from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from document_review_studio import DocumentReviewProject
from document_review_ui import StudioApp
from studio_ui_state import UIStateConflict, read_ui_draft, save_ui_draft, read_ui_language, save_ui_language


class StudioDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = DocumentReviewProject.create(self.root, filename="draft.txt", content=b"Original document.")

    def payload(self, fields=None):
        saved = read_ui_draft(self.project)
        return {"project_directory": self.project.root.name, "scope": saved["scope"],
                "revision": saved["revision"], "fields": fields or {"audience": "authors"}, "scroll": 20}

    def test_language_preference_survives_app_restart_and_corruption_is_recoverable(self):
        save_ui_language(self.root, "en")
        self.assertEqual(StudioApp.create(self.root).view()["ui_language"], "en")
        path = self.root / ".ui-preferences.json"
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            save_ui_language(self.root, "unsupported")
        self.assertEqual(path.read_bytes(), original)
        for invalid in ({"language": []}, {"language": {}}, [], {"language": "invalid"}):
            path.write_text(json.dumps(invalid), encoding="utf-8")
            self.assertIsNone(read_ui_language(self.root))
        save_ui_language(self.root, "zh-Hant")
        self.assertEqual(read_ui_language(self.root), "zh-Hant")

    def test_concurrent_page_cannot_overwrite_newer_saved_draft(self):
        first, stale = self.payload(), self.payload({"audience": "stale page"})
        save_ui_draft(self.project, first)
        with self.assertRaises(UIStateConflict):
            save_ui_draft(self.project, stale)
        actual = read_ui_draft(self.project)
        self.assertEqual(actual["fields"], first["fields"])
        self.assertEqual(actual["revision"], 1)

    def test_corrected_document_keeps_older_draft_without_injecting_it(self):
        old = self.payload({"ai-response::TASK-A": "old answer"})
        save_ui_draft(self.project, old)
        self.project.confirm_extraction("correct", corrected_text="Corrected document.")
        current = read_ui_draft(self.project)
        self.assertNotEqual(current["scope"], old["scope"])
        self.assertEqual(current["fields"], {})
        stale = {**old, "revision": current["revision"]}
        with self.assertRaises(UIStateConflict):
            save_ui_draft(self.project, stale)
        save_ui_draft(self.project, self.payload({"audience": "new draft"}))
        record = json.loads((self.project.root / ".ui-draft.json").read_text(encoding="utf-8"))
        self.assertEqual(record["documents"][old["scope"]]["fields"], old["fields"])

    def test_invalid_and_foreign_drafts_do_not_write(self):
        for update in ({"project_directory": "other"}, {"fields": {"__proto__": "x"}},
                       {"fields": {"a": []}}, {"revision": True}, {"scroll": float("nan")}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                save_ui_draft(self.project, {**self.payload(), **update})
        self.assertFalse((self.project.root / ".ui-draft.json").exists())

    def test_broken_draft_is_reported_and_preserved(self):
        path = self.project.root / ".ui-draft.json"
        path.write_bytes(b"{broken")
        self.assertIn("error", read_ui_draft(self.project))
        with self.assertRaises(ValueError):
            save_ui_draft(self.project, self.payload())
        self.assertEqual(path.read_bytes(), b"{broken")
        app = replace(StudioApp.create(self.root), project=self.project)
        self.assertIn("error", app.view()["selected"]["ui_draft"])

    def test_legacy_fields_remain_available_for_explicit_recovery(self):
        path = self.project.root / ".ui-draft.json"
        path.write_text(json.dumps({"fields": {"ai-response": "unbound old answer"}, "scroll": 0}), encoding="utf-8")
        draft = read_ui_draft(self.project)
        self.assertEqual(draft["fields"], {})
        self.assertEqual(draft["legacy"]["ai-response"], "unbound old answer")
        save_ui_draft(self.project, self.payload())
        self.assertEqual(read_ui_draft(self.project)["legacy"], draft["legacy"])

    def test_receipt_can_replay_document_change_but_new_stale_request_cannot(self):
        app = replace(StudioApp.create(self.root), project=self.project)
        action = {"action": "confirm_extraction", "data": {"choice": "correct", "corrected_text": "A corrected document."},
                  "request_id": "stable-action-123456789", "project_directory": self.project.root.name,
                  "document_scope": read_ui_draft(self.project)["scope"]}
        app.act(action)
        decisions = list((self.project.root / "extraction-decisions").glob("*.json"))
        app.act(action)
        self.assertEqual(list((self.project.root / "extraction-decisions").glob("*.json")), decisions)
        with self.assertRaises(UIStateConflict):
            app.act({**action, "request_id": "new-action-123456789"})
