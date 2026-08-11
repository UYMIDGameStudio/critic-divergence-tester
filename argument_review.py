"""Claim-centered Rule Review workflows for Argument Workbench.

This module is the Phase 2 application layer.  It reuses the existing
Argument IR check-plan and result validators, preserves every model response,
and normalizes actionable outcomes into version-qualified Finding envelopes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argument_contracts import (
    FINDING_VERDICTS,
    sha256_bytes,
    validate_artifact,
    validate_contract_bundle,
)
from argument_ir import (
    CHECK_DEPTHS,
    REVIEW_SCOPES,
    ArgumentIRError,
    build_check_plan,
    render_check_prompt,
    validate_argument_ir,
    validate_check_library,
    validate_check_plan_against_library,
    validate_check_results,
)
from argument_workbench import (
    WorkspacePaths,
    WorkbenchError,
    _atomic_write,
    _parent,
    _provenance,
    _read_json,
    _write_new,
    correction_entries,
    json_bytes,
    list_attempts,
    parse_json_strict,
    utc_now,
    verify_workspace,
    workspace_paths,
)


REVIEW_ID_PATTERN = re.compile(r"RV([1-9][0-9]*)\Z")
ATTEMPT_ID_PATTERN = re.compile(r"attempt-([0-9]{4})\Z")
FINDING_FILE_PATTERN = re.compile(r"F([0-9]{4})\.json\Z")
COLLECTION_METHODS = {"file", "terminal-paste"}


@dataclass(frozen=True)
class ReviewPaths:
    workspace: WorkspacePaths
    review_id: str

    @property
    def reviews_dir(self) -> Path:
        return self.workspace.version_dir / "reviews"

    @property
    def root(self) -> Path:
        return self.reviews_dir / self.review_id

    @property
    def record(self) -> Path:
        return self.root / "review-run.json"

    @property
    def library(self) -> Path:
        return self.root / "check-library.json"

    @property
    def reviewed_record(self) -> Path:
        return self.root / "reviewed-ir-record.json"

    @property
    def target_ir(self) -> Path:
        return self.root / "target-argument-ir.json"

    @property
    def plan(self) -> Path:
        return self.root / "check-plan.json"

    @property
    def prompt(self) -> Path:
        return self.root / "review-prompt.md"

    @property
    def results_dir(self) -> Path:
        return self.root / "results"

    @property
    def derived_dir(self) -> Path:
        return self.root / "derived"

    def attempt_dir(self, attempt_id: str) -> Path:
        return self.results_dir / attempt_id

    def derived_attempt_dir(self, attempt_id: str) -> Path:
        return self.derived_dir / attempt_id


def _review_paths(project_dir: Path | str, review_id: str) -> ReviewPaths:
    if REVIEW_ID_PATTERN.fullmatch(review_id) is None:
        raise WorkbenchError("review ID must be RV1..RVn")
    return ReviewPaths(workspace_paths(project_dir), review_id)


def list_rule_reviews(project_dir: Path | str) -> list[ReviewPaths]:
    workspace = workspace_paths(project_dir)
    reviews_dir = workspace.version_dir / "reviews"
    if not reviews_dir.exists():
        return []
    if reviews_dir.is_symlink() or not reviews_dir.is_dir():
        raise WorkbenchError("reviews must be a regular non-symlink directory")
    reviews: list[ReviewPaths] = []
    for path in reviews_dir.iterdir():
        if path.is_symlink():
            raise WorkbenchError(f"review directory must not be a symlink: {path}")
        if path.is_dir() and REVIEW_ID_PATTERN.fullmatch(path.name):
            reviews.append(ReviewPaths(workspace, path.name))
    return sorted(
        reviews,
        key=lambda item: int(REVIEW_ID_PATTERN.fullmatch(item.review_id).group(1)),
    )


def selected_rule_review(
    project_dir: Path | str, review_id: str | None = None
) -> ReviewPaths:
    if review_id is not None:
        paths = _review_paths(project_dir, review_id.upper())
        if not paths.root.is_dir() or paths.root.is_symlink():
            raise WorkbenchError(f"unknown Rule Review: {paths.review_id}")
        return paths
    reviews = list_rule_reviews(project_dir)
    if not reviews:
        raise WorkbenchError("project has no Rule Review; run `ir review prepare` first")
    return reviews[-1]


def _next_review_id(project_dir: Path | str) -> str:
    reviews = list_rule_reviews(project_dir)
    number = max(
        [int(REVIEW_ID_PATTERN.fullmatch(item.review_id).group(1)) for item in reviews]
        or [0]
    ) + 1
    return f"RV{number}"


def _next_attempt_id(paths: ReviewPaths) -> str:
    numbers: list[int] = []
    if paths.results_dir.exists():
        if paths.results_dir.is_symlink() or not paths.results_dir.is_dir():
            raise WorkbenchError("review results path must be a regular directory")
        for path in paths.results_dir.iterdir():
            match = ATTEMPT_ID_PATTERN.fullmatch(path.name)
            if path.is_dir() and not path.is_symlink() and match is not None:
                numbers.append(int(match.group(1)))
    return f"attempt-{max(numbers or [0]) + 1:04d}"


def _strict_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = parse_json_strict(data)
    except WorkbenchError as exc:
        raise WorkbenchError(f"{label}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkbenchError(f"{label} must be a JSON object")
    return value


def _read_review_inputs(
    paths: ReviewPaths,
) -> tuple[
    dict[str, Any],
    bytes,
    dict[str, Any],
    bytes,
    dict[str, Any],
    bytes,
]:
    record, record_bytes = _read_json(paths.record)
    plan, plan_bytes = _read_json(paths.plan)
    library, library_bytes = _read_json(paths.library)
    return record, record_bytes, plan, plan_bytes, library, library_bytes


def prepare_rule_review(
    project_dir: Path | str,
    library_path: Path | str,
    *,
    depth: str,
    review_scope: str = "all",
    claim_ids: list[str] | None = None,
) -> tuple[ReviewPaths, bool]:
    if depth not in CHECK_DEPTHS:
        raise WorkbenchError("review depth must be core or full")
    if review_scope not in REVIEW_SCOPES:
        raise WorkbenchError(f"review scope must be one of {REVIEW_SCOPES}")
    workspace = workspace_paths(project_dir)
    workspace_errors = verify_workspace(workspace)
    if workspace_errors:
        raise WorkbenchError(
            "Argument Workbench project is invalid: " + "; ".join(workspace_errors)
        )
    for path in (workspace.reviewed_payload, workspace.reviewed_record):
        if not path.is_file() or path.is_symlink():
            raise WorkbenchError("Reviewed IR must exist before preparing a review")
    reviewed, reviewed_bytes = _read_json(workspace.reviewed_payload)
    reviewed_record, reviewed_record_bytes = _read_json(workspace.reviewed_record)
    ir_errors = validate_argument_ir(reviewed)
    if ir_errors:
        raise WorkbenchError("Reviewed IR is invalid: " + "; ".join(ir_errors))

    source_library = Path(library_path)
    if source_library.is_symlink():
        raise WorkbenchError("check library must not be a symbolic link")
    resolved_library = source_library.resolve()
    if not resolved_library.is_file():
        raise WorkbenchError(f"check library does not exist: {resolved_library}")
    library_bytes = resolved_library.read_bytes()
    library = _strict_object(library_bytes, "check library")
    library_errors = validate_check_library(library)
    if library_errors:
        raise WorkbenchError("check library is invalid: " + "; ".join(library_errors))

    library_sha256 = sha256_bytes(library_bytes)
    plan = build_check_plan(
        reviewed,
        library,
        ir_sha256=sha256_bytes(reviewed_bytes),
        library_sha256=library_sha256,
        depth=depth,
        review_scope=review_scope,
        claim_ids=claim_ids,
    )
    plan_bytes = json_bytes(plan)
    prompt_bytes = render_check_prompt(
        plan, plan_sha256=sha256_bytes(plan_bytes)
    ).encode("utf-8")

    for existing in list_rule_reviews(workspace.root):
        try:
            record, _ = _read_json(existing.record)
        except (OSError, WorkbenchError):
            continue
        parents = {
            parent.get("role"): parent
            for parent in record.get("parents", [])
            if isinstance(parent, dict)
        }
        if (
            record.get("depth") == depth
            and record.get("lens", {}).get("library_sha256") == library_sha256
            and parents.get("reviewed-ir", {}).get("sha256")
            == sha256_bytes(reviewed_record_bytes)
            and parents.get("target-ir", {}).get("sha256")
            == sha256_bytes(reviewed_bytes)
            and record.get("plan", {}).get("sha256") == sha256_bytes(plan_bytes)
        ):
            return existing, False

    review_id = _next_review_id(workspace.root)
    paths = ReviewPaths(workspace, review_id)
    version_id = str(reviewed_record["version_id"])
    created_at = utc_now()
    record = {
        "schema_version": int(plan["schema_version"]),
        "artifact": "rule-review-run",
        "artifact_id": review_id,
        "lifecycle": "immutable",
        "provenance": _provenance(
            "deterministic", created_at, "workbench-rule-review-v1"
        ),
        "parents": [
            _parent("reviewed-ir", "reviewed-argument-ir", reviewed_record_bytes),
            _parent("target-ir", "argument-ir", reviewed_bytes),
            _parent("check-library", "argument-check-library", library_bytes),
        ],
        "review_id": review_id,
        "project_id": reviewed_record["project_id"],
        "document_id": reviewed_record["document_id"],
        "version_id": version_id,
        "lens": {
            "kind": "rule",
            "id": str(library["scope"]),
            "library_sha256": library_sha256,
        },
        "depth": depth,
        "reviewed_ir_record": {
            "relative_path": "reviewed-ir-record.json",
            "sha256": sha256_bytes(reviewed_record_bytes),
        },
        "target_ir": {
            "relative_path": "target-argument-ir.json",
            "sha256": sha256_bytes(reviewed_bytes),
        },
        "check_library": {
            "relative_path": "check-library.json",
            "sha256": library_sha256,
        },
        "plan": {
            "relative_path": "check-plan.json",
            "sha256": sha256_bytes(plan_bytes),
        },
        "prompt": {
            "relative_path": "review-prompt.md",
            "sha256": sha256_bytes(prompt_bytes),
        },
    }
    if plan["schema_version"] in {2, 3}:
        record["review_scope"] = dict(plan["review_scope"])
    contract_errors = validate_artifact(record)
    if contract_errors:
        raise WorkbenchError(
            "internal Rule Review contract error: " + "; ".join(contract_errors)
        )
    paths.reviews_dir.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{review_id}.", dir=paths.reviews_dir)
    )
    try:
        _write_new(temporary / "review-run.json", json_bytes(record))
        _write_new(temporary / "reviewed-ir-record.json", reviewed_record_bytes)
        _write_new(temporary / "target-argument-ir.json", reviewed_bytes)
        _write_new(temporary / "check-library.json", library_bytes)
        _write_new(temporary / "check-plan.json", plan_bytes)
        _write_new(temporary / "review-prompt.md", prompt_bytes)
        (temporary / "results").mkdir()
        (temporary / "derived").mkdir()
        os.replace(temporary, paths.root)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return paths, True


def _classify_review_response(
    response_bytes: bytes, plan: dict[str, Any], plan_bytes: bytes
) -> tuple[str, list[str], dict[str, Any] | None]:
    try:
        value = parse_json_strict(response_bytes)
    except WorkbenchError as exc:
        return "unusable", [str(exc)], None
    errors = validate_check_results(
        value, plan, plan_sha256=sha256_bytes(plan_bytes)
    )
    return (
        "valid" if not errors else "unusable",
        errors,
        value if isinstance(value, dict) else None,
    )


def collect_review_results(
    project_dir: Path | str,
    response_bytes: bytes,
    *,
    review_id: str | None,
    method: str,
    source_name: str,
    producer_label: str | None,
) -> tuple[Path, dict[str, Any]]:
    if method not in COLLECTION_METHODS:
        raise WorkbenchError("collection method must be file or terminal-paste")
    if not source_name or Path(source_name).name != source_name:
        raise WorkbenchError("collection source_name must be a basename")
    paths = selected_rule_review(project_dir, review_id)
    workspace_errors = verify_workspace(paths.workspace)
    if workspace_errors:
        raise WorkbenchError(
            "Argument Workbench project is invalid: " + "; ".join(workspace_errors)
        )
    review, review_bytes, plan, plan_bytes, _, _ = _read_review_inputs(paths)
    review_errors = validate_artifact(review)
    if review_errors:
        raise WorkbenchError("Rule Review is invalid: " + "; ".join(review_errors))
    status, validation_errors, _ = _classify_review_response(
        response_bytes, plan, plan_bytes
    )
    attempt_id = _next_attempt_id(paths)
    attempt_dir = paths.attempt_dir(attempt_id)
    if attempt_dir.exists() or attempt_dir.is_symlink():
        raise WorkbenchError(f"review result attempt already exists: {attempt_dir}")
    created_at = utc_now()
    record = {
        "schema_version": 1,
        "artifact": "review-result-attempt",
        "artifact_id": f"{paths.review_id}-{attempt_id}",
        "lifecycle": "immutable",
        "provenance": _provenance(
            "model-derived", created_at, producer_label or "unlabeled-model"
        ),
        "parents": [_parent("review-run", "rule-review-run", review_bytes)],
        "review_id": paths.review_id,
        "attempt_id": attempt_id,
        "collection": {
            "method": method,
            "source_name": source_name,
            "producer_label": producer_label,
        },
        "response": {
            "relative_path": "response.json",
            "sha256": sha256_bytes(response_bytes),
        },
        "validation": {"status": status, "errors": validation_errors},
    }
    contract_errors = validate_artifact(record)
    if contract_errors:
        raise WorkbenchError(
            "internal review result contract error: " + "; ".join(contract_errors)
        )
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{attempt_id}.", dir=paths.results_dir)
    )
    try:
        _write_new(temporary / "response.json", response_bytes)
        _write_new(temporary / "record.json", json_bytes(record))
        os.replace(temporary, attempt_dir)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    if status == "valid":
        rebuild_review_attempt(paths, attempt_id)
    return attempt_dir, record


def list_result_attempts(
    paths: ReviewPaths,
) -> list[tuple[Path, dict[str, Any], bytes]]:
    if not paths.results_dir.exists():
        return []
    if paths.results_dir.is_symlink() or not paths.results_dir.is_dir():
        raise WorkbenchError("review results must be a regular non-symlink directory")
    attempts: list[tuple[Path, dict[str, Any], bytes]] = []
    for path in paths.results_dir.iterdir():
        if path.is_symlink():
            raise WorkbenchError(f"review result attempt must not be a symlink: {path}")
        if not path.is_dir() or ATTEMPT_ID_PATTERN.fullmatch(path.name) is None:
            continue
        record, record_bytes = _read_json(path / "record.json")
        attempts.append((path, record, record_bytes))
    return sorted(attempts, key=lambda item: item[0].name)


def selected_result_attempt(
    paths: ReviewPaths,
) -> tuple[Path, dict[str, Any], bytes]:
    for attempt in reversed(list_result_attempts(paths)):
        if attempt[1].get("validation", {}).get("status") == "valid":
            return attempt
    raise WorkbenchError(
        f"{paths.review_id} has no valid model result; run `ir review collect`"
    )


def _versioned(version_id: str, reference: str) -> str:
    return f"{version_id}:{reference}"


def _derive_review_attempt(
    paths: ReviewPaths, attempt_id: str
) -> tuple[dict[str, bytes], list[tuple[object, bytes]]]:
    review, review_bytes, plan, plan_bytes, library, library_bytes = _read_review_inputs(
        paths
    )
    attempt_dir = paths.attempt_dir(attempt_id)
    attempt, attempt_bytes = _read_json(attempt_dir / "record.json")
    response_bytes = (attempt_dir / "response.json").read_bytes()
    status, result_errors, results = _classify_review_response(
        response_bytes, plan, plan_bytes
    )
    if status != "valid" or results is None:
        raise WorkbenchError(
            f"cannot derive findings from invalid {attempt_id}: "
            + "; ".join(result_errors)
        )
    reviewed_ir, reviewed_ir_bytes = _read_json(paths.target_ir)
    reviewed_record, reviewed_record_bytes = _read_json(paths.reviewed_record)
    version_id = str(review["version_id"])
    task_by_id = {task["id"]: task for task in plan["tasks"]}
    claim_by_id = {claim["id"]: claim for claim in reviewed_ir["claims"]}
    outcomes: list[dict[str, Any]] = []
    finding_values: list[tuple[str, dict[str, Any], bytes]] = []
    actionable_number = 0
    result_schema_version = int(results["schema_version"])
    for result in results["results"]:
        task = task_by_id[result["task_id"]]
        execution_status = (
            str(result["execution_status"])
            if result_schema_version in {2, 3}
            else "evaluated"
        )
        verdict = result["verdict"]
        basis_refs = (
            list(result["basis_refs"])
            if result_schema_version in {2, 3}
            else list(result["evidence_refs"])
        )
        support_refs = (
            list(result["support_refs"])
            if result_schema_version in {2, 3}
            else []
        )
        support_paths = (
            list(result["support_paths"])
            if result_schema_version == 3
            else []
        )
        finding_id: str | None = None
        if execution_status == "evaluated" and verdict in {"fail", "uncertain"}:
            actionable_number += 1
            finding_id = (
                f"{version_id}-{paths.review_id}-{attempt_id}-F{actionable_number:04d}"
            )
            finding = {
                "schema_version": 1,
                "artifact": "argument-finding",
                "artifact_id": finding_id,
                "lifecycle": "immutable",
                "provenance": {
                    "origin": "model-derived",
                    "created_at": attempt["provenance"]["created_at"],
                    "producer": attempt["provenance"]["producer"],
                },
                "parents": [
                    _parent("target-ir", "argument-ir", reviewed_ir_bytes),
                    _parent(
                        "lens-result", "argument-check-results", response_bytes
                    ),
                ],
                "finding_id": finding_id,
                "target_claim": _versioned(version_id, task["claim_id"]),
                "lens": {
                    "kind": "rule",
                    "id": review["lens"]["id"],
                    "check_id": task["check_id"],
                },
                "verdict": verdict,
                "reason": result["reason"],
                "evidence_refs": [
                    _versioned(version_id, reference)
                    for reference in basis_refs
                ],
                "status": "open",
            }
            finding_errors = validate_artifact(finding)
            if finding_errors:
                raise WorkbenchError(
                    "internal Finding contract error: " + "; ".join(finding_errors)
                )
            finding_bytes = json_bytes(finding)
            finding_values.append(
                (f"findings/F{actionable_number:04d}.json", finding, finding_bytes)
            )
        outcome = {
            "task_id": result["task_id"],
            "target_claim": _versioned(version_id, task["claim_id"]),
            "check_id": task["check_id"],
            "verdict": verdict,
            "reason": result["reason"],
            "consequence": result["consequence"],
        }
        if result_schema_version in {2, 3}:
            outcome["execution_status"] = execution_status
            outcome["basis_refs"] = [
                _versioned(version_id, reference) for reference in basis_refs
            ]
            outcome["support_refs"] = [
                _versioned(version_id, reference) for reference in support_refs
            ]
            if result_schema_version == 3:
                outcome["support_paths"] = [
                    {
                        "support_ref": _versioned(
                            version_id, str(path["support_ref"])
                        ),
                        "relation_ids": [
                            _versioned(version_id, str(relation_id))
                            for relation_id in path["relation_ids"]
                        ],
                    }
                    for path in support_paths
                ]
            outcome["finding_id"] = finding_id
        else:
            outcome["evidence_refs"] = [
                _versioned(version_id, reference) for reference in basis_refs
            ]
            outcome["finding_id"] = finding_id
        outcomes.append(outcome)
    summary = {
        verdict_name: sum(
            1
            for outcome in outcomes
            if outcome.get("execution_status", "evaluated") == "evaluated"
            and outcome["verdict"] == verdict_name
        )
        for verdict_name in FINDING_VERDICTS
    }
    if result_schema_version in {2, 3}:
        summary.update(
            {
                status_name: sum(
                    1
                    for outcome in outcomes
                    if outcome["execution_status"] == status_name
                )
                for status_name in (
                    "blocked_missing_context",
                    "routing_mismatch",
                    "not_applicable",
                )
            }
        )
    markdown = render_claim_review(
        review,
        plan,
        reviewed_ir,
        outcomes,
        summary,
        attempt_id=attempt_id,
        result_sha256=sha256_bytes(response_bytes),
    )
    markdown_bytes = markdown.encode("utf-8")
    index_parents = [
        _parent("review-run", "rule-review-run", review_bytes),
        _parent("result-attempt", "review-result-attempt", attempt_bytes),
        _parent("lens-result", "argument-check-results", response_bytes),
    ]
    for index, (_, _, finding_bytes) in enumerate(finding_values, 1):
        index_parents.append(
            _parent(f"finding-{index:04d}", "argument-finding", finding_bytes)
        )
    index = {
        "schema_version": result_schema_version,
        "artifact": "claim-review-index",
        "artifact_id": f"{paths.review_id}-{attempt_id}-claim-review",
        "lifecycle": "derived-replaceable",
        "provenance": _provenance(
            "deterministic",
            str(attempt["provenance"]["created_at"]),
            "workbench-rule-review-v1",
        ),
        "parents": index_parents,
        "review_id": paths.review_id,
        "attempt_id": attempt_id,
        "version_id": version_id,
        "lens": dict(review["lens"]),
        "summary": summary,
        "outcomes": outcomes,
        "view": {
            "relative_path": "claim-review.md",
            "sha256": sha256_bytes(markdown_bytes),
        },
        "field_provenance": {
            "outcomes.task_id": {
                "origin": "deterministic",
                "source": "argument-check-plan",
            },
            "outcomes.target_claim": {
                "origin": "deterministic",
                "source": "argument-check-plan",
            },
            "outcomes.check_id": {
                "origin": "deterministic",
                "source": "argument-check-plan",
            },
            "outcomes.verdict": {
                "origin": "model-derived",
                "source": "review-result-attempt",
            },
            "outcomes.reason": {
                "origin": "model-derived",
                "source": "review-result-attempt",
            },
            "outcomes.consequence": {
                "origin": "model-derived",
                "source": "review-result-attempt",
            },
            "outcomes.finding_id": {
                "origin": "deterministic",
                "source": "workbench-rule-review-v1",
            },
            "summary": {
                "origin": "deterministic",
                "source": "workbench-rule-review-v1",
            },
            "view": {
                "origin": "deterministic",
                "source": "workbench-rule-review-v1",
            },
        },
    }
    if result_schema_version in {2, 3}:
        for field in ("execution_status", "basis_refs", "support_refs"):
            index["field_provenance"][f"outcomes.{field}"] = {
                "origin": "model-derived",
                "source": "review-result-attempt",
            }
        if result_schema_version == 3:
            index["field_provenance"]["outcomes.support_paths"] = {
                "origin": "model-derived",
                "source": "review-result-attempt",
            }
    else:
        legacy_provenance = index["field_provenance"]
        index["field_provenance"] = {
            key: legacy_provenance[key]
            for key in (
                "outcomes.task_id",
                "outcomes.target_claim",
                "outcomes.check_id",
                "outcomes.verdict",
                "outcomes.reason",
                "outcomes.consequence",
            )
        }
        index["field_provenance"]["outcomes.evidence_refs"] = {
            "origin": "model-derived",
            "source": "review-result-attempt",
        }
        for key in (
            "outcomes.finding_id",
            "summary",
            "view",
        ):
            index["field_provenance"][key] = legacy_provenance[key]
    index_errors = validate_artifact(index)
    if index_errors:
        raise WorkbenchError(
            "internal claim review index error: " + "; ".join(index_errors)
        )
    index_bytes = json_bytes(index)
    files: dict[str, bytes] = {
        "claim-review.md": markdown_bytes,
        "claim-review-index.json": index_bytes,
    }
    for relative, _, finding_bytes in finding_values:
        files[relative] = finding_bytes
    entries: list[tuple[object, bytes]] = [
        (reviewed_record, reviewed_record_bytes),
        (review, review_bytes),
        (attempt, attempt_bytes),
    ]
    entries.extend((finding, data) for _, finding, data in finding_values)
    entries.append((index, index_bytes))
    return files, entries


def _write_derived_files(root: Path, files: dict[str, bytes]) -> bool:
    if root.is_symlink():
        raise WorkbenchError(f"derived review directory must not be a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True)
    findings_dir = root / "findings"
    if findings_dir.is_symlink():
        raise WorkbenchError("derived findings directory must not be a symlink")
    findings_dir.mkdir(exist_ok=True)
    allowed = {Path(relative) for relative in files}
    changed = False
    for existing in root.rglob("*"):
        if existing.is_symlink():
            raise WorkbenchError(f"derived review artifact must not be a symlink: {existing}")
        if existing.is_file():
            relative = existing.relative_to(root)
            if relative not in allowed:
                if relative.parent == Path("findings") and FINDING_FILE_PATTERN.fullmatch(
                    relative.name
                ):
                    existing.unlink()
                    changed = True
                else:
                    raise WorkbenchError(
                        f"unexpected file in derived review cache: {relative.as_posix()}"
                    )
    for relative_text, data in files.items():
        path = root / Path(relative_text)
        if path.exists() and path.is_symlink():
            raise WorkbenchError(f"derived review artifact must not be a symlink: {path}")
        if not path.exists() or path.read_bytes() != data:
            _atomic_write(path, data)
            changed = True
    return changed


def rebuild_review_attempt(paths: ReviewPaths, attempt_id: str) -> tuple[Path, bool]:
    if ATTEMPT_ID_PATTERN.fullmatch(attempt_id) is None:
        raise WorkbenchError("attempt ID must be attempt-NNNN")
    files, _ = _derive_review_attempt(paths, attempt_id)
    root = paths.derived_attempt_dir(attempt_id)
    changed = _write_derived_files(root, files)
    return root / "claim-review.md", changed


def rebuild_reviews(project_dir: Path | str) -> tuple[list[Path], bool]:
    outputs: list[Path] = []
    changed = False
    for review in list_rule_reviews(project_dir):
        for _, record, _ in list_result_attempts(review):
            attempt_id = str(record.get("attempt_id"))
            derived = review.derived_attempt_dir(attempt_id)
            if record.get("validation", {}).get("status") == "valid":
                output, attempt_changed = rebuild_review_attempt(review, attempt_id)
                outputs.append(output)
                changed = changed or attempt_changed
            elif derived.exists():
                raise WorkbenchError(
                    f"invalid review result must not have derived artifacts: {derived}"
                )
    return outputs, changed


def render_claim_review(
    review: dict[str, Any],
    plan: dict[str, Any],
    reviewed_ir: dict[str, Any],
    outcomes: list[dict[str, Any]],
    summary: dict[str, int],
    *,
    attempt_id: str,
    result_sha256: str,
    only_claim: str | None = None,
) -> str:
    claim_by_versioned = {
        _versioned(str(review["version_id"]), claim["id"]): claim
        for claim in reviewed_ir["claims"]
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for outcome in outcomes:
        grouped.setdefault(str(outcome["target_claim"]), []).append(outcome)
    if only_claim is not None:
        if only_claim not in claim_by_versioned:
            raise WorkbenchError(f"unknown claim for {review['review_id']}: {only_claim}")
        grouped = {only_claim: grouped.get(only_claim, [])}
        selected_outcomes = grouped[only_claim]
        shown_summary = {
            key: sum(
                1
                for outcome in selected_outcomes
                if (
                    outcome.get("execution_status", "evaluated") == "evaluated"
                    and outcome["verdict"] == key
                )
                or outcome.get("execution_status") == key
            )
            for key in summary
        }
    else:
        shown_summary = summary
    lines = [
        "# Claim-Centered Review",
        "",
        f"- Review: `{review['review_id']}`",
        f"- Version: `{review['version_id']}`",
        f"- Lens: `{review['lens']['id']}` (Rule Lens)",
        f"- Depth: `{review['depth']}`",
        *(
            [
                f"- Scope: `{review['review_scope']['kind']}`; Claims: "
                + ", ".join(review["review_scope"]["selected_claim_ids"])
            ]
            if "review_scope" in review
            else []
        ),
        f"- Result attempt: `{attempt_id}`",
        f"- Result SHA-256: `{result_sha256}`",
        "- Semantic verdicts and reasons are model-derived; Finding status remains open until human adjudication.",
        *(
            [
                "- Non-evaluated execution statuses are model-derived and remain open until separate human triage."
            ]
            if any("support_paths" in outcome for outcome in outcomes)
            else []
        ),
        f"- Outcomes: {shown_summary['pass']} pass, {shown_summary['fail']} fail, {shown_summary['uncertain']} uncertain",
        *(
            [
                "- Non-findings: "
                f"{shown_summary['blocked_missing_context']} blocked missing context, "
                f"{shown_summary['routing_mismatch']} routing mismatch, "
                f"{shown_summary['not_applicable']} not applicable"
            ]
            if "blocked_missing_context" in shown_summary
            else []
        ),
        "",
    ]
    for claim_id, claim in claim_by_versioned.items():
        if only_claim is not None and claim_id != only_claim:
            continue
        claim_outcomes = grouped.get(claim_id, [])
        if not claim_outcomes and only_claim is None:
            continue
        lines.extend(
            [
                f"## {claim_id}",
                "",
                f"- Claim: {claim['text']}",
                f"- Source: {claim['source_quote']}",
                f"- Position: `{claim['position']}` `[deterministic]`",
                "",
            ]
        )
        if not claim_outcomes:
            lines.extend(["No applicable checks in this review.", ""])
            continue
        for outcome in claim_outcomes:
            execution_status = outcome.get("execution_status", "evaluated")
            marker = (
                {
                    "pass": "PASS",
                    "fail": "FAIL",
                    "uncertain": "UNCERTAIN",
                }[str(outcome["verdict"])]
                if execution_status == "evaluated"
                else str(execution_status).upper()
            )
            finding = (
                f" `{outcome['finding_id']}`" if outcome["finding_id"] is not None else ""
            )
            lines.extend(
                [
                    f"### {marker}{finding} - `{outcome['check_id']}`",
                    "",
                    f"- Reason: {outcome['reason']} `[model-derived]`",
                    *(
                        [
                            f"- Basis refs: {', '.join(outcome['basis_refs']) or 'none'}",
                            f"- PASS support refs: {', '.join(outcome['support_refs']) or 'none'}",
                            *(
                                [
                                    "- PASS support paths: "
                                    + "; ".join(
                                        f"{path['support_ref']} via "
                                        + " -> ".join(path["relation_ids"])
                                        for path in outcome["support_paths"]
                                    )
                                ]
                                if "support_paths" in outcome
                                else []
                            ),
                        ]
                        if "basis_refs" in outcome
                        else [
                            f"- Evidence refs: {', '.join(outcome['evidence_refs']) or 'none'}"
                        ]
                    ),
                ]
            )
            if outcome["consequence"]:
                lines.append(f"- Consequence: {outcome['consequence']}")
            lines.append("")
    return "\n".join(lines)


def show_claim_review(
    project_dir: Path | str,
    *,
    review_id: str | None,
    claim_id: str | None,
) -> tuple[str, Path]:
    if review_id is None:
        paths = None
        for candidate in reversed(list_rule_reviews(project_dir)):
            try:
                selected_result_attempt(candidate)
            except WorkbenchError:
                continue
            paths = candidate
            break
        if paths is None:
            raise WorkbenchError(
                "project has no Rule Review with valid results; run `ir review collect`"
            )
    else:
        paths = selected_rule_review(project_dir, review_id)
    workspace_errors = verify_workspace(paths.workspace)
    if workspace_errors:
        raise WorkbenchError(
            "Argument Workbench project is invalid: " + "; ".join(workspace_errors)
        )
    attempt_dir, attempt, _ = selected_result_attempt(paths)
    attempt_id = str(attempt["attempt_id"])
    files, _ = _derive_review_attempt(paths, attempt_id)
    index = _strict_object(files["claim-review-index.json"], "claim review index")
    review, _, plan, _, _, _ = _read_review_inputs(paths)
    reviewed_ir, _ = _read_json(paths.target_ir)
    normalized_claim: str | None = None
    if claim_id is not None:
        candidate = claim_id.strip().upper()
        normalized_claim = (
            candidate
            if ":" in candidate
            else _versioned(str(review["version_id"]), candidate)
        )
    rendered = render_claim_review(
        review,
        plan,
        reviewed_ir,
        list(index["outcomes"]),
        dict(index["summary"]),
        attempt_id=attempt_id,
        result_sha256=sha256_bytes((attempt_dir / "response.json").read_bytes()),
        only_claim=normalized_claim,
    )
    return rendered, paths.derived_attempt_dir(attempt_id) / "claim-review.md"


def _verify_derived_attempt(
    paths: ReviewPaths,
    attempt_id: str,
    errors: list[str],
    entries: list[tuple[object, bytes]],
) -> None:
    derived_root = paths.derived_attempt_dir(attempt_id)
    try:
        expected, expected_entries = _derive_review_attempt(paths, attempt_id)
    except (OSError, WorkbenchError) as exc:
        errors.append(f"{paths.review_id}/{attempt_id}: cannot derive review: {exc}")
        return
    entries.extend(expected_entries[3:])
    if not derived_root.is_dir() or derived_root.is_symlink():
        errors.append(f"{paths.review_id}/{attempt_id}: derived review cache is missing")
        return
    actual_files: dict[str, Path] = {}
    for path in derived_root.rglob("*"):
        if path.is_symlink():
            errors.append(
                f"{paths.review_id}/{attempt_id}: derived artifact is a symlink: {path}"
            )
        elif path.is_file():
            actual_files[path.relative_to(derived_root).as_posix()] = path
    if set(actual_files) != set(expected):
        errors.append(
            f"{paths.review_id}/{attempt_id}: derived file set is not reproducible"
        )
    for relative, data in expected.items():
        path = actual_files.get(relative)
        if path is not None and path.read_bytes() != data:
            errors.append(
                f"{paths.review_id}/{attempt_id}: {relative} is not reproducible"
            )


def verify_reviews(project_dir: Path | str) -> list[str]:
    workspace = workspace_paths(project_dir)
    errors: list[str] = []
    reviews_dir = workspace.version_dir / "reviews"
    if not reviews_dir.exists():
        return errors
    if reviews_dir.is_symlink() or not reviews_dir.is_dir():
        return ["reviews must be a regular non-symlink directory"]
    try:
        reviews = list_rule_reviews(workspace.root)
    except (OSError, WorkbenchError) as exc:
        return [str(exc)]
    known_review_names = {review.review_id for review in reviews}
    for child in reviews_dir.iterdir():
        if child.name not in known_review_names:
            errors.append(f"unexpected entry in reviews directory: {child.name}")
    expected_numbers = list(range(1, len(reviews) + 1))
    actual_numbers = [
        int(REVIEW_ID_PATTERN.fullmatch(review.review_id).group(1)) for review in reviews
    ]
    if actual_numbers != expected_numbers:
        errors.append("Rule Review IDs must be continuous from RV1")
    base_entries: list[tuple[object, bytes]] = []
    try:
        for path in (workspace.project, workspace.document, workspace.version):
            base_entries.append(_read_json(path))
        base_entries.extend(
            (value, data) for _, value, data in list_attempts(workspace)
        )
        base_entries.extend(
            (value, data) for _, value, data in correction_entries(workspace)
        )
    except (OSError, WorkbenchError) as exc:
        errors.append(f"cannot load review provenance ancestors: {exc}")
        return errors
    for paths in reviews:
        prefix = paths.review_id
        entries: list[tuple[object, bytes]] = list(base_entries)
        try:
            review, review_bytes, plan, plan_bytes, library, library_bytes = (
                _read_review_inputs(paths)
            )
            reviewed_record, reviewed_record_bytes = _read_json(paths.reviewed_record)
            reviewed_ir, reviewed_ir_bytes = _read_json(paths.target_ir)
        except (OSError, WorkbenchError) as exc:
            errors.append(f"{prefix}: {exc}")
            continue
        entries.append((reviewed_record, reviewed_record_bytes))
        entries.append((review, review_bytes))
        reviewed_record_errors = validate_artifact(reviewed_record)
        errors.extend(
            f"{prefix}: reviewed IR snapshot: {error}"
            for error in reviewed_record_errors
        )
        ir_errors = validate_argument_ir(reviewed_ir)
        errors.extend(f"{prefix}: target IR snapshot: {error}" for error in ir_errors)
        if reviewed_record.get("payload", {}).get("sha256") != sha256_bytes(
            reviewed_ir_bytes
        ):
            errors.append(f"{prefix}: Reviewed IR snapshot payload hash is disconnected")
        contract_errors = validate_artifact(review)
        errors.extend(f"{prefix}: {error}" for error in contract_errors)
        parents = {
            parent.get("role"): parent
            for parent in review.get("parents", [])
            if isinstance(parent, dict)
        }
        expected_parent_hashes = {
            "reviewed-ir": sha256_bytes(reviewed_record_bytes),
            "target-ir": sha256_bytes(reviewed_ir_bytes),
            "check-library": sha256_bytes(library_bytes),
        }
        for role, digest in expected_parent_hashes.items():
            if parents.get(role, {}).get("sha256") != digest:
                errors.append(f"{prefix}: {role} parent hash does not match exact bytes")
        for field, path in (
            ("reviewed_ir_record", paths.reviewed_record),
            ("target_ir", paths.target_ir),
            ("check_library", paths.library),
            ("plan", paths.plan),
            ("prompt", paths.prompt),
        ):
            binding = review.get(field, {})
            expected_binding = {
                "relative_path": path.name,
                "sha256": sha256_bytes(path.read_bytes()),
            }
            if binding != expected_binding:
                errors.append(f"{prefix}: {field} binding does not match exact bytes/path")
        expected_lens = {
            "kind": "rule",
            "id": library.get("scope"),
            "library_sha256": sha256_bytes(library_bytes),
        }
        if review.get("lens") != expected_lens:
            errors.append(f"{prefix}: Rule Lens identity is not bound to the library")
        if review.get("depth") != plan.get("depth"):
            errors.append(f"{prefix}: review depth does not match check plan")
        if plan.get("schema_version") in {2, 3} and review.get("review_scope") != plan.get(
            "review_scope"
        ):
            errors.append(f"{prefix}: review scope does not match check plan")
        for field in ("project_id", "document_id", "version_id"):
            if review.get(field) != reviewed_record.get(field):
                errors.append(
                    f"{prefix}: {field} does not match Reviewed IR snapshot"
                )
        library_errors = validate_check_library(library)
        errors.extend(f"{prefix}: check library: {error}" for error in library_errors)
        plan_errors = validate_check_plan_against_library(
            plan, library, library_sha256=sha256_bytes(library_bytes)
        )
        errors.extend(f"{prefix}: check plan: {error}" for error in plan_errors)
        if plan.get("argument_ir") != reviewed_ir:
            errors.append(f"{prefix}: check plan does not contain its target IR snapshot")
        if plan.get("source", {}).get("ir_sha256") != sha256_bytes(
            reviewed_ir_bytes
        ):
            errors.append(f"{prefix}: check plan target IR hash is disconnected")
        try:
            expected_prompt = render_check_prompt(
                plan, plan_sha256=sha256_bytes(plan_bytes)
            ).encode("utf-8")
            if paths.prompt.read_bytes() != expected_prompt:
                errors.append(f"{prefix}: review prompt is not reproducible")
        except ArgumentIRError as exc:
            errors.append(f"{prefix}: cannot reproduce review prompt: {exc}")
        try:
            attempts = list_result_attempts(paths)
        except (OSError, WorkbenchError) as exc:
            errors.append(f"{prefix}: {exc}")
            continue
        known_attempt_names = {attempt_path.name for attempt_path, _, _ in attempts}
        for child in paths.results_dir.iterdir():
            if child.name not in known_attempt_names:
                errors.append(f"{prefix}: unexpected entry in results directory: {child.name}")
        if paths.derived_dir.is_symlink() or not paths.derived_dir.is_dir():
            errors.append(f"{prefix}: derived must be a regular non-symlink directory")
            continue
        for child in paths.derived_dir.iterdir():
            if child.name not in known_attempt_names:
                errors.append(f"{prefix}: unexpected entry in derived directory: {child.name}")
        actual_attempt_numbers = [
            int(ATTEMPT_ID_PATTERN.fullmatch(path.name).group(1))
            for path, _, _ in attempts
        ]
        if actual_attempt_numbers != list(range(1, len(attempts) + 1)):
            errors.append(f"{prefix}: result attempt IDs must be continuous from 0001")
        for attempt_dir, attempt, attempt_bytes in attempts:
            attempt_prefix = f"{prefix}/{attempt_dir.name}"
            entries.append((attempt, attempt_bytes))
            attempt_errors = validate_artifact(attempt)
            errors.extend(f"{attempt_prefix}: {error}" for error in attempt_errors)
            if attempt.get("review_id") != paths.review_id:
                errors.append(f"{attempt_prefix}: review_id does not match directory")
            if attempt.get("attempt_id") != attempt_dir.name:
                errors.append(f"{attempt_prefix}: attempt_id does not match directory")
            parent_by_role = {
                parent.get("role"): parent
                for parent in attempt.get("parents", [])
                if isinstance(parent, dict)
            }
            if parent_by_role.get("review-run", {}).get("sha256") != sha256_bytes(
                review_bytes
            ):
                errors.append(f"{attempt_prefix}: review-run parent hash is disconnected")
            response_path = attempt_dir / "response.json"
            if not response_path.is_file() or response_path.is_symlink():
                errors.append(f"{attempt_prefix}: response.json is missing or a symlink")
                continue
            response_bytes = response_path.read_bytes()
            if attempt.get("response", {}).get("sha256") != sha256_bytes(
                response_bytes
            ):
                errors.append(f"{attempt_prefix}: response hash does not match exact bytes")
            status, result_errors, _ = _classify_review_response(
                response_bytes, plan, plan_bytes
            )
            expected_validation = {"status": status, "errors": result_errors}
            if attempt.get("validation") != expected_validation:
                errors.append(f"{attempt_prefix}: recorded validation is not reproducible")
            derived_root = paths.derived_attempt_dir(attempt_dir.name)
            if status == "valid":
                _verify_derived_attempt(
                    paths, attempt_dir.name, errors, entries
                )
            elif derived_root.exists() or derived_root.is_symlink():
                errors.append(
                    f"{attempt_prefix}: unusable result must not have derived artifacts"
                )
        bundle_errors = validate_contract_bundle(
            [(value, data) for value, data in entries]
        )
        errors.extend(f"{prefix}: {error}" for error in bundle_errors)
    return errors
