"""Installed-runtime acceptance checks, independent of the source checkout."""

import base64
import hashlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import threading


def _pdf_fixture(stream: bytes | None = None) -> bytes:
    stream = stream if stream is not None else b"BT /F1 12 Tf 30 60 Td (Portable PDF test.) Tj ET"
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


def _fragmented_pdf_fixture() -> bytes:
    """Synthetic inverted text matrices: separate objects share each baseline."""
    lines = ("These are some short words drawn on the", "same line but stored in separate text objects.")
    ops = ["1 0 0 -1 0 100 cm", ".75 0 0 .75 0 0 cm"]
    for line_number, line in enumerate(lines):
        x, y = 10, 25 + line_number * 32
        for word in line.split():
            ops.append(f"BT /F1 9 Tf 1 0 0 -1 0 -1.45 Tm {x} {-y} Td ({word}) Tj ET")
            x += len(word) * 5 + 5
    return _pdf_fixture("\n".join(ops).encode("ascii"))


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
        _check_docx_visibility(root / "docx-visibility")
        checked.append("docx-visibility")
        _check_xml_limits(root / "xml-limits")
        checked.append("xml-limits")
        _check_table_grid(root / "table-grid")
        checked.append("table-grid")
        _check_docx_containers(root / "docx-containers")
        checked.append("docx-containers")
        _check_academic_protocols(root / "academic")
        checked.append("academic-quality-protocols")
        _check_readonly_recovery(root / "recovery")
        checked.append("readonly-recovery")
        project = DocumentReviewProject.create(root / "library", filename="draft.md", content=b"# Draft\n\nExample document.")
        project.confirm_extraction("confirm")
        project.confirm_context(ReviewContext(document_type="document", jurisdiction="unknown",
            effective_date="unknown", publisher_type="author", audience="editors").to_dict())
        project.run_local_prechecks(["expression_ambiguity"])
        reviewed_finding_id = _check_adversarial_pipeline(project)
        checked.append("adversarial-review")
        _check_review_context(project, reviewed_finding_id)
        checked.append("review-context")
        _check_resolution_pipeline(project, reviewed_finding_id)
        checked.append("revision-evidence")
        original_record = project.document_path.read_bytes()
        fresh = DocumentReviewProject.create(root / "library", filename="draft.md",
            content=b"# Draft\n\nExample document.", new_project=True)
        if (fresh.root == project.root or fresh.manifest()["project_id"] == project.manifest()["project_id"]
                or fresh.state()["extraction_state"] != "unconfirmed" or fresh.context() is not None
                or project.document_path.read_bytes() != original_record):
            raise RuntimeError("Fresh import reused or modified the reviewed project")
        checked.append("fresh-import")
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
            # This inverted-matrix fixture exercises pypdf's layout recovery.
            # The optional PyMuPDF backend takes precedence during ingestion
            # and clips this synthetic geometry differently; its presence must
            # not turn a pypdf-specific acceptance check into a false failure.
            import pypdf
            from document_review_ingest import _recover_pdf_layout
            page = pypdf.PdfReader(io.BytesIO(_fragmented_pdf_fixture())).pages[0]
            repaired, receipt = _recover_pdf_layout(page, (page.extract_text() or "").strip())
            expected = "These are some short words drawn on the same line but stored in separate text objects."
            if (" ".join(repaired.split()) != expected or len(repaired.splitlines()) > 5
                    or receipt.get("layout_recovery") != "recovered"):
                raise RuntimeError("Self-test PDF word-per-line recovery failed")
            checked.append("pdf-line-recovery")
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


def _check_readonly_recovery(library: Path) -> None:
    """A corrupt exchange must still expose the recovery view in an installed app."""
    from document_review_studio import DocumentReviewProject, ReviewStudioError
    from document_review_model import ReviewContext
    project = DocumentReviewProject.create(library, filename="recovery.md", content=b"A synthetic recovery document.")
    project.confirm_extraction("confirm")
    project.confirm_context(ReviewContext(document_type="document", jurisdiction="unknown", effective_date="unknown",
        publisher_type="author", audience="editors").to_dict())
    request = project.prepare_ai_audits(["expression_ambiguity"], provider="self-test", model="not-invoked")[0]
    path = project.root / "ai-requests" / request["request_id"] / "request.json"
    path.write_bytes(b"{truncated")
    view = project.view()
    if not view["state"].get("read_only") or view["can_review"] or view["ai_requests"] or view["findings"]:
        raise RuntimeError("Packaged app does not isolate a damaged review exchange")
    try:
        project.prepare_ai_audits(["expression_ambiguity"], provider="self-test", model="not-invoked")
    except ReviewStudioError:
        pass
    else:
        raise RuntimeError("Packaged app permits writes after exchange corruption")
    if path.read_bytes() != b"{truncated":
        raise RuntimeError("Packaged recovery silently rewrote a damaged exchange")


