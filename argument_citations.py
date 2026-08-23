"""Substantive Citation -> Evidence -> Claim provenance for Argument Workbench."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argument_contracts import (
    CITATION_BIBLIOGRAPHIC_STATUSES,
    CITATION_CONTENT_SUPPORT_STATUSES,
    CITATION_CONTEXT_STATUSES,
    CITATION_SOURCE_LOCATION_STATUSES,
    sha256_bytes,
    validate_artifact,
)
from argument_workbench import (
    WorkspacePaths,
    WorkbenchError,
    _atomic_write,
    _parent,
    _provenance,
    _read_json,
    _write_new,
    json_bytes,
    list_version_ids,
    parse_json_strict,
    utc_now,
    verify_project_versions,
    workspace_paths,
)


AUDIT_PATTERN = re.compile(r"CA([1-9][0-9]*)\Z")
ATTEMPT_PATTERN = re.compile(r"attempt-([0-9]{4})\Z")
DECISION_PATTERN = re.compile(r"CD([0-9]{4})\.json\Z")
DIMENSION_KEYS = (
    "bibliographic_existence",
    "exact_source_located",
    "content_support",
    "context_preserved",
    "uncertainty",
)


@dataclass(frozen=True)
class CitationAuditPaths:
    workspace: WorkspacePaths
    audit_id: str

    @property
    def root_dir(self) -> Path:
        return self.workspace.version_dir / "citation-audits"

    @property
    def root(self) -> Path:
        return self.root_dir / self.audit_id

    @property
    def record(self) -> Path:
        return self.root / "audit-run.json"

    @property
    def reviewed_ir(self) -> Path:
        return self.root / "reviewed-argument-ir.json"

    @property
    def context(self) -> Path:
        return self.root / "citation-audit-context.json"

    @property
    def prompt(self) -> Path:
        return self.root / "citation-audit-prompt.md"

    @property
    def attempts_dir(self) -> Path:
        return self.root / "results"

    @property
    def decisions_dir(self) -> Path:
        return self.root / "human-decisions"

    @property
    def derived_dir(self) -> Path:
        return self.root / "derived"

    @property
    def index(self) -> Path:
        return self.derived_dir / "citation-provenance-index.json"

    @property
    def report(self) -> Path:
        return self.derived_dir / "evidence-provenance.md"

    def attempt_dir(self, attempt_id: str) -> Path:
        return self.attempts_dir / attempt_id


def _current_version(project_dir: Path | str, version_id: str | None) -> WorkspacePaths:
    root = workspace_paths(project_dir)
    if version_id is not None:
        return workspace_paths(root, version_id)
    versions = list_version_ids(root)
    if not versions:
        raise WorkbenchError("project has no DocumentVersion")
    return workspace_paths(root, versions[-1])


def list_citation_audits(
    project_dir: Path | str,
    *,
    version_id: str | None = None,
    all_versions: bool = False,
) -> list[CitationAuditPaths]:
    root = workspace_paths(project_dir)
    versions = list_version_ids(root) if all_versions else [_current_version(root, version_id).version_id]
    found: list[CitationAuditPaths] = []
    for current_version in versions:
        workspace = workspace_paths(root, current_version)
        base = workspace.version_dir / "citation-audits"
        if not base.exists():
            continue
        if base.is_symlink() or not base.is_dir():
            raise WorkbenchError(f"{current_version} citation-audits must be a regular directory")
        for child in base.iterdir():
            if child.is_symlink() or not child.is_dir() or AUDIT_PATTERN.fullmatch(child.name) is None:
                raise WorkbenchError(f"unexpected {current_version} citation-audits entry: {child.name}")
            found.append(CitationAuditPaths(workspace, child.name))
    return sorted(
        found,
        key=lambda item: (int(item.workspace.version_id[1:]), int(item.audit_id[2:])),
    )


def selected_citation_audit(
    project_dir: Path | str,
    audit_id: str | None,
    *,
    version_id: str | None = None,
) -> CitationAuditPaths:
    workspace = _current_version(project_dir, version_id)
    audits = list_citation_audits(workspace.root, version_id=workspace.version_id)
    if not audits:
        raise WorkbenchError("no Citation Audit exists; run `ir citations prepare`")
    if audit_id is None:
        return audits[-1]
    normalized = audit_id.strip().upper()
    for audit in audits:
        if audit.audit_id == normalized:
            return audit
    raise WorkbenchError(f"unknown Citation Audit in {workspace.version_id}: {normalized}")


def _source_path(workspace: WorkspacePaths) -> tuple[dict[str, Any], bytes]:
    version, _ = _read_json(workspace.version)
    relative = version.get("source", {}).get("relative_path")
    if not isinstance(relative, str):
        raise WorkbenchError("DocumentVersion source path is invalid")
    source_path = workspace.version_dir / Path(relative)
    if source_path.is_symlink() or not source_path.is_file():
        raise WorkbenchError("DocumentVersion source must be a regular file")
    source_bytes = source_path.read_bytes()
    if version["source"].get("sha256") != sha256_bytes(source_bytes):
        raise WorkbenchError("DocumentVersion source hash mismatch")
    return version, source_bytes


def _result_source(context_bytes: bytes, reviewed_ir_bytes: bytes, source_bytes: bytes) -> dict[str, str]:
    return {
        "audit_context_sha256": sha256_bytes(context_bytes),
        "reviewed_ir_sha256": sha256_bytes(reviewed_ir_bytes),
        "source_sha256": sha256_bytes(source_bytes),
    }


def _render_prompt(
    *,
    selected_citations: list[str],
    reviewed_ir: dict[str, Any],
    result_source: dict[str, str],
) -> bytes:
    citations = {
        str(item["id"]): item
        for item in reviewed_ir.get("citations", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    selected = [citations[citation_id] for citation_id in selected_citations]
    relation_context = [
        relation
        for relation in reviewed_ir.get("relations", [])
        if isinstance(relation, dict)
        and (
            relation.get("from") in set(selected_citations)
            or relation.get("type") in {"supports", "qualifies"}
        )
    ]
    shape = {
        "schema_version": 1,
        "artifact": "citation-audit-results",
        "source": result_source,
        "status": "partial",
        "sources": [
            {
                "source_id": "S1",
                "kind": "primary",
                "title": "Exact source title",
                "locator": "stable URL, DOI, catalog, or repository locator",
                "accessed_at": "2026-08-23T00:00:00+00:00",
                "dimensions": [
                    "bibliographic_existence",
                    "exact_source_located",
                    "content_support",
                    "context_preserved",
                ],
                "note": "What was inspected and how it bears on the dimensions.",
            }
        ],
        "outcomes": [
            {
                "citation_id": citation_id,
                "bibliographic_existence": "uncertain",
                "exact_source_located": "uncertain",
                "content_support": "uncertain",
                "context_preserved": "uncertain",
                "reason": "Evidence-based assessment of this exact manuscript citation.",
                "source_refs": [],
                "uncertainty": "State what remains unknown.",
            }
            for citation_id in selected_citations
        ],
        "unverified": ["List unresolved search or access limits."],
    }
    text = (
        "# Substantive Citation Verification\n\n"
        "Audit only the selected Argument IR Citations below. This is not a manuscript-quality review. "
        "Do not infer bibliographic existence or content support from model memory. Use inspectable external sources and preserve their exact locators. "
        "Keep four epistemic questions separate: bibliographic existence, exact-source location, support for the manuscript wording, and preservation of context. "
        "A primary or repository source is required for exact-source and content judgments. If the exact source was not located, content_support and context_preserved must remain uncertain. "
        "Return pure JSON only, in selected Citation order. A negative or uncertain citation outcome marks dependent Evidence/Claims as unverified; it never establishes claim_false.\n\n"
        "## Selected Citations\n\n```json\n"
        + json.dumps(selected, ensure_ascii=False, indent=2)
        + "\n```\n\n## Citation / Evidence / Claim relations\n\n```json\n"
        + json.dumps(relation_context, ensure_ascii=False, indent=2)
        + "\n```\n\n## Required JSON shape\n\n```json\n"
        + json.dumps(shape, ensure_ascii=False, indent=2)
        + "\n```\n\n## Reviewed Argument IR snapshot\n\n```json\n"
        + json.dumps(reviewed_ir, ensure_ascii=False, indent=2)
        + "\n```\n"
    )
    return text.encode("utf-8")


def prepare_citation_audit(
    project_dir: Path | str,
    *,
    citation_ids: list[str] | None = None,
    version_id: str | None = None,
) -> tuple[CitationAuditPaths, bool]:
    root = workspace_paths(project_dir)
    errors = verify_project_versions(root)
    if errors:
        raise WorkbenchError("Argument Workbench project is invalid: " + "; ".join(errors))
    workspace = _current_version(root, version_id)
    document, _ = _read_json(root.document)
    version, version_bytes = _read_json(workspace.version)
    reviewed_ir, reviewed_ir_bytes = _read_json(workspace.reviewed_payload)
    _, reviewed_record_bytes = _read_json(workspace.reviewed_record)
    _, source_bytes = _source_path(workspace)
    known = [
        str(item["id"])
        for item in reviewed_ir.get("citations", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    if not known:
        raise WorkbenchError(f"{workspace.version_id} Reviewed IR contains no Citations")
    requested = list(citation_ids or known)
    if len(requested) != len(set(requested)):
        raise WorkbenchError("Citation selection must not contain duplicates")
    unknown = sorted(set(requested) - set(known))
    if unknown:
        raise WorkbenchError(f"unknown Citation IDs in {workspace.version_id}: {unknown}")
    selected = [citation_id for citation_id in known if citation_id in set(requested)]

    existing = list_citation_audits(root, version_id=workspace.version_id)
    for paths in existing:
        run, _ = _read_json(paths.record)
        if (
            run.get("selected_citations") == selected
            and run.get("reviewed_ir", {}).get("sha256") == sha256_bytes(reviewed_ir_bytes)
        ):
            return paths, False

    audit_id = f"CA{len(existing) + 1}"
    paths = CitationAuditPaths(workspace, audit_id)
    created_at = utc_now()
    parents = [
        _parent("document-version", "document-version", version_bytes),
        _parent("reviewed-record", "reviewed-argument-ir", reviewed_record_bytes),
        _parent("reviewed-ir", "argument-ir", reviewed_ir_bytes),
    ]
    context = {
        "schema_version": 1,
        "artifact": "citation-audit-context",
        "version_id": workspace.version_id,
        "selected_citations": selected,
        "reviewed_ir_sha256": sha256_bytes(reviewed_ir_bytes),
        "source_sha256": str(version["source"]["sha256"]),
    }
    context_bytes = json_bytes(context)
    run = {
        "schema_version": 1,
        "artifact": "citation-audit-run",
        "artifact_id": audit_id,
        "lifecycle": "immutable",
        "provenance": _provenance(
            "deterministic", created_at, "workbench-citation-verification-v1"
        ),
        "parents": parents,
        "audit_id": audit_id,
        "document_id": str(document["document_id"]),
        "version_id": workspace.version_id,
        "selected_citations": selected,
        "context": {
            "relative_path": "citation-audit-context.json",
            "sha256": sha256_bytes(context_bytes),
        },
        "reviewed_ir": {
            "relative_path": "reviewed-argument-ir.json",
            "sha256": sha256_bytes(reviewed_ir_bytes),
        },
        "prompt": {
            "relative_path": "citation-audit-prompt.md",
            "sha256": "0" * 64,
        },
    }
    result_source = _result_source(context_bytes, reviewed_ir_bytes, source_bytes)
    prompt_bytes = _render_prompt(
        selected_citations=selected,
        reviewed_ir=reviewed_ir,
        result_source=result_source,
    )
    run["prompt"]["sha256"] = sha256_bytes(prompt_bytes)
    contract_errors = validate_artifact(run)
    if contract_errors:
        raise WorkbenchError("internal Citation Audit run error: " + "; ".join(contract_errors))

    paths.root_dir.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{audit_id}.", dir=paths.root_dir))
    try:
        _write_new(temporary / "audit-run.json", json_bytes(run))
        _write_new(temporary / "citation-audit-context.json", context_bytes)
        _write_new(temporary / "reviewed-argument-ir.json", reviewed_ir_bytes)
        _write_new(temporary / "citation-audit-prompt.md", prompt_bytes)
        (temporary / "results").mkdir()
        (temporary / "human-decisions").mkdir()
        (temporary / "derived").mkdir()
        os.replace(temporary, paths.root)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return paths, True


def _expected_result_source(paths: CitationAuditPaths) -> dict[str, str]:
    context_bytes = paths.context.read_bytes()
    reviewed_ir_bytes = paths.reviewed_ir.read_bytes()
    _, source_bytes = _source_path(paths.workspace)
    return _result_source(context_bytes, reviewed_ir_bytes, source_bytes)


def _classify(
    paths: CitationAuditPaths,
    response_bytes: bytes,
) -> tuple[str, list[str], dict[str, Any] | None]:
    try:
        value = parse_json_strict(response_bytes)
    except WorkbenchError as exc:
        return "unusable", [str(exc)], None
    if not isinstance(value, dict):
        return "unusable", ["response must be a JSON object"], None
    errors = validate_artifact(value)
    run, _ = _read_json(paths.record)
    if value.get("source") != _expected_result_source(paths):
        errors.append("source must bind the exact Citation Audit run, Reviewed IR, and manuscript")
    actual = [
        item.get("citation_id")
        for item in value.get("outcomes", [])
        if isinstance(item, dict)
    ]
    if actual != run.get("selected_citations"):
        errors.append("outcomes must cover selected Citations exactly once and in order")
    return ("valid" if not errors else "unusable"), errors, value


def _attempt_entries(paths: CitationAuditPaths) -> list[tuple[str, dict[str, Any], bytes, bytes]]:
    entries: list[tuple[str, dict[str, Any], bytes, bytes]] = []
    if not paths.attempts_dir.exists():
        return entries
    for child in sorted(paths.attempts_dir.iterdir()):
        if child.is_symlink() or not child.is_dir() or ATTEMPT_PATTERN.fullmatch(child.name) is None:
            raise WorkbenchError(f"unexpected Citation Audit result entry: {child.name}")
        record, record_bytes = _read_json(child / "record.json")
        response = child / "response.json"
        if response.is_symlink() or not response.is_file():
            raise WorkbenchError(f"Citation Audit response is missing or unsafe: {child.name}")
        entries.append((child.name, record, record_bytes, response.read_bytes()))
    return entries


def _valid_attempts(paths: CitationAuditPaths) -> list[tuple[str, dict[str, Any], bytes, bytes]]:
    return [
        entry
        for entry in _attempt_entries(paths)
        if entry[1].get("validation", {}).get("status") == "valid"
    ]


def collect_citation_results(
    project_dir: Path | str,
    response_bytes: bytes,
    *,
    audit_id: str | None,
    version_id: str | None,
    method: str,
    source_name: str,
    producer_label: str | None,
) -> tuple[Path, dict[str, Any]]:
    paths = selected_citation_audit(
        project_dir, audit_id, version_id=version_id
    )
    if method not in {"file", "terminal-paste"}:
        raise WorkbenchError("citation collection method must be file or terminal-paste")
    if not source_name or Path(source_name).name != source_name:
        raise WorkbenchError("citation result source_name must be a safe basename")
    run, run_bytes = _read_json(paths.record)
    status, errors, _ = _classify(paths, response_bytes)
    existing = _attempt_entries(paths)
    attempt_id = f"attempt-{len(existing) + 1:04d}"
    record = {
        "schema_version": 1,
        "artifact": "citation-result-attempt",
        "artifact_id": f"{paths.audit_id}-{attempt_id}",
        "lifecycle": "immutable",
        "provenance": _provenance(
            "model-derived", utc_now(), producer_label or "unlabeled-model"
        ),
        "parents": [_parent("citation-audit-run", "citation-audit-run", run_bytes)],
        "audit_id": paths.audit_id,
        "attempt_id": attempt_id,
        "collection": {
            "method": method,
            "source_name": source_name,
            "producer_label": producer_label,
        },
        "response": {
            "relative_path": "response.json",
            "sha256": sha256_bytes(response_bytes),
        },
        "validation": {"status": status, "errors": errors},
    }
    contract_errors = validate_artifact(record)
    if contract_errors:
        raise WorkbenchError("internal Citation Audit attempt error: " + "; ".join(contract_errors))
    temporary = Path(tempfile.mkdtemp(prefix=f".{attempt_id}.", dir=paths.attempts_dir))
    attempt_dir = paths.attempt_dir(attempt_id)
    try:
        _write_new(temporary / "response.json", response_bytes)
        _write_new(temporary / "record.json", json_bytes(record))
        os.replace(temporary, attempt_dir)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    if status == "valid":
        rebuild_citation_audit(paths)
    return attempt_dir, record


def _decision_entries(paths: CitationAuditPaths) -> list[tuple[Path, dict[str, Any], bytes]]:
    entries: list[tuple[Path, dict[str, Any], bytes]] = []
    if not paths.decisions_dir.exists():
        return entries
    for child in sorted(paths.decisions_dir.iterdir()):
        if child.is_symlink() or not child.is_file() or DECISION_PATTERN.fullmatch(child.name) is None:
            raise WorkbenchError(f"unexpected Citation Audit decision entry: {child.name}")
        value, data = _read_json(child)
        entries.append((child, value, data))
    return entries


def _outcome_dimensions(outcome: dict[str, Any]) -> dict[str, str]:
    return {key: str(outcome[key]) for key in DIMENSION_KEYS}


def append_citation_decision(
    project_dir: Path | str,
    *,
    audit_id: str | None,
    version_id: str | None,
    citation_id: str,
    decision: str,
    reason: str,
    final_outcome: dict[str, str] | None = None,
    producer: str = "local-user",
) -> Path:
    paths = selected_citation_audit(project_dir, audit_id, version_id=version_id)
    valid = _valid_attempts(paths)
    if not valid:
        raise WorkbenchError("Citation Audit has no valid model result")
    attempt_id, attempt, attempt_bytes, response_bytes = valid[-1]
    response = parse_json_strict(response_bytes)
    if not isinstance(response, dict):
        raise WorkbenchError("valid Citation Audit response is not an object")
    outcome = next(
        (
            item
            for item in response["outcomes"]
            if isinstance(item, dict) and item.get("citation_id") == citation_id
        ),
        None,
    )
    if outcome is None:
        raise WorkbenchError(f"Citation {citation_id} is not in the selected audit result")
    if decision not in {"confirm", "reject", "correct"}:
        raise WorkbenchError("citation decision must be confirm, reject, or correct")
    if decision == "correct" and final_outcome is None:
        raise WorkbenchError("correct citation decision requires all four final dimensions")
    if decision != "correct" and final_outcome is not None:
        raise WorkbenchError("final dimensions are allowed only with decision=correct")
    if not reason.strip():
        raise WorkbenchError("citation decision requires a human reason")

    run, run_bytes = _read_json(paths.record)
    entries = _decision_entries(paths)
    previous_for_citation = next(
        (
            entry
            for entry in reversed(entries)
            if entry[1].get("citation_id") == citation_id
        ),
        None,
    )
    parents = [
        _parent("citation-audit-run", "citation-audit-run", run_bytes),
        _parent("result-attempt", "citation-result-attempt", attempt_bytes),
        _parent("audit-results", "citation-audit-results", response_bytes),
    ]
    supersedes = None
    if previous_for_citation is not None:
        supersedes = sha256_bytes(previous_for_citation[2])
        parents.append(
            _parent(
                "previous-decision",
                "citation-verification-decision",
                previous_for_citation[2],
            )
        )
    decision_id = f"CD{len(entries) + 1:04d}"
    artifact = {
        "schema_version": 1,
        "artifact": "citation-verification-decision",
        "artifact_id": decision_id,
        "lifecycle": "immutable",
        "provenance": _provenance("human-confirmed", utc_now(), producer),
        "parents": parents,
        "decision_id": decision_id,
        "audit_id": str(run["audit_id"]),
        "citation_id": citation_id,
        "attempt_id": attempt_id,
        "decision": decision,
        "reason": reason,
        "final_outcome": final_outcome,
        "supersedes": supersedes,
    }
    errors = validate_artifact(artifact)
    if errors:
        raise WorkbenchError("invalid Citation verification decision: " + "; ".join(errors))
    output = paths.decisions_dir / f"{decision_id}.json"
    _write_new(output, json_bytes(artifact))
    rebuild_citation_audit(paths)
    return output


def _fully_verified(outcome: dict[str, Any]) -> bool:
    return (
        outcome.get("bibliographic_existence") == "verified"
        and outcome.get("exact_source_located") == "verified"
        and outcome.get("content_support") == "supports"
        and outcome.get("context_preserved") == "yes"
    )


def _dependency_maps(
    ir: dict[str, Any],
    citation_states: dict[str, bool],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected = set(citation_states)
    adjacency: dict[str, list[str]] = {}
    citation_targets: dict[str, list[str]] = {citation_id: [] for citation_id in selected}
    for relation in ir.get("relations", []):
        if not isinstance(relation, dict):
            continue
        source = str(relation.get("from", ""))
        target = str(relation.get("to", ""))
        relation_type = relation.get("type")
        if relation_type == "cites" and source in selected:
            citation_targets[source].append(target)
        if relation_type in {"supports", "qualifies"}:
            adjacency.setdefault(source, []).append(target)

    evidence_citations: dict[str, set[str]] = {}
    claim_citations: dict[str, set[str]] = {}
    claim_evidence: dict[str, set[str]] = {}
    for citation_id, targets in citation_targets.items():
        for target in targets:
            evidence_seed = target if re.fullmatch(r"E[1-9][0-9]*", target) else None
            if evidence_seed is not None:
                evidence_citations.setdefault(evidence_seed, set()).add(citation_id)
            queue = [target]
            visited: set[str] = set()
            while queue:
                node = queue.pop(0)
                if node in visited:
                    continue
                visited.add(node)
                if re.fullmatch(r"C[1-9][0-9]*", node):
                    claim_citations.setdefault(node, set()).add(citation_id)
                    if evidence_seed is not None:
                        claim_evidence.setdefault(node, set()).add(evidence_seed)
                queue.extend(adjacency.get(node, []))

    def dependency_status(citation_ids: set[str]) -> str:
        return (
            "citation_verified"
            if citation_ids and all(citation_states.get(item, False) for item in citation_ids)
            else "depends_on_unverified_evidence"
        )

    evidence = [
        {
            "node_id": node_id,
            "citation_ids": sorted(citation_ids, key=lambda value: int(value[1:])),
            "status": dependency_status(citation_ids),
        }
        for node_id, citation_ids in sorted(
            evidence_citations.items(), key=lambda item: int(item[0][1:])
        )
    ]
    claims = [
        {
            "node_id": node_id,
            "citation_ids": sorted(citation_ids, key=lambda value: int(value[1:])),
            "evidence_ids": sorted(claim_evidence.get(node_id, set()), key=lambda value: int(value[1:])),
            "status": dependency_status(citation_ids),
        }
        for node_id, citation_ids in sorted(
            claim_citations.items(), key=lambda item: int(item[0][1:])
        )
    ]
    return evidence, claims


def _render_report(index: dict[str, Any], sources: list[dict[str, Any]]) -> bytes:
    symbols = {
        "verified": "✓",
        "supports": "✓",
        "yes": "✓",
        "contradicted": "✕",
        "does_not_support": "✕",
        "no": "✕",
        "not_verified": "?",
        "partially_supports": "?",
        "uncertain": "?",
    }
    lines = [
        "# Evidence and Citation Provenance",
        "",
        f"- Version: `{index['version_id']}`",
        f"- Citation Audit: `{index['audit_id']}`",
        f"- Model result: `{index['selected_attempt_id']}` `[model-derived]`",
        "- Dependency paths: `[deterministic]`",
        "- Final verification choices: `[human-confirmed when present]`",
        "",
        "An unverified citation marks dependent Evidence and Claims as `depends_on_unverified_evidence`. It does **not** establish `claim_false`.",
        "",
        "## Citations",
        "",
    ]
    for citation in index["citations"]:
        outcome = citation["final_outcome"] or citation["proposal"]
        human = citation["human_decision"] or "pending"
        lines.extend(
            [
                f"### {citation['citation_id']} · {citation['citation_text']}",
                "",
                f"- Bibliographic existence: {symbols.get(outcome['bibliographic_existence'], '?')} `{outcome['bibliographic_existence']}`",
                f"- Exact source located: {symbols.get(outcome['exact_source_located'], '?')} `{outcome['exact_source_located']}`",
                f"- Content supports wording: {symbols.get(outcome['content_support'], '?')} `{outcome['content_support']}`",
                f"- Context preserved: {symbols.get(outcome['context_preserved'], '?')} `{outcome['context_preserved']}`",
                f"- Human decision: `{human}`",
                f"- Verification state: `{citation['verification_state']}`",
                f"- Source refs: {', '.join(citation['source_refs']) or '—'}",
                f"- Uncertainty: {outcome['uncertainty'] or '—'}",
                "",
            ]
        )
    lines.extend(["## Sources", ""])
    if not sources:
        lines.extend(["—", ""])
    for source in sources:
        lines.extend(
            [
                f"- `{source['source_id']}` [{source['title']}]({source['locator']}) · `{source['kind']}` · dimensions: {', '.join(source['dimensions'])}",
                f"  - {source['note']}",
            ]
        )
    lines.extend(["", "## Evidence dependencies", ""])
    if not index["evidence_dependencies"]:
        lines.extend(["—", ""])
    for dependency in index["evidence_dependencies"]:
        lines.append(
            f"- `{dependency['node_id']}` ← {', '.join(dependency['citation_ids'])} · `{dependency['status']}`"
        )
    lines.extend(["", "## Claim dependencies", ""])
    if not index["claim_dependencies"]:
        lines.extend(["—", ""])
    for dependency in index["claim_dependencies"]:
        via = f" via {', '.join(dependency['evidence_ids'])}" if dependency["evidence_ids"] else ""
        lines.append(
            f"- `{dependency['node_id']}` ← {', '.join(dependency['citation_ids'])}{via} · `{dependency['status']}`"
        )
    lines.extend(
        [
            "",
            "## Summary",
            "",
            f"- Verified citations: {index['summary']['verified']}",
            f"- Unverified citations: {index['summary']['unverified']}",
            f"- Claims depending on unverified evidence: {index['summary']['claims_depending_on_unverified_evidence']}",
            "",
            "No manuscript quality score is computed.",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def _derive_index(paths: CitationAuditPaths) -> tuple[dict[str, Any], bytes, bytes]:
    valid = _valid_attempts(paths)
    if not valid:
        raise WorkbenchError("Citation Audit has no valid result to derive")
    attempt_id, attempt, attempt_bytes, response_bytes = valid[-1]
    response = parse_json_strict(response_bytes)
    if not isinstance(response, dict):
        raise WorkbenchError("valid Citation Audit response is not an object")
    run, run_bytes = _read_json(paths.record)
    reviewed_ir, _ = _read_json(paths.reviewed_ir)
    decision_entries = _decision_entries(paths)
    latest: dict[str, dict[str, Any]] = {}
    for _, decision, _ in decision_entries:
        if decision.get("attempt_id") == attempt_id:
            latest[str(decision["citation_id"])] = decision
    citation_by_id = {
        str(item["id"]): item
        for item in reviewed_ir.get("citations", [])
        if isinstance(item, dict)
    }
    citation_rows: list[dict[str, Any]] = []
    citation_states: dict[str, bool] = {}
    for outcome in response["outcomes"]:
        citation_id = str(outcome["citation_id"])
        decision = latest.get(citation_id)
        final: dict[str, str] | None = None
        current_status = "model_proposed"
        human_decision = None
        if decision is not None:
            human_decision = str(decision["decision"])
            if human_decision == "confirm":
                final = _outcome_dimensions(outcome)
                current_status = "human_confirmed"
            elif human_decision == "correct":
                final = dict(decision["final_outcome"])
                current_status = "human_confirmed"
            else:
                current_status = "proposal_rejected"
        verified = current_status == "human_confirmed" and final is not None and _fully_verified(final)
        citation_states[citation_id] = verified
        citation_rows.append(
            {
                "citation_id": citation_id,
                "citation_text": str(citation_by_id[citation_id]["text"]),
                "proposal": _outcome_dimensions(outcome),
                "human_decision": human_decision,
                "final_outcome": final,
                "current_status": current_status,
                "verification_state": "verified" if verified else "unverified",
                "source_refs": list(outcome["source_refs"]),
            }
        )
    evidence_dependencies, claim_dependencies = _dependency_maps(
        reviewed_ir, citation_states
    )
    parents = [
        _parent("citation-audit-run", "citation-audit-run", run_bytes),
        _parent("result-attempt", "citation-result-attempt", attempt_bytes),
        _parent("audit-results", "citation-audit-results", response_bytes),
    ]
    for index, (_, _, data) in enumerate(decision_entries, 1):
        parents.append(
            _parent(
                f"decision-{index:04d}",
                "citation-verification-decision",
                data,
            )
        )
    summary = {
        "citations_total": len(citation_rows),
        "verified": sum(row["verification_state"] == "verified" for row in citation_rows),
        "unverified": sum(row["verification_state"] == "unverified" for row in citation_rows),
        "human_confirmed": sum(row["current_status"] == "human_confirmed" for row in citation_rows),
        "human_pending": sum(row["current_status"] == "model_proposed" for row in citation_rows),
        "proposal_rejected": sum(row["current_status"] == "proposal_rejected" for row in citation_rows),
        "evidence_depending_on_unverified_citations": sum(
            item["status"] == "depends_on_unverified_evidence"
            for item in evidence_dependencies
        ),
        "claims_depending_on_unverified_evidence": sum(
            item["status"] == "depends_on_unverified_evidence"
            for item in claim_dependencies
        ),
    }
    index = {
        "schema_version": 1,
        "artifact": "citation-provenance-index",
        "artifact_id": f"{paths.audit_id}-index",
        "lifecycle": "derived-replaceable",
        "provenance": _provenance(
            "deterministic",
            str(attempt["provenance"]["created_at"]),
            "workbench-citation-verification-v1",
        ),
        "parents": parents,
        "audit_id": paths.audit_id,
        "version_id": paths.workspace.version_id,
        "selected_attempt_id": attempt_id,
        "citations": citation_rows,
        "evidence_dependencies": evidence_dependencies,
        "claim_dependencies": claim_dependencies,
        "summary": summary,
        "report": {
            "relative_path": "evidence-provenance.md",
            "sha256": "0" * 64,
        },
        "field_provenance": {
            "model_outcomes": {
                "origin": "model-derived",
                "source": "citation-audit-results",
            },
            "human_decisions": {
                "origin": "human-confirmed",
                "source": "citation-verification-decision",
            },
            "dependency_graph": {
                "origin": "deterministic",
                "source": "Reviewed Argument IR cites/supports paths",
            },
            "verification_state": {
                "origin": "deterministic",
                "source": "human decision plus four-dimension policy",
            },
        },
    }
    report_bytes = _render_report(index, list(response["sources"]))
    index["report"]["sha256"] = sha256_bytes(report_bytes)
    errors = validate_artifact(index)
    if errors:
        raise WorkbenchError("internal Citation provenance index error: " + "; ".join(errors))
    return index, json_bytes(index), report_bytes


def rebuild_citation_audit(paths: CitationAuditPaths) -> tuple[list[Path], bool]:
    if not _valid_attempts(paths):
        return [], False
    _, index_bytes, report_bytes = _derive_index(paths)
    changed = (
        not paths.index.exists()
        or paths.index.read_bytes() != index_bytes
        or not paths.report.exists()
        or paths.report.read_bytes() != report_bytes
    )
    if changed:
        _atomic_write(paths.index, index_bytes)
        _atomic_write(paths.report, report_bytes)
    return [paths.index, paths.report], changed


def rebuild_citation_audits(project_dir: Path | str) -> tuple[list[Path], bool]:
    outputs: list[Path] = []
    changed = False
    for paths in list_citation_audits(project_dir, all_versions=True):
        item_outputs, item_changed = rebuild_citation_audit(paths)
        outputs.extend(item_outputs)
        changed = changed or item_changed
    return outputs, changed


def render_citation_audit(
    project_dir: Path | str,
    *,
    audit_id: str | None = None,
    version_id: str | None = None,
) -> str:
    paths = selected_citation_audit(project_dir, audit_id, version_id=version_id)
    valid = _valid_attempts(paths)
    if not valid:
        run, _ = _read_json(paths.record)
        return (
            "# Evidence and Citation Provenance\n\n"
            f"- Version: `{paths.workspace.version_id}`\n"
            f"- Citation Audit: `{paths.audit_id}`\n"
            f"- Selected Citations: {', '.join(run['selected_citations'])}\n"
            "- Status: awaiting a valid model result\n"
        )
    _, _, report = _derive_index(paths)
    return report.decode("utf-8")


def verify_citation_audits(project_dir: Path | str) -> list[str]:
    errors: list[str] = []
    try:
        audits = list_citation_audits(project_dir, all_versions=True)
    except WorkbenchError as exc:
        return [str(exc)]
    by_version: dict[str, list[CitationAuditPaths]] = {}
    for paths in audits:
        by_version.setdefault(paths.workspace.version_id, []).append(paths)
    for version_id, items in by_version.items():
        expected = [f"CA{index}" for index in range(1, len(items) + 1)]
        if [item.audit_id for item in items] != expected:
            errors.append(f"{version_id}: Citation Audit IDs must be continuous from CA1")
    for paths in audits:
        prefix = f"{paths.workspace.version_id}/{paths.audit_id}"
        try:
            run, run_bytes = _read_json(paths.record)
            errors.extend(f"{prefix}: {error}" for error in validate_artifact(run))
            if run.get("version_id") != paths.workspace.version_id or run.get("audit_id") != paths.audit_id:
                errors.append(f"{prefix}: path identity does not match Citation Audit run")
            reviewed_ir, reviewed_ir_bytes = _read_json(paths.reviewed_ir)
            if run.get("reviewed_ir", {}).get("sha256") != sha256_bytes(reviewed_ir_bytes):
                errors.append(f"{prefix}: Reviewed IR snapshot hash mismatch")
            if paths.context.is_symlink() or not paths.context.is_file():
                errors.append(f"{prefix}: Citation Audit context is missing or unsafe")
                context_bytes = b""
            else:
                context_bytes = paths.context.read_bytes()
            if run.get("context", {}).get("sha256") != sha256_bytes(context_bytes):
                errors.append(f"{prefix}: Citation Audit context hash mismatch")
            _, source_bytes = _source_path(paths.workspace)
            expected_context = json_bytes(
                {
                    "schema_version": 1,
                    "artifact": "citation-audit-context",
                    "version_id": paths.workspace.version_id,
                    "selected_citations": list(run["selected_citations"]),
                    "reviewed_ir_sha256": sha256_bytes(reviewed_ir_bytes),
                    "source_sha256": sha256_bytes(source_bytes),
                }
            )
            if context_bytes != expected_context:
                errors.append(f"{prefix}: Citation Audit context is not reproducible")
            if paths.prompt.is_symlink() or not paths.prompt.is_file():
                errors.append(f"{prefix}: Citation Audit prompt is missing or unsafe")
                prompt_bytes = b""
            else:
                prompt_bytes = paths.prompt.read_bytes()
            if run.get("prompt", {}).get("sha256") != sha256_bytes(prompt_bytes):
                errors.append(f"{prefix}: Citation Audit prompt hash mismatch")
            expected_prompt = _render_prompt(
                selected_citations=list(run["selected_citations"]),
                reviewed_ir=reviewed_ir,
                result_source=_expected_result_source(paths),
            )
            if prompt_bytes != expected_prompt:
                errors.append(f"{prefix}: Citation Audit prompt is not reproducible")

            attempts = _attempt_entries(paths)
            if [entry[0] for entry in attempts] != [
                f"attempt-{index:04d}" for index in range(1, len(attempts) + 1)
            ]:
                errors.append(f"{prefix}: result attempt IDs must be continuous")
            attempt_by_id = {entry[0]: entry for entry in attempts}
            valid_attempts: list[tuple[str, dict[str, Any], bytes, bytes]] = []
            for attempt_id, attempt, attempt_bytes, response_bytes in attempts:
                item_prefix = f"{prefix}/{attempt_id}"
                errors.extend(f"{item_prefix}: {error}" for error in validate_artifact(attempt))
                if attempt.get("response", {}).get("sha256") != sha256_bytes(response_bytes):
                    errors.append(f"{item_prefix}: response hash mismatch")
                status, validation_errors, _ = _classify(paths, response_bytes)
                if attempt.get("validation") != {"status": status, "errors": validation_errors}:
                    errors.append(f"{item_prefix}: validation is not reproducible")
                if status == "valid":
                    valid_attempts.append((attempt_id, attempt, attempt_bytes, response_bytes))

            previous_by_citation: dict[str, str] = {}
            decisions = _decision_entries(paths)
            if [entry[0].name for entry in decisions] != [
                f"CD{index:04d}.json" for index in range(1, len(decisions) + 1)
            ]:
                errors.append(f"{prefix}: decision IDs must be continuous")
            for path, decision, data in decisions:
                item_prefix = f"{prefix}/{path.name}"
                errors.extend(f"{item_prefix}: {error}" for error in validate_artifact(decision))
                citation_id = str(decision.get("citation_id"))
                if decision.get("supersedes") != previous_by_citation.get(citation_id):
                    errors.append(f"{item_prefix}: supersedes chain is disconnected")
                attempt_entry = attempt_by_id.get(str(decision.get("attempt_id")))
                if attempt_entry is None or attempt_entry[1].get("validation", {}).get("status") != "valid":
                    errors.append(f"{item_prefix}: decision does not bind a valid result attempt")
                else:
                    _, _, bound_attempt_bytes, bound_response_bytes = attempt_entry
                    parents = {parent["role"]: parent for parent in decision.get("parents", [])}
                    if parents.get("citation-audit-run", {}).get("sha256") != sha256_bytes(run_bytes):
                        errors.append(f"{item_prefix}: run parent hash mismatch")
                    if parents.get("result-attempt", {}).get("sha256") != sha256_bytes(bound_attempt_bytes):
                        errors.append(f"{item_prefix}: result-attempt parent hash mismatch")
                    if parents.get("audit-results", {}).get("sha256") != sha256_bytes(bound_response_bytes):
                        errors.append(f"{item_prefix}: audit-results parent hash mismatch")
                    parsed = parse_json_strict(bound_response_bytes)
                    if isinstance(parsed, dict) and citation_id not in {
                        item.get("citation_id")
                        for item in parsed.get("outcomes", [])
                        if isinstance(item, dict)
                    }:
                        errors.append(f"{item_prefix}: Citation is absent from bound model result")
                previous_by_citation[citation_id] = sha256_bytes(data)

            if valid_attempts:
                _, expected_index, expected_report = _derive_index(paths)
                if paths.index.is_symlink() or not paths.index.is_file() or paths.index.read_bytes() != expected_index:
                    errors.append(f"{prefix}: citation provenance index is not reproducible")
                if paths.report.is_symlink() or not paths.report.is_file() or paths.report.read_bytes() != expected_report:
                    errors.append(f"{prefix}: evidence provenance report is not reproducible")
            elif paths.index.exists() or paths.report.exists():
                errors.append(f"{prefix}: invalid-only audit must not have derived provenance")
        except (OSError, WorkbenchError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"{prefix}: {exc}")
    return errors
