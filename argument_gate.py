"""Private, local Product Gate A corpus and evidence reports.

The gate stores hashes and local workspace locators, never manuscript bytes.
Gate artifacts are evaluation evidence; they are not normal writing-workflow
artifacts and never produce a manuscript score or an automatic pass decision.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from argument_adjudication import (
    current_finding_entries,
    human_review_paths,
    latest_adjudications,
    list_adjudications,
)
from argument_baseline import (
    controlled_baseline_errors,
    latest_direct_review_baseline,
)
from argument_sessions import list_work_sessions
from argument_triage import current_status_triage, current_triage_indexes
from argument_contracts import (
    GATE_A_BURDENS,
    GATE_A_COMPARISONS,
    GATE_A_DECISIONS,
    sha256_bytes,
    validate_artifact,
)
from argument_gate_common import GateLifecycle
from argument_workbench import (
    WorkbenchError,
    _atomic_write,
    _parent,
    _provenance,
    _read_json,
    _write_new,
    json_bytes,
    utc_now,
    verify_workspace,
    workspace_paths,
)


ASSESSMENT_PATTERN = re.compile(r"AS([0-9]{4})\.json\Z")
DECISION_PATTERN = re.compile(r"GD([0-9]{4})\.json\Z")
METRIC_KEYS = (
    "missed_claims",
    "wrong_claim_types",
    "wrong_relations",
    "rhetoric_as_claims",
    "reversed_attributions",
)
LEGACY_METRIC_KEYS = ("correction_minutes", *METRIC_KEYS)


_GATE_LIFECYCLE = GateLifecycle(
    label="Gate A",
    corpus_artifact="product-gate-a-corpus",
    assessment_artifact="product-gate-a-assessment",
    decision_artifact="product-gate-a-decision",
    assessment_pattern=ASSESSMENT_PATTERN,
    decision_pattern=DECISION_PATTERN,
    validator=validate_artifact,
    readiness=lambda root: (
        []
        if _derive_report(gate_paths(root))[0]["readiness"]["ready_for_human_decision"]
        else ["all 3-5 workflows and assessments must be complete"]
    ),
    error_type=WorkbenchError,
)


@dataclass(frozen=True)
class GatePaths:
    root: Path

    @property
    def corpus(self) -> Path:
        return self.root / "corpus.json"

    @property
    def assessments(self) -> Path:
        return self.root / "assessments"

    @property
    def decisions(self) -> Path:
        return self.root / "decisions"

    @property
    def report_dir(self) -> Path:
        return self.root / "report"

    @property
    def report_record(self) -> Path:
        return self.report_dir / "record.json"

    @property
    def report_markdown(self) -> Path:
        return self.report_dir / "product-gate-a.md"


def gate_paths(root: Path | str) -> GatePaths:
    return GatePaths(Path(root).expanduser().resolve())


def _regular_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise WorkbenchError(f"{label} must be a regular non-symlink file: {path}")
    return path.read_bytes()


def _project_snapshot(
    project_dir: Path | str,
    *,
    require_status_triage: bool = True,
    require_controlled_baseline: bool = True,
    require_ir_inspection_sessions: bool = True,
) -> dict[str, Any]:
    workspace = workspace_paths(project_dir)
    errors = verify_workspace(workspace)
    if errors:
        raise WorkbenchError(
            f"Gate A project is invalid ({workspace.root}): " + "; ".join(errors)
        )
    human = human_review_paths(workspace.root)
    required = {
        "project": workspace.project,
        "document_version": workspace.version,
        "reviewed_ir_record": workspace.reviewed_record,
        "reviewed_ir_payload": workspace.reviewed_payload,
        "revision_plan_record": human.plan_record,
        "revision_plan_markdown": human.plan_markdown,
    }
    data = {key: _regular_bytes(path, key) for key, path in required.items()}
    _, baseline, baseline_bytes = latest_direct_review_baseline(workspace.root)
    baseline_errors = validate_artifact(baseline)
    if baseline_errors:
        raise WorkbenchError(
            "Gate A direct-review baseline is invalid: "
            + "; ".join(baseline_errors)
        )
    if require_controlled_baseline:
        control_errors = controlled_baseline_errors(baseline)
        if control_errors:
            raise WorkbenchError(
                "Gate A direct-review baseline is not a controlled comparison: "
                + "; ".join(control_errors)
            )
    from argument_review import list_result_attempts, list_rule_reviews

    review_result_times = [
        str(attempt["provenance"]["created_at"])
        for review_paths in list_rule_reviews(workspace.root)
        for _, attempt, _ in list_result_attempts(review_paths)
        if attempt.get("validation", {}).get("status") == "valid"
    ]
    first_review: datetime | None = None
    if review_result_times:
        baseline_completed = datetime.fromisoformat(
            str(baseline["timing"]["completed_at"]).replace("Z", "+00:00")
        )
        first_review = min(
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            for value in review_result_times
        )
        if baseline_completed > first_review:
            raise WorkbenchError(
                "Gate A direct-review baseline was completed after a Workbench "
                "Rule Review result and cannot serve as an uncontaminated comparison"
            )
    project, _ = _read_json(workspace.project)
    version, _ = _read_json(workspace.version)
    reviewed, _ = _read_json(workspace.reviewed_payload)
    plan, _ = _read_json(human.plan_record)
    source_path = workspace.version_dir / str(version["source"]["relative_path"])
    source_bytes = _regular_bytes(source_path, "source")
    if sha256_bytes(source_bytes) != version["source"]["sha256"]:
        raise WorkbenchError("Gate A source binding is broken")
    summary = plan.get("summary")
    if not isinstance(summary, dict):
        raise WorkbenchError("Gate A requires a valid Phase 3 revision plan")
    if int(summary.get("open", 0)) != 0:
        raise WorkbenchError("Gate A requires every current Finding to be adjudicated")
    triage_items = current_status_triage(workspace.root)
    open_triage = [item for item in triage_items if item.decision is None]
    if require_status_triage and open_triage:
        raise WorkbenchError(
            f"Gate A requires human triage for {len(open_triage)} non-evaluated review statuses"
        )
    triage_indexes = (
        current_triage_indexes(workspace.root) if not open_triage else []
    )
    triage_bindings = [
        {
            "review_id": representative.review_id,
            "attempt_id": representative.attempt_id,
            "sha256": sha256_bytes(index_bytes),
        }
        for representative, _, index_bytes in triage_indexes
    ]
    work_sessions = list_work_sessions(workspace.root)
    open_sessions = [entry for entry in work_sessions if entry.record is None]
    if require_ir_inspection_sessions and open_sessions:
        raise WorkbenchError(
            "Gate A cannot capture while a human work session is still open: "
            + ", ".join(entry.paths.session_id for entry in open_sessions)
        )
    ir_inspection_sessions = [
        entry
        for entry in work_sessions
        if entry.record is not None
        and entry.record.get("artifact") == "gate-a-work-session"
        and entry.start.get("activity") == "ir-inspection"
        and entry.record_bytes is not None
        and (
            first_review is None
            or datetime.fromisoformat(
                str(entry.record["timing"]["completed_at"]).replace(
                    "Z", "+00:00"
                )
            )
            <= first_review
        )
    ]
    if require_ir_inspection_sessions and not ir_inspection_sessions:
        raise WorkbenchError(
            "Gate A requires at least one completed ir-inspection work session "
            "before the first Rule Review result"
        )
    session_bindings = [
        {
            "session_id": entry.paths.session_id,
            "sha256": sha256_bytes(entry.record_bytes),
            "elapsed_milliseconds": int(
                entry.record["timing"]["elapsed_milliseconds"]
            ),
        }
        for entry in ir_inspection_sessions
        if entry.record is not None and entry.record_bytes is not None
    ]
    corrections = 0
    if workspace.corrections_dir.exists():
        corrections = len(list(workspace.corrections_dir.glob("IC[0-9][0-9][0-9][0-9].json")))
    return {
        "workspace": workspace,
        "project": project,
        "version": version,
        "project_bytes": data["project"],
        "plan_bytes": data["revision_plan_record"],
        "baseline": baseline,
        "baseline_bytes": baseline_bytes,
        "bindings": {
            "project": sha256_bytes(data["project"]),
            "document_version": sha256_bytes(data["document_version"]),
            "source": sha256_bytes(source_bytes),
            "reviewed_ir_record": sha256_bytes(data["reviewed_ir_record"]),
            "reviewed_ir_payload": sha256_bytes(data["reviewed_ir_payload"]),
            "revision_plan_record": sha256_bytes(data["revision_plan_record"]),
            "revision_plan_markdown": sha256_bytes(data["revision_plan_markdown"]),
            "direct_review_baseline": sha256_bytes(baseline_bytes),
            "status_triage": triage_bindings,
            "ir_inspection_sessions": session_bindings,
        },
        "triage_items": triage_items,
        "triage_indexes": triage_indexes,
        "ir_inspection_sessions": ir_inspection_sessions,
        "ir_inspection_elapsed_milliseconds": sum(
            binding["elapsed_milliseconds"] for binding in session_bindings
        ),
        "claims": len(reviewed.get("claims", [])),
        "corrections": corrections,
        "summary": {key: int(summary.get(key, 0)) for key in ("accept", "reject", "defer", "open")},
    }


def _snapshot_bindings_match(snapshot: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Compare exactly the binding fields declared by a corpus schema version."""
    stored = entry.get("bindings")
    current = snapshot.get("bindings")
    if not isinstance(stored, dict) or not isinstance(current, dict):
        return False
    return {key: current.get(key) for key in stored} == stored


