"""Model-proposed, human-reviewable semantic Claim lineage across versions."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argument_contracts import LINEAGE_RELATIONS, sha256_bytes, validate_artifact, validate_contract_bundle
from argument_ir import validate_argument_ir
from argument_versioning import StructuralDiffPaths, build_structural_diff
from argument_workbench import (
    WorkspacePaths,
    WorkbenchError,
    _atomic_write,
    _parent,
    _provenance,
    _read_json,
    _write_new,
    correction_entries,
    document_version_chain,
    json_bytes,
    list_attempts,
    list_version_ids,
    parse_json_strict,
    utc_now,
    verify_project_versions,
    workspace_paths,
)


ANALYSIS_PATTERN = re.compile(r"LA([1-9][0-9]*)\Z")
ATTEMPT_PATTERN = re.compile(r"attempt-([0-9]{4})\Z")
PAIR_PATTERN = re.compile(r"(V[1-9][0-9]*)--(V[1-9][0-9]*)\Z")


@dataclass(frozen=True)
class LineageAnalysisPaths:
    workspace: WorkspacePaths
    from_version: str
    to_version: str
    analysis_id: str

    @property
    def lineage_id(self) -> str:
        return f"{self.from_version}--{self.to_version}"

    @property
    def lineage_dir(self) -> Path:
        return self.workspace.document_dir / "lineage" / self.lineage_id

    @property
    def analyses_dir(self) -> Path:
        return self.lineage_dir / "analyses"

    @property
    def root(self) -> Path:
        return self.analyses_dir / self.analysis_id

    @property
    def record(self) -> Path:
        return self.root / "analysis-run.json"

    @property
    def from_reviewed(self) -> Path:
        return self.root / "from-reviewed-record.json"

    @property
    def to_reviewed(self) -> Path:
        return self.root / "to-reviewed-record.json"

    @property
    def from_ir(self) -> Path:
        return self.root / "from-argument-ir.json"

    @property
    def to_ir(self) -> Path:
        return self.root / "to-argument-ir.json"

    @property
    def structural_diff(self) -> Path:
        return self.root / "structural-diff.json"

    @property
    def prompt(self) -> Path:
        return self.root / "lineage-prompt.md"

    @property
    def attempts_dir(self) -> Path:
        return self.root / "proposals"

    def attempt_dir(self, attempt_id: str) -> Path:
        return self.attempts_dir / attempt_id

    def derived_dir(self, attempt_id: str) -> Path:
        return self.root / "derived" / attempt_id

    @property
    def decisions_dir(self) -> Path:
        return self.root / "human-decisions"


def _normalize_version(value: str) -> str:
    normalized = value.strip().upper()
    if re.fullmatch(r"V[1-9][0-9]*", normalized) is None:
        raise WorkbenchError("version ID must be V1..Vn")
    return normalized


def _pair(project_dir: Path | str, from_version: str | None, to_version: str | None) -> tuple[WorkspacePaths, str, str]:
    workspace = workspace_paths(project_dir)
    versions = list_version_ids(workspace)
    if len(versions) < 2:
        raise WorkbenchError("semantic lineage requires at least two DocumentVersions")
    selected_to = _normalize_version(to_version) if to_version else versions[-1]
    if selected_to not in versions:
        raise WorkbenchError(f"unknown to-version: {selected_to}")
    index = versions.index(selected_to)
    if index == 0:
        raise WorkbenchError("V1 has no parent version")
    selected_from = _normalize_version(from_version) if from_version else versions[index - 1]
    if selected_from not in versions:
        raise WorkbenchError(f"unknown from-version: {selected_from}")
    if versions.index(selected_from) >= index:
        raise WorkbenchError("from-version must precede to-version")
    return workspace, selected_from, selected_to


def list_lineage_analyses(project_dir: Path | str, *, from_version: str | None = None, to_version: str | None = None) -> list[LineageAnalysisPaths]:
    workspace, selected_from, selected_to = _pair(project_dir, from_version, to_version)
    base = workspace.document_dir / "lineage" / f"{selected_from}--{selected_to}" / "analyses"
    if not base.exists():
        return []
    if base.is_symlink() or not base.is_dir():
        raise WorkbenchError("lineage analyses must be a regular directory")
    paths: list[LineageAnalysisPaths] = []
    for child in base.iterdir():
        if child.is_symlink() or not child.is_dir() or ANALYSIS_PATTERN.fullmatch(child.name) is None:
            raise WorkbenchError(f"unexpected lineage analysis entry: {child.name}")
        paths.append(LineageAnalysisPaths(workspace, selected_from, selected_to, child.name))
    return sorted(paths, key=lambda item: int(item.analysis_id[2:]))


def selected_lineage_analysis(project_dir: Path | str, *, from_version: str | None = None, to_version: str | None = None, analysis_id: str | None = None) -> LineageAnalysisPaths:
    analyses = list_lineage_analyses(project_dir, from_version=from_version, to_version=to_version)
    if not analyses:
        raise WorkbenchError("no lineage analysis exists; run `ir lineage prepare` first")
    if analysis_id is None:
        return analyses[-1]
    normalized = analysis_id.strip().upper()
    for paths in analyses:
        if paths.analysis_id == normalized:
            return paths
    raise WorkbenchError(f"unknown lineage analysis: {normalized}")


def _render_prompt(from_version: str, to_version: str, from_ir: dict[str, Any], to_ir: dict[str, Any], structural_diff: dict[str, Any]) -> bytes:
    example = {
        "schema_version": 1,
        "artifact": "claim-lineage-proposals",
        "source": {
            "structural_diff_sha256": "<exact hash shown below>",
            "from_ir_sha256": "<exact hash shown below>",
            "to_ir_sha256": "<exact hash shown below>",
        },
        "status": "complete",
        "unverified": [],
        "proposals": [{
            "proposal_id": "LP1",
            "from_claims": [f"{from_version}:C1"],
            "to_claims": [f"{to_version}:C1"],
            "relation": "modified",
            "semantic_changes": ["scope_narrowed"],
            "reason": "Concrete semantic comparison.",
            "basis_refs": [f"{from_version}:C1", f"{to_version}:C1"],
            "uncertainty": "",
        }],
    }
    from_bytes = json_bytes(from_ir)
    to_bytes = json_bytes(to_ir)
    diff_bytes = json_bytes(structural_diff)
    example["source"] = {
        "structural_diff_sha256": sha256_bytes(diff_bytes),
        "from_ir_sha256": sha256_bytes(from_bytes),
        "to_ir_sha256": sha256_bytes(to_bytes),
    }
    text = (
        "# Argument Workbench semantic Claim lineage proposal\n\n"
        "Compare Claims across manuscript versions. Claim IDs are local to each version and must not be assumed stable. "
        "Propose semantic correspondence only; every proposal remains model-derived until a human confirms it. "
        "Support one-to-many split, many-to-one merged, removed, new, and uncertain. Overlap is allowed when reality requires it. "
        "For status=complete, cover every Claim on both sides at least once. Return only one UTF-8 JSON object.\n\n"
        f"Allowed relations: {', '.join(LINEAGE_RELATIONS)}.\n\n"
        "Allowed semantic_changes: scope_narrowed, scope_broadened, causal_strength_reduced, "
        "causal_strength_increased, qualification_added, qualification_removed, evidence_changed, "
        "concept_reframed, argument_role_changed, wording_only, other, uncertain.\n\n"
        "## Required JSON shape\n\n```json\n" + json.dumps(example, ensure_ascii=False, indent=2) + "\n```\n\n"
        f"## {from_version} Reviewed Argument IR\n\n```json\n" + json.dumps(from_ir, ensure_ascii=False, indent=2) + "\n```\n\n"
        f"## {to_version} Reviewed Argument IR\n\n```json\n" + json.dumps(to_ir, ensure_ascii=False, indent=2) + "\n```\n\n"
        "## Deterministic structural diff\n\n```json\n" + json.dumps(structural_diff, ensure_ascii=False, indent=2) + "\n```\n"
    )
    return text.encode("utf-8")


def prepare_lineage_analysis(project_dir: Path | str, *, from_version: str | None = None, to_version: str | None = None) -> tuple[LineageAnalysisPaths, bool]:
    workspace, selected_from, selected_to = _pair(project_dir, from_version, to_version)
    errors = verify_project_versions(workspace)
    if errors:
        raise WorkbenchError("Argument Workbench project is invalid: " + "; ".join(errors))
    diff_paths, _ = build_structural_diff(workspace, from_version=selected_from, to_version=selected_to)
    from_paths = workspace_paths(workspace, selected_from)
    to_paths = workspace_paths(workspace, selected_to)
    from_reviewed, from_reviewed_bytes = _read_json(from_paths.reviewed_record)
    to_reviewed, to_reviewed_bytes = _read_json(to_paths.reviewed_record)
    from_ir, from_ir_bytes = _read_json(from_paths.reviewed_payload)
    to_ir, to_ir_bytes = _read_json(to_paths.reviewed_payload)
    structural_diff, structural_diff_bytes = _read_json(diff_paths.record)
    prompt_bytes = _render_prompt(selected_from, selected_to, from_ir, to_ir, structural_diff)
    for existing in list_lineage_analyses(workspace, from_version=selected_from, to_version=selected_to):
        record, _ = _read_json(existing.record)
        parents = {p.get("role"): p.get("sha256") for p in record.get("parents", []) if isinstance(p, dict)}
        if parents == {
            "from-reviewed": sha256_bytes(from_reviewed_bytes), "to-reviewed": sha256_bytes(to_reviewed_bytes),
            "from-ir": sha256_bytes(from_ir_bytes), "to-ir": sha256_bytes(to_ir_bytes),
            "structural-diff": sha256_bytes(structural_diff_bytes),
        } and record.get("prompt", {}).get("sha256") == sha256_bytes(prompt_bytes):
            return existing, False
    analyses = list_lineage_analyses(workspace, from_version=selected_from, to_version=selected_to)
    analysis_id = f"LA{len(analyses) + 1}"
    paths = LineageAnalysisPaths(workspace, selected_from, selected_to, analysis_id)
    created_at = utc_now()
    record = {
        "schema_version": 1, "artifact": "lineage-analysis-run", "artifact_id": analysis_id,
        "lifecycle": "immutable", "provenance": _provenance("deterministic", created_at, "workbench-semantic-lineage-v1"),
        "parents": [
            _parent("from-reviewed", "reviewed-argument-ir", from_reviewed_bytes), _parent("to-reviewed", "reviewed-argument-ir", to_reviewed_bytes),
            _parent("from-ir", "argument-ir", from_ir_bytes), _parent("to-ir", "argument-ir", to_ir_bytes),
            _parent("structural-diff", "structural-version-diff", structural_diff_bytes),
        ],
        "lineage_id": paths.lineage_id, "document_id": str(to_reviewed["document_id"]),
        "from_version": selected_from, "to_version": selected_to,
        "from_reviewed_record": {"relative_path": "from-reviewed-record.json", "sha256": sha256_bytes(from_reviewed_bytes)},
        "to_reviewed_record": {"relative_path": "to-reviewed-record.json", "sha256": sha256_bytes(to_reviewed_bytes)},
        "from_ir": {"relative_path": "from-argument-ir.json", "sha256": sha256_bytes(from_ir_bytes)},
        "to_ir": {"relative_path": "to-argument-ir.json", "sha256": sha256_bytes(to_ir_bytes)},
        "structural_diff": {"relative_path": "structural-diff.json", "sha256": sha256_bytes(structural_diff_bytes)},
        "prompt": {"relative_path": "lineage-prompt.md", "sha256": sha256_bytes(prompt_bytes)},
    }
    contract_errors = validate_artifact(record)
    if contract_errors:
        raise WorkbenchError("internal lineage analysis contract error: " + "; ".join(contract_errors))
    paths.analyses_dir.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{analysis_id}.", dir=paths.analyses_dir))
    try:
        for relative, data in (
            ("analysis-run.json", json_bytes(record)), ("from-reviewed-record.json", from_reviewed_bytes),
            ("to-reviewed-record.json", to_reviewed_bytes), ("from-argument-ir.json", from_ir_bytes),
            ("to-argument-ir.json", to_ir_bytes), ("structural-diff.json", structural_diff_bytes),
            ("lineage-prompt.md", prompt_bytes),
        ):
            _write_new(temporary / relative, data)
        (temporary / "proposals").mkdir()
        (temporary / "derived").mkdir()
        (temporary / "human-decisions").mkdir()
        os.replace(temporary, paths.root)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return paths, True


def _next_attempt(paths: LineageAnalysisPaths) -> str:
    numbers = [int(p.name.split("-")[1]) for p in paths.attempts_dir.iterdir() if p.is_dir() and ATTEMPT_PATTERN.fullmatch(p.name)]
    return f"attempt-{max(numbers, default=0) + 1:04d}"


def _classify(response_bytes: bytes, paths: LineageAnalysisPaths) -> tuple[str, list[str], dict[str, Any] | None]:
    try:
        value = parse_json_strict(response_bytes)
    except WorkbenchError as exc:
        return "unusable", [str(exc)], None
    errors = validate_artifact(value) if isinstance(value, dict) else ["response must be an object"]
    if not isinstance(value, dict):
        return "unusable", errors, None
    from_ir, from_ir_bytes = _read_json(paths.from_ir)
    to_ir, to_ir_bytes = _read_json(paths.to_ir)
    _, diff_bytes = _read_json(paths.structural_diff)
    expected_source = {"structural_diff_sha256": sha256_bytes(diff_bytes), "from_ir_sha256": sha256_bytes(from_ir_bytes), "to_ir_sha256": sha256_bytes(to_ir_bytes)}
    if value.get("source") != expected_source:
        errors.append("source must bind the exact structural diff and both IR snapshots")
    known_from = {f"{paths.from_version}:{node['id']}" for field in ("claims", "evidence", "assumptions", "citations") for node in from_ir[field]}
    known_to = {f"{paths.to_version}:{node['id']}" for field in ("claims", "evidence", "assumptions", "citations") for node in to_ir[field]}
    from_claims = {ref for ref in known_from if ":C" in ref}
    to_claims = {ref for ref in known_to if ":C" in ref}
    covered_from: set[str] = set()
    covered_to: set[str] = set()
    for index, proposal in enumerate(value.get("proposals", [])):
        if not isinstance(proposal, dict):
            continue
        proposed_from = set(proposal.get("from_claims", [])); proposed_to = set(proposal.get("to_claims", [])); basis = set(proposal.get("basis_refs", []))
        if proposed_from - from_claims:
            errors.append(f"proposals[{index}].from_claims contains unknown or wrong-version Claims: {sorted(proposed_from - from_claims)}")
        if proposed_to - to_claims:
            errors.append(f"proposals[{index}].to_claims contains unknown or wrong-version Claims: {sorted(proposed_to - to_claims)}")
        if basis - (known_from | known_to):
            errors.append(f"proposals[{index}].basis_refs contains unknown nodes: {sorted(basis - (known_from | known_to))}")
        covered_from.update(proposed_from); covered_to.update(proposed_to)
    if value.get("status") == "complete":
        if covered_from != from_claims:
            errors.append(f"complete proposals must cover every source Claim; missing={sorted(from_claims - covered_from)}")
        if covered_to != to_claims:
            errors.append(f"complete proposals must cover every descendant Claim; missing={sorted(to_claims - covered_to)}")
    return ("valid" if not errors else "unusable"), errors, value


def collect_lineage_proposals(project_dir: Path | str, response_bytes: bytes, *, from_version: str | None = None, to_version: str | None = None, analysis_id: str | None = None, method: str, source_name: str, producer_label: str | None) -> tuple[Path, dict[str, Any]]:
    if method not in {"file", "terminal-paste"}:
        raise WorkbenchError("collection method must be file or terminal-paste")
    if not source_name or Path(source_name).name != source_name:
        raise WorkbenchError("collection source_name must be a basename")
    paths = selected_lineage_analysis(project_dir, from_version=from_version, to_version=to_version, analysis_id=analysis_id)
    analysis, analysis_bytes = _read_json(paths.record)
    status, errors, _ = _classify(response_bytes, paths)
    attempt_id = _next_attempt(paths)
    attempt_dir = paths.attempt_dir(attempt_id)
    created_at = utc_now()
    record = {
        "schema_version": 1, "artifact": "lineage-proposal-attempt", "artifact_id": f"{paths.analysis_id}-{attempt_id}",
        "lifecycle": "immutable", "provenance": _provenance("model-derived", created_at, producer_label or "unlabeled-model"),
        "parents": [_parent("analysis-run", "lineage-analysis-run", analysis_bytes)],
        "lineage_id": paths.lineage_id, "attempt_id": attempt_id,
        "collection": {"method": method, "source_name": source_name, "producer_label": producer_label},
        "response": {"relative_path": "response.json", "sha256": sha256_bytes(response_bytes)},
        "validation": {"status": status, "errors": errors},
    }
    contract_errors = validate_artifact(record)
    if contract_errors:
        raise WorkbenchError("internal lineage proposal contract error: " + "; ".join(contract_errors))
    temporary = Path(tempfile.mkdtemp(prefix=f".{attempt_id}.", dir=paths.attempts_dir))
    try:
        _write_new(temporary / "response.json", response_bytes)
        _write_new(temporary / "record.json", json_bytes(record))
        os.replace(temporary, attempt_dir)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    if status == "valid":
        rebuild_lineage_attempt(paths, attempt_id)
    return attempt_dir, record


def _derive(paths: LineageAnalysisPaths, attempt_id: str) -> dict[str, bytes]:
    analysis, analysis_bytes = _read_json(paths.record)
    attempt_dir = paths.attempt_dir(attempt_id)
    attempt, attempt_bytes = _read_json(attempt_dir / "record.json")
    response_bytes = (attempt_dir / "response.json").read_bytes()
    status, errors, result = _classify(response_bytes, paths)
    if status != "valid" or result is None:
        raise WorkbenchError(f"cannot derive lineage from invalid {attempt_id}: " + "; ".join(errors))
    _, from_ir_bytes = _read_json(paths.from_ir); _, to_ir_bytes = _read_json(paths.to_ir)
    files: dict[str, bytes] = {}
    proposals: list[dict[str, Any]] = []
    lineage_parents: list[tuple[str, bytes]] = []
    for index, proposal in enumerate(result["proposals"], 1):
        artifact_id = f"{paths.lineage_id}-{paths.analysis_id}-{attempt_id}-L{index:04d}"
        parents = [_parent("proposal-attempt", "lineage-proposal-attempt", attempt_bytes), _parent("proposal-result", "claim-lineage-proposals", response_bytes)]
        if proposal["from_claims"]:
            parents.insert(0, _parent("from-version", "argument-ir", from_ir_bytes))
        if proposal["to_claims"]:
            parents.insert(1 if proposal["from_claims"] else 0, _parent("to-version", "argument-ir", to_ir_bytes))
        lineage = {
            "schema_version": 2, "artifact": "claim-lineage", "artifact_id": artifact_id,
            "lifecycle": "immutable", "provenance": dict(attempt["provenance"]), "parents": parents,
            "lineage_id": paths.lineage_id, "from_claims": list(proposal["from_claims"]), "to_claims": list(proposal["to_claims"]),
            "relation": proposal["relation"], "proposed_by": "model", "proposal_sha256": None, "status": "proposed",
            "semantic_changes": list(proposal["semantic_changes"]), "reason": proposal["reason"], "basis_refs": list(proposal["basis_refs"]), "uncertainty": proposal["uncertainty"],
        }
        lineage_errors = validate_artifact(lineage)
        if lineage_errors:
            raise WorkbenchError("internal Claim Lineage contract error: " + "; ".join(lineage_errors))
        lineage_bytes = json_bytes(lineage)
        files[f"lineages/L{index:04d}.json"] = lineage_bytes
        lineage_parents.append((f"lineage-{index:04d}", lineage_bytes))
        proposals.append({"proposal_id": proposal["proposal_id"], "lineage_artifact_id": artifact_id, **{key: proposal[key] for key in ("from_claims", "to_claims", "relation", "semantic_changes", "reason", "basis_refs", "uncertainty")}})
    summary = {relation: sum(1 for p in proposals if p["relation"] == relation) for relation in LINEAGE_RELATIONS}; summary["total"] = len(proposals)
    markdown_lines = ["# Semantic Claim Lineage Proposals", "", f"- Pair: `{paths.lineage_id}`", f"- Analysis: `{paths.analysis_id}`", f"- Attempt: `{attempt_id}`", "- Status: model proposal; human confirmation required", "", "No Claim ID is assumed stable across versions.", ""]
    for proposal in proposals:
        left = ", ".join(proposal["from_claims"]) or "new"
        right = ", ".join(proposal["to_claims"]) or "removed"
        markdown_lines.extend([f"## {proposal['proposal_id']} — {proposal['relation']}", "", f"`{left}` → `{right}`", "", f"- Semantic changes: {', '.join(proposal['semantic_changes']) or 'none'} `[model-derived]`", f"- Reason: {proposal['reason']} `[model-derived]`", f"- Basis: {', '.join(proposal['basis_refs'])}", f"- Uncertainty: {proposal['uncertainty'] or 'none stated'}", ""])
    markdown_bytes = ("\n".join(markdown_lines) + "\n").encode("utf-8")
    parents = [_parent("analysis-run", "lineage-analysis-run", analysis_bytes), _parent("proposal-attempt", "lineage-proposal-attempt", attempt_bytes), _parent("proposal-result", "claim-lineage-proposals", response_bytes)] + [_parent(role, "claim-lineage", data) for role, data in lineage_parents]
    index = {
        "schema_version": 1, "artifact": "claim-lineage-index", "artifact_id": f"{paths.analysis_id}-{attempt_id}-index",
        "lifecycle": "derived-replaceable", "provenance": _provenance("deterministic", str(attempt["provenance"]["created_at"]), "workbench-semantic-lineage-v1"), "parents": parents,
        "lineage_id": paths.lineage_id, "attempt_id": attempt_id, "from_version": paths.from_version, "to_version": paths.to_version,
        "run_status": result["status"], "unverified": list(result["unverified"]), "summary": summary, "proposals": proposals,
        "payload": {"relative_path": "claim-lineage.md", "sha256": sha256_bytes(markdown_bytes)},
        "field_provenance": {
            "proposals": {"origin": "model-derived", "source": "proposal-result"}, "run_status": {"origin": "model-derived", "source": "proposal-result"},
            "unverified": {"origin": "model-derived", "source": "proposal-result"}, "lineage_artifact_id": {"origin": "deterministic", "source": "workbench-semantic-lineage-v1"},
            "summary": {"origin": "deterministic", "source": "workbench-semantic-lineage-v1"}, "payload": {"origin": "deterministic", "source": "workbench-semantic-lineage-v1"},
        },
    }
    index_errors = validate_artifact(index)
    if index_errors:
        raise WorkbenchError("internal Claim Lineage index error: " + "; ".join(index_errors))
    files["claim-lineage.md"] = markdown_bytes; files["claim-lineage-index.json"] = json_bytes(index)
    return files


def _write_derived(root: Path, files: dict[str, bytes]) -> bool:
    if root.is_symlink():
        raise WorkbenchError(f"derived directory must not be a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True); (root / "lineages").mkdir(exist_ok=True)
    allowed = {Path(name) for name in files}; changed = False
    for existing in root.rglob("*"):
        if existing.is_symlink():
            raise WorkbenchError(f"derived artifact must not be a symlink: {existing}")
        if existing.is_file() and existing.relative_to(root) not in allowed:
            if existing.parent == root / "lineages" and re.fullmatch(r"L[0-9]{4}\.json", existing.name):
                existing.unlink(); changed = True
            else:
                raise WorkbenchError(f"unexpected derived lineage artifact: {existing.relative_to(root)}")
    for relative, data in files.items():
        path = root / relative
        if not path.exists() or path.read_bytes() != data:
            _atomic_write(path, data); changed = True
    return changed


def rebuild_lineage_attempt(paths: LineageAnalysisPaths, attempt_id: str) -> tuple[Path, bool]:
    if ATTEMPT_PATTERN.fullmatch(attempt_id) is None:
        raise WorkbenchError("attempt ID must be attempt-NNNN")
    return paths.derived_dir(attempt_id) / "claim-lineage.md", _write_derived(paths.derived_dir(attempt_id), _derive(paths, attempt_id))


def rebuild_lineage_analyses(project_dir: Path | str) -> tuple[list[Path], bool]:
    workspace = workspace_paths(project_dir); lineage_root = workspace.document_dir / "lineage"
    if not lineage_root.exists(): return [], False
    outputs: list[Path] = []; changed = False
    for pair in sorted(lineage_root.iterdir()):
        match = PAIR_PATTERN.fullmatch(pair.name)
        if pair.is_symlink() or not pair.is_dir() or match is None: raise WorkbenchError(f"unexpected lineage entry: {pair.name}")
        for paths in list_lineage_analyses(workspace, from_version=match.group(1), to_version=match.group(2)):
            for attempt_dir in sorted(paths.attempts_dir.iterdir()):
                if not attempt_dir.is_dir() or ATTEMPT_PATTERN.fullmatch(attempt_dir.name) is None: raise WorkbenchError(f"unexpected lineage proposal entry: {attempt_dir.name}")
                attempt, _ = _read_json(attempt_dir / "record.json")
                if attempt.get("validation", {}).get("status") == "valid":
                    output, item_changed = rebuild_lineage_attempt(paths, attempt_dir.name); outputs.append(output); changed = changed or item_changed
                elif paths.derived_dir(attempt_dir.name).exists(): raise WorkbenchError("invalid lineage proposal must not have derived artifacts")
    return outputs, changed


def show_lineage(project_dir: Path | str, *, from_version: str | None = None, to_version: str | None = None, analysis_id: str | None = None) -> tuple[str, Path]:
    paths = selected_lineage_analysis(project_dir, from_version=from_version, to_version=to_version, analysis_id=analysis_id)
    valid: list[str] = []
    for attempt_dir in sorted(paths.attempts_dir.iterdir()):
        if attempt_dir.is_dir() and ATTEMPT_PATTERN.fullmatch(attempt_dir.name):
            attempt, _ = _read_json(attempt_dir / "record.json")
            if attempt.get("validation", {}).get("status") == "valid": valid.append(attempt_dir.name)
    if not valid: raise WorkbenchError("lineage analysis has no valid model proposal")
    view, _ = rebuild_lineage_attempt(paths, valid[-1]); return view.read_text(encoding="utf-8"), view


def _selected_proposal_set(
    paths: LineageAnalysisPaths,
) -> tuple[str, dict[str, Any], list[tuple[dict[str, Any], bytes]]]:
    valid: list[str] = []
    for attempt_dir in sorted(paths.attempts_dir.iterdir()):
        if attempt_dir.is_dir() and ATTEMPT_PATTERN.fullmatch(attempt_dir.name):
            attempt, _ = _read_json(attempt_dir / "record.json")
            if attempt.get("validation", {}).get("status") == "valid":
                valid.append(attempt_dir.name)
    if not valid:
        raise WorkbenchError("lineage analysis has no valid model proposal")
    attempt_id = valid[-1]
    index, _ = _read_json(paths.derived_dir(attempt_id) / "claim-lineage-index.json")
    lineages: list[tuple[dict[str, Any], bytes]] = []
    for number in range(1, len(index["proposals"]) + 1):
        lineages.append(
            _read_json(paths.derived_dir(attempt_id) / "lineages" / f"L{number:04d}.json")
        )
    return attempt_id, index, lineages


def lineage_proposal_ids(
    project_dir: Path | str,
    *, from_version: str | None = None, to_version: str | None = None, analysis_id: str | None = None,
) -> list[str]:
    paths = selected_lineage_analysis(project_dir, from_version=from_version, to_version=to_version, analysis_id=analysis_id)
    _, index, _ = _selected_proposal_set(paths)
    return [str(item["proposal_id"]) for item in index["proposals"]]


def list_lineage_decisions(
    paths: LineageAnalysisPaths,
) -> list[tuple[Path, dict[str, Any], bytes]]:
    if not paths.decisions_dir.exists():
        return []
    if paths.decisions_dir.is_symlink() or not paths.decisions_dir.is_dir():
        raise WorkbenchError("human-decisions must be a regular directory")
    results: list[tuple[Path, dict[str, Any], bytes]] = []
    for path in sorted(paths.decisions_dir.iterdir()):
        if path.is_symlink() or not path.is_file() or re.fullmatch(r"LD[0-9]{4}\.json", path.name) is None:
            raise WorkbenchError(f"unexpected lineage decision entry: {path.name}")
        value, data = _read_json(path)
        results.append((path, value, data))
    if [path.name for path, _, _ in results] != [f"LD{i:04d}.json" for i in range(1, len(results) + 1)]:
        raise WorkbenchError("lineage decision IDs must be continuous")
    return results


def _current_decisions(
    paths: LineageAnalysisPaths,
) -> dict[str, tuple[dict[str, Any], bytes]]:
    current: dict[str, tuple[dict[str, Any], bytes]] = {}
    for _, decision, data in list_lineage_decisions(paths):
        proposal_hash = str(decision["proposal_sha256"])
        previous = current.get(proposal_hash)
        expected = sha256_bytes(previous[1]) if previous is not None else None
        if decision.get("supersedes_sha256") != expected:
            raise WorkbenchError(
                f"decision {decision.get('artifact_id')} does not supersede the current decision"
            )
        current[proposal_hash] = (decision, data)
    return current


def append_lineage_decision(
    project_dir: Path | str,
    *,
    proposal_ids: list[str],
    decision: str,
    human_note: str,
    from_version: str | None = None,
    to_version: str | None = None,
    analysis_id: str | None = None,
    correction: dict[str, Any] | None = None,
) -> list[Path]:
    if decision not in {"confirm", "reject", "correct"}:
        raise WorkbenchError("decision must be confirm, reject, or correct")
    if not human_note.strip():
        raise WorkbenchError("human decision requires a reason")
    paths = selected_lineage_analysis(project_dir, from_version=from_version, to_version=to_version, analysis_id=analysis_id)
    _, index, model_lineages = _selected_proposal_set(paths)
    by_id = {
        str(summary["proposal_id"]): model_lineages[position]
        for position, summary in enumerate(index["proposals"])
    }
    normalized = [value.strip().upper() for value in proposal_ids]
    if not normalized or len(normalized) != len(set(normalized)):
        raise WorkbenchError("proposal IDs must be a non-empty unique list")
    unknown = [value for value in normalized if value not in by_id]
    if unknown:
        raise WorkbenchError(f"unknown lineage proposals: {unknown}")
    if decision == "correct" and (len(normalized) != 1 or correction is None):
        raise WorkbenchError("correct requires exactly one proposal and corrected fields")
    if decision != "correct" and correction is not None:
        raise WorkbenchError("corrected fields are only valid with decision=correct")
    current = _current_decisions(paths)
    _, from_ir_bytes = _read_json(paths.from_ir)
    _, to_ir_bytes = _read_json(paths.to_ir)
    paths.decisions_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    next_number = len(list_lineage_decisions(paths)) + 1
    for proposal_id in normalized:
        proposal, proposal_bytes = by_id[proposal_id]
        fields = {
            key: proposal[key]
            for key in ("from_claims", "to_claims", "relation", "semantic_changes", "reason", "basis_refs", "uncertainty")
        }
        if correction is not None:
            fields.update(correction)
        previous = current.get(sha256_bytes(proposal_bytes))
        parents = []
        if fields["from_claims"]:
            parents.append(_parent("from-version", "argument-ir", from_ir_bytes))
        if fields["to_claims"]:
            parents.append(_parent("to-version", "argument-ir", to_ir_bytes))
        parents.append(_parent("proposal", "claim-lineage", proposal_bytes))
        supersedes_sha256 = None
        if previous is not None:
            supersedes_sha256 = sha256_bytes(previous[1])
            parents.append(_parent("supersedes", "claim-lineage", previous[1]))
        artifact_id = f"{paths.lineage_id}-{paths.analysis_id}-LD{next_number:04d}"
        artifact = {
            "schema_version": 3, "artifact": "claim-lineage", "artifact_id": artifact_id,
            "lifecycle": "immutable", "provenance": _provenance("human-confirmed", utc_now(), "local-user"),
            "parents": parents, "lineage_id": paths.lineage_id,
            **fields, "proposed_by": "model", "proposal_sha256": sha256_bytes(proposal_bytes),
            "status": "rejected" if decision == "reject" else "human_confirmed",
            "review_action": decision, "human_note": human_note.strip(), "supersedes_sha256": supersedes_sha256,
        }
        errors = validate_artifact(artifact)
        if errors:
            raise WorkbenchError("invalid human lineage decision: " + "; ".join(errors))
        output = paths.decisions_dir / f"LD{next_number:04d}.json"
        _write_new(output, json_bytes(artifact)); outputs.append(output)
        current[sha256_bytes(proposal_bytes)] = (artifact, json_bytes(artifact))
        next_number += 1
    return outputs


def render_lineage_history(
    project_dir: Path | str,
    *, from_version: str | None = None, to_version: str | None = None, analysis_id: str | None = None,
) -> str:
    paths = selected_lineage_analysis(project_dir, from_version=from_version, to_version=to_version, analysis_id=analysis_id)
    _, index, model_lineages = _selected_proposal_set(paths)
    current = _current_decisions(paths)
    lines = ["# Claim Lineage Human Review", "", f"- Pair: `{paths.lineage_id}`", f"- Analysis: `{paths.analysis_id}`", ""]
    for summary, (proposal, proposal_bytes) in zip(index["proposals"], model_lineages):
        state = current.get(sha256_bytes(proposal_bytes))
        status = "awaiting human judgment" if state is None else f"{state[0]['status']} ({state[0]['review_action']})"
        left = ", ".join(proposal["from_claims"]) or "new"; right = ", ".join(proposal["to_claims"]) or "removed"
        lines.extend([f"## {summary['proposal_id']} — {status}", "", f"`{left}` → `{right}` · `{proposal['relation']}` `[model-derived]`", f"- Model reason: {proposal['reason']}"])
        if state is not None:
            decision = state[0]
            if decision["review_action"] == "correct":
                final_left = ", ".join(decision["from_claims"]) or "new"; final_right = ", ".join(decision["to_claims"]) or "removed"
                lines.append(f"- Human correction: `{final_left}` → `{final_right}` · `{decision['relation']}`")
            lines.append(f"- Human reason: {decision['human_note']} `[human-confirmed]`")
        lines.append("")
    return "\n".join(lines)


def verify_lineage_analyses(project_dir: Path | str) -> list[str]:
    workspace = workspace_paths(project_dir); lineage_root = workspace.document_dir / "lineage"
    if not lineage_root.exists(): return []
    errors: list[str] = []
    try:
        pairs = sorted(lineage_root.iterdir())
    except OSError as exc: return [str(exc)]
    for pair in pairs:
        match = PAIR_PATTERN.fullmatch(pair.name)
        if pair.is_symlink() or not pair.is_dir() or match is None: errors.append(f"unexpected lineage entry: {pair.name}"); continue
        try: analyses = list_lineage_analyses(workspace, from_version=match.group(1), to_version=match.group(2))
        except WorkbenchError as exc: errors.append(f"{pair.name}: {exc}"); continue
        if [p.analysis_id for p in analyses] != [f"LA{i}" for i in range(1, len(analyses)+1)]: errors.append(f"{pair.name}: analysis IDs must be continuous")
        for paths in analyses:
            prefix = f"{pair.name}/{paths.analysis_id}"
            try:
                record, record_bytes = _read_json(paths.record); from_reviewed, from_reviewed_bytes = _read_json(paths.from_reviewed); to_reviewed, to_reviewed_bytes = _read_json(paths.to_reviewed)
                from_ir, from_ir_bytes = _read_json(paths.from_ir); to_ir, to_ir_bytes = _read_json(paths.to_ir); diff, diff_bytes = _read_json(paths.structural_diff); prompt_bytes = paths.prompt.read_bytes()
                project, project_bytes = _read_json(workspace.project)
                document, document_bytes = _read_json(workspace.document)
                entries: list[tuple[object, bytes]] = [
                    (project, project_bytes), (document, document_bytes),
                    *document_version_chain(workspace_paths(workspace, paths.to_version)),
                ]
                for version_id in (paths.from_version, paths.to_version):
                    version_paths = workspace_paths(workspace, version_id)
                    entries.extend((value, data) for _, value, data in list_attempts(version_paths))
                    entries.extend((value, data) for _, value, data in correction_entries(version_paths))
                entries.extend([(from_reviewed, from_reviewed_bytes), (to_reviewed, to_reviewed_bytes), (diff, diff_bytes), (record, record_bytes)])
                contract_errors = validate_artifact(record)
                errors.extend(f"{prefix}: {e}" for e in contract_errors)
                for field, data in (("from_reviewed_record", from_reviewed_bytes), ("to_reviewed_record", to_reviewed_bytes), ("from_ir", from_ir_bytes), ("to_ir", to_ir_bytes), ("structural_diff", diff_bytes), ("prompt", prompt_bytes)):
                    if record.get(field, {}).get("sha256") != sha256_bytes(data): errors.append(f"{prefix}: {field} hash mismatch")
                if validate_argument_ir(from_ir) or validate_argument_ir(to_ir): errors.append(f"{prefix}: IR snapshot is invalid")
                expected_prompt = _render_prompt(
                    paths.from_version, paths.to_version, from_ir, to_ir, diff
                )
                if prompt_bytes != expected_prompt:
                    errors.append(f"{prefix}: lineage prompt is not reproducible")
                for attempt_dir in sorted(paths.attempts_dir.iterdir()):
                    if attempt_dir.is_symlink() or not attempt_dir.is_dir() or ATTEMPT_PATTERN.fullmatch(attempt_dir.name) is None: errors.append(f"{prefix}: unexpected proposal entry {attempt_dir.name}"); continue
                    attempt, attempt_bytes = _read_json(attempt_dir / "record.json"); response_bytes = (attempt_dir / "response.json").read_bytes(); entries.append((attempt, attempt_bytes))
                    if attempt.get("response", {}).get("sha256") != sha256_bytes(response_bytes): errors.append(f"{prefix}/{attempt_dir.name}: response hash mismatch")
                    status, validation_errors, result = _classify(response_bytes, paths)
                    if attempt.get("validation") != {"status": status, "errors": validation_errors}: errors.append(f"{prefix}/{attempt_dir.name}: validation is not reproducible")
                    if status == "valid" and result is not None:
                        entries.append((result, response_bytes)); expected = _derive(paths, attempt_dir.name); derived = paths.derived_dir(attempt_dir.name)
                        if not derived.is_dir(): errors.append(f"{prefix}/{attempt_dir.name}: derived artifacts missing")
                        else:
                            actual = {str(p.relative_to(derived)).replace("\\", "/"): p.read_bytes() for p in derived.rglob("*") if p.is_file() and not p.is_symlink()}
                            if actual != expected: errors.append(f"{prefix}/{attempt_dir.name}: derived artifacts are not reproducible")
                            for relative, data in expected.items():
                                if relative.startswith("lineages/") or relative.endswith("index.json"):
                                    value = parse_json_strict(data); entries.append((value, data))
                    elif paths.derived_dir(attempt_dir.name).exists(): errors.append(f"{prefix}/{attempt_dir.name}: invalid proposal has derived artifacts")
                try:
                    _current_decisions(paths)
                    for decision_path, decision, decision_bytes in list_lineage_decisions(paths):
                        entries.append((decision, decision_bytes))
                        errors.extend(
                            f"{prefix}/{decision_path.name}: {error}"
                            for error in validate_artifact(decision)
                        )
                except WorkbenchError as exc:
                    errors.append(f"{prefix}: {exc}")
                errors.extend(f"{prefix}: {e}" for e in validate_contract_bundle(entries))
            except (OSError, WorkbenchError) as exc: errors.append(f"{prefix}: {exc}")
    return errors
