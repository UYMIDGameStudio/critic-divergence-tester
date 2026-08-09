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
import secrets
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from argument_ir import (
    ArgumentIRError,
    build_argument_findings,
    build_check_plan,
    build_ir_extraction_prompt,
    canonicalize_argument_ir,
    render_check_prompt,
    validate_argument_findings,
    validate_argument_ir,
    validate_check_library,
    validate_check_plan,
    validate_check_plan_against_library,
    validate_check_results,
)
from argument_workbench import (
    PASTE_END_MARKER as IR_PASTE_END_MARKER,
    WorkbenchError,
    collect_raw_attempt,
    initialize_workspace,
    rebuild_workspace,
    run_inspector,
    selected_attempt,
    verify_workspace,
    workspace_paths,
)
from argument_review import (
    collect_review_results,
    prepare_rule_review,
    rebuild_reviews,
    show_claim_review,
)
from argument_adjudication import (
    rebuild_adjudication_cache,
    rebuild_revision_plan as rebuild_workbench_revision_plan,
    run_adjudicator as run_workbench_adjudicator,
)
from argument_gate import (
    METRIC_KEYS as GATE_A_METRIC_KEYS,
    append_assessment as append_gate_a_assessment,
    append_gate_decision,
    initialize_gate,
    rebuild_gate_report,
    verify_gate,
)
from argument_contracts import GATE_A_BURDENS, GATE_A_COMPARISONS, GATE_A_DECISIONS
from critic_execution import ExecutorResult, execute_with_limits
from critic_scoring import (
    ALL_COMPARISONS,
    BETWEEN_COMPARISONS,
    RUN_NAMES,
    WITHIN_COMPARISONS,
    ScorecardError,
    apply_blind_pairings,
    campaign_pairing_scorecard,
    create_blind_bundle,
    pairing_scorecard,
    score_divergence,
    score_markdown,
    scorecard_template,
    validate_pairing_scorecard,
)
from critic_workflow import (
    WorkflowError,
    adjudication_template,
    revision_plan_markdown,
    validate_adjudication,
)


ROOT = Path(__file__).resolve().parent
IR_SOCIAL_SCIENCE_RULES = ROOT / "ir" / "social-science-checks.json"

PROTOCOLS = {
    "critic-social-science": ROOT / "critic-social-science.md",
    "critic-natural-science": ROOT / "critic-natural-science.md",
    "critic-engineering": ROOT / "critic-engineering.md",
    "critic-individualist": ROOT / "critic-individualist.md",
    "critic-contrastivist": ROOT / "critic-contrastivist.md",
    "citation-auditor": ROOT / "citation-auditor.md",
    "critic-generic": ROOT / "test" / "critic-generic.md",
}

TEST_ONLY = {"critic-generic"}
PROTOCOL_PREFIX = {
    "critic-social-science": "S",
    "critic-natural-science": "N",
    "critic-engineering": "E",
    "critic-individualist": "I",
    "critic-contrastivist": "C",
    "citation-auditor": "A",
    "critic-generic": "G",
}
CRITIC_PROTOCOLS = {
    "critic-social-science",
    "critic-natural-science",
    "critic-engineering",
    "critic-individualist",
    "critic-contrastivist",
    "critic-generic",
}

ACADEMIC_TRACKS = {
    "humanities-social-science": {
        "label": "文科·社会科学",
        "primary": "critic-social-science",
        "specialists": ("critic-individualist", "critic-contrastivist"),
    },
    "natural-science": {
        "label": "理科·自然科学",
        "primary": "critic-natural-science",
        "specialists": (),
    },
    "engineering": {
        "label": "工科·工程学",
        "primary": "critic-engineering",
        "specialists": (),
    },
}

QUICKSTART_TRACK_ALIASES = {
    "1": "humanities-social-science",
    "文科": "humanities-social-science",
    "社会科学": "humanities-social-science",
    "文科社会科学": "humanities-social-science",
    "humanities-social-science": "humanities-social-science",
    "2": "natural-science",
    "理科": "natural-science",
    "自然科学": "natural-science",
    "natural-science": "natural-science",
    "3": "engineering",
    "工科": "engineering",
    "工程": "engineering",
    "工程学": "engineering",
    "engineering": "engineering",
}

CROSS_DISCIPLINARY_PROTOCOLS = ("citation-auditor",)

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
PREVIOUS_PLAN_PATTERN = re.compile(
    r"revision-plan\.previous-([0-9a-f]{12})(?:-([2-9][0-9]*))?\.md\Z"
)

EXIT_INVALID_REPORT = 3
EXIT_INVALID_ARCHIVE = 4
EXIT_INTERRUPTED = 130
EXIT_TIMEOUT = 124
EXIT_OUTPUT_LIMIT = 125
EXIT_INVALID_SCORECARD = 6
EXIT_CAMPAIGN_FAILED = 7
EXIT_INVALID_WORKFLOW = 8
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
PASTE_END_MARKER = "::END::"
COLLECTION_METHODS = {"manual-import", "terminal-paste"}



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


@dataclass(frozen=True)
class RunOverview:
    run_dir: Path
    stage: str
    protocol: str
    source: str
    action: str
    next_action: str
    verification: VerificationResult


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


MANUSCRIPT_PATH_PLACEHOLDERS = {
    "path/to/draft.md",
    "path/to/article.md",
    "path/to/manuscript.md",
}


def resolve_manuscript_path(value: object) -> Path:
    raw = str(value).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        raw = raw[1:-1].strip()
    if not raw:
        raise ValueError("稿件路径不能为空")
    normalized = raw.replace("\\", "/").lower()
    while normalized.startswith("./") or normalized.startswith("/"):
        normalized = normalized.removeprefix("./").removeprefix("/")
    if normalized in MANUSCRIPT_PATH_PLACEHOLDERS:
        raise ValueError(
            f"你输入的是 README 示例占位路径 {raw!r}，它不是仓库自带文件。\n"
            "请替换成真实文章路径。PowerShell 示例：\n"
            '  py -3 critic_runner.py ir prepare "C:\\Users\\你的用户名\\Downloads\\文章.md"'
        )
    raw_path = Path(raw).expanduser()
    if raw_path.is_symlink():
        raise ValueError(f"稿件不能是符号链接: {raw_path}")
    path = raw_path.resolve()
    if not path.exists():
        raise ValueError(
            f"找不到稿件文件: {path}\n"
            f"当前工作目录: {Path.cwd()}\n"
            "请检查文件名和扩展名；PowerShell 中含空格的路径要放在双引号内。"
        )
    if not path.is_file():
        raise ValueError(f"稿件路径不是普通文件: {path}")
    return path


def read_manuscript_utf8(path: Path) -> tuple[str, bytes]:
    try:
        text, raw = read_utf8(path)
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"稿件不是 UTF-8 编码: {path}\n"
            "请用记事本的“另存为”功能选择 UTF-8 后重试。"
        ) from exc
    if not text.strip():
        raise ValueError(
            f"稿件文件是空的，无法抽取或审查: {path}\n"
            "请先用记事本粘贴正文并保存为 UTF-8，然后重新运行命令。"
        )
    return text, raw


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


def normalize_executor_label(raw_label: object) -> str | None:
    if raw_label is None:
        return None
    if (
        not isinstance(raw_label, str)
        or not raw_label
        or len(raw_label) > 256
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in raw_label
        )
    ):
        raise ValueError("executor label must be 1..256 printable characters")
    return raw_label


def executor_metadata(
    executor: list[str] | None, label: str | None = None
) -> dict[str, object] | None:
    """Record useful executor identity without persisting possibly secret arguments."""
    if not executor:
        return None
    metadata: dict[str, object] = {
        "command": Path(executor[0]).name,
        "argument_count": max(0, len(executor) - 1),
    }
    if label is not None:
        metadata["label"] = label
    return metadata


def campaign_schedule(
    protocols: list[str], repeat: int, order_seed: str
) -> list[tuple[str, int]]:
    """Build a stable counterbalanced order without relying on random internals."""
    base = sorted(
        protocols,
        key=lambda protocol: (
            sha256_text(f"counterbalanced-v1\0{order_seed}\0{protocol}"),
            protocol,
        ),
    )
    schedule: list[tuple[str, int]] = []
    for repetition in range(1, repeat + 1):
        round_protocols = base if repetition % 2 else list(reversed(base))
        schedule.extend((protocol, repetition) for protocol in round_protocols)
    return schedule


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


def extract_critic_findings(report: str) -> list[dict[str, str]]:
    """Merge validated section-1 claims with their section-2 consequence tests."""
    lines = report.splitlines()
    sections = _section_ranges(lines)
    if sections is None:
        raise ValueError("cannot extract findings from a report with invalid headings")
    claims = extract_critic_claims(report)
    errors: list[str] = []
    consequences: dict[str, dict[str, str]] = {}
    for identifier, block in _item_blocks(sections[CRITIC_SECTIONS[1]]):
        label = f"A{identifier}"
        test = _field_value(block, "检验：", label, errors)
        conclusion = _field_value(block, "结论：", label, errors)
        if test is not None and conclusion is not None:
            consequences[label] = {"test": test, "conclusion": conclusion}
    if errors:
        raise ValueError("cannot extract findings: " + "; ".join(errors))
    claim_ids = [claim["id"] for claim in claims]
    if set(claim_ids) != set(consequences):
        raise ValueError("cannot extract findings: section 1 and section 2 IDs differ")
    return [{**claim, **consequences[claim["id"]]} for claim in claims]


