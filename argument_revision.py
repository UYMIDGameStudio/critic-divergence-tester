"""Constrained, provider-neutral revision workflow for Argument Workbench.

Every model response is retained.  Only a fully validated proposal can reach
human hunk decisions, and only final human decisions can create a new immutable
DocumentVersion.  The module deliberately has no HTTP or browser dependency.
"""

from __future__ import annotations

import json
import re
import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from argument_contracts import sha256_bytes
from argument_workbench import (
    WorkbenchError,
    _atomic_write,
    _read_json,
    _write_new,
    import_document_version,
    json_bytes,
    list_version_ids,
    parse_json_strict,
    utc_now,
    workspace_paths,
)


ATOMIZATION_SCHEMA_VERSION = 1
REVISION_SCHEMA_VERSION = 1
RESOLUTION_SCHEMA_VERSION = 1
FINDING_DECISIONS = {"accept", "reject", "defer"}
HUNK_DECISIONS = {"accept", "reject", "edit", "regenerate"}
RESOLUTION_STATUSES = {
    "resolved",
    "partially_resolved",
    "unresolved",
    "not_evaluated",
}
LOCATION_KINDS = {"exact_quote", "missing_implementation", "unlocated"}
EVIDENCE_LEVELS = {"verified", "unverified", "uncertain"}
CHANGE_KINDS = {
    "replace",
    "insert_before",
    "insert_after",
    "delete",
    "restructure",
}
ID = re.compile(r"[A-Z][A-Z0-9]*[1-9][0-9]*\Z")


def _source(project_dir: Path | str, version_id: str | None = None) -> tuple[Any, dict[str, Any], bytes, str]:
    workspace = workspace_paths(project_dir, version_id)
    version, _ = _read_json(workspace.version)
    relative = version.get("source", {}).get("relative_path")
    if not isinstance(relative, str):
        raise WorkbenchError("DocumentVersion source path is invalid")
    path = workspace.version_dir / relative
    if path.is_symlink() or not path.is_file():
        raise WorkbenchError("DocumentVersion source must be a regular file")
    data = path.read_bytes()
    if sha256_bytes(data) != version.get("source", {}).get("sha256"):
        raise WorkbenchError("DocumentVersion source hash does not match")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorkbenchError(f"DocumentVersion source is not UTF-8: {exc}") from exc
    return workspace, version, data, text


def _next_id(directory: Path, prefix: str, suffix: str = ".json") -> str:
    maximum = 0
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise WorkbenchError(f"artifact collection must be a regular directory: {directory}")
        pattern = re.compile(re.escape(prefix) + r"([0-9]+)" + re.escape(suffix) + r"\Z")
        for item in directory.iterdir():
            match = pattern.fullmatch(item.name)
            if match:
                maximum = max(maximum, int(match.group(1)))
    return f"{prefix}{maximum + 1:04d}"


def _provenance(origin: str, producer: str) -> dict[str, str]:
    return {"origin": origin, "created_at": utc_now(), "producer": producer}


def _quick_root(project_dir: Path | str, version_id: str | None = None) -> Path:
    return workspace_paths(project_dir, version_id).version_dir / "quick-revision"


def _workflow_workspace(project_dir: Path | str):
    """Return the newest version that owns a quick-revision workflow."""
    root = workspace_paths(project_dir).root
    for version_id in reversed(list_version_ids(root)):
        workspace = workspace_paths(root, version_id)
        if (workspace.version_dir / "quick-revision").is_dir():
            return workspace
    return workspace_paths(root)


def _regular_files(directory: Path, pattern: str) -> list[Path]:
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise WorkbenchError(f"artifact collection must be a regular directory: {directory}")
    rows = sorted(directory.glob(pattern))
    if any(item.is_symlink() or not item.is_file() for item in rows):
        raise WorkbenchError(f"artifact collection contains a non-regular file: {directory}")
    return rows


def _integrity_marker(root: Path) -> Path:
    return root / "documents" / "D1" / "revision-integrity.json"


def _integrity_receipts_exist(root: Path) -> bool:
    """Treat any receipt directory as evidence that integrity tracking was enabled."""
    return any(path.is_dir() or path.is_symlink() for path in root.rglob(".integrity"))


def _ensure_integrity_marker(root: Path) -> dict[str, Any]:
    marker_path = _integrity_marker(root)
    if marker_path.is_file() and not marker_path.is_symlink():
        return _read_json(marker_path)[0]
    if marker_path.exists() or marker_path.is_symlink():
        raise WorkbenchError("integrity policy must be a regular file")
    if _integrity_receipts_exist(root):
        raise WorkbenchError("integrity policy missing; existing receipts require explicit repair or migration")
    marker = {
        "artifact_type": "revision-integrity-policy",
        "schema_version": 1,
        "enabled_at": utc_now(),
        "producer": "argument-revision",
        "lifecycle": "immutable",
    }
    _write_new(marker_path, json_bytes(marker))
    return marker


def _write_tracked(root: Path, path: Path, data: bytes) -> None:
    """Write an append-only artifact plus an independently checked digest receipt."""
    marker = _ensure_integrity_marker(root)
    _write_new(path, data)
    receipt = {
        "artifact_type": "revision-artifact-integrity",
        "schema_version": 1,
        "artifact_relative_path": str(path.relative_to(root)).replace("\\", "/"),
        "artifact_sha256": sha256_bytes(data),
        "policy_enabled_at": marker["enabled_at"],
        "provenance": _provenance("deterministic", "argument-revision"),
        "lifecycle": "immutable",
    }
    _write_new(path.parent / ".integrity" / path.name, json_bytes(receipt))


def _verify_tracked(root: Path, path: Path, value: dict[str, Any], errors: list[str]) -> None:
    """Verify receipts for artifacts created after integrity tracking was enabled."""
    marker_path = _integrity_marker(root)
    if not marker_path.is_file() or marker_path.is_symlink():
        return
    marker, _ = _read_json(marker_path)
    enabled_at = marker.get("enabled_at")
    created_at = value.get("provenance", {}).get("created_at") if isinstance(value.get("provenance"), dict) else None
    receipt_path = path.parent / ".integrity" / path.name
    receipt_required = isinstance(enabled_at, str) and isinstance(created_at, str) and created_at >= enabled_at
    if not receipt_path.is_file() or receipt_path.is_symlink():
        if receipt_required:
            errors.append(f"{path.name}: integrity receipt missing")
        return
    try:
        receipt, _ = _read_json(receipt_path)
        expected_fields = {"artifact_type", "schema_version", "artifact_relative_path", "artifact_sha256", "policy_enabled_at", "provenance", "lifecycle"}
        if set(receipt) != expected_fields or receipt.get("artifact_type") != "revision-artifact-integrity" or receipt.get("schema_version") != 1 or receipt.get("lifecycle") != "immutable":
            errors.append(f"{path.name}: invalid integrity receipt fields")
        expected_path = str(path.relative_to(root)).replace("\\", "/")
        if receipt.get("artifact_relative_path") != expected_path:
            errors.append(f"{path.name}: integrity receipt path mismatch")
        if receipt.get("artifact_sha256") != sha256_bytes(path.read_bytes()):
            errors.append(f"{path.name}: integrity receipt hash mismatch")
        if receipt.get("policy_enabled_at") != enabled_at:
            errors.append(f"{path.name}: integrity policy binding mismatch")
    except (OSError, WorkbenchError, KeyError, TypeError) as exc:
        errors.append(f"{path.name}: invalid integrity receipt: {exc}")


@dataclass(frozen=True)
class AttemptResult:
    attempt_id: str
    valid: bool
    errors: tuple[str, ...]
    response: Path
    repair_prompt: Path | None


def import_review_report(
    project_dir: Path | str,
    report: str | bytes,
    *,
    source_name: str = "pasted-report.md",
    producer: str = "local-workbench-ui",
) -> str:
    workspace, version, _, _ = _source(project_dir)
    data = report.encode("utf-8") if isinstance(report, str) else report
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorkbenchError(f"review report is not UTF-8: {exc}") from exc
    if not text.strip():
        raise WorkbenchError("review report must not be empty")
    if Path(source_name).name != source_name or "/" in source_name or "\\" in source_name:
        raise WorkbenchError("review report source name must be a safe basename")
    reports = _quick_root(workspace.root, workspace.version_id) / "reports"
    report_id = _next_id(reports, "RP", "")
    target = reports / report_id
    record = {
        "artifact_type": "review-report",
        "schema_version": 1,
        "report_id": report_id,
        "manuscript_version_id": workspace.version_id,
        "source_sha256": version["source"]["sha256"],
        "report": {"relative_path": "report.md", "source_name": source_name, "sha256": sha256_bytes(data)},
        "provenance": _provenance("human-confirmed", producer),
        "lifecycle": "immutable",
    }
    _write_new(target / "report.md", data)
    _write_new(target / "record.json", json_bytes(record))
    return report_id


def _latest_dir(directory: Path, prefix: str) -> Path:
    if not directory.exists() or directory.is_symlink():
        raise WorkbenchError(f"no {prefix} artifact exists")
    candidates = [item for item in directory.iterdir() if item.is_dir() and not item.is_symlink() and re.fullmatch(prefix + r"[0-9]{4}", item.name)]
    if not candidates:
        raise WorkbenchError(f"no {prefix} artifact exists")
    return sorted(candidates)[-1]


def prepare_atomization(project_dir: Path | str, report_id: str | None = None) -> Path:
    workspace, version, _, manuscript = _source(project_dir)
    reports = _quick_root(workspace.root, workspace.version_id) / "reports"
    report_dir = reports / report_id if report_id else _latest_dir(reports, "RP")
    report_record, report_record_bytes = _read_json(report_dir / "record.json")
    report_bytes = (report_dir / "report.md").read_bytes()
    if sha256_bytes(report_bytes) != report_record.get("report", {}).get("sha256"):
        raise WorkbenchError("review report hash does not match")
    report_text = report_bytes.decode("utf-8-sig")
    runs = _quick_root(workspace.root, workspace.version_id) / "atomization-runs"
    run_id = _next_id(runs, "AR", "")
    run = runs / run_id
    contract = {
        "schema_version": ATOMIZATION_SCHEMA_VERSION,
        "run_id": run_id,
        "manuscript_version_id": workspace.version_id,
        "source_sha256": version["source"]["sha256"],
        "findings": [{
            "finding_id": "F1", "claim_id": "C1", "report_quote": "verbatim quote from report",
            "manuscript_quote": "unique verbatim quote or null", "location_kind": "exact_quote | missing_implementation | unlocated",
            "assertion": "atomic problem", "criterion": "review rule or perspective", "suggested_action": "bounded action",
            "evidence_level": "verified | unverified | uncertain", "uncertainties": [],
        }],
    }
    prompt = (
        "# Review report atomization protocol\n\n"
        "Return one strict JSON object and no prose. Do not upgrade the report's interpretation into fact. "
        "Split the report into atomic findings; preserve disagreements instead of merging them. "
        "For exact_quote, manuscript_quote must occur exactly once. If the report alleges missing content, use "
        "missing_implementation and null. If it cannot be located, use unlocated, null, and evidence_level unverified.\n\n"
        f"Required contract example:\n```json\n{json.dumps(contract, ensure_ascii=False, indent=2)}\n```\n\n"
        f"## Immutable manuscript {workspace.version_id}\n\nSource SHA-256: `{version['source']['sha256']}`\n\n{manuscript}\n\n"
        f"## Imported review report {report_dir.name}\n\n{report_text}\n"
    ).encode("utf-8")
    record = {
        "artifact_type": "report-atomization-run", "schema_version": 1, "run_id": run_id,
        "manuscript_version_id": workspace.version_id, "source_sha256": version["source"]["sha256"],
        "report_id": report_dir.name, "prompt": {"relative_path": "prompt.md", "sha256": sha256_bytes(prompt)},
        "parents": [{"role": "review-report", "relative_path": str((report_dir / 'record.json').relative_to(workspace.root)).replace('\\','/'), "sha256": sha256_bytes(report_record_bytes)}],
        "provenance": _provenance("deterministic", "argument-revision"), "lifecycle": "immutable",
    }
    _write_new(run / "prompt.md", prompt)
    _write_new(run / "record.json", json_bytes(record))
    return run


