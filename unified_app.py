"""One loopback entry point, with an isolated compatibility research workspace."""

from __future__ import annotations

import threading
import webbrowser
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from argument_app import ProductApp, ProductRequestHandler, default_data_dir
from argument_workbench import _read_json
from document_review_ui import (
    StudioApp, StudioHTTPServer, StudioRequestHandler, LOOPBACK_HOSTS,
    default_studio_data_dir,
)
from document_review_studio import ReviewStudioError


@dataclass(frozen=True)
class UnifiedApp(StudioApp):
    research_data_dir: Path | None = None

    def view(self):
        value = super().view()
        research = ProductApp(self.research_data_dir or self.data_dir, self.token)
        value["research_projects"] = [
            {**row, "directory": Path(row["path"]).name} for row in research.projects()
        ]
        value["unified"] = True
        return value


class UnifiedRequestHandler(StudioRequestHandler, ProductRequestHandler):
    """Reuse legacy domain handlers without sharing their selected-project state.

    A per-request facade adapts their server contract. The common lock guards
    reading and publishing the immutable app value; no global routing mutation.
    """

    def _research(self, method):
        server, original_path = self.server, self.path
        with server.action_lock:
            facade = SimpleNamespace(app=server.research_app, action_lock=server.action_lock)
            self.server = facade
            self.path = original_path[len("/research"):] or "/"
            self._in_research = True
            try:
                method(self)
            finally:
                server.research_app = facade.app
                self.server, self.path = server, original_path
                self._in_research = False

    def _send(self, status, data, content_type, *, filename=None):
        if getattr(self, "_in_research", False) and content_type.startswith("text/html"):
            shell = data.decode("utf-8")
            # Only rewrite shell-owned URL literals, never user JSON or payloads.
            for quote in ("'", '"', "`"):
                shell = shell.replace(quote + "/api/", quote + "/research/api/")
                shell = shell.replace(quote + "/professional", quote + "/research/professional")
            shell = shell.replace("<body>", '<body><p style="padding:12px"><a href="/">← 返回统一文书与学术工作台</a></p>', 1)
            data = shell.encode("utf-8")
        return StudioRequestHandler._send(self, status, data, content_type, filename=filename)

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/research" or path.startswith("/research/"):
            return self._research(ProductRequestHandler.do_GET)
        return StudioRequestHandler.do_GET(self)

    def do_POST(self):  # noqa: N802
        if urlsplit(self.path).path.startswith("/research/"):
            return self._research(ProductRequestHandler.do_POST)
        return StudioRequestHandler.do_POST(self)


def serve_unified_app(*, data_dir=None, project_dir=None, host="127.0.0.1", port=0, open_browser=True):
    if host.casefold() not in LOOPBACK_HOSTS or not 0 <= port <= 65535:
        raise ReviewStudioError("统一工作台只能监听本机 loopback 地址和有效端口")
    candidate = Path(project_dir) if project_dir else None
    if candidate is not None and candidate.is_symlink():
        raise ReviewStudioError("项目目录不得是符号链接")
    legacy = False
    if candidate is not None:
        # Explicit CLI destinations have never required a suffix. Inspect the
        # existing manifest, then let the appropriate domain verify it fully.
        manifest, _ = _read_json(candidate / "project.json")
        legacy = manifest.get("artifact") == "argument-project"
    storage = Path(data_dir or default_studio_data_dir()).resolve()
    research_storage = Path(data_dir or default_data_dir()).resolve()
    base = StudioApp.create(storage, None if legacy else candidate)
    app = UnifiedApp(base.data_dir, base.token, base.project, research_data_dir=research_storage)
    server = StudioHTTPServer((host, port), app)
    try:
        server.research_app = replace(ProductApp.create(research_storage, candidate if legacy else None), token=base.token)
        server.RequestHandlerClass = UnifiedRequestHandler
    except Exception:
        server.server_close()
        raise
    address = server.server_address
    url = f"http://{address[0]}:{address[1]}/" + ("research/" if legacy else "")
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    return server, url
