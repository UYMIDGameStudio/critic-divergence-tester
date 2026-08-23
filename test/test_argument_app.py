from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import critic_runner
from argument_app import ProductApp, create_uploaded_project, project_state, render_product_shell
from argument_workbench import WorkbenchError, workspace_paths


class ArgumentProductAppTests(unittest.TestCase):
    def test_upload_creates_immutable_v1_and_one_next_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            library = Path(temp_dir) / "projects"
            source = "# 论证\n\n因此，结论成立。\n".encode()
            project = create_uploaded_project(
                library, filename="稿件.md", content=source, title="测试项目"
            )
            workspace = workspace_paths(project)
            version_bytes = workspace.version.read_bytes()
            archived = next((workspace.version_dir / "source").iterdir())
            self.assertEqual(archived.read_bytes(), source)
            self.assertEqual(project_state(project)["next_action"], "导入审查报告")

            recovered = create_uploaded_project(
                library, filename="稿件.md", content=source, title="被忽略的标题"
            )
            self.assertEqual(recovered, project)
            self.assertEqual(workspace.version.read_bytes(), version_bytes)

    def test_shell_lists_and_opens_only_library_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            library = Path(temp_dir) / "projects"
            project = create_uploaded_project(
                library, filename="draft.md", content=b"A supported claim.\n"
            )
            app = ProductApp.create(library)
            self.assertIsNone(app.view()["selected"])
            opened = app.open_project({"directory": project.name})
            self.assertEqual(opened.view()["selected"]["current_version"], "V1")
            with self.assertRaises(WorkbenchError):
                app.open_project({"directory": "../outside.argument-workbench"})
            shell = render_product_shell(app.token)
            self.assertIn("下一步", shell)
            self.assertIn("每一处 AI 修改", shell)

    def test_app_cli_is_top_level_and_loopback_only(self) -> None:
        parsed = critic_runner.parser().parse_args(["app", "--no-browser"])
        self.assertEqual(parsed.command, "app")
        self.assertEqual(parsed.host, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
