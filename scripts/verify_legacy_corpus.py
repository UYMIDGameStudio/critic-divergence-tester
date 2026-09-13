"""Copy authorized historical Gate projects and replay only their isolated copies.

The originals and immutable corpus locators are never rewritten. The existing
Gate snapshot functions receive a documented, in-memory locator mapping so their
normal hash, assessment, decision and report checks use only copied workspaces.
No model execution or network request is allowed. Output includes private copies;
keep it local and do not add the directory to a public release artifact.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from unittest.mock import patch

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import argument_adjudication
import argument_citations
import argument_gate
import argument_gate_b
import argument_lineage
import argument_perspective
import argument_resolution
import argument_review
import argument_triage
import argument_versioning
import argument_workbench as workbench


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def inventory(root: Path) -> dict[str, dict]:
    """Read regular files without traversing symlinks or Windows reparse points."""
    result = {}
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"Linked/reparse entry is not allowed: {path}")
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
        elif stat.S_ISREG(info.st_mode):
            data = path.read_bytes()
            record = {"sha256": digest(data), "bytes": len(data)}
            if path.suffix == ".json":
                try:
                    value = json.loads(data)
                    if isinstance(value, dict):
                        record["artifact"] = value.get("artifact", value.get("artifact_type"))
                except (ValueError, UnicodeError):
                    pass  # Raw invalid model attempts are valid historical evidence.
            result[path.relative_to(root).as_posix()] = record
        else:
            raise ValueError(f"Non-regular entry is not allowed: {path}")
    return dict(sorted(result.items()))


def identity(value: Path | str) -> str:
    return os.path.normcase(str(Path(value).resolve()))


def install_write_guard(output: Path, blocked: list[dict]) -> None:
    def allow(path):
        if isinstance(path, int):
            return  # stdout/stderr and descriptors already opened inside this process.
        candidate = Path(os.fsdecode(path)).resolve()
        if not candidate.is_relative_to(output):
            blocked.append({"path": str(candidate), "reason": "write outside isolated output"})
            raise PermissionError(f"Legacy replay cannot write outside isolated output: {candidate}")

    def guard(event, args):
        if event == "open":
            mode, flags = args[1], args[2]
            if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            ):
                allow(args[0])
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate"}:
            allow(args[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            allow(args[0]); allow(args[1])
        elif event in {"subprocess.Popen", "socket.connect"}:
            raise PermissionError("Legacy replay may not execute a model/process or connect to a network")
    sys.addaudithook(guard)


def code_inventory() -> dict:
    files = set(REPO.glob("*.py"))
    for directory in ("cli", "contracts"):
        files.update((REPO / directory).rglob("*.py"))
    files.update([Path(__file__).resolve(), REPO / "pyproject.toml"])
    return {path.relative_to(REPO).as_posix(): digest(path.read_bytes()) for path in sorted(files)}


def categories(path: str) -> list[str]:
    groups = []
    if "/source/" in path or path.endswith("document-version.json"):
        groups.append("source-and-version")
    if any(piece in path for piece in ("/raw-ir/", "/reviewed-ir/", "/corrections/", "argument-ir", "argument-map", "extraction-prompt")):
        groups.append("IR")
    if any(piece in path for piece in ("/reviews/", "/perspective-reviews/", "lens-protocol")):
        groups.append("Lens-and-review")
    if any(piece in path for piece in ("/adjudications/", "/revision-actions/", "/revision-plan/")):
        groups.append("adjudication-and-action")
    if "/lineage/" in path:
        groups.append("Lineage")
    if "/finding-resolutions/" in path:
        groups.append("Resolution")
    if "/citation-audits/" in path:
        groups.append("Citation")
    if "/version-diffs/" in path or "history" in path:
        groups.append("history-and-diff")
    return groups or ["other-project-evidence"]


def checked_call(name, callback, records: list[dict], *, rebuild=False):
    try:
        result = callback()
        if rebuild:
            outputs, changed = result
            record = {"check": name, "passed": not changed, "changed": changed}
            if changed:
                record["errors"] = ["Rebuild changed historical derived bytes; compatibility review is required."]
        else:
            record = {"check": name, "passed": not result, "errors": result}
    except Exception as exc:
        record = {"check": name, "passed": False, "errors": [f"{type(exc).__name__}: {exc}"]}
    records.append(record)
    return record["passed"]


def replay_project(copy: Path) -> list[dict]:
    checks = []
    before_ok = checked_call("verify_project_versions:before", lambda: workbench.verify_project_versions(copy), checks)
    if not before_ok:
        checks.append({"check": "replay", "passed": False, "errors": ["Not replayed because the untouched copy failed verification."]})
        return checks
    versions = workbench.list_version_ids(copy)
    for version in versions:
        workspace = workbench.WorkspacePaths(copy, version)
        for name, function in (
            ("IR", workbench.rebuild_workspace),
            ("Rule", argument_review.rebuild_reviews),
            ("Perspective", argument_perspective.rebuild_perspective_reviews),
            ("status-triage", argument_triage.rebuild_status_triages),
            ("adjudication-and-plan", argument_adjudication.rebuild_adjudication_cache),
        ):
            checked_call(f"replay:{version}:{name}", lambda f=function, w=workspace: f(w), checks, rebuild=True)
    for name, function in (
        ("structural-diff", argument_versioning.rebuild_structural_diffs),
        ("Lineage", argument_lineage.rebuild_lineage_analyses),
        ("Resolution", argument_resolution.rebuild_resolutions),
        ("Citation", argument_citations.rebuild_citation_audits),
    ):
        checked_call("replay:" + name, lambda f=function: f(copy), checks, rebuild=True)
    checked_call("verify_project_versions:after", lambda: workbench.verify_project_versions(copy), checks)
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate-a", type=Path, default=Path("D:/agent/gate-a-author-input/author-corpus.product-gate-a"))
    parser.add_argument("--gate-b", type=Path, default=Path("D:/agent/gate-b-author-input/product-gate-b"))
    parser.add_argument("--output", type=Path, default=REPO / "dist" / "legacy-replay-0.2.2")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(REPO / "dist") or output.exists():
        parser.error("output must be a new directory inside this checkout's dist directory")
    code_before = code_inventory()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    output.mkdir(parents=True)
    blocked = []
    install_write_guard(output, blocked)
    report = {"schema_version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
              "git_head": commit, "working_tree_code_sha256": code_before, "python": sys.version,
              "package_version": re.search(r'^version = "([^"]+)"', (REPO / "pyproject.toml").read_text(), re.M).group(1),
              "evidence_kind": "replay of existing author-owned artifacts; no new human/model decisions",
              "locator_policy": "immutable corpus bytes preserved; in-memory snapshot lookup maps original project paths to isolated copies",
              "output": str(output), "projects": [], "gates": [], "blocked_external_writes": blocked}
    mapping, original_snapshots = {}, []
    for label, original in (("gate-a", args.gate_a.resolve()), ("gate-b", args.gate_b.resolve())):
        before = inventory(original)
        original_snapshots.append((original, before))
        gate_copy = output / label / "corpus"
        shutil.copytree(original, gate_copy)
        if inventory(gate_copy) != before:
            raise ValueError(f"Copied {label} corpus differs from its original")
        corpus = json.loads((gate_copy / "corpus.json").read_bytes())
        entries = corpus.get("entries", corpus.get("projects", []))
        for entry in entries:
            source = Path(entry.get("workspace_locator", entry.get("locator"))).resolve()
            source_before = inventory(source)
            original_snapshots.append((source, source_before))
            copy = output / label / "projects" / entry["alias"]
            shutil.copytree(source, copy)
            copied = inventory(copy)
            if copied != source_before:
                raise ValueError(f"Copied {label}/{entry['alias']} differs from its original")
            mapping[identity(source)] = copy
            checks = replay_project(copy)
            after = inventory(copy)
            rows = [{"path": name, "bytes": metadata["bytes"], "artifact": metadata.get("artifact"),
                     "original_sha256": metadata["sha256"], "copied_sha256": copied[name]["sha256"],
                     "replayed_sha256": after.get(name, {}).get("sha256"), "chains": categories(name)}
                    for name, metadata in source_before.items()]
            changed = [row["path"] for row in rows if row["original_sha256"] != row["replayed_sha256"]]
            added = sorted(set(after) - set(source_before))
            runtime_only = all(Path(name).name == ".mutation.lock" for name in added)
            project = {"alias": f"{label}/{entry['alias']}", "original_locator": str(source),
                       "copied_locator": str(copy), "file_count": len(rows), "checks": checks,
                       "changed_historical_files": changed, "added_runtime_files": added,
                       "source_hashes": [row for row in rows if "/source/" in row["path"]],
                       "artifact_counts": dict(Counter(row["artifact"] for row in rows if row["artifact"])),
                       "coverage": {
                           "versions": sum(row["path"].endswith("document-version.json") for row in rows),
                           "reviewed_IR_records": sum(row["path"].endswith("/reviewed-ir/record.json") for row in rows),
                           "Rule_library_snapshots": sum(row["artifact"] == "argument-check-library" for row in rows),
                           "Perspective_protocol_snapshots": sum(row["artifact"] == "perspective-lens-protocol" for row in rows),
                           "human_Lineage_decisions": sum("/lineage/" in row["path"] and "/human-decisions/" in row["path"] for row in rows),
                           "human_Resolution_decisions": sum("/finding-resolutions/" in row["path"] and "/human-decisions/" in row["path"] for row in rows),
                           "human_Citation_decisions": sum("/citation-audits/" in row["path"] and "/human-decisions/" in row["path"] for row in rows),
                       },
                       "files": rows, "passed": all(row["passed"] for row in checks) and not changed and runtime_only}
            report["projects"].append(project)
            print(f"{project['alias']}: {'PASS' if project['passed'] else 'FAIL'} ({len(rows)} files)", flush=True)
        gate_checks = []
        module, attr = (argument_gate, "_project_snapshot") if label == "gate-a" else (argument_gate_b, "_snapshot")
        original_snapshot = getattr(module, attr)
        def mapped_snapshot(locator, *positional, **keywords):
            destination = mapping.get(identity(locator))
            if destination is None:
                raise ValueError("Corpus points to an uncopied workspace")
            return original_snapshot(destination, *positional, **keywords)
        with patch.object(module, attr, mapped_snapshot):
            verify = argument_gate.verify_gate if label == "gate-a" else argument_gate_b.verify_gate_b
            rebuild = argument_gate.rebuild_gate_report if label == "gate-a" else argument_gate_b.rebuild_gate_b_report
            if checked_call("verify_gate:before", lambda: verify(gate_copy), gate_checks):
                checked_call("replay_gate_report", lambda: rebuild(gate_copy), gate_checks, rebuild=True)
                checked_call("verify_gate:after", lambda: verify(gate_copy), gate_checks)
        after = inventory(gate_copy)
        changed = [name for name in before if after.get(name) != before[name]]
        assessments = [json.loads(path.read_bytes()) for path in sorted((gate_copy / "assessments").glob("*.json"))]
        report["gates"].append({"gate": label, "corpus_sha256": before["corpus.json"]["sha256"],
                                "checks": gate_checks, "historical_bytes_unchanged": not changed,
                                "changed_files": changed, "artifact_hashes": before,
                                "existing_human_assessments": len(assessments),
                                "existing_regression_anchors": sum(len(item.get("regression_anchors", [])) for item in assessments),
                                "passed": all(row["passed"] for row in gate_checks) and not changed})
    report["originals_unchanged"] = all(inventory(source) == before for source, before in original_snapshots)
    report["working_tree_code_unchanged_during_run"] = code_inventory() == code_before
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["passed"] = all(row["passed"] for row in report["projects"] + report["gates"]) and report["originals_unchanged"] and not blocked and report["working_tree_code_unchanged_during_run"]
    (output / "report.json").write_bytes(json_bytes(report))
    lines = ["# Historical Gate A/B replay", "", f"Package: {report['package_version']}; overall: {'PASS' if report['passed'] else 'FAIL'}.",
             "", "Existing author evidence only. No new model result, human assessment or Gate decision was created.",
             "Immutable corpus locators were mapped to copies in memory; corpus bytes were not edited.", "",
             "| Project | Files | Source/IR/Lens/Lineage/Resolution/Citation verification and replay | Historical files changed |", "| --- | ---: | --- | ---: |"]
    for project in report["projects"]:
        lines.append(f"| {project['alias']} | {project['file_count']} | {'PASS' if project['passed'] else 'FAIL'} | {len(project['changed_historical_files'])} |")
    lines.extend(["", "| Project | Versions / Reviewed IR | Rule / Perspective snapshots | Human Lineage | Human Resolution | Human Citation |",
                  "| --- | --- | --- | ---: | ---: | ---: |"])
    for project in report["projects"]:
        coverage = project["coverage"]
        lines.append(f"| {project['alias']} | {coverage['versions']} / {coverage['reviewed_IR_records']} | {coverage['Rule_library_snapshots']} / {coverage['Perspective_protocol_snapshots']} | {coverage['human_Lineage_decisions']} | {coverage['human_Resolution_decisions']} | {coverage['human_Citation_decisions']} |")
    lines.extend(["", f"Original trees unchanged: {report['originals_unchanged']}.",
                  f"Framework code unchanged during run: {report['working_tree_code_unchanged_during_run']}.",
                  "", "See report.json for every source/artifact hash, check result and any missing or failed coverage.",
                  "No artifacts in a chain means that chain is absent in that project, not evidence that a real example passed."])
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {output / 'report.json'}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
