"""Model JSON never persists non-finite values into review artifacts."""
from __future__ import annotations

import json
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import document_review_studio as studio
from project_lifecycle import _files
from test import test_document_review_studio as fixtures


def project_with_request(library):
    fixture = fixtures.DocumentReviewStudioTests()
    project = studio.DocumentReviewProject.create(library, filename="draft.md", content="# 活动\n\n原始内容。\n".encode())
    project.confirm_extraction("confirm")
    project.confirm_context(fixture.context())
    request = project.prepare_ai_audits(["execution_feasibility"], provider="provider", model="model")[0]
    return project, request


def envelope(project, request):
    return {**{key: request[key] for key in ("request_id", "prompt_sha256", "provider", "model", "critic")},
            "source_sha256": project.document().source.sha256, "findings": []}


def snapshot(project):
    return {name: path.read_bytes() for name, path in _files(project.root).items()}


class ModelResponseJSONTests(unittest.TestCase):
    def test_malformed_finding_fields_fail_before_writes_instead_of_breaking_export(self):
        with tempfile.TemporaryDirectory() as temp:
            project, request = project_with_request(Path(temp))
            original = fixtures.DocumentReviewStudioTests().model_finding(project, request["critic"], "F1", "Check responsibility")
            cases = [("severity", []), ("critic", {}), ("verification_state", []),
                     ("uncertainties", [{}]), ("competing_readings", [None]),
                     ("required_observation", []), ("location.block_id", {}),
                     ("location.page", True), ("location.bbox", [0, 1]),
                     ("external_basis.unresolved_facts", [{}]), ("external_basis.source_name", [])]
            for field, invalid in cases:
                with self.subTest(field=field):
                    item = copy.deepcopy(original)
                    target = item
                    keys = field.split(".")
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = invalid
                    payload = {**envelope(project, request), "findings": [item]}
                    before = snapshot(project)
                    with patch.object(studio, "_write_tracked", wraps=studio._write_tracked) as writes:
                        with self.assertRaises(studio.ReviewStudioError):
                            project.collect_model_audit(request["critic"], json.dumps(payload), provider="provider", model="model", request_id=request["request_id"])
                    writes.assert_not_called()
                    self.assertEqual(snapshot(project), before)

    def test_audit_rejects_nonfinite_constants_and_overflow_before_any_artifact_write(self):
        with tempfile.TemporaryDirectory() as temp:
            project, _ = project_with_request(Path(temp))
            for token in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
                with self.subTest(token=token):
                    request = project.prepare_ai_audits(["execution_feasibility"], provider="provider", model="model")[0]
                    raw = json.dumps(envelope(project, request))[:-1] + ', "observations": [{"measurement": ' + token + '}]}'
                    before = snapshot(project)
                    with patch.object(studio, "_write_tracked", wraps=studio._write_tracked) as writes:
                        with self.assertRaises(studio.ReviewStudioError):
                            project.collect_model_audit(request["critic"], raw, provider="provider", model="model", request_id=request["request_id"])
                    writes.assert_not_called()
                    self.assertEqual(snapshot(project), before)

    def test_audit_preserves_finite_numbers_bom_and_safe_normalization(self):
        with tempfile.TemporaryDirectory() as temp:
            project, request = project_with_request(Path(temp))
            finding = fixtures.DocumentReviewStudioTests().model_finding(project, request["critic"], "F1", "Unclear responsibility")
            finding["verification_state"] = "unverified"
            finding["external_basis"] = "No source supplied"
            payload = {**envelope(project, request), "findings": [finding], "observations": [{"measurement": 1.25e20, "ratio": 0.125}]}
            raw = b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8")
            run = project.collect_model_audit(request["critic"], raw, provider="provider", model="model", request_id=request["request_id"])
            path = project.root / "audits" / request["critic"] / f"{run.run_id}.json"
            saved = json.loads(path.read_bytes())
            self.assertEqual(saved["observations"], payload["observations"])
            self.assertEqual(saved["findings"][0]["verification_state"], "needs-human-verification")
            self.assertEqual({row["field"] for row in saved["response_normalizations"]}, {"verification_state", "external_basis"})
            self.assertEqual((path.parent / f"{run.run_id}.raw-response.json.txt").read_bytes(), raw)
            self.assertEqual(project.integrity_errors(), [])

    def test_external_recheck_rejects_nonfinite_values_even_in_unused_fields_before_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            project, request = project_with_request(Path(temp))
            finding = fixtures.DocumentReviewStudioTests().model_finding(project, request["critic"], "F1", "Unclear responsibility")
            payload = {**envelope(project, request), "findings": [finding]}
            project.collect_model_audit(request["critic"], json.dumps(payload), provider="provider", model="model", request_id=request["request_id"])
            accepted = project.findings()[0]
            project.decide_finding(accepted.finding_id, "accept", reason="Review accepted")
            action = project.prepare_revision_plan()["actions"][0]
            project.set_revision_action_operation(action["action_id"], "replace_block", reason="Specify responsibility")
            hunk = project.propose_revision_hunk(action["action_id"], "项目经理负责执行。", rationale="Assign responsibility")
            project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="Approved")
            directory = project.finalize_revision()
            revision = json.loads((directory / "revision.json").read_bytes())
            external = project.external_recheck_status(revision["revision_id"])["requests"][0]
            response = {key: external[key] for key in ("request_id", "prompt_sha256", "critic", "revision_id", "revised_sha256")}
            response.update(resolutions=[{"finding_id": accepted.finding_id, "state": "resolved", "reason": "Responsibility specified", "evidence": "项目经理负责执行。"}], new_findings=[])
            for token in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
                with self.subTest(token=token):
                    raw = json.dumps(response)[:-1] + ', "extra": {"measurement": ' + token + '}}'
                    before = snapshot(project)
                    with patch.object(studio, "_write_tracked", wraps=studio._write_tracked) as writes:
                        with self.assertRaises(studio.ReviewStudioError):
                            project.collect_external_recheck(revision["revision_id"], request["critic"], raw, provider="provider", model="rechecker")
                    writes.assert_not_called()
                    self.assertEqual(snapshot(project), before)
            self.assertEqual(project.integrity_errors(), [])


if __name__ == "__main__":
    unittest.main()
