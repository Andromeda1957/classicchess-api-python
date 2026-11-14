"""Bounded application requests with explicit per-request credentials."""

import json
import math
import re
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.parse import urljoin
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler
from urllib.request import Request
from urllib.request import build_opener
from uuid import uuid4

from .client import DEFAULT_BASE_URL
from .client import USER_AGENT
from .client import _collection_identifier
from .client import _collection_name_query
from .client import _curated_collection_payload
from .client import ccnb_format_header
from .client import encode_non_ascii
from .client import public_slug_segment
from .client import public_username_segment
from .response import DEFAULT_MAX_RESPONSE_BYTES
from .response import ApiError
from .response import decode_json_object
from .response import read_bounded

# GIF exports need a registered account, so bearers may also reach the four public GIF routes.
GIF_PATH = (r'api/v1/(?:games/[^/?]+|public/games/[^/?]+|public/imported-games/[^/?]+/[^/?]+'
            r'|annotated/books/[^/?]+/games/[^/?]+)/gif/(?:\?|$)')
PRIVATE_PATH = re.compile(
    r'^/(?:api/v1/(?:account/|desktop/|mobile/(?:notebooks/|position-scan/))|cast/api/mobile/|' + GIF_PATH + ')'
)
ORIENTATIONS = ('white', 'black')


def _collection_id(value):
    if type(value) is not int or value < 1:
        raise ApiError('Use a positive collection ID.', code='invalid_request')
    return value


def _positive_id(value, label):
    if type(value) is not int or value < 1:
        raise ApiError(f'Use a positive {label}.', code='invalid_request')
    return value


def _notebook_uuid(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}', value):
        raise ApiError('Use a Notebook UUID from the API.', code='invalid_query')
    return value.lower()


def _gif_query(orientation):
    if orientation not in ORIENTATIONS:
        raise ApiError('orientation must be white or black.', code='invalid_query')
    return '' if orientation == 'white' else '?orientation=black'


def _filename(disposition):
    match = re.search(r'filename="([^"\\/\r\n]{1,255})"', disposition or '')
    return match.group(1) if match else None


def _page_query(page, page_size):
    if type(page) is not int or type(page_size) is not int or page < 1 or not 1 <= page_size <= 100:
        raise ApiError('Use a positive page and a page size from 1 to 100.', code='invalid_request')
    return f'page={page}&page_size={page_size}'


def _puzzle_filter(result):
    if result is not None and result != 'missed':
        raise ApiError('result must be missed, or omitted for all puzzles.', code='invalid_request')
    return '&result=missed' if result else ''


UCI_MOVE = re.compile(r'[a-h][1-8][a-h][1-8][qrbn]?')
GYM_COLORS = ('white', 'black', 'random')
GYM_PLAYERS = ('me', 'others')


def _gym_game(game_id):
    return '/api/v1/account/gym/games/' + str(_positive_id(game_id, 'game ID')) + '/'


def _ply(value):
    if type(value) is not int or value < 0:
        raise ApiError('Use the ply from the game state: a whole number from 0.', code='invalid_request')
    return value


PUZZLE_ID = re.compile(r'p-[0-9a-f]{8}')
PUZZLE_RESULTS = ('solved', 'missed')


def _puzzle_id(value):
    if not isinstance(value, str) or not PUZZLE_ID.fullmatch(value):
        raise ApiError('Use a puzzle ID such as p-000ccc1f.', code='invalid_request')
    return value


def _gym_new_game_body(color, time, minutes, increment, version=None):
    if color not in GYM_COLORS:
        raise ApiError('color must be white, black or random.', code='invalid_request')
    if not isinstance(time, str) or not time:
        raise ApiError('time must be unlimited, a preset such as 3+0, or custom.', code='invalid_request')
    if version is not None and (not isinstance(version, str) or not version):
        raise ApiError("version must be a key from the bot's versions, such as v4.", code='invalid_request')
    body = {'color': color, 'time': time}
    if version is not None:
        body['version'] = version
    if time == 'custom':
        if type(minutes) is not int or minutes < 1 or type(increment) is not int or increment < 0:
            raise ApiError('A custom time needs whole minutes from 1 and increment seconds from 0.',
                           code='invalid_request')
        body.update(minutes=minutes, increment=increment)
    elif minutes is not None or increment is not None:
        raise ApiError('minutes and increment apply only to time="custom".', code='invalid_request')
    return body


LOOPBACK_HOSTS = ('localhost', '127.0.0.1', '::1')
COLLECTION_SORTS = ('added', 'date_desc', 'date_asc', 'player', 'manual')
IMPORT_SOURCES = ('lichess', 'chesscom', 'pgn')
IMPORTED_GAME_SORTS = ('imported_desc', 'imported_asc', 'date_desc', 'date_asc', 'player', 'visibility')
NOTEBOOK_VISIBILITIES = ('private', 'unlisted', 'public')
NOTEBOOK_ROLES = ('viewer', 'commenter', 'editor', 'manager')


def _choice(value, choices, label):
    if value is not None and value not in choices:
        raise ApiError(f'{label} must be one of: {", ".join(choices)}.', code='invalid_request')
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ApiError(f'Use a non-empty {label}.', code='invalid_request')
    return value


def _present(**fields):
    """The fields given, leaving out None, for an optional or partial JSON body."""
    return {key: value for key, value in fields.items() if value is not None}


def _changes(label, **fields):
    payload = _present(**fields)
    if not payload:
        raise ApiError(f'Provide at least one {label} to change.', code='invalid_request')
    return payload


def _list_query(page, page_size, **filters):
    extra = _present(**filters)
    return _page_query(page, page_size) + ('&' + urlencode(extra) if extra else '')


def _optional_id(value, label):
    return None if value is None else _positive_id(value, label)


def _collection_path(collection_id, suffix=''):
    return f'/api/v1/account/collections/{_collection_id(collection_id)}/' + suffix


def _notebook_path(notebook_uuid, suffix=''):
    return '/api/v1/account/notebooks/' + _notebook_uuid(notebook_uuid) + '/' + suffix


def _section_path(notebook_uuid, section_id, suffix=''):
    return _notebook_path(notebook_uuid, f'sections/{_positive_id(section_id, "section ID")}/' + suffix)


