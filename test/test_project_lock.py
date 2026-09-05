from __future__ import annotations

import subprocess
import errno
import os
import gc
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import project_lock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from project_lock import ProjectMutationLockedError, project_mutation_lock  # noqa: E402
import critic_runner  # noqa: E402


class ProjectMutationLockTests(unittest.TestCase):
    def test_unused_project_thread_locks_are_released(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with project_mutation_lock(directory):
                key = os.path.normcase(str(Path(directory).resolve()))
                self.assertIn(key, project_lock._THREAD_LOCKS)
            gc.collect()
            self.assertNotIn(key, project_lock._THREAD_LOCKS)

    @unittest.skipUnless(os.name == 'nt', 'Windows paths are case insensitive')
    def test_reentrant_lock_accepts_case_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with project_mutation_lock(directory):
                with project_mutation_lock(directory.upper()):
                    pass

    def test_hardlinked_lock_is_rejected_without_changing_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / 'project'
            root.mkdir()
            target = parent / 'original'
            target.write_bytes(b'')
            try:
                os.link(target, root / '.mutation.lock')
            except OSError as exc:
                self.skipTest(f'hardlinks unavailable: {exc}')
            with self.assertRaisesRegex(ValueError, 'single link'):
                with project_mutation_lock(root):
                    self.fail('hardlinked lock accepted')
            self.assertEqual(target.read_bytes(), b'')

    def test_unrelated_os_error_is_not_reported_as_contention(self) -> None:
        if os.name == 'nt':
            target = 'msvcrt.locking'
        else:
            target = 'fcntl.flock'
        with tempfile.TemporaryFile() as handle:
            with patch(target, side_effect=OSError(errno.EIO, 'device error')):
                with self.assertRaises(OSError) as raised:
                    project_lock._lock_byte(handle)
            self.assertEqual(raised.exception.errno, errno.EIO)

    def test_lock_is_reentrant_in_one_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with project_mutation_lock(root):
                with project_mutation_lock(root):
                    self.assertTrue((root / ".mutation.lock").is_file())

    def test_second_process_gets_an_immediate_clear_error(self) -> None:
        script = (
            "from pathlib import Path\n"
            "from project_lock import project_mutation_lock\n"
            "import sys\n"
            "with project_mutation_lock(Path(sys.argv[1])):\n"
            "    print('locked', flush=True)\n"
            "    sys.stdin.readline()\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(root)],
                cwd=REPO_ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "locked")
                with self.assertRaisesRegex(
                    ProjectMutationLockedError,
                    "already being modified by another local process",
                ):
                    with project_mutation_lock(root):
                        self.fail("competing process unexpectedly acquired the lock")
            finally:
                if process.stdin:
                    process.stdin.write("release\n")
                    process.stdin.flush()
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual((process.returncode, stdout, stderr), (0, "", ""))

    def test_cli_mutation_reports_contention_instead_of_entering_command(self) -> None:
        script = (
            "from pathlib import Path\n"
            "from project_lock import project_mutation_lock\n"
            "import sys\n"
            "with project_mutation_lock(Path(sys.argv[1])):\n"
            "    print('locked', flush=True)\n"
            "    sys.stdin.readline()\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(root)],
                cwd=REPO_ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "locked")
                errors = StringIO()
                with redirect_stderr(errors):
                    result = critic_runner.main(["ir", "rebuild", str(root)])
                self.assertEqual(result, 2)
                self.assertIn("already being modified by another local process", errors.getvalue())
            finally:
                if process.stdin:
                    process.stdin.write("release\n")
                    process.stdin.flush()
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