def gate_readiness(project_dirs: list[Path | str]) -> dict[str, Any]:
    if not 3 <= len(project_dirs) <= 5:
        raise WorkbenchError("Product Gate A readiness requires 3 to 5 projects")
    workspaces = [workspace_paths(project) for project in project_dirs]
    roots = [str(workspace.root) for workspace in workspaces]
    if len(roots) != len(set(roots)):
        raise WorkbenchError("Gate A readiness project paths must be unique")

    projects: list[dict[str, Any]] = []
    source_hashes: list[str] = []
    for index, workspace in enumerate(workspaces, 1):
        errors = verify_workspace(workspace)
        title = workspace.root.name
        claims = 0
        source_sha256 = ""
        reviewed_available = False
        if workspace.project.is_file() and not workspace.project.is_symlink():
            try:
                project, _ = _read_json(workspace.project)
                title = str(project.get("title") or title)
            except (OSError, WorkbenchError) as exc:
                errors.append(f"project: {exc}")
        if workspace.version.is_file() and not workspace.version.is_symlink():
            try:
                version, _ = _read_json(workspace.version)
                source_sha256 = str(version.get("source", {}).get("sha256") or "")
            except (OSError, WorkbenchError) as exc:
                errors.append(f"document-version: {exc}")
        if source_sha256:
            source_hashes.append(source_sha256)
        if workspace.reviewed_payload.is_file() and not workspace.reviewed_payload.is_symlink():
            try:
                reviewed, _ = _read_json(workspace.reviewed_payload)
                claims = len(reviewed.get("claims", []))
                reviewed_available = True
            except (OSError, WorkbenchError) as exc:
                errors.append(f"reviewed-ir: {exc}")
        workspace_invalid = bool(errors)

        corrections = (
            len(
                list(
                    workspace.corrections_dir.glob(
                        "IC[0-9][0-9][0-9][0-9].json"
                    )
                )
            )
            if workspace.corrections_dir.is_dir()
            and not workspace.corrections_dir.is_symlink()
            else 0
        )
        findings_available = reviewed_available
        if reviewed_available:
            try:
                findings = current_finding_entries(workspace.root)
            except WorkbenchError as exc:
                findings_available = False
                findings = []
                errors.append(f"current-review: {exc}")
        else:
            findings_available = False
            findings = []
        current_ids = {str(entry.value.get("finding_id")) for entry in findings}
        model_counts = {"fail": 0, "uncertain": 0}
        for finding in findings:
            model_counts[str(finding.value["verdict"])] += 1
        try:
            triage_items = current_status_triage(workspace.root)
        except WorkbenchError as exc:
            triage_items = []
            if findings_available:
                errors.append(f"status-triage: {exc}")
        triage_counts = {
            "blocked_missing_context": 0,
            "routing_mismatch": 0,
            "not_applicable": 0,
            "acknowledge": 0,
            "reject": 0,
            "open": 0,
        }
        for item in triage_items:
            triage_counts[item.model_status] += 1
            triage_counts[
                str(item.decision["decision"]) if item.decision is not None else "open"
            ] += 1
        human = human_review_paths(workspace.root)
        try:
            latest = latest_adjudications(list_adjudications(human))
        except (OSError, WorkbenchError) as exc:
            latest = {}
            errors.append(f"adjudications: {exc}")
            workspace_invalid = True
        decision_counts = {"accept": 0, "reject": 0, "defer": 0, "open": 0}
        for finding_id in current_ids:
            adjudication = latest.get(finding_id)
            decision = (
                str(adjudication[1]["decision"])
                if adjudication is not None
                else "open"
            )
            decision_counts[decision] += 1
        plan_ready = (
            human.plan_record.is_file()
            and not human.plan_record.is_symlink()
            and human.plan_markdown.is_file()
            and not human.plan_markdown.is_symlink()
        )
        try:
            _, baseline, _ = latest_direct_review_baseline(workspace.root)
            baseline_available = not validate_artifact(baseline)
            baseline_control_errors = (
                controlled_baseline_errors(baseline) if baseline_available else []
            )
            baseline_controlled = baseline_available and not baseline_control_errors
        except (OSError, WorkbenchError):
            baseline_available = False
            baseline_control_errors = []
            baseline_controlled = False
        try:
            session_entries = list_work_sessions(workspace.root)
            from argument_review import list_result_attempts, list_rule_reviews

            valid_result_times = [
                datetime.fromisoformat(
                    str(attempt["provenance"]["created_at"]).replace(
                        "Z", "+00:00"
                    )
                )
                for review_paths in list_rule_reviews(workspace.root)
                for _, attempt, _ in list_result_attempts(review_paths)
                if attempt.get("validation", {}).get("status") == "valid"
            ]
            first_review = min(valid_result_times) if valid_result_times else None
            completed_ir_inspection_sessions = [
                entry
                for entry in session_entries
                if entry.record is not None
                and entry.record.get("artifact") == "gate-a-work-session"
                and entry.start.get("activity") == "ir-inspection"
                and (
                    first_review is None
                    or datetime.fromisoformat(
                        str(entry.record["timing"]["completed_at"]).replace(
                            "Z", "+00:00"
                        )
                    )
                    <= first_review
                )
            ]
            open_work_sessions = [
                entry.paths.session_id
                for entry in session_entries
                if entry.record is None
            ]
        except (OSError, WorkbenchError) as exc:
            completed_ir_inspection_sessions = []
            open_work_sessions = []
            errors.append(f"work sessions: {exc}")
        ready = (
            not errors
            and findings_available
            and decision_counts["open"] == 0
            and triage_counts["open"] == 0
            and plan_ready
            and baseline_controlled
            and bool(completed_ir_inspection_sessions)
            and not open_work_sessions
        )
        if workspace_invalid:
            next_command = f'python critic_runner.py ir verify-project "{workspace.root}"'
        elif not baseline_controlled:
            next_command = (
                f'python critic_runner.py ir gate-a prepare-baseline "{workspace.root}" '
                "DIRECT-BASELINE-PROMPT.md"
            )
        elif not reviewed_available:
            next_command = (
                f'python critic_runner.py ir collect "{workspace.root}" '
                "--file RAW-IR.json"
            )
        elif open_work_sessions:
            next_command = (
                f'python critic_runner.py ir gate-a session finish "{workspace.root}" '
                f"{open_work_sessions[0]}"
            )
        elif not completed_ir_inspection_sessions:
            next_command = (
                f'python critic_runner.py ir gate-a session start "{workspace.root}" '
                "--activity ir-inspection"
            )
        elif not findings_available:
            next_command = f'python critic_runner.py ir review prepare "{workspace.root}"'
        elif decision_counts["open"]:
            next_command = (
                f'python critic_runner.py ir adjudicate "{workspace.root}" '
                "--summary-only"
            )
        elif triage_counts["open"]:
            next_command = f'python critic_runner.py ir review triage "{workspace.root}"'
        elif not plan_ready:
            next_command = f'python critic_runner.py ir revision-plan "{workspace.root}"'
        else:
            next_command = "ready for immutable Gate A corpus capture"
        projects.append(
            {
                "alias": f"P{index}",
                "workspace": str(workspace.root),
                "title": title,
                "source_sha256": source_sha256,
                "claims": claims,
                "corrections": corrections,
                "reviewed_ir": reviewed_available,
                "review_available": findings_available,
                "model_findings": model_counts,
                "human_decisions": decision_counts,
                "status_triage": triage_counts,
                "revision_plan": plan_ready,
                "direct_review_baseline": baseline_available,
                "direct_review_baseline_controlled": baseline_controlled,
                "baseline_control_errors": baseline_control_errors,
                "completed_ir_inspection_sessions": len(
                    completed_ir_inspection_sessions
                ),
                "open_work_sessions": open_work_sessions,
                "ready_for_capture": ready,
                "errors": errors,
                "next_command": next_command,
            }
        )
    duplicate_sources = len(source_hashes) != len(set(source_hashes))
    ready_count = sum(1 for project in projects if project["ready_for_capture"])
    return {
        "projects": projects,
        "summary": {
            "projects": len(projects),
            "ready_for_capture": ready_count,
            "open_findings": sum(
                project["human_decisions"]["open"] for project in projects
            ),
            "open_status_triage": sum(
                project["status_triage"]["open"] for project in projects
            ),
            "duplicate_sources": duplicate_sources,
            "can_capture_corpus": ready_count == len(projects)
            and not duplicate_sources,
        },
    }


