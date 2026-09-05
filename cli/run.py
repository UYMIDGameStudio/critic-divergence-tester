"""Run and prepare command family."""

from __future__ import annotations

from .core import *  # noqa: F401,F403

def write_run(
    *,
    run_dir: Path,
    protocol_name: str,
    source_path: Path,
    source_raw: bytes,
    protocol_raw: bytes,
    prompt: str,
    started_at: str,
    status: str,
    completed_at: str | None = None,
    report: str | bytes | None = None,
    stderr: str | bytes | None = None,
    executor: list[str] | None = None,
    executor_label: str | None = None,
    timeout_seconds: float | None = None,
    max_output_bytes: int | None = None,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    executor_returncode: int | None = None,
    runner_exit_code: int | None = None,
    validation: ValidationResult | None = None,
) -> None:
    prompt_bytes = prompt.encode("utf-8")
    report_bytes = report.encode("utf-8") if isinstance(report, str) else report
    stderr_bytes = stderr.encode("utf-8") if isinstance(stderr, str) else stderr

    atomic_write_bytes(run_dir / "prompt.md", prompt_bytes)
    if report_bytes is not None:
        atomic_write_bytes(run_dir / "report.md", report_bytes)
    if stderr_bytes is not None:
        atomic_write_bytes(run_dir / "stderr.log", stderr_bytes)

    manifest = {
        "schema_version": 3,
        "protocol": protocol_name,
        "source_name": source_path.name,
        "source_sha256": sha256_bytes(source_raw),
        "protocol_sha256": sha256_bytes(protocol_raw),
        "prompt_sha256": sha256_bytes(prompt_bytes),
        "report_sha256": sha256_bytes(report_bytes) if report_bytes is not None else None,
        "stderr_sha256": sha256_bytes(stderr_bytes) if stderr_bytes is not None else None,
        "started_at": started_at,
        "completed_at": completed_at,
        "status": status,
        "executor": executor_metadata(executor, executor_label),
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "executor_returncode": executor_returncode,
        "runner_exit_code": runner_exit_code,
        "report_validation": validation.as_dict() if validation is not None else None,
        "collection": None,
    }
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(run_dir / "manifest.json", manifest_text)


def _prepare_bundle(args: argparse.Namespace) -> Path:
    source_path = resolve_manuscript_path(args.manuscript)
    source_text, source_raw = read_manuscript_utf8(source_path)
    protocol, protocol_raw = load_protocol(args.protocol, args.allow_test_artifact)
    prompt = build_prompt(protocol, source_text, source_path.name)
    timestamp = utc_now()

    run_dir = new_run_dir(Path(args.runs_dir), args.protocol)
    write_run(
        run_dir=run_dir,
        protocol_name=args.protocol,
        source_path=source_path,
        source_raw=source_raw,
        protocol_raw=protocol_raw,
        prompt=prompt,
        started_at=timestamp,
        completed_at=timestamp,
        status="prepared",
        runner_exit_code=0,
    )
    return run_dir


def prepare(args: argparse.Namespace) -> int:
    run_dir = _prepare_bundle(args)
    print(run_dir / "prompt.md")
    return 0


def _print_validation_errors(validation: ValidationResult) -> None:
    for error in validation.errors:
        print(f"validation error: {error}", file=sys.stderr)


