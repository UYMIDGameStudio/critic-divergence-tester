#!/usr/bin/env python3
"""Provider-neutral runner for the critic protocols in this repository.

The runner deliberately knows nothing about Claude Code, OpenAI, Anthropic, or
any other model provider. It prepares self-contained prompt bundles, invokes an
arbitrary UTF-8 stdin/stdout command, archives exact bytes, and validates the
deterministic parts of the report contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from critic_execution import ExecutorResult, execute_with_limits
from critic_scoring import (
    ALL_COMPARISONS,
    BETWEEN_COMPARISONS,
    RUN_NAMES,
    WITHIN_COMPARISONS,
    ScorecardError,
    pairing_scorecard,
    score_divergence,
    score_markdown,
    scorecard_template,
)


ROOT = Path(__file__).resolve().parent

PROTOCOLS = {
    "critic-individualist": ROOT / "critic-individualist.md",
    "critic-contrastivist": ROOT / "critic-contrastivist.md",
    "citation-auditor": ROOT / "citation-auditor.md",
    "critic-generic": ROOT / "test" / "critic-generic.md",
}

TEST_ONLY = {"critic-generic"}
CRITIC_PROTOCOLS = {
    "critic-individualist",
    "critic-contrastivist",
    "critic-generic",
}

CRITIC_SECTIONS = (
    "## 1. 原子指控",
    "## 2. 逐条后果检验",
    "## 3. 核心论证压力测试",
    "## 4. 唯一最弱一步",
    "## 5. 唯一最强论证",
    "## 6. 让步条件",
)

STATUS_PATTERN = re.compile(r"STATUS: (complete|partial|blocked)")
ITEM_PATTERN = re.compile(r"### A([1-9][0-9]*)")
CITATION_ITEM_PATTERN = re.compile(r"## C([1-9][0-9]*)")
BIBLIOGRAPHY_DISTRIBUTION_PATTERN = re.compile(
    r"书目证据分布: A ([0-9]+) / B ([0-9]+) / C ([0-9]+) / D ([0-9]+)"
)
CONTENT_DISTRIBUTION_PATTERN = re.compile(
    r"内容证据分布: A ([0-9]+) / B ([0-9]+) / C ([0-9]+) / D ([0-9]+)"
)

EXIT_INVALID_REPORT = 3
EXIT_INVALID_ARCHIVE = 4
EXIT_INTERRUPTED = 130
EXIT_TIMEOUT = 124
EXIT_OUTPUT_LIMIT = 125
EXIT_INVALID_SCORECARD = 6
EXIT_CAMPAIGN_FAILED = 7
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024



@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"valid": self.valid, "errors": list(self.errors)}


@dataclass(frozen=True)
class VerificationResult:
    valid: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON contains a key whose meaning would be ambiguous."""


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateJsonKeyError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def parse_json(text: str) -> object:
    return json.loads(text, object_pairs_hook=_json_object)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """Hash exact UTF-8 bytes; retained as a small public helper."""
    return sha256_bytes(text.encode("utf-8"))