def _docx_visibility_fixture() -> bytes:
    """Small synthetic Word file with revisions and omitted note material."""
    import zipfile
    namespace = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr('word/document.xml', f'<w:document xmlns:w="{namespace}"><w:body><w:p><w:pPr><w:pPrChange w:id="1"><w:pPr/></w:pPrChange></w:pPr><w:r><w:t>Review the supplied claim only.</w:t><w:footnoteReference w:id="1"/><w:endnoteReference w:id="1"/></w:r></w:p></w:body></w:document>')
        for kind in ('footnote', 'endnote'):
            archive.writestr(f'word/{kind}s.xml', f'<w:{kind}s xmlns:w="{namespace}"><w:{kind} w:id="1"><w:p><w:r><w:t>SYNTHETIC-NOTE-NOT-EXTRACTED</w:t></w:r></w:p></w:{kind}></w:{kind}s>')
    return output.getvalue()


def _table_grid_fixture() -> bytes:
    """Valid minimal Word package with omitted columns and a merged cell."""
    import zipfile
    namespace = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    relation = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    body = ('<w:tbl><w:tblGrid>' + '<w:gridCol w:w="1000"/>' * 5 + '</w:tblGrid>'
        '<w:tr><w:trPr><w:gridBefore w:val="1"/><w:gridAfter w:val="1"/></w:trPr>'
        '<w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Unchanged</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>相关人员及时完成报名。</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('_rels/.rels', f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="document" Type="{relation}/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/document.xml', f'<w:document xmlns:w="{namespace}"><w:body>{body}<w:sectPr/></w:body></w:document>')
    return output.getvalue()


