from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from argument_gate_common import atomic_gate_directory  # noqa: E402


class AtomicGateDirectoryTests(unittest.TestCase):
    def test_kill_at_mkdir_and_publish_boundaries_is_recoverable(self) -> None:
        script = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "from argument_gate_common import atomic_gate_directory\n"
            "root = Path(sys.argv[1])\n"
            "phase = sys.argv[2]\n"
            "original_mkdir = Path.mkdir\n"
            "def mkdir(path, *args, **kwargs):\n"
            "    result = original_mkdir(path, *args, **kwargs)\n"
            "    if phase == 'mkdir' and path.name.endswith('.gate-staging'):\n"
            "        os._exit(9)\n"
            "    return result\n"
            "def replace(path, target):\n"
            "    os._exit(9)\n"
            "Path.mkdir = mkdir\n"
            "Path.replace = replace\n"
            "with atomic_gate_directory(root) as staging:\n"
            "    (staging / 'corpus.json').write_bytes(b'complete')\n"
        )
        for phase in ('mkdir', 'publish'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'gate'
                result = subprocess.run([sys.executable, '-c', script, str(root), phase], cwd=REPO_ROOT, timeout=15)
                self.assertEqual(result.returncode, 9)
                self.assertFalse(root.exists())
                with atomic_gate_directory(root) as staging:
                    (staging / 'corpus.json').write_bytes(b'recovered')
                self.assertEqual((root / 'corpus.json').read_bytes(), b'recovered')
                self.assertFalse(list(root.parent.glob('.gate.*')))

    def test_existing_destination_is_checked_again_under_lock(self) -> None:
        from contextlib import contextmanager

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'gate'

            @contextmanager
            def publish_competing_gate(_):
                root.mkdir()
                yield

            with patch('argument_gate_common.project_mutation_lock', publish_competing_gate):
                with self.assertRaises(FileExistsError):
                    with atomic_gate_directory(root):
                        self.fail('must refuse the destination before building')
            self.assertTrue(root.is_dir())

    def test_recovery_does_not_interpret_gate_name_as_a_glob(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            other = parent / '.gate-a.abcdefgh'
            other.mkdir()
            (other / '.gate-initialization').write_bytes(
                b'Product Gate staging directory; safe to remove after an interrupted init.\n'
            )
            with atomic_gate_directory(parent / 'gate-[ab]') as staging:
                (staging / 'corpus.json').write_bytes(b'new')
            self.assertTrue(other.is_dir())

    def test_hard_killed_initialization_is_cleaned_on_retry(self) -> None:
        script = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "from argument_gate_common import atomic_gate_directory\n"
            "with atomic_gate_directory(Path(sys.argv[1])) as staging:\n"
            "    (staging / 'partial.json').write_text('partial', encoding='utf-8')\n"
            "    os._exit(9)\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "gate-b"
            completed = subprocess.run(
                [sys.executable, "-c", script, str(root)],
                cwd=REPO_ROOT,
                check=False,
            )
            self.assertEqual(completed.returncode, 9)
            self.assertFalse(root.exists())
            self.assertTrue(list(parent.glob(".gate-b.*")))

            with atomic_gate_directory(root) as staging:
                (staging / "complete.json").write_text("complete", encoding="utf-8")

            self.assertTrue((root / "complete.json").is_file())
            self.assertEqual(list(parent.glob(".gate-b.*")), [])


if __name__ == "__main__":
    unittest.main()