def _chapter_path(notebook_uuid, chapter_id, suffix=''):
    return _notebook_path(notebook_uuid, f'chapters/{_positive_id(chapter_id, "chapter ID")}/' + suffix)


def _plan_items(items):
    """Games in playback order: slugs, or {"game": slug, "note": text} objects."""
    values = [] if isinstance(items, (str, dict)) else [
        {'game': item} if isinstance(item, str) else item for item in items]
    if not 1 <= len(values) <= 100 or not all(
            isinstance(item, dict) and isinstance(item.get('game'), str) and item['game'] for item in values):
        raise ApiError('Use 1 to 100 games: slugs or {"game": slug, "note": text}.', code='invalid_request')
    return values


def _item_ids(values):
    # A string such as '78' is not a list of IDs; it had become an empty list
    # that passed this check and was sent.
    ids = None if isinstance(values, (str, bytes)) else list(values)
    if ids is None or len(ids) > 10000 or not all(type(item) is int and item > 0 for item in ids):
        raise ApiError('Use every collection item ID once, as positive integers.', code='invalid_request')
    return ids


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class ApplicationResponse:
    status: int
    data: dict
    retry_after: str | None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass(frozen=True)
class ApplicationDownload:
    """A file response: GIF, PGN or .ccnb bytes when ok, else the JSON error in ``error``."""

    status: int
    content: bytes
    content_type: str
    filename: str | None
    retry_after: str | None
    error: dict

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class ApplicationClient:
    """Account, notebook, scanner, Remote and Cast APIs for trusted app callers.

    HTTP status and JSON are returned together, including conflict responses.
    Network failures and malformed responses raise ApiError. No cookies,
    redirects, automatic retries or stored credentials are used.
    """

    def __init__(self, base_url=DEFAULT_BASE_URL, *, user_agent=USER_AGENT,
                 timeout=30, max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES):
        try:
            base = urlsplit(base_url)
            valid = (base.scheme in ('https', 'http') and base.hostname
                     and base.path in ('', '/') and not base.username and not base.password
                     and not base.query and not base.fragment)
            port = base.port
            valid = valid and (port is None or 1 <= port <= 65535)
        except (TypeError, ValueError) as exc:
            raise ApiError('Use an HTTP(S) API origin.', code='invalid_options') from exc
        if (not valid or not isinstance(timeout, (float, int)) or not math.isfinite(timeout)
                or not 0 < timeout <= 600 or type(max_response_bytes) is not int
                or not 1 <= max_response_bytes <= 1024 * 1024 * 1024):
            raise ApiError('Use an API origin and positive bounded timeout/response limits.', code='invalid_options')
        self.base_url = base_url.rstrip('/')
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def request(self, path: str, *, method='GET', body=None, content_type='application/json; charset=utf-8',
                token=None, timeout=None) -> ApplicationResponse:
        status, data, headers = self._exchange(path, method=method, body=body, content_type=content_type,
                                               token=token, timeout=timeout, accept='application/json')
        return ApplicationResponse(status, decode_json_object(data or b'{}'), headers.get('Retry-After'))

    def download(self, path: str, *, method='GET', body=None, content_type='application/json; charset=utf-8',
                 token=None, timeout=None, accept='*/*', ccnb_format=None) -> ApplicationDownload:
        """Fetch a file. The bytes are returned only for a 2xx; otherwise ``error`` holds the JSON reply."""
        status, data, headers = self._exchange(path, method=method, body=body, content_type=content_type,
                                               token=token, timeout=timeout, accept=accept, ccnb_format=ccnb_format)
        ok = 200 <= status < 300
        error = {} if ok else decode_json_object(data or b'{}')
        return ApplicationDownload(status, data if ok else b'', headers.get('Content-Type', ''),
                                   _filename(headers.get('Content-Disposition')) if ok else None,
                                   headers.get('Retry-After'), error)

    def _exchange(self, path, *, method, body, content_type, token, timeout, accept, ccnb_format=None):
        if (not isinstance(path, str) or len(path) > 8192 or re.search(r'[\\#\x00-\x20\x7f]', path)
                or re.search(r'%|/\.{1,2}(?:/|$)', path.split('?')[0])
                or not path.startswith(('/api/v1/', '/cast/api/mobile/'))):
            raise ApiError('Use a relative application API path.', code='unsafe_url')
        if token is not None:
            if not isinstance(token, str) or not re.fullmatch(r'[\x21-\x7e]{1,4096}', token):
                raise ApiError('Invalid bearer token.', code='invalid_token')
            origin = urlsplit(self.base_url)
            if not PRIVATE_PATH.match(path) or origin.scheme != 'https' and origin.hostname not in ('localhost', '127.0.0.1', '::1'):
                raise ApiError('Bearer credentials require a private API over HTTPS or loopback development.', code='unsafe_credentials')
        if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE') or method == 'GET' and body is not None:
            raise ApiError('Use a supported method; GET cannot carry a body.', code='invalid_request')
        deadline = self.timeout if timeout is None else timeout
        if not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or not 0 < deadline <= 600:
            raise ApiError('Use a timeout between zero and 600 seconds.', code='invalid_options')
        if isinstance(body, str):
            body = body.encode('utf-8')
        if body is not None and not isinstance(body, bytes):
            raise ApiError('Use JSON text or bytes.', code='invalid_request')
        if body is not None and len(body) > 128 * 1024 * 1024:
            raise ApiError('API request exceeds 128 MiB.', code='request_too_large')
        headers = {'Accept': accept, 'User-Agent': self.user_agent}
        format_header = ccnb_format_header(ccnb_format)
        if format_header is not None:
            notebook = re.fullmatch(r'/api/v1/account/notebooks/([^/]+)/file/', path)
            if notebook is None:
                raise ApiError('CCNB negotiation requires the fixed Notebook file endpoint.', code='invalid_request')
            _notebook_uuid(notebook[1])
            headers['CCNB-Format'] = format_header
        if token is not None:
            headers['Authorization'] = f'Bearer {token}'
        if body is not None:
            if not isinstance(content_type, str) or len(content_type) > 200 or '\r' in content_type or '\n' in content_type:
                raise ApiError('Invalid content type.', code='invalid_request')
            headers['Content-Type'] = content_type
        request = Request(urljoin(self.base_url, encode_non_ascii(path)), method=method, headers=headers, data=body)
        try:
            try:
                response = build_opener(NoRedirect()).open(request, timeout=deadline)
            except HTTPError as error:
                response = error
            with response:
                return response.status, read_bounded(response, self.max_response_bytes), response.headers
        except TimeoutError as exc:
            raise ApiError('API request timed out.', code='timeout') from exc
        except URLError as exc:
            code = 'timeout' if isinstance(exc.reason, TimeoutError) else 'network_error'
            raise ApiError('Could not reach the configured API.', code=code) from exc

    def account_me(self, token: str) -> ApplicationResponse:
        return self.request('/api/v1/account/me/', token=token)

    def account_collections(self, token: str) -> ApplicationResponse:
        return self.request('/api/v1/account/collections/', token=token)

    def account_create_collection(self, name: str, token: str) -> ApplicationResponse:
        return self.request('/api/v1/account/collections/', method='POST', body=json.dumps({'name': name}), token=token)

    def account_find_collection(self, name: str, token: str) -> ApplicationResponse:
        """Exact name ignoring case; returns count/results (empty when absent)."""
        return self.request('/api/v1/account/collections/?' + _collection_name_query(name), token=token)

    def account_delete_collection(self, collection_id: int, token: str) -> ApplicationResponse:
        """Delete the collection, memberships, notes and progress; keep underlying games."""
        return self.request(f'/api/v1/account/collections/{_collection_identifier(collection_id)}/', method='DELETE', token=token)

    def account_add_curated_games(self, collection_id: int, filters: list[dict], token: str, *,
                                 exclude: list[dict] | None = None, batch_size: int = 1000,
                                 after_game_id: int = 0, dry_run: bool = False) -> ApplicationResponse:
        """One curated-only batch: AND criteria, OR groups, subtract exclusions.

        Preview with dry_run. Keep filters and pass next_after_game_id while has_more.
        HTTP errors retain status/data; writes are never automatically retried.
        """
        payload = _curated_collection_payload(collection_id, filters, exclude, batch_size, after_game_id, dry_run)
        return self.request('/api/v1/account/collections/import/', method='POST', body=json.dumps(payload), token=token)

    def account_import_public_player_games(self, archive_player: str, token: str, *, name=None,
                                          collection_id=None, since=None, until=None) -> ApplicationResponse:
        payload = {'source': 'public_player_games', 'archive_player': archive_player, 'name': name,
                   'collection_id': collection_id, 'since': since, 'until': until}
        return self.request('/api/v1/account/collections/import/', method='POST',
                            body=json.dumps({key: value for key, value in payload.items() if value is not None}), token=token)

    def account_add_collection_game(self, collection_id: int, game_slug: str, token: str) -> ApplicationResponse:
        """Add one openable game; 201 when added, 200 with changed false if present."""
        return self.request(f'/api/v1/account/collections/{_collection_id(collection_id)}/items/', method='POST',
                            body=json.dumps({'game': public_slug_segment(game_slug, 'game slug')}), token=token)

    def account_starred_players(self, token: str, *, page=1, page_size=50) -> ApplicationResponse:
        return self.request(f'/api/v1/account/starred/players/?{_page_query(page, page_size)}', token=token)

    def account_star_player(self, player_slug: str, token: str) -> ApplicationResponse:
        return self.request(f'/api/v1/account/starred/players/{public_slug_segment(player_slug, "player slug")}/',
                            method='PUT', token=token)

    def account_unstar_player(self, player_slug: str, token: str) -> ApplicationResponse:
        return self.request(f'/api/v1/account/starred/players/{public_slug_segment(player_slug, "player slug")}/',
                            method='DELETE', token=token)

    def account_starred_games(self, token: str, *, page=1, page_size=50) -> ApplicationResponse:
        return self.request(f'/api/v1/account/starred/games/?{_page_query(page, page_size)}', token=token)

    def account_star_game(self, game_slug: str, token: str) -> ApplicationResponse:
        return self.request(f'/api/v1/account/starred/games/{public_slug_segment(game_slug, "game slug")}/',
                            method='PUT', token=token)

    def account_unstar_game(self, game_slug: str, token: str) -> ApplicationResponse:
        return self.request(f'/api/v1/account/starred/games/{public_slug_segment(game_slug, "game slug")}/',
                            method='DELETE', token=token)

    def account_set_imported_game_visibility(self, game_slug: str, visibility: str, token: str) -> ApplicationResponse:
        if visibility not in ('private', 'public'):
            raise ApiError('visibility must be private or public.', code='invalid_request')
        return self.request(f'/api/v1/account/imported-games/{public_slug_segment(game_slug, "game slug")}/',
                            method='PATCH', body=json.dumps({'visibility': visibility}), token=token)

    def account_delete_imported_game(self, game_slug: str, token: str) -> ApplicationResponse:
        """Permanently delete an owned import, including from every collection and star list."""
        return self.request(f'/api/v1/account/imported-games/{public_slug_segment(game_slug, "game slug")}/',
                            method='DELETE', token=token)

    def account_imported_games(self, token: str, *, q=None, source=None, sort=None,
                               page=1, page_size=50) -> ApplicationResponse:
        """Your imports with their quota; source is lichess, chesscom or pgn. Needs library:read."""
        query = _list_query(page, page_size, q=q, source=_choice(source, IMPORT_SOURCES, 'source'),
                            sort=_choice(sort, IMPORTED_GAME_SORTS, 'sort'))
        return self.request('/api/v1/account/imported-games/?' + query, token=token)

    def account_import_game(self, source: str, token: str, *, pgn=None, remote_url=None,
                            visibility=None) -> ApplicationResponse:
        """Import one game (201): source "pgn" with pgn text, or "lichess"/"chesscom" with remote_url.

        Needs library:write. Each call imports another game; a 429 carries retry_after.
        """
        if source not in IMPORT_SOURCES:
            raise ApiError('source must be lichess, chesscom or pgn.', code='invalid_request')
        payload = _present(source=source, pgn=pgn, remote_url=remote_url,
                           visibility=_choice(visibility, ('private', 'public'), 'visibility'))
        return self.request('/api/v1/account/imported-games/', method='POST', body=json.dumps(payload), token=token)

    def account_imported_game(self, game_slug: str, token: str) -> ApplicationResponse:
        """One of your imports with its PGN and board-ready mainline."""
        return self.request(f'/api/v1/account/imported-games/{public_slug_segment(game_slug, "game slug")}/',
                            token=token)

    # Collections and study plans. Personal tokens need library:read to read and
    # library:write to change them; device sessions pass both.
    def account_collection_games(self, collection_id: int, token: str, *, page=1, page_size=50,
                                 sort=None, q=None) -> ApplicationResponse:
        """One page of a collection's games. q keeps games whose players, event, date, opening or ECO
        contain it, as the website's collection filter does; sort defaults to the saved one."""
        query = _list_query(page, page_size, sort=_choice(sort, COLLECTION_SORTS, 'sort'), q=q)
        return self.request(_collection_path(collection_id) + '?' + query, token=token)

    def account_update_collection(self, collection_id: int, token: str, *, name=None, sort=None,
                                  description=None, is_plan=None) -> ApplicationResponse:
        """Change only the given fields; is_plan=True selects manual order unless sort is given."""
        payload = _changes('collection field', name=name, sort=_choice(sort, COLLECTION_SORTS, 'sort'),
                           description=description, is_plan=is_plan)
        return self.request(_collection_path(collection_id), method='PATCH', body=json.dumps(payload), token=token)

    def account_update_collection_item(self, collection_id: int, item_id: int, token: str, *, note=None,
                                       studied=None) -> ApplicationResponse:
        """Save a game's note and/or studied mark; studied=False clears it. Safe to retry."""
        payload = _changes('of note or studied', note=note, studied=studied)
        path = _collection_path(collection_id, f'items/{_positive_id(item_id, "item ID")}/')
        return self.request(path, method='PATCH', body=json.dumps(payload), token=token)

    def account_remove_collection_item(self, collection_id: int, item_id: int, token: str) -> ApplicationResponse:
        """Remove a game by its collection item ID; a game already gone is not an error."""
        path = _collection_path(collection_id, f'items/{_positive_id(item_id, "item ID")}/')
        return self.request(path, method='DELETE', token=token)

    def account_remove_collection_game(self, collection_id: int, game_slug: str, token: str) -> ApplicationResponse:
        """Remove a game by its slug, the counterpart of account_add_collection_game; changed is
        false when it was not there. Works after its importer made it private."""
        path = _collection_path(collection_id, f'games/{public_slug_segment(game_slug, "game slug")}/')
        return self.request(path, method='DELETE', token=token)

    def account_create_plan(self, name: str, items, idempotency_key: str, token: str, *,
                            description=None) -> ApplicationResponse:
        """Create a manual-order study plan of 1 to 100 games in one step (201).

        items are game slugs or {"game": slug, "note": text}. Retrying with the same
        idempotency_key and input returns the existing plan (200); other input is 409.
        """
        payload = _present(name=_text(name, 'plan name'), description=description,
                           idempotency_key=_text(idempotency_key, 'idempotency key'), items=_plan_items(items))
        return self.request('/api/v1/account/plans/', method='POST', body=json.dumps(payload), token=token)

    def account_reorder_collection(self, collection_id: int, item_ids, token: str) -> ApplicationResponse:
        """Set manual order: every current item ID exactly once, in the order wanted."""
        return self.request(_collection_path(collection_id, 'reorder/'), method='PUT',
                            body=json.dumps({'item_ids': _item_ids(item_ids)}), token=token)

    def account_plan_progress(self, collection_id: int, token: str, *, page=1, page_size=50) -> ApplicationResponse:
        """Studied progress for a collection's games."""
        return self.request(_collection_path(collection_id, 'progress/?' + _page_query(page, page_size)), token=token)

    def account_toggle_player_bookmark(self, player_slug: str, token: str) -> ApplicationResponse:
        """Flip a player's bookmark and return the new state. A toggle: never retry it automatically."""
        path = f'/api/v1/account/players/{public_slug_segment(player_slug, "player slug")}/bookmark/'
        return self.request(path, method='POST', token=token)

    def account_game_library(self, game_slug: str, token: str) -> ApplicationResponse:
        """Whether you starred a game and which of your collections hold it. Needs library:read."""
        return self.request(f'/api/v1/account/games/{public_slug_segment(game_slug, "game slug")}/library/',
                            token=token)

    # Website preferences: preferences:read to read, preferences:write to change.
    def account_preferences(self, token: str) -> ApplicationResponse:
        """The website's Settings values with the choices each one accepts (``catalogs``)."""
        return self.request('/api/v1/account/preferences/', token=token)

    def account_update_preferences(self, token: str, **changes) -> ApplicationResponse:
        """Change only the named preferences, such as board_theme="green" or engine_lines=3.

        Keys are the fields account_preferences returns. When any value is refused,
        nothing changes. The Android app's own sound and speech are never touched.
        """
        if not changes or any(value is None for value in changes.values()):
            raise ApiError('Name at least one preference to change, without None values.', code='invalid_request')
        return self.request('/api/v1/account/preferences/', method='PATCH', body=json.dumps(changes), token=token)

    # GIF exports need a registered account: any personal token, or a device session.
    def master_game_gif(self, game_token: str, token: str, *, orientation='white') -> ApplicationDownload:
        game = public_slug_segment(game_token, 'game token')
        return self._gif('/api/v1/games/' + game + '/gif/', token, orientation)

    def public_game_gif(self, game_slug: str, token: str, *, orientation='white') -> ApplicationDownload:
        game = public_slug_segment(game_slug, 'game slug')
        return self._gif('/api/v1/public/games/' + game + '/gif/', token, orientation)

    def annotated_game_gif(self, book_slug: str, game_slug: str, token: str, *, orientation='white') -> ApplicationDownload:
        book = public_slug_segment(book_slug, 'book slug')
        game = public_slug_segment(game_slug, 'game slug')
        return self._gif('/api/v1/annotated/books/' + book + '/games/' + game + '/gif/', token, orientation)

    def public_imported_game_gif(self, username: str, game_slug: str, token: str, *,
                                 orientation='white') -> ApplicationDownload:
        game = public_slug_segment(game_slug, 'game slug')
        return self._gif('/api/v1/public/imported-games/' + public_username_segment(username) + '/' + game + '/gif/',
                         token, orientation)

    def account_imported_game_gif(self, game_slug: str, token: str, *, orientation='white') -> ApplicationDownload:
        """An owned import of either visibility; needs library:read."""
        game = public_slug_segment(game_slug, 'game slug')
        return self._gif('/api/v1/account/imported-games/' + game + '/gif/', token, orientation)

    def _gif(self, path, token, orientation):
        return self.download(path + _gif_query(orientation), token=token, accept='image/gif')

    def account_notifications(self, token: str, *, page=1, page_size=50) -> ApplicationResponse:
        return self.request('/api/v1/account/notifications/?' + _page_query(page, page_size), token=token)

    def account_mark_notification_read(self, notification_id: int, token: str) -> ApplicationResponse:
        item = str(_positive_id(notification_id, 'notification ID'))
        return self.request('/api/v1/account/notifications/' + item + '/read/', method='POST', token=token)

    def account_mark_all_notifications_read(self, token: str) -> ApplicationResponse:
        return self.request('/api/v1/account/notifications/read-all/', method='POST', token=token)

    def account_dismiss_all_notifications(self, token: str) -> ApplicationResponse:
        """Empty the inbox: dismiss every notification in it."""
        return self.request('/api/v1/account/notifications/dismiss-all/', method='POST', token=token)

    def account_dismiss_notification(self, notification_id: int, token: str) -> ApplicationResponse:
        item = str(_positive_id(notification_id, 'notification ID'))
        return self.request('/api/v1/account/notifications/' + item + '/', method='DELETE', token=token)

    def account_notification_preferences(self, token: str) -> ApplicationResponse:
        return self.request('/api/v1/account/notifications/preferences/', token=token)

    def account_update_notification_preferences(self, token: str, *, topics=None,
                                                sound_enabled=None) -> ApplicationResponse:
        """Change only the named topics and/or the sound setting."""
        payload = {}
        if topics is not None:
            if not isinstance(topics, dict) or not all(
                    isinstance(key, str) and isinstance(value, bool) for key, value in topics.items()):
                raise ApiError('topics must map topic keys to true or false.', code='invalid_request')
            payload['topics'] = topics
        if sound_enabled is not None:
            if not isinstance(sound_enabled, bool):
                raise ApiError('sound_enabled must be true or false.', code='invalid_request')
            payload['sound_enabled'] = sound_enabled
        if not payload:
            raise ApiError('Provide topics and/or sound_enabled to update.', code='invalid_request')
        return self.request('/api/v1/account/notifications/preferences/', method='PATCH',
                            body=json.dumps(payload), token=token)

    def account_notebooks(self, token: str) -> ApplicationResponse:
        return self.request('/api/v1/account/notebooks/', token=token)

    def account_notebook(self, notebook_uuid: str, token: str) -> ApplicationResponse:
        return self.request('/api/v1/account/notebooks/' + _notebook_uuid(notebook_uuid) + '/', token=token)

    def account_notebook_chapter_pgn(self, notebook_uuid: str, chapter_id: int, token: str) -> ApplicationDownload:
        chapter = str(_positive_id(chapter_id, 'chapter ID'))
        path = '/api/v1/account/notebooks/' + _notebook_uuid(notebook_uuid) + '/chapters/' + chapter + '/pgn/'
        return self.download(path, token=token, accept='application/x-chess-pgn')

    def account_notebook_file(self, notebook_uuid: str, token: str, *, password=None, ccnb_format=None) -> ApplicationDownload:
        """The whole Notebook as .ccnb bytes; a password (members only) encrypts it."""
        path = '/api/v1/account/notebooks/' + _notebook_uuid(notebook_uuid) + '/file/'
        accept = 'application/vnd.classicchess.notebook'
        if password is None:
            return self.download(path, token=token, accept=accept, ccnb_format=ccnb_format)
        if not isinstance(password, str) or not password:
            raise ApiError('Use a non-empty password.', code='invalid_request')
        return self.download(path, method='POST', body=json.dumps({'password': password}),
                             token=token, accept=accept, ccnb_format=ccnb_format)

    def account_notebook_chapter(self, notebook_uuid: str, chapter_id: int, token: str) -> ApplicationResponse:
        """A chapter with its PGN and content_version. Read both here before an edit you will save."""
        return self.request(_chapter_path(notebook_uuid, chapter_id), token=token)

    # Notebook editing: personal tokens need notebooks:write. The owner, managers
    # and editors may edit; viewers and commenters get 403 permission_denied.
    def account_create_notebook(self, title: str, token: str, *, description=None, visibility=None,
                                chapter_title=None, initial_position=None, game=None) -> ApplicationResponse:
        """Create a Notebook you own with one chapter (201). Each call creates another.

        The chapter starts from initial_position (a FEN or one PGN game), or from the
        game with that slug, or else the standard position. visibility defaults to private.
        """
        payload = _present(title=_text(title, 'title'), description=description,
                           visibility=_choice(visibility, NOTEBOOK_VISIBILITIES, 'visibility'),
                           chapter_title=chapter_title, initial_position=initial_position, game=game)
        return self.request('/api/v1/account/notebooks/', method='POST', body=json.dumps(payload), token=token)

    def account_rename_notebook(self, notebook_uuid: str, title: str, token: str) -> ApplicationResponse:
        """Owner or manager. Safe to retry."""
        return self.request(_notebook_path(notebook_uuid), method='PATCH',
                            body=json.dumps({'title': _text(title, 'title')}), token=token)

    def account_create_notebook_section(self, notebook_uuid: str, title: str, token: str, *,
                                        parent_id=None) -> ApplicationResponse:
        """Add a folder last among its siblings, inside parent_id or at the top level (201)."""
        payload = _present(title=_text(title, 'title'), parent_id=_optional_id(parent_id, 'parent folder ID'))
        return self.request(_notebook_path(notebook_uuid, 'sections/'), method='POST',
                            body=json.dumps(payload), token=token)

    def account_rename_notebook_section(self, notebook_uuid: str, section_id: int, title: str,
                                        token: str) -> ApplicationResponse:
        return self.request(_section_path(notebook_uuid, section_id), method='PATCH',
                            body=json.dumps({'title': _text(title, 'title')}), token=token)

    def account_delete_notebook_section(self, notebook_uuid: str, section_id: int, token: str) -> ApplicationResponse:
        """Delete a folder with every nested folder and chapter in it. A retry after success is 404."""
        return self.request(_section_path(notebook_uuid, section_id), method='DELETE', token=token)

    def account_move_notebook_section(self, notebook_uuid: str, section_id: int, token: str, *,
                                      parent_id=None, before_section_id=None) -> ApplicationResponse:
        """Move a folder under parent_id (else the top level) before before_section_id (else last).

        The reply is the whole Notebook tree, because siblings are renumbered.
        """
        payload = _present(parent_id=_optional_id(parent_id, 'parent folder ID'),
                           before_section_id=_optional_id(before_section_id, 'folder ID'))
        return self.request(_section_path(notebook_uuid, section_id, 'move/'), method='POST',
                            body=json.dumps(payload), token=token)

    def account_create_notebook_chapter(self, notebook_uuid: str, token: str, *, title=None, section_id=None,
                                        initial_position=None, game=None) -> ApplicationResponse:
        """Add a chapter last, in section_id or at the top level (201). Each call adds another.

        It starts from initial_position (a FEN or one PGN game), or from the game with
        that slug, or else the standard position.
        """
        payload = _present(title=title, section_id=_optional_id(section_id, 'folder ID'),
                           initial_position=initial_position, game=game)
        return self.request(_notebook_path(notebook_uuid, 'chapters/'), method='POST',
                            body=json.dumps(payload), token=token)

    def account_save_notebook_chapter(self, notebook_uuid: str, chapter_id: int, pgn: str, content_version: int,
                                      token: str, *, title=None) -> ApplicationResponse:
        """Replace a chapter's PGN if it is still at content_version; the reply has the new version.

        If the chapter changed after you read it, the reply is status 409 with error code
        chapter_conflict and nothing is saved. That reply carries no chapter content: read
        account_notebook_chapter for the current PGN and content_version, reapply the edit and
        save again. Retrying a save that succeeded also returns 409, so never retry blindly.
        """
        if not isinstance(pgn, str) or type(content_version) is not int or content_version < 1:
            raise ApiError('Use PGN text and the positive content_version you read.', code='invalid_request')
        payload = _present(pgn=pgn, content_version=content_version, title=title)
        return self.request(_chapter_path(notebook_uuid, chapter_id), method='PUT',
                            body=json.dumps(payload), token=token)

    def account_rename_notebook_chapter(self, notebook_uuid: str, chapter_id: int, title: str,
                                        token: str) -> ApplicationResponse:
        """A rename raises content_version, so a save based on the earlier version conflicts."""
        return self.request(_chapter_path(notebook_uuid, chapter_id), method='PATCH',
                            body=json.dumps({'title': _text(title, 'title')}), token=token)

    def account_delete_notebook_chapter(self, notebook_uuid: str, chapter_id: int, token: str) -> ApplicationResponse:
        """A retry after success is 404."""
        return self.request(_chapter_path(notebook_uuid, chapter_id), method='DELETE', token=token)

    def account_move_notebook_chapter(self, notebook_uuid: str, chapter_id: int, token: str, *,
                                      section_id=None, before_chapter_id=None) -> ApplicationResponse:
        """Move a chapter into section_id (else the top level) before before_chapter_id (else last).

        The reply is the whole Notebook tree, because chapters are renumbered.
        """
        payload = _present(section_id=_optional_id(section_id, 'folder ID'),
                           before_chapter_id=_optional_id(before_chapter_id, 'chapter ID'))
        return self.request(_chapter_path(notebook_uuid, chapter_id, 'move/'), method='POST',
                            body=json.dumps(payload), token=token)

    # Notebook management: personal tokens need notebooks:manage. Only the owner or a
    # manager may manage, and only the owner may delete or grant and change managers.
    def account_delete_notebook(self, notebook_uuid: str, token: str) -> ApplicationResponse:
        """Owner only: deletes every folder, chapter, collaborator and share link. A retry is 404."""
        return self.request(_notebook_path(notebook_uuid), method='DELETE', token=token)

    def account_notebook_settings(self, notebook_uuid: str, token: str) -> ApplicationResponse:
        """Settings, collaborators and the accepted choices, as the website settings page shows."""
        return self.request(_notebook_path(notebook_uuid, 'settings/'), token=token)

    def account_update_notebook_settings(self, notebook_uuid: str, token: str, *, title=None, description=None,
                                         visibility=None, allow_public_exports=None) -> ApplicationResponse:
        """Change only the given settings; the rest keep their current values. Safe to retry."""
        payload = _changes('setting', title=title, description=description,
                           visibility=_choice(visibility, NOTEBOOK_VISIBILITIES, 'visibility'),
                           allow_public_exports=allow_public_exports)
        return self.request(_notebook_path(notebook_uuid, 'settings/'), method='PATCH',
                            body=json.dumps(payload), token=token)

    def account_set_notebook_collaborator(self, notebook_uuid: str, username: str, role: str, token: str, *,
                                          can_present_live=None) -> ApplicationResponse:
        """Invite a public account (201) or change an existing collaborator's role (200). Safe to retry.

        role is viewer, commenter, editor or manager; can_present_live is kept only for
        editors and managers.
        """
        if role not in NOTEBOOK_ROLES:
            raise ApiError('role must be viewer, commenter, editor or manager.', code='invalid_request')
        payload = _present(username=_text(username, 'username'), role=role, can_present_live=can_present_live)
        return self.request(_notebook_path(notebook_uuid, 'collaborators/'), method='POST',
                            body=json.dumps(payload), token=token)

    def account_remove_notebook_collaborator(self, notebook_uuid: str, collaborator_id: int,
                                             token: str) -> ApplicationResponse:
        """Remove a collaborator by the ID in the settings; one already gone is not an error."""
        path = _notebook_path(notebook_uuid, f'collaborators/{_positive_id(collaborator_id, "collaborator ID")}/')
        return self.request(path, method='DELETE', token=token)

    def account_leave_notebook(self, notebook_uuid: str, token: str) -> ApplicationResponse:
        """Remove your own invitation. The owner cannot leave; a retry is 404."""
        return self.request(_notebook_path(notebook_uuid, 'membership/'), method='DELETE', token=token)

    def account_notebook_share(self, notebook_uuid: str, token: str) -> ApplicationResponse:
        """The active read-only share link, or active false. Keep its url private."""
        return self.request(_notebook_path(notebook_uuid, 'share/'), token=token)

    def account_create_notebook_share(self, notebook_uuid: str, token: str) -> ApplicationResponse:
        """Create a share link; an active one is returned unchanged. Safe to retry."""
        return self.request(_notebook_path(notebook_uuid, 'share/'), method='PUT', token=token)

    def account_revoke_notebook_share(self, notebook_uuid: str, token: str) -> ApplicationResponse:
        """Revoke the Notebook link; chapter links stay active. Safe to retry."""
        return self.request(_notebook_path(notebook_uuid, 'share/'), method='DELETE', token=token)

    def account_notebook_chapter_share(self, notebook_uuid: str, chapter_id: int, token: str) -> ApplicationResponse:
        """The chapter's active share link, which opens only that chapter, or active false."""
        return self.request(_chapter_path(notebook_uuid, chapter_id, 'share/'), token=token)

    def account_create_notebook_chapter_share(self, notebook_uuid: str, chapter_id: int,
                                              token: str) -> ApplicationResponse:
        return self.request(_chapter_path(notebook_uuid, chapter_id, 'share/'), method='PUT', token=token)

    def account_revoke_notebook_chapter_share(self, notebook_uuid: str, chapter_id: int,
                                              token: str) -> ApplicationResponse:
        """Revoke the chapter link; the Notebook link stays active. Safe to retry."""
        return self.request(_chapter_path(notebook_uuid, chapter_id, 'share/'), method='DELETE', token=token)

    # The Gym: play the site's bots. Personal tokens need gym:read to read and
    # gym:play for the rest; device sessions pass both.
    def account_gym(self, token: str, *, page_size=20) -> ApplicationResponse:
        """The bots, the time controls a new game may use and the newest page of your games."""
        _page_query(1, page_size)
        return self.request(f'/api/v1/account/gym/?page_size={page_size}', token=token)

    def account_gym_games(self, token: str, *, player='me', page=1, page_size=20) -> ApplicationResponse:
        """Your games, newest first; player="others" is for the Gym's review accounts only."""
        if player not in GYM_PLAYERS:
            raise ApiError('player must be me or others.', code='invalid_request')
        query = _page_query(page, page_size) + ('&player=others' if player == 'others' else '')
        return self.request('/api/v1/account/gym/games/?' + query, token=token)

    def account_gym_new_game(self, bot_key: str, token: str, *, color='white', time='unlimited',
                             minutes=None, increment=None, version=None) -> ApplicationResponse:
        """Start a game (201). time is "unlimited", a preset such as "3+0", or "custom" with minutes and increment.

        version picks one of the bot's versions from account_gym, such as "v4"; omitted,
        the game plays the bot's default_version. The game keeps it to the end.
        """
        body = _gym_new_game_body(color, time, minutes, increment, version)
        path = '/api/v1/account/gym/bots/' + public_slug_segment(bot_key, 'bot key') + '/games/'
        return self.request(path, method='POST', body=json.dumps(body), token=token)

    def account_gym_game(self, game_id: int, token: str) -> ApplicationResponse:
        return self.request(_gym_game(game_id), token=token)

    def account_gym_game_pgn(self, game_id: int, token: str) -> ApplicationDownload:
        return self.download(_gym_game(game_id) + 'pgn/', token=token, accept='application/x-chess-pgn')

    def account_gym_move(self, game_id: int, move: str, ply: int, token: str) -> ApplicationResponse:
        """Play a UCI move such as "e2e4" at the ply you have seen; a stale ply is 409 and plays nothing."""
        if not isinstance(move, str) or not UCI_MOVE.fullmatch(move):
            raise ApiError('Use a UCI move such as e2e4 or e7e8q.', code='invalid_request')
        return self._gym_post(game_id, 'move/', token, {'move': move, 'ply': _ply(ply)})

    def account_gym_bot_move(self, game_id: int, ply: int, token: str) -> ApplicationResponse:
        """Ask for the bot's move. A 429 capacity_exhausted means the engine is busy: wait
        ``retry_after`` seconds and ask again with the same ply. A 429 engine_budget means this
        account has used its 30 minutes of engine time for the current hour; ask again after
        ``retry_after`` seconds."""
        return self._gym_post(game_id, 'bot-move/', token, {'ply': _ply(ply)})

    def account_gym_resign(self, game_id: int, token: str) -> ApplicationResponse:
        return self._gym_post(game_id, 'resign/', token)

    def account_gym_take_back(self, game_id: int, token: str) -> ApplicationResponse:
        return self._gym_post(game_id, 'takeback/', token)

    def account_gym_clock(self, game_id: int, token: str) -> ApplicationResponse:
        """Settle the clock when one you show reaches zero; the server decides."""
        return self._gym_post(game_id, 'clock/', token)

    def account_gym_abort(self, game_id: int, token: str) -> ApplicationResponse:
        """Abort (and delete) a game before your first move."""
        return self._gym_post(game_id, 'abort/', token)

    def account_gym_delete_game(self, game_id: int, token: str) -> ApplicationResponse:
        return self.request(_gym_game(game_id), method='DELETE', token=token)

    def _gym_post(self, game_id, action, token, body=None):
        return self.request(_gym_game(game_id) + action, method='POST',
                            body=json.dumps(body) if body is not None else None, token=token)

    # Puzzles, for signed-in accounts only. Personal tokens need puzzles:read to
    # read and puzzles:write to save results and reset; device sessions pass both.
    def account_puzzles(self, token: str) -> ApplicationResponse:
        """Your figures: first-try solves, misses, what is left in your rotation, and the total."""
        return self.request('/api/v1/account/puzzles/', token=token)

    def account_puzzle_batch(self, token: str, *, limit=25, result=None) -> ApplicationResponse:
        """Download up to 100 playable puzzles with account/reset identity for offline attempts."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ApiError('Use a batch size from 1 to 100.', code='invalid_request')
        return self.request('/api/v1/account/puzzles/batch/?limit=' + str(limit) + _puzzle_filter(result), token=token)

    def account_puzzle_next(self, token: str, *, after=None, result=None) -> ApplicationResponse:
        """A random puzzle from your rotation, another than ``after`` (the one just played) when
        there is one. A 404 rotation_empty means every puzzle is solved; reset to play them again."""
        query = '' if after is None else '&after=' + _puzzle_id(after)
        query += _puzzle_filter(result)
        query = '?' + query[1:] if query else ''
        return self.request('/api/v1/account/puzzles/next/' + query, token=token)

    def account_puzzle(self, puzzle_id: str, token: str) -> ApplicationResponse:
        return self.request('/api/v1/account/puzzles/' + _puzzle_id(puzzle_id) + '/', token=token)

    def account_puzzle_result(self, puzzle_id: str, result: str, token: str, *,
                              event_id=None, generation=None, revision=None) -> ApplicationResponse:
        """Save one play: "missed" at the first wrong move or when the solution is shown, "solved"
        for a clean finish. A first-try solve stands until reset, so the saved result can differ."""
        if result not in PUZZLE_RESULTS:
            raise ApiError('result must be solved or missed.', code='invalid_request')
        path = '/api/v1/account/puzzles/' + _puzzle_id(puzzle_id) + '/result/'
        body = {'result': result}
        if any(value is not None for value in (event_id, generation, revision)):
            if (not isinstance(event_id, str) or not re.fullmatch(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}', event_id)
                    or type(generation) is not int or not 0 <= generation <= 2**53 - 1
                    or not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision)):
                raise ApiError('An offline attempt needs a UUID event_id, generation and puzzle revision.', code='invalid_request')
            body.update(event_id=event_id, generation=generation, revision=revision)
        return self.request(path, method='POST', body=json.dumps(body), token=token)

    def account_puzzle_history(self, token: str, *, page=1, page_size=20, result=None) -> ApplicationResponse:
        """The puzzles you have played and how you did, most recent first."""
        return self.request('/api/v1/account/puzzles/history/?' + _page_query(page, page_size) + _puzzle_filter(result), token=token)

    def account_notebook_annotation_search(self, token: str, *, q='', kind='all',
                                           notebook_uuid=None, limit=50, cursor=None) -> ApplicationResponse:
        """Search annotations in owned/directly invited Notebooks; follow cursor until null."""
        if (not isinstance(q, str) or len(q) > 128 or kind not in ('all', 'comments', 'glyphs', 'diagrams')
                or type(limit) is not int or not 1 <= limit <= 100
                or cursor is not None and (not isinstance(cursor, str) or len(cursor) > 2048)):
            raise ApiError('Use a bounded annotation query, kind and page size.', code='invalid_query')
        params = {'q': q, 'kind': kind, 'limit': limit}
        if notebook_uuid is not None:
            params['notebook_uuid'] = _notebook_uuid(notebook_uuid)
        if cursor is not None:
            params['cursor'] = cursor
        return self.request('/api/v1/account/notebooks/annotation-search/?' + urlencode(params), token=token)

    def account_puzzle_reset(self, token: str) -> ApplicationResponse:
        """Every puzzle goes back into your rotation. Your history stays."""
        return self.request('/api/v1/account/puzzles/reset/', method='POST', token=token)

    # The mobile app's device operations. Signing in returns a device session; the
    # session, device and settings operations accept only that session, not a personal token.
    def mobile_home(self) -> ApplicationResponse:
        """The app's front door: archive counts, Game of the Day and action links. No credential."""
        return self.request('/api/v1/mobile/home/')

    def mobile_android_release(self) -> ApplicationResponse:
        """The current Android build: version, APK URL, SHA-256 checksum and size. No credential."""
        return self.request('/api/v1/mobile/android/release/')

    def mobile_sign_in(self, username: str, password: str, *, two_factor_code=None,
                       device_name=None) -> ApplicationResponse:
        """Sign in a device (201) and receive its session bearer. Sent only over HTTPS or loopback.

        A 401 two_factor_required asks for the authenticator or recovery code. Keep the
        returned session out of source files and logs, as with any credential.
        """
        origin = urlsplit(self.base_url)
        if origin.scheme != 'https' and origin.hostname not in LOOPBACK_HOSTS:
            raise ApiError('Passwords require an API over HTTPS or loopback development.', code='unsafe_credentials')
        if not isinstance(password, str) or not password:
            raise ApiError('Use a non-empty password.', code='invalid_request')
        payload = _present(username=_text(username, 'username'), password=password,
                           two_factor_code=two_factor_code, device_name=device_name)
        return self.request('/api/v1/account/mobile/login/', method='POST', body=json.dumps(payload))

    def mobile_sign_out(self, token: str) -> ApplicationResponse:
        """Revoke this device session."""
        return self.request('/api/v1/account/mobile/logout/', method='POST', token=token)

    def mobile_sessions(self, token: str) -> ApplicationResponse:
        """Your signed-in devices with names, times, expiry and which one is this session."""
        return self.request('/api/v1/account/mobile/sessions/', token=token)

    def mobile_revoke_session(self, session_id: int, token: str) -> ApplicationResponse:
        session = str(_positive_id(session_id, 'session ID'))
        return self.request('/api/v1/account/mobile/sessions/' + session + '/revoke/', method='POST', token=token)

    def mobile_revoke_all_sessions(self, token: str) -> ApplicationResponse:
        """Sign out every device of the account, this one included."""
        return self.request('/api/v1/account/mobile/sessions/revoke-all/', method='POST', token=token)

    def mobile_settings(self, token: str) -> ApplicationResponse:
        """The app's board, engine, sound, speech, Cast and debug settings."""
        return self.request('/api/v1/account/mobile/settings/', token=token)

    def mobile_update_settings(self, token: str, **changes) -> ApplicationResponse:
        """Change only the named settings, such as piece_set="merida" or engine_lines=2.

        Sound and speech here are the Android app's own, separate from the website's.
        """
        if not changes or any(value is None for value in changes.values()):
            raise ApiError('Name at least one setting to change, without None values.', code='invalid_request')
        return self.request('/api/v1/account/mobile/settings/', method='POST', body=json.dumps(changes), token=token)

    def scan_position(self, image: bytes, token: str) -> ApplicationResponse:
        if not isinstance(image, bytes) or not 1 <= len(image) <= 850_000:
            raise ApiError('Use a JPEG of at most 850000 bytes.', code='invalid_upload')
        boundary = 'classicchess-' + uuid4().hex
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="diagram.jpg"\r\n'
                  'Content-Type: image/jpeg\r\n\r\n').encode()
        body = prefix + image + f'\r\n--{boundary}--\r\n'.encode()
        return self.request('/api/v1/mobile/position-scan/', method='POST', token=token, body=body,
                            content_type=f'multipart/form-data; boundary={boundary}', timeout=85)
