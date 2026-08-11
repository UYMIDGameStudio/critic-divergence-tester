"""Human triage for non-substantive Rule Review execution statuses.

These events are deliberately separate from argument Findings.  They preserve a
human acknowledgement or rejection of model-proposed routing/applicability
statuses without pretending that those statuses are defects in the manuscript.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argument_contracts import (
    REVIEW_EXECUTION_STATUSES,
    TRIAGE_ACTIONS,
    TRIAGE_DECISIONS,
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
    parse_json_strict,
    utc_now,
    verify_workspace,
    workspace_paths,
)


TRIAGE_ID_PATTERN = re.compile(r"ST([0-9]{4})\Z")
ATTEMPT_ID_PATTERN = re.compile(r"attempt-([0-9]{4})\Z")


@dataclass(frozen=True)
class StatusTriageItem:
    review_id: str
    attempt_id: str
    task_id: str
    target_claim: str
    check_id: str
    model_status: str
    reason: str
    decision: dict[str, Any] | None
    decision_bytes: bytes | None


def _attempt_triage_dir(review_root: Path, attempt_id: str) -> Path:
    return review_root / "status-triage" / attempt_id


def _decision_dir(review_root: Path, attempt_id: str) -> Path:
    return _attempt_triage_dir(review_root, attempt_id) / "decisions"


def _decision_entries(
    review_root: Path, attempt_id: str
) -> list[tuple[Path, dict[str, Any], bytes]]:
    directory = _decision_dir(review_root, attempt_id)
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise WorkbenchError("status triage path must be a regular non-symlink directory")
    entries: list[tuple[Path, dict[str, Any], bytes]] = []
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise WorkbenchError(f"unexpected status triage entry: {path.name}")
        if TRIAGE_ID_PATTERN.fullmatch(path.stem) is None or path.suffix != ".json":
            raise WorkbenchError(f"unexpected status triage entry: {path.name}")
        value, data = _read_json(path)
        entries.append((path, value, data))
    numbers = [int(TRIAGE_ID_PATTERN.fullmatch(path.stem).group(1)) for path, _, _ in entries]
    if numbers != list(range(1, len(entries) + 1)):
        raise WorkbenchError("status triage IDs must be continuous from ST0001")
    return entries


def _status_items(review: Any, attempt_id: str) -> tuple[list[dict[str, str]], bytes, bytes, bytes]:
    from argument_review import _read_review_inputs

    review_value, review_bytes, plan, _, _, _ = _read_review_inputs(review)
    attempt_dir = review.attempt_dir(attempt_id)
    attempt, attempt_bytes = _read_json(attempt_dir / "record.json")
    if attempt.get("validation", {}).get("status") != "valid":
        raise WorkbenchError(f"{review.review_id}/{attempt_id} is not a valid result")
    response_path = attempt_dir / "response.json"
    if response_path.is_symlink() or not response_path.is_file():
        raise WorkbenchError(f"{review.review_id}/{attempt_id} response is missing or unsafe")
    response_bytes = response_path.read_bytes()
    response = parse_json_strict(response_bytes)
    if not isinstance(response, dict):
        raise WorkbenchError("Rule Review result must be a JSON object")
    if int(response.get("schema_version", 0)) == 1:
        return [], review_bytes, attempt_bytes, response_bytes
    task_by_id = {task["id"]: task for task in plan["tasks"]}
    version_id = str(review_value["version_id"])
    items: list[dict[str, str]] = []
    for result in response["results"]:
        status = str(result["execution_status"])
        if status == "evaluated":
            continue
        task = task_by_id[str(result["task_id"])]
        items.append(
            {
                "review_id": review.review_id,
                "attempt_id": attempt_id,
                "task_id": str(result["task_id"]),
                "target_claim": f"{version_id}:{task['claim_id']}",
                "check_id": str(task["check_id"]),
                "model_status": status,
                "reason": str(result["reason"]),
            }
        )
    return items, review_bytes, attempt_bytes, response_bytes


def _latest_by_task(
    entries: list[tuple[Path, dict[str, Any], bytes]]
) -> dict[str, tuple[Path, dict[str, Any], bytes]]:
    latest: dict[str, tuple[Path, dict[str, Any], bytes]] = {}
    for entry in entries:
        latest[str(entry[1].get("task_id"))] = entry
    return latest


def triage_items_for_review(
    project_dir: Path | str, *, review_id: str | None = None
) -> tuple[Any, str, list[StatusTriageItem]]:
    from argument_review import selected_result_attempt, selected_rule_review

    review = selected_rule_review(project_dir, review_id)
    attempt_dir, attempt, _ = selected_result_attempt(review)
    attempt_id = str(attempt["attempt_id"])
    raw_items, _, _, _ = _status_items(review, attempt_id)
    latest = _latest_by_task(_decision_entries(review.root, attempt_id))
    items = [
        StatusTriageItem(
            **item,
            decision=latest.get(item["task_id"], (None, None, None))[1],
            decision_bytes=latest.get(item["task_id"], (None, None, None))[2],
        )
        for item in raw_items
    ]
    return review, attempt_id, items


def current_status_triage(project_dir: Path | str) -> list[StatusTriageItem]:
    from argument_review import list_rule_reviews, selected_result_attempt

    workspace = workspace_paths(project_dir)
    if not workspace.reviewed_payload.is_file() or workspace.reviewed_payload.is_symlink():
        raise WorkbenchError("Reviewed IR is required before status triage")
    current_ir_hash = sha256_bytes(workspace.reviewed_payload.read_bytes())
    collected: list[StatusTriageItem] = []
    matched = 0
    for review in list_rule_reviews(workspace.root):
        record, _ = _read_json(review.record)
        parents = {
            parent.get("role"): parent
            for parent in record.get("parents", [])
            if isinstance(parent, dict)
        }
        if parents.get("target-ir", {}).get("sha256") != current_ir_hash:
            continue
        try:
            attempt_dir, attempt, _ = selected_result_attempt(review)
        except WorkbenchError:
            continue
        matched += 1
        attempt_id = str(attempt["attempt_id"])
        raw_items, _, _, _ = _status_items(review, attempt_id)
        latest = _latest_by_task(_decision_entries(review.root, attempt_id))
        for item in raw_items:
            selected = latest.get(item["task_id"])
            collected.append(
                StatusTriageItem(
                    **item,
                    decision=selected[1] if selected else None,
                    decision_bytes=selected[2] if selected else None,
                )
            )
    if matched == 0:
        raise WorkbenchError(
            "project has no current Rule Review with valid results; run `ir review prepare/collect`"
        )
    return collected


def append_status_triage(
    project_dir: Path | str,
    *,
    task_id: str,
    decision: str,
    action: str,
    note: str,
    review_id: str | None = None,
    producer: str = "local-user",
) -> Path:
    if decision not in TRIAGE_DECISIONS:
        raise WorkbenchError(f"triage decision must be one of {TRIAGE_DECISIONS}")
    if action not in TRIAGE_ACTIONS:
        raise WorkbenchError(f"triage action must be one of {TRIAGE_ACTIONS}")
    if not note.strip():
        raise WorkbenchError("triage requires a non-empty human note")
    errors = verify_workspace(project_dir)
    if errors:
        raise WorkbenchError("Argument Workbench project is invalid: " + "; ".join(errors))
    review, attempt_id, items = triage_items_for_review(
        project_dir, review_id=review_id
    )
    normalized_task = task_id.strip().upper()
    selected = next((item for item in items if item.task_id == normalized_task), None)
    if selected is None:
        raise WorkbenchError(
            f"{normalized_task} is not a non-evaluated task in {review.review_id}/{attempt_id}"
        )
    raw_items, review_bytes, attempt_bytes, response_bytes = _status_items(
        review, attempt_id
    )
    assert any(item["task_id"] == normalized_task for item in raw_items)
    entries = _decision_entries(review.root, attempt_id)
    latest = _latest_by_task(entries).get(normalized_task)
    triage_id = f"ST{len(entries) + 1:04d}"
    parents = [
        _parent("review-run", "rule-review-run", review_bytes),
        _parent("result-attempt", "review-result-attempt", attempt_bytes),
        _parent("lens-result", "argument-check-results", response_bytes),
    ]
    supersedes: str | None = None
    if latest is not None:
        supersedes = sha256_bytes(latest[2])
        parents.append(
            _parent("previous-triage", "review-status-triage", latest[2])
        )
    value = {
        "schema_version": 1,
        "artifact": "review-status-triage",
        "artifact_id": f"{review.review_id}-{attempt_id}-{triage_id}",
        "lifecycle": "append-only",
        "provenance": _provenance("human-confirmed", utc_now(), producer),
        "parents": parents,
        "review_id": review.review_id,
        "attempt_id": attempt_id,
        "triage_id": triage_id,
        "task_id": normalized_task,
        "target_claim": selected.target_claim,
        "check_id": selected.check_id,
        "model_status": selected.model_status,
        "decision": decision,
        "action": action,
        "note": note.strip(),
        "supersedes": supersedes,
        "field_provenance": {
            "binding": {
                "origin": "deterministic",
                "source": "argument-check-plan",
            },
            "model_status": {
                "origin": "model-derived",
                "source": "review-result-attempt",
            },
            "decision": {
                "origin": "human-confirmed",
                "source": "local-user",
            },
        },
    }
    contract_errors = validate_artifact(value)
    if contract_errors:
        raise WorkbenchError("invalid status triage decision: " + "; ".join(contract_errors))
    directory = _decision_dir(review.root, attempt_id)
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"{triage_id}.json"
    _write_new(output, json_bytes(value))
    rebuild_status_triage(review, attempt_id)
    post_errors = verify_review_status_triage(project_dir)
    if post_errors:
        output.unlink(missing_ok=True)
        if _decision_entries(review.root, attempt_id):
            rebuild_status_triage(review, attempt_id)
        raise WorkbenchError("status triage postcondition failed: " + "; ".join(post_errors))
    return output


def render_status_triage(items: list[StatusTriageItem]) -> str:
    acknowledged = sum(
        1 for item in items if item.decision and item.decision["decision"] == "acknowledge"
    )
    rejected = sum(
        1 for item in items if item.decision and item.decision["decision"] == "reject"
    )
    lines = [
        "# Review Execution Status Triage",
        "",
        "These are model-proposed execution/routing states, not manuscript Findings.",
        f"Total: {len(items)} | open: {len(items) - acknowledged - rejected} | "
        f"acknowledged: {acknowledged} | rejected: {rejected}",
        "",
    ]
    for item in items:
        state = "OPEN" if item.decision is None else str(item.decision["decision"]).upper()
        lines.extend(
            [
                f"## {item.review_id}/{item.attempt_id}/{item.task_id} - {state}",
                "",
                f"- Target: `{item.target_claim}`",
                f"- Check: `{item.check_id}`",
                f"- Model status: `{item.model_status}` `[model-derived]`",
                f"- Reason: {item.reason} `[model-derived]`",
            ]
        )
        if item.decision is not None:
            lines.extend(
                [
                    f"- Human decision: `{item.decision['decision']}` `[human-confirmed]`",
                    f"- Follow-up action: `{item.decision['action']}` `[human-confirmed]`",
                    f"- Note: {item.decision['note']} `[human-confirmed]`",
                ]
            )
        lines.append("")
    return "\n".join(lines)


def _derive_status_triage(
    review: Any, attempt_id: str
) -> tuple[dict[str, Any], bytes, bytes]:
    review_value, _ = _read_json(review.record)
    raw_items, review_bytes, attempt_bytes, response_bytes = _status_items(
        review, attempt_id
    )
    entries = _decision_entries(review.root, attempt_id)
    latest = _latest_by_task(entries)
    items = [
        StatusTriageItem(
            **raw,
            decision=latest.get(raw["task_id"], (None, None, None))[1],
            decision_bytes=latest.get(raw["task_id"], (None, None, None))[2],
        )
        for raw in raw_items
    ]
    markdown_bytes = render_status_triage(items).encode("utf-8")
    summary = {
        "total": len(items),
        "open": sum(item.decision is None for item in items),
        "acknowledge": sum(
            item.decision is not None
            and item.decision["decision"] == "acknowledge"
            for item in items
        ),
        "reject": sum(
            item.decision is not None and item.decision["decision"] == "reject"
            for item in items
        ),
    }
    parents = [
        _parent("review-run", "rule-review-run", review_bytes),
        _parent("result-attempt", "review-result-attempt", attempt_bytes),
        _parent("lens-result", "argument-check-results", response_bytes),
    ]
    for index, (_, _, data) in enumerate(entries, 1):
        parents.append(
            _parent(f"triage-{index:04d}", "review-status-triage", data)
        )
    index = {
        "schema_version": 1,
        "artifact": "review-status-triage-index",
        "artifact_id": f"{review.review_id}-{attempt_id}-status-triage",
        "lifecycle": "derived-replaceable",
        "provenance": _provenance(
            "deterministic",
            str(review_value["provenance"]["created_at"]),
            "workbench-status-triage-v1",
        ),
        "parents": parents,
        "review_id": review.review_id,
        "attempt_id": attempt_id,
        "version_id": str(review_value["version_id"]),
        "summary": summary,
        "items": [
            {
                "task_id": item.task_id,
                "target_claim": item.target_claim,
                "check_id": item.check_id,
                "model_status": item.model_status,
                "reason": item.reason,
                "decision": item.decision["decision"] if item.decision else None,
                "action": item.decision["action"] if item.decision else None,
                "note": item.decision["note"] if item.decision else None,
                "triage_id": item.decision["triage_id"] if item.decision else None,
            }
            for item in items
        ],
        "view": {
            "relative_path": "status-triage.md",
            "sha256": sha256_bytes(markdown_bytes),
        },
        "field_provenance": {
            "model": {
                "origin": "model-derived",
                "source": "review-result-attempt",
            },
            "human": {
                "origin": "human-confirmed",
                "source": "review-status-triage",
            },
            "binding": {
                "origin": "deterministic",
                "source": "argument-check-plan",
            },
            "summary": {
                "origin": "deterministic",
                "source": "workbench-status-triage-v1",
            },
            "view": {
                "origin": "deterministic",
                "source": "workbench-status-triage-v1",
            },
        },
    }
    contract_errors = validate_artifact(index)
    if contract_errors:
        raise WorkbenchError(
            "internal status triage index error: " + "; ".join(contract_errors)
        )
    return index, json_bytes(index), markdown_bytes


def rebuild_status_triage(review: Any, attempt_id: str) -> tuple[Path, bool]:
    index, index_bytes, markdown_bytes = _derive_status_triage(review, attempt_id)
    root = _attempt_triage_dir(review.root, attempt_id)
    decisions = _decision_dir(review.root, attempt_id)
    root.mkdir(parents=True, exist_ok=True)
    decisions.mkdir(exist_ok=True)
    expected_files = {
        "index.json": index_bytes,
        str(index["view"]["relative_path"]): markdown_bytes,
    }
    changed = False
    for name, data in expected_files.items():
        path = root / name
        if path.exists() and path.is_symlink():
            raise WorkbenchError(f"status triage derived file must not be a symlink: {path}")
        if not path.exists() or path.read_bytes() != data:
            _atomic_write(path, data)
            changed = True
    return root / "status-triage.md", changed


def current_triage_indexes(
    project_dir: Path | str,
) -> list[tuple[StatusTriageItem, dict[str, Any], bytes]]:
    """Return one bound index tuple per current review that has status items."""
    from argument_review import list_rule_reviews, selected_result_attempt

    workspace = workspace_paths(project_dir)
    current_ir_hash = sha256_bytes(workspace.reviewed_payload.read_bytes())
    outputs: list[tuple[StatusTriageItem, dict[str, Any], bytes]] = []
    for review in list_rule_reviews(workspace.root):
        record, _ = _read_json(review.record)
        parents = {
            parent.get("role"): parent
            for parent in record.get("parents", [])
            if isinstance(parent, dict)
        }
        if parents.get("target-ir", {}).get("sha256") != current_ir_hash:
            continue
        try:
            _, attempt, _ = selected_result_attempt(review)
        except WorkbenchError:
            continue
        attempt_id = str(attempt["attempt_id"])
        raw_items, _, _, _ = _status_items(review, attempt_id)
        if not raw_items:
            continue
        index_path = _attempt_triage_dir(review.root, attempt_id) / "index.json"
        if not index_path.is_file() or index_path.is_symlink():
            raise WorkbenchError(
                f"{review.review_id}/{attempt_id} status triage index is missing"
            )
        index, index_bytes = _read_json(index_path)
        selected_item = current_status_triage(workspace.root)
        representative = next(
            item
            for item in selected_item
            if item.review_id == review.review_id and item.attempt_id == attempt_id
        )
        outputs.append((representative, index, index_bytes))
    return outputs


def rebuild_status_triages(project_dir: Path | str) -> tuple[list[Path], bool]:
    from argument_review import list_result_attempts, list_rule_reviews

    outputs: list[Path] = []
    changed = False
    for review in list_rule_reviews(project_dir):
        for _, attempt, _ in list_result_attempts(review):
            attempt_id = str(attempt.get("attempt_id"))
            if not _attempt_triage_dir(review.root, attempt_id).exists():
                continue
            output, attempt_changed = rebuild_status_triage(review, attempt_id)
            outputs.append(output)
            changed = changed or attempt_changed
    return outputs, changed


def verify_review_status_triage(project_dir: Path | str) -> list[str]:
    from argument_review import list_result_attempts, list_rule_reviews

    errors: list[str] = []
    try:
        reviews = list_rule_reviews(project_dir)
    except (OSError, WorkbenchError) as exc:
        return [str(exc)]
    for review in reviews:
        triage_root = review.root / "status-triage"
        attempts = {path.name for path, _, _ in list_result_attempts(review)}
        if not triage_root.exists():
            continue
        if triage_root.is_symlink() or not triage_root.is_dir():
            errors.append(f"{review.review_id}: status-triage must be a regular directory")
            continue
        for child in triage_root.iterdir():
            if child.name not in attempts or ATTEMPT_ID_PATTERN.fullmatch(child.name) is None:
                errors.append(
                    f"{review.review_id}: unexpected status-triage attempt: {child.name}"
                )
        for attempt_id in sorted(attempts):
            attempt_root = _attempt_triage_dir(review.root, attempt_id)
            if not attempt_root.exists():
                continue
            if attempt_root.is_symlink() or not attempt_root.is_dir():
                errors.append(
                    f"{review.review_id}/{attempt_id}: status triage must be a regular directory"
                )
                continue
            actual_names = {child.name for child in attempt_root.iterdir()}
            if actual_names != {"decisions", "index.json", "status-triage.md"}:
                errors.append(
                    f"{review.review_id}/{attempt_id}: status triage file set is not reproducible"
                )
            try:
                entries = _decision_entries(review.root, attempt_id)
                raw_items, review_bytes, attempt_bytes, response_bytes = _status_items(
                    review, attempt_id
                )
            except (OSError, WorkbenchError) as exc:
                errors.append(f"{review.review_id}/{attempt_id}: {exc}")
                continue
            item_by_task = {item["task_id"]: item for item in raw_items}
            latest: dict[str, tuple[dict[str, Any], bytes]] = {}
            for path, value, data in entries:
                prefix = f"{review.review_id}/{attempt_id}/{path.name}"
                errors.extend(f"{prefix}: {error}" for error in validate_artifact(value))
                triage_id = path.stem
                if value.get("triage_id") != triage_id:
                    errors.append(f"{prefix}: triage_id does not match filename")
                if value.get("artifact_id") != f"{review.review_id}-{attempt_id}-{triage_id}":
                    errors.append(f"{prefix}: artifact_id does not match location")
                if value.get("review_id") != review.review_id or value.get("attempt_id") != attempt_id:
                    errors.append(f"{prefix}: review/attempt binding does not match location")
                task_id = str(value.get("task_id"))
                target = item_by_task.get(task_id)
                if target is None:
                    errors.append(f"{prefix}: task is not a non-evaluated model result")
                else:
                    for field in ("target_claim", "check_id", "model_status"):
                        if value.get(field) != target[field]:
                            errors.append(f"{prefix}: {field} does not match model result")
                parents = {
                    parent.get("role"): parent
                    for parent in value.get("parents", [])
                    if isinstance(parent, dict)
                }
                expected_hashes = {
                    "review-run": sha256_bytes(review_bytes),
                    "result-attempt": sha256_bytes(attempt_bytes),
                    "lens-result": sha256_bytes(response_bytes),
                }
                for role, digest in expected_hashes.items():
                    if parents.get(role, {}).get("sha256") != digest:
                        errors.append(f"{prefix}: {role} parent hash is disconnected")
                previous = latest.get(task_id)
                expected_supersedes = sha256_bytes(previous[1]) if previous else None
                if value.get("supersedes") != expected_supersedes:
                    errors.append(f"{prefix}: supersedes must bind the prior decision for this task")
                if previous is None:
                    if "previous-triage" in parents:
                        errors.append(f"{prefix}: first task decision must not have previous-triage")
                elif parents.get("previous-triage", {}).get("sha256") != expected_supersedes:
                    errors.append(f"{prefix}: previous-triage parent hash is disconnected")
                latest[task_id] = (value, data)
            try:
                index, expected_index_bytes, expected_markdown_bytes = _derive_status_triage(
                    review, attempt_id
                )
                index_path = attempt_root / "index.json"
                markdown_path = attempt_root / str(index["view"]["relative_path"])
                if (
                    index_path.is_symlink()
                    or not index_path.is_file()
                    or index_path.read_bytes() != expected_index_bytes
                ):
                    errors.append(
                        f"{review.review_id}/{attempt_id}: status triage index is not reproducible"
                    )
                if (
                    markdown_path.is_symlink()
                    or not markdown_path.is_file()
                    or markdown_path.read_bytes() != expected_markdown_bytes
                ):
                    errors.append(
                        f"{review.review_id}/{attempt_id}: status triage view is not reproducible"
                    )
            except (OSError, WorkbenchError) as exc:
                errors.append(
                    f"{review.review_id}/{attempt_id}: cannot derive status triage: {exc}"
                )
    return errors
