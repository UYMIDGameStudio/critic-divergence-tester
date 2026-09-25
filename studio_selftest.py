"""Installed-runtime acceptance checks, independent of the source checkout."""

import base64
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import threading


def _pdf_fixture() -> bytes:
    stream = b"BT /F1 12 Tf 30 60 Td (Portable PDF test.) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    data, offsets = b"%PDF-1.4\n", [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return data + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()


def run_self_test() -> dict:
    from cli.core import load_protocol
    from document_review_studio import DocumentReviewProject
    from document_review_model import ReviewContext
    from project_lifecycle import create_backup, restore_backup
    from unified_app import serve_unified_app

    load_protocol("critic-social-science")
    checked = ["protocols", "project", "review", "backup", "restore", "http", "browser-assets"]
    _check_shell_assets()
    checked.extend(["research-browser-assets", "professional-browser-assets", "ui-locales"])
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _check_language_imports(root / "languages")
        checked.append("eight-language-import")
        project = DocumentReviewProject.create(root / "library", filename="draft.md", content=b"# Draft\n\nExample document.")
        project.confirm_extraction("confirm")
        project.confirm_context(ReviewContext(document_type="document", jurisdiction="unknown",
            effective_date="unknown", publisher_type="author", audience="editors").to_dict())
        project.run_local_prechecks(["expression_ambiguity"])
        _check_adversarial_pipeline(project)
        checked.append("adversarial-review")
        research_project = _check_research_pipeline(root / "library")
        checked.extend(["big5-research-import", "research-ir", "research-product-view", "research-workbench"])
        backup = create_backup(project.root, root / "backup.zip")
        restored = DocumentReviewProject(restore_backup(backup, root / "restored"))
        if restored.integrity_errors():
            raise RuntimeError("Self-test restored project failed integrity verification")
        if importlib.util.find_spec("pypdf"):
            pdf = DocumentReviewProject.create(root / "library", filename="smoke.pdf", content=_pdf_fixture())
            if not pdf.document() or "Portable PDF test." not in pdf.document().plain_text:
                raise RuntimeError("Self-test could not extract text from the bundled PDF adapter")
            checked.append("pdf-text")
        elif getattr(sys, "frozen", False):
            raise RuntimeError("Portable bundle is missing the PDF text adapter")
        if importlib.util.find_spec("pypdfium2"):
            from document_review_pdf_render import open_pdf_renderer
            with open_pdf_renderer(_pdf_fixture()) as renderer:
                png = renderer.render_page(1)
                if renderer.name != "pypdfium2" or renderer.page_count != 1 or not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) < 100:
                    raise RuntimeError("Self-test could not render a page with bundled PDFium")
            checked.append("pdf-page-render")
        elif getattr(sys, "frozen", False):
            raise RuntimeError("Portable bundle is missing the PDF renderer")
        server, _ = serve_unified_app(data_dir=root / "library", project_dir=project.root, open_browser=False)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            paths = ("/", "/api/state", "/research/api/open", "/research/",
                     "/research/api/state", "/research/professional", "/research/api/professional/view")
            for path in paths:
                client = http.client.HTTPConnection(*server.server_address, timeout=10)
                try:
                    headers = {"X-Document-Review-Token": server.app.token,
                               "X-Argument-Workbench-Token": server.app.token}
                    if path == "/research/api/open":
                        headers["Content-Type"] = "application/json"
                        client.request("POST", path, json.dumps({"directory": research_project.name}), headers)
                    else:
                        client.request("GET", path, headers=headers)
                    response = client.getresponse()
                    payload = response.read()
                    if response.status not in ({200, 201} if path == "/research/api/open" else {200}):
                        raise RuntimeError(f"Self-test HTTP request failed: {path} {response.status}")
                    if path in {"/", "/research/", "/research/professional"}:
                        kind = "studio" if path == "/" else "professional" if path.endswith("professional") else "product"
                        _check_rendered_shell(kind, payload.decode("utf-8"))
                        if kind != "studio" and "/research/api/" not in payload.decode("utf-8"):
                            raise RuntimeError("Self-test research HTTP routes are incomplete")
                    elif path == "/research/api/professional/view":
                        _check_research_projection(json.loads(payload))
                    elif not json.loads(payload).get("selected"):
                        raise RuntimeError("Self-test selected project is missing")
                finally:
                    client.close()
        finally:
            server.shutdown()
            worker.join(timeout=10)
            server.server_close()
        checked.append("research-http")
    return {"passed": True, "checked": checked}


