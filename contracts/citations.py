"""Artifact contract validators grouped by family."""

from __future__ import annotations

from .base import *  # noqa: F403
from .core import *  # noqa: F403

def _validate_citation_dimensions(
    value: object,
    label: str,
    errors: list[str],
) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return
    _strict_keys(
        value,
        {
            "bibliographic_existence",
            "exact_source_located",
            "content_support",
            "context_preserved",
            "uncertainty",
        },
        label,
        errors,
    )
    if value.get("bibliographic_existence") not in CITATION_BIBLIOGRAPHIC_STATUSES:
        errors.append(
            f"{label}.bibliographic_existence must be one of "
            f"{CITATION_BIBLIOGRAPHIC_STATUSES}"
        )
    if value.get("exact_source_located") not in CITATION_SOURCE_LOCATION_STATUSES:
        errors.append(
            f"{label}.exact_source_located must be one of "
            f"{CITATION_SOURCE_LOCATION_STATUSES}"
        )
    if value.get("content_support") not in CITATION_CONTENT_SUPPORT_STATUSES:
        errors.append(
            f"{label}.content_support must be one of "
            f"{CITATION_CONTENT_SUPPORT_STATUSES}"
        )
    if value.get("context_preserved") not in CITATION_CONTEXT_STATUSES:
        errors.append(
            f"{label}.context_preserved must be one of {CITATION_CONTEXT_STATUSES}"
        )
    if not isinstance(value.get("uncertainty"), str):
        errors.append(f"{label}.uncertainty must be a string")
    if value.get("exact_source_located") != "verified":
        if value.get("content_support") != "uncertain":
            errors.append(
                f"{label}.content_support must be uncertain until the exact source is located"
            )
        if value.get("context_preserved") != "uncertain":
            errors.append(
                f"{label}.context_preserved must be uncertain until the exact source is located"
            )
    if value.get("bibliographic_existence") == "contradicted" and value.get(
        "exact_source_located"
    ) == "verified":
        errors.append(
            f"{label} cannot locate an exact source after contradicting bibliographic existence"
        )