def critic_report_context(report: str) -> tuple[str, str]:
    nonempty = [line.strip() for line in report.splitlines() if line.strip()]
    if len(nonempty) < 2:
        raise ValueError("critic report has no status footer")
    status_match = STATUS_PATTERN.fullmatch(nonempty[-2])
    if status_match is None or not nonempty[-1].startswith("UNVERIFIED:"):
        raise ValueError("critic report has an invalid status footer")
    return (
        status_match.group(1),
        nonempty[-1][len("UNVERIFIED:") :].strip(),
    )


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
        manifest_bytes = manifest_path.read_bytes()
        manifest_value = parse_json(manifest_bytes.decode("utf-8"))
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
    if schema_version not in {1, 2, 3}:
        errors.append("unsupported or missing schema_version; expected 1, 2, or 3")
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
        or any(ord(character) < 32 or ord(character) == 127 for character in source_name)
    ):
        errors.append("source_name must be a non-empty basename, not a path")

    status = manifest.get("status")
    allowed_statuses = {
        "prepared",
        "collected",
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
        "collected",
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
    manual_status = status in {"prepared", "collected"}
    if manual_status:
        if executor is not None:
            errors.append(f"{status} manifest must have executor=null")
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
        label = executor.get("label")
        if label is not None:
            try:
                normalize_executor_label(label)
            except ValueError as exc:
                errors.append(f"executor.label is invalid: {exc}")

    timeout_seconds = manifest.get("timeout_seconds")
    if manual_status:
        if timeout_seconds is not None:
            errors.append(f"{status} manifest must have timeout_seconds=null")
    elif (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        errors.append("executed run must have a positive finite timeout_seconds")

    if schema_version in {2, 3}:
        max_output_bytes = manifest.get("max_output_bytes")
        if manual_status:
            if max_output_bytes is not None:
                errors.append(f"{status} manifest must have max_output_bytes=null")
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
        "collected": 0,
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
    if status == "collected" and executor_code is not None:
        errors.append("collected manifest must have executor_returncode=null")
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
            if status == "collected" and not validation.valid:
                errors.append("collected manifest contains an invalid report")
            if status == "invalid_report" and validation.valid:
                errors.append("invalid_report manifest contains a valid report")

    collection = manifest.get("collection")
    if schema_version == 3:
        if "collection" not in manifest:
            errors.append("run schema_version 3 must contain collection")
        if status == "collected":
            if not isinstance(collection, dict):
                errors.append("collected manifest must contain collection metadata")
            else:
                if set(collection) != {"method", "imported_at", "source_name"}:
                    errors.append("collection metadata has unexpected fields")
                collection_method = collection.get("method")
                if collection_method not in COLLECTION_METHODS:
                    errors.append(
                        "collection.method must be manual-import or terminal-paste"
                    )
                imported_at = _parse_timestamp(collection.get("imported_at"))
                if imported_at is None:
                    errors.append("collection.imported_at must be timezone-aware ISO-8601")
                elif parsed_completed_at is not None and imported_at != parsed_completed_at:
                    errors.append("collection.imported_at must equal completed_at")
                report_source_name = collection.get("source_name")
                if (
                    not isinstance(report_source_name, str)
                    or not report_source_name
                    or Path(report_source_name).name != report_source_name
                    or "/" in report_source_name
                    or "\\" in report_source_name
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in report_source_name
                    )
                ):
                    errors.append("collection.source_name must be a basename")
                elif (
                    collection_method == "terminal-paste"
                    and report_source_name != "pasted-report.md"
                ):
                    errors.append(
                        "terminal-paste collection.source_name must be pasted-report.md"
                    )
        elif collection is not None:
            errors.append("non-collected manifest must have collection=null")
    elif status == "collected":
        errors.append("collected status requires run schema_version 3")

    adjudication_path = run_dir / "adjudication.json"
    revision_plan_path = run_dir / "revision-plan.md"
    workflow_paths = (adjudication_path, revision_plan_path)
    previous_plan_paths = list(run_dir.glob("revision-plan.previous-*.md"))
    for previous_plan_path in previous_plan_paths:
        if status != "collected" or protocol_name not in CRITIC_PROTOCOLS:
            errors.append(
                f"{previous_plan_path.name} exists outside a collected critic workflow"
            )
            continue
        if previous_plan_path.is_symlink():
            errors.append(f"{previous_plan_path.name} must not be a symbolic link")
            continue
        if not previous_plan_path.is_file():
            errors.append(f"{previous_plan_path.name} must be a regular file")
            continue
        match = PREVIOUS_PLAN_PATTERN.fullmatch(previous_plan_path.name)
        if match is None:
            errors.append(f"invalid archived revision plan name: {previous_plan_path.name}")
            continue
        try:
            previous_plan_bytes = previous_plan_path.read_bytes()
        except OSError as exc:
            errors.append(f"cannot read {previous_plan_path.name}: {exc}")
            continue
        if not sha256_bytes(previous_plan_bytes).startswith(match.group(1)):
            errors.append(
                f"{previous_plan_path.name} content does not match its hash prefix"
            )
    if status != "collected":
        for workflow_path in workflow_paths:
            if workflow_path.exists() or workflow_path.is_symlink():
                errors.append(
                    f"{workflow_path.name} exists outside a collected workflow"
                )
    elif isinstance(protocol_name, str) and protocol_name in CRITIC_PROTOCOLS:
        adjudication_value: object | None = None
        adjudication_bytes: bytes | None = None
        if adjudication_path.is_symlink():
            errors.append("adjudication.json must not be a symbolic link")
        elif not adjudication_path.is_file():
            errors.append("collected critic run is missing adjudication.json")
        else:
            try:
                adjudication_bytes = adjudication_path.read_bytes()
                adjudication_value = parse_json(adjudication_bytes.decode("utf-8"))
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                DuplicateJsonKeyError,
            ) as exc:
                errors.append(f"cannot read adjudication.json: {exc}")
            else:
                binding_errors = _adjudication_binding_errors(
                    manifest,
                    manifest_bytes,
                    report_bytes or b"",
                    adjudication_value,
                    require_complete=False,
                )
                errors.extend(
                    f"adjudication.json: {error}" for error in binding_errors
                )

        if revision_plan_path.is_symlink():
            errors.append("revision-plan.md must not be a symbolic link")
        elif revision_plan_path.exists():
            if not revision_plan_path.is_file():
                errors.append("revision-plan.md must be a regular file")
            elif adjudication_value is None or adjudication_bytes is None:
                errors.append("revision-plan.md cannot be verified without adjudication.json")
            else:
                complete_errors = _adjudication_binding_errors(
                    manifest,
                    manifest_bytes,
                    report_bytes or b"",
                    adjudication_value,
                    require_complete=True,
                )
                if complete_errors:
                    errors.append(
                        "revision-plan.md exists before adjudication is valid and complete"
                    )
                else:
                    expected_plan = revision_plan_markdown(
                        adjudication_value,
                        adjudication_sha256=sha256_bytes(adjudication_bytes),
                    )
                    try:
                        actual_plan = revision_plan_path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError) as exc:
                        errors.append(f"cannot read revision-plan.md: {exc}")
                    else:
                        if actual_plan != expected_plan:
                            errors.append(
                                "revision-plan.md does not match the current adjudication"
                            )
    else:
        for workflow_path in workflow_paths:
            if workflow_path.exists() or workflow_path.is_symlink():
                errors.append(
                    f"{workflow_path.name} is not supported for protocol {protocol_name!r}"
                )

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
    executor_label: str | None = None,
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
        "schema_version": 3,
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
        "executor": executor_metadata(executor, executor_label),
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "executor_returncode": executor_returncode,
        "runner_exit_code": runner_exit_code,
        "report_validation": validation.as_dict() if validation is not None else None,
        "collection": None,
    }
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(run_dir / "manifest.json", manifest_text)


def _prepare_bundle(args: argparse.Namespace) -> Path:
    source_path = resolve_manuscript_path(args.manuscript)
    source_text, source_raw = read_manuscript_utf8(source_path)
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
    return run_dir


def prepare(args: argparse.Namespace) -> int:
    run_dir = _prepare_bundle(args)
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
    executor_label = normalize_executor_label(getattr(args, "executor_label", None))

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
    source_path = resolve_manuscript_path(args.manuscript)
    source_text, source_raw = read_manuscript_utf8(source_path)
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
        executor_label=executor_label,
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
            executor_label=executor_label,
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
            executor_label=executor_label,
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
        executor_label=executor_label,
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
    executor_label = normalize_executor_label(getattr(args, "executor_label", None))

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
    raw_order_seed = getattr(args, "order_seed", None)
    if raw_order_seed is None:
        order_seed = secrets.token_hex(16)
    elif (
        not isinstance(raw_order_seed, str)
        or not raw_order_seed
        or len(raw_order_seed) > 128
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in raw_order_seed
        )
    ):
        raise ValueError("campaign order seed must be 1..128 printable characters")
    else:
        order_seed = raw_order_seed

    requested_tracks = getattr(args, "track", None)
    if args.protocol and requested_tracks:
        raise ValueError("campaign accepts either --protocol or --track, not both")
    protocols = args.protocol
    if requested_tracks:
        protocols = [
            str(ACADEMIC_TRACKS[track]["primary"]) for track in requested_tracks
        ]
    protocols = protocols or ["critic-individualist", "critic-contrastivist"]
    if len(set(protocols)) != len(protocols):
        raise ValueError("campaign protocols must not contain duplicates")
    for protocol_name in protocols:
        load_protocol(protocol_name, args.allow_test_artifact)

    source_path = resolve_manuscript_path(args.manuscript)
    _, source_raw = read_manuscript_utf8(source_path)
    campaign_started_at = utc_now()
    campaign_dir = new_run_dir(Path(args.campaigns_dir), "campaign")
    runs_dir = campaign_dir / "runs"
    runs_dir.mkdir()
    if os.name == "posix":
        os.chmod(runs_dir, 0o700)

    schedule = campaign_schedule(protocols, args.repeat, order_seed)
    records: list[dict[str, object]] = []
    for protocol_name, repetition in schedule:
        label = f"{PROTOCOL_PREFIX[protocol_name]}{repetition}"
        run_args = argparse.Namespace(
            protocol=protocol_name,
            manuscript=str(source_path),
            runs_dir=str(runs_dir),
            allow_test_artifact=args.allow_test_artifact,
            executor=executor,
            executor_label=executor_label,
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
        "schema_version": 3,
        "source_name": source_path.name,
        "source_sha256": sha256_bytes(source_raw),
        "created_at": campaign_started_at,
        "completed_at": completed_at,
        "executor": executor_metadata(executor, executor_label),
        "protocols": protocols,
        "repeat": args.repeat,
        "order_strategy": "counterbalanced-v1",
        "order_seed": order_seed,
        "execution_order": [
            f"{PROTOCOL_PREFIX[protocol]}{repetition}"
            for protocol, repetition in schedule
        ],
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "runs": records,
    }
    can_score = (
        len(protocols) >= 2
        and args.repeat >= 2
        and all(protocol in CRITIC_PROTOCOLS for protocol in protocols)
        and all(record["status"] == "succeeded" for record in records)
    )
    if can_score:
        score_runs: dict[str, dict[str, object]] = {}
        records_by_run = {
            (str(record["protocol"]), int(record["repetition"])): record
            for record in records
        }
        for protocol_name in protocols:
            for repetition in range(1, args.repeat + 1):
                record = records_by_run[(protocol_name, repetition)]
                label = str(record["label"])
                run_dir = campaign_dir / str(record["run_dir"])
                run_manifest = parse_json(
                    (run_dir / "manifest.json").read_text(encoding="utf-8")
                )
                report, _ = read_utf8(run_dir / "report.md")
                score_runs[label] = {
                    "protocol": protocol_name,
                    "repetition": repetition,
                    "archive": record["run_dir"],
                    "report_sha256": run_manifest["report_sha256"],
                    "claims": extract_critic_claims(report),
                }
        template = campaign_pairing_scorecard(score_runs)
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
                "Create a blinded reviewer artifact and keep its identity key private:",
                "",
                "```bash",
                "python critic_runner.py blind-scorecard path/to/scorecard.json",
                "```",
                "This creates blind-review.json and blind-key.json beside the scorecard.",
                "",
                "After pairing, verify and merge the reviewer artifact:",
                "",
                "```bash",
                "python critic_runner.py apply-blind-scorecard path/to/scorecard.json",
                "```",
                "",
                "```bash",
                "python critic_runner.py score path/to/completed-scorecard.json --format markdown",
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
    """Re-extract immutable claims from sibling campaign archives."""
    if not isinstance(scorecard, dict) or scorecard.get("schema_version") not in {2, 3}:
        return ()
    errors: list[str] = []
    runs = scorecard.get("runs")
    if not isinstance(runs, dict):
        return ("traceable scorecard runs must be an object",)
    if scorecard.get("schema_version") == 2:
        run_names = RUN_NAMES
        campaign_records: dict[str, dict[str, object]] = {}
    else:
        raw_order = scorecard.get("run_order")
        if (
            not isinstance(raw_order, list)
            or any(not isinstance(name, str) for name in raw_order)
            or len(raw_order) != len(set(raw_order))
        ):
            return ("schema v3 scorecard run_order must be a unique string list",)
        run_names = tuple(raw_order)
        missing = [name for name in runs if name not in run_names]
        extra = [name for name in run_names if name not in runs]
        if missing or extra:
            errors.append(
                f"schema v3 scorecard run_order mismatch; missing={missing}, extra={extra}"
            )
    campaign_dir = scorecard_path.parent
    if scorecard.get("schema_version") == 3:
        campaign_path = campaign_dir / "campaign.json"
        if campaign_path.is_symlink():
            errors.append("campaign.json must not be a symbolic link")
            campaign_records = {}
        else:
            try:
                campaign = parse_json(campaign_path.read_text(encoding="utf-8"))
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                DuplicateJsonKeyError,
            ) as exc:
                errors.append(f"campaign.json cannot bind scorecard identity: {exc}")
                campaign_records = {}
            else:
                raw_records = campaign.get("runs") if isinstance(campaign, dict) else None
                if not isinstance(raw_records, list):
                    errors.append("campaign runs cannot bind scorecard identity")
                    campaign_records = {}
                else:
                    campaign_records = {
                        str(record.get("label")): record
                        for record in raw_records
                        if isinstance(record, dict)
                        and isinstance(record.get("label"), str)
                    }
                    raw_protocols = campaign.get("protocols")
                    repeat = campaign.get("repeat")
                    if (
                        isinstance(raw_protocols, list)
                        and all(
                            isinstance(protocol, str)
                            and protocol in PROTOCOL_PREFIX
                            for protocol in raw_protocols
                        )
                        and isinstance(repeat, int)
                        and not isinstance(repeat, bool)
                        and repeat > 0
                    ):
                        expected_order = tuple(
                            f"{PROTOCOL_PREFIX[protocol]}{repetition}"
                            for protocol in raw_protocols
                            for repetition in range(1, repeat + 1)
                        )
                        if run_names != expected_order:
                            errors.append(
                                "schema v3 scorecard run_order does not match campaign plan"
                            )
    for run_name in run_names:
        run = runs.get(run_name)
        if not isinstance(run, dict):
            errors.append(f"runs.{run_name} must be an object")
            continue
        run_dir = _safe_campaign_run_path(campaign_dir, run.get("archive"))
        if run_dir is None:
            errors.append(f"runs.{run_name}.archive is unsafe or outside the campaign")
            continue
        if scorecard.get("schema_version") == 3:
            record = campaign_records.get(run_name)
            if record is None:
                errors.append(f"runs.{run_name} has no matching campaign record")
            else:
                for field in ("protocol", "repetition"):
                    if run.get(field) != record.get(field):
                        errors.append(
                            f"runs.{run_name}.{field} does not match campaign record"
                        )
                if run.get("archive") != record.get("run_dir"):
                    errors.append(
                        f"runs.{run_name}.archive does not match campaign record"
                    )
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
    if schema_version not in {1, 2, 3}:
        errors.append("campaign schema_version must be 1, 2, or 3")
    elif schema_version == 1:
        warnings.append("legacy campaign schema_version 1 has no explicit run matrix")
    elif schema_version == 2:
        warnings.append("legacy campaign schema_version 2 has a fixed execution order")

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
                if isinstance(scorecard, dict) and scorecard.get("schema_version") in {
                    2,
                    3,
                }:
                    try:
                        validate_pairing_scorecard(scorecard)
                    except ScorecardError as exc:
                        errors.append(f"scorecard.json structure is invalid: {exc}")
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
    if schema_version in {2, 3}:
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

    declared_execution_order: list[str] = []
    if schema_version == 3:
        order_seed = manifest.get("order_seed")
        execution_order = manifest.get("execution_order")
        if manifest.get("order_strategy") != "counterbalanced-v1":
            errors.append("campaign order_strategy must be counterbalanced-v1")
        if (
            not isinstance(order_seed, str)
            or not order_seed
            or len(order_seed) > 128
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in order_seed
            )
        ):
            errors.append("campaign order_seed must be 1..128 printable characters")
        if not isinstance(execution_order, list) or any(
            not isinstance(label, str) for label in execution_order
        ):
            errors.append("campaign execution_order must be a string list")
        else:
            declared_execution_order = execution_order
        if planned_protocols and repeat and isinstance(order_seed, str):
            expected_order = [
                f"{PROTOCOL_PREFIX[protocol]}{repetition}"
                for protocol, repetition in campaign_schedule(
                    planned_protocols, repeat, order_seed
                )
            ]
            if declared_execution_order != expected_order:
                errors.append("campaign execution_order does not match its seed and plan")

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
        label = campaign_executor.get("label")
        if label is not None:
            try:
                normalize_executor_label(label)
            except ValueError as exc:
                errors.append(f"campaign executor.label is invalid: {exc}")

    labels: set[str] = set()
    run_paths: set[str] = set()
    observed_runs: set[tuple[str, int]] = set()
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
            if label != f"{PROTOCOL_PREFIX[protocol]}{repetition}":
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
        if schema_version in {2, 3}:
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

    if schema_version in {2, 3} and planned_protocols and repeat:
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
    if schema_version == 3:
        observed_execution_order = [
            str(record.get("label"))
            for record in records
            if isinstance(record, dict)
        ]
        if observed_execution_order != declared_execution_order:
            errors.append("campaign run record order does not match execution_order")

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


