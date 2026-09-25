"""Build and verify a wheel without reusing or deleting the checkout's build tree.

Requires the locally installed release setuptools/wheel tools; no package index
or dependency installer is invoked. Publication never replaces an earlier file.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import csv
from email.parser import BytesParser
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def _config(root):
    try:
        import tomllib
    except ImportError:  # Python 3.10 release environments use setuptools' parser.
        from setuptools._vendor import tomli as tomllib
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))


def _safe_name(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or "\x00" in name or path.is_absolute()
            or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/"))):
        raise ValueError(f"Unsafe wheel path: {name!r}")
    if "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo"}:
        raise ValueError(f"Bytecode is not a release source asset: {name}")
    return path


def _source_bytes(root, path):
    relative = path.relative_to(root)
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError(f"Release source must not be a link: {relative}")
    if not path.is_file():
        raise ValueError(f"Declared release source missing: {relative}")
    return path.read_bytes()


def source_manifest(root, config):
    """Map every declared installed source/data file to its exact checkout bytes."""
    project = config["project"]
    distribution = re.sub(r"[-_.]+", "_", project["name"])
    prefix = distribution + "-" + project["version"]
    settings = config["tool"]["setuptools"]
    expected, source_paths = {}, {}

    def add(name, source):
        _safe_name(name)
        data = _source_bytes(root, source)
        if name in expected:
            if expected[name] != data or source_paths[name] != source.relative_to(root).as_posix():
                raise ValueError(f"Conflicting installed release paths: {name}")
            return
        expected[name] = data
        source_paths[name] = source.relative_to(root).as_posix()

    modules, packages = settings.get("py-modules", []), settings.get("packages", [])
    if not isinstance(modules, list) or not isinstance(packages, list):
        raise ValueError("Release builder requires explicit py-modules and packages")
    for module in modules:
        name = module.replace(".", "/") + ".py"
        add(name, root / name)
    for package in packages:
        package_dir = root / package.replace(".", "/")
        if not (package_dir / "__init__.py").is_file():
            raise ValueError(f"Registered package is missing: {package}")
        for source in sorted(package_dir.glob("*.py")):
            add(source.relative_to(root).as_posix(), source)
        for pattern in settings.get("package-data", {}).get(package, []):
            _safe_name(pattern)
            matches = [source for source in sorted(package_dir.glob(pattern)) if source.is_file()]
            if not matches:
                raise ValueError(f"Declared package asset pattern has no files: {package}/{pattern}")
            for source in matches:
                add(source.relative_to(root).as_posix(), source)
    for destination, patterns in settings.get("data-files", {}).items():
        if destination != ".":
            _safe_name(destination)
        for pattern in patterns:
            _safe_name(pattern)
            matches = [source for source in sorted(root.glob(pattern)) if source.is_file()]
            if not matches:
                raise ValueError(f"Declared data-file pattern has no files: {pattern}")
            for source in matches:
                installed = PurePosixPath(prefix + ".data/data")
                if destination != ".":
                    installed /= destination
                add((installed / source.name).as_posix(), source)
    return prefix, expected, source_paths


def verify_wheel(wheel, root, config=None):
    config = config or _config(root)
    prefix, expected, source_paths = source_manifest(root, config)
    info_prefix = prefix + ".dist-info/"
    metadata_names = {info_prefix + name for name in
                      ("METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt")}
    with zipfile.ZipFile(wheel) as archive:
        names = set()
        total = 0
        for item in archive.infolist():
            _safe_name(item.filename)
            if item.filename in names:
                raise ValueError(f"Duplicate wheel member: {item.filename}")
            names.add(item.filename)
            mode = stat.S_IFMT(item.external_attr >> 16)
            if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError(f"Linked or special wheel member: {item.filename}")
            total += item.file_size
            if len(names) > 50_000 or total > 256 * 1024 * 1024:
                raise ValueError("Wheel exceeds the release manifest safety limits")
            if item.is_dir():
                raise ValueError(f"Unexpected directory entry in source wheel: {item.filename}")
            if item.filename in expected or item.filename in metadata_names:
                continue
            license_name = item.filename.removeprefix(info_prefix)
            if (item.filename.startswith(info_prefix) and
                    (license_name.startswith("licenses/") or re.fullmatch(r"(?i)(LICENSE|COPYING)(\.[A-Za-z0-9_-]+)?", license_name))):
                # License metadata must still originate in this checkout.
                source = root / (license_name.removeprefix("licenses/"))
                if archive.read(item) != _source_bytes(root, source):
                    raise ValueError(f"License metadata differs from checkout: {item.filename}")
                continue
            raise ValueError(f"Unexpected wheel member: {item.filename}")
        missing = set(expected) - names
        if missing:
            raise ValueError("Missing registered release files: " + ", ".join(sorted(missing)))
        for name, data in expected.items():
            if archive.read(name) != data:
                raise ValueError(f"Wheel source differs from checkout: {name}")
        required_metadata = {info_prefix + name for name in ("METADATA", "WHEEL", "RECORD")}
        if not required_metadata <= names:
            raise ValueError("Wheel metadata is incomplete")
        metadata = BytesParser().parsebytes(archive.read(info_prefix + "METADATA"))
        if metadata["Name"] != config["project"]["name"] or metadata["Version"] != config["project"]["version"]:
            raise ValueError("Wheel distribution name/version differs from pyproject.toml")
        scripts = config["project"].get("scripts", {})
        if scripts:
            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read_string(archive.read(info_prefix + "entry_points.txt").decode("utf-8"))
            if not parser.has_section("console_scripts") or dict(parser["console_scripts"]) != scripts:
                raise ValueError("Registered console scripts differ from wheel entry points")
        record_name = info_prefix + "RECORD"
        record = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
        record_names = set()
        file_names = {item.filename for item in archive.infolist() if not item.is_dir()}
        for row in record:
            if len(row) != 3 or row[0] not in file_names or row[0] in record_names:
                raise ValueError("Invalid or duplicate wheel RECORD row")
            name, digest, size = row
            record_names.add(name)
            if name == record_name:
                if digest or size:
                    raise ValueError("Wheel RECORD self-entry must omit hash and size")
                continue
            data = archive.read(name)
            expected_digest = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
            if digest != expected_digest or size != str(len(data)):
                raise ValueError(f"Wheel RECORD integrity failed: {name}")
        if record_names != file_names or archive.testzip() is not None:
            raise ValueError("Wheel RECORD does not cover its complete manifest")
    return {"distribution": config["project"]["name"], "version": config["project"]["version"],
            "wheel": wheel.name, "bytes": wheel.stat().st_size,
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "verified_source_files": len(expected), "wheel_files": len(file_names),
            "source_sha256": {source_paths[name]: hashlib.sha256(data).hexdigest() for name, data in sorted(expected.items())},
            "checks": {"no_bytecode": True, "no_links_or_unsafe_paths": True,
                       "registered_sources_and_assets_match": True, "console_scripts_match": True, "record_complete_and_valid": True}}


def build_wheel(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    initial_config_bytes = (root / "pyproject.toml").read_bytes()
    config = _config(root)
    _, initial_sources, _ = source_manifest(root, config)
    output.mkdir(parents=True, exist_ok=True)
    # All ephemeral writes, including egg metadata, are confined to this owned
    # temporary directory. Existing checkout build/ and *.egg-info are untouched.
    with tempfile.TemporaryDirectory(prefix="wheel-build-", dir=output) as temporary:
        work = Path(temporary)
        (work / "egg-info").mkdir()
        command = [sys.executable, "-c", "from setuptools import setup; setup()",
                   "egg_info", "--egg-base", str(work / "egg-info"),
                   "build", "--build-base", str(work / "build"),
                   "bdist_wheel", "--dist-dir", str(work / "dist"), "--bdist-dir", str(work / "bdist")]
        result = subprocess.run(command, cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", timeout=180)
        if result.returncode:
            raise RuntimeError("Offline wheel build failed:\n" + result.stdout[-6000:])
        wheels = list((work / "dist").glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("Expected exactly one built wheel")
        if ((root / "pyproject.toml").read_bytes() != initial_config_bytes
                or source_manifest(root, config)[1] != initial_sources):
            raise RuntimeError("Release sources changed during the build; rerun on a stable checkout")
        wheel = wheels[0]
        report = verify_wheel(wheel, root, config)
        report["checks"]["isolated_build_tree"] = True
        report["tooling"] = {name: importlib.metadata.version(name) for name in ("setuptools", "wheel")}
        report_path = work / (wheel.stem + ".verification.json")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        destination, report_destination = output / wheel.name, output / report_path.name
        if destination.exists() or report_destination.exists() or destination.is_symlink() or report_destination.is_symlink():
            raise FileExistsError("Output exists; choose a new output directory to preserve the earlier release")
        # Hard links publish complete files atomically and refuse an existing
        # destination on both POSIX and Windows; temporary cleanup drops only
        # the staging links. No caller-owned directory is recursively removed.
        os.link(wheel, destination)
        try:
            os.link(report_path, report_destination)
        except OSError:
            destination.unlink()  # Only the link just created by this call.
            raise
    return {"wheel": destination, "verification": report_destination, "report": report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    result = build_wheel(args.project, args.output)
    print(result["wheel"])
    print(result["verification"])


if __name__ == "__main__":
    main()
