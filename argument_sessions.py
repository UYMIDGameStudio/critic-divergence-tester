"""Append-only, system-timed human work sessions for Product Gate A."""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from argument_contracts import (
    GATE_A_WORK_ACTIVITIES,
    sha256_bytes,
    validate_artifact,
    validate_contract_bundle,
)
from argument_workbench import (
    WorkbenchError,
    _parent,
    _provenance,
    _read_json,
    _write_new,
    json_bytes,
    utc_now,
    workspace_paths,
)


SESSION_PATTERN = re.compile(r"GS([1-9][0-9]*)\Z")


@dataclass(frozen=True)
class WorkSessionPaths:
    version_dir: Path
    session_id: str

    @property
    def sessions_dir(self) -> Path:
        return self.version_dir / "gate-a-sessions"

    @property
    def root(self) -> Path:
        return self.sessions_dir / self.session_id

    @property
    def start(self) -> Path:
        return self.root / "start.json"

    @property
    def record(self) -> Path:
        return self.root / "record.json"


@dataclass(frozen=True)
class WorkSessionEntry:
    paths: WorkSessionPaths
    start: dict[str, Any]
    start_bytes: bytes
    record: dict[str, Any] | None
    record_bytes: bytes | None


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkbenchError("session timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkbenchError("session timestamp must be timezone-aware")
    return parsed


def list_work_sessions(project_dir: Path | str) -> list[WorkSessionEntry]:
    workspace = workspace_paths(project_dir)
    directory = workspace.version_dir / "gate-a-sessions"
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise WorkbenchError("gate-a-sessions must be a regular directory")
    entries: list[WorkSessionEntry] = []
    for child in sorted(directory.iterdir(), key=lambda path: path.name):
        match = SESSION_PATTERN.fullmatch(child.name)
        if match is None or child.is_symlink() or not child.is_dir():
            raise WorkbenchError(f"unexpected Gate A session entry: {child.name}")
        paths = WorkSessionPaths(workspace.version_dir, child.name)
        start, start_bytes = _read_json(paths.start)
        record: dict[str, Any] | None = None
        record_bytes: bytes | None = None
        if paths.record.exists() or paths.record.is_symlink():
            record, record_bytes = _read_json(paths.record)
        entries.append(
            WorkSessionEntry(paths, start, start_bytes, record, record_bytes)
        )
    numbers = [int(entry.paths.session_id[2:]) for entry in entries]
    if numbers != list(range(1, len(entries) + 1)):
        raise WorkbenchError("Gate A session IDs must be continuous from GS1")
    return entries


def start_work_session(
    project_dir: Path | str,
    *,
    activity: str,
    note: str = "",
    producer: str = "local-user",
) -> WorkSessionPaths:
    if activity not in GATE_A_WORK_ACTIVITIES:
        raise WorkbenchError(f"activity must be one of {GATE_A_WORK_ACTIVITIES}")
    if not isinstance(note, str):
        raise WorkbenchError("session note must be a string")
    if not producer.strip():
        raise WorkbenchError("producer must not be empty")
    workspace = workspace_paths(project_dir)
    project, _ = _read_json(workspace.project)
    version, version_bytes = _read_json(workspace.version)
    existing = list_work_sessions(workspace.root)
    open_sessions = [entry for entry in existing if entry.record is None]
    if open_sessions:
        raise WorkbenchError(
            f"finish open session {open_sessions[0].paths.session_id} before starting another"
        )
    session_id = f"GS{len(existing) + 1}"
    paths = WorkSessionPaths(workspace.version_dir, session_id)
    started_at = utc_now()
    value = {
        "schema_version": 1,
        "artifact": "gate-a-session-start",
        "artifact_id": session_id + "-start",
        "lifecycle": "immutable",
        "provenance": _provenance("human-confirmed", started_at, producer.strip()),
        "parents": [
            _parent("document-version", "document-version", version_bytes)
        ],
        "session_id": session_id,
        "project_id": project["project_id"],
        "document_id": version["document_id"],
        "version_id": version["version_id"],
        "activity": activity,
        "note": note,
        "started_at": started_at,
        "field_provenance": {
            "activity": {"origin": "human-confirmed", "source": "CLI selection"},
            "note": {"origin": "human-confirmed", "source": "CLI text"},
            "started_at": {"origin": "deterministic", "source": "system clock"},
        },
    }
    errors = validate_artifact(value)
    if errors:
        raise WorkbenchError("internal work-session start error: " + "; ".join(errors))
    paths.sessions_dir.mkdir(parents=True, exist_ok=True)
    if paths.sessions_dir.is_symlink():
        raise WorkbenchError("gate-a-sessions must not be a symbolic link")
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{session_id}.", dir=paths.sessions_dir)
    )
    try:
        _write_new(temporary / "start.json", json_bytes(value))
        temporary.replace(paths.root)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return paths