def _read_scorecard_json(path: Path, label: str) -> object:
    if path.is_symlink():
        raise ScorecardError(f"{label} path must not be a symbolic link")
    try:
        return parse_json(path.read_text(encoding="utf-8"))
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateJsonKeyError,
    ) as exc:
        raise ScorecardError(f"{label} cannot be read: {exc}") from exc


def blind_scorecard_command(args: argparse.Namespace) -> int:
    raw_scorecard_path = Path(args.scorecard)
    scorecard_path = raw_scorecard_path.resolve()
    raw_blind_path = (
        Path(args.output)
        if getattr(args, "output", None)
        else scorecard_path.parent / "blind-review.json"
    )
    raw_key_path = (
        Path(args.key_output)
        if getattr(args, "key_output", None)
        else scorecard_path.parent / "blind-key.json"
    )
    blind_path = raw_blind_path.resolve()
    key_path = raw_key_path.resolve()
    try:
        if raw_scorecard_path.is_symlink():
            raise ScorecardError("scorecard path must not be a symbolic link")
        if len({scorecard_path, blind_path, key_path}) != 3:
            raise ScorecardError("scorecard, blind output, and key output must differ")
        if raw_blind_path.is_symlink() or raw_key_path.is_symlink():
            raise ScorecardError("blind and key outputs must not be symbolic links")
        if blind_path.exists() or key_path.exists():
            raise ScorecardError(
                "blind or key output already exists; choose new paths to avoid data loss"
            )
        scorecard = _read_scorecard_json(scorecard_path, "scorecard")
        provenance_errors = verify_scorecard_provenance(scorecard_path, scorecard)
        if provenance_errors:
            raise ScorecardError("; ".join(provenance_errors))
        seed = args.seed if args.seed is not None else secrets.token_hex(16)
        blind, key = create_blind_bundle(scorecard, seed)
        atomic_write_text(
            key_path,
            json.dumps(key, ensure_ascii=False, indent=2) + "\n",
        )
        atomic_write_text(
            blind_path,
            json.dumps(blind, ensure_ascii=False, indent=2) + "\n",
        )
    except (OSError, ValueError) as exc:
        print(f"blind scorecard error: {exc}", file=sys.stderr)
        return EXIT_INVALID_SCORECARD
    print(blind_path)
    print(key_path)
    return 0


def apply_blind_scorecard_command(args: argparse.Namespace) -> int:
    raw_scorecard_path = Path(args.scorecard)
    scorecard_path = raw_scorecard_path.resolve()
    raw_blind_path = (
        Path(args.blind)
        if getattr(args, "blind", None)
        else scorecard_path.parent / "blind-review.json"
    )
    raw_key_path = (
        Path(args.key)
        if getattr(args, "key", None)
        else scorecard_path.parent / "blind-key.json"
    )
    blind_path = raw_blind_path.resolve()
    key_path = raw_key_path.resolve()
    raw_output_path = (
        Path(args.output)
        if getattr(args, "output", None)
        else scorecard_path.parent / "completed-scorecard.json"
    )
    output_path = raw_output_path.resolve()
    try:
        if any(
            path.is_symlink()
            for path in (raw_scorecard_path, raw_blind_path, raw_key_path)
        ):
            raise ScorecardError(
                "scorecard, blind artifact, and key must not be symbolic links"
            )
        if output_path in {scorecard_path, blind_path, key_path}:
            raise ScorecardError("output must not overwrite an input artifact")
        if output_path.parent != scorecard_path.parent:
            raise ScorecardError(
                "output must stay beside the original scorecard to preserve campaign provenance"
            )
        if raw_output_path.is_symlink():
            raise ScorecardError("output must not be a symbolic link")
        if output_path.exists():
            raise ScorecardError("output already exists; choose a new path to avoid data loss")
        scorecard = _read_scorecard_json(scorecard_path, "scorecard")
        blind = _read_scorecard_json(blind_path, "blind artifact")
        key = _read_scorecard_json(key_path, "blind key")
        provenance_errors = verify_scorecard_provenance(scorecard_path, scorecard)
        if provenance_errors:
            raise ScorecardError("; ".join(provenance_errors))
        merged = apply_blind_pairings(scorecard, blind, key)
        atomic_write_text(
            output_path,
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        )
    except (OSError, ValueError) as exc:
        print(f"apply blind scorecard error: {exc}", file=sys.stderr)
        return EXIT_INVALID_SCORECARD
    print(output_path)
    return 0


