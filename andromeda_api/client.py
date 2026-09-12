"""Python client for the public Andromeda chess archive API."""

from __future__ import annotations

import json
import re
from typing import Any
from typing import Iterable
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.parse import urljoin
from urllib.parse import urlparse
from urllib.request import Request
from urllib.request import build_opener
from urllib.request import urlopen

from andromeda_api.origin_policy import CredentialOriginError
from andromeda_api.origin_policy import SameOriginHTTPSRedirectHandler
from andromeda_api.origin_policy import require_credentialed_url
from andromeda_api.response import DEFAULT_MAX_RESPONSE_BYTES
from andromeda_api.response import ApiError
from andromeda_api.response import collect_pages
from andromeda_api.response import decode_json_object
from andromeda_api.response import read_bounded

DEFAULT_BASE_URL = "https://classicchess.com"
USER_AGENT = "andromeda-local-api-client/1.0"


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


class AndromedaClient:
    """Dependency-free client for Andromeda JSON, PGN, and account API endpoints."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_token: str | None = None,
        user_agent: str = USER_AGENT,
        timeout: int = 30,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self.base_url = normalized_base_url(base_url)
        self.api_token = api_token
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def url(self, path: str, params: dict[str, Any] | None = None) -> str:
        return api_url(self.base_url, path, params)

    def fetch_url(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, str]:
        request_headers = {"User-Agent": self.user_agent}
        request_headers.update(headers or {})
        try:
            carries_credentials = any(
                name.casefold() == "authorization" for name in request_headers
            )
            if carries_credentials:
                url = require_credentialed_url(self.base_url, url)
            request = Request(url, data=body, headers=request_headers, method=method)
            if carries_credentials:
                opener = build_opener(SameOriginHTTPSRedirectHandler(self.base_url))
                response_context = opener.open(request, timeout=self.timeout)
            else:
                response_context = urlopen(request, timeout=self.timeout)
            with response_context as response:
                content_type = response.headers.get("Content-Type", "")
                return read_bounded(response, self.max_response_bytes), content_type
        except CredentialOriginError as exc:
            raise ApiError(f"Refused credentialed request for {url}: {exc}") from exc
        except HTTPError as exc:
            with exc:
                body = read_bounded(exc, self.max_response_bytes).decode("utf-8", errors="replace")
            raise http_api_error(exc, url, body) from exc
        except TimeoutError as exc:
            raise ApiError("API request timed out.", code="timeout") from exc
        except URLError as exc:
            code = "timeout" if isinstance(exc.reason, TimeoutError) else "network_error"
            raise ApiError(f"Could not fetch {url}: {exc.reason}", code=code) from exc

    def fetch_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        body, _ = self.fetch_url(url, **kwargs)
        return decode_json_object(body)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.fetch_json(self.url(path, params))

    def get_text(self, path: str, params: dict[str, Any] | None = None) -> str:
        body, _ = self.fetch_url(self.url(path, params))
        return body.decode("utf-8")

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        api_token: str | None = None,
    ) -> dict[str, Any]:
        token = api_token or self.api_token
        if not token:
            raise ApiError("An Andromeda API token is required for account commands.")
        body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self.fetch_json(
            self.url(path),
            method=method,
            body=body,
            headers=headers,
        )

    def master_stats(
        self,
        player: str | None = None,
        *,
        query: str | None = None,
        opponent: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        if query:
            return self.get_json("/api/v1/stats/", {"q": query})
        if not player:
            raise ApiError("master_stats requires query or player.")
        selected_mode = mode or ("head_to_head" if opponent else "summary")
        if selected_mode == "head_to_head" and not opponent:
            raise ApiError("master_stats mode=head_to_head requires opponent.")
        return self.get_json(
            "/api/v1/stats/",
            {
                "mode": selected_mode,
                "player": player,
                "opponent": opponent,
            },
        )

    def master_game(self, token: str) -> dict[str, Any]:
        game_token = extract_master_game_token(token)
        return self.get_json(f"/api/v1/games/{game_token}/")

    def master_pgn(self, token: str) -> str:
        game_token = extract_master_game_token(token)
        return self.get_text(f"/api/v1/games/{game_token}/pgn/")

    def master_games(
        self,
        query: str,
        *,
        page: int = 1,
        page_size: int = 50,
        all_pages: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if not query:
            raise ApiError("master_games requires a query.")
        return self._paginated(
            "/api/v1/games/",
            {"q": query},
            page=page,
            page_size=page_size,
            all_pages=all_pages,
            limit=limit,
        )

    def export_master_games(
        self,
        *,
        query: str | None = None,
        tokens: Iterable[str] | None = None,
        format: str = "pgn",
        pgn_in_json: bool = True,
    ) -> str:
        params: dict[str, Any] = {"format": format}
        token_values = tuple(tokens or ())
        if token_values:
            params["tokens"] = ",".join(extract_master_game_token(token) for token in token_values)
        elif query:
            params["q"] = query
        else:
            raise ApiError("export_master_games requires query or tokens.")
        if format == "ndjson":
            params["pgnInJson"] = "true" if pgn_in_json else "false"
        return self.get_text("/api/v1/games/export/", params)

    def public_players(self, query: str | None = None) -> dict[str, Any]:
        return self.get_json("/api/v1/public/players/", {"q": query})

    def public_player(self, player_slug: str) -> dict[str, Any]:
        return self.get_json(public_player_path(player_slug))

    def public_player_bio(self, player_slug: str) -> dict[str, Any]:
        return self.get_json(public_player_path(player_slug, 'bio/'))

    def public_notable_games(self, player_slug: str) -> dict[str, Any]:
        return self.get_json(public_player_path(player_slug, 'notable-games/'))

    def public_events(self, query: str | None = None) -> dict[str, Any]:
        return self.get_json("/api/v1/public/events/", {"q": query})

    def public_games(
        self,
        query: str = "",
        *,
        archive_player: str | None = None,
        archive_event: str | None = None,
        since: int | None = None,
        until: int | None = None,
        sort: str = "asc",
        page: int = 1,
        page_size: int = 50,
        all_pages: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return self._paginated(
            "/api/v1/public/games/",
            {
                "q": query,
                "archive_player": archive_player,
                "archive_event": archive_event,
                "since": since,
                "until": until,
                "sort": sort,
            },
            page=page,
            page_size=page_size,
            all_pages=all_pages,
            limit=limit,
        )

    def public_game(self, token: str) -> dict[str, Any]:
        game_path = "/".join(extract_public_game_token(token))
        return self.get_json(f"/api/v1/public/games/{game_path}/")

    def public_pgn(self, token: str) -> str:
        game_path = "/".join(extract_public_game_token(token))
        return self.get_text(f"/api/v1/public/games/{game_path}/pgn/")

    def export_public_games(
        self,
        *,
        query: str = "",
        archive_player: str | None = None,
        archive_event: str | None = None,
        since: int | None = None,
        until: int | None = None,
        tokens: Iterable[str] | None = None,
        sort: str = "asc",
        format: str = "pgn",
        pgn_in_json: bool = True,
    ) -> str:
        params: dict[str, Any] = {
            "format": format,
            "archive_player": archive_player,
            "archive_event": archive_event,
            "since": since,
            "until": until,
            "sort": sort,
        }
        token_values = tuple(tokens or ())
        if token_values:
            params["tokens"] = ",".join(
                "/".join(extract_public_game_token(token)) for token in token_values
            )
        else:
            params["q"] = query
        if format == "ndjson":
            params["pgnInJson"] = "true" if pgn_in_json else "false"
        return self.get_text("/api/v1/public/games/export/", params)

    def account_me(self, *, api_token: str | None = None) -> dict[str, Any]:
        return self.request_json("GET", "/api/v1/account/me/", api_token=api_token)

    def account_collections(self, *, api_token: str | None = None) -> dict[str, Any]:
        return self.request_json("GET", "/api/v1/account/collections/", api_token=api_token)

    def account_create_collection(
        self, name: str, *, api_token: str | None = None
    ) -> dict[str, Any]:
        return self.request_json(
            "POST",
            "/api/v1/account/collections/",
            {"name": name},
            api_token=api_token,
        )

    def account_import_public_player_games(
        self,
        *,
        name: str | None = None,
        collection_id: int | None = None,
        archive_player: str,
        since: int | None = None,
        until: int | None = None,
        api_token: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": "public_player_games",
            "name": name,
            "collection_id": collection_id,
            "archive_player": archive_player,
            "since": since,
            "until": until,
        }
        return self.request_json(
            "POST",
            "/api/v1/account/collections/import/",
            payload,
            api_token=api_token,
        )

    def players(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        return self.get_json("/api/v1/players/", {"q": query, "limit": limit})

    def explorer(
        self,
        *,
        fen: str | None = None,
        play: str | None = None,
        moves: int = 12,
        top_games: int | None = None,
        source_type: str | None = None,
        source_key: str | None = None,
    ) -> dict[str, Any]:
        return self.get_json(
            "/api/v1/opening-explorer/",
            {
                "fen": fen,
                "play": play,
                "moves": moves,
                "topGames": top_games,
                "source_type": source_type,
                "source_key": source_key,
            },
        )

    def explorer_sources(self) -> dict[str, Any]:
        return self.get_json("/api/v1/opening-explorer/sources/")

    def annotated_books(self) -> dict[str, Any]:
        return self.get_json("/api/v1/annotated/books/")

    def annotated_games(
        self,
        book_slug: str,
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        return self.get_json(
            f"/api/v1/annotated/books/{book_slug}/games/",
            {"page": page, "page_size": page_size},
        )

    def annotated_game(self, book_slug: str, game_slug: str) -> dict[str, Any]:
        return self.get_json(f"/api/v1/annotated/books/{book_slug}/games/{game_slug}/")

    def annotated_pgn(self, book_slug: str, game_slug: str | None = None) -> str:
        if game_slug:
            path = f"/api/v1/annotated/books/{book_slug}/games/{game_slug}/pgn/"
        else:
            path = f"/api/v1/annotated/books/{book_slug}/pgn/"
        return self.get_text(path)

    def pgn_text_for_games(self, games: list[dict[str, Any]]) -> str:
        pgns = []
        for game in games:
            path = game.get("urls", {}).get("api_pgn")
            if not path:
                raise ApiError(f"Game result has no PGN URL: {game!r}")
            body, _ = self.fetch_url(urljoin(self.base_url, path.lstrip("/")))
            pgns.append(body.decode("utf-8").strip())
        return "\n\n".join(pgns) + ("\n" if pgns else "")

    def _paginated(
        self,
        path: str,
        params: dict[str, Any],
        *,
        page: int,
        page_size: int,
        all_pages: bool,
        limit: int | None,
    ) -> dict[str, Any]:
        first_params = {**params, "page": page, "page_size": page_size}
        if not all_pages:
            return self.get_json(path, first_params)
        return collect_pages(self.url(path, first_params), self.fetch_json, page=page, limit=limit)


def fetch_url(url: str) -> tuple[bytes, str]:
    """Compatibility helper for callers that only need one raw URL fetch."""
    return AndromedaClient().fetch_url(url)


def fetch_json(url: str) -> dict[str, Any]:
    """Compatibility helper for callers that only need one JSON URL fetch."""
    return AndromedaClient().fetch_json(url)
