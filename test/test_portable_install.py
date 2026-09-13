"""Execute the shipped PowerShell installer against isolated native EXE fixtures.

The fixture replaces only the application executable. Copying, manifest checks,
locking, project-command invocation, shortcuts and uninstall use the real scripts.
Windows PowerShell 5.1 is the baseline shipped with supported Windows machines.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell") if os.name == "nt" else None
INSTALLER = "安装或升级.ps1"
UNINSTALLER = "卸载程序.ps1"


def ps_command(source: str) -> list[str]:
    return [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", base64.b64encode(source.encode("utf-16le")).decode("ascii")]


def quote(path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


@unittest.skipUnless(POWERSHELL, "Windows PowerShell and native shortcuts required")
class PortableInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler_temp = tempfile.TemporaryDirectory(prefix="studio-installer-exe-")
        cls.executable = Path(cls.compiler_temp.name) / "DocumentReviewStudio.exe"
        source = r'''
using System;
using System.IO;
using System.Threading;
public static class StudioFixture {
    public static int Main(string[] args) {
        var root = AppDomain.CurrentDomain.BaseDirectory;
        var log = Environment.GetEnvironmentVariable("STUDIO_FIXTURE_LOG");
        if (!String.IsNullOrEmpty(log)) File.AppendAllText(log, root + "|" + String.Join("|", args) + "\n");
        var action = args.Length == 1 ? args[0] : args[1];
        if (Environment.GetEnvironmentVariable("STUDIO_FIXTURE_FAIL") == action) return 17;
        if (action == "--self-test") {
            if (Environment.GetEnvironmentVariable("STUDIO_FIXTURE_MUTATE") == "1")
                File.WriteAllText(Path.Combine(root, "payload.dat"), "changed during self-test");
            if (Environment.GetEnvironmentVariable("STUDIO_FIXTURE_WAIT") == "1") Thread.Sleep(5000);
        }
        if (action == "backup") {
            Directory.CreateDirectory(Path.GetDirectoryName(args[3]));
            File.WriteAllText(args[3], "fixture backup of " + args[2]);
        }
        return 0;
    }
}
'''
        command = "$ErrorActionPreference='Stop'; Add-Type -TypeDefinition @'\n" + source + "\n'@ -OutputAssembly " + quote(cls.executable) + " -OutputType ConsoleApplication"
        subprocess.run(ps_command(command), check=True, capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.compiler_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="studio-install-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "中文 paths with spaces"
        self.root.mkdir()
        self.install = self.root / "Programs" / "DocumentReviewStudio"
        self.projects = self.root / "Project library"
        self.shortcuts = self.root / "Start menu"
        self.log = self.root / "execution.log"
        self.env = {**os.environ, "STUDIO_FIXTURE_LOG": str(self.log)}
        self.release = self.make_release("0.2.1")

    def make_release(self, version):
        release = self.root / ("release-" + version)
        release.mkdir()
        for source, target in (("install-portable.ps1", INSTALLER), ("uninstall-portable.ps1", UNINSTALLER), ("portable-common.ps1", "portable-common.ps1")):
            shutil.copy2(ROOT / "scripts" / source, release / target)
        shutil.copy2(self.executable, release / "DocumentReviewStudio.exe")
        (release / "使用说明.md").write_text("Fixture instructions", encoding="utf-8")
        (release / "LICENSE").write_text("Fixture license", encoding="utf-8")
        (release / "payload.dat").write_bytes(b"application data")
        self.write_manifest(release, version)
        return release

    @staticmethod
    def write_manifest(release, version="0.2.1"):
        manifest = {"version": version, "platform": "win32", "signed": False, "files": {
            path.relative_to(release).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in release.rglob("*") if path.is_file() and path.name != "release-manifest.json"}}
        (release / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return manifest

    def command(self, release=None, *, uninstall=False, install=None, shortcuts=None, projects=None, operation_timeout=None):
        script = (release or self.release) / (UNINSTALLER if uninstall else INSTALLER)
        command = [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                   "-InstallRoot", str(install or self.install), "-ProjectRoot", str(projects or self.projects),
                   "-ShortcutRoot", str(shortcuts or self.shortcuts)]
        if operation_timeout is not None:
            command += ["-OperationTimeoutSeconds", str(operation_timeout)]
        return command + (["-ConfirmRemoval"] if uninstall else [])

    def run_script(self, release=None, *, success=True, env=None, **kwargs):
        result = subprocess.run(self.command(release, **kwargs), env={**self.env, **(env or {})},
                                capture_output=True, timeout=60)
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        if success:
            self.assertEqual(result.returncode, 0, output)
        else:
            self.assertNotEqual(result.returncode, 0, output)
        return output

    def assert_no_partial_install(self):
        self.assertFalse((self.install / "0.2.1").exists())
        self.assertFalse((self.install / ".installation.json").exists())
        self.assertFalse((self.shortcuts / "Document Review Studio.lnk").exists())
        self.assertEqual(list(self.install.glob(".staging-*")), [])

    def test_install_retry_upgrade_and_uninstall_preserve_projects(self):
        project = self.projects / "稿件.document-review-studio"
        project.mkdir(parents=True)
        (project / "author.txt").write_bytes(b"author material")
        output = self.run_script()
        self.assertIn("ArgumentWorkbench", output)
        self.assertEqual((self.install / "0.2.1" / "payload.dat").read_bytes(), b"application data")
        executions = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(executions), 3)
        self.assertTrue(all(".staging-" in line.split("|")[0] for line in executions))
        self.assertIn(str(project), executions[-1])
        self.assertEqual(len(list((self.projects / "backups").glob("*.zip"))), 1)
        # Same-version retry repairs the shortcut without executing or backing up again.
        (self.shortcuts / "Document Review Studio.lnk").unlink()
        self.run_script()
        self.assertTrue((self.shortcuts / "Document Review Studio.lnk").is_file())
        self.assertEqual(self.log.read_text(encoding="utf-8").splitlines(), executions)
        newer = self.make_release("0.2.2")
        self.run_script(newer)
        receipt = json.loads((self.install / ".installation.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["versions"], ["0.2.1", "0.2.2"])
        unrelated = self.install / "user-notes.txt"
        unrelated.write_bytes(b"do not remove")
        self.run_script(self.install / "0.2.2", uninstall=True)
        self.assertFalse((self.install / "0.2.1").exists())
        self.assertFalse((self.install / "0.2.2").exists())
        self.assertFalse((self.shortcuts / "Document Review Studio.lnk").exists())
        self.assertEqual((project / "author.txt").read_bytes(), b"author material")
        self.assertEqual(len(list((self.projects / "backups").glob("*.zip"))), 2)
        self.assertEqual(unrelated.read_bytes(), b"do not remove")

    def test_application_failures_leave_retryable_install(self):
        project = self.projects / "draft.document-review-studio"
        project.mkdir(parents=True)
        for action in ("--self-test", "upgrade-check", "backup"):
            with self.subTest(action=action):
                self.run_script(success=False, env={"STUDIO_FIXTURE_FAIL": action})
                self.assert_no_partial_install()
        self.run_script()

    def test_shortcut_failure_rolls_back_published_version_and_receipt(self):
        self.shortcuts.mkdir(parents=True)
        blocker = self.shortcuts / "Document Review Studio.lnk"
        blocker.mkdir()
        self.run_script(success=False)
        self.assertFalse((self.install / "0.2.1").exists())
        self.assertFalse((self.install / ".installation.json").exists())
        self.assertEqual(list(self.install.glob(".staging-*")), [])
        self.assertTrue(blocker.is_dir())
        blocker.rmdir()
        self.run_script()

    def test_failed_upgrade_preserves_previous_shortcut_and_receipt(self):
        self.run_script()
        receipt = (self.install / ".installation.json").read_bytes()
        shortcut = (self.shortcuts / "Document Review Studio.lnk").read_bytes()
        newer = self.make_release("0.2.2")
        self.run_script(newer, success=False, env={"STUDIO_FIXTURE_FAIL": "--self-test"})
        self.assertEqual((self.install / ".installation.json").read_bytes(), receipt)
        self.assertEqual((self.shortcuts / "Document Review Studio.lnk").read_bytes(), shortcut)
        self.assertTrue((self.install / "0.2.1").is_dir())
        self.assertFalse((self.install / "0.2.2").exists())

    def test_upgrade_shortcut_failure_restores_previous_registration(self):
        self.run_script()
        receipt = (self.install / ".installation.json").read_bytes()
        shortcut = self.shortcuts / "Document Review Studio.lnk"
        shortcut_bytes = shortcut.read_bytes()
        newer = self.make_release("0.2.2")
        shortcut.chmod(stat.S_IREAD)
        try:
            self.run_script(newer, success=False)
            self.assertEqual((self.install / ".installation.json").read_bytes(), receipt)
            self.assertEqual(shortcut.read_bytes(), shortcut_bytes)
            self.assertFalse((self.install / "0.2.2").exists())
            self.assertEqual(list(self.install.glob(".staging-*")), [])
        finally:
            shortcut.chmod(stat.S_IREAD | stat.S_IWRITE)
        self.run_script(newer)

    def test_manifest_rejects_unlisted_missing_tampered_and_malformed_files(self):
        original = (self.release / "release-manifest.json").read_text(encoding="utf-8")
        valid = json.loads(original)
        cases = [
            {**valid, "files": {}}, {**valid, "files": []}, {**valid, "files": None},
            {key: value for key, value in valid.items() if key != "files"},
            {**valid, "platform": "linux"}, {**valid, "platform": ["win32"]},
            {**valid, "signed": 1}, {**valid, "version": "../outside"},
            {**valid, "files": {**valid["files"], "../outside": "0" * 64}},
            {**valid, "files": {**valid["files"], "payload.dat:stream": "0" * 64}},
            {**valid, "files": {**valid["files"], "payload.dat": "0" * 64}},
            {**valid, "files": {name: digest for name, digest in valid["files"].items() if name != "DocumentReviewStudio.exe"}},
        ]
        for index, manifest in enumerate(cases):
            with self.subTest(case=index):
                (self.release / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                self.run_script(success=False)
                self.assertFalse(self.log.exists())
        # Duplicate JSON keys and unlisted executables must also stop before execution.
        (self.release / "release-manifest.json").write_text(original[:-1] + ', "files": {}}', encoding="utf-8")
        self.assertIn("Duplicate", self.run_script(success=False))
        (self.release / "release-manifest.json").write_text(original, encoding="utf-8")
        (self.release / "unlisted.dll").write_bytes(b"unlisted")
        self.assertIn("absent", self.run_script(success=False))
        self.assertFalse(self.log.exists())
        (self.release / "unlisted.dll").unlink()
        (self.release / "payload.dat").unlink()
        self.assertIn("Missing release file", self.run_script(success=False))
        self.assertFalse(self.log.exists())

    def test_program_mutation_after_validation_is_rejected(self):
        self.run_script(success=False, env={"STUDIO_FIXTURE_MUTATE": "1"})
        self.assert_no_partial_install()
        self.assertEqual((self.release / "payload.dat").read_bytes(), b"application data")

    def test_application_timeout_rolls_back_and_releases_install_lock(self):
        output = self.run_script(success=False, env={"STUDIO_FIXTURE_WAIT": "1"}, operation_timeout=1)
        self.assertIn("timed out", output)
        self.assert_no_partial_install()
        self.run_script()

    def test_install_lock_prevents_concurrent_mutation(self):
        first = subprocess.Popen(self.command(), env={**self.env, "STUDIO_FIXTURE_WAIT": "1"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 20
            while not self.log.exists() and time.monotonic() < deadline and first.poll() is None:
                time.sleep(0.05)
            self.assertTrue(self.log.exists(), "First installer did not reach its self-test")
            self.assertIn("Another installation", self.run_script(success=False))
            out, err = first.communicate(timeout=30)
            self.assertEqual(first.returncode, 0, (out + err).decode(errors="replace"))
        finally:
            if first.poll() is None:
                first.kill()
            first.communicate(timeout=10)

    def test_interrupted_installer_cleans_owned_staging_on_retry(self):
        first = subprocess.Popen(self.command(), env={**self.env, "STUDIO_FIXTURE_WAIT": "1"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 20
            while not self.log.exists() and time.monotonic() < deadline and first.poll() is None:
                time.sleep(0.05)
            self.assertTrue(self.log.exists(), "Installer did not reach its executable")
            first.kill()
            first.communicate(timeout=15)
            self.assertTrue((self.install / ".install-staging.json").is_file())
            self.assertEqual(len(list(self.install.glob(".staging-*"))), 1)
            # Its native child may finish after the killed parent. Wait for the
            # known bounded fixture before requesting interrupted-stage cleanup.
            child_deadline = time.monotonic() + 15
            stage_exe = next(self.install.glob(".staging-*")) / "DocumentReviewStudio.exe"
            while time.monotonic() < child_deadline:
                try:
                    with stage_exe.open("r+b"):
                        break
                except (PermissionError, FileNotFoundError):
                    time.sleep(0.1)
            else:
                self.fail("Interrupted install child did not release its executable")
            self.run_script()
            self.assertFalse((self.install / ".install-staging.json").exists())
            self.assertEqual(list(self.install.glob(".staging-*")), [])
        finally:
            if first.poll() is None:
                first.kill()
            first.communicate(timeout=15)

    def test_uninstall_refuses_added_files_and_linked_paths(self):
        self.run_script()
        foreign = self.install / "0.2.1" / "author-notes.txt"
        foreign.write_bytes(b"keep")
        self.run_script(uninstall=True, success=False)
        self.assertEqual(foreign.read_bytes(), b"keep")
        foreign.unlink()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"keep outside")
        junction = self.install / "0.2.1" / "linked"
        result = subprocess.run(ps_command("$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path " + quote(junction) + " -Target " + quote(outside) + " | Out-Null"), capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            self.assertIn("Linked paths", self.run_script(uninstall=True, success=False))
            self.assertEqual((outside / "keep.txt").read_bytes(), b"keep outside")
        finally:
            # Remove only the junction itself; never recursively remove its target.
            os.rmdir(junction)

    def test_rejects_project_directory_overlap(self):
        self.run_script(success=False, projects=self.install / "projects")
        self.assertFalse(self.install.exists())
        self.assertFalse(self.log.exists())

    def test_corrupt_interrupted_install_journal_is_preserved(self):
        self.install.mkdir(parents=True)
        journal = self.install / ".install-staging.json"
        data = json.dumps({"product": "DocumentReviewStudio", "stage": "../outside"}).encode()
        journal.write_bytes(data)
        outside = self.install.parent / "outside"
        outside.mkdir()
        (outside / "keep").write_bytes(b"preserved")
        self.assertIn("Invalid interrupted-install", self.run_script(success=False))
        self.assertEqual(journal.read_bytes(), data)
        self.assertEqual((outside / "keep").read_bytes(), b"preserved")
        self.assertFalse(self.log.exists())

    def test_release_and_install_ancestor_links_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_bytes(b"do not touch")
        junction = self.release / "linked"
        result = subprocess.run(ps_command("$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path " + quote(junction) + " -Target " + quote(outside) + " | Out-Null"), capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            self.assertIn("Linked paths", self.run_script(success=False))
            self.assertFalse(self.log.exists())
        finally:
            os.rmdir(junction)
        junction = self.root / "linked-install"
        result = subprocess.run(ps_command("$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path " + quote(junction) + " -Target " + quote(outside) + " | Out-Null"), capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            self.assertIn("Linked paths", self.run_script(success=False, install=junction / "Programs"))
            self.assertFalse((outside / "Programs").exists())
            self.assertEqual(marker.read_bytes(), b"do not touch")
        finally:
            os.rmdir(junction)


@unittest.skipUnless(POWERSHELL and os.environ.get("STUDIO_PORTABLE_RELEASE"), "Set STUDIO_PORTABLE_RELEASE to test the built Windows bundle")
class BuiltPortableTests(unittest.TestCase):
    def test_real_bundle_install_backup_uninstall(self):
        from document_review_studio import DocumentReviewProject
        from project_lifecycle import restore_backup

        release = Path(os.environ["STUDIO_PORTABLE_RELEASE"]).resolve()
        version = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))["version"]
        with tempfile.TemporaryDirectory(prefix="studio-real-install-") as directory:
            root = Path(directory)
            install, library, shortcuts = root / "Programs", root / "Projects", root / "Shortcuts"
            project = DocumentReviewProject.create(library, filename="draft.md", content=b"# Real package\n\nAuthor material.")
            original = (project.root / "project.json").read_bytes()
            args = ["-InstallRoot", str(install), "-ProjectRoot", str(library), "-ShortcutRoot", str(shortcuts)]
            def run(script, *extra):
                result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(release / script), *args, *extra], capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors="replace"))
            run(INSTALLER)
            self.assertTrue((install / version / "DocumentReviewStudio.exe").is_file())
            self.assertTrue((shortcuts / "Document Review Studio.lnk").is_file())
            archives = list((library / "backups").glob("*.zip"))
            self.assertEqual(len(archives), 1)
            restored = restore_backup(archives[0], root / "Restored")
            self.assertFalse(DocumentReviewProject(restored).integrity_errors())
            run(INSTALLER)
            self.assertEqual(len(list((library / "backups").glob("*.zip"))), 1)
            run(UNINSTALLER, "-ConfirmRemoval")
            self.assertFalse((install / version).exists())
            self.assertFalse((shortcuts / "Document Review Studio.lnk").exists())
            self.assertEqual((project.root / "project.json").read_bytes(), original)
            self.assertTrue(archives[0].is_file())


if __name__ == "__main__":
    unittest.main()
