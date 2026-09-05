"""Calibration campaign command family."""

from __future__ import annotations

from .core import *  # noqa: F401,F403
from .run import *  # noqa: F401,F403

def campaign(args: argparse.Namespace) -> int:
    if not args.executor:
        raise ValueError("campaign requires an executor command after --")
    executor = list(args.executor)
    if executor and executor[0] == "--":
        executor = executor[1:]
    if not executor:
        raise ValueError("campaign requires an executor command after --")
    executor_label = normalize_executor_label(getattr(args, "executor_label", None))

    if (
        not isinstance(args.repeat, int)
        or isinstance(args.repeat, bool)
        or args.repeat <= 0
    ):
        raise ValueError("campaign repeat must be a positive integer")
    try:
        timeout_seconds = float(args.timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("campaign timeout must be a positive finite number") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("campaign timeout must be a positive finite number")
    if isinstance(args.max_output_bytes, bool):
        raise ValueError("campaign max output bytes must be a positive integer")
    try:
        max_output_bytes = int(args.max_output_bytes)
    except (TypeError, ValueError) as exc:
        raise ValueError("campaign max output bytes must be a positive integer") from exc
    if max_output_bytes <= 0:
        raise ValueError("campaign max output bytes must be a positive integer")
    raw_order_seed = getattr(args, "order_seed", None)
    if raw_order_seed is None:
        order_seed = secrets.token_hex(16)
    elif (
        not isinstance(raw_order_seed, str)
        or not raw_order_seed
        or len(raw_order_seed) > 128
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in raw_order_seed
        )
    ):
        raise ValueError("campaign order seed must be 1..128 printable characters")
    else:
        order_seed = raw_order_seed

    requested_tracks = getattr(args, "track", None)
    if args.protocol and requested_tracks:
        raise ValueError("campaign accepts either --protocol or --track, not both")
    protocols = args.protocol
    if requested_tracks:
        protocols = [
            str(ACADEMIC_TRACKS[track]["primary"]) for track in requested_tracks
        ]
    protocols = protocols or ["critic-individualist", "critic-contrastivist"]
    if len(set(protocols)) != len(protocols):
        raise ValueError("campaign protocols must not contain duplicates")
    protocol_snapshots = {
        name: load_protocol(name, args.allow_test_artifact) for name in protocols
    }

    source_path = resolve_manuscript_path(args.manuscript)
    source_text, source_raw = read_manuscript_utf8(source_path)
    source_snapshot = (source_path, source_text, source_raw)
    campaign_started_at = utc_now()
    campaign_dir = new_run_dir(Path(args.campaigns_dir), "campaign")
    runs_dir = campaign_dir / "runs"
    runs_dir.mkdir()
    if os.name == "posix":
        os.chmod(runs_dir, 0o700)

    schedule = campaign_schedule(protocols, args.repeat, order_seed)
    records: list[dict[str, object]] = []
    for index, (protocol_name, repetition) in enumerate(schedule, 1):
        label = f"{PROTOCOL_PREFIX[protocol_name]}{repetition}"
        run_args = argparse.Namespace(
            protocol=protocol_name,
            manuscript=str(source_path),
            runs_dir=str(runs_dir),
            allow_test_artifact=args.allow_test_artifact,
            executor=executor,
            executor_label=executor_label,
            timeout=timeout_seconds,
            max_output_bytes=max_output_bytes,
            quiet=True,
        )
        print(f"[{index}/{len(schedule)}] {label}: running", file=sys.stderr, flush=True)
        exit_code = run(
            run_args,
            source_snapshot=source_snapshot,
            protocol_snapshot=protocol_snapshots[protocol_name],
        )
        run_dir = run_args.run_dir_result
        manifest = parse_json(
            (run_dir / "manifest.json").read_text(encoding="utf-8")
        )
        print(f"[{index}/{len(schedule)}] {label}: {manifest['status']}", file=sys.stderr, flush=True)
        relative_run = run_dir.relative_to(campaign_dir).as_posix()
        records.append(
            {
                "label": label,
                "protocol": protocol_name,
                "repetition": repetition,
                "run_dir": relative_run,
                "status": manifest["status"],
                "runner_exit_code": exit_code,
                "manifest_sha256": sha256_bytes(
                    (run_dir / "manifest.json").read_bytes()
                ),
            }
        )

    completed_at = utc_now()
    campaign_manifest = {
        "schema_version": 3,
        "source_name": source_path.name,
        "source_sha256": sha256_bytes(source_raw),
        "created_at": campaign_started_at,
        "completed_at": completed_at,
        "executor": executor_metadata(executor, executor_label),
        "protocols": protocols,
        "repeat": args.repeat,
        "order_strategy": "counterbalanced-v1",
        "order_seed": order_seed,
        "execution_order": [
            f"{PROTOCOL_PREFIX[protocol]}{repetition}"
            for protocol, repetition in schedule
        ],
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "runs": records,
    }
    can_score = (
        len(protocols) >= 2
        and args.repeat >= 2
        and all(protocol in CRITIC_PROTOCOLS for protocol in protocols)
        and all(record["status"] == "succeeded" for record in records)
    )
    if can_score:
        score_runs: dict[str, dict[str, object]] = {}
        records_by_run = {
            (str(record["protocol"]), int(record["repetition"])): record
            for record in records
        }
        for protocol_name in protocols:
            for repetition in range(1, args.repeat + 1):
                record = records_by_run[(protocol_name, repetition)]
                label = str(record["label"])
                run_dir = campaign_dir / str(record["run_dir"])
                run_manifest = parse_json(
                    (run_dir / "manifest.json").read_text(encoding="utf-8")
                )
                report, _ = read_utf8(run_dir / "report.md")
                score_runs[label] = {
                    "protocol": protocol_name,
                    "repetition": repetition,
                    "archive": record["run_dir"],
                    "report_sha256": run_manifest["report_sha256"],
                    "claims": extract_critic_claims(report),
                }
        template = campaign_pairing_scorecard(score_runs)
        atomic_write_text(
            campaign_dir / "scorecard.json",
            json.dumps(template, ensure_ascii=False, indent=2) + "\n",
        )

    summary = [
        "# Critic campaign",
        "",
        f"Source: `{source_path.name}`",
        "",
        "| Run | Protocol | Status | Report |",
        "| --- | --- | --- | --- |",
    ]
    for record in records:
        report_link = f"{record['run_dir']}/report.md"
        summary.append(
            f"| {record['label']} | {record['protocol']} | {record['status']} | "
            f"[report]({report_link}) |"
        )
    if (campaign_dir / "scorecard.json").exists():
        summary.extend(
            [
                "",
                "Create a blinded reviewer artifact and keep its identity key private:",
                "",
                "```bash",
                "python critic_runner.py blind-scorecard path/to/scorecard.json",
                "```",
                "This creates blind-review.json and blind-key.json beside the scorecard.",
                "",
                "After pairing, verify and merge the reviewer artifact:",
                "",
                "```bash",
                "python critic_runner.py apply-blind-scorecard path/to/scorecard.json",
                "```",
                "",
                "```bash",
                "python critic_runner.py score path/to/completed-scorecard.json --format markdown",
                "```",
            ]
        )
    summary_path = campaign_dir / "SUMMARY.md"
    atomic_write_text(summary_path, "\n".join(summary) + "\n")
    scorecard_path = campaign_dir / "scorecard.json"
    campaign_manifest["summary_sha256"] = sha256_bytes(summary_path.read_bytes())
    campaign_manifest["scorecard_template_sha256"] = (
        sha256_bytes(scorecard_path.read_bytes()) if scorecard_path.exists() else None
    )
    atomic_write_text(
        campaign_dir / "campaign.json",
        json.dumps(campaign_manifest, ensure_ascii=False, indent=2) + "\n",
    )

    print(campaign_dir)
    return 0 if all(record["runner_exit_code"] == 0 for record in records) else EXIT_CAMPAIGN_FAILED


