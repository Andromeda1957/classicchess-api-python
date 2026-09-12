"""Application requests preserve status and bytes without leaking credentials."""

import json
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from threading import Thread
from unittest import TestCase
from unittest.mock import patch

from classicchess_api import ApiError
from classicchess_api import ApplicationClient
from classicchess_api.test_robustness import Response


class Reply(Response):
    status = 200


class ApplicationClientTests(TestCase):
    def test_redirects_never_forward_credentials_or_cookies(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.path, self.headers.get('Cookie')))
                self.send_response(302 if self.path.endswith('/me/') else 200)
                self.send_header('Location', '/api/v1/account/redirected/')
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                api = ApplicationClient(f'http://127.0.0.1:{server.server_port}')
                response = api.account_me('fixture')
                self.assertEqual(response.status, 302)
                self.assertFalse(response.ok)
                self.assertEqual(received, [('/api/v1/account/me/', None)])
            finally:
                server.shutdown()
                thread.join()

    def test_http_status_empty_responses_and_invalid_json_are_preserved(self):
        with patch('classicchess_api.application.build_opener') as opener:
            api = ApplicationClient()
            reply = Reply(b'{"error":{"code":"conflict"}}')
            reply.status = 409
            reply.headers = {'Retry-After': '30'}
            opener.return_value.open.return_value = reply
            response = api.account_me('fixture')
            self.assertFalse(response.ok)
            self.assertEqual(response.status, 409)
            self.assertEqual(response.retry_after, '30')
            self.assertEqual(response.data['error']['code'], 'conflict')
            opener.return_value.open.return_value = Reply(b'')
            self.assertEqual(api.account_me('fixture').data, {})
            opener.return_value.open.return_value = Reply(b'<html>proxy error</html>')
            with self.assertRaises(ApiError) as caught:
                api.account_me('fixture')
            self.assertEqual(caught.exception.code, 'invalid_response')

    def test_response_limits_and_timeouts_fail_explicitly(self):
        with patch('classicchess_api.application.build_opener') as opener:
            api = ApplicationClient(max_response_bytes=8)
            opener.return_value.open.return_value = Reply(b'{"too":"large"}')
            with self.assertRaises(ApiError) as caught:
                api.account_me('fixture')
            self.assertEqual(caught.exception.code, 'response_too_large')
            opener.return_value.open.side_effect = TimeoutError()
            with self.assertRaises(ApiError) as caught:
                api.account_me('fixture')
            self.assertEqual(caught.exception.code, 'timeout')

    def test_account_helpers_and_binary_requests(self):
        api = ApplicationClient()
        with patch('classicchess_api.application.build_opener') as opener:
            opener.return_value.open.side_effect = lambda *args, **kwargs: Reply(b'{"ok":true}')
            result = api.account_create_collection('Tal archive', 'fixture')
            request = opener.return_value.open.call_args.args[0]
            self.assertTrue(result.ok)
            self.assertEqual(json.loads(request.data), {'name': 'Tal archive'})
            self.assertEqual(request.get_header('Authorization'), 'Bearer fixture')
            binary = bytes(range(256))
            api.request('/api/v1/desktop/notebooks/sync/', method='POST', body=binary,
                        content_type='application/octet-stream', token='fixture')
            self.assertEqual(opener.return_value.open.call_args.args[0].data, binary)

    def test_invalid_credentials_paths_and_bodies_never_reach_the_network(self):
        api = ApplicationClient()
        cases = [
            {'path': 'https://elsewhere.test/api/v1/account/me/', 'token': 'fixture'},
            {'path': '//elsewhere.test/api/v1/account/me/', 'token': 'fixture'},
            {'path': '/api/v1/account/../public/players/', 'token': 'fixture'},
            {'path': '/api/v1/account/%2e%2e/public/players/', 'token': 'fixture'},
            {'path': '/api/v1/public/players/', 'token': 'fixture'},
            {'path': '/api/v1/account/me/', 'token': 'bad\r\nheader'},
            {'path': '/api/v1/account/me/', 'body': b'GET body'},
        ]
        with patch('classicchess_api.application.build_opener') as opener:
            for case in cases:
                with self.subTest(case=case), self.assertRaises(ApiError):
                    api.request(**case)
            opener.assert_not_called()

    def test_scanner_encodes_a_bounded_multipart_upload(self):
        with patch('classicchess_api.application.build_opener') as opener:
            opener.return_value.open.side_effect = lambda *args, **kwargs: Reply(b'{"ok":true}')
            api = ApplicationClient()
            api.scan_position(b'jpeg fixture', 'fixture')
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://classicchess.com/api/v1/mobile/position-scan/')
            self.assertIn(b'jpeg fixture', request.data)
            self.assertIn(b'name="image"; filename="diagram.jpg"', request.data)
            self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], 85)
            for image in (b'', b'x' * 850_001):
                with self.assertRaises(ApiError):
                    api.scan_position(image, 'fixture')
            self.assertEqual(opener.return_value.open.call_count, 1)
