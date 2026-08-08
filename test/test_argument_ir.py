from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import argument_ir  # noqa: E402
import critic_runner  # noqa: E402


SOURCE_TEXT = (
    "本文认为算法导致公共议题人格化。"
    "该判断依据两年访谈与平台记录（王，2024）。"
    "人格化是把公共争议归结为个人品格。"
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def valid_ir(source_bytes: bytes, source_name: str = "article.md") -> dict[str, object]:
    return {
        "schema_version": 1,
        "artifact": "argument-ir",
        "scope": "social-science",
        "source": {"name": source_name, "sha256": digest(source_bytes)},
        "claims": [
            {
                "id": "C1",
                "text": "算法导致公共议题人格化",
                "source_quote": "算法导致公共议题人格化",
                "position": "第1句",
                "types": ["causal"],
                "methods": ["causal-observational"],
                "role": "conclusion",
                "extraction": "explicit",
                "uncertainty": "",
            },
            {
                "id": "C2",
                "text": "人格化是把公共争议归结为个人品格",
                "source_quote": "人格化是把公共争议归结为个人品格",
                "position": "第3句",
                "types": ["conceptual"],
                "methods": ["conceptual-analysis"],
                "role": "premise",
                "extraction": "explicit",
                "uncertainty": "",
            },
        ],
        "evidence": [
            {
                "id": "E1",
                "text": "两年访谈与平台记录",
                "source_quote": "两年访谈与平台记录",
                "position": "第2句",
                "kind": "data",
            }
        ],
        "assumptions": [
            {
                "id": "A1",
                "text": "同期没有足以解释变化的共同冲击",
                "source_quote": "本文认为算法导致公共议题人格化",
                "position": "第1句（隐含）",
                "extraction": "inferred",
                "uncertainty": "因果措辞隐含排除共同冲击，但原文没有明说",
            }
        ],
        "citations": [
            {
                "id": "Z1",
                "text": "王，2024",
                "source_quote": "（王，2024）",
                "position": "第2句",
                "locator": "",
            }
        ],
        "relations": [
            {"id": "R1", "type": "supports", "from": "E1", "to": "C1"},
            {"id": "R2", "type": "assumes", "from": "A1", "to": "C1"},
            {"id": "R3", "type": "cites", "from": "Z1", "to": "E1"},
            {"id": "R4", "type": "qualifies", "from": "C2", "to": "C1"},
        ],
        "unverified": [],
    }


class ArgumentIRTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source_bytes = SOURCE_TEXT.encode("utf-8")
        cls.library_bytes = (REPO_ROOT / "ir" / "social-science-checks.json").read_bytes()
        cls.library = json.loads(cls.library_bytes.decode("utf-8"))

    def test_bundled_library_is_strict_and_substantive(self) -> None:
        self.assertEqual(argument_ir.validate_check_library(self.library), [])
        self.assertGreaterEqual(len(self.library["checks"]), 30)
        identifiers = {check["id"] for check in self.library["checks"]}
        self.assertTrue(
            {
                "causal.temporal-order",
                "causal.confounding",
                "causal.reverse-causality",
                "causal.selection-bias",
                "causal.mechanism",
                "causal.alternative-explanation",
            }.issubset(identifiers)
        )

    def test_ir_binds_exact_source_and_provenance(self) -> None:
        value = valid_ir(self.source_bytes)
        self.assertEqual(
            argument_ir.validate_argument_ir(
                value, source_bytes=self.source_bytes, source_name="article.md"
            ),
            [],
        )
        normalized = argument_ir.canonicalize_argument_ir(
            value,
            source_bytes=self.source_bytes,
            source_name="article.md",
        )
        self.assertRegex(normalized["claims"][0]["position"], r"^L1:C\d+-L1:C\d+$")
        self.assertNotEqual(
            normalized["claims"][0]["position"], value["claims"][0]["position"]
        )

        forged_quote = copy.deepcopy(value)
        forged_quote["claims"][0]["source_quote"] = "原稿里不存在的话"
        self.assertTrue(
            any(
                "exact substring" in error
                for error in argument_ir.validate_argument_ir(
                    forged_quote,
                    source_bytes=self.source_bytes,
                    source_name="article.md",
                )
            )
        )

        wrong_hash = copy.deepcopy(value)
        wrong_hash["source"]["sha256"] = "0" * 64
        self.assertTrue(
            any(
                "does not match" in error
                for error in argument_ir.validate_argument_ir(
                    wrong_hash,
                    source_bytes=self.source_bytes,
                    source_name="article.md",
                )
            )
        )

        duplicated_source = self.source_bytes + self.source_bytes
        ambiguous = valid_ir(duplicated_source)
        self.assertTrue(
            any(
                "ambiguous" in error
                for error in argument_ir.validate_argument_ir(
                    ambiguous,
                    source_bytes=duplicated_source,
                    source_name="article.md",
                )
            )
        )

    def test_ir_rejects_schema_drift_and_invalid_graphs(self) -> None:
        value = valid_ir(self.source_bytes)
        value["claims"][0]["confidence"] = 0.93
        self.assertTrue(
            any("exactly" in error for error in argument_ir.validate_argument_ir(value))
        )

        unknown_endpoint = valid_ir(self.source_bytes)
        unknown_endpoint["relations"][0]["from"] = "E9"
        self.assertTrue(
            any("unknown node" in error for error in argument_ir.validate_argument_ir(unknown_endpoint))
        )

        invalid_endpoint = valid_ir(self.source_bytes)
        invalid_endpoint["relations"][0] = {
            "id": "R1",
            "type": "assumes",
            "from": "E1",
            "to": "C1",
        }
        self.assertTrue(
            any("invalid endpoint" in error for error in argument_ir.validate_argument_ir(invalid_endpoint))
        )

        duplicate = valid_ir(self.source_bytes)
        duplicate["relations"][1] = {
            "id": "R2",
            "type": "supports",
            "from": "E1",
            "to": "C1",
        }
        self.assertTrue(
            any("duplicates" in error for error in argument_ir.validate_argument_ir(duplicate))
        )

        cycle = valid_ir(self.source_bytes)
        cycle["relations"].append(
            {"id": "R5", "type": "supports", "from": "C1", "to": "C2"}
        )
        self.assertTrue(
            any(
                "must be acyclic" in error
                for error in argument_ir.validate_argument_ir(cycle)
            )
        )

        empty = valid_ir(self.source_bytes)
        empty["claims"] = []
        empty["relations"] = []
        self.assertTrue(
            any("at least one" in error for error in argument_ir.validate_argument_ir(empty))
        )

        ambiguous_method = valid_ir(self.source_bytes)
        ambiguous_method["claims"][0]["methods"] = [
            "causal-observational",
            "unspecified",
        ]
        self.assertTrue(
            any(
                "use unspecified" in error
                for error in argument_ir.validate_argument_ir(ambiguous_method)
            )
        )

    def _plan(self, depth: str = "core") -> dict[str, object]:
        value = valid_ir(self.source_bytes)
        ir_bytes = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        return argument_ir.build_check_plan(
            value,
            self.library,
            ir_sha256=digest(ir_bytes),
            library_sha256=digest(self.library_bytes),
            depth=depth,
        )

    def test_check_selection_is_method_conditional_and_deterministic(self) -> None:
        first = self._plan("core")
        second = self._plan("core")
        self.assertEqual(first, second)
        self.assertEqual(argument_ir.validate_check_plan(first), [])
        causal = {
            task["check_id"]
            for task in first["tasks"]
            if task["claim_id"] == "C1"
        }
        self.assertTrue(
            {
                "causal.temporal-order",
                "causal.confounding",
                "causal.reverse-causality",
                "causal.selection-bias",
                "causal.mechanism",
                "causal.alternative-explanation",
            }.issubset(causal)
        )
        self.assertNotIn("quantitative.estimand", causal)
        self.assertNotIn("historical.comparability", causal)
        self.assertEqual(first["argument_ir"], valid_ir(self.source_bytes))
        self.assertTrue(all(set(task) == {"id", "claim_id", "check_id"} for task in first["tasks"]))
        self.assertEqual(
            len(first["checks"]), len({task["check_id"] for task in first["tasks"]})
        )
        serialized = json.dumps(first, ensure_ascii=False)
        self.assertEqual(serialized.count("人格化是把公共争议归结为个人品格"), 2)
        repeated_argument_cost = len(
            json.dumps(first["argument_ir"], ensure_ascii=False)
        ) * len(first["tasks"])
        self.assertLess(len(serialized), repeated_argument_cost)

        full = self._plan("full")
        full_causal = {
            task["check_id"]
            for task in full["tasks"]
            if task["claim_id"] == "C1"
        }
        self.assertIn("causal.identification", full_causal)
        self.assertIn("causal.spillover", full_causal)
        self.assertGreater(len(full["tasks"]), len(first["tasks"]))

        tampered = copy.deepcopy(first)
        tampered["tasks"][0]["check_id"] = "causal.not-in-plan"
        self.assertTrue(
            any(
                "not in checks" in error
                for error in argument_ir.validate_check_plan(tampered)
            )
        )

        method_tamper = copy.deepcopy(first)
        method_tamper["checks"][0]["question"] = "被替换但结构仍合法的问题"
        self.assertEqual(argument_ir.validate_check_plan(method_tamper), [])
        self.assertTrue(
            any(
                "not the deterministic output" in error
                for error in argument_ir.validate_check_plan_against_library(
                    method_tamper,
                    self.library,
                    library_sha256=digest(self.library_bytes),
                )
            )
        )

        argument_tamper = copy.deepcopy(first)
        argument_tamper["argument_ir"]["claims"][0]["text"] = "被篡改的主张"
        self.assertTrue(
            any(
                "argument_sha256" in error
                for error in argument_ir.validate_check_plan(argument_tamper)
            )
        )

    def test_execution_prompt_contains_exact_plan_hash_and_only_selected_tasks(self) -> None:
        plan = self._plan()
        plan_bytes = (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        plan_hash = digest(plan_bytes)
        prompt = argument_ir.render_check_prompt(plan, plan_sha256=plan_hash)
        self.assertIn(plan_hash, prompt)
        self.assertIn("causal.confounding", prompt)
        self.assertNotIn("historical.known-at-time", prompt)
        self.assertNotIn("由调用方填写", prompt)

    def _results(self, plan: dict[str, object], plan_hash: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "artifact": "argument-check-results",
            "source": {"plan_sha256": plan_hash},
            "status": "complete",
            "unverified": [],
            "results": [
                {
                    "task_id": task["id"],
                    "verdict": "pass",
                    "reason": "上下文未触发该失败条件。",
                    "evidence_refs": [task["claim_id"]],
                    "consequence": "",
                }
                for task in plan["tasks"]
            ],
        }

    def test_results_are_plan_bound_complete_and_provenance_limited(self) -> None:
        plan = self._plan()
        plan_bytes = (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        plan_hash = digest(plan_bytes)
        results = self._results(plan, plan_hash)
        self.assertEqual(
            argument_ir.validate_check_results(results, plan, plan_sha256=plan_hash), []
        )

        forged = copy.deepcopy(results)
        forged["results"][0]["evidence_refs"] = ["E99"]
        self.assertTrue(
            any(
                "outside the claim context" in error
                for error in argument_ir.validate_check_results(
                    forged, plan, plan_sha256=plan_hash
                )
            )
        )

        missing = copy.deepcopy(results)
        missing["results"].pop()
        self.assertTrue(
            any(
                "every task" in error
                for error in argument_ir.validate_check_results(
                    missing, plan, plan_sha256=plan_hash
                )
            )
        )

        wrong_hash = copy.deepcopy(results)
        wrong_hash["source"]["plan_sha256"] = "f" * 64
        self.assertTrue(
            any(
                "does not match" in error
                for error in argument_ir.validate_check_results(
                    wrong_hash, plan, plan_sha256=plan_hash
                )
            )
        )

        empty_evidence = copy.deepcopy(results)
        empty_evidence["results"][0]["evidence_refs"] = []
        self.assertTrue(
            any(
                "is required for pass" in error
                for error in argument_ir.validate_check_results(
                    empty_evidence, plan, plan_sha256=plan_hash
                )
            )
        )

        escaped = copy.deepcopy(results)
        escaped["results"][0]["verdict"] = "not_applicable"
        self.assertTrue(
            any(
                "verdict" in error
                for error in argument_ir.validate_check_results(
                    escaped, plan, plan_sha256=plan_hash
                )
            )
        )

        unrelated = copy.deepcopy(results)
        c2_result = next(
            item
            for item in unrelated["results"]
            if next(
                task for task in plan["tasks"] if task["id"] == item["task_id"]
            )["claim_id"]
            == "C2"
        )
        c2_result["evidence_refs"] = ["E1"]
        self.assertTrue(
            any(
                "outside the claim context" in error
                for error in argument_ir.validate_check_results(
                    unrelated, plan, plan_sha256=plan_hash
                )
            )
        )

        partial = copy.deepcopy(results)
        partial["status"] = "partial"
        partial["unverified"] = ["T3 之后因上下文窗口中断"]
        partial["results"] = partial["results"][:2]
        self.assertEqual(
            argument_ir.validate_check_results(
                partial, plan, plan_sha256=plan_hash
            ),
            [],
        )
        partial["results"].reverse()
        self.assertTrue(
            any(
                "task order" in error
                for error in argument_ir.validate_check_results(
                    partial, plan, plan_sha256=plan_hash
                )
            )
        )

    def test_findings_are_derived_only_from_fail_and_uncertain(self) -> None:
        plan = self._plan()
        plan_bytes = (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        plan_hash = digest(plan_bytes)
        results = self._results(plan, plan_hash)
        results["results"][0].update(
            verdict="fail",
            reason="材料与结论之间缺少推理连接。",
            consequence="该主张当前只能降格为待检验假说。",
        )
        results["results"][1].update(
            verdict="uncertain",
            reason="上下文缺少范围信息。",
            consequence="不能判断外推边界。",
        )
        results_bytes = (json.dumps(results, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        findings = argument_ir.build_argument_findings(
            plan,
            results,
            plan_sha256=plan_hash,
            results_sha256=digest(results_bytes),
        )
        self.assertEqual([item["id"] for item in findings["findings"]], ["F1", "F2"])
        self.assertEqual(
            {item["verdict"] for item in findings["findings"]}, {"fail", "uncertain"}
        )
        self.assertEqual(findings["findings"][0]["evidence_refs"], ["C1"])
        self.assertEqual(
            findings["findings"][0]["evidence"][0]["source_quote"],
            "算法导致公共议题人格化",
        )
        self.assertEqual(argument_ir.validate_argument_findings(findings), [])

    def test_validators_do_not_throw_on_malformed_json_shapes(self) -> None:
        malformed = [None, 0, "x", [], {}, {"tasks": [{}]}, {"claims": [{}]}]
        for value in malformed:
            with self.subTest(value=value):
                argument_ir.validate_argument_ir(value)
                argument_ir.validate_check_library(value)
                argument_ir.validate_check_plan(value)
                argument_ir.validate_argument_findings(value)
                argument_ir.validate_check_results(value, value, plan_sha256="0" * 64)

    def test_cli_end_to_end_and_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manuscript = root / "article.md"
            manuscript.write_bytes(self.source_bytes)
            ir_path = root / "argument-ir.json"
            ir_path.write_text(
                json.dumps(valid_ir(self.source_bytes), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            prompt_path = root / "extract.md"
            plan_path = root / "plan.json"
            review_prompt_path = root / "review.md"

            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(
                    critic_runner.main(
                        ["ir", "prepare", str(manuscript), "--output", str(prompt_path)]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(["ir", "validate", str(manuscript), str(ir_path)]),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "plan",
                            str(manuscript),
                            str(ir_path),
                            "--output",
                            str(plan_path),
                            "--prompt-output",
                            str(review_prompt_path),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "plan",
                            str(manuscript),
                            str(ir_path),
                            "--output",
                            str(plan_path),
                            "--prompt-output",
                            str(review_prompt_path),
                        ]
                    ),
                    0,
                )

            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan_hash = digest(plan_path.read_bytes())
            results = self._results(plan, plan_hash)
            results["results"][0].update(
                verdict="fail",
                reason="支持链不足。",
                consequence="结论需要降格。",
            )
            results_path = root / "results.json"
            results_path.write_text(
                json.dumps(results, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            findings_path = root / "findings.json"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(
                    critic_runner.main(
                        ["ir", "validate-results", str(plan_path), str(results_path)]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "findings",
                            str(plan_path),
                            str(results_path),
                            "--output",
                            str(findings_path),
                        ]
                    ),
                    0,
                )
            findings = json.loads(findings_path.read_text(encoding="utf-8"))
            self.assertEqual(len(findings["findings"]), 1)

            plan["checks"][0]["question"] = "被篡改的问题"
            plan_path.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            results["source"]["plan_sha256"] = digest(plan_path.read_bytes())
            results_path.write_text(
                json.dumps(results, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(
                    critic_runner.main(
                        ["ir", "validate-results", str(plan_path), str(results_path)]
                    ),
                    critic_runner.EXIT_INVALID_WORKFLOW,
                )

    def test_cli_refuses_colliding_plan_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manuscript = root / "article.md"
            manuscript.write_bytes(self.source_bytes)
            ir_path = root / "argument-ir.json"
            ir_path.write_text(
                json.dumps(valid_ir(self.source_bytes), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            collision = root / "same-output"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "plan",
                            str(manuscript),
                            str(ir_path),
                            "--output",
                            str(collision),
                            "--prompt-output",
                            str(collision),
                        ]
                    ),
                    2,
                )
            self.assertFalse(collision.exists())


if __name__ == "__main__":
    unittest.main()
