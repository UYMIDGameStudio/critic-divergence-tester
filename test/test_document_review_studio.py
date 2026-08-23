from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from document_review_ingest import IngestionError, ingest_bytes, safe_upload_name
from document_review_model import CRITIC_DIMENSIONS
from document_review_studio import DocumentReviewProject, ReviewStudioError


def _docx(*, revised: bool = False) -> bytes:
    revision = '<w:ins w:id="1"><w:r><w:t>inserted</w:t></w:r></w:ins>' if revised else ""
    body = f'''<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>活动方案</w:t></w:r></w:p><w:p><w:r><w:t>相关人员完成报名</w:t></w:r>{revision}</w:p><w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>合并单元格</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:sectPr/></w:body></w:document>'''
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"/>")
        archive.writestr("word/document.xml", body)
        archive.writestr("word/settings.xml", "<w:settings xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"/>")
    return output.getvalue()


def _simple_pdf() -> bytes:
    return b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n2 0 obj << /Length 31 >> stream\nBT (Hello PDF) Tj ET\nendstream\n%%EOF\n"


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

    def test_builtin_pdf_fallback_preserves_page_location(self) -> None:
        document = ingest_bytes("notice.pdf", _simple_pdf())
        self.assertTrue(any(block.location.page == 1 for block in document.blocks))
        self.assertIn("Hello PDF", document.plain_text)

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
            revised = output / "revised.docx"
            self.assertTrue(revised.is_file())
            with zipfile.ZipFile(revised) as archive:
                self.assertIn("word/document.xml", archive.namelist())
            capability = json.loads((output / "track-changes-capability.json").read_text(encoding="utf-8"))
            self.assertFalse(capability["native_track_changes"])

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
