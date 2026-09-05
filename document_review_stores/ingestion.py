"""Document Review project responsibility store."""

from __future__ import annotations

from .base import *  # noqa: F401,F403

class IngestionState(_ProjectComponent):
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
        profile_fields = {}
        for name, default, choices in (("review_profile", "document", PROFILES), ("discipline", "general", DISCIPLINES), ("research_type", "unspecified", RESEARCH_TYPES)):
            value = payload.get(name, default)
            if not isinstance(value, str) or value not in choices:
                raise ReviewStudioError(f"{name} 无效")
            profile_fields[name] = value
        context = ReviewContext(**{name: payload[name] for name in required}, **profile_fields, confirmed=True, model_suggestion=self.suggested_document_type(), user_provided_materials=list(materials))
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
            "review_profile": "document", "discipline": "general", "research_type": "unspecified",
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

__all__ = [name for name in globals() if not name.startswith("__")]