def run(
    args: argparse.Namespace,
    *,
    source_snapshot: tuple[Path, str, bytes] | None = None,
    protocol_snapshot: tuple[str, bytes] | None = None,
) -> int:
    if not args.executor:
        raise ValueError("run requires an executor command after --")

    executor = list(args.executor)
    if executor and executor[0] == "--":
        executor = executor[1:]
    if not executor:
        raise ValueError("run requires an executor command after --")
    executor_label = normalize_executor_label(getattr(args, "executor_label", None))

    raw_timeout = getattr(args, "timeout", 900.0)
    try:
        timeout_seconds = 900.0 if raw_timeout is None else float(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a positive finite number") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout must be a positive finite number")
    raw_max_output = getattr(args, "max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)
    if isinstance(raw_max_output, bool):
        raise ValueError("max output bytes must be a positive integer")
    try:
        max_output_bytes = int(raw_max_output)
    except (TypeError, ValueError) as exc:
        raise ValueError("max output bytes must be a positive integer") from exc
    if max_output_bytes <= 0:
        raise ValueError("max output bytes must be a positive integer")
    if source_snapshot is None:
        source_path = resolve_manuscript_path(args.manuscript)
        source_text, source_raw = read_manuscript_utf8(source_path)
    else:
        source_path, source_text, source_raw = source_snapshot
    protocol, protocol_raw = (
        load_protocol(args.protocol, args.allow_test_artifact)
        if protocol_snapshot is None else protocol_snapshot
    )
    prompt = build_prompt(protocol, source_text, source_path.name)
    started_at = utc_now()
    run_dir = new_run_dir(Path(args.runs_dir), args.protocol)
    args.run_dir_result = run_dir

    write_run(
        run_dir=run_dir,
        protocol_name=args.protocol,
        source_path=source_path,
        source_raw=source_raw,
        protocol_raw=protocol_raw,
        prompt=prompt,
        started_at=started_at,
        status="running",
        executor=executor,
        executor_label=executor_label,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )

    try:
        completed = execute_with_limits(
            executor,
            prompt.encode("utf-8"),
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            capture_dir=run_dir,
        )
    except OSError as exc:
        write_run(
            run_dir=run_dir,
            protocol_name=args.protocol,
            source_path=source_path,
            source_raw=source_raw,
            protocol_raw=protocol_raw,
            prompt=prompt,
            stderr=str(exc) + "\n",
            started_at=started_at,
            completed_at=utc_now(),
            status="start_failed",
            executor=executor,
            executor_label=executor_label,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            runner_exit_code=2,
        )
        print(f"error: executor failed to start; details archived in {run_dir}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        write_run(
            run_dir=run_dir,
            protocol_name=args.protocol,
            source_path=source_path,
            source_raw=source_raw,
            protocol_raw=protocol_raw,
            prompt=prompt,
            started_at=started_at,
            completed_at=utc_now(),
            status="interrupted",
            executor=executor,
            executor_label=executor_label,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            runner_exit_code=EXIT_INTERRUPTED,
        )
        raise

    try:
        report_text = completed.stdout.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        validation = ValidationResult(False, (f"report is not UTF-8: {exc}",))
    else:
        validation = validate_report(args.protocol, report_text)

    stderr = completed.stderr
    if completed.output_limit_exceeded:
        message = f"executor exceeded the {max_output_bytes}-byte combined output limit"
        stderr = stderr.rstrip() + (b"\n" if stderr else b"") + message.encode() + b"\n"
        runner_exit_code = EXIT_OUTPUT_LIMIT
        status = "output_limit_exceeded"
    elif completed.timed_out:
        message = f"executor timed out after {timeout_seconds:g} seconds"
        stderr = stderr.rstrip() + (b"\n" if stderr else b"") + message.encode() + b"\n"
        runner_exit_code = EXIT_TIMEOUT
        status = "timed_out"
    elif completed.returncode != 0:
        runner_exit_code = completed.returncode
        status = "failed"
    elif not validation.valid:
        runner_exit_code = EXIT_INVALID_REPORT
        status = "invalid_report"
    else:
        runner_exit_code = 0
        status = "succeeded"

    write_run(
        run_dir=run_dir,
        protocol_name=args.protocol,
        source_path=source_path,
        source_raw=source_raw,
        protocol_raw=protocol_raw,
        prompt=prompt,
        report=completed.stdout,
        stderr=stderr,
        started_at=started_at,
        completed_at=utc_now(),
        status=status,
        executor=executor,
        executor_label=executor_label,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        stdout_truncated=completed.stdout_truncated,
        stderr_truncated=completed.stderr_truncated,
        executor_returncode=completed.returncode,
        runner_exit_code=runner_exit_code,
        validation=validation,
    )

    quiet = bool(getattr(args, "quiet", False))
    if status in {"timed_out", "output_limit_exceeded"}:
        print(f"error: {message}; details archived in {run_dir}", file=sys.stderr)
    elif not quiet:
        print(run_dir / "report.md")
    if not validation.valid:
        _print_validation_errors(validation)
    return runner_exit_code

__all__ = [name for name in globals() if not name.startswith("__")]