def _check_adversarial_pipeline(project) -> None:
    """Prove packaged modules can persist all stages; no model quality claim."""
    from document_review_adversarial import ENVELOPE_FIELDS, response_example
    document = project._review_document_record()[1]
    block = document.blocks[-1]
    request = project.prepare_ai_audits(["expression_ambiguity"], provider="self-test", model="fixture")[0]
    finding = {
        "finding_id": "SELFTEST", "critic": "expression_ambiguity", "document_type": "document",
        "location": {"block_id": block.block_id}, "evidence": block.text,
        "issue": "Synthetic challenge", "standard": "Synthetic test criterion",
        "consequence": "Synthetic consequence", "severity": "low", "verification_state": "model-proposed",
        "external_basis": {}, "uncertainties": [], "suggested_action": "Inspect the source",
        "suggested_owner": "author", "blocks_release_or_execution": False,
    }
    response = {key: request[key] for key in ("request_id", "prompt_sha256", "provider", "model", "source_sha256", "critic")}
    response["findings"] = [finding]
    run = project.collect_model_audit("expression_ambiguity", json.dumps(response),
                                      provider="self-test", model="fixture", request_id=request["request_id"])
    session = project.prepare_adversarial_review(run.findings[0].finding_id, provider="self-test", model="defender")
    for stage in ("defense", "assessment"):
        if stage == "assessment":
            session = project.prepare_adversarial_assessment(session["session_id"], provider="self-test", model="assessor")
        request = session["requests"][-1]
        result = response_example(stage)
        result["context_evidence"][0].update(block_id=block.block_id, quote=block.text)
        payload = {**{key: request[key] for key in ENVELOPE_FIELDS}, "result": result}
        session = project.collect_adversarial_response(session["session_id"], json.dumps(payload), request_id=request["request_id"])
    if session["status"] != "completed" or project.findings()[0].status != "open" or project.integrity_errors():
        raise RuntimeError("Packaged adversarial workflow failed or changed human decision authority")


def _check_language_imports(library: Path) -> None:
    from document_review_studio import DocumentReviewProject
    samples = [("English review", "utf-8"), ("简体中文导入测试", "gb18030"),
               ("繁體中文匯入測試", "big5"), ("Grüße für die Straße", "cp1252"),
               ("Élève, français et cœur", "cp1252"), ("日本語の文書を解析します", "cp932"),
               ("Проверка русского текста", "cp1251"), ("Līngua Latīna: æquus et œconomia", "utf-16")]
    for index, (text, encoding) in enumerate(samples):
        project = DocumentReviewProject.create(library, filename=f"language-{index}.txt", content=text.encode(encoding), encoding=encoding)
        if not project.document() or project.document().plain_text != text:
            raise RuntimeError(f"Language import self-test failed: {index} {encoding}")


def _check_rendered_shell(kind: str, shell: str) -> None:
    """Validate delivered resources without requiring Node or a live browser.

    Error messages contain only fixed check identifiers, never HTML, a token,
    a manuscript or a model response. Real browser behavior has separate tests.
    """
    if (re.search(r"__[A-Z][A-Z0-9_]*__", shell)
            or "function render()" not in shell
            or not re.search(r"<style>\s*\S.+?</style>", shell, re.DOTALL)
            or not re.search(r'<html\b[^>]*\blang=[\"\']zh-Hant[\"\']', shell)):
        raise RuntimeError("Self-test browser shell resources are incomplete")
    variable = "UI_MESSAGES" if kind == "studio" else "RESEARCH_MESSAGES"
    marker = re.search(r"\bconst\s+" + variable + r"\s*=\s*", shell)
    try:
        if marker is None:
            raise ValueError
        messages, _ = json.JSONDecoder().raw_decode(shell[marker.end():])
        groups = [messages] if kind == "studio" else [messages["text"], messages["codes"]]
        for group in groups:
            if not isinstance(group, dict) or not group:
                raise ValueError
            for value in group.values():
                if not isinstance(value, dict) or any(
                    not isinstance(value.get(locale), str) or not value[locale].strip()
                    for locale in ("zh-Hant", "en")
                ):
                    raise ValueError
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("Self-test browser translation dictionary is incomplete") from None
    if kind != "studio" and not all(value in shell for value in (
        'id="research-language"', 'value="zh-Hant"', 'value="en"',
        "function applyResearchLanguage()", "function renderResearchView()",
    )):
        raise RuntimeError("Self-test research language controls are incomplete")


