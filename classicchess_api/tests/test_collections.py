"""Collection helpers use scoped endpoints and preserve bulk continuation state."""

import json
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs
from urllib.parse import urlsplit

from classicchess_api import ApiError
from classicchess_api import ApplicationClient
from classicchess_api import ClassicChessClient
from classicchess_api.tests.test_application import Reply


class CollectionHelperTests(TestCase):
    def test_application_lookup_delete_and_bulk_cursor_use_the_wire_contract(self):
        api = ApplicationClient()
        reply = {'matched': 2, 'added': 1, 'has_more': True, 'next_after_game_id': 87}
        with patch('classicchess_api.application.build_opener') as opener:
            def respond(request, **kwargs):
                return Reply(json.dumps(reply).encode())
            opener.return_value.open.side_effect = respond
            self.assertEqual(api.account_find_collection('  1920 & café?  ', 'fixture').data, reply)
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(parse_qs(urlsplit(request.full_url).query), {'name': ['1920 & café?']})
            api.account_delete_collection(12, 'fixture')
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual((request.method, urlsplit(request.full_url).path), ('DELETE', '/api/v1/account/collections/12/'))
            filters = [{'players': ['alekhine'], 'since': 1920, 'until': 1920}, {'notable': True}]
            for after, dry in ((0, True), (87, False)):
                result = api.account_add_curated_games(12, filters, 'fixture', exclude=[{'events': ['london-1922']}],
                                                       batch_size=1, after_game_id=after, dry_run=dry)
                self.assertEqual(result.data, reply)
                request = opener.return_value.open.call_args.args[0]
                self.assertEqual(request.headers['Authorization'], 'Bearer fixture')
                self.assertEqual(json.loads(request.data), {'source': 'curated_games', 'collection_id': 12,
                    'filters': filters, 'exclude': [{'events': ['london-1922']}], 'batch_size': 1,
                    'after_game_id': after, 'dry_run': dry})
            self.assertEqual(opener.return_value.open.call_count, 4)

    def test_legacy_client_helpers_use_existing_authenticated_transport(self):
        api = ClassicChessClient(api_token='fixture')
        with patch.object(api, 'fetch_json', return_value={'ok': True}) as wire:
            self.assertEqual(api.account_find_collection('A & B'), {'ok': True})
            self.assertEqual(parse_qs(urlsplit(wire.call_args.args[0]).query), {'name': ['A & B']})
            api.account_delete_collection(12)
            self.assertEqual(wire.call_args.kwargs['method'], 'DELETE')
            api.account_add_curated_games(12, [{'notable': False}], after_game_id=87, batch_size=3, dry_run=True)
            self.assertEqual(json.loads(wire.call_args.kwargs['body']), {'source': 'curated_games', 'collection_id': 12,
                'filters': [{'notable': False}], 'exclude': [], 'batch_size': 3, 'after_game_id': 87, 'dry_run': True})
            self.assertEqual(wire.call_args.kwargs['headers']['Authorization'], 'Bearer fixture')

    def test_invalid_ids_names_and_batch_controls_fail_before_networking(self):
        for api in (ApplicationClient(), ClassicChessClient(api_token='fixture')):
            token = ('fixture',) if isinstance(api, ApplicationClient) else ()
            with patch('classicchess_api.application.build_opener') as wire, patch.object(ClassicChessClient, 'fetch_json') as legacy:
                for value in (0, -1, True, '12', 1.5):
                    with self.subTest(value=value), self.assertRaises(ApiError):
                        api.account_delete_collection(value, *token)
                for name in (' ', 'x' * 121):
                    with self.assertRaises(ApiError):
                        api.account_find_collection(name, *token)
                for controls in ({'batch_size': 0}, {'batch_size': 1001}, {'after_game_id': -1},
                                 {'after_game_id': True}, {'dry_run': 'false'}):
                    with self.assertRaises(ApiError):
                        api.account_add_curated_games(12, [{'notable': True}], *token, **controls)
                wire.assert_not_called()
                legacy.assert_not_called()