def validate_citation_audit_results(value: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["citation-audit-results must be a JSON object"]
    _strict_keys(
        value,
        {
            "schema_version",
            "artifact",
            "source",
            "status",
            "sources",
            "outcomes",
            "unverified",
        },
        "citation-audit-results",
        errors,
    )
    if value.get("schema_version") != 1:
        errors.append("citation-audit-results schema_version must be 1")
    if value.get("artifact") != "citation-audit-results":
        errors.append("artifact must be citation-audit-results")
    source = value.get("source")
    if not isinstance(source, dict):
        errors.append("source must be an object")
    else:
        _strict_keys(
            source,
            {"audit_context_sha256", "reviewed_ir_sha256", "source_sha256"},
            "source",
            errors,
        )
        for key in ("audit_context_sha256", "reviewed_ir_sha256", "source_sha256"):
            if not _digest(source.get(key)):
                errors.append(f"source.{key} must be a lowercase SHA-256 digest")
    if value.get("status") not in CITATION_AUDIT_STATUSES:
        errors.append(f"status must be one of {CITATION_AUDIT_STATUSES}")

    sources = value.get("sources")
    source_ids: set[str] = set()
    source_dimensions: dict[str, set[str]] = {}
    source_kinds: dict[str, str] = {}
    if not isinstance(sources, list):
        errors.append("sources must be an array")
        sources = []
    for index, source_item in enumerate(sources):
        label = f"sources[{index}]"
        if not isinstance(source_item, dict):
            errors.append(f"{label} must be an object")
            continue
        _strict_keys(
            source_item,
            {"source_id", "kind", "title", "locator", "accessed_at", "dimensions", "note"},
            label,
            errors,
        )
        expected_id = f"S{index + 1}"
        source_id = source_item.get("source_id")
        if source_id != expected_id:
            errors.append(f"{label}.source_id must be {expected_id}")
        elif isinstance(source_id, str):
            source_ids.add(source_id)
        kind = source_item.get("kind")
        if kind not in CITATION_SOURCE_KINDS:
            errors.append(f"{label}.kind must be one of {CITATION_SOURCE_KINDS}")
        elif isinstance(source_id, str):
            source_kinds[source_id] = str(kind)
        for key in ("title", "locator", "note"):
            if not _nonempty(source_item.get(key)):
                errors.append(f"{label}.{key} must be a non-empty string")
        if not _timestamp(source_item.get("accessed_at")):
            errors.append(f"{label}.accessed_at must be a timezone-aware ISO timestamp")
        dimensions = _string_list(
            source_item.get("dimensions"),
            f"{label}.dimensions",
            errors,
            allow_empty=False,
        )
        unknown_dimensions = sorted(set(dimensions) - set(CITATION_SOURCE_DIMENSIONS))
        if unknown_dimensions:
            errors.append(f"{label}.dimensions contains unknown values: {unknown_dimensions}")
        if isinstance(source_id, str):
            source_dimensions[source_id] = set(dimensions)

    outcomes = value.get("outcomes")
    if not isinstance(outcomes, list):
        errors.append("outcomes must be an array")
        outcomes = []
    if value.get("status") != "blocked" and not outcomes:
        errors.append("non-blocked citation audit must contain outcomes")
    citation_ids: list[str] = []
    for index, outcome in enumerate(outcomes):
        label = f"outcomes[{index}]"
        if not isinstance(outcome, dict):
            errors.append(f"{label} must be an object")
            continue
        _strict_keys(
            outcome,
            {
                "citation_id",
                "bibliographic_existence",
                "exact_source_located",
                "content_support",
                "context_preserved",
                "reason",
                "source_refs",
                "uncertainty",
            },
            label,
            errors,
        )
        citation_id = outcome.get("citation_id")
        if not isinstance(citation_id, str) or re.fullmatch(r"Z[1-9][0-9]*", citation_id) is None:
            errors.append(f"{label}.citation_id must be an Argument IR Citation ID")
        else:
            citation_ids.append(citation_id)
        dimensions = {
            key: outcome.get(key)
            for key in (
                "bibliographic_existence",
                "exact_source_located",
                "content_support",
                "context_preserved",
                "uncertainty",
            )
        }
        _validate_citation_dimensions(dimensions, label, errors)
        if not _nonempty(outcome.get("reason")):
            errors.append(f"{label}.reason must be a non-empty string")
        refs = _string_list(outcome.get("source_refs"), f"{label}.source_refs", errors)
        unknown_refs = sorted(set(refs) - source_ids)
        if unknown_refs:
            errors.append(f"{label}.source_refs contains unknown sources: {unknown_refs}")
        definitive = any(
            outcome.get(key) not in {"uncertain", "not_verified"}
            for key in (
                "bibliographic_existence",
                "exact_source_located",
                "content_support",
                "context_preserved",
            )
        )
        if definitive and not refs:
            errors.append(f"{label} definitive judgments require source_refs")
        required_dimensions = {
            "bibliographic_existence": outcome.get("bibliographic_existence")
            in {"verified", "contradicted"},
            "exact_source_located": outcome.get("exact_source_located") == "verified",
            "content_support": outcome.get("content_support")
            in {"supports", "partially_supports", "does_not_support"},
            "context_preserved": outcome.get("context_preserved") in {"yes", "no"},
        }
        for dimension, required in required_dimensions.items():
            if required and not any(
                dimension in source_dimensions.get(ref, set()) for ref in refs
            ):
                errors.append(f"{label} lacks source evidence for {dimension}")
        if outcome.get("exact_source_located") == "verified" and not any(
            source_kinds.get(ref) in {"primary", "repository"}
            and "exact_source_located" in source_dimensions.get(ref, set())
            for ref in refs
        ):
            errors.append(
                f"{label}.exact_source_located requires a primary or repository source"
            )
        if outcome.get("content_support") in {
            "supports",
            "partially_supports",
            "does_not_support",
        } and not any(
            source_kinds.get(ref) in {"primary", "repository"}
            and "content_support" in source_dimensions.get(ref, set())
            for ref in refs
        ):
            errors.append(f"{label}.content_support requires primary-source evidence")
    if len(citation_ids) != len(set(citation_ids)):
        errors.append("outcomes must not repeat Citation IDs")
    if value.get("status") == "complete":
        for index, outcome in enumerate(outcomes):
            if isinstance(outcome, dict) and any(
                outcome.get(key) in {"uncertain", "not_verified"}
                for key in (
                    "bibliographic_existence",
                    "exact_source_located",
                    "content_support",
                    "context_preserved",
                )
            ):
                errors.append(
                    f"outcomes[{index}] has unresolved dimensions, so status cannot be complete"
                )
    unverified = _string_list(value.get("unverified"), "unverified", errors)
    if value.get("status") == "complete" and unverified:
        errors.append("complete citation audit must not list unverified items")
    if value.get("status") in {"partial", "blocked"} and not unverified:
        errors.append("partial or blocked citation audit must list what remains unverified")
    return errors


def validate_citation_audit_run(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="citation-audit-run",
        lifecycle="immutable",
        extra_keys={
            "audit_id",
            "document_id",
            "version_id",
            "selected_citations",
            "context",
            "reviewed_ir",
            "prompt",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "citation-audit-run", errors)
    _require_parent_roles(item, {"document-version", "reviewed-record", "reviewed-ir"}, errors)
    _require_parent_artifacts(
        item,
        {
            "document-version": "document-version",
            "reviewed-record": "reviewed-argument-ir",
            "reviewed-ir": "argument-ir",
        },
        errors,
    )
    if re.fullmatch(r"CA[1-9][0-9]*", str(item.get("audit_id", ""))) is None:
        errors.append("audit_id must be CA1..CAn")
    if not _nonempty(item.get("document_id")):
        errors.append("document_id must be a non-empty string")
    if re.fullmatch(r"V[1-9][0-9]*", str(item.get("version_id", ""))) is None:
        errors.append("version_id must be V1..Vn")
    selected = _string_list(
        item.get("selected_citations"),
        "selected_citations",
        errors,
        allow_empty=False,
    )
    if any(re.fullmatch(r"Z[1-9][0-9]*", citation_id) is None for citation_id in selected):
        errors.append("selected_citations must contain Argument IR Citation IDs")
    _validate_bound_file(item.get("reviewed_ir"), "reviewed_ir", errors)
    _validate_bound_file(item.get("context"), "context", errors)
    _validate_bound_file(item.get("prompt"), "prompt", errors)
    return errors


def validate_citation_result_attempt(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="citation-result-attempt",
        lifecycle="immutable",
        extra_keys={"audit_id", "attempt_id", "collection", "response", "validation"},
    )
    if item is None:
        return errors
    _require_origin(item, {"model-derived"}, "citation-result-attempt", errors)
    _require_parent_roles(item, {"citation-audit-run"}, errors)
    _require_parent_artifacts(item, {"citation-audit-run": "citation-audit-run"}, errors)
    if re.fullmatch(r"CA[1-9][0-9]*", str(item.get("audit_id", ""))) is None:
        errors.append("audit_id must be CA1..CAn")
    if re.fullmatch(r"attempt-[0-9]{4}", str(item.get("attempt_id", ""))) is None:
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
        if collection.get("producer_label") is not None and not _nonempty(
            collection.get("producer_label")
        ):
            errors.append("collection.producer_label must be null or a non-empty string")
    _validate_bound_file(item.get("response"), "response", errors)
    validation = item.get("validation")
    if not isinstance(validation, dict):
        errors.append("validation must be an object")
    else:
        _strict_keys(validation, {"status", "errors"}, "validation", errors)
        if validation.get("status") not in {"valid", "unusable"}:
            errors.append("validation.status must be valid or unusable")
        _string_list(validation.get("errors"), "validation.errors", errors)
        if validation.get("status") == "valid" and validation.get("errors") != []:
            errors.append("valid citation attempt must have no validation errors")
        if validation.get("status") == "unusable" and validation.get("errors") == []:
            errors.append("unusable citation attempt must explain its validation errors")
    return errors


def validate_citation_verification_decision(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="citation-verification-decision",
        lifecycle="immutable",
        extra_keys={
            "decision_id",
            "audit_id",
            "citation_id",
            "attempt_id",
            "decision",
            "reason",
            "final_outcome",
            "supersedes",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"human-confirmed"}, "citation-verification-decision", errors)
    parents = item.get("parents")
    actual_roles = {
        parent.get("role")
        for parent in parents
        if isinstance(parent, dict)
    } if isinstance(parents, list) else set()
    required = {"citation-audit-run", "result-attempt", "audit-results"}
    if not required.issubset(actual_roles) or actual_roles - required - {"previous-decision"}:
        errors.append(
            "citation decision parents must be run, attempt, results, and optional previous-decision"
        )
    _require_parent_artifacts(
        item,
        {
            "citation-audit-run": "citation-audit-run",
            "result-attempt": "citation-result-attempt",
            "audit-results": "citation-audit-results",
            "previous-decision": "citation-verification-decision",
        },
        errors,
    )
    if re.fullmatch(r"CD[0-9]{4}", str(item.get("decision_id", ""))) is None:
        errors.append("decision_id must be CDNNNN")
    if re.fullmatch(r"CA[1-9][0-9]*", str(item.get("audit_id", ""))) is None:
        errors.append("audit_id must be CA1..CAn")
    if re.fullmatch(r"Z[1-9][0-9]*", str(item.get("citation_id", ""))) is None:
        errors.append("citation_id must be an Argument IR Citation ID")
    if re.fullmatch(r"attempt-[0-9]{4}", str(item.get("attempt_id", ""))) is None:
        errors.append("attempt_id must be attempt-NNNN")
    decision = item.get("decision")
    if decision not in CITATION_VERIFICATION_DECISIONS:
        errors.append(f"decision must be one of {CITATION_VERIFICATION_DECISIONS}")
    if not _nonempty(item.get("reason")):
        errors.append("reason must be a non-empty string")
    final_outcome = item.get("final_outcome")
    if decision == "correct":
        _validate_citation_dimensions(final_outcome, "final_outcome", errors)
    elif final_outcome is not None:
        errors.append("final_outcome is allowed only for correct decisions")
    supersedes = item.get("supersedes")
    if supersedes is not None and not _digest(supersedes):
        errors.append("supersedes must be null or a lowercase SHA-256 digest")
    if supersedes is None and "previous-decision" in actual_roles:
        errors.append("first citation decision must not have previous-decision parent")
    if supersedes is not None and "previous-decision" not in actual_roles:
        errors.append("superseding citation decision requires previous-decision parent")
    return errors


def validate_citation_provenance_index(value: object) -> list[str]:
    errors, item = _validate_base(
        value,
        artifact="citation-provenance-index",
        lifecycle="derived-replaceable",
        extra_keys={
            "audit_id",
            "version_id",
            "selected_attempt_id",
            "citations",
            "evidence_dependencies",
            "claim_dependencies",
            "summary",
            "report",
            "field_provenance",
        },
    )
    if item is None:
        return errors
    _require_origin(item, {"deterministic"}, "citation-provenance-index", errors)
    parent_roles = {
        parent.get("role")
        for parent in item.get("parents", [])
        if isinstance(parent, dict)
    }
    if not {"citation-audit-run", "result-attempt", "audit-results"}.issubset(parent_roles):
        errors.append("citation index requires run, attempt, and results parents")
    _require_parent_artifacts(
        item,
        {
            "citation-audit-run": "citation-audit-run",
            "result-attempt": "citation-result-attempt",
            "audit-results": "citation-audit-results",
        },
        errors,
    )
    for parent in item.get("parents", []):
        if not isinstance(parent, dict):
            continue
        role = str(parent.get("role", ""))
        if role.startswith("decision-"):
            if re.fullmatch(r"decision-[0-9]{4}", role) is None:
                errors.append(f"invalid citation decision parent role: {role}")
            if parent.get("artifact") != "citation-verification-decision":
                errors.append(f"parent {role!r} artifact must be citation-verification-decision")
        elif role not in {"citation-audit-run", "result-attempt", "audit-results"}:
            errors.append(f"unexpected citation index parent role: {role}")
    if re.fullmatch(r"CA[1-9][0-9]*", str(item.get("audit_id", ""))) is None:
        errors.append("audit_id must be CA1..CAn")
    if re.fullmatch(r"V[1-9][0-9]*", str(item.get("version_id", ""))) is None:
        errors.append("version_id must be V1..Vn")
    if re.fullmatch(r"attempt-[0-9]{4}", str(item.get("selected_attempt_id", ""))) is None:
        errors.append("selected_attempt_id must be attempt-NNNN")
    citations = item.get("citations")
    if not isinstance(citations, list):
        errors.append("citations must be an array")
        citations = []
    seen_citations: set[str] = set()
    for index, citation in enumerate(citations):
        label = f"citations[{index}]"
        if not isinstance(citation, dict):
            errors.append(f"{label} must be an object")
            continue
        _strict_keys(
            citation,
            {
                "citation_id",
                "citation_text",
                "proposal",
                "human_decision",
                "final_outcome",
                "current_status",
                "verification_state",
                "source_refs",
            },
            label,
            errors,
        )
        citation_id = citation.get("citation_id")
        if not isinstance(citation_id, str) or re.fullmatch(r"Z[1-9][0-9]*", citation_id) is None:
            errors.append(f"{label}.citation_id is invalid")
        elif citation_id in seen_citations:
            errors.append(f"{label}.citation_id is duplicated")
        else:
            seen_citations.add(citation_id)
        if not _nonempty(citation.get("citation_text")):
            errors.append(f"{label}.citation_text must be non-empty")
        _validate_citation_dimensions(citation.get("proposal"), f"{label}.proposal", errors)
        if citation.get("human_decision") not in {None, *CITATION_VERIFICATION_DECISIONS}:
            errors.append(f"{label}.human_decision is invalid")
        final_outcome = citation.get("final_outcome")
        if final_outcome is not None:
            _validate_citation_dimensions(final_outcome, f"{label}.final_outcome", errors)
        if citation.get("current_status") not in {
            "model_proposed",
            "human_confirmed",
            "proposal_rejected",
        }:
            errors.append(f"{label}.current_status is invalid")
        if citation.get("verification_state") not in {
            "verified",
            "unverified",
        }:
            errors.append(f"{label}.verification_state must be verified or unverified")
        _string_list(citation.get("source_refs"), f"{label}.source_refs", errors)
        current_status = citation.get("current_status")
        human_decision = citation.get("human_decision")
        verification_state = citation.get("verification_state")
        if current_status == "model_proposed" and (
            human_decision is not None or final_outcome is not None
        ):
            errors.append(f"{label} model proposal must not masquerade as a human final outcome")
        if current_status == "proposal_rejected" and (
            human_decision != "reject" or final_outcome is not None
        ):
            errors.append(f"{label} rejected proposal must have reject and no final outcome")
        if current_status == "human_confirmed" and (
            human_decision not in {"confirm", "correct"} or final_outcome is None
        ):
            errors.append(f"{label} human-confirmed status requires a final human outcome")
        fully_verified = isinstance(final_outcome, dict) and (
            final_outcome.get("bibliographic_existence") == "verified"
            and final_outcome.get("exact_source_located") == "verified"
            and final_outcome.get("content_support") == "supports"
            and final_outcome.get("context_preserved") == "yes"
        )
        if verification_state == "verified" and not (
            current_status == "human_confirmed" and fully_verified
        ):
            errors.append(f"{label} verified state requires all four human-confirmed dimensions")
    for field, node_prefix in (("evidence_dependencies", "E"), ("claim_dependencies", "C")):
        dependencies = item.get(field)
        if not isinstance(dependencies, list):
            errors.append(f"{field} must be an array")
            continue
        for index, dependency in enumerate(dependencies):
            label = f"{field}[{index}]"
            if not isinstance(dependency, dict):
                errors.append(f"{label} must be an object")
                continue
            expected = {"node_id", "citation_ids", "status"}
            if field == "claim_dependencies":
                expected.add("evidence_ids")
            _strict_keys(dependency, expected, label, errors)
            if re.fullmatch(node_prefix + r"[1-9][0-9]*", str(dependency.get("node_id", ""))) is None:
                errors.append(f"{label}.node_id is invalid")
            citation_ids = _string_list(
                dependency.get("citation_ids"),
                f"{label}.citation_ids",
                errors,
                allow_empty=False,
            )
            unknown_citations = sorted(set(citation_ids) - seen_citations)
            if unknown_citations:
                errors.append(f"{label}.citation_ids contains unknown Citations: {unknown_citations}")
            if field == "claim_dependencies":
                evidence_ids = _string_list(dependency.get("evidence_ids"), f"{label}.evidence_ids", errors)
                if any(re.fullmatch(r"E[1-9][0-9]*", evidence_id) is None for evidence_id in evidence_ids):
                    errors.append(f"{label}.evidence_ids must contain Evidence IDs")
            if dependency.get("status") not in CITATION_DEPENDENCY_STATUSES:
                errors.append(f"{label}.status must be one of {CITATION_DEPENDENCY_STATUSES}")
    summary = item.get("summary")
    summary_keys = {
        "citations_total",
        "verified",
        "unverified",
        "human_confirmed",
        "human_pending",
        "proposal_rejected",
        "evidence_depending_on_unverified_citations",
        "claims_depending_on_unverified_evidence",
    }
    if not isinstance(summary, dict):
        errors.append("summary must be an object")
    else:
        _strict_keys(summary, summary_keys, "summary", errors)
        for key in summary_keys:
            if not isinstance(summary.get(key), int) or summary.get(key, -1) < 0:
                errors.append(f"summary.{key} must be a non-negative integer")
        if summary.get("citations_total") != len(citations):
            errors.append("summary.citations_total must equal citations length")
        if isinstance(summary.get("verified"), int) and isinstance(summary.get("unverified"), int):
            if summary["verified"] + summary["unverified"] != len(citations):
                errors.append("summary verified + unverified must equal citations_total")
        expected_counts = {
            "verified": sum(
                item.get("verification_state") == "verified"
                for item in citations
                if isinstance(item, dict)
            ),
            "unverified": sum(
                item.get("verification_state") == "unverified"
                for item in citations
                if isinstance(item, dict)
            ),
            "human_confirmed": sum(
                item.get("current_status") == "human_confirmed"
                for item in citations
                if isinstance(item, dict)
            ),
            "human_pending": sum(
                item.get("current_status") == "model_proposed"
                for item in citations
                if isinstance(item, dict)
            ),
            "proposal_rejected": sum(
                item.get("current_status") == "proposal_rejected"
                for item in citations
                if isinstance(item, dict)
            ),
            "evidence_depending_on_unverified_citations": sum(
                item.get("status") == "depends_on_unverified_evidence"
                for item in item.get("evidence_dependencies", [])
                if isinstance(item, dict)
            ),
            "claims_depending_on_unverified_evidence": sum(
                item.get("status") == "depends_on_unverified_evidence"
                for item in item.get("claim_dependencies", [])
                if isinstance(item, dict)
            ),
        }
        for key, expected in expected_counts.items():
            if summary.get(key) != expected:
                errors.append(f"summary.{key} must equal {expected}")
    _validate_bound_file(item.get("report"), "report", errors)
    field_provenance = item.get("field_provenance")
    if not isinstance(field_provenance, dict):
        errors.append("field_provenance must be an object")
    else:
        _strict_keys(
            field_provenance,
            {"model_outcomes", "human_decisions", "dependency_graph", "verification_state"},
            "field_provenance",
            errors,
        )
        expected_origins = {
            "model_outcomes": "model-derived",
            "human_decisions": "human-confirmed",
            "dependency_graph": "deterministic",
            "verification_state": "deterministic",
        }
        for key, origin in expected_origins.items():
            entry = field_provenance.get(key)
            if not isinstance(entry, dict):
                errors.append(f"field_provenance.{key} must be an object")
                continue
            _strict_keys(entry, {"origin", "source"}, f"field_provenance.{key}", errors)
            if entry.get("origin") != origin:
                errors.append(f"field_provenance.{key}.origin must be {origin}")
            if not _nonempty(entry.get("source")):
                errors.append(f"field_provenance.{key}.source must be non-empty")
    return errors



__all__ = [name for name in globals() if not name.startswith("__")]
