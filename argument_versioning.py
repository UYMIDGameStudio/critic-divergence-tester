"""Deterministic structural comparison between immutable manuscript versions."""

from __future__ import annotations

import difflib
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argument_contracts import sha256_bytes, validate_artifact
from argument_ir import validate_argument_ir
from argument_workbench import (
    WorkspacePaths,
    WorkbenchError,
    _atomic_write,
    _parent,
    _provenance,
    _read_json,
    _workspace_source,
    json_bytes,
    list_version_ids,
    verify_workspace,
    workspace_paths,
)


DIFF_ID_PATTERN = re.compile(r"(V[1-9][0-9]*)--(V[1-9][0-9]*)\Z")
NODE_FIELDS = (
    ("claim", "claims"),
    ("evidence", "evidence"),
    ("assumption", "assumptions"),
    ("citation", "citations"),
)


@dataclass(frozen=True)
class StructuralDiffPaths:
    root_paths: WorkspacePaths
    from_version: str
    to_version: str

    @property
    def diff_id(self) -> str:
        return f"{self.from_version}--{self.to_version}"

    @property
    def diffs_dir(self) -> Path:
        return self.root_paths.document_dir / "version-diffs"

    @property
    def root(self) -> Path:
        return self.diffs_dir / self.diff_id

    @property
    def record(self) -> Path:
        return self.root / "structural-diff.json"

    @property
    def markdown(self) -> Path:
        return self.root / "structural-diff.md"


def _fingerprint(value: object) -> str:
    data = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(data)


def _node_anchor(kind: str, node: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "text": node["text"],
        "source_quote": node["source_quote"],
    }


def _node_content(kind: str, node: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        **{
            key: value
            for key, value in node.items()
            if key not in {"id", "position"}
        },
    }


def _versioned(version_id: str, local_ref: str) -> str:
    return f"{version_id}:{local_ref}"


def _node_diff(
    from_ir: dict[str, Any],
    to_ir: dict[str, Any],
    from_version: str,
    to_version: str,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], dict[str, str]]:
    from_nodes: list[tuple[str, dict[str, Any]]] = [
        (kind, node)
        for kind, field in NODE_FIELDS
        for node in from_ir[field]
    ]
    to_nodes: list[tuple[str, dict[str, Any]]] = [
        (kind, node)
        for kind, field in NODE_FIELDS
        for node in to_ir[field]
    ]
    from_anchor = {
        str(node["id"]): _fingerprint(_node_anchor(kind, node))
        for kind, node in from_nodes
    }
    to_anchor = {
        str(node["id"]): _fingerprint(_node_anchor(kind, node))
        for kind, node in to_nodes
    }
    to_by_content: dict[str, deque[int]] = defaultdict(deque)
    for index, (kind, node) in enumerate(to_nodes):
        to_by_content[_fingerprint(_node_content(kind, node))].append(index)
    matched_from: set[int] = set()
    matched_to: set[int] = set()
    exact: list[dict[str, Any]] = []
    for from_index, (kind, node) in enumerate(from_nodes):
        fingerprint = _fingerprint(_node_content(kind, node))
        candidates = to_by_content.get(fingerprint)
        while candidates and candidates[0] in matched_to:
            candidates.popleft()
        if not candidates:
            continue
        to_index = candidates.popleft()
        to_kind, to_node = to_nodes[to_index]
        matched_from.add(from_index)
        matched_to.add(to_index)
        exact.append(
            {
                "kind": kind,
                "from_ref": _versioned(from_version, str(node["id"])),
                "to_ref": _versioned(to_version, str(to_node["id"])),
                "fingerprint": fingerprint,
            }
        )

    to_by_anchor: dict[str, deque[int]] = defaultdict(deque)
    for index, (kind, node) in enumerate(to_nodes):
        if index not in matched_to:
            to_by_anchor[_fingerprint(_node_anchor(kind, node))].append(index)
    modified: list[dict[str, Any]] = []
    for from_index, (kind, node) in enumerate(from_nodes):
        if from_index in matched_from:
            continue
        anchor = _fingerprint(_node_anchor(kind, node))
        candidates = to_by_anchor.get(anchor)
        while candidates and candidates[0] in matched_to:
            candidates.popleft()
        if not candidates:
            continue
        to_index = candidates.popleft()
        _, to_node = to_nodes[to_index]
        matched_from.add(from_index)
        matched_to.add(to_index)
        from_content = _node_content(kind, node)
        to_content = _node_content(kind, to_node)
        changed_fields = sorted(
            key
            for key in set(from_content) | set(to_content)
            if from_content.get(key) != to_content.get(key)
        )
        modified.append(
            {
                "kind": kind,
                "from_ref": _versioned(from_version, str(node["id"])),
                "to_ref": _versioned(to_version, str(to_node["id"])),
                "anchor_fingerprint": anchor,
                "changed_fields": changed_fields,
            }
        )

    removed = [
        {
            "kind": kind,
            "ref": _versioned(from_version, str(node["id"])),
            "fingerprint": _fingerprint(_node_content(kind, node)),
        }
        for index, (kind, node) in enumerate(from_nodes)
        if index not in matched_from
    ]
    added = [
        {
            "kind": kind,
            "ref": _versioned(to_version, str(node["id"])),
            "fingerprint": _fingerprint(_node_content(kind, node)),
        }
        for index, (kind, node) in enumerate(to_nodes)
        if index not in matched_to
    ]
    return (
        {
            "exact_unchanged": exact,
            "literal_anchor_modified": modified,
            "removed": removed,
            "added": added,
        },
        from_anchor,
        to_anchor,
    )


