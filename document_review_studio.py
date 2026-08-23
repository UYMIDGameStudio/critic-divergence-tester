"""Local-first Document Review Studio workflow.

This module is intentionally independent from any model SDK.  A review can be
run with the bundled deterministic checks, or a caller can use the generated
dimension prompt and submit a validated model result later.  Each critic is
stored independently; this module never votes, averages, or silently merges
findings from different dimensions.
"""

from __future__ import annotations

import difflib
import hashlib
import html
import json
import os
import re
import secrets
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from document_review_ingest import IngestionError, IngestionLimits, ingest_bytes, safe_upload_name
from document_review_model import (
    AuditRun,
    CRITIC_DIMENSIONS,
    DocumentBlock,
    DocumentLocation,
    ExternalBasis,
    Finding,
    FINDING_DECISIONS,
    QualitySignals,
    ReviewContext,
    StructuredDocument,
    canonical_json,
    make_location,
    model_to_markdown,
    stable_id,
    validate_finding_dict,
)


STORE_SUFFIX = ".document-review-studio"
STUDIO_SCHEMA_VERSION = 1
MAX_TEXT_CORRECTION_BYTES = 8 * 1024 * 1024
INTEGRITY_POLICY_NAME = "integrity-policy.json"
INTEGRITY_RECEIPT_DIR = ".integrity"
INTEGRITY_INDEX_NAME = "integrity-index.json"
EXTRACTION_DECISION_DIR_NAME = "extraction-decisions"


CRITIC_PROTOCOLS: dict[str, dict[str, Any]] = {
    "expression_ambiguity": {
        "role": "表达歧义审查者",
        "objective": "寻找能产生至少两种可执行读法的表达，不把单纯风格偏好写成 Finding。",
        "checks": ["主语、对象、范围、条件和期限是否唯一", "代词、模糊限定词和例外是否有明确指向", "竞争读法分别会造成什么后果"],
        "evidence": "每条 Finding 必须给出原文证据、稳定 block 定位、至少两种竞争读法及用于排除读法的观察。",
        "exclusions": "不要做法律结论、预算估算或公文格式打分。",
    },
    "execution_feasibility": {
        "role": "执行可行性审查者",
        "objective": "把目标追溯到交付物、负责人、资源、依赖、时间和验收证据。",
        "checks": ["目标—交付物—负责人链", "预算、资源、依赖和风险响应", "指标、验收人和失败处理"],
        "evidence": "区分文本明确缺失与可能存在于附件的未核实信息；不得仅凭关键词存在宣称可执行。",
        "exclusions": "不要替用户分配预算或负责人，也不要做法律意见。",
    },
    "compliance_legal_screen": {
        "role": "合规与法律风险筛查者",
        "objective": "识别需要来源核验的管辖、授权、隐私、未成年人、收费、合同和知识产权问题。",
        "checks": ["适用司法辖区和主体资格", "正式来源、条款定位、有效性和适用事实", "无法核实时必须 cannot-confirm"],
        "evidence": "外部结论必须填写 external_basis；没有可核验来源时只提出待核实问题。",
        "exclusions": "不是律师意见，不得用模型记忆替代当前有效来源。",
    },
    "reasonableness_governance": {
        "role": "合理性与治理审查者",
        "objective": "审查权力来源、边界、比例性、程序正当、回避、申诉与纠错机制。",
        "checks": ["权力来源与授权边界", "利益冲突和回避", "通知、陈述、申诉、复议与纠错"],
        "evidence": "说明受影响群体、替代安排和判断所依赖的制度材料；保留价值冲突。",
        "exclusions": "不要把多数偏好或单一价值判断写成客观事实。",
    },
    "official_professional_format": {
        "role": "公文与专业格式审查者",
        "objective": "分开检查确定性格式项和需要语义判断的一致性问题。",
        "checks": ["标题、编号、日期、署名、附件", "名称、日期、金额和表格正文一致性", "发布状态对应的格式要求"],
        "evidence": "每个格式 Finding 必须定位到缺失位置或冲突的具体 block/page/table cell。",
        "exclusions": "不要把版式偏好写成强制规则，不要跨维度做实质合规结论。",
    },
}


