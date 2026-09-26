"""Academic quality routing and persisted protocols, not model-quality scores."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import review_profiles as profiles
from document_review_adversarial import response_example
from document_review_model import ReviewContext
from document_review_quality import close_reading_example
from document_review_studio import DocumentReviewProject


class AcademicQualityProtocolTests(unittest.TestCase):
    def test_all_confirmed_scopes_have_actionable_critic_specific_criteria(self):
        criterion_fields = {"id", "quality", "when", "test", "guard", "sources"}
        for critic in profiles.ACADEMIC_CRITICS:
            for discipline in profiles.DISCIPLINES:
                for kind in profiles.RESEARCH_TYPES:
                    with self.subTest(critic=critic, discipline=discipline, kind=kind):
                        protocol = profiles.academic_protocol(
                            critic, discipline=discipline, research_type=kind)
                        self.assertEqual(protocol["version"], 2)
                        self.assertEqual(protocol["confirmed_scope"],
                                         {"discipline": discipline, "research_type": kind})
                        quality = protocol["scholarly_quality"]
                        self.assertEqual(quality["version"], protocol["version"])
                        self.assertTrue(quality["workflow"])
                        self.assertTrue(all(isinstance(step, str) and step.strip()
                                            for step in quality["workflow"]))
                        self.assertTrue(quality["basis"].strip())
                        self.assertTrue(quality["source_limits"].strip())
                        self.assertTrue(quality["criteria"])
                        ids = [criterion["id"] for criterion in quality["criteria"]]
                        self.assertEqual(len(ids), len(set(ids)))
                        for criterion in quality["criteria"]:
                            self.assertEqual(set(criterion), criterion_fields)
                            for field in criterion_fields - {"sources"}:
                                self.assertIsInstance(criterion[field], str)
                                self.assertTrue(criterion[field].strip())
                            self.assertTrue(criterion["sources"])
                            self.assertEqual(len(criterion["sources"]),
                                             len(set(criterion["sources"])))
                        # A contribution reviewer must not become a bibliography
                        # checker, nor a citation reviewer a method checklist.
                        if critic == "academic_argument":
                            self.assertIn("question-contribution", ids)
                            self.assertNotIn("source-entailment", ids)
                        elif critic == "academic_citations":
                            self.assertIn("source-entailment", ids)
                            self.assertNotIn("design-fit", ids)
                        else:
                            self.assertIn("design-fit", ids)
                            self.assertNotIn("question-contribution", ids)

    def test_method_requirements_are_selected_by_actual_research_task(self):
        expected = {
            "unspecified": {"type-uncertainty"},
            "empirical": {"selection-measurement", "quantitative-uncertainty"},
            "theoretical": {"interpretive-warrant", "formal-proof"},
            "review": {"review-selection", "review-synthesis"},
            "engineering": {"fair-comparison", "failure-envelope"},
        }
        selected = {}
        for kind, expected_ids in expected.items():
            protocol = profiles.academic_protocol("academic_methods", research_type=kind)
            criteria = {item["id"]: item for item in protocol["scholarly_quality"]["criteria"]}
            selected[kind] = criteria
            self.assertTrue(expected_ids <= set(criteria), kind)
            for other_kind, other_ids in expected.items():
                if other_kind != kind:
                    self.assertTrue(set(criteria).isdisjoint(other_ids), (kind, other_kind))
        # Guardrails are part of the review method, not just a choice of label.
        self.assertIn("不得要求人文理论以实验", selected["theoretical"]["interpretive-warrant"]["guard"])
        self.assertIn("概念论说不自动需要形式证明", selected["theoretical"]["formal-proof"]["guard"])
        self.assertIn("实际使用统计", selected["empirical"]["quantitative-uncertainty"]["when"])
        self.assertIn("质性和目的性选材不默认追求统计代表性",
                      selected["empirical"]["selection-measurement"]["guard"])
        self.assertIn("叙述性综述不自动需要", selected["review"]["review-selection"]["guard"])
        self.assertIn("只有声称组件贡献时", selected["engineering"]["fair-comparison"]["test"])

    def test_sources_resolve_and_nested_metadata_is_detached(self):
        originals = copy.deepcopy((profiles.ACADEMIC_PROTOCOLS,
                                   profiles.ACADEMIC_QUALITY_CRITERIA,
                                   profiles.METHOD_QUALITY_BY_TYPE,
                                   profiles.ACADEMIC_QUALITY_SOURCES))
        for critic in profiles.ACADEMIC_CRITICS:
            for kind in profiles.RESEARCH_TYPES:
                with self.subTest(critic=critic, kind=kind):
                    protocol = profiles.academic_protocol(critic, research_type=kind)
                    fresh = copy.deepcopy(protocol)
                    quality = protocol["scholarly_quality"]
                    used = {source for item in quality["criteria"] for source in item["sources"]}
                    self.assertEqual(set(quality["sources"]), used)
                    for source_id, source in quality["sources"].items():
                        self.assertEqual(source, profiles.ACADEMIC_QUALITY_SOURCES[source_id])
                        self.assertTrue(source["title"].strip())
                        parsed = urlsplit(source["url"])
                        self.assertEqual(parsed.scheme, "https")
                        self.assertTrue(parsed.netloc)
                    quality["criteria"][0]["test"] = "changed by caller"
                    quality["criteria"][-1]["sources"].append("caller-only")
                    quality["sources"][next(iter(used))]["title"] = "caller-only"
                    quality["workflow"].clear()
                    quality["finding_mapping"]["standard"] = "caller-only"
                    protocol["checks"].clear()
                    self.assertEqual(profiles.academic_protocol(critic, research_type=kind), fresh)
        self.assertEqual((profiles.ACADEMIC_PROTOCOLS, profiles.ACADEMIC_QUALITY_CRITERIA,
                          profiles.METHOD_QUALITY_BY_TYPE, profiles.ACADEMIC_QUALITY_SOURCES), originals)

    def test_quality_metadata_does_not_change_response_contracts(self):
        self.assertEqual(set(close_reading_example()), {
            "author_position", "strongest_defense", "why_defense_fails", "repair_test", "context_evidence"})
        self.assertEqual(set(response_example("defense", version=1)), {
            "author_position", "strongest_defense", "limitations", "context_evidence"})
        self.assertEqual(set(response_example("assessment", version=1)), {
            "disposition", "reasons", "remaining_issue", "minimal_repair", "repair_test",
            "defense_evidence_ids", "context_evidence"})


class AcademicQualitySnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.project = DocumentReviewProject.create(
            directory.name, filename="paper.md",
            content="# A bounded interpretation\n\nThe interpretation necessarily explains every case.\n\nThe evidence concerns one passage only.\n".encode("utf-8"))
        self.project.confirm_extraction("confirm")
        context = ReviewContext("academic article", "unknown", "unknown", "author", "researchers").to_dict()
        context.update(review_profile="academic", discipline="humanities", research_type="theoretical")
        self.project.confirm_context(context)
        self.critic = "academic_argument"

    def request(self):
        return self.project.prepare_ai_audits([self.critic], provider="test", model="reviewer")[0]

    def import_finding(self, request):
        block = self.project.document().blocks[1]
        finding = {
            "finding_id": "F1", "critic": self.critic, "document_type": "academic article",
            "location": {"block_id": block.block_id}, "evidence": block.text,
            "issue": "The universal conclusion exceeds the bounded evidence.",
            "standard": "claim-calibration: conclusions must preserve the evidence scope.",
            "consequence": "The reader is led to infer universal coverage from a single passage.",
            "severity": "medium", "verification_state": "model-proposed",
            "external_basis": {"unresolved_facts": []}, "uncertainties": [],
            "suggested_action": "Limit the conclusion to the analyzed passage.",
            "suggested_owner": "author", "blocks_release_or_execution": False,
        }
        payload = {**{key: request[key] for key in
                      ("request_id", "prompt_sha256", "provider", "model", "critic", "source_sha256")},
                   "findings": [finding]}
        return self.project.collect_model_audit(
            self.critic, json.dumps(payload), provider="test", model="reviewer",
            request_id=request["request_id"]).findings[0]

    def test_academic_snapshot_survives_template_change_in_adversarial_and_recheck(self):
        request = self.request()
        original = copy.deepcopy(request["critic_protocol"])
        self.assertEqual(original["scholarly_quality"]["version"], 2)
        request_dir = self.project.root / "ai-requests" / request["request_id"]
        saved = {name: (request_dir / name).read_bytes() for name in ("request.json", "prompt.md")}
        finding = self.import_finding(request)
        future_criteria = copy.deepcopy(profiles.ACADEMIC_QUALITY_CRITERIA[self.critic])
        future_criteria[0]["quality"] = "FUTURE STANDARD MUST NOT APPLY RETROACTIVELY"
        with patch.object(profiles, "ACADEMIC_QUALITY_VERSION", 99), patch.dict(
                profiles.ACADEMIC_QUALITY_CRITERIA, {self.critic: future_criteria}):
            self.assertIn("FUTURE STANDARD", self.project.prompt(self.critic))
            session = self.project.prepare_adversarial_review(finding.finding_id, provider="test", model="defender")
            self.assertEqual(session["critic_origin"]["critic_protocol"], original)
            self.assertNotIn("FUTURE STANDARD", session["requests"][0]["prompt"])
            defense_request = session["requests"][0]
            defense = copy.deepcopy(defense_request["response_example"])
            block = self.project.document().blocks[2]
            defense["result"]["context_evidence"][0].update(block_id=block.block_id, quote=block.text)
            self.project.collect_adversarial_response(
                session["session_id"], json.dumps(defense), request_id=defense_request["request_id"])
            assessment = self.project.prepare_adversarial_assessment(
                session["session_id"], provider="test", model="assessor")
            self.assertEqual(assessment["critic_origin"]["critic_protocol"], original)
            self.assertNotIn("FUTURE STANDARD", assessment["requests"][1]["prompt"])
            self.assertEqual(assessment["requests"][1]["response_contract_version"], 1)
            assessment_request = assessment["requests"][1]
            assessment_response = copy.deepcopy(assessment_request["response_example"])
            assessment_response["result"]["context_evidence"][0].update(block_id=block.block_id, quote=block.text)
            completed = self.project.collect_adversarial_response(
                session["session_id"], json.dumps(assessment_response), request_id=assessment_request["request_id"])
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(self.project.findings()[0].status, "open")

            self.project.decide_finding(finding.finding_id, "accept", reason="Narrow the unsupported scope.")
            action = self.project.prepare_revision_plan()["actions"][0]
            self.project.set_revision_action_operation(action["action_id"], "replace_block", reason="Revise the claim only.")
            hunk = self.project.propose_revision_hunk(
                action["action_id"], "The interpretation explains the analyzed passage.", rationale="Match the stated evidence.")
            self.project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="The wording preserves the bounded argument.")
            revision_dir = self.project.finalize_revision()
            revision = json.loads((revision_dir / "revision.json").read_text(encoding="utf-8"))
            external = self.project.external_recheck_status(revision["revision_id"])["requests"][0]
            self.assertEqual(external["original_request_id"], request["request_id"])
            self.assertEqual(external["critic_protocol"], original)
            self.assertEqual(external["critic_protocol_sha256"], request["critic_protocol_sha256"])
            self.assertNotIn("FUTURE STANDARD", external["prompt"])
            recheck_response = {
                **{key: external[key] for key in ("request_id", "prompt_sha256", "critic")},
                "revision_id": revision["revision_id"], "revised_sha256": revision["revised_sha256"],
                "resolutions": [{"finding_id": finding.finding_id, "state": "resolved",
                                 "reason": "The revised conclusion names its bounded material.",
                                 "evidence": "The interpretation explains the analyzed passage."}],
                "new_findings": [],
            }
            self.project.collect_external_recheck(
                revision["revision_id"], self.critic, json.dumps(recheck_response), provider="test", model="reviewer")
            self.assertFalse(self.project.external_recheck_status(revision["revision_id"])["complete"])
        for name, data in saved.items():
            self.assertEqual((request_dir / name).read_bytes(), data)
        reloaded = DocumentReviewProject(self.project.root)
        self.assertEqual(reloaded.external_recheck_status(revision["revision_id"])["requests"][0]["critic_protocol"], original)
        self.assertEqual(reloaded.integrity_errors(), [])

    def test_pre_quality_legacy_prompt_is_recovered_without_adding_new_standards(self):
        request = self.request()
        marker = "## Contract and critic-specific protocol\n```json\n"
        start = request["prompt"].index(marker) + len(marker)
        contract, end = json.JSONDecoder().raw_decode(request["prompt"][start:])
        legacy_protocol = contract["protocol"]
        legacy_protocol.pop("version")
        legacy_protocol.pop("scholarly_quality")
        legacy_prompt = (request["prompt"][:start] + json.dumps(contract) + request["prompt"][start + end:]).encode("utf-8")
        legacy_request = {"critic": self.critic}
        with patch.object(profiles, "ACADEMIC_QUALITY_VERSION", 99):
            recovered = self.project._snapshotted_critic_protocol(legacy_request, legacy_prompt)
        self.assertEqual(recovered, legacy_protocol)
        self.assertNotIn("version", recovered)
        self.assertNotIn("scholarly_quality", recovered)


if __name__ == "__main__":
    unittest.main()
