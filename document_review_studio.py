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
import io
import json
import os
import re
import secrets
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
from functools import wraps
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
    VERIFICATION_STATES,
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


_PROJECT_LOCKS: dict[str, threading.RLock] = {}
_PROJECT_LOCKS_GUARD = threading.Lock()


@contextmanager
def _project_mutation_lock(root: Path):
    """Serialize one complete project mutation in this and other processes."""
    key = str(root.resolve())
    with _PROJECT_LOCKS_GUARD:
        thread_lock = _PROJECT_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        lock_path = root / ".mutation.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _serialized_mutation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with _project_mutation_lock(self.root):
            return method(self, *args, **kwargs)

    return wrapped


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


_NEGATED_TERM_RE = re.compile(r"(?:没有|无|未(?:有|提供|设置|明确|见)?|不涉及|不含|无需|不用|不存在|尚无|尚未|缺少|缺乏)[^。！？；\n,，]{0,32}")


def _contains_positive_term(text: str, terms: Iterable[str]) -> bool:
    """Detect a term as affirmative evidence, not merely in a negation.

    This is intentionally a small routing heuristic, not a natural-language
    entailment engine. It prevents obvious phrases such as “没有预算” and
    “不涉及收费” from being treated as positive coverage while leaving the
    resulting item for human review.
    """
    normalized = text.casefold()
    blocked_ranges = [(match.start(), match.end()) for match in _NEGATED_TERM_RE.finditer(normalized)]
    for term in terms:
        start = 0
        needle = term.casefold()
        while True:
            index = normalized.find(needle, start)
            if index < 0:
                break
            if not any(begin <= index < end for begin, end in blocked_ranges):
                return True
            start = index + len(needle)
    return False


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
        elif (self.root / "extraction" / "diagnostic.json").is_file() and not self.document_path.is_file():
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
        except (OSError, ValueError, KeyError, TypeError, ReviewStudioError) as exc:
            # A broken verifier is itself an integrity failure.  Failing open
            # here would turn parser/index errors into write authorization.
            errors = [f"完整性检查器异常：{exc}"]
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
            for dirname in (
                "source", "extraction", EXTRACTION_DECISION_DIR_NAME, "ai-requests",
                "audits", "finding-decisions", "revision-plans", "action-operation-decisions", "revision-hunks",
                "hunk-decisions", "revisions", "exports",
            ):
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
            errors.extend(self._ai_request_chain_errors())
            errors.extend(self._audit_run_chain_errors())
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

    def _review_round_records(self) -> list[tuple[Path, dict[str, Any], str]]:
        rows: list[tuple[Path, dict[str, Any], str]] = []
        root = self.root / "revisions"
        for path in root.glob("*/followup-round.json") if root.is_dir() else []:
            if path.is_symlink():
                continue
            value = _read_json(path)
            if value.get("artifact_type") == "document-review-followup-round":
                rows.append((path, value, _sha256(path.read_bytes())))
        return sorted(rows, key=lambda row: (int(row[1].get("sequence", 0)), str(row[1].get("created_at", ""))))

    def current_review_round(self) -> tuple[Path, dict[str, Any], str] | None:
        rows = self._review_round_records()
        return rows[-1] if rows else None

    def _review_document_record(self) -> tuple[Path, StructuredDocument]:
        current_round = self.current_review_round()
        if current_round:
            document_path = _safe_child(self.root, str(current_round[1].get("base_document_relative_path", "")))
            document = _document_from_dict(_read_json(document_path))
            if document.source.sha256 != current_round[1].get("base_revised_sha256"):
                raise ReviewStudioError("下一轮审查文档与 follow-up round 绑定不一致")
            return document_path, document
        document = self.document()
        if not document:
            raise ReviewStudioError("没有结构化文档")
        return self.document_path, document

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

    @_serialized_mutation
    def retry_extraction(self) -> dict[str, Any]:
        """Re-run ingestion after optional PDF/OCR dependencies are repaired."""
        self._ensure_writable()
        if self._latest_extraction_decision() is not None:
            raise ReviewStudioError("已经存在识别确认决定；重新识别前请新建项目或显式更换文件")
        manifest = self.manifest()
        source_path = _safe_child(self.root, str(manifest["source"]["relative_path"]))
        document = ingest_bytes(str(manifest["source"]["name"]), source_path.read_bytes())
        if self.document_path.is_file():
            retry_id = stable_id("REX", document.source.sha256, _now(), secrets.token_hex(4))
            retry_path = self.root / "extraction" / "retries" / retry_id / "document.json"
            retry_bytes = canonical_json(document.to_dict())
            _write_tracked(self.root, retry_path, retry_bytes, parents=[_parent_ref(self.root, source_path, role="original-source")], provenance="parser-retry")
            _replace_tracked(self.root, self.document_path, retry_bytes, parents=[_parent_ref(self.root, retry_path, role="successful-retry")], provenance="parser-derived-current")
            document_parent = _parent_ref(self.root, self.document_path, role="structured-document")
            for path, data in (
                (self.root / "extraction" / "quality.json", canonical_json(document.quality.to_dict())),
                (self.root / "extraction" / "warnings.json", canonical_json({"warnings": [warning.to_dict() for warning in document.warnings]})),
                (self.root / "extraction" / "source-map.json", canonical_json({"source_to_block": document.source_to_block})),
            ):
                _replace_tracked(self.root, path, data, parents=[document_parent], provenance="parser-retry-current")
        else:
            self._save_document(document)
        hard_blocks = [warning.message for warning in document.warnings if warning.code in {"ocr-unavailable", "pdf-renderer-unavailable"}]
        self._append_event("extraction_retried", {"source_sha256": document.source.sha256, "hard_block_count": len(hard_blocks)})
        return self._update_state(
            extraction_state="blocked" if hard_blocks else "unconfirmed",
            diagnostics=hard_blocks,
            read_only=False,
            integrity_errors=[],
        )

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

    @_serialized_mutation
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

    @_serialized_mutation
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
        text_fields = ("document_type", "jurisdiction", "effective_date", "publisher_type", "audience")
        for name in text_fields:
            value = payload.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ReviewStudioError(f"{name} 不能为空；未知时请明确填写 unknown 并说明")
            if len(value.strip()) > 500:
                raise ReviewStudioError(f"{name} 超过 500 字符安全上限")
        effective_date = str(payload["effective_date"]).strip()
        if effective_date.casefold() != "unknown":
            try:
                date.fromisoformat(effective_date)
            except ValueError as exc:
                raise ReviewStudioError("effective_date 必须是 YYYY-MM-DD 或 unknown") from exc
        materials = payload.get("user_provided_materials", [])
        if not isinstance(materials, list) or not all(isinstance(item, str) and item.strip() for item in materials):
            raise ReviewStudioError("user_provided_materials 必须是非空文本数组")
        context = ReviewContext(**{name: payload[name] for name in required}, confirmed=True, model_suggestion=self.suggested_document_type(), user_provided_materials=list(payload.get("user_provided_materials", [])))
        context_path = self.root / "context.json"
        existing = self.context()
        if existing is not None:
            existing_decision = {key: value for key, value in existing.to_dict().items() if key != "model_suggestion"}
            replayed_decision = {key: value for key, value in context.to_dict().items() if key != "model_suggestion"}
            if canonical_json(existing_decision) == canonical_json(replayed_decision):
                # The browser may lose the response after the immutable artifact
                # has already been committed.  Treat an exact replay as a read of
                # the successful decision instead of attempting to overwrite it.
                return self._update_state(context_state="confirmed")
            raise ReviewStudioError("审查上下文已经确认，不能覆盖已有审计产物；当前版本暂不支持直接修改已确认上下文，请新建项目后重新确认")
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
            "field_contract": {
                "severity": ["info", "low", "medium", "high", "critical"],
                "verification_state": sorted(VERIFICATION_STATES),
                "external_basis": {
                    "type": "object",
                    "fields": {
                        "jurisdiction": "string",
                        "source_name": "string",
                        "issuing_body": "string",
                        "validity": "string",
                        "locator": "string",
                        "url_or_attachment": "string",
                        "application": "string",
                        "unresolved_facts": "array of strings",
                    },
                    "empty_value": ExternalBasis().to_dict(),
                },
            },
            "rules": {"independent": True, "do_not_vote_or_score": True, "location_must_use_block_or_page": True, "legal_screen_never_claims_counsel": True},
        }
        return "# Document Review Studio independent AI review\n\nYou are exactly one independent critic. Return strict JSON only. Do not run another critic, merge dimensions, vote, score, or infer external facts without a source.\n\n## Contract and critic-specific protocol\n```json\n" + json.dumps(contract, ensure_ascii=False, indent=2) + "\n```\n\n## Confirmed review context\n```json\n" + json.dumps(context.to_dict(), ensure_ascii=False, indent=2) + "\n```\n\n## Internal document blocks\n```json\n" + json.dumps([block.to_dict() for block in document.blocks], ensure_ascii=False, indent=2) + "\n```\n"

    def _audit_run_records(self, critic: str | None = None) -> list[tuple[Path, dict[str, Any], str]]:
        root = self.root / "audits"
        records: list[tuple[Path, dict[str, Any], str]] = []
        pattern = f"{critic}/*.json" if critic else "*/*.json"
        for path in root.glob(pattern) if root.is_dir() else []:
            if path.is_symlink():
                continue
            value = _read_json(path)
            if value.get("run_id") and value.get("critic") in CRITIC_DIMENSIONS:
                records.append((path, value, _sha256(path.read_bytes())))
        return records

    def _ordered_audit_runs(self, critic: str) -> list[tuple[Path, dict[str, Any], str]]:
        records = self._audit_run_records(critic)
        return sorted(
            records,
            key=lambda row: (
                int(row[1].get("run_sequence", 0))
                if isinstance(row[1].get("run_sequence"), int)
                else 0,
                str(row[1].get("created_at", "")),
                str(row[1].get("run_id", "")),
            ),
        )

    def _next_audit_binding(self, critic: str) -> tuple[int, str | None]:
        records = self._ordered_audit_runs(critic)
        if not records:
            return 1, None
        latest = records[-1]
        latest_sequence = latest[1].get("run_sequence")
        sequence = int(latest_sequence) + 1 if isinstance(latest_sequence, int) else len(records) + 1
        return sequence, latest[2]

    def _active_audit_run_records(self) -> dict[str, tuple[Path, dict[str, Any], str]]:
        active: dict[str, tuple[Path, dict[str, Any], str]] = {}
        for critic in CRITIC_DIMENSIONS:
            records = self._ordered_audit_runs(critic)
            if records:
                active[critic] = records[-1]
        return active

    def _critic_origin_binding(self, critic: str) -> dict[str, Any]:
        current_round = self.current_review_round()
        if current_round:
            inherited = current_round[1].get("critic_bindings", {}).get(critic)
            if isinstance(inherited, dict):
                for field in ("original_prompt_relative_path", "original_request_relative_path", "original_audit_run_relative_path"):
                    path = _safe_child(self.root, str(inherited.get(field, "")))
                    if not path.is_file() or path.is_symlink():
                        raise ReviewStudioError(f"下一轮 critic 原始绑定缺失：{field}")
                return dict(inherited)
        active = self._active_audit_run_records().get(critic)
        if not active or active[1].get("model_label") == "deterministic-local-rules":
            raise ReviewStudioError(f"找不到外部 critic 的原始 AuditRun：{critic}")
        run_path, run, run_sha256 = active
        metadata = run.get("declared_model_metadata", {})
        request_id = metadata.get("request_id")
        request_path = self.root / "ai-requests" / str(request_id) / "request.json"
        prompt_path = request_path.parent / "prompt.md"
        request = _read_json(request_path)
        prompt_bytes = prompt_path.read_bytes()
        if request.get("critic") != critic or request.get("prompt_file_sha256") != _sha256(prompt_bytes):
            raise ReviewStudioError(f"原 critic request/prompt 绑定无效：{critic}")
        return {
            "critic": critic,
            "critic_protocol_sha256": _sha256(canonical_json(CRITIC_PROTOCOLS[critic])),
            "original_request_id": request_id,
            "original_request_sha256": _sha256(request_path.read_bytes()),
            "original_request_relative_path": str(request_path.relative_to(self.root)).replace("\\", "/"),
            "original_prompt_sha256": request.get("prompt_sha256"),
            "original_prompt_file_sha256": request.get("prompt_file_sha256"),
            "original_prompt_relative_path": str(prompt_path.relative_to(self.root)).replace("\\", "/"),
            "original_audit_run_id": run.get("run_id"),
            "original_audit_run_sha256": run_sha256,
            "original_audit_run_relative_path": str(run_path.relative_to(self.root)).replace("\\", "/"),
            "original_provider": metadata.get("provider"),
            "original_model": metadata.get("model"),
            "original_response_binding": run.get("response_binding"),
        }

    def _audit_run_chain_errors(self) -> list[str]:
        errors: list[str] = []
        for critic in CRITIC_DIMENSIONS:
            records = self._ordered_audit_runs(critic)
            if not records:
                continue
            legacy = [row for row in records if "run_sequence" not in row[1]]
            modern = [row for row in records if "run_sequence" in row[1]]
            if not modern:
                continue
            sequences = [row[1].get("run_sequence") for row in modern]
            if sequences != list(range(len(legacy) + 1, len(records) + 1)):
                errors.append(f"{critic}: audit run sequence must be continuous")
                continue
            previous = legacy[-1][2] if legacy else None
            for index, (_, value, digest) in enumerate(modern, start=len(legacy) + 1):
                expected = previous
                if value.get("previous_audit_run_sha256") != expected:
                    errors.append(f"{critic}: audit run parent mismatch at sequence {index}")
                previous = digest
        return errors

    @_serialized_mutation
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
            run.run_sequence, run.previous_audit_run_sha256 = self._next_audit_binding(critic)
            directory = self.root / "audits" / critic
            prompt_path = directory / f"{run.run_id}.local-precheck-protocol.md"
            parents = [_parent_ref(self.root, self.document_path, role="structured-document"), _parent_ref(self.root, self.root / "context.json", role="review-context")]
            _write_tracked(self.root, prompt_path, self.prompt(critic).encode("utf-8"), parents=parents, provenance="deterministic-local-precheck-protocol")
            run_path = directory / f"{run.run_id}.json"
            run_parents = [*parents, _parent_ref(self.root, prompt_path, role="local-precheck-protocol")]
            previous = self._ordered_audit_runs(critic)
            if previous:
                run_parents.append(_parent_ref(self.root, previous[-1][0], role="previous-audit-run"))
            _write_tracked(self.root, run_path, canonical_json(run.to_dict()), parents=run_parents, provenance="deterministic-local-precheck")
            self._append_event("local_precheck_created", {"run_id": run.run_id, "critic": critic, "finding_ids": [f.finding_id for f in run.findings]})
            runs.append(run)
        self._update_state(review_state="local_precheck_completed", last_audit_at=_now())
        return runs

    def run_audits(self, critics: Iterable[str] | None = None) -> list[AuditRun]:
        """Compatibility alias; these are deterministic local prechecks, not AI reviews."""
        return self.run_local_prechecks(critics)

    def _ai_request_records(self, critic: str | None = None) -> list[tuple[Path, dict[str, Any], str]]:
        directory = self.root / "ai-requests"
        records: list[tuple[Path, dict[str, Any], str]] = []
        for path in directory.glob("*/request.json") if directory.is_dir() else []:
            value = _read_json(path)
            if critic is None or value.get("critic") == critic:
                records.append((path, value, _sha256(path.read_bytes())))
        return sorted(
            records,
            key=lambda row: (
                int(row[1].get("request_sequence", 0))
                if isinstance(row[1].get("request_sequence"), int)
                else 0,
                str(row[1].get("created_at", "")),
                str(row[1].get("request_id", "")),
            ),
        )

    def _active_ai_request(self, critic: str) -> tuple[Path, dict[str, Any], str] | None:
        records = self._ai_request_records(critic)
        return records[-1] if records else None

    def _ai_request_chain_errors(self) -> list[str]:
        errors: list[str] = []
        for critic in CRITIC_DIMENSIONS:
            try:
                records = self._ai_request_records(critic)
            except (OSError, ValueError, KeyError, TypeError, ReviewStudioError) as exc:
                errors.append(f"{critic}: AI request chain unreadable: {exc}")
                continue
            if not records:
                continue
            legacy = [row for row in records if "request_sequence" not in row[1]]
            modern = [row for row in records if "request_sequence" in row[1]]
            if not modern:
                continue
            sequences = [row[1].get("request_sequence") for row in modern]
            if sequences != list(range(len(legacy) + 1, len(records) + 1)):
                errors.append(f"{critic}: AI request sequence must be continuous")
                continue
            previous = legacy[-1][2] if legacy else None
            for index, (_, value, digest) in enumerate(modern, start=len(legacy) + 1):
                expected = previous
                if value.get("previous_request_sha256") != expected:
                    errors.append(f"{critic}: AI request parent mismatch at sequence {index}")
                previous = digest
        return errors

    @_serialized_mutation
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
            history = self._ai_request_records(critic)
            previous = history[-1] if history else None
            request_sequence = int(previous[1]["request_sequence"]) + 1 if previous and isinstance(previous[1].get("request_sequence"), int) else len(history) + 1
            request_id = stable_id("AIR", critic, prompt_sha256, normalized_provider, normalized_model, _now(), secrets.token_hex(4))
            envelope = {"request_id": request_id, "prompt_sha256": prompt_sha256, "provider": normalized_provider, "model": normalized_model}
            response_example = {
                **envelope,
                "critic": critic,
                "source_sha256": self.document().source.sha256,
                "findings": [{
                    "finding_id": "F1",
                    "critic": critic,
                    "document_type": self.context().document_type,
                    "location": {"block_id": "COPY_AN_EXISTING_BLOCK_ID"},
                    "evidence": "exact quotation from that block",
                    "issue": "atomic problem",
                    "standard": "criterion applied",
                    "consequence": "practical consequence",
                    "severity": "medium",
                    "verification_state": "model-proposed",
                    "external_basis": ExternalBasis().to_dict(),
                    "uncertainties": [],
                    "suggested_action": "specific revision action",
                    "suggested_owner": "document owner",
                    "blocks_release_or_execution": False,
                }],
                "observations": [],
                "zero_finding_basis": [],
            }
            prompt = base_prompt + ("\n## Exact response shape\nReturn one JSON object only, without Markdown fences or commentary. Copy every bookkeeping value exactly. Use only the enum values and object shapes shown below. If there are no Findings, return an empty findings array and explain the inspected scope in zero_finding_basis.\n```json\n" + json.dumps(response_example, ensure_ascii=False, indent=2) + "\n```\n").encode("utf-8")
            directory = self.root / "ai-requests" / request_id
            prompt_path = directory / "prompt.md"
            _write_tracked(self.root, prompt_path, prompt, parents=parents, provenance="deterministic-ai-protocol")
            request = {"artifact_type": "independent-ai-review-request", "schema_version": 2, "request_id": request_id, "critic": critic, "provider": normalized_provider, "model": normalized_model, "prompt_sha256": prompt_sha256, "prompt_file_sha256": _sha256(prompt), "source_sha256": self.document().source.sha256, "request_sequence": request_sequence, "previous_request_sha256": previous[2] if previous else None, "created_at": _now(), "lifecycle": "immutable"}
            request_path = directory / "request.json"
            request_parents = [*parents, _parent_ref(self.root, prompt_path, role="critic-prompt")]
            if previous:
                request_parents.append(_parent_ref(self.root, previous[0], role="previous-ai-request"))
            _write_tracked(self.root, request_path, canonical_json(request), parents=request_parents, provenance="deterministic-ai-request")
            rows.append({**request, "prompt": prompt.decode("utf-8"), "relative_path": str(prompt_path.relative_to(self.root)).replace("\\", "/")})
        self._update_state(ai_review_state="protocols_ready", last_ai_protocol_at=_now())
        return rows

    @_serialized_mutation
    def collect_model_audit(self, critic: str, response: bytes | str, *, provider: str = "external", model: str = "unlabelled", request_id: str | None = None, model_label: str | None = None, binding_mode: str = "strict") -> AuditRun:
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
        if binding_mode not in {"strict", "manual_association"}:
            raise ReviewStudioError("AI 响应绑定模式必须是 strict 或 manual_association")
        if not provider.strip() or not model.strip():
            raise ReviewStudioError("模型审查导入必须记录 provider 和 model")
        requests: list[tuple[Path, dict[str, Any]]] = []
        for path in (self.root / "ai-requests").glob("*/request.json") if (self.root / "ai-requests").is_dir() else []:
            value = _read_json(path)
            request_matches = value.get("request_id") == request_id if request_id is not None else (value.get("provider") == provider and value.get("model") == model)
            if value.get("critic") == critic and request_matches:
                requests.append((path, value))
        if not requests:
            raise ReviewStudioError("找不到对应的已导出 AI 请求；请先导出当前 critic 协议")
        else:
            request_path, request = sorted(requests, key=lambda row: row[1]["created_at"])[-1]
        active_request = self._active_ai_request(critic)
        if active_request is None or active_request[1].get("request_id") != request.get("request_id"):
            raise ReviewStudioError("该 AI 请求已被更新协议取代；请使用当前 request")
        for _, prior_run, _ in self._audit_run_records(critic):
            if prior_run.get("declared_model_metadata", {}).get("request_id") == request.get("request_id"):
                raise ReviewStudioError("该 AI 请求已经导入过结果；需要重跑时请先生成新 request")
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
        present_envelope_fields = {field for field in expected_envelope if field in parsed}
        if binding_mode == "strict" or present_envelope_fields:
            if present_envelope_fields != set(expected_envelope):
                missing = ", ".join(sorted(set(expected_envelope) - present_envelope_fields))
                raise ReviewStudioError(f"模型响应只回显了部分请求绑定字段；缺少：{missing}")
            for field, expected in expected_envelope.items():
                if parsed.get(field) != expected:
                    raise ReviewStudioError(f"模型返回的 {field} 未与已导出请求逐项匹配")
            response_binding = "strict-response-envelope"
        else:
            response_binding = "manual-association"
        if parsed.get("critic") != critic:
            raise ReviewStudioError("模型返回的 critic 与提交维度不一致")
        returned_source_sha256 = parsed.get("source_sha256")
        if returned_source_sha256 is not None and returned_source_sha256 != document.source.sha256:
            raise ReviewStudioError("模型返回的原始文件 SHA-256 与当前任务冲突，不能人工覆盖")
        if binding_mode == "strict" and returned_source_sha256 is None:
            raise ReviewStudioError("严格绑定要求模型回显当前原始文件 SHA-256；也可改用“当前任务人工关联”导入")
        source_echo_verified = returned_source_sha256 == document.source.sha256
        raw_findings = parsed.get("findings")
        if not isinstance(raw_findings, list):
            raise ReviewStudioError("模型返回缺少 findings 数组")
        block_ids = {block.block_id for block in document.blocks}
        findings: list[Finding] = []
        seen_source_ids: set[str] = set()
        response_normalizations: list[dict[str, Any]] = []
        verification_aliases = {
            "unverified": "needs-human-verification",
            "needs_verification": "needs-human-verification",
            "needs-human-review": "needs-human-verification",
            "待核实": "needs-human-verification",
            "unknown": "cannot-confirm",
            "无法确认": "cannot-confirm",
            "proposed": "model-proposed",
            "模型判断": "model-proposed",
        }
        for index, item in enumerate(raw_findings):
            if not isinstance(item, dict):
                raise ReviewStudioError(f"第 {index + 1} 条 Finding 不是对象")
            item = dict(item)
            source_finding_id = str(item.get("finding_id", f"finding-{index + 1}"))
            verification = item.get("verification_state")
            if verification not in VERIFICATION_STATES:
                normalized_verification = verification_aliases.get(str(verification).strip().casefold(), "needs-human-verification")
                response_normalizations.append({"finding_id": source_finding_id, "field": "verification_state", "original": verification, "normalized": normalized_verification, "reason": "unsupported model enum was conservatively downgraded"})
                item["verification_state"] = normalized_verification
            basis = item.get("external_basis")
            if not isinstance(basis, Mapping):
                response_normalizations.append({"finding_id": source_finding_id, "field": "external_basis", "original_type": type(basis).__name__, "normalized": "empty ExternalBasis", "reason": "non-object model value cannot establish an external source"})
                item["external_basis"] = ExternalBasis(unresolved_facts=["模型未提供结构化 external_basis；需人工核实外部依据"]).to_dict()
                if item["verification_state"] == "verified":
                    item["verification_state"] = "needs-human-verification"
                    response_normalizations.append({"finding_id": source_finding_id, "field": "verification_state", "original": "verified", "normalized": "needs-human-verification", "reason": "verified cannot survive without a structured external basis"})
            else:
                normalized_basis = dict(basis)
                unresolved = normalized_basis.get("unresolved_facts", [])
                if isinstance(unresolved, str):
                    normalized_basis["unresolved_facts"] = [unresolved]
                    response_normalizations.append({"finding_id": source_finding_id, "field": "external_basis.unresolved_facts", "original_type": "string", "normalized": "array", "reason": "single text value wrapped as one unresolved fact"})
                elif unresolved is None:
                    normalized_basis["unresolved_facts"] = []
                    response_normalizations.append({"finding_id": source_finding_id, "field": "external_basis.unresolved_facts", "original_type": "null", "normalized": "array", "reason": "null normalized to an empty list"})
                item["external_basis"] = normalized_basis
            errors = validate_finding_dict(item)
            if errors:
                raise ReviewStudioError(f"第 {index + 1} 条 Finding contract 无效：" + "; ".join(errors))
            if item["critic"] != critic:
                raise ReviewStudioError(f"第 {index + 1} 条 Finding 跨 critic，拒绝合并")
            if item["location"].get("block_id") not in block_ids:
                raise ReviewStudioError(f"第 {index + 1} 条 Finding 定位不到内部 block")
            finding = _finding_from_dict(item)
            if finding.finding_id in seen_source_ids:
                raise ReviewStudioError(f"第 {index + 1} 条 Finding ID 在同一响应中重复")
            seen_source_ids.add(finding.finding_id)
            finding.source_finding_id = finding.finding_id
            finding.finding_id = stable_id("F", critic, str(request["request_id"]), finding.source_finding_id)[:30]
            finding.origin = "model-derived"
            findings.append(finding)
        run_sequence, previous_run_sha256 = self._next_audit_binding(critic)
        run = AuditRun(stable_id("RUN", document.source.sha256, critic, _now(), secrets.token_hex(4)), critic, document.document_id, document.source.sha256, context, findings, list(parsed.get("observations", [])) if isinstance(parsed.get("observations", []), list) else [], list(parsed.get("zero_finding_basis", [])) if isinstance(parsed.get("zero_finding_basis", []), list) else [], f"manual-import:{provider}/{model}", _now(), run_sequence, previous_run_sha256)
        directory = self.root / "audits" / critic
        raw_path = directory / f"{run.run_id}.raw-response.json.txt"
        response_parents = [_parent_ref(self.root, prompt_path, role="critic-prompt"), _parent_ref(self.root, request_path, role="ai-review-request")]
        _write_tracked(self.root, raw_path, raw, parents=response_parents, provenance="model-raw-response")
        run_value = run.to_dict()
        run_value["declared_model_metadata"] = {"provider": provider, "model": model, "request_id": request["request_id"], "prompt_sha256": request["prompt_sha256"], "prompt_file_sha256": request["prompt_file_sha256"], "raw_response_sha256": _sha256(raw), "import_mode": "manual", "response_binding": response_binding}
        if response_binding == "strict-response-envelope":
            association_note = "响应逐项回显并匹配当前导出请求与原件"
        elif source_echo_verified:
            association_note = "用户把响应关联到当前请求；模型回显的原件 SHA 已匹配，但请求字段未作严格回显验证"
        else:
            association_note = "用户把原始响应导入当前所选请求；请求与原件 SHA 由应用关联，不声称模型曾回显"
        run_value["response_binding"] = {"mode": response_binding, "request_echo_verified": response_binding == "strict-response-envelope", "source_echo_verified": source_echo_verified, "source_associated_by_application": not source_echo_verified, "association_note": association_note}
        run_value["response_normalizations"] = response_normalizations
        run_path = directory / f"{run.run_id}.json"
        run_parents = [*response_parents, _parent_ref(self.root, raw_path, role="raw-model-response")]
        previous_runs = self._ordered_audit_runs(critic)
        if previous_runs:
            run_parents.append(_parent_ref(self.root, previous_runs[-1][0], role="previous-audit-run"))
        _write_tracked(self.root, run_path, canonical_json(run_value), parents=run_parents, provenance="model-parsed-audit")
        self._append_event("model_audit_imported", {"run_id": run.run_id, "critic": critic, "declared_model_metadata": run_value["declared_model_metadata"], "finding_ids": [finding.finding_id for finding in findings]})
        self._update_state(review_state="ai_review_imported", ai_review_state="imported", last_audit_at=_now())
        return run

    def _finding(self, critic: str, document: StructuredDocument, context: ReviewContext, block: DocumentBlock, *, check_id: str, check_data: Mapping[str, Any] | None = None, issue: str, standard: str, consequence: str, severity: str = "medium", verification_state: str = "needs-human-verification", suggested_action: str, owner: str = "文档负责人", blocks: bool = False, uncertainties: list[str] | None = None, basis: ExternalBasis | None = None, evidence: str | None = None, competing: list[str] | None = None, observation: str = "") -> Finding:
        finding_id = stable_id("F", document.source.sha256, critic, block.block_id, issue)[:22]
        return Finding(finding_id, critic, context.document_type, make_location(block), evidence or block.text, issue, standard, consequence, severity, verification_state, basis or ExternalBasis(jurisdiction=context.jurisdiction, unresolved_facts=list(uncertainties or [])), list(uncertainties or []), suggested_action, owner, blocks, competing_readings=list(competing or []), required_observation=observation, check_id=check_id, check_data=dict(check_data or {}))

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
                term = ambiguous.group(0)
                findings.append(self._finding(critic, document, context, block, check_id=f"expression.ambiguous_term:{term}", check_data={"term": term}, issue=f"表达“{term}”可能产生竞争读法", standard="执行者应能唯一确定主语、对象、范围、条件与时间", consequence="不同执行者可能分别采取宽读或窄读，导致通知对象、期限或责任不一致", suggested_action="补充术语定义、适用对象、触发条件和明确期限", competing=[f"读法一：仅适用于当前段落明示的对象/情形", "读法二：扩展适用于同类但未明示的对象/情形"], observation="需要观察到适用名单、授权口径或业务实例，才能排除其中一个读法"))
            if len(document.blocks) > 1 and not any(block.kind == "heading" for block in document.blocks):
                findings.append(self._finding(critic, document, context, first, check_id="expression.document_purpose", issue="文档缺少可定位的标题或目的表述", standard="收件人应能知道文件目的和需要采取的动作", consequence="接收者无法判断这是通知、征求意见还是执行指令", severity="low", suggested_action="增加标题、目的和对收件人的明确动作", competing=["读法一：信息告知，不要求采取行动", "读法二：形成需执行的工作要求"], observation="需要看到发布类型、收件人和截止日期"))
            if not findings:
                zero_basis.extend(["逐块扫描了模糊限定词与行动主体", "未发现足以形成两个竞争读法的确定性证据；仍不替代人工语境确认"])
        elif critic == "execution_feasibility":
            if not _contains_positive_term(text, ("负责人", "责任人", "牵头", "承办")):
                findings.append(self._finding(critic, document, context, first, check_id="execution.owner", issue="执行模型缺少负责人", standard="目标必须映射到交付物和明确负责人", consequence="出现延期、质量问题或跨部门依赖时没有责任承接点，无法升级或纠偏", severity="high", suggested_action="为每项交付物指定一名负责人，并写明授权边界和替补人", owner="项目负责人", blocks=True, uncertainties=["尚未确认是否存在附件或口头任命"], observation="需要看到责任矩阵或正式任命"))
            if not _contains_positive_term(text, ("预算", "费用", "金额", "经费")):
                findings.append(self._finding(critic, document, context, first, check_id="execution.budget", issue="执行模型缺少预算依据", standard="资源与预算应能支撑交付物和风险响应", consequence="采购、场地或人员成本在执行中暴露，导致范围缩水、临时垫付或项目中止", severity="high", suggested_action="补充成本项、数量、单价、预算上限和超支审批人", owner="方案负责人", blocks=True, uncertainties=["尚未确认是否存在单独预算表"], observation="需要看到预算表及审批记录"))
            if not _contains_positive_term(text, ("验收", "指标", "完成标准", "交付")):
                findings.append(self._finding(critic, document, context, first, check_id="execution.acceptance", issue="方案没有可验证的验收指标", standard="交付物应能通过事先约定的指标判断完成", consequence="项目可能在反向情形下仍自称成功，无法决定是否补救或关闭", suggested_action="为每个交付物增加可测量指标、证据格式和验收人"))
            if not findings:
                zero_basis.append("已检查目标、交付物、负责人、时间、资源、预算和验收关键词；未发现确定性缺口")
        elif critic == "compliance_legal_screen":
            triggers = []
            for word, label in (("收费", "付款/收费"), ("赞助", "赞助"), ("合同", "合同"), ("未成年人", "未成年人保护"), ("个人信息", "个人信息"), ("隐私", "隐私"), ("版权", "知识产权"), ("知识产权", "知识产权"), ("退款", "退款/票务"), ("处分", "治理/处分")):
                if _contains_positive_term(text, (word,)) and label not in triggers:
                    triggers.append(label)
            if triggers:
                findings.append(self._finding(critic, document, context, first, check_id="compliance.risk_domains", check_data={"items": triggers}, issue="文档触及需核实的合规风险领域：" + "、".join(triggers), standard="合规筛查必须绑定管辖范围、来源条款、有效性和适用事实", consequence="在缺少法源和事实确认时直接执行，可能遗漏授权、隐私、未成年人、付款或知识产权义务", severity="high", verification_state="cannot-confirm", suggested_action="补充适用地区、正式来源、条款定位和待核实事实；必要时交专业法律审查", owner="法务/审批人", blocks=False, uncertainties=["未提供可核验的法律、政策或内部制度材料"], basis=ExternalBasis(jurisdiction=context.jurisdiction, validity="unknown", application="当前仅根据文本触发词路由待核实问题", unresolved_facts=["适用主体资格", "发布或执行授权", "具体业务事实"]), observation="只有用户提供来源或联网检索得到当前有效条款后，才能改变 verification_state"))
            else:
                zero_basis.extend(["未检测到收费、合同、未成年人、个人信息、知识产权等路由触发词", "这不是合法性证明；没有来源材料时不能宣称合规"])
        elif critic == "reasonableness_governance":
            missing = []
            for word, label in (("申诉", "申诉渠道"), ("复议", "复议渠道"), ("回避", "利益冲突回避"), ("授权", "权力来源"), ("边界", "权力边界")):
                if not _contains_positive_term(text, (word,)):
                    missing.append(label)
            if missing:
                findings.append(self._finding(critic, document, context, first, check_id="governance.required_controls", check_data={"items": missing}, issue="治理文本未显示：" + "、".join(missing), standard="规范判断采用比例原则、程序正当、可申诉和利益冲突回避等明确原则", consequence="权力来源或纠错渠道不清时，弱势参与者可能承担无法复核的处分和风险", severity="medium", suggested_action="补充权力来源、边界、回避、申诉、复议和纠错机制，并说明适用原则", owner="治理审批人", uncertainties=["未确认是否存在独立治理制度"], observation="需要审阅上位章程、授权文件和申诉流程"))
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
                findings.append(self._finding(critic, document, context, first, check_id="format.required_fields", check_data={"items": checks}, issue="确定性格式项可能缺失：" + "、".join(checks), standard="标题、日期、编号、署名和附件应完整且可定位；确定性检查与语义判断分开", consequence="正式发布时收件人无法确认文件身份、时点或附件范围", severity="medium", suggested_action="补充缺失字段，并逐项核对正文、附件和表格中的日期、金额、名称一致性", owner="发文/文控负责人", blocks=context.publication_status == "external-formal"))
            if not findings:
                zero_basis.append("已检查标题、日期和附件指示词；表格合计与外部制度条款仍需人工或规则包核验")
        run_id = stable_id("RUN", document.source.sha256, critic, _now(), secrets.token_hex(4))
        return AuditRun(run_id, critic, document.document_id, document.source.sha256, context, findings, observations, zero_basis, "deterministic-local-rules", _now())

    def findings(self) -> list[Finding]:
        rows: dict[str, Finding] = {}
        current_round = self.current_review_round()
        sources = [current_round[1]] if current_round else [value for _, value, _ in self._active_audit_run_records().values()]
        for value in sources:
            for item in value.get("findings", []):
                finding = _finding_from_dict(item)
                if finding.finding_id in rows:
                    raise ReviewStudioError(f"当前审查结果包含重复 Finding ID：{finding.finding_id}")
                rows[finding.finding_id] = finding
        decisions = self._decisions()
        decision_history = self._decision_records()
        current: list[Finding] = []
        for item in rows.values():
            decision = decisions.get(item.finding_id)
            valid_snapshots = {_sha256(canonical_json(item.to_dict()))}
            if decision and int(decision.get("sequence", 0)) > 1:
                previous_sequence = int(decision["sequence"]) - 1
                previous = next((value for _, value, _ in decision_history.get(item.finding_id, []) if value.get("sequence") == previous_sequence), None)
                if previous:
                    legacy_snapshot = replace(item, status=str(previous.get("decision", "open")))
                    valid_snapshots.add(_sha256(canonical_json(legacy_snapshot.to_dict())))
            status = decision.get("decision", item.status) if decision and decision.get("finding_snapshot_sha256") in valid_snapshots else item.status
            current.append(replace(item, status=status))
        return current

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

    def _current_decision_set(self, findings: Iterable[Finding] | None = None) -> tuple[list[dict[str, Any]], str]:
        """Bind every current Finding decision, including reject and defer."""
        current = list(findings if findings is not None else self.findings())
        decisions = self._decisions()
        bindings: list[dict[str, Any]] = []
        for finding in sorted(current, key=lambda item: item.finding_id):
            decision = decisions.get(finding.finding_id)
            if not decision or finding.status == "open":
                raise ReviewStudioError(f"Finding 尚未形成当前决定：{finding.finding_id}")
            path = self.root / "finding-decisions" / f"{decision['decision_id']}.json"
            bindings.append({
                "finding_id": finding.finding_id,
                "finding_snapshot_sha256": decision["finding_snapshot_sha256"],
                "decision_id": decision["decision_id"],
                "decision": decision["decision"],
                "decision_sha256": _sha256(path.read_bytes()),
            })
        return bindings, _sha256(canonical_json(bindings))

    def _finding_artifact_path(self, finding_id: str) -> Path:
        current_round = self.current_review_round()
        if current_round and any(isinstance(item, dict) and item.get("finding_id") == finding_id for item in current_round[1].get("findings", [])):
            return current_round[0]
        matches = [
            path
            for path, value, _ in self._active_audit_run_records().values()
            if any(isinstance(item, dict) and item.get("finding_id") == finding_id for item in value.get("findings", []))
        ]
        if not matches:
            raise ReviewStudioError("找不到 Finding 的审查父产物")
        if len(matches) > 1:
            raise ReviewStudioError("Finding ID 在多个当前 critic 中冲突，拒绝选择错误父产物")
        return matches[0]

    @_serialized_mutation
    def decide_finding(self, finding_id: str, decision: str, *, reason: str, corrected_action: str | None = None) -> dict[str, Any]:
        self._ensure_writable()
        if decision not in FINDING_DECISIONS:
            raise ReviewStudioError("Finding 决定必须是 accept、reject、defer 或 correct")
        finding = next((item for item in self.findings() if item.finding_id == finding_id), None)
        if finding is None:
            raise ReviewStudioError("找不到 Finding")
        if not isinstance(reason, str) or not reason.strip():
            raise ReviewStudioError("人工裁决必须填写理由")
        if len(reason.encode("utf-8")) > 100_000:
            raise ReviewStudioError("人工裁决理由超过大小限制")
        if corrected_action is not None and not isinstance(corrected_action, str):
            raise ReviewStudioError("人工修正动作必须是文本")
        if decision == "correct":
            if corrected_action is None or not corrected_action.strip():
                raise ReviewStudioError("修正后接受必须填写非空的人工修正动作")
            if len(corrected_action.encode("utf-8")) > 100_000:
                raise ReviewStudioError("人工修正动作超过大小限制")
            corrected_action = corrected_action.strip()
        elif decision == "accept":
            corrected_action = None
        else:
            corrected_action = None
        prior_rows = self._decision_records().get(finding_id, [])
        previous = max(prior_rows, key=lambda row: row[1]["sequence"]) if prior_rows else None
        sequence = previous[1]["sequence"] + 1 if previous else 1
        authoritative_finding = replace(finding, status="open")
        record = {"artifact_type": "finding-decision", "schema_version": 2, "decision_id": stable_id("FD", finding_id, sequence, decision, _now(), secrets.token_hex(4)), "finding_id": finding_id, "critic": finding.critic, "sequence": sequence, "previous_decision_sha256": previous[2] if previous else None, "decision": decision, "reason": reason.strip(), "corrected_action": corrected_action, "finding_snapshot_sha256": _sha256(canonical_json(authoritative_finding.to_dict())), "created_at": _now(), "lifecycle": "append-only"}
        decision_path = self.root / "finding-decisions" / f"{record['decision_id']}.json"
        parents = [_parent_ref(self.root, self._finding_artifact_path(finding_id), role="audit-run")]
        if previous:
            parents.append(_parent_ref(self.root, previous[0], role="previous-decision"))
        _write_tracked(self.root, decision_path, canonical_json(record), parents=parents, provenance="human-confirmed-append-only")
        self._append_event("finding_decided", record)
        return record

    @_serialized_mutation
    def prepare_revision_bridge(self) -> Path:
        """Create a report consumable by the existing constrained revision loop.

        The bridge is a new immutable artifact; it does not rewrite the source
        or bypass Finding → Action → Hunk → Resolution approval.
        """
        self._ensure_writable()
        document_path, document = self._review_document_record()
        findings = self.findings()
        open_findings = [item.finding_id for item in findings if item.status == "open"]
        if open_findings:
            raise ReviewStudioError("所有 Finding 完成人工裁决后才能生成修改任务：" + ", ".join(open_findings))
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
        existing_report = bridge / "findings-report.md"
        existing_binding = bridge / "bridge.json"
        if existing_report.is_file() and existing_binding.is_file():
            return existing_report
        lines = ["# Document Review Studio Findings", "", f"Source SHA-256: `{document.source.sha256}`", "", "This report is a bridge into the existing constrained revision workflow. Independent critics remain separate.", ""]
        for finding in accepted:
            decision = decisions[finding.finding_id]
            approved_action = (decision.get("corrected_action") or finding.suggested_action).strip()
            lines.extend([f"## {finding.finding_id} · {finding.critic}", "", f"- Location: `{finding.location.block_id}` page={finding.location.page}", f"- Evidence: {finding.evidence}", f"- Issue: {finding.issue}", f"- Standard: {finding.standard}", f"- Consequence: {finding.consequence}", f"- Original suggested action: {finding.suggested_action}", f"- Human-approved action: {approved_action}", f"- Human decision: {finding.status} (sequence {decision['sequence']})", ""])
        report = "\n".join(lines).encode("utf-8")
        report_path = bridge / "findings-report.md"
        bridge_parents = [_parent_ref(self.root, document_path, role="structured-document"), *[_parent_ref(self.root, path, role="current-finding-decision") for path in decision_paths]]
        _write_tracked(self.root, report_path, report, parents=bridge_parents, provenance="deterministic-revision-bridge")
        binding = {"artifact_type": "revision-bridge", "schema_version": 2, "bridge_id": bridge_id, "source_sha256": document.source.sha256, "source_name": document.source.original_name, "report_relative_path": str(report_path.relative_to(self.root)).replace("\\", "/"), "report_sha256": _sha256(report), "finding_ids": [item.finding_id for item in accepted], "decision_bindings": [{"finding_id": item.finding_id, "decision_id": decisions[item.finding_id]["decision_id"], "decision_sha256": _sha256((self.root / "finding-decisions" / f"{decisions[item.finding_id]['decision_id']}.json").read_bytes()), "approved_action": (decisions[item.finding_id].get("corrected_action") or item.suggested_action).strip()} for item in accepted], "revision_loop": "existing-argument-workbench-constrained-revision", "track_changes_claimed": False, "revised_document_ready": False, "lifecycle": "immutable"}
        binding_path = bridge / "bridge.json"
        _write_tracked(self.root, binding_path, canonical_json(binding), parents=[*bridge_parents, _parent_ref(self.root, report_path, role="bridge-report")], provenance="deterministic-revision-bridge-binding")
        self._append_event("revision_bridge_prepared", binding)
        return report_path

    def _revision_plan_records(self) -> list[tuple[Path, dict[str, Any], str]]:
        rows: list[tuple[Path, dict[str, Any], str]] = []
        directory = self.root / "revision-plans"
        if not directory.is_dir():
            return rows
        for path in directory.glob("*.json"):
            if path.is_symlink():
                continue
            value = _read_json(path)
            rows.append((path, value, _sha256(path.read_bytes())))
        return rows

    def revision_plan(self) -> dict[str, Any] | None:
        """Return the newest plan still bound to every current Finding decision."""
        decisions = self._decisions()
        current_findings = self.findings()
        if any(item.status == "open" for item in current_findings):
            return None
        current_accepted_ids = {item.finding_id for item in current_findings if item.status in {"accept", "correct"}}
        try:
            current_bindings, current_digest = self._current_decision_set(current_findings)
        except ReviewStudioError:
            return None
        candidates: list[dict[str, Any]] = []
        for _, value, _ in self._revision_plan_records():
            bindings = value.get("decision_bindings", [])
            if not isinstance(bindings, list):
                continue
            if set(value.get("accepted_finding_ids", [])) != current_accepted_ids:
                continue
            if value.get("decision_set_sha256") != current_digest or bindings != current_bindings:
                continue
            valid = True
            for binding in bindings:
                current = decisions.get(str(binding.get("finding_id", "")))
                if not current or current.get("decision_id") != binding.get("decision_id"):
                    valid = False
                    break
            if valid:
                candidates.append(value)
        return max(candidates, key=lambda row: str(row.get("created_at", ""))) if candidates else None

    @staticmethod
    def _revision_operation(finding: Finding, block: DocumentBlock, approved_action: str) -> str:
        if block.kind == "table_cell":
            return "replace_table_cell"
        if re.search(r"删除|移除|删去", approved_action):
            return "delete_block"
        if finding.check_id == "expression.document_purpose":
            return "insert_before"
        if finding.check_id and finding.check_id.startswith(("execution.", "governance.", "compliance.", "format.")):
            return "append_section"
        if re.search(r"新增|增加|补充|附加", approved_action) and block.kind not in {"heading", "list_item"}:
            return "insert_after"
        return "replace_block"

    @_serialized_mutation
    def prepare_revision_plan(self) -> dict[str, Any]:
        """Create block-scoped actions from accepted Finding decisions.

        The approved action remains an instruction.  A separate Hunk must carry
        exact replacement text and receive its own human decision before use.
        """
        self._ensure_writable()
        document_path, document = self._review_document_record()
        findings = self.findings()
        open_findings = [item.finding_id for item in findings if item.status == "open"]
        if open_findings:
            raise ReviewStudioError("所有 Finding 完成人工裁决后才能生成修改计划：" + ", ".join(open_findings))
        accepted = [item for item in findings if item.status in {"accept", "correct"}]
        if not accepted:
            raise ReviewStudioError("没有已接受的 Finding，不能生成修改计划")
        decisions = self._decisions()
        decision_bindings, decision_set_sha256 = self._current_decision_set(findings)
        decision_paths = {
            binding["finding_id"]: self.root / "finding-decisions" / f"{binding['decision_id']}.json"
            for binding in decision_bindings
        }
        plan_id = stable_id("RPL", document.source.sha256, decision_set_sha256)
        plan_path = self.root / "revision-plans" / f"{plan_id}.json"
        if plan_path.is_file():
            return _read_json(plan_path)
        grouped: dict[tuple[str, str, str], list[Finding]] = {}
        for finding in accepted:
            block = document.block(finding.location.block_id)
            approved_action = (decisions[finding.finding_id].get("corrected_action") or finding.suggested_action).strip()
            operation = self._revision_operation(finding, block, approved_action)
            normalized_action = re.sub(r"\s+", "", approved_action.casefold())
            grouped.setdefault((finding.location.block_id, normalized_action, operation), []).append(finding)
        actions: list[dict[str, Any]] = []
        for (block_id, normalized_action, operation), items in sorted(grouped.items()):
            try:
                block = document.block(block_id)
            except KeyError as exc:
                raise ReviewStudioError(f"Finding 锚点不存在：{block_id}") from exc
            supported = block.kind not in {"table", "page_break"} and bool(block.text.strip())
            work_group_id = stable_id("WG", block_id, normalized_action)
            actions.append({
                "action_id": stable_id("ACT", plan_id, work_group_id, operation),
                "work_group_id": work_group_id,
                "operation_suggestion": operation,
                "block_id": block_id,
                "block_kind": block.kind,
                "before_text": block.text,
                "before_sha256": _sha256(block.text.encode("utf-8")),
                "finding_ids": [item.finding_id for item in items],
                "critic_reasons": [
                    {
                        "finding_id": item.finding_id,
                        "critic": item.critic,
                        "issue": item.issue,
                        "approved_instruction": (decisions[item.finding_id].get("corrected_action") or item.suggested_action).strip(),
                    }
                    for item in items
                ],
                "requires_manual_synthesis": len(items) > 1,
                "supported": supported,
                "unsupported_reason": "表格容器和分页符必须在具体单元格或文本块上修改" if not supported else "",
            })
        plan = {
            "artifact_type": "document-revision-plan",
            "schema_version": 1,
            "plan_id": plan_id,
            "document_id": document.document_id,
            "source_sha256": document.source.sha256,
            "decision_bindings": decision_bindings,
            "decision_set_sha256": decision_set_sha256,
            "accepted_finding_ids": sorted(item.finding_id for item in accepted),
            "actions": actions,
            "created_at": _now(),
            "lifecycle": "immutable",
        }
        parents = [
            _parent_ref(self.root, document_path, role="structured-document"),
            *[_parent_ref(self.root, path, role="current-finding-decision") for path in decision_paths.values()],
        ]
        _write_tracked(self.root, plan_path, canonical_json(plan), parents=parents, provenance="deterministic-human-approved-revision-plan")
        self._append_event("revision_plan_prepared", {"plan_id": plan_id, "action_count": len(actions)})
        return plan

    def _action_operation_records(self, plan_id: str | None = None) -> list[tuple[Path, dict[str, Any], str]]:
        rows: list[tuple[Path, dict[str, Any], str]] = []
        directory = self.root / "action-operation-decisions"
        for path in directory.glob("*.json") if directory.is_dir() else []:
            if path.is_symlink():
                continue
            value = _read_json(path)
            if plan_id is None or value.get("plan_id") == plan_id:
                rows.append((path, value, _sha256(path.read_bytes())))
        return rows

    def _current_action_operations(self, plan_id: str) -> dict[str, tuple[Path, dict[str, Any], str]]:
        grouped: dict[str, list[tuple[Path, dict[str, Any], str]]] = {}
        for row in self._action_operation_records(plan_id):
            grouped.setdefault(str(row[1].get("action_id", "")), []).append(row)
        return {action_id: max(values, key=lambda row: int(row[1].get("sequence", 0))) for action_id, values in grouped.items()}

    def _actions_with_operations(self, plan: Mapping[str, Any]) -> list[dict[str, Any]]:
        decisions = self._current_action_operations(str(plan["plan_id"]))
        rows: list[dict[str, Any]] = []
        for value in plan.get("actions", []):
            action = dict(value)
            decision = decisions.get(str(action["action_id"]))
            action["operation"] = decision[1]["operation"] if decision else None
            action["operation_decision"] = decision[1] if decision else None
            action["operation_decision_sha256"] = decision[2] if decision else None
            rows.append(action)
        return rows

    @_serialized_mutation
    def set_revision_action_operation(self, action_id: str, operation: str, *, reason: str) -> dict[str, Any]:
        self._ensure_writable()
        allowed = {"replace_block", "insert_before", "insert_after", "delete_block", "replace_table_cell", "append_section"}
        if operation not in allowed:
            raise ReviewStudioError("修改操作类型无效")
        if not isinstance(reason, str) or not reason.strip():
            raise ReviewStudioError("确认修改操作必须填写理由")
        plan = self.revision_plan()
        if not plan:
            raise ReviewStudioError("请先生成当前修改计划")
        action = next((item for item in plan.get("actions", []) if item.get("action_id") == action_id), None)
        if not action:
            raise ReviewStudioError("找不到修改动作")
        if operation == "replace_table_cell" and action.get("block_kind") != "table_cell":
            raise ReviewStudioError("replace_table_cell 只能用于表格单元格锚点")
        if action.get("block_kind") == "table_cell" and operation != "replace_table_cell":
            raise ReviewStudioError("表格单元格当前只支持 replace_table_cell")
        history = [row for row in self._action_operation_records(str(plan["plan_id"])) if row[1].get("action_id") == action_id]
        previous = max(history, key=lambda row: int(row[1].get("sequence", 0))) if history else None
        sequence = int(previous[1]["sequence"]) + 1 if previous else 1
        record = {"artifact_type": "revision-action-operation-decision", "schema_version": 1, "decision_id": stable_id("AOD", plan["plan_id"], action_id, sequence, operation, _now(), secrets.token_hex(4)), "plan_id": plan["plan_id"], "action_id": action_id, "sequence": sequence, "previous_decision_sha256": previous[2] if previous else None, "operation": operation, "reason": reason.strip(), "created_at": _now(), "lifecycle": "append-only"}
        path = self.root / "action-operation-decisions" / f"{record['decision_id']}.json"
        parents = [_parent_ref(self.root, self.root / "revision-plans" / f"{plan['plan_id']}.json", role="revision-plan")]
        if previous:
            parents.append(_parent_ref(self.root, previous[0], role="previous-operation-decision"))
        _write_tracked(self.root, path, canonical_json(record), parents=parents, provenance="human-confirmed-action-operation")
        self._append_event("revision_action_operation_decided", record)
        return record

    def _revision_hunk_records(self, plan_id: str | None = None) -> list[tuple[Path, dict[str, Any], str]]:
        rows: list[tuple[Path, dict[str, Any], str]] = []
        directory = self.root / "revision-hunks"
        if not directory.is_dir():
            return rows
        for path in directory.glob("*.json"):
            if path.is_symlink():
                continue
            value = _read_json(path)
            if plan_id is None or value.get("plan_id") == plan_id:
                rows.append((path, value, _sha256(path.read_bytes())))
        return rows

    def _current_revision_hunks(self, plan_id: str) -> dict[str, tuple[Path, dict[str, Any], str]]:
        grouped: dict[str, list[tuple[Path, dict[str, Any], str]]] = {}
        for row in self._revision_hunk_records(plan_id):
            grouped.setdefault(str(row[1].get("action_id", "")), []).append(row)
        return {
            action_id: max(rows, key=lambda row: int(row[1].get("sequence", 0)))
            for action_id, rows in grouped.items()
        }

    def _hunk_decisions(self) -> dict[str, tuple[Path, dict[str, Any], str]]:
        rows: dict[str, tuple[Path, dict[str, Any], str]] = {}
        directory = self.root / "hunk-decisions"
        if not directory.is_dir():
            return rows
        for path in directory.glob("*.json"):
            if path.is_symlink():
                continue
            value = _read_json(path)
            hunk_id = str(value.get("hunk_id", ""))
            if hunk_id in rows:
                raise ReviewStudioError(f"Hunk 存在重复决定：{hunk_id}")
            rows[hunk_id] = (path, value, _sha256(path.read_bytes()))
        return rows

    @_serialized_mutation
    def propose_revision_hunk(self, action_id: str, revised_text: str, *, rationale: str, provenance: str = "human-authored") -> dict[str, Any]:
        self._ensure_writable()
        plan = self.revision_plan()
        if not plan:
            raise ReviewStudioError("请先生成当前 Finding 决定对应的修改计划")
        action = next((item for item in self._actions_with_operations(plan) if item.get("action_id") == action_id), None)
        if not action:
            raise ReviewStudioError("找不到修改动作")
        if not action.get("operation"):
            raise ReviewStudioError("必须先由人工显式确认该 Action 的修改操作类型")
        operation_row = self._current_action_operations(str(plan["plan_id"]))[action_id]
        if not action.get("supported"):
            raise ReviewStudioError(str(action.get("unsupported_reason") or "当前锚点不支持自动修改"))
        if not isinstance(revised_text, str):
            raise ReviewStudioError("具体修改文本必须是文本")
        if action.get("operation") != "delete_block" and not revised_text.strip():
            raise ReviewStudioError("具体修改文本不能为空")
        if action.get("operation") == "delete_block" and revised_text.strip():
            raise ReviewStudioError("delete_block 的修改文本必须为空")
        if len(revised_text.encode("utf-8")) > MAX_TEXT_CORRECTION_BYTES:
            raise ReviewStudioError("具体修改文本超过安全大小限制")
        if revised_text == action.get("before_text"):
            raise ReviewStudioError("修改后文本与原文相同")
        if not rationale.strip():
            raise ReviewStudioError("Hunk 必须说明修改理由")
        if provenance not in {"human-authored", "ai-assisted-manual-import"}:
            raise ReviewStudioError("Hunk 来源必须是 human-authored 或 ai-assisted-manual-import")
        prior = [row for row in self._revision_hunk_records(str(plan["plan_id"])) if row[1].get("action_id") == action_id]
        previous = max(prior, key=lambda row: int(row[1].get("sequence", 0))) if prior else None
        sequence = int(previous[1]["sequence"]) + 1 if previous else 1
        hunk = {
            "artifact_type": "document-revision-hunk",
            "schema_version": 1,
            "hunk_id": stable_id("HNK", plan["plan_id"], action_id, sequence, revised_text, _now(), secrets.token_hex(4)),
            "plan_id": plan["plan_id"],
            "action_id": action_id,
            "sequence": sequence,
            "previous_hunk_sha256": previous[2] if previous else None,
            "operation": action["operation"],
            "operation_decision_id": operation_row[1]["decision_id"],
            "operation_decision_sha256": operation_row[2],
            "block_id": action["block_id"],
            "before_text": action["before_text"],
            "before_sha256": action["before_sha256"],
            "after_text": revised_text,
            "after_sha256": _sha256(revised_text.encode("utf-8")),
            "finding_ids": list(action["finding_ids"]),
            "rationale": rationale.strip(),
            "provenance": provenance,
            "created_at": _now(),
            "lifecycle": "append-only",
        }
        path = self.root / "revision-hunks" / f"{hunk['hunk_id']}.json"
        plan_path = self.root / "revision-plans" / f"{plan['plan_id']}.json"
        parents = [_parent_ref(self.root, plan_path, role="revision-plan"), _parent_ref(self.root, operation_row[0], role="human-operation-decision")]
        if previous:
            parents.append(_parent_ref(self.root, previous[0], role="previous-hunk"))
        _write_tracked(self.root, path, canonical_json(hunk), parents=parents, provenance=provenance)
        self._append_event("revision_hunk_proposed", {"hunk_id": hunk["hunk_id"], "action_id": action_id, "sequence": sequence})
        return hunk

    @_serialized_mutation
    def decide_revision_hunk(self, hunk_id: str, decision: str, *, reason: str) -> dict[str, Any]:
        self._ensure_writable()
        if decision not in {"approve", "reject"}:
            raise ReviewStudioError("Hunk 决定必须是 approve 或 reject")
        match = next((row for row in self._revision_hunk_records() if row[1].get("hunk_id") == hunk_id), None)
        if not match:
            raise ReviewStudioError("找不到 Hunk")
        if hunk_id in self._hunk_decisions():
            raise ReviewStudioError("该 Hunk 已有决定；需要修改时请生成新的 Hunk")
        if not reason.strip():
            raise ReviewStudioError("Hunk 决定必须填写理由")
        hunk_path, hunk, hunk_sha = match
        current = self._current_revision_hunks(str(hunk.get("plan_id", ""))).get(str(hunk.get("action_id", "")))
        if not current or current[1].get("hunk_id") != hunk_id:
            raise ReviewStudioError("只能裁决该 Action 的最新 Hunk")
        record = {
            "artifact_type": "document-revision-hunk-decision",
            "schema_version": 1,
            "decision_id": stable_id("HD", hunk_id, decision, _now(), secrets.token_hex(4)),
            "hunk_id": hunk_id,
            "hunk_sha256": hunk_sha,
            "plan_id": hunk["plan_id"],
            "action_id": hunk["action_id"],
            "decision": decision,
            "reason": reason.strip(),
            "created_at": _now(),
            "lifecycle": "immutable",
        }
        path = self.root / "hunk-decisions" / f"{record['decision_id']}.json"
        _write_tracked(self.root, path, canonical_json(record), parents=[_parent_ref(self.root, hunk_path, role="revision-hunk")], provenance="human-confirmed-hunk-decision")
        self._append_event("revision_hunk_decided", record)
        return record

    def _latest_revision(self) -> tuple[Path, dict[str, Any]] | None:
        root = self.root / "revisions"
        rows: list[tuple[Path, dict[str, Any]]] = []
        if root.is_dir():
            for path in root.glob("*/revision.json"):
                if path.is_symlink():
                    continue
                rows.append((path.parent, _read_json(path)))
        return max(rows, key=lambda row: str(row[1].get("created_at", ""))) if rows else None

    @_serialized_mutation
    def finalize_revision(self) -> Path:
        """Materialize only approved, anchor-verified Hunks and run local rechecks."""
        self._ensure_writable()
        plan = self.revision_plan()
        _, document = self._review_document_record()
        context = self.context()
        if not plan or not document or not context:
            raise ReviewStudioError("修改计划、结构化文档或审查上下文缺失")
        actions = {str(item["action_id"]): item for item in self._actions_with_operations(plan)}
        missing_operations = [action_id for action_id, item in actions.items() if not item.get("operation")]
        if missing_operations:
            raise ReviewStudioError("每个 Action 必须先人工确认修改操作类型：" + ", ".join(missing_operations))
        unsupported = [item["block_id"] for item in actions.values() if not item.get("supported")]
        if unsupported:
            raise ReviewStudioError("以下锚点不能安全自动修改，请先调整 Finding 定位：" + ", ".join(unsupported))
        hunks = self._current_revision_hunks(str(plan["plan_id"]))
        decisions = self._hunk_decisions()
        missing = [action_id for action_id in actions if action_id not in hunks or hunks[action_id][1]["hunk_id"] not in decisions]
        if missing:
            raise ReviewStudioError("每个 Action 都必须提交并裁决一个最新 Hunk：" + ", ".join(missing))
        approved: list[tuple[dict[str, Any], tuple[Path, dict[str, Any], str], tuple[Path, dict[str, Any], str]]] = []
        rejected: list[dict[str, Any]] = []
        for action_id, action in actions.items():
            hunk_row = hunks[action_id]
            decision_row = decisions[hunk_row[1]["hunk_id"]]
            if decision_row[1].get("decision") == "approve":
                approved.append((action, hunk_row, decision_row))
            else:
                rejected.append({"action_id": action_id, "block_id": action["block_id"], "finding_ids": action["finding_ids"], "reason": decision_row[1].get("reason", "")})
        original_by_id = {block.block_id: block for block in document.blocks}
        approved_by_block: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        append_actions: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for action, (_, hunk, _), _ in approved:
            block_id = str(action["block_id"])
            current = original_by_id.get(block_id)
            if current is None:
                raise ReviewStudioError(f"Hunk 锚点已丢失：{block_id}")
            if _sha256(current.text.encode("utf-8")) != hunk.get("before_sha256"):
                raise ReviewStudioError(f"Hunk 锚点内容已变化，拒绝静默应用：{block_id}")
            if hunk.get("operation") != action.get("operation"):
                raise ReviewStudioError(f"Hunk 操作与 Action 不一致：{action['action_id']}")
            if hunk.get("operation_decision_sha256") != action.get("operation_decision_sha256"):
                raise ReviewStudioError(f"Hunk 未绑定该 Action 最新的人工操作决定：{action['action_id']}")
            if action["operation"] == "append_section":
                append_actions.append((action, hunk))
            else:
                approved_by_block.setdefault(block_id, []).append((action, hunk))
        for block_id, rows in approved_by_block.items():
            destructive = [row for row in rows if row[0]["operation"] in {"replace_block", "replace_table_cell", "delete_block"}]
            if len(destructive) > 1:
                raise ReviewStudioError(f"同一锚点存在多个互斥修改，必须先人工选择或重建计划：{block_id}")

        def generated_block(action: Mapping[str, Any], text: str, position: str) -> DocumentBlock:
            block_id = stable_id("B", plan["plan_id"], action["action_id"], position, text)
            kind = "heading" if text.lstrip().startswith("# ") else "paragraph"
            clean_text = text.lstrip()[2:].strip() if kind == "heading" else text
            return DocumentBlock(block_id, kind, clean_text, 1 if kind == "heading" else None, DocumentLocation(block_id, kind, source_path="generated"), {"generated_by_action": action["action_id"]})

        revised_blocks: list[DocumentBlock] = []
        for original in document.blocks:
            rows = approved_by_block.get(original.block_id, [])
            for action, hunk in rows:
                if action["operation"] == "insert_before":
                    revised_blocks.append(generated_block(action, str(hunk["after_text"]), "before"))
            destructive = next((row for row in rows if row[0]["operation"] in {"replace_block", "replace_table_cell", "delete_block"}), None)
            if not destructive or destructive[0]["operation"] != "delete_block":
                revised_blocks.append(replace(original, text=str(destructive[1]["after_text"])) if destructive else original)
            for action, hunk in rows:
                if action["operation"] == "insert_after":
                    revised_blocks.append(generated_block(action, str(hunk["after_text"]), "after"))
        for action, hunk in append_actions:
            revised_blocks.append(generated_block(action, str(hunk["after_text"]), "append"))

        revised_by_id = {block.block_id: index for index, block in enumerate(revised_blocks)}
        for action, (_, hunk, _), _ in approved:
            if action["operation"] != "replace_table_cell":
                continue
            cell = original_by_id[action["block_id"]]
            if cell.location and cell.location.table_id and cell.location.table_id in revised_by_id:
                table_index = revised_by_id[cell.location.table_id]
                table = revised_blocks[table_index]
                rows = [list(row) for row in table.attrs.get("rows", [])]
                row, column = cell.location.row, cell.location.column
                if row is not None and column is not None and row < len(rows) and column < len(rows[row]):
                    rows[row][column] = str(hunk["after_text"])
                    revised_blocks[table_index] = replace(table, attrs={**table.attrs, "rows": rows})
        provisional = replace(document, blocks=revised_blocks)
        revised_markdown = model_to_markdown(provisional)
        revised_sha = _sha256(revised_markdown.encode("utf-8"))
        revised_source = replace(document.source, original_name="修改稿.md", extension=".md", media_type="text/markdown", byte_size=len(revised_markdown.encode("utf-8")), sha256=revised_sha, relative_path="generated")
        revised_document = replace(provisional, document_id=stable_id("DOC", revised_sha), source=revised_source, metadata={**document.metadata, "revision_of": document.document_id, "revision_plan_id": plan["plan_id"]})
        binding_hashes = [decision_row[2] for _, _, decision_row in approved] + [decisions[hunks[action_id][1]["hunk_id"]][2] for action_id in actions if decisions[hunks[action_id][1]["hunk_id"]][1].get("decision") == "reject"]
        revision_id = stable_id("REV", plan["plan_id"], *sorted(binding_hashes))
        output = self.root / "revisions" / revision_id
        if (output / "revision.json").is_file():
            return output
        plan_path = self.root / "revision-plans" / f"{plan['plan_id']}.json"
        parents = [_parent_ref(self.root, plan_path, role="revision-plan")]
        for _, hunk_row, decision_row in approved:
            parents.extend([_parent_ref(self.root, hunk_row[0], role="approved-hunk"), _parent_ref(self.root, decision_row[0], role="hunk-decision")])
        for action_id in actions:
            hunk_row = hunks[action_id]
            decision_row = decisions[hunk_row[1]["hunk_id"]]
            if decision_row[1].get("decision") == "reject":
                parents.extend([_parent_ref(self.root, hunk_row[0], role="rejected-hunk"), _parent_ref(self.root, decision_row[0], role="hunk-decision")])
        local_critics = [
            critic for critic, (_, run, _) in self._active_audit_run_records().items()
            if run.get("model_label") == "deterministic-local-rules"
        ]
        recheck_runs = [self._deterministic_audit(critic, revised_document, context).to_dict() for critic in local_critics]
        original_findings = {item.finding_id: item for item in self.findings()}
        resolution_rows: list[dict[str, Any]] = []
        recheck_findings = [
            _finding_from_dict(item)
            for run in recheck_runs
            for item in run.get("findings", [])
        ]
        approved_finding_ids = {finding_id for action, _, _ in approved for finding_id in action["finding_ids"]}
        rejected_finding_ids = {finding_id for item in rejected for finding_id in item["finding_ids"]}
        for finding_id in plan.get("accepted_finding_ids", []):
            original = original_findings.get(str(finding_id))
            if not original:
                continue
            if original.finding_id in rejected_finding_ids:
                state, basis = "still-present", "对应 Hunk 被人工拒绝，未对原 Finding 应用修改"
            elif original.critic not in local_critics:
                state, basis = "requires-external-recheck", "原审查来自外部模型，不能由本地规则冒充复审"
            else:
                candidates = [
                    item for item in recheck_findings
                    if item.critic == original.critic
                    and item.check_id
                    and item.check_id == original.check_id
                    and (original.check_data.get("items") or item.location.block_id == original.location.block_id)
                ]
                if not original.check_id:
                    state, basis = "still-present", "原 Finding 没有稳定 check_id，系统拒绝仅凭自然语言变化判定已解决"
                elif not candidates:
                    state, basis = "resolved", f"同一确定性检查 {original.check_id} 复跑后未再次产生 Finding"
                else:
                    old_items = set(original.check_data.get("items", []))
                    new_items = {value for item in candidates for value in item.check_data.get("items", [])}
                    if old_items and new_items:
                        remaining = old_items & new_items
                        if not remaining:
                            state, basis = "resolved", f"检查 {original.check_id} 的原缺口已消失；复审中的其他项另列为 new-finding"
                        elif remaining == old_items:
                            state, basis = "still-present", "稳定检查复跑后原缺口全部仍存在"
                        else:
                            state, basis = "partially-resolved", "稳定检查复跑后仅解决部分缺口，仍存在：" + "、".join(sorted(remaining))
                    else:
                        state, basis = "still-present", f"稳定检查 {original.check_id} 复跑后仍产生 Finding"
            resolution_rows.append({"finding_id": original.finding_id, "critic": original.critic, "check_id": original.check_id, "state": state, "basis": basis, "changed_by_approved_hunk": original.finding_id in approved_finding_ids})
        for original in original_findings.values():
            if original.status == "defer":
                resolution_rows.append({"finding_id": original.finding_id, "critic": original.critic, "check_id": original.check_id, "state": "deferred-by-human", "basis": "人工裁决选择暂缓，本轮修改未处理", "changed_by_approved_hunk": False})
        original_local_checks: dict[tuple[str, str], dict[str, set[str]]] = {}
        for original in original_findings.values():
            if original.critic in local_critics and original.check_id:
                identity = original_local_checks.setdefault((original.critic, original.check_id), {"items": set(), "block_ids": set()})
                identity["items"].update(original.check_data.get("items", []))
                identity["block_ids"].add(original.location.block_id)
        new_rows: list[dict[str, Any]] = []
        for current in recheck_findings:
            identity = original_local_checks.get((current.critic, str(current.check_id)))
            old_items = identity["items"] if identity else set()
            current_items = set(current.check_data.get("items", []))
            is_new = identity is None or bool(current_items - old_items) or (not current_items and current.location.block_id not in identity["block_ids"])
            if is_new:
                new_rows.append({"finding_id": current.finding_id, "critic": current.critic, "check_id": current.check_id, "state": "new-finding", "basis": current.issue, "recheck_finding": current.to_dict(), "changed_by_approved_hunk": False})
        resolution_rows.extend(new_rows)
        external_critics = sorted({row["critic"] for row in resolution_rows if row["state"] == "requires-external-recheck"})
        external_request_payloads: list[tuple[str, str, dict[str, Any], list[dict[str, Any]]]] = []
        for critic in external_critics:
            original_rows = [item.to_dict() for item in original_findings.values() if item.critic == critic and item.status in {"accept", "correct"}]
            origin = self._critic_origin_binding(critic)
            original_prompt_path = _safe_child(self.root, str(origin["original_prompt_relative_path"]))
            original_request_path = _safe_child(self.root, str(origin["original_request_relative_path"]))
            original_run_path = _safe_child(self.root, str(origin["original_audit_run_relative_path"]))
            original_prompt_snapshot = original_prompt_path.read_text(encoding="utf-8")
            request_id = stable_id("RRQ", revision_id, critic)
            base_prompt = "\n".join([
                f"# External critic recheck · {critic}",
                "",
                "Re-run exactly the critic defined by the bound original protocol below. Preserve its objective, checks, evidence standard, exclusions, and independence boundary.",
                "Do not infer resolution from wording changes and do not add another critic, vote, or score.",
                "Return JSON with request_id, prompt_sha256, revision_id, revised_sha256, critic, resolutions, and new_findings.",
                "Each resolution must contain finding_id, state (resolved|partially-resolved|still-present), reason, and evidence.",
                "Every newly detected issue must be a full Finding in new_findings; use source_finding_id only when it truly descends from an original Finding.",
                "",
                f"request_id: {request_id}",
                f"revision_id: {revision_id}",
                f"revised_sha256: {revised_sha}",
                "",
                "## Bound critic definition",
                json.dumps(CRITIC_PROTOCOLS[critic], ensure_ascii=False, indent=2),
                "",
                "## Original request/AuditRun binding",
                json.dumps(origin, ensure_ascii=False, indent=2),
                "",
                "## Exact original prompt snapshot",
                original_prompt_snapshot,
                "",
                "## Original Findings",
                json.dumps(original_rows, ensure_ascii=False, indent=2),
                "",
                "## Revised document",
                revised_markdown,
            ])
            prompt_sha256 = _sha256(base_prompt.encode("utf-8"))
            envelope = {"request_id": request_id, "prompt_sha256": prompt_sha256, "revision_id": revision_id, "revised_sha256": revised_sha, "critic": critic}
            prompt = base_prompt + "\n\n## Required response envelope\nReturn these fields exactly:\n```json\n" + json.dumps(envelope, ensure_ascii=False, indent=2) + "\n```\n"
            request = {"artifact_type": "external-critic-recheck-request", "schema_version": 2, "request_id": request_id, "revision_id": revision_id, "revised_sha256": revised_sha, "critic": critic, "critic_protocol": CRITIC_PROTOCOLS[critic], **origin, "prompt_sha256": prompt_sha256, "prompt_file_sha256": _sha256(prompt.encode("utf-8")), "original_finding_ids": [item["finding_id"] for item in original_rows], "created_at": _now(), "lifecycle": "immutable"}
            origin_parents = [_parent_ref(self.root, original_prompt_path, role="original-critic-prompt"), _parent_ref(self.root, original_request_path, role="original-ai-request"), _parent_ref(self.root, original_run_path, role="original-audit-run")]
            external_request_payloads.append((critic, prompt, request, origin_parents))
        recheck = {"artifact_type": "document-revision-recheck", "schema_version": 2, "revision_id": revision_id, "revised_sha256": revised_sha, "local_critic_runs": recheck_runs, "recheck_findings": [item.to_dict() for item in recheck_findings], "finding_resolutions": resolution_rows, "external_recheck_required": external_critics, "external_recheck_requests": [request for _, _, request, _ in external_request_payloads], "created_at": _now()}
        unresolved_lines = ["# 未解决风险", ""]
        unresolved_rows = [row for row in resolution_rows if row["state"] != "resolved"]
        if not unresolved_rows:
            unresolved_lines.append("当前复审记录中没有未解决项；这不等于自动确认文档正确、合规或完整。")
        for row in unresolved_rows:
            unresolved_lines.extend([f"## {row['finding_id']} · {row['critic']}", "", f"- check_id：{row.get('check_id') or '未提供'}", f"- 状态：{row['state']}", f"- 依据：{row['basis']}", ""])

        # Build the complete Revision and its receipts in a private staging
        # directory.  Only after every byte is ready do we rename it into the
        # official Revision path and register the batch in the integrity index.
        # A failure restores the pre-batch index and removes the generated
        # directory, so the same Revision can be retried safely.
        markdown_path = output / "修改稿.md"
        document_path = output / "document.json"
        recheck_path = output / "recheck.json"
        difference_path = output / "修改说明.md"
        unresolved_path = output / "未解决风险.md"
        revision_path = output / "revision.json"

        def pending_ref(path: Path, data: bytes, role: str) -> dict[str, Any]:
            return {"role": role, "relative_path": str(path.relative_to(self.root)).replace("\\", "/"), "sha256": _sha256(data)}

        markdown_data = revised_markdown.encode("utf-8")
        document_data = canonical_json(revised_document.to_dict())
        recheck_data = canonical_json(recheck)
        difference_data = _difference_report(model_to_markdown(document), revised_markdown).encode("utf-8")
        unresolved_data = "\n".join(unresolved_lines).encode("utf-8")
        revision = {"artifact_type": "document-revision", "schema_version": 1, "revision_id": revision_id, "plan_id": plan["plan_id"], "source_sha256": document.source.sha256, "decision_set_sha256": plan["decision_set_sha256"], "decision_bindings": plan["decision_bindings"], "revised_sha256": revised_sha, "approved_hunk_ids": [row[1]["hunk_id"] for _, row, _ in approved], "rejected_actions": rejected, "revised_markdown_relative_path": str(markdown_path.relative_to(self.root)).replace("\\", "/"), "recheck_relative_path": str(recheck_path.relative_to(self.root)).replace("\\", "/"), "created_at": _now(), "lifecycle": "immutable"}
        revision_data = canonical_json(revision)
        artifact_batch: list[tuple[Path, bytes, list[dict[str, Any]], str]] = [
            (markdown_path, markdown_data, parents, "approved-hunks-materialized"),
            (document_path, document_data, [pending_ref(markdown_path, markdown_data, "revised-markdown")], "deterministic-revised-document-model"),
            (recheck_path, recheck_data, [pending_ref(document_path, document_data, "revised-document")], "deterministic-local-recheck"),
            (difference_path, difference_data, [pending_ref(markdown_path, markdown_data, "revised-markdown")], "deterministic-diff"),
            (unresolved_path, unresolved_data, [pending_ref(recheck_path, recheck_data, "recheck")], "deterministic-unresolved-risk-report"),
            (revision_path, revision_data, [pending_ref(markdown_path, markdown_data, "revised-markdown"), pending_ref(recheck_path, recheck_data, "recheck"), pending_ref(unresolved_path, unresolved_data, "unresolved-risks")], "approved-revision-binding"),
        ]
        for critic, prompt, request, origin_parents in external_request_payloads:
            request_path = output / "external-recheck-requests" / critic / "request.json"
            prompt_path = output / "external-recheck-requests" / critic / "prompt.md"
            request_data = canonical_json(request)
            prompt_data = prompt.encode("utf-8")
            request_parents = [pending_ref(revision_path, revision_data, "revision"), pending_ref(recheck_path, recheck_data, "local-recheck"), *origin_parents]
            artifact_batch.extend([
                (request_path, request_data, request_parents, "deterministic-external-recheck-request"),
                (prompt_path, prompt_data, [*request_parents, pending_ref(request_path, request_data, "recheck-request")], "deterministic-external-recheck-prompt"),
            ])
        staging = output.parent / f".{revision_id}.staging-{secrets.token_hex(6)}"
        output.parent.mkdir(parents=True, exist_ok=True)
        index_path = _integrity_index_path(self.root)
        index_receipt_path = _integrity_receipt_path(index_path)
        original_index = index_path.read_bytes()
        original_index_receipt = index_receipt_path.read_bytes()
        try:
            staging.mkdir(parents=False, exist_ok=False)
            for final_path, data, artifact_parents, provenance in artifact_batch:
                staged_path = staging / final_path.relative_to(output)
                _write_new(staged_path, data)
                _write_new(_integrity_receipt_path(staged_path), _integrity_receipt(self.root, final_path, data, parents=artifact_parents, provenance=provenance))
            if output.exists():
                if output.is_symlink() or not output.is_dir() or (output / "revision.json").is_file():
                    raise ReviewStudioError("Revision 目标路径已存在且不能作为失败 staging 清理")
                shutil.rmtree(output)
            os.replace(staging, output)
            for final_path, data, _, provenance in artifact_batch:
                _append_integrity_index(self.root, final_path, data, artifact_type=provenance)
        except Exception:
            _atomic_write(index_path, original_index)
            _atomic_write(index_receipt_path, original_index_receipt)
            if staging.exists() and staging.is_dir() and not staging.is_symlink():
                shutil.rmtree(staging)
            if output.exists() and output.is_dir() and not output.is_symlink() and not (output / "revision.json").is_file():
                shutil.rmtree(output)
            elif output.exists() and output.is_dir() and not output.is_symlink():
                # The output belongs to this in-flight deterministic batch; a
                # restored index cannot legitimately reference it.
                shutil.rmtree(output)
            raise
        self._append_event("revision_finalized", {"revision_id": revision_id, "approved_hunks": len(approved), "rejected_actions": len(rejected)})
        return output

    def _revision_directory(self, revision_id: str, *, require_current: bool = True) -> Path:
        if not isinstance(revision_id, str) or not re.fullmatch(r"REV-[0-9a-f]{20}", revision_id):
            raise ReviewStudioError("Revision ID 无效")
        path = _safe_child(self.root / "revisions", revision_id)
        if not (path / "revision.json").is_file():
            raise ReviewStudioError("找不到 Revision")
        if require_current:
            revision = _read_json(path / "revision.json")
            plan = self.revision_plan()
            if not plan or revision.get("plan_id") != plan.get("plan_id") or revision.get("decision_set_sha256") != plan.get("decision_set_sha256"):
                raise ReviewStudioError("Revision 已因 Finding 决定变化而失效")
        return path

    def external_recheck_requests(self, revision_id: str) -> list[dict[str, Any]]:
        revision_dir = self._revision_directory(revision_id)
        rows: list[dict[str, Any]] = []
        root = revision_dir / "external-recheck-requests"
        for request_path in sorted(root.glob("*/request.json")) if root.is_dir() else []:
            value = _read_json(request_path)
            prompt_path = request_path.parent / "prompt.md"
            prompt_bytes = prompt_path.read_bytes()
            if _sha256(prompt_bytes) != value.get("prompt_file_sha256"):
                raise ReviewStudioError("外部复审协议与 request 绑定不一致")
            for path_field, hash_field in (
                ("original_prompt_relative_path", "original_prompt_file_sha256"),
                ("original_request_relative_path", "original_request_sha256"),
                ("original_audit_run_relative_path", "original_audit_run_sha256"),
            ):
                original_path = _safe_child(self.root, str(value.get(path_field, "")))
                if not original_path.is_file() or original_path.is_symlink() or _sha256(original_path.read_bytes()) != value.get(hash_field):
                    raise ReviewStudioError(f"外部复审请求的原 critic 绑定无效：{path_field}")
            value["prompt"] = prompt_bytes.decode("utf-8")
            value["relative_path"] = str(prompt_path.relative_to(self.root)).replace("\\", "/")
            rows.append(value)
        return rows

    def _external_recheck_results(self, revision_id: str, critic: str | None = None) -> list[tuple[Path, dict[str, Any], str]]:
        revision_dir = self._revision_directory(revision_id)
        rows: list[tuple[Path, dict[str, Any], str]] = []
        root = revision_dir / "external-rechecks"
        pattern = f"{critic}/*.json" if critic else "*/*.json"
        for path in root.glob(pattern) if root.is_dir() else []:
            if path.name.endswith(".raw-response.json") or path.is_symlink():
                continue
            value = _read_json(path)
            if value.get("artifact_type") == "external-critic-recheck-result":
                rows.append((path, value, _sha256(path.read_bytes())))
        return rows

    @_serialized_mutation
    def collect_external_recheck(self, revision_id: str, critic: str, response: bytes | str, *, provider: str, model: str, binding_mode: str = "strict") -> dict[str, Any]:
        self._ensure_writable()
        revision_dir = self._revision_directory(revision_id)
        request = next((item for item in self.external_recheck_requests(revision_id) if item.get("critic") == critic), None)
        if request is None:
            raise ReviewStudioError("该 Revision 没有此 critic 的外部复审请求")
        if binding_mode not in {"strict", "manual_association"}:
            raise ReviewStudioError("外部复审绑定方式无效")
        if not isinstance(provider, str) or not provider.strip() or not isinstance(model, str) or not model.strip():
            raise ReviewStudioError("外部复审导入必须声明 provider 和 model")
        raw = response.encode("utf-8") if isinstance(response, str) else response
        if not isinstance(raw, bytes) or len(raw) > MAX_TEXT_CORRECTION_BYTES:
            raise ReviewStudioError("外部复审响应超过安全大小限制")
        def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ReviewStudioError(f"外部复审 JSON 含重复字段：{key}")
                value[key] = item
            return value

        try:
            parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewStudioError("外部复审响应必须是 UTF-8 JSON") from exc
        if not isinstance(parsed, dict):
            raise ReviewStudioError("外部复审响应必须是 JSON 对象")
        envelope = {key: parsed.get(key) for key in ("request_id", "prompt_sha256", "revision_id", "revised_sha256", "critic")}
        present = {key for key, value in envelope.items() if value is not None}
        if binding_mode == "strict":
            expected = {"request_id": request["request_id"], "prompt_sha256": request["prompt_sha256"], "revision_id": revision_id, "revised_sha256": request["revised_sha256"], "critic": critic}
            if envelope != expected:
                raise ReviewStudioError("外部复审响应未严格回显当前请求、Revision、文本哈希和 critic")
        elif present and present != set(envelope):
            raise ReviewStudioError("普通关联模式不能接受不完整的绑定字段")
        resolutions = parsed.get("resolutions")
        new_findings = parsed.get("new_findings", [])
        if not isinstance(resolutions, list) or not isinstance(new_findings, list):
            raise ReviewStudioError("外部复审 resolutions 和 new_findings 必须是数组")
        expected_ids = set(request.get("original_finding_ids", []))
        parsed_ids: set[str] = set()
        clean_resolutions: list[dict[str, Any]] = []
        for item in resolutions:
            if not isinstance(item, dict):
                raise ReviewStudioError("外部 Resolution 必须是对象")
            finding_id = str(item.get("finding_id", ""))
            state = item.get("state")
            reason, evidence = item.get("reason"), item.get("evidence")
            if finding_id not in expected_ids or finding_id in parsed_ids:
                raise ReviewStudioError(f"外部 Resolution Finding ID 无效或重复：{finding_id}")
            if state not in {"resolved", "partially-resolved", "still-present"}:
                raise ReviewStudioError(f"外部 Resolution 状态无效：{finding_id}")
            if not isinstance(reason, str) or not reason.strip() or not isinstance(evidence, str) or not evidence.strip():
                raise ReviewStudioError(f"外部 Resolution 必须提供理由和修订稿证据：{finding_id}")
            parsed_ids.add(finding_id)
            clean_resolutions.append({"finding_id": finding_id, "state": state, "reason": reason.strip(), "evidence": evidence.strip()})
        if parsed_ids != expected_ids:
            raise ReviewStudioError("外部复审必须逐项覆盖请求中的全部原 Finding")
        revised_document = _document_from_dict(_read_json(revision_dir / "document.json"))
        block_ids = {block.block_id for block in revised_document.blocks}
        clean_new_findings: list[dict[str, Any]] = []
        known_ids = set(expected_ids)
        for item in new_findings:
            if not isinstance(item, dict):
                raise ReviewStudioError("外部复审 new_finding 必须是对象")
            errors = validate_finding_dict(item)
            if errors:
                raise ReviewStudioError("外部复审 Finding contract invalid: " + "; ".join(errors))
            finding = _finding_from_dict(item)
            if finding.critic != critic or finding.location.block_id not in block_ids or finding.finding_id in known_ids:
                raise ReviewStudioError("外部复审新 Finding 的 critic、锚点或 ID 无效")
            known_ids.add(finding.finding_id)
            clean_new_findings.append(finding.to_dict())
        result_id = stable_id("RR", request["request_id"], _sha256(raw), _now(), secrets.token_hex(4))
        result = {"artifact_type": "external-critic-recheck-result", "schema_version": 2, "result_id": result_id, "request_id": request["request_id"], "revision_id": revision_id, "revised_sha256": request["revised_sha256"], "critic": critic, "resolutions": clean_resolutions, "new_findings": clean_new_findings, "declared_model_metadata": {"provider": provider.strip(), "model": model.strip(), "import_mode": "manual", "response_binding": "strict-response-envelope" if binding_mode == "strict" else "manual-association"}, "response_binding": "strict-response-envelope" if binding_mode == "strict" else "manual-association", "raw_response_sha256": _sha256(raw), "created_at": _now(), "lifecycle": "immutable"}
        result_dir = revision_dir / "external-rechecks" / critic
        raw_path = result_dir / f"{result_id}.raw-response.json"
        result_path = result_dir / f"{result_id}.json"
        request_path = revision_dir / "external-recheck-requests" / critic / "request.json"
        _write_tracked(self.root, raw_path, raw, parents=[_parent_ref(self.root, request_path, role="external-recheck-request")], provenance="model-raw-external-recheck")
        _write_tracked(self.root, result_path, canonical_json(result), parents=[_parent_ref(self.root, request_path, role="external-recheck-request"), _parent_ref(self.root, raw_path, role="raw-model-response")], provenance="model-parsed-external-recheck")
        self._append_event("external_recheck_imported", {"revision_id": revision_id, "critic": critic, "result_id": result_id})
        return result

    def _external_resolution_records(self, revision_id: str) -> list[tuple[Path, dict[str, Any], str]]:
        revision_dir = self._revision_directory(revision_id)
        rows: list[tuple[Path, dict[str, Any], str]] = []
        directory = revision_dir / "resolution-decisions"
        for path in directory.glob("*.json") if directory.is_dir() else []:
            if path.is_symlink():
                continue
            value = _read_json(path)
            rows.append((path, value, _sha256(path.read_bytes())))
        return rows

    @_serialized_mutation
    def decide_external_resolution(self, revision_id: str, result_id: str, finding_id: str, state: str, *, reason: str) -> dict[str, Any]:
        self._ensure_writable()
        if state not in {"resolved", "partially-resolved", "unresolved"}:
            raise ReviewStudioError("人工 Resolution 必须是 resolved、partially-resolved 或 unresolved")
        if not isinstance(reason, str) or not reason.strip():
            raise ReviewStudioError("人工 Resolution 必须填写理由")
        result_row = next((row for row in self._external_recheck_results(revision_id) if row[1].get("result_id") == result_id), None)
        if not result_row:
            raise ReviewStudioError("找不到外部复审结果")
        result_path, result, _ = result_row
        valid_ids = {item["finding_id"] for item in result.get("resolutions", [])}
        if finding_id not in valid_ids:
            raise ReviewStudioError("只有原 Finding 的复审结论可以做 Resolution；新 Finding 必须进入下一轮裁决")
        prior = [row for row in self._external_resolution_records(revision_id) if row[1].get("result_id") == result_id and row[1].get("finding_id") == finding_id]
        previous = max(prior, key=lambda row: int(row[1].get("sequence", 0))) if prior else None
        sequence = int(previous[1]["sequence"]) + 1 if previous else 1
        record = {"artifact_type": "external-recheck-resolution-decision", "schema_version": 1, "decision_id": stable_id("ERD", result_id, finding_id, sequence, state, _now(), secrets.token_hex(4)), "revision_id": revision_id, "result_id": result_id, "finding_id": finding_id, "sequence": sequence, "previous_decision_sha256": previous[2] if previous else None, "state": state, "reason": reason.strip(), "created_at": _now(), "lifecycle": "append-only"}
        path = self._revision_directory(revision_id) / "resolution-decisions" / f"{record['decision_id']}.json"
        parents = [_parent_ref(self.root, result_path, role="external-recheck-result")]
        if previous:
            parents.append(_parent_ref(self.root, previous[0], role="previous-resolution-decision"))
        _write_tracked(self.root, path, canonical_json(record), parents=parents, provenance="human-confirmed-external-resolution")
        self._append_event("external_resolution_decided", record)
        return record

    def external_recheck_status(self, revision_id: str) -> dict[str, Any]:
        requests = self.external_recheck_requests(revision_id)
        all_decisions = self._external_resolution_records(revision_id)
        rows: list[dict[str, Any]] = []
        for request in requests:
            candidates = [row for row in self._external_recheck_results(revision_id, str(request["critic"]))]
            latest = max(candidates, key=lambda row: str(row[1].get("created_at", ""))) if candidates else None
            decisions: dict[str, dict[str, Any]] = {}
            if latest:
                for _, value, _ in all_decisions:
                    if value.get("result_id") == latest[1].get("result_id"):
                        current = decisions.get(str(value.get("finding_id", "")))
                        if current is None or int(value.get("sequence", 0)) > int(current.get("sequence", 0)):
                            decisions[str(value["finding_id"])] = value
            items: list[dict[str, Any]] = []
            if latest:
                items.extend({**item, "kind": "original", "human_decision": decisions.get(item["finding_id"])} for item in latest[1].get("resolutions", []))
                items.extend({"finding_id": item["finding_id"], "state": "new-finding", "reason": item["issue"], "evidence": item["evidence"], "kind": "new", "finding": item, "human_decision": None, "next_step": "followup-adjudication"} for item in latest[1].get("new_findings", []))
            original_items = [item for item in items if item["kind"] == "original"]
            resolution_complete = bool(latest) and bool(original_items) and all(item.get("human_decision") for item in original_items)
            followup_items = [item for item in items if item["kind"] == "new" or (item.get("human_decision") and item["human_decision"].get("state") != "resolved")]
            rows.append({**request, "result": latest[1] if latest else None, "items": items, "resolution_complete": resolution_complete, "followup_required": bool(followup_items)})
        followup_started = (self._revision_directory(revision_id) / "followup-round.json").is_file()
        resolutions_complete = all(row["resolution_complete"] for row in rows) if rows else True
        followup_required = any(row["followup_required"] for row in rows)
        for row in rows:
            row["complete"] = row["resolution_complete"] and (not row["followup_required"] or followup_started)
        return {"revision_id": revision_id, "requests": rows, "resolutions_complete": resolutions_complete, "followup_required": followup_required, "followup_started": followup_started, "can_start_followup": resolutions_complete and followup_required and not followup_started, "complete": resolutions_complete and (not followup_required or followup_started)}

    @_serialized_mutation
    def start_followup_round(self, revision_id: str) -> dict[str, Any]:
        """Promote unresolved and newly discovered external issues into a new Finding round."""
        self._ensure_writable()
        revision_dir = self._revision_directory(revision_id)
        round_path = revision_dir / "followup-round.json"
        if round_path.is_file():
            return _read_json(round_path)
        status = self.external_recheck_status(revision_id)
        if not status["resolutions_complete"]:
            raise ReviewStudioError("必须先逐项确认所有原 Finding 的外部 Resolution")
        if not status["followup_required"]:
            raise ReviewStudioError("本次外部复审没有需要进入下一轮的 Finding")
        revised_document = _document_from_dict(_read_json(revision_dir / "document.json"))
        revised_block_ids = {block.block_id for block in revised_document.blocks}
        fallback_block = revised_document.blocks[0] if revised_document.blocks else None
        prior_findings = {item.finding_id: item for item in self.findings()}
        promoted: list[Finding] = []
        source_rows: list[dict[str, Any]] = []
        critic_bindings: dict[str, dict[str, Any]] = {}
        parents = [_parent_ref(self.root, revision_dir / "revision.json", role="base-revision"), _parent_ref(self.root, revision_dir / "document.json", role="base-revised-document")]
        resolution_records = self._external_resolution_records(revision_id)
        for request in status["requests"]:
            result = request.get("result")
            if not result:
                continue
            result_row = next(row for row in self._external_recheck_results(revision_id, str(request["critic"])) if row[1].get("result_id") == result.get("result_id"))
            parents.append(_parent_ref(self.root, result_row[0], role="external-recheck-result"))
            critic_bindings[str(request["critic"])] = {key: request[key] for key in (
                "critic", "critic_protocol_sha256", "original_request_id", "original_request_sha256", "original_request_relative_path",
                "original_prompt_sha256", "original_prompt_file_sha256", "original_prompt_relative_path", "original_audit_run_id",
                "original_audit_run_sha256", "original_audit_run_relative_path", "original_provider", "original_model", "original_response_binding",
            )}
            decisions: dict[str, tuple[Path, dict[str, Any], str]] = {}
            for item in resolution_records:
                if item[1].get("result_id") != result["result_id"]:
                    continue
                finding_id = str(item[1]["finding_id"])
                current = decisions.get(finding_id)
                if current is None or int(item[1].get("sequence", 0)) > int(current[1].get("sequence", 0)):
                    decisions[finding_id] = item
            for resolution in result.get("resolutions", []):
                decision_row = decisions.get(resolution["finding_id"])
                if not decision_row:
                    continue
                decision = decision_row[1]
                parents.append(_parent_ref(self.root, decision_row[0], role="human-external-resolution"))
                if decision.get("state") == "resolved":
                    continue
                original = prior_findings.get(resolution["finding_id"])
                if not original:
                    raise ReviewStudioError("无法把未解决的原 Finding 继承到下一轮")
                location = original.location
                if location.block_id not in revised_block_ids:
                    if fallback_block is None:
                        raise ReviewStudioError("修订稿没有可用于继承 Finding 的定位")
                    location = make_location(fallback_block)
                promoted_id = stable_id("F", revision_id, result["result_id"], original.finding_id, "carry")[:30]
                promoted.append(replace(original, finding_id=promoted_id, location=location, evidence=resolution["evidence"], status="open", source_finding_id=original.finding_id, origin="external-recheck-carried-forward"))
                source_rows.append({"finding_id": promoted_id, "source_finding_id": original.finding_id, "source_result_id": result["result_id"], "reason": "human-resolution-" + str(decision["state"])})
            for new_value in result.get("new_findings", []):
                original_new = _finding_from_dict(new_value)
                promoted_id = stable_id("F", revision_id, result["result_id"], original_new.finding_id, "new")[:30]
                promoted.append(replace(original_new, finding_id=promoted_id, status="open", source_finding_id=original_new.finding_id, origin="external-recheck-new-finding"))
                source_rows.append({"finding_id": promoted_id, "source_finding_id": original_new.finding_id, "source_result_id": result["result_id"], "reason": "external-new-finding"})
        if not promoted:
            raise ReviewStudioError("没有可进入下一轮的 Finding")
        if len({item.finding_id for item in promoted}) != len(promoted):
            raise ReviewStudioError("下一轮 Finding ID 冲突")
        history = self._review_round_records()
        sequence = len(history) + 1
        round_id = stable_id("RND", revision_id, *sorted(item.finding_id for item in promoted))
        record = {"artifact_type": "document-review-followup-round", "schema_version": 1, "round_id": round_id, "sequence": sequence, "base_revision_id": revision_id, "base_revised_sha256": _read_json(revision_dir / "revision.json")["revised_sha256"], "base_document_relative_path": str((revision_dir / "document.json").relative_to(self.root)).replace("\\", "/"), "findings": [item.to_dict() for item in promoted], "finding_sources": source_rows, "critic_bindings": critic_bindings, "created_at": _now(), "lifecycle": "immutable"}
        _write_tracked(self.root, round_path, canonical_json(record), parents=parents, provenance="human-confirmed-followup-finding-round")
        self._append_event("followup_round_started", {"round_id": round_id, "base_revision_id": revision_id, "finding_ids": [item.finding_id for item in promoted]})
        return record

    def _composed_unresolved_report(self, revision_id: str) -> str:
        revision_dir = self._revision_directory(revision_id)
        recheck = _read_json(revision_dir / "recheck.json")
        external = self.external_recheck_status(revision_id)
        external_critics = {row["critic"] for row in external["requests"]}
        rows = [
            dict(row)
            for row in recheck.get("finding_resolutions", [])
            if row.get("state") != "resolved" and not (row.get("state") == "requires-external-recheck" and row.get("critic") in external_critics)
        ]
        for request in external["requests"]:
            if not request.get("result"):
                for finding_id in request.get("original_finding_ids", []):
                    rows.append({"finding_id": finding_id, "critic": request["critic"], "check_id": None, "state": "requires-external-recheck", "basis": "外部复审协议已生成，尚未导入响应"})
                continue
            for item in request.get("items", []):
                if item.get("kind") == "new":
                    rows.append({"finding_id": item["finding_id"], "critic": request["critic"], "check_id": item.get("finding", {}).get("check_id"), "state": "new-finding-awaiting-next-round" if not external.get("followup_started") else "new-finding-promoted-to-next-round", "basis": "外部复审发现的新问题不能直接判为 resolved；必须进入下一轮 Finding 裁决与修改"})
                    continue
                human = item.get("human_decision")
                if human and human.get("state") == "resolved":
                    continue
                rows.append({"finding_id": item["finding_id"], "critic": request["critic"], "check_id": item.get("finding", {}).get("check_id"), "state": human.get("state") if human else "awaiting-human-resolution", "basis": human.get("reason") if human else f"外部 critic 提议 {item.get('state')}；尚待人工 Resolution"})
        lines = ["# 未解决风险", ""]
        if not rows:
            lines.append("当前本地与外部复审决定中没有未解决项；这不等于自动确认文档正确、合规或完整。")
        for row in rows:
            lines.extend([f"## {row['finding_id']} · {row['critic']}", "", f"- check_id：{row.get('check_id') or '未提供'}", f"- 状态：{row['state']}", f"- 依据：{row.get('basis', '')}", ""])
        return "\n".join(lines)

    @_serialized_mutation
    def export_ai_reviews(self) -> Path:
        """Export imported AI reviews before Finding adjudication."""
        self._ensure_writable()
        document = self.document()
        if not document:
            raise ReviewStudioError("没有可绑定的原始文档")
        active_runs = [row for row in self._active_audit_run_records().values() if isinstance(row[1].get("declared_model_metadata"), Mapping)]
        if not active_runs:
            raise ReviewStudioError("尚未导入任何独立 AI 审查结果")
        export_id = stable_id("AIEXP", document.source.sha256, _now(), secrets.token_hex(4))
        output = self.root / "exports" / export_id
        output.mkdir(parents=True, exist_ok=False)
        run_parents = [_parent_ref(self.root, path, role="ai-audit-run") for path, _, _ in active_runs]
        raw_parents: list[dict[str, Any]] = []
        raw_exports: list[dict[str, Any]] = []
        for run_path, run, _ in active_runs:
            critic = str(run["critic"])
            run_id = str(run["run_id"])
            raw_path = run_path.parent / f"{run_id}.raw-response.json.txt"
            if not raw_path.is_file() or raw_path.is_symlink():
                raise ReviewStudioError(f"AI 审查原始响应缺失：{critic}/{run_id}")
            raw_parent = _parent_ref(self.root, raw_path, role="raw-model-response")
            raw_parents.append(raw_parent)
            exported_raw = output / "原始响应" / f"{critic}.json.txt"
            raw_data = raw_path.read_bytes()
            _write_tracked(self.root, exported_raw, raw_data, parents=[raw_parent], provenance="verbatim-model-response-export")
            raw_exports.append({"critic": critic, "run_id": run_id, "relative_path": str(exported_raw.relative_to(output)).replace("\\", "/"), "sha256": _sha256(raw_data)})
        findings = [item for _, run, _ in active_runs for item in run.get("findings", [])]
        snapshot = {
            "artifact_type": "independent-ai-review-snapshot",
            "schema_version": 1,
            "export_id": export_id,
            "status": "unadjudicated-review-snapshot",
            "source": document.source.to_dict(),
            "runs": [run for _, run, _ in active_runs],
            "findings": findings,
            "raw_responses": raw_exports,
            "finding_count": len(findings),
            "human_decisions_included": False,
            "created_at": _now(),
        }
        json_path = output / "AI审查结果.json"
        _write_tracked(self.root, json_path, canonical_json(snapshot), parents=[*run_parents, *raw_parents], provenance="deterministic-ai-review-snapshot")
        report_path = output / "AI审查报告.md"
        _write_tracked(self.root, report_path, _ai_review_markdown(snapshot).encode("utf-8"), parents=[_parent_ref(self.root, json_path, role="ai-review-snapshot")], provenance="deterministic-ai-review-render")
        manifest = {
            "artifact_type": "independent-ai-review-export",
            "schema_version": 1,
            "export_id": export_id,
            "status": "unadjudicated-review-snapshot",
            "source_sha256": document.source.sha256,
            "report_relative_path": str(report_path.relative_to(self.root)).replace("\\", "/"),
            "result_relative_path": str(json_path.relative_to(self.root)).replace("\\", "/"),
            "finding_count": len(findings),
            "critic_count": len(active_runs),
            "created_at": snapshot["created_at"],
        }
        manifest_path = output / "ai-review-export.json"
        _write_tracked(self.root, manifest_path, canonical_json(manifest), parents=[_parent_ref(self.root, json_path, role="ai-review-snapshot"), _parent_ref(self.root, report_path, role="ai-review-report")], provenance="deterministic-ai-review-export-manifest")
        package_buffer = io.BytesIO()
        with zipfile.ZipFile(package_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(output.rglob("*")):
                if path.is_file() and INTEGRITY_RECEIPT_DIR not in path.parts:
                    archive.write(path, path.relative_to(output).as_posix())
        package_path = output / "AI审查包.zip"
        _write_tracked(self.root, package_path, package_buffer.getvalue(), parents=[_parent_ref(self.root, manifest_path, role="ai-review-export-manifest")], provenance="ai-review-snapshot-archive")
        self._append_event("ai_review_export_created", {"export_id": export_id, "relative_path": str(output.relative_to(self.root)).replace("\\", "/"), "finding_count": len(findings)})
        return output

    @_serialized_mutation
    def export(self, *, revised_markdown: str | None = None) -> Path:
        self._ensure_writable()
        allowed, reasons = self.can_review()
        if not allowed:
            raise ReviewStudioError("正式导出前必须完成识别与上下文质量门：" + "；".join(reasons))
        if not self._active_audit_run_records():
            raise ReviewStudioError("正式导出前至少要完成一次本地预检或独立 AI 审查")
        document_path, document = self._review_document_record()
        findings = self.findings()
        open_findings = [item.finding_id for item in findings if item.status == "open"]
        if open_findings:
            raise ReviewStudioError("正式导出前必须裁决全部 Finding：" + ", ".join(open_findings))
        normalized = model_to_markdown(document)
        current_plan = self.revision_plan()
        latest_revision = self._latest_revision()
        trusted_revision: tuple[Path, dict[str, Any]] | None = None
        if latest_revision and current_plan and latest_revision[1].get("plan_id") == current_plan.get("plan_id"):
            current_bindings, current_decision_digest = self._current_decision_set(findings)
            if latest_revision[1].get("decision_set_sha256") == current_decision_digest and latest_revision[1].get("decision_bindings") == current_bindings:
                trusted_revision = latest_revision
        trusted_markdown = normalized
        external_recheck: dict[str, Any] | None = None
        if trusted_revision:
            trusted_path = _safe_child(self.root, str(trusted_revision[1]["revised_markdown_relative_path"]))
            trusted_markdown = trusted_path.read_text(encoding="utf-8")
            if _sha256(trusted_markdown.encode("utf-8")) != trusted_revision[1].get("revised_sha256"):
                raise ReviewStudioError("已批准修改稿与 Revision 绑定不一致")
            external_recheck = self.external_recheck_status(str(trusted_revision[1]["revision_id"]))
        if revised_markdown is not None and revised_markdown != trusted_markdown:
            raise ReviewStudioError("外部文本未经过 Finding→Action→Hunk→人工批准链，不能作为修改稿导出")
        export_id = stable_id("EXP", document.source.sha256, _now(), secrets.token_hex(4))
        output = self.root / "exports" / export_id
        output.mkdir(parents=True, exist_ok=False)
        draft = trusted_markdown
        base_parents = [_parent_ref(self.root, document_path, role="structured-document")]
        if trusted_revision:
            base_parents.append(_parent_ref(self.root, trusted_revision[0] / "revision.json", role="approved-revision"))
        draft_path = output / "draft.md"
        _write_tracked(self.root, draft_path, draft.encode("utf-8"), parents=base_parents, provenance="normalized-editable-copy")
        runs: list[dict[str, Any]] = []
        for path in sorted((self.root / "audits").glob("*/*.json")) if (self.root / "audits").is_dir() else []:
            try:
                runs.append(_read_json(path))
            except (OSError, ValueError, ReviewStudioError):
                continue
        decisions = self._decisions()
        chain_parents = list(base_parents)
        external_evidence_parents: list[dict[str, Any]] = []
        if trusted_revision:
            revision_id = str(trusted_revision[1]["revision_id"])
            external_evidence_parents.extend(_parent_ref(self.root, row[0], role="external-recheck-result") for row in self._external_recheck_results(revision_id))
            external_evidence_parents.extend(_parent_ref(self.root, row[0], role="human-external-resolution") for row in self._external_resolution_records(revision_id))
            chain_parents.extend(external_evidence_parents)
        for path in sorted((self.root / "audits").glob("*/*.json")) if (self.root / "audits").is_dir() else []:
            chain_parents.append(_parent_ref(self.root, path, role="audit-run"))
        for value in decisions.values():
            chain_parents.append(_parent_ref(self.root, self.root / "finding-decisions" / f"{value['decision_id']}.json", role="current-finding-decision"))
        bridge_root = self.root / "exports" / "revision-bridge"
        for bridge_path in sorted(bridge_root.glob("*/bridge.json")) if bridge_root.is_dir() else []:
            chain_parents.append(_parent_ref(self.root, bridge_path, role="revision-bridge"))
        current_round = self.current_review_round()
        if current_round:
            chain_parents.append(_parent_ref(self.root, current_round[0], role="current-review-round"))
        audit = {"artifact_type": "document-review-export", "schema_version": 2, "product_status": "experimental-preview", "export_id": export_id, "source": document.source.to_dict(), "parser": {"name": document.parser_name, "version": document.parser_version}, "quality": document.quality.to_dict(), "warnings": [warning.to_dict() for warning in document.warnings], "audit_runs": runs, "findings": [finding.to_dict() for finding in findings], "decisions": list(decisions.values()), "review_round": current_round[1] if current_round else None, "revision": trusted_revision[1] if trusted_revision else None, "external_recheck": external_recheck, "independent_critics": list(CRITIC_DIMENSIONS), "scores": None, "legal_boundary": "合规筛查不是律师意见；无来源材料时只能输出待核实问题", "created_at": _now()}
        audit_path = output / "audit.json"
        _write_tracked(self.root, audit_path, canonical_json(audit), parents=chain_parents, provenance="deterministic-audit-export")
        quality_path = output / "quality-report.json"
        _write_tracked(self.root, quality_path, canonical_json({"source": document.source.to_dict(), "quality": document.quality.to_dict(), "warnings": [warning.to_dict() for warning in document.warnings]}), parents=base_parents, provenance="deterministic-quality-export")
        audit_markdown_path = output / "audit.md"
        _write_tracked(self.root, audit_markdown_path, _audit_markdown(audit).encode("utf-8"), parents=[_parent_ref(self.root, audit_path, role="audit-json")], provenance="deterministic-audit-render")
        if trusted_revision:
            revised_markdown_path = output / "修改稿.md"
            _write_tracked(self.root, revised_markdown_path, draft.encode("utf-8"), parents=[_parent_ref(self.root, trusted_revision[0] / "修改稿.md", role="approved-revised-markdown")], provenance="approved-revision-export")
            revised_docx_path = output / "修改稿.docx"
            _write_tracked(self.root, revised_docx_path, _minimal_docx(draft), parents=[_parent_ref(self.root, revised_markdown_path, role="approved-revised-markdown")], provenance="approved-revision-docx-export")
            for name in ("修改说明.md", "recheck.json"):
                source_path = trusted_revision[0] / name
                if source_path.is_file():
                    _write_tracked(self.root, output / name, source_path.read_bytes(), parents=[_parent_ref(self.root, source_path, role="revision-evidence")], provenance="approved-revision-evidence-export")
            unresolved_path = output / "未解决风险.md"
            _write_tracked(self.root, unresolved_path, self._composed_unresolved_report(str(trusted_revision[1]["revision_id"])).encode("utf-8"), parents=[_parent_ref(self.root, trusted_revision[0] / "recheck.json", role="revision-recheck"), *external_evidence_parents], provenance="composed-local-and-external-risk-export")
            capability_path = output / "track-changes-capability.json"
            _write_tracked(self.root, capability_path, canonical_json({"native_track_changes": False, "revised_document_ready": True, "output_name": "修改稿.docx", "message": "修改稿由已批准 Hunk 生成；提供逐行差异报告，但不冒充 Word 原生 Track Changes"}), parents=[_parent_ref(self.root, revised_docx_path, role="revised-docx")])
        elif document.source.extension == ".docx":
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
        package_buffer = io.BytesIO()
        package_files: list[tuple[str, str]] = []
        with zipfile.ZipFile(package_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(self.root.rglob("*")):
                if path.is_symlink() or not path.is_file() or path.name in {"state.json", ".mutation.lock", "audit-package.zip"}:
                    continue
                relative = path.relative_to(self.root).as_posix()
                if relative.startswith("exports/") and not relative.startswith(f"exports/{export_id}/") and not relative.startswith("exports/revision-bridge/"):
                    continue
                data = path.read_bytes()
                archive.writestr("project/" + relative, data)
                package_files.append((relative, _sha256(data)))
            package_manifest = {
                "artifact_type": "document-review-audit-package-manifest",
                "schema_version": 1,
                "export_id": export_id,
                "source_sha256": document.source.sha256,
                "files": [{"relative_path": relative, "sha256": digest} for relative, digest in package_files],
                "created_at": _now(),
            }
            archive.writestr("package-manifest.json", canonical_json(package_manifest))
        _write_tracked(self.root, output / "audit-package.zip", package_buffer.getvalue(), parents=[_parent_ref(self.root, audit_path, role="audit-json"), _parent_ref(self.root, audit_markdown_path, role="audit-markdown")], provenance="audit-package-archive")
        self._append_event("export_created", {"export_id": export_id, "relative_path": str(output.relative_to(self.root)).replace("\\", "/")})
        return output

    def ai_requests(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        active = [record for critic in CRITIC_DIMENSIONS if (record := self._active_ai_request(critic)) is not None]
        for request_path, request_value, _ in active:
            try:
                value = dict(request_value)
                prompt_path = request_path.parent / "prompt.md"
                value["prompt"] = prompt_path.read_text(encoding="utf-8")
                value["relative_path"] = str(prompt_path.relative_to(self.root)).replace("\\", "/")
                matching_runs: list[dict[str, Any]] = []
                for run_path in sorted((self.root / "audits" / str(value.get("critic", ""))).glob("*.json")):
                    try:
                        run = _read_json(run_path)
                    except (OSError, ValueError, ReviewStudioError):
                        continue
                    metadata = run.get("declared_model_metadata", {})
                    if metadata.get("request_id") == value.get("request_id"):
                        matching_runs.append(run)
                latest = max(
                    matching_runs,
                    key=lambda run: int(run.get("run_sequence", 0)) if isinstance(run.get("run_sequence"), int) else 0,
                ) if matching_runs else None
                value["completed"] = latest is not None
                value["run_id"] = latest.get("run_id") if latest else None
                value["finding_count"] = len(latest.get("findings", [])) if latest else 0
                value["response_binding"] = latest.get("response_binding") if latest else None
                value["storage_relative_path"] = f"audits/{value.get('critic')}" if latest else None
                rows.append(value)
            except (OSError, ValueError, ReviewStudioError):
                continue
        return rows

    def export_summary(self) -> list[dict[str, Any]]:
        """Return user-facing export files without exposing receipt internals."""
        rows: list[dict[str, Any]] = []
        root = self.root / "exports"
        if not root.is_dir():
            return rows
        labels = {
            "audit.json": "结构化结果",
            "audit.md": "审查报告",
            "audit-package.zip": "完整审计包",
            "quality-report.json": "识别质量报告",
            "normalized-editable-copy.docx": "可编辑规范化副本",
            "editable-draft.md": "可编辑规范化副本",
            "draft.md": "规范化文本副本",
            "difference-report.md": "差异报告",
            "修改稿.md": "修改稿（Markdown）",
            "修改稿.docx": "修改稿（Word）",
            "修改说明.md": "修改说明与逐行差异",
            "未解决风险.md": "未解决风险",
            "recheck.json": "复审结果",
            "track-changes-capability.json": "修订能力声明",
            "AI审查报告.md": "AI 审查报告（未经人工裁决）",
            "AI审查结果.json": "AI 审查结构化结果",
            "AI审查包.zip": "AI 审查包",
            "ai-review-export.json": "AI 审查导出清单",
        }
        for directory in sorted((path for path in root.iterdir() if path.is_dir() and path.name != "revision-bridge"), reverse=True):
            files = []
            for path in sorted(directory.rglob("*")):
                if not path.is_file() or INTEGRITY_RECEIPT_DIR in path.parts:
                    continue
                relative = str(path.relative_to(self.root)).replace("\\", "/")
                files.append({"name": path.name, "label": labels.get(path.name, path.name), "relative_path": relative, "size": path.stat().st_size})
            if not files:
                continue
            audit_value: dict[str, Any] = {}
            kind = "export"
            try:
                audit_value = _read_json(directory / "audit.json")
            except (OSError, ValueError, ReviewStudioError):
                try:
                    audit_value = _read_json(directory / "ai-review-export.json")
                    kind = "ai-review"
                except (OSError, ValueError, ReviewStudioError):
                    pass
            rows.append({"kind": kind, "export_id": directory.name, "created_at": audit_value.get("created_at") or datetime.fromtimestamp(directory.stat().st_mtime, tz=timezone.utc).isoformat(), "finding_count": audit_value.get("finding_count", 0), "files": files})
        bridge_root = root / "revision-bridge"
        for directory in sorted((path for path in bridge_root.iterdir() if path.is_dir()), reverse=True) if bridge_root.is_dir() else []:
            files = []
            for path in sorted(directory.rglob("*")):
                if path.is_file() and INTEGRITY_RECEIPT_DIR not in path.parts:
                    files.append({"name": path.name, "label": "修改任务报告" if path.name == "findings-report.md" else path.name, "relative_path": str(path.relative_to(self.root)).replace("\\", "/"), "size": path.stat().st_size})
            if not files:
                continue
            binding: dict[str, Any] = {}
            try:
                binding = _read_json(directory / "bridge.json")
            except (OSError, ValueError, ReviewStudioError):
                pass
            rows.append({"kind": "revision-bridge", "export_id": directory.name, "created_at": datetime.fromtimestamp(directory.stat().st_mtime, tz=timezone.utc).isoformat(), "finding_count": len(binding.get("finding_ids", [])), "files": files})
        return sorted(rows, key=lambda row: str(row.get("created_at", "")), reverse=True)

    def finding_work_groups(self, findings: Iterable[Finding] | None = None, *, limit: int = 30) -> dict[str, Any]:
        """Build a transparent attention queue without merging critic evidence."""
        source = list(findings if findings is not None else self.findings())
        severity_rank = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
        grouped: dict[tuple[str, str], list[Finding]] = {}
        for finding in source:
            normalized_action = re.sub(r"\s+", "", finding.suggested_action.casefold())
            grouped.setdefault((finding.location.block_id, normalized_action), []).append(finding)
        rows: list[dict[str, Any]] = []
        for (block_id, normalized_action), items in grouped.items():
            max_severity = max((severity_rank[item.severity] for item in items), default=0)
            blockers = sum(1 for item in items if item.blocks_release_or_execution)
            open_count = sum(1 for item in items if item.status == "open")
            critic_count = len({item.critic for item in items})
            structural = any(re.search(r"结论|决策|结构|执行|合规|授权|责任|期限|预算|证据|引证", item.issue + item.consequence) for item in items)
            reasons: list[str] = []
            if blockers:
                reasons.append("阻断发布或执行")
            if max_severity >= severity_rank["high"]:
                reasons.append("高严重度")
            if structural:
                reasons.append("影响结论、结构或执行链")
            if critic_count > 1:
                reasons.append(f"{critic_count} 个独立 critic 指向同一修改动作")
            if not reasons:
                reasons.append("一般审查项")
            rows.append({
                "group_id": stable_id("WG", block_id, normalized_action),
                "block_id": block_id,
                "suggested_action": items[0].suggested_action,
                "finding_count": len(items),
                "open_count": open_count,
                "critic_count": critic_count,
                "priority_reasons": reasons,
                "findings": [item.to_dict() for item in sorted(items, key=lambda item: (-severity_rank[item.severity], item.critic, item.finding_id))],
                "_priority": (1 if open_count else 0, 1 if blockers else 0, max_severity, 1 if structural else 0, critic_count),
            })
        rows.sort(key=lambda row: (tuple(-value for value in row["_priority"]), row["block_id"], row["group_id"]))
        for row in rows:
            row.pop("_priority", None)
        return {"default_limit": limit, "total_groups": len(rows), "hidden_groups": max(0, len(rows) - limit), "groups": rows}

    def revision_workspace(self) -> dict[str, Any]:
        plan = self.revision_plan()
        if not plan:
            return {"plan": None, "actions": [], "ready_to_finalize": False, "revision": None}
        hunks = self._current_revision_hunks(str(plan["plan_id"]))
        decisions = self._hunk_decisions()
        actions: list[dict[str, Any]] = []
        ready = True
        for action in self._actions_with_operations(plan):
            row = dict(action)
            hunk_row = hunks.get(str(action["action_id"]))
            hunk = dict(hunk_row[1]) if hunk_row else None
            decision = dict(decisions[hunk["hunk_id"]][1]) if hunk and hunk["hunk_id"] in decisions else None
            row["hunk"] = hunk
            row["hunk_decision"] = decision
            row["hunk_stale"] = bool(hunk and hunk.get("operation_decision_sha256") != action.get("operation_decision_sha256"))
            if not action.get("supported") or not action.get("operation") or hunk is None or decision is None or row["hunk_stale"]:
                ready = False
            actions.append(row)
        latest = self._latest_revision()
        revision = latest[1] if latest and latest[1].get("plan_id") == plan.get("plan_id") and latest[1].get("decision_set_sha256") == plan.get("decision_set_sha256") else None
        external_recheck = self.external_recheck_status(str(revision["revision_id"])) if revision else None
        return {"plan": {key: value for key, value in plan.items() if key != "actions"}, "actions": actions, "ready_to_finalize": ready, "revision": revision, "external_recheck": external_recheck}

    @_serialized_mutation
    def export_file(self, relative_path: str) -> Path:
        """Resolve one export/bridge file, rejecting traversal and symlinks."""
        if not isinstance(relative_path, str) or not relative_path.startswith(("exports/", "revisions/")):
            raise ReviewStudioError("只能访问项目导出或 Revision 目录中的文件")
        candidate = (self.root / relative_path).resolve()
        allowed_roots = ((self.root / "exports").resolve(), (self.root / "revisions").resolve())
        if not any(candidate.is_relative_to(root) for root in allowed_roots):
            raise ReviewStudioError("文件不在允许的项目产物目录内")
        if candidate.is_symlink() or not candidate.is_file():
            raise ReviewStudioError("导出文件不存在")
        errors = self.integrity_errors()
        if errors:
            raise ReviewStudioError("项目或导出文件完整性校验失败，拒绝按可信文件下载：" + "; ".join(errors))
        return candidate

    @_serialized_mutation
    def view(self) -> dict[str, Any]:
        self._enforce_integrity()
        manifest = self.manifest()
        state = self.state()
        try:
            _, document = self._review_document_record()
        except (OSError, KeyError, TypeError, ValueError, ReviewStudioError):
            document = None
        can_review, reasons = self.can_review()
        try:
            finding_objects = self.findings()
            finding_rows = [finding.to_dict() for finding in finding_objects]
            attention_queue = self.finding_work_groups(finding_objects)
        except (OSError, KeyError, TypeError, ValueError, ReviewStudioError):
            finding_rows = []
            attention_queue = {"default_limit": 30, "total_groups": 0, "hidden_groups": 0, "groups": []}
        ai_requests = self.ai_requests()
        findings_total = len(finding_rows)
        finding_summary = {
            "total": findings_total,
            **{decision: sum(1 for item in finding_rows if item.get("status") == decision) for decision in ("open", "accept", "correct", "reject", "defer")},
            "by_severity": {severity: sum(1 for item in finding_rows if item.get("severity") == severity) for severity in ("critical", "high", "medium", "low", "info")},
        }
        exports = self.export_summary()
        extraction_confirmed = state.get("extraction_state") in {"confirmed", "confirmed_corrected", "confirmed_with_warning"}
        local_complete = any(
            value.get("model_label") == "deterministic-local-rules"
            for critic in CRITIC_DIMENSIONS
            for _, value, _ in self._audit_run_records(critic)
        )
        ai_done = sum(1 for item in ai_requests if item.get("completed"))
        try:
            revision_workspace = self.revision_workspace()
        except (OSError, KeyError, TypeError, ValueError, ReviewStudioError):
            revision_workspace = {"plan": None, "actions": [], "ready_to_finalize": False, "revision": None}
        bridge_complete = revision_workspace.get("plan") is not None
        revision_complete = revision_workspace.get("revision") is not None
        export_complete = any(item.get("kind") == "export" for item in exports)
        workflow = [
            {"key": "extraction", "label": "文档识别", "status": "completed" if extraction_confirmed else "not_started", "detail": "已确认" if extraction_confirmed else "待确认"},
            {"key": "context", "label": "审查上下文", "status": "completed" if state.get("context_state") == "confirmed" else "not_started", "detail": "已确认" if state.get("context_state") == "confirmed" else "待确认"},
            {"key": "local", "label": "本地预检", "status": "completed" if local_complete else "not_started", "detail": "已运行" if local_complete else "未运行"},
            {"key": "ai", "label": "AI 专项审查", "status": "completed" if ai_requests and ai_done == len(ai_requests) else "in_progress" if ai_done else "not_started", "detail": f"{ai_done}/{len(ai_requests) or 5} 已导入"},
            {"key": "adjudication", "label": "人工裁决", "status": "completed" if findings_total and finding_summary["open"] == 0 else "in_progress" if findings_total else "not_started", "detail": f"{findings_total - finding_summary['open']}/{findings_total} 已处理" if findings_total else "暂无 Finding"},
            {"key": "bridge", "label": "受约束修改", "status": "completed" if revision_complete else "in_progress" if bridge_complete else "not_started", "detail": "修改稿已生成并复审" if revision_complete else "逐段修改中" if bridge_complete else "未开始"},
            {"key": "export", "label": "导出结果", "status": "completed" if export_complete else "not_started", "detail": "已有导出文件" if export_complete else "未导出"},
        ]
        return {"project": manifest, "product_status": "experimental-preview", "state": state, "extraction": {"available": document is not None, "quality": document.quality.to_dict() if document else {}, "warnings": [warning.to_dict() for warning in document.warnings] if document else [], "blocks": [block.to_dict() for block in document.blocks] if document else [], "total_blocks": len(document.blocks) if document else 0}, "context": self.context().to_dict() if self.context() else {"model_suggestion": self.suggested_document_type()}, "can_review": can_review, "review_blockers": reasons, "ai_requests": ai_requests, "findings": finding_rows, "finding_summary": finding_summary, "attention_queue": attention_queue, "revision_workspace": revision_workspace, "workflow": workflow, "exports": exports}


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
    return Finding(value["finding_id"], value["critic"], value["document_type"], location, value["evidence"], value["issue"], value["standard"], value["consequence"], value["severity"], value["verification_state"], basis, list(value["uncertainties"]), value["suggested_action"], value["suggested_owner"], value["blocks_release_or_execution"], value.get("status", "open"), value.get("origin", "model-derived"), list(value.get("competing_readings", [])), value.get("required_observation", ""), value.get("proposed_group_id"), value.get("source_finding_id"), value.get("check_id"), dict(value.get("check_data", {})))


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


def _ai_review_markdown(snapshot: Mapping[str, Any]) -> str:
    source = snapshot["source"]
    lines = [
        "# 独立 AI 审查报告",
        "",
        "> 状态：未经人工裁决的审查快照。本报告不表示 Finding 已被接受、解决或应用到原文。",
        "",
        f"- 原始文件：`{source['original_name']}`",
        f"- 原始文件 SHA-256：`{source['sha256']}`",
        f"- 已导入 critic：{len(snapshot['runs'])}",
        f"- Finding：{snapshot['finding_count']}",
        "",
    ]
    for run in snapshot["runs"]:
        metadata = run.get("declared_model_metadata", {})
        lines.extend([f"## {run['critic']}", "", f"- Provider / model：{metadata.get('provider', '未声明')} / {metadata.get('model', '未声明')}", f"- Run：`{run['run_id']}`", f"- Response binding：{run.get('response_binding', {}).get('mode', '未记录')}", ""])
        findings = run.get("findings", [])
        if not findings:
            basis = "；".join(run.get("zero_finding_basis", [])) or "模型未提供零 Finding 检查依据"
            lines.extend(["本 critic 未返回 Finding。", "", f"检查依据：{basis}", ""])
            continue
        for finding in findings:
            lines.extend([f"### {finding['finding_id']}", "", f"- 位置：`{finding['location']['block_id']}`，page {finding['location'].get('page') or '-'}", f"- 证据：{finding['evidence']}", f"- 问题：{finding['issue']}", f"- 判断标准：{finding['standard']}", f"- 后果：{finding['consequence']}", f"- 严重度：{finding['severity']}", f"- 核实状态：{finding['verification_state']}", f"- 建议动作：{finding['suggested_action']}", ""])
    lines.extend(["## 后续", "", "请回到 Document Review Studio 对每条 Finding 分别接受、修正、拒绝或暂缓；不要把本快照当作已经批准的修改意见。", ""])
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
