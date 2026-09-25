"""Real persisted review rounds and untrusted model-response regressions."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest

from document_review_studio import DocumentReviewProject, ReviewStudioError


LANGUAGES = "English | 简体中文 | 繁體中文 | Straße für Grüße | École française | 日本語かなカナ | Русский язык | Lingua Latīna æ œ"
CRITIC = "execution_feasibility"


class ReviewRoundProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = DocumentReviewProject.create(
            self.directory.name, filename="draft.md",
            content=("# 活动\n\n" + LANGUAGES + "\n\n| 项目 | 说明 |\n| --- | --- |\n| 场地 | 两个  空格 |\n").encode("utf-8"),
        )
        self.project.confirm_extraction("confirm")
        self.project.confirm_context({
            "document_type": "活动策划案", "jurisdiction": "中国大陆",
            "effective_date": "2026-09-01", "publisher_type": "社会组织",
            "audience": "参与者", "involves_minors": False, "involves_fees": False,
            "involves_sponsorship": False, "involves_contract": False,
            "involves_personal_information": False, "involves_intellectual_property": False,
            "publication_status": "internal-draft",
        })

    def request(self, critic: str = CRITIC) -> dict:
        return self.project.prepare_ai_audits([critic], provider="provider", model="model")[0]

    def finding(self, *, document=None, block=None, identity="F1") -> dict:
        document = document or self.project._review_document_record()[1]
        block = block or document.blocks[0]
        return {
            "finding_id": identity, "critic": CRITIC, "document_type": "活动策划案",
            "location": block.location.to_dict(), "evidence": block.text,
            "issue": "执行要求需要补充", "standard": "有明确负责人及验收人",
            "consequence": "无人承担交付和验收责任", "severity": "medium",
            "verification_state": "model-proposed", "external_basis": {"unresolved_facts": []},
            "uncertainties": [], "suggested_action": "补充负责人及验收人",
            "suggested_owner": "文档负责人", "blocks_release_or_execution": False,
        }

    def response(self, request, finding=None) -> dict:
        return {
            **{key: request[key] for key in ("request_id", "prompt_sha256", "provider", "model", "critic", "source_sha256")},
            "findings": [finding or self.finding()],
        }

    def collect(self, request, payload, **kwargs):
        return self.project.collect_model_audit(
            request["critic"], json.dumps(payload, ensure_ascii=False),
            provider="provider", model="model", request_id=request["request_id"], **kwargs,
        )

    def followup(self, *, start=True, revised_text=None):
        initial_request = self.request()
        original_run = self.collect(initial_request, self.response(initial_request))
        original = original_run.findings[0]
        self.project.decide_finding(original.finding_id, "accept", reason="需要补充")
        action = self.project.prepare_revision_plan()["actions"][0]
        self.project.set_revision_action_operation(action["action_id"], action["operation_suggestion"], reason="确认")
        hunk = self.project.propose_revision_hunk(action["action_id"], revised_text if revised_text is not None else "负责人：项目经理；" + LANGUAGES, rationale="明确责任")
        self.project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="核对修改")
        revision_dir = self.project.finalize_revision()
        revision = json.loads((revision_dir / "revision.json").read_text(encoding="utf-8"))
        external = self.project.external_recheck_status(revision["revision_id"])["requests"][0]
        payload = {
            **{key: external[key] for key in ("request_id", "prompt_sha256", "critic")},
            "revision_id": revision["revision_id"], "revised_sha256": revision["revised_sha256"],
            "resolutions": [{"finding_id": original.finding_id, "state": "still-present",
                             "reason": "尚无验收人", "evidence": "负责人：项目经理"}],
            "new_findings": [],
        }
        if not start:
            return initial_request, revision, external, payload
        result = self.project.collect_external_recheck(revision["revision_id"], CRITIC, json.dumps(payload, ensure_ascii=False), provider="provider", model="recheck-model")
        self.project.decide_external_resolution(revision["revision_id"], result["result_id"], original.finding_id, "unresolved", reason="下一轮继续")
        followup = self.project.start_followup_round(revision["revision_id"])
        return initial_request, revision, followup

    def test_followup_protocol_import_and_export_bind_current_ir(self):
        _, revision, followup = self.followup()
        carried_ids = {f.finding_id for f in self.project.findings()}
        request = self.request()
        self.assertEqual(request["source_sha256"], revision["revised_sha256"])
        self.assertEqual(request["review_round_id"], followup["round_id"])
        document_path, document = self.project._review_document_record()
        self.assertEqual(request["document_sha256"], hashlib.sha256(document_path.read_bytes()).hexdigest())
        self.assertIn("负责人：项目经理", request["prompt"])
        self.assertIn(LANGUAGES, request["prompt"])
        self.assertIn('historical-parent-document', request['prompt'])
        run = self.collect(request, self.response(request))
        self.assertEqual(run.source_sha256, revision["revised_sha256"])
        self.assertEqual(run.document_id, document.document_id)
        self.assertEqual({f.finding_id for f in self.project.findings()}, carried_ids | {run.findings[0].finding_id})
        self.assertEqual(self.project._critic_origin_binding(CRITIC)["original_request_id"], request["request_id"])
        output = self.project.export_ai_reviews()
        snapshot = json.loads((output / "AI审查结果.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["source"]["sha256"], revision["revised_sha256"])
        self.assertEqual(snapshot["runs"][0]["review_round_id"], followup["round_id"])
        for finding in self.project.findings():
            self.project.decide_finding(finding.finding_id, "defer", reason="保留到后续处理")
        self.project.export()
        self.assertEqual(self.project.integrity_errors(), [])

    def test_external_recheck_can_locate_generated_blocks_using_only_its_prompt(self):
        original_ids = {b.block_id for b in self.project.document().blocks}
        _, revision, request, payload = self.followup(start=False, revised_text='负责人：项目经理\n\nGenerated evidence paragraph')
        serialized = request['prompt'].split('## Revised document blocks\n```json\n', 1)[1].split('\n```', 1)[0]
        blocks = json.loads(serialized)
        generated = next(b for b in blocks if b['text'] == 'Generated evidence paragraph')
        self.assertNotIn(generated['block_id'], original_ids)
        finding = self.finding(identity='GENERATED-FINDING')
        finding.update(location=generated['location'], evidence=generated['text'])
        payload['new_findings'] = [finding]
        result = self.project.collect_external_recheck(revision['revision_id'], CRITIC, json.dumps(payload, ensure_ascii=False), provider='provider', model='recheck-model')
        self.assertEqual(result['new_findings'][0]['location']['block_id'], generated['block_id'])
        self.assertIn('historical-parent-document', request['prompt'])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_followup_without_fresh_audit_can_export_valid_round(self):
        self.followup()
        for finding in self.project.findings():
            self.project.decide_finding(finding.finding_id, "defer", reason="后续处理")
        output = self.project.export()
        self.assertTrue((output / "audit.json").is_file())
        self.assertEqual(self.project.integrity_errors(), [])

    def test_followup_precheck_scans_current_document(self):
        _, revision, followup = self.followup()
        inherited = {f.finding_id for f in self.project.findings()}
        run = self.project.run_local_prechecks([CRITIC])[0]
        self.assertEqual(run.source_sha256, revision["revised_sha256"])
        self.assertNotIn("execution.owner", {f.check_id for f in run.findings})
        self.assertTrue(inherited <= {f.finding_id for f in self.project.findings()})
        stored = self.project._active_audit_run_records()[CRITIC][1]
        self.assertEqual(stored["review_round_id"], followup["round_id"])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_stale_previous_round_request_cannot_be_manually_associated(self):
        stale = self.request("expression_ambiguity")
        self.followup()
        self.assertEqual(self.project.ai_requests(), [])
        self.assertEqual(self.project.state()["review_state"], "followup_round_ready")
        self.assertEqual(self.project.state()["ai_review_state"], "not_started")
        payload = {"critic": stale["critic"], "findings": [], "zero_finding_basis": ["逐块核对主语与范围"]}
        with self.assertRaises(ReviewStudioError):
            self.collect(stale, payload, binding_mode="manual_association")

    def test_hallucinated_quote_cannot_bind_valid_block(self):
        request = self.request()
        value = self.finding()
        value["evidence"] = "The document guarantees unlimited refunds."
        with self.assertRaisesRegex(ReviewStudioError, "evidence|引文|证据"):
            self.collect(request, self.response(request, value))
        self.assertEqual(self.project._audit_run_records(), [])

    def test_wrong_location_metadata_is_rejected(self):
        request = self.request()
        for field, wrong in (("block_kind", "table_cell"), ("paragraph", 999), ("page", 8),
                             ("table_id", "other-table"), ("row", 5), ("column", 4),
                             ("source_path", "other-document.docx"), ("char_start", -1),
                             ("char_end", 500), ("bbox", [0, 0, 1, 1])):
            with self.subTest(field=field):
                value = self.finding()
                value["location"][field] = wrong
                with self.assertRaisesRegex(ReviewStudioError, "定位|location"):
                    self.collect(request, self.response(request, value))

    def test_wrong_document_type_is_rejected(self):
        request = self.request()
        value = self.finding()
        value["document_type"] = "different document"
        with self.assertRaisesRegex(ReviewStudioError, "document_type|文档类型"):
            self.collect(request, self.response(request, value))

    def test_model_cannot_supply_a_human_finding_decision(self):
        request = self.request()
        for status in ("accept", "reject", "correct", "defer", "resolved", None):
            with self.subTest(status=status):
                value = self.finding()
                value["status"] = status
                with self.assertRaisesRegex(ReviewStudioError, "status|人工裁决"):
                    self.collect(request, self.response(request, value))
        self.assertEqual(self.project.findings(), [])

    def test_quoted_excerpt_and_minimal_location_preserve_eight_languages(self):
        request = self.request()
        block = next(b for b in self.project.document().blocks if b.text == LANGUAGES)
        value = self.finding(block=block)
        value["location"] = {"block_id": block.block_id}
        value["evidence"] = "«" + LANGUAGES + "»"
        value["issue"] = LANGUAGES
        run = self.collect(request, self.response(request, value))
        self.assertEqual(run.findings[0].location, block.location)
        self.assertEqual(run.findings[0].evidence, value["evidence"])
        self.assertEqual(run.findings[0].issue, LANGUAGES)

    def test_table_whitespace_and_unicode_canonical_equivalence_are_valid(self):
        import unicodedata
        request = self.request()
        document = self.project.document()
        table_cell = next(b for b in document.blocks if b.text == "两个  空格")
        value = self.finding(block=table_cell)
        value["evidence"] = "两个\t空格"
        other = self.finding(block=next(b for b in document.blocks if b.text == LANGUAGES), identity="F2")
        other["evidence"] = unicodedata.normalize("NFD", LANGUAGES)
        payload = self.response(request, value)
        payload["findings"].append(other)
        self.assertEqual(len(self.collect(request, payload).findings), 2)

    def test_empty_findings_require_model_supplied_scope_basis(self):
        request = self.request()
        for invalid in (None, [], [" "], "reviewed", [{}], [42]):
            with self.subTest(value=invalid):
                payload = self.response(request)
                payload["findings"] = []
                if invalid is not None:
                    payload["zero_finding_basis"] = invalid
                with self.assertRaisesRegex(ReviewStudioError, "zero_finding_basis|审查范围"):
                    self.collect(request, payload)
        payload["zero_finding_basis"] = ["逐块检查了标题、参与者和表格中的责任边界；未发现可据此成立的执行问题。"]
        run = self.collect(request, payload)
        self.assertEqual(run.zero_finding_basis, payload["zero_finding_basis"])

    def test_manual_association_keeps_content_checks_without_inventing_echo(self):
        request = self.request()
        value = self.finding()
        value["evidence"] = "invented text"
        payload = {"critic": CRITIC, "findings": [value]}
        with self.assertRaisesRegex(ReviewStudioError, "evidence|引文|证据"):
            self.collect(request, payload, binding_mode="manual_association")
        payload["findings"] = [self.finding()]
        self.collect(request, payload, binding_mode="manual_association")
        binding = self.project._active_audit_run_records()[CRITIC][1]["response_binding"]
        self.assertFalse(binding["source_echo_verified"])
        self.assertTrue(binding["source_associated_by_application"])

    def test_external_manual_binding_never_overwrites_conflicting_envelope(self):
        _, revision, _, valid = self.followup(start=False)
        for field in ("request_id", "prompt_sha256", "revision_id", "revised_sha256", "critic"):
            with self.subTest(field=field):
                payload = {**valid, field: "wrong-previous-document"}
                with self.assertRaisesRegex(ReviewStudioError, "绑定|回显|匹配"):
                    self.project.collect_external_recheck(revision["revision_id"], CRITIC, json.dumps(payload), provider="provider", model="recheck-model", binding_mode="manual_association")
        for bad_envelope in ({"request_id": None}, {"request_id": valid["request_id"]},
                             {field: None for field in ("request_id", "prompt_sha256", "revision_id", "revised_sha256", "critic")}):
            payload = {key: value for key, value in valid.items() if key in {"resolutions", "new_findings"}}
            payload.update(bad_envelope)
            with self.assertRaises(ReviewStudioError):
                self.project.collect_external_recheck(revision["revision_id"], CRITIC, json.dumps(payload), provider="provider", model="recheck-model", binding_mode="manual_association")
        payload = {key: value for key, value in valid.items() if key in {"resolutions", "new_findings"}}
        result = self.project.collect_external_recheck(revision["revision_id"], CRITIC, json.dumps(payload), provider="provider", model="recheck-model", binding_mode="manual_association")
        self.assertEqual(result["response_binding"], "manual-association")
        self.assertEqual(result["revised_sha256"], revision["revised_sha256"])
        self.assertEqual(len(self.project._external_recheck_results(revision["revision_id"])), 1)

    def test_external_new_findings_cannot_reuse_original_quote_or_human_status(self):
        _, revision, _, valid = self.followup(start=False)
        original = self.finding(document=self.project.document())
        revised = json.loads((self.project.root / "revisions" / revision["revision_id"] / "document.json").read_text(encoding="utf-8"))
        current = {**original, "location": revised["blocks"][0]["location"], "evidence": revised["blocks"][0]["text"]}
        for change in ({"evidence": original["evidence"]}, {"evidence": "invented evidence"}, {"status": "reject"},
                       {"document_type": "different document"},
                       {"location": {**original["location"], "page": 99}}):
            with self.subTest(change=change):
                finding = {**current, **change}
                payload = {**valid, "new_findings": [finding]}
                with self.assertRaises(ReviewStudioError):
                    self.project.collect_external_recheck(revision["revision_id"], CRITIC, json.dumps(payload), provider="provider", model="recheck-model")
        payload = {**valid, "new_findings": [current]}
        result = self.project.collect_external_recheck(revision["revision_id"], CRITIC, json.dumps(payload), provider="provider", model="recheck-model")
        self.assertIn(LANGUAGES, result["new_findings"][0]["evidence"])
        self.assertEqual(len(self.project._external_recheck_results(revision["revision_id"])), 1)


if __name__ == "__main__":
    unittest.main()
