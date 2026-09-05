"""Quickstart extracted from the public CLI facade."""

from __future__ import annotations

from .support import *  # noqa: F401,F403
from .validation import *  # noqa: F401,F403

def quickstart(args: argparse.Namespace) -> int:
    """Guide a first-time user to a manual, provider-neutral prompt bundle."""
    print("Critic Divergence Tester 快速开始")
    print("不会上传文章，也不需要 API key。按 Ctrl+C 可随时退出。")

    manuscript = getattr(args, "manuscript", None)
    if manuscript is None:
        try:
            manuscript = input("\n请粘贴文章路径（.md 或 .txt）：")
        except EOFError:
            print("\n错误：没有收到文章路径。", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("\n已取消。", file=sys.stderr)
            return EXIT_INTERRUPTED
    manuscript = _unquote_path(str(manuscript))
    if not manuscript:
        print("错误：文章路径不能为空。", file=sys.stderr)
        return 2

    source_path = Path(manuscript).expanduser().resolve()
    if not source_path.is_file():
        print(f"错误：找不到文章文件：{source_path}", file=sys.stderr)
        return 2
    try:
        source_text, _ = read_utf8(source_path)
    except UnicodeDecodeError:
        print("错误：文章不是 UTF-8 编码，请转换编码后重试。", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"错误：无法读取文章：{exc}", file=sys.stderr)
        return 2
    if not source_text.strip():
        print("错误：文章文件是空的。", file=sys.stderr)
        return 2

    track = getattr(args, "track", None)
    if track is not None and track not in ACADEMIC_TRACKS:
        print(f"错误：未知学术线：{track}", file=sys.stderr)
        return 2
    while track is None:
        print("\n请选择学术线：")
        print("  1. 文科·社会科学（历史、哲学、法学、经济学、社会学等）")
        print("  2. 理科·自然科学（实验、观察、理论与模拟）")
        print("  3. 工科·工程学（软件、产品、系统与实现）")
        try:
            choice = input("请输入 1、2 或 3（直接回车默认选 1）：").strip()
        except EOFError:
            print("\n错误：没有收到学术线选择。", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("\n已取消。", file=sys.stderr)
            return EXIT_INTERRUPTED
        track = QUICKSTART_TRACK_ALIASES.get(choice or "1")
        if track is None:
            print("无法识别，请输入 1、2、3，或学术线名称。")

    track_label = str(ACADEMIC_TRACKS[track]["label"])
    print(f"\n已选择：{track_label}")
    print("正在生成自包含审查提示……")
    run_dir = _prepare_bundle(
        argparse.Namespace(
            protocol=ACADEMIC_TRACKS[track]["primary"],
            manuscript=str(source_path),
            runs_dir=getattr(args, "runs_dir", ".critic-runs"),
            allow_test_artifact=False,
        )
    )
    prompt_path = run_dir / "prompt.md"
    print(prompt_path)
    print("完成。打开上面显示的 prompt.md，复制全部内容给你常用的 AI。")
    print("AI 回答完成后，只需运行下面命令并直接粘贴回答：")
    launcher = _python_launcher()
    print(f"{launcher} critic_runner.py resume --paste")
    return 0


def run_track(args: argparse.Namespace) -> int:
    args.protocol = ACADEMIC_TRACKS[args.track]["primary"]
    return run(args)


def positive_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive finite number")
    return value


def positive_integer(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return value


def _add_run_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("protocol", choices=PROTOCOLS)
    parser.add_argument("manuscript", help="UTF-8 manuscript path")
    parser.add_argument(
        "--runs-dir",
        default=".critic-runs",
        help="archive directory (default: .critic-runs)",
    )
    parser.add_argument(
        "--allow-test-artifact",
        action="store_true",
        help="allow critic-generic; only use this for the divergence test",
    )


def _add_track_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("track", choices=ACADEMIC_TRACKS)
    parser.add_argument("manuscript", help="UTF-8 manuscript path")
    parser.add_argument(
        "--runs-dir",
        default=".critic-runs",
        help="archive directory (default: .critic-runs)",
    )


def _add_execution_limits(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--executor-label",
        help="public reproducibility label for the model/configuration (never put secrets here)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_seconds,
        default=900.0,
        help="terminate the executor after this many seconds (default: 900)",
    )
    parser.add_argument(
        "--max-output-bytes",
        type=positive_integer,
        default=DEFAULT_MAX_OUTPUT_BYTES,
        help="terminate after this many combined stdout/stderr bytes (default: 16777216)",
    )

__all__ = [name for name in globals() if not name.startswith("__")]
