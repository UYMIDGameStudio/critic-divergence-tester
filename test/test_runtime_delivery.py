import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import studio_launcher
from studio_startup import report_startup_failure


class InstalledRuntimeSelfTests(unittest.TestCase):
    def test_source_runtime_checks_research_ir_locales_and_http(self):
        from studio_selftest import run_self_test

        result = run_self_test()
        self.assertTrue(result["passed"])
        self.assertTrue({"browser-assets", "research-browser-assets", "professional-browser-assets",
                         "ui-locales", "eight-language-import", "big5-research-import", "research-ir",
                         "research-product-view", "research-workbench", "research-http"} <= set(result["checked"]))

    def test_assets_load_from_a_zip_package_outside_the_source_directory(self):
        # This is a small importlib-resources fixture, not a release build. A
        # separate interpreter prevents pre-imported source assets masking a
        # missing wheel/zip resource, including the professional cached shell.
        from importlib.resources import files
        import studio_selftest

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "browser-assets.zip"
            with zipfile.ZipFile(package, "w") as archive:
                for resource in files("studio_web").iterdir():
                    if resource.is_file() and Path(resource.name).suffix in {".py", ".html", ".css", ".js", ".json"}:
                        archive.writestr("studio_web/" + resource.name, resource.read_bytes())
            script = """import json, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import studio_web
from studio_selftest import _check_shell_assets
assert sys.argv[1] in str(studio_web.__file__)
_check_shell_assets()
print(json.dumps({'passed': True, 'asset_origin': 'zip-package'}))
"""
            result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", script,
                                     str(package), str(Path(studio_selftest.__file__).parent)],
                                    cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"passed": True, "asset_origin": "zip-package"})

    def test_missing_packaged_resource_reports_no_content(self):
        from argument_app import render_product_shell  # load normal dependencies before fault injection
        from studio_selftest import _check_shell_assets

        render_product_shell("fixture")

        class MissingPackage:
            def joinpath(self, name):
                raise FileNotFoundError("private-manuscript-token-123")

        with patch("importlib.resources.files", return_value=MissingPackage()):
            with self.assertRaisesRegex(RuntimeError, "packaged browser resource") as caught:
                _check_shell_assets()
        self.assertNotIn("private-manuscript", str(caught.exception))

    def test_shell_checks_reject_missing_translations_and_placeholders(self):
        import copy
        import re
        from argument_app import render_product_shell
        from argument_ui import render_app_shell
        from document_review_ui import render_studio_shell
        from studio_selftest import _check_rendered_shell

        for kind, renderer in (("studio", render_studio_shell), ("product", render_product_shell),
                               ("professional", render_app_shell)):
            with self.subTest(kind=kind):
                shell = renderer("runtime-test")
                variable = "UI_MESSAGES" if kind == "studio" else "RESEARCH_MESSAGES"
                marker = re.search(r"const\s+" + variable + r"\s*=\s*", shell)
                self.assertIsNotNone(marker)
                dictionary, length = json.JSONDecoder().raw_decode(shell[marker.end():])
                for locale in ("zh-Hant", "en"):
                    incomplete = copy.deepcopy(dictionary)
                    group = incomplete if kind == "studio" else incomplete["text"]
                    group[next(iter(group))].pop(locale)
                    broken = shell[:marker.end()] + json.dumps(incomplete) + shell[marker.end() + length:]
                    with self.assertRaisesRegex(RuntimeError, "translation dictionary"):
                        _check_rendered_shell(kind, broken)
                with self.assertRaisesRegex(RuntimeError, "shell resources"):
                    _check_rendered_shell(kind, shell + "__UNRESOLVED_RUNTIME_SLOT__")
        with self.assertRaisesRegex(RuntimeError, "language controls"):
            _check_rendered_shell("product", render_product_shell("fixture").replace('id="research-language"', 'id="missing-language"'))

    def test_research_read_only_failure_does_not_expose_runtime_payload(self):
        from argument_app import ProductApp
        from studio_selftest import _check_research_pipeline

        with tempfile.TemporaryDirectory() as temp, patch.object(ProductApp, "view", return_value={
                "selected": {"stage": "read_only", "title": "private-manuscript-token-123",
                             "errors": ["private-model-response-token-456"]}}):
            with self.assertRaisesRegex(RuntimeError, "research product could not open") as caught:
                _check_research_pipeline(Path(temp))
            self.assertNotIn("private-", str(caught.exception))


class LauncherTests(unittest.TestCase):
    def test_interactive_launch_reports_failure_while_cli_preserves_exception(self):
        with patch.object(sys, "argv", ["studio"]), patch.object(sys, "platform", "linux"), \
                patch.object(studio_launcher, "_run", side_effect=RuntimeError("private document text")), \
                patch("studio_startup.report_startup_failure") as report:
            self.assertEqual(studio_launcher.main(), 1)
            report.assert_called_once()
        with patch.object(sys, "argv", ["studio", "--self-test"]), \
                patch.object(studio_launcher, "_run", side_effect=RuntimeError("CLI error")):
            with self.assertRaisesRegex(RuntimeError, "CLI error"):
                studio_launcher.main()

    def test_startup_log_omits_exception_payload_and_source_lines(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"LOCALAPPDATA": temp}), \
                patch.object(sys, "platform", "linux"), contextlib.redirect_stderr(io.StringIO()):
            try:
                raise RuntimeError("sensitive-document-token-123")
            except RuntimeError as error:
                report_startup_failure(error)
            content = (Path(temp) / "DocumentReviewStudio/diagnostics/startup-error.json").read_text(encoding="utf-8")
            self.assertNotIn("sensitive-document", content)
            record = json.loads(content)
            self.assertEqual(record["exception_type"], "RuntimeError")
            self.assertTrue(record["frames"])


class BundlePublishingTests(unittest.TestCase):
    def test_archive_failure_leaves_no_published_version_and_can_retry(self):
        from scripts import build_portable
        from project_lifecycle import APP_VERSION

        def fake_build(command, **kwargs):
            if "PyInstaller" in command:
                destination = Path(command[command.index("--distpath") + 1]) / "DocumentReviewStudio"
                destination.mkdir(parents=True)
                executable = "DocumentReviewStudio.exe" if sys.platform == "win32" else "DocumentReviewStudio"
                (destination / executable).write_bytes(b"fixture binary")

        class Distribution:
            files = []

        with tempfile.TemporaryDirectory() as temp, patch.object(sys, "argv", ["build", "--output", temp]), \
                patch.object(build_portable.subprocess, "run", side_effect=fake_build), \
                patch.object(build_portable.platform, "machine", return_value="fixture-architecture"), \
                patch.object(build_portable.importlib.metadata, "distribution", return_value=Distribution()), \
                patch.object(build_portable.importlib.metadata, "version", return_value="fixture"), \
                contextlib.redirect_stdout(io.StringIO()):
            target = Path(temp) / f"DocumentReviewStudio-{APP_VERSION}-{sys.platform}"
            with patch.object(build_portable.zipfile.ZipFile, "write", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    build_portable.main()
            self.assertFalse(target.exists())
            self.assertFalse(list(Path(temp).glob("studio-build-*")))
            build_portable.main()
            self.assertTrue((target / "app/release-manifest.json").is_file())
            self.assertEqual(len(list(target.glob("*.zip"))), 1)
            self.assertEqual(len(list(target.glob("*.sha256"))), 1)
