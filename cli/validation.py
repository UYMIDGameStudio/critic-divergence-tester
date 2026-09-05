"""Validation extracted from the public CLI facade."""

from __future__ import annotations

from .support import *  # noqa: F401,F403
from .legacy_commands import *  # noqa: F401,F403

def validate_command(args: argparse.Namespace) -> int:
    report_path = Path(args.report).resolve()
    report, _ = read_utf8(report_path)
    validation = validate_report(args.protocol, report)
    if validation.valid:
        print("valid")
        return 0
    _print_validation_errors(validation)
    return EXIT_INVALID_REPORT


def verify_run_command(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).absolute()
    source_path = Path(args.source).resolve() if args.source else None
    verification = verify_run_dir(run_dir, source_path)
    for warning in verification.warnings:
        print(f"verification warning: {warning}", file=sys.stderr)
    if verification.valid:
        print("verified")
        return 0
    for error in verification.errors:
        print(f"verification error: {error}", file=sys.stderr)
    return EXIT_INVALID_ARCHIVE


def _run_overview(run_dir: Path) -> RunOverview:
    verification = verify_run_dir(run_dir)
    if not verification.valid:
        detail = verification.errors[0] if verification.errors else "unknown error"
        return RunOverview(
            run_dir,
            "归档损坏",
            "—",
            "—",
            "invalid",
            f"先运行 verify-run 检查：{detail}",
            verification,
        )
    try:
        manifest_value = parse_json(
            (run_dir / "manifest.json").read_text(encoding="utf-8")
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateJsonKeyError,
    ) as exc:
        invalid = VerificationResult(False, (str(exc),), ())
        return RunOverview(
            run_dir,
            "归档损坏",
            "—",
            "—",
            "invalid",
            f"无法读取 manifest：{exc}",
            invalid,
        )
    assert isinstance(manifest_value, dict)
    protocol = str(manifest_value.get("protocol", "—"))
    source = str(manifest_value.get("source_name", "—"))
    status = manifest_value.get("status")
    launcher = _python_launcher()
    quoted_run = f'"{run_dir}"'
    if status == "prepared":
        return RunOverview(
            run_dir,
            "等待 AI 报告",
            protocol,
            source,
            "import-report",
            f"直接粘贴 AI 回答：{launcher} critic_runner.py resume {quoted_run} --paste",
            verification,
        )
    if status == "collected" and protocol in CRITIC_PROTOCOLS:
        adjudication = parse_json(
            (run_dir / "adjudication.json").read_text(encoding="utf-8")
        )
        assert isinstance(adjudication, dict)
        findings = adjudication.get("findings")
        assert isinstance(findings, list)
        decided = sum(
            isinstance(finding, dict)
            and finding.get("decision") in {"accept", "reject", "defer"}
            for finding in findings
        )
        total = len(findings)
        if decided < total:
            return RunOverview(
                run_dir,
                f"人工裁决 {decided}/{total}",
                protocol,
                source,
                "adjudicate",
                f"继续运行：{launcher} critic_runner.py resume {quoted_run}",
                verification,
            )
        if not (run_dir / "revision-plan.md").exists():
            return RunOverview(
                run_dir,
                f"待生成修改计划 {decided}/{total}",
                protocol,
                source,
                "revision-plan",
                f"运行：{launcher} critic_runner.py resume {quoted_run}",
                verification,
            )
        return RunOverview(
            run_dir,
            f"已完成 {decided}/{total}",
            protocol,
            source,
            "complete",
            "查看 revision-plan.md；需要复议时运行："
            f"{launcher} critic_runner.py adjudicate {quoted_run} --review-all",
            verification,
        )
    if status == "collected":
        return RunOverview(
            run_dir,
            "报告已回收",
            protocol,
            source,
            "complete",
            "查看 report.md",
            verification,
        )
    if status == "succeeded":
        return RunOverview(
            run_dir,
            "自动执行完成",
            protocol,
            source,
            "complete",
            "查看 report.md",
            verification,
        )
    if status == "running":
        return RunOverview(
            run_dir,
            "执行中或意外中断",
            protocol,
            source,
            "inspect",
            "检查执行进程；必要时运行 verify-run",
            verification,
        )
    return RunOverview(
        run_dir,
        f"执行未完成：{status}",
        protocol,
        source,
        "inspect",
        "查看 manifest.json 和 stderr.log",
        verification,
    )


def _invalid_symlink_overview(run_dir: Path) -> RunOverview:
    return RunOverview(
        run_dir,
        "归档损坏",
        "—",
        "—",
        "invalid",
        "符号链接不会被跟随；请检查此目录",
        VerificationResult(False, ("run directory must not be a symbolic link",), ()),
    )


