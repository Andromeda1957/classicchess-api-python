#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Walls
"""Local CLI for pulling Andromeda API data without curl."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urljoin
from urllib.parse import urlparse
from urllib.parse import urlsplit
from urllib.parse import urlunsplit
from urllib.request import HTTPRedirectHandler
from urllib.request import Request
from urllib.request import build_opener
from urllib.request import urlopen

DEFAULT_BASE_URL = "https://classicchess.com"
USER_AGENT = "andromeda-local-api-client/1.0"


# Embedded from andromeda_api/response.py for the dependency-free download.
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_PAGES = 100_000


class ApiError(RuntimeError):
    """Request failure with optional structured HTTP details for app callers."""

    def __init__(self, message, *, status_code=None, code=None, retry_after=None, detail=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after
        self.message = detail if isinstance(detail, str) else message


def read_bounded(response, max_bytes=DEFAULT_MAX_RESPONSE_BYTES):
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1024 * 1024 * 1024:
        raise ApiError('Use a response limit between 1 byte and 1 GiB.', code='invalid_options')
    chunks = []
    size = 0
    while True:
        chunk = response.read(min(65536, max_bytes + 1 - size))
        if not chunk:
            return b''.join(chunks)
        size += len(chunk)
        if size > max_bytes:
            raise ApiError('Response exceeds max_response_bytes.', code='response_too_large')
        chunks.append(chunk)


def decode_json_object(body):
    try:
        result = json.loads(body.decode('utf-8'))
        if not isinstance(result, dict):
            raise ValueError('Expected an object')
        return result
    except (ValueError, RecursionError) as exc:
        raise ApiError('Response was not a JSON object.', code='invalid_response') from exc


def continuation_url(first_url, current_url, next_value):
    if not isinstance(next_value, str) or not next_value or len(next_value) > 8192:
        raise ApiError('Response has an invalid next URL.', code='invalid_pagination')
    try:
        if any(ord(char) <= 32 or ord(char) == 127 for char in next_value) or '\\' in next_value:
            raise ValueError('Unsafe characters')
        target = urlsplit(urljoin(current_url, next_value))
        first = urlsplit(first_url)

        def origin(parts):
            return (parts.scheme.lower(), parts.hostname,
                    parts.port or (443 if parts.scheme == 'https' else 80))

        if (target.scheme not in ('http', 'https') or origin(target) != origin(first)
                or target.username is not None or target.password is not None
                or target.fragment or target.path != first.path or '%' in target.path):
            raise ValueError('Origin or collection changed')
        return urlunsplit(target)
    except ValueError as exc:
        raise ApiError('Pagination must stay in the same API collection.', code='unsafe_url') from exc


def collect_pages(first_url, fetch, *, page=1, limit=None, max_pages=DEFAULT_MAX_PAGES):
    if (type(max_pages) is not int or max_pages < 1 or type(page) is not int or page < 1
            or limit is not None and (type(limit) is not int or limit < 0)):
        raise ApiError('Use positive page bounds and a nonnegative limit.', code='invalid_pagination')
    if limit == 0:
        return {'results': [], 'page': page, 'pages_fetched': 0, 'next': None, 'previous': None}
    results = []
    seen = set()
    url = first_url
    initial = None
    number = page
    while True:
        parsed = urlsplit(url)
        identity = (parsed.scheme, parsed.netloc, parsed.path, tuple(sorted(parse_qsl(parsed.query))))
        if identity in seen or len(seen) >= max_pages:
            raise ApiError('Pagination repeated a page or exceeded max_pages.', code='invalid_pagination')
        seen.add(identity)
        payload = fetch(url)
        if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
            raise ApiError('Response has no results array.', code='invalid_response')
        if initial is None:
            initial = dict(payload)
        for key in ('count', 'count_is_exact', 'page_count', 'hit_result_limit', 'result_limit'):
            if key in payload:
                initial[key] = payload[key]
        results.extend(payload['results'] if limit is None else payload['results'][:max(0, limit - len(results))])
        if limit is not None and len(results) >= limit:
            break
        if 'next' in payload:
            next_value = payload['next']
            if next_value is None:
                break
        else:
            # Compatibility with older responses: refresh the lower bound on
            # every page instead of freezing the first response's page_count.
            try:
                page_count = int(payload.get('page_count') or 0)
            except (TypeError, ValueError) as exc:
                raise ApiError('Response has an invalid page_count.', code='invalid_response') from exc
            if number >= page_count:
                break
            params = dict(parse_qsl(parsed.query))
            params['page'] = str(number + 1)
            next_value = '?' + urlencode(params)
        url = continuation_url(first_url, url, next_value)
        number += 1
    return {**initial, 'results': results, 'page': page, 'pages_fetched': len(seen),
            'next': None, 'previous': None}


def http_api_error(exc, url, body):
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        payload = {}
    detail = payload.get('error') if isinstance(payload, dict) else None
    detail = detail if isinstance(detail, dict) else {}
    code = detail.get('code')
    return ApiError(
        f"HTTP {exc.code} for {url}\n{body}", status_code=exc.code,
        code=code if isinstance(code, str) else 'http_error',
        retry_after=exc.headers.get('Retry-After') if exc.headers else None,
        detail=detail.get('message'),
    )


def public_player_path(slug, suffix=''):
    if not isinstance(slug, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,255}', slug) is None:
        raise ApiError('Use an exact player slug from the public player catalog.')
    return f'/api/v1/public/players/{slug}/{suffix}'



class CredentialOriginError(RuntimeError):
    """Raised before a credential can be sent outside its configured origin."""


def _canonical_host(host: str) -> str:
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        try:
            return host.encode("idna").decode("ascii").casefold()
        except UnicodeError as exc:
            raise CredentialOriginError("Credential URL has an invalid hostname.") from exc


def _credential_origin(value: str) -> tuple[str, str, int]:
    if not value or "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CredentialOriginError("Credential URL contains unsafe characters.")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except ValueError as exc:
        raise CredentialOriginError("Credential URL is malformed.") from exc
    if parsed.scheme.casefold() != "https" or not parsed.netloc or not hostname:
        raise CredentialOriginError("Credential URL must use absolute HTTPS.")
    if username is not None or password is not None or "%" in parsed.netloc or "%" in hostname:
        raise CredentialOriginError("Credential URL has an ambiguous authority.")
    return "https", _canonical_host(hostname), port if port is not None else 443


def require_credentialed_url(base_url: str, target_url: str) -> str:
    """Resolve *target_url* and require the configured HTTPS authority."""

    try:
        raw_target = urlsplit(target_url)
    except ValueError as exc:
        raise CredentialOriginError("Credential URL is malformed.") from exc
    if raw_target.scheme and not raw_target.netloc:
        raise CredentialOriginError("Credential URL has a malformed absolute form.")
    resolved = urljoin(base_url, target_url)
    if _credential_origin(base_url) != _credential_origin(resolved):
        raise CredentialOriginError("Credential URL changes the configured HTTPS origin.")
    return resolved


class SameOriginHTTPSRedirectHandler(HTTPRedirectHandler):
    """Follow credentialed redirects only while every hop remains same-origin."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        _credential_origin(base_url)
        self.base_url = base_url

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Request | None:
        has_authorization = any(
            name.casefold() == "authorization" for name, _ in req.header_items()
        )
        if has_authorization:
            newurl = require_credentialed_url(self.base_url, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    credential_base_url: str | None = None,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> tuple[bytes, str]:
    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    try:
        carries_credentials = any(name.casefold() == "authorization" for name in request_headers)
        if carries_credentials:
            if not credential_base_url:
                raise CredentialOriginError(
                    "Credentialed requests require an explicit configured base URL."
                )
            url = require_credentialed_url(credential_base_url, url)
        request = Request(url, data=body, headers=request_headers, method=method)
        if carries_credentials:
            opener = build_opener(SameOriginHTTPSRedirectHandler(credential_base_url))
            response_context = opener.open(request, timeout=30)
        else:
            response_context = urlopen(request, timeout=30)
        with response_context as response:
            content_type = response.headers.get("Content-Type", "")
            return read_bounded(response, max_response_bytes), content_type
    except CredentialOriginError as exc:
        raise ApiError(f"Refused credentialed request for {url}: {exc}") from exc
    except HTTPError as exc:
        with exc:
            body = read_bounded(exc, max_response_bytes).decode("utf-8", errors="replace")
        raise http_api_error(exc, url, body) from exc
    except TimeoutError as exc:
        raise ApiError("API request timed out.", code="timeout") from exc
    except URLError as exc:
        code = "timeout" if isinstance(exc.reason, TimeoutError) else "network_error"
        raise ApiError(f"Could not fetch {url}: {exc.reason}", code=code) from exc


def fetch_json(url: str, **kwargs: Any) -> dict[str, Any]:
    body, _ = fetch_url(url, **kwargs)
    return decode_json_object(body)


def api_token_from_args(args: argparse.Namespace) -> str:
    token = args.api_token or os.environ.get("ANDROMEDA_API_TOKEN") or ""
    if not token:
        raise ApiError("An Andromeda API token is required for account commands.")
    return token


def request_account_json(
    args: argparse.Namespace,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_token_from_args(args)}",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    return fetch_json(
        api_url(args.base_url, path),
        method=method,
        body=body,
        headers=headers,
        credential_base_url=args.base_url,
    )