def _string_list(value: object, field: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        errors.append(f"{field} must be a list of strings")
        return []
    return list(value)


def _validate_atomization(value: object, run: dict[str, Any], manuscript: str, report: str) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["response must be an object"], []
    required = {"schema_version", "run_id", "manuscript_version_id", "source_sha256", "findings"}
    if set(value) != required:
        errors.append("response has unexpected or missing top-level fields")
    for field in ("run_id", "manuscript_version_id", "source_sha256"):
        if value.get(field) != run.get(field):
            errors.append(f"{field} does not match atomization run")
    if value.get("schema_version") != ATOMIZATION_SCHEMA_VERSION:
        errors.append("unsupported atomization schema_version")
    findings = value.get("findings")
    if not isinstance(findings, list):
        return errors + ["findings must be a list"], []
    expected = {"finding_id", "claim_id", "report_quote", "manuscript_quote", "location_kind", "assertion", "criterion", "suggested_action", "evidence_level", "uncertainties"}
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for number, item in enumerate(findings, 1):
        label = f"findings[{number}]"
        if not isinstance(item, dict) or set(item) != expected:
            errors.append(f"{label} has unexpected or missing fields")
            continue
        finding_id = item.get("finding_id")
        if not isinstance(finding_id, str) or ID.fullmatch(finding_id) is None or finding_id in seen:
            errors.append(f"{label}.finding_id must be unique and stable")
        else:
            seen.add(finding_id)
        for field in ("claim_id", "report_quote", "assertion", "criterion", "suggested_action"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                errors.append(f"{label}.{field} must be non-empty text")
        if isinstance(item.get("report_quote"), str) and item["report_quote"] not in report:
            errors.append(f"{label}.report_quote does not occur in imported report")
        kind = item.get("location_kind")
        if kind not in LOCATION_KINDS:
            errors.append(f"{label}.location_kind is invalid")
        quote = item.get("manuscript_quote")
        if kind == "exact_quote":
            if not isinstance(quote, str) or not quote:
                errors.append(f"{label}.manuscript_quote is required for exact_quote")
            elif manuscript.count(quote) != 1:
                errors.append(f"{label}.manuscript_quote must occur exactly once")
        elif quote is not None:
            errors.append(f"{label}.manuscript_quote must be null when not exactly located")
        if item.get("evidence_level") not in EVIDENCE_LEVELS:
            errors.append(f"{label}.evidence_level is invalid")
        _string_list(item.get("uncertainties"), f"{label}.uncertainties", errors)
        normalized.append(item)
    return errors, normalized


def _repair_prompt(kind: str, errors: Iterable[str], original: bytes, contract_prompt: bytes) -> bytes:
    return (
        f"# Repair invalid {kind} result\n\nReturn corrected strict JSON only. The invalid response is preserved.\n\n"
        + "\n".join(f"- {error}" for error in errors)
        + "\n\n## Original protocol\n\n" + contract_prompt.decode("utf-8")
        + "\n\n## Invalid response\n\n```text\n" + original.decode("utf-8", errors="replace") + "\n```\n"
    ).encode("utf-8")


def collect_atomization_result(project_dir: Path | str, response: str | bytes, *, run_id: str | None = None, producer: str = "manual-model-bridge") -> AttemptResult:
    workspace, _, _, manuscript = _source(project_dir)
    runs = _quick_root(workspace.root, workspace.version_id) / "atomization-runs"
    run_dir = runs / run_id if run_id else _latest_dir(runs, "AR")
    run, _ = _read_json(run_dir / "record.json")
    report_record = _read_json(_quick_root(workspace.root, workspace.version_id) / "reports" / run["report_id"] / "record.json")[0]
    report_path = _quick_root(workspace.root, workspace.version_id) / "reports" / run["report_id"] / report_record["report"]["relative_path"]
    report = report_path.read_bytes().decode("utf-8-sig")
    raw = response.encode("utf-8") if isinstance(response, str) else response
    attempts = run_dir / "attempts"
    attempt_id = _next_id(attempts, "attempt-", "")
    target = attempts / attempt_id
    errors: list[str]
    findings: list[dict[str, Any]] = []
    try:
        value = parse_json_strict(raw)
        errors, findings = _validate_atomization(value, run, manuscript, report)
    except WorkbenchError as exc:
        value = None
        errors = [str(exc)]
    response_path = target / "response.json"
    _write_new(response_path, raw)
    repair = None
    record = {
        "artifact_type": "report-atomization-attempt", "schema_version": 1,
        "attempt_id": attempt_id, "run_id": run_dir.name, "valid": not errors,
        "errors": errors, "response_sha256": sha256_bytes(raw),
        "provenance": _provenance("model-derived", producer), "lifecycle": "immutable",
    }
    _write_new(target / "record.json", json_bytes(record))
    if errors:
        repair = target / "repair-prompt.md"
        _write_new(repair, _repair_prompt("atomization", errors, raw, (run_dir / "prompt.md").read_bytes()))
    else:
        derived = {"artifact_type": "atomic-findings", "schema_version": 1, "run_id": run_dir.name, "attempt_id": attempt_id, "findings": findings, "provenance": _provenance("model-derived", producer), "lifecycle": "immutable"}
        _write_new(target / "findings.json", json_bytes(derived))
    return AttemptResult(attempt_id, not errors, tuple(errors), response_path, repair)


def _latest_valid_findings(project_dir: Path | str) -> tuple[Any, Path, dict[str, Any]]:
    workspace = _workflow_workspace(project_dir)
    runs = _quick_root(workspace.root, workspace.version_id) / "atomization-runs"
    if not runs.is_dir() or runs.is_symlink():
        raise WorkbenchError("no valid atomized findings exist")
    for run in sorted((item for item in runs.iterdir() if item.is_dir() and not item.is_symlink()), reverse=True):
        attempts = run / "attempts"
        if not attempts.is_dir() or attempts.is_symlink():
            continue
        for attempt in sorted((item for item in attempts.iterdir() if item.is_dir() and not item.is_symlink()), reverse=True):
            path = attempt / "findings.json"
            if path.is_file() and not path.is_symlink():
                return workspace, path, _read_json(path)[0]
    raise WorkbenchError("no valid atomized findings exist")


def current_quick_findings(project_dir: Path | str) -> list[dict[str, Any]]:
    workspace, findings_path, payload = _latest_valid_findings(project_dir)
    findings_sha256 = sha256_bytes(findings_path.read_bytes())
    decisions: dict[str, dict[str, Any]] = {}
    for path in _regular_files(_quick_root(workspace.root, workspace.version_id) / "finding-decisions", "FD*.json"):
        decision, _ = _read_json(path)
        if decision.get("source_findings_sha256") == findings_sha256:
            decisions[str(decision.get("finding_id"))] = decision
    rows = []
    for finding in payload["findings"]:
        decision = decisions.get(finding["finding_id"])
        corrected = dict(finding)
        if decision and isinstance(decision.get("corrections"), dict):
            corrected.update(decision["corrections"])
        rows.append({**corrected, "decision": None if decision is None else decision["decision"], "decision_id": None if decision is None else decision["decision_id"], "action_id": None if decision is None else decision.get("action_id"), "source_findings_sha256": findings_sha256})
    return rows


def append_quick_finding_decision(project_dir: Path | str, finding_id: str, *, decision: str, reason: str, corrections: dict[str, Any] | None = None, action_text: str | None = None, producer: str = "local-workbench-ui") -> str:
    workspace, findings_path, _ = _latest_valid_findings(project_dir)
    findings = {item["finding_id"]: item for item in current_quick_findings(project_dir)}
    if finding_id not in findings:
        raise WorkbenchError("unknown finding_id")
    if decision not in FINDING_DECISIONS or not reason.strip():
        raise WorkbenchError("finding decision and reason are required")
    allowed_corrections = {"claim_id", "manuscript_quote", "location_kind", "assertion", "criterion", "suggested_action", "evidence_level", "uncertainties"}
    corrections = corrections or {}
    if not isinstance(corrections, dict) or not set(corrections).issubset(allowed_corrections):
        raise WorkbenchError("finding corrections contain unsupported fields")
    candidate = dict(findings[finding_id]); candidate.update(corrections)
    for field in ("claim_id", "assertion", "criterion", "suggested_action"):
        if not isinstance(candidate.get(field), str) or not candidate[field].strip():
            raise WorkbenchError(f"corrected {field} must be non-empty text")
    if candidate.get("location_kind") not in LOCATION_KINDS or candidate.get("evidence_level") not in EVIDENCE_LEVELS:
        raise WorkbenchError("corrected location_kind or evidence_level is invalid")
    if not isinstance(candidate.get("uncertainties"), list) or any(not isinstance(item, str) for item in candidate["uncertainties"]):
        raise WorkbenchError("corrected uncertainties must be a list of strings")
    _, _, _, manuscript = _source(workspace.root, workspace.version_id)
    quote = candidate.get("manuscript_quote")
    if candidate["location_kind"] == "exact_quote":
        if not isinstance(quote, str) or not quote or manuscript.count(quote) != 1:
            raise WorkbenchError("corrected exact manuscript quote must occur exactly once")
    elif quote is not None:
        raise WorkbenchError("non-exact finding must not carry a manuscript quote")
    _ensure_integrity_marker(workspace.root)
    decisions = _quick_root(workspace.root, workspace.version_id) / "finding-decisions"
    decision_id = _next_id(decisions, "FD")
    action_id = None
    if decision == "accept":
        actions = _quick_root(workspace.root, workspace.version_id) / "revision-actions"
        action_id = _next_id(actions, "QA")
        action = {"artifact_type": "quick-revision-action", "schema_version": 1, "action_id": action_id, "finding_id": finding_id, "text": action_text or str(corrections.get("suggested_action", findings[finding_id]["suggested_action"])), "provenance": _provenance("human-confirmed", producer), "lifecycle": "append-only"}
        _write_tracked(workspace.root, actions / f"{action_id}.json", json_bytes(action))
    record = {"artifact_type": "quick-finding-decision", "schema_version": 1, "decision_id": decision_id, "finding_id": finding_id, "decision": decision, "reason": reason, "corrections": corrections, "action_id": action_id, "source_findings_sha256": sha256_bytes(findings_path.read_bytes()), "provenance": _provenance("human-confirmed", producer), "lifecycle": "append-only"}
    _write_tracked(workspace.root, decisions / f"{decision_id}.json", json_bytes(record))
    return decision_id


def prepare_revision_generation(project_dir: Path | str) -> Path:
    workflow = _workflow_workspace(project_dir)
    workspace, version, _, manuscript = _source(workflow.root, workflow.version_id)
    selected = [item for item in current_quick_findings(project_dir) if item["decision"] == "accept"]
    if not selected:
        raise WorkbenchError("at least one finding must be accepted for revision")
    if any(not item.get("action_id") for item in selected):
        raise WorkbenchError("every accepted finding needs a RevisionAction")
    runs = _quick_root(workspace.root, workspace.version_id) / "revision-generation-runs"
    run_id = _next_id(runs, "RG", "")
    run = runs / run_id
    allowed = [{key: item.get(key) for key in ("finding_id", "claim_id", "manuscript_quote", "location_kind", "assertion", "criterion", "suggested_action", "evidence_level", "uncertainties", "action_id")} for item in selected]
    contract = {"schema_version": REVISION_SCHEMA_VERSION, "generation_run_id": run_id, "manuscript_version_id": workspace.version_id, "source_sha256": version["source"]["sha256"], "changes": [{"change_id": "CH1", "original_quote": "unique text or empty for insertion", "insertion_anchor": None, "replacement_text": "new text", "finding_ids": [selected[0]["finding_id"]], "action_ids": [selected[0]["action_id"]], "change_kind": "replace", "reason": "why this solves the finding", "uncertainties": [], "fact_change": False, "verification_note": ""}]}
    prompt = ("# Constrained revision proposal\n\nReturn strict JSON only. Change only material required by the accepted findings below. Every change must be independently locatable and linked to allowed finding/action IDs. Do not return a whole rewritten manuscript. For missing content, set original_quote to an empty string and provide a unique insertion_anchor with insert_before or insert_after. Mark any change to quotations, numbers, names, citations, URLs, or factual assertions with fact_change true and a verification_note.\n\n" + f"Contract:\n```json\n{json.dumps(contract, ensure_ascii=False, indent=2)}\n```\n\nAllowed findings/actions:\n```json\n{json.dumps(allowed, ensure_ascii=False, indent=2)}\n```\n\n" + f"Immutable manuscript {workspace.version_id}, SHA-256 {version['source']['sha256']}:\n\n{manuscript}\n").encode()
    record = {"artifact_type": "revision-generation-run", "schema_version": 1, "generation_run_id": run_id, "manuscript_version_id": workspace.version_id, "source_sha256": version["source"]["sha256"], "finding_ids": [item["finding_id"] for item in selected], "action_ids": [item["action_id"] for item in selected], "finding_bindings": [{"finding_id": item["finding_id"], "manuscript_quote": item["manuscript_quote"], "location_kind": item["location_kind"]} for item in selected], "finding_action_bindings": [{"finding_id": item["finding_id"], "action_id": item["action_id"]} for item in selected], "prompt": {"relative_path": "prompt.md", "sha256": sha256_bytes(prompt)}, "provenance": _provenance("deterministic", "argument-revision"), "lifecycle": "immutable"}
    _write_new(run / "prompt.md", prompt); _write_new(run / "record.json", json_bytes(record))
    return run


def _validate_revision(value: object, run: dict[str, Any], manuscript: str) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    if not isinstance(value, dict): return ["response must be an object"], []
    if set(value) != {"schema_version", "generation_run_id", "manuscript_version_id", "source_sha256", "changes"}: errors.append("response has unexpected or missing top-level fields")
    if value.get("schema_version") != REVISION_SCHEMA_VERSION: errors.append("unsupported revision schema_version")
    for field in ("generation_run_id", "manuscript_version_id", "source_sha256"):
        if value.get(field) != run.get(field): errors.append(f"{field} does not match generation run")
    changes = value.get("changes")
    if not isinstance(changes, list) or not changes: return errors + ["changes must be a non-empty list"], []
    expected = {"change_id", "original_quote", "insertion_anchor", "replacement_text", "finding_ids", "action_ids", "change_kind", "reason", "uncertainties", "fact_change", "verification_note"}
    ranges: list[tuple[int, int, str]] = []; seen: set[str] = set(); normalized: list[dict[str, Any]] = []
    allowed_findings, allowed_actions = set(run["finding_ids"]), set(run["action_ids"])
    bindings = {item["finding_id"]: item for item in run.get("finding_bindings", []) if isinstance(item, dict)}
    raw_finding_actions = run.get("finding_action_bindings")
    finding_actions = {
        item["finding_id"]: item["action_id"]
        for item in (raw_finding_actions or [])
        if isinstance(item, dict)
        and isinstance(item.get("finding_id"), str)
        and isinstance(item.get("action_id"), str)
    }
    if raw_finding_actions is None and len(run["finding_ids"]) == len(run["action_ids"]):
        # Schema-v1 runs created before explicit pair bindings preserved both
        # arrays in the same selected-finding order.
        finding_actions = dict(zip(run["finding_ids"], run["action_ids"]))
    elif set(finding_actions) != allowed_findings or set(finding_actions.values()) != allowed_actions:
        errors.append("generation run Finding–Action bindings are incomplete or inconsistent")
    for number, change in enumerate(changes, 1):
        label = f"changes[{number}]"
        if not isinstance(change, dict) or set(change) != expected: errors.append(f"{label} has unexpected or missing fields"); continue
        cid = change.get("change_id")
        if not isinstance(cid, str) or ID.fullmatch(cid) is None or cid in seen: errors.append(f"{label}.change_id must be unique and stable")
        else: seen.add(cid)
        fids = _string_list(change.get("finding_ids"), f"{label}.finding_ids", errors); aids = _string_list(change.get("action_ids"), f"{label}.action_ids", errors)
        if len(fids) != len(set(fids)): errors.append(f"{label}.finding_ids must be unique")
        if len(aids) != len(set(aids)): errors.append(f"{label}.action_ids must be unique")
        if not fids or not set(fids).issubset(allowed_findings): errors.append(f"{label}.finding_ids reference unselected findings")
        if not aids or not set(aids).issubset(allowed_actions): errors.append(f"{label}.action_ids reference unselected actions")
        expected_actions = {finding_actions[finding_id] for finding_id in fids if finding_id in finding_actions}
        if set(aids) != expected_actions:
            errors.append(f"{label} Finding–Action bindings do not match")
        if change.get("change_kind") not in CHANGE_KINDS: errors.append(f"{label}.change_kind is invalid")
        for field in ("original_quote", "replacement_text", "reason", "verification_note"):
            if not isinstance(change.get(field), str): errors.append(f"{label}.{field} must be text")
        if not isinstance(change.get("reason"), str) or not change["reason"].strip(): errors.append(f"{label}.reason must be non-empty")
        _string_list(change.get("uncertainties"), f"{label}.uncertainties", errors)
        if not isinstance(change.get("fact_change"), bool): errors.append(f"{label}.fact_change must be boolean")
        quote, anchor = change.get("original_quote"), change.get("insertion_anchor")
        if quote:
            if change.get("change_kind") in {"insert_before", "insert_after"}: errors.append(f"{label} insertion must use insertion_anchor")
            if manuscript.count(quote) != 1: errors.append(f"{label}.original_quote must occur exactly once")
            else:
                start = manuscript.index(quote); ranges.append((start, start + len(quote), str(cid)))
            if anchor is not None: errors.append(f"{label}.insertion_anchor must be null for replacement")
            linked_quotes = [bindings.get(fid, {}).get("manuscript_quote") for fid in fids]
            if not any(isinstance(bound, str) and bound and (bound in quote or quote in bound) for bound in linked_quotes):
                errors.append(f"{label} is outside the located text of its linked findings")
        else:
            if change.get("change_kind") not in {"insert_before", "insert_after"}: errors.append(f"{label} empty original_quote requires insertion change_kind")
            if not isinstance(anchor, str) or not anchor or manuscript.count(anchor) != 1: errors.append(f"{label}.insertion_anchor must occur exactly once")
            else:
                point = manuscript.index(anchor) + (len(anchor) if change["change_kind"] == "insert_after" else 0); ranges.append((point, point, str(cid)))
            if not any(bindings.get(fid, {}).get("location_kind") == "missing_implementation" for fid in fids):
                errors.append(f"{label} insertion requires a linked missing_implementation finding")
        replacement = change.get("replacement_text", "")
        factual_signal = bool(re.search(r"\d|https?://|[“”‘’\"']", str(replacement))) and replacement != quote
        if factual_signal and change.get("fact_change") is not True: errors.append(f"{label} changes a number/quote/link and must set fact_change true")
        if change.get("fact_change") is True and not str(change.get("verification_note", "")).strip(): errors.append(f"{label}.verification_note is required for fact_change")
        normalized.append(change)
    ordered = sorted(ranges)
    for left, right in zip(ordered, ordered[1:]):
        if right[0] < left[1] or (left[0] == left[1] == right[0] == right[1]): errors.append(f"changes {left[2]} and {right[2]} overlap")
    return errors, normalized


def collect_revision_result(project_dir: Path | str, response: str | bytes, *, run_id: str | None = None, producer: str = "manual-model-bridge") -> AttemptResult:
    workflow = _workflow_workspace(project_dir)
    workspace, _, _, manuscript = _source(workflow.root, workflow.version_id)
    runs = _quick_root(workspace.root, workspace.version_id) / "revision-generation-runs"; run_dir = runs / run_id if run_id else _latest_dir(runs, "RG")
    run, _ = _read_json(run_dir / "record.json"); raw = response.encode() if isinstance(response, str) else response
    attempts = run_dir / "attempts"; attempt_id = _next_id(attempts, "attempt-", ""); target = attempts / attempt_id
    try: value = parse_json_strict(raw); errors, changes = _validate_revision(value, run, manuscript)
    except WorkbenchError as exc: value = None; errors, changes = [str(exc)], []
    response_path = target / "response.json"; _write_new(response_path, raw)
    record = {"artifact_type": "revision-result-attempt", "schema_version": 1, "attempt_id": attempt_id, "generation_run_id": run_dir.name, "valid": not errors, "errors": errors, "response_sha256": sha256_bytes(raw), "provenance": _provenance("model-derived", producer), "lifecycle": "immutable"}
    _write_new(target / "record.json", json_bytes(record)); repair = None
    if errors:
        repair = target / "repair-prompt.md"; _write_new(repair, _repair_prompt("revision", errors, raw, (run_dir / "prompt.md").read_bytes()))
    else:
        proposal = {"artifact_type": "revision-patch-proposal", "schema_version": 1, "generation_run_id": run_dir.name, "attempt_id": attempt_id, "manuscript_version_id": run["manuscript_version_id"], "source_sha256": run["source_sha256"], "changes": changes, "provenance": _provenance("model-derived", producer), "lifecycle": "immutable"}
        _write_new(target / "revision-patch-proposal.json", json_bytes(proposal))
    return AttemptResult(attempt_id, not errors, tuple(errors), response_path, repair)


def _latest_proposal(project_dir: Path | str) -> tuple[Any, Path, dict[str, Any]]:
    workspace = _workflow_workspace(project_dir); runs = _quick_root(workspace.root, workspace.version_id) / "revision-generation-runs"
    if not runs.is_dir() or runs.is_symlink(): raise WorkbenchError("no valid revision patch proposal exists")
    for run in sorted((p for p in runs.iterdir() if p.is_dir() and not p.is_symlink()), reverse=True):
        attempts = run / "attempts"
        if not attempts.is_dir(): continue
        for attempt in sorted((p for p in attempts.iterdir() if p.is_dir() and not p.is_symlink()), reverse=True):
            proposal = attempt / "revision-patch-proposal.json"
            if proposal.is_file() and not proposal.is_symlink(): return workspace, proposal, _read_json(proposal)[0]
    raise WorkbenchError("no valid revision patch proposal exists")


def revision_hunks(project_dir: Path | str) -> list[dict[str, Any]]:
    workspace, proposal_path, proposal = _latest_proposal(project_dir); decisions: dict[str, dict[str, Any]] = {}
    run = proposal_path.parents[2]
    proposal_sha256 = sha256_bytes(proposal_path.read_bytes())
    for path in _regular_files(run / "hunk-decisions", "HD*.json"):
        row, _ = _read_json(path)
        if row.get("proposal_sha256") == proposal_sha256:
            decisions[row["change_id"]] = row
    return [{**change, "decision": decisions.get(change["change_id"]), "proposal_sha256": proposal_sha256} for change in proposal["changes"]]


def append_hunk_decision(project_dir: Path | str, change_id: str, *, decision: str, reason: str, edited_text: str | None = None, producer: str = "local-workbench-ui") -> str:
    workspace, proposal_path, proposal = _latest_proposal(project_dir); changes = {item["change_id"]: item for item in proposal["changes"]}; change_ids = set(changes)
    if change_id not in change_ids or decision not in HUNK_DECISIONS or not reason.strip(): raise WorkbenchError("valid change_id, decision, and reason are required")
    if decision == "edit" and edited_text is None: raise WorkbenchError("edited_text is required for edit")
    if decision != "edit" and edited_text is not None: raise WorkbenchError("edited_text is only allowed for edit")
    _ensure_integrity_marker(workspace.root)
    directory = proposal_path.parents[2] / "hunk-decisions"; decision_id = _next_id(directory, "HD")
    manual_fact_signal = decision == "edit" and bool(re.search(r"\d|https?://|[“”‘’\"']", edited_text or "")) and edited_text != changes[change_id]["replacement_text"]
    regeneration_ref = None
    if decision == "regenerate":
        prompt = (
            "# Regenerate one revision hunk\n\n"
            f"Regenerate only `{change_id}`. Return the complete strict revision proposal object, "
            "preserving every unrelated change byte-for-byte. The full response will be revalidated, "
            "and every hunk will require a new human decision because the proposal hash changes.\n\n"
            "## Current validated proposal\n\n```json\n"
            + json.dumps(proposal, ensure_ascii=False, indent=2)
            + "\n```\n\n## Original generation protocol\n\n"
            + (proposal_path.parents[2] / "prompt.md").read_text(encoding="utf-8")
        ).encode("utf-8")
        prompt_path = directory / f"{decision_id}-regeneration-prompt.md"
        _write_new(prompt_path, prompt)
        regeneration_ref = {"relative_path": prompt_path.name, "sha256": sha256_bytes(prompt)}
    record = {"artifact_type": "revision-hunk-decision", "schema_version": 1, "decision_id": decision_id, "change_id": change_id, "decision": decision, "reason": reason, "edited_text": edited_text, "fact_change": bool(changes[change_id]["fact_change"] or manual_fact_signal), "verification_note": changes[change_id]["verification_note"] or ("UNVERIFIED: human-edited text contains a number, quotation, or link." if manual_fact_signal else ""), "regeneration_prompt": regeneration_ref, "proposal_sha256": sha256_bytes(proposal_path.read_bytes()), "provenance": _provenance("human-confirmed", producer), "lifecycle": "append-only"}
    _write_tracked(workspace.root, directory / f"{decision_id}.json", json_bytes(record)); return decision_id


def apply_approved_hunks(project_dir: Path | str, *, producer: str = "local-workbench-ui") -> dict[str, Any]:
    workspace, proposal_path, proposal = _latest_proposal(project_dir); _, version, source_bytes, text = _source(workspace.root, proposal["manuscript_version_id"])
    if sha256_bytes(source_bytes) != proposal["source_sha256"]: raise WorkbenchError("base manuscript changed after proposal generation")
    hunks = revision_hunks(project_dir); decisions = {item["change_id"]: item["decision"] for item in hunks}
    if any(value is None or value["decision"] == "regenerate" for value in decisions.values()): raise WorkbenchError("every hunk needs a final accept, reject, or edit decision")
    applications = workspace.document_dir / "revision-applications"
    proposal_hash = sha256_bytes(proposal_path.read_bytes())
    decision_hashes = [sha256_bytes((proposal_path.parents[2] / "hunk-decisions" / f"{item['decision']['decision_id']}.json").read_bytes()) for item in hunks]
    fingerprint = sha256_bytes((proposal_hash + "".join(decision_hashes)).encode())
    for path in _regular_files(applications, "AP*.json"):
        existing, _ = _read_json(path)
        if existing.get("decision_fingerprint") == fingerprint: return existing
    _ensure_integrity_marker(workspace.root)
    edits: list[tuple[int, int, str, dict[str, Any]]] = []
    for item in hunks:
        decision = item["decision"]; choice = decision["decision"]
        if choice == "reject": continue
        replacement = decision["edited_text"] if choice == "edit" else item["replacement_text"]
        if item["original_quote"]:
            start = text.index(item["original_quote"]); end = start + len(item["original_quote"])
        else:
            anchor = item["insertion_anchor"]; start = text.index(anchor) + (len(anchor) if item["change_kind"] == "insert_after" else 0); end = start
        edits.append((start, end, replacement, item))
    output = text
    for start, end, replacement, _ in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True): output = output[:start] + replacement + output[end:]
    output_bytes = output.encode("utf-8")
    application_id = _next_id(applications, "AP")
    staging = proposal_path.parents[2] / f"{application_id}-revised-manuscript.md"; _atomic_write(staging, output_bytes)
    try: new_workspace = import_document_version(workspace.root, staging, parent_version=workspace.version_id)
    finally: staging.unlink(missing_ok=True)
    record = {"artifact_type": "revision-application-record", "schema_version": 1, "application_id": application_id, "generation_run_id": proposal["generation_run_id"], "proposal_sha256": proposal_hash, "decision_fingerprint": fingerprint, "base_version_id": workspace.version_id, "base_source_sha256": proposal["source_sha256"], "output_version_id": new_workspace.version_id, "output_source_sha256": sha256_bytes(output_bytes), "applied_changes": [{"change_id": item[3]["change_id"], "decision_id": decisions[item[3]["change_id"]]["decision_id"], "finding_ids": item[3]["finding_ids"], "action_ids": item[3]["action_ids"], "fact_change": decisions[item[3]["change_id"]].get("fact_change", item[3]["fact_change"]), "verification_note": decisions[item[3]["change_id"]].get("verification_note", item[3]["verification_note"])} for item in sorted(edits, key=lambda item: (item[0], item[1]))], "rejected_changes": [item["change_id"] for item in hunks if item["decision"]["decision"] == "reject"], "decision_sha256s": decision_hashes, "provenance": _provenance("deterministic", producer), "lifecycle": "immutable"}
    _write_tracked(workspace.root, applications / f"{application_id}.json", json_bytes(record)); return record


def prepare_resolution_review(project_dir: Path | str, application_id: str | None = None) -> Path:
    root = workspace_paths(project_dir).root; applications = root / "documents" / "D1" / "revision-applications"
    app_path = applications / f"{application_id}.json" if application_id else _regular_files(applications, "AP*.json")[-1]
    application, app_bytes = _read_json(app_path); _, _, _, manuscript = _source(root, application["output_version_id"])
    findings = [item for item in current_quick_findings(root) if item["decision"] == "accept"]
    runs = root / "documents" / "D1" / "resolution-runs"; run_id = _next_id(runs, "RR", ""); run = runs / run_id
    contract = {"schema_version": RESOLUTION_SCHEMA_VERSION, "resolution_run_id": run_id, "manuscript_version_id": application["output_version_id"], "source_sha256": application["output_source_sha256"], "results": [{"finding_id": findings[0]["finding_id"], "proposed_status": "resolved | partially_resolved | unresolved | not_evaluated", "reason": "evidence under original criterion", "evidence_quotes": [], "uncertainties": []}]}
    prompt = ("# Original-criterion revision review\n\nRe-evaluate every finding against the new manuscript using its original criterion. Return strict JSON only. A text change is not evidence of resolution. Use not_evaluated whenever the available text cannot establish a status.\n\n" + f"Contract:\n```json\n{json.dumps(contract, ensure_ascii=False, indent=2)}\n```\n\nOriginal findings:\n```json\n{json.dumps(findings, ensure_ascii=False, indent=2)}\n```\n\nNew manuscript:\n\n{manuscript}\n").encode()
    record = {"artifact_type": "finding-resolution-run", "schema_version": 1, "resolution_run_id": run_id, "application_id": application["application_id"], "manuscript_version_id": application["output_version_id"], "source_sha256": application["output_source_sha256"], "finding_ids": [item["finding_id"] for item in findings], "application_sha256": sha256_bytes(app_bytes), "prompt": {"relative_path": "prompt.md", "sha256": sha256_bytes(prompt)}, "provenance": _provenance("deterministic", "argument-revision"), "lifecycle": "immutable"}
    _write_new(run / "prompt.md", prompt); _write_new(run / "record.json", json_bytes(record)); return run


def _validate_resolution(value: object, record: dict[str, Any], manuscript: str) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["response must be an object"], []
    if set(value) != {"schema_version", "resolution_run_id", "manuscript_version_id", "source_sha256", "results"}:
        errors.append("response has unexpected or missing fields")
    if value.get("schema_version") != RESOLUTION_SCHEMA_VERSION:
        errors.append("unsupported resolution schema_version")
    for field in ("resolution_run_id", "manuscript_version_id", "source_sha256"):
        if value.get(field) != record.get(field):
            errors.append(f"{field} does not match resolution run")
    finding_ids = record.get("finding_ids")
    results = value.get("results")
    if (
        not isinstance(finding_ids, list)
        or not isinstance(results, list)
        or len(results) != len(finding_ids)
        or {item.get("finding_id") for item in results if isinstance(item, dict)} != set(finding_ids)
    ):
        return errors + ["results must cover every selected finding exactly once"], []
    expected = {"finding_id", "proposed_status", "reason", "evidence_quotes", "uncertainties"}
    normalized: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict) or set(item) != expected or item.get("proposed_status") not in RESOLUTION_STATUSES or not isinstance(item.get("reason"), str):
            errors.append("resolution result item is invalid")
            continue
        evidence_quotes = _string_list(item.get("evidence_quotes"), "evidence_quotes", errors)
        _string_list(item.get("uncertainties"), "uncertainties", errors)
        for quote in evidence_quotes:
            if manuscript.count(quote) != 1:
                errors.append(f"resolution evidence quote for {item.get('finding_id')} must occur exactly once in V2")
        normalized.append(item)
    return errors, normalized


