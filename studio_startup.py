"""Small visible error boundary for double-click launches."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import traceback


def report_startup_failure(error: Exception) -> None:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "DocumentReviewStudio" / "diagnostics"
    log = base / "startup-error.json"
    record = {"created_at": datetime.now(timezone.utc).isoformat(), "exception_type": type(error).__name__,
              "frames": [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                         for frame in traceback.extract_tb(error.__traceback__)]}
    try:
        base.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        detail = f"诊断位置：{log}"
    except OSError:
        detail = "诊断文件无法保存。请从命令行启动以查看错误。"
    message = "工作台启动失败。请重新解压完整安装包后重试。\n" + detail
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "文书与学术工作台", 0x10)
    else:
        print(message, file=sys.stderr)
