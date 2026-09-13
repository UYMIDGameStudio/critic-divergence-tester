"""Double-click entry point; explicit arguments retain the complete CLI."""
import sys


def main():
    interactive_launch = len(sys.argv) == 1
    if sys.platform == "win32" and interactive_launch:
        import ctypes
        processes = (ctypes.c_ulong * 2)()
        # Hide only a console owned exclusively by this double-click launch.
        if ctypes.windll.kernel32.GetConsoleProcessList(processes, 2) == 1:
            ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
    try:
        result = _run()
        if interactive_launch and result:
            raise RuntimeError("Application returned a failure status")
        return result
    except Exception as error:
        if not interactive_launch:
            raise
        from studio_startup import report_startup_failure
        report_startup_failure(error)
        return 1


def _run():
    from project_lifecycle import APP_VERSION
    if sys.argv[1:] == ["--version"]:
        print(APP_VERSION)
        return 0
    if sys.argv[1:] == ["--self-test"]:
        import json
        from studio_selftest import run_self_test
        print(json.dumps(run_self_test(), ensure_ascii=False))
        return 0
    from critic_runner import main as run
    return run(sys.argv[1:] or ["app"])


if __name__ == "__main__":
    raise SystemExit(main())
