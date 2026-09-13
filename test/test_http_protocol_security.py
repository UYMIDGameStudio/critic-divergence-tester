"""Real loopback requests: ambiguous headers, deadlines, and stale research tabs."""
from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from argument_app import ProductRequestHandler, create_uploaded_project, serve_product_app
from argument_ui import WorkbenchRequestHandler, serve_workbench
from document_review_ui import StudioRequestHandler
from unified_app import UnifiedRequestHandler, serve_unified_app


@contextmanager
def running(kind="product", project=False):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = create_uploaded_project(root, filename="a.md", content=b"Project A claim.") if project else None
        factory = serve_unified_app if kind == "unified" else serve_product_app
        server, _ = factory(data_dir=root, project_dir=target, open_browser=False)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            yield server, root
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


def request(server, path="/api/state", *, method="GET", payload=None, headers=(), research=True, raw=None):
    body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    connection.putrequest(method, path, skip_host=True)
    pairs = list(headers)
    if not any(name.lower() == "host" for name, _ in pairs):
        pairs.append(("Host", f"127.0.0.1:{server.server_address[1]}"))
    token_name = "X-Argument-Workbench-Token" if research else "X-Document-Review-Token"
    if not any(name.lower() == token_name.lower() for name, _ in pairs):
        pairs.append((token_name, server.app.token))
    if body is not None:
        if not any(name.lower() == "content-type" for name, _ in pairs):
            pairs.append(("Content-Type", "application/json"))
        if not any(name.lower() == "content-length" for name, _ in pairs):
            pairs.append(("Content-Length", str(len(body))))
    for name, value in pairs:
        connection.putheader(name, value)
    connection.endheaders(body)
    response = connection.getresponse()
    status, data = response.status, response.read()
    connection.close()
    return status, json.loads(data) if data.startswith(b"{") else data


