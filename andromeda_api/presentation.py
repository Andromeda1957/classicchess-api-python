"""Human-readable CLI views; JSON remains the default for pipes and files."""
import json
import sys


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
