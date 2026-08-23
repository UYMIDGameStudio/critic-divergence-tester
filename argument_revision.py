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
    decisions: dict[str, dict[str, Any]] = {}
    for path in _regular_files(_quick_root(workspace.root, workspace.version_id) / "finding-decisions", "FD*.json"):
        decision, _ = _read_json(path)
        decisions[str(decision.get("finding_id"))] = decision
    rows = []
    for finding in payload["findings"]:
        decision = decisions.get(finding["finding_id"])
        corrected = dict(finding)
        if decision and isinstance(decision.get("corrections"), dict):
            corrected.update(decision["corrections"])
        rows.append({**corrected, "decision": None if decision is None else decision["decision"], "decision_id": None if decision is None else decision["decision_id"], "action_id": None if decision is None else decision.get("action_id"), "source_findings_sha256": sha256_bytes(findings_path.read_bytes())})
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
    decisions = _quick_root(workspace.root, workspace.version_id) / "finding-decisions"
    decision_id = _next_id(decisions, "FD")
    action_id = None
    if decision == "accept":
        actions = _quick_root(workspace.root, workspace.version_id) / "revision-actions"
        action_id = _next_id(actions, "QA")
        action = {"artifact_type": "quick-revision-action", "schema_version": 1, "action_id": action_id, "finding_id": finding_id, "text": action_text or str(corrections.get("suggested_action", findings[finding_id]["suggested_action"])), "provenance": _provenance("human-confirmed", producer), "lifecycle": "append-only"}
        _write_new(actions / f"{action_id}.json", json_bytes(action))
    record = {"artifact_type": "quick-finding-decision", "schema_version": 1, "decision_id": decision_id, "finding_id": finding_id, "decision": decision, "reason": reason, "corrections": corrections, "action_id": action_id, "source_findings_sha256": sha256_bytes(findings_path.read_bytes()), "provenance": _provenance("human-confirmed", producer), "lifecycle": "append-only"}
    _write_new(decisions / f"{decision_id}.json", json_bytes(record))
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
    record = {"artifact_type": "revision-generation-run", "schema_version": 1, "generation_run_id": run_id, "manuscript_version_id": workspace.version_id, "source_sha256": version["source"]["sha256"], "finding_ids": [item["finding_id"] for item in selected], "action_ids": [item["action_id"] for item in selected], "finding_bindings": [{"finding_id": item["finding_id"], "manuscript_quote": item["manuscript_quote"], "location_kind": item["location_kind"]} for item in selected], "prompt": {"relative_path": "prompt.md", "sha256": sha256_bytes(prompt)}, "provenance": _provenance("deterministic", "argument-revision"), "lifecycle": "immutable"}
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
    _, proposal_path, proposal = _latest_proposal(project_dir); changes = {item["change_id"]: item for item in proposal["changes"]}; change_ids = set(changes)
    if change_id not in change_ids or decision not in HUNK_DECISIONS or not reason.strip(): raise WorkbenchError("valid change_id, decision, and reason are required")
    if decision == "edit" and edited_text is None: raise WorkbenchError("edited_text is required for edit")
    if decision != "edit" and edited_text is not None: raise WorkbenchError("edited_text is only allowed for edit")
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
    _write_new(directory / f"{decision_id}.json", json_bytes(record)); return decision_id


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
    _write_new(applications / f"{application_id}.json", json_bytes(record)); return record


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


