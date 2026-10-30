"""Review changes require the state shown to the author, independently of drafts."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from document_review_ui import StudioApp
from document_review_studio import ReviewStudioError
from project_lifecycle import CommitValidationResult, _files
from studio_ui_state import UIStateConflict, read_ui_draft, save_ui_draft
from test.test_delivery import ready
from unified_app import serve_unified_app


class StudioReviewContextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = ready(self.root)
        self.app = StudioApp.create(self.root, self.project.root)
        self.view = self.app.view()
        self.finding_id = self.view["selected"]["findings"][0]["finding_id"]

    def decision(self, decision, context=None, request_id="context-request-12345678"):
        return {"action": "decide_finding", "request_id": request_id,
                "project_directory": self.project.root.name,
                "document_scope": self.view["selected"]["ui_draft"]["scope"],
                "review_context": context or self.view["review_context"],
                "data": {"finding_id": self.finding_id, "decision": decision, "reason": "Synthetic explicit decision"}}

    def test_stale_decision_is_rejected_even_when_the_document_and_draft_are_unchanged(self):
        self.app.act(self.decision("reject"), require_context=True)
        before = {name: path.read_bytes() for name, path in _files(self.project.root).items()}
        with self.assertRaises(UIStateConflict):
            self.app.act(self.decision("accept", request_id="stale-page-123456789"), require_context=True)
        self.assertEqual({name: path.read_bytes() for name, path in _files(self.project.root).items()}, before)
        current = self.app.view()
        self.assertEqual(current["selected"]["ui_draft"], self.view["selected"]["ui_draft"])
        self.assertNotEqual(current["review_context"], self.view["review_context"])
        self.app.act(self.decision("accept", current["review_context"], "refreshed-page-123456789"), require_context=True)
        self.assertEqual(self.project._decisions()[self.finding_id]["decision"], "accept")
        self.assertEqual(len(self.project._decision_records()[self.finding_id]), 2)

    def test_receipt_replay_never_reapplies_an_old_decision_after_a_new_one(self):
        first = self.decision("accept")
        self.app.act(first, require_context=True)
        current = self.app.view()["review_context"]
        self.app.act(self.decision("reject", current, "later-request-123456789"), require_context=True)
        self.app.act(first, require_context=True)
        self.assertEqual(self.project._decisions()[self.finding_id]["decision"], "reject")
        self.assertEqual(len(self.project._decision_records()[self.finding_id]), 2)
        with self.assertRaises(ReviewStudioError):
            self.app.act({**first, "review_context": self.app.view()["review_context"]}, require_context=True)

    def test_drafts_do_not_invalidate_review_context_and_sessions_do(self):
        saved = read_ui_draft(self.project)
        save_ui_draft(self.project, {"project_directory": self.project.root.name, "scope": saved["scope"],
            "revision": saved["revision"], "fields": {"reason": "An unsubmitted draft."}, "scroll": 0})
        self.assertEqual(self.app.view()["review_context"], self.view["review_context"])
        other = StudioApp.create(self.root, self.project.root)
        self.assertNotEqual(other.view()["review_context"], self.view["review_context"])
        with self.assertRaises(UIStateConflict):
            other.act(self.decision("accept"), require_context=True)

    def test_two_simultaneous_decisions_from_one_snapshot_commit_only_one(self):
        def submit(decision):
            try:
                self.app.act(self.decision(decision, request_id=decision + "-request-123456789"), require_context=True)
                return "committed"
            except UIStateConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, ("accept", "reject")))
        self.assertCountEqual(results, ["committed", "conflict"])
        self.assertEqual(len(self.project._decision_records()[self.finding_id]), 1)
        self.assertEqual(self.project.integrity_errors(), [])

    def test_archived_rejection_updates_only_its_own_context_for_a_corrected_retry(self):
        from test.test_revision_drafting import RevisionDraftingTests
        fixture = RevisionDraftingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.app
        old = app.view()
        payload = {"action": "import_revision_draft", "request_id": "rejected-draft-12345678",
            "project_directory": fixture.project.root.name, "review_context": old["review_context"],
            "document_scope": old["selected"]["ui_draft"]["scope"],
            "data": {"action_id": fixture.action_id, "response": "not JSON"}}
        with self.assertRaises(CommitValidationResult) as rejected:
            app.act(payload, require_context=True)
        context = rejected.exception.review_context
        self.assertNotEqual(context, old["review_context"])
        corrected = {**payload, "request_id": "corrected-draft-12345678",
            "data": {"action_id": fixture.action_id, "response": json.dumps(fixture.response())}}
        with self.assertRaises(UIStateConflict):
            app.act(corrected, require_context=True)
        app.act({**corrected, "review_context": context}, require_context=True)
        self.assertEqual(len(list((fixture.project.root / "revision-hunks").glob("*.json"))), 1)
        self.assertEqual(fixture.project.integrity_errors(), [])

    def test_http_requires_a_current_context_with_or_without_a_receipt_id(self):
        server, _ = serve_unified_app(data_dir=self.root, project_dir=self.project.root, open_browser=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def request(payload=None):
                client = http.client.HTTPConnection(*server.server_address, timeout=10)
                try:
                    client.request("POST" if payload is not None else "GET", "/api/action" if payload is not None else "/api/state",
                        body=json.dumps(payload).encode() if payload is not None else None,
                        headers={"X-Document-Review-Token": server.app.token, "Content-Type": "application/json"})
                    response = client.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    client.close()
            _, view = request()
            for invalid in (None, [], "0" * 64):
                payload = self.decision("accept", view["review_context"])
                payload["review_context"] = invalid
                payload.pop("request_id")
                self.assertEqual(request(payload)[0], 409)
            missing = self.decision("accept")
            missing.pop("review_context")
            self.assertEqual(request(missing)[0], 409)
            first = self.decision("reject", view["review_context"])
            status, current = request(first)
            self.assertEqual(status, 201)
            self.assertNotEqual(current["review_context"], view["review_context"])
            status, error = request(self.decision("accept", view["review_context"], "stale-http-123456789"))
            self.assertEqual(status, 409)
            self.assertNotIn("review_context", error)
            self.assertEqual(request(first)[0], 201)
            self.assertEqual(self.project._decisions()[self.finding_id]["decision"], "reject")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
