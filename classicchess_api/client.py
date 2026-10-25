"""Python client for the public Classic Chess archive API."""

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
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler
from urllib.request import Request
from urllib.request import build_opener
from urllib.request import urlopen

from classicchess_api.origin_policy import CredentialOriginError
from classicchess_api.origin_policy import SameOriginHTTPSRedirectHandler
from classicchess_api.origin_policy import require_credentialed_url
from classicchess_api.response import DEFAULT_MAX_RESPONSE_BYTES
from classicchess_api.response import ApiError
from classicchess_api.response import collect_pages
from classicchess_api.response import continuation_url
from classicchess_api.response import decode_json_object
from classicchess_api.response import read_bounded

DEFAULT_BASE_URL = "https://classicchess.com"
USER_AGENT = "classicchess-python-client/0.1.1"
LICHESS_USERNAME = re.compile(r"^[A-Za-z0-9_-]{2,30}$")
LICHESS_EXPLORER_COLORS = ("both", "white", "black")
LICHESS_EXPLORER_RESULTS = ("all", "win", "loss", "draw")


def _collection_identifier(value):
    if type(value) is not int or not 1 <= value <= 2147483647:
        raise ApiError('Use a positive collection ID.', code='invalid_request')
    return value


def _collection_name_query(name):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise ApiError('Use a collection name of 1–120 characters.', code='invalid_request')
    return urlencode({'name': name.strip()})


def _curated_collection_payload(collection_id, filters, exclude, batch_size, after_game_id, dry_run):
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ApiError('Use a batch size from 1 to 1000.', code='invalid_request')
    if type(after_game_id) is not int or not 0 <= after_game_id <= 2147483647 or type(dry_run) is not bool:
        raise ApiError('Use a nonnegative game ID and a boolean dry_run.', code='invalid_request')
    return {'source': 'curated_games', 'collection_id': _collection_identifier(collection_id),
            'filters': filters, 'exclude': [] if exclude is None else exclude, 'batch_size': batch_size,
            'after_game_id': after_game_id, 'dry_run': dry_run}


def ccnb_format_header(value):
    if value is None:
        return None
    if type(value) is not int or value not in (2, 3):
        raise ApiError('CCNB format must be 2 or 3.', code='invalid_request')
    return str(value)


class _NoDownloadRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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



def public_slug_segment(value, label='slug'):
    """One exact catalog slug or ID, safe to place in a URL path."""
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,255}', value) is None:
        raise ApiError(f'Use an exact {label} from the API.', code='invalid_query')
    return value


def public_username_segment(value):
    """An importer's username from a game page address, safe to place in a URL path."""
    if (not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9@.+_-]{1,150}', value) is None
            or value in ('.', '..')):
        raise ApiError('Use the exact username from the game page address.', code='invalid_query')
    return value


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



PUBLIC_GAME_SEARCH_MODES = ("all", "players", "openings")


def public_game_search_mode(value: str | None) -> str | None:
    """players: players, event, site and date. openings: opening name and ECO. all: both."""
    if value is not None and value not in PUBLIC_GAME_SEARCH_MODES:
        raise ApiError("search_mode must be all, players or openings.", code="invalid_query")
    return value

