"""Private, local Product Gate A corpus and evidence reports.

The gate stores hashes and local workspace locators, never manuscript bytes.
Gate artifacts are evaluation evidence; they are not normal writing-workflow
artifacts and never produce a manuscript score or an automatic pass decision.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argument_adjudication import (
    current_finding_entries,
    human_review_paths,
    latest_adjudications,
    list_adjudications,
)
from argument_contracts import (
    GATE_A_BURDENS,
    GATE_A_COMPARISONS,
    GATE_A_DECISIONS,
    sha256_bytes,
    validate_artifact,
)
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
    "correction_minutes",
    "missed_claims",
    "wrong_claim_types",
    "wrong_relations",
    "rhetoric_as_claims",
    "reversed_attributions",
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


def _project_snapshot(project_dir: Path | str) -> dict[str, Any]:
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
    corrections = 0
    if workspace.corrections_dir.exists():
        corrections = len(list(workspace.corrections_dir.glob("IC[0-9][0-9][0-9][0-9].json")))
    return {
        "workspace": workspace,
        "project": project,
        "version": version,
        "project_bytes": data["project"],
        "plan_bytes": data["revision_plan_record"],
        "bindings": {
            "project": sha256_bytes(data["project"]),
            "document_version": sha256_bytes(data["document_version"]),
            "source": sha256_bytes(source_bytes),
            "reviewed_ir_record": sha256_bytes(data["reviewed_ir_record"]),
            "reviewed_ir_payload": sha256_bytes(data["reviewed_ir_payload"]),
            "revision_plan_record": sha256_bytes(data["revision_plan_record"]),
            "revision_plan_markdown": sha256_bytes(data["revision_plan_markdown"]),
        },
        "claims": len(reviewed.get("claims", [])),
        "corrections": corrections,
        "summary": {key: int(summary.get(key, 0)) for key in ("accept", "reject", "defer", "open")},
    }


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
        findings_available = True
        try:
            findings = current_finding_entries(workspace.root)
        except WorkbenchError as exc:
            findings_available = False
            findings = []
            errors.append(f"current-review: {exc}")
        current_ids = {str(entry.value.get("finding_id")) for entry in findings}
        model_counts = {"fail": 0, "uncertain": 0}
        for finding in findings:
            model_counts[str(finding.value["verdict"])] += 1
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
        ready = (
            not errors
            and findings_available
            and decision_counts["open"] == 0
            and plan_ready
        )
        if workspace_invalid:
            next_command = f'python critic_runner.py ir verify-project "{workspace.root}"'
        elif not findings_available:
            next_command = f'python critic_runner.py ir review prepare "{workspace.root}"'
        elif decision_counts["open"]:
            next_command = (
                f'python critic_runner.py ir adjudicate "{workspace.root}" '
                "--summary-only"
            )
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
                "review_available": findings_available,
                "model_findings": model_counts,
                "human_decisions": decision_counts,
                "revision_plan": plan_ready,
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
        lines.extend(
            [
                f"{project['alias']} - {project['title']}",
                f"  Workspace: {project['workspace']}",
                f"  Claims: {project['claims']} · Corrections: {project['corrections']}",
                f"  Model Findings: {model['fail']} FAIL · {model['uncertain']} UNCERTAIN",
                f"  Human decisions: {human['accept']} accept · {human['reject']} reject · "
                f"{human['defer']} defer · {human['open']} open",
                f"  Revision plan: {'ready' if project['revision_plan'] else 'missing'}",
                f"  Gate capture: {'ready' if project['ready_for_capture'] else 'not ready'}",
            ]
        )
        for error in project["errors"]:
            lines.append(f"  Error: {error}")
        lines.extend([f"  Next: {project['next_command']}", ""])
    summary = readiness["summary"]
    lines.extend(
        [
            f"Summary: {summary['ready_for_capture']}/{summary['projects']} projects ready; "
            f"{summary['open_findings']} open Findings",
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
    corpus_id = "GA-" + uuid.uuid4().hex[:12]
    corpus = {
        "schema_version": 1,
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
    temporary = Path(tempfile.mkdtemp(prefix=f".{paths.root.name}.", dir=paths.root.parent))
    try:
        _write_new(temporary / "corpus.json", json_bytes(corpus))
        (temporary / "assessments").mkdir()
        (temporary / "decisions").mkdir()
        (temporary / "report").mkdir()
        temporary.replace(paths.root)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    rebuild_gate_report(paths.root)
    return paths


def _read_corpus(paths: GatePaths) -> tuple[dict[str, Any], bytes]:
    corpus, data = _read_json(paths.corpus)
    errors = validate_artifact(corpus)
    if errors:
        raise WorkbenchError("Gate A corpus is invalid: " + "; ".join(errors))
    return corpus, data


def _numbered_entries(
    directory: Path, pattern: re.Pattern[str], label: str
) -> list[tuple[Path, dict[str, Any], bytes]]:
    if directory.is_symlink() or not directory.is_dir():
        raise WorkbenchError(f"{label} must be a regular non-symlink directory")
    entries: list[tuple[Path, dict[str, Any], bytes]] = []
    for child in sorted(directory.iterdir()):
        match = pattern.fullmatch(child.name)
        if match is None:
            raise WorkbenchError(f"unexpected {label} entry: {child.name}")
        value, data = _read_json(child)
        entries.append((child, value, data))
    actual = [int(pattern.fullmatch(path.name).group(1)) for path, _, _ in entries]
    if actual != list(range(1, len(entries) + 1)):
        raise WorkbenchError(f"{label} IDs must be continuous from 0001")
    return entries


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
    if set(metrics) != set(METRIC_KEYS) or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in metrics.values()
    ):
        raise WorkbenchError("all Gate A metrics must be non-negative integers")
    if not regression_anchors or any(
        not isinstance(anchor, str) or not anchor.strip()
        for anchor in regression_anchors
    ):
        raise WorkbenchError("at least one non-empty regression anchor is required")
    if len(regression_anchors) != len(set(regression_anchors)):
        raise WorkbenchError("regression anchors must not contain duplicates")
    snapshot = _project_snapshot(entry["workspace_locator"])
    if snapshot["bindings"] != entry["bindings"]:
        raise WorkbenchError(f"{project_alias} changed after corpus capture")
    assessment_id = f"AS{len(assessments) + 1:04d}"
    assessment = {
        "schema_version": 1,
        "artifact": "product-gate-a-assessment",
        "artifact_id": assessment_id,
        "lifecycle": "immutable",
        "provenance": _provenance("human-confirmed", utc_now(), producer),
        "parents": [
            _parent("corpus", "product-gate-a-corpus", corpus_bytes),
            _parent("project", "argument-project", snapshot["project_bytes"]),
            _parent("revision-plan", "revision-plan-record", snapshot["plan_bytes"]),
        ],
        "corpus_id": corpus["corpus_id"],
        "project_alias": project_alias,
        "comparison_to_direct_chat": comparison_to_direct_chat,
        "correction_burden": correction_burden,
        "metrics": {key: metrics[key] for key in METRIC_KEYS},
        "regression_anchors": regression_anchors,
        "actual_revision_notes": actual_revision_notes,
        "notes": notes,
    }
    contract_errors = validate_artifact(assessment)
    if contract_errors:
        raise WorkbenchError("internal Gate A assessment error: " + "; ".join(contract_errors))
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
    report, _, _, _ = _derive_report(paths)
    if decision not in GATE_A_DECISIONS:
        raise WorkbenchError("Gate A decision must be pass/fail/defer")
    if not reason.strip():
        raise WorkbenchError("Gate A decision requires a human reason")
    if decision == "pass" and not report["readiness"]["ready_for_human_decision"]:
        raise WorkbenchError("Gate A cannot pass before all 3-5 workflows and assessments are complete")
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
    errors = validate_artifact(value)
    if errors:
        raise WorkbenchError("internal Gate A decision error: " + "; ".join(errors))
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
        "metrics": {key: 0 for key in METRIC_KEYS},
    }
    complete_workflows = 0
    parents = [_parent("corpus", "product-gate-a-corpus", corpus_bytes)]
    latest_created_at = corpus["provenance"]["created_at"]
    for index, entry in enumerate(corpus["entries"], 1):
        alias = entry["alias"]
        try:
            snapshot = _project_snapshot(entry["workspace_locator"])
            bindings_match = snapshot["bindings"] == entry["bindings"]
        except (OSError, WorkbenchError) as exc:
            errors.append(f"{alias}: {exc}")
            bindings_match = False
            snapshot = {"claims": 0, "corrections": 0, "summary": {key: 0 for key in ("accept", "reject", "defer", "open")}}
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
            for key in METRIC_KEYS:
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
        corpus["corpus_id"], readiness, totals, observations, project_rows, latest_decision
    )
    markdown_bytes = markdown.encode("utf-8")
    report = {
        "schema_version": 1,
        "artifact": "product-gate-a-report",
        "artifact_id": corpus["corpus_id"] + "-report",
        "lifecycle": "derived-replaceable",
        "provenance": _provenance("deterministic", latest_created_at, "product-gate-a-report-v1"),
        "parents": parents,
        "corpus_id": corpus["corpus_id"],
        "readiness": readiness,
        "workflow_totals": totals,
        "human_observations": observations,
        "projects": project_rows,
        "gate_decision": latest_decision["decision"] if latest_decision else None,
        "payload": {"relative_path": "product-gate-a.md", "sha256": sha256_bytes(markdown_bytes)},
    }
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
        "",
        "## Human Observations",
        "",
        f"- Clearer than direct chat / Same / Worse / Uncertain: {observations['clearer']} / {observations['same']} / {observations['worse']} / {observations['uncertain']}",
        f"- Acceptable / High / Uncertain correction burden: {observations['acceptable_burden']} / {observations['high_burden']} / {observations['uncertain_burden']}",
        f"- Regression anchors recorded: {observations['regression_anchors']}",
        f"- Projects with actual revision notes: {observations['actual_revisions_recorded']}",
        "- Extraction/correction observations:",
    ]
    for key in METRIC_KEYS:
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
                "",
            ]
        )
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
    changed = False
    paths.report_dir.mkdir(parents=True, exist_ok=True)
    for path, data in ((paths.report_markdown, markdown_bytes), (paths.report_record, record_bytes)):
        if path.exists() and path.is_symlink():
            raise WorkbenchError(f"Gate A report artifact must not be a symlink: {path}")
        if not path.exists() or path.read_bytes() != data:
            _atomic_write(path, data)
            changed = True
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
    actual_root_entries = {child.name for child in paths.root.iterdir()}
    if actual_root_entries != expected_root_entries:
        errors.append("Gate A root contains unexpected or missing entries")
    project_by_alias: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(corpus["entries"], 1):
        alias = entry["alias"]
        project_by_alias[alias] = entry
        try:
            snapshot = _project_snapshot(entry["workspace_locator"])
            if snapshot["bindings"] != entry["bindings"]:
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
            snapshot = _project_snapshot(entry["workspace_locator"])
            parents = {parent.get("role"): parent for parent in assessment.get("parents", []) if isinstance(parent, dict)}
            if parents.get("corpus", {}).get("sha256") != sha256_bytes(corpus_bytes):
                errors.append(f"{path.name}: corpus parent hash is disconnected")
            if parents.get("project", {}).get("sha256") != snapshot["bindings"]["project"]:
                errors.append(f"{path.name}: project parent hash is disconnected")
            if parents.get("revision-plan", {}).get("sha256") != snapshot["bindings"]["revision_plan_record"]:
                errors.append(f"{path.name}: revision-plan parent hash is disconnected")
        except (OSError, WorkbenchError) as exc:
            errors.append(f"{path.name}: {exc}")
    previous_hash: str | None = None
    for path, decision, data in decisions:
        contract_errors = validate_artifact(decision)
        errors.extend(f"{path.name}: {error}" for error in contract_errors)
        parents = {parent.get("role"): parent for parent in decision.get("parents", []) if isinstance(parent, dict)}
        if decision.get("corpus_id") != corpus.get("corpus_id"):
            errors.append(f"{path.name}: corpus_id does not match corpus")
        if parents.get("corpus", {}).get("sha256") != sha256_bytes(corpus_bytes):
            errors.append(f"{path.name}: corpus parent hash is disconnected")
        if decision.get("supersedes") != previous_hash:
            errors.append(f"{path.name}: supersedes does not identify the prior gate decision")
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
