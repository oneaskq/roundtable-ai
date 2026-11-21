import os
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase

from workspace.web_sources import SourceFetchError
from .decision_team_engine import peer_clarification, prepare_brief
from .public_research import approved_query, lookup_public_sources, research_note
from .test_decision_team import connections


class PublicResearchTests(SimpleTestCase):
    @patch('delivery.decision_team_engine._call')
    def test_suggesting_public_keywords_does_not_send_them_to_the_second_member(self, call):
        conns = connections()
        state = {'participants': [], 'sources': []}
        context = [{'role': 'user', 'content': '请帮我看看材料'}]
        call.side_effect = [{'mode': 'quick', 'quick_reply': '先看看', 'goal': '查公开资料',
                             'public_search_query': '专用电价规则'}, {'question': ''}]
        draft = prepare_brief(context, conns, state, None, None)
        self.assertEqual(draft['mode'], 'discussion')
        self.assertEqual(draft['public_search_query'], '专用电价规则')
        peer_clarification(context, conns, state, draft, {'role': 'challenger'}, None, None)
        self.assertNotIn('专用电价规则', str(call.call_args.args[3]))
        self.assertNotIn('public_search_query', state)

    @patch('delivery.decision_team_engine._call')
    def test_private_model_suggestion_is_discarded_prior_to_consent(self, call):
        for query in ['学号：20250123 奖学金', 'user@example.org 岗位', '我叫李明 薪资']:
            call.return_value = {'goal': '查招聘信息', 'public_search_query': query}
            draft = prepare_brief([{'role': 'user', 'content': '帮我查招聘规则'}], connections(),
                                  {'participants': [], 'sources': []}, None, None)
            self.assertNotIn('public_search_query', draft)
            self.assertTrue(draft['public_search_requested'])

    @patch('delivery.decision_team_engine._call')
    def test_lookup_request_without_safe_keywords_offers_empty_field_only(self, call):
        call.return_value = {'mode': 'quick', 'quick_reply': '我来看看'}
        private = '帮我查最新岗位，我的学号是 12345678'
        draft = prepare_brief([{'role': 'user', 'content': private}], connections(),
                              {'participants': [], 'sources': []}, None, None)
        self.assertEqual(draft['mode'], 'discussion')
        self.assertTrue(draft['public_search_requested'])
        self.assertNotIn('public_search_query', draft)
        call.return_value = {'mode': 'discussion', 'goal': '总结'}
        ordinary = prepare_brief([{'role': 'user', 'content': '总结我的方案'}], connections(),
                                 {'participants': [], 'sources': []}, None, None)
        self.assertNotIn('public_search_requested', ordinary)
        call.return_value = {'mode': 'discussion', 'goal': '分析原文', 'public_search_query': '额外检索'}
        declined = prepare_brief([{'role': 'user', 'content': '请不要联网，只比较我给你的材料'}], connections(),
                                 {'participants': [], 'sources': []}, None, None)
        self.assertNotIn('public_search_requested', declined)
        self.assertNotIn('public_search_query', declined)

    def test_external_query_requires_explicit_consent_and_filters_obvious_identifiers(self):
        self.assertEqual(approved_query(None, None), '')
        self.assertEqual(approved_query('  太阳能  ', True), '太阳能')
        for value, consent in [('太阳能', False), ('太阳能', 1), ('a' * 161, True),
                               ('联系 user@example.org', True), ('附上\n原话', True)]:
            with self.subTest(value=value, consent=consent), self.assertRaises(ValueError):
                approved_query(value, consent)

    @patch.dict(os.environ, {'BRAVE_SEARCH_API_KEY': ''})
    @patch('delivery.public_research._get_json')
    def test_wikipedia_returns_attributed_original_text_and_retrieval_date(self, get_json):
        get_json.return_value = {'query': {'pages': [
            {'index': 2, 'title': '条目乙', 'extract': '来源原文乙'},
            {'index': 1, 'title': '条目甲', 'extract': '来源原文甲', 'lastrevid': 42}]}}
        info, sources = lookup_public_sources('太阳能', '维基百科')
        endpoint, headers, _ = get_json.call_args.args
        self.assertEqual(urlsplit(endpoint).netloc, 'zh.wikipedia.org')
        self.assertEqual(parse_qs(urlsplit(endpoint).query)['gsrsearch'], ['太阳能'])
        self.assertEqual(headers, {})
        self.assertEqual(info['status'], 'found')
        self.assertIn('百科背景', research_note(info))
        self.assertEqual([s['reference'] for s in sources], ['W1', 'W2'])
        self.assertEqual(sources[0]['content'], '来源原文甲')
        self.assertIn('oldid=42', sources[0]['url'])
        self.assertIn('retrieved_at', sources[0])
        self.assertNotIn('published_at', sources[0])
        self.assertNotIn('content', info['sources'][0])

    @patch.dict(os.environ, {'BRAVE_SEARCH_API_KEY': 'test-only'})
    @patch('delivery.public_research.fetch_public_source')
    @patch('delivery.public_research._get_json')
    def test_brave_only_uses_fetched_text_and_never_its_search_snippets(self, get_json, fetch):
        get_json.return_value = {'web': {'results': [
            {'url': 'https://example.org/a', 'description': '一句尚未核验的搜索摘要'},
            {'url': 'https://example.org/b', 'description': '第二条未核验摘要'}]}}
        fetch.side_effect = [SourceFetchError('unreadable'), {
            'name': '原页面', 'source_url': 'https://example.org/b', 'content': '页面正文',
            'metadata': {'extracted_at': '2026-01-01T00:00:00+00:00'}}]
        info, sources = lookup_public_sources('public terms', 'Brave Search')
        self.assertEqual(info['status'], 'partial')
        self.assertEqual([s['content'] for s in sources], ['页面正文'])
        self.assertEqual(fetch.call_count, 2)
        self.assertNotIn('test-only', str(info) + str(sources) + str(fetch.call_args_list))
        self.assertNotIn('test-only', get_json.call_args.args[0])
        self.assertIn('部分结果', research_note(info))
        fetch.side_effect = SourceFetchError('unreadable')
        info, sources = lookup_public_sources('public terms', 'Brave Search')
        self.assertEqual((info['status'], sources), ('unreadable', []))
        self.assertIn('摘要未用作证据', research_note(info))

    @patch.dict(os.environ, {'BRAVE_SEARCH_API_KEY': ''})
    @patch('delivery.public_research._get_json')
    def test_no_search_service_change_without_new_consent(self, get_json):
        info, sources = lookup_public_sources('public terms', 'Brave Search')
        self.assertEqual((info['status'], sources), ('provider_changed', []))
        get_json.assert_not_called()

    @patch.dict(os.environ, {'BRAVE_SEARCH_API_KEY': ''})
    @patch('delivery.public_research._get_json')
    def test_failure_and_empty_results_have_separate_truthful_statuses(self, get_json):
        get_json.side_effect = [TimeoutError, {}]
        for status in ['unavailable', 'empty']:
            info, sources = lookup_public_sources('public terms', '维基百科')
            self.assertEqual((info['status'], sources), (status, []))
            self.assertNotIn('读取了', research_note(info))

    @patch.dict(os.environ, {'BRAVE_SEARCH_API_KEY': ''})
    @patch('delivery.public_research._get_json')
    def test_unsafe_saved_keywords_or_missing_provider_never_leave_for_research(self, get_json):
        info, _ = lookup_public_sources('联系 user@example.org', '维基百科')
        self.assertEqual(info['status'], 'invalid_query')
        info, _ = lookup_public_sources('public', None)
        self.assertEqual(info['status'], 'provider_changed')
        get_json.assert_not_called()

    @patch.dict(os.environ, {'BRAVE_SEARCH_API_KEY': ''})
    @patch('delivery.public_research.TIMEOUT', 0.03)
    @patch('delivery.public_research._get_json')
    def test_deadline_returns_without_a_second_query_or_a_different_service(self, get_json):
        release = threading.Event()
        get_json.side_effect = lambda *args: (release.wait(2), {})[1]
        try:
            info, sources = lookup_public_sources('public terms', '维基百科')
            self.assertEqual((info['status'], sources), ('unavailable', []))
        finally:
            release.set()
        self.assertEqual(get_json.call_count, 1)
