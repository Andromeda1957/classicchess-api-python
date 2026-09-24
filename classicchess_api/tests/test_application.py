"""Application requests preserve status and bytes without leaking credentials."""

import json
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from threading import Thread
from unittest import TestCase
from unittest.mock import patch

from classicchess_api import ApiError
from classicchess_api import ApplicationClient
from classicchess_api.tests.test_robustness import Response


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

    def test_library_mutations_use_exact_methods_paths_and_bodies(self):
        api = ApplicationClient()
        cases = [
            (lambda: api.account_add_collection_game(7, 'tal-vs-larsen', 't'),
             'POST', '/api/v1/account/collections/7/items/', {'game': 'tal-vs-larsen'}),
            (lambda: api.account_starred_players('t', page=2, page_size=10),
             'GET', '/api/v1/account/starred/players/?page=2&page_size=10', None),
            (lambda: api.account_star_player('mikhail-tal', 't'), 'PUT', '/api/v1/account/starred/players/mikhail-tal/', None),
            (lambda: api.account_unstar_player('mikhail-tal', 't'), 'DELETE', '/api/v1/account/starred/players/mikhail-tal/', None),
            (lambda: api.account_starred_games('t'), 'GET', '/api/v1/account/starred/games/?page=1&page_size=50', None),
            (lambda: api.account_star_game('g1', 't'), 'PUT', '/api/v1/account/starred/games/g1/', None),
            (lambda: api.account_unstar_game('g1', 't'), 'DELETE', '/api/v1/account/starred/games/g1/', None),
            (lambda: api.account_set_imported_game_visibility('mine', 'public', 't'),
             'PATCH', '/api/v1/account/imported-games/mine/', {'visibility': 'public'}),
            (lambda: api.account_delete_imported_game('mine', 't'), 'DELETE', '/api/v1/account/imported-games/mine/', None),
        ]
        for call, method, path, body in cases:
            with self.subTest(path=path, method=method), patch('classicchess_api.application.build_opener') as opener:
                opener.return_value.open.return_value = Reply(b'{"changed": true}')
                self.assertTrue(call().data['changed'])
                request = opener.return_value.open.call_args.args[0]
                self.assertEqual((request.get_method(), request.full_url), (method, 'https://classicchess.com' + path))
                self.assertEqual(request.headers['Authorization'], 'Bearer t')
                self.assertEqual(json.loads(request.data) if request.data else None, body)

    def test_library_mutations_reject_unsafe_arguments_before_sending(self):
        api = ApplicationClient()
        calls = [
            lambda: api.account_add_collection_game(0, 'g', 't'), lambda: api.account_add_collection_game('7', 'g', 't'),
            lambda: api.account_add_collection_game(7, '../me', 't'), lambda: api.account_star_player('a/b', 't'),
            lambda: api.account_unstar_game('', 't'), lambda: api.account_starred_games('t', page_size=500),
            lambda: api.account_set_imported_game_visibility('mine', 'PUBLIC', 't'),
            lambda: api.account_delete_imported_game('x?y', 't'),
        ]
        with patch('classicchess_api.application.build_opener') as opener:
            for call in calls:
                with self.subTest(call=call), self.assertRaises(ApiError):
                    call()
        opener.assert_not_called()


NOTEBOOK = '0f8fad5b-d9cb-469f-a165-70867728950e'


def headers(**values):
    return {key.replace('_', '-'): value for key, value in values.items()}


def file_reply(content, content_type, filename):
    reply = Reply(content)
    reply.headers = headers(Content_Type=content_type,
                            Content_Disposition='attachment; filename="' + filename + '"')
    return reply


class SiteGapApplicationTests(TestCase):
    def test_file_downloads_send_the_bearer_and_return_bytes(self):
        api = ApplicationClient()
        gif = b'GIF89a' + bytes(range(256))
        cases = [
            (lambda: api.master_game_gif('g1a2-0123456789ab', 't'), '/api/v1/games/g1a2-0123456789ab/gif/'),
            (lambda: api.public_game_gif('tal-vs-larsen', 't', orientation='black'),
             '/api/v1/public/games/tal-vs-larsen/gif/?orientation=black'),
            (lambda: api.annotated_game_gif('my-system', 'game-1', 't'),
             '/api/v1/annotated/books/my-system/games/game-1/gif/'),
            (lambda: api.public_imported_game_gif('ann.lee+1', 'g1', 't'),
             '/api/v1/public/imported-games/ann.lee+1/g1/gif/'),
            (lambda: api.account_imported_game_gif('mine', 't'), '/api/v1/account/imported-games/mine/gif/'),
        ]
        for call, path in cases:
            with self.subTest(path=path), patch('classicchess_api.application.build_opener') as opener:
                opener.return_value.open.return_value = file_reply(gif, 'image/gif', 'game.gif')
                result = call()
                request = opener.return_value.open.call_args.args[0]
                self.assertEqual((request.get_method(), request.full_url), ('GET', 'https://classicchess.com' + path))
                self.assertEqual(request.headers['Authorization'], 'Bearer t')
                self.assertTrue(result.ok)
                self.assertEqual((result.content, result.content_type, result.filename), (gif, 'image/gif', 'game.gif'))

    def test_failed_download_returns_the_json_error_and_retry_after(self):
        api = ApplicationClient()
        with patch('classicchess_api.application.build_opener') as opener:
            reply = Reply(json.dumps({'error': {'code': 'rate_limited', 'message': 'Slow down.'}}).encode())
            reply.status = 429
            reply.headers = headers(Content_Type='application/json', Retry_After='60')
            opener.return_value.open.return_value = reply
            result = api.public_game_gif('g1', 't')
        self.assertFalse(result.ok)
        self.assertEqual((result.status, result.content, result.retry_after), (429, b'', '60'))
        self.assertEqual(result.error['error']['code'], 'rate_limited')

