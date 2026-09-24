# Classic Chess Python SDK

Use Python to read chess games, player biographies, notable games, tournament
archives and annotated books from [Classic Chess](https://classicchess.com/).
The SDK also searches the larger MasterDB game database, retrieves opening
statistics, exports games, and calls account and application APIs.

Public reads need no account or API key. The SDK uses the Python standard
library and is MIT licensed; see [LICENSE](LICENSE).

## Install and run your first example

Install [Python 3.10 or newer](https://www.python.org/downloads/) and
[Git](https://git-scm.com/). In a terminal, create an isolated Python environment
and install directly from this public repository:

```sh
python3 -m venv classicchess-env
source classicchess-env/bin/activate
python3 -m pip install git+https://github.com/Andromeda1957/classicchess-api-python.git
classicchess-api public players --names
```

On Windows PowerShell, use `py` instead of `python3` and activate with
`classicchess-env\Scripts\Activate.ps1` instead of the `source` command.
The last command prints the names of all curated players. This package is not
published to PyPI yet; the GitHub URL above is the install source.

To use it in a Python program, create `example.py` with this complete example:

```python
from classicchess_api import ClassicChessClient

api = ClassicChessClient()
print(api.player_names()[:10])
```

Run `python3 example.py` in the same activated environment. You should see up to
ten player names. The import is `classicchess_api`; the command-line executable
is `classicchess-api`. `python3 -m classicchess_api` runs the same command-line tool.

## Read a player archive

Player slugs are identifiers returned by the API. Use them as returned rather
than guessing a slug from a display name. This complete example discovers a
player and prints the first ten games in their archive:

```python
from classicchess_api import ClassicChessClient

api = ClassicChessClient()
players = api.public_players()["results"]
if players:
    player = players[0]
    print(player["name"])
    biography = api.public_player_bio(player["slug"])["bio"]
    print(biography["lede"] if biography else "Biography unavailable")
    for game in api.iterate_public_games(archive_player=player["slug"], limit=10):
        print(game["token"], game["white"], game["black"])
```

Catalogs return complete lists. Game searches return one page by default.
The `iterate_*` methods fetch pages as you consume them; breaking the loop or
setting `limit` stops without fetching an extra page. The `all_pages=True`
option on game-list methods collects pages into one dictionary instead.
Missing biographies are `None`, and notable lists can be empty.

## Available operations

| Capability | Python methods |
| --- | --- |
| API discovery | `discovery()` |
| Player, event and book catalogs | `public_players()`, `public_events()`, `annotated_books()` |
| Names and titles only | `player_names()`, `event_names()`, `book_titles()` |
| Player profiles, biographies and notable games | `public_player(slug)`, `public_player_bio(slug)`, `public_notable_games(slug)` |
| Event series | `event_series(query=...)` or `event_series(series=...)` |
| Public game lists, detail and PGN | `public_games()`, `public_game(token)`, `public_pgn(token)` |
| MasterDB game lists, detail and PGN | `master_games(query)`, `master_game(token)`, `master_pgn(token)` |
| Annotated game lists, detail and PGN | `annotated_games(book_slug)`, `annotated_game(book_slug, game_slug)`, `annotated_pgn(book_slug, game_slug=None)` |
| Streaming games | `iterate_public_games()`, `iterate_master_games(query)`, `iterate_annotated_games(book_slug)` |
| Combine PGN text for returned games | `pgn_text_for_games(games)` |
| PGN or NDJSON export | `export_public_games()`, `export_master_games()`; select `format="pgn"` or `format="ndjson"` |
| Player search and statistics | `players(query)`, `master_stats(query=...)` or `master_stats(player=..., opponent=...)` |
| Opening explorer | `explorer(play=...)` or `explorer(fen=...)`, `explorer_sources()` |
| Event detail and About page | `public_event(event_slug)`, `public_event_about(event_slug)` |
| Photo gallery | `gallery(query=None, page=1, page_size=48)`, `gallery_photo(photo_id)` |
| Beginner games and Game of the Day | `beginner_games()`, `daily_game()` |
| Site search | `site_search(query)` for a preview of players, events and games; `site_search(query, kind="games", page=2)` for one paginated kind |
| Endgame tablebase | `tablebase(fen)` uses local Syzygy tables and falls back to the Lichess tablebase for larger positions, as the site does |
| Account collections | `ApplicationClient.account_me(token)`, `account_collections(token)`, `account_create_collection(name, token)`, `account_import_public_player_games(archive_player, token, ...)` |
| Add single games to collections | `ApplicationClient.account_add_collection_game(collection_id, game_slug, token)` |
| Starred players and games | `account_starred_players(token, page=1, page_size=50)`, `account_star_player(slug, token)`, `account_unstar_player(slug, token)`, `account_starred_games(token, ...)`, `account_star_game(slug, token)`, `account_unstar_game(slug, token)` |
| Change or delete imported games | `account_set_imported_game_visibility(slug, "public" or "private", token)`, `account_delete_imported_game(slug, token)` |
| Public imported games | `public_imported_game(username, game_slug)`, `public_imported_pgn(username, game_slug)` read a game another account imported and made public, by its page address |
| GIF exports | `ApplicationClient.master_game_gif(game_token, token)`, `public_game_gif(slug, token)`, `annotated_game_gif(book_slug, game_slug, token)`, `public_imported_game_gif(username, slug, token)`, `account_imported_game_gif(slug, token)`; add `orientation="black"` to flip the board |
| Notifications | `account_notifications(token, page=1, page_size=50)`, `account_mark_notification_read(id, token)`, `account_mark_all_notifications_read(token)`, `account_dismiss_notification(id, token)`, `account_notification_preferences(token)`, `account_update_notification_preferences(token, topics=..., sound_enabled=...)` |
| Notebook exports | `account_notebooks(token)`, `account_notebook(uuid, token)`, `account_notebook_chapter_pgn(uuid, chapter_id, token)`, `account_notebook_file(uuid, token, password=None)` |
| Notebook, Remote, Cast and other application APIs | `ApplicationClient.request(path, method=..., body=..., token=...)`, `download(path, token=...)` for files |
| Scanner upload | `ApplicationClient.scan_position(jpeg_bytes, token)` |

Public game filters include `query`, `archive_player`, `archive_event`, `since`,
`until`, `sort`, `page` and `page_size`. Each export is capped at 300 games.
For larger archives, iterate games and export batches of up to 300 returned
tokens. NDJSON means one JSON object per line; `pgn_in_json=False` omits the
PGN field from that format. PGN is the standard text format for chess games.

## Account and application requests

Public archive reads use `ClassicChessClient`. For private account or device
operations use `ApplicationClient` and pass a token explicitly for each request.
Create a personal API token in your signed-in Classic Chess profile settings
with the scopes required by the [account API reference](https://classicchess.com/api/#account-api).
Set the `CLASSICCHESS_API_TOKEN` environment variable to that token before
running this read-only account example:

```python
import os
from classicchess_api import ApplicationClient

api = ApplicationClient()
response = api.account_me(os.environ["CLASSICCHESS_API_TOKEN"])
print(response.status, response.data)
```

`response.ok` indicates HTTP success. HTTP JSON errors preserve their status,
data and `retry_after`; network failures and invalid responses raise `ApiError`.
Application requests accept JSON text or bytes, including binary notebook uploads.
Scanner uploads accept JPEG bytes up to 850,000 bytes. Account tokens and mobile
or Desktop device sessions have different scopes; use the credential required
by the endpoint. Do not embed tokens in source files.

GIF, chapter PGN and Notebook file methods return an `ApplicationDownload` with
`ok`, `status`, `content` (the file bytes when `ok`), `content_type`, `filename`,
`retry_after` and `error` (the JSON error reply otherwise). GIF exports need a
registered account, so any personal token or device session works, and they
share the site limit on GIF exports: honor `retry_after` after a 429. This saves
a GIF of a public archive game:

```python
import os
from classicchess_api import ApplicationClient

gif = ApplicationClient().public_game_gif("tal-vs-larsen-1965", os.environ["CLASSICCHESS_API_TOKEN"])
if gif.ok:
    with open(gif.filename or "game.gif", "wb") as handle:
        handle.write(gif.content)
else:
    print(gif.status, gif.error)
```

The CLI reads `CLASSICCHESS_API_TOKEN` for account commands. Existing installations
can continue using their previous token environment variable as a fallback.
The original account helpers on `ClassicChessClient` remain available and raise
`ApiError` for unsuccessful HTTP responses.

## Timeouts, pagination and failures

Configure `ClassicChessClient(timeout=30, max_response_bytes=32 * 1024 * 1024)`
or `ApplicationClient` with the same options. Public reads return dictionaries
or text; `ApiError` exposes `status_code`, `code` and `retry_after` when available.
Pagination rejects repeated pages and links outside the original API collection.
The application transport sends no cookies, follows no redirects, and accepts
bearers only on private API paths over HTTPS or loopback development.
There are no automatic retries. Respect `Retry-After` and avoid blindly retrying writes.

## Optional standalone command-line script

If you only want command-line commands without installing the Python package,
GitHub also provides [pull_api.py](pull_api.py). Save that file and run
`python3 pull_api.py public players --names`. It is the dependency-free CLI
variant; the importable SDK and application transport are installed by the
steps above. The Classic Chess website no longer serves a script download URI.

## Build and test this repository

These contributor commands start from a fresh clone:

```sh
git clone https://github.com/Andromeda1957/classicchess-api-python.git
cd classicchess-api-python
python3 -m venv .venv
source .venv/bin/activate
python3 -m unittest discover -s classicchess_api/tests -t .
python3 -m pip install .
```

The same API capabilities are available in the
[TypeScript SDK](https://github.com/Andromeda1957/classicchess-api-typescript) and
[Kotlin SDK](https://github.com/Andromeda1957/classicchess-api-kotlin).
See the [API reference](https://classicchess.com/api/) for response fields,
endpoint permissions, rate limits and data attribution. The MIT license covers
the SDK code; linked chess resources retain their own stated reuse terms.
Issues and pull requests are welcome; see [CONTRIBUTING.md](https://github.com/Andromeda1957/classicchess-api-python/blob/main/CONTRIBUTING.md).
