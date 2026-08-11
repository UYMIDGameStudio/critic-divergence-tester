"""Immutable direct-chat baselines for reproducible Product Gate A comparison."""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from argument_contracts import (
    BASELINE_INTERACTION_MODES,
    BASELINE_MANUSCRIPT_DELIVERY,
    BASELINE_PRIOR_CONTEXTS,
    sha256_bytes,
    validate_artifact,
    validate_contract_bundle,
)
from argument_workbench import (
    WorkbenchError,
    _parent,
    _read_json,
    _write_new,
    json_bytes,
    workspace_paths,
)


BASELINE_PATTERN = re.compile(r"DB([1-9][0-9]*)\Z")


@dataclass(frozen=True)
class DirectBaselinePaths:
    version_dir: Path
    baseline_id: str

    @property
    def baselines_dir(self) -> Path:
        return self.version_dir / "direct-review-baselines"

    @property
    def root(self) -> Path:
        return self.baselines_dir / self.baseline_id

    @property
    def record(self) -> Path:
        return self.root / "record.json"

    @property
    def prompt(self) -> Path:
        return self.root / "prompt.md"

    @property
    def response(self) -> Path:
        return self.root / "response.md"


def _parse_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkbenchError(f"{label} must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkbenchError(f"{label} must be a timezone-aware ISO timestamp")
    return parsed


def _regular_input(path: Path | str, label: str) -> tuple[Path, bytes]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise WorkbenchError(f"{label} must not be a symbolic link")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise WorkbenchError(f"{label} does not exist: {resolved}")
    data = resolved.read_bytes()
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkbenchError(f"{label} must be UTF-8 text") from exc
    if not data:
        raise WorkbenchError(f"{label} must not be empty")
    return resolved, data


def list_direct_review_baselines(
    project_dir: Path | str,
) -> list[tuple[DirectBaselinePaths, dict[str, Any], bytes]]:
    workspace = workspace_paths(project_dir)
    directory = workspace.version_dir / "direct-review-baselines"
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise WorkbenchError("direct-review-baselines must be a regular directory")
    entries: list[tuple[DirectBaselinePaths, dict[str, Any], bytes]] = []
    for child in sorted(directory.iterdir(), key=lambda path: path.name):
        match = BASELINE_PATTERN.fullmatch(child.name)
        if match is None or child.is_symlink() or not child.is_dir():
            raise WorkbenchError(f"unexpected direct baseline entry: {child.name}")
        paths = DirectBaselinePaths(workspace.version_dir, child.name)
        record, record_bytes = _read_json(paths.record)
        entries.append((paths, record, record_bytes))
    numbers = [int(paths.baseline_id[2:]) for paths, _, _ in entries]
    if numbers != list(range(1, len(entries) + 1)):
        raise WorkbenchError("direct baseline IDs must be continuous from DB1")
    return entries


def latest_direct_review_baseline(
    project_dir: Path | str,
) -> tuple[DirectBaselinePaths, dict[str, Any], bytes]:
    entries = list_direct_review_baselines(project_dir)
    if not entries:
        raise WorkbenchError(
            "project has no direct-review baseline; run `ir gate-a baseline`"
        )
    return entries[-1]


def controlled_baseline_errors(record: dict[str, Any]) -> list[str]:
    """Return experiment-control failures without invalidating historical v1 records."""
    errors: list[str] = []
    if record.get("schema_version") != 2:
        return ["latest direct-review baseline must use controlled schema v2"]
    conditions = record.get("conditions")
    if not isinstance(conditions, dict):
        return ["controlled baseline conditions are missing"]
    if conditions.get("interaction_mode") != "fresh-session":
        errors.append("direct review must use a fresh session")
    if conditions.get("prior_context") != "none":
        errors.append("direct review must declare no prior conversational context")
    if conditions.get("full_manuscript_confirmed") is not True:
        errors.append("direct review must include the full manuscript")
    return errors


def collect_direct_review_baseline(
    project_dir: Path | str,
    *,
    prompt_file: Path | str,
    response_file: Path | str,
    model_label: str,
    model_provider: str,
    model_id: str,
    interaction_mode: str,
    prior_context: str,
    manuscript_delivery: str,
    full_manuscript_confirmed: bool,
    started_at: str,
    completed_at: str,
    producer_label: str = "direct-chat-model",
) -> DirectBaselinePaths:
    for label, value in (
        ("model label", model_label),
        ("model provider", model_provider),
        ("model ID", model_id),
    ):
        if not value.strip():
            raise WorkbenchError(f"{label} must not be empty")
    if not producer_label.strip():
        raise WorkbenchError("producer label must not be empty")
    if interaction_mode not in BASELINE_INTERACTION_MODES:
        raise WorkbenchError(
            f"interaction mode must be one of {BASELINE_INTERACTION_MODES}"
        )
    if prior_context not in BASELINE_PRIOR_CONTEXTS:
        raise WorkbenchError(f"prior context must be one of {BASELINE_PRIOR_CONTEXTS}")
    if manuscript_delivery not in BASELINE_MANUSCRIPT_DELIVERY:
        raise WorkbenchError(
            f"manuscript delivery must be one of {BASELINE_MANUSCRIPT_DELIVERY}"
        )
    if not isinstance(full_manuscript_confirmed, bool):
        raise WorkbenchError("full manuscript confirmation must be boolean")
    started = _parse_time(started_at, "started_at")
    completed = _parse_time(completed_at, "completed_at")
    elapsed_milliseconds = round((completed - started).total_seconds() * 1000)
    if elapsed_milliseconds < 0:
        raise WorkbenchError("completed_at must not precede started_at")
    prompt_path, prompt_bytes = _regular_input(prompt_file, "baseline prompt")
    response_path, response_bytes = _regular_input(response_file, "baseline response")
    workspace = workspace_paths(project_dir)
    version, version_bytes = _read_json(workspace.version)
    project, _ = _read_json(workspace.project)
    source_relative = str(version["source"]["relative_path"])
    source_path = workspace.version_dir / source_relative
    if source_path.is_symlink() or not source_path.is_file():
        raise WorkbenchError("DocumentVersion source must be a regular file")
    source_bytes = source_path.read_bytes()
    if sha256_bytes(source_bytes) != version["source"]["sha256"]:
        raise WorkbenchError("DocumentVersion source hash is disconnected")
    existing = list_direct_review_baselines(workspace.root)
    baseline_id = f"DB{len(existing) + 1}"
    paths = DirectBaselinePaths(workspace.version_dir, baseline_id)
    record = {
        "schema_version": 2,
        "artifact": "direct-review-baseline",
        "artifact_id": baseline_id,
        "lifecycle": "immutable",
        "provenance": {
            "origin": "model-derived",
            "created_at": completed_at,
            "producer": producer_label.strip(),
        },
        "parents": [
            _parent("document-version", "document-version", version_bytes)
        ],
        "baseline_id": baseline_id,
        "project_id": project["project_id"],
        "document_id": version["document_id"],
        "version_id": version["version_id"],
        "model": {
            "label": model_label.strip(),
            "provider": model_provider.strip(),
            "model_id": model_id.strip(),
        },
        "timing": {
            "started_at": started_at,
            "completed_at": completed_at,
            "elapsed_milliseconds": elapsed_milliseconds,
        },
        "source": {
            "relative_path": source_relative,
            "sha256": sha256_bytes(source_bytes),
        },
        "prompt": {
            "relative_path": "prompt.md",
            "sha256": sha256_bytes(prompt_bytes),
        },
        "response": {
            "relative_path": "response.md",
            "sha256": sha256_bytes(response_bytes),
        },
        "collection": {
            "method": "file",
            "prompt_source_name": prompt_path.name,
            "response_source_name": response_path.name,
        },
        "conditions": {
            "interaction_mode": interaction_mode,
            "prior_context": prior_context,
            "manuscript_delivery": manuscript_delivery,
            "full_manuscript_confirmed": full_manuscript_confirmed,
        },
        "field_provenance": {
            "source": {"origin": "deterministic", "source": "document-version"},
            "prompt": {"origin": "human-confirmed", "source": "supplied-file"},
            "response": {"origin": "model-derived", "source": "supplied-file"},
            "model": {"origin": "human-confirmed", "source": "CLI metadata"},
            "timestamps": {
                "origin": "human-confirmed",
                "source": "CLI metadata",
            },
            "elapsed_milliseconds": {
                "origin": "deterministic",
                "source": "completed_at minus started_at",
            },
            "conditions": {
                "origin": "human-confirmed",
                "source": "CLI declarations",
            },
        },
    }
    errors = validate_artifact(record)
    if errors:
        raise WorkbenchError("internal direct baseline contract error: " + "; ".join(errors))
    paths.baselines_dir.mkdir(parents=True, exist_ok=True)
    if paths.baselines_dir.is_symlink():
        raise WorkbenchError("direct-review-baselines must not be a symbolic link")
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{baseline_id}.", dir=paths.baselines_dir)
    )
    try:
        _write_new(temporary / "prompt.md", prompt_bytes)
        _write_new(temporary / "response.md", response_bytes)
        _write_new(temporary / "record.json", json_bytes(record))
        temporary.replace(paths.root)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return paths