def render_gate_readiness(readiness: dict[str, Any]) -> str:
    lines = ["Product Gate A readiness (read-only)", ""]
    for project in readiness["projects"]:
        model = project["model_findings"]
        human = project["human_decisions"]
        triage = project["status_triage"]
        lines.extend(
            [
                f"{project['alias']} - {project['title']}",
                f"  Workspace: {project['workspace']}",
                f"  Claims: {project['claims']} · Corrections: {project['corrections']}",
                f"  Reviewed IR: {'ready' if project['reviewed_ir'] else 'missing'}",
                f"  Model Findings: {model['fail']} FAIL · {model['uncertain']} UNCERTAIN",
                f"  Human decisions: {human['accept']} accept · {human['reject']} reject · "
                f"{human['defer']} defer · {human['open']} open",
                f"  Status triage: {triage['acknowledge']} acknowledged / "
                f"{triage['reject']} rejected / {triage['open']} open",
                f"  Revision plan: {'ready' if project['revision_plan'] else 'missing'}",
                "  Direct-review baseline: "
                f"{'controlled' if project['direct_review_baseline_controlled'] else 'uncontrolled' if project['direct_review_baseline'] else 'missing'}",
                "  IR inspection timing: "
                f"{project['completed_ir_inspection_sessions']} complete / "
                f"{len(project['open_work_sessions'])} open",
                f"  Gate capture: {'ready' if project['ready_for_capture'] else 'not ready'}",
            ]
        )
        for error in project["errors"]:
            lines.append(f"  Error: {error}")
        for error in project["baseline_control_errors"]:
            lines.append(f"  Baseline control: {error}")
        lines.extend([f"  Next: {project['next_command']}", ""])
    summary = readiness["summary"]
    lines.extend(
        [
            f"Summary: {summary['ready_for_capture']}/{summary['projects']} projects ready; "
            f"{summary['open_findings']} open Findings; "
            f"{summary['open_status_triage']} open status triage items",
            f"Duplicate source bytes: {'yes' if summary['duplicate_sources'] else 'no'}",
            f"Can capture immutable corpus: {'yes' if summary['can_capture_corpus'] else 'no'}",
            "This command does not create an assessment or make a Gate decision.",
            "",
        ]
    )
    return "\n".join(lines)


