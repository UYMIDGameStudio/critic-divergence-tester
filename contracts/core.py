"""Artifact contract validators grouped by family."""

from __future__ import annotations

from .base import *  # noqa: F403

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



__all__ = [name for name in globals() if not name.startswith("__")]
