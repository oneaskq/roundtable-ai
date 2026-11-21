"""Length planning is advisory and only applies to unambiguous user requests."""
from django.test import SimpleTestCase

from .text_guidance import TEXT_CONSTRAINT_GUIDANCE, text_guidance_for, text_guidance_for_context


class TextGuidanceTests(SimpleTestCase):
    def test_total_range_is_distributed_across_all_requested_items(self):
        guidance = text_guidance_for('写四条建议，合计200至320个汉字。')
        self.assertIn('总计约260个汉字', guidance)
        self.assertIn('分成4项时每项平均约65个汉字', guidance)
        self.assertIn('不是实际计数', guidance)

        original = ('为第一次去自习室的学生写三条中文建议，主题分别是计划、安静、休息。'
                    '只输出三行，分别以1.、2.、3.开头，总计120至180个汉字，不加标题。')
        guidance = text_guidance_for(original)
        self.assertIn('总计约150个汉字', guidance)
        self.assertIn('分成3项时每项平均约50个汉字', guidance)

    def test_ambiguous_or_quoted_constraints_do_not_add_a_budget(self):
        cases = (
            '写三条建议，每条50字，总计120至180个汉字。',
            '写三条建议，只输出四行，总计120至180个汉字。',
            '分析材料“总计120至180个汉字，分三条”，说明是否合理。',
            '写三条，总计120至180个汉字；另写三条，合计200至300个汉字。',
            '写三条建议，总计180至120个汉字。',
            '写三条建议，总计1至5个汉字。',
            '写三条建议，大约150个汉字。',
            '写三条建议，总计120至180个汉字。' + '材料' * 1000,
            None,
        )
        for request in cases:
            with self.subTest(request=request):
                self.assertEqual(text_guidance_for(request), TEXT_CONSTRAINT_GUIDANCE)

    def test_only_the_latest_user_message_supplies_a_new_budget(self):
        context = [
            {'role': 'user', 'content': '写四条，合计200至320个汉字。'},
            {'role': 'assistant', 'content': '写三条，总计120至180个汉字。'},
            {'role': 'user', 'content': '只改一个错字。'},
        ]
        self.assertEqual(text_guidance_for_context(context), TEXT_CONSTRAINT_GUIDANCE)
        context[-1]['content'] = '写两条，总计100至140个汉字。'
        guidance = text_guidance_for_context(context)
        self.assertIn('总计约120个汉字', guidance)
        self.assertIn('分成2项时每项平均约60个汉字', guidance)