def _relation_fingerprint(
    relation: dict[str, Any], node_anchors: dict[str, str]
) -> str:
    return _fingerprint(
        {
            "type": relation["type"],
            "from_anchor": node_anchors[relation["from"]],
            "to_anchor": node_anchors[relation["to"]],
        }
    )


def _relation_diff(
    from_ir: dict[str, Any],
    to_ir: dict[str, Any],
    from_version: str,
    to_version: str,
    from_anchors: dict[str, str],
    to_anchors: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    from_relations = list(from_ir["relations"])
    to_relations = list(to_ir["relations"])
    to_by_fingerprint: dict[str, deque[int]] = defaultdict(deque)
    for index, relation in enumerate(to_relations):
        to_by_fingerprint[
            _relation_fingerprint(relation, to_anchors)
        ].append(index)
    matched_from: set[int] = set()
    matched_to: set[int] = set()
    exact: list[dict[str, Any]] = []
    for from_index, relation in enumerate(from_relations):
        fingerprint = _relation_fingerprint(relation, from_anchors)
        candidates = to_by_fingerprint.get(fingerprint)
        if not candidates:
            continue
        to_index = candidates.popleft()
        matched_from.add(from_index)
        matched_to.add(to_index)
        exact.append(
            {
                "from_ref": _versioned(from_version, str(relation["id"])),
                "to_ref": _versioned(
                    to_version, str(to_relations[to_index]["id"])
                ),
                "fingerprint": fingerprint,
            }
        )
    removed = [
        {
            "ref": _versioned(from_version, str(relation["id"])),
            "fingerprint": _relation_fingerprint(relation, from_anchors),
        }
        for index, relation in enumerate(from_relations)
        if index not in matched_from
    ]
    added = [
        {
            "ref": _versioned(to_version, str(relation["id"])),
            "fingerprint": _relation_fingerprint(relation, to_anchors),
        }
        for index, relation in enumerate(to_relations)
        if index not in matched_to
    ]
    return {"exact_unchanged": exact, "removed": removed, "added": added}


def _source_diff(from_bytes: bytes, to_bytes: bytes) -> dict[str, Any]:
    from_lines = from_bytes.decode("utf-8").splitlines(keepends=True)
    to_lines = to_bytes.decode("utf-8").splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=from_lines, b=to_lines, autojunk=False)
    hunks = [
        {
            "tag": tag,
            "from_start_index": from_start,
            "from_end_index": from_end,
            "to_start_index": to_start,
            "to_end_index": to_end,
            "from_lines": from_lines[from_start:from_end],
            "to_lines": to_lines[to_start:to_end],
        }
        for tag, from_start, from_end, to_start, to_end in matcher.get_opcodes()
        if tag != "equal"
    ]
    return {
        "from_sha256": sha256_bytes(from_bytes),
        "to_sha256": sha256_bytes(to_bytes),
        "changed": bool(hunks),
        "hunks": hunks,
    }


