"""Tiny loopback browser shell for Document Review Studio."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import threading
import webbrowser
from dataclasses import dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from document_review_ingest import IngestionLimits, doctor_dependencies, repair_dependencies
from document_review_studio import DocumentReviewProject, ReviewStudioError


LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
MAX_REQUEST_BYTES = 42 * 1024 * 1024


def default_studio_data_dir() -> Path:
    import os
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
                state = project.state()
                rows.append({"directory": path.name, "title": manifest.get("title", path.name), "source_name": manifest.get("source", {}).get("name", ""), "state": state})
            except (OSError, ValueError, KeyError):
                rows.append({"directory": path.name, "title": path.name, "invalid": True})
        return rows

    def view(self) -> dict[str, Any]:
        selected = self.project.view() if self.project else None
        if selected is not None:
            selected["directory"] = self.project.root.name
        return {"storage_path": str(self.data_dir), "projects": self.projects(), "selected": selected, "dependencies": doctor_dependencies()}

    def open_project(self, directory: str) -> "StudioApp":
        if Path(directory).name != directory or not directory.endswith(".document-review-studio"):
            raise ReviewStudioError("项目目录名无效")
        target = (self.data_dir / directory).resolve()
        if target.parent != self.data_dir or target.is_symlink() or not target.is_dir():
            raise ReviewStudioError("项目不在本地文档审查项目库中")
        return replace(self, project=DocumentReviewProject(target))

    def delete_project(self, directory: str) -> "StudioApp":
        if not isinstance(directory, str) or Path(directory).name != directory or not directory.endswith(".document-review-studio"):
            raise ReviewStudioError("项目目录名无效")
        candidate = self.data_dir / directory
        if candidate.is_symlink() or not candidate.is_dir():
            raise ReviewStudioError("项目不存在或不是安全的本地目录")
        target = candidate.resolve()
        if target.parent != self.data_dir or target == self.data_dir or not target.is_dir():
            raise ReviewStudioError("拒绝删除项目库之外的目录")
        shutil.rmtree(target)
        selected = None if self.project and self.project.root == target else self.project
        return replace(self, project=selected)

    def repair_environment(self, names: list[str] | None = None) -> "StudioApp":
        repair_dependencies(names)
        return self

    def open_export_folder(self, relative_path: str) -> "StudioApp":
        project = self.require_project()
        target = project.export_file(relative_path)
        folder = target.parent
        if os.name == "nt" and hasattr(os, "startfile"):
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif os.name == "darwin":
            subprocess.Popen(["open", str(folder)], close_fds=True)
        else:
            subprocess.Popen(["xdg-open", str(folder)], close_fds=True)
        return self

    def require_project(self) -> DocumentReviewProject:
        if self.project is None:
            raise ReviewStudioError("请先上传或打开文档")
        return self.project

    def upload(self, payload: dict[str, Any]) -> "StudioApp":
        filename = payload.get("filename")
        encoded = payload.get("content_base64")
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
        return replace(self, project=project)

    def act(self, payload: dict[str, Any]) -> "StudioApp":
        action = payload.get("action")
        data = payload.get("data", {})
        if not isinstance(action, str) or not isinstance(data, dict):
            raise ReviewStudioError("操作请求无效")
        if action == "delete_project":
            return self.delete_project(str(data.get("directory", "")))
        if action == "close_project":
            return replace(self, project=None)
        if action == "repair_environment":
            names = data.get("names")
            if names is not None and (not isinstance(names, list) or not all(isinstance(name, str) for name in names)):
                raise ReviewStudioError("环境修复目标无效")
            return self.repair_environment(names)
        if action == "open_export_folder":
            return self.open_export_folder(str(data.get("relative_path", "")))
        project = self.require_project()
        if action == "confirm_extraction":
            project.confirm_extraction(str(data.get("choice", "")), corrected_text=data.get("corrected_text"))
        elif action == "confirm_context":
            project.confirm_context(data)
        elif action in {"run_audits", "run_local_prechecks"}:
            project.run_local_prechecks(data.get("critics"))
        elif action == "prepare_ai_audits":
            project.prepare_ai_audits(data.get("critics"), provider=str(data.get("provider", "")), model=str(data.get("model", "")))
        elif action == "import_ai_audit":
            project.collect_model_audit(str(data.get("critic", "")), str(data.get("response", "")), provider=str(data.get("provider", "")), model=str(data.get("model", "")), request_id=str(data.get("request_id", "")) or None, binding_mode=str(data.get("binding_mode", "strict")))
        elif action == "decide_finding":
            project.decide_finding(str(data.get("finding_id", "")), str(data.get("decision", "")), reason=str(data.get("reason", "")), corrected_action=data.get("corrected_action"))
        elif action == "prepare_bridge":
            project.prepare_revision_bridge()
        elif action == "export":
            project.export(revised_markdown=data.get("revised_markdown"))
        else:
            raise ReviewStudioError("未知操作")
        return self


class StudioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: StudioApp):
        self.app = app
        super().__init__(address, StudioRequestHandler)


class StudioRequestHandler(BaseHTTPRequestHandler):
    server: StudioHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: HTTPStatus, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: HTTPStatus, value: Any) -> None:
        self._send(status, (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"), "application/json; charset=utf-8")

    def _auth(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Document-Review-Token", ""), self.server.app.token)

    def do_GET(self) -> None:  # noqa: N802
        parsed_url = urlsplit(self.path)
        path = parsed_url.path
        if path == "/":
            self._send(HTTPStatus.OK, render_studio_shell(self.server.app.token).encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state" and self._auth():
            self._json(HTTPStatus.OK, self.server.app.view())
        elif path == "/api/download" and self._auth():
            try:
                project = self.server.app.require_project()
                relative_path = parse_qs(parsed_url.query).get("path", [""])[0]
                target = project.export_file(relative_path)
                content = target.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Content-Disposition", f'attachment; filename="{target.name.replace(chr(34), "")}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(content)
            except (OSError, ValueError, ReviewStudioError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        else:
            self._json(HTTPStatus.FORBIDDEN if path in {"/api/state", "/api/download"} else HTTPStatus.NOT_FOUND, {"error": "local UI token required" if path in {"/api/state", "/api/download"} else "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
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
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ReviewStudioError("请求必须是对象")
            if path == "/api/upload":
                self.server.app = self.server.app.upload(payload)
            elif path == "/api/open":
                self.server.app = self.server.app.open_project(str(payload.get("directory", "")))
            else:
                self.server.app = self.server.app.act(payload)
            self._json(HTTPStatus.CREATED, self.server.app.view())
        except (UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


def render_studio_shell(token: str) -> str:
    return r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Document Review Studio</title><style>
:root{--ink:#18251e;--muted:#6d756d;--line:#d9e1d7;--paper:#fbfcf8;--green:#205a47;--amber:#b87920;--red:#a33d32}*{box-sizing:border-box}body{margin:0;background:#eef2ed;color:var(--ink);font:15px system-ui,-apple-system,"Segoe UI",sans-serif}main{max-width:1180px;margin:auto;padding:30px 20px 60px}.brand{letter-spacing:.16em;text-transform:uppercase;color:var(--green);font-size:12px}h1,h2,h3{font-family:Georgia,serif;font-weight:500}h1{font-size:40px;margin:8px 0}h2{font-size:25px;margin:0 0 10px}.card{background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:20px;margin:15px 0;box-shadow:0 8px 28px #2038260a}.next{border-left:5px solid var(--amber)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.muted{color:var(--muted)}.warning{color:#8a5d1c}.error{color:var(--red)}.ok{color:var(--green)}label{display:block;font-weight:650;margin:12px 0 5px}input,select,textarea,button{font:inherit}input[type=text],select,textarea{width:100%;border:1px solid #bfcbbd;border-radius:8px;padding:10px;background:#fff}textarea{min-height:120px;resize:vertical}button{border:0;border-radius:8px;padding:10px 15px;background:var(--green);color:#fff;cursor:pointer}button.secondary{background:#dde6dc;color:var(--ink)}button.danger{background:var(--red)}.pill{display:inline-block;background:#e4ebe1;border-radius:99px;padding:4px 9px;margin:2px;font-size:12px}.quote{white-space:pre-wrap;background:#f0f4ed;padding:12px;border-radius:8px}.finding{border-left:4px solid var(--amber)}.block{font-family:ui-monospace,monospace;font-size:13px;background:#f2f5f1;padding:8px;border-radius:6px;margin:5px 0;white-space:pre-wrap}.hidden{display:none}@media(max-width:760px){.grid{grid-template-columns:1fr}}
</style></head><body><main><div class="brand">Local-first · model-neutral · human-confirmed</div><h1>Document Review Studio</h1><p class="muted">先确认识别，再运行独立审查；Finding 不投票、不打总分，人工决定是否进入修改闭环。</p><div id="app"></div></main><script>
const TOKEN=__TOKEN__,root=document.getElementById('app');let state;const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Document-Review-Token':TOKEN},body:body?JSON.stringify(body):undefined});const j=await r.json();if(!r.ok)throw Error(j.error);return j}async function act(action,data={}){try{state=await api('/api/action',{action,data});render()}catch(e){const x=document.getElementById('err');if(x)x.textContent=e.message;else alert(e.message)}}function home(){root.innerHTML='<div class="card"><h2>导入文档</h2><p>支持 Markdown、TXT、DOCX、文本 PDF 和扫描 PDF。原件字节会以 SHA-256 绑定并永不覆盖。</p><label>项目标题（可选）</label><input id="title" type="text"><label>文件</label><input id="file" type="file" accept=".md,.txt,.docx,.pdf"><p><button id="upload">开始摄取</button></p><div id="err" class="error"></div></div>'+(state.projects.length?'<div class="card"><h2>本地项目</h2>'+state.projects.map(p=>'<p class="row"><button class="secondary open" data-dir="'+esc(p.directory)+'">打开</button><span>'+esc(p.title)+' · '+esc(p.source_name)+'</span></p>').join('')+'</div>':'')+'<div class="card"><h3>环境自检</h3>'+state.dependencies.map(d=>'<div class="'+(d.available?'ok':'warning')+'">'+esc(d.name)+'：'+(d.available?'可用':'缺失')+' · '+esc(d.purpose)+(d.install?' · '+esc(d.install):'')+'</div>').join('')+'</div>';document.getElementById('upload').onclick=async()=>{try{const f=document.getElementById('file').files[0];if(!f)throw Error('请选择文件');const bytes=new Uint8Array(await f.arrayBuffer());let binary='';for(const b of bytes)binary+=String.fromCharCode(b);state=await api('/api/upload',{filename:f.name,content_base64:btoa(binary),title:document.getElementById('title').value});render()}catch(e){document.getElementById('err').textContent=e.message}};document.querySelectorAll('.open').forEach(b=>b.onclick=async()=>{state=await api('/api/open',{directory:b.dataset.dir});render()})}function render(){if(!state.selected){home();return}const s=state.selected,st=s.state,e=s.extraction,c=s.context;let body='<div class="card"><div class="muted">'+esc(s.project.source.name)+' · SHA-256 '+esc(s.project.source.sha256.slice(0,16))+'</div><h2>'+esc(s.project.title)+'</h2><p>识别状态：<b>'+esc(st.extraction_state)+'</b> · 上下文：<b>'+esc(st.context_state)+'</b></p>'+(e.available?'<p>解析器：'+esc(s.extraction.quality.page_count?'可用':'部分可用')+' · blocks '+s.extraction.blocks.length+'</p>':'<p class="error">没有可用的结构化文档</p>')+'</div>';if(!e.available||['blocked','unconfirmed','replacement_required'].includes(st.extraction_state)){body+='<div class="card next"><h2>识别质量门</h2><p>正式审查前必须确认识别结果。空白页、乱码、OCR 低置信度、修订/批注遗漏都会显示在这里。</p>'+((e.warnings||[]).length?'<ul>'+e.warnings.map(w=>'<li class="'+(w.severity==='critical'||w.severity==='high'?'error':'warning')+'">'+esc(w.message)+'</li>').join('')+'</ul>':'<p class="ok">未发现解析警告</p>')+(s.state.diagnostics||[]).map(x=>'<p class="error">'+esc(x)+'</p>').join('')+(e.available?'<div class="row"><button id="confirm">确认识别</button><button class="secondary" id="continue">带警告继续</button><button class="secondary" id="correct">修正识别文本</button><button class="danger" id="replace">更换文件</button></div><textarea id="correction" class="hidden" placeholder="需要修正时，在这里提供完整的内部审查文本"></textarea>':'')+'<div id="err" class="error"></div></div>'}else if(['confirmed','confirmed_corrected','confirmed_with_warning'].includes(st.extraction_state)&&st.context_state!=='confirmed'){body+='<div class="card next"><h2>确认审查上下文</h2><p class="muted">模型建议：'+esc(c.model_suggestion||'专业文档')+'；最终类型、地区和发布状态由你确认。</p><div class="grid"><div><label>文档类型</label><input id="document_type" value="'+esc(c.document_type||c.model_suggestion||'专业文档')+'"><label>适用地区/司法辖区</label><input id="jurisdiction"><label>拟生效日期</label><input id="effective_date"><label>发布者/组织类型</label><input id="publisher_type"><label>目标受众</label><input id="audience"></div><div><label>发布状态</label><select id="publication_status"><option value="internal-draft">内部草案</option><option value="external-formal">对外正式文件</option></select><label><input id="involves_minors" type="checkbox"> 涉及未成年人</label><label><input id="involves_fees" type="checkbox"> 涉及收费</label><label><input id="involves_sponsorship" type="checkbox"> 涉及赞助</label><label><input id="involves_contract" type="checkbox"> 涉及合同</label><label><input id="involves_personal_information" type="checkbox"> 涉及个人信息</label><label><input id="involves_intellectual_property" type="checkbox"> 涉及知识产权</label></div></div><p><button id="context">确认上下文</button></p><div id="err" class="error"></div></div>'}else if(st.review_state==='not_started'||st.review_state==='completed'){body+='<div class="card next"><h2>运行独立审查</h2><p>每个维度单独保存，保留分歧，不产生总分。</p><div class="row">'+['expression_ambiguity','execution_feasibility','compliance_legal_screen','reasonableness_governance','official_professional_format'].map(x=>'<label><input class="critic" type="checkbox" value="'+x+'" checked> '+x+'</label>').join('')+'</div><p><button id="run">运行选中的审查</button></p><div id="err" class="error"></div></div>'}if(s.findings.length){body+='<div class="card"><h2>人工裁决 Finding</h2><p class="muted">不同审查维度不投票合并；每条都保留证据、定位、标准、后果和不确定项。</p></div>'+s.findings.map(f=>'<div class="card finding"><div class="row"><span class="pill">'+esc(f.finding_id)+'</span><span class="pill">'+esc(f.critic)+'</span><span class="pill">'+esc(f.severity)+'</span><span class="pill">'+esc(f.verification_state)+'</span></div><p><b>定位：</b>'+esc(f.location.block_id)+' · page '+esc(f.location.page||'-')+'</p><div class="quote">'+esc(f.evidence)+'</div><p><b>问题：</b>'+esc(f.issue)+'</p><p><b>后果：</b>'+esc(f.consequence)+'</p><p><b>动作：</b>'+esc(f.suggested_action)+'</p>'+(f.competing_readings&&f.competing_readings.length?'<p><b>竞争读法：</b>'+esc(f.competing_readings.join('；'))+'</p>':'')+'<input id="reason-'+esc(f.finding_id)+'" placeholder="人工决定理由"><div class="row"><button class="decision" data-id="'+esc(f.finding_id)+'" data-decision="accept">接受</button><button class="decision danger" data-id="'+esc(f.finding_id)+'" data-decision="reject">拒绝</button><button class="decision secondary" data-id="'+esc(f.finding_id)+'" data-decision="defer">暂缓</button><span>当前：'+esc(f.status)+'</span></div></div>').join('');body+='<div class="card"><button id="bridge">准备进入受约束修改闭环</button> <button id="export">导出审计报告与草稿</button><div id="err" class="error"></div></div>'}root.innerHTML=body;if(document.getElementById('confirm'))document.getElementById('confirm').onclick=()=>act('confirm_extraction',{choice:'confirm'});if(document.getElementById('continue'))document.getElementById('continue').onclick=()=>act('confirm_extraction',{choice:'continue_with_warning'});if(document.getElementById('correct'))document.getElementById('correct').onclick=()=>{document.getElementById('correction').classList.remove('hidden');document.getElementById('correct').onclick=()=>act('confirm_extraction',{choice:'correct',corrected_text:document.getElementById('correction').value})};if(document.getElementById('replace'))document.getElementById('replace').onclick=()=>act('confirm_extraction',{choice:'replace'});if(document.getElementById('context'))document.getElementById('context').onclick=()=>{const b=id=>document.getElementById(id);act('confirm_context',{document_type:b('document_type').value,jurisdiction:b('jurisdiction').value,effective_date:b('effective_date').value,publisher_type:b('publisher_type').value,audience:b('audience').value,publication_status:b('publication_status').value,...Object.fromEntries(['involves_minors','involves_fees','involves_sponsorship','involves_contract','involves_personal_information','involves_intellectual_property'].map(x=>[x,b(x).checked]))})};if(document.getElementById('run'))document.getElementById('run').onclick=()=>act('run_audits',{critics:Array.from(document.querySelectorAll('.critic:checked')).map(x=>x.value)});document.querySelectorAll('.decision').forEach(b=>b.onclick=()=>act('decide_finding',{finding_id:b.dataset.id,decision:b.dataset.decision,reason:document.getElementById('reason-'+b.dataset.id).value}));if(document.getElementById('bridge'))document.getElementById('bridge').onclick=()=>act('prepare_bridge');if(document.getElementById('export'))document.getElementById('export').onclick=()=>act('export')}api('/api/state').then(x=>{state=x;render()}).catch(e=>root.innerHTML='<div class="card error">'+esc(e.message)+'</div>')</script></body></html>'''.replace("__TOKEN__", json.dumps(token))


def serve_document_review_studio(*, data_dir: Path | str | None = None, project_dir: Path | str | None = None, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> tuple[StudioHTTPServer, str]:
    if host.casefold() not in LOOPBACK_HOSTS:
        raise ReviewStudioError("Document Review Studio 只能监听本机 loopback 地址")
    if not 0 <= port <= 65535:
        raise ReviewStudioError("端口必须在 0 到 65535 之间")
    app = StudioApp.create(data_dir, project_dir)
    server = StudioHTTPServer((host, port), app)
    address = server.server_address
    url = f"http://{address[0]}:{address[1]}/"
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    return server, url


_render_studio_shell_base = render_studio_shell


def render_studio_shell(token: str) -> str:
    """Mark the product as preview and expose local and AI review as separate paths."""
    shell = _render_studio_shell_base(token)
    shell = shell.replace("</style>", ".workflow{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin:10px 0}.workflow-step{padding:9px 7px;border-radius:8px;background:#eef3ec;font-size:12px}.workflow-step.current{background:#f7e4c3;border:1px solid var(--amber)}.workflow-step.done{background:#dcece1}.workflow-step b{display:block;margin-top:3px}.summary{display:flex;gap:8px;flex-wrap:wrap}.summary .pill{background:#eef3ec}.toolbar{display:flex;gap:8px;align-items:end;flex-wrap:wrap}.toolbar>label{min-width:145px}.review-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,2fr);gap:15px}.finding-list{min-width:0}.finding-card{scroll-margin-top:15px}.finding-card.selected{outline:2px solid var(--amber)}.sticky{position:sticky;top:12px;align-self:start}.export-file{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--line)}@media(max-width:900px){.workflow{grid-template-columns:repeat(4,1fr)}.review-grid{grid-template-columns:1fr}.sticky{position:static}}@media(max-width:760px){.workflow{grid-template-columns:repeat(2,1fr)}}" + "</style>")
    shell = shell.replace("<title>Document Review Studio</title>", "<title>Document Review Studio · Experimental Preview</title>")
    shell = shell.replace("<h1>Document Review Studio</h1>", "<h1>Document Review Studio <span class=\"pill\">experimental preview</span></h1>")
    shell = shell.replace("先确认识别，再运行独立审查；Finding 不投票、不打总分，人工决定是否进入修改闭环。", "先确认抽取内容，再按步骤完成预检、AI 专项审查、人工裁决和导出。系统不投票、不打总分，接受的 Finding 才能进入修改闭环。")
    shell = shell.replace("先确认识别，再运行独立审查；Finding 不投票、不打总分，人工决定是否进入修改闭环。", "先确认抽取内容，再分别运行本地确定性预检或导入五个独立 AI critic；Finding 不投票、不打总分，人工决定并可修正动作。")
    shell = shell.replace("<h2>运行独立审查</h2><p>每个维度单独保存，保留分歧，不产生总分。</p>", "<h2>运行本地确定性预检</h2><p>这是关键词和结构规则预检，不是专业 AI 审查；每个维度单独保存，保留分歧，不产生总分。</p>")
    shell = shell.replace("运行选中的审查</button>", "运行选中的本地预检</button>")
    shell = shell.replace("act('run_audits'", "act('run_local_prechecks'")
    shell = shell.replace("+(e.available?'<div class=\"row\">", "+(e.available?'<h3>抽取内容与定位预览</h3><input id=\"extraction-search\" placeholder=\"搜索正文或 block 定位\"><div id=\"extraction-preview\" class=\"quote\">'+(e.blocks||[]).map(b=>'['+esc(b.location&&b.location.block_id||b.block_id)+' · page '+esc(b.location&&b.location.page||'-')+'] '+esc(b.text)).join('\\n\\n')+'</div><div class=\"row\">")
    shell = shell.replace("const s=state.selected,st=s.state,e=s.extraction,c=s.context;let body='<div class=\"card\">", "const s=state.selected,st=s.state,e=s.extraction,c=s.context,labels={expression_ambiguity:'表达清晰度',execution_feasibility:'执行可行性',compliance_legal_screen:'合规风险筛查',reasonableness_governance:'治理合理性',official_professional_format:'正式规范性'},workflow=s.workflow||[],next=workflow.find(x=>x.status!=='completed'),integrity=(st.read_only||st.integrity_errors&&st.integrity_errors.length)?'<div class=\"card error\"><b>项目已切换为只读</b><p>完整性链或审计产物存在问题。请先查看诊断并恢复可信项目副本；当前不会继续写入正式审查结果。</p></div>':'';let body=integrity+'<div class=\"card\"><div class=\"workflow\">'+workflow.map(x=>'<div class=\"workflow-step '+(x.status==='completed'?'done ': '')+(next&&next.key===x.key?'current':'')+'\"><span>'+esc(x.label)+'</span><b>'+esc(x.detail)+'</b></div>').join('')+'</div><p class=\"next-step\"><b>'+(next?'继续：'+esc(next.label):'本轮流程已完成')+'</b></p></div><div class=\"card\">")
    shell = shell.replace("'<p class=\"row\"><button class=\"secondary open\" data-dir=\"'+esc(p.directory)+'\">打开</button><span>'", "'<p class=\"row\"><button class=\"secondary open\" data-dir=\"'+esc(p.directory)+'\">打开</button><button class=\"danger delete-project\" data-dir=\"'+esc(p.directory)+'\">删除</button><span>'")
    shell = shell.replace("<h3>环境自检</h3>", "<h3>环境自检</h3><p class=\"muted\">缺少 Python 适配器时可一键安装；Tesseract 等系统组件按提示处理。</p><p><button class=\"secondary\" id=\"repair-environment\">一键修复可自动修复项</button></p>")
    shell = shell.replace("state.dependencies.map(d=>'<div class=\"'+(d.available?'ok':'warning')+'\">'+esc(d.name)+'：'+(d.available?'可用':'缺失')+' · '+esc(d.purpose)+(d.install?' · '+esc(d.install):'')+'</div>').join('')", "state.dependencies.map(d=>'<div class=\"'+(d.available?'ok':'warning')+'\"><b>'+esc(d.name)+'</b>：'+(d.available?'可用':'缺失')+' · '+esc(d.purpose)+(d.install?' · '+esc(d.install):'')+(d.available?'':(d.repairable?'<button class=\"secondary repair-dependency\" data-dependency=\"'+esc(d.repair_key)+'\">一键修复</button>':'<div class=\"muted\">'+esc(d.repair_hint||'请按说明处理')+'</div>'))+'</div>').join('')")
    shell = shell.replace("<h2>'+esc(s.project.title)+'</h2>", "<div class=\"row\"><button class=\"secondary\" id=\"back-projects\">← 返回项目列表</button><button class=\"danger\" id=\"delete-selected\" data-dir=\"'+esc(s.directory||'')+'\">删除本地项目</button></div><h2>'+esc(s.project.title)+'</h2><p class=\"muted\">原件：'+esc(s.project.source.name)+' · 原始 SHA-256：'+esc(s.project.source.sha256.slice(0,16))+'…</p>")
    shell = shell.replace("if(s.findings.length){body+='<div class=\"card\"><h2>人工裁决 Finding</h2><p class=\"muted\">不同审查维度不投票合并；每条都保留证据、定位、标准、后果和不确定项。</p></div>'", "if(s.findings.length){body+='<div class=\"card\"><h2>人工裁决队列</h2><div class=\"summary\"><span class=\"pill\">共 '+(s.finding_summary?.total||0)+' 条</span><span class=\"pill\">待处理 '+(s.finding_summary?.open||0)+' 条</span><span class=\"pill\">高/严重 '+((s.finding_summary?.by_severity?.high||0)+(s.finding_summary?.by_severity?.critical||0))+' 条</span></div><div class=\"toolbar\"><label>审查维度<select id=\"finding-critic-filter\"><option value=\"\">全部维度</option>'+Object.entries(labels).map(([key,label])=>'<option value=\"'+key+'\">'+label+'</option>').join('')+'</select></label><label>严重度<select id=\"finding-severity-filter\"><option value=\"\">全部严重度</option><option>critical</option><option>high</option><option>medium</option><option>low</option><option>info</option></select></label><label>状态<select id=\"finding-status-filter\"><option value=\"\">全部状态</option><option value=\"open\">待处理</option><option value=\"accept\">接受</option><option value=\"correct\">修正</option><option value=\"reject\">拒绝</option><option value=\"defer\">暂缓</option></select></label></div></div><div class=\"review-grid\"><div class=\"finding-list\">'")
    shell = shell.replace("s.findings.map(f=>'<div class=\"card finding\">", "s.findings.map(f=>'<div class=\"card finding finding-card\" id=\"finding-'+esc(f.finding_id)+'\" data-critic=\"'+esc(f.critic)+'\" data-severity=\"'+esc(f.severity)+'\" data-status=\"'+esc(f.status)+'\">")
    shell = shell.replace("esc(f.critic)+'</span><span class=\"pill\">", "esc(labels[f.critic]||f.critic)+'</span><span class=\"pill\">")
    shell = shell.replace("+'<input id=\"reason-'+esc(f.finding_id)+'\" placeholder=\"人工决定理由\"><div class=\"row\">", "+'<details><summary>查看完整审查信息</summary><p><b>判断标准：</b>'+esc(f.standard)+'</p><p><b>验证状态：</b>'+esc(f.verification_state)+'</p><p><b>外部依据：</b>'+esc((f.external_basis&&((f.external_basis.source_name||'')+' '+(f.external_basis.locator||'')))||'未提供')+'</p><p><b>尚待确认：</b>'+esc((f.uncertainties||[]).join('；')||'无')+'</p><p><b>建议责任人：</b>'+esc(f.suggested_owner||'未指定')+' · <b>阻断发布/执行：</b>'+((f.blocks_release_or_execution)?'是':'否')+'</p><p><b>需要观察：</b>'+esc(f.required_observation||'无')+'</p>'+(f.competing_readings&&f.competing_readings.length?'<p><b>竞争读法：</b>'+esc(f.competing_readings.join('；'))+'</p>':'')+'</details><input id=\"reason-'+esc(f.finding_id)+'\" placeholder=\"人工决定理由\"><div class=\"row\">")
    shell = shell.replace("placeholder=\"人工决定理由\"><div class=\"row\">", "placeholder=\"人工决定理由\"><label>人工修正动作（accept/correct 时优先进入修改桥）</label><textarea id=\"action-'+esc(f.finding_id)+'\" placeholder=\"'+esc(f.suggested_action)+'\"></textarea><div class=\"row\">")
    shell = shell.replace("body+='<div class=\"card\"><button id=\"bridge\">准备进入受约束修改闭环</button> <button id=\"export\">导出审计报告与草稿</button><div id=\"err\" class=\"error\"></div></div>'", "body+='</div><aside class=\"card sticky\"><h3>审查摘要</h3><p>先按维度和严重度筛选，再逐条处理。接受或修正后的 Finding 才会进入修改桥。</p><p><b>待处理：</b>'+(s.finding_summary?.open||0)+' · <b>已处理：</b>'+((s.finding_summary?.accept||0)+(s.finding_summary?.correct||0)+(s.finding_summary?.reject||0)+(s.finding_summary?.defer||0))+'</p><button id=\"bridge\">生成修改任务</button> <button id=\"export\">导出审查结果</button></aside></div><div id=\"err\" class=\"error\"></div>'")
    shell = shell.replace("root.innerHTML=body;if(document.getElementById('confirm'))", "const exports=s.exports||[];if(exports.length){body+='<div class=\"card\"><h2>导出中心</h2><p class=\"ok\">导出文件已经生成，点击即可下载。</p>'+exports.map(x=>'<details open><summary>'+esc(x.kind==='revision-bridge'?'修改任务':'审查导出')+' · '+esc(x.export_id)+(x.finding_count?' · '+x.finding_count+' 条 Finding':'')+'</summary><div>'+x.files.map(f=>'<div class=\"export-file\"><span>'+esc(f.label)+' <small>'+esc(f.name)+'</small></span><button class=\"secondary download-file\" data-path=\"'+esc(f.relative_path)+'\">下载</button></div>').join('')+'</div><p><button class=\"secondary open-export-folder\" data-path=\"'+esc(x.files[0]?.relative_path||'')+'\">打开所在文件夹</button></p></details>').join('')+'</div>'}root.innerHTML=body;if(document.getElementById('confirm'))")
    ai_card = "if(s.can_review){const requests=s.ai_requests||[],done=requests.filter(r=>r.completed).length;body+='<div class=\"card next\"><h2>导出 / 导入独立 AI 审查 <span class=\"pill\">'+done+'/'+(requests.length||5)+' 已导入</span></h2><p>这里不直接调用模型。先导出五份独立协议，再把每个 critic 的结果导回。协议只需要复制一次；导入后会自动记录完成状态。模型来源和版本仅作声明记录。</p><div class=\"summary\">'+requests.map(r=>'<span class=\"pill\">'+(r.completed?'✓ ':'○ ')+esc(labels[r.critic]||r.critic)+'</span>').join('')+'</div><div class=\"grid\"><div><label>模型来源（仅记录）</label><input id=\"ai-provider\" value=\"手动导入\"><label>模型/版本（仅记录）</label><input id=\"ai-model\" value=\"未声明模型\"><button id=\"prepare-ai\">导出五份独立协议</button><p class=\"muted\">如果你暂时不接 API，这里仍然可以用任意模型手动完成；系统不会把它说成直接调用。</p></div><div><label>当前 critic</label><select id=\"ai-request\">'+requests.map(r=>'<option value=\"'+esc(r.request_id)+'\">'+(r.completed?'✓ ':'○ ')+esc(labels[r.critic]||r.critic)+'</option>').join('')+'</select><div class=\"row\"><button class=\"secondary\" id=\"copy-ai\">复制当前协议</button><button class=\"secondary\" id=\"previous-ai\">上一项</button><button class=\"secondary\" id=\"next-ai\">下一项</button></div><label>响应绑定方式</label><select id=\"ai-binding-mode\"><option value=\"strict\">严格绑定（强审计）</option><option value=\"manual_association\">普通 JSON（人工关联，较弱审计）</option></select><label>模型原始 JSON 响应（当前 critic）</label><textarea id=\"ai-response\" placeholder=\"粘贴当前 critic 的 JSON；普通 JSON 模式不要求回显绑定字段\"></textarea><button id=\"import-ai\">导入当前结果</button></div></div>'+(requests.length?requests.map(r=>'<details class=\"ai-protocol\"><summary>'+(r.completed?'✓ ':'○ ')+esc(labels[r.critic]||r.critic)+' · '+esc(r.prompt_sha256.slice(0,12))+'</summary><div class=\"row\"><button class=\"secondary copy-request\" data-request=\"'+esc(r.request_id)+'\">复制这份协议</button></div><div class=\"block\">'+esc(r.prompt)+'</div></details>').join(''):'<p class=\"muted\">还没有协议。点击“导出五份独立协议”开始。</p>')+'<div id=\"err\" class=\"error\"></div></div>';}"
    shell = shell.replace("if(s.findings.length){", ai_card + "if(s.findings.length||['local_precheck_completed','ai_review_imported'].includes(st.review_state)){if(!s.findings.length)body+='<div class=\"card\"><h2>本轮没有产生 Finding</h2><p>零 Finding 结果仍保留审查范围和依据，不代表自动确认合规或质量。</p></div>';" )
    shell = shell.replace("+'<input id=\"reason-'+esc(f.finding_id)+'\" placeholder=\"人工决定理由\"><div class=\"row\"><button class=\"decision\" data-id=\"'+esc(f.finding_id)+'\" data-decision=\"accept\">接受</button>", "+'<input id=\"reason-'+esc(f.finding_id)+'\" placeholder=\"人工决定理由\"><label>人工修正动作（accept/correct 时优先进入修改桥）</label><textarea id=\"action-'+esc(f.finding_id)+'\" placeholder=\"'+esc(f.suggested_action)+'\"></textarea><div class=\"row\"><button class=\"decision\" data-id=\"'+esc(f.finding_id)+'\" data-decision=\"accept\">接受</button><button class=\"decision secondary\" data-id=\"'+esc(f.finding_id)+'\" data-decision=\"correct\">修正后接受</button>")
    shell = shell.replace("document.querySelectorAll('.open').forEach(b=>b.onclick=async()=>{state=await api('/api/open',{directory:b.dataset.dir});render()})", "document.querySelectorAll('.open').forEach(b=>b.onclick=async()=>{state=await api('/api/open',{directory:b.dataset.dir});render()});document.querySelectorAll('.delete-project').forEach(b=>b.onclick=async()=>{if(!confirm('删除后无法恢复，确定删除这个本地项目吗？'))return;state=await api('/api/action',{action:'delete_project',data:{directory:b.dataset.dir}});render()})")
    shell = shell.replace("document.querySelectorAll('.decision').forEach(b=>b.onclick=()=>act('decide_finding',{finding_id:b.dataset.id,decision:b.dataset.decision,reason:document.getElementById('reason-'+b.dataset.id).value}));", "if(document.getElementById('prepare-ai'))document.getElementById('prepare-ai').onclick=()=>act('prepare_ai_audits',{critics:['expression_ambiguity','execution_feasibility','compliance_legal_screen','reasonableness_governance','official_professional_format'],provider:document.getElementById('ai-provider').value,model:document.getElementById('ai-model').value});if(document.getElementById('import-ai'))document.getElementById('import-ai').onclick=()=>{const id=document.getElementById('ai-request').value,r=(s.ai_requests||[]).find(x=>x.request_id===id);if(!r)return alert('请先导出协议');act('import_ai_audit',{request_id:id,critic:r.critic,provider:r.provider,model:r.model,binding_mode:document.getElementById('ai-binding-mode').value,response:document.getElementById('ai-response').value})};if(document.getElementById('repair-environment'))document.getElementById('repair-environment').onclick=()=>act('repair_environment');document.querySelectorAll('.repair-dependency').forEach(b=>b.onclick=()=>act('repair_environment',{names:[b.dataset.dependency]}));if(document.getElementById('delete-selected'))document.getElementById('delete-selected').onclick=async()=>{if(!confirm('删除后无法恢复，确定删除这个本地项目吗？'))return;state=await api('/api/action',{action:'delete_project',data:{directory:document.getElementById('delete-selected').dataset.dir}});render()};document.querySelectorAll('.decision').forEach(b=>b.onclick=()=>act('decide_finding',{finding_id:b.dataset.id,decision:b.dataset.decision,reason:document.getElementById('reason-'+b.dataset.id).value,corrected_action:document.getElementById('action-'+b.dataset.id).value||null}));")
    shell = shell.replace("if(document.getElementById('replace'))document.getElementById('replace').onclick=()=>act('confirm_extraction',{choice:'replace'});", "if(document.getElementById('replace'))document.getElementById('replace').onclick=()=>act('close_project');")
    shell = shell.replace("</script></body></html>", ";root.addEventListener('click',async event=>{const button=event.target.closest('button');if(!button)return;if(button.id==='back-projects'){state=await api('/api/action',{action:'close_project'});render()}else if(button.classList.contains('copy-ai')||button.classList.contains('copy-request')){const id=button.dataset.request||document.getElementById('ai-request')?.value,r=(state.selected?.ai_requests||[]).find(x=>x.request_id===id);if(r){try{await navigator.clipboard.writeText(r.prompt);button.textContent='已复制';setTimeout(()=>button.textContent=button.dataset.request?'复制这份协议':'复制当前协议',1200)}catch(error){alert('浏览器未允许复制，请展开协议后手动复制')}}}else if(button.id==='previous-ai'||button.id==='next-ai'){const select=document.getElementById('ai-request');if(select){select.selectedIndex=Math.max(0,Math.min(select.options.length-1,select.selectedIndex+(button.id==='next-ai'?1:-1)));select.dispatchEvent(new Event('change'))}}else if(button.classList.contains('download-file')){const response=await fetch('/api/download?path='+encodeURIComponent(button.dataset.path),{headers:{'X-Document-Review-Token':TOKEN}});if(!response.ok){const error=await response.json();alert(error.error||'下载失败');return}const blob=await response.blob(),link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download=button.dataset.path.split('/').pop();link.click();URL.revokeObjectURL(link.href)}else if(button.classList.contains('open-export-folder')){act('open_export_folder',{relative_path:button.dataset.path})}});root.addEventListener('change',event=>{if(!event.target.id?.startsWith('finding-'))return;const critic=document.getElementById('finding-critic-filter')?.value||'',severity=document.getElementById('finding-severity-filter')?.value||'',status=document.getElementById('finding-status-filter')?.value||'';document.querySelectorAll('.finding-card').forEach(card=>{card.hidden=Boolean((critic&&card.dataset.critic!==critic)||(severity&&card.dataset.severity!==severity)||(status&&card.dataset.status!==status))})});</script></body></html>")
    shell = shell.replace("</script>", "root.addEventListener('input',event=>{if(event.target.id!=='extraction-search')return;const query=event.target.value.toLowerCase(),blocks=state.selected?.extraction?.blocks||[];document.getElementById('extraction-preview').textContent=blocks.filter(b=>!query||String(b.text||'').toLowerCase().includes(query)||String(b.block_id||'').toLowerCase().includes(query)).map(b=>'['+(b.location?.block_id||b.block_id)+' · page '+(b.location?.page||'-')+'] '+(b.text||'')).join('\\n\\n')});</script>")
    return shell


__all__ = ["StudioApp", "StudioHTTPServer", "default_studio_data_dir", "render_studio_shell", "serve_document_review_studio"]
