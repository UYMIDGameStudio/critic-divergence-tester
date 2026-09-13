"""Source-grounding regressions; these do not measure model semantic accuracy."""

from __future__ import annotations

import copy
import json
import unittest
import unicodedata
from unittest.mock import patch

import document_review_studio as studio
from document_review_quality import close_reading_example, validate_close_reading
from test import test_review_round_protocols


class CloseReadingQualityTests(unittest.TestCase):
    def setUp(self):
        self.helper = test_review_round_protocols.ReviewRoundProtocolTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.project = self.helper.project

    def detail(self):
        blocks = self.project.document().blocks
        return {
            "author_position": "The text describes a bounded multilingual activity.",
            "strongest_defense": "The table might assign responsibilities; its cells must be checked.",
            "why_defense_fails": "The cited table identifies a venue but does not identify an approver.",
            "repair_test": "Locate one named approver and their acceptance criterion for the venue.",
            "context_evidence": [
                {"block_id": blocks[0].block_id, "quote": blocks[0].text, "role": "context"},
                {"block_id": blocks[-1].block_id, "quote": blocks[-1].text, "role": "qualification"},
            ],
        }

    def test_context_quotes_survive_import_reload_and_export(self):
        request = self.helper.request()
        finding = self.helper.finding()
        finding["check_data"] = {"close_reading": self.detail()}
        run = self.helper.collect(request, self.helper.response(request, finding))
        self.assertEqual(self.project.findings()[0].check_data["close_reading"], self.detail())
        record = self.project._audit_run_records()[0][1]
        self.assertEqual(record["close_reading_receipt"]["findings_with_checked_context_quotes"], [run.findings[0].finding_id])
        self.assertEqual(record["close_reading_receipt"]["semantic_accuracy"], "not-established-by-structural-validation")
        exported = self.project.export_ai_reviews()
        report = "\n".join(path.read_text(encoding="utf-8") for path in exported.glob("*.md"))
        self.assertIn(self.detail()["strongest_defense"], report)
        self.assertIn(self.detail()["repair_test"], report)
        self.assertEqual(self.project.integrity_errors(), [])

    def test_no_dossier_is_preserved_without_inventing_context_review(self):
        request = self.helper.request()
        self.helper.collect(request, self.helper.response(request))
        receipt = self.project._audit_run_records()[0][1]["close_reading_receipt"]
        self.assertEqual(receipt["findings_with_checked_context_quotes"], [])
        self.assertEqual(len(receipt["findings_without_close_reading"]), 1)

    def test_hallucinated_cross_version_or_invalid_context_never_enters_audit(self):
        request = self.helper.request()
        bad_details = [None, [], {}, {**self.detail(), "verified": True}]
        for field, bad in (("block_id", "V2:wrong"), ("block_id", {}),
                           ("quote", "invented promise of unlimited funding"),
                           ("quote", None), ("role", []), ("role", "verified")):
            detail = self.detail()
            detail["context_evidence"][0][field] = bad
            bad_details.append(detail)
        for bad in (None, [], 1, [None], [{}]):
            bad_details.append({**self.detail(), "context_evidence": bad})
        duplicate = self.detail()
        duplicate["context_evidence"].append(copy.deepcopy(duplicate["context_evidence"][0]))
        bad_details.append(duplicate)
        for detail in bad_details:
            with self.subTest(detail=detail):
                finding = self.helper.finding()
                finding["check_data"] = {"close_reading": detail}
                with self.assertRaisesRegex(studio.ReviewStudioError, "细读|close_reading"):
                    self.helper.collect(request, self.helper.response(request, finding))
        self.assertEqual(self.project._audit_run_records(), [])

    def test_context_accepts_eight_languages_without_translation_or_character_loss(self):
        document = self.project.document()
        block = next(b for b in document.blocks if b.text == test_review_round_protocols.LANGUAGES)
        detail = self.detail()
        detail["context_evidence"] = [{"block_id": block.block_id,
                                        "quote": unicodedata.normalize("NFD", block.text), "role": "support"}]
        self.assertEqual(validate_close_reading(detail, {b.block_id: b for b in document.blocks}), [])
        detail["context_evidence"][0]["quote"] = "An English translation replacing the original languages."
        self.assertTrue(validate_close_reading(detail, {b.block_id: b for b in document.blocks}))

    def test_new_prompt_asks_for_counterevidence_without_claiming_a_quality_score(self):
        request = self.helper.request()
        prompt = request["prompt"]
        for concept in ("strongest_defense", "why_defense_fails", "repair_test", "context_evidence",
                        "An absent keyword is not an absent argument", "untrusted subject matter",
                        "Do not demand a different kind of article", "Do not pad"):
            # Case is editorial, protocol behavior is the condition being checked.
            self.assertIn(concept.casefold(), prompt.casefold())
        self.assertEqual(request["close_reading_protocol_version"], 1)
        self.assertIn("not a private reasoning transcript", prompt)

    def test_retest_keeps_original_protocol_across_software_change(self):
        request = self.helper.request()
        old = copy.deepcopy(request["critic_protocol"])
        run = self.helper.collect(request, self.helper.response(request))
        finding = run.findings[0]
        self.project.decide_finding(finding.finding_id, "accept", reason="补充验收责任")
        action = self.project.prepare_revision_plan()["actions"][0]
        self.project.set_revision_action_operation(action["action_id"], action["operation_suggestion"], reason="确认")
        hunk = self.project.propose_revision_hunk(action["action_id"], "负责人：项目经理", rationale="明确责任")
        self.project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="核对")
        replacement = {**old, "objective": "NEW SOFTWARE STANDARD MUST NOT BE RETROACTIVE"}
        with patch.dict(studio.CRITIC_PROTOCOLS, {request["critic"]: replacement}):
            revision_dir = self.project.finalize_revision()
            revision = json.loads((revision_dir / "revision.json").read_text(encoding="utf-8"))
            external = self.project.external_recheck_status(revision["revision_id"])["requests"][0]
        self.assertEqual(external["critic_protocol"], old)
        self.assertNotIn(replacement["objective"], external["prompt"])
        self.assertIn(old["objective"], external["prompt"])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_legacy_request_recovers_protocol_from_its_immutable_prompt(self):
        request = self.helper.request()
        legacy = {key: value for key, value in request.items() if key not in
                  {"critic_protocol", "critic_protocol_sha256", "close_reading_protocol_version"}}
        self.assertEqual(self.project._snapshotted_critic_protocol(legacy, request["prompt"].encode("utf-8")), request["critic_protocol"])
        with self.assertRaises(studio.ReviewStudioError):
            self.project._snapshotted_critic_protocol(legacy, b"missing protocol")

    def test_recheck_new_finding_context_must_bind_revised_document(self):
        _, revision, request, payload = self.helper.followup(start=False)
        revision_path = self.project._revision_directory(revision["revision_id"])
        document = json.loads((revision_path / "document.json").read_text(encoding="utf-8"))
        block = next(b for b in document["blocks"] if "负责人" in b["text"])
        finding = self.helper.finding(identity="NEW")
        finding["location"] = {"block_id": block["block_id"]}
        finding["evidence"] = block["text"]
        detail = self.detail()
        detail["context_evidence"] = [{"block_id": block["block_id"], "quote": "text that is absent", "role": "context"}]
        finding["check_data"] = {"close_reading": detail}
        payload["new_findings"] = [finding]
        with self.assertRaisesRegex(studio.ReviewStudioError, "close_reading|细读"):
            self.project.collect_external_recheck(revision["revision_id"], request["critic"], json.dumps(payload), provider="provider", model="model")
        detail["context_evidence"][0]["quote"] = block["text"]
        result = self.project.collect_external_recheck(revision["revision_id"], request["critic"], json.dumps(payload), provider="provider", model="model")
        self.assertEqual(len(result["new_findings"]), 1)


if __name__ == "__main__":
    unittest.main()
