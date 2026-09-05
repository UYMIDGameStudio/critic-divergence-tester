"""Artifact contract validators grouped by family."""

from __future__ import annotations

from .base import *  # noqa: F403
from .core import *  # noqa: F403
from .lineage import *  # noqa: F403

def validate_resolution_retest_run(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="resolution-retest-run",
        lifecycle="immutable",
        extra_keys={
            "resolution_id", "document_id", "from_version", "to_version",
            "original_finding_id", "descendant_claims", "lens",
            "original_finding", "accepted_adjudication", "revision_actions",
            "confirmed_lineage", "target_ir", "lens_protocol", "prompt",
            "lens_content",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "resolution-retest-run", errors)
    if not _nonempty(item.get("resolution_id")):
        errors.append("resolution_id must be non-empty")
    if not _nonempty(item.get("document_id")):
        errors.append("document_id must be non-empty")
    for field in ("from_version", "to_version"):
        if not isinstance(item.get(field), str) or re.fullmatch(
            r"V[1-9][0-9]*", str(item.get(field))
        ) is None:
            errors.append(f"{field} must be V1..Vn")
    if not _nonempty(item.get("original_finding_id")):
        errors.append("original_finding_id must be non-empty")
    descendants = _string_list(
        item.get("descendant_claims"), "descendant_claims", errors
    )
    to_version = item.get("to_version")
    for claim in descendants:
        if _VERSIONED_CLAIM.fullmatch(claim) is None or not claim.startswith(
            f"{to_version}:"
        ):
            errors.append("descendant_claims must belong to to_version")
    lens = item.get("lens")
    if not isinstance(lens, dict):
        errors.append("lens must be an object")
        lens_kind = None
    else:
        _strict_keys(lens, {"kind", "id", "check_id"}, "lens", errors)
        lens_kind = lens.get("kind")
        if lens_kind not in LENS_KINDS:
            errors.append(f"lens.kind must be one of {LENS_KINDS}")
        if not _nonempty(lens.get("id")):
            errors.append("lens.id must be non-empty")
        if lens_kind == "rule" and not _nonempty(lens.get("check_id")):
            errors.append("rule resolution retest requires check_id")
        if lens_kind == "perspective" and lens.get("check_id") is not None:
            errors.append("perspective resolution retest requires check_id=null")
    actions = item.get("revision_actions")
    if not isinstance(actions, list) or not actions:
        errors.append("revision_actions must be a non-empty array")
        action_count = 0
    else:
        action_count = len(actions)
        for index, action in enumerate(actions):
            _validate_bound_file(action, f"revision_actions[{index}]", errors)
    for field in (
        "original_finding", "accepted_adjudication", "confirmed_lineage",
        "target_ir", "lens_protocol", "prompt",
        "lens_content",
    ):
        _validate_bound_file(item.get(field), field, errors)
    roles = {
        "original-finding": "argument-finding",
        "accepted-adjudication": "finding-adjudication",
        "confirmed-lineage": "claim-lineage",
        "target-ir": "argument-ir",
        "lens-protocol": (
            "perspective-lens-protocol"
            if lens_kind == "perspective"
            else "argument-check-library"
        ),
    }
    for index in range(1, action_count + 1):
        roles[f"revision-action-{index:04d}"] = "revision-action"
    _require_parent_roles(item, set(roles), errors)
    _require_parent_artifacts(item, roles, errors)
    return errors


def validate_resolution_result_attempt(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="resolution-result-attempt",
        lifecycle="immutable",
        extra_keys={"resolution_id", "attempt_id", "collection", "response", "validation"},
    )
    if item is None:
        return errors
    _require_origin(item, {"model-derived"}, "resolution-result-attempt", errors)
    _require_parent_roles(item, {"retest-run"}, errors)
    _require_parent_artifacts(item, {"retest-run": "resolution-retest-run"}, errors)
    if not _nonempty(item.get("resolution_id")):
        errors.append("resolution_id must be non-empty")
    if not isinstance(item.get("attempt_id"), str) or re.fullmatch(
        r"attempt-[0-9]{4}", str(item.get("attempt_id"))
    ) is None:
        errors.append("attempt_id must be attempt-NNNN")
    collection = item.get("collection")
    if not isinstance(collection, dict):
        errors.append("collection must be an object")
    else:
        _strict_keys(collection, {"method", "source_name", "producer_label"}, "collection", errors)
        if collection.get("method") not in {"file", "terminal-paste"}:
            errors.append("collection.method must be file or terminal-paste")
        if not _safe_basename(collection.get("source_name")):
            errors.append("collection.source_name must be a safe basename")
        if collection.get("producer_label") is not None and not _nonempty(collection.get("producer_label")):
            errors.append("collection.producer_label must be null or non-empty")
    _validate_bound_file(item.get("response"), "response", errors)
    validation = item.get("validation")
    if not isinstance(validation, dict):
        errors.append("validation must be an object")
    else:
        _strict_keys(validation, {"status", "errors"}, "validation", errors)
        if validation.get("status") not in REVIEW_RESULT_STATUSES:
            errors.append(f"validation.status must be one of {REVIEW_RESULT_STATUSES}")
        found = _string_list(validation.get("errors"), "validation.errors", errors)
        if validation.get("status") == "valid" and found:
            errors.append("valid resolution result requires errors=[]")
        if validation.get("status") == "unusable" and not found:
            errors.append("unusable resolution result requires errors")
    return errors


def validate_resolution_retest_results(value: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["resolution-retest-results must be an object"]
    _strict_keys(value, {"schema_version", "artifact", "source", "status", "unverified", "results"}, "resolution-retest-results", errors)
    if value.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if value.get("artifact") != "resolution-retest-results":
        errors.append("artifact must be resolution-retest-results")
    source = value.get("source")
    if not isinstance(source, dict):
        errors.append("source must be an object")
    else:
        _strict_keys(source, {"original_finding_sha256", "target_ir_sha256", "lens_protocol_sha256"}, "source", errors)
        for field in ("original_finding_sha256", "target_ir_sha256", "lens_protocol_sha256"):
            if not _digest(source.get(field)):
                errors.append(f"source.{field} must be a SHA-256 digest")
    status = value.get("status")
    if status not in RESOLUTION_RETEST_STATUSES:
        errors.append(f"status must be one of {RESOLUTION_RETEST_STATUSES}")
    unverified = _string_list(value.get("unverified"), "unverified", errors)
    results = value.get("results")
    if not isinstance(results, list):
        errors.append("results must be an array")
        return errors
    if status == "complete" and unverified:
        errors.append("complete retest requires unverified=[]")
    if status == "blocked" and (not unverified or results):
        errors.append("blocked retest requires unverified and results=[]")
    for index, result in enumerate(results):
        label = f"results[{index}]"
        if not isinstance(result, dict):
            errors.append(f"{label} must be an object")
            continue
        _strict_keys(result, {"target_claim", "verdict", "reason", "basis_refs", "support_refs", "support_paths", "analysis"}, label, errors)
        if not isinstance(result.get("target_claim"), str) or _VERSIONED_CLAIM.fullmatch(str(result.get("target_claim"))) is None:
            errors.append(f"{label}.target_claim must be version-qualified")
        if result.get("verdict") not in FINDING_VERDICTS:
            errors.append(f"{label}.verdict must be one of {FINDING_VERDICTS}")
        if not _nonempty(result.get("reason")):
            errors.append(f"{label}.reason must be non-empty")
        _string_list(result.get("basis_refs"), f"{label}.basis_refs", errors, allow_empty=False)
        support_refs = _string_list(result.get("support_refs"), f"{label}.support_refs", errors)
        support_paths = result.get("support_paths")
        if not isinstance(support_paths, list):
            errors.append(f"{label}.support_paths must be an array")
        else:
            for path_index, support_path in enumerate(support_paths):
                path_label = f"{label}.support_paths[{path_index}]"
                if not isinstance(support_path, dict):
                    errors.append(f"{path_label} must be an object")
                    continue
                _strict_keys(support_path, {"support_ref", "relation_ids"}, path_label, errors)
                if not isinstance(support_path.get("support_ref"), str):
                    errors.append(f"{path_label}.support_ref must be a string")
                _string_list(support_path.get("relation_ids"), f"{path_label}.relation_ids", errors, allow_empty=False)
            mapped = [path.get("support_ref") for path in support_paths if isinstance(path, dict)]
            if mapped != support_refs:
                errors.append(f"{label}.support_paths must map one-to-one to support_refs")
        if result.get("verdict") != "pass" and (support_refs or support_paths):
            errors.append(f"{label}.support_refs/support_paths are reserved for PASS")
        if not isinstance(result.get("analysis"), str):
            errors.append(f"{label}.analysis must be a string")
    return errors


def validate_finding_resolution_proposal(value: object) -> list[str]:
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    errors, item = _validate_base(
        value,
        artifact="finding-resolution-proposal",
        lifecycle="derived-replaceable",
        extra_keys={"resolution_id", "original_finding_id", "descendant_claims", "proposed_status", "mapping_reason", "retest_summary", "field_provenance"},
        schema_versions=(1, 2),
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "finding-resolution-proposal", errors)
    parent_artifacts = (
        {"retest-run": "resolution-retest-run"}
        if schema_version == 2
        else {"retest-run": "resolution-retest-run", "result-attempt": "resolution-result-attempt", "retest-results": "resolution-retest-results"}
    )
    _require_parent_roles(item, set(parent_artifacts), errors)
    _require_parent_artifacts(item, parent_artifacts, errors)
    for field in ("resolution_id", "original_finding_id", "mapping_reason"):
        if not _nonempty(item.get(field)):
            errors.append(f"{field} must be non-empty")
    descendants = _string_list(item.get("descendant_claims"), "descendant_claims", errors)
    if any(_VERSIONED_CLAIM.fullmatch(claim) is None for claim in descendants):
        errors.append("descendant_claims must be version-qualified")
    if item.get("proposed_status") not in RESOLUTION_STATUSES:
        errors.append(f"proposed_status must be one of {RESOLUTION_STATUSES}")
    if schema_version == 2 and (item.get("proposed_status") != "obsolete" or descendants):
        errors.append("schema v2 resolution proposal is reserved for removed Claims with no descendants")
    if schema_version == 1 and item.get("proposed_status") == "obsolete":
        errors.append("retest-derived schema v1 proposal cannot be obsolete")
    summary = item.get("retest_summary")
    if not isinstance(summary, dict):
        errors.append("retest_summary must be an object")
    else:
        _strict_keys(summary, set(FINDING_VERDICTS), "retest_summary", errors)
        if any(not isinstance(summary.get(key), int) or summary.get(key) < 0 for key in FINDING_VERDICTS):
            errors.append("retest_summary counts must be non-negative integers")
    provenance = item.get("field_provenance")
    expected = {"retest_summary": "deterministic", "proposed_status": "deterministic", "mapping_reason": "deterministic"}
    if not isinstance(provenance, dict):
        errors.append("field_provenance must be an object")
    else:
        _strict_keys(provenance, set(expected), "field_provenance", errors)
        for field, origin in expected.items():
            entry = provenance.get(field)
            if not isinstance(entry, dict):
                errors.append(f"field_provenance.{field} must be an object")
                continue
            _strict_keys(entry, {"origin", "source"}, f"field_provenance.{field}", errors)
            if entry.get("origin") != origin or not _nonempty(entry.get("source")):
                errors.append(f"field_provenance.{field} must be sourced deterministic provenance")
    return errors


def validate_finding_resolution_decision(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="finding-resolution-decision",
        lifecycle="immutable",
        extra_keys={"decision_id", "resolution_id", "decision", "final_status", "reason", "supersedes"},
    )
    if item is None:
        return errors
    _require_origin(item, {"human-confirmed"}, "finding-resolution-decision", errors)
    for field in ("decision_id", "resolution_id", "reason"):
        if not _nonempty(item.get(field)):
            errors.append(f"{field} must be non-empty")
    decision = item.get("decision")
    if decision not in RESOLUTION_DECISIONS:
        errors.append(f"decision must be one of {RESOLUTION_DECISIONS}")
    final_status = item.get("final_status")
    if decision == "reject" and final_status is not None:
        errors.append("reject requires final_status=null")
    if decision in {"confirm", "correct"} and final_status not in RESOLUTION_STATUSES:
        errors.append("confirm/correct requires a valid final_status")
    supersedes = item.get("supersedes")
    if supersedes is not None and not _digest(supersedes):
        errors.append("supersedes must be null or a SHA-256 digest")
    roles = {"resolution-proposal"}
    artifacts = {"resolution-proposal": "finding-resolution-proposal"}
    if supersedes is not None:
        roles.add("previous-decision")
        artifacts["previous-decision"] = "finding-resolution-decision"
    _require_parent_roles(item, roles, errors)
    _require_parent_artifacts(item, artifacts, errors)
    if supersedes is not None:
        parents = item.get("parents") if isinstance(item.get("parents"), list) else []
        previous = next((parent for parent in parents if isinstance(parent, dict) and parent.get("role") == "previous-decision"), None)
        if not isinstance(previous, dict) or previous.get("sha256") != supersedes:
            errors.append("previous-decision parent must match supersedes")
    return errors



__all__ = [name for name in globals() if not name.startswith("__")]