def initialize_gate(
    output_dir: Path | str,
    project_dirs: list[Path | str],
    *,
    producer: str = "local-evaluator",
) -> GatePaths:
    paths = gate_paths(output_dir)
    if paths.root.exists() or paths.root.is_symlink():
        raise WorkbenchError(f"refusing to overwrite Gate A directory: {paths.root}")
    if not 3 <= len(project_dirs) <= 5:
        raise WorkbenchError("Product Gate A requires 3 to 5 real manuscript projects")
    paths.root.parent.mkdir(parents=True, exist_ok=True)
    snapshots = [_project_snapshot(project) for project in project_dirs]
    roots = [str(snapshot["workspace"].root) for snapshot in snapshots]
    if len(roots) != len(set(roots)):
        raise WorkbenchError("Gate A project paths must be unique")
    created_at = utc_now()
    entries: list[dict[str, Any]] = []
    parents: list[dict[str, str]] = []
    for index, snapshot in enumerate(snapshots, 1):
        project = snapshot["project"]
        version = snapshot["version"]
        entries.append(
            {
                "alias": f"P{index}",
                "workspace_locator": str(snapshot["workspace"].root),
                "project_id": project["project_id"],
                "document_id": version["document_id"],
                "version_id": version["version_id"],
                "real_manuscript_confirmed": True,
                "bindings": snapshot["bindings"],
            }
        )
        parents.append(
            _parent(f"project-{index:03d}", "argument-project", snapshot["project_bytes"])
        )
        parents.append(
            _parent(
                f"baseline-{index:03d}",
                "direct-review-baseline",
                snapshot["baseline_bytes"],
            )
        )
        for triage_index, (_, _, index_bytes) in enumerate(
            snapshot["triage_indexes"], 1
        ):
            parents.append(
                _parent(
                    f"status-triage-{index:03d}-{triage_index:03d}",
                    "review-status-triage-index",
                    index_bytes,
                )
            )
        for session_index, session in enumerate(
            snapshot["ir_inspection_sessions"], 1
        ):
            parents.append(
                _parent(
                    f"ir-inspection-{index:03d}-{session_index:03d}",
                    "gate-a-work-session",
                    session.record_bytes,
                )
            )
    corpus_id = "GA-" + uuid.uuid4().hex[:12]
    corpus = {
        "schema_version": 5,
        "artifact": "product-gate-a-corpus",
        "artifact_id": corpus_id,
        "lifecycle": "immutable",
        "provenance": _provenance("human-confirmed", created_at, producer),
        "parents": parents,
        "corpus_id": corpus_id,
        "entries": entries,
    }
    errors = validate_artifact(corpus)
    if errors:
        raise WorkbenchError("internal Gate A corpus error: " + "; ".join(errors))
    _GATE_LIFECYCLE.initialize(
        paths.root,
        json_bytes(corpus),
        write_new=_write_new,
        build_report=rebuild_gate_report,
    )
    return paths


def _read_corpus(paths: GatePaths) -> tuple[dict[str, Any], bytes]:
    return _GATE_LIFECYCLE.read_corpus(paths.corpus, read_json=_read_json)


def _numbered_entries(
    directory: Path, pattern: re.Pattern[str], label: str
) -> list[tuple[Path, dict[str, Any], bytes]]:
    kind = "assessment" if pattern is ASSESSMENT_PATTERN else "decision"
    return _GATE_LIFECYCLE.entries(directory, kind=kind, read_json=_read_json)


def list_assessments(paths: GatePaths) -> list[tuple[Path, dict[str, Any], bytes]]:
    return _numbered_entries(paths.assessments, ASSESSMENT_PATTERN, "assessment")


def list_gate_decisions(paths: GatePaths) -> list[tuple[Path, dict[str, Any], bytes]]:
    return _numbered_entries(paths.decisions, DECISION_PATTERN, "gate decision")


def _entry_by_alias(corpus: dict[str, Any], alias: str) -> dict[str, Any]:
    for entry in corpus["entries"]:
        if entry["alias"] == alias:
            return entry
    raise WorkbenchError(f"unknown Gate A project alias: {alias}")


