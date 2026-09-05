"""Scoring Commands extracted from the public CLI facade."""

from __future__ import annotations

from .support import *  # noqa: F401,F403

def init_scorecard_command(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    atomic_write_text(output, json.dumps(scorecard_template(), indent=2) + "\n")
    print(output)
    return 0


def score_command(args: argparse.Namespace) -> int:
    scorecard_path = Path(args.scorecard).resolve()
    try:
        if Path(args.scorecard).is_symlink():
            raise ScorecardError("scorecard path must not be a symbolic link")
        scorecard = parse_json(scorecard_path.read_text(encoding="utf-8"))
        result = score_divergence(scorecard)
        provenance_errors = verify_scorecard_provenance(scorecard_path, scorecard)
        if provenance_errors:
            raise ScorecardError("; ".join(provenance_errors))
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateJsonKeyError,
        ScorecardError,
    ) as exc:
        print(f"scorecard error: {exc}", file=sys.stderr)
        return EXIT_INVALID_SCORECARD
    output = (
        score_markdown(result)
        if args.format == "markdown"
        else json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    if args.output:
        output_path = Path(args.output).resolve()
        atomic_write_text(output_path, output)
        print(output_path)
    else:
        print(output, end="")
    return 0


def _read_scorecard_json(path: Path, label: str) -> object:
    if path.is_symlink():
        raise ScorecardError(f"{label} path must not be a symbolic link")
    try:
        return parse_json(path.read_text(encoding="utf-8"))
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateJsonKeyError,
    ) as exc:
        raise ScorecardError(f"{label} cannot be read: {exc}") from exc


def blind_scorecard_command(args: argparse.Namespace) -> int:
    raw_scorecard_path = Path(args.scorecard)
    scorecard_path = raw_scorecard_path.resolve()
    raw_blind_path = (
        Path(args.output)
        if getattr(args, "output", None)
        else scorecard_path.parent / "blind-review.json"
    )
    raw_key_path = (
        Path(args.key_output)
        if getattr(args, "key_output", None)
        else scorecard_path.parent / "blind-key.json"
    )
    blind_path = raw_blind_path.resolve()
    key_path = raw_key_path.resolve()
    try:
        if raw_scorecard_path.is_symlink():
            raise ScorecardError("scorecard path must not be a symbolic link")
        if len({scorecard_path, blind_path, key_path}) != 3:
            raise ScorecardError("scorecard, blind output, and key output must differ")
        if raw_blind_path.is_symlink() or raw_key_path.is_symlink():
            raise ScorecardError("blind and key outputs must not be symbolic links")
        if blind_path.exists() or key_path.exists():
            raise ScorecardError(
                "blind or key output already exists; choose new paths to avoid data loss"
            )
        scorecard = _read_scorecard_json(scorecard_path, "scorecard")
        provenance_errors = verify_scorecard_provenance(scorecard_path, scorecard)
        if provenance_errors:
            raise ScorecardError("; ".join(provenance_errors))
        seed = args.seed if args.seed is not None else secrets.token_hex(16)
        blind, key = create_blind_bundle(scorecard, seed)
        atomic_write_text(
            key_path,
            json.dumps(key, ensure_ascii=False, indent=2) + "\n",
        )
        atomic_write_text(
            blind_path,
            json.dumps(blind, ensure_ascii=False, indent=2) + "\n",
        )
    except (OSError, ValueError) as exc:
        print(f"blind scorecard error: {exc}", file=sys.stderr)
        return EXIT_INVALID_SCORECARD
    print(blind_path)
    print(key_path)
    return 0


def apply_blind_scorecard_command(args: argparse.Namespace) -> int:
    raw_scorecard_path = Path(args.scorecard)
    scorecard_path = raw_scorecard_path.resolve()
    raw_blind_path = (
        Path(args.blind)
        if getattr(args, "blind", None)
        else scorecard_path.parent / "blind-review.json"
    )
    raw_key_path = (
        Path(args.key)
        if getattr(args, "key", None)
        else scorecard_path.parent / "blind-key.json"
    )
    blind_path = raw_blind_path.resolve()
    key_path = raw_key_path.resolve()
    raw_output_path = (
        Path(args.output)
        if getattr(args, "output", None)
        else scorecard_path.parent / "completed-scorecard.json"
    )
    output_path = raw_output_path.resolve()
    try:
        if any(
            path.is_symlink()
            for path in (raw_scorecard_path, raw_blind_path, raw_key_path)
        ):
            raise ScorecardError(
                "scorecard, blind artifact, and key must not be symbolic links"
            )
        if output_path in {scorecard_path, blind_path, key_path}:
            raise ScorecardError("output must not overwrite an input artifact")
        if output_path.parent != scorecard_path.parent:
            raise ScorecardError(
                "output must stay beside the original scorecard to preserve campaign provenance"
            )
        if raw_output_path.is_symlink():
            raise ScorecardError("output must not be a symbolic link")
        if output_path.exists():
            raise ScorecardError("output already exists; choose a new path to avoid data loss")
        scorecard = _read_scorecard_json(scorecard_path, "scorecard")
        blind = _read_scorecard_json(blind_path, "blind artifact")
        key = _read_scorecard_json(key_path, "blind key")
        provenance_errors = verify_scorecard_provenance(scorecard_path, scorecard)
        if provenance_errors:
            raise ScorecardError("; ".join(provenance_errors))
        merged = apply_blind_pairings(scorecard, blind, key)
        atomic_write_text(
            output_path,
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        )
    except (OSError, ValueError) as exc:
        print(f"apply blind scorecard error: {exc}", file=sys.stderr)
        return EXIT_INVALID_SCORECARD
    print(output_path)
    return 0

__all__ = [name for name in globals() if not name.startswith("__")]
