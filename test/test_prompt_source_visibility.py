"""The exported review must carry the parser's actual visibility limits."""
import io
import json
import tempfile
import unittest
import zipfile

from document_review_studio import DocumentReviewProject
from document_review_model import ReviewContext
from test.test_document_review_studio import _docx
from test.test_delivery import ready
from pathlib import Path


class SourceVisibilityTests(unittest.TestCase):
    def test_omitted_docx_notes_are_in_the_bound_prompt(self):
        raw = io.BytesIO(_docx())
        with zipfile.ZipFile(raw, "a") as archive:
            archive.writestr("word/footnotes.xml", '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:footnote w:id="1"><w:p><w:r><w:t>NOTE NOT EXTRACTED</w:t></w:r></w:p></w:footnote></w:footnotes>')
        with tempfile.TemporaryDirectory() as temp:
            project = DocumentReviewProject.create(temp, filename="paper.docx", content=raw.getvalue())
            project.confirm_extraction("confirm")
            project.confirm_context({**ReviewContext("academic article", "unknown", "unknown", "author", "researchers").to_dict(), "review_profile": "academic", "discipline": "humanities", "research_type": "theoretical"})
            request = project.prepare_ai_audits(["academic_citations"], provider="test", model="reviewer")[0]
            prompt = request["prompt"]
            contract = json.loads(prompt.split("```json\n", 1)[1].split("\n```", 1)[0])
            visibility = contract["source_visibility"]
            self.assertEqual(visibility["extraction_warnings"], [w.to_dict() for w in project.document().warnings])
            self.assertIn("footnotes-present", [w["code"] for w in visibility["extraction_warnings"]])
            self.assertIn("footnotes-present", visibility["quality_signals"]["footnote_comment_revision_risk"])
            self.assertNotIn("NOTE NOT EXTRACTED", prompt)
            saved = (project.root / "ai-requests" / request["request_id"] / "prompt.md").read_text(encoding="utf-8")
            self.assertEqual(saved, prompt)
            self.assertIn("make dependent criticisms conditional", visibility["rule"])

    def test_clean_source_does_not_acquire_fictitious_warnings(self):
        with tempfile.TemporaryDirectory() as temp:
            project = ready(Path(temp))
            prompt = project.prompt("expression_ambiguity")
            contract = json.loads(prompt.split("```json\n", 1)[1].split("\n```", 1)[0])
            visibility = contract["source_visibility"]
            self.assertEqual(visibility["extraction_warnings"], [])
            self.assertEqual(visibility["image_placeholder_count"], 0)
            self.assertEqual(visibility["parser_version"], project.document().parser_version)
            self.assertIn("Criticisms fully supported by visible passages remain eligible", visibility["rule"])


if __name__ == "__main__":
    unittest.main()