def append_assessment(
    gate_dir: Path | str,
    project_alias: str,
    *,
    comparison_to_direct_chat: str,
    correction_burden: str,
    metrics: dict[str, int],
    regression_anchors: list[str],
    actual_revision_notes: str,
    notes: str,
    producer: str = "local-evaluator",
) -> Path:
    paths = gate_paths(gate_dir)
    errors = verify_gate(paths.root, compare_report=False)
    if errors:
        raise WorkbenchError("Gate A evidence is invalid: " + "; ".join(errors))
    corpus, corpus_bytes = _read_corpus(paths)
    entry = _entry_by_alias(corpus, project_alias)
    assessments = list_assessments(paths)
    if any(value.get("project_alias") == project_alias for _, value, _ in assessments):
        raise WorkbenchError(f"assessment already exists for {project_alias}")
    if comparison_to_direct_chat not in GATE_A_COMPARISONS:
        raise WorkbenchError("comparison must be clearer/same/worse/uncertain")
    if correction_burden not in GATE_A_BURDENS:
        raise WorkbenchError("burden must be acceptable/high/uncertain")
    is_session_bound = corpus.get("schema_version") == 5
    expected_metric_keys = METRIC_KEYS if is_session_bound else LEGACY_METRIC_KEYS
    if set(metrics) != set(expected_metric_keys) or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in metrics.values()
    ):
        raise WorkbenchError(
            "Gate A metrics must contain exactly "
            f"{expected_metric_keys} as non-negative integers"
        )
    if not regression_anchors or any(
        not isinstance(anchor, str) or not anchor.strip()
        for anchor in regression_anchors
    ):
        raise WorkbenchError("at least one non-empty regression anchor is required")
    if len(regression_anchors) != len(set(regression_anchors)):
        raise WorkbenchError("regression anchors must not contain duplicates")
    snapshot = _project_snapshot(
        entry["workspace_locator"],
        require_status_triage=corpus.get("schema_version") in {3, 4, 5},
        require_controlled_baseline=corpus.get("schema_version") in {4, 5},
        require_ir_inspection_sessions=is_session_bound,
    )
    if not _snapshot_bindings_match(snapshot, entry):
        raise WorkbenchError(f"{project_alias} changed after corpus capture")
    assessment_id = f"AS{len(assessments) + 1:04d}"
    parents = [
        _parent("corpus", "product-gate-a-corpus", corpus_bytes),
        _parent("project", "argument-project", snapshot["project_bytes"]),
        _parent("revision-plan", "revision-plan-record", snapshot["plan_bytes"]),
        _parent(
            "direct-review-baseline",
            "direct-review-baseline",
            snapshot["baseline_bytes"],
        ),
        *[
            _parent(
                f"status-triage-{index:03d}",
                "review-status-triage-index",
                index_bytes,
            )
            for index, (_, _, index_bytes) in enumerate(
                snapshot["triage_indexes"], 1
            )
        ],
    ]
    if is_session_bound:
        parents.extend(
            _parent(
                f"ir-inspection-{index:03d}",
                "gate-a-work-session",
                session.record_bytes,
            )
            for index, session in enumerate(
                snapshot["ir_inspection_sessions"], 1
            )
        )
    assessment = {
        "schema_version": 5 if is_session_bound else 4,
        "artifact": "product-gate-a-assessment",
        "artifact_id": assessment_id,
        "lifecycle": "immutable",
        "provenance": _provenance("human-confirmed", utc_now(), producer),
        "parents": parents,
        "corpus_id": corpus["corpus_id"],
        "project_alias": project_alias,
        "comparison_to_direct_chat": comparison_to_direct_chat,
        "correction_burden": correction_burden,
        "metrics": {key: metrics[key] for key in expected_metric_keys},
        "regression_anchors": regression_anchors,
        "actual_revision_notes": actual_revision_notes,
        "notes": notes,
    }
    if is_session_bound:
        assessment["ir_inspection_timing"] = {
            "elapsed_milliseconds": snapshot[
                "ir_inspection_elapsed_milliseconds"
            ],
            "sessions": [
                {
                    "session_id": session.paths.session_id,
                    "sha256": sha256_bytes(session.record_bytes),
                }
                for session in snapshot["ir_inspection_sessions"]
            ],
        }
        assessment["field_provenance"] = {
            "human_observations": {
                "origin": "human-confirmed",
                "source": "Gate A evaluator CLI input",
            },
            "ir_inspection_timing": {
                "origin": "deterministic",
                "source": "sum of corpus-bound gate-a-work-session records",
            },
        }
    _GATE_LIFECYCLE.validate_new(assessment, kind="assessment")
    output = paths.assessments / f"{assessment_id}.json"
    _write_new(output, json_bytes(assessment))
    rebuild_gate_report(paths.root)
    return output


def append_gate_decision(
    gate_dir: Path | str,
    decision: str,
    reason: str,
    *,
    producer: str = "local-evaluator",
) -> Path:
    paths = gate_paths(gate_dir)
    if decision not in GATE_A_DECISIONS:
        raise WorkbenchError("Gate A decision must be pass/fail/defer")
    if not reason.strip():
        raise WorkbenchError("Gate A decision requires a human reason")
    if decision == "pass":
        issues = _GATE_LIFECYCLE.pass_issues(paths.root)
        if issues:
            raise WorkbenchError("Gate A cannot pass before " + "; ".join(issues))
    corpus, corpus_bytes = _read_corpus(paths)
    decisions = list_gate_decisions(paths)
    decision_id = f"GD{len(decisions) + 1:04d}"
    parents = [_parent("corpus", "product-gate-a-corpus", corpus_bytes)]
    supersedes: str | None = None
    if decisions:
        previous = decisions[-1]
        supersedes = sha256_bytes(previous[2])
        parents.append(_parent("previous-decision", "product-gate-a-decision", previous[2]))
    value = {
        "schema_version": 1,
        "artifact": "product-gate-a-decision",
        "artifact_id": decision_id,
        "lifecycle": "immutable",
        "provenance": _provenance("human-confirmed", utc_now(), producer),
        "parents": parents,
        "corpus_id": corpus["corpus_id"],
        "decision": decision,
        "reason": reason,
        "supersedes": supersedes,
    }
    _GATE_LIFECYCLE.validate_new(value, kind="decision")
    output = paths.decisions / f"{decision_id}.json"
    _write_new(output, json_bytes(value))
    rebuild_gate_report(paths.root)
    return output


