"""Artifact contract validators grouped by family."""

from __future__ import annotations

from .base import *  # noqa: F403
from .core import *  # noqa: F403

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



__all__ = [name for name in globals() if not name.startswith("__")]
