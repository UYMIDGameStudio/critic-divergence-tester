"""Document Review project responsibility store."""

from __future__ import annotations

from .base import *  # noqa: F401,F403

class ExportCenter(_ProjectComponent):
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

__all__ = [name for name in globals() if not name.startswith("__")]
