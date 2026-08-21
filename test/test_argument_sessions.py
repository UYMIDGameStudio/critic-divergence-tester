from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import argument_contracts as contracts  # noqa: E402
import argument_sessions as sessions  # noqa: E402
import argument_workbench as workbench  # noqa: E402
import critic_runner  # noqa: E402


FIXTURE = REPO_ROOT / "test" / "fixtures" / "workbench-demo"


class GateAWorkSessionTests(unittest.TestCase):
    def make_project(self, root: Path) -> workbench.WorkspacePaths:
        source = root / "real manuscript.md"
        source.write_bytes((FIXTURE / "manuscript.md").read_bytes())
        return workbench.initialize_workspace(
            source,
            root / "session demo.argument-workbench",
            title="Session timing demo",
        )

    def test_session_start_finish_and_workspace_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            paths = sessions.start_work_session(
                project.root,
                activity="ir-inspection",
                note="Inspect every Claim against the source.",
                producer="test-human",
            )
            self.assertEqual(paths.session_id, "GS1")
            start = json.loads(paths.start.read_text(encoding="utf-8"))
            self.assertEqual(start["artifact"], "gate-a-session-start")
            self.assertEqual(start["provenance"]["origin"], "human-confirmed")
            self.assertEqual(contracts.validate_artifact(start), [])
            self.assertEqual(sessions.verify_work_sessions(project.root), [])
            self.assertEqual(workbench.verify_workspace(project.root), [])
            with self.assertRaisesRegex(workbench.WorkbenchError, "finish open session"):
                sessions.start_work_session(
                    project.root, activity="finding-adjudication"
                )

            sessions.finish_work_session(
                project.root, "GS1", producer="test-human"
            )
            record = json.loads(paths.record.read_text(encoding="utf-8"))
            self.assertEqual(record["artifact"], "gate-a-work-session")
            self.assertGreaterEqual(record["timing"]["elapsed_milliseconds"], 0)
            self.assertEqual(
                record["parents"][1]["sha256"],
                contracts.sha256_bytes(paths.start.read_bytes()),
            )
            self.assertEqual(sessions.verify_work_sessions(project.root), [])
            self.assertEqual(workbench.verify_workspace(project.root), [])
            with self.assertRaisesRegex(workbench.WorkbenchError, "already complete"):
                sessions.finish_work_session(project.root, "GS1")

    def test_cli_lists_open_and_completed_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "gate-a",
                            "session",
                            "start",
                            str(project.root),
                            "--activity",
                            "status-triage",
                            "--note",
                            "Review routing outcomes.",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "gate-a",
                            "session",
                            "list",
                            str(project.root),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "gate-a",
                            "session",
                            "finish",
                            str(project.root),
                            "GS1",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "gate-a",
                            "session",
                            "list",
                            str(project.root),
                        ]
                    ),
                    0,
                )
            rendered = stdout.getvalue()
            self.assertIn("open since", rendered)
            self.assertIn("complete", rendered)
            self.assertIn("status-triage", rendered)

    def test_abandoned_session_is_closed_but_not_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            paths = sessions.start_work_session(
                project.root,
                activity="ir-inspection",
                note="The author did not begin inspection.",
                producer="test-human",
            )
            sessions.abandon_work_session(
                project.root,
                "GS1",
                reason="The user left before making a human judgment.",
                producer="test-human",
            )
            record = json.loads(paths.record.read_text(encoding="utf-8"))
            self.assertEqual(record["artifact"], "gate-a-session-abandonment")
            self.assertEqual(record["provenance"]["origin"], "human-confirmed")
            self.assertGreaterEqual(record["timing"]["elapsed_milliseconds"], 0)
            self.assertEqual(contracts.validate_artifact(record), [])
            rendered = sessions.render_work_sessions(
                sessions.list_work_sessions(project.root)
            )
            self.assertIn("abandoned", rendered)
            self.assertIn("did not begin inspection", rendered)
            self.assertEqual(sessions.verify_work_sessions(project.root), [])
            self.assertEqual(workbench.verify_workspace(project.root), [])
            second = sessions.start_work_session(
                project.root, activity="ir-inspection"
            )
            self.assertEqual(second.session_id, "GS2")
            with self.assertRaisesRegex(workbench.WorkbenchError, "already closed"):
                sessions.abandon_work_session(
                    project.root,
                    "GS1",
                    reason="Cannot abandon twice.",
                )

    def test_cli_can_abandon_an_open_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            sessions.start_work_session(project.root, activity="ir-inspection")
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "gate-a",
                            "session",
                            "abandon",
                            str(project.root),
                            "GS1",
                            "--reason",
                            "No human judgment was made.",
                        ]
                    ),
                    0,
                )
            self.assertIn("will not count", stdout.getvalue())
            self.assertEqual(workbench.verify_workspace(project.root), [])

    def test_tampering_and_noncontinuous_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            paths = sessions.start_work_session(
                project.root, activity="revision-planning"
            )
            sessions.finish_work_session(project.root, "GS1")
            record = json.loads(paths.record.read_text(encoding="utf-8"))
            record["note"] = "tampered"
            paths.record.write_text(
                json.dumps(record, ensure_ascii=False), encoding="utf-8"
            )
            errors = sessions.verify_work_sessions(project.root)
            self.assertTrue(any("differs from session start" in error for error in errors))

            second = paths.sessions_dir / "GS3"
            second.mkdir()
            (second / "start.json").write_bytes(paths.start.read_bytes())
            with self.assertRaisesRegex(workbench.WorkbenchError, "continuous"):
                sessions.list_work_sessions(project.root)


if __name__ == "__main__":
    unittest.main()
