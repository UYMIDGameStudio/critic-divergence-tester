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
    import_document_version,
    initialize_workspace,
    rebuild_workspace,
    run_inspector,
    selected_attempt,
    project_mutation_lock,
    verify_workspace,
    verify_project_versions,
    workspace_paths,
)
from argument_review import (
    collect_review_results,
    prepare_rule_review,
    rebuild_reviews,
    show_claim_review,
)
from argument_perspective import (
    collect_perspective_results,
    prepare_perspective_review,
    rebuild_perspective_reviews,
    show_perspective_review,
)
from argument_lens_view import render_claim_lenses
from argument_versioning import (
    build_structural_diff,
    rebuild_structural_diffs,
)
from argument_lineage import (
    append_lineage_decision,
    collect_lineage_proposals,
    lineage_proposal_ids,
    prepare_lineage_analysis,
    rebuild_lineage_analyses,
    render_lineage_history,
    show_lineage,
)
from argument_resolution import (
    append_resolution_decision,
    collect_resolution_results,
    prepare_resolution,
    rebuild_resolutions,
    render_resolution,
)
from argument_citations import (
    append_citation_decision,
    collect_citation_results,
    prepare_citation_audit,
    rebuild_citation_audits,
    render_citation_audit,
)
from argument_ui import serve_workbench
from argument_app import default_data_dir, serve_product_app
from document_review_ingest import doctor_dependencies, repair_dependencies
from document_review_ui import default_studio_data_dir, serve_document_review_studio
from document_review_studio import DocumentReviewProject, ReviewStudioError
from document_review_model import CRITIC_DIMENSIONS
from argument_adjudication import (
    append_claim_bundle_decisions,
    claim_bundle_status,
    rebuild_adjudication_cache,
    rebuild_revision_plan as rebuild_workbench_revision_plan,
    run_adjudicator as run_workbench_adjudicator,
)
from argument_baseline import (
    collect_direct_review_baseline,
    prepare_direct_review_prompt,
)
from argument_triage import (
    append_status_triage,
    rebuild_status_triages,
    render_status_triage,
    triage_items_for_review,
)
from argument_sessions import (
    abandon_work_session,
    finish_work_session,
    list_work_sessions,
    render_work_sessions,
    start_work_session,
)
from argument_gate import (
    METRIC_KEYS as GATE_A_METRIC_KEYS,
    append_assessment as append_gate_a_assessment,
    append_gate_decision,
    gate_readiness,
    initialize_gate,
    rebuild_gate_report,
    render_gate_readiness,
    verify_gate,
)
from argument_gate_b import (
    append_gate_b_assessment,
    append_gate_b_decision,
    initialize_gate_b,
    rebuild_gate_b_report,
    verify_gate_b,
)
from argument_contracts import (
    BASELINE_INTERACTION_MODES,
    BASELINE_MANUSCRIPT_DELIVERY,
    BASELINE_PRIOR_CONTEXTS,
    GATE_A_BURDENS,
    GATE_A_COMPARISONS,
    GATE_A_DECISIONS,
    GATE_A_WORK_ACTIVITIES,
    GATE_B_CLARITIES,
    GATE_B_DECISIONS,
    GATE_B_JUDGMENTS,
    CITATION_BIBLIOGRAPHIC_STATUSES,
    CITATION_CONTENT_SUPPORT_STATUSES,
    CITATION_CONTEXT_STATUSES,
    CITATION_SOURCE_LOCATION_STATUSES,
    LINEAGE_RELATIONS,
    RESOLUTION_STATUSES,
    REVISION_ACTION_TYPES,
)
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


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = ROOT if (ROOT / "critic-social-science.md").is_file() else Path(sys.prefix)
IR_SOCIAL_SCIENCE_RULES = RESOURCE_ROOT / "ir" / "social-science-checks.json"

PROTOCOLS = {
    "critic-social-science": RESOURCE_ROOT / "critic-social-science.md",
    "critic-natural-science": RESOURCE_ROOT / "critic-natural-science.md",
    "critic-engineering": RESOURCE_ROOT / "critic-engineering.md",
    "critic-individualist": RESOURCE_ROOT / "critic-individualist.md",
    "critic-contrastivist": RESOURCE_ROOT / "critic-contrastivist.md",
    "citation-auditor": RESOURCE_ROOT / "citation-auditor.md",
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

def _unquote_path(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1].strip()
    return value

__all__ = [name for name in globals() if not name.startswith("__")]
