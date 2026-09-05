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
LINEAGE_RUN_STATUSES = ("complete", "partial", "blocked")
SEMANTIC_CHANGE_TYPES = (
    "scope_narrowed",
    "scope_broadened",
    "causal_strength_reduced",
    "causal_strength_increased",
    "qualification_added",
    "qualification_removed",
    "evidence_changed",
    "concept_reframed",
    "argument_role_changed",
    "wording_only",
    "other",
    "uncertain",
)
RESOLUTION_STATUSES = (
    "resolved",
    "partially_resolved",
    "unresolved",
    "obsolete",
    "uncertain",
)
RESOLUTION_RETEST_STATUSES = ("complete", "blocked")
RESOLUTION_DECISIONS = ("confirm", "reject", "correct")
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
GATE_B_DECISIONS = ("pass", "fail", "defer")
GATE_B_JUDGMENTS = ("yes", "no", "not_observed", "uncertain")
GATE_B_CLARITIES = ("clear", "confusing", "uncertain")
CITATION_AUDIT_STATUSES = ("complete", "partial", "blocked")
CITATION_BIBLIOGRAPHIC_STATUSES = (
    "verified",
    "not_verified",
    "contradicted",
    "uncertain",
)
CITATION_SOURCE_LOCATION_STATUSES = ("verified", "not_verified", "uncertain")
CITATION_CONTENT_SUPPORT_STATUSES = (
    "supports",
    "partially_supports",
    "does_not_support",
    "uncertain",
)
CITATION_CONTEXT_STATUSES = ("yes", "no", "uncertain")
CITATION_SOURCE_KINDS = ("primary", "repository", "catalog", "secondary", "other")
CITATION_SOURCE_DIMENSIONS = (
    "bibliographic_existence",
    "exact_source_located",
    "content_support",
    "context_preserved",
)
CITATION_VERIFICATION_DECISIONS = ("confirm", "reject", "correct")
CITATION_DEPENDENCY_STATUSES = (
    "citation_verified",
    "depends_on_unverified_evidence",
)
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



__all__ = [name for name in globals() if not name.startswith("__")]
