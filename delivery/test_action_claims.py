"""A member's false claim of searching or testing fails the step; ordinary discussion does not."""
from django.test import SimpleTestCase

from .decision_team_engine import reject_unperformed_action_claims
from .provider import ProviderError


class UnperformedActionClaimTests(SimpleTestCase):
    def test_member_claims_of_unavailable_operations_are_rejected(self):
        for text in ('我已搜索最新政策，发现报名截止日期已延后。',
                     '我已经帮你搜索了两家公司的招聘信息。',
                     '我刚才在网上搜索了一下，A 公司评价不错。',
                     '我实际运行了代码，结果是 42。',
                     '我已对此进行检索，没有发现相关报道。',
                     '我已经测试过两种方案，A 更快。',
                     '我已部署到测试环境。',
                     '本人已亲自测试，效果稳定。',
                     '已联网搜索，最新政策为每周三天。',
                     '刚刚上网查询过，B 的地址在市中心。',
                     '已运行代码验证，结果正确。',
                     '经实测，A 方案每月节省 200 元。',
                     '{"recommendation":"我已检索相关资料，建议选 A","claims":[]}'):
            with self.subTest(text=text), self.assertRaises(ProviderError):
                reject_unperformed_action_claims(text)

    def test_facts_about_the_user_and_system_retrieval_are_allowed(self):
        for text in ('你提到系统已经部署到生产环境，那么回滚成本较高。',
                     'B 公司的产品已上线两年，口碑稳定。',
                     '如果你已经搜索过招聘信息，可以对比一下。',
                     '根据系统已检索到的资料 W1，政策已更新。',
                     '系统已经联网检索到 W1。',
                     '我已阅读系统检索到的 W1 资料。',
                     '我国已上线全国统一的报名平台。',
                     '我们已经上线的系统需要先评估迁移成本。',
                     '你告诉我已经部署好了，那就先做回滚预案。',
                     '我已经看过你提供的部署方案。',
                     '该药物已通过临床测试。',
                     '代码已测试通过，可以合并。',
                     '用户已运行代码，报错信息见资料。'):
            with self.subTest(text=text):
                reject_unperformed_action_claims(text)

    def test_negated_statements_are_allowed(self):
        for text in ('我没有联网，无法确认。', '我无法搜索或运行代码。',
                     '我没有实际测试，以下是推理。', '本轮未经实测，结论仅供参考。'):
            with self.subTest(text=text):
                reject_unperformed_action_claims(text)

    def test_quoted_first_person_material_is_not_treated_as_a_member_claim(self):
        for text in ('{"evidence":[{"source":"S1","quote":"我已部署过三个项目","scope":"简历"}]}',
                     '你在简历里写了“我已经测试过三套系统”，这一点面试时可能被追问。'):
            with self.subTest(text=text):
                reject_unperformed_action_claims(text)

    def test_human_review_claims_are_still_rejected(self):
        with self.assertRaises(Exception):
            reject_unperformed_action_claims('本轮结论已经过人工复核。')