def _derive_report(
    paths: GatePaths,
) -> tuple[dict[str, Any], bytes, str, list[str]]:
    corpus, corpus_bytes = _read_corpus(paths)
    assessments = list_assessments(paths)
    decisions = list_gate_decisions(paths)
    assessment_by_alias = {value["project_alias"]: (value, data) for _, value, data in assessments}
    session_bound = corpus.get("schema_version") == 5
    metric_keys = METRIC_KEYS if session_bound else LEGACY_METRIC_KEYS
    errors: list[str] = []
    project_rows: list[dict[str, Any]] = []
    totals = {key: 0 for key in ("claims", "corrections", "findings", "accept", "reject", "defer", "open")}
    observations = {
        "clearer": 0,
        "same": 0,
        "worse": 0,
        "uncertain": 0,
        "acceptable_burden": 0,
        "high_burden": 0,
        "uncertain_burden": 0,
        "regression_anchors": 0,
        "actual_revisions_recorded": 0,
        "metrics": {key: 0 for key in metric_keys},
    }
    work_timing = {"ir_inspection_elapsed_milliseconds": 0}
    complete_workflows = 0
    parents = [_parent("corpus", "product-gate-a-corpus", corpus_bytes)]
    latest_created_at = corpus["provenance"]["created_at"]
    for index, entry in enumerate(corpus["entries"], 1):
        alias = entry["alias"]
        try:
            snapshot = _project_snapshot(
                entry["workspace_locator"],
                require_status_triage=corpus.get("schema_version") in {3, 4, 5},
                require_controlled_baseline=corpus.get("schema_version") in {4, 5},
                require_ir_inspection_sessions=corpus.get("schema_version") == 5,
            )
            bindings_match = _snapshot_bindings_match(snapshot, entry)
        except (OSError, WorkbenchError) as exc:
            errors.append(f"{alias}: {exc}")
            bindings_match = False
            snapshot = {
                "claims": 0,
                "corrections": 0,
                "ir_inspection_elapsed_milliseconds": 0,
                "summary": {
                    key: 0 for key in ("accept", "reject", "defer", "open")
                },
            }
        summary = snapshot["summary"]
        findings = sum(summary.values())
        workflow_complete = bindings_match and summary["open"] == 0
        if workflow_complete:
            complete_workflows += 1
        row = {
            "alias": alias,
            "bindings_match": bindings_match,
            "workflow_complete": workflow_complete,
            "claims": snapshot["claims"],
            "corrections": snapshot["corrections"],
            "findings": findings,
            "accept": summary["accept"],
            "reject": summary["reject"],
            "defer": summary["defer"],
            "open": summary["open"],
            "assessment_id": None,
            "regression_anchors": 0,
            "actual_revision_recorded": False,
        }
        if session_bound:
            row["ir_inspection_elapsed_milliseconds"] = snapshot[
                "ir_inspection_elapsed_milliseconds"
            ]
            work_timing["ir_inspection_elapsed_milliseconds"] += row[
                "ir_inspection_elapsed_milliseconds"
            ]
        for key in totals:
            totals[key] += row[key]
        assessment_entry = assessment_by_alias.get(alias)
        if assessment_entry is not None:
            assessment, assessment_bytes = assessment_entry
            row["assessment_id"] = assessment["artifact_id"]
            row["regression_anchors"] = len(assessment["regression_anchors"])
            row["actual_revision_recorded"] = bool(
                assessment["actual_revision_notes"].strip()
            )
            parents.append(_parent(f"assessment-{index:03d}", "product-gate-a-assessment", assessment_bytes))
            latest_created_at = max(latest_created_at, assessment["provenance"]["created_at"])
            observations[assessment["comparison_to_direct_chat"]] += 1
            observations[f"{assessment['correction_burden']}_burden"] += 1
            observations["regression_anchors"] += len(assessment["regression_anchors"])
            if assessment["actual_revision_notes"].strip():
                observations["actual_revisions_recorded"] += 1
            for key in metric_keys:
                observations["metrics"][key] += assessment["metrics"][key]
        project_rows.append(row)
    latest_decision: dict[str, Any] | None = None
    if decisions:
        latest_decision = decisions[-1][1]
        parents.append(_parent("gate-decision", "product-gate-a-decision", decisions[-1][2]))
        latest_created_at = max(latest_created_at, latest_decision["provenance"]["created_at"])
    readiness = {
        "corpus_size": len(project_rows),
        "assessments_complete": len(assessment_by_alias),
        "workflows_complete": complete_workflows,
        "open_findings": totals["open"],
        "ready_for_human_decision": (
            len(project_rows) == len(assessment_by_alias)
            and complete_workflows == len(project_rows)
            and totals["open"] == 0
        ),
    }
    if (
        latest_decision is not None
        and latest_decision["decision"] == "pass"
        and not readiness["ready_for_human_decision"]
    ):
        errors.append("human pass decision is disconnected from complete Gate A evidence")
    markdown = render_gate_report(
        corpus["corpus_id"],
        readiness,
        totals,
        observations,
        project_rows,
        latest_decision,
        work_timing=work_timing if session_bound else None,
    )
    markdown_bytes = markdown.encode("utf-8")
    report = {
        "schema_version": 2 if session_bound else 1,
        "artifact": "product-gate-a-report",
        "artifact_id": corpus["corpus_id"] + "-report",
        "lifecycle": "derived-replaceable",
        "provenance": _provenance(
            "deterministic",
            latest_created_at,
            "product-gate-a-report-v2" if session_bound else "product-gate-a-report-v1",
        ),
        "parents": parents,
        "corpus_id": corpus["corpus_id"],
        "readiness": readiness,
        "workflow_totals": totals,
        "human_observations": observations,
        "projects": project_rows,
        "gate_decision": latest_decision["decision"] if latest_decision else None,
        "payload": {"relative_path": "product-gate-a.md", "sha256": sha256_bytes(markdown_bytes)},
    }
    if session_bound:
        report["work_timing"] = work_timing
    contract_errors = validate_artifact(report)
    if contract_errors:
        raise WorkbenchError("internal Gate A report error: " + "; ".join(contract_errors))
    return report, json_bytes(report), markdown, errors


