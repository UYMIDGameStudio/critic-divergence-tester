"""Malformed model replies remain recoverable evidence, never derived judgments."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import argument_citations as citations
import argument_perspective as perspective
import argument_resolution as resolution
import argument_workbench as workbench
from test import test_argument_citations, test_argument_perspective, test_argument_resolution


class ReviewProtocolBoundaryTests(unittest.TestCase):
    def assert_archived_unusable(self, collector, paths, payload, derived):
        raw = workbench.json_bytes(payload)
        attempt_dir, record = collector(raw)
        self.assertEqual(record["validation"]["status"], "unusable")
        self.assertTrue(record["validation"]["errors"])
        self.assertEqual((attempt_dir / "response.json").read_bytes(), raw)
        self.assertFalse(derived(attempt_dir.name).exists())

    def test_perspective_malformed_collections_are_archived_then_recover(self):
        helper = test_argument_perspective.PerspectiveReviewTests()
        with tempfile.TemporaryDirectory() as directory:
            project = helper.make_project(Path(directory))
            paths, _ = perspective.prepare_perspective_review(
                project.root, lens_id="methodological-individualism", review_scope="thesis-chain",
            )
            def collect(raw):
                return perspective.collect_perspective_results(
                    project.root, raw, review_id=paths.review_id, method="file",
                    source_name="model.json", producer_label="boundary-test",
                )
            for bad in (None, 7):
                with self.subTest(results=bad):
                    value = helper.results(paths)
                    value["results"] = bad
                    self.assert_archived_unusable(collect, paths, value, paths.derived_attempt_dir)
            for bad in (None, [{}], [[]]):
                with self.subTest(basis_refs=bad):
                    value = helper.results(paths)
                    value["results"][0]["basis_refs"] = bad
                    self.assert_archived_unusable(collect, paths, value, paths.derived_attempt_dir)
            _, record = collect(workbench.json_bytes(helper.results(paths)))
            self.assertEqual(record["validation"], {"status": "valid", "errors": []})
            self.assertEqual(perspective.verify_perspective_reviews(project.root), [])

    def test_resolution_malformed_collections_are_archived_then_recover(self):
        helper = test_argument_resolution.ArgumentResolutionTests()
        with tempfile.TemporaryDirectory() as directory:
            _, project, finding_id = helper.make_chain(Path(directory))
            paths, _ = resolution.prepare_resolution(project.root, finding_id, from_version="V1", to_version="V2")
            def collect(raw):
                return resolution.collect_resolution_results(
                    project.root, raw, resolution_id=paths.resolution_id, method="file",
                    source_name="model.json", producer_label="boundary-test",
                )
            mutations = [("results", None), ("results", 7)]
            mutations += [(field, bad) for field in ("basis_refs", "support_refs", "support_paths") for bad in (None, [{}], [[]])]
            for field, bad in mutations:
                with self.subTest(field=field, value=bad):
                    value = helper.result(paths)
                    if field == "results":
                        value[field] = bad
                    else:
                        value["results"][0][field] = copy.deepcopy(bad)
                    self.assert_archived_unusable(collect, paths, value, paths.derived_dir)
            _, record = collect(workbench.json_bytes(helper.result(paths)))
            self.assertEqual(record["validation"], {"status": "valid", "errors": []})
            self.assertEqual(resolution.verify_resolutions(project.root), [])

    def test_citation_malformed_outcomes_are_archived_then_recover(self):
        helper = test_argument_citations.ArgumentCitationTests()
        with tempfile.TemporaryDirectory() as directory:
            project = helper.make_project(Path(directory))
            paths, _ = citations.prepare_citation_audit(project.root)
            def collect(raw):
                return citations.collect_citation_results(
                    project.root, raw, audit_id=paths.audit_id, version_id="V1", method="file",
                    source_name="model.json", producer_label="boundary-test",
                )
            for bad in (None, 7):
                with self.subTest(outcomes=bad):
                    value = helper.result(paths)
                    value["outcomes"] = bad
                    raw = workbench.json_bytes(value)
                    attempt_dir, record = collect(raw)
                    self.assertEqual(record["validation"]["status"], "unusable")
                    self.assertEqual((attempt_dir / "response.json").read_bytes(), raw)
                    self.assertFalse(paths.index.exists())
            _, record = collect(workbench.json_bytes(helper.result(paths)))
            self.assertEqual(record["validation"], {"status": "valid", "errors": []})
            self.assertEqual(citations.verify_citation_audits(project.root), [])


if __name__ == "__main__":
    unittest.main()