def render_structural_diff(record: dict[str, Any]) -> str:
    summary = record["summary"]
    lines = [
        "# Structural Version Diff",
        "",
        f"- Document: `{record['document_id']}`",
        f"- Versions: `{record['from_version']}` → `{record['to_version']}`",
        "- Every result below is deterministic exact comparison, not semantic Claim lineage.",
        "- Removed + added does not prove that two Claims are unrelated; semantic correspondence requires a separate proposal and human confirmation.",
        "",
        "## Summary",
        "",
        f"- Source hunks: {summary['source_hunks']}",
        f"- Nodes exact unchanged: {summary['nodes_exact_unchanged']}",
        f"- Nodes with the same literal anchor but changed fields: {summary['nodes_literal_anchor_modified']}",
        f"- Nodes removed / added: {summary['nodes_removed']} / {summary['nodes_added']}",
        f"- Evidence added: {summary['evidence_added']}",
        f"- Relations exact unchanged: {summary['relations_exact_unchanged']}",
        f"- Relations removed / added: {summary['relations_removed']} / {summary['relations_added']}",
        "",
        "## Source changes",
        "",
    ]
    for index, hunk in enumerate(record["source_diff"]["hunks"], 1):
        lines.extend(
            [
                f"### H{index:04d} — `{hunk['tag']}`",
                "",
                f"- From line indexes: `{hunk['from_start_index']}:{hunk['from_end_index']}`",
                f"- To line indexes: `{hunk['to_start_index']}:{hunk['to_end_index']}`",
                "- Removed exact lines:",
                "```text",
                "".join(hunk["from_lines"]).rstrip("\n\r"),
                "```",
                "- Added exact lines:",
                "```text",
                "".join(hunk["to_lines"]).rstrip("\n\r"),
                "```",
                "",
            ]
        )
    if not record["source_diff"]["hunks"]:
        lines.extend(["None.", ""])

    node_diff = record["node_diff"]
    lines.extend(["## Argument IR nodes", ""])
    for status, heading in (
        ("exact_unchanged", "Exact unchanged"),
        ("literal_anchor_modified", "Literal anchor retained; fields changed"),
        ("removed", "Removed"),
        ("added", "Added"),
    ):
        lines.extend([f"### {heading}", ""])
        entries = node_diff[status]
        if not entries:
            lines.extend(["None.", ""])
            continue
        for entry in entries:
            if status in {"exact_unchanged", "literal_anchor_modified"}:
                suffix = (
                    " — changed: " + ", ".join(entry["changed_fields"])
                    if status == "literal_anchor_modified"
                    else ""
                )
                lines.append(
                    f"- `{entry['from_ref']}` → `{entry['to_ref']}` ({entry['kind']}){suffix}"
                )
            else:
                lines.append(f"- `{entry['ref']}` ({entry['kind']})")
        lines.append("")

    relation_diff = record["relation_diff"]
    lines.extend(["## Relations", ""])
    for status, heading in (
        ("exact_unchanged", "Exact unchanged"),
        ("removed", "Removed"),
        ("added", "Added"),
    ):
        lines.extend([f"### {heading}", ""])
        entries = relation_diff[status]
        if not entries:
            lines.extend(["None.", ""])
            continue
        for entry in entries:
            if status == "exact_unchanged":
                lines.append(f"- `{entry['from_ref']}` → `{entry['to_ref']}`")
            else:
                lines.append(f"- `{entry['ref']}`")
        lines.append("")
    return "\n".join(lines)


