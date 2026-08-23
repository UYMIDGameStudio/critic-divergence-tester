"""Human adjudication and revision planning for Argument Workbench Findings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from argument_contracts import (
    DECISIONS,
    REVISION_ACTION_TYPES,
    sha256_bytes,
    validate_artifact,
    validate_contract_bundle,
)
from argument_review import (
    REVIEW_ID_PATTERN,
    ReviewPaths,
    list_rule_reviews,
    selected_result_attempt,
    verify_reviews,
)
from argument_perspective import (
    PERSPECTIVE_REVIEW_ID_PATTERN,
    PerspectiveReviewPaths,
    list_perspective_reviews,
    selected_perspective_attempt,
    verify_perspective_reviews,
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
    utc_now,
    verify_workspace,
    workspace_paths,
)
from critic_workflow import decision_field_errors


ADJUDICATION_ID_PATTERN = re.compile(r"AD([0-9]{4})\Z")
ACTION_ID_PATTERN = re.compile(r"RA([0-9]{4})\Z")
FINDING_FILE_PATTERN = re.compile(r"F([0-9]{4})\.json\Z")


@dataclass(frozen=True)
class HumanReviewPaths:
    workspace: WorkspacePaths

    @property
    def adjudications_dir(self) -> Path:
        return self.workspace.version_dir / "adjudications"

    @property
    def actions_dir(self) -> Path:
        return self.workspace.version_dir / "revision-actions"

    @property
    def plan_dir(self) -> Path:
        return self.workspace.version_dir / "revision-plan"

    @property
    def plan_record(self) -> Path:
        return self.plan_dir / "record.json"

    @property
    def plan_markdown(self) -> Path:
        return self.plan_dir / "revision-plan.md"


@dataclass(frozen=True)
class FindingEntry:
    path: Path
    value: dict[str, Any]
    data: bytes
    review: ReviewPaths | PerspectiveReviewPaths
    attempt_id: str


def human_review_paths(project_dir: Path | str) -> HumanReviewPaths:
    return HumanReviewPaths(workspace_paths(project_dir))


def _all_finding_entries(project_dir: Path | str) -> list[FindingEntry]:
    entries: list[FindingEntry] = []
    reviews: list[ReviewPaths | PerspectiveReviewPaths] = [
        *list_rule_reviews(project_dir),
        *list_perspective_reviews(project_dir),
    ]
    for review in reviews:
        if not review.derived_dir.exists():
            continue
        for attempt_root in sorted(review.derived_dir.iterdir()):
            if attempt_root.is_symlink():
                raise WorkbenchError(
                    f"derived review attempt must not be a symlink: {attempt_root}"
                )
            if not attempt_root.is_dir():
                continue
            findings_dir = attempt_root / "findings"
            if not findings_dir.exists():
                continue
            if findings_dir.is_symlink() or not findings_dir.is_dir():
                raise WorkbenchError(
                    f"findings must be a regular non-symlink directory: {findings_dir}"
                )
            for path in sorted(findings_dir.glob("F[0-9][0-9][0-9][0-9].json")):
                value, data = _read_json(path)
                entries.append(
                    FindingEntry(path, value, data, review, attempt_root.name)
                )
    return entries


def current_finding_entries(
    project_dir: Path | str, *, review_id: str | None = None
) -> list[FindingEntry]:
    workspace = workspace_paths(project_dir)
    if not workspace.reviewed_payload.is_file() or workspace.reviewed_payload.is_symlink():
        raise WorkbenchError("Reviewed IR is required before adjudication")
    current_ir_sha256 = sha256_bytes(workspace.reviewed_payload.read_bytes())
    requested = review_id.upper() if review_id is not None else None
    if requested is not None and not (
        REVIEW_ID_PATTERN.fullmatch(requested)
        or PERSPECTIVE_REVIEW_ID_PATTERN.fullmatch(requested)
    ):
        raise WorkbenchError("review ID must be RV1..RVn or PV1..PVn")
    matched_reviews = 0
    entries: list[FindingEntry] = []
    reviews: list[ReviewPaths | PerspectiveReviewPaths] = [
        *list_rule_reviews(workspace),
        *list_perspective_reviews(workspace),
    ]
    for review in reviews:
        if requested is not None and review.review_id != requested:
            continue
        review_record, _ = _read_json(review.record)
        parent_by_role = {
            parent.get("role"): parent
            for parent in review_record.get("parents", [])
            if isinstance(parent, dict)
        }
        if parent_by_role.get("target-ir", {}).get("sha256") != current_ir_sha256:
            if requested is not None:
                raise WorkbenchError(
                    f"{requested} targets an older Reviewed IR; adjudicate the current review"
                )
            continue
        try:
            if isinstance(review, PerspectiveReviewPaths):
                attempt_dir, attempt, _ = selected_perspective_attempt(review)
            else:
                attempt_dir, attempt, _ = selected_result_attempt(review)
        except WorkbenchError:
            if requested is not None:
                raise
            continue
        matched_reviews += 1
        findings_dir = review.derived_attempt_dir(str(attempt["attempt_id"])) / "findings"
        if not findings_dir.is_dir() or findings_dir.is_symlink():
            raise WorkbenchError(
                f"current findings directory is missing or unsafe: {findings_dir}"
            )
        for path in sorted(findings_dir.glob("F[0-9][0-9][0-9][0-9].json")):
            value, data = _read_json(path)
            entries.append(
                FindingEntry(path, value, data, review, attempt_dir.name)
            )
    if requested is not None and matched_reviews == 0:
        raise WorkbenchError(f"no valid current result for Review {requested}")
    if requested is None and matched_reviews == 0:
        raise WorkbenchError(
            "project has no current Review Lens with valid results"
        )
    seen: set[str] = set()
    for entry in entries:
        finding_id = str(entry.value.get("finding_id"))
        if finding_id in seen:
            raise WorkbenchError(f"duplicate Finding ID in current review set: {finding_id}")
        seen.add(finding_id)
    return entries


def filter_finding_entries(
    findings: list[FindingEntry],
    *,
    verdict: str | None = None,
    claim: str | None = None,
    check_id: str | None = None,
) -> list[FindingEntry]:
    if verdict is not None and verdict not in {"fail", "uncertain"}:
        raise WorkbenchError("Finding verdict filter must be fail or uncertain")
    normalized_claim: str | None = None
    if claim is not None:
        normalized_claim = claim.strip().upper()
        if re.fullmatch(r"C[1-9][0-9]*", normalized_claim):
            normalized_claim = f"V1:{normalized_claim}"
        elif re.fullmatch(r"V[1-9][0-9]*:C[1-9][0-9]*", normalized_claim) is None:
            raise WorkbenchError("Claim filter must be C1 or V1:C1")
    normalized_check = check_id.strip() if check_id is not None else None
    if normalized_check == "":
        raise WorkbenchError("check filter must not be empty")
    selected: list[FindingEntry] = []
    for finding in findings:
        value = finding.value
        if verdict is not None and value.get("verdict") != verdict:
            continue
        if normalized_claim is not None and value.get("target_claim") != normalized_claim:
            continue
        finding_check = value.get("lens", {}).get("check_id")
        if normalized_check is not None and finding_check != normalized_check:
            continue
        selected.append(finding)
    return selected


def open_finding_entries(
    project_dir: Path | str,
    *,
    review_id: str | None = None,
    verdict: str | None = None,
    claim: str | None = None,
    check_id: str | None = None,
) -> list[FindingEntry]:
    """Return current Findings in scope that have no human decision."""
    paths = human_review_paths(project_dir)
    findings = filter_finding_entries(
        current_finding_entries(paths.workspace, review_id=review_id),
        verdict=verdict,
        claim=claim,
        check_id=check_id,
    )
    latest = latest_adjudications(list_adjudications(paths))
    return [
        finding
        for finding in findings
        if str(finding.value["finding_id"]) not in latest
    ]


def claim_bundle_status(
    project_dir: Path | str,
    *,
    review_id: str | None = None,
    verdict: str | None = None,
    claim: str | None = None,
    check_id: str | None = None,
) -> str:
    """Render undecided Findings as Claim-level confirmation bundles."""
    findings = open_finding_entries(
        project_dir,
        review_id=review_id,
        verdict=verdict,
        claim=claim,
        check_id=check_id,
    )
    claim_texts: dict[str, str] = {}
    grouped: dict[str, list[FindingEntry]] = {}
    for finding in findings:
        target = str(finding.value["target_claim"])
        grouped.setdefault(target, []).append(finding)
        if target in claim_texts:
            continue
        ir, _ = _read_json(finding.review.target_ir)
        local_id = target.split(":", 1)[1]
        node = next(
            (
                item
                for item in ir.get("claims", [])
                if isinstance(item, dict) and item.get("id") == local_id
            ),
            None,
        )
        claim_texts[target] = (
            str(node.get("text"))
            if isinstance(node, dict)
            else "Claim text unavailable"
        )
    lines = [
        "Claim-level Finding bundles",
        "",
        "One explicit batch confirmation still creates one append-only human decision per Finding.",
        "Model verdicts remain model-derived; no bundle is an automatic recommendation.",
        "",
    ]
    if not grouped:
        lines.extend(["No open Findings match the selected filters.", ""])
        return "\n".join(lines)
    for target, entries in grouped.items():
        fail = sum(1 for entry in entries if entry.value["verdict"] == "fail")
        uncertain = len(entries) - fail
        lines.extend(
            [
                f"## {target} - {len(entries)} open ({fail} FAIL / {uncertain} UNCERTAIN)",
                "",
                claim_texts[target],
                "",
            ]
        )
        for index, entry in enumerate(entries, 1):
            check_id_value = entry.value["lens"].get("check_id") or "perspective"
            lines.extend(
                [
                    f"{index}. {entry.value['finding_id']} - {check_id_value} [{entry.value['verdict']}]",
                    f"   {entry.value['reason']}",
                ]
            )
        lines.append("")
    return "\n".join(lines)


def list_adjudications(
    paths: HumanReviewPaths,
) -> list[tuple[Path, dict[str, Any], bytes]]:
    if not paths.adjudications_dir.exists():
        return []
    if paths.adjudications_dir.is_symlink() or not paths.adjudications_dir.is_dir():
        raise WorkbenchError("adjudications must be a regular non-symlink directory")
    entries: list[tuple[Path, dict[str, Any], bytes]] = []
    for path in sorted(paths.adjudications_dir.glob("AD[0-9][0-9][0-9][0-9].json")):
        value, data = _read_json(path)
        entries.append((path, value, data))
    return entries


def list_revision_actions(
    paths: HumanReviewPaths,
) -> list[tuple[Path, dict[str, Any], bytes]]:
    if not paths.actions_dir.exists():
        return []
    if paths.actions_dir.is_symlink() or not paths.actions_dir.is_dir():
        raise WorkbenchError("revision-actions must be a regular non-symlink directory")
    entries: list[tuple[Path, dict[str, Any], bytes]] = []
    for path in sorted(paths.actions_dir.glob("RA[0-9][0-9][0-9][0-9].json")):
        value, data = _read_json(path)
        entries.append((path, value, data))
    return entries


def latest_adjudications(
    entries: list[tuple[Path, dict[str, Any], bytes]],
) -> dict[str, tuple[Path, dict[str, Any], bytes]]:
    latest: dict[str, tuple[Path, dict[str, Any], bytes]] = {}
    for entry in entries:
        latest[str(entry[1].get("finding_id"))] = entry
    return latest


def _next_id(entries: list[tuple[Path, dict[str, Any], bytes]], prefix: str) -> str:
    return f"{prefix}{len(entries) + 1:04d}"


def _validate_decision_input(
    decision: str,
    reason: str,
    actions: list[tuple[str, str]],
) -> None:
    normalized_action = "; ".join(text for _, text in actions)
    errors = decision_field_errors(
        decision,
        reason,
        normalized_action,
        require_complete=True,
    )
    if decision != "accept" and actions:
        errors.append("reject/defer decisions must not create RevisionAction artifacts")
    for action_type, text in actions:
        if action_type not in REVISION_ACTION_TYPES:
            errors.append(f"unknown revision action type: {action_type}")
        if not text.strip():
            errors.append("revision action text must not be empty")
    if errors:
        raise WorkbenchError("invalid human decision: " + "; ".join(errors))


def append_finding_decision(
    project_dir: Path | str,
    finding_id: str,
    *,
    decision: str,
    reason: str,
    actions: list[tuple[str, str]],
    producer: str = "local-user",
) -> tuple[Path, list[Path]]:
    paths = human_review_paths(project_dir)
    verification_errors = verify_workspace(paths.workspace)
    if verification_errors:
        raise WorkbenchError(
            "Argument Workbench project is invalid: "
            + "; ".join(verification_errors)
        )
    current = {
        str(entry.value["finding_id"]): entry
        for entry in current_finding_entries(paths.workspace)
    }
    finding = current.get(finding_id)
    if finding is None:
        raise WorkbenchError(f"Finding is not current or does not exist: {finding_id}")
    decision = decision.casefold()
    _validate_decision_input(decision, reason, actions)
    adjudication_entries = list_adjudications(paths)
    action_entries = list_revision_actions(paths)
    previous = latest_adjudications(adjudication_entries).get(finding_id)
    adjudication_id = _next_id(adjudication_entries, "AD")
    created_at = utc_now()
    parents = [_parent("finding", "argument-finding", finding.data)]
    supersedes: str | None = None
    if previous is not None:
        supersedes = sha256_bytes(previous[2])
        parents.append(
            _parent(
                "previous-adjudication", "finding-adjudication", previous[2]
            )
        )
    adjudication = {
        "schema_version": 1,
        "artifact": "finding-adjudication",
        "artifact_id": adjudication_id,
        "lifecycle": "immutable",
        "provenance": _provenance("human-confirmed", created_at, producer),
        "parents": parents,
        "adjudication_id": adjudication_id,
        "finding_id": finding_id,
        "decision": decision,
        "reason": reason,
        "supersedes": supersedes,
    }
    adjudication_errors = validate_artifact(adjudication)
    if adjudication_errors:
        raise WorkbenchError(
            "internal adjudication contract error: "
            + "; ".join(adjudication_errors)
        )
    adjudication_bytes = json_bytes(adjudication)
    action_values: list[tuple[dict[str, Any], bytes]] = []
    for offset, (action_type, text) in enumerate(actions, 1):
        action_id = f"RA{len(action_entries) + offset:04d}"
        action = {
            "schema_version": 1,
            "artifact": "revision-action",
            "artifact_id": action_id,
            "lifecycle": "immutable",
            "provenance": _provenance("human-confirmed", created_at, producer),
            "parents": [
                _parent(
                    "adjudication", "finding-adjudication", adjudication_bytes
                )
            ],
            "action_id": action_id,
            "adjudication_id": adjudication_id,
            "target_claim": finding.value["target_claim"],
            "action_type": action_type,
            "text": text,
        }
        action_errors = validate_artifact(action)
        if action_errors:
            raise WorkbenchError(
                "internal RevisionAction contract error: "
                + "; ".join(action_errors)
            )
        action_values.append((action, json_bytes(action)))

    paths.adjudications_dir.mkdir(parents=True, exist_ok=True)
    paths.actions_dir.mkdir(parents=True, exist_ok=True)
    adjudication_path = paths.adjudications_dir / f"{adjudication_id}.json"
    action_paths = [
        paths.actions_dir / f"{value['action_id']}.json"
        for value, _ in action_values
    ]
    written: list[Path] = []
    try:
        _write_new(adjudication_path, adjudication_bytes)
        written.append(adjudication_path)
        for action_path, (_, action_bytes) in zip(action_paths, action_values):
            _write_new(action_path, action_bytes)
            written.append(action_path)
        rebuild_revision_plan(paths.workspace)
    except Exception:
        for path in reversed(written):
            path.unlink(missing_ok=True)
        try:
            if paths.plan_record.exists() or paths.plan_markdown.exists():
                rebuild_revision_plan(paths.workspace)
        except Exception:
            pass
        raise
    return adjudication_path, action_paths


def append_claim_bundle_decisions(
    project_dir: Path | str,
    *,
    claim: str,
    decision: str,
    reason: str,
    actions: list[tuple[str, str]],
    confirm_count: int,
    review_id: str | None = None,
    verdict: str | None = None,
    check_id: str | None = None,
    producer: str = "local-user",
) -> list[tuple[Path, list[Path]]]:
    """Apply one explicit human choice to the exact open Findings of one Claim.

    The count is an optimistic-lock guard: if review results or earlier decisions
    changed after the user inspected the bundle, nothing is written. Each Finding
    still receives its own immutable adjudication and, for acceptance, its own
    RevisionAction artifact(s).
    """
    if not isinstance(confirm_count, int) or isinstance(confirm_count, bool):
        raise WorkbenchError("confirm_count must be an integer")
    if confirm_count < 1:
        raise WorkbenchError("confirm_count must be at least 1")
    normalized_claim = claim.strip().upper()
    if re.fullmatch(r"C[1-9][0-9]*", normalized_claim):
        normalized_claim = f"V1:{normalized_claim}"
    elif re.fullmatch(r"V[1-9][0-9]*:C[1-9][0-9]*", normalized_claim) is None:
        raise WorkbenchError("Claim bundle requires one Claim such as C1 or V1:C1")
    _validate_decision_input(decision.casefold(), reason, actions)
    findings = open_finding_entries(
        project_dir,
        review_id=review_id,
        verdict=verdict,
        claim=normalized_claim,
        check_id=check_id,
    )
    if len(findings) != confirm_count:
        raise WorkbenchError(
            "Claim bundle changed: "
            f"confirmation expected {confirm_count} open Findings but current scope has {len(findings)}; "
            "inspect the bundle again before deciding"
        )
    created: list[tuple[Path, list[Path]]] = []
    try:
        for finding in findings:
            created.append(
                append_finding_decision(
                    project_dir,
                    str(finding.value["finding_id"]),
                    decision=decision,
                    reason=reason,
                    actions=actions,
                    producer=producer,
                )
            )
    except Exception as exc:
        if created:
            raise WorkbenchError(
                f"Claim bundle stopped after {len(created)} append-only decisions; "
                "progress was preserved and the bundle must be inspected again"
            ) from exc
        raise
    return created


def _derive_revision_plan(
    project_dir: Path | str,
) -> tuple[dict[str, Any], bytes, str, list[tuple[object, bytes]]]:
    paths = human_review_paths(project_dir)
    findings = current_finding_entries(paths.workspace)
    adjudications = list_adjudications(paths)
    actions = list_revision_actions(paths)
    latest = latest_adjudications(adjudications)
    actions_by_adjudication: dict[str, list[tuple[Path, dict[str, Any], bytes]]] = {}
    for action_entry in actions:
        actions_by_adjudication.setdefault(
            str(action_entry[1].get("adjudication_id")), []
        ).append(action_entry)
    plan_items: list[dict[str, Any]] = []
    parents: list[dict[str, str]] = []
    entries: list[tuple[object, bytes]] = []
    latest_created_at: str | None = None
    for index, finding in enumerate(findings, 1):
        parents.append(
            _parent(f"finding-{index:04d}", "argument-finding", finding.data)
        )
        entries.append((finding.value, finding.data))
        adjudication_entry = latest.get(str(finding.value["finding_id"]))
        decision: str | None = None
        human_reason = ""
        adjudication_id: str | None = None
        active_actions: list[dict[str, Any]] = []
        decision_provenance = {
            "origin": "deterministic",
            "source": "no-adjudication",
        }
        action_provenance = {
            "origin": "deterministic",
            "source": "no-active-revision-action",
        }
        if adjudication_entry is not None:
            _, adjudication, adjudication_bytes = adjudication_entry
            decision = str(adjudication["decision"])
            human_reason = str(adjudication["reason"])
            adjudication_id = str(adjudication["adjudication_id"])
            parents.append(
                _parent(
                    f"adjudication-{index:04d}",
                    "finding-adjudication",
                    adjudication_bytes,
                )
            )
            entries.append((adjudication, adjudication_bytes))
            latest_created_at = str(adjudication["provenance"]["created_at"])
            decision_provenance = {
                "origin": "human-confirmed",
                "source": adjudication_id,
            }
            if decision == "accept":
                for action_index, (_, action, action_bytes) in enumerate(
                    actions_by_adjudication.get(adjudication_id, []), 1
                ):
                    parents.append(
                        _parent(
                            f"action-{index:04d}-{action_index:04d}",
                            "revision-action",
                            action_bytes,
                        )
                    )
                    entries.append((action, action_bytes))
                    active_actions.append(
                        {
                            "action_id": action["action_id"],
                            "action_type": action["action_type"],
                            "text": action["text"],
                            "sha256": sha256_bytes(action_bytes),
                        }
                    )
                action_provenance = {
                    "origin": "human-confirmed",
                    "source": adjudication_id,
                }
        plan_items.append(
            {
                "finding_id": finding.value["finding_id"],
                "target_claim": finding.value["target_claim"],
                "lens": finding.value["lens"],
                "verdict": finding.value["verdict"],
                "model_reason": finding.value["reason"],
                "decision": decision,
                "human_reason": human_reason,
                "adjudication_id": adjudication_id,
                "actions": active_actions,
                "field_provenance": {
                    "model": {
                        "origin": "model-derived",
                        "source": finding.value["finding_id"],
                    },
                    "decision": decision_provenance,
                    "actions": action_provenance,
                },
            }
        )
    summary = {
        "accept": sum(1 for item in plan_items if item["decision"] == "accept"),
        "reject": sum(1 for item in plan_items if item["decision"] == "reject"),
        "defer": sum(1 for item in plan_items if item["decision"] == "defer"),
        "open": sum(1 for item in plan_items if item["decision"] is None),
    }
    markdown = render_revision_plan(plan_items, summary)
    markdown_bytes = markdown.encode("utf-8")
    version, _ = _read_json(paths.workspace.version)
    if latest_created_at is None:
        latest_created_at = (
            str(findings[-1].value["provenance"]["created_at"])
            if findings
            else str(version["provenance"]["created_at"])
        )
    record = {
        "schema_version": 1,
        "artifact": "revision-plan-record",
        "artifact_id": f"{version['version_id']}-revision-plan",
        "lifecycle": "derived-replaceable",
        "provenance": _provenance(
            "deterministic", latest_created_at, "workbench-revision-plan-v1"
        ),
        "parents": parents,
        "project_id": version["project_id"],
        "document_id": version["document_id"],
        "version_id": version["version_id"],
        "payload": {
            "relative_path": "revision-plan.md",
            "sha256": sha256_bytes(markdown_bytes),
        },
        "summary": summary,
        "items": plan_items,
        "field_provenance": {
            "summary": {
                "origin": "deterministic",
                "source": "workbench-revision-plan-v1",
            },
            "payload": {
                "origin": "deterministic",
                "source": "workbench-revision-plan-v1",
            },
        },
    }
    record_errors = validate_artifact(record)
    if record_errors:
        raise WorkbenchError(
            "internal revision plan contract error: " + "; ".join(record_errors)
        )
    record_bytes = json_bytes(record)
    entries.append((record, record_bytes))
    return record, record_bytes, markdown, entries


def render_revision_plan(
    items: list[dict[str, Any]], summary: dict[str, int]
) -> str:
    action_groups: list[dict[str, Any]] = []
    action_group_index: dict[tuple[str, str, str], dict[str, Any]] = {}
    action_group_by_instance: dict[tuple[str, str], str] = {}
    for item in items:
        if item["decision"] != "accept":
            continue
        for action in item["actions"]:
            key = (
                str(item["target_claim"]),
                str(action["action_type"]),
                str(action["text"]),
            )
            group = action_group_index.get(key)
            if group is None:
                group = {
                    "group_id": f"AG{len(action_groups) + 1:04d}",
                    "target_claim": key[0],
                    "action_type": key[1],
                    "text": key[2],
                    "finding_ids": [],
                    "action_ids": [],
                }
                action_groups.append(group)
                action_group_index[key] = group
            finding_id = str(item["finding_id"])
            action_id = str(action["action_id"])
            if finding_id not in group["finding_ids"]:
                group["finding_ids"].append(finding_id)
            group["action_ids"].append(action_id)
            action_group_by_instance[(finding_id, action_id)] = str(
                group["group_id"]
            )
    lines = [
        "# Revision Plan",
        "",
        "Human decisions are authoritative. Model verdicts remain model-derived and are shown only with their originating Finding.",
        "",
        "## Decision Summary",
        "",
        f"- Accepted: {summary['accept']}",
        f"- Rejected: {summary['reject']}",
        f"- Deferred: {summary['defer']}",
        f"- Open: {summary['open']}",
        "",
        "## Consolidated Revision Actions",
        "",
    ]
    if not action_groups:
        lines.extend(["None.", ""])
    else:
        lines.extend(
            [
                "Identical human-confirmed actions are shown once for readability; every underlying RevisionAction remains a separate immutable artifact.",
                "",
            ]
        )
        for group in action_groups:
            lines.extend(
                [
                    f"### {group['group_id']} - {group['target_claim']} - `{group['action_type']}`",
                    "",
                    str(group["text"]),
                    "",
                    "- Covers Findings: " + ", ".join(group["finding_ids"]),
                    "- RevisionAction artifacts: "
                    + ", ".join(group["action_ids"]),
                    "",
                ]
            )
    accepted = [item for item in items if item["decision"] == "accept"]
    lines.extend(["## Accepted Findings and Revision Actions", ""])
    if not accepted:
        lines.extend(["None.", ""])
    else:
        accepted_by_claim: dict[str, list[dict[str, Any]]] = {}
        for item in accepted:
            accepted_by_claim.setdefault(str(item["target_claim"]), []).append(
                item
            )
        for target_claim, claim_items in accepted_by_claim.items():
            lines.extend(
                [
                    f"### {target_claim} - {len(claim_items)} accepted",
                    "",
                    "- Human-confirmed reasons:",
                ]
            )
            reasons: dict[str, list[str]] = {}
            for item in claim_items:
                reasons.setdefault(str(item["human_reason"]), []).append(
                    str(item["finding_id"])
                )
            for reason, finding_ids in reasons.items():
                lines.append(
                    "  - "
                    + (reason or "none")
                    + " ("
                    + ", ".join(finding_ids)
                    + ") `[human-confirmed]`"
                )
            lines.append("- Model-derived Finding trace:")
            claim_group_ids: list[str] = []
            for item in claim_items:
                check_id = item["lens"].get("check_id")
                lens_label = str(item["lens"]["id"])
                if check_id is not None:
                    lens_label += f" / {check_id}"
                lines.append(
                    f"  - {item['finding_id']} ({item['adjudication_id']}) - "
                    f"`{lens_label}` / `{item['verdict']}`: "
                    f"{item['model_reason']} `[model-derived]`"
                )
                for action in item["actions"]:
                    group_id = action_group_by_instance[
                        (str(item["finding_id"]), str(action["action_id"]))
                    ]
                    if group_id not in claim_group_ids:
                        claim_group_ids.append(group_id)
            lines.append(
                "- Revision action groups: "
                + ", ".join(f"`{group_id}`" for group_id in claim_group_ids)
            )
            lines.append("")

    sections = (
        ("defer", "Deferred Findings"),
        ("reject", "Rejected Findings"),
        (None, "Open Findings"),
    )
    for decision, heading in sections:
        selected = [item for item in items if item["decision"] == decision]
        lines.extend([f"## {heading}", ""])
        if not selected:
            lines.extend(["None.", ""])
            continue
        for item in selected:
            check_id = item["lens"].get("check_id")
            lens_label = str(item["lens"]["id"])
            if check_id is not None:
                lens_label += f" / {check_id}"
            lines.extend(
                [
                    f"### {item['finding_id']} - {item['target_claim']}",
                    "",
                    f"- Review Lens: `{lens_label}`",
                    f"- Model verdict: `{item['verdict']}` `[model-derived]`",
                    f"- Model reason: {item['model_reason']} `[model-derived]`",
                    f"- Human decision: `{item['decision'] or 'open'}` "
                    + ("`[human-confirmed]`" if item["decision"] else "`[deterministic absence]`"),
                    f"- Human reason: {item['human_reason'] or 'none'}",
                ]
            )
            if item["actions"]:
                group_ids = [
                    action_group_by_instance[
                        (str(item["finding_id"]), str(action["action_id"]))
                    ]
                    for action in item["actions"]
                ]
                lines.append(
                    "- Revision action groups: "
                    + ", ".join(f"`{group_id}`" for group_id in group_ids)
                )
            else:
                lines.append("- Revision actions: none")
            lines.append("")
    return "\n".join(lines)


def rebuild_revision_plan(project_dir: Path | str) -> tuple[Path, bool]:
    paths = human_review_paths(project_dir)
    _, record_bytes, markdown, _ = _derive_revision_plan(paths.workspace)
    markdown_bytes = markdown.encode("utf-8")
    changed = False
    paths.plan_dir.mkdir(parents=True, exist_ok=True)
    for path, data in (
        (paths.plan_markdown, markdown_bytes),
        (paths.plan_record, record_bytes),
    ):
        if path.exists() and path.is_symlink():
            raise WorkbenchError(f"revision plan artifact must not be a symlink: {path}")
        if not path.exists() or path.read_bytes() != data:
            _atomic_write(path, data)
            changed = True
    return paths.plan_markdown, changed


def rebuild_adjudication_cache(project_dir: Path | str) -> tuple[list[Path], bool]:
    paths = human_review_paths(project_dir)
    exists = any(
        path.exists()
        for path in (
            paths.adjudications_dir,
            paths.actions_dir,
            paths.plan_dir,
        )
    )
    if not exists:
        return [], False
    output, changed = rebuild_revision_plan(paths.workspace)
    return [output], changed


def adjudication_status(
    project_dir: Path | str,
    *,
    review_id: str | None = None,
    verdict: str | None = None,
    claim: str | None = None,
    check_id: str | None = None,
    summary_only: bool = False,
) -> str:
    paths = human_review_paths(project_dir)
    findings = filter_finding_entries(
        current_finding_entries(paths.workspace, review_id=review_id),
        verdict=verdict,
        claim=claim,
        check_id=check_id,
    )
    latest = latest_adjudications(list_adjudications(paths))
    filters = [
        value
        for value in (
            f"verdict={verdict}" if verdict is not None else None,
            f"claim={claim}" if claim is not None else None,
            f"check={check_id}" if check_id is not None else None,
        )
        if value is not None
    ]
    heading = "Human Adjudication"
    if filters:
        heading += " (" + ", ".join(filters) + ")"
    lines = [heading, ""]
    counts = {"accept": 0, "reject": 0, "defer": 0, "open": 0}
    model_counts = {"fail": 0, "uncertain": 0}
    open_by_check: dict[str, dict[str, int]] = {}
    open_by_claim: dict[str, dict[str, int]] = {}
    for finding in findings:
        finding_id = str(finding.value["finding_id"])
        model_verdict = str(finding.value["verdict"])
        model_counts[model_verdict] += 1
        adjudication = latest.get(finding_id)
        decision = (
            str(adjudication[1]["decision"]) if adjudication is not None else "open"
        )
        counts[decision] += 1
        if adjudication is None:
            finding_check = str(
                finding.value["lens"].get("check_id") or "perspective"
            )
            target_claim = str(finding.value["target_claim"])
            for registry, key in (
                (open_by_check, finding_check),
                (open_by_claim, target_claim),
            ):
                bucket = registry.setdefault(key, {"fail": 0, "uncertain": 0})
                bucket[model_verdict] += 1
        if summary_only:
            continue
        lines.extend(
            [
                f"{finding_id} - {finding.value['target_claim']}",
                f"  Lens: {finding.value['lens']['id']} / {finding.value['lens'].get('check_id') or 'perspective'}",
                f"  Finding: {finding.value['reason']}",
                f"  Human decision: {decision}",
            ]
        )
        if adjudication is not None and adjudication[1]["reason"]:
            lines.append(f"  Human reason: {adjudication[1]['reason']}")
        lines.append("")
    if summary_only:
        lines.extend(
            [
                f"Model verdicts in scope: {model_counts['fail']} FAIL, "
                f"{model_counts['uncertain']} UNCERTAIN",
                "",
                "Open queue by check (FAIL / UNCERTAIN):",
            ]
        )
        if open_by_check:
            for finding_check, bucket in sorted(
                open_by_check.items(),
                key=lambda item: (
                    -(item[1]["fail"] + item[1]["uncertain"]),
                    item[0],
                ),
            ):
                lines.append(
                    f"  {finding_check}: {bucket['fail']} / {bucket['uncertain']}"
                )
        else:
            lines.append("  —")
        lines.extend(["", "Open queue by Claim (FAIL / UNCERTAIN):"])
        if open_by_claim:
            for target_claim, bucket in sorted(open_by_claim.items()):
                lines.append(
                    f"  {target_claim}: {bucket['fail']} / {bucket['uncertain']}"
                )
        else:
            lines.append("  —")
        lines.append("")
    lines.extend(
        [
            f"Summary: {counts['accept']} accepted, {counts['reject']} rejected, "
            f"{counts['defer']} deferred, {counts['open']} open",
            "",
        ]
    )
    return "\n".join(lines)


def _ask_required(
    input_fn: Callable[[str], str], output_fn: Callable[[str], None], prompt: str
) -> str:
    while True:
        value = input_fn(prompt).strip()
        if value:
            return value
        output_fn("A value is required.")


def run_adjudicator(
    project_dir: Path | str,
    *,
    review_id: str | None,
    review_all: bool,
    view_only: bool,
    verdict: str | None = None,
    claim: str | None = None,
    check_id: str | None = None,
    summary_only: bool = False,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    paths = human_review_paths(project_dir)
    verification_errors = verify_workspace(paths.workspace)
    if verification_errors:
        raise WorkbenchError(
            "Argument Workbench project is invalid: "
            + "; ".join(verification_errors)
        )
    output_fn(
        adjudication_status(
            paths.workspace,
            review_id=review_id,
            verdict=verdict,
            claim=claim,
            check_id=check_id,
            summary_only=summary_only,
        )
    )
    if view_only or summary_only:
        return 0
    findings = filter_finding_entries(
        current_finding_entries(paths.workspace, review_id=review_id),
        verdict=verdict,
        claim=claim,
        check_id=check_id,
    )
    if not findings:
        output_fn("No current Findings match the selected filters.")
        return 0
    latest = latest_adjudications(list_adjudications(paths))
    output_fn(
        "Human judgment is final. Choose Accept, Reject, or Defer for each model Finding."
    )
    for finding in findings:
        finding_id = str(finding.value["finding_id"])
        previous = latest.get(finding_id)
        if previous is not None and not review_all:
            continue
        output_fn("")
        output_fn(f"{finding_id} - {finding.value['target_claim']}")
        output_fn(
            f"Lens: {finding.value['lens']['id']} / {finding.value['lens'].get('check_id') or 'perspective'}"
        )
        output_fn(f"Problem: {finding.value['reason']}")
        if previous is not None:
            output_fn(
                f"Current human decision: {previous[1]['decision']} - {previous[1]['reason'] or 'no reason'}"
            )
        while True:
            choice = input_fn("[A]ccept [R]eject [D]efer [S]kip [Q]uit: ").strip().casefold()
            if choice in {"a", "accept", "r", "reject", "d", "defer", "s", "skip", "q", "quit"}:
                break
            output_fn("Choose A, R, D, S, or Q.")
        if choice in {"q", "quit"}:
            output_fn("Progress preserved.")
            return 0
        if choice in {"s", "skip"}:
            output_fn("Skipped.")
            continue
        decision = {
            "a": "accept",
            "accept": "accept",
            "r": "reject",
            "reject": "reject",
            "d": "defer",
            "defer": "defer",
        }[choice]
        reason = ""
        action_inputs: list[tuple[str, str]] = []
        if decision == "accept":
            reason = input_fn("Why accept? (optional): ").strip()
            while True:
                output_fn("Revision action types:")
                for index, action_type in enumerate(REVISION_ACTION_TYPES, 1):
                    output_fn(f"  {index}. {action_type}")
                raw_type = _ask_required(
                    input_fn, output_fn, "Action type number or name: "
                )
                if raw_type.isdigit() and 1 <= int(raw_type) <= len(REVISION_ACTION_TYPES):
                    action_type = REVISION_ACTION_TYPES[int(raw_type) - 1]
                elif raw_type in REVISION_ACTION_TYPES:
                    action_type = raw_type
                else:
                    output_fn("Unknown action type.")
                    continue
                text = _ask_required(input_fn, output_fn, "Concrete revision action: ")
                action_inputs.append((action_type, text))
                if input_fn("Add another action? [y/N]: ").strip().casefold() != "y":
                    break
        else:
            reason = _ask_required(
                input_fn,
                output_fn,
                "Why reject? " if decision == "reject" else "Why defer? ",
            )
        adjudication_path, action_paths = append_finding_decision(
            paths.workspace,
            finding_id,
            decision=decision,
            reason=reason,
            actions=action_inputs,
        )
        latest = latest_adjudications(list_adjudications(paths))
        output_fn(f"Saved {adjudication_path.name}.")
        for action_path in action_paths:
            output_fn(f"Saved {action_path.name}.")
    plan_path, _ = rebuild_revision_plan(paths.workspace)
    output_fn(f"Revision plan: {plan_path}")
    return 0


def verify_adjudications(project_dir: Path | str) -> list[str]:
    paths = human_review_paths(project_dir)
    errors: list[str] = []
    if not any(
        path.exists()
        for path in (paths.adjudications_dir, paths.actions_dir, paths.plan_dir)
    ):
        return errors
    review_errors = verify_reviews(paths.workspace)
    errors.extend(f"review provenance: {error}" for error in review_errors)
    perspective_errors = verify_perspective_reviews(paths.workspace)
    errors.extend(
        f"Perspective Review provenance: {error}" for error in perspective_errors
    )
    try:
        findings = _all_finding_entries(paths.workspace)
        adjudications = list_adjudications(paths)
        actions = list_revision_actions(paths)
    except (OSError, WorkbenchError) as exc:
        return errors + [str(exc)]
    finding_by_id = {
        str(entry.value.get("finding_id")): entry for entry in findings
    }
    finding_by_hash = {sha256_bytes(entry.data): entry for entry in findings}
    if paths.adjudications_dir.exists():
        known = {path.name for path, _, _ in adjudications}
        for child in paths.adjudications_dir.iterdir():
            if child.name not in known:
                errors.append(f"unexpected adjudication entry: {child.name}")
    expected_adjudication_numbers = list(range(1, len(adjudications) + 1))
    actual_adjudication_numbers = [
        int(ADJUDICATION_ID_PATTERN.fullmatch(path.stem).group(1))
        for path, _, _ in adjudications
    ]
    if actual_adjudication_numbers != expected_adjudication_numbers:
        errors.append("adjudication IDs must be continuous from AD0001")
    latest_hash_by_finding: dict[str, str] = {}
    adjudication_by_id: dict[str, tuple[dict[str, Any], bytes]] = {}
    for path, adjudication, data in adjudications:
        prefix = path.name
        contract_errors = validate_artifact(adjudication)
        errors.extend(f"{prefix}: {error}" for error in contract_errors)
        if adjudication.get("adjudication_id") != path.stem:
            errors.append(f"{prefix}: adjudication_id does not match filename")
        finding_id = str(adjudication.get("finding_id"))
        parent_by_role = {
            parent.get("role"): parent
            for parent in adjudication.get("parents", [])
            if isinstance(parent, dict)
        }
        finding_parent_hash = parent_by_role.get("finding", {}).get("sha256")
        finding = finding_by_hash.get(str(finding_parent_hash))
        if finding is None:
            errors.append(f"{prefix}: finding parent hash is not archived")
        elif finding.value.get("finding_id") != finding_id:
            errors.append(f"{prefix}: finding_id does not match finding parent")
        previous_hash = latest_hash_by_finding.get(finding_id)
        if previous_hash is None:
            if adjudication.get("supersedes") is not None:
                errors.append(f"{prefix}: first decision for Finding cannot supersede")
        else:
            if adjudication.get("supersedes") != previous_hash:
                errors.append(f"{prefix}: supersedes does not identify latest decision")
            if parent_by_role.get("previous-adjudication", {}).get("sha256") != previous_hash:
                errors.append(f"{prefix}: previous-adjudication parent is disconnected")
        latest_hash_by_finding[finding_id] = sha256_bytes(data)
        adjudication_by_id[str(adjudication.get("adjudication_id"))] = (
            adjudication,
            data,
        )
    if paths.actions_dir.exists():
        known = {path.name for path, _, _ in actions}
        for child in paths.actions_dir.iterdir():
            if child.name not in known:
                errors.append(f"unexpected RevisionAction entry: {child.name}")
    expected_action_numbers = list(range(1, len(actions) + 1))
    actual_action_numbers = [
        int(ACTION_ID_PATTERN.fullmatch(path.stem).group(1))
        for path, _, _ in actions
    ]
    if actual_action_numbers != expected_action_numbers:
        errors.append("RevisionAction IDs must be continuous from RA0001")
    for path, action, data in actions:
        prefix = path.name
        contract_errors = validate_artifact(action)
        errors.extend(f"{prefix}: {error}" for error in contract_errors)
        if action.get("action_id") != path.stem:
            errors.append(f"{prefix}: action_id does not match filename")
        adjudication_id = str(action.get("adjudication_id"))
        adjudication_entry = adjudication_by_id.get(adjudication_id)
        parent_by_role = {
            parent.get("role"): parent
            for parent in action.get("parents", [])
            if isinstance(parent, dict)
        }
        if adjudication_entry is None:
            errors.append(f"{prefix}: adjudication_id does not exist")
            continue
        adjudication, adjudication_bytes = adjudication_entry
        if parent_by_role.get("adjudication", {}).get("sha256") != sha256_bytes(
            adjudication_bytes
        ):
            errors.append(f"{prefix}: adjudication parent hash is disconnected")
        finding = finding_by_id.get(str(adjudication.get("finding_id")))
        if finding is not None and action.get("target_claim") != finding.value.get(
            "target_claim"
        ):
            errors.append(f"{prefix}: target_claim does not match Finding")
    bundle_entries: list[tuple[object, bytes]] = [
        (entry.value, entry.data) for entry in findings
    ]
    perspective_result_hashes: set[str] = set()
    for entry in findings:
        if not isinstance(entry.review, PerspectiveReviewPaths):
            continue
        response_path = entry.review.attempt_dir(entry.attempt_id) / "response.json"
        try:
            result, result_bytes = _read_json(response_path)
        except (OSError, WorkbenchError) as exc:
            errors.append(
                f"{entry.review.review_id}/{entry.attempt_id}: "
                f"cannot load Perspective result parent: {exc}"
            )
            continue
        digest = sha256_bytes(result_bytes)
        if digest not in perspective_result_hashes:
            bundle_entries.append((result, result_bytes))
            perspective_result_hashes.add(digest)
    bundle_entries.extend((value, data) for _, value, data in adjudications)
    bundle_entries.extend((value, data) for _, value, data in actions)
    if paths.plan_dir.exists():
        if paths.plan_dir.is_symlink() or not paths.plan_dir.is_dir():
            errors.append("revision-plan must be a regular non-symlink directory")
        elif not paths.plan_record.is_file() or not paths.plan_markdown.is_file():
            errors.append("revision plan cache is incomplete")
        elif paths.plan_record.is_symlink() or paths.plan_markdown.is_symlink():
            errors.append("revision plan cache files must not be symlinks")
        else:
            try:
                record, record_bytes = _read_json(paths.plan_record)
                _, expected_record_bytes, expected_markdown, expected_entries = (
                    _derive_revision_plan(paths.workspace)
                )
                if paths.plan_record.read_bytes() != expected_record_bytes:
                    errors.append("revision plan record is not reproducible")
                if paths.plan_markdown.read_bytes() != expected_markdown.encode("utf-8"):
                    errors.append("revision-plan.md is not reproducible")
                bundle_entries.extend(expected_entries[-1:])
            except (OSError, WorkbenchError) as exc:
                errors.append(f"cannot reproduce revision plan: {exc}")
    elif adjudications:
        errors.append("human decisions exist but revision plan cache is missing")
    errors.extend(validate_contract_bundle(bundle_entries))
    return errors
