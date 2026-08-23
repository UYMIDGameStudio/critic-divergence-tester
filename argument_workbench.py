"""Local-first Argument Workbench storage, correction replay, and rendering."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from argument_contracts import (
    sha256_bytes,
    validate_artifact,
    validate_contract_bundle,
    validate_ir_correction,
)
from argument_ir import (
    ASSUMPTION_KEYS,
    CITATION_KEYS,
    CLAIM_KEYS,
    CLAIM_TYPES,
    EVIDENCE_KEYS,
    IR_KEYS,
    METHOD_TYPES,
    RELATION_KEYS,
    SUPPORTED_IR_EXTRACTION_PROTOCOL_VERSIONS,
    ArgumentIRError,
    build_ir_extraction_prompt,
    canonicalize_argument_ir,
    validate_argument_ir,
)


DOCUMENT_ID = "D1"
VERSION_ID = "V1"
PASTE_END_MARKER = "::END::"
NODE_FIELDS = {
    "claim": "claims",
    "evidence": "evidence",
    "assumption": "assumptions",
    "citation": "citations",
}
NODE_PREFIXES = {"claim": "C", "evidence": "E", "assumption": "A", "citation": "Z"}
LIST_FIELDS = {"types", "methods"}


class WorkbenchError(ValueError):
    """Raised when a workspace cannot be changed without losing provenance."""


class DuplicateJsonKeyError(ValueError):
    pass


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateJsonKeyError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def parse_json_strict(data: bytes) -> object:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorkbenchError(f"JSON is not UTF-8: {exc}") from exc
    try:
        return json.loads(text, object_pairs_hook=_json_object)
    except (json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        raise WorkbenchError(f"response is not strict JSON: {exc}") from exc


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _write_new(path: Path, data: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise WorkbenchError(f"refusing to overwrite existing artifact: {path}")
    _atomic_write(path, data)


def _read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink():
        raise WorkbenchError(f"artifact must not be a symbolic link: {path}")
    data = path.read_bytes()
    value = parse_json_strict(data)
    if not isinstance(value, dict):
        raise WorkbenchError(f"artifact must be a JSON object: {path}")
    return value, data


def _provenance(origin: str, created_at: str, producer: str) -> dict[str, str]:
    return {"origin": origin, "created_at": created_at, "producer": producer}


def _parent(role: str, artifact: str, data: bytes) -> dict[str, str]:
    return {"role": role, "artifact": artifact, "sha256": sha256_bytes(data)}


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    version_id: str = VERSION_ID

    @property
    def project(self) -> Path:
        return self.root / "project.json"

    @property
    def document_dir(self) -> Path:
        return self.root / "documents" / DOCUMENT_ID

    @property
    def document(self) -> Path:
        return self.document_dir / "document.json"

    @property
    def versions_dir(self) -> Path:
        return self.document_dir / "versions"

    @property
    def version_dir(self) -> Path:
        return self.versions_dir / self.version_id

    @property
    def version(self) -> Path:
        return self.version_dir / "document-version.json"

    @property
    def prompt(self) -> Path:
        return self.version_dir / "extraction-prompt.md"

    @property
    def raw_dir(self) -> Path:
        return self.version_dir / "raw-ir"

    @property
    def corrections_dir(self) -> Path:
        return self.version_dir / "corrections"

    @property
    def reviewed_dir(self) -> Path:
        return self.version_dir / "reviewed-ir"

    @property
    def reviewed_payload(self) -> Path:
        return self.reviewed_dir / "argument-ir.json"

    @property
    def reviewed_record(self) -> Path:
        return self.reviewed_dir / "record.json"

    @property
    def argument_map(self) -> Path:
        return self.reviewed_dir / "argument-map.md"


def _validate_version_id(version_id: str) -> str:
    normalized = version_id.strip().upper()
    if re.fullmatch(r"V[1-9][0-9]*", normalized) is None:
        raise WorkbenchError("version ID must be V1..Vn")
    return normalized


def list_version_ids(raw: WorkspacePaths | Path | str) -> list[str]:
    root = raw.root if isinstance(raw, WorkspacePaths) else Path(raw).resolve()
    versions_dir = root / "documents" / DOCUMENT_ID / "versions"
    if not versions_dir.exists():
        return []
    if versions_dir.is_symlink() or not versions_dir.is_dir():
        raise WorkbenchError("versions must be a regular non-symlink directory")
    version_ids: list[str] = []
    for path in versions_dir.iterdir():
        if path.is_symlink():
            raise WorkbenchError(f"version directory must not be a symlink: {path}")
        if not path.is_dir() or re.fullmatch(r"V[1-9][0-9]*", path.name) is None:
            raise WorkbenchError(f"unexpected entry in versions directory: {path.name}")
        version_ids.append(path.name)
    return sorted(version_ids, key=lambda value: int(value[1:]))


def workspace_paths(
    raw: WorkspacePaths | Path | str,
    version_id: str | None = None,
) -> WorkspacePaths:
    if isinstance(raw, WorkspacePaths):
        if version_id is None:
            return raw
        return WorkspacePaths(raw.root, _validate_version_id(version_id))
    candidate = Path(raw)
    if candidate.is_symlink():
        raise WorkbenchError("project directory must not be a symbolic link")
    root = candidate.resolve()
    selected = _validate_version_id(version_id) if version_id is not None else None
    if selected is None:
        versions = list_version_ids(root)
        selected = versions[-1] if versions else VERSION_ID
    return WorkspacePaths(root, selected)


def _safe_source_name(name: str) -> bool:
    return (
        bool(name)
        and Path(name).name == name
        and "/" not in name
        and "\\" not in name
        and not any(ord(character) < 32 or ord(character) == 127 for character in name)
    )


def _matching_extraction_prompt_protocol(
    prompt_bytes: bytes,
    manuscript: str,
    *,
    source_name: str,
    source_sha256: str,
) -> int | None:
    for protocol_version in SUPPORTED_IR_EXTRACTION_PROTOCOL_VERSIONS:
        candidate = build_ir_extraction_prompt(
            manuscript,
            source_name=source_name,
            source_sha256=source_sha256,
            protocol_version=protocol_version,
        ).encode("utf-8")
        if prompt_bytes == candidate:
            return protocol_version
    return None


def initialize_workspace(
    manuscript: Path,
    project_dir: Path,
    *,
    title: str | None = None,
) -> WorkspacePaths:
    source_path = manuscript.resolve()
    if manuscript.is_symlink() or not source_path.is_file():
        raise WorkbenchError(f"manuscript must be a regular non-symlink file: {source_path}")
    source_bytes = source_path.read_bytes()
    try:
        source_text = source_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorkbenchError(f"manuscript is not UTF-8: {exc}") from exc
    if not source_text.strip():
        raise WorkbenchError("manuscript must not be empty")
    if not _safe_source_name(source_path.name):
        raise WorkbenchError("manuscript filename is not a safe basename")
    if title is not None and not title.strip():
        raise WorkbenchError("title must not be empty")

    target = project_dir.resolve()
    if project_dir.is_symlink():
        raise WorkbenchError("project path must not be a symbolic link")
    if target.exists():
        paths = WorkspacePaths(target)
        errors = verify_workspace(paths, allow_incomplete=True)
        if errors:
            raise WorkbenchError("existing project is invalid: " + "; ".join(errors))
        version, _ = _read_json(paths.version)
        archived_source = paths.version_dir / str(version["source"]["relative_path"])
        if (
            version["source"]["name"] != source_path.name
            or archived_source.read_bytes() != source_bytes
        ):
            raise WorkbenchError("existing project is bound to different manuscript bytes")
        return paths

    created_at = utc_now()
    project_title = title.strip() if title is not None else source_path.stem
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=parent))
    try:
        paths = WorkspacePaths(temporary)
        source_relative = PurePosixPath("source", source_path.name).as_posix()
        archived_source = paths.version_dir / Path(source_relative)
        archived_source.parent.mkdir(parents=True, exist_ok=True)
        _write_new(archived_source, source_bytes)

        project = {
            "schema_version": 1,
            "artifact": "argument-project",
            "artifact_id": "P1",
            "lifecycle": "immutable",
            "provenance": _provenance("human-confirmed", created_at, "local-user"),
            "parents": [],
            "project_id": "P1",
            "title": project_title,
        }
        project_bytes = json_bytes(project)
        document = {
            "schema_version": 1,
            "artifact": "argument-document",
            "artifact_id": DOCUMENT_ID,
            "lifecycle": "immutable",
            "provenance": _provenance("human-confirmed", created_at, "local-user"),
            "parents": [_parent("project", "argument-project", project_bytes)],
            "project_id": "P1",
            "document_id": DOCUMENT_ID,
            "title": project_title,
        }
        document_bytes = json_bytes(document)
        version = {
            "schema_version": 1,
            "artifact": "document-version",
            "artifact_id": VERSION_ID,
            "lifecycle": "immutable",
            "provenance": _provenance("human-confirmed", created_at, "local-user"),
            "parents": [_parent("document", "argument-document", document_bytes)],
            "project_id": "P1",
            "document_id": DOCUMENT_ID,
            "version_id": VERSION_ID,
            "source": {
                "name": source_path.name,
                "relative_path": source_relative,
                "sha256": sha256_bytes(source_bytes),
            },
            "parent_version": None,
        }
        version_bytes = json_bytes(version)
        for value in (project, document, version):
            errors = validate_artifact(value)
            if errors:
                raise WorkbenchError("internal contract error: " + "; ".join(errors))
        prompt = build_ir_extraction_prompt(
            source_text,
            source_name=source_path.name,
            source_sha256=sha256_bytes(source_bytes),
        ).encode("utf-8")
        _write_new(paths.project, project_bytes)
        _write_new(paths.document, document_bytes)
        _write_new(paths.version, version_bytes)
        _write_new(paths.prompt, prompt)
        paths.raw_dir.mkdir(parents=True, exist_ok=True)
        paths.corrections_dir.mkdir(parents=True, exist_ok=True)
        paths.reviewed_dir.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, target)
        temporary = target
    except Exception:
        if temporary.exists() and temporary != target:
            shutil.rmtree(temporary)
        raise
    return WorkspacePaths(target)


def import_document_version(
    project_dir: WorkspacePaths | Path | str,
    manuscript: Path | str,
    *,
    parent_version: str | None = None,
) -> WorkspacePaths:
    """Append one immutable manuscript version without changing earlier versions."""
    root_paths = workspace_paths(project_dir)
    versions = list_version_ids(root_paths)
    if not versions:
        raise WorkbenchError("project has no V1; initialize it before importing a version")
    expected_numbers = list(range(1, len(versions) + 1))
    if [int(value[1:]) for value in versions] != expected_numbers:
        raise WorkbenchError("version IDs must be continuous from V1")
    latest_id = versions[-1]
    selected_parent = (
        _validate_version_id(parent_version)
        if parent_version is not None
        else latest_id
    )
    if selected_parent != latest_id:
        raise WorkbenchError(
            f"new versions must descend from current {latest_id}; branching is not yet supported"
        )
    parent_paths = WorkspacePaths(root_paths.root, selected_parent)
    parent_errors = verify_workspace(parent_paths)
    if parent_errors:
        raise WorkbenchError(
            f"parent version {selected_parent} is invalid: " + "; ".join(parent_errors)
        )

    source_path = Path(manuscript)
    resolved_source = source_path.resolve()
    if source_path.is_symlink() or not resolved_source.is_file():
        raise WorkbenchError(
            f"manuscript must be a regular non-symlink file: {resolved_source}"
        )
    source_bytes = resolved_source.read_bytes()
    try:
        source_text = source_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorkbenchError(f"manuscript is not UTF-8: {exc}") from exc
    if not source_text.strip():
        raise WorkbenchError("manuscript must not be empty")
    if not _safe_source_name(resolved_source.name):
        raise WorkbenchError("manuscript filename is not a safe basename")
    _, parent_source_bytes, _ = _workspace_source(parent_paths)
    if source_bytes == parent_source_bytes:
        raise WorkbenchError("new DocumentVersion must differ from its parent source bytes")

    project, _ = _read_json(root_paths.project)
    document, document_bytes = _read_json(root_paths.document)
    parent_record, parent_record_bytes = _read_json(parent_paths.version)
    next_id = f"V{len(versions) + 1}"
    target_paths = WorkspacePaths(root_paths.root, next_id)
    if target_paths.version_dir.exists() or target_paths.version_dir.is_symlink():
        raise WorkbenchError(f"refusing to overwrite existing version: {next_id}")
    created_at = utc_now()
    source_relative = PurePosixPath("source", resolved_source.name).as_posix()
    version = {
        "schema_version": 1,
        "artifact": "document-version",
        "artifact_id": next_id,
        "lifecycle": "immutable",
        "provenance": _provenance("human-confirmed", created_at, "local-user"),
        "parents": [
            _parent("document", "argument-document", document_bytes),
            _parent("parent-version", "document-version", parent_record_bytes),
        ],
        "project_id": project["project_id"],
        "document_id": document["document_id"],
        "version_id": next_id,
        "source": {
            "name": resolved_source.name,
            "relative_path": source_relative,
            "sha256": sha256_bytes(source_bytes),
        },
        "parent_version": selected_parent,
    }
    contract_errors = validate_artifact(version)
    if contract_errors:
        raise WorkbenchError(
            "internal DocumentVersion contract error: " + "; ".join(contract_errors)
        )
    if parent_record.get("version_id") != selected_parent:
        raise WorkbenchError("parent version record identity does not match its directory")
    prompt = build_ir_extraction_prompt(
        source_text,
        source_name=resolved_source.name,
        source_sha256=sha256_bytes(source_bytes),
    ).encode("utf-8")
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{next_id}.", dir=target_paths.versions_dir)
    )
    try:
        archived_source = temporary / Path(source_relative)
        archived_source.parent.mkdir(parents=True, exist_ok=True)
        _write_new(archived_source, source_bytes)
        _write_new(temporary / "document-version.json", json_bytes(version))
        _write_new(temporary / "extraction-prompt.md", prompt)
        (temporary / "raw-ir").mkdir()
        (temporary / "corrections").mkdir()
        (temporary / "reviewed-ir").mkdir()
        os.replace(temporary, target_paths.version_dir)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return target_paths


def _workspace_source(paths: WorkspacePaths) -> tuple[dict[str, Any], bytes, Path]:
    version, _ = _read_json(paths.version)
    source_path = paths.version_dir / str(version["source"]["relative_path"])
    if source_path.is_symlink() or not source_path.is_file():
        raise WorkbenchError("workspace source is missing or is a symbolic link")
    source_bytes = source_path.read_bytes()
    if sha256_bytes(source_bytes) != version["source"]["sha256"]:
        raise WorkbenchError("workspace source hash does not match document-version")
    return version, source_bytes, source_path


def document_version_chain(
    paths_or_project: WorkspacePaths | Path | str,
) -> list[tuple[dict[str, Any], bytes]]:
    """Load current DocumentVersion followed by every exact declared ancestor."""
    paths = workspace_paths(paths_or_project)
    chain: list[tuple[dict[str, Any], bytes]] = []
    seen: set[str] = set()
    current_id: str | None = paths.version_id
    while current_id is not None:
        if current_id in seen:
            raise WorkbenchError("DocumentVersion parent chain contains a cycle")
        seen.add(current_id)
        value, data = _read_json(
            paths.versions_dir / current_id / "document-version.json"
        )
        if value.get("version_id") != current_id:
            raise WorkbenchError(
                f"DocumentVersion {current_id} identity does not match directory"
            )
        chain.append((value, data))
        parent = value.get("parent_version")
        current_id = str(parent) if parent is not None else None
    return chain


def _structurally_admissible_ir(value: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["raw IR must be a JSON object"]
    if set(value) != IR_KEYS:
        errors.append("raw IR must contain exactly the Argument IR v1 top-level fields")
        return errors
    if value.get("schema_version") != 1 or value.get("artifact") != "argument-ir":
        errors.append("raw IR must identify itself as argument-ir schema v1")
    expected = {
        "claims": ("C", CLAIM_KEYS),
        "evidence": ("E", EVIDENCE_KEYS),
        "assumptions": ("A", ASSUMPTION_KEYS),
        "citations": ("Z", CITATION_KEYS),
        "relations": ("R", RELATION_KEYS),
    }
    all_ids: list[str] = []
    for field, (prefix, keys) in expected.items():
        items = value.get(field)
        if not isinstance(items, list):
            errors.append(f"{field} must be an array")
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict) or set(item) != keys:
                errors.append(f"{field}[{index}] does not have the exact v1 fields")
                continue
            identifier = item.get("id")
            if not isinstance(identifier, str) or re.fullmatch(
                rf"{prefix}[1-9][0-9]*", identifier
            ) is None:
                errors.append(f"{field}[{index}].id is not a usable {prefix} ID")
            else:
                all_ids.append(identifier)
    if len(all_ids) != len(set(all_ids)):
        errors.append("raw IR node/relation IDs must be unique")
    return errors


def classify_raw_ir(
    value: object,
    *,
    source_bytes: bytes,
    source_name: str,
) -> tuple[str, list[str]]:
    admission_errors = _structurally_admissible_ir(value)
    if admission_errors:
        return "unusable", admission_errors
    assert isinstance(value, dict)
    source = value.get("source")
    expected_source = {"name": source_name, "sha256": sha256_bytes(source_bytes)}
    if source != expected_source:
        return "unusable", ["raw IR source binding does not match this DocumentVersion"]
    full_errors = validate_argument_ir(
        value, source_bytes=source_bytes, source_name=source_name
    )
    return ("valid" if not full_errors else "correctable"), full_errors


def _next_attempt_id(paths: WorkspacePaths) -> str:
    existing = [
        path.name
        for path in paths.raw_dir.glob("attempt-[0-9][0-9][0-9][0-9]")
        if path.is_dir() and not path.is_symlink()
    ]
    number = max([int(name.removeprefix("attempt-")) for name in existing] or [0]) + 1
    return f"attempt-{number:04d}"


def collect_raw_attempt(
    project_dir: Path | str,
    response_bytes: bytes,
    *,
    method: str,
    source_name: str,
    producer_label: str | None,
) -> tuple[Path, dict[str, Any]]:
    paths = workspace_paths(project_dir)
    if method not in {"file", "terminal-paste"}:
        raise WorkbenchError("collection method must be file or terminal-paste")
    if not source_name or Path(source_name).name != source_name:
        raise WorkbenchError("collection source_name must be a basename")
    version, source_bytes, _ = _workspace_source(paths)
    validation_errors: list[str]
    try:
        raw_value = parse_json_strict(response_bytes)
    except WorkbenchError as exc:
        status, validation_errors = "unusable", [str(exc)]
    else:
        status, validation_errors = classify_raw_ir(
            raw_value,
            source_bytes=source_bytes,
            source_name=str(version["source"]["name"]),
        )
    attempt_id = _next_attempt_id(paths)
    attempt_path = paths.raw_dir / attempt_id
    if attempt_path.exists() or attempt_path.is_symlink():
        raise WorkbenchError(f"attempt already exists: {attempt_path}")
    version_value, version_bytes = _read_json(paths.version)
    prompt_bytes = paths.prompt.read_bytes()
    created_at = utc_now()
    record = {
        "schema_version": 1,
        "artifact": "raw-ir-attempt",
        "artifact_id": attempt_id,
        "lifecycle": "immutable",
        "provenance": _provenance(
            "model-derived", created_at, producer_label or "unlabeled-model"
        ),
        "parents": [_parent("document-version", "document-version", version_bytes)],
        "project_id": version_value["project_id"],
        "document_id": version_value["document_id"],
        "version_id": version_value["version_id"],
        "attempt_id": attempt_id,
        "collection": {
            "method": method,
            "source_name": source_name,
            "producer_label": producer_label,
        },
        "prompt_sha256": sha256_bytes(prompt_bytes),
        "response": {"relative_path": "response.json", "sha256": sha256_bytes(response_bytes)},
        "validation": {"status": status, "errors": validation_errors},
    }
    errors = validate_artifact(record)
    if errors:
        raise WorkbenchError("internal raw attempt contract error: " + "; ".join(errors))
    temporary = Path(tempfile.mkdtemp(prefix=f".{attempt_id}.", dir=paths.raw_dir))
    try:
        _write_new(temporary / "response.json", response_bytes)
        _write_new(temporary / "record.json", json_bytes(record))
        os.replace(temporary, attempt_path)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return attempt_path, record


def list_attempts(paths: WorkspacePaths) -> list[tuple[Path, dict[str, Any], bytes]]:
    attempts: list[tuple[Path, dict[str, Any], bytes]] = []
    if not paths.raw_dir.is_dir():
        return attempts
    for attempt in sorted(paths.raw_dir.glob("attempt-[0-9][0-9][0-9][0-9]")):
        if attempt.is_symlink() or not attempt.is_dir():
            continue
        record, record_bytes = _read_json(attempt / "record.json")
        attempts.append((attempt, record, record_bytes))
    return attempts


def selected_attempt(paths: WorkspacePaths) -> tuple[Path, dict[str, Any], bytes]:
    attempts = list_attempts(paths)
    corrections = correction_entries(paths)
    if corrections:
        pinned_id = corrections[0][1].get("attempt_id")
        for attempt in attempts:
            if attempt[1].get("attempt_id") == pinned_id:
                return attempt
        raise WorkbenchError(
            f"correction history pins missing Raw IR attempt: {pinned_id}"
        )
    for attempt in reversed(attempts):
        if attempt[1].get("validation", {}).get("status") in {"valid", "correctable"}:
            return attempt
    raise WorkbenchError("project has no inspectable Raw IR attempt")


def correction_entries(paths: WorkspacePaths) -> list[tuple[Path, dict[str, Any], bytes]]:
    result: list[tuple[Path, dict[str, Any], bytes]] = []
    if not paths.corrections_dir.is_dir():
        return result
    for path in sorted(paths.corrections_dir.glob("IC[0-9][0-9][0-9][0-9].json")):
        value, data = _read_json(path)
        result.append((path, value, data))
    return result


def append_correction(
    project_dir: Path | str,
    operation: dict[str, Any],
    *,
    reason: str = "",
    producer: str = "local-user",
) -> tuple[Path, dict[str, Any]]:
    paths = workspace_paths(project_dir)
    attempt_path, attempt, attempt_bytes = selected_attempt(paths)
    version, version_bytes = _read_json(paths.version)
    existing = correction_entries(paths)
    correction_id = f"IC{len(existing) + 1:04d}"
    if operation.get("kind") == "revert_correction":
        target = operation.get("target")
        known = {value.get("correction_id"): value for _, value, _ in existing}
        if target not in known:
            raise WorkbenchError(f"cannot revert unknown correction: {target}")
        if known[str(target)].get("operation", {}).get("kind") == "revert_correction":
            raise WorkbenchError("reverting a revert event is not supported")
        already_reverted = {
            value.get("operation", {}).get("target")
            for _, value, _ in existing
            if value.get("operation", {}).get("kind") == "revert_correction"
        }
        if target in already_reverted:
            raise WorkbenchError(f"correction is already reverted: {target}")
    parents = [
        _parent("document-version", "document-version", version_bytes),
        _parent("raw-ir-attempt", "raw-ir-attempt", attempt_bytes),
    ]
    if existing:
        parents.append(_parent("previous-correction", "ir-correction", existing[-1][2]))
    correction = {
        "schema_version": 1,
        "artifact": "ir-correction",
        "artifact_id": correction_id,
        "lifecycle": "append-only",
        "provenance": _provenance("human-confirmed", utc_now(), producer),
        "parents": parents,
        "project_id": version["project_id"],
        "document_id": version["document_id"],
        "version_id": version["version_id"],
        "attempt_id": attempt["attempt_id"],
        "correction_id": correction_id,
        "operation": operation,
        "reason": reason,
    }
    errors = validate_ir_correction(correction)
    if errors:
        raise WorkbenchError("invalid correction: " + "; ".join(errors))
    output = paths.corrections_dir / f"{correction_id}.json"
    _write_new(output, json_bytes(correction))
    return output, correction


def _active_corrections(
    entries: list[tuple[Path, dict[str, Any], bytes]]
) -> list[tuple[dict[str, Any], bytes]]:
    reverted = {
        str(value["operation"]["target"])
        for _, value, _ in entries
        if value.get("operation", {}).get("kind") == "revert_correction"
    }
    return [
        (value, data)
        for _, value, data in entries
        if value.get("correction_id") not in reverted
        and value.get("operation", {}).get("kind") != "revert_correction"
    ]


@dataclass
class ReplayState:
    nodes: dict[str, tuple[str, dict[str, Any]]]
    relations: dict[str, dict[str, Any]]
    removed_nodes: dict[str, str]
    removed_relations: dict[str, str]
    field_origins: dict[str, tuple[str, str]]
    unverified: list[str]
    unverified_origin: tuple[str, str]


def replay_state(raw_ir: dict[str, Any], corrections: list[tuple[dict[str, Any], bytes]]) -> ReplayState:
    nodes: dict[str, tuple[str, dict[str, Any]]] = {}
    relations: dict[str, dict[str, Any]] = {}
    field_origins: dict[str, tuple[str, str]] = {}
    for kind, field in NODE_FIELDS.items():
        items = raw_ir.get(field, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            stable = f"raw:{item['id']}"
            payload = copy.deepcopy(item)
            payload.pop("id", None)
            nodes[stable] = (kind, payload)
            for key in payload:
                field_origins[f"{stable}.{key}"] = ("model-derived", "raw-ir-attempt")
    raw_relations = raw_ir.get("relations", [])
    if isinstance(raw_relations, list):
        for item in raw_relations:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            stable = f"raw:{item['id']}"
            relation = copy.deepcopy(item)
            relation.pop("id", None)
            if isinstance(relation.get("from"), str):
                relation["from"] = f"raw:{relation['from']}"
            if isinstance(relation.get("to"), str):
                relation["to"] = f"raw:{relation['to']}"
            relations[stable] = relation
            for key in relation:
                field_origins[f"{stable}.{key}"] = ("model-derived", "raw-ir-attempt")
    state = ReplayState(
        nodes=nodes,
        relations=relations,
        removed_nodes={},
        removed_relations={},
        field_origins=field_origins,
        unverified=list(raw_ir.get("unverified", []))
        if isinstance(raw_ir.get("unverified"), list)
        else [],
        unverified_origin=("model-derived", "raw-ir-attempt"),
    )
    for correction, _ in corrections:
        correction_id = str(correction["correction_id"])
        operation = correction["operation"]
        kind = operation["kind"]
        if kind == "update_node":
            target = str(operation["target"])
            if target not in state.nodes or target in state.removed_nodes:
                raise WorkbenchError(f"{correction_id} targets unavailable node {target}")
            for field, value in operation["changes"].items():
                if field in {"id", "position"}:
                    raise WorkbenchError(f"{correction_id} cannot edit deterministic field {field}")
                state.nodes[target][1][field] = copy.deepcopy(value)
                state.field_origins[f"{target}.{field}"] = ("human-confirmed", correction_id)
        elif kind == "add_node":
            stable = f"correction:{correction_id}"
            if stable in state.nodes:
                raise WorkbenchError(f"duplicate added node stable reference: {stable}")
            payload = copy.deepcopy(operation["node"])
            payload.pop("id", None)
            payload.pop("position", None)
            state.nodes[stable] = (str(operation["node_kind"]), payload)
            for field in payload:
                state.field_origins[f"{stable}.{field}"] = (
                    "human-confirmed",
                    correction_id,
                )
        elif kind == "remove_node":
            target = str(operation["target"])
            if target not in state.nodes or target in state.removed_nodes:
                raise WorkbenchError(f"{correction_id} targets unavailable node {target}")
            state.removed_nodes[target] = correction_id
        elif kind == "add_relation":
            stable = f"correction:{correction_id}"
            relation = copy.deepcopy(operation["relation"])
            relation.pop("id", None)
            state.relations[stable] = relation
            for field in relation:
                state.field_origins[f"{stable}.{field}"] = (
                    "human-confirmed",
                    correction_id,
                )
        elif kind == "update_relation":
            target = str(operation["target"])
            if target not in state.relations or target in state.removed_relations:
                raise WorkbenchError(f"{correction_id} targets unavailable relation {target}")
            for field, value in operation["changes"].items():
                if field == "id":
                    raise WorkbenchError(f"{correction_id} cannot edit deterministic relation ID")
                state.relations[target][field] = copy.deepcopy(value)
                state.field_origins[f"{target}.{field}"] = (
                    "human-confirmed",
                    correction_id,
                )
        elif kind == "remove_relation":
            target = str(operation["target"])
            if target not in state.relations or target in state.removed_relations:
                raise WorkbenchError(f"{correction_id} targets unavailable relation {target}")
            state.removed_relations[target] = correction_id
        elif kind == "set_unverified":
            state.unverified = list(operation["items"])
            state.unverified_origin = ("human-confirmed", correction_id)
    return state


def _raw_ir_for_attempt(attempt_path: Path) -> dict[str, Any]:
    value = parse_json_strict((attempt_path / "response.json").read_bytes())
    if not isinstance(value, dict):
        raise WorkbenchError("selected Raw IR is not an object")
    return value


def materialize_reviewed(
    paths: WorkspacePaths,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    attempt_path, attempt, attempt_bytes = selected_attempt(paths)
    raw_ir = _raw_ir_for_attempt(attempt_path)
    entries = correction_entries(paths)
    active = _active_corrections(entries)
    state = replay_state(raw_ir, active)
    version, source_bytes, _ = _workspace_source(paths)
    _, version_bytes = _read_json(paths.version)

    stable_map: dict[str, str] = {}
    output_nodes: dict[str, list[dict[str, Any]]] = {
        field: [] for field in NODE_FIELDS.values()
    }
    field_provenance: dict[str, dict[str, str]] = {
        "schema_version": {"origin": "deterministic", "source": "workbench-materializer-v1"},
        "artifact": {"origin": "deterministic", "source": "workbench-materializer-v1"},
        "scope": {"origin": "model-derived", "source": "raw-ir-attempt"},
        "source.name": {"origin": "deterministic", "source": "document-version"},
        "source.sha256": {"origin": "deterministic", "source": "document-version"},
    }
    for kind, field in NODE_FIELDS.items():
        prefix = NODE_PREFIXES[kind]
        available = [
            (stable, payload)
            for stable, (node_kind, payload) in state.nodes.items()
            if node_kind == kind and stable not in state.removed_nodes
        ]
        for index, (stable, payload) in enumerate(available, 1):
            display_id = f"{prefix}{index}"
            stable_map[stable] = display_id
            node = {"id": display_id, **copy.deepcopy(payload), "position": "pending"}
            output_nodes[field].append(node)
            field_provenance[f"{display_id}.id"] = {
                "origin": "deterministic",
                "source": "workbench-materializer-v1",
            }
            field_provenance[f"{display_id}.position"] = {
                "origin": "deterministic",
                "source": "document-version",
            }
            for node_field in payload:
                origin, source = state.field_origins.get(
                    f"{stable}.{node_field}", ("model-derived", "raw-ir-attempt")
                )
                field_provenance[f"{display_id}.{node_field}"] = {
                    "origin": origin,
                    "source": source,
                }
    output_relations: list[dict[str, Any]] = []
    for stable, relation in state.relations.items():
        if stable in state.removed_relations:
            continue
        from_ref = relation.get("from")
        to_ref = relation.get("to")
        if from_ref in state.removed_nodes or to_ref in state.removed_nodes:
            continue
        relation_id = f"R{len(output_relations) + 1}"
        stable_map[stable] = relation_id
        rendered = {
            "id": relation_id,
            "type": relation.get("type"),
            "from": stable_map.get(str(from_ref), str(from_ref)),
            "to": stable_map.get(str(to_ref), str(to_ref)),
        }
        output_relations.append(rendered)
        field_provenance[f"{relation_id}.id"] = {
            "origin": "deterministic",
            "source": "workbench-materializer-v1",
        }
        for relation_field in ("type", "from", "to"):
            origin, source = state.field_origins.get(
                f"{stable}.{relation_field}", ("model-derived", "raw-ir-attempt")
            )
            field_provenance[f"{relation_id}.{relation_field}"] = {
                "origin": origin,
                "source": source,
            }
    field_provenance["unverified"] = {
        "origin": state.unverified_origin[0],
        "source": state.unverified_origin[1],
    }
    for stable, correction_id in state.removed_nodes.items():
        field_provenance[f"removed.{stable}"] = {
            "origin": "human-confirmed",
            "source": correction_id,
        }
    for stable, correction_id in state.removed_relations.items():
        field_provenance[f"removed.{stable}"] = {
            "origin": "human-confirmed",
            "source": correction_id,
        }

    candidate = {
        "schema_version": 1,
        "artifact": "argument-ir",
        "scope": raw_ir.get("scope"),
        "source": {
            "name": version["source"]["name"],
            "sha256": version["source"]["sha256"],
        },
        "claims": output_nodes["claims"],
        "evidence": output_nodes["evidence"],
        "assumptions": output_nodes["assumptions"],
        "citations": output_nodes["citations"],
        "relations": output_relations,
        "unverified": state.unverified,
    }
    try:
        reviewed = canonicalize_argument_ir(
            candidate,
            source_bytes=source_bytes,
            source_name=str(version["source"]["name"]),
        )
    except ArgumentIRError as exc:
        raise WorkbenchError(f"Reviewed IR is not yet valid: {exc}") from exc
    payload_bytes = json_bytes(reviewed)
    correction_hashes = [sha256_bytes(data) for _, _, data in entries]
    parents = [
        _parent("document-version", "document-version", version_bytes),
        _parent("raw-ir-attempt", "raw-ir-attempt", attempt_bytes),
    ]
    for index, (_, _, data) in enumerate(entries, 1):
        parents.append(_parent(f"correction-{index:04d}", "ir-correction", data))
    created_at = (
        str(entries[-1][1]["provenance"]["created_at"])
        if entries
        else str(attempt["provenance"]["created_at"])
    )
    record = {
        "schema_version": 1,
        "artifact": "reviewed-argument-ir",
        "artifact_id": f"{VERSION_ID}-reviewed-ir",
        "lifecycle": "derived-replaceable",
        "provenance": _provenance(
            "deterministic", created_at, "workbench-materializer-v1"
        ),
        "parents": parents,
        "project_id": version["project_id"],
        "document_id": version["document_id"],
        "version_id": version["version_id"],
        "attempt_id": attempt["attempt_id"],
        "payload": {"relative_path": "argument-ir.json", "sha256": sha256_bytes(payload_bytes)},
        "correction_sha256s": correction_hashes,
        "stable_ref_map": stable_map,
        "field_provenance": field_provenance,
    }
    record_errors = validate_artifact(record)
    if record_errors:
        raise WorkbenchError("internal Reviewed IR contract error: " + "; ".join(record_errors))
    record_bytes = json_bytes(record)
    markdown = render_argument_map(
        reviewed,
        record,
        reviewed_record_sha256=sha256_bytes(record_bytes),
        raw_record_sha256=sha256_bytes(attempt_bytes),
    )
    return reviewed, record, markdown


def render_argument_map(
    ir: dict[str, Any],
    record: dict[str, Any],
    *,
    reviewed_record_sha256: str,
    raw_record_sha256: str,
) -> str:
    claims = {item["id"]: item for item in ir["claims"]}
    nodes = {
        item["id"]: item
        for field in ("claims", "evidence", "assumptions", "citations")
        for item in ir[field]
    }
    incoming: dict[str, list[dict[str, Any]]] = {identifier: [] for identifier in claims}
    outgoing: dict[str, list[dict[str, Any]]] = {identifier: [] for identifier in claims}
    for relation in ir["relations"]:
        if relation["to"] in incoming:
            incoming[relation["to"]].append(relation)
        if relation["from"] in outgoing:
            outgoing[relation["from"]].append(relation)

    lines = [
        "# Argument Map",
        "",
        f"- Version: `{record['version_id']}`",
        f"- Source SHA-256: `{ir['source']['sha256']}`",
        f"- Raw IR record SHA-256: `{raw_record_sha256}`",
        f"- Correction events: {len(record['correction_sha256s'])}",
        f"- Reviewed IR record SHA-256: `{reviewed_record_sha256}`",
        "- Provenance: positions/source hashes are deterministic; unchanged semantics are model-derived; correction-backed fields are human-confirmed.",
        "",
        "## Core Claims",
        "",
    ]
    core = [claim for claim in ir["claims"] if claim["role"] in {"conclusion", "intermediate"}]
    if not core:
        lines.append("No conclusion or intermediate Claim is currently identified.")
    else:
        for claim in core:
            lines.append(f"- `{claim['id']}` [{claim['role']}] {claim['text']}")
    lines.extend(["", "## Claims", ""])
    provenance = record["field_provenance"]
    for claim in ir["claims"]:
        claim_id = claim["id"]
        type_origin = provenance.get(f"{claim_id}.types", {}).get("origin", "model-derived")
        lines.extend(
            [
                f"### {claim_id} · {' / '.join(claim['types'])}",
                "",
                f"- Text: {claim['text']}",
                f"- Source: “{claim['source_quote']}”",
                f"- Position: `{claim['position']}` `[deterministic]`",
                f"- Types: {', '.join(claim['types'])} `[{type_origin}]`",
                f"- Methods: {', '.join(claim['methods'])}",
                f"- Role: {claim['role']}",
                f"- Extraction: {claim['extraction']}",
                f"- Uncertainty: {claim['uncertainty'] or '—'}",
            ]
        )
        incoming_items = incoming.get(claim_id, [])
        supports = [
            relation["from"]
            for relation in incoming_items
            if relation["type"] == "supports"
        ]
        assumptions = [
            relation["from"]
            for relation in incoming_items
            if relation["type"] == "assumes"
        ]
        citations = [
            relation["from"]
            for relation in incoming_items
            if relation["type"] == "cites"
        ]
        supported_claims = [
            relation["to"]
            for relation in outgoing.get(claim_id, [])
            if relation["type"] in {"supports", "qualifies"}
        ]
        lines.extend(
            [
                f"- Supported by: {', '.join(supports) or '—'}",
                f"- Supports/qualifies: {', '.join(supported_claims) or '—'}",
                f"- Assumptions: {', '.join(assumptions) or '—'}",
                f"- Direct citations: {', '.join(citations) or '—'}",
                "",
            ]
        )

    labels = {"evidence": "Evidence", "assumptions": "Assumptions", "citations": "Citations"}
    for field, heading in labels.items():
        lines.extend([f"## {heading}", ""])
        if not ir[field]:
            lines.extend(["None.", ""])
            continue
        for item in ir[field]:
            detail = item.get("kind") or item.get("extraction") or item.get("locator") or "—"
            lines.append(
                f"- `{item['id']}` {item['text']} — {detail}; `{item['position']}`"
            )
        lines.append("")
    lines.extend(["## Relations", ""])
    if not ir["relations"]:
        lines.extend(["None.", ""])
    else:
        for relation in ir["relations"]:
            source_text = nodes.get(relation["from"], {}).get("text", "unknown")
            target_text = nodes.get(relation["to"], {}).get("text", "unknown")
            origin = provenance.get(f"{relation['id']}.type", {}).get("origin", "model-derived")
            lines.append(
                f"- `{relation['id']}` `{relation['from']}` —{relation['type']}→ "
                f"`{relation['to']}` `[{origin}]` — {source_text} → {target_text}"
            )
        lines.append("")
    lines.extend(["## Unverified Extraction Items", ""])
    if ir["unverified"]:
        lines.extend(f"- {item}" for item in ir["unverified"])
    else:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


def _derived_bytes(paths: WorkspacePaths) -> tuple[bytes, bytes, bytes]:
    reviewed, record, markdown = materialize_reviewed(paths)
    return json_bytes(reviewed), json_bytes(record), markdown.encode("utf-8")


def rebuild_workspace(project_dir: Path | str) -> tuple[Path, bool]:
    paths = workspace_paths(project_dir)
    payload_bytes, record_bytes, map_bytes = _derived_bytes(paths)
    changed = False
    for path, data in (
        (paths.reviewed_payload, payload_bytes),
        (paths.reviewed_record, record_bytes),
        (paths.argument_map, map_bytes),
    ):
        if path.exists() and path.is_symlink():
            raise WorkbenchError(f"derived artifact must not be a symlink: {path}")
        if not path.exists() or path.read_bytes() != data:
            _atomic_write(path, data)
            changed = True
    return paths.argument_map, changed


def verify_workspace(
    paths_or_project: WorkspacePaths | Path | str,
    *,
    allow_incomplete: bool = False,
) -> list[str]:
    paths = (
        paths_or_project
        if isinstance(paths_or_project, WorkspacePaths)
        else workspace_paths(paths_or_project)
    )
    errors: list[str] = []
    entries: list[tuple[object, bytes]] = []
    for label, path in (
        ("project", paths.project),
        ("document", paths.document),
        ("document-version", paths.version),
    ):
        try:
            value, data = _read_json(path)
        except (OSError, WorkbenchError) as exc:
            errors.append(f"{label}: {exc}")
            continue
        entries.append((value, data))
        contract_errors = validate_artifact(value)
        errors.extend(f"{label}: {error}" for error in contract_errors)
    if errors:
        return errors
    project_value = entries[0][0]
    document_value = entries[1][0]
    version_value = entries[2][0]
    if version_value.get("version_id") != paths.version_id:
        errors.append("document-version identity does not match its directory")
    cursor = version_value
    seen_versions = {paths.version_id}
    while cursor.get("parent_version") is not None:
        parent_id = str(cursor["parent_version"])
        if parent_id in seen_versions:
            errors.append("DocumentVersion parent chain contains a cycle")
            break
        seen_versions.add(parent_id)
        parent_path = paths.versions_dir / parent_id / "document-version.json"
        try:
            parent_value, parent_bytes = _read_json(parent_path)
        except (OSError, WorkbenchError) as exc:
            errors.append(f"parent-version {parent_id}: {exc}")
            break
        parent_errors = validate_artifact(parent_value)
        errors.extend(
            f"parent-version {parent_id}: {error}" for error in parent_errors
        )
        parent_by_role = {
            parent.get("role"): parent
            for parent in cursor.get("parents", [])
            if isinstance(parent, dict)
        }
        if parent_by_role.get("parent-version", {}).get("sha256") != sha256_bytes(
            parent_bytes
        ):
            errors.append(f"parent-version {parent_id}: exact-byte parent hash is broken")
        if parent_value.get("version_id") != parent_id:
            errors.append(f"parent-version {parent_id}: identity does not match directory")
        entries.append((parent_value, parent_bytes))
        cursor = parent_value
    if (
        isinstance(project_value, dict)
        and isinstance(document_value, dict)
        and document_value.get("project_id") != project_value.get("project_id")
    ):
        errors.append("argument-document project_id does not match argument-project")
    if isinstance(document_value, dict) and isinstance(version_value, dict):
        if version_value.get("project_id") != document_value.get("project_id"):
            errors.append("document-version project_id does not match argument-document")
        if version_value.get("document_id") != document_value.get("document_id"):
            errors.append("document-version document_id does not match argument-document")
    if errors:
        return errors
    try:
        version, source_bytes, _ = _workspace_source(paths)
    except (OSError, WorkbenchError) as exc:
        errors.append(str(exc))
        return errors
    if not paths.prompt.is_file() or paths.prompt.is_symlink():
        errors.append("extraction-prompt.md is missing or is a symlink")
    else:
        try:
            prompt_protocol = _matching_extraction_prompt_protocol(
                paths.prompt.read_bytes(),
                source_bytes.decode("utf-8-sig"),
                source_name=str(version["source"]["name"]),
                source_sha256=str(version["source"]["sha256"]),
            )
            if prompt_protocol is None:
                errors.append(
                    "extraction-prompt.md is not a supported deterministic source-bound prompt"
                )
        except (UnicodeDecodeError, ArgumentIRError) as exc:
            errors.append(f"cannot reproduce extraction prompt: {exc}")

    for label, directory in (
        ("raw-ir", paths.raw_dir),
        ("corrections", paths.corrections_dir),
        ("reviewed-ir", paths.reviewed_dir),
    ):
        if directory.is_symlink():
            errors.append(f"{label} directory must not be a symbolic link")
    for attempt_path in paths.raw_dir.glob("attempt-*") if paths.raw_dir.is_dir() else []:
        if attempt_path.is_symlink():
            errors.append(f"{attempt_path.name} must not be a symbolic link")
    attempts = list_attempts(paths)
    attempt_hash_by_id: dict[str, str] = {}
    for attempt_path, record, record_bytes in attempts:
        entries.append((record, record_bytes))
        contract_errors = validate_artifact(record)
        if contract_errors:
            errors.extend(
                f"{attempt_path.name}: {error}" for error in contract_errors
            )
            continue
        attempt_hash_by_id[str(record.get("attempt_id"))] = sha256_bytes(record_bytes)
        for key in ("project_id", "document_id", "version_id"):
            if record.get(key) != version.get(key):
                errors.append(f"{attempt_path.name}: {key} does not match DocumentVersion")
        response_path = attempt_path / str(record.get("response", {}).get("relative_path"))
        if response_path.is_symlink() or not response_path.is_file():
            errors.append(f"{attempt_path.name}: response artifact is missing or a symlink")
        else:
            response_bytes = response_path.read_bytes()
            if sha256_bytes(response_bytes) != record.get("response", {}).get("sha256"):
                errors.append(f"{attempt_path.name}: response SHA-256 mismatch")
            try:
                raw_value = parse_json_strict(response_bytes)
            except WorkbenchError as exc:
                expected_validation = {"status": "unusable", "errors": [str(exc)]}
            else:
                status, validation_errors = classify_raw_ir(
                    raw_value,
                    source_bytes=source_bytes,
                    source_name=str(version["source"]["name"]),
                )
                expected_validation = {
                    "status": status,
                    "errors": validation_errors,
                }
            if record.get("validation") != expected_validation:
                errors.append(
                    f"{attempt_path.name}: validation record does not match a fresh Raw IR validation"
                )
        if record.get("prompt_sha256") != sha256_bytes(paths.prompt.read_bytes()):
            errors.append(f"{attempt_path.name}: prompt SHA-256 mismatch")
    corrections = correction_entries(paths)
    previous_hash: str | None = None
    version_hash = sha256_bytes(entries[2][1])
    for index, (path, correction, data) in enumerate(corrections, 1):
        entries.append((correction, data))
        if correction.get("correction_id") != f"IC{index:04d}":
            errors.append(f"{path.name}: correction IDs must be continuous")
        parents = {
            parent.get("role"): parent.get("sha256")
            for parent in correction.get("parents", [])
            if isinstance(parent, dict)
        }
        for key in ("project_id", "document_id", "version_id"):
            if correction.get(key) != version.get(key):
                errors.append(f"{path.name}: {key} does not match DocumentVersion")
        if parents.get("document-version") != version_hash:
            errors.append(f"{path.name}: document-version parent hash is incorrect")
        expected_attempt_hash = attempt_hash_by_id.get(str(correction.get("attempt_id")))
        if expected_attempt_hash is None:
            errors.append(f"{path.name}: attempt_id does not identify an archived Raw IR")
        elif parents.get("raw-ir-attempt") != expected_attempt_hash:
            errors.append(f"{path.name}: raw-ir-attempt parent does not match attempt_id")
        if index == 1 and "previous-correction" in parents:
            errors.append(f"{path.name}: first correction cannot have previous-correction")
        if index > 1 and parents.get("previous-correction") != previous_hash:
            errors.append(f"{path.name}: previous-correction hash is broken")
        previous_hash = sha256_bytes(data)

    if paths.reviewed_record.exists() or paths.reviewed_payload.exists() or paths.argument_map.exists():
        if not all(
            path.exists()
            for path in (paths.reviewed_record, paths.reviewed_payload, paths.argument_map)
        ):
            errors.append("Reviewed IR cache is incomplete")
        elif any(
            path.is_symlink()
            for path in (paths.reviewed_record, paths.reviewed_payload, paths.argument_map)
        ):
            errors.append("Reviewed IR cache files must not be symbolic links")
        else:
            try:
                reviewed_record, reviewed_record_bytes = _read_json(paths.reviewed_record)
                entries.append((reviewed_record, reviewed_record_bytes))
                expected_payload, expected_record, expected_map = _derived_bytes(paths)
                if paths.reviewed_payload.read_bytes() != expected_payload:
                    errors.append("reviewed argument-ir.json is not reproducible")
                if paths.reviewed_record.read_bytes() != expected_record:
                    errors.append("reviewed record.json is not reproducible")
                if paths.argument_map.read_bytes() != expected_map:
                    errors.append("argument-map.md is not reproducible")
            except (OSError, WorkbenchError) as exc:
                errors.append(f"cannot reproduce Reviewed IR: {exc}")
    elif attempts and not allow_incomplete:
        inspectable = any(
            record.get("validation", {}).get("status") in {"valid", "correctable"}
            for _, record, _ in attempts
        )
        if inspectable:
            try:
                materialize_reviewed(paths)
            except WorkbenchError:
                pass
            else:
                errors.append("valid Reviewed IR can be derived but cache is missing")
    errors.extend(validate_contract_bundle(entries))
    try:
        from argument_review import verify_reviews

        errors.extend(f"reviews: {error}" for error in verify_reviews(paths))
    except ImportError as exc:
        errors.append(f"reviews: cannot load review verifier: {exc}")
    try:
        from argument_perspective import verify_perspective_reviews

        errors.extend(
            f"perspective reviews: {error}"
            for error in verify_perspective_reviews(paths)
        )
    except ImportError as exc:
        errors.append(f"perspective reviews: cannot load verifier: {exc}")
    try:
        from argument_adjudication import verify_adjudications

        errors.extend(
            f"adjudications: {error}"
            for error in verify_adjudications(paths)
        )
    except ImportError as exc:
        errors.append(f"adjudications: cannot load verifier: {exc}")
    try:
        from argument_baseline import verify_direct_review_baselines

        errors.extend(
            f"direct baselines: {error}"
            for error in verify_direct_review_baselines(paths)
        )
    except ImportError as exc:
        errors.append(f"direct baselines: cannot load verifier: {exc}")
    try:
        from argument_sessions import verify_work_sessions

        errors.extend(
            f"work sessions: {error}"
            for error in verify_work_sessions(paths)
        )
    except ImportError as exc:
        errors.append(f"work sessions: cannot load verifier: {exc}")
    try:
        from argument_triage import verify_review_status_triage

        errors.extend(
            f"status triage: {error}"
            for error in verify_review_status_triage(paths)
        )
    except ImportError as exc:
        errors.append(f"status triage: cannot load verifier: {exc}")
    return errors


def verify_project_versions(project_dir: WorkspacePaths | Path | str) -> list[str]:
    """Verify every immutable DocumentVersion and every version-local lifecycle."""
    root_paths = workspace_paths(project_dir)
    errors: list[str] = []
    try:
        versions = list_version_ids(root_paths)
    except WorkbenchError as exc:
        return [str(exc)]
    if not versions:
        return ["project has no DocumentVersion"]
    numbers = [int(version_id[1:]) for version_id in versions]
    if numbers != list(range(1, len(versions) + 1)):
        errors.append("DocumentVersion IDs must be continuous from V1")
    previous_source_hash: str | None = None
    for index, version_id in enumerate(versions):
        paths = WorkspacePaths(root_paths.root, version_id)
        version_errors = verify_workspace(paths)
        errors.extend(f"{version_id}: {error}" for error in version_errors)
        try:
            record, _ = _read_json(paths.version)
        except (OSError, WorkbenchError):
            continue
        expected_parent = None if index == 0 else versions[index - 1]
        if record.get("parent_version") != expected_parent:
            errors.append(
                f"{version_id}: parent_version must be {expected_parent!r}"
            )
        source_hash = record.get("source", {}).get("sha256")
        if previous_source_hash is not None and source_hash == previous_source_hash:
            errors.append(f"{version_id}: source bytes duplicate its parent version")
        previous_source_hash = str(source_hash)
    try:
        from argument_versioning import verify_structural_diffs

        errors.extend(
            f"version diffs: {error}"
            for error in verify_structural_diffs(root_paths)
        )
    except ImportError as exc:
        errors.append(f"version diffs: cannot load verifier: {exc}")
    try:
        from argument_lineage import verify_lineage_analyses

        errors.extend(
            f"claim lineage: {error}"
            for error in verify_lineage_analyses(root_paths)
        )
    except ImportError as exc:
        errors.append(f"claim lineage: cannot load verifier: {exc}")
    return errors


def _display_ir(ir: dict[str, Any]) -> str:
    lines = [
        f"Claims {len(ir.get('claims', []))} · Evidence {len(ir.get('evidence', []))} · "
        f"Assumptions {len(ir.get('assumptions', []))} · Citations {len(ir.get('citations', []))}",
        "",
    ]
    for claim in ir.get("claims", []):
        if not isinstance(claim, dict):
            continue
        lines.extend(
            [
                f"{claim.get('id')} · {' / '.join(claim.get('types', [])) if isinstance(claim.get('types'), list) else claim.get('types')}",
                f"  {claim.get('text', '—')}",
                f"  source: {claim.get('source_quote', '—')}",
                f"  position: {claim.get('position', '—')}",
                f"  role: {claim.get('role', '—')} · extraction: {claim.get('extraction', '—')}",
                f"  uncertainty: {claim.get('uncertainty') or '—'}",
            ]
        )
        related = [
            relation
            for relation in ir.get("relations", [])
            if isinstance(relation, dict)
            and (relation.get("from") == claim.get("id") or relation.get("to") == claim.get("id"))
        ]
        for relation in related:
            lines.append(
                f"  {relation.get('id')}: {relation.get('from')} —{relation.get('type')}→ {relation.get('to')}"
            )
        lines.append("")
    for field, label in (("evidence", "Evidence"), ("assumptions", "Assumptions"), ("citations", "Citations")):
        lines.append(label + ":")
        items = ir.get(field, [])
        if not items:
            lines.append("  —")
        elif isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    lines.append(f"  {item.get('id')}: {item.get('text')}")
        lines.append("")
    lines.append("Relations:")
    relations = ir.get("relations", [])
    if not relations:
        lines.append("  —")
    elif isinstance(relations, list):
        for relation in relations:
            if isinstance(relation, dict):
                lines.append(
                    f"  {relation.get('id')}: {relation.get('from')} —{relation.get('type')}→ {relation.get('to')}"
                )
    lines.append("")
    return "\n".join(lines)


def _preview_projection(paths: WorkspacePaths) -> tuple[dict[str, Any], dict[str, str]]:
    """Build a best-effort display projection even while strict validation fails."""
    attempt_path, _, _ = selected_attempt(paths)
    raw = _raw_ir_for_attempt(attempt_path)
    state = replay_state(raw, _active_corrections(correction_entries(paths)))
    stable_map: dict[str, str] = {}
    output_nodes: dict[str, list[dict[str, Any]]] = {
        field: [] for field in NODE_FIELDS.values()
    }
    for kind, field in NODE_FIELDS.items():
        prefix = NODE_PREFIXES[kind]
        available = [
            (stable, payload)
            for stable, (node_kind, payload) in state.nodes.items()
            if node_kind == kind and stable not in state.removed_nodes
        ]
        for index, (stable, payload) in enumerate(available, 1):
            display_id = f"{prefix}{index}"
            stable_map[stable] = display_id
            node = {"id": display_id, **copy.deepcopy(payload)}
            node.setdefault("position", "pending correction")
            output_nodes[field].append(node)
    output_relations: list[dict[str, Any]] = []
    for stable, relation in state.relations.items():
        if stable in state.removed_relations:
            continue
        if relation.get("from") in state.removed_nodes or relation.get("to") in state.removed_nodes:
            continue
        relation_id = f"R{len(output_relations) + 1}"
        stable_map[stable] = relation_id
        output_relations.append(
            {
                "id": relation_id,
                "type": relation.get("type"),
                "from": stable_map.get(str(relation.get("from")), str(relation.get("from"))),
                "to": stable_map.get(str(relation.get("to")), str(relation.get("to"))),
            }
        )
    return (
        {
            "schema_version": 1,
            "artifact": "argument-ir",
            "scope": raw.get("scope"),
            "source": raw.get("source"),
            "claims": output_nodes["claims"],
            "evidence": output_nodes["evidence"],
            "assumptions": output_nodes["assumptions"],
            "citations": output_nodes["citations"],
            "relations": output_relations,
            "unverified": state.unverified,
        },
        {display: stable for stable, display in stable_map.items()},
    )


def current_view(project_dir: Path | str) -> tuple[str, list[str]]:
    paths = workspace_paths(project_dir)
    try:
        reviewed, _, _ = materialize_reviewed(paths)
        return _display_ir(reviewed), []
    except WorkbenchError as exc:
        _, attempt, _ = selected_attempt(paths)
        preview, _ = _preview_projection(paths)
        return _display_ir(preview), list(attempt["validation"]["errors"]) + [str(exc)]


def _display_to_stable(paths: WorkspacePaths) -> dict[str, str]:
    try:
        _, record, _ = materialize_reviewed(paths)
        return {display: stable for stable, display in record["stable_ref_map"].items()}
    except WorkbenchError:
        _, mapping = _preview_projection(paths)
        return mapping


def _ask_nonempty(input_fn: Callable[[str], str], prompt: str) -> str:
    while True:
        value = input_fn(prompt).strip()
        if value:
            return value
        print("Value is required.")


def _node_payload(input_fn: Callable[[str], str], kind: str) -> dict[str, Any]:
    text = _ask_nonempty(input_fn, "Normalized text: ")
    quote = _ask_nonempty(input_fn, "Exact source quote: ")
    if kind == "claim":
        return {
            "text": text,
            "source_quote": quote,
            "types": [item.strip() for item in _ask_nonempty(input_fn, "Types (comma-separated): ").split(",") if item.strip()],
            "methods": [item.strip() for item in _ask_nonempty(input_fn, "Methods (comma-separated): ").split(",") if item.strip()],
            "role": _ask_nonempty(input_fn, "Role: "),
            "extraction": _ask_nonempty(input_fn, "Extraction (explicit/inferred): "),
            "uncertainty": input_fn("Uncertainty (optional): ").strip(),
        }
    if kind == "evidence":
        return {"text": text, "source_quote": quote, "kind": _ask_nonempty(input_fn, "Evidence kind: ")}
    if kind == "assumption":
        return {
            "text": text,
            "source_quote": quote,
            "extraction": _ask_nonempty(input_fn, "Extraction (explicit/inferred): "),
            "uncertainty": input_fn("Uncertainty (optional): ").strip(),
        }
    return {"text": text, "source_quote": quote, "locator": input_fn("Citation locator (optional): ").strip()}


def _classification_values(
    raw_value: str,
    current: list[str],
    *,
    field: str,
    allowed: tuple[str, ...],
) -> list[str]:
    if not raw_value.strip():
        return list(current)
    values = [item.strip() for item in raw_value.split(",") if item.strip()]
    if not values:
        raise WorkbenchError(f"{field} must contain at least one value")
    if len(values) != len(set(values)):
        raise WorkbenchError(f"{field} must not contain duplicate values")
    unknown = [item for item in values if item not in allowed]
    if unknown:
        raise WorkbenchError(
            f"unknown {field}: {', '.join(unknown)}; allowed: {', '.join(allowed)}"
        )
    if len(values) > 1 and ({"unspecified", "other"} & set(values)):
        raise WorkbenchError(f"{field} must use unspecified or other alone")
    return values


def run_classification_triage(
    paths: WorkspacePaths,
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> int:
    try:
        reviewed, record, _ = materialize_reviewed(paths)
        mapping = {
            display: stable for stable, display in record["stable_ref_map"].items()
        }
    except WorkbenchError:
        reviewed, mapping = _preview_projection(paths)
    changed = 0
    output_fn(
        "Classification triage: Enter keeps model-derived values; E records a "
        "human correction; Q returns to the main menu."
    )
    for claim in reviewed.get("claims", []):
        if not isinstance(claim, dict) or not isinstance(claim.get("id"), str):
            continue
        claim_id = str(claim["id"])
        stable = mapping.get(claim_id)
        if stable is None:
            raise WorkbenchError(f"cannot resolve stable reference for {claim_id}")
        current_types = list(claim.get("types", []))
        current_methods = list(claim.get("methods", []))
        output_fn("")
        output_fn(f"{claim_id} · {claim.get('role', '—')} · {claim.get('text', '—')}")
        output_fn(f"  source: {claim.get('source_quote', '—')}")
        output_fn(f"  types: {', '.join(current_types)}")
        output_fn(f"  methods: {', '.join(current_methods)}")
        action = input_fn("[Enter] keep  [E]dit  [Q] return: ").strip().casefold()
        if action in {"q", "quit"}:
            break
        if not action:
            continue
        if action not in {"e", "edit"}:
            output_fn("Unknown classification choice; value kept.")
            continue
        new_types = _classification_values(
            input_fn(f"Types [{', '.join(current_types)}]: "),
            current_types,
            field="types",
            allowed=CLAIM_TYPES,
        )
        new_methods = _classification_values(
            input_fn(f"Methods [{', '.join(current_methods)}]: "),
            current_methods,
            field="methods",
            allowed=METHOD_TYPES,
        )
        changes: dict[str, object] = {}
        if new_types != current_types:
            changes["types"] = new_types
        if new_methods != current_methods:
            changes["methods"] = new_methods
        if not changes:
            output_fn("No classification change; no correction event written.")
            continue
        output_fn(
            f"  proposed: types={', '.join(new_types)}; methods={', '.join(new_methods)}"
        )
        if input_fn("Write human-confirmed correction? [y/N]: ").strip().casefold() != "y":
            output_fn("Cancelled; model-derived values kept.")
            continue
        reason = input_fn("Reason (optional): ").strip() or f"Classification triage for {claim_id}."
        correction_path, _ = append_correction(
            paths.root,
            {"kind": "update_node", "target": stable, "changes": changes},
            reason=reason,
        )
        rebuild_workspace(paths.root)
        changed += 1
        output_fn(f"Correction saved immediately: {correction_path.name}")
    output_fn(f"Classification triage complete; {changed} correction event(s) written.")
    return changed


def run_inspector(
    project_dir: Path | str,
    *,
    view_only: bool,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    paths = workspace_paths(project_dir)

    def show() -> None:
        view, errors = current_view(paths.root)
        output_fn(view)
        if errors:
            output_fn("Current extraction issues:")
            for error in errors:
                output_fn(f"  - {error}")

    show()
    if view_only:
        return 0
    menu = (
        "[V]iew  [E]dit node  [A]dd node  [D]elete node  "
        "[R]elation  [B]ind/unbind  [C]lassify  [U]ndo  [Q]uit"
    )
    while True:
        output_fn(menu)
        choice = input_fn("Choice: ").strip().casefold()
        if choice in {"q", "quit"}:
            try:
                map_path, _ = rebuild_workspace(paths.root)
                output_fn(f"Reviewed IR ready: {map_path}")
            except WorkbenchError as exc:
                output_fn(f"Reviewed IR is not yet valid: {exc}")
            return 0
        if choice in {"v", "view"}:
            show()
            continue
        mapping = _display_to_stable(paths)
        try:
            if choice in {"c", "classify", "classification"}:
                run_classification_triage(
                    paths, input_fn=input_fn, output_fn=output_fn
                )
                continue
            if choice in {"e", "edit"}:
                display = _ask_nonempty(input_fn, "Node ID: ").upper()
                stable = mapping.get(display)
                if stable is None or display.startswith("R"):
                    raise WorkbenchError(f"unknown node ID: {display}")
                field = _ask_nonempty(input_fn, "Field to edit: ")
                if field in {"id", "position"}:
                    raise WorkbenchError(f"{field} is deterministic and cannot be edited")
                raw_value = input_fn("New value: ").strip()
                value: object = (
                    [item.strip() for item in raw_value.split(",") if item.strip()]
                    if field in LIST_FIELDS
                    else raw_value
                )
                append_correction(
                    paths.root,
                    {"kind": "update_node", "target": stable, "changes": {field: value}},
                )
            elif choice in {"a", "add"}:
                kind = _ask_nonempty(input_fn, "Kind (claim/evidence/assumption/citation): ").casefold()
                if kind not in NODE_FIELDS:
                    raise WorkbenchError(f"unknown node kind: {kind}")
                append_correction(
                    paths.root,
                    {"kind": "add_node", "node_kind": kind, "node": _node_payload(input_fn, kind)},
                )
            elif choice in {"d", "delete"}:
                display = _ask_nonempty(input_fn, "Node ID: ").upper()
                stable = mapping.get(display)
                if stable is None or display.startswith("R"):
                    raise WorkbenchError(f"unknown node ID: {display}")
                if input_fn(f"Delete {display} and its incident relations? [y/N]: ").strip().casefold() != "y":
                    output_fn("Cancelled.")
                    continue
                append_correction(paths.root, {"kind": "remove_node", "target": stable})
            elif choice in {"r", "relation"}:
                action = _ask_nonempty(input_fn, "Relation action (add/edit/delete): ").casefold()
                if action == "add":
                    relation_type = _ask_nonempty(input_fn, "Type: ")
                    from_id = _ask_nonempty(input_fn, "From node ID: ").upper()
                    to_id = _ask_nonempty(input_fn, "To node ID: ").upper()
                    if from_id not in mapping or to_id not in mapping:
                        raise WorkbenchError("unknown relation endpoint")
                    append_correction(
                        paths.root,
                        {"kind": "add_relation", "relation": {"type": relation_type, "from": mapping[from_id], "to": mapping[to_id]}},
                    )
                elif action == "edit":
                    relation_id = _ask_nonempty(input_fn, "Relation ID: ").upper()
                    if relation_id not in mapping or not relation_id.startswith("R"):
                        raise WorkbenchError(f"unknown relation ID: {relation_id}")
                    field = _ask_nonempty(input_fn, "Field (type/from/to): ")
                    raw_value = _ask_nonempty(input_fn, "New value: ")
                    value = mapping.get(raw_value.upper(), raw_value) if field in {"from", "to"} else raw_value
                    append_correction(paths.root, {"kind": "update_relation", "target": mapping[relation_id], "changes": {field: value}})
                elif action == "delete":
                    relation_id = _ask_nonempty(input_fn, "Relation ID: ").upper()
                    if relation_id not in mapping or not relation_id.startswith("R"):
                        raise WorkbenchError(f"unknown relation ID: {relation_id}")
                    append_correction(paths.root, {"kind": "remove_relation", "target": mapping[relation_id]})
                else:
                    raise WorkbenchError(f"unknown relation action: {action}")
            elif choice in {"b", "bind"}:
                bind_action = input_fn("Bind or unbind? [bind]: ").strip().casefold() or "bind"
                if bind_action == "unbind":
                    relation_id = _ask_nonempty(input_fn, "Relation ID to unbind: ").upper()
                    if relation_id not in mapping or not relation_id.startswith("R"):
                        raise WorkbenchError(f"unknown relation ID: {relation_id}")
                    append_correction(
                        paths.root,
                        {"kind": "remove_relation", "target": mapping[relation_id]},
                    )
                elif bind_action == "bind":
                    source_id = _ask_nonempty(input_fn, "Evidence/Assumption/Citation ID: ").upper()
                    target_id = _ask_nonempty(input_fn, "Target Claim/Evidence ID: ").upper()
                    if source_id not in mapping or target_id not in mapping:
                        raise WorkbenchError("unknown binding endpoint")
                    default_type = "assumes" if source_id.startswith("A") else "cites" if source_id.startswith("Z") else "supports"
                    relation_type = input_fn(f"Relation type [{default_type}]: ").strip() or default_type
                    append_correction(
                        paths.root,
                        {"kind": "add_relation", "relation": {"type": relation_type, "from": mapping[source_id], "to": mapping[target_id]}},
                    )
                else:
                    raise WorkbenchError(f"unknown bind action: {bind_action}")
            elif choice in {"u", "undo"}:
                entries = correction_entries(paths)
                candidates = [
                    value["correction_id"]
                    for _, value, _ in entries
                    if value.get("operation", {}).get("kind") != "revert_correction"
                ]
                if not candidates:
                    raise WorkbenchError("there are no corrections to revert")
                target = input_fn(f"Correction ID [{candidates[-1]}]: ").strip().upper() or candidates[-1]
                append_correction(paths.root, {"kind": "revert_correction", "target": target})
            else:
                output_fn("Unknown choice.")
                continue
            try:
                _, changed = rebuild_workspace(paths.root)
                output_fn("Correction saved; Reviewed IR rebuilt." if changed else "Correction saved.")
            except WorkbenchError as exc:
                output_fn(f"Correction saved; remaining validation issue: {exc}")
        except WorkbenchError as exc:
            output_fn(f"Cannot apply correction: {exc}")
