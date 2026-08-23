from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from argument_revision import (
    append_hunk_decision,
    append_quick_finding_decision,
    append_resolution_decision,
    apply_approved_hunks,
    collect_atomization_result,
    collect_resolution_result,
    collect_revision_result,
    complete_without_revision,
    current_quick_findings,
    export_revision,
    import_review_report,
    prepare_atomization,
    prepare_resolution_review,
    prepare_revision_generation,
    revision_hunks,
    verify_revision_workflow,
    workflow_view,
)
from argument_app import ProductApp, project_state
from argument_workbench import WorkbenchError, initialize_workspace, workspace_paths


class ArgumentRevisionTests(unittest.TestCase):
    SOURCE = "# Draft\n\nThe bridge works.\n\nA separate paragraph stays unchanged.\n"
    REPORT = "# Review\n\nThe claim ‘The bridge works.’ lacks a stated condition.\n"

    def project(self, root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        source = root / "draft.md"
        source.write_text(self.SOURCE, encoding="utf-8", newline="\n")
        project = root / "draft.argument-workbench"
        initialize_workspace(source, project)
        return project

    def atomize_findings(
        self,
        project: Path,
        findings: list[dict[str, object]],
        *,
        report: str | None = None,
    ) -> None:
        report_id = import_review_report(project, report or self.REPORT)
        run = prepare_atomization(project, report_id)
        record = json.loads((run / "record.json").read_text(encoding="utf-8"))
        response = {
            "schema_version": 1,
            "run_id": record["run_id"],
            "manuscript_version_id": "V1",
            "source_sha256": record["source_sha256"],
            "findings": findings,
        }
        result = collect_atomization_result(project, json.dumps(response))
        self.assertTrue(result.valid, result.errors)

    def atomize(self, project: Path) -> None:
        self.atomize_findings(project, [{
                "finding_id": "F1",
                "claim_id": "C1",
                "report_quote": "lacks a stated condition",
                "manuscript_quote": "The bridge works.",
                "location_kind": "exact_quote",
                "assertion": "The claim omits its operating condition.",
                "criterion": "Explicit scope",
                "suggested_action": "State the dry-weather condition.",
                "evidence_level": "unverified",
                "uncertainties": ["The intended condition is not independently verified."],
            }])

    def proposal(self, project: Path, *, quote: str = "The bridge works.", finding_id: str = "F1"):
        append_quick_finding_decision(project, "F1", decision="accept", reason="Author chooses to qualify")
        run = prepare_revision_generation(project)
        record = json.loads((run / "record.json").read_text(encoding="utf-8"))
        response = {
            "schema_version": 1,
            "generation_run_id": record["generation_run_id"],
            "manuscript_version_id": "V1",
            "source_sha256": record["source_sha256"],
            "changes": [{
                "change_id": "CH1",
                "original_quote": quote,
                "insertion_anchor": None,
                "replacement_text": "The bridge works in dry weather.",
                "finding_ids": [finding_id],
                "action_ids": [record["action_ids"][0]],
                "change_kind": "replace",
                "reason": "Adds the accepted qualification.",
                "uncertainties": ["Condition still needs factual verification."],
                "fact_change": False,
                "verification_note": "",
            }],
        }
        return collect_revision_result(project, json.dumps(response)), record, response

    def test_complete_revision_chain_is_human_decided_and_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self.project(Path(temp_dir)); self.atomize(project)
            self.assertEqual(workflow_view(project)["stage"], "findings_confirm")
            result, _, _ = self.proposal(project)
            self.assertTrue(result.valid, result.errors)
            self.assertEqual(workflow_view(project)["stage"], "hunk_review")
            append_hunk_decision(project, "CH1", decision="edit", reason="Prefer narrower wording", edited_text="The bridge works when the deck is dry.")
            application = apply_approved_hunks(project)
            self.assertEqual(workflow_view(project)["stage"], "resolution_prepare")
            v1 = next((workspace_paths(project, "V1").version_dir / "source").iterdir())
            v2 = next((workspace_paths(project, "V2").version_dir / "source").iterdir())
            self.assertEqual(v1.read_text(encoding="utf-8"), self.SOURCE)
            self.assertIn("when the deck is dry", v2.read_text(encoding="utf-8"))
            self.assertEqual(application["output_source_sha256"], hashlib.sha256(v2.read_bytes()).hexdigest())
            self.assertEqual(apply_approved_hunks(project), application)

            run = prepare_resolution_review(project)
            record = json.loads((run / "record.json").read_text(encoding="utf-8"))
            resolution = {"schema_version": 1, "resolution_run_id": record["resolution_run_id"], "manuscript_version_id": "V2", "source_sha256": record["source_sha256"], "results": [{"finding_id": "F1", "proposed_status": "partially_resolved", "reason": "A condition is now stated but unverified.", "evidence_quotes": ["when the deck is dry"], "uncertainties": ["factual basis unverified"]}]}
            self.assertTrue(collect_resolution_result(project, json.dumps(resolution)).valid)
            append_resolution_decision(project, "F1", status="partially_resolved", reason="Author confirms wording but not fact")
            exported = export_revision(project)
            self.assertEqual(workflow_view(project)["stage"], "complete")
            self.assertTrue((exported / "V2.md").is_file())
            checklist = (exported / "revision-checklist.md").read_text(encoding="utf-8")
            self.assertIn("partially_resolved", checklist)
            self.assertIn("UNVERIFIED", checklist)

    def test_invalid_attempts_are_retained_and_cannot_reach_hunk_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self.project(Path(temp_dir)); self.atomize(project)
            result, _, response = self.proposal(project, finding_id="F999")
            self.assertFalse(result.valid)
            self.assertTrue(result.response.is_file())
            self.assertTrue(result.repair_prompt and result.repair_prompt.is_file())
            self.assertTrue(any("unselected findings" in error for error in result.errors))
            self.assertEqual(len(revision_hunks(project)) if result.valid else 0, 0)

    def test_unselected_paragraph_and_overlapping_changes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self.project(Path(temp_dir)); self.atomize(project)
            result, _, _ = self.proposal(project, quote="A separate paragraph stays unchanged.")
            self.assertFalse(result.valid)
            self.assertTrue(any("outside the located text" in error for error in result.errors))

    def test_tampered_proposal_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self.project(Path(temp_dir)); self.atomize(project)
            result, _, _ = self.proposal(project)
            self.assertTrue(result.valid)
            self.assertEqual(verify_revision_workflow(project), [])
            proposal = result.response.parent / "revision-patch-proposal.json"
            value = json.loads(proposal.read_text(encoding="utf-8"))
            value["changes"][0]["replacement_text"] = "tampered"
            proposal.write_text(json.dumps(value), encoding="utf-8")
            self.assertTrue(any("derived proposal mismatch" in error for error in verify_revision_workflow(project)))

    def test_rejected_hunk_never_appears_in_a_new_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self.project(Path(temp_dir)); self.atomize(project)
            result, record, response = self.proposal(project)
            self.assertTrue(result.valid)
            # Add a second accepted hunk so one accepted change still creates V2.
            response["changes"].append({**response["changes"][0], "change_id": "CH2", "original_quote": "The bridge works.", "replacement_text": "This must never appear."})
            # Overlap protection rejects the combined attempt.
            overlap = collect_revision_result(project, json.dumps(response), run_id=record["generation_run_id"])
            self.assertFalse(overlap.valid)
            append_hunk_decision(project, "CH1", decision="reject", reason="Do not adopt")
            self.assertEqual(revision_hunks(project)[0]["decision"]["decision"], "reject")

    def test_regeneration_prompt_is_scoped_and_new_proposal_requires_reapproval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self.project(Path(temp_dir)); self.atomize(project)
            result, record, response = self.proposal(project)
            self.assertTrue(result.valid)
            append_hunk_decision(project, "CH1", decision="regenerate", reason="Try a clearer qualification")
            state = workflow_view(project)
            self.assertIn("Regenerate only `CH1`", state["regeneration_prompt"])
            response["changes"][0]["replacement_text"] = "The bridge works whenever the deck remains dry."
            newer = collect_revision_result(project, json.dumps(response), run_id=record["generation_run_id"])
            self.assertTrue(newer.valid, newer.errors)
            self.assertIsNone(revision_hunks(project)[0]["decision"])

    def test_new_findings_hash_invalidates_old_finding_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self.project(Path(temp_dir)); self.atomize(project)
            append_quick_finding_decision(project, "F1", decision="accept", reason="Accept first finding")
            first = current_quick_findings(project)[0]
            self.assertEqual(first["decision"], "accept")
            self.atomize_findings(
                project,
                [{
                    "finding_id": "F1", "claim_id": "C2",
                    "report_quote": "separate paragraph is unsupported",
                    "manuscript_quote": "A separate paragraph stays unchanged.",
                    "location_kind": "exact_quote",
                    "assertion": "A completely different criticism.",
                    "criterion": "Evidence", "suggested_action": "Add support.",
                    "evidence_level": "unverified", "uncertainties": [],
                }],
                report="# New review\n\nThe separate paragraph is unsupported.\n",
            )
            second = current_quick_findings(project)[0]
            self.assertNotEqual(first["source_findings_sha256"], second["source_findings_sha256"])
            self.assertIsNone(second["decision"])
            self.assertIsNone(second["action_id"])

    def test_new_resolution_proposal_invalidates_old_human_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self.project(Path(temp_dir)); self.atomize(project)
            result, _, _ = self.proposal(project); self.assertTrue(result.valid)
            append_hunk_decision(project, "CH1", decision="accept", reason="Apply")
            apply_approved_hunks(project)
            run = prepare_resolution_review(project)
            record = json.loads((run / "record.json").read_text(encoding="utf-8"))
            base = {"schema_version": 1, "resolution_run_id": record["resolution_run_id"], "manuscript_version_id": "V2", "source_sha256": record["source_sha256"]}
            resolved = {**base, "results": [{"finding_id": "F1", "proposed_status": "resolved", "reason": "First review.", "evidence_quotes": ["in dry weather"], "uncertainties": []}]}
            self.assertTrue(collect_resolution_result(project, json.dumps(resolved)).valid)
            append_resolution_decision(project, "F1", status="resolved", reason="Confirm first proposal")
            unresolved = {**base, "results": [{"finding_id": "F1", "proposed_status": "unresolved", "reason": "New evidence changes the assessment.", "evidence_quotes": ["in dry weather"], "uncertainties": ["condition unsupported"]}]}
            self.assertTrue(collect_resolution_result(project, json.dumps(unresolved), run_id=record["resolution_run_id"]).valid)
            state = workflow_view(project)
            self.assertEqual(state["stage"], "resolution_confirm")
            self.assertIsNone(state["resolution_results"][0]["human_decision"])

    def test_tampered_workflow_forces_default_app_into_read_only_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self.project(Path(temp_dir)); self.atomize(project)
            result, _, _ = self.proposal(project); self.assertTrue(result.valid)
            proposal = result.response.parent / "revision-patch-proposal.json"
            value = json.loads(proposal.read_text(encoding="utf-8"))
            value["changes"][0]["replacement_text"] = "tampered"
            proposal.write_text(json.dumps(value), encoding="utf-8")
            state = project_state(project)
            self.assertEqual(state["stage"], "read_only")
            self.assertTrue(any("derived proposal mismatch" in error for error in state["errors"]))
            app = ProductApp(project.parent, "test-token", project)
            with self.assertRaisesRegex(WorkbenchError, "只能只读打开"):
                app.act({"action": "decide_hunk", "data": {"change_id": "CH1", "decision": "accept", "reason": "Should be blocked"}})

    def test_revision_action_must_match_every_linked_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self.project(Path(temp_dir))
            self.atomize_findings(project, [
                {"finding_id": "F1", "claim_id": "C1", "report_quote": "first issue", "manuscript_quote": "The bridge works.", "location_kind": "exact_quote", "assertion": "First issue.", "criterion": "Scope", "suggested_action": "Qualify it.", "evidence_level": "unverified", "uncertainties": []},
                {"finding_id": "F2", "claim_id": "C2", "report_quote": "second issue", "manuscript_quote": "A separate paragraph stays unchanged.", "location_kind": "exact_quote", "assertion": "Second issue.", "criterion": "Evidence", "suggested_action": "Support it.", "evidence_level": "unverified", "uncertainties": []},
            ], report="# Review\n\nfirst issue\n\nsecond issue\n")
            append_quick_finding_decision(project, "F1", decision="accept", reason="First")
            append_quick_finding_decision(project, "F2", decision="accept", reason="Second")
            run = prepare_revision_generation(project); record = json.loads((run / "record.json").read_text(encoding="utf-8"))
            response = {"schema_version": 1, "generation_run_id": record["generation_run_id"], "manuscript_version_id": "V1", "source_sha256": record["source_sha256"], "changes": [{"change_id": "CH1", "original_quote": "The bridge works.", "insertion_anchor": None, "replacement_text": "The bridge works conditionally.", "finding_ids": ["F1"], "action_ids": [record["action_ids"][1]], "change_kind": "replace", "reason": "Cross-wired action.", "uncertainties": [], "fact_change": False, "verification_note": ""}]}
            result = collect_revision_result(project, json.dumps(response))
            self.assertFalse(result.valid)
            self.assertTrue(any("Finding–Action" in error for error in result.errors))

    def test_zero_findings_and_all_declined_have_legal_completion_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            zero_project = self.project(Path(temp_dir) / "zero")
            self.atomize_findings(zero_project, [])
            self.assertEqual(workflow_view(zero_project)["stage"], "no_revision")
            export = complete_without_revision(zero_project, reason="No actionable findings")
            self.assertTrue((export / "audit.json").is_file())
            self.assertEqual(workflow_view(zero_project)["stage"], "complete")

        with tempfile.TemporaryDirectory() as temp_dir:
            declined_project = self.project(Path(temp_dir) / "declined"); self.atomize(declined_project)
            append_quick_finding_decision(declined_project, "F1", decision="defer", reason="Not in this revision")
            self.assertEqual(workflow_view(declined_project)["stage"], "no_revision")
            complete_without_revision(declined_project, reason="All findings deferred")
            self.assertEqual(workflow_view(declined_project)["stage"], "complete")


if __name__ == "__main__":
    unittest.main()
