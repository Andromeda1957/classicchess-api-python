"""Bounded application requests with explicit per-request credentials."""

import json
import math
import re
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler
from urllib.request import Request
from urllib.request import build_opener
from uuid import uuid4

from .client import DEFAULT_BASE_URL
from .client import USER_AGENT
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


UCI_MOVE = re.compile(r'[a-h][1-8][a-h][1-8][qrbn]?')
GYM_COLORS = ('white', 'black', 'random')
GYM_PLAYERS = ('me', 'others')


def _gym_game(game_id):
    return '/api/v1/account/gym/games/' + str(_positive_id(game_id, 'game ID')) + '/'


def _ply(value):
    if type(value) is not int or value < 0:
        raise ApiError('Use the ply from the game state: a whole number from 0.', code='invalid_request')
    return value


def _gym_new_game_body(color, time, minutes, increment):
    if color not in GYM_COLORS:
        raise ApiError('color must be white, black or random.', code='invalid_request')
    if not isinstance(time, str) or not time:
        raise ApiError('time must be unlimited, a preset such as 3+0, or custom.', code='invalid_request')
    body = {'color': color, 'time': time}
    if time == 'custom':
        if type(minutes) is not int or minutes < 1 or type(increment) is not int or increment < 0:
            raise ApiError('A custom time needs whole minutes from 1 and increment seconds from 0.',
                           code='invalid_request')
        body.update(minutes=minutes, increment=increment)
    elif minutes is not None or increment is not None:
        raise ApiError('minutes and increment apply only to time="custom".', code='invalid_request')
    return body


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
                 token=None, timeout=None, accept='*/*') -> ApplicationDownload:
        """Fetch a file. The bytes are returned only for a 2xx; otherwise ``error`` holds the JSON reply."""
        status, data, headers = self._exchange(path, method=method, body=body, content_type=content_type,
                                               token=token, timeout=timeout, accept=accept)
        ok = 200 <= status < 300
        error = {} if ok else decode_json_object(data or b'{}')
        return ApplicationDownload(status, data if ok else b'', headers.get('Content-Type', ''),
                                   _filename(headers.get('Content-Disposition')) if ok else None,
                                   headers.get('Retry-After'), error)

    def _exchange(self, path, *, method, body, content_type, token, timeout, accept):
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
        if token is not None:
            headers['Authorization'] = f'Bearer {token}'
        if body is not None:
            if not isinstance(content_type, str) or len(content_type) > 200 or '\r' in content_type or '\n' in content_type:
                raise ApiError('Invalid content type.', code='invalid_request')
            headers['Content-Type'] = content_type
        request = Request(urljoin(self.base_url, path), method=method, headers=headers, data=body)
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

    def account_notebook_file(self, notebook_uuid: str, token: str, *, password=None) -> ApplicationDownload:
        """The whole Notebook as .ccnb bytes; a password (members only) encrypts it."""
        path = '/api/v1/account/notebooks/' + _notebook_uuid(notebook_uuid) + '/file/'
        accept = 'application/vnd.classicchess.notebook'
        if password is None:
            return self.download(path, token=token, accept=accept)
        if not isinstance(password, str) or not password:
            raise ApiError('Use a non-empty password.', code='invalid_request')
        return self.download(path, method='POST', body=json.dumps({'password': password}),
                             token=token, accept=accept)

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
                             minutes=None, increment=None) -> ApplicationResponse:
        """Start a game (201). time is "unlimited", a preset such as "3+0", or "custom" with minutes and increment."""
        body = _gym_new_game_body(color, time, minutes, increment)
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

    def scan_position(self, image: bytes, token: str) -> ApplicationResponse:
        if not isinstance(image, bytes) or not 1 <= len(image) <= 850_000:
            raise ApiError('Use a JPEG of at most 850000 bytes.', code='invalid_upload')
        boundary = 'classicchess-' + uuid4().hex
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="diagram.jpg"\r\n'
                  'Content-Type: image/jpeg\r\n\r\n').encode()
        body = prefix + image + f'\r\n--{boundary}--\r\n'.encode()
        return self.request('/api/v1/mobile/position-scan/', method='POST', token=token, body=body,
                            content_type=f'multipart/form-data; boundary={boundary}', timeout=85)