def print_jq_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def add_output_options(parser, *, names=False):
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--plain', dest='output_format', action='store_const', const='plain',
                       help='Readable text (default in a terminal); catalogs show slug, game count, name.')
    modes.add_argument('--json', dest='output_format', action='store_const', const='json',
                       help='Full JSON (default when piped or redirected).')
    if names:
        modes.add_argument('--names', dest='output_format', action='store_const', const='names',
                           help='Only names or book titles, one per line.')


def plain_value(value):
    if value is None:
        return ''
    return ' '.join(''.join(c for c in str(value) if c.isprintable() or c.isspace()).split())


def print_biography(bio):
    if not bio:
        print('Biography unavailable.')
        return
    for field in ('epithet', 'lifespan', 'crown', 'lede'):
        if bio.get(field):
            print(plain_value(bio[field]))
    for item in bio.get('vitals', []):
        print(f'{plain_value(item.get("label"))}: {plain_value(item.get("value"))}')
    for section in bio.get('sections', []):
        print('\n' + plain_value(section.get('heading')))
        body = section.get('body', [])
        for paragraph in body if isinstance(body, list) else [body]:
            print(plain_value(paragraph))
    for item in bio.get('numbers', []):
        print(f'{plain_value(item.get("value"))}: {plain_value(item.get("label"))}')
    for field, title in (('quotes_by', 'Quotes by the player'), ('quotes_about', 'Quotes about the player')):
        if bio.get(field):
            print('\n' + title)
            for quote in bio[field]:
                print(f'{plain_value(quote.get("text"))} — {plain_value(quote.get("source"))}')
    if bio.get('legacy'):
        print('\n' + plain_value(bio['legacy']))
    if bio.get('sources'):
        print('\nSources')
        for source in bio['sources']:
            print(f'{plain_value(source.get("label"))}: {plain_value(source.get("url"))}')


