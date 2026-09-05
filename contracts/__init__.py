"""Public compatibility surface for all artifact contracts."""

from .base import *
from .core import *
from .lineage import *
from .resolution import *
from .gate_b import *
from .citations import *
from .gate_a import *
from .review import *

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
    "structural-version-diff": validate_structural_version_diff,
    "lineage-analysis-run": validate_lineage_analysis_run,
    "lineage-proposal-attempt": validate_lineage_proposal_attempt,
    "claim-lineage-proposals": validate_claim_lineage_proposals,
    "claim-lineage-index": validate_claim_lineage_index,
    "resolution-retest-run": validate_resolution_retest_run,
    "resolution-result-attempt": validate_resolution_result_attempt,
    "resolution-retest-results": validate_resolution_retest_results,
    "finding-resolution-proposal": validate_finding_resolution_proposal,
    "finding-resolution-decision": validate_finding_resolution_decision,
    "product-gate-b-corpus": validate_gate_b_corpus,
    "product-gate-b-assessment": validate_gate_b_assessment,
    "product-gate-b-decision": validate_gate_b_decision,
    "product-gate-b-report": validate_gate_b_report,
    "citation-audit-results": validate_citation_audit_results,
    "citation-audit-run": validate_citation_audit_run,
    "citation-result-attempt": validate_citation_result_attempt,
    "citation-verification-decision": validate_citation_verification_decision,
    "citation-provenance-index": validate_citation_provenance_index,
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

__all__ = [name for name in globals() if not name.startswith("__")]
