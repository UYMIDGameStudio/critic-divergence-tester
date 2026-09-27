"""Synthetic regressions distilled from project audits; no private manuscripts."""
import unittest

from document_review_ingest import ingest_bytes
from document_review_model import DocumentBlock, DocumentLocation, ReviewContext
import document_review_studio  # Initialize component globals without opening a project.
from document_review_stores.audits import AuditRunStore


class DocumentPrecheckScopeTests(unittest.TestCase):
    def run_check(self, kind, text, critic, *, picture=False, title_lines=1):
        document = ingest_bytes("fixture.txt", text.encode("utf-8"))
        if title_lines > 1:
            # Word manual line breaks live inside one paragraph block.
            document.blocks[0].text = "\n".join(b.text for b in document.blocks[:title_lines])
            del document.blocks[1:title_lines]
        if picture:
            document.blocks.insert(0, DocumentBlock("image", "image_placeholder", "[图片]", location=DocumentLocation("image", "image_placeholder")))
        original = document.to_dict()
        project = object.__new__(AuditRunStore)
        context = ReviewContext(document_type=kind, publication_status="external-formal", confirmed=True)
        run = project._deterministic_audit(critic, document, context)
        self.assertEqual(document.to_dict(), original)
        return run

    def test_speech_and_post_are_not_forced_to_be_execution_or_governance_plans(self):
        for kind in ("主持稿", "推文", "演講稿", "speech", "press release"):
            for critic in ("execution_feasibility", "reasonableness_governance"):
                with self.subTest(kind=kind, critic=critic):
                    run = self.run_check(kind, f"活动{kind}\n\n欢迎各位来宾。活动将在明天开始。", critic)
                    self.assertEqual(run.findings, [])
                    self.assertTrue(run.observations)
                    self.assertTrue(run.zero_finding_basis)

    def test_plain_two_line_speech_title_is_not_mistaken_for_missing_heading(self):
        for critic in ("expression_ambiguity", "official_professional_format"):
            run = self.run_check("主持稿", "社区交流活动\n开幕主持稿\n\n2026年09月27日\n\n欢迎各位来宾。", critic, title_lines=2)
            self.assertFalse(any(f.check_id == "expression.document_purpose" for f in run.findings))
            self.assertFalse(any("标题" in f.check_data.get("items", []) for f in run.findings))

    def test_positive_ambiguity_still_requires_attention_in_informational_document(self):
        run = self.run_check("推文", "社区活动推文\n\n相关人员适时报名。", "expression_ambiguity", picture=True)
        self.assertTrue(any(f.check_id.startswith("expression.ambiguous_term:") for f in run.findings))
        self.assertTrue(all(f.location.block_id != "image" for f in run.findings))

    def test_execution_plan_retains_its_applicable_checks_and_real_text_anchor(self):
        run = self.run_check("活动策划案", "执行方案\n\n本方案没有负责人、预算或验收标准。", "execution_feasibility", picture=True)
        self.assertEqual({f.check_id for f in run.findings}, {"execution.owner", "execution.budget", "execution.acceptance"})
        self.assertTrue(all(f.location.block_id != "image" and f.evidence != "[图片]" for f in run.findings))

    def test_governance_rules_continue_to_raise_control_gaps(self):
        run = self.run_check("处分规则", "违规处分规则\n\n违规者将被取消资格；尚无申诉渠道。", "reasonableness_governance")
        self.assertTrue(any(f.check_id == "governance.required_controls" for f in run.findings))


if __name__ == "__main__":
    unittest.main()
