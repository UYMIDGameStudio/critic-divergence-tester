"""Pure data contracts for human adjudication and revision planning."""

from __future__ import annotations

from collections import Counter
from typing import Any


ADJUDICATION_SCHEMA_VERSION = 1
DECISIONS = ("accept", "defer", "reject")
FINDING_KEYS = {
    "id",
    "position",
    "claim",
    "reason",
    "test",
    "conclusion",
    "decision",
    "author_reason",
    "revision_action",
}


class WorkflowError(ValueError):
    """Raised when a workflow artifact is incomplete or internally inconsistent."""


def decision_field_errors(
    decision: object,
    author_reason: object,
    revision_action: object,
    *,
    require_complete: bool,
) -> list[str]:
    """Shared human-decision semantics for legacy reports and the Workbench.

    The Workbench stores structured RevisionAction artifacts, while the legacy
    report workflow stores one text field.  Both use the same accept/reject/
    defer requirements through this adapter boundary.
    """
    errors: list[str] = []
    if decision is not None and decision not in DECISIONS:
        return [".decision must be accept, reject, defer, or null"]
    if decision is None:
        if require_complete:
            errors.append(".decision is not filled")
        return errors
    if decision == "accept" and (
        not isinstance(revision_action, str) or not revision_action.strip()
    ):
        errors.append(".revision_action is required when accepting")
    if decision in {"reject", "defer"} and (
        not isinstance(author_reason, str) or not author_reason.strip()
    ):
        verb = "rejecting" if decision == "reject" else "deferring"
        errors.append(f".author_reason is required when {verb}")
    return errors


def adjudication_template(
    *,
    protocol: str,
    report_sha256: str,
    manifest_sha256: str,
    findings: list[dict[str, str]],
    report_status: str = "complete",
    unverified: str = "none",
) -> dict[str, Any]:
    return {
        "schema_version": ADJUDICATION_SCHEMA_VERSION,
        "artifact": "critic-adjudication",
        "source": {
            "protocol": protocol,
            "report_sha256": report_sha256,
            "manifest_sha256": manifest_sha256,
            "report_status": report_status,
            "unverified": unverified,
        },
        "instructions": {
            "decision": "accept | reject | defer",
            "accept": "填写 revision_action；author_reason 可选",
            "reject": "填写 author_reason",
            "defer": "填写 author_reason；revision_action 可选",
        },
        "findings": [
            {
                **finding,
                "decision": None,
                "author_reason": "",
                "revision_action": "",
            }
            for finding in findings
        ],
    }


def validate_adjudication(value: object, *, require_complete: bool) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["adjudication must be a JSON object"]
    if value.get("schema_version") != ADJUDICATION_SCHEMA_VERSION:
        errors.append("schema_version must be 1")
    if value.get("artifact") != "critic-adjudication":
        errors.append("artifact must be critic-adjudication")

    source = value.get("source")
    if not isinstance(source, dict):
        errors.append("source must be an object")
    else:
        if not isinstance(source.get("protocol"), str) or not source.get("protocol"):
            errors.append("source.protocol must be a non-empty string")
        for key in ("report_sha256", "manifest_sha256"):
            digest = source.get(key)
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                errors.append(f"source.{key} must be a lowercase SHA-256 digest")
        report_status = source.get("report_status")
        unverified = source.get("unverified")
        if report_status not in {"complete", "partial", "blocked"}:
            errors.append("source.report_status must be complete, partial, or blocked")
        if not isinstance(unverified, str) or not unverified.strip():
            errors.append("source.unverified must be a non-empty string")
        elif report_status == "complete" and unverified.casefold() != "none":
            errors.append("complete report requires source.unverified=none")
        elif report_status in {"partial", "blocked"} and unverified.casefold() == "none":
            errors.append(f"{report_status} report requires a concrete unverified reason")

    findings = value.get("findings")
    if not isinstance(findings, list):
        errors.append("findings must be an array")
        return errors

    expected_ids = [f"A{index}" for index in range(1, len(findings) + 1)]
    actual_ids: list[object] = []
    for index, finding in enumerate(findings, 1):
        label = f"findings[{index - 1}]"
        if not isinstance(finding, dict):
            errors.append(f"{label} must be an object")
            continue
        if set(finding) != FINDING_KEYS:
            errors.append(f"{label} must contain exactly the adjudication fields")
        actual_ids.append(finding.get("id"))
        for key in (
            "position",
            "claim",
            "reason",
            "test",
            "conclusion",
            "author_reason",
            "revision_action",
        ):
            if not isinstance(finding.get(key), str):
                errors.append(f"{label}.{key} must be a string")

        decision = finding.get("decision")
        author_reason = finding.get("author_reason")
        revision_action = finding.get("revision_action")
        errors.extend(
            label + suffix
            for suffix in decision_field_errors(
                decision,
                author_reason,
                revision_action,
                require_complete=require_complete,
            )
        )

    if actual_ids != expected_ids:
        errors.append(
            f"finding IDs must be continuous and ordered: expected {expected_ids}, "
            f"got {actual_ids}"
        )
    return errors


def revision_plan_markdown(value: object, *, adjudication_sha256: str) -> str:
    errors = validate_adjudication(value, require_complete=True)
    if errors:
        raise WorkflowError("; ".join(errors))
    assert isinstance(value, dict)
    source = value["source"]
    findings = value["findings"]
    assert isinstance(source, dict)
    assert isinstance(findings, list)
    counts = Counter(finding["decision"] for finding in findings)
    lines = [
        "# 修改计划",
        "",
        f"协议：`{source['protocol']}`",
        f"报告状态：`{source['report_status']}`",
        f"未核实项：{source['unverified']}",
        f"报告 SHA-256：`{source['report_sha256']}`",
        f"裁决 SHA-256：`{adjudication_sha256}`",
        "",
        "## 裁决汇总",
        "",
        f"- 接受：{counts['accept']}",
        f"- 暂缓：{counts['defer']}",
        f"- 拒绝：{counts['reject']}",
        "",
    ]
    if not findings:
        lines.extend(["没有需要裁决的发现。", ""])
        return "\n".join(lines)

    labels = {
        "accept": "接受并修改",
        "defer": "暂缓处理",
        "reject": "拒绝的发现",
    }
    for decision in DECISIONS:
        selected = [item for item in findings if item["decision"] == decision]
        lines.extend([f"## {labels[decision]}", ""])
        if not selected:
            lines.extend(["无。", ""])
            continue
        for finding in selected:
            lines.extend(
                [
                    f"### {finding['id']}: {finding['claim']}",
                    "",
                    f"- 位置：{finding['position']}",
                    f"- critic 理由：{finding['reason']}",
                    f"- 后果检验：{finding['test']}",
                    f"- critic 结论：{finding['conclusion']}",
                    f"- 作者理由：{finding['author_reason'] or '—'}",
                    f"- 修改动作：{finding['revision_action'] or '—'}",
                    "",
                ]
            )
    return "\n".join(lines)
