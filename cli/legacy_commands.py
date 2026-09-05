"""Legacy Commands extracted from the public CLI facade."""

from __future__ import annotations

from .support import *  # noqa: F401,F403
from .scoring_commands import *  # noqa: F401,F403

def import_report_command(args: argparse.Namespace) -> int:
    raw_run_dir = Path(args.run_dir)
    raw_report_path = Path(args.report)
    run_dir = raw_run_dir.resolve()
    report_path = raw_report_path.resolve()
    manifest_path = run_dir / "manifest.json"
    archived_report_path = run_dir / "report.md"
    adjudication_path = (
        Path(args.adjudication_output).resolve()
        if getattr(args, "adjudication_output", None)
        else run_dir / "adjudication.json"
    )
    collection_method = getattr(args, "collection_method", "manual-import")
    collection_source_name = getattr(args, "collection_source_name", report_path.name)

    if collection_method not in COLLECTION_METHODS:
        print("import report error: unknown collection method", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    if collection_method == "terminal-paste":
        if collection_source_name != "pasted-report.md":
            print(
                "import report error: terminal paste must use pasted-report.md",
                file=sys.stderr,
            )
            return EXIT_INVALID_WORKFLOW
    elif collection_source_name != report_path.name:
        print(
            "import report error: imported source name must match the report file",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW

    if raw_run_dir.is_symlink() or raw_report_path.is_symlink():
        print(
            "import report error: run and report paths must not be symbolic links",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW
    if not report_path.is_file():
        print(
            f"import report error: report file does not exist: {report_path}",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW
    if report_path == run_dir or run_dir in report_path.parents:
        print(
            "import report error: source report must be outside the prepared run",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW
    if archived_report_path.exists() or archived_report_path.is_symlink():
        print(
            "import report error: archived report already exists; refusing to overwrite",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW
    if adjudication_path.parent != run_dir:
        print(
            "import report error: adjudication must stay inside the run directory",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW
    if adjudication_path.exists() or adjudication_path.is_symlink():
        print("import report error: adjudication output already exists", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW

    verification = verify_run_dir(run_dir)
    if not verification.valid:
        for error in verification.errors:
            print(f"import report archive error: {error}", file=sys.stderr)
        return EXIT_INVALID_ARCHIVE

    try:
        old_manifest_bytes = manifest_path.read_bytes()
        manifest_value = parse_json(old_manifest_bytes.decode("utf-8"))
        report_bytes = report_path.read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        print(f"import report error: {exc}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    if not isinstance(manifest_value, dict):
        print("import report error: manifest must be an object", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    manifest: dict[str, object] = manifest_value
    if manifest.get("status") != "prepared":
        print("import report error: only a prepared run can collect a report", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    if len(report_bytes) > DEFAULT_MAX_OUTPUT_BYTES:
        print(
            f"import report error: report exceeds {DEFAULT_MAX_OUTPUT_BYTES} bytes",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW

    protocol = manifest.get("protocol")
    assert isinstance(protocol, str)
    try:
        report_text = report_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        print(f"import report error: report is not UTF-8: {exc}", file=sys.stderr)
        return EXIT_INVALID_REPORT
    validation = validate_report(protocol, report_text)
    if not validation.valid:
        _print_validation_errors(validation)
        print("report was not imported; fix it and retry", file=sys.stderr)
        return EXIT_INVALID_REPORT

    imported_at = utc_now()
    manifest.update(
        {
            "schema_version": 3,
            "report_sha256": sha256_bytes(report_bytes),
            "completed_at": imported_at,
            "status": "collected",
            "executor": None,
            "timeout_seconds": None,
            "max_output_bytes": None,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "executor_returncode": None,
            "runner_exit_code": 0,
            "report_validation": validation.as_dict(),
            "collection": {
                "method": collection_method,
                "imported_at": imported_at,
                "source_name": collection_source_name,
            },
        }
    )
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    adjudication_bytes: bytes | None = None
    if protocol in CRITIC_PROTOCOLS:
        try:
            findings = extract_critic_findings(report_text)
            report_status, unverified = critic_report_context(report_text)
        except ValueError as exc:
            print(f"import report error: {exc}", file=sys.stderr)
            return EXIT_INVALID_WORKFLOW
        adjudication = adjudication_template(
            protocol=protocol,
            report_sha256=sha256_bytes(report_bytes),
            manifest_sha256=sha256_bytes(manifest_bytes),
            findings=findings,
            report_status=report_status,
            unverified=unverified,
        )
        adjudication_bytes = (
            json.dumps(adjudication, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

    created_paths: list[Path] = []
    try:
        atomic_write_bytes(archived_report_path, report_bytes)
        created_paths.append(archived_report_path)
        if adjudication_bytes is not None:
            atomic_write_bytes(adjudication_path, adjudication_bytes)
            created_paths.append(adjudication_path)
        atomic_write_bytes(manifest_path, manifest_bytes)
    except OSError as exc:
        for created_path in created_paths:
            created_path.unlink(missing_ok=True)
        try:
            atomic_write_bytes(manifest_path, old_manifest_bytes)
        except OSError:
            pass
        print(f"import report error: {exc}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW

    post_verification = verify_run_dir(run_dir)
    if not post_verification.valid:
        for created_path in created_paths:
            created_path.unlink(missing_ok=True)
        atomic_write_bytes(manifest_path, old_manifest_bytes)
        for error in post_verification.errors:
            print(f"import report postcondition error: {error}", file=sys.stderr)
        return EXIT_INVALID_ARCHIVE

    print(archived_report_path)
    if adjudication_bytes is not None:
        print(adjudication_path)
    return 0


def _python_launcher() -> str:
    return "py -3" if os.name == "nt" else "python3"


def _available_previous_plan_path(run_dir: Path, plan_bytes: bytes) -> Path:
    digest = sha256_bytes(plan_bytes)[:12]
    base = run_dir / f"revision-plan.previous-{digest}.md"
    if not base.exists() and not base.is_symlink():
        return base
    for suffix in range(2, 10_000):
        candidate = run_dir / f"revision-plan.previous-{digest}-{suffix}.md"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise OSError("too many archived revision plans")


def _archive_current_plan(run_dir: Path) -> Path | None:
    plan_path = run_dir / "revision-plan.md"
    if not plan_path.exists():
        return None
    plan_bytes = plan_path.read_bytes()
    archived_path = _available_previous_plan_path(run_dir, plan_bytes)
    os.replace(plan_path, archived_path)
    if os.name == "posix":
        os.chmod(archived_path, 0o600)
    return archived_path


def _prompt_decision(*, allow_keep: bool) -> tuple[str, str, str] | None:
    choices = {"1": "accept", "2": "reject", "3": "defer"}
    while True:
        prompt = "选择 1 接受 / 2 拒绝 / 3 暂缓"
        if allow_keep:
            prompt += " / 直接回车保留当前裁决"
        choice = input(prompt + "：").strip()
        if allow_keep and not choice:
            return None
        decision = choices.get(choice)
        if decision is not None:
            break
        print("无法识别，请输入 1、2 或 3。")

    author_reason = ""
    revision_action = ""
    if decision == "accept":
        author_reason = input("采纳理由（可直接回车）：").strip()
        while not revision_action:
            revision_action = input("具体修改动作（必填）：").strip()
            if not revision_action:
                print("接受批评时必须写清具体修改动作。")
    elif decision == "reject":
        while not author_reason:
            author_reason = input("拒绝理由（必填）：").strip()
            if not author_reason:
                print("拒绝批评时必须留下理由。")
    else:
        while not author_reason:
            author_reason = input("暂缓理由（必填）：").strip()
            if not author_reason:
                print("暂缓时必须说明缺少什么证据或判断。")
        revision_action = input("后续动作（可直接回车）：").strip()
    return decision, author_reason, revision_action


def adjudicate_command(args: argparse.Namespace) -> int:
    raw_run_dir = Path(args.run_dir)
    run_dir = raw_run_dir.resolve()
    adjudication_path = run_dir / "adjudication.json"
    if raw_run_dir.is_symlink() or adjudication_path.is_symlink():
        print("adjudication error: paths must not be symbolic links", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    verification = verify_run_dir(run_dir)
    if not verification.valid:
        for error in verification.errors:
            print(f"adjudication archive error: {error}", file=sys.stderr)
        return EXIT_INVALID_ARCHIVE
    try:
        manifest_bytes = (run_dir / "manifest.json").read_bytes()
        manifest_value = parse_json(manifest_bytes.decode("utf-8"))
        report_bytes = (run_dir / "report.md").read_bytes()
        adjudication_value = parse_json(
            adjudication_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        print(f"adjudication error: {exc}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    errors = _adjudication_binding_errors(
        manifest_value,
        manifest_bytes,
        report_bytes,
        adjudication_value,
        require_complete=False,
    )
    if errors or not isinstance(adjudication_value, dict):
        for error in errors:
            print(f"adjudication error: {error}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    findings = adjudication_value["findings"]
    assert isinstance(findings, list)
    review_all = bool(getattr(args, "review_all", False))
    print("人工裁决：AI 的批评不是结论，逐条决定是否采纳。")
    if review_all:
        print("复议模式：直接回车保留原裁决；发生修改前会自动留存旧计划。")
    else:
        print("已完成的条目会跳过；每完成一条都会立即保存。")
    archived_plan: Path | None = None
    changed = False
    for finding in findings:
        assert isinstance(finding, dict)
        completed = finding.get("decision") in {"accept", "reject", "defer"}
        if completed and not review_all:
            continue
        print(f"\n{finding['id']}  {finding['claim']}")
        print(f"位置：{finding['position']}")
        print(f"理由：{finding['reason']}")
        print(f"后果检验：{finding['test']}")
        if completed and review_all:
            labels = {"accept": "接受", "reject": "拒绝", "defer": "暂缓"}
            print(
                f"当前裁决：{labels[str(finding['decision'])]}；"
                f"理由：{finding['author_reason'] or '（无）'}；"
                f"动作：{finding['revision_action'] or '（无）'}"
            )
        try:
            decision_fields = _prompt_decision(allow_keep=completed and review_all)
        except EOFError:
            print("\n错误：裁决输入不完整。", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("\n已保存此前进度并退出。", file=sys.stderr)
            return EXIT_INTERRUPTED
        if decision_fields is None:
            print(f"已保留 {finding['id']}。")
            continue
        decision, author_reason, revision_action = decision_fields
        previous = (
            finding.get("decision"),
            finding.get("author_reason"),
            finding.get("revision_action"),
        )
        current = (decision, author_reason, revision_action)
        if previous == current:
            print(f"{finding['id']} 没有变化。")
            continue
        if not changed:
            try:
                archived_plan = _archive_current_plan(run_dir)
            except OSError as exc:
                print(
                    f"adjudication error: cannot archive current plan: {exc}",
                    file=sys.stderr,
                )
                return EXIT_INVALID_WORKFLOW
        finding["decision"] = decision
        finding["author_reason"] = author_reason
        finding["revision_action"] = revision_action
        try:
            atomic_write_text(
                adjudication_path,
                json.dumps(adjudication_value, ensure_ascii=False, indent=2) + "\n",
            )
        except OSError as exc:
            print(f"adjudication error: cannot save decision: {exc}", file=sys.stderr)
            return EXIT_INVALID_WORKFLOW
        changed = True
        print(f"已保存 {finding['id']}。")

    if archived_plan is not None:
        print(f"旧修改计划已留存：{archived_plan}")
    print("\n裁决完成，正在生成修改计划……")
    return revision_plan_command(
        argparse.Namespace(run_dir=str(run_dir), adjudication=None, output=None)
    )


def revision_plan_command(args: argparse.Namespace) -> int:
    raw_run_dir = Path(args.run_dir)
    run_dir = raw_run_dir.resolve()
    raw_adjudication_path = (
        Path(args.adjudication)
        if getattr(args, "adjudication", None)
        else run_dir / "adjudication.json"
    )
    adjudication_path = raw_adjudication_path.resolve()
    raw_output_path = (
        Path(args.output)
        if getattr(args, "output", None)
        else run_dir / "revision-plan.md"
    )
    output_path = raw_output_path.resolve()
    if any(path.is_symlink() for path in (raw_run_dir, raw_adjudication_path, raw_output_path)):
        print("revision plan error: paths must not be symbolic links", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    if adjudication_path.parent != run_dir or output_path.parent != run_dir:
        print("revision plan error: artifacts must stay inside the run directory", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    verification = verify_run_dir(run_dir)
    if not verification.valid:
        for error in verification.errors:
            print(f"revision plan archive error: {error}", file=sys.stderr)
        return EXIT_INVALID_ARCHIVE
    try:
        manifest_bytes = (run_dir / "manifest.json").read_bytes()
        manifest_value = parse_json(manifest_bytes.decode("utf-8"))
        report_bytes = (run_dir / "report.md").read_bytes()
        adjudication_bytes = adjudication_path.read_bytes()
        adjudication_value = parse_json(adjudication_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        print(f"revision plan error: {exc}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    errors = _adjudication_binding_errors(
        manifest_value,
        manifest_bytes,
        report_bytes,
        adjudication_value,
        require_complete=True,
    )
    if errors:
        for error in errors:
            print(f"revision plan error: {error}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    adjudication_sha256 = sha256_bytes(adjudication_bytes)
    try:
        markdown = revision_plan_markdown(
            adjudication_value,
            adjudication_sha256=adjudication_sha256,
        )
    except WorkflowError as exc:
        print(f"revision plan error: {exc}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    if output_path.exists():
        try:
            existing_plan = output_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"revision plan error: cannot read existing output: {exc}", file=sys.stderr)
            return EXIT_INVALID_WORKFLOW
        if existing_plan == markdown:
            print(output_path)
            return 0
        print(
            "revision plan error: output is stale or modified; "
            "move it aside or choose another --output path",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW
    try:
        atomic_write_text(output_path, markdown)
    except OSError as exc:
        print(f"revision plan error: {exc}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    print(output_path)
    return 0

__all__ = [name for name in globals() if not name.startswith("__")]
