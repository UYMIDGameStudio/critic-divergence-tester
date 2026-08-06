#!/usr/bin/env python3
"""Provider-neutral runner for the critic protocols in this repository.

The runner deliberately knows nothing about Claude Code, OpenAI, Anthropic, or
any other model provider. It can either prepare a self-contained prompt bundle
for manual use, or feed that bundle to an arbitrary command over UTF-8 stdin.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent

PROTOCOLS = {
    "critic-individualist": ROOT / "critic-individualist.md",
    "critic-contrastivist": ROOT / "critic-contrastivist.md",
    "citation-auditor": ROOT / "citation-auditor.md",
    "critic-generic": ROOT / "test" / "critic-generic.md",
}

TEST_ONLY = {"critic-generic"}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strip_frontmatter(text: str) -> str:
    """Remove Claude-Code-style YAML metadata while keeping the prompt body."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text.strip()
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :]).strip()
    raise ValueError("unterminated YAML frontmatter")


def load_protocol(name: str, allow_test_artifact: bool = False) -> tuple[str, str]:
    if name not in PROTOCOLS:
        raise ValueError(f"unknown protocol: {name}")
    if name in TEST_ONLY and not allow_test_artifact:
        raise ValueError(
            f"{name} is a test artifact; pass --allow-test-artifact only for divergence testing"
        )
    raw = PROTOCOLS[name].read_text(encoding="utf-8")
    return strip_frontmatter(raw), raw


def build_prompt(protocol: str, manuscript: str, source_name: str) -> str:
    return (
        "# 审查协议\n\n"
        f"{protocol}\n\n"
        "# 本次任务\n\n"
        f"只审查下面的稿件 `{source_name}`。不要修改稿件，也不要读取或假定存在其他审查报告。\n\n"
        "# 稿件\n\n"
        f"{manuscript.rstrip()}\n"
    )


def new_run_dir(runs_dir: Path, protocol_name: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target = runs_dir / f"{stamp}--{protocol_name}"
    target.mkdir(parents=True, exist_ok=False)
    return target


def executor_metadata(executor: list[str] | None) -> dict[str, object] | None:
    """Record useful executor identity without persisting possibly secret arguments."""
    if not executor:
        return None
    return {
        "command": Path(executor[0]).name,
        "argument_count": max(0, len(executor) - 1),
    }


def write_run(
    *,
    run_dir: Path,
    protocol_name: str,
    source_path: Path,
    source_text: str,
    protocol_raw: str,
    prompt: str,
    report: str | None = None,
    executor: list[str] | None = None,
    returncode: int | None = None,
) -> None:
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    if report is not None:
        (run_dir / "report.md").write_text(report, encoding="utf-8")

    manifest = {
        "protocol": protocol_name,
        "source": str(source_path),
        "source_sha256": sha256_text(source_text),
        "protocol_sha256": sha256_text(protocol_raw),
        "prompt_sha256": sha256_text(prompt),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "executor": executor_metadata(executor),
        "returncode": returncode,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def prepare(args: argparse.Namespace) -> int:
    source_path = Path(args.manuscript).resolve()
    source_text = source_path.read_text(encoding="utf-8")
    protocol, protocol_raw = load_protocol(args.protocol, args.allow_test_artifact)
    prompt = build_prompt(protocol, source_text, source_path.name)

    run_dir = new_run_dir(Path(args.runs_dir), args.protocol)
    write_run(
        run_dir=run_dir,
        protocol_name=args.protocol,
        source_path=source_path,
        source_text=source_text,
        protocol_raw=protocol_raw,
        prompt=prompt,
    )
    print(run_dir / "prompt.md")
    return 0


def run(args: argparse.Namespace) -> int:
    if not args.executor:
        raise ValueError("run requires an executor command after --")

    executor = list(args.executor)
    if executor and executor[0] == "--":
        executor = executor[1:]
    if not executor:
        raise ValueError("run requires an executor command after --")

    source_path = Path(args.manuscript).resolve()
    source_text = source_path.read_text(encoding="utf-8")
    protocol, protocol_raw = load_protocol(args.protocol, args.allow_test_artifact)
    prompt = build_prompt(protocol, source_text, source_path.name)
    run_dir = new_run_dir(Path(args.runs_dir), args.protocol)

    # Archive inputs before starting the executor so an interruption cannot leave
    # an empty run directory with no record of what was sent.
    write_run(
        run_dir=run_dir,
        protocol_name=args.protocol,
        source_path=source_path,
        source_text=source_text,
        protocol_raw=protocol_raw,
        prompt=prompt,
        executor=executor,
    )

    try:
        completed = subprocess.run(
            executor,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        (run_dir / "stderr.log").write_text(str(exc) + "\n", encoding="utf-8")
        print(f"error: executor failed to start; details archived in {run_dir}", file=sys.stderr)
        return 2

    write_run(
        run_dir=run_dir,
        protocol_name=args.protocol,
        source_path=source_path,
        source_text=source_text,
        protocol_raw=protocol_raw,
        prompt=prompt,
        report=completed.stdout,
        executor=executor,
        returncode=completed.returncode,
    )
    if completed.stderr:
        (run_dir / "stderr.log").write_text(completed.stderr, encoding="utf-8")

    print(run_dir / "report.md")
    return completed.returncode


def list_protocols(_: argparse.Namespace) -> int:
    for name in PROTOCOLS:
        suffix = " [test-only]" if name in TEST_ONLY else ""
        print(f"{name}{suffix}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run critic protocols without depending on Claude Code."
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list available protocols").set_defaults(func=list_protocols)

    for command, help_text, func in (
        ("prepare", "archive a self-contained prompt for manual use", prepare),
        ("run", "run one protocol through an external stdin/stdout command", run),
    ):
        sp = sub.add_parser(command, help=help_text)
        sp.add_argument("protocol", choices=PROTOCOLS)
        sp.add_argument("manuscript", help="UTF-8 manuscript path")
        sp.add_argument(
            "--runs-dir",
            default=".critic-runs",
            help="archive directory (default: .critic-runs)",
        )
        sp.add_argument(
            "--allow-test-artifact",
            action="store_true",
            help="allow critic-generic; only use this for the divergence test",
        )
        sp.set_defaults(func=func)
    return p


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    executor: list[str] = []
    if raw_argv and raw_argv[0] == "run" and "--" in raw_argv:
        separator = raw_argv.index("--")
        executor = raw_argv[separator + 1 :]
        raw_argv = raw_argv[:separator]

    args = parser().parse_args(raw_argv)
    if args.command == "run":
        args.executor = executor
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
