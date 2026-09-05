"""Document Review project responsibility store."""

from __future__ import annotations

from .base import *  # noqa: F401,F403

class AuditRunStore(_ProjectComponent):
    def review_critics(self) -> tuple[str, ...]:
        context = self.context()
        return profile_critics(context.review_profile if context else "document")

    def _selected_critics(self, critics: Iterable[str] | None) -> list[str]:
        selected = list(self.review_critics() if critics is None else critics)
        if not selected or any(critic not in self.review_critics() for critic in selected):
            raise ReviewStudioError("请选择当前审查类型中的至少一个维度")
        return list(dict.fromkeys(selected))

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
        selected = self._selected_critics(critics)
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
        selected = self._selected_critics(critics)
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
        if critic.startswith("academic_"):
            findings.extend(self._finding(critic, document, context, block, **details) for block, details in academic_prechecks(critic, document, context))
            observations.append("仅执行离线文本线索检查；未验证论证有效性、研究结果或来源真实性。引用编号仅支持单项数字标记，作者—年份、范围引用和脚注仍需独立核验。")
            if not findings:
                zero_basis.append("本地规则未触发提示，不构成学术质量或引用真实性通过；请继续独立 AI 审查和人工核验。")
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

__all__ = [name for name in globals() if not name.startswith("__")]
