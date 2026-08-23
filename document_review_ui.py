"""Tiny loopback browser shell for Document Review Studio."""

from __future__ import annotations

import base64
import json
import secrets
import threading
import webbrowser
from dataclasses import dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from document_review_ingest import IngestionLimits, doctor_dependencies
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
        return {"storage_path": str(self.data_dir), "projects": self.projects(), "selected": self.project.view() if self.project else None, "dependencies": doctor_dependencies()}

    def open_project(self, directory: str) -> "StudioApp":
        if Path(directory).name != directory or not directory.endswith(".document-review-studio"):
            raise ReviewStudioError("项目目录名无效")
        target = (self.data_dir / directory).resolve()
        if target.parent != self.data_dir or target.is_symlink() or not target.is_dir():
            raise ReviewStudioError("项目不在本地文档审查项目库中")
        return replace(self, project=DocumentReviewProject(target))

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
        project = self.require_project()
        action = payload.get("action")
        data = payload.get("data", {})
        if not isinstance(action, str) or not isinstance(data, dict):
            raise ReviewStudioError("操作请求无效")
        if action == "confirm_extraction":
            project.confirm_extraction(str(data.get("choice", "")), corrected_text=data.get("corrected_text"))
        elif action == "confirm_context":
            project.confirm_context(data)
        elif action in {"run_audits", "run_local_prechecks"}:
            project.run_local_prechecks(data.get("critics"))
        elif action == "prepare_ai_audits":
            project.prepare_ai_audits(data.get("critics"), provider=str(data.get("provider", "")), model=str(data.get("model", "")))
        elif action == "import_ai_audit":
            project.collect_model_audit(str(data.get("critic", "")), str(data.get("response", "")), provider=str(data.get("provider", "")), model=str(data.get("model", "")), request_id=str(data.get("request_id", "")) or None)
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
        path = urlsplit(self.path).path
        if path == "/":
            self._send(HTTPStatus.OK, render_studio_shell(self.server.app.token).encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state" and self._auth():
            self._json(HTTPStatus.OK, self.server.app.view())
        else:
            self._json(HTTPStatus.FORBIDDEN if path == "/api/state" else HTTPStatus.NOT_FOUND, {"error": "local UI token required" if path == "/api/state" else "not found"})

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
    shell = shell.replace("<title>Document Review Studio</title>", "<title>Document Review Studio · Experimental Preview</title>")
    shell = shell.replace("<h1>Document Review Studio</h1>", "<h1>Document Review Studio <span class=\"pill\">experimental preview</span></h1>")
    shell = shell.replace("先确认识别，再运行独立审查；Finding 不投票、不打总分，人工决定是否进入修改闭环。", "先确认抽取内容，再分别运行本地确定性预检或导入五个独立 AI critic；Finding 不投票、不打总分，人工决定并可修正动作。")
    shell = shell.replace("<h2>运行独立审查</h2><p>每个维度单独保存，保留分歧，不产生总分。</p>", "<h2>运行本地确定性预检</h2><p>这是关键词和结构规则预检，不是专业 AI 审查；每个维度单独保存，保留分歧，不产生总分。</p>")
    shell = shell.replace("运行选中的审查</button>", "运行选中的本地预检</button>")
    shell = shell.replace("act('run_audits'", "act('run_local_prechecks'")
    shell = shell.replace("+(e.available?'<div class=\"row\">", "+(e.available?'<h3>抽取内容与定位预览</h3><div class=\"quote\">'+(e.blocks||[]).map(b=>'['+esc(b.location&&b.location.block_id||b.block_id)+' · page '+esc(b.location&&b.location.page||'-')+'] '+esc(b.text)).join('\\n\\n')+'</div><div class=\"row\">")
    ai_card = "if(s.can_review){body+='<div class=\"card next\"><h2>运行 / 导入独立 AI 审查</h2><p>先为每个 critic 导出独立协议，再把严格 JSON 原始响应导回。系统保存 provider、model、prompt hash、原始响应和解析结果。</p><div class=\"grid\"><div><label>Provider</label><input id=\"ai-provider\" value=\"external\"><label>Model</label><input id=\"ai-model\" placeholder=\"例如 gpt-5\"><button id=\"prepare-ai\">导出五份独立协议</button></div><div><label>已导出的协议</label><select id=\"ai-request\">'+(s.ai_requests||[]).map(r=>'<option value=\"'+esc(r.request_id)+'\">'+esc(r.critic)+' · '+esc(r.provider)+'/'+esc(r.model)+'</option>').join('')+'</select><label>模型原始 JSON 响应</label><textarea id=\"ai-response\" placeholder=\"粘贴所选 critic 的严格 JSON 原始响应\"></textarea><button id=\"import-ai\">导入并校验 AI 审查</button></div></div>'+(s.ai_requests||[]).map(r=>'<details><summary>'+esc(r.critic)+' · prompt '+esc(r.prompt_sha256.slice(0,12))+'</summary><div class=\"block\">'+esc(r.prompt)+'</div></details>').join('')+'<div id=\"err\" class=\"error\"></div></div>';}"
    shell = shell.replace("if(s.findings.length){", ai_card + "if(s.findings.length||['local_precheck_completed','ai_review_imported'].includes(st.review_state)){if(!s.findings.length)body+='<div class=\"card\"><h2>本轮没有产生 Finding</h2><p>零 Finding 结果仍保留审查范围和依据，不代表自动确认合规或质量。</p></div>';" )
    shell = shell.replace("+'<input id=\"reason-'+esc(f.finding_id)+'\" placeholder=\"人工决定理由\"><div class=\"row\"><button class=\"decision\" data-id=\"'+esc(f.finding_id)+'\" data-decision=\"accept\">接受</button>", "+'<input id=\"reason-'+esc(f.finding_id)+'\" placeholder=\"人工决定理由\"><label>人工修正动作（accept/correct 时优先进入修改桥）</label><textarea id=\"action-'+esc(f.finding_id)+'\" placeholder=\"'+esc(f.suggested_action)+'\"></textarea><div class=\"row\"><button class=\"decision\" data-id=\"'+esc(f.finding_id)+'\" data-decision=\"accept\">接受</button><button class=\"decision secondary\" data-id=\"'+esc(f.finding_id)+'\" data-decision=\"correct\">修正后接受</button>")
    shell = shell.replace("document.querySelectorAll('.decision').forEach(b=>b.onclick=()=>act('decide_finding',{finding_id:b.dataset.id,decision:b.dataset.decision,reason:document.getElementById('reason-'+b.dataset.id).value}));", "if(document.getElementById('prepare-ai'))document.getElementById('prepare-ai').onclick=()=>act('prepare_ai_audits',{critics:['expression_ambiguity','execution_feasibility','compliance_legal_screen','reasonableness_governance','official_professional_format'],provider:document.getElementById('ai-provider').value,model:document.getElementById('ai-model').value});if(document.getElementById('import-ai'))document.getElementById('import-ai').onclick=()=>{const id=document.getElementById('ai-request').value,r=(s.ai_requests||[]).find(x=>x.request_id===id);if(!r)return alert('请先导出协议');act('import_ai_audit',{request_id:id,critic:r.critic,provider:r.provider,model:r.model,response:document.getElementById('ai-response').value})};document.querySelectorAll('.decision').forEach(b=>b.onclick=()=>act('decide_finding',{finding_id:b.dataset.id,decision:b.dataset.decision,reason:document.getElementById('reason-'+b.dataset.id).value,corrected_action:document.getElementById('action-'+b.dataset.id).value||null}));")
    return shell


__all__ = ["StudioApp", "StudioHTTPServer", "default_studio_data_dir", "render_studio_shell", "serve_document_review_studio"]
