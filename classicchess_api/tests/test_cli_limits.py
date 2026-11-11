"""The two distributed CLI entry points honor the same explicit result cap."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from classicchess_api import cli
from classicchess_api import client as packaged
from classicchess_api.tests.test_robustness import Response
from classicchess_api.tests.test_robustness import standalone_client


class CliLimitTests(unittest.TestCase):
    def test_single_page_limits_and_pgn_do_not_overfetch(self):
        for module in (cli, standalone_client()):
            for source in ('master', 'public'):
                for limit in (0, 1, 2):
                    for pgn in (False, True):
                        with self.subTest(client=module.__name__, source=source, limit=limit, pgn=pgn):
                            payload = {'results': [{'token': 'one'}, {'token': 'two'}]}
                            output = io.StringIO()
                            with patch.object(module, f'collect_{source}_game_pages', return_value=payload), \
                                    patch.object(module, 'pgn_text_for_games', return_value='') as fetch_pgn, \
                                    contextlib.redirect_stdout(output):
                                args = [source, 'games', '-q', 'Tal', '--limit', str(limit)] + (['--pgn'] if pgn else [])
                                self.assertEqual(module.main(args), 0)
                            if pgn:
                                self.assertEqual(len(fetch_pgn.call_args.args[1]), limit)
                            else:
                                self.assertEqual(len(json.loads(output.getvalue())['results']), limit)

    def test_exports_refuse_the_list_options_they_cannot_honor(self):
        # One export is one response of up to 300 games, so a cap or page it
        # accepted would be silently ignored and every game written.
        three_games = b'[Event "a"]\n\n1. e4 *\n\n' * 3
        standalone = standalone_client()
        for module, transport in ((cli, packaged), (standalone, standalone)):
            for source in ('master', 'public'):
                for option in (['--limit', '0'], ['--limit', '2'], ['--page', '2'], ['--page-size', '1'],
                               ['--all-pages'], ['--pgn']):
                    with self.subTest(client=module.__name__, source=source, option=option), \
                            patch.object(transport, 'urlopen', side_effect=lambda *_, **__: Response(three_games)) as wire, \
                            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as caught:
                            module.main([source, 'export', '-q', 'Tal', *option])
                        self.assertEqual(caught.exception.code, 2)
                        self.assertFalse(wire.called)
