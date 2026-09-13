"""Bind successful legacy conversion to both its original and actual output."""
import hashlib
import io
import json
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import Mock, patch

import document_review_legacy as legacy

from document_review_ingest import IngestionError, IngestionLimits, ingest_bytes
from document_review_model import stable_id
from document_review_studio import DocumentReviewProject, _minimal_docx
from test.test_office_formats import MULTILINGUAL, pptx_entries, xlsx_entries, zipped


class LegacyConversionTests(unittest.TestCase):
    def outputs(self):
        with zipfile.ZipFile(io.BytesIO(_minimal_docx(MULTILINGUAL))) as archive:
            docx_entries = {name: archive.read(name) for name in archive.namelist()}
        table = b"<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Same cell</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Same cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        docx_entries["word/document.xml"] = docx_entries["word/document.xml"].replace(b"</w:body>", table + b"</w:body>")
        return ((".doc", ".docx", zipped(docx_entries)),
                (".xls", ".xlsx", zipped(xlsx_entries())),
                (".ppt", ".pptx", zipped(pptx_entries())))

    def different_zip_metadata(self, data):
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(output, "w") as changed:
            changed.comment = b"A later save of the same document XML"
            for name in original.namelist():
                info = zipfile.ZipInfo(name, date_time=(2024, 5, 6, 7, 8, 10))
                changed.writestr(info, original.read(name), compress_type=zipfile.ZIP_DEFLATED)
        return output.getvalue()

    def test_same_legacy_source_has_stable_ids_when_only_conversion_zip_metadata_changes(self):
        for suffix, _, first_output in self.outputs():
            with self.subTest(suffix=suffix):
                second_output = self.different_zip_metadata(first_output)
                self.assertNotEqual(hashlib.sha256(first_output).digest(), hashlib.sha256(second_output).digest())
                original = b"same original legacy file " + suffix.encode()
                documents = []
                for converted in (first_output, second_output):
                    with patch.object(legacy, "find_libreoffice", return_value="converter"), \
                            patch.object(legacy, "_convert", return_value=converted), \
                            patch.object(legacy, "_converter_version", return_value=None):
                        documents.append(ingest_bytes("original" + suffix, original))
                first, second = documents
                self.assertEqual(first.plain_text, second.plain_text)
                self.assertEqual([b.to_dict() for b in first.blocks], [b.to_dict() for b in second.blocks])
                self.assertEqual(first.source_to_block, second.source_to_block)
                self.assertEqual([warning.to_dict() for warning in first.warnings],
                                 [warning.to_dict() for warning in second.warnings])
                self.assertIn(MULTILINGUAL, first.plain_text)
                identifiers = {b.block_id for b in first.blocks}
                self.assertEqual(len(identifiers), len(first.blocks))
                self.assertTrue(any(b.kind == "table" for b in first.blocks))
                for block in first.blocks:
                    self.assertEqual(block.location.block_id, block.block_id)
                    self.assertTrue(set(block.children) <= identifiers)
                    if block.location.table_id is not None:
                        self.assertIn(block.location.table_id, identifiers)
                        self.assertEqual(first.block(block.location.table_id).kind, "table")
                for mapping in first.source_to_block:
                    self.assertIn(mapping["block_id"], identifiers)
                    if mapping.get("table_id") is not None:
                        self.assertIn(mapping["table_id"], identifiers)
                self.assertNotEqual(first.metadata["legacy_conversion"]["output"]["sha256"],
                                    second.metadata["legacy_conversion"]["output"]["sha256"])

    def test_original_identity_override_never_bypasses_actual_conversion_binding(self):
        from document_review_ingest import _binding, _parse_docx
        from document_review_office_formats import parse_office
        identity = hashlib.sha256(b"original binary identity").hexdigest()
        for _, suffix, data in self.outputs():
            parser = _parse_docx if suffix == ".docx" else parse_office
            source = _binding("converted" + suffix, data)
            with self.subTest(suffix=suffix):
                parsed = parser(data, source, IngestionLimits(), identity_sha256=identity)
                self.assertEqual(parsed.source.sha256, hashlib.sha256(data).hexdigest())
                self.assertEqual(parsed.document_id, stable_id("DOC", identity))
                for invalid_source in (replace(source, sha256=identity), replace(source, byte_size=source.byte_size + 1)):
                    with self.assertRaisesRegex(IngestionError, "绑定不一致"):
                        parser(data, invalid_source, IngestionLimits(), identity_sha256=identity)
                for invalid_identity in ([], True, "", "x" * 64):
                    with self.assertRaisesRegex(IngestionError, "SHA-256"):
                        parser(data, source, IngestionLimits(), identity_sha256=invalid_identity)
                with self.assertRaisesRegex(IngestionError, "安全限制"):
                    parser(data, source, IngestionLimits(max_file_bytes=len(data) - 1), identity_sha256=identity)

    def test_different_original_files_do_not_share_ids_for_identical_conversion_output(self):
        for suffix, _, converted in self.outputs():
            identifiers = []
            for original in (b"first original file", b"second original file"):
                with patch.object(legacy, "find_libreoffice", return_value="converter"), \
                        patch.object(legacy, "_convert", return_value=converted), \
                        patch.object(legacy, "_converter_version", return_value=None):
                    parsed = ingest_bytes("original" + suffix, original)
                    identifiers.append({block.block_id for block in parsed.blocks})
            self.assertTrue(identifiers[0].isdisjoint(identifiers[1]))

    def test_converted_doc_xls_ppt_bind_output_then_preserve_original(self):
        for suffix, modern_suffix, output in self.outputs():
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temp:
                original = bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic legacy input " + suffix.encode()
                original_sha = hashlib.sha256(original).hexdigest()
                with patch("document_review_legacy.find_libreoffice", return_value="local-soffice"), \
                        patch("document_review_legacy._convert", return_value=output) as convert, \
                        patch("document_review_legacy._converter_version", return_value="LibreOffice 26.2.0.3 observed-fixture"):
                    project = DocumentReviewProject.create(temp, filename="original" + suffix, content=original)
                convert.assert_called_once()
                document = project.document()
                self.assertIsNotNone(document)
                self.assertIn(MULTILINGUAL, document.plain_text)
                self.assertEqual(document.source.sha256, original_sha)
                self.assertEqual(document.source.byte_size, len(original))
                self.assertEqual(document.source.extension, suffix)
                self.assertEqual(document.document_id, stable_id("DOC", original_sha))
                receipt = document.metadata["legacy_conversion"]
                self.assertEqual(receipt["input"]["sha256"], original_sha)
                self.assertEqual(receipt["output"], {"extension": modern_suffix, "byte_size": len(output),
                                                    "sha256": hashlib.sha256(output).hexdigest()})
                self.assertEqual(receipt["converter"]["version"], "LibreOffice 26.2.0.3 observed-fixture")
                self.assertTrue(receipt["converter"]["version_observed"])
                self.assertEqual(receipt["algorithm"], "libreoffice-headless-ooxml-v1")
                manifest = project.manifest()
                self.assertEqual((project.root / manifest["source"]["relative_path"]).read_bytes(), original)
                self.assertEqual(project.integrity_errors(), [])
                persisted = json.loads(project.document_path.read_text(encoding="utf-8"))
                self.assertEqual(persisted["metadata"]["legacy_conversion"], receipt)

    def test_unknown_converter_version_is_not_invented(self):
        output = zipped(xlsx_entries())
        with patch("document_review_legacy.find_libreoffice", return_value="local-soffice"), \
                patch("document_review_legacy._convert", return_value=output), \
                patch("document_review_legacy._converter_version", return_value=None):
            document = ingest_bytes("original.xls", b"legacy binary")
        converter = document.metadata["legacy_conversion"]["converter"]
        self.assertIsNone(converter["version"])
        self.assertFalse(converter["version_observed"])

    def test_conversion_output_still_passes_real_modern_safety_checks(self):
        output = zipped(xlsx_entries())
        with patch("document_review_legacy.find_libreoffice", return_value="local-soffice"), \
                patch("document_review_legacy._convert", return_value=output), \
                patch("document_review_legacy._converter_version", return_value=None):
            with self.assertRaisesRegex(IngestionError, "安全限制"):
                ingest_bytes("original.xls", b"legacy binary", limits=IngestionLimits(max_office_cells=1))

    def test_ooxml_under_legacy_suffix_does_not_claim_external_conversion(self):
        for suffix, _, raw in self.outputs():
            with self.subTest(suffix=suffix), patch("document_review_legacy.find_libreoffice", side_effect=AssertionError("converter not needed")):
                document = ingest_bytes("original" + suffix, raw)
                self.assertNotIn("legacy_conversion", document.metadata)
                self.assertEqual(document.source.sha256, hashlib.sha256(raw).hexdigest())
                self.assertEqual(document.metadata["original_format"], suffix)

    def test_converter_version_only_records_observed_success(self):
        from document_review_legacy import _converter_version
        for output, code, expected in (("LibreOffice 26.2.0.3 build-id\n", 0, "LibreOffice 26.2.0.3 build-id"),
                                       ("", 0, None), ("unexpected failure", 1, None)):
            with self.subTest(output=output), patch("document_review_legacy.subprocess.run", return_value=SimpleNamespace(stdout=output, returncode=code)):
                self.assertEqual(_converter_version("local-soffice"), expected)
        with patch("document_review_legacy.subprocess.run", side_effect=OSError("unavailable")):
            self.assertIsNone(_converter_version("local-soffice"))

    def test_windows_converter_and_taskkill_failure_never_use_unbounded_wait(self):
        waits = []
        class HangingProcess:
            pid = 31415
            def __enter__(self):
                raise AssertionError("Popen context would wait indefinitely")
            def __exit__(self, *args):
                raise AssertionError("Popen context would wait indefinitely")
            def poll(self): return None
            def wait(self, timeout=None):
                if timeout is None:
                    raise AssertionError("unbounded wait")
                waits.append(timeout)
                raise subprocess.TimeoutExpired("fixture", timeout)
            def kill(self):
                raise PermissionError("simulated OS kill refusal")

        for killer in (HangingProcess(), FileNotFoundError("taskkill missing")):
            with self.subTest(killer=type(killer).__name__), patch.object(legacy.os, "name", "nt"), \
                    patch.object(legacy.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                    patch.object(legacy.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, create=True), \
                    patch.object(legacy.subprocess, "Popen", side_effect=[HangingProcess(), killer]) as launch, \
                    patch("critic_execution._create_windows_kill_job", return_value=None), \
                    patch("critic_execution._resume_windows_process"):
                started = time.monotonic()
                with self.assertRaisesRegex(IngestionError, "转换超时"):
                    legacy._run_conversion(["converter fixture"], timeout_seconds=0.01)
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(all(value > 0 for value in waits))
                self.assertEqual(launch.call_args_list[1].args[0], ["taskkill", "/PID", "31415", "/T", "/F"])
                self.assertNotIn("/IM", launch.call_args_list[1].args[0])

    def test_posix_group_kill_failure_preserves_timeout_and_has_a_bounded_reap(self):
        process = Mock(pid=2718)
        process.wait.side_effect = subprocess.TimeoutExpired("fixture", 0.01)
        process.poll.return_value = None
        process.kill.side_effect = PermissionError("simulated kill refusal")
        with patch.object(legacy.os, "name", "posix"), patch.object(legacy.os, "killpg", side_effect=PermissionError("group kill refused"), create=True) as group_kill, \
                patch.object(legacy.signal, "SIGKILL", 9, create=True), patch.object(legacy.subprocess, "Popen", return_value=process) as launch:
            with self.assertRaisesRegex(IngestionError, "转换超时"):
                legacy._run_conversion(["converter fixture"], timeout_seconds=0.01)
        group_kill.assert_called_once_with(2718, 9)
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        self.assertEqual([call.kwargs["timeout"] for call in process.wait.call_args_list], [0.01, 5])

    def test_converter_start_assigns_private_job_before_resume_and_closes_it(self):
        events = []
        process = Mock(pid=12345)
        process.wait.return_value = 0
        process.poll.return_value = 0
        with patch.object(legacy.os, "name", "nt"), \
                patch.object(legacy.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch.object(legacy.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, create=True), \
                patch.object(legacy.subprocess, "Popen", return_value=process) as launch, \
                patch("critic_execution._create_windows_kill_job", side_effect=lambda value: events.append("assign") or 9876), \
                patch("critic_execution._resume_windows_process", side_effect=lambda value: events.append("resume")), \
                patch("critic_execution._close_windows_handle", side_effect=lambda value: events.append(("close", value))):
            self.assertEqual(legacy._run_conversion(["converter fixture"]), 0)
        self.assertEqual(events, ["assign", "resume", ("close", 9876)])
        self.assertTrue(launch.call_args.kwargs["creationflags"] & 0x00000004)
        process.kill.assert_not_called()
        launch.assert_called_once()

    def test_real_timeout_removes_only_its_own_descendants(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            child_marker, control_marker = root / "child-alive", root / "control-alive"
            child_code = f"import time,pathlib; time.sleep(1.5); pathlib.Path({str(child_marker)!r}).write_text('alive')"
            parent_code = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child_code!r}]); time.sleep(30)"
            control_code = f"import time,pathlib; time.sleep(0.8); pathlib.Path({str(control_marker)!r}).write_text('alive')"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            control = subprocess.Popen([sys.executable, "-c", control_code], creationflags=flags)
            try:
                started = time.monotonic()
                with self.assertRaisesRegex(IngestionError, "转换超时"):
                    legacy._run_conversion([sys.executable, "-c", parent_code], timeout_seconds=0.5)
                self.assertLess(time.monotonic() - started, 3)
                control.wait(timeout=5)
                time.sleep(1.0)
                self.assertFalse(child_marker.exists(), "conversion descendant survived timeout")
                self.assertTrue(control_marker.exists(), "an unrelated process was terminated")
            finally:
                if control.poll() is None:
                    control.kill()
                    control.wait(timeout=5)

    def test_real_normal_exit_removes_lingering_conversion_descendants(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "child-alive"
            child_code = f"import time,pathlib; time.sleep(0.8); pathlib.Path({str(marker)!r}).write_text('alive')"
            parent_code = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child_code!r}])"
            self.assertEqual(legacy._run_conversion([sys.executable, "-c", parent_code], timeout_seconds=5), 0)
            time.sleep(1.0)
            self.assertFalse(marker.exists(), "conversion descendant survived normal parent exit")

    def test_libreoffice_exe_path_and_macos_standard_installations_are_discoverable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "soffice.exe"
            console = root / "soffice.com"
            executable.write_bytes(b"fixture")
            def executable_only(name): return str(executable) if name == "soffice.exe" else None
            with patch.object(legacy.shutil, "which", side_effect=executable_only):
                self.assertEqual(legacy.find_libreoffice(), str(executable))
                console.write_bytes(b"console fixture")
                self.assertEqual(legacy.find_libreoffice(), str(console))
            user_install = root / "Applications/LibreOffice.app/Contents/MacOS/soffice"
            user_install.parent.mkdir(parents=True)
            user_install.write_bytes(b"fixture")
            with patch.object(legacy.shutil, "which", return_value=None), patch.object(legacy.sys, "platform", "darwin"), \
                    patch.object(legacy.Path, "home", return_value=root), patch.dict(os.environ, {"ProgramFiles": "", "ProgramFiles(x86)": ""}):
                self.assertEqual(legacy.find_libreoffice(), str(user_install))


if __name__ == "__main__":
    unittest.main()
