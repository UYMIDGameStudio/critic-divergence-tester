"""Historical projects remain readable without weakening current history checks."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import argument_workbench as w


REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "test/fixtures/workbench-demo"
OLD_TIME = "2026-08-11T07:26:16+00:00"


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and p.name != ".mutation.lock"}


def legacy_project(root, *, versions=1, created_at=OLD_TIME):
    """Reconstruct the previous exact source shape before any IR is archived."""
    with patch.object(w, "utc_now", return_value=created_at):
        paths = w.initialize_workspace(FIXTURE / "manuscript.md", root / "project")
        if versions == 2:
            second = root / "second.md"
            second.write_bytes((FIXTURE / "manuscript.md").read_bytes() + b"\nA second version.\n")
            w.import_document_version(paths, second)
    previous_bytes = None
    for version_id in w.list_version_ids(paths):
        version_path = paths.versions_dir / version_id / "document-version.json"
        record = json.loads(version_path.read_bytes())
        record["source"].pop("decoding")
        if previous_bytes is not None:
            for parent in record["parents"]:
                if parent["role"] == "parent-version":
                    parent["sha256"] = w.sha256_bytes(previous_bytes)
        previous_bytes = w.json_bytes(record)
        version_path.write_bytes(previous_bytes)
    paths.history_index.unlink()
    return paths


def next_manuscript(root, number=2):
    path = root / f"next-{number}.md"
    path.write_bytes((FIXTURE / "manuscript.md").read_bytes() + f"\nNew version {number}.\n".encode())
    return path


class LegacyHistoryCompatibilityTests(unittest.TestCase):
    def test_one_and_two_version_legacy_verification_is_read_only(self):
        for count in (1, 2):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temp:
                paths = legacy_project(Path(temp), versions=count)
                before = snapshot(paths.root)
                self.assertEqual(w.verify_project_versions(paths), [])
                self.assertEqual(snapshot(paths.root), before)
                self.assertFalse(paths.history_index.exists())

    def test_deleted_index_from_current_project_is_not_legacy(self):
        for timestamp in (None, OLD_TIME):
            with self.subTest(timestamp=timestamp), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                if timestamp is None:
                    paths = w.initialize_workspace(FIXTURE / "manuscript.md", root / "project")
                else:
                    with patch.object(w, "utc_now", return_value=timestamp):
                        paths = w.initialize_workspace(FIXTURE / "manuscript.md", root / "project")
                paths.history_index.unlink()
                self.assertTrue(w.verify_project_versions(paths))
                with self.assertRaisesRegex(w.WorkbenchError, "history"):
                    w.import_document_version(paths, next_manuscript(root))
                self.assertFalse(paths.history_index.exists())

    def test_index_era_project_without_decoding_is_not_legacy(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = legacy_project(Path(temp), created_at="2026-08-26T00:00:00+00:00")
            errors = w.verify_project_versions(paths)
            self.assertTrue(any("historical project creation" in error for error in errors))

    def test_legacy_source_and_parent_hash_errors_still_block(self):
        for target in ("source", "parent"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                paths = legacy_project(root, versions=2)
                if target == "source":
                    record = json.loads(paths.version.read_bytes())
                    (paths.version_dir / record["source"]["relative_path"]).write_bytes(b"changed original")
                else:
                    record_path = paths.versions_dir / "V2/document-version.json"
                    record = json.loads(record_path.read_bytes())
                    record["parents"][1]["sha256"] = "0" * 64
                    record_path.write_bytes(w.json_bytes(record))
                before = snapshot(paths.root)
                self.assertTrue(w.verify_project_versions(paths))
                with self.assertRaises(w.WorkbenchError):
                    w.import_document_version(paths, next_manuscript(root, 3))
                self.assertEqual(snapshot(paths.root), before)

    def test_legacy_raw_ir_and_reviewed_cache_still_require_existing_verifiers(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = legacy_project(Path(temp))
            w.collect_raw_attempt(paths, (FIXTURE / "raw-ir.json").read_bytes(), method="file",
                                  source_name="historical-response.json", producer_label="test")
            w.rebuild_workspace(paths)
            self.assertEqual(w.verify_project_versions(paths), [])
            paths.reviewed_payload.write_bytes(b"{}\n")
            errors = w.verify_project_versions(paths)
            self.assertTrue(any("reproducible" in error for error in errors))
            self.assertFalse(paths.history_index.exists())

    def test_existing_but_invalid_index_never_uses_legacy_fallback(self):
        for form in ("directory", "bad-json", "dangling-link"):
            with self.subTest(form=form), tempfile.TemporaryDirectory() as temp:
                paths = legacy_project(Path(temp))
                if form == "directory":
                    paths.history_index.mkdir()
                elif form == "bad-json":
                    paths.history_index.write_bytes(b"{")
                else:
                    try:
                        paths.history_index.symlink_to(paths.root / "missing-index")
                    except OSError:
                        continue
                self.assertTrue(w.verify_project_versions(paths))

    def test_first_append_records_migration_and_preserves_old_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = legacy_project(root, versions=2)
            before = snapshot(paths.root)
            self.assertEqual(w.import_document_version(paths, next_manuscript(root, 3)).version_id, "V3")
            self.assertEqual(w.verify_project_versions(paths), [])
            for name, data in before.items():
                self.assertEqual((paths.root / name).read_bytes(), data)
            migration_path = paths.root / w._HISTORY_MIGRATION_NAME
            receipt = json.loads(migration_path.read_bytes())
            index = json.loads(paths.history_index.read_bytes())
            self.assertEqual(receipt["legacy_version_ids"], ["V1", "V2"])
            self.assertEqual(index["migration_sha256"], w.sha256_bytes(migration_path.read_bytes()))
            prefix = w._derive_legacy_history_index(paths, ["V1", "V2"])
            self.assertEqual(receipt["legacy_prefix_sha256"], w.sha256_bytes(w.json_bytes(prefix)))
            self.assertEqual(index["entries"][:2], prefix["entries"])
            receipt_bytes = migration_path.read_bytes()
            w.import_document_version(paths, next_manuscript(root, 4))
            self.assertEqual(migration_path.read_bytes(), receipt_bytes)
            self.assertEqual(w.verify_project_versions(paths), [])

    def test_migrated_history_missing_index_or_receipt_or_tampering_is_rejected(self):
        for damage in ("index", "receipt", "receipt-directory", "receipt-data", "receipt-type", "prefix"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                paths = legacy_project(root)
                w.import_document_version(paths, next_manuscript(root))
                receipt_path = paths.root / w._HISTORY_MIGRATION_NAME
                if damage in {"index", "receipt", "receipt-directory"}:
                    (paths.history_index if damage == "index" else receipt_path).unlink()
                    if damage == "receipt-directory":
                        receipt_path.mkdir()
                elif damage.startswith("receipt"):
                    receipt = json.loads(receipt_path.read_bytes())
                    receipt["legacy_version_ids"] = {} if damage == "receipt-type" else ["V1", "V2"]
                    receipt_path.write_bytes(w.json_bytes(receipt))
                else:
                    index = json.loads(paths.history_index.read_bytes())
                    index["entries"][0]["created_at"] = "2026-08-12T00:00:00+00:00"
                    previous_hash = None
                    for entry in index["entries"]:
                        entry["previous_entry_sha256"] = previous_hash
                        entry["entry_sha256"] = w._history_entry_hash(entry)
                        previous_hash = entry["entry_sha256"]
                    index["head_sha256"] = previous_hash
                    paths.history_index.write_bytes(w.json_bytes(index))
                self.assertTrue(w.verify_project_versions(paths))

    def test_migration_write_failure_rolls_back_receipt_and_new_version(self):
        for stage in ("index", "append"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                paths = legacy_project(root)
                manuscript = next_manuscript(root)
                before = snapshot(paths.root)
                original = w._write_new
                def write(path, data):
                    if path.name == "project-history-index.json":
                        raise OSError("simulated index write failure")
                    return original(path, data)
                boundary = patch.object(w, "_write_new", side_effect=write) if stage == "index" else patch.object(w, "_append_history_version", side_effect=OSError("simulated append failure"))
                with boundary, self.assertRaises(OSError):
                    w.import_document_version(paths, manuscript)
                self.assertEqual(snapshot(paths.root), before)
                self.assertEqual(w.verify_project_versions(paths), [])
                self.assertEqual(w.import_document_version(paths, manuscript).version_id, "V2")

    def test_process_exit_during_migration_recovers_exact_old_snapshot(self):
        for stage in ("index", "append"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                paths = legacy_project(root)
                manuscript = next_manuscript(root)
                before = snapshot(paths.root)
                code = "import os,sys,argument_workbench as w\n"
                if stage == "index":
                    code += ("original=w._write_new\n"
                             "def stopped(path,data):\n"
                             " if path.name=='project-history-index.json': os._exit(81)\n"
                             " return original(path,data)\n"
                             "w._write_new=stopped\n")
                else:
                    code += "w._append_history_version=lambda *args: os._exit(81)\n"
                code += "w.import_document_version(sys.argv[1],sys.argv[2])\n"
                completed = subprocess.run([sys.executable, "-c", code, str(paths.root), str(manuscript)],
                                           cwd=REPO, capture_output=True, timeout=30)
                self.assertEqual(completed.returncode, 81, completed.stderr)
                self.assertEqual(w.workspace_paths(paths.root).version_id, "V1")
                self.assertEqual(snapshot(paths.root), before)
                self.assertEqual(w.verify_project_versions(paths), [])
                self.assertEqual(w.import_document_version(paths, manuscript).version_id, "V2")

    def test_malformed_history_values_return_issues_without_path_escape_or_type_errors(self):
        cases = [("version_id", []), ("version_id", "../../outside"), ("source_sha256", {}),
                 ("sequence", True), ("created_at", []), ("previous_entry_sha256", [])]
        with tempfile.TemporaryDirectory() as temp:
            paths = w.initialize_workspace(FIXTURE / "manuscript.md", Path(temp) / "project")
            original = json.loads(paths.history_index.read_bytes())
            for field, value in cases:
                with self.subTest(field=field, value=value):
                    changed = copy.deepcopy(original)
                    entry = changed["entries"][0]
                    entry[field] = value
                    entry["entry_sha256"] = w._history_entry_hash(entry)
                    changed["head_sha256"] = entry["entry_sha256"]
                    paths.history_index.write_bytes(w.json_bytes(changed))
                    self.assertTrue(w.verify_project_versions(paths))
            for field, value in (("entries", {}), ("schema_version", True), ("next_sequence", False)):
                with self.subTest(field=field):
                    changed = copy.deepcopy(original)
                    changed[field] = value
                    paths.history_index.write_bytes(w.json_bytes(changed))
                    self.assertTrue(w.verify_project_versions(paths))


if __name__ == "__main__":
    unittest.main()
