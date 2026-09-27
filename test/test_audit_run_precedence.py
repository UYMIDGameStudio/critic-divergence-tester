"""Local prechecks must not displace an imported review of the same document."""

from __future__ import annotations

import json
import tempfile
import unittest

from document_review_studio import DocumentReviewProject


CRITIC = "expression_ambiguity"


class AuditRunPrecedenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = DocumentReviewProject.create(
            temporary.name, filename="draft.md",
            content="# 活动方案\n\n相关人员及时完成报名。\n".encode("utf-8"),
        )
        self.project.confirm_extraction("confirm")
        self.project.confirm_context({
            "document_type": "活动策划案", "jurisdiction": "unknown",
            "effective_date": "unknown", "publisher_type": "作者",
            "audience": "读者", "publication_status": "internal-draft",
            **{"involves_" + key: False for key in (
                "contract", "fees", "intellectual_property", "minors",
                "personal_information", "sponsorship",
            )},
        })

    def import_review(self, *, findings=True):
        request = self.project.prepare_ai_audits(
            [CRITIC], provider="test-provider", model="test-model",
        )[0]
        block = self.project.document().blocks[1]
        finding = {
            "finding_id": "F1", "critic": CRITIC,
            "document_type": "活动策划案", "location": block.location.to_dict(),
            "evidence": block.text, "issue": "Registration has no deadline.",
            "standard": "State a deadline for registration.",
            "consequence": "Participants cannot determine when registration closes.",
            "severity": "medium", "verification_state": "model-proposed",
            "external_basis": {}, "uncertainties": [],
            "suggested_action": "Specify the registration deadline.",
            "suggested_owner": "Document owner", "blocks_release_or_execution": False,
            "check_data": {"close_reading": {
                "author_position": "Participants should register promptly.",
                "strongest_defense": "Prompt registration might suffice informally.",
                "why_defense_fails": "The sentence does not establish a closing time.",
                "repair_test": "Verify that a registration deadline is stated.",
                "context_evidence": [{"block_id": block.block_id,
                                      "quote": block.text, "role": "qualification"}],
            }},
        }
        response = {
            **{key: request[key] for key in (
                "request_id", "prompt_sha256", "provider", "model", "critic", "source_sha256",
            )},
            "findings": [finding] if findings else [],
            "zero_finding_basis": [] if findings else ["Checked every block for ambiguous deadlines."],
        }
        run = self.project.collect_model_audit(
            CRITIC, json.dumps(response, ensure_ascii=False),
            provider=request["provider"], model=request["model"], request_id=request["request_id"],
        )
        return request, run

    def test_later_prechecks_preserve_ai_findings_decisions_origin_state_and_export(self):
        request, ai_run = self.import_review()
        finding_id = ai_run.findings[0].finding_id
        self.project.decide_finding(finding_id, "defer", reason="Waiting for a deadline.")
        original = self.project._active_audit_run_records()[CRITIC][0]
        original_bytes = original.read_bytes()

        local_run = self.project.run_local_prechecks([CRITIC])[0]

        self.assertEqual(local_run.model_label, "deterministic-local-rules")
        self.assertTrue(local_run.findings)
        self.assertEqual(self.project._ordered_audit_runs(CRITIC)[-1][1]["run_id"], local_run.run_id)
        self.assertEqual(self.project._active_audit_run_records()[CRITIC][1]["run_id"], ai_run.run_id)
        self.assertEqual([(item.finding_id, item.status) for item in self.project.findings()],
                         [(finding_id, "defer")])
        self.assertEqual(self.project._critic_origin_binding(CRITIC)["original_request_id"],
                         request["request_id"])
        cached = json.loads(self.project.state_path.read_text(encoding="utf-8"))
        self.assertEqual(cached["review_state"], "ai_review_imported")
        self.assertEqual(cached["ai_review_state"], "imported")
        self.assertEqual(self.project.state()["review_state"], "ai_review_imported")
        output = self.project.export_ai_reviews()
        snapshot = json.loads((output / "AI审查结果.json").read_text(encoding="utf-8"))
        self.assertEqual([run["run_id"] for run in snapshot["runs"]], [ai_run.run_id])
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertEqual(self.project.integrity_errors(), [])

    def test_latest_ai_review_still_supersedes_earlier_ai_review(self):
        _, first = self.import_review()
        self.project.run_local_prechecks([CRITIC])
        request, second = self.import_review()
        self.project.run_local_prechecks([CRITIC])

        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual([item.finding_id for item in self.project.findings()],
                         [second.findings[0].finding_id])
        self.assertEqual(self.project._critic_origin_binding(CRITIC)["original_request_id"],
                         request["request_id"])
        self.assertEqual(len(self.project._ordered_audit_runs(CRITIC)), 4)
        self.assertEqual(self.project.integrity_errors(), [])

    def test_zero_finding_ai_review_is_not_replaced_by_local_findings(self):
        _, ai_run = self.import_review(findings=False)
        local_run = self.project.run_local_prechecks([CRITIC])[0]

        self.assertTrue(local_run.findings)
        self.assertEqual(self.project.findings(), [])
        self.assertEqual(self.project._active_audit_run_records()[CRITIC][1]["run_id"], ai_run.run_id)
        self.assertEqual(self.project.state()["review_state"], "ai_review_imported")

    def test_local_only_project_uses_latest_precheck(self):
        first = self.project.run_local_prechecks([CRITIC])[0]
        second = self.project.run_local_prechecks([CRITIC])[0]

        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(self.project._active_audit_run_records()[CRITIC][1]["run_id"], second.run_id)
        self.assertEqual({item.finding_id for item in self.project.findings()},
                         {item.finding_id for item in second.findings})
        cached = json.loads(self.project.state_path.read_text(encoding="utf-8"))
        self.assertEqual(cached["review_state"], "local_precheck_completed")
        self.assertEqual(self.project.integrity_errors(), [])


if __name__ == "__main__":
    unittest.main()
