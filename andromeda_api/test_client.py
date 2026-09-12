from __future__ import annotations

import unittest
from io import BytesIO
from unittest.mock import Mock
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from andromeda_api.client import AndromedaClient
from andromeda_api.client import ApiError
from andromeda_api.origin_policy import CredentialOriginError
from andromeda_api.origin_policy import SameOriginHTTPSRedirectHandler
from andromeda_api.origin_policy import require_credentialed_url


class PlayerDiscoveryClientTests(unittest.TestCase):
    def test_player_resources_request_the_exact_public_resource(self):
        client = AndromedaClient()
        for method, suffix in (('public_player', ''), ('public_player_bio', 'bio/'), ('public_notable_games', 'notable-games/')):
            with self.subTest(method=method), patch.object(client, 'fetch_json', return_value={'source': 'public'}) as fetch:
                result = getattr(client, method)('mikhail-tal')
                self.assertEqual(result, {'source': 'public'})
                fetch.assert_called_once_with(f'https://classicchess.com/api/v1/public/players/mikhail-tal/{suffix}')

    def test_player_resource_rejects_paths_and_query_injection_before_io(self):
        client = AndromedaClient()
        with patch.object(client, 'fetch_json') as fetch:
            for slug in ('../events', 'tal?format=txt', 'https://other.invalid', 'tal/bio'):
                with self.subTest(slug=slug), self.assertRaises(ApiError):
                    client.public_player(slug)
            fetch.assert_not_called()

    def test_http_errors_expose_status_code_and_retry_guidance(self):
        error = HTTPError(
            'https://classicchess.com/api/v1/games/', 429, 'Too many requests',
            {'Retry-After': '900'}, BytesIO(b'{"error":{"code":"rate_limited","message":"Please wait."}}'),
        )
        with patch('andromeda_api.client.urlopen', side_effect=error), self.assertRaises(ApiError) as caught:
            AndromedaClient().get_json('/api/v1/games/', {'q': 'Tal'})
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.code, 'rate_limited')
        self.assertEqual(caught.exception.message, 'Please wait.')
        self.assertEqual(caught.exception.retry_after, '900')


class CredentialOriginPolicyTests(unittest.TestCase):
    def test_relative_and_same_origin_https_urls_are_allowed(self) -> None:
        base = "https://classicchess.com/api/"
        self.assertEqual(
            require_credentialed_url(base, "/api/v1/account/me/"),
            "https://classicchess.com/api/v1/account/me/",
        )
        self.assertEqual(
            require_credentialed_url(
                base,
                "https://CLASSICCHESS.com:443/api/v1/account/me/",
            ),
            "https://CLASSICCHESS.com:443/api/v1/account/me/",
        )

    def test_foreign_downgraded_and_ambiguous_urls_are_rejected(self) -> None:
        base = "https://classicchess.com/"
        hostile = (
            "https://example.net/account/",
            "https://api.classicchess.com/account/",
            "https://classicchess.com:0/account/",
            "https://classicchess.com:444/account/",
            "http://classicchess.com/account/",
            "//example.net/account/",
            "https://classicchess.com%2eexample.net/account/",
            "https://classicchess.com@evil.example/account/",
            "https:%2f%2fevil.example/account/",
        )
        for value in hostile:
            with self.subTest(value=value):
                with self.assertRaises(CredentialOriginError):
                    require_credentialed_url(base, value)

    def test_redirect_handler_preserves_bearer_only_on_same_origin(self) -> None:
        handler = SameOriginHTTPSRedirectHandler("https://classicchess.com/")
        request = Request(
            "https://classicchess.com/api/v1/account/me/",
            headers={"Authorization": "Bearer secret"},
        )

        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "/api/v1/account/collections/",
        )
        self.assertEqual(
            redirected.full_url,
            "https://classicchess.com/api/v1/account/collections/",
        )
        self.assertEqual(redirected.get_header("Authorization"), "Bearer secret")

    def test_multi_hop_redirect_rejects_the_first_origin_change(self) -> None:
        handler = SameOriginHTTPSRedirectHandler("https://classicchess.com/")
        first = Request(
            "https://classicchess.com/api/v1/account/me/",
            headers={"Authorization": "Bearer secret"},
        )
        second = handler.redirect_request(
            first,
            None,
            302,
            "Found",
            {},
            "/same-origin-hop",
        )

        with self.assertRaises(CredentialOriginError):
            handler.redirect_request(
                second,
                None,
                307,
                "Temporary Redirect",
                {},
                "//evil.example/final",
            )

    def test_packaged_client_rejects_before_opening_a_foreign_url(self) -> None:
        client = AndromedaClient(base_url="https://classicchess.com/")
        with (
            patch("andromeda_api.client.build_opener") as build_opener,
            patch("andromeda_api.client.urlopen") as urlopen,
            self.assertRaisesRegex(ApiError, "Refused credentialed request"),
        ):
            client.fetch_url(
                "https://evil.example/account/",
                headers={"Authorization": "Bearer secret"},
            )

        build_opener.assert_not_called()
        urlopen.assert_not_called()