def print_resource(args, payload, kind):
    mode = args.output_format or ('plain' if sys.stdout.isatty() else 'json')
    if mode == 'json':
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if kind in {'players', 'events', 'books'}:
        name_key = 'label' if kind == 'books' else 'name'
        for item in payload.get('results', []):
            name = plain_value(item.get(name_key))
            if mode == 'names':
                print(name)
            else:
                print(f'{plain_value(item.get("slug"))}\t{plain_value(item.get("game_count"))}\t{name}')
        return
    player = payload.get('player', {})
    print(plain_value(player.get('name')))
    if kind == 'bio':
        print_biography(payload.get('bio'))
    elif kind == 'player':
        for field, label in (('slug', 'Slug'), ('game_count', 'Games'), ('archive_years', 'Years'), ('criteria', 'Selection')):
            if player.get(field) is not None:
                print(f'{label}: {plain_value(player[field])}')
        for field, label in (('api_bio', 'Biography'), ('api_notable_games', 'Notable games'), ('api_games', 'Games'), ('api_pgn', 'PGN export')):
            if player.get('urls', {}).get(field):
                print(f'{label}: {plain_value(player["urls"][field])}')
    elif kind == 'notable':
        entries = payload.get('results', [])
        if not entries:
            print('No notable games available.')
        for index, entry in enumerate(entries, start=1):
            game = entry.get('game', {})
            print(f'\n{index}. {plain_value(entry.get("title"))} [{plain_value(game.get("token"))}]')
            print(f'   {plain_value(game.get("white"))} — {plain_value(game.get("black"))}')
            if entry.get('annotation'):
                print('   ' + plain_value(entry['annotation']))
            if game.get('urls', {}).get('api_pgn'):
                print('   PGN: ' + plain_value(game['urls']['api_pgn']))


def normalized_base_url(value: str) -> str:
    return value.rstrip("/") + "/"


