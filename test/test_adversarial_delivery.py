"""Exercise deep-review delivery across UI transactions, export and restore."""

import hashlib
import io
import json
import unittest
import zipfile

from document_review_adversarial import ENVELOPE_FIELDS, response_example
from document_review_studio import DocumentReviewProject, ReviewStudioError
from document_review_ui import StudioApp
from project_lifecycle import create_backup, restore_backup
from studio_ui_state import document_scope
from test import test_review_round_protocols as fixtures


class AdversarialDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReviewRoundProtocolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.project = self.fixture.project
        request = self.fixture.request()
        self.finding = self.fixture.collect(request, self.fixture.response(request)).findings[0]
        self.app = StudioApp.create(self.project.root.parent, self.project.root)

    def act(self, action, data, identity):
        self.app = self.app.act({
            "action": action, "data": data, "request_id": identity,
            "project_directory": self.project.root.name,
            "document_scope": document_scope(self.project),
        })
        return self.project.adversarial_reviews()[0]

    def start(self):
        return self.act("prepare_adversarial_review", {
            "finding_id": self.finding.finding_id, "provider": "defender", "model": "model-D",
        }, "prepare-defense-0001")

    def response(self, request, *, disposition="narrow"):
        result = response_example(request["stage"])
        block = self.project._review_document_record()[1].blocks[0]
        result["context_evidence"][0].update(block_id=block.block_id, quote=block.text)
        if request["stage"] == "assessment":
            result["disposition"] = disposition
            if disposition == "withdraw":
                result.update(remaining_issue="", minimal_repair="")
        return {**{key: request[key] for key in ENVELOPE_FIELDS}, "result": result}

    def collect(self, session, identity, payload=None):
        request = session["requests"][-1]
        response = json.dumps(payload or self.response(request), ensure_ascii=False)
        data = {"session_id": session["session_id"], "request_id": request["request_id"], "response": response}
        return self.act("import_adversarial_response", data, identity), data

    def complete(self, *, disposition="narrow"):
        session, _ = self.collect(self.start(), "collect-defense-0001")
        session = self.act("prepare_adversarial_assessment", {
            "session_id": session["session_id"], "provider": "assessor", "model": "model-A",
        }, "prepare-assessment-0001")
        return self.collect(session, "collect-assessment-0001",
                            self.response(session["requests"][-1], disposition=disposition))[0]

    def test_ui_retries_do_not_duplicate_sessions_and_do_not_replace_human_decisions(self):
        self.project.decide_finding(self.finding.finding_id, "defer", reason="Await full evidence")
        session = self.complete(disposition="withdraw")
        replay = self.start()
        self.assertEqual(session["session_id"], replay["session_id"])
        self.assertEqual(len(self.project.adversarial_reviews()), 1)
        self.assertEqual(replay["status"], "completed")
        self.assertEqual(self.project.findings()[0].status, "defer")
        self.assertEqual(self.project.integrity_errors(), [])

    def test_both_report_exports_include_exchange_bytes_and_model_proposal(self):
        session = self.complete()
        ai = self.project.export_ai_reviews()
        with self.assertRaises(ReviewStudioError):
            self.project.export()
        self.project.decide_finding(self.finding.finding_id, "defer", reason="Model review still needs author judgment")
        formal = self.project.export()
        for output, json_name, report_name, zip_name in (
            (ai, "AI审查结果.json", "AI审查报告.md", "AI审查包.zip"),
            (formal, "audit.json", "audit.md", "audit-package.zip"),
        ):
            with self.subTest(export=json_name):
                snapshot = json.loads((output / json_name).read_text(encoding="utf-8"))
                exchange = snapshot["adversarial_reviews"][0]
                self.assertEqual(exchange["session_id"], session["session_id"])
                self.assertEqual(exchange["assessment"]["disposition"], "narrow")
                for artifact in exchange["artifacts"]:
                    path = artifact["relative_path"]
                    raw = (self.project.root / path).read_bytes()
                    self.assertEqual((output / path).read_bytes(), raw)
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), artifact["sha256"])
                report = (output / report_name).read_text(encoding="utf-8")
                self.assertIn("Adversarial deep review", report)
                self.assertIn(session["assessment"]["reasons"], report)
                self.assertIn("Human decisions remain separate", report)
                with zipfile.ZipFile(output / zip_name) as archive:
                    self.assertIsNone(archive.testzip())
                    prefix = "project/" if json_name == "audit.json" else ""
                    self.assertIn(prefix + session["relative_path"], archive.namelist())
        self.assertEqual(self.project.integrity_errors(), [])

    def test_protocol_download_contains_both_stages_and_exact_prompt(self):
        session = self.complete()
        with zipfile.ZipFile(io.BytesIO(self.app.protocol_bundle())) as archive:
            for request in session["requests"]:
                folder = f"adversarial/{session['session_id']}/{request['stage']}"
                self.assertEqual(archive.read(folder + "/prompt.md").decode(), request["prompt"])
                metadata = json.loads(archive.read(folder + "/request.json"))
                self.assertEqual(metadata["request_id"], request["request_id"])

    def test_backup_restores_completed_review_with_portable_bindings(self):
        session = self.complete()
        backup = create_backup(self.project.root, self.project.root.parent / "adversarial-backup.zip")
        restored_root = restore_backup(backup, self.project.root.parent / "restored")
        restored = DocumentReviewProject(restored_root)
        self.assertNotEqual(restored.root, self.project.root)
        self.assertEqual(restored.integrity_errors(), [])
        self.assertEqual(restored.adversarial_reviews(), [session])
        self.assertEqual(restored.view()["adversarial_reviews"], [session])

    def test_rejected_ui_import_is_archived_once_and_corrected_retry_succeeds(self):
        session = self.start()
        response = self.response(session["requests"][-1])
        response["result"]["context_evidence"][0]["quote"] = "This quotation is not in the manuscript"
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.collect(session, "collect-invalid-0001", response)
        directory = (self.project.root / session["relative_path"]).parent
        self.assertEqual(len(list(directory.glob("rejections/*/response.txt"))), 1)
        self.assertEqual(self.project.adversarial_reviews()[0]["status"], "awaiting_defense")
        accepted, _ = self.collect(session, "collect-corrected-0001")
        self.assertEqual(accepted["status"], "defense_ready")
        self.assertEqual(self.project.integrity_errors(), [])

    def test_location_correction_stales_session_without_relabeling_history(self):
        session = self.start()
        other = self.project._review_document_record()[1].blocks[1]
        self.project.correct_finding_location(self.finding.finding_id, other.block_id, reason="Correct actual passage")
        with self.assertRaises(ValueError):
            self.collect(session, "stale-defense-0001")
        self.assertFalse(self.project.adversarial_reviews()[0]["current"])
        self.assertEqual(self.project.adversarial_reviews()[0]["status"], "awaiting_defense")

    def test_tampered_exchange_keeps_read_only_recovery_view_available(self):
        session = self.start()
        path = self.project.root / session["relative_path"]
        path.write_bytes(path.read_bytes() + b" ")
        view = self.project.view()
        self.assertTrue(view["state"]["read_only"])
        self.assertTrue(view["state"]["integrity_errors"])
        self.assertFalse(view["can_review"])
        self.assertEqual(view["adversarial_reviews"], [])
        with self.assertRaises(ValueError):
            self.project.adversarial_reviews()

    def test_followup_deep_review_exports_before_new_initial_audit_or_human_decision(self):
        _, revision, followup = self.fixture.followup()
        self.finding = self.project.findings()[0]
        view = self.project.view()
        self.assertIn(self.finding.finding_id, view["adversarial_eligible_finding_ids"])
        session = self.complete()
        output = self.project.export_ai_reviews()
        snapshot = json.loads((output / "AI审查结果.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["source"]["sha256"], revision["revised_sha256"])
        self.assertEqual(snapshot["runs"][0]["review_round_id"], followup["round_id"])
        self.assertEqual(snapshot["runs"][0]["origin"], "external-recheck-followup")
        self.assertIn(self.finding.finding_id, {item["finding_id"] for item in snapshot["findings"]})
        self.assertEqual(snapshot["adversarial_reviews"][0]["session_id"], session["session_id"])
        self.assertEqual(self.project.findings()[0].status, "open")
        self.assertFalse(snapshot["human_decisions_included"])
        for raw in snapshot["raw_responses"]:
            self.assertEqual(hashlib.sha256((output / raw["relative_path"]).read_bytes()).hexdigest(), raw["sha256"])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_local_findings_are_not_offered_as_independent_model_challenges(self):
        self.project.run_local_prechecks([self.finding.critic])
        view = self.project.view()
        self.assertEqual(view["adversarial_eligible_finding_ids"], [])

    def test_restart_export_preserves_superseded_exchange_as_history(self):
        previous = self.start()
        current = self.project.prepare_adversarial_review(
            self.finding.finding_id, provider="replacement", model="new-model", restart=True)
        output = self.project.export_ai_reviews()
        snapshot = json.loads((output / "AI审查结果.json").read_text(encoding="utf-8"))
        sessions = {item["session_id"]: item for item in snapshot["adversarial_reviews"]}
        self.assertEqual(set(sessions), {previous["session_id"], current["session_id"]})
        self.assertFalse(sessions[previous["session_id"]]["current"])
        self.assertTrue(sessions[current["session_id"]]["current"])
        self.assertEqual(sessions[current["session_id"]]["supersedes_session_id"], previous["session_id"])
        for session in (previous, current):
            self.assertTrue((output / session["relative_path"]).is_file())

    def test_ui_does_not_coerce_nontext_model_declarations(self):
        for provider in (None, False, ["provider"], {"name": "provider"}):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                self.act("prepare_adversarial_review", {
                    "finding_id": self.finding.finding_id, "provider": provider, "model": "model",
                }, "invalid-provider-0001")
        self.assertEqual(self.project.adversarial_reviews(), [])


if __name__ == "__main__":
    unittest.main()
