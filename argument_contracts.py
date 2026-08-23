"""Strict product contracts for Argument Workbench artifacts.

The existing Argument IR and runner contracts intentionally remain in their
original modules.  This module defines the local-first lifecycle around those
artifacts: projects, document versions, immutable model attempts, append-only
human corrections, reviewed IR records, and the minimum future envelopes used
by findings, adjudications, revision actions, and claim lineage.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable


SCHEMA_VERSION = 1
ORIGINS = ("deterministic", "model-derived", "human-confirmed")
LIFECYCLES = ("immutable", "append-only", "derived-replaceable")
DECISIONS = ("accept", "reject", "defer")
REVISION_ACTION_TYPES = (
    "narrow_claim",
    "add_evidence",
    "add_qualification",
    "remove_claim",
    "restructure_argument",
    "clarify_concept",
    "verify_citation",
    "other",
)
LINEAGE_RELATIONS = (
    "unchanged",
    "modified",
    "split",
    "merged",
    "removed",
    "new",
    "uncertain",
)
LINEAGE_STATUSES = ("proposed", "human_confirmed", "rejected")
FINDING_VERDICTS = ("pass", "fail", "uncertain")
LENS_KINDS = ("rule", "perspective")
PERSPECTIVE_LENSES = (
    "methodological-individualism",
    "contrastive-explanation",
)
PERSPECTIVE_RESULT_STATUSES = ("complete", "partial", "blocked")
REVIEW_DEPTHS = ("core", "full")
REVIEW_SCOPES = ("thesis-chain", "claim", "claims", "all")
REVIEW_EXECUTION_STATUSES = (
    "evaluated",
    "blocked_missing_context",
    "routing_mismatch",
    "not_applicable",
)
REVIEW_RESULT_STATUSES = ("valid", "unusable")
TRIAGE_DECISIONS = ("acknowledge", "reject")
TRIAGE_ACTIONS = (
    "correct_ir",
    "add_context",
    "add_evidence",
    "acknowledge_not_applicable",
    "rerun_review",
    "other",
)
GATE_A_COMPARISONS = ("clearer", "same", "worse", "uncertain")
GATE_A_BURDENS = ("acceptable", "high", "uncertain")
GATE_A_DECISIONS = ("pass", "fail", "defer")
BASELINE_INTERACTION_MODES = ("fresh-session", "existing-session", "unknown")
BASELINE_PRIOR_CONTEXTS = (
    "none",
    "non-workbench",
    "workbench-exposed",
    "unknown",
)
BASELINE_MANUSCRIPT_DELIVERY = ("inline", "attachment", "other")
GATE_A_WORK_ACTIVITIES = (
    "ir-inspection",
    "finding-adjudication",
    "status-triage",
    "revision-planning",
    "manuscript-revision",
    "other",
)

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_VERSIONED_CLAIM = re.compile(r"V[1-9][0-9]*:C[1-9][0-9]*\Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _timestamp(value: object) -> bool:
    return isinstance(value, str) and _TIMESTAMP.fullmatch(value) is not None


def _safe_basename(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and Path(value).name == value
        and "/" not in value
        and "\\" not in value
        and value not in {".", ".."}
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.parts
        and all(part not in {"", ".", ".."} for part in path.parts)
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _strict_keys(
    value: dict[str, Any], expected: set[str], label: str, errors: list[str]
) -> None:
    if set(value) != expected:
        errors.append(f"{label} must contain exactly {sorted(expected)}")


def _string_list(
    value: object,
    label: str,
    errors: list[str],
    *,
    allow_empty: bool = True,
) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{label} must be an array")
        return []
    if not allow_empty and not value:
        errors.append(f"{label} must not be empty")
    if any(not _nonempty(item) for item in value):
        errors.append(f"{label} must contain non-empty strings")
        return []
    result = [str(item) for item in value]
    if len(result) != len(set(result)):
        errors.append(f"{label} must not contain duplicates")
    return result


def _validate_base(
    value: object,
    *,
    artifact: str,
    lifecycle: str,
    extra_keys: set[str],
    schema_versions: tuple[int, ...] = (SCHEMA_VERSION,),
) -> tuple[list[str], dict[str, Any] | None]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return [f"{artifact} must be a JSON object"], None
    _strict_keys(
        value,
        {
            "schema_version",
            "artifact",
            "artifact_id",
            "lifecycle",
            "provenance",
            "parents",
        }
        | extra_keys,
        artifact,
        errors,
    )
    if value.get("schema_version") not in schema_versions:
        errors.append(
            "schema_version must be one of "
            + ", ".join(str(version) for version in schema_versions)
        )
    if value.get("artifact") != artifact:
        errors.append(f"artifact must be {artifact}")
    if not _nonempty(value.get("artifact_id")):
        errors.append("artifact_id must be a non-empty string")
    if value.get("lifecycle") != lifecycle:
        errors.append(f"lifecycle must be {lifecycle}")

    provenance = value.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("provenance must be an object")
    else:
        _strict_keys(
            provenance, {"origin", "created_at", "producer"}, "provenance", errors
        )
        if provenance.get("origin") not in ORIGINS:
            errors.append(f"provenance.origin must be one of {ORIGINS}")
        if not _timestamp(provenance.get("created_at")):
            errors.append("provenance.created_at must be a timezone-aware ISO timestamp")
        if not _nonempty(provenance.get("producer")):
            errors.append("provenance.producer must be a non-empty string")

    parents = value.get("parents")
    if not isinstance(parents, list):
        errors.append("parents must be an array")
    else:
        roles: list[object] = []
        for index, parent in enumerate(parents):
            label = f"parents[{index}]"
            if not isinstance(parent, dict):
                errors.append(f"{label} must be an object")
                continue
            _strict_keys(parent, {"role", "artifact", "sha256"}, label, errors)
            roles.append(parent.get("role"))
            for key in ("role", "artifact"):
                if not _nonempty(parent.get(key)):
                    errors.append(f"{label}.{key} must be a non-empty string")
            if not _digest(parent.get("sha256")):
                errors.append(f"{label}.sha256 must be a lowercase SHA-256 digest")
        if len(roles) != len(set(roles)):
            errors.append("parent roles must be unique")
    return errors, value


def _require_parent_roles(
    value: dict[str, Any], roles: set[str], errors: list[str]
) -> None:
    parents = value.get("parents")
    if not isinstance(parents, list):
        return
    actual = {
        parent.get("role")
        for parent in parents
        if isinstance(parent, dict) and isinstance(parent.get("role"), str)
    }
    if actual != roles:
        errors.append(f"parent roles must be exactly {sorted(roles)}")


def _require_parent_artifacts(
    value: dict[str, Any], expected: dict[str, str], errors: list[str]
) -> None:
    parents = value.get("parents")
    if not isinstance(parents, list):
        return
    by_role = {
        parent.get("role"): parent
        for parent in parents
        if isinstance(parent, dict) and isinstance(parent.get("role"), str)
    }
    for role, artifact in expected.items():
        parent = by_role.get(role)
        if isinstance(parent, dict) and parent.get("artifact") != artifact:
            errors.append(f"parent {role!r} artifact must be {artifact}")


def _require_origin(
    value: dict[str, Any], allowed: set[str], label: str, errors: list[str]
) -> None:
    provenance = value.get("provenance")
    if isinstance(provenance, dict) and provenance.get("origin") not in allowed:
        errors.append(f"{label} provenance.origin must be one of {sorted(allowed)}")


def validate_project(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="argument-project",
        lifecycle="immutable",
        extra_keys={"project_id", "title"},
    )
    if item is None:
        return errors
    _require_origin(item, {"human-confirmed"}, "argument-project", errors)
    _require_parent_roles(item, set(), errors)
    for key in ("project_id", "title"):
        if not _nonempty(item.get(key)):
            errors.append(f"{key} must be a non-empty string")
    return errors


def validate_document(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="argument-document",
        lifecycle="immutable",
        extra_keys={"project_id", "document_id", "title"},
    )
    if item is None:
        return errors
    _require_origin(item, {"human-confirmed"}, "argument-document", errors)
    _require_parent_roles(item, {"project"}, errors)
    for key in ("project_id", "document_id", "title"):
        if not _nonempty(item.get(key)):
            errors.append(f"{key} must be a non-empty string")
    return errors


def validate_document_version(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="document-version",
        lifecycle="immutable",
        extra_keys={
            "project_id",
            "document_id",
            "version_id",
            "source",
            "parent_version",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"human-confirmed"}, "document-version", errors)
    for key in ("project_id", "document_id"):
        if not _nonempty(item.get(key)):
            errors.append(f"{key} must be a non-empty string")
    if not isinstance(item.get("version_id"), str) or re.fullmatch(
        r"V[1-9][0-9]*", str(item.get("version_id"))
    ) is None:
        errors.append("version_id must be V1..Vn")
    parent_version = item.get("parent_version")
    if parent_version is not None and (
        not isinstance(parent_version, str)
        or re.fullmatch(r"V[1-9][0-9]*", parent_version) is None
    ):
        errors.append("parent_version must be null or V1..Vn")
    _require_parent_roles(
        item,
        {"document"} if parent_version is None else {"document", "parent-version"},
        errors,
    )
    expected_parent_artifacts = {"document": "argument-document"}
    if parent_version is not None:
        expected_parent_artifacts["parent-version"] = "document-version"
    _require_parent_artifacts(item, expected_parent_artifacts, errors)
    source = item.get("source")
    if not isinstance(source, dict):
        errors.append("source must be an object")
    else:
        _strict_keys(source, {"name", "relative_path", "sha256"}, "source", errors)
        if not _safe_basename(source.get("name")):
            errors.append("source.name must be a safe basename")
        if not _safe_relative_path(source.get("relative_path")):
            errors.append("source.relative_path must stay inside the version directory")
        if not _digest(source.get("sha256")):
            errors.append("source.sha256 must be a lowercase SHA-256 digest")
    return errors


def validate_raw_ir_attempt(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="raw-ir-attempt",
        lifecycle="immutable",
        extra_keys={
            "project_id",
            "document_id",
            "version_id",
            "attempt_id",
            "collection",
            "prompt_sha256",
            "response",
            "validation",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"model-derived"}, "raw-ir-attempt", errors)
    _require_parent_roles(item, {"document-version"}, errors)
    for key in ("project_id", "document_id", "version_id", "attempt_id"):
        if not _nonempty(item.get(key)):
            errors.append(f"{key} must be a non-empty string")
    if not _digest(item.get("prompt_sha256")):
        errors.append("prompt_sha256 must be a lowercase SHA-256 digest")
    collection = item.get("collection")
    if not isinstance(collection, dict):
        errors.append("collection must be an object")
    else:
        _strict_keys(
            collection,
            {"method", "source_name", "producer_label"},
            "collection",
            errors,
        )
        if collection.get("method") not in {"file", "terminal-paste"}:
            errors.append("collection.method must be file or terminal-paste")
        if not _safe_basename(collection.get("source_name")):
            errors.append("collection.source_name must be a safe basename")
        producer = collection.get("producer_label")
        if producer is not None and not _nonempty(producer):
            errors.append("collection.producer_label must be null or a non-empty string")
    response = item.get("response")
    if not isinstance(response, dict):
        errors.append("response must be an object")
    else:
        _strict_keys(response, {"relative_path", "sha256"}, "response", errors)
        if not _safe_relative_path(response.get("relative_path")):
            errors.append("response.relative_path must stay inside the attempt directory")
        if not _digest(response.get("sha256")):
            errors.append("response.sha256 must be a lowercase SHA-256 digest")
    validation = item.get("validation")
    if not isinstance(validation, dict):
        errors.append("validation must be an object")
    else:
        _strict_keys(validation, {"status", "errors"}, "validation", errors)
        if validation.get("status") not in {"valid", "correctable", "unusable"}:
            errors.append("validation.status must be valid, correctable, or unusable")
        _string_list(validation.get("errors"), "validation.errors", errors)
    return errors


def validate_ir_correction(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="ir-correction",
        lifecycle="append-only",
        extra_keys={
            "project_id",
            "document_id",
            "version_id",
            "attempt_id",
            "correction_id",
            "operation",
            "reason",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"human-confirmed"}, "ir-correction", errors)
    parents = item.get("parents")
    expected = {"document-version", "raw-ir-attempt"}
    if isinstance(parents, list) and any(
        isinstance(parent, dict) and parent.get("role") == "previous-correction"
        for parent in parents
    ):
        expected.add("previous-correction")
    _require_parent_roles(item, expected, errors)
    for key in (
        "project_id",
        "document_id",
        "version_id",
        "attempt_id",
        "correction_id",
    ):
        if not _nonempty(item.get(key)):
            errors.append(f"{key} must be a non-empty string")
    if not isinstance(item.get("reason"), str):
        errors.append("reason must be a string")
    operation = item.get("operation")
    if not isinstance(operation, dict):
        errors.append("operation must be an object")
        return errors
    kind = operation.get("kind")
    allowed = {
        "update_node": {"kind", "target", "changes"},
        "add_node": {"kind", "node_kind", "node"},
        "remove_node": {"kind", "target"},
        "add_relation": {"kind", "relation"},
        "update_relation": {"kind", "target", "changes"},
        "remove_relation": {"kind", "target"},
        "set_unverified": {"kind", "items"},
        "revert_correction": {"kind", "target"},
    }
    if kind not in allowed:
        errors.append(f"operation.kind must be one of {tuple(allowed)}")
        return errors
    _strict_keys(operation, allowed[str(kind)], "operation", errors)
    if "target" in operation and not _nonempty(operation.get("target")):
        errors.append("operation.target must be a non-empty stable reference")
    if kind in {"update_node", "update_relation"}:
        changes = operation.get("changes")
        if not isinstance(changes, dict) or not changes:
            errors.append("operation.changes must be a non-empty object")
        elif kind == "update_node":
            allowed_node_fields = {
                "text",
                "source_quote",
                "types",
                "methods",
                "role",
                "extraction",
                "uncertainty",
                "kind",
                "locator",
            }
            if not set(changes).issubset(allowed_node_fields):
                errors.append("update_node changes contain unsupported or deterministic fields")
        elif not set(changes).issubset({"type", "from", "to"}):
            errors.append("update_relation changes may contain only type/from/to")
    if kind == "add_node":
        node_kind = operation.get("node_kind")
        if node_kind not in {
            "claim",
            "evidence",
            "assumption",
            "citation",
        }:
            errors.append("operation.node_kind is invalid")
        node = operation.get("node")
        if not isinstance(node, dict):
            errors.append("operation.node must be an object")
        elif node_kind in {"claim", "evidence", "assumption", "citation"}:
            expected_node_fields = {
                "claim": {
                    "text",
                    "source_quote",
                    "types",
                    "methods",
                    "role",
                    "extraction",
                    "uncertainty",
                },
                "evidence": {"text", "source_quote", "kind"},
                "assumption": {"text", "source_quote", "extraction", "uncertainty"},
                "citation": {"text", "source_quote", "locator"},
            }[str(node_kind)]
            if set(node) != expected_node_fields:
                errors.append(
                    f"add_node payload for {node_kind} must contain exactly {sorted(expected_node_fields)}"
                )
    if kind == "add_relation":
        relation = operation.get("relation")
        if not isinstance(relation, dict):
            errors.append("operation.relation must be an object")
        elif set(relation) != {"type", "from", "to"}:
            errors.append("operation.relation must contain exactly type/from/to")
        elif any(not _nonempty(relation.get(key)) for key in ("type", "from", "to")):
            errors.append("operation.relation fields must be non-empty strings")
    if kind == "set_unverified":
        _string_list(operation.get("items"), "operation.items", errors)
    return errors


def validate_reviewed_ir_record(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="reviewed-argument-ir",
        lifecycle="derived-replaceable",
        extra_keys={
            "project_id",
            "document_id",
            "version_id",
            "attempt_id",
            "payload",
            "correction_sha256s",
            "stable_ref_map",
            "field_provenance",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "reviewed-argument-ir", errors)
    parents = item.get("parents")
    expected = {"document-version", "raw-ir-attempt"}
    if isinstance(parents, list):
        expected.update(
            str(parent.get("role"))
            for parent in parents
            if isinstance(parent, dict)
            and isinstance(parent.get("role"), str)
            and str(parent.get("role")).startswith("correction-")
        )
    _require_parent_roles(item, expected, errors)
    for key in ("project_id", "document_id", "version_id", "attempt_id"):
        if not _nonempty(item.get(key)):
            errors.append(f"{key} must be a non-empty string")
    payload = item.get("payload")
    if not isinstance(payload, dict):
        errors.append("payload must be an object")
    else:
        _strict_keys(payload, {"relative_path", "sha256"}, "payload", errors)
        if not _safe_relative_path(payload.get("relative_path")):
            errors.append("payload.relative_path must stay inside reviewed-ir")
        if not _digest(payload.get("sha256")):
            errors.append("payload.sha256 must be a lowercase SHA-256 digest")
    hashes = _string_list(
        item.get("correction_sha256s"), "correction_sha256s", errors
    )
    for index, digest in enumerate(hashes):
        if not _digest(digest):
            errors.append(f"correction_sha256s[{index}] is not a SHA-256 digest")
    stable_map = item.get("stable_ref_map")
    if not isinstance(stable_map, dict) or any(
        not _nonempty(key) or not _nonempty(mapped)
        for key, mapped in stable_map.items()
    ):
        errors.append("stable_ref_map must map non-empty stable refs to reviewed IDs")
    field_provenance = item.get("field_provenance")
    if not isinstance(field_provenance, dict):
        errors.append("field_provenance must be an object")
    else:
        for field, provenance in field_provenance.items():
            if not _nonempty(field) or not isinstance(provenance, dict):
                errors.append("field_provenance entries must be named objects")
                continue
            _strict_keys(provenance, {"origin", "source"}, str(field), errors)
            if provenance.get("origin") not in ORIGINS:
                errors.append(f"{field}.origin is invalid")
            if not _nonempty(provenance.get("source")):
                errors.append(f"{field}.source must be a non-empty string")
    return errors


def _validate_bound_file(
    value: object, label: str, errors: list[str]
) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return
    _strict_keys(value, {"relative_path", "sha256"}, label, errors)
    if not _safe_relative_path(value.get("relative_path")):
        errors.append(f"{label}.relative_path must stay inside its artifact directory")
    if not _digest(value.get("sha256")):
        errors.append(f"{label}.sha256 must be a lowercase SHA-256 digest")


def _validate_rule_lens(value: object, label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return
    _strict_keys(value, {"kind", "id", "library_sha256"}, label, errors)
    if value.get("kind") != "rule":
        errors.append(f"{label}.kind must be rule")
    if not _nonempty(value.get("id")):
        errors.append(f"{label}.id must be a non-empty string")
    if not _digest(value.get("library_sha256")):
        errors.append(f"{label}.library_sha256 must be a lowercase SHA-256 digest")


def _validate_perspective_lens(
    value: object, label: str, errors: list[str]
) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return
    _strict_keys(value, {"kind", "id", "protocol_sha256"}, label, errors)
    if value.get("kind") != "perspective":
        errors.append(f"{label}.kind must be perspective")
    if value.get("id") not in PERSPECTIVE_LENSES:
        errors.append(f"{label}.id must be one of {PERSPECTIVE_LENSES}")
    if not _digest(value.get("protocol_sha256")):
        errors.append(f"{label}.protocol_sha256 must be a lowercase SHA-256 digest")


def _validate_review_scope(value: object, label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return
    _strict_keys(
        value,
        {"kind", "claim_ids", "selected_claim_ids"},
        label,
        errors,
    )
    if value.get("kind") not in REVIEW_SCOPES:
        errors.append(f"{label}.kind must be one of {REVIEW_SCOPES}")
    _string_list(value.get("claim_ids"), f"{label}.claim_ids", errors)
    selected = _string_list(
        value.get("selected_claim_ids"),
        f"{label}.selected_claim_ids",
        errors,
        allow_empty=False,
    )
    for index, claim_id in enumerate(selected):
        if re.fullmatch(r"C[1-9][0-9]*", claim_id) is None:
            errors.append(
                f"{label}.selected_claim_ids[{index}] must be an unversioned Claim ID"
            )


def validate_rule_review_run(value: object) -> list[str]:
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    extra_keys = {
        "review_id",
        "project_id",
        "document_id",
        "version_id",
        "lens",
        "depth",
        "reviewed_ir_record",
        "target_ir",
        "check_library",
        "plan",
        "prompt",
    }
    if schema_version in {2, 3}:
        extra_keys.add("review_scope")
    errors, item = _validate_base(
        value,
        artifact="rule-review-run",
        lifecycle="immutable",
        extra_keys=extra_keys,
        schema_versions=(1, 2, 3),
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "rule-review-run", errors)
    _require_parent_roles(item, {"reviewed-ir", "target-ir", "check-library"}, errors)
    _require_parent_artifacts(
        item,
        {
            "reviewed-ir": "reviewed-argument-ir",
            "target-ir": "argument-ir",
            "check-library": "argument-check-library",
        },
        errors,
    )
    for key in ("review_id", "project_id", "document_id", "version_id"):
        if not _nonempty(item.get(key)):
            errors.append(f"{key} must be a non-empty string")
    if not isinstance(item.get("review_id"), str) or re.fullmatch(
        r"RV[1-9][0-9]*", str(item.get("review_id"))
    ) is None:
        errors.append("review_id must be RV1..RVn")
    if not isinstance(item.get("version_id"), str) or re.fullmatch(
        r"V[1-9][0-9]*", str(item.get("version_id"))
    ) is None:
        errors.append("version_id must be V1..Vn")
    _validate_rule_lens(item.get("lens"), "lens", errors)
    if item.get("depth") not in REVIEW_DEPTHS:
        errors.append(f"depth must be one of {REVIEW_DEPTHS}")
    if schema_version in {2, 3}:
        review_scope = item.get("review_scope")
        if not isinstance(review_scope, dict):
            errors.append("review_scope must be an object")
        else:
            _strict_keys(
                review_scope,
                {"kind", "claim_ids", "selected_claim_ids"},
                "review_scope",
                errors,
            )
            if review_scope.get("kind") not in REVIEW_SCOPES:
                errors.append(f"review_scope.kind must be one of {REVIEW_SCOPES}")
            _string_list(
                review_scope.get("claim_ids"),
                "review_scope.claim_ids",
                errors,
            )
            _string_list(
                review_scope.get("selected_claim_ids"),
                "review_scope.selected_claim_ids",
                errors,
                allow_empty=False,
            )
    for field in (
        "reviewed_ir_record",
        "target_ir",
        "check_library",
        "plan",
        "prompt",
    ):
        _validate_bound_file(item.get(field), field, errors)
    return errors


def validate_review_result_attempt(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="review-result-attempt",
        lifecycle="immutable",
        extra_keys={
            "review_id",
            "attempt_id",
            "collection",
            "response",
            "validation",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"model-derived"}, "review-result-attempt", errors)
    _require_parent_roles(item, {"review-run"}, errors)
    _require_parent_artifacts(item, {"review-run": "rule-review-run"}, errors)
    if not isinstance(item.get("review_id"), str) or re.fullmatch(
        r"RV[1-9][0-9]*", str(item.get("review_id"))
    ) is None:
        errors.append("review_id must be RV1..RVn")
    if not isinstance(item.get("attempt_id"), str) or re.fullmatch(
        r"attempt-[0-9]{4}", str(item.get("attempt_id"))
    ) is None:
        errors.append("attempt_id must be attempt-NNNN")
    collection = item.get("collection")
    if not isinstance(collection, dict):
        errors.append("collection must be an object")
    else:
        _strict_keys(
            collection,
            {"method", "source_name", "producer_label"},
            "collection",
            errors,
        )
        if collection.get("method") not in {"file", "terminal-paste"}:
            errors.append("collection.method must be file or terminal-paste")
        if not _safe_basename(collection.get("source_name")):
            errors.append("collection.source_name must be a safe basename")
        producer = collection.get("producer_label")
        if producer is not None and not _nonempty(producer):
            errors.append("collection.producer_label must be null or a non-empty string")
    _validate_bound_file(item.get("response"), "response", errors)
    validation = item.get("validation")
    if not isinstance(validation, dict):
        errors.append("validation must be an object")
    else:
        _strict_keys(validation, {"status", "errors"}, "validation", errors)
        if validation.get("status") not in REVIEW_RESULT_STATUSES:
            errors.append(f"validation.status must be one of {REVIEW_RESULT_STATUSES}")
        validation_errors = _string_list(
            validation.get("errors"), "validation.errors", errors
        )
        if validation.get("status") == "valid" and validation_errors:
            errors.append("valid review results require an empty validation.errors array")
        if validation.get("status") == "unusable" and not validation_errors:
            errors.append("unusable review results require concrete validation errors")
    return errors


def validate_perspective_lens_protocol(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="perspective-lens-protocol",
        lifecycle="immutable",
        extra_keys={"lens", "legacy_protocol", "protocol"},
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "perspective-lens-protocol", errors)
    _require_parent_roles(item, set(), errors)
    lens = item.get("lens")
    if not isinstance(lens, dict):
        errors.append("lens must be an object")
    else:
        _strict_keys(lens, {"kind", "id"}, "lens", errors)
        if lens.get("kind") != "perspective":
            errors.append("lens.kind must be perspective")
        if lens.get("id") not in PERSPECTIVE_LENSES:
            errors.append(f"lens.id must be one of {PERSPECTIVE_LENSES}")
    if item.get("legacy_protocol") not in {
        "critic-individualist",
        "critic-contrastivist",
    }:
        errors.append(
            "legacy_protocol must be critic-individualist or critic-contrastivist"
        )
    expected_protocol = {
        "methodological-individualism": "critic-individualist",
        "contrastive-explanation": "critic-contrastivist",
    }
    if isinstance(lens, dict) and expected_protocol.get(str(lens.get("id"))) != item.get(
        "legacy_protocol"
    ):
        errors.append("lens.id and legacy_protocol must identify the same framework")
    _validate_bound_file(item.get("protocol"), "protocol", errors)
    return errors


def validate_perspective_review_run(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="perspective-review-run",
        lifecycle="immutable",
        extra_keys={
            "review_id",
            "project_id",
            "document_id",
            "version_id",
            "lens",
            "review_scope",
            "reviewed_ir_record",
            "target_ir",
            "protocol_record",
            "protocol",
            "plan",
            "prompt",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "perspective-review-run", errors)
    _require_parent_roles(
        item, {"reviewed-ir", "target-ir", "protocol", "plan"}, errors
    )
    _require_parent_artifacts(
        item,
        {
            "reviewed-ir": "reviewed-argument-ir",
            "target-ir": "argument-ir",
            "protocol": "perspective-lens-protocol",
            "plan": "perspective-review-plan",
        },
        errors,
    )
    if not isinstance(item.get("review_id"), str) or re.fullmatch(
        r"PV[1-9][0-9]*", str(item.get("review_id"))
    ) is None:
        errors.append("review_id must be PV1..PVn")
    for field in ("project_id", "document_id"):
        if not _nonempty(item.get(field)):
            errors.append(f"{field} must be a non-empty string")
    if not isinstance(item.get("version_id"), str) or re.fullmatch(
        r"V[1-9][0-9]*", str(item.get("version_id"))
    ) is None:
        errors.append("version_id must be V1..Vn")
    _validate_perspective_lens(item.get("lens"), "lens", errors)
    _validate_review_scope(item.get("review_scope"), "review_scope", errors)
    for field in (
        "reviewed_ir_record",
        "target_ir",
        "protocol_record",
        "protocol",
        "plan",
        "prompt",
    ):
        _validate_bound_file(item.get(field), field, errors)
    lens = item.get("lens")
    protocol = item.get("protocol")
    if (
        isinstance(lens, dict)
        and isinstance(protocol, dict)
        and lens.get("protocol_sha256") != protocol.get("sha256")
    ):
        errors.append("lens.protocol_sha256 must equal protocol.sha256")
    return errors


def validate_perspective_review_plan(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="perspective-review-plan",
        lifecycle="immutable",
        extra_keys={"review_id", "lens", "review_scope"},
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "perspective-review-plan", errors)
    _require_parent_roles(item, {"target-ir", "protocol"}, errors)
    _require_parent_artifacts(
        item,
        {
            "target-ir": "argument-ir",
            "protocol": "perspective-lens-protocol",
        },
        errors,
    )
    if not isinstance(item.get("review_id"), str) or re.fullmatch(
        r"PV[1-9][0-9]*", str(item.get("review_id"))
    ) is None:
        errors.append("review_id must be PV1..PVn")
    _validate_perspective_lens(item.get("lens"), "lens", errors)
    _validate_review_scope(item.get("review_scope"), "review_scope", errors)
    return errors


def validate_perspective_result_attempt(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="perspective-result-attempt",
        lifecycle="immutable",
        extra_keys={
            "review_id",
            "attempt_id",
            "collection",
            "response",
            "validation",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"model-derived"}, "perspective-result-attempt", errors)
    _require_parent_roles(item, {"review-run"}, errors)
    _require_parent_artifacts(item, {"review-run": "perspective-review-run"}, errors)
    if not isinstance(item.get("review_id"), str) or re.fullmatch(
        r"PV[1-9][0-9]*", str(item.get("review_id"))
    ) is None:
        errors.append("review_id must be PV1..PVn")
    if not isinstance(item.get("attempt_id"), str) or re.fullmatch(
        r"attempt-[0-9]{4}", str(item.get("attempt_id"))
    ) is None:
        errors.append("attempt_id must be attempt-NNNN")
    collection = item.get("collection")
    if not isinstance(collection, dict):
        errors.append("collection must be an object")
    else:
        _strict_keys(
            collection,
            {"method", "source_name", "producer_label"},
            "collection",
            errors,
        )
        if collection.get("method") not in {"file", "terminal-paste"}:
            errors.append("collection.method must be file or terminal-paste")
        if not _safe_basename(collection.get("source_name")):
            errors.append("collection.source_name must be a safe basename")
        producer = collection.get("producer_label")
        if producer is not None and not _nonempty(producer):
            errors.append("collection.producer_label must be null or non-empty")
    _validate_bound_file(item.get("response"), "response", errors)
    validation = item.get("validation")
    if not isinstance(validation, dict):
        errors.append("validation must be an object")
    else:
        _strict_keys(validation, {"status", "errors"}, "validation", errors)
        if validation.get("status") not in REVIEW_RESULT_STATUSES:
            errors.append(f"validation.status must be one of {REVIEW_RESULT_STATUSES}")
        validation_errors = _string_list(
            validation.get("errors"), "validation.errors", errors
        )
        if validation.get("status") == "valid" and validation_errors:
            errors.append("valid perspective results require no validation errors")
        if validation.get("status") == "unusable" and not validation_errors:
            errors.append("unusable perspective results require concrete errors")
    return errors


def validate_perspective_lens_results(value: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["perspective-lens-results must be a JSON object"]
    _strict_keys(
        value,
        {"schema_version", "artifact", "source", "status", "unverified", "results"},
        "perspective-lens-results",
        errors,
    )
    if value.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if value.get("artifact") != "perspective-lens-results":
        errors.append("artifact must be perspective-lens-results")
    source = value.get("source")
    if not isinstance(source, dict):
        errors.append("source must be an object")
    else:
        _strict_keys(
            source,
            {"plan_sha256", "target_ir_sha256", "protocol_sha256"},
            "source",
            errors,
        )
        for field in ("plan_sha256", "target_ir_sha256", "protocol_sha256"):
            if not _digest(source.get(field)):
                errors.append(f"source.{field} must be a lowercase SHA-256 digest")
    status = value.get("status")
    if status not in PERSPECTIVE_RESULT_STATUSES:
        errors.append(f"status must be one of {PERSPECTIVE_RESULT_STATUSES}")
    unverified = _string_list(value.get("unverified"), "unverified", errors)
    if status == "complete" and unverified:
        errors.append("complete perspective results require unverified=[]")
    if status in {"partial", "blocked"} and not unverified:
        errors.append(f"{status} perspective results require concrete unverified items")
    results = value.get("results")
    if not isinstance(results, list):
        errors.append("results must be an array")
        return errors
    result_ids: list[str] = []
    targets: list[str] = []
    expected_keys = {
        "result_id",
        "target_claim",
        "verdict",
        "reason",
        "basis_refs",
        "framework_analysis",
        "consequence",
    }
    for index, result in enumerate(results):
        label = f"results[{index}]"
        if not isinstance(result, dict):
            errors.append(f"{label} must be an object")
            continue
        _strict_keys(result, expected_keys, label, errors)
        expected_id = f"P{index + 1}"
        if result.get("result_id") != expected_id:
            errors.append(f"{label}.result_id must be {expected_id}")
        result_ids.append(str(result.get("result_id")))
        target = result.get("target_claim")
        if not isinstance(target, str) or re.fullmatch(r"C[1-9][0-9]*", target) is None:
            errors.append(f"{label}.target_claim must be an unversioned Claim ID")
        else:
            targets.append(target)
        if result.get("verdict") not in FINDING_VERDICTS:
            errors.append(f"{label}.verdict must be one of {FINDING_VERDICTS}")
        for field in ("reason", "framework_analysis"):
            if not _nonempty(result.get(field)):
                errors.append(f"{label}.{field} must be a non-empty string")
        basis_refs = _string_list(
            result.get("basis_refs"), f"{label}.basis_refs", errors, allow_empty=False
        )
        for basis_index, reference in enumerate(basis_refs):
            if re.fullmatch(r"[CEAZ][1-9][0-9]*", reference) is None:
                errors.append(
                    f"{label}.basis_refs[{basis_index}] must identify a Claim, Evidence, Assumption, or Citation"
                )
        if isinstance(target, str) and target not in basis_refs:
            errors.append(f"{label}.basis_refs must include target_claim")
        consequence = result.get("consequence")
        if not isinstance(consequence, str):
            errors.append(f"{label}.consequence must be a string")
        elif result.get("verdict") in {"fail", "uncertain"} and not consequence.strip():
            errors.append(f"{label}.actionable verdict requires a consequence")
        elif result.get("verdict") == "pass" and consequence:
            errors.append(f"{label}.pass requires an empty consequence")
    if len(result_ids) != len(set(result_ids)):
        errors.append("result IDs must not contain duplicates")
    if len(targets) != len(set(targets)):
        errors.append("perspective results allow at most one judgment per target Claim")
    return errors


def validate_perspective_review_index(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="perspective-review-index",
        lifecycle="derived-replaceable",
        extra_keys={
            "review_id",
            "attempt_id",
            "version_id",
            "lens",
            "run_status",
            "unverified",
            "summary",
            "outcomes",
            "view",
            "field_provenance",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "perspective-review-index", errors)
    parents = item.get("parents")
    expected_roles = {"review-run", "result-attempt", "lens-result"}
    if isinstance(parents, list):
        expected_roles.update(
            str(parent.get("role"))
            for parent in parents
            if isinstance(parent, dict)
            and isinstance(parent.get("role"), str)
            and str(parent.get("role")).startswith("finding-")
        )
    _require_parent_roles(item, expected_roles, errors)
    parent_artifacts = {
        "review-run": "perspective-review-run",
        "result-attempt": "perspective-result-attempt",
        "lens-result": "perspective-lens-results",
    }
    parent_artifacts.update(
        {
            role: "argument-finding"
            for role in expected_roles
            if role.startswith("finding-")
        }
    )
    _require_parent_artifacts(item, parent_artifacts, errors)
    if not isinstance(item.get("review_id"), str) or re.fullmatch(
        r"PV[1-9][0-9]*", str(item.get("review_id"))
    ) is None:
        errors.append("review_id must be PV1..PVn")
    if not isinstance(item.get("attempt_id"), str) or re.fullmatch(
        r"attempt-[0-9]{4}", str(item.get("attempt_id"))
    ) is None:
        errors.append("attempt_id must be attempt-NNNN")
    if not isinstance(item.get("version_id"), str) or re.fullmatch(
        r"V[1-9][0-9]*", str(item.get("version_id"))
    ) is None:
        errors.append("version_id must be V1..Vn")
    _validate_perspective_lens(item.get("lens"), "lens", errors)
    if item.get("run_status") not in PERSPECTIVE_RESULT_STATUSES:
        errors.append(f"run_status must be one of {PERSPECTIVE_RESULT_STATUSES}")
    unverified = _string_list(item.get("unverified"), "unverified", errors)
    if item.get("run_status") == "complete" and unverified:
        errors.append("complete Perspective Review requires unverified=[]")
    if item.get("run_status") in {"partial", "blocked"} and not unverified:
        errors.append(
            f"{item.get('run_status')} Perspective Review requires unverified items"
        )
    summary = item.get("summary")
    if not isinstance(summary, dict):
        errors.append("summary must be an object")
    else:
        _strict_keys(summary, set(FINDING_VERDICTS), "summary", errors)
        if any(
            not isinstance(summary.get(key), int) or summary.get(key) < 0
            for key in FINDING_VERDICTS
        ):
            errors.append("summary counts must be non-negative integers")
    outcomes = item.get("outcomes")
    counted = {key: 0 for key in FINDING_VERDICTS}
    finding_ids: list[str] = []
    if not isinstance(outcomes, list):
        errors.append("outcomes must be an array")
    else:
        expected_keys = {
            "result_id",
            "target_claim",
            "verdict",
            "reason",
            "basis_refs",
            "framework_analysis",
            "consequence",
            "finding_id",
        }
        for index, outcome in enumerate(outcomes):
            label = f"outcomes[{index}]"
            if not isinstance(outcome, dict):
                errors.append(f"{label} must be an object")
                continue
            _strict_keys(outcome, expected_keys, label, errors)
            for field in ("reason", "framework_analysis"):
                if not _nonempty(outcome.get(field)):
                    errors.append(f"{label}.{field} must be non-empty")
            if outcome.get("result_id") != f"P{index + 1}":
                errors.append(f"{label}.result_id must be P{index + 1}")
            target = outcome.get("target_claim")
            if not isinstance(target, str) or _VERSIONED_CLAIM.fullmatch(target) is None:
                errors.append(f"{label}.target_claim must be version-qualified")
            verdict = outcome.get("verdict")
            if verdict not in FINDING_VERDICTS:
                errors.append(f"{label}.verdict must be one of {FINDING_VERDICTS}")
            else:
                counted[str(verdict)] += 1
            refs = _string_list(
                outcome.get("basis_refs"), f"{label}.basis_refs", errors, allow_empty=False
            )
            if any(re.fullmatch(r"V[1-9][0-9]*:[CEAZ][1-9][0-9]*", ref) is None for ref in refs):
                errors.append(f"{label}.basis_refs must be version-qualified node IDs")
            if isinstance(target, str) and target not in refs:
                errors.append(f"{label}.basis_refs must include target_claim")
            consequence = outcome.get("consequence")
            if not isinstance(consequence, str):
                errors.append(f"{label}.consequence must be a string")
            elif verdict in {"fail", "uncertain"} and not consequence.strip():
                errors.append(f"{label}.actionable verdict requires a consequence")
            elif verdict == "pass" and consequence:
                errors.append(f"{label}.pass requires an empty consequence")
            finding_id = outcome.get("finding_id")
            if verdict == "pass" and finding_id is not None:
                errors.append(f"{label}.finding_id must be null for pass")
            if verdict in {"fail", "uncertain"}:
                if not _nonempty(finding_id):
                    errors.append(f"{label}.finding_id is required for actionable verdicts")
                else:
                    finding_ids.append(str(finding_id))
    if isinstance(summary, dict) and summary != counted:
        errors.append("summary must equal outcomes")
    if item.get("run_status") == "blocked" and outcomes:
        errors.append("blocked Perspective Review must not contain outcomes")
    if len(finding_ids) != len(set(finding_ids)):
        errors.append("finding IDs must not contain duplicates")
    _validate_bound_file(item.get("view"), "view", errors)
    provenance = item.get("field_provenance")
    expected_provenance = {
        "outcomes": "model-derived",
        "run_status": "model-derived",
        "unverified": "model-derived",
        "finding_id": "deterministic",
        "summary": "deterministic",
        "view": "deterministic",
    }
    if not isinstance(provenance, dict):
        errors.append("field_provenance must be an object")
    else:
        _strict_keys(
            provenance, set(expected_provenance), "field_provenance", errors
        )
        for field, origin in expected_provenance.items():
            entry = provenance.get(field)
            if not isinstance(entry, dict):
                errors.append(f"field_provenance.{field} must be an object")
                continue
            _strict_keys(entry, {"origin", "source"}, f"field_provenance.{field}", errors)
            if entry.get("origin") != origin:
                errors.append(f"field_provenance.{field}.origin must be {origin}")
            if not _nonempty(entry.get("source")):
                errors.append(f"field_provenance.{field}.source must be non-empty")
    return errors


def validate_direct_review_baseline(value: object) -> list[str]:
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    extra_keys = {
        "baseline_id",
        "project_id",
        "document_id",
        "version_id",
        "model",
        "timing",
        "source",
        "prompt",
        "response",
        "collection",
        "field_provenance",
    }
    if schema_version == 2:
        extra_keys.add("conditions")
    errors, item = _validate_base(
        value,
        artifact="direct-review-baseline",
        lifecycle="immutable",
        extra_keys=extra_keys,
        schema_versions=(1, 2),
    )
    if item is None:
        return errors
    _require_origin(item, {"model-derived"}, "direct-review-baseline", errors)
    _require_parent_roles(item, {"document-version"}, errors)
    _require_parent_artifacts(
        item, {"document-version": "document-version"}, errors
    )
    if not isinstance(item.get("baseline_id"), str) or re.fullmatch(
        r"DB[1-9][0-9]*", str(item.get("baseline_id"))
    ) is None:
        errors.append("baseline_id must be DB1..DBn")
    for key in ("project_id", "document_id", "version_id"):
        if not _nonempty(item.get(key)):
            errors.append(f"{key} must be a non-empty string")
    if not isinstance(item.get("version_id"), str) or re.fullmatch(
        r"V[1-9][0-9]*", str(item.get("version_id"))
    ) is None:
        errors.append("version_id must be V1..Vn")
    model = item.get("model")
    if not isinstance(model, dict):
        errors.append("model must be an object")
    else:
        model_keys = (
            {"label"}
            if schema_version == 1
            else {"label", "provider", "model_id"}
        )
        _strict_keys(model, model_keys, "model", errors)
        if not _nonempty(model.get("label")):
            errors.append("model.label must be a non-empty human-supplied label")
        if schema_version == 2:
            for key in ("provider", "model_id"):
                if not _nonempty(model.get(key)):
                    errors.append(f"model.{key} must be a non-empty human-supplied value")
    timing = item.get("timing")
    if not isinstance(timing, dict):
        errors.append("timing must be an object")
    else:
        _strict_keys(
            timing,
            {"started_at", "completed_at", "elapsed_milliseconds"},
            "timing",
            errors,
        )
        for key in ("started_at", "completed_at"):
            if not _timestamp(timing.get(key)):
                errors.append(f"timing.{key} must be timezone-aware ISO time")
        elapsed = timing.get("elapsed_milliseconds")
        if not isinstance(elapsed, int) or isinstance(elapsed, bool) or elapsed < 0:
            errors.append("timing.elapsed_milliseconds must be a non-negative integer")
        if all(_timestamp(timing.get(key)) for key in ("started_at", "completed_at")):
            started = datetime.fromisoformat(str(timing["started_at"]).replace("Z", "+00:00"))
            completed = datetime.fromisoformat(str(timing["completed_at"]).replace("Z", "+00:00"))
            expected = round((completed - started).total_seconds() * 1000)
            if expected < 0:
                errors.append("timing.completed_at must not precede started_at")
            elif elapsed != expected:
                errors.append("timing.elapsed_milliseconds must be derived from timestamps")
    for field in ("source", "prompt", "response"):
        _validate_bound_file(item.get(field), field, errors)
    collection = item.get("collection")
    if not isinstance(collection, dict):
        errors.append("collection must be an object")
    else:
        _strict_keys(
            collection,
            {"method", "prompt_source_name", "response_source_name"},
            "collection",
            errors,
        )
        if collection.get("method") != "file":
            errors.append("collection.method must be file")
        for key in ("prompt_source_name", "response_source_name"):
            if not _nonempty(collection.get(key)):
                errors.append(f"collection.{key} must be non-empty")
    if schema_version == 2:
        conditions = item.get("conditions")
        if not isinstance(conditions, dict):
            errors.append("conditions must be an object")
        else:
            _strict_keys(
                conditions,
                {
                    "interaction_mode",
                    "prior_context",
                    "manuscript_delivery",
                    "full_manuscript_confirmed",
                },
                "conditions",
                errors,
            )
            if conditions.get("interaction_mode") not in BASELINE_INTERACTION_MODES:
                errors.append(
                    f"conditions.interaction_mode must be one of {BASELINE_INTERACTION_MODES}"
                )
            if conditions.get("prior_context") not in BASELINE_PRIOR_CONTEXTS:
                errors.append(
                    f"conditions.prior_context must be one of {BASELINE_PRIOR_CONTEXTS}"
                )
            if (
                conditions.get("manuscript_delivery")
                not in BASELINE_MANUSCRIPT_DELIVERY
            ):
                errors.append(
                    "conditions.manuscript_delivery must be one of "
                    f"{BASELINE_MANUSCRIPT_DELIVERY}"
                )
            if not isinstance(conditions.get("full_manuscript_confirmed"), bool):
                errors.append("conditions.full_manuscript_confirmed must be boolean")
    field_provenance = item.get("field_provenance")
    expected_fields = (
        {"source", "prompt", "response", "model", "timing"}
        if schema_version == 1
        else {
            "source",
            "prompt",
            "response",
            "model",
            "timestamps",
            "elapsed_milliseconds",
            "conditions",
        }
    )
    if not isinstance(field_provenance, dict):
        errors.append("field_provenance must be an object")
    else:
        _strict_keys(
            field_provenance, expected_fields, "field_provenance", errors
        )
        expected_origins = (
            {
                "source": "deterministic",
                "prompt": "human-confirmed",
                "response": "model-derived",
                "model": "human-confirmed",
                "timing": "deterministic",
            }
            if schema_version == 1
            else {
                "source": "deterministic",
                "prompt": "human-confirmed",
                "response": "model-derived",
                "model": "human-confirmed",
                "timestamps": "human-confirmed",
                "elapsed_milliseconds": "deterministic",
                "conditions": "human-confirmed",
            }
        )
        for field, origin in expected_origins.items():
            provenance = field_provenance.get(field)
            if not isinstance(provenance, dict):
                errors.append(f"field_provenance.{field} must be an object")
                continue
            _strict_keys(
                provenance,
                {"origin", "source"},
                f"field_provenance.{field}",
                errors,
            )
            if provenance.get("origin") != origin:
                errors.append(f"field_provenance.{field}.origin must be {origin}")
            if not _nonempty(provenance.get("source")):
                errors.append(f"field_provenance.{field}.source must be non-empty")
    return errors


def _validate_gate_session_identity(
    item: dict[str, Any], errors: list[str]
) -> None:
    if not isinstance(item.get("session_id"), str) or re.fullmatch(
        r"GS[1-9][0-9]*", str(item.get("session_id"))
    ) is None:
        errors.append("session_id must be GS1..GSn")
    for key in ("project_id", "document_id"):
        if not _nonempty(item.get(key)):
            errors.append(f"{key} must be a non-empty string")
    if not isinstance(item.get("version_id"), str) or re.fullmatch(
        r"V[1-9][0-9]*", str(item.get("version_id"))
    ) is None:
        errors.append("version_id must be V1..Vn")
    if item.get("activity") not in GATE_A_WORK_ACTIVITIES:
        errors.append(f"activity must be one of {GATE_A_WORK_ACTIVITIES}")
    if not isinstance(item.get("note"), str):
        errors.append("note must be a string")


def _validate_gate_session_field_provenance(
    value: object, *, completed: bool, errors: list[str]
) -> None:
    expected = {"activity", "note", "timing" if completed else "started_at"}
    if not isinstance(value, dict):
        errors.append("field_provenance must be an object")
        return
    _strict_keys(value, expected, "field_provenance", errors)
    for field in expected:
        provenance = value.get(field)
        if not isinstance(provenance, dict):
            errors.append(f"field_provenance.{field} must be an object")
            continue
        _strict_keys(
            provenance,
            {"origin", "source"},
            f"field_provenance.{field}",
            errors,
        )
        expected_origin = "deterministic" if field in {"started_at", "timing"} else "human-confirmed"
        if provenance.get("origin") != expected_origin:
            errors.append(
                f"field_provenance.{field}.origin must be {expected_origin}"
            )
        if not _nonempty(provenance.get("source")):
            errors.append(f"field_provenance.{field}.source must be non-empty")


def validate_gate_a_session_start(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="gate-a-session-start",
        lifecycle="immutable",
        extra_keys={
            "session_id",
            "project_id",
            "document_id",
            "version_id",
            "activity",
            "note",
            "started_at",
            "field_provenance",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"human-confirmed"}, "gate-a-session-start", errors)
    _require_parent_roles(item, {"document-version"}, errors)
    _require_parent_artifacts(
        item, {"document-version": "document-version"}, errors
    )
    _validate_gate_session_identity(item, errors)
    if not _timestamp(item.get("started_at")):
        errors.append("started_at must be a timezone-aware ISO timestamp")
    provenance = item.get("provenance")
    if (
        isinstance(provenance, dict)
        and _timestamp(item.get("started_at"))
        and provenance.get("created_at") != item.get("started_at")
    ):
        errors.append("provenance.created_at must equal started_at")
    _validate_gate_session_field_provenance(
        item.get("field_provenance"), completed=False, errors=errors
    )
    return errors


def validate_gate_a_work_session(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="gate-a-work-session",
        lifecycle="immutable",
        extra_keys={
            "session_id",
            "project_id",
            "document_id",
            "version_id",
            "activity",
            "note",
            "timing",
            "field_provenance",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"human-confirmed"}, "gate-a-work-session", errors)
    _require_parent_roles(item, {"document-version", "session-start"}, errors)
    _require_parent_artifacts(
        item,
        {
            "document-version": "document-version",
            "session-start": "gate-a-session-start",
        },
        errors,
    )
    _validate_gate_session_identity(item, errors)
    timing = item.get("timing")
    if not isinstance(timing, dict):
        errors.append("timing must be an object")
    else:
        _strict_keys(
            timing,
            {"started_at", "completed_at", "elapsed_milliseconds"},
            "timing",
            errors,
        )
        for key in ("started_at", "completed_at"):
            if not _timestamp(timing.get(key)):
                errors.append(f"timing.{key} must be timezone-aware ISO time")
        elapsed = timing.get("elapsed_milliseconds")
        if not isinstance(elapsed, int) or isinstance(elapsed, bool) or elapsed < 0:
            errors.append("timing.elapsed_milliseconds must be non-negative")
        if all(_timestamp(timing.get(key)) for key in ("started_at", "completed_at")):
            started = datetime.fromisoformat(str(timing["started_at"]).replace("Z", "+00:00"))
            completed = datetime.fromisoformat(str(timing["completed_at"]).replace("Z", "+00:00"))
            expected = round((completed - started).total_seconds() * 1000)
            if expected < 0:
                errors.append("timing.completed_at must not precede started_at")
            elif elapsed != expected:
                errors.append("timing.elapsed_milliseconds must be derived from timestamps")
        provenance = item.get("provenance")
        if (
            isinstance(provenance, dict)
            and _timestamp(timing.get("completed_at"))
            and provenance.get("created_at") != timing.get("completed_at")
        ):
            errors.append("provenance.created_at must equal timing.completed_at")
    _validate_gate_session_field_provenance(
        item.get("field_provenance"), completed=True, errors=errors
    )
    return errors


def validate_gate_a_session_abandonment(value: object) -> list[str]:
    """Validate an immutable close event that must not count as completed work."""
    errors, item = _validate_base(
        value,
        artifact="gate-a-session-abandonment",
        lifecycle="immutable",
        extra_keys={
            "session_id",
            "project_id",
            "document_id",
            "version_id",
            "activity",
            "note",
            "reason",
            "timing",
            "field_provenance",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"human-confirmed"}, "gate-a-session-abandonment", errors)
    _require_parent_roles(item, {"document-version", "session-start"}, errors)
    _require_parent_artifacts(
        item,
        {
            "document-version": "document-version",
            "session-start": "gate-a-session-start",
        },
        errors,
    )
    _validate_gate_session_identity(item, errors)
    if not _nonempty(item.get("reason")):
        errors.append("reason must be a non-empty string")
    timing = item.get("timing")
    if not isinstance(timing, dict):
        errors.append("timing must be an object")
    else:
        _strict_keys(
            timing,
            {"started_at", "abandoned_at", "elapsed_milliseconds"},
            "timing",
            errors,
        )
        for key in ("started_at", "abandoned_at"):
            if not _timestamp(timing.get(key)):
                errors.append(f"timing.{key} must be timezone-aware ISO time")
        elapsed = timing.get("elapsed_milliseconds")
        if not isinstance(elapsed, int) or isinstance(elapsed, bool) or elapsed < 0:
            errors.append("timing.elapsed_milliseconds must be non-negative")
        if all(_timestamp(timing.get(key)) for key in ("started_at", "abandoned_at")):
            started = datetime.fromisoformat(str(timing["started_at"]).replace("Z", "+00:00"))
            abandoned = datetime.fromisoformat(str(timing["abandoned_at"]).replace("Z", "+00:00"))
            expected = round((abandoned - started).total_seconds() * 1000)
            if expected < 0:
                errors.append("timing.abandoned_at must not precede started_at")
            elif elapsed != expected:
                errors.append("timing.elapsed_milliseconds must be derived from timestamps")
        provenance = item.get("provenance")
        if (
            isinstance(provenance, dict)
            and _timestamp(timing.get("abandoned_at"))
            and provenance.get("created_at") != timing.get("abandoned_at")
        ):
            errors.append("provenance.created_at must equal timing.abandoned_at")
    field_provenance = item.get("field_provenance")
    if not isinstance(field_provenance, dict):
        errors.append("field_provenance must be an object")
    else:
        _strict_keys(
            field_provenance,
            {"activity", "note", "reason", "timing"},
            "field_provenance",
            errors,
        )
        expected_origins = {
            "activity": "human-confirmed",
            "note": "human-confirmed",
            "reason": "human-confirmed",
            "timing": "deterministic",
        }
        for field, expected_origin in expected_origins.items():
            provenance = field_provenance.get(field)
            if not isinstance(provenance, dict):
                errors.append(f"field_provenance.{field} must be an object")
                continue
            _strict_keys(
                provenance,
                {"origin", "source"},
                f"field_provenance.{field}",
                errors,
            )
            if provenance.get("origin") != expected_origin:
                errors.append(
                    f"field_provenance.{field}.origin must be {expected_origin}"
                )
            if not _nonempty(provenance.get("source")):
                errors.append(f"field_provenance.{field}.source must be non-empty")
    return errors


def validate_claim_review_index(value: object) -> list[str]:
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    errors, item = _validate_base(
        value,
        artifact="claim-review-index",
        lifecycle="derived-replaceable",
        extra_keys={
            "review_id",
            "attempt_id",
            "version_id",
            "lens",
            "summary",
            "outcomes",
            "view",
            "field_provenance",
        },
        schema_versions=(1, 2, 3),
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "claim-review-index", errors)
    parents = item.get("parents")
    expected = {"review-run", "result-attempt", "lens-result"}
    if isinstance(parents, list):
        expected.update(
            str(parent.get("role"))
            for parent in parents
            if isinstance(parent, dict)
            and isinstance(parent.get("role"), str)
            and str(parent.get("role")).startswith("finding-")
        )
    _require_parent_roles(item, expected, errors)
    expected_parent_artifacts = {
        "review-run": "rule-review-run",
        "result-attempt": "review-result-attempt",
        "lens-result": "argument-check-results",
    }
    expected_parent_artifacts.update(
        {role: "argument-finding" for role in expected if role.startswith("finding-")}
    )
    _require_parent_artifacts(item, expected_parent_artifacts, errors)
    if not isinstance(item.get("review_id"), str) or re.fullmatch(
        r"RV[1-9][0-9]*", str(item.get("review_id"))
    ) is None:
        errors.append("review_id must be RV1..RVn")
    if not isinstance(item.get("attempt_id"), str) or re.fullmatch(
        r"attempt-[0-9]{4}", str(item.get("attempt_id"))
    ) is None:
        errors.append("attempt_id must be attempt-NNNN")
    if not isinstance(item.get("version_id"), str) or re.fullmatch(
        r"V[1-9][0-9]*", str(item.get("version_id"))
    ) is None:
        errors.append("version_id must be V1..Vn")
    _validate_rule_lens(item.get("lens"), "lens", errors)
    summary = item.get("summary")
    summary_keys = set(FINDING_VERDICTS)
    if schema_version in {2, 3}:
        summary_keys.update(REVIEW_EXECUTION_STATUSES[1:])
    if not isinstance(summary, dict):
        errors.append("summary must be an object")
    else:
        _strict_keys(summary, summary_keys, "summary", errors)
        if any(
            not isinstance(summary.get(key), int) or summary.get(key) < 0
            for key in summary_keys
        ):
            errors.append("summary counts must be non-negative integers")
    outcomes = item.get("outcomes")
    if not isinstance(outcomes, list):
        errors.append("outcomes must be an array")
    else:
        task_ids: list[object] = []
        counted = {key: 0 for key in summary_keys}
        finding_ids: list[str] = []
        expected_keys = {
            "task_id",
            "target_claim",
            "check_id",
            "verdict",
            "reason",
            "consequence",
            "evidence_refs",
            "finding_id",
        }
        if schema_version in {2, 3}:
            expected_keys.remove("evidence_refs")
            expected_keys.update(
                {"execution_status", "basis_refs", "support_refs"}
            )
            if schema_version == 3:
                expected_keys.add("support_paths")
        for index, outcome in enumerate(outcomes):
            label = f"outcomes[{index}]"
            if not isinstance(outcome, dict):
                errors.append(f"{label} must be an object")
                continue
            _strict_keys(outcome, expected_keys, label, errors)
            task_ids.append(outcome.get("task_id"))
            for field in ("task_id", "check_id", "reason"):
                if not _nonempty(outcome.get(field)):
                    errors.append(f"{label}.{field} must be a non-empty string")
            target_claim = outcome.get("target_claim")
            if not isinstance(target_claim, str) or _VERSIONED_CLAIM.fullmatch(
                target_claim
            ) is None:
                errors.append(f"{label}.target_claim must be version-qualified")
            verdict = outcome.get("verdict")
            execution_status = (
                outcome.get("execution_status")
                if schema_version in {2, 3}
                else "evaluated"
            )
            if execution_status not in REVIEW_EXECUTION_STATUSES:
                errors.append(
                    f"{label}.execution_status must be one of "
                    f"{REVIEW_EXECUTION_STATUSES}"
                )
            elif execution_status == "evaluated":
                if verdict not in FINDING_VERDICTS:
                    errors.append(
                        f"{label}.verdict must be one of {FINDING_VERDICTS} when evaluated"
                    )
                else:
                    counted[str(verdict)] += 1
            else:
                counted[str(execution_status)] += 1
                if verdict is not None:
                    errors.append(f"{label}.verdict must be null when not evaluated")
            if not isinstance(outcome.get("consequence"), str):
                errors.append(f"{label}.consequence must be a string")
            if schema_version in {2, 3}:
                _string_list(
                    outcome.get("basis_refs"), f"{label}.basis_refs", errors
                )
                _string_list(
                    outcome.get("support_refs"),
                    f"{label}.support_refs",
                    errors,
                )
                if schema_version == 3:
                    support_paths = outcome.get("support_paths")
                    if not isinstance(support_paths, list):
                        errors.append(f"{label}.support_paths must be an array")
                    else:
                        for path_index, support_path in enumerate(support_paths):
                            path_label = f"{label}.support_paths[{path_index}]"
                            if not isinstance(support_path, dict):
                                errors.append(f"{path_label} must be an object")
                                continue
                            _strict_keys(
                                support_path,
                                {"support_ref", "relation_ids"},
                                path_label,
                                errors,
                            )
                            if not isinstance(support_path.get("support_ref"), str):
                                errors.append(f"{path_label}.support_ref must be a string")
                            _string_list(
                                support_path.get("relation_ids"),
                                f"{path_label}.relation_ids",
                                errors,
                                allow_empty=False,
                            )
            else:
                _string_list(
                    outcome.get("evidence_refs"),
                    f"{label}.evidence_refs",
                    errors,
                )
            finding_id = outcome.get("finding_id")
            if (execution_status != "evaluated" or verdict == "pass") and finding_id is not None:
                if schema_version == 1 and verdict == "pass":
                    errors.append(f"{label}.finding_id must be null for pass")
                else:
                    errors.append(
                        f"{label}.finding_id must be null without an actionable verdict"
                    )
            elif verdict in {"fail", "uncertain"}:
                if not _nonempty(finding_id):
                    errors.append(f"{label}.finding_id is required for {verdict}")
                else:
                    finding_ids.append(str(finding_id))
        if len(task_ids) != len(set(task_ids)):
            errors.append("outcomes must not repeat task IDs")
        if len(finding_ids) != len(set(finding_ids)):
            errors.append("outcomes must not repeat finding IDs")
        if isinstance(summary, dict) and any(
            summary.get(key) != counted[key] for key in summary_keys
        ):
            errors.append("summary counts must equal outcomes")
    _validate_bound_file(item.get("view"), "view", errors)
    field_provenance = item.get("field_provenance")
    required_field_provenance = {
        "outcomes.task_id",
        "outcomes.target_claim",
        "outcomes.check_id",
        "outcomes.verdict",
        "outcomes.reason",
        "outcomes.consequence",
        "outcomes.evidence_refs",
        "outcomes.finding_id",
        "summary",
        "view",
    }
    if schema_version in {2, 3}:
        required_field_provenance.remove("outcomes.evidence_refs")
        required_field_provenance.update(
            {
                "outcomes.execution_status",
                "outcomes.basis_refs",
                "outcomes.support_refs",
            }
        )
        if schema_version == 3:
            required_field_provenance.add("outcomes.support_paths")
    if not isinstance(field_provenance, dict):
        errors.append("field_provenance must be an object")
    else:
        _strict_keys(
            field_provenance,
            required_field_provenance,
            "field_provenance",
            errors,
        )
        for field, provenance in field_provenance.items():
            if not isinstance(provenance, dict):
                errors.append(f"field_provenance.{field} must be an object")
                continue
            _strict_keys(
                provenance,
                {"origin", "source"},
                f"field_provenance.{field}",
                errors,
            )
            if provenance.get("origin") not in ORIGINS:
                errors.append(f"field_provenance.{field}.origin is invalid")
            if not _nonempty(provenance.get("source")):
                errors.append(f"field_provenance.{field}.source must be non-empty")
        semantic_fields = [
            "outcomes.verdict",
            "outcomes.reason",
            "outcomes.consequence",
        ]
        semantic_fields.extend(
            [
                "outcomes.execution_status",
                "outcomes.basis_refs",
                "outcomes.support_refs",
            ]
            if schema_version in {2, 3}
            else ["outcomes.evidence_refs"]
        )
        if schema_version == 3:
            semantic_fields.append("outcomes.support_paths")
        for semantic_field in semantic_fields:
            provenance = field_provenance.get(semantic_field)
            if isinstance(provenance, dict) and provenance.get("origin") != "model-derived":
                errors.append(f"{semantic_field} must remain model-derived")
    return errors


def validate_review_status_triage(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="review-status-triage",
        lifecycle="append-only",
        extra_keys={
            "review_id",
            "attempt_id",
            "triage_id",
            "task_id",
            "target_claim",
            "check_id",
            "model_status",
            "decision",
            "action",
            "note",
            "supersedes",
            "field_provenance",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"human-confirmed"}, "review-status-triage", errors)
    for field, pattern, description in (
        ("review_id", r"RV[1-9][0-9]*", "RV1..RVn"),
        ("attempt_id", r"attempt-[0-9]{4}", "attempt-NNNN"),
        ("triage_id", r"ST[0-9]{4}", "STNNNN"),
        ("task_id", r"T[1-9][0-9]*", "T1..Tn"),
    ):
        if not isinstance(item.get(field), str) or re.fullmatch(
            pattern, str(item.get(field))
        ) is None:
            errors.append(f"{field} must be {description}")
    if not isinstance(item.get("target_claim"), str) or _VERSIONED_CLAIM.fullmatch(
        str(item.get("target_claim"))
    ) is None:
        errors.append("target_claim must be version-qualified")
    if not _nonempty(item.get("check_id")):
        errors.append("check_id must be non-empty")
    model_status = item.get("model_status")
    if model_status not in REVIEW_EXECUTION_STATUSES[1:]:
        errors.append(
            "model_status must be blocked_missing_context, routing_mismatch, or not_applicable"
        )
    decision = item.get("decision")
    action = item.get("action")
    if decision not in TRIAGE_DECISIONS:
        errors.append(f"decision must be one of {TRIAGE_DECISIONS}")
    if action not in TRIAGE_ACTIONS:
        errors.append(f"action must be one of {TRIAGE_ACTIONS}")
    allowed_acknowledgements = {
        "routing_mismatch": {"correct_ir", "rerun_review", "other"},
        "blocked_missing_context": {
            "add_context",
            "add_evidence",
            "rerun_review",
            "other",
        },
        "not_applicable": {"acknowledge_not_applicable", "other"},
    }
    if (
        decision == "acknowledge"
        and model_status in allowed_acknowledgements
        and action not in allowed_acknowledgements[str(model_status)]
    ):
        errors.append(f"{action} is not a valid acknowledgement action for {model_status}")
    if decision == "reject" and action not in {"rerun_review", "other"}:
        errors.append("reject requires rerun_review or other action")
    if not _nonempty(item.get("note")):
        errors.append("note must be a non-empty human explanation")
    supersedes = item.get("supersedes")
    if supersedes is not None and not _digest(supersedes):
        errors.append("supersedes must be null or a SHA-256 digest")
    parent_roles = {"review-run", "result-attempt", "lens-result"}
    parent_artifacts = {
        "review-run": "rule-review-run",
        "result-attempt": "review-result-attempt",
        "lens-result": "argument-check-results",
    }
    if supersedes is not None:
        parent_roles.add("previous-triage")
        parent_artifacts["previous-triage"] = "review-status-triage"
    _require_parent_roles(item, parent_roles, errors)
    _require_parent_artifacts(item, parent_artifacts, errors)
    field_provenance = item.get("field_provenance")
    expected_provenance = {
        "binding": "deterministic",
        "model_status": "model-derived",
        "decision": "human-confirmed",
    }
    if not isinstance(field_provenance, dict):
        errors.append("field_provenance must be an object")
    else:
        _strict_keys(
            field_provenance,
            set(expected_provenance),
            "field_provenance",
            errors,
        )
        for field, expected_origin in expected_provenance.items():
            provenance = field_provenance.get(field)
            if not isinstance(provenance, dict):
                errors.append(f"field_provenance.{field} must be an object")
                continue
            _strict_keys(
                provenance,
                {"origin", "source"},
                f"field_provenance.{field}",
                errors,
            )
            if provenance.get("origin") != expected_origin:
                errors.append(
                    f"field_provenance.{field}.origin must be {expected_origin}"
                )
            if not _nonempty(provenance.get("source")):
                errors.append(f"field_provenance.{field}.source must be non-empty")
    return errors


def validate_review_status_triage_index(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="review-status-triage-index",
        lifecycle="derived-replaceable",
        extra_keys={
            "review_id",
            "attempt_id",
            "version_id",
            "summary",
            "items",
            "view",
            "field_provenance",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "review-status-triage-index", errors)
    if not isinstance(item.get("review_id"), str) or re.fullmatch(
        r"RV[1-9][0-9]*", str(item.get("review_id"))
    ) is None:
        errors.append("review_id must be RV1..RVn")
    if not isinstance(item.get("attempt_id"), str) or re.fullmatch(
        r"attempt-[0-9]{4}", str(item.get("attempt_id"))
    ) is None:
        errors.append("attempt_id must be attempt-NNNN")
    if not isinstance(item.get("version_id"), str) or re.fullmatch(
        r"V[1-9][0-9]*", str(item.get("version_id"))
    ) is None:
        errors.append("version_id must be V1..Vn")
    summary = item.get("summary")
    summary_keys = {"total", "open", "acknowledge", "reject"}
    if not isinstance(summary, dict):
        errors.append("summary must be an object")
    else:
        _strict_keys(summary, summary_keys, "summary", errors)
        if any(
            not isinstance(summary.get(key), int)
            or isinstance(summary.get(key), bool)
            or summary.get(key) < 0
            for key in summary_keys
        ):
            errors.append("summary counts must be non-negative integers")
        elif summary["total"] != summary["open"] + summary["acknowledge"] + summary["reject"]:
            errors.append("summary total must equal open plus human decisions")
    items = item.get("items")
    counted = {"open": 0, "acknowledge": 0, "reject": 0}
    task_ids: list[str] = []
    expected_item_keys = {
        "task_id",
        "target_claim",
        "check_id",
        "model_status",
        "reason",
        "decision",
        "action",
        "note",
        "triage_id",
    }
    if not isinstance(items, list):
        errors.append("items must be an array")
    else:
        for index, triage_item in enumerate(items):
            label = f"items[{index}]"
            if not isinstance(triage_item, dict):
                errors.append(f"{label} must be an object")
                continue
            _strict_keys(triage_item, expected_item_keys, label, errors)
            task_id = triage_item.get("task_id")
            if not isinstance(task_id, str) or re.fullmatch(r"T[1-9][0-9]*", task_id) is None:
                errors.append(f"{label}.task_id is invalid")
            else:
                task_ids.append(task_id)
            if not isinstance(triage_item.get("target_claim"), str) or _VERSIONED_CLAIM.fullmatch(
                str(triage_item.get("target_claim"))
            ) is None:
                errors.append(f"{label}.target_claim must be version-qualified")
            if not _nonempty(triage_item.get("check_id")) or not _nonempty(
                triage_item.get("reason")
            ):
                errors.append(f"{label}.check_id/reason must be non-empty")
            if triage_item.get("model_status") not in REVIEW_EXECUTION_STATUSES[1:]:
                errors.append(f"{label}.model_status is invalid")
            decision = triage_item.get("decision")
            if decision is None:
                counted["open"] += 1
                if any(triage_item.get(field) is not None for field in ("action", "note", "triage_id")):
                    errors.append(f"{label} open item must not contain human fields")
            elif decision in TRIAGE_DECISIONS:
                counted[str(decision)] += 1
                if triage_item.get("action") not in TRIAGE_ACTIONS:
                    errors.append(f"{label}.action is invalid")
                if not _nonempty(triage_item.get("note")):
                    errors.append(f"{label}.note must be non-empty")
                if not isinstance(triage_item.get("triage_id"), str) or re.fullmatch(
                    r"ST[0-9]{4}", str(triage_item.get("triage_id"))
                ) is None:
                    errors.append(f"{label}.triage_id is invalid")
            else:
                errors.append(f"{label}.decision is invalid")
        if len(task_ids) != len(set(task_ids)):
            errors.append("items must not repeat task IDs")
    if isinstance(summary, dict):
        if isinstance(items, list) and summary.get("total") != len(items):
            errors.append("summary.total must equal items")
        for key in counted:
            if summary.get(key) != counted[key]:
                errors.append(f"summary.{key} must equal items")
    _validate_bound_file(item.get("view"), "view", errors)
    parents = item.get("parents")
    triage_roles = {
        str(parent.get("role"))
        for parent in parents
        if isinstance(parent, dict)
        and isinstance(parent.get("role"), str)
        and str(parent.get("role")).startswith("triage-")
    } if isinstance(parents, list) else set()
    expected_roles = {"review-run", "result-attempt", "lens-result", *triage_roles}
    expected_artifacts = {
        "review-run": "rule-review-run",
        "result-attempt": "review-result-attempt",
        "lens-result": "argument-check-results",
        **{role: "review-status-triage" for role in triage_roles},
    }
    _require_parent_roles(item, expected_roles, errors)
    _require_parent_artifacts(item, expected_artifacts, errors)
    provenance = item.get("field_provenance")
    expected_provenance = {
        "model": "model-derived",
        "human": "human-confirmed",
        "binding": "deterministic",
        "summary": "deterministic",
        "view": "deterministic",
    }
    if not isinstance(provenance, dict):
        errors.append("field_provenance must be an object")
    else:
        _strict_keys(provenance, set(expected_provenance), "field_provenance", errors)
        for field, origin in expected_provenance.items():
            value = provenance.get(field)
            if not isinstance(value, dict):
                errors.append(f"field_provenance.{field} must be an object")
                continue
            _strict_keys(value, {"origin", "source"}, f"field_provenance.{field}", errors)
            if value.get("origin") != origin:
                errors.append(f"field_provenance.{field}.origin must be {origin}")
            if not _nonempty(value.get("source")):
                errors.append(f"field_provenance.{field}.source must be non-empty")
    return errors


def validate_argument_finding(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="argument-finding",
        lifecycle="immutable",
        extra_keys={
            "finding_id",
            "target_claim",
            "lens",
            "verdict",
            "reason",
            "evidence_refs",
            "status",
        },
    )
    if item is None:
        return errors
    _require_origin(
        item, {"deterministic", "model-derived"}, "argument-finding", errors
    )
    _require_parent_roles(item, {"target-ir", "lens-result"}, errors)
    if not _nonempty(item.get("finding_id")):
        errors.append("finding_id must be a non-empty string")
    if not isinstance(item.get("target_claim"), str) or _VERSIONED_CLAIM.fullmatch(
        str(item.get("target_claim"))
    ) is None:
        errors.append("target_claim must be version-qualified, for example V1:C4")
    lens = item.get("lens")
    if not isinstance(lens, dict):
        errors.append("lens must be an object")
    else:
        _strict_keys(lens, {"kind", "id", "check_id"}, "lens", errors)
        if lens.get("kind") not in LENS_KINDS:
            errors.append(f"lens.kind must be one of {LENS_KINDS}")
        if not _nonempty(lens.get("id")):
            errors.append("lens.id must be a non-empty string")
        check_id = lens.get("check_id")
        if lens.get("kind") == "rule" and not _nonempty(check_id):
            errors.append("rule lenses require lens.check_id")
        if lens.get("kind") == "perspective" and check_id is not None:
            errors.append("perspective lenses require lens.check_id=null")
    lens_result_artifact = (
        "perspective-lens-results"
        if isinstance(lens, dict) and lens.get("kind") == "perspective"
        else "argument-check-results"
    )
    _require_parent_artifacts(
        item,
        {
            "target-ir": "argument-ir",
            "lens-result": lens_result_artifact,
        },
        errors,
    )
    if item.get("verdict") not in FINDING_VERDICTS:
        errors.append(f"verdict must be one of {FINDING_VERDICTS}")
    if not _nonempty(item.get("reason")):
        errors.append("reason must be a non-empty string")
    _string_list(item.get("evidence_refs"), "evidence_refs", errors)
    if item.get("status") != "open":
        errors.append("new findings must have status=open")
    return errors


def validate_finding_adjudication(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="finding-adjudication",
        lifecycle="immutable",
        extra_keys={"adjudication_id", "finding_id", "decision", "reason", "supersedes"},
    )
    if item is None:
        return errors
    _require_origin(item, {"human-confirmed"}, "finding-adjudication", errors)
    for key in ("adjudication_id", "finding_id"):
        if not _nonempty(item.get(key)):
            errors.append(f"{key} must be a non-empty string")
    if item.get("decision") not in DECISIONS:
        errors.append(f"decision must be one of {DECISIONS}")
    reason = item.get("reason")
    if not isinstance(reason, str):
        errors.append("reason must be a string")
    elif item.get("decision") in {"reject", "defer"} and not reason.strip():
        errors.append("reject/defer decisions require a reason")
    supersedes = item.get("supersedes")
    if supersedes is not None and not _digest(supersedes):
        errors.append("supersedes must be null or an adjudication SHA-256")
    _require_parent_roles(
        item,
        {"finding"}
        if supersedes is None
        else {"finding", "previous-adjudication"},
        errors,
    )
    _require_parent_artifacts(
        item,
        {"finding": "argument-finding"}
        if supersedes is None
        else {
            "finding": "argument-finding",
            "previous-adjudication": "finding-adjudication",
        },
        errors,
    )
    if supersedes is not None:
        parents = item.get("parents")
        previous = next(
            (
                parent
                for parent in parents
                if isinstance(parent, dict)
                and parent.get("role") == "previous-adjudication"
            ),
            None,
        ) if isinstance(parents, list) else None
        if not isinstance(previous, dict) or previous.get("sha256") != supersedes:
            errors.append("previous-adjudication parent must match supersedes")
    return errors


def validate_revision_action(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="revision-action",
        lifecycle="immutable",
        extra_keys={"action_id", "adjudication_id", "target_claim", "action_type", "text"},
    )
    if item is None:
        return errors
    _require_origin(item, {"human-confirmed"}, "revision-action", errors)
    _require_parent_roles(item, {"adjudication"}, errors)
    _require_parent_artifacts(
        item, {"adjudication": "finding-adjudication"}, errors
    )
    for key in ("action_id", "adjudication_id", "text"):
        if not _nonempty(item.get(key)):
            errors.append(f"{key} must be a non-empty string")
    if not isinstance(item.get("target_claim"), str) or _VERSIONED_CLAIM.fullmatch(
        str(item.get("target_claim"))
    ) is None:
        errors.append("target_claim must be version-qualified")
    if item.get("action_type") not in REVISION_ACTION_TYPES:
        errors.append(f"action_type must be one of {REVISION_ACTION_TYPES}")
    return errors


def validate_revision_plan_record(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="revision-plan-record",
        lifecycle="derived-replaceable",
        extra_keys={
            "project_id",
            "document_id",
            "version_id",
            "payload",
            "summary",
            "items",
            "field_provenance",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "revision-plan-record", errors)
    for key in ("project_id", "document_id"):
        if not _nonempty(item.get(key)):
            errors.append(f"{key} must be a non-empty string")
    if not isinstance(item.get("version_id"), str) or re.fullmatch(
        r"V[1-9][0-9]*", str(item.get("version_id"))
    ) is None:
        errors.append("version_id must be V1..Vn")
    _validate_bound_file(item.get("payload"), "payload", errors)
    summary = item.get("summary")
    summary_keys = {"accept", "reject", "defer", "open"}
    if not isinstance(summary, dict):
        errors.append("summary must be an object")
    else:
        _strict_keys(summary, summary_keys, "summary", errors)
        if any(
            not isinstance(summary.get(key), int) or summary.get(key) < 0
            for key in summary_keys
        ):
            errors.append("summary counts must be non-negative integers")
    items = item.get("items")
    counted = {key: 0 for key in summary_keys}
    expected_parent_artifacts: dict[str, str] = {}
    expected_parent_roles: set[str] = set()
    finding_ids: list[str] = []
    if not isinstance(items, list):
        errors.append("items must be an array")
        items = []
    for index, plan_item in enumerate(items, 1):
        label = f"items[{index - 1}]"
        finding_role = f"finding-{index:04d}"
        expected_parent_roles.add(finding_role)
        expected_parent_artifacts[finding_role] = "argument-finding"
        if not isinstance(plan_item, dict):
            errors.append(f"{label} must be an object")
            continue
        _strict_keys(
            plan_item,
            {
                "finding_id",
                "target_claim",
                "lens",
                "verdict",
                "model_reason",
                "decision",
                "human_reason",
                "adjudication_id",
                "actions",
                "field_provenance",
            },
            label,
            errors,
        )
        finding_id = plan_item.get("finding_id")
        if not _nonempty(finding_id):
            errors.append(f"{label}.finding_id must be a non-empty string")
        else:
            finding_ids.append(str(finding_id))
        target_claim = plan_item.get("target_claim")
        if not isinstance(target_claim, str) or _VERSIONED_CLAIM.fullmatch(
            target_claim
        ) is None:
            errors.append(f"{label}.target_claim must be version-qualified")
        lens = plan_item.get("lens")
        if not isinstance(lens, dict):
            errors.append(f"{label}.lens must be an object")
        else:
            _strict_keys(lens, {"kind", "id", "check_id"}, f"{label}.lens", errors)
            if lens.get("kind") not in LENS_KINDS:
                errors.append(f"{label}.lens.kind must be one of {LENS_KINDS}")
            if not _nonempty(lens.get("id")):
                errors.append(f"{label}.lens.id must be non-empty")
            if lens.get("kind") == "rule" and not _nonempty(lens.get("check_id")):
                errors.append(f"{label}.rule lens requires check_id")
            if lens.get("kind") == "perspective" and lens.get("check_id") is not None:
                errors.append(f"{label}.perspective lens requires check_id=null")
        if plan_item.get("verdict") not in FINDING_VERDICTS:
            errors.append(f"{label}.verdict must be one of {FINDING_VERDICTS}")
        if not _nonempty(plan_item.get("model_reason")):
            errors.append(f"{label}.model_reason must be non-empty")
        decision = plan_item.get("decision")
        if decision is not None and decision not in DECISIONS:
            errors.append(f"{label}.decision must be accept/reject/defer/null")
        if decision is None:
            counted["open"] += 1
        elif decision in DECISIONS:
            counted[str(decision)] += 1
        human_reason = plan_item.get("human_reason")
        if not isinstance(human_reason, str):
            errors.append(f"{label}.human_reason must be a string")
        elif decision in {"reject", "defer"} and not human_reason.strip():
            errors.append(f"{label}.{decision} requires human_reason")
        adjudication_id = plan_item.get("adjudication_id")
        if decision is None:
            if adjudication_id is not None:
                errors.append(f"{label}.open item requires adjudication_id=null")
        elif not _nonempty(adjudication_id):
            errors.append(f"{label}.decided item requires adjudication_id")
        else:
            adjudication_role = f"adjudication-{index:04d}"
            expected_parent_roles.add(adjudication_role)
            expected_parent_artifacts[adjudication_role] = "finding-adjudication"
        actions = plan_item.get("actions")
        if not isinstance(actions, list):
            errors.append(f"{label}.actions must be an array")
            actions = []
        action_ids: list[str] = []
        for action_index, action in enumerate(actions, 1):
            action_label = f"{label}.actions[{action_index - 1}]"
            action_role = f"action-{index:04d}-{action_index:04d}"
            expected_parent_roles.add(action_role)
            expected_parent_artifacts[action_role] = "revision-action"
            if not isinstance(action, dict):
                errors.append(f"{action_label} must be an object")
                continue
            _strict_keys(
                action,
                {"action_id", "action_type", "text", "sha256"},
                action_label,
                errors,
            )
            if not _nonempty(action.get("action_id")):
                errors.append(f"{action_label}.action_id must be non-empty")
            else:
                action_ids.append(str(action["action_id"]))
            if action.get("action_type") not in REVISION_ACTION_TYPES:
                errors.append(f"{action_label}.action_type is invalid")
            if not _nonempty(action.get("text")):
                errors.append(f"{action_label}.text must be non-empty")
            if not _digest(action.get("sha256")):
                errors.append(f"{action_label}.sha256 must be a SHA-256 digest")
        if len(action_ids) != len(set(action_ids)):
            errors.append(f"{label}.actions must not repeat action IDs")
        if decision == "accept" and not actions:
            errors.append(f"{label}.accept requires at least one action")
        if decision in {None, "reject", "defer"} and actions:
            errors.append(f"{label}.{decision or 'open'} must not have active actions")
        provenance = plan_item.get("field_provenance")
        if not isinstance(provenance, dict):
            errors.append(f"{label}.field_provenance must be an object")
        else:
            _strict_keys(
                provenance,
                {"model", "decision", "actions"},
                f"{label}.field_provenance",
                errors,
            )
            for field in ("model", "decision", "actions"):
                field_value = provenance.get(field)
                if not isinstance(field_value, dict):
                    errors.append(f"{label}.field_provenance.{field} must be an object")
                    continue
                _strict_keys(
                    field_value,
                    {"origin", "source"},
                    f"{label}.field_provenance.{field}",
                    errors,
                )
                if field_value.get("origin") not in ORIGINS:
                    errors.append(f"{label}.field_provenance.{field}.origin is invalid")
                if not _nonempty(field_value.get("source")):
                    errors.append(f"{label}.field_provenance.{field}.source must be non-empty")
            model_provenance = provenance.get("model")
            if isinstance(model_provenance, dict) and model_provenance.get("origin") != "model-derived":
                errors.append(f"{label}.model fields must remain model-derived")
            decision_provenance = provenance.get("decision")
            expected_decision_origin = "deterministic" if decision is None else "human-confirmed"
            if isinstance(decision_provenance, dict) and decision_provenance.get("origin") != expected_decision_origin:
                errors.append(f"{label}.decision provenance must be {expected_decision_origin}")
            action_provenance = provenance.get("actions")
            expected_action_origin = "human-confirmed" if actions else "deterministic"
            if isinstance(action_provenance, dict) and action_provenance.get("origin") != expected_action_origin:
                errors.append(f"{label}.actions provenance must be {expected_action_origin}")
    if len(finding_ids) != len(set(finding_ids)):
        errors.append("items must not repeat finding IDs")
    if isinstance(summary, dict) and any(
        summary.get(key) != counted[key] for key in summary_keys
    ):
        errors.append("summary counts must equal items")
    _require_parent_roles(item, expected_parent_roles, errors)
    _require_parent_artifacts(item, expected_parent_artifacts, errors)
    field_provenance = item.get("field_provenance")
    if not isinstance(field_provenance, dict):
        errors.append("field_provenance must be an object")
    else:
        _strict_keys(
            field_provenance, {"summary", "payload"}, "field_provenance", errors
        )
        for field in ("summary", "payload"):
            provenance = field_provenance.get(field)
            if not isinstance(provenance, dict):
                errors.append(f"field_provenance.{field} must be an object")
                continue
            _strict_keys(
                provenance,
                {"origin", "source"},
                f"field_provenance.{field}",
                errors,
            )
            if provenance.get("origin") != "deterministic":
                errors.append(f"field_provenance.{field}.origin must be deterministic")
            if not _nonempty(provenance.get("source")):
                errors.append(f"field_provenance.{field}.source must be non-empty")
    return errors


def _validate_gate_a_metrics(
    value: object,
    label: str,
    errors: list[str],
    *,
    include_correction_minutes: bool = True,
) -> None:
    expected = {
        "missed_claims",
        "wrong_claim_types",
        "wrong_relations",
        "rhetoric_as_claims",
        "reversed_attributions",
    }
    if include_correction_minutes:
        expected.add("correction_minutes")
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return
    _strict_keys(value, expected, label, errors)
    for key in expected:
        item = value.get(key)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            errors.append(f"{label}.{key} must be a non-negative integer")


def validate_gate_a_corpus(value: object) -> list[str]:
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    errors, item = _validate_base(
        value,
        artifact="product-gate-a-corpus",
        lifecycle="immutable",
        extra_keys={"corpus_id", "entries"},
        schema_versions=(1, 2, 3, 4, 5),
    )
    if item is None:
        return errors
    _require_origin(item, {"human-confirmed"}, "product-gate-a-corpus", errors)
    if not _nonempty(item.get("corpus_id")):
        errors.append("corpus_id must be a non-empty string")
    entries = item.get("entries")
    expected_roles: set[str] = set()
    expected_artifacts: dict[str, str] = {}
    aliases: list[str] = []
    source_hashes: list[str] = []
    parent_hashes = {
        parent.get("role"): parent.get("sha256")
        for parent in item.get("parents", [])
        if isinstance(parent, dict)
    }
    if not isinstance(entries, list) or not 3 <= len(entries) <= 5:
        errors.append("entries must contain 3 to 5 real manuscripts")
        entries = []
    for index, entry in enumerate(entries, 1):
        label = f"entries[{index - 1}]"
        role = f"project-{index:03d}"
        expected_roles.add(role)
        expected_artifacts[role] = "argument-project"
        if schema_version in {2, 3, 4, 5}:
            baseline_role = f"baseline-{index:03d}"
            expected_roles.add(baseline_role)
            expected_artifacts[baseline_role] = "direct-review-baseline"
        if not isinstance(entry, dict):
            errors.append(f"{label} must be an object")
            continue
        _strict_keys(
            entry,
            {
                "alias",
                "workspace_locator",
                "project_id",
                "document_id",
                "version_id",
                "real_manuscript_confirmed",
                "bindings",
            },
            label,
            errors,
        )
        for key in ("alias", "workspace_locator", "project_id", "document_id"):
            if not _nonempty(entry.get(key)):
                errors.append(f"{label}.{key} must be a non-empty string")
        aliases.append(str(entry.get("alias")))
        if not isinstance(entry.get("version_id"), str) or re.fullmatch(
            r"V[1-9][0-9]*", str(entry.get("version_id"))
        ) is None:
            errors.append(f"{label}.version_id must be V1..Vn")
        if entry.get("real_manuscript_confirmed") is not True:
            errors.append(f"{label}.real_manuscript_confirmed must be true")
        bindings = entry.get("bindings")
        binding_keys = {
            "project",
            "document_version",
            "source",
            "reviewed_ir_record",
            "reviewed_ir_payload",
            "revision_plan_record",
            "revision_plan_markdown",
        }
        if schema_version in {2, 3, 4, 5}:
            binding_keys.add("direct_review_baseline")
        if schema_version in {3, 4, 5}:
            binding_keys.add("status_triage")
        if schema_version == 5:
            binding_keys.add("ir_inspection_sessions")
        if not isinstance(bindings, dict):
            errors.append(f"{label}.bindings must be an object")
        else:
            _strict_keys(bindings, binding_keys, f"{label}.bindings", errors)
            for key in binding_keys - {
                "status_triage",
                "ir_inspection_sessions",
            }:
                if not _digest(bindings.get(key)):
                    errors.append(f"{label}.bindings.{key} must be a SHA-256 digest")
            source_hashes.append(str(bindings.get("source")))
            if schema_version in {3, 4, 5}:
                triage_bindings = bindings.get("status_triage")
                if not isinstance(triage_bindings, list):
                    errors.append(f"{label}.bindings.status_triage must be an array")
                else:
                    seen_triage_reviews: set[tuple[object, object]] = set()
                    for triage_index, triage in enumerate(triage_bindings, 1):
                        triage_label = (
                            f"{label}.bindings.status_triage[{triage_index - 1}]"
                        )
                        if not isinstance(triage, dict):
                            errors.append(f"{triage_label} must be an object")
                            continue
                        _strict_keys(
                            triage,
                            {"review_id", "attempt_id", "sha256"},
                            triage_label,
                            errors,
                        )
                        if not isinstance(triage.get("review_id"), str) or re.fullmatch(
                            r"RV[1-9][0-9]*", str(triage.get("review_id"))
                        ) is None:
                            errors.append(f"{triage_label}.review_id is invalid")
                        if not isinstance(triage.get("attempt_id"), str) or re.fullmatch(
                            r"attempt-[0-9]{4}", str(triage.get("attempt_id"))
                        ) is None:
                            errors.append(f"{triage_label}.attempt_id is invalid")
                        if not _digest(triage.get("sha256")):
                            errors.append(f"{triage_label}.sha256 must be a digest")
                        key = (
                            triage.get("review_id"),
                            triage.get("attempt_id"),
                        )
                        if key in seen_triage_reviews:
                            errors.append(f"{triage_label} repeats a review attempt")
                        seen_triage_reviews.add(key)
                        role = f"status-triage-{index:03d}-{triage_index:03d}"
                        expected_roles.add(role)
                        expected_artifacts[role] = "review-status-triage-index"
            if schema_version == 5:
                session_bindings = bindings.get("ir_inspection_sessions")
                if not isinstance(session_bindings, list) or not session_bindings:
                    errors.append(
                        f"{label}.bindings.ir_inspection_sessions must be a non-empty array"
                    )
                else:
                    session_ids: list[str] = []
                    for session_index, session in enumerate(session_bindings, 1):
                        session_label = (
                            f"{label}.bindings.ir_inspection_sessions"
                            f"[{session_index - 1}]"
                        )
                        if not isinstance(session, dict):
                            errors.append(f"{session_label} must be an object")
                            continue
                        _strict_keys(
                            session,
                            {"session_id", "sha256", "elapsed_milliseconds"},
                            session_label,
                            errors,
                        )
                        if not isinstance(session.get("session_id"), str) or re.fullmatch(
                            r"GS[1-9][0-9]*", str(session.get("session_id"))
                        ) is None:
                            errors.append(f"{session_label}.session_id is invalid")
                        session_ids.append(str(session.get("session_id")))
                        if not _digest(session.get("sha256")):
                            errors.append(f"{session_label}.sha256 must be a digest")
                        elapsed = session.get("elapsed_milliseconds")
                        if (
                            not isinstance(elapsed, int)
                            or isinstance(elapsed, bool)
                            or elapsed < 0
                        ):
                            errors.append(
                                f"{session_label}.elapsed_milliseconds must be non-negative"
                            )
                        role = (
                            f"ir-inspection-{index:03d}-{session_index:03d}"
                        )
                        expected_roles.add(role)
                        expected_artifacts[role] = "gate-a-work-session"
                        if parent_hashes.get(role) != session.get("sha256"):
                            errors.append(
                                f"{session_label}.sha256 must match parent {role}"
                            )
                    if len(session_ids) != len(set(session_ids)):
                        errors.append(
                            f"{label}.bindings.ir_inspection_sessions repeats a session"
                        )
    if len(aliases) != len(set(aliases)):
        errors.append("entry aliases must be unique")
    if len(source_hashes) != len(set(source_hashes)):
        errors.append("entry source hashes must be unique")
    _require_parent_roles(item, expected_roles, errors)
    _require_parent_artifacts(item, expected_artifacts, errors)
    return errors


def validate_gate_a_assessment(value: object) -> list[str]:
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    extra_keys = {
        "corpus_id",
        "project_alias",
        "comparison_to_direct_chat",
        "correction_burden",
        "metrics",
        "regression_anchors",
        "actual_revision_notes",
        "notes",
    }
    if schema_version == 5:
        extra_keys.update({"ir_inspection_timing", "field_provenance"})
    errors, item = _validate_base(
        value,
        artifact="product-gate-a-assessment",
        lifecycle="immutable",
        extra_keys=extra_keys,
        schema_versions=(1, 2, 3, 4, 5),
    )
    if item is None:
        return errors
    _require_origin(item, {"human-confirmed"}, "product-gate-a-assessment", errors)
    parent_roles = {"corpus", "project", "revision-plan"}
    parent_artifacts = {
        "corpus": "product-gate-a-corpus",
        "project": "argument-project",
        "revision-plan": "revision-plan-record",
    }
    if schema_version in {2, 3, 4, 5}:
        parent_roles.add("direct-review-baseline")
        parent_artifacts["direct-review-baseline"] = "direct-review-baseline"
    if schema_version in {3, 4, 5}:
        for parent in item.get("parents", []):
            if isinstance(parent, dict) and str(parent.get("role", "")).startswith(
                "status-triage-"
            ):
                role = str(parent["role"])
                if re.fullmatch(r"status-triage-[0-9]{3}", role) is None:
                    errors.append(f"invalid status triage parent role: {role}")
                parent_roles.add(role)
                parent_artifacts[role] = "review-status-triage-index"
    timing = item.get("ir_inspection_timing")
    if schema_version == 5:
        if not isinstance(timing, dict):
            errors.append("ir_inspection_timing must be an object")
        else:
            _strict_keys(
                timing,
                {"elapsed_milliseconds", "sessions"},
                "ir_inspection_timing",
                errors,
            )
            elapsed = timing.get("elapsed_milliseconds")
            if (
                not isinstance(elapsed, int)
                or isinstance(elapsed, bool)
                or elapsed < 0
            ):
                errors.append(
                    "ir_inspection_timing.elapsed_milliseconds must be non-negative"
                )
            sessions = timing.get("sessions")
            if not isinstance(sessions, list) or not sessions:
                errors.append(
                    "ir_inspection_timing.sessions must be a non-empty array"
                )
            else:
                session_ids: list[str] = []
                parent_hashes = {
                    parent.get("role"): parent.get("sha256")
                    for parent in item.get("parents", [])
                    if isinstance(parent, dict)
                }
                for index, session in enumerate(sessions, 1):
                    label = f"ir_inspection_timing.sessions[{index - 1}]"
                    role = f"ir-inspection-{index:03d}"
                    parent_roles.add(role)
                    parent_artifacts[role] = "gate-a-work-session"
                    if not isinstance(session, dict):
                        errors.append(f"{label} must be an object")
                        continue
                    _strict_keys(
                        session,
                        {"session_id", "sha256"},
                        label,
                        errors,
                    )
                    if not isinstance(session.get("session_id"), str) or re.fullmatch(
                        r"GS[1-9][0-9]*", str(session.get("session_id"))
                    ) is None:
                        errors.append(f"{label}.session_id is invalid")
                    session_ids.append(str(session.get("session_id")))
                    if not _digest(session.get("sha256")):
                        errors.append(f"{label}.sha256 must be a digest")
                    if parent_hashes.get(role) != session.get("sha256"):
                        errors.append(f"{label}.sha256 must match parent {role}")
                if len(session_ids) != len(set(session_ids)):
                    errors.append("ir_inspection_timing.sessions repeats a session")
        field_provenance = item.get("field_provenance")
        if not isinstance(field_provenance, dict):
            errors.append("field_provenance must be an object")
        else:
            _strict_keys(
                field_provenance,
                {"human_observations", "ir_inspection_timing"},
                "field_provenance",
                errors,
            )
            expected_origins = {
                "human_observations": "human-confirmed",
                "ir_inspection_timing": "deterministic",
            }
            for field, origin in expected_origins.items():
                provenance = field_provenance.get(field)
                if not isinstance(provenance, dict):
                    errors.append(f"field_provenance.{field} must be an object")
                    continue
                _strict_keys(
                    provenance,
                    {"origin", "source"},
                    f"field_provenance.{field}",
                    errors,
                )
                if provenance.get("origin") != origin:
                    errors.append(
                        f"field_provenance.{field}.origin must be {origin}"
                    )
                if not _nonempty(provenance.get("source")):
                    errors.append(
                        f"field_provenance.{field}.source must be non-empty"
                    )
    _require_parent_roles(item, parent_roles, errors)
    _require_parent_artifacts(
        item,
        parent_artifacts,
        errors,
    )
    for key in ("corpus_id", "project_alias"):
        if not _nonempty(item.get(key)):
            errors.append(f"{key} must be a non-empty string")
    if item.get("comparison_to_direct_chat") not in GATE_A_COMPARISONS:
        errors.append(f"comparison_to_direct_chat must be one of {GATE_A_COMPARISONS}")
    if item.get("correction_burden") not in GATE_A_BURDENS:
        errors.append(f"correction_burden must be one of {GATE_A_BURDENS}")
    _validate_gate_a_metrics(
        item.get("metrics"),
        "metrics",
        errors,
        include_correction_minutes=schema_version != 5,
    )
    _string_list(
        item.get("regression_anchors"),
        "regression_anchors",
        errors,
        allow_empty=False,
    )
    if not isinstance(item.get("actual_revision_notes"), str):
        errors.append("actual_revision_notes must be a string")
    if not isinstance(item.get("notes"), str):
        errors.append("notes must be a string")
    return errors


def validate_gate_a_decision(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="product-gate-a-decision",
        lifecycle="immutable",
        extra_keys={"corpus_id", "decision", "reason", "supersedes"},
    )
    if item is None:
        return errors
    _require_origin(item, {"human-confirmed"}, "product-gate-a-decision", errors)
    if not _nonempty(item.get("corpus_id")):
        errors.append("corpus_id must be a non-empty string")
    if item.get("decision") not in GATE_A_DECISIONS:
        errors.append(f"decision must be one of {GATE_A_DECISIONS}")
    if not _nonempty(item.get("reason")):
        errors.append("reason must be a non-empty string")
    supersedes = item.get("supersedes")
    if supersedes is not None and not _digest(supersedes):
        errors.append("supersedes must be null or a SHA-256 digest")
    parents = {"corpus": "product-gate-a-corpus"}
    roles = {"corpus"}
    if supersedes is not None:
        parents["previous-decision"] = "product-gate-a-decision"
        roles.add("previous-decision")
    _require_parent_roles(item, roles, errors)
    _require_parent_artifacts(item, parents, errors)
    return errors


def validate_gate_a_report(value: object) -> list[str]:
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    extra_keys = {
        "corpus_id",
        "readiness",
        "workflow_totals",
        "human_observations",
        "projects",
        "gate_decision",
        "payload",
    }
    if schema_version == 2:
        extra_keys.add("work_timing")
    errors, item = _validate_base(
        value,
        artifact="product-gate-a-report",
        lifecycle="derived-replaceable",
        extra_keys=extra_keys,
        schema_versions=(1, 2),
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "product-gate-a-report", errors)
    if not _nonempty(item.get("corpus_id")):
        errors.append("corpus_id must be a non-empty string")
    readiness = item.get("readiness")
    readiness_keys = {
        "corpus_size",
        "assessments_complete",
        "workflows_complete",
        "open_findings",
        "ready_for_human_decision",
    }
    if not isinstance(readiness, dict):
        errors.append("readiness must be an object")
    else:
        _strict_keys(readiness, readiness_keys, "readiness", errors)
        for key in ("corpus_size", "assessments_complete", "workflows_complete", "open_findings"):
            value = readiness.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"readiness.{key} must be a non-negative integer")
        if not isinstance(readiness.get("ready_for_human_decision"), bool):
            errors.append("readiness.ready_for_human_decision must be boolean")
    totals = item.get("workflow_totals")
    total_keys = {"claims", "corrections", "findings", "accept", "reject", "defer", "open"}
    if not isinstance(totals, dict):
        errors.append("workflow_totals must be an object")
    else:
        _strict_keys(totals, total_keys, "workflow_totals", errors)
        for key in total_keys:
            value = totals.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"workflow_totals.{key} must be a non-negative integer")
    observations = item.get("human_observations")
    observation_keys = {"clearer", "same", "worse", "uncertain", "acceptable_burden", "high_burden", "uncertain_burden", "regression_anchors", "actual_revisions_recorded", "metrics"}
    if not isinstance(observations, dict):
        errors.append("human_observations must be an object")
    else:
        _strict_keys(observations, observation_keys, "human_observations", errors)
        for key in observation_keys - {"metrics"}:
            value = observations.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"human_observations.{key} must be a non-negative integer")
        _validate_gate_a_metrics(
            observations.get("metrics"),
            "human_observations.metrics",
            errors,
            include_correction_minutes=schema_version == 1,
        )
    if schema_version == 2:
        work_timing = item.get("work_timing")
        if not isinstance(work_timing, dict):
            errors.append("work_timing must be an object")
        else:
            _strict_keys(
                work_timing,
                {"ir_inspection_elapsed_milliseconds"},
                "work_timing",
                errors,
            )
            elapsed = work_timing.get("ir_inspection_elapsed_milliseconds")
            if (
                not isinstance(elapsed, int)
                or isinstance(elapsed, bool)
                or elapsed < 0
            ):
                errors.append(
                    "work_timing.ir_inspection_elapsed_milliseconds must be non-negative"
                )
    projects = item.get("projects")
    expected_parent_roles = {"corpus"}
    expected_parent_artifacts = {"corpus": "product-gate-a-corpus"}
    if not isinstance(projects, list) or not 3 <= len(projects) <= 5:
        errors.append("projects must contain 3 to 5 entries")
        projects = []
    aliases: list[str] = []
    for index, project in enumerate(projects, 1):
        label = f"projects[{index - 1}]"
        if not isinstance(project, dict):
            errors.append(f"{label} must be an object")
            continue
        project_keys = {
                "alias",
                "bindings_match",
                "workflow_complete",
                "claims",
                "corrections",
                "findings",
                "accept",
                "reject",
                "defer",
                "open",
                "assessment_id",
                "regression_anchors",
                "actual_revision_recorded",
            }
        if schema_version == 2:
            project_keys.add("ir_inspection_elapsed_milliseconds")
        _strict_keys(project, project_keys, label, errors)
        if not _nonempty(project.get("alias")):
            errors.append(f"{label}.alias must be a non-empty string")
        aliases.append(str(project.get("alias")))
        for key in ("bindings_match", "workflow_complete", "actual_revision_recorded"):
            if not isinstance(project.get(key), bool):
                errors.append(f"{label}.{key} must be boolean")
        for key in ("claims", "corrections", "findings", "accept", "reject", "defer", "open", "regression_anchors"):
            value = project.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"{label}.{key} must be a non-negative integer")
        if schema_version == 2:
            elapsed = project.get("ir_inspection_elapsed_milliseconds")
            if (
                not isinstance(elapsed, int)
                or isinstance(elapsed, bool)
                or elapsed < 0
            ):
                errors.append(
                    f"{label}.ir_inspection_elapsed_milliseconds must be non-negative"
                )
        assessment_id = project.get("assessment_id")
        if assessment_id is not None and not _nonempty(assessment_id):
            errors.append(f"{label}.assessment_id must be null or non-empty")
        if assessment_id is not None:
            role = f"assessment-{index:03d}"
            expected_parent_roles.add(role)
            expected_parent_artifacts[role] = "product-gate-a-assessment"
        if project.get("workflow_complete") is True and (
            project.get("bindings_match") is not True or project.get("open") != 0
        ):
            errors.append(f"{label}.workflow_complete requires intact bindings and zero open Findings")
    if len(aliases) != len(set(aliases)):
        errors.append("project aliases must be unique")
    if schema_version == 2 and isinstance(item.get("work_timing"), dict):
        expected_elapsed = sum(
            int(project.get("ir_inspection_elapsed_milliseconds", 0))
            for project in projects
            if isinstance(project, dict)
            and isinstance(project.get("ir_inspection_elapsed_milliseconds"), int)
            and not isinstance(
                project.get("ir_inspection_elapsed_milliseconds"), bool
            )
        )
        if item["work_timing"].get(
            "ir_inspection_elapsed_milliseconds"
        ) != expected_elapsed:
            errors.append(
                "work_timing.ir_inspection_elapsed_milliseconds must equal project total"
            )
    decision = item.get("gate_decision")
    if decision is not None and decision not in GATE_A_DECISIONS:
        errors.append("gate_decision must be pass/fail/defer/null")
    if decision is not None:
        expected_parent_roles.add("gate-decision")
        expected_parent_artifacts["gate-decision"] = "product-gate-a-decision"
    _validate_bound_file(item.get("payload"), "payload", errors)
    _require_parent_roles(item, expected_parent_roles, errors)
    _require_parent_artifacts(item, expected_parent_artifacts, errors)
    return errors


def validate_claim_lineage(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="claim-lineage",
        lifecycle="immutable",
        extra_keys={
            "lineage_id",
            "from_claims",
            "to_claims",
            "relation",
            "proposed_by",
            "proposal_sha256",
            "status",
        },
    )
    if item is None:
        return errors
    if not _nonempty(item.get("lineage_id")):
        errors.append("lineage_id must be a non-empty string")
    from_claims = _string_list(item.get("from_claims"), "from_claims", errors)
    to_claims = _string_list(item.get("to_claims"), "to_claims", errors)
    for label, claims in (("from_claims", from_claims), ("to_claims", to_claims)):
        for claim in claims:
            if _VERSIONED_CLAIM.fullmatch(claim) is None:
                errors.append(f"{label} contains an invalid version-qualified claim: {claim}")
    relation = item.get("relation")
    if relation not in LINEAGE_RELATIONS:
        errors.append(f"relation must be one of {LINEAGE_RELATIONS}")
    if relation == "new" and (from_claims or not to_claims):
        errors.append("new lineage requires no from_claims and at least one to_claim")
    elif relation == "removed" and (not from_claims or to_claims):
        errors.append("removed lineage requires from_claims and no to_claims")
    elif relation not in {"new", "removed"} and (not from_claims or not to_claims):
        errors.append("lineage requires claims on both sides")
    if relation == "split" and (len(from_claims) != 1 or len(to_claims) < 2):
        errors.append("split lineage requires one source and at least two descendants")
    if relation == "merged" and (len(from_claims) < 2 or len(to_claims) != 1):
        errors.append("merged lineage requires at least two sources and one descendant")
    if item.get("status") not in LINEAGE_STATUSES:
        errors.append(f"status must be one of {LINEAGE_STATUSES}")
    provenance = item.get("provenance")
    proposed_by = item.get("proposed_by")
    if proposed_by not in {"model", "human"}:
        errors.append("proposed_by must be model or human")
    status = item.get("status")
    proposal_sha256 = item.get("proposal_sha256")
    parents = item.get("parents")
    parent_by_role = {
        parent.get("role"): parent
        for parent in parents
        if isinstance(parent, dict)
    } if isinstance(parents, list) else {}
    required_parent_roles = (
        {"to-version"}
        if relation == "new"
        else {"from-version"}
        if relation == "removed"
        else {"from-version", "to-version"}
    )
    if status in {"human_confirmed", "rejected"} and proposed_by == "model":
        required_parent_roles.add("proposal")
    if set(parent_by_role) != required_parent_roles:
        errors.append(
            f"claim-lineage parent roles must be exactly {sorted(required_parent_roles)}"
        )
    if status == "proposed":
        if proposed_by != "model":
            errors.append("proposed lineage status is reserved for model proposals")
        if isinstance(provenance, dict) and provenance.get("origin") != "model-derived":
            errors.append("model lineage proposal requires model-derived provenance")
        if proposal_sha256 is not None or "proposal" in parent_by_role:
            errors.append("initial lineage proposal must not reference another proposal")
    elif status in {"human_confirmed", "rejected"}:
        if isinstance(provenance, dict) and provenance.get("origin") != "human-confirmed":
            errors.append(f"{status} lineage requires human-confirmed provenance")
        if proposed_by == "model":
            if not _digest(proposal_sha256):
                errors.append("human decision on model lineage requires proposal_sha256")
            proposal_parent = parent_by_role.get("proposal")
            if not isinstance(proposal_parent, dict):
                errors.append("human decision on model lineage requires a proposal parent")
            elif proposal_parent.get("sha256") != proposal_sha256:
                errors.append("proposal parent hash must equal proposal_sha256")
        elif proposal_sha256 is not None or "proposal" in parent_by_role:
            errors.append("human-originated lineage must not reference a model proposal")
    return errors


VALIDATORS: dict[str, Callable[[object], list[str]]] = {
    "argument-project": validate_project,
    "argument-document": validate_document,
    "document-version": validate_document_version,
    "raw-ir-attempt": validate_raw_ir_attempt,
    "ir-correction": validate_ir_correction,
    "reviewed-argument-ir": validate_reviewed_ir_record,
    "rule-review-run": validate_rule_review_run,
    "review-result-attempt": validate_review_result_attempt,
    "perspective-lens-protocol": validate_perspective_lens_protocol,
    "perspective-review-plan": validate_perspective_review_plan,
    "perspective-review-run": validate_perspective_review_run,
    "perspective-result-attempt": validate_perspective_result_attempt,
    "perspective-lens-results": validate_perspective_lens_results,
    "perspective-review-index": validate_perspective_review_index,
    "direct-review-baseline": validate_direct_review_baseline,
    "gate-a-session-start": validate_gate_a_session_start,
    "gate-a-work-session": validate_gate_a_work_session,
    "gate-a-session-abandonment": validate_gate_a_session_abandonment,
    "claim-review-index": validate_claim_review_index,
    "review-status-triage": validate_review_status_triage,
    "review-status-triage-index": validate_review_status_triage_index,
    "argument-finding": validate_argument_finding,
    "finding-adjudication": validate_finding_adjudication,
    "revision-action": validate_revision_action,
    "revision-plan-record": validate_revision_plan_record,
    "product-gate-a-corpus": validate_gate_a_corpus,
    "product-gate-a-assessment": validate_gate_a_assessment,
    "product-gate-a-decision": validate_gate_a_decision,
    "product-gate-a-report": validate_gate_a_report,
    "claim-lineage": validate_claim_lineage,
}


def validate_artifact(value: object) -> list[str]:
    if not isinstance(value, dict):
        return ["artifact must be a JSON object"]
    artifact = value.get("artifact")
    validator = VALIDATORS.get(str(artifact))
    if validator is None:
        return [f"unknown artifact contract: {artifact!r}"]
    return validator(value)


def validate_contract_bundle(
    entries: list[tuple[object, bytes]],
) -> list[str]:
    """Validate artifacts, their exact-byte parent links, and accept/action interlocks."""
    errors: list[str] = []
    by_hash: dict[str, dict[str, Any]] = {}
    for index, (value, data) in enumerate(entries):
        entry_errors = validate_artifact(value)
        errors.extend(f"entries[{index}]: {error}" for error in entry_errors)
        if isinstance(value, dict):
            by_hash[sha256_bytes(data)] = value

    for index, (value, _) in enumerate(entries):
        if not isinstance(value, dict):
            continue
        parents = value.get("parents")
        if not isinstance(parents, list):
            continue
        for parent in parents:
            if not isinstance(parent, dict) or not _digest(parent.get("sha256")):
                continue
            linked = by_hash.get(str(parent["sha256"]))
            if linked is None:
                if parent.get("artifact") in VALIDATORS:
                    errors.append(
                        f"entries[{index}]: parent {parent.get('role')!r} is not present in bundle"
                    )
            elif linked.get("artifact") != parent.get("artifact"):
                errors.append(
                    f"entries[{index}]: parent {parent.get('role')!r} artifact type mismatch"
                )

    accepted_hashes = {
        digest
        for digest, value in by_hash.items()
        if value.get("artifact") == "finding-adjudication"
        and value.get("decision") == "accept"
    }
    action_parent_hashes = {
        str(parent.get("sha256"))
        for value in by_hash.values()
        if value.get("artifact") == "revision-action"
        for parent in value.get("parents", [])
        if isinstance(parent, dict) and parent.get("role") == "adjudication"
    }
    for accepted_hash in accepted_hashes - action_parent_hashes:
        errors.append(
            f"accepted adjudication {accepted_hash} requires at least one revision-action"
        )
    adjudication_decisions = {
        digest: value.get("decision")
        for digest, value in by_hash.items()
        if value.get("artifact") == "finding-adjudication"
    }
    for action_parent_hash in action_parent_hashes:
        if (
            action_parent_hash in adjudication_decisions
            and adjudication_decisions[action_parent_hash] != "accept"
        ):
            errors.append(
                "revision-action may only reference an accepted adjudication: "
                + action_parent_hash
            )
    return errors