def _list_run_dirs(raw_runs_root: Path) -> list[Path]:
    if raw_runs_root.is_symlink():
        raise ValueError("runs directory must not be a symbolic link")
    runs_root = raw_runs_root.resolve()
    if not runs_root.exists():
        return []
    if not runs_root.is_dir():
        raise ValueError("runs path is not a directory")
    return sorted(
        (
            entry
            for entry in runs_root.iterdir()
            if entry.is_dir() or entry.is_symlink()
        ),
        key=lambda entry: entry.name,
        reverse=True,
    )


def _safe_run_overview(run_dir: Path) -> RunOverview:
    if run_dir.is_symlink():
        return _invalid_symlink_overview(run_dir)
    try:
        return _run_overview(run_dir)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateJsonKeyError,
    ) as exc:
        return RunOverview(
            run_dir,
            "归档损坏",
            "—",
            "—",
            "invalid",
            f"无法读取：{exc}",
            VerificationResult(False, (str(exc),), ()),
        )


def status_command(args: argparse.Namespace) -> int:
    requested_run = getattr(args, "run_dir", None)
    if requested_run:
        raw_run = Path(requested_run)
        if raw_run.is_symlink():
            print("状态：归档损坏")
            print("问题：run directory must not be a symbolic link")
            return EXIT_INVALID_ARCHIVE
        run_dirs = [raw_run.resolve()]
        detailed = True
    else:
        try:
            run_dirs = _list_run_dirs(
                Path(getattr(args, "runs_dir", ".critic-runs"))
            )
        except ValueError as exc:
            print(f"status error: {exc}", file=sys.stderr)
            return EXIT_INVALID_WORKFLOW
        detailed = False
        if not run_dirs:
            print("还没有运行记录。先运行 quickstart 创建第一次审查。")
            return 0

    for index, run_dir in enumerate(run_dirs, start=1):
        overview = _safe_run_overview(run_dir)
        print(f"[{index}] {overview.stage}｜{overview.protocol}｜{overview.source}")
        print(f"    目录：{overview.run_dir}")
        print(f"    下一步：{overview.next_action}")
        if detailed and not overview.verification.valid:
            for error in overview.verification.errors:
                print(f"    问题：{error}")
    if not detailed:
        print(f"\n共 {len(run_dirs)} 次运行；最新记录显示在最前。")
    if detailed and not overview.verification.valid:
        return EXIT_INVALID_ARCHIVE
    return 0


