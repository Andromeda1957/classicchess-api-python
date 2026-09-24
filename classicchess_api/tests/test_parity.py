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

    def test_site_page_reads_request_versioned_public_routes(self):
        api = ClassicChessClient()
        cases = [
            (lambda: api.gallery('tal', page=2, page_size=12),
             '/api/v1/public/gallery/?q=tal&page=2&page_size=12'),
            (lambda: api.gallery_photo(67217590), '/api/v1/public/gallery/67217590/'),
            (lambda: api.beginner_games(), '/api/v1/public/beginner-games/'),
            (lambda: api.daily_game(), '/api/v1/public/daily/'),
            (lambda: api.public_imported_game('ann.lee+1', 'g1'), '/api/v1/public/imported-games/ann.lee+1/g1/'),
            (lambda: api.public_event('wcc-1972'), '/api/v1/public/events/wcc-1972/'),
            (lambda: api.public_event_about('wcc-1972'), '/api/v1/public/events/wcc-1972/about/'),
            (lambda: api.site_search('fischer spassky'), '/api/v1/public/search/?q=fischer+spassky'),
            (lambda: api.site_search('tal', kind='games', page=2), '/api/v1/public/search/?q=tal&kind=games&page=2'),
            (lambda: api.tablebase('8/8/8/8/8/2k5/2P5/2K5 w - - 0 1'),
             '/api/v1/tablebase/?fen=8%2F8%2F8%2F8%2F8%2F2k5%2F2P5%2F2K5+w+-+-+0+1'),
        ]
        for call, path in cases:
            with self.subTest(path=path), patch.object(
                api, 'fetch_url', return_value=(b'{"source": "public"}', 'application/json'),
            ) as wire:
                self.assertEqual(call(), {'source': 'public'})
                self.assertEqual(wire.call_args.args[0], 'https://classicchess.com' + path)

    def test_public_imported_pgn_reads_text(self):
        api = ClassicChessClient()
        with patch.object(api, 'fetch_url', return_value=(b'1. e4 *', 'application/x-chess-pgn')) as wire:
            self.assertEqual(api.public_imported_pgn('ann', 'g1'), '1. e4 *')
        self.assertEqual(wire.call_args.args[0], 'https://classicchess.com/api/v1/public/imported-games/ann/g1/pgn/')

    def test_site_page_reads_reject_unsafe_input_before_any_request(self):
        api = ClassicChessClient()
        calls = [
            lambda: api.public_event('../account/me'), lambda: api.public_event_about('a/b'),
            lambda: api.gallery_photo('x?y'), lambda: api.site_search(''), lambda: api.site_search('x' * 121),
            lambda: api.site_search('tal', kind='users'), lambda: api.site_search('tal', page=2),
            lambda: api.tablebase(''), lambda: api.tablebase('k' * 201),
            lambda: api.public_imported_game('..', 'g1'), lambda: api.public_imported_game('a/b', 'g1'),
            lambda: api.public_imported_pgn('ann', '../me'),
        ]
        with patch.object(api, 'fetch_url') as wire:
            for call in calls:
                with self.subTest(call=call), self.assertRaises(ApiError):
                    call()
        wire.assert_not_called()