def collect_resolution_result(project_dir: Path | str, response: str | bytes, *, run_id: str | None = None, producer: str = "manual-model-bridge") -> AttemptResult:
    root = workspace_paths(project_dir).root; runs = root / "documents" / "D1" / "resolution-runs"; run = runs / run_id if run_id else _latest_dir(runs, "RR"); record, _ = _read_json(run / "record.json")
    raw = response.encode() if isinstance(response, str) else response; attempts = run / "attempts"; attempt_id = _next_id(attempts, "attempt-", ""); target = attempts / attempt_id
    try:
        value = parse_json_strict(raw)
        _, _, _, manuscript = _source(root, record["manuscript_version_id"])
        errors, _ = _validate_resolution(value, record, manuscript)
    except (WorkbenchError, KeyError) as exc:
        value = None; errors = [str(exc)]
    response_path = target / "response.json"; _write_new(response_path, raw); repair = None
    attempt = {"artifact_type": "resolution-result-attempt", "schema_version": 1, "attempt_id": attempt_id, "resolution_run_id": run.name, "valid": not errors, "errors": errors, "response_sha256": sha256_bytes(raw), "provenance": _provenance("model-derived", producer), "lifecycle": "immutable"}; _write_new(target / "record.json", json_bytes(attempt))
    if errors: repair = target / "repair-prompt.md"; _write_new(repair, _repair_prompt("resolution", errors, raw, (run / "prompt.md").read_bytes()))
    else:
        proposals = {"artifact_type": "finding-resolution-proposals", "schema_version": 1, "resolution_run_id": run.name, "results": value["results"], "notice": "Model-proposed only; no finding is resolved until a human decision is appended.", "provenance": _provenance("model-derived", producer), "lifecycle": "immutable"}; _write_new(target / "resolution-proposals.json", json_bytes(proposals))
    return AttemptResult(attempt_id, not errors, tuple(errors), response_path, repair)