def import_report_command(args: argparse.Namespace) -> int:
    raw_run_dir = Path(args.run_dir)
    raw_report_path = Path(args.report)
    run_dir = raw_run_dir.resolve()
    report_path = raw_report_path.resolve()
    manifest_path = run_dir / "manifest.json"
    archived_report_path = run_dir / "report.md"
    adjudication_path = (
        Path(args.adjudication_output).resolve()
        if getattr(args, "adjudication_output", None)
        else run_dir / "adjudication.json"
    )
    collection_method = getattr(args, "collection_method", "manual-import")
    collection_source_name = getattr(args, "collection_source_name", report_path.name)

    if collection_method not in COLLECTION_METHODS:
        print("import report error: unknown collection method", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    if collection_method == "terminal-paste":
        if collection_source_name != "pasted-report.md":
            print(
                "import report error: terminal paste must use pasted-report.md",
                file=sys.stderr,
            )
            return EXIT_INVALID_WORKFLOW
    elif collection_source_name != report_path.name:
        print(
            "import report error: imported source name must match the report file",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW

    if raw_run_dir.is_symlink() or raw_report_path.is_symlink():
        print(
            "import report error: run and report paths must not be symbolic links",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW
    if not report_path.is_file():
        print(
            f"import report error: report file does not exist: {report_path}",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW
    if report_path == run_dir or run_dir in report_path.parents:
        print(
            "import report error: source report must be outside the prepared run",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW
    if archived_report_path.exists() or archived_report_path.is_symlink():
        print(
            "import report error: archived report already exists; refusing to overwrite",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW
    if adjudication_path.parent != run_dir:
        print(
            "import report error: adjudication must stay inside the run directory",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW
    if adjudication_path.exists() or adjudication_path.is_symlink():
        print("import report error: adjudication output already exists", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW

    verification = verify_run_dir(run_dir)
    if not verification.valid:
        for error in verification.errors:
            print(f"import report archive error: {error}", file=sys.stderr)
        return EXIT_INVALID_ARCHIVE

    try:
        old_manifest_bytes = manifest_path.read_bytes()
        manifest_value = parse_json(old_manifest_bytes.decode("utf-8"))
        report_bytes = report_path.read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        print(f"import report error: {exc}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    if not isinstance(manifest_value, dict):
        print("import report error: manifest must be an object", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    manifest: dict[str, object] = manifest_value
    if manifest.get("status") != "prepared":
        print("import report error: only a prepared run can collect a report", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    if len(report_bytes) > DEFAULT_MAX_OUTPUT_BYTES:
        print(
            f"import report error: report exceeds {DEFAULT_MAX_OUTPUT_BYTES} bytes",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW

    protocol = manifest.get("protocol")
    assert isinstance(protocol, str)
    try:
        report_text = report_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        print(f"import report error: report is not UTF-8: {exc}", file=sys.stderr)
        return EXIT_INVALID_REPORT
    validation = validate_report(protocol, report_text)
    if not validation.valid:
        _print_validation_errors(validation)
        print("report was not imported; fix it and retry", file=sys.stderr)
        return EXIT_INVALID_REPORT

    imported_at = utc_now()
    manifest.update(
        {
            "schema_version": 3,
            "report_sha256": sha256_bytes(report_bytes),
            "completed_at": imported_at,
            "status": "collected",
            "executor": None,
            "timeout_seconds": None,
            "max_output_bytes": None,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "executor_returncode": None,
            "runner_exit_code": 0,
            "report_validation": validation.as_dict(),
            "collection": {
                "method": collection_method,
                "imported_at": imported_at,
                "source_name": collection_source_name,
            },
        }
    )
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    adjudication_bytes: bytes | None = None
    if protocol in CRITIC_PROTOCOLS:
        try:
            findings = extract_critic_findings(report_text)
            report_status, unverified = critic_report_context(report_text)
        except ValueError as exc:
            print(f"import report error: {exc}", file=sys.stderr)
            return EXIT_INVALID_WORKFLOW
        adjudication = adjudication_template(
            protocol=protocol,
            report_sha256=sha256_bytes(report_bytes),
            manifest_sha256=sha256_bytes(manifest_bytes),
            findings=findings,
            report_status=report_status,
            unverified=unverified,
        )
        adjudication_bytes = (
            json.dumps(adjudication, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

    created_paths: list[Path] = []
    try:
        atomic_write_bytes(archived_report_path, report_bytes)
        created_paths.append(archived_report_path)
        if adjudication_bytes is not None:
            atomic_write_bytes(adjudication_path, adjudication_bytes)
            created_paths.append(adjudication_path)
        atomic_write_bytes(manifest_path, manifest_bytes)
    except OSError as exc:
        for created_path in created_paths:
            created_path.unlink(missing_ok=True)
        try:
            atomic_write_bytes(manifest_path, old_manifest_bytes)
        except OSError:
            pass
        print(f"import report error: {exc}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW

    post_verification = verify_run_dir(run_dir)
    if not post_verification.valid:
        for created_path in created_paths:
            created_path.unlink(missing_ok=True)
        atomic_write_bytes(manifest_path, old_manifest_bytes)
        for error in post_verification.errors:
            print(f"import report postcondition error: {error}", file=sys.stderr)
        return EXIT_INVALID_ARCHIVE

    print(archived_report_path)
    if adjudication_bytes is not None:
        print(adjudication_path)
    return 0


def _adjudication_binding_errors(
    manifest_value: object,
    manifest_bytes: bytes,
    report_bytes: bytes,
    adjudication_value: object,
    *,
    require_complete: bool,
) -> list[str]:
    errors = validate_adjudication(
        adjudication_value, require_complete=require_complete
    )
    if not isinstance(manifest_value, dict):
        errors.append("manifest must be an object")
        return errors
    if manifest_value.get("status") != "collected":
        errors.append("run must have collected status")
    if not isinstance(adjudication_value, dict):
        return errors

    source = adjudication_value.get("source")
    protocol = manifest_value.get("protocol")
    expected_source = {
        "protocol": protocol,
        "report_sha256": sha256_bytes(report_bytes),
        "manifest_sha256": sha256_bytes(manifest_bytes),
    }
    if isinstance(protocol, str) and protocol in CRITIC_PROTOCOLS:
        report_status, unverified = critic_report_context(
            report_bytes.decode("utf-8-sig")
        )
        expected_source.update(
            {"report_status": report_status, "unverified": unverified}
        )
    if source != expected_source:
        errors.append("adjudication source does not match the collected run")
    if isinstance(protocol, str) and protocol in CRITIC_PROTOCOLS:
        expected_findings = extract_critic_findings(report_bytes.decode("utf-8-sig"))
        findings = adjudication_value.get("findings")
        if isinstance(findings, list) and len(findings) == len(expected_findings):
            immutable_keys = (
                "id",
                "position",
                "claim",
                "reason",
                "test",
                "conclusion",
            )
            for index, (actual, expected) in enumerate(
                zip(findings, expected_findings)
            ):
                if not isinstance(actual, dict) or any(
                    actual.get(key) != expected.get(key) for key in immutable_keys
                ):
                    errors.append(
                        f"findings[{index}] was edited outside decision fields"
                    )
        else:
            errors.append("adjudication findings do not match the collected report")
    else:
        errors.append("adjudication currently requires a critic protocol")
    return errors


def _python_launcher() -> str:
    return "py -3" if os.name == "nt" else "python3"


def _available_previous_plan_path(run_dir: Path, plan_bytes: bytes) -> Path:
    digest = sha256_bytes(plan_bytes)[:12]
    base = run_dir / f"revision-plan.previous-{digest}.md"
    if not base.exists() and not base.is_symlink():
        return base
    for suffix in range(2, 10_000):
        candidate = run_dir / f"revision-plan.previous-{digest}-{suffix}.md"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise OSError("too many archived revision plans")


def _archive_current_plan(run_dir: Path) -> Path | None:
    plan_path = run_dir / "revision-plan.md"
    if not plan_path.exists():
        return None
    plan_bytes = plan_path.read_bytes()
    archived_path = _available_previous_plan_path(run_dir, plan_bytes)
    os.replace(plan_path, archived_path)
    if os.name == "posix":
        os.chmod(archived_path, 0o600)
    return archived_path


def _prompt_decision(*, allow_keep: bool) -> tuple[str, str, str] | None:
    choices = {"1": "accept", "2": "reject", "3": "defer"}
    while True:
        prompt = "选择 1 接受 / 2 拒绝 / 3 暂缓"
        if allow_keep:
            prompt += " / 直接回车保留当前裁决"
        choice = input(prompt + "：").strip()
        if allow_keep and not choice:
            return None
        decision = choices.get(choice)
        if decision is not None:
            break
        print("无法识别，请输入 1、2 或 3。")

    author_reason = ""
    revision_action = ""
    if decision == "accept":
        author_reason = input("采纳理由（可直接回车）：").strip()
        while not revision_action:
            revision_action = input("具体修改动作（必填）：").strip()
            if not revision_action:
                print("接受批评时必须写清具体修改动作。")
    elif decision == "reject":
        while not author_reason:
            author_reason = input("拒绝理由（必填）：").strip()
            if not author_reason:
                print("拒绝批评时必须留下理由。")
    else:
        while not author_reason:
            author_reason = input("暂缓理由（必填）：").strip()
            if not author_reason:
                print("暂缓时必须说明缺少什么证据或判断。")
        revision_action = input("后续动作（可直接回车）：").strip()
    return decision, author_reason, revision_action


def adjudicate_command(args: argparse.Namespace) -> int:
    raw_run_dir = Path(args.run_dir)
    run_dir = raw_run_dir.resolve()
    adjudication_path = run_dir / "adjudication.json"
    if raw_run_dir.is_symlink() or adjudication_path.is_symlink():
        print("adjudication error: paths must not be symbolic links", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    verification = verify_run_dir(run_dir)
    if not verification.valid:
        for error in verification.errors:
            print(f"adjudication archive error: {error}", file=sys.stderr)
        return EXIT_INVALID_ARCHIVE
    try:
        manifest_bytes = (run_dir / "manifest.json").read_bytes()
        manifest_value = parse_json(manifest_bytes.decode("utf-8"))
        report_bytes = (run_dir / "report.md").read_bytes()
        adjudication_value = parse_json(
            adjudication_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        print(f"adjudication error: {exc}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    errors = _adjudication_binding_errors(
        manifest_value,
        manifest_bytes,
        report_bytes,
        adjudication_value,
        require_complete=False,
    )
    if errors or not isinstance(adjudication_value, dict):
        for error in errors:
            print(f"adjudication error: {error}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    findings = adjudication_value["findings"]
    assert isinstance(findings, list)
    review_all = bool(getattr(args, "review_all", False))
    print("人工裁决：AI 的批评不是结论，逐条决定是否采纳。")
    if review_all:
        print("复议模式：直接回车保留原裁决；发生修改前会自动留存旧计划。")
    else:
        print("已完成的条目会跳过；每完成一条都会立即保存。")
    archived_plan: Path | None = None
    changed = False
    for finding in findings:
        assert isinstance(finding, dict)
        completed = finding.get("decision") in {"accept", "reject", "defer"}
        if completed and not review_all:
            continue
        print(f"\n{finding['id']}  {finding['claim']}")
        print(f"位置：{finding['position']}")
        print(f"理由：{finding['reason']}")
        print(f"后果检验：{finding['test']}")
        if completed and review_all:
            labels = {"accept": "接受", "reject": "拒绝", "defer": "暂缓"}
            print(
                f"当前裁决：{labels[str(finding['decision'])]}；"
                f"理由：{finding['author_reason'] or '（无）'}；"
                f"动作：{finding['revision_action'] or '（无）'}"
            )
        try:
            decision_fields = _prompt_decision(allow_keep=completed and review_all)
        except EOFError:
            print("\n错误：裁决输入不完整。", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("\n已保存此前进度并退出。", file=sys.stderr)
            return EXIT_INTERRUPTED
        if decision_fields is None:
            print(f"已保留 {finding['id']}。")
            continue
        decision, author_reason, revision_action = decision_fields
        previous = (
            finding.get("decision"),
            finding.get("author_reason"),
            finding.get("revision_action"),
        )
        current = (decision, author_reason, revision_action)
        if previous == current:
            print(f"{finding['id']} 没有变化。")
            continue
        if not changed:
            try:
                archived_plan = _archive_current_plan(run_dir)
            except OSError as exc:
                print(
                    f"adjudication error: cannot archive current plan: {exc}",
                    file=sys.stderr,
                )
                return EXIT_INVALID_WORKFLOW
        finding["decision"] = decision
        finding["author_reason"] = author_reason
        finding["revision_action"] = revision_action
        try:
            atomic_write_text(
                adjudication_path,
                json.dumps(adjudication_value, ensure_ascii=False, indent=2) + "\n",
            )
        except OSError as exc:
            print(f"adjudication error: cannot save decision: {exc}", file=sys.stderr)
            return EXIT_INVALID_WORKFLOW
        changed = True
        print(f"已保存 {finding['id']}。")

    if archived_plan is not None:
        print(f"旧修改计划已留存：{archived_plan}")
    print("\n裁决完成，正在生成修改计划……")
    return revision_plan_command(
        argparse.Namespace(run_dir=str(run_dir), adjudication=None, output=None)
    )


def revision_plan_command(args: argparse.Namespace) -> int:
    raw_run_dir = Path(args.run_dir)
    run_dir = raw_run_dir.resolve()
    raw_adjudication_path = (
        Path(args.adjudication)
        if getattr(args, "adjudication", None)
        else run_dir / "adjudication.json"
    )
    adjudication_path = raw_adjudication_path.resolve()
    raw_output_path = (
        Path(args.output)
        if getattr(args, "output", None)
        else run_dir / "revision-plan.md"
    )
    output_path = raw_output_path.resolve()
    if any(path.is_symlink() for path in (raw_run_dir, raw_adjudication_path, raw_output_path)):
        print("revision plan error: paths must not be symbolic links", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    if adjudication_path.parent != run_dir or output_path.parent != run_dir:
        print("revision plan error: artifacts must stay inside the run directory", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    verification = verify_run_dir(run_dir)
    if not verification.valid:
        for error in verification.errors:
            print(f"revision plan archive error: {error}", file=sys.stderr)
        return EXIT_INVALID_ARCHIVE
    try:
        manifest_bytes = (run_dir / "manifest.json").read_bytes()
        manifest_value = parse_json(manifest_bytes.decode("utf-8"))
        report_bytes = (run_dir / "report.md").read_bytes()
        adjudication_bytes = adjudication_path.read_bytes()
        adjudication_value = parse_json(adjudication_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        print(f"revision plan error: {exc}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    errors = _adjudication_binding_errors(
        manifest_value,
        manifest_bytes,
        report_bytes,
        adjudication_value,
        require_complete=True,
    )
    if errors:
        for error in errors:
            print(f"revision plan error: {error}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    adjudication_sha256 = sha256_bytes(adjudication_bytes)
    try:
        markdown = revision_plan_markdown(
            adjudication_value,
            adjudication_sha256=adjudication_sha256,
        )
    except WorkflowError as exc:
        print(f"revision plan error: {exc}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    if output_path.exists():
        try:
            existing_plan = output_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"revision plan error: cannot read existing output: {exc}", file=sys.stderr)
            return EXIT_INVALID_WORKFLOW
        if existing_plan == markdown:
            print(output_path)
            return 0
        print(
            "revision plan error: output is stale or modified; "
            "move it aside or choose another --output path",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW
    try:
        atomic_write_text(output_path, markdown)
    except OSError as exc:
        print(f"revision plan error: {exc}", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    print(output_path)
    return 0


def _ir_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _ir_read_json(raw_path: Path, label: str) -> tuple[Path, object, bytes]:
    if raw_path.is_symlink():
        raise ArgumentIRError(f"{label} must not be a symlink")
    path = raw_path.resolve()
    text, data = read_utf8(path)
    try:
        return path, parse_json(text), data
    except (json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        raise ArgumentIRError(f"{label} is not strict JSON: {exc}") from exc


def _ir_preflight_output(path: Path, data: bytes, inputs: tuple[Path, ...]) -> bool:
    """Refuse ambiguous replacement; return True when an exact artifact already exists."""
    if path.is_symlink():
        raise ArgumentIRError(f"output must not be a symlink: {path}")
    resolved = path.resolve()
    if resolved in {item.resolve() for item in inputs}:
        raise ArgumentIRError(f"output must not overwrite an input artifact: {path}")
    if resolved.exists():
        if not resolved.is_file():
            raise ArgumentIRError(f"output is not a regular file: {path}")
        if resolved.read_bytes() != data:
            raise ArgumentIRError(
                f"output already exists with different content; choose another path: {path}"
            )
        return True
    if resolved.parent.exists() and not resolved.parent.is_dir():
        raise ArgumentIRError(f"output parent is not a directory: {resolved.parent}")
    return False


def _ir_write_outputs(
    artifacts: tuple[tuple[Path, bytes], ...],
    *,
    inputs: tuple[Path, ...],
) -> None:
    resolved_outputs = [path.resolve() for path, _ in artifacts]
    if len(resolved_outputs) != len(set(resolved_outputs)):
        raise ArgumentIRError("derived artifacts must use distinct output paths")
    existing = [
        _ir_preflight_output(path, data, inputs) for path, data in artifacts
    ]
    for (path, data), already_present in zip(artifacts, existing, strict=True):
        resolved = path.resolve()
        if already_present:
            continue
        resolved.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(resolved, data)


def _ir_print_validation(kind: str, errors: list[str]) -> int:
    print(
        json.dumps(
            {"artifact": kind, "valid": not errors, "errors": errors},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not errors else EXIT_INVALID_WORKFLOW


def ir_init_command(args: argparse.Namespace) -> int:
    source_path = resolve_manuscript_path(args.manuscript)
    project_dir = (
        Path(args.project_dir)
        if args.project_dir
        else source_path.with_name(source_path.stem + ".argument-workbench")
    )
    paths = initialize_workspace(
        source_path,
        project_dir,
        title=args.title,
    )
    print(f"Argument Workbench project: {paths.root}")
    print(f"Extraction prompt: {paths.prompt}")
    return 0


def _read_ir_paste_bytes() -> bytes:
    print(
        "Paste the model's pure Argument IR JSON. On a new line enter "
        f"{IR_PASTE_END_MARKER} to finish."
    )
    lines: list[str] = []
    total = 0
    while True:
        line = sys.stdin.readline()
        if line == "":
            raise WorkbenchError(
                f"paste ended before {IR_PASTE_END_MARKER}; no artifact was collected"
            )
        normalized = line.rstrip("\r\n")
        if normalized == IR_PASTE_END_MARKER:
            break
        total += len(line.encode("utf-8"))
        if total > DEFAULT_MAX_OUTPUT_BYTES:
            raise WorkbenchError(
                f"pasted Raw IR exceeds {DEFAULT_MAX_OUTPUT_BYTES} bytes"
            )
        lines.append(normalized)
    return ("\n".join(lines) + "\n").encode("utf-8")


def ir_collect_command(args: argparse.Namespace) -> int:
    paths = workspace_paths(args.project)
    if args.paste:
        response_bytes = _read_ir_paste_bytes()
        method = "terminal-paste"
        source_name = "pasted-argument-ir.json"
    else:
        raw_file = Path(args.file)
        if raw_file.is_symlink():
            raise WorkbenchError("Raw IR input must not be a symbolic link")
        response_path = raw_file.resolve()
        if not response_path.is_file():
            raise WorkbenchError(f"Raw IR input file does not exist: {response_path}")
        response_bytes = response_path.read_bytes()
        if len(response_bytes) > DEFAULT_MAX_OUTPUT_BYTES:
            raise WorkbenchError(
                f"Raw IR input exceeds {DEFAULT_MAX_OUTPUT_BYTES} bytes"
            )
        method = "file"
        source_name = response_path.name
    attempt_path, record = collect_raw_attempt(
        paths.root,
        response_bytes,
        method=method,
        source_name=source_name,
        producer_label=args.producer_label,
    )
    status = record["validation"]["status"]
    print(f"Raw IR attempt: {attempt_path}")
    print(f"Validation status: {status}")
    for error in record["validation"]["errors"]:
        print(f"  - {error}")
    active_attempt: Path | None = None
    if status in {"valid", "correctable"}:
        active_attempt, _, _ = selected_attempt(paths)
        if active_attempt != attempt_path:
            print(
                "This attempt was archived but not selected because the project already has "
                f"an inspectable Raw IR: {active_attempt}"
            )
            return 0
    if status == "valid":
        map_path, _ = rebuild_workspace(paths.root)
        print(f"Reviewed IR initialized: {map_path}")
        return 0
    if status == "correctable":
        print("The Raw IR is structurally inspectable; run `ir inspect` to correct it.")
        return 0
    print("The attempt was preserved but cannot be inspected; collect a new attempt.")
    return EXIT_INVALID_WORKFLOW


def ir_inspect_command(args: argparse.Namespace) -> int:
    if not args.view_only:
        isatty = getattr(sys.stdin, "isatty", None)
        if isatty is not None and not isatty():
            raise WorkbenchError(
                "interactive inspection requires a terminal; use --view-only for non-interactive output"
            )
    return run_inspector(args.project, view_only=args.view_only)


def ir_rebuild_command(args: argparse.Namespace) -> int:
    map_path, changed = rebuild_workspace(args.project)
    review_outputs, reviews_changed = rebuild_reviews(args.project)
    adjudication_outputs, adjudications_changed = rebuild_adjudication_cache(
        args.project
    )
    print(f"Argument map: {map_path}")
    for output in review_outputs:
        print(f"Claim review: {output}")
    for output in adjudication_outputs:
        print(f"Revision plan: {output}")
    print(
        "Derived artifacts rebuilt."
        if changed or reviews_changed or adjudications_changed
        else "Derived artifacts already current."
    )
    return 0


def ir_verify_project_command(args: argparse.Namespace) -> int:
    errors = verify_workspace(args.project)
    return _ir_print_validation("argument-workbench-project", errors)


def ir_review_prepare_command(args: argparse.Namespace) -> int:
    paths, created = prepare_rule_review(
        args.project,
        args.rules,
        depth=args.depth,
    )
    print(f"Rule Review: {paths.review_id}")
    print(f"Review prompt: {paths.prompt}")
    print(f"Check plan: {paths.plan}")
    print("Review prepared." if created else "Matching review already exists; reused.")
    return 0


def _read_review_paste_bytes() -> bytes:
    print(
        "Paste the model's pure argument-check-results JSON. On a new line enter "
        f"{IR_PASTE_END_MARKER} to finish."
    )
    lines: list[str] = []
    total = 0
    while True:
        line = sys.stdin.readline()
        if line == "":
            raise WorkbenchError(
                f"paste ended before {IR_PASTE_END_MARKER}; no review result was collected"
            )
        normalized = line.rstrip("\r\n")
        if normalized == IR_PASTE_END_MARKER:
            break
        total += len(line.encode("utf-8"))
        if total > DEFAULT_MAX_OUTPUT_BYTES:
            raise WorkbenchError(
                f"pasted review result exceeds {DEFAULT_MAX_OUTPUT_BYTES} bytes"
            )
        lines.append(normalized)
    return ("\n".join(lines) + "\n").encode("utf-8")


def ir_review_collect_command(args: argparse.Namespace) -> int:
    if args.paste:
        response_bytes = _read_review_paste_bytes()
        method = "terminal-paste"
        source_name = "pasted-check-results.json"
    else:
        raw_file = Path(args.file)
        if raw_file.is_symlink():
            raise WorkbenchError("review result input must not be a symbolic link")
        response_path = raw_file.resolve()
        if not response_path.is_file():
            raise WorkbenchError(f"review result file does not exist: {response_path}")
        response_bytes = response_path.read_bytes()
        if len(response_bytes) > DEFAULT_MAX_OUTPUT_BYTES:
            raise WorkbenchError(
                f"review result input exceeds {DEFAULT_MAX_OUTPUT_BYTES} bytes"
            )
        method = "file"
        source_name = response_path.name
    attempt_path, record = collect_review_results(
        args.project,
        response_bytes,
        review_id=args.review_id,
        method=method,
        source_name=source_name,
        producer_label=args.producer_label,
    )
    status = record["validation"]["status"]
    print(f"Review result attempt: {attempt_path}")
    print(f"Validation status: {status}")
    for error in record["validation"]["errors"]:
        print(f"  - {error}")
    if status != "valid":
        print("The attempt was preserved; collect a corrected complete/partial result.")
        return EXIT_INVALID_WORKFLOW
    review_text, view_path = show_claim_review(
        args.project,
        review_id=str(record["review_id"]),
        claim_id=None,
    )
    actionable = review_text.count("### FAIL ") + review_text.count("### UNCERTAIN ")
    print(f"Claim review: {view_path}")
    print(f"Open Findings: {actionable}")
    return 0


def ir_review_show_command(args: argparse.Namespace) -> int:
    rendered, view_path = show_claim_review(
        args.project,
        review_id=args.review_id,
        claim_id=args.claim,
    )
    print(rendered, end="" if rendered.endswith("\n") else "\n")
    print(f"Full claim review: {view_path}")
    return 0


def ir_adjudicate_command(args: argparse.Namespace) -> int:
    if not args.view_only:
        isatty = getattr(sys.stdin, "isatty", None)
        if isatty is not None and not isatty():
            raise WorkbenchError(
                "interactive Workbench adjudication requires a terminal; use --view-only for non-interactive output"
            )
    return run_workbench_adjudicator(
        args.project,
        review_id=args.review_id,
        review_all=args.review_all,
        view_only=args.view_only,
        verdict=args.verdict,
        claim=args.claim,
        check_id=args.check_id,
    )


def ir_revision_plan_command(args: argparse.Namespace) -> int:
    plan_path, changed = rebuild_workbench_revision_plan(args.project)
    print(f"Revision plan: {plan_path}")
    print("Revision plan rebuilt." if changed else "Revision plan already current.")
    if args.show:
        print(plan_path.read_text(encoding="utf-8"), end="")
    return 0


def ir_gate_a_init_command(args: argparse.Namespace) -> int:
    paths = initialize_gate(args.output, args.projects)
    print(f"Product Gate A corpus: {paths.corpus}")
    print(f"Evidence report: {paths.report_markdown}")
    print("Only hashes and local workspace locators were stored; manuscript bytes were not copied.")
    return 0


def ir_gate_a_assess_command(args: argparse.Namespace) -> int:
    metrics = {key: getattr(args, key) for key in GATE_A_METRIC_KEYS}
    output = append_gate_a_assessment(
        args.gate,
        args.project,
        comparison_to_direct_chat=args.comparison,
        correction_burden=args.burden,
        metrics=metrics,
        regression_anchors=args.anchor,
        actual_revision_notes=args.actual_revision_notes,
        notes=args.notes,
    )
    print(f"Human Gate A assessment: {output}")
    return 0


def ir_gate_a_report_command(args: argparse.Namespace) -> int:
    report, changed = rebuild_gate_report(args.gate)
    print(f"Product Gate A report: {report}")
    print("Report rebuilt." if changed else "Report already current.")
    if args.show:
        print(report.read_text(encoding="utf-8"), end="")
    return 0


def ir_gate_a_decide_command(args: argparse.Namespace) -> int:
    output = append_gate_decision(args.gate, args.decision, args.reason)
    print(f"Human Gate A decision: {output}")
    return 0


def ir_gate_a_verify_command(args: argparse.Namespace) -> int:
    return _ir_print_validation("product-gate-a", verify_gate(args.gate))


def ir_prepare_command(args: argparse.Namespace) -> int:
    source_path = resolve_manuscript_path(args.manuscript)
    manuscript, source_bytes = read_manuscript_utf8(source_path)
    prompt = build_ir_extraction_prompt(
        manuscript,
        source_name=source_path.name,
        source_sha256=sha256_bytes(source_bytes),
    )
    output = (
        Path(args.output)
        if args.output
        else source_path.with_name(source_path.stem + ".argument-ir-prompt.md")
    )
    prompt_bytes = prompt.encode("utf-8")
    _ir_write_outputs(((output, prompt_bytes),), inputs=(source_path,))
    print(f"Argument IR extraction prompt: {output.resolve()}")
    print(f"Source SHA-256: {sha256_bytes(source_bytes)}")
    return 0


def ir_validate_command(args: argparse.Namespace) -> int:
    source_path = resolve_manuscript_path(args.manuscript)
    _, source_bytes = read_manuscript_utf8(source_path)
    _, value, _ = _ir_read_json(Path(args.argument_ir), "argument IR")
    errors = validate_argument_ir(
        value,
        source_bytes=source_bytes,
        source_name=source_path.name,
    )
    return _ir_print_validation("argument-ir", errors)


def ir_plan_command(args: argparse.Namespace) -> int:
    source_path = resolve_manuscript_path(args.manuscript)
    _, source_bytes = read_manuscript_utf8(source_path)
    ir_path, ir_value, ir_bytes = _ir_read_json(
        Path(args.argument_ir), "argument IR"
    )
    library_path, library_value, library_bytes = _ir_read_json(
        Path(args.rules), "check library"
    )
    errors = validate_argument_ir(
        ir_value,
        source_bytes=source_bytes,
        source_name=source_path.name,
    )
    errors.extend(validate_check_library(library_value))
    if errors:
        return _ir_print_validation("argument-ir-plan-inputs", errors)
    normalized_ir = canonicalize_argument_ir(
        ir_value,
        source_bytes=source_bytes,
        source_name=source_path.name,
    )
    plan = build_check_plan(
        normalized_ir,
        library_value,
        ir_sha256=sha256_bytes(ir_bytes),
        library_sha256=sha256_bytes(library_bytes),
        depth=args.depth,
    )
    plan_errors = validate_check_plan(plan)
    if plan_errors:
        return _ir_print_validation("argument-check-plan", plan_errors)
    plan_bytes = _ir_json_bytes(plan)
    plan_sha256 = sha256_bytes(plan_bytes)
    prompt_bytes = render_check_prompt(plan, plan_sha256=plan_sha256).encode("utf-8")
    output = (
        Path(args.output)
        if args.output
        else ir_path.with_name("argument-check-plan.json")
    )
    prompt_output = (
        Path(args.prompt_output)
        if args.prompt_output
        else ir_path.with_name("argument-check-prompt.md")
    )
    _ir_write_outputs(
        ((output, plan_bytes), (prompt_output, prompt_bytes)),
        inputs=(source_path, ir_path, library_path),
    )
    print(f"Check plan: {output.resolve()}")
    print(f"Execution prompt: {prompt_output.resolve()}")
    print(f"Plan SHA-256: {plan_sha256}")
    print(f"Tasks: {len(plan['tasks'])}")
    return 0


def ir_validate_results_command(args: argparse.Namespace) -> int:
    _, plan, plan_bytes = _ir_read_json(Path(args.check_plan), "check plan")
    _, results, _ = _ir_read_json(Path(args.results), "check results")
    _, library, library_bytes = _ir_read_json(Path(args.rules), "check library")
    plan_errors = validate_check_plan_against_library(
        plan,
        library,
        library_sha256=sha256_bytes(library_bytes),
    )
    if plan_errors:
        return _ir_print_validation("argument-check-plan", plan_errors)
    errors = validate_check_results(
        results,
        plan,
        plan_sha256=sha256_bytes(plan_bytes),
    )
    return _ir_print_validation("argument-check-results", errors)


def ir_findings_command(args: argparse.Namespace) -> int:
    plan_path, plan, plan_bytes = _ir_read_json(Path(args.check_plan), "check plan")
    results_path, results, results_bytes = _ir_read_json(
        Path(args.results), "check results"
    )
    _, library, library_bytes = _ir_read_json(Path(args.rules), "check library")
    plan_errors = validate_check_plan_against_library(
        plan,
        library,
        library_sha256=sha256_bytes(library_bytes),
    )
    if plan_errors:
        return _ir_print_validation("argument-check-plan", plan_errors)
    plan_sha256 = sha256_bytes(plan_bytes)
    errors = validate_check_results(results, plan, plan_sha256=plan_sha256)
    if errors:
        return _ir_print_validation("argument-check-results", errors)
    findings = build_argument_findings(
        plan,
        results,
        plan_sha256=plan_sha256,
        results_sha256=sha256_bytes(results_bytes),
    )
    findings_errors = validate_argument_findings(findings)
    if findings_errors:
        return _ir_print_validation("argument-findings", findings_errors)
    findings_bytes = _ir_json_bytes(findings)
    output = (
        Path(args.output)
        if args.output
        else results_path.with_name("argument-findings.json")
    )
    _ir_write_outputs(
        ((output, findings_bytes),), inputs=(plan_path, results_path)
    )
    print(f"Findings: {output.resolve()}")
    print(f"Actionable findings: {len(findings['findings'])}")
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


def _run_overview(run_dir: Path) -> RunOverview:
    verification = verify_run_dir(run_dir)
    if not verification.valid:
        detail = verification.errors[0] if verification.errors else "unknown error"
        return RunOverview(
            run_dir,
            "归档损坏",
            "—",
            "—",
            "invalid",
            f"先运行 verify-run 检查：{detail}",
            verification,
        )
    try:
        manifest_value = parse_json(
            (run_dir / "manifest.json").read_text(encoding="utf-8")
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateJsonKeyError,
    ) as exc:
        invalid = VerificationResult(False, (str(exc),), ())
        return RunOverview(
            run_dir,
            "归档损坏",
            "—",
            "—",
            "invalid",
            f"无法读取 manifest：{exc}",
            invalid,
        )
    assert isinstance(manifest_value, dict)
    protocol = str(manifest_value.get("protocol", "—"))
    source = str(manifest_value.get("source_name", "—"))
    status = manifest_value.get("status")
    launcher = _python_launcher()
    quoted_run = f'"{run_dir}"'
    if status == "prepared":
        return RunOverview(
            run_dir,
            "等待 AI 报告",
            protocol,
            source,
            "import-report",
            f"直接粘贴 AI 回答：{launcher} critic_runner.py resume {quoted_run} --paste",
            verification,
        )
    if status == "collected" and protocol in CRITIC_PROTOCOLS:
        adjudication = parse_json(
            (run_dir / "adjudication.json").read_text(encoding="utf-8")
        )
        assert isinstance(adjudication, dict)
        findings = adjudication.get("findings")
        assert isinstance(findings, list)
        decided = sum(
            isinstance(finding, dict)
            and finding.get("decision") in {"accept", "reject", "defer"}
            for finding in findings
        )
        total = len(findings)
        if decided < total:
            return RunOverview(
                run_dir,
                f"人工裁决 {decided}/{total}",
                protocol,
                source,
                "adjudicate",
                f"继续运行：{launcher} critic_runner.py resume {quoted_run}",
                verification,
            )
        if not (run_dir / "revision-plan.md").exists():
            return RunOverview(
                run_dir,
                f"待生成修改计划 {decided}/{total}",
                protocol,
                source,
                "revision-plan",
                f"运行：{launcher} critic_runner.py resume {quoted_run}",
                verification,
            )
        return RunOverview(
            run_dir,
            f"已完成 {decided}/{total}",
            protocol,
            source,
            "complete",
            "查看 revision-plan.md；需要复议时运行："
            f"{launcher} critic_runner.py adjudicate {quoted_run} --review-all",
            verification,
        )
    if status == "collected":
        return RunOverview(
            run_dir,
            "报告已回收",
            protocol,
            source,
            "complete",
            "查看 report.md",
            verification,
        )
    if status == "succeeded":
        return RunOverview(
            run_dir,
            "自动执行完成",
            protocol,
            source,
            "complete",
            "查看 report.md",
            verification,
        )
    if status == "running":
        return RunOverview(
            run_dir,
            "执行中或意外中断",
            protocol,
            source,
            "inspect",
            "检查执行进程；必要时运行 verify-run",
            verification,
        )
    return RunOverview(
        run_dir,
        f"执行未完成：{status}",
        protocol,
        source,
        "inspect",
        "查看 manifest.json 和 stderr.log",
        verification,
    )


def _invalid_symlink_overview(run_dir: Path) -> RunOverview:
    return RunOverview(
        run_dir,
        "归档损坏",
        "—",
        "—",
        "invalid",
        "符号链接不会被跟随；请检查此目录",
        VerificationResult(False, ("run directory must not be a symbolic link",), ()),
    )


def _list_run_dirs(raw_runs_root: Path) -> list[Path]:
    if raw_runs_root.is_symlink():
        raise ValueError("runs directory must not be a symbolic link")
    runs_root = raw_runs_root.resolve()
    if not runs_root.exists():
        return []
    if not runs_root.is_dir():
        raise ValueError("runs path is not a directory")
    return sorted(
        (
            entry
            for entry in runs_root.iterdir()
            if entry.is_dir() or entry.is_symlink()
        ),
        key=lambda entry: entry.name,
        reverse=True,
    )


def _safe_run_overview(run_dir: Path) -> RunOverview:
    if run_dir.is_symlink():
        return _invalid_symlink_overview(run_dir)
    try:
        return _run_overview(run_dir)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateJsonKeyError,
    ) as exc:
        return RunOverview(
            run_dir,
            "归档损坏",
            "—",
            "—",
            "invalid",
            f"无法读取：{exc}",
            VerificationResult(False, (str(exc),), ()),
        )


def status_command(args: argparse.Namespace) -> int:
    requested_run = getattr(args, "run_dir", None)
    if requested_run:
        raw_run = Path(requested_run)
        if raw_run.is_symlink():
            print("状态：归档损坏")
            print("问题：run directory must not be a symbolic link")
            return EXIT_INVALID_ARCHIVE
        run_dirs = [raw_run.resolve()]
        detailed = True
    else:
        try:
            run_dirs = _list_run_dirs(
                Path(getattr(args, "runs_dir", ".critic-runs"))
            )
        except ValueError as exc:
            print(f"status error: {exc}", file=sys.stderr)
            return EXIT_INVALID_WORKFLOW
        detailed = False
        if not run_dirs:
            print("还没有运行记录。先运行 quickstart 创建第一次审查。")
            return 0

    for index, run_dir in enumerate(run_dirs, start=1):
        overview = _safe_run_overview(run_dir)
        print(f"[{index}] {overview.stage}｜{overview.protocol}｜{overview.source}")
        print(f"    目录：{overview.run_dir}")
        print(f"    下一步：{overview.next_action}")
        if detailed and not overview.verification.valid:
            for error in overview.verification.errors:
                print(f"    问题：{error}")
    if not detailed:
        print(f"\n共 {len(run_dirs)} 次运行；最新记录显示在最前。")
    if detailed and not overview.verification.valid:
        return EXIT_INVALID_ARCHIVE
    return 0


def read_pasted_report_bytes() -> bytes:
    print("请从下一行开始粘贴完整 AI 回答。")
    print(f"粘贴结束后，另起一行只输入 {PASTE_END_MARKER} 并回车。")
    lines: list[str] = []
    total_bytes = 0
    too_large = False
    while True:
        try:
            line = input()
        except EOFError as exc:
            raise ValueError(
                f"输入在结束标记 {PASTE_END_MARKER} 之前结束；报告未导入"
            ) from exc
        if line.strip() == PASTE_END_MARKER:
            break
        try:
            line_size = len((line + "\n").encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError(f"粘贴内容无法编码为 UTF-8：{exc}") from exc
        total_bytes += line_size
        if total_bytes > DEFAULT_MAX_OUTPUT_BYTES:
            too_large = True
            continue
        lines.append(line)
    if too_large:
        raise ValueError(
            f"粘贴报告超过 {DEFAULT_MAX_OUTPUT_BYTES} 字节；报告未导入"
        )
    if not any(line.strip() for line in lines):
        raise ValueError("粘贴报告不能为空")
    return ("\n".join(lines) + "\n").encode("utf-8")


def resume_command(args: argparse.Namespace) -> int:
    pasted_input = bool(getattr(args, "paste", False))
    report_argument = getattr(args, "report", None)
    if pasted_input and report_argument:
        print("resume error: --paste and --report cannot be combined", file=sys.stderr)
        return EXIT_INVALID_WORKFLOW
    requested_run = getattr(args, "run_dir", None)
    if requested_run:
        raw_run = Path(requested_run)
        overview = _safe_run_overview(
            raw_run if raw_run.is_symlink() else raw_run.resolve()
        )
        actionable_count = 1 if overview.action in {
            "import-report",
            "adjudicate",
            "revision-plan",
        } else 0
    else:
        try:
            run_dirs = _list_run_dirs(
                Path(getattr(args, "runs_dir", ".critic-runs"))
            )
        except ValueError as exc:
            print(f"resume error: {exc}", file=sys.stderr)
            return EXIT_INVALID_WORKFLOW
        if not run_dirs:
            print("还没有运行记录。请先运行 quickstart 创建第一次审查。")
            return 0
        overviews = [_safe_run_overview(run_dir) for run_dir in run_dirs]
        damaged = [item for item in overviews if item.action == "invalid"]
        actionable = [
            item
            for item in overviews
            if item.action in {"import-report", "adjudicate", "revision-plan"}
        ]
        if not actionable:
            if damaged:
                print("没有可安全继续的运行，但发现损坏归档：", file=sys.stderr)
                for item in damaged:
                    print(f"- {item.run_dir}: {item.next_action}", file=sys.stderr)
                return EXIT_INVALID_ARCHIVE
            latest = overviews[0]
            if latest.action == "complete":
                print("没有待继续的运行。最新一次审查已经结束：")
            else:
                print("没有可自动继续的运行。最新记录需要人工检查：")
            print(f"{latest.stage}｜{latest.protocol}｜{latest.source}")
            print(latest.run_dir)
            print(latest.next_action)
            return 0
        overview = actionable[0]
        actionable_count = len(actionable)
        if damaged:
            print(
                f"警告：另有 {len(damaged)} 个损坏归档未处理；可用 status 查看。",
                file=sys.stderr,
            )

    if not overview.verification.valid:
        for error in overview.verification.errors:
            print(f"resume archive error: {error}", file=sys.stderr)
        return EXIT_INVALID_ARCHIVE
    if overview.action not in {"import-report", "adjudicate", "revision-plan"}:
        if report_argument or pasted_input:
            print(
                "resume error: --report/--paste can only be used with a prepared run",
                file=sys.stderr,
            )
            return EXIT_INVALID_WORKFLOW
        print(f"这次运行当前不需要继续：{overview.stage}")
        print(overview.next_action)
        return 0

    print(f"继续处理：{overview.source}｜{overview.protocol}｜{overview.stage}")
    print(f"运行目录：{overview.run_dir}")
    if actionable_count > 1:
        print(
            f"另有 {actionable_count - 1} 次待办；本次自动选择最新的一次。"
            "如需指定，请使用 resume <运行目录>。"
        )

    if overview.action == "import-report":
        if pasted_input:
            try:
                pasted_report = read_pasted_report_bytes()
            except ValueError as exc:
                print(f"错误：{exc}", file=sys.stderr)
                return EXIT_INVALID_REPORT
            except KeyboardInterrupt:
                print("\n已取消。", file=sys.stderr)
                return EXIT_INTERRUPTED
            try:
                with tempfile.TemporaryDirectory(prefix="critic-paste-") as temp_dir:
                    report_path = Path(temp_dir) / "pasted-report.md"
                    atomic_write_bytes(report_path, pasted_report)
                    result = import_report_command(
                        argparse.Namespace(
                            run_dir=str(overview.run_dir),
                            report=str(report_path),
                            adjudication_output=None,
                            collection_method="terminal-paste",
                            collection_source_name="pasted-report.md",
                        )
                    )
            except OSError as exc:
                print(f"resume paste error: {exc}", file=sys.stderr)
                return EXIT_INVALID_WORKFLOW
        else:
            report = report_argument
            if report is None:
                try:
                    report = input("请粘贴已保存的 AI 报告路径：")
                except EOFError:
                    print("\n错误：没有收到报告路径。", file=sys.stderr)
                    return 2
                except KeyboardInterrupt:
                    print("\n已取消。", file=sys.stderr)
                    return EXIT_INTERRUPTED
            report = _unquote_path(str(report))
            if not report:
                print("错误：报告路径不能为空。", file=sys.stderr)
                return 2
            report = str(Path(report).expanduser())
            result = import_report_command(
                argparse.Namespace(
                    run_dir=str(overview.run_dir),
                    report=report,
                    adjudication_output=None,
                )
            )
        if result != 0:
            return result
        if overview.protocol not in CRITIC_PROTOCOLS:
            print("报告已安全回收；该协议不需要人工裁决。")
            return 0
        return adjudicate_command(
            argparse.Namespace(run_dir=str(overview.run_dir), review_all=False)
        )
    if report_argument or pasted_input:
        print(
            "resume error: --report/--paste can only be used with a prepared run",
            file=sys.stderr,
        )
        return EXIT_INVALID_WORKFLOW
    if overview.action == "adjudicate":
        return adjudicate_command(
            argparse.Namespace(run_dir=str(overview.run_dir), review_all=False)
        )
    return revision_plan_command(
        argparse.Namespace(
            run_dir=str(overview.run_dir), adjudication=None, output=None
        )
    )


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


def list_tracks(_: argparse.Namespace) -> int:
    for name, track in ACADEMIC_TRACKS.items():
        specialists = ", ".join(track["specialists"]) or "none"
        print(f"{name}: {track['label']}")
        print(f"  primary: {track['primary']}")
        print(f"  specialists: {specialists}")
    print("cross-disciplinary: " + ", ".join(CROSS_DISCIPLINARY_PROTOCOLS))
    return 0


def doctor(args: argparse.Namespace) -> int:
    errors: list[str] = []
    checks: list[str] = []
    if sys.version_info < (3, 10):
        errors.append("Python 3.10 or newer is required")
    else:
        checks.append(f"Python {sys.version_info.major}.{sys.version_info.minor}")

    expected_protocols = set(PROTOCOLS)
    if set(PROTOCOL_PREFIX) != expected_protocols:
        errors.append("protocol prefix registry does not match available protocols")
    elif len(set(PROTOCOL_PREFIX.values())) != len(PROTOCOL_PREFIX):
        errors.append("protocol prefixes must be unique")
    else:
        checks.append("protocol prefixes are complete and unique")

    for name in PROTOCOLS:
        try:
            body, _ = load_protocol(name, allow_test_artifact=name in TEST_ONLY)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"{name} cannot be loaded: {exc}")
            continue
        if name in CRITIC_PROTOCOLS:
            for heading in CRITIC_SECTIONS:
                if body.count(heading) != 1:
                    errors.append(f"{name} must contain {heading!r} exactly once")
    if not any("cannot be loaded" in error or "must contain" in error for error in errors):
        checks.append(f"{len(PROTOCOLS)} protocol files are readable and structurally valid")

    referenced_protocols = set(CROSS_DISCIPLINARY_PROTOCOLS)
    for track in ACADEMIC_TRACKS.values():
        referenced_protocols.add(str(track["primary"]))
        referenced_protocols.update(str(name) for name in track["specialists"])
    missing_references = sorted(referenced_protocols - expected_protocols)
    if missing_references:
        errors.append(f"academic track registry references missing protocols: {missing_references}")
    else:
        checks.append(f"{len(ACADEMIC_TRACKS)} academic tracks resolve correctly")

    try:
        rules_text, _ = read_utf8(IR_SOCIAL_SCIENCE_RULES)
        rules_value = parse_json(rules_text)
        rule_errors = validate_check_library(rules_value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        errors.append(f"Argument IR check library cannot be loaded: {exc}")
    else:
        if rule_errors:
            errors.extend(
                f"Argument IR check library: {error}" for error in rule_errors
            )
        else:
            checks.append(
                f"Argument IR check library is valid ({len(rules_value['checks'])} checks)"
            )

    directory = Path(args.directory).resolve()
    if not directory.is_dir():
        errors.append(f"working directory does not exist: {directory}")
    else:
        try:
            with tempfile.NamedTemporaryFile(
                dir=directory,
                prefix=".critic-doctor-",
                delete=True,
            ) as handle:
                handle.write(b"ok")
                handle.flush()
        except OSError as exc:
            errors.append(f"working directory is not writable: {exc}")
        else:
            checks.append(f"working directory is writable: {directory}")

    for check in checks:
        print(f"[ok] {check}")
    if errors:
        for error in errors:
            print(f"[error] {error}", file=sys.stderr)
        return 2
    print("ready")
    return 0


def prepare_track(args: argparse.Namespace) -> int:
    args.protocol = ACADEMIC_TRACKS[args.track]["primary"]
    return prepare(args)


def _unquote_path(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1].strip()
    return value


def quickstart(args: argparse.Namespace) -> int:
    """Guide a first-time user to a manual, provider-neutral prompt bundle."""
    print("Critic Divergence Tester 快速开始")
    print("不会上传文章，也不需要 API key。按 Ctrl+C 可随时退出。")

    manuscript = getattr(args, "manuscript", None)
    if manuscript is None:
        try:
            manuscript = input("\n请粘贴文章路径（.md 或 .txt）：")
        except EOFError:
            print("\n错误：没有收到文章路径。", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("\n已取消。", file=sys.stderr)
            return EXIT_INTERRUPTED
    manuscript = _unquote_path(str(manuscript))
    if not manuscript:
        print("错误：文章路径不能为空。", file=sys.stderr)
        return 2

    source_path = Path(manuscript).expanduser().resolve()
    if not source_path.is_file():
        print(f"错误：找不到文章文件：{source_path}", file=sys.stderr)
        return 2
    try:
        source_text, _ = read_utf8(source_path)
    except UnicodeDecodeError:
        print("错误：文章不是 UTF-8 编码，请转换编码后重试。", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"错误：无法读取文章：{exc}", file=sys.stderr)
        return 2
    if not source_text.strip():
        print("错误：文章文件是空的。", file=sys.stderr)
        return 2

    track = getattr(args, "track", None)
    if track is not None and track not in ACADEMIC_TRACKS:
        print(f"错误：未知学术线：{track}", file=sys.stderr)
        return 2
    while track is None:
        print("\n请选择学术线：")
        print("  1. 文科·社会科学（历史、哲学、法学、经济学、社会学等）")
        print("  2. 理科·自然科学（实验、观察、理论与模拟）")
        print("  3. 工科·工程学（软件、产品、系统与实现）")
        try:
            choice = input("请输入 1、2 或 3（直接回车默认选 1）：").strip()
        except EOFError:
            print("\n错误：没有收到学术线选择。", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("\n已取消。", file=sys.stderr)
            return EXIT_INTERRUPTED
        track = QUICKSTART_TRACK_ALIASES.get(choice or "1")
        if track is None:
            print("无法识别，请输入 1、2、3，或学术线名称。")

    track_label = str(ACADEMIC_TRACKS[track]["label"])
    print(f"\n已选择：{track_label}")
    print("正在生成自包含审查提示……")
    run_dir = _prepare_bundle(
        argparse.Namespace(
            protocol=ACADEMIC_TRACKS[track]["primary"],
            manuscript=str(source_path),
            runs_dir=getattr(args, "runs_dir", ".critic-runs"),
            allow_test_artifact=False,
        )
    )
    prompt_path = run_dir / "prompt.md"
    print(prompt_path)
    print("完成。打开上面显示的 prompt.md，复制全部内容给你常用的 AI。")
    print("AI 回答完成后，只需运行下面命令并直接粘贴回答：")
    launcher = _python_launcher()
    print(f"{launcher} critic_runner.py resume --paste")
    return 0


def run_track(args: argparse.Namespace) -> int:
    args.protocol = ACADEMIC_TRACKS[args.track]["primary"]
    return run(args)


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


def _add_track_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("track", choices=ACADEMIC_TRACKS)
    parser.add_argument("manuscript", help="UTF-8 manuscript path")
    parser.add_argument(
        "--runs-dir",
        default=".critic-runs",
        help="archive directory (default: .critic-runs)",
    )


def _add_execution_limits(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--executor-label",
        help="public reproducibility label for the model/configuration (never put secrets here)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_seconds,
        default=900.0,
        help="terminate the executor after this many seconds (default: 900)",
    )
    parser.add_argument(
        "--max-output-bytes",
        type=positive_integer,
        default=DEFAULT_MAX_OUTPUT_BYTES,
        help="terminate after this many combined stdout/stderr bytes (default: 16777216)",
    )


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        description="Run critic protocols without depending on Claude Code."
    )
    sub = top.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list available protocols").set_defaults(func=list_protocols)
    sub.add_parser("tracks", help="list academic tracks and their protocols").set_defaults(
        func=list_tracks
    )
    doctor_parser = sub.add_parser(
        "doctor", help="check Python, protocol files, track mappings, and write access"
    )
    doctor_parser.add_argument(
        "--directory",
        default=".",
        help="directory to test for archive write access (default: current directory)",
    )
    doctor_parser.set_defaults(func=doctor)

    ir_parser = sub.add_parser(
        "ir",
        help="inspect, correct, and validate the Argument IR workflow",
    )
    ir_sub = ir_parser.add_subparsers(dest="ir_command", required=True)

    ir_init_parser = ir_sub.add_parser(
        "init",
        help="import a manuscript into a local Argument Workbench V1 project",
    )
    ir_init_parser.add_argument("manuscript", help="UTF-8 manuscript path")
    ir_init_parser.add_argument(
        "--project-dir",
        help="project directory (default: <manuscript>.argument-workbench beside source)",
    )
    ir_init_parser.add_argument("--title", help="project/document title (default: filename stem)")
    ir_init_parser.set_defaults(func=ir_init_command)

    ir_collect_parser = ir_sub.add_parser(
        "collect",
        help="immutably collect a model's Raw Argument IR response",
    )
    ir_collect_parser.add_argument("project", help="Argument Workbench project directory")
    ir_collect_source = ir_collect_parser.add_mutually_exclusive_group(required=True)
    ir_collect_source.add_argument(
        "--paste", action="store_true", help=f"paste JSON until {IR_PASTE_END_MARKER}"
    )
    ir_collect_source.add_argument("--file", help="existing Raw IR response file")
    ir_collect_parser.add_argument(
        "--producer-label",
        help="opaque model/executor label for provenance; no provider SDK is required",
    )
    ir_collect_parser.set_defaults(func=ir_collect_command)

    ir_inspect_parser = ir_sub.add_parser(
        "inspect",
        help="view and interactively correct Raw IR without editing JSON",
    )
    ir_inspect_parser.add_argument("project", help="Argument Workbench project directory")
    ir_inspect_parser.add_argument(
        "--view-only",
        action="store_true",
        help="print the current structure without starting the correction menu",
    )
    ir_inspect_parser.set_defaults(func=ir_inspect_command)

    ir_rebuild_parser = ir_sub.add_parser(
        "rebuild",
        help="deterministically rebuild Reviewed IR and argument-map.md",
    )
    ir_rebuild_parser.add_argument("project", help="Argument Workbench project directory")
    ir_rebuild_parser.set_defaults(func=ir_rebuild_command)

    ir_verify_project_parser = ir_sub.add_parser(
        "verify-project",
        help="verify every Workbench artifact, parent hash, and derived byte",
    )
    ir_verify_project_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_verify_project_parser.set_defaults(func=ir_verify_project_command)

    ir_review_parser = ir_sub.add_parser(
        "review",
        help="run claim-centered Review Lenses inside an Argument Workbench project",
    )
    ir_review_sub = ir_review_parser.add_subparsers(
        dest="ir_review_command", required=True
    )

    ir_review_prepare_parser = ir_review_sub.add_parser(
        "prepare",
        help="prepare an IR-native Rule Lens plan against Reviewed IR",
    )
    ir_review_prepare_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_review_prepare_parser.add_argument(
        "--rules",
        default=str(IR_SOCIAL_SCIENCE_RULES),
        help="check-library JSON path (default: bundled social-science rules)",
    )
    ir_review_prepare_parser.add_argument(
        "--depth",
        choices=("core", "full"),
        default="core",
        help="core checks only, or core plus extended checks (default: core)",
    )
    ir_review_prepare_parser.set_defaults(func=ir_review_prepare_command)

    ir_review_collect_parser = ir_review_sub.add_parser(
        "collect",
        help="immutably collect and validate a Rule Lens model result",
    )
    ir_review_collect_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_review_collect_source = ir_review_collect_parser.add_mutually_exclusive_group(
        required=True
    )
    ir_review_collect_source.add_argument(
        "--paste",
        action="store_true",
        help=f"paste JSON until {IR_PASTE_END_MARKER}",
    )
    ir_review_collect_source.add_argument(
        "--file", help="existing argument-check-results JSON file"
    )
    ir_review_collect_parser.add_argument(
        "--review-id",
        help="Rule Review ID (default: most recently prepared review)",
    )
    ir_review_collect_parser.add_argument(
        "--producer-label",
        help="opaque model/executor label for provenance",
    )
    ir_review_collect_parser.set_defaults(func=ir_review_collect_command)

    ir_review_show_parser = ir_review_sub.add_parser(
        "show",
        help="show every check outcome and open Finding for a Claim",
    )
    ir_review_show_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_review_show_parser.add_argument(
        "--review-id",
        help="Rule Review ID (default: most recent review with valid results)",
    )
    ir_review_show_parser.add_argument(
        "--claim",
        help="Claim ID such as C4 or V1:C4 (default: show all reviewed Claims)",
    )
    ir_review_show_parser.set_defaults(func=ir_review_show_command)

    ir_adjudicate_parser = ir_sub.add_parser(
        "adjudicate",
        help="accept, reject, or defer Claim-level Workbench Findings",
    )
    ir_adjudicate_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_adjudicate_parser.add_argument(
        "--review-id",
        help="limit decisions to one current Rule Review",
    )
    ir_adjudicate_parser.add_argument(
        "--review-all",
        action="store_true",
        help="include Findings that already have a human decision",
    )
    ir_adjudicate_parser.add_argument(
        "--view-only",
        action="store_true",
        help="show current human decisions without starting the prompt",
    )
    ir_adjudicate_parser.add_argument(
        "--verdict",
        choices=("fail", "uncertain"),
        help="show or adjudicate only model FAIL or UNCERTAIN Findings",
    )
    ir_adjudicate_parser.add_argument(
        "--claim",
        help="show or adjudicate one target Claim such as C4 or V1:C4",
    )
    ir_adjudicate_parser.add_argument(
        "--check",
        dest="check_id",
        help="show or adjudicate one exact Rule Lens check ID",
    )
    ir_adjudicate_parser.set_defaults(func=ir_adjudicate_command)

    ir_revision_plan_parser = ir_sub.add_parser(
        "revision-plan",
        help="deterministically rebuild the Workbench revision plan",
    )
    ir_revision_plan_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_revision_plan_parser.add_argument(
        "--show",
        action="store_true",
        help="print revision-plan.md after rebuilding it",
    )
    ir_revision_plan_parser.set_defaults(func=ir_revision_plan_command)

    ir_gate_a_parser = ir_sub.add_parser(
        "gate-a",
        help="evaluate Phase 1-3 on a private corpus of 3-5 real manuscripts",
    )
    ir_gate_a_sub = ir_gate_a_parser.add_subparsers(
        dest="ir_gate_a_command", required=True
    )
    ir_gate_a_init_parser = ir_gate_a_sub.add_parser(
        "init", help="capture exact hashes for 3-5 completed Workbench projects"
    )
    ir_gate_a_init_parser.add_argument("output", help="new local Gate A evidence directory")
    ir_gate_a_init_parser.add_argument(
        "projects", nargs="+", help="3-5 completed real-manuscript Workbench projects"
    )
    ir_gate_a_init_parser.set_defaults(func=ir_gate_a_init_command)

    ir_gate_a_assess_parser = ir_gate_a_sub.add_parser(
        "assess", help="append one human usability/IR-quality assessment"
    )
    ir_gate_a_assess_parser.add_argument("gate", help="Gate A evidence directory")
    ir_gate_a_assess_parser.add_argument("project", help="corpus alias such as P1")
    ir_gate_a_assess_parser.add_argument(
        "--comparison", choices=GATE_A_COMPARISONS, required=True,
        help="clarity/control compared with direct full-text chat review",
    )
    ir_gate_a_assess_parser.add_argument(
        "--burden", choices=GATE_A_BURDENS, required=True,
        help="human IR correction burden",
    )
    for metric in GATE_A_METRIC_KEYS:
        ir_gate_a_assess_parser.add_argument(
            "--" + metric.replace("_", "-"), type=int, required=True
        )
    ir_gate_a_assess_parser.add_argument(
        "--anchor",
        action="append",
        required=True,
        help="known important Claim, extraction trap, Finding, or framework reversal; repeatable",
    )
    ir_gate_a_assess_parser.add_argument(
        "--actual-revision-notes",
        default="",
        help="what the author actually revised, if observed during Gate A",
    )
    ir_gate_a_assess_parser.add_argument("--notes", default="", help="free-form evaluator notes")
    ir_gate_a_assess_parser.set_defaults(func=ir_gate_a_assess_command)

    ir_gate_a_report_parser = ir_gate_a_sub.add_parser(
        "report", help="rebuild the deterministic Gate A evidence report"
    )
    ir_gate_a_report_parser.add_argument("gate", help="Gate A evidence directory")
    ir_gate_a_report_parser.add_argument("--show", action="store_true", help="print the report")
    ir_gate_a_report_parser.set_defaults(func=ir_gate_a_report_command)

    ir_gate_a_decide_parser = ir_gate_a_sub.add_parser(
        "decide", help="append a human pass/fail/defer gate decision"
    )
    ir_gate_a_decide_parser.add_argument("gate", help="Gate A evidence directory")
    ir_gate_a_decide_parser.add_argument("decision", choices=GATE_A_DECISIONS)
    ir_gate_a_decide_parser.add_argument("--reason", required=True, help="human decision rationale")
    ir_gate_a_decide_parser.set_defaults(func=ir_gate_a_decide_command)

    ir_gate_a_verify_parser = ir_gate_a_sub.add_parser(
        "verify", help="verify corpus bindings, assessments, decisions, and report bytes"
    )
    ir_gate_a_verify_parser.add_argument("gate", help="Gate A evidence directory")
    ir_gate_a_verify_parser.set_defaults(func=ir_gate_a_verify_command)

    ir_prepare_parser = ir_sub.add_parser(
        "prepare",
        help="create a source-bound prompt for extracting Argument IR",
    )
    ir_prepare_parser.add_argument("manuscript", help="UTF-8 manuscript path")
    ir_prepare_parser.add_argument(
        "--output", help="output prompt path (default: beside manuscript)"
    )
    ir_prepare_parser.set_defaults(func=ir_prepare_command)

    ir_validate_parser = ir_sub.add_parser(
        "validate",
        help="validate an Argument IR against the exact manuscript bytes",
    )
    ir_validate_parser.add_argument("manuscript", help="UTF-8 manuscript path")
    ir_validate_parser.add_argument("argument_ir", help="Argument IR JSON path")
    ir_validate_parser.set_defaults(func=ir_validate_command)

    ir_plan_parser = ir_sub.add_parser(
        "plan",
        help="select method-conditional checks and create an execution prompt",
    )
    ir_plan_parser.add_argument("manuscript", help="UTF-8 manuscript path")
    ir_plan_parser.add_argument("argument_ir", help="validated Argument IR JSON path")
    ir_plan_parser.add_argument(
        "--rules",
        default=str(IR_SOCIAL_SCIENCE_RULES),
        help="check-library JSON path (default: bundled social-science rules)",
    )
    ir_plan_parser.add_argument(
        "--depth",
        choices=("core", "full"),
        default="core",
        help="core checks only, or core plus extended checks (default: core)",
    )
    ir_plan_parser.add_argument(
        "--output", help="check-plan JSON path (default: beside Argument IR)"
    )
    ir_plan_parser.add_argument(
        "--prompt-output",
        help="execution prompt path (default: beside Argument IR)",
    )
    ir_plan_parser.set_defaults(func=ir_plan_command)

    ir_results_parser = ir_sub.add_parser(
        "validate-results",
        help="validate model results against the exact check-plan bytes",
    )
    ir_results_parser.add_argument("check_plan", help="check-plan JSON path")
    ir_results_parser.add_argument("results", help="check-results JSON path")
    ir_results_parser.add_argument(
        "--rules",
        default=str(IR_SOCIAL_SCIENCE_RULES),
        help="check-library JSON path used to reproduce the plan (default: bundled rules)",
    )
    ir_results_parser.set_defaults(func=ir_validate_results_command)

    ir_findings_parser = ir_sub.add_parser(
        "findings",
        help="derive deterministic fail/uncertain findings from validated results",
    )
    ir_findings_parser.add_argument("check_plan", help="check-plan JSON path")
    ir_findings_parser.add_argument("results", help="validated check-results JSON path")
    ir_findings_parser.add_argument(
        "--rules",
        default=str(IR_SOCIAL_SCIENCE_RULES),
        help="check-library JSON path used to reproduce the plan (default: bundled rules)",
    )
    ir_findings_parser.add_argument(
        "--output", help="findings JSON path (default: beside results)"
    )
    ir_findings_parser.set_defaults(func=ir_findings_command)

    quickstart_parser = sub.add_parser(
        "quickstart", help="中文交互引导：选择文章和学术线并生成 prompt"
    )
    quickstart_parser.add_argument(
        "manuscript", nargs="?", help="可选的 UTF-8 文章路径"
    )
    quickstart_parser.add_argument(
        "--track", choices=ACADEMIC_TRACKS, help="可选；跳过交互式学术线选择"
    )
    quickstart_parser.add_argument(
        "--runs-dir",
        default=".critic-runs",
        help="归档目录（默认：.critic-runs）",
    )
    quickstart_parser.set_defaults(func=quickstart)

    prepare_parser = sub.add_parser(
        "prepare", help="archive a self-contained prompt for manual use"
    )
    _add_run_inputs(prepare_parser)
    prepare_parser.set_defaults(func=prepare)

    prepare_track_parser = sub.add_parser(
        "prepare-track", help="prepare the primary protocol for an academic track"
    )
    _add_track_inputs(prepare_track_parser)
    prepare_track_parser.set_defaults(
        allow_test_artifact=False,
        func=prepare_track,
    )

    run_parser = sub.add_parser(
        "run", help="run one protocol through an external stdin/stdout command"
    )
    _add_run_inputs(run_parser)
    _add_execution_limits(run_parser)
    run_parser.set_defaults(func=run)

    run_track_parser = sub.add_parser(
        "run-track", help="run the primary protocol for an academic track"
    )
    _add_track_inputs(run_track_parser)
    _add_execution_limits(run_track_parser)
    run_track_parser.set_defaults(
        allow_test_artifact=False,
        func=run_track,
    )

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
        "--track",
        action="append",
        choices=ACADEMIC_TRACKS,
        help="academic track to include; repeat this option; cannot combine with --protocol",
    )
    campaign_parser.add_argument(
        "--repeat",
        type=positive_integer,
        default=2,
        help="serial repetitions per protocol (default: 2)",
    )
    campaign_parser.add_argument(
        "--order-seed",
        help="reproduce the counterbalanced execution order (default: random seed)",
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
    campaign_parser.add_argument(
        "--executor-label",
        help="public reproducibility label for the model/configuration (never put secrets here)",
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

    status_parser = sub.add_parser(
        "status", help="中文显示历史运行进度、归档健康状态和下一步命令"
    )
    status_parser.add_argument(
        "run_dir", nargs="?", help="可选；只查看某一个运行目录"
    )
    status_parser.add_argument(
        "--runs-dir",
        default=".critic-runs",
        help="未指定 run_dir 时扫描的归档目录（默认：.critic-runs）",
    )
    status_parser.set_defaults(func=status_command)

    resume_parser = sub.add_parser(
        "resume", help="中文一键继续最新待办：回收报告、裁决或生成修改计划"
    )
    resume_parser.add_argument(
        "run_dir", nargs="?", help="可选；指定要继续的运行目录"
    )
    resume_parser.add_argument(
        "--runs-dir",
        default=".critic-runs",
        help="未指定 run_dir 时扫描的归档目录（默认：.critic-runs）",
    )
    resume_input = resume_parser.add_mutually_exclusive_group()
    resume_input.add_argument(
        "--report",
        help="prepared 运行的 AI 报告路径；省略时使用中文交互询问",
    )
    resume_input.add_argument(
        "--paste",
        action="store_true",
        help=f"直接粘贴 AI 回答，并以单独一行 {PASTE_END_MARKER} 结束",
    )
    resume_parser.set_defaults(func=resume_command)

    verify_campaign_parser = sub.add_parser(
        "verify-campaign", help="verify a campaign and every archived run"
    )
    verify_campaign_parser.add_argument("campaign_dir", help="campaign directory")
    verify_campaign_parser.add_argument(
        "--source", help="optional original source file to recheck"
    )
    verify_campaign_parser.set_defaults(func=verify_campaign_command)

    import_report_parser = sub.add_parser(
        "import-report",
        help="validate and bind a manual AI report to a prepared run",
    )
    import_report_parser.add_argument("run_dir", help="prepared run directory")
    import_report_parser.add_argument("report", help="UTF-8 report returned by the AI")
    import_report_parser.add_argument(
        "--adjudication-output",
        help="adjudication JSON path (default: inside the run directory)",
    )
    import_report_parser.set_defaults(func=import_report_command)

    adjudicate_parser = sub.add_parser(
        "adjudicate",
        help="中文交互裁决 AI 发现并自动生成修改计划",
    )
    adjudicate_parser.add_argument("run_dir", help="collected run directory")
    adjudicate_parser.add_argument(
        "--review-all",
        action="store_true",
        help="重新查看全部裁决；首次修改前自动留存当前 revision-plan.md",
    )
    adjudicate_parser.set_defaults(func=adjudicate_command)

    revision_plan_parser = sub.add_parser(
        "revision-plan",
        help="turn completed human adjudication into an actionable Markdown plan",
    )
    revision_plan_parser.add_argument("run_dir", help="collected run directory")
    revision_plan_parser.add_argument(
        "--adjudication",
        help="completed adjudication JSON path (default: inside the run directory)",
    )
    revision_plan_parser.add_argument(
        "--output",
        help="revision plan Markdown path (default: inside the run directory)",
    )
    revision_plan_parser.set_defaults(func=revision_plan_command)

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

    blind_scorecard_parser = sub.add_parser(
        "blind-scorecard",
        help="create an identity-free pairing artifact and a separate private key",
    )
    blind_scorecard_parser.add_argument("scorecard", help="traceable scorecard path")
    blind_scorecard_parser.add_argument(
        "--output", help="reviewer JSON path (default: beside scorecard)"
    )
    blind_scorecard_parser.add_argument(
        "--key-output", help="private key JSON path (default: beside scorecard)"
    )
    blind_scorecard_parser.add_argument(
        "--seed", help="optional reproducible blind alias seed"
    )
    blind_scorecard_parser.set_defaults(func=blind_scorecard_command)

    apply_blind_parser = sub.add_parser(
        "apply-blind-scorecard",
        help="verify and merge blinded human pairings into a scorecard",
    )
    apply_blind_parser.add_argument("scorecard", help="original scorecard path")
    apply_blind_parser.add_argument(
        "blind", nargs="?", help="completed blind JSON path (default: beside scorecard)"
    )
    apply_blind_parser.add_argument(
        "--key", help="private key JSON path (default: beside scorecard)"
    )
    apply_blind_parser.add_argument(
        "--output", help="merged JSON path (default: beside scorecard)"
    )
    apply_blind_parser.set_defaults(func=apply_blind_scorecard_command)

    return top


def main(argv: list[str] | None = None) -> int:
    stdin_reconfigure = getattr(sys.stdin, "reconfigure", None)
    if stdin_reconfigure is not None:
        stdin_reconfigure(encoding="utf-8", errors="strict")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    executor: list[str] = []
    if raw_argv and raw_argv[0] in {"run", "run-track", "campaign"} and "--" in raw_argv:
        separator = raw_argv.index("--")
        executor = raw_argv[separator + 1 :]
        raw_argv = raw_argv[:separator]

    args = parser().parse_args(raw_argv)
    if args.command in {"run", "run-track", "campaign"}:
        args.executor = executor
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
