from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import argument_adjudication as adjudication  # noqa: E402
import argument_contracts as contracts  # noqa: E402
import argument_lineage as lineage  # noqa: E402
import argument_resolution as resolution  # noqa: E402
import argument_ir  # noqa: E402
import argument_versioning as versioning  # noqa: E402
import argument_workbench as workbench  # noqa: E402
import critic_runner  # noqa: E402


FIXTURE = REPO_ROOT / "test" / "fixtures" / "workbench-demo"


class ArgumentResolutionTests(unittest.TestCase):
    def make_chain(self, root: Path, *, removed: bool = False, split: bool = False):
        from test.test_argument_adjudication import ArgumentAdjudicationTests

        helper = ArgumentAdjudicationTests()
        v1, _ = helper.make_reviewed_project(root)
        finding_id = helper.finding_ids(v1)[0]
        adjudication.append_finding_decision(
            v1.root, finding_id, decision="accept",
            reason="The original failure should be repaired.",
            actions=[("narrow_claim", "Narrow the Claim and preserve its support chain.")],
        )
        source = (FIXTURE / "manuscript.md").read_text(encoding="utf-8") + "\n"
        manuscript_v2 = root / "manuscript-v2.md"
        manuscript_v2.write_text(source, encoding="utf-8")
        v2 = workbench.import_document_version(v1, manuscript_v2)
        raw = json.loads((FIXTURE / "raw-ir.json").read_text(encoding="utf-8"))
        raw["source"] = {"name": manuscript_v2.name, "sha256": contracts.sha256_bytes(manuscript_v2.read_bytes())}
        workbench.collect_raw_attempt(v2, workbench.json_bytes(raw), method="file", source_name="raw-v2.json", producer_label="v2-model")
        workbench.rebuild_workspace(v2)
        versioning.build_structural_diff(v2.root)
        analysis, _ = lineage.prepare_lineage_analysis(v2.root)
        _, from_bytes = workbench._read_json(analysis.from_ir); _, to_bytes = workbench._read_json(analysis.to_ir); _, diff_bytes = workbench._read_json(analysis.structural_diff)
        proposal_items = [
            {"proposal_id": f"LP{index}", "from_claims": [f"V1:C{index}"], "to_claims": [f"V2:C{index}"], "relation": "unchanged", "semantic_changes": [], "reason": "Exact Claim content is retained.", "basis_refs": [f"V1:C{index}", f"V2:C{index}"], "uncertainty": ""}
            for index in range(1, 4)
        ]
        if removed:
            proposal_items = [
                {"proposal_id": "LP1", "from_claims": ["V1:C1"], "to_claims": [], "relation": "removed", "semantic_changes": ["other"], "reason": "The original Claim was removed.", "basis_refs": ["V1:C1"], "uncertainty": ""},
                {"proposal_id": "LP2", "from_claims": [], "to_claims": ["V2:C1"], "relation": "new", "semantic_changes": ["other"], "reason": "V2:C1 is treated as a new Claim.", "basis_refs": ["V2:C1"], "uncertainty": ""},
                {"proposal_id": "LP3", "from_claims": ["V1:C2"], "to_claims": ["V2:C2"], "relation": "unchanged", "semantic_changes": [], "reason": "Exact Claim content is retained.", "basis_refs": ["V1:C2", "V2:C2"], "uncertainty": ""},
                {"proposal_id": "LP4", "from_claims": ["V1:C3"], "to_claims": ["V2:C3"], "relation": "unchanged", "semantic_changes": [], "reason": "Exact Claim content is retained.", "basis_refs": ["V1:C3", "V2:C3"], "uncertainty": ""},
            ]
        elif split:
            proposal_items = [
                {"proposal_id": "LP1", "from_claims": ["V1:C1"], "to_claims": ["V2:C1", "V2:C2"], "relation": "split", "semantic_changes": ["concept_reframed"], "reason": "The source Claim was split into two descendants.", "basis_refs": ["V1:C1", "V2:C1", "V2:C2"], "uncertainty": ""},
                {"proposal_id": "LP2", "from_claims": ["V1:C2"], "to_claims": ["V2:C2"], "relation": "unchanged", "semantic_changes": [], "reason": "Exact Claim content is retained.", "basis_refs": ["V1:C2", "V2:C2"], "uncertainty": ""},
                {"proposal_id": "LP3", "from_claims": ["V1:C3"], "to_claims": ["V2:C3"], "relation": "unchanged", "semantic_changes": [], "reason": "Exact Claim content is retained.", "basis_refs": ["V1:C3", "V2:C3"], "uncertainty": ""},
            ]
        proposals = {
            "schema_version": 1, "artifact": "claim-lineage-proposals",
            "source": {"structural_diff_sha256": contracts.sha256_bytes(diff_bytes), "from_ir_sha256": contracts.sha256_bytes(from_bytes), "to_ir_sha256": contracts.sha256_bytes(to_bytes)},
            "status": "complete", "unverified": [],
            "proposals": proposal_items,
        }
        lineage.collect_lineage_proposals(v2.root, workbench.json_bytes(proposals), method="file", source_name="lineage.json", producer_label="lineage-model")
        lineage.append_lineage_decision(v2.root, proposal_ids=[item["proposal_id"] for item in proposal_items], decision="confirm", human_note="Each Claim correspondence was checked.")
        return v1, v2, finding_id

    def result(self, paths: resolution.ResolutionPaths, verdict: str | list[str] = "fail"):
        run = json.loads(paths.record.read_text(encoding="utf-8"))
        target_ir = json.loads((paths.root / "target-argument-ir.json").read_text(encoding="utf-8"))
        verdicts = [verdict] * len(run["descendant_claims"]) if isinstance(verdict, str) else verdict
        results = []
        for target, item_verdict in zip(run["descendant_claims"], verdicts):
            support_refs = []; support_paths = []
            if item_verdict == "pass":
                local_claim = target.split(":", 1)[1]
                eligible = argument_ir._eligible_pass_support_paths(target_ir, local_claim)
                local_ref = next(iter(eligible))
                support_ref = f"V2:{local_ref}"; support_refs = [support_ref]
                support_paths = [{"support_ref": support_ref, "relation_ids": [f"V2:{relation_id}" for relation_id in eligible[local_ref]]}]
            results.append({
                "target_claim": target, "verdict": item_verdict,
                "reason": "The original Lens still finds the same failure." if item_verdict == "fail" else "The original Lens now passes." if item_verdict == "pass" else "The original Lens remains uncertain.",
                "basis_refs": [target, *support_refs], "support_refs": support_refs, "support_paths": support_paths,
                "analysis": "Applied the original snapshotted Lens only.",
            })
        return {
            "schema_version": 1, "artifact": "resolution-retest-results",
            "source": {
                "original_finding_sha256": contracts.sha256_bytes((paths.root / "original-finding.json").read_bytes()),
                "target_ir_sha256": contracts.sha256_bytes((paths.root / "target-argument-ir.json").read_bytes()),
                "lens_protocol_sha256": contracts.sha256_bytes((paths.root / "lens-protocol.json").read_bytes()),
            },
            "status": "complete", "unverified": [],
            "results": results,
        }

    def test_original_lens_retest_maps_status_then_requires_human_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, v2, finding_id = self.make_chain(Path(temporary))
            paths, created = resolution.prepare_resolution(v2.root, finding_id, from_version="V1", to_version="V2")
            self.assertTrue(created)
            prompt = (paths.root / "resolution-retest-prompt.md").read_text(encoding="utf-8")
            self.assertIn("Do not answer the generic question", prompt)
            _, attempt = resolution.collect_resolution_results(v2.root, workbench.json_bytes(self.result(paths)), resolution_id=paths.resolution_id, method="file", source_name="retest.json", producer_label="retest-model")
            self.assertEqual(attempt["validation"], {"status": "valid", "errors": []})
            proposal = json.loads((paths.derived_dir("attempt-0001") / "resolution-proposal.json").read_text(encoding="utf-8"))
            self.assertEqual(proposal["proposed_status"], "unresolved")
            self.assertEqual(proposal["field_provenance"]["proposed_status"]["origin"], "deterministic")
            decision_path = resolution.append_resolution_decision(v2.root, resolution_id=paths.resolution_id, decision="confirm", reason="The original failure persists.")
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
            self.assertEqual(decision["final_status"], "unresolved")
            self.assertEqual(decision["provenance"]["origin"], "human-confirmed")
            rendered = resolution.render_resolution(v2.root)
            self.assertIn("Original Lens", rendered)
            self.assertIn("Human decision", rendered)
            self.assertEqual(resolution.verify_resolutions(v2.root), [])
            self.assertEqual(workbench.verify_project_versions(v2.root), [])
            proposal_path = paths.derived_dir("attempt-0001") / "resolution-proposal.json"
            proposal_path.write_text("tampered\n", encoding="utf-8")
            self.assertTrue(any("not reproducible" in error for error in resolution.verify_resolutions(v2.root)))
            _, changed = resolution.rebuild_resolution_attempt(paths, "attempt-0001")
            self.assertTrue(changed)
            self.assertEqual(resolution.verify_resolutions(v2.root), [])

    def test_invalid_retest_is_preserved_and_cannot_create_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, v2, finding_id = self.make_chain(Path(temporary))
            paths, _ = resolution.prepare_resolution(v2.root, finding_id, from_version="V1", to_version="V2")
            invalid = self.result(paths)
            invalid["results"][0]["target_claim"] = "V2:C999"
            attempt_dir, attempt = resolution.collect_resolution_results(v2.root, workbench.json_bytes(invalid), resolution_id=None, method="file", source_name="bad.json", producer_label=None)
            self.assertEqual(attempt["validation"]["status"], "unusable")
            self.assertTrue((attempt_dir / "response.json").is_file())
            self.assertFalse(paths.derived_dir("attempt-0001").exists())
            with self.assertRaises(workbench.WorkbenchError):
                resolution.append_resolution_decision(v2.root, resolution_id=None, decision="confirm", reason="No valid retest.")
            self.assertEqual(resolution.verify_resolutions(v2.root), [])

    def test_confirmed_removal_proposes_obsolete_without_model_retest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, v2, finding_id = self.make_chain(Path(temporary), removed=True)
            paths, _ = resolution.prepare_resolution(v2.root, finding_id, from_version="V1", to_version="V2")
            proposal_path = paths.root / "derived" / "obsolete" / "resolution-proposal.json"
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
            self.assertEqual(proposal["proposed_status"], "obsolete")
            self.assertEqual(proposal["descendant_claims"], [])
            self.assertFalse(any(paths.attempts_dir.iterdir()))
            decision_path = resolution.append_resolution_decision(v2.root, resolution_id=None, decision="confirm", reason="The accepted Finding has no descendant Claim.")
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
            self.assertEqual(decision["final_status"], "obsolete")
            self.assertEqual(resolution.verify_resolutions(v2.root), [])

    def test_split_descendants_map_mixed_retest_to_partially_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, v2, finding_id = self.make_chain(Path(temporary), split=True)
            paths, _ = resolution.prepare_resolution(v2.root, finding_id, from_version="V1", to_version="V2")
            _, attempt = resolution.collect_resolution_results(
                v2.root, workbench.json_bytes(self.result(paths, ["pass", "fail"])),
                resolution_id=None, method="file", source_name="mixed.json", producer_label="retest-model",
            )
            self.assertEqual(attempt["validation"]["status"], "valid")
            proposal = json.loads((paths.derived_dir("attempt-0001") / "resolution-proposal.json").read_text(encoding="utf-8"))
            self.assertEqual(proposal["proposed_status"], "partially_resolved")
            self.assertEqual(proposal["retest_summary"], {"pass": 1, "fail": 1, "uncertain": 0})
            self.assertEqual(resolution.verify_resolutions(v2.root), [])

    def test_resolution_cli_runs_without_editing_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, v2, finding_id = self.make_chain(Path(temporary))
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(critic_runner.main(["ir", "resolve", "prepare", str(v2.root), finding_id, "--from-version", "V1", "--to-version", "V2"]), 0)
            paths = resolution.selected_resolution(v2.root, None)
            result_file = Path(temporary) / "resolution-result.json"
            result_file.write_bytes(workbench.json_bytes(self.result(paths)))
            with redirect_stdout(output):
                self.assertEqual(critic_runner.main(["ir", "resolve", "collect", str(v2.root), "--file", str(result_file), "--producer-label", "cli-model"]), 0)
                self.assertEqual(critic_runner.main(["ir", "resolve", "decide", str(v2.root), "--decision", "confirm", "--reason", "Retest confirms persistence."]), 0)
                self.assertEqual(critic_runner.main(["ir", "resolve", "show", str(v2.root)]), 0)
            self.assertIn("Finding Resolution", output.getvalue())
            self.assertIn("unresolved", output.getvalue())
            self.assertEqual(workbench.verify_project_versions(v2.root), [])


if __name__ == "__main__":
    unittest.main()