def _latest_resolution_proposal(project_dir: Path | str, run: Path | None = None) -> tuple[Path, Path, dict[str, Any]]:
    root = workspace_paths(project_dir).root
    selected_run = run or _latest_dir(root / "documents" / "D1" / "resolution-runs", "RR")
    attempts = selected_run / "attempts"
    if not attempts.is_dir() or attempts.is_symlink():
        raise WorkbenchError("no valid resolution proposal exists")
    for attempt in sorted((path for path in attempts.iterdir() if path.is_dir() and not path.is_symlink()), reverse=True):
        proposal_path = attempt / "resolution-proposals.json"
        if proposal_path.is_file() and not proposal_path.is_symlink():
            return selected_run, proposal_path, _read_json(proposal_path)[0]
    raise WorkbenchError("no valid resolution proposal exists")


def append_resolution_decision(project_dir: Path | str, finding_id: str, *, status: str, reason: str, producer: str = "local-workbench-ui") -> str:
    root = workspace_paths(project_dir).root
    run, proposal_path, proposal = _latest_resolution_proposal(project_dir)
    proposed = {item["finding_id"] for item in proposal["results"]}
    if finding_id not in proposed or status not in RESOLUTION_STATUSES or not reason.strip(): raise WorkbenchError("valid finding, status, and reason are required")
    _ensure_integrity_marker(root)
    directory = run / "human-decisions"; decision_id = _next_id(directory, "RD")
    value = {"artifact_type": "finding-resolution-decision", "schema_version": 1, "decision_id": decision_id, "finding_id": finding_id, "final_status": status, "reason": reason, "proposal_sha256": sha256_bytes(proposal_path.read_bytes()), "provenance": _provenance("human-confirmed", producer), "lifecycle": "append-only"}; _write_tracked(root, directory / f"{decision_id}.json", json_bytes(value)); return decision_id