def _derive_structural_diff(
    project_dir: WorkspacePaths | Path | str,
    from_version: str,
    to_version: str,
) -> tuple[StructuralDiffPaths, dict[str, Any], bytes, bytes]:
    root_paths = workspace_paths(project_dir)
    from_paths = workspace_paths(root_paths, from_version)
    to_paths = workspace_paths(root_paths, to_version)
    for paths in (from_paths, to_paths):
        errors = verify_workspace(paths)
        if errors:
            raise WorkbenchError(
                f"{paths.version_id} is invalid: " + "; ".join(errors)
            )
        if not paths.reviewed_payload.is_file() or paths.reviewed_payload.is_symlink():
            raise WorkbenchError(
                f"{paths.version_id} requires Reviewed IR before structural diff"
            )
    from_record, from_record_bytes = _read_json(from_paths.version)
    to_record, to_record_bytes = _read_json(to_paths.version)
    if to_record.get("parent_version") != from_version:
        raise WorkbenchError(
            f"{to_version} must directly descend from {from_version} for this diff"
        )
    from_reviewed, from_reviewed_bytes = _read_json(from_paths.reviewed_record)
    to_reviewed, to_reviewed_bytes = _read_json(to_paths.reviewed_record)
    from_ir, from_ir_bytes = _read_json(from_paths.reviewed_payload)
    to_ir, to_ir_bytes = _read_json(to_paths.reviewed_payload)
    for label, ir in ((from_version, from_ir), (to_version, to_ir)):
        errors = validate_argument_ir(ir)
        if errors:
            raise WorkbenchError(f"{label} Reviewed IR is invalid: " + "; ".join(errors))
    _, from_source_bytes, _ = _workspace_source(from_paths)
    _, to_source_bytes, _ = _workspace_source(to_paths)
    node_diff, from_anchors, to_anchors = _node_diff(
        from_ir, to_ir, from_version, to_version
    )
    relation_diff = _relation_diff(
        from_ir,
        to_ir,
        from_version,
        to_version,
        from_anchors,
        to_anchors,
    )
    source_diff = _source_diff(from_source_bytes, to_source_bytes)
    summary = {
        "source_hunks": len(source_diff["hunks"]),
        "nodes_exact_unchanged": len(node_diff["exact_unchanged"]),
        "nodes_literal_anchor_modified": len(
            node_diff["literal_anchor_modified"]
        ),
        "nodes_removed": len(node_diff["removed"]),
        "nodes_added": len(node_diff["added"]),
        "evidence_added": sum(
            1 for entry in node_diff["added"] if entry["kind"] == "evidence"
        ),
        "relations_exact_unchanged": len(relation_diff["exact_unchanged"]),
        "relations_removed": len(relation_diff["removed"]),
        "relations_added": len(relation_diff["added"]),
    }
    paths = StructuralDiffPaths(root_paths, from_version, to_version)
    record = {
        "schema_version": 1,
        "artifact": "structural-version-diff",
        "artifact_id": paths.diff_id,
        "lifecycle": "derived-replaceable",
        "provenance": _provenance(
            "deterministic",
            str(to_reviewed["provenance"]["created_at"]),
            "workbench-structural-diff-v1",
        ),
        "parents": [
            _parent("from-version", "document-version", from_record_bytes),
            _parent("to-version", "document-version", to_record_bytes),
            _parent(
                "from-reviewed", "reviewed-argument-ir", from_reviewed_bytes
            ),
            _parent("to-reviewed", "reviewed-argument-ir", to_reviewed_bytes),
            _parent("from-ir", "argument-ir", from_ir_bytes),
            _parent("to-ir", "argument-ir", to_ir_bytes),
        ],
        "diff_id": paths.diff_id,
        "document_id": str(to_record["document_id"]),
        "from_version": from_version,
        "to_version": to_version,
        "source_diff": source_diff,
        "node_diff": node_diff,
        "relation_diff": relation_diff,
        "summary": summary,
        "payload": {"relative_path": "structural-diff.md", "sha256": "0" * 64},
        "field_provenance": {
            "source_diff": {
                "origin": "deterministic",
                "source": "exact source line comparison",
            },
            "node_diff": {
                "origin": "deterministic",
                "source": "exact node content and literal-anchor fingerprints",
            },
            "relation_diff": {
                "origin": "deterministic",
                "source": "exact relation and endpoint-anchor fingerprints",
            },
            "summary": {
                "origin": "deterministic",
                "source": "structural diff counts",
            },
            "payload": {
                "origin": "deterministic",
                "source": "workbench-structural-diff-v1",
            },
        },
    }
    markdown_bytes = render_structural_diff(record).encode("utf-8")
    record["payload"]["sha256"] = sha256_bytes(markdown_bytes)
    markdown_bytes = render_structural_diff(record).encode("utf-8")
    errors = validate_artifact(record)
    if errors:
        raise WorkbenchError(
            "internal structural diff contract error: " + "; ".join(errors)
        )
    return paths, record, json_bytes(record), markdown_bytes


