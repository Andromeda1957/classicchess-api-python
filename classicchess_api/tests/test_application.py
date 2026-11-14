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
    def test_ccnb_format_negotiates_get_and_password_post(self):
        api = ApplicationClient()
        for ccnb_format in (None, 2, 3):
            for password in (None, 'test-only-password'):
                with self.subTest(ccnb_format=ccnb_format, encrypted=password is not None), patch('classicchess_api.application.build_opener') as opener:
                    opener.return_value.open.return_value = file_reply(b'CCNB\0\3\xff', 'application/vnd.classicchess.notebook', 'n.ccnb')
                    result = api.account_notebook_file(NOTEBOOK, 't', password=password, ccnb_format=ccnb_format)
                    request = opener.return_value.open.call_args.args[0]
                    self.assertEqual(result.content, b'CCNB\0\3\xff')
                    self.assertEqual(request.get_header('Ccnb-format'), None if ccnb_format is None else str(ccnb_format))
                    self.assertEqual(request.get_method(), 'GET' if password is None else 'POST')
                    self.assertEqual(request.get_header('Authorization'), 'Bearer t')
                    self.assertEqual(request.full_url, 'https://classicchess.com/api/v1/account/notebooks/' + NOTEBOOK + '/file/')

    def test_invalid_ccnb_format_or_unrelated_path_never_reaches_io(self):
        api = ApplicationClient()
        with patch('classicchess_api.application.build_opener') as opener:
            for value in (1, 4, '3', True, 3.0):
                with self.subTest(value=value), self.assertRaises(ApiError):
                    api.account_notebook_file(NOTEBOOK, 't', ccnb_format=value)
            with self.assertRaises(ApiError):
                api.download('/api/v1/account/me/', token='t', ccnb_format=3)
            opener.assert_not_called()

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
        self.assert_request(lambda: api.account_dismiss_all_notifications('t'),
                            'POST', '/api/v1/account/notifications/dismiss-all/', None)
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

    def test_gym_requests_use_exact_methods_paths_and_bodies(self):
        api = ApplicationClient()
        game = '/api/v1/account/gym/games/7/'
        self.assert_request(lambda: api.account_gym('t'), 'GET', '/api/v1/account/gym/?page_size=20', None)
        self.assert_request(lambda: api.account_gym_games('t', page=2, page_size=10),
                            'GET', '/api/v1/account/gym/games/?page=2&page_size=10', None)
        self.assert_request(lambda: api.account_gym_games('t', player='others'),
                            'GET', '/api/v1/account/gym/games/?page=1&page_size=20&player=others', None)
        self.assert_request(lambda: api.account_gym_new_game('morphy', 't'),
                            'POST', '/api/v1/account/gym/bots/morphy/games/', {'color': 'white', 'time': 'unlimited'})
        self.assert_request(lambda: api.account_gym_new_game('morphy', 't', color='random', time='3+0'),
                            'POST', '/api/v1/account/gym/bots/morphy/games/', {'color': 'random', 'time': '3+0'})
        self.assert_request(lambda: api.account_gym_new_game('morphy', 't', time='custom', minutes=2, increment=1),
                            'POST', '/api/v1/account/gym/bots/morphy/games/',
                            {'color': 'white', 'time': 'custom', 'minutes': 2, 'increment': 1})
        self.assert_request(lambda: api.account_gym_new_game('morphy', 't', version='v4'),
                            'POST', '/api/v1/account/gym/bots/morphy/games/',
                            {'color': 'white', 'time': 'unlimited', 'version': 'v4'})
        self.assert_request(lambda: api.account_gym_game(7, 't'), 'GET', game, None)
        self.assert_request(lambda: api.account_gym_delete_game(7, 't'), 'DELETE', game, None)
        self.assert_request(lambda: api.account_gym_move(7, 'e7e8q', 0, 't'),
                            'POST', game + 'move/', {'move': 'e7e8q', 'ply': 0})
        self.assert_request(lambda: api.account_gym_bot_move(7, 1, 't'), 'POST', game + 'bot-move/', {'ply': 1})
        for call, action in ((api.account_gym_resign, 'resign/'), (api.account_gym_take_back, 'takeback/'),
                             (api.account_gym_clock, 'clock/'), (api.account_gym_abort, 'abort/')):
            with self.subTest(action=action):
                self.assert_request(lambda call=call: call(7, 't'), 'POST', game + action, None)

    def test_puzzle_requests_use_exact_methods_paths_and_bodies(self):
        api = ApplicationClient()
        base = '/api/v1/account/puzzles/'
        self.assert_request(lambda: api.account_puzzles('t'), 'GET', base, None)
        self.assert_request(lambda: api.account_puzzle_next('t'), 'GET', base + 'next/', None)
        self.assert_request(lambda: api.account_puzzle_next('t', after='p-000ccc1f'),
                            'GET', base + 'next/?after=p-000ccc1f', None)
        self.assert_request(lambda: api.account_puzzle('p-000ccc1f', 't'), 'GET', base + 'p-000ccc1f/', None)
        self.assert_request(lambda: api.account_puzzle_result('p-000ccc1f', 'missed', 't'),
                            'POST', base + 'p-000ccc1f/result/', {'result': 'missed'})
        self.assert_request(lambda: api.account_puzzle_history('t', page=2, page_size=10),
                            'GET', base + 'history/?page=2&page_size=10', None)
        self.assert_request(lambda: api.account_puzzle_reset('t'), 'POST', base + 'reset/', None)

    def test_puzzle_helpers_reject_invalid_arguments_before_sending(self):
        api = ApplicationClient()
        calls = [
            lambda: api.account_puzzle('../me', 't'),
            lambda: api.account_puzzle('p-000CCC1F', 't'),
            lambda: api.account_puzzle(7, 't'),
            lambda: api.account_puzzle_next('t', after='p-1'),
            lambda: api.account_puzzle_result('p-000ccc1f', 'skipped', 't'),
            lambda: api.account_puzzle_result('p-000ccc1f/..', 'solved', 't'),
            lambda: api.account_puzzle_history('t', page=0),
            lambda: api.account_puzzle_history('t', page_size=101),
        ]
        with patch('classicchess_api.application.build_opener') as opener:
            for call in calls:
                with self.subTest(call=call), self.assertRaises(ApiError):
                    call()
        opener.return_value.open.assert_not_called()

    def test_gym_pgn_downloads_and_a_busy_engine_keeps_its_retry_after(self):
        api = ApplicationClient()
        with patch('classicchess_api.application.build_opener') as opener:
            opener.return_value.open.return_value = file_reply(b'1. e4 *', 'application/x-chess-pgn', 'gym.pgn')
            pgn = api.account_gym_game_pgn(7, 't')
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://classicchess.com/api/v1/account/gym/games/7/pgn/')
            self.assertEqual((pgn.content, pgn.filename), (b'1. e4 *', 'gym.pgn'))
            reply = Reply(json.dumps({'error': {'code': 'capacity_exhausted', 'message': 'Busy.'}}).encode())
            reply.status = 429
            reply.headers = {'Retry-After': '1'}
            opener.return_value.open.return_value = reply
            busy = api.account_gym_bot_move(7, 1, 't')
        # No automatic retry: the caller decides, with the server's hint.
        self.assertEqual(opener.return_value.open.call_count, 2)
        self.assertEqual((busy.status, busy.retry_after, busy.data['error']['code']), (429, '1', 'capacity_exhausted'))

    def test_gym_helpers_reject_invalid_arguments_before_sending(self):
        api = ApplicationClient()
        calls = [
            lambda: api.account_gym('t', page_size=101),
            lambda: api.account_gym_games('t', player='everyone'),
            lambda: api.account_gym_games('t', page=0),
            lambda: api.account_gym_new_game('../me', 't'),
            lambda: api.account_gym_new_game('morphy', 't', color='green'),
            lambda: api.account_gym_new_game('morphy', 't', time=''),
            lambda: api.account_gym_new_game('morphy', 't', time='custom', minutes=0, increment=0),
            lambda: api.account_gym_new_game('morphy', 't', time='custom', minutes=True, increment=0),
            lambda: api.account_gym_new_game('morphy', 't', time='3+0', minutes=3),
            lambda: api.account_gym_game(0, 't'),
            lambda: api.account_gym_game('7', 't'),
            lambda: api.account_gym_move(7, 'e2-e4', 0, 't'),
            lambda: api.account_gym_move(7, 'e2e4', -1, 't'),
            lambda: api.account_gym_bot_move(7, True, 't'),
            lambda: api.account_gym_abort(-3, 't'),
            lambda: api.account_gym_new_game('morphy', 't', version=''),
            lambda: api.account_gym_new_game('morphy', 't', version=4),
        ]
        with patch('classicchess_api.application.build_opener') as opener:
            for call in calls:
                with self.subTest(call=call), self.assertRaises(ApiError):
                    call()
        opener.assert_not_called()

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


