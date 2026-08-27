from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import critic_runner
from argument_app import ProductApp, create_uploaded_project, project_state, render_product_shell, serve_product_app
from argument_workbench import WorkbenchError, workspace_paths


class ArgumentProductAppTests(unittest.TestCase):
    @staticmethod
    def _post(url: str, token: str, path: str, payload: dict[str, object]) -> dict[str, object]:
        request = urllib.request.Request(
            url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Argument-Workbench-Token": token,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

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
            self.assertIn("每一处修改", shell)
            self.assertIn("uncertainMutation", shell)
            self.assertIn("不要直接重复提交", shell)

    def test_invalid_library_entry_is_listed_without_breaking_home_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            library = Path(temp_dir) / "projects"
            invalid = library / "broken.argument-workbench"
            invalid.mkdir(parents=True)
            state = ProductApp.create(library).view()
            self.assertEqual(len(state["projects"]), 1)
            self.assertTrue(state["projects"][0]["invalid"])

    def test_project_library_rejects_symbolic_link_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            content = b"A supported claim.\n"
            real_project = create_uploaded_project(
                root / "outside", filename="draft.md", content=content
            )
            library = root / "library"
            library.mkdir()
            alias = library / real_project.name
            try:
                alias.symlink_to(real_project, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"directory symbolic links unavailable: {exc}")
            with self.assertRaisesRegex(WorkbenchError, "符号链接"):
                create_uploaded_project(library, filename="draft.md", content=content)
            app = ProductApp.create(library)
            with self.assertRaises(WorkbenchError):
                app.open_project({"directory": alias.name})
            with self.assertRaisesRegex(WorkbenchError, "符号链接"):
                ProductApp.create(library, alias)

    def test_http_unexpected_mutation_error_returns_json_and_server_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = create_uploaded_project(
                Path(temp_dir) / "projects", filename="draft.md", content=b"Claim.\n"
            )
            server, url = serve_product_app(
                data_dir=project.parent, project_dir=project, open_browser=False
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(ProductApp, "act", side_effect=RuntimeError("boom")):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        self._post(url, server.app.token, "api/action", {"action": "export", "data": {}})
                    try:
                        self.assertEqual(caught.exception.code, 500)
                        body = json.loads(caught.exception.read())
                        self.assertIn("操作可能已经完成", body["error"])
                    finally:
                        caught.exception.close()
                request = urllib.request.Request(
                    url + "api/state",
                    headers={"X-Argument-Workbench-Token": server.app.token},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(json.loads(response.read())["selected"]["current_version"], "V1")
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, PermissionError):
                    self.skipTest("local TCP connections are blocked by this sandbox")
                raise
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_http_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = create_uploaded_project(
                Path(temp_dir) / "projects", filename="draft.md", content=b"Claim.\n"
            )
            server, url = serve_product_app(
                data_dir=project.parent, project_dir=project, open_browser=False
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            request = urllib.request.Request(
                url + "api/action",
                data=b'{"action":"export","action":"apply_revision","data":{}}',
                headers={
                    "Content-Type": "application/json",
                    "X-Argument-Workbench-Token": server.app.token,
                },
                method="POST",
            )
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=5)
                try:
                    self.assertEqual(caught.exception.code, 400)
                    self.assertIn("duplicate JSON key", json.loads(caught.exception.read())["error"])
                finally:
                    caught.exception.close()
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, PermissionError):
                    self.skipTest("local TCP connections are blocked by this sandbox")
                raise
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_http_mutations_are_serialized_across_server_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = create_uploaded_project(
                Path(temp_dir) / "projects", filename="draft.md", content=b"Claim.\n"
            )
            server, url = serve_product_app(
                data_dir=project.parent, project_dir=project, open_browser=False
            )
            second_server, second_url = serve_product_app(
                data_dir=project.parent, project_dir=project, open_browser=False
            )
            server_threads = [
                threading.Thread(target=item.serve_forever, daemon=True)
                for item in (server, second_server)
            ]
            for server_thread in server_threads:
                server_thread.start()
            active = 0
            maximum = 0
            counter_lock = threading.Lock()

            def slow_action(app: ProductApp, payload: dict[str, object]) -> dict[str, object]:
                nonlocal active, maximum
                with counter_lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.1)
                with counter_lock:
                    active -= 1
                return {"ok": True}

            errors: list[BaseException] = []

            def request_action(target_url: str, token: str) -> None:
                try:
                    self._post(target_url, token, "api/action", {"action": "export", "data": {}})
                except BaseException as exc:  # captured for the main test thread
                    errors.append(exc)

            try:
                with patch.object(ProductApp, "act", autospec=True, side_effect=slow_action):
                    clients = [
                        threading.Thread(target=request_action, args=(url, server.app.token)),
                        threading.Thread(
                            target=request_action,
                            args=(second_url, second_server.app.token),
                        ),
                    ]
                    for client in clients:
                        client.start()
                    for client in clients:
                        client.join(timeout=5)
                self.assertEqual(errors, [])
                self.assertEqual(maximum, 1)
            finally:
                for item in (server, second_server):
                    item.shutdown()
                    item.server_close()
                for server_thread in server_threads:
                    server_thread.join(timeout=5)

    def test_app_cli_is_top_level_and_loopback_only(self) -> None:
        parsed = critic_runner.parser().parse_args(["app", "--no-browser"])
        self.assertEqual(parsed.command, "app")
        self.assertEqual(parsed.host, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
