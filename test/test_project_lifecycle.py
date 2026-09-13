"""Failure boundaries for project transactions and untrusted recovery archives."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import project_lifecycle as lifecycle


def write(path, data):
    lifecycle.before_write(path)
    lifecycle._atomic(path, data)


class ProjectLifecycleTests(unittest.TestCase):
    def test_explicit_completed_validation_archive_is_committed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            with self.assertRaises(lifecycle.CommitValidationResult):
                with lifecycle.transaction(root):
                    write(root / "rejected-response.txt", b"invalid response for audit")
                    raise lifecycle.CommitValidationResult("response rejected and archived")
            self.assertEqual((root / "rejected-response.txt").read_bytes(), b"invalid response for audit")
            self.assertFalse((root / lifecycle.JOURNAL).exists())

    def test_validation_archive_cannot_commit_after_nested_partial_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            with self.assertRaises(ValueError) as error:
                with lifecycle.transaction(root):
                    try:
                        with lifecycle.transaction(root):
                            write(root / "partial.txt", b"partial")
                            raise KeyError("missing")
                    except KeyError:
                        pass
                    raise lifecycle.CommitValidationResult("cannot waive nested failure")
            self.assertNotIsInstance(error.exception, lifecycle.CommitValidationResult)
            self.assertFalse((root / "partial.txt").exists())

    def test_ordinary_exceptions_rollback_every_file(self):
        for error in (ValueError("bad data"), KeyError("missing field"), TypeError("bad shape")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                existing = root / "original.txt"
                existing.write_bytes(b"before")
                with self.assertRaises(type(error)):
                    with lifecycle.transaction(root):
                        write(existing, b"after")
                        write(root / "partial" / "new.txt", b"partial")
                        raise error
                self.assertEqual(existing.read_bytes(), b"before")
                self.assertFalse((root / "partial" / "new.txt").exists())
                self.assertFalse((root / lifecycle.JOURNAL).exists())

    def test_caught_nested_error_still_rolls_back_outer_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            existing = root / "original.txt"
            existing.write_bytes(b"before")
            with self.assertRaises(ValueError):
                with lifecycle.transaction(root):
                    try:
                        with lifecycle.transaction(root):
                            write(existing, b"partial")
                            raise ValueError("nested mutation failed")
                    except ValueError:
                        pass
                    write(root / "later.txt", b"later")
            self.assertEqual(existing.read_bytes(), b"before")
            self.assertFalse((root / "later.txt").exists())

    def test_commit_marker_failure_rolls_back_before_returning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            existing = root / "original.txt"
            existing.write_bytes(b"before")
            atomic = lifecycle._atomic

            def fail_commit(path, data):
                if path.name == "committed" and data == b"committed\n":
                    raise OSError("disk full at commit")
                return atomic(path, data)

            with patch.object(lifecycle, "_atomic", side_effect=fail_commit):
                with self.assertRaises(OSError):
                    with lifecycle.transaction(root):
                        write(existing, b"after")
            self.assertEqual(existing.read_bytes(), b"before")
            self.assertFalse((root / lifecycle.JOURNAL).exists())

    def test_caught_journal_write_failure_cannot_skip_next_before_image(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            original = root / "original.txt"
            original.write_bytes(b"before")
            atomic = lifecycle._atomic
            rejected = False

            def reject_first_undo_manifest(path, data):
                nonlocal rejected
                if path.name == "manifest.json" and b'"original.txt": {' in data and not rejected:
                    rejected = True
                    raise OSError("temporary journal failure")
                return atomic(path, data)

            with self.assertRaisesRegex(OSError, "later failure"):
                with lifecycle.transaction(root):
                    with patch.object(lifecycle, "_atomic", side_effect=reject_first_undo_manifest):
                        try:
                            write(original, b"first attempt")
                        except OSError:
                            pass
                        write(original, b"second attempt")
                    raise OSError("later failure")
            self.assertTrue(rejected)
            self.assertEqual(original.read_bytes(), b"before")

    def test_recovery_preserves_original_empty_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "user-empty-directory").mkdir()
            with self.assertRaises(OSError):
                with lifecycle.transaction(root):
                    write(root / "partial" / "new.txt", b"partial")
                    raise OSError("disk full")
            self.assertTrue((root / "user-empty-directory").is_dir())
            self.assertFalse((root / "partial").exists())

    def test_backup_manifest_shapes_fail_cleanly_without_publishing(self):
        invalid = [[], None, 1, {"version": 1, "files": {"project.json": None}}]
        for manifest in invalid:
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                archive = root / "bad.zip"
                with zipfile.ZipFile(archive, "w") as target:
                    target.writestr("backup.json", json.dumps(manifest))
                    if isinstance(manifest, dict):
                        target.writestr("project/project.json", "{}")
                with self.assertRaises(ValueError):
                    lifecycle.restore_backup(archive, root / "library")
                self.assertEqual([p.name for p in (root / "library").iterdir() if p.name != ".mutation.lock"], [])

    def test_recovery_manifest_shapes_fail_closed(self):
        invalid = [[], None, {"version": 1, "files": ["original.txt"], "undo": {"original.txt": None}},
                   {"version": 1, "files": ["original.txt"], "undo": {"original.txt": {"name": "0"}}},
                   {"version": 1, "files": [".mutation.lock"], "undo": {}}]
        for manifest in invalid:
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "original.txt").write_bytes(b"keep")
                journal = root / lifecycle.JOURNAL
                (journal / "before").mkdir(parents=True)
                (journal / "before" / "0").write_bytes(b"old")
                (journal / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(ValueError):
                    lifecycle.recover(root)
                self.assertEqual((root / "original.txt").read_bytes(), b"keep")
                self.assertTrue(journal.exists())

    def test_before_write_rejects_noncanonical_path_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            with self.assertRaises(ValueError):
                with lifecycle.transaction(root):
                    write(root / "safe" / ".." / "unintended.txt", b"unexpected")
            self.assertFalse((root / "unintended.txt").exists())

    def test_control_characters_are_rejected_in_backup_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            for relative in ("bad\0name", "bad\nname", "bad\tname", "bad?name", "bad*name"):
                with self.subTest(relative=relative), self.assertRaises(ValueError):
                    lifecycle._child(Path(temp), relative)

    def test_recovery_validates_all_undo_bytes_before_first_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            journal = root / lifecycle.JOURNAL
            (journal / "before").mkdir(parents=True)
            original = {"first.txt": b"old first", "second.txt": b"old second"}
            undo = {}
            for index, (name, data) in enumerate(original.items()):
                (root / name).write_bytes(b"current")
                (journal / "before" / str(index)).write_bytes(data if index == 0 else b"corrupt")
                undo[name] = {"name": str(index), "sha256": hashlib.sha256(data).hexdigest()}
            (journal / "manifest.json").write_text(json.dumps({"version": 1, "files": list(original), "undo": undo}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "校验"):
                lifecycle.recover(root)
            self.assertTrue(all((root / name).read_bytes() == b"current" for name in original))

    def test_recovery_rejects_invalid_commit_markers_without_losing_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            journal = root / lifecycle.JOURNAL
            journal.mkdir()
            (journal / "committed").write_bytes(b"incomplete marker")
            (root / "document.txt").write_bytes(b"keep")
            with self.assertRaises(ValueError):
                lifecycle.recover(root)
            self.assertEqual((root / "document.txt").read_bytes(), b"keep")
            self.assertTrue(journal.exists())

    def test_duplicate_json_fields_are_rejected_without_removing_project_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            journal = root / lifecycle.JOURNAL
            journal.mkdir()
            (root / "document.txt").write_bytes(b"keep")
            (journal / "manifest.json").write_bytes(b'{"version":1,"files":["document.txt"],"files":[],"undo":{}}')
            with self.assertRaises(ValueError):
                lifecycle.recover(root)
            self.assertEqual((root / "document.txt").read_bytes(), b"keep")

    def test_crashed_recovery_can_be_retried_until_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "a.txt").write_bytes(b"before a")
            (root / "b.txt").write_bytes(b"before b")
            code = (
                "import os, sys\nfrom pathlib import Path\nimport project_lifecycle as p\n"
                "root = Path(sys.argv[1])\nwith p.transaction(root):\n"
                " for name in ['a.txt', 'b.txt', 'created.txt']:\n"
                "  path = root / name\n  p.before_write(path)\n  p._atomic(path, b'changed')\n"
                " os._exit(73)\n"
            )
            result = subprocess.run([sys.executable, "-c", code, str(root)], cwd=Path(__file__).resolve().parents[1], timeout=20)
            self.assertEqual(result.returncode, 73)
            atomic = lifecycle._atomic

            def fail_second_restore(path, data):
                if path == root / "b.txt":
                    raise OSError("disk still full")
                return atomic(path, data)

            with patch.object(lifecycle, "_atomic", side_effect=fail_second_restore), self.assertRaises(OSError):
                lifecycle.recover(root)
            self.assertTrue((root / lifecycle.JOURNAL / "manifest.json").exists())
            self.assertTrue(lifecycle.recover(root))
            self.assertEqual((root / "a.txt").read_bytes(), b"before a")
            self.assertEqual((root / "b.txt").read_bytes(), b"before b")
            self.assertFalse((root / "created.txt").exists())
            self.assertFalse(lifecycle.recover(root))

    def test_linked_root_is_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            real = root / "real"
            real.mkdir()
            link = root / "linked"
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            try:
                with self.assertRaises(ValueError):
                    with lifecycle.transaction(link):
                        self.fail("linked project must not enter mutation")
                self.assertEqual(list(real.iterdir()), [])
            finally:
                link.unlink()

    def test_linked_journal_metadata_is_rejected_before_reading(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            journal = project / lifecycle.JOURNAL
            journal.mkdir(parents=True)
            (project / "document.txt").write_bytes(b"keep")
            outside = root / "outside.txt"
            outside.write_bytes(b"committed\n")
            link = journal / "committed"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            try:
                with self.assertRaises(ValueError):
                    lifecycle.recover(project)
                self.assertTrue(journal.exists())
                self.assertEqual(outside.read_bytes(), b"committed\n")
                self.assertEqual((project / "document.txt").read_bytes(), b"keep")
            finally:
                link.unlink()

    def test_backup_does_not_export_uncommitted_state(self):
        from document_review_studio import DocumentReviewProject
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            project = DocumentReviewProject.create(root / "library", filename="draft.md", content=b"A document.")
            with lifecycle.transaction(project.root):
                write(project.root / ".ui-draft.json", b'{"unfinished":true}')
                with self.assertRaises(ValueError):
                    lifecycle.create_backup(project.root, root / "uncommitted.zip")
            self.assertFalse((root / "uncommitted.zip").exists())

    def test_backup_at_project_size_limit_can_be_restored(self):
        from document_review_studio import DocumentReviewProject
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            project = DocumentReviewProject.create(root / "library", filename="draft.md", content=b"A document.")
            size = sum(path.stat().st_size for path in lifecycle._files(project.root).values())
            with patch.object(lifecycle, "MAX_BACKUP_BYTES", size):
                archive = lifecycle.create_backup(project.root, root / "backup.zip")
                restored = lifecycle.restore_backup(archive, root / "destination")
            self.assertEqual(DocumentReviewProject(restored).integrity_errors(), [])

    @unittest.skipUnless(os.name == "nt", "Windows directory rename semantics")
    def test_restore_does_not_replace_concurrently_created_directory(self):
        from document_review_studio import DocumentReviewProject
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            project = DocumentReviewProject.create(root / "library", filename="draft.md", content=b"A document.")
            archive = lifecycle.create_backup(project.root, root / "backup.zip")
            rename, replace = os.rename, os.replace
            collision = None

            def publish(original):
                def race(source, target):
                    nonlocal collision
                    if Path(source).name.startswith(".restore-") and collision is None:
                        collision = Path(target)
                        collision.mkdir()
                    return original(source, target)
                return race

            with patch.object(lifecycle.os, "rename", side_effect=publish(rename)), patch.object(lifecycle.os, "replace", side_effect=publish(replace)):
                restored = lifecycle.restore_backup(archive, root / "destination")
            self.assertIsNotNone(collision)
            self.assertNotEqual(restored, collision)
            self.assertEqual(list(collision.iterdir()), [])
            self.assertEqual(DocumentReviewProject(restored).integrity_errors(), [])


if __name__ == "__main__":
    unittest.main()
