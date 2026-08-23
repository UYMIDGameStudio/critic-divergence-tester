from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import document_review_ingest
from document_review_ingest import IngestionError, ingest_bytes, safe_upload_name
from document_review_model import CRITIC_DIMENSIONS, DocumentBlock, DocumentLocation, ExtractionWarning, QualitySignals, RawFileBinding, StructuredDocument, stable_id
from document_review_studio import DocumentReviewProject, ReviewStudioError
from document_review_ui import render_studio_shell


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
            project.decide_finding(findings[0].finding_id, "accept", reason="补充责任人")
            bridge = project.prepare_revision_bridge()
            self.assertTrue(bridge.is_file())
            export = project.export()
            self.assertTrue((export / "audit.json").is_file())
            self.assertIsNone(__import__("json").loads((export / "audit.json").read_text(encoding="utf-8"))["scores"])
            self.assertEqual(project.integrity_errors(), [])

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
            payload = {"critic": selected["critic"], "source_sha256": project.document().source.sha256, "findings": [], "zero_finding_basis": ["independent pass"]}
            run = project.collect_model_audit(selected["critic"], json.dumps(payload), provider="example-provider", model="example-model", request_id=selected["request_id"])
            run_path = project.root / "audits" / selected["critic"] / f"{run.run_id}.json"
            saved = json.loads(run_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["model_invocation"]["prompt_sha256"], selected["prompt_sha256"])
            self.assertEqual(saved["model_invocation"]["provider"], "example-provider")
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
            payload = {
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
            run = project.collect_model_audit("expression_ambiguity", json.dumps(payload, ensure_ascii=False))
            self.assertEqual(len(run.findings), 1)
            source = project.root / "source" / "draft.txt"
            source.write_bytes(b"tampered\n")
            view = project.view()
            self.assertTrue(view["state"]["read_only"])
            with self.assertRaises(ReviewStudioError):
                project.decide_finding("F-model-1", "accept", reason="not allowed after tamper")

    def test_corrupt_internal_document_is_read_only_and_does_not_crash_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = DocumentReviewProject.create(temp_dir, filename="draft.txt", content=b"Draft text\n")
            project.document_path.write_text("{broken", encoding="utf-8")
            view = project.view()
            self.assertTrue(view["state"]["read_only"])
            self.assertFalse(view["extraction"]["available"])


if __name__ == "__main__":
    unittest.main()
