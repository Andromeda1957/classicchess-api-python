"""The two distributed CLI entry points honor the same explicit result cap."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from classicchess_api import cli
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

    def test_negative_limit_is_rejected_before_fetch(self):
        for module in (cli, standalone_client()):
            for source in ('master', 'public'):
                with self.subTest(client=module.__name__, source=source), \
                        patch.object(module, f'collect_{source}_game_pages', return_value={'results': []}) as fetch, \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        module.main([source, 'games', '-q', 'Tal', '--limit', '-1'])
                    self.assertEqual(error.exception.code, 2)
                    fetch.assert_not_called()
