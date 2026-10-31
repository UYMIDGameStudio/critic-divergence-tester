"""Export folder routing must distinguish macOS from other POSIX hosts."""
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import document_review_ui as ui
from document_review_model import ReviewContext
from document_review_studio import DocumentReviewProject, ReviewStudioError


class ExportFolderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='studio-export-folder-')
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name) / '資料 空格 résumé'
        project = DocumentReviewProject.create(root, filename='draft.txt', content=b'Clear source text.')
        project.confirm_extraction('confirm')
        project.confirm_context(ReviewContext('document', 'unknown', 'unknown', 'author', 'readers').to_dict())
        project.run_local_prechecks(['expression_ambiguity'])
        self.folder = project.export()
        self.relative = (self.folder / 'audit.json').relative_to(project.root).as_posix()
        self.app = ui.StudioApp.create(root, project.root)

    def test_macos_posix_action_uses_open_with_one_literal_folder_argument(self):
        with patch.object(ui, 'os', SimpleNamespace(name='posix')), patch.object(ui.sys, 'platform', 'darwin'), \
                patch.object(ui.subprocess, 'Popen') as launch:
            result = self.app.act({'action': 'open_export_folder', 'data': {'relative_path': self.relative}})
        launch.assert_called_once_with(['open', str(self.folder)], close_fds=True)
        self.assertIs(result, self.app)

    def test_windows_uses_startfile(self):
        startfile = Mock()
        with patch.object(ui, 'os', SimpleNamespace(name='nt', startfile=startfile)), \
                patch.object(ui.sys, 'platform', 'win32'), patch.object(ui.subprocess, 'Popen') as launch:
            self.app.open_export_folder(self.relative)
        startfile.assert_called_once_with(str(self.folder))
        launch.assert_not_called()

    def test_linux_uses_xdg_open(self):
        with patch.object(ui, 'os', SimpleNamespace(name='posix')), patch.object(ui.sys, 'platform', 'linux'), \
                patch.object(ui.subprocess, 'Popen') as launch:
            self.app.open_export_folder(self.relative)
        launch.assert_called_once_with(['xdg-open', str(self.folder)], close_fds=True)

    def test_invalid_or_tampered_export_never_launches_a_folder(self):
        with patch.object(ui.subprocess, 'Popen') as launch, patch.object(ui.os, 'startfile', create=True) as startfile:
            for relative in ('', '../outside', 'exports/../../outside', 'exports/missing.txt'):
                with self.subTest(relative=relative), self.assertRaises(ReviewStudioError):
                    self.app.open_export_folder(relative)
            (self.folder / 'audit.json').write_text('tampered', encoding='utf-8')
            with self.assertRaises(ReviewStudioError):
                self.app.open_export_folder(self.relative)
        launch.assert_not_called()
        startfile.assert_not_called()

    @unittest.skipUnless(sys.platform == 'darwin', 'Requires an actual macOS host')
    def test_real_macos_environment_selects_the_installed_system_opener(self):
        self.assertEqual(ui.os.name, 'posix')
        self.assertTrue(Path('/usr/bin/open').is_file())
        # Only process creation is intercepted. OS identity, export and path
        # checks run on the actual macOS runner; no interactive Finder required.
        with patch.object(ui.subprocess, 'Popen') as launch:
            self.app.open_export_folder(self.relative)
        launch.assert_called_once_with(['open', str(self.folder)], close_fds=True)


if __name__ == '__main__':
    unittest.main()