def _check_shell_assets() -> None:
    from importlib.resources import files
    from argument_app import render_product_shell
    from argument_ui import render_app_shell
    from document_review_ui import render_studio_shell
    from studio_web import SCRIPTS

    # Resolve from the installed package, not from cwd or a source-tree path.
    resources = files("studio_web")
    required = ["shell.html", "styles.css", *SCRIPTS,
                "locales-part-a.json", "locales-part-b.json", "locales-system.json",
                "research_i18n.js", "locales-research.json"]
    required.extend("research_" + kind + suffix for kind in ("product", "professional")
                    for suffix in (".html", ".css", ".js"))
    for name in required:
        try:
            content = resources.joinpath(name).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise RuntimeError("Self-test packaged browser resource is missing") from None
        if not content.strip():
            raise RuntimeError("Self-test packaged browser resource is empty")
    for kind, renderer in (("studio", render_studio_shell), ("product", render_product_shell),
                           ("professional", render_app_shell)):
        _check_rendered_shell(kind, renderer("runtime-self-test-token"))


_RESEARCH_TITLE = "繁體研究稿"
_RESEARCH_CLAIM = "資料顯示兩個案例具有相同結果。"
_RESEARCH_SOURCE = "# " + _RESEARCH_TITLE + "\n\n" + _RESEARCH_CLAIM + "\n"


def _check_research_projection(value: dict) -> None:
    if (value.get("project", {}).get("title") != _RESEARCH_TITLE
            or [row.get("text") for row in value.get("manuscript", [])] != _RESEARCH_SOURCE.splitlines()
            or not any(row.get("source_quote") == _RESEARCH_CLAIM for row in value.get("claims", []))):
        raise RuntimeError("Self-test Big5 research workbench text did not round-trip")


def _check_research_pipeline(library: Path) -> Path:
    from argument_app import ProductApp
    from argument_workbench import collect_raw_attempt, rebuild_workspace, verify_project_versions, workspace_paths

    source = _RESEARCH_SOURCE.encode("big5")
    app = ProductApp.create(library).import_manuscript({
        "filename": "research-big5.md", "title": _RESEARCH_TITLE,
        "content_base64": base64.b64encode(source).decode("ascii"), "encoding": "big5",
    })
    view = app.view().get("selected") or {}
    if view.get("stage") == "read_only" or view.get("title") != _RESEARCH_TITLE:
        raise RuntimeError("Self-test Big5 research product could not open")
    project = app.project_dir
    if project is None:
        raise RuntimeError("Self-test research project was not created")
    paths = workspace_paths(project)
    archived_source = paths.version_dir / "source" / "research-big5.md"
    if archived_source.read_bytes() != source:
        raise RuntimeError("Self-test research import did not preserve original bytes")
    ir = {
        "schema_version": 1, "artifact": "argument-ir", "scope": "social-science",
        "source": {"name": "research-big5.md", "sha256": hashlib.sha256(source).hexdigest()},
        "claims": [{"id": "C1", "text": _RESEARCH_CLAIM, "source_quote": _RESEARCH_CLAIM,
                    "position": f"L3:C1-L3:C{len(_RESEARCH_CLAIM)}", "types": ["descriptive"],
                    "methods": ["descriptive-empirical"], "role": "conclusion",
                    "extraction": "explicit", "uncertainty": ""}],
        "evidence": [], "assumptions": [], "citations": [], "relations": [], "unverified": [],
    }
    _, attempt = collect_raw_attempt(project, json.dumps(ir, ensure_ascii=False).encode("utf-8"),
                                     method="file", source_name="self-test-ir.json", producer_label="synthetic-runtime-fixture")
    if attempt.get("validation", {}).get("status") != "valid":
        raise RuntimeError("Self-test Big5 research IR did not validate")
    rebuild_workspace(project)
    if verify_project_versions(project):
        raise RuntimeError("Self-test research project integrity check failed")
    selected = app.view().get("selected") or {}
    if selected.get("stage") == "read_only" or not selected.get("professional_available"):
        raise RuntimeError("Self-test reviewed research project could not open")
    _check_research_projection(app.professional_view())
    return project
