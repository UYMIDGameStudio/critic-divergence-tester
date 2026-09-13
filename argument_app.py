"""Single-entry local application shell for ordinary Argument Workbench users.

The shell owns navigation and upload ergonomics only.  Immutable manuscript
storage remains in :mod:`argument_workbench`; later workflow mutations are
delegated to their domain services rather than implemented in HTTP handlers.
"""

from __future__ import annotations

import json
import base64
import os
import re
import secrets
import threading
import webbrowser
from contextlib import nullcontext
from dataclasses import dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from argument_contracts import sha256_bytes
from argument_revision import (
    append_hunk_decision,
    append_quick_finding_decision,
    append_resolution_decision,
    apply_approved_hunks,
    collect_atomization_result,
    collect_resolution_result,
    collect_revision_result,
    complete_without_revision,
    export_revision,
    import_review_report,
    prepare_atomization,
    prepare_resolution_review,
    prepare_revision_generation,
    verify_revision_workflow,
    workflow_view,
)
from argument_workbench import (
    WorkbenchError,
    _atomic_write,
    _read_json,
    initialize_workspace,
    list_version_ids,
    parse_json_strict,
    project_mutation_lock,
    verify_project_versions,
    workspace_paths,
)
from argument_ui import (
    LocalHTTPProtocolError, LocalHTTPServer, LocalRequestHandler,
    adjudicate_from_ui, build_project_view, render_app_shell,
    request_context, require_request_context,
)


LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
MAX_REQUEST_BYTES = 8 * 1024 * 1024
SOURCE_EXTENSIONS = {".md", ".txt"}


def default_data_dir() -> Path:
    """Return the documented per-user storage root without creating it."""
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "ArgumentWorkbench" / "projects"
    if os.environ.get("XDG_DATA_HOME"):
        return Path(os.environ["XDG_DATA_HOME"]) / "argument-workbench" / "projects"
    return Path.home() / ".local" / "share" / "argument-workbench" / "projects"


def _safe_upload_name(name: object) -> str:
    if not isinstance(name, str):
        raise WorkbenchError("稿件文件名无效")
    candidate = Path(name).name
    if (
        candidate != name
        or not candidate
        or Path(candidate).suffix.casefold() not in SOURCE_EXTENSIONS
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
    ):
        raise WorkbenchError("只接受安全文件名的 Markdown 或 TXT 稿件")
    return candidate


def _project_slug(filename: str, data: bytes) -> str:
    stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff-]+", "-", Path(filename).stem).strip("-")
    stem = stem[:48] or "manuscript"
    return f"{stem}-{sha256_bytes(data)[:10]}.argument-workbench"


def _source_details(project_dir: Path) -> dict[str, Any]:
    versions = list_version_ids(project_dir)
    if not versions:
        raise WorkbenchError("项目没有可用版本")
    workspace = workspace_paths(project_dir, versions[-1])
    version, _ = _read_json(workspace.version)
    project, _ = _read_json(workspace.project)
    return {
        "project_id": project["project_id"],
        "title": project["title"],
        "path": str(project_dir),
        "current_version": versions[-1],
        "versions": versions,
        "source_name": version["source"]["name"],
        "source_sha256": version["source"]["sha256"],
        "created_at": version["provenance"]["created_at"],
    }


