import json
from unittest.mock import patch

from django.test import SimpleTestCase

from .decision_roles import assign_responsibilities
from .decision_team_engine import make_state, prepare_brief, run_decision
from .test_decision_team import connections, fixture


class DecisionRoleTests(SimpleTestCase):
    def test_clarifier_uses_only_selected_members_and_preserves_assignment_on_resume(self):
        team = connections()
        for parallel in (False, True):
            with self.subTest(parallel=parallel):
                state = make_state(team, [])
                question = '比较实习的通勤和导师安排'
                context = [{'role': 'user', 'content': question}]
                proposals = [
                    {'member': 'p3', 'angle': 'resources', 'focus': '通勤'},
                    {'member': 'p1', 'angle': 'growth', 'focus': '导师安排'},
                    {'member': 'p2', 'angle': 'people', 'focus': '未出现在用户问题中的判断'},
                ]
                draft = {**fixture('clarifier'), 'assignments': proposals}
                with patch('delivery.decision_team_engine.generate_reply', return_value=json.dumps(draft)) as clarify:
                    prepare_brief(context, team, state, lambda *_: None, lambda *_: None)
                self.assertEqual(clarify.call_count, 1)
                self.assertEqual(json.loads(clarify.call_args.args[0][0]['content'])['member_ids'], ['p1', 'p2', 'p3'])
                state = json.loads(json.dumps(state))
                state.update(phase='confirmed', brief={'goal': question, 'assumptions': '', 'unknowns': ''})
                stage = ''
                received = {}

                def before(current, *_):
                    nonlocal stage
                    stage = current

                def reply(messages, *, connection, system_instruction, **kwargs):
                    if '只依据同一份问题说明独立分析' in system_instruction:
                        part = {'deepseek': 'p1', 'kimi': 'p2', 'qwen': 'p3'}[connection.provider]
                        received[part] = (json.loads(messages[0]['content']), system_instruction)
                        return json.dumps(fixture('analysis_' + part))
                    return json.dumps(fixture(stage))

                with patch('delivery.decision_team_engine.generate_reply', side_effect=reply):
                    run_decision(context, team, state, before, lambda *_: None, lambda _: None, parallel_analysis=parallel)
                self.assertEqual(state['phase'], 'completed')
                self.assertEqual([p['assignment']['angle'] for p in state['participants']], ['growth', 'people', 'resources'])
                self.assertEqual(received['p1'][0].pop('assigned_focus_from_user'), '导师安排')
                self.assertEqual(received['p3'][0].pop('assigned_focus_from_user'), '通勤')
                self.assertEqual(received['p1'][0], received['p2'][0])
                self.assertEqual(received['p1'][0], received['p3'][0])
                for part, own_label in zip(('p1', 'p2', 'p3'), ('成长机会', '各方影响', '成本与投入')):
                    packet, instruction = received[part]
                    self.assertIn(f'你的本轮职责是「{own_label}」', instruction)
                    self.assertNotIn('recommendation', packet)
                    self.assertNotIn('未出现在用户问题中的判断', str(packet) + instruction)
                    self.assertNotIn('导师安排', instruction)

    def test_invalid_assignments_fall_back_to_complementary_question_specific_roles(self):
        members = make_state(connections(), [], 2)['participants']
        for invalid in (None, {}, [{'member': 'p1', 'angle': 'people'}],
                [{'member': 'p1', 'angle': 'growth'}, {'member': 'p3', 'angle': 'evidence'}],
                [{'member': 'p1', 'angle': 'growth'}, {'member': 'p2', 'angle': 'growth'}],
                [{'member': 'p1', 'angle': '必须反对'}, {'member': 'p2', 'angle': ['invalid']}]):
            assign_responsibilities(members, '我们如何理解公平？', invalid)
            self.assertEqual([p['assignment']['angle'] for p in members], ['concepts', 'people'])
        assign_responsibilities(members, '软件部署需要哪些资源？')
        self.assertEqual([p['assignment']['angle'] for p in members], ['dependencies', 'feasibility'])
        state = make_state(connections(), [], 2)
        with patch('delivery.decision_team_engine.generate_reply', return_value=json.dumps(fixture('clarifier'))) as clarify:
            prepare_brief([{'role': 'user', 'content': '比较实习'}], connections(), state, lambda *_: None, lambda *_: None)
        self.assertEqual(json.loads(clarify.call_args.args[0][0]['content'])['member_ids'], ['p1', 'p2'])
        self.assertEqual([p['assignment']['angle'] for p in state['participants']], ['growth', 'criteria'])
