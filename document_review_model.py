"""Format-neutral contracts for Document Review Studio.

The existing Argument Workbench stores a manuscript as an immutable Markdown
version.  Document Review Studio deliberately keeps a richer, independent
representation for office documents and PDFs.  This module contains only
serialisable contracts; parsers and review heuristics live elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
SUPPORTED_EXTENSIONS = {".md", ".txt", ".docx", ".pdf"}
UNSUPPORTED_EXTENSIONS = {".doc", ".docm", ".pages"}
CRITIC_DIMENSIONS = (
    "expression_ambiguity",
    "execution_feasibility",
    "compliance_legal_screen",
    "reasonableness_governance",
    "official_professional_format",
)
VERIFICATION_STATES = {
    "verified",
    "model-proposed",
    "needs-human-verification",
    "source-conflict",
    "cannot-confirm",
}
SEVERITIES = {"info", "low", "medium", "high", "critical"}
FINDING_DECISIONS = {"accept", "reject", "defer", "correct"}


def _clean(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def canonical_json(value: Any) -> bytes:
    return (json.dumps(_clean(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def stable_id(prefix: str, *parts: object, length: int = 20) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:length]}"


@dataclass(frozen=True)
class RawFileBinding:
    original_name: str
    extension: str
    media_type: str
    byte_size: int
    sha256: str
    relative_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DocumentLocation:
    block_id: str
    block_kind: str
    page: int | None = None
    paragraph: int | None = None
    table_id: str | None = None
    row: int | None = None
    column: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    source_path: str | None = None
    bbox: tuple[float, float, float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.bbox is not None:
            value["bbox"] = list(self.bbox)
        return value


@dataclass
class DocumentBlock:
    block_id: str
    kind: str
    text: str = ""
    level: int | None = None
    location: DocumentLocation | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    children: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "kind": self.kind,
            "text": self.text,
            "level": self.level,
            "location": self.location.to_dict() if self.location else None,
            "attrs": _clean(self.attrs),
            "children": list(self.children),
        }


@dataclass
class ExtractionWarning:
    code: str
    severity: str
    message: str
    location: DocumentLocation | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown extraction warning severity: {self.severity}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "location": self.location.to_dict() if self.location else None,
            "details": _clean(self.details),
        }


@dataclass
class QualitySignals:
    page_count: int = 0
    blank_pages: list[int] = field(default_factory=list)
    text_coverage: float = 0.0
    garble_ratio: float = 0.0
    ocr_low_confidence_blocks: int = 0
    suspected_reading_order: bool = False
    table_count: int = 0
    tables_parsed: int = 0
    header_footer_mixed: bool = False
    footnote_comment_revision_risk: list[str] = field(default_factory=list)
    parser_available: bool = True
    ocr_available: bool | None = None
    human_corrected: bool = False
    requires_confirmation: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StructuredDocument:
    document_id: str
    title: str
    source: RawFileBinding
    parser_name: str
    parser_version: str
    blocks: list[DocumentBlock] = field(default_factory=list)
    warnings: list[ExtractionWarning] = field(default_factory=list)
    quality: QualitySignals = field(default_factory=QualitySignals)
    source_to_block: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def block(self, block_id: str) -> DocumentBlock:
        for item in self.blocks:
            if item.block_id == block_id:
                return item
        raise KeyError(block_id)

    @property
    def plain_text(self) -> str:
        return "\n\n".join(block.text for block in self.blocks if block.text.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "document_id": self.document_id,
            "title": self.title,
            "source": self.source.to_dict(),
            "parser": {"name": self.parser_name, "version": self.parser_version},
            "blocks": [block.to_dict() for block in self.blocks],
            "warnings": [warning.to_dict() for warning in self.warnings],
            "quality": self.quality.to_dict(),
            "source_to_block": _clean(self.source_to_block),
            "metadata": _clean(self.metadata),
        }


@dataclass
class ReviewContext:
    document_type: str
    jurisdiction: str = ""
    effective_date: str = ""
    publisher_type: str = ""
    audience: str = ""
    involves_minors: bool = False
    involves_fees: bool = False
    involves_sponsorship: bool = False
    involves_contract: bool = False
    involves_personal_information: bool = False
    involves_intellectual_property: bool = False
    publication_status: str = "internal-draft"
    confirmed: bool = False
    model_suggestion: str | None = None
    user_provided_materials: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExternalBasis:
    jurisdiction: str = ""
    source_name: str = ""
    issuing_body: str = ""
    validity: str = "unknown"
    locator: str = ""
    url_or_attachment: str = ""
    application: str = ""
    unresolved_facts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    finding_id: str
    critic: str
    document_type: str
    location: DocumentLocation
    evidence: str
    issue: str
    standard: str
    consequence: str
    severity: str
    verification_state: str
    external_basis: ExternalBasis
    uncertainties: list[str]
    suggested_action: str
    suggested_owner: str
    blocks_release_or_execution: bool
    status: str = "open"
    origin: str = "model-derived"
    competing_readings: list[str] = field(default_factory=list)
    required_observation: str = ""
    proposed_group_id: str | None = None
    source_finding_id: str | None = None
    check_id: str | None = None
    check_data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.critic not in CRITIC_DIMENSIONS:
            raise ValueError(f"unknown critic dimension: {self.critic}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown finding severity: {self.severity}")
        if self.verification_state not in VERIFICATION_STATES:
            raise ValueError(f"unknown verification state: {self.verification_state}")
        if not self.location.block_id:
            raise ValueError("finding location must include a stable block_id")
        if not self.finding_id.strip():
            raise ValueError("finding_id is required")
        if self.check_id is not None and (not isinstance(self.check_id, str) or not self.check_id.strip()):
            raise ValueError("finding check_id must be non-empty text when supplied")
        if not self.evidence.strip():
            raise ValueError("finding evidence is required")
        for label, value in (
            ("document_type", self.document_type),
            ("issue", self.issue),
            ("standard", self.standard),
            ("consequence", self.consequence),
            ("suggested_action", self.suggested_action),
            ("suggested_owner", self.suggested_owner),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"finding {label} is required")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["location"] = self.location.to_dict()
        value["external_basis"] = self.external_basis.to_dict()
        return value


@dataclass
class AuditRun:
    run_id: str
    critic: str
    document_id: str
    source_sha256: str
    context: ReviewContext
    findings: list[Finding] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    zero_finding_basis: list[str] = field(default_factory=list)
    model_label: str = "deterministic-local-rules"
    created_at: str = ""
    run_sequence: int = 1
    previous_audit_run_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "critic": self.critic,
            "document_id": self.document_id,
            "source_sha256": self.source_sha256,
            "context": self.context.to_dict(),
            "findings": [finding.to_dict() for finding in self.findings],
            "observations": list(self.observations),
            "zero_finding_basis": list(self.zero_finding_basis),
            "model_label": self.model_label,
            "created_at": self.created_at,
            "run_sequence": self.run_sequence,
            "previous_audit_run_sha256": self.previous_audit_run_sha256,
        }


def validate_finding_dict(value: Mapping[str, Any]) -> list[str]:
    """Validate the public Finding contract without coercing model output."""
    required = {
        "finding_id", "critic", "document_type", "location", "evidence", "issue",
        "standard", "consequence", "severity", "verification_state", "external_basis",
        "uncertainties", "suggested_action", "suggested_owner", "blocks_release_or_execution",
    }
    errors: list[str] = []
    missing = sorted(required - set(value))
    if missing:
        errors.append("missing fields: " + ", ".join(missing))
    location = value.get("location")
    if not isinstance(location, Mapping) or not str(location.get("block_id", "")).strip():
        errors.append("location.block_id is required")
    if value.get("critic") not in CRITIC_DIMENSIONS:
        errors.append("critic is not a supported independent dimension")
    if value.get("severity") not in SEVERITIES:
        errors.append("severity is invalid")
    if value.get("verification_state") not in VERIFICATION_STATES:
        errors.append("verification_state 无效；只能使用 " + " | ".join(sorted(VERIFICATION_STATES)))
    if not isinstance(value.get("external_basis"), Mapping):
        errors.append("external_basis 必须是 JSON 对象；没有外部依据时使用 {}，不要使用 null、字符串或数组")
    if not isinstance(value.get("uncertainties"), list):
        errors.append("uncertainties must be a list")
    if not isinstance(value.get("blocks_release_or_execution"), bool):
        errors.append("blocks_release_or_execution must be boolean")
    for field_name in (
        "finding_id", "document_type", "evidence", "issue", "standard",
        "consequence", "suggested_action", "suggested_owner",
    ):
        field_value = value.get(field_name)
        if not isinstance(field_value, str) or not field_value.strip():
            errors.append(f"{field_name} must be non-empty text")
        elif len(field_value) > 100_000:
            errors.append(f"{field_name} exceeds the size limit")
    finding_id = value.get("finding_id")
    if isinstance(finding_id, str) and len(finding_id) > 256:
        errors.append("finding_id exceeds 256 characters")
    check_id = value.get("check_id")
    if check_id is not None and (not isinstance(check_id, str) or not check_id.strip() or len(check_id) > 256):
        errors.append("check_id must be non-empty text of at most 256 characters")
    if "check_data" in value and not isinstance(value.get("check_data"), Mapping):
        errors.append("check_data must be an object")
    return errors


def make_location(block: DocumentBlock, **overrides: Any) -> DocumentLocation:
    """Copy a block location while allowing a critic to refine coordinates."""
    base = block.location.to_dict() if block.location else {}
    base.update(overrides)
    return DocumentLocation(
        block_id=str(base.get("block_id") or block.block_id),
        block_kind=str(base.get("block_kind") or block.kind),
        page=base.get("page"),
        paragraph=base.get("paragraph"),
        table_id=base.get("table_id"),
        row=base.get("row"),
        column=base.get("column"),
        char_start=base.get("char_start"),
        char_end=base.get("char_end"),
        source_path=base.get("source_path"),
        bbox=tuple(base["bbox"]) if base.get("bbox") is not None else None,
    )


def model_to_markdown(document: StructuredDocument) -> str:
    """Render a conservative editable draft from the internal model."""
    lines: list[str] = []
    for block in document.blocks:
        if block.kind == "heading":
            lines.append("#" * max(1, min(block.level or 1, 6)) + " " + block.text)
        elif block.kind == "list_item":
            marker = "1." if block.attrs.get("ordered") else "-"
            lines.append(f"{marker} {block.text}")
        elif block.kind == "blockquote":
            lines.append("> " + block.text)
        elif block.kind == "page_break":
            lines.append("\\page")
        elif block.kind == "table_cell":
            continue
        elif block.kind == "table":
            rows = block.attrs.get("rows") or []
            if rows:
                lines.append("| " + " | ".join(str(cell) for cell in rows[0]) + " |")
                lines.append("| " + " | ".join("---" for _ in rows[0]) + " |")
                for row in rows[1:]:
                    lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
        elif block.text:
            lines.append(block.text)
        if lines and lines[-1] != "":
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "AuditRun", "CRITIC_DIMENSIONS", "DocumentBlock", "DocumentLocation", "ExternalBasis",
    "ExtractionWarning", "FINDING_DECISIONS", "Finding", "QualitySignals", "RawFileBinding",
    "ReviewContext", "SCHEMA_VERSION", "SEVERITIES", "StructuredDocument", "SUPPORTED_EXTENSIONS",
    "UNSUPPORTED_EXTENSIONS", "VERIFICATION_STATES", "canonical_json", "make_location",
    "model_to_markdown", "stable_id", "validate_finding_dict",
]
