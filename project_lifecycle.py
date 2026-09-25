"""Local project backup, compatibility and recoverable multi-file mutations.

Only the outer mutation owns the journal. Existing files are copied before
their first overwrite; newly created files are removed on recovery. A commit
marker makes recovery idempotent even if cleanup is interrupted.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from project_lock import project_mutation_lock

APP_VERSION = "0.2.3"
PROJECT_SCHEMA = 1
JOURNAL = ".recovery"
MAX_BACKUP_BYTES = 1024 * 1024 * 1024
MAX_BACKUP_FILES = 50000
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_active = threading.local()


class CommitValidationResult(ValueError):
    """A completed rejection archive, deliberately committed before reporting it.

    Raise only after writing a complete, independently valid rejection record,
    and before attempting any accepted-result mutation. Ordinary validation and
    programming exceptions must always roll back their enclosing transaction.
    """


def _root(path):
    candidate = Path(path)
    if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
        raise ValueError("拒绝链接项目目录")
    return candidate.resolve()


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _child(root, name):
    if not isinstance(name, str) or not name or re.search(r'[\x00-\x1f<>:"\\|?*]', name):
        raise ValueError("备份/恢复路径无效")
    parts = PurePosixPath(name).parts
    if PurePosixPath(name).as_posix() != name:
        raise ValueError("备份/恢复路径必须使用唯一规范形式")
    if name.startswith("/") or any(p in {"", ".", ".."} or p.endswith((".", " ")) for p in parts):
        raise ValueError("备份/恢复路径越界")
    if any(re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p) for p in parts):
        raise ValueError("备份包含保留文件名")
    target = root.joinpath(*parts)
    for path in (target, *target.parents):
        if path == root.parent:
            break
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("拒绝备份或恢复链接路径")
    if root.resolve() not in target.resolve().parents:
        raise ValueError("备份/恢复路径越界")
    return target


def _project_child(root, name):
    target = _child(root, name)
    parts = PurePosixPath(name).parts
    if parts[0].casefold() == JOURNAL or any(part.casefold() == ".mutation.lock" for part in parts):
        raise ValueError("备份或恢复清单含运行时记录")
    return target


def _inventory(root, *, runtime_excluded=True):
    """Validate directories before descending, including Windows junctions."""
    files, directories = {}, set()
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            relative = path.relative_to(root).as_posix()
            if runtime_excluded and (relative.split("/")[0].casefold() == JOURNAL or path.name.casefold() == ".mutation.lock"):
                continue
            _child(root, relative)
            if path.is_dir():
                directories.add(relative)
                pending.append(path)
            elif path.is_file():
                files[relative] = path
            else:
                raise ValueError("项目含非常规文件，拒绝备份或恢复")
    return files, directories


def _files(root):
    return _inventory(root)[0]


def _load_manifest(data):
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("清单含重复字段")
            value[key] = item
        return value

    try:
        value = json.loads(data, object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("备份或恢复清单不是有效 JSON") from exc
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("不支持或已损坏的备份/恢复格式")
    return value


def _hash_entry(entry):
    return isinstance(entry, dict) and isinstance(entry.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])


def _remove_journal(root):
    journal = _child(root, JOURNAL)
    _inventory(journal, runtime_excluded=False)
    shutil.rmtree(journal)


def compatibility(root):
    value = json.loads(_child(_root(root), "project.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("项目元数据必须是 JSON 对象")
    version = value.get("schema_version")
    if type(version) is not int or version != PROJECT_SCHEMA:
        raise ValueError(f"此项目格式版本为 {version}，当前程序支持 {PROJECT_SCHEMA}；请使用兼容版本，原项目未改动")
    return {"app_version": APP_VERSION, "schema_version": version, "migration_required": False}


def before_write(path):
    """Called before a domain atomic write, with its project lock held."""
    transactions = getattr(_active, "transactions", {})
    path = Path(path).absolute()
    for root, manifest in transactions.items():
        if root not in path.parents:
            continue
        relative = path.relative_to(root).as_posix()
        _project_child(root, relative)
        if relative not in manifest["files"] or relative in manifest["undo"]:
            return
        data = path.read_bytes()
        name = str(len(manifest["undo"]))
        _atomic(root / JOURNAL / "before" / name, data)
        undo = {**manifest["undo"], relative: {"name": name, "sha256": hashlib.sha256(data).hexdigest()}}
        # A failed publication must not make a later retry believe its before
        # image is durable. Publish first; only then update the in-memory state.
        _atomic(root / JOURNAL / "manifest.json", _json({**manifest, "undo": undo}))
        manifest["undo"] = undo
        return


def recover(root):
    """Roll back an interrupted mutation under the project's exclusive lock."""
    root = _root(root)
    if root in getattr(_active, "transactions", {}):
        return False
    with project_mutation_lock(root):
        journal = _child(root, JOURNAL)
        if not journal.exists():
            return False
        _inventory(journal, runtime_excluded=False)
        committed = _child(journal, "committed")
        if committed.exists():
            if not committed.is_file() or committed.read_bytes() not in {b"committed\n", b"validation-result\n", b"rolled-back\n"}:
                raise ValueError("恢复提交记录损坏；请保留项目并使用备份")
            _remove_journal(root)
            return False
        manifest_path = _child(journal, "manifest.json")
        if not manifest_path.exists():
            # No writes are allowed until the manifest is durably published.
            _remove_journal(root)
            return False
        if not manifest_path.is_file():
            raise ValueError("恢复记录不是普通文件")
        manifest = _load_manifest(manifest_path.read_bytes())
        if not isinstance(manifest.get("files"), list) or not isinstance(manifest.get("undo"), dict):
            raise ValueError("恢复记录损坏；请从已验证备份恢复到新项目")
        for relative in manifest["files"]:
            _project_child(root, relative)
        original = set(manifest["files"])
        if len({name.casefold() for name in original}) != len(manifest["files"]):
            raise ValueError("恢复记录包含重复文件")
        original_directories = manifest.get("directories")
        if original_directories is not None:
            if not isinstance(original_directories, list):
                raise ValueError("恢复目录记录损坏")
            for relative in original_directories:
                _project_child(root, relative)
            original_directories = set(original_directories)
        restored = []
        for relative, entry in manifest["undo"].items():
            if relative not in original or not _hash_entry(entry) or not isinstance(entry.get("name"), str) or not re.fullmatch(r"0|[1-9][0-9]*", entry["name"]):
                raise ValueError("恢复记录内容无效")
            undo_path = _child(journal, "before/" + entry["name"])
            if not undo_path.is_file():
                raise ValueError("恢复原件缺失；请保留项目并使用备份")
            data = undo_path.read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("恢复记录校验失败；请保留项目并使用备份")
            restored.append((_child(root, relative), data))
        # Validate every recovery input before changing a project file.
        current, directories = _inventory(root)
        for path, data in restored:
            _atomic(path, data)
        for relative, path in current.items():
            if relative not in original:
                path.unlink()
        # Legacy journals did not record empty directories, so retain them.
        if original_directories is not None:
            for relative in sorted(directories - original_directories, key=lambda p: len(PurePosixPath(p).parts), reverse=True):
                try:
                    _child(root, relative).rmdir()
                except OSError:
                    pass
        _atomic(journal / "committed", b"rolled-back\n")
        _remove_journal(root)
        return True