def _safe_campaign_run_path(campaign_dir: Path, relative: object) -> Path | None:
    if not isinstance(relative, str) or "\\" in relative:
        return None
    relative_path = PurePosixPath(relative)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or relative_path.parts[0] != "runs"
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        return None
    candidate = campaign_dir
    for part in relative_path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            return None
    try:
        candidate.resolve().relative_to(campaign_dir.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def verify_scorecard_provenance(
    scorecard_path: Path, scorecard: object
) -> tuple[str, ...]:
    """Re-extract immutable claims from sibling campaign archives."""
    if not isinstance(scorecard, dict) or scorecard.get("schema_version") not in {2, 3}:
        return ()
    errors: list[str] = []
    runs = scorecard.get("runs")
    if not isinstance(runs, dict):
        return ("traceable scorecard runs must be an object",)
    if scorecard.get("schema_version") == 2:
        run_names = RUN_NAMES
        campaign_records: dict[str, dict[str, object]] = {}
    else:
        raw_order = scorecard.get("run_order")
        if (
            not isinstance(raw_order, list)
            or any(not isinstance(name, str) for name in raw_order)
            or len(raw_order) != len(set(raw_order))
        ):
            return ("schema v3 scorecard run_order must be a unique string list",)
        run_names = tuple(raw_order)
        missing = [name for name in runs if name not in run_names]
        extra = [name for name in run_names if name not in runs]
        if missing or extra:
            errors.append(
                f"schema v3 scorecard run_order mismatch; missing={missing}, extra={extra}"
            )
    campaign_dir = scorecard_path.parent
    if scorecard.get("schema_version") == 3:
        campaign_path = campaign_dir / "campaign.json"
        if campaign_path.is_symlink():
            errors.append("campaign.json must not be a symbolic link")
            campaign_records = {}
        else:
            try:
                campaign = parse_json(campaign_path.read_text(encoding="utf-8"))
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                DuplicateJsonKeyError,
            ) as exc:
                errors.append(f"campaign.json cannot bind scorecard identity: {exc}")
                campaign_records = {}
            else:
                raw_records = campaign.get("runs") if isinstance(campaign, dict) else None
                if not isinstance(raw_records, list):
                    errors.append("campaign runs cannot bind scorecard identity")
                    campaign_records = {}
                else:
                    campaign_records = {
                        str(record.get("label")): record
                        for record in raw_records
                        if isinstance(record, dict)
                        and isinstance(record.get("label"), str)
                    }
                    raw_protocols = campaign.get("protocols")
                    repeat = campaign.get("repeat")
                    if (
                        isinstance(raw_protocols, list)
                        and all(
                            isinstance(protocol, str)
                            and protocol in PROTOCOL_PREFIX
                            for protocol in raw_protocols
                        )
                        and isinstance(repeat, int)
                        and not isinstance(repeat, bool)
                        and repeat > 0
                    ):
                        expected_order = tuple(
                            f"{PROTOCOL_PREFIX[protocol]}{repetition}"
                            for protocol in raw_protocols
                            for repetition in range(1, repeat + 1)
                        )
                        if run_names != expected_order:
                            errors.append(
                                "schema v3 scorecard run_order does not match campaign plan"
                            )
    for run_name in run_names:
        run = runs.get(run_name)
        if not isinstance(run, dict):
            errors.append(f"runs.{run_name} must be an object")
            continue
        run_dir = _safe_campaign_run_path(campaign_dir, run.get("archive"))
        if run_dir is None:
            errors.append(f"runs.{run_name}.archive is unsafe or outside the campaign")
            continue
        if scorecard.get("schema_version") == 3:
            record = campaign_records.get(run_name)
            if record is None:
                errors.append(f"runs.{run_name} has no matching campaign record")
            else:
                for field in ("protocol", "repetition"):
                    if run.get(field) != record.get(field):
                        errors.append(
                            f"runs.{run_name}.{field} does not match campaign record"
                        )
                if run.get("archive") != record.get("run_dir"):
                    errors.append(
                        f"runs.{run_name}.archive does not match campaign record"
                    )
        report_path = run_dir / "report.md"
        if report_path.is_symlink():
            errors.append(f"runs.{run_name} report.md must not be a symbolic link")
            continue
        try:
            report_text, report_raw = read_utf8(report_path)
            extracted = extract_critic_claims(report_text)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"runs.{run_name} report cannot be re-extracted: {exc}")
            continue
        if sha256_bytes(report_raw) != run.get("report_sha256"):
            errors.append(f"runs.{run_name}.report_sha256 does not match report.md")
        if extracted != run.get("claims"):
            errors.append(f"runs.{run_name}.claims do not match archived report.md")
    return tuple(errors)


