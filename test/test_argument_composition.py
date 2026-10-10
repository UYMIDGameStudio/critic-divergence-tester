"""Argument design contracts and real persistence; not model-quality scores."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from unittest.mock import patch

import review_profiles
from document_review_composition import (
    assessment_example, assessment_protocol, assessment_contract_errors, validate_argument_assessment,
)
from document_review_model import ReviewContext
from document_review_studio import DocumentReviewProject, ReviewStudioError


def synthetic_assessment(project):
    """A hand-authored fixture, never a claim about model performance."""
    value = assessment_example()
    blocks = project.document().blocks
    value["review_frame"]["evidence"] = [{"block_id": blocks[1].block_id, "quote": blocks[1].text}]
    for index, axis in enumerate(value["dimensions"]):
        name = axis["id"]
        block = blocks[min(index, len(blocks) - 1)]
        axis.update(description=f"Synthetic {name} description · Straße 日本語",
                    assessment="This bounded design serves the stated question.",
                    strengths=["The selected contrast helps distinguish the two cases."],
                    recommendation="Keep this supported choice.",
                    evidence=[{"block_id": block.block_id, "quote": block.text}])
    return value


class ArgumentCompositionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = DocumentReviewProject.create(temp.name, filename="article.md", content=(
            "# Bounded comparison\n\nThe question concerns two interpretations.\n\n"
            "First distinguish their shared assumptions.\n\nThen compare how each explains the same passage.\n\n"
            "The conclusion preserves what both interpretations establish.\n").encode())
        self.project.confirm_extraction("confirm")
        self.project.confirm_context(ReviewContext("academic article", "unknown", "unknown", "author", "researchers",
            review_profile="academic", discipline="humanities", research_type="theoretical").to_dict())

    def request(self, critic="academic_argument"):
        return self.project.prepare_ai_audits([critic], provider="test", model="fixture")[0]

    def payload(self, request):
        return {**{k: request[k] for k in ("request_id", "prompt_sha256", "provider", "model", "critic", "source_sha256")},
                "findings": [], "zero_finding_basis": ["Synthetic review inspected only supplied blocks."],
                "argument_assessment": synthetic_assessment(self.project)}

    def collect(self, request, payload):
        return self.project.collect_model_audit(request["critic"], json.dumps(payload),
            provider="test", model="fixture", request_id=request["request_id"])

    def test_zero_defects_still_preserves_four_design_judgements_in_view_and_export(self):
        request = self.request()
        payload = self.payload(request)
        run = self.collect(request, payload)
        self.assertEqual(run.findings, [])
        self.assertEqual(run.argument_assessment, payload["argument_assessment"])
        restored = DocumentReviewProject(self.project.root)
        self.assertEqual(restored.view()["argument_assessments"][0]["assessment"], run.argument_assessment)
        self.assertEqual(restored.view()["finding_summary"]["total"], 0)
        folder = restored.export_ai_reviews()
        markdown = next(folder.glob("*.md")).read_text(encoding="utf-8")
        for label in ("论证结构", "论证方向", "论证方法", "特征特点"):
            self.assertIn(label, markdown)
        self.assertIn("Keep this supported choice.", markdown)
        final = restored.export()
        audit = json.loads((final / "audit.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["audit_runs"][0]["argument_assessment"], run.argument_assessment)
        self.assertIn("论证与文章综合审查", (final / "audit.md").read_text(encoding="utf-8"))
        self.assertEqual(restored.integrity_errors(), [])

    def test_protocol_only_requires_overview_from_its_owner(self):
        requests = self.project.prepare_ai_audits(provider="test", model="fixture")
        for request in requests:
            owner = request["critic"] == "academic_argument"
            self.assertEqual("argument_assessment_protocol" in request, owner)
            self.assertEqual('"argument_assessment"' in request["prompt"], owner)
        original = assessment_protocol()
        mutated = assessment_protocol()
        mutated["dimension_seeds"]["structure"] = "caller mutation"
        self.assertEqual(assessment_protocol(), original)

    def test_requires_every_axis_and_current_exact_evidence_even_with_no_findings(self):
        request = self.request()
        payload = self.payload(request)
        changes = [lambda p: p.pop("argument_assessment"),
                   lambda p: p["argument_assessment"].update(dimensions=[]),
                   lambda p: p["argument_assessment"]["dimensions"][1]["evidence"][0].update(block_id="another-project"),
                   lambda p: p["argument_assessment"]["dimensions"][2]["evidence"][0].update(quote="Invented premise."),
                   lambda p: p["argument_assessment"]["dimensions"][0].update(status="verified"),
                   lambda p: p["argument_assessment"].update(version=True),
                   lambda p: p["argument_assessment"]["dimensions"][3].update(human_approved=True)]
        for change in changes:
            altered = copy.deepcopy(payload)
            change(altered)
            with self.subTest(change=change), self.assertRaises(ReviewStudioError):
                self.collect(request, altered)
        self.assertFalse(self.project.ai_requests()[0]["completed"])
        self.assertEqual(self.project.integrity_errors(), [])
        self.collect(request, payload)

    def test_unavailable_material_is_not_forced_into_a_positive_or_negative_judgement(self):
        request = self.request()
        payload = self.payload(request)
        axis = payload["argument_assessment"]["dimensions"][3]
        axis.update(status="unable-to-assess", evidence=[], strengths=[],
                    assessment="Only a short fragment is visible; recurrence cannot be assessed.",
                    limitations=["The rest of the manuscript was not supplied."])
        self.collect(request, payload)
        self.assertEqual(self.project.view()["argument_assessments"][0]["assessment"]["dimensions"][3], axis)

    def test_saved_v3_request_does_not_acquire_the_new_response_requirement(self):
        old = review_profiles.academic_protocol("academic_argument", discipline="humanities", research_type="theoretical")
        old["version"] = old["scholarly_quality"]["version"] = 3
        with patch("document_review_stores.audits.academic_protocol", return_value=old):
            request = self.request()
        self.assertNotIn("argument_assessment_protocol", request)
        payload = self.payload(request)
        payload.pop("argument_assessment")
        run = self.collect(request, payload)
        self.assertIsNone(run.argument_assessment)
        self.assertEqual(self.project.view()["argument_assessments"], [])
        new = self.request()
        self.assertEqual(new["critic_protocol"]["version"], 4)
        self.assertIn("argument_assessment_protocol", new)
        self.assertEqual(self.project.integrity_errors(), [])

    def test_response_cannot_add_unbound_design_reviews_to_other_critics(self):
        request = self.request("academic_methods")
        with self.assertRaisesRegex(ReviewStudioError, "未绑定"):
            self.collect(request, self.payload(request))

    def test_dimensions_can_extend_beyond_examples_and_are_selected_without_a_fixed_quota(self):
        request = self.request()
        payload = self.payload(request)
        angle = copy.deepcopy(payload["argument_assessment"]["dimensions"][0])
        angle.update(id="argument-economy", title="Argument economy",
                     why_relevant="The article repeats the same distinction in multiple stages.",
                     description="A bounded review of the repeated explanation.")
        payload["argument_assessment"]["dimensions"] = [angle]
        self.collect(request, payload)
        self.assertEqual(self.project.view()["argument_assessments"][0]["assessment"]["dimensions"], [angle])

    def test_selected_angles_need_distinct_id_relevance_and_a_review_plan(self):
        blocks = {b.block_id: b for b in self.project.document().blocks}
        valid = synthetic_assessment(self.project)
        for change in (
            lambda v: v["dimensions"].append(copy.deepcopy(v["dimensions"][0])),
            lambda v: v["dimensions"][0].update(why_relevant=""),
            lambda v: v["review_frame"].update(priorities=[]),
            lambda v: v["review_frame"]["evidence"][0].update(quote="Made-up purpose."),
            lambda v: v.update(dimensions=v["dimensions"] * 3),
        ):
            altered = copy.deepcopy(valid)
            change(altered)
            self.assertTrue(validate_argument_assessment(altered, blocks))

    def test_field_limits_and_snapshot_version_are_enforced(self):
        value = synthetic_assessment(self.project)
        blocks = {b.block_id: b for b in self.project.document().blocks}
        for field, invalid in (("description", "x" * 3001), ("strengths", ["a"] * 4),
                               ("evidence", []), ("status", [])):
            changed = copy.deepcopy(value)
            changed["dimensions"][2][field] = invalid
            self.assertTrue(validate_argument_assessment(changed, blocks))
        for version in (True, 1.0, 2):
            protocol = assessment_protocol()
            protocol["version"] = version
            self.assertTrue(assessment_contract_errors(protocol))
        request = self.request()
        altered = dict(request, argument_assessment_protocol_sha256="f" * 64)
        with self.assertRaisesRegex(ReviewStudioError, "快照"):
            self.project._snapshotted_argument_assessment_protocol(altered, request["prompt"].encode())


if __name__ == "__main__":
    unittest.main()