@contextmanager
def transaction(root):
    root = _root(root)
    transactions = getattr(_active, "transactions", None)
    if transactions is None:
        transactions = _active.transactions = {}
    if root in transactions:
        try:
            yield
        except CommitValidationResult:
            raise
        except BaseException:
            transactions[root]["rollback_only"] = True
            raise
        return
    with project_mutation_lock(root):
        if (root / ".deleting").exists():
            raise ValueError("项目已进入删除流程，请从删除前备份恢复")
        recover(root)
        files, directories = _inventory(root)
        manifest = {"version": 1, "files": sorted(files), "directories": sorted(directories), "undo": {}}
        _atomic(root / JOURNAL / "manifest.json", _json(manifest))
        transactions[root] = manifest
        try:
            try:
                yield
            except CommitValidationResult:
                if manifest.get("rollback_only"):
                    raise ValueError("嵌套操作失败，已回滚本次全部修改")
                _atomic(root / JOURNAL / "committed", b"validation-result\n")
                raise
            if manifest.get("rollback_only"):
                raise ValueError("嵌套操作失败，已回滚本次全部修改")
            _atomic(root / JOURNAL / "committed", b"committed\n")
        except CommitValidationResult:
            raise
        except BaseException:
            transactions.pop(root, None)
            recover(root)
            raise
        finally:
            transactions.pop(root, None)
            if (root / JOURNAL / "committed").is_file():
                _remove_journal(root)


