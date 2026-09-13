from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import argument_contracts as contracts
import argument_ir as ir
import argument_perspective as perspective
import argument_review as review
import argument_workbench as workbench
from test.test_argument_ir import SOURCE_TEXT, valid_ir


def encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")


def library(version: int = 3) -> dict:
    value = json.loads((REPO_ROOT / "ir/social-science-checks.json").read_bytes())
    value["schema_version"] = version
    value["checks"] = [value["checks"][0]]
    value["checks"][0]["applies_to"] = {"claim_types": ["*"], "methods": ["*"]}
    if version == 1:
        value["checks"][0].pop("evidence_policy")
    return value


def plan(version: int = 3) -> dict:
    return ir.build_check_plan(
        valid_ir(SOURCE_TEXT.encode("utf-8")), library(version),
        ir_sha256="a" * 64, library_sha256="b" * 64, depth="full",
    )


def results(check_plan: dict, digest: str = "c" * 64) -> dict:
    version = check_plan["schema_version"]
    rows = []
    for task in check_plan["tasks"]:
        row = {
            "task_id": task["id"], "verdict": "uncertain", "reason": "Insufficient evidence.",
            "consequence": "Inspect the original argument.",
        }
        if version == 1:
            row["evidence_refs"] = [task["claim_id"]]
        else:
            row.update(execution_status="evaluated", basis_refs=[task["claim_id"]], support_refs=[])
            if version == 3:
                row["support_paths"] = []
        rows.append(row)
    return {
        "schema_version": version, "artifact": "argument-check-results",
        "source": {"plan_sha256": digest}, "status": "complete", "unverified": [], "results": rows,
    }


def project(root: Path) -> workbench.WorkspacePaths:
    fixture = REPO_ROOT / "test/fixtures/workbench-demo"
    paths = workbench.initialize_workspace(fixture / "manuscript.md", root / "project", title="Protocol security")
    workbench.collect_raw_attempt(
        paths.root, (fixture / "raw-ir.json").read_bytes(), method="file",
        source_name="raw-ir.json", producer_label="fixture",
    )
    workbench.rebuild_workspace(paths.root)
    return paths