def read_pasted_report_bytes() -> bytes:
    print("请从下一行开始粘贴完整 AI 回答。")
    print(f"粘贴结束后，另起一行只输入 {PASTE_END_MARKER} 并回车。")
    lines: list[str] = []
    total_bytes = 0
    too_large = False
    while True:
        try:
            line = input()
        except EOFError as exc:
            raise ValueError(
                f"输入在结束标记 {PASTE_END_MARKER} 之前结束；报告未导入"
            ) from exc
        if line.strip() == PASTE_END_MARKER:
            break
        try:
            line_size = len((line + "\n").encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError(f"粘贴内容无法编码为 UTF-8：{exc}") from exc
        total_bytes += line_size
        if total_bytes > DEFAULT_MAX_OUTPUT_BYTES:
            too_large = True
            continue
        lines.append(line)
    if too_large:
        raise ValueError(
            f"粘贴报告超过 {DEFAULT_MAX_OUTPUT_BYTES} 字节；报告未导入"
        )
    if not any(line.strip() for line in lines):
        raise ValueError("粘贴报告不能为空")
    return ("\n".join(lines) + "\n").encode("utf-8")


def resume_command(args: argparse.Namespace) -> int:
    pasted_input = bool(getattr(args, "paste", False))
    report_argument = getattr(args, "report", None)
    if pasted_input and report_argument:
        print("resume error: --paste and --report cannot be combined", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    requested_run = getattr(args, "run_dir", None)
    if requested_run:
        raw_run = Path(requested_run)
        overview = _safe_run_overview(
            raw_run if raw_run.is_symlink() else raw_run.resolve()
        )
        actionable_count = 1 if overview.action in {
            "import-report",
            "adjudicate",
            "revision-plan",
        } else 0
    else:
        try:
            run_dirs = _list_run_dirs(
                Path(getattr(args, "runs_dir", ".critic-runs"))
            )
        except ValueError as exc:
            print(f"resume error: {exc}", file=sys.stderr)
            return EXIT_INVALID_WORKFLOW
        if not run_dirs:
            print("还没有运行记录。请先运行 quickstart 创建第一次审查。")
            return 0
        overviews = [_safe_run_overview(run_dir) for run_dir in run_dirs]
        damaged = [item for item in overviews if item.action == "invalid"]
        actionable = [
            item
            for item in overviews
            if item.action in {"import-report", "adjudicate", "revision-plan"}
        ]
        if not actionable:
            if damaged:
                print("没有可安全继续的运行，但发现损坏归档：", file=sys.stderr)
                for item in damaged:
                    print(f"- {item.run_dir}: {item.next_action}", file=sys.stderr)
                return EXIT_INVALID_ARCHIVE
            latest = overviews[0]
            if latest.action == "complete":
                print("没有待继续的运行。最新一次审查已经结束：")
            else:
                print("没有可自动继续的运行。最新记录需要人工检查：")
            print(f"{latest.stage}｜{latest.protocol}｜{latest.source}")
            print(latest.run_dir)
            print(latest.next_action)
            return 0
        overview = actionable[0]
        actionable_count = len(actionable)
        if damaged:
            print(
                f"警告：另有 {len(damaged)} 个损坏归档未处理；可用 status 查看。",
                file=sys.stderr,
            )

    if not overview.verification.valid:
        for error in overview.verification.errors:
            print(f"resume archive error: {error}", file=sys.stderr)
        return EXIT_INVALID_ARCHIVE
    if overview.action not in {"import-report", "adjudicate", "revision-plan"}:
        if report_argument or pasted_input:
            print(
                "resume error: --report/--paste can only be used with a prepared run",
                file=sys.stderr,
            )
            return EXIT_INVALID_WORKFLOW
        print(f"这次运行当前不需要继续：{overview.stage}")
        print(overview.next_action)
        return 0

    print(f"继续处理：{overview.source}｜{overview.protocol}｜{overview.stage}")
    print(f"运行目录：{overview.run_dir}")
    if actionable_count > 1:
        print(
            f"另有 {actionable_count - 1} 次待办；本次自动选择最新的一次。"
            "如需指定，请使用 resume <运行目录>。"
        )

    if overview.action == "import-report":
        if pasted_input:
            try:
                pasted_report = read_pasted_report_bytes()
            except ValueError as exc:
                print(f"错误：{exc}", file=sys.stderr)
                return EXIT_INVALID_REPORT
            except KeyboardInterrupt:
                print("\n已取消。", file=sys.stderr)
                return EXIT_INTERRUPTED
            try:
                with tempfile.TemporaryDirectory(prefix="critic-paste-") as temp_dir:
                    report_path = Path(temp_dir) / "pasted-report.md"
                    atomic_write_bytes(report_path, pasted_report)
                    result = import_report_command(
                        argparse.Namespace(
                            run_dir=str(overview.run_dir),
                            report=str(report_path),
                            adjudication_output=None,
                            collection_method="terminal-paste",
                            collection_source_name="pasted-report.md",
                        )
                    )
            except OSError as exc:
                print(f"resume paste error: {exc}", file=sys.stderr)
                return EXIT_INVALID_WORKFLOW
        else:
            report = report_argument
            if report is None:
                try:
                    report = input("请粘贴已保存的 AI 报告路径：")
                except EOFError:
                    print("\n错误：没有收到报告路径。", file=sys.stderr)
                    return 2
                except KeyboardInterrupt:
                    print("\n已取消。", file=sys.stderr)
                    return EXIT_INTERRUPTED
            report = _unquote_path(str(report))
            if not report:
                print("错误：报告路径不能为空。", file=sys.stderr)
                return 2
            report = str(Path(report).expanduser())
            result = import_report_command(
                argparse.Namespace(
                    run_dir=str(overview.run_dir),
                    report=report,
                    adjudication_output=None,
                )
            )
        if result != 0:
            return result
        if overview.protocol not in CRITIC_PROTOCOLS:
            print("报告已安全回收；该协议不需要人工裁决。")
            return 0
        return adjudicate_command(
            argparse.Namespace(run_dir=str(overview.run_dir), review_all=False)
        )
    if report_argument or pasted_input:
        print(
            "resume error: --report/--paste can only be used with a prepared run",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW
    if overview.action == "adjudicate":
        return adjudicate_command(
            argparse.Namespace(run_dir=str(overview.run_dir), review_all=False)
        )
    return revision_plan_command(
        argparse.Namespace(
            run_dir=str(overview.run_dir), adjudication=None, output=None
        )
    )


def verify_campaign_command(args: argparse.Namespace) -> int:
    campaign_dir = Path(args.campaign_dir).absolute()
    source_path = Path(args.source).resolve() if args.source else None
    verification = verify_campaign_dir(campaign_dir, source_path)
    for warning in verification.warnings:
        print(f"verification warning: {warning}", file=sys.stderr)
    if verification.valid:
        print("verified")
        return 0
    for error in verification.errors:
        print(f"verification error: {error}", file=sys.stderr)
    return EXIT_INVALID_ARCHIVE


def list_protocols(_: argparse.Namespace) -> int:
    for name in PROTOCOLS:
        suffix = " [test-only]" if name in TEST_ONLY else ""
        print(f"{name}{suffix}")
    return 0


def list_tracks(_: argparse.Namespace) -> int:
    for name, track in ACADEMIC_TRACKS.items():
        specialists = ", ".join(track["specialists"]) or "none"
        print(f"{name}: {track['label']}")
        print(f"  primary: {track['primary']}")
        print(f"  specialists: {specialists}")
    print("cross-disciplinary: " + ", ".join(CROSS_DISCIPLINARY_PROTOCOLS))
    return 0


def doctor(args: argparse.Namespace) -> int:
    errors: list[str] = []
    checks: list[str] = []
    warnings: list[str] = []
    if sys.version_info < (3, 10):
        errors.append("Python 3.10 or newer is required")
    else:
        checks.append(f"Python {sys.version_info.major}.{sys.version_info.minor}")

    expected_protocols = set(PROTOCOLS)
    if set(PROTOCOL_PREFIX) != expected_protocols:
        errors.append("protocol prefix registry does not match available protocols")
    elif len(set(PROTOCOL_PREFIX.values())) != len(PROTOCOL_PREFIX):
        errors.append("protocol prefixes must be unique")
    else:
        checks.append("protocol prefixes are complete and unique")

    for name in PROTOCOLS:
        try:
            body, _ = load_protocol(name, allow_test_artifact=name in TEST_ONLY)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"{name} cannot be loaded: {exc}")
            continue
        if name in CRITIC_PROTOCOLS:
            for heading in CRITIC_SECTIONS:
                if body.count(heading) != 1:
                    errors.append(f"{name} must contain {heading!r} exactly once")
    if not any("cannot be loaded" in error or "must contain" in error for error in errors):
        checks.append(f"{len(PROTOCOLS)} protocol files are readable and structurally valid")

    referenced_protocols = set(CROSS_DISCIPLINARY_PROTOCOLS)
    for track in ACADEMIC_TRACKS.values():
        referenced_protocols.add(str(track["primary"]))
        referenced_protocols.update(str(name) for name in track["specialists"])
    missing_references = sorted(referenced_protocols - expected_protocols)
    if missing_references:
        errors.append(f"academic track registry references missing protocols: {missing_references}")
    else:
        checks.append(f"{len(ACADEMIC_TRACKS)} academic tracks resolve correctly")

    try:
        rules_text, _ = read_utf8(IR_SOCIAL_SCIENCE_RULES)
        rules_value = parse_json(rules_text)
        rule_errors = validate_check_library(rules_value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        errors.append(f"Argument IR check library cannot be loaded: {exc}")
    else:
        if rule_errors:
            errors.extend(
                f"Argument IR check library: {error}" for error in rule_errors
            )
        else:
            checks.append(
                f"Argument IR check library is valid ({len(rules_value['checks'])} checks)"
            )

    directory = Path(args.directory).resolve()
    if not directory.is_dir():
        errors.append(f"working directory does not exist: {directory}")
    else:
        try:
            with tempfile.NamedTemporaryFile(
                dir=directory,
                prefix=".critic-doctor-",
                delete=True,
            ) as handle:
                handle.write(b"ok")
                handle.flush()
        except OSError as exc:
            errors.append(f"working directory is not writable: {exc}")
        else:
            checks.append(f"working directory is writable: {directory}")

    # Document Review Studio keeps parser and OCR dependencies replaceable.
    # Missing optional components are visible here, but do not make the legacy
    # Markdown/TXT runner unusable.
    try:
        if getattr(args, "repair", False):
            repair_dependencies()
        for dependency in doctor_dependencies():
            if dependency["available"]:
                checks.append(f"Document Review Studio {dependency['name']} available")
            else:
                warnings.append(
                    f"Document Review Studio {dependency['name']} unavailable: "
                    f"{dependency.get('detail') or dependency.get('install', 'install the adapter')}"
                )
    except (OSError, ValueError) as exc:
        warnings.append(f"Document Review Studio dependency check failed: {exc}")

    for check in checks:
        print(f"[ok] {check}")
    for warning in warnings:
        print(f"[warning] {warning}")
    if errors:
        for error in errors:
            print(f"[error] {error}", file=sys.stderr)
        return 2
    print("ready")
    return 0


def prepare_track(args: argparse.Namespace) -> int:
    args.protocol = ACADEMIC_TRACKS[args.track]["primary"]
    return prepare(args)

__all__ = [name for name in globals() if not name.startswith("__")]