def complete_without_revision(project_dir: Path | str, *, reason: str, producer: str = "local-workbench-ui") -> Path:
    """Close a workflow without creating a fake V2 when no revision is selected."""
    if not isinstance(reason, str) or not reason.strip():
        raise WorkbenchError("a reason is required to complete without revision")
    workspace, findings_path, findings_payload = _latest_valid_findings(project_dir)
    findings = current_quick_findings(workspace.root)
    if any(item["decision"] == "accept" for item in findings):
        raise WorkbenchError("accepted findings require the constrained revision workflow")
    if findings and any(item["decision"] is None for item in findings):
        raise WorkbenchError("every finding must be rejected or deferred before completing without revision")

    findings_sha256 = sha256_bytes(findings_path.read_bytes())
    completions = _quick_root(workspace.root, workspace.version_id) / "no-revision-completions"
    for path in reversed(_regular_files(completions, "NC*.json")):
        existing, _ = _read_json(path)
        if existing.get("source_findings_sha256") == findings_sha256:
            export_dir = workspace.root / "exports" / existing["completion_id"]
            if (export_dir / "audit.json").is_file():
                return export_dir

    _ensure_integrity_marker(workspace.root)

    decisions: list[dict[str, Any]] = []
    decision_sha256s: list[str] = []
    decision_dir = _quick_root(workspace.root, workspace.version_id) / "finding-decisions"
    for path in _regular_files(decision_dir, "FD*.json"):
        decision, raw = _read_json(path)
        if decision.get("source_findings_sha256") == findings_sha256:
            decisions.append(decision)
            decision_sha256s.append(sha256_bytes(raw))

    completion_id = _next_id(completions, "NC")
    outcome = "no_findings" if not findings else "all_declined"
    record = {
        "artifact_type": "no-revision-completion",
        "schema_version": 1,
        "completion_id": completion_id,
        "manuscript_version_id": workspace.version_id,
        "source_sha256": _source(workspace.root, workspace.version_id)[1]["source"]["sha256"],
        "source_findings_sha256": findings_sha256,
        "outcome": outcome,
        "reason": reason.strip(),
        "finding_decision_ids": [item["decision_id"] for item in decisions],
        "finding_decision_sha256s": decision_sha256s,
        "provenance": _provenance("human-confirmed", producer),
        "lifecycle": "immutable",
    }
    _write_tracked(workspace.root, completions / f"{completion_id}.json", json_bytes(record))

    _, _, source_bytes, _ = _source(workspace.root, workspace.version_id)
    export_dir = workspace.root / "exports" / completion_id
    _write_new(export_dir / f"{workspace.version_id}.md", source_bytes)
    checklist = [
        "# Revision checklist",
        "",
        f"- Manuscript: `{workspace.version_id}` `{record['source_sha256']}`",
        f"- Outcome: `{outcome}`",
        f"- Reason: {record['reason']}",
        "",
        "No revised document version was created.",
        "",
    ]
    _write_new(export_dir / "revision-checklist.md", "\n".join(checklist).encode("utf-8"))
    audit = {
        "artifact_type": "no-revision-audit-export",
        "schema_version": 1,
        "completion": record,
        "atomic_findings": findings_payload,
        "atomic_findings_sha256": findings_sha256,
        "findings": findings,
        "finding_decisions": decisions,
        "source_sha256": record["source_sha256"],
    }
    _write_new(export_dir / "audit.json", json_bytes(audit))
    _write_new(export_dir / "audit.md", ("# No-revision audit\n\n" + "\n".join(checklist[2:])).encode("utf-8"))
    return export_dir


def export_revision(project_dir: Path | str, application_id: str | None = None) -> Path:
    root = workspace_paths(project_dir).root; applications = root / "documents" / "D1" / "revision-applications"; app_path = applications / f"{application_id}.json" if application_id else _regular_files(applications, "AP*.json")[-1]; application, _ = _read_json(app_path)
    workspace, _, source_bytes, _ = _source(root, application["output_version_id"]); exports = root / "exports" / application["application_id"]
    manuscript = exports / f"{application['output_version_id']}.md"; _atomic_write(manuscript, source_bytes)
    _, _, base_bytes, base_text = _source(root, application["base_version_id"])
    output_text = source_bytes.decode("utf-8-sig")
    diff_text = "".join(difflib.unified_diff(base_text.splitlines(keepends=True), output_text.splitlines(keepends=True), fromfile=application["base_version_id"], tofile=application["output_version_id"]))
    _atomic_write(exports / "V1-V2.diff", diff_text.encode("utf-8"))
    findings = current_quick_findings(root); resolutions: dict[str, dict[str, Any]] = {}
    runs = root / "documents" / "D1" / "resolution-runs"
    if runs.is_dir():
        matching_runs: list[Path] = []
        for run in sorted(path for path in runs.iterdir() if path.is_dir() and not path.is_symlink()):
            record, _ = _read_json(run / "record.json")
            if record.get("application_id") == application["application_id"]:
                matching_runs.append(run)
        if matching_runs:
            resolution_run, resolution_proposal_path, _ = _latest_resolution_proposal(root, matching_runs[-1])
            proposal_sha256 = sha256_bytes(resolution_proposal_path.read_bytes())
            for path in _regular_files(resolution_run / "human-decisions", "RD*.json"):
                item, _ = _read_json(path)
                if item.get("proposal_sha256") == proposal_sha256:
                    resolutions[item["finding_id"]] = item
    lines = ["# Executable revision checklist", "", f"- Base: `{application['base_version_id']}` `{application['base_source_sha256']}`", f"- Output: `{application['output_version_id']}` `{application['output_source_sha256']}`", "", "## Findings", ""]
    for item in findings:
        status = resolutions.get(item["finding_id"], {}).get("final_status", "not_evaluated")
        marker = " UNVERIFIED" if item["evidence_level"] != "verified" or item["uncertainties"] else ""
        lines.append(f"- `{item['finding_id']}` [{item['decision'] or 'open'}] → `{status}`{marker}: {item['assertion']}")
    lines.extend(["", "## Applied hunks", ""]); lines.extend(f"- `{item['change_id']}` ← {', '.join(item['finding_ids'])}" for item in application["applied_changes"]); lines.append("")
    _atomic_write(exports / "revision-checklist.md", "\n".join(lines).encode())
    workflow = _workflow_workspace(root); quick = _quick_root(root, workflow.version_id)
    report_dir = _latest_dir(quick / "reports", "RP"); report_record, _ = _read_json(report_dir / "record.json")
    _, findings_path, findings_payload = _latest_valid_findings(root); _, proposal_path, proposal = _latest_proposal(root)
    finding_decisions = [_read_json(path)[0] for path in _regular_files(quick / "finding-decisions", "FD*.json")]
    actions = [_read_json(path)[0] for path in _regular_files(quick / "revision-actions", "QA*.json")]
    hunk_decisions = [_read_json(path)[0] for path in _regular_files(proposal_path.parents[2] / "hunk-decisions", "HD*.json")]
    audit = {"artifact_type": "revision-audit-export", "schema_version": 1, "source_report": {"record": report_record, "relative_path": str((report_dir / 'report.md').relative_to(root)).replace('\\','/'), "sha256": sha256_bytes((report_dir / 'report.md').read_bytes())}, "atomic_findings": findings_payload, "atomic_findings_sha256": sha256_bytes(findings_path.read_bytes()), "finding_decisions": finding_decisions, "revision_actions": actions, "patch_proposal": proposal, "patch_proposal_sha256": sha256_bytes(proposal_path.read_bytes()), "hunk_decisions": hunk_decisions, "application": application, "v1_sha256": sha256_bytes(base_bytes), "v2_sha256": sha256_bytes(source_bytes), "diff_sha256": sha256_bytes(diff_text.encode('utf-8')), "findings": findings, "resolution_decisions": list(resolutions.values()), "unverified_visible": [item["finding_id"] for item in findings if item["evidence_level"] != "verified" or item["uncertainties"]] + [item["change_id"] for item in application["applied_changes"] if item["fact_change"]]}
    _atomic_write(exports / "audit.json", json_bytes(audit)); _atomic_write(exports / "audit.md", ("# Revision audit\n\n" + "\n".join(lines[2:])).encode()); return exports


