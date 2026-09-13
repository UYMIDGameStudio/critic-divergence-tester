"""Versioned, document-scoped UI drafts; drafts never authorize a review change."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from project_lifecycle import _atomic
from project_lock import project_mutation_lock

MAX_DRAFT_BYTES = 2 * 1024 * 1024
MAX_FIELDS = 4000


def read_ui_language(directory) -> str | None:
    path = directory / ".ui-preferences.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        language = value.get("language") if isinstance(value, dict) else None
        return language if isinstance(language, str) and language in {"zh-Hant", "en"} else None
    except (OSError, ValueError):
        return None


def save_ui_language(directory, language) -> None:
    if language not in ("zh-Hant", "en"):
        raise ValueError("Invalid interface language")
    with project_mutation_lock(directory):
        path = directory / ".ui-preferences.json"
        if path.is_symlink():
            raise ValueError("Preferences cannot be a symbolic link")
        _atomic(path, json.dumps({"version": 1, "language": language}).encode("utf-8"))


class UIStateConflict(ValueError):
    """A stale page must retain its local input instead of overwriting a draft."""


def document_scope(project) -> str:
    """Bind to the effective document, including corrected and follow-up copies."""
    if project.document_path.is_file():
        path, _ = project._review_document_record()
        content = path.read_bytes()
    else:
        content = json.dumps(project.manifest()["source"], sort_keys=True).encode()
    return hashlib.sha256(content).hexdigest()


def _fields(value: Any) -> dict[str, str | bool]:
    if not isinstance(value, dict) or len(value) > MAX_FIELDS:
        raise ValueError("表单草稿字段过多或格式无效")
    for key, item in value.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,320}", key)
                or key in {"__proto__", "constructor", "prototype"}
                or not isinstance(item, (str, bool))):
            raise ValueError("表单草稿字段无效")
    return value


def _scroll(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 10_000_000:
        raise ValueError("草稿滚动位置无效")
    return value


def _read(project) -> dict[str, Any]:
    path = project.root / ".ui-draft.json"
    if path.is_symlink():
        raise ValueError("草稿不能是符号链接")
    if not path.exists():
        return {"version": 2, "revision": 0, "documents": {}}
    if path.stat().st_size > MAX_DRAFT_BYTES:
        raise ValueError("已保存草稿过大；原文件已保留，请先导出或整理")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("已保存草稿格式无效；原文件已保留")
    # Preserve old fields for explicit recovery, never inject them into a new task.
    if "version" not in value and "fields" in value:
        return {"version": 2, "revision": 0, "documents": {}, "legacy": _fields(value["fields"])}
    if (value.get("version") != 2 or type(value.get("revision")) is not int
            or value["revision"] < 0 or not isinstance(value.get("documents"), dict)):
        raise ValueError("草稿版本或结构无法识别；原文件已保留")
    for scope, record in value["documents"].items():
        if not isinstance(scope, str) or not re.fullmatch(r"[a-f0-9]{64}", scope) or not isinstance(record, dict):
            raise ValueError("草稿文档绑定无效")
        _fields(record.get("fields"))
        _scroll(record.get("scroll"))
    if "legacy" in value:
        _fields(value["legacy"])
    return value


def read_ui_draft(project) -> dict[str, Any]:
    scope = None
    try:
        scope = document_scope(project)
        value = _read(project)
    except (OSError, ValueError) as exc:
        return {"version": 2, "scope": scope, "revision": 0, "fields": {},
                "scroll": 0, "error": str(exc)}
    return {"version": 2, "scope": scope, "revision": value["revision"],
            **value["documents"].get(scope, {"fields": {}, "scroll": 0}),
            **({"legacy": value["legacy"]} if value.get("legacy") else {})}


def save_ui_draft(project, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("project_directory") != project.root.name:
        raise ValueError("草稿所属项目已变化，拒绝写入其他项目")
    fields, scroll = _fields(payload.get("fields")), _scroll(payload.get("scroll", 0))
    if type(payload.get("revision")) is not int:
        raise ValueError("草稿缺少版本，请重新打开页面")
    with project_mutation_lock(project.root):
        project._ensure_writable()
        scope = document_scope(project)
        if payload.get("scope") != scope:
            raise UIStateConflict("文档已变化，当前输入仍保留在页面；请复制后重新打开项目")
        value = _read(project)
        if payload["revision"] != value["revision"]:
            raise UIStateConflict("另一页面已保存新草稿；当前输入仍保留，请先下载当前草稿，再重新打开页面")
        value["documents"][scope] = {"fields": fields, "scroll": scroll}
        value["revision"] += 1
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_DRAFT_BYTES:
            raise ValueError("草稿累计超过 2 MiB，请先下载并整理，已保存内容保持不变")
        _atomic(project.root / ".ui-draft.json", encoded)
    return {"saved": True, "revision": value["revision"], "scope": scope}
