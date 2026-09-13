"""Real UI/store transaction boundaries for archived draft rejections."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import document_review_studio as studio
from document_review_ui import StudioApp
from project_lifecycle import CommitValidationResult, _files
from test.test_delivery import ready


class RevisionDraftingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library = Path(self.temporary.name)
        self.project = ready(self.library)
        for index, finding in enumerate(self.project.findings()):
            self.project.decide_finding(finding.finding_id, "accept" if index == 0 else "reject", reason="scope")
        self.action_id = self.project.prepare_revision_plan()["actions"][0]["action_id"]
        self.project.set_revision_action_operation(self.action_id, "replace_block", reason="write a draft")
        self.request = self.project.revision_drafting_prompt(self.action_id)
        self.app = StudioApp.create(self.library, self.project.root)

    def response(self, **changes):
        return {"request_sha256": self.request["request_sha256"], "action_id": self.action_id,
                "after_text": "项目负责人周五前完成报名。", "rationale": "明确负责人和截止时间", **changes}

    def submit(self, response):
        return self.app.act({"request_id": "draft-request-12345678", "project_directory": self.project.root.name,
                             "action": "import_revision_draft", "data": {"action_id": self.action_id, "response": response}})

    def snapshot(self):
        return {relative: path.read_bytes() for relative, path in _files(self.project.root).items()}

    def assert_rejected_archive(self, raw):
        attempts = list((self.project.root / "drafting-attempts").iterdir())
        self.assertEqual(len(attempts), 1)
        directory = attempts[0]
        self.assertEqual((directory / "response.txt").read_text(encoding="utf-8"), raw)
        result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["request_sha256"], self.request["request_sha256"])
        self.assertEqual(result["action_id"], self.action_id)
        self.assertTrue(result["errors"])
        self.assertFalse(list((self.project.root / "revision-hunks").glob("*.json")))
        self.assertFalse((self.project.root / ".requests" / "draft-request-12345678.json").exists())
        self.assertEqual(studio.DocumentReviewProject(self.project.root).integrity_errors(), [])

    def test_invalid_json_archive_survives_ui_and_store_nested_transactions(self):
        raw = "not JSON"
        with self.assertRaises(CommitValidationResult):
            self.submit(raw)
        self.assert_rejected_archive(raw)

    def test_wrong_request_archive_survives_nested_transaction(self):
        raw = json.dumps(self.response(request_sha256="0" * 64))
        with self.assertRaises(CommitValidationResult):
            self.submit(raw)
        self.assert_rejected_archive(raw)

    def test_non_text_draft_is_archived_before_proposal(self):
        raw = json.dumps(self.response(after_text=["wrong shape"]))
        with patch.object(studio.DocumentReviewProject, "propose_revision_hunk") as propose:
            with self.assertRaises(CommitValidationResult):
                self.submit(raw)
        propose.assert_not_called()
        self.assert_rejected_archive(raw)

    def test_duplicate_json_fields_are_archived_as_rejected(self):
        raw = json.dumps(self.response())[:-1] + ', "rationale": "duplicate"}'
        with self.assertRaises(CommitValidationResult):
            self.submit(raw)
        self.assert_rejected_archive(raw)

    def test_nonfinite_json_values_are_archived_as_rejected(self):
        raw = json.dumps(self.response(after_text=float("nan")))
        with self.assertRaises(CommitValidationResult):
            self.submit(raw)
        self.assert_rejected_archive(raw)

    def test_proposal_failure_after_hunk_write_rolls_back_entire_attempt(self):
        before = self.snapshot()
        append = studio.DocumentReviewProject._append_event

        def fail_after_hunk(project, event, payload):
            if event == "revision_hunk_proposed":
                raise ValueError("injected proposal write failure")
            return append(project, event, payload)

        with patch.object(studio.DocumentReviewProject, "_append_event", autospec=True, side_effect=fail_after_hunk):
            with self.assertRaisesRegex(ValueError, "injected proposal") as rejected:
                self.submit(json.dumps(self.response()))
        self.assertNotIsInstance(rejected.exception, CommitValidationResult)
        self.assertEqual(self.snapshot(), before)

    def test_success_result_binding_failure_rolls_back_hunk_and_archive(self):
        before = self.snapshot()
        tracked = studio._write_tracked

        def fail_binding(root, path, data, **kwargs):
            if kwargs.get("provenance") == "draft-response-to-hunk-binding":
                raise KeyError("injected result binding failure")
            return tracked(root, path, data, **kwargs)

        with patch.object(studio, "_write_tracked", side_effect=fail_binding):
            with self.assertRaisesRegex(KeyError, "injected result binding"):
                self.submit(json.dumps(self.response()))
        self.assertEqual(self.snapshot(), before)

    def test_semantic_proposal_rejection_does_not_commit_partial_archive(self):
        before = self.snapshot()
        raw = json.dumps(self.response(after_text=""))
        with self.assertRaises(ValueError) as rejected:
            self.submit(raw)
        self.assertNotIsInstance(rejected.exception, CommitValidationResult)
        self.assertEqual(self.snapshot(), before)

    def test_rejection_record_failure_rolls_back_prompt_and_response(self):
        before = self.snapshot()
        tracked = studio._write_tracked

        def fail_rejection(root, path, data, **kwargs):
            if path.name == "result.json" and "drafting-attempts" in path.parts:
                raise OSError("cannot persist rejection result")
            return tracked(root, path, data, **kwargs)

        with patch.object(studio, "_write_tracked", side_effect=fail_rejection):
            with self.assertRaisesRegex(OSError, "cannot persist rejection"):
                self.submit("invalid JSON")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
