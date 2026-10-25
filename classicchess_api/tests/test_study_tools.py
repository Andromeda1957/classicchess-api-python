import json
from unittest import TestCase
from unittest.mock import patch

from classicchess_api import ApiError
from classicchess_api import ApplicationClient
from classicchess_api.tests.test_application import Reply


class StudyToolsTests(TestCase):
    def test_offline_review_and_annotation_search_reach_authenticated_transport(self):
        api = ApplicationClient()
        event = 'f39112c7-f108-475f-9d44-98e1765bf538'
        with patch('classicchess_api.application.build_opener') as opener:
            opener.return_value.open.side_effect = lambda *a, **kw: Reply(b'{}')
            api.account_puzzle_batch('fixture', limit=50, result='missed')
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url.split('/api/v1/')[1], 'account/puzzles/batch/?limit=50&result=missed')
            api.account_puzzle_next('fixture', result='missed')
            self.assertIn('result=missed', opener.return_value.open.call_args.args[0].full_url)
            api.account_puzzle_history('fixture', result='missed')
            self.assertIn('result=missed', opener.return_value.open.call_args.args[0].full_url)
            api.account_puzzle_result('p-00000001', 'missed', 'fixture', event_id=event, generation=3, revision='a' * 64)
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(json.loads(req.data), {'result': 'missed', 'event_id': event, 'generation': 3, 'revision': 'a' * 64})
            self.assertEqual(req.get_header('Authorization'), 'Bearer fixture')
            api.account_notebook_annotation_search('fixture', q='king & pawn', kind='comments', cursor='a:b')
            req = opener.return_value.open.call_args.args[0]
            self.assertIn('q=king+%26+pawn', req.full_url)
            self.assertIn('cursor=a%3Ab', req.full_url)
            self.assertEqual(req.get_header('Authorization'), 'Bearer fixture')

    def test_partial_envelopes_and_unbounded_batches_never_send(self):
        api = ApplicationClient()
        with patch('classicchess_api.application.build_opener') as opener:
            for limit in (0, 101, True):
                with self.assertRaises(ApiError):
                    api.account_puzzle_batch('fixture', limit=limit)
            with self.assertRaises(ApiError):
                api.account_puzzle_result('p-00000001', 'missed', 'fixture', generation=1)
            opener.assert_not_called()
