"""Professional human decisions keep their durable boundaries on failure."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import argument_adjudication as adjudication
import argument_workbench as workbench
from test import test_argument_adjudication as fixtures

REPO = Path(__file__).resolve().parents[1]


class AdjudicationRecoveryTests(unittest.TestCase):
    def snapshot(self, root):
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*") if path.is_file() and path.name != ".mutation.lock"}

    def prepare(self, root, *, bundle=False):
        fixture = fixtures.ArgumentAdjudicationTests()
        workspace, _ = fixture.make_reviewed_project(root, two_findings_on_c1=bundle)
        return workspace, fixture.finding_ids(workspace)[0]

    def decide(self, root, finding_id):
        return adjudication.append_finding_decision(
            root, finding_id, decision="accept", reason="Human checked the scope",
            actions=[("narrow_claim", "Limit the claim to the inspected cases")])

    def test_individual_decision_and_plan_rebuild_roll_back_late_disk_failure(self):
        for operation in ("decision", "plan"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temporary:
                workspace, finding_id = self.prepare(Path(temporary))
                before = self.snapshot(workspace.root)
                writer = "_write_new" if operation == "decision" else "_atomic_write"
                original = getattr(adjudication, writer)
                injected = []

                def fail(path, data):
                    original(path, data)
                    if Path(path).name == ("RA0001.json" if operation == "decision" else "record.json"):
                        injected.append(path)
                        raise OSError("disk failed after publication")

                def perform():
                    return self.decide(workspace.root, finding_id) if operation == "decision" else adjudication.rebuild_revision_plan(workspace)

                with patch.object(adjudication, writer, side_effect=fail):
                    with self.assertRaises(OSError):
                        perform()
                self.assertTrue(injected)
                self.assertEqual(self.snapshot(workspace.root), before)
                self.assertEqual(workbench.verify_workspace(workspace), [])
                perform()
                self.assertEqual(workbench.verify_workspace(workspace), [])

    def test_bundle_failure_keeps_only_fully_completed_decisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace, _ = self.prepare(Path(temporary), bundle=True)
            original_write, original_decide = adjudication._write_new, adjudication.append_finding_decision
            completed = []

            def fail(path, data):
                original_write(path, data)
                if Path(path).name == "RA0002.json":
                    raise OSError("second decision disk failure")

            def capture(*args, **kwargs):
                result = original_decide(*args, **kwargs)
                completed.append(self.snapshot(workspace.root))
                return result

            with patch.object(adjudication, "_write_new", side_effect=fail), patch.object(adjudication, "append_finding_decision", side_effect=capture):
                with self.assertRaisesRegex(workbench.WorkbenchError, "after 1 append-only decisions"):
                    adjudication.append_claim_bundle_decisions(
                        workspace.root, claim="C1", decision="accept", reason="Human checked the scope",
                        actions=[("narrow_claim", "Limit the claim")], confirm_count=2)
            self.assertEqual(len(completed), 1)
            self.assertEqual(self.snapshot(workspace.root), completed[0])
            self.assertEqual(workbench.verify_workspace(workspace), [])
            remaining = adjudication.append_claim_bundle_decisions(
                workspace.root, claim="C1", decision="accept", reason="Human checked the scope",
                actions=[("narrow_claim", "Limit the claim")], confirm_count=1)
            self.assertEqual(len(remaining), 1)
            self.assertEqual(workbench.verify_workspace(workspace), [])

    def test_real_process_crash_recovers_individual_and_partial_bundle(self):
        for bundle in (False, True):
            with self.subTest(bundle=bundle), tempfile.TemporaryDirectory() as temporary:
                workspace, finding_id = self.prepare(Path(temporary), bundle=bundle)
                before = self.snapshot(workspace.root)
                invoke = ("a.append_claim_bundle_decisions(root,claim='C1',decision='accept',reason='Checked',actions=[('narrow_claim','Limit scope')],confirm_count=2)"
                          if bundle else "a.append_finding_decision(root,sys.argv[2],decision='accept',reason='Checked',actions=[('narrow_claim','Limit scope')])")
                code = ("import os,sys\nfrom pathlib import Path\nimport argument_adjudication as a\n"
                        "root=Path(sys.argv[1])\noriginal=a._write_new\n"
                        "def crash(path,data):\n original(path,data)\n"
                        f" if path.name == '{'RA0002.json' if bundle else 'RA0001.json'}': os._exit(83)\n"
                        "a._write_new=crash\n" + invoke)
                child = subprocess.run([sys.executable, "-c", code, str(workspace.root), finding_id],
                                       cwd=REPO, capture_output=True, timeout=30)
                self.assertEqual(child.returncode, 83, child.stderr)
                workbench.workspace_paths(workspace.root)
                self.assertEqual(workbench.verify_workspace(workspace), [])
                paths = adjudication.human_review_paths(workspace.root)
                if bundle:
                    self.assertEqual(len(adjudication.list_adjudications(paths)), 1)
                    self.assertEqual(len(adjudication.list_revision_actions(paths)), 1)
                    plan = json.loads(paths.plan_record.read_bytes())
                    self.assertEqual(plan["summary"]["accept"], 1)
                    self.assertEqual(len(adjudication.open_finding_entries(workspace.root, claim="V1:C1")), 1)
                else:
                    self.assertEqual(self.snapshot(workspace.root), before)
                    self.decide(workspace.root, finding_id)
                    self.assertEqual(workbench.verify_workspace(workspace), [])

    def test_bundle_is_locked_before_inspecting_its_confirmation_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace, _ = self.prepare(Path(temporary), bundle=True)
            before = self.snapshot(workspace.root)
            code = ("import sys\nfrom project_lock import project_mutation_lock\n"
                    "with project_mutation_lock(sys.argv[1]):\n"
                    " print('locked',flush=True)\n sys.stdin.read(1)\n")
            child = subprocess.Popen([sys.executable, "-c", code, str(workspace.root)], cwd=REPO,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                with self.assertRaisesRegex(workbench.WorkbenchError, "another local process"):
                    adjudication.append_claim_bundle_decisions(
                        workspace.root, claim="C1", decision="reject", reason="Checked", actions=[], confirm_count=2)
                self.assertEqual(self.snapshot(workspace.root), before)
            finally:
                child.communicate("x", timeout=10)
            self.assertEqual(child.returncode, 0)
            self.assertEqual(workbench.verify_workspace(workspace), [])


if __name__ == "__main__":
    unittest.main()
