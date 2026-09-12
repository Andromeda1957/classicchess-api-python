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
from .response import DEFAULT_MAX_RESPONSE_BYTES
from .response import ApiError
from .response import decode_json_object
from .response import read_bounded

PRIVATE_PATH = re.compile(r'^/(?:api/v1/(?:account/|desktop/|mobile/(?:notebooks/|position-scan/))|cast/api/mobile/)')


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
        headers = {'Accept': 'application/json', 'User-Agent': self.user_agent}
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
                data = read_bounded(response, self.max_response_bytes)
                return ApplicationResponse(response.status, decode_json_object(data or b'{}'), response.headers.get('Retry-After'))
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

    def scan_position(self, image: bytes, token: str) -> ApplicationResponse:
        if not isinstance(image, bytes) or not 1 <= len(image) <= 850_000:
            raise ApiError('Use a JPEG of at most 850000 bytes.', code='invalid_upload')
        boundary = 'classicchess-' + uuid4().hex
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="diagram.jpg"\r\n'
                  'Content-Type: image/jpeg\r\n\r\n').encode()
        body = prefix + image + f'\r\n--{boundary}--\r\n'.encode()
        return self.request('/api/v1/mobile/position-scan/', method='POST', token=token, body=body,
                            content_type=f'multipart/form-data; boundary={boundary}', timeout=85)
