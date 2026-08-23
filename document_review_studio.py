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
        source_dir = target / "source"
        source_dir.mkdir()
        _write_new(source_dir / safe_name, content)
        manifest = {
            "schema_version": STUDIO_SCHEMA_VERSION,
            "project_id": stable_id("PRJ", _sha256(content), safe_name),
            "title": title or Path(safe_name).stem,
            "source": {"name": safe_name, "sha256": _sha256(content), "bytes": len(content), "relative_path": f"source/{safe_name}"},
            "created_at": _now(),
            "original_never_overwritten": True,
        }
        _write_new(target / "project.json", canonical_json(manifest))
        state = {"extraction_state": "unconfirmed", "context_state": "missing", "review_state": "not_started", "read_only": False, "diagnostics": []}
        _write_new(target / "state.json", canonical_json(state))
        project = cls(target)
        try:
            document = ingest_bytes(safe_name, content, limits=limits, ocr=ocr)
        except (IngestionError, OSError, ValueError) as exc:
            diagnostic = {"schema_version": 1, "kind": "ingestion-failure", "safe": True, "message": str(exc)[:1000], "source_sha256": _sha256(content), "created_at": _now()}
            _write_new(target / "extraction" / "diagnostic.json", canonical_json(diagnostic))
            project._update_state(extraction_state="blocked", diagnostics=[diagnostic["message"]])
            project._append_event("ingestion_failed", diagnostic)
            return project
        project._save_document(document)
        project._append_event("uploaded", {"source_sha256": document.source.sha256, "parser": document.parser_name})
        return project

    def manifest(self) -> dict[str, Any]:
        return _read_json(self.manifest_path)

    def state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            raise ReviewStudioError("项目 state.json 缺失")
        return _read_json(self.state_path)

    def integrity_errors(self) -> list[str]:
        """Recheck immutable bindings before every state-changing action."""
        errors: list[str] = []
        try:
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
            document = self.document()
            if document is not None and document.source.sha256 != source.get("sha256"):
                errors.append("结构化文档未绑定当前原始文件")
            audits = self.root / "audits"
            if audits.is_dir():
                for path in audits.glob("*/*.json"):
                    if path.is_symlink():
                        errors.append(f"审查产物是符号链接：{path.name}")
                        continue
                    try:
                        value = _read_json(path)
                    except (OSError, ValueError, ReviewStudioError) as exc:
                        errors.append(f"审查产物无法读取：{path.name}: {exc}")
                        continue
                    if value.get("source_sha256") != source.get("sha256"):
                        errors.append(f"审查产物来源绑定不匹配：{path.name}")
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
        _write_new(self.document_path, canonical_json(document.to_dict()))
        _write_new(self.root / "extraction" / "quality.json", canonical_json(document.quality.to_dict()))
        _write_new(self.root / "extraction" / "warnings.json", canonical_json({"warnings": [warning.to_dict() for warning in document.warnings]}))
        _write_new(self.root / "extraction" / "source-map.json", canonical_json({"source_to_block": document.source_to_block}))

    def _update_state(self, **updates: Any) -> dict[str, Any]:
        state = self.state()
        state.update(updates)
        _atomic_write(self.state_path, canonical_json(state))
        return state

    def _append_event(self, event: str, payload: Mapping[str, Any]) -> None:
        event_record = {"event_id": stable_id("EV", self.manifest().get("project_id", ""), event, _now(), secrets.token_hex(4)), "event": event, "created_at": _now(), "payload": dict(payload)}
        path = self.root / "audit-log.jsonl"
        if path.is_symlink():
            raise ReviewStudioError("审计日志不能是符号链接")
        with path.open("ab") as handle:
            handle.write(canonical_json(event_record))

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

    def confirm_extraction(self, choice: str, *, corrected_text: str | None = None) -> dict[str, Any]:
        self._ensure_writable()
        if choice not in {"confirm", "correct", "continue_with_warning", "replace"}:
            raise ReviewStudioError("识别确认选项必须是 confirm、correct、continue_with_warning 或 replace")
        if choice == "replace":
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
            _write_new(self.root / "extraction" / "human-correction.md", corrected_bytes)
            _write_new(self.root / "extraction" / "document-corrected.json", canonical_json(corrected.to_dict()))
            _atomic_write(self.document_path, canonical_json(corrected.to_dict()))
            self._append_event("extraction_corrected", {"corrected_sha256": _sha256(corrected_bytes), "source_sha256": document.source.sha256})
            return self._update_state(extraction_state="confirmed_corrected", read_only=False)
        if choice == "confirm" and any(w.severity in {"critical", "high"} for w in document.warnings):
            raise ReviewStudioError("识别存在高风险警告；请选择带警告继续或先修正/更换文件")
        state_name = "confirmed" if choice == "confirm" else "confirmed_with_warning"
        self._append_event("extraction_confirmed", {"decision": choice, "source_sha256": document.source.sha256})
        return self._update_state(extraction_state=state_name, read_only=False)

    def confirm_context(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_writable()
        required = {"document_type", "jurisdiction", "effective_date", "publisher_type", "audience", "involves_minors", "involves_fees", "involves_sponsorship", "involves_contract", "involves_personal_information", "involves_intellectual_property", "publication_status"}
        missing = sorted(required - set(payload))
        if missing:
            raise ReviewStudioError("文档上下文缺少字段：" + ", ".join(missing))
        if not all(isinstance(payload[name], bool) for name in required if name.startswith("involves_")):
            raise ReviewStudioError("涉及范围字段必须是布尔值")
        if payload.get("publication_status") not in {"internal-draft", "external-formal"}:
            raise ReviewStudioError("publication_status 必须是 internal-draft 或 external-formal")
        context = ReviewContext(**{name: payload[name] for name in required}, confirmed=True, model_suggestion=self.suggested_document_type(), user_provided_materials=list(payload.get("user_provided_materials", [])))
        _write_new(self.root / "context.json", canonical_json(context.to_dict()))
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
        state = self.state()
        reasons: list[str] = []
        if state.get("extraction_state") not in {"confirmed", "confirmed_corrected", "confirmed_with_warning"}:
            reasons.append("识别结果尚未由用户确认")
        if state.get("context_state") != "confirmed":
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
            "source_sha256": document.source.sha256,
            "document_type": context.document_type,
            "required_finding_fields": ["finding_id", "critic", "document_type", "location", "evidence", "issue", "standard", "consequence", "severity", "verification_state", "external_basis", "uncertainties", "suggested_action", "suggested_owner", "blocks_release_or_execution"],
            "rules": {"independent": True, "do_not_vote_or_score": True, "location_must_use_block_or_page": True, "legal_screen_never_claims_counsel": True},
        }
        return "# Document Review Studio independent review\n\nReturn strict JSON only. Do not merge findings from other critics.\n\n## Contract\n```json\n" + json.dumps(contract, ensure_ascii=False, indent=2) + "\n```\n\n## Internal document blocks\n```json\n" + json.dumps([block.to_dict() for block in document.blocks], ensure_ascii=False, indent=2) + "\n```\n"

    def run_audits(self, critics: Iterable[str] | None = None) -> list[AuditRun]:
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
            _write_new(directory / f"{run.run_id}.json", canonical_json(run.to_dict()))
            _write_new(directory / f"{run.run_id}.prompt.md", self.prompt(critic).encode("utf-8"))
            self._append_event("audit_run_created", {"run_id": run.run_id, "critic": critic, "finding_ids": [f.finding_id for f in run.findings]})
            runs.append(run)
        self._update_state(review_state="completed", last_audit_at=_now())
        return runs

    def collect_model_audit(self, critic: str, response: bytes | str, *, model_label: str = "external-model") -> AuditRun:
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
        run = AuditRun(stable_id("RUN", document.source.sha256, critic, _now(), secrets.token_hex(4)), critic, document.document_id, document.source.sha256, context, findings, list(parsed.get("observations", [])) if isinstance(parsed.get("observations", []), list) else [], list(parsed.get("zero_finding_basis", [])) if isinstance(parsed.get("zero_finding_basis", []), list) else [], model_label, _now())
        directory = self.root / "audits" / critic
        _write_new(directory / f"{run.run_id}.json", canonical_json(run.to_dict()))
        _write_new(directory / f"{run.run_id}.raw-response.json.txt", raw)
        self._append_event("model_audit_collected", {"run_id": run.run_id, "critic": critic, "raw_response_sha256": _sha256(raw), "finding_ids": [finding.finding_id for finding in findings]})
        self._update_state(review_state="completed", last_audit_at=_now())
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
        rows: list[Finding] = []
        audits = self.root / "audits"
        if not audits.is_dir():
            return rows
        for path in sorted(audits.glob("*/*.json")):
            if path.name.endswith(".prompt.json") or path.is_symlink():
                continue
            try:
                value = _read_json(path)
                for item in value.get("findings", []):
                    rows.append(_finding_from_dict(item))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        decisions = self._decisions()
        return [replace(item, status=decisions.get(item.finding_id, {}).get("decision", item.status)) for item in rows]

    def _decisions(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        directory = self.root / "finding-decisions"
        if not directory.is_dir():
            return result
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink():
                continue
            value = _read_json(path)
            result[str(value.get("finding_id"))] = value
        return result

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
        record = {"schema_version": 1, "decision_id": stable_id("FD", finding_id, decision, _now(), secrets.token_hex(4)), "finding_id": finding_id, "critic": finding.critic, "decision": decision, "reason": reason, "corrected_action": corrected_action, "finding_snapshot_sha256": _sha256(canonical_json(finding.to_dict())), "created_at": _now(), "lifecycle": "append-only"}
        _write_new(self.root / "finding-decisions" / f"{record['decision_id']}.json", canonical_json(record))
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
        accepted = [item for item in findings if item.status in {"accept", "correct"}]
        if not accepted:
            raise ReviewStudioError("没有已接受的 Finding，不能准备修改桥接")
        bridge = self.root / "exports" / "revision-bridge"
        bridge.mkdir(parents=True, exist_ok=True)
        lines = ["# Document Review Studio Findings", "", f"Source SHA-256: `{document.source.sha256}`", "", "This report is a bridge into the existing constrained revision workflow. Independent critics remain separate.", ""]
        for finding in accepted:
            lines.extend([f"## {finding.finding_id} · {finding.critic}", "", f"- Location: `{finding.location.block_id}` page={finding.location.page}", f"- Evidence: {finding.evidence}", f"- Issue: {finding.issue}", f"- Standard: {finding.standard}", f"- Consequence: {finding.consequence}", f"- Suggested action: {finding.suggested_action}", f"- Human decision: {finding.status}", ""])
        report = "\n".join(lines).encode("utf-8")
        report_path = bridge / "findings-report.md"
        if not report_path.exists():
            _write_new(report_path, report)
        binding = {"schema_version": 1, "source_sha256": document.source.sha256, "source_name": document.source.original_name, "report_relative_path": str(report_path.relative_to(self.root)).replace("\\", "/"), "report_sha256": _sha256(report), "finding_ids": [item.finding_id for item in accepted], "revision_loop": "existing-argument-workbench-constrained-revision", "track_changes_claimed": False}
        binding_path = bridge / "bridge.json"
        if not binding_path.exists():
            _write_new(binding_path, canonical_json(binding))
        self._append_event("revision_bridge_prepared", binding)
        return report_path

    def export(self, *, revised_markdown: str | None = None) -> Path:
        self._ensure_writable()
        document = self.document()
        if not document:
            raise ReviewStudioError("没有可导出的结构化文档")
        export_id = stable_id("EXP", document.source.sha256, _now(), secrets.token_hex(4))
        output = self.root / "exports" / export_id
        output.mkdir(parents=True, exist_ok=False)
        draft = revised_markdown if revised_markdown is not None else model_to_markdown(document)
        _write_new(output / "draft.md", draft.encode("utf-8"))
        findings = self.findings()
        runs: list[dict[str, Any]] = []
        for path in sorted((self.root / "audits").glob("*/*.json")) if (self.root / "audits").is_dir() else []:
            try:
                runs.append(_read_json(path))
            except (OSError, ValueError, ReviewStudioError):
                continue
        audit = {"schema_version": 1, "export_id": export_id, "source": document.source.to_dict(), "parser": {"name": document.parser_name, "version": document.parser_version}, "quality": document.quality.to_dict(), "warnings": [warning.to_dict() for warning in document.warnings], "audit_runs": runs, "findings": [finding.to_dict() for finding in findings], "decisions": list(self._decisions().values()), "independent_critics": list(CRITIC_DIMENSIONS), "scores": None, "legal_boundary": "合规筛查不是律师意见；无来源材料时只能输出待核实问题", "created_at": _now()}
        _write_new(output / "audit.json", canonical_json(audit))
        _write_new(output / "quality-report.json", canonical_json({"source": document.source.to_dict(), "quality": document.quality.to_dict(), "warnings": [warning.to_dict() for warning in document.warnings]}))
        _write_new(output / "audit.md", _audit_markdown(audit).encode("utf-8"))
        if document.source.extension == ".docx":
            docx_bytes = _minimal_docx(draft)
            _write_new(output / "revised.docx", docx_bytes)
            _write_new(output / "difference-report.md", _difference_report(model_to_markdown(document), draft).encode("utf-8"))
            _write_new(output / "track-changes-capability.json", canonical_json({"native_track_changes": False, "message": "输出为新 DOCX + 逐项差异报告，未冒充 Word Track Changes"}))
        else:
            _write_new(output / "editable-draft.md", draft.encode("utf-8"))
        self._append_event("export_created", {"export_id": export_id, "relative_path": str(output.relative_to(self.root)).replace("\\", "/")})
        return output

    def view(self) -> dict[str, Any]:
        self._enforce_integrity()
        manifest = self.manifest()
        state = self.state()
        try:
            document = self.document()
        except (OSError, KeyError, TypeError, ValueError, ReviewStudioError):
            document = None
        can_review, reasons = self.can_review()
        return {"project": manifest, "state": state, "extraction": {"available": document is not None, "quality": document.quality.to_dict() if document else {}, "warnings": [warning.to_dict() for warning in document.warnings] if document else [], "blocks": [block.to_dict() for block in document.blocks[:100]] if document else []}, "context": self.context().to_dict() if self.context() else {"model_suggestion": self.suggested_document_type()}, "can_review": can_review, "review_blockers": reasons, "findings": [finding.to_dict() for finding in self.findings()]}


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