OK_BODY = json.dumps({'ok': True}).encode()


class SiteGapRequestTests(TestCase):
    def assert_request(self, call, method, path, body):
        with patch('classicchess_api.application.build_opener') as opener:
            opener.return_value.open.return_value = Reply(OK_BODY)
            self.assertTrue(call().data['ok'])
            request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(), method)
        self.assertEqual(request.full_url, 'https://classicchess.com' + path)
        self.assertEqual(request.headers['Authorization'], 'Bearer t')
        self.assertEqual(json.loads(request.data) if request.data else None, body)

    def test_notification_requests_use_exact_methods_paths_and_bodies(self):
        api = ApplicationClient()
        change = {'topics': {'new_games': False}, 'sound_enabled': True}
        self.assert_request(lambda: api.account_notifications('t', page=2, page_size=20),
                            'GET', '/api/v1/account/notifications/?page=2&page_size=20', None)
        self.assert_request(lambda: api.account_mark_notification_read(5, 't'),
                            'POST', '/api/v1/account/notifications/5/read/', None)
        self.assert_request(lambda: api.account_mark_all_notifications_read('t'),
                            'POST', '/api/v1/account/notifications/read-all/', None)
        self.assert_request(lambda: api.account_dismiss_notification(5, 't'),
                            'DELETE', '/api/v1/account/notifications/5/', None)
        self.assert_request(lambda: api.account_notification_preferences('t'),
                            'GET', '/api/v1/account/notifications/preferences/', None)
        self.assert_request(lambda: api.account_update_notification_preferences('t', **change),
                            'PATCH', '/api/v1/account/notifications/preferences/', change)

    def test_notebook_requests_use_exact_paths(self):
        api = ApplicationClient()
        self.assert_request(lambda: api.account_notebooks('t'), 'GET', '/api/v1/account/notebooks/', None)
        self.assert_request(lambda: api.account_notebook(NOTEBOOK.upper(), 't'),
                            'GET', '/api/v1/account/notebooks/' + NOTEBOOK + '/', None)

    def test_notebook_exports_download_pgn_and_optionally_encrypted_files(self):
        api = ApplicationClient()
        base = 'https://classicchess.com/api/v1/account/notebooks/' + NOTEBOOK
        with patch('classicchess_api.application.build_opener') as opener:
            opener.return_value.open.return_value = file_reply(b'1. e4 *', 'application/x-chess-pgn', 'line.pgn')
            chapter = api.account_notebook_chapter_pgn(NOTEBOOK, 9, 't')
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), 'GET')
            self.assertEqual(request.full_url, base + '/chapters/9/pgn/')
            self.assertEqual(chapter.content, b'1. e4 *')
            self.assertEqual(chapter.filename, 'line.pgn')
            ccnb = 'application/vnd.classicchess.notebook'
            opener.return_value.open.return_value = file_reply(b'CCNB', ccnb, 'n.ccnb')
            api.account_notebook_file(NOTEBOOK, 't')
            self.assertEqual(opener.return_value.open.call_args.args[0].get_method(), 'GET')
            opener.return_value.open.return_value = file_reply(b'CCNB', ccnb, 'n.ccnb')
            api.account_notebook_file(NOTEBOOK, 't', password='secret')
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), 'POST')
            self.assertEqual(request.full_url, base + '/file/')
            self.assertEqual(json.loads(request.data), {'password': 'secret'})

    def test_bearers_reach_only_gif_routes_among_public_paths(self):
        api = ApplicationClient()
        paths = ('/api/v1/public/games/g1/', '/api/v1/public/games/g1/pgn/',
                 '/api/v1/public/games/g1/gif/extra/', '/api/v1/public/imported-games/ann/g1/')
        with patch('classicchess_api.application.build_opener') as opener:
            opener.return_value.open.side_effect = lambda *args, **kwargs: Reply(OK_BODY)
            for path in paths:
                with self.subTest(path=path), self.assertRaises(ApiError) as caught:
                    api.download(path, token='t')
                self.assertEqual(caught.exception.code, 'unsafe_credentials')
            opener.assert_not_called()

    def test_site_gap_helpers_reject_unsafe_arguments_before_sending(self):
        api = ApplicationClient()
        calls = [
            lambda: api.public_game_gif('../me', 't'),
            lambda: api.public_game_gif('g1', 't', orientation='left'),
            lambda: api.public_imported_game_gif('..', 'g1', 't'),
            lambda: api.public_imported_game_gif('a/b', 'g1', 't'),
            lambda: api.account_mark_notification_read(0, 't'),
            lambda: api.account_dismiss_notification('5', 't'),
            lambda: api.account_update_notification_preferences('t'),
            lambda: api.account_update_notification_preferences('t', topics={'new_games': 'no'}),
            lambda: api.account_notebook('not-a-uuid', 't'),
            lambda: api.account_notebook_chapter_pgn(NOTEBOOK, 0, 't'),
            lambda: api.account_notebook_file(NOTEBOOK, 't', password=''),
        ]
        with patch('classicchess_api.application.build_opener') as opener:
            for call in calls:
                with self.subTest(call=call), self.assertRaises(ApiError):
                    call()
        opener.assert_not_called()