def collect_resolution_result(project_dir: Path | str, response: str | bytes, *, run_id: str | None = None, producer: str = "manual-model-bridge") -> AttemptResult:
    root = workspace_paths(project_dir).root; runs = root / "documents" / "D1" / "resolution-runs"; run = runs / run_id if run_id else _latest_dir(runs, "RR"); record, _ = _read_json(run / "record.json")
    raw = response.encode() if isinstance(response, str) else response; attempts = run / "attempts"; attempt_id = _next_id(attempts, "attempt-", ""); target = attempts / attempt_id; errors: list[str] = []
    try: value = parse_json_strict(raw)
    except WorkbenchError as exc: value = None; errors.append(str(exc))
    if isinstance(value, dict):
        if set(value) != {"schema_version", "resolution_run_id", "manuscript_version_id", "source_sha256", "results"}: errors.append("response has unexpected or missing fields")
        for field in ("resolution_run_id", "manuscript_version_id", "source_sha256"):
            if value.get(field) != record.get(field): errors.append(f"{field} does not match resolution run")
        results = value.get("results")
        if not isinstance(results, list) or len(results) != len(record["finding_ids"]) or {item.get("finding_id") for item in results if isinstance(item, dict)} != set(record["finding_ids"]): errors.append("results must cover every selected finding exactly once")
        else:
            expected = {"finding_id", "proposed_status", "reason", "evidence_quotes", "uncertainties"}
            for item in results:
                if set(item) != expected or item.get("proposed_status") not in RESOLUTION_STATUSES or not isinstance(item.get("reason"), str): errors.append("resolution result item is invalid"); continue
                evidence_quotes = _string_list(item.get("evidence_quotes"), "evidence_quotes", errors); _string_list(item.get("uncertainties"), "uncertainties", errors)
                _, _, _, manuscript = _source(root, record["manuscript_version_id"])
                for quote in evidence_quotes:
                    if manuscript.count(quote) != 1: errors.append(f"resolution evidence quote for {item.get('finding_id')} must occur exactly once in V2")
    elif value is not None: errors.append("response must be an object")
    response_path = target / "response.json"; _write_new(response_path, raw); repair = None
    attempt = {"artifact_type": "resolution-result-attempt", "schema_version": 1, "attempt_id": attempt_id, "resolution_run_id": run.name, "valid": not errors, "errors": errors, "response_sha256": sha256_bytes(raw), "provenance": _provenance("model-derived", producer), "lifecycle": "immutable"}; _write_new(target / "record.json", json_bytes(attempt))
    if errors: repair = target / "repair-prompt.md"; _write_new(repair, _repair_prompt("resolution", errors, raw, (run / "prompt.md").read_bytes()))
    else:
        proposals = {"artifact_type": "finding-resolution-proposals", "schema_version": 1, "resolution_run_id": run.name, "results": value["results"], "notice": "Model-proposed only; no finding is resolved until a human decision is appended.", "provenance": _provenance("model-derived", producer), "lifecycle": "immutable"}; _write_new(target / "resolution-proposals.json", json_bytes(proposals))
    return AttemptResult(attempt_id, not errors, tuple(errors), response_path, repair)


