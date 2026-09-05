"""Artifact contract validators grouped by family."""

from __future__ import annotations

from .base import *  # noqa: F403
from .core import *  # noqa: F403

def _validate_structural_node_entry(
    value: object,
    label: str,
    errors: list[str],
    *,
    status: str,
) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return
    if status == "exact_unchanged":
        keys = {"kind", "from_ref", "to_ref", "fingerprint"}
    elif status == "literal_anchor_modified":
        keys = {
            "kind",
            "from_ref",
            "to_ref",
            "anchor_fingerprint",
            "changed_fields",
        }
    else:
        keys = {"kind", "ref", "fingerprint"}
    _strict_keys(value, keys, label, errors)
    if value.get("kind") not in {"claim", "evidence", "assumption", "citation"}:
        errors.append(f"{label}.kind must identify an Argument IR node kind")
    if status in {"exact_unchanged", "literal_anchor_modified"}:
        for field in ("from_ref", "to_ref"):
            if not isinstance(value.get(field), str) or re.fullmatch(
                r"V[1-9][0-9]*:[CEAZ][1-9][0-9]*", str(value.get(field))
            ) is None:
                errors.append(f"{label}.{field} must be a version-qualified node")
        digest_field = (
            "fingerprint" if status == "exact_unchanged" else "anchor_fingerprint"
        )
        if not _digest(value.get(digest_field)):
            errors.append(f"{label}.{digest_field} must be a SHA-256 digest")
        if status == "literal_anchor_modified":
            changed = _string_list(
                value.get("changed_fields"),
                f"{label}.changed_fields",
                errors,
                allow_empty=False,
            )
            if len(changed) != len(set(changed)):
                errors.append(f"{label}.changed_fields must not repeat fields")
    else:
        if not isinstance(value.get("ref"), str) or re.fullmatch(
            r"V[1-9][0-9]*:[CEAZ][1-9][0-9]*", str(value.get("ref"))
        ) is None:
            errors.append(f"{label}.ref must be a version-qualified node")
        if not _digest(value.get("fingerprint")):
            errors.append(f"{label}.fingerprint must be a SHA-256 digest")


def _validate_structural_relation_entry(
    value: object,
    label: str,
    errors: list[str],
    *,
    paired: bool,
) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return
    keys = {"from_ref", "to_ref", "fingerprint"} if paired else {"ref", "fingerprint"}
    _strict_keys(value, keys, label, errors)
    for field in (("from_ref", "to_ref") if paired else ("ref",)):
        if not isinstance(value.get(field), str) or re.fullmatch(
            r"V[1-9][0-9]*:R[1-9][0-9]*", str(value.get(field))
        ) is None:
            errors.append(f"{label}.{field} must be a version-qualified relation")
    if not _digest(value.get("fingerprint")):
        errors.append(f"{label}.fingerprint must be a SHA-256 digest")


