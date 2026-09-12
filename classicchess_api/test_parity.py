"""Capability requests and streaming match the other maintained SDKs."""

import json
from unittest import TestCase
from unittest.mock import patch

from classicchess_api import ApiError
from classicchess_api import ClassicChessClient


class CapabilityParityTests(TestCase):
    def test_discovery_event_series_and_catalog_names(self):
        api = ClassicChessClient()
        cases = [
            (lambda: api.discovery(), {}, '/api/v1/', {}),
            (lambda: api.event_series(series='world-championship'), {},
             '/api/v1/public/event-series/?series=world-championship', {}),
            (lambda: api.player_names(), {'results': [{'name': 'Tal'}]},
             '/api/v1/public/players/', ['Tal']),
            (lambda: api.event_names(), {'results': [{'name': 'London'}]},
             '/api/v1/public/events/', ['London']),
            (lambda: api.book_titles(), {'results': [{'label': 'Chess'}]},
             '/api/v1/annotated/books/', ['Chess']),
        ]
        for call, payload, path, expected in cases:
            with self.subTest(path=path), patch.object(
                api, 'fetch_url', return_value=(json.dumps(payload).encode(), 'application/json'),
            ) as wire:
                self.assertEqual(call(), expected)
                self.assertEqual(wire.call_args.args[0], 'https://classicchess.com' + path)

    def test_all_game_iterators_are_lazy_and_stop_without_an_extra_request(self):
        api = ClassicChessClient()
        for iterator in (lambda: api.iterate_public_games(limit=1),
                         lambda: api.iterate_master_games('Tal', limit=1),
                         lambda: api.iterate_annotated_games('book', limit=1)):
            with self.subTest(iterator=iterator), patch.object(api, 'fetch_json') as fetch:
                fetch.return_value = {'results': [{'token': 'one'}, {'token': 'two'}], 'next': '?page=2'}
                games = iterator()
                fetch.assert_not_called()
                self.assertEqual(list(games), [{'token': 'one'}])
                self.assertEqual(fetch.call_count, 1)

    def test_annotated_collection_follows_next_pages(self):
        api = ClassicChessClient()
        pages = [{'results': [{'token': 'one'}], 'next': '?page=2'},
                 {'results': [{'token': 'two'}], 'next': None}]
        with patch.object(api, 'fetch_json', side_effect=pages) as fetch:
            result = api.annotated_games('book', all_pages=True)
        self.assertEqual([game['token'] for game in result['results']], ['one', 'two'])
        self.assertEqual(fetch.call_count, 2)

    def test_iterators_reject_unsafe_or_malformed_continuations_before_fetching_them(self):
        api = ClassicChessClient()
        for next_url in ('https://elsewhere.test/api/v1/public/games/',
                         '/api/v1/account/me/', '', 42, False):
            with self.subTest(next_url=next_url), patch.object(api, 'fetch_json') as fetch:
                fetch.return_value = {'results': [], 'next': next_url}
                with self.assertRaises(ApiError):
                    list(api.iterate_public_games())
                self.assertEqual(fetch.call_count, 1)

    def test_iterator_detects_a_repeated_first_page_and_honors_page_bound(self):
        api = ClassicChessClient()
        for next_url in ('?page=1&page_size=50&sort=asc', '?page=2'):
            with self.subTest(next_url=next_url), patch.object(api, 'fetch_json') as fetch:
                fetch.return_value = {'results': [], 'next': next_url}
                with self.assertRaises(ApiError):
                    list(api.iterate_public_games(max_pages=1))
                self.assertEqual(fetch.call_count, 1)
