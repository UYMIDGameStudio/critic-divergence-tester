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
REVIEW_DEPTHS = ("core", "full")
REVIEW_RESULT_STATUSES = ("valid", "unusable")

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
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version must be 1")
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


def validate_rule_review_run(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="rule-review-run",
        lifecycle="immutable",
        extra_keys={
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
        },
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


def validate_claim_review_index(value: object) -> list[str]:
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
    if not isinstance(summary, dict):
        errors.append("summary must be an object")
    else:
        _strict_keys(summary, {"pass", "fail", "uncertain"}, "summary", errors)
        if any(
            not isinstance(summary.get(verdict), int) or summary.get(verdict) < 0
            for verdict in FINDING_VERDICTS
        ):
            errors.append("summary counts must be non-negative integers")
    outcomes = item.get("outcomes")
    if not isinstance(outcomes, list):
        errors.append("outcomes must be an array")
    else:
        task_ids: list[object] = []
        counted = {verdict: 0 for verdict in FINDING_VERDICTS}
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
            if verdict not in FINDING_VERDICTS:
                errors.append(f"{label}.verdict must be one of {FINDING_VERDICTS}")
            else:
                counted[str(verdict)] += 1
            if not isinstance(outcome.get("consequence"), str):
                errors.append(f"{label}.consequence must be a string")
            _string_list(outcome.get("evidence_refs"), f"{label}.evidence_refs", errors)
            finding_id = outcome.get("finding_id")
            if verdict == "pass" and finding_id is not None:
                errors.append(f"{label}.finding_id must be null for pass")
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
            summary.get(verdict) != counted[verdict] for verdict in FINDING_VERDICTS
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
        for semantic_field in (
            "outcomes.verdict",
            "outcomes.reason",
            "outcomes.consequence",
            "outcomes.evidence_refs",
        ):
            provenance = field_provenance.get(semantic_field)
            if isinstance(provenance, dict) and provenance.get("origin") != "model-derived":
                errors.append(f"{semantic_field} must remain model-derived")
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
    _require_parent_artifacts(
        item,
        {
            "target-ir": "argument-ir",
            "lens-result": "argument-check-results",
        },
        errors,
    )
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
    "claim-review-index": validate_claim_review_index,
    "argument-finding": validate_argument_finding,
    "finding-adjudication": validate_finding_adjudication,
    "revision-action": validate_revision_action,
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