def read_utf8(path: Path) -> tuple[str, bytes]:
    """Read UTF-8 while accepting a BOM and retaining the original bytes."""
    raw = path.read_bytes()
    return raw.decode("utf-8-sig"), raw


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace a file atomically using a temporary file in the same directory."""
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def strip_frontmatter(text: str) -> str:
    """Remove Claude-Code-style YAML metadata while keeping the prompt body."""
    lines = text.removeprefix("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return "\n".join(lines).strip()
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :]).strip()
    raise ValueError("unterminated YAML frontmatter")


def load_protocol(name: str, allow_test_artifact: bool = False) -> tuple[str, bytes]:
    if name not in PROTOCOLS:
        raise ValueError(f"unknown protocol: {name}")
    if name in TEST_ONLY and not allow_test_artifact:
        raise ValueError(
            f"{name} is a test artifact; pass --allow-test-artifact only for divergence testing"
        )
    raw_text, raw_bytes = read_utf8(PROTOCOLS[name])
    return strip_frontmatter(raw_text), raw_bytes


def build_prompt(protocol: str, manuscript: str, source_name: str) -> str:
    return (
        "# 审查协议\n\n"
        f"{protocol}\n\n"
        "# 本次任务\n\n"
        f"只审查下面的稿件 `{source_name}`。不要修改稿件，也不要读取或假定存在其他审查报告。\n\n"
        "# 稿件\n\n"
        f"{manuscript.rstrip()}\n"
    )


def new_run_dir(runs_dir: Path, protocol_name: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target = runs_dir / f"{stamp}--{protocol_name}"
    target.mkdir(parents=True, exist_ok=False)
    if os.name == "posix":
        os.chmod(target, 0o700)
    return target


def executor_metadata(executor: list[str] | None) -> dict[str, object] | None:
    """Record useful executor identity without persisting possibly secret arguments."""
    if not executor:
        return None
    return {
        "command": Path(executor[0]).name,
        "argument_count": max(0, len(executor) - 1),
    }


def _section_ranges(lines: list[str]) -> dict[str, list[str]] | None:
    positions = [(index, line) for index, line in enumerate(lines) if line.startswith("## ")]
    headings = tuple(line for _, line in positions)
    if headings != CRITIC_SECTIONS:
        return None

    sections: dict[str, list[str]] = {}
    for position, (start, heading) in enumerate(positions):
        end = positions[position + 1][0] if position + 1 < len(positions) else len(lines)
        sections[heading] = lines[start + 1 : end]
    return sections


def _item_ids(lines: list[str], section_name: str, errors: list[str]) -> list[int]:
    identifiers: list[int] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("### A"):
            continue
        match = ITEM_PATTERN.fullmatch(stripped)
        if match is None:
            errors.append(f"{section_name} contains a malformed atomic-item heading: {stripped}")
            continue
        identifiers.append(int(match.group(1)))
    return identifiers


def _item_blocks(lines: list[str]) -> list[tuple[int, list[str]]]:
    positions: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = ITEM_PATTERN.fullmatch(line.strip())
        if match is not None:
            positions.append((index, int(match.group(1))))

    blocks: list[tuple[int, list[str]]] = []
    for position, (start, identifier) in enumerate(positions):
        end = positions[position + 1][0] if position + 1 < len(positions) else len(lines)
        blocks.append((identifier, lines[start + 1 : end]))
    return blocks


def extract_critic_claims(report: str) -> list[dict[str, str]]:
    """Extract validated section-1 A items for a traceable pairing scorecard."""
    lines = report.splitlines()
    sections = _section_ranges(lines)
    if sections is None:
        raise ValueError("cannot extract claims from a report with invalid headings")
    errors: list[str] = []
    claims: list[dict[str, str]] = []
    for identifier, block in _item_blocks(sections[CRITIC_SECTIONS[0]]):
        position = _field_value(block, "位置：", f"A{identifier}", errors)
        claim = _field_value(block, "指控：", f"A{identifier}", errors)
        reason = _field_value(block, "理由：", f"A{identifier}", errors)
        if position is not None and claim is not None and reason is not None:
            claims.append(
                {
                    "id": f"A{identifier}",
                    "position": position,
                    "claim": claim,
                    "reason": reason,
                }
            )
    if errors:
        raise ValueError("cannot extract claims: " + "; ".join(errors))
    return claims


def _validate_item_fields(
    lines: list[str],
    section_name: str,
    required_fields: tuple[str, ...],
    errors: list[str],
) -> None:
    for identifier, block in _item_blocks(lines):
        stripped_block = [line.strip() for line in block]
        for field in required_fields:
            matches = [line for line in stripped_block if line.startswith(field)]
            if len(matches) != 1:
                errors.append(
                    f"{section_name} A{identifier} must contain exactly one {field} field"
                )
            elif not matches[0][len(field) :].strip():
                errors.append(f"{section_name} A{identifier} has an empty {field} field")


def _has_substance(lines: list[str]) -> bool:
    ignored_prefixes = ("STATUS:", "UNVERIFIED:")
    return any(
        line.strip() and not line.strip().startswith(ignored_prefixes)
        for line in lines
    )


def _validate_footer(
    lines: list[str], errors: list[str], *, require_adjacent: bool = True
) -> list[str]:
    nonempty = [line.strip() for line in lines if line.strip()]
    status_lines = [line for line in nonempty if STATUS_PATTERN.fullmatch(line)]
    unverified_lines = [line for line in nonempty if line.startswith("UNVERIFIED:")]
    if len(status_lines) != 1:
        errors.append(
            f"report must contain exactly one valid STATUS line; found {len(status_lines)}"
        )
    if len(unverified_lines) != 1:
        errors.append(
            f"report must contain exactly one UNVERIFIED line; found {len(unverified_lines)}"
        )
    if len(nonempty) < 2:
        errors.append("report must end with STATUS and UNVERIFIED lines")
        return nonempty
    if require_adjacent and STATUS_PATTERN.fullmatch(nonempty[-2]) is None:
        errors.append("penultimate non-empty line must be STATUS: complete | partial | blocked")
    if not nonempty[-1].startswith("UNVERIFIED:"):
        errors.append("last non-empty line must start with UNVERIFIED:")
    elif not nonempty[-1][len("UNVERIFIED:") :].strip():
        errors.append("UNVERIFIED must contain a value; use none when nothing is unverified")
    if len(status_lines) == 1 and len(unverified_lines) == 1:
        status = STATUS_PATTERN.fullmatch(status_lines[0]).group(1)
        unverified = unverified_lines[0][len("UNVERIFIED:") :].strip()
        is_none = unverified.casefold() == "none"
        if status == "complete" and not is_none:
            errors.append("STATUS complete requires UNVERIFIED: none")
        if status in {"partial", "blocked"} and is_none:
            errors.append(f"STATUS {status} requires a concrete UNVERIFIED reason")
    return nonempty


def _field_value(
    block: list[str],
    prefix: str,
    item_name: str,
    errors: list[str],
) -> str | None:
    matches = [line.strip() for line in block if line.strip().startswith(prefix)]
    if len(matches) != 1:
        errors.append(f"{item_name} must contain exactly one {prefix} field")
        return None
    value = matches[0][len(prefix) :].strip()
    if not value:
        errors.append(f"{item_name} has an empty {prefix} field")
        return None
    return value


def _citation_blocks(lines: list[str], errors: list[str]) -> list[tuple[int, list[str]]]:
    positions: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        if not line.startswith("## C"):
            continue
        match = CITATION_ITEM_PATTERN.fullmatch(line.strip())
        if match is None:
            errors.append(f"malformed citation heading: {line.strip()}")
            continue
        positions.append((index, int(match.group(1))))

    blocks: list[tuple[int, list[str]]] = []
    for position, (start, identifier) in enumerate(positions):
        end = positions[position + 1][0] if position + 1 < len(positions) else len(lines)
        blocks.append((identifier, lines[start + 1 : end]))
    return blocks


def _distribution_counts(
    line: str,
    pattern: re.Pattern[str],
    label: str,
    errors: list[str],
) -> dict[str, int] | None:
    match = pattern.fullmatch(line)
    if match is None:
        errors.append(f"invalid {label} line")
        return None
    return dict(zip("ABCD", (int(value) for value in match.groups()), strict=True))


def _validate_citation_report(report: str) -> ValidationResult:
    errors: list[str] = []
    lines = report.splitlines()
    nonempty = _validate_footer(lines, errors, require_adjacent=False)
    if len(nonempty) < 4:
        errors.append("citation report must end with STATUS, two distributions, and UNVERIFIED")
        return ValidationResult(False, tuple(errors))

    if STATUS_PATTERN.fullmatch(nonempty[-4]) is None:
        errors.append("fourth-last non-empty line must be a valid STATUS line")
    bibliography_counts = _distribution_counts(
        nonempty[-3],
        BIBLIOGRAPHY_DISTRIBUTION_PATTERN,
        "bibliography evidence distribution",
        errors,
    )
    content_counts = _distribution_counts(
        nonempty[-2],
        CONTENT_DISTRIBUTION_PATTERN,
        "content evidence distribution",
        errors,
    )

    extra_headings = [
        line for line in lines if line.startswith("## ") and not line.startswith("## C")
    ]
    if extra_headings:
        errors.append(f"citation report contains unexpected level-2 headings: {extra_headings}")

    blocks = _citation_blocks(lines, errors)
    identifiers = [identifier for identifier, _ in blocks]
    if identifiers:
        expected = list(range(1, len(identifiers) + 1))
        if identifiers != expected:
            errors.append(f"citation IDs must be continuous C1..Cn; found {identifiers}")
    elif "无引证" not in report:
        errors.append("citation report must contain C1..Cn items or an explicit 无引证 conclusion")

    actual_bibliography = dict.fromkeys("ABCD", 0)
    actual_content = dict.fromkeys("ABCD", 0)
    has_unverified_evidence = False
    for identifier, block in blocks:
        item_name = f"C{identifier}"
        for field in (
            "文献：",
            "稿件位置：",
            "核对版本：",
            "定位：",
        ):
            _field_value(block, field, item_name, errors)

        bibliography_grade = _field_value(block, "书目证据：", item_name, errors)
        bibliography_source = _field_value(block, "书目来源：", item_name, errors)
        content_grade = _field_value(block, "内容证据：", item_name, errors)
        content_source = _field_value(block, "内容来源：", item_name, errors)
        existence = _field_value(block, "存在性：", item_name, errors)
        bibliography_verdict = _field_value(block, "书目：", item_name, errors)
        viewpoint = _field_value(block, "观点：", item_name, errors)
        context = _field_value(block, "语境：", item_name, errors)
        problem = _field_value(block, "问题：", item_name, errors)

        if bibliography_grade not in {"A", "B", "C", "D"}:
            errors.append(f"{item_name} 书目证据 must be A, B, C, or D")
        else:
            actual_bibliography[bibliography_grade] += 1
        if content_grade not in {"A", "B", "C", "D"}:
            errors.append(f"{item_name} 内容证据 must be A, B, C, or D")
        else:
            actual_content[content_grade] += 1
        if bibliography_grade in {"C", "D"} or content_grade in {"B", "C", "D"}:
            has_unverified_evidence = True

        if existence not in {"通过", "无法确认"}:
            errors.append(f"{item_name} has an invalid 存在性 verdict")
        if bibliography_verdict not in {"通过", "不通过", "无法确认"}:
            errors.append(f"{item_name} has an invalid 书目 verdict")
        if viewpoint not in {
            "明确支持",
            "基本一致但有简化",
            "学界有争议",
            "误归属",
            "无法确认",
        }:
            errors.append(f"{item_name} has an invalid 观点 verdict")
        if context not in {"通过", "不通过", "不适用", "无法确认"}:
            errors.append(f"{item_name} has an invalid 语境 verdict")

        if content_grade in {"B", "C", "D"} and context not in {"不适用", "无法确认"}:
            errors.append(
                f"{item_name} content grade {content_grade} requires 语境=不适用 or 无法确认"
            )
        if content_grade in {"B", "C", "D"} and viewpoint != "无法确认":
            errors.append(
                f"{item_name} content grade {content_grade} requires 观点=无法确认"
            )
        if bibliography_grade == "D" and existence != "无法确认":
            errors.append(f"{item_name} bibliography grade D requires 存在性=无法确认")

        empty_sources = {None, "none", "无", "未检索", "未获得"}
        if bibliography_grade == "A" and bibliography_source in empty_sources:
            errors.append(f"{item_name} bibliography grade A requires a concrete source")
        if content_grade == "A" and content_source in empty_sources:
            errors.append(f"{item_name} content grade A requires a concrete source")

        needs_problem = (
            existence != "通过"
            or bibliography_verdict != "通过"
            or viewpoint != "明确支持"
            or context not in {"通过", "不适用"}
        )
        if needs_problem and problem in {None, "none", "无"}:
            errors.append(f"{item_name} requires a concrete 问题 explanation")

    if bibliography_counts is not None and bibliography_counts != actual_bibliography:
        errors.append(
            "bibliography evidence distribution does not match item grades: "
            f"declared={bibliography_counts}, actual={actual_bibliography}"
        )
    if content_counts is not None and content_counts != actual_content:
        errors.append(
            "content evidence distribution does not match item grades: "
            f"declared={content_counts}, actual={actual_content}"
        )

    status_match = STATUS_PATTERN.fullmatch(nonempty[-4])
    if has_unverified_evidence and status_match is not None:
        if status_match.group(1) == "complete":
            errors.append("citation report with unverified evidence cannot be STATUS complete")
        if nonempty[-1].casefold() == "unverified: none":
            errors.append("citation report must list items with unverified evidence")

    return ValidationResult(not errors, tuple(errors))


def validate_report(protocol_name: str, report: str) -> ValidationResult:
    if protocol_name == "citation-auditor":
        return _validate_citation_report(report)

    errors: list[str] = []
    lines = report.splitlines()
    _validate_footer(lines, errors)

    if protocol_name not in CRITIC_PROTOCOLS:
        return ValidationResult(not errors, tuple(errors))

    sections = _section_ranges(lines)
    if sections is None:
        actual = [line for line in lines if line.startswith("## ")]
        errors.append(
            "critic report headings must appear exactly once and in order: "
            + " | ".join(CRITIC_SECTIONS)
            + f"; found: {actual or 'none'}"
        )
        return ValidationResult(False, tuple(errors))

    first_lines = sections[CRITIC_SECTIONS[0]]
    second_lines = sections[CRITIC_SECTIONS[1]]
    first_ids = _item_ids(first_lines, CRITIC_SECTIONS[0], errors)
    second_ids = _item_ids(second_lines, CRITIC_SECTIONS[1], errors)

    _validate_item_fields(
        first_lines,
        CRITIC_SECTIONS[0],
        ("位置：", "指控：", "理由："),
        errors,
    )
    _validate_item_fields(
        second_lines,
        CRITIC_SECTIONS[1],
        ("检验：", "结论："),
        errors,
    )

    if first_ids:
        expected = list(range(1, len(first_ids) + 1))
        if first_ids != expected:
            errors.append(f"section 1 item IDs must be continuous A1..An; found {first_ids}")
        if second_ids != first_ids:
            errors.append(
                "section 2 must contain exactly one matching entry for every section 1 item; "
                f"section 1={first_ids}, section 2={second_ids}"
            )
    else:
        if "无实质异议" not in "\n".join(first_lines):
            errors.append("section 1 must contain A1..An items or an explicit 无实质异议 conclusion")
        if second_ids:
            errors.append("section 2 cannot contain atomic items when section 1 has none")
        if "不适用" not in "\n".join(second_lines):
            errors.append("section 2 must explicitly say 不适用 when section 1 has no items")

    for heading in CRITIC_SECTIONS[2:]:
        if not _has_substance(sections[heading]):
            errors.append(f"{heading} must not be empty")

    weakest_lines = [line.strip() for line in sections[CRITIC_SECTIONS[3]]]
    weakest_markers = [
        line for line in weakest_lines if line.startswith(("位置：", "不适用："))
    ]
    if len(weakest_markers) != 1:
        errors.append("section 4 must contain exactly one 位置： or 不适用： line")
    elif first_ids and not weakest_markers[0].startswith("位置："):
        errors.append("section 4 must identify one 位置： when atomic items exist")
    elif not first_ids and not weakest_markers[0].startswith("不适用："):
        errors.append("section 4 must use 不适用： when no atomic items exist")
    if first_ids:
        weakest_reasons = [line for line in weakest_lines if line.startswith("理由：")]
        if len(weakest_reasons) != 1 or not weakest_reasons[0][len("理由：") :].strip():
            errors.append("section 4 must contain exactly one non-empty 理由： line")

    strongest_lines = [line.strip() for line in sections[CRITIC_SECTIONS[4]]]
    strongest_markers = [
        line
        for line in strongest_lines
        if line.startswith(("位置：", "无一处满足："))
    ]
    if len(strongest_markers) != 1:
        errors.append("section 5 must contain exactly one 位置： or 无一处满足： line")
    elif strongest_markers[0].startswith("位置："):
        strongest_reasons = [
            line for line in strongest_lines if line.startswith("理由：")
        ]
        if (
            len(strongest_reasons) != 1
            or not strongest_reasons[0][len("理由：") :].strip()
        ):
            errors.append("section 5 must contain exactly one non-empty 理由： line")

    return ValidationResult(not errors, tuple(errors))


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _verify_artifact(
    run_dir: Path,
    manifest: dict[str, object],
    filename: str,
    hash_key: str,
    required: bool,
    errors: list[str],
) -> bytes | None:
    path = run_dir / filename
    if path.is_symlink():
        errors.append(f"{filename} must not be a symbolic link")
        return None
    expected_hash = manifest.get(hash_key)
    if expected_hash is None:
        if required:
            errors.append(f"manifest is missing required {hash_key}")
        if path.exists():
            errors.append(f"{filename} exists but {hash_key} is null")
        return None
    if not _valid_sha256(expected_hash):
        errors.append(f"{hash_key} is not a lowercase SHA-256 hex digest")
        return None
    if not path.is_file():
        errors.append(f"{filename} is missing")
        return None
    data = path.read_bytes()
    actual_hash = sha256_bytes(data)
    if actual_hash != expected_hash:
        errors.append(
            f"{filename} hash mismatch: manifest={expected_hash}, actual={actual_hash}"
        )
    return data


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def verify_run_dir(run_dir: Path, source_path: Path | None = None) -> VerificationResult:
    errors: list[str] = []
    warnings: list[str] = []
    if run_dir.is_symlink():
        return VerificationResult(False, ("run directory must not be a symbolic link",), ())
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_symlink():
        return VerificationResult(False, ("manifest.json must not be a symbolic link",), ())
    try:
        manifest_value = parse_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        return VerificationResult(
            False,
            (f"cannot read manifest.json: {exc}",),
            (),
        )
    if not isinstance(manifest_value, dict):
        return VerificationResult(False, ("manifest.json must contain an object",), ())
    manifest: dict[str, object] = manifest_value

    schema_version = manifest.get("schema_version")
    if schema_version not in {1, 2}:
        errors.append("unsupported or missing schema_version; expected 1 or 2")
    elif schema_version == 1:
        warnings.append("legacy schema_version 1 archive has no output-limit metadata")

    protocol_name = manifest.get("protocol")
    if not isinstance(protocol_name, str) or protocol_name not in PROTOCOLS:
        errors.append(f"unknown protocol in manifest: {protocol_name!r}")

    source_name = manifest.get("source_name")
    if (
        not isinstance(source_name, str)
        or not source_name
        or Path(source_name).name != source_name
        or "/" in source_name
        or "\\" in source_name
    ):
        errors.append("source_name must be a non-empty basename, not a path")

    status = manifest.get("status")
    allowed_statuses = {
        "prepared",
        "running",
        "succeeded",
        "failed",
        "invalid_report",
        "start_failed",
        "interrupted",
        "timed_out",
        "output_limit_exceeded",
    }
    if status not in allowed_statuses:
        errors.append(f"unknown run status: {status!r}")

    started_at = _parse_timestamp(manifest.get("started_at"))
    if started_at is None:
        errors.append("started_at must be a timezone-aware ISO-8601 timestamp")
    completed_at = manifest.get("completed_at")
    parsed_completed_at: datetime | None = None
    if status == "running":
        if completed_at is not None:
            errors.append("running manifest must have completed_at=null")
    else:
        parsed_completed_at = _parse_timestamp(completed_at)
        if parsed_completed_at is None:
            errors.append("completed_at must be a timezone-aware ISO-8601 timestamp")
    if (
        started_at is not None
        and parsed_completed_at is not None
        and parsed_completed_at < started_at
    ):
        errors.append("completed_at cannot be earlier than started_at")

    report_required = status in {
        "succeeded",
        "failed",
        "invalid_report",
        "timed_out",
        "output_limit_exceeded",
    }
    stderr_required = status in {
        "start_failed",
        "timed_out",
        "output_limit_exceeded",
    }
    _verify_artifact(
        run_dir,
        manifest,
        "prompt.md",
        "prompt_sha256",
        True,
        errors,
    )
    report_bytes = _verify_artifact(
        run_dir,
        manifest,
        "report.md",
        "report_sha256",
        report_required,
        errors,
    )
    _verify_artifact(
        run_dir,
        manifest,
        "stderr.log",
        "stderr_sha256",
        stderr_required,
        errors,
    )

    for key in ("source_sha256", "protocol_sha256"):
        if not _valid_sha256(manifest.get(key)):
            errors.append(f"{key} is not a lowercase SHA-256 hex digest")

    if source_path is None:
        warnings.append("source bytes not supplied; source_sha256 was not rechecked")
    else:
        try:
            source_raw = source_path.read_bytes()
        except OSError as exc:
            errors.append(f"cannot read source file: {exc}")
        else:
            if source_path.name != manifest.get("source_name"):
                errors.append(
                    f"source name mismatch: manifest={manifest.get('source_name')!r}, "
                    f"supplied={source_path.name!r}"
                )
            if sha256_bytes(source_raw) != manifest.get("source_sha256"):
                errors.append("supplied source bytes do not match source_sha256")

    if isinstance(protocol_name, str) and protocol_name in PROTOCOLS:
        try:
            current_protocol_raw = PROTOCOLS[protocol_name].read_bytes()
        except OSError as exc:
            warnings.append(f"current protocol file could not be read: {exc}")
        else:
            current_protocol_hash = sha256_bytes(current_protocol_raw)
            if current_protocol_hash != manifest.get("protocol_sha256"):
                warnings.append(
                    "current protocol file differs from the version recorded by this run"
                )

    executor = manifest.get("executor")
    if status == "prepared":
        if executor is not None:
            errors.append("prepared manifest must have executor=null")
    elif not isinstance(executor, dict):
        errors.append("executed run must contain redacted executor metadata")
    else:
        command = executor.get("command")
        argument_count = executor.get("argument_count")
        if (
            not isinstance(command, str)
            or not command
            or "/" in command
            or "\\" in command
        ):
            errors.append("executor.command must be a non-empty basename")
        if (
            not isinstance(argument_count, int)
            or isinstance(argument_count, bool)
            or argument_count < 0
        ):
            errors.append("executor.argument_count must be a non-negative integer")

    timeout_seconds = manifest.get("timeout_seconds")
    if status == "prepared":
        if timeout_seconds is not None:
            errors.append("prepared manifest must have timeout_seconds=null")
    elif (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        errors.append("executed run must have a positive finite timeout_seconds")

    if schema_version == 2:
        max_output_bytes = manifest.get("max_output_bytes")
        if status == "prepared":
            if max_output_bytes is not None:
                errors.append("prepared manifest must have max_output_bytes=null")
        elif (
            not isinstance(max_output_bytes, int)
            or isinstance(max_output_bytes, bool)
            or max_output_bytes <= 0
        ):
            errors.append("executed run must have a positive integer max_output_bytes")
        for key in ("stdout_truncated", "stderr_truncated"):
            if not isinstance(manifest.get(key), bool):
                errors.append(f"{key} must be a boolean")

    executor_code = manifest.get("executor_returncode")
    runner_code = manifest.get("runner_exit_code")
    expected_runner_codes = {
        "prepared": 0,
        "running": None,
        "succeeded": 0,
        "invalid_report": EXIT_INVALID_REPORT,
        "start_failed": 2,
        "interrupted": EXIT_INTERRUPTED,
        "timed_out": EXIT_TIMEOUT,
        "output_limit_exceeded": EXIT_OUTPUT_LIMIT,
    }
    if status in expected_runner_codes and runner_code != expected_runner_codes[status]:
        errors.append(
            f"runner_exit_code {runner_code!r} is inconsistent with status {status!r}"
        )
    if status == "succeeded" and executor_code != 0:
        errors.append("succeeded manifest must have executor_returncode=0")
    if status == "invalid_report" and executor_code != 0:
        errors.append("invalid_report manifest must have executor_returncode=0")
    if status == "failed":
        if not isinstance(executor_code, int) or executor_code == 0:
            errors.append("failed manifest must have a nonzero executor_returncode")
        if runner_code != executor_code:
            errors.append("failed manifest runner_exit_code must equal executor_returncode")

    if report_bytes is not None and isinstance(protocol_name, str) and protocol_name in PROTOCOLS:
        try:
            report_text = report_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            validation = ValidationResult(False, (f"report is not UTF-8: {exc}",))
            if manifest.get("report_validation") != validation.as_dict():
                errors.append("report_validation does not match the UTF-8 decoding failure")
            if status != "invalid_report":
                errors.append("non-UTF-8 report must have status invalid_report")
        else:
            validation = validate_report(protocol_name, report_text)
            if manifest.get("report_validation") != validation.as_dict():
                errors.append("report_validation does not match a fresh validation pass")
            if status == "succeeded" and not validation.valid:
                errors.append("succeeded manifest contains an invalid report")
            if status == "invalid_report" and validation.valid:
                errors.append("invalid_report manifest contains a valid report")

    return VerificationResult(not errors, tuple(errors), tuple(warnings))


def write_run(
    *,
    run_dir: Path,
    protocol_name: str,
    source_path: Path,
    source_raw: bytes,
    protocol_raw: bytes,
    prompt: str,
    started_at: str,
    status: str,
    completed_at: str | None = None,
    report: str | bytes | None = None,
    stderr: str | bytes | None = None,
    executor: list[str] | None = None,
    timeout_seconds: float | None = None,
    max_output_bytes: int | None = None,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    executor_returncode: int | None = None,
    runner_exit_code: int | None = None,
    validation: ValidationResult | None = None,
) -> None:
    prompt_bytes = prompt.encode("utf-8")
    report_bytes = report.encode("utf-8") if isinstance(report, str) else report
    stderr_bytes = stderr.encode("utf-8") if isinstance(stderr, str) else stderr

    atomic_write_bytes(run_dir / "prompt.md", prompt_bytes)
    if report_bytes is not None:
        atomic_write_bytes(run_dir / "report.md", report_bytes)
    if stderr_bytes is not None:
        atomic_write_bytes(run_dir / "stderr.log", stderr_bytes)

    manifest = {
        "schema_version": 2,
        "protocol": protocol_name,
        "source_name": source_path.name,
        "source_sha256": sha256_bytes(source_raw),
        "protocol_sha256": sha256_bytes(protocol_raw),
        "prompt_sha256": sha256_bytes(prompt_bytes),
        "report_sha256": sha256_bytes(report_bytes) if report_bytes is not None else None,
        "stderr_sha256": sha256_bytes(stderr_bytes) if stderr_bytes is not None else None,
        "started_at": started_at,
        "completed_at": completed_at,
        "status": status,
        "executor": executor_metadata(executor),
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "executor_returncode": executor_returncode,
        "runner_exit_code": runner_exit_code,
        "report_validation": validation.as_dict() if validation is not None else None,
    }
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(run_dir / "manifest.json", manifest_text)


def prepare(args: argparse.Namespace) -> int:
    source_path = Path(args.manuscript).resolve()
    source_text, source_raw = read_utf8(source_path)
    protocol, protocol_raw = load_protocol(args.protocol, args.allow_test_artifact)
    prompt = build_prompt(protocol, source_text, source_path.name)
    timestamp = utc_now()

    run_dir = new_run_dir(Path(args.runs_dir), args.protocol)
    write_run(
        run_dir=run_dir,
        protocol_name=args.protocol,
        source_path=source_path,
        source_raw=source_raw,
        protocol_raw=protocol_raw,
        prompt=prompt,
        started_at=timestamp,
        completed_at=timestamp,
        status="prepared",
        runner_exit_code=0,
    )
    print(run_dir / "prompt.md")
    return 0


def _print_validation_errors(validation: ValidationResult) -> None:
    for error in validation.errors:
        print(f"validation error: {error}", file=sys.stderr)


def run(args: argparse.Namespace) -> int:
    if not args.executor:
        raise ValueError("run requires an executor command after --")

    executor = list(args.executor)
    if executor and executor[0] == "--":
        executor = executor[1:]
    if not executor:
        raise ValueError("run requires an executor command after --")

    raw_timeout = getattr(args, "timeout", 900.0)
    try:
        timeout_seconds = 900.0 if raw_timeout is None else float(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a positive finite number") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout must be a positive finite number")
    raw_max_output = getattr(args, "max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)
    if isinstance(raw_max_output, bool):
        raise ValueError("max output bytes must be a positive integer")
    try:
        max_output_bytes = int(raw_max_output)
    except (TypeError, ValueError) as exc:
        raise ValueError("max output bytes must be a positive integer") from exc
    if max_output_bytes <= 0:
        raise ValueError("max output bytes must be a positive integer")
    source_path = Path(args.manuscript).resolve()
    source_text, source_raw = read_utf8(source_path)
    protocol, protocol_raw = load_protocol(args.protocol, args.allow_test_artifact)
    prompt = build_prompt(protocol, source_text, source_path.name)
    started_at = utc_now()
    run_dir = new_run_dir(Path(args.runs_dir), args.protocol)
    args.run_dir_result = run_dir

    write_run(
        run_dir=run_dir,
        protocol_name=args.protocol,
        source_path=source_path,
        source_raw=source_raw,
        protocol_raw=protocol_raw,
        prompt=prompt,
        started_at=started_at,
        status="running",
        executor=executor,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )

    try:
        completed = execute_with_limits(
            executor,
            prompt.encode("utf-8"),
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            capture_dir=run_dir,
        )
    except OSError as exc:
        write_run(
            run_dir=run_dir,
            protocol_name=args.protocol,
            source_path=source_path,
            source_raw=source_raw,
            protocol_raw=protocol_raw,
            prompt=prompt,
            stderr=str(exc) + "\n",
            started_at=started_at,
            completed_at=utc_now(),
            status="start_failed",
            executor=executor,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            runner_exit_code=2,
        )
        print(f"error: executor failed to start; details archived in {run_dir}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        write_run(
            run_dir=run_dir,
            protocol_name=args.protocol,
            source_path=source_path,
            source_raw=source_raw,
            protocol_raw=protocol_raw,
            prompt=prompt,
            started_at=started_at,
            completed_at=utc_now(),
            status="interrupted",
            executor=executor,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            runner_exit_code=EXIT_INTERRUPTED,
        )
        raise

    try:
        report_text = completed.stdout.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        validation = ValidationResult(False, (f"report is not UTF-8: {exc}",))
    else:
        validation = validate_report(args.protocol, report_text)

    stderr = completed.stderr
    if completed.output_limit_exceeded:
        message = f"executor exceeded the {max_output_bytes}-byte combined output limit"
        stderr = stderr.rstrip() + (b"\n" if stderr else b"") + message.encode() + b"\n"
        runner_exit_code = EXIT_OUTPUT_LIMIT
        status = "output_limit_exceeded"
    elif completed.timed_out:
        message = f"executor timed out after {timeout_seconds:g} seconds"
        stderr = stderr.rstrip() + (b"\n" if stderr else b"") + message.encode() + b"\n"
        runner_exit_code = EXIT_TIMEOUT
        status = "timed_out"
    elif completed.returncode != 0:
        runner_exit_code = completed.returncode
        status = "failed"
    elif not validation.valid:
        runner_exit_code = EXIT_INVALID_REPORT
        status = "invalid_report"
    else:
        runner_exit_code = 0
        status = "succeeded"

    write_run(
        run_dir=run_dir,
        protocol_name=args.protocol,
        source_path=source_path,
        source_raw=source_raw,
        protocol_raw=protocol_raw,
        prompt=prompt,
        report=completed.stdout,
        stderr=stderr,
        started_at=started_at,
        completed_at=utc_now(),
        status=status,
        executor=executor,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        stdout_truncated=completed.stdout_truncated,
        stderr_truncated=completed.stderr_truncated,
        executor_returncode=completed.returncode,
        runner_exit_code=runner_exit_code,
        validation=validation,
    )

    quiet = bool(getattr(args, "quiet", False))
    if status in {"timed_out", "output_limit_exceeded"}:
        print(f"error: {message}; details archived in {run_dir}", file=sys.stderr)
    elif not quiet:
        print(run_dir / "report.md")
    if not validation.valid:
        _print_validation_errors(validation)
    return runner_exit_code


def campaign(args: argparse.Namespace) -> int:
    if not args.executor:
        raise ValueError("campaign requires an executor command after --")
    executor = list(args.executor)
    if executor and executor[0] == "--":
        executor = executor[1:]
    if not executor:
        raise ValueError("campaign requires an executor command after --")

    if (
        not isinstance(args.repeat, int)
        or isinstance(args.repeat, bool)
        or args.repeat <= 0
    ):
        raise ValueError("campaign repeat must be a positive integer")
    try:
        timeout_seconds = float(args.timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("campaign timeout must be a positive finite number") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("campaign timeout must be a positive finite number")
    if isinstance(args.max_output_bytes, bool):
        raise ValueError("campaign max output bytes must be a positive integer")
    try:
        max_output_bytes = int(args.max_output_bytes)
    except (TypeError, ValueError) as exc:
        raise ValueError("campaign max output bytes must be a positive integer") from exc
    if max_output_bytes <= 0:
        raise ValueError("campaign max output bytes must be a positive integer")

    protocols = args.protocol or ["critic-individualist", "critic-contrastivist"]
    if len(set(protocols)) != len(protocols):
        raise ValueError("campaign protocols must not contain duplicates")
    for protocol_name in protocols:
        load_protocol(protocol_name, args.allow_test_artifact)

    source_path = Path(args.manuscript).resolve()
    _, source_raw = read_utf8(source_path)
    campaign_started_at = utc_now()
    campaign_dir = new_run_dir(Path(args.campaigns_dir), "campaign")
    runs_dir = campaign_dir / "runs"
    runs_dir.mkdir()
    if os.name == "posix":
        os.chmod(runs_dir, 0o700)

    prefix = {
        "critic-individualist": "I",
        "critic-contrastivist": "C",
        "citation-auditor": "A",
        "critic-generic": "G",
    }
    records: list[dict[str, object]] = []
    for protocol_name in protocols:
        for repetition in range(1, args.repeat + 1):
            label = f"{prefix[protocol_name]}{repetition}"
            run_args = argparse.Namespace(
                protocol=protocol_name,
                manuscript=str(source_path),
                runs_dir=str(runs_dir),
                allow_test_artifact=args.allow_test_artifact,
                executor=executor,
                timeout=timeout_seconds,
                max_output_bytes=max_output_bytes,
                quiet=True,
            )
            exit_code = run(run_args)
            run_dir = run_args.run_dir_result
            manifest = parse_json(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )
            relative_run = run_dir.relative_to(campaign_dir).as_posix()
            records.append(
                {
                    "label": label,
                    "protocol": protocol_name,
                    "repetition": repetition,
                    "run_dir": relative_run,
                    "status": manifest["status"],
                    "runner_exit_code": exit_code,
                    "manifest_sha256": sha256_bytes(
                        (run_dir / "manifest.json").read_bytes()
                    ),
                }
            )

    completed_at = utc_now()
    campaign_manifest = {
        "schema_version": 2,
        "source_name": source_path.name,
        "source_sha256": sha256_bytes(source_raw),
        "created_at": campaign_started_at,
        "completed_at": completed_at,
        "executor": executor_metadata(executor),
        "protocols": protocols,
        "repeat": args.repeat,
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "runs": records,
    }
    can_score = (
        protocols == ["critic-individualist", "critic-contrastivist"]
        and args.repeat == 2
        and all(record["status"] == "succeeded" for record in records)
    )
    if can_score:
        score_runs: dict[str, dict[str, object]] = {}
        for record in records:
            label = str(record["label"])
            run_dir = campaign_dir / str(record["run_dir"])
            run_manifest = parse_json(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )
            report, _ = read_utf8(run_dir / "report.md")
            score_runs[label] = {
                "archive": record["run_dir"],
                "report_sha256": run_manifest["report_sha256"],
                "claims": extract_critic_claims(report),
            }
        template = pairing_scorecard(score_runs)
        atomic_write_text(
            campaign_dir / "scorecard.json",
            json.dumps(template, ensure_ascii=False, indent=2) + "\n",
        )

    summary = [
        "# Critic campaign",
        "",
        f"Source: `{source_path.name}`",
        "",
        "| Run | Protocol | Status | Report |",
        "| --- | --- | --- | --- |",
    ]
    for record in records:
        report_link = f"{record['run_dir']}/report.md"
        summary.append(
            f"| {record['label']} | {record['protocol']} | {record['status']} | "
            f"[report]({report_link}) |"
        )
    if (campaign_dir / "scorecard.json").exists():
        summary.extend(
            [
                "",
                "Fill each `scorecard.json` pairs list after blind one-to-one pairing, "
                "set complete=true, then run:",
                "",
                "```bash",
                "python critic_runner.py score path/to/scorecard.json --format markdown",
                "```",
            ]
        )
    summary_path = campaign_dir / "SUMMARY.md"
    atomic_write_text(summary_path, "\n".join(summary) + "\n")
    scorecard_path = campaign_dir / "scorecard.json"
    campaign_manifest["summary_sha256"] = sha256_bytes(summary_path.read_bytes())
    campaign_manifest["scorecard_template_sha256"] = (
        sha256_bytes(scorecard_path.read_bytes()) if scorecard_path.exists() else None
    )
    atomic_write_text(
        campaign_dir / "campaign.json",
        json.dumps(campaign_manifest, ensure_ascii=False, indent=2) + "\n",
    )

    print(campaign_dir)
    return 0 if all(record["runner_exit_code"] == 0 for record in records) else EXIT_CAMPAIGN_FAILED


def _safe_campaign_run_path(campaign_dir: Path, relative: object) -> Path | None:
    if not isinstance(relative, str) or "\\" in relative:
        return None
    relative_path = PurePosixPath(relative)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or relative_path.parts[0] != "runs"
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        return None
    candidate = campaign_dir
    for part in relative_path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            return None
    try:
        candidate.resolve().relative_to(campaign_dir.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def verify_scorecard_provenance(
    scorecard_path: Path, scorecard: object
) -> tuple[str, ...]:
    """Re-extract immutable claims from sibling campaign archives for schema v2."""
    if not isinstance(scorecard, dict) or scorecard.get("schema_version") != 2:
        return ()
    errors: list[str] = []
    runs = scorecard.get("runs")
    if not isinstance(runs, dict):
        return ("schema v2 scorecard runs must be an object",)
    campaign_dir = scorecard_path.parent
    for run_name in RUN_NAMES:
        run = runs.get(run_name)
        if not isinstance(run, dict):
            errors.append(f"runs.{run_name} must be an object")
            continue
        run_dir = _safe_campaign_run_path(campaign_dir, run.get("archive"))
        if run_dir is None:
            errors.append(f"runs.{run_name}.archive is unsafe or outside the campaign")
            continue
        report_path = run_dir / "report.md"
        if report_path.is_symlink():
            errors.append(f"runs.{run_name} report.md must not be a symbolic link")
            continue
        try:
            report_text, report_raw = read_utf8(report_path)
            extracted = extract_critic_claims(report_text)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"runs.{run_name} report cannot be re-extracted: {exc}")
            continue
        if sha256_bytes(report_raw) != run.get("report_sha256"):
            errors.append(f"runs.{run_name}.report_sha256 does not match report.md")
        if extracted != run.get("claims"):
            errors.append(f"runs.{run_name}.claims do not match archived report.md")
    return tuple(errors)


def verify_campaign_dir(
    campaign_dir: Path, source_path: Path | None = None
) -> VerificationResult:
    errors: list[str] = []
    warnings: list[str] = []
    if campaign_dir.is_symlink():
        return VerificationResult(False, ("campaign directory must not be a symbolic link",), ())
    manifest_path = campaign_dir / "campaign.json"
    if manifest_path.is_symlink():
        return VerificationResult(False, ("campaign.json must not be a symbolic link",), ())
    try:
        manifest_value = parse_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        return VerificationResult(False, (f"cannot read campaign.json: {exc}",), ())
    if not isinstance(manifest_value, dict):
        return VerificationResult(False, ("campaign.json must contain an object",), ())
    manifest: dict[str, object] = manifest_value
    schema_version = manifest.get("schema_version")
    if schema_version not in {1, 2}:
        errors.append("campaign schema_version must be 1 or 2")
    elif schema_version == 1:
        warnings.append("legacy campaign schema_version 1 has no explicit run matrix")

    source_name = manifest.get("source_name")
    if (
        not isinstance(source_name, str)
        or not source_name
        or "/" in source_name
        or "\\" in source_name
    ):
        errors.append("campaign source_name must be a non-empty basename")
    if not _valid_sha256(manifest.get("source_sha256")):
        errors.append("campaign source_sha256 is invalid")
    if source_path is None:
        warnings.append("source bytes not supplied; campaign source_sha256 was not rechecked")
    else:
        try:
            source_raw = source_path.read_bytes()
        except OSError as exc:
            errors.append(f"cannot read source file: {exc}")
        else:
            if source_path.name != source_name:
                errors.append("campaign source name does not match supplied source")
            if sha256_bytes(source_raw) != manifest.get("source_sha256"):
                errors.append("supplied source bytes do not match campaign source_sha256")

    created_at = _parse_timestamp(manifest.get("created_at"))
    completed_at = _parse_timestamp(manifest.get("completed_at"))
    if created_at is None or completed_at is None:
        errors.append("campaign timestamps must be timezone-aware ISO-8601 values")
    elif completed_at < created_at:
        errors.append("campaign completed_at cannot be earlier than created_at")

    _verify_artifact(
        campaign_dir, manifest, "SUMMARY.md", "summary_sha256", True, errors
    )
    template_hash = manifest.get("scorecard_template_sha256")
    scorecard_path = campaign_dir / "scorecard.json"
    if template_hash is not None:
        if not _valid_sha256(template_hash):
            errors.append("scorecard_template_sha256 is invalid")
        elif scorecard_path.is_symlink():
            errors.append("scorecard.json must not be a symbolic link")
        elif not scorecard_path.is_file():
            errors.append("scorecard.json is missing")
        elif sha256_bytes(scorecard_path.read_bytes()) != template_hash:
            warnings.append(
                "scorecard.json differs from its blank template, likely because it was filled"
            )
        if scorecard_path.is_file() and not scorecard_path.is_symlink():
            try:
                scorecard = parse_json(scorecard_path.read_text(encoding="utf-8"))
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                DuplicateJsonKeyError,
            ) as exc:
                errors.append(f"scorecard.json cannot be read: {exc}")
            else:
                errors.extend(verify_scorecard_provenance(scorecard_path, scorecard))
    elif scorecard_path.exists():
        warnings.append("scorecard.json exists but this campaign did not create a template")

    records = manifest.get("runs")
    if not isinstance(records, list) or not records:
        errors.append("campaign runs must be a non-empty list")
        records = []
    repeat = manifest.get("repeat")
    if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat <= 0:
        errors.append("campaign repeat must be a positive integer")
        repeat = 0
    planned_protocols: list[str] = []
    if schema_version == 2:
        raw_protocols = manifest.get("protocols")
        if (
            not isinstance(raw_protocols, list)
            or not raw_protocols
            or any(
                not isinstance(protocol, str) or protocol not in PROTOCOLS
                for protocol in raw_protocols
            )
            or len(set(raw_protocols)) != len(raw_protocols)
        ):
            errors.append("campaign protocols must be a non-empty unique protocol list")
        else:
            planned_protocols = raw_protocols
        timeout = manifest.get("timeout_seconds")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            errors.append("campaign timeout_seconds must be positive and finite")
        max_output = manifest.get("max_output_bytes")
        if (
            not isinstance(max_output, int)
            or isinstance(max_output, bool)
            or max_output <= 0
        ):
            errors.append("campaign max_output_bytes must be a positive integer")

    campaign_executor = manifest.get("executor")
    if not isinstance(campaign_executor, dict):
        errors.append("campaign executor must contain redacted metadata")
    else:
        command = campaign_executor.get("command")
        argument_count = campaign_executor.get("argument_count")
        if (
            not isinstance(command, str)
            or not command
            or "/" in command
            or "\\" in command
        ):
            errors.append("campaign executor.command must be a non-empty basename")
        if (
            not isinstance(argument_count, int)
            or isinstance(argument_count, bool)
            or argument_count < 0
        ):
            errors.append("campaign executor.argument_count must be non-negative")

    labels: set[str] = set()
    run_paths: set[str] = set()
    observed_runs: set[tuple[str, int]] = set()
    label_prefix = {
        "critic-individualist": "I",
        "critic-contrastivist": "C",
        "citation-auditor": "A",
        "critic-generic": "G",
    }
    for index, record in enumerate(records):
        item = f"runs[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{item} must be an object")
            continue
        label = record.get("label")
        if not isinstance(label, str) or not label:
            errors.append(f"{item}.label must be a non-empty string")
        elif label in labels:
            errors.append(f"duplicate campaign label: {label}")
        else:
            labels.add(label)
        protocol = record.get("protocol")
        repetition = record.get("repetition")
        if not isinstance(protocol, str) or protocol not in PROTOCOLS:
            errors.append(f"{item}.protocol is invalid")
        elif (
            not isinstance(repetition, int)
            or isinstance(repetition, bool)
            or repetition <= 0
            or (repeat and repetition > repeat)
        ):
            errors.append(f"{item}.repetition is invalid")
        else:
            run_key = (protocol, repetition)
            if run_key in observed_runs:
                errors.append(f"duplicate campaign run: {run_key}")
            observed_runs.add(run_key)
            if label != f"{label_prefix[protocol]}{repetition}":
                errors.append(f"{item}.label does not match protocol and repetition")
        relative = record.get("run_dir")
        run_dir = _safe_campaign_run_path(campaign_dir, relative)
        if run_dir is None or relative in run_paths:
            errors.append(f"{item}.run_dir is unsafe or duplicated: {relative!r}")
            continue
        run_paths.add(relative)
        child_manifest_path = run_dir / "manifest.json"
        if child_manifest_path.is_symlink():
            errors.append(f"{item} manifest.json must not be a symbolic link")
            continue
        try:
            child_manifest_bytes = child_manifest_path.read_bytes()
            child_manifest = parse_json(child_manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError) as exc:
            errors.append(f"{item} manifest cannot be read: {exc}")
            continue
        if sha256_bytes(child_manifest_bytes) != record.get("manifest_sha256"):
            errors.append(f"{item} manifest_sha256 mismatch")
        if not isinstance(child_manifest, dict):
            errors.append(f"{item} manifest must contain an object")
            continue
        if child_manifest.get("source_name") != source_name:
            errors.append(f"{item} source_name does not match campaign")
        if child_manifest.get("source_sha256") != manifest.get("source_sha256"):
            errors.append(f"{item} source_sha256 does not match campaign")
        if child_manifest.get("executor") != campaign_executor:
            errors.append(f"{item} executor metadata does not match campaign")
        if schema_version == 2:
            if child_manifest.get("timeout_seconds") != manifest.get("timeout_seconds"):
                errors.append(f"{item} timeout_seconds does not match campaign")
            if child_manifest.get("max_output_bytes") != manifest.get("max_output_bytes"):
                errors.append(f"{item} max_output_bytes does not match campaign")
        for record_key, manifest_key in (
            ("protocol", "protocol"),
            ("status", "status"),
            ("runner_exit_code", "runner_exit_code"),
        ):
            if record.get(record_key) != child_manifest.get(manifest_key):
                errors.append(f"{item}.{record_key} does not match its run manifest")
        child = verify_run_dir(run_dir, source_path)
        errors.extend(f"{label or item}: {error}" for error in child.errors)
        warnings.extend(f"{label or item}: {warning}" for warning in child.warnings)

    if schema_version == 2 and planned_protocols and repeat:
        expected_runs = {
            (protocol, repetition)
            for protocol in planned_protocols
            for repetition in range(1, repeat + 1)
        }
        if observed_runs != expected_runs:
            errors.append(
                "campaign run matrix mismatch: "
                f"missing={sorted(expected_runs - observed_runs)}, "
                f"extra={sorted(observed_runs - expected_runs)}"
            )

    return VerificationResult(not errors, tuple(errors), tuple(warnings))


def init_scorecard_command(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    atomic_write_text(output, json.dumps(scorecard_template(), indent=2) + "\n")
    print(output)
    return 0


def score_command(args: argparse.Namespace) -> int:
    scorecard_path = Path(args.scorecard).resolve()
    try:
        if Path(args.scorecard).is_symlink():
            raise ScorecardError("scorecard path must not be a symbolic link")
        scorecard = parse_json(scorecard_path.read_text(encoding="utf-8"))
        result = score_divergence(scorecard)
        provenance_errors = verify_scorecard_provenance(scorecard_path, scorecard)
        if provenance_errors:
            raise ScorecardError("; ".join(provenance_errors))
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateJsonKeyError,
        ScorecardError,
    ) as exc:
        print(f"scorecard error: {exc}", file=sys.stderr)
        return EXIT_INVALID_SCORECARD
    output = (
        score_markdown(result)
        if args.format == "markdown"
        else json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    if args.output:
        output_path = Path(args.output).resolve()
        atomic_write_text(output_path, output)
        print(output_path)
    else:
        print(output, end="")
    return 0


def validate_command(args: argparse.Namespace) -> int:
    report_path = Path(args.report).resolve()
    report, _ = read_utf8(report_path)
    validation = validate_report(args.protocol, report)
    if validation.valid:
        print("valid")
        return 0
    _print_validation_errors(validation)
    return EXIT_INVALID_REPORT


def verify_run_command(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).absolute()
    source_path = Path(args.source).resolve() if args.source else None
    verification = verify_run_dir(run_dir, source_path)
    for warning in verification.warnings:
        print(f"verification warning: {warning}", file=sys.stderr)
    if verification.valid:
        print("verified")
        return 0
    for error in verification.errors:
        print(f"verification error: {error}", file=sys.stderr)
    return EXIT_INVALID_ARCHIVE


def verify_campaign_command(args: argparse.Namespace) -> int:
    campaign_dir = Path(args.campaign_dir).absolute()
    source_path = Path(args.source).resolve() if args.source else None
    verification = verify_campaign_dir(campaign_dir, source_path)
    for warning in verification.warnings:
        print(f"verification warning: {warning}", file=sys.stderr)
    if verification.valid:
        print("verified")
        return 0
    for error in verification.errors:
        print(f"verification error: {error}", file=sys.stderr)
    return EXIT_INVALID_ARCHIVE


def list_protocols(_: argparse.Namespace) -> int:
    for name in PROTOCOLS:
        suffix = " [test-only]" if name in TEST_ONLY else ""
        print(f"{name}{suffix}")
    return 0


def positive_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive finite number")
    return value


def positive_integer(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return value


def _add_run_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("protocol", choices=PROTOCOLS)
    parser.add_argument("manuscript", help="UTF-8 manuscript path")
    parser.add_argument(
        "--runs-dir",
        default=".critic-runs",
        help="archive directory (default: .critic-runs)",
    )
    parser.add_argument(
        "--allow-test-artifact",
        action="store_true",
        help="allow critic-generic; only use this for the divergence test",
    )


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        description="Run critic protocols without depending on Claude Code."
    )
    sub = top.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list available protocols").set_defaults(func=list_protocols)

    prepare_parser = sub.add_parser(
        "prepare", help="archive a self-contained prompt for manual use"
    )
    _add_run_inputs(prepare_parser)
    prepare_parser.set_defaults(func=prepare)

    run_parser = sub.add_parser(
        "run", help="run one protocol through an external stdin/stdout command"
    )
    _add_run_inputs(run_parser)
    run_parser.add_argument(
        "--timeout",
        type=positive_seconds,
        default=900.0,
        help="terminate the executor after this many seconds (default: 900)",
    )
    run_parser.add_argument(
        "--max-output-bytes",
        type=positive_integer,
        default=DEFAULT_MAX_OUTPUT_BYTES,
        help="terminate after this many combined stdout/stderr bytes (default: 16777216)",
    )
    run_parser.set_defaults(func=run)

    campaign_parser = sub.add_parser(
        "campaign",
        help="run a serial, isolated multi-protocol calibration campaign",
    )
    campaign_parser.add_argument("manuscript", help="UTF-8 manuscript path")
    campaign_parser.add_argument(
        "--protocol",
        action="append",
        choices=PROTOCOLS,
        help="protocol to include; repeat this option (default: individualist and contrastivist)",
    )
    campaign_parser.add_argument(
        "--repeat",
        type=positive_integer,
        default=2,
        help="serial repetitions per protocol (default: 2)",
    )
    campaign_parser.add_argument(
        "--campaigns-dir",
        default=".critic-campaigns",
        help="campaign archive directory (default: .critic-campaigns)",
    )
    campaign_parser.add_argument(
        "--allow-test-artifact",
        action="store_true",
        help="allow critic-generic for second-stage calibration",
    )
    campaign_parser.add_argument(
        "--timeout", type=positive_seconds, default=900.0
    )
    campaign_parser.add_argument(
        "--max-output-bytes",
        type=positive_integer,
        default=DEFAULT_MAX_OUTPUT_BYTES,
    )
    campaign_parser.set_defaults(func=campaign)

    validate_parser = sub.add_parser(
        "validate", help="validate the deterministic report structure"
    )
    validate_parser.add_argument("protocol", choices=PROTOCOLS)
    validate_parser.add_argument("report", help="UTF-8 report path")
    validate_parser.set_defaults(func=validate_command)

    verify_parser = sub.add_parser(
        "verify-run", help="verify an archived run against its manifest"
    )
    verify_parser.add_argument("run_dir", help="archived run directory")
    verify_parser.add_argument(
        "--source",
        help="optional original source file to recheck against source_sha256",
    )
    verify_parser.set_defaults(func=verify_run_command)

    verify_campaign_parser = sub.add_parser(
        "verify-campaign", help="verify a campaign and every archived run"
    )
    verify_campaign_parser.add_argument("campaign_dir", help="campaign directory")
    verify_campaign_parser.add_argument(
        "--source", help="optional original source file to recheck"
    )
    verify_campaign_parser.set_defaults(func=verify_campaign_command)

    init_scorecard_parser = sub.add_parser(
        "init-scorecard", help="create a blank reproducible W/B scorecard"
    )
    init_scorecard_parser.add_argument("output", help="output JSON path")
    init_scorecard_parser.set_defaults(func=init_scorecard_command)

    score_parser = sub.add_parser(
        "score", help="calculate divergence intervals and the W/B verdict"
    )
    score_parser.add_argument("scorecard", help="completed scorecard JSON path")
    score_parser.add_argument(
        "--format", choices=("json", "markdown"), default="json"
    )
    score_parser.add_argument("--output", help="optional result file path")
    score_parser.set_defaults(func=score_command)

    return top


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    executor: list[str] = []
    if raw_argv and raw_argv[0] in {"run", "campaign"} and "--" in raw_argv:
        separator = raw_argv.index("--")
        executor = raw_argv[separator + 1 :]
        raw_argv = raw_argv[:separator]

    args = parser().parse_args(raw_argv)
    if args.command in {"run", "campaign"}:
        args.executor = executor
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