def validate_structural_version_diff(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="structural-version-diff",
        lifecycle="derived-replaceable",
        extra_keys={
            "diff_id",
            "document_id",
            "from_version",
            "to_version",
            "source_diff",
            "node_diff",
            "relation_diff",
            "summary",
            "payload",
            "field_provenance",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "structural-version-diff", errors)
    roles = {
        "from-version": "document-version",
        "to-version": "document-version",
        "from-reviewed": "reviewed-argument-ir",
        "to-reviewed": "reviewed-argument-ir",
        "from-ir": "argument-ir",
        "to-ir": "argument-ir",
    }
    _require_parent_roles(item, set(roles), errors)
    _require_parent_artifacts(item, roles, errors)
    from_version = item.get("from_version")
    to_version = item.get("to_version")
    for field, version in (("from_version", from_version), ("to_version", to_version)):
        if not isinstance(version, str) or re.fullmatch(r"V[1-9][0-9]*", version) is None:
            errors.append(f"{field} must be V1..Vn")
    if from_version == to_version:
        errors.append("structural diff versions must be distinct")
    if item.get("diff_id") != f"{from_version}--{to_version}":
        errors.append("diff_id must be <from_version>--<to_version>")
    if not _nonempty(item.get("document_id")):
        errors.append("document_id must be non-empty")

    source_diff = item.get("source_diff")
    if not isinstance(source_diff, dict):
        errors.append("source_diff must be an object")
    else:
        _strict_keys(
            source_diff,
            {"from_sha256", "to_sha256", "changed", "hunks"},
            "source_diff",
            errors,
        )
        for field in ("from_sha256", "to_sha256"):
            if not _digest(source_diff.get(field)):
                errors.append(f"source_diff.{field} must be a SHA-256 digest")
        if not isinstance(source_diff.get("changed"), bool):
            errors.append("source_diff.changed must be boolean")
        hunks = source_diff.get("hunks")
        if not isinstance(hunks, list):
            errors.append("source_diff.hunks must be an array")
        else:
            for index, hunk in enumerate(hunks):
                label = f"source_diff.hunks[{index}]"
                if not isinstance(hunk, dict):
                    errors.append(f"{label} must be an object")
                    continue
                _strict_keys(
                    hunk,
                    {
                        "tag",
                        "from_start_index",
                        "from_end_index",
                        "to_start_index",
                        "to_end_index",
                        "from_lines",
                        "to_lines",
                    },
                    label,
                    errors,
                )
                if hunk.get("tag") not in {"replace", "delete", "insert"}:
                    errors.append(f"{label}.tag must be replace/delete/insert")
                for field in (
                    "from_start_index",
                    "from_end_index",
                    "to_start_index",
                    "to_end_index",
                ):
                    raw = hunk.get(field)
                    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                        errors.append(f"{label}.{field} must be a non-negative integer")
                for field in ("from_lines", "to_lines"):
                    lines = hunk.get(field)
                    if not isinstance(lines, list) or any(
                        not isinstance(line, str) for line in lines
                    ):
                        errors.append(f"{label}.{field} must be an array of strings")
            if source_diff.get("changed") != bool(hunks):
                errors.append("source_diff.changed must equal whether hunks exist")

    node_diff = item.get("node_diff")
    node_counts = {
        "exact_unchanged": 0,
        "literal_anchor_modified": 0,
        "removed": 0,
        "added": 0,
    }
    evidence_added = 0
    if not isinstance(node_diff, dict):
        errors.append("node_diff must be an object")
    else:
        _strict_keys(node_diff, set(node_counts), "node_diff", errors)
        for status in node_counts:
            entries = node_diff.get(status)
            if not isinstance(entries, list):
                errors.append(f"node_diff.{status} must be an array")
                continue
            node_counts[status] = len(entries)
            for index, entry in enumerate(entries):
                _validate_structural_node_entry(
                    entry,
                    f"node_diff.{status}[{index}]",
                    errors,
                    status=status,
                )
                if status == "added" and isinstance(entry, dict) and entry.get("kind") == "evidence":
                    evidence_added += 1

    relation_diff = item.get("relation_diff")
    relation_counts = {"exact_unchanged": 0, "removed": 0, "added": 0}
    if not isinstance(relation_diff, dict):
        errors.append("relation_diff must be an object")
    else:
        _strict_keys(relation_diff, set(relation_counts), "relation_diff", errors)
        for status in relation_counts:
            entries = relation_diff.get(status)
            if not isinstance(entries, list):
                errors.append(f"relation_diff.{status} must be an array")
                continue
            relation_counts[status] = len(entries)
            for index, entry in enumerate(entries):
                _validate_structural_relation_entry(
                    entry,
                    f"relation_diff.{status}[{index}]",
                    errors,
                    paired=status == "exact_unchanged",
                )

    summary = item.get("summary")
    expected_summary = {
        "source_hunks": len(source_diff.get("hunks", [])) if isinstance(source_diff, dict) and isinstance(source_diff.get("hunks"), list) else 0,
        "nodes_exact_unchanged": node_counts["exact_unchanged"],
        "nodes_literal_anchor_modified": node_counts["literal_anchor_modified"],
        "nodes_removed": node_counts["removed"],
        "nodes_added": node_counts["added"],
        "evidence_added": evidence_added,
        "relations_exact_unchanged": relation_counts["exact_unchanged"],
        "relations_removed": relation_counts["removed"],
        "relations_added": relation_counts["added"],
    }
    if not isinstance(summary, dict):
        errors.append("summary must be an object")
    else:
        _strict_keys(summary, set(expected_summary), "summary", errors)
        if summary != expected_summary:
            errors.append("summary must equal deterministic diff counts")
    _validate_bound_file(item.get("payload"), "payload", errors)
    provenance = item.get("field_provenance")
    fields = {"source_diff", "node_diff", "relation_diff", "summary", "payload"}
    if not isinstance(provenance, dict):
        errors.append("field_provenance must be an object")
    else:
        _strict_keys(provenance, fields, "field_provenance", errors)
        for field in fields:
            entry = provenance.get(field)
            if not isinstance(entry, dict):
                errors.append(f"field_provenance.{field} must be an object")
                continue
            _strict_keys(entry, {"origin", "source"}, f"field_provenance.{field}", errors)
            if entry.get("origin") != "deterministic":
                errors.append(f"field_provenance.{field}.origin must be deterministic")
            if not _nonempty(entry.get("source")):
                errors.append(f"field_provenance.{field}.source must be non-empty")
    return errors


def validate_lineage_analysis_run(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="lineage-analysis-run",
        lifecycle="immutable",
        extra_keys={
            "lineage_id",
            "document_id",
            "from_version",
            "to_version",
            "from_reviewed_record",
            "to_reviewed_record",
            "from_ir",
            "to_ir",
            "structural_diff",
            "prompt",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "lineage-analysis-run", errors)
    roles = {
        "from-reviewed": "reviewed-argument-ir",
        "to-reviewed": "reviewed-argument-ir",
        "from-ir": "argument-ir",
        "to-ir": "argument-ir",
        "structural-diff": "structural-version-diff",
    }
    _require_parent_roles(item, set(roles), errors)
    _require_parent_artifacts(item, roles, errors)
    from_version = item.get("from_version")
    to_version = item.get("to_version")
    for field, version in (("from_version", from_version), ("to_version", to_version)):
        if not isinstance(version, str) or re.fullmatch(r"V[1-9][0-9]*", version) is None:
            errors.append(f"{field} must be V1..Vn")
    if item.get("lineage_id") != f"{from_version}--{to_version}":
        errors.append("lineage_id must be <from_version>--<to_version>")
    if not _nonempty(item.get("document_id")):
        errors.append("document_id must be non-empty")
    for field in (
        "from_reviewed_record",
        "to_reviewed_record",
        "from_ir",
        "to_ir",
        "structural_diff",
        "prompt",
    ):
        _validate_bound_file(item.get(field), field, errors)
    return errors


def validate_lineage_proposal_attempt(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="lineage-proposal-attempt",
        lifecycle="immutable",
        extra_keys={"lineage_id", "attempt_id", "collection", "response", "validation"},
    )
    if item is None:
        return errors
    _require_origin(item, {"model-derived"}, "lineage-proposal-attempt", errors)
    _require_parent_roles(item, {"analysis-run"}, errors)
    _require_parent_artifacts(item, {"analysis-run": "lineage-analysis-run"}, errors)
    if not isinstance(item.get("lineage_id"), str) or re.fullmatch(
        r"V[1-9][0-9]*--V[1-9][0-9]*", str(item.get("lineage_id"))
    ) is None:
        errors.append("lineage_id must be Vn--Vm")
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
            errors.append("valid lineage proposal requires validation.errors=[]")
        if validation.get("status") == "unusable" and not validation_errors:
            errors.append("unusable lineage proposal requires concrete errors")
    return errors


def _validate_lineage_relation_shape(
    relation: object,
    from_claims: list[str],
    to_claims: list[str],
    label: str,
    errors: list[str],
) -> None:
    if relation not in LINEAGE_RELATIONS:
        errors.append(f"{label}.relation must be one of {LINEAGE_RELATIONS}")
        return
    if relation in {"unchanged", "modified"} and (
        len(from_claims) != 1 or len(to_claims) != 1
    ):
        errors.append(f"{label}.{relation} requires one Claim on each side")
    if relation == "split" and (len(from_claims) != 1 or len(to_claims) < 2):
        errors.append(f"{label}.split requires one source and at least two descendants")
    if relation == "merged" and (len(from_claims) < 2 or len(to_claims) != 1):
        errors.append(f"{label}.merged requires at least two sources and one descendant")
    if relation == "removed" and (not from_claims or to_claims):
        errors.append(f"{label}.removed requires from_claims only")
    if relation == "new" and (from_claims or not to_claims):
        errors.append(f"{label}.new requires to_claims only")
    if relation == "uncertain" and not (from_claims or to_claims):
        errors.append(f"{label}.uncertain requires at least one Claim")


def validate_claim_lineage_proposals(value: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["claim-lineage-proposals must be a JSON object"]
    _strict_keys(
        value,
        {"schema_version", "artifact", "source", "status", "unverified", "proposals"},
        "claim-lineage-proposals",
        errors,
    )
    if value.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if value.get("artifact") != "claim-lineage-proposals":
        errors.append("artifact must be claim-lineage-proposals")
    source = value.get("source")
    if not isinstance(source, dict):
        errors.append("source must be an object")
    else:
        _strict_keys(
            source,
            {"structural_diff_sha256", "from_ir_sha256", "to_ir_sha256"},
            "source",
            errors,
        )
        for field in ("structural_diff_sha256", "from_ir_sha256", "to_ir_sha256"):
            if not _digest(source.get(field)):
                errors.append(f"source.{field} must be a SHA-256 digest")
    status = value.get("status")
    if status not in LINEAGE_RUN_STATUSES:
        errors.append(f"status must be one of {LINEAGE_RUN_STATUSES}")
    unverified = _string_list(value.get("unverified"), "unverified", errors)
    if status == "complete" and unverified:
        errors.append("complete lineage proposal requires unverified=[]")
    if status in {"partial", "blocked"} and not unverified:
        errors.append(f"{status} lineage proposal requires unverified items")
    proposals = value.get("proposals")
    if not isinstance(proposals, list):
        errors.append("proposals must be an array")
        return errors
    if status == "blocked" and proposals:
        errors.append("blocked lineage proposal must not contain proposals")
    expected_keys = {
        "proposal_id",
        "from_claims",
        "to_claims",
        "relation",
        "semantic_changes",
        "reason",
        "basis_refs",
        "uncertainty",
    }
    proposal_ids: list[str] = []
    for index, proposal in enumerate(proposals):
        label = f"proposals[{index}]"
        if not isinstance(proposal, dict):
            errors.append(f"{label} must be an object")
            continue
        _strict_keys(proposal, expected_keys, label, errors)
        expected_id = f"LP{index + 1}"
        if proposal.get("proposal_id") != expected_id:
            errors.append(f"{label}.proposal_id must be {expected_id}")
        proposal_ids.append(str(proposal.get("proposal_id")))
        from_claims = _string_list(proposal.get("from_claims"), f"{label}.from_claims", errors)
        to_claims = _string_list(proposal.get("to_claims"), f"{label}.to_claims", errors)
        for field, claims in (("from_claims", from_claims), ("to_claims", to_claims)):
            if len(claims) != len(set(claims)):
                errors.append(f"{label}.{field} must not repeat Claims")
            for claim in claims:
                if _VERSIONED_CLAIM.fullmatch(claim) is None:
                    errors.append(f"{label}.{field} contains invalid Claim {claim!r}")
        relation = proposal.get("relation")
        _validate_lineage_relation_shape(relation, from_claims, to_claims, label, errors)
        semantic_changes = _string_list(
            proposal.get("semantic_changes"),
            f"{label}.semantic_changes",
            errors,
        )
        unknown_changes = sorted(set(semantic_changes) - set(SEMANTIC_CHANGE_TYPES))
        if unknown_changes:
            errors.append(f"{label}.semantic_changes contains unknown values: {unknown_changes}")
        if relation == "unchanged" and semantic_changes:
            errors.append(f"{label}.unchanged requires semantic_changes=[]")
        if relation in {"modified", "split", "merged"} and not semantic_changes:
            errors.append(f"{label}.{relation} requires semantic_changes")
        if not _nonempty(proposal.get("reason")):
            errors.append(f"{label}.reason must be non-empty")
        basis_refs = _string_list(
            proposal.get("basis_refs"), f"{label}.basis_refs", errors, allow_empty=False
        )
        for reference in basis_refs:
            if re.fullmatch(r"V[1-9][0-9]*:[CEAZ][1-9][0-9]*", reference) is None:
                errors.append(f"{label}.basis_refs contains invalid node {reference!r}")
        if not set(from_claims + to_claims).issubset(set(basis_refs)):
            errors.append(f"{label}.basis_refs must include every linked Claim")
        if not isinstance(proposal.get("uncertainty"), str):
            errors.append(f"{label}.uncertainty must be a string")
    if len(proposal_ids) != len(set(proposal_ids)):
        errors.append("proposal IDs must not repeat")
    return errors


def validate_claim_lineage_index(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="claim-lineage-index",
        lifecycle="derived-replaceable",
        extra_keys={
            "lineage_id",
            "attempt_id",
            "from_version",
            "to_version",
            "run_status",
            "unverified",
            "summary",
            "proposals",
            "payload",
            "field_provenance",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "claim-lineage-index", errors)
    parents = item.get("parents")
    roles = {"analysis-run", "proposal-attempt", "proposal-result"}
    if isinstance(parents, list):
        roles.update(
            str(parent.get("role"))
            for parent in parents
            if isinstance(parent, dict)
            and str(parent.get("role", "")).startswith("lineage-")
        )
    artifacts = {
        "analysis-run": "lineage-analysis-run",
        "proposal-attempt": "lineage-proposal-attempt",
        "proposal-result": "claim-lineage-proposals",
        **{role: "claim-lineage" for role in roles if role.startswith("lineage-")},
    }
    _require_parent_roles(item, roles, errors)
    _require_parent_artifacts(item, artifacts, errors)
    if not isinstance(item.get("lineage_id"), str) or re.fullmatch(
        r"V[1-9][0-9]*--V[1-9][0-9]*", str(item.get("lineage_id"))
    ) is None:
        errors.append("lineage_id must be Vn--Vm")
    if not isinstance(item.get("attempt_id"), str) or re.fullmatch(
        r"attempt-[0-9]{4}", str(item.get("attempt_id"))
    ) is None:
        errors.append("attempt_id must be attempt-NNNN")
    if item.get("run_status") not in LINEAGE_RUN_STATUSES:
        errors.append(f"run_status must be one of {LINEAGE_RUN_STATUSES}")
    _string_list(item.get("unverified"), "unverified", errors)
    proposals = item.get("proposals")
    if not isinstance(proposals, list):
        errors.append("proposals must be an array")
        proposal_count = 0
    else:
        proposal_count = len(proposals)
        expected_keys = {
            "proposal_id",
            "lineage_artifact_id",
            "from_claims",
            "to_claims",
            "relation",
            "semantic_changes",
            "reason",
            "basis_refs",
            "uncertainty",
        }
        for index, proposal in enumerate(proposals):
            label = f"proposals[{index}]"
            if not isinstance(proposal, dict):
                errors.append(f"{label} must be an object")
                continue
            _strict_keys(proposal, expected_keys, label, errors)
            if proposal.get("proposal_id") != f"LP{index + 1}":
                errors.append(f"{label}.proposal_id must be LP{index + 1}")
            if not _nonempty(proposal.get("lineage_artifact_id")):
                errors.append(f"{label}.lineage_artifact_id must be non-empty")
    summary = item.get("summary")
    expected_summary = {relation: 0 for relation in LINEAGE_RELATIONS}
    if isinstance(proposals, list):
        for proposal in proposals:
            if isinstance(proposal, dict) and proposal.get("relation") in expected_summary:
                expected_summary[str(proposal["relation"])] += 1
    expected_summary["total"] = proposal_count
    if not isinstance(summary, dict):
        errors.append("summary must be an object")
    else:
        _strict_keys(summary, set(expected_summary), "summary", errors)
        if summary != expected_summary:
            errors.append("summary must equal proposal relation counts")
    _validate_bound_file(item.get("payload"), "payload", errors)
    provenance = item.get("field_provenance")
    expected_origins = {
        "proposals": "model-derived",
        "run_status": "model-derived",
        "unverified": "model-derived",
        "lineage_artifact_id": "deterministic",
        "summary": "deterministic",
        "payload": "deterministic",
    }
    if not isinstance(provenance, dict):
        errors.append("field_provenance must be an object")
    else:
        _strict_keys(provenance, set(expected_origins), "field_provenance", errors)
        for field, origin in expected_origins.items():
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


def validate_claim_lineage(value: object) -> list[str]:
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    extra_keys = {
        "lineage_id",
        "from_claims",
        "to_claims",
        "relation",
        "proposed_by",
        "proposal_sha256",
        "status",
    }
    if schema_version in {2, 3}:
        extra_keys.update(
            {"semantic_changes", "reason", "basis_refs", "uncertainty"}
        )
    if schema_version == 3:
        extra_keys.update(
            {"review_action", "human_note", "supersedes_sha256"}
        )
    errors, item = _validate_base(
        value,
        artifact="claim-lineage",
        lifecycle="immutable",
        extra_keys=extra_keys,
        schema_versions=(1, 2, 3),
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
    if schema_version in {2, 3}:
        semantic_changes = _string_list(
            item.get("semantic_changes"), "semantic_changes", errors
        )
        unknown_changes = sorted(set(semantic_changes) - set(SEMANTIC_CHANGE_TYPES))
        if unknown_changes:
            errors.append(
                f"semantic_changes contains unknown values: {unknown_changes}"
            )
        if relation == "unchanged" and semantic_changes:
            errors.append("unchanged lineage requires semantic_changes=[]")
        if relation in {"modified", "split", "merged"} and not semantic_changes:
            errors.append(f"{relation} lineage requires semantic_changes")
        if not _nonempty(item.get("reason")):
            errors.append("reason must be non-empty")
        basis_refs = _string_list(
            item.get("basis_refs"), "basis_refs", errors, allow_empty=False
        )
        for reference in basis_refs:
            if re.fullmatch(r"V[1-9][0-9]*:[CEAZ][1-9][0-9]*", reference) is None:
                errors.append(f"basis_refs contains invalid node: {reference}")
        if not set(from_claims + to_claims).issubset(set(basis_refs)):
            errors.append("basis_refs must include every linked Claim")
        if not isinstance(item.get("uncertainty"), str):
            errors.append("uncertainty must be a string")
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
    if schema_version == 2 and status == "proposed":
        required_parent_roles.update({"proposal-attempt", "proposal-result"})
    if status in {"human_confirmed", "rejected"} and proposed_by == "model":
        required_parent_roles.add("proposal")
    if schema_version == 3:
        review_action = item.get("review_action")
        if review_action not in {"confirm", "reject", "correct"}:
            errors.append("review_action must be confirm/reject/correct")
        if status == "rejected" and review_action != "reject":
            errors.append("rejected lineage requires review_action=reject")
        if status == "human_confirmed" and review_action not in {"confirm", "correct"}:
            errors.append("human_confirmed lineage requires confirm or correct")
        if not _nonempty(item.get("human_note")):
            errors.append("human_note must be non-empty")
        supersedes_sha256 = item.get("supersedes_sha256")
        if supersedes_sha256 is not None:
            required_parent_roles.add("supersedes")
            if not _digest(supersedes_sha256):
                errors.append("supersedes_sha256 must be null or a SHA-256 digest")
    if set(parent_by_role) != required_parent_roles:
        errors.append(
            f"claim-lineage parent roles must be exactly {sorted(required_parent_roles)}"
        )
    expected_parent_artifacts = {
        role: (
            "claim-lineage"
            if role == "proposal"
            else "claim-lineage"
            if role == "supersedes"
            else "lineage-proposal-attempt"
            if role == "proposal-attempt"
            else "claim-lineage-proposals"
            if role == "proposal-result"
            else "argument-ir"
        )
        for role in required_parent_roles
    }
    _require_parent_artifacts(item, expected_parent_artifacts, errors)
    if schema_version == 3 and item.get("supersedes_sha256") is not None:
        supersedes_parent = parent_by_role.get("supersedes")
        if isinstance(supersedes_parent, dict) and supersedes_parent.get("sha256") != item.get("supersedes_sha256"):
            errors.append("supersedes parent hash must equal supersedes_sha256")
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

__all__ = [name for name in globals() if not name.startswith("__")]
