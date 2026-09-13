"""Faults at durable workflow boundaries must not strand an accepted project."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import argument_revision as revision
import argument_workbench as workbench
from argument_app import ProductApp, project_state
from test import test_argument_revision as fixtures

REPO = Path(__file__).resolve().parents[1]


class ResearchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ArgumentRevisionTests()

    def snapshot(self, root):
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*") if path.is_file() and path.name != ".mutation.lock"}

    def prepare(self, root, operation):
        project = self.fixture.project(root)
        if operation == "import_review_report":
            return project, lambda: revision.import_review_report(project, self.fixture.REPORT)
        if operation in {"prepare_atomization", "collect_atomization_result"}:
            report_id = revision.import_review_report(project, self.fixture.REPORT)
            if operation == "prepare_atomization":
                return project, lambda: revision.prepare_atomization(project, report_id)
            run = revision.prepare_atomization(project, report_id)
            record = json.loads((run / "record.json").read_bytes())
            response = {"schema_version": 1, "run_id": record["run_id"], "manuscript_version_id": "V1",
                        "source_sha256": record["source_sha256"], "findings": []}
            return project, lambda: revision.collect_atomization_result(project, json.dumps(response))
        if operation == "complete_without_revision":
            self.fixture.atomize_findings(project, [])
            return project, lambda: revision.complete_without_revision(project, reason="No issues found")
        self.fixture.atomize(project)
        if operation == "append_quick_finding_decision":
            return project, lambda: revision.append_quick_finding_decision(project, "F1", decision="accept", reason="Confirmed")
        if operation == "prepare_revision_generation":
            revision.append_quick_finding_decision(project, "F1", decision="accept", reason="Confirmed")
            return project, lambda: revision.prepare_revision_generation(project)
        _, _, response = self.fixture.proposal(project)
        if operation == "collect_revision_result":
            return project, lambda: revision.collect_revision_result(project, json.dumps(response))
        if operation == "append_hunk_decision":
            return project, lambda: revision.append_hunk_decision(project, "CH1", decision="regenerate", reason="Try clearer wording")
        revision.append_hunk_decision(project, "CH1", decision="accept", reason="Approved exact change")
        if operation == "apply_approved_hunks":
            return project, lambda: revision.apply_approved_hunks(project)
        revision.apply_approved_hunks(project)
        if operation == "prepare_resolution_review":
            return project, lambda: revision.prepare_resolution_review(project)
        run = revision.prepare_resolution_review(project)
        record = json.loads((run / "record.json").read_bytes())
        response = {"schema_version": 1, "resolution_run_id": record["resolution_run_id"], "manuscript_version_id": "V2",
                    "source_sha256": record["source_sha256"], "results": [{"finding_id": "F1", "proposed_status": "resolved",
                    "reason": "Scope stated", "evidence_quotes": ["The bridge works in dry weather."], "uncertainties": []}]}
        if operation == "collect_resolution_result":
            return project, lambda: revision.collect_resolution_result(project, json.dumps(response))
        revision.collect_resolution_result(project, json.dumps(response))
        if operation == "append_resolution_decision":
            return project, lambda: revision.append_resolution_decision(project, "F1", status="resolved", reason="Verified wording")
        revision.append_resolution_decision(project, "F1", status="resolved", reason="Verified wording")
        if operation == "export_revision":
            return project, lambda: revision.export_revision(project)
        raise AssertionError(operation)

    def assert_usable(self, project):
        self.assertEqual(workbench.verify_project_versions(project), [])
        self.assertEqual(revision.verify_revision_workflow(project), [])
        self.assertNotEqual(project_state(project)["stage"], "read_only")

    def test_each_multifile_service_rolls_back_a_late_disk_failure(self):
        cases = (
            ("import_review_report", "_write_new", lambda p: p.name == "record.json"),
            ("prepare_atomization", "_write_new", lambda p: p.name == "record.json"),
            ("collect_atomization_result", "_write_new", lambda p: p.name == "findings.json"),
            ("append_quick_finding_decision", "_write_new", lambda p: p.parent.name == ".integrity" and p.name.startswith("FD")),
            ("prepare_revision_generation", "_write_new", lambda p: p.name == "record.json"),
            ("collect_revision_result", "_write_new", lambda p: p.name == "revision-patch-proposal.json"),
            ("append_hunk_decision", "_write_new", lambda p: p.parent.name == ".integrity"),
            ("apply_approved_hunks", "_write_new", lambda p: p.parent.name == ".integrity" and p.name.startswith("AP")),
            ("prepare_resolution_review", "_write_new", lambda p: p.name == "record.json"),
            ("collect_resolution_result", "_write_new", lambda p: p.name == "resolution-proposals.json"),
            ("append_resolution_decision", "_write_new", lambda p: p.parent.name == ".integrity"),
            ("complete_without_revision", "_write_new", lambda p: p.name == "audit.md"),
            ("export_revision", "_atomic_write", lambda p: p.name == "audit.md"),
        )
        for operation, writer, matches in cases:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp:
                project, perform = self.prepare(Path(temp), operation)
                before, stage = self.snapshot(project), project_state(project)["stage"]
                original = getattr(revision, writer)
                injected = []

                def fail_after_write(path, data):
                    original(path, data)
                    if matches(Path(path)):
                        injected.append(str(path))
                        raise OSError("simulated disk failure after publication")

                with patch.object(revision, writer, side_effect=fail_after_write):
                    with self.assertRaises(OSError):
                        perform()
                self.assertTrue(injected)
                self.assertEqual(self.snapshot(project), before)
                self.assertEqual(project_state(project)["stage"], stage)
                self.assert_usable(project)
                perform()
                self.assert_usable(project)

    def test_model_rejections_commit_complete_audit_archives_in_product_actions(self):
        for operation, action in (("collect_atomization_result", "collect_atomization"),
                                  ("collect_revision_result", "collect_revision"),
                                  ("collect_resolution_result", "collect_resolution")):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp:
                project, _ = self.prepare(Path(temp), operation)
                app = ProductApp.create(project.parent, project)
                before = self.snapshot(project)
                app.act({"action": action, "data": {"response": "not JSON: original response"}})
                added = set(self.snapshot(project)) - set(before)
                response = next(project / name for name in added if name.endswith("response.json"))
                self.assertEqual(response.read_bytes(), b"not JSON: original response")
                attempt = json.loads(response.with_name("record.json").read_bytes())
                self.assertFalse(attempt["valid"])
                self.assertTrue(response.with_name("repair-prompt.md").is_file())
                self.assert_usable(project)

    def test_rejected_archive_write_failure_rolls_back_and_raw_bytes_can_be_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.prepare(Path(temp), "collect_atomization_result")
            raw = b"\xff\xfeinvalid original response"
            before = self.snapshot(project)
            original = revision._write_new

            def fail(path, data):
                if Path(path).name == "repair-prompt.md":
                    raise OSError("repair archive disk failure")
                original(path, data)

            with patch.object(revision, "_write_new", side_effect=fail):
                with self.assertRaises(OSError):
                    revision.collect_atomization_result(project, raw)
            self.assertEqual(self.snapshot(project), before)
            result = revision.collect_atomization_result(project, raw)
            self.assertFalse(result.valid)
            self.assertEqual(result.response.read_bytes(), raw)
            self.assert_usable(project)

    def test_malformed_model_field_types_are_auditable_rejections(self):
        cases = (("collect_atomization_result", "findings", "location_kind", []),
                 ("collect_atomization_result", "findings", "evidence_level", {}),
                 ("collect_revision_result", "changes", "change_kind", []),
                 ("collect_revision_result", "changes", "original_quote", 7),
                 ("collect_resolution_result", "results", "finding_id", []),
                 ("collect_resolution_result", "results", "proposed_status", {}))
        for operation, collection, field, value in cases:
            with self.subTest(operation=operation, field=field), tempfile.TemporaryDirectory() as temp:
                project, perform = self.prepare(Path(temp), operation)
                if collection == "findings":
                    self.fixture.atomize(project)
                    response_path = revision._latest_valid_findings(project)[1].with_name("response.json")
                else:
                    result = perform()
                    response_path = result.response
                raw = json.loads(response_path.read_bytes())
                raw[collection][0][field] = value
                collector = getattr(revision, operation)
                result = collector(project, json.dumps(raw))
                self.assertFalse(result.valid)
                self.assertEqual(json.loads(result.response.read_bytes()), raw)
                self.assertTrue(result.repair_prompt.is_file())
                self.assert_usable(project)

    def test_process_crash_during_collection_and_after_v2_recovers_on_open(self):
        for operation, match in (("collect_atomization_result", "path.name == 'findings.json'"),
                                 ("apply_approved_hunks", "path.parent.name == '.integrity' and path.name.startswith('AP')")):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp:
                project, perform = self.prepare(Path(temp), operation)
                before = self.snapshot(project)
                if operation == "collect_atomization_result":
                    run = revision._latest_dir(revision._quick_root(project) / "atomization-runs", "AR")
                    record = json.loads((run / "record.json").read_bytes())
                    payload = {"schema_version": 1, "run_id": record["run_id"], "manuscript_version_id": "V1",
                               "source_sha256": record["source_sha256"], "findings": []}
                    invoke = "r.collect_atomization_result(project, sys.argv[2])"
                else:
                    payload = None
                    invoke = "r.apply_approved_hunks(project)"
                code = ("import os,sys\nfrom pathlib import Path\nimport argument_revision as r\n"
                        "project=Path(sys.argv[1])\noriginal=r._write_new\n"
                        "def crash(path,data):\n original(path,data)\n"
                        f" if {match}: os._exit(83)\n"
                        "r._write_new=crash\n" + invoke)
                child = subprocess.run([sys.executable, "-c", code, str(project), json.dumps(payload)],
                                       cwd=REPO, capture_output=True, timeout=30)
                self.assertEqual(child.returncode, 83, child.stderr)
                ProductApp.create(project.parent, project).view()
                self.assertEqual(self.snapshot(project), before)
                self.assert_usable(project)
                perform()
                self.assert_usable(project)

    def test_direct_service_rejects_another_process_writer_without_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            project, perform = self.prepare(Path(temp), "import_review_report")
            before = self.snapshot(project)
            code = ("import sys\nfrom project_lock import project_mutation_lock\n"
                    "with project_mutation_lock(sys.argv[1]):\n"
                    " print('locked',flush=True)\n sys.stdin.read(1)\n")
            child = subprocess.Popen([sys.executable, "-c", code, str(project)], cwd=REPO,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                with self.assertRaisesRegex(workbench.WorkbenchError, "another local process"):
                    perform()
                self.assertEqual(self.snapshot(project), before)
            finally:
                child.communicate("x", timeout=10)
            self.assertEqual(child.returncode, 0)
            perform()
            self.assert_usable(project)

    def test_explicit_run_cannot_write_another_project_outside_its_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            first, _ = self.prepare(Path(temp) / "first", "collect_atomization_result")
            second, _ = self.prepare(Path(temp) / "second", "collect_atomization_result")
            other_run = revision._latest_dir(revision._quick_root(second) / "atomization-runs", "AR")
            first_before, second_before = self.snapshot(first), self.snapshot(second)
            with self.assertRaisesRegex(workbench.WorkbenchError, "run_id"):
                revision.collect_atomization_result(first, "invalid raw response", run_id=str(other_run))
            self.assertEqual(self.snapshot(first), first_before)
            self.assertEqual(self.snapshot(second), second_before)

    def test_explicit_artifact_selectors_reject_paths_and_wrong_types(self):
        cases = (("prepare_atomization", "report_id"), ("collect_atomization_result", "run_id"),
                 ("collect_revision_result", "run_id"), ("collect_resolution_result", "run_id"),
                 ("prepare_resolution_review", "application_id"), ("export_revision", "application_id"))
        with tempfile.TemporaryDirectory() as temp:
            project = self.fixture.project(Path(temp))
            before = self.snapshot(project)
            for name, field in cases:
                for value in ("../foreign", "", [], True):
                    with self.subTest(service=name, value=value):
                        args = (project, "raw response") if name.startswith("collect_") else (project,)
                        with self.assertRaisesRegex(workbench.WorkbenchError, field):
                            getattr(revision, name)(*args, **{field: value})
                        self.assertEqual(self.snapshot(project), before)


if __name__ == "__main__":
    unittest.main()
