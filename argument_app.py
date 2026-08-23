"""Single-entry local application shell for ordinary Argument Workbench users.

The shell owns navigation and upload ergonomics only.  Immutable manuscript
storage remains in :mod:`argument_workbench`; later workflow mutations are
delegated to their domain services rather than implemented in HTTP handlers.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import webbrowser
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
    export_revision,
    import_review_report,
    prepare_atomization,
    prepare_resolution_review,
    prepare_revision_generation,
    workflow_view,
)
from argument_workbench import (
    WorkbenchError,
    _atomic_write,
    _read_json,
    initialize_workspace,
    list_version_ids,
    verify_project_versions,
    workspace_paths,
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
) -> Path:
    """Create an immutable V1 from browser-uploaded bytes."""
    safe_name = _safe_upload_name(filename)
    if not content or len(content) > MAX_REQUEST_BYTES:
        raise WorkbenchError("稿件必须非空且不超过 8 MiB")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorkbenchError(f"稿件不是 UTF-8：{exc}") from exc
    if not text.strip():
        raise WorkbenchError("稿件不能为空")
    root = Path(data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / _project_slug(safe_name, content)
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
        initialize_workspace(source, target, title=title)
    finally:
        source.unlink(missing_ok=True)
        try:
            staging.rmdir()
        except OSError:
            pass
    return target


def project_state(project_dir: Path) -> dict[str, Any]:
    errors = verify_project_versions(project_dir)
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
        selected = None if project_dir is None else workspace_paths(project_dir).root
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
                rows.append(_source_details(candidate))
            except (OSError, WorkbenchError, KeyError):
                rows.append({"title": candidate.name, "path": str(candidate), "invalid": True})
        return rows

    def view(self) -> dict[str, Any]:
        selected = None
        if self.project_dir is not None:
            selected = {**_source_details(self.project_dir), **project_state(self.project_dir)}
        return {
            "storage_path": str(self.data_dir),
            "projects": self.projects(),
            "selected": selected,
        }

    def import_manuscript(self, payload: dict[str, Any]) -> "ProductApp":
        if set(payload) != {"filename", "content", "title"}:
            raise WorkbenchError("导入请求字段不完整")
        content = payload.get("content")
        if not isinstance(content, str):
            raise WorkbenchError("稿件内容必须是文本")
        title = payload.get("title")
        if title is not None and not isinstance(title, str):
            raise WorkbenchError("标题必须是文本")
        target = create_uploaded_project(
            self.data_dir,
            filename=str(payload.get("filename", "")),
            content=content.encode("utf-8"),
            title=title or None,
        )
        return replace(self, project_dir=target)

    def open_project(self, payload: dict[str, Any]) -> "ProductApp":
        if set(payload) != {"directory"} or not isinstance(payload.get("directory"), str):
            raise WorkbenchError("项目选择请求无效")
        name = str(payload["directory"])
        if Path(name).name != name or not name.endswith(".argument-workbench"):
            raise WorkbenchError("项目目录名无效")
        target = (self.data_dir / name).resolve()
        if target.parent != self.data_dir or target.is_symlink() or not target.is_dir():
            raise WorkbenchError("项目不在本地项目库中")
        errors = verify_project_versions(target)
        if errors:
            raise WorkbenchError("项目校验失败：" + "; ".join(errors))
        return replace(self, project_dir=target)

    def act(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.project_dir is None:
            raise WorkbenchError("请先创建或打开项目")
        action = payload.get("action")
        data = payload.get("data")
        if not isinstance(action, str) or not isinstance(data, dict) or set(payload) != {"action", "data"}:
            raise WorkbenchError("操作请求无效")
        if action == "import_report":
            report = data.get("report")
            source_name = data.get("source_name", "pasted-report.md")
            if not isinstance(report, str) or not isinstance(source_name, str):
                raise WorkbenchError("审查报告必须是文本")
            report_id = import_review_report(self.project_dir, report, source_name=source_name)
            prepare_atomization(self.project_dir, report_id)
        elif action == "collect_atomization":
            if not isinstance(data.get("response"), str): raise WorkbenchError("AI 返回必须是文本")
            collect_atomization_result(self.project_dir, data["response"])
        elif action == "decide_finding":
            append_quick_finding_decision(self.project_dir, str(data.get("finding_id", "")), decision=str(data.get("decision", "")), reason=str(data.get("reason", "")), corrections=data.get("corrections"), action_text=data.get("action_text"))
        elif action == "prepare_revision": prepare_revision_generation(self.project_dir)
        elif action == "collect_revision":
            if not isinstance(data.get("response"), str): raise WorkbenchError("AI 返回必须是文本")
            collect_revision_result(self.project_dir, data["response"])
        elif action == "decide_hunk":
            edited = data.get("edited_text")
            append_hunk_decision(self.project_dir, str(data.get("change_id", "")), decision=str(data.get("decision", "")), reason=str(data.get("reason", "")), edited_text=edited if isinstance(edited, str) else None)
        elif action == "apply_revision": apply_approved_hunks(self.project_dir)
        elif action == "prepare_resolution": prepare_resolution_review(self.project_dir)
        elif action == "collect_resolution":
            if not isinstance(data.get("response"), str): raise WorkbenchError("AI 返回必须是文本")
            collect_resolution_result(self.project_dir, data["response"])
        elif action == "decide_resolution":
            append_resolution_decision(self.project_dir, str(data.get("finding_id", "")), status=str(data.get("status", "")), reason=str(data.get("reason", "")))
        elif action == "export": export_revision(self.project_dir)
        else: raise WorkbenchError("未知操作")
        return self.view()


class ProductHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: ProductApp):
        self.app = app
        super().__init__(address, ProductRequestHandler)


class ProductRequestHandler(BaseHTTPRequestHandler):
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
        return secrets.compare_digest(self.headers.get("X-Argument-Workbench-Token", ""), self.server.app.token)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/":
            self._send(HTTPStatus.OK, render_product_shell(self.server.app.token).encode(), "text/html; charset=utf-8")
            return
        if path == "/api/state" and self._authorized():
            self._json(HTTPStatus.OK, self.server.app.view())
            return
        self._json(HTTPStatus.FORBIDDEN if path == "/api/state" else HTTPStatus.NOT_FOUND, {"error": "local UI token required" if path == "/api/state" else "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path not in {"/api/projects", "/api/open", "/api/action"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            self._json(HTTPStatus.FORBIDDEN, {"error": "local UI token required"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON required"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise WorkbenchError("请求大小无效")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise WorkbenchError("请求必须是对象")
            if path == "/api/projects":
                self.server.app = self.server.app.import_manuscript(payload)
                result = self.server.app.view()
            elif path == "/api/open":
                self.server.app = self.server.app.open_project(payload)
                result = self.server.app.view()
            else:
                result = self.server.app.act(payload)
            self._json(HTTPStatus.CREATED, result)
        except (UnicodeDecodeError, json.JSONDecodeError, WorkbenchError, OSError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


def render_product_shell(token: str) -> str:
    shell = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Argument Workbench</title><style>
body{margin:0;background:#f4f1eb;color:#23201b;font:16px system-ui,sans-serif}main{max-width:980px;margin:auto;padding:44px 24px}.brand{font-size:13px;letter-spacing:.16em;text-transform:uppercase;color:#735f3d}h1{font:42px Georgia,serif;margin:.3em 0}.card{background:#fff;border:1px solid #d9d1c4;border-radius:16px;padding:24px;margin:18px 0;box-shadow:0 8px 24px #352c1d0d}.next{border-left:5px solid #c28b2c}.muted{color:#6e675d}.error{color:#9a2f27}.warning{color:#8b5b0c}.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}label{display:block;margin:12px 0 6px;font-weight:600}input,button,textarea,select{font:inherit}input[type=text],textarea,select{width:100%;box-sizing:border-box;padding:11px;border:1px solid #bcb3a5;border-radius:8px}textarea{min-height:150px;resize:vertical}button{border:0;border-radius:8px;padding:11px 18px;background:#245a48;color:#fff;cursor:pointer}button.secondary{background:#e9e3d8;color:#332c22}button.danger{background:#8c352e}code{background:#eee8dd;padding:2px 5px;border-radius:4px}.quote{white-space:pre-wrap;background:#f7f3ec;border-radius:10px;padding:14px}.hunk{border-left:4px solid #557b6c}.original{background:#fff0ed}.replacement{background:#edf7f1}.pill{display:inline-block;border-radius:99px;background:#eee8dd;padding:4px 9px;margin:2px;font-size:13px}.hidden{display:none}@media(max-width:700px){.grid{grid-template-columns:1fr}}
</style></head><body><main><div class="brand">Local-first · model-neutral</div><h1>Argument Workbench</h1><p>普通 AI 给你一篇“改好了”的文章；这里让每一处修改都可追溯、由你批准，并能复查。</p><div id="app"></div></main><script>
const TOKEN=__TOKEN__,el=document.getElementById('app');let state;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Argument-Workbench-Token':TOKEN},body:body?JSON.stringify(body):undefined});const j=await r.json();if(!r.ok)throw Error(j.error);return j}
async function act(action,data={}){try{state=await api('/api/action',{action,data});render()}catch(e){const x=document.getElementById('err');if(x)x.textContent=e.message;else alert(e.message)}}
function copyText(value){navigator.clipboard.writeText(value).catch(()=>{});}
function errors(attempt){return attempt&&!attempt.valid?`<div class="error"><b>这次返回未通过校验，原始内容已保留。</b><ul>${attempt.errors.map(e=>`<li>${esc(e)}</li>`).join('')}</ul>${attempt.repair_prompt?'<button class="secondary" id="copyRepair">复制修复提示词</button>':''}</div>`:''}
function promptPaste(title,prompt,attempt,action){return `<div class="card"><h2>${esc(title)}</h2><p>复制提示词，到任意 AI 运行，再把完整返回粘贴回来。</p><p><button id="copyPrompt">复制提示词</button></p><textarea id="response" placeholder="在这里粘贴 AI 返回">${esc(attempt&&!attempt.valid?attempt.raw:'')}</textarea><p><button id="submitResponse">校验并保存返回</button></p>${errors(attempt)}<div id="err" class="error"></div></div>`}
function home(){el.innerHTML=`<div class="card"><h2>新建项目</h2><p class="muted">选择 Markdown/TXT 原稿。文件只保存在本机。</p><label>项目标题（可选）</label><input id="title" type="text"><label>原稿</label><input id="file" type="file" accept=".md,.txt,text/plain,text/markdown"><p><button id="create">导入为不可变 V1</button></p><div id="err" class="error"></div></div>${state.projects.length?`<div class="card"><h2>打开已有项目</h2>${state.projects.map(p=>`<p class="row"><button class="secondary open" data-dir="${esc(p.path.split(/[\\/]/).pop())}">打开</button><span>${esc(p.title)} · ${esc(p.current_version||'校验失败')}</span></p>`).join('')}</div>`:''}`;document.getElementById('create').onclick=async()=>{try{const f=document.getElementById('file').files[0];if(!f)throw Error('请选择稿件');state=await api('/api/projects',{filename:f.name,content:await f.text(),title:document.getElementById('title').value});render()}catch(e){document.getElementById('err').textContent=e.message}};document.querySelectorAll('.open').forEach(b=>b.onclick=async()=>{state=await api('/api/open',{directory:b.dataset.dir});render()})}
function bindPrompt(prompt,attempt,action){document.getElementById('copyPrompt').onclick=()=>copyText(prompt);document.getElementById('submitResponse').onclick=()=>act(action,{response:document.getElementById('response').value});const repair=document.getElementById('copyRepair');if(repair)repair.onclick=()=>copyText(attempt.repair_prompt)}
function render(){if(!state.selected){home();return}const p=state.selected;let body=`<div class="card"><div class="muted">当前项目 · ${esc(p.current_version)}</div><h2>${esc(p.title)}</h2><p>${esc(p.source_name)} · <code>${esc(p.source_sha256.slice(0,12))}</code></p></div><div class="card next"><div class="muted">唯一下一步</div><h2>${esc(p.next_action)}</h2><p>V1 永久保留；模型只能提案，决定权在你。</p></div>`;
if(p.stage==='review_material')body+=`<div class="card"><h2>导入现有审查报告</h2><p class="muted">支持任意格式。原始报告会永久归档。</p><textarea id="report" placeholder="粘贴 AI 审查报告"></textarea><p><button id="importReport">导入并生成原子化提示词</button></p><div id="err" class="error"></div></div>`;
else if(p.stage==='atomization_result')body+=promptPaste('把报告拆成可核验的发现',p.atomization_prompt,p.atomization_attempt,'collect_atomization');
else if(p.stage==='findings_confirm')body+=`<div class="card"><h2>逐条确认发现</h2><p>UNVERIFIED 不会被当成事实。可以直接修正定位、标准和建议动作。</p></div>${p.findings.map(f=>`<div class="card"><div class="row"><span class="pill">${esc(f.finding_id)}</span><span class="pill">${esc(f.claim_id)}</span><span class="pill">${esc(f.evidence_level)}</span></div><label>问题</label><textarea id="assert-${esc(f.finding_id)}">${esc(f.assertion)}</textarea><div class="grid"><div><label>原文定位</label><textarea id="quote-${esc(f.finding_id)}">${esc(f.manuscript_quote||'')}</textarea></div><div><label>审查标准</label><textarea id="criterion-${esc(f.finding_id)}">${esc(f.criterion)}</textarea></div></div><label>建议动作</label><textarea id="action-${esc(f.finding_id)}">${esc(f.suggested_action)}</textarea><label>你的理由</label><input id="reason-${esc(f.finding_id)}" type="text"><div class="row"><button class="finding" data-id="${esc(f.finding_id)}" data-decision="accept">接受处理</button><button class="finding danger" data-id="${esc(f.finding_id)}" data-decision="reject">拒绝</button><button class="finding secondary" data-id="${esc(f.finding_id)}" data-decision="defer">暂缓</button>${f.decision?`<span>当前：${esc(f.decision)}</span>`:''}</div></div>`).join('')}<div id="err" class="error"></div>`;
else if(p.stage==='revision_prepare')body+=`<div class="card"><h2>只为已接受的问题生成方案</h2><p>拒绝和暂缓的发现不会进入提示词。</p><button id="prepareRevision">生成受约束修改提示词</button><div id="err" class="error"></div></div>`;
else if(p.stage==='revision_result')body+=promptPaste('获取受约束修改提案',p.revision_prompt,p.revision_attempt,'collect_revision');
else if(p.stage==='hunk_review')body+=`<div class="card"><h2>逐项审批 diff</h2><p>每一项都显示 Finding、Action、理由和不确定项。</p></div>${p.hunks.map(h=>`<div class="card hunk"><div>${h.finding_ids.map(x=>`<span class="pill">Finding ${esc(x)}</span>`).join('')}${h.action_ids.map(x=>`<span class="pill">Action ${esc(x)}</span>`).join('')}</div><div class="grid"><div><h3>原文</h3><div class="quote original">${esc(h.original_quote||`插入锚点：${h.insertion_anchor}`)}</div></div><div><h3>建议</h3><textarea class="replacement" id="edit-${esc(h.change_id)}">${esc(h.replacement_text)}</textarea></div></div><p>${esc(h.reason)}</p>${h.uncertainties.length?`<p class="warning">未确认：${esc(h.uncertainties.join('；'))}</p>`:''}${h.fact_change?`<p class="warning">事实/引文变化，需核验：${esc(h.verification_note)}</p>`:''}<label>决定理由</label><input id="hreason-${esc(h.change_id)}" type="text"><div class="row"><button class="hunkDecision" data-id="${esc(h.change_id)}" data-decision="accept">接受</button><button class="hunkDecision danger" data-id="${esc(h.change_id)}" data-decision="reject">拒绝</button><button class="hunkDecision secondary" data-id="${esc(h.change_id)}" data-decision="edit">编辑后接受</button><button class="hunkDecision secondary" data-id="${esc(h.change_id)}" data-decision="regenerate">重新生成此项</button>${h.decision?`<span>当前：${esc(h.decision.decision)}</span>`:''}</div></div>`).join('')}<div id="err" class="error"></div>`;
else if(p.stage==='apply_revision')body+=`<div class="card"><h2>生成不可变 V2</h2><p>只应用已批准的 hunks；拒绝项绝不会进入 V2。哈希或范围冲突会安全停止。</p><button id="applyRevision">确定生成 V2</button><div id="err" class="error"></div></div>`;
else if(p.stage==='resolution_prepare')body+=`<div class="card"><h2>复查 V2</h2><p>复用每条 finding 的原始审查标准，不因“文字变了”就宣称已解决。</p><button id="prepareResolution">生成复查提示词</button><div id="err" class="error"></div></div>`;
else if(p.stage==='resolution_result')body+=promptPaste('用原标准复查 V2',p.resolution_prompt,p.resolution_attempt,'collect_resolution');
else if(p.stage==='resolution_confirm')body+=`<div class="card"><h2>确认复查结论</h2></div>${p.resolution_results.map(r=>`<div class="card"><span class="pill">${esc(r.finding_id)}</span><h3>${esc(r.proposed_status)}</h3><p>${esc(r.reason)}</p><label>最终状态</label><select id="status-${esc(r.finding_id)}"><option>resolved</option><option>partially_resolved</option><option>unresolved</option><option>not_evaluated</option></select><label>你的确认理由</label><input id="rreason-${esc(r.finding_id)}" type="text"><button class="resolution" data-id="${esc(r.finding_id)}">保存人工结论</button></div>`).join('')}<div id="err" class="error"></div>`;
else if(p.stage==='export')body+=`<div class="card"><h2>导出文章与审计记录</h2><button id="export">生成导出包</button><div id="err" class="error"></div></div>`;
else if(p.stage==='complete')body+=`<div class="card"><h2>闭环完成</h2><p>V2、修订清单和完整审计记录已生成。</p><p><code>${esc(p.export_path)}</code></p></div>`;
el.innerHTML=body+`<div class="card"><p class="muted">本地存储：${esc(state.storage_path)}</p></div>`;
if(p.stage==='review_material')document.getElementById('importReport').onclick=()=>act('import_report',{report:document.getElementById('report').value,source_name:'pasted-report.md'});
if(p.stage==='atomization_result')bindPrompt(p.atomization_prompt,p.atomization_attempt,'collect_atomization');
if(p.stage==='findings_confirm')document.querySelectorAll('.finding').forEach(b=>b.onclick=()=>{const id=b.dataset.id,decision=b.dataset.decision;act('decide_finding',{finding_id:id,decision,reason:document.getElementById('reason-'+id).value,action_text:document.getElementById('action-'+id).value,corrections:{assertion:document.getElementById('assert-'+id).value,manuscript_quote:document.getElementById('quote-'+id).value||null,criterion:document.getElementById('criterion-'+id).value,suggested_action:document.getElementById('action-'+id).value}})});
if(p.stage==='revision_prepare')document.getElementById('prepareRevision').onclick=()=>act('prepare_revision');
if(p.stage==='revision_result')bindPrompt(p.revision_prompt,p.revision_attempt,'collect_revision');
if(p.stage==='hunk_review')document.querySelectorAll('.hunkDecision').forEach(b=>b.onclick=()=>{const id=b.dataset.id,d=b.dataset.decision;act('decide_hunk',{change_id:id,decision:d,reason:document.getElementById('hreason-'+id).value,edited_text:d==='edit'?document.getElementById('edit-'+id).value:null})});
if(p.stage==='apply_revision')document.getElementById('applyRevision').onclick=()=>act('apply_revision');
if(p.stage==='resolution_prepare')document.getElementById('prepareResolution').onclick=()=>act('prepare_resolution');
if(p.stage==='resolution_result')bindPrompt(p.resolution_prompt,p.resolution_attempt,'collect_resolution');
if(p.stage==='resolution_confirm')document.querySelectorAll('.resolution').forEach(b=>b.onclick=()=>{const id=b.dataset.id;act('decide_resolution',{finding_id:id,status:document.getElementById('status-'+id).value,reason:document.getElementById('rreason-'+id).value})});
if(p.stage==='export')document.getElementById('export').onclick=()=>act('export');
}
api('/api/state').then(x=>{state=x;render()}).catch(e=>el.innerHTML=`<div class="card error">${esc(e.message)}</div>`)
</script></body></html>'''
    return shell.replace("__TOKEN__", json.dumps(token))


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