def render_gate_report(
    corpus_id: str,
    readiness: dict[str, Any],
    totals: dict[str, int],
    observations: dict[str, Any],
    projects: list[dict[str, Any]],
    decision: dict[str, Any] | None,
    *,
    work_timing: dict[str, int] | None = None,
) -> str:
    lines = [
        "# Product Gate A Evidence",
        "",
        f"Corpus: `{corpus_id}`",
        "",
        "This report contains workflow counts and human observations, not a manuscript quality score.",
        "",
        "## Readiness",
        "",
        f"- Real manuscript projects: {readiness['corpus_size']} (required: 3-5)",
        f"- Completed workflow snapshots: {readiness['workflows_complete']}",
        f"- Human assessments: {readiness['assessments_complete']}",
        f"- Open Findings: {readiness['open_findings']}",
        f"- Ready for human gate decision: {'yes' if readiness['ready_for_human_decision'] else 'no'}",
        "",
        "## Workflow Totals",
        "",
        f"- Claims: {totals['claims']}",
        f"- Human correction events: {totals['corrections']}",
        f"- Findings: {totals['findings']}",
        f"- Accepted / Rejected / Deferred / Open: {totals['accept']} / {totals['reject']} / {totals['defer']} / {totals['open']}",
    ]
    if work_timing is not None:
        elapsed = work_timing["ir_inspection_elapsed_milliseconds"]
        lines.extend(
            [
                f"- System-timed IR inspection: {elapsed} ms ({elapsed / 60000:.2f} minutes)",
            ]
        )
    lines.extend(
        [
        "",
        "## Human Observations",
        "",
        f"- Clearer than direct chat / Same / Worse / Uncertain: {observations['clearer']} / {observations['same']} / {observations['worse']} / {observations['uncertain']}",
        f"- Acceptable / High / Uncertain correction burden: {observations['acceptable_burden']} / {observations['high_burden']} / {observations['uncertain_burden']}",
        f"- Regression anchors recorded: {observations['regression_anchors']}",
        f"- Projects with actual revision notes: {observations['actual_revisions_recorded']}",
        "- Extraction/correction observations:",
        ]
    )
    for key in observations["metrics"]:
        lines.append(f"  - {key}: {observations['metrics'][key]}")
    lines.extend(["", "## Projects", ""])
    for project in projects:
        lines.extend(
            [
                f"### {project['alias']}",
                "",
                f"- Bound snapshot intact: {'yes' if project['bindings_match'] else 'no'}",
                f"- Workflow complete: {'yes' if project['workflow_complete'] else 'no'}",
                f"- Claims / corrections / Findings: {project['claims']} / {project['corrections']} / {project['findings']}",
                f"- Accept / Reject / Defer / Open: {project['accept']} / {project['reject']} / {project['defer']} / {project['open']}",
                f"- Human assessment: {project['assessment_id'] or 'missing'}",
                f"- Regression anchors: {project['regression_anchors']}",
                f"- Actual revision recorded: {'yes' if project['actual_revision_recorded'] else 'no'}",
            ]
        )
        if "ir_inspection_elapsed_milliseconds" in project:
            elapsed = project["ir_inspection_elapsed_milliseconds"]
            lines.append(
                f"- System-timed IR inspection: {elapsed} ms "
                f"({elapsed / 60000:.2f} minutes)"
            )
        lines.append("")
    lines.extend(["## Human Gate Decision", ""])
    if decision is None:
        lines.extend(["Pending. The program never passes Product Gate A automatically.", ""])
    else:
        lines.extend(
            [
                f"- Decision: `{decision['decision']}` `[human-confirmed]`",
                f"- Reason: {decision['reason']}",
                "",
            ]
        )
    return "\n".join(lines)


def rebuild_gate_report(gate_dir: Path | str) -> tuple[Path, bool]:
    paths = gate_paths(gate_dir)
    _, record_bytes, markdown, derivation_errors = _derive_report(paths)
    if derivation_errors:
        raise WorkbenchError("cannot rebuild Gate A report: " + "; ".join(derivation_errors))
    markdown_bytes = markdown.encode("utf-8")
    changed = _GATE_LIFECYCLE.rebuild_report(
        (
            (paths.report_markdown, markdown_bytes),
            (paths.report_record, record_bytes),
        ),
        atomic_write=_atomic_write,
    )
    return paths.report_markdown, changed


