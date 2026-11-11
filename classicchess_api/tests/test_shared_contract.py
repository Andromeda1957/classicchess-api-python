"""The same wire fixtures run through Python, TypeScript and Kotlin clients."""

import json
from argparse import Namespace
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from classicchess_api import ApplicationClient
from classicchess_api import client as packaged
from classicchess_api.tests.test_application import file_reply
from classicchess_api.tests.test_robustness import Response
from classicchess_api.tests.test_robustness import standalone_client

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / 'contracts/client-fixtures.json'
if not FIXTURE_PATH.is_file():
    FIXTURE_PATH = ROOT.parents[1] / 'docs/api/client-fixtures.json'
FIXTURES = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
USERNAMES = FIXTURES['imported_game_usernames']


class SharedClientContractTests(TestCase):
    def clients(self):
        return (packaged, standalone_client())

    def collect(self, module, *, limit=None):
        if module is packaged:
            return module.ClassicChessClient().master_games(query='Tal', all_pages=True, limit=limit, page_size=1)
        args = Namespace(base_url='https://classicchess.com', page=1, page_size=1, all_pages=True, limit=limit)
        return module.collect_master_game_pages(args, 'Tal')

    def test_shared_growing_pagination_sequences(self):
        for module in self.clients():
            for case in FIXTURES['page_sequences']:
                with self.subTest(client=module.__name__, case=case['name']):
                    replies = [Response(json.dumps(page).encode()) for page in case['pages']]
                    with patch.object(module, 'urlopen', side_effect=replies) as wire:
                        result = self.collect(module)
                    self.assertEqual([row['token'] for row in result['results']], case['expected_tokens'])
                    self.assertEqual(wire.call_count, len(case['pages']))
                    self.assertTrue(all(reply.closed for reply in replies))

    def test_shared_missing_data_and_malformed_success_responses(self):
        for module in self.clients():
            for case in FIXTURES['invalid_pages']:
                with self.subTest(client=module.__name__, case=case['name']):
                    with patch.object(module, 'urlopen', return_value=Response(case['body'].encode())):
                        with self.assertRaises(module.ApiError) as caught:
                            self.collect(module)
                    self.assertEqual(caught.exception.code, case['code'])

    def test_shared_http_errors_retain_status_and_retry_after(self):
        for module in self.clients():
            for case in FIXTURES['http_errors']:
                with self.subTest(client=module.__name__, case=case['name']):
                    headers = {'Retry-After': case['retry_after']} if case['retry_after'] is not None else {}
                    error = HTTPError('https://classicchess.com/api/v1/games/', case['status'], 'Failure', headers, Response(case['body'].encode()))
                    with patch.object(module, 'urlopen', side_effect=error):
                        with self.assertRaises(module.ApiError) as caught:
                            self.collect(module)
                    self.assertEqual(caught.exception.code, case['code'])
                    self.assertEqual(caught.exception.status_code, case['status'])
                    self.assertEqual(caught.exception.retry_after, case['retry_after'])

    def test_stopping_or_interrupting_never_fetches_the_next_page(self):
        class InterruptedResponse(Response):
            def read(self, size=-1):
                raise KeyboardInterrupt()

        for module in self.clients():
            with self.subTest(client=module.__name__):
                response = Response(json.dumps(FIXTURES['page_sequences'][0]['pages'][0]).encode())
                with patch.object(module, 'urlopen', return_value=response) as wire:
                    result = self.collect(module, limit=FIXTURES['stop_after_first']['items'])
                self.assertEqual(len(result['results']), FIXTURES['stop_after_first']['items'])
                self.assertEqual(wire.call_count, FIXTURES['stop_after_first']['requests'])
                interrupted = InterruptedResponse(b'')
                with patch.object(module, 'urlopen', return_value=interrupted) as wire:
                    with self.assertRaises(KeyboardInterrupt):
                        self.collect(module)
                self.assertTrue(interrupted.closed)
                self.assertEqual(wire.call_count, 1)


class ImportedGameUsernameTests(TestCase):
    """Every username the server accepts reaches the imported-game routes; nothing else is sent."""

    def requested_paths(self, username):
        sent = []

        def reply(request, timeout):
            sent.append(urlsplit(request.full_url).path)
            return Response(b'{}')

        api = packaged.ClassicChessClient()
        with patch.object(packaged, 'urlopen', side_effect=reply):
            api.public_imported_game(username, 'g1')
            api.public_imported_pgn(username, 'g1')
        with patch('classicchess_api.application.build_opener') as opener:
            opener.return_value.open.return_value = file_reply(b'GIF89a', 'image/gif', 'game.gif')
            ApplicationClient().public_imported_game_gif(username, 'g1', 't')
        sent.append(urlsplit(opener.return_value.open.call_args.args[0].full_url).path)
        return sent

    def test_server_usernames_in_any_script_reach_the_imported_game_routes(self):
        for case in USERNAMES['accepted']:
            with self.subTest(username=case['username']):
                game = '/api/v1/public/imported-games/' + case['path_segment'] + '/g1/'
                self.assertEqual(self.requested_paths(case['username']), [game, game + 'pgn/', game + 'gif/'])
        for character in USERNAMES['length_characters']:
            with self.subTest(longest=character):
                self.assertEqual(len(self.requested_paths(character * USERNAMES['max_length'])), 3)

    def test_other_names_fail_before_any_request(self):
        too_long = [character * (USERNAMES['max_length'] + 1) for character in USERNAMES['length_characters']]
        api = packaged.ClassicChessClient()
        calls = ((api.public_imported_game, ()), (api.public_imported_pgn, ()),
                 (ApplicationClient().public_imported_game_gif, ('t',)))
        for username in [case['username'] for case in USERNAMES['rejected']] + too_long:
            with self.subTest(username=username[:8]), patch.object(packaged, 'urlopen') as wire, \
                    patch('classicchess_api.application.build_opener') as opener:
                for call, token in calls:
                    with self.assertRaises(packaged.ApiError):
                        call(username, 'g1', *token)
                self.assertFalse(wire.called)
                self.assertFalse(opener.return_value.open.called)