def build_structural_diff(
    project_dir: WorkspacePaths | Path | str,
    *,
    from_version: str | None = None,
    to_version: str | None = None,
) -> tuple[StructuralDiffPaths, bool]:
    versions = list_version_ids(project_dir)
    if len(versions) < 2:
        raise WorkbenchError("structural diff requires at least two DocumentVersions")
    selected_to = to_version.upper() if to_version is not None else versions[-1]
    if selected_to not in versions:
        raise WorkbenchError(f"unknown to-version: {selected_to}")
    to_index = versions.index(selected_to)
    if to_index == 0:
        raise WorkbenchError("V1 has no parent version to compare")
    selected_from = (
        from_version.upper() if from_version is not None else versions[to_index - 1]
    )
    paths, _, record_bytes, markdown_bytes = _derive_structural_diff(
        project_dir, selected_from, selected_to
    )
    if paths.root.is_symlink():
        raise WorkbenchError("structural diff directory must not be a symlink")
    paths.root.mkdir(parents=True, exist_ok=True)
    changed = False
    for path, data in (
        (paths.record, record_bytes),
        (paths.markdown, markdown_bytes),
    ):
        if path.exists() and path.is_symlink():
            raise WorkbenchError(f"structural diff artifact must not be a symlink: {path}")
        if not path.exists() or path.read_bytes() != data:
            _atomic_write(path, data)
            changed = True
    return paths, changed


def list_structural_diffs(
    project_dir: WorkspacePaths | Path | str,
) -> list[StructuralDiffPaths]:
    root_paths = workspace_paths(project_dir)
    diffs_dir = root_paths.document_dir / "version-diffs"
    if not diffs_dir.exists():
        return []
    if diffs_dir.is_symlink() or not diffs_dir.is_dir():
        raise WorkbenchError("version-diffs must be a regular directory")
    results: list[StructuralDiffPaths] = []
    for child in diffs_dir.iterdir():
        if child.is_symlink():
            raise WorkbenchError(f"structural diff must not be a symlink: {child}")
        match = DIFF_ID_PATTERN.fullmatch(child.name)
        if not child.is_dir() or match is None:
            raise WorkbenchError(f"unexpected version-diffs entry: {child.name}")
        results.append(
            StructuralDiffPaths(root_paths, match.group(1), match.group(2))
        )
    return sorted(
        results,
        key=lambda item: (int(item.from_version[1:]), int(item.to_version[1:])),
    )


def verify_structural_diffs(
    project_dir: WorkspacePaths | Path | str,
) -> list[str]:
    errors: list[str] = []
    try:
        diffs = list_structural_diffs(project_dir)
    except WorkbenchError as exc:
        return [str(exc)]
    for paths in diffs:
        prefix = paths.diff_id
        try:
            _, expected_record, record_bytes, markdown_bytes = _derive_structural_diff(
                paths.root_paths, paths.from_version, paths.to_version
            )
            actual_record, actual_record_bytes = _read_json(paths.record)
        except (OSError, WorkbenchError) as exc:
            errors.append(f"{prefix}: {exc}")
            continue
        errors.extend(
            f"{prefix}: {error}" for error in validate_artifact(actual_record)
        )
        if actual_record_bytes != record_bytes or actual_record != expected_record:
            errors.append(f"{prefix}: structural-diff.json is not reproducible")
        if paths.markdown.is_symlink() or not paths.markdown.is_file():
            errors.append(f"{prefix}: structural-diff.md is missing or unsafe")
        elif paths.markdown.read_bytes() != markdown_bytes:
            errors.append(f"{prefix}: structural-diff.md is not reproducible")
        if {child.name for child in paths.root.iterdir()} != {
            "structural-diff.json",
            "structural-diff.md",
        }:
            errors.append(f"{prefix}: structural diff directory has unexpected entries")
    return errors


def rebuild_structural_diffs(
    project_dir: WorkspacePaths | Path | str,
) -> tuple[list[Path], bool]:
    outputs: list[Path] = []
    changed = False
    for paths in list_structural_diffs(project_dir):
        rebuilt, item_changed = build_structural_diff(
            project_dir,
            from_version=paths.from_version,
            to_version=paths.to_version,
        )
        outputs.append(rebuilt.markdown)
        changed = changed or item_changed
    return outputs, changed