class ReviewStudioError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ReviewStudioError(f"拒绝写入符号链接：{path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_new(path: Path, data: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ReviewStudioError(f"拒绝覆盖已有审计产物：{path}")
    _atomic_write(path, data)


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReviewStudioError(f"JSON 不是安全普通文件：{path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReviewStudioError(f"JSON 必须是对象：{path}")
    return value


def _slug(name: str, digest: str) -> str:
    stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff-]+", "-", Path(name).stem).strip("-")[:48] or "document"
    return f"{stem}-{digest[:10]}{STORE_SUFFIX}"


def _safe_child(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ReviewStudioError("路径穿越被拒绝")
    return candidate


def _integrity_policy_path(root: Path) -> Path:
    return root / INTEGRITY_POLICY_NAME


def _integrity_index_path(root: Path) -> Path:
    return root / INTEGRITY_INDEX_NAME


def _integrity_receipt_path(path: Path) -> Path:
    return path.parent / INTEGRITY_RECEIPT_DIR / f"{path.name}.json"


def _integrity_receipts_exist(root: Path) -> bool:
    return any(path.is_dir() or path.is_symlink() for path in root.rglob(INTEGRITY_RECEIPT_DIR))


def _ensure_integrity_policy(root: Path, *, create: bool = False) -> dict[str, Any]:
    path = _integrity_policy_path(root)
    if path.is_file() and not path.is_symlink():
        value = _read_json(path)
        expected = {"artifact_type", "schema_version", "enabled_at", "producer", "lifecycle"}
        if set(value) != expected or value.get("artifact_type") != "document-review-integrity-policy" or value.get("schema_version") != 1 or value.get("producer") != "document-review-studio" or value.get("lifecycle") != "immutable":
            raise ReviewStudioError("integrity-policy.json: 完整性策略无效")
        return value
    if path.exists() or path.is_symlink():
        raise ReviewStudioError("integrity-policy.json: 完整性策略必须是普通文件")
    if _integrity_receipts_exist(root):
        raise ReviewStudioError("integrity-policy.json: integrity policy missing; existing receipts forbid downgrade")
    if not create:
        raise ReviewStudioError("integrity-policy.json: integrity policy missing")
    value = {
        "artifact_type": "document-review-integrity-policy",
        "schema_version": 1,
        "enabled_at": _now(),
        "producer": "document-review-studio",
        "lifecycle": "immutable",
    }
    _write_new(path, canonical_json(value))
    return value


def _parent_ref(root: Path, path: Path, *, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReviewStudioError(f"父产物缺失或不是普通文件：{path}")
    return {
        "role": role,
        "relative_path": str(path.relative_to(root)).replace("\\", "/"),
        "sha256": _sha256(path.read_bytes()),
    }


def _integrity_receipt(root: Path, path: Path, data: bytes, *, parents: Iterable[Mapping[str, Any]] = (), provenance: str = "deterministic") -> bytes:
    policy_path = _integrity_policy_path(root)
    policy = _ensure_integrity_policy(root)
    parent_rows = [dict(item) for item in parents]
    return canonical_json({
        "artifact_type": "document-review-artifact-integrity",
        "schema_version": 1,
        "artifact_relative_path": str(path.relative_to(root)).replace("\\", "/"),
        "artifact_sha256": _sha256(data),
        "policy_sha256": _sha256(policy_path.read_bytes()),
        "policy_enabled_at": policy["enabled_at"],
        "parents": parent_rows,
        "provenance": provenance,
        "lifecycle": "immutable" if "append-only" not in provenance else "append-only",
    })


def _integrity_index_entry_hash(entry: Mapping[str, Any]) -> str:
    value = {key: item for key, item in entry.items() if key != "entry_sha256"}
    return _sha256(canonical_json(value))


def _write_integrity_index(root: Path, index: Mapping[str, Any], *, new: bool) -> None:
    index_path = _integrity_index_path(root)
    data = canonical_json(index)
    receipt = _integrity_receipt(root, index_path, data, provenance="system-integrity-index")
    if new:
        _write_new(index_path, data)
        _write_new(_integrity_receipt_path(index_path), receipt)
    else:
        _atomic_write(index_path, data)
        _atomic_write(_integrity_receipt_path(index_path), receipt)


def _initialize_integrity_index(root: Path) -> None:
    index = {
        "artifact_type": "document-review-integrity-index",
        "schema_version": 1,
        "index_id": stable_id("IDX", root.name, _now(), secrets.token_hex(4)),
        "entries": [],
        "head_sha256": None,
        "next_sequence": 1,
        "lifecycle": "append-only",
    }
    _write_integrity_index(root, index, new=True)


def _append_integrity_index(root: Path, path: Path, data: bytes, *, artifact_type: str) -> None:
    index_path = _integrity_index_path(root)
    index = _read_json(index_path)
    entries = index.get("entries")
    if not isinstance(entries, list) or not isinstance(index.get("next_sequence"), int):
        raise ReviewStudioError("integrity-index.json: index is not appendable")
    sequence = index["next_sequence"]
    previous_head = index.get("head_sha256")
    relative_path = str(path.relative_to(root)).replace("\\", "/")
    receipt_path = _integrity_receipt_path(path)
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise ReviewStudioError(f"{relative_path}: integrity receipt missing before index registration")
    entry = {
        "artifact_id": stable_id("ART", relative_path, _sha256(data), str(sequence), _now(), secrets.token_hex(4)),
        "relative_path": relative_path,
        "sha256": _sha256(data),
        "receipt_relative_path": str(receipt_path.relative_to(root)).replace("\\", "/"),
        "receipt_sha256": _sha256(receipt_path.read_bytes()),
        "artifact_type": artifact_type,
        "sequence": sequence,
        "previous_index_head_sha256": previous_head,
        "created_at": _now(),
    }
    entry["entry_sha256"] = _integrity_index_entry_hash(entry)
    updated = dict(index)
    updated["entries"] = [*entries, entry]
    updated["head_sha256"] = entry["entry_sha256"]
    updated["next_sequence"] = sequence + 1
    _write_integrity_index(root, updated, new=False)


def _write_tracked(root: Path, path: Path, data: bytes, *, parents: Iterable[Mapping[str, Any]] = (), provenance: str = "deterministic", artifact_type: str | None = None) -> None:
    _write_new(path, data)
    _write_new(_integrity_receipt_path(path), _integrity_receipt(root, path, data, parents=parents, provenance=provenance))
    _append_integrity_index(root, path, data, artifact_type=artifact_type or provenance)


def _replace_tracked(root: Path, path: Path, data: bytes, *, parents: Iterable[Mapping[str, Any]] = (), provenance: str = "deterministic", artifact_type: str | None = None) -> None:
    _atomic_write(path, data)
    _atomic_write(_integrity_receipt_path(path), _integrity_receipt(root, path, data, parents=parents, provenance=provenance))
    _append_integrity_index(root, path, data, artifact_type=artifact_type or provenance)


class DocumentReviewProject:
    """One immutable upload plus append-only workflow decisions."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self._ensure_root()

    def _ensure_root(self) -> None:
        if self.root.is_symlink() or not self.root.is_dir():
            raise ReviewStudioError("项目目录必须是本地普通目录")
        for child in self.root.iterdir():
            if child.is_symlink():
                raise ReviewStudioError(f"项目包含符号链接：{child.name}")

    @property
    def manifest_path(self) -> Path:
        return self.root / "project.json"

    @property
    def document_path(self) -> Path:
        return self.root / "extraction" / "document.json"

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @classmethod
    def create(
        cls,
        data_dir: Path | str,
        *,
        filename: str,
        content: bytes,
        title: str | None = None,
        limits: IngestionLimits | None = None,
        ocr: Any | None = None,
    ) -> "DocumentReviewProject":
        safe_name = safe_upload_name(filename)
        if not isinstance(content, bytes) or not content:
            raise ReviewStudioError("上传文件必须是非空原始字节")
        limits = limits or IngestionLimits()
        if len(content) > limits.max_file_bytes:
            raise ReviewStudioError(f"文件超过 {limits.max_file_bytes // (1024 * 1024)} MiB 安全上限")
        storage = Path(data_dir).resolve()
        storage.mkdir(parents=True, exist_ok=True)
        target = storage / _slug(safe_name, _sha256(content))
        if target.exists():
            project = cls(target)
            manifest = project.manifest()
            if manifest.get("source", {}).get("sha256") != _sha256(content):
                raise ReviewStudioError("同名项目的原件 SHA-256 不匹配")
            return project
        target.mkdir()
        _ensure_integrity_policy(target, create=True)
        _initialize_integrity_index(target)
        source_dir = target / "source"
        source_dir.mkdir()
        source_path = source_dir / safe_name
        _write_tracked(target, source_path, content, provenance="user-uploaded")
        manifest = {
            "schema_version": STUDIO_SCHEMA_VERSION,
            "project_id": stable_id("PRJ", _sha256(content), safe_name),
            "title": title or Path(safe_name).stem,
            "source": {"name": safe_name, "sha256": _sha256(content), "bytes": len(content), "relative_path": f"source/{safe_name}"},
            "created_at": _now(),
            "original_never_overwritten": True,
        }
        _write_tracked(target, target / "project.json", canonical_json(manifest), parents=[_parent_ref(target, source_path, role="original-source")])
        state = {"extraction_state": "unconfirmed", "context_state": "missing", "review_state": "not_started", "read_only": False, "diagnostics": []}
        _write_new(target / "state.json", canonical_json(state))
        project = cls(target)
        try:
            document = ingest_bytes(safe_name, content, limits=limits, ocr=ocr)
        except (IngestionError, OSError, ValueError) as exc:
            diagnostic = {"schema_version": 1, "kind": "ingestion-failure", "safe": True, "message": str(exc)[:1000], "source_sha256": _sha256(content), "created_at": _now()}
            _write_tracked(target, target / "extraction" / "diagnostic.json", canonical_json(diagnostic), parents=[_parent_ref(target, source_path, role="original-source")])
            project._update_state(extraction_state="blocked", diagnostics=[diagnostic["message"]])
            project._append_event("ingestion_failed", diagnostic)
            return project
        project._save_document(document)
        project._append_event("uploaded", {"source_sha256": document.source.sha256, "parser": document.parser_name})
        return project

    def manifest(self) -> dict[str, Any]:
        return _read_json(self.manifest_path)

    def _default_state(self) -> dict[str, Any]:
        return {"extraction_state": "unconfirmed", "context_state": "missing", "review_state": "not_started", "read_only": False, "diagnostics": []}

    def _extraction_decision_records(self) -> list[tuple[Path, dict[str, Any], str]]:
        records: list[tuple[Path, dict[str, Any], str]] = []
        directory = self.root / EXTRACTION_DECISION_DIR_NAME
        if not directory.is_dir():
            return records
        for path in directory.glob("*.json"):
            if path.is_symlink():
                continue
            value = _read_json(path)
            records.append((path, value, _sha256(path.read_bytes())))
        return records

    def _latest_extraction_decision(self) -> dict[str, Any] | None:
        try:
            records = self._extraction_decision_records()
        except (OSError, ValueError, ReviewStudioError):
            return None
        valid = [value for _, value, _ in records if isinstance(value.get("sequence"), int)]
        return max(valid, key=lambda value: value["sequence"]) if valid else None

    def _derive_state(self, cached: Mapping[str, Any]) -> dict[str, Any]:
        state = self._default_state()
        decision = self._latest_extraction_decision()
        if decision:
            state["extraction_state"] = decision.get("extraction_state", "unconfirmed")
        elif (self.root / "extraction" / "diagnostic.json").is_file():
            state["extraction_state"] = "blocked"
        context_path = self.root / "context.json"
        if context_path.is_file():
            try:
                state["context_state"] = "confirmed" if _read_json(context_path).get("confirmed") is True else "missing"
            except (OSError, ValueError, ReviewStudioError):
                state["context_state"] = "missing"
        audit_runs = list((self.root / "audits").glob("*/*.json")) if (self.root / "audits").is_dir() else []
        has_ai = False
        has_audit = False
        for path in audit_runs:
            try:
                value = _read_json(path)
            except (OSError, ValueError, ReviewStudioError):
                continue
            has_audit = True
            if str(value.get("model_label", "")).startswith("manual-import:"):
                has_ai = True
        if has_ai:
            state["review_state"] = "ai_review_imported"
            state["ai_review_state"] = "imported"
        elif has_audit:
            state["review_state"] = "local_precheck_completed"
        cached_diagnostics = cached.get("diagnostics", [])
        if isinstance(cached_diagnostics, list):
            state["diagnostics"] = cached_diagnostics
        for key in ("last_audit_at", "last_ai_protocol_at"):
            if key in cached:
                state[key] = cached[key]
        state["read_only"] = bool(cached.get("read_only", False))
        if isinstance(cached.get("integrity_errors"), list):
            state["integrity_errors"] = list(cached["integrity_errors"])
        return state

    def state(self) -> dict[str, Any]:
        if self.state_path.is_symlink():
            raise ReviewStudioError("项目 state.json 不能是符号链接")
        cached: dict[str, Any] = {}
        if self.state_path.is_file():
            try:
                cached = _read_json(self.state_path)
            except (OSError, ValueError, ReviewStudioError):
                cached = {}
        state = self._derive_state(cached)
        try:
            errors = self.integrity_errors()
        except (OSError, ValueError, KeyError, TypeError, ReviewStudioError):
            errors = []
        if errors:
            state["read_only"] = True
            state["integrity_errors"] = errors
        if cached != state or not self.state_path.is_file():
            _atomic_write(self.state_path, canonical_json(state))
        return state

    def _extraction_decision_chain_errors(self) -> list[str]:
        errors: list[str] = []
        expected = {"artifact_type", "schema_version", "decision_id", "sequence", "previous_extraction_decision_sha256", "decision", "extraction_state", "source_sha256", "document_relative_path", "document_sha256", "quality_relative_path", "quality_sha256", "warnings_relative_path", "warnings_sha256", "corrected_text_sha256", "created_at", "lifecycle"}
        try:
            records = self._extraction_decision_records()
        except (OSError, ValueError, ReviewStudioError) as exc:
            return [f"extraction-decisions: 无法读取决定链：{exc}"]
        valid: list[tuple[Path, dict[str, Any], str]] = []
        for path, value, digest in records:
            if set(value) != expected or value.get("artifact_type") != "extraction-decision" or value.get("schema_version") != 1 or value.get("lifecycle") != "append-only":
                errors.append(f"{path.name}: extraction decision fields or policy invalid")
                continue
            allowed_states = {"confirm": "confirmed", "continue_with_warning": "confirmed_with_warning", "correct": "confirmed_corrected", "replace": "replacement_required"}
            if value.get("decision") not in allowed_states or value.get("extraction_state") != allowed_states.get(value.get("decision")) or not isinstance(value.get("sequence"), int) or value.get("sequence", 0) < 1:
                errors.append(f"{path.name}: extraction decision values invalid")
                continue
            valid.append((path, value, digest))
        if not valid:
            return errors
        ordered = sorted(valid, key=lambda row: row[1]["sequence"])
        sequences = [row[1]["sequence"] for row in ordered]
        if sequences != list(range(1, len(ordered) + 1)):
            errors.append("extraction decisions: sequence must be continuous")
            return errors
        for index, (_, value, digest) in enumerate(ordered):
            expected_previous = None if index == 0 else ordered[index - 1][2]
            if value.get("previous_extraction_decision_sha256") != expected_previous:
                errors.append(f"{value.get('decision_id')}: previous extraction decision mismatch")
        latest = ordered[-1][1]
        current_paths = {
            "document_relative_path": self.document_path,
            "quality_relative_path": self.root / "extraction" / "quality.json",
            "warnings_relative_path": self.root / "extraction" / "warnings.json",
        }
        for path_key, path in current_paths.items():
            relative = str(path.relative_to(self.root)).replace("\\", "/")
            digest_key = path_key.replace("relative_path", "sha256")
            if latest.get(path_key) != relative:
                errors.append(f"latest extraction decision path mismatch: {path_key}")
            if not path.is_file() or _sha256(path.read_bytes()) != latest.get(digest_key):
                errors.append(f"latest extraction decision is not bound to current {path_key}")
        source = self.manifest().get("source", {})
        if latest.get("source_sha256") != source.get("sha256"):
            errors.append("latest extraction decision source mismatch")
        return errors

    def _authoritative_extraction_decision(self) -> dict[str, Any] | None:
        if self._extraction_decision_chain_errors():
            return None
        return self._latest_extraction_decision()

    def integrity_errors(self) -> list[str]:
        """Recheck the complete artifact chain before every state-changing action."""
        errors: list[str] = []
        try:
            try:
                policy = _ensure_integrity_policy(self.root)
            except ReviewStudioError as exc:
                errors.append(str(exc))
                return errors
            policy_hash = _sha256(_integrity_policy_path(self.root).read_bytes())
            index_path = _integrity_index_path(self.root)
            integrity_index: dict[str, Any] | None = None
            if index_path.is_symlink() or not index_path.is_file():
                errors.append("integrity-index.json: project artifact register missing")
            else:
                try:
                    integrity_index = _read_json(index_path)
                except (OSError, ValueError, ReviewStudioError) as exc:
                    errors.append(f"integrity-index.json: invalid project artifact register: {exc}")
            manifest = self.manifest()
            source = manifest.get("source", {})
            relative = str(source.get("relative_path", ""))
            source_path = _safe_child(self.root, relative)
            if source_path.is_symlink() or not source_path.is_file():
                errors.append("原始文件缺失或不是普通文件")
            else:
                raw = source_path.read_bytes()
                if _sha256(raw) != source.get("sha256"):
                    errors.append("原始文件 SHA-256 不匹配")
                if len(raw) != source.get("bytes"):
                    errors.append("原始文件字节数不匹配")
            protected: set[Path] = set()
            for name in ("project.json", "context.json", "audit-log.jsonl", INTEGRITY_INDEX_NAME):
                path = self.root / name
                if path.exists() or path.is_symlink():
                    protected.add(path)
            for dirname in ("source", "extraction", EXTRACTION_DECISION_DIR_NAME, "ai-requests", "audits", "finding-decisions", "exports"):
                directory = self.root / dirname
                if not directory.is_dir():
                    continue
                for path in directory.rglob("*"):
                    if INTEGRITY_RECEIPT_DIR in path.parts or path.is_dir():
                        continue
                    protected.add(path)
            expected_receipt_fields = {"artifact_type", "schema_version", "artifact_relative_path", "artifact_sha256", "policy_sha256", "policy_enabled_at", "parents", "provenance", "lifecycle"}
            for path in sorted(protected):
                relative_path = str(path.relative_to(self.root)).replace("\\", "/")
                receipt_path = _integrity_receipt_path(path)
                if path.is_symlink() or not path.is_file():
                    errors.append(f"{relative_path}: 产物缺失或不是普通文件")
                    continue
                if receipt_path.is_symlink() or not receipt_path.is_file():
                    errors.append(f"{relative_path}: integrity receipt missing")
                    continue
                try:
                    receipt = _read_json(receipt_path)
                    if set(receipt) != expected_receipt_fields or receipt.get("artifact_type") != "document-review-artifact-integrity" or receipt.get("schema_version") != 1 or receipt.get("lifecycle") not in {"immutable", "append-only"} or not isinstance(receipt.get("provenance"), str):
                        errors.append(f"{relative_path}: invalid integrity receipt fields")
                        continue
                    if receipt.get("artifact_relative_path") != relative_path:
                        errors.append(f"{relative_path}: integrity receipt path mismatch")
                    if receipt.get("artifact_sha256") != _sha256(path.read_bytes()):
                        errors.append(f"{relative_path}: integrity receipt hash mismatch")
                    if receipt.get("policy_sha256") != policy_hash:
                        errors.append(f"{relative_path}: integrity policy binding mismatch")
                    if receipt.get("policy_enabled_at") != policy.get("enabled_at"):
                        errors.append(f"{relative_path}: integrity policy timestamp mismatch")
                    parents = receipt.get("parents")
                    if not isinstance(parents, list):
                        errors.append(f"{relative_path}: integrity parents invalid")
                        continue
                    for parent in parents:
                        if not isinstance(parent, dict) or set(parent) != {"role", "relative_path", "sha256"}:
                            errors.append(f"{relative_path}: parent binding invalid")
                            continue
                        parent_path = _safe_child(self.root, str(parent.get("relative_path", "")))
                        if parent_path.is_symlink() or not parent_path.is_file():
                            errors.append(f"{relative_path}: parent artifact missing: {parent.get('relative_path')}")
                        elif _sha256(parent_path.read_bytes()) != parent.get("sha256"):
                            errors.append(f"{relative_path}: parent artifact hash mismatch: {parent.get('relative_path')}")
                except (OSError, ValueError, KeyError, TypeError, ReviewStudioError) as exc:
                    errors.append(f"{relative_path}: invalid integrity receipt: {exc}")
            latest_index_entries: dict[str, dict[str, Any]] = {}
            if integrity_index is not None:
                expected_index_fields = {"artifact_type", "schema_version", "index_id", "entries", "head_sha256", "next_sequence", "lifecycle"}
                if set(integrity_index) != expected_index_fields or integrity_index.get("artifact_type") != "document-review-integrity-index" or integrity_index.get("schema_version") != 1 or integrity_index.get("lifecycle") != "append-only" or not isinstance(integrity_index.get("index_id"), str):
                    errors.append("integrity-index.json: invalid index fields")
                entries = integrity_index.get("entries")
                if not isinstance(entries, list):
                    errors.append("integrity-index.json: entries must be an array")
                    entries = []
                expected_entry_fields = {"artifact_id", "relative_path", "sha256", "receipt_relative_path", "receipt_sha256", "artifact_type", "sequence", "previous_index_head_sha256", "created_at", "entry_sha256"}
                previous_head: str | None = None
                for expected_sequence, entry in enumerate(entries, start=1):
                    if not isinstance(entry, dict) or set(entry) != expected_entry_fields:
                        errors.append(f"integrity-index.json: invalid entry at sequence {expected_sequence}")
                        continue
                    if entry.get("sequence") != expected_sequence:
                        errors.append(f"integrity-index.json: sequence gap at {expected_sequence}")
                    if entry.get("previous_index_head_sha256") != previous_head:
                        errors.append(f"integrity-index.json: previous head mismatch at sequence {expected_sequence}")
                    if _integrity_index_entry_hash(entry) != entry.get("entry_sha256"):
                        errors.append(f"integrity-index.json: entry hash mismatch at sequence {expected_sequence}")
                    relative_entry_path = entry.get("relative_path")
                    receipt_entry_path = entry.get("receipt_relative_path")
                    if not isinstance(relative_entry_path, str) or not isinstance(receipt_entry_path, str) or not isinstance(entry.get("artifact_id"), str) or not isinstance(entry.get("artifact_type"), str) or not isinstance(entry.get("created_at"), str):
                        errors.append(f"integrity-index.json: entry types invalid at sequence {expected_sequence}")
                        continue
                    try:
                        artifact_entry_path = _safe_child(self.root, relative_entry_path)
                        expected_receipt_path = _integrity_receipt_path(artifact_entry_path)
                        if str(expected_receipt_path.relative_to(self.root)).replace("\\", "/") != receipt_entry_path:
                            errors.append(f"integrity-index.json: receipt path mismatch at sequence {expected_sequence}")
                    except (OSError, ValueError, ReviewStudioError) as exc:
                        errors.append(f"integrity-index.json: unsafe entry path at sequence {expected_sequence}: {exc}")
                        continue
                    latest_index_entries[relative_entry_path] = entry
                    previous_head = entry.get("entry_sha256")
                if integrity_index.get("head_sha256") != previous_head:
                    errors.append("integrity-index.json: head hash mismatch")
                if integrity_index.get("next_sequence") != len(entries) + 1:
                    errors.append("integrity-index.json: next sequence mismatch")
                current_artifacts = {
                    str(path.relative_to(self.root)).replace("\\", "/")
                    for path in protected
                    if path != index_path
                }
                expected_artifacts = set(latest_index_entries)
                for relative_entry_path in sorted(expected_artifacts - current_artifacts):
                    errors.append(f"integrity index artifact missing: {relative_entry_path}")
                for relative_path in sorted(current_artifacts - expected_artifacts):
                    errors.append(f"unregistered integrity artifact: {relative_path}")
                for relative_entry_path, entry in latest_index_entries.items():
                    try:
                        artifact_path = _safe_child(self.root, relative_entry_path)
                        if artifact_path.is_symlink() or not artifact_path.is_file():
                            continue
                        if _sha256(artifact_path.read_bytes()) != entry.get("sha256"):
                            errors.append(f"integrity-index artifact hash mismatch: {relative_entry_path}")
                        receipt_path = _integrity_receipt_path(artifact_path)
                        if receipt_path.is_file() and not receipt_path.is_symlink() and _sha256(receipt_path.read_bytes()) != entry.get("receipt_sha256"):
                            errors.append(f"integrity-index receipt hash mismatch: {relative_entry_path}")
                    except (OSError, ValueError, ReviewStudioError) as exc:
                        errors.append(f"integrity-index artifact validation failed: {relative_entry_path}: {exc}")
            for receipt_dir in self.root.rglob(INTEGRITY_RECEIPT_DIR):
                if receipt_dir.is_symlink() or not receipt_dir.is_dir():
                    errors.append(f"{receipt_dir}: integrity receipt directory invalid")
                    continue
                for receipt_path in receipt_dir.glob("*.json"):
                    artifact_path = receipt_dir.parent / receipt_path.name[:-5]
                    if artifact_path not in protected:
                        errors.append(f"{receipt_path}: orphan integrity receipt")
            try:
                document = self.document()
                if document is not None and document.source.sha256 != source.get("sha256"):
                    errors.append("结构化文档未绑定当前原始文件")
            except (OSError, ValueError, KeyError, TypeError, ReviewStudioError) as exc:
                errors.append(f"结构化文档无法读取：{exc}")
            errors.extend(self._extraction_decision_chain_errors())
            errors.extend(self._decision_chain_errors())
            errors.extend(self._audit_log_chain_errors())
        except (OSError, KeyError, TypeError, ValueError, ReviewStudioError) as exc:
            errors.append(f"项目完整性检查失败：{exc}")
        return errors

    def _enforce_integrity(self) -> None:
        errors = self.integrity_errors()
        state = self.state()
        if errors and not state.get("read_only"):
            self._update_state(read_only=True, integrity_errors=errors)

    def _ensure_writable(self) -> None:
        self._enforce_integrity()
        state = self.state()
        if state.get("read_only"):
            raise ReviewStudioError("项目完整性校验失败，当前强制只读：" + "; ".join(state.get("integrity_errors", [])))

    def document(self) -> StructuredDocument | None:
        if not self.document_path.is_file():
            return None
        value = _read_json(self.document_path)
        return _document_from_dict(value)

    def _save_document(self, document: StructuredDocument) -> None:
        source_path = _safe_child(self.root, self.manifest()["source"]["relative_path"])
        source_parent = _parent_ref(self.root, source_path, role="original-source")
        document_bytes = canonical_json(document.to_dict())
        original_path = self.root / "extraction" / "document-original.json"
        _write_tracked(self.root, original_path, document_bytes, parents=[source_parent], provenance="parser-derived")
        _write_tracked(self.root, self.document_path, document_bytes, parents=[source_parent, _parent_ref(self.root, original_path, role="initial-structured-document")], provenance="parser-derived-current")
        document_parent = _parent_ref(self.root, self.document_path, role="structured-document")
        _write_tracked(self.root, self.root / "extraction" / "quality.json", canonical_json(document.quality.to_dict()), parents=[document_parent])
        _write_tracked(self.root, self.root / "extraction" / "warnings.json", canonical_json({"warnings": [warning.to_dict() for warning in document.warnings]}), parents=[document_parent])
        _write_tracked(self.root, self.root / "extraction" / "source-map.json", canonical_json({"source_to_block": document.source_to_block}), parents=[document_parent])

    def _update_state(self, **updates: Any) -> dict[str, Any]:
        state = self.state()
        state.update(updates)
        _atomic_write(self.state_path, canonical_json(state))
        return state

    def _audit_log_chain_errors(self) -> list[str]:
        path = self.root / "audit-log.jsonl"
        if not path.exists():
            return []
        errors: list[str] = []
        expected = {"artifact_type", "schema_version", "event_id", "sequence", "previous_event_sha256", "event", "created_at", "payload", "event_sha256", "lifecycle"}
        try:
            raw_lines = path.read_bytes().splitlines()
        except OSError as exc:
            return [f"audit-log.jsonl: 无法读取事件链：{exc}"]
        previous: str | None = None
        for sequence, raw_line in enumerate(raw_lines, start=1):
            try:
                value = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                errors.append(f"audit-log.jsonl:{sequence}: invalid event JSON: {exc}")
                continue
            if not isinstance(value, dict) or set(value) != expected or value.get("artifact_type") != "audit-event" or value.get("schema_version") != 1 or value.get("lifecycle") != "append-only" or not isinstance(value.get("event"), str) or not isinstance(value.get("payload"), dict):
                errors.append(f"audit-log.jsonl:{sequence}: invalid event fields")
                continue
            if value.get("sequence") != sequence:
                errors.append(f"audit-log.jsonl:{sequence}: event sequence mismatch")
            if value.get("previous_event_sha256") != previous:
                errors.append(f"audit-log.jsonl:{sequence}: previous event hash mismatch")
            event_without_hash = {key: item for key, item in value.items() if key != "event_sha256"}
            event_hash = _sha256(canonical_json(event_without_hash))
            if value.get("event_sha256") != event_hash:
                errors.append(f"audit-log.jsonl:{sequence}: event hash mismatch")
            previous = value.get("event_sha256") if isinstance(value.get("event_sha256"), str) else None
        return errors

    def _append_event(self, event: str, payload: Mapping[str, Any]) -> None:
        path = self.root / "audit-log.jsonl"
        if path.is_symlink():
            raise ReviewStudioError("审计日志不能是符号链接")
        existing = path.read_bytes() if path.is_file() else b""
        if existing and self._audit_log_chain_errors():
            raise ReviewStudioError("审计日志事件链无效，拒绝继续写入")
        records = [json.loads(line.decode("utf-8")) for line in existing.splitlines()] if existing else []
        previous = records[-1].get("event_sha256") if records else None
        event_record = {"artifact_type": "audit-event", "schema_version": 1, "event_id": stable_id("EV", self.manifest().get("project_id", ""), event, _now(), secrets.token_hex(4)), "sequence": len(records) + 1, "previous_event_sha256": previous, "event": event, "created_at": _now(), "payload": dict(payload), "lifecycle": "append-only"}
        event_record["event_sha256"] = _sha256(canonical_json(event_record))
        updated = existing + canonical_json(event_record)
        if path.is_file():
            _replace_tracked(self.root, path, updated, provenance="append-only-audit-event-chain", artifact_type="audit-event-chain")
        else:
            _write_tracked(self.root, path, updated, provenance="append-only-audit-event-chain", artifact_type="audit-event-chain")

    def extraction_quality(self) -> dict[str, Any]:
        document = self.document()
        return {"quality": document.quality.to_dict() if document else {}, "warnings": [warning.to_dict() for warning in document.warnings] if document else [], "diagnostics": self.state().get("diagnostics", [])}

    def suggested_document_type(self) -> str:
        try:
            document = self.document()
        except (OSError, KeyError, TypeError, ValueError, ReviewStudioError):
            document = None
        text = document.plain_text.casefold() if document else ""
        if any(word in text for word in ("活动", "策划", "预算", "负责人", "场地")):
            return "活动策划案"
        if any(word in text for word in ("通知", "关于", "发文", "附件", "落款")):
            return "公文/通知"
        if any(word in text for word in ("章程", "理事会", "申诉", "处分", "回避")):
            return "组织章程/治理制度"
        if any(word in text for word in ("项目方案", "里程碑", "验收")):
            return "项目执行方案"
        return "专业文档"

    def _append_extraction_decision(self, decision: str, extraction_state: str, *, corrected_text_sha256: str | None = None) -> dict[str, Any]:
        document = self.document()
        if document is None:
            raise ReviewStudioError("没有可供确认的结构化识别结果")
        quality_path = self.root / "extraction" / "quality.json"
        warnings_path = self.root / "extraction" / "warnings.json"
        previous = self._latest_extraction_decision()
        previous_path = None
        sequence = 1
        previous_hash = None
        if previous:
            sequence = int(previous["sequence"]) + 1
            previous_path = self.root / EXTRACTION_DECISION_DIR_NAME / f"{previous['decision_id']}.json"
            if previous_path.is_file():
                previous_hash = _sha256(previous_path.read_bytes())
        decision_id = stable_id("EXD", document.source.sha256, decision, str(sequence), _now(), secrets.token_hex(4))
        record = {
            "artifact_type": "extraction-decision",
            "schema_version": 1,
            "decision_id": decision_id,
            "sequence": sequence,
            "previous_extraction_decision_sha256": previous_hash,
            "decision": decision,
            "extraction_state": extraction_state,
            "source_sha256": document.source.sha256,
            "document_relative_path": str(self.document_path.relative_to(self.root)).replace("\\", "/"),
            "document_sha256": _sha256(self.document_path.read_bytes()),
            "quality_relative_path": str(quality_path.relative_to(self.root)).replace("\\", "/"),
            "quality_sha256": _sha256(quality_path.read_bytes()),
            "warnings_relative_path": str(warnings_path.relative_to(self.root)).replace("\\", "/"),
            "warnings_sha256": _sha256(warnings_path.read_bytes()),
            "corrected_text_sha256": corrected_text_sha256,
            "created_at": _now(),
            "lifecycle": "append-only",
        }
        parents = [
            _parent_ref(self.root, _safe_child(self.root, self.manifest()["source"]["relative_path"]), role="original-source"),
            _parent_ref(self.root, self.document_path, role="current-structured-document"),
            _parent_ref(self.root, quality_path, role="current-extraction-quality"),
            _parent_ref(self.root, warnings_path, role="current-extraction-warnings"),
        ]
        if previous_path is not None:
            parents.append(_parent_ref(self.root, previous_path, role="previous-extraction-decision"))
        _write_tracked(self.root, self.root / EXTRACTION_DECISION_DIR_NAME / f"{decision_id}.json", canonical_json(record), parents=parents, provenance="human-confirmed-extraction-decision", artifact_type="extraction-decision")
        return record

    def confirm_extraction(self, choice: str, *, corrected_text: str | None = None) -> dict[str, Any]:
        self._ensure_writable()
        if choice not in {"confirm", "correct", "continue_with_warning", "replace"}:
            raise ReviewStudioError("识别确认选项必须是 confirm、correct、continue_with_warning 或 replace")
        if choice == "replace":
            self._append_extraction_decision("replace", "replacement_required")
            self._append_event("extraction_replaced", {"decision": "user_must_upload_replacement"})
            return self._update_state(extraction_state="replacement_required")
        document = self.document()
        if document is None:
            raise ReviewStudioError("当前文件没有可确认的结构化识别结果，请更换文件")
        hard_block_codes = {"ocr-unavailable", "pdf-renderer-unavailable"}
        if choice in {"confirm", "continue_with_warning"} and any(warning.code in hard_block_codes for warning in document.warnings):
            raise ReviewStudioError("当前 PDF 识别缺少 OCR 或渲染器；不能把残缺文本送入审查，请安装依赖、修正识别文本或更换文件")
        if choice == "correct":
            if not isinstance(corrected_text, str) or not corrected_text.strip():
                raise ReviewStudioError("修正识别文本不能为空")
            corrected_bytes = corrected_text.encode("utf-8")
            if len(corrected_bytes) > MAX_TEXT_CORRECTION_BYTES:
                raise ReviewStudioError("人工修正文本超过安全上限")
            corrected = ingest_bytes("human-correction.md", corrected_bytes)
            corrected.source = document.source
            corrected.document_id = document.document_id
            corrected.parser_name = "human-correction"
            corrected.quality.human_corrected = True
            corrected.quality.requires_confirmation = False
            corrected.warnings.extend(document.warnings)
            original_document_parent = _parent_ref(self.root, self.document_path, role="previous-structured-document")
            correction_path = self.root / "extraction" / "human-correction.md"
            _write_tracked(self.root, correction_path, corrected_bytes, parents=[original_document_parent], provenance="human-confirmed")
            corrected_path = self.root / "extraction" / "document-corrected.json"
            corrected_document_bytes = canonical_json(corrected.to_dict())
            _write_tracked(self.root, corrected_path, corrected_document_bytes, parents=[_parent_ref(self.root, correction_path, role="recognition-correction"), original_document_parent], provenance="human-confirmed-derived")
            _replace_tracked(self.root, self.document_path, corrected_document_bytes, parents=[_parent_ref(self.root, corrected_path, role="current-structured-document")], provenance="human-confirmed-current")
            corrected_parent = _parent_ref(self.root, self.document_path, role="structured-document")
            _replace_tracked(self.root, self.root / "extraction" / "quality.json", canonical_json(corrected.quality.to_dict()), parents=[corrected_parent])
            _replace_tracked(self.root, self.root / "extraction" / "warnings.json", canonical_json({"warnings": [warning.to_dict() for warning in corrected.warnings]}), parents=[corrected_parent])
            _replace_tracked(self.root, self.root / "extraction" / "source-map.json", canonical_json({"source_to_block": corrected.source_to_block}), parents=[corrected_parent])
            self._append_extraction_decision("correct", "confirmed_corrected", corrected_text_sha256=_sha256(corrected_bytes))
            self._append_event("extraction_corrected", {"corrected_sha256": _sha256(corrected_bytes), "source_sha256": document.source.sha256})
            return self._update_state(extraction_state="confirmed_corrected", read_only=False)
        if choice == "confirm" and any(w.severity in {"critical", "high"} for w in document.warnings):
            raise ReviewStudioError("识别存在高风险警告；请选择带警告继续或先修正/更换文件")
        state_name = "confirmed" if choice == "confirm" else "confirmed_with_warning"
        self._append_extraction_decision(choice, state_name)
        self._append_event("extraction_confirmed", {"decision": choice, "source_sha256": document.source.sha256})
        return self._update_state(extraction_state=state_name, read_only=False)

    def confirm_context(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_writable()
        extraction_decision = self._authoritative_extraction_decision()
        if extraction_decision is None or extraction_decision.get("extraction_state") not in {"confirmed", "confirmed_corrected", "confirmed_with_warning"}:
            raise ReviewStudioError("必须先完成并保存可信的识别确认决定")
        required = {"document_type", "jurisdiction", "effective_date", "publisher_type", "audience", "involves_minors", "involves_fees", "involves_sponsorship", "involves_contract", "involves_personal_information", "involves_intellectual_property", "publication_status"}
        missing = sorted(required - set(payload))
        if missing:
            raise ReviewStudioError("文档上下文缺少字段：" + ", ".join(missing))
        if not all(isinstance(payload[name], bool) for name in required if name.startswith("involves_")):
            raise ReviewStudioError("涉及范围字段必须是布尔值")
        if payload.get("publication_status") not in {"internal-draft", "external-formal"}:
            raise ReviewStudioError("publication_status 必须是 internal-draft 或 external-formal")
        context = ReviewContext(**{name: payload[name] for name in required}, confirmed=True, model_suggestion=self.suggested_document_type(), user_provided_materials=list(payload.get("user_provided_materials", [])))
        context_path = self.root / "context.json"
        _write_tracked(self.root, context_path, canonical_json(context.to_dict()), parents=[_parent_ref(self.root, self.document_path, role="structured-document")], provenance="human-confirmed")
        self._append_event("context_confirmed", context.to_dict())
        return self._update_state(context_state="confirmed")

    def context(self) -> ReviewContext | None:
        path = self.root / "context.json"
        if not path.is_file():
            return None
        value = _read_json(path)
        return ReviewContext(**{key: value.get(key, default) for key, default in {
            "document_type": "专业文档", "jurisdiction": "", "effective_date": "", "publisher_type": "", "audience": "", "involves_minors": False, "involves_fees": False, "involves_sponsorship": False, "involves_contract": False, "involves_personal_information": False, "involves_intellectual_property": False, "publication_status": "internal-draft", "confirmed": False, "model_suggestion": None, "user_provided_materials": [],
        }.items()})

    def can_review(self) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        integrity = self.integrity_errors()
        if integrity:
            reasons.append("项目完整性校验失败")
        extraction_decision = self._authoritative_extraction_decision()
        if extraction_decision is None or extraction_decision.get("extraction_state") not in {"confirmed", "confirmed_corrected", "confirmed_with_warning"}:
            reasons.append("识别结果尚未由用户确认")
        context = self.context()
        if context is None or not context.confirmed:
            reasons.append("文档类型与适用上下文尚未确认")
        try:
            document = self.document()
        except (OSError, KeyError, TypeError, ValueError, ReviewStudioError) as exc:
            document = None
            reasons.append(f"结构化文档无法读取：{exc}")
        if not document:
            reasons.append("没有结构化内部文档")
        elif not document.quality.human_corrected and any(warning.code in {"ocr-unavailable", "pdf-renderer-unavailable"} for warning in document.warnings):
            reasons.append("OCR 或 PDF 渲染能力缺失，识别结果不允许进入正式审查")
        return not reasons, reasons

    def prompt(self, critic: str) -> str:
        if critic not in CRITIC_DIMENSIONS:
            raise ReviewStudioError("未知审查维度")
        document = self.document()
        context = self.context()
        if not document or not context:
            raise ReviewStudioError("需要先完成识别和上下文确认")
        contract = {
            "critic": critic,
            "protocol": CRITIC_PROTOCOLS[critic],
            "source_sha256": document.source.sha256,
            "document_type": context.document_type,
            "required_finding_fields": ["finding_id", "critic", "document_type", "location", "evidence", "issue", "standard", "consequence", "severity", "verification_state", "external_basis", "uncertainties", "suggested_action", "suggested_owner", "blocks_release_or_execution"],
            "required_response_envelope": ["request_id", "prompt_sha256", "provider", "model"],
            "rules": {"independent": True, "do_not_vote_or_score": True, "location_must_use_block_or_page": True, "legal_screen_never_claims_counsel": True},
        }
        return "# Document Review Studio independent AI review\n\nYou are exactly one independent critic. Return strict JSON only. Do not run another critic, merge dimensions, vote, score, or infer external facts without a source.\n\n## Contract and critic-specific protocol\n```json\n" + json.dumps(contract, ensure_ascii=False, indent=2) + "\n```\n\n## Confirmed review context\n```json\n" + json.dumps(context.to_dict(), ensure_ascii=False, indent=2) + "\n```\n\n## Internal document blocks\n```json\n" + json.dumps([block.to_dict() for block in document.blocks], ensure_ascii=False, indent=2) + "\n```\n"

    def run_local_prechecks(self, critics: Iterable[str] | None = None) -> list[AuditRun]:
        self._ensure_writable()
        allowed, reasons = self.can_review()
        if not allowed:
            raise ReviewStudioError("；".join(reasons))
        document = self.document()
        context = self.context()
        assert document is not None and context is not None
        selected = list(critics or CRITIC_DIMENSIONS)
        if not selected or any(critic not in CRITIC_DIMENSIONS for critic in selected):
            raise ReviewStudioError("审查维度无效")
        runs: list[AuditRun] = []
        for critic in selected:
            run = self._deterministic_audit(critic, document, context)
            directory = self.root / "audits" / critic
            prompt_path = directory / f"{run.run_id}.local-precheck-protocol.md"
            parents = [_parent_ref(self.root, self.document_path, role="structured-document"), _parent_ref(self.root, self.root / "context.json", role="review-context")]
            _write_tracked(self.root, prompt_path, self.prompt(critic).encode("utf-8"), parents=parents, provenance="deterministic-local-precheck-protocol")
            run_path = directory / f"{run.run_id}.json"
            _write_tracked(self.root, run_path, canonical_json(run.to_dict()), parents=[*parents, _parent_ref(self.root, prompt_path, role="local-precheck-protocol")], provenance="deterministic-local-precheck")
            self._append_event("local_precheck_created", {"run_id": run.run_id, "critic": critic, "finding_ids": [f.finding_id for f in run.findings]})
            runs.append(run)
        self._update_state(review_state="local_precheck_completed", last_audit_at=_now())
        return runs

    def run_audits(self, critics: Iterable[str] | None = None) -> list[AuditRun]:
        """Compatibility alias; these are deterministic local prechecks, not AI reviews."""
        return self.run_local_prechecks(critics)

    def prepare_ai_audits(self, critics: Iterable[str] | None = None, *, provider: str, model: str) -> list[dict[str, Any]]:
        self._ensure_writable()
        allowed, reasons = self.can_review()
        if not allowed:
            raise ReviewStudioError("；".join(reasons))
        if not provider.strip() or not model.strip():
            raise ReviewStudioError("独立 AI 审查必须记录 provider 和 model")
        selected = list(critics or CRITIC_DIMENSIONS)
        if not selected or any(critic not in CRITIC_DIMENSIONS for critic in selected):
            raise ReviewStudioError("审查维度无效")
        parents = [_parent_ref(self.root, self.document_path, role="structured-document"), _parent_ref(self.root, self.root / "context.json", role="review-context")]
        rows: list[dict[str, Any]] = []
        for critic in selected:
            base_prompt = self.prompt(critic).encode("utf-8")
            prompt_sha256 = _sha256(base_prompt)
            normalized_provider = provider.strip()
            normalized_model = model.strip()
            request_id = stable_id("AIR", critic, prompt_sha256, normalized_provider, normalized_model, _now(), secrets.token_hex(4))
            envelope = {"request_id": request_id, "prompt_sha256": prompt_sha256, "provider": normalized_provider, "model": normalized_model}
            prompt = base_prompt + ("\n## Required response envelope\nReturn these four fields exactly as shown, in addition to the review result:\n```json\n" + json.dumps(envelope, ensure_ascii=False, indent=2) + "\n```\nDo not omit or alter any envelope value.\n").encode("utf-8")
            directory = self.root / "ai-requests" / request_id
            prompt_path = directory / "prompt.md"
            _write_tracked(self.root, prompt_path, prompt, parents=parents, provenance="deterministic-ai-protocol")
            request = {"artifact_type": "independent-ai-review-request", "schema_version": 1, "request_id": request_id, "critic": critic, "provider": normalized_provider, "model": normalized_model, "prompt_sha256": prompt_sha256, "prompt_file_sha256": _sha256(prompt), "source_sha256": self.document().source.sha256, "created_at": _now(), "lifecycle": "immutable"}
            request_path = directory / "request.json"
            _write_tracked(self.root, request_path, canonical_json(request), parents=[*parents, _parent_ref(self.root, prompt_path, role="critic-prompt")], provenance="deterministic-ai-request")
            rows.append({**request, "prompt": prompt.decode("utf-8"), "relative_path": str(prompt_path.relative_to(self.root)).replace("\\", "/")})
        self._update_state(ai_review_state="protocols_ready", last_ai_protocol_at=_now())
        return rows

    def collect_model_audit(self, critic: str, response: bytes | str, *, provider: str = "external", model: str = "unlabelled", request_id: str | None = None, model_label: str | None = None) -> AuditRun:
        """Validate and archive one provider-neutral model response.

        The raw response is stored separately from the parsed run.  A model
        result can never replace a deterministic run or another critic's
        result, and every location must resolve to this document's blocks.
        """
        self._ensure_writable()
        allowed, reasons = self.can_review()
        if not allowed:
            raise ReviewStudioError("；".join(reasons))
        document = self.document()
        context = self.context()
        assert document is not None and context is not None
        if critic not in CRITIC_DIMENSIONS:
            raise ReviewStudioError("未知审查维度")
        if model_label and model == "unlabelled":
            model = model_label
        if not provider.strip() or not model.strip():
            raise ReviewStudioError("模型审查导入必须记录 provider 和 model")
        requests: list[tuple[Path, dict[str, Any]]] = []
        for path in (self.root / "ai-requests").glob("*/request.json") if (self.root / "ai-requests").is_dir() else []:
            value = _read_json(path)
            request_matches = value.get("request_id") == request_id if request_id is not None else (value.get("provider") == provider and value.get("model") == model)
            if value.get("critic") == critic and request_matches:
                requests.append((path, value))
        if not requests:
            prepared = self.prepare_ai_audits([critic], provider=provider, model=model)[0]
            request_path = self.root / "ai-requests" / prepared["request_id"] / "request.json"
            request = _read_json(request_path)
        else:
            request_path, request = sorted(requests, key=lambda row: row[1]["created_at"])[-1]
        if request.get("provider") != provider or request.get("model") != model:
            raise ReviewStudioError("导入结果的 provider/model 与已导出协议不一致")
        prompt_path = request_path.parent / "prompt.md"
        if _sha256(prompt_path.read_bytes()) != request.get("prompt_file_sha256"):
            raise ReviewStudioError("AI 审查 prompt hash 不匹配")
        raw = response.encode("utf-8") if isinstance(response, str) else response
        if not isinstance(raw, bytes) or not raw:
            raise ReviewStudioError("模型审查返回不能为空")

        def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ReviewStudioError(f"模型 JSON 含重复字段：{key}")
                value[key] = item
            return value

        try:
            parsed = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=reject_duplicate)
        except (UnicodeDecodeError, json.JSONDecodeError, ReviewStudioError) as exc:
            raise ReviewStudioError(f"模型审查返回不是严格 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ReviewStudioError("模型审查返回必须是 JSON 对象")
        expected_envelope = {
            "request_id": request.get("request_id"),
            "prompt_sha256": request.get("prompt_sha256"),
            "provider": request.get("provider"),
            "model": request.get("model"),
        }
        for field, expected in expected_envelope.items():
            if parsed.get(field) != expected:
                raise ReviewStudioError(f"模型返回的 {field} 未与已导出请求逐项匹配")
        if parsed.get("critic") != critic:
            raise ReviewStudioError("模型返回的 critic 与提交维度不一致")
        if parsed.get("source_sha256") != document.source.sha256:
            raise ReviewStudioError("模型返回没有绑定当前原始文件 SHA-256")
        raw_findings = parsed.get("findings")
        if not isinstance(raw_findings, list):
            raise ReviewStudioError("模型返回缺少 findings 数组")
        block_ids = {block.block_id for block in document.blocks}
        findings: list[Finding] = []
        for index, item in enumerate(raw_findings):
            if not isinstance(item, dict):
                raise ReviewStudioError(f"第 {index + 1} 条 Finding 不是对象")
            errors = validate_finding_dict(item)
            if errors:
                raise ReviewStudioError(f"第 {index + 1} 条 Finding contract 无效：" + "; ".join(errors))
            if item["critic"] != critic:
                raise ReviewStudioError(f"第 {index + 1} 条 Finding 跨 critic，拒绝合并")
            if item["location"].get("block_id") not in block_ids:
                raise ReviewStudioError(f"第 {index + 1} 条 Finding 定位不到内部 block")
            finding = _finding_from_dict(item)
            finding.origin = "model-derived"
            findings.append(finding)
        run = AuditRun(stable_id("RUN", document.source.sha256, critic, _now(), secrets.token_hex(4)), critic, document.document_id, document.source.sha256, context, findings, list(parsed.get("observations", [])) if isinstance(parsed.get("observations", []), list) else [], list(parsed.get("zero_finding_basis", [])) if isinstance(parsed.get("zero_finding_basis", []), list) else [], f"manual-import:{provider}/{model}", _now())
        directory = self.root / "audits" / critic
        raw_path = directory / f"{run.run_id}.raw-response.json.txt"
        response_parents = [_parent_ref(self.root, prompt_path, role="critic-prompt"), _parent_ref(self.root, request_path, role="ai-review-request")]
        _write_tracked(self.root, raw_path, raw, parents=response_parents, provenance="model-raw-response")
        run_value = run.to_dict()
        run_value["declared_model_metadata"] = {"provider": provider, "model": model, "request_id": request["request_id"], "prompt_sha256": request["prompt_sha256"], "prompt_file_sha256": request["prompt_file_sha256"], "raw_response_sha256": _sha256(raw), "import_mode": "manual"}
        run_path = directory / f"{run.run_id}.json"
        _write_tracked(self.root, run_path, canonical_json(run_value), parents=[*response_parents, _parent_ref(self.root, raw_path, role="raw-model-response")], provenance="model-parsed-audit")
        self._append_event("model_audit_imported", {"run_id": run.run_id, "critic": critic, "declared_model_metadata": run_value["declared_model_metadata"], "finding_ids": [finding.finding_id for finding in findings]})
        self._update_state(review_state="ai_review_imported", ai_review_state="imported", last_audit_at=_now())
        return run

    def _finding(self, critic: str, document: StructuredDocument, context: ReviewContext, block: DocumentBlock, *, issue: str, standard: str, consequence: str, severity: str = "medium", verification_state: str = "needs-human-verification", suggested_action: str, owner: str = "文档负责人", blocks: bool = False, uncertainties: list[str] | None = None, basis: ExternalBasis | None = None, evidence: str | None = None, competing: list[str] | None = None, observation: str = "") -> Finding:
        finding_id = stable_id("F", document.source.sha256, critic, block.block_id, issue)[:22]
        return Finding(finding_id, critic, context.document_type, make_location(block), evidence or block.text, issue, standard, consequence, severity, verification_state, basis or ExternalBasis(jurisdiction=context.jurisdiction, unresolved_facts=list(uncertainties or [])), list(uncertainties or []), suggested_action, owner, blocks, competing_readings=list(competing or []), required_observation=observation)

    def _deterministic_audit(self, critic: str, document: StructuredDocument, context: ReviewContext) -> AuditRun:
        text = document.plain_text
        lower = text.casefold()
        first = document.blocks[0] if document.blocks else DocumentBlock("B-empty", "paragraph", "[空文档]", location=DocumentLocation("B-empty", "paragraph"))
        findings: list[Finding] = []
        observations: list[str] = []
        zero_basis: list[str] = []
        if critic == "expression_ambiguity":
            ambiguous = re.search(r"相关人员|原则上|视情况|适时|必要时|等有关单位|尽快|适当", text)
            if ambiguous:
                block = next((item for item in document.blocks if ambiguous.group(0) in item.text), first)
                findings.append(self._finding(critic, document, context, block, issue=f"表达“{ambiguous.group(0)}”可能产生竞争读法", standard="执行者应能唯一确定主语、对象、范围、条件与时间", consequence="不同执行者可能分别采取宽读或窄读，导致通知对象、期限或责任不一致", suggested_action="补充术语定义、适用对象、触发条件和明确期限", competing=[f"读法一：仅适用于当前段落明示的对象/情形", "读法二：扩展适用于同类但未明示的对象/情形"], observation="需要观察到适用名单、授权口径或业务实例，才能排除其中一个读法"))
            if len(document.blocks) > 1 and not any(block.kind == "heading" for block in document.blocks):
                findings.append(self._finding(critic, document, context, first, issue="文档缺少可定位的标题或目的表述", standard="收件人应能知道文件目的和需要采取的动作", consequence="接收者无法判断这是通知、征求意见还是执行指令", severity="low", suggested_action="增加标题、目的和对收件人的明确动作", competing=["读法一：信息告知，不要求采取行动", "读法二：形成需执行的工作要求"], observation="需要看到发布类型、收件人和截止日期"))
            if not findings:
                zero_basis.extend(["逐块扫描了模糊限定词与行动主体", "未发现足以形成两个竞争读法的确定性证据；仍不替代人工语境确认"])
        elif critic == "execution_feasibility":
            if not any(word in lower for word in ("负责人", "责任人", "牵头", "承办")):
                findings.append(self._finding(critic, document, context, first, issue="执行模型缺少负责人", standard="目标必须映射到交付物和明确负责人", consequence="出现延期、质量问题或跨部门依赖时没有责任承接点，无法升级或纠偏", severity="high", suggested_action="为每项交付物指定一名负责人，并写明授权边界和替补人", owner="项目负责人", blocks=True, uncertainties=["尚未确认是否存在附件或口头任命"], observation="需要看到责任矩阵或正式任命"))
            if not any(word in lower for word in ("预算", "费用", "金额", "经费")):
                findings.append(self._finding(critic, document, context, first, issue="执行模型缺少预算依据", standard="资源与预算应能支撑交付物和风险响应", consequence="采购、场地或人员成本在执行中暴露，导致范围缩水、临时垫付或项目中止", severity="high", suggested_action="补充成本项、数量、单价、预算上限和超支审批人", owner="方案负责人", blocks=True, uncertainties=["尚未确认是否存在单独预算表"], observation="需要看到预算表及审批记录"))
            if not any(word in lower for word in ("验收", "指标", "完成标准", "交付")):
                findings.append(self._finding(critic, document, context, first, issue="方案没有可验证的验收指标", standard="交付物应能通过事先约定的指标判断完成", consequence="项目可能在反向情形下仍自称成功，无法决定是否补救或关闭", suggested_action="为每个交付物增加可测量指标、证据格式和验收人"))
            if not findings:
                zero_basis.append("已检查目标、交付物、负责人、时间、资源、预算和验收关键词；未发现确定性缺口")
        elif critic == "compliance_legal_screen":
            triggers = []
            for word, label in (("收费", "付款/收费"), ("赞助", "赞助"), ("合同", "合同"), ("未成年人", "未成年人保护"), ("个人信息", "个人信息"), ("隐私", "隐私"), ("版权", "知识产权"), ("知识产权", "知识产权"), ("退款", "退款/票务"), ("处分", "治理/处分")):
                if word in lower and label not in triggers:
                    triggers.append(label)
            if triggers:
                findings.append(self._finding(critic, document, context, first, issue="文档触及需核实的合规风险领域：" + "、".join(triggers), standard="合规筛查必须绑定管辖范围、来源条款、有效性和适用事实", consequence="在缺少法源和事实确认时直接执行，可能遗漏授权、隐私、未成年人、付款或知识产权义务", severity="high", verification_state="cannot-confirm", suggested_action="补充适用地区、正式来源、条款定位和待核实事实；必要时交专业法律审查", owner="法务/审批人", blocks=False, uncertainties=["未提供可核验的法律、政策或内部制度材料"], basis=ExternalBasis(jurisdiction=context.jurisdiction, validity="unknown", application="当前仅根据文本触发词路由待核实问题", unresolved_facts=["适用主体资格", "发布或执行授权", "具体业务事实"]), observation="只有用户提供来源或联网检索得到当前有效条款后，才能改变 verification_state"))
            else:
                zero_basis.extend(["未检测到收费、合同、未成年人、个人信息、知识产权等路由触发词", "这不是合法性证明；没有来源材料时不能宣称合规"])
        elif critic == "reasonableness_governance":
            missing = []
            for word, label in (("申诉", "申诉渠道"), ("复议", "复议渠道"), ("回避", "利益冲突回避"), ("授权", "权力来源"), ("边界", "权力边界")):
                if word not in lower:
                    missing.append(label)
            if missing:
                findings.append(self._finding(critic, document, context, first, issue="治理文本未显示：" + "、".join(missing), standard="规范判断采用比例原则、程序正当、可申诉和利益冲突回避等明确原则", consequence="权力来源或纠错渠道不清时，弱势参与者可能承担无法复核的处分和风险", severity="medium", suggested_action="补充权力来源、边界、回避、申诉、复议和纠错机制，并说明适用原则", owner="治理审批人", uncertainties=["未确认是否存在独立治理制度"], observation="需要审阅上位章程、授权文件和申诉流程"))
            else:
                zero_basis.append("已检查权力来源、边界、回避和申诉词项；仍需人工判断具体条款是否成比例")
        elif critic == "official_professional_format":
            checks = []
            if not any(block.kind == "heading" for block in document.blocks):
                checks.append("标题")
            if not re.search(r"20\d{2}[年/-]\s*\d{1,2}[月/-]\s*\d{1,2}", text):
                checks.append("日期")
            if "附件" in lower and not any(word in lower for word in ("附件一", "附件：", "附：")):
                checks.append("附件定位")
            if checks:
                findings.append(self._finding(critic, document, context, first, issue="确定性格式项可能缺失：" + "、".join(checks), standard="标题、日期、编号、署名和附件应完整且可定位；确定性检查与语义判断分开", consequence="正式发布时收件人无法确认文件身份、时点或附件范围", severity="medium", suggested_action="补充缺失字段，并逐项核对正文、附件和表格中的日期、金额、名称一致性", owner="发文/文控负责人", blocks=context.publication_status == "external-formal"))
            if not findings:
                zero_basis.append("已检查标题、日期和附件指示词；表格合计与外部制度条款仍需人工或规则包核验")
        run_id = stable_id("RUN", document.source.sha256, critic, _now(), secrets.token_hex(4))
        return AuditRun(run_id, critic, document.document_id, document.source.sha256, context, findings, observations, zero_basis, "deterministic-local-rules", _now())

    def findings(self) -> list[Finding]:
        rows: dict[str, Finding] = {}
        audits = self.root / "audits"
        if not audits.is_dir():
            return []
        for path in sorted(audits.glob("*/*.json")):
            if path.name.endswith(".prompt.json") or path.is_symlink():
                continue
            try:
                value = _read_json(path)
                for item in value.get("findings", []):
                    finding = _finding_from_dict(item)
                    rows[finding.finding_id] = finding
            except (OSError, ValueError, KeyError, TypeError, ReviewStudioError):
                continue
        decisions = self._decisions()
        return [replace(item, status=decisions.get(item.finding_id, {}).get("decision", item.status)) for item in rows.values()]

    def _decision_records(self) -> dict[str, list[tuple[Path, dict[str, Any], str]]]:
        grouped: dict[str, list[tuple[Path, dict[str, Any], str]]] = {}
        directory = self.root / "finding-decisions"
        if not directory.is_dir():
            return grouped
        for path in directory.glob("*.json"):
            if path.is_symlink():
                continue
            value = _read_json(path)
            finding_id = str(value.get("finding_id", ""))
            grouped.setdefault(finding_id, []).append((path, value, _sha256(path.read_bytes())))
        return grouped

    def _decision_chain_errors(self) -> list[str]:
        errors: list[str] = []
        expected = {"artifact_type", "schema_version", "decision_id", "finding_id", "critic", "sequence", "previous_decision_sha256", "decision", "reason", "corrected_action", "finding_snapshot_sha256", "created_at", "lifecycle"}
        try:
            groups = self._decision_records()
        except (OSError, ValueError, ReviewStudioError) as exc:
            return [f"finding-decisions: 无法读取决定链：{exc}"]
        for finding_id, rows in groups.items():
            valid_shape = True
            for path, value, _ in rows:
                if set(value) != expected or value.get("artifact_type") != "finding-decision" or value.get("schema_version") != 2 or value.get("lifecycle") != "append-only":
                    errors.append(f"{path.name}: 决定记录字段或策略无效")
                    valid_shape = False
                if value.get("finding_id") != finding_id or value.get("decision") not in FINDING_DECISIONS or not isinstance(value.get("sequence"), int) or value.get("sequence", 0) < 1:
                    errors.append(f"{path.name}: 决定记录值无效")
                    valid_shape = False
            if not valid_shape:
                continue
            ordered = sorted(rows, key=lambda row: row[1]["sequence"])
            sequences = [row[1]["sequence"] for row in ordered]
            if sequences != list(range(1, len(ordered) + 1)):
                errors.append(f"{finding_id}: 决定 sequence 必须从 1 连续递增且不得分叉")
                continue
            for index, (path, value, _) in enumerate(ordered):
                expected_previous = None if index == 0 else ordered[index - 1][2]
                if value.get("previous_decision_sha256") != expected_previous:
                    errors.append(f"{path.name}: previous_decision_sha256 与父决定不匹配")
        return errors

    def _decisions(self) -> dict[str, dict[str, Any]]:
        errors = self._decision_chain_errors()
        if errors:
            raise ReviewStudioError("决定链完整性校验失败：" + "; ".join(errors))
        result: dict[str, dict[str, Any]] = {}
        for finding_id, rows in self._decision_records().items():
            result[finding_id] = max(rows, key=lambda row: row[1]["sequence"])[1]
        return result

    def _finding_artifact_path(self, finding_id: str) -> Path:
        candidates: list[Path] = []
        audits = self.root / "audits"
        for path in audits.glob("*/*.json") if audits.is_dir() else []:
            try:
                if any(item.get("finding_id") == finding_id for item in _read_json(path).get("findings", [])):
                    candidates.append(path)
            except (OSError, ValueError, ReviewStudioError, AttributeError):
                continue
        if not candidates:
            raise ReviewStudioError("找不到 Finding 的审查父产物")
        return sorted(candidates)[-1]

    def decide_finding(self, finding_id: str, decision: str, *, reason: str, corrected_action: str | None = None) -> dict[str, Any]:
        self._ensure_writable()
        if decision not in FINDING_DECISIONS:
            raise ReviewStudioError("Finding 决定必须是 accept、reject、defer 或 correct")
        finding = next((item for item in self.findings() if item.finding_id == finding_id), None)
        if finding is None:
            raise ReviewStudioError("找不到 Finding")
        if not reason.strip():
            raise ReviewStudioError("人工裁决必须填写理由")
        if decision in {"accept", "correct"} and corrected_action is None and not finding.suggested_action.strip():
            raise ReviewStudioError("接受的 Finding 必须有具体修改动作")
        prior_rows = self._decision_records().get(finding_id, [])
        previous = max(prior_rows, key=lambda row: row[1]["sequence"]) if prior_rows else None
        sequence = previous[1]["sequence"] + 1 if previous else 1
        record = {"artifact_type": "finding-decision", "schema_version": 2, "decision_id": stable_id("FD", finding_id, sequence, decision, _now(), secrets.token_hex(4)), "finding_id": finding_id, "critic": finding.critic, "sequence": sequence, "previous_decision_sha256": previous[2] if previous else None, "decision": decision, "reason": reason, "corrected_action": corrected_action, "finding_snapshot_sha256": _sha256(canonical_json(finding.to_dict())), "created_at": _now(), "lifecycle": "append-only"}
        decision_path = self.root / "finding-decisions" / f"{record['decision_id']}.json"
        parents = [_parent_ref(self.root, self._finding_artifact_path(finding_id), role="audit-run")]
        if previous:
            parents.append(_parent_ref(self.root, previous[0], role="previous-decision"))
        _write_tracked(self.root, decision_path, canonical_json(record), parents=parents, provenance="human-confirmed-append-only")
        self._append_event("finding_decided", record)
        return record

    def prepare_revision_bridge(self) -> Path:
        """Create a report consumable by the existing constrained revision loop.

        The bridge is a new immutable artifact; it does not rewrite the source
        or bypass Finding → Action → Hunk → Resolution approval.
        """
        self._ensure_writable()
        document = self.document()
        if not document:
            raise ReviewStudioError("没有结构化文档")
        findings = self.findings()
        decisions = self._decisions()
        accepted = [item for item in findings if item.status in {"accept", "correct"}]
        if not accepted:
            raise ReviewStudioError("没有已接受的 Finding，不能准备修改桥接")
        decision_paths: list[Path] = []
        for finding in accepted:
            decision_id = decisions[finding.finding_id]["decision_id"]
            decision_paths.append(self.root / "finding-decisions" / f"{decision_id}.json")
        bridge_id = stable_id("BRG", *[_sha256(path.read_bytes()) for path in decision_paths])
        bridge = self.root / "exports" / "revision-bridge" / bridge_id
        bridge.mkdir(parents=True, exist_ok=True)
        lines = ["# Document Review Studio Findings", "", f"Source SHA-256: `{document.source.sha256}`", "", "This report is a bridge into the existing constrained revision workflow. Independent critics remain separate.", ""]
        for finding in accepted:
            decision = decisions[finding.finding_id]
            approved_action = (decision.get("corrected_action") or finding.suggested_action).strip()
            lines.extend([f"## {finding.finding_id} · {finding.critic}", "", f"- Location: `{finding.location.block_id}` page={finding.location.page}", f"- Evidence: {finding.evidence}", f"- Issue: {finding.issue}", f"- Standard: {finding.standard}", f"- Consequence: {finding.consequence}", f"- Original suggested action: {finding.suggested_action}", f"- Human-approved action: {approved_action}", f"- Human decision: {finding.status} (sequence {decision['sequence']})", ""])
        report = "\n".join(lines).encode("utf-8")
        report_path = bridge / "findings-report.md"
        bridge_parents = [_parent_ref(self.root, self.document_path, role="structured-document"), *[_parent_ref(self.root, path, role="current-finding-decision") for path in decision_paths]]
        _write_tracked(self.root, report_path, report, parents=bridge_parents, provenance="deterministic-revision-bridge")
        binding = {"artifact_type": "revision-bridge", "schema_version": 2, "bridge_id": bridge_id, "source_sha256": document.source.sha256, "source_name": document.source.original_name, "report_relative_path": str(report_path.relative_to(self.root)).replace("\\", "/"), "report_sha256": _sha256(report), "finding_ids": [item.finding_id for item in accepted], "decision_bindings": [{"finding_id": item.finding_id, "decision_id": decisions[item.finding_id]["decision_id"], "decision_sha256": _sha256((self.root / "finding-decisions" / f"{decisions[item.finding_id]['decision_id']}.json").read_bytes()), "approved_action": (decisions[item.finding_id].get("corrected_action") or item.suggested_action).strip()} for item in accepted], "revision_loop": "existing-argument-workbench-constrained-revision", "track_changes_claimed": False, "revised_document_ready": False, "lifecycle": "immutable"}
        binding_path = bridge / "bridge.json"
        _write_tracked(self.root, binding_path, canonical_json(binding), parents=[*bridge_parents, _parent_ref(self.root, report_path, role="bridge-report")], provenance="deterministic-revision-bridge-binding")
        self._append_event("revision_bridge_prepared", binding)
        return report_path

    def export(self, *, revised_markdown: str | None = None) -> Path:
        self._ensure_writable()
        document = self.document()
        if not document:
            raise ReviewStudioError("没有可导出的结构化文档")
        normalized = model_to_markdown(document)
        if revised_markdown is not None and revised_markdown != normalized:
            raise ReviewStudioError("尚未完成 Finding→Action→Hunk→Resolution 审批链，不能生成或命名 revised 文档")
        export_id = stable_id("EXP", document.source.sha256, _now(), secrets.token_hex(4))
        output = self.root / "exports" / export_id
        output.mkdir(parents=True, exist_ok=False)
        draft = normalized
        base_parents = [_parent_ref(self.root, self.document_path, role="structured-document")]
        draft_path = output / "draft.md"
        _write_tracked(self.root, draft_path, draft.encode("utf-8"), parents=base_parents, provenance="normalized-editable-copy")
        findings = self.findings()
        runs: list[dict[str, Any]] = []
        for path in sorted((self.root / "audits").glob("*/*.json")) if (self.root / "audits").is_dir() else []:
            try:
                runs.append(_read_json(path))
            except (OSError, ValueError, ReviewStudioError):
                continue
        decisions = self._decisions()
        chain_parents = list(base_parents)
        for path in sorted((self.root / "audits").glob("*/*.json")) if (self.root / "audits").is_dir() else []:
            chain_parents.append(_parent_ref(self.root, path, role="audit-run"))
        for value in decisions.values():
            chain_parents.append(_parent_ref(self.root, self.root / "finding-decisions" / f"{value['decision_id']}.json", role="current-finding-decision"))
        bridge_root = self.root / "exports" / "revision-bridge"
        for bridge_path in sorted(bridge_root.glob("*/bridge.json")) if bridge_root.is_dir() else []:
            chain_parents.append(_parent_ref(self.root, bridge_path, role="revision-bridge"))
        audit = {"artifact_type": "document-review-export", "schema_version": 2, "product_status": "experimental-preview", "export_id": export_id, "source": document.source.to_dict(), "parser": {"name": document.parser_name, "version": document.parser_version}, "quality": document.quality.to_dict(), "warnings": [warning.to_dict() for warning in document.warnings], "audit_runs": runs, "findings": [finding.to_dict() for finding in findings], "decisions": list(decisions.values()), "independent_critics": list(CRITIC_DIMENSIONS), "scores": None, "legal_boundary": "合规筛查不是律师意见；无来源材料时只能输出待核实问题", "created_at": _now()}
        audit_path = output / "audit.json"
        _write_tracked(self.root, audit_path, canonical_json(audit), parents=chain_parents, provenance="deterministic-audit-export")
        quality_path = output / "quality-report.json"
        _write_tracked(self.root, quality_path, canonical_json({"source": document.source.to_dict(), "quality": document.quality.to_dict(), "warnings": [warning.to_dict() for warning in document.warnings]}), parents=base_parents, provenance="deterministic-quality-export")
        audit_markdown_path = output / "audit.md"
        _write_tracked(self.root, audit_markdown_path, _audit_markdown(audit).encode("utf-8"), parents=[_parent_ref(self.root, audit_path, role="audit-json")], provenance="deterministic-audit-render")
        if document.source.extension == ".docx":
            docx_bytes = _minimal_docx(draft)
            copy_path = output / "normalized-editable-copy.docx"
            _write_tracked(self.root, copy_path, docx_bytes, parents=[_parent_ref(self.root, draft_path, role="normalized-markdown")], provenance="normalized-editable-copy")
            difference_path = output / "difference-report.md"
            _write_tracked(self.root, difference_path, _difference_report(normalized, draft).encode("utf-8"), parents=[_parent_ref(self.root, draft_path, role="normalized-markdown")])
            capability_path = output / "track-changes-capability.json"
            _write_tracked(self.root, capability_path, canonical_json({"native_track_changes": False, "revised_document_ready": False, "output_name": "normalized-editable-copy.docx", "message": "当前仅输出规范化可编辑副本；未完成受约束修改闭环，不生成 revised.docx，也不冒充 Word Track Changes"}), parents=[_parent_ref(self.root, copy_path, role="normalized-docx")])
        else:
            editable_path = output / "editable-draft.md"
            _write_tracked(self.root, editable_path, draft.encode("utf-8"), parents=[_parent_ref(self.root, draft_path, role="normalized-markdown")], provenance="normalized-editable-copy")
        self._append_event("export_created", {"export_id": export_id, "relative_path": str(output.relative_to(self.root)).replace("\\", "/")})
        return output

    def ai_requests(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        directory = self.root / "ai-requests"
        for request_path in sorted(directory.glob("*/request.json")) if directory.is_dir() else []:
            try:
                value = _read_json(request_path)
                prompt_path = request_path.parent / "prompt.md"
                value["prompt"] = prompt_path.read_text(encoding="utf-8")
                value["relative_path"] = str(prompt_path.relative_to(self.root)).replace("\\", "/")
                rows.append(value)
            except (OSError, ValueError, ReviewStudioError):
                continue
        return rows

    def view(self) -> dict[str, Any]:
        self._enforce_integrity()
        manifest = self.manifest()
        state = self.state()
        try:
            document = self.document()
        except (OSError, KeyError, TypeError, ValueError, ReviewStudioError):
            document = None
        can_review, reasons = self.can_review()
        try:
            finding_rows = [finding.to_dict() for finding in self.findings()]
        except (OSError, KeyError, TypeError, ValueError, ReviewStudioError):
            finding_rows = []
        return {"project": manifest, "product_status": "experimental-preview", "state": state, "extraction": {"available": document is not None, "quality": document.quality.to_dict() if document else {}, "warnings": [warning.to_dict() for warning in document.warnings] if document else [], "blocks": [block.to_dict() for block in document.blocks[:100]] if document else []}, "context": self.context().to_dict() if self.context() else {"model_suggestion": self.suggested_document_type()}, "can_review": can_review, "review_blockers": reasons, "ai_requests": self.ai_requests(), "findings": finding_rows}


def _document_from_dict(value: Mapping[str, Any]) -> StructuredDocument:
    source_value = value["source"]
    source = type("RawFileBindingProxy", (), {})
    from document_review_model import RawFileBinding, ExtractionWarning
    binding = RawFileBinding(**source_value)
    blocks: list[DocumentBlock] = []
    for item in value.get("blocks", []):
        location_value = item.get("location")
        location = DocumentLocation(**location_value) if location_value else None
        blocks.append(DocumentBlock(item["block_id"], item["kind"], item.get("text", ""), item.get("level"), location, dict(item.get("attrs", {})), list(item.get("children", []))))
    warnings = [ExtractionWarning(item["code"], item["severity"], item["message"], DocumentLocation(**item["location"]) if item.get("location") else None, dict(item.get("details", {}))) for item in value.get("warnings", [])]
    quality = QualitySignals(**{key: value.get("quality", {}).get(key, default) for key, default in QualitySignals().__dict__.items()})
    parser = value.get("parser", {})
    return StructuredDocument(value["document_id"], value.get("title", ""), binding, parser.get("name", "unknown"), parser.get("version", "unknown"), blocks, warnings, quality, list(value.get("source_to_block", [])), dict(value.get("metadata", {})))


def _finding_from_dict(value: Mapping[str, Any]) -> Finding:
    errors = validate_finding_dict(value)
    if errors:
        raise ReviewStudioError("Finding contract invalid: " + "; ".join(errors))
    location = DocumentLocation(**{key: value["location"].get(key) for key in DocumentLocation.__dataclass_fields__})
    basis = ExternalBasis(**{key: value["external_basis"].get(key, default) for key, default in ExternalBasis().__dict__.items()})
    return Finding(value["finding_id"], value["critic"], value["document_type"], location, value["evidence"], value["issue"], value["standard"], value["consequence"], value["severity"], value["verification_state"], basis, list(value["uncertainties"]), value["suggested_action"], value["suggested_owner"], value["blocks_release_or_execution"], value.get("status", "open"), value.get("origin", "model-derived"), list(value.get("competing_readings", [])), value.get("required_observation", ""), value.get("proposed_group_id"))


def _audit_markdown(audit: Mapping[str, Any]) -> str:
    lines = ["# Document Review Studio audit report", "", f"Source: `{audit['source']['original_name']}`", f"SHA-256: `{audit['source']['sha256']}`", "", "## Recognition quality", "", f"- Text coverage: {audit['quality'].get('text_coverage', 0):.2f}", f"- Blank pages: {audit['quality'].get('blank_pages', [])}", f"- OCR low-confidence blocks: {audit['quality'].get('ocr_low_confidence_blocks', 0)}", f"- Reading order suspected: {audit['quality'].get('suspected_reading_order', False)}", "", "## Independent findings", ""]
    if not audit["findings"]:
        lines.append("No Finding was produced. This is supported only by the recorded recognition scope and deterministic checks; it is not a guarantee of quality or legality.")
    for finding in audit["findings"]:
        lines.extend([f"### {finding['finding_id']} · {finding['critic']}", "", f"- Location: `{finding['location']['block_id']}` page {finding['location'].get('page') or '-'}", f"- Evidence: {finding['evidence']}", f"- Issue: {finding['issue']}", f"- Standard: {finding['standard']}", f"- Consequence: {finding['consequence']}", f"- Severity: {finding['severity']}; verification: {finding['verification_state']}", f"- Action: {finding['suggested_action']}", ""])
        if finding.get("external_basis", {}).get("unresolved_facts"):
            lines.append("- Unresolved facts: " + "；".join(finding["external_basis"]["unresolved_facts"]))
    lines.extend(["", "## Boundary", "", audit["legal_boundary"], ""])
    return "\n".join(lines)


def _difference_report(before: str, after: str) -> str:
    diff = "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile="internal-document", tofile="revised-draft"))
    return "# V1/V2 difference report\n\n```diff\n" + diff + "```\n\nNative Word Track Changes are not claimed. Review the audit JSON and the existing constrained revision chain before approval.\n"


def _minimal_docx(markdown: str) -> bytes:
    paragraphs: list[str] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line or line.startswith("|---"):
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        elif re.match(r"^(?:[-*]|\d+\.)\s+", line):
            line = re.sub(r"^(?:[-*]|\d+\.)\s+", "", line)
        paragraphs.append(line)
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{html.escape(line)}</w:t></w:r></w:p>' for line in paragraphs)
    document_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body></w:document>'''.encode("utf-8")
    content_types = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'''
    rels = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'''
    output = __import__("io").BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document_xml)
    return output.getvalue()


__all__ = ["DocumentReviewProject", "ReviewStudioError", "STORE_SUFFIX"]
