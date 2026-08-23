"""Local, auditable Product Gate B evidence for real multi-version writing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argument_contracts import sha256_bytes, validate_artifact
from argument_lineage import list_lineage_analyses, list_lineage_decisions
from argument_resolution import list_resolutions
from argument_workbench import WorkbenchError, _atomic_write, _parent, _provenance, _read_json, _write_new, json_bytes, list_version_ids, utc_now, verify_project_versions, workspace_paths


ASSESSMENT_PATTERN = re.compile(r"BA([0-9]{4})\.json\Z")
DECISION_PATTERN = re.compile(r"BD([0-9]{4})\.json\Z")


@dataclass(frozen=True)
class GateBPaths:
    root: Path
    @property
    def corpus(self): return self.root / "corpus.json"
    @property
    def assessments(self): return self.root / "assessments"
    @property
    def decisions(self): return self.root / "decisions"
    @property
    def report_dir(self): return self.root / "report"
    @property
    def report_record(self): return self.report_dir / "record.json"
    @property
    def report_markdown(self): return self.report_dir / "product-gate-b.md"


def gate_b_paths(root: Path | str) -> GateBPaths:
    return GateBPaths(Path(root).expanduser().resolve())


def _snapshot(project_dir: Path | str) -> dict[str, Any]:
    workspace = workspace_paths(project_dir)
    errors = verify_project_versions(workspace)
    if errors: raise WorkbenchError("Gate B project is invalid: " + "; ".join(errors))
    versions = list_version_ids(workspace)
    if len(versions) < 2: raise WorkbenchError("Gate B requires a real multi-version project")
    project, project_bytes = _read_json(workspace.project)
    document, _ = _read_json(workspace.document)
    version_hashes: list[str] = []; reviewed_hashes: list[str] = []
    for version in versions:
        paths = workspace_paths(workspace, version)
        _, version_bytes = _read_json(paths.version); _, reviewed_bytes = _read_json(paths.reviewed_record)
        version_hashes.append(sha256_bytes(version_bytes)); reviewed_hashes.append(sha256_bytes(reviewed_bytes))
    lineage_hashes: list[str] = []; observed: set[str] = set()
    for from_version, to_version in zip(versions, versions[1:]):
        analyses = list_lineage_analyses(workspace, from_version=from_version, to_version=to_version)
        if not analyses: raise WorkbenchError(f"Gate B requires lineage analysis for {from_version}--{to_version}")
        found_human = False
        for analysis in analyses:
            for _, decision, data in list_lineage_decisions(analysis):
                found_human = True; lineage_hashes.append(sha256_bytes(data))
                if decision.get("status") == "human_confirmed": observed.add(str(decision.get("relation")))
        if not found_human: raise WorkbenchError(f"Gate B requires human lineage decisions for {from_version}--{to_version}")
    resolution_hashes: list[str] = []
    for resolution in list_resolutions(workspace):
        for path in sorted(resolution.decisions_dir.glob("RD[0-9][0-9][0-9][0-9].json")):
            _, data = _read_json(path); resolution_hashes.append(sha256_bytes(data))
    return {"workspace": workspace, "project": project, "project_bytes": project_bytes, "document": document, "versions": versions, "bindings": {"project": sha256_bytes(project_bytes), "document_versions": version_hashes, "reviewed_irs": reviewed_hashes, "lineage_decisions": lineage_hashes, "resolution_decisions": resolution_hashes}, "observed_relations": sorted(observed)}


def initialize_gate_b(output: Path | str, projects: list[Path | str]) -> GateBPaths:
    if not 2 <= len(projects) <= 3: raise WorkbenchError("Product Gate B requires 2-3 real multi-version projects")
    paths = gate_b_paths(output)
    if paths.root.exists() or paths.root.is_symlink(): raise WorkbenchError("refusing to overwrite an existing Gate B directory")
    snapshots = [_snapshot(project) for project in projects]
    locators = [str(snapshot["workspace"].root) for snapshot in snapshots]
    project_hashes = [str(snapshot["bindings"]["project"]) for snapshot in snapshots]
    if len(locators) != len(set(locators)) or len(project_hashes) != len(set(project_hashes)):
        raise WorkbenchError("Gate B projects must be distinct workspace artifacts")
    created_at = utc_now()
    corpus = {"schema_version": 1, "artifact": "product-gate-b-corpus", "artifact_id": "GB1", "lifecycle": "immutable", "provenance": _provenance("human-confirmed", created_at, "local-user"), "parents": [], "gate_id": "GB1", "projects": [{"alias": f"P{index}", "locator": str(snapshot["workspace"].root), "project_id": snapshot["project"]["project_id"], "document_id": snapshot["document"]["document_id"], "versions": snapshot["versions"], "bindings": snapshot["bindings"], "observed_relations": snapshot["observed_relations"]} for index, snapshot in enumerate(snapshots, 1)]}
    errors = validate_artifact(corpus)
    if errors: raise WorkbenchError("internal Gate B corpus error: " + "; ".join(errors))
    paths.root.mkdir(parents=True); paths.assessments.mkdir(); paths.decisions.mkdir(); paths.report_dir.mkdir()
    _write_new(paths.corpus, json_bytes(corpus)); rebuild_gate_b_report(paths.root)
    return paths


def _entries(directory: Path, pattern: re.Pattern[str]):
    entries = []
    if not directory.exists(): return entries
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file() or pattern.fullmatch(path.name) is None: raise WorkbenchError(f"unexpected Gate B evidence entry: {path.name}")
        value, data = _read_json(path); entries.append((path, value, data))
    return entries


def append_gate_b_assessment(root: Path | str, project_alias: str, *, lineage_correction_minutes: int, lineage_reasonable: str, split_merge_worked: str, finding_inheritance_correct: str, resolved_stopped_reappearing: str, unresolved_persisted: str, revision_rationale_clarity: str, notes: str) -> Path:
    paths = gate_b_paths(root); corpus, corpus_bytes = _read_json(paths.corpus)
    aliases = {project["alias"] for project in corpus["projects"]}
    if project_alias not in aliases: raise WorkbenchError(f"unknown Gate B project alias: {project_alias}")
    existing = _entries(paths.assessments, ASSESSMENT_PATTERN)
    if project_alias in {entry[1]["project_alias"] for entry in existing}: raise WorkbenchError("Gate B project is already assessed")
    assessment_id = f"BA{len(existing) + 1:04d}"
    value = {"schema_version": 1, "artifact": "product-gate-b-assessment", "artifact_id": assessment_id, "lifecycle": "immutable", "provenance": _provenance("human-confirmed", utc_now(), "local-user"), "parents": [_parent("corpus", "product-gate-b-corpus", corpus_bytes)], "assessment_id": assessment_id, "project_alias": project_alias, "lineage_correction_minutes": lineage_correction_minutes, "lineage_reasonable": lineage_reasonable, "split_merge_worked": split_merge_worked, "finding_inheritance_correct": finding_inheritance_correct, "resolved_stopped_reappearing": resolved_stopped_reappearing, "unresolved_persisted": unresolved_persisted, "revision_rationale_clarity": revision_rationale_clarity, "notes": notes}
    errors = validate_artifact(value)
    if errors: raise WorkbenchError("invalid Gate B assessment: " + "; ".join(errors))
    output = paths.assessments / f"{assessment_id}.json"; _write_new(output, json_bytes(value)); rebuild_gate_b_report(paths.root); return output


def gate_b_readiness(root: Path | str) -> list[str]:
    paths = gate_b_paths(root); corpus, _ = _read_json(paths.corpus); assessments = _entries(paths.assessments, ASSESSMENT_PATTERN)
    issues: list[str] = []
    if len(assessments) != len(corpus["projects"]): issues.append("every corpus project needs one human assessment")
    by_alias = {entry[1]["project_alias"]: entry[1] for entry in assessments}
    if not any(relation in {"split", "merged"} for project in corpus["projects"] for relation in project["observed_relations"]): issues.append("corpus has no human-confirmed split or merged lineage")
    if not any(project["bindings"]["resolution_decisions"] for project in corpus["projects"]): issues.append("corpus has no human Finding Resolution decision")
    for project in corpus["projects"]:
        assessment = by_alias.get(project["alias"])
        if assessment is None: continue
        for field in ("lineage_reasonable", "finding_inheritance_correct"):
            if assessment[field] != "yes": issues.append(f"{project['alias']} does not confirm {field}")
        if assessment["revision_rationale_clarity"] != "clear": issues.append(f"{project['alias']} revision rationale is not clear")
    if assessments and not any(entry[1]["resolved_stopped_reappearing"] == "yes" for entry in assessments): issues.append("no project confirms resolved Findings stopped reappearing")
    if assessments and not any(entry[1]["unresolved_persisted"] == "yes" for entry in assessments): issues.append("no project confirms unresolved Findings persisted")
    return issues


def append_gate_b_decision(root: Path | str, decision: str, reason: str) -> Path:
    paths = gate_b_paths(root); corpus, corpus_bytes = _read_json(paths.corpus)
    if decision == "pass":
        issues = gate_b_readiness(paths.root)
        if issues: raise WorkbenchError("Gate B cannot pass: " + "; ".join(issues))
    existing = _entries(paths.decisions, DECISION_PATTERN); decision_id = f"BD{len(existing) + 1:04d}"; supersedes = sha256_bytes(existing[-1][2]) if existing else None
    parents = [_parent("corpus", "product-gate-b-corpus", corpus_bytes)]
    if existing: parents.append(_parent("previous-decision", "product-gate-b-decision", existing[-1][2]))
    value = {"schema_version": 1, "artifact": "product-gate-b-decision", "artifact_id": decision_id, "lifecycle": "immutable", "provenance": _provenance("human-confirmed", utc_now(), "local-user"), "parents": parents, "decision_id": decision_id, "decision": decision, "reason": reason, "supersedes": supersedes}
    errors = validate_artifact(value)
    if errors: raise WorkbenchError("invalid Gate B decision: " + "; ".join(errors))
    output = paths.decisions / f"{decision_id}.json"; _write_new(output, json_bytes(value)); rebuild_gate_b_report(paths.root); return output


def _derive_report(paths: GateBPaths):
    corpus, corpus_bytes = _read_json(paths.corpus); assessments = _entries(paths.assessments, ASSESSMENT_PATTERN); decisions = _entries(paths.decisions, DECISION_PATTERN)
    summary = {"projects": len(corpus["projects"]), "assessed": len(assessments), "lineage_decisions": sum(len(project["bindings"]["lineage_decisions"]) for project in corpus["projects"]), "resolution_decisions": sum(len(project["bindings"]["resolution_decisions"]) for project in corpus["projects"]), "split_merge_projects": sum(any(relation in {"split", "merged"} for relation in project["observed_relations"]) for project in corpus["projects"])}
    current_decision = decisions[-1][1]["decision"] if decisions else None; issues = gate_b_readiness(paths.root)
    lines = ["# Product Gate B", "", f"- Projects: {summary['projects']}", f"- Assessed: {summary['assessed']}", f"- Human lineage decisions: {summary['lineage_decisions']}", f"- Human resolution decisions: {summary['resolution_decisions']}", f"- Gate decision: `{current_decision or 'pending'}`", "", "## Readiness", ""]
    lines.extend([f"- {issue}" for issue in issues] if issues else ["- Ready for a human pass/fail/defer decision."])
    lines.extend(["", "No score is computed. Gate B remains a human product decision.", ""]); markdown = "\n".join(lines).encode("utf-8")
    parents = [_parent("corpus", "product-gate-b-corpus", corpus_bytes)] + [_parent(f"assessment-{index:04d}", "product-gate-b-assessment", entry[2]) for index, entry in enumerate(assessments, 1)] + [_parent(f"decision-{index:04d}", "product-gate-b-decision", entry[2]) for index, entry in enumerate(decisions, 1)]
    record = {"schema_version": 1, "artifact": "product-gate-b-report", "artifact_id": "GB1-report", "lifecycle": "derived-replaceable", "provenance": _provenance("deterministic", str(corpus["provenance"]["created_at"]), "workbench-product-gate-b-v1"), "parents": parents, "gate_id": corpus["gate_id"], "summary": summary, "gate_decision": current_decision, "payload": {"relative_path": "product-gate-b.md", "sha256": sha256_bytes(markdown)}}
    errors = validate_artifact(record)
    if errors: raise WorkbenchError("internal Gate B report error: " + "; ".join(errors))
    return record, json_bytes(record), markdown


def rebuild_gate_b_report(root: Path | str):
    paths = gate_b_paths(root); record, record_bytes, markdown = _derive_report(paths); changed = False
    for path, data in ((paths.report_record, record_bytes), (paths.report_markdown, markdown)):
        if not path.exists() or path.read_bytes() != data: _atomic_write(path, data); changed = True
    return paths.report_markdown, changed


def verify_gate_b(root: Path | str) -> list[str]:
    paths = gate_b_paths(root); errors: list[str] = []
    try:
        corpus, corpus_bytes = _read_json(paths.corpus); errors.extend(validate_artifact(corpus))
        for project in corpus["projects"]:
            snapshot = _snapshot(project["locator"])
            if snapshot["bindings"] != project["bindings"] or snapshot["versions"] != project["versions"] or snapshot["observed_relations"] != project["observed_relations"]: errors.append(f"{project['alias']}: bound project state changed")
        previous = None
        for _, assessment, _ in _entries(paths.assessments, ASSESSMENT_PATTERN): errors.extend(validate_artifact(assessment))
        for _, decision, data in _entries(paths.decisions, DECISION_PATTERN):
            errors.extend(validate_artifact(decision))
            if decision["supersedes"] != previous: errors.append("Gate B decision supersedes chain is disconnected")
            previous = sha256_bytes(data)
        _, expected_record, expected_markdown = _derive_report(paths); actual_record, actual_bytes = _read_json(paths.report_record)
        if actual_bytes != expected_record or paths.report_markdown.read_bytes() != expected_markdown: errors.append("Gate B report is not reproducible")
    except (OSError, WorkbenchError, KeyError, TypeError) as exc: errors.append(str(exc))
    return errors