class HTTPProtocolSecurityTests(unittest.TestCase):
    def test_research_shell_never_exposes_token_for_foreign_host(self):
        with running() as (server, _):
            status, body = request(server, "/", headers=[("Host", f"rebind.example:{server.server_address[1]}")])
            self.assertEqual(status, 403)
            self.assertNotIn(server.app.token, str(body))

    def test_duplicate_or_nonascii_auth_and_origin_headers_are_rejected(self):
        for kind in ("product", "unified"):
            with self.subTest(kind=kind), running(kind) as (server, _):
                token_name = "X-Argument-Workbench-Token" if kind == "product" else "X-Document-Review-Token"
                local = f"http://127.0.0.1:{server.server_address[1]}"
                for headers in (
                    [(token_name, server.app.token), (token_name, "wrong")],
                    [(token_name, "\u00e9")],
                    [("Origin", local), ("Origin", "https://evil.example")],
                    [("Host", local.removeprefix("http://") + "#ignored")],
                ):
                    with self.subTest(headers=headers):
                        self.assertEqual(request(server, headers=headers, research=kind == "product")[0], 403)

    def test_ambiguous_body_framing_and_media_type_are_rejected(self):
        for kind in ("product", "unified"):
            with self.subTest(kind=kind), running(kind) as (server, _):
                path = "/api/projects" if kind == "product" else "/api/upload"
                for headers, expected in (
                    ([("Content-Length", "2"), ("Content-Length", "2")], 400),
                    ([("Transfer-Encoding", "")], 400),
                    ([("Content-Type", "text/plain")], 415),
                    ([("Content-Type", "application/json"), ("Content-Type", "text/plain")], 415),
                    ([("Content-Length", "+2")], 400),
                ):
                    with self.subTest(headers=headers):
                        status, _ = request(server, path, method="POST", raw=b"{}", headers=headers, research=kind == "product")
                        self.assertEqual(status, expected)

    def test_stale_tab_cannot_write_report_into_newly_selected_project(self):
        with running(project=True) as (server, root):
            original = server.app.project_dir
            old = request(server)[1].get("request_context", "pre-fix-context")
            second = create_uploaded_project(root, filename="b.md", content=b"Project B claim.")
            self.assertEqual(request(server, "/api/open", method="POST", payload={"directory": second.name})[0], 201)
            status, _ = request(server, "/api/action", method="POST", payload={"action": "import_report", "data": {"report": "For A only"}}, headers=[("X-Argument-Project-Context", old)])
            self.assertEqual(status, 409)
            self.assertEqual(list(second.rglob("report.md")), [])
            self.assertEqual(list(original.rglob("report.md")), [])

    def test_current_context_is_stable_for_reads_and_consumed_by_changed_workflow(self):
        with running(project=True) as (server, root):
            state = request(server)[1]
            current = state.get("request_context", "pre-fix-context")
            self.assertEqual(request(server)[1].get("request_context", "pre-fix-context"), current)
            payload = {"action": "import_report", "data": {"report": "One saved report"}}
            self.assertEqual(request(server, "/api/action", method="POST", payload=payload, headers=[("X-Argument-Project-Context", current)])[0], 201)
            self.assertEqual(request(server, "/api/action", method="POST", payload=payload, headers=[("X-Argument-Project-Context", current)])[0], 409)
            self.assertEqual(len(list(root.rglob("report.md"))), 1)

    def test_mutation_without_context_cannot_bypass_stale_tab_guard(self):
        with running(project=True) as (server, root):
            status, _ = request(server, "/api/action", method="POST", payload={"action": "import_report", "data": {"report": "Not bound"}})
            self.assertEqual(status, 409)
            self.assertEqual(list(root.rglob("report.md")), [])

    def test_unfinished_research_upload_does_not_lock_studio_and_has_deadline(self):
        with patch.object(UnifiedRequestHandler, "body_timeout", .5, create=True), running("unified") as (server, _):
            connection = socket.create_connection(server.server_address, timeout=2)
            connection.sendall((f"POST /research/api/projects HTTP/1.1\r\nHost: 127.0.0.1:{server.server_address[1]}\r\nX-Argument-Workbench-Token: {server.app.token}\r\nContent-Type: application/json\r\nContent-Length: 200\r\n\r\n").encode())
            try:
                time.sleep(.05)
                start = time.monotonic()
                # A normal independent read must complete before the slow upload deadline.
                self.assertEqual(request(server, research=False)[0], 200)
                self.assertLess(time.monotonic() - start, .4)
                self.assertIn(b"408", connection.recv(4096).split(b"\r\n", 1)[0])
            finally:
                connection.close()

    def test_body_completion_after_project_switch_is_checked_against_new_selection(self):
        with running(project=True) as (server, root):
            current = request(server)[1]["request_context"]
            body = json.dumps({"action": "import_report", "data": {"report": "Old tab report"}}).encode()
            connection = socket.create_connection(server.server_address, timeout=2)
            connection.sendall((f"POST /api/action HTTP/1.1\r\nHost: 127.0.0.1:{server.server_address[1]}\r\nX-Argument-Workbench-Token: {server.app.token}\r\nX-Argument-Project-Context: {current}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode())
            try:
                second = create_uploaded_project(root, filename="b.md", content=b"B.")
                self.assertEqual(request(server, "/api/open", method="POST", payload={"directory": second.name})[0], 201)
                connection.sendall(body)
                self.assertIn(b"409", connection.recv(4096).split(b"\r\n", 1)[0])
                self.assertEqual(list(root.rglob("report.md")), [])
            finally:
                connection.close()

    def test_non_json_numbers_surrogates_and_root_types_do_not_mutate(self):
        with running("unified") as (server, root):
            for raw in (b'{"directory":1e999}', b'{"directory":"\\ud800"}', b'{"directory":NaN}', b'[]'):
                with self.subTest(raw=raw):
                    self.assertEqual(request(server, "/api/open", method="POST", raw=raw, research=False)[0], 400)
            self.assertIsNone(server.app.project)

    def test_oversize_body_is_rejected_before_reading_it(self):
        for kind in ("product", "unified"):
            with self.subTest(kind=kind), running(kind) as (server, _):
                path = "/api/projects" if kind == "product" else "/api/upload"
                self.assertEqual(request(server, path, method="POST", raw=b"{}", headers=[("Content-Length", "999999999")], research=kind == "product")[0], 413)

    def test_professional_adjudication_requires_its_displayed_snapshot(self):
        from test.test_argument_ui import ArgumentUITests
        with tempfile.TemporaryDirectory() as temporary:
            workspace = ArgumentUITests().make_project(Path(temporary))
            server, _ = serve_workbench(workspace, open_browser=False)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            try:
                foreign = [("Host", f"foreign.example:{server.server_address[1]}")]
                self.assertEqual(request(server, "/", headers=foreign)[0], 403)
                view = request(server, "/api/view")[1]
                finding = view["findings"][0]
                payload = {"finding_id": finding["finding_id"], "decision": "reject", "reason": "Human declines this issue", "actions": []}
                self.assertEqual(request(server, "/api/adjudications", method="POST", payload=payload)[0], 409)
                headers = [("X-Argument-Project-Context", view["request_context"])]
                self.assertEqual(request(server, "/api/adjudications", method="POST", payload=payload, headers=headers)[0], 201)
                self.assertEqual(request(server, "/api/adjudications", method="POST", payload=payload, headers=headers)[0], 409)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)

    def test_incomplete_headers_have_absolute_deadline(self):
        with patch.object(ProductRequestHandler, "header_timeout", .2, create=True), running() as (server, _):
            connection = socket.create_connection(server.server_address, timeout=1)
            connection.sendall(b"GET / HTTP/1.1\r\nHost: ")
            try:
                time.sleep(.35)
                self.assertEqual(connection.recv(1024), b"")
                self.assertEqual(request(server)[0], 200)
            finally:
                connection.close()

    def test_non_text_reason_cannot_become_a_formal_human_approval(self):
        from test.test_argument_revision import ArgumentRevisionTests
        with running() as (server, root):
            fixture = ArgumentRevisionTests()
            project = fixture.project(root)
            fixture.atomize(project)
            self.assertEqual(request(server, "/api/open", method="POST", payload={"directory": project.name})[0], 201)
            current = request(server)[1]["request_context"]
            status, _ = request(server, "/api/action", method="POST", payload={"action": "decide_finding", "data": {"finding_id": "F1", "decision": "accept", "reason": None}}, headers=[("X-Argument-Project-Context", current)])
            self.assertEqual(status, 400)
            self.assertEqual(request(server)[1]["selected"]["findings"][0]["decision"], None)


if __name__ == "__main__":
    unittest.main()
