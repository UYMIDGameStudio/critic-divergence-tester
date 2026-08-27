"""Loopback-only browser application for Document Review Studio."""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import threading
import traceback
import webbrowser
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from document_review_ingest import doctor_dependencies, repair_dependencies
from document_review_studio import DocumentReviewProject, ReviewStudioError
from document_review_ui_shell import SHELL_TEMPLATE


LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
MAX_REQUEST_BYTES = 42 * 1024 * 1024


def _strict_json_payload(data: bytes) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ReviewStudioError(f"JSON 包含重复字段：{key}")
            value[key] = item
        return value

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except UnicodeDecodeError as exc:
        raise ReviewStudioError(f"请求不是 UTF-8：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReviewStudioError(f"请求不是有效 JSON：{exc}") from exc


def default_studio_data_dir() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "DocumentReviewStudio" / "projects"
    if os.environ.get("XDG_DATA_HOME"):
        return Path(os.environ["XDG_DATA_HOME"]) / "document-review-studio" / "projects"
    return Path.home() / ".local" / "share" / "document-review-studio" / "projects"


@dataclass(frozen=True)
class StudioApp:
    data_dir: Path
    token: str
    project: DocumentReviewProject | None = None
    notice: str | None = None

    @classmethod
    def create(cls, data_dir: Path | str | None = None, project_dir: Path | str | None = None) -> "StudioApp":
        storage = Path(data_dir or default_studio_data_dir()).resolve()
        storage.mkdir(parents=True, exist_ok=True)
        selected = DocumentReviewProject(Path(project_dir)) if project_dir else None
        return cls(storage, secrets.token_urlsafe(32), selected)

    def projects(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(self.data_dir.glob("*.document-review-studio")):
            if path.is_symlink() or not path.is_dir():
                continue
            try:
                project = DocumentReviewProject(path)
                manifest = project.manifest()
                rows.append({"directory": path.name, "title": manifest.get("title", path.name), "source_name": manifest.get("source", {}).get("name", ""), "state": project.state()})
            except (OSError, ValueError, KeyError, ReviewStudioError):
                rows.append({"directory": path.name, "title": path.name, "invalid": True})
        return rows

    def view(self) -> dict[str, Any]:
        selected = self.project.view() if self.project else None
        if selected is not None:
            selected["directory"] = self.project.root.name
        return {"storage_path": str(self.data_dir), "projects": self.projects(), "selected": selected, "dependencies": doctor_dependencies(), "notice": self.notice}

    def _project_directory(self, directory: str, *, must_exist: bool) -> Path:
        if not isinstance(directory, str) or Path(directory).name != directory or not directory.endswith(".document-review-studio"):
            raise ReviewStudioError("项目目录名无效")
        candidate = self.data_dir / directory
        if candidate.is_symlink():
            raise ReviewStudioError("拒绝访问符号链接项目")
        target = candidate.resolve()
        if target.parent != self.data_dir or target == self.data_dir:
            raise ReviewStudioError("项目不在本地文档审查项目库中")
        if must_exist and not target.is_dir():
            raise ReviewStudioError("本地项目不存在")
        return target

    def open_project(self, directory: str) -> "StudioApp":
        return replace(self, project=DocumentReviewProject(self._project_directory(directory, must_exist=True)), notice=None)

    def delete_project(self, directory: str) -> "StudioApp":
        target = self._project_directory(directory, must_exist=True)
        shutil.rmtree(target)
        selected = None if self.project and self.project.root == target else self.project
        return replace(self, project=selected, notice="本地项目已删除；此操作无法恢复。")

    def repair_environment(self, names: list[str] | None = None) -> "StudioApp":
        before = {row["name"]: bool(row["available"]) for row in doctor_dependencies()}
        refreshed = repair_dependencies(names)
        newly_available = [row["name"] for row in refreshed if row["available"] and not before.get(row["name"], False)]
        retried = False
        if self.project is not None:
            state = self.project.state()
            if state.get("extraction_state") == "blocked" and not (self.project.root / "extraction-decisions").exists():
                try:
                    self.project.retry_extraction()
                    retried = True
                except ReviewStudioError:
                    pass
        detail = "、".join(newly_available) if newly_available else "所选适配器已经可用"
        if retried:
            detail += "；当前项目已重新识别"
        return replace(self, notice=f"环境修复完成：{detail}")

    def require_project(self) -> DocumentReviewProject:
        if self.project is None:
            raise ReviewStudioError("请先上传或打开文档")
        return self.project

    def open_export_folder(self, relative_path: str) -> "StudioApp":
        folder = self.require_project().export_file(relative_path).parent
        if os.name == "nt" and hasattr(os, "startfile"):
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif os.name == "darwin":
            subprocess.Popen(["open", str(folder)], close_fds=True)
        else:
            subprocess.Popen(["xdg-open", str(folder)], close_fds=True)
        return self

    def protocol_bundle(self) -> bytes:
        project = self.require_project()
        if project.integrity_errors():
            raise ReviewStudioError("项目完整性校验失败，拒绝导出 AI 协议")
        requests = project.ai_requests()
        if not requests:
            raise ReviewStudioError("请先导出独立 AI 审查协议")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for request in requests:
                safe_name = request["critic"].replace("/", "-").replace("\\", "-")
                archive.writestr(f"{safe_name}/prompt.md", request["prompt"])
                metadata = {key: value for key, value in request.items() if key not in {"prompt", "completed"}}
                archive.writestr(f"{safe_name}/request.json", json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        return output.getvalue()

    def upload(self, payload: dict[str, Any]) -> "StudioApp":
        filename, encoded = payload.get("filename"), payload.get("content_base64")
        if not isinstance(filename, str) or not isinstance(encoded, str):
            raise ReviewStudioError("上传请求缺少 filename 或 content_base64")
        try:
            content = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise ReviewStudioError("上传内容不是有效 Base64") from exc
        title = payload.get("title")
        if title is not None and not isinstance(title, str):
            raise ReviewStudioError("标题必须是文本")
        project = DocumentReviewProject.create(self.data_dir, filename=filename, content=content, title=title or None)
        return replace(self, project=project, notice="文档已导入，请确认识别结果。")

    def act(self, payload: dict[str, Any]) -> "StudioApp":
        action, data = payload.get("action"), payload.get("data", {})
        if not isinstance(action, str) or not isinstance(data, dict):
            raise ReviewStudioError("操作请求无效")
        if action == "delete_project":
            return self.delete_project(str(data.get("directory", "")))
        if action == "close_project":
            return replace(self, project=None, notice=None)
        if action == "repair_environment":
            names = data.get("names")
            if names is not None and (not isinstance(names, list) or not all(isinstance(name, str) for name in names)):
                raise ReviewStudioError("环境修复目标无效")
            return self.repair_environment(names)
        if action == "open_export_folder":
            return self.open_export_folder(str(data.get("relative_path", "")))
        project = self.require_project()
        notice = "操作已完成。"
        if action == "confirm_extraction":
            project.confirm_extraction(str(data.get("choice", "")), corrected_text=data.get("corrected_text"))
        elif action == "confirm_context":
            project.confirm_context(data)
        elif action == "retry_extraction":
            project.retry_extraction()
        elif action in {"run_audits", "run_local_prechecks"}:
            runs = project.run_local_prechecks(data.get("critics")) or []
            selected_critics = [run.critic for run in runs]
            active_critics = {
                str(request.get("critic"))
                for request in (project.ai_requests() or [])
                if request.get("critic")
            }
            missing_critics = [critic for critic in selected_critics if critic not in active_critics]
            generated = []
            if missing_critics:
                provider = str(data.get("provider", "")).strip() or "手动导入"
                model = str(data.get("model", "")).strip() or "未声明模型"
                generated = project.prepare_ai_audits(missing_critics, provider=provider, model=model)
            notice = f"本地确定性预检已完成；产生 {len(project.findings() or [])} 条当前 Finding"
            if generated:
                notice += f"，并生成 {len(generated)} 份独立 AI 审查任务"
            elif selected_critics:
                notice += "；对应 AI 审查任务已经存在"
            notice += "。AI 任务仅包含待执行协议，尚未调用模型。"
        elif action == "prepare_ai_audits":
            project.prepare_ai_audits(data.get("critics"), provider=str(data.get("provider", "")), model=str(data.get("model", "")))
        elif action == "import_ai_audit":
            run = project.collect_model_audit(str(data.get("critic", "")), str(data.get("response", "")), provider=str(data.get("provider", "")), model=str(data.get("model", "")), request_id=str(data.get("request_id", "")) or None, binding_mode=str(data.get("binding_mode", "strict")))
            run_path = project.root / "audits" / run.critic / f"{run.run_id}.json"
            imported = json.loads(run_path.read_text(encoding="utf-8"))
            normalization_count = len(imported.get("response_normalizations", []))
            binding = imported.get("response_binding", {})
            binding_text = "严格回显已验证" if binding.get("request_echo_verified") else "已人工关联到当前任务"
            notice = f"AI 审查已导入（{binding_text}）"
            if normalization_count:
                notice += f"；{normalization_count} 处格式已保守归一化，原始响应保持不变"
            notice += f"。原始响应与解析结果保存在：{run_path.parent}"
        elif action == "export_ai_reviews":
            output = project.export_ai_reviews()
            notice = f"当前 AI 审查已导出：{output}"
        elif action == "decide_finding":
            project.decide_finding(str(data.get("finding_id", "")), str(data.get("decision", "")), reason=str(data.get("reason", "")), corrected_action=data.get("corrected_action"))
        elif action == "prepare_bridge":
            project.prepare_revision_plan()
        elif action == "propose_revision_hunk":
            project.propose_revision_hunk(
                str(data.get("action_id", "")),
                str(data.get("revised_text", "")),
                rationale=str(data.get("rationale", "")),
                provenance=str(data.get("provenance", "human-authored")),
            )
        elif action == "set_revision_action_operation":
            project.set_revision_action_operation(
                str(data.get("action_id", "")),
                str(data.get("operation", "")),
                reason=str(data.get("reason", "")),
            )
        elif action == "decide_revision_hunk":
            project.decide_revision_hunk(
                str(data.get("hunk_id", "")),
                str(data.get("decision", "")),
                reason=str(data.get("reason", "")),
            )
        elif action == "finalize_revision":
            project.finalize_revision()
        elif action == "import_external_recheck":
            project.collect_external_recheck(
                str(data.get("revision_id", "")),
                str(data.get("critic", "")),
                str(data.get("response", "")),
                provider=str(data.get("provider", "")),
                model=str(data.get("model", "")),
                binding_mode=str(data.get("binding_mode", "strict")),
            )
        elif action == "decide_external_resolution":
            project.decide_external_resolution(
                str(data.get("revision_id", "")),
                str(data.get("result_id", "")),
                str(data.get("finding_id", "")),
                str(data.get("state", "")),
                reason=str(data.get("reason", "")),
            )
        elif action == "start_followup_round":
            project.start_followup_round(str(data.get("revision_id", "")))
        elif action == "export":
            project.export(revised_markdown=data.get("revised_markdown"))
        else:
            raise ReviewStudioError("未知操作")
        return replace(self, notice=notice)


class StudioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: StudioApp):
        self.app = app
        self.action_lock = threading.RLock()
        self.error_log_lock = threading.Lock()
        super().__init__(address, StudioRequestHandler)


class StudioRequestHandler(BaseHTTPRequestHandler):
    server: StudioHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: HTTPStatus, data: bytes, content_type: str, *, filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename.replace(chr(34), "")}"')
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: HTTPStatus, value: Any) -> None:
        self._send(status, (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"), "application/json; charset=utf-8")

    def _auth(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Document-Review-Token", ""), self.server.app.token)

    def _record_unexpected_error(self, exc: Exception, *, request_kind: str, action: str | None = None) -> str:
        """Persist a correlation-safe traceback without recording request content."""
        incident_id = f"ERR-{secrets.token_hex(6)}"
        rendered_traceback = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        record = {
            "incident_id": incident_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "request_kind": request_kind,
            "request_path": urlsplit(self.path).path,
            "action": action,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc)[:1000],
            "traceback": rendered_traceback[-20000:],
        }
        try:
            log_path = self.server.app.data_dir / ".studio-errors.jsonl"
            with self.server.error_log_lock:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        return incident_id

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/":
            self._send(HTTPStatus.OK, render_studio_shell(self.server.app.token).encode("utf-8"), "text/html; charset=utf-8")
            return
        if path not in {"/api/state", "/api/download", "/api/protocols.zip"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._auth():
            self._json(HTTPStatus.FORBIDDEN, {"error": "local UI token required"})
            return
        try:
            with self.server.action_lock:
                if path == "/api/state":
                    payload = ("json", self.server.app.view())
                elif path == "/api/protocols.zip":
                    payload = ("zip", self.server.app.protocol_bundle())
                else:
                    relative_path = parse_qs(parsed.query).get("path", [""])[0]
                    target = self.server.app.require_project().export_file(relative_path)
                    payload = ("file", target, target.read_bytes())
            if payload[0] == "json":
                self._json(HTTPStatus.OK, payload[1])
            elif payload[0] == "zip":
                self._send(HTTPStatus.OK, payload[1], "application/zip", filename="ai-review-protocols.zip")
            else:
                target, content = payload[1], payload[2]
                self._send(HTTPStatus.OK, content, mimetypes.guess_type(target.name)[0] or "application/octet-stream", filename=target.name)
        except (OSError, ValueError, ReviewStudioError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            incident_id = self._record_unexpected_error(exc, request_kind="GET")
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": (
                        "本地服务读取项目状态时发生未预期错误"
                        f"（{type(exc).__name__}，故障编号 {incident_id}）。"
                        "请重新打开本次启动的 Studio 页面；详细调用栈已保存到项目库的 .studio-errors.jsonl。"
                    )
                },
            )

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        request_action: str | None = None
        if path not in {"/api/upload", "/api/open", "/api/action"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._auth():
            self._json(HTTPStatus.FORBIDDEN, {"error": "local UI token required"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ReviewStudioError("请求大小无效")
            payload = _strict_json_payload(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ReviewStudioError("请求必须是对象")
            request_action = payload.get("action") if isinstance(payload.get("action"), str) else None
            with self.server.action_lock:
                if path == "/api/upload":
                    self.server.app = self.server.app.upload(payload)
                elif path == "/api/open":
                    self.server.app = self.server.app.open_project(str(payload.get("directory", "")))
                else:
                    self.server.app = self.server.app.act(payload)
                response = self.server.app.view()
            self._json(HTTPStatus.CREATED, response)
        except (UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError, ReviewStudioError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            incident_id = self._record_unexpected_error(exc, request_kind="POST", action=request_action)
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": (
                        "本地服务处理请求时发生未预期错误"
                        f"（{type(exc).__name__}，故障编号 {incident_id}）。"
                        "请先刷新或重新打开本次启动的 Studio 页面；操作可能已经完成，"
                        "重新提交前请检查项目状态。详细调用栈已保存到项目库的 .studio-errors.jsonl。"
                    )
                },
            )


def render_studio_shell(token: str) -> str:
    return SHELL_TEMPLATE.replace("__TOKEN__", json.dumps(token))


def serve_document_review_studio(*, data_dir: Path | str | None = None, project_dir: Path | str | None = None, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> tuple[StudioHTTPServer, str]:
    if host.casefold() not in LOOPBACK_HOSTS:
        raise ReviewStudioError("Document Review Studio 只能监听本机 loopback 地址")
    if not 0 <= port <= 65535:
        raise ReviewStudioError("端口必须在 0 到 65535 之间")
    server = StudioHTTPServer((host, port), StudioApp.create(data_dir, project_dir))
    address = server.server_address
    url = f"http://{address[0]}:{address[1]}/"
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    return server, url


__all__ = ["StudioApp", "StudioHTTPServer", "default_studio_data_dir", "render_studio_shell", "serve_document_review_studio"]
