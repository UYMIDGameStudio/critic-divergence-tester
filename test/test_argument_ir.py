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
            {"id": "R5", "type": "supports", "from": "E1", "to": "C2"},
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

    def test_placeholder_manuscript_path_has_copyable_windows_guidance(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = critic_runner.main(
                ["ir", "prepare", "path/to/draft.md"]
            )
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("README 示例占位路径", stderr.getvalue())
        self.assertIn("py -3 critic_runner.py ir prepare", stderr.getvalue())
        self.assertIn("Downloads", stderr.getvalue())

    def test_missing_manuscript_path_reports_resolved_path_and_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "does-not-exist.md"
            stderr = StringIO()
            with redirect_stdout(StringIO()), redirect_stderr(stderr):
                exit_code = critic_runner.main(["ir", "prepare", str(missing)])
            self.assertEqual(exit_code, 2)
            self.assertIn("找不到稿件文件", stderr.getvalue())
            self.assertIn(str(missing.resolve()), stderr.getvalue())
            self.assertIn("当前工作目录", stderr.getvalue())
            self.assertIn("双引号", stderr.getvalue())

    def test_quoted_manuscript_path_with_spaces_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "folder with spaces"
            root.mkdir()
            manuscript = root / "article with spaces.md"
            manuscript.write_text("一个可审查的主张。", encoding="utf-8")
            output = root / "prompt.md"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                exit_code = critic_runner.main(
                    [
                        "ir",
                        "prepare",
                        f'"{manuscript}"',
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertTrue(output.is_file())

    def test_empty_manuscript_explains_how_to_fix_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manuscript = Path(temporary) / ".txt"
            manuscript.write_text(" \n\t", encoding="utf-8")
            stderr = StringIO()
            with redirect_stdout(StringIO()), redirect_stderr(stderr):
                exit_code = critic_runner.main(
                    ["ir", "prepare", str(manuscript)]
                )
            self.assertEqual(exit_code, 2)
            self.assertIn("稿件文件是空的", stderr.getvalue())
            self.assertIn(str(manuscript.resolve()), stderr.getvalue())
            self.assertIn("记事本", stderr.getvalue())
            self.assertIn("UTF-8", stderr.getvalue())

    def test_non_utf8_manuscript_explains_encoding_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manuscript = Path(temporary) / "legacy.txt"
            manuscript.write_bytes(b"\x81\x81\x81")
            stderr = StringIO()
            with redirect_stdout(StringIO()), redirect_stderr(stderr):
                exit_code = critic_runner.main(
                    ["ir", "prepare", str(manuscript)]
                )
            self.assertEqual(exit_code, 2)
            self.assertIn("不是 UTF-8 编码", stderr.getvalue())
            self.assertIn("另存为", stderr.getvalue())

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

    def test_empirical_causal_checks_do_not_apply_to_conceptual_causal_claims(
        self,
    ) -> None:
        value = valid_ir(self.source_bytes)
        value["claims"][0]["methods"] = ["conceptual-analysis"]
        ir_bytes = (
            json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        plan = argument_ir.build_check_plan(
            value,
            self.library,
            ir_sha256=digest(ir_bytes),
            library_sha256=digest(self.library_bytes),
            depth="core",
        )
        selected = {
            task["check_id"]
            for task in plan["tasks"]
            if task["claim_id"] == "C1"
        }
        self.assertTrue(
            {"causal.mechanism", "causal.alternative-explanation"}.issubset(
                selected
            )
        )
        self.assertTrue(
            {
                "causal.temporal-order",
                "causal.confounding",
                "causal.reverse-causality",
                "causal.selection-bias",
            }.isdisjoint(selected)
        )
        self.assertEqual(argument_ir.validate_check_plan(plan), [])

    def test_review_scope_is_orthogonal_to_depth_and_defaults_can_target_thesis_chain(
        self,
    ) -> None:
        value = valid_ir(self.source_bytes)
        value["relations"] = [
            relation for relation in value["relations"] if relation["id"] != "R4"
        ]
        value["relations"][-1]["id"] = "R4"
        ir_bytes = (
            json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        common = {
            "ir_sha256": digest(ir_bytes),
            "library_sha256": digest(self.library_bytes),
            "depth": "core",
        }
        thesis = argument_ir.build_check_plan(
            value, self.library, review_scope="thesis-chain", **common
        )
        audit = argument_ir.build_check_plan(
            value, self.library, review_scope="all", **common
        )
        single = argument_ir.build_check_plan(
            value,
            self.library,
            review_scope="claim",
            claim_ids=["C2"],
            **common,
        )
        self.assertEqual(thesis["review_scope"]["selected_claim_ids"], ["C1"])
        self.assertEqual(single["review_scope"]["selected_claim_ids"], ["C2"])
        self.assertTrue(all(task["claim_id"] == "C1" for task in thesis["tasks"]))
        self.assertTrue(all(task["claim_id"] == "C2" for task in single["tasks"]))
        self.assertGreater(len(audit["tasks"]), len(thesis["tasks"]))
        self.assertEqual(argument_ir.validate_check_plan(thesis), [])
        with self.assertRaisesRegex(argument_ir.ArgumentIRError, "unknown Claim"):
            argument_ir.build_check_plan(
                value,
                self.library,
                review_scope="claim",
                claim_ids=["C99"],
                **common,
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

    def test_legacy_v1_library_plan_and_results_remain_explicitly_verifiable(self) -> None:
        legacy_library = copy.deepcopy(self.library)
        legacy_library["schema_version"] = 1
        for check in legacy_library["checks"]:
            check.pop("evidence_policy")
        legacy_bytes = (
            json.dumps(legacy_library, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        value = valid_ir(self.source_bytes)
        ir_bytes = (
            json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        plan = argument_ir.build_check_plan(
            value,
            legacy_library,
            ir_sha256=digest(ir_bytes),
            library_sha256=digest(legacy_bytes),
            depth="core",
        )
        self.assertEqual(plan["schema_version"], 1)
        self.assertNotIn("review_scope", plan)
        plan_bytes = (
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        results = {
            "schema_version": 1,
            "artifact": "argument-check-results",
            "source": {"plan_sha256": digest(plan_bytes)},
            "status": "complete",
            "unverified": [],
            "results": [
                {
                    "task_id": task["id"],
                    "verdict": "pass",
                    "reason": "Legacy result retained for byte-compatible verification.",
                    "evidence_refs": [task["claim_id"]],
                    "consequence": "",
                }
                for task in plan["tasks"]
            ],
        }
        self.assertEqual(
            argument_ir.validate_check_results(
                results, plan, plan_sha256=digest(plan_bytes)
            ),
            [],
        )

    def _results(self, plan: dict[str, object], plan_hash: str) -> dict[str, object]:
        check_by_id = {check["id"]: check for check in plan["checks"]}
        argument = plan["argument_ir"]
        node_kinds = {
            item["id"]: kind
            for kind, field in (
                ("claim", "claims"),
                ("evidence", "evidence"),
                ("assumption", "assumptions"),
                ("citation", "citations"),
            )
            for item in argument[field]
        }
        items = []
        for task in plan["tasks"]:
            claim_id = task["claim_id"]
            policy = check_by_id[task["check_id"]]["evidence_policy"]
            eligible_paths = argument_ir._eligible_pass_support_paths(
                argument, claim_id
            )
            support_refs = []
            if policy == "upstream-required":
                support_refs = [
                    next(iter(eligible_paths))
                ]
            elif policy == "citation-required":
                support_refs = [
                    next(
                        ref
                        for ref in eligible_paths
                        if node_kinds.get(ref) == "citation"
                    )
                ]
            item = {
                    "task_id": task["id"],
                    "execution_status": "evaluated",
                    "verdict": "pass",
                    "reason": "上下文未触发该失败条件。",
                    "basis_refs": [claim_id, *support_refs],
                    "support_refs": support_refs,
                    "consequence": "",
                }
            if plan["schema_version"] == 3:
                item["support_paths"] = [
                    {
                        "support_ref": ref,
                        "relation_ids": eligible_paths[ref],
                    }
                    for ref in support_refs
                ]
            items.append(item)
        return {
            "schema_version": plan["schema_version"],
            "artifact": "argument-check-results",
            "source": {"plan_sha256": plan_hash},
            "status": "complete",
            "unverified": [],
            "results": items,
        }

    def test_v2_plan_and_results_remain_verifiable_without_support_paths(self) -> None:
        library = copy.deepcopy(self.library)
        library["schema_version"] = 2
        library_bytes = (
            json.dumps(library, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        value = valid_ir(self.source_bytes)
        ir_bytes = (
            json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        plan = argument_ir.build_check_plan(
            value,
            library,
            ir_sha256=digest(ir_bytes),
            library_sha256=digest(library_bytes),
            depth="core",
        )
        plan_bytes = (
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        results = self._results(plan, digest(plan_bytes))
        self.assertEqual(plan["schema_version"], 2)
        self.assertNotIn("support_paths", results["results"][0])
        self.assertEqual(
            argument_ir.validate_check_results(
                results, plan, plan_sha256=digest(plan_bytes)
            ),
            [],
        )

    def test_results_are_plan_bound_complete_and_provenance_limited(self) -> None:
        plan = self._plan()
        plan_bytes = (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        plan_hash = digest(plan_bytes)
        results = self._results(plan, plan_hash)
        self.assertEqual(
            argument_ir.validate_check_results(results, plan, plan_sha256=plan_hash), []
        )

        forged = copy.deepcopy(results)
        forged["results"][0]["basis_refs"] = ["E99"]
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
        empty_evidence["results"][0]["basis_refs"] = []
        self.assertTrue(
            any(
                "must not be empty" in error
                for error in argument_ir.validate_check_results(
                    empty_evidence, plan, plan_sha256=plan_hash
                )
            )
        )

        self_supported_pass = copy.deepcopy(results)
        first = self_supported_pass["results"][0]
        first["basis_refs"] = ["C1"]
        first["support_refs"] = ["C1"]
        first["support_paths"] = []
        self.assertTrue(
            any(
                "independent of the target Claim" in error
                for error in argument_ir.validate_check_results(
                    self_supported_pass, plan, plan_sha256=plan_hash
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
        c2_result["basis_refs"] = ["A1"]
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

    def test_v3_pass_support_path_rejects_a_contradicting_relation(self) -> None:
        value = valid_ir(self.source_bytes)
        value["relations"][0]["type"] = "contradicts"
        ir_bytes = (
            json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        plan = argument_ir.build_check_plan(
            value,
            self.library,
            ir_sha256=digest(ir_bytes),
            library_sha256=digest(self.library_bytes),
            depth="core",
        )
        plan_bytes = (
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        plan_hash = digest(plan_bytes)
        results = self._results(plan, plan_hash)
        target = next(
            result
            for result in results["results"]
            if next(
                task for task in plan["tasks"] if task["id"] == result["task_id"]
            )["claim_id"]
            == "C1"
            and result["support_refs"]
        )
        target["support_refs"] = ["E1"]
        target["support_paths"] = [
            {"support_ref": "E1", "relation_ids": ["R1"]}
        ]
        self.assertTrue(
            any(
                "uses contradicts" in error
                for error in argument_ir.validate_check_results(
                    results, plan, plan_sha256=plan_hash
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
            support_refs=[],
            support_paths=[],
            consequence="该主张当前只能降格为待检验假说。",
        )
        results["results"][1].update(
            verdict="uncertain",
            reason="上下文缺少范围信息。",
            support_refs=[],
            support_paths=[],
            consequence="不能判断外推边界。",
        )
        results["results"][2].update(
            execution_status="routing_mismatch",
            verdict=None,
            reason="The Claim method classification routed an empirical check incorrectly.",
            basis_refs=["C1"],
            support_refs=[],
            support_paths=[],
            consequence="",
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
        self.assertIn("C1", findings["findings"][0]["evidence_refs"])
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
                support_refs=[],
                support_paths=[],
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