def verify_gate(gate_dir: Path | str, *, compare_report: bool = True) -> list[str]:
    paths = gate_paths(gate_dir)
    errors: list[str] = []
    if paths.root.is_symlink() or not paths.root.is_dir():
        return [f"Gate A root must be a regular non-symlink directory: {paths.root}"]
    try:
        corpus, corpus_bytes = _read_corpus(paths)
        assessments = list_assessments(paths)
        decisions = list_gate_decisions(paths)
    except (OSError, WorkbenchError) as exc:
        return [str(exc)]
    expected_root_entries = {"corpus.json", "assessments", "decisions", "report"}
    actual_root_entries = {
        child.name for child in paths.root.iterdir() if child.name != ".mutation.lock"
    }
    if actual_root_entries != expected_root_entries:
        errors.append("Gate A root contains unexpected or missing entries")
    project_by_alias: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(corpus["entries"], 1):
        alias = entry["alias"]
        project_by_alias[alias] = entry
        try:
            snapshot = _project_snapshot(
                entry["workspace_locator"],
                require_status_triage=corpus.get("schema_version") in {3, 4, 5},
                require_controlled_baseline=corpus.get("schema_version") in {4, 5},
                require_ir_inspection_sessions=corpus.get("schema_version") == 5,
            )
            if not _snapshot_bindings_match(snapshot, entry):
                errors.append(f"{alias}: bound workspace bytes changed")
            if snapshot["project"].get("project_id") != entry["project_id"]:
                errors.append(f"{alias}: project_id does not match bound Project")
            if snapshot["version"].get("document_id") != entry["document_id"]:
                errors.append(f"{alias}: document_id does not match bound DocumentVersion")
            if snapshot["version"].get("version_id") != entry["version_id"]:
                errors.append(f"{alias}: version_id does not match bound DocumentVersion")
            expected_parent = _parent(f"project-{index:03d}", "argument-project", snapshot["project_bytes"])
            if expected_parent not in corpus["parents"]:
                errors.append(f"{alias}: corpus project parent is disconnected")
            if corpus.get("schema_version") in {2, 3, 4, 5}:
                expected_baseline_parent = _parent(
                    f"baseline-{index:03d}",
                    "direct-review-baseline",
                    snapshot["baseline_bytes"],
                )
                if expected_baseline_parent not in corpus["parents"]:
                    errors.append(f"{alias}: corpus baseline parent is disconnected")
            if corpus.get("schema_version") in {3, 4, 5}:
                for triage_index, (_, _, index_bytes) in enumerate(
                    snapshot["triage_indexes"], 1
                ):
                    expected_triage_parent = _parent(
                        f"status-triage-{index:03d}-{triage_index:03d}",
                        "review-status-triage-index",
                        index_bytes,
                    )
                    if expected_triage_parent not in corpus["parents"]:
                        errors.append(f"{alias}: corpus status triage parent is disconnected")
            if corpus.get("schema_version") == 5:
                for session_index, session in enumerate(
                    snapshot["ir_inspection_sessions"], 1
                ):
                    expected_session_parent = _parent(
                        f"ir-inspection-{index:03d}-{session_index:03d}",
                        "gate-a-work-session",
                        session.record_bytes,
                    )
                    if expected_session_parent not in corpus["parents"]:
                        errors.append(
                            f"{alias}: corpus IR inspection session parent is disconnected"
                        )
        except (OSError, WorkbenchError) as exc:
            errors.append(f"{alias}: {exc}")
    seen_aliases: set[str] = set()
    for path, assessment, data in assessments:
        contract_errors = validate_artifact(assessment)
        errors.extend(f"{path.name}: {error}" for error in contract_errors)
        alias = str(assessment.get("project_alias"))
        if alias in seen_aliases:
            errors.append(f"duplicate assessment for {alias}")
        seen_aliases.add(alias)
        entry = project_by_alias.get(alias)
        if assessment.get("corpus_id") != corpus.get("corpus_id"):
            errors.append(f"{path.name}: corpus_id does not match corpus")
        if entry is None:
            errors.append(f"{path.name}: project alias is not in corpus")
            continue
        try:
            snapshot = _project_snapshot(
                entry["workspace_locator"],
                require_status_triage=corpus.get("schema_version") in {3, 4, 5},
                require_controlled_baseline=corpus.get("schema_version") in {4, 5},
                require_ir_inspection_sessions=corpus.get("schema_version") == 5,
            )
            parents = {parent.get("role"): parent for parent in assessment.get("parents", []) if isinstance(parent, dict)}
            if parents.get("corpus", {}).get("sha256") != sha256_bytes(corpus_bytes):
                errors.append(f"{path.name}: corpus parent hash is disconnected")
            if parents.get("project", {}).get("sha256") != snapshot["bindings"]["project"]:
                errors.append(f"{path.name}: project parent hash is disconnected")
            if parents.get("revision-plan", {}).get("sha256") != snapshot["bindings"]["revision_plan_record"]:
                errors.append(f"{path.name}: revision-plan parent hash is disconnected")
            if assessment.get("schema_version") in {2, 3, 4, 5} and parents.get(
                "direct-review-baseline", {}
            ).get("sha256") != snapshot["bindings"]["direct_review_baseline"]:
                errors.append(f"{path.name}: direct baseline parent hash is disconnected")
            if assessment.get("schema_version") in {3, 4, 5}:
                for triage_index, (_, _, index_bytes) in enumerate(
                    snapshot["triage_indexes"], 1
                ):
                    role = f"status-triage-{triage_index:03d}"
                    if parents.get(role, {}).get("sha256") != sha256_bytes(
                        index_bytes
                    ):
                        errors.append(f"{path.name}: {role} parent hash is disconnected")
            if assessment.get("schema_version") == 5:
                expected_timing = {
                    "elapsed_milliseconds": snapshot[
                        "ir_inspection_elapsed_milliseconds"
                    ],
                    "sessions": [
                        {
                            "session_id": session.paths.session_id,
                            "sha256": sha256_bytes(session.record_bytes),
                        }
                        for session in snapshot["ir_inspection_sessions"]
                    ],
                }
                if assessment.get("ir_inspection_timing") != expected_timing:
                    errors.append(
                        f"{path.name}: IR inspection timing is not reproducible"
                    )
                for session_index, session in enumerate(
                    snapshot["ir_inspection_sessions"], 1
                ):
                    role = f"ir-inspection-{session_index:03d}"
                    if parents.get(role, {}).get("sha256") != sha256_bytes(
                        session.record_bytes
                    ):
                        errors.append(
                            f"{path.name}: {role} parent hash is disconnected"
                        )
        except (OSError, WorkbenchError) as exc:
            errors.append(f"{path.name}: {exc}")
    errors.extend(
        _GATE_LIFECYCLE.decision_chain_errors(decisions, digest=sha256_bytes)
    )
    previous_hash: str | None = None
    for path, decision, data in decisions:
        contract_errors = validate_artifact(decision)
        errors.extend(f"{path.name}: {error}" for error in contract_errors)
        parents = {parent.get("role"): parent for parent in decision.get("parents", []) if isinstance(parent, dict)}
        if decision.get("corpus_id") != corpus.get("corpus_id"):
            errors.append(f"{path.name}: corpus_id does not match corpus")
        if parents.get("corpus", {}).get("sha256") != sha256_bytes(corpus_bytes):
            errors.append(f"{path.name}: corpus parent hash is disconnected")
        if previous_hash is not None and parents.get("previous-decision", {}).get("sha256") != previous_hash:
            errors.append(f"{path.name}: previous-decision parent is disconnected")
        previous_hash = sha256_bytes(data)
    if compare_report:
        if paths.report_dir.is_symlink() or not paths.report_dir.is_dir():
            errors.append("Gate A report must be a regular non-symlink directory")
        elif {child.name for child in paths.report_dir.iterdir()} != {"record.json", "product-gate-a.md"}:
            errors.append("Gate A report cache is incomplete or contains unexpected files")
        else:
            try:
                _, expected_record, expected_markdown, derivation_errors = _derive_report(paths)
                errors.extend(derivation_errors)
                if _regular_bytes(paths.report_record, "Gate A report record") != expected_record:
                    errors.append("Gate A report record is not reproducible")
                if _regular_bytes(paths.report_markdown, "Gate A report Markdown") != expected_markdown.encode("utf-8"):
                    errors.append("Gate A report Markdown is not reproducible")
            except (OSError, WorkbenchError) as exc:
                errors.append(f"cannot reproduce Gate A report: {exc}")
    return errors
