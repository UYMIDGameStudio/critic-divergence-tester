"""Artifact contract validators grouped by family."""

from __future__ import annotations

from .base import *  # noqa: F403
from .core import *  # noqa: F403

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



__all__ = [name for name in globals() if not name.startswith("__")]