def verify_campaign_dir(
    campaign_dir: Path, source_path: Path | None = None
) -> VerificationResult:
    errors: list[str] = []
    warnings: list[str] = []
    if campaign_dir.is_symlink():
        return VerificationResult(False, ("campaign directory must not be a symbolic link",), ())
    manifest_path = campaign_dir / "campaign.json"
    if manifest_path.is_symlink():
        return VerificationResult(False, ("campaign.json must not be a symbolic link",), ())
    try:
        manifest_value = parse_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        return VerificationResult(False, (f"cannot read campaign.json: {exc}",), ())
    if not isinstance(manifest_value, dict):
        return VerificationResult(False, ("campaign.json must contain an object",), ())
    manifest: dict[str, object] = manifest_value
    schema_version = manifest.get("schema_version")
    if schema_version not in {1, 2, 3}:
        errors.append("campaign schema_version must be 1, 2, or 3")
    elif schema_version == 1:
        warnings.append("legacy campaign schema_version 1 has no explicit run matrix")
    elif schema_version == 2:
        warnings.append("legacy campaign schema_version 2 has a fixed execution order")

    source_name = manifest.get("source_name")
    if (
        not isinstance(source_name, str)
        or not source_name
        or "/" in source_name
        or "\\" in source_name
    ):
        errors.append("campaign source_name must be a non-empty basename")
    if not _valid_sha256(manifest.get("source_sha256")):
        errors.append("campaign source_sha256 is invalid")
    if source_path is None:
        warnings.append("source bytes not supplied; campaign source_sha256 was not rechecked")
    else:
        try:
            source_raw = source_path.read_bytes()
        except OSError as exc:
            errors.append(f"cannot read source file: {exc}")
        else:
            if source_path.name != source_name:
                errors.append("campaign source name does not match supplied source")
            if sha256_bytes(source_raw) != manifest.get("source_sha256"):
                errors.append("supplied source bytes do not match campaign source_sha256")

    created_at = _parse_timestamp(manifest.get("created_at"))
    completed_at = _parse_timestamp(manifest.get("completed_at"))
    if created_at is None or completed_at is None:
        errors.append("campaign timestamps must be timezone-aware ISO-8601 values")
    elif completed_at < created_at:
        errors.append("campaign completed_at cannot be earlier than created_at")

    _verify_artifact(
        campaign_dir, manifest, "SUMMARY.md", "summary_sha256", True, errors
    )
    template_hash = manifest.get("scorecard_template_sha256")
    scorecard_path = campaign_dir / "scorecard.json"
    if template_hash is not None:
        if not _valid_sha256(template_hash):
            errors.append("scorecard_template_sha256 is invalid")
        elif scorecard_path.is_symlink():
            errors.append("scorecard.json must not be a symbolic link")
        elif not scorecard_path.is_file():
            errors.append("scorecard.json is missing")
        elif sha256_bytes(scorecard_path.read_bytes()) != template_hash:
            warnings.append(
                "scorecard.json differs from its blank template, likely because it was filled"
            )
        if scorecard_path.is_file() and not scorecard_path.is_symlink():
            try:
                scorecard = parse_json(scorecard_path.read_text(encoding="utf-8"))
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                DuplicateJsonKeyError,
            ) as exc:
                errors.append(f"scorecard.json cannot be read: {exc}")
            else:
                if isinstance(scorecard, dict) and scorecard.get("schema_version") in {
                    2,
                    3,
                }:
                    try:
                        validate_pairing_scorecard(scorecard)
                    except ScorecardError as exc:
                        errors.append(f"scorecard.json structure is invalid: {exc}")
                errors.extend(verify_scorecard_provenance(scorecard_path, scorecard))
    elif scorecard_path.exists():
        warnings.append("scorecard.json exists but this campaign did not create a template")

    records = manifest.get("runs")
    if not isinstance(records, list) or not records:
        errors.append("campaign runs must be a non-empty list")
        records = []
    repeat = manifest.get("repeat")
    if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat <= 0:
        errors.append("campaign repeat must be a positive integer")
        repeat = 0
    planned_protocols: list[str] = []
    if schema_version in {2, 3}:
        raw_protocols = manifest.get("protocols")
        if (
            not isinstance(raw_protocols, list)
            or not raw_protocols
            or any(
                not isinstance(protocol, str) or protocol not in PROTOCOLS
                for protocol in raw_protocols
            )
            or len(set(raw_protocols)) != len(raw_protocols)
        ):
            errors.append("campaign protocols must be a non-empty unique protocol list")
        else:
            planned_protocols = raw_protocols
        timeout = manifest.get("timeout_seconds")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            errors.append("campaign timeout_seconds must be positive and finite")
        max_output = manifest.get("max_output_bytes")
        if (
            not isinstance(max_output, int)
            or isinstance(max_output, bool)
            or max_output <= 0
        ):
            errors.append("campaign max_output_bytes must be a positive integer")

    declared_execution_order: list[str] = []
    if schema_version == 3:
        order_seed = manifest.get("order_seed")
        execution_order = manifest.get("execution_order")
        if manifest.get("order_strategy") != "counterbalanced-v1":
            errors.append("campaign order_strategy must be counterbalanced-v1")
        if (
            not isinstance(order_seed, str)
            or not order_seed
            or len(order_seed) > 128
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in order_seed
            )
        ):
            errors.append("campaign order_seed must be 1..128 printable characters")
        if not isinstance(execution_order, list) or any(
            not isinstance(label, str) for label in execution_order
        ):
            errors.append("campaign execution_order must be a string list")
        else:
            declared_execution_order = execution_order
        if planned_protocols and repeat and isinstance(order_seed, str):
            expected_order = [
                f"{PROTOCOL_PREFIX[protocol]}{repetition}"
                for protocol, repetition in campaign_schedule(
                    planned_protocols, repeat, order_seed
                )
            ]
            if declared_execution_order != expected_order:
                errors.append("campaign execution_order does not match its seed and plan")

    campaign_executor = manifest.get("executor")
    if not isinstance(campaign_executor, dict):
        errors.append("campaign executor must contain redacted metadata")
    else:
        command = campaign_executor.get("command")
        argument_count = campaign_executor.get("argument_count")
        if (
            not isinstance(command, str)
            or not command
            or "/" in command
            or "\\" in command
        ):
            errors.append("campaign executor.command must be a non-empty basename")
        if (
            not isinstance(argument_count, int)
            or isinstance(argument_count, bool)
            or argument_count < 0
        ):
            errors.append("campaign executor.argument_count must be non-negative")
        label = campaign_executor.get("label")
        if label is not None:
            try:
                normalize_executor_label(label)
            except ValueError as exc:
                errors.append(f"campaign executor.label is invalid: {exc}")

    labels: set[str] = set()
    run_paths: set[str] = set()
    observed_runs: set[tuple[str, int]] = set()
    for index, record in enumerate(records):
        item = f"runs[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{item} must be an object")
            continue
        label = record.get("label")
        if not isinstance(label, str) or not label:
            errors.append(f"{item}.label must be a non-empty string")
        elif label in labels:
            errors.append(f"duplicate campaign label: {label}")
        else:
            labels.add(label)
        protocol = record.get("protocol")
        repetition = record.get("repetition")
        if not isinstance(protocol, str) or protocol not in PROTOCOLS:
            errors.append(f"{item}.protocol is invalid")
        elif (
            not isinstance(repetition, int)
            or isinstance(repetition, bool)
            or repetition <= 0
            or (repeat and repetition > repeat)
        ):
            errors.append(f"{item}.repetition is invalid")
        else:
            run_key = (protocol, repetition)
            if run_key in observed_runs:
                errors.append(f"duplicate campaign run: {run_key}")
            observed_runs.add(run_key)
            if label != f"{PROTOCOL_PREFIX[protocol]}{repetition}":
                errors.append(f"{item}.label does not match protocol and repetition")
        relative = record.get("run_dir")
        run_dir = _safe_campaign_run_path(campaign_dir, relative)
        if run_dir is None or relative in run_paths:
            errors.append(f"{item}.run_dir is unsafe or duplicated: {relative!r}")
            continue
        run_paths.add(relative)
        child_manifest_path = run_dir / "manifest.json"
        if child_manifest_path.is_symlink():
            errors.append(f"{item} manifest.json must not be a symbolic link")
            continue
        try:
            child_manifest_bytes = child_manifest_path.read_bytes()
            child_manifest = parse_json(child_manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError) as exc:
            errors.append(f"{item} manifest cannot be read: {exc}")
            continue
        if sha256_bytes(child_manifest_bytes) != record.get("manifest_sha256"):
            errors.append(f"{item} manifest_sha256 mismatch")
        if not isinstance(child_manifest, dict):
            errors.append(f"{item} manifest must contain an object")
            continue
        if child_manifest.get("source_name") != source_name:
            errors.append(f"{item} source_name does not match campaign")
        if child_manifest.get("source_sha256") != manifest.get("source_sha256"):
            errors.append(f"{item} source_sha256 does not match campaign")
        if child_manifest.get("executor") != campaign_executor:
            errors.append(f"{item} executor metadata does not match campaign")
        if schema_version in {2, 3}:
            if child_manifest.get("timeout_seconds") != manifest.get("timeout_seconds"):
                errors.append(f"{item} timeout_seconds does not match campaign")
            if child_manifest.get("max_output_bytes") != manifest.get("max_output_bytes"):
                errors.append(f"{item} max_output_bytes does not match campaign")
        for record_key, manifest_key in (
            ("protocol", "protocol"),
            ("status", "status"),
            ("runner_exit_code", "runner_exit_code"),
        ):
            if record.get(record_key) != child_manifest.get(manifest_key):
                errors.append(f"{item}.{record_key} does not match its run manifest")
        child = verify_run_dir(run_dir, source_path)
        errors.extend(f"{label or item}: {error}" for error in child.errors)
        warnings.extend(f"{label or item}: {warning}" for warning in child.warnings)

    if schema_version in {2, 3} and planned_protocols and repeat:
        expected_runs = {
            (protocol, repetition)
            for protocol in planned_protocols
            for repetition in range(1, repeat + 1)
        }
        if observed_runs != expected_runs:
            errors.append(
                "campaign run matrix mismatch: "
                f"missing={sorted(expected_runs - observed_runs)}, "
                f"extra={sorted(observed_runs - expected_runs)}"
            )
    if schema_version == 3:
        observed_execution_order = [
            str(record.get("label"))
            for record in records
            if isinstance(record, dict)
        ]
        if observed_execution_order != declared_execution_order:
            errors.append("campaign run record order does not match execution_order")

    return VerificationResult(not errors, tuple(errors), tuple(warnings))

__all__ = [name for name in globals() if not name.startswith("__")]