def _docx_container_fixture(*, special: bool = False) -> bytes:
    import zipfile
    namespace = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    relation = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    body = ('<w:sdt><w:sdtPr><w:alias w:val="contribution"/></w:sdtPr><w:sdtContent>'
        '<w:p><w:r><w:t>Visible contribution inside a control.</w:t></w:r></w:p>'
        '</w:sdtContent></w:sdt><w:p><w:r><w:t>Outside paragraph.</w:t></w:r></w:p>')
    if special:
        body += '<w:altChunk r:id="chunk"/>'
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(_table_grid_fixture())) as template, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in template.namelist():
            if name == 'word/document.xml':
                archive.writestr(name, f'<w:document xmlns:w="{namespace}" xmlns:r="{relation}"><w:body>{body}<w:sectPr/></w:body></w:document>')
            elif name == '[Content_Types].xml' and special:
                archive.writestr(name, template.read(name).decode().replace('</Types>', '<Default Extension="html" ContentType="text/html"/></Types>'))
            else:
                archive.writestr(name, template.read(name))
        if special:
            archive.writestr('word/_rels/document.xml.rels', f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="chunk" Type="{relation}/aFChunk" Target="import.html" TargetMode="Internal"/></Relationships>')
            archive.writestr('word/import.html', '<p>SYNTHETIC-EMBEDDED-NOT-EXTRACTED</p>')
    return output.getvalue()


def _check_docx_containers(library: Path) -> None:
    from copy import deepcopy
    from document_review_studio import DocumentReviewProject, ReviewStudioError
    from document_review_ingest import ingest_bytes
    from document_review_word import preserve_docx
    raw = _docx_container_fixture()
    original = ingest_bytes('control.docx', raw)
    if [block.text for block in original.blocks] != ['Visible contribution inside a control.', 'Outside paragraph.']:
        raise RuntimeError('Packaged Word import omits content controls')
    revised = deepcopy(original)
    revised.blocks[0].text = 'A clarified contribution.'
    native, report = preserve_docx(raw, original, revised)
    if not report['source_layout_preserved'] or ingest_bytes('native.docx', native).blocks[0].text != revised.blocks[0].text:
        raise RuntimeError('Packaged Word export loses a content control edit')
    raw = _docx_container_fixture(special=True)
    project = DocumentReviewProject.create(library, filename='special.docx', content=raw)
    if 'docx-special-content' not in [warning.code for warning in project.document().warnings]:
        raise RuntimeError('Packaged Word import hides unexpanded embedded content')
    try:
        project.confirm_extraction('confirm')
    except ReviewStudioError:
        pass
    else:
        raise RuntimeError('Packaged Word confirmation bypasses unexpanded content warning')
    if (project.root / 'source/special.docx').read_bytes() != raw or project.integrity_errors():
        raise RuntimeError('Packaged content-control import changed its original')


def _check_table_grid(library: Path) -> None:
    from document_review_studio import DocumentReviewProject
    from document_review_model import ReviewContext
    from document_review_ingest import ingest_bytes
    old, new = '相关人员及时完成报名。', '负责人周五前完成报名。'
    html = f'<table><tr><td colspan="2">Unchanged</td><td>{old}</td></tr></table>'.encode()
    for filename, raw, column in (('grid.html', html, 2), ('grid.docx', _table_grid_fixture(), 3)):
        project = DocumentReviewProject.create(library, filename=filename, content=raw)
        project.confirm_extraction('confirm')
        project.confirm_context(ReviewContext('document', 'unknown', 'unknown', 'author', 'readers').to_dict())
        project.run_local_prechecks(['expression_ambiguity'])
        target = next(block for block in project.document().blocks if block.kind == 'table_cell' and block.text == old)
        finding = next(item for item in project.findings() if item.location.block_id == target.block_id)
        for item in project.findings():
            project.decide_finding(item.finding_id, 'accept' if item.finding_id == finding.finding_id else 'reject', reason='Synthetic grid self-test')
        action = project.prepare_revision_plan()['actions'][0]
        project.set_revision_action_operation(action['action_id'], 'replace_table_cell', reason='Synthetic target-cell edit')
        hunk = project.propose_revision_hunk(action['action_id'], new, rationale='Synthetic clarification')
        project.decide_revision_hunk(hunk['hunk_id'], 'approve', reason='Synthetic explicit approval')
        output = project.finalize_revision()
        document = json.loads((output / 'document.json').read_text(encoding='utf-8'))
        rows = next(block['attrs']['rows'] for block in document['blocks'] if block['kind'] == 'table')
        if rows[0][column] != new or old in (output / '修改稿.md').read_text(encoding='utf-8'):
            raise RuntimeError('Packaged revision writes a table cell into the wrong grid column')
        if filename.endswith('.docx'):
            exported = project.export()
            native = ingest_bytes('checked.docx', (exported / '修改稿.docx').read_bytes())
            native_rows = next(block.attrs['rows'] for block in native.blocks if block.kind == 'table')
            if native_rows != [['', 'Unchanged', '', new, '']]:
                raise RuntimeError('Packaged Word export changes the wrong source cell')
        if (project.root / 'source' / filename).read_bytes() != raw or project.integrity_errors():
            raise RuntimeError('Packaged grid revision changed its immutable source')


def _check_xml_limits(library: Path) -> None:
    from dataclasses import replace
    from document_review_ingest import IngestionError, IngestionLimits, ingest_bytes
    from document_review_studio import DocumentReviewProject
    raw = _docx_visibility_fixture()
    for changes, message in (({"max_xml_elements": 1}, "单个部件"),
                             ({"max_xml_total_elements": 1}, "总解析"),
                             ({"max_xml_depth": 0}, "嵌套深度")):
        try:
            ingest_bytes('structure.docx', raw, limits=replace(IngestionLimits(), **changes))
        except IngestionError as error:
            if message not in str(error):
                raise RuntimeError("Packaged XML budget failed for an unrelated reason") from error
        else:
            raise RuntimeError("Packaged XML parser ignores its structural limits")
    project = DocumentReviewProject.create(library, filename='structure.docx', content=raw,
        limits=replace(IngestionLimits(), max_xml_elements=1))
    if (project.document_path.exists() or project.state()['extraction_state'] != 'blocked'
            or (project.root / 'source/structure.docx').read_bytes() != raw):
        raise RuntimeError("Packaged XML rejection loses the source or leaves partial IR")
    fresh = DocumentReviewProject.create(library, filename='structure.docx', content=raw, new_project=True)
    if fresh.document() is None or fresh.integrity_errors():
        raise RuntimeError("An XML budget leaked into a separate ingestion")


def _check_docx_visibility(library: Path) -> None:
    from document_review_studio import DocumentReviewProject, ReviewStudioError
    from document_review_model import ReviewContext
    project = DocumentReviewProject.create(library, filename="visibility.docx", content=_docx_visibility_fixture())
    expected = {"unaccepted-revisions", "footnotes-present", "endnotes-present"}
    if not expected <= {warning.code for warning in project.document().warnings}:
        raise RuntimeError("Packaged Word recognition misses revisions or omitted notes")
    try:
        project.confirm_extraction("confirm")
    except ReviewStudioError:
        pass
    else:
        raise RuntimeError("Packaged Word recognition bypasses its warning gate")
    project.confirm_extraction("continue_with_warning")
    project.confirm_context(ReviewContext('academic article', 'unknown', 'unknown', 'author', 'researchers',
        review_profile='academic', discipline='humanities', research_type='theoretical').to_dict())
    request = project.prepare_ai_audits(['academic_citations'], provider='self-test', model='not-invoked')[0]
    contract = json.loads(request['prompt'].split('```json\n', 1)[1].split('\n```', 1)[0])
    if not expected <= {row['code'] for row in contract['source_visibility']['extraction_warnings']}:
        raise RuntimeError("Packaged Word visibility limits do not reach the saved prompt")
    if 'SYNTHETIC-NOTE-NOT-EXTRACTED' in request['prompt'] or project.integrity_errors():
        raise RuntimeError("Packaged Word prompt misrepresents its visible source")


def _check_academic_protocols(library: Path) -> None:
    """Verify packaged, persisted academic standards without invoking a model."""
    from document_review_studio import DocumentReviewProject
    from document_review_model import ReviewContext
    project = DocumentReviewProject.create(library, filename="paper.md",
        content=b"# Interpretation\n\nThis argument concerns the supplied passage only.")
    project.confirm_extraction("confirm")
    project.confirm_context(ReviewContext(document_type="paper", jurisdiction="unknown",
        effective_date="unknown", publisher_type="author", audience="researchers",
        review_profile="academic", discipline="humanities", research_type="theoretical").to_dict())
    requests = project.prepare_ai_audits(provider="self-test", model="not-invoked")
    required = {"academic_argument": "question-contribution",
                "academic_methods": "interpretive-warrant", "academic_citations": "source-entailment"}
    if {row["critic"] for row in requests} != set(required):
        raise RuntimeError("Packaged academic critic routing is incomplete")
    for row in requests:
        if '"source_visibility"' not in row["prompt"] or '"extraction_warnings"' not in row["prompt"]:
            raise RuntimeError("Packaged review omits extraction visibility limits")
        protocol = row["critic_protocol"]
        quality = protocol.get("scholarly_quality", {})
        criteria = quality.get("criteria", [])
        structure = protocol.get("argument_structure", {})
        if (protocol.get("version") != 4 or quality.get("version") != 4
                or structure.get("version") != 2 or not structure.get("checks")
                or structure.get("reference", {}).get("snapshot_sha256") != "cffe1595dfb116f4d02a1476e4aece76169eeadbadb1bb8a708376b9a7efab28"
                or required[row["critic"]] not in {item.get("id") for item in criteria}
                or not quality.get("sources")
                or any(not item.get("when") or not item.get("guard") for item in criteria)):
            raise RuntimeError("Packaged academic quality standards are missing or stale")
        restored = project._snapshotted_critic_protocol(row, row["prompt"].encode("utf-8"))
        if restored != protocol:
            raise RuntimeError("Packaged academic prompt differs from its saved protocol")
        if row["critic"] == "academic_argument":
            from document_review_composition import validate_argument_assessment
            if row.get("argument_assessment_protocol", {}).get("response_fields") != ["version", "review_frame", "dimensions"]:
                raise RuntimeError("Packaged academic argument design review is missing")
            response = json.loads(row["prompt"].rsplit("```json\n", 1)[1].split("```", 1)[0])
            overview = response["argument_assessment"]
            seeds = row["argument_assessment_protocol"].get("dimension_seeds", {})
            if len(overview["dimensions"]) != 1 or overview["dimensions"][0]["id"] in seeds:
                raise RuntimeError("Packaged review template prescribes the example angles")
            block = project.document().blocks[-1]
            overview["review_frame"]["evidence"] = [{"block_id": block.block_id, "quote": block.text}]
            shape = overview["dimensions"][0]
            overview["dimensions"] = [dict(shape, id=identity, title=title,
                evidence=[{"block_id": block.block_id, "quote": block.text}]) for identity, title in (
                    ("interpretive-scope", "Interpretive scope"), ("reader-orientation", "Reader orientation"))]
            if validate_argument_assessment(overview, {block.block_id: block}):
                raise RuntimeError("Packaged argument design review evidence cannot validate")
            response.update(findings=[], zero_finding_basis=["Synthetic check of the supplied passage only."])
            project.collect_model_audit("academic_argument", json.dumps(response),
                provider="self-test", model="not-invoked", request_id=row["request_id"])
            saved = DocumentReviewProject(project.root).view()["argument_assessments"]
            if len(saved) != 1 or saved[0]["assessment"] != overview:
                raise RuntimeError("Packaged review cannot persist article-specific angles")
    if project.integrity_errors():
        raise RuntimeError("Packaged academic protocol provenance failed verification")


def _check_review_context(project, finding_id: str) -> None:
    """A stale page cannot replace a newer decision, even with unchanged text."""
    from document_review_ui import StudioApp
    from studio_ui_state import UIStateConflict
    app = StudioApp.create(project.root.parent, project.root)
    view = app.view()
    payload = {"action": "decide_finding", "request_id": "selftest-review-12345678",
        "project_directory": project.root.name, "review_context": view["review_context"],
        "document_scope": view["selected"]["ui_draft"]["scope"],
        "data": {"finding_id": finding_id, "decision": "reject", "reason": "Synthetic concurrency check"}}
    app.act(payload, require_context=True)
    try:
        app.act({**payload, "request_id": "selftest-stale-12345678",
            "data": {**payload["data"], "decision": "accept"}}, require_context=True)
    except UIStateConflict:
        pass
    else:
        raise RuntimeError("Packaged app allows a stale review decision")
    app.act(payload, require_context=True)
    if project._decisions()[finding_id]["decision"] != "reject" or len(project._decision_records()[finding_id]) != 1:
        raise RuntimeError("Packaged request replay changed a human decision")


def _check_adversarial_pipeline(project) -> str:
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
    return run.findings[0].finding_id


def _check_resolution_pipeline(project, finding_id: str) -> None:
    """Persist a source-grounded recheck and carry it to the right revised block."""
    from document_review_studio import ReviewStudioError
    project.decide_finding(finding_id, "accept", reason="Synthetic self-test decision")
    action = project.prepare_revision_plan()["actions"][0]
    project.set_revision_action_operation(action["action_id"], "replace_block", reason="Synthetic replacement")
    hunk = project.propose_revision_hunk(action["action_id"], "The editor checks the revised passage.", rationale="Synthetic repair")
    project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="Synthetic approval")
    revision_dir = project.finalize_revision()
    revision = json.loads((revision_dir / "revision.json").read_text(encoding="utf-8"))
    request = project.external_recheck_requests(revision["revision_id"])[0]
    block = next(item for item in json.loads((revision_dir / "document.json").read_text(encoding="utf-8"))["blocks"]
                 if item["text"] == "The editor checks the revised passage.")
    payload = {key: request[key] for key in ("request_id", "prompt_sha256", "revision_id", "revised_sha256", "critic")}
    resolution = {"finding_id": finding_id, "state": "still-present", "reason": "Synthetic pending issue",
                  "evidence": "Synthetic explanation, not a quotation",
                  "source_evidence": [{"block_id": block["block_id"], "quote": "A fabricated excerpt"}]}
    payload.update(resolutions=[resolution], new_findings=[])
    try:
        project.collect_external_recheck(revision["revision_id"], request["critic"], json.dumps(payload), provider="self-test", model="fixture")
    except ReviewStudioError:
        pass
    else:
        raise RuntimeError("Packaged recheck accepted fabricated evidence")
    if project._external_recheck_results(revision["revision_id"]):
        raise RuntimeError("Rejected recheck left a published result")
    resolution["source_evidence"][0]["quote"] = block["text"]
    result = project.collect_external_recheck(revision["revision_id"], request["critic"], json.dumps(payload), provider="self-test", model="fixture")
    if result["resolution_evidence_receipt"]["protocol_version"] != 1 or project.external_recheck_status(revision["revision_id"])["complete"]:
        raise RuntimeError("Packaged recheck did not preserve evidence binding or human authority")
    project.decide_external_resolution(revision["revision_id"], result["result_id"], finding_id, "unresolved", reason="Synthetic followup decision")
    followup = project.start_followup_round(revision["revision_id"])
    carried = followup["findings"][0]
    if (carried["location"]["block_id"] != block["block_id"] or carried["evidence"] != block["text"]
            or project.integrity_errors()):
        raise RuntimeError("Packaged followup lost its revised evidence anchor")


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
    required = ["shell.html", "theme.css", "styles.css", *SCRIPTS,
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
    if "Protocol: argument-ir-extraction-v3" not in paths.prompt.read_text(encoding="utf-8"):
        raise RuntimeError("Packaged IR extraction protocol is stale")
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
