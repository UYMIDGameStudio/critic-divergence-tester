"""Project maintenance and complete Studio action access for automation."""
import json
from pathlib import Path


def add_maintenance_parser(sub):
    parser = sub.add_parser("studio-manage", help="备份、恢复、验证、升级检查和完整工作流操作")
    commands = parser.add_subparsers(dest="maintenance_action", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("project", type=Path)
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("library", type=Path)
    for name in ("verify", "upgrade-check"):
        command = commands.add_parser(name)
        command.add_argument("project", type=Path)
    action = commands.add_parser("action", help="使用 JSON 请求文件执行 UI 同等操作；含完整 Action/Hunk/复查链")
    action.add_argument("project", type=Path)
    action.add_argument("request", type=Path)
    for command in (backup, restore, action, *[commands.choices[n] for n in ("verify", "upgrade-check")]):
        command.set_defaults(func=maintenance_command)


def maintenance_command(args):
    from project_lifecycle import create_backup, restore_backup, compatibility
    from document_review_studio import DocumentReviewProject
    if args.maintenance_action == "backup":
        result = {"backup": str(create_backup(args.project, args.destination))}
    elif args.maintenance_action == "restore":
        result = {"project": str(restore_backup(args.archive, args.library))}
    elif args.maintenance_action == "action":
        from document_review_ui import StudioApp, _strict_json_payload
        payload = _strict_json_payload(args.request.read_bytes())
        if not isinstance(payload, dict):
            raise ValueError("请求文件必须是 JSON 对象")
        app = StudioApp.create(args.project.resolve().parent, args.project).act(payload)
        result = {"notice": app.notice, "project": str(app.project.root) if app.project else None}
    else:
        project = DocumentReviewProject(args.project)
        errors = project.integrity_errors()
        result = {**compatibility(project.root), "valid": not errors, "errors": errors,
                  "upgrade_policy": "0.2 retains schema 1; no rewrite. Back up before replacing the application; restore to a new project for rollback."}
        if errors:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
