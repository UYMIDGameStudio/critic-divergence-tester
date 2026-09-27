"""Preserve a single AI pilot's original submissions and score their structure.

This script invokes no model and makes no semantic or accuracy determination.
Run from the repository root. Existing original files are never rewritten.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
INPUT = ROOT / "dist/close-reading-pilot-20260927"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate key: {key}")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique)


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def preserve(source: Path, relative: str):
    raw = source.read_bytes()
    destination = OUT / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.read_bytes() != raw:
        raise ValueError(f"Refusing to replace preserved evidence: {relative}")
    if not destination.exists():
        destination.write_bytes(raw)
    assert destination.read_bytes() == raw
    return {"path": relative, "source_path": source.relative_to(ROOT).as_posix(), "sha256": digest(raw), "bytes": len(raw)}


def main():
    assignment = read_json(INPUT / "manifest.json")
    benchmark_path = ROOT / "test/fixtures/close-reading-benchmark.json"
    benchmark_before = benchmark_path.read_bytes()
    benchmark = read_json(benchmark_path)
    assert digest(benchmark_before) == assignment["benchmark_sha256"]
    assert benchmark["benchmark_id"] == assignment["benchmark_id"]
    benchmark_cases = {row["case_id"]: row for row in benchmark["cases"]}
    original_hashes = {name: digest((INPUT / name).read_bytes()) for name in ("manifest.json", "tasks-a.json", "tasks-b.json", "responses-a.json", "responses-b.json")}
    combined_cases, parts, seen = [], {}, set()
    tasks_by_part = {}
    preserved = [preserve(INPUT / "manifest.json", "assignment-manifest.json"), preserve(benchmark_path, "benchmark.json")]
    for part in ("a", "b"):
        task_name, response_name = f"tasks-{part}.json", f"responses-{part}.json"
        task, response = read_json(INPUT / task_name), read_json(INPUT / response_name)
        tasks_by_part[part] = task
        expected_ids = assignment["parts"][part]["case_ids"]
        task_ids = [row["case_id"] for row in task["tasks"]]
        response_ids = [row["case_id"] for row in response["cases"]]
        assert task_ids == expected_ids == response_ids, (part, task_ids, response_ids)
        assert len(set(task_ids)) == len(task_ids) and not seen.intersection(task_ids)
        seen.update(task_ids)
        assert assignment["parts"][part]["task_file"] == task_name
        assert original_hashes[task_name] == assignment["parts"][part]["task_sha256"]
        protocol_bytes = json.dumps(task["protocol"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        assert digest(protocol_bytes) == assignment["protocol_sha256"]
        for document in (task, response):
            assert document["benchmark_id"] == assignment["benchmark_id"]
            assert document["benchmark_sha256"] == assignment["benchmark_sha256"]
        assert response["source"]["kind"] == "externally_provided"
        assert response["source"]["producer"] == f"Codex independent pilot reviewer {part.upper()}, single run, model settings inherited"
        assert task["issue_codes"] == benchmark["issue_codes"]
        for row in task["tasks"]:
            expected = benchmark_cases[row["case_id"]]
            assert row == {key: expected[key] for key in ("case_id", "languages", "article_type", "purpose", "blocks")}
        combined_cases.extend(response["cases"])
        preserved.extend([preserve(INPUT / task_name, task_name), preserve(INPUT / response_name, response_name)])
        parts[part] = {"case_ids": task_ids, "task_sha256": original_hashes[task_name], "response_sha256": original_hashes[response_name], "source": response["source"], "findings": sum(len(row["findings"]) for row in response["cases"])}
    assert seen == set(benchmark_cases)
    assert tasks_by_part["a"]["protocol"] == tasks_by_part["b"]["protocol"]
    combined = {
        "schema_version": 1,
        "benchmark_id": assignment["benchmark_id"],
        "benchmark_sha256": assignment["benchmark_sha256"],
        "source": {"kind": "externally_provided", "producer": "Unedited concatenation of Codex independent pilot reviewers A and B, one run per assigned partition, model settings inherited; original attribution and raw responses retained in manifest.json"},
        "cases": combined_cases,
    }
    write_json(OUT / "responses-combined.json", combined)
    assert read_json(OUT / "responses-combined.json")["cases"] == combined_cases
    scorer = ROOT / "scripts/evaluate_close_reading.py"
    validator = ROOT / "document_review_quality.py"
    preserved.extend([preserve(scorer, "scoring-source/scripts/evaluate_close_reading.py"), preserve(validator, "scoring-source/document_review_quality.py")])
    scorer_hash, validator_hash = digest(scorer.read_bytes()), digest(validator.read_bytes())
    command = [sys.executable, str(scorer), "--benchmark", str(OUT / "benchmark.json"), "score", "--responses", str(OUT / "responses-combined.json"), "--output", str(OUT / "result.json")]
    run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    public_command = ["python", *[str(Path(arg).relative_to(ROOT)).replace("\\", "/")
                                  if Path(arg).is_absolute() and Path(arg).is_relative_to(ROOT) else arg
                                  for arg in command[1:]]]
    write_json(OUT / "scoring-invocation.json", {"argv": public_command, "cwd": ".", "path_representation": "Interpreter and repository paths normalized for publication; original task and response bytes unchanged.", "exit_code": run.returncode, "stdout": run.stdout, "stderr": run.stderr})
    if run.returncode:
        raise RuntimeError(run.stderr)
    assert digest(scorer.read_bytes()) == scorer_hash
    assert digest(validator.read_bytes()) == validator_hash
    assert benchmark_path.read_bytes() == benchmark_before
    assert original_hashes == {name: digest((INPUT / name).read_bytes()) for name in original_hashes}
    result = read_json(OUT / "result.json")
    assert result["evaluated_cases"] == len(seen) and result["pending_case_ids"] == []
    current_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    relevant_status = subprocess.check_output(["git", "status", "--short", "--", "scripts/evaluate_close_reading.py", "document_review_quality.py", "test/fixtures/close-reading-benchmark.json"], cwd=ROOT, text=True)
    manifest = {
        "schema_version": 1,
        "artifact": "close-reading-pilot-evidence-manifest",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_commit": assignment["source_commit"],
        "scoring_base_commit": current_commit,
        "scoring_source_status": relevant_status.strip().splitlines(),
        "scorer_sha256": scorer_hash,
        "validator_sha256": validator_hash,
        "benchmark_id": assignment["benchmark_id"],
        "benchmark_sha256": assignment["benchmark_sha256"],
        "protocol_sha256": assignment["protocol_sha256"],
        "protocol_hash_format": "UTF-8 JSON, ensure_ascii=False, sort_keys=True, separators=(',', ':')",
        "parts": parts,
        "verification": {"exact_assigned_ids_and_order": True, "task_file_digests_match_assignment": True, "task_payloads_match_benchmark_without_labels": True, "no_missing_or_duplicate_cases": True, "all_16_cases_supplied": True, "combined_case_objects_equal_originals": True, "original_task_response_and_benchmark_bytes_unchanged": True, "scorer_and_validator_unchanged_during_run": True},
        "preserved_files": preserved,
        "generated_files": [{"path": name, "sha256": digest((OUT / name).read_bytes()), "bytes": (OUT / name).stat().st_size} for name in ("responses-combined.json", "result.json", "scoring-invocation.json", "consolidate.py")],
        "structural_result": {"evaluated_cases": result["evaluated_cases"], "empty_finding_cases": sum(not row["findings"] for row in combined_cases), "submitted_findings": sum(len(row["findings"]) for row in combined_cases), "counts": result["counts"]},
        "semantic_review": {"status": "AI_only_post_run_review", "report": "../../docs/close-reading-pilot-20260927.md", "human_validation": False},
        "limits": ["Source kind externally_provided means separately supplied to a local scorer; the responders were Codex AI agents, not external human participants.", "Each partition has one original response. Original findings are unedited; combined serialization is a derivative with preserved case objects.", "The scorer matches issue codes and exact quoted spans. It does not judge semantic reasoning, label validity, or general model accuracy.", "The 16 short synthetic cases do not constitute the full academic-v2 framework, naturalistic academic review, a baseline comparison, or independent human validation.", "The post-run report author produced reviewer A's response before viewing labels. Later consolidation and qualitative review are not independent of that prior authorship."],
    }
    report = ROOT / "docs/close-reading-pilot-20260927.md"
    if report.exists():
        manifest["semantic_review"]["report_sha256"] = digest(report.read_bytes())
    write_json(OUT / "manifest.json", manifest)
    print(json.dumps({"evidence": str(OUT), "scorer_sha256": scorer_hash, "structural_result": manifest["structural_result"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