def workflow_view(project_dir: Path | str) -> dict[str, Any]:
    """Build the ordinary-author state machine without mutating artifacts."""
    workflow = _workflow_workspace(project_dir)
    quick = _quick_root(workflow.root, workflow.version_id)
    view: dict[str, Any] = {"stage": "review_material", "next_action": "导入审查报告", "base_version_id": workflow.version_id}
    reports = quick / "reports"
    if not reports.is_dir() or not any(item.is_dir() for item in reports.iterdir()): return view
    report = _latest_dir(reports, "RP"); view["report_id"] = report.name
    atom_runs = quick / "atomization-runs"
    if not atom_runs.is_dir() or not any(item.is_dir() for item in atom_runs.iterdir()):
        return {**view, "stage": "atomization_prepare", "next_action": "生成报告原子化提示词"}
    atom = _latest_dir(atom_runs, "AR"); view["atomization_prompt"] = (atom / "prompt.md").read_text(encoding="utf-8")
    attempts = atom / "attempts"; valid_findings = None; latest_attempt = None
    if attempts.is_dir():
        attempt_dirs = sorted((p for p in attempts.iterdir() if p.is_dir() and not p.is_symlink()))
        if attempt_dirs:
            latest_attempt = attempt_dirs[-1]; attempt_record, _ = _read_json(latest_attempt / "record.json")
            view["atomization_attempt"] = {"attempt_id": latest_attempt.name, "valid": attempt_record["valid"], "errors": attempt_record["errors"], "raw": (latest_attempt / "response.json").read_text(encoding="utf-8", errors="replace"), "repair_prompt": (latest_attempt / "repair-prompt.md").read_text(encoding="utf-8") if (latest_attempt / "repair-prompt.md").is_file() else None}
            if (latest_attempt / "findings.json").is_file(): valid_findings = latest_attempt / "findings.json"
    if valid_findings is None:
        return {**view, "stage": "atomization_result", "next_action": "粘贴 AI 的原子化结果"}
    findings = current_quick_findings(workflow.root); view["findings"] = findings
    findings_sha256 = sha256_bytes(valid_findings.read_bytes())
    for path in reversed(_regular_files(quick / "no-revision-completions", "NC*.json")):
        completion, _ = _read_json(path)
        if completion.get("source_findings_sha256") == findings_sha256:
            export_dir = workflow.root / "exports" / completion["completion_id"]
            if (export_dir / "audit.json").is_file():
                return {**view, "stage": "complete", "next_action": "无修改闭环完成", "completion": completion, "export_path": str(export_dir)}
    if not findings:
        return {**view, "stage": "no_revision", "completion_kind": "no_findings", "next_action": "确认无可处理发现并完成"}
    if any(item["decision"] is None for item in findings):
        return {**view, "stage": "findings_confirm", "next_action": "确认要处理的发现"}
    if not any(item["decision"] == "accept" for item in findings):
        return {**view, "stage": "no_revision", "completion_kind": "all_declined", "next_action": "确认本轮不修改并完成"}
    revision_runs = quick / "revision-generation-runs"
    if not revision_runs.is_dir() or not any(item.is_dir() for item in revision_runs.iterdir()):
        return {**view, "stage": "revision_prepare", "next_action": "为已选问题生成修改提示词"}
    revision_run = _latest_dir(revision_runs, "RG"); view["revision_prompt"] = (revision_run / "prompt.md").read_text(encoding="utf-8")
    proposal = None; latest_revision_attempt = None; revision_attempts = revision_run / "attempts"
    if revision_attempts.is_dir():
        rows = sorted((p for p in revision_attempts.iterdir() if p.is_dir() and not p.is_symlink()))
        if rows:
            latest_revision_attempt = rows[-1]; attempt_record, _ = _read_json(latest_revision_attempt / "record.json")
            view["revision_attempt"] = {"attempt_id": latest_revision_attempt.name, "valid": attempt_record["valid"], "errors": attempt_record["errors"], "raw": (latest_revision_attempt / "response.json").read_text(encoding="utf-8", errors="replace"), "repair_prompt": (latest_revision_attempt / "repair-prompt.md").read_text(encoding="utf-8") if (latest_revision_attempt / "repair-prompt.md").is_file() else None}
            if (latest_revision_attempt / "revision-patch-proposal.json").is_file(): proposal = latest_revision_attempt / "revision-patch-proposal.json"
    if proposal is None: return {**view, "stage": "revision_result", "next_action": "粘贴 AI 的受约束修改提案"}
    hunks = revision_hunks(workflow.root); view["hunks"] = hunks
    if any(item["decision"] is None or item["decision"]["decision"] == "regenerate" for item in hunks):
        regenerations = [item["decision"] for item in hunks if item["decision"] and item["decision"]["decision"] == "regenerate"]
        if regenerations:
            ref = regenerations[-1].get("regeneration_prompt")
            if isinstance(ref, dict):
                view["regeneration_prompt"] = (proposal.parents[2] / "hunk-decisions" / ref["relative_path"]).read_text(encoding="utf-8")
        return {**view, "stage": "hunk_review", "next_action": "逐项审批修改"}
    applications = workflow.document_dir / "revision-applications"; application = None
    if applications.is_dir():
        rows = _regular_files(applications, "AP*.json")
        if rows: application, _ = _read_json(rows[-1])
    if application is None: return {**view, "stage": "apply_revision", "next_action": "生成不可变 V2"}
    view["application"] = application
    resolution_runs = workflow.document_dir / "resolution-runs"
    if not resolution_runs.is_dir() or not any(item.is_dir() for item in resolution_runs.iterdir()):
        return {**view, "stage": "resolution_prepare", "next_action": "用原审查标准复查 V2"}
    resolution_run = _latest_dir(resolution_runs, "RR"); view["resolution_prompt"] = (resolution_run / "prompt.md").read_text(encoding="utf-8")
    resolution_proposal = None; resolution_proposal_path = None; resolution_attempts = resolution_run / "attempts"
    if resolution_attempts.is_dir():
        rows = sorted((p for p in resolution_attempts.iterdir() if p.is_dir() and not p.is_symlink()))
        if rows:
            latest = rows[-1]; attempt_record, _ = _read_json(latest / "record.json")
            view["resolution_attempt"] = {"attempt_id": latest.name, "valid": attempt_record["valid"], "errors": attempt_record["errors"], "raw": (latest / "response.json").read_text(encoding="utf-8", errors="replace"), "repair_prompt": (latest / "repair-prompt.md").read_text(encoding="utf-8") if (latest / "repair-prompt.md").is_file() else None}
            if (latest / "resolution-proposals.json").is_file():
                resolution_proposal_path = latest / "resolution-proposals.json"
                resolution_proposal, _ = _read_json(resolution_proposal_path)
    if resolution_proposal is None: return {**view, "stage": "resolution_result", "next_action": "粘贴 AI 的复查结果"}
    proposal_sha256 = sha256_bytes(resolution_proposal_path.read_bytes())
    decisions: dict[str, dict[str, Any]] = {}
    for path in _regular_files(resolution_run / "human-decisions", "RD*.json"):
        item, _ = _read_json(path)
        if item.get("proposal_sha256") == proposal_sha256:
            decisions[item["finding_id"]] = item
    view["resolution_results"] = [{**item, "human_decision": decisions.get(item["finding_id"])} for item in resolution_proposal["results"]]
    if set(decisions) != set(resolution_run_record["finding_ids"] if (resolution_run_record := _read_json(resolution_run / "record.json")[0]) else []):
        return {**view, "stage": "resolution_confirm", "next_action": "确认复查结论"}
    export_dir = workflow.root / "exports" / application["application_id"]
    if not (export_dir / "audit.json").is_file(): return {**view, "stage": "export", "next_action": "导出文章与审计记录"}
    return {**view, "stage": "complete", "next_action": "闭环完成", "export_path": str(export_dir)}


