"""Partition ordinary unittest discovery without dropping skips or loader errors."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def flatten_tests(suite):
    if isinstance(suite, unittest.TestSuite):
        for child in suite:
            yield from flatten_tests(child)
    else:
        yield suite


def select_shard(tests, index, count):
    if type(index) is not int or type(count) is not int or count < 1 or not 0 <= index < count:
        raise ValueError('Require a positive shard count and a zero-based index below it')
    return [test for position, test in enumerate(tests) if position % count == index]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start-directory', type=Path, default=ROOT/'test')
    parser.add_argument('--pattern', default='test_*.py')
    parser.add_argument('--shard-index', type=int, default=0)
    parser.add_argument('--shard-count', type=int, default=1)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--list', action='store_true', help='Show selected IDs without running tests')
    args = parser.parse_args(argv)
    try:
        select_shard([], args.shard_index, args.shard_count)
    except ValueError as exc:
        parser.error(str(exc))
    tests = list(flatten_tests(unittest.TestLoader().discover(str(args.start_directory.resolve()), pattern=args.pattern)))
    selected = select_shard(tests, args.shard_index, args.shard_count)
    all_ids = [test.id() for test in tests]
    report = {'shard_index':args.shard_index, 'shard_count':args.shard_count,
        'total_discovered':len(tests), 'selected_count':len(selected),
        'discovery_sha256':hashlib.sha256(json.dumps(all_ids, ensure_ascii=False).encode('utf-8')).hexdigest(),
        'test_ids':[test.id() for test in selected]}
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    if args.list:
        print(json.dumps(report, ensure_ascii=False))
        return 0
    if not selected:
        parser.error('No tests selected; refusing an empty successful CI job')
    print(f'Running shard {args.shard_index+1}/{args.shard_count}: {len(selected)} of {len(tests)} test entries', flush=True)
    return 0 if unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected)).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
