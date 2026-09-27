"""Persisted revised-text evidence contracts; no mocked validation or quality claims."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import document_review_studio as studio
from document_review_quality import close_reading_example
from project_lifecycle import _files
from test import test_review_round_protocols as fixtures


CONTRACT_MARKER = "## Resolution evidence contract\n```json\n"
BLOCKS_MARKER = "## Revised document blocks\n```json\n"


def snapshot(project):
    return {name: path.read_bytes() for name, path in _files(project.root).items()}


class ResolutionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.ReviewRoundProtocolTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.project = self.helper.project
        request = self.helper.request()
        finding = self.helper.finding()
        self.old_detail = close_reading_example()
        self.old_detail["context_evidence"] = [{
            "block_id": finding["location"]["block_id"], "quote": finding["evidence"], "role": "context",
        }]
        finding["check_data"] = {"close_reading": copy.deepcopy(self.old_detail)}
        self.original = self.helper.collect(request, self.helper.response(request, finding)).findings[0]
        self.project.decide_finding(self.original.finding_id, "accept", reason="明确执行和验收责任")
        action = self.project.prepare_revision_plan()["actions"][0]
        self.project.set_revision_action_operation(action["action_id"], "replace_block", reason="补充当前段落")
        hunk = self.project.propose_revision_hunk(
            action["action_id"], "负责人：项目经理。\n\n验收标准尚待确认。", rationale="分别陈述负责人和验收条件")
        self.project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="核对修改稿")
        self.revision_dir = self.project.finalize_revision()
        self.revision = json.loads((self.revision_dir / "revision.json").read_text(encoding="utf-8"))
        self.revision_id = self.revision["revision_id"]
        self.request = self.project.external_recheck_requests(self.revision_id)[0]
        self.blocks = json.loads(self.request["prompt"].split(BLOCKS_MARKER, 1)[1].split("\n```", 1)[0])
        self.primary = next(block for block in self.blocks if block["text"] == "验收标准尚待确认。")
        self.anchor = {"block_id": self.primary["block_id"], "quote": self.primary["text"]}
        self.payload = {
            **{key: self.request[key] for key in ("request_id", "prompt_sha256", "critic", "revision_id", "revised_sha256")},
            "resolutions": [{"finding_id": self.original.finding_id, "state": "still-present",
                             "reason": "执行负责人已明确，但验收条件仍未落实。",
                             "evidence": "修订稿明确保留了待确认的验收标准。",
                             "source_evidence": [copy.deepcopy(self.anchor)]}],
            "new_findings": [],
        }

    def collect(self, payload=None, *, project=None, binding_mode="strict"):
        return (project or self.project).collect_external_recheck(
            self.revision_id, fixtures.CRITIC,
            json.dumps(self.payload if payload is None else payload, ensure_ascii=False),
            provider="provider", model="rechecker", binding_mode=binding_mode)

    def assert_rejected_without_writes(self, payload, *, project=None, binding_mode="strict"):
        project = project or self.project
        before = snapshot(project)
        with self.assertRaises(studio.ReviewStudioError):
            self.collect(payload, project=project, binding_mode=binding_mode)
        self.assertEqual(snapshot(project), before)
        self.assertEqual(project._external_recheck_results(self.revision_id), [])

    def request_fixture(self, transform):
        """Materialize a separate immutable fixture, with each artifact written once.

        Historical and drift fixtures use the real importer and integrity checks.
        Only the fresh fixture's request bytes are selected before publication;
        the source project's artifacts, receipts and ledger are never rewritten.
        """
        data = snapshot(self.project)
        prefix = f"revisions/{self.revision_id}/external-recheck-requests/{fixtures.CRITIC}/"
        request = json.loads(data[prefix + "request.json"])
        prompt = data[prefix + "prompt.md"].decode("utf-8")
        request, prompt = transform(request, prompt)
        data[prefix + "request.json"] = studio.canonical_json(request)
        data[prefix + "prompt.md"] = prompt.encode("utf-8")
        recheck_path = f"revisions/{self.revision_id}/recheck.json"
        recheck = json.loads(data[recheck_path])
        recheck["external_recheck_requests"] = [request]
        data[recheck_path] = studio.canonical_json(recheck)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / self.project.root.name
        root.mkdir()
        index = json.loads(data[studio.INTEGRITY_INDEX_NAME])
        latest = {row["relative_path"]: row for row in index["entries"]}
        for name, content in data.items():
            if name in latest or name == studio.INTEGRITY_INDEX_NAME or studio.INTEGRITY_RECEIPT_DIR in Path(name).parts:
                continue
            studio._write_new(root / name, content)
        studio._initialize_integrity_index(root)
        # Genuine old requests keep their original writer compatibility. New
        # requests preserve the source project's downgrade-protection barrier.
        legacy = request.get("schema_version") in {1, 2} and "resolution_evidence_protocol" not in request and CONTRACT_MARKER not in prompt
        fresh_index = json.loads((root / studio.INTEGRITY_INDEX_NAME).read_bytes())
        fresh_index["schema_version"] = 1 if legacy else index["schema_version"]
        studio._write_integrity_index(root, fresh_index, new=False)
        for name, row in latest.items():
            receipt = json.loads(data[row["receipt_relative_path"]])
            parents = [{**parent, "sha256": hashlib.sha256(data[parent["relative_path"]]).hexdigest()}
                       for parent in receipt["parents"]]
            studio._write_tracked(root, root / name, data[name], parents=parents,
                                  provenance=receipt["provenance"], artifact_type=row["artifact_type"])
        project = studio.DocumentReviewProject(root)
        self.assertEqual(project.integrity_errors(), [])
        return project, request

    def test_known_old_writer_cache_clears_only_after_successful_current_integrity_check(self):
        self.assertEqual(json.loads((self.project.root / studio.INTEGRITY_INDEX_NAME).read_bytes())["schema_version"], 2)
        cached = {**self.project.state(), "read_only": True,
                  "integrity_errors": ["integrity-index.json: invalid index fields"]}
        studio._atomic_write(self.project.state_path, studio.canonical_json(cached))
        reloaded = studio.DocumentReviewProject(self.project.root)
        self.assertEqual(reloaded.integrity_errors(), [])
        state = reloaded.state()
        self.assertFalse(state["read_only"])
        self.assertEqual(state["integrity_errors"], [])
        self.assertEqual(self.collect(project=reloaded)["resolutions"][0]["source_evidence"], [self.anchor])
        self.assertEqual(reloaded.integrity_errors(), [])

    def test_unrelated_readonly_reasons_and_legacy_writer_cache_are_preserved(self):
        for errors in (["Unrelated integrity hold"],
                       ["integrity-index.json: invalid index fields", "Unrelated integrity hold"]):
            with self.subTest(errors=errors):
                cached = {**self.project.state(), "read_only": True, "integrity_errors": errors}
                studio._atomic_write(self.project.state_path, studio.canonical_json(cached))
                self.assertEqual(self.project.integrity_errors(), [])
                self.assertTrue(self.project.state()["read_only"])
                self.assertEqual(self.project.state()["integrity_errors"], errors)
                self.assert_rejected_without_writes(self.payload)
        # The compatibility cache exemption is specific to the new index, not a
        # blanket way to unlock historical projects marked read-only.
        legacy, request = self.legacy_fixture()
        self.assertEqual(json.loads((legacy.root / studio.INTEGRITY_INDEX_NAME).read_bytes())["schema_version"], 1)
        cached = {**legacy.state(), "read_only": True,
                  "integrity_errors": ["integrity-index.json: invalid index fields"]}
        studio._atomic_write(legacy.state_path, studio.canonical_json(cached))
        self.assertTrue(legacy.state()["read_only"])
        self.assertEqual(legacy.state()["integrity_errors"], cached["integrity_errors"])
        payload = copy.deepcopy(self.payload)
        payload["prompt_sha256"] = request["prompt_sha256"]
        self.assert_rejected_without_writes(payload, project=legacy)

    def legacy_fixture(self, version=2):
        def transform(request, prompt):
            start = prompt.index(CONTRACT_MARKER)
            end = prompt.index("\n```", start + len(CONTRACT_MARKER)) + len("\n```")
            prompt = prompt[:start] + prompt[end:]
            prompt = prompt.replace(
                "Each resolution must follow the Resolution evidence contract below, including source_evidence and the unable-to-assess option.",
                "Each resolution must contain finding_id, state (resolved|partially-resolved|still-present), reason, and evidence.")
            base = prompt.split("\n\n## Required response envelope", 1)[0]
            request["schema_version"] = version
            request.pop("resolution_evidence_protocol")
            request.pop("resolution_evidence_protocol_sha256")
            request["prompt_sha256"] = hashlib.sha256(base.encode("utf-8")).hexdigest()
            envelope = {key: request[key] for key in ("request_id", "prompt_sha256", "revision_id", "revised_sha256", "critic")}
            prompt = base + "\n\n## Required response envelope\nReturn these fields exactly:\n```json\n" + json.dumps(envelope, ensure_ascii=False, indent=2) + "\n```\n"
            request["prompt_file_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            return request, prompt
        return self.request_fixture(transform)

    def test_real_request_snapshots_contract_and_import_records_only_structural_checks(self):
        protocol = json.loads(self.request["prompt"].split(CONTRACT_MARKER, 1)[1].split("\n```", 1)[0])
        self.assertEqual(self.request["schema_version"], 3)
        self.assertEqual(protocol["version"], 1)
        self.assertEqual(protocol, self.request["resolution_evidence_protocol"])
        self.assertEqual(hashlib.sha256(studio.canonical_json(protocol)).hexdigest(), self.request["resolution_evidence_protocol_sha256"])
        with patch.object(studio, "_now", return_value="2030-01-01T00:00:00Z"):
            result = self.collect()
        self.assertEqual(result["resolutions"][0]["source_evidence"], [self.anchor])
        self.assertEqual(result["resolutions"][0]["evidence_validation"], "checked-revised-excerpts")
        receipt = result["resolution_evidence_receipt"]
        self.assertEqual(receipt["protocol_version"], 1)
        self.assertEqual(receipt["protocol_sha256"], self.request["resolution_evidence_protocol_sha256"])
        self.assertEqual(receipt["checked_findings"], [self.original.finding_id])
        self.assertEqual(receipt["semantic_accuracy"], "not-established-by-structural-validation")
        status = studio.DocumentReviewProject(self.project.root).external_recheck_status(self.revision_id)
        self.assertFalse(status["complete"])
        self.assertIsNone(status["requests"][0]["items"][0]["human_decision"])
        self.assertEqual(status["revised_blocks"], [self.primary])
        self.assertEqual(self.project._external_resolution_records(self.revision_id), [])
        # Import order remains authoritative even when the wall clock moves back.
        resolved = copy.deepcopy(self.payload)
        resolved["resolutions"][0]["state"] = "resolved"
        with patch.object(studio, "_now", return_value="2020-01-01T00:00:00Z"):
            newest = self.collect(resolved)
        self.assertEqual(newest["result_sequence"], result["result_sequence"] + 1)
        status = self.project.external_recheck_status(self.revision_id)
        self.assertEqual(status["requests"][0]["result"]["result_id"], newest["result_id"])
        self.assertFalse(status["complete"])
        self.project.decide_external_resolution(self.revision_id, newest["result_id"], self.original.finding_id,
                                                "resolved", reason="人工确认修订后的安排")
        self.assertTrue(self.project.external_recheck_status(self.revision_id)["complete"])
        self.assertEqual(next(row for row in self.project.view()["workflow"] if row["key"] == "bridge")["status"], "completed")
        self.assertEqual(self.project.integrity_errors(), [])

    def test_forged_stale_and_wrong_block_quotes_reject_atomically(self):
        wrong_block = next(block for block in self.blocks if block["block_id"] != self.anchor["block_id"])
        for state in ("resolved", "partially-resolved", "still-present"):
            for anchor in ({**self.anchor, "quote": "预算无限且无需验收。"},
                           {**self.anchor, "quote": self.original.evidence},
                           {**self.anchor, "block_id": wrong_block["block_id"]},
                           {**self.anchor, "block_id": "historical-block-that-does-not-exist"}):
                with self.subTest(state=state, anchor=anchor):
                    payload = copy.deepcopy(self.payload)
                    payload["resolutions"][0].update(state=state, source_evidence=[anchor])
                    self.assert_rejected_without_writes(payload)

    def test_new_request_requires_anchors_in_strict_and_manual_modes(self):
        for binding in ("strict", "manual_association"):
            for state in ("resolved", "partially-resolved", "still-present"):
                for omitted in (True, False):
                    with self.subTest(binding=binding, state=state, omitted=omitted):
                        payload = copy.deepcopy(self.payload)
                        if binding == "manual_association":
                            payload = {key: payload[key] for key in ("resolutions", "new_findings")}
                        payload["resolutions"][0]["state"] = state
                        if omitted:
                            payload["resolutions"][0].pop("source_evidence")
                        else:
                            payload["resolutions"][0]["source_evidence"] = []
                        self.assert_rejected_without_writes(payload, binding_mode=binding)

    def test_malformed_anchor_fields_counts_and_explanation_limits_reject_atomically(self):
        changes = [{"source_evidence": value} for value in (
            None, "quote", [None], [{}], [{**self.anchor, "verified": True}],
            [{**self.anchor, "block_id": []}], [{**self.anchor, "quote": " "}],
            [{**self.anchor, "quote": "x" * 100_001}], [self.anchor] * 65,
        )]
        changes += [{field: invalid} for field in ("reason", "evidence")
                    for invalid in (None, [], " ", "x" * 20_001)]
        for change in changes:
            with self.subTest(fields=tuple(change)):
                payload = copy.deepcopy(self.payload)
                payload["resolutions"][0].update(change)
                self.assert_rejected_without_writes(payload)

    def test_unable_to_assess_without_excerpts_remains_pending_and_blocks_unanchored_followup(self):
        payload = copy.deepcopy(self.payload)
        payload["resolutions"][0].update(state="unable-to-assess", source_evidence=[],
                                        reason="未提供验收附件。", evidence="现有材料不能判断附件中的验收安排。")
        result = self.collect(payload)
        self.assertEqual(result["resolutions"][0]["evidence_validation"], "no-revised-excerpts")
        self.assertEqual(result["resolution_evidence_receipt"]["checked_findings"], [])
        self.assertEqual(result["resolution_evidence_receipt"]["findings_without_checked_excerpts"], [self.original.finding_id])
        self.assertFalse(self.project.external_recheck_status(self.revision_id)["complete"])
        self.assertEqual(next(row for row in self.project.view()["workflow"] if row["key"] == "bridge")["status"], "in_progress")
        self.assertEqual(self.project._external_resolution_records(self.revision_id), [])
        self.project.decide_external_resolution(self.revision_id, result["result_id"], self.original.finding_id,
                                                "unresolved", reason="等待补充附件")
        status = self.project.external_recheck_status(self.revision_id)
        self.assertFalse(status["complete"])
        self.assertFalse(status["can_start_followup"])
        self.assertEqual(status["followup_blockers"], [self.original.finding_id])
        self.assertEqual(next(row for row in self.project.view()["workflow"] if row["key"] == "bridge")["status"], "in_progress")
        before = snapshot(self.project)
        with self.assertRaises(studio.ReviewStudioError):
            self.project.start_followup_round(self.revision_id)
        self.assertEqual(snapshot(self.project), before)

    def test_unable_to_assess_still_requires_explanations_and_valid_supplied_excerpts(self):
        for change in ({"reason": ""}, {"evidence": ""}, {"source_evidence": [{**self.anchor, "quote": "invented"}]}):
            payload = copy.deepcopy(self.payload)
            payload["resolutions"][0].update(state="unable-to-assess", source_evidence=[])
            payload["resolutions"][0].update(change)
            self.assert_rejected_without_writes(payload)
        payload = copy.deepcopy(self.payload)
        payload["resolutions"][0]["state"] = "unable-to-assess"
        self.assertEqual(self.collect(payload)["resolutions"][0]["source_evidence"], [self.anchor])

    def test_saved_contract_version_and_hash_drift_fail_with_intact_integrity(self):
        for field, value in (("schema_version", 99), ("resolution_evidence_protocol_sha256", "0" * 64),
                             ("resolution_evidence_protocol", {**self.request["resolution_evidence_protocol"], "version": 99})):
            with self.subTest(field=field):
                def transform(request, prompt):
                    request[field] = value
                    if field == "resolution_evidence_protocol":
                        # Even matching hashes must not authorize an unsupported contract.
                        prompt = prompt.replace(json.dumps(self.request[field], ensure_ascii=False, indent=2),
                                                json.dumps(value, ensure_ascii=False, indent=2), 1)
                        request["resolution_evidence_protocol_sha256"] = hashlib.sha256(studio.canonical_json(value)).hexdigest()
                        request["prompt_file_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                    return request, prompt
                project, _ = self.request_fixture(transform)
                self.assert_rejected_without_writes(self.payload, project=project)

    def test_new_saved_request_cannot_drop_contract_metadata_or_prompt_section(self):
        for drop_prompt in (False, True):
            with self.subTest(drop_prompt=drop_prompt):
                def transform(request, prompt):
                    if drop_prompt:
                        start = prompt.index(CONTRACT_MARKER)
                        end = prompt.index("\n```", start + len(CONTRACT_MARKER)) + len("\n```")
                        prompt = prompt[:start] + prompt[end:]
                        request["prompt_file_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                    else:
                        request.pop("resolution_evidence_protocol")
                        request.pop("resolution_evidence_protocol_sha256")
                    return request, prompt
                project, _ = self.request_fixture(transform)
                self.assert_rejected_without_writes(self.payload, project=project)

    def test_legacy_schema_one_and_two_import_without_claiming_checked_evidence(self):
        for version in (1, 2):
            with self.subTest(version=version):
                project, request = self.legacy_fixture(version)
                payload = copy.deepcopy(self.payload)
                payload["prompt_sha256"] = request["prompt_sha256"]
                payload["resolutions"][0].pop("source_evidence")
                result = self.collect(payload, project=project)
                self.assertEqual(result["resolutions"][0]["evidence_validation"], "legacy-unchecked")
                self.assertIsNone(result["resolution_evidence_receipt"]["protocol_version"])
                self.assertEqual(result["resolution_evidence_receipt"]["protocol_binding"], "legacy-no-evidence-contract")
                self.assertEqual(result["resolution_evidence_receipt"]["checked_findings"], [])
                self.assertEqual(result["resolution_evidence_receipt"]["findings_without_checked_excerpts"], [self.original.finding_id])
                self.assertFalse(project.external_recheck_status(self.revision_id)["complete"])
                self.assertEqual(project.integrity_errors(), [])

    def test_legacy_optional_excerpts_are_validated_when_present(self):
        project, request = self.legacy_fixture()
        payload = copy.deepcopy(self.payload)
        payload["prompt_sha256"] = request["prompt_sha256"]
        for anchors in (None, [], [{**self.anchor, "quote": "forged legacy quote"}]):
            invalid = copy.deepcopy(payload)
            invalid["resolutions"][0]["source_evidence"] = anchors
            self.assert_rejected_without_writes(invalid, project=project)
        result = self.collect(payload, project=project)
        self.assertEqual(result["resolutions"][0]["evidence_validation"], "checked-revised-excerpts")
        self.assertIsNone(result["resolution_evidence_receipt"]["protocol_version"])

    def test_followup_uses_primary_current_excerpt_and_keeps_old_close_reading_historical(self):
        payload = copy.deepcopy(self.payload)
        other = next(block for block in self.blocks if block["text"] == "负责人：项目经理。")
        payload["resolutions"][0]["source_evidence"].append({"block_id": other["block_id"], "quote": other["text"]})
        result = self.collect(payload)
        self.project.decide_external_resolution(self.revision_id, result["result_id"], self.original.finding_id,
                                                "partially-resolved", reason="负责人明确，验收条件仍需补充")
        followup = self.project.start_followup_round(self.revision_id)
        carried = self.project.findings()[0]
        self.assertEqual(carried.location.to_dict(), self.primary["location"])
        self.assertEqual(carried.evidence, self.anchor["quote"])
        self.assertNotEqual(carried.evidence, payload["resolutions"][0]["evidence"])
        self.assertEqual(carried.status, "open")
        self.assertNotIn("close_reading", carried.check_data)
        self.assertEqual(carried.check_data["historical_close_reading"], {
            "source_finding_id": self.original.finding_id, "detail": self.old_detail,
        })
        self.assertEqual(followup["finding_sources"][0]["source_evidence"], [self.anchor])
        request = self.helper.request()
        self.assertIn(self.anchor["quote"], request["prompt"])
        self.assertEqual(self.project.integrity_errors(), [])

    def test_legacy_unanchored_explanation_cannot_be_promoted_to_a_random_current_block(self):
        project, request = self.legacy_fixture()
        payload = copy.deepcopy(self.payload)
        payload["prompt_sha256"] = request["prompt_sha256"]
        payload["resolutions"][0].pop("source_evidence")
        result = self.collect(payload, project=project)
        project.decide_external_resolution(self.revision_id, result["result_id"], self.original.finding_id,
                                            "unresolved", reason="仍需补充验收标准")
        status = project.external_recheck_status(self.revision_id)
        self.assertEqual(status["followup_blockers"], [self.original.finding_id])
        self.assertFalse(status["can_start_followup"])
        with self.assertRaises(studio.ReviewStudioError):
            project.start_followup_round(self.revision_id)
        self.assertEqual(project.integrity_errors(), [])


class ResolutionEvidenceWriterCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.ReviewRoundProtocolTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.project = self.helper.project

    def index_version(self):
        return json.loads((self.project.root / studio.INTEGRITY_INDEX_NAME).read_bytes())["schema_version"]

    def prepare_approved_revision(self):
        request = self.helper.request()
        original = self.helper.collect(request, self.helper.response(request)).findings[0]
        self.project.decide_finding(original.finding_id, "accept", reason="需要明确负责人")
        action = self.project.prepare_revision_plan()["actions"][0]
        self.project.set_revision_action_operation(action["action_id"], "replace_block", reason="明确当前段落")
        hunk = self.project.propose_revision_hunk(action["action_id"], "负责人：项目经理。", rationale="补充负责人")
        self.project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="已核对当前段落")

    def test_new_recheck_contract_upgrades_index_only_when_revision_is_published(self):
        self.assertEqual(self.index_version(), 1)
        self.prepare_approved_revision()
        self.assertEqual(self.index_version(), 1)
        revision_dir = self.project.finalize_revision()
        revision = json.loads((revision_dir / "revision.json").read_bytes())
        requests = self.project.external_recheck_requests(revision["revision_id"])
        self.assertEqual([request["schema_version"] for request in requests], [3])
        self.assertEqual(self.index_version(), 2)
        self.assertEqual(self.project.integrity_errors(), [])

    def test_failure_after_index_upgrade_restores_schema_one_and_unpublished_revision(self):
        self.prepare_approved_revision()
        before = snapshot(self.project)
        self.assertEqual(self.index_version(), 1)
        write_index = studio._write_integrity_index
        attempted_upgrade = []

        def fail_after_upgrade(root, index, *, new):
            write_index(root, index, new=new)
            if index["schema_version"] == 2:
                attempted_upgrade.append(True)
                raise OSError("simulated disk failure after writer barrier update")

        # One storage-failure injection, after the real write, exercises the
        # complete transaction rather than bypassing any integrity validator.
        with patch.object(studio, "_write_integrity_index", side_effect=fail_after_upgrade):
            with self.assertRaisesRegex(OSError, "writer barrier"):
                self.project.finalize_revision()
        self.assertTrue(attempted_upgrade)
        self.assertEqual(snapshot(self.project), before)
        self.assertEqual(self.index_version(), 1)
        self.assertEqual(list((self.project.root / "revisions").glob("*/revision.json")), [])
        self.assertEqual(self.project.integrity_errors(), [])
        revision_dir = self.project.finalize_revision()
        self.assertTrue((revision_dir / "revision.json").is_file())
        self.assertEqual(self.index_version(), 2)
        self.assertEqual(self.project.integrity_errors(), [])


if __name__ == "__main__":
    unittest.main()