def sent(call, reply=OK_BODY):
    """The one request a helper sends, answered with ``reply``."""
    with patch('classicchess_api.application.build_opener') as opener:
        opener.return_value.open.return_value = Reply(reply)
        call()
        assert opener.return_value.open.call_count == 1
        return opener.return_value.open.call_args.args[0]


class AccountApiGapTests(TestCase):
    def assert_requests(self, cases, token='Bearer t'):
        for call, method, path, body in cases:
            with self.subTest(method=method, path=path):
                request = sent(call)
                self.assertEqual((request.get_method(), request.full_url), (method, 'https://classicchess.com' + path))
                self.assertEqual(request.get_header('Authorization'), token)
                self.assertEqual(json.loads(request.data) if request.data else None, body)

    def test_preferences_and_notebook_editing_send_the_contract_requests(self):
        api = ApplicationClient()
        notebook = '/api/v1/account/notebooks/' + NOTEBOOK + '/'
        chapter = notebook + 'chapters/9/'
        self.assert_requests([
            (lambda: api.account_preferences('t'), 'GET', '/api/v1/account/preferences/', None),
            (lambda: api.account_update_preferences('t', board_theme='green', engine_lines=3, engine_eval_bar=False),
             'PATCH', '/api/v1/account/preferences/', {'board_theme': 'green', 'engine_lines': 3, 'engine_eval_bar': False}),
            (lambda: api.account_create_notebook('Sicilian', 't', visibility='unlisted', game='tal-vs-larsen'),
             'POST', '/api/v1/account/notebooks/', {'title': 'Sicilian', 'visibility': 'unlisted', 'game': 'tal-vs-larsen'}),
            (lambda: api.account_rename_notebook(NOTEBOOK.upper(), 'Najdorf', 't'), 'PATCH', notebook, {'title': 'Najdorf'}),
            (lambda: api.account_create_notebook_section(NOTEBOOK, 'Openings', 't', parent_id=4),
             'POST', notebook + 'sections/', {'title': 'Openings', 'parent_id': 4}),
            (lambda: api.account_rename_notebook_section(NOTEBOOK, 4, 'Lines', 't'),
             'PATCH', notebook + 'sections/4/', {'title': 'Lines'}),
            (lambda: api.account_delete_notebook_section(NOTEBOOK, 4, 't'), 'DELETE', notebook + 'sections/4/', None),
            (lambda: api.account_move_notebook_section(NOTEBOOK, 4, 't', before_section_id=2),
             'POST', notebook + 'sections/4/move/', {'before_section_id': 2}),
            (lambda: api.account_move_notebook_section(NOTEBOOK, 4, 't'), 'POST', notebook + 'sections/4/move/', {}),
            (lambda: api.account_create_notebook_chapter(NOTEBOOK, 't', section_id=4, initial_position='8/8/8/8/8/8/8/K6k w'),
             'POST', notebook + 'chapters/', {'section_id': 4, 'initial_position': '8/8/8/8/8/8/8/K6k w'}),
            (lambda: api.account_notebook_chapter(NOTEBOOK, 9, 't'), 'GET', chapter, None),
            (lambda: api.account_save_notebook_chapter(NOTEBOOK, 9, '1. e4 *', 3, 't'),
             'PUT', chapter, {'pgn': '1. e4 *', 'content_version': 3}),
            (lambda: api.account_save_notebook_chapter(NOTEBOOK, 9, '1. d4 *', 4, 't', title='Queen pawn'),
             'PUT', chapter, {'pgn': '1. d4 *', 'content_version': 4, 'title': 'Queen pawn'}),
            (lambda: api.account_rename_notebook_chapter(NOTEBOOK, 9, 'Main line', 't'), 'PATCH', chapter, {'title': 'Main line'}),
            (lambda: api.account_delete_notebook_chapter(NOTEBOOK, 9, 't'), 'DELETE', chapter, None),
            (lambda: api.account_move_notebook_chapter(NOTEBOOK, 9, 't', section_id=4, before_chapter_id=2),
             'POST', chapter + 'move/', {'section_id': 4, 'before_chapter_id': 2}),
            (lambda: api.account_delete_notebook(NOTEBOOK, 't'), 'DELETE', notebook, None),
            (lambda: api.account_notebook_settings(NOTEBOOK, 't'), 'GET', notebook + 'settings/', None),
            (lambda: api.account_update_notebook_settings(NOTEBOOK, 't', visibility='public', allow_public_exports=False),
             'PATCH', notebook + 'settings/', {'visibility': 'public', 'allow_public_exports': False}),
            (lambda: api.account_set_notebook_collaborator(NOTEBOOK, 'ann', 'editor', 't', can_present_live=True),
             'POST', notebook + 'collaborators/', {'username': 'ann', 'role': 'editor', 'can_present_live': True}),
            (lambda: api.account_remove_notebook_collaborator(NOTEBOOK, 12, 't'), 'DELETE', notebook + 'collaborators/12/', None),
            (lambda: api.account_leave_notebook(NOTEBOOK, 't'), 'DELETE', notebook + 'membership/', None),
            (lambda: api.account_notebook_share(NOTEBOOK, 't'), 'GET', notebook + 'share/', None),
            (lambda: api.account_create_notebook_share(NOTEBOOK, 't'), 'PUT', notebook + 'share/', None),
            (lambda: api.account_revoke_notebook_share(NOTEBOOK, 't'), 'DELETE', notebook + 'share/', None),
            (lambda: api.account_notebook_chapter_share(NOTEBOOK, 9, 't'), 'GET', chapter + 'share/', None),
            (lambda: api.account_create_notebook_chapter_share(NOTEBOOK, 9, 't'), 'PUT', chapter + 'share/', None),
            (lambda: api.account_revoke_notebook_chapter_share(NOTEBOOK, 9, 't'), 'DELETE', chapter + 'share/', None),
        ])

    def test_a_stale_chapter_save_returns_the_conflict_without_retrying(self):
        conflict = Reply(json.dumps({'error': {'code': 'chapter_conflict', 'message': 'Changed elsewhere.'}}).encode())
        conflict.status = 409
        with patch('classicchess_api.application.build_opener') as opener:
            opener.return_value.open.return_value = conflict
            saved = ApplicationClient().account_save_notebook_chapter(NOTEBOOK, 9, '1. e4 *', 3, 't')
        self.assertEqual((saved.ok, saved.status, saved.data['error']['code']), (False, 409, 'chapter_conflict'))
        self.assertEqual(opener.return_value.open.call_count, 1)

    def test_mobile_device_requests_send_the_contract_requests(self):
        api = ApplicationClient()
        self.assert_requests([
            (lambda: api.mobile_sign_out('t'), 'POST', '/api/v1/account/mobile/logout/', None),
            (lambda: api.mobile_sessions('t'), 'GET', '/api/v1/account/mobile/sessions/', None),
            (lambda: api.mobile_revoke_session(5, 't'), 'POST', '/api/v1/account/mobile/sessions/5/revoke/', None),
            (lambda: api.mobile_revoke_all_sessions('t'), 'POST', '/api/v1/account/mobile/sessions/revoke-all/', None),
            (lambda: api.mobile_settings('t'), 'GET', '/api/v1/account/mobile/settings/', None),
            (lambda: api.mobile_update_settings('t', piece_set='merida', cast_cc_emails=['coach@example.test']),
             'POST', '/api/v1/account/mobile/settings/', {'piece_set': 'merida', 'cast_cc_emails': ['coach@example.test']}),
        ])
        self.assert_requests([
            (lambda: api.mobile_home(), 'GET', '/api/v1/mobile/home/', None),
            (lambda: api.mobile_android_release(), 'GET', '/api/v1/mobile/android/release/', None),
            (lambda: api.mobile_sign_in('ann', 'test-only-password', two_factor_code='123456', device_name='Pixel'),
             'POST', '/api/v1/account/mobile/login/',
             {'username': 'ann', 'password': 'test-only-password', 'two_factor_code': '123456', 'device_name': 'Pixel'}),
        ], token=None)

    def test_mobile_sign_in_sends_passwords_only_over_https_or_loopback(self):
        with patch('classicchess_api.application.build_opener') as opener:
            with self.assertRaises(ApiError) as caught:
                ApplicationClient('http://classicchess.example').mobile_sign_in('ann', 'test-only-password')
            opener.assert_not_called()
        self.assertEqual(caught.exception.code, 'unsafe_credentials')
        request = sent(lambda: ApplicationClient('http://127.0.0.1:8123').mobile_sign_in('ann', 'test-only-password'))
        self.assertEqual(request.full_url, 'http://127.0.0.1:8123/api/v1/account/mobile/login/')

    def test_api_gap_application_helpers_reject_unsafe_arguments_before_sending(self):
        api = ApplicationClient()
        calls = [
            lambda: api.account_update_preferences('t'),
            lambda: api.account_update_preferences('t', board_theme=None),
            lambda: api.account_create_notebook(' ', 't'),
            lambda: api.account_create_notebook('Sicilian', 't', visibility='secret'),
            lambda: api.account_rename_notebook('../me', 'x', 't'),
            lambda: api.account_create_notebook_section(NOTEBOOK, 'x', 't', parent_id=0),
            lambda: api.account_rename_notebook_section(NOTEBOOK, '4', 'x', 't'),
            lambda: api.account_move_notebook_section(NOTEBOOK, 4, 't', before_section_id=True),
            lambda: api.account_notebook_chapter(NOTEBOOK, 0, 't'),
            lambda: api.account_save_notebook_chapter(NOTEBOOK, 9, '1. e4 *', 0, 't'),
            lambda: api.account_save_notebook_chapter(NOTEBOOK, 9, '1. e4 *', True, 't'),
            lambda: api.account_save_notebook_chapter(NOTEBOOK, 9, None, 3, 't'),
            lambda: api.account_move_notebook_chapter(NOTEBOOK, 9, 't', section_id=-1),
            lambda: api.account_update_notebook_settings(NOTEBOOK, 't'),
            lambda: api.account_set_notebook_collaborator(NOTEBOOK, 'ann', 'owner', 't'),
            lambda: api.account_remove_notebook_collaborator(NOTEBOOK, '12', 't'),
            lambda: api.account_revoke_notebook_chapter_share(NOTEBOOK, -9, 't'),
            lambda: api.mobile_revoke_session(0, 't'),
            lambda: api.mobile_update_settings('t'),
            lambda: api.mobile_sign_in('ann', ''),
        ]
        with patch('classicchess_api.application.build_opener') as opener:
            for call in calls:
                with self.subTest(call=call), self.assertRaises(ApiError):
                    call()
        opener.assert_not_called()
