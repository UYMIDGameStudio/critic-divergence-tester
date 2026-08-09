from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import argument_contracts as contracts  # noqa: E402


STAMP = "2026-08-08T00:00:00+00:00"


def encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def base(
    artifact: str,
    artifact_id: str,
    lifecycle: str,
    origin: str,
    *,
    parents: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "artifact": artifact,
        "artifact_id": artifact_id,
        "lifecycle": lifecycle,
        "provenance": {
            "origin": origin,
            "created_at": STAMP,
            "producer": "test",
        },
        "parents": parents or [],
    }


def parent(role: str, artifact: str, value: object) -> dict[str, str]:
    return {
        "role": role,
        "artifact": artifact,
        "sha256": contracts.sha256_bytes(encoded(value)),
    }


class ArgumentContractTests(unittest.TestCase):
    def test_project_document_version_contracts_are_strict(self) -> None:
        project = {
            **base("argument-project", "P1", "immutable", "human-confirmed"),
            "project_id": "P1",
            "title": "结构的替身",
        }
        document = {
            **base(
                "argument-document",
                "D1",
                "immutable",
                "human-confirmed",
                parents=[parent("project", "argument-project", project)],
            ),
            "project_id": "P1",
            "document_id": "D1",
            "title": "结构的替身",
        }
        version = {
            **base(
                "document-version",
                "V1",
                "immutable",
                "human-confirmed",
                parents=[parent("document", "argument-document", document)],
            ),
            "project_id": "P1",
            "document_id": "D1",
            "version_id": "V1",
            "source": {
                "name": "draft.md",
                "relative_path": "source/draft.md",
                "sha256": "a" * 64,
            },
            "parent_version": None,
        }
        self.assertEqual(contracts.validate_project(project), [])
        self.assertEqual(contracts.validate_document(document), [])
        self.assertEqual(contracts.validate_document_version(version), [])
        self.assertEqual(
            contracts.validate_contract_bundle(
                [(project, encoded(project)), (document, encoded(document)), (version, encoded(version))]
            ),
            [],
        )

        drift = copy.deepcopy(version)
        drift["quality_score"] = 91
        self.assertTrue(any("exactly" in error for error in contracts.validate_document_version(drift)))
        escaped = copy.deepcopy(version)
        escaped["source"]["relative_path"] = "../../outside.md"
        self.assertTrue(
            any("inside" in error for error in contracts.validate_document_version(escaped))
        )
        wrong_origin = copy.deepcopy(project)
        wrong_origin["provenance"]["origin"] = "model-derived"
        self.assertTrue(
            any("human-confirmed" in error for error in contracts.validate_project(wrong_origin))
        )

    def test_lineage_supports_split_merge_new_and_removed(self) -> None:
        def lineage(
            relation: str, from_claims: list[str], to_claims: list[str]
        ) -> dict[str, object]:
            lineage_parents: list[dict[str, str]] = []
            if relation != "new":
                lineage_parents.append(
                    {"role": "from-version", "artifact": "argument-ir", "sha256": "3" * 64}
                )
            if relation != "removed":
                lineage_parents.append(
                    {"role": "to-version", "artifact": "argument-ir", "sha256": "4" * 64}
                )
            return {
                **base(
                    "claim-lineage",
                    "L1",
                    "immutable",
                    "model-derived",
                    parents=lineage_parents,
                ),
                "lineage_id": "L1",
                "from_claims": from_claims,
                "to_claims": to_claims,
                "relation": relation,
                "proposed_by": "model",
                "proposal_sha256": None,
                "status": "proposed",
            }

        self.assertEqual(
            contracts.validate_claim_lineage(lineage("split", ["V1:C4"], ["V2:C7", "V2:C8"])),
            [],
        )
        self.assertEqual(
            contracts.validate_claim_lineage(lineage("merged", ["V1:C4", "V1:C5"], ["V2:C7"])),
            [],
        )
        self.assertEqual(
            contracts.validate_claim_lineage(lineage("new", [], ["V2:C11"])),
            [],
        )
        self.assertEqual(
            contracts.validate_claim_lineage(lineage("removed", ["V1:C8"], [])),
            [],
        )
        bad = lineage("split", ["V1:C4", "V1:C5"], ["V2:C7"])
        self.assertTrue(any("split" in error for error in contracts.validate_claim_lineage(bad)))

        confirmed = lineage("modified", ["V1:C4"], ["V2:C7"])
        confirmed["status"] = "human_confirmed"
        self.assertTrue(any("human-confirmed" in error for error in contracts.validate_claim_lineage(confirmed)))

        proposal = lineage("modified", ["V1:C4"], ["V2:C7"])
        confirmation = copy.deepcopy(proposal)
        confirmation["artifact_id"] = "L1-confirmation"
        confirmation["status"] = "human_confirmed"
        confirmation["provenance"]["origin"] = "human-confirmed"
        confirmation["proposal_sha256"] = contracts.sha256_bytes(encoded(proposal))
        confirmation["parents"] = list(proposal["parents"]) + [
            parent("proposal", "claim-lineage", proposal)
        ]
        self.assertEqual(contracts.validate_claim_lineage(confirmation), [])

    def test_accept_requires_revision_action_in_bundle(self) -> None:
        finding = {
            **base(
                "argument-finding",
                "F1",
                "immutable",
                "model-derived",
                parents=[
                    {"role": "target-ir", "artifact": "argument-ir", "sha256": "1" * 64},
                    {
                        "role": "lens-result",
                        "artifact": "argument-check-results",
                        "sha256": "2" * 64,
                    },
                ],
            ),
            "finding_id": "F1",
            "target_claim": "V1:C4",
            "lens": {"kind": "rule", "id": "social-science", "check_id": "descriptive.denominator"},
            "verdict": "fail",
            "reason": "三个案例都是正例，没有比较分母。",
            "evidence_refs": ["V1:C4", "V1:E3"],
            "status": "open",
        }
        adjudication = {
            **base(
                "finding-adjudication",
                "AD1",
                "immutable",
                "human-confirmed",
                parents=[parent("finding", "argument-finding", finding)],
            ),
            "adjudication_id": "AD1",
            "finding_id": "F1",
            "decision": "accept",
            "reason": "",
            "supersedes": None,
        }
        entries = [(finding, encoded(finding)), (adjudication, encoded(adjudication))]
        self.assertTrue(
            any("requires at least one revision-action" in error for error in contracts.validate_contract_bundle(entries))
        )
        action = {
            **base(
                "revision-action",
                "RA1",
                "immutable",
                "human-confirmed",
                parents=[parent("adjudication", "finding-adjudication", adjudication)],
            ),
            "action_id": "RA1",
            "adjudication_id": "AD1",
            "target_claim": "V1:C4",
            "action_type": "narrow_claim",
            "text": "把‘总会’收窄为当前三个案例中的观察。",
        }
        self.assertEqual(
            contracts.validate_contract_bundle(entries + [(action, encoded(action))]),
            [],
        )

    def test_all_validators_tolerate_malformed_shapes(self) -> None:
        for value in (None, 1, "x", [], {}, {"artifact": "unknown"}):
            with self.subTest(value=value):
                contracts.validate_artifact(value)
                for validator in contracts.VALIDATORS.values():
                    validator(value)


if __name__ == "__main__":
    unittest.main()
