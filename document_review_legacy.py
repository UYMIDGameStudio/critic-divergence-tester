"""Legacy Office import through an optional local LibreOffice installation.

Conversion uses a private profile and staging directory, never the user's open
Office session. See https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html
"""

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from document_review_model import ExtractionWarning, stable_id


def find_libreoffice() -> str | None:
    for name in ("soffice.com", "soffice", "libreoffice", "soffice.exe"):
        found = shutil.which(name)
        if found:
            # The console launcher waits for headless conversion. A PATH entry
            # may expose only the .exe even when its .com sibling is installed.
            console = Path(found).with_name("soffice.com")
            if Path(found).suffix.casefold() == ".exe" and console.is_file():
                return str(console)
            return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            for name in ("soffice.com", "soffice.exe"):
                path = Path(base) / "LibreOffice/program" / name
                if path.is_file():
                    return str(path)
    if sys.platform == "darwin":
        for base in (Path("/Applications"), Path.home() / "Applications"):
            path = base / "LibreOffice.app/Contents/MacOS/soffice"
            if path.is_file():
                return str(path)
    return None


def _converter_version(executable: str) -> str | None:
    """Record an observed version; successful conversion never invents one."""
    try:
        result = subprocess.run([executable, "--version"], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=5,
                                check=False, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = result.stdout.splitlines()
    version = lines[0].strip() if lines else ""
    if result.returncode != 0 or not version.startswith("LibreOffice ") or len(version) > 512:
        return None
    return version


def _bounded_reap(process, *, timeout=5) -> bool:
    """Never enter Popen.__exit__, whose implicit wait has no time limit."""
    try:
        process.wait(timeout=timeout)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _stop_conversion(process, windows_job) -> None:
    """Release only the job/session created for this one conversion."""
    from critic_execution import _close_windows_handle
    if windows_job is not None:
        try:
            _close_windows_handle(windows_job)
        except OSError:
            pass
    elif os.name == "nt" and process.poll() is None:
        # Older/restricted Windows environments can refuse job assignment.
        # Even taskkill's own failure must not create another unbounded wait.
        killer = None
        try:
            killer = subprocess.Popen(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            if not _bounded_reap(killer):
                killer.kill()
        except OSError:
            pass
        finally:
            if killer is not None:
                _bounded_reap(killer, timeout=1)
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    _bounded_reap(process)


def _run_conversion(command, *, timeout_seconds=60) -> int:
    from critic_execution import _create_windows_kill_job, _resume_windows_process
    from document_review_ingest import IngestionError
    # Assign the suspended Windows launcher before it can spawn soffice.bin;
    # closing this private job also removes descendants after normal exit.
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
             | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)) if os.name == "nt" else 0
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=flags, start_new_session=os.name != "nt")
    windows_job = None
    try:
        if os.name == "nt":
            windows_job = _create_windows_kill_job(process)
            _resume_windows_process(process)
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        raise IngestionError("旧格式转换超时，请先在 Office 中另存为现代格式") from exc
    finally:
        _stop_conversion(process, windows_job)


def _convert(executable: str, data: bytes, suffix: str, target_suffix: str, limits) -> bytes:
    from document_review_ingest import IngestionError
    # If an OS-level kill is refused, a locked temporary file must not hide the
    # bounded timeout result. Temporary staging never replaces the bound source.
    with tempfile.TemporaryDirectory(prefix="studio-office-", ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        source, output, profile = root / ("input" + suffix), root / "output", root / "profile"
        source.write_bytes(data)
        output.mkdir()
        (profile / "user").mkdir(parents=True)
        (profile / "user/registrymodifications.xcu").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '</item></oor:items>', encoding="utf-8")
        command = [executable, "-env:UserInstallation=" + profile.as_uri(), "--headless",
                   "--nologo", "--nodefault", "--norestore", "--convert-to", target_suffix.lstrip("."),
                   "--outdir", str(output), str(source)]
        result = _run_conversion(command)
        target = output / ("input" + target_suffix)
        if result != 0 or not target.is_file() or target.is_symlink():
            raise IngestionError("旧格式转换失败；文件可能损坏、加密或属于不支持的 WPS 变体。请另存为 DOCX/XLSX/PPTX 后导入")
        if target.stat().st_size > limits.max_file_bytes:
            raise IngestionError("转换后的文件超过导入大小上限")
        return target.read_bytes()


def parse_legacy(data, source, limits, *, encoding=None):
    from document_review_ingest import MEDIA_TYPES, ParserUnavailable, _parse_docx, _zip_safety
    from document_review_office_formats import parse_office
    from document_review_text_formats import parse_text_format
    prefix = data[:512].lstrip().lower()
    conversion_receipt = None
    # Office sometimes saves HTML/RTF or OOXML under legacy suffixes.
    if prefix.startswith(b"{\\rtf"):
        document = parse_text_format(data, replace(source, extension=".rtf"), limits, encoding=encoding)
    elif prefix.startswith((b"<!doctype html", b"<html")):
        document = parse_text_format(data, replace(source, extension=".html"), limits, encoding=encoding)
    else:
        target_suffix = {".doc": ".docx", ".wps": ".docx", ".xls": ".xlsx", ".et": ".xlsx",
                         ".ppt": ".pptx", ".dps": ".pptx"}[source.extension]
        if data.startswith(b"PK"):
            archive = _zip_safety(data, limits)
            required = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}[target_suffix]
            if required not in archive.namelist():
                raise ParserUnavailable("文件内部格式与扩展名不符，请另存为正确的 Office 格式")
            converted = data
        else:
            executable = find_libreoffice()
            if not executable:
                raise ParserUnavailable("此旧版 Office/WPS 文件需要本机 LibreOffice 转换组件。安装 LibreOffice 后点击重新识别，或先另存为 DOCX、XLSX、PPTX；原文件已保留")
            converted = _convert(executable, data, source.extension, target_suffix, limits)
            version = _converter_version(executable)
            conversion_receipt = {
                "schema_version": 1,
                "algorithm": "libreoffice-headless-ooxml-v1",
                "converter": {"name": "LibreOffice", "version": version, "version_observed": version is not None},
                "input": {"extension": source.extension, "byte_size": source.byte_size, "sha256": source.sha256},
                "output": {"extension": target_suffix, "byte_size": len(converted), "sha256": hashlib.sha256(converted).hexdigest()},
                "profile": "isolated-temporary-profile",
                "block_identity": {"algorithm": "original-source-sha256-modern-parser-v1", "source_sha256": source.sha256},
            }
        # The modern parser must verify the bytes it actually receives. Original
        # binary bytes and converted OOXML are separate immutable identities.
        rebound = replace(source, extension=target_suffix, media_type=MEDIA_TYPES[target_suffix],
                          byte_size=len(converted), sha256=hashlib.sha256(converted).hexdigest())
        document = (_parse_docx(converted, rebound, limits, identity_sha256=source.sha256) if target_suffix == ".docx"
                    else parse_office(converted, rebound, limits, identity_sha256=source.sha256))
    document.source = source
    document.document_id = stable_id("DOC", source.sha256)
    document.metadata["original_format"] = source.extension
    if conversion_receipt is not None:
        document.metadata["legacy_conversion"] = conversion_receipt
    document.warnings.append(ExtractionWarning("legacy-format-extraction", "medium",
        "此文件按内部格式或转换后的正文抽取；原件保留，请核对文本顺序、表格和格式。导出提供规范化审查副本"))
    return document
