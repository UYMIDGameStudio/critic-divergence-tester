"""Document Review project responsibility store."""

from __future__ import annotations

from .base import *  # noqa: F401,F403

class RevisionPlanBuilder(_ProjectComponent):
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
        try:
            recheck_runs = [self._deterministic_audit(critic, revised_document, context).to_dict() for critic in local_critics]
        except Exception:
            if output.exists() and output.is_dir() and not output.is_symlink() and not (output / "revision.json").is_file():
                shutil.rmtree(output)
            raise
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
                    if original.critic.startswith("academic_"):
                        basis += "；仅表示本地文本线索不再触发，不代表论证成立、方法有效或引用真实；这些仍需独立审查与人工核验"
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

__all__ = [name for name in globals() if not name.startswith("__")]