def verify_direct_review_baselines(project_dir: Path | str) -> list[str]:
    workspace = workspace_paths(project_dir)
    errors: list[str] = []
    try:
        version, version_bytes = _read_json(workspace.version)
        project, project_bytes = _read_json(workspace.project)
        document, document_bytes = _read_json(workspace.document)
        entries = list_direct_review_baselines(workspace.root)
    except (OSError, WorkbenchError) as exc:
        return [str(exc)]
    for paths, record, record_bytes in entries:
        prefix = paths.baseline_id
        contract_errors = validate_artifact(record)
        errors.extend(f"{prefix}: {error}" for error in contract_errors)
        if contract_errors:
            continue
        try:
            if (
                paths.prompt.is_symlink()
                or paths.response.is_symlink()
                or not paths.prompt.is_file()
                or not paths.response.is_file()
            ):
                raise WorkbenchError(
                    "prompt/response must be regular non-symlink files"
                )
            prompt_bytes = paths.prompt.read_bytes()
            response_bytes = paths.response.read_bytes()
            source_path = workspace.version_dir / str(record["source"]["relative_path"])
            resolved_source = source_path.resolve()
            resolved_source.relative_to(workspace.version_dir.resolve())
            if source_path.is_symlink() or not source_path.is_file():
                raise WorkbenchError("bound source must be a regular non-symlink file")
            source_bytes = source_path.read_bytes()
        except (OSError, KeyError, TypeError, ValueError, WorkbenchError) as exc:
            errors.append(f"{prefix}: cannot read bound bytes: {exc}")
            continue
        if {child.name for child in paths.root.iterdir()} != {
            "record.json",
            "prompt.md",
            "response.md",
        }:
            errors.append(f"{prefix}: baseline directory has unexpected entries")
        expected_hashes = {
            "source": sha256_bytes(source_bytes),
            "prompt": sha256_bytes(prompt_bytes),
            "response": sha256_bytes(response_bytes),
        }
        for field, digest in expected_hashes.items():
            if record.get(field, {}).get("sha256") != digest:
                errors.append(f"{prefix}: {field} exact-byte hash is disconnected")
        expected_source = {
            "relative_path": version.get("source", {}).get("relative_path"),
            "sha256": version.get("source", {}).get("sha256"),
        }
        if record.get("source") != expected_source:
            errors.append(f"{prefix}: source binding does not match DocumentVersion")
        if record.get("project_id") != project.get("project_id"):
            errors.append(f"{prefix}: project_id does not match Project")
        for field in ("document_id", "version_id"):
            if record.get(field) != version.get(field):
                errors.append(f"{prefix}: {field} does not match DocumentVersion")
        entries_for_bundle = [
            (project, project_bytes),
            (document, document_bytes),
            (version, version_bytes),
            (record, record_bytes),
        ]
        bundle_errors = validate_contract_bundle(entries_for_bundle)
        errors.extend(f"{prefix}: {error}" for error in bundle_errors)
    return errors
