from __future__ import annotations

import io
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import document_review_ingest
from document_review_ingest import IngestionError, ingest_bytes, safe_upload_name
from document_review_model import CRITIC_DIMENSIONS, DocumentBlock, DocumentLocation, ExternalBasis, ExtractionWarning, Finding, QualitySignals, RawFileBinding, StructuredDocument, stable_id
from document_review_studio import DocumentReviewProject, ReviewStudioError
from document_review_ui import StudioApp, render_studio_shell


def _docx(*, revised: bool = False) -> bytes:
    revision = '<w:ins w:id="1"><w:r><w:t>inserted</w:t></w:r></w:ins>' if revised else ""
    body = f'''<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>活动方案</w:t></w:r></w:p><w:p><w:r><w:t>相关人员完成报名</w:t></w:r>{revision}</w:p><w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>合并单元格</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:sectPr/></w:body></w:document>'''
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"/>")
        archive.writestr("word/document.xml", body)
        archive.writestr("word/settings.xml", "<w:settings xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"/>")
    return output.getvalue()


def _simple_pdf(text: str = "Hello PDF") -> bytes:
    stream = b"BT /F1 12 Tf 72 720 Td (" + text.encode("ascii") + b") Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode())
        data.extend(body)
        data.extend(b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(data)


class DocumentReviewStudioTests(unittest.TestCase):
    def context(self) -> dict[str, object]:
        return {
            "document_type": "活动策划案",
            "jurisdiction": "中国大陆",
            "effective_date": "2026-09-01",
            "publisher_type": "社会组织",
            "audience": "参与者",
            "involves_minors": True,
            "involves_fees": True,
            "involves_sponsorship": False,
            "involves_contract": False,
            "involves_personal_information": True,
            "involves_intellectual_property": False,
            "publication_status": "internal-draft",
        }

    def model_finding(self, project: DocumentReviewProject, critic: str, source_id: str, issue: str) -> dict[str, object]:
        block = project.document().blocks[0]
        return {
            "finding_id": source_id,
            "critic": critic,
            "document_type": "活动策划案",
            "location": block.location.to_dict(),
            "evidence": block.text,
            "issue": issue,
            "standard": "结果必须有可定位、可复核的判断标准",
            "consequence": "错误关联会让人工裁决采用过期或跨维度结论",
            "severity": "medium",
            "verification_state": "model-proposed",
            "external_basis": {"jurisdiction": "中国大陆", "unresolved_facts": []},
            "uncertainties": [],
            "suggested_action": "核对当前请求后再处理",
            "suggested_owner": "文档负责人",
            "blocks_release_or_execution": False,
        }

    def test_text_model_has_stable_block_and_table_cell_locations(self) -> None:
        value = ingest_bytes("plan.md", "# 计划\n\n| 项目 | 金额 |\n| --- | --- |\n| 场地 | 100 |\n".encode())
        self.assertTrue(value.blocks)
        self.assertEqual(value.source.extension, ".md")
        self.assertTrue(any(block.kind == "table_cell" and block.location.column == 1 for block in value.blocks))
        again = ingest_bytes("plan.md", "# 计划\n\n| 项目 | 金额 |\n| --- | --- |\n| 场地 | 100 |\n".encode())
        self.assertEqual([block.block_id for block in value.blocks], [block.block_id for block in again.blocks])

    def test_ui_labels_preview_local_precheck_ai_import_and_corrected_action(self) -> None:
        shell = render_studio_shell("test-token")
        self.assertIn("experimental preview", shell)
        self.assertIn("运行选中的本地预检", shell)
        self.assertIn("导出 / 导入独立 AI 审查", shell)
        self.assertIn("普通 JSON（人工关联，较弱审计）", shell)
        self.assertIn("默认只展示前", shell)
        self.assertIn("逐项修改与批准", shell)
        self.assertIn("生成修改稿并复审", shell)
        self.assertIn("外部 critic 复审与人工 Resolution", shell)
        self.assertIn("删除本地项目", shell)
        self.assertIn("一键修复可自动修复项", shell)
        self.assertIn("导出五份独立协议", shell)
        self.assertIn("模型原始 JSON 响应", shell)
        self.assertIn("抽取内容与定位预览", shell)
        self.assertIn("人工修正动作", shell)

    def test_builtin_pdf_fallback_preserves_page_location(self) -> None:
        with patch("document_review_ingest._pdf_backend", return_value=None):
            document = ingest_bytes("notice.pdf", _simple_pdf())
        self.assertTrue(any(block.location.page == 1 for block in document.blocks))
        self.assertIn("Hello PDF", document.plain_text)

    def test_real_pdf_backend_parses_valid_text_pdf_when_installed(self) -> None:
        if document_review_ingest._pdf_backend() is None:
            self.skipTest("optional PDF backend is not installed")
        document = ingest_bytes("notice.pdf", _simple_pdf())
        self.assertIn("Hello PDF", document.plain_text)
        self.assertFalse(any(w.code == "scan-pages-detected" for w in document.warnings))

    def test_short_text_pdf_is_not_routed_to_ocr(self) -> None:
        if document_review_ingest._pdf_backend() is None:
            self.skipTest("optional PDF backend is not installed")
        document = ingest_bytes("short.pdf", _simple_pdf("Short notice"))
        self.assertIn("Short notice", document.plain_text)
        self.assertFalse(any(w.code in {"scan-pages-detected", "ocr-unavailable"} for w in document.warnings))

    def test_mock_ocr_fixture_covers_scanned_page_flow(self) -> None:
        source = RawFileBinding("scan.pdf", ".pdf", "application/pdf", 9, "a" * 64, "source/scan.pdf")
        scan = StructuredDocument(stable_id("DOC", source.sha256), "scan", source, "mock-pdf", "1", [], [ExtractionWarning("scan-pages-detected", "high", "scan", details={"pages": [1]})], QualitySignals(page_count=1, blank_pages=[1]), [], {"pdf_kind": "scanned"})

        class OCR:
            name = "mock-ocr"
            version = "1"
            def available(self): return True, "ready"
            def recognize_pdf_page(self, image, *, page_number, language):
                return {"text": "OCR fixture text", "confidence": 99, "low_confidence_words": 0, "engine": self.name, "engine_version": self.version, "language": language}

        class Page:
            def get_pixmap(self, **kwargs): return SimpleNamespace(tobytes=lambda kind: b"png")
        fake_fitz = SimpleNamespace(open=lambda **kwargs: [Page()], Matrix=lambda x, y: (x, y))
        with patch("document_review_ingest._pdf_text", return_value=scan), patch.dict("sys.modules", {"fitz": fake_fitz}):
            document = ingest_bytes("scan.pdf", b"%PDF-x\n", ocr=OCR())
        self.assertIn("OCR fixture text", document.plain_text)
        self.assertTrue(any(w.code == "ocr-used" for w in document.warnings))
        self.assertEqual(document.quality.blank_pages, [])
        self.assertLessEqual(document.quality.text_coverage, 1.0)

    def test_builtin_pdf_fallback_limits_decompressed_streams(self) -> None:
        compressed = zlib.compress(b"A" * 100_000)
        malicious = b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\nstream\n" + compressed + b"\nendstream\n%%EOF"
        limits = document_review_ingest.IngestionLimits(max_pdf_stream_uncompressed_bytes=1024, max_pdf_total_uncompressed_bytes=2048)
        with patch("document_review_ingest._pdf_backend", return_value=None), self.assertRaises(IngestionError):
            ingest_bytes("large-stream.pdf", malicious, limits=limits)

    def test_docx_revisions_are_blocking_and_table_structure_is_retained(self) -> None:
        document = ingest_bytes("plan.docx", _docx(revised=True))
        self.assertTrue(any(w.code == "unaccepted-revisions" for w in document.warnings))
        self.assertTrue(any(block.kind == "table" for block in document.blocks))
        self.assertTrue(any(block.kind == "table_cell" and block.attrs.get("grid_span") == 2 for block in document.blocks))
        with self.assertRaises(IngestionError):
            safe_upload_name("../plan.docx")

    def test_docx_export_is_a_new_file_and_does_not_claim_track_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="clean.docx", content=_docx())
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            project.run_audits(["official_professional_format"])
            for finding in project.findings():
                project.decide_finding(finding.finding_id, "reject", reason="只验证规范化副本导出")
            output = project.export()
            normalized = output / "normalized-editable-copy.docx"
            self.assertTrue(normalized.is_file())
            self.assertFalse((output / "revised.docx").exists())
            with zipfile.ZipFile(normalized) as archive:
                self.assertIn("word/document.xml", archive.namelist())
            capability = json.loads((output / "track-changes-capability.json").read_text(encoding="utf-8"))
            self.assertFalse(capability["native_track_changes"])
            self.assertFalse(capability["revised_document_ready"])
            self.assertEqual(project.integrity_errors(), [])
            with self.assertRaises(ReviewStudioError):
                project.export(revised_markdown="# Materially changed\n")

    def test_quality_gate_context_five_independent_critics_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="活动.md", content="# 活动通知\n\n请相关人员适时报名。\n活动涉及收费和未成年人。\n".encode())
            with self.assertRaises(ReviewStudioError):
                project.run_audits()
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            runs = project.run_audits(CRITIC_DIMENSIONS)
            self.assertEqual({run.critic for run in runs}, set(CRITIC_DIMENSIONS))
            findings = project.findings()
            self.assertTrue(findings)
            for finding in findings:
                self.assertTrue(finding.location.block_id)
                self.assertTrue(finding.evidence)
                self.assertIn("external_basis", finding.to_dict())
            for index, finding in enumerate(findings):
                decision = "accept" if index == 0 else "reject"
                project.decide_finding(finding.finding_id, decision, reason="完成正式导出前的逐项裁决")
            bridge = project.prepare_revision_bridge()
            self.assertTrue(bridge.is_file())
            export = project.export()
            self.assertTrue((export / "audit.json").is_file())
            self.assertTrue((export / "audit-package.zip").is_file())
            export_rows = project.view()["exports"]
            self.assertTrue(any(row["kind"] == "export" and any(file["name"] == "audit-package.zip" for file in row["files"]) for row in export_rows))
            self.assertIsNone(__import__("json").loads((export / "audit.json").read_text(encoding="utf-8"))["scores"])
            self.assertEqual(project.integrity_errors(), [])

    def test_local_precheck_does_not_treat_negated_terms_as_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="活动.md", content="# 活动方案\n\n但没有负责人、预算依据或验收指标。\n本活动不涉及收费，也没有申诉渠道。\n".encode())
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            project.run_local_prechecks(["execution_feasibility", "compliance_legal_screen", "reasonableness_governance"])
            findings = project.findings()
            execution_issues = {item.issue for item in findings if item.critic == "execution_feasibility"}
            self.assertIn("执行模型缺少负责人", execution_issues)
            self.assertIn("执行模型缺少预算依据", execution_issues)
            self.assertIn("方案没有可验证的验收指标", execution_issues)
            self.assertFalse(any(item.critic == "compliance_legal_screen" for item in findings))
            governance = [item for item in findings if item.critic == "reasonableness_governance"]
            self.assertTrue(governance)
            self.assertIn("申诉渠道", governance[0].issue)

    def test_decision_chain_uses_sequence_and_previous_hash_not_filename_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.md", content="# 活动\n\n相关人员适时报名。\n".encode())
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            project.run_local_prechecks(["expression_ambiguity"])
            finding = project.findings()[0]
            cycle = (("accept", None), ("reject", None), ("correct", "由报名负责人在三个工作日内书面通知名单"))
            records = []
            for index in range(30):
                decision, action = cycle[index % len(cycle)]
                records.append(project.decide_finding(finding.finding_id, decision, reason=f"round {index + 1}", corrected_action=action))
            self.assertEqual([row["sequence"] for row in records], list(range(1, 31)))
            self.assertIsNone(records[0]["previous_decision_sha256"])
            self.assertTrue(all(row["previous_decision_sha256"] for row in records[1:]))
            self.assertEqual(project._decisions()[finding.finding_id]["decision"], "correct")
            self.assertEqual(project.findings()[0].status, "correct")

    def test_intermediate_artifact_tampering_and_policy_deletion_force_read_only(self) -> None:
        for target in ("document", "finding", "decision", "context", "policy"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp_dir:
                project = DocumentReviewProject.create(temp_dir, filename="draft.md", content="# 活动\n\n相关人员适时报名。\n".encode())
                project.confirm_extraction("confirm")
                project.confirm_context(self.context())
                project.run_local_prechecks(["expression_ambiguity"])
                finding = project.findings()[0]
                project.decide_finding(finding.finding_id, "accept", reason="initial")
                if target == "document":
                    value = json.loads(project.document_path.read_text(encoding="utf-8")); value["title"] = "tampered"
                    project.document_path.write_text(json.dumps(value), encoding="utf-8")
                elif target == "finding":
                    path = project._finding_artifact_path(finding.finding_id)
                    value = json.loads(path.read_text(encoding="utf-8")); value["findings"][0]["issue"] = "tampered"; value["findings"][0]["suggested_action"] = "tampered"
                    path.write_text(json.dumps(value), encoding="utf-8")
                elif target == "decision":
                    path = next((project.root / "finding-decisions").glob("*.json"))
                    value = json.loads(path.read_text(encoding="utf-8")); value["reason"] = "tampered"
                    path.write_text(json.dumps(value), encoding="utf-8")
                elif target == "context":
                    path = project.root / "context.json"
                    value = json.loads(path.read_text(encoding="utf-8")); value["audience"] = "tampered"
                    path.write_text(json.dumps(value), encoding="utf-8")
                else:
                    (project.root / "integrity-policy.json").unlink()
                view = project.view()
                self.assertTrue(view["state"]["read_only"])
                self.assertTrue(view["state"]["integrity_errors"])
                with self.assertRaises(ReviewStudioError):
                    project.decide_finding(finding.finding_id, "reject", reason="must not write")

    def test_context_cannot_be_confirmed_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            with self.assertRaises(ReviewStudioError):
                project.confirm_context(self.context())

    def test_state_tampering_cannot_unlock_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            state = project.state()
            state["extraction_state"] = "confirmed"
            state["context_state"] = "confirmed"
            project.state_path.write_bytes(json.dumps(state).encode("utf-8"))
            self.assertEqual(project.integrity_errors(), [])
            self.assertEqual(project.state()["extraction_state"], "unconfirmed")
            self.assertFalse(project.can_review()[0])
            with self.assertRaises(ReviewStudioError):
                project.run_local_prechecks(["expression_ambiguity"])
            self.assertFalse(project.state()["read_only"])

    def test_extraction_decision_binds_current_document_quality_and_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            project.confirm_extraction("confirm")
            decision_path = next((project.root / "extraction-decisions").glob("*.json"))
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
            self.assertEqual(decision["document_relative_path"], "extraction/document.json")
            self.assertEqual(decision["document_sha256"], hashlib.sha256(project.document_path.read_bytes()).hexdigest())
            quality_path = project.root / "extraction" / "quality.json"
            warnings_path = project.root / "extraction" / "warnings.json"
            self.assertEqual(decision["quality_sha256"], hashlib.sha256(quality_path.read_bytes()).hexdigest())
            self.assertEqual(decision["warnings_sha256"], hashlib.sha256(warnings_path.read_bytes()).hexdigest())
            project.document_path.write_bytes(project.document_path.read_bytes() + b"\n")
            self.assertTrue(any("latest extraction decision is not bound" in error for error in project.integrity_errors()))

    def test_state_is_rebuilt_from_authoritative_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            tampered = project.state()
            tampered["extraction_state"] = "blocked"
            tampered["context_state"] = "missing"
            project.state_path.write_bytes(json.dumps(tampered).encode("utf-8"))
            rebuilt = project.state()
            self.assertEqual(rebuilt["extraction_state"], "confirmed")
            self.assertEqual(rebuilt["context_state"], "confirmed")
            self.assertEqual(json.loads(project.state_path.read_text(encoding="utf-8")), rebuilt)

    def test_audit_log_tampering_forces_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            (project.root / "audit-log.jsonl").write_text("forged or truncated\n", encoding="utf-8")
            view = project.view()
            self.assertTrue(view["state"]["read_only"])
            self.assertTrue(any("audit-log.jsonl" in error for error in view["state"].get("integrity_errors", [])))

    def _delete_artifact_and_receipt(self, project: DocumentReviewProject, path: Path) -> None:
        path.unlink()
        receipt = path.parent / ".integrity" / f"{path.name}.json"
        receipt.unlink()

    def test_integrity_index_prevents_paired_deletion_from_rolling_back_history(self) -> None:
        for target in ("decision", "audit", "raw-response", "bridge", "export"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp_dir:
                project = DocumentReviewProject.create(temp_dir, filename="draft.md", content="# 活动\n\n相关人员适时报名。\n".encode())
                project.confirm_extraction("confirm")
                project.confirm_context(self.context())
                project.run_local_prechecks(["expression_ambiguity"])
                finding = project.findings()[0]
                if target == "decision":
                    project.decide_finding(finding.finding_id, "accept", reason="round 1")
                    project.decide_finding(finding.finding_id, "reject", reason="round 2")
                    decision_paths = list((project.root / "finding-decisions").glob("*.json"))
                    latest = max(decision_paths, key=lambda path: json.loads(path.read_text(encoding="utf-8"))["sequence"])
                    self._delete_artifact_and_receipt(project, latest)
                elif target == "audit":
                    audit_path = next((project.root / "audits" / "expression_ambiguity").glob("*.json"))
                    protocol_path = next((project.root / "audits" / "expression_ambiguity").glob("*.local-precheck-protocol.md"))
                    self._delete_artifact_and_receipt(project, audit_path)
                    self._delete_artifact_and_receipt(project, protocol_path)
                elif target == "raw-response":
                    request = project.prepare_ai_audits(["expression_ambiguity"], provider="example-provider", model="example-model")[0]
                    payload = {"request_id": request["request_id"], "prompt_sha256": request["prompt_sha256"], "provider": request["provider"], "model": request["model"], "critic": request["critic"], "source_sha256": project.document().source.sha256, "findings": [], "zero_finding_basis": ["independent pass"]}
                    run = project.collect_model_audit(request["critic"], json.dumps(payload), provider=request["provider"], model=request["model"], request_id=request["request_id"])
                    raw_path = project.root / "audits" / request["critic"] / f"{run.run_id}.raw-response.json.txt"
                    self._delete_artifact_and_receipt(project, raw_path)
                elif target == "bridge":
                    project.decide_finding(finding.finding_id, "accept", reason="bridge")
                    self._delete_artifact_and_receipt(project, project.prepare_revision_bridge())
                else:
                    project.decide_finding(finding.finding_id, "reject", reason="export gate")
                    output = project.export()
                    shutil.rmtree(output)
                view = project.view()
                self.assertTrue(view["state"]["read_only"])
                self.assertTrue(any("integrity index artifact missing" in error for error in view["state"].get("integrity_errors", [])))

    def test_ai_response_requires_request_envelope_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            request = project.prepare_ai_audits(["expression_ambiguity"], provider="example-provider", model="example-model")[0]
            payload = {"critic": request["critic"], "source_sha256": project.document().source.sha256, "findings": []}
            with self.assertRaises(ReviewStudioError):
                project.collect_model_audit(request["critic"], json.dumps(payload), provider=request["provider"], model=request["model"], request_id=request["request_id"])
            run = project.collect_model_audit(request["critic"], json.dumps(payload), provider=request["provider"], model=request["model"], request_id=request["request_id"], binding_mode="manual_association")
            run_path = project.root / "audits" / request["critic"] / f"{run.run_id}.json"
            saved = json.loads(run_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["response_binding"]["mode"], "manual-association")
            self.assertFalse(saved["response_binding"]["request_echo_verified"])
            self.assertEqual(saved["declared_model_metadata"]["response_binding"], "manual-association")

    def test_local_project_can_be_deleted_only_from_project_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            app = StudioApp.create(temp_dir)
            deleted = app.delete_project(project.root.name)
            self.assertFalse(project.root.exists())
            self.assertIsNone(deleted.project)
            with self.assertRaises(ReviewStudioError):
                app.delete_project("..\\outside.document-review-studio")

    def test_environment_repair_action_returns_to_dependency_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch("document_review_ui.repair_dependencies") as repair:
            app = StudioApp.create(temp_dir)
            result = app.repair_environment()
            repair.assert_called_once_with(None)
            self.assertIn("dependencies", result.view())

    def test_python_dependency_repair_uses_current_interpreter(self) -> None:
        completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("document_review_ingest.subprocess.run", return_value=completed) as run, patch("document_review_ingest.importlib.import_module"):
            document_review_ingest.repair_dependency("pypdf")
        pip_calls = [call.args[0] for call in run.call_args_list if call.args and call.args[0][:3] == [sys.executable, "-m", "pip"]]
        self.assertEqual(len(pip_calls), 1)
        command = pip_calls[0]
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1:3], ["-m", "pip"])
        self.assertEqual(command[3:6], ["install", "--disable-pip-version-check", "--no-input"])
        self.assertEqual(command[6], "pypdf>=5.0,<7.0")

    def test_independent_ai_protocol_export_and_import_preserve_invocation_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            requests = project.prepare_ai_audits(CRITIC_DIMENSIONS, provider="example-provider", model="example-model")
            self.assertEqual(len(requests), 5)
            self.assertEqual(len({row["prompt_sha256"] for row in requests}), 5)
            self.assertTrue(all(row["provider"] == "example-provider" and row["model"] == "example-model" for row in requests))
            selected = requests[0]
            payload = {"request_id": selected["request_id"], "prompt_sha256": selected["prompt_sha256"], "provider": selected["provider"], "model": selected["model"], "critic": selected["critic"], "source_sha256": project.document().source.sha256, "findings": [], "zero_finding_basis": ["independent pass"]}
            run = project.collect_model_audit(selected["critic"], json.dumps(payload), provider="example-provider", model="example-model", request_id=selected["request_id"])
            run_path = project.root / "audits" / selected["critic"] / f"{run.run_id}.json"
            saved = json.loads(run_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["declared_model_metadata"]["prompt_sha256"], selected["prompt_sha256"])
            self.assertEqual(saved["declared_model_metadata"]["provider"], "example-provider")
            self.assertEqual(saved["declared_model_metadata"]["import_mode"], "manual")
            self.assertNotIn("model_invocation", saved)
            self.assertTrue((run_path.parent / f"{run.run_id}.raw-response.json.txt").is_file())
            self.assertEqual(project.integrity_errors(), [])

    def test_corrected_action_is_used_by_revision_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.md", content="# 活动\n\n相关人员适时报名。\n".encode())
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            project.run_local_prechecks(["expression_ambiguity"])
            finding = project.findings()[0]
            corrected = "由报名负责人在三个工作日内书面通知完整名单"
            project.decide_finding(finding.finding_id, "correct", reason="人工细化动作", corrected_action=corrected)
            report = project.prepare_revision_bridge().read_text(encoding="utf-8")
            self.assertIn(f"Human-approved action: {corrected}", report)
            self.assertIn("Original suggested action:", report)
            self.assertEqual(project.integrity_errors(), [])

    def test_finding_action_hunk_revision_recheck_and_export_close_the_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.md", content="# 活动\n\n相关人员适时报名。\n".encode())
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            project.run_local_prechecks(["expression_ambiguity"])
            findings = project.findings()
            self.assertTrue(findings)
            for finding in findings:
                project.decide_finding(finding.finding_id, "accept", reason="进入受约束修改")
            plan = project.prepare_revision_plan()
            self.assertTrue(plan["actions"])
            for action in plan["actions"]:
                hunk = project.propose_revision_hunk(
                    action["action_id"],
                    "报名负责人须在 2026 年 9 月 1 日前书面通知符合条件的参与者。",
                    rationale="明确责任主体、对象和期限",
                    provenance="ai-assisted-manual-import",
                )
                project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="逐段 diff 与任务要求一致")
            revision = project.finalize_revision()
            self.assertTrue((revision / "修改稿.md").is_file())
            self.assertTrue((revision / "修改说明.md").is_file())
            self.assertTrue((revision / "未解决风险.md").is_file())
            recheck = json.loads((revision / "recheck.json").read_text(encoding="utf-8"))
            self.assertTrue(recheck["local_critic_runs"])
            self.assertTrue(all(row["state"] in {"resolved", "partially-resolved", "still-present", "new-finding"} for row in recheck["finding_resolutions"]))
            output = project.export()
            self.assertTrue((output / "修改稿.docx").is_file())
            self.assertTrue((output / "修改说明.md").is_file())
            self.assertTrue((output / "未解决风险.md").is_file())
            audit = json.loads((output / "audit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["revision"]["revision_id"], json.loads((revision / "revision.json").read_text(encoding="utf-8"))["revision_id"])
            self.assertEqual(project.integrity_errors(), [])

    def test_rejected_hunk_is_not_applied_and_remains_an_unresolved_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.md", content="# 活动\n\n相关人员适时报名。\n".encode())
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            project.run_local_prechecks(["expression_ambiguity"])
            for finding in project.findings():
                project.decide_finding(finding.finding_id, "accept", reason="问题成立")
            plan = project.prepare_revision_plan()
            for action in plan["actions"]:
                hunk = project.propose_revision_hunk(action["action_id"], "不应进入修改稿的文本", rationale="候选方案")
                project.decide_revision_hunk(hunk["hunk_id"], "reject", reason="候选文本引入新的歧义")
            revision = project.finalize_revision()
            self.assertNotIn("不应进入修改稿的文本", (revision / "修改稿.md").read_text(encoding="utf-8"))
            unresolved = (revision / "未解决风险.md").read_text(encoding="utf-8")
            self.assertIn("still-present", unresolved)
            self.assertIn("对应 Hunk 被人工拒绝", unresolved)

    def test_governance_partial_fix_remains_in_unresolved_risks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="policy.md", content="# 治理规则\n\n违规行为将被处分。\n".encode())
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            project.run_local_prechecks(["reasonableness_governance"])
            finding = project.findings()[0]
            self.assertEqual(finding.check_id, "governance.required_controls")
            self.assertEqual(len(finding.check_data["items"]), 5)
            project.decide_finding(finding.finding_id, "accept", reason="治理缺口成立")
            action = project.prepare_revision_plan()["actions"][0]
            hunk = project.propose_revision_hunk(action["action_id"], "## 申诉\n参与者可以提交申诉。", rationale="本轮仅补申诉渠道")
            project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="先应用已核实部分")
            revision = project.finalize_revision()
            recheck = json.loads((revision / "recheck.json").read_text(encoding="utf-8"))
            resolution = next(row for row in recheck["finding_resolutions"] if row["finding_id"] == finding.finding_id)
            self.assertEqual(resolution["state"], "partially-resolved")
            self.assertIn("复议渠道", resolution["basis"])
            risks = (revision / "未解决风险.md").read_text(encoding="utf-8")
            self.assertIn("partially-resolved", risks)
            self.assertNotIn("没有未解决项", risks)

    def test_recheck_new_finding_is_never_dropped_from_risk_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="notice.md", content="第一段。\n\n第二段。\n".encode())
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            project.run_local_prechecks(["expression_ambiguity"])
            finding = project.findings()[0]
            self.assertEqual(finding.check_id, "expression.document_purpose")
            project.decide_finding(finding.finding_id, "accept", reason="需要标题")
            action = project.prepare_revision_plan()["actions"][0]
            hunk = project.propose_revision_hunk(action["action_id"], "# 适时通知", rationale="加入标题")
            project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="批准标题")
            revision = project.finalize_revision()
            recheck = json.loads((revision / "recheck.json").read_text(encoding="utf-8"))
            new_rows = [row for row in recheck["finding_resolutions"] if row["state"] == "new-finding"]
            self.assertEqual(len(new_rows), 1)
            self.assertEqual(new_rows[0]["check_id"], "expression.ambiguous_term:适时")
            self.assertIn(new_rows[0]["finding_id"], (revision / "未解决风险.md").read_text(encoding="utf-8"))

    def test_revision_is_invalidated_by_any_current_decision_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="plan.md", content="# 活动\n\n开始执行。\n".encode())
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            project.run_local_prechecks(["execution_feasibility"])
            findings = project.findings()
            accepted, rejected = findings[0], findings[1:]
            project.decide_finding(accepted.finding_id, "accept", reason="纳入修改")
            for finding in rejected:
                project.decide_finding(finding.finding_id, "reject", reason="本轮不处理")
            action = project.prepare_revision_plan()["actions"][0]
            hunk = project.propose_revision_hunk(action["action_id"], "## 责任\n负责人：项目经理。", rationale="补负责人")
            project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="批准")
            project.finalize_revision()
            project.decide_finding(rejected[0].finding_id, "defer", reason="改为后续处理")
            self.assertIsNone(project.revision_plan())
            self.assertIsNone(project.revision_workspace()["revision"])
            output = project.export()
            audit = json.loads((output / "audit.json").read_text(encoding="utf-8"))
            self.assertIsNone(audit["revision"])
            self.assertFalse((output / "修改稿.docx").exists())

    def test_revision_actions_preserve_distinct_work_groups_and_operations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="plan.md", content="# 活动\n\n相关人员参加。\n".encode())
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            project.run_local_prechecks(["expression_ambiguity", "execution_feasibility", "reasonableness_governance", "official_professional_format"])
            findings = project.findings()
            self.assertEqual(len(findings), 6)
            for finding in findings:
                project.decide_finding(finding.finding_id, "accept", reason="分别处理")
            queue = project.finding_work_groups()
            plan = project.prepare_revision_plan()
            self.assertEqual(len(plan["actions"]), queue["total_groups"])
            self.assertEqual(len({row["work_group_id"] for row in plan["actions"]}), len(plan["actions"]))
            self.assertIn("insert_after", {row["operation"] for row in plan["actions"]})
            self.assertIn("append_section", {row["operation"] for row in plan["actions"]})

    def test_finalize_recheck_failure_leaves_no_revision_and_retry_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.md", content="# 活动\n\n相关人员报名。\n".encode())
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            project.run_local_prechecks(["expression_ambiguity"])
            finding = project.findings()[0]
            project.decide_finding(finding.finding_id, "accept", reason="问题成立")
            action = project.prepare_revision_plan()["actions"][0]
            hunk = project.propose_revision_hunk(action["action_id"], "明确名单内参与者报名。", rationale="消除歧义")
            project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="批准")
            original = project._deterministic_audit
            with patch.object(project, "_deterministic_audit", side_effect=RuntimeError("simulated recheck crash")):
                with self.assertRaises(RuntimeError):
                    project.finalize_revision()
            revisions = project.root / "revisions"
            self.assertFalse(revisions.exists() and any(path.is_dir() for path in revisions.iterdir()))
            with patch.object(project, "_deterministic_audit", side_effect=original):
                revision = project.finalize_revision()
            self.assertTrue((revision / "revision.json").is_file())
            self.assertEqual(project.integrity_errors(), [])

    def test_correct_decision_requires_nonblank_bounded_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.md", content="# 活动\n\n相关人员报名。\n".encode())
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            project.run_local_prechecks(["expression_ambiguity"])
            finding_id = project.findings()[0].finding_id
            for value in (None, "   ", 42):
                with self.assertRaises(ReviewStudioError):
                    project.decide_finding(finding_id, "correct", reason="修正", corrected_action=value)
            with self.assertRaises(ReviewStudioError):
                project.decide_finding(finding_id, "correct", reason="修正", corrected_action="字" * 100_001)

    def test_external_critic_recheck_requires_import_and_human_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.md", content="# 活动\n\n原始内容。\n".encode())
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            critic = "execution_feasibility"
            request = project.prepare_ai_audits([critic], provider="example-provider", model="example-model")[0]
            finding_payload = self.model_finding(project, critic, "MODEL-F-1", "执行责任不明确")
            response = {
                "request_id": request["request_id"],
                "prompt_sha256": request["prompt_sha256"],
                "provider": request["provider"],
                "model": request["model"],
                "critic": critic,
                "source_sha256": project.document().source.sha256,
                "findings": [finding_payload],
            }
            project.collect_model_audit(critic, json.dumps(response, ensure_ascii=False), provider=request["provider"], model=request["model"], request_id=request["request_id"])
            finding = project.findings()[0]
            project.decide_finding(finding.finding_id, "accept", reason="接受外部批评")
            action = project.prepare_revision_plan()["actions"][0]
            hunk = project.propose_revision_hunk(action["action_id"], "负责人：项目经理。", rationale="明确负责人")
            project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="批准")
            revision_dir = project.finalize_revision()
            revision = json.loads((revision_dir / "revision.json").read_text(encoding="utf-8"))
            status = project.external_recheck_status(revision["revision_id"])
            self.assertFalse(status["complete"])
            external_request = status["requests"][0]
            self.assertIsNone(external_request["result"])
            recheck_response = {
                "request_id": external_request["request_id"],
                "prompt_sha256": external_request["prompt_sha256"],
                "revision_id": revision["revision_id"],
                "revised_sha256": revision["revised_sha256"],
                "critic": critic,
                "resolutions": [{"finding_id": finding.finding_id, "state": "resolved", "reason": "负责人已经明确", "evidence": "负责人：项目经理。"}],
                "new_findings": [],
            }
            result = project.collect_external_recheck(revision["revision_id"], critic, json.dumps(recheck_response, ensure_ascii=False))
            status = project.external_recheck_status(revision["revision_id"])
            self.assertFalse(status["complete"])
            self.assertIsNotNone(status["requests"][0]["result"])
            project.decide_external_resolution(revision["revision_id"], result["result_id"], finding.finding_id, "resolved", reason="人工核对修改稿后确认")
            self.assertTrue(project.external_recheck_status(revision["revision_id"])["complete"])
            output = project.export()
            audit = json.loads((output / "audit.json").read_text(encoding="utf-8"))
            self.assertTrue(audit["external_recheck"]["complete"])
            risks = (output / "未解决风险.md").read_text(encoding="utf-8")
            self.assertNotIn(finding.finding_id, risks)
            self.assertEqual(project.integrity_errors(), [])

    def test_attention_queue_groups_only_same_location_and_action_without_losing_critics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            findings: list[Finding] = []
            for index in range(35):
                critic = CRITIC_DIMENSIONS[index % len(CRITIC_DIMENSIONS)]
                group_number = 0 if index < 2 else index
                findings.append(Finding(
                    finding_id=f"F-{index}", critic=critic, document_type="测试文档",
                    location=DocumentLocation(f"B-{group_number}", "paragraph"), evidence="证据",
                    issue="影响执行链", standard="必须可执行", consequence="可能改变决策",
                    severity="high" if index == 0 else "medium", verification_state="model-proposed",
                    external_basis=ExternalBasis(), uncertainties=[], suggested_action="补充负责人" if index < 2 else f"动作 {index}",
                    suggested_owner="负责人", blocks_release_or_execution=index == 0,
                ))
            queue = project.finding_work_groups(findings)
            self.assertEqual(queue["default_limit"], 30)
            self.assertEqual(queue["total_groups"], 34)
            self.assertEqual(queue["hidden_groups"], 4)
            first = queue["groups"][0]
            self.assertEqual(first["finding_count"], 2)
            self.assertEqual(first["critic_count"], 2)
            self.assertEqual({item["finding_id"] for item in first["findings"]}, {"F-0", "F-1"})
            self.assertIn("阻断发布或执行", first["priority_reasons"])

    def test_pdf_dependency_failure_is_saved_without_fake_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="scan.pdf", content=b"%PDF-1.7\nnot a real pdf")
            self.assertEqual(project.state()["extraction_state"], "blocked")
            self.assertFalse(project.view()["can_review"])
            self.assertTrue((project.root / "extraction" / "diagnostic.json").is_file())

    def test_model_response_is_hash_bound_and_tampering_forces_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            block = project.document().blocks[0]
            request = project.prepare_ai_audits(["expression_ambiguity"], provider="example-provider", model="example-model")[0]
            payload = {
                "request_id": request["request_id"],
                "prompt_sha256": request["prompt_sha256"],
                "provider": request["provider"],
                "model": request["model"],
                "critic": "expression_ambiguity",
                "source_sha256": project.document().source.sha256,
                "findings": [{
                    "finding_id": "F-model-1",
                    "critic": "expression_ambiguity",
                    "document_type": "活动策划案",
                    "location": block.location.to_dict(),
                    "evidence": block.text,
                    "issue": "模型提出待核对的表达问题",
                    "standard": "表达应唯一可理解",
                    "consequence": "可能造成执行分歧",
                    "severity": "low",
                    "verification_state": "model-proposed",
                    "external_basis": {"jurisdiction": "中国大陆", "unresolved_facts": []},
                    "uncertainties": [],
                    "suggested_action": "补充定义",
                    "suggested_owner": "文档负责人",
                    "blocks_release_or_execution": False,
                }],
            }
            run = project.collect_model_audit("expression_ambiguity", json.dumps(payload, ensure_ascii=False), provider="example-provider", model="example-model", request_id=request["request_id"])
            self.assertEqual(len(run.findings), 1)
            source = project.root / "source" / "draft.txt"
            source.write_bytes(b"tampered\n")
            view = project.view()
            self.assertTrue(view["state"]["read_only"])
            with self.assertRaises(ReviewStudioError):
                project.decide_finding("F-model-1", "accept", reason="not allowed after tamper")

    def test_empty_context_is_rejected_and_export_has_formal_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            with self.assertRaises(ReviewStudioError):
                project.export()
            project.confirm_extraction("confirm")
            empty = self.context()
            empty["jurisdiction"] = "   "
            with self.assertRaises(ReviewStudioError):
                project.confirm_context(empty)
            project.confirm_context(self.context())
            with self.assertRaises(ReviewStudioError):
                project.export()
            project.run_local_prechecks(["execution_feasibility"])
            with self.assertRaises(ReviewStudioError):
                project.export()
            for finding in project.findings():
                project.decide_finding(finding.finding_id, "reject", reason="formal completion gate")
            self.assertTrue(project.export().is_dir())

    def test_active_ai_requests_and_latest_runs_do_not_depend_on_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            first = project.prepare_ai_audits(CRITIC_DIMENSIONS, provider="provider", model="model")
            second = project.prepare_ai_audits(CRITIC_DIMENSIONS, provider="provider", model="model")
            active = project.ai_requests()
            self.assertEqual(len(first), 5)
            self.assertEqual(len(second), 5)
            self.assertEqual(len(active), 5)
            self.assertTrue(all(row["request_sequence"] == 2 for row in active))
            selected = next(row for row in active if row["critic"] == "expression_ambiguity")
            payload = {"request_id": selected["request_id"], "prompt_sha256": selected["prompt_sha256"], "provider": selected["provider"], "model": selected["model"], "critic": selected["critic"], "source_sha256": project.document().source.sha256, "findings": [self.model_finding(project, selected["critic"], "same-source-id", "latest issue")], "zero_finding_basis": []}
            project.collect_model_audit(selected["critic"], json.dumps(payload, ensure_ascii=False), provider=selected["provider"], model=selected["model"], request_id=selected["request_id"])
            rows = [row for row in project.findings() if row.critic == selected["critic"]]
            self.assertEqual([row.issue for row in rows], ["latest issue"])
            self.assertEqual(rows[0].source_finding_id, "same-source-id")

    def test_same_source_finding_id_from_two_critics_remains_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            requests = project.prepare_ai_audits(["expression_ambiguity", "execution_feasibility"], provider="provider", model="model")
            for request in requests:
                payload = {"request_id": request["request_id"], "prompt_sha256": request["prompt_sha256"], "provider": request["provider"], "model": request["model"], "critic": request["critic"], "source_sha256": project.document().source.sha256, "findings": [self.model_finding(project, request["critic"], "F-1", f"issue for {request['critic']}")], "zero_finding_basis": []}
                project.collect_model_audit(request["critic"], json.dumps(payload, ensure_ascii=False), provider=request["provider"], model=request["model"], request_id=request["request_id"])
            findings = project.findings()
            self.assertEqual(len(findings), 2)
            self.assertEqual(len({finding.finding_id for finding in findings}), 2)
            self.assertEqual({finding.source_finding_id for finding in findings}, {"F-1"})

    def test_concurrent_decisions_keep_integrity_index_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.md", content="# 活动\n\n相关人员适时报名。\n".encode())
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            project.run_local_prechecks(["expression_ambiguity"])
            finding_id = project.findings()[0].finding_id
            choices = ["accept", "reject", "correct", "defer"] * 5
            with ThreadPoolExecutor(max_workers=8) as executor:
                records = list(executor.map(lambda pair: project.decide_finding(finding_id, pair[1], reason=f"concurrent {pair[0]}", corrected_action="人工动作" if pair[1] == "correct" else None), enumerate(choices)))
            self.assertEqual(sorted(record["sequence"] for record in records), list(range(1, 21)))
            self.assertEqual(project.integrity_errors(), [])

    def test_complete_audit_package_and_tampered_download_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            project.confirm_extraction("confirm")
            project.confirm_context(self.context())
            request = project.prepare_ai_audits(["expression_ambiguity"], provider="provider", model="model")[0]
            payload = {"request_id": request["request_id"], "prompt_sha256": request["prompt_sha256"], "provider": request["provider"], "model": request["model"], "critic": request["critic"], "source_sha256": project.document().source.sha256, "findings": [], "zero_finding_basis": ["independent pass"]}
            run = project.collect_model_audit(request["critic"], json.dumps(payload), provider=request["provider"], model=request["model"], request_id=request["request_id"])
            output = project.export()
            package = output / "audit-package.zip"
            with zipfile.ZipFile(package) as archive:
                names = set(archive.namelist())
            self.assertIn("package-manifest.json", names)
            self.assertTrue(any(name.startswith("project/source/") for name in names))
            self.assertTrue(any(name.endswith(f"{run.run_id}.raw-response.json.txt") for name in names))
            self.assertTrue(any(name.endswith("integrity-index.json") for name in names))
            audit_json = output / "audit.json"
            relative = str(audit_json.relative_to(project.root)).replace("\\", "/")
            audit_json.write_text("tampered", encoding="utf-8")
            with self.assertRaises(ReviewStudioError):
                project.export_file(relative)

    def test_environment_repair_retries_blocked_project_and_protocol_zip_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="scan.pdf", content=b"%PDF-1.7\nnot a real pdf")
            app = StudioApp.create(temp_dir, project.root)
            with patch("document_review_ui.repair_dependencies", return_value=[]), patch.object(DocumentReviewProject, "retry_extraction") as retry:
                repaired = app.repair_environment()
            retry.assert_called_once_with()
            self.assertIn("环境修复完成", repaired.notice)

            clean = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            clean.confirm_extraction("confirm")
            clean.confirm_context(self.context())
            clean.prepare_ai_audits(CRITIC_DIMENSIONS, provider="provider", model="model")
            bundle = StudioApp.create(temp_dir, clean.root).protocol_bundle()
            with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
                self.assertEqual(len([name for name in archive.namelist() if name.endswith("prompt.md")]), 5)
                self.assertFalse(any(".integrity" in name for name in archive.namelist()))

    def test_ui_is_single_template_without_runtime_patch_chain(self) -> None:
        source = Path(__import__("document_review_ui").__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("def render_studio_shell"), 1)
        self.assertNotIn("_render_studio_shell_base", source)
        self.assertIn("修正后接受", render_studio_shell("token"))
        self.assertIn("下载全部协议 ZIP", render_studio_shell("token"))

    def test_corrupt_internal_document_is_read_only_and_does_not_crash_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            project.document_path.write_text("{broken", encoding="utf-8")
            view = project.view()
            self.assertTrue(view["state"]["read_only"])
            self.assertFalse(view["extraction"]["available"])


if __name__ == "__main__":
    unittest.main()
