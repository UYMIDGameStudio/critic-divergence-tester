"""Shared lifecycle primitives for local Product Gate evidence directories."""

from __future__ import annotations

import shutil
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Pattern

from project_lock import ProjectMutationLockedError, project_mutation_lock


JsonEntry = tuple[Path, dict[str, Any], bytes]


@contextmanager
def atomic_gate_directory(root: Path) -> Iterator[Path]:
    """Build in a reserved sibling directory, then publish the complete Gate.

    The deterministic staging name survives a kill at any instruction, including
    immediately after mkdir and immediately before rename. The parent lock makes
    recovery exclusive. No marker needs to be removed before publication.
    """

    if root.exists() or root.is_symlink():
        raise FileExistsError(str(root))
    root.parent.mkdir(parents=True, exist_ok=True)
    with project_mutation_lock(root.parent):
        if root.exists() or root.is_symlink():
            raise FileExistsError(str(root))
        # Migrate only exact legacy tempfile names with our original marker.
        legacy_name = re.compile(r"\." + re.escape(root.name) + r"\.[a-z0-9_]{8}")
        for candidate in root.parent.iterdir():
            marker = candidate / ".gate-initialization"
            if (legacy_name.fullmatch(candidate.name) and candidate.is_dir()
                    and not candidate.is_symlink() and not marker.is_symlink()
                    and marker.is_file() and marker.read_bytes() ==
                    b"Product Gate staging directory; safe to remove after an interrupted init.\n"):
                shutil.rmtree(candidate)
        temporary = root.parent / f".{root.name}.gate-staging"
        if temporary.is_symlink() or (temporary.exists() and not temporary.is_dir()):
            raise ValueError(f"Gate staging path must be a regular directory: {temporary}")
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir()
        try:
            yield temporary
            if root.exists() or root.is_symlink():
                raise FileExistsError(str(root))
            temporary.replace(root)
        except BaseException:
            if temporary.exists() and not temporary.is_symlink():
                shutil.rmtree(temporary)
            raise


@dataclass(frozen=True)
class GateLifecycle:
    """Parameterized storage lifecycle shared by Product Gates A and B."""

    label: str
    corpus_artifact: str
    assessment_artifact: str
    decision_artifact: str
    assessment_pattern: Pattern[str]
    decision_pattern: Pattern[str]
    validator: Callable[[dict[str, Any]], list[str]]
    readiness: Callable[[Path], list[str]]
    error_type: type[Exception]
    continuous_ids: bool = True

    def initialize(
        self,
        root: Path,
        corpus_bytes: bytes,
        *,
        write_new: Callable[[Path, bytes], None],
        build_report: Callable[[Path], object],
    ) -> None:
        try:
            with atomic_gate_directory(root) as temporary:
                write_new(temporary / "corpus.json", corpus_bytes)
                (temporary / "assessments").mkdir()
                (temporary / "decisions").mkdir()
                (temporary / "report").mkdir()
                build_report(temporary)
        except ProjectMutationLockedError as exc:
            raise self.error_type(str(exc)) from exc

    def read_corpus(
        self,
        path: Path,
        *,
        read_json: Callable[[Path], tuple[dict[str, Any], bytes]],
    ) -> tuple[dict[str, Any], bytes]:
        value, data = read_json(path)
        errors = self.validator(value)
        if value.get("artifact") != self.corpus_artifact:
            errors = [f"artifact must be {self.corpus_artifact}", *errors]
        if errors:
            raise self.error_type(f"{self.label} corpus is invalid: " + "; ".join(errors))
        return value, data

    def entries(
        self,
        directory: Path,
        *,
        kind: str,
        read_json: Callable[[Path], tuple[dict[str, Any], bytes]],
    ) -> list[JsonEntry]:
        pattern = self.assessment_pattern if kind == "assessment" else self.decision_pattern
        artifact = self.assessment_artifact if kind == "assessment" else self.decision_artifact
        if directory.is_symlink() or not directory.is_dir():
            raise self.error_type(f"{self.label} {kind} directory must be regular and non-symlink")
        entries: list[JsonEntry] = []
        numbers: list[int] = []
        for path in sorted(directory.iterdir()):
            match = pattern.fullmatch(path.name)
            if path.is_symlink() or not path.is_file() or match is None:
                raise self.error_type(f"unexpected {self.label} {kind} entry: {path.name}")
            value, data = read_json(path)
            if value.get("artifact") != artifact:
                raise self.error_type(f"{path.name}: artifact must be {artifact}")
            entries.append((path, value, data))
            numbers.append(int(match.group(1)))
        if self.continuous_ids and numbers != list(range(1, len(entries) + 1)):
            raise self.error_type(f"{self.label} {kind} IDs must be continuous from 0001")
        return entries

    def validate_new(self, value: dict[str, Any], *, kind: str) -> None:
        expected = self.assessment_artifact if kind == "assessment" else self.decision_artifact
        errors = self.validator(value)
        if value.get("artifact") != expected:
            errors = [f"artifact must be {expected}", *errors]
        if errors:
            raise self.error_type(f"invalid {self.label} {kind}: " + "; ".join(errors))

    def pass_issues(self, root: Path) -> list[str]:
        return self.readiness(root)

    def rebuild_report(
        self,
        outputs: tuple[tuple[Path, bytes], ...],
        *,
        atomic_write: Callable[[Path, bytes], None],
    ) -> bool:
        """Publish deterministic report bytes without changing current outputs."""

        changed = False
        for path, data in outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.is_symlink():
                raise self.error_type(f"{self.label} report artifact must not be a symlink: {path}")
            if not path.exists() or path.read_bytes() != data:
                atomic_write(path, data)
                changed = True
        return changed

    def decision_chain_errors(
        self,
        entries: list[JsonEntry],
        *,
        digest: Callable[[bytes], str],
    ) -> list[str]:
        """Verify the common append-only ``supersedes`` decision invariant."""

        errors: list[str] = []
        previous: str | None = None
        for path, value, data in entries:
            if value.get("supersedes") != previous:
                errors.append(f"{path.name}: supersedes does not identify the prior gate decision")
            previous = digest(data)
        return errors
