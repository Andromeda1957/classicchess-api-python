"""Exercise both distributed Python clients against the same wire failures."""

import importlib.util
from argparse import Namespace
from io import BytesIO
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.parse import urlsplit

from classicchess_api import client as packaged


def standalone_client():
    root = Path(__file__).resolve().parents[2]
    path = root / 'classicchess_api/standalone.py'
    if not path.is_file():
        path = root / 'pull_api.py'
    spec = importlib.util.spec_from_file_location('standalone_api', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response(BytesIO):
    headers = {'Content-Type': 'application/json'}
    bytes_read = 0

    def read(self, size=-1):
        data = super().read(size)
        self.bytes_read += len(data)
        return data


class PythonClientRobustnessTests(TestCase):
    def clients(self):
        return (packaged, standalone_client())

    def collect(self, module, fetch, *, limit=None):
        if module is packaged:
            client = packaged.ClassicChessClient()
            client.fetch_json = fetch
            return client.master_games(query='Tal', all_pages=True, limit=limit, page_size=1)
        args = Namespace(base_url='https://classicchess.com', page=1, page_size=1,
                         all_pages=True, limit=limit)
        with patch.object(module, 'fetch_json', side_effect=fetch):
            return module.collect_master_game_pages(args, 'Tal')

    def test_all_pages_follows_growing_counts_in_both_clients(self):
        for module in self.clients():
            for total in (3, 4, 6):
                calls = []

                def fetch(url, calls=calls, total=total):
                    page = int(parse_qs(urlsplit(url).query).get('page', ['1'])[0])
                    calls.append(page)
                    return {'page': page, 'count': page, 'page_count': page + (page < total),
                            'results': [{'token': str(page)}],
                            'next': f'?q=Tal&page={page + 1}' if page < total else None}

                with self.subTest(client=module.__name__, pages=total):
                    result = self.collect(module, fetch)
                    self.assertEqual(calls, list(range(1, total + 1)))
                    self.assertEqual(len(result['results']), total)
                    self.assertEqual(result['pages_fetched'], total)

    def test_continuations_cannot_leave_the_origin_or_collection(self):
        for module in self.clients():
            for next_url in ('https://other.invalid/api/v1/games/',
                             '/api/v1/account/me/', '//other.invalid/api/v1/games/',
                             '/api/v1/games/%2e%2e/account/', '?page=2#fragment'):
                calls = []

                def fetch(url, calls=calls, next_url=next_url):
                    calls.append(url)
                    return {'results': [{'token': 'one'}], 'page_count': 2, 'next': next_url}

                with self.subTest(client=module.__name__, next=next_url):
                    with self.assertRaises(module.ApiError):
                        self.collect(module, fetch)
                    self.assertEqual(len(calls), 1)

    def test_empty_and_limited_iteration_do_not_overfetch(self):
        for module in self.clients():
            calls = []

            def fetch(url, calls=calls):
                calls.append(url)
                return {'results': [{'token': 'one'}], 'page_count': 2, 'next': '?page=2'}

            with self.subTest(client=module.__name__):
                self.assertEqual(self.collect(module, fetch, limit=0)['results'], [])
                self.assertEqual(calls, [])
                self.assertEqual(len(self.collect(module, fetch, limit=1)['results']), 1)
                self.assertEqual(len(calls), 1)

    def test_json_errors_are_consistent_for_invalid_utf8_arrays_and_proxy_html(self):
        for module in self.clients():
            for body in (b'\xff', b'[]', b'<h1>Unavailable</h1>'):
                with self.subTest(client=module.__name__, body=body), \
                        patch.object(module, 'urlopen', return_value=Response(body)):
                    fetch = packaged.ClassicChessClient().fetch_json if module is packaged else module.fetch_json
                    with self.assertRaises(module.ApiError) as caught:
                        fetch('https://classicchess.com/api/v1/')
                    self.assertEqual(caught.exception.code, 'invalid_response')

    def test_success_and_error_bodies_are_bounded(self):
        for module in self.clients():
            for status in (200, 429):
                stream = Response(b'x' * 100)
                error = HTTPError('https://classicchess.com/api/v1/', status, 'Too many',
                                  {'Retry-After': '30'}, stream) if status != 200 else None
                with self.subTest(client=module.__name__, status=status), patch.object(
                    module, 'urlopen', side_effect=error, return_value=stream,
                ):
                    with self.assertRaises(module.ApiError) as caught:
                        if module is packaged:
                            packaged.ClassicChessClient(max_response_bytes=8).fetch_url('https://classicchess.com/api/v1/')
                        else:
                            module.fetch_url('https://classicchess.com/api/v1/', max_response_bytes=8)
                    self.assertEqual(caught.exception.code, 'response_too_large')
                    self.assertLessEqual(stream.bytes_read, 9)
                    self.assertTrue(stream.closed)

    def test_timeout_uses_the_api_error_contract(self):
        for module in self.clients():
            with self.subTest(client=module.__name__), patch.object(module, 'urlopen', side_effect=TimeoutError()):
                with self.assertRaises(module.ApiError) as caught:
                    if module is packaged:
                        packaged.ClassicChessClient().fetch_url('https://classicchess.com/api/v1/')
                    else:
                        module.fetch_url('https://classicchess.com/api/v1/')
                self.assertEqual(caught.exception.code, 'timeout')