class ClassicChessClient:
    """Dependency-free client for Classic Chess JSON, PGN, and account API endpoints."""

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
        follow_redirects: bool = True,
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
            elif not follow_redirects:
                response_context = build_opener(_NoDownloadRedirect()).open(request, timeout=self.timeout)
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
            raise ApiError("A Classic Chess API token is required for account commands.")
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

    def public_player_cards(self, query: str | None = None) -> dict[str, Any]:
        """The players catalog for lists and pickers: biography headline only, about a tenth the size."""
        return self.get_json("/api/v1/public/players/cards/", {"q": query})

    def discovery(self) -> dict[str, Any]:
        return self.get_json("/api/v1/")

    def player_names(self, query: str | None = None) -> list[str]:
        return [player['name'] for player in self.public_player_cards(query)['results']]

    def event_names(self, query: str | None = None) -> list[str]:
        return [event['name'] for event in self.public_events(query)['results']]

    def book_titles(self, *, ccnb_format=None) -> list[str]:
        return [book['label'] for book in self.annotated_books(ccnb_format=ccnb_format)['results']]

    def event_series(self, query: str | None = None, series: str | None = None) -> dict[str, Any]:
        if query and series or query and len(query) > 120 or series and not re.fullmatch(r'[a-z0-9-]{1,160}', series):
            raise ApiError('Use either an event query of at most 120 characters or an exact series slug.', code='invalid_query')
        return self.get_json('/api/v1/public/event-series/', {'q': query, 'series': series})

    def public_player(self, player_slug: str) -> dict[str, Any]:
        return self.get_json(public_player_path(player_slug))

    def public_player_bio(self, player_slug: str) -> dict[str, Any]:
        return self.get_json(public_player_path(player_slug, 'bio/'))

    def public_notable_games(self, player_slug: str) -> dict[str, Any]:
        return self.get_json(public_player_path(player_slug, 'notable-games/'))

    def public_events(self, query: str | None = None) -> dict[str, Any]:
        return self.get_json("/api/v1/public/events/", {"q": query})

    def public_event(self, event_slug: str) -> dict[str, Any]:
        return self.get_json(f"/api/v1/public/events/{public_slug_segment(event_slug, 'event slug')}/")

    def public_event_about(self, event_slug: str) -> dict[str, Any]:
        return self.get_json(f"/api/v1/public/events/{public_slug_segment(event_slug, 'event slug')}/about/")

    def gallery(
        self, query: str | None = None, *, page: int = 1, page_size: int = 48,
    ) -> dict[str, Any]:
        return self.get_json(
            "/api/v1/public/gallery/", {"q": query, "page": page, "page_size": page_size},
        )

    def gallery_photo(self, photo_id: str | int) -> dict[str, Any]:
        return self.get_json(f"/api/v1/public/gallery/{public_slug_segment(str(photo_id), 'photo ID')}/")

    def beginner_games(self) -> dict[str, Any]:
        return self.get_json("/api/v1/public/beginner-games/")

    def daily_game(self) -> dict[str, Any]:
        return self.get_json("/api/v1/public/daily/")

    def event_of_the_day(self) -> dict[str, Any]:
        """The homepage's Event of the day; ``event`` is None when none can be published."""
        return self.get_json("/api/v1/public/event-of-the-day/")

    def todays_picks(self) -> dict[str, Any]:
        """Every face of today's homepage card; each of player, game, on_this_day and event may be None."""
        return self.get_json("/api/v1/public/todays-picks/")

    def site_search(
        self, query: str, *, kind: str | None = None, page: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 120:
            raise ApiError('Use a search query of 1 to 120 characters.', code='invalid_query')
        if kind not in (None, 'games', 'events', 'players'):
            raise ApiError('kind must be games, events, or players.', code='invalid_query')
        if page is not None and kind is None:
            raise ApiError('page requires kind.', code='invalid_query')
        return self.get_json("/api/v1/public/search/", {"q": query, "kind": kind, "page": page})

    def tablebase(self, fen: str) -> dict[str, Any]:
        if not isinstance(fen, str) or not fen.strip() or len(fen.encode('utf-8')) > 200:
            raise ApiError('Use a FEN of at most 200 bytes.', code='invalid_query')
        return self.get_json("/api/v1/tablebase/", {"fen": fen})

    def public_games(
        self,
        query: str = "",
        *,
        archive_player: str | None = None,
        archive_event: str | None = None,
        search_mode: str | None = None,
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
                "search_mode": public_game_search_mode(search_mode),
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

    def public_imported_game(self, username: str, game_slug: str) -> dict[str, Any]:
        """A game another account imported and made public, by its page address."""
        user = public_username_segment(username)
        game = public_slug_segment(game_slug, 'game slug')
        return self.get_json('/api/v1/public/imported-games/' + user + '/' + game + '/')

    def public_imported_pgn(self, username: str, game_slug: str) -> str:
        user = public_username_segment(username)
        game = public_slug_segment(game_slug, 'game slug')
        return self.get_text('/api/v1/public/imported-games/' + user + '/' + game + '/pgn/')

    def export_public_games(
        self,
        *,
        query: str = "",
        archive_player: str | None = None,
        archive_event: str | None = None,
        search_mode: str | None = None,
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
            params["search_mode"] = public_game_search_mode(search_mode)
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

    def account_find_collection(self, name: str, *, api_token: str | None = None) -> dict[str, Any]:
        """Return count/results for the exact name, ignoring case; empty when absent."""
        return self.request_json('GET', '/api/v1/account/collections/?' + _collection_name_query(name), api_token=api_token)

    def account_delete_collection(self, collection_id: int, *, api_token: str | None = None) -> dict[str, Any]:
        """Delete an owned collection, memberships, notes and progress; keep underlying games."""
        return self.request_json('DELETE', f'/api/v1/account/collections/{_collection_identifier(collection_id)}/', api_token=api_token)

    def account_add_curated_games(self, collection_id: int, filters: list[dict[str, Any]], *,
                                 exclude: list[dict[str, Any]] | None = None, batch_size: int = 1000,
                                 after_game_id: int = 0, dry_run: bool = False,
                                 api_token: str | None = None) -> dict[str, Any]:
        """Add one curated-only batch. Repeat with next_after_game_id while has_more is true.

        Criteria within each group are AND; groups are OR; exclude subtracts groups.
        Keep filters unchanged between calls. Dry runs make no changes. No automatic retries.
        """
        payload = _curated_collection_payload(collection_id, filters, exclude, batch_size, after_game_id, dry_run)
        return self.request_json('POST', '/api/v1/account/collections/import/', payload, api_token=api_token)

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

    def lichess_explorer(
        self,
        *,
        fen: str | None = None,
        player: str | None = None,
        color: str = "both",
        result: str = "all",
    ) -> dict[str, Any]:
        """Explore a position in the Lichess database through Classic Chess.

        The site relays the request with its own Lichess credential, so no
        Lichess token is needed. Without ``player`` the answer covers the whole
        Lichess database; with a Lichess username it covers that player's games.
        """
        player = (player or "").strip()
        if fen is not None and len(fen) > 100:
            raise ApiError("Use a FEN of at most 100 characters.", code="invalid_query")
        if player and not LICHESS_USERNAME.fullmatch(player):
            raise ApiError("Use a valid Lichess username.", code="invalid_query")
        if color not in LICHESS_EXPLORER_COLORS or result not in LICHESS_EXPLORER_RESULTS:
            raise ApiError("color must be both, white or black; result must be all, win, loss or draw.",
                           code="invalid_query")
        if not player and result != "all":
            raise ApiError("A Lichess username is needed for result filters.", code="invalid_query")
        params: dict[str, Any] = {"fen": fen or None}
        if player:
            params.update({"player": player, "color": color, "result": result})
        return self.get_json("/api/v1/opening-explorer/lichess/", params)

    def annotated_books(self, *, ccnb_format=None) -> dict[str, Any]:
        header = ccnb_format_header(ccnb_format)
        if header is None:
            return self.get_json('/api/v1/annotated/books/')
        return self.fetch_json(self.url('/api/v1/annotated/books/'), headers={'Accept': 'application/json', 'CCNB-Format': header})

    def annotated_notebook_file(self, book_slug: str, *, ccnb_format=None) -> bytes:
        """Exact package bytes; use the same ccnb_format as annotated_books for matching revisions."""
        slug = public_slug_segment(book_slug, 'book slug')
        header = ccnb_format_header(ccnb_format)
        try:
            origin = urlsplit(self.base_url)
            valid = (origin.scheme in ('https', 'http') and origin.hostname and not origin.username
                     and not origin.password and not origin.query and not origin.fragment
                     and (origin.port is None or 1 <= origin.port <= 65535))
        except ValueError as exc:
            raise ApiError('Use an HTTP(S) API origin.', code='invalid_options') from exc
        if not valid:
            raise ApiError('Use an HTTP(S) API origin.', code='invalid_options')
        headers = {'Accept': 'application/vnd.classicchess.notebook'}
        if header is not None:
            headers['CCNB-Format'] = header
        body, media_type = self.fetch_url(urljoin(self.base_url, f'/annotated/{slug}/download/'), headers=headers, follow_redirects=False)
        if not media_type.startswith('application/vnd.classicchess.notebook'):
            raise ApiError('Response was not a Notebook file.', code='invalid_response')
        return body

    def annotated_games(
        self,
        book_slug: str,
        *,
        page: int = 1,
        page_size: int = 50,
        all_pages: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return self._paginated(
            f"/api/v1/annotated/books/{book_slug}/games/",
            {}, page=page, page_size=page_size, all_pages=all_pages, limit=limit,
        )

    def iterate_public_games(self, query: str = '', *, archive_player=None, archive_event=None,
                             search_mode=None, since=None, until=None, sort='asc', page=1,
                             page_size=50, limit: int | None = None, max_pages: int = 100_000):
        url = self._iteration_url('/api/v1/public/games/', {
            'q': query, 'archive_player': archive_player, 'archive_event': archive_event,
            'search_mode': public_game_search_mode(search_mode), 'since': since, 'until': until, 'sort': sort,
        }, page, page_size)
        yield from self._iterate(url, limit, max_pages)

    def iterate_master_games(self, query: str, *, page=1, page_size=50,
                             limit: int | None = None, max_pages: int = 100_000):
        if not query:
            raise ApiError('iterate_master_games requires a query.', code='invalid_query')
        url = self._iteration_url('/api/v1/games/', {'q': query}, page, page_size)
        yield from self._iterate(url, limit, max_pages)

    def iterate_annotated_games(self, book_slug: str, *, page=1, page_size=50,
                                limit: int | None = None, max_pages: int = 100_000):
        url = self._iteration_url(f'/api/v1/annotated/books/{book_slug}/games/', {}, page, page_size)
        yield from self._iterate(url, limit, max_pages)

    def _iteration_url(self, path, params, page, page_size):
        if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 100:
            raise ApiError('Use a positive page and page_size between 1 and 100.', code='invalid_pagination')
        return self.url(path, {**params, 'page': page, 'page_size': page_size})

    def _iterate(self, first: str, limit: int | None, max_pages: int):
        if (type(max_pages) is not int or max_pages < 1
                or limit is not None and (type(limit) is not int or limit < 0)):
            raise ApiError('Use positive max_pages and a nonnegative limit.', code='invalid_pagination')
        if limit == 0:
            return
        count = 0
        current = first
        seen = set()
        while True:
            if current in seen or len(seen) >= max_pages:
                raise ApiError('Pagination repeated a page or exceeded max_pages.', code='invalid_pagination')
            seen.add(current)
            page = self.fetch_json(current)
            if not isinstance(page, dict) or not isinstance(page.get('results'), list) or 'next' not in page:
                raise ApiError('Response is not a game page.', code='invalid_response')
            for game in page['results']:
                yield game
                count += 1
                if limit is not None and count >= limit:
                    return
            if page['next'] is None:
                return
            current = continuation_url(first, current, page['next'])

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
    return ClassicChessClient().fetch_url(url)


def fetch_json(url: str) -> dict[str, Any]:
    """Compatibility helper for callers that only need one JSON URL fetch."""
    return ClassicChessClient().fetch_json(url)
