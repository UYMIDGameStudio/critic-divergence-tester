"""Exercise the offline release builder against deliberately dirty build trees."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

from scripts import build_wheel


BUILD_TOOLS = all(importlib.util.find_spec(name) is not None for name in ("setuptools", "wheel"))


class WheelManifestPathTests(unittest.TestCase):
    def test_untrusted_member_names_cannot_be_release_sources(self):
        for name in ("../outside.py", "/absolute.py", "C:/drive.py", "pkg\\module.py", "pkg//module.py",
                     "pkg/./module.py", "pkg/__pycache__/module.pyc", "module.PYC", "pkg/module.pyo"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                build_wheel._safe_name(name)


@unittest.skipUnless(BUILD_TOOLS, "Offline wheel verification requires installed setuptools and wheel")
class WheelDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wheel-delivery-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.project = self.directory / "project"
        self.project.mkdir()
        self.output = self.directory / "delivery"
        (self.project / "pyproject.toml").write_text('''[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"
[project]
name = "clean-wheel-fixture"
version = "1.2.3"
license = "MIT"
[project.scripts]
clean-fixture = "sample:main"
[tool.setuptools]
py-modules = ["sample"]
packages = ["web"]
[tool.setuptools.package-data]
web = ["*.js", "*.html"]
[tool.setuptools.data-files]
protocols = ["review.md"]
''', encoding="utf-8")
        for name, data in {
            "sample.py": b'def main():\n    return "current source"\n',
            "web/__init__.py": b'"""Browser resources."""\n',
            "web/review.js": b'export const protocol = "current";\n',
            "web/index.html": b'<main>Current UI</main>\n',
            "review.md": b'# Current review protocol\n',
            "LICENSE": b'MIT License\n',
            "build/lib/sample.py": b'raise RuntimeError("stale module must never ship")\n',
            "build/lib/private-unregistered.py": b'SHOULD_NOT_SHIP = True\n',
            "build/lib/__pycache__/sample.cpython-314.pyc": b'old bytecode',
            "web/__pycache__/__init__.cpython-314.pyc": b'old package bytecode',
            "clean_wheel_fixture.egg-info/SOURCES.txt": b'build/lib/private-unregistered.py\n',
        }.items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def build(self):
        result = subprocess.run([sys.executable, str(Path(build_wheel.__file__).resolve()),
                                 "--project", str(self.project), "--output", str(self.output)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout)
        wheels = list(self.output.glob("*.whl"))
        self.assertEqual(len(wheels), 1)
        return wheels[0]

    def test_real_offline_build_ignores_stale_build_and_egg_metadata(self):
        before = {path.relative_to(self.project).as_posix(): path.read_bytes()
                  for path in self.project.rglob("*") if path.is_file()}
        wheel = self.build()
        with zipfile.ZipFile(wheel) as archive:
            self.assertEqual(archive.read("sample.py"), before["sample.py"])
            self.assertEqual(archive.read("web/review.js"), before["web/review.js"])
            self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") or "private-unregistered" in name
                                 for name in archive.namelist()))
            self.assertEqual(archive.read("clean_wheel_fixture-1.2.3.data/data/protocols/review.md"), before["review.md"])
        after = {path.relative_to(self.project).as_posix(): path.read_bytes()
                 for path in self.project.rglob("*") if path.is_file()}
        self.assertEqual(before, after, "caller-owned build and metadata files must remain untouched")
        report = json.loads(next(self.output.glob("*.verification.json")).read_text(encoding="utf-8"))
        self.assertEqual(report["verified_source_files"], 5)
        self.assertTrue(all(report["checks"].values()))
        self.assertFalse(list(self.output.glob("wheel-build-*")))

    def test_publication_refuses_to_replace_existing_release(self):
        self.build()
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        with self.assertRaisesRegex(FileExistsError, "Output exists"):
            build_wheel.build_wheel(self.project, self.output)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.output.iterdir()})

    def test_manifest_rejects_stale_source_cache_unsafe_path_link_and_bad_record(self):
        original = self.build()
        scenarios = (
            ("stale", "sample.py", b'print("stale")\n', None),
            ("cache", "web/__pycache__/stale.pyc", b"bytecode", None),
            ("traversal", "../outside.py", b"outside", None),
            ("unknown", "unregistered.py", b"unregistered", None),
            ("link", "linked.py", b"sample.py", stat.S_IFLNK | 0o777),
            ("record", "clean_wheel_fixture-1.2.3.dist-info/RECORD", b"invalid-record\n", None),
        )
        for label, name, data, mode in scenarios:
            bad = self.directory / (label + ".whl")
            with zipfile.ZipFile(original) as source, zipfile.ZipFile(bad, "w") as target:
                for item in source.infolist():
                    if item.filename != name:
                        target.writestr(item, source.read(item))
                entry = zipfile.ZipInfo(name)
                if mode is not None:
                    entry.create_system = 3
                    entry.external_attr = mode << 16
                target.writestr(entry, data)
            with self.subTest(case=label), self.assertRaises(ValueError):
                build_wheel.verify_wheel(bad, self.project)


if __name__ == "__main__":
    unittest.main()
