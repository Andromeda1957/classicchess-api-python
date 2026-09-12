#!/usr/bin/env python3
"""Local CLI for pulling Classic Chess API data without curl."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .client import DEFAULT_BASE_URL
from .client import ApiError
from .client import ClassicChessClient
from .presentation import add_output_options
from .presentation import print_resource


def print_jq_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


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


def client_from_args(args: argparse.Namespace) -> ClassicChessClient:
    return ClassicChessClient(
        args.base_url,
        api_token=args.api_token or os.environ.get("CLASSICCHESS_API_TOKEN") or os.environ.get("ANDROMEDA_API_TOKEN"),
    )


def command_master_stats(args: argparse.Namespace) -> int:
    if args.year:
        raise ApiError("The stats endpoint does not support --year; use games.")

    if args.query:
        payload = client_from_args(args).master_stats(query=args.query.strip())
    else:
        if not args.player:
            raise ApiError("stats requires --query or --player.")
        mode = args.mode or ("head_to_head" if args.opponent else "summary")
        if mode == "head_to_head" and not args.opponent:
            raise ApiError("stats --mode head_to_head requires --opponent.")
        payload = client_from_args(args).master_stats(
            args.player,
            opponent=args.opponent,
            mode=mode,
        )

    print_jq_json(payload)
    return 0


def command_master_game(args: argparse.Namespace) -> int:
    print_jq_json(client_from_args(args).master_game(args.token))
    return 0


def command_master_pgn(args: argparse.Namespace) -> int:
    write_or_print_text(client_from_args(args).master_pgn(args.token), args.output)
    return 0


def command_master_export(args: argparse.Namespace) -> int:
    text = client_from_args(args).export_master_games(
        query=None if args.tokens else build_game_query(args),
        tokens=args.tokens,
        format=args.format,
        pgn_in_json=args.pgn_in_json,
    )
    write_or_print_text(text, args.output)
    return 0


def collect_master_game_pages(args: argparse.Namespace, query: str) -> dict[str, Any]:
    return client_from_args(args).master_games(
        query,
        page=args.page,
        page_size=args.page_size,
        all_pages=args.all_pages,
        limit=args.limit,
    )


def collect_public_game_pages(args: argparse.Namespace, query: str) -> dict[str, Any]:
    return client_from_args(args).public_games(
        query,
        archive_player=args.archive_player,
        archive_event=args.archive_event,
        since=args.since,
        until=args.until,
        sort=args.sort,
        page=args.page,
        page_size=args.page_size,
        all_pages=args.all_pages,
        limit=args.limit,
    )


def pgn_text_for_games(base_url: str, games: list[dict[str, Any]]) -> str:
    return ClassicChessClient(base_url).pgn_text_for_games(games)


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
    payload = client_from_args(args).public_players(args.query)
    print_resource(args, payload, 'players')
    return 0


def command_public_player(args):
    print_resource(args, client_from_args(args).public_player(args.player_slug), 'player')
    return 0


def command_public_bio(args):
    print_resource(args, client_from_args(args).public_player_bio(args.player_slug), 'bio')
    return 0


def command_public_notable(args):
    print_resource(args, client_from_args(args).public_notable_games(args.player_slug), 'notable')
    return 0


def command_public_events(args: argparse.Namespace) -> int:
    payload = client_from_args(args).public_events(args.query)
    print_resource(args, payload, 'events')
    return 0


def command_public_game(args: argparse.Namespace) -> int:
    print_jq_json(client_from_args(args).public_game(args.token))
    return 0


def command_public_pgn(args: argparse.Namespace) -> int:
    write_or_print_text(client_from_args(args).public_pgn(args.token), args.output)
    return 0


def command_public_export(args: argparse.Namespace) -> int:
    text = client_from_args(args).export_public_games(
        query="" if args.tokens else build_public_game_query(args),
        archive_player=args.archive_player,
        archive_event=args.archive_event,
        since=args.since,
        until=args.until,
        tokens=args.tokens,
        sort=args.sort,
        format=args.format,
        pgn_in_json=args.pgn_in_json,
    )
    write_or_print_text(text, args.output)
    return 0


def command_players(args: argparse.Namespace) -> int:
    print_jq_json(client_from_args(args).players(args.query, limit=args.limit))
    return 0


def command_account_me(args: argparse.Namespace) -> int:
    print_jq_json(client_from_args(args).account_me())
    return 0


def command_account_collections_list(args: argparse.Namespace) -> int:
    print_jq_json(client_from_args(args).account_collections())
    return 0


def command_account_collections_create(args: argparse.Namespace) -> int:
    print_jq_json(client_from_args(args).account_create_collection(args.name))
    return 0


def command_account_collections_import_player(args: argparse.Namespace) -> int:
    print_jq_json(
        client_from_args(args).account_import_public_player_games(
            name=args.name,
            collection_id=args.collection_id,
            archive_player=args.archive_player,
            since=args.since,
            until=args.until,
        )
    )
    return 0


def command_explorer(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    if args.sources:
        print_jq_json(client.explorer_sources())
        return 0
    payload = client.explorer(
        fen=args.fen,
        play=args.play,
        moves=args.moves,
        top_games=args.top_games,
        source_type=args.source_type,
        source_key=args.source_key,
    )
    print_jq_json(payload)
    return 0


def command_annotated_books(args: argparse.Namespace) -> int:
    print_resource(args, client_from_args(args).annotated_books(), 'books')
    return 0


def command_annotated_games(args: argparse.Namespace) -> int:
    print_jq_json(
        client_from_args(args).annotated_games(
            args.book_slug,
            page=args.page,
            page_size=args.page_size,
        )
    )
    return 0


def command_annotated_game(args: argparse.Namespace) -> int:
    print_jq_json(client_from_args(args).annotated_game(args.book_slug, args.game_slug))
    return 0


def command_annotated_pgn(args: argparse.Namespace) -> int:
    text = client_from_args(args).annotated_pgn(args.book_slug, args.game_slug)
    write_or_print_text(text, args.output)
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

    export = master_subparsers.add_parser("export", help="Bulk export MasterDB games as PGN or NDJSON.")
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

    export = public_subparsers.add_parser("export", help="Bulk export public archive games as PGN or NDJSON.")
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

    collections = account_subparsers.add_parser("collections", help="Manage account game collections.")
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
    import_player.add_argument("--collection-id", type=int, help="Existing collection ID to use instead of name.")
    import_player.add_argument("--since", type=int, help="Include games from this year or later.")
    import_player.add_argument("--until", type=int, help="Include games from this year or earlier.")
    import_player.set_defaults(func=command_account_collections_import_player)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pull Classic Chess API data from an explicit source.",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"API base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--api-token",
        help="Account API token. Defaults to CLASSICCHESS_API_TOKEN when omitted.",
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
