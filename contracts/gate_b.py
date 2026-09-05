"""Artifact contract validators grouped by family."""

from __future__ import annotations

from .base import *  # noqa: F403
from .core import *  # noqa: F403

def validate_gate_b_corpus(value: object) -> list[str]:
    errors, item = _validate_base(value, artifact="product-gate-b-corpus", lifecycle="immutable", extra_keys={"gate_id", "projects"})
    if item is None: return errors
    _require_origin(item, {"human-confirmed"}, "product-gate-b-corpus", errors)
    if not _nonempty(item.get("gate_id")): errors.append("gate_id must be non-empty")
    projects = item.get("projects")
    if not isinstance(projects, list) or not 2 <= len(projects) <= 3:
        errors.append("Gate B corpus must contain 2-3 real multi-version projects"); return errors
    aliases: list[str] = []
    for index, project in enumerate(projects):
        label = f"projects[{index}]"
        if not isinstance(project, dict): errors.append(f"{label} must be an object"); continue
        _strict_keys(project, {"alias", "locator", "project_id", "document_id", "versions", "bindings", "observed_relations"}, label, errors)
        for field in ("alias", "locator", "project_id", "document_id"):
            if not _nonempty(project.get(field)): errors.append(f"{label}.{field} must be non-empty")
        aliases.append(str(project.get("alias")))
        versions = _string_list(project.get("versions"), f"{label}.versions", errors, allow_empty=False)
        if len(versions) < 2 or any(re.fullmatch(r"V[1-9][0-9]*", version) is None for version in versions): errors.append(f"{label}.versions must contain at least two Version IDs")
        relations = _string_list(project.get("observed_relations"), f"{label}.observed_relations", errors)
        if any(relation not in LINEAGE_RELATIONS for relation in relations): errors.append(f"{label}.observed_relations contains invalid lineage relation")
        bindings = project.get("bindings")
        if not isinstance(bindings, dict): errors.append(f"{label}.bindings must be an object"); continue
        _strict_keys(bindings, {"project", "document_versions", "reviewed_irs", "lineage_decisions", "resolution_decisions"}, f"{label}.bindings", errors)
        if not _digest(bindings.get("project")): errors.append(f"{label}.bindings.project must be a digest")
        for field in ("document_versions", "reviewed_irs", "lineage_decisions", "resolution_decisions"):
            hashes = _string_list(bindings.get(field), f"{label}.bindings.{field}", errors, allow_empty=field == "resolution_decisions")
            if any(not _digest(digest) for digest in hashes): errors.append(f"{label}.bindings.{field} must contain digests")
        if isinstance(bindings.get("document_versions"), list) and len(bindings["document_versions"]) != len(versions): errors.append(f"{label}.document version bindings must match versions")
        if isinstance(bindings.get("reviewed_irs"), list) and len(bindings["reviewed_irs"]) != len(versions): errors.append(f"{label}.Reviewed IR bindings must match versions")
    if len(aliases) != len(set(aliases)): errors.append("Gate B project aliases must be unique")
    return errors


def validate_gate_b_assessment(value: object) -> list[str]:
    errors, item = _validate_base(value, artifact="product-gate-b-assessment", lifecycle="immutable", extra_keys={"assessment_id", "project_alias", "lineage_correction_minutes", "lineage_reasonable", "split_merge_worked", "finding_inheritance_correct", "resolved_stopped_reappearing", "unresolved_persisted", "revision_rationale_clarity", "notes"})
    if item is None: return errors
    _require_origin(item, {"human-confirmed"}, "product-gate-b-assessment", errors)
    _require_parent_roles(item, {"corpus"}, errors); _require_parent_artifacts(item, {"corpus": "product-gate-b-corpus"}, errors)
    for field in ("assessment_id", "project_alias"):
        if not _nonempty(item.get(field)): errors.append(f"{field} must be non-empty")
    minutes = item.get("lineage_correction_minutes")
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 0: errors.append("lineage_correction_minutes must be a non-negative integer")
    for field in ("lineage_reasonable", "split_merge_worked", "finding_inheritance_correct", "resolved_stopped_reappearing", "unresolved_persisted"):
        if item.get(field) not in GATE_B_JUDGMENTS: errors.append(f"{field} must be one of {GATE_B_JUDGMENTS}")
    if item.get("revision_rationale_clarity") not in GATE_B_CLARITIES: errors.append(f"revision_rationale_clarity must be one of {GATE_B_CLARITIES}")
    if not isinstance(item.get("notes"), str): errors.append("notes must be a string")
    return errors


def validate_gate_b_decision(value: object) -> list[str]:
    errors, item = _validate_base(value, artifact="product-gate-b-decision", lifecycle="immutable", extra_keys={"decision_id", "decision", "reason", "supersedes"})
    if item is None: return errors
    _require_origin(item, {"human-confirmed"}, "product-gate-b-decision", errors)
    for field in ("decision_id", "reason"):
        if not _nonempty(item.get(field)): errors.append(f"{field} must be non-empty")
    if item.get("decision") not in GATE_B_DECISIONS: errors.append(f"decision must be one of {GATE_B_DECISIONS}")
    supersedes = item.get("supersedes")
    if supersedes is not None and not _digest(supersedes): errors.append("supersedes must be null or a digest")
    roles = {"corpus"} if supersedes is None else {"corpus", "previous-decision"}
    artifacts = {"corpus": "product-gate-b-corpus"}
    if supersedes is not None: artifacts["previous-decision"] = "product-gate-b-decision"
    _require_parent_roles(item, roles, errors); _require_parent_artifacts(item, artifacts, errors)
    return errors


def validate_gate_b_report(value: object) -> list[str]:
    errors, item = _validate_base(value, artifact="product-gate-b-report", lifecycle="derived-replaceable", extra_keys={"gate_id", "summary", "gate_decision", "payload"})
    if item is None: return errors
    _require_origin(item, {"deterministic"}, "product-gate-b-report", errors)
    if not _nonempty(item.get("gate_id")): errors.append("gate_id must be non-empty")
    summary = item.get("summary")
    keys = {"projects", "assessed", "lineage_decisions", "resolution_decisions", "split_merge_projects"}
    if not isinstance(summary, dict): errors.append("summary must be an object")
    else:
        _strict_keys(summary, keys, "summary", errors)
        if any(not isinstance(summary.get(key), int) or isinstance(summary.get(key), bool) or summary.get(key) < 0 for key in keys): errors.append("summary values must be non-negative integers")
    if item.get("gate_decision") is not None and item.get("gate_decision") not in GATE_B_DECISIONS: errors.append("gate_decision must be pass/fail/defer/null")
    _validate_bound_file(item.get("payload"), "payload", errors)
    parents = item.get("parents") if isinstance(item.get("parents"), list) else []
    roles = {str(parent.get("role")) for parent in parents if isinstance(parent, dict)}
    expected = {"corpus", *{role for role in roles if role.startswith("assessment-") or role.startswith("decision-")}}
    _require_parent_roles(item, expected, errors)
    artifacts = {role: "product-gate-b-assessment" if role.startswith("assessment-") else "product-gate-b-decision" if role.startswith("decision-") else "product-gate-b-corpus" for role in expected}
    _require_parent_artifacts(item, artifacts, errors)
    return errors



__all__ = [name for name in globals() if not name.startswith("__")]
