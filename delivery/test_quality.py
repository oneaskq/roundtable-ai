"""Offline quality workflow; no external models or generated-code execution."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase

from workspace.views import export_data
from .models import CapabilityEvidence, QualityRound, QualitySample, RequestBudget
from .quality_cases import available_cases, case_snapshot, evaluate


class QualityCaseTests(SimpleTestCase):
    def test_cases_have_stable_versions_and_use_fictional_material(self):
        cases = available_cases()
        self.assertEqual(len(cases), 6)
        self.assertEqual(len({case['id'] for case in cases}), len(cases))
        self.assertTrue(all(case['revision'] == case_snapshot(case['id'])['revision'] for case in cases))
        self.assertTrue(all(len(case['rubric']) == 3 and case['version'] == 1 for case in cases))
        self.assertIsNone(case_snapshot('not-a-case'))

    def test_known_right_and_wrong_outputs_have_distinct_mechanical_checks(self):
        examples = {
            'arithmetic': ('323', '17×19=323'),
            'materials': ('{"conflict":true,"budget_values":[14000,12000],"final_budget":null,"location":"未确定"}',
                          '{"conflict":false,"budget_values":[12000,14000],"final_budget":12000,"location":"未确定"}'),
            'planning': ('{"independent_tasks":[{"id":"a","purpose":"调查人数"},{"id":"b","purpose":"比较场地"}],'
                         '"follow_up_task":{"id":"c","depends_on":["a","b"],"purpose":"汇总"},"acceptance":["不超过1500元"]}',
                         '{"independent_tasks":[],"follow_up_task":{},"acceptance":[]}'),
            'critique': ('{"independent_execution_evidence":false,"self_report_sufficient":false,"status":"未验证","next_step":"检查真实运行记录"}',
                         '{"independent_execution_evidence":true,"self_report_sufficient":true,"status":"已验证","next_step":"无"}'),
            'synthesis': ('{"recommendation":"B","total":1400,"other_total":1500}',
                          '{"recommendation":"A","total":1500,"other_total":1400}'),
            'verification': ('{"approved":false,"executed_cases":0,"reason":"缺少运行记录"}',
                             '{"approved":true,"executed_cases":0,"reason":"自报通过"}'),
        }
        for case_id, (correct, wrong) in examples.items():
            with self.subTest(case=case_id):
                self.assertTrue(all(check['passed'] for check in evaluate(case_id, correct)))
                self.assertFalse(all(check['passed'] for check in evaluate(case_id, wrong)))

    def test_invalid_json_duplicate_fields_and_booleans_do_not_count_as_numbers(self):
        for content in ('{"approved":false,"approved":true}', '{"approved":NaN}', '<script>alert(1)</script>'):
            self.assertFalse(evaluate('verification', content)[0]['passed'])
        values = evaluate('synthesis', '{"recommendation":"B","total":true,"other_total":1500}')
        self.assertFalse(values[2]['passed'])


class QualityApiTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(email='quality@example.test', password='Fixture-123!')
        self.other = get_user_model().objects.create_user(email='quality-other@example.test', password='Fixture-123!')
        self.client.force_login(self.owner)

    def post_json(self, path, body):
        return self.client.post(path, json.dumps(body), content_type='application/json')

    def create_round(self, case='arithmetic'):
        result = self.post_json('/api/quality/rounds', {'case_id': case})
        self.assertEqual(result.status_code, 201, result.content)
        return result.json()['round']

    def sample(self, round_id, name, output):
        return self.post_json(f'/api/quality/rounds/{round_id}/samples', {'model_label': name, 'output': output})

    def test_end_to_end_manual_comparison_never_calls_models_or_enables_capabilities(self):
        with patch('delivery.provider._request', side_effect=AssertionError('model transport forbidden')):
            self.assertEqual(self.client.get('/api/quality/cases').status_code, 200)
            round_ = self.create_round()
            round_id = round_['id']
            for label, output in [('Model Alpha v1', '323'), ('Model Beta v2', '17 × 19 = 323')]:
                response = self.sample(round_id, label, output)
                self.assertEqual(response.status_code, 201)
                self.assertTrue(all('model_label' not in sample for sample in response.json()['round']['samples']))
            result = self.post_json(f'/api/quality/rounds/{round_id}/start', {})
            self.assertEqual(result.status_code, 200)
            samples = result.json()['round']['samples']
            self.assertEqual([item['alias'] for item in samples], ['样本 A', '样本 B'])
            self.assertTrue(all(item['created_at'] for item in samples))
            self.assertTrue(all('model_label' not in item for item in samples))
            self.assertTrue(all(check['passed'] for check in samples[0]['checks']))
            self.assertFalse(all(check['passed'] for check in samples[1]['checks']))
            first = self.post_json(f'/api/quality/samples/{samples[0]["id"]}/review',
                                   {'ratings': {'accuracy': 2, 'coverage': 2, 'usefulness': 2}, 'note': '简洁'})
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()['round']['status'], 'reviewing')
            self.assertTrue(all('model_label' not in item for item in first.json()['round']['samples']))
            second = self.post_json(f'/api/quality/samples/{samples[1]["id"]}/review',
                                    {'ratings': {'accuracy': 2, 'coverage': 1, 'usefulness': 1}, 'note': '包含额外解释'})
            self.assertEqual(second.status_code, 200)
            finished = second.json()['round']
            self.assertEqual(finished['status'], 'completed')
            self.assertEqual([item['model_label'] for item in finished['samples']], ['Model Alpha v1', 'Model Beta v2'])
            self.assertEqual(finished['source'], 'manual_unverified')
            self.assertIn('未经验证', finished['notice'])
        self.assertFalse(RequestBudget.objects.exists())
        self.assertFalse(CapabilityEvidence.objects.exists())

    def test_workflow_freezes_case_and_rejects_late_changes(self):
        round_ = self.create_round('verification')
        self.assertEqual(self.post_json(f'/api/quality/rounds/{round_["id"]}/start', {}).status_code, 409)
        self.sample(round_['id'], 'A model', '{"approved":false,"executed_cases":0,"reason":"未运行"}')
        self.sample(round_['id'], 'B model', '{"approved":true,"executed_cases":0,"reason":"自报"}')
        result = self.post_json(f'/api/quality/rounds/{round_["id"]}/start', {}).json()['round']
        self.assertEqual(result['case']['revision'], round_['case']['revision'])
        self.assertEqual(self.sample(round_['id'], 'C model', 'late').status_code, 409)
        sample_id = result['samples'][0]['id']
        for ratings in ({'accuracy': True, 'coverage': 1, 'usefulness': 2},
                        {'accuracy': 3, 'coverage': 1, 'usefulness': 2}, {'accuracy': 1}):
            self.assertEqual(self.post_json(f'/api/quality/samples/{sample_id}/review',
                                            {'ratings': ratings, 'note': ''}).status_code, 400)
        rating = {'ratings': {'accuracy': 0, 'coverage': 1, 'usefulness': 2}, 'note': ''}
        self.assertEqual(self.post_json(f'/api/quality/samples/{sample_id}/review', rating).status_code, 200)
        self.assertEqual(self.post_json(f'/api/quality/samples/{sample_id}/review', rating).status_code, 409)

    def test_account_isolation_and_personal_export(self):
        round_ = self.create_round()
        self.sample(round_['id'], 'My model', '323')
        sample_id = QualitySample.objects.get().pk
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f'/api/quality/rounds/{round_["id"]}').status_code, 404)
        self.assertEqual(self.client.delete(f'/api/quality/rounds/{round_["id"]}').status_code, 404)
        self.assertEqual(self.sample(round_['id'], 'Other', '323').status_code, 404)
        self.assertEqual(self.post_json(f'/api/quality/samples/{sample_id}/review',
            {'ratings': {'accuracy': 1, 'coverage': 1, 'usefulness': 1}, 'note': ''}).status_code, 404)
        self.assertEqual(export_data(self.other)['quality_rounds'], [])
        exported = export_data(self.owner)['quality_rounds']
        self.assertEqual(exported[0]['samples'][0]['model_label'], 'My model')
        self.assertEqual(exported[0]['samples'][0]['output'], '323')

    def test_auth_csrf_and_user_deletion_are_enforced(self):
        round_ = self.create_round()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post('/api/quality/rounds', json.dumps({'case_id': 'arithmetic'}),
                                         content_type='application/json').status_code, 403)
        self.assertEqual(csrf_client.delete(f'/api/quality/rounds/{round_["id"]}').status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get('/api/quality/cases').status_code, 401)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.delete(f'/api/quality/rounds/{round_["id"]}').status_code, 200)
        self.assertFalse(QualityRound.objects.exists())

    def test_administrator_must_complete_mfa_before_using_quality_data(self):
        self.owner.is_superuser = True
        self.owner.save(update_fields=['is_superuser'])
        self.assertEqual(self.client.get('/api/quality/cases').status_code, 403)
        self.assertEqual(self.post_json('/api/quality/rounds', {'case_id': 'arithmetic'}).status_code, 403)
        self.assertFalse(QualityRound.objects.exists())

    def test_input_limits_secret_like_names_and_duplicate_labels(self):
        round_ = self.create_round()
        for name, output in [('sk-secret-example', '323'), ('A', ''), ('A', 'x' * 12001)]:
            self.assertEqual(self.sample(round_['id'], name, output).status_code, 400)
        self.assertEqual(self.sample(round_['id'], 'Model A', '323').status_code, 201)
        self.assertEqual(self.sample(round_['id'], 'model a', '323').status_code, 409)
        self.assertEqual(self.sample(round_['id'], 'Model B', '323').status_code, 201)
        self.assertEqual(self.sample(round_['id'], 'Model C', '323').status_code, 201)
        self.assertEqual(self.sample(round_['id'], 'Model D', '323').status_code, 409)

    def test_changed_case_cannot_score_against_old_frozen_prompt(self):
        round_ = self.create_round()
        with patch('delivery.quality_views.case_snapshot', return_value={'revision': '0' * 64}):
            response = self.sample(round_['id'], 'Model A', '323')
        self.assertEqual(response.status_code, 409)
        self.assertFalse(QualitySample.objects.exists())