def finish_work_session(
    project_dir: Path | str,
    session_id: str,
    *,
    producer: str = "local-user",
) -> WorkSessionPaths:
    if not producer.strip():
        raise WorkbenchError("producer must not be empty")
    entries = list_work_sessions(project_dir)
    entry = next(
        (item for item in entries if item.paths.session_id == session_id), None
    )
    if entry is None:
        raise WorkbenchError(f"unknown Gate A session: {session_id}")
    if entry.record is not None:
        raise WorkbenchError(f"Gate A session {session_id} is already complete")
    workspace = workspace_paths(project_dir)
    project, _ = _read_json(workspace.project)
    version, version_bytes = _read_json(workspace.version)
    completed_at = utc_now()
    started = _parse_time(str(entry.start["started_at"]))
    completed = _parse_time(completed_at)
    elapsed = round((completed - started).total_seconds() * 1000)
    if elapsed < 0:
        raise WorkbenchError("system clock moved before the session start")
    value = {
        "schema_version": 1,
        "artifact": "gate-a-work-session",
        "artifact_id": session_id,
        "lifecycle": "immutable",
        "provenance": _provenance(
            "human-confirmed", completed_at, producer.strip()
        ),
        "parents": [
            _parent("document-version", "document-version", version_bytes),
            _parent("session-start", "gate-a-session-start", entry.start_bytes),
        ],
        "session_id": session_id,
        "project_id": project["project_id"],
        "document_id": version["document_id"],
        "version_id": version["version_id"],
        "activity": entry.start["activity"],
        "note": entry.start["note"],
        "timing": {
            "started_at": entry.start["started_at"],
            "completed_at": completed_at,
            "elapsed_milliseconds": elapsed,
        },
        "field_provenance": {
            "activity": {
                "origin": "human-confirmed",
                "source": "gate-a-session-start",
            },
            "note": {
                "origin": "human-confirmed",
                "source": "gate-a-session-start",
            },
            "timing": {
                "origin": "deterministic",
                "source": "system clock difference",
            },
        },
    }
    errors = validate_artifact(value)
    if errors:
        raise WorkbenchError("internal work-session error: " + "; ".join(errors))
    _write_new(entry.paths.record, json_bytes(value))
    return entry.paths


def abandon_work_session(
    project_dir: Path | str,
    session_id: str,
    *,
    reason: str,
    producer: str = "local-user",
) -> WorkSessionPaths:
    """Close an interrupted interval without claiming that work was completed."""
    if not producer.strip():
        raise WorkbenchError("producer must not be empty")
    if not isinstance(reason, str) or not reason.strip():
        raise WorkbenchError("abandonment reason must not be empty")
    entries = list_work_sessions(project_dir)
    entry = next(
        (item for item in entries if item.paths.session_id == session_id), None
    )
    if entry is None:
        raise WorkbenchError(f"unknown Gate A session: {session_id}")
    if entry.record is not None:
        raise WorkbenchError(f"Gate A session {session_id} is already closed")
    workspace = workspace_paths(project_dir)
    project, _ = _read_json(workspace.project)
    version, version_bytes = _read_json(workspace.version)
    abandoned_at = utc_now()
    started = _parse_time(str(entry.start["started_at"]))
    abandoned = _parse_time(abandoned_at)
    elapsed = round((abandoned - started).total_seconds() * 1000)
    if elapsed < 0:
        raise WorkbenchError("system clock moved before the session start")
    value = {
        "schema_version": 1,
        "artifact": "gate-a-session-abandonment",
        "artifact_id": session_id + "-abandoned",
        "lifecycle": "immutable",
        "provenance": _provenance(
            "human-confirmed", abandoned_at, producer.strip()
        ),
        "parents": [
            _parent("document-version", "document-version", version_bytes),
            _parent("session-start", "gate-a-session-start", entry.start_bytes),
        ],
        "session_id": session_id,
        "project_id": project["project_id"],
        "document_id": version["document_id"],
        "version_id": version["version_id"],
        "activity": entry.start["activity"],
        "note": entry.start["note"],
        "reason": reason.strip(),
        "timing": {
            "started_at": entry.start["started_at"],
            "abandoned_at": abandoned_at,
            "elapsed_milliseconds": elapsed,
        },
        "field_provenance": {
            "activity": {
                "origin": "human-confirmed",
                "source": "gate-a-session-start",
            },
            "note": {
                "origin": "human-confirmed",
                "source": "gate-a-session-start",
            },
            "reason": {
                "origin": "human-confirmed",
                "source": "CLI text",
            },
            "timing": {
                "origin": "deterministic",
                "source": "system clock difference",
            },
        },
    }
    errors = validate_artifact(value)
    if errors:
        raise WorkbenchError("internal session-abandonment error: " + "; ".join(errors))
    _write_new(entry.paths.record, json_bytes(value))
    return entry.paths