def create_backup(root, destination):
    from document_review_studio import DocumentReviewProject
    root, destination = _root(root), Path(destination).absolute()
    if destination.is_symlink():
        raise ValueError("备份目标不能是链接")
    destination = destination.resolve()
    if destination == root or root in destination.parents or destination.exists():
        raise ValueError("备份必须保存到项目外的新文件，不覆盖已有文件")
    with project_mutation_lock(root):
        if root in getattr(_active, "transactions", {}):
            raise ValueError("请在本次修改完成后创建备份，不能备份尚未提交的项目状态")
        recover(root)
        compatibility(root)
        errors = DocumentReviewProject(root).integrity_errors()
        if errors:
            raise ValueError("项目未通过完整性验证，拒绝生成可信备份：" + "; ".join(errors[:3]))
        entries = _files(root)
        if len(entries) > MAX_BACKUP_FILES or sum(p.stat().st_size for p in entries.values()) > MAX_BACKUP_BYTES:
            raise ValueError("项目超过备份容量限制")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
        manifest = {"version": 1, "app_version": APP_VERSION, "project_name": root.name, "files": {}}
        try:
            with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
                for relative, path in sorted(entries.items()):
                    data = path.read_bytes()
                    archive.writestr("project/" + relative, data)
                    manifest["files"][relative] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                manifest_data = _json(manifest)
                if len(manifest_data) > MAX_MANIFEST_BYTES:
                    raise ValueError("备份清单过大，请减少项目文件数量或文件名长度")
                archive.writestr("backup.json", manifest_data)
            with temp.open("r+b") as stream:
                os.fsync(stream.fileno())
            # Atomic create without a race that could overwrite another backup.
            os.link(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
        return destination


def restore_backup(archive_path, library):
    from document_review_studio import DocumentReviewProject
    library = _root(library)
    library.mkdir(parents=True, exist_ok=True)
    staging = library / (".restore-" + uuid.uuid4().hex)
    with project_mutation_lock(library):
        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                if len(infos) > MAX_BACKUP_FILES + 1 or len(set(n.casefold() for n in names)) != len(names):
                    raise ValueError("备份条目重复或数量超限")
                if sum(i.file_size for i in infos if i.filename != "backup.json") > MAX_BACKUP_BYTES or any(stat.S_ISLNK(i.external_attr >> 16) or i.flag_bits & 1 for i in infos):
                    raise ValueError("备份体积超限、包含链接或加密条目")
                info = archive.getinfo("backup.json")
                if info.file_size > MAX_MANIFEST_BYTES:
                    raise ValueError("备份清单过大")
                manifest = _load_manifest(archive.read(info))
                if not isinstance(manifest.get("files"), dict):
                    raise ValueError("不支持的备份格式")
                expected = {"backup.json", *("project/" + n for n in manifest["files"])}
                if set(names) != expected:
                    raise ValueError("备份文件与清单不一致")
                staging.mkdir()
                for relative, entry in manifest["files"].items():
                    target = _project_child(staging, relative)
                    if not _hash_entry(entry) or type(entry.get("bytes")) is not int or entry["bytes"] < 0:
                        raise ValueError("备份文件清单字段无效")
                    if archive.getinfo("project/" + relative).file_size != entry["bytes"]:
                        raise ValueError("备份校验失败：文件长度与清单不符")
                    data = archive.read("project/" + relative)
                    if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                        raise ValueError("备份校验失败：" + relative)
                    _atomic(target, data)
            compatibility(staging)
            errors = DocumentReviewProject(staging).integrity_errors()
            if errors:
                raise ValueError("恢复后的项目未通过完整性验证：" + "; ".join(errors[:3]))
            name = manifest.get("project_name", "restored.document-review-studio")
            if not isinstance(name, str) or not name.endswith(".document-review-studio") or "/" in name or "\\" in name:
                raise ValueError("备份项目名称无效")
            for attempt in range(10):
                candidate = name if attempt == 0 else name.removesuffix(".document-review-studio") + "-restored-" + uuid.uuid4().hex + ".document-review-studio"
                target = _child(library, candidate)
                if target.exists():
                    continue
                try:
                    # Windows rename refuses an existing target, including a
                    # directory another process creates after the check above.
                    os.rename(staging, target)
                except FileExistsError:
                    continue
                return target
            raise ValueError("恢复目标名称连续冲突，请稍后重试")
        except (KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise ValueError("备份格式无效：请选择工作台生成的完整项目备份 ZIP，审查导出包不是恢复备份") from exc
        finally:
            if staging.is_dir() and not staging.is_symlink() and staging.parent == library:
                shutil.rmtree(staging)
