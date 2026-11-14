# Classic Chess Python SDK

Use Python to read chess games, player biographies, notable games, tournament
archives and annotated books from [Classic Chess](https://classicchess.com/).
The SDK also searches the larger MasterDB game database, retrieves opening
statistics, exports games, and calls account and application APIs.

Public reads need no account or API key. The SDK uses the Python standard
library and is MIT licensed; see
[LICENSE](https://github.com/Andromeda1957/classicchess-api-python/blob/main/LICENSE).

## Install and run your first example

Install [Python 3.10 or newer](https://www.python.org/downloads/). In a terminal,
create an isolated Python environment and install from
[PyPI](https://pypi.org/project/classicchess-sdk/):

```sh
python3 -m venv classicchess-env
source classicchess-env/bin/activate
python3 -m pip install classicchess-sdk
classicchess-api public players --names
```

On Windows PowerShell, use `py` instead of `python3` and activate with
`classicchess-env\Scripts\Activate.ps1` instead of the `source` command.
The last command prints the names of all curated players. To install from the
public source repository instead, install [Git](https://git-scm.com/) and use
`python3 -m pip install git+https://github.com/Andromeda1957/classicchess-api-python.git`.

To use it in a Python program, create `example.py` with this complete example:

```python
from classicchess_api import ClassicChessClient

api = ClassicChessClient()
print(api.player_names()[:10])
```

Run `python3 example.py` in the same activated environment. You should see up to
ten player names. The PyPI distribution is `classicchess-sdk`; the import is
`classicchess_api` and the command-line executable is `classicchess-api`. `python3 -m classicchess_api` runs the same command-line tool.

If you installed the previous `classicchess-api` distribution, run
`python3 -m pip uninstall classicchess-api` before installing `classicchess-sdk`
in that environment. Both distributions provide the same import and command,
so install only one. Existing Python programs and CLI commands still work.

The MasterDB and public game-list commands accept a nonnegative `--limit`. `--limit 0` returns no
results and makes no per-game PGN requests. This applies to the default single
page and to `--all-pages`; negative limits are rejected before a request is sent.
The `export` commands take the same search filters but no `--limit`, `--page`,
`--page-size`, `--all-pages` or `--pgn`: each export is one reply of at most 300
games, so those options are refused before a request is sent.

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
| Player cards for lists and pickers | `public_player_cards()` returns every player with only the biography headline (epithet, lifespan, crown), about a tenth the size of `public_players()`; `player_names()` reads it |
| Player profiles, biographies and notable games | `public_player(slug)`, `public_player_bio(slug)`, `public_notable_games(slug)` |
| Event series | `event_series(query=...)` or `event_series(series=...)` |
| Public game lists, detail and PGN | `public_games()`, `public_game(token)`, `public_pgn(token)` |
| MasterDB game lists, detail and PGN | `master_games(query)`, `master_game(token)`, `master_pgn(token)` |
| Annotated game lists, detail and PGN | `annotated_games(book_slug, q=None, author=None, header_q=None, sort=None)`, `annotated_game(book_slug, game_slug)`, `annotated_pgn(book_slug, game_slug=None)`; `q` searches the notes, `author` the source label, `header_q` the game headers, and `sort` is `"asc"` or `"desc"` by date |
| Streaming games | `iterate_public_games()`, `iterate_master_games(query)`, `iterate_annotated_games(book_slug)` |
| Combine PGN text for returned games | `pgn_text_for_games(games)` |
| PGN or NDJSON export | `export_public_games()`, `export_master_games()`; select `format="pgn"` or `format="ndjson"` |
| Export chosen games | `export_public_games_by_token(tokens, sort="asc")`, `export_master_games_by_token(tokens)` post up to 300 game tokens and return them in one PGN or NDJSON reply |
| Player search and statistics | `players(query)`, `master_stats(query=...)` or `master_stats(player=..., opponent=...)` |
| Opening explorer | `explorer(play=...)` or `explorer(fen=...)`, `explorer_sources()`; `since` and `until` limit the years |
| One player's openings | `player_explorer(player, fen=..., color="both", result="all", page=None, since=None, until=None)` lists one MasterDB player's games through a position; a reply with `pending` asks you to wait `retry_after_ms` and ask again |
| Lichess opening explorer | `lichess_explorer(fen=..., player=..., color=..., result=..., since=..., until=...)` relays through Classic Chess, so no Lichess token is needed; no player covers the whole Lichess database, a Lichess username covers that player's games. `lichess_explorer_game(game_id)` returns one listed Lichess game with its PGN |
| Open a game link | `resolve_game_link(url)` turns a Lichess or Chess.com game address into its PGN for analysis, without account credentials |
| Event detail and About page | `public_event(event_slug)`, `public_event_about(event_slug)` |
| Photo gallery | `gallery(query=None, page=1, page_size=48)`, `gallery_photo(photo_id)` |
| Beginner games and Game of the Day | `beginner_games()`, `daily_game()` |
| Event of the day | `event_of_the_day()` returns the landmark event on today's homepage, its tagline and pictured winner; `event` is `None` when none can be published |
| Today's homepage card | `todays_picks()` returns the Player of the day, Game of the Day, On this day game and Event of the day in one read, with portraits where available; each is `None` when absent |
| Site search | `site_search(query)` for a preview of players, events and games; `site_search(query, kind="games", page=2)` for one paginated kind; `kind="site"` searches the site's pages, player biographies, event histories and What's New posts |
| Chess Today | `chess_today()` for today's sourced chess-history anniversaries; `chess_today_on_date(month, day)` for another date |
| Study themes and What's New | `study_themes()` lists the themes games can be filtered by; `site_updates(page=1, page_size=20)` and `site_update(id)` read the What's New posts |
| Shared Gym games | `shared_gym_game(share_token)` reads a Gym game someone shared by link |
| Endgame tablebase | `tablebase(fen)` uses local Syzygy tables and falls back to the Lichess tablebase for larger positions, as the site does |
| Account collections | `ApplicationClient.account_me(token)`, `account_collections(token)`, `account_create_collection(name, token)`, `account_import_public_player_games(archive_player, token, ...)` |
| Add single games to collections | `ApplicationClient.account_add_collection_game(collection_id, game_slug, token)` |
| A collection's games | `account_collection_games(id, token, page=1, page_size=50, sort=None, q=None)`, `account_update_collection(id, token, name=..., sort=..., description=..., is_plan=...)`, `account_update_collection_item(id, item_id, token, note=..., studied=...)`, `account_remove_collection_item(id, item_id, token)`, `account_remove_collection_game(id, game_slug, token)` |
| Study plans | `account_create_plan(name, items, idempotency_key, token, description=None)`, `account_reorder_collection(id, item_ids, token)`, `account_plan_progress(id, token, page=1, page_size=50)` |
| Starred players and games | `account_starred_players(token, page=1, page_size=50)`, `account_star_player(slug, token)`, `account_unstar_player(slug, token)`, `account_starred_games(token, ...)`, `account_star_game(slug, token)`, `account_unstar_game(slug, token)` |
| A game's place in your library | `account_game_library(game_slug, token)` returns whether you starred it and which collections hold it; `account_toggle_player_bookmark(player_slug, token)` flips a player's bookmark |
| Import, list, change or delete imported games | `account_imported_games(token, q=None, source=None, sort=None, page=1, page_size=50)`, `account_import_game(source, token, pgn=... or remote_url=..., visibility=None)`, `account_imported_game(slug, token)`, `account_set_imported_game_visibility(slug, "public" or "private", token)`, `account_delete_imported_game(slug, token)` |
| Website preferences | `account_preferences(token)`, `account_update_preferences(token, board_theme="green", ...)` |
| Public imported games | `public_imported_game(username, game_slug)`, `public_imported_pgn(username, game_slug)` read a game another account imported and made public, by its page address |
| GIF exports | `ApplicationClient.master_game_gif(game_token, token)`, `public_game_gif(slug, token)`, `annotated_game_gif(book_slug, game_slug, token)`, `public_imported_game_gif(username, slug, token)`, `account_imported_game_gif(slug, token)`; add `orientation="black"` to flip the board |
| Notifications | `account_notifications(token, page=1, page_size=50)`, `account_mark_notification_read(id, token)`, `account_mark_all_notifications_read(token)`, `account_dismiss_notification(id, token)`, `account_dismiss_all_notifications(token)`, `account_notification_preferences(token)`, `account_update_notification_preferences(token, topics=..., sound_enabled=...)` |
| Notebook exports | `account_notebooks(token)`, `account_notebook(uuid, token)`, `account_notebook_chapter(uuid, chapter_id, token)`, `account_notebook_chapter_pgn(uuid, chapter_id, token)`, `account_notebook_file(uuid, token, password=None, ccnb_format=None)` |
| Notebook editing | `account_create_notebook(title, token, ...)`, `account_rename_notebook(uuid, title, token)`, `account_create_notebook_section(uuid, title, token, parent_id=None)`, `account_rename_notebook_section(uuid, section_id, title, token)`, `account_delete_notebook_section(uuid, section_id, token)`, `account_move_notebook_section(uuid, section_id, token, parent_id=None, before_section_id=None)`, `account_create_notebook_chapter(uuid, token, ...)`, `account_save_notebook_chapter(uuid, chapter_id, pgn, content_version, token, title=None)`, `account_rename_notebook_chapter(uuid, chapter_id, title, token)`, `account_delete_notebook_chapter(uuid, chapter_id, token)`, `account_move_notebook_chapter(uuid, chapter_id, token, section_id=None, before_chapter_id=None)` |
| Notebook management | `account_delete_notebook(uuid, token)`, `account_notebook_settings(uuid, token)`, `account_update_notebook_settings(uuid, token, ...)`, `account_set_notebook_collaborator(uuid, username, role, token)`, `account_remove_notebook_collaborator(uuid, collaborator_id, token)`, `account_leave_notebook(uuid, token)`, `account_notebook_share(uuid, token)`, `account_create_notebook_share(uuid, token)`, `account_revoke_notebook_share(uuid, token)`, `account_notebook_chapter_share(uuid, chapter_id, token)`, `account_create_notebook_chapter_share(uuid, chapter_id, token)`, `account_revoke_notebook_chapter_share(uuid, chapter_id, token)` |
| Annotated Notebook packages | `ClassicChessClient.annotated_books(ccnb_format=3)`, `annotated_notebook_file(book_slug, ccnb_format=3)`, `annotated_book_ccnb(book_slug, ccnb_format=3)` for a whole book with its notes and source pages |
| Play the Gym's bots | `account_gym(token)`, `account_gym_games(token, page=1, page_size=20)`, `account_gym_new_game(bot_key, token, color="white", time="unlimited", version=None)`, `account_gym_game(id, token)`, `account_gym_move(id, uci, ply, token)`, `account_gym_bot_move(id, ply, token)`, `account_gym_take_back(id, token)`, `account_gym_resign(id, token)`, `account_gym_clock(id, token)`, `account_gym_abort(id, token)`, `account_gym_delete_game(id, token)`, `account_gym_game_pgn(id, token)` |
| Puzzles | `account_puzzles(token)`, `account_puzzle_next(token, after=None)`, `account_puzzle(id, token)`, `account_puzzle_result(id, "solved" or "missed", token)`, `account_puzzle_history(token, page=1, page_size=20)`, `account_puzzle_reset(token)` |
| Mobile app devices | `mobile_home()`, `mobile_android_release()`, `mobile_sign_in(username, password, two_factor_code=None, device_name=None)`, `mobile_sign_out(token)`, `mobile_sessions(token)`, `mobile_revoke_session(session_id, token)`, `mobile_revoke_all_sessions(token)`, `mobile_settings(token)`, `mobile_update_settings(token, piece_set="merida", ...)` |
| Notebook, Remote, Cast and other application APIs | `ApplicationClient.request(path, method=..., body=..., token=...)`, `download(path, token=...)` for files |
| Scanner upload | `ApplicationClient.scan_position(jpeg_bytes, token)` |

Notebook downloads accept `ccnb_format=2` for legacy exchange or `ccnb_format=3`
for the binary record container. Omission retains legacy behavior. Use the same
format for the annotated catalog and package download so their revisions match.
The public package helper returns exact `bytes` from the fixed same-origin
endpoint for a validated catalog slug, sends no account credentials, and rejects
redirects. Unsupported values fail before a request. Capabilities unavailable in
legacy exchange require format 3.

Public game filters include `query`, `archive_player`, `archive_event`,
`search_mode`, `since`, `until`, `sort`, `page` and `page_size`. `search_mode`
chooses which fields `query` searches: `players` (players, event, site and
date), `openings` (opening name and ECO) or `all` (the default). The same
methods, their iterators and exports also take `theme` (taxonomy slugs from
`study_themes()`, as a list or comma-separated text; every tag must match), `eco`
(a code or prefix such as `D` or `D30`), `result` (`1-0`, `0-1`, `1/2-1/2` or
`*`) and `annotation_q`, `annotation_author` and `annotation_book`, which search
the published notes of annotated games. Each export is capped at 300 games.
For larger archives, iterate games and export batches of up to 300 returned
tokens with `export_public_games_by_token(tokens)`, which posts them one per
line so long token lists fit. NDJSON means one JSON object per line; `pgn_in_json=False` omits the
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

### Play the Gym's bots

Any account can play the Gym's bots; the moves are computed on the server.
Personal tokens need `gym:read` to read games and `gym:play` to start and play
them. Send the `ply` from the latest game state with each move: a stale ply
returns 409 and plays nothing, so a repeated request is safe. The bot engine
plays one move at a time. While it is busy, `account_gym_bot_move` returns 429
with `capacity_exhausted`: wait `retry_after` seconds and ask again with the
same ply. Each account may use 30 minutes of engine time in each hour; once it
is spent the answer is 429 `engine_budget` with `retry_after` set to the seconds
until that hour ends. An account starts at most 30 games an hour.
Each bot in `account_gym(token).data["bots"]` lists its `versions`, newest
first, and the `default_version` a new game plays. To play an earlier one, pass
its `key` as `version`, such as
`account_gym_new_game(bot["key"], token, version=bot["versions"][-1]["key"])`;
the game keeps that version to the end.

```python
import os
import time
from classicchess_api import ApplicationClient

token = os.environ["CLASSICCHESS_API_TOKEN"]  # a token with gym:read and gym:play
api = ApplicationClient()
created = api.account_gym_new_game("morphy", token, color="white", time="5+0")
if not created.ok:
    raise SystemExit(f"{created.status} {created.data}")
game = api.account_gym_move(created.data["id"], "e2e4", created.data["ply"], token).data
reply = api.account_gym_bot_move(game["id"], game["ply"], token)
while reply.status == 429 and reply.data["error"]["code"] == "capacity_exhausted":
    time.sleep(int(reply.retry_after or 1))
    reply = api.account_gym_bot_move(game["id"], game["ply"], token)
print(reply.status, reply.data.get("sans"), reply.data.get("clock"))
```

### Play puzzles

Puzzles come from master games and are for signed-in accounts only. Personal
tokens need `puzzles:read` to read puzzles, figures and history and
`puzzles:write` to save a result or reset; cookie sessions are never enough.
Each account has its own rotation: a puzzle solved on the first try leaves it
until reset, and a missed one stays. A puzzle carries the game so far
(`start_fen` and `lead`), the position (`fen`) and `solution.moves`: the
accepted solver moves at each point are the entries whose `after` equals the
moves played so far. Save each play once, `missed` at the first wrong move or
when the solution is shown and `solved` for a clean finish. A 404
`rotation_empty` reply means every puzzle is solved; reset to play them again.

This plays one puzzle in the terminal and saves how it went:

```python
import os
from classicchess_api import ApplicationClient

token = os.environ["CLASSICCHESS_API_TOKEN"]  # a token with puzzles:read and puzzles:write
api = ApplicationClient()
puzzle = api.account_puzzle_next(token)
if not puzzle.ok:
    raise SystemExit(f"{puzzle.status} {puzzle.data}")
print(puzzle.data["prompt"], puzzle.data["fen"])
played, result = [], "solved"
while True:
    accepted = {m["move"]: m["reply"] for m in puzzle.data["solution"]["moves"] if m["after"] == played}
    answer = input("Your move in UCI, such as e2e4: ").strip()
    if answer not in accepted:
        result = "missed"
        break
    played.append(answer)
    if accepted[answer] is None:
        break
    print("Reply:", accepted[answer])
    played.append(accepted[answer])
saved = api.account_puzzle_result(puzzle.data["id"], result, token)
print(saved.data["result"], saved.data["summary"])
```

The CLI reads `CLASSICCHESS_API_TOKEN` for account commands. Existing installations
can continue using their previous token environment variable as a fallback.
The original account helpers on `ClassicChessClient` remain available and raise
`ApiError` for unsuccessful HTTP responses.

### Website preferences

`account_preferences(token)` returns the website's Settings values and, under
`catalogs`, the choices each one accepts. `account_update_preferences(token,
board_theme="green", engine_lines=3)` changes only the named fields; when any
value is refused, nothing changes. Personal tokens need `preferences:read` to
read and `preferences:write` to change them.

### Edit and manage Notebooks

Reading Notebooks needs `notebooks:read`. Creating a Notebook and editing its
folders and chapters needs `notebooks:write`; the owner, managers and editors
may edit. Settings, collaborators, share links and deleting a Notebook need
`notebooks:manage`, and only the owner or a manager may use them.

Chapter saves are guarded by `content_version`. Read the chapter with
`account_notebook_chapter(uuid, chapter_id, token)` and send its
`content_version` with the new PGN. If the chapter changed in the meantime, the
reply is 409 `chapter_conflict` and nothing is saved; that reply carries no
chapter content, so read the chapter again, reapply your edit and save with the
new version. A save that succeeded also returns 409 when repeated, so never
retry a save blindly. Share links open the Notebook or chapter read-only for
anyone holding them; keep their URLs private.

### Mobile app devices

`mobile_home()` and `mobile_android_release()` need no credential.
`mobile_sign_in(username, password)` signs a device in with the account's
password and returns a device session bearer; a 401 `two_factor_required`
asks for `two_factor_code`. The SDK sends passwords only to an HTTPS or
loopback API origin. The session, device-list and app-settings methods accept
only that device session, not a personal token. Store the session as you would
any credential, and sign out with `mobile_sign_out(token)` when you are done.

## Timeouts, pagination and failures

Configure `ClassicChessClient(timeout=30, max_response_bytes=32 * 1024 * 1024)`
or `ApplicationClient` with the same options. Public reads return dictionaries
or text; `ApiError` exposes `status_code`, `code` and `retry_after` when available.
Pagination rejects repeated pages and links outside the original API collection.
The application transport sends no cookies, follows no redirects, and accepts
bearers only on private API paths over HTTPS or loopback development.
There are no automatic retries. Respect `Retry-After` and avoid blindly retrying writes.

## Collections and curated bulk additions

These helpers require a personal token with `library:read` for
lookup and `library:write` for creation, deletion and bulk additions.

`ApplicationClient.account_create_collection(name, token)` creates an empty
collection. `account_find_collection(name, token)` returns `count` and `results`
for the exact name ignoring case (empty when absent). Use the returned ID.
`account_delete_collection(id, token)` deletes that collection, including its
memberships, notes and progress, while retaining the underlying games.

`account_add_curated_games(id, filters, token, ...)` processes one batch, never
MasterDB or user imports. Each filter group's criteria are AND; groups are OR;
`exclude` subtracts groups. Filter keys are `players`, `events`, `since`, `until`,
`notable`, `q`, `theme`, `eco`, `result`, `annotation_q`, `annotation_author`, and
`annotation_book`. Players/events take lists of catalog slugs, OR within the
list; players match either participant, including event games. Use
`ClassicChessClient.public_players(...)` and `public_events(...)` to find slugs.
Years are inclusive, including games with unknown months or days.
`[{"notable": True}]` selects notable games across every curated player.

For every curated game played in 1920, save this as `collections_example.py`.
Set `CLASSICCHESS_API_TOKEN` in your private environment, then run
`python collections_example.py`. It creates a collection and adds all batches:

```python
import os
from classicchess_api import ApplicationClient

api = ApplicationClient()
token = os.environ["CLASSICCHESS_API_TOKEN"]
created = api.account_create_collection("Curated games from 1920", token)
if not created.ok:
    raise RuntimeError(created.data)
collection_id = created.data["id"]
filters = [{"since": 1920, "until": 1920}]
preview = api.account_add_curated_games(collection_id, filters, token, dry_run=True)
if not preview.ok:
    raise RuntimeError(preview.data)
print("Matched:", preview.data["matched"])
after = 0
while True:
    response = api.account_add_curated_games(collection_id, filters, token, after_game_id=after)
    if not response.ok:
        raise RuntimeError(response.data)
    print("Added this batch:", response.data["added"])
    if not response.data["has_more"]:
        break
    after = response.data["next_after_game_id"]
```

Keep filters/exclusions fixed while paging. `batch_size` is 1–1000 (default
1000); `after_game_id` starts at 0. `matched` counts the entire live selection;
`added`, `already_present` and `processed` count this batch. A dry run writes
nothing; begin actual additions at 0 afterward. Retrying a batch preserves
existing entries, notes and progress. The catalog is reevaluated on each call;
there is no frozen snapshot or automatic write retry. Stop on errors and retain
the last successfully returned cursor if you need to resume.

The same three helpers exist on `ClassicChessClient`, with `api_token=` instead
of positional `token`, and its usual exception-on-HTTP-error behavior. The
application client instead retains HTTP errors in `status`/`data`.
The standalone CLI retains its existing commands; these are library helpers.

### Study a collection

`account_collection_games(id, token, q=..., sort=...)` pages through a
collection as its page on the website does; each game carries its
`collection_item_id`, `note` and `studied` mark. This marks the Sicilian games
of the collection created above as studied:

```python
import os
from classicchess_api import ApplicationClient

api = ApplicationClient()
token = os.environ["CLASSICCHESS_API_TOKEN"]  # a token with library:read and library:write
found = api.account_find_collection("Curated games from 1920", token).data["results"]
collection_id = found[0]["id"]
page = api.account_collection_games(collection_id, token, q="Sicilian", sort="date_asc")
for game in page.data["results"]:
    api.account_update_collection_item(collection_id, game["collection_item_id"], token, studied=True)
    print(game["white"], "-", game["black"])
```

`account_create_plan(name, items, idempotency_key, token)` creates a study plan
of 1 to 100 games in manual order in one call; retrying with the same key
returns the plan already made. `account_reorder_collection` sets that order,
`account_plan_progress` reports what is studied, and
`account_remove_collection_game(id, game_slug, token)` removes a game by the slug
you added it with.

## Optional standalone command-line script

If you only want command-line commands without installing the Python package,
GitHub also provides [pull_api.py](https://github.com/Andromeda1957/classicchess-api-python/blob/main/pull_api.py). Save that file and run
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

## Notebook annotation search and offline puzzle attempts

These helpers require the matching server release. Search comments, glyphs and diagrams with
`account_notebook_annotation_search(token, q="king", kind="comments", notebook_uuid=None, limit=50, cursor=None)`.
It searches owned and directly invited Notebooks. Open a hit with its authorized PGN URL and
child-index `path`; keep its `content_version` to detect edits. Follow a non-null cursor,
including after an empty page. `complete=False` reports a bounded or partial scan.

`account_puzzle_next(token, result="missed")` and `account_puzzle_history(token, result="missed")`
review current incorrect attempts. `account_puzzle_batch(token, limit=25)` downloads 1–100
playable puzzles. Keep the returned account ID, generation and each puzzle revision in
account-separated private storage. Save each attempt locally before sending
`account_puzzle_result(id, result, token, event_id=uuid, generation=generation, revision=revision)`;
retry the same UUID and fields until acknowledged. Saved/duplicate responses acknowledge it;
409 stale_generation/stale_puzzle/event_conflict and 404 are terminal. Keep queued attempts
at their original reset generation. The SDK provides transport; your app owns durable storage,
network retry/backoff, account isolation and sign-out cleanup.
