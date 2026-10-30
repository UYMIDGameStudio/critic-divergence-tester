"""Early HTTP rejection must survive in-flight bodies without executing them."""
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from argument_app import ProductApp, ProductRequestHandler
from argument_ui import _finish_http_response, serve_workbench
from test.test_http_protocol_security import running, request
from unified_app import UnifiedRequestHandler


def delayed_post(server, path, *, headers=None, delay=.005):
    body = b'{"directory":"owned-test.argument-workbench","padding":"' + b'x'*2048 + b'"}'
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        connection.putrequest('POST', path)
        pairs = {'Content-Type':'application/json', 'Content-Length':str(len(body)), **(headers or {})}
        for key, value in pairs.items():
            connection.putheader(key, value)
        connection.endheaders()
        if delay:
            time.sleep(delay)
        connection.send(body)
        response = connection.getresponse()
        data = response.read()
        assert len(data) == int(response.getheader('Content-Length'))
        assert response.getheader('Connection') == 'close'
        return response.status, json.loads(data)
    finally:
        connection.close()


class HTTPTeardownTests(unittest.TestCase):
    def test_rejected_in_flight_bodies_deliver_complete_responses_on_both_interfaces(self):
        for kind, paths, handler in [('product', ['/api/open'], ProductRequestHandler),
                                     ('unified', ['/api/open', '/research/api/open'], UnifiedRequestHandler)]:
            with running(kind) as (server, _), patch.object(handler, '_read_json_body', side_effect=AssertionError('Unauthorized body reached the JSON reader')) as reader:
                for path in paths:
                    for delay in (0, .003, .01):
                        with self.subTest(kind=kind, path=path, delay=delay):
                            status, data = delayed_post(server, path, delay=delay)
                            self.assertEqual(status, 403)
                            self.assertIn('token', data['error'])
                reader.assert_not_called()
                self.assertEqual(request(server, research=kind=='product')[0], 200)

    def test_professional_workbench_also_delivers_early_rejection(self):
        from test.test_argument_ui import ArgumentUITests
        with tempfile.TemporaryDirectory() as temp:
            workspace = ArgumentUITests().make_project(Path(temp))
            server, _ = serve_workbench(workspace, open_browser=False)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
            thread.start()
            try:
                self.assertEqual(delayed_post(server, '/api/adjudications')[0], 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)

    def test_unknown_route_oversize_media_type_and_foreign_origin_still_reject_before_body_parse(self):
        with running('unified') as (server, _):
            auth = {'X-Document-Review-Token':server.app.token}
            for path, headers, expected in [('/api/unknown', {}, 404),
                ('/api/open', {**auth, 'Content-Length':'999999999'}, 413),
                ('/api/open', {**auth, 'Content-Type':'text/plain'}, 415),
                ('/api/open', {**auth, 'Origin':'https://foreign.invalid'}, 403)]:
                with self.subTest(expected=expected):
                    self.assertEqual(delayed_post(server, path, headers=headers)[0], expected)
            self.assertIsNone(server.app.project)

    def test_pipelined_authorized_request_is_discarded_after_rejection(self):
        with running() as (server, _), patch.object(ProductApp, 'open_project') as opened:
            host = f'127.0.0.1:{server.server_address[1]}'
            first = f'POST /api/open HTTP/1.1\r\nHost: {host}\r\nContent-Length: 2\r\n\r\n{{}}'
            second = f'POST /api/open HTTP/1.1\r\nHost: {host}\r\nX-Argument-Workbench-Token: {server.app.token}\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{{}}'
            connection = socket.create_connection(server.server_address, timeout=3)
            try:
                connection.sendall((first+second).encode())
                chunks = []
                while data := connection.recv(8192):
                    chunks.append(data)
                response = b''.join(chunks)
                self.assertIn(b' 403 ', response.split(b'\r\n',1)[0])
                self.assertEqual(response.count(b'HTTP/1.'), 1)
                opened.assert_not_called()
            finally:
                connection.close()

    def test_overload_response_survives_an_in_flight_body_and_keeps_the_slot_count(self):
        with running() as (server, _):
            for _ in range(server.max_connections):
                self.assertTrue(server.connection_slots.acquire(blocking=False))
            connection = http.client.HTTPConnection(*server.server_address, timeout=3)
            try:
                connection.putrequest('POST', '/api/open')
                connection.putheader('Content-Length', '2048')
                connection.endheaders()
                time.sleep(.005)
                connection.send(b'x'*2048)
                response = connection.getresponse()
                self.assertEqual(response.status, 503)
                self.assertEqual(response.read(), b'Too many clients')
                self.assertEqual(response.getheader('Connection'), 'close')
            finally:
                connection.close()
                for _ in range(server.max_connections):
                    server.connection_slots.release()
            self.assertEqual(request(server)[0], 200)

    def test_discard_is_byte_bounded_without_parsing_or_buffering_a_whole_body(self):
        connection = Mock()
        sizes = []
        def receive(size):
            sizes.append(size)
            return b'x'*size
        _finish_http_response(connection, receive, timeout=1, maximum=65536)
        self.assertEqual(sum(sizes), 65536)
        self.assertLessEqual(max(sizes), 8192)
        connection.shutdown.assert_called_once_with(socket.SHUT_WR)

    def test_dripping_peer_cannot_extend_the_absolute_teardown_deadline(self):
        server, client = socket.socketpair()
        stop = threading.Event()
        def drip():
            while not stop.is_set():
                try:
                    client.sendall(b'x')
                except OSError:
                    break
                stop.wait(.01)
        thread = threading.Thread(target=drip, daemon=True)
        thread.start()
        try:
            started = time.monotonic()
            _finish_http_response(server, timeout=.05)
            self.assertLess(time.monotonic()-started, .5)
        finally:
            stop.set()
            server.close()
            client.close()
            thread.join(1)

    def test_held_rejected_connections_release_slots_without_waiting_for_advertised_length(self):
        with running() as (server, _):
            held = []
            try:
                for _ in range(server.max_connections):
                    connection = socket.create_connection(server.server_address, timeout=3)
                    connection.sendall(f'POST /api/open HTTP/1.1\r\nHost: 127.0.0.1:{server.server_address[1]}\r\nContent-Length: 999999999\r\n\r\n'.encode())
                    held.append(connection)
                    response = http.client.HTTPResponse(connection)
                    response.begin()
                    self.assertEqual(response.status, 403)
                    response.read()
                    response.close()
                time.sleep(.2)
                self.assertEqual(request(server)[0], 200)
            finally:
                for connection in held:
                    connection.close()


if __name__ == '__main__':
    unittest.main()
