"""Damaged exchanges remain recoverable; batch lookups do not cache history."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import document_review_studio as studio
from project_lifecycle import create_backup, restore_backup
from test.test_delivery import ready
from unified_app import serve_unified_app


class ReviewRecoveryTests(unittest.TestCase):
    def test_damaged_exchange_and_context_records_return_a_safe_recovery_view(self):
        for kind in ("request-json", "request-shape", "prompt", "audit-json", "context-json", "context-shape", "round-shape"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                project = ready(Path(temp))
                request = project.prepare_ai_audits(["execution_feasibility"], provider="fixture", model="not-invoked")[0]
                folder = project.root / "ai-requests" / request["request_id"]
                if kind.startswith("request"):
                    path = folder / "request.json"
                    data = b'{"UNTRUSTED-RECOVERY-CONTENT":' if kind == "request-json" else b'[]'
                elif kind == "prompt":
                    path, data = folder / "prompt.md", b"UNTRUSTED-RECOVERY-CONTENT"
                elif kind == "audit-json":
                    path = project._audit_run_records()[0][0]
                    data = b'{"UNTRUSTED-RECOVERY-CONTENT":'
                elif kind.startswith("context"):
                    path = project.root / "context.json"
                    data = b'{"UNTRUSTED-RECOVERY-CONTENT":' if kind == "context-json" else b'{"unexpected":true}'
                else:
                    path = project.root / "revisions" / "synthetic" / "followup-round.json"
                    path.parent.mkdir(parents=True)
                    data = b'{"artifact_type":"document-review-followup-round","sequence":{}}'
                path.write_bytes(data)
                view = project.view()
                self.assertTrue(view["state"]["read_only"])
                self.assertTrue(view["state"]["integrity_errors"])
                self.assertFalse(view["can_review"])
                self.assertFalse(view["extraction"]["available"])
                for key in ("ai_requests", "argument_assessments", "adversarial_reviews", "findings", "exports"):
                    self.assertEqual(view[key], [])
                self.assertNotIn("UNTRUSTED-RECOVERY-CONTENT", json.dumps(view))
                with self.assertRaises(studio.ReviewStudioError):
                    project.prepare_ai_audits(["execution_feasibility"], provider="fixture", model="not-invoked")
                self.assertEqual(path.read_bytes(), data)

    def test_verified_backup_can_restore_to_a_separate_project_after_request_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = ready(root / "library")
            request = project.prepare_ai_audits(["execution_feasibility"], provider="fixture", model="not-invoked")[0]
            archive = create_backup(project.root, root / "verified.zip")
            path = project.root / "ai-requests" / request["request_id"] / "request.json"
            path.write_bytes(b"{truncated")
            self.assertTrue(project.view()["state"]["read_only"])
            restored = studio.DocumentReviewProject(restore_backup(archive, root / "restored"))
            view = restored.view()
            self.assertFalse(view["state"]["read_only"])
            self.assertEqual(view["ai_requests"][0]["request_id"], request["request_id"])
            self.assertNotEqual(restored.root, project.root)
            self.assertEqual(path.read_bytes(), b"{truncated")
            self.assertEqual(restored.integrity_errors(), [])

    def test_http_recovery_state_remains_available_and_download_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = ready(root)
            request = project.prepare_ai_audits(["execution_feasibility"], provider="fixture", model="not-invoked")[0]
            (project.root / "ai-requests" / request["request_id"] / "request.json").write_bytes(b"{truncated")
            server, url = serve_unified_app(data_dir=root, project_dir=project.root, open_browser=False)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                headers = {"X-Document-Review-Token": server.app.token}
                with urlopen(Request(url + "api/state", headers=headers), timeout=10) as response:
                    state = json.load(response)
                self.assertTrue(state["selected"]["state"]["read_only"])
                self.assertEqual(state["selected"]["ai_requests"], [])
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(url + "api/download?path=project.json", headers=headers), timeout=10)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class BatchedReviewLookupTests(unittest.TestCase):
    def test_batch_listing_reads_each_request_once_and_preserves_supersession(self):
        with tempfile.TemporaryDirectory() as temp:
            project = ready(Path(temp))
            requests = project.prepare_ai_audits(provider="fixture", model="not-invoked")
            replacement = project.prepare_ai_audits(["execution_feasibility"], provider="fixture", model="replacement")[0]
            expected = {row["critic"]: row["request_id"] for row in requests}
            expected["execution_feasibility"] = replacement["request_id"]
            original = studio._read_json
            reads = {}

            def counted(path):
                if path.name == "request.json":
                    reads[path] = reads.get(path, 0) + 1
                return original(path)

            with patch.object(studio, "_read_json", counted):
                rows = project.ai_requests()
            self.assertEqual({row["critic"]: row["request_id"] for row in rows}, expected)
            self.assertEqual(len(reads), len(requests) + 1)
            self.assertTrue(all(count == 1 for count in reads.values()))
            # A later action must observe new files rather than retain a stale cache.
            next_request = project.prepare_ai_audits(["expression_ambiguity"], provider="fixture", model="later")[0]
            latest = {row["critic"]: row for row in project.ai_requests()}
            self.assertEqual(latest["expression_ambiguity"]["request_id"], next_request["request_id"])
            self.assertEqual(project.integrity_errors(), [])

    def test_later_byte_changes_are_detected_after_a_batch_lookup(self):
        with tempfile.TemporaryDirectory() as temp:
            project = ready(Path(temp))
            request = project.prepare_ai_audits(["execution_feasibility"], provider="fixture", model="not-invoked")[0]
            before = project._active_ai_request("execution_feasibility")
            path = project.root / "ai-requests" / request["request_id"] / "request.json"
            path.write_bytes(path.read_bytes() + b" ")
            after = project._active_ai_request("execution_feasibility")
            self.assertNotEqual(before[2], after[2])
            self.assertTrue(project.view()["state"]["read_only"])


if __name__ == "__main__":
    unittest.main()
