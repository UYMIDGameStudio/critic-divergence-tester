"""Real persisted adversarial sessions and adversarial-input regressions."""

from __future__ import annotations

import copy
import json
import unicodedata
import unittest
from unittest.mock import patch

import document_review_studio as studio
from document_review_adversarial import MAX_RESPONSE_BYTES, response_example, validate_response
from project_lifecycle import CommitValidationResult
from test import test_review_round_protocols

LANGUAGES = test_review_round_protocols.LANGUAGES
CRITIC = test_review_round_protocols.CRITIC


class AdversarialReviewTests(unittest.TestCase):
    def setUp(self):
        self.helper = test_review_round_protocols.ReviewRoundProtocolTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.project = self.helper.project
        request = self.helper.request()
        run = self.helper.collect(request, self.helper.response(request))
        self.finding_id = run.findings[0].finding_id

    def prepare(self):
        return self.project.prepare_adversarial_review(self.finding_id, provider="defense-provider", model="defense-model")

    def response(self, session, stage="defense", *, disposition="narrow"):
        request = next(item for item in session["requests"] if item["stage"] == stage)
        value = copy.deepcopy(request["response_example"])
        block = next(block for block in self.project._review_document_record()[1].blocks if block.text == LANGUAGES)
        value["result"]["context_evidence"][0].update(block_id=block.block_id, quote=block.text)
        if stage == "assessment":
            value["result"]["disposition"] = disposition
            if disposition == "withdraw":
                value["result"]["remaining_issue"] = ""
                value["result"]["minimal_repair"] = ""
        return value

    def collect(self, session, response=None, *, stage="defense"):
        request = next(item for item in session["requests"] if item["stage"] == stage)
        response = self.response(session, stage) if response is None else response
        raw = json.dumps(response, ensure_ascii=False) if isinstance(response, dict) else response
        return self.project.collect_adversarial_response(session["session_id"], raw, request_id=request["request_id"])

    def assess(self, session):
        return self.project.prepare_adversarial_assessment(session["session_id"], provider="assessment-provider", model="assessment-model")

    def assert_tracked_write_rollback(self, operation):
        before = {path.relative_to(self.project.root).as_posix(): path.read_bytes()
                  for path in self.project.root.rglob("*") if path.is_file()}
        saved_sessions = self.project.adversarial_reviews()
        original_write = studio._write_tracked
        writes = []

        def fail_after_first_write(root, path, data, **kwargs):
            if writes:
                raise OSError("injected adversarial tracked-write failure")
            original_write(root, path, data, **kwargs)
            writes.append(path.relative_to(root).as_posix())

        with patch("document_review_studio._write_tracked", side_effect=fail_after_first_write):
            with self.assertRaisesRegex(OSError, "injected adversarial"):
                operation()
        self.assertEqual(len(writes), 1, "failure must follow a successful artifact/index write")
        after = {path.relative_to(self.project.root).as_posix(): path.read_bytes()
                 for path in self.project.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before, "all artifacts, receipts, index, events and prior prompt bytes must roll back")
        self.assertFalse((self.project.root / ".recovery").exists())
        self.assertEqual(self.project.adversarial_reviews(), saved_sessions)
        self.assertEqual(self.project.integrity_errors(), [])

    def test_history_reads_are_bounded_and_refresh_after_a_new_session(self):
        sessions = [self.prepare()]
        for _ in range(5):
            sessions.append(self.project.prepare_adversarial_review(
                self.finding_id, provider="defense-provider", model="defense-model", restart=True))
        original_read = studio._read_json
        reads = []

        def record_read(path):
            if path.name == "session.json" and path.parent.parent.name == "adversarial-reviews":
                reads.append(path)
            return original_read(path)

        with patch("document_review_studio._read_json", side_effect=record_read):
            history = self.project.adversarial_reviews()
        self.assertEqual({item["session_id"] for item in history}, {item["session_id"] for item in sessions})
        self.assertEqual([item["session_id"] for item in history if item["current"]], [sessions[-1]["session_id"]])
        self.assertLessEqual(len(reads), 2 * len(sessions), "Viewing history must not rescan all sessions for every row")
        replacement = self.project.prepare_adversarial_review(
            self.finding_id, provider="replacement", model="replacement", restart=True)
        refreshed = self.project.adversarial_reviews()
        self.assertEqual(len(refreshed), len(sessions) + 1)
        self.assertEqual([item["session_id"] for item in refreshed if item["current"]], [replacement["session_id"]])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_partial_restart_write_rolls_back_without_superseding_prior_session(self):
        original = self.prepare()
        restart = lambda: self.project.prepare_adversarial_review(
            self.finding_id, provider="replacement-provider", model="replacement-model", restart=True)
        self.assert_tracked_write_rollback(restart)
        self.assertTrue(self.project.adversarial_reviews()[0]["current"])
        retried = restart()
        self.assertEqual(retried["supersedes_session_id"], original["session_id"])
        self.assertEqual([session["current"] for session in self.project.adversarial_reviews()], [False, True])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_partial_accepted_response_write_rolls_back_then_same_response_retries(self):
        session = self.prepare()
        response = self.response(session)
        self.assert_tracked_write_rollback(lambda: self.collect(session, response))
        self.assertEqual(self.project.adversarial_reviews()[0]["status"], "awaiting_defense")
        self.assertEqual(self.collect(session, response)["status"], "defense_ready")
        self.assertEqual(self.project.findings()[0].status, "open")
        self.assertEqual(self.project.integrity_errors(), [])

    def test_partial_assessment_request_write_rolls_back_preserving_defense(self):
        session = self.collect(self.prepare())
        self.assert_tracked_write_rollback(lambda: self.assess(session))
        self.assertEqual(self.project.adversarial_reviews()[0]["status"], "defense_ready")
        retried = self.assess(session)
        self.assertEqual(retried["defense"], session["defense"])
        self.assertEqual(retried["status"], "awaiting_assessment")
        self.assertEqual(self.project.integrity_errors(), [])

    def test_complete_workflow_preserves_human_authority_and_history(self):
        initial = self.project.findings()[0].to_dict()
        session = self.prepare()
        self.assertEqual(session["status"], "awaiting_defense")
        self.assertEqual(self.prepare()["session_id"], session["session_id"])
        self.assertIn(LANGUAGES, session["requests"][0]["prompt"])
        self.assertIn("fresh conversation", session["requests"][0]["prompt"])
        self.assertEqual(self.project.findings()[0].to_dict(), initial)
        with self.assertRaisesRegex(studio.ReviewStudioError, "先导入"):
            self.assess(session)
        session = self.collect(session)
        self.assertEqual(session["status"], "defense_ready")
        session = self.assess(session)
        self.assertEqual(session["status"], "awaiting_assessment")
        self.assertIn(session["defense"]["strongest_defense"], session["requests"][1]["prompt"])
        session = self.collect(session, self.response(session, "assessment", disposition="withdraw"), stage="assessment")
        self.assertEqual(session["status"], "completed")
        self.assertEqual(session["assessment"]["disposition"], "withdraw")
        self.assertEqual(self.project.findings()[0].to_dict(), initial)
        reloaded = studio.DocumentReviewProject(self.project.root)
        self.assertEqual(reloaded.adversarial_reviews()[0], session)
        self.assertEqual(self.project.integrity_errors(), [])
        self.assertNotEqual(self.prepare()["session_id"], session["session_id"])
        self.assertEqual(len(self.project.adversarial_reviews()), 2)

    def test_all_assessment_outcomes_remain_proposals(self):
        for disposition in ("retain", "narrow", "withdraw", "insufficient"):
            with self.subTest(disposition=disposition):
                session = self.assess(self.collect(self.prepare()))
                result = self.collect(session, self.response(session, "assessment", disposition=disposition), stage="assessment")
                self.assertEqual(result["assessment"]["disposition"], disposition)
                self.assertEqual(self.project.findings()[0].status, "open")

    def test_rejected_responses_archive_once_then_recover(self):
        session = self.prepare()
        rejected = ['not json', b'\xff', '{"result": {}, "result": {}}', '[]', 'NaN', '',
                    '{"\\ud800":1,"\\ud800":2}']
        for field in ("request_id", "session_id", "stage", "prompt_sha256", "source_sha256", "provider", "model"):
            value = self.response(session)
            value[field] = "wrong"
            rejected.append(value)
        for bad in rejected:
            with self.subTest(bad=bad):
                with self.assertRaises(CommitValidationResult):
                    self.collect(session, bad)
                count = self.project.adversarial_reviews()[0]["rejected_attempts"]
                with self.assertRaises(CommitValidationResult):
                    self.collect(session, bad)
                self.assertEqual(self.project.adversarial_reviews()[0]["rejected_attempts"], count)
                self.assertEqual(self.project.adversarial_reviews()[0]["status"], "awaiting_defense")
        self.assertEqual(self.collect(session)["status"], "defense_ready")
        self.assertEqual(self.project.integrity_errors(), [])

    def test_fabricated_quotes_extra_controls_and_invalid_types_rejected(self):
        session = self.prepare()
        mutations = [
            lambda result: result.update(status="accept"),
            lambda result: result.update(verified=True),
            lambda result: result.update(strongest_defense="\ud800"),
            lambda result: result.update(author_position=[]),
            lambda result: result.update(limitations=""),
            lambda result: result["context_evidence"][0].update(quote="invented funding commitment"),
            lambda result: result["context_evidence"][0].update(block_id="another-document:block"),
            lambda result: result["context_evidence"][0].update(role=[]),
            lambda result: result["context_evidence"][0].update(evidence_id="D0"),
            lambda result: result["context_evidence"].append(copy.deepcopy(result["context_evidence"][0])),
        ]
        for mutate in mutations:
            value = self.response(session)
            mutate(value["result"])
            raw = json.dumps(value)  # Preserve invalid surrogate as escaped JSON.
            with self.subTest(raw=raw), self.assertRaises(CommitValidationResult):
                self.collect(session, raw)
        self.assertEqual(self.project.findings()[0].status, "open")
        self.assertEqual(self.collect(session)["status"], "defense_ready")

    def test_assessment_must_address_real_defense_evidence(self):
        session = self.assess(self.collect(self.prepare()))
        for references in ([], ["D999"], ["D1", "D1"], [False], "D1"):
            value = self.response(session, "assessment")
            value["result"]["defense_evidence_ids"] = references
            with self.subTest(references=references), self.assertRaises(CommitValidationResult):
                self.collect(session, value, stage="assessment")
        value = self.response(session, "assessment")
        value["result"]["disposition"] = "resolved"
        with self.assertRaises(CommitValidationResult):
            self.collect(session, value, stage="assessment")
        value = self.response(session, "assessment")
        value["result"]["disposition"] = "withdraw"
        with self.assertRaises(CommitValidationResult):
            self.collect(session, value, stage="assessment")
        self.assertEqual(self.collect(session, stage="assessment")["status"], "completed")

    def test_stage_and_session_replay_is_bound_and_idempotent(self):
        session = self.prepare()
        defense = self.response(session)
        accepted = self.collect(session, defense)
        file_count = len(list(self.project.root.rglob("*")))
        self.assertEqual(self.collect(session, defense), accepted)
        self.assertEqual(len(list(self.project.root.rglob("*"))), file_count)
        defense["result"]["strongest_defense"] = "A changed answer"
        with self.assertRaisesRegex(studio.ReviewStudioError, "不能覆盖"):
            self.collect(session, defense)
        with self.assertRaisesRegex(studio.ReviewStudioError, "不属于"):
            self.project.collect_adversarial_response(session["session_id"], "{}", request_id="wrong")
        with self.assertRaises(studio.ReviewStudioError):
            self.project.collect_adversarial_response("../outside", "{}", request_id="wrong")
        session = self.assess(accepted)
        value = self.response(session, "assessment")
        value["stage"] = "defense"
        with self.assertRaises(CommitValidationResult):
            self.collect(session, value, stage="assessment")

    def test_human_decision_does_not_invalidate_but_location_correction_does(self):
        session = self.prepare()
        self.project.decide_finding(self.finding_id, "reject", reason="作者不接受原批评")
        self.assertTrue(self.project.adversarial_reviews()[0]["current"])
        session = self.collect(session)
        self.assertEqual(self.project.findings()[0].status, "reject")
        block = self.project.document().blocks[-1]
        self.project.correct_finding_location(self.finding_id, block.block_id, reason="定位到具体说明")
        self.assertFalse(self.project.adversarial_reviews()[0]["current"])
        with self.assertRaisesRegex(studio.ReviewStudioError, "过期"):
            self.assess(session)
        with self.assertRaisesRegex(studio.ReviewStudioError, "过期"):
            self.collect(session)
        self.assertNotEqual(self.prepare()["session_id"], session["session_id"])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_explicit_restart_preserves_and_supersedes_pending_or_accepted_stage(self):
        session = self.prepare()
        old_response = self.response(session)
        for invalid in (None, 1, "true", []):
            with self.assertRaisesRegex(studio.ReviewStudioError, "布尔"):
                self.project.prepare_adversarial_review(self.finding_id, provider="p", model="m", restart=invalid)
        next_session = self.project.prepare_adversarial_review(self.finding_id, provider="corrected", model="corrected", restart=True)
        self.assertEqual(next_session["supersedes_session_id"], session["session_id"])
        self.assertFalse(self.project.adversarial_reviews()[0]["current"])
        with self.assertRaisesRegex(studio.ReviewStudioError, "过期"):
            self.collect(session, old_response)
        accepted = self.collect(next_session)
        latest = self.project.prepare_adversarial_review(self.finding_id, provider="p", model="m", restart=True)
        self.assertEqual(latest["supersedes_session_id"], accepted["session_id"])
        with self.assertRaisesRegex(studio.ReviewStudioError, "过期"):
            self.assess(accepted)
        self.assertEqual([item["current"] for item in self.project.adversarial_reviews()], [False, False, True])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_new_active_audit_stales_original_challenge(self):
        session = self.prepare()
        request = self.helper.request()
        self.helper.collect(request, self.helper.response(request))
        self.assertFalse(self.project.adversarial_reviews()[0]["current"])
        with self.assertRaisesRegex(studio.ReviewStudioError, "过期"):
            self.collect(session)

    def test_restart_chain_follows_parents_when_clock_moves_backwards(self):
        initial = self.prepare()
        with patch("document_review_studio._now", return_value="2000-01-01T00:00:00+00:00"):
            earlier_clock = self.project.prepare_adversarial_review(self.finding_id, provider="p", model="m", restart=True)
        final = self.project.prepare_adversarial_review(self.finding_id, provider="p", model="m", restart=True)
        self.assertEqual(earlier_clock["supersedes_session_id"], initial["session_id"])
        self.assertEqual(final["supersedes_session_id"], earlier_clock["session_id"])
        self.assertEqual([item["session_id"] for item in self.project.adversarial_reviews() if item["current"]], [final["session_id"]])

    def test_protocol_snapshot_survives_software_template_change(self):
        session = self.prepare()
        original = copy.deepcopy(session["adversarial_protocol"])
        with patch.dict("document_review_stores.adversarial.ADVERSARIAL_PROTOCOL", {"version": 99, "purpose": "changed"}):
            session = self.assess(self.collect(session))
        self.assertEqual(session["adversarial_protocol"], original)
        self.assertIn(original["purpose"], session["requests"][1]["prompt"])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_pending_contract_version_and_examples_survive_changed_defaults(self):
        session = self.prepare()
        original_example = copy.deepcopy(session["requests"][0]["response_example"])
        # A later software default must not reinterpret an already issued v1
        # response, or change the follow-on assessment's response contract.
        def future_default(stage, *, version=2):
            return response_example(stage, version=version)
        with patch("document_review_stores.adversarial.response_example", side_effect=future_default), patch("document_review_stores.adversarial.RESPONSE_CONTRACT_VERSION", 2):
            self.assertEqual(self.project.adversarial_reviews()[0]["requests"][0]["response_example"], original_example)
            session = self.assess(self.collect(session))
            self.assertEqual(session["requests"][1]["response_contract_version"], 1)
            self.collect(session, stage="assessment")
        for version in (None, True, 1.0, 2, "1"):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "Unsupported"):
                validate_response(b"{}", {}, {}, version=version)

    def test_followup_round_uses_its_original_critic_even_after_new_audit(self):
        stale = self.prepare()
        original_request, revision, followup = self.helper.followup()
        self.assertFalse(self.project.adversarial_reviews()[0]["current"])
        with self.assertRaisesRegex(studio.ReviewStudioError, "过期"):
            self.collect(stale)
        carried = next(item for item in self.project.findings() if item.origin == "external-recheck-carried-forward")
        changed = {**studio.CRITIC_PROTOCOLS[CRITIC], "objective": "new unrelated reviewer standard"}
        with patch.dict(studio.CRITIC_PROTOCOLS, {CRITIC: changed}):
            request = self.helper.request()
            self.helper.collect(request, self.helper.response(request))
            session = self.project.prepare_adversarial_review(carried.finding_id, provider="p", model="m")
        self.assertEqual(session["source_sha256"], revision["revised_sha256"])
        self.assertEqual(session["review_round_id"], followup["round_id"])
        self.assertEqual(session["critic_origin"]["original_request_id"], original_request["request_id"])
        self.assertNotEqual(session["critic_origin"]["critic_protocol"]["objective"], changed["objective"])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_new_review_round_stales_session_even_when_rendered_source_is_unchanged(self):
        context = self.project.context().to_dict()
        self.project = studio.DocumentReviewProject.create(
            self.helper.directory.name, filename="unchanged-round.md", content=(LANGUAGES + "\n").encode("utf-8"))
        self.helper.project = self.project
        self.project.confirm_extraction("confirm")
        self.project.confirm_context(context)
        original_request = self.helper.request()
        original = self.helper.collect(original_request, self.helper.response(original_request)).findings[0]
        self.finding_id = original.finding_id
        session = self.assess(self.collect(self.prepare()))
        pending_response = self.response(session, "assessment")
        _, _, old_binding = self.project._current_review_binding()
        self.project.decide_finding(self.finding_id, "accept", reason="记录问题并进入复审")
        action = self.project.prepare_revision_plan()["actions"][0]
        self.project.set_revision_action_operation(action["action_id"], "replace_block", reason="明确替换范围")
        # The approved whitespace change normalizes to exactly the same source
        # bytes; a new review round must still invalidate the old stage request.
        hunk = self.project.propose_revision_hunk(action["action_id"], action["before_text"] + "\n", rationale="调整末尾换行")
        self.project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="保留原意")
        revision_dir = self.project.finalize_revision()
        revision = json.loads((revision_dir / "revision.json").read_text(encoding="utf-8"))
        self.assertEqual(revision["revised_sha256"], old_binding["source_sha256"])
        external = self.project.external_recheck_status(revision["revision_id"])["requests"][0]
        payload = {**{key: external[key] for key in ("request_id", "prompt_sha256", "critic")},
                   "revision_id": revision["revision_id"], "revised_sha256": revision["revised_sha256"],
                   "resolutions": [{"finding_id": self.finding_id, "state": "still-present",
                                    "reason": "实质问题尚未修改", "evidence": LANGUAGES}], "new_findings": []}
        result = self.project.collect_external_recheck(revision["revision_id"], CRITIC, json.dumps(payload, ensure_ascii=False), provider="p", model="m")
        self.project.decide_external_resolution(revision["revision_id"], result["result_id"], self.finding_id, "unresolved", reason="下一轮继续")
        followup = self.project.start_followup_round(revision["revision_id"])
        _, _, new_binding = self.project._current_review_binding()
        self.assertEqual(new_binding["source_sha256"], old_binding["source_sha256"])
        self.assertEqual(new_binding["review_round_id"], followup["round_id"])
        self.assertNotEqual(new_binding["review_round_id"], old_binding["review_round_id"])
        self.assertFalse(self.project._belongs_to_current_review(session, {**old_binding, "review_round_id": new_binding["review_round_id"]}))
        self.assertFalse(self.project.adversarial_reviews()[0]["current"])
        with self.assertRaisesRegex(studio.ReviewStudioError, "过期"):
            self.collect(session, pending_response, stage="assessment")
        with self.assertRaisesRegex(studio.ReviewStudioError, "过期"):
            self.assess(session)
        self.assertEqual(self.project.integrity_errors(), [])

    def test_eight_languages_multiline_and_canonical_unicode_are_preserved(self):
        session = self.prepare()
        response = self.response(session)
        response["result"]["author_position"] = LANGUAGES + "\n\n作者的限制條件。\n限定がある。"
        response["result"]["context_evidence"][0]["quote"] = unicodedata.normalize("NFD", LANGUAGES).replace(" | ", "\n| ")
        session = self.collect(session, response)
        self.assertEqual(session["defense"], response["result"])
        self.assertEqual(studio.DocumentReviewProject(self.project.root).adversarial_reviews()[0]["defense"], response["result"])

    def test_direct_read_detects_tampered_challenge_parent(self):
        session = self.prepare()
        path = self.project.root / session["challenge_parent"]["relative_path"]
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(studio.ReviewStudioError, "完整性"):
            self.project.adversarial_reviews()

    def test_stage_results_and_prompt_are_immutable_tracked_parents(self):
        session = self.assess(self.collect(self.prepare()))
        request_path = self.project.root / session["requests"][1]["relative_path"]
        receipt = json.loads((request_path.parent / ".integrity" / "request.json.json").read_text(encoding="utf-8"))
        self.assertIn("independent-defense-result", {parent["role"] for parent in receipt["parents"]})
        defense_path = request_path.parent.parent / "defense" / "result.json"
        defense_path.write_bytes(defense_path.read_bytes() + b" ")
        with self.assertRaisesRegex(studio.ReviewStudioError, "完整性"):
            self.project.adversarial_reviews()

    def test_limits_and_local_findings_cannot_bypass_protocol(self):
        session = self.prepare()
        with self.assertRaisesRegex(studio.ReviewStudioError, "1 MiB"):
            self.collect(session, b"x" * (MAX_RESPONSE_BYTES + 1))
        for provider, model in (("", "m"), ("p", []), ("x" * 201, "m"), ("p\n", "m")):
            with self.subTest(provider=provider), self.assertRaises(studio.ReviewStudioError):
                self.project.prepare_adversarial_review(self.finding_id, provider=provider, model=model)
        local = self.project.run_local_prechecks([CRITIC])[0].findings[0]
        with self.assertRaisesRegex(studio.ReviewStudioError, "独立 AI"):
            self.project.prepare_adversarial_review(local.finding_id, provider="p", model="m")


if __name__ == "__main__":
    unittest.main()
