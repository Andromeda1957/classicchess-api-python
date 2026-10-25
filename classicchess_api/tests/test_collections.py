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