def api_url(base_url: str, path: str, params: dict[str, Any] | None = None) -> str:
    url = urljoin(normalized_base_url(base_url), path.lstrip("/"))
    cleaned = {
        key: value for key, value in (params or {}).items() if value is not None and value != ""
    }
    if cleaned:
        url = f"{url}?{urlencode(cleaned)}"
    return url


def extract_master_game_token(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme:
        return value.strip("/")

    parts = tuple(part for part in parsed.path.split("/") if part)
    for index, part in enumerate(parts):
        if part in {"games", "game-search"} and index + 1 < len(parts):
            return parts[index + 1]
    raise ApiError(f"Could not find a game token in URL: {value}")


def extract_public_game_token(value: str) -> tuple[str, ...]:
    parsed = urlparse(value)
    raw_value = parsed.path if parsed.scheme else value
    parts = tuple(part for part in raw_value.strip("/").split("/") if part)

    if not parsed.scheme and len(parts) == 2 and parts[0] == "games":
        return (parts[1],)
    if not parsed.scheme and len(parts) in (1, 2):
        return parts

    for index in range(max(0, len(parts) - 3)):
        if parts[index : index + 3] == ("api", "v1", "public") and parts[index + 3] == "games":
            suffix = parts[index + 4 :]
            if suffix and suffix[-1] == "pgn":
                suffix = suffix[:-1]
            if len(suffix) in (1, 2):
                return suffix

    for index, part in enumerate(parts):
        if part == "games" and index > 0 and index + 1 < len(parts):
            return parts[index - 1], parts[index + 1]

    if len(parts) >= 2 and parts[-2] == "games":
        return (parts[-1],)

    raise ApiError(f"Could not find a public game token in URL: {value}")


def write_or_print_text(text: str, output: str | None) -> None:
    if output:
        Path(output).write_text(text, encoding="utf-8")
        return
    print(text, end="" if text.endswith("\n") else "\n")


def build_game_query(args: argparse.Namespace) -> str:
    if args.query:
        query = args.query.strip()
    elif args.player and args.opponent:
        query = f"{args.player} {args.opponent}"
    elif args.players:
        query = " ".join(args.players)
    elif args.player:
        query = args.player
    elif args.eco:
        query = ""
    else:
        raise ApiError("games requires --query, --player, --players, or --eco.")

    if args.eco:
        query = f"{query} {args.eco}".strip()

    if args.year:
        query = f"{query} {args.year}"
    return query


def build_public_game_query(args: argparse.Namespace) -> str:
    if args.query:
        query = args.query.strip()
    elif args.player and args.opponent:
        query = f"{args.player} {args.opponent}"
    elif args.players:
        query = " ".join(args.players)
    elif args.player:
        query = args.player
    else:
        query = ""

    if args.eco:
        query = f"{query} {args.eco}".strip()

    if args.year:
        query = f"{query} {args.year}".strip()
    return query


def command_master_stats(args: argparse.Namespace) -> int:
    if args.year:
        raise ApiError("The stats endpoint does not support --year; use games.")

    if args.query:
        params = {"q": args.query.strip()}
    else:
        if not args.player:
            raise ApiError("stats requires --query or --player.")
        mode = args.mode or ("head_to_head" if args.opponent else "summary")
        if mode == "head_to_head" and not args.opponent:
            raise ApiError("stats --mode head_to_head requires --opponent.")
        params = {
            "mode": mode,
            "player": args.player,
            "opponent": args.opponent,
        }

    url = api_url(
        args.base_url,
        "/api/v1/stats/",
        params,
    )
    print_jq_json(fetch_json(url))
    return 0


def command_master_game(args: argparse.Namespace) -> int:
    token = extract_master_game_token(args.token)
    url = api_url(args.base_url, f"/api/v1/games/{token}/")
    print_jq_json(fetch_json(url))
    return 0


def command_master_pgn(args: argparse.Namespace) -> int:
    token = extract_master_game_token(args.token)
    url = api_url(args.base_url, f"/api/v1/games/{token}/pgn/")
    body, _ = fetch_url(url)
    write_or_print_text(body.decode("utf-8"), args.output)
    return 0


def command_master_export(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"format": args.format}
    if args.tokens:
        params["tokens"] = ",".join(extract_master_game_token(token) for token in args.tokens)
    else:
        params["q"] = build_game_query(args)
    if args.format == "ndjson":
        params["pgnInJson"] = "true" if args.pgn_in_json else "false"
    body, _ = fetch_url(api_url(args.base_url, "/api/v1/games/export/", params))
    write_or_print_text(body.decode("utf-8"), args.output)
    return 0


def collect_master_game_pages(args: argparse.Namespace, query: str) -> dict[str, Any]:
    url = api_url(args.base_url, "/api/v1/games/", {
        "q": query, "page": args.page, "page_size": args.page_size,
    })
    if not args.all_pages:
        return fetch_json(url)
    return collect_pages(url, fetch_json, page=args.page, limit=args.limit)


def collect_public_game_pages(args: argparse.Namespace, query: str) -> dict[str, Any]:
    url = api_url(args.base_url, "/api/v1/public/games/", {
        "q": query, "archive_player": args.archive_player, "archive_event": args.archive_event,
        "since": args.since, "until": args.until, "sort": args.sort,
        "page": args.page, "page_size": args.page_size,
    })
    if not args.all_pages:
        return fetch_json(url)
    return collect_pages(url, fetch_json, page=args.page, limit=args.limit)


def pgn_text_for_games(base_url: str, games: list[dict[str, Any]]) -> str:
    pgns = []
    for game in games:
        path = game.get("urls", {}).get("api_pgn")
        if not path:
            raise ApiError(f"Game result has no PGN URL: {game!r}")
        body, _ = fetch_url(urljoin(normalized_base_url(base_url), path.lstrip("/")))
        pgns.append(body.decode("utf-8").strip())
    return "\n\n".join(pgns) + ("\n" if pgns else "")


def command_master_games(args: argparse.Namespace) -> int:
    query = build_game_query(args)
    payload = collect_master_game_pages(args, query)
    return write_games_payload(args, payload)


def command_public_games(args: argparse.Namespace) -> int:
    query = build_public_game_query(args)
    payload = collect_public_game_pages(args, query)
    return write_games_payload(args, payload)


def write_games_payload(args: argparse.Namespace, payload: dict[str, Any]) -> int:
    if args.limit and not args.all_pages:
        payload["results"] = payload.get("results", [])[: args.limit]

    if args.pgn:
        text = pgn_text_for_games(args.base_url, list(payload.get("results", ())))
        write_or_print_text(text, args.output)
        return 0

    print_jq_json(payload)
    return 0


def command_public_players(args: argparse.Namespace) -> int:
    payload = fetch_json(
        api_url(
            args.base_url,
            "/api/v1/public/players/",
            {"q": args.query},
        )
    )
    print_resource(args, payload, 'players')
    return 0


def command_public_player(args):
    print_resource(args, fetch_json(api_url(args.base_url, public_player_path(args.player_slug))), 'player')
    return 0


def command_public_bio(args):
    print_resource(args, fetch_json(api_url(args.base_url, public_player_path(args.player_slug, 'bio/'))), 'bio')
    return 0


def command_public_notable(args):
    print_resource(args, fetch_json(api_url(args.base_url, public_player_path(args.player_slug, 'notable-games/'))), 'notable')
    return 0


def command_public_events(args: argparse.Namespace) -> int:
    payload = fetch_json(
        api_url(
            args.base_url,
            "/api/v1/public/events/",
            {"q": args.query},
        )
    )
    print_resource(args, payload, 'events')
    return 0


def command_public_game(args: argparse.Namespace) -> int:
    game_path = "/".join(extract_public_game_token(args.token))
    url = api_url(args.base_url, f"/api/v1/public/games/{game_path}/")
    print_jq_json(fetch_json(url))
    return 0


def command_public_pgn(args: argparse.Namespace) -> int:
    game_path = "/".join(extract_public_game_token(args.token))
    url = api_url(args.base_url, f"/api/v1/public/games/{game_path}/pgn/")
    body, _ = fetch_url(url)
    write_or_print_text(body.decode("utf-8"), args.output)
    return 0


def command_public_export(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {
        "format": args.format,
        "archive_player": args.archive_player,
        "archive_event": args.archive_event,
        "since": args.since,
        "until": args.until,
        "sort": args.sort,
    }
    if args.tokens:
        params["tokens"] = ",".join(
            "/".join(extract_public_game_token(token)) for token in args.tokens
        )
    else:
        params["q"] = build_public_game_query(args)
    if args.format == "ndjson":
        params["pgnInJson"] = "true" if args.pgn_in_json else "false"
    body, _ = fetch_url(api_url(args.base_url, "/api/v1/public/games/export/", params))
    write_or_print_text(body.decode("utf-8"), args.output)
    return 0


def command_players(args: argparse.Namespace) -> int:
    payload = fetch_json(
        api_url(args.base_url, "/api/v1/players/", {"q": args.query, "limit": args.limit})
    )
    print_jq_json(payload)
    return 0


def command_account_me(args: argparse.Namespace) -> int:
    print_jq_json(request_account_json(args, "GET", "/api/v1/account/me/"))
    return 0


def command_account_collections_list(args: argparse.Namespace) -> int:
    print_jq_json(request_account_json(args, "GET", "/api/v1/account/collections/"))
    return 0


def command_account_collections_create(args: argparse.Namespace) -> int:
    print_jq_json(
        request_account_json(
            args,
            "POST",
            "/api/v1/account/collections/",
            {"name": args.name},
        )
    )
    return 0


def command_account_collections_import_player(args: argparse.Namespace) -> int:
    print_jq_json(
        request_account_json(
            args,
            "POST",
            "/api/v1/account/collections/import/",
            {
                "source": "public_player_games",
                "name": args.name,
                "collection_id": args.collection_id,
                "archive_player": args.archive_player,
                "since": args.since,
                "until": args.until,
            },
        )
    )
    return 0


def command_explorer(args: argparse.Namespace) -> int:
    if args.sources:
        print_jq_json(fetch_json(api_url(args.base_url, "/api/v1/opening-explorer/sources/")))
        return 0
    payload = fetch_json(
        api_url(
            args.base_url,
            "/api/v1/opening-explorer/",
            {
                "fen": args.fen,
                "play": args.play,
                "moves": args.moves,
                "topGames": args.top_games,
                "source_type": args.source_type,
                "source_key": args.source_key,
            },
        )
    )
    print_jq_json(payload)
    return 0


def command_annotated_books(args: argparse.Namespace) -> int:
    print_resource(args, fetch_json(api_url(args.base_url, "/api/v1/annotated/books/")), 'books')
    return 0


def command_annotated_games(args: argparse.Namespace) -> int:
    print_jq_json(
        fetch_json(
            api_url(
                args.base_url,
                f"/api/v1/annotated/books/{args.book_slug}/games/",
                {"page": args.page, "page_size": args.page_size},
            )
        )
    )
    return 0


def command_annotated_game(args: argparse.Namespace) -> int:
    print_jq_json(
        fetch_json(
            api_url(
                args.base_url,
                f"/api/v1/annotated/books/{args.book_slug}/games/{args.game_slug}/",
            )
        )
    )
    return 0


def command_annotated_pgn(args: argparse.Namespace) -> int:
    if args.game_slug:
        path = f"/api/v1/annotated/books/{args.book_slug}/games/{args.game_slug}/pgn/"
    else:
        path = f"/api/v1/annotated/books/{args.book_slug}/pgn/"
    body, _ = fetch_url(api_url(args.base_url, path))
    write_or_print_text(body.decode("utf-8"), args.output)
    return 0


def add_games_args(parser: argparse.ArgumentParser, *, public: bool = False) -> None:
    parser.add_argument("-q", "--query", help="Raw game-search query.")
    parser.add_argument("--player", help="Player name query.")
    parser.add_argument("--opponent", help="Opponent name query.")
    parser.add_argument(
        "--players",
        nargs="+",
        help="One or more player-name fragments joined into the game query.",
    )
    parser.add_argument("--eco", help="Filter by ECO code or code fragment, such as B92.")
    parser.add_argument("--year", type=int, help="Append a year filter to the query.")
    if public:
        parser.add_argument(
            "--archive-player",
            help="Restrict to one public archive by study-player slug, name, or alias.",
        )
        parser.add_argument(
            "--archive-event",
            help="Restrict to one public event archive by event slug.",
        )
        parser.add_argument("--since", type=int, help="Include games from this year or later.")
        parser.add_argument("--until", type=int, help="Include games from this year or earlier.")
        parser.add_argument(
            "--sort",
            choices=("asc", "desc"),
            default="asc",
            help="Sort by public archive display date. Default: asc.",
        )
    parser.add_argument("--page", type=int, default=1, help="Result page. Default: 1.")
    parser.add_argument(
        "--page-size",
        type=int,
        default=50,
        help="Results per page, max 100 on the API. Default: 50.",
    )
    parser.add_argument("--all-pages", action="store_true", help="Fetch all pages.")
    parser.add_argument("--limit", type=int, help="Limit returned/fetched results.")
    parser.add_argument("--pgn", action="store_true", help="Fetch PGN for results.")
    parser.add_argument("--output", help="Write PGN output to this file.")


def add_export_args(parser: argparse.ArgumentParser, *, public: bool = False) -> None:
    add_games_args(parser, public=public)
    parser.add_argument(
        "--tokens",
        nargs="+",
        help="Explicit game tokens or game URLs to export. Overrides query arguments.",
    )
    parser.add_argument(
        "--format",
        choices=("pgn", "ndjson"),
        default="pgn",
        help="Export format. Default: pgn.",
    )
    parser.add_argument(
        "--pgn-in-json",
        action="store_true",
        default=True,
        help="Include PGN in NDJSON rows. Default: enabled.",
    )


def add_master_commands(subparsers: argparse._SubParsersAction) -> None:
    master = subparsers.add_parser("master", help="Pull from the MasterDB stats/search API.")
    master_subparsers = master.add_subparsers(dest="master_command", required=True)

    stats = master_subparsers.add_parser("stats", help="Pull /api/v1/stats/ JSON.")
    stats.add_argument("--query", "-q", help="Single stats query, such as 'tal' or 'tal smyslov'.")
    stats.add_argument("--player", help="Legacy player name query.")
    stats.add_argument("--opponent", help="Legacy opponent name query for head-to-head.")
    stats.add_argument("--year", type=int, help="Not supported by stats API.")
    stats.add_argument(
        "--mode",
        choices=("summary", "head_to_head"),
        help="Legacy stats mode. Defaults to head_to_head when --opponent is present.",
    )
    stats.set_defaults(func=command_master_stats)

    games = master_subparsers.add_parser("games", help="Pull /api/v1/games/ JSON or PGN.")
    add_games_args(games)
    games.set_defaults(func=command_master_games)

    game = master_subparsers.add_parser("game", help="Pull one MasterDB game detail JSON by token.")
    game.add_argument("token", help="MasterDB game token or game/detail URL.")
    game.set_defaults(func=command_master_game)

    pgn = master_subparsers.add_parser("pgn", help="Pull one raw MasterDB PGN by token.")
    pgn.add_argument("token", help="MasterDB game token or game/detail URL.")
    pgn.add_argument("--output", help="Write PGN output to this file.")
    pgn.set_defaults(func=command_master_pgn)

    export = master_subparsers.add_parser(
        "export", help="Bulk export MasterDB games as PGN or NDJSON."
    )
    add_export_args(export)
    export.set_defaults(func=command_master_export)


def add_public_commands(subparsers: argparse._SubParsersAction) -> None:
    public = subparsers.add_parser(
        "public", help="Discover curated players, events, bios, notable games and PGN.",
        description="Catalogs and player resources use readable text in a terminal and JSON in pipes. "
                    "Use --names for names only, --plain for readable text, or --json for full data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples (use exact slugs/tokens from the lists):\n"
               "  python3 pull_api.py public players --names\n"
               "  python3 pull_api.py public players --plain\n"
               "  python3 pull_api.py public events --names\n"
               "  python3 pull_api.py public bio PLAYER_SLUG\n"
               "  python3 pull_api.py public notable PLAYER_SLUG\n"
               "  python3 pull_api.py public notable PLAYER_SLUG --json\n"
               "  python3 pull_api.py public pgn GAME_TOKEN",
    )
    public_subparsers = public.add_subparsers(dest="public_command", required=True)

    players = public_subparsers.add_parser("players", help="List public archive players.")
    players.add_argument("-q", "--query", help="Filter players by name, slug, or source alias.")
    add_output_options(players, names=True)
    players.set_defaults(func=command_public_players)

    for name, handler, help_text in (
        ('player', command_public_player, 'Read one curated player profile.'),
        ('bio', command_public_bio, 'Read an approved biography and its sources.'),
        ('notable', command_public_notable, 'List all notable games in curated order.'),
    ):
        resource = public_subparsers.add_parser(name, help=help_text)
        resource.add_argument('player_slug', help='Exact slug from public players.')
        add_output_options(resource)
        resource.set_defaults(func=handler)

    events = public_subparsers.add_parser("events", help="List public archive events.")
    events.add_argument("-q", "--query", help="Filter events by name, slug, series, site, or year.")
    add_output_options(events, names=True)
    events.set_defaults(func=command_public_events)

    games = public_subparsers.add_parser("games", help="Pull /api/v1/public/games/ JSON or PGN.")
    add_games_args(games, public=True)
    games.set_defaults(func=command_public_games)

    game = public_subparsers.add_parser("game", help="Pull one public archive game detail JSON.")
    game.add_argument("token", help="Public token, /players/... URL, or public API game URL.")
    game.set_defaults(func=command_public_game)

    pgn = public_subparsers.add_parser("pgn", help="Pull one raw public archive PGN.")
    pgn.add_argument("token", help="Public token, /players/... URL, or public API game URL.")
    pgn.add_argument("--output", help="Write PGN output to this file.")
    pgn.set_defaults(func=command_public_pgn)

    export = public_subparsers.add_parser(
        "export", help="Bulk export public archive games as PGN or NDJSON."
    )
    add_export_args(export, public=True)
    export.set_defaults(func=command_public_export)


def add_annotated_commands(subparsers: argparse._SubParsersAction) -> None:
    annotated = subparsers.add_parser("annotated", help="Pull from the annotated-book API.")
    annotated_subparsers = annotated.add_subparsers(dest="annotated_command", required=True)

    books = annotated_subparsers.add_parser("books", help="List annotated books.")
    add_output_options(books, names=True)
    books.set_defaults(func=command_annotated_books)

    games = annotated_subparsers.add_parser("games", help="List games in an annotated book.")
    games.add_argument("book_slug")
    games.add_argument("--page", type=int, default=1)
    games.add_argument("--page-size", type=int, default=50)
    games.set_defaults(func=command_annotated_games)

    game = annotated_subparsers.add_parser("game", help="Pull one annotated game detail JSON.")
    game.add_argument("book_slug")
    game.add_argument("game_slug")
    game.set_defaults(func=command_annotated_game)

    pgn = annotated_subparsers.add_parser("pgn", help="Pull an annotated book or game PGN.")
    pgn.add_argument("book_slug")
    pgn.add_argument("game_slug", nargs="?")
    pgn.add_argument("--output")
    pgn.set_defaults(func=command_annotated_pgn)


def add_account_commands(subparsers: argparse._SubParsersAction) -> None:
    account = subparsers.add_parser("account", help="Use authenticated account API commands.")
    account_subparsers = account.add_subparsers(dest="account_command", required=True)

    me = account_subparsers.add_parser("me", help="Show the authenticated account.")
    me.set_defaults(func=command_account_me)

    collections = account_subparsers.add_parser(
        "collections", help="Manage account game collections."
    )
    collection_subparsers = collections.add_subparsers(dest="collections_command", required=True)

    list_command = collection_subparsers.add_parser("list", help="List account collections.")
    list_command.set_defaults(func=command_account_collections_list)

    create = collection_subparsers.add_parser("create", help="Create a collection.")
    create.add_argument("name")
    create.set_defaults(func=command_account_collections_create)

    import_player = collection_subparsers.add_parser(
        "import-player",
        help="Import public archive player games into a collection.",
    )
    import_player.add_argument("name", help="Collection name to create or reuse.")
    import_player.add_argument("archive_player", help="Public archive player slug, name, or alias.")
    import_player.add_argument(
        "--collection-id", type=int, help="Existing collection ID to use instead of name."
    )
    import_player.add_argument("--since", type=int, help="Include games from this year or later.")
    import_player.add_argument("--until", type=int, help="Include games from this year or earlier.")
    import_player.set_defaults(func=command_account_collections_import_player)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pull Andromeda API data from an explicit source.",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"API base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--api-token",
        help="Account API token. Defaults to ANDROMEDA_API_TOKEN when omitted.",
    )
    subparsers = parser.add_subparsers(dest="source", required=True)
    add_master_commands(subparsers)
    add_public_commands(subparsers)
    add_annotated_commands(subparsers)
    add_account_commands(subparsers)

    players = subparsers.add_parser("players", help="Resolve player names.")
    players.add_argument("query")
    players.add_argument("--limit", type=int, default=10)
    players.set_defaults(func=command_players)

    explorer = subparsers.add_parser("explorer", help="Pull opening explorer data.")
    explorer.add_argument("--fen", help="Root FEN. Omit for the starting position.")
    explorer.add_argument("--play", help="Comma-separated UCI moves from the root FEN.")
    explorer.add_argument("--moves", type=int, default=12)
    explorer.add_argument("--top-games", type=int)
    explorer.add_argument("--source-type")
    explorer.add_argument("--source-key")
    explorer.add_argument("--sources", action="store_true", help="List indexed explorer sources.")
    explorer.set_defaults(func=command_explorer)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
