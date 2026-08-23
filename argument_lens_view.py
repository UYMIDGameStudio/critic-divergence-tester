"""Claim-centered, non-aggregating view across current Review Lenses."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from argument_contracts import sha256_bytes
from argument_perspective import (
    list_perspective_reviews,
    selected_perspective_attempt,
)
from argument_review import list_rule_reviews, selected_result_attempt
from argument_workbench import (
    WorkbenchError,
    _read_json,
    verify_workspace,
    workspace_paths,
)


def _normalized_claim(version_id: str, claim_id: str) -> str:
    candidate = claim_id.strip().upper()
    if re.fullmatch(r"C[1-9][0-9]*", candidate):
        return f"{version_id}:{candidate}"
    if re.fullmatch(r"V[1-9][0-9]*:C[1-9][0-9]*", candidate):
        if not candidate.startswith(f"{version_id}:"):
            raise WorkbenchError(
                f"Claim {candidate} is not part of current version {version_id}"
            )
        return candidate
    raise WorkbenchError("Claim must be C1 or version-qualified, for example V1:C1")


def _current_rule_indexes(
    project_dir: Path | str, current_ir_sha256: str
) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    by_lens: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {}
    for review in list_rule_reviews(project_dir):
        record, _ = _read_json(review.record)
        parents = {
            parent.get("role"): parent
            for parent in record.get("parents", [])
            if isinstance(parent, dict)
        }
        if parents.get("target-ir", {}).get("sha256") != current_ir_sha256:
            continue
        try:
            _, attempt, _ = selected_result_attempt(review)
        except WorkbenchError:
            continue
        index_path = (
            review.derived_attempt_dir(str(attempt["attempt_id"]))
            / "claim-review-index.json"
        )
        index, index_bytes = _read_json(index_path)
        by_lens[str(record["lens"]["id"])] = (
            record,
            index,
            sha256_bytes(index_bytes),
        )
    return list(by_lens.values())


def _current_perspective_indexes(
    project_dir: Path | str, current_ir_sha256: str
) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    by_lens: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {}
    for review in list_perspective_reviews(project_dir):
        record, _ = _read_json(review.record)
        parents = {
            parent.get("role"): parent
            for parent in record.get("parents", [])
            if isinstance(parent, dict)
        }
        if parents.get("target-ir", {}).get("sha256") != current_ir_sha256:
            continue
        try:
            _, attempt, _ = selected_perspective_attempt(review)
        except WorkbenchError:
            continue
        index_path = (
            review.derived_attempt_dir(str(attempt["attempt_id"]))
            / "perspective-review-index.json"
        )
        index, index_bytes = _read_json(index_path)
        by_lens[str(record["lens"]["id"])] = (
            record,
            index,
            sha256_bytes(index_bytes),
        )
    return list(by_lens.values())


def render_claim_lenses(project_dir: Path | str, claim_id: str) -> str:
    workspace = workspace_paths(project_dir)
    errors = verify_workspace(workspace)
    if errors:
        raise WorkbenchError(
            "Argument Workbench project is invalid: " + "; ".join(errors)
        )
    reviewed_ir, reviewed_ir_bytes = _read_json(workspace.reviewed_payload)
    reviewed_record, _ = _read_json(workspace.reviewed_record)
    version_id = str(reviewed_record["version_id"])
    target = _normalized_claim(version_id, claim_id)
    local_id = target.split(":", 1)[1]
    claim = next(
        (item for item in reviewed_ir["claims"] if item["id"] == local_id), None
    )
    if claim is None:
        raise WorkbenchError(f"unknown current Claim: {target}")

    current_sha256 = sha256_bytes(reviewed_ir_bytes)
    rule_indexes = _current_rule_indexes(workspace, current_sha256)
    perspective_indexes = _current_perspective_indexes(
        workspace, current_sha256
    )
    lines = [
        f"# Review Lenses — {target}",
        "",
        f"- Claim: {claim['text']}",
        f"- Source: {claim['source_quote']}",
        f"- Position: `{claim['position']}` `[deterministic]`",
        f"- Current Reviewed IR SHA-256: `{current_sha256}`",
        "- Lens results are displayed separately. No vote, average, winner, or automatic synthesis is computed.",
        "",
    ]
    shown = 0
    for record, index, index_sha256 in rule_indexes:
        outcomes = [
            outcome
            for outcome in index["outcomes"]
            if outcome["target_claim"] == target
        ]
        if not outcomes:
            continue
        shown += 1
        lines.extend(
            [
                f"## {record['lens']['id']} — Rule Lens",
                "",
                f"- Review: `{record['review_id']}` / `{index['attempt_id']}`",
                f"- Index SHA-256: `{index_sha256}`",
                "",
            ]
        )
        for outcome in outcomes:
            status = outcome.get("execution_status", "evaluated")
            verdict = (
                str(outcome["verdict"]).upper()
                if status == "evaluated"
                else str(status).upper()
            )
            finding = (
                f" — Finding `{outcome['finding_id']}`"
                if outcome.get("finding_id") is not None
                else ""
            )
            lines.extend(
                [
                    f"### {verdict} — `{outcome['check_id']}`{finding}",
                    "",
                    f"- Reason: {outcome['reason']} `[model-derived]`",
                ]
            )
            if outcome.get("consequence"):
                lines.append(f"- Consequence: {outcome['consequence']}")
            lines.append("")

    for record, index, index_sha256 in perspective_indexes:
        outcomes = [
            outcome
            for outcome in index["outcomes"]
            if outcome["target_claim"] == target
        ]
        if not outcomes:
            continue
        shown += 1
        lines.extend(
            [
                f"## {record['lens']['id']} — Perspective Lens",
                "",
                f"- Review: `{record['review_id']}` / `{index['attempt_id']}`",
                f"- Index SHA-256: `{index_sha256}`",
                "",
            ]
        )
        for outcome in outcomes:
            finding = (
                f" — Finding `{outcome['finding_id']}`"
                if outcome["finding_id"] is not None
                else ""
            )
            lines.extend(
                [
                    f"### {str(outcome['verdict']).upper()}{finding}",
                    "",
                    f"- Reason: {outcome['reason']} `[model-derived]`",
                    f"- Framework analysis: {outcome['framework_analysis']} `[model-derived]`",
                    f"- Basis refs: {', '.join(outcome['basis_refs'])}",
                ]
            )
            if outcome["consequence"]:
                lines.append(f"- Consequence: {outcome['consequence']}")
            lines.append("")
    if shown == 0:
        lines.extend(
            [
                "No current Review Lens has an outcome for this Claim.",
                "",
            ]
        )
    return "\n".join(lines)