def value_paths(value: object, prefix: tuple = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield (*prefix, key)
            yield from value_paths(child, (*prefix, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield (*prefix, index)
            yield from value_paths(child, (*prefix, index))


class IRProtocolSecurityTests(unittest.TestCase):
    def test_check_protocol_schema_versions_are_integers(self) -> None:
        check_plan = plan(1)
        cases = (
            (library(1), ir.validate_check_library),
            (check_plan, ir.validate_check_plan),
            (results(check_plan), lambda v: ir.validate_check_results(v, check_plan, plan_sha256="c" * 64)),
        )
        for value, validator in cases:
            self.assertEqual(validator(value), [])
            for invalid in (True, 1.0, [], {}):
                with self.subTest(artifact=value["artifact"], version=invalid):
                    changed = copy.deepcopy(value)
                    changed["schema_version"] = invalid
                    self.assertTrue(validator(changed))

    def test_malformed_task_and_result_types_return_errors(self) -> None:
        check_plan = plan()
        for field in ("claim_id", "check_id"):
            for invalid in ([], {}):
                with self.subTest(task_field=field, invalid=invalid):
                    changed = copy.deepcopy(check_plan)
                    changed["tasks"][0][field] = invalid
                    self.assertTrue(ir.validate_check_plan(changed))
        for field in ("task_id", "verdict", "execution_status"):
            for invalid in ([], {}):
                with self.subTest(result_field=field, invalid=invalid):
                    value = results(check_plan)
                    value["results"][0][field] = invalid
                    self.assertTrue(ir.validate_check_results(value, check_plan, plan_sha256="c" * 64))
        for invalid in (True, 1, "C1", {"C1": True}, [[]], [{}]):
            with self.subTest(claim_selection=invalid):
                with self.assertRaises(ir.ArgumentIRError):
                    ir.build_check_plan(
                        check_plan["argument_ir"], library(), ir_sha256="a" * 64,
                        library_sha256="b" * 64, depth="core", claim_ids=invalid,
                    )

    def test_protocol_text_rejects_invalid_unicode_before_persistence(self) -> None:
        check_plan = plan()
        bad_library = library()
        bad_library["checks"][0]["label"] = "\ud800"
        bad_plan = copy.deepcopy(check_plan)
        bad_plan["checks"][0]["label"] = "\ud800"
        bad_results = results(check_plan)
        bad_results["results"][0]["reason"] = "\ud800"
        for value, validator in (
            (bad_library, ir.validate_check_library),
            (bad_plan, ir.validate_check_plan),
            (bad_results, lambda v: ir.validate_check_results(v, check_plan, plan_sha256="c" * 64)),
        ):
            with self.subTest(artifact=value["artifact"]):
                self.assertTrue(validator(value))

    def test_invalid_model_results_are_fully_archived(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = project(Path(temporary))
            paths, _ = review.prepare_rule_review(workspace.root, REPO_ROOT / "ir/social-science-checks.json", depth="core")
            check_plan = json.loads(paths.plan.read_bytes())
            digest = hashlib.sha256(paths.plan.read_bytes()).hexdigest()
            for index, (field, invalid) in enumerate((
                ("task_id", []), ("task_id", {}), ("verdict", []), ("verdict", {}),
                ("basis_refs", ["V2:C1"]), ("human_reviewed", True),
            ), 1):
                with self.subTest(field=field, invalid=invalid):
                    value = results(check_plan, digest)
                    value["results"][0][field] = invalid
                    response = encoded(value)
                    attempt, record = review.collect_review_results(
                        workspace.root, response, review_id=paths.review_id, method="file",
                        source_name=f"invalid-{index}.json", producer_label="untrusted-model",
                    )
                    self.assertEqual(record["validation"]["status"], "unusable")
                    self.assertTrue(record["validation"]["errors"])
                    self.assertEqual((attempt / "response.json").read_bytes(), response)
                    self.assertFalse(paths.derived_attempt_dir(attempt.name).exists())
            original = json.dumps(results(check_plan, digest), ensure_ascii=True).encode("utf-8")
            for index, response in enumerate((
                original.replace(b'"reason": "Insufficient evidence."', b'"reason": "Insufficient evidence.", "reason": "duplicate"', 1),
                original.replace(b'"reason": "Insufficient evidence."', b'"reason": NaN', 1),
                original.replace(b'"reason": "Insufficient evidence."', b'"reason": Infinity', 1),
                original.replace(b'"reason": "Insufficient evidence."', b'"reason": 1e999', 1),
                original.replace(b'"reason": "Insufficient evidence."', b'"reason": "\\ud800"', 1),
            )):
                with self.subTest(raw_json=index):
                    attempt, record = review.collect_review_results(
                        workspace.root, response, review_id=paths.review_id, method="file",
                        source_name=f"invalid-json-{index}.json", producer_label="untrusted-model",
                    )
                    self.assertEqual(record["validation"]["status"], "unusable")
                    self.assertEqual((attempt / "response.json").read_bytes(), response)
                    self.assertFalse(paths.derived_attempt_dir(attempt.name).exists())
            self.assertEqual(workbench.verify_workspace(workspace), [])

    def test_base_contract_rejects_noninteger_schema_and_invalid_unicode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = project(Path(temporary))
            value = json.loads((workspace.root / "project.json").read_bytes())
            self.assertEqual(contracts.validate_project(value), [])
            for key, invalid in (("schema_version", True), ("schema_version", 1.0), ("title", "\ud800")):
                with self.subTest(key=key, invalid=repr(invalid)):
                    changed = copy.deepcopy(value)
                    changed[key] = invalid
                    self.assertTrue(contracts.validate_project(changed))
            changed = copy.deepcopy(value)
            changed["provenance"]["origin"] = []
            self.assertTrue(contracts.validate_project(changed))
            changed = copy.deepcopy(value)
            changed["parents"] = [{"role": {}, "artifact": "opaque", "sha256": "a" * 64}]
            self.assertTrue(contracts.validate_project(changed))

    def test_reviewed_record_requires_correction_backed_human_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = project(Path(temporary))
            value = json.loads(workspace.reviewed_record.read_bytes())
            self.assertEqual(contracts.validate_reviewed_ir_record(value), [])
            for field, origin, source in (
                ("C1.text", "human-confirmed", "raw-ir-attempt"),
                ("C1.text", "human-confirmed", "IC0001"),
                ("C1.position", "human-confirmed", "IC0001"),
                ("C1.text", "deterministic", "workbench-materializer-v1"),
                ("source.sha256", "model-derived", "raw-ir-attempt"),
            ):
                with self.subTest(field=field, origin=origin, source=source):
                    changed = copy.deepcopy(value)
                    changed["field_provenance"][field] = {"origin": origin, "source": source}
                    self.assertTrue(contracts.validate_reviewed_ir_record(changed))
            changed = copy.deepcopy(value)
            changed["correction_sha256s"] = ["f" * 64]
            self.assertTrue(contracts.validate_reviewed_ir_record(changed))
            changed = copy.deepcopy(value)
            changed["parents"][0]["artifact"] = "opaque-external-payload"
            self.assertTrue(contracts.validate_reviewed_ir_record(changed))
            workbench.append_correction(workspace.root, {
                "kind": "update_node", "target": "raw:C1", "changes": {"text": "Human clarified argument"},
            }, reason="Author clarification")
            workbench.rebuild_workspace(workspace.root)
            corrected = json.loads(workspace.reviewed_record.read_bytes())
            self.assertEqual(contracts.validate_reviewed_ir_record(corrected), [])
            self.assertEqual(corrected["field_provenance"]["C1.text"], {"origin": "human-confirmed", "source": "IC0001"})
            for key, invalid in (("role", "correction-9999"), ("artifact", "raw-ir-attempt"), ("sha256", "f" * 64)):
                with self.subTest(correction_parent=key):
                    changed = copy.deepcopy(corrected)
                    changed["parents"][-1][key] = invalid
                    self.assertTrue(contracts.validate_reviewed_ir_record(changed))

    def test_validators_tolerate_wrong_shapes_at_each_nested_field(self) -> None:
        check_plan = plan()
        candidates = [
            (valid_ir(SOURCE_TEXT.encode("utf-8")), ir.validate_argument_ir),
            (library(), ir.validate_check_library), (check_plan, ir.validate_check_plan),
            (results(check_plan), lambda v: ir.validate_check_results(v, check_plan, plan_sha256="c" * 64)),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            workspace = project(Path(temporary))
            workbench.append_correction(workspace.root, {
                "kind": "update_node", "target": "raw:C1", "changes": {"text": "Human clarified argument"},
            })
            workbench.rebuild_workspace(workspace.root)
            rule_paths, _ = review.prepare_rule_review(workspace.root, REPO_ROOT / "ir/social-science-checks.json", depth="core")
            rule_plan = json.loads(rule_paths.plan.read_bytes())
            review.collect_review_results(
                workspace.root, encoded(results(rule_plan, hashlib.sha256(rule_paths.plan.read_bytes()).hexdigest())),
                review_id=rule_paths.review_id, method="file", source_name="results.json", producer_label="fixture",
            )
            perspective_paths, _ = perspective.prepare_perspective_review(
                workspace.root, lens_id="methodological-individualism", review_scope="claim", claim_ids=["C1"],
            )
            perspective_value = {
                "schema_version": 1, "artifact": "perspective-lens-results",
                "source": {field: hashlib.sha256(path.read_bytes()).hexdigest() for field, path in (
                    ("plan_sha256", perspective_paths.plan), ("target_ir_sha256", perspective_paths.target_ir),
                    ("protocol_sha256", perspective_paths.protocol),
                )},
                "status": "complete", "unverified": [], "results": [{
                    "result_id": "P1", "target_claim": "C1", "verdict": "uncertain", "reason": "Inspect evidence.",
                    "basis_refs": ["C1"], "framework_analysis": "Mechanism is unstated.", "consequence": "Inspect source.",
                }],
            }
            perspective.collect_perspective_results(
                workspace.root, encoded(perspective_value), review_id=perspective_paths.review_id,
                method="file", source_name="perspective.json", producer_label="fixture",
            )
            for path in workspace.root.rglob("*.json"):
                value = json.loads(path.read_bytes())
                validator = contracts.VALIDATORS.get(value.get("artifact")) if isinstance(value, dict) else None
                if validator is not None and validator.__module__ == "contracts.core":
                    candidates.append((value, validator))
            for original, validator in candidates:
                self.assertEqual(validator(original), [])
                for location in value_paths(original):
                    for invalid in ([], {}, None, float("nan"), float("inf"), "\ud800"):
                        with self.subTest(artifact=original["artifact"], path=location, invalid=repr(invalid)):
                            changed = copy.deepcopy(original)
                            parent = changed
                            for part in location[:-1]:
                                parent = parent[part]
                            parent[location[-1]] = invalid
                            errors = validator(changed)
                            self.assertIsInstance(errors, list)
                            if isinstance(invalid, float) or invalid == "\ud800":
                                self.assertTrue(errors)

    def test_eight_languages_preserve_exact_unicode_positions(self) -> None:
        sentences = (
            "English evidence.", "简体中文的论据。", "繁體中文的論據。", "Größe über zwölf.",
            "L’élève étudie l’œuvre.", "日本語の漢字と仮名。", "Русский текст и довод.", "Rōma: æquitas et cœlum.",
        )
        text = "introductory line\n" + "\n".join("😀  " + line for line in sentences)
        for encoding in ("utf-8", "utf-16", "utf-32"):
            data = text.encode(encoding)
            value = valid_ir(data)
            value["evidence"] = value["assumptions"] = value["citations"] = value["relations"] = []
            prototype = value["claims"][0]
            value["claims"] = [dict(prototype, id=f"C{index}", text=line, source_quote=line, position="untrusted") for index, line in enumerate(sentences, 1)]
            normalized = ir.canonicalize_argument_ir(value, source_bytes=data, source_name="article.md", source_encoding=encoding)
            for index, line in enumerate(sentences, 2):
                with self.subTest(encoding=encoding, language=line):
                    self.assertEqual(normalized["claims"][index - 2]["source_quote"], line)
                    self.assertEqual(normalized["claims"][index - 2]["position"], f"L{index}:C4-L{index}:C{4 + len(line)}")
            changed = copy.deepcopy(value)
            changed["source"]["sha256"] = "0" * 64
            self.assertTrue(ir.validate_argument_ir(changed, source_bytes=data, source_encoding=encoding))

    def test_raw_ir_cannot_add_human_state_or_foreign_version_references(self) -> None:
        data = SOURCE_TEXT.encode("utf-8")
        original = valid_ir(data)
        for field, invalid in (("human_reviewed", True), ("field_provenance", {})):
            changed = copy.deepcopy(original)
            changed["claims"][0][field] = invalid
            self.assertTrue(ir.validate_argument_ir(changed, source_bytes=data))
        for invalid in ("V2:C1", "raw:V2:C1", "C１", "C1\u200b"):
            changed = copy.deepcopy(original)
            changed["relations"][0]["to"] = invalid
            self.assertTrue(ir.validate_argument_ir(changed, source_bytes=data))


if __name__ == "__main__":
    unittest.main()
