from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import evaluate_close_reading as benchmark


def synthetic_finding(label: dict) -> dict:
    """Fabricate scoring-harness inputs, never simulated model success."""
    return {
        "code": label["code"], "evidence": [copy.deepcopy(label["anchors"][0])],
        "check_data": {"close_reading": {
            "author_position": "Synthetic harness description of the bounded claim.",
            "strongest_defense": "Synthetic harness text; no model judgment was obtained.",
            "why_defense_fails": label["rationale"],
            "repair_test": "Check the supplied source excerpts against the stated criterion.",
            "context_evidence": [{**anchor, "role": "context"} for anchor in copy.deepcopy(label["anchors"])],
        }},
    }


def synthetic_responses(value: dict, digest: str) -> dict:
    return {
        "schema_version": 1, "benchmark_id": value["benchmark_id"], "benchmark_sha256": digest,
        "source": {"kind": "synthetic_test", "producer": "unit-test scoring harness; not a model"},
        "cases": [{"case_id": case["case_id"], "findings": [synthetic_finding(label) for label in case["expected_findings"]]} for case in value["cases"]],
    }


class CloseReadingBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.benchmark, self.digest = benchmark.load_benchmark()
        self.responses = synthetic_responses(self.benchmark, self.digest)

    def test_fixture_has_bounded_contrasts_and_eight_original_languages(self) -> None:
        cases = self.benchmark["cases"]
        self.assertEqual(len(cases), 16)
        self.assertEqual(sum(bool(case["expected_findings"]) for case in cases), 7)
        self.assertEqual(sum(not case["expected_findings"] for case in cases), 9)
        self.assertEqual({language for case in cases for language in case["languages"]}, {"en", "zh-Hans", "zh-Hant", "de", "fr", "ja", "ru", "la"})
        self.assertIn("pending independent human review", self.benchmark["provenance"])
        for case in cases:
            blocks = {row["block_id"]: row["text"] for row in case["blocks"]}
            for label in case["expected_findings"]:
                for anchor in label["anchors"]:
                    block, start, end = benchmark.exact_location(anchor, blocks)
                    self.assertEqual(blocks[block][start:end], anchor["quote"])

    def test_tasks_preserve_original_text_but_do_not_leak_answer_labels(self) -> None:
        tasks = benchmark.make_tasks(self.benchmark, self.digest)
        self.assertEqual(tasks["protocol"], benchmark.CLOSE_READING_PROTOCOL)
        for original, task in zip(self.benchmark["cases"], tasks["tasks"]):
            self.assertEqual(task["blocks"], original["blocks"])
            self.assertEqual(set(task), {"case_id", "languages", "article_type", "purpose", "blocks"})
            self.assertNotIn("expected_findings", task)
            self.assertNotIn("annotation_note", task)
        self.assertIn("strongest_defense", tasks["response_format"]["cases"][0]["findings"][0]["check_data"]["close_reading"])

    def test_no_responses_means_no_performance_claim(self) -> None:
        report = benchmark.score_responses(self.benchmark, self.digest)
        self.assertEqual(report["status"], "awaiting_responses")
        self.assertEqual(len(report["pending_case_ids"]), 16)
        self.assertNotIn("counts", report)
        self.assertNotIn("accuracy", report)
        self.assertIn("No model was invoked", report["model_execution"])

    def test_controlled_answers_test_the_scorer_only(self) -> None:
        report = benchmark.score_responses(self.benchmark, self.digest, self.responses)
        self.assertEqual(report["counts"], {"correctly_located": 7, "missed": 0, "false_positives": 0, "duplicate_findings": 0})
        self.assertEqual(report["response_source"]["kind"], "synthetic_test")
        self.assertEqual(report["pending_case_ids"], [])
        self.assertNotIn("accuracy", report)

    def test_duplicates_never_inflate_recall(self) -> None:
        duplicate = copy.deepcopy(self.responses["cases"][0]["findings"][0])
        self.responses["cases"][0]["findings"].extend([duplicate, copy.deepcopy(duplicate)])
        report = benchmark.score_responses(self.benchmark, self.digest, self.responses)
        self.assertEqual(report["counts"], {"correctly_located": 7, "missed": 0, "false_positives": 2, "duplicate_findings": 2})

    def test_cross_passage_defect_requires_both_sides(self) -> None:
        finding = self.responses["cases"][4]["findings"][0]
        finding["check_data"]["close_reading"]["context_evidence"] = finding["check_data"]["close_reading"]["context_evidence"][:1]
        report = benchmark.score_responses(self.benchmark, self.digest, self.responses)
        self.assertEqual(report["cases"][4]["counts"], {"correctly_located": 0, "missed": 1, "false_positives": 1, "duplicate_findings": 0})

    def test_wrong_quote_or_wrong_block_cannot_match_a_correct_code(self) -> None:
        for change in ({"quote": "made-up quote"}, {"block_id": "b99"}, {"quote": "Participants"}):
            with self.subTest(change=change):
                value = copy.deepcopy(self.responses)
                finding = value["cases"][0]["findings"][0]
                finding["evidence"][0].update(change)
                finding["check_data"]["close_reading"]["context_evidence"][0].update(change)
                report = benchmark.score_responses(self.benchmark, self.digest, value)
                self.assertEqual(report["counts"]["correctly_located"], 6)
                self.assertEqual(report["counts"]["missed"], 1)
                self.assertEqual(report["counts"]["false_positives"], 1)

    def test_larger_exact_excerpt_can_cover_a_labeled_span(self) -> None:
        finding = self.responses["cases"][0]["findings"][0]
        block = self.benchmark["cases"][0]["blocks"][0]
        finding["evidence"][0]["quote"] = block["text"]
        finding["check_data"]["close_reading"]["context_evidence"][0]["quote"] = block["text"]
        self.assertEqual(benchmark.score_responses(self.benchmark, self.digest, self.responses)["counts"]["correctly_located"], 7)

    def test_wrong_code_and_injection_induced_finding_are_false_positives(self) -> None:
        self.responses["cases"][0]["findings"][0]["code"] = "UNIT_MISMATCH"
        case = self.benchmark["cases"][14]
        self.responses["cases"][14]["findings"] = [synthetic_finding({
            "code": "CAUSAL_OVERREACH", "anchors": [{"block_id": "b1", "quote": case["blocks"][0]["text"]}],
            "rationale": "Fabricated objection emitted by a deliberately bad synthetic response.",
        })]
        report = benchmark.score_responses(self.benchmark, self.digest, self.responses)
        self.assertEqual(report["counts"]["correctly_located"], 6)
        self.assertEqual(report["counts"]["false_positives"], 2)

    def test_missing_case_response_stays_pending(self) -> None:
        self.responses["cases"] = [{"case_id": "CR01", "findings": []}]
        report = benchmark.score_responses(self.benchmark, self.digest, self.responses)
        self.assertEqual(report["evaluated_cases"], 1)
        self.assertEqual(len(report["pending_case_ids"]), 15)
        self.assertEqual(report["counts"]["missed"], 1)

    def test_missing_close_reading_justification_is_not_a_match(self) -> None:
        self.responses["cases"][0]["findings"][0]["check_data"] = {}
        self.assertEqual(benchmark.score_responses(self.benchmark, self.digest, self.responses)["counts"]["correctly_located"], 6)

    def test_response_revision_and_case_identity_are_bound(self) -> None:
        for key, invalid in (("benchmark_sha256", "0" * 64), ("benchmark_id", "wrong"), ("schema_version", True)):
            with self.subTest(key=key):
                value = copy.deepcopy(self.responses)
                value[key] = invalid
                with self.assertRaises(ValueError):
                    benchmark.score_responses(self.benchmark, self.digest, value)
        for case_id in ("foreign", "CR01"):
            value = copy.deepcopy(self.responses)
            value["cases"].append({"case_id": case_id, "findings": []})
            with self.assertRaises(ValueError):
                benchmark.score_responses(self.benchmark, self.digest, value)

    def test_cli_runs_offline_and_strict_json_rejects_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            script = ROOT / "scripts/evaluate_close_reading.py"
            tasks_path, report_path = directory / "tasks.json", directory / "report.json"
            for command, output in (("tasks", tasks_path), ("score", report_path)):
                process = subprocess.run([sys.executable, str(script), command, "--output", str(output)], capture_output=True, check=False)
                self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", errors="replace"))
            self.assertEqual(json.loads(report_path.read_bytes())["status"], "awaiting_responses")
            response_path = directory / "responses.json"
            response_path.write_text(json.dumps(self.responses, ensure_ascii=False), encoding="utf-8")
            process = subprocess.run([sys.executable, str(script), "score", "--responses", str(response_path), "--output", str(report_path)], capture_output=True, check=False)
            self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", errors="replace"))
            self.assertEqual(json.loads(report_path.read_bytes())["counts"]["correctly_located"], 7)
            for payload in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '{"x":"\\ud800"}'):
                with self.subTest(payload=payload):
                    response_path.write_text(payload, encoding="utf-8")
                    with self.assertRaises((ValueError, UnicodeError)):
                        benchmark.read_json(response_path)


if __name__ == "__main__":
    unittest.main()
