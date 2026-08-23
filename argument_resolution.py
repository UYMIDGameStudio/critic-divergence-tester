"""Finding Resolution by rerunning the original Lens on confirmed descendants."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argument_adjudication import (
    _all_finding_entries,
    human_review_paths,
    latest_adjudications,
    list_adjudications,
    list_revision_actions,
)
from argument_contracts import FINDING_VERDICTS, RESOLUTION_STATUSES, sha256_bytes, validate_artifact
from argument_ir import _eligible_pass_support_paths
from argument_lineage import (
    _current_decisions,
    list_lineage_analyses,
    selected_lineage_analysis,
)
from argument_perspective import PerspectiveReviewPaths
from argument_workbench import (
    WorkspacePaths,
    WorkbenchError,
    _atomic_write,
    _parent,
    _provenance,
    _read_json,
    _write_new,
    json_bytes,
    parse_json_strict,
    utc_now,
    verify_project_versions,
    workspace_paths,
)


RESOLUTION_PATTERN = re.compile(r"RR([1-9][0-9]*)\Z")
ATTEMPT_PATTERN = re.compile(r"attempt-([0-9]{4})\Z")
DECISION_PATTERN = re.compile(r"RD([0-9]{4})\.json\Z")


@dataclass(frozen=True)
class ResolutionPaths:
    workspace: WorkspacePaths
    from_version: str
    to_version: str
    resolution_id: str

    @property
    def root_dir(self) -> Path:
        return self.workspace.document_dir / "finding-resolutions" / f"{self.from_version}--{self.to_version}"

    @property
    def root(self) -> Path:
        return self.root_dir / self.resolution_id

    @property
    def record(self) -> Path:
        return self.root / "retest-run.json"

    @property
    def attempts_dir(self) -> Path:
        return self.root / "results"

    @property
    def decisions_dir(self) -> Path:
        return self.root / "human-decisions"

    def attempt_dir(self, attempt_id: str) -> Path:
        return self.attempts_dir / attempt_id

    def derived_dir(self, attempt_id: str) -> Path:
        return self.root / "derived" / attempt_id


def list_resolutions(project_dir: Path | str) -> list[ResolutionPaths]:
    workspace = workspace_paths(project_dir)
    base = workspace.document_dir / "finding-resolutions"
    if not base.exists():
        return []
    if base.is_symlink() or not base.is_dir():
        raise WorkbenchError("finding-resolutions must be a regular directory")
    found: list[ResolutionPaths] = []
    for pair in base.iterdir():
        match = re.fullmatch(r"(V[1-9][0-9]*)--(V[1-9][0-9]*)", pair.name)
        if pair.is_symlink() or not pair.is_dir() or match is None:
            raise WorkbenchError(f"unexpected finding-resolutions entry: {pair.name}")
        for child in pair.iterdir():
            if child.is_symlink() or not child.is_dir() or RESOLUTION_PATTERN.fullmatch(child.name) is None:
                raise WorkbenchError(f"unexpected resolution entry: {child.name}")
            found.append(ResolutionPaths(workspace, match.group(1), match.group(2), child.name))
    return sorted(found, key=lambda item: int(item.resolution_id[2:]))


def selected_resolution(project_dir: Path | str, resolution_id: str | None) -> ResolutionPaths:
    items = list_resolutions(project_dir)
    if not items:
        raise WorkbenchError("no Finding Resolution exists; run `ir resolve prepare`")
    if resolution_id is None:
        return items[-1]
    normalized = resolution_id.strip().upper()
    for item in items:
        if item.resolution_id == normalized:
            return item
    raise WorkbenchError(f"unknown Finding Resolution: {normalized}")


def _accepted_finding(workspace: WorkspacePaths, finding_id: str):
    review_paths = human_review_paths(workspace)
    findings = {str(item.value["finding_id"]): item for item in _all_finding_entries(workspace)}
    finding = findings.get(finding_id)
    if finding is None:
        raise WorkbenchError(f"unknown original Finding in {workspace.version_id}: {finding_id}")
    latest = latest_adjudications(list_adjudications(review_paths)).get(finding_id)
    if latest is None or latest[1].get("decision") != "accept":
        raise WorkbenchError("Finding Resolution requires the latest human adjudication to be accept")
    actions = [entry for entry in list_revision_actions(review_paths) if entry[1].get("adjudication_id") == latest[1].get("adjudication_id")]
    if not actions:
        raise WorkbenchError("accepted Finding has no RevisionAction")
    return finding, latest, actions


def _confirmed_lineage(project_dir: Path | str, from_version: str, to_version: str, target_claim: str, decision_id: str | None):
    analysis = selected_lineage_analysis(project_dir, from_version=from_version, to_version=to_version)
    matches: list[tuple[dict[str, Any], bytes]] = []
    for decision, data in _current_decisions(analysis).values():
        if decision.get("status") != "human_confirmed" or target_claim not in decision.get("from_claims", []):
            continue
        if decision_id is None or decision.get("artifact_id") == decision_id:
            matches.append((decision, data))
    if len(matches) != 1:
        raise WorkbenchError(
            f"expected exactly one human-confirmed lineage for {target_claim}; found {len(matches)}"
        )
    return analysis, matches[0]


def _lens_snapshot(finding) -> tuple[bytes, str, bytes]:
    if isinstance(finding.review, PerspectiveReviewPaths):
        _, record_bytes = _read_json(finding.review.protocol_record)
        protocol_text = finding.review.protocol.read_bytes()
        return record_bytes, "perspective-lens-protocol", protocol_text
    library, library_bytes = _read_json(finding.review.library)
    check_id = str(finding.value["lens"]["check_id"])
    check = next((item for item in library.get("checks", []) if item.get("id") == check_id), None)
    if check is None:
        raise WorkbenchError(f"original check is missing from its snapshotted library: {check_id}")
    return library_bytes, "argument-check-library", json_bytes(check)


def _render_prompt(finding: dict[str, Any], actions: list[dict[str, Any]], lineage: dict[str, Any], target_ir: dict[str, Any], lens_text: bytes, source: dict[str, str]) -> bytes:
    descendants = list(lineage["to_claims"])
    shape = {
        "schema_version": 1, "artifact": "resolution-retest-results", "source": source,
        "status": "complete", "unverified": [],
        "results": [{"target_claim": claim, "verdict": "pass", "reason": "Apply only the original Lens.", "basis_refs": [claim], "support_refs": [], "support_paths": [], "analysis": "Lens-specific analysis."} for claim in descendants],
    }
    lens_text_decoded = lens_text.decode("utf-8")
    text = (
        "# Original-Lens Finding retest\n\n"
        "Do not answer the generic question ‘was this resolved?’. Re-execute only the exact original Rule or complete Perspective Lens below against each listed descendant Claim. "
        "Return results in descendant order. A PASS requires the original failure to be absent under the same Lens; FAIL means it persists; UNCERTAIN means the available descendant context cannot decide. "
        "For Rule Lens PASS, obey its original evidence_policy and give version-qualified support_refs plus exact relation_ids in support_paths. Perspective Lenses use basis_refs and leave support fields empty.\n\n"
        "## Original Finding\n\n```json\n" + json.dumps(finding, ensure_ascii=False, indent=2) + "\n```\n\n"
        "## Human RevisionActions\n\n```json\n" + json.dumps(actions, ensure_ascii=False, indent=2) + "\n```\n\n"
        "## Human-confirmed Claim Lineage\n\n```json\n" + json.dumps(lineage, ensure_ascii=False, indent=2) + "\n```\n\n"
        "## Exact original Lens\n\n```text\n" + lens_text_decoded + "\n```\n\n"
        "## Required JSON\n\n```json\n" + json.dumps(shape, ensure_ascii=False, indent=2) + "\n```\n\n"
        "## Descendant Reviewed Argument IR\n\n```json\n" + json.dumps(target_ir, ensure_ascii=False, indent=2) + "\n```\n"
    )
    return text.encode("utf-8")


def prepare_resolution(project_dir: Path | str, finding_id: str, *, from_version: str, to_version: str, lineage_decision_id: str | None = None) -> tuple[ResolutionPaths, bool]:
    root = workspace_paths(project_dir)
    errors = verify_project_versions(root)
    if errors:
        raise WorkbenchError("Argument Workbench project is invalid: " + "; ".join(errors))
    from_paths = workspace_paths(root, from_version)
    to_paths = workspace_paths(root, to_version)
    document, _ = _read_json(root.document)
    finding, adjudication, actions = _accepted_finding(from_paths, finding_id)
    _, (confirmed, confirmed_bytes) = _confirmed_lineage(root, from_paths.version_id, to_paths.version_id, str(finding.value["target_claim"]), lineage_decision_id)
    descendants = list(confirmed["to_claims"])
    target_ir, target_ir_bytes = _read_json(to_paths.reviewed_payload)
    known = {f"{to_paths.version_id}:{claim['id']}" for claim in target_ir["claims"]}
    if not set(descendants).issubset(known):
        raise WorkbenchError("confirmed lineage references Claims absent from descendant Reviewed IR")
    protocol_bytes, protocol_artifact, lens_text = _lens_snapshot(finding)
    source = {"original_finding_sha256": sha256_bytes(finding.data), "target_ir_sha256": sha256_bytes(target_ir_bytes), "lens_protocol_sha256": sha256_bytes(protocol_bytes)}
    prompt_bytes = _render_prompt(finding.value, [item[1] for item in actions], confirmed, target_ir, lens_text, source)
    for existing in list_resolutions(root):
        if existing.from_version != from_paths.version_id or existing.to_version != to_paths.version_id:
            continue
        record, _ = _read_json(existing.record)
        parents = {parent["role"]: parent["sha256"] for parent in record["parents"]}
        if record.get("original_finding_id") == finding_id and parents.get("confirmed-lineage") == sha256_bytes(confirmed_bytes) and parents.get("target-ir") == sha256_bytes(target_ir_bytes):
            return existing, False
    resolution_id = f"RR{len(list_resolutions(root)) + 1}"
    paths = ResolutionPaths(root, from_paths.version_id, to_paths.version_id, resolution_id)
    action_bounds = [{"relative_path": f"revision-actions/{item[1]['action_id']}.json", "sha256": sha256_bytes(item[2])} for item in actions]
    parents = [_parent("original-finding", "argument-finding", finding.data), _parent("accepted-adjudication", "finding-adjudication", adjudication[2])]
    parents.extend(_parent(f"revision-action-{index:04d}", "revision-action", action[2]) for index, action in enumerate(actions, 1))
    parents.extend([_parent("confirmed-lineage", "claim-lineage", confirmed_bytes), _parent("target-ir", "argument-ir", target_ir_bytes), _parent("lens-protocol", protocol_artifact, protocol_bytes)])
    record = {
        "schema_version": 1, "artifact": "resolution-retest-run", "artifact_id": resolution_id, "lifecycle": "immutable",
        "provenance": _provenance("deterministic", utc_now(), "workbench-finding-resolution-v1"), "parents": parents,
        "resolution_id": resolution_id, "document_id": str(document["document_id"]),
        "from_version": from_paths.version_id, "to_version": to_paths.version_id, "original_finding_id": finding_id,
        "descendant_claims": descendants, "lens": dict(finding.value["lens"]),
        "original_finding": {"relative_path": "original-finding.json", "sha256": sha256_bytes(finding.data)},
        "accepted_adjudication": {"relative_path": "accepted-adjudication.json", "sha256": sha256_bytes(adjudication[2])},
        "revision_actions": action_bounds, "confirmed_lineage": {"relative_path": "confirmed-lineage.json", "sha256": sha256_bytes(confirmed_bytes)},
        "target_ir": {"relative_path": "target-argument-ir.json", "sha256": sha256_bytes(target_ir_bytes)},
        "lens_protocol": {"relative_path": "lens-protocol.json", "sha256": sha256_bytes(protocol_bytes)},
        "lens_content": {"relative_path": "lens-content.txt", "sha256": sha256_bytes(lens_text)},
        "prompt": {"relative_path": "resolution-retest-prompt.md", "sha256": sha256_bytes(prompt_bytes)},
    }
    contract_errors = validate_artifact(record)
    if contract_errors:
        raise WorkbenchError("internal resolution run contract error: " + "; ".join(contract_errors))
    paths.root_dir.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{resolution_id}.", dir=paths.root_dir))
    try:
        files = [("retest-run.json", json_bytes(record)), ("original-finding.json", finding.data), ("accepted-adjudication.json", adjudication[2]), ("confirmed-lineage.json", confirmed_bytes), ("target-argument-ir.json", target_ir_bytes), ("lens-protocol.json", protocol_bytes), ("lens-content.txt", lens_text), ("resolution-retest-prompt.md", prompt_bytes)]
        files.extend((f"revision-actions/{item[1]['action_id']}.json", item[2]) for item in actions)
        for relative, data in files:
            _write_new(temporary / relative, data)
        (temporary / "results").mkdir(); (temporary / "derived").mkdir(); (temporary / "human-decisions").mkdir()
        os.replace(temporary, paths.root)
    except Exception:
        if temporary.exists(): shutil.rmtree(temporary)
        raise
    if not descendants:
        _write_obsolete_proposal(paths)
    return paths, True


def _obsolete_proposal(paths: ResolutionPaths) -> tuple[dict[str, Any], bytes]:
    run, run_bytes = _read_json(paths.record)
    if run["descendant_claims"]:
        raise WorkbenchError("obsolete proposal requires no descendant Claims")
    proposal = {
        "schema_version": 2, "artifact": "finding-resolution-proposal",
        "artifact_id": f"{paths.resolution_id}-obsolete-proposal",
        "lifecycle": "derived-replaceable",
        "provenance": _provenance("deterministic", str(run["provenance"]["created_at"]), "workbench-finding-resolution-v1"),
        "parents": [_parent("retest-run", "resolution-retest-run", run_bytes)],
        "resolution_id": paths.resolution_id, "original_finding_id": run["original_finding_id"],
        "descendant_claims": [], "proposed_status": "obsolete",
        "mapping_reason": "Human-confirmed Claim Lineage records removal with no descendant Claim to retest.",
        "retest_summary": {verdict: 0 for verdict in FINDING_VERDICTS},
        "field_provenance": {field: {"origin": "deterministic", "source": "confirmed removal mapping v1"} for field in ("retest_summary", "proposed_status", "mapping_reason")},
    }
    errors = validate_artifact(proposal)
    if errors: raise WorkbenchError("internal obsolete resolution proposal error: " + "; ".join(errors))
    return proposal, json_bytes(proposal)


def _write_obsolete_proposal(paths: ResolutionPaths) -> tuple[Path, bool]:
    _, data = _obsolete_proposal(paths)
    output = paths.root / "derived" / "obsolete" / "resolution-proposal.json"; output.parent.mkdir(parents=True, exist_ok=True)
    changed = not output.exists() or output.read_bytes() != data
    if changed: _atomic_write(output, data)
    return output, changed


def _classify(paths: ResolutionPaths, response_bytes: bytes) -> tuple[str, list[str], dict[str, Any] | None]:
    try: value = parse_json_strict(response_bytes)
    except WorkbenchError as exc: return "unusable", [str(exc)], None
    errors = validate_artifact(value) if isinstance(value, dict) else ["response must be an object"]
    if not isinstance(value, dict): return "unusable", errors, None
    run, _ = _read_json(paths.record); _, finding_bytes = _read_json(paths.root / "original-finding.json"); target_ir, target_ir_bytes = _read_json(paths.root / "target-argument-ir.json"); protocol_bytes = (paths.root / "lens-protocol.json").read_bytes()
    expected_source = {"original_finding_sha256": sha256_bytes(finding_bytes), "target_ir_sha256": sha256_bytes(target_ir_bytes), "lens_protocol_sha256": sha256_bytes(protocol_bytes)}
    if value.get("source") != expected_source: errors.append("source must bind the exact original Finding, descendant IR, and original Lens")
    targets = [item.get("target_claim") for item in value.get("results", []) if isinstance(item, dict)]
    if value.get("status") == "complete" and targets != run["descendant_claims"]: errors.append("complete retest must cover descendant Claims exactly once and in order")
    known = {f"{paths.to_version}:{node['id']}" for field in ("claims", "evidence", "assumptions", "citations") for node in target_ir[field]}
    run_lens = run["lens"]
    check = None
    policy = None
    if run_lens["kind"] == "rule":
        protocol = parse_json_strict(protocol_bytes)
        if isinstance(protocol, dict):
            check = next((item for item in protocol.get("checks", []) if isinstance(item, dict) and item.get("id") == run_lens["check_id"]), None)
        if check is None:
            errors.append("original Rule Lens check is missing from its protocol snapshot")
        else:
            policy = check.get("evidence_policy")
    for index, result in enumerate(value.get("results", [])):
        if isinstance(result, dict):
            unknown = (set(result.get("basis_refs", [])) | set(result.get("support_refs", []))) - known
            if unknown: errors.append(f"results[{index}].basis_refs contains unknown descendant nodes: {sorted(unknown)}")
            if run_lens["kind"] == "perspective" and (result.get("support_refs") or result.get("support_paths")):
                errors.append(f"results[{index}] Perspective Lens must use basis_refs, not Rule-Lens support fields")
            if run_lens["kind"] == "rule" and result.get("verdict") == "pass":
                local_claim = str(result.get("target_claim", "")).split(":", 1)[-1]
                eligible = _eligible_pass_support_paths(target_ir, local_claim) if local_claim else {}
                refs = list(result.get("support_refs", []))
                if policy in {"upstream-required", "citation-required"} and not refs:
                    errors.append(f"results[{index}] PASS requires independent support under policy {policy}")
                if policy == "claim-text-sufficient" and refs:
                    errors.append(f"results[{index}] claim-text-sufficient PASS must not invent upstream support")
                citation_ids = {str(item["id"]) for item in target_ir["citations"]}
                for path_index, support_path in enumerate(result.get("support_paths", [])):
                    ref = str(support_path.get("support_ref", "")); local_ref = ref.split(":", 1)[-1]
                    expected_relations = [f"{paths.to_version}:{relation_id}" for relation_id in eligible.get(local_ref, [])]
                    if ref not in refs or support_path.get("relation_ids") != expected_relations:
                        errors.append(f"results[{index}].support_paths[{path_index}] is not an exact allowed support path")
                    if policy == "citation-required" and local_ref not in citation_ids:
                        errors.append(f"results[{index}] citation-required PASS support must be a Citation")
    return ("valid" if not errors else "unusable"), errors, value


def collect_resolution_results(project_dir: Path | str, response_bytes: bytes, *, resolution_id: str | None, method: str, source_name: str, producer_label: str | None):
    paths = selected_resolution(project_dir, resolution_id)
    if method not in {"file", "terminal-paste"} or not source_name or Path(source_name).name != source_name: raise WorkbenchError("invalid resolution collection source")
    run, run_bytes = _read_json(paths.record); status, errors, _ = _classify(paths, response_bytes)
    attempts = [item for item in paths.attempts_dir.iterdir() if item.is_dir() and ATTEMPT_PATTERN.fullmatch(item.name)]
    attempt_id = f"attempt-{len(attempts) + 1:04d}"
    record = {"schema_version": 1, "artifact": "resolution-result-attempt", "artifact_id": f"{paths.resolution_id}-{attempt_id}", "lifecycle": "immutable", "provenance": _provenance("model-derived", utc_now(), producer_label or "unlabeled-model"), "parents": [_parent("retest-run", "resolution-retest-run", run_bytes)], "resolution_id": paths.resolution_id, "attempt_id": attempt_id, "collection": {"method": method, "source_name": source_name, "producer_label": producer_label}, "response": {"relative_path": "response.json", "sha256": sha256_bytes(response_bytes)}, "validation": {"status": status, "errors": errors}}
    contract_errors = validate_artifact(record)
    if contract_errors: raise WorkbenchError("internal resolution attempt error: " + "; ".join(contract_errors))
    attempt_dir = paths.attempt_dir(attempt_id); temporary = Path(tempfile.mkdtemp(prefix=f".{attempt_id}.", dir=paths.attempts_dir))
    try:
        _write_new(temporary / "response.json", response_bytes); _write_new(temporary / "record.json", json_bytes(record)); os.replace(temporary, attempt_dir)
    except Exception:
        if temporary.exists(): shutil.rmtree(temporary)
        raise
    if status == "valid": rebuild_resolution_attempt(paths, attempt_id)
    return attempt_dir, record


def _derive(paths: ResolutionPaths, attempt_id: str) -> tuple[dict[str, Any], bytes]:
    run, run_bytes = _read_json(paths.record); attempt, attempt_bytes = _read_json(paths.attempt_dir(attempt_id) / "record.json"); response_bytes = (paths.attempt_dir(attempt_id) / "response.json").read_bytes()
    status, errors, result = _classify(paths, response_bytes)
    if status != "valid" or result is None: raise WorkbenchError("cannot derive invalid resolution result: " + "; ".join(errors))
    counts = {verdict: sum(1 for item in result["results"] if item["verdict"] == verdict) for verdict in FINDING_VERDICTS}
    if result["status"] == "blocked" or counts["uncertain"]: proposed = "uncertain"; reason = "At least one descendant retest is uncertain or blocked."
    elif counts["pass"] and counts["fail"]: proposed = "partially_resolved"; reason = "Descendants have mixed PASS and FAIL results under the original Lens."
    elif counts["pass"] and not counts["fail"]: proposed = "resolved"; reason = "Every descendant passes the original Lens."
    else: proposed = "unresolved"; reason = "Every descendant still fails the original Lens."
    proposal = {"schema_version": 1, "artifact": "finding-resolution-proposal", "artifact_id": f"{paths.resolution_id}-{attempt_id}-proposal", "lifecycle": "derived-replaceable", "provenance": _provenance("deterministic", str(attempt["provenance"]["created_at"]), "workbench-finding-resolution-v1"), "parents": [_parent("retest-run", "resolution-retest-run", run_bytes), _parent("result-attempt", "resolution-result-attempt", attempt_bytes), _parent("retest-results", "resolution-retest-results", response_bytes)], "resolution_id": paths.resolution_id, "original_finding_id": run["original_finding_id"], "descendant_claims": list(run["descendant_claims"]), "proposed_status": proposed, "mapping_reason": reason, "retest_summary": counts, "field_provenance": {field: {"origin": "deterministic", "source": "resolution status mapping v1"} for field in ("retest_summary", "proposed_status", "mapping_reason")}}
    contract_errors = validate_artifact(proposal)
    if contract_errors: raise WorkbenchError("internal resolution proposal error: " + "; ".join(contract_errors))
    return proposal, json_bytes(proposal)


def rebuild_resolution_attempt(paths: ResolutionPaths, attempt_id: str) -> tuple[Path, bool]:
    proposal, data = _derive(paths, attempt_id); root = paths.derived_dir(attempt_id); root.mkdir(parents=True, exist_ok=True); output = root / "resolution-proposal.json"; changed = not output.exists() or output.read_bytes() != data
    if changed: _atomic_write(output, data)
    return output, changed


def append_resolution_decision(project_dir: Path | str, *, resolution_id: str | None, decision: str, reason: str, final_status: str | None = None) -> Path:
    paths = selected_resolution(project_dir, resolution_id)
    valid = []
    for attempt_dir in sorted(paths.attempts_dir.iterdir()):
        if attempt_dir.is_dir() and ATTEMPT_PATTERN.fullmatch(attempt_dir.name):
            attempt, _ = _read_json(attempt_dir / "record.json")
            if attempt["validation"]["status"] == "valid": valid.append(attempt_dir.name)
    run, _ = _read_json(paths.record)
    if valid:
        proposal, proposal_bytes = _derive(paths, valid[-1])
    elif not run["descendant_claims"]:
        obsolete_path, _ = _write_obsolete_proposal(paths); proposal, proposal_bytes = _read_json(obsolete_path)
    else:
        raise WorkbenchError("resolution has no valid original-Lens retest")
    existing = []
    for path in sorted(paths.decisions_dir.iterdir()):
        if not path.is_file() or DECISION_PATTERN.fullmatch(path.name) is None: raise WorkbenchError(f"unexpected resolution decision: {path.name}")
        existing.append((*_read_json(path), path))
    previous = existing[-1] if existing else None
    if decision == "confirm": final_status = str(proposal["proposed_status"])
    if decision == "reject": final_status = None
    parents = [_parent("resolution-proposal", "finding-resolution-proposal", proposal_bytes)]; supersedes = None
    if previous is not None: supersedes = sha256_bytes(previous[1]); parents.append(_parent("previous-decision", "finding-resolution-decision", previous[1]))
    decision_id = f"RD{len(existing) + 1:04d}"
    artifact = {"schema_version": 1, "artifact": "finding-resolution-decision", "artifact_id": decision_id, "lifecycle": "immutable", "provenance": _provenance("human-confirmed", utc_now(), "local-user"), "parents": parents, "decision_id": decision_id, "resolution_id": paths.resolution_id, "decision": decision, "final_status": final_status, "reason": reason, "supersedes": supersedes}
    errors = validate_artifact(artifact)
    if errors: raise WorkbenchError("invalid resolution decision: " + "; ".join(errors))
    output = paths.decisions_dir / f"{decision_id}.json"; _write_new(output, json_bytes(artifact)); return output


def render_resolution(project_dir: Path | str, resolution_id: str | None = None) -> str:
    paths = selected_resolution(project_dir, resolution_id); run, _ = _read_json(paths.record)
    lines = ["# Finding Resolution", "", f"- Resolution: `{paths.resolution_id}`", f"- Original Finding: `{run['original_finding_id']}`", f"- Original Lens: `{run['lens']['id']}` / `{run['lens'].get('check_id') or 'perspective'}`", f"- Descendants: {', '.join(run['descendant_claims'])}", ""]
    valid = [path.name for path in sorted(paths.attempts_dir.iterdir()) if path.is_dir() and (path / "record.json").is_file() and _read_json(path / "record.json")[0]["validation"]["status"] == "valid"]
    if not valid and run["descendant_claims"]: lines.append("Awaiting original-Lens retest.")
    elif not valid:
        obsolete_path, _ = _write_obsolete_proposal(paths); proposal, _ = _read_json(obsolete_path)
        lines.extend([f"- Proposed resolution: `{proposal['proposed_status']}` `[deterministic mapping]`", f"- Mapping reason: {proposal['mapping_reason']}"])
        decisions = sorted(paths.decisions_dir.glob("RD[0-9][0-9][0-9][0-9].json"))
        if decisions:
            human, _ = _read_json(decisions[-1]); lines.extend([f"- Human decision: `{human['decision']}` `[human-confirmed]`", f"- Final status: `{human['final_status']}`", f"- Human reason: {human['reason']}"])
        else: lines.append("- Human decision: pending")
    else:
        proposal, _ = _derive(paths, valid[-1]); lines.extend([f"- Proposed resolution: `{proposal['proposed_status']}` `[deterministic mapping]`", f"- Mapping reason: {proposal['mapping_reason']}", f"- Retest: {proposal['retest_summary']} `[model-derived inputs]`"])
        decisions = sorted(paths.decisions_dir.glob("RD[0-9][0-9][0-9][0-9].json"))
        if decisions:
            decision, _ = _read_json(decisions[-1]); lines.extend([f"- Human decision: `{decision['decision']}` `[human-confirmed]`", f"- Final status: `{decision['final_status']}`", f"- Human reason: {decision['reason']}"])
        else: lines.append("- Human decision: pending")
    return "\n".join(lines) + "\n"


def rebuild_resolutions(project_dir: Path | str) -> tuple[list[Path], bool]:
    outputs: list[Path] = []; changed = False
    for paths in list_resolutions(project_dir):
        run, _ = _read_json(paths.record)
        if not run["descendant_claims"]:
            output, item_changed = _write_obsolete_proposal(paths); outputs.append(output); changed = changed or item_changed
        for attempt_dir in sorted(paths.attempts_dir.iterdir()):
            if attempt_dir.is_dir() and ATTEMPT_PATTERN.fullmatch(attempt_dir.name):
                attempt, _ = _read_json(attempt_dir / "record.json")
                derived = paths.derived_dir(attempt_dir.name)
                if attempt.get("validation", {}).get("status") == "valid":
                    output, item_changed = rebuild_resolution_attempt(paths, attempt_dir.name); outputs.append(output); changed = changed or item_changed
                elif derived.exists():
                    raise WorkbenchError("invalid resolution result must not have derived artifacts")
    return outputs, changed


def verify_resolutions(project_dir: Path | str) -> list[str]:
    errors: list[str] = []
    try: items = list_resolutions(project_dir)
    except WorkbenchError as exc: return [str(exc)]
    if [item.resolution_id for item in items] != [f"RR{i}" for i in range(1, len(items) + 1)]:
        errors.append("Finding Resolution IDs must be continuous from RR1")
    for paths in items:
        prefix = paths.resolution_id
        try:
            run, run_bytes = _read_json(paths.record)
            errors.extend(f"{prefix}: {error}" for error in validate_artifact(run))
            snapshots = {
                "original_finding": paths.root / "original-finding.json",
                "accepted_adjudication": paths.root / "accepted-adjudication.json",
                "confirmed_lineage": paths.root / "confirmed-lineage.json",
                "target_ir": paths.root / "target-argument-ir.json",
                "lens_protocol": paths.root / "lens-protocol.json",
                "lens_content": paths.root / "lens-content.txt",
                "prompt": paths.root / "resolution-retest-prompt.md",
            }
            snapshot_bytes: dict[str, bytes] = {}
            for field, path in snapshots.items():
                if path.is_symlink() or not path.is_file():
                    errors.append(f"{prefix}: {field} snapshot missing or unsafe"); continue
                data = path.read_bytes(); snapshot_bytes[field] = data
                if run.get(field, {}).get("sha256") != sha256_bytes(data): errors.append(f"{prefix}: {field} hash mismatch")
            action_values: list[dict[str, Any]] = []
            for index, bound in enumerate(run.get("revision_actions", [])):
                action_path = paths.root / str(bound["relative_path"])
                if action_path.is_symlink() or not action_path.is_file(): errors.append(f"{prefix}: RevisionAction snapshot missing"); continue
                action, data = _read_json(action_path); action_values.append(action)
                if bound["sha256"] != sha256_bytes(data): errors.append(f"{prefix}: RevisionAction hash mismatch")
            finding = parse_json_strict(snapshot_bytes.get("original_finding", b"{}")); lineage = parse_json_strict(snapshot_bytes.get("confirmed_lineage", b"{}")); target_ir = parse_json_strict(snapshot_bytes.get("target_ir", b"{}"))
            if isinstance(finding, dict) and isinstance(lineage, dict) and isinstance(target_ir, dict) and "lens_content" in snapshot_bytes:
                source = {"original_finding_sha256": sha256_bytes(snapshot_bytes["original_finding"]), "target_ir_sha256": sha256_bytes(snapshot_bytes["target_ir"]), "lens_protocol_sha256": sha256_bytes(snapshot_bytes["lens_protocol"])}
                expected_prompt = _render_prompt(finding, action_values, lineage, target_ir, snapshot_bytes["lens_content"], source)
                if snapshot_bytes.get("prompt") != expected_prompt: errors.append(f"{prefix}: retest prompt is not reproducible")
            attempts = []
            proposal_by_hash: dict[str, bytes] = {}
            if not run["descendant_claims"]:
                obsolete_path = paths.root / "derived" / "obsolete" / "resolution-proposal.json"
                if obsolete_path.is_symlink() or not obsolete_path.is_file():
                    errors.append(f"{prefix}: obsolete proposal is missing")
                else:
                    obsolete, obsolete_bytes = _read_json(obsolete_path)
                    _, expected_data = _obsolete_proposal(paths)
                    if obsolete_bytes != expected_data or validate_artifact(obsolete): errors.append(f"{prefix}: obsolete proposal is invalid or not reproducible")
                    proposal_by_hash[sha256_bytes(obsolete_bytes)] = obsolete_bytes
            for attempt_dir in sorted(paths.attempts_dir.iterdir()):
                if attempt_dir.is_symlink() or not attempt_dir.is_dir() or ATTEMPT_PATTERN.fullmatch(attempt_dir.name) is None: errors.append(f"{prefix}: unexpected result entry {attempt_dir.name}"); continue
                attempts.append(attempt_dir.name); attempt, attempt_bytes = _read_json(attempt_dir / "record.json"); response_bytes = (attempt_dir / "response.json").read_bytes()
                errors.extend(f"{prefix}/{attempt_dir.name}: {error}" for error in validate_artifact(attempt))
                if attempt.get("response", {}).get("sha256") != sha256_bytes(response_bytes): errors.append(f"{prefix}/{attempt_dir.name}: response hash mismatch")
                status, validation_errors, result = _classify(paths, response_bytes)
                if attempt.get("validation") != {"status": status, "errors": validation_errors}: errors.append(f"{prefix}/{attempt_dir.name}: validation is not reproducible")
                derived = paths.derived_dir(attempt_dir.name)
                if status == "valid" and result is not None:
                    proposal, proposal_bytes = _derive(paths, attempt_dir.name); proposal_by_hash[sha256_bytes(proposal_bytes)] = proposal_bytes
                    output = derived / "resolution-proposal.json"
                    if output.is_symlink() or not output.is_file() or output.read_bytes() != proposal_bytes: errors.append(f"{prefix}/{attempt_dir.name}: resolution proposal is not reproducible")
                elif derived.exists(): errors.append(f"{prefix}/{attempt_dir.name}: invalid result has derived artifacts")
            if attempts != [f"attempt-{i:04d}" for i in range(1, len(attempts) + 1)]: errors.append(f"{prefix}: attempt IDs must be continuous")
            decisions = sorted(paths.decisions_dir.iterdir()); previous_hash = None
            for index, path in enumerate(decisions, 1):
                if path.is_symlink() or not path.is_file() or path.name != f"RD{index:04d}.json": errors.append(f"{prefix}: unexpected decision entry {path.name}"); continue
                decision, data = _read_json(path); errors.extend(f"{prefix}/{path.name}: {error}" for error in validate_artifact(decision))
                if decision.get("supersedes") != previous_hash: errors.append(f"{prefix}/{path.name}: supersedes chain is disconnected")
                proposal_parent = next((parent for parent in decision.get("parents", []) if parent.get("role") == "resolution-proposal"), None)
                if not isinstance(proposal_parent, dict) or proposal_parent.get("sha256") not in proposal_by_hash: errors.append(f"{prefix}/{path.name}: resolution proposal parent is unavailable")
                previous_hash = sha256_bytes(data)
        except (OSError, WorkbenchError, KeyError, TypeError) as exc:
            errors.append(f"{prefix}: {exc}")
    return errors