class LibraryApiGapTests(TestCase):
    def test_library_operations_send_the_contract_requests(self):
        from classicchess_api.tests.test_application import sent

        api = ApplicationClient()
        collection = 'https://classicchess.com/api/v1/account/collections/12/'
        cases = [
            (lambda: api.account_collection_games(12, 'fixture', page=2, page_size=20, sort='player', q='Ruy Lopez'),
             'GET', collection + '?page=2&page_size=20&sort=player&q=Ruy+Lopez', None),
            (lambda: api.account_collection_games(12, 'fixture'), 'GET', collection + '?page=1&page_size=50', None),
            (lambda: api.account_update_collection(12, 'fixture', name='Endgames', is_plan=True),
             'PATCH', collection, {'name': 'Endgames', 'is_plan': True}),
            (lambda: api.account_update_collection_item(12, 7, 'fixture', note='Rook ending', studied=False),
             'PATCH', collection + 'items/7/', {'note': 'Rook ending', 'studied': False}),
            (lambda: api.account_remove_collection_item(12, 7, 'fixture'), 'DELETE', collection + 'items/7/', None),
            (lambda: api.account_remove_collection_game(12, 'tal-vs-larsen', 'fixture'),
             'DELETE', collection + 'games/tal-vs-larsen/', None),
            (lambda: api.account_create_plan('Tal', ['tal-vs-larsen', {'game': 'tal-vs-smyslov', 'note': 'Sacrifice'}],
                                             'plan-1', 'fixture', description='Attacks'),
             'POST', 'https://classicchess.com/api/v1/account/plans/',
             {'name': 'Tal', 'description': 'Attacks', 'idempotency_key': 'plan-1',
              'items': [{'game': 'tal-vs-larsen'}, {'game': 'tal-vs-smyslov', 'note': 'Sacrifice'}]}),
            (lambda: api.account_reorder_collection(12, (9, 7, 8), 'fixture'),
             'PUT', collection + 'reorder/', {'item_ids': [9, 7, 8]}),
            (lambda: api.account_plan_progress(12, 'fixture', page=3), 'GET', collection + 'progress/?page=3&page_size=50', None),
            (lambda: api.account_toggle_player_bookmark('tal', 'fixture'),
             'POST', 'https://classicchess.com/api/v1/account/players/tal/bookmark/', None),
            (lambda: api.account_game_library('tal-vs-larsen', 'fixture'),
             'GET', 'https://classicchess.com/api/v1/account/games/tal-vs-larsen/library/', None),
            (lambda: api.account_imported_games('fixture', q='Tal 1960', source='lichess', sort='date_desc'),
             'GET', 'https://classicchess.com/api/v1/account/imported-games/?page=1&page_size=50'
                    '&q=Tal+1960&source=lichess&sort=date_desc', None),
            (lambda: api.account_import_game('pgn', 'fixture', pgn='1. e4 e5 *', visibility='private'),
             'POST', 'https://classicchess.com/api/v1/account/imported-games/',
             {'source': 'pgn', 'pgn': '1. e4 e5 *', 'visibility': 'private'}),
            (lambda: api.account_import_game('lichess', 'fixture', remote_url='https://lichess.org/abcdefgh'),
             'POST', 'https://classicchess.com/api/v1/account/imported-games/',
             {'source': 'lichess', 'remote_url': 'https://lichess.org/abcdefgh'}),
            (lambda: api.account_imported_game('ann-vs-bob-1', 'fixture'),
             'GET', 'https://classicchess.com/api/v1/account/imported-games/ann-vs-bob-1/', None),
        ]
        for call, method, url, body in cases:
            with self.subTest(method=method, url=url):
                request = sent(call)
                self.assertEqual((request.get_method(), request.full_url), (method, url))
                self.assertEqual(request.get_header('Authorization'), 'Bearer fixture')
                self.assertEqual(json.loads(request.data) if request.data else None, body)

    def test_library_operations_reject_unsafe_arguments_before_networking(self):
        api = ApplicationClient()
        calls = [
            lambda: api.account_collection_games(0, 'fixture'),
            lambda: api.account_collection_games(12, 'fixture', sort='random'),
            lambda: api.account_collection_games(12, 'fixture', page_size=101),
            lambda: api.account_update_collection(12, 'fixture'),
            lambda: api.account_update_collection(12, 'fixture', sort='rating'),
            lambda: api.account_update_collection_item(12, 7, 'fixture'),
            lambda: api.account_update_collection_item(12, '7', 'fixture', studied=True),
            lambda: api.account_remove_collection_item(12, True, 'fixture'),
            lambda: api.account_remove_collection_game(12, '../12', 'fixture'),
            lambda: api.account_create_plan('Tal', [], 'plan-1', 'fixture'),
            lambda: api.account_create_plan('Tal', 'tal-vs-larsen', 'plan-1', 'fixture'),
            lambda: api.account_create_plan('Tal', [{'note': 'x'}], 'plan-1', 'fixture'),
            lambda: api.account_create_plan('Tal', ['g'] * 101, 'plan-1', 'fixture'),
            lambda: api.account_create_plan('Tal', ['g'], ' ', 'fixture'),
            lambda: api.account_create_plan(' ', ['g'], 'plan-1', 'fixture'),
            lambda: api.account_reorder_collection(12, [7, 0], 'fixture'),
            lambda: api.account_reorder_collection(12, '78', 'fixture'),
            lambda: api.account_plan_progress(12, 'fixture', page=0),
            lambda: api.account_toggle_player_bookmark('tal/../x', 'fixture'),
            lambda: api.account_game_library('', 'fixture'),
            lambda: api.account_imported_games('fixture', source='pdf'),
            lambda: api.account_imported_games('fixture', sort='rating'),
            lambda: api.account_import_game('chessbase', 'fixture', pgn='1. e4 *'),
            lambda: api.account_import_game('pgn', 'fixture', pgn='1. e4 *', visibility='unlisted'),
            lambda: api.account_imported_game('a/b', 'fixture'),
        ]
        with patch('classicchess_api.application.build_opener') as opener:
            for call in calls:
                with self.subTest(call=call), self.assertRaises(ApiError):
                    call()
        opener.assert_not_called()