def verify_revision_workflow(project_dir: Path | str) -> list[str]:
    """Recompute every revision artifact and human-decision binding."""
    root = workspace_paths(project_dir).root
    errors: list[str] = []
    proposals: dict[str, tuple[Path, dict[str, Any]]] = {}
    hunk_decisions: dict[str, tuple[Path, dict[str, Any]]] = {}

    marker_path = _integrity_marker(root)
    if marker_path.exists():
        try:
            marker, _ = _read_json(marker_path)
            if set(marker) != {"artifact_type", "schema_version", "enabled_at", "producer", "lifecycle"} or marker.get("artifact_type") != "revision-integrity-policy" or marker.get("schema_version") != 1 or marker.get("producer") != "argument-revision" or marker.get("lifecycle") != "immutable" or not isinstance(marker.get("enabled_at"), str):
                errors.append("revision-integrity.json: invalid integrity policy")
        except (OSError, WorkbenchError) as exc:
            errors.append(f"revision-integrity.json: {exc}")
    elif marker_path.is_symlink():
        errors.append("revision-integrity.json: integrity policy must be a regular file")
    elif _integrity_receipts_exist(root):
        errors.append("revision-integrity.json: integrity policy missing")

    for version_id in list_version_ids(root):
        _, version, _, manuscript = _source(root, version_id)
        quick = _quick_root(root, version_id)
        if not quick.exists():
            continue
        reports: dict[str, tuple[dict[str, Any], str]] = {}
        findings_by_hash: dict[str, dict[str, Any]] = {}
        decisions_by_hash: dict[str, tuple[Path, dict[str, Any]]] = {}
        actions: dict[str, tuple[Path, dict[str, Any]]] = {}
        referenced_actions: set[str] = set()

        report_dir = quick / "reports"
        if report_dir.is_dir():
            for item in sorted(path for path in report_dir.iterdir() if path.is_dir() and not path.is_symlink()):
                try:
                    record, _ = _read_json(item / "record.json")
                    data = (item / "report.md").read_bytes()
                    if sha256_bytes(data) != record.get("report", {}).get("sha256"):
                        errors.append(f"{item.name}: report hash mismatch")
                    if record.get("source_sha256") != version["source"]["sha256"]:
                        errors.append(f"{item.name}: source hash mismatch")
                    reports[item.name] = (record, data.decode("utf-8-sig"))
                except (OSError, WorkbenchError, UnicodeDecodeError) as exc:
                    errors.append(f"{item.name}: {exc}")

        atom_runs = quick / "atomization-runs"
        if atom_runs.is_dir():
            for run_dir in sorted(path for path in atom_runs.iterdir() if path.is_dir() and not path.is_symlink()):
                try:
                    run, _ = _read_json(run_dir / "record.json")
                    prompt = (run_dir / "prompt.md").read_bytes()
                    if sha256_bytes(prompt) != run.get("prompt", {}).get("sha256"):
                        errors.append(f"{run_dir.name}: prompt hash mismatch")
                    if run.get("source_sha256") != version["source"]["sha256"]:
                        errors.append(f"{run_dir.name}: atomization source mismatch")
                    report = reports.get(run.get("report_id"), ({}, ""))[1]
                    attempts = run_dir / "attempts"
                    if attempts.is_dir():
                        for attempt in sorted(path for path in attempts.iterdir() if path.is_dir() and not path.is_symlink()):
                            record, _ = _read_json(attempt / "record.json")
                            raw = (attempt / "response.json").read_bytes()
                            if sha256_bytes(raw) != record.get("response_sha256"):
                                errors.append(f"{run_dir.name}/{attempt.name}: response hash mismatch")
                            try:
                                value = parse_json_strict(raw)
                                fresh, normalized = _validate_atomization(value, run, manuscript, report)
                            except WorkbenchError as exc:
                                fresh, normalized = [str(exc)], []
                            if (not fresh) != bool(record.get("valid")) or fresh != record.get("errors"):
                                errors.append(f"{run_dir.name}/{attempt.name}: validation record mismatch")
                            findings_path = attempt / "findings.json"
                            if findings_path.is_file() and not findings_path.is_symlink():
                                derived, _ = _read_json(findings_path)
                                if fresh or derived.get("findings") != normalized:
                                    errors.append(f"{run_dir.name}/{attempt.name}: derived findings mismatch")
                                else:
                                    digest = sha256_bytes(findings_path.read_bytes())
                                    findings_by_hash[digest] = derived
                            elif record.get("valid"):
                                errors.append(f"{run_dir.name}/{attempt.name}: valid attempt is missing derived findings")
                except (OSError, WorkbenchError, KeyError, TypeError) as exc:
                    errors.append(f"{run_dir.name}: {exc}")

        for action_path in _regular_files(quick / "revision-actions", "QA*.json"):
            try:
                action, _ = _read_json(action_path)
                _verify_tracked(root, action_path, action, errors)
                expected = {"artifact_type", "schema_version", "action_id", "finding_id", "text", "provenance", "lifecycle"}
                if set(action) != expected or action.get("artifact_type") != "quick-revision-action" or action.get("schema_version") != 1 or action.get("lifecycle") != "append-only":
                    errors.append(f"{action_path.name}: invalid RevisionAction fields")
                if action.get("action_id") != action_path.stem or not isinstance(action.get("finding_id"), str) or not isinstance(action.get("text"), str) or not action.get("text", "").strip():
                    errors.append(f"{action_path.name}: invalid RevisionAction content")
                actions[str(action.get("action_id"))] = (action_path, action)
            except (OSError, WorkbenchError, KeyError, TypeError) as exc:
                errors.append(f"{action_path.name}: {exc}")

        allowed_corrections = {"claim_id", "manuscript_quote", "location_kind", "assertion", "criterion", "suggested_action", "evidence_level", "uncertainties"}
        for decision_path in _regular_files(quick / "finding-decisions", "FD*.json"):
            try:
                decision, raw = _read_json(decision_path)
                _verify_tracked(root, decision_path, decision, errors)
                digest = sha256_bytes(raw)
                decisions_by_hash[digest] = (decision_path, decision)
                expected = {"artifact_type", "schema_version", "decision_id", "finding_id", "decision", "reason", "corrections", "action_id", "source_findings_sha256", "provenance", "lifecycle"}
                if set(decision) != expected or decision.get("artifact_type") != "quick-finding-decision" or decision.get("schema_version") != 1 or decision.get("lifecycle") != "append-only":
                    errors.append(f"{decision_path.name}: invalid finding decision fields")
                if decision.get("decision_id") != decision_path.stem or decision.get("decision") not in FINDING_DECISIONS or not isinstance(decision.get("reason"), str) or not decision.get("reason", "").strip():
                    errors.append(f"{decision_path.name}: invalid finding decision content")
                source_hash = decision.get("source_findings_sha256")
                source_payload = findings_by_hash.get(str(source_hash))
                source_findings = {item.get("finding_id"): item for item in source_payload.get("findings", []) if isinstance(item, dict)} if source_payload else {}
                finding = source_findings.get(decision.get("finding_id"))
                if finding is None:
                    errors.append(f"{decision_path.name}: findings binding mismatch")
                corrections = decision.get("corrections")
                if not isinstance(corrections, dict) or not set(corrections).issubset(allowed_corrections):
                    errors.append(f"{decision_path.name}: invalid finding corrections")
                    corrections = {}
                if finding is not None:
                    candidate = dict(finding); candidate.update(corrections)
                    for field in ("claim_id", "assertion", "criterion", "suggested_action"):
                        if not isinstance(candidate.get(field), str) or not candidate[field].strip():
                            errors.append(f"{decision_path.name}: corrected {field} is invalid")
                    if candidate.get("location_kind") not in LOCATION_KINDS or candidate.get("evidence_level") not in EVIDENCE_LEVELS:
                        errors.append(f"{decision_path.name}: corrected location/evidence is invalid")
                    if not isinstance(candidate.get("uncertainties"), list) or any(not isinstance(item, str) for item in candidate.get("uncertainties", [])):
                        errors.append(f"{decision_path.name}: corrected uncertainties are invalid")
                    quote = candidate.get("manuscript_quote")
                    if candidate.get("location_kind") == "exact_quote" and (not isinstance(quote, str) or not quote or manuscript.count(quote) != 1):
                        errors.append(f"{decision_path.name}: corrected quote binding mismatch")
                    if candidate.get("location_kind") != "exact_quote" and quote is not None:
                        errors.append(f"{decision_path.name}: non-exact finding carries a quote")
                action_id = decision.get("action_id")
                if decision.get("decision") == "accept":
                    action_entry = actions.get(str(action_id))
                    if action_entry is None or action_entry[1].get("finding_id") != decision.get("finding_id"):
                        errors.append(f"{decision_path.name}: RevisionAction binding mismatch")
                    else:
                        referenced_actions.add(str(action_id))
                elif action_id is not None:
                    errors.append(f"{decision_path.name}: non-accept decision must not bind an action")
            except (OSError, WorkbenchError, KeyError, TypeError) as exc:
                errors.append(f"{decision_path.name}: {exc}")
        for action_id in set(actions) - referenced_actions:
            errors.append(f"{action_id}.json: RevisionAction is not bound to an accepted finding decision")

        revision_runs = quick / "revision-generation-runs"
        if revision_runs.is_dir():
            for run_dir in sorted(path for path in revision_runs.iterdir() if path.is_dir() and not path.is_symlink()):
                try:
                    run, _ = _read_json(run_dir / "record.json")
                    prompt = (run_dir / "prompt.md").read_bytes()
                    if sha256_bytes(prompt) != run.get("prompt", {}).get("sha256"):
                        errors.append(f"{run_dir.name}: prompt hash mismatch")
                    if run.get("source_sha256") != version["source"]["sha256"] or run.get("manuscript_version_id") != version_id:
                        errors.append(f"{run_dir.name}: revision source binding mismatch")
                    pair_rows = run.get("finding_action_bindings")
                    if pair_rows is None:
                        pair_rows = [{"finding_id": finding_id, "action_id": action_id} for finding_id, action_id in zip(run.get("finding_ids", []), run.get("action_ids", []))]
                    for pair in pair_rows if isinstance(pair_rows, list) else []:
                        if not isinstance(pair, dict) or actions.get(str(pair.get("action_id")), ({}, {}))[1].get("finding_id") != pair.get("finding_id"):
                            errors.append(f"{run_dir.name}: Finding–Action run binding mismatch")
                    local_proposals: dict[str, tuple[Path, dict[str, Any]]] = {}
                    attempts = run_dir / "attempts"
                    if attempts.is_dir():
                        for attempt in sorted(path for path in attempts.iterdir() if path.is_dir() and not path.is_symlink()):
                            record, _ = _read_json(attempt / "record.json")
                            raw = (attempt / "response.json").read_bytes()
                            if sha256_bytes(raw) != record.get("response_sha256"):
                                errors.append(f"{run_dir.name}/{attempt.name}: response hash mismatch")
                            try:
                                value = parse_json_strict(raw); fresh, changes = _validate_revision(value, run, manuscript)
                            except (WorkbenchError, KeyError, TypeError) as exc:
                                fresh, changes = [str(exc)], []
                            if (not fresh) != bool(record.get("valid")) or fresh != record.get("errors"):
                                errors.append(f"{run_dir.name}/{attempt.name}: validation record mismatch")
                            proposal_path = attempt / "revision-patch-proposal.json"
                            if proposal_path.is_file() and not proposal_path.is_symlink():
                                proposal, _ = _read_json(proposal_path)
                                if fresh or proposal.get("changes") != changes or proposal.get("generation_run_id") != run_dir.name:
                                    errors.append(f"{run_dir.name}/{attempt.name}: derived proposal mismatch")
                                digest = sha256_bytes(proposal_path.read_bytes())
                                local_proposals[digest] = (proposal_path, proposal)
                                proposals[digest] = (proposal_path, proposal)
                            elif record.get("valid"):
                                errors.append(f"{run_dir.name}/{attempt.name}: valid attempt is missing derived proposal")
                    for decision_path in _regular_files(run_dir / "hunk-decisions", "HD*.json"):
                        decision, raw = _read_json(decision_path)
                        _verify_tracked(root, decision_path, decision, errors)
                        decision_digest = sha256_bytes(raw)
                        hunk_decisions[decision_digest] = (decision_path, decision)
                        expected = {"artifact_type", "schema_version", "decision_id", "change_id", "decision", "reason", "edited_text", "fact_change", "verification_note", "regeneration_prompt", "proposal_sha256", "provenance", "lifecycle"}
                        if set(decision) != expected or decision.get("artifact_type") != "revision-hunk-decision" or decision.get("schema_version") != 1 or decision.get("lifecycle") != "append-only":
                            errors.append(f"{decision_path.name}: invalid hunk decision fields")
                        if decision.get("decision_id") != decision_path.stem or decision.get("decision") not in HUNK_DECISIONS or not isinstance(decision.get("reason"), str) or not decision.get("reason", "").strip():
                            errors.append(f"{decision_path.name}: invalid hunk decision content")
                        proposal_entry = local_proposals.get(str(decision.get("proposal_sha256")))
                        changes = {item.get("change_id"): item for item in proposal_entry[1].get("changes", []) if isinstance(item, dict)} if proposal_entry else {}
                        change = changes.get(decision.get("change_id"))
                        if change is None:
                            errors.append(f"{decision_path.name}: proposal/change binding mismatch")
                            continue
                        edited = decision.get("edited_text")
                        if decision.get("decision") == "edit" and not isinstance(edited, str):
                            errors.append(f"{decision_path.name}: edit decision requires edited_text")
                        if decision.get("decision") != "edit" and edited is not None:
                            errors.append(f"{decision_path.name}: edited_text is only valid for edit")
                        manual_signal = decision.get("decision") == "edit" and bool(re.search(r"\d|https?://|[“”‘’\"]", edited or "")) and edited != change.get("replacement_text")
                        expected_fact = bool(change.get("fact_change") or manual_signal)
                        expected_note = change.get("verification_note") or ("UNVERIFIED: human-edited text contains a number, quotation, or link." if manual_signal else "")
                        if decision.get("fact_change") != expected_fact or decision.get("verification_note") != expected_note:
                            errors.append(f"{decision_path.name}: hunk fact-change fields do not recompute")
                        regeneration = decision.get("regeneration_prompt")
                        if decision.get("decision") == "regenerate":
                            if not isinstance(regeneration, dict) or set(regeneration) != {"relative_path", "sha256"}:
                                errors.append(f"{decision_path.name}: regeneration prompt binding is invalid")
                            else:
                                regen_path = decision_path.parent / str(regeneration.get("relative_path"))
                                if not regen_path.is_file() or regen_path.is_symlink() or sha256_bytes(regen_path.read_bytes()) != regeneration.get("sha256"):
                                    errors.append(f"{decision_path.name}: regeneration prompt hash mismatch")
                        elif regeneration is not None:
                            errors.append(f"{decision_path.name}: non-regenerate decision carries a prompt")
                except (OSError, WorkbenchError, KeyError, TypeError) as exc:
                    errors.append(f"{run_dir.name}: {exc}")

        for completion_path in _regular_files(quick / "no-revision-completions", "NC*.json"):
            try:
                completion, _ = _read_json(completion_path)
                _verify_tracked(root, completion_path, completion, errors)
                expected = {"artifact_type", "schema_version", "completion_id", "manuscript_version_id", "source_sha256", "source_findings_sha256", "outcome", "reason", "finding_decision_ids", "finding_decision_sha256s", "provenance", "lifecycle"}
                if set(completion) != expected or completion.get("artifact_type") != "no-revision-completion" or completion.get("schema_version") != 1 or completion.get("lifecycle") != "immutable":
                    errors.append(f"{completion_path.name}: invalid no-revision completion fields")
                source_hash = completion.get("source_findings_sha256")
                findings_payload = findings_by_hash.get(str(source_hash))
                if completion.get("completion_id") != completion_path.stem or completion.get("manuscript_version_id") != version_id or completion.get("source_sha256") != version["source"]["sha256"] or findings_payload is None:
                    errors.append(f"{completion_path.name}: no-revision source binding mismatch")
                    continue
                ids = completion.get("finding_decision_ids")
                hashes = completion.get("finding_decision_sha256s")
                if not isinstance(ids, list) or not isinstance(hashes, list) or len(ids) != len(hashes):
                    errors.append(f"{completion_path.name}: no-revision decision bindings are invalid")
                    continue
                bound: list[dict[str, Any]] = []
                for decision_id, digest in zip(ids, hashes):
                    entry = decisions_by_hash.get(str(digest))
                    if entry is None or entry[1].get("decision_id") != decision_id or entry[1].get("source_findings_sha256") != source_hash:
                        errors.append(f"{completion_path.name}: finding decision hash mismatch")
                    else:
                        bound.append(entry[1])
                base_findings = findings_payload.get("findings", [])
                latest = {item.get("finding_id"): item for item in bound}
                expected_outcome = "no_findings" if not base_findings else "all_declined"
                if completion.get("outcome") != expected_outcome or (base_findings and ({item.get("finding_id") for item in base_findings} != set(latest) or any(item.get("decision") not in {"reject", "defer"} for item in latest.values()))):
                    errors.append(f"{completion_path.name}: no-revision outcome does not recompute")
                if not isinstance(completion.get("reason"), str) or not completion.get("reason", "").strip():
                    errors.append(f"{completion_path.name}: completion reason is invalid")
                corrected_rows = []
                for finding in base_findings:
                    decision = latest.get(finding.get("finding_id"))
                    corrected = dict(finding)
                    if decision and isinstance(decision.get("corrections"), dict):
                        corrected.update(decision["corrections"])
                    corrected_rows.append({**corrected, "decision": None if decision is None else decision["decision"], "decision_id": None if decision is None else decision["decision_id"], "action_id": None if decision is None else decision.get("action_id"), "source_findings_sha256": source_hash})
                export_dir = root / "exports" / str(completion.get("completion_id"))
                source_export = export_dir / f"{version_id}.md"
                if not source_export.is_file() or source_export.is_symlink() or source_export.read_bytes() != _source(root, version_id)[2]:
                    errors.append(f"{completion_path.name}: exported manuscript hash mismatch")
                checklist = ["# Revision checklist", "", f"- Manuscript: `{version_id}` `{completion['source_sha256']}`", f"- Outcome: `{completion['outcome']}`", f"- Reason: {completion['reason']}", "", "No revised document version was created.", ""]
                expected_checklist = "\n".join(checklist).encode("utf-8")
                expected_audit = {"artifact_type": "no-revision-audit-export", "schema_version": 1, "completion": completion, "atomic_findings": findings_payload, "atomic_findings_sha256": source_hash, "findings": corrected_rows, "finding_decisions": bound, "source_sha256": completion["source_sha256"]}
                exports = {"revision-checklist.md": expected_checklist, "audit.json": json_bytes(expected_audit), "audit.md": ("# No-revision audit\n\n" + "\n".join(checklist[2:])).encode("utf-8")}
                for name, expected_bytes in exports.items():
                    exported = export_dir / name
                    if not exported.is_file() or exported.is_symlink() or exported.read_bytes() != expected_bytes:
                        errors.append(f"{completion_path.name}: {name} hash mismatch")
            except (OSError, WorkbenchError, KeyError, TypeError) as exc:
                errors.append(f"{completion_path.name}: {exc}")

    applications_dir = root / "documents" / "D1" / "revision-applications"
    applications: dict[str, tuple[Path, dict[str, Any], bytes]] = {}
    for application_path in _regular_files(applications_dir, "AP*.json"):
        try:
            application, application_raw = _read_json(application_path)
            _verify_tracked(root, application_path, application, errors)
            applications[str(application.get("application_id"))] = (application_path, application, application_raw)
            expected_fields = {"artifact_type", "schema_version", "application_id", "generation_run_id", "proposal_sha256", "decision_fingerprint", "base_version_id", "base_source_sha256", "output_version_id", "output_source_sha256", "applied_changes", "rejected_changes", "decision_sha256s", "provenance", "lifecycle"}
            if set(application) != expected_fields or application.get("artifact_type") != "revision-application-record" or application.get("schema_version") != 1 or application.get("lifecycle") != "immutable" or application.get("application_id") != application_path.stem:
                errors.append(f"{application_path.name}: invalid application fields")
            proposal_entry = proposals.get(str(application.get("proposal_sha256")))
            if proposal_entry is None:
                errors.append(f"{application_path.name}: application proposal binding mismatch")
                continue
            proposal = proposal_entry[1]
            if application.get("generation_run_id") != proposal.get("generation_run_id") or application.get("base_version_id") != proposal.get("manuscript_version_id") or application.get("base_source_sha256") != proposal.get("source_sha256"):
                errors.append(f"{application_path.name}: application base binding mismatch")
            decision_hashes = application.get("decision_sha256s")
            changes = proposal.get("changes")
            if not isinstance(decision_hashes, list) or not isinstance(changes, list) or len(decision_hashes) != len(changes):
                errors.append(f"{application_path.name}: application decision hashes are incomplete")
                continue
            bound_decisions: list[dict[str, Any]] = []
            for change, digest in zip(changes, decision_hashes):
                entry = hunk_decisions.get(str(digest))
                if entry is None or entry[1].get("proposal_sha256") != application.get("proposal_sha256") or entry[1].get("change_id") != change.get("change_id") or entry[1].get("decision") == "regenerate":
                    errors.append(f"{application_path.name}: bound hunk decision mismatch")
                else:
                    bound_decisions.append(entry[1])
            if len(bound_decisions) != len(changes):
                continue
            expected_fingerprint = sha256_bytes((str(application["proposal_sha256"]) + "".join(decision_hashes)).encode())
            if application.get("decision_fingerprint") != expected_fingerprint:
                errors.append(f"{application_path.name}: decision fingerprint mismatch")
            _, base_version, _, base_text = _source(root, str(application["base_version_id"]))
            if base_version["source"]["sha256"] != application.get("base_source_sha256"):
                errors.append(f"{application_path.name}: base source hash mismatch")
            edits: list[tuple[int, int, str, dict[str, Any], dict[str, Any]]] = []
            rejected: list[str] = []
            for change, decision in zip(changes, bound_decisions):
                if decision["decision"] == "reject":
                    rejected.append(change["change_id"]); continue
                replacement = decision["edited_text"] if decision["decision"] == "edit" else change["replacement_text"]
                if change["original_quote"]:
                    start = base_text.index(change["original_quote"]); end = start + len(change["original_quote"])
                else:
                    anchor = change["insertion_anchor"]; start = base_text.index(anchor) + (len(anchor) if change["change_kind"] == "insert_after" else 0); end = start
                edits.append((start, end, replacement, change, decision))
            output = base_text
            for start, end, replacement, _, _ in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
                output = output[:start] + replacement + output[end:]
            output_bytes = output.encode("utf-8")
            _, _, stored_output, _ = _source(root, str(application["output_version_id"]))
            if stored_output != output_bytes or application.get("output_source_sha256") != sha256_bytes(output_bytes):
                errors.append(f"{application_path.name}: recomputed V2 bytes mismatch")
            expected_applied = [{"change_id": item[3]["change_id"], "decision_id": item[4]["decision_id"], "finding_ids": item[3]["finding_ids"], "action_ids": item[3]["action_ids"], "fact_change": item[4].get("fact_change", item[3]["fact_change"]), "verification_note": item[4].get("verification_note", item[3]["verification_note"])} for item in sorted(edits, key=lambda item: (item[0], item[1]))]
            if application.get("applied_changes") != expected_applied or application.get("rejected_changes") != rejected:
                errors.append(f"{application_path.name}: applied/rejected change sets mismatch")
        except (OSError, WorkbenchError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"{application_path.name}: {exc}")

    resolution_runs = root / "documents" / "D1" / "resolution-runs"
    if resolution_runs.is_dir():
        for run_dir in sorted(path for path in resolution_runs.iterdir() if path.is_dir() and not path.is_symlink()):
            try:
                run, _ = _read_json(run_dir / "record.json")
                prompt = (run_dir / "prompt.md").read_bytes()
                if sha256_bytes(prompt) != run.get("prompt", {}).get("sha256"):
                    errors.append(f"{run_dir.name}: resolution prompt hash mismatch")
                application_entry = applications.get(str(run.get("application_id")))
                if application_entry is None or sha256_bytes(application_entry[2]) != run.get("application_sha256"):
                    errors.append(f"{run_dir.name}: resolution application binding mismatch")
                _, version, _, manuscript = _source(root, str(run["manuscript_version_id"]))
                if run.get("source_sha256") != version["source"]["sha256"]:
                    errors.append(f"{run_dir.name}: resolution source binding mismatch")
                proposal_rows: dict[str, dict[str, Any]] = {}
                attempts = run_dir / "attempts"
                if attempts.is_dir():
                    for attempt in sorted(path for path in attempts.iterdir() if path.is_dir() and not path.is_symlink()):
                        attempt_record, _ = _read_json(attempt / "record.json")
                        raw = (attempt / "response.json").read_bytes()
                        if sha256_bytes(raw) != attempt_record.get("response_sha256"):
                            errors.append(f"{run_dir.name}/{attempt.name}: resolution response hash mismatch")
                        try:
                            value = parse_json_strict(raw); fresh, results = _validate_resolution(value, run, manuscript)
                        except (WorkbenchError, KeyError, TypeError) as exc:
                            fresh, results = [str(exc)], []
                        if (not fresh) != bool(attempt_record.get("valid")) or fresh != attempt_record.get("errors"):
                            errors.append(f"{run_dir.name}/{attempt.name}: resolution validation record mismatch")
                        proposal_path = attempt / "resolution-proposals.json"
                        if proposal_path.is_file() and not proposal_path.is_symlink():
                            proposal, _ = _read_json(proposal_path)
                            if fresh or proposal.get("results") != results or proposal.get("resolution_run_id") != run_dir.name:
                                errors.append(f"{run_dir.name}/{attempt.name}: derived resolution proposal mismatch")
                            proposal_rows[sha256_bytes(proposal_path.read_bytes())] = proposal
                        elif attempt_record.get("valid"):
                            errors.append(f"{run_dir.name}/{attempt.name}: valid resolution attempt is missing proposal")
                for decision_path in _regular_files(run_dir / "human-decisions", "RD*.json"):
                    decision, _ = _read_json(decision_path)
                    _verify_tracked(root, decision_path, decision, errors)
                    expected = {"artifact_type", "schema_version", "decision_id", "finding_id", "final_status", "reason", "proposal_sha256", "provenance", "lifecycle"}
                    if set(decision) != expected or decision.get("artifact_type") != "finding-resolution-decision" or decision.get("schema_version") != 1 or decision.get("lifecycle") != "append-only":
                        errors.append(f"{decision_path.name}: invalid resolution decision fields")
                    proposal = proposal_rows.get(str(decision.get("proposal_sha256")))
                    proposal_findings = {item.get("finding_id") for item in proposal.get("results", []) if isinstance(item, dict)} if proposal else set()
                    if decision.get("decision_id") != decision_path.stem or decision.get("finding_id") not in proposal_findings or decision.get("final_status") not in RESOLUTION_STATUSES or not isinstance(decision.get("reason"), str) or not decision.get("reason", "").strip():
                        errors.append(f"{decision_path.name}: invalid resolution decision content/binding")
            except (OSError, WorkbenchError, KeyError, TypeError) as exc:
                errors.append(f"{run_dir.name}: {exc}")
    return errors


__all__ = [
    "AttemptResult", "append_hunk_decision", "append_quick_finding_decision", "append_resolution_decision",
    "apply_approved_hunks", "collect_atomization_result", "collect_resolution_result", "collect_revision_result",
    "complete_without_revision", "current_quick_findings", "export_revision", "import_review_report", "prepare_atomization",
    "prepare_resolution_review", "prepare_revision_generation", "revision_hunks", "verify_revision_workflow", "workflow_view",
]
