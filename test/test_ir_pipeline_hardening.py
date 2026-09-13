from __future__ import annotations

import json
import copy
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import argument_ir as ir
import argument_workbench as w
from argument_contracts import validate_artifact
from document_text_encoding import decode_document_text

FIXTURE = REPO / "test" / "fixtures" / "workbench-demo"


class IRPipelineHardeningTests(unittest.TestCase):
    def make_project(self, root):
        return w.initialize_workspace(FIXTURE / "manuscript.md", root / "project")

    def snapshot(self, root):
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
                if p.is_file() and p.name != ".mutation.lock"}

    def raw(self):
        return json.loads((FIXTURE / "raw-ir.json").read_text(encoding="utf-8"))

    def collect(self, paths, value):
        return w.collect_raw_attempt(paths, json.dumps(value).encode(), method="file",
                                     source_name="response.json", producer_label="test")

    def test_bad_relation_types_are_archived_without_crashing(self):
        for field in ("from", "to", "type"):
            for value in ([], {}, None, 1):
                with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as temp:
                    paths = self.make_project(Path(temp))
                    raw = self.raw()
                    raw["relations"][0][field] = value
                    _, record = self.collect(paths, raw)
                    self.assertNotEqual(record["validation"]["status"], "valid")
                    self.assertEqual(len(w.list_attempts(paths)), 1)
                    self.assertEqual(w.verify_workspace(paths, allow_incomplete=True), [])

    def test_invalid_unicode_and_numeric_json_are_unusable_archives(self):
        for bad in ("\ud800", float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=repr(bad)), tempfile.TemporaryDirectory() as temp:
                paths = self.make_project(Path(temp))
                raw = self.raw()
                raw["claims"][0]["text"] = bad
                _, record = self.collect(paths, raw)
                self.assertEqual(record["validation"]["status"], "unusable")
                self.assertEqual(w.verify_workspace(paths, allow_incomplete=True), [])
        with self.assertRaises(w.WorkbenchError):
            w.parse_json_strict(b'{"overflow":1e999}')

    def test_boolean_schema_version_is_rejected(self):
        raw = self.raw()
        raw["schema_version"] = True
        self.assertTrue(ir.validate_argument_ir(raw))
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_project(Path(temp))
            _, record = self.collect(paths, raw)
            self.assertEqual(record["validation"]["status"], "unusable")

    def test_direct_validator_rejects_unpaired_surrogate(self):
        raw = self.raw()
        raw["claims"][0]["uncertainty"] = "\ud800"
        self.assertTrue(ir.validate_argument_ir(raw))

    def test_version_publication_failure_restores_exact_files_and_can_retry(self):
        for after_write in (False, True):
            with self.subTest(after_write=after_write), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                paths = self.make_project(root)
                manuscript = root / "v2.md"
                manuscript.write_bytes((FIXTURE / "manuscript.md").read_bytes() + b"\nNew text.\n")
                before = self.snapshot(paths.root)
                original = w._append_history_version

                def fail(*args):
                    if after_write:
                        original(*args)
                    raise OSError("simulated full disk")

                with patch.object(w, "_append_history_version", side_effect=fail):
                    with self.assertRaises(OSError):
                        w.import_document_version(paths, manuscript)
                self.assertEqual(self.snapshot(paths.root), before)
                self.assertEqual(w.verify_project_versions(paths.root), [])
                self.assertEqual(w.import_document_version(paths, manuscript).version_id, "V2")
                self.assertEqual(w.verify_project_versions(paths.root), [])

    def test_interrupted_version_publication_recovers_on_open(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_project(root)
            manuscript = root / "v2.md"
            manuscript.write_bytes((FIXTURE / "manuscript.md").read_bytes() + b"\nNew text.\n")
            before = self.snapshot(paths.root)
            code = ("import os,sys,argument_workbench as w; "
                    "w._append_history_version=lambda *args: os._exit(81); "
                    "w.import_document_version(sys.argv[1],sys.argv[2])")
            result = subprocess.run([sys.executable, "-c", code, str(paths.root), str(manuscript)],
                                    cwd=REPO, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 81, result.stderr)
            self.assertEqual(w.workspace_paths(paths.root).version_id, "V1")
            self.assertEqual(self.snapshot(paths.root), before)
            self.assertEqual(w.verify_project_versions(paths.root), [])
            self.assertEqual(w.import_document_version(paths, manuscript).version_id, "V2")

    def simple_ir(self, path, text):
        raw = self.raw()
        raw["source"] = {"name": path.name, "sha256": w.sha256_bytes(path.read_bytes())}
        raw["claims"] = [raw["claims"][0]]
        raw["claims"][0].update(text=text, source_quote=text)
        for field in ("evidence", "assumptions", "citations", "relations", "unverified"):
            raw[field] = []
        return raw

    def test_eight_languages_keep_source_bytes_quotes_and_revision_binding(self):
        samples = (
            ("English evidence supports a claim.", "cp1252"),
            ("中文论证需要证据。", "gb18030"),
            ("繁體中文論證需要證據。", "big5"),
            ("Straße und überprüfbare Gründe.", "cp1252"),
            ("Une démonstration étayée par des preuves.", "cp1252"),
            ("日本語の論証には証拠が必要です。", "shift_jis"),
            ("Русский аргумент требует доказательств.", "cp1251"),
            ("Rātiō et causae: æquus, œconomia.", "utf-8"),
        )
        for text, encoding in samples:
            with self.subTest(encoding=encoding, text=text), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "draft.txt"
                original = text.encode(encoding)
                source.write_bytes(original)
                paths = w.initialize_workspace(source, root / "project", encoding=encoding)
                self.assertEqual(w.initialize_workspace(source, paths.root, encoding=encoding), paths)
                version = json.loads(paths.version.read_bytes())
                receipt = version["source"]["decoding"]
                self.assertEqual(receipt["text_sha256"], w.sha256_bytes(text.encode("utf-8")))
                self.assertEqual(version["source"]["sha256"], w.sha256_bytes(original))
                self.assertEqual((paths.version_dir / version["source"]["relative_path"]).read_bytes(), original)
                _, record = self.collect(paths, self.simple_ir(source, text))
                self.assertEqual(record["validation"]["status"], "valid")
                w.rebuild_workspace(paths)
                reviewed = json.loads(paths.reviewed_payload.read_bytes())
                self.assertEqual(reviewed["claims"][0]["source_quote"], text)
                self.assertEqual(reviewed["claims"][0]["position"], f"L1:C1-L1:C{len(text) + 1}")
                w.append_correction(paths, {"kind": "update_node", "target": "raw:C1",
                                            "changes": {"uncertainty": "Human review"}})
                w.rebuild_workspace(paths)
                self.assertEqual(w.verify_workspace(paths), [])
                self.assertFalse(w.rebuild_workspace(paths)[1])
                v1_files = self.snapshot(paths.version_dir)
                source.write_bytes((text + "\nV2").encode(encoding))
                new_paths = w.import_document_version(paths, source, encoding=encoding)
                self.assertEqual(new_paths.version_id, "V2")
                self.assertEqual(w.verify_project_versions(paths.root), [])
                self.assertEqual(self.snapshot(paths.version_dir), v1_files)

    def test_mixed_languages_auto_unicode_roundtrip(self):
        text = "English 简体 繁體 Straße français 日本語 русский Rātiō æ œ 🙂"
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-be", "utf-32", "utf-32-be"):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "mixed.txt"
                source.write_bytes(text.encode(encoding))
                paths = w.initialize_workspace(source, root / "project")
                self.collect(paths, self.simple_ir(source, text))
                w.rebuild_workspace(paths)
                self.assertEqual(w.verify_workspace(paths), [])
                self.assertEqual(json.loads(paths.reviewed_payload.read_bytes())["claims"][0]["text"], text)

    def test_ambiguous_auto_decode_does_not_create_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "draft.txt"
            source.write_bytes(b"\xa4\xa4\xa4\xe5")
            self.assertTrue(decode_document_text(source.read_bytes()).ambiguous)
            with self.assertRaisesRegex(w.WorkbenchError, "ambiguous.*explicit"):
                w.initialize_workspace(source, root / "project")
            self.assertFalse((root / "project").exists())

    def test_same_bytes_with_different_decoding_require_distinct_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "draft.txt"
            source.write_bytes(b"\xa4\xa4\xa4\xe5")
            paths = w.initialize_workspace(source, root / "project", encoding="big5")
            before = self.snapshot(paths.root)
            with self.assertRaisesRegex(w.WorkbenchError, "different manuscript decoding"):
                w.initialize_workspace(source, paths.root, encoding="gb18030")
            self.assertEqual(self.snapshot(paths.root), before)
            v2 = w.import_document_version(paths, source, encoding="gb18030")
            self.assertEqual(v2.version_id, "V2")
            self.assertNotEqual(v2.prompt.read_bytes(), paths.prompt.read_bytes())
            self.assertEqual(w.verify_project_versions(paths.root), [])

    def test_decoded_text_hash_is_checked_before_collecting(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_project(Path(temp))
            version = json.loads(paths.version.read_bytes())
            version["source"]["decoding"]["text_sha256"] = "0" * 64
            paths.version.write_bytes(w.json_bytes(version))
            with self.assertRaisesRegex(w.WorkbenchError, "decoded manuscript text SHA-256"):
                self.collect(paths, self.raw())
            self.assertEqual(w.list_attempts(paths), [])

    def test_legacy_utf8_version_without_receipt_remains_verifiable(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_project(Path(temp))
            version = json.loads(paths.version.read_bytes())
            del version["source"]["decoding"]
            version_bytes = w.json_bytes(version)
            paths.version.write_bytes(version_bytes)
            paths.history_index.write_bytes(w.json_bytes(w._new_history_index(
                version, version_bytes, version["provenance"]["created_at"])))
            self.collect(paths, self.raw())
            w.rebuild_workspace(paths)
            self.assertEqual(w.verify_workspace(paths), [])
            self.assertEqual(w.verify_project_versions(paths.root), [])

    def test_concurrent_direct_version_imports_keep_continuous_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_project(root)
            sources = [root / "second.md", root / "third.md"]
            for index, source in enumerate(sources):
                source.write_text(f"Version input {index}.", encoding="utf-8")
            ready = threading.Barrier(2)

            def run(source):
                ready.wait(timeout=10)
                return w.import_document_version(paths, source).version_id

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(run, source) for source in sources]
                self.assertEqual({future.result(timeout=20) for future in futures}, {"V2", "V3"})
            self.assertEqual(w.verify_project_versions(paths.root), [])
            self.assertEqual(w.list_version_ids(paths), ["V1", "V2", "V3"])

    def test_decoding_contract_rejects_malformed_receipts(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_project(Path(temp))
            version = json.loads(paths.version.read_bytes())
            for field, value in (("encoding", "rot_13"), ("encoding", "UTF8"),
                                 ("requested_encoding", False), ("text_sha256", "x"),
                                 ("ambiguous", 1), ("candidates", [1]),
                                 ("candidates", ["utf-8", "utf-8"])):
                with self.subTest(field=field, value=value):
                    changed = copy.deepcopy(version)
                    changed["source"]["decoding"][field] = value
                    self.assertTrue(validate_artifact(changed))


if __name__ == "__main__":
    unittest.main()