def append_resolution_decision(project_dir: Path | str, finding_id: str, *, status: str, reason: str, producer: str = "local-workbench-ui") -> str:
    root = workspace_paths(project_dir).root; run = _latest_dir(root / "documents" / "D1" / "resolution-runs", "RR"); attempts = run / "attempts"; proposal_path = None
    for attempt in sorted((p for p in attempts.iterdir() if p.is_dir()), reverse=True):
        if (attempt / "resolution-proposals.json").is_file(): proposal_path = attempt / "resolution-proposals.json"; break
    if proposal_path is None: raise WorkbenchError("no valid resolution proposal exists")
    proposal, _ = _read_json(proposal_path); proposed = {item["finding_id"] for item in proposal["results"]}
    if finding_id not in proposed or status not in RESOLUTION_STATUSES or not reason.strip(): raise WorkbenchError("valid finding, status, and reason are required")
    directory = run / "human-decisions"; decision_id = _next_id(directory, "RD")
    value = {"artifact_type": "finding-resolution-decision", "schema_version": 1, "decision_id": decision_id, "finding_id": finding_id, "final_status": status, "reason": reason, "proposal_sha256": sha256_bytes(proposal_path.read_bytes()), "provenance": _provenance("human-confirmed", producer), "lifecycle": "append-only"}; _write_new(directory / f"{decision_id}.json", json_bytes(value)); return decision_id


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
        for run in runs.iterdir():
            directory = run / "human-decisions"
            for path in _regular_files(directory, "RD*.json"):
                item, _ = _read_json(path); resolutions[item["finding_id"]] = item
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
    if any(item["decision"] is None for item in findings) or not any(item["decision"] == "accept" for item in findings):
        return {**view, "stage": "findings_confirm", "next_action": "确认要处理的发现"}
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
    resolution_proposal = None; resolution_attempts = resolution_run / "attempts"
    if resolution_attempts.is_dir():
        rows = sorted((p for p in resolution_attempts.iterdir() if p.is_dir() and not p.is_symlink()))
        if rows:
            latest = rows[-1]; attempt_record, _ = _read_json(latest / "record.json")
            view["resolution_attempt"] = {"attempt_id": latest.name, "valid": attempt_record["valid"], "errors": attempt_record["errors"], "raw": (latest / "response.json").read_text(encoding="utf-8", errors="replace"), "repair_prompt": (latest / "repair-prompt.md").read_text(encoding="utf-8") if (latest / "repair-prompt.md").is_file() else None}
            if (latest / "resolution-proposals.json").is_file(): resolution_proposal, _ = _read_json(latest / "resolution-proposals.json")
    if resolution_proposal is None: return {**view, "stage": "resolution_result", "next_action": "粘贴 AI 的复查结果"}
    decisions: dict[str, dict[str, Any]] = {}
    for path in _regular_files(resolution_run / "human-decisions", "RD*.json"):
        item, _ = _read_json(path); decisions[item["finding_id"]] = item
    view["resolution_results"] = [{**item, "human_decision": decisions.get(item["finding_id"])} for item in resolution_proposal["results"]]
    if set(decisions) != set(resolution_run_record["finding_ids"] if (resolution_run_record := _read_json(resolution_run / "record.json")[0]) else []):
        return {**view, "stage": "resolution_confirm", "next_action": "确认复查结论"}
    export_dir = workflow.root / "exports" / application["application_id"]
    if not (export_dir / "audit.json").is_file(): return {**view, "stage": "export", "next_action": "导出文章与审计记录"}
    return {**view, "stage": "complete", "next_action": "闭环完成", "export_path": str(export_dir)}


