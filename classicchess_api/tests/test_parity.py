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
             '/api/v1/public/players/cards/', ['Tal']),
            (lambda: api.public_player_cards('tal'), {'results': [{'name': 'Tal'}]},
             '/api/v1/public/players/cards/?q=tal', {'results': [{'name': 'Tal'}]}),
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
            (lambda: api.event_of_the_day(), '/api/v1/public/event-of-the-day/'),
            (lambda: api.todays_picks(), '/api/v1/public/todays-picks/'),
            (lambda: api.public_imported_game('ann.lee+1', 'g1'), '/api/v1/public/imported-games/ann.lee+1/g1/'),
            (lambda: api.public_event('wcc-1972'), '/api/v1/public/events/wcc-1972/'),
            (lambda: api.public_event_about('wcc-1972'), '/api/v1/public/events/wcc-1972/about/'),
            (lambda: api.site_search('fischer spassky'), '/api/v1/public/search/?q=fischer+spassky'),
            (lambda: api.site_search('tal', kind='games', page=2), '/api/v1/public/search/?q=tal&kind=games&page=2'),
            (lambda: api.tablebase('8/8/8/8/8/2k5/2P5/2K5 w - - 0 1'),
             '/api/v1/tablebase/?fen=8%2F8%2F8%2F8%2F8%2F2k5%2F2P5%2F2K5+w+-+-+0+1'),
            (lambda: api.lichess_explorer(), '/api/v1/opening-explorer/lichess/'),
            (lambda: api.lichess_explorer(fen='8/8/8/8/8/2k5/2P5/2K5 w - - 0 1', player='DrNykterstein',
                                          color='white', result='win'),
             '/api/v1/opening-explorer/lichess/?fen=8%2F8%2F8%2F8%2F8%2F2k5%2F2P5%2F2K5+w+-+-+0+1'
             '&player=DrNykterstein&color=white&result=win'),
        ]
        for call, path in cases:
            with self.subTest(path=path), patch.object(
                api, 'fetch_url', return_value=(b'{"source": "public"}', 'application/json'),
            ) as wire:
                self.assertEqual(call(), {'source': 'public'})
                self.assertEqual(wire.call_args.args[0], 'https://classicchess.com' + path)

    def test_api_gap_reads_send_the_contract_paths_and_filters(self):
        api = ClassicChessClient()
        fen = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'
        fen_query = 'fen=rnbqkbnr%2Fpppppppp%2F8%2F8%2F4P3%2F8%2FPPPP1PPP%2FRNBQKBNR+b+KQkq+-+0+1'
        cases = [
            (lambda: api.chess_today(), '/api/v1/public/chess-today/'),
            (lambda: api.chess_today_on_date(2, 29), '/api/v1/public/chess-today/2/29/'),
            (lambda: api.study_themes(), '/api/v1/public/themes/'),
            (lambda: api.site_updates(page=2, page_size=10), '/api/v1/public/site-updates/?page=2&page_size=10'),
            (lambda: api.site_update('2026-11-12-gallery'), '/api/v1/public/site-updates/2026-11-12-gallery/'),
            (lambda: api.site_search('najdorf', kind='site', page=2),
             '/api/v1/public/search/?q=najdorf&kind=site&page=2'),
            (lambda: api.shared_gym_game('42.Ab_c-1'), '/api/v1/public/gym/games/42.Ab_c-1/'),
            (lambda: api.lichess_explorer_game('AbCd1234'), '/api/v1/opening-explorer/lichess/games/AbCd1234/'),
            (lambda: api.player_explorer('Tal', fen=fen, color='white', result='win', page=2, since=1960, until=1970),
             '/api/v1/opening-explorer/player/?player=Tal&' + fen_query
             + '&color=white&result=win&page=2&since=1960&until=1970'),
            (lambda: api.explorer(play='e2e4', since=1950, until=1960),
             '/api/v1/opening-explorer/?play=e2e4&moves=12&since=1950&until=1960'),
            (lambda: api.lichess_explorer(player='DrNykterstein', since=2020, until=2021),
             '/api/v1/opening-explorer/lichess/?player=DrNykterstein&color=both&result=all&since=2020&until=2021'),
            (lambda: api.public_games(archive_player='mikhail-tal', theme=['king-attack', 'sacrifice'], eco='B9',
                                      result='1/2-1/2', annotation_q='sacrifice', annotation_author='Tal',
                                      annotation_book='Life'),
             '/api/v1/public/games/?archive_player=mikhail-tal&sort=asc&theme=king-attack%2Csacrifice&eco=B9'
             '&result=1%2F2-1%2F2&annotation_q=sacrifice&annotation_author=Tal&annotation_book=Life'
             '&page=1&page_size=50'),
            (lambda: api.annotated_games('my-system', q='blockade', author='Nimzowitsch', header_q='Riga',
                                         sort='desc', page=2),
             '/api/v1/annotated/books/my-system/games/?q=blockade&author=Nimzowitsch&header_q=Riga&sort=desc'
             '&page=2&page_size=50'),
        ]
        for call, path in cases:
            with self.subTest(path=path), patch.object(
                api, 'fetch_url', return_value=(b'{"source": "public"}', 'application/json'),
            ) as wire:
                self.assertEqual(call(), {'source': 'public'})
                self.assertEqual(wire.call_args.args[0], 'https://classicchess.com' + path)

    def test_game_filters_reach_iteration_and_query_exports(self):
        api = ClassicChessClient()
        with patch.object(api, 'fetch_json', return_value={'results': [], 'next': None}) as fetch:
            list(api.iterate_public_games(theme='sacrifice', result='0-1', limit=1))
            list(api.iterate_annotated_games('my-system', header_q='Riga', sort='desc'))
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [
            'https://classicchess.com/api/v1/public/games/?sort=asc&theme=sacrifice&result=0-1&page=1&page_size=50',
            'https://classicchess.com/api/v1/annotated/books/my-system/games/?header_q=Riga&sort=desc'
            '&page=1&page_size=50',
        ])
        with patch.object(api, 'fetch_url', return_value=(b'[Event "x"]', 'application/x-chess-pgn')) as wire:
            api.export_public_games(query='tal', eco='B', annotation_book='Life')
        self.assertEqual(wire.call_args.args[0], 'https://classicchess.com/api/v1/public/games/export/'
                         '?format=pgn&sort=asc&eco=B&annotation_book=Life&q=tal')

    def test_token_exports_post_one_token_per_line(self):
        api = ClassicChessClient(api_token='fixture')
        cases = [
            (lambda: api.export_public_games_by_token(
                ['tal-vs-larsen-1965', 'https://classicchess.com/mikhail-tal/games/tal-vs-x/'],
                sort='desc', format='ndjson', pgn_in_json=False),
             '/api/v1/public/games/export/?format=ndjson&sort=desc&pgnInJson=false',
             b'tal-vs-larsen-1965\nmikhail-tal/tal-vs-x'),
            (lambda: api.export_master_games_by_token(
                ['g1a2-0123456789ab', 'https://classicchess.com/games/g3b4-0123456789ab/']),
             '/api/v1/games/export/?format=pgn', b'g1a2-0123456789ab\ng3b4-0123456789ab'),
        ]
        for call, path, body in cases:
            with self.subTest(path=path), patch.object(api, 'fetch_url', return_value=(b'1. e4 *', 'text/plain')) as wire:
                self.assertEqual(call(), '1. e4 *')
                self.assertEqual(wire.call_args.args[0], 'https://classicchess.com' + path)
                self.assertEqual((wire.call_args.kwargs['method'], wire.call_args.kwargs['body']), ('POST', body))
                self.assertEqual(wire.call_args.kwargs['headers'], {'Content-Type': 'text/plain; charset=utf-8'})

    def test_game_link_and_book_package_send_no_account_credentials(self):
        api = ClassicChessClient(api_token='fixture')
        link = 'https://lichess.org/abcdefgh/black'
        with patch.object(api, 'fetch_url', return_value=(b'{"pgn": "1. e4 *", "orientation": "black"}',
                                                          'application/json')) as wire:
            self.assertEqual(api.resolve_game_link(link), {'pgn': '1. e4 *', 'orientation': 'black'})
        self.assertEqual(wire.call_args.args[0], 'https://classicchess.com/api/v1/analysis/game-link/')
        self.assertEqual(wire.call_args.kwargs['method'], 'POST')
        self.assertEqual(json.loads(wire.call_args.kwargs['body']), {'url': link})
        self.assertEqual(wire.call_args.kwargs['headers'],
                         {'Accept': 'application/json', 'Content-Type': 'application/json'})
        package = b'CCNB\0\3\xff'
        with patch.object(api, 'fetch_url', return_value=(package, 'application/vnd.classicchess.notebook')) as wire:
            self.assertEqual(api.annotated_book_ccnb('my-system', ccnb_format=3), package)
        self.assertEqual(wire.call_args.args[0], 'https://classicchess.com/api/v1/annotated/books/my-system/ccnb/')
        self.assertEqual(wire.call_args.kwargs['headers'],
                         {'Accept': 'application/vnd.classicchess.notebook', 'CCNB-Format': '3'})
        self.assertFalse(wire.call_args.kwargs['follow_redirects'])
        with patch.object(api, 'fetch_url', return_value=(b'<html>', 'text/html')), self.assertRaises(ApiError):
            api.annotated_book_ccnb('my-system')

    def test_api_gap_reads_reject_unsafe_input_before_any_request(self):
        api = ClassicChessClient()
        calls = [
            lambda: api.chess_today_on_date(13, 1), lambda: api.chess_today_on_date(0, 1),
            lambda: api.chess_today_on_date(True, 1), lambda: api.chess_today_on_date('2', 29),
            lambda: api.chess_today_on_date(2, 32),
            lambda: api.site_update('../account/me'), lambda: api.site_updates(page_size=101),
            lambda: api.site_updates(page=0),
            lambda: api.shared_gym_game('42'), lambda: api.shared_gym_game('../me'),
            lambda: api.shared_gym_game('42.abc/../x'),
            lambda: api.lichess_explorer_game('abc'), lambda: api.lichess_explorer_game('abcdefg/'),
            lambda: api.player_explorer('Tal', color='red'), lambda: api.player_explorer('Tal', page=0),
            lambda: api.player_explorer('x' * 81),
            lambda: api.public_games(result='win'), lambda: api.public_games(theme=['sacrifice', 3]),
            lambda: api.annotated_games('my-system', sort='random'),
            lambda: list(api.iterate_annotated_games('my-system', sort='newest')),
            lambda: api.export_public_games_by_token('tal-vs-larsen-1965'),
            lambda: api.export_public_games_by_token([]),
            lambda: api.export_public_games_by_token(['a b']),
            lambda: api.export_master_games_by_token(['g1,g2']),
            lambda: api.export_master_games_by_token([f'g{n}' for n in range(301)]),
            lambda: api.resolve_game_link(''), lambda: api.resolve_game_link('x' * 501),
            lambda: api.annotated_book_ccnb('../book'), lambda: api.annotated_book_ccnb('book', ccnb_format=4),
        ]
        with patch.object(api, 'fetch_url') as wire:
            for call in calls:
                with self.subTest(call=call), self.assertRaises(ApiError):
                    call()
        wire.assert_not_called()

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
            lambda: api.lichess_explorer(player='a'), lambda: api.lichess_explorer(player='x/../me'),
            lambda: api.lichess_explorer(result='win'), lambda: api.lichess_explorer(player='abc', color='red'),
            lambda: api.lichess_explorer(fen='k' * 101),
            lambda: api.public_imported_game('..', 'g1'), lambda: api.public_imported_game('a/b', 'g1'),
            lambda: api.public_imported_pgn('ann', '../me'),
        ]
        with patch.object(api, 'fetch_url') as wire:
            for call in calls:
                with self.subTest(call=call), self.assertRaises(ApiError):
                    call()
        wire.assert_not_called()