def render_work_sessions(entries: list[WorkSessionEntry]) -> str:
    lines = ["Product Gate A human work sessions", ""]
    if not entries:
        lines.extend(["No sessions recorded.", ""])
        return "\n".join(lines)
    for entry in entries:
        if entry.record is None:
            state = f"open since {entry.start['started_at']}"
        elif entry.record.get("artifact") == "gate-a-session-abandonment":
            elapsed = int(entry.record["timing"]["elapsed_milliseconds"])
            state = f"abandoned · {elapsed / 60000:.2f} minutes"
        else:
            elapsed = int(entry.record["timing"]["elapsed_milliseconds"])
            state = f"complete · {elapsed / 60000:.2f} minutes"
        lines.append(
            f"{entry.paths.session_id} · {entry.start['activity']} · {state}"
        )
        if entry.start["note"]:
            lines.append(f"  {entry.start['note']}")
        if (
            entry.record is not None
            and entry.record.get("artifact") == "gate-a-session-abandonment"
        ):
            lines.append(f"  Abandoned: {entry.record['reason']}")
    lines.append("")
    return "\n".join(lines)


def verify_work_sessions(project_dir: Path | str) -> list[str]:
    workspace = workspace_paths(project_dir)
    errors: list[str] = []
    try:
        project, project_bytes = _read_json(workspace.project)
        document, document_bytes = _read_json(workspace.document)
        version, version_bytes = _read_json(workspace.version)
        entries = list_work_sessions(workspace.root)
    except (OSError, WorkbenchError) as exc:
        return [str(exc)]
    for entry in entries:
        prefix = entry.paths.session_id
        expected_files = {"start.json"}
        if entry.record is not None:
            expected_files.add("record.json")
        if {child.name for child in entry.paths.root.iterdir()} != expected_files:
            errors.append(f"{prefix}: session directory has unexpected entries")
        start_errors = validate_artifact(entry.start)
        errors.extend(f"{prefix}/start: {error}" for error in start_errors)
        for key in ("project_id", "document_id", "version_id"):
            expected = project.get(key) if key == "project_id" else version.get(key)
            if entry.start.get(key) != expected:
                errors.append(f"{prefix}/start: {key} does not match workspace")
        bundle = [
            (project, project_bytes),
            (document, document_bytes),
            (version, version_bytes),
            (entry.start, entry.start_bytes),
        ]
        if entry.record is not None and entry.record_bytes is not None:
            record_errors = validate_artifact(entry.record)
            errors.extend(f"{prefix}/record: {error}" for error in record_errors)
            for key in (
                "session_id",
                "project_id",
                "document_id",
                "version_id",
                "activity",
                "note",
            ):
                if entry.record.get(key) != entry.start.get(key):
                    errors.append(f"{prefix}/record: {key} differs from session start")
            if entry.record.get("timing", {}).get("started_at") != entry.start.get(
                "started_at"
            ):
                errors.append(f"{prefix}/record: timing start is disconnected")
            parents = {
                parent.get("role"): parent
                for parent in entry.record.get("parents", [])
                if isinstance(parent, dict)
            }
            if parents.get("session-start", {}).get("sha256") != sha256_bytes(
                entry.start_bytes
            ):
                errors.append(f"{prefix}/record: session-start parent is disconnected")
            bundle.append((entry.record, entry.record_bytes))
        bundle_errors = validate_contract_bundle(bundle)
        errors.extend(f"{prefix}: {error}" for error in bundle_errors)
    return errors
