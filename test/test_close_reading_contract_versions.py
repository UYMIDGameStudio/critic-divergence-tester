"""Persisted close-reading contracts survive later prompt-template changes."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from unittest.mock import patch

import document_review_quality as quality
import document_review_studio as studio
from test import test_review_round_protocols


CONTRACT_MARKER = "## Contract and critic-specific protocol\n```json\n"
EXAMPLE_MARKER = "\n## Exact response shape\n"


def digest(value):
    return hashlib.sha256(studio.canonical_json(value)).hexdigest()


def replace_contract(prompt, transform):
    start = prompt.index(CONTRACT_MARKER) + len(CONTRACT_MARKER)
    contract, length = json.JSONDecoder().raw_decode(prompt[start:])
    transform(contract)
    return prompt[:start] + json.dumps(contract, ensure_ascii=False, indent=2) + prompt[start + length:]


class CloseReadingContractVersionTests(unittest.TestCase):
    def setUp(self):
        self.helper = test_review_round_protocols.ReviewRoundProtocolTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.project = self.helper.project

    def detail(self, block=None):
        block = block or self.project._review_document_record()[1].blocks[0]
        return {
            "author_position": "The author describes a bounded activity.",
            "strongest_defense": "The surrounding material could identify the approver.",
            "why_defense_fails": "The quoted passage still does not identify an approver.",
            "repair_test": "Identify a named approver and an acceptance criterion.",
            "context_evidence": [{"block_id": block.block_id, "quote": block.text, "role": "context"}],
        }

    def changed_current_protocol(self):
        return patch.dict(quality.CLOSE_READING_PROTOCOL, {
            "version": 99,
            "fields": {"future_explanation": "A field added by a later software version."},
        })

    def assert_v1_receipt(self, receipt, request, checked, missing=()):
        self.assertEqual(receipt["protocol_version"], 1)
        self.assertEqual(receipt["validation_version"], 1)
        self.assertEqual(receipt["protocol_binding"], "original-prompt")
        self.assertEqual(receipt["protocol_sha256"], digest(request["close_reading_protocol"]))
        self.assertEqual(receipt["findings_with_checked_context_quotes"], list(checked))
        self.assertEqual(receipt["findings_without_close_reading"], list(missing))
        self.assertEqual(receipt["semantic_accuracy"], "not-established-by-structural-validation")

    def persisted_historical_request(self, *, absent_contract=False):
        """Append a historical fixture once, preserving every tracked parent/hash.

        The first request supplies current document bindings. The fixture is a
        second immutable request, never a rewrite of that request or its ledger.
        """
        source = self.helper.request()
        prior_path, prior, prior_sha = self.project._ai_request_records(source["critic"])[-1]
        value = copy.deepcopy(prior)
        value["request_id"] += "-historical"
        value["request_sequence"] += 1
        value["previous_request_sha256"] = prior_sha
        for field in ("close_reading_protocol", "close_reading_protocol_sha256"):
            value.pop(field, None)
        base, example = source["prompt"].split(EXAMPLE_MARKER, 1)
        if absent_contract:
            value.pop("close_reading_protocol_version", None)
            base = replace_contract(base, lambda contract: contract.pop("close_reading_protocol"))
        value["prompt_sha256"] = hashlib.sha256(base.encode("utf-8")).hexdigest()
        example_start = example.index("```json\n") + len("```json\n")
        response, length = json.JSONDecoder().raw_decode(example[example_start:])
        response.update(request_id=value["request_id"], prompt_sha256=value["prompt_sha256"])
        if absent_contract:
            for finding in response["findings"]:
                finding.pop("check_data", None)
        example = example[:example_start] + json.dumps(response, ensure_ascii=False, indent=2) + example[example_start + length:]
        prompt = (base + EXAMPLE_MARKER + example).encode("utf-8")
        value["prompt_file_sha256"] = hashlib.sha256(prompt).hexdigest()
        directory = self.project.root / "ai-requests" / value["request_id"]
        prompt_path, request_path = directory / "prompt.md", directory / "request.json"
        parents = [studio._parent_ref(self.project.root, prior_path, role="previous-ai-request")]
        studio._write_tracked(self.project.root, prompt_path, prompt, parents=parents,
                              provenance="historical-test-ai-protocol")
        studio._write_tracked(self.project.root, request_path, studio.canonical_json(value),
                              parents=[*parents, studio._parent_ref(self.project.root, prompt_path, role="critic-prompt")],
                              provenance="historical-test-ai-request")
        self.assertEqual(self.project.integrity_errors(), [])
        return {**value, "prompt": prompt.decode("utf-8")}

    def new_recheck_finding(self, request, identity="NEW"):
        marker = "## Revised document blocks\n```json\n"
        blocks, _ = json.JSONDecoder().raw_decode(request["prompt"].split(marker, 1)[1])
        block = next(row for row in blocks if "负责人" in row["text"])
        finding = self.helper.finding(identity=identity)
        finding.update(location=block["location"], evidence=block["text"])
        detail = self.detail()
        detail["context_evidence"] = [{"block_id": block["block_id"], "quote": block["text"], "role": "context"}]
        finding["check_data"] = {"close_reading": detail}
        return finding

    def test_initial_import_uses_saved_v1_after_current_version_and_fields_change(self):
        request = self.helper.request()
        saved = copy.deepcopy(request["close_reading_protocol"])
        self.assertEqual(request["close_reading_protocol_sha256"], digest(saved))
        finding = self.helper.finding()
        finding["check_data"] = {"close_reading": self.detail()}
        with self.changed_current_protocol():
            run = self.helper.collect(request, self.helper.response(request, finding))
        receipt = self.project._audit_run_records()[0][1]["close_reading_receipt"]
        self.assert_v1_receipt(receipt, request, [run.findings[0].finding_id])
        self.assertEqual(request["close_reading_protocol"], saved)
        self.assertEqual(self.project.integrity_errors(), [])

    def test_optional_dossier_stays_optional_after_current_protocol_changes(self):
        request = self.helper.request()
        with self.changed_current_protocol():
            run = self.helper.collect(request, self.helper.response(request))
        receipt = self.project._audit_run_records()[0][1]["close_reading_receipt"]
        self.assert_v1_receipt(receipt, request, [], [run.findings[0].finding_id])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_manual_association_uses_original_v1_after_current_protocol_changes(self):
        request = self.helper.request()
        finding = self.helper.finding()
        finding["check_data"] = {"close_reading": self.detail()}
        payload = {"critic": request["critic"], "findings": [finding]}
        with self.changed_current_protocol():
            run = self.helper.collect(request, payload, binding_mode="manual_association")
        record = self.project._audit_run_records()[0][1]
        self.assertEqual(record["response_binding"]["mode"], "manual-association")
        self.assertFalse(record["response_binding"]["request_echo_verified"])
        self.assert_v1_receipt(record["close_reading_receipt"], request, [run.findings[0].finding_id])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_version_only_historical_request_recovers_full_v1_from_prompt(self):
        request = self.persisted_historical_request()
        expected = self.project._snapshotted_close_reading_protocol(request, request["prompt"].encode("utf-8"))
        finding = self.helper.finding()
        finding["check_data"] = {"close_reading": self.detail()}
        with self.changed_current_protocol():
            run = self.helper.collect(request, self.helper.response(request, finding))
        receipt = self.project._audit_run_records()[0][1]["close_reading_receipt"]
        self.assert_v1_receipt(receipt, {"close_reading_protocol": expected}, [run.findings[0].finding_id])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_pre_contract_request_is_not_relabelled_as_v1(self):
        request = self.persisted_historical_request(absent_contract=True)
        self.assertIsNone(self.project._snapshotted_close_reading_protocol(request, request["prompt"].encode("utf-8")))
        finding = self.helper.finding()
        finding["check_data"] = {"close_reading": self.detail()}
        with self.changed_current_protocol():
            run = self.helper.collect(request, self.helper.response(request, finding))
        receipt = self.project._audit_run_records()[0][1]["close_reading_receipt"]
        self.assertIsNone(receipt["protocol_version"])
        self.assertIsNone(receipt["protocol_sha256"])
        self.assertEqual(receipt["validation_version"], 1)
        self.assertEqual(receipt["protocol_binding"], "legacy-no-close-reading-contract")
        self.assertEqual(receipt["findings_with_checked_context_quotes"], [run.findings[0].finding_id])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_unknown_or_boolean_saved_versions_are_rejected(self):
        request = self.helper.request()
        blocks = {block.block_id: block for block in self.project.document().blocks}
        for version in (99, True, 1.0, "1", None):
            with self.subTest(version=version):
                value = copy.deepcopy(request)
                value["close_reading_protocol"]["version"] = version
                value["close_reading_protocol_version"] = version
                value["close_reading_protocol_sha256"] = digest(value["close_reading_protocol"])
                prompt = replace_contract(request["prompt"], lambda contract: contract.update(
                    close_reading_protocol=value["close_reading_protocol"]))
                with self.assertRaises(studio.ReviewStudioError):
                    self.project._snapshotted_close_reading_protocol(value, prompt.encode("utf-8"))
                self.assertTrue(quality.validate_close_reading(self.detail(), blocks, version=version))
        self.assertEqual(self.project._audit_run_records(), [])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_declared_version_snapshot_hash_or_fields_mismatch_is_rejected(self):
        request = self.helper.request()
        changes = [
            {"close_reading_protocol_version": 99},
            {"close_reading_protocol_version": True},
            {"close_reading_protocol_sha256": "0" * 64},
            {"close_reading_protocol": {**request["close_reading_protocol"], "limits": "changed"}},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(studio.ReviewStudioError):
                self.project._snapshotted_close_reading_protocol({**request, **change}, request["prompt"].encode("utf-8"))
        protocol = copy.deepcopy(request["close_reading_protocol"])
        protocol["fields"] = {"future_explanation": "unsupported v1 field"}
        prompt = replace_contract(request["prompt"], lambda contract: contract.update(close_reading_protocol=protocol))
        value = {**request, "close_reading_protocol": protocol, "close_reading_protocol_sha256": digest(protocol)}
        with self.assertRaises(studio.ReviewStudioError):
            self.project._snapshotted_close_reading_protocol(value, prompt.encode("utf-8"))

    def test_full_snapshot_rejects_json_type_changes_that_compare_equal_in_python(self):
        request = self.helper.request()
        for field, original, changed in (
            ("version", 1, True),
            ("version", 1, 1.0),
            ("future_metadata", {"revision": 1}, {"revision": True}),
            ("future_metadata", {"revision": 1}, {"revision": 1.0}),
        ):
            with self.subTest(field=field, changed=changed):
                protocol = copy.deepcopy(request["close_reading_protocol"])
                protocol[field] = original
                prompt = replace_contract(request["prompt"], lambda contract: contract.update(close_reading_protocol=protocol))
                value = copy.deepcopy(request)
                value["close_reading_protocol"] = copy.deepcopy(protocol)
                value["close_reading_protocol"][field] = changed
                value["close_reading_protocol_sha256"] = digest(protocol)
                self.assertEqual(value["close_reading_protocol"], protocol)
                self.assertNotEqual(digest(value["close_reading_protocol"]), value["close_reading_protocol_sha256"])
                with self.assertRaises(studio.ReviewStudioError):
                    self.project._snapshotted_close_reading_protocol(value, prompt.encode("utf-8"))

    def test_missing_contract_is_legacy_only_when_no_request_claims_it(self):
        request = self.helper.request()
        prompt = replace_contract(request["prompt"], lambda contract: contract.pop("close_reading_protocol"))
        minimal = {"critic": request["critic"]}
        self.assertIsNone(self.project._snapshotted_close_reading_protocol(minimal, prompt.encode("utf-8")))
        for field in ("close_reading_protocol", "close_reading_protocol_sha256", "close_reading_protocol_version"):
            with self.subTest(field=field), self.assertRaises(studio.ReviewStudioError):
                self.project._snapshotted_close_reading_protocol({**minimal, field: request[field]}, prompt.encode("utf-8"))
        malformed = replace_contract(request["prompt"], lambda contract: contract.update(close_reading_protocol=None))
        with self.assertRaises(studio.ReviewStudioError):
            self.project._snapshotted_close_reading_protocol(minimal, malformed.encode("utf-8"))

    def test_recheck_uses_original_v1_and_receipts_only_new_findings(self):
        initial, revision, request, payload = self.helper.followup(start=False)
        payload["new_findings"] = [self.new_recheck_finding(request)]
        with self.changed_current_protocol():
            result = self.project.collect_external_recheck(revision["revision_id"], request["critic"],
                json.dumps(payload), provider="provider", model="model")
        self.assert_v1_receipt(result["close_reading_receipt"], initial, ["NEW"])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_recheck_without_new_findings_records_no_checked_dossiers(self):
        initial, revision, request, payload = self.helper.followup(start=False)
        with self.changed_current_protocol():
            result = self.project.collect_external_recheck(revision["revision_id"], request["critic"],
                json.dumps(payload), provider="provider", model="model")
        self.assert_v1_receipt(result["close_reading_receipt"], initial, [])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_recheck_recovery_rejects_changed_origin_hashes_or_identity(self):
        _, _, request, _ = self.helper.followup(start=False)
        for field, wrong in (("original_request_sha256", "0" * 64),
                             ("original_prompt_file_sha256", "0" * 64),
                             ("original_request_id", "another-request"),
                             ("critic", "expression_ambiguity")):
            with self.subTest(field=field), self.assertRaises(studio.ReviewStudioError):
                self.project._close_reading_for_recheck({**request, field: wrong})
        self.assertEqual(self.project.integrity_errors(), [])

    def test_followup_inherited_origin_recovers_original_close_reading_contract(self):
        initial, _, followup = self.helper.followup()
        binding = followup["critic_bindings"][initial["critic"]]
        self.assertEqual(binding["original_request_id"], initial["request_id"])
        with self.changed_current_protocol():
            origin = self.project._critic_origin_binding(initial["critic"])
            recovered = self.project._close_reading_for_recheck(origin)
        self.assertEqual(recovered, initial["close_reading_protocol"])
        self.assertEqual(self.project.integrity_errors(), [])


if __name__ == "__main__":
    unittest.main()
