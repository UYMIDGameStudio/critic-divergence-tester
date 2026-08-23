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

import argument_contracts as contracts  # noqa: E402
import argument_versioning as versioning  # noqa: E402
import argument_workbench as workbench  # noqa: E402
import critic_runner  # noqa: E402


FIXTURE = REPO_ROOT / "test" / "fixtures" / "workbench-demo"


class ArgumentVersioningTests(unittest.TestCase):
    @staticmethod
    def encoded(value: object) -> bytes:
        return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )

    def make_two_versions(
        self, root: Path
    ) -> tuple[workbench.WorkspacePaths, workbench.WorkspacePaths]:
        v1 = workbench.initialize_workspace(
            FIXTURE / "manuscript.md",
            root / "versioning.argument-workbench",
            title="Versioning demo",
        )
        workbench.collect_raw_attempt(
            v1,
            (FIXTURE / "raw-ir.json").read_bytes(),
            method="file",
            source_name="raw-ir.json",
            producer_label="v1-model",
        )
        workbench.rebuild_workspace(v1)

        source_v1 = (FIXTURE / "manuscript.md").read_text(encoding="utf-8")
        source_v2 = source_v1.replace(
            "本文主张，结构性的压力最后总会收敛成一个具体的人。",
            "本文只主张，在所考察的三个案例中，结构性压力都收敛成了一个具体的人。",
        ).replace(
            "在甲案例中，推荐规则调整一周后，主要批评从平台转向主播；乙案例和丙案例也出现了相似变化。",
            "在甲案例中，推荐规则调整一周后，主要批评从平台转向主播；乙案例和丙案例也出现了相似变化。\n\n另一个制度争议始终围绕规则本身，没有形成单一人物焦点。",
        )
        manuscript_v2 = root / "manuscript-v2.md"
        manuscript_v2.write_text(source_v2, encoding="utf-8")
        v2 = workbench.import_document_version(v1, manuscript_v2)

        raw_v1 = json.loads((FIXTURE / "raw-ir.json").read_text(encoding="utf-8"))
        raw_v2 = copy.deepcopy(raw_v1)
        raw_v2["source"] = {
            "name": manuscript_v2.name,
            "sha256": workbench.sha256_bytes(manuscript_v2.read_bytes()),
        }
        old_c1, old_c2, old_c3 = raw_v2["claims"]
        changed_c1 = copy.deepcopy(old_c1)
        changed_c1.update(
            id="C2",
            text="在所考察的三个案例中，结构性压力都收敛成了一个具体的人",
            source_quote="本文只主张，在所考察的三个案例中，结构性压力都收敛成了一个具体的人。",
        )
        renumbered_c2 = copy.deepcopy(old_c2)
        renumbered_c2.update(id="C1", role="intermediate")
        raw_v2["claims"] = [renumbered_c2, changed_c1, old_c3]
        old_e1, old_e2 = raw_v2["evidence"]
        old_e1["id"] = "E2"
        old_e2["id"] = "E1"
        raw_v2["evidence"] = [
            old_e2,
            old_e1,
            {
                "id": "E3",
                "text": "存在未形成单一人物焦点的制度争议",
                "source_quote": "另一个制度争议始终围绕规则本身，没有形成单一人物焦点。",
                "position": "新增段落",
                "kind": "case",
            },
        ]
        raw_v2["relations"] = [
            {"id": "R1", "type": "supports", "from": "E2", "to": "C1"},
            {"id": "R2", "type": "supports", "from": "E1", "to": "C2"},
            {"id": "R3", "type": "qualifies", "from": "C1", "to": "C2"},
            {"id": "R4", "type": "assumes", "from": "A1", "to": "C2"},
            {"id": "R5", "type": "cites", "from": "Z1", "to": "C1"},
            {"id": "R6", "type": "supports", "from": "C1", "to": "C3"},
            {"id": "R7", "type": "qualifies", "from": "E3", "to": "C2"},
        ]
        workbench.collect_raw_attempt(
            v2,
            self.encoded(raw_v2),
            method="file",
            source_name="raw-v2.json",
            producer_label="v2-model",
        )
        workbench.rebuild_workspace(v2)
        return v1, v2

    def test_structural_diff_is_exact_reproducible_and_not_semantic_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            v1, v2 = self.make_two_versions(Path(temporary))
            paths, changed = versioning.build_structural_diff(v2.root)
            self.assertTrue(changed)
            record = json.loads(paths.record.read_text(encoding="utf-8"))
            self.assertEqual(contracts.validate_structural_version_diff(record), [])
            self.assertEqual(record["from_version"], "V1")
            self.assertEqual(record["to_version"], "V2")
            self.assertGreater(record["summary"]["source_hunks"], 0)
            self.assertEqual(record["summary"]["evidence_added"], 1)
            self.assertTrue(
                any(
                    entry["from_ref"] == "V1:C2"
                    and entry["to_ref"] == "V2:C1"
                    and "role" in entry["changed_fields"]
                    for entry in record["node_diff"]["literal_anchor_modified"]
                )
            )
            self.assertTrue(
                any(
                    entry["ref"] == "V1:C1"
                    for entry in record["node_diff"]["removed"]
                )
            )
            self.assertTrue(
                any(
                    entry["ref"] == "V2:C2"
                    for entry in record["node_diff"]["added"]
                )
            )
            markdown = paths.markdown.read_text(encoding="utf-8")
            self.assertIn("not semantic Claim lineage", markdown)
            self.assertIn("V1:C2` → `V2:C1", markdown)

            first = (paths.record.read_bytes(), paths.markdown.read_bytes())
            _, changed_again = versioning.build_structural_diff(v2.root)
            self.assertFalse(changed_again)
            self.assertEqual(first, (paths.record.read_bytes(), paths.markdown.read_bytes()))
            self.assertEqual(workbench.verify_project_versions(v1.root), [])

    def test_structural_diff_cli_and_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, v2 = self.make_two_versions(Path(temporary))
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    critic_runner.main(["ir", "diff-versions", str(v2.root)]),
                    0,
                )
            self.assertIn("Structural diff generated", stdout.getvalue())
            paths = versioning.list_structural_diffs(v2.root)[0]
            paths.markdown.write_text("tampered\n", encoding="utf-8")
            self.assertTrue(
                any(
                    "not reproducible" in error
                    for error in workbench.verify_project_versions(v2.root)
                )
            )
            _, changed = versioning.build_structural_diff(v2.root)
            self.assertTrue(changed)
            self.assertEqual(workbench.verify_project_versions(v2.root), [])


if __name__ == "__main__":
    unittest.main()