class PaginatedClientTests(unittest.TestCase):
    def client_with_pages(self, pages: list[dict[str, object]]) -> tuple[AndromedaClient, Mock]:
        client = AndromedaClient(base_url="https://example.test")
        get_json = Mock(side_effect=pages)
        client.fetch_json = get_json
        return client, get_json

    def test_all_pages_reports_actual_fetches_when_limit_stops_early(self) -> None:
        client, get_json = self.client_with_pages(
            [
                {"results": ["one", "two"], "page_count": 4},
                {"results": ["three", "four"], "page_count": 4},
            ]
        )

        payload = client._paginated(
            "/api/v1/games/",
            {"q": "Tal"},
            page=1,
            page_size=2,
            all_pages=True,
            limit=3,
        )

        self.assertEqual(payload["results"], ["one", "two", "three"])
        self.assertEqual(payload["pages_fetched"], 2)
        self.assertEqual(get_json.call_count, 2)

    def test_all_pages_counts_the_requested_nonfirst_page(self) -> None:
        client, get_json = self.client_with_pages(
            [
                {"results": ["three", "four"], "page_count": 4},
                {"results": ["five", "six"], "page_count": 4},
            ]
        )

        payload = client._paginated(
            "/api/v1/games/",
            {"q": "Tal"},
            page=3,
            page_size=2,
            all_pages=True,
            limit=None,
        )

        self.assertEqual(payload["results"], ["three", "four", "five", "six"])
        self.assertEqual(payload["pages_fetched"], 2)
        self.assertEqual(get_json.call_count, 2)

    def test_all_pages_rejects_malformed_page_count(self) -> None:
        client, get_json = self.client_with_pages([{"results": [], "page_count": "not-a-number"}])

        with self.assertRaisesRegex(ApiError, "invalid page_count"):
            client._paginated(
                "/api/v1/games/",
                {},
                page=1,
                page_size=50,
                all_pages=True,
                limit=None,
            )

        self.assertEqual(get_json.call_count, 1)

    def test_all_pages_returns_empty_first_page_as_one_fetch(self) -> None:
        client, get_json = self.client_with_pages([{"results": [], "page_count": 3}])

        payload = client._paginated(
            "/api/v1/games/",
            {},
            page=3,
            page_size=50,
            all_pages=True,
            limit=None,
        )

        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["pages_fetched"], 1)
        self.assertEqual(get_json.call_count, 1)

    def test_all_pages_propagates_a_later_network_failure(self) -> None:
        client, _ = self.client_with_pages(
            [
                {"results": ["one"], "page_count": 2},
                ApiError("Could not fetch https://example.test/api/v1/games/"),
            ]
        )

        with self.assertRaisesRegex(ApiError, "Could not fetch"):
            client._paginated(
                "/api/v1/games/",
                {},
                page=1,
                page_size=50,
                all_pages=True,
                limit=None,
            )