def create_uploaded_project(
    data_dir: Path | str,
    *,
    filename: str,
    content: bytes,
    title: str | None = None,
    encoding: str | None = None,
) -> Path:
    """Create an immutable V1 from browser-uploaded bytes."""
    safe_name = _safe_upload_name(filename)
    if not content or len(content) > MAX_REQUEST_BYTES:
        raise WorkbenchError("稿件必须非空且不超过 8 MiB")
    from document_text_encoding import decode_document_text
    decoded = decode_document_text(content, encoding)
    if decoded.ambiguous:
        raise WorkbenchError("稿件编码有多个可能解释，请显式选择编码：" + ", ".join(decoded.candidates))
    text = decoded.text
    if not text.strip():
        raise WorkbenchError("稿件不能为空")
    root = Path(data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    slug = _project_slug(safe_name, content)
    # A legacy byte stream can have more than one valid interpretation. Preserve
    # the established UTF-8 project name while separating other decoded texts.
    try:
        original_text = content.decode("utf-8-sig")
    except UnicodeError:
        original_text = None
    if decoded.text != original_text:
        stem = slug.removesuffix(".argument-workbench")
        slug = f"{stem}-{sha256_bytes(decoded.text.encode('utf-8'))[:10]}.argument-workbench"
    target = root / slug
    if target.is_symlink():
        raise WorkbenchError("项目路径不得是符号链接")
    if target.exists():
        errors = verify_project_versions(target)
        if errors:
            raise WorkbenchError("同名项目存在但校验失败：" + "; ".join(errors))
        return target
    staging = root / (target.name + ".import")
    if staging.exists() or staging.is_symlink():
        raise WorkbenchError("导入暂存路径已存在，请稍后重试")
    staging.mkdir(parents=False)
    source = staging / safe_name
    _atomic_write(source, content)
    try:
        initialize_workspace(source, target, title=title, encoding=decoded.encoding)
    finally:
        source.unlink(missing_ok=True)
        try:
            staging.rmdir()
        except OSError:
            pass
    return target


def project_state(project_dir: Path) -> dict[str, Any]:
    try:
        errors = verify_project_versions(project_dir)
    except (OSError, WorkbenchError, KeyError, TypeError, ValueError) as exc:
        errors = [f"project verification failed: {exc}"]
    if not errors:
        try:
            errors.extend(verify_revision_workflow(project_dir))
        except (OSError, WorkbenchError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"revision workflow verification failed: {exc}")
    if errors:
        return {"stage": "read_only", "next_action": "项目校验失败，只读打开", "errors": errors}
    return workflow_view(project_dir)


@dataclass(frozen=True)
class ProductApp:
    data_dir: Path
    token: str
    project_dir: Path | None = None

    @classmethod
    def create(
        cls, data_dir: Path | str | None = None, project_dir: Path | str | None = None
    ) -> "ProductApp":
        storage = Path(data_dir or default_data_dir()).resolve()
        storage.mkdir(parents=True, exist_ok=True)
        project_candidate = None if project_dir is None else Path(project_dir)
        if project_candidate is not None and project_candidate.is_symlink():
            raise WorkbenchError("项目目录不得是符号链接")
        selected = None if project_candidate is None else workspace_paths(project_candidate).root
        if selected is not None:
            errors = verify_project_versions(selected)
            if errors:
                raise WorkbenchError("项目校验失败：" + "; ".join(errors))
        return cls(storage, secrets.token_urlsafe(32), selected)

    def projects(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for candidate in sorted(self.data_dir.glob("*.argument-workbench")):
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                errors = verify_project_versions(candidate)
                if errors:
                    raise WorkbenchError("; ".join(errors))
                rows.append(_source_details(candidate))
            except (OSError, WorkbenchError, IndexError, KeyError, TypeError, ValueError):
                rows.append({"title": candidate.name, "path": str(candidate), "invalid": True})
        return rows

    def view(self) -> dict[str, Any]:
        selected = None
        if self.project_dir is not None:
            state = project_state(self.project_dir)
            try:
                details = _source_details(self.project_dir)
            except (OSError, WorkbenchError, IndexError, KeyError, TypeError, ValueError):
                details = {
                    "title": self.project_dir.name,
                    "path": str(self.project_dir),
                    "invalid": True,
                }
            selected = {**details, **state}
            selected["professional_available"] = False
            if state["stage"] != "read_only":
                current = workspace_paths(self.project_dir)
                selected["professional_available"] = current.reviewed_payload.is_file() and not current.reviewed_payload.is_symlink()
        return {
            "storage_path": str(self.data_dir),
            "projects": self.projects(),
            "selected": selected,
            "request_context": request_context(self.token, self.project_dir or self.data_dir, {"selected": selected}),
        }

    def import_manuscript(self, payload: dict[str, Any]) -> "ProductApp":
        if (not {"filename", "title"}.issubset(payload) or set(payload) - {"filename", "title", "content", "content_base64", "encoding"}
                or ("content" in payload) == ("content_base64" in payload)):
            raise WorkbenchError("导入请求字段不完整")
        content = payload.get("content_base64", payload.get("content"))
        if not isinstance(content, str):
            raise WorkbenchError("稿件内容必须是文本")
        try:
            raw = base64.b64decode(content, validate=True) if "content_base64" in payload else content.encode("utf-8")
        except (UnicodeError, ValueError) as exc:
            raise WorkbenchError("稿件字节编码无效") from exc
        title = payload.get("title")
        if title is not None and not isinstance(title, str):
            raise WorkbenchError("标题必须是文本")
        target = create_uploaded_project(
            self.data_dir,
            filename=payload.get("filename", ""),
            content=raw,
            title=title or None,
            encoding=payload.get("encoding"),
        )
        return replace(self, project_dir=target)

    def open_project(self, payload: dict[str, Any]) -> "ProductApp":
        if set(payload) != {"directory"} or not isinstance(payload.get("directory"), str):
            raise WorkbenchError("项目选择请求无效")
        name = str(payload["directory"])
        if Path(name).name != name or not name.endswith(".argument-workbench"):
            raise WorkbenchError("项目目录名无效")
        candidate = self.data_dir / name
        if candidate.is_symlink():
            raise WorkbenchError("项目不在本地项目库中")
        target = candidate.resolve()
        if target.parent != self.data_dir or not target.is_dir():
            raise WorkbenchError("项目不在本地项目库中")
        errors = verify_project_versions(target)
        if errors:
            raise WorkbenchError("项目校验失败：" + "; ".join(errors))
        return replace(self, project_dir=target)

    def act(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.project_dir is None:
            raise WorkbenchError("请先创建或打开项目")
        # Serialize the state check and dispatch for direct API callers too.
        # Domain services own their transactions: a saved report remains a
        # complete artifact if the following prompt preparation must be retried.
        with project_mutation_lock(self.project_dir):
            return self._act_locked(payload)

    def _act_locked(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = project_state(self.project_dir)
        if state["stage"] == "read_only":
            raise WorkbenchError("项目修改链校验失败，当前只能只读打开：" + "; ".join(state["errors"]))
        action = payload.get("action")
        data = payload.get("data")
        if not isinstance(action, str) or not isinstance(data, dict) or set(payload) != {"action", "data"}:
            raise WorkbenchError("操作请求无效")
        text_fields = {
            "import_report": {"report"}, "collect_atomization": {"response"},
            "collect_revision": {"response"}, "collect_resolution": {"response"},
            "decide_finding": {"finding_id", "decision", "reason"},
            "decide_hunk": {"change_id", "decision", "reason"},
            "decide_resolution": {"finding_id", "status", "reason"},
            "complete_without_revision": {"reason"},
            "prepare_atomization": set(), "prepare_revision": set(),
            "apply_revision": set(), "prepare_resolution": set(), "export": set(),
        }
        optional_fields = {"import_report": {"source_name"},
                           "decide_finding": {"corrections", "action_text"},
                           "decide_hunk": {"edited_text"}}
        required = text_fields.get(action)
        if (required is None or not required.issubset(data)
                or set(data) - required - optional_fields.get(action, set())
                or any(not isinstance(data[field], str) for field in required)):
            raise WorkbenchError("操作字段或文本类型无效")
        for field in optional_fields.get(action, set()):
            if field in data and not (data[field] is None and field != "source_name"):
                expected = dict if field == "corrections" else str
                if not isinstance(data[field], expected):
                    raise WorkbenchError("操作字段或文本类型无效")
        if action == "import_report":
            report = data.get("report")
            source_name = data.get("source_name", "pasted-report.md")
            if not isinstance(report, str) or not isinstance(source_name, str):
                raise WorkbenchError("审查报告必须是文本")
            report_id = import_review_report(self.project_dir, report, source_name=source_name)
            prepare_atomization(self.project_dir, report_id)
        elif action == "prepare_atomization":
            if state["stage"] != "atomization_prepare" or data:
                raise WorkbenchError("仅可为已保存且尚未生成提示的报告继续准备")
            prepare_atomization(self.project_dir)
        elif action == "collect_atomization":
            if not isinstance(data.get("response"), str): raise WorkbenchError("AI 返回必须是文本")
            collect_atomization_result(self.project_dir, data["response"])
        elif action == "decide_finding":
            append_quick_finding_decision(self.project_dir, data["finding_id"], decision=data["decision"], reason=data["reason"], corrections=data.get("corrections"), action_text=data.get("action_text"))
        elif action == "prepare_revision": prepare_revision_generation(self.project_dir)
        elif action == "collect_revision":
            if not isinstance(data.get("response"), str): raise WorkbenchError("AI 返回必须是文本")
            collect_revision_result(self.project_dir, data["response"])
        elif action == "decide_hunk":
            edited = data.get("edited_text")
            append_hunk_decision(self.project_dir, data["change_id"], decision=data["decision"], reason=data["reason"], edited_text=edited)
        elif action == "apply_revision": apply_approved_hunks(self.project_dir)
        elif action == "prepare_resolution": prepare_resolution_review(self.project_dir)
        elif action == "collect_resolution":
            if not isinstance(data.get("response"), str): raise WorkbenchError("AI 返回必须是文本")
            collect_resolution_result(self.project_dir, data["response"])
        elif action == "decide_resolution":
            append_resolution_decision(self.project_dir, data["finding_id"], status=data["status"], reason=data["reason"])
        elif action == "complete_without_revision":
            complete_without_revision(self.project_dir, reason=data["reason"])
        elif action == "export": export_revision(self.project_dir)
        else: raise WorkbenchError("未知操作")
        return self.view()

    def professional_view(self, version_id: str | None = None) -> dict[str, Any]:
        if self.project_dir is None:
            raise WorkbenchError("请先打开专业研究项目")
        value = build_project_view(self.project_dir, version_id)
        return {**value, "request_context": request_context(self.token, self.project_dir, value)}

    def professional_adjudicate(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.project_dir is None:
            raise WorkbenchError("请先打开专业研究项目")
        with project_mutation_lock(self.project_dir):
            state = project_state(self.project_dir)
            if state["stage"] == "read_only":
                raise WorkbenchError("项目修改链校验失败，当前只能只读打开：" + "; ".join(state["errors"]))
            adjudicate_from_ui(self.project_dir, payload)
            return self.professional_view()


class ProductHTTPServer(LocalHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: ProductApp):
        self.app = app
        self.action_lock = threading.RLock()
        super().__init__(address, ProductRequestHandler)


class ProductRequestHandler(LocalRequestHandler):
    server: ProductHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: HTTPStatus, value: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(value)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(value)

    def _json(self, status: HTTPStatus, value: Any) -> None:
        self._send(status, (json.dumps(value, ensure_ascii=False) + "\n").encode(), "application/json; charset=utf-8")

    def _authorized(self) -> bool:
        return self._token_authorized("X-Argument-Workbench-Token")

    def do_GET(self) -> None:  # noqa: N802
        if not self._local_request():
            self._json(HTTPStatus.FORBIDDEN, {"error": "只接受当前本机地址和同源页面"})
            return
        path = urlsplit(self.path).path
        if path == "/":
            self._send(HTTPStatus.OK, render_product_shell(self.server.app.token).encode(), "text/html; charset=utf-8")
            return
        if path == "/professional":
            try:
                with self.server.action_lock:
                    self.server.app.professional_view()
                shell = render_app_shell(self.server.app.token).replace("'/api/view", "'/api/professional/view").replace("'/api/adjudications", "'/api/professional/adjudications")
                self._send(HTTPStatus.OK, shell.encode(), "text/html; charset=utf-8")
            except WorkbenchError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "本地服务处理请求时发生未预期错误，请刷新页面并检查项目状态"})
            return
        if path == "/api/state" and self._authorized():
            try:
                with self.server.action_lock:
                    result = self.server.app.view()
                self._json(HTTPStatus.OK, result)
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "本地服务处理请求时发生未预期错误，请刷新页面并检查项目状态"})
            return
        if path == "/api/professional/view" and self._authorized():
            try:
                from urllib.parse import parse_qs
                version = parse_qs(urlsplit(self.path).query).get("version", [None])[0]
                with self.server.action_lock:
                    result = self.server.app.professional_view(version)
                self._json(HTTPStatus.OK, result)
            except WorkbenchError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "本地服务处理请求时发生未预期错误，请刷新页面并检查项目状态"})
            return
        self._json(HTTPStatus.FORBIDDEN if path == "/api/state" else HTTPStatus.NOT_FOUND, {"error": "local UI token required" if path == "/api/state" else "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._local_request():
            self._json(HTTPStatus.FORBIDDEN, {"error": "只接受当前本机地址和同源页面"})
            return
        path = urlsplit(self.path).path
        if path not in {"/api/projects", "/api/open", "/api/action", "/api/professional/adjudications"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            self._json(HTTPStatus.FORBIDDEN, {"error": "local UI token required"})
            return
        try:
            payload = parse_json_strict(self._read_json_body(MAX_REQUEST_BYTES))
            if not isinstance(payload, dict):
                raise WorkbenchError("请求必须是对象")
            # Choose the project lock only after navigation is excluded. Read
            # the submitted body before this lock so slow clients cannot stall
            # another user's local tab or the independent Studio interface.
            with self.server.action_lock:
                if path == "/api/projects":
                    lock_root = self.server.app.data_dir
                elif path in {"/api/action", "/api/professional/adjudications"}:
                    lock_root = self.server.app.project_dir
                else:
                    lock_root = None
                mutation_guard = project_mutation_lock(lock_root) if lock_root is not None else nullcontext()
                with mutation_guard:
                    if path == "/api/professional/adjudications":
                        require_request_context(self, self.server.app.professional_view()["request_context"])
                        result = self.server.app.professional_adjudicate(payload)
                    elif path == "/api/projects":
                        self.server.app = self.server.app.import_manuscript(payload)
                        result = self.server.app.view()
                    elif path == "/api/open":
                        self.server.app = self.server.app.open_project(payload)
                        result = self.server.app.view()
                    else:
                        require_request_context(self, self.server.app.view()["request_context"])
                        result = self.server.app.act(payload)
            self._json(HTTPStatus.CREATED, result)
        except LocalHTTPProtocolError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (UnicodeDecodeError, json.JSONDecodeError, WorkbenchError, OSError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "本地服务处理请求时发生未预期错误；操作可能已经完成，请刷新页面并检查项目状态后再决定是否重试"})


def render_product_shell(token: str) -> str:
    from studio_web.research import research_shell

    token_json = json.dumps(token).replace("<", "\\u003c")
    return research_shell("product").replace("__TOKEN__", token_json)


def serve_product_app(
    *,
    data_dir: Path | str | None = None,
    project_dir: Path | str | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
) -> tuple[ProductHTTPServer, str]:
    if host.casefold() not in LOOPBACK_HOSTS:
        raise WorkbenchError("Argument Workbench 只能监听本机 loopback 地址")
    if not 0 <= port <= 65535:
        raise WorkbenchError("端口必须在 0 到 65535 之间")
    app = ProductApp.create(data_dir, project_dir)
    server = ProductHTTPServer((host, port), app)
    address = server.server_address
    url = f"http://{address[0]}:{address[1]}/"
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    return server, url


__all__ = [
    "ProductApp",
    "ProductHTTPServer",
    "create_uploaded_project",
    "default_data_dir",
    "project_state",
    "render_product_shell",
    "serve_product_app",
]
