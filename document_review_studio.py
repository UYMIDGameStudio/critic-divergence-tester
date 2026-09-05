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
import shutil
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Iterable, Mapping

from document_review_ingest import IngestionError, IngestionLimits, ingest_bytes, safe_upload_name
from academic_review import academic_prechecks
from document_review_model import (
    AuditRun,
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
from project_lock import (
    ProjectMutationLockedError,
    project_mutation_lock as _shared_project_mutation_lock,
)
from review_profiles import (
    ALL_CRITICS as CRITIC_DIMENSIONS, ACADEMIC_PROTOCOLS, CRITIC_LABELS,
    PROFILES, DISCIPLINES, RESEARCH_TYPES, profile_critics,
)


STORE_SUFFIX = ".document-review-studio"
STUDIO_SCHEMA_VERSION = 1
MAX_TEXT_CORRECTION_BYTES = 8 * 1024 * 1024
INTEGRITY_POLICY_NAME = "integrity-policy.json"
INTEGRITY_RECEIPT_DIR = ".integrity"
INTEGRITY_INDEX_NAME = "integrity-index.json"
EXTRACTION_DECISION_DIR_NAME = "extraction-decisions"


@contextmanager
def _project_mutation_lock(root: Path):
    """Serialize one complete project mutation in this and other processes."""
    try:
        with _shared_project_mutation_lock(root):
            yield
    except (ProjectMutationLockedError, ValueError) as exc:
        raise ReviewStudioError(str(exc)) from exc


def _serialized_mutation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with _project_mutation_lock(self.root):
            return method(self, *args, **kwargs)

    return wrapped


CRITIC_PROTOCOLS: dict[str, dict[str, Any]] = {
    **ACADEMIC_PROTOCOLS,
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


from document_review_components import (
    COMPONENT_BY_METHOD,
    COMPONENT_TYPES,
    bind_studio_globals,
)


class DocumentReviewProject:
    """One immutable upload plus append-only workflow decisions."""

    def __init__(self, root: Path):
        candidate = Path(root)
        if candidate.is_symlink():
            raise ReviewStudioError("项目目录不得是符号链接")
        self.root = candidate.resolve()
        self._components = {
            component_type: component_type(self) for component_type in COMPONENT_TYPES
        }
        self._ensure_root()

    def __getattr__(self, name: str):
        component_type = COMPONENT_BY_METHOD.get(name)
        if component_type is None:
            raise AttributeError(name)
        bind_studio_globals(globals(), component_type)
        return getattr(self._components[component_type], name)

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
        with _project_mutation_lock(storage):
            if target.is_symlink():
                raise ReviewStudioError("项目路径不得是符号链接")
            if target.exists():
                project = cls(target)
                manifest = project.manifest()
                if manifest.get("source", {}).get("sha256") != _sha256(content):
                    raise ReviewStudioError("同名项目的原件 SHA-256 不匹配")
                return project
            staging = storage / f".{target.name}.{os.getpid()}.{secrets.token_hex(8)}.import"
            staging.mkdir()
            try:
                _ensure_integrity_policy(staging, create=True)
                _initialize_integrity_index(staging)
                source_dir = staging / "source"
                source_dir.mkdir()
                source_path = source_dir / safe_name
                _write_tracked(staging, source_path, content, provenance="user-uploaded")
                manifest = {
                    "schema_version": STUDIO_SCHEMA_VERSION,
                    "project_id": stable_id("PRJ", _sha256(content), safe_name),
                    "title": title or Path(safe_name).stem,
                    "source": {"name": safe_name, "sha256": _sha256(content), "bytes": len(content), "relative_path": f"source/{safe_name}"},
                    "created_at": _now(),
                    "original_never_overwritten": True,
                }
                _write_tracked(staging, staging / "project.json", canonical_json(manifest), parents=[_parent_ref(staging, source_path, role="original-source")])
                state = {"extraction_state": "unconfirmed", "context_state": "missing", "review_state": "not_started", "read_only": False, "diagnostics": []}
                _write_new(staging / "state.json", canonical_json(state))
                project = cls(staging)
                try:
                    document = ingest_bytes(safe_name, content, limits=limits, ocr=ocr)
                except (IngestionError, OSError, ValueError) as exc:
                    diagnostic = {"schema_version": 1, "kind": "ingestion-failure", "safe": True, "message": str(exc)[:1000], "source_sha256": _sha256(content), "created_at": _now()}
                    _write_tracked(staging, staging / "extraction" / "diagnostic.json", canonical_json(diagnostic), parents=[_parent_ref(staging, source_path, role="original-source")])
                    project._update_state(extraction_state="blocked", diagnostics=[diagnostic["message"]])
                    project._append_event("ingestion_failed", diagnostic)
                else:
                    project._save_document(document)
                    project._append_event("uploaded", {"source_sha256": document.source.sha256, "parser": document.parser_name})
                os.replace(staging, target)
            except Exception:
                if staging.exists() and not staging.is_symlink():
                    shutil.rmtree(staging)
                raise
        return cls(target)
















































































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
            {"key": "ai", "label": "AI 专项审查", "status": "completed" if ai_requests and ai_done == len(ai_requests) else "in_progress" if ai_done else "not_started", "detail": f"{ai_done}/{len(ai_requests) or len(self.review_critics())} 已导入"},
            {"key": "adjudication", "label": "人工裁决", "status": "completed" if findings_total and finding_summary["open"] == 0 else "in_progress" if findings_total else "not_started", "detail": f"{findings_total - finding_summary['open']}/{findings_total} 已处理" if findings_total else "暂无 Finding"},
            {"key": "bridge", "label": "受约束修改", "status": "completed" if revision_complete else "in_progress" if bridge_complete else "not_started", "detail": "修改稿已生成并复审" if revision_complete else "逐段修改中" if bridge_complete else "未开始"},
            {"key": "export", "label": "导出结果", "status": "completed" if export_complete else "not_started", "detail": "已有导出文件" if export_complete else "未导出"},
        ]
        return {"review_critics": {key: CRITIC_LABELS[key] for key in self.review_critics()}, "project": manifest, "product_status": "experimental-preview", "state": state, "extraction": {"available": document is not None, "quality": document.quality.to_dict() if document else {}, "warnings": [warning.to_dict() for warning in document.warnings] if document else [], "blocks": [block.to_dict() for block in document.blocks] if document else [], "total_blocks": len(document.blocks) if document else 0}, "context": self.context().to_dict() if self.context() else {"model_suggestion": self.suggested_document_type()}, "can_review": can_review, "review_blockers": reasons, "ai_requests": ai_requests, "findings": finding_rows, "finding_summary": finding_summary, "attention_queue": attention_queue, "revision_workspace": revision_workspace, "workflow": workflow, "exports": exports}


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


bind_studio_globals(globals())


def _install_component_methods() -> None:
    for name, component_type in COMPONENT_BY_METHOD.items():
        if hasattr(DocumentReviewProject, name):
            continue
        component_method = getattr(component_type, name)

        @wraps(component_method)
        def delegated(self, *args, __name=name, __type=component_type, **kwargs):
            bind_studio_globals(globals(), __type)
            return getattr(self._components[__type], __name)(*args, **kwargs)

        setattr(DocumentReviewProject, name, delegated)


_install_component_methods()