def verify_revision_workflow(project_dir: Path | str) -> list[str]:
    """Recompute revision bindings and report tampering without changing files."""
    root = workspace_paths(project_dir).root; errors: list[str] = []
    for version_id in list_version_ids(root):
        workspace, version, _, manuscript = _source(root, version_id)
        quick = _quick_root(root, version_id)
        if not quick.exists(): continue
        reports: dict[str, tuple[dict[str, Any], str]] = {}
        report_dir = quick / "reports"
        if report_dir.is_dir():
            for item in sorted(p for p in report_dir.iterdir() if p.is_dir()):
                try:
                    record, _ = _read_json(item / "record.json"); data = (item / "report.md").read_bytes()
                    if sha256_bytes(data) != record.get("report", {}).get("sha256"): errors.append(f"{item.name}: report hash mismatch")
                    if record.get("source_sha256") != version["source"]["sha256"]: errors.append(f"{item.name}: source hash mismatch")
                    reports[item.name] = (record, data.decode("utf-8-sig"))
                except (OSError, WorkbenchError, UnicodeDecodeError) as exc: errors.append(f"{item.name}: {exc}")
        atom_runs = quick / "atomization-runs"
        if atom_runs.is_dir():
            for run_dir in sorted(p for p in atom_runs.iterdir() if p.is_dir()):
                try:
                    run, _ = _read_json(run_dir / "record.json"); prompt = (run_dir / "prompt.md").read_bytes()
                    if sha256_bytes(prompt) != run.get("prompt", {}).get("sha256"): errors.append(f"{run_dir.name}: prompt hash mismatch")
                    report = reports.get(run.get("report_id"), ({}, ""))[1]
                    attempts = run_dir / "attempts"
                    if attempts.is_dir():
                        for attempt in sorted(p for p in attempts.iterdir() if p.is_dir()):
                            record, _ = _read_json(attempt / "record.json"); raw = (attempt / "response.json").read_bytes()
                            if sha256_bytes(raw) != record.get("response_sha256"): errors.append(f"{run_dir.name}/{attempt.name}: response hash mismatch")
                            try: value = parse_json_strict(raw); fresh, _ = _validate_atomization(value, run, manuscript, report)
                            except WorkbenchError as exc: fresh = [str(exc)]
                            if (not fresh) != bool(record.get("valid")) or fresh != record.get("errors"): errors.append(f"{run_dir.name}/{attempt.name}: validation record mismatch")
                except (OSError, WorkbenchError) as exc: errors.append(f"{run_dir.name}: {exc}")
        revision_runs = quick / "revision-generation-runs"
        if revision_runs.is_dir():
            for run_dir in sorted(p for p in revision_runs.iterdir() if p.is_dir()):
                try:
                    run, _ = _read_json(run_dir / "record.json"); prompt = (run_dir / "prompt.md").read_bytes()
                    if sha256_bytes(prompt) != run.get("prompt", {}).get("sha256"): errors.append(f"{run_dir.name}: prompt hash mismatch")
                    attempts = run_dir / "attempts"
                    if attempts.is_dir():
                        for attempt in sorted(p for p in attempts.iterdir() if p.is_dir()):
                            record, _ = _read_json(attempt / "record.json"); raw = (attempt / "response.json").read_bytes()
                            if sha256_bytes(raw) != record.get("response_sha256"): errors.append(f"{run_dir.name}/{attempt.name}: response hash mismatch")
                            try: value = parse_json_strict(raw); fresh, changes = _validate_revision(value, run, manuscript)
                            except WorkbenchError as exc: fresh, changes = [str(exc)], []
                            if (not fresh) != bool(record.get("valid")) or fresh != record.get("errors"): errors.append(f"{run_dir.name}/{attempt.name}: validation record mismatch")
                            proposal_path = attempt / "revision-patch-proposal.json"
                            if proposal_path.is_file():
                                proposal, _ = _read_json(proposal_path)
                                if fresh or proposal.get("changes") != changes: errors.append(f"{run_dir.name}/{attempt.name}: derived proposal mismatch")
                    proposal_paths = sorted(run_dir.glob("attempts/attempt-*/revision-patch-proposal.json"))
                    if proposal_paths:
                        proposal_hashes = {sha256_bytes(path.read_bytes()) for path in proposal_paths}
                        for decision_path in _regular_files(run_dir / "hunk-decisions", "HD*.json"):
                            decision, _ = _read_json(decision_path)
                            if decision.get("proposal_sha256") not in proposal_hashes: errors.append(f"{decision_path.name}: proposal binding mismatch")
                except (OSError, WorkbenchError) as exc: errors.append(f"{run_dir.name}: {exc}")
    applications = root / "documents" / "D1" / "revision-applications"
    for path in _regular_files(applications, "AP*.json"):
        try:
            application, _ = _read_json(path); _, _, output, _ = _source(root, application["output_version_id"])
            if sha256_bytes(output) != application.get("output_source_sha256"): errors.append(f"{path.name}: output version hash mismatch")
        except (OSError, WorkbenchError, KeyError) as exc: errors.append(f"{path.name}: {exc}")
    return errors


__all__ = [
    "AttemptResult", "append_hunk_decision", "append_quick_finding_decision", "append_resolution_decision",
    "apply_approved_hunks", "collect_atomization_result", "collect_resolution_result", "collect_revision_result",
    "current_quick_findings", "export_revision", "import_review_report", "prepare_atomization",
    "prepare_resolution_review", "prepare_revision_generation", "revision_hunks", "verify_revision_workflow", "workflow_view",
]
