"""Bounded response decoding and complete, origin-confined page collection.

The standalone download embeds this module's implementation so it needs no
installed package. Both distributions run the same behavioral fixtures.
"""

import json
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urljoin
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

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
