"""Utils extracted from the public CLI facade."""

from __future__ import annotations

from .support import *  # noqa: F401,F403

def _ir_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _ir_read_json(raw_path: Path, label: str) -> tuple[Path, object, bytes]:
    if raw_path.is_symlink():
        raise ArgumentIRError(f"{label} must not be a symlink")
    path = raw_path.resolve()
    text, data = read_utf8(path)
    try:
        return path, parse_json(text), data
    except (json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        raise ArgumentIRError(f"{label} is not strict JSON: {exc}") from exc


def _ir_preflight_output(path: Path, data: bytes, inputs: tuple[Path, ...]) -> bool:
    """Refuse ambiguous replacement; return True when an exact artifact already exists."""
    if path.is_symlink():
        raise ArgumentIRError(f"output must not be a symlink: {path}")
    resolved = path.resolve()
    if resolved in {item.resolve() for item in inputs}:
        raise ArgumentIRError(f"output must not overwrite an input artifact: {path}")
    if resolved.exists():
        if not resolved.is_file():
            raise ArgumentIRError(f"output is not a regular file: {path}")
        if resolved.read_bytes() != data:
            raise ArgumentIRError(
                f"output already exists with different content; choose another path: {path}"
            )
        return True
    if resolved.parent.exists() and not resolved.parent.is_dir():
        raise ArgumentIRError(f"output parent is not a directory: {resolved.parent}")
    return False


def _ir_write_outputs(
    artifacts: tuple[tuple[Path, bytes], ...],
    *,
    inputs: tuple[Path, ...],
) -> None:
    resolved_outputs = [path.resolve() for path, _ in artifacts]
    if len(resolved_outputs) != len(set(resolved_outputs)):
        raise ArgumentIRError("derived artifacts must use distinct output paths")
    existing = [
        _ir_preflight_output(path, data, inputs) for path, data in artifacts
    ]
    for (path, data), already_present in zip(artifacts, existing, strict=True):
        resolved = path.resolve()
        if already_present:
            continue
        resolved.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(resolved, data)


def _ir_print_validation(kind: str, errors: list[str]) -> int:
    print(
        json.dumps(
            {"artifact": kind, "valid": not errors, "errors": errors},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not errors else EXIT_INVALID_WORKFLOW

__all__ = [name for name in globals() if not name.startswith("__")]
